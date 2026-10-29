#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The admin audit settings in deployment/configure.py.

AUDIT_RETENTION_DAYS becomes the bucket's Object Lock default retention and the
lifecycle expiry, and S3 rejects a fractional day count mid-update, which would roll the
stack update back. So its validator must refuse a fraction up front.

AUDIT_VAULT is read back from the console switch on every deploy (a super admin owns it
after install), while AUDIT_RETENTION_DAYS deliberately is not: it is the floor, and
reading a console-raised value back would ratchet the floor up on every redeploy.

Hermetic: aws() is stubbed, so nothing here calls AWS.

    python3 tests/test_configure_audit.py      (from the repository root)
"""
import importlib.util
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


spec = importlib.util.spec_from_file_location("configure", REPO / "deployment/configure.py")
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)

print("══ whole_number_between(1, 3653) ══")
v = configure.whole_number_between(1, 3653)
for good in ("1", "365", "2555", "3653"):
    check(v(good) == "", f"accepts {good}")
for bad, why in (("1.5", "whole number"), ("0", "between"), ("3654", "between"),
                 ("abc", "number"), ("", "number"), ("-5", "between")):
    msg = v(bad)
    check(bool(msg) and why in msg, f"rejects {bad!r} ({msg!r})")

print("")
print("══ the two settings ══")
vault = configure.OPTIONAL_SETTINGS.get("AUDIT_VAULT") or {}
days = configure.OPTIONAL_SETTINGS.get("AUDIT_RETENTION_DAYS") or {}
check(vault.get("cfn") == "AuditVault" and vault.get("default") == "on"
      and vault.get("choices") == ["on", "off"], "AUDIT_VAULT -> AuditVault, default on, on / off")
check(vault["validator"]("on") == "" and vault["validator"]("off") == "" and vault["validator"]("yes"),
      "AUDIT_VAULT accepts only on / off")
check(vault.get("live_default") is configure.live_audit_vault,
      "AUDIT_VAULT is read back from the console switch")
check(days.get("cfn") == "AuditRetentionDays" and days.get("default") == "365",
      "AUDIT_RETENTION_DAYS -> AuditRetentionDays, default 365")
check(days["validator"]("1.5") and days["validator"]("365") == "",
      "AUDIT_RETENTION_DAYS uses the whole-number validator")
check("live_default" not in days, "AUDIT_RETENTION_DAYS has no live_default (it is the floor)")
check(days.get("note", "").endswith("see Indicative costs in the README"),
      "its note points to Indicative costs in the README")
port = configure.OPTIONAL_SETTINGS["DB_PORT_RANGE"]
check(port.get("cfn") == "DbPortRange" and port.get("default") == "5432-5432",
      "DB_PORT_RANGE -> DbPortRange, default 5432-5432")
for good in ("5432-5432", "5433-5440", "1-65535"):
    check(port["validator"](good) == "", f"DB_PORT_RANGE accepts {good}")
for bad in ("5440-5433", "0-10", "abc", "5432", "5432-70000", ""):
    check(bool(port["validator"](bad)), f"DB_PORT_RANGE refuses {bad!r}")
keys = list(configure.OPTIONAL_SETTINGS)
check(keys.index("DB_PORT_RANGE") < keys.index("SAME_REGION_CONNECTIVITY"),
      "DB_PORT_RANGE comes before SAME_REGION_CONNECTIVITY")
i = keys.index("SAME_REGION_CONNECTIVITY")
check(keys[i + 1:i + 3] == ["AUDIT_VAULT", "AUDIT_RETENTION_DAYS"],
      "both follow SAME_REGION_CONNECTIVITY")

print("")
print("══ live_audit_vault reads the console switch ══")
calls = []


def stub(answer, account="123456789012"):
    def fake_aws(*args):
        calls.append(args)
        if args[:2] == ("sts", "get-caller-identity"):
            return account
        if args[:2] == ("dynamodb", "get-item"):
            return answer
        return ""
    configure.aws = fake_aws


os.environ.pop("AWS_PROFILE", None)
values = {"RESOURCE_PREFIX": "acme", "AWS_REGION": "eu-west-1"}
for answer, want in (("True", "on"), ("False", "off"), ("None", ""), ("", "")):
    calls.clear()
    stub(answer)
    check(configure.live_audit_vault(values) == want, f"vault_enabled {answer or '(no item)'!r} -> {want!r}")
get = [c for c in calls if c[:2] == ("dynamodb", "get-item")]
check(len(get) == 1, "it makes one get-item")
if get:
    a = get[0]
    check(a[a.index("--table-name") + 1] == "acme-config", "on <prefix>-config")
    check(a[a.index("--key") + 1] == '{"config_key":{"S":"audit_settings"}}', "for the audit_settings item")
    check(a[a.index("--query") + 1] == "Item.vault_enabled.BOOL", "reading vault_enabled")
    check(a[a.index("--region") + 1] == "eu-west-1", "in the deployment's region")
calls.clear()
stub("True", account="")
check(configure.live_audit_vault(values) == "", "unauthenticated -> '' (the deploy keeps deploy.config's value)")
check(not any(c[:2] == ("dynamodb", "get-item") for c in calls), "and makes no get-item")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
