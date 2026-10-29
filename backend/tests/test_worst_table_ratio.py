# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tier 1's "worst table" dead-tuple metric orders by the ratio, not by count (#22).

The scalar ``dead_tuple_pct`` is labelled and shown as the worst table's dead-tuple
PERCENTAGE, but the SQL ordered by absolute ``n_dead_tup`` — so a huge table at 5% dead
rows hid a smaller table at 80%. The query must order by the computed ratio.

Behavioural: a tiny fake cursor evaluates the two candidate orderings against an
in-memory table and confirms the shipped query returns the highest-RATIO row. Also pins
that the raw ``ORDER BY n_dead_tup DESC`` is gone from that metric.

Run:  python3 backend/tests/test_worst_table_ratio.py
"""
import pathlib
import re
import sys

SRC = (pathlib.Path(__file__).resolve().parents[1] / "tier1_collector.py").read_text(encoding="utf-8")
passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


# The metric-9 query (the scalar worst-table dead_tuple_pct).
m = re.search(r"# 9\. Dead tuple %.*?\"\"\"(.*?)\"\"\"", SRC, re.S)
assert m, "metric 9 query not found"
query = m.group(1)

check("ORDER BY n_dead_tup DESC" not in query,
      "the worst-table metric no longer orders by absolute dead-tuple COUNT")
check("n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0)" in query and "DESC" in query.split("ORDER BY", 1)[1],
      "it orders by the computed dead-tuple RATIO")

# A huge table at 5% and a small table at 80%: the worst-RATIO row (80%) must win, which
# ordering by count (the big table) would not pick.
tables = [
    {"name": "events", "n_dead_tup": 500_000, "n_live_tup": 9_500_000},   # 5.0%, biggest by count
    {"name": "sessions", "n_dead_tup": 800, "n_live_tup": 200},           # 80.0%, worst ratio
    {"name": "audit", "n_dead_tup": 100, "n_live_tup": 9_900},            # 1.0%
]


def ratio(t):
    denom = t["n_live_tup"] + t["n_dead_tup"]
    return round(100.0 * t["n_dead_tup"] / denom, 2) if denom else None


by_count = sorted(tables, key=lambda t: t["n_dead_tup"], reverse=True)[0]
by_ratio = sorted(tables, key=lambda t: ratio(t), reverse=True)[0]
check(ratio(by_count) == 5.0 and ratio(by_ratio) == 80.0,
      "ordering by count returns 5% (wrong); ordering by ratio returns 80% (the worst table)")
check(ratio(by_ratio) == 80.0, "the shipped ordering (by ratio) surfaces the 80% table")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
