# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The deep scan's structured sections.

The shell report (``postgres_health_insights.sh``) is organised in nine sections. This
module re-extracts what matters from each as structured data, so the store holds it per
section (``metrics_store.put_tier2_scan``) and the agent, the report panel and the
comparison read it without parsing HTML. Everything here is SELECT-only, fail-soft (a
missing view, extension or privilege leaves that part None/[] rather than failing the
scan), bounded (top-N everywhere) and keeps query TEXT only after the literal mask
(``redact``), the same rule the report-context text follows.

``extract_sections(cur, data, redact)`` runs the extra queries given the flat ``data``
the findings engine already consumes, and returns ``{section: {...}}``.
``tier2_metrics(sections, data, health_score, findings)`` picks the daily numerics
CloudWatch gets (nothing Tier 1 already publishes every five minutes).
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger("deep-sections")

QUERY_TEXT_MAX = 600   # characters of (masked) query text kept per entry


def _soft(cur, sql: str, one: bool = False):
    """Run one SELECT; a failure (missing view, extension, privilege) is None/[] and is
    logged at debug. Autocommit on the connection keeps the session usable."""
    try:
        cur.execute(sql)
        if one:
            r = cur.fetchone()
            return dict(r) if r else None
        return [dict(r) for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001 - deliberately fail-soft per sub-query
        logger.debug("deep-section query skipped: %s: %s", type(e).__name__, str(e)[:160])
        return None if one else []


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _trim_query(q: Optional[str], redact: Callable[[str], str]) -> str:
    text = redact(q or "")
    return text if len(text) <= QUERY_TEXT_MAX else text[:QUERY_TEXT_MAX] + " …"


# ── Extraction ────────────────────────────────────────────────────────────────

def extract_sections(cur, data: dict, redact: Callable[[str], str]) -> dict:
    """The nine sections from the live catalogs plus the flat ``data`` the scan already
    extracted (overview, cache_ratios, xid_age, activity, database_sizes, table_bloat,
    wait_events, indexes, configuration, security, health_summary, idle_connections)."""
    s: dict = {}

    # ── 1. Overview: environment and workload profile ────────────────────────
    env = _soft(cur, """
        SELECT version() AS version, pg_postmaster_start_time() AS start_time,
               EXTRACT(EPOCH FROM now() - pg_postmaster_start_time())::bigint AS uptime_sec,
               current_setting('server_encoding') AS server_encoding,
               current_setting('TimeZone') AS timezone,
               current_setting('max_connections')::int AS max_connections,
               pg_is_in_recovery() AS in_recovery
    """, one=True) or {}
    extensions = _soft(cur, "SELECT extname AS name, extversion AS version FROM pg_extension ORDER BY extname")
    workload = _soft(cur, """
        SELECT xact_commit, xact_rollback, deadlocks, temp_files,
               ROUND(temp_bytes / (1024*1024.0), 2) AS temp_bytes_mb,
               tup_returned, tup_fetched, tup_inserted, tup_updated, tup_deleted,
               stats_reset,
               ROUND(100.0 * xact_rollback / NULLIF(xact_commit + xact_rollback, 0), 3) AS rollback_ratio_pct,
               ROUND(100.0 * (tup_returned + tup_fetched)
                     / NULLIF(tup_returned + tup_fetched + tup_inserted + tup_updated + tup_deleted, 0), 1) AS read_pct
        FROM pg_stat_database WHERE datname = current_database()
    """, one=True) or {}
    ext_names = {e.get("name") for e in extensions or []}
    s["overview"] = {
        **data.get("overview", {}),
        "start_time": env.get("start_time"), "uptime_sec": env.get("uptime_sec"),
        "server_encoding": env.get("server_encoding"), "timezone": env.get("timezone"),
        "extensions": extensions,
        "pg_stat_statements": "pg_stat_statements" in ext_names,
        "track_io_timing": (_soft(cur, "SELECT current_setting('track_io_timing') AS v", one=True) or {}).get("v"),
        "workload": workload,
        "database_sizes": data.get("database_sizes", []),
        "monitored_database_size_mb": data.get("monitored_database_size_mb"),
    }

    # ── 2. Configuration: the tuned parameter set and what sits outside guidance ──
    settings = _soft(cur, """
        SELECT name, setting, unit, boot_val, source, pending_restart, context, category
        FROM pg_settings WHERE name IN (
            'shared_buffers','effective_cache_size','work_mem','maintenance_work_mem',
            'max_connections','random_page_cost','effective_io_concurrency','seq_page_cost',
            'max_parallel_workers_per_gather','max_parallel_workers','max_worker_processes',
            'wal_compression','max_wal_size','min_wal_size','checkpoint_timeout',
            'checkpoint_completion_target','wal_buffers','synchronous_commit',
            'autovacuum','autovacuum_max_workers','autovacuum_vacuum_scale_factor',
            'autovacuum_analyze_scale_factor','autovacuum_vacuum_cost_limit','autovacuum_naptime',
            'autovacuum_vacuum_insert_scale_factor',
            'idle_in_transaction_session_timeout','statement_timeout','lock_timeout',
            'ssl','password_encryption','log_min_duration_statement','track_io_timing',
            'track_activity_query_size','shared_preload_libraries','default_statistics_target',
            'hot_standby_feedback','max_standby_streaming_delay','wal_level','max_replication_slots',
            'max_wal_senders','jit','huge_pages','temp_file_limit','log_lock_waits','log_temp_files',
            'rds.force_ssl'
        ) ORDER BY name
    """)
    by_name = {r["name"]: r for r in settings or []}

    def _setting(name):
        return (by_name.get(name) or {}).get("setting")

    outside = []

    def _flag(name, condition, recommendation):
        if name in by_name and condition:
            outside.append({"name": name, "current": _setting(name), "recommendation": recommendation})

    _flag("random_page_cost", _num(_setting("random_page_cost")) not in (None,) and _num(_setting("random_page_cost")) > 1.5,
          "1.1 on SSD-backed storage (RDS, Aurora)")
    _flag("effective_io_concurrency", _num(_setting("effective_io_concurrency")) not in (None,) and _num(_setting("effective_io_concurrency")) < 100,
          "200 on SSD-backed storage")
    _flag("autovacuum", _setting("autovacuum") == "off", "on")
    _flag("track_io_timing", _setting("track_io_timing") == "off", "on, so I/O time per query is measured")
    _flag("log_min_duration_statement", _num(_setting("log_min_duration_statement")) == -1,
          "a threshold (e.g. 1000 ms) so slow statements are logged")
    _flag("idle_in_transaction_session_timeout", _num(_setting("idle_in_transaction_session_timeout")) == 0,
          "a timeout so idle-in-transaction sessions cannot hold locks and bloat forever")
    _flag("statement_timeout", _num(_setting("statement_timeout")) == 0, "a timeout matched to the application's SLA")
    _flag("log_lock_waits", _setting("log_lock_waits") == "off", "on, so waits over deadlock_timeout are logged")
    _flag("rds.force_ssl", _setting("rds.force_ssl") == "0", "1, so every connection is encrypted")
    _flag("password_encryption", _setting("password_encryption") == "md5", "scram-sha-256")
    non_default = [r for r in settings or [] if r.get("source") not in (None, "default")]
    s["configuration"] = {
        "settings": settings,
        "legacy": data.get("configuration", []),
        "non_default_count": len(non_default),
        "pending_restart": [r["name"] for r in settings or [] if r.get("pending_restart")],
        "outside_recommendation": outside,
        "role_setting_overrides": _soft(cur, """
            SELECT COALESCE(r.rolname, '(all roles)') AS role, COALESCE(d.datname, '(all databases)') AS database,
                   s.setconfig AS settings
            FROM pg_db_role_setting s
            LEFT JOIN pg_roles r ON r.oid = s.setrole LEFT JOIN pg_database d ON d.oid = s.setdatabase
            ORDER BY 1, 2 LIMIT 50
        """),
    }

    # ── 2b. Security: roles, grants, SSL, RLS ────────────────────────────────
    roles = _soft(cur, """
        SELECT rolname AS name, rolsuper AS is_superuser, rolcreatedb AS can_create_db,
               rolcreaterole AS can_create_role, rolcanlogin AS can_login,
               rolreplication AS replication, rolbypassrls AS bypass_rls, rolconnlimit AS connection_limit,
               (rolvaliduntil IS NOT NULL AND rolvaliduntil < now()) AS password_expired,
               CASE WHEN rolname LIKE 'rds%' THEN 'AWS-MANAGED'
                    WHEN rolsuper THEN 'HIGH'
                    WHEN rolcreatedb OR rolcreaterole OR rolbypassrls THEN 'HIGH'
                    ELSE 'OK' END AS risk
        FROM pg_roles ORDER BY rolsuper DESC, rolname LIMIT 200
    """)
    public_grants = _soft(cur, """
        SELECT table_schema AS schema, COUNT(DISTINCT table_name) AS tables,
               string_agg(DISTINCT privilege_type, ', ') AS privileges
        FROM information_schema.table_privileges
        WHERE grantee = 'PUBLIC' AND table_schema NOT IN ('pg_catalog', 'information_schema')
        GROUP BY table_schema ORDER BY 2 DESC LIMIT 20
    """)
    ssl = _soft(cur, """
        SELECT COUNT(*) FILTER (WHERE s.ssl) AS ssl_connections,
               COUNT(*) FILTER (WHERE NOT s.ssl) AS plain_connections,
               string_agg(DISTINCT s.version, ', ') FILTER (WHERE s.ssl) AS tls_versions
        FROM pg_stat_ssl s JOIN pg_stat_activity a ON a.pid = s.pid
        WHERE a.pid != pg_backend_pid() AND a.client_addr IS NOT NULL
    """, one=True) or {}
    rls = _soft(cur, """
        SELECT n.nspname AS schema, c.relname AS table, c.relrowsecurity AS enabled, c.relforcerowsecurity AS forced,
               (SELECT COUNT(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'r' AND (c.relrowsecurity OR EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid))
        ORDER BY 1, 2 LIMIT 50
    """)
    high_risk = [r for r in roles or [] if r.get("risk") == "HIGH" and r.get("can_login")]
    s["security"] = {
        "roles": roles, "high_risk_roles": [r["name"] for r in high_risk],
        "public_grants": public_grants, "ssl": ssl,
        "password_encryption": _setting("password_encryption"),
        "force_ssl": _setting("rds.force_ssl") or _setting("ssl"),
        "row_level_security": rls,
        "legacy": data.get("security", {}),
    }

    # ── 3. Activity: who is connected, from where, doing what ────────────────
    apps = _soft(cur, """
        SELECT COALESCE(NULLIF(application_name, ''), '(none)') AS application, COUNT(*) AS connections,
               COUNT(*) FILTER (WHERE state = 'active') AS active,
               COUNT(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_txn,
               COUNT(DISTINCT usename) AS users
        FROM pg_stat_activity WHERE pid != pg_backend_pid() AND backend_type = 'client backend'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 25
    """)
    clients = _soft(cur, """
        SELECT host(client_addr) AS client, COUNT(*) AS connections,
               COUNT(DISTINCT usename) AS users, COUNT(DISTINCT application_name) AS applications,
               MIN(backend_start) AS oldest_connection
        FROM pg_stat_activity WHERE pid != pg_backend_pid() AND client_addr IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT 25
    """)
    pooler = _soft(cur, """
        SELECT COUNT(*) AS client_backends,
               ROUND(STDDEV(EXTRACT(EPOCH FROM now() - backend_start)) / 60.0, 1) AS backend_age_stddev_min,
               ROUND(AVG(EXTRACT(EPOCH FROM now() - backend_start)) / 60.0, 1) AS backend_age_avg_min,
               bool_or(application_name ILIKE ANY (ARRAY['%pgbouncer%','%pgpool%','%proxy%','%pool%'])) AS pooler_named,
               COUNT(DISTINCT client_addr) AS distinct_clients
        FROM pg_stat_activity WHERE pid != pg_backend_pid() AND backend_type = 'client backend'
    """, one=True) or {}
    pooled = bool(pooler.get("pooler_named")) or (
        (pooler.get("client_backends") or 0) >= 10 and (pooler.get("backend_age_stddev_min") or 0) < 5
        and (pooler.get("backend_age_avg_min") or 0) > 60)
    long_running = _soft(cur, """
        SELECT pid, usename AS user, application_name AS application, state,
               EXTRACT(EPOCH FROM now() - query_start)::int AS running_sec,
               wait_event_type, wait_event, LEFT(query, %d) AS query
        FROM pg_stat_activity
        WHERE pid != pg_backend_pid() AND state = 'active' AND query_start < now() - interval '30 seconds'
        ORDER BY query_start LIMIT 15
    """ % QUERY_TEXT_MAX)
    for q in long_running or []:
        q["query"] = _trim_query(q.get("query"), redact)
    locks = _soft(cur, """
        SELECT blocked.pid AS blocked_pid, blocking.pid AS blocking_pid,
               blocked.usename AS blocked_user, blocking.usename AS blocking_user,
               EXTRACT(EPOCH FROM now() - blocked.query_start)::int AS blocked_for_sec,
               blocking.state AS blocking_state,
               LEFT(blocked.query, %d) AS blocked_query, LEFT(blocking.query, %d) AS blocking_query
        FROM pg_stat_activity blocked
        JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS b(pid) ON true
        JOIN pg_stat_activity blocking ON blocking.pid = b.pid
        WHERE blocked.pid != pg_backend_pid() ORDER BY blocked.query_start LIMIT 20
    """ % (QUERY_TEXT_MAX, QUERY_TEXT_MAX))
    for l in locks or []:
        l["blocked_query"] = _trim_query(l.get("blocked_query"), redact)
        l["blocking_query"] = _trim_query(l.get("blocking_query"), redact)
    s["activity"] = {
        **data.get("activity", {}),
        "by_application": apps, "by_client": clients,
        "pooler": {**pooler, "pooled_likely": pooled},
        "long_running_queries": long_running, "blocking": locks,
        "idle_connections": data.get("idle_connections", []),
        "wait_events": data.get("wait_events", []),
    }

    # ── 4. Replication (as seen from this member) ────────────────────────────
    standbys = _soft(cur, """
        SELECT application_name, client_addr::text AS client, state, sync_state, sync_priority,
               pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS replay_lag_bytes,
               EXTRACT(EPOCH FROM replay_lag) * 1000 AS replay_lag_ms,
               EXTRACT(EPOCH FROM write_lag) * 1000 AS write_lag_ms,
               EXTRACT(EPOCH FROM flush_lag) * 1000 AS flush_lag_ms
        FROM pg_stat_replication ORDER BY application_name LIMIT 20
    """)
    slots = _soft(cur, """
        SELECT slot_name, slot_type, active, database, plugin,
               pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS retained_wal_bytes,
               wal_status
        FROM pg_replication_slots ORDER BY retained_wal_bytes DESC NULLS LAST LIMIT 20
    """)
    if not slots:
        slots = _soft(cur, """
            SELECT slot_name, slot_type, active, database, plugin,
                   pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS retained_wal_bytes
            FROM pg_replication_slots ORDER BY 6 DESC NULLS LAST LIMIT 20
        """)
    pubs = _soft(cur, "SELECT pubname AS name, puballtables AS all_tables, pubinsert, pubupdate, pubdelete FROM pg_publication LIMIT 20")
    subs = _soft(cur, "SELECT subname AS name, subenabled AS enabled, subslotname AS slot FROM pg_subscription LIMIT 20")
    wal = _soft(cur, "SELECT wal_records, wal_fpi, wal_bytes, wal_buffers_full, stats_reset FROM pg_stat_wal", one=True)
    s["replication"] = {
        "in_recovery": bool((data.get("overview") or {}).get("in_recovery")),
        "standbys": standbys, "slots": slots, "publications": pubs, "subscriptions": subs,
        "settings": {k: _setting(k) for k in ("wal_level", "synchronous_commit", "max_wal_senders",
                                                "max_replication_slots", "hot_standby_feedback",
                                                "max_standby_streaming_delay")},
        "wal_stats": wal,
        "max_replay_lag_ms": max((_num(r.get("replay_lag_ms")) or 0 for r in standbys or []), default=None) if standbys else None,
        "max_retained_wal_mb": (max((_num(r.get("retained_wal_bytes")) or 0 for r in slots or []), default=0) / (1024 * 1024)) if slots else None,
    }

    # ── 5. Data footprint ────────────────────────────────────────────────────
    top_tables = _soft(cur, """
        SELECT n.nspname AS schema, c.relname AS table,
               ROUND(pg_total_relation_size(c.oid) / (1024*1024.0), 2) AS total_mb,
               ROUND(pg_relation_size(c.oid) / (1024*1024.0), 2) AS heap_mb,
               ROUND(pg_indexes_size(c.oid) / (1024*1024.0), 2) AS index_mb,
               ROUND(COALESCE(pg_total_relation_size(c.reltoastrelid), 0) / (1024*1024.0), 2) AS toast_mb,
               c.reltuples::bigint AS est_rows
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p', 'm') AND n.nspname NOT IN ('pg_catalog', 'information_schema')
        ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 25
    """)
    top_indexes = _soft(cur, """
        SELECT n.nspname AS schema, ct.relname AS table, ci.relname AS index,
               ROUND(pg_relation_size(ci.oid) / (1024*1024.0), 2) AS size_mb, COALESCE(s.idx_scan, 0) AS scans
        FROM pg_index i JOIN pg_class ci ON ci.oid = i.indexrelid JOIN pg_class ct ON ct.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = ct.relnamespace
        LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = i.indexrelid
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
        ORDER BY pg_relation_size(ci.oid) DESC LIMIT 25
    """)
    waldir = _soft(cur, "SELECT COUNT(*) AS files, ROUND(SUM(size) / (1024*1024.0), 2) AS size_mb FROM pg_ls_waldir()", one=True)
    totals = _soft(cur, """
        SELECT ROUND(SUM(pg_total_relation_size(c.oid)) / (1024*1024.0), 2) AS user_tables_mb,
               ROUND(SUM(pg_indexes_size(c.oid)) / (1024*1024.0), 2) AS user_indexes_mb,
               COUNT(*) AS user_tables
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    """, one=True) or {}
    s["footprint"] = {
        "database_sizes": data.get("database_sizes", []),
        "monitored_database_size_mb": data.get("monitored_database_size_mb"),
        "totals": totals, "top_tables": top_tables, "top_indexes": top_indexes,
        "wal_directory": waldir,
        "temp_files": workload.get("temp_files"), "temp_bytes_mb": workload.get("temp_bytes_mb"),
    }

    # ── 6. Query & I/O performance ───────────────────────────────────────────
    checkpoints = _soft(cur, """
        SELECT num_timed AS checkpoints_timed, num_requested AS checkpoints_req,
               write_time AS checkpoint_write_time_ms, sync_time AS checkpoint_sync_time_ms,
               buffers_written AS buffers_checkpoint, stats_reset
        FROM pg_stat_checkpointer
    """, one=True)
    if checkpoints is None:
        checkpoints = _soft(cur, """
            SELECT checkpoints_timed, checkpoints_req, checkpoint_write_time AS checkpoint_write_time_ms,
                   checkpoint_sync_time AS checkpoint_sync_time_ms, buffers_checkpoint, stats_reset
            FROM pg_stat_bgwriter
        """, one=True)
    bgwriter = _soft(cur, """
        SELECT buffers_clean, maxwritten_clean, buffers_alloc, stats_reset FROM pg_stat_bgwriter
    """, one=True)
    if checkpoints:
        t, r = _num(checkpoints.get("checkpoints_timed")) or 0, _num(checkpoints.get("checkpoints_req")) or 0
        checkpoints["forced_checkpoint_ratio_pct"] = round(100.0 * r / (t + r), 1) if (t + r) > 0 else None
    io = _soft(cur, """
        SELECT SUM(heap_blks_read) AS heap_blks_read, SUM(heap_blks_hit) AS heap_blks_hit,
               SUM(idx_blks_read) AS idx_blks_read, SUM(idx_blks_hit) AS idx_blks_hit,
               SUM(toast_blks_read) AS toast_blks_read, SUM(toast_blks_hit) AS toast_blks_hit,
               ROUND(100.0 * SUM(heap_blks_hit) / NULLIF(SUM(heap_blks_hit + heap_blks_read), 0), 2) AS heap_hit_pct,
               ROUND(100.0 * SUM(idx_blks_hit) / NULLIF(SUM(idx_blks_hit + idx_blks_read), 0), 2) AS index_hit_pct
        FROM pg_statio_user_tables
    """, one=True)
    io_tables = _soft(cur, """
        SELECT schemaname AS schema, relname AS table, heap_blks_read, heap_blks_hit, idx_blks_read, idx_blks_hit,
               ROUND(100.0 * heap_blks_hit / NULLIF(heap_blks_hit + heap_blks_read, 0), 2) AS heap_hit_pct
        FROM pg_statio_user_tables WHERE heap_blks_read + heap_blks_hit > 0
        ORDER BY heap_blks_read DESC LIMIT 15
    """)
    # pg_stat_statements: PG17 renamed blk_read_time → shared_blk_read_time; PG13 renamed
    # total_time → total_exec_time and added WAL columns. Try newest first.
    pss_variants = (
        """SELECT LEFT(query, {q}) AS query, calls, rows, total_exec_time, mean_exec_time, stddev_exec_time,
                  max_exec_time, total_plan_time, shared_blks_hit, shared_blks_read, shared_blks_dirtied,
                  shared_blks_written, temp_blks_read, temp_blks_written,
                  shared_blk_read_time AS blk_read_time, shared_blk_write_time AS blk_write_time,
                  wal_records, wal_fpi, wal_bytes, userid::regrole::text AS user
           FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
           ORDER BY {order} DESC LIMIT {n}""",
        """SELECT LEFT(query, {q}) AS query, calls, rows, total_exec_time, mean_exec_time, stddev_exec_time,
                  max_exec_time, total_plan_time, shared_blks_hit, shared_blks_read, shared_blks_dirtied,
                  shared_blks_written, temp_blks_read, temp_blks_written,
                  blk_read_time, blk_write_time, wal_records, wal_fpi, wal_bytes, userid::regrole::text AS user
           FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
           ORDER BY {order} DESC LIMIT {n}""",
        """SELECT LEFT(query, {q}) AS query, calls, rows, total_time AS total_exec_time, mean_time AS mean_exec_time,
                  stddev_time AS stddev_exec_time, max_time AS max_exec_time, NULL::numeric AS total_plan_time,
                  shared_blks_hit, shared_blks_read, shared_blks_dirtied, shared_blks_written,
                  temp_blks_read, temp_blks_written, blk_read_time, blk_write_time,
                  NULL::bigint AS wal_records, NULL::bigint AS wal_fpi, NULL::numeric AS wal_bytes,
                  userid::regrole::text AS user
           FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
           ORDER BY {order} DESC LIMIT {n}""",
    )

    # The ORDER BY expression per ranking, per variant (PG17 / PG13-16 / older).
    pss_orders = {
        "time": ("total_exec_time", "total_exec_time", "total_time"),
        "wal": ("wal_bytes", "wal_bytes", None),
        "io": ("shared_blk_read_time + shared_blk_write_time", "blk_read_time + blk_write_time",
               "blk_read_time + blk_write_time"),
        "calls": ("calls", "calls", "calls"),
        "temp": ("temp_blks_written", "temp_blks_written", "temp_blks_written"),
    }

    def _pss(ranking: str, n: int = 25):
        for v, order_col in zip(pss_variants, pss_orders[ranking]):
            if order_col is None:
                continue
            rows = _soft(cur, v.format(q=QUERY_TEXT_MAX, order=order_col, n=n))
            if rows:
                for r in rows:
                    r["query"] = _trim_query(r.get("query"), redact)
                return rows
        return []

    top_by_time = _pss("time", 25)
    top_by_wal = _pss("wal", 10) if top_by_time else []
    top_by_io = _pss("io", 10)
    top_by_calls = _pss("calls", 10)
    temp_heavy = _pss("temp", 10)
    temp_heavy = [r for r in temp_heavy if (_num(r.get("temp_blks_written")) or 0) > 0]
    functions = _soft(cur, """
        SELECT schemaname AS schema, funcname AS function, calls, total_time AS total_time_ms, self_time AS self_time_ms
        FROM pg_stat_user_functions ORDER BY total_time DESC LIMIT 10
    """)
    sessions = _soft(cur, """
        SELECT COUNT(*) AS sessions,
               ROUND(AVG(EXTRACT(EPOCH FROM now() - backend_start)) / 60.0, 1) AS avg_age_min,
               ROUND(MAX(EXTRACT(EPOCH FROM now() - backend_start)) / 60.0, 1) AS max_age_min,
               COUNT(*) FILTER (WHERE now() - backend_start > interval '1 day') AS older_than_1d
        FROM pg_stat_activity WHERE pid != pg_backend_pid() AND backend_type = 'client backend'
    """, one=True)
    prepared = _soft(cur, "SELECT COUNT(*) AS prepared_statements FROM pg_prepared_statements", one=True)
    av = _soft(cur, """
        SELECT COUNT(*) FILTER (WHERE backend_type = 'autovacuum worker') AS workers_active,
               current_setting('autovacuum_max_workers')::int AS workers_max
        FROM pg_stat_activity
    """, one=True) or {}
    if av.get("workers_max"):
        av["utilization_pct"] = round(100.0 * (av.get("workers_active") or 0) / av["workers_max"], 1)
    buffer_by_db = _soft(cur, """
        SELECT datname AS database, ROUND(100.0 * blks_hit / NULLIF(blks_hit + blks_read, 0), 2) AS hit_pct,
               blks_read, blks_hit
        FROM pg_stat_database WHERE datname IS NOT NULL AND blks_hit + blks_read > 0 ORDER BY 3 DESC LIMIT 20
    """)
    s["performance"] = {
        "checkpoints": checkpoints, "bgwriter": bgwriter, "io": io, "io_by_table": io_tables,
        "index_cache_hit_ratio": data.get("index_cache_ratio"),
        "top_queries_by_time": top_by_time, "top_queries_by_wal": top_by_wal,
        "top_queries_by_io_time": top_by_io, "top_queries_by_calls": top_by_calls,
        "temp_file_queries": temp_heavy, "functions": functions,
        "transactions": {k: workload.get(k) for k in ("xact_commit", "xact_rollback", "rollback_ratio_pct", "deadlocks")},
        "sessions": sessions, "prepared_statements": (prepared or {}).get("prepared_statements"),
        "autovacuum_workers": av, "buffer_hit_by_database": buffer_by_db,
        "wait_events": data.get("wait_events", []),
    }

    # ── 7. Maintenance & health ──────────────────────────────────────────────
    vacuum = _soft(cur, """
        SELECT COUNT(*) AS user_tables,
               COUNT(*) FILTER (WHERE last_autovacuum IS NULL AND last_vacuum IS NULL AND n_live_tup > 1000) AS never_vacuumed,
               COUNT(*) FILTER (WHERE last_autoanalyze IS NULL AND last_analyze IS NULL AND n_live_tup > 1000) AS never_analyzed,
               COUNT(*) FILTER (WHERE n_mod_since_analyze > 0.2 * NULLIF(n_live_tup, 0) AND n_live_tup > 1000) AS stale_statistics,
               COUNT(*) FILTER (WHERE n_dead_tup > 0.2 * (n_live_tup + n_dead_tup) AND n_dead_tup > 1000
                                      AND pg_total_relation_size(relid) > 1048576) AS needing_vacuum,
               MAX(n_dead_tup) AS max_dead_tuples
        FROM pg_stat_user_tables
    """, one=True)
    stale = _soft(cur, """
        SELECT schemaname AS schema, relname AS table, n_live_tup AS live_rows, n_mod_since_analyze AS modified_since_analyze,
               last_autoanalyze, last_analyze
        FROM pg_stat_user_tables
        WHERE n_live_tup > 1000 AND n_mod_since_analyze > 0.2 * NULLIF(n_live_tup, 0)
        ORDER BY n_mod_since_analyze DESC LIMIT 15
    """)
    sequences = _soft(cur, """
        SELECT schemaname AS schema, sequencename AS sequence, last_value, max_value,
               ROUND(100.0 * last_value / NULLIF(max_value, 0), 2) AS used_pct
        FROM pg_sequences WHERE last_value IS NOT NULL ORDER BY used_pct DESC NULLS LAST LIMIT 15
    """)
    invalid_idx = _soft(cur, """
        SELECT n.nspname AS schema, ct.relname AS table, ci.relname AS index
        FROM pg_index i JOIN pg_class ci ON ci.oid = i.indexrelid JOIN pg_class ct ON ct.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = ct.relnamespace WHERE NOT i.indisvalid LIMIT 25
    """)
    constraints = _soft(cur, """
        SELECT n.nspname AS schema, c.relname AS table, con.conname AS constraint, con.contype AS type
        FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE NOT con.convalidated LIMIT 25
    """)
    matviews = _soft(cur, """
        SELECT schemaname AS schema, matviewname AS view, ispopulated AS populated,
               ROUND(pg_total_relation_size(format('%I.%I', schemaname, matviewname)::regclass) / (1024*1024.0), 2) AS size_mb
        FROM pg_matviews ORDER BY 4 DESC LIMIT 25
    """)
    s["maintenance"] = {
        "xid_age": data.get("xid_age", []),
        "health_summary": data.get("health_summary", {}),
        "vacuum": vacuum, "stale_statistics": stale, "sequences": sequences,
        "max_sequence_used_pct": max((_num(r.get("used_pct")) or 0 for r in sequences or []), default=None) if sequences else None,
        "invalid_indexes": invalid_idx, "unvalidated_constraints": constraints,
        "materialized_views": matviews,
        "table_bloat": data.get("table_bloat", []),
    }

    # ── 8. Optimization: index and schema ────────────────────────────────────
    idx = data.get("indexes") or {}
    missing_fk = _soft(cur, """
        SELECT n.nspname AS schema, c.relname AS table, con.conname AS constraint,
               array_to_string(ARRAY(SELECT a.attname FROM unnest(con.conkey) k(attnum)
                                     JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum), ', ') AS columns,
               ROUND(pg_total_relation_size(c.oid) / (1024*1024.0), 2) AS table_mb
        FROM pg_constraint con
        JOIN pg_class c ON c.oid = con.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE con.contype = 'f' AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND NOT EXISTS (
              SELECT 1 FROM pg_index i WHERE i.indrelid = con.conrelid
                AND (i.indkey::int2[])[0:array_length(con.conkey, 1) - 1] = con.conkey::int2[])
        ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 50
    """)
    seq_scan = _soft(cur, """
        SELECT schemaname AS schema, relname AS table, seq_scan, seq_tup_read, idx_scan, n_live_tup AS live_rows,
               ROUND(pg_total_relation_size(relid) / (1024*1024.0), 2) AS size_mb,
               ROUND(100.0 * seq_scan / NULLIF(seq_scan + COALESCE(idx_scan, 0), 0), 1) AS seq_scan_pct
        FROM pg_stat_user_tables
        WHERE seq_scan > 100 AND n_live_tup > 10000 AND pg_total_relation_size(relid) > 10485760
          AND seq_scan > COALESCE(idx_scan, 0)
        ORDER BY seq_tup_read DESC LIMIT 25
    """)
    unused_tables = _soft(cur, """
        SELECT schemaname AS schema, relname AS table, n_live_tup AS live_rows,
               ROUND(pg_total_relation_size(relid) / (1024*1024.0), 2) AS size_mb
        FROM pg_stat_user_tables
        WHERE COALESCE(seq_scan, 0) + COALESCE(idx_scan, 0) = 0 AND pg_total_relation_size(relid) > 1048576
        ORDER BY pg_total_relation_size(relid) DESC LIMIT 25
    """)
    triggers = _soft(cur, """
        SELECT n.nspname AS schema, c.relname AS table, COUNT(*) AS triggers,
               COUNT(*) FILTER (WHERE t.tgenabled = 'D') AS disabled
        FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE NOT t.tgisinternal GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 25
    """)
    partitioned = _soft(cur, """
        SELECT n.nspname AS schema, c.relname AS table,
               (SELECT COUNT(*) FROM pg_inherits i WHERE i.inhparent = c.oid) AS partitions,
               ROUND((SELECT COALESCE(SUM(pg_total_relation_size(i.inhrelid)), 0)
                      FROM pg_inherits i WHERE i.inhparent = c.oid) / (1024*1024.0), 2) AS total_mb
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'p' ORDER BY 4 DESC LIMIT 25
    """)
    large_unpartitioned = [t for t in top_tables or [] if (_num(t.get("total_mb")) or 0) > 50 * 1024
                           and not any(p.get("table") == t.get("table") and p.get("schema") == t.get("schema")
                                       for p in partitioned or [])]
    toast = _soft(cur, """
        SELECT n.nspname AS schema, c.relname AS table,
               ROUND(pg_total_relation_size(c.reltoastrelid) / (1024*1024.0), 2) AS toast_mb,
               ROUND(pg_relation_size(c.oid) / (1024*1024.0), 2) AS heap_mb,
               ROUND(100.0 * pg_total_relation_size(c.reltoastrelid) / NULLIF(pg_total_relation_size(c.oid), 0), 1) AS toast_share_pct
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.reltoastrelid <> 0 AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND pg_total_relation_size(c.reltoastrelid) > 10485760
        ORDER BY 3 DESC LIMIT 15
    """)
    s["optimization"] = {
        "unused_indexes": idx.get("unused", []),
        "duplicate_indexes": idx.get("duplicate", []),
        "fk_without_index": missing_fk,
        "sequential_scan_tables": seq_scan,
        "unused_tables": unused_tables,
        "triggers": triggers,
        "partitioned_tables": partitioned,
        "large_unpartitioned_tables": large_unpartitioned[:10],
        "toast": toast,
    }
    # Keep the flat data's index block honest too (the findings engine reads it).
    idx["missing_fk"] = missing_fk or []
    data["indexes"] = idx
    return s


# ── Daily numerics for CloudWatch ─────────────────────────────────────────────

HEALTH_NUM = {"ok": 0, "healthy": 0, "warning": 1, "critical": 2}


def _count(v) -> int:
    return len(v) if isinstance(v, list) else 0


def tier2_metrics(sections: dict, data: dict, health_score: str, findings: list) -> dict:
    """The deep scan's daily numerics for CloudWatch: what a line chart or an alarm can
    use and Tier 1 does not already publish every five minutes. Missing → omitted."""
    sev = [str((f or {}).get("severity", "")).upper() for f in findings or [] if isinstance(f, dict)]
    perf = sections.get("performance") or {}
    maint = sections.get("maintenance") or {}
    opt = sections.get("optimization") or {}
    conf = sections.get("configuration") or {}
    sec = sections.get("security") or {}
    repl = sections.get("replication") or {}
    foot = sections.get("footprint") or {}
    act = sections.get("activity") or {}
    top_q = (perf.get("top_queries_by_time") or [{}])[0] if perf.get("top_queries_by_time") else {}
    top_w = (perf.get("top_queries_by_wal") or [{}])[0] if perf.get("top_queries_by_wal") else {}
    m = {
        "health_score_num": HEALTH_NUM.get(str(health_score).lower()),
        "findings_total": len(sev),
        "findings_critical": sev.count("CRITICAL"),
        "findings_warning": sev.count("WARNING"),
        "findings_info": sev.count("INFO"),
        "unused_indexes_count": _count(opt.get("unused_indexes")),
        "duplicate_indexes_count": _count(opt.get("duplicate_indexes")),
        "fk_without_index_count": _count(opt.get("fk_without_index")),
        "seq_scan_heavy_tables_count": _count(opt.get("sequential_scan_tables")),
        "unused_tables_count": _count(opt.get("unused_tables")),
        "invalid_indexes_count": _count(maint.get("invalid_indexes")),
        "unvalidated_constraints_count": _count(maint.get("unvalidated_constraints")),
        "max_sequence_used_pct": maint.get("max_sequence_used_pct"),
        "stale_statistics_tables_count": (maint.get("vacuum") or {}).get("stale_statistics"),
        "never_vacuumed_tables_count": (maint.get("vacuum") or {}).get("never_vacuumed"),
        "forced_checkpoint_ratio_pct": (perf.get("checkpoints") or {}).get("forced_checkpoint_ratio_pct"),
        "autovacuum_worker_utilization_pct": (perf.get("autovacuum_workers") or {}).get("utilization_pct"),
        "index_cache_hit_ratio": perf.get("index_cache_hit_ratio"),
        "top_query_total_exec_ms": top_q.get("total_exec_time"),
        "top_query_mean_exec_ms": top_q.get("mean_exec_time"),
        "top_query_wal_mb": (float(top_w["wal_bytes"]) / (1024 * 1024)) if top_w.get("wal_bytes") is not None else None,
        "temp_file_queries_count": _count(perf.get("temp_file_queries")),
        "connection_utilization_pct": (data.get("activity") or {}).get("connection_utilization_pct"),
        "settings_outside_recommendation_count": _count(conf.get("outside_recommendation")),
        "high_risk_roles_count": _count(sec.get("high_risk_roles")),
        "plain_connections_count": (sec.get("ssl") or {}).get("plain_connections"),
        "replication_slots_count": _count(repl.get("slots")),
        "max_replay_lag_ms": repl.get("max_replay_lag_ms"),
        "max_retained_wal_mb": repl.get("max_retained_wal_mb"),
        "wal_directory_size_mb": (foot.get("wal_directory") or {}).get("size_mb"),
        "blocking_pairs_count": _count(act.get("blocking")),
        "long_running_queries_count": _count(act.get("long_running_queries")),
    }
    out = {}
    for k, v in m.items():
        f = _num(v)
        if f is not None:
            out[k] = f
    return out
