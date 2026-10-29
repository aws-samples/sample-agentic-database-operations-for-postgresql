#!/bin/sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Runtime configuration for the frontend container.
#
# Writes the environment provided at container start into a single `config.js`,
# which index.html loads as a classic script before the module bundle. The same
# file is what the S3/CloudFront deploy writes, so container and static hosting
# share one mechanism. The built bundle is environment-agnostic and is never
# rewritten in place.

set -e

WEB_ROOT="/usr/share/nginx/html"

# Escape for embedding in a double-quoted JS string literal.
js_escape() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

{
  printf '// Generated at container start by docker-entrypoint.sh. Do not edit.\n'
  printf 'window.__APP_CONFIG__ = {\n'
  printf '  apiUrl: "%s",\n'             "$(js_escape "${VITE_API_URL:-}")"
  printf '  cognitoDomain: "%s",\n'      "$(js_escape "${VITE_COGNITO_DOMAIN:-}")"
  printf '  clientId: "%s",\n'           "$(js_escape "${VITE_CLIENT_ID:-}")"
  printf '  awsRegion: "%s",\n'          "$(js_escape "${VITE_AWS_REGION:-}")"
  printf '  deployedBackendUrl: "%s",\n' "$(js_escape "${VITE_DEPLOYED_BACKEND_URL:-}")"
  printf '  localBackendUrl: "%s",\n'    "$(js_escape "${VITE_LOCAL_BACKEND_URL:-}")"
  printf '  appName: "%s",\n'            "$(js_escape "${VITE_APP_NAME:-}")"
  printf '};\n'
} > "$WEB_ROOT/config.js"

exec "$@"
