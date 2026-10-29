---
name: system-parameters
description: Audit PostgreSQL configuration — global parameters (autovacuum, wal_level, ssl, shared_buffers, logging, preload libraries) and per-database/role overrides that mask them. Activate on parameter/config audit, "is the DB configured safely", tuning review, or unexpected per-role settings.
allowed-tools: postgres_agent observability_agent
metadata:
  surface: investigation
  check_ids: [CHK-041, CHK-048]
---

# System Parameter Investigation

Audit the effective configuration and any overrides for unsafe settings. The shared diagnostic
guardrails apply; verdicts are already computed by the tools.

## When to use
Symptoms involving: a config/parameter audit, "is this configured safely", tuning review, onboarding
assessment, or unexpected per-database/role settings.

## Steps (which tool to call)
1. **Global config — `get_db_configuration`** (via `postgres_agent`). Returns the key parameters (with
   effective value + source + deployment_type), a `config_findings` list, and a `status` from the
   safety invariants (autovacuum=off, wal_level=minimal, ssl=off are CRITICAL; pg_stat_statements not
   preloaded is WARNING). **Trust `status`.**
2. **Overrides — `get_setting_overrides`** (via `postgres_agent`). Per-database / per-role
   `ALTER ... SET` overrides with a `status` (any override = NOTICE review item; security-sensitive
   ones — `autovacuum=off`, `log_statement=none` per role — escalate). An override can silently mask
   the global config, so always check both.
3. **Config values are facts, not memory.** Cite only what the tools returned this run — never a
   parameter's "default" from memory (Aurora/RDS ship non-default values). Memory/sizing params
   (shared_buffers, work_mem) depend on instance RAM — treat sizing as a tuning judgment, not a hard
   flag.

## Output
Report the flagged parameters (value + source) and any risky overrides, with verdicts. Remediation is
a proposal: on Aurora/RDS, parameter changes go through the **parameter group** (never `ALTER SYSTEM`);
per-db/role overrides via `ALTER DATABASE/ROLE ... SET`/`RESET`. Correlate recent changes with
CloudTrail `ModifyDBParameterGroup` if a setting looks unexpected.

Refer to this in user-facing output by name — "system parameters / configuration" — never by an
internal catalog code. Catalog ids live only in this skill's frontmatter metadata.
