#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Regenerate the pinned, hashed dependency locks (requirements.txt) from the
# human-edited requirements.in sources, targeting the image's Python version.
#
# The images install with `pip install --require-hashes -r requirements.txt`, so the
# lock must list every dependency (including transitive ones) with its hash. Edit
# requirements.in, then run `make lock` (or this script) to refresh the lock.
#
# Uses uv if available (resolves for the target Python from any host), else pip-compile.
set -euo pipefail

PYVER=3.13

gen() {
  local dir="$1"
  if command -v uv >/dev/null 2>&1; then
    uv pip compile "$dir/requirements.in" --generate-hashes --universal \
      --python-version "$PYVER" -o "$dir/requirements.txt"
  elif command -v pip-compile >/dev/null 2>&1; then
    pip-compile --generate-hashes --output-file "$dir/requirements.txt" "$dir/requirements.in"
  else
    echo "ERROR: need 'uv' or 'pip-compile' (pip-tools) to regenerate the locks." >&2
    echo "  pip install uv    # or: pip install pip-tools" >&2
    exit 1
  fi
  echo "  wrote $dir/requirements.txt"
}

cd "$(dirname "$0")/.."
echo "Regenerating hashed dependency locks (Python $PYVER)..."
gen backend
gen batch
echo "Done. Review the diff, then rebuild images to verify (make images)."
