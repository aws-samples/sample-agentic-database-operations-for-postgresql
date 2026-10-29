# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""An unknown VPC id fails Configure; it is never a successful "no network needed" onboard.

``_configure_single_db`` used to treat a missing database VPC id or a missing agent VPC
id as "No VPC configuration needed": status configured, both tiers scheduled, success,
without any login. A failed RDS lookup that leaves the VPC empty is not evidence that the
database shares the agent's VPC. Now:

  - either id unknown: status ``config_failed`` with a retryable message, no network built,
    no schedules, no membership;
  - both known and equal: configure_network says ``same_vpc`` and the login still decides
    (membership only after it succeeds);
  - both known and different: the normal network path.

The task is extracted from backend/app.py by source and run with fake registry, network,
RDS enrichment and psycopg2. Hermetic, no AWS.

Run:  python3 backend/tests/test_configure_unknown_vpc.py
"""
import json
import os
import pathlib
import re
import sys
import types

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
    assert m, f"{name} not found in app.py"
    return m.group(0)


class Registry:
    def __init__(self, rec):
        self.rec = dict(rec)
        self.promoted = []
        self.candidates = []

    def get_instance(self, iid):
        return dict(self.rec)

    def update_instance(self, iid, fields):
        self.rec.update(fields)

    def promote_candidate(self, iid, sid, secret):
        self.promoted.append((iid, sid, secret))
        self.rec["member_space_ids"] = set(self.rec.get("member_space_ids") or ()) | {sid}

    def add_candidate(self, iid, sid):
        self.candidates.append(sid)


class Network:
    def __init__(self, agent_vpc):
        self.agent_vpc_id = agent_vpc
        self.calls = []

    def configure_network(self, **kw):
        self.calls.append(kw)
        if kw["db_vpc_id"] == self.agent_vpc_id:
            return {"status": "same_vpc", "steps": ["Same VPC — no peering needed"]}
        return {"status": "configured", "method": "peering", "peering_id": "pcx-1", "steps": ["peered"]}


class Secrets:
    def get_secret_value(self, SecretId):
        return {"SecretString": json.dumps({"username": "ro", "password": "p"})}


class Session:
    def client(self, name, **kw):
        if name == "rds":
            return object()
        return Secrets()


def build(rec, agent_vpc, enriched_vpc):
    reg = Registry(rec)
    net = Network(agent_vpc)
    scheduled, connects, audits = [], [], []

    def enrich(rds, arn, region):
        return {"endpoint": "db.example", "port": 5432, "vpc_id": enriched_vpc, "subnet_ids": "",
                "security_group_ids": "", "engine": "postgres", "members": []}

    psycopg2 = types.ModuleType("psycopg2")

    def connect(**kw):
        connects.append(kw)
        return types.SimpleNamespace(close=lambda: None)

    psycopg2.connect = connect
    sys.modules["psycopg2"] = psycopg2
    ns = {
        "json": json, "os": os, "re": re,
        "db_registry": reg, "network_configurator": net,
        "governance_registry": types.SimpleNamespace(audit=lambda *a, **k: audits.append(a[5] if len(a) > 5 else a)),
        "resource_explorer": types.SimpleNamespace(_enrich_instance=enrich, _enrich_cluster=enrich),
        "db_discovery": types.SimpleNamespace(_get_cross_account_session=lambda *a, **k: None),
        "_space_session": lambda space, account, region: Session(),
        "_auto_schedule_on_configure": lambda iid, name="": scheduled.append(iid),
        "_port_range_warning": lambda port: "",
        "_replica_tags": lambda tags, details: {},
        "_instance_member_spaces": lambda r: set(r.get("member_space_ids") or ()),
        "_refresh_space_db_count": lambda sid: None,
        "_redact_identifiers": lambda s: s,
        "owner_admin_of": lambda sp: "owner",
        "ssl_kwargs": lambda: {},
        "logger": types.SimpleNamespace(info=lambda *a, **k: None, error=lambda *a, **k: None,
                                        warning=lambda *a, **k: None),
    }
    exec(_extract("_configure_single_db"), ns)  # noqa: S102 - pinning shipped source
    return ns["_configure_single_db"], reg, net, scheduled, connects


BASE = {"instance_id": "db-1", "display_name": "orders", "account_id": "111111111111", "region": "us-east-1",
        "secret_arn": "arn:aws:secretsmanager:us-east-1:111111111111:secret:dbops/owner/ro",
        "reach_space_id": "s1", "candidate_space_ids": {"s1"},
        "tags": {"arn": "arn:aws:rds:us-east-1:111111111111:db:orders"}}


def run(agent_vpc, enriched_vpc, rec=BASE):
    fn, reg, net, scheduled, connects = build(rec, agent_vpc, enriched_vpc)
    fn("s1", "db-1", {"space_id": "s1", "name": "S"}, "999999999999", "us-east-1",
       submitted_secret_arn=rec["secret_arn"], actor_sub="admin")
    return reg, net, scheduled, connects


print("database VPC unknown (enrichment left it empty)")
reg, net, scheduled, connects = run("vpc-agent", "")
check(reg.rec["status"] == "config_failed", "status is config_failed, not configured")
check("Retry Configure" in reg.rec.get("config_error", ""), "the error is retryable and says so")
check("database's VPC" in reg.rec.get("config_error", ""), "the error names what could not be read")
check("No VPC configuration needed" not in " ".join(reg.rec["config_log"]), "no 'no VPC needed' claim")
check(not scheduled, "no schedules created")
check(not net.calls, "no network built")
check(not connects, "no connectivity claimed")
check(not reg.promoted and "member_space_ids" not in reg.rec, "no membership granted")

print("agent VPC unknown (AGENT_VPC_ID unset)")
reg, net, scheduled, connects = run("", "vpc-db")
check(reg.rec["status"] == "config_failed", "status is config_failed")
check("AGENT_VPC_ID" in reg.rec.get("config_error", ""), "the error names the deployment setting")
check(not scheduled and not net.calls and not reg.promoted, "nothing scheduled, built or granted")

print("same VPC (both known and equal)")
reg, net, scheduled, connects = run("vpc-1", "vpc-1")
check(len(net.calls) == 1, "configure_network decides (same_vpc)")
check(len(connects) == 1, "the login still runs")
check(reg.rec["status"] == "configured" and reg.promoted, "configured and promoted only after the login")
check(scheduled == ["db-1"], "scheduled after the proof")

print("different VPCs (the normal network path)")
reg, net, scheduled, connects = run("vpc-agent", "vpc-db")
check(len(net.calls) == 1 and net.calls[0]["db_vpc_id"] == "vpc-db", "network path taken")
check(reg.rec["status"] == "configured" and reg.promoted and scheduled, "configured after the login")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
