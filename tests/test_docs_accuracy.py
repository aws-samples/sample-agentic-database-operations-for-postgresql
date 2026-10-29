#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Pins the countable claims in README.md and docs/ARCHITECTURE.md to the code.

Prose goes stale silently. Both documents had drifted in ways no other check could see:

  * ARCHITECTURE said /api/security-posture reports "4 configurable" and "9 built-in"
    controls, naming a `StoreAuthMode` parameter. The code reports 3 and 10, and that
    parameter does not exist anywhere - the mode it selected went away with the
    template-declared master secret. A reader auditing the security surface would have
    looked for a control that cannot be configured, and a toggle that cannot be set.
  * README said master.yaml exposes "twelve" optional settings. It exposed twelve when
    written; a thirteenth was added and the sentence was not.

Each was accurate once, which is what makes this class of error worth a test rather than
a proofread. Only claims that can be derived from the source are asserted here - the
counts, and the absence of names that no longer exist. Wording is left alone.

    python3 tests/test_docs_accuracy.py      (from the repository root)
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
ARCH = (ROOT / "docs/ARCHITECTURE.md").read_text(encoding="utf-8")
APP = (ROOT / "backend/app.py").read_text(encoding="utf-8")
EXAMPLE = (ROOT / "deploy.config.example").read_text(encoding="utf-8")

NUMBER_WORDS = {
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "twenty-one": 21, "twenty-two": 22, "twenty-three": 23,
    "twenty-four": 24, "twenty-five": 25,
}

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def posture_controls():
    """(total, configurable, builtin) from the controls list in get_security_posture."""
    tree = ast.parse(APP)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "get_security_posture"):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Assign) or not isinstance(sub.value, ast.List):
                continue
            if not any(isinstance(t, ast.Name) and t.id == "controls" for t in sub.targets):
                continue
            cfg = bi = 0
            for el in sub.value.elts:
                if not isinstance(el, ast.Dict):
                    continue
                for k, v in zip(el.keys, el.values):
                    if (isinstance(k, ast.Constant) and k.value == "category"
                            and isinstance(v, ast.Constant)):
                        if v.value == "configurable":
                            cfg += 1
                        elif v.value in ("builtin", "built-in"):
                            bi += 1
            return cfg + bi, cfg, bi
    return None


print("══ the security-posture counts match the code ══")
counts = posture_controls()
check(counts is not None, "found the controls list in get_security_posture()")
if counts:
    total, cfg, bi = counts
    check(f"**{total} controls**" in ARCH,
          f"ARCHITECTURE states {total} controls (code has {total})")
    check(f"**{cfg} configurable**" in ARCH,
          f"ARCHITECTURE states {cfg} configurable (code has {cfg})")
    check(f"**{bi} built-in**" in ARCH,
          f"ARCHITECTURE states {bi} built-in (code has {bi})")

    # Every parameter named as configurable must exist in a template, or the doc is
    # pointing a reviewer at a control they cannot find.
    named = set(re.findall(r"`(Enable[A-Za-z]+)`", ARCH))
    templates = "".join((ROOT / p).read_text(encoding="utf-8")
                        for p in ("infrastructure/master.yaml", "backend/template.yaml"))
    for param in sorted(named):
        check(param in templates, f"{param} named in ARCHITECTURE exists in a template")

print("")
print("══ names that no longer exist are not documented ══")
# StoreAuthMode selected between IAM and a Data API path against a master secret. Both
# the parameter and the mode are gone; a doc that still names it describes a deployment
# nobody can produce.
for name in ("StoreAuthMode", "STORE_AUTH_MODE"):
    where = [f for f, body in (("README.md", README), ("ARCHITECTURE.md", ARCH))
             if name in body]
    check(not where, f"{name} is not referenced by the docs (found in {where})")

# The runtime writes to the metrics store with psycopg2 over IAM auth, so the docs must
# not describe a *write to Aurora* as going through the RDS Data API. Naming the Data API
# is legitimate in other contexts (the deploy-time bootstrap), so the assertion is narrow:
# no line may pair a write to Aurora with the Data API.
writes_via_dataapi = re.compile(
    r"(written|writes|write)\b[^\n]*Aurora[^\n]*Data[ -]API"
    r"|Aurora[^\n]*(written|writes)[^\n]*Data[ -]API"
    r"|Data[ -]API[^\n]*(written|writes)\b[^\n]*Aurora",
    re.I)
offenders = [line.strip()[:95] for line in ARCH.splitlines() if writes_via_dataapi.search(line)]
check(not offenders,
      f"no line describes an Aurora write as going via the Data API (found {offenders})")

print("")
print("══ the README settings table matches deploy.config.example ══")
# The optional-settings table is the customer-facing list. A setting that exists and is
# not in it is a setting nobody knows they have.
table = re.findall(r"^\| `([A-Z][A-Z0-9_]*)`(?: / `([A-Z][A-Z0-9_]*)`)? \|", README, re.M)
documented = {a for a, _ in table} | {b for _, b in table if b}
behaviour = EXAMPLE.split("Template behaviour")[-1].split("Network addressing")[0]
declared = set(re.findall(r"^([A-Z][A-Z0-9_]*) *=", behaviour, re.M))
# The two SES sender fields are documented with the identity settings, not here.
declared -= {"EMAIL_SENDER_ADDRESS", "EMAIL_SENDER_NAME"}

missing = sorted(declared - documented)
check(not missing, f"every optional setting appears in the README table (missing {missing})")

stale = sorted(documented & {"STORE_AUTH_MODE"})
check(not stale, f"the table names no removed settings (found {stale})")

# And the sentence introducing the table has to agree with the table.
m = re.search(r"`master\.yaml` exposes ([\w-]+) more settings", README)
check(m is not None, "found the sentence stating how many optional settings there are")
if m:
    word = m.group(1).lower()
    stated = NUMBER_WORDS.get(word)
    check(stated is not None, f"the count is a word this test knows ({word!r})")
    if stated is not None:
        check(stated == len(declared),
              f"README says {word} ({stated}); deploy.config.example declares {len(declared)}")

print("")
print("== README says which databases are supported, and ARCHITECTURE agrees ==")
supported = README.split("\n## Supported databases\n", 1)
check(len(supported) == 2, "README has a '## Supported databases' section")
sup_body = supported[1].split("\n## ", 1)[0] if len(supported) == 2 else ""
for needle in ("Aurora Global Database", "RDS Proxy", "custom endpoints", "0 ACU", "read replica",
               "DB_PORT_RANGE", "Multi-AZ DB cluster", "IAM-authentication-only", "rds!",
               "overlaps the deployment VPC", "blue/green"):
    check(needle in sup_body, f"the section names {needle!r}")
check(README.index("\n## Supported databases\n") < README.index("\n## Quick Start\n"),
      "it sits before Quick Start")
ARCH = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
limit = ARCH.split("**Known limit:**", 1)[1][:600] if "**Known limit:**" in ARCH else ""
check("Aurora failover" in limit and "blue/green" in limit,
      "the ARCHITECTURE known limit covers Aurora failover and blue/green, not only Multi-AZ")
check("../README.md#supported-databases" in ARCH, "ARCHITECTURE points at the README section")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
