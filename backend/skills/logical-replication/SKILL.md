---
name: logical-replication
description: Monitor logical replication health — logical slot lag, publications, and subscription status (CDC/DMS pipelines). Activate on logical replication, CDC, DMS, publication/subscription issues, a disabled subscription, or a logical slot pinning WAL.
allowed-tools: postgres_agent observability_agent
metadata:
  surface: investigation
  check_ids: [CHK-036]
---

# Logical Replication Investigation

Assess logical replication (CDC/DMS) — slots, publications, subscriptions. The shared diagnostic
guardrails apply; verdicts are already computed by the tool.

## When to use
Symptoms involving: logical replication / CDC / DMS, a publication or subscription problem, a disabled
subscription, or a logical slot pinning WAL.

## Steps (which tool to call)
1. **Primary — `get_logical_replication`** (via `postgres_agent`). Returns `logical_slots` (each with
   `lag_bytes`, `active`, and a `status`: >1GB WARNING, >10GB CRITICAL; inactive escalates),
   `publications`, and `subscriptions` (each with a `status`: `subenabled = false` is WARNING —
   data divergence). **Trust the statuses.** If `subscriptions` is empty with a note, `pg_subscription`
   needs superuser — report that as a gap, not "no subscriptions".
2. **Orphaned logical slot = the classic WAL-fill cause.** A finished CDC/DMS job often leaves an
   inactive logical slot whose `restart_lsn` pins WAL forever. Cross-link the WAL-directory-size
   investigation; corroborate WAL growth via CloudWatch `TransactionLogsDiskUsage`.

## Output
Report each logical slot (lag, active, verdict), disabled subscriptions, and publication config.
Remediation is a proposal: re-enable a subscription that should be running (`ALTER SUBSCRIPTION ...
ENABLE`); for a confirmed-abandoned logical slot, `pg_drop_replication_slot` — with a strong caveat to
verify no CDC/DMS consumer depends on it first, since dropping a needed slot breaks the pipeline.

Refer to this in user-facing output by name — "logical replication" — never by an internal catalog
code. Catalog ids live only in this skill's frontmatter metadata.
