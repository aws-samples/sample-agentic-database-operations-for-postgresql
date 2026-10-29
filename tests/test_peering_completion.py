#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Guard: an existing peering is finished, not merely acknowledged.

Configuring connectivity to a database is a sequence: create the peering, get the
other account to accept it, wait for AWS to activate it, then add routes on both
sides, enable DNS, open the security group, and record the connection in the
registry. Only the last four make traffic possible.

Anything that interrupts the sequence after the accept leaves an active peering
whose routes and security-group rule were never built. An active peering is
therefore not proof the path carries traffic, so both the no-registry path and the
reuse path adopt the peering and run the remaining steps. They are all idempotent
(`_ensure_route` replaces, `_ensure_sg_ingress` ignores duplicates), so repeating
them against a working connection costs nothing and completes a partial one.

These tests drive the real code with fake EC2 clients - no AWS, no network.

Run:  python3 tests/test_peering_completion.py
"""
import importlib
import pathlib
import sys
import types

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

AGENT_VPC = "vpc-agent0000000001"
AGENT_CIDR = "172.20.0.0/16"
AGENT_RT = "rtb-agent000000001"
DB_VPC = "vpc-db000000000001"
DB_CIDR = "172.31.0.0/16"
DB_RT = "rtb-db00000000001"
DB_SG = "sg-db00000000001"
PCX = "pcx-existing000001"

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


class FakeEc2:
    """Records every call; answers the handful of reads the peering path makes."""

    def __init__(self, peering_status=None, pcx_id=PCX):
        self.calls = []
        self.peering_status = peering_status      # None = no existing peering
        self.pcx_id = pcx_id

    def _record(self, name, **kw):
        self.calls.append((name, kw))

    def names(self):
        return [c[0] for c in self.calls]

    def args_for(self, name):
        return [kw for n, kw in self.calls if n == name]

    # ── reads ──
    def describe_vpc_peering_connections(self, **kw):
        self._record("describe_vpc_peering_connections", **kw)
        if self.peering_status is None:
            return {"VpcPeeringConnections": []}
        return {"VpcPeeringConnections": [{
            "VpcPeeringConnectionId": self.pcx_id,
            "Status": {"Code": self.peering_status},
            "RequesterVpcInfo": {"VpcId": AGENT_VPC, "CidrBlock": AGENT_CIDR},
            "AccepterVpcInfo": {"VpcId": DB_VPC, "CidrBlock": DB_CIDR},
        }]}

    def describe_route_tables(self, **kw):
        self._record("describe_route_tables", **kw)
        return {"RouteTables": [{"RouteTableId": DB_RT}]}

    # ── writes ──
    def __getattr__(self, name):
        # Any other EC2 call succeeds and is recorded. Keeps the fake honest about
        # what the code actually invoked without hand-writing every stub.
        def _call(**kw):
            self._record(name, **kw)
            return {}
        return _call


class FakeSession:
    def __init__(self, ec2):
        self._ec2 = ec2

    def client(self, service, **kw):
        return self._ec2


class FakeRegistry:
    def __init__(self):
        self.created, self.references, self.updates, self.deleted = [], [], [], []
        # Set by a test to simulate a registered peering the reuse path finds.
        self._peering = None

    def get_connection(self, cid):
        return None

    def create_connection(self, rec):
        self.created.append(rec)

    def update_connection(self, cid, patch):
        self.updates.append((cid, patch))

    def add_reference(self, cid, iid):
        self.references.append((cid, iid))

    # Reads the reuse path in configure_network makes.
    def find_active_peering(self, db_vpc_id):
        return self._peering

    def find_active_lattice(self, db_endpoint, agent_vpc_id):
        return None

    def delete_connection(self, cid):
        self.deleted.append(cid)


class FakeSts:
    def __init__(self, account):
        self._account = account

    def get_caller_identity(self):
        return {"Account": self._account}


class FakeBoto3:
    """Stands in for the boto3 module so configure_network's sts identity lookup and
    any same-account ec2 client resolve to fakes rather than reaching AWS."""

    def __init__(self, ec2, sts_account="111111111111"):
        self._ec2 = ec2
        self._sts_account = sts_account

    def client(self, service, **kw):
        if service == "sts":
            return FakeSts(self._sts_account)
        return self._ec2


def install_registry_stub():
    """`_configure_peering` imports the registry lazily, so overriding the entry in
    sys.modules is enough. The real `tools.aws` package is imported first so that
    the rest of the package still resolves normally."""
    reg = FakeRegistry()
    importlib.import_module("tools.aws")   # real package: keeps sibling imports working
    mod = types.ModuleType("tools.aws.network_registry")
    mod.network_registry = reg

    class NetworkRegistry:
        @staticmethod
        def make_connection_id(a, b):
            return f"{a}--{b}"

    mod.NetworkRegistry = NetworkRegistry
    sys.modules["tools.aws.network_registry"] = mod
    return reg


def make_configurator(monkeypatch_env=True):
    import os
    if monkeypatch_env:
        os.environ["AGENT_VPC_ID"] = AGENT_VPC
        os.environ["AGENT_VPC_CIDR"] = AGENT_CIDR
        os.environ["AGENT_PRIVATE_ROUTE_TABLE_ID"] = AGENT_RT
        os.environ.setdefault("AWS_REGION", "us-east-1")
    from tools.aws.network_client import NetworkConfigurator
    return NetworkConfigurator()


class FakeTime:
    """Deterministic clock so the wait loop finishes instantly."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, secs):
        self.now += secs


print("== adopting an active peering still finishes the job ==")
reg = install_registry_stub()
cfg = make_configurator()
fake = FakeEc2(peering_status="active")
cfg._ec2 = fake

import tools.aws.network_client as nc  # noqa: E402

result = cfg._configure_peering(
    db_vpc_id=DB_VPC, db_vpc_cidr=DB_CIDR, db_security_group_ids=DB_SG,
    db_port=5432, db_account_id="222222222222", db_region="us-east-1",
    is_cross_account=True, is_cross_region=False,
    cross_account_session=FakeSession(fake), steps=[], instance_id="i-abc",
)
called = fake.names()
log = " | ".join(result.get("steps", []))

check(result.get("status") == "configured", f"reports configured (got {result.get('status')!r})")
check("create_vpc_peering_connection" not in called, "does not create a second peering")
check("accept_vpc_peering_connection" not in called, "does not re-accept an active peering")
check("create_route" in called, "creates routes — the step a bare early return would skip")
check("modify_vpc_peering_connection_options" in called, "enables DNS resolution")
check("authorize_security_group_ingress" in called, "opens the security group")
check(bool(reg.created or reg.references),
      "records the connection in the registry, so teardown is reference-counted")

# The route must point at the adopted peering, on both sides.
routes = fake.args_for("create_route")
cidrs = {r.get("DestinationCidrBlock") for r in routes}
check(DB_CIDR in cidrs, f"agent-side route to {DB_CIDR}")
check(AGENT_CIDR in cidrs, f"database-side route back to {AGENT_CIDR}")
check(bool(routes) and all(r.get("VpcPeeringConnectionId") == PCX for r in routes),
      "every route points at the adopted peering")
check("Adopting active peering" in log, "the log says it adopted rather than 'reusing'")

print()
print("== a peering that never activates says what AWS last reported ==")
reg2 = install_registry_stub()
cfg2 = make_configurator()
stuck = FakeEc2(peering_status="provisioning", pcx_id="pcx-stuck00000001")
# No existing peering to adopt, so it creates one, accepts, then waits forever.
stuck_find = {"first": True}
real_describe = stuck.describe_vpc_peering_connections

def describe_no_existing(**kw):
    # The initial search must find nothing, so the code takes the create path;
    # every later describe reports the stuck status.
    if not kw.get("VpcPeeringConnectionIds") and stuck_find["first"]:
        stuck_find["first"] = False
        stuck._record("describe_vpc_peering_connections", **kw)
        return {"VpcPeeringConnections": []}
    return real_describe(**kw)

stuck.describe_vpc_peering_connections = describe_no_existing
cfg2._ec2 = stuck
stuck.create_vpc_peering_connection = lambda **kw: (
    stuck._record("create_vpc_peering_connection", **kw)
    or {"VpcPeeringConnection": {"VpcPeeringConnectionId": "pcx-stuck00000001"}}
)

saved_time = nc.time
nc.time = FakeTime()
try:
    res2 = cfg2._configure_peering(
        db_vpc_id=DB_VPC, db_vpc_cidr=DB_CIDR, db_security_group_ids=DB_SG,
        db_port=5432, db_account_id="222222222222", db_region="us-east-1",
        is_cross_account=True, is_cross_region=False,
        cross_account_session=FakeSession(stuck), steps=[], instance_id="i-abc",
    )
finally:
    nc.time = saved_time

err2 = res2.get("error", "")
log2 = " | ".join(res2.get("steps", []))
check(res2.get("status") == "failed", f"reports failed (got {res2.get('status')!r})")
check("provisioning" in err2,
      "names the status AWS last returned, not just the elapsed time")
check("pcx-stuck00000001" in err2, "names the peering, so it can be inspected")
check("Configure again" in err2 or "adopted" in err2,
      "tells the operator a retry will finish the job")
check("create_route" not in stuck.names(),
      "does not build routes over a peering that is not active")
check("300s" in log2, "uses the longer cross-account budget")

print()
print("== a reused, active registered peering is still completed, not trusted ==")
# The registry holds an active peering for this VPC pair, so configure_network takes
# the reuse branch. An active peering does not prove its routes and security-group
# rule exist, so reuse must still assert them.
reg3 = install_registry_stub()
reg3._peering = {"peering_id": PCX, "connection_id": "conn-existing-0001"}
cfg3 = make_configurator()
fake3 = FakeEc2(peering_status="active")
# describe_vpcs is only called on the reuse branch (to learn the DB VPC CIDR for the
# agent-side route); the shared fake does not stub it, so provide it here.
fake3.describe_vpcs = lambda **kw: (
    fake3._record("describe_vpcs", **kw) or {"Vpcs": [{"CidrBlock": DB_CIDR}]}
)
cfg3._ec2 = fake3

saved_boto3 = nc.boto3
nc.boto3 = FakeBoto3(fake3)
try:
    result3 = cfg3.configure_network(
        db_vpc_id=DB_VPC, db_security_group_ids=DB_SG, db_port=5432,
        db_account_id="222222222222", db_region="eu-west-2",
        cross_account_session=FakeSession(fake3), db_endpoint="",
        db_subnet_ids="", instance_id="i-reuse",
    )
finally:
    nc.boto3 = saved_boto3

called3 = fake3.names()
log3 = " | ".join(result3.get("steps", []))

check(result3.get("status") == "configured", f"reports configured (got {result3.get('status')!r})")
check("reasserting routes" in log3, "the log says reuse reasserts the path, not that it trusts it")
check("create_vpc_peering_connection" not in called3, "does not recreate the peering it is reusing")
check("create_route" in called3, "builds routes on reuse — the step the early return skipped")
check("authorize_security_group_ingress" in called3, "opens the security group on reuse")
check("modify_vpc_peering_connection_options" in called3, "enables DNS on reuse")
routes3 = fake3.args_for("create_route")
cidrs3 = {r.get("DestinationCidrBlock") for r in routes3}
check(DB_CIDR in cidrs3 and AGENT_CIDR in cidrs3, "routes on both sides of the reused peering")
check(bool(routes3) and all(r.get("VpcPeeringConnectionId") == PCX for r in routes3),
      "every reasserted route points at the registered peering")
check(bool(reg3.created or reg3.references),
      "re-references the connection so teardown stays reference-counted")

# ── Two VPCs with the same CIDR ──────────────────────────────────────────────────
# Most VPC templates default to 10.0.0.0/16, so a second onboarded account routinely
# brings a VPC whose CIDR the agent route table already routes to another VPC's
# peering. Overwriting that route cuts off the first database; refusing it leaves the
# second one unreachable and reports the failure as a connection timeout. The agent
# side must instead route the second database's own address through its own peering.

OTHER_PCX = "pcx-othervpc000001"
OTHER_VPC = "vpc-other000000001"
DB_IP = "172.31.10.4"          # inside DB_CIDR, as a private RDS address is


class TakenCidrEc2(FakeEc2):
    """The agent route table already routes DB_CIDR to OTHER_PCX, a live peering to
    OTHER_VPC - or to DB_VPC itself when `holder_reaches_db` is set."""

    def __init__(self, holder_reaches_db=False):
        super().__init__(peering_status="active")
        self.holder_reaches_db = holder_reaches_db
        self.routes = {DB_CIDR: OTHER_PCX}  # agent table: destination -> peering

    def describe_vpc_peering_connections(self, **kw):
        if kw.get("VpcPeeringConnectionIds") == [OTHER_PCX]:
            self._record("describe_vpc_peering_connections", **kw)
            return {"VpcPeeringConnections": [{
                "VpcPeeringConnectionId": OTHER_PCX, "Status": {"Code": "active"},
                "RequesterVpcInfo": {"VpcId": AGENT_VPC},
                "AccepterVpcInfo": {"VpcId": DB_VPC if self.holder_reaches_db else OTHER_VPC},
            }]}
        return super().describe_vpc_peering_connections(**kw)

    def describe_route_tables(self, **kw):
        if kw.get("RouteTableIds") == [AGENT_RT]:
            self._record("describe_route_tables", **kw)
            return {"RouteTables": [{"RouteTableId": AGENT_RT, "Routes": [
                {"DestinationCidrBlock": c, "VpcPeeringConnectionId": p, "State": "active"}
                for c, p in self.routes.items()]}]}
        return super().describe_route_tables(**kw)

    def create_route(self, **kw):
        self._record("create_route", **kw)
        if kw.get("RouteTableId") == AGENT_RT and kw.get("DestinationCidrBlock") in self.routes:
            raise Exception("An error occurred (RouteAlreadyExists) when calling the CreateRoute operation")
        return {}


def agent_routes(fake):
    return [(r.get("DestinationCidrBlock"), r.get("VpcPeeringConnectionId"))
            for r in fake.args_for("create_route") if r.get("RouteTableId") == AGENT_RT]


def configure_taken(fake, reg, hosts, taken=None):
    cfg = make_configurator()
    cfg._ec2 = fake
    cfg._resolve_hosts = lambda endpoint: list(hosts)   # no DNS in tests
    # The other VPC's registered databases and their addresses; none unless a test says so.
    cfg._endpoint_addresses_in_vpc = lambda vpc_id, exclude_vpc_id="": dict(taken or {})
    return cfg._configure_peering(
        db_vpc_id=DB_VPC, db_vpc_cidr=DB_CIDR, db_security_group_ids=DB_SG,
        db_port=5432, db_account_id="222222222222", db_region="eu-west-1",
        is_cross_account=True, is_cross_region=True,
        cross_account_session=FakeSession(fake), steps=[], instance_id="i-taken",
        db_endpoint="db.example.internal",
    )


print()
print("== a VPC whose CIDR is already routed to another VPC gets host routes, not a refusal ==")
reg4 = install_registry_stub()
fake4 = TakenCidrEc2()
result4 = configure_taken(fake4, reg4, [DB_IP])
log4 = " | ".join(result4.get("steps", []))

check(result4.get("status") == "configured", f"reports configured (got {result4.get('status')!r})")
check((f"{DB_IP}/32", PCX) in agent_routes(fake4),
      "routes the database's own address through its own peering")
check("replace_route" not in fake4.names() and "delete_route" not in fake4.names(),
      "leaves the other VPC's route for the shared CIDR untouched")
check("routing only this database's address" in log4, "the log explains the host route")
check("Refusing" not in log4, "no refusal for a collision it can route around")
check(bool(reg4.created or reg4.references), "still records the connection")

print()
print("== a route that already reaches this VPC over another peering is reused ==")
reg5 = install_registry_stub()
fake5 = TakenCidrEc2(holder_reaches_db=True)
result5 = configure_taken(fake5, reg5, [DB_IP])
log5 = " | ".join(result5.get("steps", []))

check(result5.get("status") == "configured", f"reports configured (got {result5.get('status')!r})")
check(not any(c.endswith("/32") for c, _ in agent_routes(fake5)),
      "adds no host routes when the existing route already gets there")
check("reusing it" in log5, "the log says the existing route was reused")

print()
print("== an unroutable collision fails the configure, not the later connectivity check ==")
reg6 = install_registry_stub()
fake6 = TakenCidrEc2()
result6 = configure_taken(fake6, reg6, [])
err6 = result6.get("error", "")

check(result6.get("status") == "failed", f"reports failed (got {result6.get('status')!r})")
check(DB_CIDR in err6 and OTHER_PCX in err6, "names the route and the peering holding it")
check("could not be resolved" in err6, "says why it could not route the address individually")
check("authorize_security_group_ingress" in fake6.names(),
      "still runs the remaining idempotent steps")
check(bool(reg6.created or reg6.references),
      "still records the connection, so teardown stays reference-counted")

print()
print("== an address outside the database's VPC is never routed through the peering ==")
reg8 = install_registry_stub()
fake8 = TakenCidrEc2()
result8 = configure_taken(fake8, reg8, ["54.12.34.56"])   # a public address
err8 = result8.get("error", "")
check(result8.get("status") == "failed", f"reports failed (got {result8.get('status')!r})")
check(not any(c.endswith("/32") for c, _ in agent_routes(fake8)), "adds no host route for it")
check("outside the database's VPC" in err8 and "54.12.34.56" in err8, "names the address and why it was refused")
print()
print("== an address another database in the route-holding VPC also uses is refused ==")
reg9 = install_registry_stub()
fake9 = TakenCidrEc2()
result9 = configure_taken(fake9, reg9, [DB_IP], taken={DB_IP: "orders-prod"})
err9 = result9.get("error", "")
check(result9.get("status") == "failed", f"reports failed (got {result9.get('status')!r})")
check(not any(c.endswith("/32") for c, _ in agent_routes(fake9)), "adds no host route that would hijack it")
check("take over that database's traffic" in err9, "says why the host route is refused")
check("orders-prod" not in err9 and DB_IP not in err9,
      "names neither the other tenant's database nor the address (config_log is shown to this space's admins)")
# A different address in that VPC is no obstacle.
reg9b = install_registry_stub()
fake9b = TakenCidrEc2()
result9b = configure_taken(fake9b, reg9b, [DB_IP], taken={"172.31.99.9": "orders-prod"})
check(result9b.get("status") == "configured", "a non-colliding neighbour does not block the host route")

print()
print("== not KNOWING whether the address clashes refuses the host route (fail closed) ==")
# The registry cannot be read: the gate must refuse, not answer {} and let the /32 through.


def _registry_down(vpc_id, exclude_vpc_id=""):
    raise RuntimeError("the database registry could not be read (EndpointConnectionError)")


reg10 = install_registry_stub()
fake10 = TakenCidrEc2()
cfg10 = make_configurator()
cfg10._ec2 = fake10
cfg10._resolve_hosts = lambda endpoint: [DB_IP]
cfg10._endpoint_addresses_in_vpc = _registry_down
result10 = cfg10._configure_peering(
    db_vpc_id=DB_VPC, db_vpc_cidr=DB_CIDR, db_security_group_ids=DB_SG,
    db_port=5432, db_account_id="222222222222", db_region="eu-west-1",
    is_cross_account=True, is_cross_region=True,
    cross_account_session=FakeSession(fake10), steps=[], instance_id="i-taken",
    db_endpoint="db.example.internal",
)
err10 = result10.get("error", "")
check(result10.get("status") == "failed", f"unreadable registry: reports failed (got {result10.get('status')!r})")
check(not any(c.endswith("/32") for c, _ in agent_routes(fake10)), "unreadable registry: adds no host route")
check("registry could not be read" in err10 and "refused" in err10, "unreadable registry: says why")

# The holder's far VPC cannot be determined: same refusal.
reg11 = install_registry_stub()
fake11 = TakenCidrEc2()
cfg11 = make_configurator()
cfg11._ec2 = fake11
cfg11._resolve_hosts = lambda endpoint: [DB_IP]
cfg11._peer_vpc_of = lambda peering_id: ""
cfg11._endpoint_addresses_in_vpc = lambda vpc_id, exclude_vpc_id="": {}
result11 = cfg11._configure_peering(
    db_vpc_id=DB_VPC, db_vpc_cidr=DB_CIDR, db_security_group_ids=DB_SG,
    db_port=5432, db_account_id="222222222222", db_region="eu-west-1",
    is_cross_account=True, is_cross_region=True,
    cross_account_session=FakeSession(fake11), steps=[], instance_id="i-taken",
    db_endpoint="db.example.internal",
)
check(result11.get("status") == "failed", f"unknown far VPC: reports failed (got {result11.get('status')!r})")
check(not any(c.endswith("/32") for c, _ in agent_routes(fake11)), "unknown far VPC: adds no host route")
check("could not determine which VPC" in result11.get("error", ""), "unknown far VPC: says why")

# The real gate itself: an unreadable registry and an unresolvable neighbour both raise.
cfg12 = make_configurator()
_real_registry = sys.modules.get("tools.aws.dynamodb_client")


class _BrokenRegistryModule(types.ModuleType):
    class db_registry:  # noqa: N801 - mirrors the real module attribute
        @staticmethod
        def list_instances():
            raise ConnectionError("dynamodb unreachable")


sys.modules["tools.aws.dynamodb_client"] = _BrokenRegistryModule("tools.aws.dynamodb_client")
try:
    cfg12._endpoint_addresses_in_vpc(OTHER_VPC)
    check(False, "gate: unreadable registry raises")
except RuntimeError as e:
    check("registry could not be read" in str(e), "gate: unreadable registry raises")


class _OneRecordModule(types.ModuleType):
    class db_registry:  # noqa: N801
        @staticmethod
        def list_instances():
            return [{"vpc_id": OTHER_VPC, "endpoint": "ghost.example.internal", "display_name": "orders-prod"}]


sys.modules["tools.aws.dynamodb_client"] = _OneRecordModule("tools.aws.dynamodb_client")
cfg12._resolve_hosts = lambda endpoint: []
try:
    cfg12._endpoint_addresses_in_vpc(OTHER_VPC)
    check(False, "gate: an unresolvable neighbour raises")
except RuntimeError as e:
    check("could not be resolved" in str(e) and "orders-prod" not in str(e),
          "gate: an unresolvable neighbour raises, without naming the other tenant's database")
cfg12._resolve_hosts = lambda endpoint: ["172.31.99.9"]
check(cfg12._endpoint_addresses_in_vpc(OTHER_VPC) == {"172.31.99.9": "orders-prod"},
      "gate: a resolvable neighbour is reported with its address")
if _real_registry is not None:
    sys.modules["tools.aws.dynamodb_client"] = _real_registry
else:
    sys.modules.pop("tools.aws.dynamodb_client", None)

print()
print("== the far side of a peering is resolved from the agent VPC's point of view ==")
cfgv = make_configurator(); cfgv._ec2 = TakenCidrEc2()
check(cfgv._peer_vpc_of(OTHER_PCX) == OTHER_VPC, "requester=agent -> accepter is the peer")
check(cfgv._ip_in_cidr("172.31.10.4", "172.31.0.0/16") and not cfgv._ip_in_cidr("10.0.10.4", "172.31.0.0/16")
      and not cfgv._ip_in_cidr("garbage", "172.31.0.0/16"), "CIDR membership, with bad input as not-inside")
print()
print("== the clash path routes EVERY member endpoint and reports what it owns ==")
MEMBER_DNS = {"db.example.internal": [DB_IP], "db-ro.example.internal": ["172.31.10.6"],
              "db-1.example.internal": [DB_IP], "db-2.example.internal": ["172.31.10.6"],
              "db-3.example.internal": ["172.31.10.7"]}


def configure_members(fake, dns, taken=None):
    cfg = make_configurator()
    cfg._ec2 = fake
    cfg._resolve_hosts = lambda endpoint: list(dns.get(endpoint, []))
    cfg._endpoint_addresses_in_vpc = lambda vpc_id, exclude_vpc_id="": dict(taken or {})
    return cfg._configure_peering(
        db_vpc_id=DB_VPC, db_vpc_cidr=DB_CIDR, db_security_group_ids=DB_SG,
        db_port=5432, db_account_id="222222222222", db_region="eu-west-1",
        is_cross_account=True, is_cross_region=True,
        cross_account_session=FakeSession(fake), steps=[], instance_id="i-taken",
        db_endpoint="db.example.internal",
        db_member_endpoints=["db.example.internal", "db-ro.example.internal", "db-1.example.internal",
                             "db-2.example.internal", "db-3.example.internal"],
    )


install_registry_stub()
fake13 = TakenCidrEc2()
result13 = configure_members(fake13, MEMBER_DNS)
hosts13 = sorted(c for c, _ in agent_routes(fake13) if c.endswith("/32"))
check(result13.get("status") == "configured", f"configured (got {result13.get('status')!r})")
check(hosts13 == [f"{DB_IP}/32", "172.31.10.6/32", "172.31.10.7/32"],
      f"one /32 per member address, each once (got {hosts13})")
check(result13.get("route_mode") == "host" and result13.get("db_vpc_cidr") == DB_CIDR,
      "returns route_mode=host and the DB VPC CIDR")
check(sorted(o["ip"] for o in result13.get("host_routes", [])) == [DB_IP, "172.31.10.6", "172.31.10.7"],
      "returns the /32s the record now owns")

install_registry_stub()
fake14 = TakenCidrEc2()
result14 = configure_members(fake14, dict(MEMBER_DNS, **{"db-3.example.internal": ["54.1.2.3"]}))
log14 = " | ".join(result14.get("steps", []))
check(result14.get("status") == "configured", "a partly refused member set is still a success")
check("⚠ Host route refused" in log14 and "54.1.2.3" in log14, "with a ⚠ step naming the refusal")
check("54.1.2.3/32" not in [c for c, _ in agent_routes(fake14)], "and no route for the refused address")

install_registry_stub()
fake15 = TakenCidrEc2()
result15 = configure_members(fake15, MEMBER_DNS, taken={DB_IP: "a", "172.31.10.6": "b", "172.31.10.7": "c"})
check(result15.get("status") == "failed" and "take over that database's traffic" in result15.get("error", ""),
      "every address refused: Configure fails visibly")
check(not any(c.endswith("/32") for c, _ in agent_routes(fake15)), "and adds no host route")

install_registry_stub()
fake16 = TakenCidrEc2()
fake16.holder_reaches_db = False
cfg16 = make_configurator()
cfg16._ec2 = fake16
cfg16._ensure_route = lambda *a, **k: None   # the VPC-wide route goes in
check(cfg16._ensure_agent_routes(DB_VPC, DB_CIDR, PCX, "db.example.internal", []) == {"route_mode": "vpc"},
      "no clash: route_mode=vpc")
check(make_configurator()._ensure_agent_routes.__doc__ and "route_mode" in
      make_configurator()._ensure_agent_routes.__doc__, "the docstring states the route modes")
fake16b = TakenCidrEc2(holder_reaches_db=True)
cfg16b = make_configurator()
cfg16b._ec2 = fake16b
check(cfg16b._ensure_agent_routes(DB_VPC, DB_CIDR, PCX, "db.example.internal", []) == {"route_mode": "reused"},
      "an existing route that reaches the VPC: route_mode=reused")

print()
print("== teardown with other references deletes only this record's unshared /32s ==")


class _PeersModule(types.ModuleType):
    rows = []

    class db_registry:  # noqa: N801
        @staticmethod
        def list_instances():
            if _PeersModule.rows is None:
                raise ConnectionError("dynamodb unreachable")
            return list(_PeersModule.rows)


class SharedRegistry:
    """The peering still has another reference after this one goes."""

    def get_connection(self, cid):
        return {"connection_id": cid, "ref_count": 2}

    def remove_reference(self, cid, iid):
        return {"connection_id": cid, "ref_count": 1}


def cleanup_shared(fake, rows):
    _PeersModule.rows = rows
    saved = sys.modules.get("tools.aws.dynamodb_client")
    sys.modules["tools.aws.dynamodb_client"] = _PeersModule("tools.aws.dynamodb_client")
    nr = sys.modules["tools.aws.network_registry"]
    saved_reg = nr.network_registry
    nr.network_registry = SharedRegistry()
    cfg = make_configurator()
    cfg._ec2 = fake
    try:
        return cfg.cleanup_peering(db_vpc_id=DB_VPC, peering_id=PCX, db_region="eu-west-1",
                                   instance_id="i-taken",
                                   host_routes=[{"ip": DB_IP, "host": "a"}, {"ip": "172.31.10.6", "host": "b"}])
    finally:
        nr.network_registry = saved_reg
        if saved is not None:
            sys.modules["tools.aws.dynamodb_client"] = saved
        else:
            sys.modules.pop("tools.aws.dynamodb_client", None)


install_registry_stub()
fake17 = TakenCidrEc2()
fake17.routes = {DB_CIDR: OTHER_PCX, f"{DB_IP}/32": PCX, "172.31.10.6/32": PCX, "172.31.10.9/32": PCX}
out17 = cleanup_shared(fake17, [{"instance_id": "i-neighbour", "network_peering_id": PCX,
                                 "host_routes": [{"ip": "172.31.10.6", "host": "n"}]},
                                {"instance_id": "i-taken", "network_peering_id": PCX,
                                 "host_routes": [{"ip": DB_IP}]}])
deleted17 = [r.get("DestinationCidrBlock") for r in fake17.args_for("delete_route")]
check(deleted17 == [f"{DB_IP}/32"], f"only this record's unshared /32 is deleted (got {deleted17})")
check("delete_vpc_peering_connection" not in fake17.names(), "the shared peering stays")

install_registry_stub()
fake18 = TakenCidrEc2()
fake18.routes = {DB_CIDR: OTHER_PCX, f"{DB_IP}/32": PCX}
out18 = cleanup_shared(fake18, None)
check(not fake18.args_for("delete_route"), "registry unreadable: nothing is deleted")
check(any("Could not read the registry" in s for s in out18["steps"]), "and the step says why")

print()
print("== a Reconfigure reconciles the /32s the record already owns ==")
OWNED3 = [{"ip": DB_IP, "host": "db-1.example.internal"}, {"ip": "172.31.10.6", "host": "db-2.example.internal"},
          {"ip": "172.31.10.7", "host": "db-3.example.internal"}]


def reconfigure(fake, dns, owned, rows=(), taken=None, members=("db-1", "db-2")):
    """`_ensure_agent_routes` as Configure calls it on a Reconfigure, with the record's
    current host_routes and a registry of `rows` (None: unreadable)."""
    _PeersModule.rows = None if rows is None else list(rows)
    saved = sys.modules.get("tools.aws.dynamodb_client")
    sys.modules["tools.aws.dynamodb_client"] = _PeersModule("tools.aws.dynamodb_client")
    cfg = make_configurator()
    cfg._ec2 = fake
    cfg._resolve_hosts = lambda endpoint: list(dns.get(endpoint, []))
    cfg._endpoint_addresses_in_vpc = lambda vpc_id, exclude_vpc_id="": dict(taken or {})
    steps = []
    try:
        out = cfg._ensure_agent_routes(DB_VPC, DB_CIDR, PCX, "db.example.internal", steps,
                                       hosts=[f"{m}.example.internal" for m in members],
                                       owned=owned, instance_id="i-taken")
    finally:
        if saved is not None:
            sys.modules["tools.aws.dynamodb_client"] = saved
        else:
            sys.modules.pop("tools.aws.dynamodb_client", None)
    return out, steps


def owned_fake():
    fake = TakenCidrEc2()
    fake.routes = {DB_CIDR: OTHER_PCX, f"{DB_IP}/32": PCX, "172.31.10.6/32": PCX, "172.31.10.7/32": PCX}
    return fake


install_registry_stub()
fake19 = owned_fake()
out19, steps19 = reconfigure(fake19, MEMBER_DNS, OWNED3)
deleted19 = [r.get("DestinationCidrBlock") for r in fake19.args_for("delete_route")]
check(deleted19 == ["172.31.10.7/32"], f"a departed member's /32 is removed (got {deleted19})")
check(sorted(o["ip"] for o in out19["host_routes"]) == [DB_IP, "172.31.10.6"],
      "and the record keeps owning the rest")
check(not [c for c, _ in agent_routes(fake19) if c.endswith("/32")], "nothing it already routes is re-created")

install_registry_stub()
fake20 = owned_fake()
out20, _ = reconfigure(fake20, MEMBER_DNS, OWNED3[:2], taken={"172.31.10.6": "orders-prod"})
deleted20 = [r.get("DestinationCidrBlock") for r in fake20.args_for("delete_route")]
check(deleted20 == ["172.31.10.6/32"], f"an owned address now taken by another database is removed (got {deleted20})")
check([o["ip"] for o in out20["host_routes"]] == [DB_IP], "and no longer owned")

install_registry_stub()
fake21 = owned_fake()
out21, steps21 = reconfigure(fake21, MEMBER_DNS, OWNED3, rows=None)
check(not fake21.args_for("delete_route"), "registry unreadable: no owned /32 is deleted")
check(sorted(o["ip"] for o in out21["host_routes"]) == [DB_IP, "172.31.10.6", "172.31.10.7"],
      "and every owned /32 stays tracked")

install_registry_stub()
fake22 = owned_fake()
out22, _ = reconfigure(fake22, {k: v for k, v in MEMBER_DNS.items() if k != "db-2.example.internal"}, OWNED3[:2])
check(not fake22.args_for("delete_route") and len(out22["host_routes"]) == 2,
      "an owned member whose name does not resolve keeps its /32")

install_registry_stub()
fake23 = owned_fake()
out23, _ = reconfigure(fake23, MEMBER_DNS, OWNED3,
                       rows=[{"instance_id": "i-neighbour", "network_peering_id": PCX,
                              "host_routes": [{"ip": "172.31.10.7", "host": "n"}]}])
check(not fake23.args_for("delete_route") and "172.31.10.7" not in {o["ip"] for o in out23["host_routes"]},
      "a departed address another record on the peering owns is released, not deleted")

install_registry_stub()
fake24 = owned_fake()
fake24.routes.pop(DB_CIDR)   # the VPC-wide route is free now: Reconfigure lands on vpc mode
out24, steps24 = reconfigure(fake24, MEMBER_DNS, OWNED3)
deleted24 = sorted(r.get("DestinationCidrBlock") for r in fake24.args_for("delete_route"))
check(out24["route_mode"] == "vpc", "the VPC-wide route is used")
check(deleted24 == [f"{DB_IP}/32", "172.31.10.6/32", "172.31.10.7/32"],
      f"a switch to vpc mode removes the old /32s (got {deleted24})")
check(out24["host_routes"] == [], "and the record owns none")

install_registry_stub()
fake25 = owned_fake()
fake25.routes.pop(DB_CIDR)
out25, _ = reconfigure(fake25, MEMBER_DNS, OWNED3, rows=None)
check(not fake25.args_for("delete_route") and len(out25["host_routes"]) == 3,
      "switch to vpc with the registry unreadable: kept and still tracked for teardown")

install_registry_stub()
fake26 = owned_fake()
fake26.routes.pop(DB_CIDR)
out26, _ = reconfigure(fake26, MEMBER_DNS, [])
check(out26 == {"route_mode": "vpc"} and not fake26.args_for("delete_route"), "a record owning none: unchanged")

install_registry_stub()
fake27 = owned_fake()
try:
    reconfigure(fake27, MEMBER_DNS, OWNED3[:2],
                taken={DB_IP: "a", "172.31.10.6": "b"})
    check(False, "every address now taken: Configure fails")
except RuntimeError as e:
    check("take over that database's traffic" in str(e), "every address now taken: Configure fails")
deleted27 = sorted(r.get("DestinationCidrBlock") for r in fake27.args_for("delete_route"))
check(deleted27 == [f"{DB_IP}/32", "172.31.10.6/32"], f"and the hijacking /32s are still removed (got {deleted27})")

print()
print("== Configure passes the record's host_routes into the network path ==")
_app_src = (pathlib.Path(__file__).resolve().parent.parent / "backend" / "app.py").read_text()
check('owned_host_routes=instance.get("host_routes") or []' in _app_src,
      "_configure_single_db hands the owned /32s to configure_network")

print()
print("== teardown removes the host routes and only them ==")
reg7 = install_registry_stub()
cfg7 = make_configurator()
fake7 = TakenCidrEc2()
fake7.routes = {DB_CIDR: OTHER_PCX, f"{DB_IP}/32": PCX}
cfg7._ec2 = fake7
saved_boto3 = nc.boto3
nc.boto3 = FakeBoto3(fake7)
try:
    cfg7.cleanup_peering(db_vpc_id=DB_VPC, peering_id=PCX, db_region="eu-west-1",
                         cross_account_session=FakeSession(fake7), instance_id="i-taken")
finally:
    nc.boto3 = saved_boto3

deleted7 = [r.get("DestinationCidrBlock") for r in fake7.args_for("delete_route")
            if r.get("RouteTableId") == AGENT_RT]
check(deleted7 == [f"{DB_IP}/32"],
      f"deletes the host route over the torn-down peering and nothing else (got {deleted7})")
check("delete_vpc_peering_connection" in fake7.names(), "deletes the peering")

print()
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
