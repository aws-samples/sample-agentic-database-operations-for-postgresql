# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A metrics-store read failure is 'unavailable' (503), never 'no data' (#12).

`_store_query` used to swallow every exception and return [], so an IAM/TLS/Aurora/schema
outage looked exactly like a database the collector never scanned: a 404 "no results",
empty history, a monitoring status of none. Now a failed read raises ``StoreUnavailable``
(503) on request-serving routes, while a genuinely empty result is still []. Deliberately
degraded views use ``_store_query_soft`` to keep prior data and carry ``store_error``.

Helpers and the latest-scan route are extracted from backend/app.py and run against a
fake metrics_store. Hermetic, no AWS, no FastAPI.

Run:  python3 backend/tests/test_store_unavailable.py
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
    m = re.search(rf"^(?:class|def) {re.escape(name)}[\s(:].*?(?=^(?:@|def |class |[A-Z_]+ = )|\Z)",
                  APP_SRC, re.S | re.M)
    assert m, f"{name} not found in app.py"
    return m.group(0)


class FakeHTTPException(Exception):
    def __init__(self, status_code, detail="", headers=None):
        self.status_code, self.detail = status_code, detail


class FakeStore:
    """Stands in for tools.aws.metrics_store: ``tier2_latest_full`` returns ``row`` or raises."""
    def __init__(self, row=None, raises=None):
        self.row, self.raises = row, raises

    def tier2_latest_full(self, instance_id):
        if self.raises:
            raise self.raises
        return self.row


def build(store, viewer_ok=True, configured=True):
    ns = {
        "HTTPException": FakeHTTPException, "Request": object,
        "logger": types.SimpleNamespace(error=lambda *a, **k: None, warning=lambda *a, **k: None,
                                        info=lambda *a, **k: None),
        "_can_view_instance": lambda auth, iid: viewer_ok,
        "_store_is_configured": lambda: configured,
        "_STORE_NOT_CONFIGURED": "The metrics store is not configured",
    }
    # The route imports the store via `from tools.aws import metrics_store`.
    tools_mod = sys.modules.setdefault("tools", types.ModuleType("tools"))
    aws_mod = types.ModuleType("tools.aws")
    aws_mod.metrics_store = store
    tools_mod.aws = aws_mod
    sys.modules["tools.aws"] = aws_mod
    sys.modules["tools.aws.metrics_store"] = store
    exec(_extract("StoreUnavailable"), ns)  # noqa: S102
    for fn in ("_store_call", "_store_call_soft", "get_latest_scan_result"):
        exec(_extract(fn), ns)  # noqa: S102 - pinning shipped source
    return ns


REQ = types.SimpleNamespace(headers={"Authorization": "Bearer x"})
OUTAGE = RuntimeError("AccessDeniedException: not authorized to perform dynamodb:Query")


def status(fn, *a, **k):
    try:
        return 200, fn(*a, **k)
    except FakeHTTPException as e:
        return e.status_code, e.detail


def boom():
    raise OUTAGE


print("the helpers distinguish an outage from an empty result")
ns = build(FakeStore())
try:
    ns["_store_call"](boom)
    check(False, "_store_call raised on an outage")
except FakeHTTPException as e:
    check(e.status_code == 503, "_store_call raises 503 on an outage")
    check("AccessDenied" not in str(e.detail), "the 503 carries no AWS error text")
rows, err = ns["_store_call_soft"](boom, [])
check(rows == [] and err, "_store_call_soft returns (default, message) on an outage")
check(ns["_store_call"](lambda: []) == [], "an empty result is just [] (no raise)")
check(ns["_store_call_soft"](lambda: [], None) == ([], None), "soft: empty result, no error")
try:
    ns["_store_call"](lambda: (_ for _ in ()).throw(FakeHTTPException(404, "x")))
    check(False, "an HTTPException passes through")
except FakeHTTPException as e:
    check(e.status_code == 404, "an HTTPException raised inside passes through unchanged")

print("the latest-scan route: outage is 503, genuinely empty is 404")
ns = build(FakeStore(raises=OUTAGE))
code, _ = status(ns["get_latest_scan_result"], "db-1", REQ)
check(code == 503, f"a store outage is 503, not 404 ({code})")
ns = build(FakeStore(row=None))
code, _ = status(ns["get_latest_scan_result"], "db-1", REQ)
check(code == 404, f"a database the collector never scanned is 404 ({code})")
ns = build(FakeStore(row={"id": "db-1|t|db-1", "health_score": "ok"}))
code, out = status(ns["get_latest_scan_result"], "db-1", REQ)
check(code == 200 and out["health_score"] == "ok", "a real row is returned")
ns = build(FakeStore(row={"id": "x"}), configured=False)
code, _ = status(ns["get_latest_scan_result"], "db-1", REQ)
check(code == 503, "an unconfigured store is 503")
ns = build(FakeStore(row={"id": "x"}), viewer_ok=False)
code, _ = status(ns["get_latest_scan_result"], "db-1", REQ)
check(code == 403, "a non-member is refused before the store is read")

print("the degraded views carry store_error and the UI warns")
src = APP_SRC
mem = re.search(r"def get_database_members\(.*?(?=\n@app\.)", src, re.S).group(0)
check('"store_error": store_error' in mem and "_store_call_soft(" in mem,
      "get_database_members uses the soft read and returns store_error")
mon = re.search(r"def admin_monitoring_status\(.*?(?=\n\n\n|@app\.)", src, re.S).group(0)
check('"store_error": store_error' in mon and 'tier1_state="unknown"' in mon,
      "admin_monitoring_status carries store_error and blanks the status to unknown")
ROOT = pathlib.Path(__file__).resolve().parents[2]
for app_name in ("frontend", "admin-console"):
    cm = (ROOT / app_name / "src/components/ClusterMembers.jsx").read_text()
    check("data.store_error" in cm, f"{app_name} members view warns on store_error")
mc = (ROOT / "admin-console/src/components/MonitoringCollection.jsx").read_text()
check("storeError" in mc and "unknown" in mc, "the collection view warns on store_error and shows Unknown")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
