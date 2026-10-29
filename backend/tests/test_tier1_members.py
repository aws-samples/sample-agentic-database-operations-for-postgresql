# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tier 1 collects from every cluster member, detects a failover once, flags host-route drift.

Through the real ``tier1_collector.handler`` with fakes for everything outside it: a fake
``psycopg2.connect`` (records the dialled host, answers by SQL substring, and reports
``pg_is_in_recovery`` per host), fake STS / Secrets Manager / RDS / EC2 / SNS clients, a
fake instances table that evaluates the collector's condition expressions the way
DynamoDB would, a fake ``metrics_store`` (put_tier1_rows / publish_tier1_metrics) and a patched
``audit_log.write_event``. Hermetic, no AWS, no DNS.

Sections: collection (every member, observed roles, writer fallback, partial, a
standalone replica, the wall-clock budget, the secret-host refusal, the Lattice and
host-route dial gates); failover (exactly one audit and one notice, races, first run,
membership change, describe denied); host-route DETECTION (moved address, departed
member, unsafe address, unresolved names, no drift, drift gone, deleting record): Tier 1
flags route_repair_needed and never writes a route; the API task repairs
(test_route_repair.py).

Run:  python3 backend/tests/test_tier1_members.py
"""
import copy
import os
import pathlib
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["RESOURCE_PREFIX"] = "acme"

# psycopg2 is a Lambda layer, not a test dependency: a stub module with the attributes the
# collector names at import time. connect() is replaced per test.
_pg = types.ModuleType("psycopg2")
_pg.extras = types.SimpleNamespace(RealDictCursor=object)
_pg.extensions = types.SimpleNamespace(connection=object)
_pg.connect = lambda **kw: (_ for _ in ()).throw(AssertionError("connect not patched"))
sys.modules.setdefault("psycopg2", _pg)
sys.modules.setdefault("psycopg2.extras", _pg.extras)

from botocore.exceptions import ClientError  # noqa: E402
import tools.aws  # noqa: E402
from tools.aws import audit_log, host_routes  # noqa: E402
from tools.aws import metrics_store as _REAL_STORE  # noqa: E402  (constants only)
import tier1_collector as t1  # noqa: E402

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


REGION = "us-east-1"
ARN = f"arn:aws:rds:{REGION}:111111111111:cluster:orders"
W, R1, R2 = "orders-1", "orders-2", "orders-3"
EP = {W: "orders-1.x.rds.amazonaws.com", R1: "orders-2.x.rds.amazonaws.com", R2: "orders-3.x.rds.amazonaws.com"}
CLUSTER_EP = "orders.cluster-x.rds.amazonaws.com"
READER_EP = "orders.cluster-ro-x.rds.amazonaws.com"


def cfail(code, op="UpdateItem"):
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


# ── fake DynamoDB ──────────────────────────────────────────────────────────────

class FakeTable:
    """The instances (or spaces) table. update_item evaluates the condition shapes the
    collector writes, as DynamoDB would."""

    def __init__(self, rows=None, key="instance_id"):
        self.key = key
        self.rows = {r[key]: copy.deepcopy(r) for r in rows or []}
        self.updates = []
        self.before_update = None   # hook: mutate the row between read and write (races)

    def get_item(self, Key):
        r = self.rows.get(Key[self.key])
        return {"Item": copy.deepcopy(r)} if r else {}

    def scan(self, **kw):
        return {"Items": [copy.deepcopy(r) for r in self.rows.values()]}

    def _ok(self, cond, row, names, vals):
        n = lambda tok: names.get(tok, tok)  # noqa: E731
        parts = {
            "attribute_exists(instance_id) AND (attribute_not_exists(#writer_instance_id) "
            "OR #writer_instance_id = :old_w)":
                lambda: row is not None and ("writer_instance_id" not in row
                                             or row["writer_instance_id"] == vals[":old_w"]),
            "#st = :configured": lambda: row is not None and row.get(n("#st")) == vals[":configured"],
            "#st = :configured AND #hr = :old_routes":
                lambda: row is not None and row.get("status") == vals[":configured"]
                and row.get("host_routes") == vals[":old_routes"],
            "#st = :configured AND attribute_not_exists(#hr)":
                lambda: row is not None and row.get("status") == vals[":configured"]
                and "host_routes" not in row,
            "attribute_exists(instance_id)": lambda: row is not None,
        }
        assert cond in parts, f"unexpected condition {cond!r}"
        return parts[cond]()

    def update_item(self, Key, UpdateExpression, ConditionExpression, ExpressionAttributeNames,
                    ExpressionAttributeValues=None):
        ExpressionAttributeValues = ExpressionAttributeValues or {}
        if self.before_update:
            hook, self.before_update = self.before_update, None
            hook(self)
        row = self.rows.get(Key[self.key])
        if not self._ok(ConditionExpression, row, ExpressionAttributeNames, ExpressionAttributeValues):
            raise cfail("ConditionalCheckFailedException")
        set_part, _, remove_part = UpdateExpression.partition("REMOVE ")
        sets = {}
        for part in filter(None, set_part.strip().removeprefix("SET ").split(", ")):
            name, val = part.split(" = ")
            sets[ExpressionAttributeNames.get(name, name)] = copy.deepcopy(ExpressionAttributeValues[val])
        row.update(sets)
        for name in filter(None, remove_part.split(", ")):
            row.pop(ExpressionAttributeNames.get(name.strip(), name.strip()), None)
        self.updates.append(dict(sets, **({"REMOVE": remove_part} if remove_part else {})))


class FakeDynamo:
    def __init__(self, instances, spaces=None):
        self.instances = instances
        self.spaces = spaces or FakeTable([], key="space_id")

    def Table(self, name):  # noqa: N802 - boto3's name
        return self.instances if name.endswith("-instances") or "instances" in name else self.spaces


# ── fake AWS clients ───────────────────────────────────────────────────────────

class FakeRds:
    def __init__(self, world):
        self.world = world

    def describe_db_clusters(self, DBClusterIdentifier):
        self.world.calls.append(("describe_db_clusters", DBClusterIdentifier))
        if self.world.rds_error:
            raise cfail(self.world.rds_error, "DescribeDBClusters")
        return {"DBClusters": [{"DBClusterIdentifier": DBClusterIdentifier, "ReaderEndpoint": READER_EP,
                                "DBClusterMembers": [{"DBInstanceIdentifier": m, "IsClusterWriter": m == self.world.writer}
                                                     for m in self.world.members]}]}

    def describe_db_instances(self, Filters):
        self.world.calls.append(("describe_db_instances", Filters[0]["Values"][0]))
        # A member still `creating` (endpoint None) has no Endpoint yet, as RDS reports it.
        return {"DBInstances": [{"DBInstanceIdentifier": m, "AvailabilityZone": "us-east-1c",
                                 "DBInstanceClass": "db.r6g.large",
                                 **({"Endpoint": {"Address": self.world.endpoints[m]}}
                                    if self.world.endpoints.get(m) else {})}
                                for m in self.world.members]}


class FakeSecrets:
    def __init__(self, world):
        self.world = world

    def get_secret_value(self, SecretId):
        import json
        return {"SecretString": json.dumps({"host": self.world.secret_host, "username": "ro",
                                            "password": "x", "dbname": "app"})}


class FakeSession:
    def __init__(self, world, **kw):
        self.world, self.kw = world, kw

    def client(self, name, region_name=None):
        if name == "rds":
            return FakeRds(self.world)
        if name == "secretsmanager":
            return FakeSecrets(self.world)
        raise AssertionError(name)


class FakeEc2:
    def __init__(self, routes):
        self.routes = dict(routes)   # cidr -> (target, state)
        self.calls = []

    def describe_route_tables(self, RouteTableIds):
        out = []
        for c, (t, st) in self.routes.items():
            r = {"DestinationCidrBlock": c, "State": st}
            r["VpcPeeringConnectionId" if t.startswith("pcx-") else "GatewayId"] = t
            out.append(r)
        return {"RouteTables": [{"Routes": out}]}

    def describe_vpc_peering_connections(self, VpcPeeringConnectionIds):
        return {"VpcPeeringConnections": [{"RequesterVpcInfo": {"VpcId": "vpc-agent"},
                                           "AccepterVpcInfo": {"VpcId": "vpc-other"}}]}

    def create_route(self, RouteTableId, DestinationCidrBlock, VpcPeeringConnectionId):
        self.calls.append(("create", DestinationCidrBlock))
        self.routes[DestinationCidrBlock] = (VpcPeeringConnectionId, "active")

    def replace_route(self, RouteTableId, DestinationCidrBlock, VpcPeeringConnectionId):
        self.calls.append(("replace", DestinationCidrBlock))
        self.routes[DestinationCidrBlock] = (VpcPeeringConnectionId, "active")

    def delete_route(self, RouteTableId, DestinationCidrBlock):
        self.calls.append(("delete", DestinationCidrBlock))
        self.routes.pop(DestinationCidrBlock, None)


class FakeSns:
    def __init__(self, world):
        self.world = world

    def publish(self, **kw):
        self.world.published.append(kw)


# ── fake PostgreSQL ────────────────────────────────────────────────────────────

class FakeCursor:
    def __init__(self, conn):
        self.conn, self.sql = conn, ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, *a):
        self.sql = sql
        if "aurora_replica_status" in sql and self.conn.lag_error:
            raise RuntimeError("function aurora_replica_status() does not exist")

    def fetchone(self):
        if "pg_is_in_recovery" in self.sql:
            return (self.conn.in_recovery,)
        if "aurora_db_instance_identifier()" in self.sql and "aurora_replica_status" not in self.sql:
            return (getattr(self.conn, "server_id", ""),)
        if "pg_last_xact_replay_timestamp" in self.sql and self.conn.standby:
            # A PostgreSQL standby: (received LSN, replayed LSN, last replayed commit age s[,
            # WAL receiver connected]). Evaluates the collector's CASE.
            recv, replay, age_s, *rest = self.conn.standby
            receiving = rest[0] if rest else True
            if "CASE WHEN pg_last_wal_receive_lsn() = pg_last_wal_replay_lsn()" in self.sql and recv == replay:
                gated = "pg_stat_wal_receiver WHERE status IS NULL OR status = 'streaming'" in self.sql
                return (0 if (receiving or not gated) else None,)
            return (age_s * 1000,)
        if "aurora_replica_status" in self.sql or "pg_last_xact_replay_timestamp" in self.sql:
            return (12.5,)
        return {"ratio": 99.0, "total": self.conn.conns, "active": 1, "idle": 3, "idle_in_txn": 0,
                "waiting": 0, "blocked": 0, "pct": 1.0, "size_mb": 10.0, "workers": 0, "cnt": 0, "secs": 2.0,
                # pg_stat_database counters (deadlocks, transactions, temp files)
                "deadlocks": 4, "xact_commit": 1000, "xact_rollback": 0, "temp_files": 2,
                "temp_bytes_mb": 1.5, "max_conn": 200}

    def fetchall(self):
        return []


class FakeConn:
    def __init__(self, host, in_recovery, conns, lag_error=False, standby=None):
        self.host, self.in_recovery, self.conns, self.lag_error = host, in_recovery, conns, lag_error
        self.standby = standby

    def set_session(self, **kw):
        pass

    def cursor(self, cursor_factory=None):
        return FakeCursor(self)

    def close(self):
        pass


class World:
    """One test's universe: the cluster as RDS reports it and the hosts as they behave."""

    def __init__(self, record, writer=W, members=(W, R1, R2)):
        self.writer, self.members = writer, list(members)
        self.endpoints = dict(EP)
        self.rds_error = ""
        self.secret_host = record.get("endpoint", "")
        self.calls, self.dialled, self.published, self.audits, self.written = [], [], [], [], []
        self.host_behaviour = {}   # host -> "fail" | threading.Event (block until set)
        self.lag_error = False
        self.store_error = None    # an exception the store write raises
        self.retention, self.cw_published, self.previous = [], [], []
        self.statuses = []         # collection_failed datums: True = collected
        self.standby = None        # (received LSN, replayed LSN, last commit age s) on a PG standby
        self.instances = FakeTable([record])
        self.spaces = FakeTable([], key="space_id")
        self.ec2 = FakeEc2({})
        self.dns = {}

    def role_of(self, host):
        by_ep = {ep: m for m, ep in self.endpoints.items()}
        if host in by_ep:
            return by_ep[host] != self.writer
        return False   # the cluster endpoint follows the writer

    def connect(self, **kw):
        host = kw["host"]
        self.dialled.append(host)
        b = self.host_behaviour.get(host)
        if b == "fail":
            raise RuntimeError(f"could not connect to server {host}: timeout")
        if isinstance(b, threading.Event):
            b.wait(10)
            raise RuntimeError("released")
        conn = FakeConn(host, self.role_of(host), conns=len(self.dialled), lag_error=self.lag_error,
                        standby=self.standby)
        # The instance the connection reached: the cluster endpoint follows the live writer.
        by_ep = {ep: m for m, ep in self.endpoints.items() if ep}
        conn.server_id = by_ep.get(host, self.writer)
        return conn


def install(world):
    world_boto = types.SimpleNamespace(
        Session=lambda **kw: FakeSession(world, **kw),
        client=lambda name, region_name=None, **kw: {
            "sts": types.SimpleNamespace(assume_role=lambda **k: {"Credentials": {
                "AccessKeyId": "a", "SecretAccessKey": "b", "SessionToken": "c"}}),
            "ec2": world.ec2, "sns": FakeSns(world)}[name],
        resource=lambda *a, **k: FakeDynamo(world.instances, world.spaces),
    )
    t1.boto3 = world_boto
    t1._dynamodb = FakeDynamo(world.instances, world.spaces)
    t1.psycopg2 = types.SimpleNamespace(connect=world.connect, extras=_pg.extras, extensions=_pg.extensions)
    t1.ssl_kwargs = lambda: {}
    store = types.ModuleType("tools.aws.metrics_store")
    store.TIER1_NUMERIC_METRICS = _REAL_STORE.TIER1_NUMERIC_METRICS
    store.TIER1_LIST_FIELDS = _REAL_STORE.TIER1_LIST_FIELDS

    def put_tier1_rows(instance_id, rows, retention_days=None):
        if world.store_error:
            raise world.store_error
        world.written.extend(rows)
        world.retention.append(retention_days)
        return len(rows)

    def publish_tier1_metrics(instance_id, rows):
        world.cw_published.extend(rows)
        return len(rows)

    store.put_tier1_rows = put_tier1_rows
    store.publish_tier1_metrics = publish_tier1_metrics
    store.publish_collection_status = lambda iid, ok, when=None: world.statuses.append(ok)
    store.tier1_previous = lambda iid, member, scan_time, hours=24: next(
        (dict(r) for r in world.previous
         if r.get("member_instance_id", iid) == member and r["scan_time"] < scan_time), None)
    tools.aws.metrics_store = store
    sys.modules["tools.aws.metrics_store"] = store
    audit_log.write_event = lambda *a, **k: (world.audits.append((a, k)), "stored")[1]
    host_routes.resolve = lambda h: list(world.dns.get(h, []))


class Ctx:
    def __init__(self, ms=170000):
        self.ms = ms

    def get_remaining_time_in_millis(self):
        return self.ms


def cluster_record(**over):
    rec = {"instance_id": "db-orders", "display_name": "orders", "arn": ARN, "region": REGION,
           "engine": "aurora-postgresql", "status": "configured", "endpoint": CLUSTER_EP,
           "reader_endpoint": READER_EP, "port": 5432, "database_name": "app",
           "secret_arn": f"arn:aws:secretsmanager:{REGION}:111111111111:secret:ro",
           "account_id": "111111111111", "member_space_ids": {"space-a"},
           "network_method": "peering", "network_route_mode": "vpc", "writer_instance_id": W,
           "cluster_member_roles": [{"instance_id": m, "is_writer": m == W, "endpoint": EP[m],
                                     "availability_zone": "us-east-1a", "instance_class": "db.r6g.large"}
                                    for m in (W, R1, R2)]}
    rec.update(over)
    return rec


def run(world, ctx=None):
    install(world)
    return t1.handler({"instance_id": "db-orders"}, ctx or Ctx())


def stored(world):
    return world.instances.rows["db-orders"]


# ══ collection ══════════════════════════════════════════════════════════════════
print("== (a) a 1-writer, 2-reader cluster writes one row per member ==")
w = World(cluster_record())
out = run(w)
by = {r["member_instance_id"]: r for r in w.written}
check(out["status"] == "ok" and set(by) == {W, R1, R2}, f"three rows, status ok (got {out['status']})")
check(by[W]["member_role"] == "writer" and by[R1]["member_role"] == "reader" and by[R2]["member_role"] == "reader",
      "each row carries its observed role")
check(set(w.dialled) == {EP[W], EP[R1], EP[R2]}, "every member dialled on its own instance endpoint")
check(by[R1]["replica_lag_ms"] == 12.5 and by[W]["replica_lag_ms"] is None, "replica lag on readers only")
check(out["members"] == {W: "ok", R1: "ok", R2: "ok"}, "per-member outcomes")
check(all(r.get("cache_hit_ratio") == 99.0 for r in w.written), "the metrics themselves are collected")
check(len(w.written) == 3, "one store write carries all three rows")
check(len(w.cw_published) == 3, "and the same three rows are published to CloudWatch")
check(w.retention == [None], "the record's retention (none set: the default) goes with the write")
check(w.statuses == [True], "the run publishes collection_failed = 0 (collected)")
check(by[W]["connections_used_pct"] == round(100.0 * by[W]["total_connections"] / 200, 2),
      "connections as a share of max_connections")

print("== the row carries every Tier 1 field, the new counters included ==")
check(all(k in by[W] for k in _REAL_STORE.TIER1_NUMERIC_METRICS + _REAL_STORE.TIER1_LIST_FIELDS),
      "every numeric and list field the store knows is on the row")
check(by[W]["deadlocks"] == 4 and by[W]["xact_commit"] == 1000 and by[W]["temp_files"] == 2,
      "deadlocks, transactions and temp files are collected")
row = t1._row("db-orders", {"member_instance_id": R1, "member_role": "reader", "replica_lag_ms": 3.0})
check(row["member_instance_id"] == R1 and row["member_role"] == "reader" and row["replica_lag_ms"] == 3.0, "_row")

print("== one scheduled run delivered twice (a retry, an overlap) writes nothing new ==")
EVENT = {"instance_id": "db-orders", "scheduled_time": "2030-01-01T12:08:27Z"}


def keys(rows):
    return [(r["instance_id"], r["member_instance_id"], r["scan_time"]) for r in rows]


w = World(cluster_record())
install(w)
t1.handler(dict(EVENT), Ctx())
first = keys([t1._row("db-orders", r) for r in w.written])
w.written.clear()
t1.handler(dict(EVENT), Ctx())
second = keys([t1._row("db-orders", r) for r in w.written])
check(len(first) == 3 and {k[2] for k in first} == {"2030-01-01T12:08:00+00:00"},
      f"every member row carries the scheduled time, to the minute ({sorted({k[2] for k in first})})")
check(sorted(second) == sorted(first),
      "the second delivery has the same (instance, member, scan_time) keys, so the unique index takes none")
w.written.clear()
t1.handler(dict(EVENT, scheduled_time="2030-01-01T12:13:27Z"), Ctx())
check({k[2] for k in keys(w.written)} == {"2030-01-01T12:13:00+00:00"}, "the next scheduled run is its own slot")
w.written.clear()
t1.handler({"instance_id": "db-orders", "time": "2030-01-01T12:18:27Z"}, Ctx())
check({k[2] for k in keys(w.written)} == {"2030-01-01T12:18:00+00:00"}, "an EventBridge event's time is used too")
w.written.clear()
t1.handler({"instance_id": "db-orders"}, Ctx())
check(len(w.written) == 3 and all(r["scan_time"].startswith(str(datetime.now(timezone.utc).year))
                                  for r in w.written), "a manual invoke without a time uses the clock")
APP = (pathlib.Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
check('"scheduled_time": "<aws.scheduler.scheduled-time>"' in APP,
      "the Tier 1 schedule's input asks EventBridge Scheduler for the scheduled time")

print("== (b) the writer's instance endpoint fails: the cluster endpoint, tagged with the writer ==")
w = World(cluster_record())
w.host_behaviour[EP[W]] = "fail"
out = run(w)
by = {r["member_instance_id"]: r for r in w.written}
check(CLUSTER_EP in w.dialled and by[W]["member_role"] == "writer", "fell back and still tagged writer")
check(out["status"] == "ok", "a successful fallback is not a failure")

print("== (c) one reader fails: partial, the others written ==")
w = World(cluster_record())
w.host_behaviour[EP[R2]] = "fail"
out = run(w)
check(out["status"] == "partial" and {r["member_instance_id"] for r in w.written} == {W, R1}, "partial, two rows")
check(out["members"][R2].startswith("RuntimeError:"), "the failure is reported per member")

print("== (d) a standalone RDS read replica writes one reader row ==")
rep = {"instance_id": "db-orders", "display_name": "inv-replica", "region": REGION, "engine": "rds-postgresql",
       "status": "configured", "endpoint": "inv-replica.x.rds.amazonaws.com", "port": 5432,
       "secret_arn": "arn:aws:secretsmanager:us-east-1:1:secret:ro",
       "tags": {"arn": f"arn:aws:rds:{REGION}:111111111111:db:inv-replica", "replica_of": "inv"}}
w = World(rep)
w.endpoints = {}
w.role_of = lambda host: True
out = run(w)
check(out["status"] == "ok" and len(w.written) == 1, "one row")
check(w.written[0]["member_instance_id"] == "inv-replica" and w.written[0]["member_role"] == "reader",
      "id from tags.arn (N5), role observed as reader")
check(("describe_db_clusters", "inv-replica") not in w.calls and not w.calls, "no cluster sync for an instance")

print("== an idle primary: a caught-up RDS standby reports lag 0, not the last commit's age ==")
w = World(rep)
w.endpoints = {}
w.role_of = lambda host: True
w.standby = ("0/5000", "0/5000", 3600.0)
run(w)
lag = w.written[0]["replica_lag_ms"]
check(lag == 0, f"received == replayed: lag 0 ms (got {lag})")
check(lag is not None and lag < 5000, "under the 5 s WARNING band, so the Members tab shows no false lag")
w = World(rep)
w.endpoints = {}
w.role_of = lambda host: True
w.standby = ("0/9000", "0/5000", 45.0)
run(w)
check(w.written[0]["replica_lag_ms"] == 45000.0, "WAL outstanding: the last commit's age is the lag")
w = World(rep)
w.endpoints = {}
w.role_of = lambda host: True
w.standby = ("0/5000", "0/5000", 3600.0, False)
run(w)
check(w.written[0]["replica_lag_ms"] is None,
      "caught up but its WAL receiver is down: lag unknown (NULL), never 0")
check("pg_stat_wal_receiver" in t1.REPLAY_LAG_MS_SQL, "the lag query checks the WAL receiver")

print("== (e) a member that hangs past the budget is skipped; the rest are written ==")
w = World(cluster_record())
gate = threading.Event()
w.host_behaviour[EP[R2]] = gate
t0 = time.monotonic()
out = run(w, Ctx(ms=(t1.BUDGET_MARGIN_S + 1) * 1000))
took = time.monotonic() - t0
gate.set()
check(took < 5, f"the handler returns in bounded time ({took:.1f}s)")
check(out["members"][R2] == "skipped: time budget" and out["status"] == "partial", "skipped: time budget, partial")
check({r["member_instance_id"] for r in w.written} == {W, R1}, "the finished rows are written")

print("== (f) the secret-host refusal is unchanged ==")
w = World(cluster_record())
w.secret_host = "someone-else.x.rds.amazonaws.com"
out = run(w)
check(out["status"] == "error" and not w.written and not w.dialled, "no member is dialled, nothing written")
check(w.statuses == [False], "a run that collects nothing publishes collection_failed = 1")
check(all("is not this instance's endpoint" in v for v in out["members"].values()), "each member names why")

print("== a failed store write: the Collection status says so, and nothing reports ok ==")
w = World(cluster_record())
w.store_error = RuntimeError("AccessDeniedException")
out = run(w)
note = stored(w).get("tier1_store_error") or {}
check(out["status"] == "error" and "metrics store write failed" in out["message"],
      "the handler reports the store write failure")
check("Tier 1 table" in note.get("message", "") and note.get("at"),
      "the record carries the note the Collection view shows, naming what to check")
check(set(out["members"].values()) == {"collected, not written: the store write failed"} and not w.written,
      "the outcomes match what was written: no member reports ok when its row did not land")
check(not w.cw_published, "nothing is published to CloudWatch for rows that were not stored")
check(w.statuses == [False], "a failed store write counts as a failed collection")
print("== a crash still publishes collection_failed = 1, and still raises ==")
w = World(cluster_record())
install(w)
_real_creds = t1._get_db_credentials
t1._get_db_credentials = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("AccessDenied"))
try:
    t1.handler({"instance_id": "db-orders"}, Ctx())
    check(False, "the crash propagates (the DLQ keeps the event)")
except RuntimeError:
    check(w.statuses == [False], "the crash is published as a failed collection before it propagates")
finally:
    t1._get_db_credentials = _real_creds
w = World(cluster_record())
install(w)
t1.handler({"instance_id": "db-gone"}, Ctx())
check(w.statuses == [], "a database that is not in the registry publishes nothing (it is not broken, it is gone)")
n_updates = len(w.instances.updates)
install(w)
t1.handler({"instance_id": "db-orders"}, Ctx())
check(len(w.instances.updates) == n_updates, "the same failure again is not rewritten")
w.store_error = None
install(w)
out = t1.handler({"instance_id": "db-orders"}, Ctx())
check(out["status"] == "ok" and "tier1_store_error" not in stored(w), "a later good write clears the note")

print("== per-interval counters come from the member's previous sample ==")
prev = {"member_instance_id": W, "scan_time": "2030-01-01T12:00:00+00:00",
        "deadlocks": 1, "xact_commit": 400, "xact_rollback": 0}
cur = {"member_instance_id": W, "scan_time": "2030-01-01T12:05:00+00:00",
       "deadlocks": 4, "xact_commit": 1000, "xact_rollback": 0}
rates = t1.derive_counter_rates(cur, prev)
check(rates["deadlocks_delta"] == 3, "deadlocks since the previous sample")
check(rates["txn_per_sec"] == 2.0, "600 transactions over 300 s is 2/s")
check(rates["rollback_ratio_pct"] == 0.0, "no rollbacks in the interval")
reset = t1.derive_counter_rates({**cur, "deadlocks": 0, "xact_commit": 10}, prev)
check(reset["deadlocks_delta"] is None and reset["txn_per_sec"] is None,
      "a statistics reset (a counter went backwards) gives no delta, never a negative")
first = t1.derive_counter_rates({**cur, "xact_rollback": 10}, None)
check(first["deadlocks_delta"] is None and first["rollback_ratio_pct"] == round(100 * 10 / 1010, 3),
      "without a previous sample: no deltas, the cumulative rollback share")
w = World(cluster_record())
# A manual invoke stamps the rows with the clock, so the previous sample is 5 min before it.
w.previous = [{**prev, "scan_time": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()}]
run(w)
row = next(r for r in w.written if r["member_instance_id"] == W)
check(row["deadlocks_delta"] == 3 and row["txn_per_sec"] is not None,
      "the handler fills the counters from the previous stored sample before writing")

print("== (g) Lattice: readers skipped, the writer on the cluster endpoint ==")
w = World(cluster_record(network_method="lattice", network_route_mode=""))
out = run(w)
check(w.dialled == [CLUSTER_EP] and out["status"] == "ok", "only the cluster endpoint; status ok")
check(out["members"][R1] == host_routes.LATTICE_SKIP and out["members"][W] == "ok", "readers reported as skipped")

print("== (g) host routes: a reader whose address has no owned /32 is skipped ==")
w = World(cluster_record(network_route_mode="host", status="configuring",
                         host_routes=[{"ip": "10.0.1.1", "host": EP[W]}, {"ip": "10.0.1.2", "host": EP[R1]}]))
w.dns = {EP[W]: ["10.0.1.1"], EP[R1]: ["10.0.1.2"], EP[R2]: ["10.0.1.3"]}
out = run(w)
check(EP[R2] not in w.dialled and out["status"] == "ok", "not dialled; status ok")
check(out["members"][R2] == "skipped: not routed (10.0.1.3 has no owned host route)", "the reason says why")
check(EP[W] in w.dialled and EP[R1] in w.dialled, "routed members are dialled directly")

# ══ failover ═══════════════════════════════════════════════════════════════════
print("== a writer flip db-a -> db-b updates the record, audits and notifies once ==")
os.environ["NOTIFICATION_TOPIC_ARN"] = "arn:aws:sns:us-east-1:111111111111:acme-alerts"
w = World(cluster_record(), writer=R1)
out = run(w)
rec = stored(w)
check(rec["writer_instance_id"] == R1, "writer_instance_id moves to the new writer")
check([m["instance_id"] for m in rec["cluster_member_roles"] if m["is_writer"]] == [R1], "roles follow")
check(any("Failover detected: writer orders-1 -> orders-2" in x for x in rec["config_log"]), "a config_log line")
check(rec.get("members_changed_at"), "members_changed_at is stamped")
(args, kw), = w.audits
check(args[:4] == ("system", "cluster_failover_detected", "db-orders", "account"), "one audit event as system")
check(kw["after"]["writer_instance_id"] == R1 and kw["after"]["previous_writer_instance_id"] == W
      and kw["space_ids"] == ["space-a"], "audit carries old/new writer and the member spaces")
(pub,) = w.published
check(pub["Subject"] == "Database failover detected" and W in pub["Message"] and R1 in pub["Message"]
      and '"instance_id"' not in pub["Message"], "one plain-text notice")
by = {r["member_instance_id"]: r for r in w.written}
check(by[R1]["member_role"] == "writer" and by[W]["member_role"] == "reader", "this run's rows already carry the new roles")

print("== a second run makes no event ==")
w.audits.clear(); w.published.clear()
install(w)
t1.handler({"instance_id": "db-orders"}, Ctx())
check(not w.audits and not w.published, "no second audit or notice")

print("== two runs racing on the condition make one event ==")
w = World(cluster_record(), writer=R1)
install(w)


def other_run_won(table):
    table.rows["db-orders"]["writer_instance_id"] = R1


w.instances.before_update = other_run_won
t1.handler({"instance_id": "db-orders"}, Ctx())
check(not w.audits and not w.published, "the run that lost the conditional write neither audits nor notifies")
check(len(w.written) == 3, "and still collects")

print("== the first run without writer_instance_id initialises silently ==")
rec0 = cluster_record()
rec0.pop("writer_instance_id")
w = World(rec0)
run(w)
check(stored(w)["writer_instance_id"] == W and not w.audits and not w.published, "initialised, no event")
check("members_changed_at" not in stored(w), "nothing changed but the key: no members_changed_at")

print("== a reader added: one describe_db_instances, no audit ==")
w = World(cluster_record(), members=(W, R1, R2, "orders-4"))
w.endpoints["orders-4"] = "orders-4.x.rds.amazonaws.com"
run(w)
check(("describe_db_instances", "orders") in w.calls and not w.audits and not w.published, "detail lookup, no event")
new = {m["instance_id"]: m for m in stored(w)["cluster_member_roles"]}
check(new["orders-4"]["endpoint"] == "orders-4.x.rds.amazonaws.com" and new["orders-4"]["availability_zone"],
      "the new member is stored with its endpoint and AZ")
check(any("Cluster members changed" in x for x in stored(w)["config_log"]), "a config_log line")
check("orders-4" in {r["member_instance_id"] for r in w.written}, "and collected in the same run")

print("== a reader added while `creating` gets its endpoint on a later run ==")
w = World(cluster_record(), members=(W, R1, R2, "orders-4"))
w.endpoints["orders-4"] = None
out = run(w)
new = {m["instance_id"]: m for m in stored(w)["cluster_member_roles"]}
check(new["orders-4"]["endpoint"] == "" and out["members"]["orders-4"] == "skipped: no instance endpoint",
      "first run: stored without an endpoint, skipped")
w.endpoints["orders-4"] = "orders-4.x.rds.amazonaws.com"
w.calls.clear(); w.written.clear()
install(w)
out = t1.handler({"instance_id": "db-orders"}, Ctx())
new = {m["instance_id"]: m for m in stored(w)["cluster_member_roles"]}
check(("describe_db_instances", "orders") in w.calls, "the gap triggers another detail lookup")
check(new["orders-4"]["endpoint"] == "orders-4.x.rds.amazonaws.com", "the endpoint is filled in")
check("orders-4" in {r["member_instance_id"] for r in w.written} and out["members"]["orders-4"] == "ok",
      "and the reader is collected in that run")
check(sum("Cluster members changed" in x for x in stored(w)["config_log"]) == 1,
      "filling a detail is not logged as a membership change")
w.calls.clear()
install(w)
t1.handler({"instance_id": "db-orders"}, Ctx())
check(("describe_db_instances", "orders") not in w.calls, "once every member is complete, no more lookups")

print("== describe AccessDenied: collection proceeds on the stored roles ==")
w = World(cluster_record(), writer=R1)
w.rds_error = "AccessDenied"
out = run(w)
check(out["status"] == "ok" and len(w.written) == 3 and not w.audits, "collected, no event")
check(stored(w)["writer_instance_id"] == W, "the record is unchanged")

print("== roles change between the stored record and connection time, the sync failing ==")
# Stored: writer orders-1. Live: orders-2 is the writer. RDS throttles, so the record
# still says orders-1. Over Lattice the writer is dialled on the cluster endpoint, which
# reaches orders-2.
w = World(cluster_record(network_method="lattice", network_route_mode=""), writer=R1)
w.rds_error = "Throttling"
out = run(w)
(row,) = w.written
check(w.dialled == [CLUSTER_EP] and row["member_role"] == "writer", "one writer row, through the cluster endpoint")
check(row["member_instance_id"] == R1, f"tagged with the instance the server reports ({row['member_instance_id']}), "
      "not the stale stored writer")
w = World(cluster_record(network_method="lattice", network_route_mode="", engine="postgres"), writer=R1)
w.rds_error = "Throttling"
run(w)
check(w.written[0]["member_instance_id"] == t1.UNKNOWN_MEMBER,
      "an engine that cannot name itself: the row is marked unknown, not given the old id")
w = World(cluster_record(), writer=R1)
w.rds_error = "Throttling"
w.host_behaviour[EP[W]] = "fail"
run(w)
by_dial = [(r["member_instance_id"], r["member_role"]) for r in w.written]
check((W, "writer") not in by_dial and (R1, "writer") in by_dial,
      f"the writer's fallback through the cluster endpoint is not tagged with orders-1 ({by_dial})")
w = World(cluster_record(network_method="lattice", network_route_mode=""), writer=R1)
run(w)
check(w.written[0]["member_instance_id"] == R1 and stored(w)["writer_instance_id"] == R1,
      "with the sync working, the record moves and the row follows it")

print("== member sync failing on consecutive runs: logged once, shown, then cleared ==")
w = World(cluster_record())
w.rds_error = "Throttling"
run(w)
rec = stored(w)
check(rec["member_sync_failures"] == 1 and rec.get("member_sync_failing_since")
      and not any("Member sync failing" in x for x in rec.get("config_log") or []),
      "one failure: counted, not logged")
since = rec["member_sync_failing_since"]
install(w)
t1.handler({"instance_id": "db-orders"}, Ctx())
rec = stored(w)
lines = [x for x in rec.get("config_log") or [] if "Member sync failing since" in x]
check(rec["member_sync_failures"] == 2 and rec["member_sync_failing_since"] == since and len(lines) == 1
      and since in lines[0] and "Throttling" in lines[0], "the second in a row: one config_log line with the time")
install(w)
t1.handler({"instance_id": "db-orders"}, Ctx())
rec = stored(w)
check(rec["member_sync_failures"] == 3
      and len([x for x in rec["config_log"] if "Member sync failing since" in x]) == 1, "the third: no second line")
w.rds_error = ""
install(w)
t1.handler({"instance_id": "db-orders"}, Ctx())
rec = stored(w)
check("member_sync_failures" not in rec and "member_sync_failing_since" not in rec
      and rec["config_log"][-1].endswith("Member sync recovered"), "a good run clears it and says so")
n = len(w.instances.updates)
install(w)
t1.handler({"instance_id": "db-orders"}, Ctx())
check(len(w.instances.updates) == n, "and the next good run writes nothing")

# ══ host-route repair ══════════════════════════════════════════════════════════
PCX, CIDR = "pcx-ours", "10.0.0.0/16"


def host_record(**over):
    return cluster_record(network_route_mode="host", network_peering_id=PCX, db_vpc_cidr=CIDR,
                          vpc_id="vpc-db", host_routes=[{"ip": "10.0.1.1", "host": EP[W]},
                                                        {"ip": "10.0.1.2", "host": EP[R1]},
                                                        {"ip": "10.0.1.3", "host": EP[R2]}], **over)


def host_world(rec, dns_over=None):
    w = World(rec)
    w.dns = {CLUSTER_EP: ["10.0.1.1"], READER_EP: ["10.0.1.2"], EP[W]: ["10.0.1.1"],
             EP[R1]: ["10.0.1.2"], EP[R2]: ["10.0.1.3"]}
    w.dns.update(dns_over or {})
    w.ec2 = FakeEc2({CIDR: ("pcx-other", "active"), "10.0.1.1/32": (PCX, "active"),
                     "10.0.1.2/32": (PCX, "active"), "10.0.1.3/32": (PCX, "active")})
    return w


os.environ["AGENT_PRIVATE_ROUTE_TABLE_ID"] = "rtb-agent"
os.environ["AGENT_VPC_ID"] = "vpc-agent"

FLAG = "route_repair_needed"


def no_route_writes(w):
    return not [c for c in w.ec2.calls if c[0] in ("create", "replace", "delete")]


print("== a member's address changed: flagged for repair, the member skipped, no route touched ==")
w = host_world(host_record(), {EP[R2]: ["10.0.1.9"]})
out = run(w)
rec = stored(w)
flag = rec.get(FLAG) or {}
check(no_route_writes(w) and len(w.ec2.routes) == 4, "Tier 1 never writes a route")
check(flag.get("member") == EP[R2] and flag.get("address") == "10.0.1.9" and flag.get("detected_at")
      and flag.get("issue") == "unrouted", f"route_repair_needed names the member, the address and when ({flag})")
check(sorted(o["ip"] for o in rec["host_routes"]) == ["10.0.1.1", "10.0.1.2", "10.0.1.3"],
      "host_routes is not Tier 1's to change")
lines = [x for x in rec["config_log"] if "route repair needed" in x]
check(len(lines) == 1 and "10.0.1.9" in lines[0], "one config_log line")
check(EP[R2] not in w.dialled and out["members"][R2] == "skipped: not routed (10.0.1.9 has no owned host route)",
      "the member is skipped with the reason")
check(not w.audits, "detection is not audited (the repair is)")
n_updates = len(w.instances.updates)
install(w)
t1.handler({"instance_id": "db-orders"}, Ctx())
check(len(w.instances.updates) == n_updates and len([x for x in stored(w)["config_log"]
                                                      if "route repair needed" in x]) == 1,
      "the same drift on the next run: no second write, no second line")

print("== a departed member's /32: flagged as member gone ==")
w = host_world(host_record())
w.members = [W, R1]
run(w)
flag = stored(w).get(FLAG) or {}
check(no_route_writes(w) and {(i["address"], i["issue"]) for i in flag.get("items", [])} == {("10.0.1.3", "member gone")},
      f"the owned /32 whose member is gone is flagged, not deleted ({flag.get('items')})")

print("== an address outside the VPC is flagged too (the repair refuses it) ==")
w = host_world(host_record(), {EP[R2]: ["54.1.2.3"]})
run(w)
check(no_route_writes(w) and (stored(w).get(FLAG) or {}).get("address") == "54.1.2.3", "flagged, nothing written")

print("== the live /32 is checked, not only the recorded ownership ==")
for label, route, issue in (("missing", None, "route missing"),
                            ("blackholed", (PCX, "blackhole"), "route blackholed"),
                            ("repointed", ("pcx-elsewhere", "active"), "route points elsewhere")):
    w = host_world(host_record())
    if route is None:
        w.ec2.routes.pop("10.0.1.3/32")
    else:
        w.ec2.routes["10.0.1.3/32"] = route
    out = run(w)
    rec = stored(w)
    flag = rec.get(FLAG) or {}
    check(no_route_writes(w) and flag.get("member") == EP[R2] and flag.get("address") == "10.0.1.3"
          and flag.get("issue") == issue, f"an owned /32 {label} in the route table: flagged ({flag.get('items')})")
    check(EP[R2] not in w.dialled
          and out["members"][R2] == f"skipped: not routed (10.0.1.3/32: {issue}; route repair needed)",
          f"{label}: the member is skipped with the reason ({out['members'].get(R2)})")
    check(sorted(o["ip"] for o in rec["host_routes"]) == ["10.0.1.1", "10.0.1.2", "10.0.1.3"]
          and EP[R1] in w.dialled, f"{label}: ownership untouched, the routed reader still collected")

print("== a member name that fails to resolve: its /32 stays, no flag ==")
w = host_world(host_record())
w.dns.pop(EP[R2])
run(w)
check(no_route_writes(w) and FLAG not in stored(w) and not w.instances.updates, "no flag, no write")
w = host_world(host_record())
w.dns = {}
out = run(w)
check(no_route_writes(w) and FLAG not in stored(w) and CLUSTER_EP in w.dialled,
      "the resolver down for every name: no flag; the writer is still collected")

print("== a DNS failure never clears a pending flag or its refusal reason ==")
REFUSED_FLAG = {"member": EP[R2], "address": "10.0.1.9", "issue": "unrouted",
                "detected_at": "2030-01-01T00:00:00+00:00",
                "items": [{"member": EP[R2], "address": "10.0.1.9", "issue": "unrouted"}],
                "refused": "another database uses that address", "refused_at": "2030-01-01T00:05:00+00:00"}
for label, dns in (("the resolver down for every name", {}),
                   ("one member's name unresolvable", "drop R2")):
    w = host_world(host_record(route_repair_needed=dict(REFUSED_FLAG)))
    if dns == "drop R2":
        w.dns.pop(EP[R2])
    else:
        w.dns = dns
    run(w)
    check(stored(w).get(FLAG) == REFUSED_FLAG and not w.instances.updates and no_route_writes(w),
          f"{label}: the flag and its refusal reason are kept exactly, nothing written")

print("== nothing drifted: nothing written; drift gone: the flag is cleared ==")
w = host_world(host_record())
run(w)
check(no_route_writes(w) and not w.instances.updates and not w.audits, "no route call, no write, no audit")
w = host_world(host_record(route_repair_needed={"member": EP[R2], "address": "10.0.1.9", "issue": "unrouted",
                                                 "detected_at": "2030-01-01T00:00:00+00:00",
                                                 "items": [{"member": EP[R2], "address": "10.0.1.9",
                                                            "issue": "unrouted"}]}))
run(w)
check(FLAG not in stored(w) and stored(w)["config_log"][-1].endswith("all member addresses routed"),
      "the flag goes once every address is covered again, with a line")

print("== the VPC-wide route reaches it again: no drift ==")
w = host_world(host_record(), {EP[R2]: ["10.0.1.9"]})
w.ec2.routes[CIDR] = (PCX, "active")
run(w)
check(FLAG not in stored(w) and no_route_writes(w), "not flagged")

print("== a record being deleted is not checked ==")
w = host_world(host_record(status="deleting"), {EP[R2]: ["10.0.1.9"]})
run(w)
check(FLAG not in stored(w) and no_route_writes(w), "no flag on a deleting record")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
