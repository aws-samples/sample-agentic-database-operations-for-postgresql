---
name: trigger-inventory
description: Inventory user-defined triggers and flag high-overhead ones — FOR EACH ROW triggers firing on all DML on high-write tables, and disabled triggers. Activate on trigger inventory, trigger performance/overhead, disabled triggers, or "what triggers run on this table".
allowed-tools: postgres_agent
metadata:
  surface: both
  check_ids: [CHK-017]
---

# Trigger Inventory

Inventory user triggers and flag the ones that add write-path overhead. The shared diagnostic
guardrails apply; the verdict is already computed by the tool.

## When to use
Symptoms involving: a trigger inventory, trigger performance/overhead, disabled triggers, or "what
fires on this table when I write to it".

## Steps (which tool to call)
1. **Primary — `get_trigger_analysis`** (via `postgres_agent`). Per trigger it returns `table_name`,
   `trigger_name`, `timing` (BEFORE/AFTER/INSTEAD OF), `event`, `granularity` (FOR EACH ROW/STATEMENT),
   `function_name`, `status` (ENABLED/DISABLED/...), and an `assessment`. **Trust `assessment`**:
   WARNING = a FOR EACH ROW trigger firing on all DML (INSERT/UPDATE/DELETE) — per-row overhead on
   every write; NOTICE = a disabled trigger (dead weight or a latent surprise).
2. **Weigh the table's write volume.** Per-row trigger overhead only bites on high-write tables.
   Multiple FOR EACH ROW triggers on the same hot table compound write latency — call that out. On a
   low-write table the same trigger is usually fine.
3. **Confirm real overhead (optional) — `get_query_performance`.** If write latency is the complaint,
   check whether simple single-row DML shows elevated write time relative to rows changed (a sign of
   heavy per-row trigger/cascade work).

## Output
Report flagged triggers as `schema.table` → `trigger_name` (`timing` `event`, `granularity`), with the
`assessment` and `status`. Remediation is a proposal: consider consolidating multiple per-row triggers,
moving logic to a statement-level trigger or the application, or dropping a disabled trigger if it is
truly dead. Do not recommend disabling a trigger that enforces data integrity.

Refer to this in user-facing output by name — "triggers" — never by an internal catalog code. Catalog
ids live only in this skill's frontmatter metadata.
