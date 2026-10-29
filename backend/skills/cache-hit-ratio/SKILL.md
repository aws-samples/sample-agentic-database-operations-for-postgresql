---
name: cache-hit-ratio
description: Assess buffer cache hit ratios — per table and per database — to find working sets that no longer fit in shared_buffers / instance memory. Activate on low cache hit ratio, rising read IOPS/latency, "queries got slower after data grew", or memory-pressure symptoms.
allowed-tools: postgres_agent observability_agent
metadata:
  surface: investigation
  check_ids: [CHK-019, CHK-032]
---

# Cache Hit Ratio Investigation

Find where reads are going to disk instead of the buffer cache. The shared diagnostic guardrails
apply; verdicts are already computed by the tools.

## When to use
Symptoms involving: a low cache hit ratio, rising `ReadIOPS`/`ReadLatency`, queries slowing as data
grew, or general memory pressure.

## Steps (which tool to call)
1. **Database-level — `get_buffer_pool_stats`** (via `postgres_agent`). Per database: `hit_ratio_percent`
   and a `status` (lower is worse — <99% WARNING, <95% CRITICAL). **Trust `status`.** This tells you
   whether the whole database is cache-cold.
2. **Per-table — `get_io_statistics`** (via `postgres_agent`). Per hot table: `table_cache_hit_ratio`,
   `index_cache_hit_ratio`, and a `status` (only judged once meaningful disk reads have occurred — a
   ratio computed from a handful of reads is noise). Identifies which tables drive the misses.
3. **Corroborate with memory/IO — `observability_agent`.** A genuinely low ratio shows up as high
   CloudWatch `ReadIOPS`/`ReadLatency` and low `FreeableMemory`. If those are flat, the low ratio is
   not currently hurting — say so rather than asserting an I/O problem you did not measure.

## Output
Report the database ratio and the top offending tables with their ratios and verdict. Remediation is a
proposal and is usually about the working set, not a per-table tweak: the hot data no longer fits in
`shared_buffers` / instance RAM — options are a larger instance (more RAM), reducing the hot set, or
better indexing so fewer blocks are touched. **Do not cite `shared_buffers` values from memory** —
only what a config tool returned this run; Aurora manages buffer cache differently from RDS.

Refer to this in user-facing output by name — "cache hit ratio" — never by an internal catalog code.
Catalog ids live only in this skill's frontmatter metadata.
