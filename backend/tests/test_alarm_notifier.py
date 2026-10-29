# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The alarm notifier Lambda (backend/alarm_notifier.py).

A database alarm entering ALARM sends one well-formed email; its return to OK sends a
"resolved" one only after an alarm email went out; INSUFFICIENT_DATA sends nothing; a
flapping alarm is held to one email per window; switched-off alarms and removed
databases send nothing; every value from the record or the event is escaped in the
HTML; SES is used only when it can reach the recipient, and an SES failure falls back
to the topic with an ASCII subject.

Hermetic: boto3 clients and the config/instances tables are faked.

Run:  python3 backend/tests/test_alarm_notifier.py
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.update(RESOURCE_PREFIX="acme", ALARM_EMAIL_RECIPIENT="ops@example.com",
                  NOTIFICATION_TOPIC_ARN="arn:aws:sns:eu-west-1:1:acme-alerts",
                  OPERATOR_APP_URL="https://app.example.com", AWS_REGION_NAME="eu-west-1")

import alarm_notifier as an  # noqa: E402
from tools.aws import db_alarms  # noqa: E402

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


class Table:
    def __init__(self, items, key):
        self.items, self.key = dict(items), key

    def get_item(self, Key):
        it = self.items.get(Key[self.key])
        return {"Item": dict(it)} if it else {}

    def put_item(self, Item):
        self.items[Item[self.key]] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(Key[self.key], None)


class SES:
    def __init__(self, verified=("alerts@example.com",), production=True, fail=False):
        self.verified, self.production, self.fail, self.sent = set(verified), production, fail, []

    def get_email_identity(self, EmailIdentity):
        return {"VerifiedForSendingStatus": EmailIdentity in self.verified}

    def get_account(self):
        return {"SendingEnabled": True, "ProductionAccessEnabled": self.production}

    def send_email(self, **kw):
        if self.fail:
            raise RuntimeError("MessageRejected")
        self.sent.append(kw)


class SNS:
    def __init__(self):
        self.published = []

    def publish(self, **kw):
        self.published.append(kw)


IID = "11111111-aaaa"
RECORD = {"instance_id": IID, "display_name": "orders <prod> & co", "engine": "aurora-postgresql",
          "region": "eu-west-1"}


def world(settings=None, sender=None, ses=None, records=None):
    config = {}
    if settings is not None:
        config["alarm_settings"] = dict(settings, config_key="alarm_settings")
    config["email_sender"] = dict(sender if sender is not None else {"from_address": "alerts@example.com",
                                                                      "from_name": "DBOps"},
                                  config_key="email_sender")
    tables = {"config": Table(config, "config_key"),
              "instances": Table({IID: RECORD} if records is None else records, "instance_id")}
    an._table = lambda key: tables[key]
    clients = {"sesv2": ses or SES(), "sns": SNS()}
    an._client = lambda name: clients[name]
    return tables, clients


def event(name, new, old, value=52.3, threshold=46.57):
    return {"detail-type": "CloudWatch Alarm State Change", "time": "2026-10-07T09:05:00Z",
            "detail": {"alarmName": name,
                       "state": {"value": new, "timestamp": "2026-10-07T09:05:00.000+0000",
                                 "reasonData": json.dumps({"recentDatapoints": [value], "threshold": threshold})},
                       "previousState": {"value": old}}}


NAME = db_alarms.alarm_name("xid_wraparound", IID)

print("== an alarm email ==")
tables, clients = world()
out = an.handler(event(NAME, "ALARM", "OK"))
check(out == {"sent_via": "ses", "kind": "alarm"}, f"OK -> ALARM sends through SES ({out})")
(mail,) = clients["sesv2"].sent
html = mail["Content"]["Simple"]["Body"]["Html"]["Data"]
text = mail["Content"]["Simple"]["Body"]["Text"]["Data"]
subject = mail["Content"]["Simple"]["Subject"]["Data"]
check(mail["Destination"] == {"ToAddresses": ["ops@example.com"]} and mail["FromEmailAddress"] == "DBOps <alerts@example.com>",
      "to the notification address, from the configured sender")
check(subject == "[Alarm] orders <prod> & co: Transaction ID wraparound risk", f"subject ({subject})")
check("orders &lt;prod&gt; &amp; co" in html and "<prod>" not in html, "the database name is escaped in the HTML")
check("52.3% of the wraparound limit" in html and "46.57% of the wraparound limit" in html,
      "the current value and the threshold, as people read them")
check("Aurora PostgreSQL" in html and "eu-west-1" in html and "the writer" in html, "engine, region and series")
check("What to do" in html and "VACUUM (FREEZE)" in html and 'href="https://app.example.com"' in html,
      "what it means, what to do, and a link to the operator app")
check("Settings › Alarms" in html and "2026-10-07 09:05 UTC" in text, "how to switch it off; when it changed")
check(tables["config"].items.get(f"alarm_sent:{NAME}", {}).get("sent_at"), "the send is recorded (flap guard)")

print("== flapping, resolving and the transitions that send nothing ==")
check(an.handler(event(NAME, "ALARM", "OK"))["skipped"].startswith("flapping"), "a second alarm within the window is held")
out = an.handler(event(NAME, "OK", "ALARM", value=40.1))
check(out["kind"] == "resolved" and "[Resolved]" in clients["sesv2"].sent[-1]["Content"]["Simple"]["Subject"]["Data"],
      "ALARM -> OK sends a resolved email")
check(f"alarm_sent:{NAME}" not in tables["config"].items, "and clears the marker, so the next alarm sends")
check(an.handler(event(NAME, "OK", "ALARM"))["skipped"] == "no alarm email to resolve",
      "a resolve with no alarm email before it sends nothing")
for new, old in (("INSUFFICIENT_DATA", "OK"), ("OK", "INSUFFICIENT_DATA"), ("ALARM", "ALARM")):
    check("skipped" in an.handler(event(NAME, new, old)), f"{old} -> {new} sends nothing")
tables["config"].items[f"alarm_sent:{NAME}"] = {
    "config_key": f"alarm_sent:{NAME}", "sent_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()}
check(an.handler(event(NAME, "ALARM", "OK")).get("sent_via") == "ses", "after the window, a new alarm sends again")

print("== what is never emailed ==")
world(settings={"enabled": False})
check(an.handler(event(NAME, "ALARM", "OK")) == {"skipped": "switched off"}, "alarms off: nothing")
world(settings={"alarms": {"xid_wraparound": False}})
check(an.handler(event(NAME, "ALARM", "OK")) == {"skipped": "switched off"}, "this alarm off: nothing")
world(records={})
check(an.handler(event(NAME, "ALARM", "OK")) == {"skipped": "database not found"}, "a removed database: nothing")
world()
for bad in ("acme-guardrail-interventions-high", "other-db-xid_wraparound-x", ""):
    check(an.handler(event(bad, "ALARM", "OK")) == {"skipped": "not a database alarm"}, f"not ours: {bad!r}")

print("== delivery ==")
tables, clients = world(ses=SES(production=False))
check(an.handler(event(NAME, "ALARM", "OK"))["sent_via"] == "sns", "SES sandbox, recipient unverified: the topic")
(pub,) = clients["sns"].published
check(pub["TopicArn"] == os.environ["NOTIFICATION_TOPIC_ARN"] and pub["Subject"].isascii()
      and len(pub["Subject"]) <= 100 and "\n" not in pub["Subject"], f"an ASCII one-line subject ({pub['Subject']})")
check("52.3% of the wraparound limit" in pub["Message"] and "What to do" in pub["Message"], "the plain-text body")
tables, clients = world(ses=SES(verified=("alerts@example.com", "ops@example.com"), production=False))
check(an.handler(event(NAME, "ALARM", "OK"))["sent_via"] == "ses", "sandbox with a verified recipient: SES")
tables, clients = world(ses=SES(fail=True))
check(an.handler(event(NAME, "ALARM", "OK"))["sent_via"] == "sns" and clients["sns"].published,
      "an SES failure falls back to the topic")
tables, clients = world(sender={})
check(an.handler(event(NAME, "ALARM", "OK"))["sent_via"] == "sns", "no sender set up: the topic")

print("== every alarm renders ==")
for a in db_alarms.CATALOGUE:
    msg = an.build_message("alarm", a, RECORD, 3, db_alarms.thresholds()[a["id"]], "2026-10-07 09:05 UTC")
    check(a["label"] in msg["html"] and msg["text"] and "<script" not in msg["html"], f"{a['id']}")
check(an.format_value("long_transaction", 4000) == "open for 1 h 6 min" and an.format_value("replica_lag", 12400)
      == "12.4 s behind" and an.format_value("deadlocks", 1) == "1 deadlock in the interval",
      "values per metric")
check(an.format_value("cache_hit_ratio", None) == "no recent value", "no value: said so")

print("== the test email ==")
tables, clients = world()
check(an.handler({"test": True}) == {"sent_via": "ses"}, "a test email goes the way a real alarm would")
check("[Test]" in clients["sesv2"].sent[-1]["Content"]["Simple"]["Subject"]["Data"]
      and not tables["config"].items.get("alarm_sent:"), "marked as a test, and no flap marker")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
