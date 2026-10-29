#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Stage the CloudFormation templates and Lambda artifacts that infrastructure/master.yaml
# fetches at deploy time.
#
#   Usage: deployment/build-s3-dist.sh <DIST_OUTPUT_BUCKET> <SOLUTION_NAME> <VERSION> [--upload]
#
#   DIST_OUTPUT_BUCKET  S3 bucket that will serve the staged assets.
#   SOLUTION_NAME       Prefix under which templates are staged.
#   VERSION             Version segment, e.g. v1.0.0.
#   --upload            Also copy the built assets to S3 (creates the bucket if absent).
#                       Without it the script only populates the local output dirs, which
#                       is the AWS Solutions convention - their pipeline uploads separately.
#
# The three positional arguments are the AWS Solutions build contract, so migrating to the
# Solutions publishing pipeline later is a move, not a rewrite: TemplateBaseUrl,
# ArtifactsBucket and ArtifactsVersion are the private-account equivalents of the
# solutions-reference bucket layout.
#
# Output layout (mirrors Solutions global vs regional split):
#   deployment/global-s3-assets/    templates       -> s3://<bucket>/<solution>/<version>/
#   deployment/regional-s3-assets/  Lambda archives -> s3://<bucket>/_lambda/
#
# Two things this script deliberately does NOT do:
#
#   1. Build container images. BackendImageUri / BatchImageUri are required master.yaml
#      parameters that point at ECR in the deploying account, so they cannot be staged by
#      a publisher. The Makefile owns that step.
#   2. Build the SPA bundles. master.yaml does not carry an asset-deployer custom
#      resource (it only exposes the bucket names and distribution IDs as outputs), so a
#      staged bundle would have no consumer. The Makefile builds and syncs the SPAs after
#      the stack exists, because config.js needs the stack outputs.

set -euo pipefail

# ── arguments ─────────────────────────────────────────────────────────────────
if [ "$#" -lt 3 ]; then
    sed -n '5,15p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
fi

DIST_OUTPUT_BUCKET="$1"
SOLUTION_NAME="$2"
VERSION="$3"
shift 3

DO_UPLOAD=false
for arg in "$@"; do
    case "$arg" in
        --upload) DO_UPLOAD=true ;;
        *) echo "❌ Unknown option: $arg" >&2; exit 1 ;;
    esac
done

AWS_REGION="${AWS_REGION:-us-east-1}"

# Resolve paths relative to this script so it runs from anywhere.
DEPLOYMENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEPLOYMENT_DIR/.." && pwd)"

# Build state lives per environment (deployment/.state/<name>/, set by the Makefile as
# DEPLOY_STATE_DIR), so two terminals staging two environments never overwrite each
# other's templates, archives or .artifacts-env. Unset = this directory, as before.
STATE_DIR="${DEPLOY_STATE_DIR:-$DEPLOYMENT_DIR}"
mkdir -p "$STATE_DIR"
GLOBAL_DIR="$STATE_DIR/global-s3-assets"
REGIONAL_DIR="$STATE_DIR/regional-s3-assets"

echo ""
echo "📦 Staging deployment assets"
echo "   bucket:   $DIST_OUTPUT_BUCKET"
echo "   solution: $SOLUTION_NAME"
echo "   version:  $VERSION"
echo "   region:   $AWS_REGION"
echo ""

# ── prerequisites ─────────────────────────────────────────────────────────────
need() {
    command -v "$1" >/dev/null 2>&1 || { echo "❌ $1 is required but not installed." >&2; exit 1; }
}
need python3
need zip
need aws

CONTAINER_CMD="${CONTAINER_CMD:-$(command -v docker 2>/dev/null \
    || command -v finch 2>/dev/null \
    || command -v podman 2>/dev/null \
    || command -v nerdctl 2>/dev/null || true)}"
if [ -z "$CONTAINER_CMD" ]; then
    echo "❌ A container runtime (docker, finch, podman or nerdctl) is required to build" >&2
    echo "   the psycopg2 Lambda layer for linux/arm64." >&2
    exit 1
fi

# ── clean output dirs ─────────────────────────────────────────────────────────
rm -rf "$GLOBAL_DIR" "$REGIONAL_DIR"
mkdir -p "$GLOBAL_DIR" "$REGIONAL_DIR"

# ── 1. templates ──────────────────────────────────────────────────────────────
# master.yaml resolves every nested stack as ${TemplateBaseUrl}/<name>.yaml, so each
# child must sit beside it under the same prefix.
echo "📄 Staging CloudFormation templates..."
for tpl in "$REPO_ROOT"/infrastructure/*.yaml; do
    cp "$tpl" "$GLOBAL_DIR/"
    echo "   $(basename "$tpl")"
done

# master.yaml's AgentStack points at agent.yaml, but the template lives at
# backend/template.yaml and there is no infrastructure/agent.yaml. Without this
# copy-and-rename the AgentStack TemplateURL 404s at deploy time.
cp "$REPO_ROOT/backend/template.yaml" "$GLOBAL_DIR/agent.yaml"
echo "   agent.yaml (copied from backend/template.yaml)"

# Every nested stack master.yaml references must have been staged, or the deploy fails
# mid-flight with a 404 on that child's TemplateURL. This is exactly how the missing
# agent.yaml would have surfaced, so assert it here rather than discovering it in
# CloudFormation.
MISSING=""
while read -r child; do
    [ -z "$child" ] && continue
    [ -f "$GLOBAL_DIR/$child" ] || MISSING="$MISSING $child"
done < <(grep -oE 'TemplateBaseUrl\}/[A-Za-z0-9._-]+\.yaml' "$REPO_ROOT/infrastructure/master.yaml" \
         | sed 's|.*/||' | sort -u)

if [ -n "$MISSING" ]; then
    echo "❌ master.yaml references nested templates that were not staged:$MISSING" >&2
    exit 1
fi
echo "   all nested TemplateURL references resolve"

# ── 2. artifacts version ──────────────────────────────────────────────────────
# Hash the Lambda SOURCES, not the zip bytes: zip archives embed timestamps, so
# hashing the archive would produce a new version on every build and churn the stack
# on no-op redeploys. Same sources therefore mean the same S3 key, and CloudFormation
# correctly sees no change.
#
# This list is the tier1 zip's file set. backend/scripts/build-tier1.sh is the source
# of truth for it (see the packaging contract); the guard below fails the build if the
# two ever drift.
TIER1_FILES=(
    "backend/tier1_collector.py"
    # The alarm notifier Lambda (Handler alarm_notifier.handler) ships in this same zip,
    # with the alarm catalogue it reads.
    "backend/alarm_notifier.py"
    "backend/tools/aws/db_alarms.py"
    "backend/tools/__init__.py"
    "backend/tools/resource_names.py"
    # tier1_collector.py imports the shared XID-wraparound denominator (tools/xid.py).
    # Omitting it shipped a zip the Lambda could not import ("No module named tools.xid").
    "backend/tools/xid.py"
    "backend/tools/aws/__init__.py"
    "backend/tools/aws/metrics_store.py"
    "backend/tools/aws/db_tls.py"
    "backend/tools/aws/host_routes.py"
    "backend/tools/aws/audit_log.py"
    "backend/tools/aws/spaces_client.py"
    "backend/tools/aws/teardown_claim.py"
    "backend/tools/aws/ddb_pages.py"
    "backend/certs/rds-global-bundle.pem"
)

BUILD_TIER1_SH="$REPO_ROOT/backend/scripts/build-tier1.sh"
for f in "${TIER1_FILES[@]}"; do
    [ -f "$REPO_ROOT/$f" ] || { echo "❌ Missing tier1 source: $f" >&2; exit 1; }
    # Every file here must also be staged by build-tier1.sh, or the local dev zip and
    # the staged zip would ship different code.
    rel_to_backend="${f#backend/}"
    if ! grep -q "$rel_to_backend" "$BUILD_TIER1_SH"; then
        echo "❌ $f is in this script's tier1 file set but not in" >&2
        echo "   backend/scripts/build-tier1.sh. Reconcile the two before releasing." >&2
        exit 1
    fi
done
# Reverse direction: every source file build-tier1.sh stages INTO THE ZIP ($TMPDIR, not
# the psycopg2 layer) must also be in TIER1_FILES. The one-directional check above missed
# tools/xid.py — it was added to build-tier1.sh but not here, so the staged (deployed) zip
# shipped without it and the Lambda failed to import at runtime. This closes that gap.
while IFS= read -r staged; do
    case "$staged" in tier1_collector.py|alarm_notifier.py|tools/*|certs/*) ;; *) continue ;; esac
    in_set=0
    for f in "${TIER1_FILES[@]}"; do [ "${f#backend/}" = "$staged" ] && { in_set=1; break; }; done
    if [ "$in_set" -eq 0 ]; then
        echo "❌ build-tier1.sh stages '$staged' into the tier1 zip, but it is not in this" >&2
        echo "   script's TIER1_FILES. Add backend/$staged to TIER1_FILES before releasing." >&2
        exit 1
    fi
done < <(grep -oE 'cp [^ ]+ "\$TMPDIR/' "$BUILD_TIER1_SH" | awk '{print $2}')

# Pin comes from requirements.txt so there is one source of truth for the version.
# requirements.txt is a hashed lock (pip --require-hashes), so the psycopg2-binary line
# ends with a "\" line-continuation before its --hash entries; take only the first field
# so PSYCOPG2_PIN is a clean "psycopg2-binary==X.Y.Z" and not "...==X.Y.Z \".
PSYCOPG2_PIN="$(grep -E '^psycopg2-binary==' "$REPO_ROOT/backend/requirements.txt" | head -1 | awk '{print $1}')"
if [ -z "$PSYCOPG2_PIN" ]; then
    echo "❌ Could not find a psycopg2-binary pin in backend/requirements.txt" >&2
    exit 1
fi

ARTIFACTS_VERSION="$(
    cd "$REPO_ROOT" && {
        # The pin participates in the hash so bumping psycopg2 produces a new layer key;
        # a LayerVersion is immutable, so an unchanged key would never yield a new version.
        printf '%s\n' "$PSYCOPG2_PIN"
        for f in "${TIER1_FILES[@]}"; do
            printf '%s ' "$f"
            python3 -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$f"
        done
    } | python3 -c "import hashlib,sys;print(hashlib.sha256(sys.stdin.read().encode()).hexdigest()[:12])"
)"
echo ""
echo "🔖 ArtifactsVersion: $ARTIFACTS_VERSION"
echo "   ($PSYCOPG2_PIN + ${#TIER1_FILES[@]} tier1 source files)"

# ── 3. tier1 collector zip ────────────────────────────────────────────────────
echo ""
echo "📦 Packaging tier1_collector-${ARTIFACTS_VERSION}.zip..."
TIER1_STAGE="$(mktemp -d)"
trap 'rm -rf "$TIER1_STAGE"' EXIT

mkdir -p "$TIER1_STAGE/tools/aws" "$TIER1_STAGE/certs"
for f in "${TIER1_FILES[@]}"; do
    cp "$REPO_ROOT/$f" "$TIER1_STAGE/${f#backend/}"
done

# -X drops platform extras; the fixed mtime makes the archive reproducible so the same
# sources produce identical bytes as well as an identical key.
find "$TIER1_STAGE" -exec touch -t 200001010000 {} +
( cd "$TIER1_STAGE" && zip -qrX "$REGIONAL_DIR/tier1_collector-${ARTIFACTS_VERSION}.zip" . )

# The collector imports tools.aws.metrics_store (the DynamoDB/CloudWatch writer), so a
# zip holding only the .py file fails at runtime with ModuleNotFoundError. Assert the
# package made it in.
#
# Capture the listing into a variable rather than piping into `grep -q`: grep exits on
# first match, unzip takes SIGPIPE, and `set -o pipefail` would report the whole
# pipeline as failed even though the archive is fine.
TIER1_LISTING="$(unzip -l "$REGIONAL_DIR/tier1_collector-${ARTIFACTS_VERSION}.zip")"
case "$TIER1_LISTING" in
    *"tools/"*) ;;
    *) echo "❌ tier1 zip has no tools/ package - the collector would fail to import." >&2
       exit 1 ;;
esac
# db_tls fails closed without the RDS CA bundle, so a zip without certs/ cannot connect.
case "$TIER1_LISTING" in
    *"certs/rds-global-bundle.pem"*) ;;
    *) echo "❌ tier1 zip has no certs/rds-global-bundle.pem - the collector could not verify any server." >&2
       exit 1 ;;
esac
# The alarm notifier's handler lives in the same zip.
case "$TIER1_LISTING" in
    *"alarm_notifier.py"*) ;;
    *) echo "❌ tier1 zip has no alarm_notifier.py - the alarm notifier would fail to import." >&2
       exit 1 ;;
esac
echo "   $(printf '%s\n' "$TIER1_LISTING" | tail -1 | awk '{print $2}') entries"

# ── 4. psycopg2 layer zip ─────────────────────────────────────────────────────
# Built inside the Lambda arm64 base image so the compiled wheel matches the runtime.
# Consumed by the tier1 collector to reach the MONITORED PostgreSQL databases.
echo ""
echo "📦 Building psycopg2 layer for python3.13/arm64..."
LAYER_BUILD_DIR="$STATE_DIR/.layer-build"
rm -rf "$LAYER_BUILD_DIR"
mkdir -p "$LAYER_BUILD_DIR"

"$CONTAINER_CMD" run --rm \
    --platform linux/arm64 \
    --entrypoint "" \
    -v "$LAYER_BUILD_DIR:/out" \
    public.ecr.aws/lambda/python:3.13 \
    bash -c "pip install $PSYCOPG2_PIN -t /out/python/lib/python3.13/site-packages/ --quiet"

if [ ! -d "$LAYER_BUILD_DIR/python" ]; then
    echo "❌ psycopg2 install produced no python/ directory in $LAYER_BUILD_DIR" >&2
    exit 1
fi

# The RDS CA bundle rides in the layer too (/opt/certs/ once mounted), so any inline
# Lambda that connects to a monitored database can verify its certificate. Same file as
# the tier1 zip's.
mkdir -p "$LAYER_BUILD_DIR/certs"
cp "$REPO_ROOT/backend/certs/rds-global-bundle.pem" "$LAYER_BUILD_DIR/certs/rds-global-bundle.pem"

find "$LAYER_BUILD_DIR" -exec touch -t 200001010000 {} + 2>/dev/null || true
( cd "$LAYER_BUILD_DIR" && zip -qrX "$REGIONAL_DIR/psycopg2-layer-${ARTIFACTS_VERSION}.zip" python/ certs/ )
rm -rf "$LAYER_BUILD_DIR"
echo "   $(du -h "$REGIONAL_DIR/psycopg2-layer-${ARTIFACTS_VERSION}.zip" | cut -f1)"

# ── 5. upload ─────────────────────────────────────────────────────────────────
TEMPLATE_BASE_URL="https://${DIST_OUTPUT_BUCKET}.s3.${AWS_REGION}.amazonaws.com/${SOLUTION_NAME}/${VERSION}"

if [ "$DO_UPLOAD" = true ]; then
    echo ""
    echo "☁️  Uploading to s3://${DIST_OUTPUT_BUCKET}..."

    if ! aws s3api head-bucket --bucket "$DIST_OUTPUT_BUCKET" --region "$AWS_REGION" >/dev/null 2>&1; then
        echo "   Creating bucket $DIST_OUTPUT_BUCKET"
        if [ "$AWS_REGION" = "us-east-1" ]; then
            aws s3api create-bucket --bucket "$DIST_OUTPUT_BUCKET" --region "$AWS_REGION" >/dev/null
        else
            aws s3api create-bucket --bucket "$DIST_OUTPUT_BUCKET" --region "$AWS_REGION" \
                --create-bucket-configuration "LocationConstraint=$AWS_REGION" >/dev/null
        fi
        # Secure by default: the templates are read by CloudFormation using the caller's
        # credentials, so this bucket never needs to be public.
        aws s3api put-public-access-block --bucket "$DIST_OUTPUT_BUCKET" --region "$AWS_REGION" \
            --public-access-block-configuration \
            'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true' >/dev/null
        aws s3api put-bucket-encryption --bucket "$DIST_OUTPUT_BUCKET" --region "$AWS_REGION" \
            --server-side-encryption-configuration \
            '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' >/dev/null
        aws s3api put-bucket-versioning --bucket "$DIST_OUTPUT_BUCKET" --region "$AWS_REGION" \
            --versioning-configuration 'Status=Enabled' >/dev/null
    fi

    aws s3 sync "$GLOBAL_DIR/" "s3://${DIST_OUTPUT_BUCKET}/${SOLUTION_NAME}/${VERSION}/" \
        --region "$AWS_REGION" --delete --only-show-errors

    # Lambda archives go to _lambda/ at the bucket root because backend/template.yaml
    # builds that key literally and takes no prefix parameter. The version
    # hash is in the filename, so multiple releases coexist without collision.
    aws s3 sync "$REGIONAL_DIR/" "s3://${DIST_OUTPUT_BUCKET}/_lambda/" \
        --region "$AWS_REGION" --only-show-errors

    echo "   done"
fi

# ── summary ───────────────────────────────────────────────────────────────────
cat <<SUMMARY

✅ Assets staged

   Templates:  $GLOBAL_DIR
   Artifacts:  $REGIONAL_DIR

Pass these to infrastructure/master.yaml:

   TemplateBaseUrl=$TEMPLATE_BASE_URL
   ArtifactsBucket=$DIST_OUTPUT_BUCKET
   ArtifactsVersion=$ARTIFACTS_VERSION

SUMMARY

if [ "$DO_UPLOAD" != true ]; then
    echo "Nothing was uploaded. Re-run with --upload, or copy the two directories yourself."
    echo ""
fi

# Machine-readable output for the Makefile to source.
cat > "$STATE_DIR/.artifacts-env" <<ENVFILE
# Generated by deployment/build-s3-dist.sh - do not edit.
TEMPLATE_BASE_URL=$TEMPLATE_BASE_URL
ARTIFACTS_BUCKET=$DIST_OUTPUT_BUCKET
ARTIFACTS_VERSION=$ARTIFACTS_VERSION
ENVFILE
