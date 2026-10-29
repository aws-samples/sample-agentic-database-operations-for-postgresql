# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Metadata collector tests for tools.aws.topology_refresh.

Covers the Aurora cluster path (writer/reader roles with endpoints, the writer's
real instance class, subnet AZs via the subnet group, replicas, reader endpoint),
the RDS instance path (AZs straight from the instance's subnet group, replica
links, storage facts), alarm matching by dimension across pages (cluster members
included, unrelated alarms ignored), fail-soft behaviour when EC2 or CloudWatch is
denied, placement backfill only for fields the record lacks, and the refresh stamp.
Hermetic: boto3 is replaced by fake clients; no network.

Run:  python3 backend/tests/test_topology_refresh.py   (or via pytest)
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.modules.setdefault("boto3", types.ModuleType("boto3"))

from tools.aws import topology_refresh as tr  # noqa: E402


class Denied(Exception):
    pass


class FakeRDS:
    def __init__(self, cluster=None, instances=None, subnet_groups=None):
        self.cluster = cluster
        self.instances = instances or {}
        self.subnet_groups = subnet_groups or {}

    def describe_db_clusters(self, DBClusterIdentifier):
        if self.cluster and self.cluster["DBClusterIdentifier"] == DBClusterIdentifier:
            return {"DBClusters": [self.cluster]}
        raise Denied("DBClusterNotFoundFault")

    def describe_db_instances(self, DBInstanceIdentifier=None, Filters=None):
        if Filters:
            cid = Filters[0]["Values"][0]
            return {"DBInstances": [i for i in self.instances.values() if i.get("DBClusterIdentifier") == cid]}
        if DBInstanceIdentifier in self.instances:
            return {"DBInstances": [self.instances[DBInstanceIdentifier]]}
        raise Denied("DBInstanceNotFound")

    def describe_db_subnet_groups(self, DBSubnetGroupName):
        return {"DBSubnetGroups": [self.subnet_groups[DBSubnetGroupName]]}


class FakeLogs:
    def describe_log_groups(self, logGroupNamePrefix, limit):
        if "cluster/orders" in logGroupNamePrefix:
            return {"logGroups": [{"logGroupName": logGroupNamePrefix + "postgresql"}]}
        return {"logGroups": []}


class FakeCW:
    def __init__(self, pages, deny=False):
        self.pages = pages
        self.deny = deny
        self.calls = 0

    def describe_alarms(self, **kwargs):
        if self.deny:
            raise Denied("AccessDenied")
        idx = int(kwargs.get("NextToken") or 0)
        self.calls += 1
        page = {"MetricAlarms": self.pages[idx]}
        if idx + 1 < len(self.pages):
            page["NextToken"] = str(idx + 1)
        return page


class FakeEC2:
    def __init__(self, deny=False):
        self.deny = deny

    def describe_vpcs(self, VpcIds):
        if self.deny:
            raise Denied("UnauthorizedOperation")
        return {"Vpcs": [{"VpcId": VpcIds[0], "CidrBlock": "10.42.0.0/16"}]}


class FakeSession:
    def __init__(self, **clients):
        self.clients = clients

    def client(self, name):
        if name not in self.clients:
            raise Denied(f"no {name}")
        return self.clients[name]


def _alarm(name, dim_name, dim_value, state="OK"):
    return {"AlarmName": name, "StateValue": state, "Dimensions": [{"Name": dim_name, "Value": dim_value}]}


SUBNET_GROUP = {"VpcId": "vpc-db", "Subnets": [
    {"SubnetIdentifier": "subnet-b", "SubnetAvailabilityZone": {"Name": "us-east-1b"}},
    {"SubnetIdentifier": "subnet-a", "SubnetAvailabilityZone": {"Name": "us-east-1a"}},
]}

CLUSTER = {
    "DBClusterIdentifier": "orders", "EngineVersion": "16.4", "MultiAZ": True, "StorageEncrypted": True,
    "KmsKeyId": "arn:aws:kms:us-east-1:1:key/k", "DBClusterParameterGroup": "default.aurora-postgresql16",
    "PerformanceInsightsEnabled": True, "PerformanceInsightsRetentionPeriod": 7,
    "EnabledCloudwatchLogsExports": ["postgresql"], "DeletionProtection": True, "BackupRetentionPeriod": 7,
    "ReaderEndpoint": "orders.cluster-ro.example", "DBSubnetGroup": "orders-subnets",
    "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-1"}],
    "DBClusterMembers": [
        {"DBInstanceIdentifier": "orders-2", "IsClusterWriter": False},
        {"DBInstanceIdentifier": "orders-1", "IsClusterWriter": True},
    ],
    "ReadReplicaIdentifiers": ["arn:aws:rds:eu-west-1:111122223333:cluster:orders-eu"],
    "ReplicationSourceIdentifier": "",
}
MEMBERS = {
    "orders-1": {"DBInstanceIdentifier": "orders-1", "DBClusterIdentifier": "orders",
                 "DBInstanceClass": "db.r6g.xlarge", "Endpoint": {"Address": "orders-1.example"},
                 "AvailabilityZone": "us-east-1a"},
    "orders-2": {"DBInstanceIdentifier": "orders-2", "DBClusterIdentifier": "orders",
                 "DBInstanceClass": "db.r6g.large", "Endpoint": {"Address": "orders-2.example"},
                 "AvailabilityZone": "us-east-1b"},
}
INSTANCE = {
    "DBInstanceIdentifier": "inventory", "EngineVersion": "15.3", "MultiAZ": False, "StorageEncrypted": True,
    "DBInstanceClass": "db.t4g.medium", "StorageType": "gp3", "AllocatedStorage": 100,
    "DBParameterGroups": [{"DBParameterGroupName": "custom-pg15"}],
    "AvailabilityZone": "us-east-1c", "SecondaryAvailabilityZone": "",
    "DBSubnetGroup": {"VpcId": "vpc-inv", "Subnets": [
        {"SubnetIdentifier": "subnet-x", "SubnetAvailabilityZone": {"Name": "us-east-1c"}}]},
    "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-9"}],
    "ReadReplicaDBInstanceIdentifiers": ["inventory-replica"],
    "ReadReplicaSourceDBInstanceIdentifier": "",
}
ALARM_PAGES = [
    [_alarm("orders-cpu", "DBClusterIdentifier", "orders", "ALARM"),
     _alarm("unrelated", "DBInstanceIdentifier", "someone-else")],
    [_alarm("orders-1 free storage", "DBInstanceIdentifier", "orders-1"),
     _alarm("orders,commas", "DBInstanceIdentifier", "orders-2")],
]


def _cluster_session(**over):
    clients = {"rds": FakeRDS(cluster=CLUSTER, instances=MEMBERS, subnet_groups={"orders-subnets": SUBNET_GROUP}),
               "logs": FakeLogs(), "cloudwatch": FakeCW(ALARM_PAGES), "ec2": FakeEC2()}
    clients.update(over)
    return FakeSession(**clients)


# ── cluster path ───────────────────────────────────────────────────────────────
def test_cluster_roles_endpoints_and_real_instance_class():
    out = tr.collect_db_metadata(_cluster_session(), "orders", existing={"vpc_id": "vpc-db"})
    roles = out["fields"]["cluster_member_roles"]
    assert roles == [
        {"instance_id": "orders-2", "is_writer": False, "endpoint": "orders-2.example",
         "availability_zone": "us-east-1b", "instance_class": "db.r6g.large"},
        {"instance_id": "orders-1", "is_writer": True, "endpoint": "orders-1.example",
         "availability_zone": "us-east-1a", "instance_class": "db.r6g.xlarge"},
    ]
    assert "writer_instance_id" not in out["fields"], "only Tier 1 writes the failover key"
    assert out["fields"]["reader_endpoint"] == "orders.cluster-ro.example"
    assert out["tags"]["instance_class"] == "db.r6g.xlarge"   # the writer's class, not a member id
    assert out["tags"]["cluster_members"] == "orders-2,orders-1"


def test_cluster_placement_replicas_and_engine_facts():
    out = tr.collect_db_metadata(_cluster_session(), "orders", existing={"vpc_id": "vpc-db"})
    t = out["tags"]
    assert t["subnet_azs"] == "us-east-1a,us-east-1b"          # sorted, from the subnet group
    assert t["replicas"] == "orders-eu" and t["replica_of"] == ""
    assert t["engine_version"] == "16.4" and t["multi_az"] == "True" and t["pi_retention"] == "7"
    assert t["parameter_group"] == "default.aurora-postgresql16"
    assert t["log_groups"] == "/aws/rds/cluster/orders/postgresql"
    assert t["vpc_cidr"] == "10.42.0.0/16"
    assert t["metadata_refreshed_at"].endswith("+00:00")


def test_cluster_alarms_match_cluster_and_member_dimensions_across_pages():
    cw = FakeCW(ALARM_PAGES)
    out = tr.collect_db_metadata(_cluster_session(cloudwatch=cw), "orders", existing={"vpc_id": "vpc-db"})
    t = out["tags"]
    assert cw.calls == 2
    assert t["alarm_count"] == "3" and t["alarms_in_alarm"] == "1"
    assert t["alarm_names"] == "orders-cpu,orders-1 free storage,orders commas"   # comma sanitised


def test_placement_backfills_only_missing_fields():
    out = tr.collect_db_metadata(_cluster_session(), "orders", existing={})
    assert out["fields"]["vpc_id"] == "vpc-db"
    assert out["fields"]["subnet_ids"] == "subnet-b,subnet-a"
    assert out["fields"]["security_group_ids"] == "sg-1"
    kept = tr.collect_db_metadata(_cluster_session(), "orders",
                                  existing={"vpc_id": "vpc-db", "subnet_ids": "x", "security_group_ids": "y"})
    assert not any(k in kept["fields"] for k in ("vpc_id", "subnet_ids", "security_group_ids"))


# ── instance path ──────────────────────────────────────────────────────────────
def test_instance_path_facts():
    rds = FakeRDS(instances={"inventory": INSTANCE})
    out = tr.collect_db_metadata(FakeSession(rds=rds, logs=FakeLogs(), cloudwatch=FakeCW([[]]), ec2=FakeEC2()),
                                 "inventory", existing={})
    t = out["tags"]
    assert t["instance_class"] == "db.t4g.medium" and t["storage_type"] == "gp3" and t["allocated_storage"] == "100"
    assert t["parameter_group"] == "custom-pg15"
    assert t["subnet_azs"] == "us-east-1c" and t["availability_zone"] == "us-east-1c"
    assert t["replicas"] == "inventory-replica" and t["replica_of"] == ""
    assert t["alarm_count"] == "0" and t["alarms_in_alarm"] == "0" and t["alarm_names"] == ""
    assert out["fields"] == {"vpc_id": "vpc-inv", "subnet_ids": "subnet-x", "security_group_ids": "sg-9"}
    assert "cluster_member_roles" not in out["fields"]


# ── fail-soft ──────────────────────────────────────────────────────────────────
def test_denied_ec2_and_cloudwatch_leave_those_facets_empty():
    sess = _cluster_session(cloudwatch=FakeCW([], deny=True), ec2=FakeEC2(deny=True))
    out = tr.collect_db_metadata(sess, "orders", existing={"vpc_id": "vpc-db"})
    assert "vpc_cidr" not in out["tags"]
    assert "alarm_count" not in out["tags"]          # unknown, not a false zero
    assert out["tags"]["engine_version"] == "16.4"    # the rest still landed
    assert out["tags"]["metadata_refreshed_at"]


def test_missing_clients_do_not_abort():
    sess = FakeSession(rds=FakeRDS(cluster=CLUSTER, instances=MEMBERS, subnet_groups={"orders-subnets": SUBNET_GROUP}))
    out = tr.collect_db_metadata(sess, "orders", existing={"vpc_id": "vpc-db"})
    assert out["tags"]["engine_version"] == "16.4"
    assert "log_groups" not in out["tags"] and "alarm_count" not in out["tags"]


def test_unknown_identifier_returns_none():
    assert tr.collect_db_metadata(_cluster_session(), "ghost", existing={}) is None


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
