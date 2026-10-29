#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Prove the destroy confirmation cannot be walked past.
#
#   bash tests/test_destroy_confirm.sh          (from the repository root)
#
# Everything under deployment/destroy/ deletes data, and it is reachable directly, not
# only through `make`. With the confirmation in a Makefile target and the deletion in the
# script,
#
#     DELETE=1 ./deployment/check-orphans.sh dbops dbops us-east-1
#
# would wipe an installation with no prompt at all. The guard travels with the action
# instead, and these cases exist so it cannot quietly stop doing so.
#
# No AWS calls. require_confirmation is pure shell, so it is tested directly - which also
# means these run anywhere, with or without credentials.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

pass=0
fail=0
check() {
    if [ "$2" = "$3" ]; then
        echo "  ok   $1"
        pass=$((pass + 1))
    else
        echo "  FAIL $1 (exit $2, expected $3)"
        fail=$((fail + 1))
    fi
}

# shellcheck source=../deployment/destroy/confirm.sh
. "$REPO/deployment/destroy/confirm.sh"

echo "══ the confirmation refuses anything but the exact token ══"

printf 'wrong\n' | require_confirmation "s" "'a@b'" "a@b" >/dev/null 2>&1
check "a wrong answer aborts" "$?" "1"

require_confirmation "s" "'a@b'" "a@b" </dev/null >/dev/null 2>&1
check "no input at all aborts" "$?" "1"

printf '\n' | require_confirmation "s" "'a@b'" "a@b" >/dev/null 2>&1
check "a bare Enter aborts" "$?" "1"

# "yes" is the answer a hurried operator gives a prompt they did not read. It must not
# work: the whole point of a typed token is that it cannot be produced by reflex.
printf 'yes\n' | require_confirmation "s" "'a@b'" "a@b" >/dev/null 2>&1
check "'yes' aborts" "$?" "1"

# The stack name alone does not say WHICH ACCOUNT — every account uses the same stack
# name — so the prompt must not accept it. That ambiguity could aim a teardown at the
# wrong account.
printf 'a\n' | require_confirmation "s" "'a@b'" "a@b" >/dev/null 2>&1
check "the name without the account aborts" "$?" "1"

printf ' a@b \n' | require_confirmation "s" "'a@b'" "a@b" >/dev/null 2>&1
check "surrounding whitespace aborts (no silent trimming)" "$?" "1"

printf 'A@B\n' | require_confirmation "s" "'a@b'" "a@b" >/dev/null 2>&1
check "wrong case aborts" "$?" "1"

printf 'a@b\n' | require_confirmation "s" "'a@b'" "a@b" >/dev/null 2>&1
check "the exact token proceeds" "$?" "0"

echo "══ DESTROY_CONFIRMED is scoped to one target ══"
# `make destroy` confirms once and passes this down, so a mid-teardown retry does not
# stop to ask again. It has to be worthless anywhere else, or it becomes a way to
# pre-authorise a delete against something the operator never saw.

printf '' | DESTROY_CONFIRMED="a@b" require_confirmation "s" "'a@b'" "a@b" >/dev/null 2>&1
check "a value matching this target skips the prompt" "$?" "0"

printf 'wrong\n' | DESTROY_CONFIRMED="other@account" require_confirmation "s" "'a@b'" "a@b" >/dev/null 2>&1
check "a value for a DIFFERENT target still prompts" "$?" "1"

printf 'wrong\n' | DESTROY_CONFIRMED="1" require_confirmation "s" "'a@b'" "a@b" >/dev/null 2>&1
check "a truthy-looking value does not count as consent" "$?" "1"

echo "══ the read-only checker stays read-only ══"
# It is named check-* and is run unprompted by `make deploy`. If a delete call ever
# appears in it, the name becomes a lie and the deploy path becomes destructive.
if grep -qE '(delete-[a-z]|delete-object|s3 rm|delete_)' "$REPO/deployment/check-orphans.sh"; then
    echo "  FAIL deployment/check-orphans.sh contains a delete call"
    fail=$((fail + 1))
else
    echo "  ok   deployment/check-orphans.sh contains no delete call"
    pass=$((pass + 1))
fi

# Anything that can delete belongs in deployment/destroy/, so the directory listing is
# an accurate map of the risk.
strays=""
for f in "$REPO"/deployment/*.sh; do
    case "$(basename "$f")" in
        # build-s3-dist.sh removes its own staged artifacts from S3, which is not a
        # teardown of anything an operator owns.
        build-s3-dist.sh) continue ;;
    esac
    if grep -qE '(cloudformation delete-stack|dynamodb delete-table|delete-db-cluster|delete-user-pool|delete-secret|s3api delete-bucket)' "$f"; then
        strays="$strays $(basename "$f")"
    fi
done
if [ -n "$strays" ]; then
    echo "  FAIL destructive calls outside deployment/destroy/:$strays"
    fail=$((fail + 1))
else
    echo "  ok   no destructive AWS calls in deployment/ outside destroy/"
    pass=$((pass + 1))
fi

# Each script that deletes must actually source the guard, not merely sit beside it.
for f in "$REPO"/deployment/destroy/purge-orphans.sh "$REPO"/deployment/destroy/retry-stack-delete.sh; do
    b="$(basename "$f")"
    if grep -q 'confirm.sh' "$f"; then
        echo "  ok   $b sources the confirmation"
        pass=$((pass + 1))
    else
        echo "  FAIL $b does not source confirm.sh"
        fail=$((fail + 1))
    fi
done

echo ""
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ] || exit 1
