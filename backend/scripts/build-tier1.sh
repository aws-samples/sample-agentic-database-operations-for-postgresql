#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

set -e

# Build and upload Tier 1 Lambda collector deployment artifacts
# Usage: bash scripts/build-tier1.sh   (run from backend/)
#
# Requires: .env with AWS_REGION set
# Creates:  tier1_collector.zip and psycopg2-layer.zip, uploads to S3

echo "🔧 Building Tier 1 Lambda collector..."

if [ ! -f .env ]; then
    echo "❌ .env not found. Generate it from the repository root: make env"
    exit 1
fi

set -a && . ./.env && set +a

AWS_REGION="${AWS_REGION:-us-east-1}"

# Check the metrics store is configured
if [ -z "$TIER1_METRICS_TABLE" ]; then
    echo "⚠️  TIER1_METRICS_TABLE not set in .env — skipping Tier 1 build"
    echo "   Deploy the stack first: make deploy (from the project root)"
    exit 0
fi

# Get the reports S3 bucket (already exists from storage stack)
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
CODE_BUCKET="${REPORTS_S3_BUCKET}"

if [ -z "$CODE_BUCKET" ]; then
    echo "❌ REPORTS_S3_BUCKET not set in .env"
    exit 1
fi

echo "   Using bucket: $CODE_BUCKET (_lambda/ prefix)"

TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

# 1. Package the Lambda code (+ the metrics_store helper it lazy-imports to write
#    the DynamoDB rows and publish to CloudWatch; psycopg2, for the MONITORED
#    PostgreSQL databases, comes from the layer, boto3 from the Lambda runtime)
echo "📦 Packaging tier1_collector.py..."
cp tier1_collector.py "$TMPDIR/tier1_collector.py"
mkdir -p "$TMPDIR/tools/aws"
cp tools/__init__.py "$TMPDIR/tools/__init__.py"
# resource_names.py resolves table names from RESOURCE_PREFIX; tier1_collector.py
# imports it, so it MUST be in the zip or the Lambda fails with ModuleNotFoundError.
cp tools/resource_names.py "$TMPDIR/tools/resource_names.py"
# tier1_collector.py imports the shared XID-wraparound denominator (tools/xid.py).
cp tools/xid.py "$TMPDIR/tools/xid.py"
cp tools/aws/__init__.py "$TMPDIR/tools/aws/__init__.py"
cp tools/aws/metrics_store.py "$TMPDIR/tools/aws/metrics_store.py"
# db_tls.py picks the TLS mode and the CA bundle for every database connection, and
# the bundle itself (certs/, resolved relative to the code root) is what lets the
# collector VERIFY the server certificate instead of merely encrypting. Both are
# required: without the bundle db_tls fails closed and the collector cannot connect.
cp tools/aws/db_tls.py "$TMPDIR/tools/aws/db_tls.py"
# host_routes.py: the dial gate and the cross-Region /32 drift check (the API task
# repairs). audit_log.py records failovers as system admin events; it looks a space up
# through spaces_client.py, which needs teardown_claim.py and ddb_pages.py.
cp tools/aws/host_routes.py "$TMPDIR/tools/aws/host_routes.py"
cp tools/aws/audit_log.py "$TMPDIR/tools/aws/audit_log.py"
cp tools/aws/spaces_client.py "$TMPDIR/tools/aws/spaces_client.py"
cp tools/aws/teardown_claim.py "$TMPDIR/tools/aws/teardown_claim.py"
cp tools/aws/ddb_pages.py "$TMPDIR/tools/aws/ddb_pages.py"
# The alarm notifier Lambda (Handler alarm_notifier.handler) ships in this same zip,
# with the alarm catalogue it reads (db_alarms.py) and tools/xid.py for its thresholds.
cp alarm_notifier.py "$TMPDIR/alarm_notifier.py"
cp tools/aws/db_alarms.py "$TMPDIR/tools/aws/db_alarms.py"
mkdir -p "$TMPDIR/certs"
cp certs/rds-global-bundle.pem "$TMPDIR/certs/rds-global-bundle.pem"
(cd "$TMPDIR" && zip -qr tier1_collector.zip tier1_collector.py alarm_notifier.py tools certs)

# 2. Build psycopg2 Lambda layer using Docker
echo "📦 Building psycopg2 Lambda layer..."

# The pin comes from requirements.txt so the layer built here and the one built by
# deployment/build-s3-dist.sh can never disagree about the version. Fails closed:
# a silently missing pin would install an unpinned psycopg2 into the layer.
PSYCOPG2_PIN="$(grep -E '^psycopg2-binary==' requirements.txt | head -1)"
if [ -z "$PSYCOPG2_PIN" ]; then
    echo "❌ Could not find a psycopg2-binary pin in backend/requirements.txt"
    exit 1
fi
echo "   pin: $PSYCOPG2_PIN (from requirements.txt)"

CONTAINER_CMD="${CONTAINER_CMD:-$(command -v docker 2>/dev/null \
    || command -v finch 2>/dev/null \
    || command -v podman 2>/dev/null \
    || command -v nerdctl 2>/dev/null || echo "")}"
if [ -z "$CONTAINER_CMD" ]; then
    echo "❌ A container runtime (docker, finch, podman or nerdctl) is required to build the psycopg2 layer"
    exit 1
fi

mkdir -p "$TMPDIR/layer-build"

# Use a project-local build dir to avoid macOS temp dir volume mount issues
LAYER_BUILD_DIR="$(pwd)/.tier1-build"
rm -rf "$LAYER_BUILD_DIR"
mkdir -p "$LAYER_BUILD_DIR"

$CONTAINER_CMD run --rm \
    --platform linux/arm64 \
    --entrypoint "" \
    -v "$LAYER_BUILD_DIR:/out" \
    public.ecr.aws/lambda/python:3.13 \
    bash -c "
        pip install $PSYCOPG2_PIN -t /out/python/lib/python3.13/site-packages/ --quiet
    "

# Verify files exist
if [ ! -d "$LAYER_BUILD_DIR/python" ]; then
    echo "❌ psycopg2 install failed — python/ directory not found in $LAYER_BUILD_DIR"
    ls -la "$LAYER_BUILD_DIR/"
    exit 1
fi

# The RDS CA bundle rides in the layer too, at /opt/certs/ once mounted, so any inline
# Lambda that connects to a monitored database can verify its certificate (RDS_CA_BUNDLE).
mkdir -p "$LAYER_BUILD_DIR/certs"
cp certs/rds-global-bundle.pem "$LAYER_BUILD_DIR/certs/rds-global-bundle.pem"

# Create the zip on the host
(cd "$LAYER_BUILD_DIR" && zip -qr "$TMPDIR/psycopg2-layer.zip" python/ certs/)
rm -rf "$LAYER_BUILD_DIR"

echo "   Layer size: $(du -h "$TMPDIR/psycopg2-layer.zip" | cut -f1)"

# 3. Upload to S3
echo "📤 Uploading to s3://$CODE_BUCKET/_lambda/..."
aws s3 cp "$TMPDIR/tier1_collector.zip" "s3://$CODE_BUCKET/_lambda/tier1_collector.zip" --region "$AWS_REGION"
aws s3 cp "$TMPDIR/psycopg2-layer.zip" "s3://$CODE_BUCKET/_lambda/psycopg2-layer.zip" --region "$AWS_REGION"

echo "✅ Tier 1 Lambda artifacts uploaded"
echo "   s3://$CODE_BUCKET/_lambda/tier1_collector.zip"
echo "   s3://$CODE_BUCKET/_lambda/psycopg2-layer.zip"
echo ""
echo "   Next: run 'make deploy' to update the CloudFormation stack"
