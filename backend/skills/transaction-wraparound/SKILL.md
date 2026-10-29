---
name: transaction-wraparound
description: Investigate transaction-ID (XID) wraparound risk — database- and table-level frozen-XID age climbing toward the 2-billion limit, whether autovacuum freeze is keeping up, and what is holding it back. Activate on wraparound, XID age, datfrozenxid / relfrozenxid, "vacuum to prevent wraparound", MaximumUsedTransactionIDs, or read-only-shutdown risk.
allowed-tools: postgres_agent observability_agent get_historical_metrics
metadata:
  surface: investigation
  check_ids: [CHK-001, CHK-002]
---

# Transaction-ID Wraparound Investigation

Investigate how close the database is to XID wraparound and why freeze is or isn't keeping up. Run the
steps in order via the specialist agents, branching on what each reveals. Threshold bands are already
applied by the primary tool (from the internal references); the shared diagnostic guardrails apply to
every finding.

## When to use
Symptoms involving: XID age / wraparound, `datfrozenxid` / `relfrozenxid`, "vacuum to prevent
wraparound" log lines, a climbing `MaximumUsedTransactionIDs`, or fear of a read-only shutdown.

## Steps (which tool to call)
1. **Database age — `get_maintenance_status`** (via `postgres_agent`). Read `xid_wraparound`: per
   database it returns `xid_age`, `xid_age_pct` (of the 2B budget), `freeze_max_age`, and a `status`
   verdict the tool has already computed (relative tiers vs `autovacuum_freeze_max_age` plus absolute
   2B-budget floors). **Trust `status`.** Always report XID age as BOTH the absolute count AND the
   percentage of 2B.
2. **Which table drives it — same tool's `table_xid_age`.** Returns schema-qualified per-table
   `xid_age`, `freeze_pct`, `last_vacuum`/`last_autovacuum`, dead/live tuples, and its own `status`.
   The top row is usually the table pinning the database age. (Partitioned parents are already
   excluded.) Trust `status`; rank by it.
3. **What is holding freeze back — `get_current_activity`** (via `postgres_agent`). Read
   `snapshot_holders` (`backend_xmin`, `xmin_age_xids`) — a long-running or idle-in-transaction
   session with an old snapshot stops freeze from advancing. STRICT: flag a session as a blocker ONLY
   when `xmin_age_xids` > 1,000,000. An idle session with NULL `backend_xmin` or age 0 is NOT a
   blocker — never recommend terminating it. Do not hand-roll a `pg_stat_activity` query.
4. **Freeze configuration — `get_db_configuration`** (via `postgres_agent`). Check
   `autovacuum_freeze_max_age` and worker/cost settings. **Never state a parameter's value or default
   from memory** — cite only what the tool returned this run. Engine note to VERIFY, not assume: on
   RDS/Aurora `rds.adaptive_autovacuum` is on by default and makes autovacuum more aggressive once age
   reaches `max(autovacuum_freeze_max_age, 500M)`; self-managed has no such mechanism, so the early
   WATCH/NOTICE tiers matter most there.
5. **Burn rate / trend — `get_historical_metrics` + `observability_agent`.** For "how fast / when will
   it wrap" use the Tier 1 `xid_age` trend and CloudWatch `MaximumUsedTransactionIDs`; project time to
   the 1B/1.5B/1.8B tiers from the measured rate. Do NOT extrapolate from a single point. If the
   signal is unavailable, say so — do not assert a burn rate you did not measure.

## autovacuum-disabled caveat
A table with `autovacuum_enabled = false` still gets a FORCED anti-wraparound freeze once it crosses
`autovacuum_freeze_max_age` — it only loses ROUTINE autovacuum. Never say such a table "will never be
frozen" or is "excluded from anti-wraparound"; the accurate risk is no routine cleanup plus an
eventual disruptive forced freeze.

## Prerequisite / no-match caveat
"Vacuum to prevent wraparound" and "oldest xmin" log signals are valid only if PostgreSQL log export
to CloudWatch is enabled (and `log_autovacuum_min_duration` set for autovacuum lines). If unconfirmed,
report absent log lines as INCONCLUSIVE (a monitoring gap), never as healthy.

## Output
Report by severity (EMERGENCY / CRITICAL / WARNING / NOTICE / WATCH), each tagged with its source
(`[pg_database]`, `[pg_stat_user_tables]`, `[CloudWatch]`). For a database at risk: the age (absolute
and % of 2B), the driving table(s), any freeze blocker (with `xmin_age_xids`), and the projected
window if a trend was measured.

Remediation wording must be precise: the fix is to let autovacuum catch up or run a manual
`VACUUM (FREEZE)` on the driving table(s), and to clear the blocking snapshot/transaction if one
exists. On Aurora/RDS, parameter changes (e.g. `autovacuum_freeze_max_age`, worker/cost settings) go
through the **parameter group**, never `ALTER SYSTEM`; per-table `ALTER TABLE ... SET (...)` is fine.
Freeze runs on the writer.

Refer to this in user-facing output by name — "transaction ID wraparound" — never by an internal
catalog code. Catalog ids live only in this skill's frontmatter metadata for traceability.
