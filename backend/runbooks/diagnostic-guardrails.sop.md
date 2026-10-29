# Diagnostic Guardrails

## Overview
Check-agnostic accuracy and safety RULES for every diagnostic check. Cluster skills
inherit these — a skill states only what is specific to its check and assumes these
hold. They exist so the same correctness lessons are not re-learned per check.
(These are behavioral rules for the diagnostic agent, not a Bedrock Guardrails config.)

## Grounding & determinism
- Trust the tool's verdict; never recompute it. The tool applies the reference
  thresholds and returns a severity/`health` field — rank and report by that, not by a
  formula improvised in prose (e.g. do NOT multiply dead% by size to re-rank bloat).
- Report only objects, numbers, and states present in a tool's output THIS run.
  Never fabricate a table, index, metric, or parameter value.
- No extrapolation. A growth/trend claim ("+X GB/month", "filling up") requires the
  trend tool's own verdict. If a trend tool says flat/not-growing, report STABLE and
  do not project a rate. Never eyeball a series and estimate.

## Schema qualification
- Schema-qualify every object as `schema.object`. Remediation DDL must use the object's
  OWN schema from the row.
- Never infer one object's schema from another. If a tool row lacks a schema, say so
  rather than guessing.

## Config & engine facts
- Config values are facts, not memory. Never cite a parameter's current value OR its
  "default" unless a tool returned it this run. Aurora/RDS ship non-default values, so a
  memorized default is often wrong. If it was not fetched, describe the lever
  qualitatively with no number, or fetch it first.
- Read before you recommend a change. Before proposing ANY parameter change, you MUST have
  fetched that parameter's current effective value this run (get_db_configuration) and
  state it as current -> proposed. Never recommend a target value, or claim the current
  value is a problem, from a memorized default, a runbook number, or web search without
  having read it. If it was not fetched, fetch it first; otherwise give only the direction
  of change with no number.
- Verify engine-specific claims. Confirm a parameter/behavior applies to the actual
  engine (Aurora vs RDS vs self-managed) before asserting it; mark unconfirmed ones
  "verify".

## Observability & prerequisites
- Observability-unavailable disclosure. If a CloudWatch / Performance Insights /
  CloudTrail signal or its prerequisite is missing, report it as a monitoring gap.
  Never assert an effect (I/O, latency, cache-miss) that was not measured.
- Tool-failure disclosure. When a tool errors, times out, or returns no result, name it
  in the response and state which part of the analysis it left uncovered. A failed tool
  is a gap in the evidence; a response that omits it presents partial findings as
  complete. This covers the in-database tools exactly as it covers the AWS APIs above.
- A tool failure is never a customer symptom. A connection timeout, an authentication
  error, or an exhausted deadline inside a tool describes the diagnostic path, not the
  database's behavior. Never restate one as an observed fault ("users are experiencing
  connection timeouts"), and never build a root cause or a remediation plan on it.
- No diagnosis without evidence. When every tool that would have measured a condition
  fails, the finding is INCONCLUSIVE and names the tools needed to settle it. Do not
  offer candidate root causes for a condition that was never observed — a list of
  hypotheses reads as findings, and sends an operator auditing a fault that may not
  exist.
- Prerequisite / no-match honesty. Absent log lines are NOT "healthy" when the logging
  prerequisite is unconfirmed — report INCONCLUSIVE and name the missing prerequisite.

## Remediation wording & safety
- Read-only against customer databases. Every recommendation is a proposal for the
  operator to run; the agent never executes changes.
- No destructive statement as a runnable fix. Never present `DROP`, `TRUNCATE`, or a
  `DELETE`/`UPDATE` without a `WHERE` clause as a copy-paste remediation (a fenced
  "run this" block). State what must change, give the scoped form — an explicit `WHERE`
  that names exactly the rows, a `SELECT count(*)` with that same predicate to confirm
  the count, wrapped in a transaction, with a snapshot first — and leave running it to
  the operator. Showing a destructive statement to ANALYZE it (the customer's own query,
  an `EXPLAIN`) is fine; proposing one as the fix is not.
- Precise remediation wording. Be exact about mechanism and effect (e.g. plain VACUUM
  frees space for REUSE, it does not shrink the on-disk file or return space to the OS).
- Parameter changes on Aurora/RDS go through the parameter group, never `ALTER SYSTEM`.
  Per-object `ALTER TABLE ... SET (...)` reloptions are fine.
- Documentation verification. When managed web search is on, cite the source; when off,
  mark the guidance unverified rather than presenting it as confirmed.

## Traceability
- Never surface an internal catalog id or audit tag in findings or any user-facing text.
  Refer to a check by its display name. Traceability to the source catalog is preserved
  in metadata/provenance fields, not in prose.
