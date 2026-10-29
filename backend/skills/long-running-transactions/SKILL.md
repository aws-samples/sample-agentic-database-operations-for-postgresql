---
name: long-running-transactions
description: Find long-running active transactions that pin an old backend_xmin — blocking autovacuum from cleaning dead tuples, holding back XID freeze, and stalling replication slot advancement. Activate on long-running transaction, idle-in-transaction holding a snapshot, "vacuum is blocked", oldest xmin, or a transaction open for a long time.
allowed-tools: postgres_agent observability_agent
metadata:
  surface: investigation
  check_ids: [CHK-049]
---

# Long-Running Transaction Investigation

Find transactions that have been open long enough to pin a snapshot and block maintenance. The shared
diagnostic guardrails apply; the verdict is already computed by the tool. This investigation is the
shared "what's blocking vacuum/freeze" step that the vacuum-and-bloat and transaction-wraparound
investigations also lean on.

## When to use
Symptoms involving: a long-running transaction, an idle-in-transaction session holding a snapshot,
"vacuum is not cleaning dead tuples", `oldest xmin` warnings, or a transaction open for a long time.

## Steps (which tool to call)
1. **Primary — `get_current_activity`** (via `postgres_agent`). `active_queries` carries
   `txn_duration_seconds` and a `status` (>30m WARNING, >1h CRITICAL). `snapshot_holders` carries
   `backend_xmin` and `xmin_age_xids` — the vacuum-blocker signal. **Trust `status`** for duration;
   apply the STRICT blocker rule below for snapshots.
2. **Strict vacuum-blocker rule.** Flag a session as blocking vacuum ONLY when `xmin_age_xids`
   > 1,000,000 AND dead tuples exist on some table. An idle session with NULL `backend_xmin` or age 0
   is NOT a blocker — never recommend terminating it. Do not hand-roll a pg_stat_activity query or
   cast backend_xmin; read the `snapshot_holders` field.
3. **Trace the impact.** A held snapshot blocks autovacuum (cross-link vacuum-and-bloat), holds back
   XID freeze (cross-link transaction-wraparound), and can stop a replication slot from advancing
   (cross-link `get_replication_status`). Name which of these is actually happening.

## Output
Report each long transaction with `txn_duration_seconds`, `backend_xmin` / `xmin_age_xids`,
`application_name`, and `client_addr` — never a bare PID. Remediation is a proposal: fix the
application path that leaves transactions open, set `idle_in_transaction_session_timeout`, and — only
for a confirmed genuine blocker (xmin age > 1M with dead tuples) — the operator may terminate it. Never
recommend terminating a session whose `backend_xmin` is NULL or young.

Refer to this in user-facing output by name — "long-running transactions" — never by an internal
catalog code. Catalog ids live only in this skill's frontmatter metadata.
