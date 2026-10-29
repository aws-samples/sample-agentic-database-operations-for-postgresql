---
name: unused-tables
description: Identify tables with zero recorded reads on this instance as review candidates for lifecycle/archival assessment by owners. Activate on unused tables, dead tables, archival candidates, table cleanup, or reclaiming space from tables nobody queries.
allowed-tools: postgres_agent
metadata:
  surface: both
  check_ids: [CHK-015]
---

# Unused Table Review

Surface tables with no recorded reads — as owner-driven review candidates, never automatic drops. The
shared diagnostic guardrails apply; the review priority is already computed by the tool.

## When to use
Symptoms involving: unused/dead tables, archival candidates, or reclaiming space from tables nobody
reads. This is a lifecycle decision for owners — the agent never drops a table.

## Steps (which tool to call)
1. **Primary — `get_unused_tables`** (via `postgres_agent`). Returns tables with zero scans
   schema-qualified (`schemaname.table_name`), `total_size`, `estimated_rows`, `writes_since_reset`,
   last vacuum/analyze, and a size-banded `status` (>100MB = WARNING review priority, else NOTICE).
   There is deliberately no CRITICAL tier — this is never an automated action.
2. **Reads are per-instance — do not conclude "unused" from the writer alone.** An index/table
   unused on the writer may be read on a replica. Recommend confirming across the writer AND every
   read replica before archiving. On a cluster, SQL reaches the member the operator chose in the
   Endpoint menu of the database picker ("Cluster", the default, is the writer; see [ROLE FOCUS] /
   [MEMBERS] in the scope): report the writer's figures as the writer's and ask the operator to
   choose the reader in the Endpoint menu and ask again. Do not probe the connection to find out where it landed.
3. **Check `writes_since_reset`.** A table with zero reads but active writes is likely append-only
   (an audit/event log written here, read elsewhere or later) — that is NOT dead. Only a table with
   zero reads AND negligible writes is a genuine archival candidate.
4. **Trust gate.** These stats are cumulative since the last stats reset; require `stats_reset` older
   than ~30 days before treating "zero reads" as meaningful. If reset recently, say INCONCLUSIVE.

## Output
Report each candidate schema-qualified with size and `writes_since_reset`, phrased as a review
candidate with an owner decision — never "drop this table". If archival is pursued, the safe path is a
proposal: rename/move or `pg_dump` the table first, confirm nothing breaks, then drop in a later
window. Seasonal/quarterly access patterns may not appear in the stats window.

Refer to this in user-facing output by name — "unused tables" — never by an internal catalog code.
Catalog ids live only in this skill's frontmatter metadata.
