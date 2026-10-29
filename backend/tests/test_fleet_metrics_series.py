# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Trends shows Tier 1 data from the first collection (item 15).

The fleet Trends drew only Tier 2 deep-scan rows and said "No trend data yet" when there
were no deep scans, even though Tier 1 metrics existed. The backend now serves a fleet
Tier 1 series — the writer's daily average per database from CloudWatch — in one read, so the
headline metrics draw continuously from the first collection; deep scans overlay health
and findings on the same timeline.

Pins ``scan_records.fleet_metrics_series`` (over an in-memory CloudWatch) and the ``/metrics-series`` route, plus
the Trends component merging both tiers. Hermetic, no AWS.

Run:  python3 backend/tests/test_fleet_metrics_series.py
"""
import os
import pathlib
import re
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
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


def _extract(name):
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^(?:@|def |class |[A-Z_]+ = )|\Z)", APP_SRC, re.S | re.M)
    assert m, f"{name} not found"
    return m.group(0)


# ── the series ─────────────────────────────────────────────────────────────────
print("the fleet Tier 1 series: the writer's daily average per database from CloudWatch")
from datetime import datetime, timedelta, timezone  # noqa: E402
sys.path.insert(0, os.path.dirname(__file__))
import fake_metrics_store  # noqa: E402
from tools.aws import metrics_store as ms  # noqa: E402

fake = fake_metrics_store.install()
today = datetime.now(timezone.utc).replace(hour=0, minute=30, second=0, microsecond=0)
yesterday = today - timedelta(days=1)


def _row(member, role, when, chr_, conns):
    return {"scan_time": when.isoformat(), "member_instance_id": member, "member_role": role,
            "cache_hit_ratio": chr_, "total_connections": conns, "dead_tuple_pct": 2.0,
            "database_size_mb": 2048.0, "blocked_queries": 0, "txn_per_sec": 5.0}


ms.publish_tier1_metrics("db-1", [_row("db-1a", "writer", yesterday, 98.0, 10),
                                  _row("db-1a", "writer", yesterday + timedelta(minutes=5), 96.0, 14),
                                  _row("db-1b", "reader", yesterday, 10.0, 99),
                                  _row("db-1a", "writer", today, 99.0, 12)])
ms.publish_tier1_metrics("db-2", [_row("db-2", "writer", today, 95.0, 3)])
series, days = scan_records.fleet_metrics_series(["db-1", "db-2", "db-3"], days=7)
check(days == 7 and set(series) == {"db-1", "db-2"}, "a database with no series is absent, not zero-filled")
pts = series["db-1"]
check([p["date"] for p in pts] == [yesterday.date().isoformat(), today.date().isoformat()],
      "one point per day, oldest first")
check(pts[0]["cache_hit_ratio"] == 97.0 and pts[0]["connections"] == 12.0,
      "the writer's daily AVERAGE; the reader is not mixed in; connections from total_connections")
check("txn_per_sec" in pts[0] and "total_connections" not in pts[0], "new counters ride along; the key is renamed")
q = fake.cw.get_calls[-1]["queries"]
check(all(x["MetricStat"]["Period"] == 86400 and x["MetricStat"]["Stat"] == "Average" for x in q), "daily averages")
check(all({"Name": "Role", "Value": "writer"} in x["MetricStat"]["Metric"]["Dimensions"] for x in q),
      "the writer's continuous series")
check({d["Value"] for x in q for d in x["MetricStat"]["Metric"]["Dimensions"] if d["Name"] == "InstanceId"}
      == {"db-1", "db-2", "db-3"}, "only the ids the route passed are queried")
for col in ("cache_hit_ratio", "dead_tuple_pct", "xid_age_pct", "total_connections",
            "database_size_mb", "blocked_queries", "txn_per_sec", "deadlocks_delta"):
    check(col in scan_records.FLEET_SERIES_METRICS, f"the series carries {col}")
check("findings" not in scan_records.FLEET_SERIES_METRICS, "no Tier 2 columns (that is /trends)")
_, d2 = scan_records.fleet_metrics_series(["db-1"], days=9999)
check(d2 == scan_records.MAX_FLEET_SERIES_DAYS, f"days is clamped to CloudWatch's 15 months ({d2})")
check(scan_records.fleet_metrics_series([], days=7) == ({}, 7), "no databases: no call")

# ── the route ──────────────────────────────────────────────────────────────────
print("the route returns per-database day points")


class FakeHTTPException(Exception):
    def __init__(self, status_code, detail=""):
        self.status_code, self.detail = status_code, detail


def build(configured=True, viewer_ok=True):
    calls = []
    ns = {
        "HTTPException": FakeHTTPException, "Request": object, "scan_records": scan_records,
        "logger": types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
        "_can_view_space": lambda a, s: viewer_ok,
        "_instance_in_space": lambda d, sid: sid in (d.get("member_space_ids") or ()),
        "_store_is_configured": lambda: configured,
        "_store_call": lambda fn: calls.append(1) or fn(),
        "db_registry": types.SimpleNamespace(list_instances=lambda: [
            {"instance_id": "db-1", "display_name": "orders", "status": "configured",
             "engine": "aurora-postgresql", "region": "eu-west-1", "member_space_ids": {"s1"}},
            {"instance_id": "db-2", "display_name": "other", "status": "configured",
             "engine": "postgres", "region": "eu-west-1", "member_space_ids": {"s2"}}]),
    }
    exec(_extract("get_space_metrics_series"), ns)  # noqa: S102
    return ns["get_space_metrics_series"], calls


REQ = types.SimpleNamespace(headers={"Authorization": "Bearer x"})
fn, calls = build()
out = fn("s1", REQ, days=7)
check(len(calls) == 1, "one store read for the whole fleet")
check([d["instance_id"] for d in out["databases"]] == ["db-1"], "only the space's databases are read and returned")
db = out["databases"][0]
check(len(db["points"]) == 2 and db["points"][1]["cache_hit_ratio"] == 99.0, "the database's day points are returned")
fn, calls = build(configured=False)
out = fn("s1", REQ, days=7)
check(out["databases"] == [] and not calls, "unconfigured store: no read, empty series (no error)")
fn, _ = build(viewer_ok=False)
try:
    fn("s1", REQ)
    check(False, "a non-member is refused")
except FakeHTTPException as e:
    check(e.status_code == 403, "access is gated on the space")
# ── the Trends component merges both tiers ──────────────────────────────────────
print("the Trends component draws Tier 1 and overlays Tier 2")
TR = (pathlib.Path(__file__).resolve().parents[2] / "frontend/src/components/Trends.jsx").read_text()
check("/metrics-series?days=" in TR, "Trends fetches the Tier 1 fleet series")
check("tier1Series" in TR and "scanResults" in TR, "it merges the Tier 1 series with the Tier 2 scans")
check("health_score: 'unknown'" in TR, "a Tier 1-only day has no verdict until a deep scan overlays it")
# hasData now counts any data_points (Tier 1 or Tier 2), so 'No trend data yet' only when both are empty.
check("const hasData = databases.some(db => db.data_points?.length > 0)" in TR,
      "'No trend data yet' is gated on any data_points, which now include Tier 1")


print("")
print("== the fleet series picks any published metric, and only those ==")
import importlib as _il  # noqa: E402
_sr = _il.import_module("utils.scan_records")
check(_sr.parse_fleet_metrics("") == _sr.FLEET_SERIES_METRICS, "no pick: the headline set")
check(_sr.parse_fleet_metrics("connections_used_pct") == ("connections_used_pct",),
      "a Tier 1 series outside the headline set can be picked")
check(_sr.parse_fleet_metrics("findings_total,index_cache_hit_ratio") == ("findings_total", "index_cache_hit_ratio"),
      "deep-scan daily series can be picked, several at once")
check(_sr.parse_fleet_metrics("cache_hit_ratio, cache_hit_ratio ,") == ("cache_hit_ratio",),
      "duplicates and blanks are dropped")
for bad in ("not_a_metric", "cache_hit_ratio,evil", "InstanceId", "*"):
    try:
        _sr.parse_fleet_metrics(bad)
        check(False, f"{bad!r} is refused")
    except ValueError:
        check(True, f"{bad!r} is refused: only names on the server's own list reach CloudWatch")
too_many = ",".join(_sr.ALL_SERIES_METRICS[:_sr.MAX_FLEET_METRICS + 1])
try:
    _sr.parse_fleet_metrics(too_many)
    check(False, "a pick above the cap is refused")
except ValueError:
    check(True, f"a pick above {_sr.MAX_FLEET_METRICS} metrics is refused (databases x metrics fan-out)")
check(set(_sr.TIER2_SERIES_METRICS).isdisjoint(_sr.BUCKET_METRICS), "Tier 2 daily names never reuse a Tier 1 name")
check("connections_used_pct" in _sr.BUCKET_METRICS and "connections_used_pct" in _sr.METRIC_COLUMNS,
      "connections_used_pct (the saturation alarm's series) is in the record and chart projections")

print("")
print("== a per-database chart reads the Tier 1 or the Tier 2 series, by name ==")
check(_sr.parse_series("") == "tier1" and _sr.parse_series("tier1") == "tier1", "tier1 is the default")
check(_sr.parse_series("TIER2") == "tier2", "tier2 selects the deep-scan dailies")
check(_sr.SERIES_SETS["tier2"] == _sr.TIER2_SERIES_METRICS and _sr.SERIES_SETS["tier1"] == _sr.BUCKET_METRICS,
      "each name maps to its own metric set")
try:
    _sr.parse_series("tier3")
    check(False, "an unknown series is refused")
except ValueError:
    check(True, "an unknown series is refused")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
