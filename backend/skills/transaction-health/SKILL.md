---
name: transaction-health
description: Assess per-database transaction health — the commit/rollback ratio, with transaction-ID age as context. Activate on high rollback rate, aborted transactions, deadlock-driven rollbacks, or transaction throughput/health questions.
allowed-tools: postgres_agent observability_agent
metadata:
  surface: investigation
  check_ids: [CHK-021]
---

# Transaction Health Investigation

Assess the commit/rollback balance and flag rollback storms. The shared diagnostic guardrails apply;
the verdict is already computed by the tool.

## When to use
Symptoms involving: a high rollback rate, many aborted transactions, deadlock-driven rollbacks, or
"is the transaction throughput healthy".

## Steps (which tool to call)
1. **Primary — `get_transaction_stats`** (via `postgres_agent`). Per database: `xact_commit`,
   `xact_rollback`, `rollback_pct`, `xid_age`, and a `status` = the worse of the rollback bands
   (>5% WARNING, >10% CRITICAL) and the XID-age model (shared with the wraparound check, so they never
   disagree). **Trust `status`.**
2. **Explain a high rollback ratio.** Rollbacks come from application errors, explicit ROLLBACKs,
   deadlocks, or statement timeouts. If deadlocks are suspected, cross-check `get_lock_analysis`. A
   high ratio is an application/contention signal, not a database-config problem.
3. **XID context.** If `status` is driven by XID age rather than rollbacks, hand off to the
   transaction-wraparound investigation — the remediation there (vacuum/freeze) is different.

## Output
Report per-database `rollback_pct` (with the commit/rollback counts) and the verdict, and name the
likely rollback source. Remediation is a proposal aimed at the application/contention (fix the erroring
path, resolve deadlocks, set sane timeouts) — the database is reporting the symptom. Note the counts
are cumulative since the last stats reset, so a high lifetime ratio may predate the current concern —
correlate timing before blaming a current incident.

Refer to this in user-facing output by name — "transaction health" — never by an internal catalog
code. Catalog ids live only in this skill's frontmatter metadata.
