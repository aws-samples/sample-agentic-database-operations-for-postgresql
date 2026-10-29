---
name: missing-indexes
description: Find where an index is probably MISSING — foreign keys with no supporting index, and tables read by selective sequential scans. Activate on slow joins, slow CASCADE delete/update, "should I add an index", missing FK index, or heavy sequential scans on a large table.
allowed-tools: postgres_agent get_historical_metrics
metadata:
  surface: investigation
  check_ids: [CHK-016, CHK-018]
---

# Missing Index Investigation

Find places where adding an index is likely to help — without recommending one blindly. The shared
diagnostic guardrails apply; verdicts are already computed by the tools.

## When to use
Symptoms involving: slow JOINs or `ON DELETE/UPDATE CASCADE`, a foreign key with no index, "should I
add an index here", or a large table dominated by sequential scans.

## Steps (which tool to call)
1. **Foreign keys without an index — `get_fk_without_indexes`** (via `postgres_agent`). Per FK it
   returns `table_name`, `constraint_name`, a `recommended_index` (`CREATE INDEX ...`), `table_size`,
   and a size-banded `status` (>1GB CRITICAL, >100MB WARNING, else NOTICE). **Trust `status`.** A
   missing FK index makes joins and CASCADE operations scan the child table and can hold locks on the
   parent — the bigger the child table, the worse.
2. **Selective sequential scans — `get_sequential_scan_tables`** (via `postgres_agent`). Returns
   `table_name`, `table_size`, `seq_scan`, `idx_scan`, `avg_rows_per_seq_scan`, `est_rows`, and a
   `status`. **WARNING = selective scans** (each scan reads a small fraction of the table) — an index
   MAY help. **NOTICE = full-table reads** — often intentional (reporting, ETL, append-only) and NOT
   a problem. Do not recommend an index for NOTICE rows.
3. **Confirm the query pattern BEFORE recommending an index (mandatory for the sequential-scan case).**
   A selective sequential scan is a *candidate*, not a conclusion. Confirm the actual query shape via
   `get_query_performance` (pg_stat_statements) — the right index depends on the predicate, not on
   scan counts. If `pg_stat_statements` is not installed, say the recommendation is unconfirmed
   (prerequisite not met), not a certainty.

## Output
Report each candidate schema-qualified. For a missing FK index, give the `recommended_index` DDL. For
a selective-scan table, describe the likely index only after confirming the query pattern; otherwise
present it as "candidate — confirm query shape first". Remediation is a proposal: create the index
`CONCURRENTLY` (`CREATE INDEX CONCURRENTLY ...`) to avoid locking writes; note that index creation on
a large table is time- and IO-intensive. Use each table's OWN schema in the DDL.

Refer to these in user-facing output by name — "missing FK index", "sequential scans" — never by an
internal catalog code. Catalog ids live only in this skill's frontmatter metadata.
