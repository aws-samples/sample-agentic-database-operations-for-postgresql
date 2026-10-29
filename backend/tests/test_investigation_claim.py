# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Investigations claim the run, do not re-stream on reopen, and reject duplicate completion (#5).

  * claim_run stamps a RUNNING, UNCLAIMED record once; a second claim loses.
  * The /invocations investigation branch claims before streaming and refuses to
    re-stream a running record that already carries a claim, or a finished record
    (source assertions on the branch).
  * complete_investigation never overwrites a completed/cancelled run (idempotent).
  * reset_for_retry re-opens a failed/cancelled run (clears claim + summary), and the
    retry endpoint is operator-gated.

The registry methods run against a fake DynamoDB table that honours the conditional
writes. Handler source is extracted from app.py. Hermetic, no AWS.

Run:  python3 backend/tests/test_investigation_claim.py
"""
import pathlib
import re
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if "boto3" not in sys.modules:
    try:
        __import__("boto3")  # real boto3 is fine; we never create a client
    except Exception:
        _b = types.ModuleType("boto3")
        _ddb = types.ModuleType("boto3.dynamodb")
        _cond = types.ModuleType("boto3.dynamodb.conditions")
        _cond.Key = lambda *a, **k: types.SimpleNamespace(eq=lambda v: None)
        _ddb.conditions = _cond
        _b.dynamodb = _ddb
        _b.resource = lambda *a, **k: None
        sys.modules["boto3"] = _b
        sys.modules["boto3.dynamodb"] = _ddb
        sys.modules["boto3.dynamodb.conditions"] = _cond
APP_SRC = (ROOT / "app.py").read_text(encoding="utf-8")
passed = failed = 0


def check(cond, label):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}")


# ── a fake DynamoDB table that enforces the conditions claim_run / reset_for_retry use ──
class CondFail(Exception):
    def __init__(self):
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeTable:
    def __init__(self, item):
        self.item = dict(item)

    def get_item(self, Key):
        return {"Item": dict(self.item)} if self.item else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None, ConditionExpression=None):
        v = ExpressionAttributeValues or {}
        cond = ConditionExpression or ""
        status = self.item.get("status")
        # claim_run condition: "#s = :running AND attribute_not_exists(run_claim)"
        if "attribute_not_exists(run_claim)" in cond:
            if status != v.get(":running") or "run_claim" in self.item:
                raise CondFail()
        # reset_for_retry condition: "#s IN (:failed, :cancelled)"
        if "IN (:failed, :cancelled)" in cond:
            if status not in (v.get(":failed"), v.get(":cancelled")):
                raise CondFail()
        # Apply SET assignments we care about.
        if "run_claim = :c" in UpdateExpression:
            self.item["run_claim"] = v[":c"]
        if ":running" in v and "SET #s = :running" in UpdateExpression:
            self.item["status"] = v[":running"]
        # Apply REMOVE.
        m = re.search(r"REMOVE (.+)$", UpdateExpression)
        if m:
            for field in [f.strip() for f in m.group(1).split(",")]:
                self.item.pop(field, None)


# Build a registry wrapper that uses the fake table.
def _reg(item):
    import importlib
    mod = importlib.import_module("tools.aws.investigations_client")
    reg = mod.InvestigationsRegistry()
    reg._table = FakeTable(item)
    return reg


print("══ claim_run: first stream wins, a reopen loses ══")
reg = _reg({"investigation_id": "i1", "status": "running"})
check(reg.claim_run("i1", "sess-A") is True, "the first stream claims a running, unclaimed run")
check(reg.table.item.get("run_claim") == "sess-A", "the claim is stamped on the record")
check(reg.claim_run("i1", "sess-B") is False, "a second (reopen) stream loses the claim")

reg = _reg({"investigation_id": "i2", "status": "completed"})
check(reg.claim_run("i2", "sess-A") is False, "a finished run cannot be claimed/streamed")

print("")
print("══ reset_for_retry: a failed run re-opens; a completed one does not ══")
reg = _reg({"investigation_id": "i3", "status": "failed", "run_claim": "old",
            "summary": "it broke", "severity": "warning", "completed_at": "t"})
check(reg.reset_for_retry("i3") is True, "a failed run is re-opened")
it = reg.table.item
check(it["status"] == "running", "status back to running")
check("run_claim" not in it and "summary" not in it and "severity" not in it,
      "the claim and the prior summary/severity are cleared")

reg = _reg({"investigation_id": "i4", "status": "completed"})
check(reg.reset_for_retry("i4") is False, "a completed run is not retryable")

# ── the /invocations investigation branch claims before streaming ──
print("")
print("══ /invocations claims the run and refuses a re-stream ══")
branch = APP_SRC[APP_SRC.find('if payload.prompt.startswith("[INVESTIGATION:"):'):
                 APP_SRC.find("    else:\n        stream_model = resolved")]
check("investigations_registry.claim_run(investigation_id, session_id)" in branch,
      "the branch claims the run on the session id")
check("already running in another window" in branch,
      "a claimed running record is refused with a visible message")
check('inv_record.get("status") in ("completed", "cancelled", "failed")' in branch,
      "a finished record is not re-streamed")

# ── complete rejects a duplicate completion (idempotent no-op on completed/cancelled) ──
print("")
print("══ complete_investigation never overwrites a finished run ══")
_cstart = APP_SRC.find("def complete_investigation(")
comp = APP_SRC[_cstart: _cstart + 4000]
check('inv.get("status") in ("completed", "cancelled")' in comp,
      "a completed/cancelled run short-circuits (the duplicate is rejected, not applied)")

# ── the retry endpoint is operator-gated ──
print("")
print("══ the retry endpoint is operator-only ══")
start = APP_SRC.find('@app.post("/api/spaces/{space_id}/investigations/{investigation_id}/retry")')
retry_body = APP_SRC[start: APP_SRC.find("\n@app.", start + 10)]
check("not is_operator_of(" in retry_body, "retry gates on is_operator_of")
check("reset_for_retry(" in retry_body, "retry resets the record for another run")
check('not in ("failed", "cancelled")' in retry_body,
      "only a failed/cancelled run can be retried")

# ── the frontend respects the claim and offers Retry ──
print("")
print("══ the operator app respects the claim and shows Retry ══")
JSX = (ROOT.parent / "frontend/src/components/InvestigationDetail.jsx").read_text(encoding="utf-8")
check("claimedElsewhere" in JSX and "run_claim" in JSX,
      "the component does not re-stream a server-claimed running run")
check("retryInvestigation" in JSX and "/retry" in JSX, "a failed run offers Retry")
check("status === 'failed'" in JSX and "Retry" in JSX, "the Retry control shows on a failed run")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
