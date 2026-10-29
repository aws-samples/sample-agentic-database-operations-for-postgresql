#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Guard tests for deployment/check-config.sh.
#
# That script is what stands between an operator and a 30-minute deploy that ends in a
# rejected parameter, or - worse - a successful deploy that mails the first temporary
# password to whoever happened to be in an inherited deploy.config. So each rejection
# it is supposed to make is asserted here; a checker that silently stops rejecting is
# no checker at all.
#
# Every case supplies a full, valid set of settings and then breaks exactly one, so a
# failure points at the specific rule that stopped working.
#
# Run with --settings-only, so the credential lookup is skipped. These cases are about
# the validation rules, and without that flag the result would depend on whether the
# machine happens to have a profile exported - the suite passed or failed according to
# the ambient environment rather than the code.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

pass=0
fail=0

# Runs the checker with a valid baseline, overridden by whatever is passed in.
# <description> <expected exit> [VAR=value ...]
expect() {
    local desc="$1" want="$2"; shift 2
    local out got
    out=$(env CONFIG_FILE=deploy.config \
              RESOURCE_PREFIX=dbops \
              STACK_NAME=dbops \
              INITIAL_ADMIN_EMAIL=admin@example.com \
              INITIAL_ADMIN_NAME=Administrator \
              NOTIFICATION_EMAIL= \
              EMAIL_SENDER_ADDRESS= \
              AWS_REGION=us-east-1 \
              "$@" \
              ./deployment/check-config.sh --quiet --settings-only 2>&1)
    got=$?
    if [ "$got" = "$want" ]; then
        printf '  ok   %s\n' "$desc"
        pass=$((pass + 1))
    else
        printf '  FAIL %s (exit=%s, expected %s)\n' "$desc" "$got" "$want"
        printf '%s\n' "$out" | sed 's/^/         /' | head -4
        fail=$((fail + 1))
    fi
}

echo "══ check-config.sh ══"

expect "a valid set of settings passes"            0

# The admin email is the one setting with no safe default: Cognito mails the first
# temporary password there, so a wrong or missing value means nobody can sign in.
expect "a missing admin email is rejected"         1 INITIAL_ADMIN_EMAIL=
expect "an admin email with no @ is rejected"      1 INITIAL_ADMIN_EMAIL=notanemail
expect "an admin email with no domain dot is rejected" 1 INITIAL_ADMIN_EMAIL=admin@localhost
expect "an admin email with a space is rejected"   1 'INITIAL_ADMIN_EMAIL=a b@c.com'

# The prefix has to satisfy the AllowedPattern the templates enforce. Catching it here
# rather than in CloudFormation matters because the artifacts are staged to S3 first,
# so a rejection there wastes a full build.
expect "an empty prefix is rejected"               1 RESOURCE_PREFIX=
expect "an uppercase prefix is rejected"           1 RESOURCE_PREFIX=DBOps
expect "a prefix starting with a digit is rejected" 1 RESOURCE_PREFIX=9dbops
expect "a prefix with an underscore is rejected"   1 RESOURCE_PREFIX=db_ops
expect "a prefix over 21 characters is rejected"   1 RESOURCE_PREFIX=abcdefghijklmnopqrstuvwxyz
expect "a single-character prefix is rejected"     1 RESOURCE_PREFIX=d

expect "an empty region is rejected"               1 AWS_REGION=

# The profile is read from the shell only - deploy.config stores none - so there is no
# precedence to get wrong and no combination of values to reconcile. Two earlier bugs
# lived here: a saved profile overrode the shell (an assignment in an included makefile
# beats an environment variable), which pointed a `make destroy` at the wrong account;
# and the saved copy then looked authoritative while being ignored. Removing the setting
# removed both. What remains is that the checker must not care what a profile is called.
expect "any exported profile is accepted"          0 AWS_PROFILE=whatever-name
expect "no exported profile is accepted"           0 AWS_PROFILE=

# The setting must stay gone: reintroducing it would restore the precedence trap.
echo -n "  deploy.config.example carries no AWS_PROFILE ... "
if grep -qE '^\s*AWS_PROFILE\s*=' "$REPO/deploy.config.example"; then
    echo "FAIL (AWS_PROFILE is back in the example)"; fail=$((fail + 1))
else
    echo "ok"; pass=$((pass + 1))
fi
echo -n "  configure.py does not persist a profile ... "
if grep -qE '^\s*"AWS_PROFILE":' "$REPO/deployment/configure.py"; then
    echo "FAIL (AWS_PROFILE is back in SETTINGS)"; fail=$((fail + 1))
else
    echo "ok"; pass=$((pass + 1))
fi
# A region other than us-east-1 is a warning, not an error: it is a real constraint on
# the optional CloudFront WAF and on AgentCore availability, but not every deployment
# needs either, so it must not block.
expect "a region other than us-east-1 warns but passes" 0 AWS_REGION=eu-west-1

echo "══ yes/no answers mean the same thing everywhere ══"
# Both prompts go through configure.py --confirm, and the accepted set is derived from the
# same table the true/false settings use — so a word cannot mean one thing at one prompt
# and something else at another in the same run (e.g. 'true' read as "no" at one prompt
# after meaning yes at another).
confirm_says() {
    local answer="$1" want="$2"
    local got
    if printf '%s\n' "$answer" | ./deployment/configure.py --confirm "Q?" >/dev/null 2>&1; then
        got=yes
    else
        got=no
    fi
    if [ "$got" = "$want" ]; then
        printf '  ok   %-8s means %s\n' "'$answer'" "$want"
        pass=$((pass + 1))
    else
        printf '  FAIL %-8s meant %s, expected %s\n' "'$answer'" "$got" "$want"
        fail=$((fail + 1))
    fi
}

for a in y Y yes YES true on enable 1; do confirm_says "$a" yes; done
for a in n no false off disable 0 maybe ""; do confirm_says "$a" no; done

printf 'pass=%d fail=%d\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
