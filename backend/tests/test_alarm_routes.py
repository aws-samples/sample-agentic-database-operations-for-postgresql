# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The alarm settings routes and the alarm sync hooks (backend/app.py).

``/api/settings/alarms`` is super-admin only, validates every alarm id, saves, audits
and syncs the CloudWatch alarms in the background; the test email goes through the
notifier and is audited; and the alarms follow a database's monitoring (configure,
cadence, pause) and its removal. Handlers are extracted from app.py by source and run
against fakes. Hermetic, no AWS, no FastAPI.

Run:  python3 backend/tests/test_alarm_routes.py
"""
import io
import json
import os
import pathlib
import re
import sys
import types
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.update(RESOURCE_PREFIX="acme", ALARM_EMAIL_RECIPIENT="ops@example.com",
                  NOTIFICATION_TOPIC_ARN="arn:topic", CW_METRICS_NAMESPACE="acme/Metrics")

from pydantic import BaseModel  # noqa: E402
from tools.aws import db_alarms  # noqa: E402

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
    m = re.search(rf"^(?:def|class) {re.escape(name)}[\s(:].*?(?=^(?:@|def |class |_[A-Z_]+ = |[A-Z_]+ = )|\Z)",
                  APP_SRC, re.S | re.M)
    assert m, f"{name} not found in app.py"
    return m.group(0)


class FakeHTTPException(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


class CW:
    def __init__(self):
        self.alarms, self.calls = {}, []

    def describe_alarms(self, AlarmNamePrefix="", StateValue=None, **kw):
        return {"MetricAlarms": [dict(a) for n, a in sorted(self.alarms.items()) if n.startswith(AlarmNamePrefix)
                                 and (StateValue is None or a.get("StateValue") == StateValue)]}

    def put_metric_alarm(self, **spec):
        self.calls.append(("put", spec["AlarmName"]))
        self.alarms[spec["AlarmName"]] = dict(spec)

    def delete_alarms(self, AlarmNames):
        self.calls.append(("delete", tuple(AlarmNames)))
        for n in AlarmNames:
            self.alarms.pop(n, None)


DB = {"instance_id": "db-1", "display_name": "orders", "status": "configured", "arn": "arn:aws:rds:x:1:db:orders"}


class Gov:
    def __init__(self, stored=None, fail=False):
        self.stored, self.fail, self.audits = stored, fail, []

    def get_alarm_settings(self):
        if self.fail:
            raise RuntimeError("ddb down")
        return self.stored

    def put_alarm_settings(self, enabled, alarms, actor):
        self.stored = {"config_key": "alarm_settings", "enabled": enabled, "alarms": alarms,
                       "updated_by": actor, "updated_at": "2026-10-07T09:00:00+00:00"}
        return self.stored

    def get_email_sender(self):
        return {"from_address": "alerts@example.com"}

    def audit(self, *a):
        self.audits.append(a)


class Lambda:
    def __init__(self, body, error=None):
        self.body, self.error, self.invocations = body, error, []

    def invoke(self, **kw):
        self.invocations.append(kw)
        out = {"Payload": io.BytesIO(json.dumps(self.body).encode())}
        if self.error:
            out["FunctionError"] = "Unhandled"
        return out


def build(admin=True, gov=None, records=(DB,), lam=None):
    cw, tasks = CW(), []
    gov = gov or Gov()
    rows = {r["instance_id"]: dict(r) for r in records}
    ns = {
        "HTTPException": FakeHTTPException, "Request": object, "BackgroundTasks": object, "BaseModel": BaseModel,
        "Optional": Optional, "os": os, "json": json,
        "is_admin": lambda a: admin, "get_user_id": lambda a: "super-1",
        "governance_registry": gov,
        "email_sender": types.SimpleNamespace(identity_status=lambda a: {"verified": True},
                                              account_status=lambda: {"sending_enabled": True, "production_access": True}),
        "db_registry": types.SimpleNamespace(list_instances=lambda: [dict(r) for r in rows.values()],
                                             get_instance=lambda i: dict(rows[i]) if i in rows else None),
        "_cloudwatch_client": lambda: cw,
        "logger": types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
        "audit_log": types.SimpleNamespace(detached_context=lambda: types.SimpleNamespace(run=lambda fn, *a, **k: fn(*a, **k))),
        "boto3": types.SimpleNamespace(client=lambda name, **k: lam),
    }
    for name in ("AlarmSettingsUpdate", "_alarm_settings_stored", "_alarm_delivery", "_alarm_settings_view",
                 "_sync_alarms", "get_alarm_settings", "set_alarm_settings", "send_test_alarm_email",
                 "_delete_db_alarms"):
        exec(_extract(name), ns)  # noqa: S102 - pinning shipped source
    bg = types.SimpleNamespace(add_task=lambda fn, *a, **k: tasks.append((fn, a, k)))
    return ns, cw, gov, bg, tasks


REQ = types.SimpleNamespace(headers={"Authorization": "Bearer x"})


def status(fn, *a, **k):
    try:
        return 200, fn(*a, **k)
    except FakeHTTPException as e:
        return e.status_code, e.detail


print("== the settings view ==")
ns, cw, gov, bg, tasks = build()
code, view = status(ns["get_alarm_settings"], REQ)
check(code == 200 and view["enabled"] is True and view["configured"] is False, "never set: on, the defaults")
check([a["id"] for a in view["alarms"]] == [a["id"] for a in db_alarms.CATALOGUE]
      and all(a["threshold"] and a["meaning"] for a in view["alarms"]), "every alarm with its threshold and meaning")
check(view["delivery"]["channel"] == "ses" and view["delivery"]["recipient"] == "ops@example.com",
      "where emails go, and how")
check(view["status"]["alarm_count"] == 0 and view["status"]["databases_monitored"] == 1, "the live alarm count")
ns, *_ = build(admin=False)
check(status(ns["get_alarm_settings"], REQ)[0] == 403, "super-admin only")
ns, *_ = build(gov=Gov(fail=True))
check(status(ns["get_alarm_settings"], REQ)[0] == 502, "a settings read failure is 502, never the defaults")

print("== saving ==")
ns, cw, gov, bg, tasks = build()
Upd = ns["AlarmSettingsUpdate"]
code, view = status(ns["set_alarm_settings"], Upd(alarms={"deadlocks": False}), REQ, bg)
check(code == 200 and gov.stored["alarms"]["deadlocks"] is False and gov.stored["enabled"] is True,
      "one alarm off; the rest keep their state")
check(gov.audits and gov.audits[-1][1] == "alarm_settings_set" and gov.audits[-1][2] == "deadlocks",
      "audited, naming what changed")
check(len(tasks) == 1 and tasks[0][1][0] is ns["_sync_alarms"], "the alarm sync runs in the background")
tasks[0][0](*tasks[0][1], **tasks[0][2])
names = {db_alarms.parse_alarm_name(n)[0] for n in cw.alarms}
check(names and "deadlocks" not in names and "xid_wraparound" in names, "the sync puts the enabled alarms only")
code, view = status(ns["set_alarm_settings"], Upd(enabled=False), REQ, bg)
check(gov.stored["enabled"] is False and gov.audits[-1][2] == "alarms off", "all alarms off, audited as such")
tasks[-1][0](*tasks[-1][1], **tasks[-1][2])
check(not cw.alarms, "and every database alarm is removed")
for bad, why in ((Upd(alarms={"bogus": True}), "an unknown alarm"), (Upd(alarms={"deadlocks": "yes"}), "a non-boolean"),
                 (Upd(), "an empty update")):
    check(status(ns["set_alarm_settings"], bad, REQ, bg)[0] == 400, f"{why} is 400")
ns, cw, gov, bg, tasks = build(admin=False)
check(status(ns["set_alarm_settings"], Upd(enabled=False), REQ, bg)[0] == 403 and gov.stored is None,
      "a non-super-admin changes nothing")

print("== the test email ==")
lam = Lambda({"sent_via": "ses"})
ns, cw, gov, bg, tasks = build(lam=lam)
code, out = status(ns["send_test_alarm_email"], REQ)
check(code == 200 and out == {"sent_via": "ses", "recipient": "ops@example.com"}, "sent, and how")
check(lam.invocations[0]["FunctionName"] == "acme-alarm-notifier"
      and json.loads(lam.invocations[0]["Payload"]) == {"test": True}, "through this install's notifier, as a test")
check(gov.audits[-1][1] == "alarm_test_sent", "audited")
ns, *_ = build(lam=Lambda({"sent_via": "none"}))
check(status(ns["send_test_alarm_email"], REQ)[0] == 409, "nothing to send to: 409 with the fix")
ns, *_ = build(lam=Lambda({"errorType": "KeyError"}, error=True))
code, detail = status(ns["send_test_alarm_email"], REQ)
check(code == 502 and "KeyError" not in detail, "a notifier failure is 502 without its internals")
ns, *_ = build(admin=False, lam=lam)
check(status(ns["send_test_alarm_email"], REQ)[0] == 403, "super-admin only")

print("== the alarms follow the database ==")
mon = re.search(r"^def _apply_monitoring_config\(.*?(?=^def )", APP_SRC, re.S | re.M).group(0)
check("_sync_alarms(only_instance=instance_id)" in mon, "a monitoring change (configure, cadence, pause) syncs that database")
dele = re.search(r"^def _delete_db_schedules\(.*?(?=^def )", APP_SRC, re.S | re.M).group(0)
tear = re.search(r"^def _teardown_database\(.*?(?=^def )", APP_SRC, re.S | re.M).group(0)
check("_delete_db_alarms(instance_id)" in dele and "_delete_db_alarms(instance_id)" in tear,
      "a database whose schedules go loses its alarms too")
ns, cw, *_ = build()
ns["_sync_alarms"]()
check(cw.alarms, "a full sync creates the alarms")
ns["_delete_db_alarms"]("db-1")
check(not cw.alarms, "removing the database removes them")
ns["db_registry"].get_instance = lambda i: None
check(ns["_sync_alarms"](only_instance="db-1")["errors"] == [], "a per-database sync of a gone record is clean")
check("def _start_alarm_sync_loop" in APP_SRC and 'ROUTE_REPAIR_LOOP' in _extract("_start_alarm_sync_loop"),
      "the periodic sync runs in the API task only")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
