# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The cluster members API and the member selector on the record routes.

``GET /api/database-members/{id}`` is the members table: the record's members (who is
writer NOW), each with its newest Tier 1 row and newest scan from the store, plus the
cluster / reader / instance endpoints. ``GET /api/scan-metrics/{id}?member=`` and
``GET /api/scan-results?instance_id=&member=&kind=`` narrow the record views to a
member; the views that mean "the database's deep scan" read only full scans (M1).

The handlers are extracted from backend/app.py by source and run with the REAL
``utils.scan_records`` and ``tools.aws.metrics_store`` over an in-memory DynamoDB
(tests/fake_metrics_store), a fake registry and fake read gates. Hermetic, no AWS, no FastAPI.

Run:  python3 backend/tests/test_members_endpoint.py
"""
import os
import pathlib
import re
import sys
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import scan_records  # noqa: E402
from tools.aws import host_routes  # noqa: E402
sys.path.insert(0, os.path.dirname(__file__))
import fake_metrics_store  # noqa: E402
from tools.aws import metrics_store as ms  # noqa: E402

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


def _extract(name: str) -> str:
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^(?:@|def |class |[A-Z_]+ = )|\Z)", APP_SRC, re.S | re.M)
    assert m, f"{name} not found in app.py"
    return m.group(0)


class FakeHTTPException(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


def ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


CLUSTER = {"instance_id": "db-orders", "display_name": "orders", "status": "configured",
           "member_space_ids": {"space-a"}, "endpoint": "orders.cluster-x.example",
           "reader_endpoint": "orders.cluster-ro-x.example", "tier1_enabled": True, "tier1_frequency": "5min",
           "writer_instance_id": "db-b", "members_changed_at": "2030-01-01T00:00:00+00:00",
           "network_method": "peering", "network_route_mode": "vpc",
           "cluster_member_roles": [
               {"instance_id": "db-a", "is_writer": False, "endpoint": "db-a.example", "availability_zone": "us-east-1a",
                "instance_class": "db.r6g.large"},
               {"instance_id": "db-b", "is_writer": True, "endpoint": "db-b.example", "availability_zone": "us-east-1b",
                "instance_class": "db.r6g.large"}]}
SINGLE = {"instance_id": "db-inv", "display_name": "inventory", "status": "configured",
          "member_space_ids": {"space-a"}, "endpoint": "inv.example",
          "arn": "arn:aws:rds:us-east-1:111111111111:db:inventory"}


def build(viewer_ok=True, configured=True, records=(CLUSTER, SINGLE), manager=True):
    """The handlers over the REAL store code and a fresh in-memory DynamoDB."""
    store = fake_metrics_store.install()
    rows = {r["instance_id"]: dict(r) for r in records}
    ns = {
        "HTTPException": FakeHTTPException, "Request": object, "scan_records": scan_records,
        "db_registry": types.SimpleNamespace(get_instance=lambda iid: dict(rows[iid]) if iid in rows else None,
                                             list_instances=lambda: [dict(r) for r in rows.values()]),
        "_can_view_instance": lambda auth, iid: viewer_ok,
        "_can_manage_instance": lambda auth, iid: manager,
        "_can_view_space": lambda auth, sid: viewer_ok,
        "_instance_in_space": lambda d, sid: sid in (d.get("member_space_ids") or ()),
        "_store_is_configured": lambda: configured,
        "_STORE_NOT_CONFIGURED": "The metrics store is not configured",
        "_store_call": lambda fn: fn(),
        "_store_call_soft": lambda fn, default: (fn(), None),
        "logger": types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None,
                                        error=lambda *a, **k: None),
        "report_storage": types.SimpleNamespace(get_report=lambda key: None),
        "_redact_identifiers": lambda s: s,
    }
    for fn in ("_record_or_404", "get_database_members", "list_scan_results", "get_metrics_history",
               "get_fleet_metrics_api", "get_latest_scan_result", "_compare_report_from_row",
               "_load_compare_scan", "compare_reports"):
        exec(_extract(fn), ns)  # noqa: S102 - pinning shipped source
    return ns, store


def t1(iid, member, role, **kw):
    """One Tier 1 row; ``minutes``/``hours`` ago."""
    when = datetime.now(timezone.utc) - timedelta(minutes=kw.pop("minutes", 0), hours=kw.pop("hours", 0))
    return dict({"scan_time": when.isoformat(), "member_instance_id": member, "member_role": role}, **kw)


def t2(iid, member, role, kind, health="ok", hours=1, **kw):
    when = datetime.now(timezone.utc) - timedelta(hours=hours)
    return ms.put_tier2_scan(iid, dict({"scan_time": when.isoformat(), "member_instance_id": member,
                                        "member_role": role, "scan_kind": kind, "health_score": health,
                                        "findings": []}, **kw), {})


def pks(store):
    return {q["pk"] for t in (store.t1, store.t2) for q in t.queries}


REQ = types.SimpleNamespace(headers={"Authorization": "Bearer x"})


def status(fn, *a, **k):
    try:
        fn(*a, **k)
    except FakeHTTPException as e:
        return e.status_code, e.detail
    return 200, ""




print("== /members: the gate and unknown ids ==")
ns, store = build(viewer_ok=False)
check(status(ns["get_database_members"], "db-orders", REQ)[0] == 403, "a non-member viewer gets 403")
ns, store = build()
check(status(ns["get_database_members"], "db-missing", REQ)[0] == 404, "an unknown database is 404")

print("== /members: merge of the store rows ==")
ns, store = build()
# db-a's newest row was taken while it was still the writer (before the failover).
ms.put_tier1_rows("db-orders", [t1("db-orders", "db-a", "writer", minutes=2),
                                t1("db-orders", "db-b", "reader", hours=2, replica_lag_ms=15)])
t2("db-orders", "db-b", "writer", "full", health="warning", hours=3)
out = ns["get_database_members"]("db-orders", REQ)
by = {m["instance_id"]: m for m in out["members"]}
check(out["is_cluster"] and [m["instance_id"] for m in out["members"]] == ["db-b", "db-a"], "writer first")
check(by["db-b"]["role"] == "writer" and by["db-a"]["role"] == "reader",
      "roles are the record's current labels (after the failover), not the rows'")
check(by["db-a"]["tier1_state"] == "fresh" and by["db-b"]["tier1_state"] == "stale", "fresh / stale per member")
check(by["db-b"]["replica_lag_ms"] == 15.0 and by["db-b"]["health"] == "warning" and by["db-a"]["health"] == "unknown",
      "lag and health per member")
check(by["db-a"]["availability_zone"] == "us-east-1a" and by["db-b"]["endpoint"] == "db-b.example", "AZ and endpoint")
check(out["endpoints"] == {"cluster": "orders.cluster-x.example", "reader": "orders.cluster-ro-x.example",
                           "instance": {"db-b": "db-b.example", "db-a": "db-a.example"}}, "the endpoints block")
check(out["writer_instance_id"] == "db-b" and out["members_changed_at"].startswith("2030"), "writer id and last change")
check(out["store_configured"] is True, "store_configured")
check(pks(store) == {"db-orders"} and store.t1.queries and store.t2.queries,
      "every read is a keyed Query on this database's own partition")

print("== /members: a Lattice cluster shows readers as skipped ==")
ns, store = build(records=(dict(CLUSTER, network_method="lattice"),))
out = ns["get_database_members"]("db-orders", REQ)
by = {m["instance_id"]: m for m in out["members"]}
check(by["db-a"]["tier1_state"] == "skipped" and by["db-a"]["skip_reason"] == host_routes.LATTICE_SKIP,
      "the reader is skipped with the reason")
check(by["db-b"].get("tier1_state") != "skipped", "the writer is not")
check(out["reader_skip_reason"] == host_routes.LATTICE_SKIP, "the reader endpoint carries the same reason (pickers)")
ns, store = build()
check(ns["get_database_members"]("db-orders", REQ)["reader_skip_reason"] == "",
      "a VPC-wide route reaches the reader endpoint")

print("== /members: every viewer gets the full skip reason, the host_routes list stays out ==")
HOST_CLUSTER = dict(CLUSTER, network_route_mode="host", db_vpc_cidr="10.0.0.0/16",
                    host_routes=[{"ip": "10.0.1.5", "host": "db-b.example"}])
HOST_DNS = {"db-a.example": ["10.0.1.6"], "db-b.example": ["10.0.1.5"]}


def strings(obj):
    """Every string anywhere in a JSON-shaped value: keys and values, at any depth."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from strings(k)
            yield from strings(v)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            yield from strings(v)


_real_resolve = host_routes.resolve
host_routes.resolve = lambda h: list(HOST_DNS.get(h, []))
try:
    ns, store = build(records=(HOST_CLUSTER,), manager=False)
    ms.put_tier1_rows("db-orders", [t1("db-orders", "db-a", "reader", minutes=2, replica_lag_ms=12)])
    op = ns["get_database_members"]("db-orders", REQ)
    by = {m["instance_id"]: m for m in op["members"]}
    check(by["db-a"]["tier1_state"] == "skipped"
          and by["db-a"]["skip_reason"] == "skipped: not routed (10.0.1.6 has no owned host route)",
          "an operator sees the full skip reason, the member's address included")
    check("host_routes" not in set(strings(op)) and "10.0.1.5" not in set(strings(op)),
          "the host_routes list (the owned /32s) is not in the response")
    ns, store = build(records=(HOST_CLUSTER,), manager=True)
    mg = ns["get_database_members"]("db-orders", REQ)
    check([m.get("skip_reason") for m in mg["members"]] == [m.get("skip_reason") for m in op["members"]],
          "a manager sees the same reason")
    host_routes.resolve = lambda h: [] if h == "db-a.example" else list(HOST_DNS.get(h, []))
    ns, store = build(records=(HOST_CLUSTER,), manager=False)
    by = {m["instance_id"]: m for m in ns["get_database_members"]("db-orders", REQ)["members"]}
    check(by["db-a"]["skip_reason"] == host_routes.UNRESOLVABLE_SKIP
          and "not routed" not in by["db-a"]["skip_reason"],
          "a member whose name does not resolve is told so, not 'not routed'")
    host_routes.resolve = lambda h: list(HOST_DNS.get(h, []))
    ns, store = build(records=(dict(CLUSTER, network_method="lattice"),), manager=False)
    by = {m["instance_id"]: m for m in ns["get_database_members"]("db-orders", REQ)["members"]}
    check(by["db-a"]["skip_reason"] == host_routes.LATTICE_SKIP, "the Lattice reason is unchanged")
finally:
    host_routes.resolve = _real_resolve

print("== /members: the last reader leg's outcome per reader ==")
ns, store = build(records=(dict(CLUSTER, reader_scan_outcomes={
    "db-a": {"outcome": "not reached (job ended)", "at": "2030-01-01T00:00:00+00:00", "job_id": "job-456"}}),))
by = {m["instance_id"]: m for m in ns["get_database_members"]("db-orders", REQ)["members"]}
check(by["db-a"]["reader_scan_outcome"] == "not reached (job ended)"
      and by["db-a"]["reader_scan_at"].startswith("2030") and by["db-a"]["reader_scan_job_id"] == "job-456",
      "a reader carries its outcome, when, and which job wrote it")
check("reader_scan_outcome" not in by["db-b"], "a member with none carries nothing")
CM = (pathlib.Path(__file__).resolve().parents[2] / "frontend/src/components/ClusterMembers.jsx").read_text()
check("m.reader_scan_outcome" in CM and "Last reader scan" in CM, "the members view shows it")

print("== /members: the store unconfigured, and a single instance ==")
ns, store = build(configured=False)
out = ns["get_database_members"]("db-orders", REQ)
check(out["store_configured"] is False and not store.t1.queries and not store.t2.queries, "no store call")
check(len(out["members"]) == 2 and all(m["tier1_state"] is None and m["health"] is None for m in out["members"]),
      "members and endpoints still come back, with no status")
ns, store = build()
out = ns["get_database_members"]("db-inv", REQ)
check(out["is_cluster"] is False and [m["instance_id"] for m in out["members"]] == ["inventory"]
      and out["members"][0]["role"] == "writer", "a single instance is its own one member")
check(out["endpoints"]["instance"] == {"inventory": "inv.example"} and out["endpoints"]["cluster"] == "",
      "with its instance endpoint")

print("== member selector on /api/scan-metrics/{id} ==")
ns, store = build()
for bad in ("primary", "instance:", "instance:DB B", "instance:x' OR '1'='1"):
    code, detail = status(ns["get_metrics_history"], "db-orders", REQ, member=bad)
    check(code == 400 and "member must be" in detail, f"member={bad!r} is 400")
check(not store.t1.queries, "a refused selector reads nothing")


def history_ns():
    ns, store = build()
    ms.put_tier1_rows("db-orders", [t1("db-orders", "db-b", "writer", minutes=1),
                                    t1("db-orders", "db-a", "reader", minutes=1),
                                    t1("db-orders", "db-b", "reader", minutes=40),
                                    t1("db-orders", "db-a", "writer", minutes=40)])
    ms.put_tier1_rows("db-inv", [t1("db-inv", "inventory", "reader", minutes=1)])
    return ns, store


ns, store = history_ns()
out = ns["get_metrics_history"]("db-orders", REQ, member="instance:db-b")
check({r["member_instance_id"] for r in out["rows"]} == {"db-b"} and len(out["rows"]) == 2,
      "instance:db-b reads that member's rows, both roles")
check(out["member"] == "instance:db-b" and out["total"] == 2, "the effective selector and the total are echoed")
out = ns["get_metrics_history"]("db-orders", REQ)
check({r["member_role"] for r in out["rows"]} == {"writer"} and len(out["rows"]) == 2,
      "a cluster's default is its writer's history, continuous across the failover")
check(pks(store) == {"db-orders"}, "every read is on the database's own partition")
out = ns["get_metrics_history"]("db-inv", REQ, member="writer")
check(len(out["rows"]) == 1, "a single instance is not narrowed by a role")
check(status(ns["get_metrics_history"], "db-missing", REQ)[0] == 404, "an unknown database is 404")

print("== member selector and scan kind on /api/scan-results ==")


def results_ns():
    ns, store = build()
    t2("db-orders", "db-b", "writer", "full", hours=1)
    t2("db-orders", "db-a", "reader", "reader", hours=2)
    t2("db-orders", "db-b", "writer", "reader", hours=3)     # a reader-kind scan on the writer: not a deep scan
    t2("db-inv", "inventory", "writer", "full", hours=1)
    return ns, store


ns, store = results_ns()
out = ns["list_scan_results"](REQ, instance_id="db-orders", member="reader", kind="reader")
check([(r["member_instance_id"], r["scan_kind"]) for r in out["results"]] == [("db-a", "reader")],
      "member=reader&kind=reader reads the readers' reader scans")
check(status(ns["list_scan_results"], REQ, instance_id="db-orders", member="bogus")[0] == 400, "a bad member is 400")
for sel in ("writer", ""):
    out = ns["list_scan_results"](REQ, instance_id="db-orders", member=sel)
    check(out["kind"] == "full" and {r["scan_kind"] for r in out["results"]} == {"full"},
          f"member={sel!r} on a cluster without kind reads full scans only")
out = ns["list_scan_results"](REQ, instance_id="db-orders", member="writer", kind="reader")
check(out["kind"] == "reader" and [r["scan_kind"] for r in out["results"]] == ["reader"], "an explicit kind is kept")
out = ns["list_scan_results"](REQ, instance_id="db-orders", member="instance:db-b")
check(out["kind"] == "" and {r["scan_kind"] for r in out["results"]} == {"full", "reader"},
      "an instance selector without kind reads every kind")
check(status(ns["list_scan_results"], REQ, instance_id="db-orders", kind="sideways")[0] == 400, "a bad kind is 400")
check(all("data" not in r for r in out["results"]), "no section data in the list")

print("== M1: the space-wide list reads deep scans only and refuses a member ==")
ns, store = results_ns()
out = ns["list_scan_results"](REQ, space_id="space-a")
check(out["kind"] == "full" and {r["scan_kind"] for r in out["results"]} == {"full"}
      and {r["instance_id"] for r in out["results"]} == {"db-orders", "db-inv"}, "kind='full' across the space")
code, detail = status(ns["list_scan_results"], REQ, space_id="space-a", member="reader")
check(code == 400 and "instance_id" in detail, "member with space_id is 400")
ns, store = build(viewer_ok=False)
check(status(ns["list_scan_results"], REQ, space_id="space-a")[0] == 403, "the space list is gated")

print("== M1: the latest-result banner and the learning agent read full scans ==")
ns, store = build()
t2("db-orders", "db-b", "writer", "full", health="warning", hours=5)
t2("db-orders", "db-a", "reader", "reader", health="critical", hours=1)
out = ns["get_latest_scan_result"]("db-orders", REQ)
check(out["scan_kind"] == "full" and out["health_score"] == "warning", "the banner reads the latest FULL scan")
check('metrics_store.tier2_list([instance_id], 3650, 5, kind="full")' in APP_SRC,
      "the learning agent reads full scans only")
check("scan_records.clamp(hours, 1, scan_records.MAX_HOURS, 24)" in _extract("get_fleet_metrics_api"),
      "fleet hours is clamped to MAX_HOURS (#27)")

print("== fleet: the writer's row while fresh, else the newest of any role ==")


def fleet(rows_by_db):
    ns, store = build()
    for iid, rows in rows_by_db.items():
        ms.put_tier1_rows(iid, rows)
    return {r["instance_id"]: r for r in ns["get_fleet_metrics_api"]("space-a", REQ)["databases"]}


out = fleet({"db-orders": [t1("db-orders", "db-a", "reader", minutes=1), t1("db-orders", "db-b", "writer", minutes=3)]})
check(out["db-orders"]["member_role"] == "writer", "a writer row within the 5 min interval is the database's reading")
out = fleet({"db-orders": [t1("db-orders", "db-a", "reader", minutes=1), t1("db-orders", "db-b", "writer", hours=2)]})
check(out["db-orders"]["member_role"] == "reader" and out["db-orders"]["member_instance_id"] == "db-a",
      "a writer row older than the interval gives way to the newer reader row")
check(out["db-orders"]["display_name"] == "orders", "the card still gets its database name")
out = fleet({"db-orders": [t1("db-orders", "db-b", "writer", hours=2)]})
check(out["db-orders"]["member_role"] == "writer", "a stale writer with nothing newer stays (its age shows it)")
out = fleet({"db-inv": [t1("db-inv", "inventory", "reader", minutes=1)]})
check(out["db-inv"]["member_role"] == "reader", "a standalone replica's own reader row")
for f in ("frontend/src/components/Trends.jsx", "frontend/src/components/Dashboard.jsx"):
    src = (pathlib.Path(__file__).resolve().parents[2] / f).read_text(encoding="utf-8")
    check("scan_time" in src and ("sampleAge(p.scan_time)" in src or "timeAgo(latest.scan_time)" in src),
          f"{f.rsplit('/', 1)[-1]}: the fleet card shows the row's age")

print("== compare refuses two scans of different kinds, and gates each id before reading ==")
ns, store = build()
id1 = t2("db-orders", "db-b", "writer", "full", hours=5)
id2 = t2("db-orders", "db-a", "reader", "reader", hours=1)
code, detail = status(ns["compare_reports"], REQ, id1=id1, id2=id2)
check(code == 400 and detail == "Compare two deep scans of the same kind", "mixed kinds: 400")
id3 = t2("db-orders", "db-b", "writer", "full", hours=2)
out = ns["compare_reports"](REQ, id1=id3, id2=id1)
check(out["report1"]["id"] == id1 and out["report2"]["id"] == id3, "oldest first, by string scan ids")
check(status(ns["compare_reports"], REQ, id1="7", id2="8")[0] == 400, "a non-scan id is 400")
ns, store = build(viewer_ok=False)
check(status(ns["compare_reports"], REQ, id1=id1, id2=id3)[0] == 403 and not store.t2.queries,
      "a scan of a database the caller cannot see is refused before it is read")
check(status(ns["compare_reports"], REQ, key1="report.html", key2="x/report.html")[0] == 400,
      "a report key without its database segment is refused, not read unchecked")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
