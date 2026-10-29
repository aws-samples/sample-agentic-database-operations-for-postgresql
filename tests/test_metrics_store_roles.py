#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The metrics store separates the readers from the writers, by IAM.

The two record tables (``<prefix>-tier1-metrics``, ``<prefix>-tier2-scans``) carry
``instance_id`` fleet-wide. If the identity a scan container or the Tier 1 collector runs
as could READ them, a compromised one could enumerate every tenant's databases and their
scan history. So:

  * the Tier 1 collector WRITES the Tier 1 table (Put / BatchWrite) and reads only that
    table with Query (its previous sample per member, for the counter deltas);
  * the Batch scan WRITES the Tier 2 table only, and reads nothing;
  * both publish to CloudWatch only under this installation's namespace
    (``cloudwatch:namespace = <prefix>/Metrics``) and hold no GetMetricData;
  * the API task and the agent runtime READ both tables and the series, and write
    neither, so they cannot forge or erase collection history;
  * nobody holds Scan, Update or Delete on the store tables, or any Aurora grant.

And the tables themselves: encrypted, keyed by database, Tier 1 expiring by TTL, Tier 2
retained with point-in-time recovery and the sparse report index.

Reads the templates; no AWS.
"""
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


class _Loader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        v = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        v = loader.construct_sequence(node, deep=True)
    else:
        v = loader.construct_mapping(node, deep=True)
    return {"Fn::" + suffix: v}


_Loader.add_multi_constructor("!", _tag)


def load(rel):
    return yaml.load((ROOT / rel).read_text(encoding="utf-8"), Loader=_Loader)


def as_list(v):
    return v if isinstance(v, list) else [v]


def flat(v) -> str:
    """Every string inside a (tagged) value, joined: enough to see which table an ARN names."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return " ".join(flat(x) for x in v.values())
    if isinstance(v, list):
        return " ".join(flat(x) for x in v)
    return str(v)


def statements(role: dict):
    for pol in (role.get("Properties") or {}).get("Policies") or []:
        doc = pol.get("PolicyDocument") or {}
        for st in as_list(doc.get("Statement") or []):
            if isinstance(st, dict) and "Fn::If" in st:
                # A conditional statement: judge the branch that grants (the other is
                # AWS::NoValue).
                st = next((b for b in st["Fn::If"][1:] if isinstance(b, dict) and "Effect" in b), None)
                if st is None:
                    continue
            yield pol.get("PolicyName"), st


def grants(role: dict):
    """{'tier1': set(actions), 'tier2': set(actions), 'cw': [(action, condition)]}"""
    out = {"tier1": set(), "tier2": set(), "index": set(), "cw": [], "all_actions": set()}
    for _, st in statements(role):
        if st.get("Effect") != "Allow":
            continue
        actions = set(as_list(st.get("Action")))
        out["all_actions"] |= actions
        res = flat(st.get("Resource"))
        if "Tier1MetricsTable" in res:
            out["tier1"] |= actions
        if "Tier2ScansTable" in res:
            out["tier2"] |= actions
        if "Tier2ScansTable}/index" in res:
            out["index"] |= actions
        for a in actions:
            if a.startswith("cloudwatch:"):
                out["cw"].append((a, st.get("Condition"), res))
    return out


BACKEND = load("backend/template.yaml")["Resources"]
API = load("infrastructure/api.yaml")["Resources"]
STORAGE = load("infrastructure/storage.yaml")["Resources"]
READ = {"dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query"}
METRICS_NS = "${ResourcePrefix}/Metrics"


def namespaces(cw):
    """The cloudwatch:namespace values the PutMetricData statements are pinned to."""
    return {flat((c or {}).get("StringEquals", {}).get("cloudwatch:namespace")) for _, c, _ in cw}


def role_with_policy(resources, name):
    hits = []
    for rid, r in resources.items():
        if r.get("Type") == "AWS::IAM::Role" and any(p == name for p, _ in statements(r)):
            hits.append((rid, r))
    return hits


writers = {rid: grants(r) for rid, r in role_with_policy(BACKEND, "MetricsStoreWrite")}
readers = {rid: grants(r) for rid, r in role_with_policy(BACKEND, "MetricsStoreRead")}

print("== the writers ==")
check(len(writers) == 2, f"two writer roles (the Tier 1 collector and the Batch scan): {sorted(writers)}")
tier1_writer = next((g for g in writers.values() if g["tier1"]), None)
batch_writer = next((g for g in writers.values() if g["tier2"]), None)
check(tier1_writer is not None and batch_writer is not None and tier1_writer is not batch_writer,
      "one writes the Tier 1 table, the other the Tier 2 table")
if tier1_writer:
    check(tier1_writer["tier1"] == {"dynamodb:PutItem", "dynamodb:BatchWriteItem", "dynamodb:Query"},
          f"the collector: Put/BatchWrite/Query on Tier 1 only ({sorted(tier1_writer['tier1'])})")
    check(not tier1_writer["tier2"], "the collector holds nothing on the Tier 2 table")
if batch_writer:
    check(batch_writer["tier2"] == {"dynamodb:PutItem", "dynamodb:BatchWriteItem"},
          f"the scan: Put/BatchWrite on Tier 2, no read ({sorted(batch_writer['tier2'])})")
    check(not batch_writer["tier1"] and not batch_writer["index"], "the scan holds nothing on Tier 1 or the index")
for rid, g in writers.items():
    cw = g["cw"]
    check(cw and all(a == "cloudwatch:PutMetricData" for a, _, _ in cw),
          f"{rid}: its only CloudWatch action is PutMetricData (no GetMetricData)")
    check(METRICS_NS in namespaces(cw) and all(c for _, c, _ in cw),
          f"{rid}: PutMetricData is pinned to this installation's namespaces ({sorted(namespaces(cw))})")

print("== the readers ==")
api_role = API.get("ECSTaskRole") or next(r for r in API.values() if r.get("Type") == "AWS::IAM::Role"
                                         and grants(r)["tier1"])
read_roles = dict(readers, api_task=grants(api_role))
check(len(read_roles) == 2, f"two reader roles (the agent runtime and the API task): {sorted(read_roles)}")
for rid, g in read_roles.items():
    check(g["tier1"] == READ and g["tier2"] == READ, f"{rid}: Get/BatchGet/Query on both tables, nothing else")
    check(g["index"] == READ, f"{rid}: Query on the Tier 2 report index")
    cw_actions = {a for a, _, _ in g["cw"]}
    check("cloudwatch:GetMetricData" in cw_actions
          and METRICS_NS not in namespaces([x for x in g["cw"] if x[0] == "cloudwatch:PutMetricData"]),
          f"{rid}: reads the series, never publishes into the metrics namespace")

print("== nobody scans, updates or deletes the store; no Aurora grant survives ==")
roles = [r for res in (BACKEND, API) for r in res.values() if r.get("Type") == "AWS::IAM::Role"]
bad = set()
for r in roles:
    g = grants(r)
    bad |= {a for a in g["tier1"] | g["tier2"]
            if a in ("dynamodb:Scan", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:*")}
check(not bad, f"no Scan/Update/Delete/* on the store tables ({sorted(bad)})")
text = "".join((ROOT / p).read_text(encoding="utf-8") for p in
               ("backend/template.yaml", "infrastructure/api.yaml", "infrastructure/master.yaml"))
for gone in ("rds-db:connect", "rds-data:", "AuroraClusterArn", "AURORA_DB_USER", "AURORA_CLUSTER_ENDPOINT"):
    check(gone not in text, f"no {gone} left in the templates")
check(not (ROOT / "infrastructure/aurora-store.yaml").exists(), "the Aurora store template is gone")

print("== the tables ==")
t1, t2 = STORAGE["Tier1MetricsTable"]["Properties"], STORAGE["Tier2ScansTable"]["Properties"]
for name, p in (("tier1", t1), ("tier2", t2)):
    check((p.get("SSESpecification") or {}).get("SSEEnabled") is True, f"{name}: encrypted with KMS")
    check([k["AttributeName"] for k in p["KeySchema"]] == ["instance_id", "sk"], f"{name}: keyed by database")
    check(p.get("BillingMode") == "PAY_PER_REQUEST", f"{name}: on-demand")
check(t1.get("TimeToLiveSpecification") == {"AttributeName": "expires_at", "Enabled": True},
      "tier1: samples expire by TTL on expires_at")
check(STORAGE["Tier2ScansTable"].get("DeletionPolicy") == "Retain"
      and t2["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True,
      "tier2: retained, point-in-time recovery on")
gsi = {g["IndexName"]: g for g in t2.get("GlobalSecondaryIndexes") or []}
check("by-report-key" in gsi and gsi["by-report-key"]["KeySchema"][0]["AttributeName"] == "report_s3_key",
      "tier2: the sparse report index")

print("== the database alarms ==")
# The operational-settings writes (budget, alarms, the notifier invoke) live in the
# OperationalSettingsAccess managed policy attached to the API role, kept out of the
# inline AppAccess policy so it stays under IAM's 10,240-byte limit. Judge the role's
# inline statements and that managed policy's together.
ops_policy = API.get("OperationalSettingsAccess") or {}
ops_stmts = (((ops_policy.get("Properties") or {}).get("PolicyDocument") or {}).get("Statement")) or []
check(any(isinstance(r, dict) and r.get("Fn::Ref") == "ECSTaskRole"
          for r in (ops_policy.get("Properties") or {}).get("Roles") or []),
      "OperationalSettingsAccess managed policy is attached to the ECSTaskRole")
api_all = [st for _, st in statements(api_role)] + ops_stmts
api_stmts = [st for st in api_all if st.get("Effect") == "Allow"]
alarm_writes = [st for st in api_stmts
                if {"cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms"} & set(as_list(st.get("Action")))]
db_alarm_stmt = [st for st in alarm_writes if "cloudwatch:DeleteAlarms" in as_list(st.get("Action"))]
check(len(db_alarm_stmt) == 1 and flat(db_alarm_stmt[0]["Resource"])
      == "arn:aws:cloudwatch:${AWS::Region}:${AWS::AccountId}:alarm:${ResourcePrefix}-db-*",
      "the API puts and deletes alarms under this install's database-alarm prefix only")
check(all("*" not in flat(st["Resource"]).replace("-db-*", "") for st in alarm_writes),
      "no alarm write reaches any other alarm")
deny_actions = [st for st in api_all if st.get("Effect") == "Deny"
                and "cloudwatch:PutMetricAlarm" in as_list(st.get("Action"))]
check(len(deny_actions) == 1
      and as_list(deny_actions[0]["Action"]) == ["cloudwatch:PutMetricAlarm"]
      and flat(deny_actions[0]["Resource"]) == flat(db_alarm_stmt[0]["Resource"])
      and deny_actions[0]["Condition"] == {"ForAnyValue:StringLike": {"cloudwatch:AlarmActions": "*"}},
      "a database alarm with any action attached is refused (alarms only notify)")
invokes = [st for st in api_stmts if "lambda:InvokeFunction" in as_list(st.get("Action"))]
check(any(flat(st["Resource"]).endswith("function:${ResourcePrefix}-alarm-notifier") for st in invokes),
      "the API may invoke the notifier (the test email)")
notifier = BACKEND["AlarmNotifierRole"]
nst = [st for _, st in statements(notifier)]
acts = {a for st in nst for a in as_list(st.get("Action"))}
check(acts <= {"dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "ses:GetEmailIdentity",
               "ses:SendEmail", "ses:GetAccount", "sns:Publish"}
      and not any(a.startswith("cloudwatch:") for a in acts),
      f"the notifier reads records and settings and sends, nothing else ({sorted(acts)})")
writes = [st for st in nst if {"dynamodb:PutItem", "dynamodb:DeleteItem"} & set(as_list(st.get("Action")))]
check(writes and all(st["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"] == ["alarm_sent:*"]
                     for st in writes), "its only writes are its own send markers in the config table")
reads = [st for st in nst if "dynamodb:GetItem" in as_list(st.get("Action")) and "config" in flat(st["Resource"])]
check(reads and set(reads[0]["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"])
      == {"alarm_settings", "email_sender", "alarm_sent:*"}, "and it reads only the keys it needs")
rule = BACKEND["AlarmStateChangeRule"]["Properties"]["EventPattern"]
check(rule["source"] == ["aws.cloudwatch"] and rule["detail-type"] == ["CloudWatch Alarm State Change"]
      and flat(rule["detail"]["alarmName"]) == "${ResourcePrefix}-db-",
      "the EventBridge rule hears this install's database alarms only")
check(BACKEND["AlarmNotifierFunction"]["Properties"]["Handler"] == "alarm_notifier.handler", "the notifier's handler")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
