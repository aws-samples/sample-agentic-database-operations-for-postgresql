#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The Tier 1 collector's role grants what the member sync, the host-route CHECK and
the system audit events need, and nothing wider.

* No route write at all: Tier 1 detects host-route drift and flags the record; the API
  task repairs it. The one EC2 action is DescribeRouteTables.
* The record write: UpdateItem on the instances table, held by dynamodb:Attributes to
  the member fields and notes the collector owns (plus status, its condition), never a
  connection-routing field or host_routes.
* Audit: PutItem alone on the audit table, GetItem on the config table's
  audit_settings key only, PutObject alone on the vault under HasAuditBucket,
  PutMetricData in <prefix>/Audit, and the DenyAuditSettingsWrite statement.
* The metrics store: PutItem / BatchWriteItem / Query on the Tier 1 table only (Query
  reads its own previous sample for the per-interval counters), nothing on the Tier 2
  table, PutMetricData in <prefix>/Metrics, and no GetMetricData (it cannot read the
  fleet's series back).
* The failover notice: sns:Publish on the deployment's topic only.

Reads backend/template.yaml with the CloudFormation tags kept. Hermetic.

Run:  python3 tests/test_tier1_role_policy.py
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


class _TagLoader(yaml.SafeLoader):
    pass


def _tagged(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {"!" + suffix: value}


_TagLoader.add_multi_constructor("!", _tagged)
DOC = yaml.load((ROOT / "backend/template.yaml").read_text(encoding="utf-8"), Loader=_TagLoader)  # nosec B506
ROLE = DOC["Resources"]["Tier1CollectorRole"]["Properties"]


def as_list(v):
    return [] if v is None else (v if isinstance(v, list) else [v])


def statements():
    """[(policy, condition or '', statement)] with statement-level !If unwrapped."""
    out = []
    for pol in ROLE["Policies"]:
        for st in pol["PolicyDocument"]["Statement"]:
            if "!If" in st:
                cond, then, _else = st["!If"]
                check(_else == {"!Ref": "AWS::NoValue"}, f"{pol['PolicyName']}: a conditional statement drops out")
                out.append((pol["PolicyName"], cond, then))
            else:
                out.append((pol["PolicyName"], "", st))
    return out


STMTS = statements()


def with_action(action):
    return [(p, c, st) for p, c, st in STMTS if action in as_list(st.get("Action"))]


print("== no route write: Tier 1 detects, the API task repairs ==")
ec2_actions = sorted({a for _p, _c, st in STMTS for a in as_list(st.get("Action"))
                      if isinstance(a, str) and a.startswith("ec2:")})
check(ec2_actions == ["ec2:DescribeRouteTables"], f"one EC2 action, a read ({ec2_actions})")
for verb in ("ec2:CreateRoute", "ec2:ReplaceRoute", "ec2:DeleteRoute"):
    check(not with_action(verb), f"{verb} is not granted to the Tier 1 collector")
check(not any(isinstance(a, str) and (a in ("ec2:*", "*") or (a.startswith("ec2:") and "Route" in a
                                                             and not a.startswith("ec2:Describe")))
              for _p, _c, st in STMTS for a in as_list(st.get("Action"))), "no ec2:*, *, or any route write")
(pol, cond, st), = with_action("ec2:DescribeRouteTables")
check(as_list(st["Action"]) == ["ec2:DescribeRouteTables"] and st["Resource"] == "*",
      "DescribeRouteTables alone (no resource scoping exists)")

print("== the record write: the collector's own fields ==")
(pol, cond, st), = [x for x in with_action("dynamodb:UpdateItem") if x[2]["Effect"] == "Allow"]
check(st["Resource"] == [{"!Sub": "arn:aws:dynamodb:${AWS::Region}:${AWS::AccountId}:table/${DbConfigTable}"}],
      "UpdateItem on the instances table only")
attrs = st.get("Condition", {}).get("ForAllValues:StringEquals", {}).get("dynamodb:Attributes")
check(attrs == ["instance_id", "status", "cluster_member_roles", "reader_endpoint", "writer_instance_id",
                "members_changed_at", "route_repair_needed", "config_log", "updated_at", "tier1_store_error",
                "member_sync_failures", "member_sync_failing_since"],
      f"held to exactly the member attributes and the collector's notes ({attrs})")
for f in ("endpoint", "secret_arn", "port", "network_peering_id", "db_vpc_cidr", "network_route_mode",
          "host_routes"):
    check(f not in (attrs or []), f"{f} is not writable by Tier 1")

print("== describes for the member sync ==")
(pol, cond, st), = with_action("rds:DescribeDBClusters")
check(sorted(as_list(st["Action"])) == ["rds:DescribeDBClusters", "rds:DescribeDBInstances"],
      "DescribeDBClusters / DescribeDBInstances, nothing else in RDS")
check(not any(isinstance(a, str) and a.startswith("rds:") and not a.startswith("rds:Describe")
              for _p, _c, s in STMTS for a in as_list(s.get("Action"))), "no RDS write")

print("== audit ==")
audit = [(p, c, s) for p, c, s in STMTS
         if any(isinstance(r, dict) and r.get("!Sub", "").endswith("table/${ResourcePrefix}-audit")
                for r in as_list(s.get("Resource")))]
check(len(audit) == 1 and as_list(audit[0][2]["Action"]) == ["dynamodb:PutItem"], "PutItem alone on the audit table")
cfg_reads = [(p, c, s) for p, c, s in with_action("dynamodb:GetItem")
             if any(isinstance(r, dict) and r.get("!Sub", "").endswith("${ResourcePrefix}-config")
                    for r in as_list(s.get("Resource")))]
check(len(cfg_reads) == 1 and cfg_reads[0][2].get("Condition") == {
    "ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["audit_settings"]}},
    "GetItem on the config table's audit_settings key only")
s3 = [(p, c, s) for p, c, s in STMTS if any(isinstance(a, str) and a.startswith("s3:") for a in as_list(s.get("Action")))]
check(len(s3) == 1 and s3[0][1] == "HasAuditBucket" and s3[0][2].get("Sid") == "Tier1AuditVaultPut"
      and as_list(s3[0][2]["Action"]) == ["s3:PutObject"]
      and s3[0][2]["Resource"] == {"!Sub": "arn:aws:s3:::${AuditBucketName}/*"},
      "the one S3 statement: PutObject on the vault's objects, under HasAuditBucket")
pmd = [s.get("Condition") for _p, _c, s in with_action("cloudwatch:PutMetricData")]
check(sorted(str(c) for c in pmd) == sorted(str(c) for c in (
    {"StringEquals": {"cloudwatch:namespace": {"!Sub": "${ResourcePrefix}/Audit"}}},
    {"StringEquals": {"cloudwatch:namespace": {"!Sub": "${ResourcePrefix}/Metrics"}}})),
      "PutMetricData only in <prefix>/Audit and <prefix>/Metrics, one statement each")
print("== the metrics store ==")
t1 = [(p, c, s) for p, c, s in STMTS
      if any(isinstance(r, dict) and r.get("!Sub", "").endswith("table/${Tier1MetricsTable}")
             for r in as_list(s.get("Resource")))]
check(len(t1) == 1 and sorted(as_list(t1[0][2]["Action"])) == [
    "dynamodb:BatchWriteItem", "dynamodb:PutItem", "dynamodb:Query"],
      "PutItem / BatchWriteItem / Query on the Tier 1 table, one statement")
check(not any(isinstance(r, dict) and "Tier2ScansTable" in r.get("!Sub", "")
              for _p, _c, s in STMTS for r in as_list(s.get("Resource"))),
      "nothing on the Tier 2 table")
check(not with_action("cloudwatch:GetMetricData"), "no GetMetricData: the collector cannot read series back")
deny = [s for _p, _c, s in STMTS if s.get("Sid") == "DenyAuditSettingsWrite"]
check(len(deny) == 1 and deny[0]["Effect"] == "Deny" and sorted(as_list(deny[0]["Action"])) == [
    "dynamodb:BatchWriteItem", "dynamodb:DeleteItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    and deny[0].get("Condition") == {"ForAnyValue:StringEquals": {"dynamodb:LeadingKeys": ["audit_settings"]}},
    "DenyAuditSettingsWrite: no write to the vault switch")

print("== the failover notice ==")
(pol, cond, st), = with_action("sns:Publish")
check(cond == "HasNotificationTopic" and st["Resource"] == {"!Ref": "NotificationTopicArn"},
      "sns:Publish on the deployment's topic only, when there is one")

print("== the function ==")
fn = DOC["Resources"]["Tier1CollectorFunction"]["Properties"]
env = fn["Environment"]["Variables"]
check(fn["Timeout"] == 180, "Timeout 180 s (every member, four at a time)")
check(env.get("AGENT_PRIVATE_ROUTE_TABLE_ID") == {"!Ref": "PrivateRouteTableId"}
      and env.get("NOTIFICATION_TOPIC_ARN") == {"!Ref": "NotificationTopicArn"},
      "the route table (read) and the topic are passed in")
master = yaml.load((ROOT / "infrastructure/master.yaml").read_text(encoding="utf-8"), Loader=_TagLoader)  # nosec B506
agent = master["Resources"]["AgentStack"]["Properties"]["Parameters"]
check(agent.get("PrivateRouteTableId") == {"!GetAtt": "VPCStack.Outputs.PrivateRouteTableId"}
      and agent.get("AgentVpcId") == {"!GetAtt": "VPCStack.Outputs.VpcId"},
      "master passes AgentStack the route table and the agent VPC")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
