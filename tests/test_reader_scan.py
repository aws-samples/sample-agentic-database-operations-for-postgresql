#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The Tier 2 job runs the writer's deep scan and then a reader scan on each reader.

Through the real ``batch/run_scan.main`` with fakes for everything outside it: a fake
``psycopg2`` (records the dialled host; answers the full-scan and reader queries by SQL
substring), a fake shell-script runner, fake S3 / DynamoDB / SNS steps and a fake
``metrics_store`` that captures every stored scan (summary + sections). Hermetic, no AWS.

Pinned: one ``full`` row tagged with the writer and two ``reader`` rows tagged with each
reader's id and observed role, carrying the same account/region/engine/display name as
the writer row (N4); a reader failure does not stop the next; over a Lattice resource
endpoint readers are skipped; a writer failure still runs the reader leg and then exits
1; ``derive_reader_findings`` bands (Aurora lag in ms, recovery conflicts, a long query
with hot_standby_feedback on) and the long query's literals are redacted.

Run:  python3 tests/test_reader_scan.py
"""
import json
import os
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
# backend/ first so `tools` is the source tree (the image stages a copy into batch/);
# batch/ for run_scan and its findings engine (`utils`).
sys.path.insert(0, str(ROOT / "batch"))
sys.path.insert(0, str(ROOT / "backend"))

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


import tools.aws  # noqa: E402
from tools.aws import host_routes  # noqa: E402
import run_scan  # noqa: E402

W, R1, R2 = "orders-1", "orders-2", "orders-3"
EP = {W: "orders-1.x.rds.amazonaws.com", R1: "orders-2.x.rds.amazonaws.com", R2: "orders-3.x.rds.amazonaws.com"}
CLUSTER_EP = "orders.cluster-x.rds.amazonaws.com"


def record(**over):
    rec = {"instance_id": "db-orders", "display_name": "orders", "account_id": "111111111111",
           "region": "us-east-1", "engine": "aurora-postgresql", "endpoint": CLUSTER_EP, "port": 5432,
           "database_name": "app", "secret_arn": "arn:aws:secretsmanager:us-east-1:111111111111:secret:ro",
           "arn": "arn:aws:rds:us-east-1:111111111111:cluster:orders", "network_method": "peering",
           "network_route_mode": "vpc", "writer_instance_id": W,
           "cluster_member_roles": [{"instance_id": m, "is_writer": m == W, "endpoint": EP[m]} for m in (W, R1, R2)]}
    rec.update(over)
    return rec


class Cursor:
    def __init__(self, conn):
        self.conn, self.sql = conn, ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, *a):
        self.sql = sql

    def fetchone(self):
        s, reader = self.sql, self.conn.reader
        if "aurora_db_instance_identifier() AS server_id" in s:
            if self.conn.server_id is None:
                raise RuntimeError("function aurora_db_instance_identifier() does not exist")
            return {"server_id": self.conn.server_id}
        if "pg_is_in_recovery() AS in_recovery, version()" in s:
            return {"in_recovery": reader, "version": "PostgreSQL 16", "max_connections": "100"}
        if "aurora_replica_status" in s:
            return {"replica_lag_in_msec": 6500}
        if "pg_stat_database_conflicts" in s:
            return {"confl_tablespace": 0, "confl_lock": 1, "confl_snapshot": 3, "confl_bufferpin": 0,
                    "confl_deadlock": 0}
        if "pg_database_size(current_database())" in s:
            return {"size_mb": 2048.0}
        if "FROM pg_stat_activity WHERE pid != pg_backend_pid()" in s and "total_connections" in s:
            return {"total_connections": 12, "active_queries": 2, "waiting_queries": 0, "idle_in_txn": 0,
                    "idle_connections": 10, "connection_utilization_pct": 12.0}
        # the full scan's single-row queries
        return {"version": "PostgreSQL 16", "max_connections": "100", "in_recovery": reader, "cnt": 0,
                "start_time": "2030-01-01"}

    def fetchall(self):
        s = self.sql
        if "hot_standby_feedback" in s:
            return [{"name": "hot_standby_feedback", "setting": "on", "unit": None}]
        if "interval '60 seconds'" in s:
            return [{"pid": 7, "duration_sec": 4000, "state": "active", "wait_event_type": None,
                     "wait_event": None, "xmin_age": 90000, "query": "SELECT * FROM t WHERE email = 'a@b.example'"}]
        if "blks_hit" in s:
            return [{"database": "app", "hit_ratio": 99.5}]
        return []


class Conn:
    def __init__(self, host, reader):
        self.host, self.reader = host, reader

    def set_session(self, **kw):
        pass

    def cursor(self, cursor_factory=None):
        return Cursor(self)

    def close(self):
        pass


class World:
    def __init__(self, rec):
        self.rec = rec
        self.dialled, self.rows, self.dynamo, self.sns = [], [], [], []
        self.fail_hosts = set()
        self.answers_writer = set()   # reader hosts that report pg_is_in_recovery() = false
        self.shell_fails = False
        # RDS now: the live writer and members (None: as the record says), or an error.
        self.live_writer, self.live_members, self.rds_error = None, None, ""
        self.outcome_writes = []      # reader_scan_outcomes writes to the record, in order
        self.outcomes = {}            # reader_scan_outcomes as the record holds it after them
        self.on_outcome = None        # called after each per-member write (a job kill)
        self.during_scan = None       # called while the full scan runs (a failover mid-scan)
        self.server_names = True      # the server answers aurora_db_instance_identifier()
        self.scan_errors = []         # last_scan_error writes (record_scan_failure)
        self.store_fails = False      # the metrics store refuses every write
        self.extract_fails = False    # structured extraction raises

    def update_item(self, **kw):
        if kw["UpdateExpression"] == "SET last_scan_error = :e":
            self.scan_errors.append(kw["ExpressionAttributeValues"][":e"])
            return
        self.outcome_writes.append(kw)
        expr, val = kw["UpdateExpression"], kw["ExpressionAttributeValues"][":o"]
        if expr == "SET reader_scan_outcomes = :o":
            self.outcomes = dict(val)
        elif expr == "SET reader_scan_outcomes.#m = :o":
            self.outcomes[kw["ExpressionAttributeNames"]["#m"]] = val
            if self.on_outcome:
                self.on_outcome()
        else:
            raise AssertionError(f"unmodelled write {expr}")

    def describe_db_clusters(self, DBClusterIdentifier):
        if self.rds_error:
            raise RuntimeError(self.rds_error)
        roles = self.rec.get("cluster_member_roles") or []
        writer = self.live_writer or next((m["instance_id"] for m in roles if m.get("is_writer")), "")
        ids = self.live_members or [m["instance_id"] for m in roles]
        return {"DBClusters": [{"DBClusterMembers": [{"DBInstanceIdentifier": i, "IsClusterWriter": i == writer}
                                                     for i in ids]}]}

    def connect(self, **kw):
        host = kw["host"]
        self.dialled.append(host)
        if host in self.fail_hosts:
            raise RuntimeError(f"connection to {host} timed out")
        writer_now = self.live_writer or W
        conn = Conn(host, reader=host in EP.values() and host != EP[writer_now]
                    and host not in self.answers_writer)
        # The instance the connection reached: the cluster endpoint follows the live writer.
        by_ep = {ep: m for m, ep in EP.items()}
        conn.server_id = (by_ep.get(host) or writer_now) if self.server_names else None
        return conn


def install(world):
    pg = types.ModuleType("psycopg2")
    pg.connect = world.connect
    pg.extras = types.SimpleNamespace(RealDictCursor=object)
    sys.modules["psycopg2"] = pg
    sys.modules["psycopg2.extras"] = pg.extras
    store = types.ModuleType("tools.aws.metrics_store")

    def put_tier2_scan(instance_id, summary, sections):
        if world.store_fails:
            raise RuntimeError("could not reach store.example: AccessDeniedException")
        # One captured "row" per scan, shaped like the old scan_results row so the
        # assertions read the same: the summary fields, the findings and (as `data`) the
        # stored sections — the reader scan's data is its one `reader` section.
        row = dict(summary, instance_id=instance_id)
        row["findings"] = json.dumps(summary.get("findings") or [], default=str)
        row["data"] = json.dumps(sections.get("reader") if "reader" in sections else sections, default=str)
        row["sections"] = sorted(sections)
        world.rows.append(row)
        return f"{instance_id}|{summary.get('scan_time')}|{summary.get('member_instance_id')}"

    store.put_tier2_scan = put_tier2_scan
    store.publish_tier2_metrics = lambda *a, **k: world.cw.append(a) or 1
    store.is_configured = lambda: bool(os.getenv("TIER1_METRICS_TABLE") and os.getenv("TIER2_SCANS_TABLE"))
    world.cw = []
    tools.aws.metrics_store = store
    sys.modules["tools.aws.metrics_store"] = store
    run_scan.ssl_kwargs = lambda: {}
    run_scan.get_instance_info = lambda iid: dict(world.rec)
    run_scan.get_db_credentials = lambda arn, external_id="": {"host": CLUSTER_EP, "username": "ro",
                                                                "password": "x", "dbname": "app"}

    def shell(creds, info, database, display):
        if world.shell_fails:
            raise RuntimeError("psql: could not connect")
        if world.during_scan:
            world.during_scan()
        return "/tmp/report.html"
    run_scan.run_shell_script = shell
    if not hasattr(run_scan, "_real_extract"):
        run_scan._real_extract = run_scan.extract_structured_data

    def extract(*a, **k):
        if world.extract_fails:
            raise RuntimeError("permission denied for view pg_stat_statements")
        return run_scan._real_extract(*a, **k)
    run_scan.extract_structured_data = extract
    run_scan.analyze_report_with_llm = lambda *a, **k: {"health_score": "ok", "summary": "", "findings": []}
    world.uploads = []
    run_scan.upload_to_s3 = lambda *a, **k: world.uploads.append(a) or "111111111111/db-orders/2030-01-01/r/report.html"
    run_scan.update_dynamodb = lambda iid, score="scanned": world.dynamo.append((iid, score))
    run_scan.send_sns_notification = lambda *a, **k: world.sns.append(a)
    run_scan._reach_rds = lambda info: world
    table = types.SimpleNamespace(update_item=world.update_item)
    run_scan.boto3 = types.SimpleNamespace(resource=lambda *a, **k: types.SimpleNamespace(Table=lambda n: table))
    host_routes.resolve = lambda h: []
    os.environ.update({"INSTANCE_ID": "db-orders", "ACCOUNT_ID": "111111111111", "AWS_REGION": "us-east-1",
                       "ENGINE": "aurora-postgresql", "DISPLAY_NAME": "orders", "DATABASE_NAME": "app",
                       "TIER1_METRICS_TABLE": "dbops-tier1-metrics", "TIER2_SCANS_TABLE": "dbops-tier2-scans"})
    os.environ.pop("SECRET_ARN", None)


def run(world):
    install(world)
    try:
        run_scan.main()
        return 0
    except SystemExit as e:
        return e.code


print("== one full row for the writer, one reader row per reader ==")
w = World(record())
code = run(w)
by = {(r["member_instance_id"], r["scan_kind"]): r for r in w.rows}
check(code == 0 and set(by) == {(W, "full"), (R1, "reader"), (R2, "reader")}, f"three rows ({sorted(by)})")
full, r1 = by[(W, "full")], by[(R1, "reader")]
check(full["member_role"] == "writer" and r1["member_role"] == "reader" and by[(R2, "reader")]["member_role"] == "reader",
      "observed roles")
check(EP[R1] in w.dialled and EP[R2] in w.dialled, "readers dialled on their instance endpoints")
for k in ("account_id", "region", "engine", "display_name", "instance_id"):
    check(r1[k] == full[k], f"reader row {k} matches the writer row ({r1[k]!r})")
check(not r1.get("report_s3_key") and full["report_s3_key"].endswith("report.html"), "no report object for a reader")
check(r1["cache_hit_ratio"] == 99.5 and r1["total_connections"] == 12 and r1["database_size_mb"] == 2048.0,
      "reader top-level metric columns are filled")
data = json.loads(r1["data"])
check(data["replication"]["replica_lag_ms"] == 6500 and data["recovery_conflicts"]["confl_snapshot"] == 3,
      "the reader data shape")
check("a@b.example" not in r1["data"], "long-query literals are redacted")
findings = json.loads(r1["findings"])
check(findings and all(f["member_instance_id"] == R1 and f["member_role"] == "reader" for f in findings),
      "reader findings carry the member id and role")
check(all(f["member_instance_id"] == W and f["member_role"] == "writer" for f in json.loads(full["findings"])),
      "full-scan findings carry the writer id and role")
check(w.dynamo == [("db-orders", full["health_score"])] and len(w.sns) == 1,
      "last_health_score and the notification are the writer's only")

print("== a reader failure does not stop the next ==")
w = World(record())
w.fail_hosts = {EP[R1]}
code = run(w)
kinds = sorted((r["member_instance_id"], r["scan_kind"]) for r in w.rows)
check(code == 0 and kinds == [(W, "full"), (R2, "reader")], f"the other reader is written ({kinds})")

print("== over a Lattice resource endpoint readers are skipped ==")
w = World(record(network_method="lattice", network_route_mode=""))
code = run(w)
check(code == 0 and [r["scan_kind"] for r in w.rows] == ["full"], "only the full row")
check(EP[R1] not in w.dialled and EP[R2] not in w.dialled, "no reader is dialled")

print("== host routes: an unrouted reader is skipped ==")
w = World(record(network_route_mode="host", host_routes=[{"ip": "10.0.1.2", "host": EP[R1]}]))
install(w)
host_routes.resolve = lambda h: {EP[R1]: ["10.0.1.2"], EP[R2]: ["10.0.1.3"]}.get(h, [])
try:
    run_scan.main()
except SystemExit:
    pass
check(sorted(r["member_instance_id"] for r in w.rows if r["scan_kind"] == "reader") == [R1],
      "only the routed reader is scanned")

print("== a writer failure still runs the reader leg, then exits 1 ==")
w = World(record())
w.shell_fails = True
code = run(w)
check(code == 1, f"exit 1 (got {code})")
check(sorted(r["member_instance_id"] for r in w.rows) == [R1, R2] and all(r["scan_kind"] == "reader" for r in w.rows),
      "both reader rows written before exiting")
check(not w.dynamo and not w.sns, "no writer status or notification")

print("== the writer's row could not be stored: the scan fails, nobody is told it completed ==")
w = World(record())
w.store_fails = True
os.environ["AWS_BATCH_JOB_ID"] = "job-123"
code = run(w)
check(code == 1, f"exit 1, so Batch marks the job FAILED (got {code})")
check(not w.dynamo, "no health stamp on the record")
check(not w.sns, "no scan-completed notification")
check(len(w.scan_errors) == 1 and w.scan_errors[0]["job_id"] == "job-123"
      and "could not be saved to the metrics store" in w.scan_errors[0]["reason"],
      f"the reason is recorded against this job ({w.scan_errors})")
check("store.example" not in w.scan_errors[0]["reason"] and "PAM" not in w.scan_errors[0]["reason"],
      "the recorded reason carries no host or driver text")

print("== structured extraction failed: no row is possible, so the scan fails before any upload ==")
w = World(record())
w.extract_fails = True
code = run(w)
check(code == 1 and not w.dynamo and not w.sns, "exit 1, no health stamp, no notification")
check(not w.uploads, "no report uploaded for a scan with no stored result")
check(w.scan_errors and "could not be read back" in w.scan_errors[0]["reason"], "the reason says what happened")

print("== an unexpected failure records only its class ==")
w = World(record())
w.shell_fails = True
code = run(w)
check(code == 1 and w.scan_errors and "RuntimeError" in w.scan_errors[0]["reason"]
      and "psql" not in w.scan_errors[0]["reason"], f"class only ({w.scan_errors})")

print("== no metrics store configured: the S3 report is the result, as before ==")
w = World(record())
install(w)
os.environ.pop("TIER1_METRICS_TABLE")
os.environ.pop("TIER2_SCANS_TABLE")
try:
    run_scan.main()
    code = 0
except SystemExit as e:
    code = e.code
check(code == 0 and w.dynamo and len(w.sns) == 1 and not w.scan_errors, "success, notified, no failure recorded")
os.environ.pop("AWS_BATCH_JOB_ID", None)

print("== a reader that answers as the writer is not stored as writer + reader ==")
w = World(record())
w.answers_writer = {EP[R1]}
code = run(w)
pairs = sorted((r["member_role"], r["scan_kind"]) for r in w.rows)
check(("writer", "reader") not in pairs, f"no row pairs member_role=writer with scan_kind=reader ({pairs})")
check(sorted(r["member_instance_id"] for r in w.rows if r["scan_kind"] == "reader") == [R2],
      "the other reader is still stored, with its real kind")
check(run_scan.write_to_store("db-orders", {}, "ok", "", [], "", member_instance_id=R1,
                               member_role="writer", scan_kind="reader") is False, "the write itself refuses it")

print("== roles change between the stored record and the job: the live members are used ==")
w = World(record())          # the record says orders-1 is the writer
w.live_writer = R1           # RDS says orders-2 is, after a failover Tier 1 has not seen yet
code = run(w)
by = {(r["member_instance_id"], r["scan_kind"]): r for r in w.rows}
check(code == 0 and (R1, "full") in by and (W, "full") not in by,
      f"the full scan's row is tagged with the live writer ({sorted(by)})")
check(by.get((R1, "full"), {}).get("member_role") == "writer", "and its observed role is writer")
check(sorted(m for m, k in by if k == "reader") == [W, R2] and by[(W, "reader")]["member_role"] == "reader",
      "the reader fan-out covers the old writer (now a reader) and skips the new writer")
print("== a failover DURING the full scan: the row is tagged with the writer at write time ==")


def fail_over_to(world, member):
    def go():
        world.live_writer = member
    return go


w = World(record())
w.during_scan = fail_over_to(w, R2)
run(w)
full = [r for r in w.rows if r["scan_kind"] == "full"]
check([r["member_instance_id"] for r in full] == [R2],
      f"Aurora: the server names the instance behind the cluster endpoint at write time ({[r['member_instance_id'] for r in full]})")
w = World(record(engine="postgres"))
w.during_scan = fail_over_to(w, R2)
run(w)
full = [r for r in w.rows if r["scan_kind"] == "full"]
check([r["member_instance_id"] for r in full] == [R2],
      "an RDS Multi-AZ DB cluster (cannot name itself): the writer RDS reports at write time")
w = World(record())
w.server_names = False
w.during_scan = fail_over_to(w, R2)
run(w)
check([r["member_instance_id"] for r in w.rows if r["scan_kind"] == "full"] == [R2],
      "Aurora with the server's name unreadable: the writer RDS reports at write time")
w = World(record())
w.server_names = False


def moved_and_describe_fails():
    w.live_writer, w.rds_error = R2, "Throttling"


w.during_scan = moved_and_describe_fails
run(w)
check([r["member_instance_id"] for r in w.rows if r["scan_kind"] == "full"] == [W],
      "neither readable: the writer known at job start, as before")

w = World(record())
w.live_writer, w.rds_error = R1, "Throttling"
run(w)
check(any(k == "full" for _m, k in ((r["member_instance_id"], r["scan_kind"]) for r in w.rows)),
      "the describe failing: the scan still runs on the record's roles")
w = World(record())
w.live_members = [W, R1, R2, "orders-4"]
run(w)
outs = w.outcomes
check(outs["orders-4"]["outcome"] == "skipped: no instance endpoint",
      "a live member the record does not know yet is reported as skipped, not dropped")

print("== per-member outcomes of the reader leg, recorded on the record ==")
w = World(record(cluster_member_roles=[{"instance_id": W, "is_writer": True, "endpoint": EP[W]},
                                       {"instance_id": R1, "is_writer": False, "endpoint": EP[R1]},
                                       {"instance_id": R2, "is_writer": False, "endpoint": ""}]))
w.fail_hosts = set()
os.environ["AWS_BATCH_JOB_ID"] = "job-123"
run(w)
outs = w.outcomes
first, rest = w.outcome_writes[0], w.outcome_writes[1:]
check(first["Key"] == {"instance_id": "db-orders"} and first["UpdateExpression"] == "SET reader_scan_outcomes = :o"
      and first["ConditionExpression"] == "attribute_exists(instance_id)"
      and {m: o["outcome"] for m, o in first["ExpressionAttributeValues"][":o"].items()}
      == {R1: run_scan.NOT_REACHED, R2: run_scan.NOT_REACHED},
      "the leg starts by marking every reader not reached, on the record")
check([x["ExpressionAttributeNames"]["#m"] for x in rest] == [R1, R2]
      and all(x["UpdateExpression"] == "SET reader_scan_outcomes.#m = :o"
              and x["ConditionExpression"] == "attribute_exists(instance_id)" for x in rest),
      "then one write per reader, as each outcome is known")
check(outs[R1]["outcome"] == "reached" and outs[R2]["outcome"] == "skipped: no instance endpoint"
      and outs[R1]["at"] and outs[R1]["job_id"] == "job-123",
      "reached, and a reader with no endpoint is skipped with the reason (#18), with the job id")
w = World(record())
w.fail_hosts = {EP[R1]}
run(w)
outs = w.outcomes
check(outs[R1]["outcome"] == "failed: RuntimeError" and outs[R2]["outcome"] == "reached", "a failure is its own outcome")
w = World(record(network_method="lattice", network_route_mode=""))
run(w)
outs = w.outcomes
check(outs[R1]["outcome"] == host_routes.LATTICE_SKIP, "a Lattice reader is skipped with the reason")

print("== a job killed mid-leg: the readers it reached, and not reached for the rest ==")


class Killed(BaseException):
    """Batch ending the container at the attempt timeout (nothing after it runs)."""


w = World(record(cluster_member_roles=record()["cluster_member_roles"]
                 + [{"instance_id": "orders-4", "is_writer": False, "endpoint": "orders-4.x.rds.amazonaws.com"}]))
w.rec["reader_scan_outcomes"] = {R1: {"outcome": "reached", "at": "2029-12-31T00:00:00+00:00", "job_id": "job-old"}}
w.outcomes = dict(w.rec["reader_scan_outcomes"])
os.environ["AWS_BATCH_JOB_ID"] = "job-456"


def kill_after_first():
    raise Killed()


w.on_outcome = kill_after_first
try:
    run(w)
    check(False, "the kill ends the job")
except Killed:
    pass
outs = w.outcomes
check(outs[R1]["outcome"] == "reached" and outs[R1]["job_id"] == "job-456",
      "the reader it reached shows this job's outcome")
check(outs[R2]["outcome"] == run_scan.NOT_REACHED and outs["orders-4"]["outcome"] == run_scan.NOT_REACHED
      and {outs[m]["job_id"] for m in (R2, "orders-4")} == {"job-456"} and outs[R2]["at"],
      "the rest say not reached (job ended), with this job's id and time, not an earlier job's outcome")
os.environ.pop("AWS_BATCH_JOB_ID", None)

print("== the reader leg's time budget ==")
check(run_scan.JOB_TIMEOUT_S == 1800 and "AttemptDurationSeconds: 1800" in
      (ROOT / "backend/template.yaml").read_text(encoding="utf-8"), "JOB_TIMEOUT_S is the job's attempt timeout")
w = World(record())
install(w)
t = {"now": 0.0}


def slow_clock():
    # Each reader scan takes 900 s of the job's 1800 s.
    t["now"] += 900.0
    return t["now"]


creds = {"host": CLUSTER_EP, "username": "ro", "password": "x"}
outs = run_scan.run_reader_leg(creds, dict(w.rec, cluster_member_roles=w.rec["cluster_member_roles"]
                                           + [{"instance_id": "orders-4", "is_writer": False,
                                               "endpoint": "orders-4.x.rds.amazonaws.com"}]),
                               "arn:secret", job_started=0.0, clock=slow_clock)
check(outs[R1] == "reached" and outs[R2] == run_scan.OUT_OF_BUDGET and outs["orders-4"] == run_scan.OUT_OF_BUDGET,
      f"a reader that would not finish before the budget ends is not started ({outs})")
check([r["member_instance_id"] for r in w.rows] == [R1], "and has no row")
outs = run_scan.run_reader_leg(creds, w.rec, "arn:secret", job_started=0.0,
                               clock=lambda: run_scan.JOB_TIMEOUT_S - run_scan.BUDGET_MARGIN_S - 1)
check(set(outs.values()) == {run_scan.OUT_OF_BUDGET}, "with the budget spent, every reader is out of budget")

print("== a single instance writes one full row tagged with its ARN segment ==")
single = {k: v for k, v in record().items() if k not in ("cluster_member_roles", "writer_instance_id", "arn")}
single["tags"] = {"arn": "arn:aws:rds:us-east-1:111111111111:db:inventory"}
w = World(single)
run(w)
check([(r["member_instance_id"], r["scan_kind"]) for r in w.rows] == [("inventory", "full")], "one full row")

print("== an idle primary: a caught-up RDS standby reports no lag, no CRITICAL ==")


class StandbyCursor:
    """A PostgreSQL standby's replay functions: the LSNs it received and replayed, and the
    age of the last replayed commit. Evaluates the CASE the reader query uses."""

    def __init__(self, receive, replay, last_commit_age_s, receiver="streaming"):
        # receiver: the pg_stat_wal_receiver row's status, "hidden" (a row whose status
        # reads NULL without pg_read_all_stats), or None (no receiver: it is down).
        self.receive, self.replay, self.age, self.sql = receive, replay, last_commit_age_s, ""
        self.receiver = receiver

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, *a):
        self.sql = sql

    def fetchone(self):
        s = self.sql
        if "pg_is_in_recovery() AS in_recovery" in s:
            return {"in_recovery": True, "version": "PostgreSQL 16", "max_connections": "100"}
        status = None if self.receiver in (None, "hidden") else self.receiver
        receiving = self.receiver is not None and status in (None, "streaming")
        if "pg_last_xact_replay_timestamp" in s:
            same = self.receive == self.replay
            if "CASE WHEN pg_last_wal_receive_lsn() = pg_last_wal_replay_lsn()" in s and same:
                gated = "pg_stat_wal_receiver WHERE status IS NULL OR status = 'streaming'" in s
                delay = 0 if (receiving or not gated) else None
            else:
                delay = self.age
            return {"replay_delay_s": delay, "replay_lag_bytes": 0 if same else 4096}
        if "AS receivers" in s:
            return {"receivers": 0 if self.receiver is None else 1, "status": status}
        return None

    def fetchall(self):
        return []


class StandbyConn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self, cursor_factory=None):
        return self.cur


install(World(record()))
idle = run_scan.extract_reader_data(StandbyConn(StandbyCursor("0/5000", "0/5000", 3600.0)), "postgres")
from utils.findings_engine import derive_reader_findings  # noqa: E402
health, _, fs = derive_reader_findings(idle)
check(idle["replication"]["replay_delay_s"] == 0, "caught up (received == replayed): replay delay 0, not 3600 s")
check(health != "critical" and not any(f["severity"] == "CRITICAL" for f in fs),
      f"no CRITICAL on an idle writer ({health})")
behind = run_scan.extract_reader_data(StandbyConn(StandbyCursor("0/9000", "0/5000", 45.0)), "postgres")
health, _, fs = derive_reader_findings(behind)
check(behind["replication"]["replay_delay_s"] == 45.0 and health == "critical",
      "WAL outstanding: the last commit's age is the lag (45 s is critical)")
check(idle["replication"]["wal_receiving"] is True
      and not any("not receiving WAL" in f["title"] for f in derive_reader_findings(idle)[2]),
      "caught up and streaming: receiving, no receiver finding")
hidden = run_scan.extract_reader_data(StandbyConn(StandbyCursor("0/5000", "0/5000", 3600.0, receiver="hidden")),
                                      "postgres")
check(hidden["replication"]["replay_delay_s"] == 0 and hidden["replication"]["wal_receiving"] is True
      and not any("not receiving WAL" in f["title"] for f in derive_reader_findings(hidden)[2]),
      "a receiver row whose status the login cannot read counts as connected: 0, no finding")
down = run_scan.extract_reader_data(StandbyConn(StandbyCursor("0/5000", "0/5000", 3600.0, receiver=None)), "postgres")
health, _, fs = derive_reader_findings(down)
check(down["replication"]["replay_delay_s"] is None and down["replication"]["wal_receiving"] is False,
      "caught up but no WAL receiver: the delay is unknown (NULL), not 0")
check([f["title"] for f in fs if "WAL" in f["title"]] == ["Standby not receiving WAL (no WAL receiver)"]
      and next(f for f in fs if "WAL" in f["title"])["severity"] == "WARNING",
      "and a finding says the standby is not receiving WAL")
waiting = run_scan.extract_reader_data(StandbyConn(StandbyCursor("0/5000", "0/5000", 3600.0, receiver="waiting")),
                                       "postgres")
check(waiting["replication"]["replay_delay_s"] is None
      and any(f["title"] == "Standby not receiving WAL (WAL receiver waiting)" for f in derive_reader_findings(waiting)[2]),
      "a receiver that is not streaming: unknown delay, and the finding names its status")

print("== derive_reader_findings bands ==")
from utils.findings_engine import derive_reader_findings  # noqa: E402

health, summary, fs = derive_reader_findings({
    "replication": {"replica_lag_ms": 6500}, "recovery_conflicts": {"confl_snapshot": 2},
    "standby_settings": [{"name": "hot_standby_feedback", "setting": "on"}],
    "long_queries": [{"duration_sec": 4000}], "cache_ratios": [{"hit_ratio": 99.9}],
    "activity": {"connection_utilization_pct": 10}})
titles = " | ".join(f["title"] for f in fs)
check(health == "warning", f"warning overall ({health})")
check("Replica lag 6.5s" in titles, "Aurora milliseconds are judged in seconds (6.5 s >= 5 s)")
check("2 queries cancelled by recovery conflicts" in titles, "recovery conflicts are a warning")
check("hot_standby_feedback on" in titles, "a long query with feedback on is flagged for the writer's vacuum")
health, _, fs = derive_reader_findings({"replication": {"replay_delay_s": 45.0}})
check(health == "critical" and fs[0]["severity"] == "CRITICAL", "45 s replay delay is critical")
health, _, fs = derive_reader_findings({"replication": {"replica_lag_ms": 20}, "recovery_conflicts": {},
                                        "long_queries": [{"duration_sec": 70}]})
check(health == "ok" and not fs, "a healthy reader has no findings")
health, _, fs = derive_reader_findings({"long_queries": [{"duration_sec": 4000}],
                                        "standby_settings": [{"name": "hot_standby_feedback", "setting": "off"}]})
check(fs and fs[0]["category"] == "Queries", "with feedback off, a long query is a query finding")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
