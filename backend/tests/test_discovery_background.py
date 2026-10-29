# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Discovery runs in the background and the console polls its status (#24).

Discovery walked several accounts and Regions serially and could exceed the 29 s gateway
cap, so the console showed 'Discovery failed' while the writes landed anyway. The POST
route now sets ``discovery_status=running``, schedules the work after the response, and
returns at once; ``GET /discovery-status`` is what the console polls. The audit of the
action is kept (in the background body).

The route and the status reader are extracted from backend/app.py and run against fakes.

Run:  python3 backend/tests/test_discovery_background.py
"""
import pathlib
import re
import sys
import types
from datetime import datetime, timezone

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


class FakeHTTPException(Exception):
    def __init__(self, status_code, detail=""):
        self.status_code, self.detail = status_code, detail


class FakeBackground:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *a, **k):
        self.tasks.append((fn, a, k))


class Spaces:
    def __init__(self, space):
        self.space = dict(space)
        self.updates = []

    def get_space(self, sid):
        return dict(self.space) if self.space else None

    def update_space(self, sid, fields):
        self.updates.append(fields)
        self.space.update(fields)


def build(space, can_manage=True):
    sp = Spaces(space)
    scheduled = {"fn": None}
    ns = {
        "HTTPException": FakeHTTPException, "Request": object, "BackgroundTasks": object,
        "datetime": datetime, "timezone": timezone, "os": __import__("os"),
        "logger": types.SimpleNamespace(info=lambda *a, **k: None, error=lambda *a, **k: None,
                                        warning=lambda *a, **k: None),
        "_can_manage_space": lambda a, s: can_manage,
        "spaces_registry": sp,
        "get_user_id": lambda a: "admin-1",
        "_run_discovery": lambda **k: scheduled.update(fn="_run_discovery", kwargs=k),
        "audit_log": types.SimpleNamespace(detached_context=lambda: types.SimpleNamespace(run=lambda fn, **k: fn(**k))),
        "DISCOVERY_STALE_AFTER_S": 600,
        "_DISCOVERY_STALE_MESSAGE": "Discovery did not finish — the service may have restarted. Start it again.",
        "Optional": __import__("typing").Optional,
        "DiscoverRequest": object,          # the optional body's type, only named in the signature
    }
    # The route scopes the run to the space's accounts (real helper, pure).
    exec(_extract("_discovery_scope"), ns)  # noqa: S102
    exec(_extract("_discovery_is_stale"), ns)  # noqa: S102 - the staleness escape hatch
    exec(_extract("discover_space_databases"), ns)  # noqa: S102
    exec(_extract("get_discovery_status"), ns)  # noqa: S102
    return ns, sp, scheduled


def _iso(secs_ago):
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(seconds=secs_ago)).isoformat()


REQ = types.SimpleNamespace(headers={"Authorization": "Bearer x"})
SPACE = {"space_id": "s1", "status": "active", "regions": ["us-east-1"], "primary_account_id": "111111111111"}

print("the route returns at once with a running status and schedules the work")
ns, sp, sched = build(SPACE)
bg = FakeBackground()
out = ns["discover_space_databases"]("s1", REQ, bg)
check(out["status"] == "running" and out["discovery_status"]["state"] == "running", "answers running at once")
check(sp.space["discovery_status"]["state"] == "running" and sp.space["discovery_status"]["by"] == "admin-1",
      "the space is marked running, with who started it")
check(len(bg.tasks) == 1, "the discovery body is scheduled as a background task")
# The task is scheduled through detached_context().run with _run_discovery.
fn, a, k = bg.tasks[0]
fn(*a, **k)
check(sched.get("fn") == "_run_discovery" and sched["kwargs"].get("space_id") == "s1"
      and sched["kwargs"].get("actor_sub") == "admin-1", "it runs _run_discovery with the space and the caller")

print("a RECENT discovery already running is reported, not started twice")
recent = _iso(5)
ns, sp, _ = build({**SPACE, "discovery_status": {"state": "running", "started_at": recent}})
bg = FakeBackground()
out = ns["discover_space_databases"]("s1", REQ, bg)
check(out["discovery_status"]["started_at"] == recent and not bg.tasks, "no second task is scheduled")

print("a STALE running marker does NOT block a new trigger (escape hatch)")
# started_at older than DISCOVERY_STALE_AFTER_S (600s): the worker was lost before the
# terminal write, so the next trigger must start a fresh run rather than no-op forever.
ns, sp, sched = build({**SPACE, "discovery_status": {"state": "running", "started_at": _iso(1200)}})
bg = FakeBackground()
out = ns["discover_space_databases"]("s1", REQ, bg)
check(out["status"] == "running" and len(bg.tasks) == 1, "a fresh discovery task is scheduled")
check(sp.space["discovery_status"]["started_at"] != _iso(1200)
      and sp.space["discovery_status"]["by"] == "admin-1", "the running marker is refreshed with a new start")

print("a running marker with a missing/unparseable started_at is treated as stale")
ns, sp, _ = build({**SPACE, "discovery_status": {"state": "running", "started_at": "t0"}})
bg = FakeBackground()
out = ns["discover_space_databases"]("s1", REQ, bg)
check(len(bg.tasks) == 1, "an unparseable timestamp cannot prove liveness → fresh run")

print("the status route surfaces a stale running as failed, so the spinner clears")
ns, sp, _ = build({**SPACE, "discovery_status": {"state": "running", "started_at": _iso(1200)}})
st = ns["get_discovery_status"]("s1", REQ)["discovery_status"]
check(st["state"] == "failed" and st.get("stale") is True and st.get("message"),
      "a stranded run reports failed + a message for the console's failed branch")
# A recent running is still reported as running (happy path untouched).
ns, sp, _ = build({**SPACE, "discovery_status": {"state": "running", "started_at": _iso(5)}})
check(ns["get_discovery_status"]("s1", REQ)["discovery_status"]["state"] == "running",
      "a recent running is still reported as running")

print("the status route returns the stored status; idle when none")
ns, sp, _ = build({**SPACE, "discovery_status": {"state": "done", "total_discovered": 3}})
check(ns["get_discovery_status"]("s1", REQ)["discovery_status"]["state"] == "done", "done status is returned")
ns, sp, _ = build(SPACE)
check(ns["get_discovery_status"]("s1", REQ)["discovery_status"] == {"state": "idle"}, "idle when never run")

print("access is gated; a deleting space is refused")
ns, sp, _ = build(SPACE, can_manage=False)
try:
    ns["discover_space_databases"]("s1", REQ, FakeBackground())
    check(False, "non-manager refused")
except FakeHTTPException as e:
    check(e.status_code == 403, "a non-manager is refused with 403")
ns, sp, _ = build({**SPACE, "status": "deleting"})
try:
    ns["discover_space_databases"]("s1", REQ, FakeBackground())
    check(False, "deleting refused")
except FakeHTTPException as e:
    check(e.status_code == 409, "a deleting space is refused with 409")

print("the body writes a terminal status and keeps the audit; the console polls")
body = _extract("_run_discovery")
check('"state": "done"' in body and '"state": "failed"' in body, "_run_discovery records done and failed")
check('governance_registry.audit(' in body and '"space_discover"' in body, "the discovery action is still audited")

print("discovery_status survives the real update_space allowlist (not just the test fake)")
# The POST route and _run_discovery both persist discovery_status via
# spaces_registry.update_space, which only writes attributes named in its allowlist. If the
# field is missing there, every write is silently dropped and the console polls a status
# stuck at "idle" forever. This guard reads the real list so the fix cannot regress.
SC = (pathlib.Path(__file__).resolve().parents[1] / "tools/aws/spaces_client.py").read_text(encoding="utf-8")
allow = SC.split("allowed_fields = [", 1)[1].split("]", 1)[0]
check('"discovery_status"' in allow, "update_space allowlist includes discovery_status")
JSX = (pathlib.Path(__file__).resolve().parents[2] / "admin-console/src/components/SpaceDetail.jsx").read_text()
check("/discovery-status" in JSX and "state === 'done'" in JSX, "the console polls discovery-status")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
