#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The deep-scan headline DB size is the MONITORED database, in MB, not quantised (review #9).

findings_engine took the LARGEST database's size (sizes[0]) and multiplied a GB value
already rounded to 2 dp by 1024 — so the Reports/Trends size described `postgres` rather
than the monitored database, a sub-5 MB database read 0, and growth moved in 10.24 MB
steps. run_scan now measures pg_database_size(current_database()) directly in MB
(monitored_database_size_mb), and extract_top_level_metrics uses it for the headline,
keeping the per-database list for display only.

    python3 tests/test_deep_scan_db_size.py   (from the repository root)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "batch"))

from utils.findings_engine import extract_top_level_metrics  # noqa: E402

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


# The monitored database is 3.33 MB; the instance's largest database (postgres) is 50 GB.
# The old code would report ~51200 MB (postgres, quantised); the fix reports 3.33 MB.
DATA = {
    "monitored_database_size_mb": 3.33,
    "database_sizes": [
        {"name": "postgres", "size_gb": 50.0},   # the largest — must NOT be the headline
        {"name": "appdb", "size_gb": 0.0},        # the monitored one, rounds to 0 GB
    ],
    "cache_ratios": [{"hit_ratio": 99.0}],
    "activity": {"total_connections": 7},
    "indexes": {"unused": []},
    "table_bloat": [],
    "xid_age": [],
}

print("══ the headline size is the monitored database, measured in MB ══")
m = extract_top_level_metrics(DATA)
check(m["database_size_mb"] == 3.33, f"reports the monitored DB's size, 3.33 MB (got {m['database_size_mb']})")
check(m["database_size_mb"] != 50.0 * 1024, "not the largest database's size")
check(m["database_size_mb"] < 10, "a small monitored DB is not rounded up to a 10 MB step / 0")

print("")
print("══ a sub-5 MB monitored database is not reported as 0 ══")
small = dict(DATA, monitored_database_size_mb=4.7)
check(extract_top_level_metrics(small)["database_size_mb"] == 4.7, "4.7 MB survives (old code read 0 MB)")

print("")
print("══ legacy scans with no direct measurement still render (fallback) ══")
legacy = {k: v for k, v in DATA.items() if k != "monitored_database_size_mb"}
m2 = extract_top_level_metrics(legacy)
check(m2["database_size_mb"] == round(50.0 * 1024, 2),
      "an older stored scan falls back to the per-database list so it still renders")

# ── run_scan measures the monitored DB directly in MB ──
print("")
print("══ run_scan collects the monitored database size directly in MB ══")
RS = (ROOT / "batch/run_scan.py").read_text(encoding="utf-8")
check("pg_database_size(current_database()) / (1024*1024.0) AS size_mb" in RS,
      "run_scan measures pg_database_size(current_database()) in MB")
check('data["monitored_database_size_mb"]' in RS, "it stores monitored_database_size_mb")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
