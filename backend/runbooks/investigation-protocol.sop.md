# Investigation Protocol

## Overview
Standard operating procedure for database health investigations. This SOP defines
the investigation workflow, output format, and rules that MUST be followed.

## Temporal Mode (choose the leading data source)
Every investigation runs in one of three temporal modes (supplied in the
[DATABASE SCOPE] block / by the router). The mode decides which data source LEADS:
- **live** — an issue happening now. Current live state (Step 1) is the GROUND TRUTH / headline.
- **conjunction** — "what changed / is it growing": current live state PLUS the Tier 1 trend
  (get_historical_metrics). Lead with live, corroborate with the trend. A growth claim REQUIRES
  the trend data — never extrapolate a monthly figure from a single point.
- **historical** — investigating a PAST window (e.g., "last night's incident"). Live state is
  NOT the headline; reconstruct from Tier 1 metrics (get_historical_metrics), Tier 2 reports
  (get_report_analysis), and CloudWatch/CloudTrail history over the incident window. Report the
  current live reading only as "state now" context.
Default to **live** when the mode is unspecified.

## Step 1: Live Discovery (start here for live / conjunction — for historical, lead with Tier 1/Tier 2 history per Temporal Mode)

For a live/conjunction incident, current live state is your GROUND TRUTH; CloudWatch metrics provide context, not the headline. For a historical investigation, current state is context only — lead with the Tier 1/Tier 2 history over the incident window.

Delegate to specialist agents:
- postgres_agent: get_current_activity FIRST, then locks, wait events, maintenance status
- observability_agent: CloudWatch metrics, Performance Insights, CloudTrail

The agent MUST check:
1. Current activity (pg_stat_activity) — what is the database doing RIGHT NOW
   For EVERY active/blocking session, record client_addr and application_name.
   These MUST appear in the final FINDINGS section — never omit source attribution.
2. Lock analysis and wait events — any contention or blocking
3. Dead tuple status across all tables — maintenance health
4. Snapshot holders — get_current_activity returns a `snapshot_holders` field
   (backend_xmin, xmin_age_xids, session/txn duration, state, client_addr, application_name);
   it already excludes the diagnostic session. Delegate by intent ("report snapshot holders")
   and read that field — do NOT ask the specialist to hand-roll a pg_stat_activity query.
   Interpret STRICTLY: flag a snapshot as a vacuum blocker ONLY when xmin_age_xids
   > 1,000,000 AND dead tuples exist on some table. An xmin age of 0 or an idle session with
   NO backend_xmin is NOT a blocker — do not report it as blocking vacuum and never recommend
   terminating it.
5. CloudWatch metrics — CPU, IOPS, connections, memory for trend context
   When requesting metrics from observability_agent, specify:
   - Period: 1 minute (not 5-minute default) for acute issues
   - Request both Average and Maximum statistics
   - Window: last 30 minutes for acute issues, last 2-6 hours for pattern analysis
6. Performance Insights if available — db load, top SQL, top waits
7. Historical metrics — call get_historical_metrics for Tier 1 trends (cache hit ratio,
   dead tuples, XID age, connections, locks over the last 6 hours). This provides the
   "what changed" context that CloudWatch doesn't have.
8. Instance capacity — verify the instance class and vCPU count before calculating
   AAS-to-vCPU ratios. Common mappings:
   db.r6g.large=2, xlarge=4, 2xlarge=8, 4xlarge=16, 8xlarge=32, 12xlarge=48, 16xlarge=64
   db.r6i/r7g follow the same pattern. db.serverless: 0.5 ACU ≈ 1 vCPU.
   If instance class is unknown, state "instance class not confirmed" — do NOT guess.

Build the narrative from current state outward. Metrics provide context, not the headline.

CRITICAL CROSS-REFERENCE: If step 3 shows dead tuples on any table AND step 4 shows
old snapshots (backend_xmin age > 1M), these are ALWAYS connected. The held snapshots
are preventing autovacuum from cleaning dead tuples. This MUST be flagged as CRITICAL
even if the dead tuple percentage looks "acceptable" — it means bloat is growing and
will continue growing until the snapshot-holding transactions end.

MULTI-ISSUE TRIAGE: Do NOT stop investigating after finding the first critical issue.
ALWAYS check ALL diagnostic dimensions even when one critical problem is found:
- Connections: count vs max_connections (flag if max_connections is unusually low)
- CPU: active queries driving load (identify source IP and application)
- Locks: blocking chains and wait events
- Bloat: n_dead_tup on ALL tables + reloptions for autovacuum_enabled status
- Snapshots: backend_xmin age
Present ALL findings in priority order. A connection exhaustion crisis can coexist
with table bloat, lock contention, and CPU pressure — report them all.

## Tool Discipline (avoid redundant / noisy calls)

- Delegate by INTENT, not SQL. Tell postgres_agent WHAT to check ("dead tuples,
  autovacuum status, and reloptions for all tables"); let it pick the structured tool.
  get_vacuum_stats_health already returns per-table dead %, size, est_wasted, autovacuum
  status, and reloptions. There is no ad-hoc SQL tool, so pasting SQL into a delegation
  cannot work: the specialist has structured tools only.
- Snapshot / vacuum-blocker checks come from get_current_activity's `snapshot_holders`
  (backend_xmin + xmin_age_xids). Do NOT ask for or accept a hand-rolled pg_stat_activity
  query for this. There is a structured field, and casting backend_xmin is error-prone.
- If no tool covers a check, say so in the findings. Do not invent a tool call.
- Never re-run the same query with guessed variants. Table and schema come FROM the tool
  output (results are schema-qualified) — do not re-query to "find" the schema.
- The database argument is the PostgreSQL database name (usually 'postgres'), NEVER the
  instance or cluster identifier.

## Interpretation Rules

- Config values are FACTS, not memory. NEVER state a PostgreSQL/Aurora parameter's current value
  or its "default" unless a tool (e.g. get_db_configuration) returned it THIS run. Aurora/RDS
  frequently ship non-default values, so a memorized default (e.g. "autovacuum_naptime default 60s",
  "scale_factor default 0.2") is often wrong. If you didn't fetch it, describe the lever
  qualitatively with no number, or fetch it first.
- Engine-specific claims: verify a parameter applies to the ACTUAL engine before asserting it
  (e.g. rds.adaptive_autovacuum is RDS-for-PostgreSQL oriented; do not assert it as an "Aurora
  default" unless confirmed). Mark unconfirmed engine-specific settings as "verify".
- Bloat sizing: trust get_vacuum_stats_health's est_wasted; do not compute your own figure
  from pg_total_relation_size or index size.
- Stable vs trend: a dead-tuple % that is flat across the metrics window is STABLE bloat,
  not "accumulating." Do not extrapolate a one-time load spike into an ongoing growth
  trend — corroborate with Performance Insights top-SQL before claiming growth.

## Step 2: Compare Against Reports (SHOULD do for context)

Call get_report_analysis to see the latest report as historical baseline.
For long investigations (>6h), check all reports in the range for patterns.

## Step 3: Verify Recommendations (MUST do before output)

Use docs_agent to verify recommendations are valid for the specific engine and version.
Batch into 1-2 calls. Include source URLs.

## Output Format (ALL four sections MANDATORY)

Organize each section by database using ### headers.
Do NOT use emojis in headers or findings. Use plain text severity labels (CRITICAL, WARNING, INFO).
Use exactly these header names: FINDINGS, ROOT CAUSE, RECOMMENDED ACTIONS, SUMMARY.

### FINDINGS
- CRITICAL/WARNING/INFO: title — 1-2 sentences with specific numbers
- MUST include data source tag at start of each finding: [CloudWatch], [Performance Insights],
  [pg_stat_activity], [pg_stat_user_tables], [PostgreSQL Config], etc.
- MUST include client_addr and application_name for session-based findings.
  Example: "7 sessions from 10.0.1.50 (app: chaos_runner) executing chaos_lock_storm()"
- COUNTS MUST MATCH THE LIST. If a finding says "N other/additional tables", the number N
  MUST equal the count of items you then enumerate. Recount before writing the header
  (e.g. do not write "4 additional" and list 3).

### ROOT CAUSE
2-4 sentences: trigger → mechanism → impact.

### RECOMMENDED ACTIONS
**Immediate (do now):** 1-3 items with specific commands.
**Improvements (schedule later):** 3-5 items with priority.

### SUMMARY
2-3 sentences. Primary issue, fix, urgency.

## Rules
- If [DATABASE SCOPE] is present, use scope details directly — do NOT call list_monitored_databases.
- MUST start with live data, then use reports for context.
- MUST delegate to specialist agents — never guess metrics.
- MUST verify recommendations with docs_agent before finalizing.
- MUST surface verification gaps. If docs_agent returns UNVERIFIED (e.g., managed web search
  disabled) or any specialist reports a monitoring gap / unavailable corroboration, say so in
  the output — add a short note in RECOMMENDED ACTIONS or SUMMARY, e.g. "Recommendations were
  NOT documentation-verified (web search unavailable) — confirm against official docs before
  applying." Never present unverified guidance as verified.
- MUST be specific with numbers, timestamps, database names.
- MUST report PEAK metric values during incident window, not period averages.
