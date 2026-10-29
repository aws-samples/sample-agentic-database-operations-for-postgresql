---
name: storage-capacity
description: Understand where storage is going and whether capacity is at risk — database sizes, largest tables, and their heap-vs-index-vs-dead-row composition. Activate on storage growth, running out of disk, largest tables, over-indexed tables, or capacity planning.
allowed-tools: postgres_agent observability_agent get_historical_metrics
metadata:
  surface: investigation
  check_ids: [CHK-026, CHK-027]
---

# Storage & Capacity Investigation

Explain storage consumption and flag capacity risk and composition problems. The shared diagnostic
guardrails apply; the composition verdict is already computed by the tool.

## When to use
Symptoms involving: storage growth, "are we running out of disk", the largest tables, over-indexed
tables, or capacity planning.

## Steps (which tool to call)
1. **Sizes + composition — `get_storage_analysis`** (via `postgres_agent`). Returns per-database sizes
   (`databases`) and the largest tables (`top_tables`) with `total_size`, `table_size` (heap),
   `index_size`, `live_rows`, `dead_rows`, and a per-table `status` (over-indexed: index >2x heap
   WARNING / >5x CRITICAL; dead rows >20% WARNING / >50% on a huge table CRITICAL). **Trust `status`.**
2. **Capacity risk is observability-led, not SQL.** "Running out of space" cannot be judged from SQL
   sizes alone — lead with CloudWatch `FreeStorageSpace` (RDS; Aurora auto-scales): <10% remaining is
   CRITICAL. Use `observability_agent`. If the signal is unavailable, say so.
3. **Growth — `get_historical_metrics`.** For "how fast is it growing / when will it fill up", use the
   trend; do NOT extrapolate from a single size reading.
4. **Root-cause a composition finding.** An over-indexed table → cross-check index-hygiene; a high
   dead-row % → cross-check vacuum-and-bloat. Storage is a symptom; the cause is elsewhere.

## Output
Report database sizes and flagged tables schema-qualified with composition (heap/index/dead) and the
verdict. For capacity, report the CloudWatch FreeStorageSpace reading and headroom. Remediation is a
proposal and points at the root cause (drop unused indexes / repack a bloated table / archive an
unused table), not just "add storage".

Refer to this in user-facing output by name — "storage & capacity" — never by an internal catalog
code. Catalog ids live only in this skill's frontmatter metadata.
