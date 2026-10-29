---
name: replication-health
description: Assess physical replication and WAL configuration — replica lag, replication slot lag, inactive/orphaned slots, wal_level, and WAL sender state. Activate on replica lag, standby falling behind, replication slot lag, WAL level, or "is replication healthy".
allowed-tools: postgres_agent observability_agent
metadata:
  surface: investigation
  check_ids: [CHK-034, CHK-035]
---

# Replication Health Investigation

Assess physical replication and the WAL config that underpins it. The shared diagnostic guardrails
apply; verdicts are already computed by the tools.

## When to use
Symptoms involving: replica lag, a standby falling behind, replication slot lag, WAL level questions,
or "is replication healthy".

## Steps (which tool to call)
1. **Replication status — `get_replication_status`** (via `postgres_agent`). Returns `role`, `slots`
   (each with `lag_bytes`, `active`, and a `status`: >1GB WARNING, >10GB CRITICAL; an inactive slot
   with lag escalates), and `replicas` (each with `replay_lag_seconds` and a `status`: >5s WARNING,
   >30s CRITICAL). **Trust the statuses.**
2. **WAL config + senders — `get_wal_status`** (via `postgres_agent`). Returns WAL `parameters`
   (with `wal_level_status` — `minimal` is CRITICAL: replication is impossible) and `senders` (each
   with a `status`; `streaming` is OK, `catchup`/`backup`/`startup` WARNING).
3. **Aurora caveat (critical).** Aurora reader instances use storage-level replication and **never
   appear in pg_stat_replication**. Do NOT report "no replicas" or "replica missing" on Aurora from
   these tools — judge Aurora replica health with **`get_replica_lag`** (`observability_agent`), which
   returns CloudWatch `AuroraReplicaLag` (milliseconds) per reader.
   **RDS Multi-AZ DB clusters are not Aurora:** their two readers are PostgreSQL streaming standbys,
   so they DO appear in pg_stat_replication, and `get_replica_lag` returns their `ReplicaLag`
   (seconds). RDS read replicas likewise appear as senders and report `ReplicaLag`.
4. **Inactive slot = WAL/disk risk.** An inactive slot pins WAL and can fill disk — cross-link the
   WAL-directory-size investigation.

## Output
Report role, each replica's lag and verdict, and each slot's lag/active/verdict. For Aurora, report
`AuroraReplicaLag` from `get_replica_lag` instead of the (empty) sender list; for RDS read replicas
and Multi-AZ DB cluster readers, corroborate the sender list with their `ReplicaLag`. Remediation is a proposal:
investigate/resume a lagging replica; for a confirmed-orphaned slot, `pg_drop_replication_slot` (with
a strong caveat to confirm no consumer needs it); never set `wal_level = minimal` if replication/PITR
is needed. WAL/replication params change via the parameter group on Aurora/RDS.

Refer to this in user-facing output by name — "replication health" — never by an internal catalog
code. Catalog ids live only in this skill's frontmatter metadata.
