# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Charts read server-bucketed metrics, one request per chart (#23/charts).

The bucketed route returns at most ~300 points per window — CloudWatch's average per
fixed period, per member for a cluster, with per-bucket min/max — in ONE
GetMetricData call per ~500 series, for windows up to 15 months. The raw-sample route
(DynamoDB) stays for exports and record views.

Drives the real store code over an in-memory CloudWatch (tests/fake_metrics_store).

Run:  python3 backend/tests/test_metrics_bucketed.py
"""
import os
import pathlib
import re
import sys
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import fake_metrics_store  # noqa: E402
from tools.aws import metrics_store as ms  # noqa: E402
from utils import scan_records  # noqa: E402

APP_SRC = (pathlib.Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def _rows(member, role, values, step_min=5):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    return [{"scan_time": (now - timedelta(minutes=step_min * (i + 1))).isoformat(), "member_instance_id": member,
             "member_role": role, "cache_hit_ratio": v, "txn_per_sec": v / 10} for i, v in enumerate(values)]


# ── the period ────────────────────────────────────────────────────────────────
print("the period keeps a chart at ~300 points and never finer than CloudWatch keeps")
check(ms.chart_period(1) == 60, "an hour is minute buckets")
p7 = ms.chart_period(168)
check(168 * 3600 / p7 <= 301 and p7 % 60 == 0, f"7 days is ≤300 buckets ({p7} s)")
check(ms.chart_period(24 * 30) >= 300, "past 15 days the period is at least 5 minutes (CloudWatch's retention)")
check(ms.chart_period(24 * 365) >= 3600 and ms.chart_period(24 * 365) % 300 == 0, "past 63 days it is at least an hour")

# ── publish and read back ─────────────────────────────────────────────────────
print("a published collection reads back as bucketed avg/min/max")
fake = fake_metrics_store.install()
standalone = {"instance_id": "db-1", "arn": "arn:aws:rds:eu-west-1:1:db:db-1"}
n = ms.publish_tier1_metrics("db-1", _rows("db-1", "writer", [90.0, 98.0, 99.0], step_min=1))
check(n == 3 * 2 * 2, "each numeric goes to both dimension sets (Member and Role)")
check(all(c["Namespace"] == fake_metrics_store.NAMESPACE for c in fake.cw.put_calls), "one per-install namespace")
dims = {tuple(sorted(x["Name"] for x in d["Dimensions"])) for d in fake.cw.datums}
check(dims == {("InstanceId", "Member"), ("InstanceId", "Role")}, "dimensions carry only opaque ids and the role")
units = {d["MetricName"]: d["Unit"] for d in fake.cw.datums}
check(units["txn_per_sec"] == "Count/Second" and units["cache_hit_ratio"] == "Percent", "units per metric")
rows, eff = scan_records.metrics_bucketed(standalone, hours=24, max_buckets=1)
check(len(fake.cw.get_calls) == 1, "one GetMetricData call")
check(eff["hours"] == 24 and eff["bucket_seconds"] == ms.chart_period(24, 1), "the period is echoed")
check(len(rows) in (1, 2) and rows[0]["member_instance_id"] == "db-1", "the standalone instance reads its Member series")
merged = [r for r in rows if r.get("samples")]
check(sum(r["samples"] for r in merged) == 3, "every sample is counted once")
lo = min(r["cache_hit_ratio_min"] for r in rows)
hi = max(r["cache_hit_ratio_max"] for r in rows)
check(lo == 90.0 and hi == 99.0, "per-bucket min/max carry the range")
_, eff = scan_records.metrics_bucketed(standalone, hours=99999)
check(eff["hours"] == scan_records.MAX_CHART_HOURS, "the window is clamped to 15 months")

print("a cluster reads the continuous writer series, or each reader")
fake = fake_metrics_store.install()
cluster = {"instance_id": "orders", "cluster_member_roles": [
    {"instance_id": "orders-1", "is_writer": False}, {"instance_id": "orders-2", "is_writer": True},
    {"instance_id": "orders-3", "is_writer": False}]}
# orders-1 was the writer before a failover; orders-2 is now.
ms.publish_tier1_metrics("orders", _rows("orders-1", "writer", [91.0], step_min=30)
                         + _rows("orders-2", "writer", [92.0])
                         + _rows("orders-1", "reader", [81.0]) + _rows("orders-3", "reader", [83.0]))
rows, _ = scan_records.metrics_bucketed(cluster, hours=2)
check({r["member_role"] for r in rows} == {"writer"} and {r["member_instance_id"] for r in rows} == {None},
      "the default is the Role=writer series (continuous across the failover)")
check(sorted(r["cache_hit_ratio"] for r in rows) == [91.0, 92.0], "both writers' samples are in it")
q = fake.cw.get_calls[-1]["queries"]
check(all({"Name": "Role", "Value": "writer"} in x["MetricStat"]["Metric"]["Dimensions"] for x in q),
      "the query is built server-side from the record (Role=writer)")
rows, _ = scan_records.metrics_bucketed(cluster, hours=2, member="reader")
check(sorted({r["member_instance_id"] for r in rows}) == ["orders-1", "orders-3"], "reader: one series per current reader")
rows, _ = scan_records.metrics_bucketed(cluster, hours=2, member="instance:orders-3")
check([r["cache_hit_ratio"] for r in rows] == [83.0], "one member by id")
try:
    scan_records.metrics_bucketed(cluster, member="instance:x y")
    check(False, "a bad selector is refused")
except ValueError:
    check(True, "a bad selector is refused")

# ── the route ───────────────────────────────────────────────────────────────
print("the route reads once and echoes the bucket size")


class FakeHTTPException(Exception):
    def __init__(self, status_code, detail=""):
        self.status_code, self.detail = status_code, detail


def build(configured=True, record=None):
    calls = []
    ns = {
        "HTTPException": FakeHTTPException, "Request": object, "scan_records": scan_records,
        "logger": types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
        "_can_view_instance": lambda a, i: True,
        "_store_is_configured": lambda: configured,
        "_STORE_NOT_CONFIGURED": "The metrics store is not configured",
        "_store_call": lambda fn: calls.append(1) or fn(),
        "_record_or_404": lambda i: record or standalone,
        "_redact_identifiers": lambda s: s,
    }
    m = re.search(r"^def get_metrics_bucketed\(.*?(?=^@app\.)", APP_SRC, re.S | re.M)
    exec(m.group(0), ns)  # noqa: S102 - pinning shipped source
    return ns["get_metrics_bucketed"], calls


REQ = types.SimpleNamespace(headers={"Authorization": "Bearer x"})
fake = fake_metrics_store.install()
ms.publish_tier1_metrics("db-1", _rows("db-1", "writer", [99.0]))
fn, calls = build()
out = fn("db-1", REQ, hours=24)
check(len(calls) == 1, "exactly one store read (no paging loop)")
check(out["rows"] and out["bucket_seconds"] >= 60 and out["hours"] == 24, "rows and the bucket size are returned")
check("txn_per_sec" in out["metrics"] and "deadlocks_delta" in out["metrics"], "the new counters are chartable")
fn, _ = build(configured=False)
try:
    fn("db-1", REQ)
    check(False, "unconfigured store is 503")
except FakeHTTPException as e:
    check(e.status_code == 503, "unconfigured store is 503")
fn, _ = build()
try:
    fn("db-1", REQ, member="primary")
    check(False, "a bad selector is 400")
except FakeHTTPException as e:
    check(e.status_code == 400, "a bad selector is 400")

# ── the chart calls it once; the raw route is kept for records ──────────────────
print("the chart uses the bucketed route; records keep the raw route")
TC = (pathlib.Path(__file__).resolve().parents[2] / "frontend/src/components/TrendChart.jsx").read_text()
check("/bucketed?hours=" in TC and "MAX_PAGES" not in TC, "TrendChart calls the bucketed route, no paging loop")
RS = (pathlib.Path(__file__).resolve().parents[2] / "admin-console/src/components/RecordsSheet.jsx").read_text()
check("/api/scan-metrics/${instanceId}?hours=" in RS, "the records sheet keeps the raw paged route")
print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
