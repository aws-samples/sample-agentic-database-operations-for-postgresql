#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""`make test-scan` requires INSTANCE_ID and passes it in the payload (#29).

The target invoked the scan trigger with an empty payload; the Lambda requires
instance_id and returned submitted:0, so the advertised command reported success while
scanning nothing. It now fails with a usage error when INSTANCE_ID is unset and sends it
in the payload otherwise.

Run:  python3 tests/test_make_test_scan.py
"""
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")
passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


m = re.search(r"^test-scan:\n(.*?)(?=^\S|\Z)", MAKEFILE, re.S | re.M)
assert m, "test-scan target not found"
body = m.group(1)
check('INSTANCE_ID' in body and 'exit 1' in body, "the target guards on INSTANCE_ID and exits non-zero when unset")
check('"instance_id":"$(INSTANCE_ID)"' in body, "INSTANCE_ID is sent in the Lambda payload")
check("'{}'" not in body, "the empty payload that scanned nothing is gone")

# Behavioural: `make test-scan` with no INSTANCE_ID fails with the usage error, before
# any aws call (dry-run avoids needing AWS).
# A CONFIG_FILE on the command line, so make does not ask which deployment environment
# this is (a checkout with no deploy.config asks first; see tests/test_deploy_envs.py).
_NO_ENV = "CONFIG_FILE=/nonexistent/deploy.config"
r = subprocess.run(["make", "test-scan", _NO_ENV], cwd=str(ROOT), capture_output=True, text=True,
                   stdin=subprocess.DEVNULL)
check(r.returncode != 0, "make test-scan without INSTANCE_ID fails")
check("INSTANCE_ID is required" in r.stdout + r.stderr, "it prints a clear usage error")

# With INSTANCE_ID, the expanded recipe carries it into the payload (dry run, no AWS).
r = subprocess.run(["make", "-n", "test-scan", "INSTANCE_ID=db-abc", _NO_ENV], cwd=str(ROOT),
                   capture_output=True, text=True)
check('"instance_id":"db-abc"' in r.stdout, "the payload carries the given instance id")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
