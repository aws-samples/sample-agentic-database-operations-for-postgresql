#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The member components the two front ends share stay identical.

``ClusterMembers.jsx``, ``MemberPicker.jsx`` and ``livePoll.js`` exist in both ``frontend/src/components``
(the operator app) and ``admin-console/src/components``. They must not drift into
showing different member data or different pickers for the same cluster, so the two
copies are compared line by line, ignoring only the one comment line that points at the
other copy.

Run:  python3 tests/test_shared_components.py
"""
import difflib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHARED = ("ClusterMembers.jsx", "MemberPicker.jsx", "livePoll.js")
CROSS_REF = ("keeps an identical copy (admin-console/src/components)",
             "keeps an identical copy (frontend/src/components)")
passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def body(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    refs = [ln for ln in lines if any(c in ln for c in CROSS_REF)]
    return [ln for ln in lines if ln not in refs], refs


for name in SHARED:
    a, a_refs = body(ROOT / "frontend/src/components" / name)
    b, b_refs = body(ROOT / "admin-console/src/components" / name)
    check(len(a_refs) == 1 and len(b_refs) == 1, f"{name}: each copy has its one cross-reference comment")
    diff = list(difflib.unified_diff(a, b, "frontend", "admin-console", lineterm="", n=1))
    check(not diff, f"{name}: the two copies are identical" + ("\n" + "\n".join(diff[:20]) if diff else ""))

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
