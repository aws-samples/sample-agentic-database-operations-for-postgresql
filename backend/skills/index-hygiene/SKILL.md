---
name: index-hygiene
description: Find indexes that waste resources — unused, rarely-used, oversized, or true duplicates — as REVIEW CANDIDATES for cleanup. Activate on unused indexes, redundant/duplicate indexes, index bloat/cleanup, "which indexes can I drop", or write amplification from too many indexes.
allowed-tools: postgres_agent get_historical_metrics
metadata:
  surface: both
  check_ids: [CHK-008, CHK-012]
---

# Index Hygiene Investigation

Surface indexes that cost writes/storage without paying their way — as **review candidates**, never
automatic drops. The shared diagnostic guardrails apply; verdicts are already computed by the tools.

## When to use
Symptoms involving: unused or rarely-used indexes, duplicate/redundant indexes, index-driven write
amplification, or "which indexes can we remove". (For an invalid index that needs REINDEX, use the
invalid-indexes investigation — this one treats invalid as a CRITICAL cross-reference.)

## Steps (which tool to call)
1. **Unused / rarely-used / large — `get_index_statistics`** (via `postgres_agent`). Per index it
   returns `schemaname.tablename`, `indexname`, `index_size`, `scans`, `category`, and a `status`
   already applied from the references (invalid = CRITICAL; unused > band = CRITICAL else NOTICE;
   rarely-used and large = WARNING). **Trust `status`.** PRIMARY KEY and UNIQUE indexes are excluded
   from review candidates — they enforce constraints regardless of scan count.
2. **Duplicates — `get_duplicate_indexes`** (via `postgres_agent`). Returns TRUE duplicate pairs only
   (identical columns AND opclass AND collation AND access method AND predicate AND expression), with
   `combined_bytes`, `involves_primary_key`, `involves_unique`, and a `status` (CRITICAL when the pair
   is large). Report the redundant one as a review candidate.
3. **Trust gate BEFORE recommending any drop (mandatory).** Index usage stats are per-instance and
   cumulative since the last stats reset. Do NOT recommend dropping an index unless: (a) `stats_reset`
   is old enough to trust (ideally > 30 days) — if reset recently, say the data is INCONCLUSIVE;
   (b) usage was checked on the writer AND every read replica (an index unused on the writer may be
   used by a reader); and (c) no seasonal/monthly/quarterly job depends on it.
   On a cluster, SQL reaches the member the operator chose in the Endpoint menu of the database
   picker ("Cluster", the default, is the writer; see [ROLE FOCUS] / [MEMBERS] in the scope). To
   check a reader, report the writer's figures as the writer's and ask the operator to choose that
   reader in the Endpoint menu and ask again. Do not probe the connection to find out where it landed.
4. **Never drop a constraint-backing index.** For a duplicate, verify the redundant index does NOT
   back a UNIQUE or EXCLUSION constraint and is not the table's REPLICA IDENTITY (`involves_unique` /
   `involves_primary_key` flag the common cases; confirm EXCLUSION/REPLICA IDENTITY manually).

## Output
Report each candidate schema-qualified as `schema.index` on `schema.table`, with size, scans, and
verdict — phrased as a **review candidate** with the owner decision, never a bare "drop it".
Remediation is a proposal: `DROP INDEX CONCURRENTLY <schema>.<index>;` (online) after the trust gate
passes. Note that recreating a large index later is expensive (hours; a plain CREATE INDEX locks
writes — use CONCURRENTLY). Unused indexes still cost a write + WAL on every row change, so cleanup
reduces write amplification.

Refer to these in user-facing output by name — "unused/duplicate indexes", "index hygiene" — never by
an internal catalog code. Catalog ids live only in this skill's frontmatter metadata.
