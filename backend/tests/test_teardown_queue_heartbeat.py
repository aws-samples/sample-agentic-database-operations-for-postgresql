# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Databases queued in a space deletion keep a live claim.

A space deletion claims every database it is the last member of (``status=deleting``)
and then tears them down one by one. Databases still waiting their turn used to get no
heartbeat: after TEARDOWN_STALE_S they looked stalled, the console offered Retry, and a
second teardown could start while the first was still queued. Now every beat of the
running teardown also refreshes the queued claims, so only a claim whose process really
stopped can be taken over.

`_teardown_heartbeat`, `_background_teardown`, `_begin_teardown` and
`_background_delete_space` are extracted from backend/app.py by source so the test pins
the code that ships. Time is a fake clock. Hermetic, no AWS.

Run:  python3 backend/tests/test_teardown_queue_heartbeat.py
"""
import contextlib
import contextvars
import os
import pathlib
import re
import sys
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools.aws.teardown_claim import (  # noqa: E402
    TEARDOWN_HEARTBEAT_S, TEARDOWN_STALE_S, teardown_stalled)

APP_SRC = (pathlib.Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
SPACE = "space-a"

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def _extract(name: str) -> str:
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^(?:@|def |class |[A-Z_]+ = )|\Z)",
                  APP_SRC, re.S | re.M)
    assert m, f"{name} not found in app.py"
    return m.group(0)


def ago(s):
    return (datetime.now(timezone.utc) - timedelta(seconds=s)).isoformat()


class FakeClock:
    """time.monotonic / time.sleep for the heartbeat gate and the wait loop."""

    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeRegistry:
    """The claim rule of DbInstanceRegistry (via teardown_stalled), in memory."""

    def __init__(self, rows):
        self.rows = {r["instance_id"]: dict(r) for r in rows}
        self.touched = []
        self.deleted = []

    def get_instance(self, iid):
        r = self.rows.get(iid)
        return dict(r) if r else None

    def list_instances(self):
        return [dict(r) for r in self.rows.values()]

    def claim_teardown(self, iid):
        r = self.rows.get(iid)
        if not r or (r.get("status") == "deleting" and not teardown_stalled(r)):
            return False
        r["status"] = "deleting"
        r["teardown_claimed_at"] = datetime.now(timezone.utc).isoformat()
        return True

    def touch_teardown(self, iid):
        self.touched.append(iid)
        r = self.rows.get(iid)
        if r and r.get("status") == "deleting":
            r["teardown_claimed_at"] = datetime.now(timezone.utc).isoformat()

    def update_instance(self, iid, patch):
        self.rows[iid].update(patch)

    def delete_instance(self, iid):
        self.deleted.append(iid)
        self.rows.pop(iid, None)


class FakeSpaces:
    def __init__(self):
        self.status = "deleting"
        self.deleted = False

    def touch_teardown(self, sid):
        pass

    def update_space(self, sid, patch):
        self.status = patch.get("status", self.status)

    def delete_space(self, sid):
        self.deleted = True


class FakeBackgroundTasks:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *a, **k):
        self.tasks.append((fn, a, k))


def _build(rows, during_first=None):
    reg, spaces, clock = FakeRegistry(rows), FakeSpaces(), FakeClock()
    torn = []
    holder = {}
    ns = {
        "db_registry": reg, "spaces_registry": spaces,
        "governance_registry": types.SimpleNamespace(audit=lambda *a, **k: None),
        "logger": types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None,
                                        error=lambda *a, **k: None),
        "time": types.SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep),
        "TEARDOWN_HEARTBEAT_S": TEARDOWN_HEARTBEAT_S, "teardown_stalled": teardown_stalled,
        "SPACE_TEARDOWN_WAIT_S": 600, "SPACE_TEARDOWN_POLL_S": 5,
        "_refresh_space_db_count": lambda sid: None, "_notify_teardown_failed": lambda *a: None,
        "_instance_in_space": lambda d, sid: sid in (d.get("member_space_ids") or ()),
        "audit_log": types.SimpleNamespace(detached_context=contextvars.copy_context),
    }

    @contextlib.contextmanager
    def wait_heartbeat(beat):
        holder["beat"] = beat
        try:
            yield
        finally:
            holder.pop("beat", None)

    def fake_teardown_database(iid, db):
        torn.append(iid)
        if len(torn) == 1 and during_first:
            during_first(clock, reg, holder)
        reg.delete_instance(iid)
        return {"steps": [f"tore down {iid}"], "complete": True}

    ns["wait_heartbeat"] = wait_heartbeat
    for fn in ("_teardown_heartbeat", "_background_teardown", "_begin_teardown",
               "_background_delete_space"):
        exec(_extract(fn), ns)  # noqa: S102 - pinning shipped source
    ns["_teardown_database"] = fake_teardown_database
    return ns, reg, spaces, torn


def _three(claimed_at):
    return [{"instance_id": f"db-{i}", "status": "deleting", "teardown_claimed_at": claimed_at,
             "member_space_ids": {SPACE}} for i in (1, 2, 3)]


print("== queued databases are beaten with the running one ==")
seen = {}


def long_lattice_wait(clock, reg, holder):
    # The first teardown waits on Lattice for longer than the stale cutoff: the queued
    # claims age past it, then the active wait's heartbeat fires.
    for iid in ("db-2", "db-3"):
        reg.rows[iid]["teardown_claimed_at"] = ago(2 * TEARDOWN_STALE_S)
    seen["stale_before_beat"] = all(teardown_stalled(reg.rows[i]) for i in ("db-2", "db-3"))
    clock.t += TEARDOWN_HEARTBEAT_S + 1
    holder["beat"]()
    seen["fresh_after_beat"] = [not teardown_stalled(reg.rows[i]) for i in ("db-2", "db-3")]
    # A concurrent delete of a queued database (another admin, or the console's Retry).
    bg = FakeBackgroundTasks()
    seen["concurrent"] = ns["_begin_teardown"]("db-2", reg.get_instance("db-2"), bg,
                                                "database_delete", "user-2", [SPACE])
    seen["concurrent_tasks"] = len(bg.tasks)


ns, reg, spaces, torn = _build(_three(ago(5)), during_first=long_lattice_wait)
teardown = [(iid, reg.get_instance(iid)) for iid in ("db-1", "db-2", "db-3")]
out = ns["_background_delete_space"](SPACE, teardown, "Alpha", "owner-a", "user-1")
check(seen["stale_before_beat"], "(setup) without a beat the queued claims would look stalled")
check(seen["fresh_after_beat"] == [True, True], "the running teardown's beat refreshes both queued claims")
check(seen["concurrent"] is False and seen["concurrent_tasks"] == 0,
      "a concurrent delete of a queued database is refused (no second teardown)")
check(torn == ["db-1", "db-2", "db-3"], "every database is torn down exactly once, in order")
check(out == {"complete": True, "left": []} and spaces.deleted, "the space row goes at the end")
check(reg.touched.count("db-1") >= 1 and "db-1" not in reg.rows, "the running database beats its own claim")


print("== the running database leaves the queue ==")
order = []


def watch_queue(clock, reg, holder):
    clock.t += TEARDOWN_HEARTBEAT_S + 1
    reg.touched.clear()
    holder["beat"]()
    order.extend(reg.touched)


ns, reg, spaces, torn = _build(_three(ago(5)), during_first=watch_queue)
ns["_background_delete_space"](SPACE, [(i, reg.get_instance(i)) for i in ("db-1", "db-2", "db-3")],
                               "Alpha", "owner-a", "user-1")
check(order.count("db-1") == 1 and order.count("db-2") == 1 and order.count("db-3") == 1,
      "one beat touches the running database once and each queued one once")


print("== a failing touch does not stop the beat ==")
ns, reg, spaces, torn = _build(_three(ago(5)))
calls = []


def flaky_touch(iid):
    calls.append(iid)
    if iid == "db-2":
        raise RuntimeError("throttled")


reg.touch_teardown = flaky_touch
beat = ns["_teardown_heartbeat"](space_id=SPACE, queued=["db-2", "db-3"])
try:
    beat(force=True)
    raised = False
except Exception:  # noqa: BLE001
    raised = True
check(not raised and calls == ["db-2", "db-3"], "db-3 is still beaten after db-2's touch fails")
check(ns["_teardown_heartbeat"]("db-9")() is None, "the old call shape (no queue) still works")


print("== a dead process stops beating: queued claims can be taken over ==")
ns, reg, spaces, torn = _build(_three(ago(2 * TEARDOWN_STALE_S)))
# Nothing runs _background_delete_space: the process that claimed them is gone.
check(all(teardown_stalled(r) for r in reg.rows.values()), "unbeaten queued claims are stalled")
bg = FakeBackgroundTasks()
check(ns["_begin_teardown"]("db-2", reg.get_instance("db-2"), bg, "database_delete", "user-2", [SPACE])
      and len(bg.tasks) == 1, "a later delete takes a stalled queued claim over")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
