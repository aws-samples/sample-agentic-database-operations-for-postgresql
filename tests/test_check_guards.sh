#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Prove the deploy-time checkers actually fail when their invariants are broken.
#
#   bash tests/test_check_guards.sh          (from the repository root)
#
# A checker that cannot fail is worse than no checker: it reports PASS and buys
# false confidence. Two of the guards here were written, appeared to work, and
# were only found to be inert when a test like this broke the thing they watch.
#
# Each case breaks one thing on purpose, runs the checker, asserts it fails with the
# expected message, and moves on. The final case is a control: with nothing broken,
# everything must pass.
#
# All of that happens inside a throwaway copy of the repository in a temp directory,
# never in your working tree. Editing the real files and relying on a trap to restore
# them is not equivalent: a run killed outright skips the trap and leaves a template
# broken, and the next run then backs up the already-broken file and faithfully restores
# the breakage. Copying first removes the whole class of problem, because the only files
# these tests can damage are ones that get deleted seconds later.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Re-run inside a copy, once.
if [ -z "${GUARD_SANDBOX:-}" ]; then
    SANDBOX="$(mktemp -d)"
    trap 'rm -rf "$SANDBOX"' EXIT INT TERM HUP
    # Everything the checkers read, minus what they never touch. .git is excluded
    # deliberately - none of the checkers invoke git, and it is the bulk of the size.
    (cd "$REPO" && tar -cf - \
        --exclude='./.git' \
        --exclude='node_modules' \
        --exclude='__pycache__' \
        --exclude='./aidlc' \
        --exclude='./.kiro' \
        --exclude='./.security-agent' \
        --exclude='./deployment/global-s3-assets' \
        --exclude='./deployment/regional-s3-assets' \
        --exclude='./frontend/dist' \
        --exclude='./admin-console/dist' \
        . 2>/dev/null) | (cd "$SANDBOX" && tar -xf - 2>/dev/null)
    if [ ! -f "$SANDBOX/deployment/check-resource-names.py" ]; then
        echo "❌ Could not copy the repository into $SANDBOX" >&2
        exit 1
    fi
    GUARD_SANDBOX=1 bash "$SANDBOX/tests/test_check_guards.sh"
    exit $?
fi

cd "$REPO"

# Pristine copies of the files these cases break, so each case starts from known
# contents rather than inheriting the previous case's damage. Both this and the files
# it protects live inside the sandbox, so nothing here can reach the real repository -
# no trap, no restore-on-exit and no guard against a half-finished run is needed.
BACKUP_DIR="$(mktemp -d)"
TRACKED=(
    "infrastructure/storage.yaml"
    "infrastructure/api.yaml"
    "backend/scripts/build-tier1.sh"
    "Makefile"
)
for f in "${TRACKED[@]}"; do
    mkdir -p "$BACKUP_DIR/$(dirname "$f")"
    cp "$f" "$BACKUP_DIR/$f"
done

pass=0
fail=0

# A stand-in container runtime, so the psycopg2 layer step can be exercised
# without a running VM. It parses the -v mount and materialises what
# `pip install -t` would have produced.
STUB_DIR="$(mktemp -d)"
cat > "$STUB_DIR/runtime" <<'STUB'
#!/bin/bash
set -e
hostdir=""; prev=""
for a in "$@"; do
    [ "$prev" = "-v" ] && hostdir="${a%%:*}"
    prev="$a"
done
[ -n "$hostdir" ] || { echo "stub: no -v mount" >&2; exit 1; }
mkdir -p "$hostdir/python/lib/python3.13/site-packages/psycopg2"
echo "stub" > "$hostdir/python/lib/python3.13/site-packages/psycopg2/__init__.py"
STUB
chmod +x "$STUB_DIR/runtime"
export CONTAINER_CMD="$STUB_DIR/runtime"

# assert_fails <label> <expected substring> <command...>
assert_fails() {
    local label="$1" expect="$2"; shift 2
    local out rc
    out="$("$@" 2>&1)"; rc=$?
    if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "$expect"; then
        echo "  ok   $label"
        pass=$((pass + 1))
    else
        echo "  FAIL $label (exit=$rc, expected message: $expect)"
        printf '%s\n' "$out" | tail -6 | sed 's/^/         /'
        fail=$((fail + 1))
    fi
}

assert_passes() {
    local label="$1"; shift
    local out rc
    out="$("$@" 2>&1)"; rc=$?
    if [ "$rc" -eq 0 ]; then
        echo "  ok   $label"
        pass=$((pass + 1))
    else
        echo "  FAIL $label (exit=$rc)"
        printf '%s\n' "$out" | tail -8 | sed 's/^/         /'
        fail=$((fail + 1))
    fi
}

reset_files() {
    for f in "${TRACKED[@]}"; do cp "$BACKUP_DIR/$f" "$REPO/$f"; done
}

echo "══ check-resource-names.py ══"

# The seeded email-sender record must stay identical to what
# governance_store.put_email_sender writes, or the deploy seeds a record the
# application misreads - and the only symptom is invitations failing with 409.
python3 - <<'PY'
import pathlib
p = pathlib.Path("infrastructure/storage.yaml")
p.write_text(p.read_text().replace(
    "EMAIL_SENDER_KEY = 'email_sender'", "EMAIL_SENDER_KEY = 'wrong_key'"))
PY
assert_fails "config_key drift is rejected" "config_key differs" \
    python3 deployment/check-resource-names.py
reset_files

python3 - <<'PY'
import pathlib
p = pathlib.Path("infrastructure/storage.yaml")
p.write_text(p.read_text().replace("'source_arn': '',", "", 1))
PY
assert_fails "a dropped item field is rejected" "missing field" \
    python3 deployment/check-resource-names.py
reset_files

python3 - <<'PY'
import pathlib
p = pathlib.Path("infrastructure/storage.yaml")
p.write_text(p.read_text().replace(
    "'action': 'email_sender_set'", "'action': 'renamed_action'"))
PY
assert_fails "an audit-action rename is rejected" "audit action differs" \
    python3 deployment/check-resource-names.py
reset_files

# A table name that does not follow the prefix convention.
python3 - <<'PY'
import pathlib
p = pathlib.Path("infrastructure/storage.yaml")
p.write_text(p.read_text().replace(
    "TableName: !Sub '${ResourcePrefix}-spaces'", "TableName: hardcoded-spaces"))
PY
assert_fails "an unprefixed table name is rejected" "FAIL" \
    python3 deployment/check-resource-names.py
reset_files

# A table named from a parameter that master.yaml does not pass to *this* stack.
# The instances table shipped this way: master passes DbConfigTable to APIStack but
# never to StorageStack, so the table got a CloudFormation-generated name while the
# application looked up '<prefix>-instances'. The first version of the checker
# resolved the Ref against any nested stack's parameters and so missed it.
python3 - <<'PY'
import pathlib, re
p = pathlib.Path("infrastructure/storage.yaml")
t = p.read_text()
t = t.replace("Parameters:\n",
              "Parameters:\n  DbConfigTable:\n    Type: String\n    Default: ''\n", 1)
t = re.sub(r"      TableName: !Sub '\$\{ResourcePrefix\}-instances'",
           "      TableName: !Ref DbConfigTable", t, count=1)
p.write_text(t)
PY
assert_fails "a table named from an unpassed parameter is rejected" \
    "never passes to the stack that owns" python3 deployment/check-resource-names.py
reset_files

echo "══ check-templates.py ══"

# A parameter the child does not declare: CloudFormation hard-fails on this, but
# only part-way through a deploy.
python3 - <<'PY'
import pathlib
p = pathlib.Path("infrastructure/api.yaml")
t = p.read_text()
t = t.replace("\nParameters:\n", "\nParameters:\n  # removed for the guard test\n", 1)
# Drop the ResourcePrefix declaration so master.yaml passes an unknown parameter.
import re
t = re.sub(r"\n  ResourcePrefix:\n(?:    .*\n|\n)*?(?=  [A-Za-z]+:\n)", "\n", t, count=1)
p.write_text(t)
PY
assert_fails "an undeclared nested-stack parameter is rejected" "does not declare" \
    python3 deployment/check-templates.py
reset_files

# A parameter the Makefile passes that master.yaml does not declare. CloudFormation
# rejects the whole deploy on this, so it must never reach an account.
python3 - <<'PY'
import pathlib
p = pathlib.Path("Makefile")
t = p.read_text()
t = t.replace("\t\t\tAppName='$(APP_NAME)' \\",
              "\t\t\tNoSuchParameter='x' \\\n\t\t\tAppName='$(APP_NAME)' \\", 1)
p.write_text(t)
PY
assert_fails "a parameter master.yaml does not declare is rejected" \
    "master.yaml does not declare" python3 deployment/check-templates.py
reset_files

# A duplicate top-level key. YAML keeps the last occurrence and both PyYAML and
# CloudFormation do it silently, so a second Conditions: block erases the first.
# The failure then surfaces mid-deploy as "Condition <name> is not defined", after
# several stacks have already been created.
python3 - <<'PY'
import pathlib
p = pathlib.Path("infrastructure/storage.yaml")
t = p.read_text()
t = t.replace("\nOutputs:\n",
              "\nConditions:\n  HasEmail: !Not [!Equals [!Ref NotificationEmail, '']]\n\nOutputs:\n", 1)
p.write_text(t)
PY
assert_fails "a duplicate top-level key is rejected" "duplicate key" \
    python3 deployment/check-templates.py
reset_files

# A condition referenced but not defined - the error CloudFormation itself reported.
python3 - <<'PY'
import pathlib
p = pathlib.Path("infrastructure/storage.yaml")
t = p.read_text()
t = t.replace("  HasEmailSender: !Not [!Equals [!Ref EmailSenderAddress, '']]\n", "", 1)
p.write_text(t)
PY
assert_fails "an undefined condition is rejected" "does not define" \
    python3 deployment/check-templates.py
reset_files

echo "══ build-s3-dist.sh ══"

# The tier1 file set must agree with build-tier1.sh, or the staged Lambda ships
# different code than the local-development one.
python3 - <<'PY'
import pathlib
p = pathlib.Path("backend/scripts/build-tier1.sh")
p.write_text("\n".join(
    l for l in p.read_text().splitlines()
    if "cp tools/resource_names.py" not in l) + "\n")
PY
assert_fails "tier1 file-set drift is rejected" "not in" \
    ./deployment/build-s3-dist.sh guard-bucket guard-solution v0
reset_files

# Every nested TemplateURL must resolve to a template on disk. This is how the
# missing agent.yaml would otherwise surface: a mid-deploy 404.
mv infrastructure/storage.yaml "$BACKUP_DIR/storage.moved"
assert_fails "an unstageable nested template is rejected" "not staged" \
    ./deployment/build-s3-dist.sh guard-bucket guard-solution v0
mv "$BACKUP_DIR/storage.moved" infrastructure/storage.yaml

echo "══ control: unmutated tree ══"
assert_passes "check-resource-names.py passes" python3 deployment/check-resource-names.py
assert_passes "check-templates.py passes" python3 deployment/check-templates.py
assert_passes "build-s3-dist.sh succeeds" \
    ./deployment/build-s3-dist.sh guard-bucket guard-solution v0

rm -rf "$STUB_DIR"
echo ""
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ] || exit 1
