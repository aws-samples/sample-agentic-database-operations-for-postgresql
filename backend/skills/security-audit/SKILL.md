---
name: security-audit
description: Audit database security posture — roles/superusers, schema privileges, connection encryption (SSL/TLS), Row-Level Security, audit logging (pgaudit), and heuristic PII/credential detection. Activate on security audit, compliance review, who-can-access-what, unencrypted connections, RLS, pgaudit, or PII/sensitive-data discovery.
allowed-tools: postgres_agent observability_agent
metadata:
  surface: investigation
  check_ids: [CHK-042, CHK-043, CHK-044, CHK-045, CHK-046, CHK-047]
---

# Security Posture Audit

Read-only audit of database security posture across roles, privileges, encryption, RLS, audit logging,
and sensitive data. The shared diagnostic guardrails apply; verdicts are already computed by the tool.
Everything here is a **review finding for the operator/security team** — never an automated change.

## When to use
Symptoms involving: a security or compliance audit, "who can access what", unencrypted connections,
Row-Level Security, audit logging / pgaudit, or PII / sensitive-data discovery.

## Steps (one tool, six lenses)
Call **`get_security_audit`** (via `postgres_agent`) and read each subsection with its status:
1. **Roles (`roles` + `roles_status`)** — superuser count, replication/`bypasses_rls` login roles.
   Minimal superusers expected; an RLS-bypassing login role is a WARNING. Report role names.
2. **Privileges (`privileges` + `privileges_status`)** — grantee → schema → privileges. `PUBLIC` with
   table access is broad exposure. "Expected vs unexpected" is site-specific — compare to intended access.
3. **Encryption (`ssl_connections` + `ssl_status`)** — any unencrypted session is exposure (WARNING);
   dominant unencrypted is CRITICAL. Enforce with `rds.force_ssl`.
4. **RLS (`row_level_security` + `rls_status`)** — RLS enabled with 0 policies blocks non-owner access;
   a multi-tenant table not `FORCE`d while the app connects as owner means RLS is bypassed (leak risk).
5. **Audit logging (`audit_config` + `audit_config_status`)** — pgaudit present? `log_statement`? Missing
   pgaudit on a regulated DB is a compliance gap; confirm real audit trail via CW Logs `AUDIT:` entries
   (INCONCLUSIVE without the prerequisite).
6. **Sensitive data (`sensitive_data` + `sensitive_data_status`)** — heuristic by column NAME: a
   Credit Card column is a PCI-DSS CRITICAL; a Credential column is WARNING. These are CANDIDATES —
   confirm contents and access controls; do not assert confirmed exposure from a name match.

## Output
Report each lens with its finding and verdict, security/compliance framed. Remediation is a proposal
for the operator/security owner (revoke a grant, drop a superuser, force SSL, add an RLS policy,
install pgaudit, encrypt/restrict a sensitive column) — the platform is read-only and never applies
security changes itself.

Refer to these in user-facing output by name — "roles", "privileges", "encryption", "RLS", "audit
logging", "sensitive data" — never by an internal catalog code. Catalog ids live only in this skill's
frontmatter metadata.
