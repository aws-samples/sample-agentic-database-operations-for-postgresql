# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""`/complete` separates a real result from a failure (#8).

A streamed error, a rejected request, or a stream that closes without a result must be
recorded as a FAILED investigation — not a green "completed" one — keeping the partial
text as the failure detail and storing no findings or severity. A real result still
completes with findings. An already completed or cancelled run is never overwritten.

The handler is extracted from backend/app.py by source and run against a fake
investigations registry. Hermetic, no AWS, no FastAPI.

Run:  python3 backend/tests/test_investigation_complete.py
"""
import pathlib
import re
import sys
import types

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
    assert m, f"{name} not found in app.py"
    return m.group(0)


class FakeTable:
    def __init__(self):
        self.updates = []

    def update_item(self, **kw):
        self.updates.append(kw)


class Registry:
    def __init__(self, inv):
        self.inv = dict(inv)
        self.table = FakeTable()
        self.findings = []
        self.summary = None
        self.severity = "UNSET"
        self.status = inv.get("status")
        self.completed_at = None

    def get(self, iid):
        return dict(self.inv)

    def add_finding(self, iid, f):
        self.findings.append(f)

    def set_summary(self, iid, summary, severity):
        self.summary, self.severity = summary, severity

    def update_status(self, iid, status, completed_at=None):
        self.status, self.completed_at = status, completed_at
        self.inv["status"] = status


class FakeHTTPException(Exception):
    def __init__(self, status_code, detail=""):
        self.status_code, self.detail = status_code, detail


def build(inv):
    reg = Registry(inv)
    ns = {
        "HTTPException": FakeHTTPException, "Request": object, "InvestigationComplete": object,
        "datetime": __import__("datetime").datetime,
        "timezone": __import__("datetime").timezone,
        "investigations_registry": reg,
        "_can_view_space": lambda auth, sid: True,
        "is_operator_of": lambda auth, sid: True,
        "get_user_id": lambda auth: "user-1",
        "_extract_findings": lambda text: [{"severity": "warning", "title": "x"}],
        "_extract_sections": lambda text: {"root_cause": "rc"},
        "logger": types.SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None),
    }
    exec(_extract("complete_investigation"), ns)  # noqa: S102 - pinning shipped source
    return ns["complete_investigation"], reg


REQ = types.SimpleNamespace(headers={"Authorization": "Bearer x"})
INV = {"space_id": "s1", "status": "running"}


def data(**kw):
    base = {"summary": "", "findings": [], "sections": {}, "timeline": [],
            "status": "completed", "failure_detail": ""}
    base.update(kw)
    return types.SimpleNamespace(**base)


print("a real result completes with findings")
fn, reg = build(INV)
out = fn("s1", "i1", data(summary="## Root cause\nAll fine", status="completed"), REQ)
check(out["status"] == "completed", "status completed")
check(reg.status == "completed" and reg.findings, "findings stored, run completed")

print("an error event records a failure, not a completion")
fn, reg = build(INV)
out = fn("s1", "i1", data(summary="partial text so far", status="failed",
                          failure_detail="guardrail intervened"), REQ)
check(out["status"] == "failed", "status failed")
check(reg.status == "failed", "the run is marked failed")
check(reg.findings == [], "no findings are stored for a failure")
check(reg.severity is None, "no severity for a failure")
check(reg.summary == "partial text so far", "the partial text is kept as the detail")
sec = reg.table.updates[0]["ExpressionAttributeValues"][":sec"]
check(sec.get("failure_detail") == "guardrail intervened", "the failure detail is recorded")

print("a stream that closed with no partial text still records an honest detail")
fn, reg = build(INV)
out = fn("s1", "i1", data(status="failed", failure_detail="The agent stream ended without a result."), REQ)
check(reg.status == "failed" and "ended without a result" in reg.summary,
      "the failure summary explains it did not finish")

print("an already completed run is never overwritten")
fn, reg = build({"space_id": "s1", "status": "completed", "findings": [{}], "severity": "warning"})
out = fn("s1", "i1", data(summary="late", status="failed"), REQ)
check(out["status"] == "completed" and reg.status == "completed",
      "a failed post does not downgrade a finished investigation")

print("a cancelled run is never overwritten")
fn, reg = build({"space_id": "s1", "status": "cancelled"})
out = fn("s1", "i1", data(status="completed", summary="x"), REQ)
check(out["status"] == "cancelled", "a cancelled run stays cancelled")

print("the operator app shows a failed run as failed and does not offer chat/escalate")
JSX = (pathlib.Path(__file__).resolve().parents[2] / "frontend/src/components/InvestigationDetail.jsx").read_text()
check("status === 'failed'" in JSX, "the status badge has a failed state")
check("onChatAbout && status === 'completed'" in JSX, "chat handoff is gated on completed")
check("finalizeInvestigation('failed'" in JSX, "the stream client finalizes failures as failed")
check(JSX.count("finalizeInvestigation('failed'") >= 3,
      "error event, stream-close-without-result and rejected stream all finalize as failed")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
