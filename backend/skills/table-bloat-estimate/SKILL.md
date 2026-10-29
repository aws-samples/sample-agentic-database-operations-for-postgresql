---
name: table-bloat-estimate
description: Estimate physical table bloat (wasted on-disk space) to find pg_repack / VACUUM FULL candidates, without scanning table data. Activate on table bloat, wasted disk space, reclaim on-disk space, pg_repack, or VACUUM FULL candidates. (For dead-tuple/autovacuum effectiveness use the vacuum-and-bloat investigation.)
allowed-tools: postgres_agent
metadata:
  surface: investigation
  check_ids: [CHK-011]
---

# Estimated Table Bloat Investigation

Estimate physical bloat cheaply to find tables worth repacking — then confirm before acting. The
shared diagnostic guardrails apply; the verdict is already computed by the tool.

## When to use
Symptoms involving: wasted on-disk space, reclaiming storage, pg_repack / VACUUM FULL candidates. This
is about PHYSICAL bloat (shrinking the file). For "is autovacuum keeping up with dead tuples", use the
vacuum-and-bloat investigation instead.

## Steps (which tool to call)
1. **Estimate — `get_bloat_analysis`** (via `postgres_agent`, default `method=stats`). Per table it
   returns `total_size`, `bloat_percent` (estimate), and a `bloat_status` from the references bands
   (NOTICE 10%, WARNING 30%, CRITICAL 50%). **Trust `bloat_status`.** This is a no-scan estimate — fast
   but approximate.
2. **Confirm before remediation — `get_bloat_analysis` with `method=pgstattuple`.** For a table the
   estimate flags high, get the accurate figure. If `pgstattuple` is not installed, say the exact bloat
   is unconfirmed and do NOT recommend a disruptive rewrite on an estimate alone.
3. **Root cause.** Growing bloat usually means autovacuum is not keeping up — cross-check
   `get_vacuum_stats_health`. Repacking without fixing the cause just delays the problem.

## Output
Report each candidate schema-qualified with `total_size`, `bloat_percent`, and verdict. Remediation is
a proposal, and be precise: plain VACUUM frees space for REUSE but does NOT shrink the on-disk file.
To reduce on-disk size, prefer `pg_repack` (online, cautioned last resort) over `VACUUM FULL` (takes an
ACCESS EXCLUSIVE lock that blocks all queries, needs ~2x disk, and rebuilds indexes) — and confirm the
exact bloat with pgstattuple first.

Refer to this in user-facing output by name — "estimated table bloat" — never by an internal catalog
code. Catalog ids live only in this skill's frontmatter metadata.
