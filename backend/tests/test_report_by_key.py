# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The report panel loads the analysis for the SELECTED report, not the latest (#13).

Scan history passed the selected report's S3 key but the panel fetched
``/scan-results/{id}/latest`` regardless, so an old report showed under today's health,
findings and metrics. ``GET /api/scan-results/by-report?key=`` returns the exact
stored scan for that key (the sparse ``by-report-key`` index on ``report_s3_key``),
falling back to the S3 analysis beside the report; access is checked against the key's
own database, and a key without that segment is refused.

Extracted from backend/app.py and run against a fake store and report storage.

Run:  python3 backend/tests/test_report_by_key.py
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


class FakeHTTPException(Exception):
    def __init__(self, status_code, detail=""):
        self.status_code, self.detail = status_code, detail


import os  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import fake_metrics_store  # noqa: E402
from tools.aws import metrics_store as ms  # noqa: E402

KEY_OLD = "111111111111/db-1/2026-09-01/r/report.html"
KEY_NEW = "111111111111/db-1/2026-10-06/r/report.html"


def build(viewer_ok=True, configured=True, s3=None):
    ns = {
        "HTTPException": FakeHTTPException, "Request": object,
        "_can_view_instance": lambda auth, iid: viewer_ok(iid) if callable(viewer_ok) else viewer_ok,
        "_store_is_configured": lambda: configured,
        "_store_call": lambda fn: fn(),
        "report_storage": types.SimpleNamespace(get_report=lambda k: (s3 or {}).get(k)),
    }
    for name in ("_section_names", "get_scan_result_by_report"):
        exec(_extract(name), ns)  # noqa: S102 - pinning shipped source
    return ns["get_scan_result_by_report"]


REQ = types.SimpleNamespace(headers={"Authorization": "Bearer x"})


def status(fn, **k):
    try:
        return 200, fn(REQ, **k)
    except FakeHTTPException as e:
        return e.status_code, e.detail


fake = fake_metrics_store.install()
for key, health, when in ((KEY_OLD, "critical", "2026-09-01T06:00:00Z"), (KEY_NEW, "ok", "2026-10-06T06:00:00Z")):
    ms.put_tier2_scan("db-1", {"scan_time": when, "health_score": health, "report_s3_key": key,
                               "scan_kind": "full", "member_instance_id": "db-1"},
                      {"overview": {"version": "16.4"}, "performance": {"top_queries": []}})
# Another database's scan whose stored key names db-1: never served to a db-1 viewer.
ms.put_tier2_scan("db-2", {"scan_time": "2026-10-06T07:00:00Z", "health_score": "warning",
                           "report_s3_key": "111111111111/db-1/2026-10-07/r/report.html",
                           "scan_kind": "full", "member_instance_id": "db-2"}, {})

print("the row matched is the selected report's, not the latest")
fn = build()
code, out = status(fn, key=KEY_OLD)
check(code == 200 and out["health_score"] == "critical", "the old report returns its own (critical) scan")
check(out["report_s3_key"] == KEY_OLD and fake.t2.queries[-1]["index"] == "by-report-key",
      "matched on report_s3_key through the sparse index")
code, out = status(fn, key=KEY_NEW)
check(out["health_score"] == "ok" and "sections_data" not in out, "the new report returns its own (ok) scan")
code, out = status(fn, key=KEY_NEW, sections="all")
check(out["sections_data"]["overview"] == {"version": "16.4"}, "sections=all adds the stored sections")
code, out = status(fn, key=KEY_NEW, sections="overview,bogus")
check(list(out["sections_data"]) == ["overview"], "unknown section names are ignored")
code, _ = status(fn, key="111111111111/db-1/2026-10-07/r/report.html")
check(code == 404, "a scan of another database is not served under this database's key")

print("a key with no stored scan falls back to the S3 analysis beside it")
fn = build(s3={"111111111111/db-1/2026-08-01/r/analysis.json": {"health_score": "warning"}})
code, out = status(fn, key="111111111111/db-1/2026-08-01/r/report.html")
check(code == 200 and out["analysis"] == {"health_score": "warning"},
      "the analysis.json beside the report is returned")
fn = build()
code, _ = status(fn, key="111111111111/db-1/2026-08-01/r/report.html")
check(code == 404, "no scan and no analysis is a 404, not a wrong report")

print("access is gated on the key's own database, and fails closed")
fn = build(viewer_ok=False)
code, _ = status(fn, key=KEY_OLD)
check(code == 403, "a caller who cannot see the database is refused")
seen = []
fn = build(viewer_ok=lambda iid: seen.append(iid) or True, s3={"report.html": {"health_score": "ok"}})
for bad in ("report.html", "/report.html", "111111111111/../db-1/x/report.html"):
    code, _ = status(fn, key=bad)
    check(code == 400, f"a key without its database segment is refused, not read unchecked ({bad!r})")
check(not seen, "nothing was read or gated for a malformed key")

print("the panel uses the by-report route, not /latest")
JSX = (pathlib.Path(__file__).resolve().parents[2] / "frontend/src/components/ReportPanel.jsx").read_text()
check("/api/scan-results/by-report?key=" in JSX, "ReportPanel fetches the selected report's analysis")
check("/latest`" not in JSX, "ReportPanel no longer fetches the latest scan for a historical report")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
