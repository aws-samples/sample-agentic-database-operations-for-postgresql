# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Aurora Global Database members are never candidates; they are listed as not supported.

A Global Database's writer moves between Regions on a switchover or failover, which the
network path and the secret-host rule do not follow yet. So discovery calls
describe_global_clusters in each space Region (paginated) and marks every member cluster
with `global_cluster_id` and `global_cluster_role` (primary for the writer cluster, else
secondary). Such a database is not proposed to the space, not even as a disabled row:
discovery returns it separately (`not_supported`, kept on the space) with the reason "Not
supported yet: part of an Aurora Global Database", and the console lists it in a
collapsed "Not supported" section with no action. The server still refuses to onboard
one (test-credentials and configure both go through _verify_instance_onboardable).

Classification fails closed: when describe_global_clusters fails in a Region, an Aurora
cluster discovered there in that run is not onboardable ("Could not check for Aurora
Global Database membership; retry discovery", in the same section), and a database the
space already had keeps its state; the failure is logged and earlier marks stay.

Hermetic: boto3 stubbed, fake RDS pages, functions lifted from app.py by source.

Run:  python3 backend/tests/test_global_cluster_block.py   (or via pytest)
"""
import os
import pathlib
import re
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.modules.setdefault("boto3", types.ModuleType("boto3"))

APP_SRC = (pathlib.Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
REASON = "Not supported yet: part of an Aurora Global Database"
ACCT = "111111111111"


def _extract(name):
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^(?:@|def |class )|\Z)", APP_SRC, re.S | re.M)
    assert m, f"{name} not found in app.py"
    return m.group(0)


class HTTPException(Exception):
    def __init__(self, status_code=None, detail=None):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


def _arn(name):
    return f"arn:aws:rds:us-east-1:{ACCT}:cluster:{name}"


def _cluster(name):
    return {"DBClusterIdentifier": name, "Engine": "aurora-postgresql", "DBClusterArn": _arn(name),
            "Endpoint": f"{name}.cluster-x.us-east-1.rds.amazonaws.com", "Port": 5432,
            "DBClusterMembers": [], "VpcSecurityGroups": [], "DBSubnetGroup": ""}


class _Pager:
    def __init__(self, pages, fail=False):
        self._pages, self._fail = pages, fail

    def paginate(self, **kw):
        if self._fail:
            raise Exception("AccessDenied: rds:DescribeGlobalClusters")
        return iter(self._pages)


class _FakeRds:
    def __init__(self, fail_global=False):
        self.fail_global = fail_global
        self.paginated = []

    def get_paginator(self, name):
        self.paginated.append(name)
        if name == "describe_global_clusters":
            # Two pages: the paginator, not one call, is what finds the second.
            return _Pager([
                {"GlobalClusters": [{"GlobalClusterIdentifier": "gdb-1", "GlobalClusterMembers": [
                    {"DBClusterArn": _arn("gwriter"), "IsWriter": True},
                    {"DBClusterArn": "arn:aws:rds:eu-west-1:111111111111:cluster:geu", "IsWriter": False}]}]},
                {"GlobalClusters": [{"GlobalClusterIdentifier": "gdb-2", "GlobalClusterMembers": [
                    {"DBClusterArn": _arn("gsecondary"), "IsWriter": False}]}]},
            ], fail=self.fail_global)
        if name == "describe_db_clusters":
            return _Pager([{"DBClusters": [_cluster("gwriter"), _cluster("plain")]},
                           {"DBClusters": [_cluster("gsecondary")]}])
        return _Pager([{"DBInstances": []}])


def _discover(fail_global=False):
    from tools.aws import discovery_client as dc
    rds = _FakeRds(fail_global)
    dc.boto3 = types.SimpleNamespace(Session=lambda region_name=None, **kw: types.SimpleNamespace(
        client=lambda name, region_name=None, **k: rds))
    d = dc.DbDiscovery()
    d._sts_client = types.SimpleNamespace(get_caller_identity=lambda: {"Account": ACCT})
    log = []
    found = d.discover_instances_in_account(ACCT, ["us-east-1"], scan_log=log)
    return {i["identifier"]: i for i in found}, log, rds


NS = {"HTTPException": HTTPException,
      "db_registry": None,
      "logger": types.SimpleNamespace(warning=lambda *a, **k: None),
      "get_user_id": lambda auth: "user-1",
      "datetime": __import__("datetime").datetime, "timezone": __import__("datetime").timezone,
      "CONFIGURE_STALE_AFTER_S": 900,
      "_CONFIGURE_STALE_MESSAGE": "Configuration did not finish — the service may have restarted. Onboard it again.",
      "_instance_member_spaces": lambda inst: set((inst or {}).get("member_space_ids") or []),
      "_instance_candidate_spaces": lambda inst: set((inst or {}).get("candidate_space_ids") or []),
      "teardown_stalled": __import__("tools.aws.teardown_claim", fromlist=["x"]).teardown_stalled}
for _fn in ("_unsupported_reason", "_teardown_reason", "_discovery_rds_tags", "_verify_instance_onboardable",
            "_configure_is_stale", "_flag_stale_configuring"):
    exec(_extract(_fn), NS)  # noqa: S102 - pinning shipped source


def test_discovery_marks_every_member_across_pages():
    found, log, rds = _discover()
    assert "describe_global_clusters" in rds.paginated, "paginated, not a single call"
    assert found["gwriter"]["global_cluster_id"] == "gdb-1" and found["gwriter"]["global_cluster_role"] == "primary"
    assert found["gsecondary"]["global_cluster_id"] == "gdb-2", "the member on the second page is marked"
    assert found["gsecondary"]["global_cluster_role"] == "secondary"
    assert found["plain"]["global_cluster_id"] == "" and found["plain"]["global_cluster_role"] == ""


def test_discovery_tags_carry_the_marks():
    found, _, _ = _discover()
    tags = NS["_discovery_rds_tags"](found["gwriter"])
    assert tags["global_cluster_id"] == "gdb-1" and tags["global_cluster_role"] == "primary"
    assert NS["_discovery_rds_tags"](found["plain"])["global_cluster_id"] == "", "a non-member is cleared"


def test_a_failed_global_scan_is_logged_and_leaves_marks_alone():
    found, log, _ = _discover(fail_global=True)
    assert any("global cluster scan error" in line for line in log), log
    tags = NS["_discovery_rds_tags"](found["gwriter"])
    assert "global_cluster_id" not in tags, "nothing to overwrite earlier marks with"
    assert found["plain"]["identifier"] == "plain", "the rest of discovery carries on"


def test_onboarding_a_global_member_is_refused_with_the_reason():
    rec = {"instance_id": "db-g", "candidate_space_ids": {"sp-1"},
           "tags": {"global_cluster_id": "gdb-1", "global_cluster_role": "primary"}}
    NS["db_registry"] = types.SimpleNamespace(get_instance=lambda iid: rec)
    try:
        NS["_verify_instance_onboardable"]("db-g", "sp-1", "Bearer x")
        raise AssertionError("expected a refusal")
    except HTTPException as e:
        assert e.status_code == 400 and e.detail == REASON, (e.status_code, e.detail)


def test_onboarding_a_plain_candidate_still_works():
    rec = {"instance_id": "db-p", "candidate_space_ids": {"sp-1"}, "tags": {"global_cluster_id": ""}}
    NS["db_registry"] = types.SimpleNamespace(get_instance=lambda iid: rec)
    assert NS["_verify_instance_onboardable"]("db-p", "sp-1", "Bearer x") is rec


def test_candidates_never_list_a_global_member():
    rows = [{"instance_id": "db-g", "display_name": "gwriter", "candidate_space_ids": {"sp-1"},
             "tags": {"global_cluster_id": "gdb-1", "raw_identifier": "gwriter"}},
            {"instance_id": "db-p", "display_name": "plain", "candidate_space_ids": {"sp-1"}, "tags": {}},
            # A removal whose process stopped: the console shows it as Delete failed with Retry.
            {"instance_id": "db-s", "display_name": "stalled", "candidate_space_ids": {"sp-1"}, "tags": {},
             "status": "deleting", "teardown_claimed_at": "2020-01-01T00:00:00+00:00"}]
    ns = dict(NS)
    ns.update({"Request": object,
               "_can_manage_space": lambda auth, sid: True,
               "owner_admin_of": lambda sp: "admin-1",
               "spaces_registry": types.SimpleNamespace(get_space=lambda sid: {"space_id": sid},
                                                        list_spaces=lambda: []),
               "db_registry": types.SimpleNamespace(list_instances=lambda: rows)})
    exec(re.sub(r"^@app\.get\([^\n]*\)\n", "", _extract("list_space_candidates"), flags=re.M), ns)  # noqa: S102
    out = {r["instance_id"]: r for r in ns["list_space_candidates"]("sp-1", types.SimpleNamespace(headers={}))}
    assert "db-g" not in out, "a Global Database member is not a candidate, not even a disabled row"
    assert out["db-s"]["teardown_stalled"] is True and out["db-p"]["teardown_stalled"] is False
    assert "unsupported_reason" not in out["db-p"]


# ── discovery returns the not-supported databases separately ───────────────────

class _Reg:
    """The instances registry discovery writes to."""

    def __init__(self, rows=()):
        self.rows = {r["instance_id"]: dict(r) for r in rows}
        self.created, self.candidates_added, self.candidates_removed, self.deleted = [], [], [], []

    def list_instances(self):
        return [dict(r) for r in self.rows.values()]

    def find_by_arn(self, arn):
        # The ARN index discovery checks before creating a record.
        return next((dict(r) for r in self.rows.values()
                     if (r.get("arn") or (r.get("tags") or {}).get("arn")) == arn), None)

    def get_instance(self, iid):
        return dict(self.rows[iid]) if iid in self.rows else None

    def create_instance(self, data):
        iid = f"db-new-{len(self.created)}"
        rec = dict(data, instance_id=iid, candidate_space_ids={data["space_id"]})
        self.rows[iid] = rec
        self.created.append(rec)
        return rec

    def update_instance(self, iid, fields):
        self.rows[iid].update(fields)

    def add_candidate(self, iid, sid):
        self.candidates_added.append(iid)
        self.rows[iid].setdefault("candidate_space_ids", set()).add(sid)

    def remove_candidate(self, iid, sid):
        self.candidates_removed.append(iid)
        self.rows[iid].get("candidate_space_ids", set()).discard(sid)

    def remove_member(self, iid, sid):
        self.rows[iid].get("member_space_ids", set()).discard(sid)

    def delete_instance(self, iid):
        self.deleted.append(iid)
        self.rows.pop(iid, None)


def _run_discovery(found, rows=()):
    reg, space_updates = _Reg(rows), []
    space = {"space_id": "sp-1", "regions": ["us-east-1"], "primary_account_id": ACCT}
    ns = dict(NS)
    ns.update({"Request": object, "os": os, "datetime": __import__("datetime").datetime,
               "Optional": __import__("typing").Optional,   # named in _run_discovery's signature
               "timezone": __import__("datetime").timezone,
               "_can_manage_space": lambda auth, sid: True,
               "_instance_in_space": lambda inst, sid: sid in set((inst or {}).get("member_space_ids") or []),
               "spaces_registry": types.SimpleNamespace(get_space=lambda sid: dict(space),
                                                        update_space=lambda sid, d: space_updates.append(d)),
               "governance_registry": types.SimpleNamespace(audit=lambda *a, **k: None),
               "db_registry": reg, "_redact_identifiers": lambda s: s,
               "db_discovery": types.SimpleNamespace(discover_all=lambda **kw: (
                   [dict(f) for f in found], [],
                   {(a, r): {"clusters_ok": True, "instances_ok": True}
                    for a in (kw.get("account_ids") or []) for r in (kw.get("regions") or [])})),
               "logger": types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None,
                                               error=lambda *a, **k: None)})
    for fn in ("_index_existing_dbs", "_index_one_db", "_match_discovered_db", "_rows_this_scan_no_longer_sees",
               "_discovery_unsupported_reason", "_cap_unsupported", "_stale_candidate_reset",
               # Per-account Region resolution used by _run_discovery (real helpers, pure).
               "_regions_for_account", "_regions_by_account"):
        exec(_extract(fn), ns)  # noqa: S102
    exec(re.search(r"^GLOBAL_UNCHECKED_REASON = .*$", APP_SRC, re.M).group(0), ns)  # noqa: S102
    exec(re.search(r"^UNSUPPORTED_PER_REGION_MAX = .*$", APP_SRC, re.M).group(0), ns)  # noqa: S102
    # The discovery body moved to _run_discovery (the route now only schedules it).
    exec(_extract("_run_discovery"), ns)  # noqa: S102
    out = ns["_run_discovery"]("sp-1", "admin-1")
    return out, reg, space_updates[-1]


def _found(name, checked=True, gid="", engine="aurora-postgresql"):
    return {"identifier": name, "region": "us-east-1", "account_id": ACCT, "engine": engine,
            "cluster_arn": _arn(name), "global_checked": checked, "global_cluster_id": gid,
            "global_cluster_role": "primary" if gid else ""}


UNCHECKED = "Could not check for Aurora Global Database membership; retry discovery"


def test_discovery_does_not_propose_a_global_member_and_lists_it_separately():
    out, reg, space = _run_discovery([_found("gwriter", gid="gdb-1"), _found("plain")])
    assert [r["display_name"] for r in reg.created] == ["plain"], "only the plain cluster becomes a candidate"
    assert out["not_supported"] == [{"name": "gwriter", "region": "us-east-1", "account": ACCT,
                                     "engine": "aurora-postgresql", "reason": REASON}]
    assert [d["name"] for d in out["databases"]] == ["plain"], "not among the discovered candidates"
    assert space["unsupported_databases"] == out["not_supported"], "kept on the space for the console"


def test_a_known_global_candidate_is_withdrawn():
    rows = [{"instance_id": "db-g", "display_name": "gwriter", "account_id": ACCT, "region": "us-east-1",
             "candidate_space_ids": {"sp-1"}, "tags": {"arn": _arn("gwriter")}}]
    out, reg, _ = _run_discovery([_found("gwriter", gid="gdb-1")], rows)
    assert reg.deleted == ["db-g"] and not reg.created, "a candidate only this space had is dropped"
    assert out["not_supported"][0]["reason"] == REASON


def test_a_failed_global_check_fails_closed_for_new_clusters():
    out, reg, _ = _run_discovery([_found("newc", checked=False), _found("maz", checked=False, engine="postgres")])
    assert [r["display_name"] for r in reg.created] == ["maz"], \
        "an Aurora cluster found in that Region is not onboardable this run (a Multi-AZ DB cluster cannot be a member)"
    assert out["not_supported"] == [{"name": "newc", "region": "us-east-1", "account": ACCT,
                                     "engine": "aurora-postgresql", "reason": UNCHECKED}]


def test_a_failed_global_check_keeps_known_candidates_as_they_are():
    rows = [{"instance_id": "db-k", "display_name": "known", "account_id": ACCT, "region": "us-east-1",
             "candidate_space_ids": {"sp-1"}, "tags": {"arn": _arn("known")}},
            {"instance_id": "db-o", "display_name": "other", "account_id": ACCT, "region": "us-east-1",
             "member_space_ids": {"sp-2"}, "status": "configured", "tags": {"arn": _arn("other")}}]
    out, reg, _ = _run_discovery([_found("known", checked=False), _found("other", checked=False)], rows)
    assert "db-k" in reg.rows and "sp-1" in reg.rows["db-k"]["candidate_space_ids"], "the known candidate stays"
    assert not reg.candidates_removed and not reg.deleted
    assert "db-o" not in reg.candidates_added, "a database this space did not have is not proposed unchecked"
    assert [d["reason"] for d in out["not_supported"]] == [UNCHECKED] and out["not_supported"][0]["name"] == "other"
    assert [d["name"] for d in out["databases"]] == ["known"]


def test_the_stored_list_is_capped_per_region_without_duplicates():
    many = [_found(f"c{i:02d}", checked=False) for i in range(30)]
    dup = _found("c29", gid="gdb-9")          # found again in the same run, now classified
    west = dict(_found("w1", gid="gdb-2"), region="us-west-2")
    out, _reg, space = _run_discovery(many + [dup, west])
    stored = space["unsupported_databases"]
    east = [d for d in stored if d["region"] == "us-east-1"]
    assert len(out["not_supported"]) == 32, "the discovery answer carries every one"
    assert len(east) == 20 and [d["region"] for d in stored].count("us-west-2") == 1, \
        f"at most 20 per Region on the space item ({len(east)})"
    assert [d["name"] for d in east][-1] == "c29" and "c00" not in [d["name"] for d in east], \
        "the newest are kept"
    assert [d["name"] for d in stored].count("c29") == 1 and \
        next(d for d in stored if d["name"] == "c29")["reason"] == REASON, \
        "a repeat replaces the earlier entry rather than adding a second"


def _space_routes(manages):
    """GET /api/spaces and GET /api/spaces/{id}, lifted from app.py, over one space that
    carries a not-supported list; `manages` is what _can_manage_space answers."""
    space = {"space_id": "sp-1", "name": "orders", "created_by": "admin-1",
             "unsupported_databases": [{"name": "gwriter", "region": "us-east-1", "account": ACCT,
                                        "engine": "aurora-postgresql", "reason": REASON}]}
    ns = {"HTTPException": HTTPException, "Request": object,
          "spaces_registry": types.SimpleNamespace(list_spaces=lambda: [dict(space)], get_space=lambda sid: dict(space)),
          "_accessible_space_ids": lambda auth: {"sp-1"}, "_can_view_space": lambda auth, sid: True,
          "_can_manage_space": lambda auth, sid: manages, "_owner_display": lambda s: "owner@example.com",
          "_flag_stalled_teardown": lambda out, record=None: out}
    exec(re.search(r"^SPACE_MANAGER_FIELDS = .*$", APP_SRC, re.M).group(0), ns)  # noqa: S102
    for fn in ("_space_for_caller", "list_spaces", "get_space"):
        exec(_extract(fn), ns)  # noqa: S102
    req = types.SimpleNamespace(headers={"Authorization": "Bearer x"})
    return ns["list_spaces"](req), ns["get_space"]("sp-1", req)


def test_only_a_manager_reads_the_not_supported_list():
    listed, one = _space_routes(manages=True)
    assert listed[0]["unsupported_databases"][0]["name"] == "gwriter" and one["unsupported_databases"], \
        "the owner or a super admin gets the list on both routes, and the space is in their console list"
    listed, one = _space_routes(manages=False)
    assert listed == [], "the console list skips a space the caller does not manage (operators cannot open the console)"
    assert "unsupported_databases" not in one and one["owner"] == "owner@example.com", \
        "the read route still returns the space (a viewer) but without the manager-only list"


def test_the_console_lists_not_supported_in_a_collapsed_section():
    jsx = (pathlib.Path(__file__).resolve().parents[2] / "admin-console/src/components/SpaceDetail.jsx").read_text()
    assert "discoveryResult?.not_supported || space.unsupported_databases" in jsx
    section = jsx.split("Not supported ({notSupported.length})", 1)
    assert len(section) == 2 and "<details" in section[0].rsplit("{notSupported.length > 0", 1)[1], \
        "a collapsed <details> section"
    body = section[1].split("</details>", 1)[0]
    assert "{d.name}" in body and "{d.region}" in body and "{d.reason}" in body, "name, region, reason"
    assert "<button" not in body, "no action"
    assert "unsupported_reason" not in jsx, "candidate rows no longer carry a disabled Global Database state"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  ok   {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {e!r}")
    print(f"pass={passed} fail={failed}")
    sys.exit(1 if failed else 0)
