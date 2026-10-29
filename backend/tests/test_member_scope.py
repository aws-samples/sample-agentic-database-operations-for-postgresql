# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The agent's member focus has data: the scope block names the member and its current
role, and the Trends and Findings tools read the focused member's rows.

``surfaces.scope`` is pure. ``db_tools.get_historical_metrics`` and
``db_tools.get_report_analysis`` run for real against the real ``metrics_store`` over an
in-memory DynamoDB and CloudWatch (tests/fake_metrics_store), a fake registry and the real space
scoping (``model_resolver.CURRENT_SPACE_ID``) and role focus
(``model_resolver.CURRENT_ROLE_FOCUS``). Hermetic, no AWS.

Run:  python3 backend/tests/test_member_scope.py
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_strands = types.ModuleType("strands")
_strands.tool = lambda f: f
sys.modules.setdefault("strands", _strands)
_fake_pg = types.ModuleType("tools.aws.pg_client")
_fake_pg.execute_query = lambda *a, **k: []
_fake_pg.test_connection = lambda *a, **k: {"ok": True}
_fake_pg.lookup_record_database_name = lambda *a, **k: ""
_fake_pg.SECRET_NOT_IN_SPACE_MESSAGE = "not available in the current space"
sys.modules["tools.aws.pg_client"] = _fake_pg
SPACE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CLUSTER = {"instance_id": "db-orders", "display_name": "orders", "status": "configured", "engine": "aurora-postgresql",
           "member_space_ids": {SPACE}, "endpoint": "orders.cluster-x.example",
           "reader_endpoint": "orders.cluster-ro-x.example",
           "cluster_member_roles": [{"instance_id": "db-a", "is_writer": False, "endpoint": "a.example"},
                                    {"instance_id": "db-b", "is_writer": True, "endpoint": "b.example"},
                                    {"instance_id": "db-c", "is_writer": False, "endpoint": "c.example"}]}
OTHER = dict(CLUSTER, instance_id="db-other", member_space_ids={"bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"})
_fake_ddb = types.ModuleType("tools.aws.dynamodb_client")
_RECORDS = {"db-orders": CLUSTER, "db-other": OTHER}
_fake_ddb.db_registry = types.SimpleNamespace(
    get_instance=lambda iid: dict(_RECORDS[iid]) if iid in _RECORDS else None,
    list_instances=lambda *a, **k: [dict(r) for r in _RECORDS.values()])
sys.modules["tools.aws.dynamodb_client"] = _fake_ddb
sys.path.insert(0, os.path.dirname(__file__))
import fake_metrics_store  # noqa: E402
from tools.aws import metrics_store as ms  # noqa: E402
import model_resolver as mr  # noqa: E402
from surfaces.scope import chat_scope_block, role_focus_block, scope_entry  # noqa: E402
from tools import db_tools as dbt  # noqa: E402

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def with_focus(focus, fn, *a, **k):
    t1, t2 = mr.CURRENT_ROLE_FOCUS.set(focus), mr.CURRENT_SPACE_ID.set(SPACE)
    try:
        return fn(*a, **k)
    finally:
        mr.CURRENT_ROLE_FOCUS.reset(t1)
        mr.CURRENT_SPACE_ID.reset(t2)


print("== the scope block names members and the focused member's current role ==")
line = scope_entry(CLUSTER, "db-1")
check("members=writer:db-b,reader:db-a,reader:db-c" in line, f"scope_entry lists members, writer first ({line})")
check("members=" not in scope_entry({"instance_id": "x", "endpoint": "e"}, "db-1"), "no members field for an instance")
b = role_focus_block("instance:db-b", CLUSTER)
check('"db-b", currently the writer' in b and "writer symptoms" in b, "an instance focus on the writer says so")
r = role_focus_block("instance:db-c", CLUSTER)
check('"db-c", currently the reader' in r and "reader symptoms" in r, "an instance focus on a reader says so")
u = role_focus_block("instance:db-zzz", CLUSTER)
check("not a current member" in u and "analyse the cluster endpoint and say so" in u, "an unknown id says so")
for focus in ("writer", "reader", "instance:db-c"):
    check("Performance Insights follow this focus" in role_focus_block(focus, CLUSTER),
          f"{focus}: states that history, scans and PI follow the focus")
block = chat_scope_block([CLUSTER], "instance:db-b")
check('currently the writer' in block, "chat_scope_block passes the one record in scope")
block2 = chat_scope_block([CLUSTER, dict(CLUSTER, instance_id="db-2")], "instance:db-b")
check('currently the' not in block2, "with two records no role is claimed")

print("== without a focus, a cluster scope says how a member is chosen ==")
nofocus = chat_scope_block([CLUSTER], "")
check("[MEMBERS]" in nofocus and "Endpoint menu" in nofocus and "writer" in nofocus,
      "a cluster with no focus carries the members note (cluster endpoint is the writer, Endpoint menu chooses)")
check("[ROLE FOCUS]" not in nofocus, "the members note replaces, never joins, a focus block")
check("[MEMBERS]" not in chat_scope_block([CLUSTER], "reader"), "a focus block takes the note's place")
check("[MEMBERS]" not in chat_scope_block([{"instance_id": "x", "endpoint": "e"}], ""),
      "a single instance has no members and no note")
check("[MEMBERS]" in chat_scope_block([{"instance_id": "x", "endpoint": "e"}, CLUSTER], ""),
      "one cluster among several databases is enough for the note")

from datetime import datetime, timedelta, timezone  # noqa: E402
_now = datetime.now(timezone.utc).replace(second=0, microsecond=0)


def t1(member, role, minutes_ago, **m):
    return dict({"scan_time": (_now - timedelta(minutes=minutes_ago)).isoformat(),
                 "member_instance_id": member, "member_role": role}, **m)


def t1_count():
    return len(FAKE.t1.queries)


print("== get_historical_metrics follows the focus ==")
FAKE = fake_metrics_store.install()
ms.put_tier1_rows("db-orders", [
    t1("db-b", "writer", 5, cache_hit_ratio=99), t1("db-b", "writer", 10, cache_hit_ratio=98),
    t1("db-c", "reader", 5, replica_lag_ms=40, total_connections=9),
    t1("db-a", "reader", 6, replica_lag_ms=5, total_connections=2),
    t1("db-c", "reader", 15, replica_lag_ms=10, total_connections=3),
    t1("db-a", "reader", 16, replica_lag_ms=5, total_connections=2)])
ms.put_tier1_rows("db-other", [t1("db-x", "writer", 5, cache_hit_ratio=1)])
out = with_focus("", dbt.get_historical_metrics, "db-orders")
check(out["member_selector"] == "" and {r["member_instance_id"] for r in out["metrics"]} == {"db-b"},
      "no focus: the writer's rows")
check(out["source"] == "samples" and out["latest"]["cache_hit_ratio"] == 99, "a short window reads the samples")
check(all(c in out["latest"] for c in ("member_instance_id", "member_role")), "rows carry the member and role")
check(all("instance_id" not in r and "id" not in r for r in out["metrics"]),
      "rows carry no registry id or item key (the model gets the alias)")
out = with_focus("reader", dbt.get_historical_metrics, "db-orders")
check(set(out.get("by_member") or {}) == {"db-a", "db-c"}, "reader focus: summarised by_member")
check(any("db-c: Replica lag: 10.0ms → 40.0ms" in x for x in out["trend_summary"]), "per-member trend lines")
out = with_focus("instance:db-c", dbt.get_historical_metrics, "db-orders")
check({r["member_instance_id"] for r in out["metrics"]} == {"db-c"}, "instance focus: that member's rows only")
out = with_focus("instance:db-c", dbt.get_historical_metrics, "db-orders", member="writer")
check({r["member_role"] for r in out["metrics"]} == {"writer"}, "an explicit member argument wins")
n = t1_count()
bad = with_focus("", dbt.get_historical_metrics, "db-orders", member="instance:DB C")
check(bad.get("error", "").startswith("member must be") and t1_count() == n, "a bad member is an error dict, no read")
n = t1_count()
out = with_focus("", dbt.get_historical_metrics, "db-other")
check(out.get("error") and not out["metrics"] and t1_count() == n,
      "another space's database is refused before any read (fail closed)")

print("== get_historical_metrics: long windows read the CloudWatch series ==")
ms.publish_tier1_metrics("db-orders", [t1("db-b", "writer", 60 * 24 * 20, cache_hit_ratio=90.0),
                                       t1("db-b", "writer", 5, cache_hit_ratio=99.0)])
out = with_focus("", dbt.get_historical_metrics, "db-orders", hours=24 * 30)
check(out["source"] == "series" and out["hours"] == 720, "a 30-day window reads the series")
check(len(out["metrics"]) == 2 and out["latest"]["cache_hit_ratio"] == 99.0, "newest bucket first")
check(all(r.get("member_instance_id") != "db-orders" for r in out["metrics"]),
      "a role series is not labelled with the registry id")
q = FAKE.cw.get_calls[-1]["queries"]
check(all({"Name": "Role", "Value": "writer"} in x["MetricStat"]["Metric"]["Dimensions"]
          and {"Name": "InstanceId", "Value": "db-orders"} in x["MetricStat"]["Metric"]["Dimensions"] for x in q),
      "the series query is built server-side from the scoped id (Role=writer)")
with_focus("instance:db-c", dbt.get_historical_metrics, "db-orders", hours=24 * 30)
q = FAKE.cw.get_calls[-1]["queries"]
check(all({"Name": "Member", "Value": "db-c"} in x["MetricStat"]["Metric"]["Dimensions"] for x in q),
      "an instance focus reads that member's series")
out = with_focus("", dbt.get_historical_metrics, "db-orders", hours=10 ** 9)
check(out["hours"] == 24 * 450, "the window is clamped to 15 months")


def scan(iid, minutes_ago, member, role, kind, health, findings=(), sections=None, **extra):
    return ms.put_tier2_scan(iid, dict({"scan_time": (_now - timedelta(minutes=minutes_ago)).isoformat(),
                                        "member_instance_id": member, "member_role": role, "scan_kind": kind,
                                        "health_score": health, "summary": kind, "findings": list(findings)},
                                       **extra), sections or {})


print("== get_report_analysis: writer view carries reader_scans ==")
FAKE = fake_metrics_store.install()
scan("db-orders", 60 * 30, "db-b", "writer", "full", "ok", cache_hit_ratio=97.0,
     sections={"configuration": {"settings": [{"name": "work_mem", "setting": "4MB"}]},
               "security": {"roles": [{"name": "app", "can_login": True}]},
               "overview": {"extensions": [{"name": "pg_stat_statements", "version": "1.10"}]}})
scan("db-orders", 10, "db-b", "writer", "full", "warning",
     [{"severity": "WARNING", "title": "bloat", "resolution": "vacuum"}], cache_hit_ratio=99.0,
     sections={"configuration": {"settings": [{"name": "work_mem", "setting": "16MB"}]},
               "security": {"roles": [{"name": "app", "can_login": True}, {"name": "etl", "can_login": True}]},
               "overview": {"extensions": [{"name": "pg_stat_statements", "version": "1.11"}]},
               "performance": {"top_queries": [{"query": "SELECT ? FROM t"}]}})
scan("db-orders", 20, "db-a", "reader", "reader", "critical",
     [{"severity": "INFO", "title": f"i{n}"} for n in range(4)]
     + [{"severity": "CRITICAL", "title": "lag"}, {"severity": "WARNING", "title": "conflicts"}],
     sections={"reader": {"replica_lag_ms": 40}})
scan("db-orders", 15, "db-c", "reader", "reader", "warning", sections={"reader": {"replica_lag_ms": 5}})
scan("db-other", 5, "db-x", "writer", "full", "critical")
out = with_focus("", dbt.get_report_analysis, "db-orders")
(rep,) = out["reports"]
check(rep["scan_kind"] == "full" and rep["analysis"]["health_score"] == "warning", "the writer's latest FULL scan")
check(rep["analysis"]["recommendations"] == ["vacuum"] and "sections" not in rep, "the summary only by default")
check(set(rep["sections_available"]) >= {"configuration", "performance"}, "it says which sections exist")
rs = {r["member_instance_id"]: r for r in rep["reader_scans"]}
check(set(rs) == {"db-a", "db-c"} and [f["title"] for f in rs["db-a"]["findings"]][:2] == ["lag", "conflicts"]
      and len(rs["db-a"]["findings"]) == 5, "reader_scans: latest per reader, top 5 findings by severity")
check(out["member_selector"] == "", "member_selector echoed")
out = with_focus("", dbt.get_report_analysis, "db-orders", sections="configuration,performance,bogus")
(rep,) = out["reports"]
check(set(rep["sections"]) == {"configuration", "performance"}, "the requested sections are added; unknown ones ignored")
out = with_focus("", dbt.get_report_analysis, "db-orders", sections="all")
check("reader" not in out["reports"][0]["sections"], "'all' is the writer's sections, not the reader section")
out = with_focus("", dbt.get_report_analysis, "db-orders", compare_to="previous")
ch = out["reports"][0]["changes"]
check(ch["health_score"] == ["ok", "warning"] and ch["metrics"]["cache_hit_ratio"] == {"from": 97.0, "to": 99.0},
      "compare_to=previous: health and headline metrics moved")
check(ch["configuration"] == {"work_mem": {"from": "4MB", "to": "16MB"}}, "a changed setting")
check(ch["login_roles"] == {"added": ["etl"], "removed": []} and ch["findings"]["new"] == ["bloat"],
      "a new login role and a new finding")
check(ch["extensions"] == {"pg_stat_statements": {"from": "1.10", "to": "1.11"}}, "an upgraded extension")
day = (_now - timedelta(minutes=60 * 30)).date().isoformat()
out = with_focus("", dbt.get_report_analysis, "db-orders", compare_to=day)
check(out["reports"][0]["changes"].get("from", "").startswith(day), "compare_to=<date>: the newest scan on or before it")
out = with_focus("", dbt.get_report_analysis, "db-orders", compare_to="soon")
check("error" in out["reports"][0]["changes"], "a junk compare_to is an error in changes")

print("== get_report_analysis: reader / instance focus reads reader scans ==")
out = with_focus("instance:db-c", dbt.get_report_analysis, "db-orders")
(rep,) = out["reports"]
check(rep["member_instance_id"] == "db-c" and rep["writer_health_score"] == "warning" and rep["scan_kind"] == "reader",
      "the member's reader scan, with the writer's health for context")
out = with_focus("reader", dbt.get_report_analysis, "db-orders", sections="all")
check({r["member_instance_id"] for r in out["reports"]} == {"db-a", "db-c"}
      and all(set(r["sections"]) == {"reader"} for r in out["reports"]), "reader focus: each reader, its reader section")
out = with_focus("instance:db-b", dbt.get_report_analysis, "db-orders")
check(out["reports"][0]["scan_kind"] == "full", "an instance focus on the writer reads the full scan")
bad = with_focus("", dbt.get_report_analysis, "db-orders", member="nope")
check(bad.get("error", "").startswith("member must be"), "a bad member is an error dict")
out = with_focus("", dbt.get_report_analysis, "db-other")
check(out.get("error") == "No monitored databases found", "another space's database is refused (fail closed)")

print("== assess_growth covers the whole requested window, not the display page (#15) ==")
FAKE = fake_metrics_store.install()
# 110 recent rows (the display page is the newest 100: ~9 h of flat size) and one row
# near the start of the 24 h window at half the size.
rows = [t1("db-b", "writer", 5 * i, database_size_mb=2000.0 - i * 0.1, dead_tuple_pct=5.0) for i in range(110)]
rows.append(t1("db-b", "writer", 60 * 24 - 5, database_size_mb=1000.0, dead_tuple_pct=4.0))
ms.put_tier1_rows("db-orders", rows)
g = with_focus("", dbt.assess_growth, "db-orders", hours=24)
check(g["is_growing"] is True and g["size_change_pct"] == 100.0,
      f"growth is judged across the whole window (2000 vs 1000 = +100%), not the flat page ({g})")
check(g["observed_window_hours"] and 23.5 <= g["observed_window_hours"] <= 24.5
      and g["covers_requested_window"] is True, f"the observed span ~24 h covers the ask ({g})")

print("== a short history is reported as not covering the window ==")
FAKE = fake_metrics_store.install()
ms.put_tier1_rows("db-orders", [t1("db-b", "writer", 0, database_size_mb=2000.0, dead_tuple_pct=5.0),
                                t1("db-b", "writer", 60, database_size_mb=1980.0, dead_tuple_pct=5.0)])
g = with_focus("", dbt.assess_growth, "db-orders", hours=24)
check(g["covers_requested_window"] is False and g["observed_window_hours"] and g["observed_window_hours"] <= 2,
      f"a 1 h history does not cover a 24 h ask ({g.get('observed_window_hours')})")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
