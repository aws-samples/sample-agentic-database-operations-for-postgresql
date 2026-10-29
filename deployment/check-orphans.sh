#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Report resources left behind by a previous failed deploy, which would block a retry.
#
# READ-ONLY. This script cannot delete anything - it only looks and reports. Removal
# lives in deployment/destroy/purge-orphans.sh, behind a typed confirmation.
#
# It did both jobs once, switched by a DELETE=1 environment variable, and that was a
# trap: a file called "check-*" wiped an entire installation with no prompt, because the
# only confirmation was in the Makefile target that usually called it. The two jobs are
# now separate files so that reading this one can never destroy anything.
#
# Why it exists
# -------------
# Most of this solution's stateful resources carry DeletionPolicy: Retain, so that
# tearing the stack down does not destroy metrics, reports or audit records. But
# CloudFormation applies that policy when it rolls back a FAILED CREATE too. So a
# deploy that fails for any reason - a bad parameter, a quota, a transient error -
# leaves those resources behind, and the retry then fails because they already exist.
#
# CloudFormation reports that collision badly. Its early-validation pass
# (AWS::EarlyValidation::ResourceExistenceCheck) says only:
#
#   Validation failed with 9 error(s). Call DescribeEvents to retrieve the full list
#
# It names no resource and no reason, and the detail is not in the stack events. So
# without this check, the second deploy attempt fails with a message that gives an
# operator nothing to act on. This turns it into an explicit list.
#
# Why it matches exact names, not the prefix
# ------------------------------------------
# This searched for anything starting with "<prefix>-", which is wrong in a way that
# blocked valid deploys. RESOURCE_PREFIX exists so two installations can coexist in one
# account (see deploy.config.example), and a prefix match breaks that whenever one
# prefix is a string-prefix of another: deploying "dbops" matched "dbops-test-reports-…"
# and refused to continue, over resources it would never have tried to create.
#
# The name sets are disjoint, so there is no collision to find. A deploy of "dbops"
# creates "dbops-reports-<account>-<region>"; only that exact name can collide. So this now
# compares against the exact names this deployment creates. Those lists are pinned to
# the templates by tests/test_orphan_names.py, which re-derives them and fails if a
# template gains or renames a resource without this file being updated.
#
# Not everything found is a collision, either. An Aurora snapshot embeds the old
# stack's id in its name, so it can never clash with a new deploy - it was still
# counted, and still blocked. Those are reported separately now, and do not block.
#
# Usage:  check-orphans.sh <stack-name> <resource-prefix> <region>
# Exit:   0 = nothing in the way, 1 = genuine collisions found (listed on stdout)
set -uo pipefail

STACK="${1:?stack name required}"
PREFIX="${2:?resource prefix required}"
REGION="${3:-us-east-1}"

# If the stack is alive, its resources are not orphans - they belong to it. Only a
# missing stack (or one stuck in a rolled-back state) can leave orphans behind.
stack_status=$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo ABSENT)
case "$stack_status" in
    CREATE_COMPLETE|UPDATE_COMPLETE|UPDATE_ROLLBACK_COMPLETE)
        echo "   stack $STACK is $stack_status - its resources are in use, not orphans"
        exit 0 ;;
esac

# Bucket names embed the account id, so the exact names cannot be built without it.
#
# Fails closed. This runs as a deploy gate, so reporting "nothing in the way" after
# checking nothing would hand the caller a false all-clear and let the deploy hit the
# unreadable CloudFormation error this script exists to prevent. Exact matching is what
# introduced the dependency on the account id; the prefix version it replaced needed no
# credentials, so this failure mode is new and has to be closed deliberately.
ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)
if [ -z "$ACCOUNT" ] || [ "$ACCOUNT" = "None" ]; then
    echo ""
    echo "❌ Cannot resolve the AWS account id, so the leftover check could not run."
    echo "   This is not a collision - it means the check itself could not be completed,"
    echo "   and it will not report success without having looked."
    echo ""
    echo "   Check your credentials:  aws sts get-caller-identity"
    echo "   Then re-run. Or point the shell at an account: export AWS_PROFILE=<name>"
    echo ""
    exit 1
fi

# ── The exact names this deployment creates ────────────────────────────────────
# Kept in one place (deployment/orphan-names.sh), shared with the purge script so the
# report and the deletes act on the same set, and pinned to the templates by
# tests/test_orphan_names.py. PREFIX, ACCOUNT and STACK are resolved above.
# shellcheck source=./orphan-names.sh
. "$(dirname "${BASH_SOURCE[0]}")/orphan-names.sh"

found=0
report() {
    if [ "$found" -eq 0 ]; then
        echo ""
        echo "⚠️  Resources this deploy needs to create already exist:"
        echo ""
    fi
    found=$((found + 1))
    echo "     $1"
}

noted=0
notice() {
    if [ "$noted" -eq 0 ]; then
        echo ""
        echo "ℹ️  Also present, but harmless - these do not block a deploy:"
        echo ""
    fi
    noted=$((noted + 1))
    echo "     $1"
}

# True when $1 is exactly equal to one of the remaining arguments.
in_list() {
    local needle="$1"; shift
    local item
    for item in "$@"; do
        [ "$item" = "$needle" ] && return 0
    done
    return 1
}

# DynamoDB tables.
for t in $(aws dynamodb list-tables --region "$REGION" --query 'TableNames' --output text 2>/dev/null \
          | tr '\t' '\n' || true); do
    in_list "$t" "${TABLES[@]}" && report "dynamodb table    $t"
done

# S3 buckets. Bucket names are global, so this is deliberately not region-scoped: a
# bucket made by this account in another region still owns the name, and a create in
# this region would still fail.
for b in $(aws s3api list-buckets --query 'Buckets[].Name' --output text 2>/dev/null \
          | tr '\t' '\n' || true); do
    in_list "$b" "${BUCKETS[@]}" && report "s3 bucket         $b"
done

# Cognito user pool.
for name in $(aws cognito-idp list-user-pools --max-results 60 --region "$REGION" \
              --query 'UserPools[].Name' --output text 2>/dev/null | tr '\t' '\n' || true); do
    [ "$name" = "$USER_POOL" ] && report "cognito pool      $name"
done

# AgentCore memory. The id is the name with a generated suffix appended.
for m in $(aws bedrock-agentcore-control list-memories --region "$REGION" \
           --query 'memories[].id' --output text 2>/dev/null | tr '\t' '\n' || true); do
    case "$m" in
        "$MEMORY"|"$MEMORY"-*) report "agentcore memory  $m" ;;
    esac
done

# Log groups.
for lg in $(aws logs describe-log-groups --region "$REGION" \
            --query 'logGroups[].logGroupName' --output text 2>/dev/null \
            | tr '\t' '\n' || true); do
    in_list "$lg" "${LOG_GROUPS[@]}" && report "log group         $lg"
done

# ── Informational only ────────────────────────────────────────────────────────
# The database alarms the API task created. They cannot collide with a deploy (the next
# API task re-puts what it needs), so they are noted, not blocking.
n_alarms=0
for a in $(aws cloudwatch describe-alarms --alarm-name-prefix "${PREFIX}-db-" --region "$REGION" \
           --query 'MetricAlarms[].AlarmName' --output text 2>/dev/null | tr '\t' '\n' || true); do
    [[ "$a" =~ $DB_ALARM_RE ]] && n_alarms=$((n_alarms + 1))
done
[ "$n_alarms" -gt 0 ] && notice "database alarms   $n_alarms  (${PREFIX}-db-*, the email alarms)"
# LEGACY: the Aurora metrics store this solution used before DynamoDB + CloudWatch. The
# stack creates no cluster now, so none of these can collide with a deploy; they are
# reported because they are storage (and, for the secret, a working password) nobody
# needs any more. 'make purge-orphans' removes them.
for c in $(aws rds describe-db-clusters --region "$REGION" \
           --query 'DBClusters[].DBClusterIdentifier' --output text 2>/dev/null \
           | tr '\t' '\n' || true); do
    [ "$c" = "$LEGACY_CLUSTER" ] && notice "legacy aurora cluster $c  (the old metrics store)"
done
# Aurora final snapshots (CloudFormation's default DeletionPolicy for a DBCluster is
# Snapshot, so the old store's teardown left one).
for s in $(aws rds describe-db-cluster-snapshots --snapshot-type manual --region "$REGION" \
           --query "DBClusterSnapshots[?starts_with(DBClusterSnapshotIdentifier,'${PREFIX}-')].DBClusterSnapshotIdentifier" \
           --output text 2>/dev/null | tr '\t' '\n' || true); do
    notice "aurora snapshot   $s"
done

# A pre-RDS-managed installation kept the Aurora master credential in its own secret.
for arn in $(aws secretsmanager list-secrets --include-planned-deletion --region "$REGION" \
             --query "SecretList[?Name=='${PREFIX}-aurora-credentials'].ARN" --output text 2>/dev/null \
             | tr '\t' '\n' || true); do
    notice "legacy secret     ${arn##*:secret:}  (from a pre-RDS-managed deploy)"
done

# Retained admin audit vault buckets. The name carries the deleted storage stack's id,
# so a new deploy creates a fresh bucket and can never collide with one. Its records
# are under Object Lock (COMPLIANCE), so nothing can empty it before they expire; it is
# deleted once empty ('make destroy' / 'make purge-orphans' do that).
for b in $(aws s3api list-buckets --query 'Buckets[].Name' --output text 2>/dev/null \
          | tr '\t' '\n' | grep -E "$AUDIT_BUCKET_RE" || true); do
    notice "retained audit bucket $b: locked records, deletable once empty; does not block a deploy"
done

if [ "$found" -eq 0 ]; then
    if [ "$noted" -eq 0 ]; then
        echo "   no leftovers from a previous deploy"
    else
        echo ""
        echo "   Nothing blocks this deploy. Remove the above with 'make purge-orphans'."
    fi
    exit 0
fi

echo ""
echo "   $found name(s) already taken, so CloudFormation will refuse to create them."
echo "   They carry DeletionPolicy: Retain, so a rolled-back deploy kept them."
echo ""
echo "   To remove them and retry:"
echo ""
echo "     make purge-orphans"
echo ""
echo "   Review the list first. If this account holds an installation you want to keep,"
echo "   deploy the new one under a different name instead:"
echo ""
echo "     RESOURCE_PREFIX=myprefix STACK_NAME=myprefix make deploy"
echo ""
exit 1
