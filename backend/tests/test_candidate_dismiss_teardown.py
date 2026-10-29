# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Dismissing a failed first onboard tears its network path down.

A first onboard builds the network path (peering or a Lattice resource endpoint) before it
checks connectivity. When that check fails the record stays a candidate, carrying the
`network_method` it built. Dismissing that last candidate must not just delete the record
and leave the peering or the endpoint, its share and its configuration running untracked:
it goes through `_teardown_database`, which cleans the network path up and removes the
record and the schedules. A plain discovery candidate (no `network_method`) is still just
deleted, and a record another space still proposes is kept as it is.

`dismiss_space_candidate` and `_teardown_database` are extracted from backend/app.py by
source so the test pins the code that ships. Hermetic, no AWS.

Run:  python3 backend/tests/test_candidate_dismiss_teardown.py   (or via pytest)
"""
import contextlib
import contextvars
import os
import pathlib
import re
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import time  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from tools.aws.teardown_claim import teardown_stalled, TEARDOWN_STALE_S  # noqa: E402
APP_SRC = (pathlib.Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")

SPACE_A, SPACE_B = "space-a", "space-b"


def _extract(name: str) -> str:
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^(?:@|def |class )|\Z)", APP_SRC, re.S | re.M)
    assert m, f"{name} not found in app.py"
    return m.group(0)


class FakeHTTPException(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


class FakeRegistry:
    def __init__(self, rows):
        self.rows = {r["instance_id"]: dict(r) for r in rows}
        self.calls = []

    def get_instance(self, iid):
        row = self.rows.get(iid)
        return dict(row) if row else None

    def remove_candidate(self, iid, sid):
        self.calls.append(("remove_candidate", iid, sid))
        c = set(self.rows[iid].get("candidate_space_ids") or [])
        c.discard(sid)
        self.rows[iid]["candidate_space_ids"] = c

    def remove_member(self, iid, sid, keep_one=False):
        self.calls.append(("remove_member", iid, sid))

    def delete_instance(self, iid):
        self.calls.append(("delete_instance", iid))
        self.rows.pop(iid, None)

    def update_instance(self, iid, patch):
        self.calls.append(("update_instance", iid))
        self.rows[iid].update(patch)

    def claim_teardown(self, iid):
        row = self.rows.get(iid)
        if not row or (row.get("status") == "deleting" and not teardown_stalled(row)):
            return False
        row["status"] = "deleting"
        row["teardown_claimed_at"] = datetime.now(timezone.utc).isoformat()
        return True
    def touch_teardown(self, iid):
        self.calls.append(("touch_teardown", iid))


class FakeBackgroundTasks:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *a, **k):
        self.tasks.append((fn, a, k))

    def run(self):
        while self.tasks:
            fn, a, k = self.tasks.pop(0)
            fn(*a, **k)


class FakeAudit:
    def __init__(self):
        self.events = []

    def audit(self, *a, **k):
        self.events.append((a, k))


class FakeNetwork:
    def __init__(self):
        self.calls = []

    def cleanup_lattice(self, **k):
        self.calls.append(("lattice", k))
        return {"steps": ["Deleted resource endpoint vpce-1", "Deleted the resource configuration"], "complete": True}

    def cleanup_peering(self, **k):
        self.calls.append(("peering", k))
        return {"steps": ["Deleted peering: pcx-1"]}


def _build(rows):
    reg, gov, net = FakeRegistry(rows), FakeAudit(), FakeNetwork()
    teardowns = []
    ns = {
        "db_registry": reg, "governance_registry": gov, "network_configurator": net,
        "HTTPException": FakeHTTPException, "Request": object, "os": os,
        "logger": types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None,
                                        error=lambda *a, **k: None),
        "get_user_id": lambda auth: "user-1",
        "_can_manage_space": lambda auth, sid: True,
        "_instance_member_spaces": lambda inst: set((inst or {}).get("member_space_ids") or []),
        "_instance_candidate_spaces": lambda inst: set((inst or {}).get("candidate_space_ids") or []),
        "spaces_registry": types.SimpleNamespace(get_space=lambda sid: {"primary_account_id": "111111111111"}),
        "db_discovery": types.SimpleNamespace(_get_cross_account_session=lambda *a, **k: None),
        "_get_scheduler_client": lambda: (_ for _ in ()).throw(RuntimeError("no scheduler")),
        "_schedule_name": lambda iid: f"t2-{iid}", "_tier1_schedule_name": lambda iid: f"t1-{iid}",
        "_delete_db_alarms": lambda iid: None,
        "BackgroundTasks": object,
        "audit_log": types.SimpleNamespace(detached_context=contextvars.copy_context),
        "_refresh_space_db_count": lambda sid: None,
        "_notify_teardown_failed": lambda *a: None,
        "teardown_stalled": teardown_stalled, "TEARDOWN_HEARTBEAT_S": 60, "time": time,
        "wait_heartbeat": lambda beat: contextlib.nullcontext(),
    }
    exec(_extract("_teardown_database"), ns)  # noqa: S102 - pinning shipped source
    for fn in ("_teardown_heartbeat", "_background_teardown", "_begin_teardown"):
        exec(_extract(fn), ns)  # noqa: S102
    real_teardown = ns["_teardown_database"]

    def counting_teardown(iid, db):
        teardowns.append(iid)
        return real_teardown(iid, db)

    ns["_teardown_database"] = counting_teardown
    handler = re.sub(r"^@app\.delete\([^\n]*\)\n", "", _extract("dismiss_space_candidate"), flags=re.M)
    exec(handler, ns)  # noqa: S102
    return ns, reg, gov, net, teardowns


def _req():
    return types.SimpleNamespace(headers={"Authorization": "Bearer x"})


def _dismiss(ns, iid="db-1"):
    """Dismiss, then run the background tasks (the response has gone)."""
    bg = FakeBackgroundTasks()
    out = ns["dismiss_space_candidate"](SPACE_A, iid, _req(), bg)
    pending = len(bg.tasks)
    bg.run()
    return out, pending


def _failed_onboard(method, **over):
    row = {"instance_id": "db-1", "display_name": "orders", "status": "config_failed",
           "network_method": method, "candidate_space_ids": {SPACE_A}, "member_space_ids": set(),
           "vpc_id": "vpc-db", "region": "us-east-1", "account_id": "111111111111",
           "endpoint": "orders.cluster-x.us-east-1.rds.amazonaws.com", "vpce_id": "vpce-1",
           "resource_configuration_arn": "arn:rcfg", "resource_share_arn": "arn:share",
           "network_peering_id": "pcx-1"}
    row.update(over)
    return row


def _audit_after(gov):
    (args, kwargs), = [e for e in gov.events if e[0][1] == "space_candidate_dismiss"]
    return args[5]


def test_dismissing_a_failed_lattice_onboard_tears_the_endpoint_down():
    ns, reg, gov, net, teardowns = _build([_failed_onboard("lattice")])
    out, pending = _dismiss(ns)
    assert out == {"message": "Deleting", "status": "deleting", "removed_record": True}
    assert pending == 1, "the teardown runs after the response"
    assert teardowns == ["db-1"], "the record goes through _teardown_database exactly once"
    (kind, kw), = net.calls
    assert kind == "lattice" and kw["vpce_id"] == "vpce-1" and kw["instance_id"] == "db-1"
    assert kw["resource_configuration_arn"] == "arn:rcfg" and kw["resource_share_arn"] == "arn:share"
    assert "db-1" not in reg.rows and reg.calls.count(("delete_instance", "db-1")) == 1, \
        "the record is deleted by the teardown, once"
    after = _audit_after(gov)
    assert after["removed_record"] is True and after["network_teardown"] is True
    (args, kwargs), = [e for e in gov.events if e[0][1] == "database_teardown_complete"]
    assert args[5]["trigger"] == "space_candidate_dismiss" and len(args[5]["cleanup"]) == 2
    assert kwargs["space_ids"] == [SPACE_A]


def test_dismissing_a_failed_peering_onboard_tears_the_peering_down():
    ns, reg, gov, net, teardowns = _build([_failed_onboard("peering")])
    _dismiss(ns)
    (kind, kw), = net.calls
    assert kind == "peering" and kw["peering_id"] == "pcx-1" and kw["db_vpc_id"] == "vpc-db"
    assert teardowns == ["db-1"] and "db-1" not in reg.rows
    assert _audit_after(gov)["network_teardown"] is True


def test_a_plain_discovery_candidate_is_just_deleted():
    ns, reg, gov, net, teardowns = _build([_failed_onboard("", status="discovered")])
    out, pending = _dismiss(ns)
    assert out == {"message": "Dismissed", "removed_record": True} and pending == 0
    assert not teardowns and not net.calls, "no network path was built, nothing to tear down"
    assert ("delete_instance", "db-1") in reg.calls and "db-1" not in reg.rows
    after = _audit_after(gov)
    assert after["network_teardown"] is False


def test_a_record_another_space_still_proposes_is_kept():
    ns, reg, gov, net, teardowns = _build([_failed_onboard("lattice", candidate_space_ids={SPACE_A, SPACE_B})])
    out, _ = _dismiss(ns)
    assert out["removed_record"] is False
    assert not teardowns and not net.calls and "db-1" in reg.rows
    assert not any(c[0] == "delete_instance" for c in reg.calls)
    assert _audit_after(gov)["network_teardown"] is False


def test_a_dismiss_while_deleting_is_a_no_op():
    fresh = datetime.now(timezone.utc).isoformat()
    ns, reg, gov, net, teardowns = _build([_failed_onboard("lattice", status="deleting",
                                                           teardown_claimed_at=fresh)])
    out, pending = _dismiss(ns)
    assert out["status"] == "deleting" and pending == 0 and not net.calls and not gov.events
    assert "db-1" in reg.rows, "the running teardown owns the record"


def test_a_dismiss_of_a_stalled_removal_retries_it():
    # The teardown's process stopped: no heartbeat for TEARDOWN_STALE_S. Dismissing the
    # row again (the console shows it as Delete failed) takes the claim over.
    stale = (datetime.now(timezone.utc) - timedelta(seconds=2 * TEARDOWN_STALE_S)).isoformat()
    ns, reg, gov, net, teardowns = _build([_failed_onboard("lattice", status="deleting",
                                                           teardown_claimed_at=stale)])
    out, pending = _dismiss(ns)
    assert out["status"] == "deleting" and pending == 1 and teardowns == ["db-1"]
    assert "db-1" not in reg.rows and [c[0] for c in net.calls] == ["lattice"]


def test_a_failed_teardown_keeps_the_candidate_and_dismissing_again_retries():
    ns, reg, gov, net, teardowns = _build([_failed_onboard("lattice")])
    results = [{"steps": ["Teardown stopped at endpoint: timeout"], "complete": False},
               {"steps": ["Deleted resource endpoint vpce-1"], "complete": True}]
    net.cleanup_lattice = lambda **k: (net.calls.append(("lattice", k)), results.pop(0))[1]
    _dismiss(ns)
    row = reg.rows["db-1"]
    assert row["status"] == "delete_failed" and row["config_log"] == ["Teardown stopped at endpoint: timeout"]
    assert SPACE_A in row["candidate_space_ids"], "the row stays in the candidates as Delete failed"
    assert [e for e in gov.events if e[0][1] == "database_teardown_failed"]
    out, pending = _dismiss(ns)
    assert out["status"] == "deleting" and pending == 1, "dismissing again is the Retry"
    assert "db-1" not in reg.rows and len(net.calls) == 2


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  ok   {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {e!r}")
    print(f"pass={passed} fail={failed}")
    sys.exit(1 if failed else 0)
