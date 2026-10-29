# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Cross-Region host-route repair, run by the API task.

On the cross-Region CIDR-clash path (``network_route_mode == "host"``) a database is
reached through one `/32` per member address. The Tier 1 collector DETECTS drift (a
member whose address has no owned `/32`, an owned `/32` whose member is gone) and flags
the record with ``route_repair_needed``; it holds no route permission. This module
REPAIRS a flagged record, from the API task, which already holds the route permissions
Configure uses. Two callers: a background loop in the API task every
``REPAIR_INTERVAL_S`` (never the agent runtime), and the admin console's Repair button
for an owner or a super admin.

A repair takes a conditional claim on the record first (the teardown-claim pattern), so
two API tasks never repair the same record at once; a claim with no release after
``CLAIM_STALE_S`` belongs to a stopped process and is taken over. The plan is
``host_routes.plan``, the planner Configure uses, with every safety refusal: nothing
outside the database's VPC CIDR, nothing another registered database uses, nothing over
another live route, no adds when that cannot be known (fail closed), and a name that
does not resolve keeps its route. Before any EC2 call the intended ownership is written
ahead (conditional on the claim and the routes it read), then the EC2 calls run, then
the result is confirmed; a crash or a lost race after the EC2 calls therefore never
leaves an untracked `/32`, and the audit event names what was done to the route table
even when the confirm lost. On success the flag is cleared. Every repair and every
new refusal writes an audit record and a config_log line; a refusal also sends a
notification, and stays on the flag as ``refused`` until a later repair succeeds.

AWS clients, the registry table and the audit and notification writers are passed in
(``Deps``), so the rules are testable without AWS (``tests/test_route_repair.py``).
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from tools.aws import host_routes

logger = logging.getLogger("route-repair")

REPAIR_INTERVAL_S = 300
CLAIM_STALE_S = 240
CONFIG_LOG_MAX = 50
FLAG = "route_repair_needed"
CLAIM, CLAIMED_AT = "route_repair_claim", "route_repair_claimed_at"


@dataclass
class Deps:
    table: object                     # the instances table (get_item / update_item / scan)
    ec2: object                       # EC2 in the deployment account (the agent route table)
    route_table_id: str
    agent_vpc_id: str
    audit: Callable                   # (actor, action, iid, before, after, record) -> state
    notify: Callable                  # (subject, message) -> None
    now: Callable = lambda: datetime.now(timezone.utc)


def _code(e) -> str:
    return ((getattr(e, "response", None) or {}).get("Error") or {}).get("Code", "")


def _iso(dt) -> str:
    return dt.isoformat()


def flagged(table) -> list:
    """Configured host-route records whose Tier 1 check flagged a repair."""
    items, kwargs = [], {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if not resp.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return [r for r in items if r.get(FLAG) and r.get("status") == "configured"
            and r.get("network_route_mode") == "host"]


def claim(deps: Deps, iid: str, force: bool = False) -> Optional[str]:
    """Take the record's repair claim; the token, or None when another repair holds a
    live claim or the record is not (any longer) a configured host-route record (or, for
    the background loop, no longer flagged)."""
    token, now = uuid.uuid4().hex, deps.now()
    cond = ("attribute_exists(instance_id) AND #st = :configured AND #rm = :host AND "
            "(attribute_not_exists(#cl) OR #ca < :cutoff)")
    if not force:
        cond += " AND attribute_exists(#fl)"
    names = {"#st": "status", "#rm": "network_route_mode", "#cl": CLAIM, "#ca": CLAIMED_AT}
    if not force:
        names["#fl"] = FLAG
    try:
        deps.table.update_item(
            Key={"instance_id": iid}, UpdateExpression="SET #cl = :t, #ca = :now",
            ConditionExpression=cond, ExpressionAttributeNames=names,
            ExpressionAttributeValues={":t": token, ":now": _iso(now), ":configured": "configured",
                                       ":host": "host", ":cutoff": _iso(now - timedelta(seconds=CLAIM_STALE_S))})
        return token
    except Exception as e:  # noqa: BLE001
        if _code(e) == "ConditionalCheckFailedException":
            return None
        raise


def release(deps: Deps, iid: str, token: str) -> None:
    """Drop our claim (a no-op when it is no longer ours). Never raises."""
    try:
        deps.table.update_item(
            Key={"instance_id": iid}, UpdateExpression="REMOVE #cl, #ca",
            ConditionExpression="#cl = :t",
            ExpressionAttributeNames={"#cl": CLAIM, "#ca": CLAIMED_AT},
            ExpressionAttributeValues={":t": token})
    except Exception as e:  # noqa: BLE001
        if _code(e) != "ConditionalCheckFailedException":
            logger.warning("route repair claim on %s not released (%s)", iid, type(e).__name__)


def _scan(table) -> list:
    items, kwargs = [], {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if not resp.get("LastEvaluatedKey"):
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _owned_ips(record: dict) -> set:
    return {o.get("ip") for o in record.get("host_routes") or [] if isinstance(o, dict)}


def _plan(deps: Deps, record: dict):
    """``(plan, table_routes, reason)``: the repair plan, or ``(None, None, why)`` when no
    plan can be made (``why`` empty: the VPC-wide route reaches it now, nothing to do)."""
    iid = record.get("instance_id", "")
    peering, cidr = record.get("network_peering_id", ""), record.get("db_vpc_cidr", "")
    if not (deps.route_table_id and peering and cidr):
        return None, None, "the agent route table, the peering or the database VPC CIDR is unknown"
    try:
        table = deps.ec2.describe_route_tables(
            RouteTableIds=[deps.route_table_id])["RouteTables"][0].get("Routes", [])
    except Exception as e:  # noqa: BLE001
        return None, None, f"the agent route table could not be read ({_code(e) or type(e).__name__})"
    holder = host_routes.route_holder(table, cidr)
    if not holder or holder == peering:
        return None, table, ""
    try:
        records = _scan(deps.table)
    except Exception as e:  # noqa: BLE001 - who else owns a /32 is unknown: change nothing
        return None, None, f"the database registry could not be read ({type(e).__name__})"
    desired, unresolved = host_routes.desired_addresses(host_routes.record_hosts(record))
    taken, gate_reason = None, ""
    far_vpc = host_routes.peer_vpc_of(deps.ec2, holder, deps.agent_vpc_id)
    if not far_vpc:
        gate_reason = (f"could not determine which VPC {holder} leads to, so a host route cannot "
                       f"be shown safe; refusing it")
    else:
        try:
            taken = host_routes.registered_addresses(records, far_vpc, record.get("vpc_id", ""))
        except RuntimeError as e:
            gate_reason = f"{e}; a host route cannot be shown safe without it, so it is refused"
    other_owned = {ip for r in records
                   if r.get("instance_id") != iid and r.get("network_peering_id") == peering
                   for ip in _owned_ips(r)}
    p = host_routes.plan(list(record.get("host_routes") or []), desired, cidr, peering, table, taken,
                         other_owned, gate_reason, unresolved=unresolved)
    return p, table, ""


def repair(deps: Deps, iid: str, actor: str = "system", force: bool = False) -> dict:
    """Repair one record's host routes under its claim. ``force`` (the console's Repair
    button) runs it whether or not Tier 1 flagged it. Returns ``{"status", "message",
    "steps"}`` with status ``repaired`` | ``not_needed`` | ``refused`` | ``busy`` |
    ``skipped`` (the record moved on: a teardown or a Configure won)."""
    token = claim(deps, iid, force=force)
    if not token:
        return {"status": "busy", "message": "Another route repair is running for this database, "
                                             "or it is not a configured host-route database", "steps": []}
    try:
        return _repair_claimed(deps, iid, token, actor, force)
    finally:
        release(deps, iid, token)


def _routes_cond(record: dict, routes: list, token: str, values: dict) -> str:
    """The claim condition plus "host_routes is still `routes`" (the list this repair
    read, or the one it wrote ahead). Adds its values to `values`."""
    values.update({":configured": "configured", ":tok": token})
    if "host_routes" in record:
        values[":cur_routes"] = routes
        return "#st = :configured AND #tok = :tok AND #hr = :cur_routes"
    return "#st = :configured AND #tok = :tok AND attribute_not_exists(#hr)"


def _write_ahead(deps: Deps, iid: str, token: str, record: dict, old_routes: list, p) -> Optional[list]:
    """Record the ownership the plan intends BEFORE any EC2 call: every `/32` it owns now
    plus every one it is about to create or replace (a removed one stays owned until its
    delete is confirmed). Conditional on the claim and on the routes it read, so a
    Configure or a teardown that got in first wins and nothing in EC2 is touched; and a
    crash or a lost race after the EC2 calls leaves every `/32` it made owned, for a later
    pass to reconcile. The intended list, or None when the record moved on."""
    owned = {o.get("ip"): o.get("host", "") for o in old_routes if isinstance(o, dict) and o.get("ip")}
    for ip, host in list(p.add) + list(p.replace):
        owned[ip] = host
    intended = [{"ip": ip, "host": owned[ip]} for ip in sorted(owned)]
    values = {":intended": intended, ":ts": _iso(deps.now())}
    cond = _routes_cond(record, old_routes, token, values)
    try:
        deps.table.update_item(
            Key={"instance_id": iid}, UpdateExpression="SET #hr = :intended, #ua = :ts",
            ConditionExpression=cond,
            ExpressionAttributeNames={"#hr": "host_routes", "#ua": "updated_at", "#st": "status", "#tok": CLAIM},
            ExpressionAttributeValues=values)
    except Exception as e:  # noqa: BLE001
        if _code(e) == "ConditionalCheckFailedException":
            return None
        raise
    return intended


def _repair_claimed(deps: Deps, iid: str, token: str, actor: str, manual: bool) -> dict:
    record = (deps.table.get_item(Key={"instance_id": iid}) or {}).get("Item") or {}
    old_routes = list(record.get("host_routes") or [])
    old_flag = record.get(FLAG) or {}
    p, _table, why = _plan(deps, record)
    steps, res, cur_routes = [], None, old_routes
    if p is not None and p.changes:
        intended = _write_ahead(deps, iid, token, record, old_routes, p)
        if intended is None:
            logger.info("route repair for %s: the record moved on before any route change", iid)
            return {"status": "skipped", "message": "The database changed before it could be repaired "
                                                    "(a Configure or a removal); nothing was changed",
                    "steps": []}
        record, cur_routes = {**record, "host_routes": intended}, intended
        res = host_routes.apply(deps.ec2, deps.route_table_id, p, record.get("network_peering_id", ""))
        steps = res["steps"]
    refused = []
    if p is None and why:
        refused = [why]
    elif p is not None:
        for _ip, reason in (res["refused"] if res else p.refused):
            if reason not in refused:
                refused.append(reason)
        if res and res["failed"]:
            refused += [s for s in steps if s.startswith("✕")]
    now = _iso(deps.now())
    reason = "; ".join(refused)
    # A refusal is recorded (config_log, audit, notification) when it is new or its reason
    # changed, and on every Repair click; the loop's identical retry records nothing.
    new_refusal = bool(refused) and (manual or old_flag.get("refused") != reason)
    if refused and not new_refusal and not res:
        return {"status": "refused", "message": f"Route repair refused: {reason}", "steps": []}
    log = list(record.get("config_log") or [])
    if res:
        log += [f"{now} Host route repair: {s}" for s in steps]
    if new_refusal:
        log.append(f"{now} Host route repair refused: {reason}")
    elif not refused and not res:
        log.append(f"{now} Host route repair: nothing to change"
                   + (" (the VPC-wide route reaches it now)" if p is None else ""))
    log = log[-CONFIG_LOG_MAX:]
    sets = {"#cl": (":log", log), "#ua": (":ts", now)}
    if res:
        sets["#hr"] = (":owned", res["owned"])
    removes = []
    if refused:
        flag = {**old_flag, "refused": reason,
                "refused_at": now if new_refusal else old_flag.get("refused_at", now)}
        flag.setdefault("detected_at", now)
        sets["#fl"] = (":flag", flag)
    else:
        removes.append("#fl")
    names = {"#cl": "config_log", "#ua": "updated_at", "#hr": "host_routes", "#fl": FLAG,
             "#st": "status", "#tok": CLAIM}
    values = {v[0]: v[1] for v in sets.values()}
    # Confirm against what this repair wrote ahead (or read, when it changed nothing).
    cond = _routes_cond(record, cur_routes, token, values)
    expr = "SET " + ", ".join(f"{k} = {v[0]}" for k, v in sets.items())
    if removes:
        expr += " REMOVE " + ", ".join(removes)
    used = {k for k in names if k in expr or k in cond}
    mutated = bool(res and (res["added"] or res["removed"]))

    def audit_mutation(confirmed: bool):
        # Names what was actually done to the route table, confirmed on the record or not.
        deps.audit(actor, "network_host_routes_repaired", iid,
                   {"host_routes": sorted(_owned_ips({"host_routes": old_routes}))},
                   {"added": res["added"], "removed": res["removed"],
                    "refused": [{"ip": ip, "reason": r} for ip, r in res["refused"]],
                    "peering_id": record.get("network_peering_id", ""),
                    **({} if confirmed else {"confirmed": False,
                                             "recorded_ahead": sorted(_owned_ips({"host_routes": cur_routes}))})},
                   record)

    try:
        deps.table.update_item(Key={"instance_id": iid}, UpdateExpression=expr, ConditionExpression=cond,
                               ExpressionAttributeNames={k: v for k, v in names.items() if k in used},
                               ExpressionAttributeValues=values)
    except Exception as e:  # noqa: BLE001
        if _code(e) == "ConditionalCheckFailedException":
            logger.info("route repair for %s: the record moved on (teardown or Configure)", iid)
            if mutated:
                audit_mutation(confirmed=False)
                return {"status": "skipped",
                        "message": "The database changed while it was being repaired (a Configure or a "
                                   "removal). The route changes made were recorded as owned before they were "
                                   "made, and are audited; the next pass reconciles them",
                        "steps": steps}
            return {"status": "skipped", "message": "The database changed while it was being repaired "
                                                    "(a Configure or a removal); nothing was recorded",
                    "steps": steps}
        raise
    record = {**record, "config_log": log}
    if mutated:
        audit_mutation(confirmed=True)
    if new_refusal:
        deps.audit(actor, "network_host_routes_repair_refused", iid,
                   {"route_repair_needed": {k: old_flag.get(k) for k in ("member", "address", "detected_at")}},
                   {"reason": reason}, record)
        label = record.get("display_name") or iid
        deps.notify("Host route repair refused",
                    f"The cross-Region host routes of {label} ({iid}) could not be repaired: {reason}. "
                    f"Members without a route are not collected. Fix the cause, then use Repair or "
                    f"Reconfigure network in the admin console.")
    if refused:
        return {"status": "refused", "message": f"Route repair refused: {reason}", "steps": steps}
    if res and (res["added"] or res["removed"]):
        return {"status": "repaired", "message": "Host routes repaired", "steps": steps}
    return {"status": "not_needed", "message": "Nothing to change: every member address is routed",
            "steps": steps}


def run_flagged(deps: Deps) -> dict:
    """One pass of the background loop: repair every flagged record. ``{iid: status}``."""
    out = {}
    for rec in flagged(deps.table):
        iid = rec.get("instance_id", "")
        try:
            out[iid] = repair(deps, iid)["status"]
        except Exception as e:  # noqa: BLE001 - one record never stops the pass
            logger.error("route repair of %s failed: %s", iid, type(e).__name__)
            out[iid] = "error"
    return out
