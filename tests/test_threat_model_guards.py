#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Guards for the template-level decisions taken after threat modelling this system.

Each assertion here stands in for a threat that was verified against the source and
then closed. They are cheap to keep and they fail loudly, which matters because every
one of these is the kind of change that looks harmless in review: widening an IAM
resource back to '*', dropping a response-headers policy while refactoring a
distribution, or flipping a default to save a few dollars a month.

The ALB-to-VPC-Link narrowing is guarded in test_cidr_wiring.py instead, because the
rule it replaced was one of that file's two CIDR ingress rules and the count assertion
there already had to change.

Not guarded here, deliberately: the findings that were accepted rather than fixed
(/config.js exposing public Cognito identifiers, knowledge-document content trusted as
agent context). Those are recorded
in docs/ARCHITECTURE.md#security-model with reasoning; a test cannot express "we
considered this and chose to live with it". Certificate verification on database
connections is a rule with its own guard: tests/test_db_tls.py.
"""
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent

passed = 0
failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


class _Loader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation's short-form tags (!Ref, !Sub, ...)."""


def _keep_scalar(loader, suffix, node):
    """Preserve scalars so !Sub strings stay inspectable; collapse the rest."""
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_Loader.add_multi_constructor("!", _keep_scalar)


def load(rel):
    return yaml.load((ROOT / rel).read_text(encoding="utf-8"),  # nosec B506
                     Loader=_Loader)


print("══ batch:SubmitJob is scoped to this deployment ══")
# SubmitJob + DescribeJobs + ListJobs must be scoped to this deployment's own queue and
# job definition, not '*'. A '*' grant would let a compromised task submit jobs against
# any queue or job definition in the account, and would widen job-parameter tampering
# from "this deployment's databases" to anything in the account.
api_text = (ROOT / "infrastructure/api.yaml").read_text(encoding="utf-8")
api = load("infrastructure/api.yaml")

statements = []
for res in api.get("Resources", {}).values():
    if not isinstance(res, dict) or res.get("Type") != "AWS::IAM::Role":
        continue
    for pol in res.get("Properties", {}).get("Policies", []) or []:
        doc = pol.get("PolicyDocument", {}) if isinstance(pol, dict) else {}
        for st in doc.get("Statement", []) or []:
            if isinstance(st, dict):
                statements.append(st)
            elif isinstance(st, list):
                # An Fn::If-wrapped statement loads as [condition, then, else]. Keep the
                # dict branches (the real statements); skip the condition name and the
                # AWS::NoValue else-branch. This is how the batch:SubmitJob statement is
                # now conditionally included (omitted, not '*', when batch is disabled).
                for branch in st:
                    if isinstance(branch, dict) and "Ref" not in branch:
                        statements.append(branch)


def actions_of(st):
    act = st.get("Action", [])
    return act if isinstance(act, list) else [act]


# The reads that AWS refuses to authorise against anything but '*'. ListJobs is here
# on evidence, not assumption: granted on the job-queue ARN and called with that
# queue, it still returned "not authorized to perform: batch:ListJobs on resource: *".
# Scoping it therefore denies it outright rather than narrowing it - which is how the
# console's scan history silently emptied while scans kept running.
UNSCOPABLE_BATCH_READS = {"batch:DescribeJobs", "batch:ListJobs"}

submit = [st for st in statements if "batch:SubmitJob" in actions_of(st)]
check(len(submit) == 1, f"exactly one statement grants batch:SubmitJob (found {len(submit)})")
if submit:
    st = submit[0]
    check(st.get("Resource") != "*",
          "batch:SubmitJob is not granted on Resource '*'")
    # Neither read action may share this statement: neither supports resource types
    # in IAM, so bundling either one forces the whole statement back to '*' and
    # SubmitJob loses its scoping.
    for read_action in UNSCOPABLE_BATCH_READS:
        check(read_action not in actions_of(st),
              f"{read_action} is split out (it cannot be resource-scoped)")

wildcard = [st for st in statements
            if UNSCOPABLE_BATCH_READS & set(actions_of(st))]
check(len(wildcard) == 1,
      f"the unscopable Batch reads share one statement (found {len(wildcard)})")
if wildcard:
    check(wildcard[0].get("Resource") == "*",
          "that statement is on '*' — the only scope AWS accepts for them")
    # Still a containment guard: '*' is forced for these two and nothing else may
    # ride along on it.
    check(set(actions_of(wildcard[0])) == UNSCOPABLE_BATCH_READS,
          "the wildcard statement grants those two reads and nothing else")

check("HasBatchJobs" in api.get("Conditions", {}),
      "a HasBatchJobs condition guards the scoping against unset parameters")

print("")
print("══ the auth WAF rate-limits credential submissions, and blocks ══")
# The rule counts credential submissions, not all traffic to the user pool. Counting
# everything would make a sign-in cost ~10 requests instead of ~1 (the login page and
# its assets), so the limit would track page weight, not people, and no single number
# could both tolerate an office signing in together and constrain an attacker.
#
# So the rate rule now scopes down to POST (credential submissions and token
# exchanges; the page and its assets are GETs) and blocks, with a second, much
# looser rule over all traffic as the volumetric backstop.
#
# Regressions this catches: a rule slipping back to Count during calibration, the
# scope-down being dropped (which silently makes the limit ~10x tighter per user
# and will lock out an office), or the limit being hardcoded again.
cognito = load("infrastructure/cognito.yaml")
acl = cognito.get("Resources", {}).get("AuthWebAcl", {}).get("Properties", {})
rules = {r["Name"]: r for r in acl.get("Rules", []) or [] if isinstance(r, dict)}

check("AuthRateLimit" in rules, "a credential rate rule exists")
check("AuthVolumeLimit" in rules, "a volumetric backstop rule exists")

cred = rules.get("AuthRateLimit", {})
check("Block" in (cred.get("Action") or {}),
      "the credential rate rule blocks rather than counts")

rbs = (cred.get("Statement") or {}).get("RateBasedStatement") or {}
check(rbs.get("AggregateKeyType") == "IP", "it aggregates per source IP")
# The limit must stay tunable: an organisation behind one NAT address needs to
# raise it without editing the template.
check(not isinstance(rbs.get("Limit"), int),
      "the limit is a parameter reference, not hardcoded")
check("AuthRateLimitPerIp" in cognito.get("Parameters", {}),
      "an AuthRateLimitPerIp parameter is declared")

scope = rbs.get("ScopeDownStatement") or {}
bms = scope.get("ByteMatchStatement") or {}
check("Method" in (bms.get("FieldToMatch") or {}),
      "the scope-down inspects the request method")
check(str(bms.get("SearchString", "")).lower() == "post",
      "the scope-down counts POST only (credential submissions)")

vol = rules.get("AuthVolumeLimit", {})
check("Block" in (vol.get("Action") or {}), "the volumetric rule blocks")
vol_rbs = (vol.get("Statement") or {}).get("RateBasedStatement") or {}
check("ScopeDownStatement" not in vol_rbs,
      "the volumetric rule deliberately counts all traffic")
vol_limit = vol_rbs.get("Limit")
check(isinstance(vol_limit, int) and vol_limit >= 2000,
      f"the volumetric ceiling stays clear of a legitimate burst (found {vol_limit})")

print("")
print("══ the report frame stays sandboxed from this origin ══")
# The scan's HTML report is rendered with srcDoc, and it carries database-derived
# text - table, index and column names arrive through EXPLAIN output and the
# generator interpolates them unescaped. So the frame renders untrusted content
# next to a page whose localStorage holds the session tokens.
#
# `sandbox="allow-scripts allow-same-origin"` is the combination that cancels
# itself out: a frame that may run script AND shares the embedder's origin can
# reach parent.localStorage, so the sandbox stops being a boundary at all. The
# report needs neither: its accordions are CSS-only, and the two things its one
# inline script did (the "Expand All" button and the section navigator) are
# rewritten into CSS by prepareReportHtml before the HTML reaches the frame, so
# the frame runs no script at all.
#
# Easy to reintroduce while chasing a rendering problem, and nothing about the
# page looks different afterwards - hence this check.
panel = (ROOT / "frontend/src/components/ReportPanel.jsx").read_text(encoding="utf-8")
frames = re.findall(r"<iframe\b[^>]*>", panel, re.S)
check(len(frames) == 1, f"exactly one iframe in ReportPanel (found {len(frames)})")
for f in frames:
    attr = re.search(r'sandbox="([^"]*)"', f)
    check(attr is not None, "the report iframe declares a sandbox attribute")
    if attr:
        tokens = set(attr.group(1).split())
        check("allow-scripts" not in tokens,
              "the report frame runs no script (prepareReportHtml replaced the report's script with CSS)")
        check("allow-same-origin" not in tokens,
              "the report frame is NOT allow-same-origin (that would cancel the sandbox)")
    check("srcDoc={prepareReportHtml(" in f,
          "the report HTML goes through prepareReportHtml before it reaches the frame")

print("")
print("══ both public distributions send a Content-Security-Policy (t-3927876e, "
      "t-56e1866c, t-1585cb20) ══")
# All three token-theft findings need script injection into the SPA origin first: the
# tokens sit in localStorage, so a script running there can read them. The CSP attacks
# that precondition. If the policy is dropped, or script-src is loosened to allow
# inline script, the mitigation is gone while the tokens stay exactly where they were.
for rel, dist_key, policy_name in (
    ("infrastructure/frontend.yaml", "CloudFrontDistribution", "frontend"),
    ("infrastructure/admin-console.yaml", "AdminDistribution", "admin"),
):
    doc = load(rel)
    resources = doc.get("Resources", {})
    label = rel.split("/")[-1]

    policies = {
        k: v for k, v in resources.items()
        if isinstance(v, dict) and v.get("Type") == "AWS::CloudFront::ResponseHeadersPolicy"
    }
    check(len(policies) == 1, f"{label}: declares a ResponseHeadersPolicy")
    if not policies:
        continue
    cfg = next(iter(policies.values()))["Properties"]["ResponseHeadersPolicyConfig"]
    sec = cfg.get("SecurityHeadersConfig", {})

    csp = (sec.get("ContentSecurityPolicy") or {}).get("ContentSecurityPolicy", "")
    check(bool(csp), f"{label}: a CSP is set")
    directives = {
        d.strip().split()[0]: d.strip()
        for d in csp.split(";") if d.strip()
    }
    for required in ("default-src", "script-src", "connect-src", "object-src",
                     "base-uri", "frame-ancestors"):
        check(required in directives, f"{label}: CSP sets {required}")
    check("'unsafe-inline'" not in directives.get("script-src", ""),
          f"{label}: script-src forbids inline script")
    check("'unsafe-eval'" not in csp, f"{label}: CSP forbids eval")
    check(directives.get("object-src") == "object-src 'none'",
          f"{label}: object-src is 'none'")
    check(directives.get("frame-ancestors") == "frame-ancestors 'none'",
          f"{label}: frame-ancestors is 'none'")

    # A CSP that blocks the app is not a mitigation, it is an outage - and the checks
    # above all pass on one, because they only assert that the directives exist. The
    # first cut of this policy named the Cognito HOSTED UI domain
    # (*.auth.<region>.amazoncognito.com, used for the oauth2/token exchange) but not
    # the Cognito API endpoint that useAuth.js posts InitiateAuth to, so login failed
    # with "Refused to connect" on a deployment whose guards were green.
    #
    # Each entry below is an origin the SPA source fetches at runtime. Adding a call
    # to a new AWS endpoint means adding it here and to the template together.
    required_origins = {
        # frontend/src/hooks/useAuth.js:11 and admin-console/src/hooks/useAuth.js:11
        "frontend": ["https://cognito-idp.${AWS::Region}.amazonaws.com",
                     # App.jsx:27 - the operator app streams from the AgentCore
                     # runtime directly; deploy-spas.sh:82 writes that endpoint into
                     # deployedBackendUrl. The console has no equivalent.
                     "https://bedrock-agentcore.${AWS::Region}.amazonaws.com"],
        "admin": ["https://cognito-idp.${AWS::Region}.amazonaws.com"],
    }[policy_name]
    connect = directives.get("connect-src", "")
    for origin in required_origins:
        check(origin in connect, f"{label}: connect-src allows {origin}")

    # The transport and sniffing headers come free with the same policy; assert them so
    # a future edit cannot quietly trade them away.
    check("StrictTransportSecurity" in sec, f"{label}: sends HSTS")
    check("ContentTypeOptions" in sec, f"{label}: sends X-Content-Type-Options")
    check("ReferrerPolicy" in sec, f"{label}: sends a Referrer-Policy")

    # The policy is worthless unless the behaviour actually references it.
    dist = resources.get(dist_key, {})
    behaviour = (dist.get("Properties", {})
                     .get("DistributionConfig", {})
                     .get("DefaultCacheBehavior", {}))
    check("ResponseHeadersPolicyId" in behaviour,
          f"{label}: the default cache behaviour attaches the policy")

print("")
print("══ WAF defaults agree across the nesting (t-1af13302) ══")
# master.yaml always passes its own EnableWAF down, so the nested defaults never take
# effect on a full deploy. They were 'false' anyway, which made both templates read as
# "unprotected by default" to anyone opening them — and this is a sample, so the
# templates are documentation. The finding was a false positive about behaviour and a
# real one about legibility.
master = load("infrastructure/master.yaml")
master_waf = str(master.get("Parameters", {}).get("EnableWAF", {}).get("Default", ""))
check(master_waf == "true", f"master.yaml EnableWAF defaults to 'true' (got {master_waf!r})")
for rel in ("infrastructure/frontend.yaml", "infrastructure/admin-console.yaml"):
    doc = load(rel)
    got = str(doc.get("Parameters", {}).get("EnableWAF", {}).get("Default", ""))
    check(got == master_waf,
          f"{rel.split('/')[-1]}: EnableWAF default matches master ({got!r})")

master_text = (ROOT / "infrastructure/master.yaml").read_text(encoding="utf-8")
check(len(re.findall(r"EnableWAF: !Ref EnableWAF", master_text)) >= 3,
      "master.yaml passes EnableWAF to the frontend, admin and api stacks")


def iam_statements(rel):
    """Every IAM policy statement in a template, as (role, policy, statement).

    Tolerates resources and policy lists that resolve to something other than a dict —
    an !If around a resource collapses to a list under the permissive loader above.
    """
    doc = load(rel)
    out = []
    for name, res in (doc.get("Resources") or {}).items():
        if not isinstance(res, dict) or res.get("Type") != "AWS::IAM::Role":
            continue
        props = res.get("Properties")
        if not isinstance(props, dict):
            continue
        for pol in props.get("Policies") or []:
            if not isinstance(pol, dict):
                continue
            pdoc = pol.get("PolicyDocument")
            if not isinstance(pdoc, dict):
                continue
            for st in pdoc.get("Statement") or []:
                if isinstance(st, dict):
                    out.append((name, pol.get("PolicyName"), st))
    return out


def as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


print("")
print("══ the governance audit trail is append-only ══")
# The audit index shares a role with the app's other tables. If it also shares the
# statement, the credential that writes the audit trail can erase it, so anything
# reaching the task can delete the record of what it did. Nothing needs the omitted
# verbs: audit_log.write_event only calls put_item, the console reads with paged Query
# (no Scan), the table has no TTL, and the seed function is granted PutItem alone.
AUDIT_TEMPLATES = ("infrastructure/api.yaml", "backend/template.yaml", "infrastructure/storage.yaml")


def all_statements(rel):
    """Every statement a role holds, as (role, policy, statement): its inline policies
    and the managed policies of this template attached to it (`Roles: [!Ref role]`),
    unwrapping `!If [Cond, stmt, !Ref AWS::NoValue]`."""
    doc = load(rel)
    out = []
    for name, res in (doc.get("Resources") or {}).items():
        if not isinstance(res, dict) or not isinstance(res.get("Properties"), dict):
            continue
        props = res["Properties"]
        if res.get("Type") == "AWS::IAM::Role":
            docs = [(name, pol.get("PolicyName"), pol.get("PolicyDocument"))
                    for pol in props.get("Policies") or [] if isinstance(pol, dict)]
        elif res.get("Type") == "AWS::IAM::ManagedPolicy":
            docs = [(role, name, props.get("PolicyDocument"))
                    for role in props.get("Roles") or [] if isinstance(role, str)]
        else:
            continue
        for role, policy, pdoc in docs:
            if not isinstance(pdoc, dict):
                continue
            for st in pdoc.get("Statement") or []:
                branches = st if isinstance(st, list) else [st]
                for b in branches:
                    if isinstance(b, dict):
                        out.append((role, policy, b))
    return out


def _is_audit_table(r):
    return r.endswith("-audit") or r.endswith("-audit/index/*") or r == "AuditTable.Arn"


audit_seen = {}
for rel in AUDIT_TEMPLATES:
    for role, policy, st in all_statements(rel):
        if st.get("Effect") != "Allow":
            continue
        resources = [r for r in as_list(st.get("Resource")) if isinstance(r, str)]
        if not any(_is_audit_table(r) for r in resources):
            continue
        audit_seen.setdefault(rel, []).append(st)
        label = f"{rel.split('/')[-1]}:{role}/{policy}"
        actions = [a for a in as_list(st.get("Action")) if isinstance(a, str)]
        check(all(_is_audit_table(r) for r in resources),
              f"{label}: the audit table sits in its own statement (table and its indexes only)")
        for forbidden in ("dynamodb:DeleteItem", "dynamodb:UpdateItem", "dynamodb:BatchWriteItem",
                          "dynamodb:Scan", "dynamodb:*", "*"):
            check(forbidden not in actions, f"{label}: does not grant {forbidden} on the audit table")
        check("dynamodb:PutItem" in actions, f"{label}: can still write audit records")
for rel in AUDIT_TEMPLATES:
    check(rel in audit_seen, f"{rel}: a statement granting access to the audit table exists")
api_audit = audit_seen.get("infrastructure/api.yaml") or [{}]
check(sorted(as_list(api_audit[0].get("Action"))) == ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"],
      "api.yaml: the task reads the index with GetItem / Query only (Scan removed)")
check(any(isinstance(r, str) and r.endswith("-audit/index/*") for r in as_list(api_audit[0].get("Resource"))),
      "api.yaml: and may Query the month-index")
# The agent stack holds exactly two audit-table statements: the runtime's (query_plan
# index rows) and the Tier 1 collector's (system admin events), each PutItem alone.
rt_audit = sorted((role, as_list(st.get("Action")))
                  for role, _p, st in all_statements("backend/template.yaml")
                  if st.get("Effect") == "Allow"
                  and any(_is_audit_table(r) for r in as_list(st.get("Resource")) if isinstance(r, str)))
check(rt_audit == [("RuntimeExecutionRole", ["dynamodb:PutItem"]), ("Tier1CollectorRole", ["dynamodb:PutItem"])],
      f"backend/template.yaml: exactly the runtime's and Tier 1's audit-table statements, each "
      f"[dynamodb:PutItem] ({rt_audit})")

print("")
print("══ the audit vault is write-once ══")
# No application role may delete a record, weaken the lock or rewrite the bucket's
# rules. Object Lock COMPLIANCE holds against root, but only if nothing can shorten a
# lock or change the configuration in the first place.
VAULT_FORBIDDEN = ("s3:*", "*", "s3:PutObjectLegalHold", "s3:BypassGovernanceRetention",
                   "s3:PutBucketObjectLockConfiguration", "s3:PutLifecycleConfiguration",
                   "s3:PutBucketPolicy")


def _names_vault(r):
    return isinstance(r, str) and ("AuditBucketName" in r or "AuditBucket.Arn" in r or "-audit-" in r)


vault_stmts = 0
retention = []
for rel in AUDIT_TEMPLATES:
    for role, policy, st in all_statements(rel):
        actions = [a for a in as_list(st.get("Action")) if isinstance(a, str)]
        resources = as_list(st.get("Resource"))
        label = f"{rel.split('/')[-1]}:{role}/{st.get('Sid') or policy}"
        if "s3:PutObjectRetention" in actions:
            retention.append((rel, role, st))
        if st.get("Effect") != "Allow":
            continue
        if not (any(_names_vault(r) for r in resources) or "*" in resources):
            continue
        if any(_names_vault(r) for r in resources):
            vault_stmts += 1
        bad = [a for a in actions if a in VAULT_FORBIDDEN or a.startswith("s3:Delete")]
        check(not bad, f"{label}: grants no delete / lock / policy verb on the vault ({bad})")
check(vault_stmts >= 3, f"the vault grants are all inspected (found {vault_stmts})")
check(len(retention) == 1, f"s3:PutObjectRetention appears in exactly one statement (found {len(retention)})")
if retention:
    rel, role, st = retention[0]
    check(rel == "infrastructure/api.yaml" and role == "ECSTaskRole"
          and st.get("Sid") == "AuditVaultRaiseOnlyRetention" and st.get("Effect") == "Allow",
          "it is AuditVaultRaiseOnlyRetention on the API task role")
    check(as_list(st.get("Action")) == ["s3:PutObjectRetention"], "and grants that verb alone")

# The exact conditions, with the !Ref kept visible (the permissive loader drops tags).
class _TagLoader(yaml.SafeLoader):
    """Keeps CloudFormation short-form tags as {"!Tag": value}."""


def _tagged(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {"!" + suffix: value}


_TagLoader.add_multi_constructor("!", _tagged)


def load_tagged(rel):
    return yaml.load((ROOT / rel).read_text(encoding="utf-8"), Loader=_TagLoader)  # nosec B506


def tagged_statements(rel, role):
    """[(sid, condition-name or '', statement)] a role holds (inline and attached
    managed policies of the same template), tags kept."""
    resources = load_tagged(rel)["Resources"]
    docs = [pol["PolicyDocument"] for pol in resources[role]["Properties"].get("Policies") or []]
    docs += [res["Properties"]["PolicyDocument"] for res in resources.values()
             if isinstance(res, dict) and res.get("Type") == "AWS::IAM::ManagedPolicy"
             and {"!Ref": role} in (res["Properties"].get("Roles") or [])]
    out = []
    for pdoc in docs:
        for st in pdoc["Statement"]:
            if isinstance(st, dict) and "!If" in st:
                cond, then, _else = st["!If"]
                out.append((then.get("Sid") or "", cond, then))
            elif isinstance(st, dict):
                out.append((st.get("Sid") or "", "", st))
    return out


api_tagged = {(sid, cond): st for sid, cond, st in tagged_statements("infrastructure/api.yaml", "ECSTaskRole")
              if sid}
raise_only = api_tagged.get(("AuditVaultRaiseOnlyRetention", "HasAuditBucket"))
check(raise_only is not None, "AuditVaultRaiseOnlyRetention is present only under !If HasAuditBucket")
if raise_only:
    check(raise_only.get("Condition") == {
        "StringEquals": {"s3:object-lock-mode": "COMPLIANCE"},
        "NumericGreaterThanEquals": {"s3:object-lock-remaining-retention-days": {"!Ref": "AuditRetentionDays"}},
        "NumericLessThanEquals": {"s3:object-lock-remaining-retention-days": 3653},
    }, f"it has exactly the three conditions: COMPLIANCE, >= !Ref AuditRetentionDays, <= 3653 "
       f"({raise_only.get('Condition')})")
    check(raise_only.get("Resource") == {"!Sub": "arn:aws:s3:::${AuditBucketName}/*"},
          "on the vault's objects only")
vault_rw = api_tagged.get(("AuditVaultReadWrite", "HasAuditBucket"))
check(vault_rw is not None and sorted(as_list(vault_rw.get("Action"))) == sorted([
    "s3:PutObject", "s3:GetObject", "s3:GetObjectVersion", "s3:GetObjectRetention",
    "s3:ListBucket", "s3:ListBucketVersions", "s3:GetBucketObjectLockConfiguration"]),
    "AuditVaultReadWrite (under HasAuditBucket) grants exactly put / get / list / read-lock verbs")
metrics = api_tagged.get(("AuditMetrics", ""))
check(metrics is not None and metrics.get("Action") == "cloudwatch:PutMetricData"
      and metrics.get("Condition") == {"StringEquals": {"cloudwatch:namespace": {"!Sub": "${ResourcePrefix}/Audit"}}},
      "the API task's PutMetricData is confined to the <prefix>/Audit namespace")

print("")
print("══ only the API task can change the vault switch ══")
DENY_SWITCH = {"ForAnyValue:StringEquals": {"dynamodb:LeadingKeys": ["audit_settings"]}}
for rel, role in (("backend/template.yaml", "RuntimeExecutionRole"),
                  ("backend/template.yaml", "Tier1CollectorRole"),
                  ("infrastructure/storage.yaml", "EmailSenderSeedRole")):
    denies = [st for r, _p, st in all_statements(rel)
              if r == role and st.get("Sid") == "DenyAuditSettingsWrite"]
    check(len(denies) == 1, f"{rel.split('/')[-1]}:{role}: carries DenyAuditSettingsWrite")
    if denies:
        st = denies[0]
        check(st.get("Effect") == "Deny" and set(as_list(st.get("Action"))) >= {
            "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem"},
            f"{role}: denies every write verb")
        check(st.get("Condition") == DENY_SWITCH, f"{role}: on the audit_settings key only")
        check(any(isinstance(r, str) and (r.endswith("-config") or r == "ConfigTable.Arn")
                  for r in as_list(st.get("Resource"))), f"{role}: on the config table")
check(not any(st.get("Sid") == "DenyAuditSettingsWrite" for _r, _p, st in all_statements("infrastructure/api.yaml")),
      "the API task (the switch's only writer) has no such deny")

print("")
print("══ in the agent stack only the Tier 1 collector touches the vault, put-only ══")
# Agent query_plan events are index-only: the runtime and the Batch scan get no grant, env
# or parameter for the vault. The Tier 1 collector records system admin events (cluster
# failover, host-route repair), so it alone may PUT a vault object: no read, no
# retention verb (objects take the bucket's COMPLIANCE floor), and DenyAuditSettingsWrite.
rt_text = (ROOT / "backend/template.yaml").read_text(encoding="utf-8")
rt_doc = load("backend/template.yaml")
naming = [(role, st) for role, _p, st in all_statements("backend/template.yaml")
          if any(_names_vault(r) for r in as_list(st.get("Resource")))]
check([role for role, _st in naming] == ["Tier1CollectorRole"],
      f"only Tier1CollectorRole names the vault ({[r for r, _ in naming]})")
t1_tagged = tagged_statements("backend/template.yaml", "Tier1CollectorRole")
vault_put = [(sid, cond, st) for sid, cond, st in t1_tagged
             if any(_names_vault(r.get("!Sub", "") if isinstance(r, dict) else r)
                    for r in as_list(st.get("Resource")))]
check(len(vault_put) == 1 and vault_put[0][0] == "Tier1AuditVaultPut" and vault_put[0][1] == "HasAuditBucket",
      "it is one statement, Sid Tier1AuditVaultPut, under !If HasAuditBucket")
if vault_put:
    st = vault_put[0][2]
    check(as_list(st.get("Action")) == ["s3:PutObject"] and st.get("Effect") == "Allow", "Action is [s3:PutObject]")
    check(st.get("Resource") == {"!Sub": "arn:aws:s3:::${AuditBucketName}/*"}, "on the vault's objects only")
for role in ("RuntimeExecutionRole", "BatchJobRole"):
    check(not any(_names_vault(r) for r_, _p, st in all_statements("backend/template.yaml") if r_ == role
                  for r in as_list(st.get("Resource"))), f"{role} names the vault nowhere")
# The AUDIT_* variables reach the Tier 1 function only; the retention-extension one nowhere.
env_by_fn = {}
for name, res in (rt_doc.get("Resources") or {}).items():
    if isinstance(res, dict) and isinstance(res.get("Properties"), dict):
        env = (res["Properties"].get("Environment") or {})
        variables = env.get("Variables") if isinstance(env, dict) else None
        if isinstance(variables, dict):
            env_by_fn[name] = variables
audit_env = {fn: {k: v for k, v in env.items() if k.startswith("AUDIT_")} for fn, env in env_by_fn.items()}
check({fn for fn, e in audit_env.items() if e} == {"Tier1CollectorFunction"},
      f"AUDIT_* variables only in Tier1CollectorFunction ({sorted(fn for fn, e in audit_env.items() if e)})")
check(audit_env.get("Tier1CollectorFunction") == {"AUDIT_BUCKET": "AuditBucketName",
                                                  "AUDIT_VAULT_DEFAULT": "AuditVault",
                                                  "AUDIT_RETENTION_FLOOR": "AuditRetentionDays"},
      "mapped to AuditBucketName / AuditVault / AuditRetentionDays")
for name in ("AUDIT_BUCKET", "AUDIT_VAULT_DEFAULT", "AUDIT_RETENTION_FLOOR"):
    check(rt_text.count(name) == 1, f"backend/template.yaml names {name} once (the Tier 1 env)")
check("AUDIT_EXTEND_RETENTION" not in rt_text, "backend/template.yaml has no AUDIT_EXTEND_RETENTION")
check("HasAuditBucket" in (rt_doc.get("Conditions") or {}) and rt_text.count("HasAuditBucket") == 2,
      "HasAuditBucket appears only in Conditions and in Tier1CollectorRole")
audit_params = sorted(k for k in (rt_doc.get("Parameters") or {}) if k.startswith("Audit"))
check(audit_params == ["AuditBucketName", "AuditRetentionDays", "AuditVault"],
      f"backend/template.yaml declares exactly the three Audit* parameters ({audit_params})")
master_doc = load("infrastructure/master.yaml")
agent_params = master_doc["Resources"]["AgentStack"]["Properties"]["Parameters"]
api_params = master_doc["Resources"]["APIStack"]["Properties"]["Parameters"]
check(api_params.get("AuditBucketName") == "StorageStack.Outputs.AuditBucketName"
      and api_params.get("AuditVault") == "AuditVault" and api_params.get("AuditRetentionDays") == "AuditRetentionDays",
      "master.yaml passes APIStack the bucket name, the switch default and the floor")
check({k: v for k, v in agent_params.items() if k.startswith("Audit")}
      == {k: api_params[k] for k in ("AuditBucketName", "AuditVault", "AuditRetentionDays")},
      "master.yaml passes AgentStack exactly those three, with the APIStack values")
storage_params = master_doc["Resources"]["StorageStack"]["Properties"]["Parameters"]
check(storage_params.get("AuditVault") == "AuditVault" and storage_params.get("AuditRetentionDays") == "AuditRetentionDays",
      "master.yaml passes StorageStack the switch default and the floor")
# PutMetricData has no resource-level form, so the namespace condition IS the scope.
# The runtime publishes only the audit index metric; the Tier 1 collector also publishes
# the metric series (<prefix>/Metrics). Every grant names one of exactly those two.
_AUDIT_NS = {"StringEquals": {"cloudwatch:namespace": {"!Sub": "${ResourcePrefix}/Audit"}}}
_METRICS_NS = {"StringEquals": {"cloudwatch:namespace": {"!Sub": "${ResourcePrefix}/Metrics"}}}
for role, label, allowed in (("RuntimeExecutionRole", "the runtime", [_AUDIT_NS]),
                             ("Tier1CollectorRole", "the Tier 1 collector", [_AUDIT_NS, _METRICS_NS])):
    pm = [st for _sid, _cond, st in tagged_statements("backend/template.yaml", role)
          if "cloudwatch:PutMetricData" in as_list(st.get("Action"))]
    conds = [st.get("Condition") for st in pm]
    check(len(pm) == len(allowed) and all(c in allowed for c in conds)
          and all(a in conds for a in allowed),
        f"{label} keeps PutMetricData confined to {', '.join(sorted(a['StringEquals']['cloudwatch:namespace']['!Sub'] for a in allowed))}")
api_env = {}
for res in load("infrastructure/api.yaml")["Resources"].values():
    if isinstance(res, dict) and res.get("Type") == "AWS::ECS::TaskDefinition":
        for c in res["Properties"]["ContainerDefinitions"]:
            api_env.update({e["Name"]: e["Value"] for e in c.get("Environment") or []})
check(api_env.get("AUDIT_BUCKET") == "AuditBucketName" and api_env.get("AUDIT_VAULT_DEFAULT") == "AuditVault"
      and api_env.get("AUDIT_RETENTION_FLOOR") == "AuditRetentionDays"
      and api_env.get("AUDIT_EXTEND_RETENTION") == "true",
      "the API container gets AUDIT_BUCKET, AUDIT_VAULT_DEFAULT, AUDIT_RETENTION_FLOOR, AUDIT_EXTEND_RETENTION")

print("")
print("══ the vault bucket is locked COMPLIANCE and outlives the stack ══")
storage_t = load_tagged("infrastructure/storage.yaml")["Resources"]
bucket = storage_t.get("AuditBucket") or {}
bprops = bucket.get("Properties") or {}
check(bprops.get("ObjectLockEnabled") is True, "ObjectLockEnabled: true")
rule = ((bprops.get("ObjectLockConfiguration") or {}).get("Rule") or {}).get("DefaultRetention") or {}
check(rule.get("Mode") == "COMPLIANCE", "the default retention mode is the literal COMPLIANCE")
check(rule.get("Days") == {"!Ref": "AuditRetentionDays"}, "and its days are !Ref AuditRetentionDays")
check((bprops.get("VersioningConfiguration") or {}).get("Status") == "Enabled", "versioning is on")
for lid in ("AuditBucket", "AuditBucketPolicy"):
    res = storage_t.get(lid) or {}
    check(res.get("DeletionPolicy") == "Retain" and res.get("UpdateReplacePolicy") == "Retain",
          f"{lid}: DeletionPolicy and UpdateReplacePolicy are Retain")
name = bprops.get("BucketName") or {}
sub = name.get("!Sub") if isinstance(name, dict) else None
check(isinstance(sub, list) and len(sub) == 2 and sub[0] == "${ResourcePrefix}-audit-${AWS::AccountId}-${Suffix}"
      and "AWS::StackId" in repr(sub[1]),
      "BucketName is the list form of !Sub with an AWS::StackId suffix")
pab = bprops.get("PublicAccessBlockConfiguration") or {}
check(all(pab.get(k) is True for k in ("BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls",
                                         "RestrictPublicBuckets")), "public access is fully blocked")
check((bprops.get("LoggingConfiguration") or {}).get("LogFilePrefix") == "s3-access/audit/",
      "access logs go to the log bucket under s3-access/audit/")
rules = {r.get("Id"): r for r in ((bprops.get("LifecycleConfiguration") or {}).get("Rules") or [])}
check(set(rules) == {"ExpireAfterRetention", "CleanExpiredDeleteMarkers", "AbortIncompleteUploads"}
      and rules["ExpireAfterRetention"].get("ExpirationInDays") == {"!Ref": "AuditRetentionDays"},
      "the three lifecycle rules, expiring after the floor")
policy = ((storage_t.get("AuditBucketPolicy") or {}).get("Properties") or {})
check(policy.get("Bucket") == {"!Ref": "AuditBucket"}, "AuditBucketPolicy targets AuditBucket")
sids = {st.get("Sid"): st for st in (policy.get("PolicyDocument") or {}).get("Statement") or []}
check(set(sids) == {"DenyInsecureTransport", "DenyOverwrite"},
      f"the bucket policy holds DenyInsecureTransport and DenyOverwrite only ({sorted(sids)})")
ow = sids.get("DenyOverwrite") or {}
check(ow.get("Effect") == "Deny" and ow.get("Principal") == "*" and ow.get("Action") == "s3:PutObject"
      and ow.get("Condition") == {"Null": {"s3:if-none-match": "true"}},
      "DenyOverwrite refuses any PutObject without If-None-Match")
table = (storage_t.get("AuditTable") or {}).get("Properties") or {}
gsi = {g.get("IndexName"): g for g in table.get("GlobalSecondaryIndexes") or []}
check("month-index" in gsi and gsi["month-index"].get("KeySchema") == [
    {"AttributeName": "month", "KeyType": "HASH"}, {"AttributeName": "ts", "KeyType": "RANGE"}]
    and gsi["month-index"].get("Projection") == {"ProjectionType": "ALL"},
    "AuditTable has the sparse month-index (month HASH, ts RANGE, ALL)")
for out in ("AuditBucketName", "AuditBucketArn"):
    check(out in (load("infrastructure/storage.yaml").get("Outputs") or {}), f"storage.yaml outputs {out}")
check("AuditBucketName" in (master_doc.get("Outputs") or {}),
      "master.yaml outputs AuditBucketName (make destroy reads it before deleting the stack)")

print("")
print("══ COMPLIANCE only: there is no lock-mode knob ══")
lock_mode_files = sorted((ROOT / "infrastructure").glob("*.yaml")) + [
    ROOT / "backend/template.yaml", ROOT / "deployment/configure.py", ROOT / "Makefile",
    ROOT / "deploy.config.example"]
hits = [p.relative_to(ROOT).as_posix() for p in lock_mode_files
        if "AuditLockMode" in p.read_text(encoding="utf-8")]
check(not hits, f"no AuditLockMode anywhere ({hits})")

print("")
print("══ audit settings, descriptions and alarms carry no price (design-review finding 6) ══")
# Cost lives in one place, the README's "Indicative costs"; every audit string points
# there instead of quoting a figure that would go stale.
PRICE = re.compile(r"(\$\s*\d|\bUSD\s*\d|\d\s*USD\b)")
price_texts = []
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("configure_for_guard", ROOT / "deployment/configure.py")
_configure = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_configure)
for key in ("AUDIT_VAULT", "AUDIT_RETENTION_DAYS"):
    spec = _configure.OPTIONAL_SETTINGS.get(key) or {}
    check(bool(spec), f"configure.py offers {key}")
    price_texts.append((f"configure.py {key}", f"{spec.get('prompt', '')} {spec.get('note', '')}"))
check("see Indicative costs in the README"
      in (_configure.OPTIONAL_SETTINGS.get("AUDIT_RETENTION_DAYS") or {}).get("note", ""),
      "the AUDIT_RETENTION_DAYS note points to Indicative costs in the README")
for rel in ("infrastructure/storage.yaml", "infrastructure/master.yaml", "infrastructure/api.yaml"):
    params = load(rel).get("Parameters") or {}
    for p in ("AuditVault", "AuditRetentionDays", "AuditBucketName"):
        if p in params:
            price_texts.append((f"{rel.split('/')[-1]} {p}", str(params[p].get("Description", ""))))
alarm_names = []
for lid, res in (load("infrastructure/api.yaml").get("Resources") or {}).items():
    if isinstance(res, dict) and res.get("Type") == "AWS::CloudWatch::Alarm" and lid.startswith("Audit"):
        alarm_names.append(res["Properties"].get("AlarmName"))
        price_texts.append((f"api.yaml {lid}", str(res["Properties"].get("AlarmDescription", ""))))
check(sorted(alarm_names) == sorted(["${ResourcePrefix}-audit-index-write-failed",
                                     "${ResourcePrefix}-audit-vault-off",
                                     "${ResourcePrefix}-audit-vault-write-failed"]),
      f"the three audit alarms exist ({alarm_names})")
example = (ROOT / "deploy.config.example").read_text(encoding="utf-8")
block = re.search(r"^# Admin audit log\..*?^AUDIT_RETENTION_DAYS = .*?$", example, re.S | re.M)
check(block is not None, "deploy.config.example has the audit block")
if block:
    price_texts.append(("deploy.config.example audit block", block.group(0)))
for jsx in sorted((ROOT / "admin-console/src/components").glob("Audit*.jsx")):
    price_texts.append((jsx.relative_to(ROOT).as_posix(), jsx.read_text(encoding="utf-8")))
priced = [label for label, text in price_texts if PRICE.search(text)]
check(len(price_texts) >= 12, f"the audit strings are all inspected ({len(price_texts)})")
check(not priced, f"no price figure in any audit string ({priced})")
readme = (ROOT / "README.md").read_text(encoding="utf-8")
drivers = readme.split("\n## Indicative costs\n", 1)[1].split("\n## ", 1)[0] if "\n## Indicative costs\n" in readme else ""
audit_lines = [ln for ln in drivers.splitlines() if "audit" in ln.lower()]
check(bool(audit_lines), "the README's Indicative costs names the audit vault bucket")
check(not any(PRICE.search(ln) for ln in audit_lines), "and quotes no price for it")

print("")
print("══ Secrets Manager reads are scoped, never '*' ══")
# A wildcard here lets anything holding the role read every secret in the account,
# including secrets belonging to unrelated applications. Every role that reads database
# credentials is scoped to this deployment's own namespaces; cross-account secrets are
# reached by assuming the per-space access role first.
secret_stmts = 0
for rel in ("infrastructure/api.yaml", "backend/template.yaml"):
    for role, policy, st in iam_statements(rel):
        actions = [a for a in as_list(st.get("Action")) if isinstance(a, str)]
        if "secretsmanager:GetSecretValue" not in actions:
            continue
        secret_stmts += 1
        resources = as_list(st.get("Resource"))
        label = f"{rel.split('/')[-1]}:{role}/{policy}"
        check(resources != ["*"], f"{label}: not granted on Resource '*'")
        check(any(isinstance(r, str) and "${ResourcePrefix}/" in r for r in resources),
              f"{label}: scoped to the ResourcePrefix-derived secret namespace")
check(secret_stmts >= 4,
      f"all four secret-reading roles are covered (found {secret_stmts})")

# The section above only ever read CloudFormation, and the role a customer creates in
# their own account is not defined there alone. It has three definitions - the template
# they can deploy, the policy the console hands them to paste, and the one
# /api/agent-role-policy returns - and the console's copy granted secretsmanager on '*',
# so an operator who followed the instructions built the widest of the three. Scoping
# only the templates leaves the instructions handing out what the templates refuse.
print("")
print("══ the same scoping in the policies we hand the operator to paste ══")
for rel, marker in (
    # the block the console shows for a hand-created member-account role; the read is
    # pinned to the member account the role lives in, never any account
    ("admin-console/src/components/SpaceDetail.jsx", "${accountId || '*'}:secret:${prefix}/${ownerAdmin || '*'}/"),
    # the policy create_space attaches and the console renders, pinned the same way: the
    # role's own account AND the owning admin's secret namespace <prefix>/<owner_admin>/
    ("backend/app.py", "{account_id or '*'}:secret:{resource_prefix()}/{owner_admin or '*'}/"),
):
    body = (ROOT / rel).read_text(encoding="utf-8")
    name = rel.split("/")[-1]
    check("secretsmanager:GetSecretValue" in body,
          f"{name}: grants GetSecretValue (so this check is looking at the right file)")
    # the scoped resource must appear...
    check(f"secretsmanager:*:{marker}" in body,
          f"{name}: GetSecretValue is scoped to the role's own account and the owning admin's secret namespace")
    check("secretsmanager:*:*:secret:" not in body,
          f"{name}: no GetSecretValue resource spans every account")
    # ...and no statement may pair GetSecretValue with a bare '*'
    wildcard = re.search(
        r'secretsmanager:GetSecretValue[^}]{0,400}?"Resource"\s*:\s*"\*"', body)
    check(wildcard is None,
          f"{name}: no GetSecretValue statement falls back to Resource '*'")

print("")
print("══ every S3 bucket refuses plaintext HTTP (AwsSolutions-S10) ══")
# Encryption covered these objects at rest while S3 still answered plain HTTP. The
# guard enumerates every bucket rather than a fixed list, so one added later
# without a TLS statement fails.
bucket_templates = (
    "infrastructure/frontend.yaml",
    "infrastructure/admin-console.yaml",
    "infrastructure/master.yaml",
    "infrastructure/storage.yaml",
)
total_buckets = 0
for rel in bucket_templates:
    doc = load(rel)
    resources = doc.get("Resources", {}) or {}
    buckets = [n for n, r in resources.items()
               if isinstance(r, dict) and r.get("Type") == "AWS::S3::Bucket"]
    # Map each BucketPolicy to the bucket it targets, via its Bucket: !Ref <name>.
    protected = set()
    for res in resources.values():
        if not isinstance(res, dict) or res.get("Type") != "AWS::S3::BucketPolicy":
            continue
        props = res.get("Properties", {}) or {}
        target = props.get("Bucket")
        if not isinstance(target, str):
            continue
        for st in (props.get("PolicyDocument", {}) or {}).get("Statement", []) or []:
            if not isinstance(st, dict) or st.get("Effect") != "Deny":
                continue
            cond = st.get("Condition", {}) or {}
            bool_block = cond.get("Bool", {}) or {}
            # Exactly aws:SecureTransport, nothing else. An earlier revision
            # checked only that the key was present and passed while the control
            # was dead: a stray aws:SourceAccount in the same block makes Bool
            # unsatisfiable. cfn-lint and cdk-nag both missed it; the shape is
            # the only tell.
            if list(bool_block) != ["aws:SecureTransport"]:
                continue
            if str(bool_block["aws:SecureTransport"]).lower() != "false":
                continue
            if st.get("Principal") != "*":
                continue
            if "s3:*" in as_list(st.get("Action")):
                protected.add(target)
    name = rel.split("/")[-1]
    for b in buckets:
        total_buckets += 1
        check(b in protected,
              f"{name}:{b}: denies s3:* when aws:SecureTransport is false")
check(total_buckets >= 7,
      f"all buckets across the four templates are covered (found {total_buckets})")

# The CloudTrail aws:SourceAccount condition was clobbered once by an insertion
# that landed mid-statement. Any statement letting an AWS service write to a
# bucket must name the account it may act for, or any account's copy of that
# service can deliver into it.
service_writers = 0
for rel in bucket_templates:
    for name, res in (load(rel).get("Resources", {}) or {}).items():
        if not isinstance(res, dict) or res.get("Type") != "AWS::S3::BucketPolicy":
            continue
        for st in ((res.get("Properties", {}) or {})
                   .get("PolicyDocument", {}) or {}).get("Statement", []) or []:
            if not isinstance(st, dict) or st.get("Effect") != "Allow":
                continue
            principal = st.get("Principal")
            svc = principal.get("Service") if isinstance(principal, dict) else None
            if not svc:
                continue
            service_writers += 1
            se = (st.get("Condition", {}) or {}).get("StringEquals", {}) or {}
            label = f"{rel.split('/')[-1]}:{name}/{st.get('Sid', '?')}"
            check("aws:SourceAccount" in se or "AWS:SourceArn" in se,
                  f"{label}: constrains {svc} to this account "
                  f"(SourceAccount or SourceArn), got {sorted(se)}")
check(service_writers >= 4,
      f"every service-principal grant is checked (found {service_writers})")

print("")
print("══ no role carries CloudWatchLogsFullAccess (AwsSolutions-IAM4) ══")
# CloudWatchLogsFullAccess grants logs:* account-wide, including DeleteLogGroup
# over this application's own CloudTrail and API Gateway logs - so a compromised
# API task could erase the record of what it did. Replaced with inline policies
# carrying no Delete action, writes confined to this deployment's log groups.
for rel in ("infrastructure/api.yaml", "backend/template.yaml"):
    name = rel.split("/")[-1]
    # Read ManagedPolicyArns rather than grepping the text, so that the prose
    # explaining why the policy was removed does not itself trip the guard.
    attached = []
    for res in (load(rel).get("Resources", {}) or {}).values():
        if not isinstance(res, dict) or res.get("Type") != "AWS::IAM::Role":
            continue
        for arn in as_list((res.get("Properties", {}) or {}).get("ManagedPolicyArns")):
            if isinstance(arn, str) and "CloudWatchLogsFullAccess" in arn:
                attached.append(arn)
    check(not attached,
          f"{name}: no role attaches the account-wide CloudWatchLogsFullAccess policy")
    scoped = [st for _r, pol, st in iam_statements(rel) if pol == "CloudWatchLogsScoped"]
    check(bool(scoped), f"{name}: has a CloudWatchLogsScoped inline policy instead")
    writes = [st for st in scoped
              if "logs:PutLogEvents" in as_list(st.get("Action"))]
    check(bool(writes), f"{name}: the scoped policy still grants logs:PutLogEvents")
    for st in writes:
        check(as_list(st.get("Resource")) != ["*"],
              f"{name}: log writes are not granted on Resource '*'")
    deletes = [a for st in scoped for a in as_list(st.get("Action"))
               if isinstance(a, str) and a.lower().startswith("logs:delete")]
    check(not deletes,
          f"{name}: the scoped policy grants no logs:Delete* action ({deletes or 'none'})")

print("")
print("══ the metrics store has no cluster, no credential and no database login ══")
# The metrics store is two DynamoDB tables plus CloudWatch metrics: there is no master
# credential to rotate, no rds-db:connect grant to scope, and no endpoint to reach.
# Guarded so a later change cannot quietly bring a database login back onto a runtime path.
check(not (ROOT / "infrastructure/aurora-store.yaml").exists(),
      "there is no Aurora metrics-store template")
for rel in ("infrastructure/api.yaml", "backend/template.yaml", "infrastructure/master.yaml"):
    body = (ROOT / rel).read_text(encoding="utf-8")
    check("rds-db:connect" not in body, f"{rel.split('/')[-1]}: grants no rds-db:connect")
    check("aurora-credentials" not in body, f"{rel.split('/')[-1]}: reads no store credential secret")

# Nothing may carry a store credential ARN to a runtime component, nor to the
# local-development env files.
for rel in ("infrastructure/api.yaml", "backend/template.yaml",
            "deployment/generate-env.py", "backend/.env.example"):
    body = (ROOT / rel).read_text(encoding="utf-8")
    check("AURORA_SECRET_ARN" not in body,
          f"{rel.split('/')[-1]}: does not carry the master credential ARN")

print("")
print("══ the service principal is pinned to one scope (t-c6d06521) ══")
# is_m2m_principal() treats a scoped, user-less access token as the Learning Agent,
# and that check is the only authorization on /api/learn. The scope allowlist that
# narrows it to the intended client was supported in code but never set, so the loose
# form shipped: any user-less scoped token qualified. The value has to agree in three
# places - the resource server that defines the scope, the scope the learning agent
# asks for, and the allowlist the API enforces - and a mismatch fails closed by
# rejecting the legitimate caller, which is why it is worth a test rather than a comment.
task_env = {}
for res in (load("infrastructure/api.yaml").get("Resources", {}) or {}).values():
    if not isinstance(res, dict) or res.get("Type") != "AWS::ECS::TaskDefinition":
        continue
    for c in (res.get("Properties", {}) or {}).get("ContainerDefinitions", []) or []:
        for e in c.get("Environment", []) or []:
            if isinstance(e, dict) and isinstance(e.get("Name"), str):
                task_env[e["Name"]] = e.get("Value")

enforced = task_env.get("M2M_ALLOWED_SCOPES")
check(bool(enforced),
      f"the API task sets M2M_ALLOWED_SCOPES, so the allowlist is active ({enforced!r})")

agent_tpl = load("backend/template.yaml")
requested = None
for res in (agent_tpl.get("Resources", {}) or {}).values():
    if not isinstance(res, dict) or res.get("Type") != "AWS::Lambda::Function":
        continue
    env = ((res.get("Properties", {}) or {}).get("Environment", {}) or {}).get("Variables", {}) or {}
    if "M2M_SCOPE" in env:
        requested = env["M2M_SCOPE"]
check(requested is not None,
      f"the learning agent declares the scope it requests ({requested!r})")
check(enforced == requested,
      f"the enforced allowlist matches the requested scope ({enforced!r} vs {requested!r})")

cognito_text = (ROOT / "infrastructure/cognito.yaml").read_text(encoding="utf-8")
check(isinstance(enforced, str) and enforced in cognito_text,
      f"the scope exists on the Cognito resource server ({enforced!r})")

print("")
print("══ model-authored SQL is planned, never executed (t-62336eb2) ══")
# A threat model run rated this CRITICAL: an operator crafts a prompt that steers the
# LLM into calling get_query_plan with SQL that reaches a sensitive catalog through a
# view, leaking credential hashes. The blocklist, the no-multi-statement rule and the
# ban on Unicode-escape identifiers close the direct routes, but none of them can see
# through a view - you cannot know statically what a view selects from.
#
# What actually defeats it is that EXPLAIN without ANALYZE does not run the query. It
# returns a plan: column names and row estimates, not rows. Adding ANALYZE for a better
# plan is a natural-looking change that would turn a blocked read into an executed one,
# so it is asserted rather than left to a comment.
db_tools = (ROOT / "backend/tools/db_tools.py").read_text(encoding="utf-8")

check("EXPLAIN ANALYZE" not in db_tools.upper().replace("(ANALYZE", "ANALYZE"),
      "no tool runs EXPLAIN ANALYZE, so a planned query is never executed")
plan_fn = db_tools[db_tools.index("def get_query_plan"):]
plan_fn = plan_fn[:plan_fn.index("\ndef ", 1)]
explains = re.findall(r"EXPLAIN[^\"'}]*", plan_fn)
check(bool(explains), f"get_query_plan issues EXPLAIN ({len(explains)} strategies)")
check(not any("ANALYZE" in e.upper() for e in explains),
      "none of its EXPLAIN strategies carries ANALYZE")
check("_reject_unsafe_read_sql(query_text)" in plan_fn,
      "it passes model-authored SQL through the shared safety gate")
check(re.search(r"re\.match\(r'\^\\s\*\(WITH\\s\+\|SELECT\\s\)'", plan_fn) is not None
      or "Only SELECT queries can be explained" in plan_fn,
      "it refuses anything that is not SELECT or WITH ... SELECT")

# The gate is only meaningful if it stays the single entry point for model SQL.
sql_takers = re.findall(r"def (\w+)\([^)]*query_text", db_tools)
check(sql_takers == ["get_query_plan"],
      f"get_query_plan is still the only tool taking SQL from the model ({sql_takers})")
for needed in ("pg_authid", "pg_shadow", "pg_read_file", "dblink", "query_to_xml"):
    check(needed in db_tools,
          f"the sensitive-catalog blocklist still names {needed}")

print("")
print("══ the deep-scan history survives a teardown; the raw samples expire ══")
# The Tier 2 table is the long-term scan history every surface reads; like the
# registries it is retained on stack delete and has point-in-time recovery. The Tier 1
# table is a rolling window: its items carry a TTL, and the series outlives them in
# CloudWatch, so it is not retained.
storage = load("infrastructure/storage.yaml").get("Resources", {}) or {}
t2 = storage.get("Tier2ScansTable", {}) or {}
t1 = storage.get("Tier1MetricsTable", {}) or {}
check(t2.get("DeletionPolicy") == "Retain" and t2.get("UpdateReplacePolicy") == "Retain",
      "Tier2ScansTable is retained on delete and replace")
check(((t2.get("Properties") or {}).get("PointInTimeRecoverySpecification") or {})
      .get("PointInTimeRecoveryEnabled") is True, "Tier2ScansTable has point-in-time recovery")
check(((t1.get("Properties") or {}).get("TimeToLiveSpecification") or {}) ==
      {"AttributeName": "expires_at", "Enabled": True},
      "Tier1MetricsTable expires its items on expires_at")
for name, tbl in (("Tier1MetricsTable", t1), ("Tier2ScansTable", t2)):
    check(((tbl.get("Properties") or {}).get("SSESpecification") or {}).get("SSEEnabled") is True,
          f"{name} is encrypted at rest")

print("")
print("══ no template contains a credential literal (CKV_SECRET_6) ══")
# checkov flags backend/template.yaml for a high-entropy string, and the reason it is
# suppressed cannot be confirmed from a scanner report - reports redact the matched
# value, so a reviewer is left taking the justification on trust. Three separate
# reviews raised it for exactly that reason. This replaces the prose with something
# runnable: the match is a line of Python inside an inline Lambda that reads
# resp["UserPoolClient"]["ClientSecret"] from a describe_user_pool_client response,
# and there is no credential literal anywhere in any template. The assertion is worth
# keeping on its own merits, because pasting a real secret into a template is the
# thing the rule exists to catch.
cred_literal = re.compile(
    r"""(?ix)
    (client_?secret|password|passwd|secret_?key|api_?key|access_?key|token)
    \s*[:=]\s*
    ['"][A-Za-z0-9+/=_\-]{12,}['"]
    """)
# Placeholders and CloudFormation dynamic references are not literals.
ALLOWED = ("{{resolve:", "!Ref", "!Sub", "!GetAtt", "os.environ", "REPLACE_ME",
           "example", "EXAMPLE", "xxxx", "123456789012")
scanned = 0
for tpl in sorted(list((ROOT / "infrastructure").glob("*.yaml"))
                  + [ROOT / "backend/template.yaml"]):
    body = tpl.read_text(encoding="utf-8")
    scanned += 1
    hits = [(body.count("\n", 0, m.start()) + 1, m.group(0))
            for m in cred_literal.finditer(body)
            if not any(a in m.group(0) for a in ALLOWED)]
    check(not hits,
          f"{tpl.name}: no credential literal ({hits if hits else 'none'})")
# 9 nested stack templates (the metrics store is DynamoDB + CloudWatch, which need no stack).
check(scanned >= 9, f"every template was scanned (found {scanned})")

# And the specific line checkov reports must stay a read, not become an assignment
# of something literal.
agent_body = (ROOT / "backend/template.yaml").read_text(encoding="utf-8")
check('_client_secret = resp["UserPoolClient"]["ClientSecret"]' in agent_body,
      "the flagged line is still a read from a describe_user_pool_client response")
check("GenerateSecret: true" in (ROOT / "infrastructure/cognito.yaml").read_text(encoding="utf-8"),
      "the M2M client secret is generated by Cognito, so no template needs to hold it")

print("")
print("══ deep scans are bounded and deduplicated ══")
# A Tier-2 scan runs ~90 psql queries against a customer production DB. Three bounds close
# the DoS: a per-query statement_timeout on the shell path, a whole-job timeout, and a
# one-in-flight dedup. The trigger stays operator-accessible by design (_can_view_instance,
# a membership-based READ gate — not the _can_manage_space WRITE gate), so an operator can
# still scan a database in any of its member spaces and the DoS is closed by the bounds,
# not by removing the button.
tmpl = load("backend/template.yaml")
jobdef = next((r for r in (tmpl.get("Resources", {}) or {}).values()
               if isinstance(r, dict) and r.get("Type") == "AWS::Batch::JobDefinition"), None)
check(jobdef is not None, "the Batch scan job definition is present")
if jobdef:
    dur = ((jobdef.get("Properties", {}) or {}).get("Timeout", {}) or {}).get("AttemptDurationSeconds")
    check(isinstance(dur, int) and dur > 0,
          f"the scan job has a hard AttemptDurationSeconds bound ({dur!r})")

run_scan = (ROOT / "batch/run_scan.py").read_text(encoding="utf-8")
check("PGOPTIONS" in run_scan and "statement_timeout=" in run_scan,
      "run_scan bounds every shell psql session via PGOPTIONS statement_timeout")

app_body = (ROOT / "backend/app.py").read_text(encoding="utf-8")
trig = app_body[app_body.index("async def trigger_scan("):]
trig = trig[:trig.index("\n@app.")]
# The submit + one-in-flight dedup now live in the shared _submit_scan_job helper (used by
# both the scan endpoint and the first-onboard auto-scan), so trigger_scan delegates to it.
submit = app_body[app_body.index("def _submit_scan_job("):]
submit = submit[:submit.index("\n@app.")]
# The lookup lives in _scan_jobs_for (one filtered, paginated ListJobs); behaviour is
# pinned in backend/tests/test_registry_pagination.py.
check("_submit_scan_job(" in trig and "_scan_jobs_for(" in submit and "409" in submit and "list_jobs(" in app_body,
      "trigger_scan rejects a duplicate scan while one is already active (409)")
check("_can_view_instance(" in trig and "_can_manage_space(" not in trig,
      "the scan trigger stays operator-accessible (_can_view_instance), by decision")

print("")
print("══ batch:SubmitJob is scoped, not account-wide ══")
# A direct Batch submit (bypassing the app) is the concern. SubmitJob on the two agent-stack
# roles must be scoped to this deployment's queue + job definition, not Resource '*'.
def _submitjob_resources(doc):
    out = []
    for r in (doc.get("Resources", {}) or {}).values():
        if not isinstance(r, dict) or r.get("Type") != "AWS::IAM::Role":
            continue
        for pol in ((r.get("Properties", {}) or {}).get("Policies", []) or []):
            for st in (((pol.get("PolicyDocument", {}) or {}).get("Statement", []) or [])):
                if not isinstance(st, dict):
                    continue
                actions = st.get("Action", [])
                actions = actions if isinstance(actions, list) else [actions]
                if "batch:SubmitJob" in actions:
                    out.append(st.get("Resource"))
    return out

subs = _submitjob_resources(load("backend/template.yaml"))
check(len(subs) >= 2, f"both agent-stack roles' SubmitJob statements are present ({len(subs)})")
for res in subs:
    aslist = res if isinstance(res, list) else [res]
    check("*" not in aslist and res != "*", f"SubmitJob is not Resource '*' ({res})")
    check("BatchJobQueue" in aslist and "BatchJobDefinition" in aslist,
          f"SubmitJob is scoped to the queue and job definition ({res})")

api_src = (ROOT / "infrastructure/api.yaml").read_text(encoding="utf-8")
# The SubmitJob statement ends at its AWS::NoValue else-branch; check that span only, so
# the '*' in the following ListJobs/DescribeJobs comment isn't mistaken for a fallback.
seg = api_src.split("batch:SubmitJob", 1)[1].split("AWS::NoValue", 1)[0]
check("'*'" not in seg, "api.yaml SubmitJob has no Resource '*' fallback")
check("AWS::NoValue" in api_src.split("batch:SubmitJob", 1)[1].split("batch:ListJobs", 1)[0],
      "api.yaml omits the SubmitJob grant (AWS::NoValue) when batch is disabled")

print("")
print("══ the batch scan trusts only INSTANCE_ID ══")
# Every security-relevant field is re-read from the registry record, so a directly-submitted
# job cannot point the scan at a credential/host/output-key of its choosing.
rs = (ROOT / "batch/run_scan.py").read_text(encoding="utf-8")
main_seg = rs.split("def main(", 1)[1]
check("get_instance_info(instance_id)" in main_seg and "refusing to scan" in main_seg,
      "run_scan loads the registry record by INSTANCE_ID first and refuses if it is missing")
check('instance_info.get("secret_arn"' in main_seg,
      "run_scan takes secret_arn from the registry record, not the job env")
check("disagrees with the registry record" in main_seg,
      "run_scan refuses when a submitted SECRET_ARN/ACCOUNT_ID disagrees with the record")
check('endpoint = instance_info.get("endpoint")' in rs and 'os.getenv("DB_HOST"' not in rs
      and 'os.getenv("PRIVATELINK_ENDPOINT"' not in rs,
      "the scan host comes from the registry record's endpoint, never from the job env")

print("")
print("══ the batch scan cannot rewrite a DB's connection target ══")
# The scan's only registry write is a status stamp on its own row (update_dynamodb:
# last_scan_at / last_health_score / updated_at). The connection-routing fields
# (privatelink_endpoint, endpoint, secret_arn, ...) are set by the admin Configure flow,
# never by a scan, so the job role's UpdateItem is held to those status attributes by a
# dynamodb:Attributes condition. A compromised scan container therefore cannot plant a
# rogue host/secret on any row and redirect another database's next scan to a server it
# controls. This asserts the condition is present with the right whitelist and that the
# two Batch roles are split; DynamoDB's attribute-level enforcement itself cannot be
# exercised without a live AWS call.
resources = (tmpl.get("Resources", {}) or {})
job_role = resources.get("BatchJobRole")
exec_role = resources.get("BatchExecutionRole")
check(isinstance(job_role, dict), "BatchJobRole exists (the scan's application identity)")
check(isinstance(exec_role, dict), "BatchExecutionRole exists (the image-pull/log identity)")

# reader_scan_outcomes: the reader leg's per-member outcome, a status field the members
# view shows; nothing reads it to route a connection.
ALLOWED_ATTRS = {"instance_id", "last_scan_at", "last_health_score", "updated_at", "reader_scan_outcomes",
                 "last_scan_error"}
ROUTING_ATTRS = {"privatelink_endpoint", "endpoint", "secret_arn",
                 "reach_space_id", "reach_role_arn", "account_id"}


def _role_statements(role):
    out = []
    for pol in (((role or {}).get("Properties", {}) or {}).get("Policies", []) or []):
        for st in (((pol.get("PolicyDocument", {}) or {}).get("Statement", []) or [])):
            if isinstance(st, dict):
                out.append(st)
    return out


def _acts(st):
    a = st.get("Action", [])
    return a if isinstance(a, list) else [a]


job_stmts = _role_statements(job_role)
update_stmts = [st for st in job_stmts if "dynamodb:UpdateItem" in _acts(st)]
check(len(update_stmts) == 1,
      f"exactly one statement grants dynamodb:UpdateItem ({len(update_stmts)})")
if update_stmts:
    st = update_stmts[0]
    attrs = (((st.get("Condition", {}) or {}).get("ForAllValues:StringEquals", {}) or {})
             .get("dynamodb:Attributes"))
    attrs = attrs if isinstance(attrs, list) else ([attrs] if attrs else [])
    attrset = set(attrs)
    check(attrset == ALLOWED_ATTRS,
          f"UpdateItem is held to the status attributes only ({sorted(attrset)})")
    check(not (attrset & ROUTING_ATTRS),
          "no connection-routing attribute is writable by the scan")
    check("dynamodb:GetItem" not in _acts(st),
          "UpdateItem and GetItem are separate statements (the condition binds only the write)")

getitem_stmts = [st for st in job_stmts if "dynamodb:GetItem" in _acts(st)]
check(len(getitem_stmts) == 1 and "dynamodb:UpdateItem" not in _acts(getitem_stmts[0]),
      "GetItem is a separate, unconditioned read statement")

# Execution vs job role split: the job definition runs under two distinct roles, and the
# execution (image-pull/log) identity holds none of the application grants.
cp = ((jobdef.get("Properties", {}) or {}).get("ContainerProperties", {}) or {}) if jobdef else {}
exec_arn = cp.get("ExecutionRoleArn")
job_arn = cp.get("JobRoleArn")
check(exec_arn == "BatchExecutionRole.Arn",
      f"job definition ExecutionRoleArn is the minimal execution role ({exec_arn!r})")
check(job_arn == "BatchJobRole.Arn",
      f"job definition JobRoleArn is the application job role ({job_arn!r})")
check(exec_arn != job_arn, "execution and job roles are distinct")

APP_ACTIONS = {"secretsmanager:GetSecretValue", "sts:AssumeRole", "dynamodb:UpdateItem",
               "dynamodb:GetItem", "s3:PutObject", "s3:GetObject", "bedrock:InvokeModel",
               "sns:Publish", "rds-db:connect", "dynamodb:PutItem",
               "dynamodb:Query", "cloudwatch:PutMetricData", "cloudwatch:GetMetricData"}
exec_actions = {a for st in _role_statements(exec_role) for a in _acts(st)}
check(not (exec_actions & APP_ACTIONS),
      f"the execution role holds no application grants ({sorted(exec_actions & APP_ACTIONS)})")

print("")
print("══ DB reads/authz are membership-based (DB-level tenancy) ══")
# A physical DB is one record shared across the spaces in member_space_ids; access is
# granted to any member space, not a single owner.
check("def _instance_member_spaces(" in app_body and "member_space_ids" in app_body,
      "app resolves a DB's member spaces (member_space_ids, falling back to tags.space_id)")
check("def _can_view_instance(" in app_body,
      "app has a DB-level read gate spanning all member spaces")
verify_seg = app_body[app_body.index("def _verify_instance_in_space("):]
verify_seg = verify_seg[:verify_seg.index("\n\ndef ")]
check("_instance_member_spaces(instance)" in verify_seg and "space_id not in members" in verify_seg,
      "_verify_instance_in_space authorizes by membership, not a single tags.space_id owner")

print("")
print("══ scans reach the DB via its reach provider (DB-level) ══")
# The scan runs under the DB's reach provider's role; the sts:ExternalId it presents is
# the id of the admin who owns that space, denormalised onto the record as
# reach_owner_admin when reach is set. Both unattended paths read it from the trusted
# record, never from a job parameter.
t1_src = (ROOT / "backend/tier1_collector.py").read_text(encoding="utf-8")
check('record.get("reach_space_id"' in t1_src and 'record.get("reach_owner_admin"' in t1_src,
      "tier1_collector resolves the role from reach_space_id and the ExternalId from reach_owner_admin")
check('instance_info.get("reach_owner_admin"' in main_seg and "external_id=" in main_seg,
      "run_scan presents the record's reach_owner_admin as sts:ExternalId")
check("ExternalId" in rs,
      "run_scan's cross-account assume presents sts:ExternalId when set")

print("")
print("══ the report-comparison summary fences scan text into the prompt ══")
# compare_summary feeds database-derived report content (findings, summaries) into a model
# prompt. That text is untrusted, so it must be fenced as data rather than concatenated into
# the instructions, where a crafted finding could steer the summary. The guardrail on the
# call is response-only; the fence is the input-side control.
cs = app_body.split("def compare_summary(", 1)[1].split("\n@app.", 1)[0]
check("<SCAN_DATA>" in cs and "</SCAN_DATA>" in cs,
      "compare_summary fences the report content between <SCAN_DATA> markers")
check("as DATA" in cs,
      "compare_summary tells the model to treat the fenced content as data, not instructions")
check("guardrail_invoke_kwargs_from_env()" in cs,
      "compare_summary still attaches the guardrail to the invoke_model call")

print("")
print("══ the learn endpoint derives space_id from the DB record, not the query param ══")
# run_learning_agent stamps each recommendation with space_id. Trusting the caller-supplied
# query param (the M2M Learning Agent has no membership; a human's space is not the DB's
# canonical one) could attribute a recommendation to a space that should not see it. Derive
# it from the record instead.
learn = app_body.split("async def run_learning_agent(", 1)[1].split("\n@app.", 1)[0]
check('space_id = instance.get("reach_space_id", "")' in learn,
      "run_learning_agent derives space_id from the DB record (reach provider)")
check("if not space_id:\n        space_id = owner_space" not in learn,
      "the old 'trust the query param, else owner' derivation is gone")

# Recommendations are DB-level, so a space's recommendations view is resolved by the
# databases it is a member of, not by a single frozen space stamp on each record (which
# showed a shared DB's recs only to its reach space). Same membership axis.
recs_list = app_body.split("def list_recommendations(", 1)[1].split("\ndef ", 1)[0]
check("_instance_member_spaces(inst)" in recs_list and "list_for_instance(" in recs_list,
      "list_recommendations returns a space's member-DB recommendations (membership-based)")
check("list_for_space(" not in recs_list,
      "list_recommendations no longer filters by the single space stamp")
recs_upd = app_body.split("def update_recommendation_status(", 1)[1].split("\ndef ", 1)[0]
check('rec.get("space_id") != space_id' not in recs_upd and "_instance_member_spaces(inst)" in recs_upd,
      "update_recommendation_status gates by DB membership, not the frozen space stamp")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
