---
name: invalid-indexes
description: Find invalid or not-ready indexes — indexes left behind by a failed CREATE INDEX CONCURRENTLY, an interrupted build, or a crash/failover. They cost storage and write I/O on every change but the planner ignores them. Activate on invalid index, orphaned index, REINDEX, "index not ready", or index cleanup after a deployment/failover.
allowed-tools: postgres_agent observability_agent get_storage_analysis
metadata:
  surface: both
  check_ids: [CHK-003]
---

# Invalid / Orphaned Index Investigation

Find indexes that PostgreSQL has marked `NOT indisvalid` or `NOT indisready`. The shared diagnostic
guardrails apply to every finding; the collection verdict is already computed by the primary tool.

## When to use
Symptoms involving: an invalid or orphaned index, a failed/interrupted `CREATE INDEX CONCURRENTLY`,
"index not ready", or a post-deployment / post-failover cleanup.

## Steps (which tool to call)
1. **Primary — `get_maintenance_status`** (via `postgres_agent`). Read `invalid_indexes` (schema-
   qualified `schema_name`, `index_name`, `table_name`, `indisvalid`, `indisready`) and the
   collection verdict `invalid_indexes_status`. **Trust `invalid_indexes_status`**: no rows = OK;
   any row = WARNING (there is something to clean up).
2. **Weigh escalation.** Escalate to CRITICAL only when there are multiple invalid indexes, or one
   sits on a large / high-write table (each wasted write and WAL record hurts more there). The tool
   returns names, not sizes — use `get_storage_analysis` (via `postgres_agent`) to check the table's
   size before calling a single invalid index CRITICAL. Never guess a table's size.
3. **Rebuild in progress vs. abandoned — `get_current_activity`** (via `postgres_agent`). A `CREATE
   INDEX CONCURRENTLY` that is still running will show as active; an invalid index with no active
   build is abandoned and safe to drop/rebuild.
4. **Provenance (optional) — `observability_agent`.** CloudTrail deployment/failover events (up to
   90 days) can explain when the invalid index appeared. If log export / PI is unavailable, say so —
   do not assert a cause you could not observe.

## Output
Report each invalid index schema-qualified as `schema.index` on `schema.table`, with `indisvalid` /
`indisready`, and the verdict. Remediation is a proposal: rebuild with
`REINDEX INDEX CONCURRENTLY <schema>.<index>` (online) where supported, or
`DROP INDEX <schema>.<index>` then recreate. Use each index's OWN schema in the DDL. Note that an
invalid index provides zero read benefit while still costing writes/WAL, so cleanup is low-risk.

Refer to this in user-facing output by name — "invalid / orphaned indexes" — never by an internal
catalog code. Catalog ids live only in this skill's frontmatter metadata.
