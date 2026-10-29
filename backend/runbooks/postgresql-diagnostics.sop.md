# PostgreSQL Diagnostic Knowledge Base

## Overview
Domain knowledge for interpreting PostgreSQL diagnostic data: reporting-accuracy rules,
threshold references, capacity checks, and reasoning anti-patterns.

Symptom-specific investigation procedures (how to investigate high CPU, table bloat,
lock contention, connection issues, XID wraparound, TOAST bloat, etc.) are NOT listed
here. They are delivered separately as loadable diagnostic **skills** — each skill holds
the step-by-step procedure, the tools to call, and the interpretation rules for one
symptom. Identify the symptom, then load the matching skill and follow it. This document
holds only the check-agnostic discipline every investigation shares.

## Accuracy Guidelines

### Answer the Question
If the user asks "why is CPU high?", finding #1 MUST explain what is causing CPU —
even if it's an expected workload like a load test. The user asked, so tell them.

### Report Peak Values
When reporting metric spikes (CPU, IOPS, connections), MUST report the PEAK value
during the incident window, not the period average.

### Identify the Source
Extract client_addr and application_name from pg_stat_activity for EVERY session-based
finding. This is MANDATORY — never report session counts or PIDs without their source.
Report format: "[count] sessions from [client_addr] (app: [application_name]) doing [action]"
Example: "20 connections from 10.0.1.50 (app: pgbench) running UPDATE pgbench_accounts"
If multiple source IPs are involved, group by IP and report each group separately.
If client_addr is NULL, report as "local connection (unix socket)" — do NOT omit it.

### Separate Trigger from Pre-existing
The active workload causing the user's complaint is the primary finding.
Pre-existing issues (old bloat, suboptimal params) go under Improvements.

### Verify PIDs Are Live
Before claiming sessions are "idle in transaction for X hours", confirm those PIDs
exist in current pg_stat_activity. Never present stale data as current findings.

### Exclude Monitoring Overhead
aurora_stat_plans(), pg_stat_statements queries, and diagnostic tool calls are
observation overhead — NOT production issues. Note separately if significant.

### Check Held Snapshots
If autovacuum ran but dead tuples remain, check for long-running transactions
holding snapshots (old xact_start or backend_xmin).

### Correlate Timing
If a parameter was set weeks ago but the spike started 10 minutes ago,
the parameter is NOT the trigger.

### Focus
Don't pad with unrelated tuning advice. Pre-existing config issues get ONE bullet
under Improvements.

## Threshold Reference
| Metric | OK | WARNING | CRITICAL |
|--------|-----|---------|----------|
| CPU Utilization | <70% | 70-85% | >85% |
| Dead Tuple Ratio (size-weighted) | <10% | small >20% · large >10% · v.large >5% | small >50% · large >20% · v.large >10% |
| Cache Hit Ratio | >95% | 90-95% | <90% |
| Rollback Ratio | <2% | 2-5% | >5% |
| XID Age (% of wraparound) | <50% | 50-75% | >75% |
| Replication Lag | <1s | 1-10s | >10s |

## Instance Size Verification
When reporting capacity metrics (AAS vs vCPU):
1. Get the actual instance class from the database record or RDS API
2. Map instance class to vCPU count — do NOT guess from AAS numbers:
   - db.r6g.large = 2 vCPU, db.r6g.xlarge = 4, db.r6g.2xlarge = 8
   - db.r6g.4xlarge = 16, db.r6g.8xlarge = 32, db.r6g.12xlarge = 48
   - db.r6g.16xlarge = 64, db.r6i/r7g follow same pattern
   - db.serverless = ACU-based (0.5 ACU ≈ 1 vCPU)
3. Report the ratio precisely: "Database load is X AAS on Y vCPU instance (Z× capacity)"
4. If instance class is unknown, state "instance class not confirmed" rather than guessing

## Engine-Specific Rules
Parameter recommendations and architecture assumptions differ by engine.
Refer to the engine-specific runbook loaded alongside this file:
- Aurora PostgreSQL: aurora-postgresql.sop.md
- RDS PostgreSQL: rds-postgresql.sop.md

Key reminder: ALWAYS check the engine type before recommending parameter changes.

## Anti-Patterns (NEVER do this)

### Correlation ≠ Causation
Log errors that occur during a CPU spike are NOT automatically the cause.
A failed query consumes near-zero CPU. Before claiming errors cause CPU spikes:
- Quantify: how many errors per second? Is that enough to drive 97% CPU?
- Verify: would stopping the errors reduce CPU?
- Check: does the primary workload (by session count and throughput) already explain
  the CPU independently? If yes, the errors are coincidental, not causal.

### Per-Query AAS vs Aggregate Load
If Performance Insights shows 80 sessions each at 0.005 AAS, the individual numbers
look tiny. But 80 clients cycling through queries at thousands of TPS saturate CPU
through THROUGHPUT, not individual query weight. High throughput of lightweight queries
IS the CPU cause — don't dismiss it because per-query AAS is low.

### Don't Dismiss the Obvious
If 80 pgbench/application clients are connected and CPU is at 97%, the connected
workload is almost certainly the cause. Don't look for exotic explanations (log errors,
monitoring tools, configuration drift) when the simple explanation fits:
- 80 clients × fast queries × max throughput = CPU saturation
- This is the root cause. Say so directly.

### Don't Invent Mechanisms
Never claim "connection churn", "retry storms", or "cascading failures" without
concrete evidence (e.g., connection count oscillating rapidly in CloudWatch, error
rates in the thousands per second). A few log errors do not constitute a storm.

### Instance Awareness
Before recommending instance scaling or estimating CPU oversubscription, look up the
actual instance type via the database metadata or describe-db-instances. Never estimate
or guess vCPU count — report the actual value. Wrong vCPU estimates lead to inflated
oversubscription claims and incorrect scaling recommendations.
