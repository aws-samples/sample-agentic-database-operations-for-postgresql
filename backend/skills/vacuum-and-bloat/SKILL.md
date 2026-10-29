---
name: vacuum-and-bloat
description: Investigate dead-tuple accumulation, table/index bloat, and autovacuum effectiveness. Activate when the user reports storage growth, slow scans, rising read latency/IO, or asks about VACUUM, autovacuum, dead tuples, or bloat.
allowed-tools: postgres_agent observability_agent get_historical_metrics assess_growth
metadata:
  surface: investigation
  check_ids: [CHK-007]
---

# Vacuum & Bloat Investigation

Investigate dead-tuple accumulation and autovacuum effectiveness. Run the steps in order via the specialist agents, but apply judgment — branch on what each step reveals. The *why* (size-weighting, Aurora specifics, anti-patterns) is in the PostgreSQL diagnostics knowledge document; the threshold ranges live in the internal references and are already applied by the primary tool.

## When to use
Symptoms involving: table or index bloat, dead tuples, storage growing, VACUUM / VACUUM FULL / pg_repack, autovacuum not keeping up, or rising read latency / IO that could be bloat-driven.

## Steps (which tool to call)
1. **Primary check — `get_vacuum_stats_health`** (via `postgres_agent`). Per table it returns `dead_pct`, `table_size`, `est_wasted`, `autovacuum_enabled`, `vacuum_status`, and a size-weighted **`health`** verdict (the tool has already applied the reference thresholds). Trust `health`; rank remediation by `est_wasted`, not by raw `dead_pct`.
2. **Autovacuum disabled?** If a table shows `autovacuum_enabled = DISABLED`, that's the root cause — CRITICAL regardless of dead %. Fix: `ALTER TABLE <schema>.<table> SET (autovacuum_enabled = true)`, not parameter tuning. Anti-wraparound caveat: `autovacuum_enabled=false` disables ROUTINE autovacuum only — PostgreSQL still FORCES an anti-wraparound freeze when the table crosses `autovacuum_freeze_max_age` (default 200M). Never state such a table "will never be frozen" or is "excluded from anti-wraparound"; the accurate risk is no routine cleanup plus an eventual disruptive forced freeze.
3. **Autovacuum config — `get_db_configuration`** (via `postgres_agent`). Check `autovacuum_max_workers`, `autovacuum_vacuum_cost_delay`, `autovacuum_vacuum_scale_factor`. Too few workers / high cost_delay / a high scale_factor on huge tables = autovacuum starved; recommend a per-table lower scale_factor (0.01–0.05) for very large tables. **Never state a parameter's current value OR its "default" from memory** — cite only what `get_db_configuration` returned THIS run (it reports the effective value and source). If you did not fetch it, describe the lever qualitatively without a number (e.g. "lower the per-table scale_factor" — not "the default is 0.2"). Aurora often ships non-default values (e.g. a tuned naptime), so a memorized default is frequently wrong.
4. **Is it actually growing? — `assess_growth`.** For any "storage keeps growing / how fast / will it fill up" question, call `assess_growth` and **trust its `is_growing` verdict** — it is computed in code from the Tier 1 trend. If `is_growing` is false, the size and dead-tuple trend are flat: report bloat as STABLE and do **NOT** extrapolate a monthly or "+X GB" figure. Never estimate growth by eyeballing the series yourself.
5. **Blocked vacuum?** If autovacuum ran recently but dead tuples persist, check held snapshots via `get_current_activity`'s `snapshot_holders` field (it reports `backend_xmin` and `xmin_age_xids`, and already excludes the diagnostic session) and lock chains via `get_lock_analysis` (via `postgres_agent`). Do NOT hand-roll a `pg_stat_activity` query or cast `backend_xmin` — use the field. STRICT: flag a snapshot as a vacuum blocker ONLY when `xmin_age_xids` > 1,000,000 AND dead tuples exist. An idle session with NULL `backend_xmin` or age 0 is NOT a blocker — never recommend terminating it. A genuinely held snapshot — not config — is then the cause.
6. **Append-heavy tables (PG 13+) — `get_insert_autovacuum_lag`** (via `postgres_agent`). Surfaces insert-only tables overdue for insert-triggered autovacuum, where freeze falls behind even with few dead tuples.
7. **Corroborate with AWS — `observability_agent`.** Fetch CloudWatch `ReadLatency` / `ReadIOPS` and Performance Insights `IO:DataFileRead` over the incident window. **If `observability_agent` returns empty, an error, or "access denied", the I/O corroboration is UNAVAILABLE — say so explicitly and do NOT assert I/O, disk latency, or cache-miss effects you could not measure.** Only call the slowness I/O-driven if the data shows it; if reads/IOPS are ~0 and waits are CPU-dominated, the cost is CPU scanning dead tuples, not I/O. Use `get_historical_metrics` for the dead-tuple trend before querying live.

## Prerequisite / no-match caveat
If you use autovacuum-completion log lines to judge health, that signal is valid ONLY if `log_autovacuum_min_duration` is set and PostgreSQL log export to CloudWatch is enabled. If those aren't confirmed, report the log evidence as INCONCLUSIVE (a monitoring gap) — never conclude "healthy" from absent logs.

## Output
Report by severity (CRITICAL / WARNING / NOTICE), each labeled with its source (`[pg_stat_user_tables]`, `[CloudWatch]`, `[Performance Insights]`). For each bloated table: `schema.table`, `dead_pct`, size, `est_wasted`, autovacuum status, and the specific remediation.

Remediation wording must be precise about reclamation: **plain VACUUM (and autovacuum) return dead space to the table's free space map for REUSE — they do NOT shrink the on-disk file or hand space back to the OS.** Only `VACUUM FULL` (takes an AccessExclusiveLock, blocks all queries — offer only as a windowed option with that warning) or `pg_repack` (cautioned last resort) reduce on-disk size. So describe `est_wasted` as space "freed for reuse", never "reclaimed to disk" or the table "shrinking", unless you are recommending VACUUM FULL / pg_repack.

Refer to this in user-facing output by name — "table bloat / autovacuum" — never by an internal catalog code. The catalog id lives only in this skill's frontmatter metadata for traceability and is never user-facing.
