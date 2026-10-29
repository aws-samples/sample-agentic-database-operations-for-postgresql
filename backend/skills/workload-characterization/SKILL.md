---
name: workload-characterization
description: Characterize the database workload — the live query mix (read/write/DDL/COPY) and the long-term READ-HEAVY / WRITE-HEAVY / MIXED profile. Activate on "is this read or write heavy", workload profile, unexpected DDL during peak, a bulk COPY load, or workload trending.
allowed-tools: postgres_agent observability_agent
metadata:
  surface: investigation
  check_ids: [CHK-039, CHK-040]
---

# Workload Characterization Investigation

Describe what kind of work the database is doing — now and over time. The shared diagnostic guardrails
apply; verdicts are already computed by the tools. This is largely descriptive; treat it as context
for other investigations rather than a standalone alarm.

## When to use
Symptoms involving: "is this database read- or write-heavy", a workload profile request, unexpected
DDL during peak hours, a bulk COPY consuming I/O, or workload trending.

## Steps (which tool to call)
1. **Live mix — `get_workload_characterization`** (via `postgres_agent`). Breaks running queries into
   SELECT/INSERT/UPDATE/DELETE/COPY/DDL with active/idle counts and a `status` (active DDL = WARNING:
   schema change = lock-contention risk; active COPY = NOTICE: bulk-load I/O). **Trust `status`**, but
   the "is this the wrong time" judgment (DDL during peak) is yours — correlate with the clock and
   current load.
2. **Long-term profile — `get_workload_profile`** (via `postgres_agent`). Classifies READ-HEAVY /
   WRITE-HEAVY / MIXED from cumulative `pg_stat_database` with `read_pct`/`write_pct`, `rollback_pct`,
   `temp_files`, `stats_days`, and a `status`. **NO ACTIVITY on production is CRITICAL** (something is
   wrong). If `stats_days` is small the classification is unreliable — say so (INCONCLUSIVE), do not
   present a confident READ/WRITE label from a few hours of data.
3. **Corroborate — `observability_agent`.** A workload shift should show in CloudWatch CPU/WriteIOPS.

## Output
Report the live mix and the long-term profile (with read/write %), plus any active DDL/COPY and its
verdict. Remediation is usually a proposal to reschedule (run DDL/bulk loads off-peak) or to
investigate an unexpected shift — this check mostly informs the others (a WRITE-HEAVY profile changes
how you read bloat/checkpoint findings). Note the profile is cumulative since the last stats reset.

Refer to this in user-facing output by name — "workload characterization" — never by an internal
catalog code. Catalog ids live only in this skill's frontmatter metadata.
