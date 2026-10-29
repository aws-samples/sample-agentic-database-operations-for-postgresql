# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Removing a database from a space is an UNTAG, never an implicit global teardown.

A space is a visibility boundary tagged onto a physical database. Removing the database
from one space must therefore:

  * drop only that space from ``member_space_ids`` (and its verified secret), leaving the
    record, its network path, its schedules and its shared history in place while any other
    space still includes it — for EVERY role, super admins included (a super admin acting
    inside one space must not tear the database out of every other space as a side effect);
  * hand reach to a surviving member when the departed space was the reach provider,
    preferring a survivor that verified its OWN secret and making that secret active in the
    same write, so a departed provider's secret is never left active under another space's role;
  * refuse a `space_id` the database is not a member of (opaque 404), and refuse
    `decommission` (the explicit "remove everywhere") to anyone but a super admin;
  * tear the physical database down only when the LAST member leaves, or on an explicit
    super-admin decommission.

The two functions are extracted from backend/app.py by source so the test pins the code that
ships, without importing the whole application. Hermetic — no AWS.

Run:  python3 backend/tests/test_space_aware_delete.py   (or via pytest)
"""
import contextlib
import contextvars
import os
import pathlib
import re
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import datetime, timedelta, timezone  # noqa: E402
from tools.aws.teardown_claim import teardown_stalled, TEARDOWN_STALE_S  # noqa: E402
APP_SRC = (pathlib.Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")

SPACE_A, SPACE_B, SPACE_C = "space-a", "space-b", "space-c"


def _extract(name: str) -> str:
    """Source of a top-level `def name(` (up to the next top-level def/decorator/class)."""
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^(?:@|def |class )|\Z)", APP_SRC, re.S | re.M)
    assert m, f"{name} not found in app.py"
    return m.group(0)


class FakeHTTPException(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


class FakeLastMemberError(Exception):
    def __init__(self, iid, sid):
        super().__init__(f"{sid} is the last member of {iid}")
        self.instance_id, self.space_id = iid, sid


class FakeRegistry:
    """Minimal stand-in for db_registry that records what the handler does."""

    def __init__(self, rows):
        self.rows = {r["instance_id"]: dict(r) for r in rows}
        self.calls = []

    def get_instance(self, iid):
        return self.rows.get(iid)

    def list_instances(self, *a, **k):
        return list(self.rows.values())

    def remove_member(self, iid, sid, keep_one=False):
        """Mirrors the real conditional DELETE: with keep_one the set must hold MORE than
        this space and contain it, evaluated against the CURRENT row (not a value the
        handler read earlier), else LastMemberError."""
        self.calls.append(("remove_member", iid, sid, keep_one))
        m = set(self.rows[iid].get("member_space_ids") or [])
        if keep_one and (sid not in m or len(m) <= 1):
            raise FakeLastMemberError(iid, sid)
        m.discard(sid)
        self.rows[iid]["member_space_ids"] = m

    def add_candidate(self, iid, sid):
        self.calls.append(("add_candidate", iid, sid))
        c = set(self.rows[iid].get("candidate_space_ids") or []); c.add(sid)
        self.rows[iid]["candidate_space_ids"] = c
    def remove_space_secret(self, iid, sid):
        self.calls.append(("remove_space_secret", iid, sid))
        (self.rows[iid].get("space_secrets") or {}).pop(sid, None)

    def set_reach_space(self, iid, sid, secret_arn="", owner_admin=""):
        self.calls.append(("set_reach_space", iid, sid, secret_arn))
        self.rows[iid]["reach_space_id"] = sid
        if secret_arn:
            self.rows[iid]["secret_arn"] = secret_arn

    def delete_instance(self, iid):
        self.calls.append(("delete_instance", iid))
        self.rows.pop(iid, None)
    def update_instance(self, iid, patch):
        self.calls.append(("update_instance", iid, dict(patch)))
        self.rows[iid].update(patch)

    def claim_teardown(self, iid):
        """Mirrors the real conditional write: refused when the record is gone or is
        already deleting under a live claim; a stalled claim is taken over."""
        self.calls.append(("claim_teardown", iid))
        row = self.rows.get(iid)
        if not row or (row.get("status") == "deleting" and not teardown_stalled(row)):
            return False
        row["status"] = "deleting"
        row["teardown_claimed_at"] = _now_iso()
        return True

    def touch_teardown(self, iid):
        self.calls.append(("touch_teardown", iid))
        row = self.rows.get(iid)
        if row and row.get("status") == "deleting":
            row["teardown_claimed_at"] = _now_iso()


def _now_iso(ago_s=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=ago_s)).isoformat()


class FakeBackgroundTasks:
    """FastAPI's BackgroundTasks: tasks run only when the test says the response is sent."""
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *a, **k):
        self.tasks.append((fn, a, k))

    def run(self):
        while self.tasks:
            fn, a, k = self.tasks.pop(0)
            fn(*a, **k)


class FakeSpaces:
    def __init__(self, creator=""):
        self.updates = []
        self.creator = creator
        self.state = {}      # space_id -> {"status", "teardown_claimed_at"} written so far
        self.touched = []

    def update_space(self, sid, patch):
        self.updates.append((sid, patch))
        if "status" in patch:
            self.state.setdefault(sid, {})["status"] = patch["status"]

    def get_space(self, sid):
        return {"space_id": sid, "primary_account_id": "111111111111", "owner_admin": "admin-1",
                "created_by": self.creator, **self.state.get(sid, {})}

    def claim_teardown(self, sid):
        """Mirrors the real conditional write (see SpacesRegistry.claim_teardown)."""
        sp = self.get_space(sid)
        if sp.get("status") == "deleting" and not teardown_stalled(sp):
            return False
        self.state[sid] = {"status": "deleting", "teardown_claimed_at": _now_iso()}
        return True

    def touch_teardown(self, sid):
        self.touched.append(sid)
        if self.state.get(sid, {}).get("status") == "deleting":
            self.state[sid]["teardown_claimed_at"] = _now_iso()
    def list_spaces(self):
        return [self.get_space(s) for s in (SPACE_A, SPACE_B, SPACE_C)]


class FakeAudit:
    def __init__(self):
        self.events = []
        self.kwargs = []

    def audit(self, *a, **k):
        self.events.append(a)
        self.kwargs.append(k)

    def of(self, action):
        """[(positional, kwargs)] of every event with this action."""
        return [(a, k) for a, k in zip(self.events, self.kwargs) if a[1] == action]


def _build(rows, *, admin=False, manageable=(), real_gate=False, creator=""):
    """Assemble a namespace with the real helper + handler and controllable auth fakes.

    `real_gate=True` runs the shipped _created_by_caller / _can_manage_space /
    _can_manage_instance instead of the fakes: the caller is a super admin when `admin`,
    else a space admin, and `creator` is the `created_by` of every space."""
    reg, spaces, gov = FakeRegistry(rows), FakeSpaces(creator), FakeAudit()
    ns = {
        "db_registry": reg, "spaces_registry": spaces, "governance_registry": gov,
        "HTTPException": FakeHTTPException, "LastMemberError": FakeLastMemberError,
        "logger": types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None,
                                        error=lambda *a, **k: None),
        "os": os, "Request": object,
        "is_admin": lambda auth: admin,
        "get_user_id": lambda auth: "user-1",
        "_can_manage_space": lambda auth, sid: admin or sid in manageable,
        "_can_manage_instance": lambda auth, iid: True,
        "_instance_member_spaces": lambda inst: set((inst or {}).get("member_space_ids") or []),
        "_instance_in_space": lambda inst, sid: sid in set((inst or {}).get("member_space_ids") or []),
        "owner_admin_of": lambda sp: (sp or {}).get("owner_admin") or (sp or {}).get("created_by") or "",
        # The physical-teardown tail of the handler touches network/scheduler clients; these
        # fakes let it run to completion so the test can assert delete_instance was reached.
        "network_configurator": types.SimpleNamespace(
            cleanup_lattice=lambda **k: {"steps": ["lt"]}, cleanup_peering=lambda **k: {"steps": ["peer"]}),
        "db_discovery": types.SimpleNamespace(_get_cross_account_session=lambda *a, **k: None),
        "_get_scheduler_client": lambda: (_ for _ in ()).throw(RuntimeError("no scheduler")),
        "_schedule_name": lambda iid: f"t2-{iid}", "_tier1_schedule_name": lambda iid: f"t1-{iid}",
        "_deleted_schedules": [],
        "_deleted_alarms": [],
        "BackgroundTasks": object,
        "time": types.SimpleNamespace(sleep=lambda s: None, monotonic=lambda: 0.0),
        "teardown_stalled": teardown_stalled, "TEARDOWN_HEARTBEAT_S": 60,
        "wait_heartbeat": lambda beat: contextlib.nullcontext(),
        "audit_log": types.SimpleNamespace(detached_context=contextvars.copy_context),
        "SPACE_TEARDOWN_WAIT_S": 600, "SPACE_TEARDOWN_POLL_S": 5,
        "_notified": [],
    }
    ns["_notify_teardown_failed"] = lambda label, iid, steps: ns["_notified"].append(iid)
    ns["_delete_db_schedules"] = lambda iid: ns["_deleted_schedules"].append(iid)
    ns["_delete_db_alarms"] = lambda iid: ns["_deleted_alarms"].append(iid)
    # Strip the FastAPI decorator so the handler is a plain function.
    handler_src = re.sub(r"^@app\.delete\([^\n]*\)\n", "", _extract("delete_database"), flags=re.M)
    exec(_extract("_refresh_space_db_count"), ns)  # noqa: S102 - pinning shipped source
    exec(_extract("_reassign_reach_if_needed"), ns)  # noqa: S102
    exec(_extract("_untag_space_from_database"), ns)  # noqa: S102
    for fn in ("_teardown_heartbeat", "_teardown_database", "_background_teardown", "_begin_teardown",
               "_background_delete_space"):
        exec(_extract(fn), ns)  # noqa: S102
    if real_gate:
        ns["is_space_admin"] = lambda auth: not admin
        for fn in ("_created_by_caller", "_can_manage_space", "_can_manage_instance"):
            exec(_extract(fn), ns)  # noqa: S102
    exec(handler_src, ns)  # noqa: S102
    return ns, reg, spaces, gov


def _req():
    return types.SimpleNamespace(headers={"Authorization": "Bearer x"})


def _del(ns, iid, req, **kw):
    """Call delete_database, then run its background tasks (the response has gone)."""
    bg = FakeBackgroundTasks()
    out = ns["delete_database"](iid, req, bg, **kw)
    bg.run()
    return out


def _del_space(ns, sid, req):
    bg = FakeBackgroundTasks()
    out = ns["delete_space"](sid, req, bg)
    bg.run()
    return out


def _shared_row(**over):
    row = {"instance_id": "db-x", "status": "configured", "network_method": "peering",
           "member_space_ids": {SPACE_A, SPACE_B}, "reach_space_id": SPACE_A,
           "secret_arn": "arn:secret:A", "space_secrets": {SPACE_A: "arn:secret:A", SPACE_B: "arn:secret:B"},
           "vpc_id": "vpc-1", "region": "us-east-1", "account_id": "111111111111"}
    row.update(over)
    return row


# ── untag semantics ──────────────────────────────────────────────────────────

def test_space_admin_removing_shared_db_only_untags_their_space():
    ns, reg, spaces, gov = _build([_shared_row()], manageable={SPACE_B})
    out = _del(ns, "db-x", _req(), space_id=SPACE_B)
    assert out["message"] == "Removed from space" and out["space_id"] == SPACE_B
    assert out["remaining_members"] == 1
    assert "db-x" in reg.rows, "physical record must survive while another space includes it"
    assert reg.rows["db-x"]["member_space_ids"] == {SPACE_A}
    assert SPACE_B not in reg.rows["db-x"]["space_secrets"]
    assert not any(c[0] == "delete_instance" for c in reg.calls)
    assert any(e[1] == "database_space_remove" for e in gov.events)


def test_super_admin_removing_shared_db_also_only_untags():
    # The pre-fix behaviour tore the DB out of EVERY space when a super admin deleted it.
    ns, reg, *_ = _build([_shared_row()], admin=True)
    out = _del(ns, "db-x", _req(), space_id=SPACE_B)
    assert out["message"] == "Removed from space"
    assert "db-x" in reg.rows and reg.rows["db-x"]["member_space_ids"] == {SPACE_A}
    assert not any(c[0] == "delete_instance" for c in reg.calls)


def test_legacy_caller_without_space_id_still_never_tears_down_shared_db():
    ns, reg, *_ = _build([_shared_row()], admin=True)
    out = _del(ns, "db-x", _req())  # no space_id (old client)
    assert out["message"] == "Removed from space"
    assert "db-x" in reg.rows


def test_space_id_not_a_member_is_an_opaque_404():
    ns, *_ = _build([_shared_row()], admin=True)
    try:
        _del(ns, "db-x", _req(), space_id=SPACE_C)
    except FakeHTTPException as e:
        assert e.status_code == 404
    else:
        raise AssertionError("expected 404 for a space that does not include the DB")


def test_space_admin_cannot_act_in_a_space_they_do_not_manage():
    ns, *_ = _build([_shared_row()], manageable={SPACE_B})
    try:
        _del(ns, "db-x", _req(), space_id=SPACE_A)  # manages B, asks to act in A
    except FakeHTTPException as e:
        assert e.status_code == 403
    else:
        raise AssertionError("expected 403")


# ── reach handoff ────────────────────────────────────────────────────────────

def test_removing_reach_provider_hands_reach_to_survivor_and_swaps_secret():
    ns, reg, *_ = _build([_shared_row()], admin=True)
    _del(ns, "db-x", _req(), space_id=SPACE_A)  # A was the reach provider
    row = reg.rows["db-x"]
    assert row["reach_space_id"] == SPACE_B
    assert row["secret_arn"] == "arn:secret:B", "active secret must switch to the survivor's verified secret"
    assert ("set_reach_space", "db-x", SPACE_B, "arn:secret:B") in reg.calls


def test_removing_non_provider_leaves_reach_and_secret_alone():
    ns, reg, *_ = _build([_shared_row()], admin=True)
    _del(ns, "db-x", _req(), space_id=SPACE_B)  # B is not the provider
    row = reg.rows["db-x"]
    assert row["reach_space_id"] == SPACE_A and row["secret_arn"] == "arn:secret:A"
    assert not any(c[0] == "set_reach_space" for c in reg.calls)


def test_handoff_prefers_a_survivor_with_a_verified_secret():
    # Three members; the alphabetically-first survivor (B) has NO verified secret, C does.
    row = _shared_row(member_space_ids={SPACE_A, SPACE_B, SPACE_C},
                      space_secrets={SPACE_A: "arn:secret:A", SPACE_C: "arn:secret:C"})
    ns, reg, *_ = _build([row], admin=True)
    _del(ns, "db-x", _req(), space_id=SPACE_A)
    assert reg.rows["db-x"]["reach_space_id"] == SPACE_C
    assert reg.rows["db-x"]["secret_arn"] == "arn:secret:C"


def test_handoff_with_no_verified_survivor_pauses_instead_of_reusing_the_departed_secret():
    row = _shared_row(space_secrets={SPACE_A: "arn:secret:A"})  # B never recorded a secret
    ns, reg, *_ = _build([row], admin=True)
    _del(ns, "db-x", _req(), space_id=SPACE_A)
    r = reg.rows["db-x"]
    assert r["reach_space_id"] == SPACE_B
    assert r["secret_arn"] == "", "A's secret must not keep running under B's role"
    assert r["status"] == "config_failed" and "onboard it again" in r["config_error"]
    assert "db-x" in ns["_deleted_schedules"], "scans are paused until a member proves a secret"


# ── teardown only on last member / explicit decommission ─────────────────────

def test_last_member_leaving_tears_the_physical_db_down():
    row = _shared_row(member_space_ids={SPACE_A}, space_secrets={SPACE_A: "arn:secret:A"})
    ns, reg, *_ = _build([row], manageable={SPACE_A})
    out = _del(ns, "db-x", _req(), space_id=SPACE_A)
    assert out["message"] == "Deleting" and out["status"] == "deleting"
    assert "db-x" not in reg.rows and any(c[0] == "delete_instance" for c in reg.calls)
    ev = ns["governance_registry"].of("database_space_remove")
    assert len(ev) == 1, "the last-member teardown is audited as the space removal it is"
    (a, k), = ev
    assert a[3] == f"space:{SPACE_A}" and a[4] == {"member_space_ids": [SPACE_A]}
    assert a[5]["member_space_ids"] == [] and a[5]["teardown"] is True
    assert "space_ids" not in k, "a space removal never fans out to the remaining members"
    (a, k), = ns["governance_registry"].of("database_teardown_complete")
    assert a[5]["trigger"] == "database_space_remove" and a[5]["cleanup"] == ["peer"]
    assert k["space_ids"] == [SPACE_A]
    assert ns["_deleted_alarms"] == ["db-x"], "the torn-down database's alarms go with it"


def test_last_member_teardown_without_an_acting_space_is_account_scoped():
    # A legacy caller with no space_id who manages no member space by name still reaches
    # the teardown through _can_manage_instance; the event then has no space to live in.
    row = _shared_row(member_space_ids={SPACE_A}, space_secrets={SPACE_A: "arn:secret:A"})
    ns, reg, spaces, gov = _build([row])
    out = _del(ns, "db-x", _req())
    assert out["message"] == "Deleting"
    (a, _k), = gov.of("database_space_remove")
    assert a[3] == "account" and a[5]["teardown"] is True


def test_decommission_requires_super_admin():
    ns, reg, *_ = _build([_shared_row()], manageable={SPACE_A, SPACE_B})
    try:
        _del(ns, "db-x", _req(), decommission=True)
    except FakeHTTPException as e:
        assert e.status_code == 403
    else:
        raise AssertionError("expected 403 for a non-super-admin decommission")
    assert "db-x" in reg.rows


def test_super_admin_decommission_tears_down_a_shared_db_and_audits():
    ns, reg, spaces, gov = _build([_shared_row()], admin=True)
    out = _del(ns, "db-x", _req(), decommission=True)
    assert out["message"] == "Deleting" and "db-x" not in reg.rows
    assert any(e[1] == "database_decommission" for e in gov.events)
    # every former member's count is refreshed
    assert {s for s, _ in spaces.updates} >= {SPACE_A, SPACE_B}


def test_super_admin_with_no_seat_decommissions_a_memberless_record():
    # Never onboarded: no member, so _can_manage_instance refuses everyone. The
    # decommission branch is checked before that gate, so a super admin still reaches it.
    row = _shared_row(member_space_ids=set(), space_secrets={}, reach_space_id="", secret_arn="")
    ns, reg, spaces, gov = _build([row], admin=True, real_gate=True)
    assert ns["_can_manage_instance"]("Bearer x", "db-x") is False, "precondition: nobody manages it"
    out = _del(ns, "db-x", _req(), decommission=True)
    assert out["message"] == "Deleting" and "db-x" not in reg.rows
    assert ("delete_instance", "db-x") in reg.calls, "teardown ran"
    (a, _k), = gov.of("database_teardown_complete")
    assert a[5]["trigger"] == "database_decommission" and "peer" in a[5]["cleanup"]
    ev = [e for e in gov.events if e[1] == "database_decommission"]
    assert len(ev) == 1 and ev[0][2] == "db-x" and ev[0][4] == {"member_space_ids": []}, "audited"


def test_super_admin_with_no_seat_decommissions_a_shared_record():
    # The super admin created neither space and is in neither group.
    ns, reg, spaces, gov = _build([_shared_row()], admin=True, real_gate=True, creator="someone-else")
    out = _del(ns, "db-x", _req(), decommission=True)
    assert out["message"] == "Deleting" and "db-x" not in reg.rows
    assert ("delete_instance", "db-x") in reg.calls, "teardown ran"
    (a, _k), = gov.of("database_teardown_complete")
    assert a[5]["trigger"] == "database_decommission" and "peer" in a[5]["cleanup"]
    ev = [e for e in gov.events if e[1] == "database_decommission"]
    assert len(ev) == 1 and ev[0][4] == {"member_space_ids": [SPACE_A, SPACE_B]}, "audited with the members"
    assert gov.of("database_decommission")[0][1]["space_ids"] == [SPACE_A, SPACE_B], \
        "fanned out to every former member space's tab"
    assert not gov.of("database_space_remove"), "a decommission is not also a space removal"
    assert {s for s, _ in spaces.updates} >= {SPACE_A, SPACE_B}


def test_space_admin_owner_asking_to_decommission_gets_403():
    # The caller created both member spaces and so manages the database, yet only a
    # super admin may remove it everywhere.
    ns, reg, spaces, gov = _build([_shared_row()], real_gate=True, creator="user-1")
    assert ns["_can_manage_instance"]("Bearer x", "db-x") is True, "precondition: they manage it"
    try:
        _del(ns, "db-x", _req(), decommission=True)
    except FakeHTTPException as e:
        assert e.status_code == 403 and e.detail == "Only a super admin can decommission a database everywhere."
    else:
        raise AssertionError("expected 403")
    assert "db-x" in reg.rows and not gov.events and not reg.calls, "nothing changed, nothing audited"


def test_decommission_is_checked_before_the_manage_gate():
    body = _extract("delete_database")
    assert body.index("if decommission:") < body.index("_can_manage_instance(auth, instance_id)"), \
        "the super-admin decommission must not depend on holding a seat in a member space"


# ── the last-member decision is made at write time, not from an earlier read ───────
def test_two_spaces_leaving_at_once_cannot_both_untag():
    # Both handlers read a two-member set. The first untag passes; the second one's
    # conditional DELETE fails (the set now holds one space) and it tears down instead.
    row = _shared_row()
    ns, reg, spaces, gov = _build([row], admin=True)
    first = _del(ns, "db-x", _req(), space_id=SPACE_A)
    assert first["message"] == "Removed from space" and first["remaining_members"] == 1
    # Simulate the second request having read the same stale two-member set: the handler
    # re-reads, but the FAKE condition is what decides, exactly as DynamoDB would.
    reg.rows["db-x"]["member_space_ids"] = {SPACE_B}
    second = _del(ns, "db-x", _req(), space_id=SPACE_B)
    assert second["message"] == "Deleting", "the second leaver takes the teardown path"
    assert ("remove_member", "db-x", SPACE_B, True) in reg.calls, "it asked for a conditional removal"
    assert ("delete_instance", "db-x") in reg.calls and "db-x" not in reg.rows
    assert not any(c[0] == "remove_space_secret" and c[2] == SPACE_B for c in reg.calls), \
        "no half-untag happens once the condition refused"


def test_a_retried_remove_from_space_is_a_no_op_not_a_teardown():
    # Two requests for the SAME space (a double click, or two admins of that space): both
    # read the record with the space present; the first removes it; the second's
    # conditional delete fails because the space is already gone. That failure must not
    # be read as "last member": the database still has another member and must survive.
    row = _shared_row(member_space_ids={SPACE_A, SPACE_B, SPACE_C},
                      space_secrets={SPACE_A: "arn:secret:A", SPACE_B: "arn:secret:B", SPACE_C: "arn:secret:C"})
    ns, reg, spaces, gov = _build([row], admin=True)
    first = _del(ns, "db-x", _req(), space_id=SPACE_A)
    assert first["message"] == "Removed from space" and first["remaining_members"] == 2
    # The retry: make the handler's own membership read see the stale set (it is what the
    # second request read before the first wrote), while the fake table holds the truth.
    truth = set(reg.rows["db-x"]["member_space_ids"])
    stale = {SPACE_A, SPACE_B, SPACE_C}
    reads = iter([stale])
    real_get = reg.get_instance
    def staggered_get(iid):
        r = dict(real_get(iid))
        try:
            r["member_space_ids"] = set(next(reads))
        except StopIteration:
            pass
        return r
    reg.get_instance = staggered_get
    calls_before = len(reg.calls)
    second = _del(ns, "db-x", _req(), space_id=SPACE_A)
    reg.get_instance = real_get
    assert second["message"] == "Removed from space", "the retry answers like the first, it does not fail"
    assert "db-x" in reg.rows and set(reg.rows["db-x"]["member_space_ids"]) == truth == {SPACE_B, SPACE_C}, \
        "the database survives for the other spaces"
    new_calls = reg.calls[calls_before:]
    assert ("remove_member", "db-x", SPACE_A, True) in new_calls, "the retry reached the conditional delete (which refused)"
    assert ("delete_instance", "db-x") not in new_calls, "NOTHING is torn down"
    assert not any(c[0] in ("remove_space_secret", "revoke_grant", "set_reach_space") for c in new_calls), \
        "and no half-untag runs on the retry"
    assert len([e for e in gov.events if e[1] == "database_space_remove"]) == 1, "one removal, one audit row"


def test_the_genuine_last_member_still_tears_down_after_the_re_read():
    row = _shared_row(member_space_ids={SPACE_A}, space_secrets={SPACE_A: "arn:secret:A"})
    ns, reg, *_ = _build([row], admin=True)
    out = _del(ns, "db-x", _req(), space_id=SPACE_A)
    assert out["message"] == "Deleting" and ("delete_instance", "db-x") in reg.calls


def test_untag_source_uses_the_conditional_removal_and_handles_the_refusal():
    src = _extract("_untag_space_from_database")
    assert "remove_member(instance_id, space_id, keep_one=True)" in src
    assert "except LastMemberError" in src and "return set(), True" in src
    handler = src.split("except LastMemberError")[1].split("db_registry.remove_space_secret")[0]
    assert "get_instance(instance_id)" in handler, "a failed condition is followed by a re-read, never by a guess"
    assert "if space_id not in now:" in handler and "return now, False" in handler, \
        "a space that is already gone is a no-op, not a last member"
    assert "if now == {space_id}:" in handler, "teardown only when this space is the one remaining member"
    for fn in ("delete_database", "delete_space"):
        body = _extract(fn)
        assert "_untag_space_from_database(" in body and ("_begin_teardown(" in body or "claim_teardown(" in body), \
            f"{fn} goes through the shared untag/teardown helpers"
        assert "remove_member(" not in body, f"{fn} does not call remove_member directly"


def test_delete_space_untags_fully_and_tears_down_the_last_one():
    # Space A owns db-x (shared with B) and db-y alone, and was granted db-z by B.
    rows = [
        _shared_row(instance_id="db-x"),
        _shared_row(instance_id="db-y", member_space_ids={SPACE_A}, space_secrets={SPACE_A: "arn:secret:A"}),
        _shared_row(instance_id="db-z", member_space_ids={SPACE_A, SPACE_B}, reach_space_id=SPACE_B,
                    secret_arn="arn:secret:B", space_secrets={SPACE_A: "arn:secret:B", SPACE_B: "arn:secret:B"}),
    ]
    ns, reg, spaces, gov = _build(rows, admin=True)
    ns["spaces_registry"].deleted = []
    ns["spaces_registry"].delete_space = lambda sid: ns["spaces_registry"].deleted.append(sid)
    ns["iam_client"] = types.SimpleNamespace(delete_role=lambda n: {"status": "deleted"})
    ns["_role_deletable_for_space"] = lambda n, sid: True
    exec(re.sub(r"^@app\.delete\([^\n]*\)\n", "", _extract("delete_space"), flags=re.M), ns)  # noqa: S102
    out = _del_space(ns, SPACE_A, _req())
    assert out["message"] == "Deleting" and "Databases: detached 2, deleting 1" in " ".join(out["cleanup"])
    x, z = reg.rows["db-x"], reg.rows["db-z"]
    assert x["member_space_ids"] == {SPACE_B} and SPACE_A not in x["space_secrets"], \
        "the deleted space's membership AND its secret are gone"
    assert x["reach_space_id"] == SPACE_B and x["secret_arn"] == "arn:secret:B", "reach handed over"
    assert z["member_space_ids"] == {SPACE_B} and SPACE_A not in z["space_secrets"], "its entry is gone, B's stays"
    assert "db-y" not in reg.rows and ("delete_instance", "db-y") in reg.calls, "the lone database is torn down"
    assert ns["network_configurator"] is not None  # teardown ran through _teardown_database
    assert spaces.state[SPACE_A]["status"] == "deleting" and spaces.deleted == [SPACE_A], \
        "the space row is claimed (deleting) and kept until its databases are gone, then deleted"
    assert any(c == ("remove_member", "db-y", SPACE_A, True) for c in reg.calls)
    (a, k), = gov.of("space_delete")
    assert a[2] == SPACE_A and a[3] == f"space:{SPACE_A}"
    assert a[5]["result"] == "teardown_started"
    assert a[5]["databases_detached"] == 2 and a[5]["databases_deleted"] == 1
    assert k["owner_admin"] == "admin-1" and "space_name" in k and k["target_label"]
    (a, k), = gov.of("space_delete_teardown_complete")
    assert a[5]["result"] == "completed" and a[5]["databases_deleted"] == 1 and k["owner_admin"] == "admin-1"


def _delete_space_ns(rows=(), **over):
    ns, reg, spaces, gov = _build(list(rows), admin=True)
    ns["spaces_registry"].delete_space = lambda sid: None
    ns["iam_client"] = types.SimpleNamespace(delete_role=lambda n: {"status": "deleted"})
    ns.update(over)
    exec(_extract("delete_space"), ns)  # noqa: S102
    return ns, reg, spaces, gov


def test_delete_space_with_a_cleanup_error_is_completed_with_errors():
    ns, reg, spaces, gov = _delete_space_ns([_shared_row()])
    reg.list_instances = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("throttled"))
    out = _del_space(ns, SPACE_A, _req())
    assert any(e.startswith("Database cleanup:") for e in out["cleanup"])
    (a, _k), = gov.of("space_delete")
    assert a[5]["result"] == "completed_with_errors"
    assert a[5]["databases_detached"] == 0 and a[5]["databases_deleted"] == 0, \
        "the counters are defined on every path"


def test_delete_space_that_raises_is_audited_as_failed_and_re_raised():
    class Boom(Exception):
        pass

    ns, reg, spaces, gov = _delete_space_ns([_shared_row()])
    ns["spaces_registry"].delete_space = lambda sid: (_ for _ in ()).throw(Boom("ddb down"))
    try:
        _del_space(ns, SPACE_A, _req())
    except Boom:
        pass
    else:
        raise AssertionError("the exception must be re-raised unchanged")
    (a, k), = gov.of("space_delete")
    assert a[5]["result"] == "failed: Boom"
    assert a[5]["databases_detached"] == 1 and isinstance(a[5]["cleanup_log"], list)
    assert k["owner_admin"] == "admin-1", "name and owner were captured before the cleanup"


def test_delete_space_source_writes_the_event_in_finally():
    body = _extract("delete_space")
    head = body.split("try:", 1)[0]
    for init in ("detached = deleted = 0", "cleanup_log = []", 'outcome = "failed: unknown"',
                 "owner = owner_admin_of(", 'name = (space or {}).get("name"'):
        assert init in head, f"{init!r} must be set before the outer try"
    fin = body.split("finally:", 1)[1]
    assert '"space_delete"' in fin and "space_name=name" in fin and "owner_admin=owner" in fin \
        and "target_label=" in fin
    assert "raise\n" in body.split("except Exception as e:\n        outcome", 1)[1].split("finally:", 1)[0]


# ── a stranded "deleting" claim can be taken over; the space row outlives its teardowns ──
STALE = TEARDOWN_STALE_S * 2


def _lone_row(**over):
    return _shared_row(member_space_ids={SPACE_A}, space_secrets={SPACE_A: "arn:secret:A"}, **over)


def test_a_live_deleting_claim_is_a_no_op():
    ns, reg, spaces, gov = _build([_lone_row(status="deleting", teardown_claimed_at=_now_iso(30))],
                                  manageable={SPACE_A})
    bg = FakeBackgroundTasks()
    out = ns["delete_database"]("db-x", _req(), bg, space_id=SPACE_A)
    assert out == {"message": "Deleting", "status": "deleting", "cleanup": []} and not bg.tasks
    assert "db-x" in reg.rows and not gov.events, "nothing scheduled, nothing audited"


def test_a_stalled_deleting_claim_is_taken_over_by_the_retry():
    # The process running the teardown stopped (deploy, task replacement, crash): the
    # claim is older than TEARDOWN_STALE_S with no heartbeat. Deleting again finishes it.
    ns, reg, spaces, gov = _build([_lone_row(status="deleting", teardown_claimed_at=_now_iso(STALE))],
                                  manageable={SPACE_A})
    assert teardown_stalled(reg.rows["db-x"]), "precondition: the claim is stalled"
    out = _del(ns, "db-x", _req(), space_id=SPACE_A)
    assert out["status"] == "deleting" and "db-x" not in reg.rows, "the retry ran the teardown to the end"
    assert ("claim_teardown", "db-x") in reg.calls
    assert len(gov.of("database_teardown_complete")) == 1


def test_a_deleting_record_without_a_claim_time_is_taken_over():
    # No teardown_claimed_at means no heartbeat: the claim is stalled whatever updated_at says.
    ns, reg, *_ = _build([_lone_row(status="deleting", updated_at=_now_iso(30))], manageable={SPACE_A})
    _del(ns, "db-x", _req(), space_id=SPACE_A)
    assert "db-x" not in reg.rows, "updated_at is not a heartbeat"


def test_the_running_teardown_beats_its_claims():
    ns, reg, spaces, gov = _build([_lone_row()], admin=True)
    reg.rows["db-x"]["status"] = "deleting"
    spaces.state[SPACE_A] = {"status": "deleting", "teardown_claimed_at": _now_iso(600)}
    ns["_background_teardown"]("db-x", dict(reg.rows["db-x"]), "space_delete", "user-1", [SPACE_A],
                               beat_space=SPACE_A)
    assert ("touch_teardown", "db-x") in reg.calls and spaces.touched == [SPACE_A], \
        "the database and the space claims are refreshed when the teardown starts"
    beats = []
    ns["db_registry"].touch_teardown = lambda iid: beats.append(iid)
    clock = {"t": 0.0}
    ns["time"] = types.SimpleNamespace(sleep=lambda s: None, monotonic=lambda: clock["t"])
    beat = ns["_teardown_heartbeat"]("db-x")
    beat(); beat()
    clock["t"] = 59.0; beat()
    clock["t"] = 61.0; beat()
    beat(force=True)
    assert beats == ["db-x", "db-x", "db-x"], "at most once per TEARDOWN_HEARTBEAT_S unless forced"
    body = _extract("_background_teardown")
    assert "with wait_heartbeat(beat):" in body and "_teardown_database(" in body.split("with wait_heartbeat(beat):")[1], \
        "the Lattice waits inside the teardown beat too"


def _space_ns(rows):
    ns, reg, spaces, gov = _build(list(rows), admin=True)
    spaces.deleted = []
    spaces.delete_space = lambda sid: spaces.deleted.append(sid)
    ns["iam_client"] = types.SimpleNamespace(delete_role=lambda n: {"status": "deleted"})
    exec(_extract("delete_space"), ns)  # noqa: S102
    return ns, reg, spaces, gov


def test_a_second_delete_of_a_space_is_refused_by_the_conditional_claim():
    ns, reg, spaces, gov = _space_ns([_lone_row()])
    spaces.state[SPACE_A] = {"status": "deleting", "teardown_claimed_at": _now_iso(10)}
    bg = FakeBackgroundTasks()
    out = ns["delete_space"](SPACE_A, _req(), bg)
    assert out["status"] == "deleting" and not bg.tasks and not spaces.deleted and not gov.events
    assert reg.rows["db-x"]["member_space_ids"] == {SPACE_A}, "nothing untagged or claimed"


def test_a_stalled_space_deletion_is_taken_over():
    ns, reg, spaces, gov = _space_ns([_lone_row(status="deleting", teardown_claimed_at=_now_iso(STALE))])
    spaces.state[SPACE_A] = {"status": "deleting", "teardown_claimed_at": _now_iso(STALE)}
    out = _del_space(ns, SPACE_A, _req())
    assert out["status"] == "deleting" and "db-x" not in reg.rows and spaces.deleted == [SPACE_A], \
        "the retry claims the space and the stalled database, tears it down, deletes the row"
    (a, _k), = gov.of("space_delete_teardown_complete")


def test_the_space_row_stays_while_another_request_tears_its_database_down():
    # Another request (a delete-database, or the first click of a double click) already
    # holds a live claim on the space's last database. The space row must outlive that
    # teardown: it builds the cross-account session from the row.
    ns, reg, spaces, gov = _space_ns([_lone_row(status="deleting", teardown_claimed_at=_now_iso(5))])
    seen = []

    def other_teardown_finishes(_s):
        seen.append(spaces.get_space(SPACE_A).get("status"))
        reg.rows.pop("db-x", None)
    ns["time"] = types.SimpleNamespace(sleep=other_teardown_finishes, monotonic=lambda: 0.0)
    bg = FakeBackgroundTasks()
    out = ns["delete_space"](SPACE_A, _req(), bg)
    assert out["status"] == "deleting" and bg.tasks and not spaces.deleted, \
        "the row is kept and the deletion is finished in the background"
    assert any("another request is removing" in e for e in out["cleanup"])
    bg.run()
    assert seen == ["deleting"], "the row was still there while the other teardown ran"
    assert spaces.deleted == [SPACE_A], "and goes once that teardown has finished"
    (a, _k), = gov.of("space_delete_teardown_complete")
    assert a[5]["databases_left"] == []


def test_the_space_is_kept_as_delete_failed_when_the_other_teardown_outlasts_the_wait():
    ns, reg, spaces, gov = _space_ns([_lone_row(status="deleting", teardown_claimed_at=_now_iso(5))])
    _del_space(ns, SPACE_A, _req())
    assert not spaces.deleted and spaces.state[SPACE_A]["status"] == "delete_failed"
    (a, _k), = gov.of("space_delete_teardown_failed")
    assert a[5]["databases_left"] == ["db-x"]


def test_a_shared_database_being_decommissioned_also_keeps_the_row():
    # A decommission of a database this space shares is running; this space only untags
    # it, but the decommission may still need this space's role. Wait for it too.
    row = _shared_row(status="deleting", teardown_claimed_at=_now_iso(5))
    ns, reg, spaces, gov = _space_ns([row])
    ns["time"] = types.SimpleNamespace(sleep=lambda _s: reg.rows.pop("db-x", None), monotonic=lambda: 0.0)
    bg = FakeBackgroundTasks()
    out = ns["delete_space"](SPACE_A, _req(), bg)
    assert out["status"] == "deleting" and bg.tasks and not spaces.deleted
    bg.run()
    assert spaces.deleted == [SPACE_A]


def test_a_stalled_database_is_not_waited_for():
    # A decommission of a shared database stopped with its process (a stalled claim):
    # nothing is running, so this space untags it and its row goes at once.
    row = _shared_row(status="deleting", teardown_claimed_at=_now_iso(STALE))
    ns, reg, spaces, gov = _space_ns([row])
    out = _del_space(ns, SPACE_A, _req())
    assert out["message"] == "Deleted" and spaces.deleted == [SPACE_A], \
        "a shared stalled database is untagged and does not hold the row"


def test_a_failed_space_delete_releases_its_claim():
    class Boom(Exception):
        pass
    ns, reg, spaces, gov = _space_ns([])
    spaces.delete_space = lambda sid: (_ for _ in ()).throw(Boom("ddb down"))
    try:
        _del_space(ns, SPACE_A, _req())
    except Boom:
        pass
    assert spaces.state[SPACE_A]["status"] == "active", "the space goes back to what it was"
    spaces.state[SPACE_A] = {"status": "deleting", "teardown_claimed_at": _now_iso(STALE)}
    try:
        _del_space(ns, SPACE_A, _req())
    except Boom:
        pass
    assert spaces.state[SPACE_A]["status"] == "delete_failed", "a taken-over stalled one goes back as delete_failed"


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception as e:  # noqa: BLE001 - report every failure
            failed += 1
            print("FAIL", fn.__name__, "-", e)
    print("\n%d passed, %d failed" % (len(fns) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
