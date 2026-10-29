# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""RDS Multi-AZ DB clusters are not Aurora.

describe_db_clusters returns both Aurora clusters and RDS Multi-AZ DB clusters (RDS
engine ``postgres``, a writer and two readable standbys). Onboarding enriches a cluster
ARN through ``_enrich_cluster``, which used to stamp every cluster ``aurora-postgresql``:
a Multi-AZ DB cluster then loaded the Aurora runbook and was labelled Aurora. The record
engine must follow the cluster's real engine: Aurora stays ``aurora-postgresql``; a
Multi-AZ DB cluster is ``rds-postgresql`` (the value ``_enrich_instance`` uses), and keeps
its members and reader endpoint. Hermetic, no AWS.

Run:  python3 backend/tests/test_enrich_cluster_engine.py   (or via pytest)
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.modules.setdefault("boto3", types.ModuleType("boto3"))

from tools.aws import resource_explorer_client as rec  # noqa: E402
from tools import runbook_loader  # noqa: E402

REGION = "us-east-1"


class FakeRds:
    def __init__(self, engine):
        self.engine = engine

    def describe_db_clusters(self, DBClusterIdentifier):
        return {"DBClusters": [{
            "DBClusterIdentifier": DBClusterIdentifier, "Engine": self.engine, "EngineVersion": "16.4",
            "Endpoint": f"{DBClusterIdentifier}.cluster-x.{REGION}.rds.amazonaws.com",
            "ReaderEndpoint": f"{DBClusterIdentifier}.cluster-ro-x.{REGION}.rds.amazonaws.com",
            "Port": 5432, "DatabaseName": "app", "DBSubnetGroup": "sng", "VpcSecurityGroups": [],
            "Status": "available", "MultiAZ": True,
            "DBClusterMembers": [{"DBInstanceIdentifier": f"{DBClusterIdentifier}-1", "IsClusterWriter": True},
                                 {"DBInstanceIdentifier": f"{DBClusterIdentifier}-2", "IsClusterWriter": False},
                                 {"DBInstanceIdentifier": f"{DBClusterIdentifier}-3", "IsClusterWriter": False}],
        }]}

    def describe_db_subnet_groups(self, DBSubnetGroupName):
        return {"DBSubnetGroups": [{"VpcId": "vpc-1", "Subnets": [{"SubnetIdentifier": "subnet-a"}]}]}

    def describe_db_instances(self, Filters):
        cid = Filters[0]["Values"][0]
        return {"DBInstances": [{"DBInstanceIdentifier": f"{cid}-{i}",
                                 "Endpoint": {"Address": f"{cid}-{i}.x.{REGION}.rds.amazonaws.com"},
                                 "AvailabilityZone": f"{REGION}{'abc'[i - 1]}",
                                 "DBInstanceClass": "db.r6g.large"}
                                for i in (1, 2, 3)]}


def _enrich(engine, name):
    arn = f"arn:aws:rds:{REGION}:111122223333:cluster:{name}"
    return rec.ResourceExplorerClient()._enrich_cluster(FakeRds(engine), arn, REGION)


def test_aurora_cluster_stays_aurora():
    d = _enrich("aurora-postgresql", "aur")
    assert d["engine"] == "aurora-postgresql", d


def test_multi_az_db_cluster_is_rds_postgresql():
    d = _enrich("postgres", "maz")
    assert d["engine"] == "rds-postgresql", d
    assert d["reader_endpoint"] == f"maz.cluster-ro-x.{REGION}.rds.amazonaws.com"
    assert [m["instance_id"] for m in d["members"]] == ["maz-1", "maz-2", "maz-3"]
    assert [m["is_writer"] for m in d["members"]] == [True, False, False]
    assert all(m["endpoint"] for m in d["members"]), "member endpoints still resolved"
    assert [m["availability_zone"] for m in d["members"]] == [f"{REGION}a", f"{REGION}b", f"{REGION}c"]
    assert all(m["instance_class"] == "db.r6g.large" for m in d["members"]), "members carry their class"
    assert "writer_instance_id" not in d, "discovery never writes the failover key"


def test_non_postgres_cluster_is_still_skipped():
    assert _enrich("aurora-mysql", "my").get("skip") is True


def test_multi_az_db_cluster_loads_the_rds_runbook():
    assert runbook_loader._ENGINE_SOP_MAP["rds-postgresql"] == "rds-postgresql.sop.md"
    assert runbook_loader._ENGINE_SOP_MAP[_enrich("postgres", "maz")["engine"]] == "rds-postgresql.sop.md"
    assert runbook_loader._ENGINE_SOP_MAP[_enrich("aurora-postgresql", "aur")["engine"]] == "aurora-postgresql.sop.md"


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
