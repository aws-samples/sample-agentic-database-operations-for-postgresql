#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Guard: creating a space never makes the creator an operator of it.

The space's Cognito group is its OPERATOR group: the operator app lists a space only for
the holders of that group (GET /api/my-spaces), and the Operators page lists its members.
The creator manages and views the space through `created_by`, so enrolling them into the
group served no purpose and silently made every creator an operator. An admin who wants
the operator app for a space adds themselves as an operator like anyone else.

This pins both halves: create_space adds nobody to any group, and the creator still sees
the space in the admin console and passes the view and manage gates without the group.

Run:  python3 tests/test_creator_not_operator.py
"""
import pathlib
import re
import sys
import types

APP_SRC = (pathlib.Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
passed = failed = 0


def check(cond, label):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}")


def _extract(name):
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^(?:@|def |class )|\Z)", APP_SRC, re.S | re.M)
    assert m, f"{name} not found in app.py"
    return m.group(0)


print("== create_space adds nobody to a group ==")
create_src = _extract("create_space")
check("admin_add_user_to_group" not in create_src, "create_space makes no Cognito group change")
check("_enroll_user_in_space_group" not in APP_SRC, "the creator-enrolment helper is gone")

print("")
print("== the creator still reaches their space without the group ==")
SPACE_ID = "sp-1"
CREATOR = "sub-creator"
SPACES = [{"space_id": SPACE_ID, "created_by": CREATOR}, {"space_id": "sp-2", "created_by": "someone-else"}]
ns = {
    "spaces_registry": types.SimpleNamespace(list_spaces=lambda: SPACES,
                                             get_space=lambda sid: next((s for s in SPACES if s["space_id"] == sid), None)),
    "space_group_name": lambda sid: f"space-{sid}",
}
TOKENS = {}
ns["get_user_groups"] = lambda auth: TOKENS[auth]["groups"]
ns["get_user_id"] = lambda auth: TOKENS[auth]["sub"]
ns["is_space_admin"] = lambda auth: "space-admins" in TOKENS[auth]["groups"]
ns["is_admin"] = lambda auth: "admins" in TOKENS[auth]["groups"]
ns["is_space_member"] = lambda auth, sid: ns["is_admin"](auth) or f"space-{sid}" in TOKENS[auth]["groups"]
for fn in ("_accessible_space_ids", "_created_by_caller", "_can_view_space", "_can_manage_space"):
    exec(_extract(fn), ns)  # noqa: S102 - pinning shipped source

TOKENS["creator"] = {"sub": CREATOR, "groups": ["space-admins"]}            # no space-sp-1 group
TOKENS["super"] = {"sub": "sub-super", "groups": ["admins"]}
TOKENS["operator"] = {"sub": "sub-op", "groups": ["operators", f"space-{SPACE_ID}"]}

check(ns["_accessible_space_ids"]("creator") == {SPACE_ID}, "the creator's console lists their space, and only it")
check(ns["_can_view_space"]("creator", SPACE_ID), "the creator can read the space without its group")
check(ns["_can_manage_space"]("creator", SPACE_ID), "the creator manages the space without its group")
check(not ns["_can_manage_space"]("creator", "sp-2"), "the creator does not manage another admin's space")
check(ns["_accessible_space_ids"]("super") is None, "a super admin sees every space")
check(ns["_can_view_space"]("operator", SPACE_ID) and not ns["_can_manage_space"]("operator", SPACE_ID),
      "an operator added on purpose uses the space but never manages it")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
