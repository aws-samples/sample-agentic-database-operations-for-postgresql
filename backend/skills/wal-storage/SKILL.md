---
name: wal-storage
description: Detect WAL directory accumulation — a growing WAL file count / total size that signals a stuck replication slot or a failing archive_command. Activate on WAL growing, pg_wal filling up, WAL disk usage, TransactionLogsDiskUsage, or WAL not being recycled.
allowed-tools: postgres_agent observability_agent
metadata:
  surface: investigation
  check_ids: [CHK-028]
---

# WAL Directory Size Investigation

Detect WAL piling up on disk and find why it is not being recycled. The shared diagnostic guardrails
apply; the verdict is already computed by the tool.

## When to use
Symptoms involving: `pg_wal` filling up, rising WAL disk usage, WAL not being recycled, or a
`TransactionLogsDiskUsage` alarm.

## Steps (which tool to call)
1. **WAL directory size — `get_storage_analysis`** (via `postgres_agent`, `wal_directory` field).
   Returns `wal_file_count`, `total_wal_size`, and a `status` (worst of the file-count and total-size
   bands: >500 files / >10GB WARNING; >2000 / >50GB CRITICAL). **Trust `status`.** If WAL directory
   access is not available (needs `pg_ls_waldir()` / `rds_superuser`), report it as a monitoring gap —
   do not assume WAL is fine.
2. **Find why WAL is retained — `get_replication_status`** (via `postgres_agent`). The usual cause is
   a replication slot that is inactive or lagging (its `restart_lsn` pins WAL) or a failing
   `archive_command`. An abandoned logical slot from a finished CDC/DMS job is a classic offender.
   If there is no stuck slot or failing `archive_command`, also check the checkpoint / background-writer
   investigation — an undersized `max_wal_size` driving frequent forced checkpoints can inflate WAL churn.
3. **Corroborate — `observability_agent`.** CloudWatch `TransactionLogsDiskUsage` / checkpoint lag over
   the window confirms accumulation vs. a transient spike.

## Output
Report `wal_file_count` and `total_wal_size` with the verdict, and name the retention cause if found
(which slot, its state and `restart_lsn`; or archive failure). Remediation is a proposal: drop a
confirmed-abandoned replication slot (`pg_drop_replication_slot`) — with a strong caveat to confirm no
consumer needs it — or fix the archive_command; adjust `wal_keep_size` only if that is the cause. Note
WAL settings on Aurora/RDS change via the parameter group.

Refer to this in user-facing output by name — "WAL directory size" — never by an internal catalog
code. Catalog ids live only in this skill's frontmatter metadata.
