# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Space Trends reads the store and shows the NEWEST same-day scan (#17, #21).

Space Trends used to do one S3 GET per scan per database behind the 29 s gateway cap (a
504 for a space of any size). It now reads health, findings and metrics from the
stored deep-scan summaries (DynamoDB) in one read (the newest full scan per database per day), and
orders each series by the full timestamp so the fleet card's "latest" and the heatmap
cell take the newest scan of the day, not the oldest.

``get_space_trends`` is extracted from backend/app.py and run against a fake store and
registry (the real ``scan_records`` and store code over an in-memory DynamoDB). Hermetic, no AWS, no S3, no FastAPI.

Run:  python3 backend/tests/test_space_trends_order.py
"""
import json
import os
import pathlib
import re
import sys
import types
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import scan_records  # noqa: E402

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


sys.path.insert(0, os.path.dirname(__file__))
import fake_metrics_store  # noqa: E402
from datetime import timedelta  # noqa: E402
from tools.aws import metrics_store as ms  # noqa: E402

# Two scans on D (06:00 critical scheduled, 14:00 ok rescan), one on D-1, and a reader
# scan on D that must not count. Written deliberately out of order.
D = (datetime.now(timezone.utc) - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
D1 = D - timedelta(days=1)
fake = fake_metrics_store.install()
for when, health, findings, chr_, kind, member in (
        (D.replace(hour=6), "critical", [{"severity": "CRITICAL"}], 90.0, "full", "db-1"),
        (D1.replace(hour=6), "warning", [], 95.0, "full", "db-1"),
        (D.replace(hour=14), "ok", [], 99.0, "full", "db-1"),
        (D.replace(hour=20), "critical", [{"severity": "CRITICAL"}], 1.0, "reader", "db-1r")):
    ms.put_tier2_scan("db-1", {"scan_time": when.isoformat(), "health_score": health, "findings": findings,
                               "cache_hit_ratio": chr_, "scan_kind": kind, "member_instance_id": member,
                               "member_role": "writer" if kind == "full" else "reader"}, {})


def build(configured=True):
    captured = {"calls": 0}

    def store_call(fn):
        captured["calls"] += 1
        return fn()
    db = {"instance_id": "db-1", "display_name": "orders", "status": "configured",
          "account_id": "a", "engine": "aurora-postgresql", "member_space_ids": {"s1"}}
    ns = {
        "HTTPException": type("E", (Exception,), {"__init__": lambda s, status_code=0, detail="": None}),
        "Request": object, "datetime": datetime, "timezone": timezone, "json": json,
        "scan_records": scan_records,
        "logger": types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None,
                                        debug=lambda *a, **k: None),
        "_can_view_space": lambda auth, sid: True,
        "_instance_in_space": lambda d, sid: sid in (d.get("member_space_ids") or ()),
        "_store_is_configured": lambda: configured,
        "_store_call": store_call,
        "db_registry": types.SimpleNamespace(list_instances=lambda: [dict(db)]),
        "investigations_registry": types.SimpleNamespace(list_for_space=lambda sid: []),
    }
    for name in ("_findings_list", "_trend_point", "get_space_trends"):
        exec(_extract(name), ns)  # noqa: S102 - pinning shipped source
    return ns["get_space_trends"], captured


REQ = types.SimpleNamespace(headers={"Authorization": "Bearer x"})
fn, cap = build()
out = fn("s1", REQ, days=30)
print("it reads the store, not S3")
check(cap["calls"] == 1 and fake.t2.queries and all(q["index"] is None for q in fake.t2.queries),
      "one store read (keyed Queries on the database's partition) backs the trends")
dbrow = out["databases"][0]
pts = dbrow["data_points"]
check(len(pts) == 2 and all(p["cache_hit_ratio"] != 1.0 for p in pts),
      "one point per day; the reader scan is not a day's deep scan")
print("the series ascends by full timestamp")
check([p["time"] for p in pts] == sorted(p["time"] for p in pts), "data_points ascend by timestamp")
check(pts[-1]["health_score"] == "ok" and "T14:00:00" in pts[-1]["time"],
      "the last point is the newest same-day scan (the 14:00 rescan)")
print("the fleet card latest is the newest scan, not the oldest same-day one")
check(dbrow["latest"]["health_score"] == "ok", "latest is the 14:00 ok rescan, not 06:00 critical")
print("the heatmap cell takes the newest scan of the day")
by_date = {}
for p in pts:
    by_date[p["date"]] = p["health_score"]   # mirrors Trends.jsx lookup build
check(by_date[D.date().isoformat()] == "ok" and by_date[D1.date().isoformat()] == "warning", "newest per day")
check(all(p["findings_critical"] == 0 for p in pts) and all(isinstance(p["id"], str) for p in pts),
      "findings counts come from the stored findings; ids are scan ids")

print("days is clamped and no store call is made when the store is not configured")
fn, cap = build(configured=False)
out = fn("s1", REQ, days=9999)
check(out["days"] <= 365 and cap["calls"] == 0, "no read when unconfigured; days clamped")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
