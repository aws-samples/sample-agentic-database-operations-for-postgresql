# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The DB-side route-conflict claim: automatic only when provably safe, otherwise refused.

A conflicting agent-CIDR route in the database VPC points at a peering other than the one
being asserted. The configurator classifies that holder and acts:

  DEAD           (peering gone / not active)               -> auto-claim, continue
  OURS           (tagged with THIS agent VPC, 0 live refs)  -> auto-claim, continue
  ALIVE_FOREIGN  (active, another installation's)          -> refuse, fail fast, no mutation
  UNKNOWN        (describe denied, or not a peering)       -> refuse, fail fast, no mutation

Hard scope guard: a claim may ONLY repoint a route whose destination is the agent's own
CIDR. It never replaces or deletes any other customer route, and never deletes the old
peering (that stays with the reference-counted teardown).

Hermetic: the real NetworkConfigurator source runs against fake EC2 / registry objects. No
AWS, no network.

Run:  python3 backend/tests/test_route_conflict_claim.py
"""
import os
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
NET_SRC = (ROOT / "tools/aws/network_client.py").read_text(encoding="utf-8")
passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


if "boto3" not in sys.modules:
    _b = types.ModuleType("boto3")
    _b.client = lambda *a, **k: (_ for _ in ()).throw(AssertionError("boto3.client must not be called"))
    sys.modules["boto3"] = _b

os.environ["AGENT_VPC_ID"] = "vpc-ME"
os.environ["AGENT_VPC_CIDR"] = "172.20.0.0/16"
os.environ["AGENT_PRIVATE_ROUTE_TABLE_ID"] = "rtb-agent"
os.environ.pop("RESOURCE_PREFIX", None)

ns = {"__name__": "tools.aws.network_client"}
exec(compile(NET_SRC, str(ROOT / "tools/aws/network_client.py"), "exec"), ns)  # noqa: S102 - pinning shipped source
NetworkConfigurator, RouteConflict = ns["NetworkConfigurator"], ns["RouteConflict"]
from tools.resource_names import agent_vpc_tag_key  # noqa: E402

TAG = agent_vpc_tag_key()


class PeerEC2:
    """DB-account EC2. `peerings` maps pcx id -> describe result (or an Exception to raise)."""
    def __init__(self, holder="pcx-OLD", peerings=None):
        self.holder, self.peerings, self.replaced = holder, peerings or {}, []

    def describe_route_tables(self, **kw):
        if "Filters" in kw:
            return {"RouteTables": [{"RouteTableId": "rtb-db"}]}
        return {"RouteTables": [{"Routes": [{"DestinationCidrBlock": "172.20.0.0/16",
                                            "VpcPeeringConnectionId": self.holder, "State": "active"}]}]}

    def create_route(self, **kw):
        raise Exception("RouteAlreadyExists")

    def replace_route(self, **kw):
        self.replaced.append(kw)

    def describe_vpc_peering_connections(self, VpcPeeringConnectionIds):
        r = self.peerings.get(VpcPeeringConnectionIds[0])
        if isinstance(r, Exception):
            raise r
        return {"VpcPeeringConnections": [r] if r else []}

    def describe_security_groups(self, **kw):
        return {"SecurityGroups": [{"IpPermissions": []}]}

    def authorize_security_group_ingress(self, **kw):
        return {}


class Sess:
    def __init__(self, ec2): self._ec2 = ec2
    def client(self, *a, **k): return self._ec2


class Reg:
    def __init__(self, by_peering=None):
        self.by_peering = by_peering or {}
        self.created, self.refs = [], []

    def get_connection(self, cid): return None
    def create_connection(self, d): self.created.append(d)
    def add_reference(self, cid, iid): self.refs.append(iid)
    def update_connection(self, *a, **k): pass
    def find_by_peering_id(self, pcx, db_vpc): return self.by_peering.get(pcx)


def peering(status="active", req_vpc="vpc-OTHER", acct="999999999999", tags=None):
    return {"Status": {"Code": status}, "RequesterVpcInfo": {"VpcId": req_vpc, "OwnerId": acct},
            "Tags": [{"Key": k, "Value": v} for k, v in (tags or {}).items()]}


def run(peer, reg):
    mod = types.ModuleType("tools.aws.network_registry")
    mod.network_registry, mod.NetworkRegistry = reg, types.SimpleNamespace(make_connection_id=lambda a, b: f"{a}|{b}")
    sys.modules["tools.aws.network_registry"] = mod
    cfg = NetworkConfigurator.__new__(NetworkConfigurator)
    cfg._ec2 = types.SimpleNamespace(describe_route_tables=lambda **k: {"RouteTables": [{"Routes": []}]},
                                     create_route=lambda **k: {})
    cfg._find_existing_peering = lambda v: {"peering_id": "pcx-NEW", "status": "active"}
    cfg._ensure_agent_routes = lambda *a, **k: {"route_mode": "vpc"}
    cfg._release_host_routes = lambda *a, **k: {}
    import inspect
    for name, _ in inspect.getmembers(NetworkConfigurator, inspect.isfunction):
        if name.startswith("_") and ("dns" in name.lower() or "accept" in name.lower() or "wait" in name.lower()):
            setattr(cfg, name, lambda *a, **k: None)
    sig = inspect.signature(NetworkConfigurator._configure_peering)
    kw = dict(db_vpc_id="vpc-db", db_vpc_cidr="10.0.0.0/16", db_security_group_ids="sg-db", db_port=5432,
              db_account_id="222222222222", db_region="eu-west-1", is_cross_account=True, is_cross_region=True,
              cross_account_session=Sess(peer), steps=[], instance_id="db-1", db_endpoint="db.example",
              db_member_endpoints=(), owned_host_routes=())
    return cfg, cfg._configure_peering(**{k: v for k, v in kw.items() if k in sig.parameters})


# ── DEAD: peering no longer active -> auto-claim ──
print("DEAD holder (peering not active) is auto-claimed and onboarding continues")
peer = PeerEC2(peerings={"pcx-OLD": peering(status="deleted")})
cfg, out = run(peer, Reg())
check(out["status"] == "configured", f"status configured (got {out['status']!r})")
check(len(peer.replaced) == 1 and peer.replaced[0]["VpcPeeringConnectionId"] == "pcx-NEW"
      and peer.replaced[0]["DestinationCidrBlock"] == "172.20.0.0/16" and peer.replaced[0]["RouteTableId"] == "rtb-db",
      "exactly the agent-CIDR route in rtb-db is repointed to pcx-NEW")
check(any("reclaimed from pcx-OLD" in s for s in out["steps"]), "the step log says what was reclaimed and why")

print("DEAD holder (peering no longer exists) is auto-claimed")
peer = PeerEC2(peerings={"pcx-OLD": Exception("InvalidVpcPeeringConnectionID.NotFound")})
cfg, out = run(peer, Reg())
check(out["status"] == "configured" and len(peer.replaced) == 1, "a vanished peering is treated as dead")

# ── OURS: tagged with this agent VPC and no live reference -> auto-claim ──
print("OURS holder (tagged with THIS agent VPC, zero live refs) is auto-claimed")
peer = PeerEC2(peerings={"pcx-OLD": peering(req_vpc="vpc-ME", acct="111111111111", tags={TAG: "vpc-ME"})})
cfg, out = run(peer, Reg(by_peering={"pcx-OLD": {"ref_count": 0}}))
check(out["status"] == "configured" and len(peer.replaced) == 1, "our own zero-ref leftover is reclaimed")

print("OURS by tag but still LIVE-referenced is NOT auto-claimed")
peer = PeerEC2(peerings={"pcx-OLD": peering(req_vpc="vpc-ME", tags={TAG: "vpc-ME"})})
cfg, out = run(peer, Reg(by_peering={"pcx-OLD": {"ref_count": 2}}))
check(out["status"] == "failed" and peer.replaced == [], "a peering of ours that something still uses is refused")

# ── ALIVE_FOREIGN: active, another installation's -> refuse, no mutation ──
print("ALIVE_FOREIGN holder (active, another installation) is refused with no mutation")
peer = PeerEC2(peerings={"pcx-OLD": peering(req_vpc="vpc-THEM", acct="999999999999")})
cfg, out = run(peer, Reg())
check(out["status"] == "failed", "status failed")
check(peer.replaced == [], "NOTHING was replaced")
check("Claiming it would disconnect" in out["error"] and "vpc-THEM" in out["error"] and "999999999999" in out["error"],
      "the error names the far end and the consequence of claiming")
rc = out.get("route_conflict") or {}
check(rc.get("kind") == "alive_foreign" and rc.get("claimable") is True and rc.get("holder") == "pcx-OLD",
      "a structured route_conflict is returned for the console's Claim action")
check(rc.get("peering_id") == "pcx-NEW" and rc.get("route_table_id") == "rtb-db" and rc.get("cidr") == "172.20.0.0/16",
      "the conflict records exactly what a claim would repoint")

print("same-prefix other deployment (ManagedBy alike, DIFFERENT agent VPC tag) is foreign, not ours")
peer = PeerEC2(peerings={"pcx-OLD": peering(req_vpc="vpc-THEM", tags={TAG: "vpc-THEM", "ManagedBy": "dbops-agent"})})
cfg, out = run(peer, Reg())
check(out["status"] == "failed" and peer.replaced == [], "a different agent VPC's peering is never auto-claimed")

# ── UNKNOWN: cannot classify -> refuse, fail closed ──
print("UNKNOWN holder (describe denied) is refused, fail closed")
peer = PeerEC2(peerings={"pcx-OLD": Exception("UnauthorizedOperation: You are not authorized")})
cfg, out = run(peer, Reg())
check(out["status"] == "failed" and peer.replaced == [], "permission denied never leads to a claim")
check("refusing to assume it is unused" in out["error"], "the error says why it was not classified")

print("a non-peering holder (e.g. a transit gateway) is never claimed")
peer = PeerEC2(holder="tgw-1234")
cfg, out = run(peer, Reg())
check(out["status"] == "failed" and peer.replaced == [], "a TGW/NAT/ENI target is refused")

# ── SCOPE GUARD: the claim refuses any non-agent-CIDR destination before any AWS call ──
print("scope guard: _claim_conflicting_route refuses any route that is not the agent CIDR")
cfg = NetworkConfigurator.__new__(NetworkConfigurator)
called = []
guard_ec2 = types.SimpleNamespace(replace_route=lambda **k: called.append(k))
try:
    cfg._claim_conflicting_route("rtb-db", "10.0.0.0/16", "pcx-NEW", ec2_client=guard_ec2)
    check(False, "a non-agent CIDR must raise")
except PermissionError as e:
    check("may only repoint the agent's own CIDR" in str(e), "it raises PermissionError naming the rule")
check(called == [], "no AWS call was made for the refused CIDR")
cfg._claim_conflicting_route("rtb-db", "172.20.0.0/16", "pcx-NEW", ec2_client=guard_ec2)
check(len(called) == 1 and called[0]["DestinationCidrBlock"] == "172.20.0.0/16", "the agent CIDR is allowed")

print("the claim repoints only; it never deletes the old peering")
check("delete_vpc_peering_connection" not in NET_SRC.split("def _claim_conflicting_route")[1].split("def _existing_route")[0],
      "no peering deletion inside the claim")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
