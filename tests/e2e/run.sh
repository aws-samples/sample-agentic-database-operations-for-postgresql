#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# ──────────────────────────────────────────────────────────────────────────────
# End-to-end smoke tests against a deployed stack.
#
#   make smoke                       (resolves the URL from the deployed stack)
#   bash tests/e2e/run.sh <app-url>  (explicit URL)
#
# Run this after `make deploy`. A non-zero exit means the deployment is not
# serving. These tests need a live deployment, so they are separate from
# `make check` (static, no AWS) and `make test` (unit, no AWS).
#
# ── Adding more tests ──
#   * an executable  tests/e2e/*.sh      - receives the URL as $1, or
#   * a              tests/e2e/test_*.py - pytest, reads APP_URL from the env.
# This runner discovers and runs both. Nothing else needs changing.
# ──────────────────────────────────────────────────────────────────────────────
set -uo pipefail
APP_URL="${1:-${APP_URL:-}}"
DIR="$(cd "$(dirname "$0")" && pwd)"
fail=0

echo "════════════════════════════════════════════════"
echo " Smoke tests against: ${APP_URL:-<none>}"
echo "════════════════════════════════════════════════"

# 1) Built-in smoke test — the deployed app must be reachable.
if [ -n "$APP_URL" ]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$APP_URL" 2>/dev/null || echo 000)
  echo "[smoke] GET $APP_URL -> HTTP $code"
  case "$code" in
    2*|3*|401|403) echo "  ✓ reachable" ;;
    *) echo "  ✗ unreachable"; fail=1 ;;
  esac
else
  echo "[smoke] no URL provided — skipping"
fi

# 2) Extra shell tests: any other *.sh in this directory.
for t in "$DIR"/*.sh; do
  [ "$t" = "$DIR/run.sh" ] && continue
  [ -f "$t" ] || continue
  echo "── running $(basename "$t") ──"
  bash "$t" "$APP_URL" || { echo "  ✗ $(basename "$t") failed"; fail=1; }
done

# 3) Pytest tests: any test_*.py in this directory.
if ls "$DIR"/test_*.py >/dev/null 2>&1; then
  if command -v python3 >/dev/null 2>&1; then
    python3 -m pip install --quiet pytest requests 2>/dev/null || true
    APP_URL="$APP_URL" python3 -m pytest "$DIR" -q || fail=1
  fi
fi

echo "════════════════════════════════════════════════"
[ "$fail" -eq 0 ] && echo " ✅ ALL SMOKE TESTS PASSED" || echo " ❌ SMOKE TESTS FAILED"
echo "════════════════════════════════════════════════"
exit "$fail"
