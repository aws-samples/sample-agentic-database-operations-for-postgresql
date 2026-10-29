#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Pins deployment/check-orphans.sh to the templates.

check-orphans.sh decides whether a deploy may proceed. It compares against the exact names
the deployment creates rather than everything starting with "<prefix>-": RESOURCE_PREFIX
exists so two installations can share an account, and a prefix match would break that
whenever one prefix is a string-prefix of another (deploying "dbops" would match
"dbops-test-reports-…" and refuse to continue over resources it never created).

Matching exact names is only correct while those lists agree with the templates. A template
gaining a bucket or renaming a table would silently stop the check from seeing a real
collision, and the failure would surface later as CloudFormation's unreadable "Validation
failed with N error(s)".

So this re-derives every name from the templates and compares. Adding a resource to a
template fails here until check-orphans.sh is updated to match.

    python3 tests/test_orphan_names.py      (from the repository root)
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deployment/check-orphans.sh"
NAMES = ROOT / "deployment/orphan-names.sh"           # the shared exact-name lists
PURGE = ROOT / "deployment/destroy/purge-orphans.sh"  # the destructive half
TEMPLATES = sorted((ROOT / "infrastructure").glob("*.yaml")) + [ROOT / "backend/template.yaml"]

passed = failed = 0


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
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_Loader.add_multi_constructor("!", _keep_scalar)


def declared(res_type, prop):
    """Every literal value of Properties.<prop> on resources of <res_type>."""
    out = set()
    for path in TEMPLATES:
        doc = yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader) or {}  # nosec B506
        for _lid, res in (doc.get("Resources") or {}).items():
            if res.get("Type") != res_type:
                continue
            value = (res.get("Properties") or {}).get(prop)
            if isinstance(value, str):
                out.add(value)
    return out


def normalise(name):
    """Reduce a template !Sub name to the shape check-orphans.sh builds.

    ${ResourcePrefix} -> P, ${AWS::AccountId} -> A, ${AWS::StackName} -> S, so a
    template string and a shell string can be compared without running either.
    """
    name = name.replace("${ResourcePrefix}", "P")
    name = name.replace("${AWS::AccountId}", "A")
    name = name.replace("${AWS::StackName}", "S")
    name = name.replace("${AWS::Region}", "R")
    return name.strip().strip("'\"")


def shell_array(var):
    """Values of a bash array literal in orphan-names.sh, in the same normal form."""
    body = NAMES.read_text(encoding="utf-8")
    m = re.search(rf"^{var}=\((.*?)\)\s*$", body, re.S | re.M)
    if not m:
        return None
    items = re.findall(r'"([^"]+)"', m.group(1))
    return {
        i.replace("${PREFIX}", "P").replace("${ACCOUNT}", "A").replace("${STACK}", "S")
         .replace("${REGION}", "R")
        for i in items
    }


def shell_scalar(var):
    body = NAMES.read_text(encoding="utf-8")
    m = re.search(rf'^{var}="([^"]+)"\s*$', body, re.M)
    return m.group(1).replace("${PREFIX}", "P") if m else None


print("══ the script cannot delete anything ══")
body = SCRIPT.read_text(encoding="utf-8")
# The whole reason check and purge are separate files.
for verb in ("delete-bucket", "delete-table", "delete-secret", "delete-db-cluster",
             "delete-user-pool", "delete-log-group", "rm -rf", "s3 rb"):
    check(verb not in body, f"no {verb!r} in a file called check-*")

print("")
print("══ names match the templates ══")

for label, res_type, prop, var in (
    ("S3 buckets", "AWS::S3::Bucket", "BucketName", "BUCKETS"),
    ("DynamoDB tables", "AWS::DynamoDB::Table", "TableName", "TABLES"),
):
    want = {normalise(v) for v in declared(res_type, prop)}
    got = shell_array(var)
    check(got is not None, f"{var} array is present in orphan-names.sh")
    if got is not None:
        check(want == got,
              f"{label}: {len(want)} declared, script checks {len(got)}"
              + ("" if want == got else f" - missing {sorted(want - got)}, extra {sorted(got - want)}"))

# Single-name resources.
for label, res_type, prop, var in (
    ("Cognito user pool", "AWS::Cognito::UserPool", "UserPoolName", "USER_POOL"),
):
    want = {normalise(v) for v in declared(res_type, prop)}
    got = shell_scalar(var)
    check(got in want, f"{label}: script checks {got!r}, template declares {sorted(want)}")

# Log groups: the templates declare four; the rest are created by Lambda on first
# invoke and so are not in any template. Only the declared four are asserted here.
declared_lgs = {normalise(v) for v in declared("AWS::Logs::LogGroup", "LogGroupName")}
script_lgs = shell_array("LOG_GROUPS")
check(script_lgs is not None, "LOG_GROUPS array is present")
if script_lgs is not None:
    missing = declared_lgs - script_lgs
    check(not missing, f"every template log group is checked (missing {sorted(missing)})")
    # Each Lambda in the FUNCTIONS registry writes to /aws/lambda/<prefix>-<name>.
    registry = (ROOT / "backend/tools/resource_names.py").read_text(encoding="utf-8")
    fn_block = re.search(r"FUNCTIONS = \{(.*?)\}", registry, re.S)
    check(fn_block is not None, "FUNCTIONS registry found in resource_names.py")
    if fn_block:
        fns = re.findall(r'"([a-z0-9-]+)"\)', fn_block.group(1))
        for fn in fns:
            check(f"/aws/lambda/P-{fn}" in script_lgs,
                  f"lambda log group for {fn!r} is checked")

# The legacy Aurora metrics store: no template declares it any more, but a deployment
# that ran the old stack can still hold the cluster, its snapshots and its two fixed-name
# Lambda log groups. They are named exactly (never by prefix) and only ever noted.
check(shell_scalar("LEGACY_CLUSTER") == "P-metrics",
      "LEGACY_CLUSTER names the old store's exact cluster identifier")
legacy_lgs = shell_array("LEGACY_LOG_GROUPS") or set()
check(legacy_lgs == {"/aws/lambda/P-aurora-schema-init", "/aws/lambda/P-aurora-retention"},
      f"LEGACY_LOG_GROUPS names the old store's two Lambda log groups ({sorted(legacy_lgs)})")
check(not any("aurora" in lg for lg in (script_lgs or set())),
      "no legacy log group sits in LOG_GROUPS, where it would block a deploy")
check(not declared("AWS::RDS::DBCluster", "DBClusterIdentifier"),
      "no template declares a DB cluster")

print("")
print("══ the gate fails closed ══")
# Exact matching needs the account id, because bucket names embed it - a dependency the
# prefix version did not have. If that lookup fails the script must not exit 0: the
# Makefile gates the deploy on this exit code, so a zero after checking nothing is a
# false all-clear, and the deploy proceeds into the unreadable CloudFormation error this
# script exists to replace. Found by a security diff scan on the commit that added it.
acct = re.search(r"ACCOUNT=\$\(aws sts get-caller-identity.*?\bfi\b", body, re.S)
check(acct is not None, "the account-id lookup is still present")
if acct:
    check("exit 1" in acct.group(0),
          "an unresolvable account id exits non-zero rather than reporting all-clear")
    check("exit 0" not in acct.group(0),
          "and does not exit 0 anywhere in that branch")

print("")
print("══ things that cannot collide must not block ══")
# A snapshot name embeds the deleted stack's id, so a new deploy can never clash with it
# and must not be counted as a blocker over storage that is in nobody's way. Same for a
# credential secret: the stack declares no secret at all, so nothing it creates can
# collide with one.
check("AWS::SecretsManager::Secret" not in "".join(
          p.read_text(encoding="utf-8") for p in TEMPLATES),
      "no template declares a Secret, so no secret name can collide")

snap = re.search(r"describe-db-cluster-snapshots.*?done", body, re.S)
check(snap is not None, "the snapshot check is still present")
if snap:
    check("notice " in snap.group(0) and "report " not in snap.group(0),
          "snapshots are reported via notice(), so they do not set the failing exit code")

legacy = re.search(r"list-secrets.*?done", body, re.S)
check(legacy is not None, "the legacy-secret check is still present")
if legacy:
    check("notice " in legacy.group(0) and "report " not in legacy.group(0),
          "a legacy secret is reported via notice(), not as a blocker")

print("")
print("══ matching is exact, not by prefix ══")
# The bug this file exists to prevent coming back.
check('grep "^${PREFIX}-"' not in body,
      "no bare prefix grep (that is what matched another installation's resources)")
check("in_list()" in body, "in_list() compares full names with =, not a pattern")
check("starts_with(DBClusterIdentifier" not in body,
      "the cluster is matched by equality, not starts_with")

# The reported case, asserted directly. A tester deploying prefix "dbops" was blocked by
# buckets belonging to a separate "dbops-test" installation. Under prefix P, every name
# a "P-test" install owns must be absent from what P checks for, and P's own must be
# present - which is what makes the two installations independent.
buckets = shell_array("BUCKETS") or set()
tables = shell_array("TABLES") or set()
for other in ("P-test-reports-A-R", "P-test-admin-A-R", "P-test-logs-A-R"):
    check(other not in buckets, f"a separate install's bucket {other} is not claimed")
for other in ("P-test-config", "P-test-audit"):
    check(other not in tables, f"a separate install's table {other} is not claimed")
check("P-reports-A-R" in buckets, "this install's own reports bucket IS still checked")
check("P-config" in tables, "this install's own config table IS still checked")

print("")
print("══ both scripts share one source of truth ══")
# The names live in orphan-names.sh, and each script sources it rather than carrying its
# own copy - so the report and the deletes cannot drift apart.
check("orphan-names.sh" in SCRIPT.read_text(encoding="utf-8"),
      "check-orphans.sh sources the shared name list")
purge = PURGE.read_text(encoding="utf-8")
check("orphan-names.sh" in purge, "purge-orphans.sh sources the same shared name list")

print("")
print("══ purge deletes exact names, not a prefix glob ══")
# Purge must select every deterministic resource by exact name / equality, never by a
# "^${PREFIX}-" glob — so purging one install (e.g. "dbops") cannot touch a separate
# install that shares the prefix (e.g. "dbops-test").
check('grep "^${PREFIX}-"' not in purge,
      "no bare-prefix grep for tables/buckets/log groups")
check("starts_with(Name,'${PREFIX}-')" not in purge,
      "secrets are not swept by bare prefix")
check("starts_with(DBClusterIdentifier,'${PREFIX}-')" not in purge,
      "the aurora cluster is selected by exact name, not starts_with prefix")
check("UserPools[?starts_with(Name,'${PREFIX}-')]" not in purge
      and "UserPools[?Name=='${USER_POOL}']" in purge,
      "the user pool is selected by exact name")
check("mem_prefix" not in purge,
      "the agentcore memory is matched by its exact name, not the underscored prefix")
check("${TABLES[@]}" in purge and "${BUCKETS[@]}" in purge and "${LOG_GROUPS[@]}" in purge,
      "tables, buckets and log groups are iterated from the shared exact-name arrays")
# The one unavoidable glob (a snapshot's generated suffix) is scoped to the cluster name,
# which a string-prefix sibling ("${PREFIX}-test-metrics") cannot match.
check("starts_with(DBClusterSnapshotIdentifier,'${LEGACY_CLUSTER}')" in purge,
      "aurora snapshots are scoped to the cluster identifier, not the bare prefix")

print("")
print("══ the audit vault bucket is noted, never purged ══")
# The admin audit vault holds records under Object Lock COMPLIANCE. Its name ends in the
# storage stack's id, so it never collides with a redeploy, and nothing can empty it
# before its records expire. So it is not in BUCKETS (which would block every redeploy
# and route it through the emptying loop); it is matched by an anchored pattern, only
# ever noted by the check, and deleted by teardown only once it is already empty.
names_body = NAMES.read_text(encoding="utf-8")
m = re.search(r'^AUDIT_BUCKET_RE="([^"]+)"\s*$', names_body, re.M)
check(m is not None, "AUDIT_BUCKET_RE is defined in orphan-names.sh")
if m:
    check(m.group(1) == "^${PREFIX}-audit-${ACCOUNT}-[0-9a-f]{12}\\$",
          f"AUDIT_BUCKET_RE is the anchored full-shape pattern ({m.group(1)})")
check(not any("audit" in b for b in buckets), "no audit bucket is listed in BUCKETS")
check(not any(normalise(v).startswith("P-audit") for v in declared("AWS::S3::Bucket", "BucketName")),
      "the stack-suffixed vault name is not a plain string (declared() skips the list form of !Sub)")
try:
    _re = subprocess.run(
        ["bash", "-c", '. "$0" && printf "%s" "$AUDIT_BUCKET_RE"', str(NAMES)],
        env=dict(os.environ, PREFIX="dbops", ACCOUNT="123456789012", STACK="dbops", REGION="us-east-1"),
        capture_output=True, text=True, timeout=20, check=False).stdout
except (OSError, subprocess.SubprocessError):
    _re = ""
check(_re == "^dbops-audit-123456789012-[0-9a-f]{12}$", f"sourced, it expands to an ERE ({_re!r})")
if _re:
    pat = re.compile(_re)
    check(bool(pat.search("dbops-audit-123456789012-0123456789ab")), "it matches this install's vault bucket")
    for other in ("dbops-test-audit-123456789012-0123456789ab", "dbops-audit-999999999999-0123456789ab",
                  "dbops-audit-123456789012-0123456789abc", "xdbops-audit-123456789012-0123456789ab",
                  "dbops-audit-123456789012-0123456789AB", "dbops-audits-123456789012-0123456789ab"):
        check(not pat.search(other), f"it does not match {other}")

audit_branch = re.search(r"for b in [^\n]*\n[^\n]*AUDIT_BUCKET_RE.*?\ndone", body, re.S)
check(audit_branch is not None, "check-orphans.sh lists AUDIT_BUCKET_RE matches")
if audit_branch:
    check("notice " in audit_branch.group(0) and "report " not in audit_branch.group(0),
          "a retained audit bucket is reported via notice(), so it never blocks a deploy")

block = re.search(r"# ── audit vault buckets \(empty-only\) ──(.*?)# ── end audit vault buckets ──", purge, re.S)
check(block is not None, "purge-orphans.sh has a dedicated audit-bucket block")
if block:
    text = block.group(1)
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    verbs = set(re.findall(r"\baws\s+(?:s3api|s3)\s+([a-z-]+)", code))
    check(verbs == {"list-buckets", "list-object-versions", "delete-bucket"},
          f"its only S3 calls are list-buckets / list-object-versions / delete-bucket ({sorted(verbs)})")
    check("AUDIT_BUCKET_RE" in code, "it selects buckets by AUDIT_BUCKET_RE")
    for verb in ("s3 rm", "delete-objects", "delete-object ", "rb "):
        check(verb not in code, f"it never calls {verb.strip()!r}")
    check('= "0"' in code, "it deletes a bucket only when no version or delete marker is left")

offenders = [p.relative_to(ROOT).as_posix()
             for p in sorted((ROOT / "deployment").rglob("*")) + [ROOT / "Makefile"]
             if p.is_file() and p.suffix in ("", ".sh", ".py")
             and "bypass-governance-retention" in p.read_text(encoding="utf-8", errors="ignore")]
check(not offenders, f"no script or the Makefile uses bypass-governance-retention ({offenders})")

makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
destroy = makefile.split("\ndestroy:", 1)[1].split("\nclean:", 1)[0] if "\ndestroy:" in makefile else ""
loops = re.findall(r"for (?:KEY|SUFFIX) in .*?done;", destroy, re.S)
check(len(loops) == 2, f"the destroy target's two bucket-emptying loops are found ({len(loops)})")
check(loops and not any("audit" in lp.lower() for lp in loops),
      "neither emptying loop names the audit vault bucket")
check("AuditBucketName" in destroy.split("delete-stack", 1)[0],
      "destroy reads the AuditBucketName output before deleting the stack")
audit_step = destroy.split("Stack deleted", 1)[1] if "Stack deleted" in destroy else ""
check("list-object-versions" in audit_step and "delete-bucket" in audit_step
      and "s3 rm" not in audit_step.split("check-orphans", 1)[0],
      "after the stack is gone, destroy deletes an audit bucket only via list-object-versions + delete-bucket")
check("the audit vault bucket" in destroy.split("To proceed", 1)[0],
      "the confirmation banner names the audit vault bucket among what is kept")

print("")
print("══ make destroy deletes an empty vault bucket and keeps a locked one ══")
# Driven end to end through `make destroy` with a fake aws CLI on PATH, so the shell the
# recipe actually runs (one command, sourced name list, JMESPath queries) is exercised.
FAKE_AWS = r"""#!/usr/bin/env bash
echo "aws $*" >> "$FAKE_LOG"
case "$*" in
  "sts get-caller-identity"*) echo 123456789012 ;;
  *describe-stacks*AuditBucketName*) echo dbops-audit-123456789012-abcdef012345 ;;
  *describe-stacks*StackStatus*) echo ABSENT; exit 255 ;;
  *describe-stacks*OutputKey*) echo None ;;
  *describe-stacks*) echo '{}' ;;
  *describe-db-clusters*DeletionProtection*) echo False ;;
  *"s3api head-bucket"*) exit 254 ;;
  *"s3api list-buckets"*) printf 'dbops-audit-123456789012-abcdef012345\tdbops-audit-123456789012-0123456789ab\tdbops-test-audit-123456789012-abcdef012345\n' ;;
  *"list-object-versions --bucket dbops-audit-123456789012-abcdef012345"*) echo 0 ;;
  *"list-object-versions --bucket dbops-audit-123456789012-0123456789ab"*max-keys*) echo 1 ;;
  *"list-object-versions --bucket dbops-audit-123456789012-0123456789ab"*max_by*) printf 'audit/2025/01/01/x.json\tv1\n' ;;
  *get-object-retention*"--key audit/2025/01/01/x.json --version-id v1"*) echo 2031-01-01T00:00:00+00:00 ;;
esac
exit 0
"""
if shutil.which("make") and shutil.which("bash"):
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "aws"
        fake.write_text(FAKE_AWS, encoding="utf-8")
        fake.chmod(0o755)
        log = Path(tmp) / "calls.log"
        env = dict(os.environ, PATH=f"{tmp}:{os.environ.get('PATH', '')}", FAKE_LOG=str(log))
        env.pop("AWS_PROFILE", None)
        try:
            run = subprocess.run(
                ["make", "-s", "destroy", f"CONFIG_FILE={tmp}/none.config", "RESOURCE_PREFIX=dbops",
                 "STACK_NAME=dbops", "AWS_REGION=us-east-1"],
                cwd=str(ROOT), env=env, input="dbops@123456789012\n", capture_output=True,
                text=True, timeout=60, check=False)
            out, rc = run.stdout + run.stderr, run.returncode
        except (OSError, subprocess.SubprocessError) as e:
            out, rc = str(e), None
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        check(rc == 0, f"make destroy completes (exit {rc})")
        check("deleting stack dbops" in out and any("delete-stack" in c for c in calls),
              "it gets past the confirmation and deletes the stack")
        deleted = [c for c in calls if "delete-bucket" in c]
        check(deleted == ["aws s3api delete-bucket --bucket dbops-audit-123456789012-abcdef012345 --region us-east-1"],
              f"only the empty vault bucket is deleted ({deleted})")
        check("deleted empty audit bucket dbops-audit-123456789012-abcdef012345" in out,
              "and the deletion is reported")
        check("audit bucket dbops-audit-123456789012-0123456789ab: kept until its last record's lock ends "
              "(newest record locked until 2031-01-01T00:00:00+00:00)" in out,
              "the locked bucket is kept, with its newest record's retain-until date")
        check(not any("dbops-test-audit" in c and ("delete" in c or "list-object-versions" in c) for c in calls),
              "another install's vault bucket is never touched")
        check(not any(("s3 rm" in c or "delete-object" in c) and "audit" in c for c in calls),
              "no object in a vault bucket is ever removed")
else:
    print("  skipped: make or bash not available")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
