---
name: oversized-datatypes
description: Identify columns using potentially oversized or imprecise data types (bigint where integer fits, unbounded numeric, double precision, unbounded varchar) as advisory review candidates. Activate on data type sizing, column type optimization, storage-per-row reduction, or schema/type review.
allowed-tools: postgres_agent
metadata:
  surface: both
  check_ids: [CHK-014]
---

# Oversized Data Type Review

Surface columns whose types MAY be larger/looser than needed — as advisory review items only. The
shared diagnostic guardrails apply.

## When to use
Symptoms involving: data-type sizing, storage-per-row reduction, or a schema/type design review. This
is advisory — never actionable without verifying the actual value range.

## Steps (which tool to call)
1. **Primary — `get_oversized_datatypes`** (via `postgres_agent`). Returns candidate columns
   schema-qualified (`table_schema`, `table_name`, `column_name`, `current_type`, `table_size`) and a
   collection verdict `oversized_types_status` (NOTICE when any exist). Rows are ordered by table size
   — focus on the largest tables.
2. **Apply the correct guidance per type (this is where accuracy matters):**
   - `text` / unbounded `varchar`: **no-op for performance** — `text` and `varchar` are stored
     identically in PostgreSQL. Add `varchar(n)` ONLY for explicit length validation; it does not save
     space or time. Do NOT recommend it for performance.
   - `bigint` → `integer`: only if values fit AND will not grow. **Never** downsize a primary key /
     identity / sequence-backed column (overflow / sequence exhaustion risk).
   - unbounded `numeric`/`decimal`: adding precision/scale removes variable-storage + compute overhead.
   - `double precision` → `real`: only if reduced precision is acceptable (silent precision loss otherwise).

## Output
Report candidates schema-qualified with `current_type` and `table_size`, as review items. Every
remediation is a proposal AND a caution: `ALTER TABLE ... ALTER COLUMN ... TYPE ...` takes an ACCESS
EXCLUSIVE lock and rewrites the whole table — expensive on large tables, and not worth doing without
confirming the real value range first. Prioritize only large tables with a clear win.

Refer to this in user-facing output by name — "oversized data types" — never by an internal catalog
code. Catalog ids live only in this skill's frontmatter metadata.
