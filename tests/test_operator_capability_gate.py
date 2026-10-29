#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Operator-only capabilities gate on operator membership, not view access (review #1).

Rule 5: creating or administering a space does not make someone an operator. The chat
turn (`/invocations`) and the investigation lifecycle actions (create / cancel /
complete, plus the investigation branch inside `/invocations`) are operator-only, so a
super admin or a creator who was never enrolled as an operator must be refused, while an
operator is allowed. Genuine admin reads (listing and viewing investigations) keep
`_can_view_space`.

Asserted against source (no AWS, no app import), plus the auth-helper semantics directly.

    python3 tests/test_operator_capability_gate.py      (from the repository root)
"""
import base64
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from utils.auth import is_operator_of, space_group_name  # noqa: E402

APP = (ROOT / "backend/app.py").read_text(encoding="utf-8")

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def route_body(decorator: str) -> str:
    start = APP.find(decorator)
    if start == -1:
        return ""
    nxt = APP.find("\n@app.", start + len(decorator))
    return APP[start: nxt if nxt != -1 else len(APP)]


def bearer(groups) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "u-1", "cognito:groups": groups}).encode())
    return "Bearer x." + payload.decode().rstrip("=") + ".y"


SPACE_A = "aaaaaaaa-1111-2222-3333-444444444444"

# ── the auth predicate: admin / creator role does NOT grant operator capability ──
print("══ is_operator_of: membership only ══")
# A super admin with no operator group is refused.
check(not is_operator_of(bearer(["admins"]), SPACE_A),
      "a super admin who is not an operator is refused")
# A space admin (role) with no operator group is refused.
check(not is_operator_of(bearer(["space-admins"]), SPACE_A),
      "a space admin who is not an operator is refused")
# An operator of the space is allowed.
check(is_operator_of(bearer([space_group_name(SPACE_A)]), SPACE_A),
      "an operator of the space is allowed")
# A super admin who is ALSO an operator is allowed.
check(is_operator_of(bearer(["admins", space_group_name(SPACE_A)]), SPACE_A),
      "a super admin enrolled as an operator is allowed")

# ── the chat turn (/invocations) gates the chat path on is_operator_of ──
print("")
print("══ /invocations: chat and the investigation branch gate on operator membership ══")
inv_body = route_body('@app.post("/invocations")')
check(bool(inv_body), "the route exists")
check("not is_operator_of(auth_header, space_id)" in inv_body,
      "the chat path gates on is_operator_of, not _can_view_space")
check("not is_operator_of(auth_header, inv_space_id)" in inv_body,
      "the investigation branch gates on is_operator_of, not _can_view_space")
# No _can_view_space anywhere in the turn.
check("_can_view_space(" not in inv_body,
      "the agent turn no longer admits a non-operator through _can_view_space")

# ── the investigation lifecycle actions gate on is_operator_of ──
print("")
print("══ investigation lifecycle: create / cancel / complete are operator-only ══")
for dec in ('@app.post("/api/spaces/{space_id}/investigations")',
            '@app.post("/api/spaces/{space_id}/investigations/{investigation_id}/cancel")',
            '@app.post("/api/spaces/{space_id}/investigations/{investigation_id}/complete")'):
    body = route_body(dec)
    check(bool(body), f"{dec} exists")
    check(re.search(r"not is_operator_of\(", body) is not None,
          f"{dec} gates on is_operator_of")
    check("_can_view_space(" not in body, f"{dec} no longer gates on _can_view_space")

# ── genuine admin reads keep _can_view_space (listing + viewing) ──
print("")
print("══ listing and viewing investigations stay admin-readable (_can_view_space) ══")
for dec in ('@app.get("/api/spaces/{space_id}/investigations")',
            '@app.get("/api/spaces/{space_id}/investigations/{investigation_id}")'):
    body = route_body(dec)
    check(bool(body), f"{dec} exists")
    check("_can_view_space(" in body, f"{dec} keeps _can_view_space for admin reads")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
