# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Cross-Region host routes: the `/32`s that reach a database whose VPC CIDR clashes.

When a database's VPC CIDR is already routed in the agent's private route table to
another VPC's peering (two VPCs on one template default), the database is reached by a
`/32` host route per member address instead. Configure creates those routes
(``network_client._ensure_agent_routes``); the Tier 1 collector flags drift when a
member's address moves (failover, instance replacement, a member added or removed) and
the API task repairs it (``route_repair.repair``). Both writers plan through this one
module so their safety rules cannot drift:

* never route an address outside the database's own VPC CIDR (a public or foreign
  address would black-hole through the peering);
* never take over an address another registered database in the route holder's VPC
  uses (the `/32` would win longest-prefix match and hijack its traffic);
* never overwrite a `/32` that points at another live target;
* fail closed: when it cannot be shown that an address is not another database's (the
  holder's VPC is unknown, the registry or a registered endpoint cannot be read), add
  nothing. Removing our own `/32` is allowed only when it is KNOWN stale (the member is
  gone or its name now resolves elsewhere) or known to hijack; a name that does not
  resolve, or a registry that cannot be read, keeps the route it has.

A record OWNS the `/32`s listed in its ``host_routes`` (``[{"ip", "host"}]``). It only
ever deletes a route it owns, that still points at its peering, and that no other record
on the same peering owns (two databases in one DB VPC share a peering).

A leaf module: standard library only. Every AWS client, resolver and record list is
passed in, so the rules are testable without AWS (``tests/test_host_routes.py``).
"""
import concurrent.futures
import ipaddress
import logging
import os
import socket
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("host-routes")


# ── Addresses ─────────────────────────────────────────────────────────────────

# socket.getaddrinfo has no per-call timeout and ignores socket.setdefaulttimeout, so a
# slow or unreachable resolver blocks the request handler for the OS default (tens of
# seconds) — enough to stall the Members tab, the member picker or a chat turn's
# connection setup past the 29 s gateway cap. Run it in a worker and bound the wait; a
# timeout returns [] like any other failure to resolve, which every caller already treats
# as UNKNOWN (plan keeps the route, dial_gate shows the skip badge, registered_addresses
# fails closed), never as proof the address is absent.
def _resolve_timeout_s() -> float:
    try:
        return max(0.1, float(os.getenv("HOST_RESOLVE_TIMEOUT_S") or "2"))
    except (TypeError, ValueError):
        return 2.0


_resolver_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="dns-resolve")


def _getaddrinfo(host) -> List[str]:
    infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def resolve(host) -> List[str]:
    """IPv4 addresses `host` resolves to, sorted; [] if none, unresolvable, or the lookup
    timed out. A timeout is UNKNOWN, not absent: callers keep the route / show the skip
    badge rather than deleting a working one."""
    if not host:
        return []
    future = _resolver_pool.submit(_getaddrinfo, host)
    try:
        return future.result(timeout=_resolve_timeout_s())
    except concurrent.futures.TimeoutError:
        # The worker stays blocked until the OS lookup returns; the handler does not.
        logger.warning("DNS resolution of %s timed out after %ss; treating as unknown",
                       host, _resolve_timeout_s())
        return []
    except OSError as e:
        logger.warning(f"Could not resolve {host}: {e}")
        return []


def _default_resolve(host) -> List[str]:
    # Looked up at call time, so a test can replace the module's resolve().
    return resolve(host)


def ip_in_cidr(ip, cidr) -> bool:
    """Whether `ip` is inside `cidr`; bad input is 'not inside'."""
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False


def route_target(route: dict) -> str:
    """The target id of a route-table entry ('' when it has none)."""
    return (route.get("VpcPeeringConnectionId") or route.get("GatewayId")
            or route.get("NatGatewayId") or route.get("TransitGatewayId")
            or route.get("NetworkInterfaceId") or route.get("InstanceId") or "")


def route_holder(routes: Iterable[dict], cidr: str) -> str:
    """The target of the VPC-wide route for `cidr` in a route table, or ''."""
    for r in routes or ():
        if r.get("DestinationCidrBlock") == cidr:
            return route_target(r)
    return ""


def peer_vpc_of(ec2, peering_id: str, agent_vpc_id: str) -> str:
    """The VPC on the far side of `peering_id` from the agent VPC, or '' (unknown)."""
    if not peering_id:
        return ""
    try:
        conns = ec2.describe_vpc_peering_connections(
            VpcPeeringConnectionIds=[peering_id])["VpcPeeringConnections"]
    except Exception as e:  # noqa: BLE001 - unknown means fail closed, never a pass
        logger.warning(f"Could not describe {peering_id}: {e}")
        return ""
    for c in conns:
        req = c.get("RequesterVpcInfo", {}).get("VpcId")
        acc = c.get("AccepterVpcInfo", {}).get("VpcId")
        return acc if req == agent_vpc_id else req or ""
    return ""


def record_hosts(record: dict) -> List[str]:
    """Every name a record's members answer on: the record endpoint, the reader
    endpoint and each member's instance endpoint (deduplicated, in that order)."""
    hosts = [record.get("endpoint") or "", record.get("reader_endpoint") or ""]
    hosts += [m.get("endpoint") or "" for m in record.get("cluster_member_roles") or []]
    out = []
    for h in hosts:
        if h and h not in out:
            out.append(h)
    return out


def registered_addresses(records: Iterable[dict], vpc_id: str, exclude_vpc_id: str = "",
                         resolve: Optional[Callable[[str], List[str]]] = None) -> Dict[str, str]:
    """{private_ip: label} for every registered database in `vpc_id`: its endpoint,
    reader endpoint and every member endpoint. This is the GATE for a host route, so
    it fails closed: a name in that VPC that does not resolve raises RuntimeError
    rather than contributing nothing. The label (another tenant's name) is for the
    server log only."""
    resolve = resolve or _default_resolve
    if not vpc_id or vpc_id == exclude_vpc_id:
        return {}
    out = {}
    for rec in records:
        if rec.get("vpc_id") != vpc_id:
            continue
        label = rec.get("display_name") or rec.get("instance_id") or rec.get("endpoint", "")
        for host in record_hosts(rec):
            ips = resolve(host)
            if not ips:
                logger.warning("address check: %s of %s in %s could not be resolved", host, label, vpc_id)
                raise RuntimeError(
                    f"the endpoint of another registered database in {vpc_id} could not be resolved")
            for ip in ips:
                out[ip] = label
    return out


def desired_addresses(hosts: Iterable[str], resolve: Optional[Callable[[str], List[str]]] = None
                      ) -> Tuple[Dict[str, str], List[str]]:
    """``({ip: host}, [unresolved hosts])`` for the names a record must be reachable on."""
    resolve = resolve or _default_resolve
    desired, unresolved = {}, []
    for host in hosts:
        ips = resolve(host)
        if not ips:
            unresolved.append(host)
        for ip in ips:
            desired.setdefault(ip, host)
    return desired, unresolved


# ── Planning ──────────────────────────────────────────────────────────────────

def outside_reason(host, ip, cidr, peering_id) -> str:
    return (f"{host} resolved to {ip}, outside the database's VPC {cidr}; refusing to route a "
            f"public or foreign address through {peering_id}. If the instance is publicly "
            f"accessible, DNS must resolve to its private address from the agent VPC before it "
            f"can be onboarded this way")


def taken_reason(host, cidr) -> str:
    # Names neither the other database nor the address: this text lands in this
    # database's config_log, which this space's admins read.
    return (f"{host} resolves to a private address that another registered database in the VPC "
            f"holding the {cidr} route also uses; a host route would take over that database's "
            f"traffic. Onboard this database through a VPC with a different CIDR, or re-address "
            f"one of them")


def foreign_reason(ip, target) -> str:
    return f"{ip}/32 is already routed to {target}, not this database's peering; leaving it"


@dataclass
class Plan:
    """What to do with the record's `/32`s. ``add``/``replace``/``keep``: ``[(ip, host)]``;
    ``remove``/``forget``: ``[ip]``; ``refused``: ``[(ip, reason)]``."""
    add: List[Tuple[str, str]] = field(default_factory=list)
    replace: List[Tuple[str, str]] = field(default_factory=list)
    keep: List[Tuple[str, str]] = field(default_factory=list)
    remove: List[str] = field(default_factory=list)
    forget: List[str] = field(default_factory=list)
    refused: List[Tuple[str, str]] = field(default_factory=list)
    hosts: Dict[str, str] = field(default_factory=dict)   # ip -> host, for every ip above

    @property
    def changes(self) -> bool:
        """Whether applying it touches the route table or the ownership list."""
        return bool(self.add or self.replace or self.remove or self.forget)


def _owned_map(owned) -> Dict[str, str]:
    out = {}
    for o in owned or ():
        if isinstance(o, dict):
            if o.get("ip"):
                out[o["ip"]] = o.get("host", "")
        elif o:
            out[str(o)] = ""
    return out


def plan(owned, desired: Dict[str, str], db_vpc_cidr: str, our_peering: str,
         table_routes: Iterable[dict], taken: Optional[Dict[str, str]],
         other_owned: Optional[Iterable[str]] = (), gate_reason: str = "",
         unresolved: Iterable[str] = ()) -> Plan:
    """Plan a record's host routes. Pure.

    ``owned``: the record's ``host_routes``. ``desired``: ``{ip: host}`` the record must
    be reachable on now. ``table_routes``: the agent route table's routes.
    ``taken``: ``{ip: label}`` addresses of registered databases in the route holder's
    VPC, or ``None`` when that cannot be known; then ``gate_reason`` says why, and
    nothing is added (fail closed). ``other_owned``: IPs other records on the same
    peering own, or ``None`` when that cannot be known.

    An owned address that is not desired is only removed when it is KNOWN to be stale.
    It is kept (still routed, still owned) when its recorded host is in ``unresolved``
    (the name did not resolve this time, so where it points now is unknown) or when
    ``other_owned`` is ``None`` (whether another record shares the `/32` is unknown):
    a resolver or registry outage never deletes a working route. An owned address
    refused because another registered database uses it is always removed: that `/32`
    is a hijack."""
    owned_map = _owned_map(owned)
    unsure = other_owned is None
    other = set(other_owned or ())
    unresolved_hosts = {h for h in unresolved or () if h}
    routes = {r.get("DestinationCidrBlock"): r for r in table_routes or ()
              if (r.get("DestinationCidrBlock") or "").endswith("/32")}
    p = Plan()
    settled, hijack = set(), set()   # hijack: known to be another database's address
    for ip in sorted(desired):
        host = desired[ip]
        p.hosts[ip] = host
        if not ip_in_cidr(ip, db_vpc_cidr):
            p.refused.append((ip, outside_reason(host, ip, db_vpc_cidr, our_peering)))
            continue
        route = routes.get(f"{ip}/32")
        target = route_target(route) if route else ""
        if taken is None:
            if route and target == our_peering and route.get("State") != "blackhole":
                p.keep.append((ip, host))
                settled.add(ip)
            else:
                p.refused.append((ip, gate_reason or "the address check could not be completed"))
            continue
        if ip in taken:
            p.refused.append((ip, taken_reason(host, db_vpc_cidr)))
            hijack.add(ip)
            continue
        if not route:
            p.add.append((ip, host))
        elif route.get("State") == "blackhole":
            p.replace.append((ip, host))
        elif target == our_peering:
            p.keep.append((ip, host))
        else:
            p.refused.append((ip, foreign_reason(ip, target)))
            continue
        settled.add(ip)
    for ip in sorted(owned_map):
        if ip in settled:
            continue
        p.hosts.setdefault(ip, owned_map[ip])
        route = routes.get(f"{ip}/32")
        ours = bool(route) and route_target(route) == our_peering
        # Kept only while it is not known to be another registered database's address:
        # a name that fails DNS keeps its /32, but never one that now hijacks a neighbour.
        if ours and ip not in hijack and (taken is None or ip not in taken) \
                and (unsure or owned_map[ip] in unresolved_hosts):
            p.keep.append((ip, owned_map[ip]))
        elif ours and ip not in other:
            p.remove.append(ip)
        else:
            p.forget.append(ip)
    return p


# ── Applying ──────────────────────────────────────────────────────────────────

def _code(e: Exception) -> str:
    resp = getattr(e, "response", None) or {}
    code = (resp.get("Error") or {}).get("Code", "")
    return code or str(e)


def _short(e: Exception) -> str:
    code = (getattr(e, "response", None) or {}).get("Error", {}).get("Code", "")
    return code or type(e).__name__


def _read_route(ec2, route_table_id, cidr) -> Optional[dict]:
    try:
        resp = ec2.describe_route_tables(RouteTableIds=[route_table_id])
        for r in resp["RouteTables"][0].get("Routes", []):
            if r.get("DestinationCidrBlock") == cidr:
                return r
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not re-read {cidr} in {route_table_id}: {e}")
    return None


def apply(ec2, route_table_id: str, p: Plan, peering_id: str) -> dict:
    """Carry the plan out on the agent route table: adds and replaces first, then
    removes, so a moved writer never loses its path mid-run. Never raises: a failed
    call is a step ``"✕ <ip>/32: <error>"`` and the address keeps its previous
    ownership state. Returns ``{"steps", "owned", "added", "removed", "refused",
    "failed"}`` where ``owned`` is the new ``host_routes`` list."""
    steps, added, removed, failed = [], [], [], []
    refused = list(p.refused)
    owned = {ip: host for ip, host in p.keep}
    for ip, host in p.add:
        cidr = f"{ip}/32"
        try:
            ec2.create_route(RouteTableId=route_table_id, DestinationCidrBlock=cidr,
                             VpcPeeringConnectionId=peering_id)
            owned[ip] = host
            added.append(ip)
            steps.append(f"Agent route: {cidr} -> {peering_id}")
        except Exception as e:  # noqa: BLE001
            if "RouteAlreadyExists" not in _code(e) and "RouteAlreadyExists" not in str(e):
                failed.append(ip)
                steps.append(f"✕ {cidr}: {_short(e)}")
                continue
            route = _read_route(ec2, route_table_id, cidr)
            if route and route_target(route) == peering_id:
                owned[ip] = host
                steps.append(f"Agent route: {cidr} -> {peering_id} (already in place)")
            else:
                reason = foreign_reason(ip, route_target(route) if route else "an unreadable target")
                refused.append((ip, reason))
                steps.append(f"⚠ Host route refused: {reason}")
    for ip, host in p.replace:
        cidr = f"{ip}/32"
        try:
            ec2.replace_route(RouteTableId=route_table_id, DestinationCidrBlock=cidr,
                              VpcPeeringConnectionId=peering_id)
            owned[ip] = host
            added.append(ip)
            steps.append(f"Agent route: {cidr} -> {peering_id} (replaced a blackhole route)")
        except Exception as e:  # noqa: BLE001
            failed.append(ip)
            steps.append(f"✕ {cidr}: {_short(e)}")
    for ip in p.remove:
        cidr = f"{ip}/32"
        try:
            ec2.delete_route(RouteTableId=route_table_id, DestinationCidrBlock=cidr)
            removed.append(ip)
            steps.append(f"Removed agent route: {cidr}")
        except Exception as e:  # noqa: BLE001
            if "InvalidRoute.NotFound" in _code(e) or "InvalidRoute.NotFound" in str(e):
                removed.append(ip)
                steps.append(f"Removed agent route: {cidr} (already gone)")
            else:
                owned[ip] = p.hosts.get(ip, "")   # still ours; the next run retries
                failed.append(ip)
                steps.append(f"✕ {cidr}: {_short(e)}")
    for ip in p.forget:
        steps.append(f"Released {ip}/32 (no longer this database's route)")
    for ip, reason in p.refused:
        steps.append(f"⚠ Host route refused: {reason}")
    return {"steps": steps, "added": added, "removed": removed, "failed": failed,
            "refused": refused,
            "owned": [{"ip": ip, "host": owned[ip]} for ip in sorted(owned)]}


# ── Which member Tier 1 and the reader scan may dial ──────────────────────────

LATTICE_SKIP = "skipped: not reachable over the Lattice resource endpoint"
# A member whose instance endpoint does not resolve: the cause is DNS, not a missing
# route, so it has its own text (its address is unknown, and its owned /32 is kept).
UNRESOLVABLE_SKIP = "skipped: instance endpoint does not resolve"


def not_routed_skip(ip: str) -> str:
    """The skip reason for a member whose address has no owned `/32`."""
    return f"skipped: not routed ({ip} has no owned host route)"


def broken_route_skip(ip: str, issue: str) -> str:
    """The skip reason for a member whose owned `/32` Tier 1 found missing, blackholed or
    pointing elsewhere in the live route table (``route_repair_needed``)."""
    return f"skipped: not routed ({ip}/32: {issue}; route repair needed)"


def dial_gate(record: dict, member: dict, resolve: Optional[Callable[[str], List[str]]] = None
              ) -> Tuple[Optional[str], str]:
    """``(host, reason)``: the host to dial for one cluster `member` of `record`, or
    ``(None, "skipped: ...")`` when that member cannot be reached on this record's
    network path. The writer always has a host: the cluster endpoint follows it and is
    always routed. The host comes only from the registry (RDS), never from a request.

    * Lattice: the resource endpoint serves the cluster endpoint only. Readers are
      skipped; the writer is dialled on the record endpoint.
    * Host routes (`network_route_mode == "host"`): a member's instance endpoint is
      dialled only when every address it resolves to has an owned `/32` that Tier 1 has
      not flagged (``route_repair_needed``: the live route is missing, blackholed or
      points elsewhere); otherwise a reader is skipped and the writer falls back to the
      record endpoint.
    * Otherwise (a VPC-wide route, a reused route, the same VPC): the instance endpoint.
    """
    resolve = resolve or _default_resolve
    is_writer = bool(member.get("is_writer"))
    endpoint = member.get("endpoint") or ""
    if record.get("network_method") == "lattice":
        if is_writer:
            return record.get("endpoint") or endpoint or None, ""
        return None, LATTICE_SKIP
    if not endpoint:
        if is_writer:
            return record.get("endpoint") or None, ""
        return None, "skipped: no instance endpoint"
    if record.get("network_route_mode") == "host":
        routed = {r.get("ip") for r in record.get("host_routes") or [] if isinstance(r, dict)}
        broken = {i.get("address"): i.get("issue", "") for i in
                  (record.get("route_repair_needed") or {}).get("items") or [] if isinstance(i, dict)}
        ips = resolve(endpoint)
        why = ""
        if not ips:
            why = UNRESOLVABLE_SKIP
        else:
            missing = [ip for ip in ips if ip not in routed]
            flagged = [ip for ip in ips if ip in broken]
            if missing:
                why = not_routed_skip(missing[0])
            elif flagged:
                why = broken_route_skip(flagged[0], broken[flagged[0]])
        if why:
            if is_writer:
                return record.get("endpoint") or None, ""
            return None, why
    return endpoint, ""
