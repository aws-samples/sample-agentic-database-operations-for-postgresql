# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Reconfigure network changes the network, never the monitoring an admin chose.

Every successful configure ends in ``_auto_schedule_on_configure``: the first onboard, the
"Reconfigure network" icon on a database row and batch reconfigure. It used to apply the
defaults every time (Tier 1 every 5 minutes, Tier 2 daily, both on), so one click reset a
chosen cadence and switched a deliberately paused Tier 1 back on, and the record was
rewritten to match, so nothing showed that anything had changed. A narrower variant lived
in ``_apply_monitoring_config``: a save that sent only ``tier1_enabled`` scheduled the
default cadence while the stored label kept the old one.

Pinned, through the real functions (extracted from backend/app.py) with a fake
EventBridge Scheduler and registry:
  * reconfigure of a database at Tier 1 15min (paused) and Tier 2 weekly: Tier 1 stays
    paused (no schedule created), Tier 2 keeps its weekly expression, stored fields unchanged;
  * reconfigure of a database at Tier 1 10min (on) re-creates a missing schedule at 10 min;
  * a first onboard (no cadence stored) still gets the defaults;
  * switching Tier 1 on with no frequency runs the stored cadence, not 5 minutes.

Hermetic, no AWS, no FastAPI.
Run:  python3 backend/tests/test_reconfigure_keeps_cadence.py
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP_SRC = (ROOT / "backend/app.py").read_text(encoding="utf-8")
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
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^(?:@|def |class |[A-Z_]+ = )|\Z)", APP_SRC, re.S | re.M)
    assert m, f"{name} not found in app.py"
    return m.group(0)


class NotFound(Exception):
    pass


class FakeScheduler:
    def __init__(self, existing=(), fail_ops=()):
        self.schedules = {n: {} for n in existing}
        self.calls = []
        # Schedule names whose create/update/delete must raise, to model an IAM or
        # service error on one tier.
        self.fail_ops = set(fail_ops)
        self.exceptions = type("E", (), {"ResourceNotFoundException": NotFound})

    def get_schedule(self, Name):
        if Name not in self.schedules:
            raise NotFound(Name)
        return self.schedules[Name]

    def create_schedule(self, **kw):
        self.calls.append(("create", kw["Name"], kw["ScheduleExpression"]))
        if ("create", kw["Name"]) in self.fail_ops:
            raise RuntimeError("AccessDeniedException: not authorized to CreateSchedule")
        self.schedules[kw["Name"]] = kw

    def update_schedule(self, **kw):
        self.calls.append(("update", kw["Name"], kw["ScheduleExpression"]))
        if ("update", kw["Name"]) in self.fail_ops:
            raise RuntimeError("AccessDeniedException: not authorized to UpdateSchedule")
        self.schedules[kw["Name"]] = kw

    def delete_schedule(self, Name):
        self.calls.append(("delete", Name, ""))
        if ("delete", Name) in self.fail_ops:
            raise RuntimeError("AccessDeniedException: not authorized to DeleteSchedule")
        if Name not in self.schedules:
            raise NotFound(Name)
        del self.schedules[Name]


class FakeRegistry:
    def __init__(self, rows):
        self.rows = {r["instance_id"]: dict(r) for r in rows}

    def get_instance(self, iid):
        return dict(self.rows[iid]) if iid in self.rows else None

    def update_instance(self, iid, updates):
        self.rows[iid].update(updates)


def build(rows, existing=(), fail_ops=()):
    scheduler = FakeScheduler(existing, fail_ops)
    registry = FakeRegistry(rows)
    ns = {"os": __import__("os"), "json": json, "db_registry": registry,
          "_redact_identifiers": lambda s: s,
          "_get_scheduler_client": lambda: scheduler,
          "_alarm_syncs": []}
    ns["_sync_alarms"] = lambda only_instance=None: ns["_alarm_syncs"].append(only_instance)
    exec(re.search(r"^TIER1_FREQUENCIES = .*?$", APP_SRC, re.M).group(0), ns)  # noqa: S102
    exec(re.search(r"^TIER1_RUN_TIMEOUT_S = .*?$", APP_SRC, re.M).group(0), ns)  # noqa: S102
    exec(re.search(r"^TIER1_MIN_FREQUENCY = .*?$", APP_SRC, re.M).group(0), ns)  # noqa: S102
    exec(re.search(r"^TIER2_FREQUENCIES = \{.*?\}", APP_SRC, re.S | re.M).group(0), ns)  # noqa: S102
    for fn in ("_schedule_name", "_tier1_schedule_name", "_effective_tier1_frequency",
               "_apply_monitoring_config", "_auto_schedule_on_configure"):
        exec(_extract(fn), ns)  # noqa: S102
    return ns, scheduler, registry


import os  # noqa: E402
os.environ.update({"TIER1_COLLECTOR_LAMBDA_ARN": "arn:lambda:t1", "TIER1_SCHEDULER_ROLE_ARN": "arn:role:t1",
                   "SCAN_TRIGGER_LAMBDA_ARN": "arn:lambda:t2", "SCHEDULER_ROLE_ARN": "arn:role:t2"})

T1, T2 = "db-tier1-db-paused", "db-scan-db-paused"

print("== reconfigure keeps a paused Tier 1 paused and a chosen Tier 2 cadence ==")
PAUSED = {"instance_id": "db-paused", "display_name": "orders", "tier1_frequency": "15min",
          "tier1_enabled": False, "tier2_frequency": "weekly", "tier2_enabled": True}
ns, sched, reg = build([PAUSED], existing=[T2])
ns["_auto_schedule_on_configure"]("db-paused", "orders")
check(not any(c[0] in ("create", "update") and c[1] == T1 for c in sched.calls),
      f"no Tier 1 schedule is created or updated ({sched.calls})")
check(T1 not in sched.schedules, "Tier 1 stays without a schedule (paused)")
check(sched.schedules[T2]["ScheduleExpression"] == "cron(0 6 ? * MON *)",
      "Tier 2 keeps its weekly expression")
row = reg.rows["db-paused"]
check((row["tier1_frequency"], row["tier1_enabled"], row["tier2_frequency"], row["tier2_enabled"])
      == ("15min", False, "weekly", True), f"stored cadence unchanged ({row})")

print("")
print("== reconfigure re-creates a missing schedule at the stored cadence ==")
ON = {"instance_id": "db-on", "display_name": "inv", "tier1_frequency": "10min", "tier1_enabled": True,
      "tier2_frequency": "6h", "tier2_enabled": False}
ns, sched, reg = build([ON])
ns["_auto_schedule_on_configure"]("db-on", "inv")
check(sched.schedules.get("db-tier1-db-on", {}).get("ScheduleExpression") == "rate(10 minutes)",
      "Tier 1 is created at 10 minutes, not 5")
check("db-scan-db-on" not in sched.schedules, "a paused Tier 2 is not switched on")
check(reg.rows["db-on"]["tier1_frequency"] == "10min" and reg.rows["db-on"]["tier2_enabled"] is False,
      "stored cadence unchanged")

print("")
print("== a first onboard still gets the defaults ==")
NEW = {"instance_id": "db-new", "display_name": "new"}
ns, sched, reg = build([NEW])
ns["_auto_schedule_on_configure"]("db-new", "new")
check(sched.schedules.get("db-tier1-db-new", {}).get("ScheduleExpression") == "rate(5 minutes)",
      "Tier 1 every 5 minutes")
check(sched.schedules.get("db-scan-db-new", {}).get("ScheduleExpression") == "cron(0 6 * * ? *)",
      "Tier 2 daily")
row = reg.rows["db-new"]
check((row.get("tier1_frequency"), row.get("tier1_enabled"), row.get("tier2_frequency"), row.get("tier2_enabled"))
      == ("5min", True, "daily", True), "the defaults are stored")

print("")
print("== switching a tier on without a frequency runs the stored cadence ==")
ns, sched, reg = build([dict(PAUSED)])
ns["_apply_monitoring_config"]("db-paused", {"tier1_enabled": True}, "orders")
check(sched.schedules.get(T1, {}).get("ScheduleExpression") == "rate(15 minutes)",
      "Tier 1 resumes at its stored 15 minutes, not the 5-minute default")
check(reg.rows["db-paused"]["tier1_frequency"] == "15min", "the stored label still matches the schedule")
ns["_apply_monitoring_config"]("db-paused", {"tier2_enabled": True}, "orders")
check(sched.schedules.get(T2, {}).get("ScheduleExpression") == "cron(0 6 ? * MON *)",
      "Tier 2 resumes weekly, not daily")

print("")
print("== a scheduler failure on one tier does not store that tier (#7) ==")
# A mixed update: Tier 1 to 10min (its update will fail), Tier 2 to 6h (succeeds).
MIX = {"instance_id": "db-mix", "display_name": "mix", "tier1_frequency": "5min", "tier1_enabled": True,
       "tier2_frequency": "daily", "tier2_enabled": True}
ns, sched, reg = build([dict(MIX)], existing=["db-tier1-db-mix", "db-scan-db-mix"],
                       fail_ops=[("update", "db-tier1-db-mix")])
out = ns["_apply_monitoring_config"]("db-mix", {"tier1_frequency": "10min", "tier2_frequency": "6h"}, "mix")
check(out["failed"] and "tier1" in out["failed"] and "tier2" not in out["failed"],
      f"only Tier 1 is reported failed ({out['failed']})")
check("AccessDenied" in out["failed"]["tier1"], "the failed tier carries the reason")
check(reg.rows["db-mix"]["tier1_frequency"] == "5min", "the failed Tier 1 keeps its stored cadence")
check(reg.rows["db-mix"]["tier2_frequency"] == "6h", "the succeeded Tier 2 is stored")
check(out["applied"] == {"tier2_frequency": "6h"}, f"only the succeeded tier is in applied ({out['applied']})")

print("")
print("== the Tier 2 schedule carries a retry policy and a DLQ (#11) ==")
os.environ["SCAN_SCHEDULER_DLQ_ARN"] = "arn:aws:sqs:us-east-1:111111111111:dbops-scan-trigger-dlq"
ns, sched, reg = build([dict(ON)])
ns["_apply_monitoring_config"]("db-on", {"tier2_frequency": "6h", "tier2_enabled": True}, "inv")
t2 = sched.schedules["db-scan-db-on"]["Target"]
check(t2.get("RetryPolicy", {}).get("MaximumRetryAttempts", 0) >= 1, "the scan target retries on failure")
check(t2.get("DeadLetterConfig", {}).get("Arn", "").endswith("scan-trigger-dlq"),
      "the scan target has the DLQ configured")
os.environ.pop("SCAN_SCHEDULER_DLQ_ARN")
ns, sched, reg = build([dict(ON)])
ns["_apply_monitoring_config"]("db-on", {"tier2_frequency": "6h", "tier2_enabled": True}, "inv")
t2 = sched.schedules["db-scan-db-on"]["Target"]
check("RetryPolicy" in t2 and "DeadLetterConfig" not in t2,
      "the retry policy still applies when no DLQ arn is configured")

print("")
print("== the template has the DLQ, the alarm, the role grant, and the Lambda raises (#11) ==")
import pathlib as _pl  # noqa: E402
TPL = (_pl.Path(__file__).resolve().parents[2] / "backend/template.yaml").read_text(encoding="utf-8")
check("ScanTriggerDLQ:" in TPL and "${ResourcePrefix}-scan-trigger-dlq" in TPL, "a prefix-named DLQ exists")
check("SqsManagedSseEnabled: true" in TPL.split("ScanTriggerDLQ:", 1)[1][:300], "the DLQ is encrypted")
check("MessageRetentionPeriod: 1209600" in TPL.split("ScanTriggerDLQ:", 1)[1][:300], "retained 14 days")
check("ScanTriggerDLQAlarm:" in TPL and "scan-trigger-dlq-not-empty" in TPL, "a depth alarm exists")
sched_role = TPL.split("SchedulerRole:", 1)[1].split("# Per-database schedules", 1)[0]
check("sqs:SendMessage" in sched_role and "ScanTriggerDLQ.Arn" in sched_role,
      "the scheduler role may deliver to the DLQ")
scan_fn = TPL.split("Failed to submit job for", 1)[1][:200]
check("raise" in scan_fn and "'submitted': 0, 'error'" not in scan_fn,
      "the scan trigger raises on a submit failure instead of returning success")
check("ScanTriggerDLQArn:" in TPL, "the DLQ arn is exported")
API = (_pl.Path(__file__).resolve().parents[2] / "infrastructure/api.yaml").read_text(encoding="utf-8")
check("SCAN_SCHEDULER_DLQ_ARN" in API and "ScanSchedulerDlqArn" in API, "the DLQ arn reaches the API task")
MASTER = (_pl.Path(__file__).resolve().parents[2] / "infrastructure/master.yaml").read_text(encoding="utf-8")
check("ScanSchedulerDlqArn: !GetAtt AgentStack.Outputs.ScanTriggerDLQArn" in MASTER,
      "master wires the DLQ arn from the agent stack to the API stack")

print("")
print("== a pause (delete) that fails does not store the pause ==")
ns, sched, reg = build([dict(MIX)], existing=["db-tier1-db-mix", "db-scan-db-mix"],
                       fail_ops=[("delete", "db-tier1-db-mix")])
out = ns["_apply_monitoring_config"]("db-mix", {"tier1_enabled": False}, "mix")
check("tier1" in out["failed"] and reg.rows["db-mix"]["tier1_enabled"] is True,
      "a failed pause keeps Tier 1 enabled in the record")
check("db-tier1-db-mix" in sched.schedules, "the schedule is still there (the pause did not take)")

print("")
print("== a monitoring save re-syncs that database's alarms ==")
ns, sched, reg = build([dict(MIX)], existing=["db-tier1-db-mix", "db-scan-db-mix"])
ns["_apply_monitoring_config"]("db-mix", {"tier1_enabled": False}, "mix")
check(ns["_alarm_syncs"] == ["db-mix"],
      "pausing Tier 1 re-syncs the alarms for that database only")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
