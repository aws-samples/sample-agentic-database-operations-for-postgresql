---
name: stale-statistics
description: Find tables whose planner statistics are stale — ANALYZE has not kept up, so the query planner works from outdated row-distribution estimates and picks bad plans (wrong join order, wrong index, wrong scan). Activate on stale statistics, bad/slow plans after a bulk load or migration, ANALYZE overdue, or "the planner is choosing a bad plan".
allowed-tools: postgres_agent get_historical_metrics
metadata:
  surface: both
  check_ids: [CHK-050]
---

# Stale Statistics Investigation

Find large tables whose planner statistics are out of date. The shared diagnostic guardrails apply;
the severity is already computed by the primary tool.

## When to use
Symptoms involving: bad or suddenly-worse query plans (especially after a bulk load or migration), a
planner choosing the wrong index/join, or a suspicion that `ANALYZE` has fallen behind. This is about
PLANNER STATISTICS — not vacuum/bloat and not vacuum freeze.

## Steps (which tool to call)
1. **Primary — `get_stale_statistics`** (via `postgres_agent`). For tables > 10 MB it returns
   `schemaname`, `relname`, `last_analyze`/`last_autoanalyze`, `days_since_analyze`, `n_live_tup`,
   `n_mod_since_analyze`, `mod_pct`, and a `status` (worst of the days-since-analyze band and the
   modified-row-% band). **Trust `status`.** A never-analyzed table shows a very large
   `days_since_analyze` and lands CRITICAL — that is expected, not a bug.
2. **Distinguish from vacuum.** Stale statistics is separate from dead-tuple bloat: a table can be
   vacuumed regularly yet never analyzed. Do not conflate this with the bloat/vacuum investigation —
   the fix here is ANALYZE, not VACUUM.
3. **Correlate with a load event (optional) — `get_historical_metrics`.** A high `n_mod_since_analyze`
   right after a bulk load or migration is the classic cause; autoanalyze simply has not caught up
   yet. Do not extrapolate a trend from a single point.

## Output
Report each stale table schema-qualified as `schema.table`, with `days_since_analyze`, `mod_pct`, and
`status`. Remediation is a proposal: `ANALYZE <schema>.<table>;` (fast, takes only a
`SHARE UPDATE EXCLUSIVE` lock — does not block reads/writes). For tables that go stale repeatedly on
heavy write volume, suggest a lower per-table `autovacuum_analyze_scale_factor` via
`ALTER TABLE <schema>.<table> SET (...)` (a per-table reloption, not a parameter-group change). Use
each table's OWN schema in the DDL.

Refer to this in user-facing output by name — "stale statistics" — never by an internal catalog code.
Catalog ids live only in this skill's frontmatter metadata.
