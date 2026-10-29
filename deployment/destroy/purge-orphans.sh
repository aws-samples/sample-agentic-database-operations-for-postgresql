#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Delete the retained resources a previous failed deploy left behind.
#
# THIS DELETES DATA. It removes the registries, the metrics store, the report and log
# buckets, the user pool and the agent's memory - everything the stack deliberately kept
# so that a teardown would not lose them. Reporting lives in the read-only
# deployment/check-orphans.sh; this file is the half that destroys.
#
# It refuses to run without a typed confirmation naming the account, and it prints the
# exact list first. Both live here rather than in a Makefile target, so invoking the
# script directly is still guarded.
#
# Usage:  purge-orphans.sh <stack-name> <resource-prefix> <region>
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./confirm.sh
. "$HERE/confirm.sh"

STACK="${1:?stack name required}"
PREFIX="${2:?resource prefix required}"
REGION="${3:-us-east-1}"

ACCOUNT=$(account_or_die) || exit 1

# The exact names this install created, shared with check-orphans.sh so the deletes
# below match exactly what the report showed. PREFIX/ACCOUNT/STACK/REGION are set above.
# shellcheck source=../orphan-names.sh
. "$HERE/../orphan-names.sh"

# Show what is actually there before asking. An operator cannot consent to "delete the
# leftovers" without seeing them, and the list is often shorter than feared - or contains
# something they wanted to keep.
echo ""
echo "   Looking for leftovers of '$PREFIX' in account $ACCOUNT ($REGION)..."
"$HERE/../check-orphans.sh" "$STACK" "$PREFIX" "$REGION" || true

# Nothing to do is not a failure - it is the common case after a clean teardown.
if "$HERE/../check-orphans.sh" "$STACK" "$PREFIX" "$REGION" >/dev/null 2>&1; then
    echo "   Nothing to purge."
    exit 0
fi

require_confirmation \
"   Everything listed above will be PERMANENTLY DELETED from:

       account  $ACCOUNT
       region   $REGION
       install  $PREFIX  (only the exact names this install created - never $PREFIX-*)

   That includes the DynamoDB registries (spaces, onboarded databases,
   investigations, the audit trail, the deep-scan history and the Tier 1
   metrics), any leftover of the legacy Aurora metrics store, the report and log buckets, the Cognito user pool with all
   its users, and the agent's memory." \
    "'$PREFIX@$ACCOUNT'" "$PREFIX@$ACCOUNT" || exit 1

# Print the concrete names once more, after consent and before the first delete, so the
# operator sees exactly what will be removed - no wildcard. Only names that actually
# exist are deleted below; a sibling install ($PREFIX-test-*) is not in this set.
echo ""
echo "   Deleting only these exact resources (those that exist):"
for t in "${TABLES[@]}";     do echo "     table       $t"; done
for b in "${BUCKETS[@]}";    do echo "     bucket      $b"; done
echo                              "     audit vault buckets matching $AUDIT_BUCKET_RE, only if already empty"
echo                              "     aurora      $LEGACY_CLUSTER  (legacy store, + its final snapshots ${LEGACY_CLUSTER}*)"
echo                              "     alarms      the database alarms matching $DB_ALARM_RE"
echo                              "     cognito     $USER_POOL"
echo                              "     memory      $MEMORY*"
echo                              "     secret      $LEGACY_SECRET"
for lg in "${LOG_GROUPS[@]}" "${LEGACY_LOG_GROUPS[@]}"; do echo "     log group   $lg"; done
echo ""

removed=0
gone() { removed=$((removed + 1)); echo "   deleted $1"; }

# DynamoDB tables. Exact names only; delete-table fails on a name that is not there, so
# a missing one is simply not reported.
for t in "${TABLES[@]}"; do
    aws dynamodb delete-table --table-name "$t" --region "$REGION" >/dev/null 2>&1 && gone "table $t"
done

# S3 buckets. Exact names only. The artifacts bucket is not in this list - `make stage`
# owns it and it holds the templates a redeploy reads - so it is never in scope.
for b in "${BUCKETS[@]}"; do
    aws s3api head-bucket --bucket "$b" >/dev/null 2>&1 || continue
    aws s3 rm "s3://$b" --recursive --only-show-errors 2>/dev/null
    # `s3 rm` leaves versions and delete markers behind on a versioned bucket, and the
    # bucket cannot be deleted while any remain.
    while :; do
        payload=$(aws s3api list-object-versions --bucket "$b" --region "$REGION" --max-keys 500 \
            --query '{Objects: (([Versions, DeleteMarkers][] || [])[].{Key:Key,VersionId:VersionId})}' \
            --output json 2>/dev/null)
        count=$(printf '%s' "$payload" \
            | python3 -c 'import sys,json;d=json.load(sys.stdin) or {};print(len(d.get("Objects") or []))' 2>/dev/null)
        [ "${count:-0}" -eq 0 ] && break
        printf '%s' "$payload" > "${TMPDIR:-/tmp}/purge-versions.json"
        aws s3api delete-objects --bucket "$b" --region "$REGION" \
            --delete "file://${TMPDIR:-/tmp}/purge-versions.json" >/dev/null 2>&1 || break
    done
    rm -f "${TMPDIR:-/tmp}/purge-versions.json"
    aws s3api delete-bucket --bucket "$b" --region "$REGION" >/dev/null 2>&1 && gone "bucket $b"
done

# ── audit vault buckets (empty-only) ──
# The admin audit vault buckets match AUDIT_BUCKET_RE and are not in BUCKETS, so the
# emptying loop above can never reach one. Their records are under Object Lock
# COMPLIANCE: nothing, the account root user included, can remove one before its
# retention ends, and lifecycle removes each once it has. So this block never empties
# anything. It deletes a bucket only when it is already empty and otherwise leaves it.
for b in $(aws s3api list-buckets --query 'Buckets[].Name' --output text 2>/dev/null \
          | tr '\t' '\n' | grep -E "$AUDIT_BUCKET_RE" || true); do
    left=$(aws s3api list-object-versions --bucket "$b" --region "$REGION" --max-keys 1 \
        --no-paginate --query 'sum([length(Versions || `[]`), length(DeleteMarkers || `[]`)])' \
        --output text 2>/dev/null)
    if [ "$left" = "0" ]; then
        aws s3api delete-bucket --bucket "$b" --region "$REGION" >/dev/null 2>&1 \
            && gone "empty audit bucket $b"
    elif [ -n "$left" ]; then
        echo "   kept audit bucket $b: it still holds locked records; deletable once empty"
    else
        echo "   audit bucket $b: could not inspect; left in place"
    fi
done
# ── end audit vault buckets ──

# Secret. The stack declares none now; the only leftover that can belong to THIS install
# is the pre-RDS-managed master credential, matched by its exact name. Forced, because a
# normal delete keeps the NAME reserved for the recovery window - up to 30 days - and a
# redeploy then collides with a secret it cannot see.
for arn in $(aws secretsmanager list-secrets --include-planned-deletion --region "$REGION" \
             --query "SecretList[?Name=='${LEGACY_SECRET}'].ARN" --output text 2>/dev/null \
             | tr '\t' '\n' || true); do
    aws secretsmanager delete-secret --secret-id "$arn" --force-delete-without-recovery \
        --region "$REGION" >/dev/null 2>&1 && gone "secret ${arn##*:secret:}"
done

# LEGACY Aurora metrics store (pre-DynamoDB): instances first, then the cluster.
# Exact cluster name only.
if aws rds describe-db-clusters --db-cluster-identifier "$LEGACY_CLUSTER" --region "$REGION" >/dev/null 2>&1; then
    for i in $(aws rds describe-db-clusters --db-cluster-identifier "$LEGACY_CLUSTER" --region "$REGION" \
               --query 'DBClusters[0].DBClusterMembers[].DBInstanceIdentifier' \
               --output text 2>/dev/null | tr '\t' '\n'); do
        aws rds delete-db-instance --db-instance-identifier "$i" \
            --skip-final-snapshot --region "$REGION" >/dev/null 2>&1
    done
    aws rds delete-db-cluster --db-cluster-identifier "$LEGACY_CLUSTER" \
        --skip-final-snapshot --region "$REGION" >/dev/null 2>&1 && gone "aurora cluster $LEGACY_CLUSTER"
fi

# Aurora final snapshots. CloudFormation's default DeletionPolicy for a DBCluster is
# Snapshot, so a teardown leaves one even when nothing asked for it. Its name embeds a
# generated stack id, so it is the one resource without a fixed name - scoped to the
# cluster identifier ($LEGACY_CLUSTER), which a string-prefix sibling ($PREFIX-test-metrics)
# cannot match, rather than the bare prefix.
for s in $(aws rds describe-db-cluster-snapshots --snapshot-type manual --region "$REGION" \
           --query "DBClusterSnapshots[?starts_with(DBClusterSnapshotIdentifier,'${LEGACY_CLUSTER}')].DBClusterSnapshotIdentifier" \
           --output text 2>/dev/null | tr '\t' '\n' || true); do
    aws rds delete-db-cluster-snapshot --db-cluster-snapshot-identifier "$s" \
        --region "$REGION" >/dev/null 2>&1 && gone "snapshot $s"
done

# AgentCore memory: the live id is the exact name (hyphens underscored) plus a generated
# suffix, so match the exact name / name-with-suffix, not the underscored prefix (which
# would also match a sibling install's memory).
for m in $(aws bedrock-agentcore-control list-memories --region "$REGION" \
           --query 'memories[].id' --output text 2>/dev/null | tr '\t' '\n' || true); do
    case "$m" in
        "$MEMORY"|"$MEMORY"-*) aws bedrock-agentcore-control delete-memory --memory-id "$m" \
            --region "$REGION" >/dev/null 2>&1 && gone "agentcore memory $m" ;;
    esac
done

# Cognito: the domain has to go before the pool will delete. Exact pool name only.
for id in $(aws cognito-idp list-user-pools --max-results 60 --region "$REGION" \
            --query "UserPools[?Name=='${USER_POOL}'].Id" --output text 2>/dev/null \
            | tr '\t' '\n' || true); do
    dom=$(aws cognito-idp describe-user-pool --user-pool-id "$id" --region "$REGION" \
          --query 'UserPool.Domain' --output text 2>/dev/null)
    case "$dom" in ''|None) ;; *) aws cognito-idp delete-user-pool-domain \
        --user-pool-id "$id" --domain "$dom" --region "$REGION" >/dev/null 2>&1 ;; esac
    aws cognito-idp delete-user-pool --user-pool-id "$id" --region "$REGION" >/dev/null 2>&1 \
        && gone "cognito pool $id"
done

# The database alarms the API task created at runtime. Anchored full-shape match only
# (DB_ALARM_RE), so a sibling install's alarms are never listed, let alone deleted.
alarms=()
for a in $(aws cloudwatch describe-alarms --alarm-name-prefix "${PREFIX}-db-" --region "$REGION" \
           --query 'MetricAlarms[].AlarmName' --output text 2>/dev/null | tr '\t' '\n' || true); do
    [[ "$a" =~ $DB_ALARM_RE ]] && alarms+=("$a")
done
i=0
while [ "$i" -lt "${#alarms[@]}" ]; do
    chunk=("${alarms[@]:$i:100}")    # DeleteAlarms takes up to 100 names
    aws cloudwatch delete-alarms --alarm-names "${chunk[@]}" --region "$REGION" >/dev/null 2>&1 \
        && gone "${#chunk[@]} database alarm(s)"
    i=$((i + 100))
done
# Log groups. Lambda and API Gateway create their own outside the stack, so a teardown
# leaves them; one has a fixed name and collides on redeploy. Exact names only;
# delete-log-group fails on a missing one, so it is simply not reported.
for lg in "${LOG_GROUPS[@]}" "${LEGACY_LOG_GROUPS[@]}"; do
    aws logs delete-log-group --log-group-name "$lg" --region "$REGION" >/dev/null 2>&1 \
        && gone "log group $lg"
done

echo ""
if [ "$removed" -eq 0 ]; then
    echo "   Nothing was deleted."
    exit 0
fi
echo "🧹 Removed $removed resource(s) from account $ACCOUNT."
echo "   Aurora and Cognito deletions take a few minutes to finish - if a deploy still"
echo "   reports a collision, wait and retry."
