#!/bin/sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Runtime configuration for the admin console container.
# See frontend/docker-entrypoint.sh for the rationale.

set -e

WEB_ROOT="/usr/share/nginx/html"

# Escape for embedding in a double-quoted JS string literal.
js_escape() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

{
  printf '// Generated at container start by docker-entrypoint.sh. Do not edit.\n'
  printf 'window.__APP_CONFIG__ = {\n'
  printf '  apiUrl: "%s",\n'        "$(js_escape "${VITE_API_URL:-}")"
  printf '  cognitoDomain: "%s",\n' "$(js_escape "${VITE_COGNITO_DOMAIN:-}")"
  printf '  clientId: "%s",\n'      "$(js_escape "${VITE_CLIENT_ID:-}")"
  printf '  awsRegion: "%s",\n'     "$(js_escape "${VITE_AWS_REGION:-}")"
  printf '  webAppUrl: "%s",\n'     "$(js_escape "${VITE_WEBAPP_URL:-}")"
  printf '  appName: "%s",\n'       "$(js_escape "${VITE_APP_NAME:-}")"
  printf '};\n'
} > "$WEB_ROOT/config.js"

exec "$@"
