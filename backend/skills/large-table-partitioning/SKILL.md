---
name: large-table-partitioning
description: Identify large unpartitioned tables that are candidates for partitioning. Activate on partitioning, very large tables, table growth planning, or "should I partition this table".
allowed-tools: postgres_agent get_historical_metrics
metadata:
  surface: investigation
  check_ids: [CHK-010]
---

# Large Table Partitioning Review

Surface large REGULAR (non-partitioned) tables as partitioning candidates. The shared diagnostic
guardrails apply; the size-banded verdict is already computed by the tool.

## When to use
Symptoms involving: a very large table, partitioning questions, or growth/capacity planning for a
table that keeps getting bigger.

## Steps (which tool to call)
1. **Primary — `get_partitioning_analysis`** (via `postgres_agent`). Per large table it returns
   `schema_name.table_name`, `size_gb`, `table_type` (REGULAR / PARTITIONED / PARTITION), and a
   size-banded `status` (REGULAR >50GB CRITICAL, >20GB WARNING, else NOTICE). Already-partitioned
   tables score OK. **Trust `status`.**
2. **Partitioning is a design decision, not a mechanical fix.** Recommend it only after considering
   the query patterns (does the workload filter on a natural partition key like a date range?) and the
   growth rate — check the trend with `get_historical_metrics`. A large table that is queried by
   primary key and not growing may not need partitioning at all.

## Output
Report each candidate as `schema.table` with `size_gb` and the verdict — as a candidate to evaluate,
not a mandate. Remediation is a proposal that is a significant migration: converting a large table to
partitioned requires a rewrite or a create-and-swap approach; call out that it is a planned project,
not a quick change, and that the right partition key comes from the query patterns.

Refer to this in user-facing output by name — "large-table partitioning" — never by an internal
catalog code. Catalog ids live only in this skill's frontmatter metadata.
