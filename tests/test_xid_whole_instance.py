#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""XID wraparound age is measured across the whole instance, not just the connected DB (review #8).

Wraparound pressure accumulates on a rarely-vacuumed database (often `postgres` or a
secondary), which the once-a-day deep scan already measures across every datallowconn
database. Tier 1 measured only current_database(), so the live dashboard / Metrics tab /
assess_growth / the agent's XID line could read near 0% while the deep scan reported 80%
on the same instance. Tier 1 and the live health summary now take MAX(age(datfrozenxid))
over datallowconn, matching the deep scan.

This models the "worst database wins" rule over a fake pg_database row set and asserts the
shipped queries measure the whole instance.

    python3 tests/test_xid_whole_instance.py   (from the repository root)
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


# ── the rule: the worst database on the instance wins ──
# A fake pg_database: the connected database is young, a rarely-vacuumed one is near
# wraparound. The metric must reflect the worst (postgres), not the connected one.
XID_MAX = 2147483647
DBS = [
    {"datname": "appdb",     "age": 50_000_000,   "datallowconn": True},    # connected, young
    {"datname": "postgres",  "age": 1_700_000_000, "datallowconn": True},   # rarely vacuumed, ~79%
    {"datname": "template0", "age": 2_000_000_000, "datallowconn": False},  # not connectable — excluded
]


def whole_instance_pct(rows):
    ages = [r["age"] for r in rows if r["datallowconn"]]
    return round(100.0 * max(ages) / XID_MAX, 2)


def connected_only_pct(rows, connected="appdb"):
    age = next(r["age"] for r in rows if r["datname"] == connected)
    return round(100.0 * age / XID_MAX, 2)


print("══ the worst datallowconn database decides the metric ══")
whole = whole_instance_pct(DBS)
connected = connected_only_pct(DBS)
check(abs(whole - 79.16) < 0.5, f"the whole-instance pct reflects the worst DB (~79%, got {whole})")
check(connected < 3, f"the connected DB alone would read near 0% ({connected})")
check(whole > connected, "the whole-instance view is higher than the connected-only view")
# template0 (datallowconn=false) is excluded even though it is the oldest.
check(max(r["age"] for r in DBS) > max(r["age"] for r in DBS if r["datallowconn"]),
      "the oldest DB (template0) is excluded because it is not connectable")

# ── the shipped queries measure the whole instance ──
print("")
print("══ Tier 1, the live health summary and the deep scan measure the whole instance ══")
tier1 = (ROOT / "backend/tier1_collector.py").read_text(encoding="utf-8")
# The XID query in Tier 1 (query #4) uses MAX(age(datfrozenxid)) over datallowconn.
m = re.search(r"MAX\(age\(datfrozenxid\)\)[^;]*?datallowconn", tier1, re.S)
check(bool(m), "Tier 1 uses MAX(age(datfrozenxid)) over datallowconn")
check("WHERE datname = current_database()\n        \"\"\")\n        row = cur.fetchone()\n        metrics[\"xid_age_pct\"]"
      not in tier1, "Tier 1 no longer scopes the XID metric to current_database()")

dbt = (ROOT / "backend/tools/db_tools.py").read_text(encoding="utf-8")
# The get_health_summary xid CTE now uses MAX(age(datfrozenxid)) over datallowconn.
xid_cte = re.search(r"xid AS \(.*?FROM pg_database WHERE (\w+)", dbt, re.S)
check(bool(xid_cte) and xid_cte.group(1) == "datallowconn",
      "the live health summary's XID CTE scans datallowconn, not current_database()")
check("MAX(age(datfrozenxid))" in dbt, "the live health summary takes MAX(age(datfrozenxid))")

run_scan = (ROOT / "batch/run_scan.py").read_text(encoding="utf-8")
check("FROM pg_database WHERE datallowconn ORDER BY age(datfrozenxid) DESC" in run_scan,
      "the deep scan already orders every datallowconn DB by age DESC (worst first)")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
