# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""An empty space id must never reach DynamoDB GetItem.

A physical-DB record with no member space (a candidate-only row, or an unassigned/unknown
instance) sends _can_view_instance down the fallback branch _can_view_space(auth, "").
That resolves the creator via _created_by_caller -> spaces_registry.get_space(""), and
DynamoDB rejects an empty key attribute with a ValidationException -- surfacing as a 500
on an ordinary read (e.g. GET /api/database-members/{id}). Both guards below stop the
empty id before the AWS call; the fallback decision then stays "super admin only" (via
is_space_member) with no crash.

Run:  python3 backend/tests/test_view_gate_empty_space.py
"""
import os
import pathlib
import re
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools.aws.spaces_client import SpacesRegistry  # noqa: E402

APP_SRC = (pathlib.Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def _extract(name):
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^(?:@|def |class |[A-Z_]+ = )|\Z)", APP_SRC, re.S | re.M)
    assert m, f"{name} not found"
    return m.group(0)


print("== get_space refuses an empty key before touching DynamoDB ==")
reg = SpacesRegistry()


class Boom:
    def __getattr__(self, _):
        raise AssertionError("get_space must not reach the DynamoDB table for an empty id")


reg._table = Boom()  # any table access would raise; the guard must return first
check(reg.get_space("") is None, "get_space('') is None without a GetItem call")
check(reg.get_space(None) is None, "get_space(None) is None without a GetItem call")

print("== _created_by_caller short-circuits an empty space id ==")
ns = {
    # If the guard ever falls through, these would be consulted; get_space would raise.
    "is_space_admin": lambda a: True,
    "is_admin": lambda a: True,
    "get_user_id": lambda a: "admin-1",
    "spaces_registry": types.SimpleNamespace(
        get_space=lambda sid: (_ for _ in ()).throw(AssertionError("empty id reached get_space"))),
}
exec(_extract("_created_by_caller"), ns)  # noqa: S102 - pin shipped source
check(ns["_created_by_caller"]("Bearer x", "") is False,
      "an empty space id is not creatable-by-caller (no get_space call)")

print("== the fallback branch exists in _can_view_instance (the crash site) ==")
civ = _extract("_can_view_instance")
check('_can_view_space(auth_header, "")' in civ,
      "an instance with no member spaces still falls back to _can_view_space(auth, '')")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
