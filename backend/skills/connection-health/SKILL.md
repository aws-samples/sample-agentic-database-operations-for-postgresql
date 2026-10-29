---
name: connection-health
description: Assess connection and session health — slot utilization vs max_connections, idle connections wasting slots, connection pooling efficiency (churn / oversize), and a real-time activity snapshot. Activate on connection exhaustion, too many connections, idle connections, pooling/PgBouncer/RDS Proxy questions, or "are we running out of connections".
allowed-tools: postgres_agent observability_agent
metadata:
  surface: investigation
  check_ids: [CHK-013, CHK-031, CHK-037, CHK-038]
---

# Connection Health Investigation

Assess connection utilization, idle waste, and pooling. The shared diagnostic guardrails apply;
verdicts are already computed by the tools.

## When to use
Symptoms involving: connection exhaustion / approaching max_connections, too many idle connections,
connection churn, pooling questions, or a real-time activity snapshot.

## Steps (which tool to call)
1. **Activity snapshot — `get_current_activity`** (via `postgres_agent`). `summary` gives `usage_pct`
   (connections / max_connections) with a `status` (>80 WARNING, >90 CRITICAL) and the
   `idle_in_txn` / `idle_in_txn_aborted` counts (aborted > 0 is a hard CRITICAL — broken transactions
   holding locks). **Trust `summary.status`.**
2. **Idle connections — `get_idle_connections`** (via `postgres_agent`). Per idle session:
   `idle_seconds`, `application_name`, `client_address`, and a `status` (>15m WARNING, >1h CRITICAL).
   A plain idle session wastes a slot + memory but holds no snapshot — the danger is buildup toward
   max_connections.
3. **Pooling efficiency — `get_connection_pooling_efficiency`** (via `postgres_agent`). `churn_pct`
   (very-short sessions = no reuse) and `long_idle_pct` (pool oversized) with a `status`.
4. **Characterization — `get_connection_analysis`** (via `postgres_agent`). `pooling_detection` (is a
   pooler in front), per-`applications` / per-`clients` breakdown, `connection_churn` buckets, a
   `dominance` block (top_app_pct / top_client_pct / churn_pct), and a `status` (single app >60%
   WARNING, single client IP >80% CRITICAL, churn >30/50%). A pooler is *expected* to concentrate
   connections — read dominance alongside `pooling_detection`, don't flag a pooler as a hot client.
5. **Report source attribution.** For any session-based finding, always include `application_name`
   and `client_addr` (group by IP). Corroborate slot pressure with CloudWatch `DatabaseConnections`.

## Output
Report `usage_pct` vs max_connections, the idle/churn picture, and per-session detail with
`application_name`/`client_addr`. Remediation is a proposal: a connection pooler (RDS Proxy /
PgBouncer) for churn or idle buildup; `idle_in_transaction_session_timeout` / `idle_session_timeout`
for leaks; raising `max_connections` only if the instance has the memory. If connections are exhausted
and blocked diagnosis, say which checks could not run rather than assuming those dimensions are
healthy. Terminating a session is the operator's call.

Refer to this in user-facing output by name — "connection health" — never by an internal catalog code.
Catalog ids live only in this skill's frontmatter metadata.
