#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Keep the deploy settings, the Makefile and master.yaml telling the same story.

The optional settings - WAF, CloudTrail, the guardrail knobs, Aurora capacity, the model
- now exist in three places, and all three have to agree:

  1. master.yaml declares the CloudFormation parameter and its default.
  2. configure.py offers a config key with a default, and writes it to deploy.config.
  3. The Makefile passes that config value through as a --parameter-override.

Any one of those going missing fails quietly and in a different way. A parameter the
Makefile does not pass reverts to the template default, so a setting a user deliberately
changed is ignored - which is the bug that prompted this: fourteen parameters, including
WAF and every guardrail setting, were never passed and so were silently at their
defaults. A default that disagrees with the template means deploying through `make`
behaves differently from deploying the same template through the console. And a config
key naming a parameter that no longer exists makes CloudFormation reject the whole
stack, after the artifacts have been staged.

None of that is visible by reading any single file, so it is checked here.
"""

import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "deployment"))

from configure import TEMPLATE_SETTINGS  # noqa: E402

problems = 0


def fail(msg: str) -> None:
    global problems
    print(f"  ❌ {msg}")
    problems += 1


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def _passthrough(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


yaml.SafeLoader.add_multi_constructor("!", _passthrough)

master = yaml.safe_load((REPO / "infrastructure/master.yaml").read_text(encoding="utf-8"))
params = master.get("Parameters", {})

makefile = (REPO / "Makefile").read_text(encoding="utf-8")
# The deploy recipe runs two `aws cloudformation deploy` commands - the us-east-1
# CloudFront WAF edge stack first, then master.yaml - so read the overrides of the one
# that deploys master.yaml, not the first in the file.
_master = re.search(r"--template-file\s+infrastructure/master\.yaml.*?--parameter-overrides"
                    r"(.*?)--no-fail-on-empty-changeset", makefile, re.S)
if not _master:
    print("  ❌ no master.yaml --parameter-overrides block found in the Makefile")
    sys.exit(1)
overrides = _master.group(1)

example = (REPO / "deploy.config.example").read_text(encoding="utf-8")
example_values = dict(re.findall(r"^([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$", example, re.M))

print("── optional settings reach CloudFormation ──")
for key, setting in TEMPLATE_SETTINGS.items():
    cfn = setting["cfn"]
    default = setting["default"]
    validator = setting["validator"]
    choices = setting.get("choices")
    spec = params.get(cfn)
    if spec is None:
        fail(f"{key} maps to parameter {cfn}, which master.yaml does not declare. "
             f"CloudFormation rejects unknown parameters, so the deploy would fail.")
        continue

    # The Makefile has to pass it, or the user's choice is silently discarded.
    if not re.search(rf"\b{re.escape(cfn)}=", overrides):
        fail(f"{cfn} is not passed in deploy-stack's --parameter-overrides, so "
             f"{key} in deploy.config would be ignored and the template default used.")

    template_default = spec.get("Default")
    if template_default is None:
        fail(f"{cfn} has no Default in master.yaml, but {key} defaults to {default!r}. "
             f"Give the parameter a default or stop defaulting it here.")
    elif str(template_default) != str(default):
        fail(f"{key} defaults to {default!r} but master.yaml's {cfn} defaults to "
             f"{str(template_default)!r}. Deploying with make would differ from "
             f"deploying the same template directly.")

    # A value validated here must be one the template will also accept.
    allowed = spec.get("AllowedValues")
    if allowed and validator is not None:
        bad = [a for a in allowed if validator(str(a))]
        if bad:
            fail(f"{key}'s validator rejects {bad}, which {cfn} lists in AllowedValues.")
        if validator(str(template_default)):
            fail(f"{key}'s validator rejects {cfn}'s own default {template_default!r}.")

    # The options shown at the prompt must be exactly what the template accepts,
    # otherwise the prompt either hides a valid choice or offers one that is rejected.
    if allowed and choices and sorted(map(str, allowed)) != sorted(choices):
        fail(f"{key} offers {choices} at the prompt but {cfn} allows {allowed}.")

    if key not in example_values:
        fail(f"{key} is missing from deploy.config.example, so nobody reading the repo "
             f"would know the setting exists.")
    elif example_values[key] != str(default):
        fail(f"deploy.config.example sets {key}={example_values[key]!r} but the default "
             f"is {default!r}.")

if problems == 0:
    ok(f"{len(TEMPLATE_SETTINGS)} settings agree across master.yaml, "
       f"the Makefile and deploy.config.example")

print("")
print("── every master.yaml parameter is either passed or deliberately defaulted ──")
# Parameters the Makefile does not pass and that are not offered as settings. Each one
# is a deliberate choice to leave at the template default; listing them means adding a
# parameter without wiring it shows up here rather than being discovered in a deploy.
DELIBERATELY_DEFAULTED = {
    # Resolved after the fact by deploy-spas.sh from the frontend stack's CloudFront URL.
    "WebAppUrl",
    # REST API Gateway stage throttle. The defaults (100 req/s, 200 burst) sit well
    # above normal use and only shed a flood or runaway loop, so they are not a
    # make-config prompt; an operator running a large fleet raises them at the
    # template/CloudFormation level. master.yaml still passes them to the API stack.
    "ApiThrottleRateLimit",
    "ApiThrottleBurstLimit",
}
unwired = []
for name in params:
    if re.search(rf"\b{re.escape(name)}=", overrides):
        continue
    if name in {s["cfn"] for s in TEMPLATE_SETTINGS.values()}:
        continue
    if name in DELIBERATELY_DEFAULTED:
        continue
    unwired.append(name)

if unwired:
    for name in unwired:
        fail(f"{name} is declared in master.yaml but never passed and never offered as a "
             f"setting, so it is silently stuck at its default. Either wire it up or add "
             f"it to DELIBERATELY_DEFAULTED with a reason.")
else:
    ok(f"all {len(params)} parameters are passed, offered, or listed as deliberate")

print("")
print("── each SPA's runtime config resolves end to end ──")
# A fourth place the same value has to line up. deploy-spas.sh writes keys into the
# published config.js, src/config.js has to expose each one through `pick`, and the
# components read them off the exported object. A key present in `buildTime` or
# `defaults` but absent from the export is the quiet failure: `pick` is never called
# for it, so its default cannot fire, `config.<key>` is undefined, and the UI renders
# the string "undefined" into whatever the operator is told to copy. That is not a
# syntax error and the bundle builds, so only a check like this finds it.
SPA_DIRS = ("frontend", "admin-console")

published: dict[str, set[str]] = {app: set() for app in SPA_DIRS}
spa_sh = (REPO / "deployment/deploy-spas.sh").read_text(encoding="utf-8")
current = None
for line in spa_sh.splitlines():
    m = re.match(r"\s*publish\s+(\S+)\s", line)
    if m:
        current = m.group(1) if m.group(1) in published else None
        continue
    if current:
        for key in re.findall(r'"(\w+)=', line):
            published[current].add(key)
        if not line.rstrip().endswith("\\"):
            current = None

for app in SPA_DIRS:
    cfg = (REPO / app / "src/config.js").read_text(encoding="utf-8")
    build_time = set(re.findall(r"^\s+(\w+): import\.meta\.env", cfg, re.M))
    defaults_ = set(re.findall(r"^\s+(\w+): '", cfg, re.M))
    exported = set(re.findall(r"^\s+(\w+): pick\('", cfg, re.M))

    for key in sorted((build_time | defaults_) - exported):
        fail(f"{app}/src/config.js declares {key!r} but never exports it, so "
             f"config.{key} is undefined and its default can never apply.")
    for key in sorted(published[app] - exported):
        fail(f"deploy-spas.sh publishes {key!r} to {app}, but "
             f"{app}/src/config.js does not export it, so the deployed value is unread.")

    # The direction that actually breaks a screen: a component reading a key the
    # config object does not carry. Prose and comments mention the file by name, so
    # a bare `config.<ext>` is the filename rather than a key access.
    NOT_KEYS = {"js", "jsx", "mjs", "cjs", "json", "ts", "tsx"}
    read: set[str] = set()
    for src in sorted((REPO / app / "src").rglob("*.js*")):
        for key in re.findall(r"\bconfig\.(\w+)", src.read_text(encoding="utf-8")):
            if key not in NOT_KEYS:
                read.add(key)
    for key in sorted(read - exported):
        fail(f"{app} reads config.{key} but {app}/src/config.js does not export it.")

    if not ((build_time | defaults_ | published[app] | read) - exported):
        ok(f"{app}: {len(exported)} config keys are declared, published, exported "
           f"and read consistently")

print("")
if problems:
    print(f"FAIL — {problems} problem(s)")
    sys.exit(1)
print("PASS")
