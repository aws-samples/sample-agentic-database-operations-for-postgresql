# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""State-changing endpoints gate at manage level and the stale sweep runs server-side (#11, #5).

Two halves:
  * topology/refresh writes RDS/CloudWatch metadata back onto registry records, so it is
    gated on _can_manage_space (not _can_view_space) and writes an audit record (rules 3,
    23). Pinned against source.
  * investigations/cleanup is GONE as an HTTP route — a member could otherwise force-fail
    every running investigation in the space, unaudited. The sweep now runs server-side:
    sweep_stale_investigations() retires only runs older than the staleness window to
    'failed'. Run against a fake registry.

Handler/function source is extracted from backend/app.py. Hermetic, no AWS.

Run:  python3 backend/tests/test_investigation_sweep.py
"""
import pathlib
import re
import sys
import types
from datetime import datetime, timedelta, timezone

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


def route_body(decorator: str) -> str:
    start = APP_SRC.find(decorator)
    if start == -1:
        return ""
    nxt = APP_SRC.find("\n@app.", start + len(decorator))
    return APP_SRC[start: nxt if nxt != -1 else len(APP_SRC)]


# ── topology/refresh: manage gate + audit ──
print("══ topology/refresh is manage-gated and audited ══")
body = route_body('@app.post("/api/spaces/{space_id}/topology/refresh")')
check(bool(body), "the route exists")
check("_can_manage_space(" in body, "gated on _can_manage_space")
check("_can_view_space(" not in body, "no longer gated on _can_view_space")
check('status_code=403, detail="Admin access required"' in body, "a non-manager is refused 403")
check('governance_registry.audit(' in body and '"space_topology_refresh"' in body,
      "the refresh writes an audit record")

# ── investigations/cleanup HTTP route is gone ──
print("")
print("══ the investigations/cleanup HTTP route is removed ══")
check('"/api/spaces/{space_id}/investigations/cleanup"' not in APP_SRC,
      "no POST /investigations/cleanup route remains")
check("def cleanup_stuck_investigations" not in APP_SRC,
      "the handler is gone")
check("def sweep_stale_investigations" in APP_SRC and "def _investigation_sweep_loop" in APP_SRC,
      "the sweep runs server-side (function + background loop)")
start_loop = _extract("_start_investigation_sweep_loop")
check("ROUTE_REPAIR_LOOP" in start_loop,
      "the sweep loop starts only in the API task (same gate as route-repair)")


# ── the sweep itself ──
print("")
print("══ sweep_stale_investigations retires only stale running runs ══")


class FakeReg:
    def __init__(self, running):
        self.running = running
        self.status = {}
        self.summaries = {}

    def list_running(self):
        return list(self.running)

    def update_status(self, iid, status, completed_at=None):
        self.status[iid] = status

    def set_summary(self, iid, summary, severity=None):
        self.summaries[iid] = summary


NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat()


reg = FakeReg([
    {"investigation_id": "old", "status": "running", "started_at": iso(NOW - timedelta(minutes=20))},
    {"investigation_id": "fresh", "status": "running", "started_at": iso(NOW - timedelta(minutes=2))},
    {"investigation_id": "nostart", "status": "running", "started_at": ""},
])

ns = {
    "investigations_registry": reg,
    "datetime": datetime, "timezone": timezone,
    "logger": types.SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None),
}
exec(_extract("sweep_stale_investigations").replace(
    "INVESTIGATION_STALE_MINUTES", "10"), ns)  # noqa: S102
n = ns["sweep_stale_investigations"](now=NOW)
check(n == 1, f"exactly one stale run retired (got {n})")
check(reg.status.get("old") == "failed", "the 20-minute-old run is failed")
check("timed out" in reg.summaries.get("old", ""), "its summary says it timed out")
check("fresh" not in reg.status, "a 2-minute-old run is left alone")
check("nostart" not in reg.status, "a run with no start time is left alone")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
