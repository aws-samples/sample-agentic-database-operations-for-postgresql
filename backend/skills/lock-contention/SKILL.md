---
name: lock-contention
description: Investigate active lock contention — blocked queries, their blockers, lock types, wait duration, and deadlock history. Activate on blocked queries, lock waits, deadlocks, "queries are stuck", or hot-row / idle-in-transaction contention.
allowed-tools: postgres_agent observability_agent
metadata:
  surface: investigation
  check_ids: [CHK-033]
---

# Lock Contention Investigation

Find blocked queries and the sessions blocking them. The shared diagnostic guardrails apply; verdicts
are already computed by the tool.

## When to use
Symptoms involving: blocked/stuck queries, lock waits, deadlocks, or hot-row contention.

## Steps (which tool to call)
1. **Primary — `get_lock_analysis`** (via `postgres_agent`). Returns `blocked_queries` (each with
   `blocked_pid`/`blocking_pid`, users, lock type, `locked_relation`, `wait_seconds`, and a `status`:
   wait >10s WARNING, >60s CRITICAL) and `deadlock_stats` (deadlock count with its own `status`:
   any WARNING, >10 CRITICAL). **Trust the statuses.** Report the blocker's `client_addr` /
   `application_name`, never a bare PID.
2. **Identify the pattern.** Small hot tables with many concurrent UPDATEs = hot-row contention;
   `transactionid` locks = row-level contention on UPDATE/DELETE. An **idle-in-transaction** session
   holding locks is a common root — cross-check `get_current_activity`.
3. **Wait events — `get_wait_event_analysis`.** Confirms the breadth of `Lock:*` waiting across
   sessions (a cascade of blocked sessions is more urgent than a single wait).

## Output
Report the blocking chain(s): `blocking_pid` (with client_addr/app) → `blocked_pid`(s), the
`locked_relation`, lock type, and `wait_seconds`; plus the deadlock count and verdict. Remediation is a
proposal: resolve or terminate the confirmed blocker (an idle-in-transaction holder), fix the
application transaction that holds locks too long, and set `idle_in_transaction_session_timeout`.
Terminating a session is the operator's call — present it, do not imply the agent does it.

Refer to this in user-facing output by name — "lock contention" — never by an internal catalog code.
Catalog ids live only in this skill's frontmatter metadata.
