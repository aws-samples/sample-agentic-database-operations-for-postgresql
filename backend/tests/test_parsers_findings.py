# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""_extract_findings parses the investigation SOP's own output layout.

The SOP (backend/runbooks/investigation-protocol.sop.md) tells the agent to use
"### FINDINGS" and to organise each section by database with ### headers. The parser
used to split on every #/##/### header and keep only the slice labelled FINDINGS, which
severed it at the first per-database ### sub-header and left it holding just the word
"FINDINGS" — so a completed investigation reported zero findings and no severity. These
tests build input from the SOP template and assert the findings come back.

Run:  python3 backend/tests/test_parsers_findings.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.parsers import _extract_findings  # noqa: E402

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


# The SOP layout: ### FINDINGS, then per-database ### sub-headers at the SAME level, then
# the next section (### ROOT CAUSE). This is the exact shape that produced zero findings.
SOP_OUTPUT = """\
### FINDINGS

### orders-db
- CRITICAL: [pg_stat_activity] 7 sessions from 10.0.1.50 holding locks
- WARNING: [PostgreSQL Config] work_mem is low at 4MB

### inventory-db
- INFO: [CloudWatch] CPU steady at 30%

### ROOT CAUSE
orders-db: a lock storm from chaos_runner. inventory-db: nothing wrong.

### RECOMMENDED ACTIONS
- CRITICAL: this line is in RECOMMENDED ACTIONS and must NOT be read as a finding

### SUMMARY
Two databases investigated.
"""

print("== the SOP layout yields findings, attributed by database, bounded by sections ==")
f = _extract_findings(SOP_OUTPUT)
sev = sorted((x["severity"], x["title"][:20]) for x in f)
check(len(f) == 3, f"three findings extracted from the SOP layout (got {len(f)})")
by_db = {}
for x in f:
    by_db.setdefault(x.get("database"), []).append(x["severity"])
check(by_db.get("orders-db") == ["critical", "warning"], "orders-db carries its two findings in order")
check(by_db.get("inventory-db") == ["info"], "inventory-db carries its one finding")
check(all("ROOT CAUSE" not in x["title"].upper() for x in f), "nothing from ROOT CAUSE leaked in")
check(not any("must NOT be read" in x["title"] for x in f),
      "a CRITICAL line under RECOMMENDED ACTIONS is not counted as a finding")

print("== bold severity labels are accepted ==")
BOLD = """\
### FINDINGS
### db-1
- **CRITICAL**: a bolded severity label
- **WARNING:** a bolded label with the colon inside
### ROOT CAUSE
done
"""
fb = _extract_findings(BOLD)
check(len(fb) == 2 and {x["severity"] for x in fb} == {"critical", "warning"},
      "both **CRITICAL**: and **WARNING:** forms are parsed")
check(any(x["title"] == "a bolded severity label" for x in fb), "the title drops the bold markers")

print("== a single-database investigation with findings directly under the header ==")
SINGLE = """\
## FINDINGS
- CRITICAL: one finding, no per-database header
## ROOT CAUSE
x
"""
fs = _extract_findings(SINGLE)
check(len(fs) == 1 and fs[0]["severity"] == "critical", "a level-2 FINDINGS with direct findings works too")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
