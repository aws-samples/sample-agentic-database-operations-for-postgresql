---
name: toast-health
description: Assess TOAST tables (out-of-line storage for large text/jsonb/bytea columns) for xid-age and dead-tuple health. Activate on TOAST bloat, slow reads of large text/jsonb/bytea columns, TOAST vacuum, or a TOAST relation flagged large.
allowed-tools: postgres_agent observability_agent
metadata:
  surface: investigation
  check_ids: [CHK-009]
---

# TOAST Health Investigation

Assess TOAST relations for the failure modes that matter — without mistaking size for bloat. The
shared diagnostic guardrails apply; the verdict is already computed by the tool.

## When to use
Symptoms involving: slow reads of large `text`/`jsonb`/`bytea` columns, a TOAST relation flagged as
large, or TOAST vacuum/freeze questions.

## Steps (which tool to call)
1. **Primary — `get_toast_status`** (via `postgres_agent`). Per parent table it returns
   `main_table_size`, `toast_size`, `toast_xid_age`, `toast_dead_pct`, vacuum times, and a `status`
   (worst of the TOAST xid-age floors and the TOAST dead-tuple % bands). **Trust `status`.**
2. **A large TOAST is NORMAL — do not call it bloat.** Big `text`/`jsonb`/`bytea` values are stored
   out-of-line by design; a TOAST larger than the heap is expected for such tables and is not bloat by
   itself. Only `toast_dead_pct` or `toast_xid_age` indicate a problem.
3. **Confirm suspected physical bloat — pgstattuple.** For a large TOAST you suspect is bloated, the
   accurate confirmation is `pgstattuple` (see the bloat-estimate investigation). If it is not
   installed, say the physical bloat is unconfirmed.
4. **TOAST vacuums independently.** If `toast_dead_pct` is high but the parent looks fine, the TOAST
   relation needs vacuum — check that the parent's autovacuum is reaching it (`get_vacuum_stats_health`).

## Output
Report each flagged TOAST as `schema.table` (TOAST `toast_table_name`), with `toast_size`,
`toast_dead_pct`, `toast_xid_age`, and the verdict. Remediation is a proposal: VACUUM the parent table
(which vacuums its TOAST) if dead %/xid age is high; for confirmed physical bloat on a very large
TOAST, pg_repack the parent (cautioned). Tag each finding with its source (`[pg_class/pg_stat]`).

Refer to this in user-facing output by name — "TOAST health" — never by an internal catalog code.
Catalog ids live only in this skill's frontmatter metadata.
