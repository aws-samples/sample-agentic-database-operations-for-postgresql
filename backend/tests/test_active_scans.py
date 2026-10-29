# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The dashboard learns every running scan in a space in ONE request (#20).

The dashboard probed Batch once per database (seven ListJobs each) serially on open.
``GET /api/spaces/{id}/active-scans`` lists active jobs over the whole queue once per
active status (paginated) and matches them to databases by the deterministic job name
``scan-<instance_id>`` from round 1, bounded to the space's databases.

Run:  python3 backend/tests/test_active_scans.py
"""
import os
import pathlib
import re
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
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


class FakeBatch:
    """list_jobs by jobStatus, paginated. jobs = {status: [jobName, ...]}."""
    def __init__(self, jobs):
        self.jobs = jobs
        self.calls = []

    def list_jobs(self, jobQueue=None, jobStatus=None, nextToken=None, **kw):
        self.calls.append((jobStatus, nextToken))
        names = self.jobs.get(jobStatus, [])
        # Two pages: one name per page to prove nextToken is followed.
        if nextToken is None:
            first = names[:1]
            return {"jobSummaryList": [{"jobName": n, "status": jobStatus} for n in first],
                    **({"nextToken": "t2"} if len(names) > 1 else {})}
        rest = names[1:]
        return {"jobSummaryList": [{"jobName": n, "status": jobStatus} for n in rest]}


DBS = [
    {"instance_id": "db-1", "status": "configured", "member_space_ids": {"s1"}},
    {"instance_id": "db-2", "status": "configured", "member_space_ids": {"s1"}},
    {"instance_id": "db-3", "status": "configured", "member_space_ids": {"s1"}},
    {"instance_id": "other", "status": "configured", "member_space_ids": {"s2"}},  # another space
]


def build(batch, viewer_ok=True):
    ns = {
        "HTTPException": FakeHTTPException, "Request": object, "os": os,
        "logger": types.SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None),
        "_can_view_space": lambda a, s: viewer_ok,
        "_instance_in_space": lambda d, sid: sid in (d.get("member_space_ids") or ()),
        "_get_batch_client": lambda: batch,
        "db_registry": types.SimpleNamespace(list_instances=lambda: [dict(d) for d in DBS]),
    }
    exec(re.search(r"^_SCAN_ACTIVE_STATUSES = .*$", APP_SRC, re.M).group(0), ns)  # noqa: S102
    exec(_extract("_active_scan_instance_ids"), ns)  # noqa: S102
    exec(_extract("get_space_active_scans"), ns)  # noqa: S102
    return ns


REQ = types.SimpleNamespace(headers={"Authorization": "Bearer x"})
os.environ["BATCH_JOB_QUEUE"] = "q"

print("one request returns the running databases, matched by the deterministic job name")
batch = FakeBatch({"RUNNING": ["scan-db-1", "scan-other"], "RUNNABLE": ["scan-db-2", "scan-db-3"]})
ns = build(batch)
out = ns["get_space_active_scans"]("s1", REQ)
check(out["active"] == ["db-1", "db-2", "db-3"], f"only this space's running scans ({out['active']})")
check("other" not in out["active"], "a job for another space's database is excluded")
# One listing per active status (5), nextToken followed where there was a second page.
statuses = [c[0] for c in batch.calls]
check(statuses.count("RUNNING") == 2 and statuses.count("RUNNABLE") == 2, "each page is followed via nextToken")
check(len(set(statuses)) == 5, "one listing pass per active status, not seven per database")

print("nothing running: an empty list, not an error")
ns = build(FakeBatch({}))
check(ns["get_space_active_scans"]("s1", REQ)["active"] == [], "empty when no scans run")

print("a Batch failure degrades to empty, never breaks the dashboard")


class Boom(FakeBatch):
    def list_jobs(self, **kw):
        raise RuntimeError("Batch throttled")


ns = build(Boom({}))
check(ns["get_space_active_scans"]("s1", REQ)["active"] == [], "a listing failure returns empty")

print("access is gated on the space")
ns = build(FakeBatch({"RUNNING": ["scan-db-1"]}), viewer_ok=False)
try:
    ns["get_space_active_scans"]("s1", REQ)
    check(False, "a non-member is refused")
except FakeHTTPException as e:
    check(e.status_code == 403, "a non-member is refused with 403")

print("the dashboard polls one shared loop and clears timers on unmount")
DASH = (pathlib.Path(__file__).resolve().parents[2] / "frontend/src/components/Dashboard.jsx").read_text()
check("/active-scans" in DASH and "checkActiveScans" not in DASH, "the dashboard uses the space-wide active-scans route")
check("pollRef" in DASH and "clearInterval(pollRef.current)" in DASH and "useEffect(() => stopPoll" in DASH,
      "one shared poll, cleared on unmount")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
