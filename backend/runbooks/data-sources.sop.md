# Data Source Guidelines

## Overview
Rules for interpreting PostgreSQL data sources correctly. Misattributing cumulative
stats as current activity is the most dangerous failure mode for investigations.

## Data Source Classification

### pg_stat_activity — CURRENT live state
- Shows what sessions are doing RIGHT NOW (point-in-time snapshot)
- Trust this for: "what's happening now", "who is connected", "what queries are running"
- Fields: pid, state, query, client_addr, application_name, xact_start, wait_event
- MUST be the primary source for current state findings
- Filter: state='active' to see executing queries

### pg_stat_statements — CUMULATIVE lifetime stats
- Contains ALL queries ever run since stats were last reset
- Includes queries from previous load tests, batch jobs, maintenance, past workloads
- Trust this for: "what queries run most over time", "which queries are slowest on average"
- MUST NOT be used to determine what is running NOW
- MUST NOT present cumulative call counts or total_time as current activity volume
- Only use for: historical trends, query performance comparison across time

### pg_stat_database — CUMULATIVE database-level stats
- Shows aggregate database stats (transactions, tuple operations, temp files)
- Trust this for: workload classification (read-heavy vs write-heavy), rollback ratios
- Same rules as pg_stat_statements — cumulative, not current

### pg_stat_user_tables — CUMULATIVE table stats
- Shows dead tuples, vacuum counts, analyze counts per table
- Trust this for: maintenance health, bloat detection
- n_dead_tup is current count; vacuum_count/autovacuum_count are cumulative

### Lock analysis tools — CURRENT locks
- Shows active locks and blocking chains RIGHT NOW
- Trust this for: current lock contention, blocked queries

### Reports (analysis.json) — HISTORICAL snapshots
- Point-in-time snapshots from when the scan ran (may be hours/days old)
- Trust this for: "what was the state at scan time", trend comparison
- MUST NOT present as current state

## How to Identify Current Workload

CORRECT approach:
"[pg_stat_activity] 80 active sessions running SELECT abalance FROM pgbench_accounts
— this is the current CPU driver"

WRONG approach:
"pg_stat_statements shows 33.4M UPDATE calls on pgbench_branches — this is causing CPU"
(these are cumulative from ALL past runs, not current activity)

To determine the current workload type:
1. Look at pg_stat_activity WHERE state='active' — what queries are actually executing NOW
2. If you see only SELECT queries active, it's a read workload — even if pg_stat_statements
   shows millions of historical UPDATEs from previous test runs
3. If you see UPDATE/INSERT/DELETE queries active, it's a write workload

## Section Rules for pg_stat_statements (CRITICAL)

pg_stat_statements data MUST NOT appear in FINDINGS or ROOT CAUSE sections.
These sections MUST be built EXCLUSIVELY from:
- pg_stat_activity (current sessions)
- CloudWatch metrics
- Performance Insights
- CloudTrail events
- Lock analysis tools

pg_stat_statements MAY ONLY appear in RECOMMENDED ACTIONS as historical context.
Example: "pg_stat_statements shows this query has historically high execution time —
consider adding an index."

This rule exists because pg_stat_statements contains cumulative totals from ALL past
workloads. Including "33M UPDATE calls" in findings when the current workload is
SELECT-only is factually wrong and misleads operators.

## Labeling Requirement (MANDATORY)

Every data point in FINDINGS MUST include its source tag:
- [pg_stat_activity] for live snapshot data
- [pg_stat_statements] for cumulative historical data (ONLY in Recommendations)
- [pg_stat_database] for cumulative database stats
- [CloudWatch] for metrics
- [Performance Insights] for PI data
- [CloudTrail] for API events

## Conflict Resolution
If live data contradicts report data or cumulative stats, LIVE DATA WINS.
Say so explicitly: "The previous report showed X, but current live state shows Y."

If pg_stat_activity shows SELECT-only workload but pg_stat_statements shows UPDATEs,
the UPDATEs are from PREVIOUS runs. Report the current workload as SELECT-only.
