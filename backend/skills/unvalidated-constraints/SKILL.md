---
name: unvalidated-constraints
description: Find constraints added with NOT VALID that were never validated — they enforce rules on new rows only, so existing rows may violate them and the planner cannot use unvalidated foreign keys for optimizations. Activate on unvalidated constraint, NOT VALID, VALIDATE CONSTRAINT, incomplete migration, or data-integrity gap.
allowed-tools: postgres_agent
metadata:
  surface: both
  check_ids: [CHK-005]
---

# Unvalidated Constraint Investigation

Find constraints stuck in the `NOT VALID` state. The shared diagnostic guardrails apply; the
collection verdict is already computed by the primary tool.

## When to use
Symptoms involving: an unvalidated constraint, a constraint added `NOT VALID`, an incomplete
migration, or doubt about data integrity on historical rows.

## Steps (which tool to call)
1. **Primary — `get_constraint_validation`** (via `postgres_agent`). Returns each unvalidated
   constraint schema-qualified (`schema_name`, `table_name`, `constraint_name`, `constraint_type`,
   `is_validated`, `definition`) plus the collection verdict `unvalidated_constraints_status`.
   **Trust the status**: no rows = OK; any row = WARNING (integrity is not guaranteed for existing
   rows until validated).
2. **Weigh escalation by type + table.** `constraint_type = 'f'` (foreign key) on a high-traffic or
   business-critical table is the CRITICAL case — an unvalidated FK also blocks certain planner
   optimizations. `'c'` (CHECK) is usually WARNING. State the type for each finding.

## Output
Report each unvalidated constraint as `schema.table` → `constraint_name` (`type`), with its
`definition`. Remediation is a proposal:
`ALTER TABLE <schema>.<table> VALIDATE CONSTRAINT <constraint_name>;` — this scans existing rows once
and takes only a `SHARE UPDATE EXCLUSIVE` lock (it does NOT block reads/writes), so it is safe to run
online. Warn that if existing rows actually violate the rule, VALIDATE will fail and surface the bad
data — that is the point. Use each constraint's OWN table/schema in the DDL.

Refer to this in user-facing output by name — "unvalidated constraints" — never by an internal
catalog code. Catalog ids live only in this skill's frontmatter metadata.
