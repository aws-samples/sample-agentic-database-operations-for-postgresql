# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Taking access away ends it now, not when the user's tokens expire.

Authorization reads cognito:groups from the bearer token, and an access or ID token lives
60 minutes whatever happens in Cognito. Global sign-out revokes refresh tokens only, so a
removed operator, a demoted admin or a deleted user kept using the API until expiry.

The fix: on role change, space-access removal, offboarding and deletion the backend writes
"tokens not before now" for the user (tools.aws.token_revocation), and the authentication
middleware refuses any token whose ``iat`` is earlier. The lookup is cached per user for at
most 30 seconds. Global sign-out stays on the changes that need a new sign-in.

Pinned here, hermetically (fake DynamoDB table, fake Cognito, a controllable clock):
  * a token issued before the change is refused, a fresh one is accepted, an unrelated
    user is untouched;
  * the cache: another task sees a revocation only after its cached answer expires, and
    never later than 30 seconds; the writing task sees it at once;
  * a store read failure keeps the last known value, and allows when there is none;
  * the middleware returns 401 for a refused token and passes a fresh one through;
  * role change, space removal, user deletion and operator offboarding each write the
    cutoff for the right sub; all but space removal also sign the user out; a failed
    write is reported, never success, and a delete does not go ahead without it;
  * space removal end to end, across two API tasks: the old token stops opening the
    space (at once on the writing task, within the 30 s cache on another), and after a
    token refresh the user's other spaces keep working while the removed one stays shut.

Run:  python3 backend/tests/test_token_not_before.py
"""
import asyncio
import base64
import json
import os
import pathlib
import re
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
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


class FakeTable:
    def __init__(self):
        self.items = {}
        self.fail_reads = False
        self.fail_writes = False
        self.reads = 0

    def put_item(self, Item):
        if self.fail_writes:
            raise RuntimeError("DynamoDB unavailable")
        self.items[Item["config_key"]] = dict(Item)

    def get_item(self, Key):
        self.reads += 1
        if self.fail_reads:
            raise RuntimeError("DynamoDB unavailable")
        item = self.items.get(Key["config_key"])
        return {"Item": dict(item)} if item else {}


TABLE = FakeTable()
_boto3 = types.ModuleType("boto3")
_boto3.resource = lambda *a, **k: types.SimpleNamespace(Table=lambda name: TABLE)
sys.modules["boto3"] = _boto3

from tools.aws import token_revocation as tr  # noqa: E402


class Clock:
    """Stands in for the module's `time`: wall clock (iat, cutoff) and monotonic (cache)."""
    def __init__(self):
        self.wall = 1_800_000_000.0
        self.mono = 1000.0

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


CLOCK = Clock()
tr.time = CLOCK


def reset():
    tr._cache.clear()
    TABLE.items.clear()
    TABLE.fail_reads = TABLE.fail_writes = False
    TABLE.reads = 0


print("== a token issued before the change is refused; a fresh one works ==")
reset()
before = int(CLOCK.wall) - 600          # signed in ten minutes ago
cutoff = tr.revoke_tokens("sub-ann", "removed from space s1")
check(cutoff == int(CLOCK.wall), "the cutoff is the moment of the change")
check(tr.token_revoked({"sub": "sub-ann", "iat": before}), "Ann's earlier token is refused")
check(not tr.token_revoked({"sub": "sub-ann", "iat": cutoff}), "a token issued at the change is accepted")
CLOCK.advance(5)
check(not tr.token_revoked({"sub": "sub-ann", "iat": int(CLOCK.wall)}), "a token issued after it is accepted")
check(not tr.token_revoked({"sub": "sub-bob", "iat": before}), "another user's token is untouched")
check(not tr.token_revoked({"sub": "sub-ann"}), "a token without iat is left to the other checks")
check(TABLE.items["tokens_not_before:sub-ann"]["reason"] == "removed from space s1", "the reason is stored")

print("")
print("== the lookup is cached per user for at most 30 seconds ==")
reset()
check(tr.CACHE_TTL_S <= 30, f"cache TTL is at most 30 s ({tr.CACHE_TTL_S})")
old = int(CLOCK.wall) - 60
check(not tr.token_revoked({"sub": "sub-cy", "iat": old}), "no cutoff yet: allowed (and cached)")
reads = TABLE.reads
# Another task writes the cutoff: simulate by writing the store directly, bypassing this
# task's cache, exactly what this task sees when the API task that served the change is
# a different one.
TABLE.items["tokens_not_before:sub-cy"] = {"config_key": "tokens_not_before:sub-cy",
                                            "not_before": int(CLOCK.wall)}
CLOCK.advance(10)
check(not tr.token_revoked({"sub": "sub-cy", "iat": old}), "within the TTL the cached answer is used")
check(TABLE.reads == reads, "no database read inside the TTL")
CLOCK.advance(21)                         # 31 s after the first read
check(tr.token_revoked({"sub": "sub-cy", "iat": old}), "after the TTL the cutoff is read and applied")
check(TABLE.reads == reads + 1, "exactly one read when the cache expires")
reset()
tr.revoke_tokens("sub-dee")
reads = TABLE.reads
check(tr.token_revoked({"sub": "sub-dee", "iat": int(CLOCK.wall) - 1}),
      "the task that wrote the cutoff applies it at once")
check(TABLE.reads == reads, "and without reading it back")

print("")
print("== a store read failure keeps the last known value, else allows ==")
reset()
tr.revoke_tokens("sub-eve")
CLOCK.advance(31)
TABLE.fail_reads = True
check(tr.token_revoked({"sub": "sub-eve", "iat": int(CLOCK.wall) - 100}),
      "read failure after expiry: the last known cutoff still refuses")
check(not tr.token_revoked({"sub": "sub-fay", "iat": int(CLOCK.wall) - 100}),
      "read failure with nothing known: allowed (the store must not lock everyone out)")


# ── The middleware ────────────────────────────────────────────────────────────
def _extract(name, is_async=False):
    kw = "async def" if is_async else "def"
    m = re.search(rf"^{kw} {re.escape(name)}\(.*?(?=^(?:@|def |async def |class |[A-Z_]+ = )|\Z)",
                  APP_SRC, re.S | re.M)
    assert m, f"{name} not found in app.py"
    return m.group(0)


def _jwt(claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"Bearer h.{body}.s"


class JSONResponse:
    def __init__(self, status_code, content):
        self.status_code, self.content = status_code, content


async def _threadpool(fn, *a):
    return fn(*a)


from utils.auth import decode_jwt_unverified  # noqa: E402
mw_ns = {"JSONResponse": JSONResponse, "Request": object, "jwt_rejection_reason": lambda a: None,
         "token_revocation": tr, "run_in_threadpool": _threadpool,
         "decode_jwt_unverified": decode_jwt_unverified}
exec(_extract("_enforce_jwt_signature", is_async=True), mw_ns)  # noqa: S102
middleware = mw_ns["_enforce_jwt_signature"]


def call(auth, method="GET"):
    req = types.SimpleNamespace(method=method, headers={"Authorization": auth} if auth else {})

    async def nxt(r):
        return "handled"
    return asyncio.run(middleware(req, nxt))


print("")
print("== the middleware refuses a token from before the change ==")
reset()
signed_in = int(CLOCK.wall) - 300
tr.revoke_tokens("sub-gus")
res = call(_jwt({"sub": "sub-gus", "iat": signed_in, "cognito:groups": ["admins"]}))
check(getattr(res, "status_code", None) == 401, "the earlier token gets 401")
check("Sign in again" in (getattr(res, "content", {}) or {}).get("detail", ""),
      "the 401 says what to do")
CLOCK.advance(2)
check(call(_jwt({"sub": "sub-gus", "iat": int(CLOCK.wall)})) == "handled", "a fresh token reaches the route")
check(call(_jwt({"sub": "sub-hal", "iat": signed_in})) == "handled", "other users are unaffected")
check(call("") == "handled", "no bearer: left to the route")
check(call(_jwt({"sub": "sub-gus", "iat": signed_in}), method="OPTIONS") == "handled",
      "a CORS preflight passes untouched")


# ── The endpoints that take access away ──────────────────────────────────────
class UserNotFound(Exception):
    pass


class FakeCognito:
    def __init__(self, users):
        self.users = {u: {"sub": s, "groups": set(g)} for u, (s, g) in users.items()}
        self.calls = []
        self.exceptions = types.SimpleNamespace(UserNotFoundException=UserNotFound)

    def admin_get_user(self, UserPoolId, Username):
        if Username not in self.users:
            raise UserNotFound(Username)
        return {"Username": Username,
                "UserAttributes": [{"Name": "email", "Value": Username},
                                   {"Name": "sub", "Value": self.users[Username]["sub"]}]}

    def admin_add_user_to_group(self, UserPoolId, Username, GroupName):
        self.calls.append(("add", Username, GroupName))
        self.users[Username]["groups"].add(GroupName)

    def admin_remove_user_from_group(self, UserPoolId, Username, GroupName):
        self.calls.append(("remove", Username, GroupName))
        self.users[Username]["groups"].discard(GroupName)

    def admin_list_groups_for_user(self, UserPoolId, Username, **kw):
        return {"Groups": [{"GroupName": g} for g in sorted(self.users[Username]["groups"])]}

    def list_users_in_group(self, UserPoolId, GroupName, **kw):
        # Every current member of the group, plus a standing second admin so the
        # last-admin guard never trips here (covered in test_last_admin_guard.py).
        users = [{"Username": u, "Enabled": True, "UserStatus": "CONFIRMED",
                  "Attributes": [{"Name": "email", "Value": u}]}
                 for u, info in self.users.items() if GroupName in info["groups"]]
        users.append({"Username": "standing-admin@example.com", "Enabled": True,
                      "UserStatus": "CONFIRMED",
                      "Attributes": [{"Name": "email", "Value": "standing-admin@example.com"}]})
        return {"Users": users}

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
                                                  error=lambda *a, **k: None, info=lambda *a, **k: None),
        "HTTPException": HTTPException, "Request": object, "UserRoleUpdate": object,
        "token_revocation": tr, "_cognito": lambda: cog,
        "boto3": types.SimpleNamespace(client=lambda *a, **k: cog),
        "_ALLOWED_GROUPS": {"admins", "space-admins", "operators"},
        "is_admin": lambda auth: True, "_can_manage_space": lambda auth, sid: True,
        "get_user_id": lambda auth: "actor-sub", "space_group_name": lambda sid: f"space-{sid}",
        "governance_registry": types.SimpleNamespace(
            audit=lambda *a, **k: audits.append(a), clear_invite_sent=lambda u: None),
    }
    sys.modules.setdefault("tools.aws.space_admins",
                           types.SimpleNamespace(invalidate=lambda: None))
    ns["_USABLE_ADMIN_STATES"] = {"CONFIRMED", "FORCE_CHANGE_PASSWORD"}
    ns["_LAST_ADMIN_RECOVERY"] = "last admin recovery"
    ns["_server_error"] = lambda e, action, status_code=500: HTTPException(status_code, action)
    ns["_redact_identifiers"] = lambda s: s
    for fn in ("_user_sub", "_end_current_sign_in", "_set_role", "_list_user_group_names",
               "_other_usable_admins_exist", "update_user_role", "delete_cognito_user",
               "delete_operator", "remove_space_user"):
        exec(_extract(fn), ns)  # noqa: S102
    return ns, audits


def req():
    return types.SimpleNamespace(headers={"Authorization": "Bearer x"})


os.environ["USER_POOL_ID"] = "pool-1"

print("")
print("== role change: old tokens refused, user signed out ==")
reset()
TOKEN_IAT = int(CLOCK.wall) - 120
cog = FakeCognito({"ann@example.com": ("sub-ann", {"admins"})})
ns, _ = build(cog)
CLOCK.advance(1)
res = ns["update_user_role"]("ann@example.com", types.SimpleNamespace(role="operators"), req())
check(res.get("status") == "updated", "the role change reports success")
check(tr.token_revoked({"sub": "sub-ann", "iat": TOKEN_IAT}), "Ann's admin token from before is refused")
check(("sign_out", "ann@example.com", "") in cog.calls, "Ann is signed out (refresh tokens revoked)")

print("")
print("== space removal: that access ends, no sign-out ==")
reset()
cog = FakeCognito({"op@example.com": ("sub-op", {"operators", "space-s1", "space-s2"})})
ns, _ = build(cog)
CLOCK.advance(1)
res = ns["remove_space_user"]("s1", "op@example.com", "USER", req())
check(res == {"status": "removed"}, "the removal reports success")
check("space-s1" not in cog.users["op@example.com"]["groups"], "the space group is removed")
check(tr.token_revoked({"sub": "sub-op", "iat": TOKEN_IAT}),
      "the operator's token naming the space is refused (the app refreshes to one without it)")
check(not any(c[0] == "sign_out" for c in cog.calls), "no global sign-out: other spaces keep working")
TABLE.fail_writes = True
try:
    ns["remove_space_user"]("s2", "op@example.com", "USER", req())
    check(False, "a failed cutoff write is reported, not success")
except HTTPException as e:
    check(e.status_code == 500 and "Remove them again" in e.detail,
          f"a failed cutoff write is reported, not success ({e.detail})")

print("")
print("== user deletion: cutoff and sign-out before the delete ==")
reset()
cog = FakeCognito({"gone@example.com": ("sub-gone", {"space-admins"})})
ns, _ = build(cog)
CLOCK.advance(1)
res = ns["delete_cognito_user"]("gone@example.com", req())
check(res.get("status") == "deleted", "the delete reports success")
check(tr.token_revoked({"sub": "sub-gone", "iat": TOKEN_IAT}), "the deleted user's token is refused")
order = [c[0] for c in cog.calls]
check(order == ["sign_out", "delete"], f"signed out, then deleted ({order})")
reset()
cog = FakeCognito({"keep@example.com": ("sub-keep", {"space-admins"})})
ns, _ = build(cog)
TABLE.fail_writes = True
try:
    ns["delete_cognito_user"]("keep@example.com", req())
    check(False, "without the cutoff the user is not deleted")
except HTTPException as e:
    check(e.status_code == 500 and "keep@example.com" in cog.users,
          f"without the cutoff the user is not deleted ({e.detail})")
try:
    ns["delete_cognito_user"]("nobody@example.com", req())
    check(False, "an unknown user is 404")
except HTTPException as e:
    check(e.status_code == 404, "an unknown user is 404")

print("")
print("== operator offboarding: cutoff and sign-out before the delete ==")
reset()
cog = FakeCognito({"leaver@example.com": ("sub-leaver", {"operators", "space-s1"})})
ns, audits = build(cog)
CLOCK.advance(1)
res = ns["delete_operator"]("leaver@example.com", req())
check(res.get("status") == "deleted" and res.get("spaces_revoked") == 1, "the offboarding reports success")
check(tr.token_revoked({"sub": "sub-leaver", "iat": TOKEN_IAT}), "the leaver's token is refused")
check([c[0] for c in cog.calls] == ["sign_out", "delete"], "signed out, then deleted")

print("")
print("== space removal, end to end: the old token stops opening that space; the others work ==")
# Kept as designed: removal from one space writes the cutoff but does not sign the user
# out. Two API tasks, each with its own cache: task A serves the removal, task B had
# already cached "no cutoff" for this user. A request is the middleware followed by the
# real space gate (utils.auth.is_space_member) on the token's groups.
from utils.auth import is_space_member  # noqa: E402
import utils.auth as _auth  # noqa: E402
_auth.decode_jwt = lambda tok: decode_jwt_unverified(tok)   # claims only; signatures are not under test here


def opens(task_cache, token, space_id):
    tr._cache = task_cache
    req = types.SimpleNamespace(method="GET", headers={"Authorization": token})

    async def route(r):
        return "200" if is_space_member(r.headers["Authorization"], space_id) else "403"
    res = asyncio.run(middleware(req, route))
    return res if isinstance(res, str) else str(getattr(res, "status_code", res))


reset()
TASK_A, TASK_B = {}, {}
cog = FakeCognito({"op@example.com": ("sub-op", {"operators", "space-s1", "space-s2"})})
ns, _ = build(cog)
old_token = _jwt({"sub": "sub-op", "iat": int(CLOCK.wall) - 300,
                  "cognito:groups": ["operators", "space-s1", "space-s2"]})
check(opens(TASK_A, old_token, "s1") == "200" and opens(TASK_B, old_token, "s1") == "200",
      "before the removal the token opens s1 on both tasks")
CLOCK.advance(1)
tr._cache = TASK_A
check(ns["remove_space_user"]("s1", "op@example.com", "USER", req()) == {"status": "removed"},
      "the operator is removed from s1")
check(not any(c[0] == "sign_out" for c in cog.calls), "and not signed out globally")
check(opens(TASK_A, old_token, "s1") == "401", "task A refuses the old token at once")
CLOCK.advance(10)
check(opens(TASK_B, old_token, "s1") == "200",
      "task B may still accept it while its cached answer lasts (at most 30 s)")
CLOCK.advance(21)                         # 31 s after task B's cached read
check(opens(TASK_B, old_token, "s1") == "401", "after the cache expires task B refuses it too")
check(opens(TASK_B, old_token, "s2") == "401",
      "the old token is refused everywhere, so the app refreshes it")
# The app's retry-once on a 401 gets a fresh token, issued now, from the groups the user
# still holds in Cognito.
fresh_groups = sorted(cog.users["op@example.com"]["groups"])
fresh_token = _jwt({"sub": "sub-op", "iat": int(CLOCK.wall), "cognito:groups": fresh_groups})
check("space-s1" not in fresh_groups and "space-s2" in fresh_groups, f"the fresh token's groups ({fresh_groups})")
for name, cache in (("A", TASK_A), ("B", TASK_B)):
    check(opens(cache, fresh_token, "s2") == "200", f"task {name}: the fresh token opens s2, the user's other space")
    check(opens(cache, fresh_token, "s1") == "403", f"task {name}: the fresh token does not open s1")
tr._cache = {}

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
