# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Credentials and access roles are keyed on the admin who OWNS a space.

The rule: all of one admin's spaces share one access role per account and one Secrets
Manager namespace (``<prefix>/<owner admin>/``); different admins' spaces share nothing,
and IAM enforces that line. This test runs the pieces that carry the rule, with fakes
where AWS would be:

* ``session_factory.external_id_for_space`` presents the space's owner admin, never the
  space id and never anything a caller supplied;
* ``list_space_candidates`` prefills a secret ARN only from a space with the SAME owner
  admin, computed from ownership and not from what the caller manages;
* ``_can_manage_space`` admits a space's creator and a super admin and nobody else:
  a space admin who is only a member of another admin's space group is refused the
  candidate list (so never sees a suggested secret ARN) and onboarding.

Run:  python3 backend/tests/test_owner_admin_model.py
"""
import os
import pathlib
import re
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
for _m in ("psycopg2", "psycopg2.extras"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

BACKEND = pathlib.Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
APP_SRC = (BACKEND / "app.py").read_text(encoding="utf-8")
ADMIN_A, ADMIN_B = "aaaaaaaa-0000-0000-0000-00000000000a", "bbbbbbbb-0000-0000-0000-00000000000b"
S1, S2, T1 = "s1-space", "s2-space", "t1-space"
SPACES = {
    S1: {"space_id": S1, "name": "S1", "owner_admin": ADMIN_A, "created_by": ADMIN_A},
    S2: {"space_id": S2, "name": "S2", "owner_admin": ADMIN_A, "created_by": ADMIN_A},
    T1: {"space_id": T1, "name": "T1", "owner_admin": ADMIN_B, "created_by": ADMIN_B},
}

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
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^(?:@|def |class )|\Z)", APP_SRC, re.S | re.M)
    assert m, name
    return re.sub(r"^@app\.[^\n]*\n", "", m.group(0), flags=re.M)


# ── 1. the ExternalId is the owner admin, from the space row ───────────────────────
print("== sts:ExternalId is the id of the admin who owns the space ==")
from tools.aws import session_factory as sf  # noqa: E402

sf._space_cache.clear()
sf._lookup_space = lambda sid: SPACES.get(sid, {})
check(sf.external_id_for_space(S1) == ADMIN_A, "S1 presents admin A")
check(sf.external_id_for_space(S2) == ADMIN_A, "S2 presents admin A too: one role answers to both")
check(sf.external_id_for_space(T1) == ADMIN_B, "T1 presents admin B")
check(sf.external_id_for_space("nope") == "", "an unknown space presents nothing, so the assume cannot happen")
check(sf.external_id_for_space(S1) != S1, "the space id itself is never the ExternalId")
src = (BACKEND / "tools" / "aws" / "session_factory.py").read_text(encoding="utf-8")
check("ExternalId=external_id_for_space(space_id)" in src, "the agent's assume presents it")
disc = (BACKEND / "tools" / "aws" / "discovery_client.py").read_text(encoding="utf-8")
check("external_id = external_id_for_space(space_id)" in disc and "ExternalId=external_id" in disc,
      "the discovery/onboarding assume presents it")
t1 = (BACKEND / "tier1_collector.py").read_text(encoding="utf-8")
check('record.get("reach_owner_admin"' in t1, "the Tier 1 collector presents the reach provider's owner admin")
rs = (REPO / "batch" / "run_scan.py").read_text(encoding="utf-8")
check('instance_info.get("reach_owner_admin"' in rs, "the Tier 2 scan presents the reach provider's owner admin")

# ── 2. the candidate prefill follows ownership, not management ─────────────────────
print("")
print("== Add to this space prefills a secret only from a space of the same owner admin ==")


class FakeHTTPException(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


DB_X = {"instance_id": "db-x", "display_name": "DB X", "status": "configured",
        "member_space_ids": {S1}, "candidate_space_ids": {S2, T1},
        "space_secrets": {S1: "arn:aws:secretsmanager:us-east-1:7656:secret:dbops/" + ADMIN_A + "/postgres/dbx/readonly-abc"}}
ns = {
    "HTTPException": FakeHTTPException, "Request": object,
    "db_registry": types.SimpleNamespace(list_instances=lambda: [DB_X]),
    "spaces_registry": types.SimpleNamespace(get_space=lambda sid: SPACES.get(sid), list_spaces=lambda: list(SPACES.values())),
    "owner_admin_of": lambda sp: (sp or {}).get("owner_admin") or (sp or {}).get("created_by") or "",
    "_instance_candidate_spaces": lambda inst: set(inst.get("candidate_space_ids") or []),
    "_instance_member_spaces": lambda inst: set(inst.get("member_space_ids") or []),
    # The caller manages EVERY space here, to prove management does not drive the prefill.
    "_can_manage_space": lambda auth, sid: True,
    "teardown_stalled": __import__("tools.aws.teardown_claim", fromlist=["x"]).teardown_stalled,
    "datetime": __import__("datetime").datetime, "timezone": __import__("datetime").timezone,
    "CONFIGURE_STALE_AFTER_S": 900,
    "_CONFIGURE_STALE_MESSAGE": "Configuration did not finish — the service may have restarted. Onboard it again.",
}
exec(_extract("_unsupported_reason"), ns)  # noqa: S102 - pinning shipped source
exec(_extract("_configure_is_stale"), ns)  # noqa: S102 - pinning shipped source
exec(_extract("_flag_stale_configuring"), ns)  # noqa: S102 - pinning shipped source
exec(_extract("list_space_candidates"), ns)  # noqa: S102 - pinning shipped source
req = types.SimpleNamespace(headers={"Authorization": "Bearer x"})
for_s2 = ns["list_space_candidates"](S2, req)
for_t1 = ns["list_space_candidates"](T1, req)
check(len(for_s2) == 1 and for_s2[0]["suggested_secret_arn"] == DB_X["space_secrets"][S1],
      "S2 (admin A) is offered S1's ARN")
check(for_s2[0]["suggested_secret_from"] == "S1", "and told which space it comes from")
check(len(for_t1) == 1 and for_t1[0]["suggested_secret_arn"] == "" and for_t1[0]["suggested_secret_from"] == "",
      "T1 (admin B) is offered nothing, although the caller manages S1 too")
check(ADMIN_A not in str(for_t1), "admin B's response carries no trace of admin A's namespace")
check("can_share_from" not in str(for_s2) and "granted" not in str(for_s2), "no sharing or grant fields remain")

# ── 3. a space is managed by its creator or a super admin, nobody else ──────────────
print("")
print("== a space is managed by its creator or a super admin; group membership grants use only ==")
ADMIN_C, ADMIN_D, ADMIN_S = ("cccccccc-0000-0000-0000-00000000000c", "dddddddd-0000-0000-0000-00000000000d",
                             "5555eeee-0000-0000-0000-00000000000e")
# Each caller: (sub, cognito:groups). Group membership of T1 (admin B's space) is held by a
# space admin who did not create it and by an operator; neither may manage it.
CALLERS = {
    "creator-a": (ADMIN_A, ["space-admins", f"space-{S1}", f"space-{S2}"]),
    "creator-b": (ADMIN_B, ["space-admins", f"space-{T1}"]),
    "super": (ADMIN_S, ["admins"]),
    "member-sa": (ADMIN_C, ["space-admins", f"space-{T1}"]),
    "operator": ("op-sub", ["operators", f"space-{T1}"]),
    "nonmember-sa": (ADMIN_D, ["space-admins"]),
    "former-admin-a": (ADMIN_A, [f"space-{S1}"]),
}


def _groups(auth):
    return CALLERS.get(auth, ("", []))[1]


GATE_NS = {
    "is_admin": lambda auth: "admins" in _groups(auth),
    "is_space_admin": lambda auth: "space-admins" in _groups(auth),
    "get_user_groups": _groups,
    "get_user_id": lambda auth: CALLERS.get(auth, ("unknown", []))[0] or "unknown",
    "space_group_name": lambda sid: f"space-{sid}",
    "spaces_registry": types.SimpleNamespace(get_space=lambda sid: SPACES.get(sid),
                                             list_spaces=lambda: list(SPACES.values())),
}
ns2 = dict(GATE_NS)
exec(_extract("_created_by_caller"), ns2)  # noqa: S102
exec(_extract("_can_manage_space"), ns2)  # noqa: S102
can = ns2["_can_manage_space"]
check(can("creator-a", S1) is True and can("creator-a", S2) is True, "the creator manages their spaces")
check(can("creator-b", T1) is True, "admin B manages the space B created")
check(can("super", T1) is True and can("super", S1) is True,
      "a super admin manages another admin's space (break-glass)")
check(can("member-sa", T1) is False, "a space admin in T1's group who did not create it does NOT manage it")
check(can("operator", T1) is False, "an operator in T1's group does not manage it")
check(can("nonmember-sa", T1) is False, "a space admin outside the group does not manage it")
check(can("creator-a", T1) is False, "a creator manages only their own spaces")
check(can("former-admin-a", S1) is False, "a creator whose admin role was removed manages nothing")
check(can("super", "") is False and can("creator-a", "") is False, "no space, no management")
body = _extract("_can_manage_space")
check("return _created_by_caller(auth_header, space_id) or is_admin(auth_header)" in body,
      "the gate is exactly creator-or-super-admin")
check("space_group_name" not in body and "get_user_groups" not in body,
      "group membership plays no part in management")
auth_src = (BACKEND / "utils" / "auth.py").read_text(encoding="utf-8")
member = auth_src.split("def is_space_member(", 1)[1].split("\ndef ", 1)[0]
check('"admins" in groups' in member, "viewing every space stays with the super admin")

# ── 4. a co-member space admin cannot onboard or see the owner's secret ───────────────
print("")
print("== a space admin in another admin's space group gets 403 from candidates and onboarding ==")
# T2 is admin B's second space: DB Y is a member of T1 and a candidate of T2, so T2's
# managers are offered T1's secret. The co-member space admin is in both groups.
T2 = "t2-space"
SPACES[T2] = {"space_id": T2, "name": "T2", "owner_admin": ADMIN_B, "created_by": ADMIN_B}
DB_Y = {"instance_id": "db-y", "display_name": "DB Y", "status": "configured",
        "member_space_ids": {T1}, "candidate_space_ids": {T2},
        "space_secrets": {T1: "arn:aws:secretsmanager:us-east-1:7656:secret:dbops/" + ADMIN_B + "/postgres/dby/ro-abc"}}
onboard_calls = []


class Reached(Exception):
    """Raised by the fake onboarding gate: the caller got past _can_manage_space."""


def _fake_onboardable(instance_id, space_id, auth=""):
    onboard_calls.append((instance_id, space_id, auth))
    raise Reached()


from typing import Optional  # noqa: E402

ns3 = dict(GATE_NS)
ns3.update({
    "HTTPException": FakeHTTPException, "Request": object, "BackgroundTasks": object,
    "Optional": Optional, "ConfigureDatabaseRequest": object,
    "db_registry": types.SimpleNamespace(list_instances=lambda: [DB_Y]),
    "owner_admin_of": lambda sp: (sp or {}).get("owner_admin") or (sp or {}).get("created_by") or "",
    "_instance_candidate_spaces": lambda inst: set(inst.get("candidate_space_ids") or []),
    "_instance_member_spaces": lambda inst: set(inst.get("member_space_ids") or []),
    "_verify_instance_onboardable": _fake_onboardable,
    "teardown_stalled": __import__("tools.aws.teardown_claim", fromlist=["x"]).teardown_stalled,
    "datetime": __import__("datetime").datetime, "timezone": __import__("datetime").timezone,
    "CONFIGURE_STALE_AFTER_S": 900,
    "_CONFIGURE_STALE_MESSAGE": "Configuration did not finish — the service may have restarted. Onboard it again.",
})
for fn in ("_created_by_caller", "_can_manage_space", "_configure_is_stale", "_flag_stale_configuring",
           "list_space_candidates",
           "configure_space_database", "test_database_credentials", "_unsupported_reason",
           "_allowed_secret_hosts", "_secret_host_refusal", "_replica_tags"):
    exec(_extract(fn), ns3)  # noqa: S102


def _status(fn, *args):
    try:
        return fn(*args)
    except FakeHTTPException as e:
        return e.status_code
    except Reached:
        return "reached"


def _req(auth):
    return types.SimpleNamespace(headers={"Authorization": auth})


CALLERS["member-sa"] = (ADMIN_C, ["space-admins", f"space-{T1}", f"space-{T2}"])
cands = _status(ns3["list_space_candidates"], T2, _req("member-sa"))
check(cands == 403, "list_space_candidates: 403 for a space admin who is only a member of T2")
check(ADMIN_B not in str(cands), "so admin B's secret ARN is never suggested to them")
owner_view = _status(ns3["list_space_candidates"], T2, _req("creator-b"))
check(isinstance(owner_view, list) and owner_view[0]["suggested_secret_arn"] == DB_Y["space_secrets"][T1],
      "the owner of T2 is offered the sibling space's ARN (control)")
super_view = _status(ns3["list_space_candidates"], T2, _req("super"))
check(isinstance(super_view, list) and len(super_view) == 1, "a super admin passes the gate too (break-glass)")

body = types.SimpleNamespace(secret_arn=DB_Y["space_secrets"][T1])
check(_status(ns3["configure_space_database"], T2, "db-y", _req("member-sa"), None, body) == 403,
      "configure_space_database: the co-member space admin is refused onboarding")
check(_status(ns3["test_database_credentials"], T2, "db-y", body, _req("member-sa")) == 403,
      "test_database_credentials: and refused the pre-flight test")
check(onboard_calls == [], "neither request got as far as the instance lookup")
check(_status(ns3["configure_space_database"], T2, "db-y", _req("creator-b"), None, body) == "reached",
      "the owner gets past the gate (control)")
check(_status(ns3["configure_space_database"], T2, "db-y", _req("super"), None, body) == "reached",
      "and so does a super admin")

# ── 5. the space-admin cascade tier is the owner's alone ─────────────────────────────
print("")
print("== the space-admin model/knowledge tier of a space is its owner's, not a co-member's ==")
from tools.aws import space_admins as sa  # noqa: E402

GROUPS = {  # group -> [(username, sub)]
    "space-admins": [("b@x", ADMIN_B), ("c@x", ADMIN_C)],
    f"space-{T1}": [("b@x", ADMIN_B), ("c@x", ADMIN_C), ("op@x", "op-sub")],
}
listed = []


class FakePaginator:
    def paginate(self, UserPoolId, GroupName):
        listed.append(GroupName)
        yield {"Users": [{"Username": u, "Attributes": [{"Name": "sub", "Value": s}]}
                         for u, s in GROUPS.get(GroupName, [])]}


sa._cognito = lambda: types.SimpleNamespace(get_paginator=lambda name: FakePaginator())
os.environ["USER_POOL_ID"] = "pool-1"
sa.invalidate()
check(sa.managing_admin_subs(T1, ADMIN_B) == frozenset({ADMIN_B}),
      "T1's tier is admin B's (the creator, holding space-admins)")
check(ADMIN_C not in sa.managing_admin_subs(T1, ADMIN_B),
      "a space admin who is only in T1's group adds no tier")
check(listed == ["space-admins"], "only the role group is listed; the space group plays no part")
sa.invalidate()
check(sa.managing_admin_subs(T1, ADMIN_A) == frozenset(), "a creator without the space-admins role adds no tier")
sa.invalidate()
check(sa.managing_admin_subs(T1, "") == frozenset(), "no creator, no tier")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
