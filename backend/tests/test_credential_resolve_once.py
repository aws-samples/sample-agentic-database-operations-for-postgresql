# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A chat turn's credential handle is resolved exactly once, through the REAL chain.

Seen in a real agent trace: every PostgreSQL specialist tool failed with "Pass the
database id from [DATABASE SCOPE]; secret ARNs are not accepted." although the model had
passed the database id. ``pg_client.get_connection`` resolved the id to the space's
secret ARN, then ``get_db_credentials`` -> ``_get_secrets_client`` ->
``session_factory.get_session`` resolved that ARN a second time, and inside an agent
request ``resolve_credential`` refuses anything starting with ``arn:``. So every
database connection made from chat was refused. The other tests stub the session factory
or the resolver, which is why none of them saw it.

This test runs the real code from the tool down:

    db_tools tool -> pg_client.execute_query -> get_connection -> resolve_credential
      -> _enforce_secret_space_ownership -> get_db_credentials -> _get_secrets_client
      -> session_factory.get_session -> _lookup_db_record -> STS assume_role
      -> Secrets Manager -> psycopg2.connect

inside an agent request (CURRENT_USER_ID and CURRENT_SPACE_ID set). Only the edges are
fake: boto3 (DynamoDB tables, STS, Secrets Manager, RDS) and psycopg2. Nothing between
them is stubbed. The database lives in a member account, so the cross-account role is
assumed with the space owner's ExternalId and the secret is read through that session.

Pinned:
  * a database id (UUID) from the scope connects, through get_health_summary (the tool
    in the trace) and check_db_connectivity (test_connection, a second double-resolve site);
  * a raw secret ARN from the model is still refused, with the right message, before any
    AWS call;
  * the observability tool that takes the handle (get_instance_details) resolves it once
    and reaches the member account; a raw ARN there is refused too.

Run:  python3 backend/tests/test_credential_resolve_once.py
"""
import json
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

passed = failed = 0


def check(cond, label):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}")


DEPLOYMENT_ACCOUNT = "111111111111"
MEMBER_ACCOUNT = "222222222222"
SPACE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OWNER = "owner-admin-sub"
DB_ID = "9e46bfaa-19d8-4fd5-9909-909918dc8ea8"
ROLE = "dbops-agent-f4489458a071"
SECRET = (f"arn:aws:secretsmanager:us-east-1:{MEMBER_ACCOUNT}:secret:"
          f"dbops/{OWNER}/aurora-pg-database-1/readonly-xjmoUY")
ENDPOINT = "aurora-pg-database-1.cluster-abc.us-east-1.rds.amazonaws.com"

RECORD = {
    "instance_id": DB_ID, "display_name": "aurora-pg-database-1", "status": "configured",
    "account_id": MEMBER_ACCOUNT, "region": "us-east-1", "engine": "aurora-postgresql",
    "endpoint": ENDPOINT, "port": 5432, "database_name": "postgres",
    "secret_arn": SECRET, "reach_space_id": SPACE,
    "member_space_ids": {SPACE}, "space_secrets": {SPACE: SECRET},
    "tags": {"raw_identifier": "aurora-pg-database-1",
             "arn": f"arn:aws:rds:us-east-1:{MEMBER_ACCOUNT}:cluster:aurora-pg-database-1"},
}
SPACE_ROW = {"space_id": SPACE, "owner_admin": OWNER,
             "secondary_role_names": {MEMBER_ACCOUNT: ROLE}}

CALLS = []  # (edge, detail)


# ── fake boto3 (the AWS edge) ───────────────────────────────────────────────────
class FakeTable:
    def __init__(self, name):
        self.name = name

    def get_item(self, Key=None, **kw):
        CALLS.append(("ddb.get_item", self.name))
        if "space_id" in (Key or {}):
            return {"Item": dict(SPACE_ROW)} if Key["space_id"] == SPACE else {}
        if (Key or {}).get("instance_id") == DB_ID:
            return {"Item": dict(RECORD)}
        return {}

    def scan(self, **kw):
        CALLS.append(("ddb.scan", self.name))
        return {"Items": [dict(RECORD)] if "instances" in self.name else []}


class FakeSts:
    def get_caller_identity(self):
        return {"Account": DEPLOYMENT_ACCOUNT}

    def assume_role(self, RoleArn, RoleSessionName, DurationSeconds, ExternalId):
        CALLS.append(("sts.assume_role", (RoleArn, ExternalId)))
        return {"Credentials": {"AccessKeyId": "AKIDFAKE", "SecretAccessKey": "fake",
                                "SessionToken": "fake"}}


class FakeSecrets:
    def __init__(self, session):
        self.session = session

    def get_secret_value(self, SecretId):
        CALLS.append(("secretsmanager.get_secret_value", (SecretId, self.session.assumed)))
        return {"SecretString": json.dumps({"username": "dbops_ro", "password": "pw",
                                            "host": ENDPOINT, "port": 5432})}


class NotFound(Exception):
    pass


class FakeRds:
    def __init__(self, session):
        self.session = session
        self.exceptions = types.SimpleNamespace(DBClusterNotFoundFault=NotFound)

    def describe_db_clusters(self, DBClusterIdentifier):
        CALLS.append(("rds.describe_db_clusters", (DBClusterIdentifier, self.session.assumed)))
        return {"DBClusters": [{"DBClusterIdentifier": DBClusterIdentifier, "EngineVersion": "17.7",
                                "DBClusterMembers": [{"DBInstanceIdentifier": "database-1-instance-1",
                                                      "IsClusterWriter": True}]}]}

    def describe_db_instances(self, DBInstanceIdentifier):
        return {"DBInstances": [{"DBInstanceIdentifier": DBInstanceIdentifier,
                                 "DBInstanceClass": "db.r6g.2xlarge", "AvailabilityZone": "us-east-1b",
                                 "DBInstanceStatus": "available"}]}


class FakeSession:
    def __init__(self, region_name=None, aws_access_key_id=None, **kw):
        self.region_name = region_name
        self.assumed = aws_access_key_id == "AKIDFAKE"

    def client(self, name, region_name=None, config=None):
        return {"secretsmanager": FakeSecrets, "rds": FakeRds}[name](self)


_boto3 = types.ModuleType("boto3")
_boto3.Session = FakeSession
_boto3.resource = lambda *a, **k: types.SimpleNamespace(Table=FakeTable)
_boto3.client = lambda name, **k: FakeSts()
sys.modules["boto3"] = _boto3
_ddb = types.ModuleType("boto3.dynamodb")
_cond = types.ModuleType("boto3.dynamodb.conditions")
_cond.Key = _cond.Attr = lambda *a, **k: types.SimpleNamespace(
    eq=lambda *x: None, is_in=lambda *x: None, exists=lambda: None)
sys.modules["boto3.dynamodb"] = _ddb
sys.modules["boto3.dynamodb.conditions"] = _cond
try:
    import importlib
    importlib.import_module("botocore.config")
    importlib.import_module("botocore.exceptions")
except ImportError:  # pragma: no cover - botocore ships with the backend image
    _bc = types.ModuleType("botocore")
    _bcc = types.ModuleType("botocore.config")
    _bcc.Config = lambda **k: None
    _bce = types.ModuleType("botocore.exceptions")
    _bce.ClientError = type("ClientError", (Exception,), {})
    sys.modules.update({"botocore": _bc, "botocore.config": _bcc, "botocore.exceptions": _bce})


# ── fake psycopg2 (the database edge) ───────────────────────────────────────────
class FakeCursor:
    def __init__(self):
        self.description = None
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        self.description = [("col",)]
        if "version()" in query:
            self._rows = [{"version": "PostgreSQL 17.7", "database": "postgres",
                           "user": "dbops_ro", "host": "10.0.0.5"}]
        else:
            self._rows = [{"conn_pct": 4.0, "current_conn": 8, "max_conn": 200, "xid_pct": 1.0,
                           "xid_age": 2000, "freeze_max_age": 200000000, "cache_hit_ratio": 99.9,
                           "long_queries": 0, "waiting_queries": 0, "needs_vacuum": 0,
                           "total_tables": 10, "blocked_queries": 0}]

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def cursor(self, cursor_factory=None):
        return FakeCursor()

    def set_session(self, **kw):
        CALLS.append(("pg.set_session", kw))

    def close(self):
        pass


def _connect(**kw):
    CALLS.append(("pg.connect", kw))
    return FakeConn()


_pg = types.ModuleType("psycopg2")
_pg.connect = _connect
_pg.Error = type("Error", (Exception,), {})
_pg.OperationalError = type("OperationalError", (_pg.Error,), {})
_pg_extras = types.ModuleType("psycopg2.extras")
_pg_extras.RealDictCursor = object
_pg.extras = _pg_extras
sys.modules["psycopg2"] = _pg
sys.modules["psycopg2.extras"] = _pg_extras

# strands is the agent framework, not part of the chain: @tool is the identity here.
_strands = types.ModuleType("strands")
_strands.tool = lambda f=None, **kw: (f if f is not None else (lambda g: g))
sys.modules["strands"] = _strands

os.environ["AWS_REGION"] = "us-east-1"

from tools.aws import pg_client as pg  # noqa: E402
from tools.aws import session_factory as sf  # noqa: E402
from tools import db_tools  # noqa: E402
import model_resolver as mr  # noqa: E402


def _fresh():
    """Clear every cache in the chain so each call walks it end to end."""
    CALLS.clear()
    pg._secrets_cache.clear()
    pg._secret_space_cache.clear()
    sf._db_record_cache.clear()
    sf._space_cache.clear()
    sf._agent_account_id = None
    sf._sts_client = None


def edges(name):
    return [d for e, d in CALLS if e == name]


# The agent request: /invocations sets these after validating the caller's space.
mr.CURRENT_USER_ID.set("operator-1")
mr.CURRENT_SPACE_ID.set(SPACE)

print("== a database id from the scope connects (get_health_summary, as in the trace) ==")
_fresh()
try:
    out = db_tools.get_health_summary(secret_arn=DB_ID, database="postgres")
    err = ""
except Exception as e:  # noqa: BLE001 - the failure IS the finding
    out, err = {}, f"{type(e).__name__}: {e}"
check(not err, f"the UUID handle is not refused ({err or 'no error'})")
check(out.get("overall_score") == "ok" and out.get("metrics", {}).get("max_conn") == 200,
      "the health summary is computed from the database's rows")
check(edges("sts.assume_role") == [(f"arn:aws:iam::{MEMBER_ACCOUNT}:role/{ROLE}", OWNER)],
      f"the member account's role is assumed with the owner's ExternalId ({edges('sts.assume_role')})")
check(edges("secretsmanager.get_secret_value") == [(SECRET, True)],
      "the space's own secret is read through the assumed session")
conn = (edges("pg.connect") or [{}])[0]
check(conn.get("host") == ENDPOINT and conn.get("user") == "dbops_ro"
      and conn.get("sslmode") == "verify-full",
      "the connection dials the registry endpoint with verify-full")
check(edges("pg.set_session") == [{"readonly": True, "autocommit": True}], "the session is read-only")

print("")
print("== check_db_connectivity (test_connection) resolves once too ==")
_fresh()
res = db_tools.check_db_connectivity(secret_arn=DB_ID, database="postgres")
check(res.get("connected") is True, f"connected ({res.get('error', 'no error')})")
check(len(edges("pg.connect")) == 1 and edges("secretsmanager.get_secret_value") == [(SECRET, True)],
      "one connection, with the space's secret")

print("")
print("== a raw secret ARN from the model is still refused ==")
_fresh()
try:
    db_tools.get_health_summary(secret_arn=SECRET, database="postgres")
    msg = ""
except PermissionError as e:
    msg = str(e)
check(msg == "Use a database alias from [DATABASE SCOPE]; secret ARNs are not accepted.",
      f"refused with the scope message ({msg or 'not refused'})")
check(not edges("secretsmanager.get_secret_value") and not edges("sts.assume_role")
      and not edges("pg.connect"), "no AWS call and no connection for the refused ARN")
_fresh()
res = db_tools.check_db_connectivity(secret_arn=SECRET)
check(res.get("connected") is False and "secret ARNs are not accepted" in res.get("error", ""),
      "check_db_connectivity reports the same refusal")

print("")
print("== another space cannot use the id ==")
_fresh()
mr.CURRENT_SPACE_ID.set("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
try:
    db_tools.get_health_summary(secret_arn=DB_ID, database="postgres")
    msg = ""
except PermissionError as e:
    msg = str(e)
check(msg == "This database is not accessible in the current space.",
      f"a non-member space is refused ({msg or 'not refused'})")
check(not edges("pg.connect"), "and nothing is dialled")
mr.CURRENT_SPACE_ID.set(SPACE)

print("")
print("== the observability tool that takes the handle (get_instance_details) ==")
_fresh()
det = db_tools.get_instance_details(identifier=DB_ID, secret_arn=DB_ID)
check("error" not in det and det.get("cluster_identifier") == "aurora-pg-database-1",
      f"the UUID handle resolves and the cluster is described ({det.get('error', 'no error')})")
check(edges("rds.describe_db_clusters") == [("aurora-pg-database-1", True)],
      "RDS is read in the member account, through the assumed role")
_fresh()
det = db_tools.get_instance_details(identifier=DB_ID, secret_arn=SECRET)
check(det.get("error") == "Use a database alias from [DATABASE SCOPE]; secret ARNs are not accepted.",
      f"a raw ARN from the model is refused there too ({det.get('error')})")
check(not edges("rds.describe_db_clusters") and not edges("sts.assume_role"), "before any AWS call")

print("")
print("== outside an agent request (monitoring engine) ARNs still pass ==")
_fresh()
mr.CURRENT_USER_ID.set("")
mr.CURRENT_SPACE_ID.set("")
check(pg.resolve_credential(SECRET) == SECRET, "an ARN passes through unchanged")
check(pg.get_db_credentials(SECRET).get("username") == "dbops_ro", "and its credentials are read")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
