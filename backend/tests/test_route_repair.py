# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The API task repairs host routes Tier 1 flagged; nothing else does.

``tools.aws.route_repair`` runs for real against a fake instances table that evaluates
the module's condition expressions the way DynamoDB would, a fake EC2 (the agent route
table and the peering), a fake DNS resolver, and captured audit and notification
writers. The Repair endpoint is lifted from backend/app.py by source. Hermetic.

Pinned:
* a flagged record is repaired with Configure's planner: the new `/32` first, the stale
  one after; host_routes updated; the flag cleared; one audit as ``system``; config_log;
* the claim: a live claim held by another API task means no second repair; a stale
  claim is taken over; the claim is released afterwards;
* every safety refusal holds: an address another registered database uses, an address
  outside the database VPC, an unknown far VPC (fail closed), and a name that does not
  resolve keeps its route. A refusal keeps the flag with its reason, is audited, logged
  and notified once per change, and again on each Repair click;
* write-ahead: the intended ownership is on the record before the first EC2 call; a
  Configure racing ahead of it wins with no EC2 call; one landing after the EC2 calls
  gets the mutation audited (unconfirmed); a crash after them leaves every `/32` owned
  and the next pass reconciles;
* the loop picks only flagged, configured host-route records; only the API task runs it
  (ROUTE_REPAIR_LOOP is set in the ECS task alone; the agent runtime never has it);
* the Repair endpoint: managers and super admins only, host-route databases only.

Run:  python3 backend/tests/test_route_repair.py
"""
import copy
import os
import pathlib
import re
import sys
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from botocore.exceptions import ClientError  # noqa: E402
from tools.aws import host_routes, route_repair as rr  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP_SRC = (ROOT / "backend/app.py").read_text(encoding="utf-8")
passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def cfail():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "UpdateItem")


# ── a fake DynamoDB table that evaluates conditions ────────────────────────────

def _split_and(expr):
    parts, depth, cur = [], 0, ""
    tokens = re.split(r"(\(|\)| AND )", expr)
    for t in tokens:
        if t == "(":
            depth += 1
        elif t == ")":
            depth -= 1
        if t == " AND " and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += t
    parts.append(cur.strip())
    return [p for p in parts if p]


def _eval(expr, row, names, vals):
    n = lambda tok: names.get(tok, tok)  # noqa: E731
    v = lambda tok: vals[tok] if tok.startswith(":") else row.get(n(tok))  # noqa: E731
    expr = expr.strip()
    if expr.startswith("(") and expr.endswith(")"):
        return any(_eval(x, row, names, vals) for x in expr[1:-1].split(" OR "))
    m = re.fullmatch(r"attribute_exists\((\S+)\)", expr)
    if m:
        return n(m.group(1)) in row
    m = re.fullmatch(r"attribute_not_exists\((\S+)\)", expr)
    if m:
        return n(m.group(1)) not in row
    m = re.fullmatch(r"(\S+) (=|<) (\S+)", expr)
    assert m, f"unmodelled condition {expr!r}"
    a, op, b = v(m.group(1)), m.group(2), v(m.group(3))
    if op == "=":
        return a == b
    return a is not None and b is not None and a < b


class Table:
    def __init__(self, rows):
        self.rows = {r["instance_id"]: copy.deepcopy(r) for r in rows}
        self.updates = []
        self.before_update = None

    def get_item(self, Key):
        r = self.rows.get(Key["instance_id"])
        return {"Item": copy.deepcopy(r)} if r else {}

    def scan(self, **kw):
        return {"Items": [copy.deepcopy(r) for r in self.rows.values()]}

    def update_item(self, Key, UpdateExpression, ConditionExpression, ExpressionAttributeNames,
                    ExpressionAttributeValues=None):
        vals = ExpressionAttributeValues or {}
        if self.before_update:
            hook, self.before_update = self.before_update, None
            hook(self)
        row = self.rows.get(Key["instance_id"])
        if row is None or not all(_eval(c, row, ExpressionAttributeNames, vals)
                                  for c in _split_and(ConditionExpression)):
            raise cfail()
        set_part, _, rem = UpdateExpression.partition("REMOVE ")
        for part in filter(None, set_part.strip().removeprefix("SET ").split(", ")):
            name, val = part.split(" = ")
            row[ExpressionAttributeNames.get(name, name)] = copy.deepcopy(vals[val])
        for name in filter(None, (x.strip() for x in rem.split(","))):
            row.pop(ExpressionAttributeNames.get(name, name), None)
        self.updates.append(UpdateExpression)


class Ec2:
    def __init__(self, routes, far_vpc="vpc-other"):
        self.routes, self.far_vpc, self.calls = dict(routes), far_vpc, []

    def describe_route_tables(self, RouteTableIds):
        out = []
        for c, (t, st) in self.routes.items():
            out.append({"DestinationCidrBlock": c, "State": st,
                        ("VpcPeeringConnectionId" if t.startswith("pcx-") else "GatewayId"): t})
        return {"RouteTables": [{"Routes": out}]}

    def describe_vpc_peering_connections(self, VpcPeeringConnectionIds):
        if not self.far_vpc:
            raise RuntimeError("InvalidVpcPeeringConnectionID.NotFound")
        return {"VpcPeeringConnections": [{"RequesterVpcInfo": {"VpcId": "vpc-agent"},
                                           "AccepterVpcInfo": {"VpcId": self.far_vpc}}]}

    def create_route(self, RouteTableId, DestinationCidrBlock, VpcPeeringConnectionId):
        self.calls.append(("create", DestinationCidrBlock))
        self.routes[DestinationCidrBlock] = (VpcPeeringConnectionId, "active")

    def replace_route(self, RouteTableId, DestinationCidrBlock, VpcPeeringConnectionId):
        self.calls.append(("replace", DestinationCidrBlock))
        self.routes[DestinationCidrBlock] = (VpcPeeringConnectionId, "active")

    def delete_route(self, RouteTableId, DestinationCidrBlock):
        self.calls.append(("delete", DestinationCidrBlock))
        self.routes.pop(DestinationCidrBlock, None)


PCX, CIDR = "pcx-ours", "10.0.0.0/16"
EP = {"w": "w.example", "r1": "r1.example", "r2": "r2.example"}
CLUSTER_EP, READER_EP = "c.example", "ro.example"
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


def record(**over):
    rec = {"instance_id": "db-orders", "display_name": "orders", "status": "configured",
           "network_route_mode": "host", "network_peering_id": PCX, "db_vpc_cidr": CIDR, "vpc_id": "vpc-db",
           "endpoint": CLUSTER_EP, "reader_endpoint": READER_EP, "member_space_ids": {"space-a"},
           "cluster_member_roles": [{"instance_id": k, "is_writer": k == "w", "endpoint": e} for k, e in EP.items()],
           "host_routes": [{"ip": "10.0.1.1", "host": EP["w"]}, {"ip": "10.0.1.2", "host": EP["r1"]},
                           {"ip": "10.0.1.3", "host": EP["r2"]}],
           "route_repair_needed": {"member": EP["r2"], "address": "10.0.1.9", "issue": "unrouted",
                                   "detected_at": "2029-12-31T23:55:00+00:00",
                                   "items": [{"member": EP["r2"], "address": "10.0.1.9", "issue": "unrouted"}]},
           "config_log": []}
    rec.update(over)
    return rec


class World:
    def __init__(self, rows, dns_over=None, far_vpc="vpc-other"):
        self.table = Table(rows)
        self.ec2 = Ec2({CIDR: ("pcx-other", "active"), "10.0.1.1/32": (PCX, "active"),
                        "10.0.1.2/32": (PCX, "active"), "10.0.1.3/32": (PCX, "active")}, far_vpc)
        self.dns = {CLUSTER_EP: ["10.0.1.1"], READER_EP: ["10.0.1.2"], EP["w"]: ["10.0.1.1"],
                    EP["r1"]: ["10.0.1.2"], EP["r2"]: ["10.0.1.9"]}
        self.dns.update(dns_over or {})
        self.audits, self.notices = [], []
        host_routes.resolve = lambda h: list(self.dns.get(h, []))
        self.deps = rr.Deps(table=self.table, ec2=self.ec2, route_table_id="rtb-agent", agent_vpc_id="vpc-agent",
                            audit=lambda *a: self.audits.append(a) or "stored",
                            notify=lambda s, m: self.notices.append((s, m)), now=lambda: NOW)

    def row(self, iid="db-orders"):
        return self.table.rows[iid]


print("== a flagged record is repaired: the new /32 in, the stale one out, the flag cleared ==")
w = World([record()])
out = rr.repair(w.deps, "db-orders")
row = w.row()
check(out["status"] == "repaired", f"status repaired ({out})")
check(w.ec2.calls == [("create", "10.0.1.9/32"), ("delete", "10.0.1.3/32")], "create first, then delete")
check(sorted(o["ip"] for o in row["host_routes"]) == ["10.0.1.1", "10.0.1.2", "10.0.1.9"], "host_routes updated")
check("route_repair_needed" not in row, "the flag is cleared")
check(rr.CLAIM not in row and rr.CLAIMED_AT not in row, "the claim is released")
check(any("Host route repair: Agent route: 10.0.1.9/32" in x for x in row["config_log"]), "config_log lines")
(actor, action, iid, before, after, _rec), = w.audits
check(actor == "system" and action == "network_host_routes_repaired" and after["added"] == ["10.0.1.9"]
      and after["removed"] == ["10.0.1.3"], "one audit record, actor system")
check(not w.notices, "a repair sends no notification")

print("== a live-route finding (an owned /32 blackholed or missing) is repaired too ==")
for label, route, call in (("blackholed", (PCX, "blackhole"), ("replace", "10.0.1.3/32")),
                           ("missing", None, ("create", "10.0.1.3/32"))):
    flag = {"member": EP["r2"], "address": "10.0.1.3", "issue": f"route {label}",
            "detected_at": "2029-12-31T23:55:00+00:00",
            "items": [{"member": EP["r2"], "address": "10.0.1.3", "issue": f"route {label}"}]}
    w = World([record(route_repair_needed=flag)], {EP["r2"]: ["10.0.1.3"]})
    if route is None:
        w.ec2.routes.pop("10.0.1.3/32")
    else:
        w.ec2.routes["10.0.1.3/32"] = route
    out = rr.repair(w.deps, "db-orders")
    check(out["status"] == "repaired" and w.ec2.calls == [call] and w.ec2.routes["10.0.1.3/32"] == (PCX, "active")
          and "route_repair_needed" not in w.row(), f"{label}: {call[0]} to our peering, flag cleared ({out})")

print("== the claim: two API tasks never repair the same record ==")
live = record(**{rr.CLAIM: "other-task", rr.CLAIMED_AT: (NOW - timedelta(seconds=30)).isoformat()})
w = World([live])
out = rr.repair(w.deps, "db-orders")
check(out["status"] == "busy" and not w.ec2.calls and w.row()[rr.CLAIM] == "other-task",
      "a live claim held elsewhere: no repair, the claim untouched")
stale = record(**{rr.CLAIM: "dead-task", rr.CLAIMED_AT: (NOW - timedelta(seconds=rr.CLAIM_STALE_S + 1)).isoformat()})
w = World([stale])
check(rr.repair(w.deps, "db-orders")["status"] == "repaired" and rr.CLAIM not in w.row(),
      "a stale claim (a stopped process) is taken over")
w = World([record(route_repair_needed=None)])
w.row().pop("route_repair_needed")
check(rr.repair(w.deps, "db-orders")["status"] == "busy" and not w.ec2.calls,
      "the loop does not repair an unflagged record")
check(rr.repair(w.deps, "db-orders", actor="admin-1", force=True)["status"] in ("repaired", "not_needed"),
      "the Repair button (force) runs it anyway")

print("== refusal: an address another registered database uses ==")
neighbour = {"instance_id": "db-n", "vpc_id": "vpc-other", "endpoint": "n.example", "status": "configured"}
w = World([record(), neighbour], {"n.example": ["10.0.1.9"]})
out = rr.repair(w.deps, "db-orders")
row = w.row()
check(out["status"] == "refused" and ("create", "10.0.1.9/32") not in w.ec2.calls, "never takes the address")
check("take over that database's traffic" in row["route_repair_needed"]["refused"]
      and row["route_repair_needed"]["refused_at"], "the flag stays, with the reason")
refusals = [a for a in w.audits if a[1] == "network_host_routes_repair_refused"]
check(len(refusals) == 1 and refusals[0][0] == "system", "the refusal is audited as system")
check(len(w.notices) == 1 and "could not be repaired" in w.notices[0][1], "and notified")
check(any("Host route repair refused:" in x for x in row["config_log"]), "and logged")
check(("delete", "10.0.1.3/32") in w.ec2.calls and any(a[1] == "network_host_routes_repaired" for a in w.audits),
      "the stale /32 of this record is still removed (and that repair audited)")
n_audits, n_log = len(w.audits), len(row["config_log"])
rr.repair(w.deps, "db-orders")
check(len(w.audits) == n_audits and len(w.notices) == 1 and len(w.row()["config_log"]) == n_log,
      "the loop's identical retry records nothing new")
rr.repair(w.deps, "db-orders", actor="admin-1", force=True)
check(len(w.audits) == n_audits + 1 and w.audits[-1][0] == "admin-1" and len(w.notices) == 2,
      "a Repair click records the refusal again, as the admin")

print("== refusal: an address outside the database VPC ==")
w = World([record()], {EP["r2"]: ["54.1.2.3"]})
out = rr.repair(w.deps, "db-orders")
check(out["status"] == "refused" and "outside the database's VPC" in out["message"]
      and ("create", "54.1.2.3/32") not in w.ec2.calls, "no public /32")

print("== fail closed: the far VPC cannot be read ==")
w = World([record()], far_vpc="")
out = rr.repair(w.deps, "db-orders")
check(out["status"] == "refused" and not [c for c in w.ec2.calls if c[0] == "create"]
      and "cannot be shown safe" in out["message"], "nothing is added")

print("== a name that does not resolve keeps its route ==")
w = World([record()])
w.dns.pop(EP["r2"])
out = rr.repair(w.deps, "db-orders")
check(not w.ec2.calls and "10.0.1.3/32" in w.ec2.routes and out["status"] == "not_needed",
      "no delete of the working route")
check(sorted(o["ip"] for o in w.row()["host_routes"]) == ["10.0.1.1", "10.0.1.2", "10.0.1.3"], "still owned")

print("== the VPC-wide route reaches it now: nothing to do, flag cleared ==")
w = World([record()])
w.ec2.routes[CIDR] = (PCX, "active")
out = rr.repair(w.deps, "db-orders")
check(out["status"] == "not_needed" and not w.ec2.calls and "route_repair_needed" not in w.row(), "cleared")

print("== a Configure racing the repair wins ==")
w = World([record()])
w.table.before_update = None


def configure_wrote(table):
    table.rows["db-orders"]["host_routes"] = [{"ip": "10.0.1.1", "host": EP["w"]}]


orig = w.table.update_item
calls = {"n": 0}


def update_item(**kw):
    calls["n"] += 1
    if calls["n"] == 2:          # after the claim, before the result write
        configure_wrote(w.table)
    return orig(**kw)


w.table.update_item = update_item
out = rr.repair(w.deps, "db-orders")
check(out["status"] == "skipped" and not w.audits and w.row()["host_routes"] == [{"ip": "10.0.1.1", "host": EP["w"]}],
      "Configure's routes stand, nothing audited")
check(not w.ec2.calls, "Configure got in before the write-ahead: the route table is not touched")
check(rr.CLAIM not in w.row(), "the claim is still released")

print("== write-ahead: the intended /32 is owned before EC2 is changed ==")
w = World([record()])
seen_at_create = []
orig_create = w.ec2.create_route


def create_route(**kw):
    seen_at_create.append(sorted(o["ip"] for o in w.row()["host_routes"]))
    return orig_create(**kw)


w.ec2.create_route = create_route
out = rr.repair(w.deps, "db-orders")
check(seen_at_create == [["10.0.1.1", "10.0.1.2", "10.0.1.3", "10.0.1.9"]],
      f"when create_route runs, the record already owns the new /32 and still the old one ({seen_at_create})")
check(out["status"] == "repaired" and sorted(o["ip"] for o in w.row()["host_routes"]) == ["10.0.1.1", "10.0.1.2", "10.0.1.9"],
      "then confirmed: the removed /32 is released")

print("== a Configure lands after the EC2 calls: the mutation is audited, nothing is untracked ==")
w = World([record()])
orig = w.table.update_item
calls = {"n": 0}


def update_item_late(**kw):
    calls["n"] += 1
    if calls["n"] == 3:          # after the write-ahead and the EC2 calls, before the confirm
        w.table.rows["db-orders"]["host_routes"] = [{"ip": "10.0.1.1", "host": EP["w"]}]
    return orig(**kw)


w.table.update_item = update_item_late
out = rr.repair(w.deps, "db-orders")
(actor, action, _iid, _before, after, _rec), = w.audits
check(out["status"] == "skipped" and "recorded as owned before they were made" in out["message"],
      f"skipped, saying the changes were recorded ahead ({out['message']})")
check(action == "network_host_routes_repaired" and after["added"] == ["10.0.1.9"] and after["removed"] == ["10.0.1.3"]
      and after.get("confirmed") is False, "the audit event names the actual mutation, unconfirmed")

print("== a crash after the EC2 calls: the /32 stays owned, and the next pass reconciles ==")
w = World([record()])
orig = w.table.update_item
calls = {"n": 0}


def update_item_crash(**kw):
    calls["n"] += 1
    if calls["n"] == 3:
        raise RuntimeError("the task stopped")
    return orig(**kw)


w.table.update_item = update_item_crash
try:
    rr.repair(w.deps, "db-orders")
    check(False, "the crash surfaces")
except RuntimeError:
    pass
ours = sorted(c.split("/")[0] for c, (t, _st) in w.ec2.routes.items() if t == PCX and c.endswith("/32"))
owned = sorted(o["ip"] for o in w.row()["host_routes"])
check(ours == ["10.0.1.1", "10.0.1.2", "10.0.1.9"] and set(ours) <= set(owned),
      f"every /32 to our peering is owned by the record ({ours} within {owned})")
w.table.update_item = orig
out = rr.repair(w.deps, "db-orders")
check(sorted(o["ip"] for o in w.row()["host_routes"]) == ours and "route_repair_needed" not in w.row()
      and out["status"] in ("not_needed", "repaired"),
      f"the next pass reconciles ownership with the table and clears the flag ({out['status']})")

print("== the loop picks flagged, configured host-route records only ==")
w = World([record(), record(instance_id="db-vpc", network_route_mode="vpc"),
           record(instance_id="db-del", status="deleting"),
           {**record(instance_id="db-ok"), "route_repair_needed": None}])
w.row("db-ok").pop("route_repair_needed")
check([r["instance_id"] for r in rr.flagged(w.table)] == ["db-orders"], "flagged()")
check(rr.run_flagged(w.deps) == {"db-orders": "repaired"}, "run_flagged repairs it")
check(rr.REPAIR_INTERVAL_S == 300, "every 5 minutes")

print("== only the API task runs the loop ==")
api = (ROOT / "infrastructure/api.yaml").read_text(encoding="utf-8")
tpl = (ROOT / "backend/template.yaml").read_text(encoding="utf-8")
check(re.search(r"- Name: ROUTE_REPAIR_LOOP\s*\n\s*Value: enabled", api) is not None,
      "the ECS API task sets ROUTE_REPAIR_LOOP=enabled")
check("ROUTE_REPAIR_LOOP" not in tpl, "the agent stack (AgentCore runtime, Tier 1, Batch) never sets it")
start = re.search(r"^def _start_route_repair_loop\(.*?(?=^(?:@|def ))", APP_SRC, re.S | re.M).group(0)
check('os.getenv("ROUTE_REPAIR_LOOP")' in start and '!= "enabled" or _ROUTE_REPAIR_POOL:\n        return' in start
      and "audit_log.submit_in_context(_ROUTE_REPAIR_POOL, _route_repair_loop)" in start,
      "the startup hook starts it only when set")
loop = re.search(r"^def _route_repair_loop\(.*?(?=^(?:@|def ))", APP_SRC, re.S | re.M).group(0)
check("while not _ROUTE_REPAIR_STOP.wait(route_repair.REPAIR_INTERVAL_S):" in loop
      and "route_repair.run_flagged(" in loop, "the loop runs a pass every interval until shutdown")
check(all(v in api for v in ("ec2:CreateRoute", "ec2:DeleteRoute", "ec2:ReplaceRoute")),
      "the API task role holds the route permissions")

print("== the Repair endpoint ==")


class HTTPException(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


def endpoint(rec, manager=True, result=None, limited=False, super_admin=False, reads=None):
    seen = []
    fake_rr = types.SimpleNamespace(repair=lambda deps, iid, actor="system", force=False: (
        seen.append((iid, actor, force)), result or {"status": "repaired", "message": "ok", "steps": []})[1])
    reads = reads if reads is not None else []

    def get_instance(iid):
        reads.append(iid)
        return dict(rec) if rec else None

    ns = {"HTTPException": HTTPException, "Request": object,
          "db_registry": types.SimpleNamespace(get_instance=get_instance),
          # As in app.py: an unknown id has no member space, so nobody manages it.
          "_can_manage_instance": lambda auth, iid: manager and bool(rec),
          "is_admin": lambda auth: super_admin,
          "check_rate_limit": lambda uid: (False, 17) if limited else (True, 0),
          "get_user_id": lambda auth: "admin-1",
          "_route_repair_deps": lambda: None}
    src = re.search(r"^def repair_database_routes\(.*?(?=^(?:@|def ))", APP_SRC, re.S | re.M).group(0)
    exec(src, ns)  # noqa: S102 - pinning shipped source
    import tools.aws as pkg
    saved = sys.modules.get("tools.aws.route_repair"), getattr(pkg, "route_repair", None)
    sys.modules["tools.aws.route_repair"], pkg.route_repair = fake_rr, fake_rr
    try:
        return 200, ns["repair_database_routes"]("db-orders", types.SimpleNamespace(
            headers={"Authorization": "Bearer x"})), seen
    except HTTPException as e:
        return e.status_code, e.detail, seen
    finally:
        sys.modules["tools.aws.route_repair"], pkg.route_repair = saved


code, out, seen = endpoint(record())
check(code == 200 and out["status"] == "repaired" and seen == [("db-orders", "admin-1", True)],
      "a manager's Repair runs the same repair at once, as them")
check(endpoint(record(), manager=False)[0] == 403, "anyone else: 403")
check(endpoint(record(network_route_mode="vpc"))[0] == 400, "not a host-route database: 400")
denied, unknown = endpoint(record(), manager=False), endpoint(None, manager=False)
check(unknown[:2] == denied[:2] == (403, "Admin access required"),
      f"a non-manager gets the same 403 for an unknown id as for a real one ({unknown[:2]} / {denied[:2]})")
reads = []
endpoint(record(), manager=False, reads=reads)
check(not reads, "authorization comes before the registry read")
check(endpoint(None, super_admin=True)[0] == 404, "a super admin (who sees every database) is told it is not found")
code, detail, seen = endpoint(record(), limited=True)
check(code == 429 and "17 seconds" in detail and not seen,
      "over the caller's per-minute budget: 429 with the wait, and no repair runs")
code, _detail, seen = endpoint(record(), manager=False, limited=True)
check(code == 429 and not seen, "the limit counts before authorization, so probing ids is limited too")
check("check_rate_limit(get_user_id(auth))" in
      re.search(r"^def repair_database_routes\(.*?(?=^(?:@|def ))", APP_SRC, re.S | re.M).group(0),
      "the endpoint uses the existing per-caller limiter")
check(endpoint(record(), result={"status": "busy", "message": "Another route repair is running"})[0] == 409,
      "a repair already running: 409")

print("== the console ==")
jsx = (ROOT / "admin-console/src/components/SpaceDetail.jsx").read_text(encoding="utf-8")
check("Route repair needed" in jsx and "Route repair refused: " in jsx
      and "/route-repair" in jsx and "Reconfigure network" in jsx,
      "the database row shows the flag or the refusal, a Repair button, and Reconfigure network")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
