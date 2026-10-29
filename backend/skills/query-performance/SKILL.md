---
name: query-performance
description: Find the most expensive SQL and functions — slow queries by mean time, queries spilling sorts/hashes to disk (work_mem pressure), and hot user-defined functions. Activate on slow queries, high database load, "what's the most expensive query", temp file spills, work_mem, or slow functions/procedures.
allowed-tools: postgres_agent observability_agent
metadata:
  surface: investigation
  check_ids: [CHK-020, CHK-022, CHK-025]
---

# Query & Function Performance Investigation

Find the SQL and functions consuming the most database time. The shared diagnostic guardrails apply;
verdicts are already computed by the tools.

## When to use
Symptoms involving: slow queries, high DB load, "the most expensive query", temp-file spills /
work_mem pressure, or slow user-defined functions.

## Steps (which tool to call)
1. **Top queries — `get_query_performance`** (via `postgres_agent`). Returns top queries with `calls`,
   `mean_time_ms`, `cache_hit_ratio`, `temp_blocks`, and a `severity` (mean >=500ms WARNING / >=1000ms
   CRITICAL, plus a temp-spill or low-cache flag). **Trust `severity`.** The `source` field tells you
   whether it came from `pg_stat_statements` or the live-activity fallback — if fallback, say the
   ranking is limited (no pg_stat_statements).
2. **Temp spills — `get_temp_file_queries`** (via `postgres_agent`). Queries spilling sorts/hashes to
   disk, with `temp_blks_written` and a `severity` (>1000 WARNING, >10000 CRITICAL). A spill means the
   operation exceeded `work_mem`.
3. **Functions — `get_function_performance`** (via `postgres_agent`). Hot user-defined functions with
   `avg_time_ms`, `calls`, and a `status`. If `track_functions` is off, the tool says so — report that
   as a monitoring gap, not "no slow functions".
4. **Corroborate load — `observability_agent`** (Performance Insights top SQL / db load) when
   available.

## Output
Report the offending queries/functions with their metrics and verdict; include the `source` and any
prerequisite gap (pg_stat_statements / track_functions off). Remediation is a proposal: index or
rewrite the slow query (confirm the plan first); for temp spills raise `work_mem` **at the session or
role scope** (it is per-operation memory — a high global value multiplies across connections), not
globally; for a hot function, optimize its body or reduce call volume. Aurora/RDS `work_mem` defaults
change via the parameter group — cite only fetched values.

Refer to these in user-facing output by name — "expensive queries", "temp spills", "function
performance" — never by an internal catalog code. Catalog ids live only in this skill's frontmatter.
