# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The chat's member focus goes through the same reachability gate as Tier 1.

``pg_client.get_connection`` with a ``reader`` or ``instance:<id>`` focus asks
``host_routes.dial_gate`` before dialling. A member the gate marks unreachable (a reader
over a VPC Lattice resource endpoint, a host-route member with no owned /32, one whose
name does not resolve) returns its known reason instead of a dial; the reader endpoint
over Lattice is refused with a clear reason. The focus is a request field, so the server
enforces it whatever the UI offered.

The real ``get_connection`` runs with a fake registry, fake credentials, a fake
``psycopg2.connect`` that records the dialled host, a fake DNS resolver, and in one case
a fake gate. Hermetic, no AWS.

Run:  python3 backend/tests/test_focus_dial_gate.py
"""
import os
import pathlib
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
for _m in ("psycopg2", "psycopg2.extras", "boto3"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

from tools.aws import host_routes  # noqa: E402
from tools.aws import pg_client as pg  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


DB_ID = "11111111-2222-3333-4444-555555555555"
CLUSTER_EP, READER_EP = "orders.cluster-x.example", "orders.cluster-ro-x.example"
EP = {"db-a": "db-a.example", "db-b": "db-b.example"}


def record(**over):
    rec = {"instance_id": DB_ID, "endpoint": CLUSTER_EP, "reader_endpoint": READER_EP, "port": 5432,
           "secret_arn": "arn:aws:secretsmanager:us-east-1:1:secret:ro", "network_method": "peering",
           "network_route_mode": "vpc",
           "cluster_member_roles": [{"instance_id": "db-a", "is_writer": True, "endpoint": EP["db-a"]},
                                    {"instance_id": "db-b", "is_writer": False, "endpoint": EP["db-b"]}]}
    rec.update(over)
    return rec


dialled = []


def install(rec, focus, dns=None):
    sys.modules["tools.aws.dynamodb_client"] = types.SimpleNamespace(
        db_registry=types.SimpleNamespace(get_instance=lambda iid: dict(rec) if iid == DB_ID else None))
    pg.get_db_credentials = lambda arn: {"username": "ro", "password": "pw", "host": CLUSTER_EP}
    pg._enforce_secret_space_ownership = lambda arn: None
    pg.resolve_credential = lambda handle: rec["secret_arn"]
    pg.ssl_kwargs = lambda: {}
    pg._role_focus = lambda: focus
    pg._lookup_role_fields = lambda arn, iid="": {"reader_endpoint": rec.get("reader_endpoint"),
                                                  "cluster_member_roles": rec.get("cluster_member_roles")}
    pg.psycopg2.connect = lambda **kw: dialled.append(kw["host"]) or types.SimpleNamespace(set_session=lambda **k: None)
    host_routes.resolve = lambda h: list((dns or {}).get(h, []))


def outcome(rec, focus, dns=None):
    install(rec, focus, dns)
    dialled.clear()
    try:
        pg.get_connection(DB_ID)
        return dialled[-1] if dialled else None, ""
    except pg.MemberNotReachable as e:
        return None, str(e)


print("== a reachable focus dials as before ==")
check(outcome(record(), "instance:db-b") == (EP["db-b"], ""), "instance:db-b on a VPC-wide route: its endpoint")
check(outcome(record(), "reader") == (READER_EP, ""), "reader: the reader endpoint")
check(outcome(record(), "") == (CLUSTER_EP, ""), "no focus: the cluster endpoint")

print("== Lattice: per-instance names are not served ==")
lat = record(network_method="lattice", network_route_mode="")
host, why = outcome(lat, "instance:db-b")
check(host is None and not dialled and "Member db-b is not reachable" in why
      and "Lattice resource endpoint" in why, "a reader focus is refused with the reason, nothing dialled")
host, why = outcome(lat, "reader")
check(host is None and not dialled and "The reader endpoint is not reachable" in why
      and "Lattice" in why, "the reader endpoint over Lattice is refused with a clear reason")
check(outcome(lat, "instance:db-a") == (CLUSTER_EP, ""), "the writer is dialled on the cluster endpoint")

print("== host routes: a member with no owned /32 is not dialled ==")
hr = record(network_route_mode="host", host_routes=[{"ip": "10.0.1.5", "host": EP["db-a"]}])
dns = {EP["db-a"]: ["10.0.1.5"], EP["db-b"]: ["10.0.1.6"], READER_EP: ["10.0.1.6"]}
host, why = outcome(hr, "instance:db-b", dns)
check(host is None and "10.0.1.6 has no owned host route" in why,
      "refused with its known reason (no dial toward the broad CIDR route)")
host, why = outcome(hr, "reader", dns)
check(host is None and "no owned host route" in why, "the reader endpoint resolving to an unrouted address too")
check(outcome(hr, "instance:db-a", dns) == (EP["db-a"], ""), "a routed member is dialled")
host, why = outcome(hr, "instance:db-b", {EP["db-a"]: ["10.0.1.5"]})
check(host is None and "instance endpoint does not resolve" in why, "a name that does not resolve says so")
ok = record(network_route_mode="host", host_routes=[{"ip": "10.0.1.5", "host": EP["db-a"]},
                                                    {"ip": "10.0.1.6", "host": EP["db-b"]}])
check(outcome(ok, "instance:db-b", dns) == (EP["db-b"], ""), "once its /32 is owned it is dialled")

print("== get_connection asks the gate (a fake gate) ==")
real_gate, seen = host_routes.dial_gate, []
host_routes.dial_gate = lambda rec, member, resolve=None: (seen.append(member.get("endpoint")),
                                                             (None, "skipped: fake gate says no"))[1]
try:
    host, why = outcome(record(), "instance:db-b")
    check(seen == [EP["db-b"]] and host is None and "fake gate says no" in why,
          "the member is gated server-side; the gate's reason is returned")
    seen.clear()
    outcome(record(), "")
    check(not seen, "no focus: no gate call (the cluster endpoint is always routed)")
finally:
    host_routes.dial_gate = real_gate

print("== the pickers disable such members, with the reason ==")
for app in ("frontend", "admin-console"):
    src = (ROOT / f"{app}/src/components/MemberPicker.jsx").read_text(encoding="utf-8")
    check("m.skip_reason" in src and "data.reader_skip_reason" in src and "disabled={!!skipOf(m)}" in src,
          f"{app} MemberPicker reads the members API's reasons and disables a skipped member")
scope = (ROOT / "frontend/src/components/ScopeSelector.jsx").read_text(encoding="utf-8")
check("useLiveMembers(api, focusTarget?.instance_id)" in scope and "reachFrom(liveMembers)" in scope and "disabled={!!memberSkip(m)}" in scope
      and "disabled={!!readerSkip}" in scope, "the chat endpoint picker does too, the reader endpoint included")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
