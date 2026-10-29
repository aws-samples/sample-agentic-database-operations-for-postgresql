#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The cross-Region host-route planner (backend/tools/aws/host_routes.py).

On the CIDR-clash path a database is reached by one `/32` per member address. Configure
creates them and the API task repairs them when Tier 1 flags a moved member. Both plan
through ``host_routes.plan`` and carry it out with ``host_routes.apply``, so the safety
rules are pinned here once: never route outside the database's VPC CIDR, never take an
address another registered database uses, never overwrite another live `/32`, fail
closed when that cannot be known, and only ever delete a `/32` the record owns that no
other record on the same peering owns. ``dial_gate`` decides which members Tier 1 and
the reader scan may dial on a record's network path.

Pure functions plus a fake EC2 route table. Hermetic, no AWS, no DNS.

Run:  python3 tests/test_host_routes.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from tools.aws import host_routes as hr  # noqa: E402

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


CIDR = "10.0.0.0/16"
OURS, OTHER = "pcx-ours", "pcx-other"
RT = "rtb-agent"


class Err(Exception):
    def __init__(self, code):
        super().__init__(f"An error occurred ({code})")
        self.response = {"Error": {"Code": code}}


class FakeEc2:
    """An agent route table: destination -> {target, state}. Records the call order."""

    def __init__(self, routes=None, fail=None):
        self.routes = dict(routes or {})
        self.calls = []
        self.fail = dict(fail or {})   # (op, cidr) -> error code

    def _route_list(self):
        out = []
        for cidr, (target, state) in self.routes.items():
            r = {"DestinationCidrBlock": cidr, "State": state}
            if target.startswith("pcx-"):
                r["VpcPeeringConnectionId"] = target
            else:
                r["GatewayId"] = target
            out.append(r)
        return out

    def describe_route_tables(self, RouteTableIds):
        return {"RouteTables": [{"RouteTableId": RouteTableIds[0], "Routes": self._route_list()}]}

    def _maybe_fail(self, op, cidr):
        code = self.fail.get((op, cidr))
        if code:
            raise Err(code)

    def create_route(self, RouteTableId, DestinationCidrBlock, VpcPeeringConnectionId):
        self.calls.append(("create", DestinationCidrBlock))
        self._maybe_fail("create", DestinationCidrBlock)
        if DestinationCidrBlock in self.routes:
            raise Err("RouteAlreadyExists")
        self.routes[DestinationCidrBlock] = (VpcPeeringConnectionId, "active")

    def replace_route(self, RouteTableId, DestinationCidrBlock, VpcPeeringConnectionId):
        self.calls.append(("replace", DestinationCidrBlock))
        self._maybe_fail("replace", DestinationCidrBlock)
        self.routes[DestinationCidrBlock] = (VpcPeeringConnectionId, "active")

    def delete_route(self, RouteTableId, DestinationCidrBlock):
        self.calls.append(("delete", DestinationCidrBlock))
        self._maybe_fail("delete", DestinationCidrBlock)
        if DestinationCidrBlock not in self.routes:
            raise Err("InvalidRoute.NotFound")
        del self.routes[DestinationCidrBlock]


def run(ec2, owned, desired, taken=None, other_owned=(), gate_reason="", gate_known=True):
    p = hr.plan(owned, desired, CIDR, OURS, ec2._route_list(),
                (taken or {}) if gate_known else None, other_owned, gate_reason)
    return p, hr.apply(ec2, RT, p, OURS)


def own(*ips):
    return [{"ip": ip, "host": f"h-{ip}"} for ip in ips]


print("== a member's address moved: the new /32 first, then the old one goes ==")
ec2 = FakeEc2({CIDR: (OTHER, "active"), "10.0.1.5/32": (OURS, "active"), "10.0.2.6/32": (OURS, "active")})
p, res = run(ec2, own("10.0.1.5", "10.0.2.6"), {"10.0.1.5": "db-a", "10.0.3.7": "db-b"})
check(p.add == [("10.0.3.7", "db-b")] and p.keep == [("10.0.1.5", "db-a")] and p.remove == ["10.0.2.6"],
      "plan: add the new address, keep the unchanged one, remove the old one")
check(ec2.calls == [("create", "10.0.3.7/32"), ("delete", "10.0.2.6/32")], "create before delete")
check(res["owned"] == [{"ip": "10.0.1.5", "host": "db-a"}, {"ip": "10.0.3.7", "host": "db-b"}],
      "ownership follows the route table")
check(res["added"] == ["10.0.3.7"] and res["removed"] == ["10.0.2.6"], "added / removed reported")
check(CIDR in ec2.routes and ec2.routes[CIDR][0] == OTHER, "the other VPC's VPC-wide route is untouched")

print("== a departed member's route is removed ==")
ec2 = FakeEc2({"10.0.1.5/32": (OURS, "active"), "10.0.2.6/32": (OURS, "active")})
p, res = run(ec2, own("10.0.1.5", "10.0.2.6"), {"10.0.1.5": "db-a"})
check(p.remove == ["10.0.2.6"] and "10.0.2.6/32" not in ec2.routes, "its /32 is deleted")
check([o["ip"] for o in res["owned"]] == ["10.0.1.5"], "and no longer owned")

print("== a blackhole /32 is replaced, not refused ==")
ec2 = FakeEc2({"10.0.1.5/32": (OURS, "blackhole")})
p, res = run(ec2, [], {"10.0.1.5": "db-a"})
check(p.replace == [("10.0.1.5", "db-a")] and ec2.calls == [("replace", "10.0.1.5/32")], "replace_route")
check(ec2.routes["10.0.1.5/32"] == (OURS, "active") and res["owned"][0]["ip"] == "10.0.1.5", "now ours")

print("== an address outside the database's VPC is refused ==")
ec2 = FakeEc2()
p, res = run(ec2, [], {"54.12.34.56": "db-a", "10.0.1.5": "db-b"})
check([ip for ip, _ in p.refused] == ["54.12.34.56"] and "outside the database's VPC" in p.refused[0][1],
      "refused with the outside-CIDR reason")
check(ec2.calls == [("create", "10.0.1.5/32")], "only the in-VPC address is routed")
check(any("⚠ Host route refused" in s for s in res["steps"]), "the refusal is a ⚠ step")

print("== an address another registered database uses is refused ==")
ec2 = FakeEc2()
p, res = run(ec2, [], {"10.0.1.5": "db-a"}, taken={"10.0.1.5": "orders-prod"})
check(not ec2.calls and p.refused and "take over that database's traffic" in p.refused[0][1],
      "no /32 that would hijack it")
check("orders-prod" not in " ".join(res["steps"]) and "10.0.1.5" not in p.refused[0][1],
      "the reason names neither the other tenant's database nor the address")

print("== a foreign /32 (another live target) is left alone ==")
ec2 = FakeEc2({"10.0.1.5/32": (OTHER, "active")})
p, res = run(ec2, [], {"10.0.1.5": "db-a"})
check(not ec2.calls and p.refused and OTHER in p.refused[0][1], "refused, not overwritten")
ec2 = FakeEc2({"10.0.1.5/32": ("tgw-1", "active")})
p, _ = run(ec2, own("10.0.1.5"), {"10.0.1.5": "db-a"})
check(not ec2.calls and p.forget == ["10.0.1.5"], "an owned address now routed elsewhere is forgotten, not deleted")

print("== gate unknown: no adds, removes still run ==")
ec2 = FakeEc2({"10.0.1.5/32": (OURS, "active"), "10.0.2.6/32": (OURS, "active")})
p, res = run(ec2, own("10.0.1.5", "10.0.2.6"), {"10.0.1.5": "db-a", "10.0.3.7": "db-b"},
             gate_known=False, gate_reason="the database registry could not be read; refused")
check(not p.add and not p.replace, "nothing is added")
check(p.keep == [("10.0.1.5", "db-a")], "an address already routed to us is kept")
check(p.refused == [("10.0.3.7", "the database registry could not be read; refused")], "the new one is refused with the reason")
check(ec2.calls == [("delete", "10.0.2.6/32")], "our own departed /32 is still removed")

print("== an address another record on the same peering owns is forgotten, not deleted ==")
ec2 = FakeEc2({"10.0.2.6/32": (OURS, "active")})
p, res = run(ec2, own("10.0.2.6"), {}, other_owned={"10.0.2.6"})
check(p.forget == ["10.0.2.6"] and not ec2.calls and "10.0.2.6/32" in ec2.routes, "the shared /32 stays")
check(res["owned"] == [], "and leaves this record's ownership")

print("== a refused address this record still routes is removed (it would hijack) ==")
ec2 = FakeEc2({"10.0.1.5/32": (OURS, "active")})
p, _ = run(ec2, own("10.0.1.5"), {"10.0.1.5": "db-a"}, taken={"10.0.1.5": "orders-prod"})
check(p.remove == ["10.0.1.5"] and ("delete", "10.0.1.5/32") in ec2.calls, "deleted")

print("== RouteAlreadyExists on create is re-read ==")


class RacingEc2(FakeEc2):
    """Another writer creates the route between our read and our create."""

    def __init__(self, target):
        super().__init__()
        self.target = target

    def create_route(self, RouteTableId, DestinationCidrBlock, VpcPeeringConnectionId):
        self.routes[DestinationCidrBlock] = (self.target, "active")
        return super().create_route(RouteTableId, DestinationCidrBlock, VpcPeeringConnectionId)


ec2 = RacingEc2(OURS)
p, res = run(ec2, [], {"10.0.1.5": "db-a"})
check(res["owned"] == [{"ip": "10.0.1.5", "host": "db-a"}], "ours: kept")
ec2 = RacingEc2(OTHER)
p, res = run(ec2, [], {"10.0.1.5": "db-a"})
check(res["owned"] == [] and res["refused"] and OTHER in res["refused"][-1][1], "theirs: refused")

print("== other failures are steps; ownership is unchanged ==")
ec2 = FakeEc2({"10.0.2.6/32": (OURS, "active")},
              fail={("create", "10.0.3.7/32"): "RouteLimitExceeded", ("delete", "10.0.2.6/32"): "Throttling"})
p, res = run(ec2, own("10.0.2.6"), {"10.0.3.7": "db-b"})
check("✕ 10.0.3.7/32: RouteLimitExceeded" in res["steps"] and "✕ 10.0.2.6/32: Throttling" in res["steps"],
      "each failure is a ✕ step with the short error")
check([o["ip"] for o in res["owned"]] == ["10.0.2.6"], "the failed add is not owned; the failed remove stays owned")
ec2 = FakeEc2({"10.0.2.6/32": (OURS, "active")}, fail={("delete", "10.0.2.6/32"): "InvalidRoute.NotFound"})
p, res = run(ec2, own("10.0.2.6"), {})
check(res["removed"] == ["10.0.2.6"] and res["owned"] == [], "NotFound on delete counts as done")

print("== a no-op plan changes nothing ==")
ec2 = FakeEc2({"10.0.1.5/32": (OURS, "active")})
p, res = run(ec2, own("10.0.1.5"), {"10.0.1.5": "db-a"})
check(not p.changes and not ec2.calls and not res["steps"], "nothing to do")

print("== a name that does not resolve keeps its owned /32 (unknown is not gone) ==")
ec2 = FakeEc2({"10.0.1.5/32": (OURS, "active"), "10.0.2.6/32": (OURS, "active")})
owned = [{"ip": "10.0.1.5", "host": "db-a"}, {"ip": "10.0.2.6", "host": "db-b"}]
p = hr.plan(owned, {"10.0.1.5": "db-a"}, CIDR, OURS, ec2._route_list(), {}, (), unresolved=["db-b"])
res = hr.apply(ec2, RT, p, OURS)
check(not p.remove and ("10.0.2.6", "db-b") in p.keep and not p.changes, "one name unresolved: kept, no change")
check(not ec2.calls and [o["ip"] for o in res["owned"]] == ["10.0.1.5", "10.0.2.6"], "no delete; still owned")
ec2 = FakeEc2({"10.0.1.5/32": (OURS, "active"), "10.0.2.6/32": (OURS, "active")})
p = hr.plan(owned, {}, CIDR, OURS, ec2._route_list(), None, (), "the resolver is down",
            unresolved=["db-a", "db-b"])
check(not p.remove and not p.changes and len(p.keep) == 2, "every name unresolved: every owned route kept")
ec2 = FakeEc2({"10.0.2.6/32": (OURS, "active")})
p = hr.plan(owned[1:], {}, CIDR, OURS, ec2._route_list(), {}, (), unresolved=["db-a"])
check(p.remove == ["10.0.2.6"], "a different name unresolved does not protect a departed member's route")
ec2 = FakeEc2({"10.0.2.6/32": (OURS, "active")})
p = hr.plan(owned[1:], {}, CIDR, OURS, ec2._route_list(), {}, None)
check(not p.remove and p.keep == [("10.0.2.6", "db-b")], "other ownership unknown (None): nothing is deleted")
ec2 = FakeEc2({"10.0.1.5/32": (OURS, "active")})
p = hr.plan([{"ip": "10.0.1.5", "host": "db-x"}], {"10.0.1.5": "db-a"}, CIDR, OURS, ec2._route_list(),
            {"10.0.1.5": "orders-prod"}, None, unresolved=["db-x"])
check(p.remove == ["10.0.1.5"], "a known hijack is removed even when ownership or a name is unknown")

print("== a kept /32 for a name that does not resolve still passes the taken check ==")
ec2 = FakeEc2({"10.0.2.6/32": (OURS, "active")})
p = hr.plan(owned[1:], {}, CIDR, OURS, ec2._route_list(), {"10.0.2.6": "orders-prod"}, (), unresolved=["db-b"])
res = hr.apply(ec2, RT, p, OURS)
check(p.remove == ["10.0.2.6"] and not p.keep and ("delete", "10.0.2.6/32") in ec2.calls,
      "db-b fails DNS and its old address now belongs to another registered database: the /32 goes")
check(res["owned"] == [], "and is no longer owned")
ec2 = FakeEc2({"10.0.2.6/32": (OURS, "active")})
p = hr.plan(owned[1:], {}, CIDR, OURS, ec2._route_list(), {"10.9.9.9": "orders-prod"}, (), unresolved=["db-b"])
check(p.keep == [("10.0.2.6", "db-b")] and not p.changes, "an address nobody else uses is still kept")
p = hr.plan(owned[1:], {}, CIDR, OURS, ec2._route_list(), None, (), "gate unknown", unresolved=["db-b"])
check(p.keep == [("10.0.2.6", "db-b")] and not p.changes, "with the gate unknown the working route is kept")

print("== the gate: every name of every registered database ==")
recs = [{"vpc_id": "vpc-o", "display_name": "orders", "endpoint": "c.example", "reader_endpoint": "r.example",
         "cluster_member_roles": [{"endpoint": "m1.example"}, {"endpoint": "m2.example"}]},
        {"vpc_id": "vpc-elsewhere", "endpoint": "x.example"}]
dns = {"c.example": ["10.0.1.1"], "r.example": ["10.0.1.2"], "m1.example": ["10.0.1.1"], "m2.example": ["10.0.1.2", "10.0.1.3"]}
got = hr.registered_addresses(recs, "vpc-o", "", lambda h: dns.get(h, []))
check(set(got) == {"10.0.1.1", "10.0.1.2", "10.0.1.3"}, "endpoint, reader endpoint and members resolved")
check(hr.registered_addresses(recs, "vpc-o", "vpc-o", lambda h: []) == {}, "the database's own VPC is excluded")
dns.pop("m2.example")
try:
    hr.registered_addresses(recs, "vpc-o", "", lambda h: dns.get(h, []))
    check(False, "an unresolvable member endpoint raises (fail closed)")
except RuntimeError as e:
    check("orders" not in str(e), "an unresolvable member endpoint raises, without naming the database")
check(hr.route_holder([{"DestinationCidrBlock": CIDR, "VpcPeeringConnectionId": OTHER}], CIDR) == OTHER
      and hr.route_holder([], CIDR) == "", "route_holder")

print("== dial_gate ==")
writer = {"instance_id": "db-a", "is_writer": True, "endpoint": "db-a.example"}
reader = {"instance_id": "db-b", "is_writer": False, "endpoint": "db-b.example"}
lat = {"network_method": "lattice", "endpoint": "cluster.example"}
check(hr.dial_gate(lat, reader, lambda h: ["10.0.0.9"]) == (None, hr.LATTICE_SKIP), "lattice: readers skipped")
check(hr.dial_gate(lat, writer, lambda h: ["10.0.0.9"]) == ("cluster.example", ""), "lattice: writer on the cluster endpoint")
host = {"network_method": "peering", "network_route_mode": "host", "endpoint": "cluster.example",
        "host_routes": [{"ip": "10.0.1.5", "host": "db-a.example"}]}
dns = {"db-a.example": ["10.0.1.5"], "db-b.example": ["10.0.1.6"]}
check(hr.dial_gate(host, writer, dns.get) == ("db-a.example", ""), "host mode: a routed member is dialled directly")
check(hr.dial_gate(host, reader, dns.get) == (None, "skipped: not routed (10.0.1.6 has no owned host route)"),
      "host mode: an unrouted reader is skipped")
check(hr.dial_gate(host, reader, lambda h: []) == (None, "skipped: instance endpoint does not resolve"),
      "host mode: an unresolvable reader is skipped with its own text")
_, routed_why = hr.dial_gate(host, reader, dns.get)
_, dns_why = hr.dial_gate(host, reader, lambda h: [])
check("not routed" in routed_why and "not routed" not in dns_why and "route" not in dns_why,
      "the two causes read differently: a missing route vs a name that does not resolve")
check(hr.dial_gate(dict(host, host_routes=[]), writer, dns.get) == ("cluster.example", ""),
      "host mode: an unrouted writer falls back to the cluster endpoint")
broken = dict(host, host_routes=[{"ip": "10.0.1.5", "host": "db-a.example"}, {"ip": "10.0.1.6", "host": "db-b.example"}],
              route_repair_needed={"items": [{"member": "db-b.example", "address": "10.0.1.6",
                                              "issue": "route blackholed"}]})
check(hr.dial_gate(broken, reader, dns.get)
      == (None, "skipped: not routed (10.0.1.6/32: route blackholed; route repair needed)"),
      "host mode: an owned /32 that Tier 1 flagged in the live table is not routed")
check(hr.dial_gate(broken, writer, dns.get) == ("db-a.example", ""),
      "host mode: a member whose /32 is not flagged is still dialled")
check(hr.dial_gate(dict(broken, route_repair_needed={"items": [{"address": "10.0.1.5", "issue": "route missing"}]}),
                   writer, dns.get) == ("cluster.example", ""),
      "host mode: a flagged writer falls back to the cluster endpoint")
vpc = {"network_method": "peering", "network_route_mode": "vpc", "endpoint": "cluster.example"}
check(hr.dial_gate(vpc, reader, dns.get) == ("db-b.example", ""), "vpc mode: the instance endpoint")
check(hr.dial_gate(vpc, dict(reader, endpoint=""), dns.get) == (None, "skipped: no instance endpoint"),
      "a reader with no endpoint is skipped")

print("")
print("== DNS resolution is bounded, and a timeout is UNKNOWN, not absent (#25) ==")
import time as _time  # noqa: E402

_orig_getaddrinfo = hr._getaddrinfo


def _slow(host):
    _time.sleep(5)   # longer than the resolver timeout
    return ["10.0.0.9"]


os.environ["HOST_RESOLVE_TIMEOUT_S"] = "0.3"
hr._getaddrinfo = _slow
try:
    t0 = _time.monotonic()
    out = hr.resolve("slow.example")
    elapsed = _time.monotonic() - t0
    check(out == [] and elapsed < 2.0, f"a slow lookup is bounded and returns [] (unknown) in {elapsed:.2f}s")
    # [] is UNKNOWN: dial_gate skips the reader with the badge, never dials blind.
    host_mode = {"network_method": "peering", "network_route_mode": "host",
                 "endpoint": "cluster.example", "host_routes": [{"ip": "10.0.1.5", "host": "db-a.example"}]}
    reader_m = {"instance_id": "db-a", "is_writer": False, "endpoint": "db-a.example"}
    gate = hr.dial_gate(host_mode, reader_m)   # uses the module resolve() (now timing out)
    check(gate[0] is None and "resolve" in gate[1].lower(),
          f"a reader whose endpoint will not resolve in time is skipped with a badge ({gate[1]})")
    # registered_addresses stays fail-closed: an unresolvable name raises rather than
    # contributing nothing, so a host route is never added past an unknown address.
    try:
        hr.registered_addresses([{"vpc_id": "vpc-x", "display_name": "other", "endpoint": "other.example"}], "vpc-x")
        check(False, "registered_addresses must fail closed on an unresolvable name")
    except RuntimeError:
        check(True, "registered_addresses fails closed when an address cannot be resolved in time")
finally:
    hr._getaddrinfo = _orig_getaddrinfo
    os.environ.pop("HOST_RESOLVE_TIMEOUT_S", None)

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
