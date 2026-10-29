# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Performance Insights follows the chosen cluster member, not always the writer.

``get_db_load``, ``get_top_sql`` and ``get_top_waits`` resolve the cluster's members from
RDS and read one of them: an explicit ``member_instance_id`` (which must be a current
member), else the conversation's role focus (``instance:<id>``, ``reader``), else the
writer. Run through the real tools with a fake session factory, fake RDS and a fake PI
client that records which DbiResourceId was read. Hermetic.

Run:  python3 backend/tests/test_pi_focus.py
"""
import os
import sys
import types
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_strands = types.ModuleType("strands")
_strands.tool = lambda f: f
sys.modules.setdefault("strands", _strands)

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


MEMBERS = [("db-a", False), ("db-c", False), ("db-b", True)]   # writer is db-b (after a failover)
reads = []


class FakeRds:
    def describe_db_clusters(self, DBClusterIdentifier):
        return {"DBClusters": [{"DBClusterMembers": [{"DBInstanceIdentifier": i, "IsClusterWriter": w}
                                                     for i, w in MEMBERS]}]}

    def describe_db_instances(self, DBInstanceIdentifier):
        return {"DBInstances": [{"DBInstanceIdentifier": DBInstanceIdentifier,
                                 "DbiResourceId": f"res-{DBInstanceIdentifier}"}]}


class FakePi:
    def get_resource_metrics(self, **kw):
        reads.append(kw["Identifier"])
        return {"MetricList": [{"Key": {"Metric": "db.load.avg", "Dimensions": {}},
                                "DataPoints": [{"Timestamp": datetime(2030, 1, 1, tzinfo=timezone.utc), "Value": 1.0}]}]}


class FakeSession:
    def client(self, name, **kw):
        return {"rds": FakeRds(), "pi": FakePi()}[name]


_sf = types.ModuleType("tools.aws.session_factory")
_sf.get_session = lambda identifier=None: FakeSession()
_sf.get_session_context = lambda session: "fake"
# No registry row: the handle is already the RDS identifier (resolve_target passes it through).
_sf.resolve_target = lambda handle: {"record": {}, "rds_id": handle, "account_id": "", "region": ""}
sys.modules["tools.aws.session_factory"] = _sf

import model_resolver  # noqa: E402
from tools.aws import performance_insights_client as pi  # noqa: E402

TOOLS = (pi.get_db_load, pi.get_top_sql, pi.get_top_waits)


def run(focus="", member=""):
    token = model_resolver.CURRENT_ROLE_FOCUS.set(focus)
    try:
        out = []
        for t in TOOLS:
            reads.clear()
            r = t("orders", member_instance_id=member)
            out.append((r, list(reads)))
        return out
    finally:
        model_resolver.CURRENT_ROLE_FOCUS.reset(token)


print("== no focus: the writer ==")
for r, rd in run():
    check(r["db_instance_id"] == "db-b" and r["role"] == "writer" and rd == ["res-db-b"] and "focus_note" not in r,
          f"reads the writer's resource ({rd})")

print("== instance:<id> focus: that member ==")
for r, rd in run("instance:db-c"):
    check(r["db_instance_id"] == "db-c" and r["role"] == "reader" and rd == ["res-db-c"], f"reads db-c ({rd})")

print("== reader focus: the first reader by id, with a note ==")
for r, rd in run("reader"):
    check(r["db_instance_id"] == "db-a" and rd == ["res-db-a"]
          and "round-robins" in r.get("focus_note", "") and "db-a" in r["focus_note"], "db-a with a focus_note")

print("== an unknown focused instance: the writer, with a note ==")
for r, rd in run("instance:db-gone"):
    check(r["db_instance_id"] == "db-b" and "not a current member" in r.get("focus_note", ""), "writer + note")

print("== an explicit member_instance_id wins over the focus ==")
for r, rd in run("reader", member="db-c"):
    check(r["db_instance_id"] == "db-c" and rd == ["res-db-c"] and "focus_note" not in r, "db-c")

print("== a non-member is an error, and nothing is read ==")
for r, rd in run(member="db-zzz"):
    check(r.get("error") == "member db-zzz is not in cluster orders" and rd == [], "error dict, no PI call")

print("== the agent sees the new parameter ==")
for t in TOOLS:
    check("member_instance_id" in (t.__doc__ or ""), f"{t.__name__} documents member_instance_id")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
