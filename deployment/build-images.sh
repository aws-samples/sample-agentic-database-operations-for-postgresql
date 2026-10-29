#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Build and push the two container images master.yaml requires.
#
#   Usage: deployment/build-images.sh <RESOURCE_PREFIX> [AWS_REGION]
#
# Writes deployment/.images-env with BACKEND_IMAGE_URI and BATCH_IMAGE_URI for the
# Makefile to source. Both images are linux/arm64: the backend runs on Bedrock
# AgentCore Runtime and the scan container on Fargate arm64, so an amd64 image is
# rejected at task start rather than at build time.
#
# This is separate from build-s3-dist.sh because image URIs point at ECR in the
# deploying account, so unlike templates and Lambda archives they cannot be staged
# once by a publisher and consumed by everyone.

set -euo pipefail

if [ "$#" -lt 1 ]; then
    echo "Usage: deployment/build-images.sh <RESOURCE_PREFIX> [AWS_REGION]" >&2
    exit 1
fi

RESOURCE_PREFIX="$1"
AWS_REGION="${2:-${AWS_REGION:-us-east-1}}"

DEPLOYMENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEPLOYMENT_DIR/.." && pwd)"

BACKEND_REPO="${RESOURCE_PREFIX}-backend"
BATCH_REPO="${RESOURCE_PREFIX}-batch"

CONTAINER_CMD="${CONTAINER_CMD:-$(command -v docker 2>/dev/null \
    || command -v finch 2>/dev/null \
    || command -v podman 2>/dev/null \
    || command -v nerdctl 2>/dev/null || true)}"
if [ -z "$CONTAINER_CMD" ]; then
    echo "❌ A container runtime (docker, finch, podman or nerdctl) is required." >&2
    exit 1
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
TAG="$(date +%Y%m%d-%H%M%S)"

echo ""
echo "🐳 Building container images"
echo "   registry: $REGISTRY"
echo "   tag:      $TAG"
echo "   runtime:  $CONTAINER_CMD"
echo ""

ensure_repo() {
    if ! aws ecr describe-repositories --region "$AWS_REGION" --repository-names "$1" >/dev/null 2>&1; then
        echo "   creating ECR repository $1"
        # IMMUTABLE tags: a pushed tag cannot be re-pointed at a different image, so a
        # principal with ecr:PutImage cannot silently swap the code a running task pulls
        #. Scan on push so a vulnerable base image surfaces here rather than in prod.
        aws ecr create-repository --region "$AWS_REGION" --repository-name "$1" \
            --image-tag-mutability IMMUTABLE \
            --image-scanning-configuration scanOnPush=true \
            --encryption-configuration encryptionType=AES256 >/dev/null
    else
        # Idempotently harden a repository created before this change (or by an older
        # deploy): flipping to IMMUTABLE is safe because tags are unique build timestamps.
        aws ecr put-image-tag-mutability --region "$AWS_REGION" --repository-name "$1" \
            --image-tag-mutability IMMUTABLE >/dev/null 2>&1 || true
    fi
}
ensure_repo "$BACKEND_REPO"
ensure_repo "$BATCH_REPO"

# The URI the consumers deploy: prefer the pushed image's immutable @sha256 digest so the
# Batch job definition, ECS task and AgentCore container run an exact image rather than a
# resolvable tag. Fail-soft to the tag form if the digest cannot be read, so a
# fresh deploy is never blocked by a describe-images hiccup.
digest_uri() {
    local repo="$1" registry="$2" tag="$3" digest
    digest="$(aws ecr describe-images --region "$AWS_REGION" \
        --repository-name "$repo" --image-ids imageTag="$tag" \
        --query 'imageDetails[0].imageDigest' --output text 2>/dev/null || true)"
    if [ -n "$digest" ] && [ "$digest" != "None" ]; then
        echo "${registry}/${repo}@${digest}"
    else
        echo "${registry}/${repo}:${tag}"
    fi
}

aws ecr get-login-password --region "$AWS_REGION" \
    | "$CONTAINER_CMD" login --username AWS --password-stdin "$REGISTRY" >/dev/null
echo "   authenticated to ECR"

# ── backend / agent image ─────────────────────────────────────────────────────
BACKEND_IMAGE_URI="${REGISTRY}/${BACKEND_REPO}:${TAG}"
echo ""
echo "📦 backend -> $BACKEND_REPO:$TAG"
( cd "$REPO_ROOT/backend" && "$CONTAINER_CMD" build --platform linux/arm64 -t "$BACKEND_IMAGE_URI" . )
"$CONTAINER_CMD" push "$BACKEND_IMAGE_URI"
# Deploy by immutable digest, not the resolvable tag.
BACKEND_IMAGE_URI="$(digest_uri "$BACKEND_REPO" "$REGISTRY" "$TAG")"

# ── batch scan image ──────────────────────────────────────────────────────────
# batch/Dockerfile COPYs references.yaml and tools/ from its own build context, but
# both live under backend/. Stage them first or the build fails on a missing COPY
# source. Mirrors the staging in backend/Makefile's build-batch target; the staged
# copies are gitignored.
BATCH_IMAGE_URI="${REGISTRY}/${BATCH_REPO}:${TAG}"
echo ""
echo "📦 batch -> $BATCH_REPO:$TAG"
echo "   staging references.yaml + curated tools/ into the batch build context"
# Each copy lands under a temporary name and is renamed into place: two environments
# building at once write identical content here, and a rename is atomic, so a build
# reading the context never sees a half-written file.
stage_copy() {
    local tmp="$2.tmp.$$"
    cp "$1" "$tmp" && mv -f "$tmp" "$2"
}
stage_copy "$REPO_ROOT/backend/checks/references.yaml" "$REPO_ROOT/batch/references.yaml"
mkdir -p "$REPO_ROOT/batch/tools/aws"
stage_copy "$REPO_ROOT/backend/tools/__init__.py"           "$REPO_ROOT/batch/tools/__init__.py"
stage_copy "$REPO_ROOT/backend/tools/resource_names.py"     "$REPO_ROOT/batch/tools/resource_names.py"
# The shared XID-wraparound denominator (run_scan + findings_engine import it).
stage_copy "$REPO_ROOT/backend/tools/xid.py"                "$REPO_ROOT/batch/tools/xid.py"
stage_copy "$REPO_ROOT/backend/tools/aws/__init__.py"       "$REPO_ROOT/batch/tools/aws/__init__.py"
stage_copy "$REPO_ROOT/backend/tools/aws/metrics_store.py"  "$REPO_ROOT/batch/tools/aws/metrics_store.py"
# db_tls.py + the RDS CA bundle: the scan verifies every server certificate (psql via
# PGSSLROOTCERT, psycopg2 via sslrootcert) and refuses to run without the bundle.
stage_copy "$REPO_ROOT/backend/tools/aws/db_tls.py"         "$REPO_ROOT/batch/tools/aws/db_tls.py"
# host_routes.py: the reader scan dials a member only where the network path reaches it.
stage_copy "$REPO_ROOT/backend/tools/aws/host_routes.py"    "$REPO_ROOT/batch/tools/aws/host_routes.py"
mkdir -p "$REPO_ROOT/batch/certs"
stage_copy "$REPO_ROOT/backend/certs/rds-global-bundle.pem" "$REPO_ROOT/batch/certs/rds-global-bundle.pem"

( cd "$REPO_ROOT/batch" && "$CONTAINER_CMD" build --platform linux/arm64 -t "$BATCH_IMAGE_URI" . )
"$CONTAINER_CMD" push "$BATCH_IMAGE_URI"
# Deploy by immutable digest, not the resolvable tag.
BATCH_IMAGE_URI="$(digest_uri "$BATCH_REPO" "$REGISTRY" "$TAG")"

# ── record for the Makefile ───────────────────────────────────────────────────
# Per environment (DEPLOY_STATE_DIR from the Makefile), so another terminal's push
# cannot hand this deploy its image URIs.
STATE_DIR="${DEPLOY_STATE_DIR:-$DEPLOYMENT_DIR}"
mkdir -p "$STATE_DIR"
cat > "$STATE_DIR/.images-env" <<ENVFILE
# Generated by deployment/build-images.sh - do not edit.
BACKEND_IMAGE_URI=$BACKEND_IMAGE_URI
BATCH_IMAGE_URI=$BATCH_IMAGE_URI
ENVFILE

cat <<SUMMARY

✅ Images pushed

   BackendImageUri=$BACKEND_IMAGE_URI
   BatchImageUri=$BATCH_IMAGE_URI

SUMMARY
