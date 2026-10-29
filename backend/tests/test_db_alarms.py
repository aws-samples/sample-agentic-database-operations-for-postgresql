# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Database alarms (tools.aws.db_alarms): what exists, what each reads, and the sync.

The thresholds are the references.yaml bands the findings use; every alarm is built by
the server from a registry record (no caller-supplied query, dimension, threshold or
action); a paused, unconfigured or removed database has none; the sync only ever touches
this install's ``<prefix>-db-`` alarms; and delivery picks SES only when it can reach the
recipient.

Run:  python3 backend/tests/test_db_alarms.py
"""
import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["RESOURCE_PREFIX"] = "acme"

from tools.aws import db_alarms as da  # noqa: E402
from tools.xid import XID_WRAPAROUND_MAX  # noqa: E402

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


NS = "acme/Metrics"
CLUSTER = {"instance_id": "11111111-aaaa", "display_name": "orders", "status": "configured",
           "tier1_frequency": "10min",
           "cluster_member_roles": [{"instance_id": "orders-1", "is_writer": True},
                                    {"instance_id": "orders-2", "is_writer": False}]}
WRITER_ONLY = {"instance_id": "22222222-bbbb", "status": "configured",
               "cluster_member_roles": [{"instance_id": "solo-1", "is_writer": True}]}
SINGLE = {"instance_id": "33333333-cccc", "display_name": "billing", "status": "configured",
          "arn": "arn:aws:rds:eu-west-1:1:db:billing"}
REPLICA = {"instance_id": "44444444-dddd", "status": "configured",
           "tags": {"arn": "arn:aws:rds:eu-west-1:1:db:billing-ro", "replica_of": "billing"}}
PAUSED = dict(SINGLE, instance_id="55555555-eeee", tier1_enabled=False)
CONFIGURING = dict(SINGLE, instance_id="66666666-ffff", status="configuring")

print("== the catalogue and its thresholds ==")
t = da.thresholds()
check(set(t) == set(da.BY_ID), "every alarm has a threshold")
check(t["xid_wraparound"] == round(100 * 1_000_000_000 / XID_WRAPAROUND_MAX, 2),
      f"XID: the references' absolute warning floor (1B) on the shared denominator ({t['xid_wraparound']}%)")
check(t["long_transaction"] == 1800 and t["replica_lag"] == 5000 and t["cache_hit_ratio"] == 95
      and t["rollback_ratio"] == 10 and t["deadlocks"] == 0,
      "long transaction 30 min, replica lag 5 s, cache < 95%, rollback 10%, any deadlock: the references' bands")
check([a["id"] for a in da.CATALOGUE if not a["default"]] == ["rollback_ratio"], "only rollback ratio is off by default")
check(all(a["severity"] in da.SEVERITIES and a["meaning"] and a["next_steps"] for a in da.CATALOGUE),
      "every alarm says what it means and what to do")
from tools.aws.metrics_store import TIER1_NUMERIC_METRICS  # noqa: E402
check(all(a["metric"] in TIER1_NUMERIC_METRICS or a["metric"] == "collection_failed" for a in da.CATALOGUE),
      "every alarm reads a metric the collector publishes")
check("billion" in da.threshold_text("xid_wraparound", t["xid_wraparound"]), "thresholds read as people say them")

print("== settings ==")
s0 = da.resolve_settings(None)
check(s0["enabled"] is True and s0["alarms"]["xid_wraparound"] and not s0["alarms"]["rollback_ratio"],
      "never set: alarms on, each at its default")
s1 = da.resolve_settings({"enabled": False, "alarms": {"rollback_ratio": True, "bogus": True}})
check(s1["enabled"] is False and s1["alarms"]["rollback_ratio"] and "bogus" not in s1["alarms"],
      "a stored choice wins; an unknown key is ignored")
check(da.resolve_settings(s1) == s1, "resolving twice changes nothing")

print("== naming ==")
n = da.alarm_name("connection_saturation", CLUSTER["instance_id"])
check(n == "acme-db-connection_saturation-11111111-aaaa", "<prefix>-db-<alarm>-<instance>")
check(da.parse_alarm_name(n) == ("connection_saturation", "11111111-aaaa"), "parsed back")
for other in ("acme-guardrail-interventions-high", "other-db-xid_wraparound-x", "acme-db-bogus-x", "acme-db-deadlocks-"):
    check(da.parse_alarm_name(other) is None, f"not ours: {other}")

print("== desired alarms ==")
want = da.desired_alarms([CLUSTER, WRITER_ONLY, SINGLE, REPLICA, PAUSED, CONFIGURING], None, t, NS)
by = {}
for name, spec in want.items():
    by.setdefault(da.parse_alarm_name(name)[1], {})[da.parse_alarm_name(name)[0]] = spec
check(set(by) == {CLUSTER["instance_id"], WRITER_ONLY["instance_id"], SINGLE["instance_id"], REPLICA["instance_id"]},
      "a paused or not-yet-configured database has no alarms")
on_by_default = {a["id"] for a in da.CATALOGUE if a["default"]}
check(set(by[CLUSTER["instance_id"]]) == on_by_default, "a cluster with a reader: every default alarm")
check("replica_lag" not in by[WRITER_ONLY["instance_id"]], "a cluster with no reader has no replica-lag alarm")
check("replica_lag" not in by[SINGLE["instance_id"]] and "replica_lag" in by[REPLICA["instance_id"]],
      "a single instance has no reader alarm; a standalone read replica has its own")
c = by[CLUSTER["instance_id"]]
dims = lambda spec: {d["Name"]: d["Value"] for d in spec["Dimensions"]}  # noqa: E731
check(dims(c["xid_wraparound"]) == {"InstanceId": CLUSTER["instance_id"], "Role": "writer"},
      "a cluster's writer alarms read the Role=writer series (continuous across a failover)")
check(dims(c["replica_lag"]) == {"InstanceId": CLUSTER["instance_id"], "Role": "reader"}
      and c["replica_lag"]["Statistic"] == "Maximum", "replica lag reads the worst reader")
check(dims(c["collection_failed"]) == {"InstanceId": CLUSTER["instance_id"]}, "collection status is database-level")
check(dims(by[SINGLE["instance_id"]]["cache_hit_ratio"]) == {"InstanceId": SINGLE["instance_id"], "Member": "billing"},
      "a single instance reads its own member series")
check(dims(by[REPLICA["instance_id"]]["replica_lag"]) == {"InstanceId": REPLICA["instance_id"], "Member": "billing-ro"},
      "a read replica's lag is its own")
check(c["xid_wraparound"]["Period"] == 600 and by[SINGLE["instance_id"]]["xid_wraparound"]["Period"] == 300,
      "one period is one Tier 1 interval (10 min here, 5 min by default)")
check(all(s["Namespace"] == NS and s["TreatMissingData"] == "notBreaching" for s in want.values()),
      "this install's namespace; a gap never alarms")
check(all("AlarmActions" not in s and "OKActions" not in s and "InsufficientDataActions" not in s for s in want.values()),
      "no alarm carries an action: the notifier hears state changes through EventBridge only")
check("orders" in c["deadlocks"]["AlarmDescription"] and "Settings › Alarms" in c["deadlocks"]["AlarmDescription"],
      "the description names the database and where to switch it")
check(c["cache_hit_ratio"]["ComparisonOperator"] == "LessThanThreshold" and c["cache_hit_ratio"]["Threshold"] == 95.0,
      "cache hit alarms below the band")
off = da.desired_alarms([CLUSTER], {"enabled": False}, t, NS)
check(off == {}, "alarms off: none")
some = da.desired_alarms([CLUSTER], {"alarms": {"deadlocks": False, "rollback_ratio": True}}, t, NS)
ids = {da.parse_alarm_name(k)[0] for k in some}
check("deadlocks" not in ids and "rollback_ratio" in ids, "single alarms switch on and off")


class FakeCW:
    def __init__(self, alarms=()):
        self.alarms = {a["AlarmName"]: copy.deepcopy(a) for a in alarms}
        self.puts, self.deletes = [], []

    def describe_alarms(self, AlarmNamePrefix="", AlarmTypes=None, MaxRecords=100, NextToken=None, StateValue=None):
        names = sorted(n for n in self.alarms if n.startswith(AlarmNamePrefix)
                       and (StateValue is None or self.alarms[n].get("StateValue") == StateValue))
        start = int(NextToken or 0)
        page = names[start:start + 2]       # small pages: the caller must follow NextToken
        out = {"MetricAlarms": [copy.deepcopy(self.alarms[n]) for n in page]}
        if start + 2 < len(names):
            out["NextToken"] = str(start + 2)
        return out

    def put_metric_alarm(self, **spec):
        self.puts.append(spec["AlarmName"])
        self.alarms[spec["AlarmName"]] = copy.deepcopy(spec)

    def delete_alarms(self, AlarmNames):
        assert len(AlarmNames) <= 100
        self.deletes.extend(AlarmNames)
        for n in AlarmNames:
            self.alarms.pop(n, None)


print("== reconcile ==")
foreign = {"AlarmName": "acme-guardrail-interventions-high", "MetricName": "x"}
other_install = {"AlarmName": "beta-db-deadlocks-11111111-aaaa", "MetricName": "x"}
stale = {"AlarmName": da.alarm_name("deadlocks", "99999999-gone"), "MetricName": "deadlocks_delta"}
cw = FakeCW([foreign, other_install, stale])
out = da.reconcile(cw, [CLUSTER, SINGLE], None, t, NS)
check(out["put"] == len(da.desired_alarms([CLUSTER, SINGLE], None, t, NS)) and out["errors"] == [],
      f"every desired alarm is put ({out})")
check(cw.deletes == [stale["AlarmName"]], "a removed database's alarm is deleted")
check(foreign["AlarmName"] in cw.alarms and other_install["AlarmName"] in cw.alarms,
      "nothing outside this install's database-alarm prefix is touched")
out = da.reconcile(cw, [CLUSTER, SINGLE], None, t, NS)
check(out["put"] == 0 and out["deleted"] == 0 and out["unchanged"] > 0, "a second pass changes nothing")
out = da.reconcile(cw, [dict(CLUSTER, tier1_frequency="30min"), SINGLE], None, t, NS)
check(out["put"] == len(da.desired_alarms([CLUSTER], None, t, NS)) and out["deleted"] == 0,
      "a cadence change re-puts that database's alarms with the new period")
out = da.reconcile(cw, [CLUSTER, SINGLE], {"alarms": {"deadlocks": False}}, t, NS)
check(out["deleted"] == 2 and not any(n.startswith("acme-db-deadlocks-") for n in cw.alarms),
      "switching one alarm off deletes it on every database")
out = da.reconcile(cw, [CLUSTER, dict(SINGLE, tier1_enabled=False)], None, t, NS, only_instance=SINGLE["instance_id"])
check(out["deleted"] > 0 and not any(n.endswith(SINGLE["instance_id"]) for n in cw.alarms)
      and any(n.endswith(CLUSTER["instance_id"]) for n in cw.alarms),
      "a per-database pass touches that database only (pausing Tier 1 removes its alarms)")
out = da.reconcile(cw, [CLUSTER], {"enabled": False}, t, NS)
check(not any(da.parse_alarm_name(n) for n in cw.alarms), "alarms off: every database alarm is deleted")
cw = FakeCW()
cw.put_metric_alarm = lambda **spec: (_ for _ in ()).throw(RuntimeError("throttled"))
out = da.reconcile(cw, [CLUSTER], None, t, NS)
check(len(out["errors"]) == len(da.desired_alarms([CLUSTER], None, t, NS)) and out["put"] == 0,
      "a failing put is reported per alarm and does not stop the rest")
cw = FakeCW()
da.reconcile(cw, [CLUSTER, SINGLE], None, t, NS)
check(da.delete_for_instance(cw, CLUSTER["instance_id"]) > 0
      and not any(n.endswith(CLUSTER["instance_id"]) for n in cw.alarms)
      and any(n.endswith(SINGLE["instance_id"]) for n in cw.alarms), "a removed database takes only its own alarms")
cw.alarms[da.alarm_name("xid_wraparound", SINGLE["instance_id"])]["StateValue"] = "ALARM"
f = da.firing(cw)
check([(x["alarm_id"], x["instance_id"]) for x in f] == [("xid_wraparound", SINGLE["instance_id"])],
      "the alarms firing now")

print("== delivery ==")
SENDER = {"from_address": "alerts@example.com"}
ch = lambda **k: da.choose_channel(**{**dict(sender=SENDER, sender_verified=True, sending_enabled=True,  # noqa: E731
                                            production=True, recipient="ops@example.com",
                                            recipient_verified=False, topic_arn="arn:topic"), **k})
check(ch()["channel"] == "ses", "a verified sender with production access: SES HTML")
check(ch(production=False)["channel"] == "sns" and "sandbox" in ch(production=False)["reason"],
      "sandbox and an unverified recipient: the topic, saying why")
check(ch(production=False, recipient_verified=True)["channel"] == "ses", "sandbox with a verified recipient: SES")
check(ch(sender={})["channel"] == "sns" and "Settings › Email" in ch(sender={})["reason"], "no sender: the topic")
check(ch(sender_verified=False)["channel"] == "sns", "an unverified sender: the topic")
check(ch(recipient="")["channel"] == "sns" and "NOTIFICATION_EMAIL" in ch(recipient="")["reason"], "no recipient")
check(ch(sender={}, topic_arn="")["channel"] == "none", "nothing to send through: none")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
