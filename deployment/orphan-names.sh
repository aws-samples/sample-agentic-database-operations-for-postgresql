#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# The exact resource names one installation of this solution creates, kept in ONE place
# so the read-only check (deployment/check-orphans.sh) and the destructive purge
# (deployment/destroy/purge-orphans.sh) act on the same set. check reports these to gate
# a deploy; purge deletes exactly these. Neither derives them from a "<prefix>-*" glob.
#
# Why exact names, not a prefix
# -----------------------------
# RESOURCE_PREFIX lets two installations share one account (see deploy.config.example).
# A prefix match breaks that whenever one prefix is a string-prefix of another: a purge
# of "dbops" that matched "^dbops-" would also delete a separate "dbops-test" install -
# its registries, audit trail, metrics tables, buckets and user pool - none
# of which the operator was shown or consented to. Matching the exact names this install
# creates makes the two installations independent.
#
# Pinned to the templates by tests/test_orphan_names.py: adding or renaming a resource
# fails that test until this file is updated to match.
#
# Sourced, not executed. The caller MUST have set PREFIX, ACCOUNT, STACK and REGION
# first - bucket names embed the account id, so ACCOUNT has to be resolved before this
# file is sourced.

# ── The exact names this deployment creates ────────────────────────────────────
BUCKETS=(
    "${PREFIX}-admin-${ACCOUNT}-${REGION}"
    "${PREFIX}-admin-logs-${ACCOUNT}-${REGION}"
    "${PREFIX}-cloudtrail-${ACCOUNT}-${REGION}"
    "${PREFIX}-frontend-${ACCOUNT}-${REGION}"
    "${PREFIX}-frontend-logs-${ACCOUNT}-${REGION}"
    "${PREFIX}-logs-${ACCOUNT}-${REGION}"
    "${PREFIX}-reports-${ACCOUNT}-${REGION}"
)
TABLES=(
    "${PREFIX}-audit" "${PREFIX}-config" "${PREFIX}-instances"
    "${PREFIX}-investigations" "${PREFIX}-network" "${PREFIX}-recommendations"
    "${PREFIX}-rate-limits" "${PREFIX}-sessions" "${PREFIX}-spaces" "${PREFIX}-user-knowledge"
    "${PREFIX}-tier1-metrics" "${PREFIX}-tier2-scans"
)
# Four are declared in the templates; the rest are the ones Lambda creates on first
# invoke, which a teardown leaves behind.
LOG_GROUPS=(
    "/${PREFIX}/apigw/${STACK}/access"
    "/${PREFIX}/vpc/${STACK}/flowlogs"
    "/aws/batch/${PREFIX}-scan"
    "/ecs/${PREFIX}-api"
    "/aws/lambda/${PREFIX}-scan-trigger"
    "/aws/lambda/${PREFIX}-learning-trigger"
    "/aws/lambda/${PREFIX}-tier1-collector"
    "/aws/lambda/${PREFIX}-alarm-notifier"
)
# LEGACY: the Aurora metrics store this solution used before DynamoDB + CloudWatch,
# and its two fixed-name Lambda log groups. The stack creates none of these any more;
# check-orphans.sh only notes them and purge-orphans.sh removes them.
LEGACY_CLUSTER="${PREFIX}-metrics"
LEGACY_LOG_GROUPS=(
    "/aws/lambda/${PREFIX}-aurora-schema-init"
    "/aws/lambda/${PREFIX}-aurora-retention"
)
# The database alarms the API task creates at runtime (tools/aws/db_alarms.py): one per
# monitored database per enabled alarm, so not in any template and left behind by a
# teardown. Matched by this anchored full-shape pattern (the alarm ids, then a uuid4
# instance id), never a "<prefix>-db-" sweep: a "dbops-db" install's alarms are named
# "dbops-db-db-...", which this does not match. Pinned to the catalogue by
# tests/test_orphan_names.py. check-orphans.sh notes them; purge-orphans.sh deletes them.
DB_ALARM_RE="^${PREFIX}-db-(collection_failed|xid_wraparound|connection_saturation|long_transaction|blocked_queries|deadlocks|replica_lag|cache_hit_ratio|rollback_ratio)-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\$"
USER_POOL="${PREFIX}-users"
# AgentCore memory names cannot contain hyphens, so the template joins the parts with
# underscores: !Join ['_', !Split ['-', '${ResourcePrefix}-memory']]. The live id is
# this name with a generated suffix appended.
MEMORY="$(echo "${PREFIX}-memory" | tr '-' '_')"
# A pre-RDS-managed installation kept the Aurora master credential in its own secret.
# The stack declares no secrets now (RDS manages the cluster credential under an
# "rds!cluster-…" name), so this exact name is the only secret that can be a leftover of
# THIS install. Matched by exact name, never a prefix sweep.
LEGACY_SECRET="${PREFIX}-aurora-credentials"
# The admin audit vault bucket (storage.yaml AuditBucket) is NOT in BUCKETS. Its name
# ends in the storage stack's id, so it never collides with a redeploy, and its records
# are under Object Lock COMPLIANCE, so it outlives a teardown until the last one
# expires. Listing it in BUCKETS would make check-orphans.sh block every redeploy over
# a bucket nobody can empty. Matched instead by this anchored full-shape pattern, so a
# "dbops-test" install's "dbops-test-audit-..." buckets never match a "dbops" prefix.
# check-orphans.sh only notes matches; teardown deletes one only when it is empty.
AUDIT_BUCKET_RE="^${PREFIX}-audit-${ACCOUNT}-[0-9a-f]{12}\$"
