#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""One XID-wraparound denominator on every surface (#18).

The same database must not read 83.8% on the dashboard and 90% in the agent's answer or
the HTML report. Every surface divides age(datfrozenxid) by the SAME constant,
tools.xid.XID_WRAPAROUND_MAX (2^31-1). This pins:

  * the shared constant's value;
  * the stored collectors (tier1_collector.py, batch/run_scan.py) divide by it;
  * the agent's prose queries (backend/tools/db_tools.py) divide by it;
  * the Batch findings engine recovers the raw age with it;
  * the shell report (batch/postgres_health_insights.sh, which cannot import Python)
    uses the same literal;
  * no surface still uses the old 2,000,000,000 denominator for the wraparound percent;
  * the shared module is staged into the batch image and the tier1 zip.

Run:  python3 tests/test_xid_denominator.py
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from tools.xid import XID_WRAPAROUND_MAX  # noqa: E402

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


VAL = str(XID_WRAPAROUND_MAX)
check(XID_WRAPAROUND_MAX == 2_147_483_647, "the shared constant is 2^31-1")

print("the Python collectors and prose queries use the shared constant")
for rel in ("backend/tier1_collector.py", "backend/tools/db_tools.py", "batch/run_scan.py"):
    src = read(rel)
    check("XID_WRAPAROUND_MAX" in src and "from tools.xid import XID_WRAPAROUND_MAX" in src,
          f"{rel} imports and uses the shared constant")
    # The old prose denominator must be gone from the wraparound-percent queries.
    check("/ 2000000000" not in src and "/ 2_000_000_000" not in src,
          f"{rel} no longer uses the 2,000,000,000 denominator")

fe = read("batch/utils/findings_engine.py")
check("XID_WRAPAROUND_MAX" in fe and "/ 100.0 * 2147483647" not in fe,
      "findings_engine recovers the age with the shared constant")

print("the shell report uses the same literal, not 2,000,000,000")
sh = read("batch/postgres_health_insights.sh")
check("/ 2000000000" not in sh, "no 2,000,000,000 wraparound denominator in the shell report")
pct_lines = [ln for ln in sh.splitlines() if "age(" in ln and "xid_age_percent" in ln and "100.0 *" in ln]
check(pct_lines and all(VAL in ln for ln in pct_lines),
      f"every xid_age_percent line divides by {VAL} ({len(pct_lines)} lines)")

print("the shared module is staged into the batch image and the tier1 zip")
check("backend/tools/xid.py" in read("deployment/build-images.sh"),
      "build-images.sh stages tools/xid.py into the batch build context")
check("tools/xid.py" in read("backend/scripts/build-tier1.sh"),
      "build-tier1.sh copies tools/xid.py into the collector zip")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
