---
name: extension-inventory
description: Verify expected PostgreSQL extensions are installed and check for available version upgrades. Activate on extension inventory, missing extension, "is pg_stat_statements/pgaudit installed", extension version upgrades, or post-major-upgrade extension review.
allowed-tools: postgres_agent
metadata:
  surface: both
  check_ids: [CHK-051]
---

# Extension Inventory Investigation

Check which extensions are installed, whether the expected ones are present, and whether upgrades are
available. The shared diagnostic guardrails apply; the verdict is already computed by the tool.

## When to use
Symptoms involving: an extension inventory, a missing extension, "is pg_stat_statements / pgaudit
installed", available extension upgrades, or a post-major-version-upgrade extension review.

## Steps (which tool to call)
1. **Primary — `get_extension_inventory`** (via `postgres_agent`). Returns installed `extensions` (with
   `installed_version`, `latest_available`, `upgrade_available` + per-row `status`), `missing_expected`
   (expected extensions not installed), and an overall `status` (missing expected = WARNING; missing
   `pg_stat_statements` = CRITICAL — the diagnostics depend on it; a version upgrade available = NOTICE).
   **Trust `status`.**
2. **Tie missing extensions back to their impact.** `pg_stat_statements` missing → the query/temp-spill
   checks fall back or cannot run. `pgaudit` missing → the audit-logging check flags a compliance gap.
   `pgstattuple` missing → exact bloat confirmation is unavailable. Name the concrete impact, not just
   "extension missing".

## Output
Report installed extensions with versions/upgrade flags and the list of missing expected extensions
with the impact of each gap. Remediation is a proposal: `CREATE EXTENSION <name>;` (some, like
pg_stat_statements/pgaudit, require adding to `shared_preload_libraries` via the parameter group and a
restart); `ALTER EXTENSION <name> UPDATE;` for an available upgrade. Note major-version upgrades may
require re-creating extensions.

Refer to this in user-facing output by name — "extension inventory" — never by an internal catalog
code. Catalog ids live only in this skill's frontmatter metadata.
