# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The observability tools read the database they were asked about, in its own Region.

The agent used to pass the display name to CloudWatch, Performance Insights, CloudTrail
and the log tools, and the tools used that string as the RDS identifier. A display name is
a label: a rename changes it freely, and two databases in one space can share it (the same
RDS name in two Regions). So a renamed database read as empty, the registry id the scope
block hands out was not an RDS name at all, and a repeated name resolved to whichever row
came first, which is another database's metrics. `get_instance_details` also overrode the
resolved session's Region with the deployment's.

Pinned here, through the real tools with fake AWS clients:
  * two databases named "orders" in us-east-1 and eu-west-1, same space: each registry id
    reads its own Region's CloudWatch, PI, CloudTrail, logs and RDS describe, under the
    RDS identifier from the record;
  * the bare name "orders" is refused with a message naming db_instance_id, never
    silently resolved to one of them;
  * a renamed database (display name no longer the RDS name) still reads its metrics;
  * a single, unique display name still resolves (older turns keep working).

Hermetic: boto3 and strands stubbed; no AWS.
Run:  python3 backend/tests/test_observability_target.py
"""
import os
import sys
import types
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

passed = failed = 0


def check(cond, label):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}")


SPACE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ACCOUNT = "111111111111"
US = "11111111-1111-4111-8111-111111111111"
EU = "22222222-2222-4222-8222-222222222222"
RENAMED = "33333333-3333-4333-8333-333333333333"

ROWS = [
    {"instance_id": US, "display_name": "orders", "account_id": ACCOUNT, "region": "us-east-1",
     "member_space_ids": [SPACE],
     "tags": {"raw_identifier": "orders", "arn": f"arn:aws:rds:us-east-1:{ACCOUNT}:db:orders"}},
    {"instance_id": EU, "display_name": "orders", "account_id": ACCOUNT, "region": "eu-west-1",
     "member_space_ids": [SPACE],
     "tags": {"raw_identifier": "orders", "arn": f"arn:aws:rds:eu-west-1:{ACCOUNT}:db:orders"}},
    # Renamed in the console; no raw_identifier, so the ARN carries the RDS name.
    {"instance_id": RENAMED, "display_name": "Billing (prod)", "account_id": ACCOUNT,
     "region": "eu-west-1", "member_space_ids": [SPACE],
     "tags": {"arn": f"arn:aws:rds:eu-west-1:{ACCOUNT}:cluster:billing-cluster"}},
]

# What exists in RDS, per Region: instances and clusters by RDS identifier.
RDS = {
    "us-east-1": {"instances": {"orders": "db.r6g.large"}, "clusters": {}},
    "eu-west-1": {"instances": {"orders": "db.r6g.2xlarge", "billing-1": "db.r6g.xlarge"},
                  "clusters": {"billing-cluster": ["billing-1"]}},
}
CALLS = []  # (service, region, what)


class NotFound(Exception):
    pass


class FakeTable:
    def get_item(self, Key=None, **kw):
        for r in ROWS:
            if r["instance_id"] == (Key or {}).get("instance_id"):
                return {"Item": r}
        return {}

    def scan(self, **kw):
        # Two pages, to prove the name lookup reads every page.
        field = next(iter(kw.get("ExpressionAttributeNames", {}).values()), "")
        want = kw.get("ExpressionAttributeValues", {}).get(":val")
        match = [r for r in ROWS if r.get(field) == want]
        if "ExclusiveStartKey" not in kw:
            return {"Items": match[:1], "LastEvaluatedKey": {"instance_id": "page2"}}
        return {"Items": match[1:]}


class FakeRds:
    def __init__(self, region):
        self.region = region
        self.exceptions = types.SimpleNamespace(DBClusterNotFoundFault=NotFound)

    def describe_db_clusters(self, DBClusterIdentifier):
        CALLS.append(("rds", self.region, DBClusterIdentifier))
        members = RDS[self.region]["clusters"].get(DBClusterIdentifier)
        if members is None:
            raise NotFound(DBClusterIdentifier)
        return {"DBClusters": [{"DBClusterIdentifier": DBClusterIdentifier, "Engine": "aurora-postgresql",
                                "DBClusterMembers": [{"DBInstanceIdentifier": m, "IsClusterWriter": True}
                                                     for m in members]}]}

    def describe_db_instances(self, DBInstanceIdentifier):
        CALLS.append(("rds", self.region, DBInstanceIdentifier))
        cls = RDS[self.region]["instances"].get(DBInstanceIdentifier)
        if cls is None:
            raise NotFound(DBInstanceIdentifier)
        return {"DBInstances": [{"DBInstanceIdentifier": DBInstanceIdentifier, "DBInstanceClass": cls,
                                 "DbiResourceId": f"db-{self.region}-{DBInstanceIdentifier}"}]}


class FakeCloudWatch:
    def __init__(self, region):
        self.region = region

    def get_metric_statistics(self, Dimensions, **kw):
        dim = Dimensions[0]["Value"]
        CALLS.append(("cloudwatch", self.region, dim))
        known = dim in RDS[self.region]["instances"]
        value = {"us-east-1": 11.0, "eu-west-1": 77.0}[self.region]
        return {"Datapoints": [{"Timestamp": datetime(2026, 10, 6, tzinfo=timezone.utc),
                                "Average": value}] if known else []}

    def describe_alarms_for_metric(self, Dimensions, **kw):
        CALLS.append(("cloudwatch-alarms", self.region, Dimensions[0]["Value"]))
        return {"MetricAlarms": []}

    def describe_alarms(self, AlarmNamePrefix, **kw):
        CALLS.append(("cloudwatch-alarms", self.region, AlarmNamePrefix))
        return {"MetricAlarms": []}


class FakePi:
    def __init__(self, region):
        self.region = region

    def get_resource_metrics(self, Identifier, **kw):
        CALLS.append(("pi", self.region, Identifier))
        return {"MetricList": []}


class FakeCloudTrail:
    def __init__(self, region):
        self.region = region

    def lookup_events(self, LookupAttributes, **kw):
        CALLS.append(("cloudtrail", self.region, LookupAttributes[0]["AttributeValue"]))
        return {"Events": []}


class FakeLogs:
    def __init__(self, region):
        self.region = region
        self.exceptions = types.SimpleNamespace(ResourceNotFoundException=NotFound)

    def filter_log_events(self, logGroupName, **kw):
        CALLS.append(("logs", self.region, logGroupName))
        raise NotFound(logGroupName)


CLIENTS = {"rds": FakeRds, "cloudwatch": FakeCloudWatch, "pi": FakePi,
           "cloudtrail": FakeCloudTrail, "logs": FakeLogs}


class FakeSession:
    def __init__(self, region_name=None, **kw):
        self.region_name = region_name

    def client(self, name, region_name=None):
        return CLIENTS[name](region_name or self.region_name)


_boto3 = types.ModuleType("boto3")
_boto3.Session = FakeSession
_boto3.resource = lambda *a, **k: types.SimpleNamespace(Table=lambda name: FakeTable())
_boto3.client = lambda *a, **k: None
sys.modules["boto3"] = _boto3
_strands = types.ModuleType("strands")
_strands.tool = lambda f=None, **kw: (f if f is not None else (lambda g: g))
sys.modules["strands"] = _strands
_fake_pg = types.ModuleType("tools.aws.pg_client")
_fake_pg.execute_query = lambda *a, **k: []
_fake_pg.test_connection = lambda *a, **k: {"ok": True}
_fake_pg.lookup_record_database_name = lambda *a, **k: ""
_fake_pg.SECRET_NOT_IN_SPACE_MESSAGE = "not available in the current space"
_fake_pg.resolve_credential = lambda h: h
sys.modules["tools.aws.pg_client"] = _fake_pg
os.environ["AWS_REGION"] = "us-east-1"  # the deployment's Region

from tools.aws import session_factory as sf  # noqa: E402
from tools.aws import cloudwatch_client as cw  # noqa: E402
from tools.aws import performance_insights_client as pi  # noqa: E402
from tools.aws import cloudtrail_client as ct  # noqa: E402
from tools.aws import cloudwatch_logs_client as logs  # noqa: E402
from tools import db_tools  # noqa: E402
import model_resolver as mr  # noqa: E402

sf._agent_account_id = ACCOUNT  # same-account databases: no role to assume
mr.CURRENT_USER_ID.set("operator-1")
mr.CURRENT_SPACE_ID.set(SPACE)


def calls(service):
    return [(r, what) for s, r, what in CALLS if s == service]


def run(fn, *a, **k):
    CALLS.clear()
    sf._db_record_cache.clear()
    return fn(*a, **k)


print("== two databases named \"orders\": each registry id reads its own Region ==")
us = run(cw.get_cpu_utilization, US)
check(calls("cloudwatch") == [("us-east-1", "orders")],
      f"US id: CloudWatch in us-east-1 on DBInstanceIdentifier=orders ({calls('cloudwatch')})")
check(us.get("summary", {}).get("current") == 11.0, "US id: the us-east-1 database's CPU is returned")
eu = run(cw.get_cpu_utilization, EU)
check(calls("cloudwatch") == [("eu-west-1", "orders")],
      f"EU id: CloudWatch in eu-west-1, not the deployment Region ({calls('cloudwatch')})")
check(eu.get("summary", {}).get("current") == 77.0, "EU id: the eu-west-1 database's CPU is returned")
check(all(r == "eu-west-1" for r, _ in calls("rds")) and calls("rds"),
      "EU id: the RDS describe that resolves members runs in eu-west-1")

run(pi.get_db_load, EU)
check(calls("pi") == [("eu-west-1", "db-eu-west-1-orders")],
      f"EU id: Performance Insights reads the eu-west-1 instance's resource id ({calls('pi')})")
run(ct.get_rds_events, EU)
check(calls("cloudtrail") == [("eu-west-1", "orders")],
      f"EU id: CloudTrail looks up ResourceName=orders in eu-west-1 ({calls('cloudtrail')})")
run(logs.get_db_error_logs, EU)
check(calls("logs") and all(r == "eu-west-1" and "/orders/" in g for r, g in calls("logs")),
      f"EU id: log groups are /aws/rds/.../orders/... in eu-west-1 ({calls('logs')})")
run(cw.get_cloudwatch_alarms, EU)
check(calls("cloudwatch-alarms") == [("eu-west-1", "orders"), ("eu-west-1", "orders")],
      "EU id: alarms are looked up by the RDS name in eu-west-1")
details = run(db_tools.get_instance_details, EU)
check(details.get("instance_class") == "db.r6g.2xlarge",
      f"get_instance_details(EU id) describes the eu-west-1 instance, not us-east-1's ({details})")
check(calls("rds") and all(r == "eu-west-1" for r, _ in calls("rds")),
      "get_instance_details keeps the database's Region (no deployment-Region override)")

print("")
print("== the repeated name is refused, never resolved to the first row ==")
res = run(cw.get_cpu_utilization, "orders")
check(not calls("cloudwatch"), "no metric is read for the ambiguous name")
check("alias from [DATABASE SCOPE]" in (res.get("error") or ""),
      f"the error tells the agent to pass the scope's alias ({res.get('error')})")
try:
    run(sf.resolve_target, "orders")
    check(False, "resolve_target refuses the ambiguous name")
except LookupError as e:
    check("More than one database" in str(e), "resolve_target refuses the ambiguous name")

print("")
print("== a renamed database still reads its metrics ==")
res = run(cw.get_cpu_utilization, RENAMED)
check(calls("cloudwatch") == [("eu-west-1", "billing-1")],
      f"renamed cluster: members resolved from the ARN's RDS name ({calls('cloudwatch')})")
check(res.get("summary", {}).get("current") == 77.0, "renamed cluster: data returned, not empty")
check(sf.rds_identifier(ROWS[2]) == "billing-cluster", "rds_identifier falls back to the ARN segment")
check(sf.rds_identifier({}, "given") == "given", "an unresolved handle passes through unchanged")

print("")
print("== a unique display name still resolves ==")
res = run(cw.get_cpu_utilization, "Billing (prod)")
check(calls("cloudwatch") == [("eu-west-1", "billing-1")], "unique name: resolves to its database")

print("")
print("== the prompts hand the tools the registry id ==")
here = os.path.dirname(os.path.abspath(__file__))
app_src = open(os.path.join(here, "..", "app.py")).read()
obs_src = open(os.path.join(here, "..", "sub_agents", "observability_agent.py")).read()
check("Pass the display_name as db_instance_id" not in app_src,
      "the coordinator no longer passes the display name to the observability agent")
check("never the display_name: names can repeat" in app_src,
      "the coordinator passes the scope's alias as db_instance_id")
check("Never pass a display name" in obs_src, "the observability agent passes the id through unchanged")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
