#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Static checks on the CloudFormation templates and their nested-stack wiring.

Every failure here is something CloudFormation would otherwise only report
part-way through a deploy, after resources have already been created:

  1. Templates parse.
  2. Every ``!GetAtt <Stack>.Outputs.<Name>`` resolves to an output the child
     actually declares. A typo yields a create-time failure.
  3. Every parameter the parent passes exists in the child. CloudFormation
     hard-fails on an unknown parameter.
  4. Every child parameter without a ``Default`` is supplied by the parent.
  5. No dependency cycles among the nested stacks.
  6. Every ``${TemplateBaseUrl}/<child>.yaml`` has a template on disk that
     ``deployment/build-s3-dist.sh`` can stage. This is how the missing
     ``agent.yaml`` shows up: master.yaml points AgentStack at ``agent.yaml``,
     but that file is staged from ``backend/template.yaml`` and does not exist
     under ``infrastructure/``.

Usage: python3 deployment/check-templates.py     (from the repository root)
"""

import glob
import os
import pathlib
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    print("pyyaml is required: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MASTER = "infrastructure/master.yaml"
# master.yaml's AgentStack TemplateURL is agent.yaml, but the template lives here.
STAGED_FROM = {"agent.yaml": "backend/template.yaml"}

# Characters EC2 accepts in a security-group rule description. Anything outside this set
# is rejected when the rule is submitted, which is at deploy time, not at template
# validation. The error names the security group but not the rule, so a single em-dash in
# a comment-like description reads as an unexplained rollback.
EC2_DESCRIPTION_CHARS = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    ". _-:/()#,@[]+=&;{}!$*"
)


class DuplicateKey(Exception):
    """A mapping key appears twice - YAML keeps the last, so the first vanishes."""


class Loader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form intrinsics.

    Also rejects duplicate mapping keys. YAML resolves a duplicate by keeping the
    last occurrence, and both PyYAML and CloudFormation do so silently, so a second
    `Conditions:` block simply erases the first. That shipped here: task 7 added a
    `Conditions:` section to storage.yaml which already had one further down, and the
    only symptom was a mid-deploy "Condition HasEmailSender is not defined" after
    several stacks had already been created.
    """

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if isinstance(key, str):
                if key in seen:
                    raise DuplicateKey(
                        f"duplicate key {key!r} at line {key_node.start_mark.line + 1} "
                        "- YAML keeps the last, so the earlier one is silently discarded"
                    )
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _passthrough(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


Loader.add_multi_constructor("!", _passthrough)


def main() -> int:
    if not os.path.exists(MASTER):
        print(f"❌ {MASTER} not found - run this from the repository root.", file=sys.stderr)
        return 2

    problems = 0
    docs = {}

    # ── 1. parse ──────────────────────────────────────────────────────────────
    print("── parse ──")
    paths = sorted(glob.glob("infrastructure/*.yaml")) + sorted(glob.glob("backend/*.yaml"))
    for path in paths:
        try:
            with open(path) as fh:
                docs[path] = yaml.load(fh, Loader=Loader) or {}  # nosec B506 - Loader subclasses SafeLoader, adding only CloudFormation tags and duplicate-key detection
        except DuplicateKey as exc:
            print(f"  FAIL {path}: {exc}")
            problems += 1
        except Exception as exc:  # noqa: BLE001 - report any malformed template
            print(f"  FAIL {path}: {exc}")
            problems += 1
    print(f"  {len(docs)} templates parsed, {problems} failure(s)")

    if MASTER not in docs:
        print(f"❌ {MASTER} did not parse; cannot continue.", file=sys.stderr)
        return 1

    master = docs[MASTER]
    master_text = open(MASTER).read()
    resources = master.get("Resources") or {}

    # ── resolve each nested stack to a template on disk ───────────────────────
    stack_file, missing_templates = {}, []
    for name, body in resources.items():
        if not isinstance(body, dict) or body.get("Type") != "AWS::CloudFormation::Stack":
            continue
        url = str((body.get("Properties") or {}).get("TemplateURL", ""))
        match = re.search(r"([A-Za-z0-9._-]+\.yaml)", url)
        if not match:
            continue
        child = match.group(1)
        candidate = STAGED_FROM.get(child, f"infrastructure/{child}")
        if candidate in docs:
            stack_file[name] = candidate
        else:
            missing_templates.append((name, child))

    print("\n── nested stacks ──")
    for name, path in sorted(stack_file.items()):
        note = f"  (staged as {[k for k, v in STAGED_FROM.items() if v == path][0]})" \
            if path in STAGED_FROM.values() else ""
        print(f"  {name:20s} -> {path}{note}")
    for name, child in missing_templates:
        print(f"  FAIL {name}: references {child}, which no template on disk provides")
        problems += 1

    # ── 2. output references ──────────────────────────────────────────────────
    print("\n── output references ──")
    bad = 0
    refs = set(re.findall(r"!GetAtt\s+(\w+)\.Outputs\.(\w+)", master_text))
    for stack, output in sorted(refs):
        path = stack_file.get(stack)
        if not path:
            print(f"  FAIL {stack}.Outputs.{output}: unknown nested stack")
            bad += 1
        elif output not in (docs[path].get("Outputs") or {}):
            print(f"  FAIL {stack}.Outputs.{output}: not declared in {path}")
            bad += 1
    print(f"  {len(refs)} references checked, {bad} broken")
    problems += bad

    # ── 3 + 4. parameter wiring ───────────────────────────────────────────────
    print("\n── parameters ──")
    bad = 0
    for stack, path in sorted(stack_file.items()):
        passed = set(((resources[stack].get("Properties") or {}).get("Parameters") or {}))
        child_params = docs[path].get("Parameters") or {}

        for name in sorted(passed - set(child_params)):
            print(f"  FAIL {stack}: passes '{name}', which {path} does not declare")
            bad += 1

        required = {
            n for n, b in child_params.items()
            if isinstance(b, dict) and "Default" not in b
        }
        for name in sorted(required - passed):
            print(f"  FAIL {stack}: {path} requires '{name}' (no Default), parent does not pass it")
            bad += 1

        if not (passed - set(child_params)) and not (required - passed):
            print(f"  ok   {stack:20s} {len(passed)} passed / {len(child_params)} declared")
    print(f"  {bad} problem(s)")
    problems += bad

    # ── 5. dependency cycles ──────────────────────────────────────────────────
    print("\n── dependency graph ──")
    edges = {}
    for stack in stack_file:
        body = yaml.dump(resources[stack])
        deps = {d for d in re.findall(r"!GetAtt\s+(\w+)\.Outputs\.", body) if d in stack_file}
        # yaml.dump loses the short form, so also scan the raw block.
        block = re.search(
            rf"^  {stack}:\n(.*?)(?=^  \w+:\n|\Z)", master_text, re.S | re.M
        )
        if block:
            deps |= {d for d in re.findall(r"!GetAtt\s+(\w+)\.Outputs\.", block.group(1))
                     if d in stack_file}
            depends_on = re.search(r"^\s*DependsOn:\s*(.+)$", block.group(1), re.M)
            if depends_on:
                deps |= {d for d in re.findall(r"\w+", depends_on.group(1)) if d in stack_file}
        deps.discard(stack)
        edges[stack] = deps

    colour = dict.fromkeys(edges, 0)  # 0 unvisited, 1 in progress, 2 done
    cycles, order = [], []

    def visit(node, path):
        colour[node] = 1
        for nxt in sorted(edges[node]):
            if colour[nxt] == 1:
                cycles.append(path + [node, nxt])
            elif colour[nxt] == 0:
                visit(nxt, path + [node])
        colour[node] = 2
        order.append(node)

    for node in sorted(edges):
        if colour[node] == 0:
            visit(node, [])

    for cycle in cycles:
        print("  FAIL cycle: " + " -> ".join(cycle))
        problems += 1
    if not cycles:
        print("  no cycles")
        print("  create order: " + " -> ".join(order))

    # ── 5b. every referenced condition is defined ─────────────────────────────
    # The exact error CloudFormation reported when a duplicate `Conditions:` block
    # erased the first one: "Condition HasEmailSender is not defined". Cheap to check
    # directly, and it does not depend on the duplicate-key detection above catching
    # the cause.
    print("\n── conditions ──")
    bad = 0
    for path in sorted(docs):
        text = open(path).read()
        defined = set((docs[path].get("Conditions") or {}))
        referenced = set(re.findall(r"^\s*Condition:\s*([A-Za-z][A-Za-z0-9]*)\s*$",
                                    text, re.M))
        # Both !If spellings: inline `!If [Cond, a, b]` and the block form
        #   !If
        #     - Cond
        # Missing the block form made real uses look unused, which would have taught
        # people to ignore these notes.
        referenced |= set(re.findall(r"!If\s*\[\s*([A-Za-z][A-Za-z0-9]*)", text))
        referenced |= set(re.findall(r"!If\s*\n\s*-\s*([A-Za-z][A-Za-z0-9]*)", text))
        referenced |= set(re.findall(r"Fn::If:\s*\n\s*-\s*([A-Za-z][A-Za-z0-9]*)", text))
        for name in sorted(referenced - defined):
            print(f"  FAIL {path}: uses condition '{name}', which it does not define")
            bad += 1
        unused = defined - referenced
        if unused:
            print(f"  note {path}: defines unused condition(s) "
                  f"{', '.join(sorted(unused))}")
    if not bad:
        print("  ok   every referenced condition is defined")
    problems += bad

    # ── 6. the Makefile agrees with master.yaml's parameters ──────────────────
    # Two failure modes, one loud and one silent:
    #   * a parameter the Makefile passes but master.yaml does not declare -
    #     CloudFormation rejects the whole deploy;
    #   * a parameter master.yaml declares with no default that the Makefile does
    #     not pass - same outcome.
    # The quiet one is a parameter that exists and is simply never supplied, so the
    # feature behind it can never be switched on from `make deploy`. That is exactly
    # how EmailSenderAddress shipped unreachable. Those are listed, not failed,
    # because most of them are deliberately left at their defaults.
    print("\n── Makefile parameters ──")
    bad = 0
    makefile = REPO_ROOT / "Makefile"
    if not makefile.exists():
        print("  --   no Makefile, skipped")
    else:
        text = makefile.read_text()
        # Scope to the deploy that targets master.yaml. The recipe runs TWO
        # `aws cloudformation deploy` commands - a us-east-1 CloudFront-WAF edge stack
        # (edge-waf.yaml, which takes its own AppRegion/ResourcePrefix) and then the main
        # master.yaml - so matching the first --parameter-overrides would check the edge
        # stack's parameters against master.yaml. Anchor on the master template file.
        block = re.search(
            r"--template-file\s+infrastructure/master\.yaml.*?--parameter-overrides"
            r"(.*?)(?=--no-fail-on-empty-changeset|\Z)",
            text, re.S)
        if not block:
            print("  --   no master.yaml --parameter-overrides block found, skipped")
        else:
            passed = set(re.findall(r"^\t+\$?\(?[a-z ]*\)?\s*([A-Za-z][A-Za-z0-9]*)=",
                                    block.group(1), re.M))
            passed |= set(re.findall(r"\b([A-Z][A-Za-z0-9]*)=\$", block.group(1)))
            declared = master.get("Parameters") or {}
            required = {
                n for n, b in declared.items()
                if isinstance(b, dict) and "Default" not in b
            }

            for name in sorted(passed - set(declared)):
                print(f"  FAIL Makefile passes '{name}', which master.yaml does not declare")
                bad += 1
            for name in sorted(required - passed):
                print(f"  FAIL master.yaml requires '{name}' (no default); Makefile does not pass it")
                bad += 1
            if not bad:
                print(f"  ok   {len(passed)} passed, all declared; "
                      f"all {len(required)} required parameter(s) supplied")

            never = sorted(set(declared) - passed)
            if never:
                print(f"  note {len(never)} parameter(s) never passed, left at their defaults:")
                print("       " + ", ".join(never))
    problems += bad

    # ── 7. staging completeness ───────────────────────────────────────────────
    print("\n── staging ──")
    bad = 0
    children = sorted(set(re.findall(r"TemplateBaseUrl\}/([A-Za-z0-9._-]+\.yaml)", master_text)))
    for child in children:
        source = STAGED_FROM.get(child, f"infrastructure/{child}")
        if os.path.exists(source):
            via = f" (from {source})" if child in STAGED_FROM else ""
            print(f"  ok   {child}{via}")
        else:
            print(f"  FAIL {child}: no source template at {source}")
            bad += 1
    print(f"  {len(children)} nested templates, {bad} unstageable")
    problems += bad

    # ── 9. nested-stack parameters that would ship empty ──────────────────────
    # A child parameter the parent never passes falls back to the child's default. Where
    # that default is the empty string and the child uses the value directly - in an IAM
    # policy ARN, or as an environment variable - the result is a resource that deploys
    # cleanly and then denies itself access, reported far from the cause.
    #
    # An empty default is legitimate when it is a sentinel: the child defines a Condition
    # comparing the parameter to '' and switches behaviour on it (an optional KMS key, for
    # instance). That case is recognised and allowed, which keeps this check quiet enough
    # to be worth having.
    print("\n── nested-stack parameters ──")
    bad = 0
    checked = 0
    for name, body in (master.get("Resources") or {}).items():
        if not isinstance(body, dict) or body.get("Type") != "AWS::CloudFormation::Stack":
            continue
        source = stack_file.get(name)
        if not source or source not in docs:
            continue
        child = docs[source]
        passed = set(((body.get("Properties") or {}).get("Parameters") or {}).keys())
        declared = child.get("Parameters") or {}
        # Parameters a Condition compares to '' are sentinels: empty means "off".
        conditions_text = yaml.dump(child.get("Conditions") or {}, default_flow_style=False)
        for pname, spec in declared.items():
            if pname in passed:
                continue
            spec = spec if isinstance(spec, dict) else {}
            default = spec.get("Default")
            empty = ("Default" not in spec) or (
                isinstance(default, str) and default.strip() == "")
            if not empty:
                continue
            checked += 1
            if pname in conditions_text:
                continue  # empty is a deliberate sentinel this template tests for
            why = ("has no Default" if "Default" not in spec
                   else "defaults to the empty string")
            print(f"  FAIL {name} ({source}): {pname} is never passed and {why}, "
                  f"with no Condition treating empty as a valid state")
            bad += 1
    if bad == 0:
        print(f"  ok   every unpassed child parameter has a usable default "
              f"({checked} empty default(s), all sentinels)")
    problems += bad

    # ── 8. EC2 description fields ─────────────────────────────────────────────
    # EC2 limits a security-group description to 255 characters from a narrow set, and
    # enforces it when the resource is submitted rather than at template validation. The
    # resulting error names the security group but not the field, so a single em-dash in
    # otherwise ordinary prose reads as an unexplained rollback minutes into a deploy.
    #
    # Three shapes carry the constraint: a group's own GroupDescription, the rule
    # descriptions nested inside it, and standalone SecurityGroupIngress/Egress resources.
    print("\n── EC2 description fields ──")
    bad = 0
    checked = 0

    def check_ec2_description(path, where, value):
        """Report a description EC2 would reject. Returns 1 if it is a problem."""
        if not isinstance(value, str):
            return 0  # a !Sub or !Ref is resolved at deploy time
        stray = sorted({c for c in value if c not in EC2_DESCRIPTION_CHARS})
        if stray:
            shown = ", ".join(f"{c!r} U+{ord(c):04X}" for c in stray)
            print(f"  FAIL {path} {where}: EC2 rejects {shown}")
            print(f"       {value}")
            return 1
        if len(value) > 255:
            print(f"  FAIL {path} {where}: {len(value)} characters, the limit is 255")
            return 1
        return 0

    for path, doc in sorted(docs.items()):
        for rname, body in (doc.get("Resources") or {}).items():
            if not isinstance(body, dict):
                continue
            rtype = body.get("Type")
            props = body.get("Properties") or {}
            if not isinstance(props, dict):
                continue

            if rtype == "AWS::EC2::SecurityGroup":
                if isinstance(props.get("GroupDescription"), str):
                    checked += 1
                    bad += check_ec2_description(
                        path, f"{rname}.GroupDescription", props["GroupDescription"])
                for prop in ("SecurityGroupIngress", "SecurityGroupEgress"):
                    for rule in props.get(prop) or []:
                        if not isinstance(rule, dict):
                            continue
                        if isinstance(rule.get("Description"), str):
                            checked += 1
                            bad += check_ec2_description(
                                path, f"{rname}.{prop}", rule["Description"])

            elif rtype in ("AWS::EC2::SecurityGroupIngress",
                           "AWS::EC2::SecurityGroupEgress"):
                if isinstance(props.get("Description"), str):
                    checked += 1
                    bad += check_ec2_description(
                        path, f"{rname}.Description", props["Description"])

    if bad == 0:
        print(f"  ok   {checked} description(s) use only characters EC2 accepts")
    problems += bad

    print()
    print("PASS" if problems == 0 else f"FAIL - {problems} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
