# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Topology builder tests for tools.aws.topology_builder.

Covers the reachability classifier (same VPC, active/creating/failed/orphaned
connections, missing VPC, failed or in-progress configuration), the graph the
builder emits for a mixed fleet (node and edge types, per-database `reaches`
edges, `in_region` edges, dedup, network and cluster facets surfaced from the
registry record), the `coverage` and `warnings` sections, and get_node_detail.
Hermetic: every registry the builder reads is a stub; no boto3, no network.

Run:  python3 backend/tests/test_topology_builder.py   (or via pytest)
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── stubs for the registries the builder imports at module load ────────────────
DBS = []          # what db_registry.list_instances() returns
CONNECTIONS = []  # what the network registry scan returns
AGENT = types.SimpleNamespace(agent_vpc_id="vpc-agent", agent_vpc_cidr="172.20.0.0/16")


class _Registry:
    def list_instances(self):
        return list(DBS)


class _Table:
    def scan(self):
        return {"Items": list(CONNECTIONS)}


class _NetworkRegistry:
    def __init__(self):
        self.table = _Table()

    @staticmethod
    def make_connection_id(agent_vpc_id, db_vpc_id, db_endpoint="", method=""):
        # Same derivation as the real NetworkRegistry (which needs boto3 to import).
        import hashlib
        if method == "lattice" and db_endpoint:
            key = f"{agent_vpc_id}:{db_endpoint}:lattice"
        elif method == "lattice-gateway":
            key = f"{agent_vpc_id}:{db_vpc_id}:lattice-gateway"
        else:
            key = f"{agent_vpc_id}:{db_vpc_id}:peering"
        return hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()[:16]


def _lt_id(endpoint, vpc="vpc-pl"):
    return _NetworkRegistry.make_connection_id("vpc-agent", vpc, endpoint, "lattice")


def _gw_id(vpc="vpc-pl"):
    return _NetworkRegistry.make_connection_id("vpc-agent", vpc, method="lattice-gateway")


class _Reports:
    def list_reports(self, account_id="", instance_id=""):
        return [{"type": "html"}, {"type": "json"}] if instance_id == "db-1" else []


class _Recs:
    def list_for_instance(self, instance_id):
        return [{"status": "open"}, {"status": "resolved"}] if instance_id == "db-1" else []


def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


sys.modules.setdefault("boto3", types.ModuleType("boto3"))
_mod("tools.aws.dynamodb_client", db_registry=_Registry())
_mod("tools.aws.network_registry", NetworkRegistry=_NetworkRegistry)
_mod("tools.aws.network_client", network_configurator=AGENT)
_mod("tools.aws.s3_client", report_storage=_Reports())
_mod("tools.aws.recommendations_client", recommendations_registry=_Recs())

from tools.aws import topology_builder as tb  # noqa: E402  (tenancy is a real leaf module)


def _db(instance_id, **over):
    base = {
        "instance_id": instance_id, "display_name": instance_id, "engine": "aurora-postgresql",
        "endpoint": f"{instance_id}.cluster.example",
        "status": "configured", "member_space_ids": ["sp-1"], "account_id": "111122223333",
        "region": "us-east-1", "vpc_id": "vpc-db", "network_method": "peering",
        "subnet_ids": "subnet-a,subnet-b", "security_group_ids": "sg-1",
        "reader_endpoint": f"{instance_id}.cluster-ro.example",
        "cluster_member_roles": [
            {"instance_id": f"{instance_id}-1", "is_writer": True, "endpoint": "w.example"},
            {"instance_id": f"{instance_id}-2", "is_writer": False, "endpoint": "r.example"},
        ],
        "tags": {"engine_version": "16.4", "metadata_refreshed_at": "2026-09-01T00:00:00+00:00",
                 "subnet_azs": "us-east-1a,us-east-1b", "alarm_count": "2", "alarm_names": "cpu-high",
                 "replicas": "", "replica_of": ""},
    }
    base.update(over)
    return base


def _reset():
    DBS.clear()
    CONNECTIONS.clear()
    AGENT.agent_vpc_id = "vpc-agent"


# ── classify_reachability ──────────────────────────────────────────────────────
def test_classify_same_vpc_is_reachable_without_a_connection():
    r = tb.classify_reachability({"vpc_id": "vpc-agent", "status": "configured"}, None, "vpc-agent")
    assert r["reachability"] == "reachable" and r["method"] == "same_vpc"


def test_classify_connection_statuses():
    db = {"vpc_id": "vpc-x", "status": "configured", "network_method": "lattice"}
    for status, expect in (("active", "reachable"), ("creating", "pending"),
                           ("failed", "blocked"), ("orphaned", "blocked"), ("delete_failed", "blocked"),
                           ("weird", "unknown")):
        r = tb.classify_reachability(db, {"status": status, "method": "lattice"}, "vpc-agent")
        assert r["reachability"] == expect, (status, r)
        assert r["method"] == "lattice"


def test_classify_incomplete_teardown_names_the_failed_step():
    db = {"vpc_id": "vpc-x", "status": "configured", "network_method": "lattice"}
    r = tb.classify_reachability(db, {"status": "delete_failed", "method": "lattice",
                                      "failed_step": "resource_configuration"}, "vpc-agent")
    assert r["reachability"] == "blocked"
    assert r["detail"] == "Teardown incomplete: resource_configuration"


def test_classify_database_state_wins_over_connection():
    conn = {"status": "active", "method": "peering"}
    failed = tb.classify_reachability({"vpc_id": "vpc-x", "status": "config_failed",
                                       "config_error": "route table full"}, conn, "vpc-agent")
    assert failed["reachability"] == "blocked" and failed["detail"] == "route table full"
    pending = tb.classify_reachability({"vpc_id": "vpc-x", "status": "configuring"}, conn, "vpc-agent")
    assert pending["reachability"] == "pending"
    from datetime import datetime, timezone
    deleting = tb.classify_reachability({"vpc_id": "vpc-x", "status": "deleting",
                                         "teardown_claimed_at": datetime.now(timezone.utc).isoformat()},
                                        conn, "vpc-agent")
    assert deleting["reachability"] == "pending" and deleting["detail"] == "Being removed"
    # A deleting claim with no heartbeat for TEARDOWN_STALE_S: its process stopped.
    stalled = tb.classify_reachability({"vpc_id": "vpc-x", "status": "deleting",
                                        "teardown_claimed_at": "2020-01-01T00:00:00+00:00"},
                                       conn, "vpc-agent")
    assert stalled["reachability"] == "blocked" and stalled["status"] == "delete_failed"
    assert stalled["detail"].startswith("Teardown incomplete: the removal stopped")
    left = tb.classify_reachability({"vpc_id": "vpc-x", "status": "delete_failed",
                                     "config_error": "The network teardown did not finish."}, conn, "vpc-agent")
    assert left["reachability"] == "blocked" and left["detail"].startswith("Teardown incomplete: The network")


def test_classify_missing_information_is_unknown_not_reachable():
    assert tb.classify_reachability({"status": "configured"}, None, "vpc-agent")["reachability"] == "unknown"
    r = tb.classify_reachability({"vpc_id": "vpc-x", "status": "configured", "network_method": "peering"},
                                 None, "vpc-agent")
    assert r["reachability"] == "unknown" and r["method"] == "peering"


# ── build_topology ─────────────────────────────────────────────────────────────
def _fleet():
    _reset()
    DBS.extend([
        _db("db-1"),                                                       # peering, active
        _db("db-2", vpc_id="vpc-pl", network_method="lattice", engine="rds-postgresql",
            cluster_member_roles=[], reader_endpoint="", region="eu-west-1",
            tags={"engine_version": "15.3"}),                              # lattice, creating
        _db("db-3", vpc_id="vpc-agent", network_method="", tags={}),      # same VPC, never refreshed
        _db("db-4", vpc_id="", network_method="", status="config_failed",
            config_error="No subnet group", tags={}),                     # nothing recorded
        _db("other-space", member_space_ids=["sp-2"]),                    # excluded
        _db("discovered-only", status="discovered"),                       # excluded
    ])
    CONNECTIONS.extend([
        {"db_vpc_id": "vpc-db", "method": "peering", "status": "active", "db_vpc_cidr": "10.0.0.0/16",
         "connection_id": "c1", "peering_id": "pcx-1"},
        {"db_vpc_id": "vpc-pl", "method": "lattice", "status": "creating",
         "connection_id": _lt_id("db-2.cluster.example"), "vpce_id": "vpce-1",
         "resource_configuration_arn": "arn:rcfg-2"},
        {"db_vpc_id": "vpc-pl", "method": "lattice-gateway", "status": "active", "db_vpc_cidr": "10.1.0.0/16",
         "connection_id": _gw_id(), "resource_gateway_id": "rgw-1"},
    ])
    return tb.build_topology("sp-1")


def _by_type(topo, t):
    return [n for n in topo["nodes"] if n["type"] == t]


def _edges(topo, t):
    return [e for e in topo["edges"] if e["type"] == t]


def test_build_keeps_the_existing_shape_and_adds_coverage_and_warnings():
    topo = _fleet()
    assert set(topo) == {"space_id", "generated_at", "stats", "coverage", "warnings", "nodes", "edges"}
    assert topo["stats"]["databases"] == 4
    assert topo["stats"]["accounts"] == 1 and topo["stats"]["regions"] == 2
    assert topo["stats"]["peering_connections"] == 1
    assert topo["stats"]["lattice_connections"] == 1
    assert topo["stats"]["same_vpc_connections"] == 1
    assert topo["stats"]["agents"] == len(tb.SUB_AGENTS) and topo["stats"]["services"] == len(tb.AGENT_SERVICES)


def test_build_excludes_other_spaces_and_discovered_rows():
    topo = _fleet()
    labels = {n["label"] for n in _by_type(topo, "database")}
    assert labels == {"db-1", "db-2", "db-3", "db-4"}


def test_build_reaches_edges_one_per_database_with_classification():
    topo = _fleet()
    reaches = {e["target"]: e for e in _edges(topo, "reaches")}
    assert set(reaches) == {"db-db-1", "db-db-2", "db-db-3", "db-db-4"}
    assert all(e["source"] == "agent-vpc" for e in reaches.values())
    assert reaches["db-db-1"]["metadata"]["reachability"] == "reachable"
    assert reaches["db-db-2"]["metadata"]["reachability"] == "pending"
    assert reaches["db-db-3"]["metadata"] == {"method": "same_vpc", "status": "active",
                                              "reachability": "reachable",
                                              "detail": "Database shares the agent's VPC"}
    assert reaches["db-db-4"]["metadata"]["reachability"] == "blocked"
    assert reaches["db-db-4"]["metadata"]["detail"] == "No subnet group"


def test_build_vpc_level_edges_include_same_vpc_and_region():
    topo = _fleet()
    same = _edges(topo, "same_vpc")
    assert len(same) == 1 and same[0]["target"] == "vpc-vpc-agent"
    pl = _edges(topo, "lattice")[0]
    assert pl["metadata"]["vpce_id"] == "vpce-1" and pl["metadata"]["vpce_ids"] == ["vpce-1"]
    assert pl["metadata"]["status"] == "active" and pl["metadata"]["resource_gateway_id"] == "rgw-1"
    reach2 = next(e for e in _edges(topo, "reaches") if e["target"] == "db-db-2")
    assert reach2["metadata"]["vpce_id"] == "vpce-1" and reach2["metadata"]["status"] == "creating"
    in_region = {(e["source"], e["target"]) for e in _edges(topo, "in_region")}
    assert ("vpc-vpc-db", "region-us-east-1") in in_region
    assert ("vpc-vpc-pl", "region-eu-west-1") in in_region
    # region nodes are no longer orphans
    touched = {e["source"] for e in topo["edges"]} | {e["target"] for e in topo["edges"]}
    assert all(n["id"] in touched for n in _by_type(topo, "region"))


def test_build_edges_are_deduplicated():
    topo = _fleet()
    keys = [(e["source"], e["type"], e["target"]) for e in topo["edges"]]
    assert len(keys) == len(set(keys))


def test_build_database_node_surfaces_network_and_cluster_facets():
    topo = _fleet()
    db1 = next(n for n in _by_type(topo, "database") if n["label"] == "db-1")
    m = db1["metadata"]
    # backwards-compatible keys are still there
    for key in ("endpoint", "port", "vpc_id", "network_method", "engine_version", "alarm_count",
                "report_count", "rec_total", "rec_active", "cluster_members", "log_groups"):
        assert key in m, key
    assert m["report_count"] == 1 and m["rec_total"] == 2 and m["rec_active"] == 1
    # new facets
    assert m["subnet_ids"] == ["subnet-a", "subnet-b"]
    assert m["subnet_azs"] == ["us-east-1a", "us-east-1b"]
    assert m["security_group_ids"] == ["sg-1"]
    assert m["reader_endpoint"] == "db-1.cluster-ro.example"
    assert m["is_cluster"] is True
    assert m["writer_instance"] == "db-1-1" and m["reader_instances"] == ["db-1-2"]
    assert m["alarm_names"] == ["cpu-high"]
    assert m["reachability"] == "reachable" and m["reach_method"] == "peering"
    assert m["metadata_refreshed_at"].startswith("2026-09-01")
    # a VPC node picks up the CIDR from the connection entry
    vpc = next(n for n in _by_type(topo, "vpc") if n["metadata"]["vpc_id"] == "vpc-db")
    assert vpc["metadata"]["cidr"] == "10.0.0.0/16" and vpc["metadata"]["is_agent_vpc"] is False
    agent_vpc = next(n for n in _by_type(topo, "vpc") if n["metadata"]["vpc_id"] == "vpc-agent")
    assert agent_vpc["metadata"]["is_agent_vpc"] is True and agent_vpc["metadata"]["cidr"] == "172.20.0.0/16"


def test_build_cluster_members_fall_back_to_the_legacy_tag():
    _reset()
    DBS.append(_db("legacy", cluster_member_roles=[], tags={"cluster_members": "legacy-1,legacy-2"}))
    topo = tb.build_topology("sp-1")
    m = _by_type(topo, "database")[0]["metadata"]
    assert [r["instance_id"] for r in m["cluster_member_roles"]] == ["legacy-1", "legacy-2"]
    assert m["writer_instance"] == "" and m["is_cluster"] is True


def test_build_coverage_counts():
    topo = _fleet()
    assert topo["coverage"] == {
        "databases": 4, "with_network": 3, "with_metadata": 2, "refreshed": 1,
        "reachable": 2, "pending": 1, "blocked": 1, "unknown": 0,
    }


def test_build_warnings_name_the_database():
    topo = _fleet()
    codes = {(w["code"], w.get("node_id")) for w in topo["warnings"]}
    assert ("metadata_missing", "db-db-3") in codes
    assert ("metadata_missing", "db-db-4") in codes
    assert ("no_vpc", "db-db-4") in codes
    assert ("connection_blocked", "db-db-4") in codes
    assert not any(w["code"] == "metadata_missing" and w["node_id"] == "db-db-1" for w in topo["warnings"])
    assert all(w["message"] for w in topo["warnings"])


def test_build_without_agent_vpc_has_no_connectivity_edges_and_says_so():
    _fleet()
    AGENT.agent_vpc_id = ""
    topo = tb.build_topology("sp-1")
    AGENT.agent_vpc_id = "vpc-agent"
    assert not _edges(topo, "reaches") and not _edges(topo, "peering") and not _edges(topo, "runs")
    assert any(w["code"] == "agent_vpc_unknown" for w in topo["warnings"])
    assert not any(n["type"] == "agent_vpc" for n in topo["nodes"])


def test_build_network_registry_failure_is_a_warning_not_an_error():
    _fleet()
    orig = _Table.scan
    _Table.scan = lambda self: (_ for _ in ()).throw(RuntimeError("ddb down"))
    try:
        topo = tb.build_topology("sp-1")
    finally:
        _Table.scan = orig
    assert any(w["code"] == "network_registry_unavailable" for w in topo["warnings"])
    # without the registry the peering DB is "unknown", not silently green
    reach = next(e for e in _edges(topo, "reaches") if e["target"] == "db-db-1")
    assert reach["metadata"]["reachability"] == "unknown"


def _two_lattice_dbs(reverse):
    _reset()
    DBS.extend([
        _db("lt-a", vpc_id="vpc-pl", network_method="lattice", cluster_member_roles=[]),
        _db("lt-b", vpc_id="vpc-pl", network_method="lattice", cluster_member_roles=[]),
    ])
    rows = [
        {"db_vpc_id": "vpc-pl", "method": "lattice", "status": "active",
         "connection_id": _lt_id("lt-a.cluster.example"), "vpce_id": "vpce-a", "resource_configuration_arn": "arn:a"},
        {"db_vpc_id": "vpc-pl", "method": "lattice", "status": "creating",
         "connection_id": _lt_id("lt-b.cluster.example"), "vpce_id": "vpce-b", "resource_configuration_arn": "arn:b"},
        {"db_vpc_id": "vpc-pl", "method": "lattice-gateway", "status": "active",
         "connection_id": _gw_id(), "resource_gateway_id": "rgw-9"},
    ]
    CONNECTIONS.extend(reversed(rows) if reverse else rows)
    return tb.build_topology("sp-1")


def test_build_two_lattice_databases_in_one_vpc_each_get_their_own_edge():
    for reverse in (False, True):
        topo = _two_lattice_dbs(reverse)
        reaches = {e["target"]: e["metadata"] for e in _edges(topo, "reaches")}
        assert reaches["db-lt-a"]["vpce_id"] == "vpce-a" and reaches["db-lt-a"]["status"] == "active", reaches
        assert reaches["db-lt-a"]["reachability"] == "reachable"
        assert reaches["db-lt-b"]["vpce_id"] == "vpce-b" and reaches["db-lt-b"]["status"] == "creating"
        assert reaches["db-lt-b"]["reachability"] == "pending"
        assert reaches["db-lt-a"]["connection_id"] == _lt_id("lt-a.cluster.example")
        nodes = {n["id"]: n["metadata"] for n in _by_type(topo, "database")}
        assert nodes["db-lt-a"]["vpce_id"] == "vpce-a" and nodes["db-lt-b"]["vpce_id"] == "vpce-b"
        assert nodes["db-lt-b"]["resource_configuration_arn"] == "arn:b"
        lattice = _edges(topo, "lattice")
        assert len(lattice) == 1, lattice
        m = lattice[0]["metadata"]
        assert m["status"] == "active" and m["resource_gateway_id"] == "rgw-9"
        assert m["connection_id"] == _gw_id() and m["vpce_ids"] == ["vpce-a", "vpce-b"] and "vpce_id" not in m
        assert topo["stats"]["lattice_connections"] == 2


# ── get_node_detail ────────────────────────────────────────────────────────────
def test_node_detail_lists_neighbours():
    _fleet()
    detail = tb.get_node_detail("sp-1", "db-db-1")
    assert detail["node"]["label"] == "db-1"
    ids = {n["id"] for n in detail["connected_nodes"]}
    assert ids == {"vpc-vpc-db", "agent-vpc"}
    assert tb.get_node_detail("sp-1", "nope") == {"error": "Node not found"}


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
