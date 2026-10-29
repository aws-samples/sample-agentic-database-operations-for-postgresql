# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 1 Lightweight Metrics Collector — Lambda handler.

Runs every few minutes per monitored database. Connects to the target PostgreSQL
instance, runs fast queries against system catalogs (shared memory — no table scans),
and writes one row per MEMBER to the Aurora metrics store: for a DB cluster (Aurora or
an RDS Multi-AZ DB cluster) the writer and each reader, dialled on their own instance
endpoints; for a single instance, the instance. Each row carries the member's id and
its observed role (``pg_is_in_recovery``) and, for a reader, its replica lag.

Before collecting, a cluster's members are re-read from RDS (``sync_members``): when
the writer moved (a failover or switchover) the record's roles are updated, the event
is audited (``cluster_failover_detected``, actor ``system``) and a notice goes to the
notification topic, exactly once. On the cross-Region CIDR-clash path the member
``/32`` host routes are then checked (``check_host_routes``): drift flags the record
``route_repair_needed`` for the API task to repair (``tools.aws.route_repair``). This
collector holds no route-write permission.

The rows go to the metrics store (``tools.aws.metrics_store``): one DynamoDB item per
member (every numeric plus the bloat and wait-event lists, TTL-expired after the
database's retention), and every numeric to CloudWatch as a custom metric, which keeps
the 15-month time series the charts, the fleet views, the agent and the alarms read.
Credentials for the monitored instance come from the Secrets Manager ARN on that
instance's registry record, not from the environment.

Invoked by EventBridge Scheduler with: {"instance_id": "xxx", "scheduled_time": "<the run's
scheduled time>"}; the scheduled time (to the minute) is the rows' scan_time (``slot_time``).

Environment variables:
  DB_CONFIG_TABLE               — DynamoDB table for instance registry
  TIER1_METRICS_TABLE           — DynamoDB table for Tier 1 rows
  TIER2_SCANS_TABLE             — DynamoDB table for Tier 2 scans (read for completeness)
  CW_METRICS_NAMESPACE          — CloudWatch namespace (default: <prefix>/Metrics)
  AWS_REGION                    — AWS region
  NOTIFICATION_TOPIC_ARN        — where a detected failover is announced (optional)
  AGENT_PRIVATE_ROUTE_TABLE_ID  — the agent route table the host-route check reads
  AUDIT_*                       — the audit vault switch, bucket and retention floor
"""

import concurrent.futures
import json
import logging
import os
from datetime import datetime, timezone

import boto3
import psycopg2
import psycopg2.extras
from tools.resource_names import table_name as resolve_table_name
from tools.xid import XID_WRAPAROUND_MAX
from tools.aws.db_tls import ssl_kwargs

logger = logging.getLogger("tier1-collector")
logger.setLevel(logging.INFO)

# ── Cached clients ──
_dynamodb = None


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1"))
    return _dynamodb


# ─────────────────────────────────────────────
# Database record + credential resolution
# ─────────────────────────────────────────────

def _get_instance_record(instance_id: str) -> dict:
    """Fetch instance record from DynamoDB."""
    table_name = resolve_table_name("instances")
    table = _get_dynamodb().Table(table_name)
    resp = table.get_item(Key={"instance_id": instance_id})
    return resp.get("Item", {})


def _region() -> str:
    return os.getenv("AWS_REGION", "us-east-1")


def record_arn(record: dict) -> str:
    """The database's RDS ARN: the record's ``arn``, else ``tags.arn`` (older discovery
    rows carry it only there). Same rule as app.py."""
    return record.get("arn") or (record.get("tags") or {}).get("arn", "")


def _is_cluster(record: dict) -> bool:
    return ":cluster:" in record_arn(record)


def _reach_session(cross_account_role: str = "", owner_admin: str = ""):
    """The session that reaches the database's account: the reach provider's role,
    assumed with ``sts:ExternalId`` = ``owner_admin``, or this function's own session
    for a database in the deployment account. Secrets Manager (the credential) and RDS
    (the member sync) use it.

    ``owner_admin`` is the id of the admin who owns the database's reach-provider space.
    The roles this assumes carry a trust policy requiring that admin's id (one role per
    admin serves all of their spaces), so omitting it would be refused — and this
    collector runs unattended, where an opaque AccessDenied is the hardest kind of
    failure to trace. Duplicated rather than imported from ``session_factory``: this
    module ships as a Lambda zip with a curated file set that does not include it.
    """
    region = _region()
    if cross_account_role:
        if not owner_admin:
            raise ValueError(
                f"Cannot assume {cross_account_role}: no owner admin on the record, so no "
                "sts:ExternalId to present. The target role's trust policy requires the "
                "id of the admin who owns the reach-provider space."
            )
        sts = boto3.client("sts", region_name=region)
        assumed = sts.assume_role(
            RoleArn=cross_account_role,
            RoleSessionName="tier1-collector",
            DurationSeconds=900,
            ExternalId=owner_admin,
        )
        creds = assumed["Credentials"]
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=region,
        )
    return boto3.Session(region_name=region)


def _get_db_credentials(session, secret_arn: str) -> dict:
    """Fetch target DB credentials from Secrets Manager through the reach session."""
    secret_region = _region()
    if secret_arn.startswith("arn:"):
        parts = secret_arn.split(":")
        if len(parts) >= 4:
            secret_region = parts[3]
    client = session.client("secretsmanager", region_name=secret_region)
    resp = client.get_secret_value(SecretId=secret_arn)
    return json.loads(resp["SecretString"])


def _resolve_cross_account_role(record: dict) -> str:
    """Resolve cross-account role ARN from the reach provider's space config."""
    account_id = record.get("account_id", "")
    # DB-level tenancy: reach is provided by the DB's reach_space_id, whose owner admin's
    # role scans the DB.
    space_id = record.get("reach_space_id", "")
    if not account_id or not space_id:
        return ""

    try:
        spaces_table = _get_dynamodb().Table(resolve_table_name("spaces"))
        space = spaces_table.get_item(Key={"space_id": space_id}).get("Item", {})
        primary_id = space.get("primary_account_id", "")
        if account_id != primary_id:
            role_names = space.get("secondary_role_names") or {}
            role_name = role_names.get(account_id, "")
            if role_name:
                return f"arn:aws:iam::{account_id}:role/{role_name}"
    except Exception as e:
        logger.warning(f"Failed to resolve cross-account role: {e}")
    return ""


# ─────────────────────────────────────────────
# PostgreSQL metric queries
# ─────────────────────────────────────────────

def _connect_to_target(creds: dict, record: dict,
                       dial_host: str = "") -> psycopg2.extensions.connection:
    """Connect to the target PostgreSQL instance. `dial_host` is a cluster member's own
    instance endpoint (from the registry, which RDS filled; never request input): the
    secret's host is still checked against the RECORD's endpoint below, and the
    connection only goes to that member instead of the cluster endpoint."""
    endpoint = record.get("endpoint", "") or ""
    secret_host = creds.get("host") or ""
    # HOST PRECEDENCE (identical in pg_client, run_scan, tier1_collector and the
    # Configure check - see tests/test_host_precedence.py):
    #   1. the instance's endpoint - read from the RDS API by discovery, so it is
    #      authoritative for WHICH database this record refers to; over a Lattice
    #      resource endpoint this same name resolves to the endpoint inside the agent VPC
    #   2. the secret's host - hand-written, so only a last resort when the record
    #      carries no endpoint
    # The instance endpoint is authoritative for which database this record refers to,
    # so a secret whose host disagrees is refused rather than followed. Two databases
    # sharing a read-only password must not let one silently redirect the other's
    # traffic and report on the wrong database.
    # An RDS read replica shares its source's read-only user: a secret naming the source
    # (tags.replica_source_endpoint, recorded from RDS) is accepted; we dial the replica.
    tags = record.get("tags") or {}
    allowed = {endpoint}
    if tags.get("replica_of") and tags.get("replica_source_endpoint"):
        allowed.add(tags["replica_source_endpoint"])
    if endpoint and secret_host and secret_host not in allowed:
        raise ValueError(
            f"secret host {secret_host} is not this instance's endpoint {endpoint}; "
            f"the secret belongs to a different database"
        )
    host = endpoint or secret_host
    if dial_host:
        host = dial_host
    # PORT: the record's port (from RDS) wins; the secret's port is only a fallback.
    port = int(record.get("port") or creds.get("port") or 5432)
    dbname = record.get("database_name") or creds.get("dbname", "postgres")
    user = creds.get("username", "")
    password = creds.get("password", "")

    conn = psycopg2.connect(
        host=host, port=port, dbname=dbname, user=user, password=password,
        # TLS with certificate verification against the bundled RDS CA (db_tls):
        # verify-full. Mirrors pg_client.
        **ssl_kwargs(),
        connect_timeout=10,
        options="-c statement_timeout=15000",  # 15s timeout — these are fast queries
    )
    conn.set_session(readonly=True, autocommit=True)
    return conn


def collect_metrics(conn) -> dict:
    """Run all Tier 1 queries and return a metrics dict. Total: <2 seconds."""
    metrics = {"scan_time": datetime.now(timezone.utc).isoformat()}

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # 1. Cache hit ratio
        cur.execute("""
            SELECT ROUND(100.0 * blks_hit / NULLIF(blks_hit + blks_read, 0), 2) AS ratio
            FROM pg_stat_database WHERE datname = current_database()
        """)
        row = cur.fetchone()
        metrics["cache_hit_ratio"] = float(row["ratio"]) if row and row["ratio"] is not None else None

        # 2. Connection states (total, active, idle, idle_in_txn, waiting)
        cur.execute("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE state = 'active') AS active,
                COUNT(*) FILTER (WHERE state = 'idle') AS idle,
                COUNT(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_txn,
                -- A waiting QUERY is an ACTIVE backend blocked on a wait event. An idle
                -- client waits on ClientRead (wait_event_type = 'Client'), which is not
                -- contention; require state='active' so idle / idle-in-transaction
                -- sessions are not counted as waiting queries.
                COUNT(*) FILTER (WHERE state = 'active'
                                   AND wait_event_type IS NOT NULL
                                   AND wait_event_type != 'Activity') AS waiting
            FROM pg_stat_activity
            WHERE pid != pg_backend_pid()
        """)
        row = cur.fetchone()
        if row:
            metrics["total_connections"] = row["total"]
            # As a share of max_connections, for the connection-saturation alarm. Every
            # backend counts against the limit, so the total is the right numerator.
            cur.execute("SELECT current_setting('max_connections')::int AS max_conn")
            mc = cur.fetchone() or {}
            max_conn = mc.get("max_conn") if hasattr(mc, "get") else None
            if max_conn:
                metrics["connections_used_pct"] = round(100.0 * row["total"] / max_conn, 2)
            metrics["active_connections"] = row["active"]
            metrics["idle_connections"] = row["idle"]
            metrics["idle_in_txn_connections"] = row["idle_in_txn"]
            metrics["waiting_queries"] = row["waiting"]

        # 3. Blocked queries (lock contention)
        cur.execute("""
            SELECT COUNT(*) AS blocked
            FROM pg_locks WHERE NOT granted
        """)
        row = cur.fetchone()
        metrics["blocked_queries"] = row["blocked"] if row else 0

        # 4. XID age (% of wraparound threshold). The WORST database on the instance, not
        # just the connected one: wraparound pressure accumulates on a rarely-vacuumed
        # database (often `postgres` or a secondary), which the deep scan already measures
        # across every datallowconn database. Measuring only current_database() left the
        # live surface near 0% while the deep scan reported 80% on the same instance.
        # One shared denominator (tools.xid) on every surface so the dashboard, the agent
        # and the HTML report never disagree.
        # nosemgrep: sqlalchemy-execute-raw-query - only the XID_WRAPAROUND_MAX int constant is interpolated; no request input
        cur.execute(f"""
            SELECT ROUND(100.0 * MAX(age(datfrozenxid)) / {XID_WRAPAROUND_MAX}, 2) AS pct
            FROM pg_database WHERE datallowconn
        """)
        row = cur.fetchone()
        metrics["xid_age_pct"] = float(row["pct"]) if row and row["pct"] is not None else None

        # 5. Database size (MB)
        cur.execute("SELECT pg_database_size(current_database()) / (1024*1024.0) AS size_mb")
        row = cur.fetchone()
        metrics["database_size_mb"] = round(float(row["size_mb"]), 2) if row else None

        # 6. Autovacuum workers active
        cur.execute("""
            SELECT COUNT(*) AS workers
            FROM pg_stat_activity WHERE backend_type = 'autovacuum worker'
        """)
        row = cur.fetchone()
        metrics["autovacuum_workers_active"] = row["workers"] if row else 0

        # 7. Tables needing vacuum (>20% dead tuples, >1000 dead, >1MB)
        cur.execute("""
            SELECT COUNT(*) AS cnt FROM pg_stat_user_tables
            WHERE n_dead_tup > 0.2 * (n_live_tup + n_dead_tup)
              AND n_dead_tup > 1000
              AND pg_total_relation_size(relid) > 1048576
        """)
        row = cur.fetchone()
        metrics["tables_needing_vacuum"] = row["cnt"] if row else 0

        # 8. Longest open CLIENT transaction (seconds). Client backends only: an
        # autovacuum worker on a large table, or a WAL sender, holds a transaction for a
        # long time by design, and counting it would raise the long-transaction alarm on
        # a healthy database.
        cur.execute("""
            SELECT EXTRACT(EPOCH FROM MAX(now() - xact_start))::numeric AS secs
            FROM pg_stat_activity
            WHERE xact_start IS NOT NULL AND pid != pg_backend_pid()
              AND backend_type = 'client backend'
        """)
        row = cur.fetchone()
        metrics["longest_transaction_sec"] = round(float(row["secs"]), 1) if row and row["secs"] is not None else None

        # 9. Dead tuple % (worst table — the scalar metric). Order by the computed RATIO,
        # not by absolute n_dead_tup: this metric is "the worst table's dead-tuple
        # percentage", so a huge table at 5% must not hide a smaller one at 80%.
        cur.execute("""
            SELECT ROUND(100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0), 2) AS pct
            FROM pg_stat_user_tables
            WHERE n_live_tup + n_dead_tup > 0
            ORDER BY (100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0)) DESC NULLS LAST
            LIMIT 1
        """)
        row = cur.fetchone()
        metrics["dead_tuple_pct"] = float(row["pct"]) if row and row["pct"] is not None else None

        # 10. Table bloat — top 10 by dead tuples (JSONB)
        cur.execute("""
            SELECT schemaname AS schema, relname AS table,
                   ROUND(100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0), 2) AS bloat_pct,
                   n_dead_tup AS dead_tuples, n_live_tup AS live_tuples
            FROM pg_stat_user_tables
            WHERE n_dead_tup > 0
            ORDER BY n_dead_tup DESC LIMIT 10
        """)
        rows = cur.fetchall()
        metrics["table_bloat"] = [
            {
                "schema": r["schema"], "table": r["table"],
                "bloat_pct": float(r["bloat_pct"]) if r["bloat_pct"] is not None else 0,
                "dead_tuples": r["dead_tuples"], "live_tuples": r["live_tuples"],
            }
            for r in rows
        ]

        # 11. Wait events summary (JSONB)
        cur.execute("""
            SELECT wait_event_type AS type, wait_event AS event, COUNT(*) AS sessions
            FROM pg_stat_activity
            WHERE wait_event IS NOT NULL AND pid != pg_backend_pid()
            GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 20
        """)
        rows = cur.fetchall()
        metrics["wait_events"] = [
            {"type": r["type"], "event": r["event"], "sessions": r["sessions"]}
            for r in rows
        ]

        # 12. Cumulative pg_stat_database counters, one cheap row: deadlocks (real app
        # contention), transactions committed / rolled back (workload rate and error
        # ratio), temp files and bytes (queries spilling to disk). Stored raw; the
        # per-interval deltas and rates are derived against the previous sample in
        # ``derive_counter_rates`` (a counter reset gives None, never a negative).
        cur.execute("""
            SELECT deadlocks, xact_commit, xact_rollback, temp_files,
                   ROUND(temp_bytes / (1024*1024.0), 2) AS temp_bytes_mb
            FROM pg_stat_database WHERE datname = current_database()
        """)
        row = cur.fetchone()
        if row:
            metrics["deadlocks"] = row["deadlocks"]
            metrics["xact_commit"] = row["xact_commit"]
            metrics["xact_rollback"] = row["xact_rollback"]
            metrics["temp_files"] = row["temp_files"]
            metrics["temp_bytes_mb"] = float(row["temp_bytes_mb"]) if row["temp_bytes_mb"] is not None else None

    return metrics


def derive_counter_rates(current: dict, previous: dict | None) -> dict:
    """Per-interval figures from two cumulative samples of one member: ``deadlocks_delta``,
    ``txn_per_sec`` and ``rollback_ratio_pct`` (rollbacks as a share of the interval's
    transactions). Without a previous sample, or after a statistics reset (a counter went
    backwards), the deltas are None and the rollback ratio falls back to the cumulative
    share since the last reset; a zero-transaction interval has no ratio."""
    out = {"deadlocks_delta": None, "txn_per_sec": None, "rollback_ratio_pct": None}

    def _i(d, k):
        try:
            return int(d.get(k))
        except (TypeError, ValueError, AttributeError):
            return None

    c, r = _i(current, "xact_commit"), _i(current, "xact_rollback")
    if c is not None and r is not None and (c + r) > 0:
        out["rollback_ratio_pct"] = round(100.0 * r / (c + r), 3)
    if not previous:
        return out
    try:
        t0 = datetime.fromisoformat(str(previous.get("scan_time")).replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(str(current.get("scan_time")).replace("Z", "+00:00"))
        secs = (t1 - t0).total_seconds()
    except (TypeError, ValueError):
        return out
    if secs <= 0:
        return out
    d0, d1 = _i(previous, "deadlocks"), _i(current, "deadlocks")
    if d0 is not None and d1 is not None and d1 >= d0:
        out["deadlocks_delta"] = d1 - d0
    c0, r0 = _i(previous, "xact_commit"), _i(previous, "xact_rollback")
    if None not in (c0, r0, c, r) and c >= c0 and r >= r0:
        dc, dr = c - c0, r - r0
        out["txn_per_sec"] = round((dc + dr) / secs, 3)
        out["rollback_ratio_pct"] = round(100.0 * dr / (dc + dr), 3) if dc + dr > 0 else None
    return out


# ─────────────────────────────────────────────
# Write to the metrics store (DynamoDB rows + CloudWatch metrics)
# ─────────────────────────────────────────────

def _row(instance_id: str, metrics: dict) -> dict:
    """One member's collected metrics as the store row: every field the collector
    measured (``metrics_store.TIER1_NUMERIC_METRICS`` plus the two lists), with the
    member tags filled in. A field the collector could not read stays None."""
    from tools.aws import metrics_store
    row = {
        "instance_id": instance_id,
        "scan_time": metrics.get("scan_time", datetime.now(timezone.utc).isoformat()),
        "member_instance_id": metrics.get("member_instance_id") or instance_id,
        "member_role": metrics.get("member_role") or "writer",
    }
    for k in metrics_store.TIER1_NUMERIC_METRICS:
        row[k] = metrics.get(k)
    for k in metrics_store.TIER1_LIST_FIELDS:
        v = metrics.get(k)
        row[k] = v if isinstance(v, list) else []
    return row


def slot_time(event: dict) -> str:
    """The scheduled run's sample time, as the rows' ``scan_time``: EventBridge
    Scheduler's ``scheduled_time`` (``<aws.scheduler.scheduled-time>`` in the schedule's
    input), or an EventBridge event's ``time``, truncated to the minute. Every retry or
    duplicate delivery of one scheduled run carries the same value, so its rows land on the
    same (instance_id, member, scan_time) item key and rewrite it rather than add one.
    Not truncated to a cadence boundary: a schedule's own runs are already one cadence
    apart, and a boundary would date every row up to a cadence early. '' when the event
    has no usable time (a manual invoke): the invocation's clock is used."""
    raw = str(event.get("scheduled_time") or event.get("time") or "")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(second=0, microsecond=0).isoformat()


def attach_counter_rates(instance_id: str, rows: list) -> None:
    """Fill each member row's per-interval figures (``derive_counter_rates``) from that
    member's previous stored sample. One cheap read per member; a read failure leaves
    the deltas None rather than failing the collection."""
    from tools.aws import metrics_store
    for m in rows:
        member = m.get("member_instance_id") or instance_id
        try:
            prev = metrics_store.tier1_previous(
                instance_id, member, m.get("scan_time") or datetime.now(timezone.utc).isoformat())
        except Exception as e:  # noqa: BLE001
            logger.warning("previous Tier 1 sample for %s not read (%s); rates left empty",
                           instance_id, type(e).__name__)
            prev = None
        m.update(derive_counter_rates(m, prev))


def write_to_store(instance_id: str, rows: list, retention_days=None) -> int:
    """Write one DynamoDB item per collected member, then publish every numeric to
    CloudWatch. The DynamoDB write is the collection's success criterion (it raises); the
    CloudWatch publish is fail-soft, logged, so a metrics hiccup never loses the rows."""
    from tools.aws import metrics_store
    items = [_row(instance_id, m) for m in rows]
    n = metrics_store.put_tier1_rows(instance_id, items, retention_days)
    logger.info(f"Wrote {n} member row(s) for {instance_id} to the metrics store")
    try:
        k = metrics_store.publish_tier1_metrics(instance_id, items)
        logger.info(f"Published {k} CloudWatch datum(s) for {instance_id}")
    except Exception as e:  # noqa: BLE001
        logger.warning("CloudWatch publish for %s failed: %s", instance_id, e)
    return n


def _set_store_error(record: dict, message: str) -> None:
    """Record (``message``) or clear (``""``) the store-write problem the admin Collection
    view shows for this database. Written only when it changes; never raises."""
    iid = record.get("instance_id", "")
    current = (record.get("tier1_store_error") or {}).get("message", "")
    if current == message:
        return
    try:
        if message:
            _instances_table().update_item(
                Key={"instance_id": iid}, UpdateExpression="SET #e = :e",
                ConditionExpression="attribute_exists(instance_id)",
                ExpressionAttributeNames={"#e": "tier1_store_error"},
                ExpressionAttributeValues={":e": {"at": _now_iso(), "message": message}})
        else:
            _instances_table().update_item(
                Key={"instance_id": iid}, UpdateExpression="REMOVE #e",
                ConditionExpression="attribute_exists(instance_id)",
                ExpressionAttributeNames={"#e": "tier1_store_error"})
    except Exception as e:  # noqa: BLE001
        logger.warning("store error note for %s not written (%s)", iid, type(e).__name__)


# Retention: each Tier 1 item carries an ``expires_at`` TTL set from the database's
# ``retention_days`` at write time (metrics_store.put_tier1_rows); DynamoDB deletes it.
# There is no prune step any more. Tier 2 scans are kept (the long-term baseline).


# ─────────────────────────────────────────────
# Cluster members: re-read from RDS, detect a failover
# ─────────────────────────────────────────────

CONFIG_LOG_MAX = 50


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _instances_table():
    return _get_dynamodb().Table(resolve_table_name("instances"))


def _is_conditional_failure(e: Exception) -> bool:
    return (getattr(e, "response", None) or {}).get("Error", {}).get("Code") == \
        "ConditionalCheckFailedException"


_MEMBER_DETAILS = ("endpoint", "availability_zone", "instance_class")


def _member_key(roles) -> list:
    return sorted((m.get("instance_id", ""), bool(m.get("is_writer")),
                   *(m.get(k, "") for k in _MEMBER_DETAILS))
                  for m in roles or [])


def _notify(subject: str, message: str) -> None:
    """Plain-text notice on the deployment's notification topic (no JSON instance_id
    field, so the Learning Agent ignores it). Skipped when no topic is configured; a
    failure is logged, never raised."""
    topic = os.getenv("NOTIFICATION_TOPIC_ARN", "")
    if not topic:
        return
    try:
        boto3.client("sns", region_name=_region()).publish(TopicArn=topic, Subject=subject, Message=message)
    except Exception as e:  # noqa: BLE001
        logger.warning("notification failed (%s)", type(e).__name__)


def _audit_scope(record: dict) -> dict:
    """The fan-out and label of a system admin event about this database: its member
    spaces (the same shape as database_teardown_complete)."""
    return {"space_ids": sorted(record.get("member_space_ids") or []),
            "target_label": record.get("display_name") or record.get("instance_id", "")}


def _audited(action: str, record: dict, state) -> None:
    """``audit_log.write_event`` never raises; a result other than stored is logged."""
    if state != "stored":
        logger.warning("audit %s for %s: vault state %s", action, record.get("instance_id"), state)


def sync_members(record: dict, session) -> dict:
    """Re-read the cluster's members and roles (one ``describe_db_clusters`` through the
    reach session) and bring the record up to date. Returns the record to collect with.

    This function is the only writer of ``writer_instance_id``, and it writes it with a
    condition on the value it read, so a failover is acted on exactly once even when two
    runs overlap: one record update, one ``cluster_failover_detected`` audit event, one
    notification, one config_log line. A membership change without a writer change (a
    reader added or removed) updates the record and logs a line, without audit or
    notice. The first run after onboarding initialises ``writer_instance_id`` silently.
    Collection never depends on this: any RDS error keeps the stored roles.

    Returns ``(record, synced)``: ``synced`` is False when the roles could not be read
    this run (the call failed, or RDS named no writer), so the caller does not trust
    the stored writer's id for a row it collects through the cluster endpoint. Failed
    calls are counted on the record (``member_sync_failures`` since
    ``member_sync_failing_since``); the second in a row writes a config_log line and
    the admin Collection view shows it, until a run reads the members again."""
    iid = record.get("instance_id", "")
    cluster_id = record_arn(record).rsplit(":", 1)[-1]
    try:
        rds = session.client("rds", region_name=record.get("region") or _region())
        cluster = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
    except Exception as e:  # noqa: BLE001
        why = ((getattr(e, "response", None) or {}).get("Error") or {}).get("Code") or type(e).__name__
        logger.warning("member sync skipped for %s: %s", iid, why)
        return _sync_failed(record, why), False
    members = cluster.get("DBClusterMembers") or []
    new_writer = next((m.get("DBInstanceIdentifier", "") for m in members if m.get("IsClusterWriter")), "")
    if not new_writer:
        logger.info("member sync for %s: no writer reported (mid-failover?); keeping stored roles", iid)
        return record, False
    record = _sync_recovered(record)

    stored = {m.get("instance_id"): m for m in record.get("cluster_member_roles") or []}
    details = {}
    # Instance details are fetched while any member is new OR still lacks one: RDS
    # lists a reader in DBClusterMembers before it has an Endpoint (while `creating`),
    # and a failed lookup is tolerated, so a stored gap is filled on a later run.
    if any(not all((stored.get(m.get("DBInstanceIdentifier")) or {}).get(k) for k in _MEMBER_DETAILS)
           for m in members):
        try:
            resp = rds.describe_db_instances(Filters=[{"Name": "db-cluster-id", "Values": [cluster_id]}])
            details = {i["DBInstanceIdentifier"]: i for i in resp.get("DBInstances", [])}
        except Exception as e:  # noqa: BLE001 - a new member without endpoint is skipped this run
            logger.warning("member detail lookup failed for %s: %s", iid, type(e).__name__)
    roles = []
    for m in members:
        mid = m.get("DBInstanceIdentifier", "")
        prev, inst = stored.get(mid) or {}, details.get(mid) or {}
        roles.append({
            "instance_id": mid,
            "is_writer": bool(m.get("IsClusterWriter")),
            "endpoint": prev.get("endpoint") or (inst.get("Endpoint") or {}).get("Address", ""),
            "availability_zone": prev.get("availability_zone") or inst.get("AvailabilityZone", ""),
            "instance_class": prev.get("instance_class") or inst.get("DBInstanceClass", ""),
        })
    reader_ep = cluster.get("ReaderEndpoint") or record.get("reader_endpoint", "")
    old_w = record.get("writer_instance_id", "") or ""
    members_changed = _member_key(roles) != _member_key(record.get("cluster_member_roles"))
    reader_changed = reader_ep != (record.get("reader_endpoint") or "")
    if not (members_changed or reader_changed or old_w != new_writer):
        return record, True

    now = _now_iso()
    failover = bool(old_w) and old_w != new_writer
    log = list(record.get("config_log") or [])
    if failover:
        log.append(f"{now} Failover detected: writer {old_w} -> {new_writer}")
    elif members_changed and stored:
        before_ids, after_ids = sorted(stored), sorted(r["instance_id"] for r in roles)
        if before_ids != after_ids:
            log.append(f"{now} Cluster members changed: {', '.join(before_ids)} -> {', '.join(after_ids)}")
    log = log[-CONFIG_LOG_MAX:]
    fields = {"cluster_member_roles": roles, "reader_endpoint": reader_ep,
              "writer_instance_id": new_writer, "config_log": log, "updated_at": now}
    if members_changed or reader_changed:
        fields["members_changed_at"] = now
    names = {f"#{k}": k for k in fields}
    values = {f":{k}": v for k, v in fields.items()}
    values[":old_w"] = old_w
    try:
        _instances_table().update_item(
            Key={"instance_id": iid},
            UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
            ConditionExpression=("attribute_exists(instance_id) AND "
                                 "(attribute_not_exists(#writer_instance_id) OR #writer_instance_id = :old_w)"),
            ExpressionAttributeNames=names, ExpressionAttributeValues=values)
    except Exception as e:  # noqa: BLE001
        if _is_conditional_failure(e):
            logger.info("member sync for %s: another run applied a newer state; reloading", iid)
            return _get_instance_record(iid) or record, True
        logger.error("member sync write failed for %s: %s; collecting on the members read now",
                     iid, type(e).__name__)
        return {**record, **fields}, True
    record = {**record, **fields}
    if failover:
        ids = [r["instance_id"] for r in roles]
        from tools.aws import audit_log
        _audited("cluster_failover_detected", record, audit_log.write_event(
            "system", "cluster_failover_detected", iid, "account",
            before={"writer_instance_id": old_w, "members": sorted(stored)},
            after={"writer_instance_id": new_writer, "previous_writer_instance_id": old_w, "members": ids},
            **_audit_scope(record)))
        label = record.get("display_name") or iid
        _notify("Database failover detected",
                f"The database {label} ({iid}) failed over: its writer moved from {old_w} to "
                f"{new_writer}. Collection continued on the cluster endpoint, which follows "
                f"the writer; the member roles in the dashboard are updated.")
    return record, True


SYNC_FAILING_AFTER = 2   # consecutive failed member syncs before it is logged and shown


def _sync_failed(record: dict, why: str) -> dict:
    """Count a failed member sync on the record. The second consecutive failure writes
    one config_log line; the admin Collection view shows "member sync failing since"
    from then on. Never raises."""
    iid = record.get("instance_id", "")
    n = int(record.get("member_sync_failures") or 0) + 1
    now = _now_iso()
    since = record.get("member_sync_failing_since") or now
    fields = {"member_sync_failures": n, "member_sync_failing_since": since}
    if n == SYNC_FAILING_AFTER:
        log = list(record.get("config_log") or [])
        log.append(f"{now} Member sync failing since {since} ({why}, {n} runs in a row): "
                   f"failover detection and member roles are not current")
        fields.update(config_log=log[-CONFIG_LOG_MAX:], updated_at=now)
    try:
        _instances_table().update_item(
            Key={"instance_id": iid},
            UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
            ConditionExpression="attribute_exists(instance_id)",
            ExpressionAttributeNames={f"#{k}": k for k in fields},
            ExpressionAttributeValues={f":{k}": v for k, v in fields.items()})
    except Exception as e:  # noqa: BLE001
        logger.warning("member sync failure count for %s not written (%s)", iid, type(e).__name__)
    return {**record, **fields}


def _sync_recovered(record: dict) -> dict:
    """Clear the failure count once the members are read again (logged when it had been
    shown). A no-op, with no write, when nothing was counted."""
    n = int(record.get("member_sync_failures") or 0)
    if not n:
        return record
    iid = record.get("instance_id", "")
    rest = {k: v for k, v in record.items() if k not in ("member_sync_failures", "member_sync_failing_since")}
    try:
        if n >= SYNC_FAILING_AFTER:
            now = _now_iso()
            log = (list(record.get("config_log") or []) + [f"{now} Member sync recovered"])[-CONFIG_LOG_MAX:]
            _instances_table().update_item(
                Key={"instance_id": iid},
                UpdateExpression="SET #cl = :log, #ua = :ts REMOVE #f, #s",
                ConditionExpression="attribute_exists(instance_id)",
                ExpressionAttributeNames={"#cl": "config_log", "#ua": "updated_at",
                                          "#f": "member_sync_failures", "#s": "member_sync_failing_since"},
                ExpressionAttributeValues={":log": log, ":ts": now})
            rest.update(config_log=log, updated_at=now)
        else:
            _instances_table().update_item(
                Key={"instance_id": iid}, UpdateExpression="REMOVE #f, #s",
                ConditionExpression="attribute_exists(instance_id)",
                ExpressionAttributeNames={"#f": "member_sync_failures", "#s": "member_sync_failing_since"})
    except Exception as e:  # noqa: BLE001
        logger.warning("member sync recovery for %s not written (%s)", iid, type(e).__name__)
        return record
    return rest


# ─────────────────────────────────────────────
# Cross-Region host routes: detect drift (the API task repairs)
# ─────────────────────────────────────────────

ROUTE_CHECK = "Host route check:"
ROUTE_FLAG = "route_repair_needed"


def _owned(record: dict) -> dict:
    """``{ip: host}`` of the record's owned `/32`s."""
    return {o.get("ip"): o.get("host", "") for o in record.get("host_routes") or []
            if isinstance(o, dict) and o.get("ip")}


def _live_issue(ip: str, routes: dict, peering: str) -> str:
    """Why the live `/32` for an owned address does not reach the database, or ''. Only
    an active route to the record's own peering counts as routed."""
    from tools.aws import host_routes
    r = routes.get(f"{ip}/32")
    if not r:
        return "route missing"
    if r.get("State") != "active":
        return "route blackholed"
    if host_routes.route_target(r) != peering:
        return "route points elsewhere"
    return ""


def route_findings(record: dict, table_routes=()) -> tuple:
    """``(findings, unresolved)``: what the record's host routes do not cover,
    ``[{"member", "address", "issue"}]``, and the names that did not resolve. A finding
    is a member name that resolves to an address with no owned `/32` (``unrouted``), an
    owned `/32` that the live agent route table (`table_routes`) does not hold as an
    active route to the record's peering (``route missing`` / ``route blackholed`` /
    ``route points elsewhere``), or an owned `/32` whose member is gone (``member
    gone``). A name that does not resolve is not a finding: its owned `/32` is kept
    ("unknown" is not "gone")."""
    from tools.aws import host_routes
    names = host_routes.record_hosts(record)
    desired, unresolved = host_routes.desired_addresses(names)
    owned = _owned(record)
    routes = {r.get("DestinationCidrBlock"): r for r in table_routes or ()}
    peering = record.get("network_peering_id", "")
    out = []
    for ip, host in sorted(desired.items()):
        issue = "unrouted" if ip not in owned else _live_issue(ip, routes, peering)
        if issue:
            out.append({"member": host, "address": ip, "issue": issue})
    out += [{"member": host or ip, "address": ip, "issue": "member gone"}
            for ip, host in sorted(owned.items()) if host and host not in names]
    return out, unresolved


def check_host_routes(record: dict) -> dict:
    """Detect host-route drift on the cross-Region CIDR-clash path; never touch a route.

    Runs only for a configured record with ``network_route_mode == "host"``, after
    ``sync_members`` so new and departed members count, and before collection. When a
    member's address has no owned `/32`, its owned `/32` is not an active route to the
    record's peering in the live agent route table (missing, blackholed, pointing
    elsewhere), or an owned `/32`'s member is gone, the record is flagged
    ``route_repair_needed`` (the member, the address, when it was detected) with one
    config_log line, and the member is skipped (``host_routes.dial_gate`` skips a
    reader whose address is unowned or flagged; the writer stays on the cluster
    endpoint). The API task repairs a flagged record with Configure's planner
    (``tools.aws.route_repair``); this collector only reads the route table
    (``ec2:DescribeRouteTables``) and holds no route-write permission. Nothing to
    report clears a flag the repair has not cleared yet (the drift went away), unless a
    name did not resolve. When the VPC-wide route reaches the database again there is
    no drift. Writes are conditional on the record still being configured."""
    if record.get("network_route_mode") != "host" or record.get("status") != "configured":
        return record
    from tools.aws import host_routes
    iid = record.get("instance_id", "")
    rt = os.getenv("AGENT_PRIVATE_ROUTE_TABLE_ID", "")
    peering, cidr = record.get("network_peering_id", ""), record.get("db_vpc_cidr", "")
    if not (rt and peering and cidr):
        logger.info("host route check skipped for %s: route table, peering or CIDR unknown", iid)
        return record
    try:
        table = boto3.client("ec2", region_name=_region()).describe_route_tables(
            RouteTableIds=[rt])["RouteTables"][0].get("Routes", [])
    except Exception as e:  # noqa: BLE001
        logger.warning("host route check skipped for %s: route table unreadable (%s)", iid, type(e).__name__)
        return record
    holder = host_routes.route_holder(table, cidr)
    findings, unresolved = ([], []) if (not holder or holder == peering) else route_findings(record, table)
    flag = record.get(ROUTE_FLAG) or {}
    if flag and unresolved:
        # A name that did not resolve is unknown, not healthy: a DNS failure never
        # clears or rewrites a pending flag, nor the refusal reason the repair put on it.
        logger.info("host route check for %s: %d name(s) did not resolve; the repair flag is kept",
                    iid, len(unresolved))
        return record
    key = sorted((f["member"], f["address"], f["issue"]) for f in findings)
    old_key = sorted((f.get("member"), f.get("address"), f.get("issue")) for f in flag.get("items") or [])
    if findings and key == old_key:
        return record          # already flagged for exactly this; the repair has it
    if not findings and not flag:
        return record
    now = _now_iso()
    log = list(record.get("config_log") or [])
    tbl = _instances_table()
    try:
        if findings:
            first = findings[0]
            new_flag = {"member": first["member"], "address": first["address"], "issue": first["issue"],
                        "detected_at": now, "items": findings}
            what = "; ".join(f"{f['member']} {f['address']} ({f['issue']})" for f in findings)
            log = (log + [f"{now} {ROUTE_CHECK} route repair needed: {what}; the member is skipped "
                          f"until the API task repairs it"])[-CONFIG_LOG_MAX:]
            tbl.update_item(
                Key={"instance_id": iid}, UpdateExpression="SET #fl = :fl, #cl = :log, #ua = :ts",
                ConditionExpression="#st = :configured",
                ExpressionAttributeNames={"#fl": ROUTE_FLAG, "#cl": "config_log", "#ua": "updated_at",
                                          "#st": "status"},
                ExpressionAttributeValues={":fl": new_flag, ":log": log, ":ts": now, ":configured": "configured"})
            return {**record, ROUTE_FLAG: new_flag, "config_log": log}
        log = (log + [f"{now} {ROUTE_CHECK} all member addresses routed"])[-CONFIG_LOG_MAX:]
        tbl.update_item(
            Key={"instance_id": iid}, UpdateExpression="SET #cl = :log, #ua = :ts REMOVE #fl",
            ConditionExpression="#st = :configured",
            ExpressionAttributeNames={"#fl": ROUTE_FLAG, "#cl": "config_log", "#ua": "updated_at",
                                      "#st": "status"},
            ExpressionAttributeValues={":log": log, ":ts": now, ":configured": "configured"})
        return {**{k: v for k, v in record.items() if k != ROUTE_FLAG}, "config_log": log}
    except Exception as e:  # noqa: BLE001
        logger.info("host route check for %s not recorded (%s)", iid, type(e).__name__)
        return record


# ─────────────────────────────────────────────
# Members to collect from
# ─────────────────────────────────────────────

def _member_targets(record: dict, synced: bool = True) -> tuple:
    """``(targets, skipped)``. A target is ``{"instance_id", "host", "fallback",
    "verify_id"}``: `host` is the member's instance endpoint (``None`` means the record
    endpoint) and `fallback` the record endpoint to retry the writer on. `verify_id`
    (the writer of a cluster whose members were not read this run): a row collected
    through the cluster endpoint is tagged with the instance the server says it is, or
    ``unknown``, never the stored writer's id, which may be stale after a failover.
    `skipped` maps a member that is deliberately not dialled on this record's network
    path to why."""
    from tools.aws import host_routes
    roles = record.get("cluster_member_roles") or []
    if not _is_cluster(record) or not roles:
        arn = record_arn(record)
        mid = arn.rsplit(":", 1)[-1] if arn else record.get("instance_id", "")
        return [{"instance_id": mid, "host": None, "fallback": None}], {}
    targets, skipped = [], {}
    endpoint = record.get("endpoint", "")
    for m in sorted(roles, key=lambda m: (not m.get("is_writer"), m.get("instance_id", ""))):
        mid = m.get("instance_id", "")
        host, reason = host_routes.dial_gate(record, m)
        if host is None:
            skipped[mid] = reason
            logger.info("member %s of %s %s", mid, record.get("instance_id"), reason)
            continue
        fallback = endpoint if m.get("is_writer") and host != endpoint else None
        targets.append({"instance_id": mid, "host": host, "fallback": fallback,
                        "verify_id": bool(m.get("is_writer")) and not synced})
    return targets, skipped


UNKNOWN_MEMBER = "unknown"


def _server_member_id(conn, engine: str) -> str:
    """The instance a connection reached, as the server reports it (Aurora names
    itself); '' when the engine cannot say."""
    if engine != "aurora-postgresql":
        return ""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT aurora_db_instance_identifier()")
            row = cur.fetchone()
        val = row[0] if isinstance(row, (tuple, list)) else None
        return str(val) if val else ""
    except Exception as e:  # noqa: BLE001
        logger.info("server instance id not readable (%s)", type(e).__name__)
        return ""


# Non-Aurora replica lag: 0 when the standby has replayed all the WAL it received (caught
# up) AND its WAL receiver is connected (a pg_stat_wal_receiver row, streaming; the
# status reads NULL for a login without pg_read_all_stats, so a present row counts);
# NULL (unknown) when it has replayed everything but receives nothing, because it may be
# falling behind a primary that is still writing; otherwise the age of the last replayed
# commit. That age alone grows one second per second on an idle primary, with nothing
# behind. The deep scan's reader findings report the receiver being down.
WAL_RECEIVING_SQL = ("EXISTS (SELECT 1 FROM pg_stat_wal_receiver "
                     "WHERE status IS NULL OR status = 'streaming')")
REPLAY_LAG_MS_SQL = (
    "SELECT CASE WHEN pg_last_wal_receive_lsn() = pg_last_wal_replay_lsn() "
    f"THEN CASE WHEN {WAL_RECEIVING_SQL} THEN 0 END "
    "ELSE EXTRACT(EPOCH FROM now() - pg_last_xact_replay_timestamp()) * 1000 END")


def _observe_role(conn, engine: str) -> tuple:
    """``(member_role, replica_lag_ms)`` as the server reports it: a server in recovery
    is a reader. Lag is measured on readers only; NULL when it cannot be."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_is_in_recovery()")
        row = cur.fetchone()
    in_recovery = bool(row[0] if isinstance(row, (tuple, list)) else (row or {}).get("pg_is_in_recovery"))
    if not in_recovery:
        return "writer", None
    if engine == "aurora-postgresql":
        sql = ("SELECT replica_lag_in_msec FROM aurora_replica_status() "
               "WHERE server_id = aurora_db_instance_identifier()")
    else:
        sql = REPLAY_LAG_MS_SQL
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
        val = row[0] if isinstance(row, (tuple, list)) else None
        return "reader", round(float(val), 1) if val is not None else None
    except Exception as e:  # noqa: BLE001 - the function may be missing or not granted
        logger.info("replica lag not measurable (%s)", type(e).__name__)
        return "reader", None


def _collect_member(creds: dict, record: dict, target: dict) -> dict:
    """Connect to one member, collect, observe its role. Raises on failure."""
    conn = None
    endpoint = record.get("endpoint", "") or ""
    mid = target["instance_id"]
    try:
        dialled = target["host"] or endpoint
        try:
            conn = _connect_to_target(creds, record, dial_host=target["host"] or "")
        except ValueError:
            raise   # the secret belongs to another database: no endpoint changes that
        except Exception as e:
            if not target.get("fallback"):
                raise
            logger.warning("writer %s instance endpoint failed (%s); using the cluster endpoint",
                           target["instance_id"], type(e).__name__)
            conn = _connect_to_target(creds, record, dial_host=target["fallback"])
            dialled = target["fallback"]
        metrics = collect_metrics(conn)
        role, lag = _observe_role(conn, record.get("engine", ""))
        if target.get("verify_id") and dialled == endpoint:
            # The cluster endpoint follows the writer, which may not be the one stored.
            mid = _server_member_id(conn, record.get("engine", "")) or UNKNOWN_MEMBER
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    metrics.update(member_instance_id=mid, member_role=role, replica_lag_ms=lag)
    return metrics


MAX_WORKERS = 4
BUDGET_MARGIN_S = 20
DEFAULT_BUDGET_S = 150


def _collect_all(creds: dict, record: dict, targets: list, context) -> tuple:
    """Collect from every target, four at a time, inside the invocation's time budget
    (remaining time minus a margin for the write and prune). ``(rows, outcomes)``:
    a member still running when the budget ends is ``skipped: time budget``."""
    try:
        budget = max(0.0, context.get_remaining_time_in_millis() / 1000.0 - BUDGET_MARGIN_S)
    except Exception:  # noqa: BLE001 - no Lambda context (local run)
        budget = DEFAULT_BUDGET_S
    rows, outcomes = [], {}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)
    try:
        futures = {pool.submit(_collect_member, creds, record, t): t for t in targets}
        done, _pending = concurrent.futures.wait(futures, timeout=budget)
        for fut, t in futures.items():
            mid = t["instance_id"]
            if fut not in done:
                fut.cancel()
                outcomes[mid] = "skipped: time budget"
                logger.error("member %s of %s: no result within the time budget", mid, record.get("instance_id"))
                continue
            try:
                rows.append(fut.result())
                outcomes[mid] = "ok"
            except Exception as e:  # noqa: BLE001
                outcomes[mid] = f"{type(e).__name__}: {str(e)[:200]}"
                logger.error("Failed to collect metrics from member %s of %s: %s",
                             mid, record.get("instance_id"), e)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return rows, outcomes


# ─────────────────────────────────────────────
# Lambda handler
# ─────────────────────────────────────────────

# Outcomes that mean "nothing to collect", not "collection is broken": no status datum.
_NOT_A_COLLECTION = ("Missing instance_id", "Instance not found")


def handler(event, context):
    """Lambda entry point. Expects {"instance_id": "xxx", "scheduled_time": "..."} from
    EventBridge Scheduler (``scheduled_time`` optional; see ``slot_time``).

    Every run of a known database ends with a ``collection_failed`` datum (0 collected,
    1 not), crash included, for the collection-failing alarm. Its publish never changes
    the run's outcome."""
    try:
        out = _handle(event, context)
    except Exception:
        _publish_status(event, ok=False)
        raise
    if out.get("message") not in _NOT_A_COLLECTION:
        _publish_status(event, ok=out.get("status") in ("ok", "partial"))
    return out


def _publish_status(event: dict, ok: bool) -> None:
    instance_id = (event or {}).get("instance_id")
    if not instance_id:
        return
    try:
        from tools.aws import metrics_store
        metrics_store.publish_collection_status(instance_id, ok, slot_time(event) or None)
    except Exception as e:  # noqa: BLE001
        logger.warning("collection status for %s not published: %s", instance_id, type(e).__name__)


def _handle(event, context):
    instance_id = event.get("instance_id")
    if not instance_id:
        logger.error("No instance_id in event")
        return {"status": "error", "message": "Missing instance_id"}

    logger.info(f"Collecting Tier 1 metrics for {instance_id}")

    # 1. Look up instance record
    record = _get_instance_record(instance_id)
    if not record:
        logger.error(f"Instance {instance_id} not found in DynamoDB")
        return {"status": "error", "message": "Instance not found"}

    secret_arn = record.get("secret_arn", "")
    if not secret_arn:
        logger.error(f"No secret_arn for {instance_id}")
        return {"status": "error", "message": "No secret_arn configured"}

    # 2. The reach session (cross-account support) and the credentials
    cross_account_role = _resolve_cross_account_role(record)
    session = _reach_session(
        cross_account_role,
        # sts:ExternalId is the id of the admin who owns the reach-provider space,
        # denormalised onto the record when reach is set (see set_reach_space).
        owner_admin=record.get("reach_owner_admin", ""),
    )
    creds = _get_db_credentials(session, secret_arn)

    # 3. Members: re-read the cluster (failover detection), then check host routes
    synced = True
    if _is_cluster(record):
        record, synced = sync_members(record, session)
    record = check_host_routes(record)

    # 4. Connect to every member and collect
    targets, skipped = _member_targets(record, synced)
    rows, outcomes = _collect_all(creds, record, targets, context)
    outcomes.update(skipped)

    # 5. Write to the metrics store (DynamoDB rows + CloudWatch metrics)
    if not rows:
        return {"status": "error", "instance_id": instance_id, "members": outcomes,
                "message": "no member could be collected"}
    slot = slot_time(event)
    if slot:
        for r in rows:
            r["scan_time"] = slot
    # Per-interval counters (deadlocks, transaction rate, rollback ratio) need the
    # member's previous sample; read it before the write so this run's rows carry them.
    attach_counter_rates(instance_id, rows)
    try:
        write_to_store(instance_id, rows, record.get("retention_days"))
    except Exception as e:
        logger.error(f"Failed to write metrics for {instance_id}: {e}")
        message = (f"The metrics store write failed ({type(e).__name__}); no Tier 1 rows were "
                   f"written this run. Check the collector's permissions on the Tier 1 table.")
        _set_store_error(record, message)
        for mid, outcome in outcomes.items():
            if outcome == "ok":
                outcomes[mid] = "collected, not written: the store write failed"
        return {"status": "error", "instance_id": instance_id, "members": outcomes,
                "message": message}
    _set_store_error(record, "")
    # Retention is the items' TTL (set at write time from retention_days); nothing to prune.

    # Partial when a member FAILED (or ran out of time); a deliberate skip (not
    # reachable on this network path) is not a failure.
    failed = [m for m, o in outcomes.items() if o != "ok" and not
              (o.startswith("skipped:") and o != "skipped: time budget")]
    writer = next((r for r in rows if r.get("member_role") == "writer"), rows[0])
    logger.info(f"Tier 1 collection complete for {instance_id}: {len(rows)} member(s), "
                f"cache={writer.get('cache_hit_ratio')}%, conns={writer.get('total_connections')}")
    return {
        "status": "partial" if failed else "ok",
        "instance_id": instance_id,
        "members": outcomes,
        "metrics_summary": {
            "cache_hit_ratio": writer.get("cache_hit_ratio"),
            "total_connections": writer.get("total_connections"),
            "xid_age_pct": writer.get("xid_age_pct"),
            "blocked_queries": writer.get("blocked_queries"),
        },
    }
