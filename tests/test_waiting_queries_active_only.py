#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A waiting query is an ACTIVE backend on a wait event, not an idle client (review #7).

An ordinary idle PostgreSQL client waits on ClientRead, giving it a non-null
wait_event_type of 'Client'. Counting any non-Activity wait without requiring
state='active' inflates "waiting queries" with idle sessions. Every surface that reports
the figure — Tier 1 history (backend/tier1_collector.py), the deep-scan report
(batch/run_scan.py, two sites) and the live health summary (backend/tools/db_tools.py
get_health_summary) — must require state='active'.

This asserts each shipped query pairs state='active' with the wait-event filter, and
models the predicate over a fake pg_stat_activity row set to show only the active+waiting
row counts.

    python3 tests/test_waiting_queries_active_only.py   (from the repository root)
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


# ── the predicate itself, modelled over a fake row set ──
def is_waiting_query(row):
    """The shipped predicate: active AND a non-Activity wait event."""
    return (row["state"] == "active"
            and row["wait_event_type"] is not None
            and row["wait_event_type"] != "Activity")


ROWS = [
    {"state": "active", "wait_event_type": "Lock"},                 # a real waiting query
    {"state": "idle", "wait_event_type": "Client"},                 # idle client on ClientRead
    {"state": "idle in transaction", "wait_event_type": "Client"},  # idle in txn
    {"state": "active", "wait_event_type": "Activity"},             # autovacuum launcher etc.
    {"state": "active", "wait_event_type": None},                   # running, not waiting
]
print("══ the predicate counts only active + waiting ══")
waiting = [r for r in ROWS if is_waiting_query(r)]
check(len(waiting) == 1 and waiting[0]["wait_event_type"] == "Lock",
      f"only the active backend on a real wait event counts (got {len(waiting)})")
check(not any(r["state"].startswith("idle") for r in waiting),
      "no idle or idle-in-transaction session counts")

# ── every shipped surface requires state='active' next to the wait filter ──
print("")
print("══ every surface requires state='active' on the waiting count ══")


def _active_before_wait(sql_blob: str) -> bool:
    """True if every `wait_event_type ... != 'Activity'` waiting count in the blob is
    guarded by a nearby state = 'active'. We scan each FILTER clause that mentions the
    wait filter and require 'active' in the same clause."""
    # Each waiting-count FILTER clause.
    clauses = re.findall(r"FILTER \(WHERE[^)]*wait_event_type[^)]*!= 'Activity'\)", sql_blob, re.S)
    if not clauses:
        return False
    return all("state = 'active'" in c for c in clauses)


surfaces = {
    "backend/tier1_collector.py": ROOT / "backend/tier1_collector.py",
    "batch/run_scan.py": ROOT / "batch/run_scan.py",
    "backend/tools/db_tools.py": ROOT / "backend/tools/db_tools.py",
}
for label, path in surfaces.items():
    src = path.read_text(encoding="utf-8")
    check(_active_before_wait(src), f"{label}: every waiting-query count requires state='active'")

# Explicitly: no surface counts a bare wait_event without the active guard.
print("")
print("══ no surface counts a wait event without the active guard ══")
for label, path in surfaces.items():
    src = path.read_text(encoding="utf-8")
    bad = re.findall(r"FILTER \(WHERE wait_event_type IS NOT NULL\s+AND wait_event_type != 'Activity'\)", src)
    check(not bad, f"{label}: no unguarded waiting-query FILTER remains")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
