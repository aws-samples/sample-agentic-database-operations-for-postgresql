# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Onboarding runs network configuration in the background and the console polls the
database's status. The POST route persists ``status=configuring`` before scheduling the
work, so a worker lost before its terminal write (a Fargate replacement on deploy, or an
OOM mid-provision) stranded the record at ``configuring`` forever and the console spun
indefinitely.

Mirrors the discovery staleness fix (commit e675464): a single ``configuring_at`` stamp
plus a ``CONFIGURE_STALE_AFTER_S`` env threshold and a ``_configure_is_stale`` helper. The
admin read paths swap a stale ``configuring`` to ``config_failed`` so the console's
existing Failed branch clears the spinner and offers Onboard/Retry.

The read-path helpers are extracted from backend/app.py and run against dict records; no
strands, no network, no AWS.

Run:  python3 backend/tests/test_configure_background.py
"""
import pathlib
import re
import sys
from datetime import datetime, timedelta, timezone

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


def _iso(secs_ago):
    return (datetime.now(timezone.utc) - timedelta(seconds=secs_ago)).isoformat()


# Namespace with just what the extracted helpers reference. teardown_stalled is stubbed
# (never True here) so _teardown_reason exercises only the configure/onboard path.
ns = {
    "datetime": datetime, "timezone": timezone, "os": __import__("os"),
    "CONFIGURE_STALE_AFTER_S": 900,
    "_CONFIGURE_STALE_MESSAGE": "Configuration did not finish — the service may have restarted. Onboard it again.",
    "teardown_stalled": lambda record: False,
}
exec(_extract("_configure_is_stale"), ns)  # noqa: S102 - the staleness escape hatch
exec(_extract("_flag_stale_configuring"), ns)  # noqa: S102
exec(_extract("_teardown_reason"), ns)  # noqa: S102

_configure_is_stale = ns["_configure_is_stale"]
_flag_stale_configuring = ns["_flag_stale_configuring"]
_teardown_reason = ns["_teardown_reason"]

print("a configuring record with an OLD configuring_at reads as config_failed")
old = {"instance_id": "i-1", "status": "configuring", "configuring_at": _iso(1200)}
out = _flag_stale_configuring(old)
check(out["status"] == "config_failed", "a stale configuring is reclassified to config_failed")
check(bool(out.get("config_error")), "it carries a non-empty config_error message")
check(out.get("configure_stalled") is True, "it is flagged configure_stalled for the console")

print("a configuring record with a RECENT configuring_at still reads as configuring")
recent = {"instance_id": "i-2", "status": "configuring", "configuring_at": _iso(5)}
out = _flag_stale_configuring(recent)
check(out["status"] == "configuring" and "configure_stalled" not in out,
      "a recent configuring is returned unchanged — the happy path still polls")

print("re-onboarding a stale configuring record is permitted (not blocked)")
check(_teardown_reason({"status": "configuring"}) == "",
      "a configuring record is not blocked from re-onboard by _teardown_reason")

print("a configuring record with a missing configuring_at is treated as stale")
missing = {"instance_id": "i-3", "status": "configuring"}
check(_configure_is_stale(missing) is True, "an absent timestamp cannot prove liveness → stale")
out = _flag_stale_configuring(missing)
check(out["status"] == "config_failed" and out.get("configure_stalled") is True,
      "a configuring record with no timestamp is reclassified to config_failed")

print("a non-configuring record is never stale and is returned unchanged")
done = {"instance_id": "i-4", "status": "configured", "configuring_at": _iso(1200)}
check(_configure_is_stale(done) is False, "a terminal status is never stale")
check(_flag_stale_configuring(done) is done, "a non-configuring record passes through untouched")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
