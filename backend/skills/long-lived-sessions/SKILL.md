---
name: long-lived-sessions
description: Identify long-lived client sessions that may be connection leaks or forgotten sessions holding resources. Activate on long-running sessions, connection leaks, sessions open for hours/days, or "why are there so many old connections".
allowed-tools: postgres_agent
metadata:
  surface: investigation
  check_ids: [CHK-024]
---

# Long-Lived Session Investigation

Find client sessions open far longer than expected. The shared diagnostic guardrails apply; the
verdict is already computed by the tool.

## When to use
Symptoms involving: sessions open for hours or days, a suspected connection leak, or an unexplained
pile of old connections.

## Steps (which tool to call)
1. **Primary — `get_session_duration_analysis`** (via `postgres_agent`). Per client session:
   `usename`, `application_name`, `state`, `session_age_seconds`, `last_activity_seconds`,
   `client_addr`, and a `status` (>1h WARNING, >24h CRITICAL). **Trust `status`.** Always report
   `application_name` and `client_addr` for each flagged session (mandatory source attribution).
2. **Long is not automatically bad.** A pooled connection (PgBouncer/RDS Proxy/app pool) is expected
   to live a long time. Distinguish a legitimate pool connection (recent `last_activity_seconds`,
   `state = idle`) from a leak (very old with no recent activity) using `application_name` + activity.
3. **The harmful variant is idle-in-transaction.** A long session that is `idle in transaction` holds
   snapshots (blocks vacuum) and possibly row locks — far worse than a long idle session. Cross-check
   `get_current_activity` `snapshot_holders` for `backend_xmin` age before recommending action.
4. **Prepared-statement memory growth is not directly visible.** When the symptom is a slow
   FreeableMemory decline on long-lived connections, suspect prepared statements accumulating per
   connection (an app that PREPAREs without DEALLOCATE). `get_prepared_statement_analysis` is
   session-local and normally returns empty — an empty result is NOT healthy, because PostgreSQL has
   no cross-session view. Diagnose from the FreeableMemory trend plus the long-lived connections here;
   recommend `DEALLOCATE ALL` periodically and a pool `max-lifetime`.

## Output
Report each flagged session with `application_name`, `client_addr`, age, and `state` — never a bare PID
count. Remediation is a proposal: for a genuine leak, fix the application's connection handling or set
`idle_in_transaction_session_timeout` / `idle_session_timeout`; never recommend terminating a session
that is a healthy pool connection or that has a NULL / young `backend_xmin`.

Refer to this in user-facing output by name — "long-lived sessions" — never by an internal catalog
code. Catalog ids live only in this skill's frontmatter metadata.
