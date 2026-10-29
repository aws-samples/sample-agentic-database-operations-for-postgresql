# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deterministic findings engine — replaces LLM for health_score and findings derivation.

Takes structured scan data (from psycopg2 extraction or JSONB) and applies threshold
rules matching the shell script's severity classifications. Returns (health_score, summary,
findings_list).

Thresholds are the same as the shell script's HTML color coding:
  CRITICAL / WARNING / INFO / OK
"""

import logging

logger = logging.getLogger("findings-engine")

# Severity bands come from backend/checks/references.yaml (bundled into the image),
# so this deterministic engine and the agentic tools share one set of numbers (D4).
try:
    from .thresholds import (
        cache_hit_severity, bloat_severity, xid_severity,
        connection_usage_severity, idle_connection_severity,
        replication_lag_severity, replication_bytes_severity, recovery_conflicts_severity,
        session_age_severity, managed_roles,
    )
except ImportError:  # imported as a top-level module rather than utils.findings_engine
    from thresholds import (
        cache_hit_severity, bloat_severity, xid_severity,
        connection_usage_severity, idle_connection_severity,
        replication_lag_severity, replication_bytes_severity, recovery_conflicts_severity,
        session_age_severity, managed_roles,
    )

# The wraparound denominator, shared with every other surface (tools.xid, staged into
# the batch image). Used to recover a raw XID age from a stored percentage.
try:
    from tools.xid import XID_WRAPAROUND_MAX
except ImportError:  # pragma: no cover - unit tests without the staged tools/ package
    XID_WRAPAROUND_MAX = 2_147_483_647


def derive_findings(data: dict) -> tuple[str, str, list[dict]]:
    """Derive health_score, summary, and findings from structured scan data."""
    findings = []

    # Cache hit ratio — bands from references.yaml#database-cache-hit
    for cr in data.get("cache_ratios", []):
        ratio = cr.get("hit_ratio")
        db = cr.get("database", "")
        sev = cache_hit_severity(ratio)
        if sev == "CRITICAL":
            findings.append(_finding("CRITICAL", "Cache", f"Low cache hit ratio: {ratio}%",
                f"Database '{db}' buffer cache hit ratio is {ratio}%, below the critical threshold.",
                "Increase shared_buffers or investigate queries causing excessive disk reads."))
        elif sev == "WARNING":
            findings.append(_finding("WARNING", "Cache", f"Cache hit ratio below optimal: {ratio}%",
                f"Database '{db}' buffer cache hit ratio is {ratio}%, below the optimal threshold.",
                "Monitor for degradation. Consider increasing shared_buffers if workload is growing."))

    # Table bloat / dead tuples — size-weighted bands from references.yaml#vacuum-and-bloat
    # (CHK-007), the same model the agent path (get_vacuum_stats_health) applies. Larger
    # tables escalate at a lower dead-tuple %. The NOTICE tier (minor bloat on small tables)
    # is intentionally not surfaced as a finding here to keep the batch report's signal high;
    # the raw per-table numbers are still in the stored scan data.
    for table in data.get("table_bloat", []):
        pct = table.get("bloat_pct", 0)
        tname = table.get("table", table.get("tablename", "unknown"))
        schema = table.get("schema", table.get("schemaname", "public"))
        dead = table.get("dead_tuples", table.get("n_dead_tup", 0))
        sev = bloat_severity(pct, table.get("total_bytes"))
        if sev == "CRITICAL":
            findings.append(_finding("CRITICAL", "Bloat", f"Severe bloat on {schema}.{tname}: {pct}%",
                f"Table {schema}.{tname} has {pct}% dead tuples ({dead:,} dead rows).",
                f"Run: VACUUM (VERBOSE) {schema}.{tname}; Consider pg_repack if space must be reclaimed."))
        elif sev == "WARNING":
            findings.append(_finding("WARNING", "Bloat", f"Table bloat on {schema}.{tname}: {pct}%",
                f"Table {schema}.{tname} has {pct}% dead tuples ({dead:,} dead rows).",
                f"Verify autovacuum is running for '{tname}'."))

    # XID wraparound — classified on the raw XID age against the absolute floors in
    # references.yaml#xid-wraparound-database (CHK-001), so the batch and agent paths
    # agree. pct (of the 2.1B hard max) is kept for the human-readable message.
    for db in data.get("xid_age", []):
        pct = db.get("pct", db.get("xid_age_percent", 0))
        age = db.get("age", db.get("xid_age"))
        dbname = db.get("database", db.get("datname", ""))
        age_int = int(age) if age is not None else None
        sev = xid_severity(age_int)
        # Fallback if only the pct is present: derive an age from the 2.1B hard max.
        if sev is None and age_int is None and pct:
            sev = xid_severity(int(float(pct) / 100.0 * XID_WRAPAROUND_MAX))
        age_txt = f" ({age_int:,} XIDs)" if age_int is not None else ""
        if sev == "CRITICAL":
            findings.append(_finding("CRITICAL", "XID", f"XID wraparound risk: {pct}% on {dbname}",
                f"Transaction ID age is at {pct}% of the wraparound threshold{age_txt}.",
                f"Immediately run: VACUUM FREEZE; on database '{dbname}'."))
        elif sev == "WARNING":
            findings.append(_finding("WARNING", "XID", f"XID age elevated: {pct}% on {dbname}",
                f"Transaction ID age is at {pct}% of the wraparound threshold{age_txt}.",
                "Schedule VACUUM FREEZE during the next maintenance window."))

    # Connection usage — utilisation bands from references.yaml#current-activity-snapshot
    activity = data.get("activity", {})
    conn_pct = activity.get("connection_utilization_pct", 0)
    total_conns = activity.get("total_connections", 0)
    idle_in_txn = activity.get("idle_in_txn", 0)
    conn_sev = connection_usage_severity(conn_pct)
    if conn_sev == "CRITICAL":
        findings.append(_finding("CRITICAL", "Connections", f"Connection usage critical: {conn_pct}%",
            f"{total_conns} connections, using {conn_pct}% of max_connections.",
            "Implement connection pooling immediately."))
    elif conn_sev == "WARNING":
        findings.append(_finding("WARNING", "Connections", f"Connection usage high: {conn_pct}%",
            f"{total_conns} connections, using {conn_pct}% of max_connections.",
            "Consider connection pooling."))
    elif conn_sev == "NOTICE":
        findings.append(_finding("INFO", "Connections", f"Connection usage elevated: {conn_pct}%",
            f"{total_conns} connections, using {conn_pct}% of max_connections.",
            "Headroom is shrinking; plan for connection pooling."))
    # idle-in-transaction COUNT is a batch-specific aggregate (no single-value band in
    # references.yaml — the catalog treats idle-in-transaction as a per-session/lock signal).
    if idle_in_txn > 10:
        sev = "CRITICAL" if idle_in_txn > 50 else "WARNING"
        findings.append(_finding(sev, "Connections", f"{idle_in_txn} idle-in-transaction connections",
            f"{idle_in_txn} connections are idle in transaction, holding locks.",
            "Set idle_in_transaction_session_timeout."))

    # ── Count-based aggregate heuristics (blocked queries, unused/duplicate indexes,
    # high-risk roles) are batch-specific: they roll up how MANY items a whole-fleet scan
    # found, which has no single-value counterpart in references.yaml (the agent-path
    # checks judge each item individually). These stay engine-local by design. ──

    # Blocked queries
    blocked = _get_nested(data, "health_summary", "blocked_queries", default=0)
    if blocked > 20:
        findings.append(_finding("CRITICAL", "Locks", f"{blocked} blocked queries",
            f"{blocked} queries waiting for locks.", "Check pg_locks for blocking chains."))
    elif blocked > 0:
        findings.append(_finding("WARNING", "Locks", f"{blocked} blocked queries",
            f"{blocked} queries waiting for locks.", "Monitor lock contention."))

    # Unused indexes
    unused = data.get("indexes", {}).get("unused", [])
    if len(unused) > 20:
        findings.append(_finding("WARNING", "Indexes", f"{len(unused)} unused indexes",
            f"{len(unused)} indexes have zero scans.", "Review and drop unused indexes."))
    elif len(unused) > 5:
        findings.append(_finding("INFO", "Indexes", f"{len(unused)} unused indexes",
            f"{len(unused)} indexes have zero scans.", "Review unused indexes periodically."))

    # Duplicate indexes
    dupes = data.get("indexes", {}).get("duplicate", [])
    if dupes:
        findings.append(_finding("WARNING", "Indexes", f"{len(dupes)} duplicate indexes",
            f"{len(dupes)} indexes have identical definitions.", "Drop redundant indexes."))

    # Security — AWS-managed roles (rdsadmin et al.) are skipped: RDS reserves them and the
    # customer cannot act on them, matching the agent's security audit (CHK-042).
    security = data.get("security", {})
    managed = managed_roles()
    for role in security.get("roles", []):
        if str(role.get("name") or "").lower() in managed:
            continue
        if role.get("risk") == "HIGH" and role.get("can_login"):
            findings.append(_finding("WARNING", "Security", f"High-risk role: {role.get('name', 'unknown')}",
                f"Role '{role.get('name')}' has elevated privileges and can login.",
                "Review role privileges. Apply principle of least privilege."))

    # Idle connections — per-session severity from references.yaml#idle-connections
    # (age bands), then the >5 long-idle roll-up (a batch-specific aggregate).
    idle_conns = data.get("idle_connections", [])
    critical_idle = [c for c in idle_conns
                     if idle_connection_severity(c.get("duration_sec")) == "CRITICAL"]
    if len(critical_idle) > 5:
        findings.append(_finding("WARNING", "Connections", f"{len(critical_idle)} long-idle connections",
            f"{len(critical_idle)} connections idle for extended periods.",
            "Set idle_session_timeout or implement connection pooling."))

    # Critical metrics
    for cm in data.get("critical_metrics", []):
        if cm.get("status") == "CRITICAL":
            findings.append(_finding("CRITICAL", "System", cm.get("category", "Unknown"),
                f"{cm.get('category', '')}: {cm.get('value', '')}", "Investigate immediately."))

    # Derive health_score
    severities = [f["severity"] for f in findings]
    if "CRITICAL" in severities:
        health_score = "critical"
    elif "WARNING" in severities:
        health_score = "warning"
    else:
        health_score = "ok"

    # Build summary
    crit_count = severities.count("CRITICAL")
    warn_count = severities.count("WARNING")
    info_count = severities.count("INFO")
    if health_score == "critical":
        top = [f["title"] for f in findings if f["severity"] == "CRITICAL"][:3]
        summary = f"Database health is critical. {crit_count} critical and {warn_count} warning findings. Key issues: {'; '.join(top)}."
    elif health_score == "warning":
        top = [f["title"] for f in findings if f["severity"] == "WARNING"][:3]
        summary = f"Database health has warnings. {warn_count} warning and {info_count} informational findings. Key issues: {'; '.join(top)}."
    else:
        summary = f"Database health is good. {info_count} informational findings, no critical or warning issues."

    return health_score, summary, findings


def derive_reader_findings(data: dict) -> tuple[str, str, list[dict]]:
    """Health score, summary and findings for a READER scan (``extract_reader_data``):
    what matters on a standby. Bands from references.yaml through thresholds.py."""
    findings = []
    repl = data.get("replication") or {}

    # Replica lag: Aurora reports milliseconds, a streaming standby a replay delay in s.
    lag_s = None
    if repl.get("replica_lag_ms") is not None:
        lag_s = float(repl["replica_lag_ms"]) / 1000.0
    elif repl.get("replay_delay_s") is not None:
        lag_s = float(repl["replay_delay_s"])
    sev = replication_lag_severity(lag_s)
    if sev in ("CRITICAL", "WARNING"):
        findings.append(_finding(sev, "Replication", f"Replica lag {lag_s:.1f}s",
            f"This reader is {lag_s:.1f}s behind the writer; reads here return data that old.",
            "Check the writer's write load and this reader's replay (I/O, long queries holding "
            "replay back, instance size)."))
    lag_bytes = repl.get("replay_lag_bytes")
    sev = replication_bytes_severity(lag_bytes)
    if sev in ("CRITICAL", "WARNING"):
        findings.append(_finding(sev, "Replication", f"{int(lag_bytes):,} bytes of WAL not yet replayed",
            "WAL has been received but not replayed on this reader.",
            "Replay is falling behind: look for long queries and recovery conflicts on this reader."))
    status = repl.get("wal_receiver_status")
    if repl.get("wal_receiving") is False or status not in (None, "", "streaming"):
        findings.append(_finding("WARNING", "Replication",
            "Standby not receiving WAL" + (f" (WAL receiver {status})" if status else " (no WAL receiver)"),
            "This standby is not streaming from its primary, so it can fall behind while showing "
            "nothing left to replay; its replay lag is unknown, not 0.",
            "Check the primary and the network path."))

    # Recovery conflicts: cumulative since the statistics were last reset.
    conflicts = data.get("recovery_conflicts") or {}
    total = sum(int(conflicts.get(k) or 0) for k in
                ("confl_tablespace", "confl_lock", "confl_snapshot", "confl_bufferpin", "confl_deadlock"))
    sev = recovery_conflicts_severity(total)
    if sev in ("CRITICAL", "WARNING"):
        findings.append(_finding(sev, "Replication", f"{total} queries cancelled by recovery conflicts",
            f"Since statistics were last reset, {total} queries on this reader were cancelled because "
            f"replay needed what they were reading (snapshot {conflicts.get('confl_snapshot') or 0}, "
            f"lock {conflicts.get('confl_lock') or 0}).",
            "Raise max_standby_streaming_delay, enable hot_standby_feedback, or move long reads "
            "elsewhere."))

    # Long-running queries; with hot_standby_feedback on they hold back vacuum on the writer.
    settings = {s.get("name"): s.get("setting") for s in data.get("standby_settings") or []}
    feedback = str(settings.get("hot_standby_feedback", "")).lower() == "on"
    longest = max((q.get("duration_sec") or 0 for q in data.get("long_queries") or []), default=0)
    sev = session_age_severity(longest)
    if sev in ("CRITICAL", "WARNING"):
        n = len(data.get("long_queries") or [])
        if feedback:
            findings.append(_finding("WARNING", "Replication",
                f"Long query on a reader with hot_standby_feedback on ({int(longest)}s)",
                f"{n} queries have run over a minute, the longest {int(longest)}s. With "
                "hot_standby_feedback on, the oldest one holds back vacuum on the writer, so dead "
                "rows accumulate there.",
                "Shorten or move the long reads, or set a statement_timeout for this workload."))
        else:
            findings.append(_finding(sev, "Queries", f"Long-running query on the reader ({int(longest)}s)",
                f"{n} queries have run over a minute, the longest {int(longest)}s.",
                "Review the query; with hot_standby_feedback off it risks a recovery-conflict cancel."))

    # Cache and connections, as for the writer.
    for cr in data.get("cache_ratios") or []:
        sev = cache_hit_severity(cr.get("hit_ratio"))
        if sev in ("CRITICAL", "WARNING"):
            findings.append(_finding(sev, "Cache", f"Reader cache hit ratio {cr.get('hit_ratio')}%",
                f"This reader's buffer cache hit ratio is {cr.get('hit_ratio')}%.",
                "Read load on this reader misses cache; consider its instance size or the queries."))
    activity = data.get("activity") or {}
    conn_pct = activity.get("connection_utilization_pct")
    sev = connection_usage_severity(conn_pct)
    if sev in ("CRITICAL", "WARNING"):
        findings.append(_finding(sev, "Connections", f"Reader connection usage {conn_pct}%",
            f"{activity.get('total_connections')} connections, {conn_pct}% of max_connections.",
            "Spread reads across readers or pool connections."))

    # Bloat as seen from the reader: a standby's counters reflect only its local activity.
    for table in data.get("table_bloat") or []:
        sev = bloat_severity(table.get("bloat_pct", 0), table.get("total_bytes"))
        if sev in ("CRITICAL", "WARNING"):
            findings.append(_finding("INFO", "Bloat",
                f"Bloat seen from the reader on {table.get('schema')}.{table.get('table')}: {table.get('bloat_pct')}%",
                "On a physical standby these counters reflect local activity only; the writer's deep "
                "scan is authoritative for bloat.", "Compare with the writer's scan."))

    severities = [f["severity"] for f in findings]
    health = "critical" if "CRITICAL" in severities else "warning" if "WARNING" in severities else "ok"
    if health == "ok":
        summary = "Reader is healthy: no critical or warning findings."
    else:
        top = [f["title"] for f in findings if f["severity"] == health.upper()][:3]
        summary = f"Reader health is {health}. Key issues: {'; '.join(top)}."
    return health, summary, findings


def _finding(severity, category, title, detail, resolution):
    return {"severity": severity, "category": category, "title": title, "detail": detail, "resolution": resolution}


def _get_nested(data, *keys, default=None):
    current = data
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return default
        if current is None:
            return default
    return current


def extract_top_level_metrics(data: dict) -> dict:
    cache_ratios = data.get("cache_ratios", [])
    cache_hit = cache_ratios[0].get("hit_ratio") if cache_ratios else None
    bloat = data.get("table_bloat", [])
    worst_bloat = max((t.get("bloat_pct", 0) for t in bloat), default=None) if bloat else None
    xid = data.get("xid_age", [])
    worst_xid = max((x.get("pct", x.get("xid_age_percent", 0)) for x in xid), default=None) if xid else None
    activity = data.get("activity", {})
    total_conns = activity.get("total_connections")
    # Headline size is the MONITORED database, in MB, measured directly (run_scan's
    # monitored_database_size_mb). The per-database `database_sizes` list is for display
    # only — sourcing the headline from sizes[0] gave the LARGEST database's size (every
    # RDS instance has `postgres` etc.) and quantised it (a GB value rounded to 2 dp,
    # then *1024). Fall back to the old computation only for a legacy scan with no direct
    # measurement, so an older stored report still renders.
    db_size_mb = data.get("monitored_database_size_mb")
    if db_size_mb is None:
        sizes = data.get("database_sizes", [])
        db_size_mb = round(sizes[0].get("size_gb", 0) * 1024, 2) if sizes else None
    unused_idx = len(data.get("indexes", {}).get("unused", []))
    return {
        "cache_hit_ratio": cache_hit, "dead_tuple_pct": worst_bloat, "xid_age_pct": worst_xid,
        "total_connections": total_conns,
        "database_size_mb": db_size_mb,
        "unused_indexes_count": unused_idx,
    }
