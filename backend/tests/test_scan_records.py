# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tier 1 / Tier 2 record browsing (utils.scan_records over tools.aws.metrics_store).

The records views read the two DynamoDB tables. Every read must be a keyed Query on the
database's own partition (never a Scan, never a key the caller chose), clamp every
window, page by keyset on ``(scan_time, member)`` so a page ending inside one
collection slot loses no member's row, and keep the section data out of list
responses. The collection-status merge must judge "stale" against the configured
cadence, not a fixed number, and never drop a database that has no rows yet.

Hermetic: an in-memory DynamoDB (tests/fake_metrics_store), no AWS.

Run:  python3 backend/tests/test_scan_records.py   (or via pytest)
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import fake_metrics_store  # noqa: E402
from tools.aws import metrics_store as ms  # noqa: E402
from utils import scan_records as sr  # noqa: E402


def _now():
    return datetime.now(timezone.utc).replace(second=0, microsecond=0)


def _t1_row(member, minutes_ago, role="writer", **metrics):
    return dict({"scan_time": (_now() - timedelta(minutes=minutes_ago)).isoformat(),
                 "member_instance_id": member, "member_role": role}, **metrics)


CLUSTER = {"instance_id": "orders", "cluster_member_roles": [
    {"instance_id": "orders-1", "is_writer": True},
    {"instance_id": "orders-2", "is_writer": False},
    {"instance_id": "orders-3", "is_writer": False}]}
STANDALONE = {"instance_id": "plain", "arn": "arn:aws:rds:eu-west-1:1:db:plain"}


def _raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except ValueError as e:
        return str(e)
    return None


# ── Tier 1 history ─────────────────────────────────────────────────────────────

def test_metrics_history_clamps_the_window_and_reads_newest_first():
    fake = fake_metrics_store.install()
    ms.put_tier1_rows("plain", [_t1_row("plain", m, cache_hit_ratio=99.0 - m) for m in (5, 10, 15)])
    rows, eff = sr.metrics_history(STANDALONE, hours=9999, limit=-5)
    assert eff["hours"] == sr.MAX_HOURS and eff["limit"] == 1 and eff["before"] is None
    assert len(rows) == 1 and rows[0]["cache_hit_ratio"] == 94.0, "newest first"
    rows, eff = sr.metrics_history(STANDALONE, hours="soon", limit=None)
    assert eff["hours"] == sr.DEFAULT_HOURS and eff["limit"] == sr.DEFAULT_LIMIT
    assert [r["cache_hit_ratio"] for r in rows] == [94.0, 89.0, 84.0]
    # Every read is a keyed Query on this database's partition.
    assert fake.t1.queries and all(q["index"] is None for q in fake.t1.queries)


def test_a_row_round_trips_with_lists_new_counters_and_a_ttl():
    fake = fake_metrics_store.install()
    row = _t1_row("plain", 1, cache_hit_ratio=99.5, txn_per_sec=12.25, deadlocks_delta=2,
                  rollback_ratio_pct=0.5, temp_files=3, table_bloat=[{"table": "t", "pct": 40}],
                  wait_events=[{"event": "Lock", "count": 2}], blocked_queries=None)
    assert ms.put_tier1_rows("plain", [row], retention_days=7) == 1
    (item,) = fake.t1.items.values()
    assert item["expires_at"] - int(datetime.fromisoformat(item["scan_time"]).timestamp()) == 7 * 86400
    assert "blocked_queries" not in item, "a missing metric is absent, not a null"
    (got,), _ = sr.metrics_history(STANDALONE)
    assert got["txn_per_sec"] == 12.25 and got["deadlocks_delta"] == 2 and got["temp_files"] == 3
    assert got["table_bloat"] == [{"table": "t", "pct": 40}] and got["wait_events"][0]["event"] == "Lock"
    assert "expires_at" not in got and got["id"] == f"plain#{got['scan_time']}"
    assert got["scan_time"].endswith(".000+00:00"), "scan times are canonical (UTC, ms) so key order is time order"
    # The default retention is 30 days.
    fake = fake_metrics_store.install()
    ms.put_tier1_rows("plain", [row])
    (item,) = fake.t1.items.values()
    assert item["expires_at"] - int(datetime.fromisoformat(item["scan_time"]).timestamp()) == 30 * 86400


def test_cursors_are_validated():
    fake_metrics_store.install()
    for bad in ("2026-09-24T10:00:00Z|x y", "2026-09-24T10:00:00Z|1;DROP", "nope|db-a",
                "yesterday; DROP TABLE x"):
        assert _raises(sr.metrics_history, STANDALONE, before=bad) is not None, bad
    _, eff = sr.metrics_history(STANDALONE, before="2026-09-24T10:00:00Z|plain")
    assert eff["before"] == "2026-09-24T10:00:00+00:00|plain"
    _, eff = sr.metrics_history(STANDALONE, before="2026-09-24T10:00:00Z")
    assert eff["before"] == "2026-09-24T10:00:00+00:00"


def test_paging_through_one_slot_of_cluster_members_loses_no_row():
    # One Tier 1 run writes a row per member with the SAME scan_time. A page that ends
    # inside that slot must resume inside it.
    fake_metrics_store.install()
    record = {"instance_id": "big", "cluster_member_roles": [{"instance_id": "w", "is_writer": True}] + [
        {"instance_id": f"r{i}", "is_writer": False} for i in range(1, 6)]}
    rows = []
    for slot in (5, 10):
        rows += [_t1_row("w", slot)] + [_t1_row(f"r{i}", slot, role="reader") for i in range(1, 6)]
    ms.put_tier1_rows("big", rows)
    seen, before = [], None
    for _ in range(10):
        page, eff = sr.metrics_history(record, limit=3, before=before, member="reader")
        seen += page
        info = sr.page_info(page, eff["limit"])
        if not info["has_more"]:
            break
        before = info["next_before"]
    keys = [(r["scan_time"], r["member_instance_id"]) for r in seen]
    assert len(keys) == 10 and len(set(keys)) == 10, keys
    assert all(r["member_role"] == "reader" for r in seen)
    assert keys == sorted(keys, reverse=True), "newest first, then member"


def test_member_selectors_narrow_the_history():
    fake_metrics_store.install()
    ms.put_tier1_rows("orders", [_t1_row("orders-1", 1), _t1_row("orders-2", 1, role="reader"),
                                 _t1_row("orders-3", 1, role="reader"),
                                 # orders-2 before a failover: it was the writer then.
                                 _t1_row("orders-2", 30, role="writer")])
    by = lambda m: sorted((r["member_instance_id"], r["member_role"]) for r in sr.metrics_history(CLUSTER, member=m)[0])
    assert by("") == [("orders-1", "writer"), ("orders-2", "writer")], "a cluster's default is the observed writer"
    assert by("writer") == by("")
    assert by("reader") == [("orders-2", "reader"), ("orders-3", "reader")]
    assert by("instance:orders-3") == [("orders-3", "reader")]
    assert sr.metrics_history_count(CLUSTER, member="reader") == 2
    assert sr.metrics_history_count(CLUSTER) == 2
    assert _raises(sr.metrics_history, CLUSTER, member="primary") == sr.MEMBER_ERROR
    # A single instance is its own one member: a role selector does not narrow it.
    ms.put_tier1_rows("plain", [_t1_row("plain", 1, role="reader")])
    assert len(sr.metrics_history(STANDALONE, member="writer")[0]) == 1


def test_the_count_ignores_the_cursor_and_degrades_to_none():
    fake = fake_metrics_store.install()
    ms.put_tier1_rows("plain", [_t1_row("plain", m) for m in range(1, 8)])
    assert sr.metrics_history_count(STANDALONE, hours=9999) == 7

    def boom(**kw):
        raise RuntimeError("throttled")
    fake.t1.query = boom
    assert sr.metrics_history_count(STANDALONE) is None, "a count failure degrades the pager only"


# ── Tier 2 list ────────────────────────────────────────────────────────────────

def _scan(iid, hours_ago, member=None, role="writer", kind="full", health="ok", **extra):
    ts = (_now() - timedelta(hours=hours_ago)).isoformat()
    summary = dict({"scan_time": ts, "member_instance_id": member or iid, "member_role": role,
                    "scan_kind": kind, "health_score": health, "report_s3_key": f"reports/{iid}/{hours_ago}.html",
                    "findings": [{"severity": "high", "title": "x"}]}, **extra)
    return ms.put_tier2_scan(iid, summary, {"overview": {"version": "16.4"},
                                            "performance": {"top_queries": [{"query": "SELECT ?"}]}})


def test_scan_results_never_return_section_data():
    fake_metrics_store.install()
    sid = _scan("a", 1)
    (row,), _ = sr.scan_results(["a"])
    assert row["id"] == sid and "data" not in row and "overview" not in row
    assert row["sections"] == ["overview", "performance"], "the list says which sections exist"
    assert {"findings", "report_s3_key", "health_score"} <= set(row)


def test_scan_results_filter_clamp_and_validate():
    fake_metrics_store.install()
    _scan("a", 1, health="critical")
    _scan("a", 2, health="ok")
    _scan("b", 3, health="critical")
    _scan("a", 4, member="a-reader", role="reader", kind="reader", health="warning")
    rows, eff = sr.scan_results(["a", "b"], days=5000, limit=10, health="CRITICAL")
    assert eff["days"] == sr.MAX_DAYS and eff["health"] == "critical"
    assert [(r["instance_id"], r["health_score"]) for r in rows] == [("a", "critical"), ("b", "critical")]
    rows, _ = sr.scan_results(["a"], kind="reader")
    assert [r["member_instance_id"] for r in rows] == ["a-reader"]
    rows, _ = sr.scan_results(["a"], member="reader", is_cluster=True)
    assert [r["member_role"] for r in rows] == ["reader"]
    rows, _ = sr.scan_results(["a"], is_cluster=True)
    assert all(r["member_role"] == "writer" for r in rows), "a cluster's default is the writer's scans"
    assert sr.scan_results_count(["a", "b"], health="critical") == 2
    assert sr.scan_results_count(["a"], kind="full") == 2
    assert sr.scan_results_count([]) == 0
    assert _raises(sr.scan_results, ["a"], health="fine") is not None
    assert _raises(sr.scan_results, []) is not None
    assert _raises(sr.scan_results, ["a"], kind="sideways") is not None
    _, eff = sr.scan_results(["a"])
    assert eff["limit"] == sr.DEFAULT_RESULTS_LIMIT == 200


def test_scan_results_page_by_keyset():
    fake_metrics_store.install()
    for h in range(1, 8):
        _scan("a", h)
    seen, before = [], None
    for _ in range(10):
        page, eff = sr.scan_results(["a"], limit=3, before=before)
        seen += page
        info = sr.page_info(page, eff["limit"])
        if not info["has_more"]:
            break
        before = info["next_before"]
    assert len(seen) == 7 and len({r["id"] for r in seen}) == 7


def test_space_trends_keep_the_newest_full_scan_per_day():
    fake_metrics_store.install()
    _scan("a", 0.5, health="critical")
    _scan("a", 0.6, health="ok")
    _scan("a", 0.4, member="a-r", role="reader", kind="reader", health="warning")
    _scan("a", 50, health="warning")
    series, days = sr.space_trends(["a"], days=9999)
    assert days == sr.MAX_DAYS
    pts = series["a"]
    assert [p["scan_time"] < q["scan_time"] for p, q in zip(pts, pts[1:])] == [True] * (len(pts) - 1)
    assert pts[-1]["health_score"] in ("critical", "ok") and all(p["scan_kind"] == "full" for p in pts)


# ── paging hint ────────────────────────────────────────────────────────────────

def test_page_info_signals_more_only_on_a_full_page():
    rows = [{"scan_time": f"2026-09-24T10:0{i}:00.000+00:00"} for i in range(3)]
    full = sr.page_info(rows, 3)
    assert full["has_more"] is True and full["next_before"] == rows[-1]["scan_time"]  # no member: bare time
    with_members = [dict(r, member_instance_id=f"db-{i}") for i, r in enumerate(rows)]
    assert sr.page_info(with_members, 3)["next_before"] == f"{rows[-1]['scan_time']}|db-2"
    partial = sr.page_info(rows[:2], 3)
    assert partial["has_more"] is False and partial["next_before"] is None
    assert sr.page_info([], 3) == {"count": 0, "has_more": False, "next_before": None}


def test_page_info_carries_the_window_total_only_when_known():
    rows = [{"scan_time": "2026-09-24T10:00:00+00:00"}]
    assert sr.page_info(rows, 50, total=288)["total"] == 288
    assert sr.page_info(rows, 50, total="7")["total"] == 7
    assert "total" not in sr.page_info(rows, 50)
    assert "total" not in sr.page_info(rows, 50, total=None)


# ── collection status ──────────────────────────────────────────────────────────

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def _ago(**kw):
    return (NOW - timedelta(**kw)).isoformat()


def test_collection_state_is_judged_against_the_configured_cadence():
    assert sr.collection_state(_ago(minutes=8), True, "5min", 1, NOW) == "fresh"
    assert sr.collection_state(_ago(minutes=12), True, "5min", 1, NOW) == "stale"
    assert sr.collection_state(_ago(minutes=12), True, "30min", 1, NOW) == "fresh"
    assert sr.collection_state(_ago(hours=30), True, "daily", 2, NOW) == "fresh"
    assert sr.collection_state(_ago(hours=40), True, "daily", 2, NOW) == "stale"
    assert sr.collection_state(_ago(days=9), False, "5min", 1, NOW) == "paused"
    assert sr.collection_state(None, True, "5min", 1, NOW) == "none"
    assert sr.collection_state(_ago(minutes=8), True, "bogus", 1, NOW) == "fresh"


def test_merge_status_keeps_every_database_and_derives_states():
    views = [
        {"instance_id": "a", "tier1_frequency": "5min", "tier1_enabled": True, "tier2_frequency": "daily", "tier2_enabled": True},
        {"instance_id": "b", "tier1_frequency": "1min", "tier1_enabled": False, "tier2_frequency": "weekly", "tier2_enabled": True},
        {"instance_id": "c"},
    ]
    t1 = [{"instance_id": "a", "tier1_last_at": _ago(minutes=3), "tier1_count_24h": 280}]
    t2 = [{"instance_id": "a", "tier2_last_at": _ago(hours=5), "tier2_last_health": "warning", "tier2_count_30d": 30},
          {"instance_id": "b", "tier2_last_at": _ago(days=20), "tier2_last_health": "ok", "tier2_count_30d": 2}]
    by = {v["instance_id"]: v for v in sr.merge_status(views, t1, t2, NOW)}
    assert set(by) == {"a", "b", "c"}
    assert by["a"]["tier1_state"] == "fresh" and by["a"]["tier1_count_24h"] == 280
    assert by["a"]["tier2_state"] == "fresh" and by["a"]["tier2_last_health"] == "warning"
    assert by["b"]["tier1_state"] == "paused" and by["b"]["tier1_last_at"] is None
    assert by["b"]["tier2_state"] == "stale"
    assert by["c"]["tier1_state"] == "none" and by["c"]["tier2_state"] == "none"
    assert by["c"]["tier1_count_24h"] == 0 and by["c"]["tier2_count_30d"] == 0
    assert by["a"]["tier1_frequency"] == "5min"


def test_status_counts_a_standalone_replica_and_only_a_clusters_writer():
    fake_metrics_store.install()
    ms.put_tier1_rows("orders", [_t1_row("orders-1", 1), _t1_row("orders-2", 1, role="reader"),
                                 _t1_row("orders-3", 1, role="reader")])
    ms.put_tier1_rows("inv", [_t1_row("inv-replica", 2, role="reader"), _t1_row("inv-replica", 7, role="reader")])
    _scan("orders", 1)
    _scan("orders", 2, member="orders-2", role="reader", kind="reader")
    replica = {"instance_id": "inv", "arn": "arn:aws:rds:eu-west-1:1:db:inv-replica", "tags": {"replica_of": "inv-primary"}}
    empty = {"instance_id": "empty"}
    t1, t2 = sr.status_rows([CLUSTER, replica, empty])
    by1 = {r["instance_id"]: r for r in t1}
    by2 = {r["instance_id"]: r for r in t2}
    assert by1["orders"]["tier1_count_24h"] == 1, "a cluster counts its writer's rows only"
    assert by1["inv"]["tier1_count_24h"] == 2, "a standalone read replica's reader rows count"
    assert by1["empty"]["tier1_count_24h"] == 0 and by1["empty"]["tier1_last_at"] is None
    assert by2["orders"]["tier2_count_30d"] == 1, "reader scans are not the database's deep scan"
    assert by2["orders"]["tier2_last_health"] == "ok" and by2["empty"]["tier2_last_at"] is None


# ── cluster members ────────────────────────────────────────────────────────────

def test_parse_member_accepts_the_focus_grammar_only():
    for ok in ("", "writer", "reader", "instance:db-b", " WRITER ", "instance:a"):
        assert sr.parse_member(ok) == ok.strip().lower()
    assert sr.parse_member(None) == ""
    for bad in ("primary", "instance:", "instance:DB_B!", "instance:" + "a" * 64,
                "instance:db-b' OR '1'='1", "writer;", "readers"):
        assert _raises(sr.parse_member, bad) == sr.MEMBER_ERROR, bad


def test_member_filter_per_selector_and_record_kind():
    ids = ["a", "b"]
    assert ms.member_filter("", True, ids) == (ids, "writer")
    assert ms.member_filter("", False, ids) == (ids, None)
    assert ms.member_filter("reader", True, ids) == (ids, "reader")
    assert ms.member_filter("reader", False, ids) == (ids, None), "a single instance is its own one member"
    assert ms.member_filter("instance:b", True, ids) == (["b"], None)


def test_new_columns_are_listed():
    for c in ("member_instance_id", "member_role", "replica_lag_ms", "txn_per_sec", "deadlocks_delta",
              "rollback_ratio_pct", "temp_files"):
        assert c in sr.METRIC_COLUMNS
    for c in ("member_instance_id", "member_role", "scan_kind", "sections"):
        assert c in sr.RESULT_COLUMNS
    assert "data" not in sr.RESULT_COLUMNS
    for c in ("deadlocks_delta", "txn_per_sec", "rollback_ratio_pct", "temp_files"):
        assert c in ms.TIER1_NUMERIC_METRICS and c in sr.BUCKET_METRICS


def test_member_status_rows_read_each_member():
    fake_metrics_store.install()
    ms.put_tier1_rows("orders", [_t1_row("orders-1", 1), _t1_row("orders-1", 6),
                                 _t1_row("orders-2", 3, role="reader", replica_lag_ms=12.5)])
    _scan("orders", 1)
    _scan("orders", 2, member="orders-2", role="reader", kind="reader", health="warning")
    t1, t2 = sr.member_status_rows(CLUSTER)
    assert sorted(r["member_instance_id"] for r in t1) == ["orders-1", "orders-2"], "the newest row per member"
    assert sorted(r["member_instance_id"] for r in t2) == ["orders", "orders-2"]
    out = sr.merge_member_status(CLUSTER, t1, t2)
    by = {m["instance_id"]: m for m in out}
    assert by["orders-2"]["replica_lag_ms"] == 12.5 and by["orders-2"]["health"] == "warning"
    assert by["orders-3"]["tier1_state"] == "none"


def test_merge_member_status_uses_current_roles_and_derives_states():
    record = {"instance_id": "orders", "tier1_frequency": "5min", "tier1_enabled": True,
              "cluster_member_roles": [
                  {"instance_id": "db-b", "is_writer": False, "endpoint": "b.example", "availability_zone": "us-east-1b"},
                  {"instance_id": "db-a", "is_writer": True, "endpoint": "a.example", "availability_zone": "us-east-1a"},
                  {"instance_id": "db-c", "is_writer": False, "endpoint": "c.example"}]}
    t1 = [{"member_instance_id": "db-a", "member_role": "reader", "scan_time": _ago(minutes=2), "replica_lag_ms": None},
          {"member_instance_id": "db-b", "member_role": "reader", "scan_time": _ago(minutes=40), "replica_lag_ms": "12.5"}]
    t2 = [{"member_instance_id": "db-a", "scan_time": _ago(hours=3), "health_score": "warning", "scan_kind": "full"},
          {"member_instance_id": "db-b", "scan_time": _ago(hours=3), "health_score": "bogus", "scan_kind": "reader"}]
    out = sr.merge_member_status(record, t1, t2, NOW)
    assert [m["instance_id"] for m in out] == ["db-a", "db-b", "db-c"], "writer first"
    a, b, c = out
    assert a["role"] == "writer", "the record's current label, not the row's (pre-failover) role"
    assert a["tier1_state"] == "fresh" and a["health"] == "warning" and a["last_scan_kind"] == "full"
    assert b["tier1_state"] == "stale" and b["replica_lag_ms"] == 12.5 and b["health"] == "unknown"
    assert c["tier1_state"] == "none" and c["last_tier1_at"] is None and c["health"] == "unknown"
    paused = sr.merge_member_status(dict(record, tier1_enabled=False), t1, t2, NOW)
    assert all(m["tier1_state"] == "paused" for m in paused)


def test_a_standalone_record_is_its_own_member():
    (m,) = sr.merge_member_status({"instance_id": "x", "endpoint": "inv.example",
                                   "tags": {"arn": "arn:aws:rds:us-east-1:1:db:inventory-replica",
                                            "replica_of": "inventory"}}, [], [], NOW)
    assert m["instance_id"] == "inventory-replica" and m["role"] == "reader" and m["endpoint"] == "inv.example"
    (m,) = sr.merge_member_status({"instance_id": "x", "arn": "arn:aws:rds:us-east-1:1:db:inventory"}, [], [], NOW)
    assert m["instance_id"] == "inventory" and m["role"] == "writer"
    assert sr.member_ids({"instance_id": "x", "arn": "arn:aws:rds:us-east-1:1:db:inventory"}) == ["inventory", "x"]


if __name__ == "__main__":
    import traceback
    fails = 0
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            fn()
            print(f"  ok   {name}")
        except Exception:  # noqa: BLE001
            fails += 1
            print(f"  FAIL {name}")
            traceback.print_exc()
    print(f"pass={len(tests) - fails} fail={fails}")
    sys.exit(1 if fails else 0)
