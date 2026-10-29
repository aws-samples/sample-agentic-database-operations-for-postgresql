#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for backend/tools/resource_names.py.

Two things are checked, and the second is the valuable one:

  1. Name resolution: defaults, a configured prefix, per-name environment
     overrides, blank-prefix fallback, and an unknown key raising.

  2. That the module is importable **from the two stripped-down layouts it is
     bundled into** - the Tier 1 Lambda zip and the Batch scan image. Both carry
     only a curated subset of ``tools/``, so an import this module grows could
     resolve fine in the repo and then fail at runtime with ModuleNotFoundError.
     Each layout is reconstructed in a temp directory and imported in a
     subprocess with an empty PYTHONPATH, which is the closest cheap analogue of
     the real environment.

    python3 tests/test_resource_names.py      (from the repository root)
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"

# The curated subset both bundles carry. Kept in step with
# backend/scripts/build-tier1.sh and deployment/build-images.sh; the staging
# check in deployment/build-s3-dist.sh enforces the tier1 half.
BUNDLED = (
    "tools/__init__.py",
    "tools/resource_names.py",
    "tools/aws/__init__.py",
    "tools/aws/metrics_store.py",
    "tools/aws/db_tls.py",
)

failures = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}: {got!r}")
    else:
        print(f"  FAIL {label}: {got!r} (want {want!r})")
        failures.append(label)


def stage(tmp: Path, entrypoint: Path) -> None:
    """Reconstruct a bundle layout: the entry-point module beside a curated tools/."""
    (tmp / "tools" / "aws").mkdir(parents=True)
    shutil.copy(entrypoint, tmp / entrypoint.name)
    for rel in BUNDLED:
        shutil.copy(BACKEND / rel, tmp / rel)


def import_in(tmp: Path, snippet: str, prefix: str) -> str:
    """Run a snippet with tmp as the working directory and no inherited PYTHONPATH."""
    result = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=tmp, capture_output=True, text=True,
        env={**os.environ, "RESOURCE_PREFIX": prefix, "PYTHONPATH": ""},
    )
    if result.returncode:
        print("    stderr:", result.stderr.strip()[:400])
    return result.stdout.strip()


def main() -> int:
    sys.path.insert(0, str(BACKEND))
    # Start from a clean slate: a stray override in the caller's environment would
    # otherwise make the default-value assertions pass or fail for the wrong reason.
    for key in ("RESOURCE_PREFIX", "SPACES_TABLE", "DB_CONFIG_TABLE", "REPORTS_S3_BUCKET"):
        os.environ.pop(key, None)

    from tools.resource_names import (  # noqa: E402 - after the sys.path insert
        DEFAULT_PREFIX, resource_prefix, table_name, function_name,
        managed_by_tag, security_group_name, reports_bucket,
    )

    print("── defaults ──")
    check("prefix", resource_prefix(), "dbops")
    check("spaces table", table_name("spaces"), "dbops-spaces")
    check("instances table", table_name("instances"), "dbops-instances")
    check("user_knowledge table", table_name("user_knowledge"), "dbops-user-knowledge")
    check("tier1 function", function_name("tier1_collector"), "dbops-tier1-collector")
    check("ManagedBy tag", managed_by_tag(), "dbops-agent")
    check("VPC endpoint sg", security_group_name("vpce-sg"), "dbops-vpce-sg")
    check("reports fallback", reports_bucket(), "dbops-reports")

    print("── configured prefix ──")
    os.environ["RESOURCE_PREFIX"] = "acme"
    check("prefix", resource_prefix(), "acme")
    check("spaces table", table_name("spaces"), "acme-spaces")
    # Load-bearing for isolation: the network configurator queries by this tag to
    # find the peering connections and routes it owns.
    check("ManagedBy tag", managed_by_tag(), "acme-agent")

    print("── a per-name override wins over the prefix ──")
    os.environ["SPACES_TABLE"] = "legacy-spaces-table"
    check("override applied", table_name("spaces"), "legacy-spaces-table")
    check("sibling unaffected", table_name("config"), "acme-config")
    del os.environ["SPACES_TABLE"]

    print("── blank prefix falls back to the default ──")
    os.environ["RESOURCE_PREFIX"] = "   "
    check("blank -> default", resource_prefix(), DEFAULT_PREFIX)
    del os.environ["RESOURCE_PREFIX"]

    print("── an unknown key raises rather than inventing a name ──")
    try:
        table_name("nope")
        print("  FAIL unknown key did not raise")
        failures.append("unknown key")
    except KeyError as exc:
        print(f"  ok   raises KeyError: {str(exc)[:60]}")

    print("── importable from the staged Tier 1 Lambda zip layout ──")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        stage(tmp, BACKEND / "tier1_collector.py")
        got = import_in(
            tmp,
            "from tools.resource_names import table_name; print(table_name('spaces'))",
            "zip",
        )
        check("resolves in zip layout", got, "zip-spaces")

    print("── importable from the staged Batch image layout ──")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        stage(tmp, REPO / "batch" / "run_scan.py")
        got = import_in(
            tmp,
            "from tools.resource_names import reports_bucket, table_name; "
            "print(table_name('instances'), reports_bucket())",
            "bat",
        )
        check("resolves in batch layout", got, "bat-instances bat-reports")

    print()
    if failures:
        print(f"FAIL - {len(failures)}: {', '.join(failures)}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
