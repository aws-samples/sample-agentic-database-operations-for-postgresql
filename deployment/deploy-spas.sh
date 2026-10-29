#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Build both single-page apps, point them at the deployed stack, and publish them
# to their S3 buckets behind CloudFront.
#
#   Usage: deployment/deploy-spas.sh <STACK_NAME> [AWS_REGION]
#
# Runs AFTER the CloudFormation stack, because every value config.js needs is a
# stack output. The bundle itself is environment-agnostic: index.html loads /config.js
# as a classic script before the module bundle, so the deploy writes one small file
# instead of rebuilding the app or string-substituting the minified output.

set -euo pipefail

if [ "$#" -lt 1 ]; then
    echo "Usage: deployment/deploy-spas.sh <STACK_NAME> [AWS_REGION]" >&2
    exit 1
fi

STACK_NAME="$1"
AWS_REGION="${2:-${AWS_REGION:-us-east-1}}"

DEPLOYMENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEPLOYMENT_DIR/.." && pwd)"
# shellcheck source=./build-lock.sh
. "$DEPLOYMENT_DIR/build-lock.sh"

command -v npm >/dev/null 2>&1 || { echo "❌ npm is required." >&2; exit 1; }

echo ""
echo "🎨 Publishing the web apps"
echo "   stack:  $STACK_NAME"
echo "   region: $AWS_REGION"

# ── read every stack output in one call ───────────────────────────────────────
OUTPUTS_JSON="$(aws cloudformation describe-stacks \
    --region "$AWS_REGION" --stack-name "$STACK_NAME" \
    --query 'Stacks[0].Outputs' --output json)"

out() {
    printf '%s' "$OUTPUTS_JSON" | python3 -c "
import json, sys
key = sys.argv[1]
for o in json.load(sys.stdin) or []:
    if o['OutputKey'] == key:
        print(o['OutputValue']); break
" "$1"
}

API_URL="$(out ApiUrl)"
COGNITO_DOMAIN="$(out CognitoDomain)"
CLIENT_ID="$(out WebClientId)"
DASHBOARD_URL="$(out DashboardUrl)"
ADMIN_URL="$(out AdminConsoleUrl)"
FRONTEND_BUCKET="$(out FrontendBucketName)"
FRONTEND_DIST="$(out FrontendDistributionId)"
ADMIN_BUCKET="$(out AdminBucketName)"
ADMIN_DIST="$(out AdminDistributionId)"
RUNTIME_ARN="$(out AgentRuntimeArn)"
APP_NAME="${APP_NAME:-DBOps Agent (Beta)}"
# The admin console builds the Secrets Manager name an operator is told to create, and
# the deployment's IAM policies authorise exactly this prefix. Falls back to the same
# default as the CloudFormation parameter and admin-console/src/config.js, so all three
# agree when the stack predates the output.
RESOURCE_PREFIX="$(out ResourcePrefix)"
RESOURCE_PREFIX="${RESOURCE_PREFIX:-dbops}"

for pair in "ApiUrl:$API_URL" "CognitoDomain:$COGNITO_DOMAIN" "WebClientId:$CLIENT_ID" \
            "FrontendBucketName:$FRONTEND_BUCKET" "AdminBucketName:$ADMIN_BUCKET"; do
    if [ -z "${pair#*:}" ]; then
        echo "❌ Stack output ${pair%%:*} is empty - is $STACK_NAME fully deployed?" >&2
        exit 1
    fi
done

# The operator app streams chat straight from AgentCore Runtime rather than through the
# REST API, and that endpoint is derived from the runtime ARN rather than published as a
# URL. Empty is tolerated: App.jsx falls back to config.localBackendUrl.
DEPLOYED_BACKEND_URL=""
if [ -n "$RUNTIME_ARN" ] && [ "$RUNTIME_ARN" != "None" ]; then
    ENCODED_ARN="$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$RUNTIME_ARN")"
    DEPLOYED_BACKEND_URL="https://bedrock-agentcore.${AWS_REGION}.amazonaws.com/runtimes/${ENCODED_ARN}/invocations?qualifier=DEFAULT"
fi

echo "   api:       $API_URL"
echo "   operator:  $DASHBOARD_URL"
echo "   admin:     $ADMIN_URL"

# Escape for embedding in a double-quoted JS string literal. Same helper the container
# entrypoints use, so both paths produce identical config.js.
js_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

# ── build, configure, publish ─────────────────────────────────────────────────
publish() {
    local app="$1" bucket="$2" distribution="$3"
    shift 3
    # Remaining arguments are key=value config entries.

    echo ""
    echo "── $app ──"
    # Each environment builds into its own directory and writes its own config.js
    # there, so two terminals publishing two environments cannot ship each other's
    # API URL or Cognito client. The install and build share node_modules, so they run
    # one environment at a time (the lock); the publish below runs in parallel.
    local dist="$REPO_ROOT/$app/dist"
    if [ -n "${DEPLOY_STATE_DIR:-}" ] && [ "$DEPLOY_STATE_DIR" != "$DEPLOYMENT_DIR" ]; then
        dist="$DEPLOY_STATE_DIR/spa/$app"
    fi
    build_lock_acquire "$REPO_ROOT/$app/.build.lock" "$app build"
    ( cd "$REPO_ROOT/$app" && {
        # Fail closed on a dependency mismatch: `npm ci` installs exactly the
        # committed lock file, so an out-of-step lock is a real problem to resolve, not a
        # cue to silently resolve fresh versions. A `|| npm install` fallback would
        # discard the lock on any failure and ship whatever npm resolved, so there is none.
        # stderr is kept so the reason is visible.
        npm ci
        npm run build --silent -- --outDir "$dist" --emptyOutDir
    } )
    build_lock_release

    [ -d "$dist" ] || { echo "❌ $app build output not found at $dist." >&2; exit 1; }

    # Written after the build on purpose: `npm run build` copies public/config.js (the
    # empty placeholder) into dist/, so writing it earlier would be overwritten.
    {
        printf '// Generated by deployment/deploy-spas.sh. Do not edit.\n'
        printf 'window.__APP_CONFIG__ = {\n'
        local entry
        for entry in "$@"; do
            printf '  %s: "%s",\n' "${entry%%=*}" "$(js_escape "${entry#*=}")"
        done
        printf '};\n'
    } > "$dist/config.js"

    # Sync the immutable hashed assets first, then the entry points, so a browser can
    # never fetch a new index.html that references chunks not yet uploaded.
    aws s3 sync "$dist/" "s3://$bucket/" --region "$AWS_REGION" \
        --exclude 'index.html' --exclude 'config.js' --only-show-errors
    aws s3 cp "$dist/config.js" "s3://$bucket/config.js" --region "$AWS_REGION" \
        --cache-control 'no-cache, no-store, must-revalidate' --only-show-errors
    aws s3 cp "$dist/index.html" "s3://$bucket/index.html" --region "$AWS_REGION" \
        --cache-control 'no-cache, no-store, must-revalidate' --only-show-errors
    # Remove assets from previous releases now that nothing references them.
    aws s3 sync "$dist/" "s3://$bucket/" --region "$AWS_REGION" --delete --only-show-errors

    if [ -n "$distribution" ] && [ "$distribution" != "None" ]; then
        aws cloudfront create-invalidation --distribution-id "$distribution" \
            --paths '/*' --query 'Invalidation.Id' --output text >/dev/null
        echo "   invalidated $distribution"
    fi
    echo "   published to s3://$bucket"
}

publish frontend "$FRONTEND_BUCKET" "$FRONTEND_DIST" \
    "apiUrl=$API_URL" \
    "cognitoDomain=$COGNITO_DOMAIN" \
    "clientId=$CLIENT_ID" \
    "awsRegion=$AWS_REGION" \
    "deployedBackendUrl=$DEPLOYED_BACKEND_URL" \
    "localBackendUrl=http://localhost:8084" \
    "appName=$APP_NAME"

publish admin-console "$ADMIN_BUCKET" "$ADMIN_DIST" \
    "apiUrl=$API_URL" \
    "cognitoDomain=$COGNITO_DOMAIN" \
    "clientId=$CLIENT_ID" \
    "awsRegion=$AWS_REGION" \
    "webAppUrl=$DASHBOARD_URL" \
    "resourcePrefix=$RESOURCE_PREFIX" \
    "appName=$APP_NAME"

cat <<SUMMARY

✅ Web apps published

   Operator app:  $DASHBOARD_URL
   Admin console: $ADMIN_URL

SUMMARY
