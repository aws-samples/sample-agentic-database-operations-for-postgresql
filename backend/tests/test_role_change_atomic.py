# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A role change never leaves a user with no role while reporting success.

`_set_role` used to log and ignore a failed AdminAddUserToGroup, then remove every other
base-role group, and the endpoint answered {"status": "updated"}. One transient Cognito
error turned an admin into a user with no role at all, while the console said the new role
was applied.

Now the new role is added first (a failure stops before anything is removed), membership
is confirmed, and only then are the other base roles the user holds removed. A failed
removal puts the previous state back and is reported. Pinned through the real functions
(extracted from backend/app.py) with a fake Cognito that fails on demand:
  * add fails: error, the old role kept, nothing removed, sessions untouched, no audit;
  * a removal fails: the new role is taken back off, the old role kept, error reported;
  * a removal fails and so does the restore: the error says to check and set it again;
  * the add "succeeds" but Cognito does not show the group: nothing removed, error;
  * success: exactly the new role, then the user's sessions are ended.

Run:  python3 backend/tests/test_role_change_atomic.py
"""
import os
import pathlib
import re
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP_SRC = (ROOT / "backend/app.py").read_text(encoding="utf-8")
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


class UserNotFound(Exception):
    pass


class FakeCognito:
    """Cognito groups for one user; `fail` maps (op, group) to the number of calls that fail."""
    def __init__(self, groups, fail=None, ghost_add=False):
        self.groups = set(groups)
        self.fail = dict(fail or {})
        self.ghost_add = ghost_add
        self.calls = []
        self.exceptions = types.SimpleNamespace(UserNotFoundException=UserNotFound)

    def _maybe_fail(self, op, group):
        if self.fail.get((op, group), 0) > 0:
            self.fail[(op, group)] -= 1
            raise RuntimeError(f"TooManyRequestsException on {op} {group}")

    def admin_get_user(self, UserPoolId, Username):
        return {"Username": Username, "UserAttributes": [{"Name": "sub", "Value": "sub-ann"}]}

    def admin_add_user_to_group(self, UserPoolId, Username, GroupName):
        self.calls.append(("add", GroupName))
        self._maybe_fail("add", GroupName)
        if not self.ghost_add:
            self.groups.add(GroupName)

    def admin_remove_user_from_group(self, UserPoolId, Username, GroupName):
        self.calls.append(("remove", GroupName))
        self._maybe_fail("remove", GroupName)
        self.groups.discard(GroupName)

    def admin_list_groups_for_user(self, UserPoolId, Username, **kw):
        return {"Groups": [{"GroupName": g} for g in sorted(self.groups)]}

    def list_users_in_group(self, UserPoolId, GroupName, **kw):
        # A second confirmed super admin always exists here, so the last-admin guard
        # (tested separately in test_last_admin_guard.py) never trips in this suite.
        return {"Users": [
            {"Username": "other-admin@example.com", "Enabled": True, "UserStatus": "CONFIRMED",
             "Attributes": [{"Name": "email", "Value": "other-admin@example.com"}]},
        ]}

    def admin_user_global_sign_out(self, UserPoolId, Username):
        self.calls.append(("sign_out", ""))


class HTTPException(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


def build(cog):
    revoked, audits = [], []
    ns = {
        "os": os, "logger": types.SimpleNamespace(warning=lambda *a, **k: None),
        "HTTPException": HTTPException, "Request": object, "UserRoleUpdate": object,
        "_cognito": lambda: cog,
        "token_revocation": types.SimpleNamespace(revoke_tokens=lambda sub, reason="": revoked.append(sub)),
        "_ALLOWED_GROUPS": {"admins", "space-admins", "operators"},
        "is_admin": lambda auth: True, "get_user_id": lambda auth: "actor",
        "governance_registry": types.SimpleNamespace(audit=lambda *a, **k: audits.append(a)),
    }
    sys.modules.setdefault("tools.aws.space_admins", types.SimpleNamespace(invalidate=lambda: None))
    ns["_USABLE_ADMIN_STATES"] = {"CONFIRMED", "FORCE_CHANGE_PASSWORD"}
    ns["_LAST_ADMIN_RECOVERY"] = "last admin recovery"
    ns["_server_error"] = lambda e, action, status_code=500: HTTPException(status_code, action)
    ns["_redact_identifiers"] = lambda s: s
    for fn in ("_user_sub", "_end_current_sign_in", "_list_user_group_names",
               "_other_usable_admins_exist", "_set_role", "update_user_role"):
        exec(_extract(fn), ns)  # noqa: S102
    return ns, revoked, audits


def change(cog, role):
    ns, revoked, audits = build(cog)
    req = types.SimpleNamespace(headers={"Authorization": "Bearer x"})
    try:
        return ns["update_user_role"]("ann@example.com", types.SimpleNamespace(role=role), req), revoked, audits
    except HTTPException as e:
        return e, revoked, audits


os.environ["USER_POOL_ID"] = "pool-1"

print("== the add fails: nothing changes and the failure is reported ==")
cog = FakeCognito({"admins", "space-abc"}, fail={("add", "operators"): 1})
res, revoked, audits = change(cog, "operators")
check(isinstance(res, HTTPException) and res.status_code == 500, "the endpoint answers 500, not updated")
check("not changed" in getattr(res, "detail", "") and "unchanged" in getattr(res, "detail", ""),
      f"the message says the role is unchanged ({getattr(res, 'detail', '')})")
check(cog.groups == {"admins", "space-abc"}, f"the user keeps admins ({cog.groups})")
check(not any(op == "remove" for op, _ in cog.calls), "nothing was removed")
check(not revoked and not audits, "no sessions ended, no change audited")

print("")
print("== a removal fails: the previous role is restored ==")
cog = FakeCognito({"admins", "space-abc"}, fail={("remove", "admins"): 1})
res, revoked, audits = change(cog, "space-admins")
check(isinstance(res, HTTPException) and "previous role is kept" in res.detail,
      f"reported as not changed ({getattr(res, 'detail', '')})")
check(cog.groups == {"admins", "space-abc"}, f"exactly the old role again ({cog.groups})")
check(not revoked and not audits, "no sessions ended, no change audited")

print("")
print("== a removal fails and the restore fails too: told to check ==")
cog = FakeCognito({"admins"}, fail={("remove", "admins"): 1, ("remove", "operators"): 1})
res, revoked, _ = change(cog, "operators")
check(isinstance(res, HTTPException) and "Check their groups" in res.detail,
      f"the message says to check and set the role again ({getattr(res, 'detail', '')})")
check("admins" in cog.groups, "the user is never left with no role")

print("")
print("== Cognito does not show the new group after the add: nothing removed ==")
cog = FakeCognito({"admins"}, ghost_add=True)
res, revoked, _ = change(cog, "operators")
check(isinstance(res, HTTPException) and "does not show them" in res.detail, "reported as not changed")
check(cog.groups == {"admins"} and not any(op == "remove" for op, _ in cog.calls), "admins kept")

print("")
print("== success: exactly the new role, then the sessions end ==")
cog = FakeCognito({"admins", "space-abc"})
res, revoked, audits = change(cog, "operators")
check(res.get("status") == "updated" and res.get("role") == "operators", "updated to operators")
check(cog.groups == {"operators", "space-abc"}, f"exactly one base role, space access untouched ({cog.groups})")
ops = [op for op, _ in cog.calls]
check(ops.index("add") < ops.index("remove"), f"the new role was added before the old was removed ({ops})")
check(revoked == ["sub-ann"] and ops[-1] == "sign_out", "then the user's current sign-in was ended")
check(len(audits) == 1, "the change is audited once")
cog = FakeCognito({"operators"})
res, _, _ = change(cog, "operators")
check(res.get("status") == "updated" and cog.groups == {"operators"}
      and not any(op == "remove" for op, _ in cog.calls), "setting the same role removes nothing")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
