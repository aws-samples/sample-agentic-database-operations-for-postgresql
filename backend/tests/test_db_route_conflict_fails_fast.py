# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A DB-side route conflict fails the network configuration with its real reason.

The DB's return traffic follows the DB-side route for the agent CIDR. When that route
already points at a DIFFERENT, still-active peering (a leftover from an earlier onboarding
whose teardown did not finish), `_ensure_route` raises RouteConflict. Before this fix the
peering step swallowed it into a log line and returned ``status: configured``, so onboarding
went on to the connectivity check, which failed with asymmetric routing and recorded the
misleading "timeout expired" as config_error - hiding the real cause.

Now the conflict is a ``failed`` result carrying an actionable error (which route table,
which CIDR, which peering holds it, what to do), so onboarding stops with it. The remaining
idempotent steps still run and the registry still records the peering, so teardown stays
reference-counted.

Also: the candidates projection carries ``config_log`` for a failed row, so the console's
FailureDetail can show the step that failed, not only the one-line reason.

Hermetic: `_configure_peering` and the RouteConflict class are run against fake EC2 /
registry objects. No AWS, no network.

Run:  python3 backend/tests/test_db_route_conflict_fails_fast.py
"""
import pathlib
import re
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
NET_SRC = (ROOT / "tools/aws/network_client.py").read_text(encoding="utf-8")
APP_SRC = (ROOT / "app.py").read_text(encoding="utf-8")
passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


# ── Pin the shipped source shape (cheap, catches a regression to swallowing) ──
print("the DB-side route step reports a RouteConflict as the result error, not a log line")
peering_fn = re.search(r"def _configure_peering\(.*?(?=\n    def _ensure_agent_routes)", NET_SRC, re.S).group(0)
check("except RouteConflict as e:" in peering_fn.split("# Step 6: Security group rules")[0],
      "the DB-route try/except has a dedicated RouteConflict branch")
check("db_route_error" in peering_fn, "a db_route_error is captured")
check('if db_route_error:\n            return {"status": "failed"' in peering_fn,
      "a db_route_error makes the peering result status 'failed'")
check(peering_fn.index("if agent_route_error:") < peering_fn.index("if db_route_error:")
      < peering_fn.index('return {"status": "configured"'),
      "the failed returns precede the configured return")
check("Route conflict in the database VPC" in peering_fn and "run Configure again" in peering_fn,
      "the error names the conflict and tells the operator what to do")

print("the candidates projection carries config_log for a failed row")
cand = re.search(r"out\.append\(_flag_stale_configuring\(\{.*?\}\)\)", APP_SRC, re.S).group(0)
check('"config_log":' in cand, "config_log is projected onto the candidate row")
check('"config_failed", "delete_failed"' in cand.split('"config_log":')[1].split("\n")[0],
      "config_log is only sent for a failed onboard or an unfinished removal")

# ── Behavioural: a RouteConflict on the DB side yields status failed with the reason ──
print("behaviour: a DB-side RouteConflict fails the configuration with the real reason")


class FakeEC2:
    """The agent-side EC2 client; routes/peerings there succeed."""
    def describe_route_tables(self, **kw):
        return {"RouteTables": [{"Routes": []}]}

    def create_route(self, **kw):
        return {}


class ConflictingPeerEC2:
    """The DB-account EC2 client: the agent-CIDR route already points at another peering."""
    def __init__(self):
        self.replaced = []

    def describe_route_tables(self, **kw):
        if "Filters" in kw:   # _get_route_tables_for_vpc
            return {"RouteTables": [{"RouteTableId": "rtb-db"}]}
        return {"RouteTables": [{"Routes": [{"DestinationCidrBlock": "172.20.0.0/16",
                                            "VpcPeeringConnectionId": "pcx-OLD",
                                            "State": "active"}]}]}

    def create_route(self, **kw):
        raise Exception("RouteAlreadyExists: the route identified by 172.20.0.0/16 already exists")

    def replace_route(self, **kw):
        self.replaced.append(kw)

    def describe_security_groups(self, **kw):
        return {"SecurityGroups": [{"IpPermissions": []}]}

    def authorize_security_group_ingress(self, **kw):
        return {}


class FakeSession:
    def __init__(self, ec2):
        self._ec2 = ec2

    def client(self, name, region_name=None):
        return self._ec2


class FakeRegistry:
    def __init__(self):
        self.created, self.refs = [], []

    def get_connection(self, cid):
        return None

    def create_connection(self, data):
        self.created.append(data)

    def add_reference(self, cid, iid):
        self.refs.append(iid)

    def update_connection(self, *a, **k):
        pass


# Build a NetworkConfigurator with the real module code but a fake ec2 property.
# boto3 is not needed by the code under test (every client is injected), but the module
# imports it at the top; stub it so the real source loads without the SDK installed.
if "boto3" not in sys.modules:
    _boto3 = types.ModuleType("boto3")
    _boto3.client = lambda *a, **k: (_ for _ in ()).throw(AssertionError("boto3.client must not be called"))
    sys.modules["boto3"] = _boto3
sys.path.insert(0, str(ROOT))
ns = {"__name__": "tools.aws.network_client"}
exec(compile(NET_SRC, str(ROOT / "tools/aws/network_client.py"), "exec"), ns)  # noqa: S102 - pinning shipped source
NetworkConfigurator = ns["NetworkConfigurator"]
RouteConflict = ns["RouteConflict"]

reg = FakeRegistry()
fake_registry_mod = types.ModuleType("tools.aws.network_registry")
fake_registry_mod.network_registry = reg
fake_registry_mod.NetworkRegistry = types.SimpleNamespace(make_connection_id=lambda a, b: f"{a}|{b}")
sys.modules["tools.aws.network_registry"] = fake_registry_mod

import os  # noqa: E402
# The agent's own identity comes from the environment (env-backed properties).
os.environ["AGENT_VPC_ID"] = "vpc-agent"
os.environ["AGENT_VPC_CIDR"] = "172.20.0.0/16"
os.environ["AGENT_PRIVATE_ROUTE_TABLE_ID"] = "rtb-agent"
cfg = NetworkConfigurator.__new__(NetworkConfigurator)
cfg._ec2 = FakeEC2()
peer = ConflictingPeerEC2()

# Drive _configure_peering on the "adopt an existing active peering" path, which is the
# real failure scenario: the peering is fine, only the DB-side route is held by another.
cfg._find_existing_peering = lambda db_vpc_id: {"peering_id": "pcx-NEW", "status": "active"}
cfg._ensure_agent_routes = lambda *a, **k: {"route_mode": "vpc"}
cfg._release_host_routes = lambda *a, **k: {}
cfg._ensure_dns = lambda *a, **k: None
for name in ("_enable_dns_resolution", "_ensure_peering_dns"):
    if hasattr(NetworkConfigurator, name):
        setattr(cfg, name, lambda *a, **k: None)

import inspect  # noqa: E402
sig = inspect.signature(NetworkConfigurator._configure_peering)
kwargs = dict(db_vpc_id="vpc-db", db_vpc_cidr="10.0.0.0/16", db_security_group_ids="sg-db",
              db_port=5432, db_account_id="222222222222", db_region="eu-west-1",
              is_cross_account=True, is_cross_region=True, cross_account_session=FakeSession(peer),
              steps=[], instance_id="db-1", db_endpoint="db.example", db_member_endpoints=(),
              owned_host_routes=())
kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}

# Any peering-level DNS/accept/wait helpers must be no-ops: the peering is already active.
for name, member in inspect.getmembers(NetworkConfigurator, inspect.isfunction):
    if name.startswith("_") and name not in (
            "_configure_peering", "_ensure_route", "_existing_route", "_get_route_tables_for_vpc",
            "_ensure_sg_ingress", "_find_existing_peering") and name not in dir(cfg.__dict__):
        if name in ("_ensure_agent_routes", "_release_host_routes"):
            continue
        if "dns" in name.lower() or "accept" in name.lower() or "wait" in name.lower():
            setattr(cfg, name, lambda *a, **k: None)

out = cfg._configure_peering(**kwargs)
check(out.get("status") == "failed", f"status is 'failed' (got {out.get('status')!r})")
err = out.get("error", "")
check("Route conflict in the database VPC" in err, "the error names the route conflict")
check("rtb-db" in err and "172.20.0.0/16" in err and "pcx-OLD" in err and "pcx-NEW" in err,
      "the error names the route table, the CIDR, the holder and this peering")
check(out.get("peering_id") == "pcx-NEW", "the result still carries the peering id (for teardown)")
check(peer.replaced == [], "the LIVE conflicting route is NOT overwritten")
check(reg.created or reg.refs, "the peering is still registered so teardown stays ref-counted")
check(any("DB routes failed" in s for s in out.get("steps", [])), "the step log records the failure")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
