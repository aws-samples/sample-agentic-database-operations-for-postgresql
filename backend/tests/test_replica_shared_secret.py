# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""An RDS read replica can use its primary's secret.

A read replica is its own database record (discovery lists it separately), but its
read-only user IS the primary's: physical replication copies the roles. So the primary's
secret, whose `host` is the primary's endpoint, must be accepted for the replica, at bind
time and at runtime, while any other mismatch is still refused. The source is taken only
from RDS (`ReadReplicaSourceDBInstanceIdentifier`, recorded by discovery as
`tags.replica_of` and `tags.replica_source_endpoint`), never from user input.

Pinned here:
  * discovery records the source and its endpoint, same-Region (from the listing) and
    cross-Region (a best-effort describe in the source's Region);
  * `_allowed_secret_hosts` and `_login_with_secret` accept the primary's host for its
    replica (and still log in to the REPLICA), and refuse it for an unrelated record;
  * once one secret is held by two records, pg_client resolves a database id to that
    record (it dials the replica, not the primary) and refuses an ambiguous ARN handle.

Hermetic: boto3/psycopg2 stubbed, fake RDS and registry. No AWS.

Run:  python3 backend/tests/test_replica_shared_secret.py   (or via pytest)
"""
import json
import os
import pathlib
import re
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
for _m in ("psycopg2", "psycopg2.extras", "boto3"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

APP_SRC = (pathlib.Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")

PRIMARY_EP = "prim.abc.us-east-1.rds.amazonaws.com"
REPLICA_EP = "rep.abc.us-east-1.rds.amazonaws.com"
XR_EP = "rep-west.def.us-west-2.rds.amazonaws.com"
OTHER_EP = "other.abc.us-east-1.rds.amazonaws.com"
SECRET = "arn:aws:secretsmanager:us-east-1:111111111111:secret:dbops/admin-1/prim-ro-AbCdEf"
REPLICA_ID = "11111111-2222-3333-4444-555555555555"
PRIMARY_ID = "99999999-8888-7777-6666-555555555555"


def _extract(name):
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^(?:@|def |class )|\Z)", APP_SRC, re.S | re.M)
    assert m, f"{name} not found in app.py"
    return m.group(0)


class HTTPException(Exception):
    def __init__(self, status_code=None, detail=None):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


# ── discovery ──────────────────────────────────────────────────────────────────

class _Pager:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kw):
        return iter(self._pages)


class _FakeRds:
    def __init__(self, region, instances, clusters=(), globals_=()):
        self.region, self.instances, self.clusters, self.globals_ = region, list(instances), list(clusters), list(globals_)

    def get_paginator(self, name):
        return _Pager({"describe_db_instances": [{"DBInstances": self.instances}],
                       "describe_db_clusters": [{"DBClusters": self.clusters}],
                       "describe_global_clusters": [{"GlobalClusters": self.globals_}]}[name])

    def describe_db_instances(self, DBInstanceIdentifier=None, **kw):
        for i in self.instances:
            if DBInstanceIdentifier in (i["DBInstanceIdentifier"], i.get("DBInstanceArn")):
                return {"DBInstances": [i]}
        raise Exception("DBInstanceNotFound")

    def describe_db_subnet_groups(self, **kw):
        return {"DBSubnetGroups": [{"VpcId": "vpc-1", "Subnets": []}]}


def _inst(name, region, endpoint, source=""):
    return {"DBInstanceIdentifier": name, "Engine": "postgres",
            "DBInstanceArn": f"arn:aws:rds:{region}:111111111111:db:{name}",
            "Endpoint": {"Address": endpoint, "Port": 5432},
            "DBSubnetGroup": {"VpcId": "vpc-1", "DBSubnetGroupName": "sng", "Subnets": []},
            **({"ReadReplicaSourceDBInstanceIdentifier": source} if source else {})}


RDS = {
    "us-east-1": _FakeRds("us-east-1", [_inst("prim", "us-east-1", PRIMARY_EP),
                                        _inst("rep", "us-east-1", REPLICA_EP, source="prim")]),
    "us-west-2": _FakeRds("us-west-2", [_inst("rep-west", "us-west-2", XR_EP,
                                              source="arn:aws:rds:us-east-1:111111111111:db:prim")]),
}


class _FakeSession:
    def __init__(self, region_name=None, **kw):
        self.region = region_name

    def client(self, name, region_name=None, **kw):
        return RDS[region_name or self.region]


def _discover():
    from tools.aws import discovery_client as dc
    dc.boto3 = types.SimpleNamespace(Session=_FakeSession)
    d = dc.DbDiscovery()
    d._sts_client = types.SimpleNamespace(get_caller_identity=lambda: {"Account": "111111111111"})
    found = d.discover_instances_in_account("111111111111", ["us-east-1", "us-west-2"], scan_log=[])
    return {i["identifier"]: i for i in found}


def test_discovery_records_the_source_same_and_cross_region():
    found = _discover()
    assert found["prim"]["replica_of"] == "" and found["prim"]["replica_source_endpoint"] == ""
    assert found["rep"]["replica_of"] == "prim" and found["rep"]["replica_source_endpoint"] == PRIMARY_EP
    assert found["rep-west"]["replica_of"] == "prim", found["rep-west"]
    assert found["rep-west"]["replica_source_endpoint"] == PRIMARY_EP, "cross-Region source resolved in its Region"
    assert "replica_source" not in found["rep"], "the raw source does not leak into the record"


def test_discovery_writes_the_facts_into_tags():
    ns = {}
    exec(_extract("_discovery_rds_tags"), ns)  # noqa: S102
    tags = ns["_discovery_rds_tags"](_discover()["rep"])
    assert tags["replica_of"] == "prim" and tags["replica_source_endpoint"] == PRIMARY_EP


# ── bind time ──────────────────────────────────────────────────────────────────

_connects = []
NS = {"json": json, "HTTPException": HTTPException, "os": os, "ssl_kwargs": lambda: {},
      "logger": types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)}
for _fn in ("_allowed_secret_hosts", "_secret_host_refusal", "_secret_host", "_reject_secret_host_mismatch",
            "_login_with_secret", "_replica_tags"):
    exec(_extract(_fn), NS)  # noqa: S102 - pinning shipped source

REPLICA = {"instance_id": REPLICA_ID, "endpoint": REPLICA_EP, "port": 5432, "database_name": "app",
           "tags": {"replica_of": "prim", "replica_source_endpoint": PRIMARY_EP}}
UNRELATED = {"instance_id": "x", "endpoint": OTHER_EP, "port": 5432, "tags": {}}


class _Sm:
    def __init__(self, host):
        self.host = host

    def get_secret_value(self, SecretId=None):
        return {"SecretString": json.dumps({"username": "ro", "password": "pw", "host": self.host})}


class _Sess:
    def __init__(self, host):
        self.sm = _Sm(host)

    def client(self, name, region_name=None, **kw):
        return self.sm


def test_allowed_hosts_take_the_source_only_for_a_replica():
    allowed = NS["_allowed_secret_hosts"]
    assert allowed(REPLICA) == {REPLICA_EP, PRIMARY_EP}
    assert allowed(UNRELATED) == {OTHER_EP}
    assert allowed({"endpoint": REPLICA_EP, "tags": {"replica_of": "prim"}}) == {REPLICA_EP}, \
        "no recorded source endpoint -> strict"


def test_login_accepts_the_primary_secret_for_its_replica_and_dials_the_replica():
    sys.modules["psycopg2"].connect = lambda **kw: _connects.append(kw) or types.SimpleNamespace(close=lambda: None)
    _connects.clear()
    NS["_login_with_secret"](_Sess(PRIMARY_EP), SECRET, "us-east-1", REPLICA)
    assert _connects and _connects[-1]["host"] == REPLICA_EP, "the login goes to the replica, not the source"


def test_login_refuses_the_primary_secret_for_an_unrelated_record():
    _connects.clear()
    try:
        NS["_login_with_secret"](_Sess(PRIMARY_EP), SECRET, "us-east-1", UNRELATED)
        raise AssertionError("expected a refusal")
    except HTTPException as e:
        assert e.status_code == 400 and OTHER_EP in e.detail
    assert not _connects


def test_configure_persist_path_follows_the_same_rule():
    NS["_reject_secret_host_mismatch"](_Sess(PRIMARY_EP), SECRET, "us-east-1", REPLICA)   # no raise
    try:
        NS["_reject_secret_host_mismatch"](_Sess("third.example.com"), SECRET, "us-east-1", REPLICA)
        raise AssertionError("expected a refusal")
    except HTTPException as e:
        assert e.status_code == 400


def test_enrichment_keeps_discoverys_cross_region_source_endpoint():
    rt = NS["_replica_tags"]
    old = {"replica_of": "prim", "replica_source_endpoint": PRIMARY_EP}
    assert rt(old, {"replica_of": "prim", "replica_source_endpoint": ""}) == old
    assert rt(old, {"replica_of": ""}) == {"replica_of": "", "replica_source_endpoint": ""}, \
        "RDS says it is no longer a replica (promoted): the exception goes"


# ── runtime: pg_client with two holders of one secret ──────────────────────────

def _pg():
    from tools.aws import pg_client as pg
    records = {REPLICA_ID: {**REPLICA, "secret_arn": SECRET},
               PRIMARY_ID: {"instance_id": PRIMARY_ID, "endpoint": PRIMARY_EP, "port": 5432,
                            "secret_arn": SECRET, "tags": {}}}
    sys.modules["tools.aws.dynamodb_client"] = types.SimpleNamespace(
        db_registry=types.SimpleNamespace(get_instance=lambda iid: dict(records[iid]) if iid in records else None))
    pg.get_db_credentials = lambda arn: {"username": "ro", "password": "pw", "host": PRIMARY_EP}
    pg._records_holding_secret = lambda arn, first_only=False: [dict(r) for r in records.values()]
    pg.ssl_kwargs = lambda: {}
    pg._enforce_secret_space_ownership = lambda arn: None
    pg.psycopg2.connect = lambda **kw: _connects.append(kw) or types.SimpleNamespace(set_session=lambda **k: None)
    return pg


def test_pg_client_resolves_a_database_id_to_that_record():
    pg = _pg()
    _connects.clear()
    pg.get_connection(REPLICA_ID)
    assert _connects[-1]["host"] == REPLICA_EP, "the replica's query dials the replica"
    pg.get_connection(PRIMARY_ID)
    assert _connects[-1]["host"] == PRIMARY_EP


def test_pg_client_picks_the_record_the_secret_names_for_an_arn():
    pg = _pg()
    _connects.clear()
    pg.get_connection(SECRET)
    assert _connects[-1]["host"] == PRIMARY_EP, "ARN handle: the holder whose endpoint the secret names"


def test_pg_client_refuses_an_ambiguous_arn():
    pg = _pg()
    pg.get_db_credentials = lambda arn: {"username": "ro", "password": "pw"}   # no host to disambiguate
    try:
        pg.get_connection(SECRET)
        raise AssertionError("expected an ambiguity refusal")
    except ValueError as e:
        assert "several databases" in str(e)


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
