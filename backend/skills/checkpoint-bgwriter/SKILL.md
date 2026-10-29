---
name: checkpoint-bgwriter
description: Assess checkpoint and background-writer health — too many forced checkpoints (max_wal_size too small) and backends forced to write/fsync dirty buffers themselves. Activate on checkpoint tuning, forced checkpoints, I/O spikes on checkpoints, background writer, or write-latency stalls.
allowed-tools: postgres_agent observability_agent
metadata:
  surface: investigation
  check_ids: [CHK-029, CHK-030]
---

# Checkpoint & Background Writer Investigation

Assess the write/checkpoint path for forced-checkpoint pressure and bgwriter shortfalls. The shared
diagnostic guardrails apply; verdicts are already computed by the tools.

## When to use
Symptoms involving: periodic I/O spikes aligned to checkpoints, checkpoint tuning, `max_wal_size`
questions, or write-latency stalls that smell like buffer/fsync pressure.

## Steps (which tool to call)
1. **Checkpoints — `get_checkpoint_stats`** (via `postgres_agent`). Returns `checkpoints_timed`,
   `checkpoints_requested`, `requested_pct`, write/sync time, config, and a `status` (requested_pct
   >20% WARNING, >50% CRITICAL). **Trust `status`.** A high forced-checkpoint share means
   `max_wal_size` is too small for the write workload — forced checkpoints cause I/O spikes. Also flag
   `sync_time` far exceeding `write_time` (storage saturated).
2. **Background writer — `get_bgwriter_stats`** (via `postgres_agent`). Returns bgwriter counters and a
   `status`: `buffers_backend_fsync > 0` is CRITICAL (backends fsync'ing — severe latency);
   `maxwritten_clean > 0` or backends writing a large share of buffers is WARNING. **On PG17+ the
   backend-write counters moved to `pg_stat_io`** and are not in this view — say those conditions are
   not evaluable there rather than implying health.
3. **Corroborate — `observability_agent`.** CloudWatch WriteIOPS spikes aligned to checkpoint times
   confirm forced-checkpoint I/O pressure.

## Output
Report the forced-checkpoint share and any bgwriter shortfall with the verdict. Remediation is a
proposal: raise `max_wal_size` (and review `checkpoint_completion_target`) when forced checkpoints
dominate; the fix for backend writes is usually the same (let checkpoints/bgwriter keep up). **Config
values are facts** — cite only what `get_checkpoint_stats` returned this run, and note Aurora/RDS
changes go through the parameter group. If forced checkpoints coincide with WAL piling up on disk,
cross-check the WAL directory size investigation — an undersized `max_wal_size` and WAL retention
often appear together.

Refer to this in user-facing output by name — "checkpoint / background writer" — never by an internal
catalog code. Catalog ids live only in this skill's frontmatter metadata.
