# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Report comparison fills Index Cache Hit and Deadlocks (#28).

The comparison UI shows Index Cache Hit and Deadlocks, but the store never recorded them,
so they were always blank. The Batch scan collects both into the stored scan summary,
and the comparison reads them from there (the S3 analysis doc carries them too). A scan
without them is blank, not an error.

Run:  python3 backend/tests/test_compare_metrics.py
"""
import pathlib
import re
import sys

APP_SRC = (pathlib.Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
RUN_SCAN = (pathlib.Path(__file__).resolve().parents[2] / "batch/run_scan.py").read_text(encoding="utf-8")
passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def _extract(name):
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^(?:@|def |class |[A-Z_]+ = )|\Z)", APP_SRC, re.S | re.M)
    assert m, f"{name} not found in app.py"
    return m.group(0)


import os  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import fake_metrics_store  # noqa: E402
from tools.aws import metrics_store as ms  # noqa: E402

ns = {"_store_call": lambda fn: fn()}
exec(_extract("_compare_report_from_row"), ns)  # noqa: S102
fake_metrics_store.install()
print("the comparison reads the two metrics from the stored summary")
sid = ms.put_tier2_scan("db-1", {"scan_time": "2026-10-06T06:00:00Z", "cache_hit_ratio": 99.0, "dead_tuple_pct": 2.0,
                                 "index_cache_hit_ratio": 97.5, "deadlocks": 3, "scan_kind": "full",
                                 "member_instance_id": "db-1"},
                        {"performance": {"top_queries": []}, "configuration": {"settings": []}})
row = ms.tier2_by_id(sid)
rep = ns["_compare_report_from_row"](row)
m = rep["metrics"]
check(m["index_cache_hit_ratio"] == 97.5, "Index Cache Hit comes from the summary")
check(m["deadlocks"] == 3, "Deadlocks comes from the summary")
check(m["table_cache_hit_ratio"] == 99.0, "the other headline metrics are unchanged")
check(set(rep["detailed_sections"]) == {"performance", "configuration"} and rep["id"] == sid,
      "the scan's stored sections ride along as detailed_sections")
print("a scan without them degrades to blank, not an error")
sid = ms.put_tier2_scan("db-1", {"scan_time": "2026-10-05T06:00:00Z", "scan_kind": "full",
                                 "member_instance_id": "db-1"}, {})
m = ns["_compare_report_from_row"](ms.tier2_by_id(sid))["metrics"]
check(m["index_cache_hit_ratio"] is None and m["deadlocks"] is None, "blank when the scan did not collect them")
print("the Batch scan collects both and carries them on every surface")
check("pg_statio_user_indexes" in RUN_SCAN and 'data["index_cache_ratio"]' in RUN_SCAN,
      "run_scan computes the index cache hit ratio from pg_statio")
check("SELECT deadlocks FROM pg_stat_database" in RUN_SCAN and 'data["deadlocks"]' in RUN_SCAN,
      "run_scan reads deadlocks from pg_stat_database")
check('"index_cache_hit_ratio": structured_data.get("index_cache_ratio")' in RUN_SCAN
      and '"deadlocks": structured_data.get("deadlocks")' in RUN_SCAN,
      "the S3 legacy analysis doc carries both too")

print("the comparison section still wires both fields to the UI")
sec = re.search(r'"performance": \{.*?\}', APP_SRC, re.S).group(0)
check("report1_index_cache" in sec and "report1_deadlocks" in sec, "the performance section exposes both")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
