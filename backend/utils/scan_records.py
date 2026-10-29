# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Browsing the Tier 1 / Tier 2 records (the DynamoDB tables and CloudWatch series).

The clamping, member-selector grammar, paging, fleet-row picking and collection-status
classification behind the records views in both SPAs: the admin console's Monitoring ›
Collection page and the operator app's per-database Metrics / Deep scans tabs. The
store calls themselves live in ``tools.aws.metrics_store``; this module keeps the pure
logic testable without AWS and gives the routes one place to read from.

Paging is keyset on ``(scan_time, member)``: ``before`` is the previous page's oldest
row as ``<scan_time>|<member_instance_id>`` (``page_info``'s ``next_before``; opaque to
the apps). Not ``scan_time`` alone: Tier 1 writes one row per cluster member with the
same scan_time, so keying on ``scan_time`` alone would let a page ending inside one
collection slot skip that slot's remaining members for good.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, List, Optional, Tuple

from surfaces.scope import MEMBER_ID_RE

# ── Bounds (match the agent tool's clamp in tools/db_tools and the store's retention) ──
MAX_HOURS = 168          # 7 days of Tier 1 ROWS per request window (the detail table)
DEFAULT_HOURS = 24
MAX_DAYS = 365           # Tier 2 list window
DEFAULT_DAYS = 30
MAX_LIMIT = 500
DEFAULT_LIMIT = 100
DEFAULT_RESULTS_LIMIT = 200   # /api/scan-results rows per page when no limit is given
MAX_CHART_HOURS = 24 * 450    # CloudWatch keeps hourly points for 15 months

HEALTH_VALUES = ("ok", "warning", "critical", "unknown")

# Tier 1 columns as written by tier1_collector (+ the row id for keys). Kept for the
# tests and the apps' column lists; the store writes exactly these attributes.
METRIC_COLUMNS = (
    "id", "instance_id", "scan_time",
    "cache_hit_ratio", "dead_tuple_pct", "xid_age_pct",
    "total_connections", "active_connections", "idle_connections",
    "idle_in_txn_connections", "waiting_queries", "blocked_queries",
    "database_size_mb", "autovacuum_workers_active", "tables_needing_vacuum",
    "longest_transaction_sec", "table_bloat", "wait_events",
    "member_instance_id", "member_role", "replica_lag_ms",
    "deadlocks", "deadlocks_delta", "xact_commit", "xact_rollback",
    "txn_per_sec", "rollback_ratio_pct", "temp_files", "temp_bytes_mb",
    "connections_used_pct",
)

# Tier 2 list columns: the summary item (no section data, which the report views load
# per scan).
RESULT_COLUMNS = (
    "id", "instance_id", "account_id", "region", "engine", "display_name",
    "scan_time", "health_score", "summary", "report_s3_key",
    "cache_hit_ratio", "dead_tuple_pct", "xid_age_pct",
    "total_connections", "database_size_mb", "unused_indexes_count",
    "index_cache_hit_ratio", "deadlocks",
    "findings", "created_at",
    "member_instance_id", "member_role", "scan_kind", "sections",
)
SCAN_KINDS = ("full", "reader")
MEMBER_ERROR = "member must be '', 'writer', 'reader' or 'instance:<id>'"

# Cadence labels (as stored on the instance record) → minutes. Used to judge
# whether the newest row is recent enough for the configured schedule.
TIER1_MINUTES = {"1min": 1, "2min": 2, "5min": 5, "10min": 10, "15min": 15, "30min": 30}
TIER2_MINUTES = {"1h": 60, "3h": 180, "6h": 360, "12h": 720, "daily": 1440, "weekly": 10080}
TIER1_DEFAULT = "5min"
TIER2_DEFAULT = "daily"
# A row is "stale" once it is older than this many cadence intervals (+ a minute
# of scheduler jitter). Tier 1 is allowed one missed run; Tier 2 half an interval.
TIER1_STALE_FACTOR = 2.0
TIER2_STALE_FACTOR = 1.5
JITTER_SECONDS = 60


def clamp(value, low: int, high: int, default: int) -> int:
    """Coerce ``value`` to an int within [low, high]; ``default`` when unusable."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def parse_before(before: Optional[str]) -> Optional[str]:
    """Validate a keyset cursor's timestamp: an ISO-8601 timestamp (as the store emits
    them). Returns it normalised to an aware UTC ISO string, or ``None`` when no cursor
    was given. Raises ``ValueError`` for anything that is not a timestamp, so a route
    can answer 400 rather than passing junk to the store."""
    if before is None or not str(before).strip():
        return None
    s = str(before).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise ValueError("before must be an ISO-8601 timestamp") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_cursor(before: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Validate a keyset cursor: ``<ISO-8601 timestamp>|<member id>`` (``page_info``), or
    a bare timestamp. Returns ``(timestamp, member)`` with the timestamp normalised to
    aware UTC and ``member`` None for a bare timestamp; ``(None, None)`` when no cursor
    was given. Raises ``ValueError`` for anything else, so a route can answer 400."""
    if before is None or not str(before).strip():
        return None, None
    text = str(before).strip()
    if "|" in text:
        ts, _, member = text.partition("|")
        member = member.strip()
        if member and not MEMBER_ID_RE.fullmatch(member):
            raise ValueError("before must be a cursor from next_before")
        return parse_before(ts), (member or None)
    return parse_before(text), None


def normalise_cursor(before: Optional[str]) -> Optional[str]:
    """The cursor as echoed back (validated and normalised), or None."""
    ts, member = parse_cursor(before)
    if ts is None:
        return None
    return f"{ts}|{member}" if member else ts


# ── Cluster members ───────────────────────────────────────────────────────────
# A member selector, the same grammar as the agent's role focus: '' (the database as a
# whole: its writer for a cluster), 'writer', 'reader' (every reader), or
# 'instance:<id>' (one member). Rows carry the member's id and its OBSERVED role
# (pg_is_in_recovery at collection time), so 'writer' is continuous across a failover.


def parse_member(selector) -> str:
    """Validate a member selector. Returns it normalised (lower case, trimmed); raises
    ``ValueError`` with MEMBER_ERROR for anything else, so a route can answer 400."""
    s = str(selector or "").strip().lower()
    if s in ("", "writer", "reader"):
        return s
    if s.startswith("instance:") and MEMBER_ID_RE.fullmatch(s[len("instance:"):]):
        return s
    raise ValueError(MEMBER_ERROR)


def record_member_list(record: dict) -> List[dict]:
    """The record's members as ``[{instance_id, role, endpoint, availability_zone,
    instance_class}]``, writer first. A standalone record is its own one member (the
    last segment of its ARN, the role from whether RDS reports it as a replica)."""
    roles = record.get("cluster_member_roles") or []
    if roles:
        out = [{"instance_id": m.get("instance_id", ""),
                "role": "writer" if m.get("is_writer") else "reader",
                "endpoint": m.get("endpoint", ""),
                "availability_zone": m.get("availability_zone", ""),
                "instance_class": m.get("instance_class", "")} for m in roles]
        return sorted(out, key=lambda m: (m["role"] != "writer", m["instance_id"]))
    tags = record.get("tags") or {}
    arn = record.get("arn") or tags.get("arn", "")
    mid = arn.rsplit(":", 1)[-1] if arn else record.get("instance_id", "")
    return [{"instance_id": mid, "role": "reader" if tags.get("replica_of") else "writer",
             "endpoint": record.get("endpoint", ""), "availability_zone": "",
             "instance_class": tags.get("instance_class", "")}]


def member_ids(record: dict) -> List[str]:
    """The member ids whose key ranges hold this database's rows: the record's current
    members plus, for a standalone instance, the registry id (older rows and the
    single-instance fallback were tagged with it)."""
    ids = [m["instance_id"] for m in record_member_list(record) if m.get("instance_id")]
    iid = record.get("instance_id")
    if iid and iid not in ids:
        ids.append(iid)
    return ids


def members_with_roles(record: dict) -> List[dict]:
    return [{"instance_id": m["instance_id"], "role": m["role"]} for m in record_member_list(record)]


# ── Tier 1 reads ──────────────────────────────────────────────────────────────

def metrics_history(record: dict, hours=DEFAULT_HOURS, limit=DEFAULT_LIMIT,
                    before: Optional[str] = None, member: str = "") -> Tuple[List[dict], dict]:
    """Tier 1 rows for one database, newest first. Returns ``(rows, effective)`` where
    ``effective`` carries the clamped ``hours`` / ``limit`` and the normalised cursor so
    the route can echo them. Raises ``ValueError`` for a bad cursor or selector."""
    from tools.aws import metrics_store
    hours = clamp(hours, 1, MAX_HOURS, DEFAULT_HOURS)
    limit = clamp(limit, 1, MAX_LIMIT, DEFAULT_LIMIT)
    selector = parse_member(member)
    cursor = normalise_cursor(before)
    is_cluster = bool(record.get("cluster_member_roles"))
    rows = metrics_store.tier1_history(record["instance_id"], member_ids(record), hours, limit,
                                       before=cursor, selector=selector, is_cluster=is_cluster)
    return rows, {"hours": hours, "limit": limit, "before": cursor}


def metrics_history_count(record: dict, hours=DEFAULT_HOURS, member: str = "") -> Optional[int]:
    """How many Tier 1 rows the window holds in total, cursor aside, so a pager can show
    "page 3 of 29"; None when the store could not be read."""
    from tools.aws import metrics_store
    hours = clamp(hours, 1, MAX_HOURS, DEFAULT_HOURS)
    try:
        return metrics_store.tier1_count(record["instance_id"], member_ids(record), hours,
                                         selector=parse_member(member),
                                         is_cluster=bool(record.get("cluster_member_roles")))
    except Exception:  # noqa: BLE001 - a count failure degrades the pager, never the page
        return None


# The numeric Tier 1 metrics a chart draws, averaged per bucket with min/max. Every
# numeric the collector publishes is chartable; this is the order the pickers show.
BUCKET_METRICS = (
    "cache_hit_ratio", "dead_tuple_pct", "xid_age_pct",
    "total_connections", "active_connections", "idle_connections", "idle_in_txn_connections",
    "connections_used_pct",
    "waiting_queries", "blocked_queries", "database_size_mb",
    "autovacuum_workers_active", "tables_needing_vacuum", "longest_transaction_sec",
    "replica_lag_ms", "deadlocks_delta", "txn_per_sec", "rollback_ratio_pct",
    "temp_files", "temp_bytes_mb",
)

# The deep scan's daily numerics published to CloudWatch (batch/deep_sections.py
# tier2_metrics). Listed here so the per-database CloudWatch view and the fleet series
# can offer them by name; the server only ever queries a name on this list.
TIER2_SERIES_METRICS = (
    "health_score_num", "findings_total", "findings_critical", "findings_warning", "findings_info",
    "unused_indexes_count", "duplicate_indexes_count", "fk_without_index_count",
    "seq_scan_heavy_tables_count", "unused_tables_count", "invalid_indexes_count",
    "unvalidated_constraints_count", "max_sequence_used_pct", "stale_statistics_tables_count",
    "never_vacuumed_tables_count", "forced_checkpoint_ratio_pct",
    "autovacuum_worker_utilization_pct", "index_cache_hit_ratio",
    "top_query_total_exec_ms", "top_query_mean_exec_ms", "top_query_wal_mb",
    "temp_file_queries_count", "connection_utilization_pct",
    "settings_outside_recommendation_count", "high_risk_roles_count", "plain_connections_count",
)
# Every series a chart may ask for by name, Tier 1 and Tier 2 together.
ALL_SERIES_METRICS = BUCKET_METRICS + TIER2_SERIES_METRICS
# The two sets a per-database chart may read, by the route's ``series`` value.
SERIES_SETS = {"tier1": BUCKET_METRICS, "tier2": TIER2_SERIES_METRICS}
MAX_BUCKETS = 300          # at most ~300 points per chart window


def parse_series(raw: str) -> str:
    """Which published series a chart reads: ``tier1`` (the collector's numerics, the
    default) or ``tier2`` (the deep scan's daily numerics)."""
    s = (raw or "tier1").strip().lower()
    if s not in SERIES_SETS:
        raise ValueError("series must be 'tier1' or 'tier2'")
    return s


def metrics_bucketed(record: dict, hours=DEFAULT_HOURS, member: str = "",
                     max_buckets: int = MAX_BUCKETS, series: str = "tier1") -> Tuple[List[dict], dict]:
    """Metrics for one database AVERAGED into fixed time buckets from CloudWatch, so a
    chart gets at most ~``max_buckets`` points per window in one call. ``series`` picks
    the Tier 1 numerics (default) or the deep scan's daily numerics; both are published
    under the same per-member and per-role dimensions. Per member for a cluster, with
    per-bucket min/max. Oldest first. Windows up to 15 months. Returns
    ``(rows, effective)`` with the clamped ``hours``, ``bucket_seconds`` and the
    ``metrics`` the rows carry."""
    from tools.aws import metrics_store
    hours = clamp(hours, 1, MAX_CHART_HOURS, DEFAULT_HOURS)
    selector = parse_member(member)
    names = SERIES_SETS[parse_series(series)]
    rows, period = metrics_store.tier1_bucketed(
        record["instance_id"], hours, selector, bool(record.get("cluster_member_roles")),
        members_with_roles(record), metrics=names, max_points=max_buckets)
    return rows, {"hours": hours, "bucket_seconds": period, "metrics": list(names)}


def fleet_latest_rows(records: List[dict], hours: int = 24) -> List[dict]:
    """The newest Tier 1 row per database for the fleet cards: each member's newest row
    within ``hours``, then ``pick_fleet_rows`` keeps the writer's unless it is stale."""
    from tools.aws import metrics_store
    hours = clamp(hours, 1, MAX_HOURS, 24)
    rows: List[dict] = []
    for rec in records:
        try:
            rows.extend(metrics_store.tier1_newest_per_member(rec["instance_id"], member_ids(rec), hours))
        except Exception:  # noqa: BLE001 - one database's read failing leaves it off the cards
            continue
    return pick_fleet_rows(rows, {r["instance_id"]: r.get("tier1_frequency") for r in records})


# The Tier 1 metrics the fleet Trends series carries (the headline metrics the charts
# draw). Tier 1 has no findings or unused-index count — those stay Tier 2.
FLEET_SERIES_METRICS = ("cache_hit_ratio", "dead_tuple_pct", "xid_age_pct",
                        "total_connections", "database_size_mb", "blocked_queries",
                        "deadlocks_delta", "txn_per_sec", "rollback_ratio_pct",
                        "longest_transaction_sec", "replica_lag_ms")
MAX_FLEET_SERIES_DAYS = 450
# A fleet request fans out as databases x metrics CloudWatch queries, so a caller's pick
# is capped; the Trends page asks for one metric at a time.
MAX_FLEET_METRICS = 12


def parse_fleet_metrics(raw: str) -> tuple:
    """The fleet series' metric set: the headline set by default, or a caller-chosen
    subset of every published series (comma-separated). Only names on the server's own
    list are ever queried; anything else is a ValueError (a 400), never a CloudWatch
    query the caller shaped."""
    if not (raw or "").strip():
        return FLEET_SERIES_METRICS
    wanted = []
    for name in dict.fromkeys(x.strip() for x in str(raw).split(",")):
        if not name:
            continue
        if name not in ALL_SERIES_METRICS:
            raise ValueError(f"unknown metric: {name}")
        wanted.append(name)
    if len(wanted) > MAX_FLEET_METRICS:
        raise ValueError(f"at most {MAX_FLEET_METRICS} metrics per request")
    return tuple(wanted) or FLEET_SERIES_METRICS


def fleet_metrics_series(instance_ids: Iterable[str], days=DEFAULT_DAYS,
                         metrics: Iterable[str] = FLEET_SERIES_METRICS) -> Tuple[dict, int]:
    """Per database, one point per UTC day (the writer's daily average) over the window,
    from CloudWatch, so the fleet Trends draws a continuous series from the first
    collection. ``metrics`` is the headline set or a validated pick from
    ``ALL_SERIES_METRICS`` (see ``parse_fleet_metrics``). Returns
    ``({instance_id: [points]}, clamped_days)``."""
    from tools.aws import metrics_store
    ids = list(instance_ids)
    days = clamp(days, 1, MAX_FLEET_SERIES_DAYS, min(DEFAULT_DAYS, MAX_FLEET_SERIES_DAYS))
    if not ids:
        return {}, days
    series = metrics_store.fleet_daily_series(ids, days, tuple(metrics))
    for pts in series.values():
        for p in pts:
            if "total_connections" in p:
                p["connections"] = p.pop("total_connections")
    return series, days


# ── Tier 2 reads ──────────────────────────────────────────────────────────────

def scan_results(instance_ids: Iterable[str], days=DEFAULT_DAYS, limit=DEFAULT_RESULTS_LIMIT,
                 before: Optional[str] = None, health: Optional[str] = None, member: str = "",
                 is_cluster: bool = False, kind: str = "") -> Tuple[List[dict], dict]:
    """Deep-scan summaries (no section data) for a set of databases, newest first.
    ``health`` narrows to one health_score value; anything outside the known set raises
    ``ValueError`` so the route can answer 400. ``member`` narrows to a cluster member
    and ``kind`` to a scan kind ('full' or 'reader')."""
    from tools.aws import metrics_store
    ids = list(instance_ids)
    if not ids:
        raise ValueError("at least one instance_id is required")
    days = clamp(days, 1, MAX_DAYS, DEFAULT_DAYS)
    limit = clamp(limit, 1, MAX_LIMIT, DEFAULT_RESULTS_LIMIT)
    health = _health(health)
    if kind and kind not in SCAN_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(SCAN_KINDS)}")
    selector = parse_member(member)
    cursor = normalise_cursor(before)
    rows = metrics_store.tier2_list(ids, days, limit, before=cursor, health=health, kind=kind or "",
                                    selector=selector, is_cluster=is_cluster)
    return rows, {"days": days, "limit": limit, "before": cursor, "health": health}


def scan_results_count(instance_ids: Iterable[str], days=DEFAULT_DAYS, health: Optional[str] = None,
                       member: str = "", is_cluster: bool = False, kind: str = "") -> Optional[int]:
    from tools.aws import metrics_store
    ids = list(instance_ids)
    if not ids:
        return 0
    try:
        return metrics_store.tier2_count(ids, clamp(days, 1, MAX_DAYS, DEFAULT_DAYS), health=_health(health),
                                         kind=kind or "", selector=parse_member(member), is_cluster=is_cluster)
    except Exception:  # noqa: BLE001
        return None


def _health(health: Optional[str]) -> Optional[str]:
    if health is None or health == "":
        return None
    h = str(health).strip().lower()
    if h not in HEALTH_VALUES:
        raise ValueError(f"health must be one of: {', '.join(HEALTH_VALUES)}")
    return h


def space_trends(instance_ids: Iterable[str], days=DEFAULT_DAYS) -> Tuple[dict, int]:
    """The NEWEST full scan per database per calendar day over the window, for Space
    Trends: ``({instance_id: [summaries oldest first]}, clamped_days)``."""
    from tools.aws import metrics_store
    ids = list(instance_ids)
    if not ids:
        raise ValueError("at least one instance_id is required")
    days = clamp(days, 1, MAX_DAYS, DEFAULT_DAYS)
    return metrics_store.tier2_newest_per_day(ids, days), days


def page_info(rows: List[dict], limit: int, total: Optional[int] = None) -> dict:
    """Keyset paging hint for a newest-first page: when the page is full there
    may be more, and the next request passes ``next_before`` (the oldest row as
    ``<scan_time>|<member>``) as ``before``. ``total`` (the window's row count, when the
    route looked it up) lets a pager number its pages up front; it is omitted, not
    zeroed, when unknown."""
    n = len(rows)
    has_more = n >= limit and n > 0
    info = {
        "count": n,
        "has_more": has_more,
        "next_before": _row_cursor(rows[-1]) if has_more else None,
    }
    if total is not None:
        info["total"] = int(total)
    return info


def _row_cursor(row: dict) -> Optional[str]:
    ts = row.get("scan_time")
    if ts is None:
        return None
    member = row.get("member_instance_id") or ""
    return f"{ts}|{member}" if member else str(ts)


# ── Collection status (admin Monitoring › Collection) ─────────────────────────

def status_rows(records: List[dict]) -> Tuple[List[dict], List[dict]]:
    """Per database: Tier 1's newest row time and 24-hour row count (the WRITER's rows
    for a cluster, every row for a single instance), and Tier 2's newest full scan with
    its health and the 30-day count. Returns ``(tier1_rows, tier2_rows)`` in the shape
    ``merge_status`` consumes. Raises on a store failure (the route degrades)."""
    from tools.aws import metrics_store
    t1: List[dict] = []
    for rec in records:
        iid = rec["instance_id"]
        is_cluster = bool(rec.get("cluster_member_roles"))
        ids = member_ids(rec)
        # A COUNT query, not a row read: a cluster counts its writer's rows, a single
        # instance (a standalone read replica included) every row.
        count = metrics_store.tier1_count(iid, ids, 24, selector="", is_cluster=is_cluster)
        newest = metrics_store.tier1_newest_per_member(iid, ids, hours=24 * 7)
        if is_cluster:
            newest = [r for r in newest if r.get("member_role") == "writer"] or newest
        last_at = max((r.get("scan_time", "") for r in newest), default=None) or None
        t1.append({"instance_id": iid, "tier1_last_at": last_at, "tier1_count_24h": count})
    t2 = list(metrics_store.tier2_status([r["instance_id"] for r in records], 30).values())
    return t1, t2


def pick_fleet_rows(rows: List[dict], cadence_by_id: dict,
                    now: Optional[datetime] = None) -> List[dict]:
    """One Tier 1 row per database for the fleet cards, from up to two per database (its
    writer's newest and its newest of any other role). The writer's row is the database's
    reading while it is within one collection interval (the database's Tier 1 cadence);
    once it is older, the newest row of any role is, so a stale writer cannot make a
    cluster whose readers still report look quiet. Each card shows the row's age."""
    now = now or datetime.now(timezone.utc)
    by_db: dict = {}
    for r in rows or []:
        by_db.setdefault(r.get("instance_id"), []).append(r)
    out = []
    for iid, group in by_db.items():
        newest = max(group, key=lambda r: _age_key(r.get("scan_time")))
        writer = max((r for r in group if r.get("member_role") == "writer"),
                     key=lambda r: _age_key(r.get("scan_time")), default=None)
        minutes = TIER1_MINUTES.get(cadence_by_id.get(iid) or "", TIER1_MINUTES[TIER1_DEFAULT])
        age = _age_seconds(writer.get("scan_time"), now) if writer else None
        out.append(writer if writer is not None and age is not None and age <= minutes * 60 else newest)
    return out


def _age_key(last_at: Optional[str]) -> str:
    try:
        return parse_before(last_at) or ""
    except ValueError:
        return ""


def _age_seconds(last_at: Optional[str], now: datetime) -> Optional[float]:
    if not last_at:
        return None
    try:
        cursor = parse_before(last_at)
    except ValueError:
        return None
    return (now - datetime.fromisoformat(cursor)).total_seconds()


def collection_state(last_at: Optional[str], enabled, cadence_label: Optional[str],
                     tier: int, now: Optional[datetime] = None) -> str:
    """Classify one tier's collection for a database.

    ``paused`` — the tier is switched off; ``none`` — never collected;
    ``stale`` — the newest row is older than the schedule allows; ``fresh``
    otherwise. Unknown cadence labels fall back to the tier's default.
    """
    if enabled is False:
        return "paused"
    now = now or datetime.now(timezone.utc)
    age = _age_seconds(last_at, now)
    if age is None:
        return "none"
    if tier == 1:
        minutes = TIER1_MINUTES.get(cadence_label or "", TIER1_MINUTES[TIER1_DEFAULT])
        allowed = minutes * 60 * TIER1_STALE_FACTOR + JITTER_SECONDS
    else:
        minutes = TIER2_MINUTES.get(cadence_label or "", TIER2_MINUTES[TIER2_DEFAULT])
        allowed = minutes * 60 * TIER2_STALE_FACTOR + JITTER_SECONDS
    return "stale" if age > allowed else "fresh"


def merge_status(views: List[dict], tier1_rows: List[dict], tier2_rows: List[dict],
                 now: Optional[datetime] = None) -> List[dict]:
    """Attach the aggregates (and the derived fresh/stale/none/paused states) to
    the monitoring views, matched on instance_id. Databases without rows get
    ``None`` timestamps and zero counts, never a missing key."""
    now = now or datetime.now(timezone.utc)
    t1 = {r.get("instance_id"): r for r in tier1_rows or []}
    t2 = {r.get("instance_id"): r for r in tier2_rows or []}
    out = []
    for v in views:
        iid = v.get("instance_id")
        a, b = t1.get(iid, {}), t2.get(iid, {})
        merged = {
            **v,
            "tier1_last_at": a.get("tier1_last_at"),
            "tier1_count_24h": int(a.get("tier1_count_24h") or 0),
            "tier2_last_at": b.get("tier2_last_at"),
            "tier2_last_health": b.get("tier2_last_health"),
            "tier2_count_30d": int(b.get("tier2_count_30d") or 0),
        }
        merged["tier1_state"] = collection_state(
            merged["tier1_last_at"], v.get("tier1_enabled"), v.get("tier1_frequency"), 1, now)
        merged["tier2_state"] = collection_state(
            merged["tier2_last_at"], v.get("tier2_enabled"), v.get("tier2_frequency"), 2, now)
        out.append(merged)
    return out


# ── Per-member status (the members table) ─────────────────────────────────────

def member_status_rows(record: dict) -> Tuple[List[dict], List[dict]]:
    """The newest Tier 1 row (7 days) and the newest scan (30 days) of each member of
    one database, for the members table. Raises on a store failure."""
    from tools.aws import metrics_store
    iid = record["instance_id"]
    return (metrics_store.tier1_newest_per_member(iid, member_ids(record), hours=24 * 7),
            metrics_store.tier2_member_status(iid, 30))


def merge_member_status(record: dict, tier1_rows: List[dict], tier2_rows: List[dict],
                        now: Optional[datetime] = None) -> List[dict]:
    """The record's members with their newest Tier 1 / Tier 2 rows attached. The role is
    the record's CURRENT label (who is writer now), not the row's; a member with no rows
    has ``tier1_state`` ``none`` (or ``paused``) and ``health`` ``unknown``."""
    now = now or datetime.now(timezone.utc)
    t1 = {r.get("member_instance_id"): r for r in tier1_rows or []}
    t2 = {r.get("member_instance_id"): r for r in tier2_rows or []}
    out = []
    for m in record_member_list(record):
        a, b = t1.get(m["instance_id"], {}), t2.get(m["instance_id"], {})
        lag = a.get("replica_lag_ms")
        health = (b.get("health_score") or "unknown") if b else "unknown"
        out.append({
            **m,
            "last_tier1_at": a.get("scan_time"),
            "tier1_state": collection_state(a.get("scan_time"), record.get("tier1_enabled"),
                                            record.get("tier1_frequency"), 1, now),
            "replica_lag_ms": float(lag) if lag is not None else None,
            "last_scan_at": b.get("scan_time"),
            "last_scan_kind": b.get("scan_kind"),
            "health": health if health in HEALTH_VALUES else "unknown",
        })
    return out
