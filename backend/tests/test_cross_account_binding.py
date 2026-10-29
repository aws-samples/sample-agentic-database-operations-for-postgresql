# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Cross-account reach fails closed, and binding changes are audited.

A space admin binds a space to secondary AWS accounts. Two properties matter:

  * When the agent needs a database in a secondary account, it assumes the space's role
    there (carrying the space's sts:ExternalId). If that assume is DENIED or FAILS, or no
    role is configured, ``session_factory.get_session`` must RAISE — not fall back to a
    deployment-account session, which silently ran the subsequent AWS calls against the
    wrong (deployment) account and returned empty/wrong-account data with no signal.
  * Every change to a space's account binding (PUT /api/spaces/{id} and configure-access)
    is written to the governance audit trail.

Same-account resolution (the deployment account) must still return a normal session.

Hermetic: boto3 stubbed at import; no AWS.

Run:  python3 backend/tests/test_cross_account_binding.py   (or via pytest)
"""
import os
import pathlib
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REPO = pathlib.Path(__file__).resolve().parents[2]
SPACE_A = "aaaaaaaa-space-a"

# One cross-account database row, held by SPACE_A, living in account 222222222222.
ROWS = [
    {"instance_id": "uuid-f", "display_name": "finance-db", "account_id": "222222222222",
     "region": "us-east-1", "secret_arn": "arn:secret:f",
     "member_space_ids": [SPACE_A], "tags": {"space_id": SPACE_A}},
]


class FakeTable:
    def __init__(self, name):
        self.name = name

    def get_item(self, Key=None, **kw):
        iid = (Key or {}).get("instance_id")
        return next(({"Item": r} for r in ROWS if r["instance_id"] == iid), {})

    def scan(self, **kw):
        names = kw.get("ExpressionAttributeNames", {})
        vals = kw.get("ExpressionAttributeValues", {})
        field = next(iter(names.values()), "")
        want = vals.get(":val")
        return {"Items": [r for r in ROWS if r.get(field) == want]}


class FakeResource:
    def Table(self, name):
        return FakeTable(name)


class FakeSession:
    def __init__(self, **kw):
        self.kw = kw


_fake_boto3 = types.ModuleType("boto3")
_fake_boto3.resource = lambda *a, **k: FakeResource()
_fake_boto3.client = lambda *a, **k: None
_fake_boto3.Session = FakeSession
sys.modules["boto3"] = _fake_boto3

from tools.aws import session_factory as sf  # noqa: E402
import model_resolver as mr  # noqa: E402


def _ctx(space=SPACE_A):
    mr.CURRENT_USER_ID.set("u1")
    mr.CURRENT_SPACE_ID.set(space)
    sf._db_record_cache.clear()
    sf._space_cache.clear()


class _RaisingSTS:
    def assume_role(self, **kw):
        raise Exception("AccessDenied: not authorized to perform sts:AssumeRole")


def _read(rel):
    return (REPO / rel).read_text(encoding="utf-8")


# ── session_factory fails closed on a cross-account reach failure ─────────────

def test_denied_assume_raises_not_falls_back():
    _ctx()
    sf._get_agent_account_id = lambda: "999999999999"   # different from the DB's account
    sf._lookup_role_name = lambda *a, **k: "dbops-agent-role"
    sf._get_sts = lambda: _RaisingSTS()
    try:
        sf.get_session(identifier="finance-db")
    except PermissionError:
        return
    raise AssertionError("a denied cross-account assume must raise, not return a session")


def test_missing_role_raises_not_falls_back():
    _ctx()
    sf._get_agent_account_id = lambda: "999999999999"
    sf._lookup_role_name = lambda *a, **k: ""            # no reachable role configured
    try:
        sf.get_session(identifier="finance-db")
    except PermissionError:
        return
    raise AssertionError("a cross-account DB with no configured role must raise")


def test_same_account_still_returns_a_session():
    _ctx()
    sf._get_agent_account_id = lambda: "222222222222"    # SAME as the DB's account
    session = sf.get_session(identifier="finance-db")
    assert isinstance(session, FakeSession), "same-account resolution must not raise"


def test_no_deployment_account_fallback_remains_in_the_cross_account_path():
    # Source guard: the two cross-account failure branches must raise, not return a Session.
    src = _read("backend/tools/aws/session_factory.py")
    # isolate get_session
    body = src.split("def get_session(", 1)[1].split("\ndef ", 1)[0]
    cross = body.split("Cross-account", 1)[1]  # after the same-account early return
    assert "raise PermissionError" in cross
    assert "return boto3.Session" not in cross, \
        "the cross-account path must not fall back to a deployment-account session"


# ── binding changes are audited ───────────────────────────────────────────────

def test_update_space_audits_binding_changes():
    src = _read("backend/app.py")
    assert '"space_binding_update"' in src
    # it snapshots the prior space and diffs the binding fields
    assert "before = spaces_registry.get_space(space_id)" in src
    for f in ("primary_account_id", "secondary_account_ids", "secondary_role_names"):
        assert f in src


def test_configure_access_audits_the_binding():
    src = _read("backend/app.py")
    assert '"space_access_configure"' in src
    # audited on both the incomplete and configured persist branches
    assert src.count('"space_access_configure"') >= 2


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", fn.__name__, "-", e or "assertion failed")
    print("\n%d passed, %d failed" % (len(fns) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
