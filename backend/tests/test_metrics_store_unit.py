# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The metrics store module on its own (tools.aws.metrics_store).

Key order is time order (canonical scan times), scan ids round-trip and refuse junk, a
section too big for one item is trimmed rather than failing the scan, the report index
stays sparse, PutMetricData and GetMetricData calls stay inside their API limits, chart
periods never ask CloudWatch for finer points than it keeps, and the collector's baseline
sample is strictly earlier than the run being written.

Hermetic: an in-memory DynamoDB and CloudWatch (tests/fake_metrics_store), no AWS.

Run:  python3 backend/tests/test_metrics_store_unit.py
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import fake_metrics_store  # noqa: E402
from tools.aws import metrics_store as ms  # noqa: E402

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


print("== configuration ==")
for k in ("TIER1_METRICS_TABLE", "TIER2_SCANS_TABLE", "CW_METRICS_NAMESPACE"):
    os.environ.pop(k, None)
check(ms.is_configured() is False, "not configured until both tables are named in the environment")
os.environ["RESOURCE_PREFIX"] = "acme"
check(ms.cw_namespace() == "acme/Metrics", "the namespace is per installation (<prefix>/Metrics)")
check(ms.tier1_table_name() == "acme-tier1-metrics" and ms.tier2_table_name() == "acme-tier2-scans",
      "the table names derive from the prefix")
fake = fake_metrics_store.install()
check(ms.is_configured() and ms.cw_namespace() == fake_metrics_store.NAMESPACE, "the environment overrides")

print("== canonical scan times ==")
a = ms.canon_ts("2026-10-07T09:05:00Z")
b = ms.canon_ts("2026-10-07T10:05:00.5+01:00")
c = ms.canon_ts(datetime(2026, 10, 7, 9, 5, 0, 600000))
check(a == "2026-10-07T09:05:00.000+00:00" and b == "2026-10-07T09:05:00.500+00:00", "UTC, millisecond width")
check(c == "2026-10-07T09:05:00.600+00:00", "a naive datetime is taken as UTC")
check(sorted([c, a, b]) == [a, b, c], "string order is time order")
check(raises(ms.canon_ts, "yesterday"), "junk is refused")

print("== scan ids ==")
sid = ms.scan_id("db-1", "2026-10-07T09:05:00Z", "db-1a")
check(sid == "db-1|2026-10-07T09:05:00.000+00:00|db-1a", "instance|time|member")
check(ms.parse_scan_id(sid) == ("db-1", "2026-10-07T09:05:00.000+00:00", "db-1a"), "round trip")
for bad in ("7", "", "a|b", "a||c", "a|b|c|d", "db-1|soon|m"):
    check(raises(ms.parse_scan_id, bad), f"refused: {bad!r}")

print("== a deep scan's items ==")
sid = ms.put_tier2_scan("db-1", {"scan_time": "2026-10-07T09:05:00Z", "member_instance_id": "db-1a",
                                 "health_score": "ok", "report_s3_key": "", "summary": "s",
                                 "not_a_field": "x", "cache_hit_ratio": 99.5, "dead_tuple_pct": float("nan")},
                        {"overview": {"version": "16"}, "bogus": {"x": 1}, "performance": None})
keys = sorted(k[1].split("#")[0] for k in fake.t2.items)
check(keys == ["overview", "summary"], f"one summary item plus one per known, non-empty section ({keys})")
summary = fake.t2.items[("db-1", "summary#2026-10-07T09:05:00.000+00:00#db-1a")]
check("report_s3_key" not in summary, "an empty report key is absent, so the index stays sparse")
check("not_a_field" not in summary and "dead_tuple_pct" not in summary, "unknown fields and NaN are not stored")
check(summary["sections"] == ["overview"] and summary["scan_kind"] == "full" and summary["member_role"] == "writer",
      "the summary lists its sections; kind and role default")
row = ms.tier2_by_id(sid)
check(row["id"] == sid and row["cache_hit_ratio"] == 99.5 and "sk" not in row, "read back by id, plain numbers")
check(ms.tier2_sections("db-1", row["scan_time"], "db-1a") == {"overview": {"version": "16"}}, "sections by key")
check(ms.tier2_data_doc(row, ["overview", "nope"]) == {"overview": {"version": "16"}}, "unknown names are ignored")

print("== a section too big for one item is trimmed, not dropped ==")
big = {"top_queries": [{"query": "x" * 1000, "n": i} for i in range(800)], "note": "keep"}
small = ms._shrink(big)
check(len(json.dumps(small)) <= ms._ITEM_BUDGET and small["truncated"] is True and small["note"] == "keep"
      and len(small["top_queries"]) >= 5, "its longest list is halved until it fits")
check(ms._shrink({"a": [1, 2]}) == {"a": [1, 2]}, "a small section is untouched")
huge = ms._shrink({"blob": "y" * (ms._ITEM_BUDGET + 10)})
check(huge == {"blob": None, "truncated": True}, "an unsplittable value is dropped with the keys kept")
ms.put_tier2_scan("db-2", {"scan_time": "2026-10-07T09:05:00Z"}, {"performance": big})
item = fake.t2.items[("db-2", "performance#2026-10-07T09:05:00.000+00:00#db-2")]
check(item["data"]["truncated"] is True, "the trimmed section is what lands")

print("== the report index ==")
ms.put_tier2_scan("db-3", {"scan_time": "2026-10-06T09:00:00Z", "report_s3_key": "k/db-3/r.html", "health_score": "ok"}, {})
ms.put_tier2_scan("db-3", {"scan_time": "2026-10-07T09:00:00Z", "report_s3_key": "k/db-3/r.html",
                           "health_score": "warning"}, {})
check(ms.tier2_by_report_key("k/db-3/r.html")["health_score"] == "warning", "the newest scan for a key")
check(ms.tier2_by_report_key("k/none") is None, "no scan for a key: None")

print("== the collector's baseline is strictly earlier ==")
now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
t_prev, t_cur = (now - timedelta(minutes=5)).isoformat(), now.isoformat()
ms.put_tier1_rows("db-9", [{"scan_time": t_prev, "member_instance_id": "m", "deadlocks": 1},
                           {"scan_time": t_cur, "member_instance_id": "m", "deadlocks": 4}])
prev = ms.tier1_previous("db-9", "m", t_cur)
check(prev and prev["deadlocks"] == 1, "a retried run of a written slot compares against the slot before it")
check(ms.tier1_previous("db-9", "m", t_prev) is None, "nothing earlier: None")
check(ms.tier1_previous("db-9", "other", t_cur) is None, "another member's samples are not used")

print("== CloudWatch calls stay inside their limits ==")
rows = [{"scan_time": (now - timedelta(minutes=i)).isoformat(), "member_instance_id": f"m{i % 3}",
         "member_role": "reader", **{k: 1.0 for k in ms.TIER1_NUMERIC_METRICS}} for i in range(30)]
n = ms.publish_tier1_metrics("db-cw", rows)
check(n == 30 * len(ms.TIER1_NUMERIC_METRICS) * 2, f"every numeric, both dimension sets ({n} datums)")
check(len(fake.cw.put_calls) == -(-n // 1000) and all(c["count"] <= 1000 for c in fake.cw.put_calls),
      "chunked to 1000 datums per PutMetricData")
check(all(d["StorageResolution"] == 60 for d in fake.cw.datums), "Tier 1 at 60-second storage resolution")
check(all({x["Name"] for x in d["Dimensions"]} in ({"InstanceId", "Member"}, {"InstanceId", "Role"})
          for d in fake.cw.datums), "dimensions: InstanceId with Member or Role, nothing else")
check(ms.publish_tier1_metrics("db-cw", [{"scan_time": t_cur, "cache_hit_ratio": None}]) == 0,
      "nothing numeric: no call")
k = ms.publish_tier2_metrics("db-cw", "m0", "writer", t_cur, {"findings_total": 3, "bad": "x", "none": None})
check(k == 2 and "StorageResolution" not in fake.cw.datums[-1], "Tier 2: numerics only, standard resolution")
fake.cw.get_calls.clear()
ms.fleet_daily_series([f"db-{i}" for i in range(60)], 30, list(ms.TIER1_NUMERIC_METRICS))
check(len(fake.cw.get_calls) == -(-60 * len(ms.TIER1_NUMERIC_METRICS) // 500)
      and all(len(c["queries"]) <= 500 for c in fake.cw.get_calls), "chunked to 500 queries per GetMetricData")

print("== chart periods ==")
for hours, floor in ((1, 60), (24, 60), (24 * 15, 60), (24 * 16, 300), (24 * 63, 300), (24 * 64, 3600),
                     (24 * 450, 3600)):
    p = ms.chart_period(hours)
    check(p >= floor and hours * 3600 / p <= ms.MAX_SERIES_POINTS + 1 and p % 60 == 0,
          f"{hours} h → {p} s (≥ {floor} s, ≤ ~{ms.MAX_SERIES_POINTS} points)")
check(ms._metric_unit("txn_per_sec") == "Count/Second" and ms._metric_unit("longest_transaction_sec") == "Seconds"
      and ms._metric_unit("replica_lag_ms") == "Milliseconds" and ms._metric_unit("database_size_mb") == "Megabytes"
      and ms._metric_unit("rollback_ratio_pct") == "Percent" and ms._metric_unit("deadlocks_delta") == "Count",
      "units per metric (a rate is not a duration)")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
