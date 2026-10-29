#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Assert the CloudFormation templates and the application agree on resource names.

Every physical name is `<ResourcePrefix>-<suffix>`. The suffixes live in two places -
the templates, and `backend/tools/resource_names.py` - and if they drift the stack
still deploys cleanly and then fails at request time with a DynamoDB
`ResourceNotFoundException`. That is an expensive way to find a typo, so this turns
the drift into a failed check instead.

Run from the repository root:

    python3 deployment/check-resource-names.py
"""
import ast
import glob
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent


class CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation's short-form intrinsics."""


def _passthrough(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {"__tag__": tag_suffix, "value": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {"__tag__": tag_suffix, "value": loader.construct_sequence(node)}
    return {"__tag__": tag_suffix, "value": loader.construct_mapping(node)}


CfnLoader.add_multi_constructor("!", _passthrough)

failures: list[str] = []
notes: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)
    print(f"  FAIL {msg}")


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


# ── load ─────────────────────────────────────────────────────────────────────
templates = {}
for path in sorted(glob.glob(str(REPO / "infrastructure" / "*.yaml"))) + [
    str(REPO / "backend" / "template.yaml")
]:
    rel = str(Path(path).relative_to(REPO))
    try:
        with open(path) as fh:
            templates[rel] = yaml.load(fh, Loader=CfnLoader) or {}  # nosec B506 - CfnLoader subclasses SafeLoader, adding only CloudFormation tag constructors
    except Exception as exc:  # noqa: BLE001
        fail(f"{rel} does not parse: {exc}")

sys.path.insert(0, str(REPO / "backend"))
from tools.resource_names import TABLES, FUNCTIONS  # noqa: E402

MASTER = "infrastructure/master.yaml"
master_res = (templates.get(MASTER) or {}).get("Resources") or {}


def scalar(v):
    """Reduce a CloudFormation value to a plain string when possible."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict) and "__tag__" in v and isinstance(v["value"], str):
        return v["value"]
    return None


def master_param_value(param: str, child_template: str):
    """What master.yaml passes for `param` **to the stack that owns child_template**.

    Scoping to the owning stack is the whole point. Searching every nested stack and
    returning the first match would let a `!Ref` in template A be validated against a
    value master passes only to template B, so a template could name a resource from a
    parameter it never receives and still pass.
    """
    for body in master_res.values():
        if not isinstance(body, dict) or body.get("Type") != "AWS::CloudFormation::Stack":
            continue
        url = str(((body.get("Properties") or {}).get("TemplateURL")) or "")
        if isinstance((body.get("Properties") or {}).get("TemplateURL"), dict):
            url = str((body["Properties"]["TemplateURL"] or {}).get("value", ""))
        child = re.search(r"([A-Za-z0-9._-]+\.yaml)", url)
        if not child:
            continue
        # agent.yaml is staged from backend/template.yaml by build-s3-dist.sh, so the
        # source to check is that file rather than an infrastructure/agent.yaml.
        owns = child.group(1)
        expected = "backend/template.yaml" if owns == "agent.yaml" else f"infrastructure/{owns}"
        if expected != child_template:
            continue
        passed = (body.get("Properties") or {}).get("Parameters") or {}
        if param in passed:
            return scalar(passed[param])
    return None


def suffix_of(value: str, label: str):
    """Extract `x` from a `${ResourcePrefix}-x` expression."""
    m = re.fullmatch(r"\$\{ResourcePrefix\}-(.+)", value or "")
    if m:
        return m.group(1)
    fail(f"{label}: {value!r} is not derived from ResourcePrefix")
    return None


def resources_of(kind: str):
    for rel, doc in templates.items():
        for name, body in ((doc or {}).get("Resources") or {}).items():
            if isinstance(body, dict) and body.get("Type") == kind:
                yield rel, name, (body.get("Properties") or {})


print("\n── DynamoDB table suffixes ──")
tmpl_tables = set()
for rel, name, props in resources_of("AWS::DynamoDB::Table"):
    raw = scalar(props.get("TableName"))
    if raw is None:
        fail(f"{rel}/{name}: TableName is not a simple value")
        continue
    # `TableName: !Ref DbConfigTable` - resolve through what master passes
    if isinstance(props.get("TableName"), dict) and \
            props["TableName"].get("__tag__") == "Ref":
        resolved = master_param_value(raw, rel)
        if resolved is None:
            fail(f"{rel}/{name}: TableName refs parameter {raw!r}, which master.yaml "
                 f"never passes to the stack that owns {rel}")
            continue
        raw = resolved
    s = suffix_of(raw, f"{rel}/{name}")
    if s:
        tmpl_tables.add(s)

py_tables = {suffix for _env, suffix in TABLES.values()}
if tmpl_tables == py_tables:
    ok(f"{len(tmpl_tables)} table suffixes match resource_names.TABLES")
else:
    for s in sorted(tmpl_tables - py_tables):
        fail(f"table '{s}' exists in a template but not in resource_names.TABLES")
    for s in sorted(py_tables - tmpl_tables):
        fail(f"table '{s}' is in resource_names.TABLES but no template creates it")

print("\n── Lambda function suffixes ──")
tmpl_funcs = set()
for rel, name, props in resources_of("AWS::Lambda::Function"):
    raw = scalar(props.get("FunctionName"))
    if raw is None:
        notes.append(f"{rel}/{name}: no explicit FunctionName (CloudFormation will generate one)")
        continue
    s = suffix_of(raw, f"{rel}/{name}")
    if s:
        tmpl_funcs.add(s)

py_funcs = {suffix for _env, suffix in FUNCTIONS.values()}
missing_in_py = tmpl_funcs - py_funcs
missing_in_tmpl = py_funcs - tmpl_funcs
if not missing_in_py and not missing_in_tmpl:
    ok(f"{len(tmpl_funcs)} function suffixes match resource_names.FUNCTIONS")
else:
    for s in sorted(missing_in_py):
        fail(f"function '{s}' exists in a template but not in resource_names.FUNCTIONS")
    for s in sorted(missing_in_tmpl):
        fail(f"function '{s}' is in resource_names.FUNCTIONS but no template creates it")

print("\n── RESOURCE_PREFIX reaches everything that reads a table ──")
for rel in templates:
    body = (REPO / rel).read_text(encoding="utf-8")
    for block in re.finditer(
        r"^(\s+)(?:Environment:\n\s+Variables:|Environment:\n\s+-|environment:)",
        body, re.M,
    ):
        pass  # structural walk below is more reliable than regex

for rel, name, props in list(resources_of("AWS::Lambda::Function")) + \
        list(resources_of("AWS::Batch::JobDefinition")) + \
        list(resources_of("AWS::ECS::TaskDefinition")):
    text = yaml.dump(props, default_flow_style=False)
    reads_table = "_TABLE" in text
    has_prefix = "RESOURCE_PREFIX" in text

    # Inline functions (Code.ZipFile) are exempt: they cannot import
    # tools/resource_names.py, so they are always handed fully-resolved table names
    # via !Ref. Setting RESOURCE_PREFIX on them would be dead configuration that
    # implies a link to the naming module that does not exist. Reported as a note so
    # the exemption stays visible.
    code = props.get("Code") or {}
    if reads_table and not has_prefix and isinstance(code, dict) and "ZipFile" in code:
        notes.append(
            f"{rel}/{name}: inline function, exempt from RESOURCE_PREFIX "
            "(receives resolved table names by !Ref)"
        )
        continue

    if reads_table and not has_prefix:
        fail(f"{rel}/{name} sets a *_TABLE env var but not RESOURCE_PREFIX")
if not any("_TABLE env var but not RESOURCE_PREFIX" in f for f in failures):
    ok("every compute resource that sets a *_TABLE var also sets RESOURCE_PREFIX")

# ── email-sender seed record shape ───────────────────────────────────────────
# storage.yaml's EmailSenderSeedFunction writes the same DynamoDB item that
# GovernanceRegistry.put_email_sender writes, but as an inline CloudFormation
# function it cannot import the module. If the two drift, the deploy seeds a record
# the application misreads - and the only symptom is invitations failing with 409,
# which looks like an SES problem rather than a schema one. So compare them here.
#
# governance_store.py imports boto3, so parse it rather than importing it.
print("\n── email-sender seed matches governance_store ──")


def _string_const(tree, name: str):
    """Value of a module-level `NAME = '...'` assignment."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return node.value.value
    return None


def _item_dict_fields(scope) -> set:
    """Keys of the `item = {...}` literal in a scope, plus later `item['x'] = ...` keys.

    Scoped to the item assignment specifically. Searching the whole function for each
    field name would pass even when the field is absent from the item but still
    mentioned elsewhere, such as in the audit block.
    """
    fields = set()
    for node in ast.walk(scope):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "item" in names and isinstance(node.value, ast.Dict):
            for key in node.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    fields.add(key.value)
        for target in node.targets:
            if (isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name) and target.value.id == "item"
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)):
                fields.add(target.slice.value)
    return fields


def _function_named(tree, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _module_shape() -> tuple:
    """(config_key, item fields, audit action) from governance_store.put_email_sender."""
    tree = ast.parse((REPO / "backend" / "tools" / "aws" / "governance_store.py").read_text())
    key = _string_const(tree, "_EMAIL_SENDER_KEY")
    func = _function_named(tree, "put_email_sender")
    if func is None:
        return key, set(), None
    action = None
    for node in ast.walk(func):
        # self.audit(actor, "email_sender_set", ...) - action is the second positional.
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "audit" and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)):
            action = node.args[1].value
    return key, _item_dict_fields(func), action


def _seed_shape() -> tuple:
    """The same triple, parsed out of storage.yaml's inline seed function."""
    source = None
    for rel, name, props in resources_of("AWS::Lambda::Function"):
        if name != "EmailSenderSeedFunction":
            continue
        code = props.get("Code") or {}
        if isinstance(code, dict):
            source = code.get("ZipFile")
    if not isinstance(source, str):
        return None, set(), None

    tree = ast.parse(source)
    key = _string_const(tree, "EMAIL_SENDER_KEY")
    handler = _function_named(tree, "handler")
    if handler is None:
        return key, set(), None

    action = None
    for node in ast.walk(handler):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "action"
                        and isinstance(v, ast.Constant)):
                    action = v.value
    return key, _item_dict_fields(handler), action


try:
    mod_key, mod_fields, mod_action = _module_shape()
    seed_key, seed_fields, seed_action = _seed_shape()

    if not mod_key or not mod_fields:
        fail("could not parse put_email_sender from governance_store.py")
    elif seed_key is None and not seed_fields:
        fail("could not parse the inline seed function out of storage.yaml")
    else:
        if seed_key != mod_key:
            fail(f"config_key differs: governance_store {mod_key!r} vs seed {seed_key!r}")
        else:
            ok(f"config_key '{mod_key}' matches")

        # The seeded record must be indistinguishable from one the console writes.
        absent = sorted(mod_fields - seed_fields)
        extra = sorted(seed_fields - mod_fields)
        if absent:
            fail("seeded item is missing field(s) put_email_sender writes: "
                 + ", ".join(absent))
        if extra:
            fail("seeded item writes field(s) put_email_sender does not: "
                 + ", ".join(extra))
        if not absent and not extra:
            ok(f"all {len(mod_fields)} item fields match exactly")

        if mod_action != seed_action:
            fail(f"audit action differs: governance_store {mod_action!r} vs seed {seed_action!r}")
        else:
            ok(f"audit action '{mod_action}' matches")
except (FileNotFoundError, SyntaxError) as exc:
    fail(f"email-sender shape check failed: {exc}")

print("\n── account-unique names derive from the prefix ──")
# These resource types take a name that must be unique across the whole account, so a
# hardcoded one makes a second installation impossible - including the very common case
# of an older installation already sitting in the target account. A literal name is
# therefore a deploy-blocking bug, and CloudFormation reports it late and unhelpfully:
# AgentCore returns AlreadyExists, and CloudFormation's early-validation check reports
# only "Validation failed with N error(s)" with no resource named.
ACCOUNT_UNIQUE = {
    "AWS::BedrockAgentCore::Memory": "Name",
    "AWS::BedrockAgentCore::Runtime": "AgentRuntimeName",
}
for kind, prop in ACCOUNT_UNIQUE.items():
    for rel, name, props in resources_of(kind):
        value = props.get(prop)
        if value is None:
            continue
        # A derived name is a !Sub/!Join/!Ref structure; a plain string is a literal.
        if isinstance(value, str):
            fail(f"{rel}/{name}: {prop} is the literal {value!r}. {kind} names are unique "
                 f"per account, so this collides with any existing installation. Derive it "
                 f"from ResourcePrefix.")
        elif "ResourcePrefix" not in str(value):
            fail(f"{rel}/{name}: {prop} does not derive from ResourcePrefix, so a second "
                 f"installation in the same account would collide.")
        else:
            ok(f"{rel}/{name}: {prop} derives from ResourcePrefix")

if notes:
    print("\n── notes ──")
    for n in notes:
        print(f"  - {n}")

print(f"\n{'PASS' if not failures else f'FAIL - {len(failures)} problem(s)'}")
sys.exit(1 if failures else 0)
