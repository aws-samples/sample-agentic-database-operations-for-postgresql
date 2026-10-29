# Documentation Sources

Canonical documentation entry points the agent should cite and read from when verifying
recommendations. Prefer these over general web results. When a managed web search tool is
available, scope queries to these domains; otherwise use them as the pages to fetch and cite.

## AWS — Aurora & RDS PostgreSQL
- Amazon Aurora PostgreSQL User Guide — https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/
- Amazon RDS for PostgreSQL User Guide — https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_PostgreSQL.html
- Amazon Aurora Serverless v2 — https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2.html
- AWS Prescriptive Guidance — PostgreSQL maintenance (autovacuum, VACUUM) —
  https://docs.aws.amazon.com/prescriptive-guidance/latest/postgresql-maintenance-rds-aurora/

## PostgreSQL (official, version-aware)
- PostgreSQL documentation (all versions) — https://www.postgresql.org/docs/
  Use the version that matches the target database, e.g. https://www.postgresql.org/docs/17/
  for PostgreSQL 17. Key references: `runtime-config-autovacuum`, `sql-vacuum`, `sql-altertable`,
  `runtime-config-client` (idle_in_transaction_session_timeout).

## Usage rules
- Be version-specific: cite the doc page matching the engine version under investigation.
- Distinguish Aurora from vanilla RDS/PostgreSQL — Aurora manages storage/WAL/checkpoints
  differently (see the Aurora PostgreSQL engine guide); do not apply vanilla-PG storage advice
  to Aurora.
- Verify in ONE focused pass: a single search, then read the single most relevant page.
- If the sources can't be reached, mark the guidance "Unverified" — never present model-memory
  as verified documentation.
