# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for Aurora role labeling + observed data window (tools.aws.cloudwatch_client).

Three properties that keep role labels and reported durations honest:
  - `_resolve_instance_ids` returns writer-first role dicts ({"id","is_writer","role"});
  - a metric tool annotates each per-instance result with role/is_writer and exposes
    a top-level `primary_instance` (the writer) + `requested_hours` (not "hours");
  - `_data_window` reports the OBSERVED span + point_count (None when empty).

Hermetic: `strands` is stubbed (identity `@tool`) and the cross-account session
factory is faked in sys.modules — no live AWS, no strands install required.

Run:  python3 backend/tests/test_cloudwatch_client.py   (or via pytest)
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── Stub `strands` so the @tool decorator is a no-op (module isn't installed here
#    and we want to call the tool functions as plain callables). ──
_strands = types.ModuleType("strands")
_strands.tool = lambda f: f
sys.modules.setdefault("strands", _strands)


# ── Fake the cross-account session factory (lazily imported inside the tools). ──
class _FakeSession:
    def __init__(self, rds):
        self._rds = rds

    def client(self, name):
        return self._rds


_SF_STATE = {"rds": None}
_fake_sf = types.ModuleType("tools.aws.session_factory")
_fake_sf._STATE = _SF_STATE
_fake_sf.get_session = lambda **kwargs: _FakeSession(_SF_STATE["rds"])
_fake_sf.get_session_context = lambda session: {}
# No registry row: the handle is already the RDS identifier (resolve_target passes it through).
_fake_sf.resolve_target = lambda handle: {"record": {}, "rds_id": handle, "account_id": "", "region": ""}
sys.modules["tools.aws.session_factory"] = _fake_sf

from tools.aws import cloudwatch_client as cw  # noqa: E402


class _FakeRDS:
    """Minimal RDS stub for describe_db_clusters / describe_db_instances."""

    def __init__(self, members=None, instance_id=None,
                 cluster_error=False, instance_error=False):
        self._members = members
        self._instance_id = instance_id
        self._cluster_error = cluster_error
        self._instance_error = instance_error

    def describe_db_clusters(self, DBClusterIdentifier=None):
        if self._cluster_error or self._members is None:
            raise Exception("not a cluster")
        return {"DBClusters": [{"DBClusterMembers": self._members}]}

    def describe_db_instances(self, DBInstanceIdentifier=None):
        if self._instance_error or self._instance_id is None:
            raise Exception("not an instance")
        return {"DBInstances": [{"DBInstanceIdentifier": self._instance_id}]}


_POINTS = [
    {"timestamp": "2026-02-01T00:00:00+00:00", "value": 10.0},
    {"timestamp": "2026-02-01T00:30:00+00:00", "value": 20.0},
    {"timestamp": "2026-02-01T01:00:00+00:00", "value": 30.0},
]


# ── _resolve_instance_ids returns writer-first role dicts ───────────────────

def test_resolve_instance_ids_writer_first_role_dicts():
    # Input deliberately lists the reader FIRST — the resolver must sort writer first.
    _SF_STATE["rds"] = _FakeRDS(members=[
        {"DBInstanceIdentifier": "reader-1", "IsClusterWriter": False},
        {"DBInstanceIdentifier": "writer-1", "IsClusterWriter": True},
    ])
    members = cw._resolve_instance_ids("my-cluster")
    assert members[0] == {"id": "writer-1", "is_writer": True, "role": "writer"}, members[0]
    assert members[1] == {"id": "reader-1", "is_writer": False, "role": "reader"}, members[1]


def test_resolve_instance_ids_standalone_is_its_own_writer():
    # Not a cluster, but a resolvable instance → single writer dict (its own primary).
    _SF_STATE["rds"] = _FakeRDS(cluster_error=True, instance_id="solo-db")
    members = cw._resolve_instance_ids("solo-db")
    assert members == [{"id": "solo-db", "is_writer": True, "role": "writer"}]


def test_resolve_instance_ids_unresolvable_falls_back_to_writer():
    _SF_STATE["rds"] = _FakeRDS(cluster_error=True, instance_error=True)
    members = cw._resolve_instance_ids("mystery")
    assert members == [{"id": "mystery", "is_writer": True, "role": "writer"}]


# ── a metric tool annotates role/is_writer + primary_instance ───────────────

def test_metric_tool_annotates_role_and_primary_instance():
    members = [
        {"id": "writer-1", "is_writer": True, "role": "writer"},
        {"id": "reader-1", "is_writer": False, "role": "reader"},
    ]
    orig_resolve, orig_metrics = cw._resolve_instance_ids, cw._get_rds_metrics
    try:
        cw._resolve_instance_ids = lambda identifier: members
        cw._get_rds_metrics = lambda iid, metric, **kw: list(_POINTS)
        res = cw.get_cpu_utilization("my-cluster", hours=6)
    finally:
        cw._resolve_instance_ids, cw._get_rds_metrics = orig_resolve, orig_metrics

    # Multi-instance shape: top-level writer anchor.
    assert res["primary_instance"] == "writer-1", res.get("primary_instance")
    insts = res["instances"]
    assert insts[0]["role"] == "writer" and insts[0]["is_writer"] is True
    assert insts[1]["role"] == "reader" and insts[1]["is_writer"] is False
    # The requested lookback is named distinctly, not the misleading "hours".
    assert insts[0]["requested_hours"] == 6
    assert "hours" not in insts[0]
    # The observed window is attached per instance.
    assert insts[0]["data_window"]["point_count"] == 3
    assert insts[0]["data_window"]["span_minutes"] == 60


def test_single_instance_result_still_labeled():
    members = [{"id": "solo-db", "is_writer": True, "role": "writer"}]
    orig_resolve, orig_metrics = cw._resolve_instance_ids, cw._get_rds_metrics
    try:
        cw._resolve_instance_ids = lambda identifier: members
        cw._get_rds_metrics = lambda iid, metric, **kw: list(_POINTS)
        res = cw.get_cpu_utilization("solo-db", hours=2)
    finally:
        cw._resolve_instance_ids, cw._get_rds_metrics = orig_resolve, orig_metrics
    # Single-instance return is flattened (no "instances"), but still role-labeled.
    assert "instances" not in res
    assert res["role"] == "writer" and res["is_writer"] is True
    assert res["requested_hours"] == 2
    assert res["data_window"]["point_count"] == 3


# ── _data_window span/point_count, and None on empty ────────────────────────

def test_data_window_span_and_point_count():
    w = cw._data_window(_POINTS)
    assert w["point_count"] == 3
    assert w["span_minutes"] == 60
    assert w["start"] == "2026-02-01T00:00:00+00:00"
    assert w["end"] == "2026-02-01T01:00:00+00:00"


def test_data_window_none_on_empty():
    assert cw._data_window([]) is None
    assert cw._data_window(None) is None


def test_data_window_unsorted_input_still_correct():
    shuffled = [_POINTS[2], _POINTS[0], _POINTS[1]]
    w = cw._data_window(shuffled)
    assert w["start"] == "2026-02-01T00:00:00+00:00"
    assert w["end"] == "2026-02-01T01:00:00+00:00"
    assert w["span_minutes"] == 60


# ── get_replica_lag picks the metric each member publishes ──────────────────

class _LagRDS:
    """describe_db_clusters / describe_db_instances with engine and replica source."""

    def __init__(self, cluster=None, instance=None):
        self._cluster, self._instance = cluster, instance

    def describe_db_clusters(self, DBClusterIdentifier=None):
        if not self._cluster:
            raise Exception("DBClusterNotFoundFault")
        return {"DBClusters": [self._cluster]}

    def describe_db_instances(self, DBInstanceIdentifier=None):
        if not self._instance:
            raise Exception("DBInstanceNotFound")
        return {"DBInstances": [self._instance]}


def _lag(rds, identifier):
    fetched = []
    orig = cw._get_rds_metrics
    try:
        _SF_STATE["rds"] = rds
        cw._get_rds_metrics = lambda iid, metric, **kw: fetched.append((iid, metric)) or list(_POINTS)
        res = cw.get_replica_lag(identifier)
    finally:
        cw._get_rds_metrics = orig
    return res, fetched


def test_replica_lag_aurora_reader_gets_aurora_replica_lag():
    res, fetched = _lag(_LagRDS(cluster={"Engine": "aurora-postgresql", "DBClusterMembers": [
        {"DBInstanceIdentifier": "aur-r", "IsClusterWriter": False},
        {"DBInstanceIdentifier": "aur-w", "IsClusterWriter": True}]}), "aur")
    assert "error" not in res, res
    assert fetched == [("aur-r", "AuroraReplicaLag")], fetched
    w, r = res["instances"]
    assert w["db_instance_id"] == "aur-w" and w["metric"] is None and w["note"] == "writer: no replica lag"
    assert r["role"] == "reader" and r["metric"] == "AuroraReplicaLag" and r["unit"] == "Milliseconds"
    assert r["data_window"]["point_count"] == 3 and r["summary"]["current"] == 30.0
    assert res["primary_instance"] == "aur-w"


def test_replica_lag_multi_az_db_cluster_readers_get_replica_lag():
    res, fetched = _lag(_LagRDS(cluster={"Engine": "postgres", "DBClusterMembers": [
        {"DBInstanceIdentifier": "maz-1", "IsClusterWriter": True},
        {"DBInstanceIdentifier": "maz-2", "IsClusterWriter": False},
        {"DBInstanceIdentifier": "maz-3", "IsClusterWriter": False}]}), "maz")
    assert fetched == [("maz-2", "ReplicaLag"), ("maz-3", "ReplicaLag")], fetched
    assert all(i["unit"] == "Seconds" for i in res["instances"][1:])


def test_replica_lag_rds_read_replica_gets_replica_lag():
    res, fetched = _lag(_LagRDS(instance={"DBInstanceIdentifier": "rep-1",
                                          "ReadReplicaSourceDBInstanceIdentifier": "prim-1"}), "rep-1")
    assert fetched == [("rep-1", "ReplicaLag")], fetched
    assert res["role"] == "replica" and res["metric"] == "ReplicaLag" and res["replica_of"] == "prim-1"
    assert "instances" not in res and res["data_window"]["point_count"] == 3


def test_replica_lag_plain_instance_has_no_metric_and_no_error():
    res, fetched = _lag(_LagRDS(instance={"DBInstanceIdentifier": "solo"}), "solo")
    assert fetched == [] and "error" not in res, res
    assert res["metric"] is None and res["note"] == "writer: no replica lag" and res["data_points"] == []


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", fn.__name__, "-", e or "assertion failed")
    print("\n%d passed, %d failed" % (len(fns) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
