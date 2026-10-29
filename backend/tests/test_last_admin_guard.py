# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The last super admin can never remove itself (#2).

A fresh deployment has one bootstrap admin. If that account could change its own role
away from `admins` or delete itself, nobody would be left who can open the admin console
— recovery would need a redeploy. So before removing `admins` from a user, or deleting an
admin account, the backend lists the `admins` group and refuses (409 with a recovery
message) unless another USABLE super admin would remain. "Usable" = enabled and in a
sign-in-capable state. With a second admin present, both actions are allowed again.

Handlers and the guard helper are extracted from backend/app.py and run against a fake
Cognito. Hermetic, no AWS.

Run:  python3 backend/tests/test_last_admin_guard.py
"""
import os
import pathlib
import re
import sys
import types

APP_SRC = (pathlib.Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
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
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^(?:@|def |async def |class |[A-Z_]+ = )|\Z)",
                  APP_SRC, re.S | re.M)
    assert m, f"{name} not found in app.py"
    return m.group(0)


def _const(name):
    # Matches `NAME = <value>` where the value may be a parenthesised, multi-line
    # string literal (ends on a line that is just `)`), or a single-line value.
    m = re.search(rf"^{name} = \(.*?\n\)\n", APP_SRC, re.S | re.M)
    if not m:
        m = re.search(rf"^{name} = .*$", APP_SRC, re.M)
    assert m, f"{name} not found in app.py"
    return m.group(0)


class UserNotFound(Exception):
    pass


class FakeCognito:
    """A pool of admins. `users` maps username -> (groups set, status, enabled)."""
    def __init__(self, users):
        self.users = {u: {"groups": set(g), "status": s, "enabled": e}
                      for u, (g, s, e) in users.items()}
        self.calls = []
        self.exceptions = types.SimpleNamespace(UserNotFoundException=UserNotFound)

    def admin_get_user(self, UserPoolId, Username):
        if Username not in self.users:
            raise UserNotFound(Username)
        return {"Username": Username,
                "UserAttributes": [{"Name": "email", "Value": Username},
                                   {"Name": "sub", "Value": "sub-" + Username}]}

    def admin_add_user_to_group(self, UserPoolId, Username, GroupName):
        self.users[Username]["groups"].add(GroupName)

    def admin_remove_user_from_group(self, UserPoolId, Username, GroupName):
        self.calls.append(("remove", Username, GroupName))
        self.users[Username]["groups"].discard(GroupName)

    def admin_list_groups_for_user(self, UserPoolId, Username, **kw):
        return {"Groups": [{"GroupName": g} for g in sorted(self.users[Username]["groups"])]}

    def list_users_in_group(self, UserPoolId, GroupName, **kw):
        return {"Users": [
            {"Username": u, "Enabled": info["enabled"], "UserStatus": info["status"],
             "Attributes": [{"Name": "email", "Value": u}]}
            for u, info in self.users.items() if GroupName in info["groups"]
        ]}

    def admin_user_global_sign_out(self, UserPoolId, Username):
        self.calls.append(("sign_out", Username, ""))

    def admin_delete_user(self, UserPoolId, Username):
        self.calls.append(("delete", Username, ""))
        del self.users[Username]


class HTTPException(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


def build(cog):
    audits = []
    ns = {
        "os": os, "logger": types.SimpleNamespace(warning=lambda *a, **k: None,
                                                  info=lambda *a, **k: None),
        "HTTPException": HTTPException, "Request": object, "UserRoleUpdate": object,
        "_cognito": lambda: cog,
        "token_revocation": types.SimpleNamespace(revoke_tokens=lambda *a, **k: None),
        "_end_current_sign_in": lambda *a, **k: None,
        "_ALLOWED_GROUPS": {"admins", "space-admins", "operators"},
        "is_admin": lambda auth: True, "get_user_id": lambda auth: "actor",
        "governance_registry": types.SimpleNamespace(
            audit=lambda *a, **k: audits.append(a), clear_invite_sent=lambda u: None),
    }
    sys.modules.setdefault("tools.aws.space_admins", types.SimpleNamespace(invalidate=lambda: None))
    exec(_const("_USABLE_ADMIN_STATES"), ns)  # noqa: S102
    exec(_const("_LAST_ADMIN_RECOVERY"), ns)  # noqa: S102
    for fn in ("_user_sub", "_list_user_group_names", "_other_usable_admins_exist",
               "_set_role", "update_user_role", "delete_cognito_user"):
        exec(_extract(fn), ns)  # noqa: S102
    return ns, audits


def req():
    return types.SimpleNamespace(headers={"Authorization": "Bearer x"})


os.environ["USER_POOL_ID"] = "pool-1"

print("== the only admin cannot demote itself ==")
cog = FakeCognito({"boss@x.com": ({"admins"}, "CONFIRMED", True)})
ns, audits = build(cog)
try:
    ns["update_user_role"]("boss@x.com", types.SimpleNamespace(role="operators"), req())
    check(False, "the self-demote should be refused")
except HTTPException as e:
    check(e.status_code == 409 and "only super admin" in e.detail.lower(),
          f"self-demote refused with a recovery message ({e.status_code})")
check("admins" in cog.users["boss@x.com"]["groups"], "the admin keeps the role")
check(not any(c[0] == "remove" for c in cog.calls), "nothing was removed")
check(not audits, "no role change was audited")

print("")
print("== the only admin cannot delete itself ==")
cog = FakeCognito({"boss@x.com": ({"admins"}, "CONFIRMED", True)})
ns, audits = build(cog)
try:
    ns["delete_cognito_user"]("boss@x.com", req())
    check(False, "the self-delete should be refused")
except HTTPException as e:
    check(e.status_code == 409, "self-delete refused (409)")
check("boss@x.com" in cog.users, "the admin still exists")
check(not any(c[0] == "delete" for c in cog.calls), "no delete was issued")

print("")
print("== a disabled or unconfirmed second admin does not count as a fallback ==")
cog = FakeCognito({"boss@x.com": ({"admins"}, "CONFIRMED", True),
                   "ghost@x.com": ({"admins"}, "CONFIRMED", False),      # disabled
                   "pending@x.com": ({"admins"}, "UNCONFIRMED", True)})  # not yet usable
ns, audits = build(cog)
try:
    ns["delete_cognito_user"]("boss@x.com", req())
    check(False, "a disabled/unconfirmed peer is not a usable fallback")
except HTTPException as e:
    check(e.status_code == 409, "still refused — no usable fallback admin")

print("")
print("== with a second usable admin, both actions are allowed ==")
cog = FakeCognito({"boss@x.com": ({"admins"}, "CONFIRMED", True),
                   "deputy@x.com": ({"admins"}, "CONFIRMED", True)})
ns, audits = build(cog)
res = ns["update_user_role"]("boss@x.com", types.SimpleNamespace(role="operators"), req())
check(res.get("status") == "updated", "the demote is allowed")
check(cog.users["boss@x.com"]["groups"] == {"operators"}, "the role actually changed")

cog = FakeCognito({"boss@x.com": ({"admins"}, "CONFIRMED", True),
                   "deputy@x.com": ({"admins"}, "CONFIRMED", True)})
ns, audits = build(cog)
res = ns["delete_cognito_user"]("boss@x.com", req())
check(res.get("status") == "deleted" and "boss@x.com" not in cog.users, "the delete is allowed")

print("")
print("== a non-admin user is never blocked by the guard ==")
cog = FakeCognito({"op@x.com": ({"operators"}, "CONFIRMED", True)})
ns, audits = build(cog)
res = ns["delete_cognito_user"]("op@x.com", req())
check(res.get("status") == "deleted", "deleting a lone operator is fine")

print("")
print("== a demote FROM admin TO admin is a no-op the guard allows (role unchanged) ==")
cog = FakeCognito({"boss@x.com": ({"admins"}, "CONFIRMED", True)})
ns, audits = build(cog)
res = ns["update_user_role"]("boss@x.com", types.SimpleNamespace(role="admins"), req())
check(res.get("status") == "updated" and cog.users["boss@x.com"]["groups"] == {"admins"},
      "keeping the only admin as admin is allowed")

# ── the console disables the controls on the last admin's row ──
print("")
print("== the admin console disables the controls on the last admin ==")
JSX = (pathlib.Path(__file__).resolve().parents[2]
       / "admin-console/src/components/UserManagement.jsx").read_text(encoding="utf-8")
check("isLastAdmin" in JSX and "usableAdminCount" in JSX,
      "the component computes whether a row is the last usable admin")
check("disabled={lastAdmin}" in JSX, "the role-change and delete controls disable on the last admin")
check(JSX.count("disabled={lastAdmin}") >= 2, "both the role select and the delete button are disabled")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
