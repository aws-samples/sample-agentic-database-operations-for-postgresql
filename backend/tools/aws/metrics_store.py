# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The metrics store: DynamoDB for records, CloudWatch for time series.

Replaces the Aurora Serverless v2 store. Two tables and one CloudWatch namespace:

* ``<prefix>-tier1-metrics`` — one item per cluster member per Tier 1 run: every numeric
  the collector measures plus the two lists (``table_bloat``, ``wait_events``). Short
  lived (``expires_at`` TTL, default 30 days): this is the detail view and the agent's
  "what happened last night", not the long-term series.
* ``<prefix>-tier2-scans`` — one ``summary`` item per deep scan (health, findings,
  headline metrics, the report key) and one item per report SECTION (overview,
  configuration, activity, replication, footprint, performance, maintenance,
  optimization, security; ``reader`` for a reader scan). Kept for years; this is the
  structured deep-scan history the agent and the report panel read.
* CloudWatch custom metrics (``<prefix>/Metrics`` namespace) — every Tier 1 numeric and the Tier 2
  daily numerics, published by the collectors next to the DynamoDB write. CloudWatch
  keeps 5-minute points for 63 days and hourly to 15 months, and does the per-bucket
  averaging the charts need in one ``GetMetricData`` call; alarms hang off the same
  metrics. Two dimension sets per metric: ``{InstanceId, Member}`` (one member) and
  ``{InstanceId, Role}`` (writer / reader): the role series is continuous across a
  failover, which is what the SQL ``member_role`` filter gave us before.

Key design. Tier 1: ``PK = instance_id``, ``SK = <member_instance_id>#<scan_time>``, so a
member's history is one key range and a run's rows are idempotent (the same scheduled
slot writes the same keys). Tier 2: ``PK = instance_id``, ``SK = <section>#<scan_time>#
<member_instance_id>``, so the newest summaries are one newest-first Query and a scan's
sections are direct gets. ``scan_time`` is stored canonically (UTC, milliseconds) so the
string order of a key IS its time order. A sparse GSI on ``report_s3_key`` (summary
items only) answers "the scan behind this report".

Who reads what. Every read takes the database's MEMBER list (from the record): per-member
key ranges replace the SQL ``WHERE instance_id = ..`` scan, and a member the record no
longer lists simply ages out. Reads of other tenants' data are impossible by
construction only because every caller scopes the instance ids first — the routes and
the agent tools keep that gate; this module trusts its ``instance_id`` arguments.

Nothing here has a schema to upgrade: a new metric is a new attribute. Fail-soft only
where the old store was (``is_configured`` false → the surfaces say "not configured").
"""

from __future__ import annotations

import logging
import math
import os
import time
import concurrent.futures
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable, Optional

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger("metrics-store")

# ── Configuration ─────────────────────────────────────────────────────────────


def _region() -> str:
    return os.getenv("AWS_REGION", "us-east-1")


def _table(key: str, env: str) -> str:
    """A store table's name: the explicit env override when set, else the registry name
    derived from RESOURCE_PREFIX (``<prefix>-tier1-metrics`` / ``<prefix>-tier2-scans``).
    '' only when neither is available (a packaging without resource_names)."""
    explicit = (os.getenv(env) or "").strip()
    if explicit:
        return explicit
    try:
        from tools.resource_names import table_name
        return table_name(key)
    except Exception:  # noqa: BLE001
        return ""


def tier1_table_name() -> str:
    return _table("tier1_metrics", "TIER1_METRICS_TABLE")


def tier2_table_name() -> str:
    return _table("tier2_scans", "TIER2_SCANS_TABLE")


def cw_namespace() -> str:
    """``<prefix>/Metrics``: per installation, like ``<prefix>/Audit``, so two installs in
    one account never mix series (and the collectors' PutMetricData grant can be pinned
    to this install's namespace). CW_METRICS_NAMESPACE overrides."""
    explicit = (os.getenv("CW_METRICS_NAMESPACE") or "").strip()
    if explicit:
        return explicit
    try:
        from tools.resource_names import resource_prefix
        prefix = resource_prefix()
    except Exception:  # noqa: BLE001 - a packaging gap must not stop a collection
        prefix = os.getenv("RESOURCE_PREFIX") or "dbops"
    return f"{prefix}/Metrics"


def is_configured() -> bool:
    """True when the deployment declares the store: both record tables named in the
    environment (the templates always set them). Not inferred from the prefix alone, so
    a local run without the stack says "not configured" instead of failing on AWS."""
    return bool((os.getenv("TIER1_METRICS_TABLE") or "").strip()
                and (os.getenv("TIER2_SCANS_TABLE") or "").strip())


_ddb = None
_cw = None


def _dynamodb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb", region_name=_region())
    return _ddb


def _cloudwatch():
    global _cw
    if _cw is None:
        _cw = boto3.client("cloudwatch", region_name=_region())
    return _cw


def _t1():
    return _dynamodb().Table(tier1_table_name())


def _t2():
    return _dynamodb().Table(tier2_table_name())


# ── Time and value canon ──────────────────────────────────────────────────────

def canon_ts(value) -> str:
    """A scan time as the store writes it: aware UTC ISO-8601 with millisecond precision
    (``2026-10-07T18:35:00.000+00:00``), fixed width, so key order is time order.
    Accepts a datetime or any ISO string (``Z`` allowed); ``None``/'' → now."""
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value or "").strip()
        if not s:
            dt = datetime.now(timezone.utc)
        else:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _to_ddb(value):
    """Floats → Decimal (DynamoDB has no float); nested structures recursively; NaN/inf
    and None are dropped by the caller via ``_clean``."""
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, dict):
        return {k: _to_ddb(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_to_ddb(v) for v in value]
    return value


def _from_ddb(value):
    """Decimal → int/float so rows are JSON-friendly and match what the SQL path returned."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _from_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_ddb(v) for v in value]
    return value


def _clean(item: dict) -> dict:
    out = {}
    for k, v in item.items():
        if v is None:
            continue
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            continue
        out[k] = _to_ddb(v)
    return out


def _num(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


# ── Tier 1: what the collector measures ───────────────────────────────────────
# The numeric columns, in one place: the collector writes them, CloudWatch gets them, the
# charts and the agent read them by these names.
TIER1_NUMERIC_METRICS = (
    "cache_hit_ratio", "dead_tuple_pct", "xid_age_pct",
    "total_connections", "active_connections", "idle_connections",
    "idle_in_txn_connections", "waiting_queries", "blocked_queries",
    "database_size_mb", "autovacuum_workers_active", "tables_needing_vacuum",
    "longest_transaction_sec", "replica_lag_ms",
    # Added with the DynamoDB/CloudWatch store: cheap pg_stat_database counters.
    "deadlocks", "deadlocks_delta", "xact_commit", "xact_rollback",
    "txn_per_sec", "rollback_ratio_pct", "temp_files", "temp_bytes_mb",
    # Connections as a share of max_connections (the connection-saturation alarm).
    "connections_used_pct",
)
TIER1_LIST_FIELDS = ("table_bloat", "wait_events")

_DEFAULT_TIER1_TTL_DAYS = 30


def _tier1_ttl_days() -> int:
    try:
        return max(1, int(os.getenv("TIER1_RETENTION_DAYS") or _DEFAULT_TIER1_TTL_DAYS))
    except ValueError:
        return _DEFAULT_TIER1_TTL_DAYS


def tier1_sk(member_instance_id: str, scan_time: str) -> str:
    return f"{member_instance_id}#{scan_time}"


def put_tier1_rows(instance_id: str, rows: list[dict], retention_days=None) -> int:
    """Write one item per collected member. Idempotent: the key is the member and the
    run's slot time, so a retried invocation rewrites the same items. Returns the count.
    ``retention_days`` (the database's monitoring setting) sets the TTL; default 30."""
    if not rows:
        return 0
    try:
        days = int(retention_days) if retention_days else _tier1_ttl_days()
    except (TypeError, ValueError):
        days = _tier1_ttl_days()
    days = max(1, days)
    table = _t1()
    n = 0
    with table.batch_writer(overwrite_by_pkeys=["instance_id", "sk"]) as batch:
        for m in rows:
            ts = canon_ts(m.get("scan_time"))
            member = m.get("member_instance_id") or instance_id
            expires = int(_parse_ts(ts).timestamp()) + days * 86400
            item = {
                "instance_id": instance_id,
                "sk": tier1_sk(member, ts),
                "scan_time": ts,
                "member_instance_id": member,
                "member_role": m.get("member_role") or "writer",
                "expires_at": expires,
            }
            for k in TIER1_NUMERIC_METRICS:
                item[k] = _num(m.get(k))
            for k in TIER1_LIST_FIELDS:
                v = m.get(k)
                item[k] = v if isinstance(v, list) else []
            batch.put_item(Item=_clean(item))
            n += 1
    return n


def _row_from_item(item: dict) -> dict:
    row = _from_ddb(dict(item))
    row.pop("expires_at", None)
    sk = row.pop("sk", "")
    row["id"] = sk
    return row


def _member_rows(instance_id: str, member: str, lo: str, hi: str, newest_first: bool,
                 limit: Optional[int], role: Optional[str] = None,
                 hi_exclusive: bool = False) -> list[dict]:
    """One member's Tier 1 items with ``lo <= scan_time <= hi`` (``hi`` exclusive on
    request), optionally only those observed in ``role``. Pages internally until
    ``limit`` rows (after the role filter) or the range is exhausted."""
    table = _t1()
    cond = Key("instance_id").eq(instance_id) & Key("sk").between(
        tier1_sk(member, lo), tier1_sk(member, hi))
    kwargs = {"KeyConditionExpression": cond, "ScanIndexForward": not newest_first}
    if role:
        from boto3.dynamodb.conditions import Attr
        kwargs["FilterExpression"] = Attr("member_role").eq(role)
    if limit:
        kwargs["Limit"] = max(limit * 2, 50)
    out: list[dict] = []
    while True:
        resp = table.query(**kwargs)
        for it in resp.get("Items", []):
            row = _row_from_item(it)
            if hi_exclusive and row.get("scan_time") == hi:
                continue
            out.append(row)
            if limit and len(out) >= limit:
                return out
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return out
        kwargs["ExclusiveStartKey"] = lek


def _window(hours: float) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    return canon_ts(now - timedelta(hours=hours)), canon_ts(now + timedelta(minutes=5))


def member_filter(selector: str, is_cluster: bool, members: list[str]) -> tuple[list[str], Optional[str]]:
    """``(member_ids, role)`` for a member selector ('' | writer | reader | instance:<id>):
    which members' key ranges to read and which observed role to keep (None = any). A
    single instance is its own one member: a role selector does not narrow it."""
    s = (selector or "").strip().lower()
    if s.startswith("instance:"):
        return [s[len("instance:"):]], None
    if not is_cluster:
        return list(members), None
    if s == "":
        return list(members), "writer"
    if s in ("writer", "reader"):
        return list(members), s
    return list(members), None


def _parallel(fn, args_list, max_workers=8):
    if len(args_list) <= 1:
        return [fn(*a) for a in args_list]
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(args_list))) as pool:
        return list(pool.map(lambda a: fn(*a), args_list))


def _cursor_parts(before: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """A page cursor ``<scan_time>|<member>`` (or a bare scan_time) → (ts, member)."""
    if not before or not str(before).strip():
        return None, None
    text = str(before).strip()
    if "|" in text:
        ts, _, member = text.partition("|")
        return canon_ts(ts), member or None
    return canon_ts(text), None


def row_cursor(row: dict) -> Optional[str]:
    ts = row.get("scan_time")
    if not ts:
        return None
    return f"{ts}|{row.get('member_instance_id') or ''}"


def tier1_history(instance_id: str, members: list[str], hours: float, limit: int,
                  before: Optional[str] = None, selector: str = "", is_cluster: bool = False) -> list[dict]:
    """Tier 1 rows for one database, newest first, within the last ``hours``; keyset
    paged on ``(scan_time, member)`` so a page ending inside one collection slot never
    skips that slot's remaining members."""
    lo, hi = _window(hours)
    ids, role = member_filter(selector, is_cluster, members)
    cts, cmember = _cursor_parts(before)
    if cts and cts < hi:
        hi = cts

    def one(m):
        rows = _member_rows(instance_id, m, lo, hi, True, limit, role, hi_exclusive=bool(cts))
        if cts and cmember is not None:
            # Rows at the cursor's own slot: only members sorting before the cursor member.
            rows = [r for r in rows if not (r.get("scan_time") == cts and m >= cmember)]
            # The slot itself was excluded above (hi_exclusive); re-admit members < cursor.
            if m < cmember:
                extra = _member_rows(instance_id, m, cts, cts, True, 1, role)
                rows = sorted(extra + rows, key=lambda r: r.get("scan_time", ""), reverse=True)
        return rows

    merged = [r for rows in _parallel(one, [(m,) for m in ids]) for r in rows]
    merged.sort(key=lambda r: (r.get("scan_time", ""), r.get("member_instance_id", "")), reverse=True)
    return merged[:limit]


def tier1_count(instance_id: str, members: list[str], hours: float, selector: str = "",
                is_cluster: bool = False) -> int:
    lo, hi = _window(hours)
    ids, role = member_filter(selector, is_cluster, members)
    table = _t1()

    def one(m):
        cond = Key("instance_id").eq(instance_id) & Key("sk").between(tier1_sk(m, lo), tier1_sk(m, hi))
        kwargs = {"KeyConditionExpression": cond, "Select": "COUNT"}
        if role:
            from boto3.dynamodb.conditions import Attr
            kwargs["FilterExpression"] = Attr("member_role").eq(role)
        total = 0
        while True:
            resp = table.query(**kwargs)
            total += int(resp.get("Count", 0))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                return total
            kwargs["ExclusiveStartKey"] = lek

    return sum(_parallel(one, [(m,) for m in ids]))


def tier1_newest_per_member(instance_id: str, members: list[str], hours: float = 168) -> list[dict]:
    """Each member's newest Tier 1 row within ``hours`` (the members table, the fleet
    cards). One row per member that has any."""
    lo, hi = _window(hours)
    rows = _parallel(lambda m: _member_rows(instance_id, m, lo, hi, True, 1), [(m,) for m in members])
    return [r[0] for r in rows if r]


def tier1_previous(instance_id: str, member: str, scan_time, hours: float = 24) -> Optional[dict]:
    """The member's newest sample strictly BEFORE ``scan_time`` (within ``hours``): the
    baseline for the collector's per-interval counters. Strictly before, so a retried run
    of a slot already written compares against the slot before it, not against itself."""
    hi = canon_ts(scan_time)
    lo = canon_ts(_parse_ts(hi) - timedelta(hours=hours))
    rows = _member_rows(instance_id, member, lo, hi, True, 1, hi_exclusive=True)
    return rows[0] if rows else None


def tier1_oldest_in_window(instance_id: str, members: list[str], hours: float, selector: str = "",
                           is_cluster: bool = False) -> Optional[dict]:
    lo, hi = _window(hours)
    ids, role = member_filter(selector, is_cluster, members)
    rows = _parallel(lambda m: _member_rows(instance_id, m, lo, hi, False, 1, role), [(m,) for m in ids])
    flat = [r[0] for r in rows if r]
    return min(flat, key=lambda r: r.get("scan_time", "")) if flat else None


# ── CloudWatch publish ────────────────────────────────────────────────────────

def _metric_unit(name: str) -> str:
    if name.endswith("_pct") or name.endswith("_ratio") or name == "cache_hit_ratio":
        return "Percent"
    if name.endswith("_mb"):
        return "Megabytes"
    if name.endswith("_ms"):
        return "Milliseconds"
    if name.endswith("_per_sec"):     # before "_sec": txn_per_sec is a rate, not a duration
        return "Count/Second"
    if name.endswith("_sec"):
        return "Seconds"
    return "Count"


def _put_metric_data(data: list[dict]) -> int:
    cw = _cloudwatch()
    n = 0
    for i in range(0, len(data), 1000):   # PutMetricData accepts 1000 datums per call
        chunk = data[i:i + 1000]
        cw.put_metric_data(Namespace=cw_namespace(), MetricData=chunk)
        n += len(chunk)
    return n


def publish_tier1_metrics(instance_id: str, rows: list[dict]) -> int:
    """Publish every Tier 1 numeric of every collected member to CloudWatch, under both
    dimension sets. Returns the datum count. Never raises past the caller's log: a
    CloudWatch hiccup must not fail a collection whose DynamoDB write succeeded."""
    data = []
    for m in rows:
        ts = _parse_ts(canon_ts(m.get("scan_time")))
        member = m.get("member_instance_id") or instance_id
        role = m.get("member_role") or "writer"
        for k in TIER1_NUMERIC_METRICS:
            v = _num(m.get(k))
            if v is None:
                continue
            for dims in (
                [{"Name": "InstanceId", "Value": instance_id}, {"Name": "Member", "Value": member}],
                [{"Name": "InstanceId", "Value": instance_id}, {"Name": "Role", "Value": role}],
            ):
                data.append({"MetricName": k, "Dimensions": dims, "Timestamp": ts,
                             "Value": v, "Unit": _metric_unit(k), "StorageResolution": 60})
    if not data:
        return 0
    return _put_metric_data(data)


def publish_collection_status(instance_id: str, ok: bool, when=None) -> int:
    """One datum per Tier 1 run: ``collection_failed`` 0 (collected) or 1 (the run could
    not collect the database), under the InstanceId-only dimension set. The
    collection-failing alarm reads it, so a broken collection is a value rather than a
    gap (a gap never alarms: a paused or new database is not broken)."""
    ts = _parse_ts(canon_ts(when))
    _put_metric_data([{"MetricName": "collection_failed",
                       "Dimensions": [{"Name": "InstanceId", "Value": instance_id}],
                       "Timestamp": ts, "Value": 0.0 if ok else 1.0, "Unit": "Count",
                       "StorageResolution": 60}])
    return 1


def publish_tier2_metrics(instance_id: str, member: str, role: str, scan_time, metrics: dict) -> int:
    """Publish a deep scan's daily numerics (the Tier 2 set, see run_scan) to CloudWatch."""
    ts = _parse_ts(canon_ts(scan_time))
    data = []
    for k, raw in (metrics or {}).items():
        v = _num(raw)
        if v is None:
            continue
        for dims in (
            [{"Name": "InstanceId", "Value": instance_id}, {"Name": "Member", "Value": member}],
            [{"Name": "InstanceId", "Value": instance_id}, {"Name": "Role", "Value": role}],
        ):
            data.append({"MetricName": k, "Dimensions": dims, "Timestamp": ts,
                         "Value": v, "Unit": _metric_unit(k)})
    return _put_metric_data(data) if data else 0


# ── CloudWatch read (charts, fleet series, the agent's long windows) ──────────

MAX_SERIES_POINTS = 300
MAX_SERIES_HOURS = 24 * 450      # CloudWatch keeps hourly points for 15 months


def chart_period(hours: float, max_points: int = MAX_SERIES_POINTS) -> int:
    """A GetMetricData period (seconds) for a window: at most ~``max_points`` buckets, and
    never finer than CloudWatch keeps for that age (60 s to 15 days, 300 s to 63 days,
    3600 s beyond)."""
    hours = max(1.0, float(hours))
    base = int(math.ceil(hours * 3600 / max(1, max_points)))
    floor = 60 if hours <= 24 * 15 else (300 if hours <= 24 * 63 else 3600)
    p = max(base, floor, 60)
    unit = 60 if p <= 300 else 300
    return int(math.ceil(p / unit) * unit)


def _series_queries(instance_id: str, dims_list: list[tuple[str, dict]], metrics: Iterable[str],
                    period: int, stats: Iterable[str]) -> list[dict]:
    """MetricDataQueries: one per (dimension set, metric, stat). Ids encode the triple."""
    q = []
    for label, dims in dims_list:
        dimensions = [{"Name": "InstanceId", "Value": instance_id}] + [
            {"Name": k, "Value": v} for k, v in dims.items()]
        for mi, name in enumerate(metrics):
            for stat in stats:
                q.append({
                    "Id": f"q_{len(q)}",
                    "Label": f"{label}|{name}|{stat}",
                    "MetricStat": {
                        "Metric": {"Namespace": cw_namespace(), "MetricName": name,
                                   "Dimensions": dimensions},
                        "Period": period, "Stat": stat,
                    },
                    "ReturnData": True,
                })
    return q


def _get_metric_data(queries: list[dict], start: datetime, end: datetime) -> dict[str, list[tuple[datetime, float]]]:
    """Run ``queries`` (chunked to 500 per call, paginated) and return
    ``{label: [(timestamp, value), ...]}`` oldest first."""
    cw = _cloudwatch()
    out: dict[str, list[tuple[datetime, float]]] = {}
    for i in range(0, len(queries), 500):
        chunk = queries[i:i + 500]
        token = None
        while True:
            kwargs = {"MetricDataQueries": chunk, "StartTime": start, "EndTime": end,
                      "ScanBy": "TimestampAscending"}
            if token:
                kwargs["NextToken"] = token
            resp = cw.get_metric_data(**kwargs)
            for r in resp.get("MetricDataResults", []):
                series = out.setdefault(r.get("Label", ""), [])
                series.extend(zip(r.get("Timestamps", []), r.get("Values", [])))
            token = resp.get("NextToken")
            if not token:
                break
    for v in out.values():
        v.sort(key=lambda p: p[0])
    return out


def tier1_bucketed(instance_id: str, hours: float, selector: str, is_cluster: bool,
                   members_with_roles: list[dict], metrics: Iterable[str] = TIER1_NUMERIC_METRICS,
                   max_points: int = MAX_SERIES_POINTS) -> tuple[list[dict], int]:
    """Tier 1 metrics AVERAGED into fixed buckets for a chart, from CloudWatch, with per
    bucket min/max. Per member for a cluster (``members_with_roles``: the record's
    ``[{instance_id, role}]``); the writer selector reads the continuous Role=writer
    series. Returns ``(rows, period_seconds)``; rows carry ``scan_time``,
    ``member_instance_id``, ``member_role``, ``samples`` and ``<metric>``,
    ``<metric>_min``, ``<metric>_max`` — the shape the SQL bucketed query returned."""
    hours = max(1.0, min(float(hours), MAX_SERIES_HOURS))
    period = chart_period(hours, max_points)
    s = (selector or "").strip().lower()
    dims_list: list[tuple[str, dict]] = []
    if s.startswith("instance:"):
        mid = s[len("instance:"):]
        role = next((m.get("role") for m in members_with_roles if m.get("instance_id") == mid), "")
        dims_list.append((f"{mid}|{role}", {"Member": mid}))
    elif not is_cluster:
        m0 = members_with_roles[0] if members_with_roles else {"instance_id": instance_id, "role": "writer"}
        dims_list.append((f"{m0.get('instance_id')}|{m0.get('role', 'writer')}", {"Member": m0.get("instance_id")}))
    elif s in ("", "writer"):
        dims_list.append(("writer|writer", {"Role": "writer"}))
    elif s == "reader":
        for m in members_with_roles:
            if m.get("role") == "reader":
                dims_list.append((f"{m.get('instance_id')}|reader", {"Member": m.get("instance_id")}))
    if not dims_list:
        return [], period
    metrics = list(metrics)
    stats = ("Average", "Minimum", "Maximum", "SampleCount")
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    queries = _series_queries(instance_id, dims_list, metrics, period, stats)
    series = _get_metric_data(queries, start, end)
    rows: dict[tuple, dict] = {}
    for label, points in series.items():
        # Label = "<member or writer>|<role>|<metric>|<stat>" (see _series_queries).
        parts = label.split("|")
        if len(parts) != 4:
            continue
        member_label, member_role, name, stat = parts
        for ts, val in points:
            key = (ts, member_label)
            row = rows.setdefault(key, {
                "scan_time": ts.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
                "member_instance_id": None if member_label == "writer" else member_label,
                "member_role": member_role, "samples": 0})
            if stat == "Average":
                row[name] = round(val, 2)
            elif stat == "Minimum":
                row[f"{name}_min"] = round(val, 2)
            elif stat == "Maximum":
                row[f"{name}_max"] = round(val, 2)
            elif stat == "SampleCount":
                row["samples"] = max(row["samples"], int(val))
    out = sorted(rows.values(), key=lambda r: (r["scan_time"], r.get("member_instance_id") or ""))
    return out, period


def fleet_daily_series(instance_ids: list[str], days: int, metrics: Iterable[str]) -> dict[str, list[dict]]:
    """Per database, one point per day (daily Average of the writer's series) over the
    window, for the fleet Trends lines. ``{instance_id: [{date, time, <metric>...}]}``."""
    metrics = list(metrics)
    if not instance_ids or not metrics:
        return {}
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=max(1, int(days)))
    queries = []
    for iid in instance_ids:
        for name in metrics:
            queries.append({
                "Id": f"q_{len(queries)}", "Label": f"{iid}|{name}",
                "MetricStat": {"Metric": {"Namespace": cw_namespace(), "MetricName": name,
                                          "Dimensions": [{"Name": "InstanceId", "Value": iid},
                                                         {"Name": "Role", "Value": "writer"}]},
                               "Period": 86400, "Stat": "Average"},
                "ReturnData": True})
    series = _get_metric_data(queries, start, end)
    by_db: dict[str, dict[str, dict]] = {}
    for label, points in series.items():
        if not points:
            continue
        iid, name = label.split("|", 1)
        days_map = by_db.setdefault(iid, {})
        for ts, val in points:
            date = ts.astimezone(timezone.utc).date().isoformat()
            pt = days_map.setdefault(date, {"date": date, "time": ts.astimezone(timezone.utc).isoformat()})
            pt[name] = round(val, 2)
    return {iid: [days_map[d] for d in sorted(days_map)] for iid, days_map in by_db.items()}


def series_points(instance_id: str, member: Optional[str], role: Optional[str], hours: float,
                  metrics: Iterable[str], max_points: int = 60) -> list[dict]:
    """A compact series for the agent: ``[{scan_time, <metric>...}]`` averaged into at
    most ``max_points`` buckets over ``hours`` (up to 15 months), for one member or one
    role (default: the writer)."""
    hours = max(1.0, min(float(hours), MAX_SERIES_HOURS))
    period = chart_period(hours, max_points)
    dims = {"Member": member} if member else {"Role": role or "writer"}
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    queries = _series_queries(instance_id, [("s", dims)], list(metrics), period, ("Average",))
    series = _get_metric_data(queries, start, end)
    rows: dict[datetime, dict] = {}
    for label, points in series.items():
        name = label.split("|")[1]
        for ts, val in points:
            rows.setdefault(ts, {"scan_time": ts.astimezone(timezone.utc).isoformat(timespec="seconds")})[name] = round(val, 2)
    return [rows[k] for k in sorted(rows)]


# ── Tier 2: deep-scan summaries and sections ──────────────────────────────────

TIER2_SECTIONS = ("overview", "configuration", "activity", "replication", "footprint",
                  "performance", "maintenance", "optimization", "security", "reader")
# The Tier 2 columns the list views carry (the SQL RESULT_COLUMNS), now item attributes.
TIER2_SUMMARY_FIELDS = (
    "instance_id", "account_id", "region", "engine", "display_name",
    "scan_time", "health_score", "summary", "report_s3_key",
    "cache_hit_ratio", "dead_tuple_pct", "xid_age_pct",
    "total_connections", "database_size_mb", "unused_indexes_count",
    "index_cache_hit_ratio", "deadlocks",
    "findings", "created_at", "member_instance_id", "member_role", "scan_kind",
    "sections", "metrics",
)


def tier2_sk(section: str, scan_time: str, member: str) -> str:
    return f"{section}#{scan_time}#{member}"


def scan_id(instance_id: str, scan_time: str, member: str) -> str:
    """The opaque id of one deep scan (what the apps hand back for compare and detail)."""
    return f"{instance_id}|{canon_ts(scan_time)}|{member}"


def parse_scan_id(sid: str) -> tuple[str, str, str]:
    parts = str(sid or "").split("|")
    if len(parts) != 3 or not all(parts):
        raise ValueError("Scan id must be <instance>|<time>|<member>")
    return parts[0], canon_ts(parts[1]), parts[2]


def put_tier2_scan(instance_id: str, summary: dict, sections: dict[str, dict]) -> str:
    """Write a deep scan: the ``summary`` item plus one item per section. ``summary``
    carries the TIER2_SUMMARY_FIELDS (scan_time, member_instance_id, member_role,
    scan_kind required); ``sections`` is ``{section_name: {...}}``. A section item is
    the section's structured data alone (plus the keys); a section bigger than the item
    limit is stored truncated with ``truncated: true``. Returns the scan id."""
    ts = canon_ts(summary.get("scan_time"))
    member = summary.get("member_instance_id") or instance_id
    table = _t2()
    base = {"instance_id": instance_id, "scan_time": ts, "member_instance_id": member,
            "member_role": summary.get("member_role") or "writer",
            "scan_kind": summary.get("scan_kind") or "full"}
    written = []
    for name, body in (sections or {}).items():
        if name not in TIER2_SECTIONS or body is None:
            continue
        item = dict(base, sk=tier2_sk(name, ts, member), section=name, data=_shrink(body))
        table.put_item(Item=_clean(item))
        written.append(name)
    item = dict(base, sk=tier2_sk("summary", ts, member), section="summary",
                created_at=canon_ts(None), sections=written)
    for k in TIER2_SUMMARY_FIELDS:
        if k in summary and k not in item:
            item[k] = summary[k]
    item["scan_time"] = ts
    if not item.get("report_s3_key"):
        item.pop("report_s3_key", None)    # sparse GSI key: absent, never empty
    table.put_item(Item=_clean(item))
    return scan_id(instance_id, ts, member)


_ITEM_BUDGET = 350_000   # bytes; DynamoDB items are capped at 400 KB


def _shrink(body):
    """Keep a section under the item size cap by trimming its longest lists."""
    import json
    try:
        size = len(json.dumps(body, default=str))
    except (TypeError, ValueError):
        return body
    if size <= _ITEM_BUDGET or not isinstance(body, dict):
        return body
    body = dict(body)
    lists = sorted(((k, v) for k, v in body.items() if isinstance(v, list) and v),
                   key=lambda kv: -len(kv[1]))
    for k, v in lists:
        while len(v) > 5 and len(json.dumps(body, default=str)) > _ITEM_BUDGET:
            v = v[: max(5, len(v) // 2)]
            body[k] = v
        if len(json.dumps(body, default=str)) <= _ITEM_BUDGET:
            break
    if len(json.dumps(body, default=str)) > _ITEM_BUDGET:
        # Still too big (one huge scalar or nested blob): keep the keys, drop the values,
        # so the scan's other sections and its summary still land.
        body = {k: None for k in body}
    body["truncated"] = True
    return body


def _summary_from_item(item: dict) -> dict:
    row = _from_ddb(dict(item))
    row.pop("sk", None)
    row.pop("section", None)
    row["id"] = scan_id(row.get("instance_id", ""), row.get("scan_time", ""), row.get("member_instance_id", ""))
    return row


def _summaries(instance_id: str, lo: str, hi: str, newest_first: bool = True,
               limit: Optional[int] = None, predicate=None) -> list[dict]:
    """Summary items of one database with ``lo <= scan_time <= hi``. ``predicate`` keeps
    a row; the Query pages until ``limit`` kept rows or the range is exhausted."""
    table = _t2()
    cond = Key("instance_id").eq(instance_id) & Key("sk").between(f"summary#{lo}", f"summary#{hi}~")
    kwargs = {"KeyConditionExpression": cond, "ScanIndexForward": not newest_first}
    out: list[dict] = []
    while True:
        resp = table.query(**kwargs)
        for it in resp.get("Items", []):
            row = _summary_from_item(it)
            if predicate and not predicate(row):
                continue
            out.append(row)
            if limit and len(out) >= limit:
                return out
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return out
        kwargs["ExclusiveStartKey"] = lek


def _days_window(days: float) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    return canon_ts(now - timedelta(days=days)), canon_ts(now + timedelta(minutes=5))


def tier2_latest_full(instance_id: str, days: float = 3650) -> Optional[dict]:
    lo, hi = _days_window(days)
    rows = _summaries(instance_id, lo, hi, True, 1, lambda r: r.get("scan_kind") == "full")
    return rows[0] if rows else None


def tier2_latest_readers(instance_id: str, days: float = 30, member: Optional[str] = None) -> list[dict]:
    """The newest reader scan of each reader (or of ``member``) within ``days``."""
    lo, hi = _days_window(days)
    seen, out = set(), []
    for r in _summaries(instance_id, lo, hi, True, None,
                        lambda r: r.get("scan_kind") == "reader"
                        and (member is None or r.get("member_instance_id") == member)):
        m = r.get("member_instance_id")
        if m in seen:
            continue
        seen.add(m)
        out.append(r)
    return out


def _tier2_predicate(health: Optional[str], kind: str, selector: str, is_cluster: bool):
    s = (selector or "").strip().lower()

    def keep(r: dict) -> bool:
        if health and r.get("health_score") != health:
            return False
        if kind and r.get("scan_kind") != kind:
            return False
        if s.startswith("instance:"):
            return r.get("member_instance_id") == s[len("instance:"):]
        if is_cluster:
            if s == "":
                return r.get("member_role") == "writer"
            if s in ("writer", "reader"):
                return r.get("member_role") == s
        return True
    return keep


def tier2_list(instance_ids: list[str], days: float, limit: int, before: Optional[str] = None,
               health: Optional[str] = None, kind: str = "", selector: str = "",
               is_cluster: bool = False) -> list[dict]:
    """Deep-scan summaries (no section data) for databases, newest first, keyset paged
    on ``(scan_time, member)``."""
    lo, hi = _days_window(days)
    cts, cmember = _cursor_parts(before)
    if cts and cts < hi:
        hi = cts
    keep = _tier2_predicate(health, kind, selector, is_cluster)

    def one(iid):
        rows = _summaries(iid, lo, hi, True, limit + 5, keep)
        if cts:
            rows = [r for r in rows if r.get("scan_time") < cts
                    or (r.get("scan_time") == cts and cmember and (r.get("member_instance_id") or "") < cmember)]
        return rows[:limit]

    merged = [r for rows in _parallel(one, [(i,) for i in instance_ids]) for r in rows]
    merged.sort(key=lambda r: (r.get("scan_time", ""), r.get("member_instance_id", "")), reverse=True)
    return merged[:limit]


def tier2_count(instance_ids: list[str], days: float, health: Optional[str] = None, kind: str = "",
                selector: str = "", is_cluster: bool = False) -> int:
    lo, hi = _days_window(days)
    keep = _tier2_predicate(health, kind, selector, is_cluster)
    return sum(len(_summaries(iid, lo, hi, True, None, keep)) for iid in instance_ids)


def tier2_newest_per_day(instance_ids: list[str], days: float) -> dict[str, list[dict]]:
    """The newest FULL scan per database per UTC day over the window (Space Trends, the
    per-database trend): ``{instance_id: [summaries oldest first]}``."""
    lo, hi = _days_window(days)

    def one(iid):
        per_day: dict[str, dict] = {}
        for r in _summaries(iid, lo, hi, True, None, lambda r: r.get("scan_kind") == "full"):
            d = str(r.get("scan_time", ""))[:10]
            per_day.setdefault(d, r)     # newest first: the first seen per day wins
        return iid, [per_day[d] for d in sorted(per_day)]

    return dict(_parallel(one, [(i,) for i in instance_ids]))


def tier2_member_status(instance_id: str, days: float = 30) -> list[dict]:
    """Each member's newest scan (any kind) within ``days``."""
    lo, hi = _days_window(days)
    seen, out = set(), []
    for r in _summaries(instance_id, lo, hi, True, None):
        m = r.get("member_instance_id")
        if m in seen:
            continue
        seen.add(m)
        out.append(r)
    return out


def tier2_status(instance_ids: list[str], days: float = 30) -> dict[str, dict]:
    """Per database: the newest full scan's time and health, and the full-scan count over
    ``days`` (the admin Collection page)."""
    lo, hi = _days_window(days)

    def one(iid):
        rows = _summaries(iid, lo, hi, True, None, lambda r: r.get("scan_kind") == "full")
        newest = rows[0] if rows else None
        return iid, {"instance_id": iid,
                     "tier2_last_at": newest.get("scan_time") if newest else None,
                     "tier2_last_health": newest.get("health_score") if newest else None,
                     "tier2_count_30d": len(rows)}

    return dict(_parallel(one, [(i,) for i in instance_ids]))


def tier2_by_id(sid: str) -> Optional[dict]:
    iid, ts, member = parse_scan_id(sid)
    resp = _t2().get_item(Key={"instance_id": iid, "sk": tier2_sk("summary", ts, member)})
    item = resp.get("Item")
    return _summary_from_item(item) if item else None


def tier2_by_report_key(key: str) -> Optional[dict]:
    """The summary behind a report (sparse GSI on ``report_s3_key``), newest first."""
    resp = _t2().query(IndexName="by-report-key",
                       KeyConditionExpression=Key("report_s3_key").eq(key))
    items = resp.get("Items", [])
    if not items:
        return None
    rows = sorted((_summary_from_item(i) for i in items), key=lambda r: r.get("scan_time", ""), reverse=True)
    return rows[0]


def tier2_sections(instance_id: str, scan_time: str, member: str,
                   names: Optional[Iterable[str]] = None) -> dict[str, dict]:
    """The section data of one scan: ``{section: data}`` for ``names`` (default: all)."""
    ts = canon_ts(scan_time)
    wanted = [n for n in (names or TIER2_SECTIONS) if n in TIER2_SECTIONS]
    if not wanted:
        return {}
    keys = [{"instance_id": instance_id, "sk": tier2_sk(n, ts, member)} for n in wanted]
    out: dict[str, dict] = {}
    client = _dynamodb().meta.client
    table = tier2_table_name()
    pending = keys
    attempts = 0
    while pending:
        resp = client.batch_get_item(RequestItems={table: {"Keys": pending[:100]}})
        for it in resp.get("Responses", {}).get(table, []):
            row = _from_ddb(it)
            out[row.get("section")] = row.get("data")
        unprocessed = resp.get("UnprocessedKeys", {}).get(table, {}).get("Keys", [])
        pending = unprocessed + pending[100:]
        if unprocessed:
            attempts += 1
            if attempts > 5:
                raise RuntimeError("Tier 2 section read throttled")
            time.sleep(0.1 * 2 ** attempts)
    return out


def tier2_data_doc(summary: dict, names: Optional[Iterable[str]] = None) -> dict:
    """A scan's sections merged into one document (what the old ``data`` JSONB held),
    for the comparison and the report panel."""
    if not summary:
        return {}
    secs = tier2_sections(summary.get("instance_id", ""), summary.get("scan_time", ""),
                          summary.get("member_instance_id") or summary.get("instance_id", ""), names)
    doc: dict = {}
    for name, data in secs.items():
        if isinstance(data, dict):
            doc[name] = data
    return doc
