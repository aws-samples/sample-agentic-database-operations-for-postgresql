---
name: sequence-exhaustion
description: 'Detect sequences approaching their maximum value, where the next INSERT will fail with "nextval: reached maximum value of sequence". Activate on sequence exhaustion, nextval failing, INSERTs failing on an identity/serial column, or int4/int2 sequence nearing its limit.'
allowed-tools: postgres_agent observability_agent
metadata:
  surface: both
  check_ids: [CHK-004]
---

# Sequence Exhaustion Investigation

Find sequences running out of headroom before an INSERT fails. The shared diagnostic guardrails apply;
the usage band is already computed by the primary tool.

## When to use
Symptoms involving: a sequence near its max, `nextval: reached maximum value of sequence`, INSERTs
failing on a serial/identity column, or an `int4`/`int2` sequence approaching its limit.

## Steps (which tool to call)
1. **Primary — `get_sequence_exhaustion`** (via `postgres_agent`). Per sequence it returns
   `schemaname`, `sequencename`, `data_type`, `last_value`, `max_value`, `min_value`, `increment_by`,
   `usage_pct`, and a `status` band the tool has already applied (OK / NOTICE / WARNING / CRITICAL /
   EMERGENCY). **Trust `status`**; rank by `usage_pct`. `usage_pct` correctly handles descending
   sequences (`last_value / min_value`), not just ascending.
2. **Weigh the data type.** An `int4` (max ~2.1B) or `int2` (max 32K) sequence on a high-write table
   is the real danger — it can exhaust in production lifetimes. An `int8` sequence is effectively
   unbounded; a high `usage_pct` there is far less urgent. Say which type each flagged sequence is.
3. **Confirm the failure signal (optional) — `observability_agent`.** Look for
   `nextval: reached maximum value of sequence` in CloudWatch Logs. If log export is not confirmed,
   report its absence as INCONCLUSIVE — never conclude "no failures" from missing logs.

## Output
Report each at-risk sequence schema-qualified as `schema.sequence`, with its type, `usage_pct`, and
`status`. Remediation is a proposal, and depends on the type:
- **int4/int2 nearing the limit:** widen the column and sequence to `bigint` —
  `ALTER SEQUENCE <schema>.<sequence> AS bigint MAXVALUE 9223372036854775807;` plus the owning
  column's type change (call out that the column change can be a heavy operation on a large table).
- **Only if wrap-around is acceptable:** `ALTER SEQUENCE ... CYCLE` (wraps instead of failing — gaps,
  not exhaustion). Note the uniqueness risk before suggesting CYCLE.
Use each sequence's OWN schema in the DDL. On Aurora/RDS these are standard DDL, not parameter-group
changes.

Refer to this in user-facing output by name — "sequence exhaustion" — never by an internal catalog
code. Catalog ids live only in this skill's frontmatter metadata.
