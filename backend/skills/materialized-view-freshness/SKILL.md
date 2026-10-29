---
name: materialized-view-freshness
description: Assess materialized views — whether they are populated, whether the planner has statistics for them, and whether they can be refreshed CONCURRENTLY. Activate on stale materialized view, matview not refreshed, unpopulated matview, REFRESH MATERIALIZED VIEW, or a view serving outdated data.
allowed-tools: postgres_agent observability_agent
metadata:
  surface: investigation
  check_ids: [CHK-006]
---

# Materialized View Freshness Investigation

Assess materialized views for the failure modes that actually matter. The shared diagnostic guardrails
apply; the state verdict is already computed by the primary tool.

## When to use
Symptoms involving: a stale or unpopulated materialized view, a matview serving outdated data, refresh
that seems not to run, or `REFRESH MATERIALIZED VIEW` questions.

## Steps (which tool to call)
1. **Primary — `get_materialized_view_freshness`** (via `postgres_agent`). Per matview it returns
   `ispopulated`, `size`, `est_rows`, `last_analyze`/`last_autoanalyze`, `has_unique_index`, and a
   `status`. **Trust `status`**: `ispopulated = false` is CRITICAL (queries against it error);
   never-analyzed or no-unique-index is WARNING.
2. **Do NOT equate ANALYZE recency with refresh recency.** `last_analyze`/`last_autoanalyze` is a
   planner-STATS proxy only — `REFRESH` does not update statistics and autoanalyze rarely fires on
   matviews. A matview can be freshly refreshed but show old analyze timestamps, or vice versa. Never
   report "stale data" purely from analyze timestamps.
3. **True refresh recency — `observability_agent`.** The only real freshness signal is a
   `REFRESH MATERIALIZED VIEW` line in CloudWatch Logs. If log export / `log_statement` capture is not
   confirmed, report refresh recency as INCONCLUSIVE (a monitoring gap) — do not conclude "not
   refreshed" from missing logs.
4. **Refresh in progress? — `get_current_activity`** (via `postgres_agent`). A non-CONCURRENT REFRESH
   takes an AccessExclusiveLock and blocks readers; a running one explains transient query blocking.

## Output
Report each flagged matview schema-qualified as `schema.matview`, with `ispopulated`,
`has_unique_index`, size, and the verdict. Remediation is a proposal:
- **Unpopulated:** `REFRESH MATERIALIZED VIEW <schema>.<matview>;` (first populate errors-out queries
  until done).
- **No unique index:** add a unique index so future refreshes can use
  `REFRESH MATERIALIZED VIEW CONCURRENTLY` (which does not block readers).
- **Never analyzed:** `ANALYZE <schema>.<matview>;` to give the planner stats.
Note that REFRESH is a writer-side heap rewrite (spikes WriteIOPS/WAL and, on Aurora, VolumeBytesUsed).
Use each matview's OWN schema in the DDL.

Refer to this in user-facing output by name — "materialized view freshness" — never by an internal
catalog code. Catalog ids live only in this skill's frontmatter metadata.
