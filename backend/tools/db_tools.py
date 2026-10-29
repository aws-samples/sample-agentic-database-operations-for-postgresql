# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Core database analysis tools — converted from postgres_health_insights.sh v2.0.

Each tool maps to one or more subsections of the 9-section shell script.
All tools are read-only (SELECT only) and return structured JSON.

Section mapping:
  1. Database Overview       → get_db_overview
  2. System Configuration    → get_db_configuration, get_security_audit, get_setting_overrides
  3. Current Activity        → get_current_activity, get_connection_analysis, get_workload_characterization
  4. Replication Status      → get_replication_status, get_wal_status, get_logical_replication
  5. Data Footprint          → get_storage_analysis, get_checkpoint_stats, get_bgwriter_stats,
                               get_autovacuum_utilization, get_connection_pooling_efficiency,
                               get_buffer_pool_stats, get_lock_analysis
  6. Query & I/O Performance → get_io_statistics, get_query_performance, get_query_execution_plans,
                               get_transaction_stats, get_function_performance, get_wait_event_analysis,
                               get_session_duration_analysis, get_prepared_statement_analysis,
                               get_temp_file_queries
  7. Maintenance & Health    → get_maintenance_status, get_sequence_exhaustion,
                               get_constraint_validation, get_materialized_view_freshness,
                               get_vacuum_stats_health
  8. Optimization            → get_index_statistics, get_toast_status, get_partitioning_analysis,
                               get_bloat_analysis, get_unused_indexes, get_duplicate_indexes,
                               get_idle_connections, get_oversized_datatypes, get_unused_tables,
                               get_fk_without_indexes, get_trigger_analysis, get_sequential_scan_tables
  9. Health Summary          → get_health_summary
  Utility                    → check_db_connectivity, get_query_plan,
                               list_monitored_databases, get_report_analysis
"""

import datetime
import logging
import re
from typing import Optional

from strands import tool
from tools.aws.pg_client import (execute_query, test_connection, lookup_record_database_name,
                                 SECRET_NOT_IN_SPACE_MESSAGE)
from tools.xid import XID_WRAPAROUND_MAX
from tools.aws.tenancy import record_in_space

logger = logging.getLogger("db-tools")


# ── Sensitive-catalog blocklist ─────────────────────────────────────────────
# Read-only enforcement (SELECT-only, RO role, 30s timeout) is preserved upstream; this only
# narrows WHAT a read may target. Even a read-only role must not be able to read credential
# hashes (pg_shadow, pg_authid), server files (pg_read_file*), directory listings via ANY
# pg_ls_* function (pg_ls_dir / pg_ls_logdir / pg_ls_waldir / pg_ls_tmpdir / ...),
# large-object filesystem I/O (lo_import/lo_export/lo_get), outbound DB connections (dblink*),
# TLS/session detail (pg_stat_ssl / pg_stat_file), OR execute DYNAMIC SQL that smuggles a
# blocked read past this literal check — the *_to_xml dump family (query_/cursor_/table_/
# schema_/database_to_xml) runs its argument or dumps whole tables/schemas/db server-side
# The function NAME must appear literally (SQL cannot compute a
# function/table name), so blocking the names defeats chr()-obfuscation. Defense-in-depth:
# the `dbops_ro` least-privilege role is the REAL backstop and must not be granted these
# catalogs/functions. `pg_ls_\w+` covers every ls variant; trailing \w* catches suffixes
# (dblink_exec, query_to_xmlschema, ...).
_SENSITIVE_CATALOG_RE = re.compile(
    r"\b(pg_shadow|pg_authid|pg_read_file|pg_read_binary_file|"
    r"pg_read_server_files|pg_stat_ssl|pg_stat_file|pg_hba_file_rules|pg_file_settings|"
    r"pg_ls_\w+|lo_import|lo_export|lo_get|dblink|"
    r"query_to_xml|cursor_to_xml|table_to_xml|schema_to_xml|database_to_xml)\w*",
    re.IGNORECASE,
)
# NOTE: pg_stat_activity / current_setting() / pg_is_in_recovery() are
# intentionally NOT blocked — legitimate diagnostic tools (get_current_activity,
# get_db_overview, test_connection) rely on them. The `dbops_ro` least-privilege role
# is the real control for what a read may reach; this list is defense-in-depth only.


def _current_space_id() -> str:
    """The caller's resolved space (published on the ContextVar by /invocations), or ''."""
    try:
        from model_resolver import CURRENT_SPACE_ID
        return CURRENT_SPACE_ID.get() or ""
    except Exception:
        return ""


def _current_user_id() -> str:
    """The caller's resolved identity (Cognito sub) for the current request, or ''."""
    try:
        from model_resolver import CURRENT_USER_ID
        return CURRENT_USER_ID.get() or ""
    except Exception:
        return ""


def _audit_query_plan(sql: str, database: str, allowed: bool, reason: str = "") -> None:
    """Record an EXPLAIN attempt (accepted or refused) to the governance store with the
    caller id. The one tool that accepts model-authored SQL leaves a durable trail
    of what it ran or refused. Fail-soft: never breaks the tool if the store is unavailable."""
    try:
        from tools.aws.governance_store import governance_registry
        governance_registry.audit(
            _current_user_id() or "unknown",
            "query_plan_run" if allowed else "query_plan_refused",
            (database or "postgres"),
            "account",
            None,
            {"database": database or "postgres", "query": (sql or "")[:1000],
             **({"reason": reason} if reason else {})},
            # a plain record field: agent events never fan out and are never vaulted
            space_id=_current_space_id(),
        )
    except Exception as e:  # noqa: BLE001 - audit must never break the tool
        logger.warning("query-plan audit write failed: %s", e)


def _scope_instances_to_space(instances: list) -> list:
    """Filter instances to the caller's resolved space; FAIL CLOSED (empty) when the space
    is unresolved. Every agent tool that reads tenant data (reports, metrics, listings) must
    route instance selection through this so a missing/forged space never yields cross-tenant
    data."""
    space = _current_space_id()
    if not space:
        return []
    # Membership is the tenancy authority: include a database when the caller's space is
    # one of its member spaces, so a shared database is listed for every member.
    return [i for i in instances if record_in_space(i, space)]


def _reject_unsafe_read_sql(q: str) -> Optional[str]:
    """The one safety gate every SQL-accepting tool passes through.

    Only get_query_plan takes SQL from the model today. It stays a single function so that
    adding another SQL-accepting tool cannot introduce a second, weaker set of rules.

    Returns an error string to refuse the query, or None to allow it:
      no multi-statement (embedded ';'), which blocks 'SELECT 1; SET ROLE admin; ...' -
        that escalates in-session even under readonly=True;
      no PostgreSQL Unicode-escape identifiers or strings (U&"pg_\\0061uthid"), which
        resolve to a blocked name at parse time and slip past a literal blocklist;
      no privileged or session statements (SET/RESET/DO/CALL/PREPARE/EXECUTE/LOAD/DML/DDL);
      no sensitive catalog or function, delegated to _blocked_sensitive_catalog.

    Defence in depth only. The dbops_ro least-privilege role is the real backstop."""
    if ";" in q:
        return "Multiple statements are not allowed; submit a single statement."
    if re.search(r'[uU]&[\'"]', q):
        return 'Unicode-escape identifiers/strings (U&"...") are not allowed.'
    if re.search(r'\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|GRANT|REVOKE|COPY|'
                 r'SET|RESET|DO|CALL|PREPARE|EXECUTE|DEALLOCATE|LOAD)\b', q, re.IGNORECASE):
        return "Query contains a forbidden statement/keyword."
    blocked = _blocked_sensitive_catalog(q)
    if blocked:
        return f"Query references a restricted catalog/function ({blocked}) and is not allowed."
    return None


def _blocked_sensitive_catalog(sql: str) -> str:
    """Return the offending catalog/function name if `sql` references a blocked
    sensitive catalog, else ''. Used by the shared read-query gate above."""
    m = _SENSITIVE_CATALOG_RE.search(sql or "")
    return m.group(1) if m else ""


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _detect_version(secret_arn: str, database: str) -> int:
    """Return PG version as integer (e.g. 150004 for 15.4)."""
    rows = execute_query(secret_arn,
        "SELECT current_setting('server_version_num')::int AS v", database=database)
    return rows[0]["v"] if rows else 150000


def _detect_deployment(secret_arn: str, database: str) -> str:
    """Detect Aurora / RDS / Self-managed."""
    rows = execute_query(secret_arn, """
        SELECT
            (SELECT count(*) FROM pg_settings WHERE name LIKE 'apg_%%') AS apg,
            (SELECT count(*) FROM pg_settings WHERE name LIKE 'rds.%%') AS rds
    """, database=database)
    if rows:
        if rows[0]["apg"] > 0:
            return "Aurora"
        if rows[0]["rds"] > 0:
            return "RDS"
    return "Self-managed"


def _has_extension(secret_arn: str, database: str, ext: str) -> bool:
    rows = execute_query(secret_arn,
        "SELECT count(*) AS c FROM pg_extension WHERE extname = %s",
        params=(ext,), database=database)
    return rows[0]["c"] > 0 if rows else False


# ─── Check references (thresholds) ─────────────────────────────────────────────
# Diagnostic checks apply their severity thresholds in Python, reading the ranges
# from a single INTERNAL references file (backend/checks/references.yaml). This
# keeps thresholds tunable without touching SQL/code and keeps them out of the
# tool's SQL. Not user-visible.

_CHECK_REFERENCES = None


def _check_references() -> dict:
    """Load the internal check-references (thresholds), cached after the first read."""
    global _CHECK_REFERENCES
    if _CHECK_REFERENCES is None:
        import yaml
        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / "checks" / "references.yaml"
        try:
            _CHECK_REFERENCES = yaml.safe_load(path.read_text()) or {}
        except FileNotFoundError:
            _CHECK_REFERENCES = {}
    return _CHECK_REFERENCES


def _severity_from_bands(thresholds: dict, value_pct, total_bytes,
                         hard_critical: bool = False) -> str:
    """Classify a size-weighted percentage into OK / NOTICE / WARNING / CRITICAL
    using a check's threshold bands (mirrors the reviewed size-weighted policy)."""
    if hard_critical:
        return "CRITICAL"
    if value_pct is None:
        return "OK"
    tb = total_bytes or 0
    band = "small"
    for b in thresholds.get("size_bands", []):
        lo, hi = b.get("min_bytes"), b.get("max_bytes")
        if (lo is None or tb >= lo) and (hi is None or tb < hi):
            band = b["name"]
            break
    levels = thresholds.get("levels", {})
    for sev in ("critical", "warning", "notice"):
        thr = (levels.get(sev) or {}).get(band)
        if thr is not None and value_pct > thr:
            return sev.upper()
    return "OK"


_XID_ORDER = ("OK", "WATCH", "NOTICE", "WARNING", "CRITICAL", "EMERGENCY")


def _severity_from_xid_bands(thresholds: dict, xid_age, freeze_max_age) -> str:
    """Classify a transaction-ID age into OK/WATCH/NOTICE/WARNING/CRITICAL/EMERGENCY
    (XID wraparound checks). Severity is the HIGHER of two independent tiers, both
    from references.yaml (kind: xid_relative) — no constants here:
      - relative: multiples of the instance's autovacuum_freeze_max_age
        (e.g. watch 0.8x, notice 1.0x, warning 1.5x)
      - absolute: transaction-count floors vs the 2B budget
        (e.g. warning 1e9, critical 1.5e9, emergency 1.8e9)
    freeze_max_age is passed in from the tool row (fetched this run), never assumed."""
    if xid_age is None:
        return "OK"
    rank = {s: i for i, s in enumerate(_XID_ORDER)}
    sev = "OK"

    def _bump(s):
        nonlocal sev
        if rank[s] > rank[sev]:
            sev = s

    absolute = thresholds.get("absolute", {}) or {}
    for name in ("notice", "warning", "critical", "emergency"):
        floor = absolute.get(name)
        if floor is not None and xid_age >= floor:
            _bump(name.upper())

    relative = thresholds.get("relative", {}) or {}
    if freeze_max_age:
        for name in ("watch", "notice", "warning"):
            mult = relative.get(name)
            if mult is not None and xid_age > mult * freeze_max_age:
                _bump(name.upper())
    return sev


def _severity_rows_returned(thresholds: dict, row_count: int) -> str:
    """Collection-level severity for 'zero-rows-is-healthy' checks (invalid indexes,
    unvalidated constraints, ...). Deterministic part only: 0 rows => OK, >= 1 => the
    configured `any_rows` severity (default WARNING). CRITICAL escalation (e.g. on a
    large / high-write object) is a skill judgment — the tool lists names, not sizes."""
    if not row_count:
        return "OK"
    return str(thresholds.get("any_rows", "WARNING")).upper()


def _severity_from_percent(thresholds: dict, value) -> str:
    """Single-metric percent bands. `thresholds.bands` is an ordered list of
    {name, min}, highest severity first; the first band whose `min` is strictly
    exceeded wins (the reference catalog uses '>' semantics). Returns the band name uppercased,
    else OK. Coerces value defensively (RDS Data API may hand back strings)."""
    if value is None:
        return "OK"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "OK"
    for b in thresholds.get("bands", []):
        mn = b.get("min")
        if mn is not None and v > mn:
            return str(b.get("name", "OK")).upper()
    return "OK"


_STATE_ORDER = {"OK": 0, "INFO": 1, "WATCH": 2, "NOTICE": 2, "WARNING": 3, "CRITICAL": 4, "EMERGENCY": 5}


def _severity_below(thresholds: dict, value) -> str:
    """Bands where a value BELOW the threshold is worse (e.g. a cache hit ratio).
    `thresholds.bands` is ordered worst-first as {name, max}; the first band whose `max`
    the value is strictly below wins. Returns the band name uppercased, else OK."""
    if value is None:
        return "OK"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "OK"
    for b in thresholds.get("bands", []):
        mx = b.get("max")
        if mx is not None and v < mx:
            return str(b.get("name", "OK")).upper()
    return "OK"


def _as_bool(v) -> bool:
    """Coerce a DB boolean that may arrive as a real bool or a 't'/'true'/'1' string."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("t", "true", "1", "yes")
    return bool(v)


def _severity_from_states(states: dict, active) -> str:
    """Highest severity among the active categorical conditions (state checks, e.g.
    materialized-view freshness). `states` maps condition name -> severity label
    (from references.yaml); `active` lists the condition names that hold for the row."""
    best = "OK"
    for name in active:
        label = str(states.get(name, "") or "").upper()
        if _STATE_ORDER.get(label, 0) > _STATE_ORDER.get(best, 0):
            best = label
    return best


def _severity_largest_table(thr: dict, r: dict) -> str:
    """CHK-027 storage-composition verdict from references.yaml. CRITICAL: a very large
    table (> huge_bytes) with dead-row % over dead_critical_pct, OR index size over
    index_critical_ratio x heap. WARNING: over-indexed (> index_warning_ratio x heap) OR
    dead-row % over dead_warning_pct."""
    live = r.get("live_rows") or 0
    dead = r.get("dead_rows") or 0
    heap = r.get("heap_bytes") or 0
    idx = r.get("index_bytes") or 0
    total = r.get("total_bytes") or 0
    dead_pct = (100.0 * dead / (live + dead)) if (live + dead) else 0.0
    ratio = (idx / heap) if heap else 0.0
    if (total > thr.get("huge_bytes", 107374182400) and dead_pct > thr.get("dead_critical_pct", 50)) \
            or ratio > thr.get("index_critical_ratio", 5):
        return "CRITICAL"
    if ratio > thr.get("index_warning_ratio", 2) or dead_pct > thr.get("dead_warning_pct", 20):
        return "WARNING"
    return "OK"


_MEMORY_UNIT_BYTES = {
    "B": 1, "kB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4,
    # pg_settings reports some memory GUCs in a block-count unit instead of a byte unit.
    "8kB": 8 * 1024, "16kB": 16 * 1024, "32kB": 32 * 1024,
}


def _setting_to_bytes(value, unit) -> int | None:
    """Convert a pg_settings (value, unit) pair to bytes. Returns None for a non-numeric
    value (e.g. 'on'/'off') or an unrecognized unit, so a caller never silently treats a
    byte count as a raw count or vice versa — the bug this fixes (CHK-022/C-028): reporting
    `setting=16384 unit=8kB` as "16384" with no byte conversion or RAM anchor made a
    128 MB shared_buffers unreadable as undersized."""
    if value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if not unit:
        return int(n)
    mult = _MEMORY_UNIT_BYTES.get(unit)
    return int(n * mult) if mult is not None else None


def _human_bytes(n: int | None) -> str | None:
    """Render a byte count the way an operator reads it (e.g. '128 MB'), or None if n is
    None. Mirrors pg_size_pretty's units so tool output reads consistently."""
    if n is None:
        return None
    n = float(n)
    for unit, size in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2), ("kB", 1024)):
        if abs(n) >= size:
            return f"{round(n / size, 2)} {unit}"
    return f"{int(n)} B"


_STATS_WINDOW_WARN_HOURS = 24  # a counter reset-window this long dwarfs a typical incident


def _add_stats_window_disclosure(row: dict, ratio_desc: str) -> None:
    """Attach the age of a cumulative counter's reset window to `row` in place, so a ratio
    computed from pg_stat_* lifetime counters (reset only at pg_stat_reset_shared /
    pg_stat_reset / instance reboot) is never read as the CURRENT state of a short-lived
    incident. Fixes the recurring defect behind C-045/C-047: a WAL-forced-checkpoint storm
    or a cache-hit collapse lasting minutes shows up as a negligible percentage of a
    months-long lifetime counter and gets cleared, when a live/short-window measurement
    would show it clearly. This does not change the tool's own severity verdict (that stays
    governed by references.yaml); it gives the agent the fact needed to distinguish
    "healthy right now" from "healthy on average since stats_reset"."""
    reset = row.get("stats_reset")
    if reset is None:
        row["stats_window_disclosure"] = (
            f"No stats_reset available; {ratio_desc} cannot be dated. Treat it as unknown "
            f"age and corroborate with a live/short-window signal before clearing an "
            f"active-incident question on this ratio alone."
        )
        return
    age_hours = _hours_since(reset)
    row["stats_reset"] = reset
    row["stats_window_age_hours"] = round(age_hours, 1) if age_hours is not None else None
    if age_hours is not None and age_hours > _STATS_WINDOW_WARN_HOURS:
        row["stats_window_disclosure"] = (
            f"{ratio_desc}, and the counters have been accumulating for "
            f"{round(age_hours, 1)} hours (since stats_reset). A short, severe incident can "
            f"be diluted to a negligible share of a window this long. Do not clear an "
            f"active-incident question on this ratio alone; corroborate with a live/short-"
            f"window signal (e.g. the PostgreSQL log, or two samples taken minutes apart "
            f"during the incident) before concluding the condition this ratio tracks is "
            f"not currently happening."
        )
    else:
        row["stats_window_disclosure"] = (
            f"{ratio_desc}; window is recent (~{round(age_hours, 1) if age_hours is not None else '?'} "
            f"hours since stats_reset), so it reasonably reflects current behavior."
        )


def _hours_since(ts) -> float | None:
    """Hours between a timestamp (datetime or ISO/str from the driver) and now, or None if
    it cannot be parsed. Timezone-naive values are assumed UTC, matching PostgreSQL's own
    `stats_reset` (timestamptz) as returned by this project's query layer."""
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.datetime.fromisoformat(ts)
        except ValueError:
            return None
    if not isinstance(ts, datetime.datetime):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - ts).total_seconds() / 3600.0


def _severity_slot_lag(thr: dict, lag_bytes, active=True) -> str:
    """Replication-slot lag severity (physical + logical), from references.yaml. lag_bytes
    bands set the base; an INACTIVE slot with lag is at least WARNING and escalates to
    CRITICAL past `inactive_critical_bytes` (an orphaned slot pins WAL — disk-fill risk)."""
    lag = lag_bytes or 0
    s = _severity_from_percent({"bands": thr.get("lag_bytes_bands", [])}, lag)
    if not _as_bool(active) and lag > 0:
        if lag > thr.get("inactive_critical_bytes", 53687091200):
            s = "CRITICAL"
        elif _STATE_ORDER["WARNING"] > _STATE_ORDER.get(s, 0):
            s = "WARNING"
    return s


def _severity_index_usage(thr: dict, r: dict) -> str:
    """Per-index review severity (CHK-008), thresholds from references.yaml. Invalid =>
    CRITICAL. PRIMARY KEY / UNIQUE indexes are never review candidates (they enforce
    constraints regardless of scans). Unused (0 scans): CRITICAL if larger than
    `unused_critical_bytes`, else NOTICE. Rarely used (< `rarely_used_scans`): WARNING if
    larger than `rarely_warning_bytes`, else NOTICE."""
    if not _as_bool(r.get("indisvalid")) or not _as_bool(r.get("indisready")):
        return "CRITICAL"
    if _as_bool(r.get("indisprimary")) or _as_bool(r.get("indisunique")):
        return "OK"
    size = r.get("index_size_bytes") or 0
    scans = r.get("scans") or 0
    if scans == 0:
        return "CRITICAL" if size > thr.get("unused_critical_bytes", 104857600) else "NOTICE"
    if scans < thr.get("rarely_used_scans", 50):
        return "WARNING" if size > thr.get("rarely_warning_bytes", 10485760) else "NOTICE"
    return "OK"


def _severity_ssl(force_ssl, encrypted: int, unencrypted: int) -> str:
    """CHK-044 TLS verdict. The headline is the instance's ENFORCEMENT (rds.force_ssl),
    not the live connection mix: an instance that allows plaintext is not secure merely
    because no plaintext session is connected this moment. force_ssl off => WARNING (it
    allows plaintext), or CRITICAL if a plaintext session is actually present. When
    enforcement is unknown — off RDS/Aurora, or the parameter was unreadable — fall back
    to judging by the live mix, as before."""
    enforced = None
    val = "" if force_ssl is None else str(force_ssl).strip().lower()
    if val:  # an empty/ambiguous value stays "unknown", never asserted as "off"
        enforced = val in ("1", "on", "true")
    if enforced is False:
        return "CRITICAL" if unencrypted > 0 else "WARNING"
    return ("CRITICAL" if unencrypted > encrypted and unencrypted > 0
            else "WARNING" if unencrypted > 0 else "OK")


# ─── Section 1: DATABASE OVERVIEW ──────────────────────────────────────────────

@tool
def check_db_connectivity(secret_arn: str, database: str = "postgres") -> dict:
    """Test connectivity to a PostgreSQL database and return basic info.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to connect to (default: postgres)

    Returns:
        Connection status, version, current database, user, and the role of the
        cluster member reached ("writer" or "reader"). Which member an alias
        reaches is the operator's choice in the Endpoint menu of the database
        picker (see the [DATABASE SCOPE] note); no tool argument changes it.
    """
    return test_connection(secret_arn, database)


@tool
def get_db_overview(secret_arn: str, database: str = "postgres") -> dict:
    """Section 1: Database overview — version, size, uptime, extensions, deployment type,
    connection stats, and extension availability checks.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Comprehensive database overview with deployment detection and extension status
    """
    overview = {}

    rows = execute_query(secret_arn, """
        SELECT version() AS full_version,
               current_setting('server_version') AS version,
               current_setting('server_version_num')::int AS version_num,
               current_database() AS database,
               current_user AS connected_user,
               pg_postmaster_start_time() AS start_time,
               now() - pg_postmaster_start_time() AS uptime,
               pg_size_pretty(pg_database_size(current_database())) AS db_size,
               pg_database_size(current_database()) AS db_size_bytes
    """, database=database)
    if rows:
        overview["server"] = rows[0]

    overview["deployment_type"] = _detect_deployment(secret_arn, database)

    rows = execute_query(secret_arn, """
        SELECT extname, extversion FROM pg_extension ORDER BY extname
    """, database=database)
    overview["extensions"] = rows

    # Extension availability for downstream tools
    overview["has_pg_stat_statements"] = _has_extension(secret_arn, database, "pg_stat_statements")
    overview["has_pg_buffercache"] = _has_extension(secret_arn, database, "pg_buffercache")
    overview["has_pgstattuple"] = _has_extension(secret_arn, database, "pgstattuple")

    rows = execute_query(secret_arn, """
        SELECT count(*) AS total_connections,
               count(*) FILTER (WHERE state = 'active') AS active,
               count(*) FILTER (WHERE state = 'idle') AS idle,
               count(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_transaction,
               current_setting('max_connections')::int AS max_connections,
               ROUND(100.0 * count(*) / current_setting('max_connections')::int, 1) AS usage_pct
        FROM pg_stat_activity
    """, database=database)
    if rows:
        overview["connections"] = rows[0]

    # All databases sizes
    rows = execute_query(secret_arn, """
        SELECT datname, pg_size_pretty(pg_database_size(datname)) AS size,
               pg_database_size(datname) AS size_bytes
        FROM pg_database WHERE datallowconn ORDER BY pg_database_size(datname) DESC
    """, database=database)
    overview["all_databases"] = rows

    return overview


# ─── Section 2: SYSTEM CONFIGURATION ──────────────────────────────────────────

@tool
def get_db_configuration(secret_arn: str, database: str = "postgres") -> dict:
    """Section 2: PostgreSQL parameters with deployment-aware tuning matrix.
    Shows memory, connections, WAL, autovacuum, logging, and parallel query settings
    with tuning priority and recommendations per deployment type (Aurora/RDS/EC2).

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Configuration parameters with deployment-aware recommendations
    """
    deploy = _detect_deployment(secret_arn, database)
    config = {"deployment_type": deploy}

    rows = execute_query(secret_arn, """
        SELECT name, setting, unit, source, boot_val, reset_val,
               short_desc, vartype, min_val, max_val, context
        FROM pg_settings
        WHERE name IN (
            'shared_buffers', 'effective_cache_size', 'work_mem', 'maintenance_work_mem',
            'max_connections', 'max_wal_size', 'min_wal_size', 'wal_level',
            'checkpoint_timeout', 'checkpoint_completion_target',
            'autovacuum', 'autovacuum_max_workers', 'autovacuum_naptime',
            'autovacuum_vacuum_threshold', 'autovacuum_vacuum_scale_factor',
            'autovacuum_analyze_threshold', 'autovacuum_analyze_scale_factor',
            'autovacuum_freeze_max_age', 'autovacuum_multixact_freeze_max_age',
            'log_min_duration_statement', 'log_statement', 'log_connections',
            'log_disconnections', 'log_lock_waits', 'log_temp_files',
            'shared_preload_libraries', 'track_activity_query_size',
            'max_parallel_workers', 'max_parallel_workers_per_gather',
            'max_worker_processes', 'random_page_cost', 'effective_io_concurrency',
            'password_encryption', 'ssl', 'track_functions',
            'idle_in_transaction_session_timeout', 'statement_timeout',
            'default_statistics_target', 'huge_pages', 'temp_buffers',
            'wal_compression', 'archive_mode', 'archive_command',
            'max_replication_slots', 'max_wal_senders', 'hot_standby',
            'hot_standby_feedback', 'recovery_min_apply_delay',
            'track_io_timing', 'track_counts'
        )
        ORDER BY name
    """, database=database)
    # A parameter the agent's OWN connection overrode reports that override in `setting`
    # with `source` = session/client (the read-only client sets statement_timeout on itself,
    # for instance), masking the INSTANCE value that lives in `reset_val`. Expose the instance
    # value as `effective_value` so the config describes the database, not this session; the
    # raw setting/source/reset_val stay on the row for transparency. (references.yaml#CHK-041
    # already states the tool returns the effective value + source.)
    _SESSION_SOURCES = {"session", "client", "override"}
    for p in rows:
        overridden = str(p.get("source") or "").strip().lower() in _SESSION_SOURCES
        p["overridden_by_session"] = overridden
        p["effective_value"] = p.get("reset_val") if overridden else p.get("setting")
    config["parameters"] = rows

    # Byte conversion + instance-RAM anchor (C-028): pg_settings reports a memory GUC as a
    # raw number in whatever `unit` it uses (often `8kB` blocks), which is unreadable without
    # converting it and has nothing to size it against. Convert every memory-type parameter
    # to bytes and anchor shared_buffers/work_mem/maintenance_work_mem against
    # effective_cache_size — RDS/Aurora set effective_cache_size proportional to the real
    # instance RAM (unlike a hardcoded instance-class table, this stays correct across
    # instance classes and resizes without a lookup table to maintain). This is sizing
    # DATA for the skill's tuning judgment, not a new auto-severity verdict — the parameter
    # audit's safety invariants (references.yaml#system-parameter-audit) are unchanged.
    by_name = {p.get("name"): p for p in rows}
    for p in rows:
        p["effective_value_bytes"] = _setting_to_bytes(p.get("effective_value"), p.get("unit"))
        if p["effective_value_bytes"] is not None:
            p["effective_value_human"] = _human_bytes(p["effective_value_bytes"])

    ecs_param = by_name.get("effective_cache_size")
    ecs_bytes = _setting_to_bytes(ecs_param.get("effective_value"), ecs_param.get("unit")) if ecs_param else None
    if ecs_bytes:
        config["ram_anchor"] = {
            "source": "effective_cache_size (RDS/Aurora set this proportional to instance "
                      "RAM; not a hardcoded instance-class table)",
            "effective_cache_size_bytes": ecs_bytes,
            "effective_cache_size_human": _human_bytes(ecs_bytes),
        }
        for name in ("shared_buffers", "maintenance_work_mem"):
            p = by_name.get(name)
            b = p.get("effective_value_bytes") if p else None
            if b is not None:
                config["ram_anchor"][f"{name}_pct_of_effective_cache_size"] = round(100.0 * b / ecs_bytes, 2)
        wm = by_name.get("work_mem")
        maxc = by_name.get("max_connections")
        wm_bytes = wm.get("effective_value_bytes") if wm else None
        maxc_val = _setting_to_bytes(maxc.get("effective_value"), None) if maxc else None  # plain integer, no unit
        if wm_bytes is not None and maxc_val:
            worst_case = wm_bytes * maxc_val
            config["ram_anchor"]["work_mem_x_max_connections_bytes"] = worst_case
            config["ram_anchor"]["work_mem_x_max_connections_human"] = _human_bytes(worst_case)
            config["ram_anchor"]["work_mem_x_max_connections_pct_of_effective_cache_size"] = (
                round(100.0 * worst_case / ecs_bytes, 2)
            )
    else:
        config["ram_anchor"] = {
            "note": "effective_cache_size unavailable this run; cannot anchor shared_buffers/"
                    "work_mem sizing to instance RAM. Report byte values only, with no "
                    "RAM-relative verdict."
        }

    # CHK-041 — facts only; config-safety verdict from references.yaml#system-parameter-audit.
    # These are safety INVARIANTS (autovacuum off is always dangerous), not tunable numeric bands.
    # Judge the INSTANCE value (effective_value), never a value this session happened to set.
    settings = {p.get("name"): (p.get("effective_value") or "") for p in rows}
    rules = _check_references().get("system-parameter-audit", {}).get("thresholds", {})
    findings, status = [], "OK"
    for spec in rules.get("critical_params", []):
        if settings.get(spec.get("name")) == spec.get("bad"):
            findings.append(spec.get("msg", spec.get("name")))
            sev = str(spec.get("severity", "WARNING")).upper()
            if _STATE_ORDER.get(sev, 0) > _STATE_ORDER.get(status, 0):
                status = sev
    for ext in rules.get("require_preload", []):
        if ext not in settings.get("shared_preload_libraries", ""):
            findings.append(f"{ext} not in shared_preload_libraries")
            if _STATE_ORDER["WARNING"] > _STATE_ORDER.get(status, 0):
                status = "WARNING"
    config["config_findings"] = findings
    config["status"] = status
    return config


@tool
def get_security_audit(secret_arn: str, database: str = "postgres") -> dict:
    """Section 2 subsections: Security audit — user/role analysis, privilege audit,
    SSL connections, password security, sensitive data detection, row-level security,
    and audit configuration.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Comprehensive security audit results
    """
    audit = {}

    # User & Role Analysis
    rows = execute_query(secret_arn, """
        SELECT rolname AS role_name,
               rolsuper AS is_superuser,
               rolinherit AS inherits,
               rolcreaterole AS can_create_role,
               rolcreatedb AS can_create_db,
               rolcanlogin AS can_login,
               rolreplication AS is_replication,
               rolbypassrls AS bypasses_rls,
               rolconnlimit AS connection_limit,
               rolvaliduntil AS password_expiry,
               ARRAY(SELECT b.rolname FROM pg_auth_members m
                     JOIN pg_roles b ON m.roleid = b.oid
                     WHERE m.member = r.oid) AS member_of
        FROM pg_roles r
        ORDER BY rolcanlogin DESC, rolsuper DESC, rolname
        LIMIT 200
    """, database=database)
    audit["roles"] = rows

    # Privilege Audit
    rows = execute_query(secret_arn, """
        WITH table_privs AS (
            SELECT grantee, table_schema, table_name, privilege_type
            FROM information_schema.table_privileges
            WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
        )
        SELECT grantee, table_schema,
               count(DISTINCT table_name) AS tables_accessible,
               array_agg(DISTINCT privilege_type) AS privileges
        FROM table_privs
        GROUP BY grantee, table_schema
        ORDER BY grantee, table_schema
        LIMIT 200
    """, database=database)
    audit["privileges"] = rows

    # SSL Connection Analysis
    rows = execute_query(secret_arn, """
        SELECT ssl, count(*) AS connections,
               CASE WHEN ssl THEN 'Encrypted' ELSE 'Unencrypted' END AS status
        FROM pg_stat_ssl JOIN pg_stat_activity USING (pid)
        WHERE pid != pg_backend_pid()
        GROUP BY ssl
    """, database=database)
    audit["ssl_connections"] = rows

    # Whether the INSTANCE enforces TLS, independent of who is connected right now:
    # rds.force_ssl (RDS / Aurora PostgreSQL). missing_ok => NULL off RDS or on a
    # self-managed server where the parameter does not exist, so enforcement is reported
    # "unknown" rather than erroring.
    rows = execute_query(secret_arn,
                         "SELECT current_setting('rds.force_ssl', true) AS force_ssl",
                         database=database)
    audit["ssl_enforcement"] = {"force_ssl": (rows[0].get("force_ssl") if rows else None)}

    # Attribute any plaintext sessions by role and client address, so an unencrypted
    # connection is an actionable finding and not just a count.
    rows = execute_query(secret_arn, """
        SELECT a.usename AS role, host(a.client_addr) AS client_addr, count(*) AS connections
        FROM pg_stat_ssl s JOIN pg_stat_activity a USING (pid)
        WHERE NOT s.ssl AND a.pid != pg_backend_pid()
        GROUP BY a.usename, a.client_addr
        ORDER BY count(*) DESC
        LIMIT 50
    """, database=database)
    audit["unencrypted_sessions"] = rows

    # Row-Level Security Status
    rows = execute_query(secret_arn, """
        SELECT n.nspname AS schema_name, c.relname AS table_name,
               c.relrowsecurity AS rls_enabled,
               c.relforcerowsecurity AS rls_forced,
               count(p.polname) AS policy_count
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_policy p ON p.polrelid = c.oid
        WHERE c.relkind = 'r'
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
        GROUP BY n.nspname, c.relname, c.relrowsecurity, c.relforcerowsecurity
        HAVING c.relrowsecurity OR count(p.polname) > 0
        ORDER BY n.nspname, c.relname
        LIMIT 200
    """, database=database)
    audit["row_level_security"] = rows

    # Audit Configuration
    rows = execute_query(secret_arn, """
        SELECT 'pgaudit Extension' AS check_item,
               CASE WHEN EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgaudit')
                    THEN 'Installed' ELSE 'Not installed' END AS status,
               'Provides detailed audit logging' AS description
        UNION ALL
        SELECT 'log_statement', current_setting('log_statement'),
               'Controls which SQL statements are logged'
        UNION ALL
        SELECT 'log_connections', current_setting('log_connections'),
               'Logs each successful connection'
        UNION ALL
        SELECT 'log_disconnections', current_setting('log_disconnections'),
               'Logs session terminations'
    """, database=database)
    audit["audit_config"] = rows

    # Sensitive Data Detection (heuristic)
    rows = execute_query(secret_arn, """
        SELECT table_schema, table_name, column_name, data_type,
               CASE
                   WHEN column_name ~* '(ssn|social.?sec|national.?id)' THEN 'SSN/National ID'
                   WHEN column_name ~* '(credit.?card|card.?num|pan)' THEN 'Credit Card'
                   WHEN column_name ~* '(password|passwd|pwd|secret|token)' THEN 'Credential'
                   WHEN column_name ~* '(email|e.?mail)' THEN 'Email'
                   WHEN column_name ~* '(phone|mobile|cell)' THEN 'Phone'
                   WHEN column_name ~* '(address|street|city|zip|postal)' THEN 'Address'
                   WHEN column_name ~* '(birth|dob|date.?of.?birth)' THEN 'Date of Birth'
                   ELSE 'Other PII'
               END AS detected_type
        FROM information_schema.columns
        WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
          AND column_name ~* '(ssn|social.?sec|credit.?card|card.?num|password|passwd|pwd|secret|token|email|phone|mobile|address|birth|dob)'
        ORDER BY table_schema, table_name
        LIMIT 200
    """, database=database)
    audit["sensitive_data"] = rows

    # One tool backs six security checks (CHK-042..047); apply a per-subsection verdict from
    # references.yaml (read-only audit — findings are review items, never auto-remediated).
    def _worse(cur, s):
        return s if _STATE_ORDER.get(s, 0) > _STATE_ORDER.get(cur, 0) else cur

    # CHK-042 roles: too many superusers, or an RLS-bypassing login role. AWS-managed service
    # roles (rdsadmin et al.) are excluded from the count and the risk flag — RDS reserves those
    # names and the customer cannot act on them, so flagging them is noise that buries real
    # customer superusers. They are marked managed_role for transparency; nothing is removed
    # from the database, only from the findings.
    rthr = _check_references().get("role-audit", {}).get("thresholds", {})
    managed = {str(m).lower() for m in rthr.get("managed_roles", [])}
    def _is_managed(r):
        return str(r.get("role_name") or "").lower() in managed
    supers = sum(1 for r in audit.get("roles", []) if _as_bool(r.get("is_superuser")) and not _is_managed(r))
    rstatus = "WARNING" if supers > rthr.get("max_superusers", 3) else "OK"
    for r in audit.get("roles", []):
        r["managed_role"] = _is_managed(r)
        r["risk"] = "" if r["managed_role"] else (
            "bypasses_rls+login" if (_as_bool(r.get("bypasses_rls")) and _as_bool(r.get("can_login"))) else "")
        if r["risk"]:
            rstatus = _worse(rstatus, "WARNING")
    audit["roles_status"] = rstatus

    # CHK-043 privileges: PUBLIC with broad access
    audit["privileges_status"] = "WARNING" if any(
        str(p.get("grantee", "")).upper() == "PUBLIC" for p in audit.get("privileges", [])) else "OK"

    # CHK-044 SSL: the instance's TLS ENFORCEMENT is the headline (rds.force_ssl), not just
    # who happens to be encrypted right now — a snapshot with no plaintext session is not
    # "secure" if the instance still allows plaintext.
    enc = sum((r.get("connections") or 0) for r in audit.get("ssl_connections", []) if _as_bool(r.get("ssl")))
    unenc = sum((r.get("connections") or 0) for r in audit.get("ssl_connections", []) if not _as_bool(r.get("ssl")))
    audit["ssl_status"] = _severity_ssl(audit.get("ssl_enforcement", {}).get("force_ssl"), enc, unenc)

    # CHK-045 RLS: enabled but no policy (blocks non-owner access) / forced off
    rls = "OK"
    for r in audit.get("row_level_security", []):
        if _as_bool(r.get("rls_enabled")) and (r.get("policy_count") or 0) == 0:
            rls = _worse(rls, "WARNING")
    audit["rls_status"] = rls

    # CHK-046 audit config: pgaudit missing / no statement logging
    astatus = "OK"
    for r in audit.get("audit_config", []):
        item, val = str(r.get("check_item", "")), str(r.get("status", "")).lower()
        if item == "pgaudit Extension" and "not installed" in val:
            astatus = _worse(astatus, "WARNING")
        if item == "log_statement" and val == "none":
            astatus = _worse(astatus, "WARNING")
    audit["audit_config_status"] = astatus

    # CHK-047 sensitive data: credential plaintext / credit card (PCI)
    sstatus = "OK"
    for r in audit.get("sensitive_data", []):
        dt = str(r.get("detected_type", ""))
        if dt == "Credit Card":
            sstatus = _worse(sstatus, "CRITICAL")
        elif dt == "Credential":
            sstatus = _worse(sstatus, "WARNING")
        elif _STATE_ORDER["NOTICE"] > _STATE_ORDER.get(sstatus, 0):
            sstatus = "NOTICE"
    audit["sensitive_data_status"] = sstatus

    return audit


@tool
def get_setting_overrides(secret_arn: str, database: str = "postgres") -> dict:
    """Section 2.8: Database and role-level setting overrides that differ from defaults.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        List of database/role setting overrides
    """
    rows = execute_query(secret_arn, """
        SELECT COALESCE(d.datname, 'ALL DATABASES') AS database_name,
               COALESCE(r.rolname, 'ALL ROLES') AS role_name,
               s.setconfig AS settings
        FROM pg_db_role_setting s
        LEFT JOIN pg_database d ON d.oid = s.setdatabase
        LEFT JOIN pg_roles r ON r.oid = s.setrole
        ORDER BY d.datname, r.rolname
        LIMIT 200
    """, database=database)
    # CHK-048 — facts only. Any override is a review item (NOTICE); an override matching a
    # security-sensitive keyword escalates. Keywords from references.yaml#parameter-overrides.
    thr = _check_references().get("parameter-overrides", {}).get("thresholds", {})
    crit_kw = [k.lower() for k in thr.get("critical_keywords", [])]
    warn_kw = [k.lower() for k in thr.get("warning_keywords", [])]
    for r in rows:
        sc = r.get("settings")
        blob = (" ".join(sc) if isinstance(sc, list) else str(sc or "")).lower()
        if any(k in blob for k in crit_kw):
            r["status"] = "CRITICAL"
        elif any(k in blob for k in warn_kw):
            r["status"] = "WARNING"
        else:
            r["status"] = "NOTICE"
    return {"overrides": rows}


@tool
def get_extension_inventory(secret_arn: str, database: str = "postgres") -> dict:
    """Section 2 subsection: installed extensions with available version upgrades, and
    whether expected extensions (pg_stat_statements, pgaudit, ...) are present.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Installed extensions (with upgrade_available), the list of missing expected
        extensions, and an overall status.
    """
    # CHK-051 — facts only. Which expected extensions are missing (and how severe) comes
    # from references.yaml#extension-inventory; an available version upgrade is a NOTICE.
    rows = execute_query(secret_arn, """
        SELECT e.extname, e.extversion AS installed_version,
               a.default_version AS latest_available,
               (a.default_version IS DISTINCT FROM e.extversion) AS upgrade_available
        FROM pg_extension e
        LEFT JOIN pg_available_extensions a ON a.name = e.extname
        ORDER BY e.extname
    """, database=database)
    for r in rows:
        r["status"] = "NOTICE" if _as_bool(r.get("upgrade_available")) else "OK"
    installed = {r.get("extname") for r in rows}
    thr = _check_references().get("extension-inventory", {}).get("thresholds", {})
    critical_missing = set(thr.get("critical_if_missing", []))
    missing = [e for e in thr.get("expected", []) if e not in installed]
    status = "OK"
    for m in missing:
        s = "CRITICAL" if m in critical_missing else "WARNING"
        if _STATE_ORDER.get(s, 0) > _STATE_ORDER.get(status, 0):
            status = s
    return {"extensions": rows, "missing_expected": missing, "status": status}


# ─── Section 3: CURRENT ACTIVITY ──────────────────────────────────────────────

@tool
def get_current_activity(secret_arn: str, database: str = "postgres") -> dict:
    """Section 3: Current database activity — connection breakdown, active queries,
    waiting queries, and idle-in-transaction sessions.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Current activity snapshot with connection stats, active queries,
        idle-in-transaction sessions, and snapshot_holders (sessions pinning an
        old backend_xmin, with xmin_age_xids — the signal for vacuum-blocking
        snapshots). Use snapshot_holders instead of a custom pg_stat_activity query.
    """
    activity = {}

    rows = execute_query(secret_arn, """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE state = 'active') AS active,
               count(*) FILTER (WHERE state = 'idle') AS idle,
               count(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_txn,
               count(*) FILTER (WHERE state = 'idle in transaction (aborted)') AS idle_in_txn_aborted,
               count(*) FILTER (WHERE wait_event_type IS NOT NULL AND state = 'active') AS waiting,
               current_setting('max_connections')::int AS max_connections,
               ROUND(100.0 * count(*) / current_setting('max_connections')::int, 1) AS usage_pct
        FROM pg_stat_activity WHERE pid != pg_backend_pid()
    """, database=database)
    if rows:
        # CHK-037 — connection-utilization verdict from references.yaml#current-activity-snapshot
        # (usage_pct bands); idle_in_transaction(aborted) is a hard CRITICAL (broken txns hold locks).
        s = rows[0]
        thr = _check_references().get("current-activity-snapshot", {}).get("thresholds", {})
        sev = _severity_from_percent({"bands": thr.get("usage_pct_bands", [])}, s.get("usage_pct"))
        if (s.get("idle_in_txn_aborted") or 0) > 0:
            sev = "CRITICAL"
        s["status"] = sev
        activity["summary"] = s

    rows = execute_query(secret_arn, """
        SELECT pid, usename, datname, state, wait_event_type, wait_event,
               backend_type,
               EXTRACT(EPOCH FROM (now() - query_start))::int AS query_duration_seconds,
               EXTRACT(EPOCH FROM (now() - xact_start))::int AS txn_duration_seconds,
               LEFT(query, 200) AS query_preview,
               client_addr::text, application_name
        FROM pg_stat_activity
        WHERE state = 'active' AND pid != pg_backend_pid()
        ORDER BY query_start ASC NULLS LAST
        LIMIT 50
    """, database=database)
    # CHK-049 — long-running active transactions (hold XID pins, block autovacuum freeze).
    # Per-query severity from references.yaml#long-running-transactions (txn-duration seconds).
    _lrt = {"bands": _check_references().get("long-running-transactions", {}).get(
        "thresholds", {}).get("txn_seconds_bands", [])}
    for r in rows:
        r["status"] = _severity_from_percent(_lrt, r.get("txn_duration_seconds"))
    activity["active_queries"] = rows

    rows = execute_query(secret_arn, """
        SELECT pid, usename, datname, state,
               EXTRACT(EPOCH FROM (now() - state_change))::int AS idle_seconds,
               LEFT(query, 200) AS last_query,
               client_addr::text, application_name
        FROM pg_stat_activity
        WHERE state = 'idle in transaction' AND pid != pg_backend_pid()
        ORDER BY state_change ASC NULLS LAST
        LIMIT 50
    """, database=database)
    activity["idle_in_transaction"] = rows

    # Snapshot holders — sessions pinning an old backend_xmin, which is what
    # blocks autovacuum from cleaning dead tuples. `age(backend_xmin)` is the
    # CORRECT xmin age in XIDs (do NOT hand-roll now()::text::bigint casts).
    # This session's own backend is excluded via pg_backend_pid(), so the
    # diagnostic query never shows up as a false blocker. Interpretation rule
    # (applied by the caller): treat as a vacuum blocker ONLY when
    # xmin_age_xids > 1,000,000 AND dead tuples exist on some table.
    rows = execute_query(secret_arn, """
        SELECT pid, usename, datname, state,
               backend_xmin::text AS backend_xmin,
               age(backend_xmin) AS xmin_age_xids,
               EXTRACT(EPOCH FROM (now() - xact_start))::int AS txn_duration_seconds,
               EXTRACT(EPOCH FROM (now() - backend_start))::int AS session_duration_seconds,
               LEFT(query, 200) AS query_preview,
               client_addr::text, application_name
        FROM pg_stat_activity
        WHERE backend_xmin IS NOT NULL AND pid != pg_backend_pid()
        ORDER BY age(backend_xmin) DESC
        LIMIT 50
    """, database=database)
    activity["snapshot_holders"] = rows

    return activity


@tool
def get_connection_analysis(secret_arn: str, database: str = "postgres") -> dict:
    """Section 3 subsections: Connection pooling detection, application identification,
    client analysis, and connection churn.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Connection analysis with pooling detection and client breakdown
    """
    analysis = {}

    # Connection Pooling Detection
    rows = execute_query(secret_arn, """
        WITH session_ages AS (
            SELECT pid, usename, application_name,
                   EXTRACT(EPOCH FROM (now() - backend_start))::int AS session_age_seconds,
                   client_addr::text
            FROM pg_stat_activity WHERE pid != pg_backend_pid()
        )
        SELECT
            CASE
                WHEN count(*) FILTER (WHERE session_age_seconds < 5) > count(*) * 0.3
                    THEN 'Likely using connection pooler (many short-lived sessions)'
                WHEN count(DISTINCT application_name) FILTER (WHERE application_name ~* 'pgbouncer|pgpool|odyssey')  > 0
                    THEN 'Connection pooler detected by application name'
                ELSE 'No connection pooler detected'
            END AS pooling_status,
            count(*) AS total_sessions,
            count(*) FILTER (WHERE session_age_seconds < 5) AS short_lived,
            count(*) FILTER (WHERE session_age_seconds > 3600) AS long_lived,
            ROUND(avg(session_age_seconds)::numeric, 0) AS avg_session_age_seconds
        FROM session_ages
    """, database=database)
    analysis["pooling_detection"] = rows[0] if rows else {}

    # Application Identification
    rows = execute_query(secret_arn, """
        SELECT COALESCE(NULLIF(application_name, ''), '<unnamed>') AS application,
               count(*) AS connections,
               count(*) FILTER (WHERE state = 'active') AS active,
               count(*) FILTER (WHERE state = 'idle') AS idle
        FROM pg_stat_activity WHERE pid != pg_backend_pid()
        GROUP BY application_name ORDER BY count(*) DESC LIMIT 50
    """, database=database)
    analysis["applications"] = rows

    # Client Analysis
    rows = execute_query(secret_arn, """
        SELECT COALESCE(client_addr::text, 'local/unix') AS client_address,
               count(*) AS connections,
               count(*) FILTER (WHERE state = 'active') AS active,
               count(*) FILTER (WHERE state = 'idle') AS idle,
               array_agg(DISTINCT usename) AS users
        FROM pg_stat_activity WHERE pid != pg_backend_pid()
        GROUP BY client_addr ORDER BY count(*) DESC LIMIT 50
    """, database=database)
    analysis["clients"] = rows

    # Connection Churn
    rows = execute_query(secret_arn, """
        WITH age_buckets AS (
            SELECT CASE
                WHEN EXTRACT(EPOCH FROM (now() - backend_start)) < 60 THEN '< 1 min'
                WHEN EXTRACT(EPOCH FROM (now() - backend_start)) < 300 THEN '1-5 min'
                WHEN EXTRACT(EPOCH FROM (now() - backend_start)) < 3600 THEN '5-60 min'
                WHEN EXTRACT(EPOCH FROM (now() - backend_start)) < 86400 THEN '1-24 hours'
                ELSE '> 24 hours'
            END AS age_bucket
            FROM pg_stat_activity WHERE pid != pg_backend_pid()
        )
        SELECT age_bucket, count(*) AS connections
        FROM age_buckets GROUP BY age_bucket
        ORDER BY CASE age_bucket
            WHEN '< 1 min' THEN 1 WHEN '1-5 min' THEN 2
            WHEN '5-60 min' THEN 3 WHEN '1-24 hours' THEN 4 ELSE 5 END
    """, database=database)
    analysis["connection_churn"] = rows

    # CHK-038 — facts only; verdict from references.yaml#connection-characterization:
    # single-application or single-client-IP dominance + connection churn (< 1 min bucket).
    thr = _check_references().get("connection-characterization", {}).get("thresholds", {})
    total = (analysis.get("pooling_detection") or {}).get("total_sessions") or 0
    top_app = max((a.get("connections") or 0 for a in analysis.get("applications") or []), default=0)
    top_client = max((c.get("connections") or 0 for c in analysis.get("clients") or []), default=0)
    short = next((b.get("connections") or 0 for b in analysis.get("connection_churn") or []
                  if b.get("age_bucket") == "< 1 min"), 0)
    app_pct = round(100.0 * top_app / total, 1) if total else 0
    client_pct = round(100.0 * top_client / total, 1) if total else 0
    churn_pct = round(100.0 * short / total, 1) if total else 0
    sev = _severity_from_percent({"bands": thr.get("churn_bands", [])}, churn_pct)
    if client_pct > thr.get("client_critical_pct", 80):
        sev = "CRITICAL"
    elif app_pct > thr.get("app_warning_pct", 60) and _STATE_ORDER["WARNING"] > _STATE_ORDER.get(sev, 0):
        sev = "WARNING"
    analysis["dominance"] = {"top_app_pct": app_pct, "top_client_pct": client_pct, "churn_pct": churn_pct}
    analysis["status"] = sev
    return analysis


@tool
def get_workload_characterization(secret_arn: str, database: str = "postgres") -> dict:
    """Section 3.3: Workload characterization — breakdown of query types
    (SELECT/INSERT/UPDATE/DELETE) currently running.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Workload breakdown by query type
    """
    rows = execute_query(secret_arn, """
        WITH query_types AS (
            SELECT CASE
                WHEN query ~* '^\\s*(WITH\\s+.+\\s+)?SELECT' THEN 'SELECT'
                WHEN query ~* '^\\s*(WITH\\s+.+\\s+)?INSERT' THEN 'INSERT'
                WHEN query ~* '^\\s*(WITH\\s+.+\\s+)?UPDATE' THEN 'UPDATE'
                WHEN query ~* '^\\s*(WITH\\s+.+\\s+)?DELETE' THEN 'DELETE'
                WHEN query ~* '^\\s*COPY' THEN 'COPY'
                WHEN query ~* '^\\s*(CREATE|ALTER|DROP)' THEN 'DDL'
                ELSE 'OTHER'
            END AS query_type, state
            FROM pg_stat_activity
            WHERE pid != pg_backend_pid() AND query IS NOT NULL AND query != ''
        )
        SELECT query_type, count(*) AS total,
               count(*) FILTER (WHERE state = 'active') AS active,
               count(*) FILTER (WHERE state = 'idle') AS idle
        FROM query_types GROUP BY query_type ORDER BY count(*) DESC
    """, database=database)
    # CHK-039 — facts only; light verdict from references.yaml#workload-mix. Active DDL is a
    # lock-contention risk (WARNING); a dominant active COPY is a bulk-load NOTICE. "During peak"
    # is a timing judgment for the skill — the tool flags the presence, not the schedule.
    states = _check_references().get("workload-mix", {}).get("thresholds", {}).get("states", {})
    ddl_active = next((r.get("active") or 0 for r in rows if r.get("query_type") == "DDL"), 0)
    copy_active = next((r.get("active") or 0 for r in rows if r.get("query_type") == "COPY"), 0)
    status = "OK"
    if ddl_active > 0:
        status = str(states.get("ddl_active", "WARNING")).upper()
    elif copy_active > 0:
        status = str(states.get("copy_active", "NOTICE")).upper()
    return {"workload": rows, "status": status}


@tool
def get_workload_profile(secret_arn: str, database: str = "postgres") -> dict:
    """Database workload profile — classifies workload as READ-HEAVY, WRITE-HEAVY, or MIXED
    based on cumulative pg_stat_database stats. Shows transaction counts, rollback ratio,
    temp file usage, and stats window age.

    NOTE: This uses CUMULATIVE stats since last pg_stat_database reset, not current activity.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Workload classification, read/write breakdown, transaction stats, temp file count
    """
    rows = execute_query(secret_arn, """
        WITH db_stats AS (
            SELECT
                tup_returned + tup_fetched AS total_reads,
                tup_inserted + tup_updated + tup_deleted AS total_writes,
                tup_returned, tup_fetched, tup_inserted, tup_updated, tup_deleted,
                xact_commit, xact_rollback, temp_files, temp_bytes,
                stats_reset,
                EXTRACT(EPOCH FROM (now() - COALESCE(stats_reset, pg_postmaster_start_time()))) AS stats_seconds
            FROM pg_stat_database WHERE datname = current_database()
        )
        SELECT
            CASE
                WHEN total_reads + total_writes = 0 THEN 'NO ACTIVITY'
                WHEN stats_seconds < 3600 THEN 'INSUFFICIENT DATA'
                WHEN total_reads::numeric / NULLIF(total_reads + total_writes, 0) >= 0.80 THEN 'READ-HEAVY'
                WHEN total_writes::numeric / NULLIF(total_reads + total_writes, 0) >= 0.60 THEN 'WRITE-HEAVY'
                ELSE 'MIXED'
            END AS workload_type,
            ROUND(100.0 * total_reads / NULLIF(total_reads + total_writes, 0), 1) AS read_pct,
            ROUND(100.0 * total_writes / NULLIF(total_reads + total_writes, 0), 1) AS write_pct,
            total_reads, total_writes,
            tup_inserted, tup_updated, tup_deleted,
            xact_commit, xact_rollback,
            ROUND(100.0 * xact_rollback / NULLIF(xact_commit + xact_rollback, 0), 2) AS rollback_pct,
            temp_files, pg_size_pretty(temp_bytes) AS temp_bytes_pretty,
            COALESCE(to_char(stats_reset, 'YYYY-MM-DD HH24:MI'), 'Never') AS stats_reset,
            ROUND(stats_seconds / 86400) AS stats_days
        FROM db_stats
    """, database=database)
    # CHK-040 — facts only; verdict from references.yaml#workload-pattern. NO ACTIVITY on a
    # production DB is CRITICAL; a short stats window (stats_days < min_stats_days) makes the
    # classification unreliable (WARNING). Rollback severity reuses transaction-health bands.
    thr = _check_references().get("workload-pattern", {}).get("thresholds", {})
    roll_thr = _check_references().get("transaction-health", {}).get("thresholds", {})
    for r in rows:
        status = _severity_from_percent(roll_thr, r.get("rollback_pct"))
        if r.get("workload_type") == "NO ACTIVITY":
            status = "CRITICAL"
        if (r.get("stats_days") or 0) < thr.get("min_stats_days", 7) and _STATE_ORDER["WARNING"] > _STATE_ORDER.get(status, 0):
            status = "WARNING"
        r["status"] = status
    return {"source": "pg_stat_database (cumulative since stats reset)", "profile": rows}


# ─── Section 4: REPLICATION STATUS ─────────────────────────────────────────────

@tool
def get_replication_status(secret_arn: str, database: str = "postgres") -> dict:
    """Section 4: Replication information — role (primary/standby), replication lag,
    WAL senders, replication slots, and synchronous replication config.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Replication status with lag metrics and slot information
    """
    repl = {}
    deploy = _detect_deployment(secret_arn, database)
    repl["deployment_type"] = deploy

    rows = execute_query(secret_arn, """
        SELECT CASE WHEN pg_is_in_recovery() THEN 'standby' ELSE 'primary' END AS role,
               pg_is_in_recovery() AS in_recovery
    """, database=database)
    if rows:
        repl["role"] = rows[0]

    # Replication slots. xmin / catalog_xmin are included deliberately (C-083): a slot's
    # xmin is the oldest transaction id the primary must retain for that consumer, so a
    # non-null xmin means the slot is PINNING the primary's vacuum horizon — the actual
    # cause of primary-side bloat when a standby runs hot_standby_feedback=on. Slot lag
    # alone (restart_lsn distance) does NOT show this, which is why a "lag looks fine"
    # read missed it. On RDS this is the only place the signal is visible, because
    # pg_stat_replication.backend_xmin is blank on RDS. catalog_xmin is the logical-slot
    # equivalent for catalog tuples. Cast to text so the xid serializes cleanly.
    rows = execute_query(secret_arn, """
        SELECT slot_name, slot_type, active, restart_lsn,
               pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS lag_size,
               pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS lag_bytes,
               xmin::text AS xmin, catalog_xmin::text AS catalog_xmin,
               age(xmin) AS xmin_age_xids, age(catalog_xmin) AS catalog_xmin_age_xids
        FROM pg_replication_slots
        ORDER BY pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) DESC NULLS LAST
        LIMIT 50
    """, database=database)
    # CHK-034 — facts only; severity from references.yaml#replication-health (slot lag
    # bands + inactive-slot escalation; replica replay-lag seconds bands).
    _rt = _check_references().get("replication-health", {}).get("thresholds", {})
    pinning = False
    for r in rows:
        r["status"] = _severity_slot_lag(_rt, r.get("lag_bytes"), r.get("active"))
        r["pins_vacuum_horizon"] = (r.get("xmin") is not None) or (r.get("catalog_xmin") is not None)
        pinning = pinning or r["pins_vacuum_horizon"]
    repl["slots"] = rows
    if pinning:
        if deploy == "RDS":
            repl["slots_xmin_note"] = (
                "At least one replication slot has a non-null xmin/catalog_xmin, so it is "
                "holding back the primary's vacuum horizon (age shown as *_age_xids). On RDS "
                "this slot xmin is the signal that a standby running hot_standby_feedback=on "
                "is the cause of primary-side table/catalog bloat, because "
                "pg_stat_replication.backend_xmin is not populated on RDS. Correlate a pinned "
                "xmin with rising dead tuples / unvacuumed bloat before blaming autovacuum "
                "tuning; the fix is on the feedback-sending standby, not the primary."
            )
        elif deploy == "Aurora":
            repl["slots_xmin_note"] = (
                "A replication slot has a non-null xmin/catalog_xmin. Note this instance is "
                "Aurora: physical replication uses the shared Aurora storage layer rather than "
                "WAL-shipping standbys, so the classic 'hot_standby_feedback pins the primary's "
                "vacuum horizon' story applies to logical slots here, not physical replicas. "
                "Treat a pinned catalog_xmin on a logical slot as the bloat signal; do not "
                "assume an RDS-style physical-standby feedback loop."
            )
        else:
            repl["slots_xmin_note"] = (
                "A replication slot has a non-null xmin/catalog_xmin, meaning it is pinning the "
                "vacuum horizon (age shown as *_age_xids). Correlate with rising dead tuples "
                "before attributing bloat to autovacuum tuning."
            )

    # Replication lag (if replicas connected)
    rows = execute_query(secret_arn, """
        SELECT client_addr::text, usename, application_name, state, sync_state,
               sent_lsn, write_lsn, flush_lsn, replay_lsn,
               pg_size_pretty(pg_wal_lsn_diff(sent_lsn, replay_lsn)) AS replay_lag_size,
               pg_wal_lsn_diff(sent_lsn, replay_lsn) AS replay_lag_bytes,
               write_lag, flush_lag, replay_lag,
               EXTRACT(EPOCH FROM replay_lag)::numeric(10,2) AS replay_lag_seconds
        FROM pg_stat_replication
        ORDER BY replay_lag DESC NULLS LAST
        LIMIT 50
    """, database=database)
    _rl_bands = {"bands": _rt.get("replay_lag_seconds_bands", [])}
    for r in rows:
        r["status"] = _severity_from_percent(_rl_bands, r.get("replay_lag_seconds"))
    repl["replicas"] = rows

    # Synchronous replication config + the configured apply delay. recovery_min_apply_delay
    # (C-081) is read here so a "my reads are stale" / post-failover investigation that calls
    # the replication tool sees the delay directly: a non-zero value is the textbook cause of
    # WAL being flushed within milliseconds while replay runs minutes behind — a deliberate
    # setting, not infrastructure lag. On a primary it is normally 0.
    rows = execute_query(secret_arn, """
        SELECT current_setting('synchronous_standby_names') AS sync_standbys,
               current_setting('synchronous_commit') AS sync_commit,
               current_setting('recovery_min_apply_delay') AS recovery_min_apply_delay
    """, database=database)
    if rows:
        repl["sync_config"] = rows[0]
        _delay = str(rows[0].get("recovery_min_apply_delay") or "").strip()
        if _delay and _delay not in ("0", "0ms"):
            repl["apply_delay_note"] = (
                f"recovery_min_apply_delay is set to {_delay}. Replay lag up to roughly this "
                f"amount is the configured, intended delay, not a replication problem: expect "
                f"WAL flushed on the standby within milliseconds while replay stays behind by "
                f"about this interval. Trust pg_stat_replication (write/flush/replay LSN) as "
                f"ground truth over a CloudWatch ReplicaLag reading, which can report seconds "
                f"of lag at low write rates even with no real problem."
            )

    return repl


@tool
def get_wal_status(secret_arn: str, database: str = "postgres") -> dict:
    """Section 4 subsections: WAL configuration and WAL sender status.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        WAL configuration parameters and sender status
    """
    wal = {}
    pg_ver = _detect_version(secret_arn, database)

    # WAL parameters (version-aware)
    if pg_ver >= 130000:
        rows = execute_query(secret_arn, """
            SELECT name, setting, unit, short_desc
            FROM pg_settings
            WHERE name IN ('wal_level', 'wal_keep_size', 'max_wal_size', 'min_wal_size',
                           'wal_compression', 'archive_mode', 'archive_command')
            ORDER BY name
        """, database=database)
    else:
        rows = execute_query(secret_arn, """
            SELECT name, setting, unit, short_desc
            FROM pg_settings
            WHERE name IN ('wal_level', 'wal_keep_segments', 'max_wal_size', 'min_wal_size',
                           'wal_compression', 'archive_mode', 'archive_command')
            ORDER BY name
        """, database=database)
    wal["parameters"] = rows

    # WAL senders
    rows = execute_query(secret_arn, """
        SELECT pid, usename, application_name, client_addr::text,
               state, sent_lsn, write_lsn, flush_lsn, replay_lsn, sync_state
        FROM pg_stat_replication
        ORDER BY client_addr
        LIMIT 50
    """, database=database)
    # CHK-035 — facts only; verdicts from references.yaml#wal-configuration. wal_level=minimal
    # blocks replication entirely (CRITICAL); a sender in catchup/backup/startup is WARNING.
    # Aurora readers never appear here (storage-level replication) — judge them via AuroraReplicaLag
    # (observability get_replica_lag). RDS read replicas and Multi-AZ DB cluster readers are
    # streaming standbys: they do appear here, and get_replica_lag returns their ReplicaLag.
    states = _check_references().get("wal-configuration", {}).get("thresholds", {}).get("states", {})
    wal_level = next((p.get("setting") for p in (wal.get("parameters") or [])
                      if p.get("name") == "wal_level"), None)
    wal["wal_level_status"] = "CRITICAL" if wal_level == "minimal" else "OK"
    for r in rows:
        st = (r.get("state") or "").lower()
        if st == "streaming":
            r["status"] = "OK"
        elif st in ("catchup", "backup", "startup"):
            r["status"] = str(states.get("sender_recovering", "WARNING")).upper()
        else:
            r["status"] = "NOTICE"
    wal["senders"] = rows

    return wal


@tool
def get_logical_replication(secret_arn: str, database: str = "postgres") -> dict:
    """Section 4.4: Logical replication monitoring — publications, subscriptions,
    and logical replication slots.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Logical replication slot and subscription status
    """
    logical = {}

    rows = execute_query(secret_arn, """
        SELECT slot_name, plugin, slot_type, active,
               pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS lag_size,
               pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS lag_bytes,
               confirmed_flush_lsn
        FROM pg_replication_slots
        WHERE slot_type = 'logical'
        ORDER BY lag_bytes DESC NULLS LAST
        LIMIT 50
    """, database=database)
    # CHK-036 — facts only; severity from references.yaml#logical-replication-health (same
    # slot-lag model as physical slots; a disabled subscription is a data-divergence WARNING).
    _lt = _check_references().get("logical-replication-health", {}).get("thresholds", {})
    for r in rows:
        r["status"] = _severity_slot_lag(_lt, r.get("lag_bytes"), r.get("active"))
        # C-082 — a logical slot's output plugin must match what the subscriber decodes with.
        # Native PostgreSQL logical replication (and AWS DMS CDC) uses `pgoutput`; a slot on a
        # non-pgoutput plugin (test_decoding, wal2json, decoderbufs, ...) that a native
        # subscriber expects will never be consumed, so its restart_lsn is pinned and WAL
        # accumulates — exactly the "wrong plugin, never consumed" case. The publisher side
        # cannot see the subscriber's expectation, so this is a flag to investigate, not a
        # verdict: confirm the consumer actually speaks this plugin.
        _plugin = str(r.get("plugin") or "").strip()
        if _plugin and _plugin != "pgoutput":
            r["plugin_note"] = (
                f"Output plugin is '{_plugin}', not the usual 'pgoutput' used by native "
                f"logical replication and DMS. If the subscriber expects pgoutput it will not "
                f"consume this slot, so the slot stays unconsumed and its WAL is retained "
                f"(check lag_bytes and whether the slot is active). Verify the consumer is "
                f"actually configured for '{_plugin}' before treating this slot as healthy."
            )
    logical["logical_slots"] = rows

    # Publications
    rows = execute_query(secret_arn, """
        SELECT pubname, puballtables, pubinsert, pubupdate, pubdelete, pubtruncate
        FROM pg_publication
        LIMIT 50
    """, database=database)
    logical["publications"] = rows

    # Subscriptions — pg_subscription is readable only by a superuser, so the read-only
    # monitoring role is often denied. A denial is NOT "no subscriptions": an empty list
    # reads as "none, all healthy" and would hide a disabled subscription we simply could
    # not see. Disclose the gap explicitly (scope=unreadable, INCONCLUSIVE) so the result
    # is never mistaken for a clear all-clear.
    try:
        rows = execute_query(secret_arn, """
            SELECT subname, subenabled, subslotname, subpublications
            FROM pg_subscription
            LIMIT 50
        """, database=database)
        for r in rows:
            r["status"] = "OK" if _as_bool(r.get("subenabled")) else "WARNING"
        logical["subscriptions"] = rows
        logical["subscriptions_scope"] = "complete"
    except Exception as e:
        logical["subscriptions"] = []
        logical["subscriptions_scope"] = "unreadable"
        logical["subscriptions_status"] = "INCONCLUSIVE"
        logical["subscriptions_note"] = (
            "Subscription health is UNKNOWN: reading pg_subscription needs a superuser and the "
            "read-only monitoring role was denied. This is not a clear result; a disabled "
            "subscription would not be visible here. Grant the monitoring role access or check "
            "as a superuser to settle it."
        )
        logger.debug(f"pg_subscription read denied: {e}")

    return logical


# ─── Section 5: DATA FOOTPRINT ─────────────────────────────────────────────────

@tool
def get_storage_analysis(secret_arn: str, database: str = "postgres") -> dict:
    """Section 5: Data footprint — database sizes, table sizes, WAL directory analysis.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Storage analysis with database and table sizes
    """
    storage = {}

    rows = execute_query(secret_arn, """
        SELECT datname AS database_name,
               pg_size_pretty(pg_database_size(datname)) AS size,
               pg_database_size(datname) AS size_bytes
        FROM pg_database WHERE datallowconn
        ORDER BY pg_database_size(datname) DESC
    """, database=database)
    storage["databases"] = rows

    # Top tables by size + storage composition (CHK-027). Facts here; the composition
    # verdict (over-indexed / high dead-row %) is applied in Python from references.yaml.
    rows = execute_query(secret_arn, """
        SELECT schemaname, relname AS table_name,
               pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
               pg_total_relation_size(relid) AS total_bytes,
               pg_relation_size(relid) AS heap_bytes,
               pg_indexes_size(relid) AS index_bytes,
               pg_size_pretty(pg_relation_size(relid)) AS table_size,
               pg_size_pretty(pg_indexes_size(relid)) AS index_size,
               n_live_tup AS live_rows, n_dead_tup AS dead_rows
        FROM pg_stat_user_tables
        ORDER BY pg_total_relation_size(relid) DESC
        LIMIT 50
    """, database=database)
    _t = _check_references().get("largest-tables", {}).get("thresholds", {})
    for r in rows:
        r["status"] = _severity_largest_table(_t, r)
    storage["top_tables"] = rows

    # WAL directory (if accessible)
    rows = execute_query(secret_arn, """
        SELECT CASE WHEN has_function_privilege('pg_ls_waldir()', 'execute')
               THEN 'accessible' ELSE 'not accessible' END AS wal_dir_status
    """, database=database)
    if rows and rows[0].get("wal_dir_status") == "accessible":
        wal_rows = execute_query(secret_arn, """
            SELECT count(*) AS wal_file_count,
                   pg_size_pretty(sum(size)) AS total_wal_size,
                   sum(size) AS total_wal_bytes
            FROM pg_ls_waldir()
        """, database=database)
        wd = wal_rows[0] if wal_rows else {}
        if wd:
            # CHK-028 — WAL directory size verdict: worse of the file-count bands and the
            # total-size bands, from references.yaml#wal-directory-size.
            wt = _check_references().get("wal-directory-size", {}).get("thresholds", {})
            by_count = _severity_from_percent({"bands": wt.get("file_count_bands", [])}, wd.get("wal_file_count"))
            by_size = _severity_from_percent({"bands": wt.get("total_bytes_bands", [])}, wd.get("total_wal_bytes"))
            wd["status"] = max((by_count, by_size), key=lambda s: _STATE_ORDER.get(s, 0))
        storage["wal_directory"] = wd

    return storage


@tool
def get_checkpoint_stats(secret_arn: str, database: str = "postgres") -> dict:
    """Section 5 subsection: Checkpoint statistics — checkpoint frequency,
    timing, and WAL configuration.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Checkpoint statistics and configuration
    """
    pg_ver = _detect_version(secret_arn, database)

    # PG17+ uses pg_stat_checkpointer, older uses pg_stat_bgwriter
    if pg_ver >= 170000:
        rows = execute_query(secret_arn, """
            SELECT num_timed AS checkpoints_timed,
                   num_requested AS checkpoints_requested,
                   write_time, sync_time, buffers_written,
                   stats_reset
            FROM pg_stat_checkpointer
        """, database=database)
    else:
        rows = execute_query(secret_arn, """
            SELECT checkpoints_timed, checkpoints_req AS checkpoints_requested,
                   checkpoint_write_time AS write_time,
                   checkpoint_sync_time AS sync_time,
                   buffers_checkpoint AS buffers_written,
                   stats_reset
            FROM pg_stat_bgwriter
        """, database=database)

    config = execute_query(secret_arn, """
        SELECT current_setting('checkpoint_timeout') AS checkpoint_timeout,
               current_setting('checkpoint_completion_target') AS completion_target,
               current_setting('max_wal_size') AS max_wal_size,
               current_setting('min_wal_size') AS min_wal_size
    """, database=database)

    # CHK-029 — forced-checkpoint ratio verdict from references.yaml#checkpoint-health.
    # A high share of REQUESTED (forced) checkpoints means max_wal_size is too small for
    # the write workload (I/O spike risk). requested_pct computed in Python (facts-only SQL).
    st = rows[0] if rows else {}
    if st:
        timed = st.get("checkpoints_timed") or 0
        req = st.get("checkpoints_requested") or 0
        total_cp = timed + req
        st["requested_pct"] = round(100.0 * req / total_cp, 2) if total_cp else 0
        st["status"] = _severity_from_percent(
            _check_references().get("checkpoint-health", {}).get("thresholds", {}), st["requested_pct"])
        # C-045 — requested_pct is a LIFETIME ratio since stats_reset, which on a long-lived
        # instance can span months. A short, severe forced-checkpoint storm (minutes) reads
        # as a tiny lifetime percentage and gets cleared ("0.4% requested, OK") even while it
        # is actively happening. Surface the counter's age so the agent cannot treat a
        # months-long denominator as "current": a window far longer than a typical incident
        # (hours) means requested_pct describes history, not now, and a WARNING/CRITICAL
        # verdict from a short window is more informative than an OK lifetime ratio.
        _add_stats_window_disclosure(st, "requested_pct is a lifetime ratio since stats_reset")
    return {"stats": st, "config": config[0] if config else {}}


@tool
def get_bgwriter_stats(secret_arn: str, database: str = "postgres") -> dict:
    """Section 5 subsection: Background writer statistics — buffer allocation,
    writes, and clean-up efficiency.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Background writer statistics
    """
    pg_ver = _detect_version(secret_arn, database)

    if pg_ver >= 170000:
        # PG17+ moved backend-write counters (buffers_backend / _fsync) out of
        # pg_stat_bgwriter into pg_stat_io; they are not available from this view.
        rows = execute_query(secret_arn, """
            SELECT buffers_clean, maxwritten_clean, buffers_alloc, stats_reset
            FROM pg_stat_bgwriter
        """, database=database)
    else:
        rows = execute_query(secret_arn, """
            SELECT buffers_clean, maxwritten_clean, buffers_alloc,
                   buffers_backend, buffers_backend_fsync, stats_reset
            FROM pg_stat_bgwriter
        """, database=database)

    # CHK-030 — bgwriter efficiency verdict (state model) from references.yaml. Backends
    # forced to fsync = CRITICAL; bgwriter throttled (maxwritten_clean > 0) or backends
    # doing its job (buffers_backend > backend_write_pct of buffers_clean) = WARNING.
    bg = rows[0] if rows else {}
    if bg:
        thr = _check_references().get("bgwriter-efficiency", {}).get("thresholds", {})
        states = thr.get("states", {})
        pct = thr.get("backend_write_pct", 10)
        active = []
        if (bg.get("buffers_backend_fsync") or 0) > 0:
            active.append("backend_fsync")
        if (bg.get("maxwritten_clean") or 0) > 0:
            active.append("maxwritten_clean")
        bb, bc = bg.get("buffers_backend"), bg.get("buffers_clean") or 0
        if bb is not None and bc and bb > (pct / 100.0) * bc:
            active.append("backends_writing")
        bg["status"] = _severity_from_states(states, active)
    return {"bgwriter": bg}


@tool
def get_autovacuum_utilization(secret_arn: str, database: str = "postgres") -> dict:
    """Section 5 subsection: Autovacuum worker utilization — current workers,
    max workers, and tables being vacuumed.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Autovacuum worker utilization and currently running vacuums
    """
    result = {}

    rows = execute_query(secret_arn, """
        SELECT current_setting('autovacuum_max_workers')::int AS max_workers,
               count(*) FILTER (WHERE query ~* 'autovacuum') AS active_workers
        FROM pg_stat_activity
    """, database=database)
    if rows:
        result["utilization"] = rows[0]

    rows = execute_query(secret_arn, """
        SELECT pid, datname, usename,
               EXTRACT(EPOCH FROM (now() - query_start))::int AS duration_seconds,
               LEFT(query, 200) AS query_preview
        FROM pg_stat_activity
        WHERE query ~* 'autovacuum|VACUUM|ANALYZE'
          AND state = 'active' AND pid != pg_backend_pid()
        ORDER BY query_start
        LIMIT 50
    """, database=database)
    result["running_vacuums"] = rows

    # Check for tables with autovacuum explicitly disabled.
    # schema_name is REQUIRED here: without it the caller only has a bare table
    # name and must guess a schema for the ALTER TABLE remediation — which
    # produces wrong-schema DDL when the disabled tables live in a different
    # schema than the flagged/bloated one. Always return schema.table.
    rows = execute_query(secret_arn, """
        SELECT s.schemaname AS schema_name,
               c.relname AS table_name,
               c.reloptions,
               s.n_dead_tup AS dead_tuples,
               s.n_live_tup AS live_tuples,
               CASE WHEN s.n_live_tup + s.n_dead_tup > 0
                    THEN ROUND(100.0 * s.n_dead_tup / (s.n_live_tup + s.n_dead_tup), 2)
                    ELSE 0 END AS dead_pct,
               s.last_autovacuum,
               s.autovacuum_count
        FROM pg_class c
        JOIN pg_stat_user_tables s ON c.oid = s.relid
        WHERE c.reloptions::text LIKE '%%autovacuum_enabled=false%%'
        ORDER BY s.n_dead_tup DESC
    """, database=database)
    if rows:
        result["autovacuum_disabled_tables"] = rows
        qualified = ', '.join(f"{r['schema_name']}.{r['table_name']}" for r in rows)
        result["CRITICAL"] = (
            f"{len(rows)} table(s) have autovacuum explicitly DISABLED "
            f"(autovacuum_enabled=false in reloptions). These tables will NEVER be "
            f"auto-vacuumed regardless of dead tuple count or XID age. "
            f"Tables (schema-qualified): {qualified}. "
            f"Fix per table, using its OWN schema: "
            f"ALTER TABLE <schema>.<table> SET (autovacuum_enabled = true);"
        )

    # Check tables needing vacuum but not being vacuumed (schema-qualified).
    rows = execute_query(secret_arn, """
        SELECT s.schemaname AS schema_name, s.relname AS table_name,
               s.n_dead_tup, s.n_live_tup,
               CASE WHEN s.n_live_tup + s.n_dead_tup > 0
                    THEN ROUND(100.0 * s.n_dead_tup / (s.n_live_tup + s.n_dead_tup), 2)
                    ELSE 0 END AS dead_pct,
               s.last_autovacuum, s.autovacuum_count
        FROM pg_stat_user_tables s
        WHERE s.n_dead_tup > 1000
          AND pg_total_relation_size(s.relid) > 1048576
        ORDER BY s.n_dead_tup DESC LIMIT 20
    """, database=database)
    result["tables_needing_vacuum"] = rows

    return result


@tool
def get_connection_pooling_efficiency(secret_arn: str, database: str = "postgres") -> dict:
    """Section 5 subsection: Connection pooling efficiency — session reuse patterns
    and connection lifetime analysis.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Connection pooling efficiency metrics
    """
    rows = execute_query(secret_arn, """
        SELECT count(*) AS total_connections,
               ROUND(avg(EXTRACT(EPOCH FROM (now() - backend_start)))::numeric, 0) AS avg_session_age_sec,
               min(EXTRACT(EPOCH FROM (now() - backend_start)))::int AS min_session_age_sec,
               max(EXTRACT(EPOCH FROM (now() - backend_start)))::int AS max_session_age_sec,
               count(*) FILTER (WHERE EXTRACT(EPOCH FROM (now() - backend_start)) < 10) AS very_short_sessions,
               count(*) FILTER (WHERE state = 'idle' AND EXTRACT(EPOCH FROM (now() - state_change)) > 300) AS long_idle
        FROM pg_stat_activity
        WHERE pid != pg_backend_pid() AND backend_type = 'client backend'
    """, database=database)
    eff = rows[0] if rows else {}
    # CHK-031 — facts only. Severity = worse of connection churn (very_short/total) and
    # pool-oversize (long_idle/total), % bands from references.yaml#connection-pooling-efficiency.
    if eff:
        total = eff.get("total_connections") or 0
        thr = _check_references().get("connection-pooling-efficiency", {}).get("thresholds", {})
        churn_pct = (100.0 * (eff.get("very_short_sessions") or 0) / total) if total else 0
        idle_pct = (100.0 * (eff.get("long_idle") or 0) / total) if total else 0
        eff["churn_pct"] = round(churn_pct, 1)
        eff["long_idle_pct"] = round(idle_pct, 1)
        s_churn = _severity_from_percent({"bands": thr.get("churn_bands", [])}, churn_pct)
        s_idle = _severity_from_percent({"bands": thr.get("oversized_bands", [])}, idle_pct)
        eff["status"] = max((s_churn, s_idle), key=lambda s: _STATE_ORDER.get(s, 0))
    return {"efficiency": eff}


@tool
def get_buffer_pool_stats(secret_arn: str, database: str = "postgres") -> dict:
    """Section 5 subsection: Buffer pool hit ratios by database.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Buffer pool hit ratios per database
    """
    rows = execute_query(secret_arn, """
        SELECT datname,
               blks_hit, blks_read,
               CASE WHEN blks_hit + blks_read = 0 THEN 100.0
                    ELSE ROUND(100.0 * blks_hit / (blks_hit + blks_read), 2)
               END AS hit_ratio_percent,
               stats_reset,
               pg_size_pretty(pg_database_size(datname)) AS db_size,
               pg_database_size(datname) AS db_size_bytes
        FROM pg_stat_database
        WHERE datname NOT LIKE 'template%%'
        ORDER BY hit_ratio_percent ASC, pg_database_size(datname) DESC
    """, database=database)
    # CHK-032 — facts only. Cache hit ratio is INVERSE (lower = worse); bands from
    # references.yaml#database-cache-hit via the below-threshold evaluator.
    thr = _check_references().get("database-cache-hit", {}).get("thresholds", {})
    for r in rows:
        r["status"] = _severity_below(thr, r.get("hit_ratio_percent"))
        # C-047 — hit_ratio_percent here is computed from blks_hit/blks_read in
        # pg_stat_database, which are LIFETIME counters since stats_reset. On a long-lived
        # instance this cumulative ratio (e.g. 98.8%) can completely mask a current cache
        # collapse (e.g. 71% live after a working-set grew past shared_buffers): the recent
        # misses are a rounding error against months of accumulated hits. Surface the
        # counter-window age so the agent reads this as the average-since-reset, not the
        # live ratio, and corroborates a suspected cache-hit incident with a short-window
        # signal (CloudWatch BufferCacheHitRatio, or two samples minutes apart) before
        # clearing it. Verdict bands (references.yaml#database-cache-hit) are unchanged.
        _add_stats_window_disclosure(r, "hit_ratio_percent is a lifetime ratio since stats_reset")
    return {"buffer_stats": rows}


@tool
def get_lock_analysis(secret_arn: str, database: str = "postgres") -> dict:
    """Section 5 subsection: Lock tree and deadlock analysis — blocked queries,
    blocking chains, and lock contention.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Lock contention analysis with blocking chains
    """
    lock_data = {}

    rows = execute_query(secret_arn, """
        WITH lock_waits AS (
            SELECT blocked_locks.pid AS blocked_pid,
                   blocked_activity.usename AS blocked_user,
                   blocking_locks.pid AS blocking_pid,
                   blocking_activity.usename AS blocking_user,
                   blocked_locks.mode AS blocked_mode,
                   blocking_locks.mode AS blocking_mode,
                   blocked_locks.locktype AS lock_type,
                   blocked_locks.relation::regclass AS locked_relation,
                   EXTRACT(EPOCH FROM (now() - blocked_activity.query_start))::int AS wait_seconds,
                   LEFT(blocked_activity.query, 100) AS blocked_query,
                   LEFT(blocking_activity.query, 100) AS blocking_query
            FROM pg_locks blocked_locks
            JOIN pg_stat_activity blocked_activity ON blocked_activity.pid = blocked_locks.pid
            JOIN pg_locks blocking_locks
                ON blocking_locks.locktype = blocked_locks.locktype
                AND blocking_locks.database IS NOT DISTINCT FROM blocked_locks.database
                AND blocking_locks.relation IS NOT DISTINCT FROM blocked_locks.relation
                AND blocking_locks.page IS NOT DISTINCT FROM blocked_locks.page
                AND blocking_locks.tuple IS NOT DISTINCT FROM blocked_locks.tuple
                AND blocking_locks.virtualxid IS NOT DISTINCT FROM blocked_locks.virtualxid
                AND blocking_locks.transactionid IS NOT DISTINCT FROM blocked_locks.transactionid
                AND blocking_locks.classid IS NOT DISTINCT FROM blocked_locks.classid
                AND blocking_locks.objid IS NOT DISTINCT FROM blocked_locks.objid
                AND blocking_locks.objsubid IS NOT DISTINCT FROM blocked_locks.objsubid
                AND blocking_locks.pid != blocked_locks.pid
            JOIN pg_stat_activity blocking_activity ON blocking_activity.pid = blocking_locks.pid
            WHERE NOT blocked_locks.granted
        )
        SELECT * FROM lock_waits ORDER BY wait_seconds DESC LIMIT 50
    """, database=database)
    # CHK-033 — facts only. Per blocked-query severity from the wait-seconds bands, and a
    # separate deadlock verdict from the deadlock-count bands (references.yaml#lock-contention).
    _lt = _check_references().get("lock-contention", {}).get("thresholds", {})
    _wait_bands = {"bands": _lt.get("wait_seconds_bands", [])}
    for r in rows:
        r["status"] = _severity_from_percent(_wait_bands, r.get("wait_seconds"))
    lock_data["blocked_queries"] = rows

    # Deadlock stats
    drows = execute_query(secret_arn, """
        SELECT datname, deadlocks, conflicts
        FROM pg_stat_database WHERE datname = current_database()
    """, database=database)
    if drows:
        d = drows[0]
        d["status"] = _severity_from_percent({"bands": _lt.get("deadlock_bands", [])}, d.get("deadlocks"))
        lock_data["deadlock_stats"] = d

    return lock_data


# ─── Section 6: QUERY & I/O PERFORMANCE ────────────────────────────────────────

@tool
def get_io_statistics(secret_arn: str, database: str = "postgres") -> dict:
    """Section 6: I/O statistics — per-table heap/index block reads and hits,
    cache hit ratios, and I/O health assessment.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        I/O statistics with cache hit ratios per table
    """
    rows = execute_query(secret_arn, """
        WITH tio AS (
            SELECT schemaname, relname, relid,
                   heap_blks_read, heap_blks_hit,
                   idx_blks_read, idx_blks_hit,
                   toast_blks_read, toast_blks_hit,
                   COALESCE(pg_total_relation_size(relid), 0) AS total_bytes
            FROM pg_statio_user_tables WHERE heap_blks_read > 0
        ),
        ts AS (
            SELECT relid, n_live_tup, n_dead_tup, seq_scan, idx_scan
            FROM pg_stat_user_tables
        )
        SELECT tio.schemaname, tio.relname AS table_name,
               pg_size_pretty(tio.total_bytes) AS total_size,
               tio.heap_blks_read AS table_blocks_read,
               tio.heap_blks_hit AS table_blocks_hit,
               ROUND(100.0 * tio.heap_blks_hit / NULLIF(tio.heap_blks_hit + tio.heap_blks_read, 0), 2) AS table_cache_hit_ratio,
               tio.idx_blks_read AS index_blocks_read,
               tio.idx_blks_hit AS index_blocks_hit,
               ROUND(100.0 * tio.idx_blks_hit / NULLIF(tio.idx_blks_hit + tio.idx_blks_read, 0), 2) AS index_cache_hit_ratio,
               tio.heap_blks_read AS heap_blks_read_raw
        FROM tio JOIN ts ON tio.relid = ts.relid
        ORDER BY tio.heap_blks_read DESC LIMIT 30
    """, database=database)
    # CHK-019 — facts only. Cache hit ratio is INVERSE (lower = worse); bands from
    # references.yaml#per-table-cache-hit applied via the below-threshold evaluator. Only
    # meaningful once meaningful disk reads have occurred (heap_blks_read > min_reads).
    thr = _check_references().get("per-table-cache-hit", {}).get("thresholds", {})
    min_reads = thr.get("min_reads", 1000)
    for r in rows:
        if (r.get("heap_blks_read_raw") or 0) > min_reads:
            r["status"] = _severity_below(thr, r.get("table_cache_hit_ratio"))
        else:
            r["status"] = "OK"  # too few reads to judge the ratio
    return {"io_stats": rows}


@tool
def get_query_performance(secret_arn: str, database: str = "postgres", order_by: str = "time") -> dict:
    """Section 6: Query performance analysis from pg_stat_statements — top queries
    by total time, calls, mean time, with cache hit ratios and severity classification,
    plus what the daily report measures per query: WAL generated (wal_bytes, full-page
    images), I/O time (block read/write time, when track_io_timing is on), planning
    time, stddev / max execution time and temp blocks.
    Use ``order_by`` to rank by what the question is about: 'time' (default), 'wal'
    ("which queries burn the most WAL"), 'io' ("which wait most on I/O"), 'calls' or
    'temp'. Falls back to pg_stat_activity if pg_stat_statements is not available.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze
        order_by: 'time' | 'wal' | 'io' | 'calls' | 'temp' (default 'time')

    Returns:
        Top queries with performance metrics and severity classification
    """
    pg_ver = _detect_version(secret_arn, database)
    has_pgss = _has_extension(secret_arn, database, "pg_stat_statements")

    if has_pgss:
        # Version-aware column names. These are hard-coded literals selected by a
        # PostgreSQL version check — never user input — so the f-string below is
        # not an injection vector. PG13 renamed total_time → total_exec_time and added
        # planning and WAL columns; PG17 renamed blk_read_time → shared_blk_read_time.
        new_cols = pg_ver >= 130000
        time_col = "total_exec_time" if new_cols else "total_time"
        mean_col = "mean_exec_time" if new_cols else "mean_time"
        stddev_col = "stddev_exec_time" if new_cols else "stddev_time"
        max_col = "max_exec_time" if new_cols else "max_time"
        plan_col = "ROUND(total_plan_time::numeric, 2)" if new_cols else "NULL::numeric"
        wal_cols = ("wal_records, wal_fpi, wal_bytes" if new_cols
                    else "NULL::bigint AS wal_records, NULL::bigint AS wal_fpi, NULL::numeric AS wal_bytes")
        io_read = "shared_blk_read_time" if pg_ver >= 170000 else "blk_read_time"
        io_write = "shared_blk_write_time" if pg_ver >= 170000 else "blk_write_time"
        orders = {
            "time": time_col,
            "wal": "wal_bytes" if new_cols else time_col,
            "io": f"{io_read} + {io_write}",
            "calls": "calls",
            "temp": "temp_blks_read + temp_blks_written",
        }
        order_sql = orders.get(str(order_by or "time").strip().lower(), time_col)

        rows = execute_query(secret_arn, f"""
            SELECT queryid, LEFT(query, 300) AS query_preview,
                   calls, ROUND({time_col}::numeric, 2) AS total_time_ms,
                   ROUND({mean_col}::numeric, 2) AS mean_time_ms,
                   ROUND({stddev_col}::numeric, 2) AS stddev_time_ms,
                   ROUND({max_col}::numeric, 2) AS max_time_ms,
                   {plan_col} AS total_plan_time_ms,
                   rows,
                   shared_blks_hit, shared_blks_read, shared_blks_dirtied, shared_blks_written,
                   ROUND(100.0 * shared_blks_hit / NULLIF(shared_blks_hit + shared_blks_read, 0), 2) AS cache_hit_ratio,
                   temp_blks_read + temp_blks_written AS temp_blocks,
                   ROUND({io_read}::numeric, 2) AS blk_read_time_ms,
                   ROUND({io_write}::numeric, 2) AS blk_write_time_ms,
                   {wal_cols}
            FROM pg_stat_statements
            WHERE calls > 10 AND query NOT LIKE 'SET %%' AND query NOT LIKE 'SHOW %%'
            ORDER BY {order_sql} DESC NULLS LAST LIMIT 30
        """, database=database)  # nosec B608 - every interpolated piece is a version-selected literal, not user input
        for r in rows:
            wb = r.get("wal_bytes")
            r["wal_mb"] = round(float(wb) / (1024 * 1024), 2) if wb is not None else None
        # CHK-020 — facts only. Severity = worst of the mean-time bands, a temp-spill flag,
        # and a per-query low cache-hit flag; all from references.yaml#top-expensive-queries.
        thr = _check_references().get("top-expensive-queries", {}).get("thresholds", {})
        mean_bands = {"bands": thr.get("mean_time_bands", [])}
        cache_floor = thr.get("cache_hit_warning_below", 95)
        for r in rows:
            s = _severity_from_percent(mean_bands, r.get("mean_time_ms"))
            if (r.get("temp_blocks") or 0) > 0 and _STATE_ORDER["WARNING"] > _STATE_ORDER.get(s, 0):
                s = "WARNING"
            chr_ = r.get("cache_hit_ratio")
            if chr_ is not None and float(chr_) < cache_floor and _STATE_ORDER["WARNING"] > _STATE_ORDER.get(s, 0):
                s = "WARNING"
            r["severity"] = s
        return {"source": "pg_stat_statements", "queries": rows}
    else:
        # Fallback: active queries from pg_stat_activity
        rows = execute_query(secret_arn, """
            SELECT pid, usename, datname,
                   EXTRACT(EPOCH FROM (now() - query_start))::numeric(10,2) AS duration_seconds,
                   state, wait_event_type, wait_event,
                   LEFT(query, 300) AS query_preview
            FROM pg_stat_activity
            WHERE state = 'active' AND pid != pg_backend_pid()
            ORDER BY query_start ASC LIMIT 30
        """, database=database)
        # Fallback (no pg_stat_statements): severity from the running duration bands.
        fb = {"bands": _check_references().get("top-expensive-queries", {}).get(
            "thresholds", {}).get("fallback_duration_bands", [])}
        for r in rows:
            r["severity"] = _severity_from_percent(fb, r.get("duration_seconds"))
        return {"source": "pg_stat_activity (pg_stat_statements not available)", "queries": rows}


@tool
def get_query_plan(secret_arn: str, query_text: str,
                   database: str = "postgres") -> dict:
    """Run EXPLAIN on a query and return the execution plan with recommendations.
    Uses multiple fallback strategies: JSON format, TEXT format, parameter replacement.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        query_text: The SQL query to explain (SELECT only)
        database: Database name

    Returns:
        Query execution plan with analysis
    """
    result = {"query": query_text[:500]}

    # Security: only allow SELECT / WITH ... SELECT
    if not re.match(r'^\s*(WITH\s+|SELECT\s)', query_text, re.IGNORECASE):
        return {"error": "Only SELECT queries can be explained for safety"}

    # The shared read-query safety gate: multi-statement, Unicode-escape,
    # privileged keywords, and sensitive catalogs — applied even under EXPLAIN.
    _err = _reject_unsafe_read_sql(query_text)
    if _err:
        _audit_query_plan(query_text, database, allowed=False, reason="unsafe_sql")
        return {"error": _err}

    # Target-database pin: this is the one tool that accepts BOTH model-authored SQL
    # and a model-chosen database, so pin the target to the record this credential belongs to.
    # Allow the record's own database and the standard maintenance db ('postgres', used for
    # cluster-wide catalog reads); refuse a third, arbitrary name so the model cannot aim an
    # EXPLAIN at an unrelated co-tenant database on the same instance. Fail-soft: when the
    # record has no registered database_name, keep the prior behaviour rather than blocking.
    record_db = lookup_record_database_name(secret_arn)
    requested = (database or "").strip()
    if record_db and requested and requested.lower() not in {record_db.lower(), "postgres"}:
        _audit_query_plan(query_text, database, allowed=False, reason="database_mismatch")
        # Non-confirming: do NOT echo record_db or the requested name, or the refusal would
        # confirm the co-tenant database's existence/name. The real target is recorded in
        # the audit log above. Shares the tenancy guard's wording.
        return {"error": SECRET_NOT_IN_SPACE_MESSAGE}
    _audit_query_plan(query_text, database, allowed=True)

    # Strategy 1: EXPLAIN (FORMAT JSON)
    try:
        rows = execute_query(secret_arn, f"EXPLAIN (FORMAT JSON) {query_text}", database=database)
        if rows:
            result["plan"] = rows
            result["format"] = "json"
            return result
    except Exception as e:
        result["json_error"] = str(e)

    # Strategy 2: EXPLAIN (FORMAT TEXT)
    try:
        rows = execute_query(secret_arn, f"EXPLAIN {query_text}", database=database)
        if rows:
            result["plan"] = rows
            result["format"] = "text"
            return result
    except Exception as e:
        result["text_error"] = str(e)

    # Strategy 3: Replace $1, $2 params with NULL
    try:
        cleaned = re.sub(r'\$\d+', 'NULL', query_text)
        rows = execute_query(secret_arn, f"EXPLAIN {cleaned}", database=database)
        if rows:
            result["plan"] = rows
            result["format"] = "text (params replaced with NULL)"
            return result
    except Exception as e:
        result["fallback_error"] = str(e)

    result["error"] = "All EXPLAIN strategies failed"
    return result


@tool
def get_transaction_stats(secret_arn: str, database: str = "postgres") -> dict:
    """Section 6 subsection: Transaction statistics — XID age, wraparound risk,
    commit/rollback ratios per database.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Transaction statistics with XID wraparound risk assessment
    """
    # datfrozenxid lives on pg_database; the commit/rollback counters live on
    # pg_stat_database — join both (join on oid=datid so shared/global rows drop out).
    rows = execute_query(secret_arn, f"""
        SELECT d.datname,
               age(d.datfrozenxid) AS xid_age,
               ROUND(100.0 * age(d.datfrozenxid) / {XID_WRAPAROUND_MAX}, 2) AS xid_age_percent,
               current_setting('autovacuum_freeze_max_age')::bigint AS freeze_max_age,
               s.xact_commit, s.xact_rollback,
               CASE WHEN s.xact_commit + s.xact_rollback > 0
                    THEN ROUND(100.0 * s.xact_rollback / (s.xact_commit + s.xact_rollback), 2)
                    ELSE 0 END AS rollback_pct
        FROM pg_database d
        JOIN pg_stat_database s ON s.datid = d.oid
        WHERE d.datallowconn
        ORDER BY age(d.datfrozenxid) DESC
    """, database=database)
    # CHK-021 — facts only. Severity = worse of the rollback-ratio bands
    # (references.yaml#transaction-health) and the XID age (reusing the shared
    # xid-wraparound-database model, so the two checks can never disagree).
    roll_thr = _check_references().get("transaction-health", {}).get("thresholds", {})
    xid_thr = _check_references().get("xid-wraparound-database", {}).get("thresholds", {})
    for r in rows:
        s_roll = _severity_from_percent(roll_thr, r.get("rollback_pct"))
        s_xid = _severity_from_xid_bands(xid_thr, r.get("xid_age"), r.get("freeze_max_age"))
        r["status"] = max((s_roll, s_xid), key=lambda s: _STATE_ORDER.get(s, 0))
    return {"transactions": rows}


@tool
def get_function_performance(secret_arn: str, database: str = "postgres") -> dict:
    """Section 6 subsection: Function/procedure performance — top functions by
    total time, calls, and self time. Requires track_functions = 'all' or 'pl'.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Function performance metrics
    """
    # Check if tracking is enabled
    rows = execute_query(secret_arn, """
        SELECT current_setting('track_functions') AS track_functions
    """, database=database)
    tracking = rows[0]["track_functions"] if rows else "none"

    if tracking in ("all", "pl"):
        rows = execute_query(secret_arn, """
            SELECT schemaname, funcname, calls,
                   ROUND(total_time::numeric, 2) AS total_time_ms,
                   ROUND(self_time::numeric, 2) AS self_time_ms,
                   ROUND((total_time / NULLIF(calls, 0))::numeric, 2) AS avg_time_ms
            FROM pg_stat_user_functions
            ORDER BY total_time DESC LIMIT 50
        """, database=database)
        # CHK-022 — facts only. Severity from references.yaml#function-performance:
        # avg_time_ms > avg_critical_ms CRITICAL; > avg_warning_ms on > warning_min_calls
        # calls WARNING. Applied in Python (per-row, two-signal).
        thr = _check_references().get("function-performance", {}).get("thresholds", {})
        crit_ms = thr.get("avg_critical_ms", 1000)
        warn_ms = thr.get("avg_warning_ms", 100)
        warn_calls = thr.get("warning_min_calls", 1000)
        for r in rows:
            avg = float(r.get("avg_time_ms") or 0)
            calls = r.get("calls") or 0
            if avg > crit_ms:
                r["status"] = "CRITICAL"
            elif avg > warn_ms and calls > warn_calls:
                r["status"] = "WARNING"
            else:
                r["status"] = "OK"
        return {"tracking": tracking, "functions": rows}
    else:
        return {"tracking": tracking, "functions": [],
                "note": "track_functions is not enabled. Set to 'all' or 'pl' to collect function stats."}


@tool
def get_wait_event_analysis(secret_arn: str, database: str = "postgres") -> dict:
    """Section 6 subsection: Wait event analysis — breakdown of wait events
    by type and event for currently active sessions.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Wait event breakdown
    """
    rows = execute_query(secret_arn, """
        SELECT wait_event_type, wait_event, count(*) AS sessions,
               array_agg(DISTINCT usename) AS users,
               array_agg(DISTINCT datname) AS databases
        FROM pg_stat_activity
        WHERE wait_event_type IS NOT NULL AND pid != pg_backend_pid()
        GROUP BY wait_event_type, wait_event
        ORDER BY count(*) DESC
        LIMIT 50
    """, database=database)
    # CHK-023 — facts only. Severity by wait_event_type + session count, from
    # references.yaml#wait-events. Client:* / idle waits are normal (OK). Lock:* and IO:*
    # escalate with concurrency (contention / storage bottleneck).
    thr = _check_references().get("wait-events", {}).get("thresholds", {})
    for r in rows:
        et = (r.get("wait_event_type") or "").lower()
        n = r.get("sessions") or 0
        if et == "lock":
            r["status"] = ("CRITICAL" if n > thr.get("lock_critical_sessions", 10)
                           else "WARNING" if n >= thr.get("lock_warning_sessions", 2) else "NOTICE")
        elif et == "io":
            r["status"] = ("CRITICAL" if n > thr.get("io_critical_sessions", 20)
                           else "WARNING" if n > thr.get("io_warning_sessions", 5) else "NOTICE")
        else:
            r["status"] = "OK"  # Client:*, Activity:*, idle waits — normal
    return {"wait_events": rows}


@tool
def get_session_duration_analysis(secret_arn: str, database: str = "postgres") -> dict:
    """Section 6 subsection: Session duration analysis — distribution of session
    ages and identification of long-running sessions.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Session duration distribution
    """
    rows = execute_query(secret_arn, """
        SELECT usename, datname, application_name,
               backend_type, state,
               EXTRACT(EPOCH FROM (now() - backend_start))::int AS session_age_seconds,
               EXTRACT(EPOCH FROM (now() - COALESCE(query_start, state_change)))::int AS last_activity_seconds,
               client_addr::text
        FROM pg_stat_activity
        WHERE pid != pg_backend_pid() AND backend_type = 'client backend'
        ORDER BY backend_start ASC
        LIMIT 50
    """, database=database)
    # CHK-024 — facts only. Severity from references.yaml#long-lived-sessions (session age
    # in seconds; >1h WARNING, >24h CRITICAL). A long session may be a legitimate pool
    # connection — the skill checks application_name / state before calling it a leak.
    thr = _check_references().get("long-lived-sessions", {}).get("thresholds", {})
    for r in rows:
        r["status"] = _severity_from_percent(thr, r.get("session_age_seconds"))
    return {"sessions": rows}


@tool
def get_prepared_statement_analysis(secret_arn: str, database: str = "postgres") -> dict:
    """Section 6 subsection: Prepared statement analysis — prepared statements
    visible to THIS monitoring session only.

    pg_prepared_statements is session-local: it lists only the current connection's
    prepared statements, and PostgreSQL exposes no cross-session view. This tool opens
    its own connection, so the result is normally empty, and an empty list does NOT mean
    there is no prepared-statement memory growth on the application's own long-lived
    connections. Diagnose that from the FreeableMemory trend plus long-lived sessions,
    never from an empty list here.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Prepared statement usage for this session, with a scope caveat
    """
    rows = execute_query(secret_arn, """
        SELECT name, statement, prepare_time, parameter_types, from_sql
        FROM pg_prepared_statements
        ORDER BY prepare_time DESC
        LIMIT 50
    """, database=database)
    return {
        "prepared_statements": rows,
        "scope": "session-local (this monitoring connection only)",
        "note": (
            "pg_prepared_statements shows only this session's prepared statements; "
            "PostgreSQL has no cross-session view. An empty list is NOT evidence of "
            "healthy memory use. Assess prepared-statement accumulation from the "
            "FreeableMemory trend on the application's long-lived connections instead."
        ),
    }


@tool
def get_temp_file_queries(secret_arn: str, database: str = "postgres") -> dict:
    """Section 6 subsection: Temp file heavy queries — queries that spill to disk
    via temp files, indicating work_mem pressure.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Queries with high temp file usage
    """
    has_pgss = _has_extension(secret_arn, database, "pg_stat_statements")
    if not has_pgss:
        return {"note": "pg_stat_statements not available", "queries": []}

    pg_ver = _detect_version(secret_arn, database)
    mean_col = "mean_exec_time" if pg_ver >= 130000 else "mean_time"

    rows = execute_query(secret_arn, f"""
        SELECT LEFT(query, 200) AS query_preview,
               calls, rows,
               temp_blks_read, temp_blks_written,
               pg_size_pretty((temp_blks_read + temp_blks_written)::bigint * 8192) AS temp_size,
               ROUND({mean_col}::numeric, 2) AS mean_time_ms
        FROM pg_stat_statements
        WHERE temp_blks_read + temp_blks_written > 0
        ORDER BY temp_blks_read + temp_blks_written DESC
        LIMIT 30
    """, database=database)  # nosec B608 - mean_col is a version-selected literal, not user input
    # CHK-025 — facts only. Severity from references.yaml#temp-file-spills (temp_blks_written
    # bands: > 1000 WARNING, > 10000 CRITICAL).
    thr = _check_references().get("temp-file-spills", {}).get("thresholds", {})
    for r in rows:
        r["severity"] = _severity_from_percent(thr, r.get("temp_blks_written"))
    return {"queries": rows}


# ─── Section 7: MAINTENANCE & HEALTH ──────────────────────────────────────────

@tool
def get_maintenance_status(secret_arn: str, database: str = "postgres") -> dict:
    """Section 7: Database integrity check — data checksums, corruption detection,
    invalid indexes, orphaned large objects, and XID wraparound risk.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Database integrity and maintenance health status
    """
    maint = {}

    # Data checksums
    rows = execute_query(secret_arn, """
        SELECT current_setting('data_checksums') AS data_checksums
    """, database=database)
    if rows:
        maint["data_checksums"] = rows[0]["data_checksums"]

    # Invalid / not-ready indexes (CHK-003). Facts only (schema-qualified list); the
    # collection verdict (0 rows = OK, >= 1 = WARNING) is applied from
    # references.yaml#invalid-indexes. CRITICAL escalation (invalid index on a large /
    # high-write table) is a skill judgment — this lists names, not sizes.
    rows = execute_query(secret_arn, """
        SELECT n.nspname AS schema_name, c.relname AS index_name,
               ct.relname AS table_name, i.indisvalid, i.indisready
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indexrelid
        JOIN pg_class ct ON ct.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE NOT i.indisvalid OR NOT i.indisready
        LIMIT 50
    """, database=database)
    maint["invalid_indexes"] = rows
    maint["invalid_indexes_status"] = _severity_rows_returned(
        _check_references().get("invalid-indexes", {}).get("thresholds", {}), len(rows))

    # XID wraparound risk (CHK-001, database level). Facts only: the severity POLICY
    # (relative-to-autovacuum_freeze_max_age tiers + absolute 2B-budget floors) lives in
    # references.yaml#xid-wraparound-database and is applied in Python below — so tuning
    # the bands needs no code change, and the tool hardcodes no threshold constants.
    rows = execute_query(secret_arn, f"""
        SELECT datname,
               age(datfrozenxid) AS xid_age,
               ROUND(100.0 * age(datfrozenxid) / {XID_WRAPAROUND_MAX}, 2) AS xid_age_pct,
               current_setting('autovacuum_freeze_max_age')::bigint AS freeze_max_age
        FROM pg_database WHERE datallowconn
        ORDER BY age(datfrozenxid) DESC
    """, database=database)
    _db_xid_thr = _check_references().get("xid-wraparound-database", {}).get("thresholds", {})
    for r in rows:
        r["status"] = _severity_from_xid_bands(_db_xid_thr, r.get("xid_age"), r.get("freeze_max_age"))
    maint["xid_wraparound"] = rows

    # Per-table XID age (CHK-002, top offenders). `WHERE c.relfrozenxid <> 0` skips
    # partitioned parents and other zero-frozenxid relations (false positives). Columns
    # are qualified (s./c.) to avoid the ambiguous 'relname'. Facts only — severity from
    # references.yaml#xid-wraparound-table (same model as db-level, applied in Python).
    rows = execute_query(secret_arn, """
        SELECT s.schemaname, s.relname,
               age(c.relfrozenxid) AS xid_age,
               ROUND(100.0 * age(c.relfrozenxid) /
                     current_setting('autovacuum_freeze_max_age')::numeric, 2) AS freeze_pct,
               current_setting('autovacuum_freeze_max_age')::bigint AS freeze_max_age,
               s.last_vacuum, s.last_autovacuum,
               s.n_dead_tup, s.n_live_tup
        FROM pg_stat_user_tables s
        JOIN pg_class c ON c.oid = s.relid
        WHERE c.relfrozenxid <> 0
        ORDER BY age(c.relfrozenxid) DESC
        LIMIT 30
    """, database=database)
    _tbl_xid_thr = _check_references().get("xid-wraparound-table", {}).get("thresholds", {})
    for r in rows:
        r["status"] = _severity_from_xid_bands(_tbl_xid_thr, r.get("xid_age"), r.get("freeze_max_age"))
    maint["table_xid_age"] = rows

    return maint


@tool
def get_sequence_exhaustion(secret_arn: str, database: str = "postgres") -> dict:
    """Section 7 subsection: Sequence exhaustion analysis — sequences approaching
    their max value.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Sequences with exhaustion risk
    """
    pg_ver = _detect_version(secret_arn, database)
    if pg_ver < 100000:
        return {"note": "pg_sequences view requires PostgreSQL 10+", "sequences": []}

    # CHK-004 — facts only. usage_pct handles ascending (last/max) AND descending
    # (last/min) sequences; severity bands (NOTICE/WARNING/CRITICAL/EMERGENCY) are read
    # from references.yaml#sequence-exhaustion and applied in Python (no CASE in SQL).
    rows = execute_query(secret_arn, """
        SELECT schemaname, sequencename, data_type,
               last_value, max_value, min_value, increment_by,
               CASE WHEN increment_by > 0 AND max_value > 0
                        THEN ROUND(100.0 * last_value / max_value, 2)
                    WHEN increment_by < 0 AND min_value < 0
                        THEN ROUND(100.0 * last_value / min_value, 2)
                    ELSE 0 END AS usage_pct
        FROM pg_sequences
        WHERE last_value IS NOT NULL
        ORDER BY usage_pct DESC
        LIMIT 50
    """, database=database)
    thr = _check_references().get("sequence-exhaustion", {}).get("thresholds", {})
    for r in rows:
        r["status"] = _severity_from_percent(thr, r.get("usage_pct"))
    return {"sequences": rows}


@tool
def get_constraint_validation(secret_arn: str, database: str = "postgres") -> dict:
    """Section 7 subsection: Constraint validation status — NOT VALID constraints
    that need validation.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Constraints needing validation
    """
    # CHK-005 — facts only (schema-qualified list, matches the reference catalog). Collection
    # verdict (0 rows = OK, >= 1 = WARNING) from references.yaml#unvalidated-constraints;
    # CRITICAL escalation (unvalidated FK on a high-traffic/critical table) is skill judgment.
    rows = execute_query(secret_arn, """
        SELECT n.nspname AS schema_name, c.relname AS table_name,
               con.conname AS constraint_name, con.contype AS constraint_type,
               con.convalidated AS is_validated,
               pg_get_constraintdef(con.oid) AS definition
        FROM pg_constraint con
        JOIN pg_class c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE NOT con.convalidated
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
        ORDER BY n.nspname, c.relname
        LIMIT 50
    """, database=database)
    return {
        "unvalidated_constraints": rows,
        "unvalidated_constraints_status": _severity_rows_returned(
            _check_references().get("unvalidated-constraints", {}).get("thresholds", {}), len(rows)),
    }


@tool
def get_materialized_view_freshness(secret_arn: str, database: str = "postgres") -> dict:
    """Section 7 subsection: Materialized view freshness — last refresh time
    and staleness assessment.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Materialized view freshness status
    """
    # CHK-006 — facts only. Joins pg_class (relkind='m') by oid (robust vs a name join)
    # and reports relispopulated + has_unique_index. last_analyze is a STATS proxy, NOT
    # refresh recency (REFRESH doesn't update stats); true freshness = CW Logs REFRESH
    # (see references). Severity (state model) is applied in Python from references.yaml.
    rows = execute_query(secret_arn, """
        SELECT nsp.nspname AS schemaname,
               c.relname AS matviewname,
               c.relispopulated AS ispopulated,
               pg_size_pretty(pg_total_relation_size(c.oid)) AS size,
               s.n_live_tup AS est_rows,
               s.last_analyze, s.last_autoanalyze,
               EXISTS (SELECT 1 FROM pg_index i
                       WHERE i.indrelid = c.oid AND i.indisunique) AS has_unique_index
        FROM pg_class c
        JOIN pg_namespace nsp ON nsp.oid = c.relnamespace
        LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
        WHERE c.relkind = 'm'
          AND nsp.nspname NOT IN ('pg_catalog', 'information_schema')
        ORDER BY (NOT c.relispopulated) DESC,
                 (s.last_analyze IS NULL AND s.last_autoanalyze IS NULL) DESC,
                 pg_total_relation_size(c.oid) DESC
        LIMIT 50
    """, database=database)
    states = _check_references().get("materialized-view-freshness", {}).get("thresholds", {}).get("states", {})
    for r in rows:
        active = []
        if not _as_bool(r.get("ispopulated")):
            active.append("unpopulated")
        if r.get("last_analyze") is None and r.get("last_autoanalyze") is None:
            active.append("never_analyzed")
        if not _as_bool(r.get("has_unique_index")):
            active.append("no_unique_index")
        r["status"] = _severity_from_states(states, active)
    return {"materialized_views": rows}


@tool
def get_stale_statistics(secret_arn: str, database: str = "postgres") -> dict:
    """Section 7 subsection: tables whose planner statistics (ANALYZE) are stale — the
    planner is working from outdated row-distribution estimates, which produces bad
    plans (wrong join order/index/scan). Distinct from vacuum staleness: a table can be
    vacuumed but not analyzed.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Tables > 10MB with days_since_analyze + modified-row backlog, worst first.
    """
    # CHK-050 — facts only. Severity (days-since-analyze bands + modified-row-% bands,
    # worst of the two) is read from references.yaml#stale-statistics and applied in
    # Python. COALESCE to 'epoch' makes a never-analyzed table surface as a very large
    # days_since_analyze, so it lands in the top (CRITICAL) day band without special-casing.
    rows = execute_query(secret_arn, """
        SELECT schemaname, relname,
               last_analyze, last_autoanalyze,
               (EXTRACT(EPOCH FROM (now() - GREATEST(
                    COALESCE(last_analyze, 'epoch'::timestamptz),
                    COALESCE(last_autoanalyze, 'epoch'::timestamptz))))::bigint / 86400)
                   AS days_since_analyze,
               n_live_tup, n_mod_since_analyze,
               ROUND(100.0 * n_mod_since_analyze / NULLIF(n_live_tup, 0), 2) AS mod_pct
        FROM pg_stat_user_tables
        WHERE pg_total_relation_size(relid) > 10485760
        ORDER BY days_since_analyze DESC NULLS FIRST
        LIMIT 30
    """, database=database)
    thr = _check_references().get("stale-statistics", {}).get("thresholds", {})
    day_bands = {"bands": thr.get("days_bands", [])}
    mod_bands = {"bands": thr.get("mod_pct_bands", [])}
    for r in rows:
        by_days = _severity_from_percent(day_bands, r.get("days_since_analyze"))
        by_mod = _severity_from_percent(mod_bands, r.get("mod_pct"))
        r["status"] = max((by_days, by_mod), key=lambda s: _STATE_ORDER.get(s, 0))
    return {"tables": rows}


@tool
def get_vacuum_stats_health(secret_arn: str, database: str = "postgres") -> dict:
    """Section 7 subsection: Per-table vacuum and stats health — autovacuum
    effectiveness, dead tuple ratios, and tables needing attention.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Per-table vacuum health with autovacuum effectiveness
    """
    # CHK-007 — Autovacuum Effectiveness & Dead Tuple Health.
    # Reviewed SQL (facts only) lives here in the tool; the size-weighted severity
    # POLICY is read from backend/checks/references.yaml and applied in Python
    # (tool-applies-thresholds), so tuning the ranges needs no code change.
    rows = execute_query(secret_arn, """
        WITH gd AS (
            SELECT current_setting('autovacuum_vacuum_threshold')::bigint AS g_thr,
                   current_setting('autovacuum_vacuum_scale_factor')::float AS g_scale
        ), t AS (
            SELECT s.schemaname, s.relname, s.relid, s.n_live_tup, s.n_dead_tup,
                   s.last_vacuum, s.last_autovacuum, c.reloptions,
                   pg_total_relation_size(s.relid) AS total_bytes,
                   (c.reloptions::text LIKE '%%autovacuum_enabled=false%%') AS av_disabled,
                   COALESCE((SELECT split_part(o,'=',2)::bigint FROM unnest(c.reloptions) o
                             WHERE o LIKE 'autovacuum_vacuum_threshold=%%'), gd.g_thr) AS eff_thr,
                   COALESCE((SELECT split_part(o,'=',2)::float FROM unnest(c.reloptions) o
                             WHERE o LIKE 'autovacuum_vacuum_scale_factor=%%'), gd.g_scale) AS eff_scale
            FROM pg_stat_user_tables s
            JOIN pg_class c ON c.oid = s.relid
            CROSS JOIN gd
            WHERE s.n_dead_tup > 1000
        ), m AS (
            SELECT *, n_dead_tup::float / NULLIF(n_live_tup + n_dead_tup, 0) AS dead_ratio FROM t
        )
        SELECT schemaname, relname, n_live_tup, n_dead_tup,
               ROUND((100.0 * dead_ratio)::numeric, 2) AS dead_pct,
               total_bytes,
               pg_size_pretty(total_bytes) AS table_size,
               pg_size_pretty((total_bytes * dead_ratio)::bigint) AS est_wasted,
               last_vacuum, last_autovacuum,
               CASE WHEN av_disabled THEN 'DISABLED' ELSE 'ENABLED' END AS autovacuum_enabled,
               CASE WHEN n_dead_tup > (eff_thr + eff_scale * n_live_tup) THEN 'OVERDUE' ELSE 'OK' END AS vacuum_status
        FROM m
        WHERE total_bytes > 1048576
        ORDER BY (total_bytes * dead_ratio) DESC
        LIMIT 50
    """, database=database)

    # Tool applies the size-weighted thresholds from the internal references file.
    thresholds = _check_references().get("vacuum-and-bloat", {}).get("thresholds", {})
    for r in rows:
        r["health"] = _severity_from_bands(
            thresholds, r.get("dead_pct"), r.get("total_bytes"),
            hard_critical=(r.get("autovacuum_enabled") == "DISABLED"),
        )
    return {"tables": rows}


@tool
def get_insert_autovacuum_lag(secret_arn: str, database: str = "postgres") -> dict:
    """CHK-007 complementary (PG 13+): insert-only / append tables overdue for
    insert-triggered autovacuum. Freeze can fall behind on append-heavy tables
    even when few dead tuples exist. Returns an empty list on PG < 13.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Tables whose inserts since last vacuum exceed autovacuum_vacuum_insert_threshold
    """
    if _detect_version(secret_arn, database) < 130000:
        return {"tables": [], "note": "Requires PostgreSQL 13+ (insert-triggered autovacuum)."}
    # PG13+ insert-triggered autovacuum formula:
    #   n_ins_since_vacuum > insert_threshold + insert_scale_factor * n_live_tup
    # GATED to low-dead-tuple tables (dead % < 10) so it stays silent on UPDATE/DELETE
    # churn tables — those are already covered by the dead-tuple bands in
    # get_vacuum_stats_health. This prevents double-reporting a bloated table (e.g. one
    # at 50%+ dead) as "insert-heavy overdue" — the genuine target is append-heavy tables
    # with few dead tuples where freeze falls behind.
    rows = execute_query(secret_arn, """
        SELECT schemaname, relname,
               n_ins_since_vacuum, n_live_tup, n_dead_tup,
               ROUND(100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0), 2) AS dead_pct,
               last_autovacuum, last_vacuum,
               (GREATEST(current_setting('autovacuum_vacuum_insert_threshold', true)::bigint, 0)
                + current_setting('autovacuum_vacuum_insert_scale_factor', true)::float
                  * n_live_tup)::bigint AS insert_threshold_effective
        FROM pg_stat_user_tables
        WHERE n_ins_since_vacuum >
              GREATEST(current_setting('autovacuum_vacuum_insert_threshold', true)::bigint, 0)
              + current_setting('autovacuum_vacuum_insert_scale_factor', true)::float * n_live_tup
          AND COALESCE(ROUND(100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0), 2), 0) < 10
        ORDER BY n_ins_since_vacuum DESC
        LIMIT 20
    """, database=database)
    return {
        "tables": rows,
        "note": ("Insert-triggered autovacuum lag (PG13+ formula: n_ins_since_vacuum > "
                 "insert_threshold + insert_scale_factor * n_live_tup). Gated to dead % < 10 "
                 "so churn tables already flagged by the dead-tuple check are excluded."),
    }


# ─── Section 8: OPTIMIZATION ──────────────────────────────────────────────────

@tool
def get_index_statistics(secret_arn: str, database: str = "postgres") -> dict:
    """Section 8: Comprehensive index statistics — index health, usage, size,
    invalid/unused/rarely-used indexes with recommendations.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Index statistics with health assessment and recommendations
    """
    rows = execute_query(secret_arn, """
        WITH index_stats AS (
            SELECT ns.nspname AS schemaname, ct.relname AS tablename,
                   ci.relname AS indexname, am.amname AS index_type,
                   pg_relation_size(i.indexrelid) AS index_size_bytes,
                   COALESCE(s.idx_scan, 0) AS idx_scan,
                   COALESCE(s.idx_tup_read, 0) AS idx_tup_read,
                   COALESCE(s.idx_tup_fetch, 0) AS idx_tup_fetch,
                   i.indisunique, i.indisprimary, i.indisvalid, i.indisready
            FROM pg_index i
            JOIN pg_class ci ON ci.oid = i.indexrelid
            JOIN pg_class ct ON ct.oid = i.indrelid
            JOIN pg_namespace ns ON ns.oid = ct.relnamespace
            JOIN pg_am am ON ci.relam = am.oid
            LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = i.indexrelid
            WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
        )
        SELECT schemaname, tablename, indexname, index_type,
               index_size_bytes,
               pg_size_pretty(index_size_bytes) AS index_size,
               idx_scan AS scans, idx_tup_read AS rows_read,
               indisunique, indisprimary, indisvalid, indisready,
               CASE WHEN indisprimary THEN 'PRIMARY KEY'
                    WHEN indisunique THEN 'UNIQUE'
                    ELSE 'NORMAL' END AS category
        FROM index_stats
        -- Noise net only (mirrors CHK-007's WHERE-guards); the review SEVERITY is applied
        -- in Python from references.yaml. PK/UNIQUE excluded from the unused/rarely nets.
        WHERE NOT indisvalid OR NOT indisready
           OR (idx_scan = 0 AND index_size_bytes > 1048576 AND NOT indisprimary AND NOT indisunique)
           OR (idx_scan < 50 AND index_size_bytes > 10485760 AND NOT indisprimary AND NOT indisunique)
        ORDER BY CASE WHEN NOT indisvalid THEN 1 WHEN idx_scan = 0 THEN 2 ELSE 3 END,
                 index_size_bytes DESC
        LIMIT 50
    """, database=database)
    thr = _check_references().get("index-usage-efficiency", {}).get("thresholds", {})
    for r in rows:
        r["status"] = _severity_index_usage(thr, r)
    return {"indexes": rows}


@tool
def get_toast_status(secret_arn: str, database: str = "postgres") -> dict:
    """Section 8 subsection: TOAST tables status — TOAST storage sizes,
    dead tuple counts, and vacuum health for TOAST tables.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        TOAST table health status
    """
    rows = execute_query(secret_arn, """
        SELECT n.nspname AS schema_name, t.relname AS table_name,
               tt.relname AS toast_table_name,
               pg_size_pretty(pg_total_relation_size(t.oid)) AS main_table_size,
               pg_size_pretty(pg_total_relation_size(tt.oid)) AS toast_size,
               pg_total_relation_size(tt.oid) AS toast_bytes,
               COALESCE(s.n_dead_tup, 0) AS dead_tuples,
               COALESCE(s.n_live_tup, 0) AS live_tuples,
               s.last_vacuum, s.last_autovacuum,
               age(t.relfrozenxid) AS main_table_xid_age,
               age(tt.relfrozenxid) AS toast_xid_age
        FROM pg_class t
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN pg_class tt ON tt.oid = t.reltoastrelid
        LEFT JOIN pg_stat_user_tables s ON s.relid = t.oid
        WHERE t.relkind = 'r' AND t.reltoastrelid != 0
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND pg_total_relation_size(t.oid) > 1048576
        ORDER BY pg_total_relation_size(tt.oid) DESC
        LIMIT 50
    """, database=database)
    # CHK-009 — facts only. A large TOAST is NORMAL for big text/jsonb/bytea columns and is
    # not bloat by itself. Severity = worse of the TOAST xid-age (absolute floors) and the
    # TOAST dead-tuple % (percent bands), both from references.yaml#toast-health.
    thr = _check_references().get("toast-health", {}).get("thresholds", {})
    xid_thr = thr.get("xid", {})
    dead_bands = {"bands": thr.get("dead_pct_bands", [])}
    for r in rows:
        live = r.get("live_tuples") or 0
        dead = r.get("dead_tuples") or 0
        r["toast_dead_pct"] = round(100.0 * dead / (live + dead), 2) if (live + dead) else 0
        s_xid = _severity_from_xid_bands(xid_thr, r.get("toast_xid_age"), None)
        s_dead = _severity_from_percent(dead_bands, r["toast_dead_pct"])
        r["status"] = max((s_xid, s_dead), key=lambda s: _STATE_ORDER.get(s, 0))
    return {"toast_tables": rows}


@tool
def get_partitioning_analysis(secret_arn: str, database: str = "postgres") -> dict:
    """Section 8 subsection: Large table partitioning analysis — identifies tables
    over 1GB that could benefit from partitioning.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Large tables with partitioning recommendations
    """
    rows = execute_query(secret_arn, """
        WITH table_sizes AS (
            SELECT n.nspname AS schema_name, c.relname AS table_name,
                   c.relkind,
                   pg_total_relation_size(c.oid) AS total_size,
                   pg_size_pretty(pg_total_relation_size(c.oid)) AS formatted_size,
                   CASE
                       WHEN c.relkind = 'p' THEN 'PARTITIONED'
                       WHEN c.relispartition THEN 'PARTITION'
                       ELSE 'REGULAR'
                   END AS table_type
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND c.relkind IN ('r', 'p')
              AND pg_total_relation_size(c.oid) > 1073741824
        )
        SELECT schema_name, table_name, formatted_size,
               total_size,
               ROUND(total_size::numeric / 1073741824.0, 2) AS size_gb,
               table_type
        FROM table_sizes
        ORDER BY total_size DESC
        LIMIT 50
    """, database=database)
    # CHK-010 — facts only. Only REGULAR (non-partitioned) tables are partitioning
    # candidates; severity is size-banded from references.yaml#unpartitioned-large-tables.
    bands = _check_references().get("unpartitioned-large-tables", {}).get("thresholds", {})
    for r in rows:
        r["status"] = (_severity_from_percent(bands, r.get("total_size"))
                       if r.get("table_type") == "REGULAR" else "OK")
    return {"large_tables": rows}


@tool
def get_bloat_analysis(secret_arn: str, database: str = "postgres",
                       method: str = "stats") -> dict:
    """Section 8 subsection: Table bloat analysis — estimates dead tuple bloat
    using either stats-based (fast) or pgstattuple (accurate) method.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze
        method: 'stats' for fast estimation, 'pgstattuple' for accurate measurement

    Returns:
        Table bloat analysis with severity classification
    """
    if method == "pgstattuple" and _has_extension(secret_arn, database, "pgstattuple"):
        rows = execute_query(secret_arn, """
            SELECT schemaname, relname AS tablename,
                   pg_size_pretty(pg_total_relation_size(
                       quote_ident(schemaname) || '.' || quote_ident(relname))) AS total_size,
                   (pgstattuple_approx(
                       quote_ident(schemaname) || '.' || quote_ident(relname))).dead_tuple_percent AS bloat_percent,
                   n_dead_tup, n_live_tup,
                   last_vacuum, last_autovacuum,
                   'pgstattuple (accurate)' AS analysis_method
            FROM pg_stat_user_tables
            WHERE n_live_tup + n_dead_tup > 0
              AND pg_total_relation_size(
                  quote_ident(schemaname) || '.' || quote_ident(relname)) > 1048576
            ORDER BY bloat_percent DESC NULLS LAST
            LIMIT 50
        """, database=database)
    else:
        rows = execute_query(secret_arn, """
            SELECT schemaname, relname AS tablename,
                   pg_size_pretty(pg_total_relation_size(
                       quote_ident(schemaname) || '.' || quote_ident(relname))) AS total_size,
                   ROUND(100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0), 2) AS bloat_percent,
                   n_dead_tup, n_live_tup,
                   last_vacuum, last_autovacuum,
                   'stats-based (estimated)' AS analysis_method
            FROM pg_stat_user_tables
            WHERE n_live_tup + n_dead_tup > 0
              AND pg_total_relation_size(
                  quote_ident(schemaname) || '.' || quote_ident(relname)) > 1048576
            ORDER BY bloat_percent DESC NULLS LAST
            LIMIT 50
        """, database=database)

    # CHK-011 — severity from references.yaml#estimated-table-bloat (percent bands over
    # bloat_percent). The estimate finds pg_repack/VACUUM FULL candidates; confirm exact
    # bloat with pgstattuple before remediation (the skill enforces that).
    thr = _check_references().get("estimated-table-bloat", {}).get("thresholds", {})
    for r in rows:
        r["bloat_status"] = _severity_from_percent(thr, r.get("bloat_percent"))
    return {"method": method if method == "pgstattuple" and _has_extension(secret_arn, database, "pgstattuple") else "stats",
            "tables": rows}


@tool
def get_unused_indexes(secret_arn: str, database: str = "postgres") -> dict:
    """Section 8.1: Unused indexes — indexes with zero scans that waste storage
    and slow down writes.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Unused indexes with size and drop recommendations
    """
    # A primary-key or UNIQUE index, and any index backing a constraint, is NEVER a
    # cleanup candidate — dropping it removes a data-integrity guarantee or breaks a
    # cluster's replica identity. We join pg_index (indisprimary / indisunique, the same
    # predicate get_index_statistics uses) and pg_constraint (conindid), and exclude all
    # of them in SQL, so the model is only ever shown droppable indexes.
    rows = execute_query(secret_arn, """
        SELECT s.schemaname, s.relname AS table_name,
               s.indexrelname AS index_name,
               pg_size_pretty(pg_relation_size(s.indexrelid)) AS index_size,
               pg_relation_size(s.indexrelid) AS index_size_bytes,
               s.idx_scan, s.idx_tup_read,
               CASE
                   WHEN pg_relation_size(s.indexrelid) > 104857600
                   THEN 'HIGH PRIORITY - Unused index > 100MB'
                   WHEN pg_relation_size(s.indexrelid) > 10485760
                   THEN 'MEDIUM PRIORITY - Unused index > 10MB'
                   ELSE 'LOW PRIORITY - Small unused index'
               END AS recommendation
        FROM pg_stat_user_indexes s
        JOIN pg_index ix ON ix.indexrelid = s.indexrelid
        WHERE s.idx_scan = 0
          AND NOT ix.indisprimary
          AND NOT ix.indisunique
          AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = s.indexrelid)
          AND s.schemaname NOT IN ('pg_catalog', 'information_schema')
        ORDER BY pg_relation_size(s.indexrelid) DESC
        LIMIT 200
    """, database=database)
    return {"unused_indexes": rows}


@tool
def get_duplicate_indexes(secret_arn: str, database: str = "postgres") -> dict:
    """Section 8.2: Duplicate indexes — indexes on the same columns that waste space.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Duplicate index pairs with drop recommendations
    """
    # CHK-012 — facts only. TRUE duplicates only: identical columns AND opclasses AND
    # collations AND access method AND partial predicate AND expression (IS NOT DISTINCT
    # FROM). Matching on columns alone would produce false positives for partial /
    # expression / different-opclass indexes. Severity + advisory wording are
    # applied from references.yaml / the skill (never a bare "drop it").
    rows = execute_query(secret_arn, """
        WITH index_info AS (
            SELECT n.nspname AS schema_name, ct.relname AS table_name,
                   ci.relname AS index_name, i.indkey::text AS index_columns,
                   i.indclass::text AS opclasses, i.indcollation::text AS collations,
                   ci.relam AS access_method,
                   pg_get_expr(i.indpred, i.indrelid) AS partial_pred,
                   pg_get_expr(i.indexprs, i.indrelid) AS index_expr,
                   pg_size_pretty(pg_relation_size(i.indexrelid)) AS index_size,
                   pg_relation_size(i.indexrelid) AS index_size_bytes,
                   i.indisunique, i.indisprimary
            FROM pg_index i
            JOIN pg_class ci ON ci.oid = i.indexrelid
            JOIN pg_class ct ON ct.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = ct.relnamespace
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
        )
        SELECT a.schema_name, a.table_name,
               a.index_name AS index_1, b.index_name AS index_2,
               a.index_columns AS shared_columns,
               a.index_size AS index_1_size, b.index_size AS index_2_size,
               (a.index_size_bytes + b.index_size_bytes) AS combined_bytes,
               (a.indisprimary OR b.indisprimary) AS involves_primary_key,
               (a.indisunique OR b.indisunique) AS involves_unique
        FROM index_info a
        JOIN index_info b ON a.schema_name = b.schema_name
            AND a.table_name = b.table_name
            AND a.index_columns = b.index_columns
            AND a.opclasses = b.opclasses
            AND a.collations = b.collations
            AND a.access_method = b.access_method
            AND a.partial_pred IS NOT DISTINCT FROM b.partial_pred
            AND a.index_expr IS NOT DISTINCT FROM b.index_expr
            AND a.index_name < b.index_name
        ORDER BY a.schema_name, a.table_name
        LIMIT 200
    """, database=database)
    thr = _check_references().get("duplicate-indexes", {}).get("thresholds", {})
    crit_bytes = thr.get("combined_critical_bytes", 104857600)
    for r in rows:
        r["status"] = "CRITICAL" if (r.get("combined_bytes") or 0) > crit_bytes else "WARNING"
    return {"duplicate_indexes": rows}


@tool
def get_idle_connections(secret_arn: str, database: str = "postgres") -> dict:
    """Section 8.3: Idle connections — long-idle sessions wasting connection slots.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Idle connections with duration and termination recommendations
    """
    rows = execute_query(secret_arn, """
        SELECT pid, usename, datname,
               COALESCE(NULLIF(application_name, ''), '<unnamed>') AS application_name,
               state,
               EXTRACT(EPOCH FROM (now() - state_change))::int AS idle_seconds,
               COALESCE(client_addr::text, 'local') AS client_address
        FROM pg_stat_activity
        WHERE state = 'idle' AND pid != pg_backend_pid()
        ORDER BY state_change ASC NULLS FIRST
        LIMIT 200
    """, database=database)
    # CHK-013 — facts only. Per-connection idle severity from references.yaml#idle-connections
    # (idle seconds bands). A pure 'idle' session (not idle-in-transaction) wastes a slot but
    # does not hold snapshots — the skill weighs that and slot pressure vs max_connections.
    thr = _check_references().get("idle-connections", {}).get("thresholds", {})
    for r in rows:
        r["status"] = _severity_from_percent(thr, r.get("idle_seconds"))
    return {"idle_connections": rows}


@tool
def get_oversized_datatypes(secret_arn: str, database: str = "postgres") -> dict:
    """Section 8.4: Oversized data types — columns using unnecessarily large types.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Columns with oversized data types and optimization suggestions
    """
    # CHK-014 — facts only (columns with potentially oversized/imprecise types + table size
    # so the skill can prioritize large tables). The type-specific guidance (and the caveat
    # that text/varchar are stored identically, and that ALTER TYPE = full table rewrite)
    # lives in the skill, not as SQL text. Advisory: any row = a NOTICE-level review item.
    rows = execute_query(secret_arn, """
        SELECT col.table_schema, col.table_name, col.column_name,
               col.data_type ||
                   CASE WHEN col.character_maximum_length IS NOT NULL
                        THEN '(' || col.character_maximum_length || ')'
                        WHEN col.numeric_precision IS NOT NULL AND col.data_type IN ('numeric', 'decimal')
                        THEN '(' || col.numeric_precision || ',' || COALESCE(col.numeric_scale, 0) || ')'
                        ELSE '' END AS current_type,
               pg_size_pretty(pg_total_relation_size(c.oid)) AS table_size,
               pg_total_relation_size(c.oid) AS table_bytes
        FROM information_schema.columns col
        JOIN pg_namespace n ON n.nspname = col.table_schema
        JOIN pg_class c ON c.relname = col.table_name
             AND c.relnamespace = n.oid AND c.relkind = 'r'
        WHERE col.table_schema NOT IN ('pg_catalog', 'information_schema')
          AND (col.data_type = 'text'
               OR col.data_type = 'bigint'
               OR (col.data_type IN ('numeric', 'decimal') AND col.numeric_precision IS NULL)
               OR (col.data_type = 'character varying' AND col.character_maximum_length IS NULL)
               OR col.data_type = 'double precision')
        ORDER BY pg_total_relation_size(c.oid) DESC, col.table_schema, col.table_name, col.ordinal_position
        LIMIT 200
    """, database=database)
    return {
        "oversized_types": rows,
        "oversized_types_status": _severity_rows_returned(
            _check_references().get("oversized-datatypes", {}).get("thresholds", {}), len(rows)),
    }


@tool
def get_unused_tables(secret_arn: str, database: str = "postgres") -> dict:
    """Section 8.5: Unused tables — tables with zero scans since stats reset.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Unused tables with size and archive recommendations
    """
    # CHK-015 — facts only. Tables with ZERO recorded reads on THIS instance are review
    # candidates (owner-driven lifecycle/archival), never an automated drop. writes_since_reset
    # distinguishes a truly dead table from an append-only one written but read elsewhere.
    # Size-banded review priority is applied in Python from references.yaml#unused-tables.
    rows = execute_query(secret_arn, """
        SELECT schemaname, relname AS table_name,
               pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
               pg_total_relation_size(relid) AS total_bytes,
               n_live_tup AS estimated_rows,
               COALESCE(last_vacuum::text, 'never') AS last_vacuum,
               COALESCE(last_analyze::text, 'never') AS last_analyze,
               seq_scan + COALESCE(idx_scan, 0) AS total_scans,
               (n_tup_ins + n_tup_upd + n_tup_del) AS writes_since_reset
        FROM pg_stat_user_tables
        WHERE seq_scan + COALESCE(idx_scan, 0) = 0
          AND schemaname NOT IN ('pg_catalog', 'information_schema')
        ORDER BY pg_total_relation_size(relid) DESC
        LIMIT 200
    """, database=database)
    thr = _check_references().get("unused-tables", {}).get("thresholds", {})
    for r in rows:
        r["status"] = _severity_from_percent(thr, r.get("total_bytes"))
    return {"unused_tables": rows}


@tool
def get_fk_without_indexes(secret_arn: str, database: str = "postgres") -> dict:
    """Section 8.6: Foreign keys without indexes — FK columns missing indexes,
    causing slow joins and cascading deletes.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Foreign keys missing indexes with CREATE INDEX suggestions
    """
    rows = execute_query(secret_arn, """
        SELECT c.conrelid::regclass AS table_name,
               c.conname AS constraint_name,
               pg_get_constraintdef(c.oid) AS constraint_definition,
               'CREATE INDEX ON ' || c.conrelid::regclass || ' (' ||
                   string_agg(a.attname, ', ' ORDER BY x.n) || ');' AS recommended_index,
               pg_total_relation_size(c.conrelid) AS table_bytes,
               pg_size_pretty(pg_total_relation_size(c.conrelid)) AS table_size
        FROM pg_constraint c
        CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS x(attnum, n)
        JOIN pg_attribute a ON a.attnum = x.attnum AND a.attrelid = c.conrelid
        WHERE c.contype = 'f'
          AND NOT EXISTS (
              SELECT 1 FROM pg_index i
              WHERE i.indrelid = c.conrelid
                AND c.conkey::int[] <@ i.indkey::int[])
        GROUP BY c.conrelid, c.conname, c.oid
        ORDER BY pg_total_relation_size(c.conrelid) DESC
        LIMIT 200
    """, database=database)
    # CHK-016 — facts only; size-banded severity from references.yaml#fk-without-index.
    thr = _check_references().get("fk-without-index", {}).get("thresholds", {})
    for r in rows:
        r["status"] = _severity_from_percent(thr, r.get("table_bytes"))
    return {"fk_without_indexes": rows}


@tool
def get_trigger_analysis(secret_arn: str, database: str = "postgres") -> dict:
    """Section 8.7: Trigger analysis — all user triggers with timing, events,
    and performance impact assessment.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Trigger inventory with performance recommendations
    """
    rows = execute_query(secret_arn, """
        SELECT tgrelid::regclass AS table_name,
               tgname AS trigger_name,
               CASE tgtype & 66
                   WHEN 2 THEN 'BEFORE' WHEN 64 THEN 'INSTEAD OF' ELSE 'AFTER'
               END AS timing,
               CASE
                   WHEN tgtype & 4 > 0 AND tgtype & 8 > 0 AND tgtype & 16 > 0 THEN 'INSERT/UPDATE/DELETE'
                   WHEN tgtype & 4 > 0 AND tgtype & 8 > 0 THEN 'INSERT/UPDATE'
                   WHEN tgtype & 4 > 0 THEN 'INSERT'
                   WHEN tgtype & 8 > 0 THEN 'UPDATE'
                   WHEN tgtype & 16 > 0 THEN 'DELETE'
                   ELSE 'OTHER'
               END AS event,
               CASE WHEN tgtype & 1 > 0 THEN 'FOR EACH ROW' ELSE 'FOR EACH STATEMENT' END AS granularity,
               p.proname AS function_name,
               CASE tgenabled
                   WHEN 'O' THEN 'ENABLED' WHEN 'D' THEN 'DISABLED'
                   WHEN 'R' THEN 'REPLICA' WHEN 'A' THEN 'ALWAYS' ELSE 'ENABLED'
               END AS status
        FROM pg_trigger t
        JOIN pg_proc p ON p.oid = t.tgfoid
        WHERE NOT t.tgisinternal
        ORDER BY tgrelid::regclass::text, tgname
        LIMIT 200
    """, database=database)
    # CHK-017 — inventory (facts); the review verdict (`assessment`) is applied in Python
    # from references.yaml#trigger-inventory. A FOR EACH ROW trigger firing on all DML adds
    # per-row overhead on every write (WARNING); a disabled trigger is a NOTICE. The
    # high-write-table qualification is a skill judgment.
    states = _check_references().get("trigger-inventory", {}).get("thresholds", {}).get("states", {})
    for r in rows:
        active = []
        if r.get("granularity") == "FOR EACH ROW" and r.get("event") == "INSERT/UPDATE/DELETE":
            active.append("for_each_row_all_dml")
        if r.get("status") == "DISABLED":
            active.append("disabled")
        r["assessment"] = _severity_from_states(states, active)
    return {"triggers": rows}


@tool
def get_sequential_scan_tables(secret_arn: str, database: str = "postgres") -> dict:
    """Section 8 subsection: Sequential scan heavy tables — tables dominated by
    sequential scans that likely need indexes.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Tables with high sequential scan ratios
    """
    # CHK-018 — facts only. Surfaces tables > 10MB that see sequential scans; severity is
    # applied in Python from references.yaml#sequential-scan-tables. The signal that an
    # index MAY help is a SELECTIVE scan (avg rows/scan < selective_pct of est. rows, with
    # seq_scan over a floor) -> WARNING pending confirming the query pattern in
    # pg_stat_statements. Full-table reads (reporting/ETL/append-only) are NOTICE, not bad.
    rows = execute_query(secret_arn, """
        SELECT s.schemaname || '.' || s.relname AS table_name,
               pg_total_relation_size(s.relid) AS table_bytes,
               pg_size_pretty(pg_total_relation_size(s.relid)) AS table_size,
               s.seq_scan, COALESCE(s.idx_scan, 0) AS idx_scan, s.seq_tup_read,
               c.reltuples::bigint AS est_rows,
               CASE WHEN s.seq_scan > 0
                    THEN ROUND(s.seq_tup_read::numeric / s.seq_scan, 0)
                    ELSE 0 END AS avg_rows_per_seq_scan
        FROM pg_stat_user_tables s
        JOIN pg_class c ON c.oid = s.relid
        WHERE pg_total_relation_size(s.relid) > 10485760
          AND s.seq_scan > 0
        ORDER BY s.seq_tup_read DESC
        LIMIT 50
    """, database=database)
    thr = _check_references().get("sequential-scan-tables", {}).get("thresholds", {})
    min_seq = thr.get("min_seq_scan", 1000)
    sel_pct = thr.get("selective_pct", 5)
    for r in rows:
        est = r.get("est_rows") or 0
        seq = r.get("seq_scan") or 0
        avg = float(r.get("avg_rows_per_seq_scan") or 0)
        if est > 0 and seq > min_seq and avg < est * (sel_pct / 100.0):
            r["status"] = "WARNING"   # selective scans -> an index may help (confirm the query pattern)
        else:
            r["status"] = "NOTICE"    # full-table reads, may be intentional (reporting / ETL / append-only)
    return {"sequential_scan_tables": rows}


# ─── Section 9: HEALTH SUMMARY ────────────────────────────────────────────────

@tool
def get_health_summary(secret_arn: str, database: str = "postgres") -> dict:
    """Section 9: Health summary — aggregated core health metrics, critical system
    metrics, and overall health score with prioritized findings.

    Args:
        secret_arn: The database alias from [DATABASE SCOPE] (e.g. db-1)
        database: Database name to analyze

    Returns:
        Health summary with severity-ranked findings and overall score
    """
    summary = {}

    # Core health metrics
    rows = execute_query(secret_arn, """
        WITH health AS (
            SELECT ROUND(100.0 * count(*) / current_setting('max_connections')::int, 1) AS conn_pct,
                   count(*) AS current_conn,
                   current_setting('max_connections')::int AS max_conn
            FROM pg_stat_activity WHERE pid != pg_backend_pid()
        ),
        xid AS (
            -- The WORST database on the instance, not just the connected one: wraparound
            -- pressure accumulates on a rarely-vacuumed database (often `postgres`), so a
            -- per-database view hides the real risk. MAX(age) over every datallowconn
            -- database, matching Tier 1 and the deep scan.
            SELECT ROUND(100.0 * MAX(age(datfrozenxid)) /
                   current_setting('autovacuum_freeze_max_age')::numeric, 1) AS xid_pct,
                   MAX(age(datfrozenxid)) AS xid_age,
                   current_setting('autovacuum_freeze_max_age')::bigint AS freeze_max_age
            FROM pg_database WHERE datallowconn
        ),
        cache AS (
            SELECT CASE WHEN blks_hit + blks_read = 0 THEN 100.0
                        ELSE ROUND(100.0 * blks_hit / (blks_hit + blks_read), 1)
                   END AS hit_ratio
            FROM pg_stat_database WHERE datname = current_database()
        ),
        queries AS (
            SELECT count(*) FILTER (WHERE state = 'active' AND query_start IS NOT NULL
                   AND EXTRACT(EPOCH FROM (now() - query_start)) > 300) AS long_queries,
                   -- Only ACTIVE backends on a non-Activity wait event are waiting
                   -- queries; an idle client on ClientRead is not contention.
                   count(*) FILTER (WHERE state = 'active' AND wait_event_type IS NOT NULL
                   AND wait_event_type != 'Activity') AS waiting
            FROM pg_stat_activity WHERE pid != pg_backend_pid()
        ),
        vacuum AS (
            SELECT count(*) FILTER (WHERE pg_total_relation_size(relid) > 1048576
                   AND n_live_tup + n_dead_tup > 0
                   AND n_dead_tup::numeric / (n_live_tup + n_dead_tup) > 0.2) AS needs_vacuum,
                   count(*) AS total_tables
            FROM pg_stat_user_tables
        ),
        locks AS (
            SELECT count(*) FILTER (WHERE NOT granted) AS blocked
            FROM pg_locks
        )
        SELECT
            h.conn_pct, h.current_conn, h.max_conn,
            x.xid_pct, x.xid_age, x.freeze_max_age,
            c.hit_ratio AS cache_hit_ratio,
            q.long_queries, q.waiting AS waiting_queries,
            v.needs_vacuum, v.total_tables,
            l.blocked AS blocked_queries
        FROM health h, xid x, cache c, queries q, vacuum v, locks l
    """, database=database)
    if rows:
        m = rows[0]
        summary["metrics"] = m

        # Derive overall score
        findings = []

        # XID wraparound (CHK-001). Severity comes from references.yaml#xid-wraparound-database
        # (relative-to-autovacuum_freeze_max_age tiers + absolute 2B-budget floors) via the shared
        # classifier — the same policy the maintenance and transaction tools apply. xid_pct is the
        # age as a percentage of autovacuum_freeze_max_age (routine autovacuum territory), not of
        # the wraparound budget, so it is reported for context but does not set the severity.
        xid_thr = _check_references().get("xid-wraparound-database", {}).get("thresholds", {})
        xid_sev = _severity_from_xid_bands(xid_thr, m.get("xid_age"), m.get("freeze_max_age"))
        if xid_sev in ("WARNING", "CRITICAL", "EMERGENCY"):
            _xid_action = {"EMERGENCY": "immediate VACUUM FREEZE required",
                           "CRITICAL": "schedule VACUUM FREEZE",
                           "WARNING": "monitor freeze age"}[xid_sev]
            findings.append({"severity": xid_sev, "area": "XID",
                             "detail": f"XID age at {m.get('xid_pct')}% of autovacuum_freeze_max_age — {_xid_action}"})

        if m.get("conn_pct") and m["conn_pct"] > 85:
            findings.append({"severity": "CRITICAL", "area": "Connections",
                             "detail": f"Connection usage at {m['conn_pct']}% — implement connection pooling"})
        elif m.get("conn_pct") and m["conn_pct"] > 70:
            findings.append({"severity": "WARNING", "area": "Connections",
                             "detail": f"Connection usage at {m['conn_pct']}%"})

        if m.get("cache_hit_ratio") is not None and m["cache_hit_ratio"] < 85:
            findings.append({"severity": "WARNING", "area": "Cache",
                             "detail": f"Cache hit ratio at {m['cache_hit_ratio']}% — increase shared_buffers"})

        if m.get("long_queries") and m["long_queries"] > 0:
            findings.append({"severity": "WARNING", "area": "Queries",
                             "detail": f"{m['long_queries']} queries running > 5 minutes"})

        if m.get("blocked_queries") and m["blocked_queries"] > 5:
            findings.append({"severity": "CRITICAL", "area": "Locks",
                             "detail": f"{m['blocked_queries']} blocked queries — severe lock contention"})

        if m.get("needs_vacuum") and m.get("total_tables") and m["total_tables"] > 0:
            vac_pct = 100.0 * m["needs_vacuum"] / m["total_tables"]
            if vac_pct > 50:
                findings.append({"severity": "CRITICAL", "area": "Vacuum",
                                 "detail": f"{m['needs_vacuum']}/{m['total_tables']} tables need vacuum"})
            elif m["needs_vacuum"] > 0:
                findings.append({"severity": "WARNING", "area": "Vacuum",
                                 "detail": f"{m['needs_vacuum']} tables with >20% dead tuples"})

        summary["findings"] = findings

        # Overall score
        severities = [f["severity"] for f in findings]
        if "EMERGENCY" in severities:
            summary["overall_score"] = "critical"
        elif "CRITICAL" in severities:
            summary["overall_score"] = "critical"
        elif "WARNING" in severities:
            summary["overall_score"] = "warning"
        else:
            summary["overall_score"] = "ok"

    return summary


# Do not add a general-purpose "run any SQL" tool here.
#
# A tool that executes whatever SQL the model produces re-opens several distinct escapes at
# once: multi-statement escalation (`SELECT 1; SET ROLE ...`), which changes session state
# even under readonly=True; Unicode-escape identifiers, which resolve to a blocked catalog
# only at parse time and so slip past a literal blocklist; and the *_to_xml family, which
# runs its argument server-side and can dump whole tables or schemas.
#
# Model-supplied SQL reaches the database through exactly one path, get_query_plan, and that
# path goes through _reject_unsafe_read_sql. The dbops_ro least-privilege role is the
# backstop underneath. A general query tool would defeat both.


VCPU_MAP = {
    "db.t3.micro": 2, "db.t3.small": 2, "db.t3.medium": 2, "db.t3.large": 2,
    "db.t4g.micro": 2, "db.t4g.small": 2, "db.t4g.medium": 2, "db.t4g.large": 2,
    "db.r5.large": 2, "db.r5.xlarge": 4, "db.r5.2xlarge": 8, "db.r5.4xlarge": 16,
    "db.r5.8xlarge": 32, "db.r5.12xlarge": 48, "db.r5.16xlarge": 64,
    "db.r6g.large": 2, "db.r6g.xlarge": 4, "db.r6g.2xlarge": 8, "db.r6g.4xlarge": 16,
    "db.r6g.8xlarge": 32, "db.r6g.12xlarge": 48, "db.r6g.16xlarge": 64,
    "db.r6i.large": 2, "db.r6i.xlarge": 4, "db.r6i.2xlarge": 8, "db.r6i.4xlarge": 16,
    "db.r6i.8xlarge": 32, "db.r6i.12xlarge": 48, "db.r6i.16xlarge": 64,
    "db.r7g.large": 2, "db.r7g.xlarge": 4, "db.r7g.2xlarge": 8, "db.r7g.4xlarge": 16,
    "db.r7g.8xlarge": 32, "db.r7g.12xlarge": 48, "db.r7g.16xlarge": 64,
}


@tool
def get_instance_details(identifier: str, secret_arn: str = None) -> dict:
    """Get infrastructure details for a database instance from the RDS API.
    Returns instance class, vCPU count, memory, storage, engine version, and
    cluster topology. Call this FIRST to establish capacity baseline before
    analyzing performance metrics (AAS vs vCPU ratio).

    Args:
        identifier: The database alias from [DATABASE SCOPE] (db_instance_id, e.g. db-1)
        secret_arn: Optional credential handle (the same database alias)

    Returns:
        Instance infrastructure details including instance_class and vcpu_count
    """
    try:
        from tools.aws.session_factory import get_session, resolve_target

        # The session is already in the database's own account AND Region (from the
        # registry record); overriding its Region would describe a cross-Region database
        # in the deployment's Region and find nothing.
        session = get_session(identifier=identifier, secret_arn=secret_arn)
        target = resolve_target(identifier)
        rds_id = target["rds_id"]
        rds = session.client("rds")

        result = {
            "display_name": (target["record"] or {}).get("display_name") or identifier,
        }

        # Try as Aurora cluster first
        try:
            resp = rds.describe_db_clusters(DBClusterIdentifier=rds_id)
            cluster = resp["DBClusters"][0]
            result["cluster_identifier"] = cluster.get("DBClusterIdentifier", "")
            result["engine_version"] = cluster.get("EngineVersion", "")
            result["storage_encrypted"] = cluster.get("StorageEncrypted", False)
            result["multi_az"] = cluster.get("MultiAZ", False)
            result["deletion_protection"] = cluster.get("DeletionProtection", False)
            result["cluster_members"] = []

            for member in cluster.get("DBClusterMembers", []):
                member_id = member.get("DBInstanceIdentifier", "")
                is_writer = member.get("IsClusterWriter", False)
                try:
                    resp2 = rds.describe_db_instances(DBInstanceIdentifier=member_id)
                    db_inst = resp2["DBInstances"][0]
                    instance_class = db_inst.get("DBInstanceClass", "")
                    vcpus = VCPU_MAP.get(instance_class, 0)
                    member_info = {
                        "instance_identifier": member_id,
                        "instance_class": instance_class,
                        "vcpu_count": vcpus if vcpus else f"unknown for {instance_class}",
                        "is_writer": is_writer,
                        "availability_zone": db_inst.get("AvailabilityZone", ""),
                        "instance_status": db_inst.get("DBInstanceStatus", ""),
                    }
                    result["cluster_members"].append(member_info)
                    if is_writer:
                        result["writer_instance_class"] = instance_class
                        result["writer_vcpu_count"] = vcpus if vcpus else f"unknown for {instance_class}"
                except Exception:
                    result["cluster_members"].append({"instance_identifier": member_id, "is_writer": is_writer})

            return result
        except rds.exceptions.DBClusterNotFoundFault:
            pass

        # Try as standalone RDS instance
        try:
            resp = rds.describe_db_instances(DBInstanceIdentifier=rds_id)
            db_inst = resp["DBInstances"][0]
            instance_class = db_inst.get("DBInstanceClass", "")
            vcpus = VCPU_MAP.get(instance_class, 0)
            result["instance_identifier"] = db_inst.get("DBInstanceIdentifier", "")
            result["instance_class"] = instance_class
            result["vcpu_count"] = vcpus if vcpus else f"unknown for {instance_class}"
            result["engine_version"] = db_inst.get("EngineVersion", "")
            result["storage_encrypted"] = db_inst.get("StorageEncrypted", False)
            result["multi_az"] = db_inst.get("MultiAZ", False)
            result["availability_zone"] = db_inst.get("AvailabilityZone", "")
            result["allocated_storage_gb"] = db_inst.get("AllocatedStorage", 0)
            result["instance_status"] = db_inst.get("DBInstanceStatus", "")
            return result
        except Exception as e:
            return {"error": f"RDS API lookup failed for '{identifier}': {e}"}

    except Exception as e:
        logger.error(f"get_instance_details failed: {e}")
        return {"error": str(e)}


@tool
def list_monitored_databases() -> dict:
    """List all database instances registered in the monitoring system.
    Fast operation — reads from DynamoDB, no database connections needed.
    Only returns configured/monitored instances, not discovered ones without credentials.

    Returns:
        List of monitored database instances with their configuration
    """
    try:
        from tools.aws.dynamodb_client import db_registry
        instances = db_registry.list_instances()
        # Filter out discovered instances that haven't been configured yet
        monitored = [i for i in instances if i.get("status") != "discovered"]

        # Space-scope the results to the caller's space.
        # /invocations publishes the caller's VALIDATED space on this ContextVar. We
        # filter to it when set; when it is EMPTY we FAIL CLOSED and return nothing — an
        # unscoped context must never enumerate every tenant's databases. (An omitted or
        # unresolvable space on /invocations therefore yields an empty list, not a
        # cross-tenant dump.)
        # Membership-based, like every other tenant read in this module (the legacy
        # `tags.space_id` stamp is not maintained, so filtering on it under-shared a
        # database with its other member spaces).
        monitored = _scope_instances_to_space(monitored)

        # The model gets the OPERATOR shape of each record (utils.db_projection, an
        # allowlist): identity, placement and display state. No credential pointer (the
        # credential handle comes from the request's [DATABASE SCOPE] block), no space
        # ids of any kind, no network internals. Space scoping already happened above.
        from utils.db_projection import project_db_for_operator
        from tools.aws.db_aliases import alias_for
        sanitized = []
        for i in monitored:
            rec = project_db_for_operator(i, set())
            rec.pop("member_space_ids", None)
            rec.pop("granted", None)
            # A database in this request's scope is named by its alias, as in the block.
            rec["instance_id"] = alias_for(rec.get("instance_id"))
            sanitized.append(rec)

        return {
            "databases": sanitized,
            "count": len(sanitized),
        }
    except Exception as e:
        logger.error(f"Failed to list databases: {e}")
        return {"error": str(e), "databases": [], "count": 0}


# ── Cluster members: which member the Trends / Findings tools read ─────────────

def _focus_member_selector(member: str = "") -> str:
    """The member selector a tool reads: an explicit ``member`` argument, else the
    conversation's role focus ('' | writer | reader | instance:<id>). Validated by
    ``scan_records.parse_member``; raises ValueError for anything else."""
    from utils import scan_records
    if member:
        return scan_records.parse_member(member)
    try:
        from model_resolver import CURRENT_ROLE_FOCUS
        focus = CURRENT_ROLE_FOCUS.get() or ""
    except Exception:  # noqa: BLE001 - no request context
        focus = ""
    return scan_records.parse_member(focus)


_SEVERITY_RANK = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}


def _top_findings(findings, n=5):
    rows = [f for f in (findings or []) if isinstance(f, dict)]
    return sorted(rows, key=lambda f: _SEVERITY_RANK.get(str(f.get("severity", "")).upper(), 3))[:n]


def _trend_lines(metrics) -> list:
    """Oldest-to-newest movement of the headline Tier 1 metrics over newest-first rows."""
    latest, oldest = metrics[0], metrics[-1]
    out = []
    for key, label, unit, direction in [
        ("cache_hit_ratio", "Cache hit ratio", "%", "drop"),
        ("dead_tuple_pct", "Dead tuple %", "%", "rise"),
        ("xid_age_pct", "XID age", "%", "rise"),
        ("total_connections", "Connections", "", "rise"),
        ("blocked_queries", "Blocked queries", "", "rise"),
        ("database_size_mb", "DB size", "MB", "rise"),
        ("replica_lag_ms", "Replica lag", "ms", "rise"),
    ]:
        curr = latest.get(key)
        prev = oldest.get(key)
        if curr is not None and prev is not None:
            try:
                curr_f, prev_f = float(curr), float(prev)
                diff = curr_f - prev_f
                if abs(diff) > 0.01:
                    arrow = "↑" if diff > 0 else "↓"
                    concern = (direction == "rise" and diff > 0) or (direction == "drop" and diff < 0)
                    out.append(
                        f"{label}: {prev_f:.1f}{unit} → {curr_f:.1f}{unit} ({arrow}{abs(diff):.1f})"
                        + (" ⚠️" if concern else "")
                    )
            except (ValueError, TypeError):
                pass
    return out


@tool
def get_report_analysis(instance_id: Optional[str] = None, member: str = "",
                        sections: str = "", compare_to: str = "") -> dict:
    """Get the latest deep-scan analysis for monitored databases, from the metrics store
    (falls back to the S3 analysis document for scans that predate the store).
    Use this for questions like 'show me the latest report', 'what are the findings',
    'what did the deep scan find about indexes / config / roles / queries', or 'did any
    parameter change since the last scan'.

    Every deep scan is stored as a summary (health, findings, headline metrics) plus
    nine SECTIONS of structured data: overview (version, extensions, workload profile),
    configuration (tuned parameters and those outside recommendation), security (roles,
    PUBLIC grants, SSL, RLS), activity (connections by application and client, pooler
    detection, long-running and blocking queries), replication (standbys, slots, WAL),
    footprint (sizes, largest tables and indexes, WAL directory), performance
    (checkpoints, I/O, top queries by time / WAL / I/O time / calls, temp-file queries,
    autovacuum workers), maintenance (vacuum and statistics health, sequences, invalid
    indexes, unvalidated constraints, matviews, bloat) and optimization (unused and
    duplicate indexes, FKs without an index, seq-scan-heavy tables, unused tables,
    triggers, partitioning, TOAST). Ask for the sections you need; each adds context.

    For a DB cluster the writer gets the full deep scan and each reader a focused
    reader scan (replica lag, recovery conflicts, long queries). With the writer (or
    no focus) this returns the writer's deep scan plus ``reader_scans``, the latest
    reader scan of each reader with its top findings; with a reader or instance focus
    it returns that member's reader scan(s) plus the writer's health for context.

    Args:
        instance_id: Optional database alias from [DATABASE SCOPE] (e.g. db-1) to
            filter reports
        member: Optional member selector: 'writer', 'reader' (every reader) or
            'instance:<id>'. Empty follows the conversation's focus.
        sections: Comma-separated section names to include (e.g.
            'configuration,security'), or 'all'. Empty returns the summary only.
        compare_to: Optional: 'previous' (the full scan before the latest) or an ISO
            date (the newest full scan on or before it). Adds ``changes``: what moved
            in the headline metrics, findings, configuration settings, roles and
            extensions between that scan and the latest one.

    Returns:
        Latest report analysis with findings and recommendations, ``sections`` when
        requested, ``changes`` when compared, and member_selector
    """
    try:
        selector = _focus_member_selector(member)
    except ValueError as e:
        return {"error": str(e)}
    try:
        from tools.aws.dynamodb_client import db_registry
        from tools.aws.db_aliases import resolve_handle, scoped_ids
        from tools.aws import metrics_store
        instance_id = resolve_handle(instance_id)

        if instance_id:
            inst = db_registry.get_instance(instance_id)
            if not inst:
                # The caller may have passed a display_name instead of the UUID
                # instance_id (e.g. from a free-form question) — resolve it so
                # report lookups still succeed.
                inst = next(
                    (i for i in db_registry.list_instances()
                     if i.get("display_name") == instance_id),
                    None,
                )
            instances = [inst] if inst else []
        else:
            # No explicit database: honor the turn's SELECTION. When the user picked a
            # subset (e.g. 2 of 10), only those are in the request's alias map, so report
            # on exactly those — otherwise "the latest report" covered every database in
            # the space and the unpicked ones showed as raw ids (no alias). With no
            # selection (or outside an agent request) scoped_ids() is None and we list the
            # whole space, as before.
            picked = scoped_ids()
            if picked:
                instances = [db_registry.get_instance(i) for i in picked]
                instances = [i for i in instances if i]
            else:
                instances = db_registry.list_instances()

        # Tenant isolation: restrict to the caller's space, fail-closed
        # when unscoped — a forged/absent space must not surface another tenant's reports.
        instances = _scope_instances_to_space(instances)

        if not instances:
            return {"error": "No monitored databases found"}

        wanted = _section_list(sections)
        reports = []

        if metrics_store.is_configured():
            for inst in instances:
                iid = inst["instance_id"]
                writer_id = next((m.get("instance_id") for m in inst.get("cluster_member_roles") or []
                                  if m.get("is_writer")), "")
                member_view = selector == "reader" or (
                    selector.startswith("instance:") and selector[len("instance:"):] != writer_id)
                try:
                    wrow = metrics_store.tier2_latest_full(iid)
                    if member_view:
                        # A reader or one member: its reader scan(s), with the writer's health.
                        only = selector[len("instance:"):] if selector.startswith("instance:") else None
                        rows = metrics_store.tier2_latest_readers(iid, 30, member=only)
                        for row in rows:
                            rep = {
                                "instance_id": iid,
                                "display_name": inst.get("display_name", iid),
                                "member_instance_id": row.get("member_instance_id"),
                                "member_role": row.get("member_role"),
                                "scan_kind": "reader",
                                "report_date": row.get("scan_time"),
                                "writer_health_score": (wrow or {}).get("health_score"),
                                "analysis": {"health_score": row.get("health_score"),
                                             "summary": row.get("summary"),
                                             "findings": row.get("findings", [])},
                            }
                            if wanted:
                                rep["sections"] = _redacted_sections(metrics_store.tier2_sections(
                                    iid, row["scan_time"], row.get("member_instance_id") or iid, ["reader"]))
                            reports.append(rep)
                        if not rows:
                            reports.append({"instance_id": iid, "display_name": inst.get("display_name", iid),
                                            "scan_kind": "reader",
                                            "writer_health_score": (wrow or {}).get("health_score"),
                                            "analysis": None,
                                            "note": "No reader scan found for this member yet."})
                        continue
                    row = wrow
                    reader_rows = (metrics_store.tier2_latest_readers(iid, 30)
                                   if inst.get("cluster_member_roles") else [])
                    if row:
                        rep = {
                            "instance_id": iid,
                            "display_name": inst.get("display_name", iid),
                            "report_date": row.get("scan_time"),
                            "report_key": row.get("report_s3_key", ""),
                            "analysis": _analysis_from_summary(row),
                            "member_instance_id": row.get("member_instance_id"),
                            "scan_kind": "full",
                            "sections_available": row.get("sections") or [],
                            "reader_scans": [{"member_instance_id": r.get("member_instance_id"),
                                              "health_score": r.get("health_score"),
                                              "scan_time": r.get("scan_time"),
                                              "findings": _top_findings(r.get("findings"))}
                                             for r in reader_rows],
                        }
                        if wanted:
                            rep["sections"] = _redacted_sections(metrics_store.tier2_sections(
                                iid, row["scan_time"], row.get("member_instance_id") or iid, wanted))
                        if compare_to:
                            rep["changes"] = _scan_changes(iid, row, compare_to)
                        reports.append(rep)
                except Exception as e:
                    logger.warning(f"Metrics-store read failed for {iid}: {e}")

        # Fall back to S3 for any instances not found in the store
        if len(reports) < len(instances):
            from tools.aws.s3_client import report_storage
            found_ids = {r["instance_id"] for r in reports}
            for inst in instances:
                iid = inst["instance_id"]
                if iid in found_ids:
                    continue
                aid = inst.get("account_id", "unknown")
                try:
                    report_list = report_storage.list_reports(aid, iid)
                    if report_list:
                        latest = report_list[0]
                        report = report_storage.get_report(latest["key"])
                        reports.append({
                            "instance_id": iid,
                            "display_name": inst.get("display_name", iid),
                            "report_key": latest["key"],
                            "report_date": latest.get("date"),
                            "report_type": latest.get("type"),
                            "analysis": report,
                        })
                except Exception as e:
                    logger.warning(f"Failed to get report for {iid}: {e}")

        from tools.aws.db_aliases import alias_for
        for r in reports:
            r["instance_id"] = alias_for(r.get("instance_id"))
        return {"reports": reports, "count": len(reports), "member_selector": selector}
    except Exception as e:
        return {"error": str(e)}


def _section_list(sections: str) -> list:
    from tools.aws import metrics_store
    s = (sections or "").strip().lower()
    if not s:
        return []
    if s == "all":
        return [n for n in metrics_store.TIER2_SECTIONS if n != "reader"]
    return [n.strip() for n in s.split(",") if n.strip() in metrics_store.TIER2_SECTIONS]


def _analysis_from_summary(row: dict) -> dict:
    findings = row.get("findings") or []
    return {
        "health_score": row.get("health_score"),
        "summary": row.get("summary"),
        "findings": findings,
        "metrics": {
            "table_cache_hit_ratio": row.get("cache_hit_ratio"),
            "dead_tuple_pct": row.get("dead_tuple_pct"),
            "xid_age_pct": row.get("xid_age_pct"),
            "total_connections": row.get("total_connections"),
            "database_size_mb": row.get("database_size_mb"),
            "unused_indexes_count": row.get("unused_indexes_count"),
            "index_cache_hit_ratio": row.get("index_cache_hit_ratio"),
            "deadlocks": row.get("deadlocks"),
        },
        "recommendations": [f["resolution"] for f in findings if isinstance(f, dict) and f.get("resolution")],
    }


# Section data holds the monitored database's own identifiers (client addresses, user
# names), kept per product rule 19: they are the customer's diagnostic data. Query text
# was literal-masked at write time (deep_sections / run_scan). What must NOT reach the
# model is anything about THIS deployment, and no section holds any, so nothing is
# dropped today; a key listed here would be removed from every section the tool returns.
_SECTION_DROP_KEYS: tuple = ()


def _redacted_sections(sections: dict) -> dict:
    out = {}
    for name, data in (sections or {}).items():
        if isinstance(data, dict):
            out[name] = {k: v for k, v in data.items() if k not in _SECTION_DROP_KEYS}
        else:
            out[name] = data
    return out


def _scan_changes(instance_id: str, latest: dict, compare_to: str) -> dict:
    """What moved between an earlier full scan and ``latest``: headline metrics,
    findings (new / resolved by title), configuration settings, login roles and
    extensions. ``compare_to``: 'previous' or an ISO date."""
    from tools.aws import metrics_store
    ref = (compare_to or "").strip().lower()
    base = None
    if ref == "previous":
        rows = metrics_store.tier2_list([instance_id], 3650, 2, kind="full")
        base = rows[1] if len(rows) > 1 else None
    else:
        try:
            # A bare date means "on or before that day": through its last millisecond.
            upto = metrics_store.canon_ts(ref + "T23:59:59.999+00:00" if len(ref) == 10 else ref)
        except ValueError:
            return {"error": "compare_to must be 'previous' or an ISO date"}
        rows = metrics_store.tier2_list([instance_id], 3650, 1, before=f"{upto}|~", kind="full")
        base = rows[0] if rows else None
    if not base or base.get("scan_time") == latest.get("scan_time"):
        return {"note": "No earlier full scan to compare against."}
    changes = {"from": base.get("scan_time"), "to": latest.get("scan_time"),
               "health_score": [base.get("health_score"), latest.get("health_score")]}
    metric_moves = {}
    for k in ("cache_hit_ratio", "dead_tuple_pct", "xid_age_pct", "total_connections",
              "database_size_mb", "unused_indexes_count", "index_cache_hit_ratio", "deadlocks"):
        a, b = base.get(k), latest.get(k)
        if a is not None and b is not None and a != b:
            metric_moves[k] = {"from": a, "to": b}
    changes["metrics"] = metric_moves
    bt = {f.get("title") for f in base.get("findings") or [] if isinstance(f, dict)}
    lt = {f.get("title") for f in latest.get("findings") or [] if isinstance(f, dict)}
    changes["findings"] = {"new": sorted(lt - bt), "resolved": sorted(bt - lt)}
    want = ["configuration", "security", "overview"]
    s0 = metrics_store.tier2_sections(instance_id, base["scan_time"], base.get("member_instance_id") or instance_id, want)
    s1 = metrics_store.tier2_sections(instance_id, latest["scan_time"], latest.get("member_instance_id") or instance_id, want)

    def _settings(sec):
        return {r.get("name"): r.get("setting") for r in ((sec or {}).get("settings") or []) if isinstance(r, dict)}
    c0, c1 = _settings(s0.get("configuration")), _settings(s1.get("configuration"))
    changes["configuration"] = {n: {"from": c0.get(n), "to": c1.get(n)}
                                for n in sorted(set(c0) | set(c1)) if c0.get(n) != c1.get(n)}

    def _roles(sec):
        return {r.get("name") for r in ((sec or {}).get("roles") or []) if isinstance(r, dict) and r.get("can_login")}
    r0, r1 = _roles(s0.get("security")), _roles(s1.get("security"))
    changes["login_roles"] = {"added": sorted(r1 - r0), "removed": sorted(r0 - r1)}

    def _ext(sec):
        return {e.get("name"): e.get("version") for e in ((sec or {}).get("extensions") or []) if isinstance(e, dict)}
    e0, e1 = _ext(s0.get("overview")), _ext(s1.get("overview"))
    changes["extensions"] = {n: {"from": e0.get(n), "to": e1.get(n)}
                             for n in sorted(set(e0) | set(e1)) if e0.get(n) != e1.get(n)}
    return changes


@tool
def get_historical_metrics(instance_id: str, hours: int = 6, member: str = "") -> dict:
    """Get PostgreSQL-internal metrics history from the Tier 1 metrics store. These are
    metrics CloudWatch's RDS namespace does NOT have: cache hit ratio trends, dead tuple
    growth, XID age progression, connection state breakdown, lock contention, deadlocks
    per interval, transaction rate and rollback ratio, temp files, autovacuum activity,
    and per-table bloat and wait events.

    Collected every 5 minutes. Up to 7 days (168 h) the raw samples are returned with
    the bloat and wait-event lists ("what happened at 02:00 last night"); for longer
    windows, up to 15 months (10800 h), the series is averaged into buckets from the
    long-term store ("is dead-tuple % creeping over the quarter"). Use this to
    understand what changed — e.g. 'cache hit ratio dropped from 99% to 87% over the
    last 2 hours' or 'connections have grown 30% this quarter'.

    For a DB cluster each member (the writer and every reader) has its own rows,
    tagged with member_instance_id and member_role, plus replica_lag_ms on readers.

    Args:
        instance_id: The database alias from [DATABASE SCOPE] (e.g. db-1)
        hours: How many hours of history to fetch (default: 6, max: 10800)
        member: Optional member selector: 'writer', 'reader' (every reader) or
            'instance:<id>'. Empty follows the conversation's focus (the writer when
            there is none).

    Returns:
        Recent metrics with trend summary (per member under by_member when the rows
        span more than one), the ``source`` read (samples or series), and
        member_selector: what was read
    """
    return _historical_metrics(instance_id, hours, member)


_DETAIL_WINDOW_HOURS = 168          # raw rows (DynamoDB) up to a week
_SERIES_WINDOW_HOURS = 24 * 450     # CloudWatch keeps hourly points for 15 months


def _historical_metrics(instance_id: str, hours: int = 6, member: str = "",
                        for_growth: bool = False) -> dict:
    """Implementation of get_historical_metrics. ``for_growth`` additionally returns a
    two-point ``growth_series`` (newest, oldest-in-window) plus the actual observed
    ``window`` span, so a growth verdict covers the whole window rather than the most
    recent display page."""
    try:
        selector = _focus_member_selector(member)
    except ValueError as e:
        return {"error": str(e), "metrics": []}
    try:
        from tools.aws import metrics_store
        from utils import scan_records
        if not metrics_store.is_configured():
            return {"error": "The metrics store is not configured", "metrics": []}

        # The scope's alias (db-1) is this request's name for a registry id.
        from tools.aws.db_aliases import alias_for, resolve_handle
        alias = instance_id
        instance_id = resolve_handle(instance_id)
        # Resolve display_name to actual instance_id if needed
        resolved_id = instance_id
        _inst = None
        try:
            from tools.aws.dynamodb_client import db_registry
            # Try direct lookup first
            _inst = db_registry.get_instance(instance_id)
            if not _inst:
                # Search by display_name
                for db in db_registry.list_instances():
                    if db.get("display_name", "").lower() == instance_id.lower():
                        resolved_id = db["instance_id"]
                        _inst = db
                        break
        except Exception:
            pass

        # Tenant isolation: the resolved instance must belong to the
        # caller's space — refuse otherwise (fail-closed, opaque). Prevents reading another
        # tenant's historical metrics by supplying their instance_id/display_name.
        if not _scope_instances_to_space([_inst] if _inst else []):
            return {"error": "No metrics found for the requested database", "metrics": []}

        hours = min(max(int(hours or 6), 1), _SERIES_WINDOW_HOURS)
        record = _inst or {"instance_id": resolved_id}
        is_cluster = bool(record.get("cluster_member_roles"))
        members = scan_records.member_ids(record)

        if hours <= _DETAIL_WINDOW_HOURS:
            metrics = metrics_store.tier1_history(resolved_id, members, hours, 100,
                                                  selector=selector, is_cluster=is_cluster)
            source = "samples"
        else:
            # Beyond the detail window: the long-term series, one bucket per point, for
            # the writer (or the selected member / role). The lists are not in it.
            s = selector
            member_id = s[len("instance:"):] if s.startswith("instance:") else None
            role = None if member_id else (s if s in ("writer", "reader") else "writer")
            if not is_cluster and not member_id:
                member_id, role = (members[0] if members else resolved_id), None
            metrics = list(reversed(metrics_store.series_points(
                resolved_id, member_id, role, hours, scan_records.BUCKET_METRICS, max_points=60)))
            for m in metrics:
                # The series is the member's (an RDS instance identifier) or the role's
                # (no single member); never the registry id.
                m.setdefault("member_instance_id", member_id)
                m.setdefault("member_role", role or "writer")
            source = "series"
        # Store rows carry the registry id and their item key; the model gets the alias
        # (above) and the member, never the registry id.
        for m in metrics:
            m.pop("instance_id", None)
            m.pop("id", None)

        if not metrics:
            return {"instance_id": alias_for(resolved_id), "display_name": alias, "hours": hours,
                    "member_selector": selector, "source": source,
                    "metrics": [], "summary": "No Tier 1 metrics found for this instance."}

        latest = metrics[0]
        oldest = metrics[-1]
        if for_growth:
            if source == "samples":
                # The display read returns the newest 100 rows, which for a window longer
                # than ~8 h (at 5-min cadence) does NOT reach back to the window's start.
                # For a growth verdict fetch the OLDEST row still inside the window.
                window_oldest_row = metrics_store.tier1_oldest_in_window(
                    resolved_id, members, hours, selector=selector, is_cluster=is_cluster) or oldest
            else:
                window_oldest_row = oldest
            # Newest first, oldest-in-window last — the shape compute_growth expects.
            return {
                "instance_id": alias_for(resolved_id), "display_name": alias,
                "member_selector": selector, "hours": hours, "source": source,
                "growth_series": [latest, window_oldest_row],
                "window": {"from": window_oldest_row.get("scan_time"), "to": latest.get("scan_time")},
                "requested_hours": hours, "data_points": len(metrics),
            }
        groups = {}
        for row in metrics:
            groups.setdefault(row.get("member_instance_id") or alias_for(resolved_id), []).append(row)
        out = {
            "instance_id": alias_for(resolved_id),
            "display_name": alias,
            "member_selector": selector,
            "hours": hours,
            "source": source,
            "data_points": len(metrics),
            "time_range": f"{oldest.get('scan_time', '?')} to {latest.get('scan_time', '?')}",
            "latest": latest,
            "metrics": metrics[:20],  # Return last 20 data points
        }
        if len(groups) > 1:
            out["by_member"] = {
                mid: {"member_role": rows[0].get("member_role"), "data_points": len(rows),
                      "latest": rows[0],
                      "trend_summary": _trend_lines(rows) or ["All metrics stable over the period."]}
                for mid, rows in groups.items()}
            out["trend_summary"] = [f"{mid}: {line}" for mid, v in out["by_member"].items()
                                    for line in v["trend_summary"]]
        else:
            out["trend_summary"] = _trend_lines(metrics) or ["All metrics stable over the period."]
        return out
    except Exception as e:
        logger.error(f"get_historical_metrics failed: {e}")
        return {"error": str(e), "metrics": []}


@tool
def assess_growth(instance_id: str, hours: int = 24) -> dict:
    """Deterministic growth verdict for a database over a recent window.

    Decides — in CODE, not by model estimation — whether the database is ACTUALLY
    growing, from the Tier 1 metrics series (database size + worst-table dead-tuple
    %, with a noise floor). Use this for any "is it growing / how fast / will it run
    out of space" question about bloat or storage, and TRUST the `is_growing`
    verdict: if it is false, the size/dead-tuple trend is flat — report bloat as
    STABLE and do NOT extrapolate a monthly or "+X GB" figure the data doesn't show.

    Args:
        instance_id: The database instance ID (or display name).
        hours: Lookback window in hours (default: 24, max: 168).

    Returns:
        {is_growing, reason, size_change_pct, dead_pct_delta, points, window,
         requested_window_hours, observed_window_hours, covers_requested_window}.
        When covers_requested_window is false the samples do not reach back across the
        whole ask (e.g. a newly onboarded database); say so rather than claim the window.
    """
    from tools.growth import compute_growth
    # The writer's series: one continuous history, whatever member the chat focuses. Ask
    # for the growth series (window endpoints), not the display page, so the verdict
    # covers the whole requested window rather than the most recent ~100 minutes.
    res = _historical_metrics(instance_id, hours, member="writer", for_growth=True)
    if isinstance(res, dict) and res.get("error"):
        return {"is_growing": False, "reason": f"growth unknown: {res.get('error')}",
                "size_change_pct": None, "dead_pct_delta": None, "points": 0}
    series = res.get("growth_series", []) if isinstance(res, dict) else []
    out = compute_growth(series)
    # Report the window the data ACTUALLY covers and whether it spans what was asked, so
    # the agent can decline a 24 h / 7 d claim the samples do not reach back far enough to
    # support (e.g. the database was onboarded an hour ago).
    observed = res.get("window") if isinstance(res, dict) else None
    if observed:
        out["window"] = observed
    out["requested_window_hours"] = hours
    covered_hours = _span_hours(observed) if observed else None
    out["observed_window_hours"] = covered_hours
    # Covered when the oldest sample reaches back to within ~15% (or 30 min) of the ask.
    out["covers_requested_window"] = bool(
        covered_hours is not None and covered_hours >= max(hours * 0.85, hours - 0.5))
    return out


def _span_hours(window: dict):
    """Hours between a window's ``from`` and ``to`` ISO timestamps, or None."""
    from datetime import datetime
    try:
        a = datetime.fromisoformat(str(window.get("from")).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(window.get("to")).replace("Z", "+00:00"))
        return round(abs((b - a).total_seconds()) / 3600.0, 2)
    except Exception:
        return None


# `discover_databases` was removed here. It took account_ids and regions straight from
# the model, enumerated the AWS Organization and assumed the conventional discovery
# role in each
# account, so it returned databases from every onboarded account regardless of the
# caller's space — while its siblings in this module (list_monitored_databases,
# get_report_analysis, get_historical_metrics) all scope to the caller's space and fail
# closed. Nothing needed it: onboarding runs through
# POST /api/spaces/{space_id}/discover, which is space-scoped and _can_manage_space
# gated, and no runbook, SOP or test referenced the tool. It had also plainly never been
# exercised — it reported `count` as len() of the (instances, scan_log) TUPLE that
# discover_all returns, so the count was always 2, and the raw cross-account scan log
# was returned to the model.
#
# Deleted rather than scoped, because scoping it would only rebuild
# list_monitored_databases. If a chat-time discovery capability is ever wanted, it must
# derive its account set from the space record (primary_account_id +
# secondary_account_ids) rather than from the model.
