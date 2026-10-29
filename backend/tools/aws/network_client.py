# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Network path from the agent VPC to a monitored database.

VPC peering by default, in any region. When the database VPC's CIDR is already routed
to another peered VPC, a same-region database is reached over a VPC Lattice resource
endpoint (resource gateway in the database VPC, resource endpoint in the agent VPC,
private DNS so the database's own hostname resolves to it) and a cross-region database
over its own peering with a host route for just its address. The SameRegionConnectivity
template parameter (SAME_REGION_CONNECTIVITY) can make Lattice the rule for every
same-region database instead of the fallback.
"""

import contextlib
import contextvars
import os
import re
import time
import logging
import boto3
from tools.resource_names import (agent_vpc_tag_key, managed_by_tag, peering_name, resource_prefix,
                                  security_group_name)

logger = logging.getLogger("network-client")

SAME_REGION_MODES = ("peering-first", "lattice")


def same_region_connectivity() -> str:
    """How same-region databases are reached: ``peering-first`` (default) or ``lattice``.
    An unrecognised value is treated as the default rather than refused, so a typo in the
    environment cannot stop onboarding."""
    mode = os.getenv("SAME_REGION_CONNECTIVITY", "").strip().lower()
    return mode if mode in SAME_REGION_MODES else "peering-first"


# Bounded waits on the Lattice path (seconds).
GATEWAY_ACTIVE_BUDGET_S = 300
SHARE_CONFIRM_BUDGET_S = 120
DELETE_WAIT_BUDGET_S = 300
POLL_INTERVAL_S = 5

_ARN_TO_HIDE = re.compile(r"arn:aws[a-z-]*:(iam|sts|secretsmanager):[^\s'\"]+")


def _paged(call, items_key, token_key="nextToken", **kw):
    """Yield every item of a paginated AWS list call, following `token_key`
    (``nextToken`` for VPC Lattice and RAM, ``NextToken`` for EC2)."""
    token = None
    while True:
        args = dict(kw)
        if token:
            args[token_key] = token
        resp = call(**args) or {}
        for item in resp.get(items_key) or []:
            yield item
        token = resp.get(token_key)
        if not token:
            return


# The caller's heartbeat for long waits (a background teardown refreshes its
# ``deleting`` claim with it, so a live teardown is never mistaken for a stalled one).
# A context variable, so concurrent teardowns in the thread pool each keep their own.
_WAIT_HEARTBEAT = contextvars.ContextVar("network_wait_heartbeat", default=None)


@contextlib.contextmanager
def wait_heartbeat(beat):
    """Call `beat()` on every poll of every bounded wait run inside this block."""
    token = _WAIT_HEARTBEAT.set(beat)
    try:
        yield
    finally:
        _WAIT_HEARTBEAT.reset(token)


def _wait_until(probe, budget_s, interval_s):
    """Poll `probe()` until it reports done, a terminal error, or `budget_s` runs out.

    `probe()` returns ``(done, terminal_error, last_state)``. Returns
    ``(ok, last_state, error)``: ``error`` is the terminal error, or "" on success and on
    a timeout (``ok`` False with no error means the budget ran out). Each poll calls the
    heartbeat set by wait_heartbeat, if any; a failing heartbeat does not stop the wait."""
    deadline = time.monotonic() + budget_s
    while True:
        done, error, last_state = probe()
        if done:
            return True, last_state, ""
        if error:
            return False, last_state, error
        if time.monotonic() >= deadline:
            return False, last_state, ""
        beat = _WAIT_HEARTBEAT.get()
        if beat:
            try:
                beat()
            except Exception as e:  # noqa: BLE001 - a missed heartbeat must not fail the teardown
                logger.warning("wait heartbeat failed: %s", type(e).__name__)
        time.sleep(interval_s)  # nosemgrep: arbitrary-sleep - bounded poll while a Lattice/RAM/EC2 resource changes state


def _short_error(e) -> str:
    """An AWS error for the registry and the steps: role and secret ARNs removed, bounded."""
    return _ARN_TO_HIDE.sub("<arn>", str(e))[:300]


class RouteConflict(RuntimeError):
    """A route for the destination already exists and points at a live target of ours
    that is not the one being asserted. `holder` is that target (a peering ID when the
    route goes over a peering), so the caller can decide what to do about it."""

    def __init__(self, route_table_id, cidr, holder):
        self.route_table_id, self.cidr, self.holder = route_table_id, cidr, holder
        super().__init__(
            f"A route for {cidr} in {route_table_id} already points to {holder or 'another target'}. "
            f"Refusing to overwrite it; resolve the conflicting route and retry.")


class NetworkConfigurator:

    def __init__(self):
        self._ec2 = None

    @property
    def ec2(self):
        if self._ec2 is None:
            self._ec2 = boto3.client("ec2", region_name=os.getenv("AWS_REGION", "us-east-1"))
        return self._ec2

    @property
    def agent_vpc_id(self):
        return os.getenv("AGENT_VPC_ID", "")

    @property
    def agent_vpc_cidr(self):
        return os.getenv("AGENT_VPC_CIDR", "172.20.0.0/16")

    @property
    def agent_route_table_id(self):
        return os.getenv("AGENT_PRIVATE_ROUTE_TABLE_ID", "")

    @property
    def agent_security_group_id(self):
        return os.getenv("AGENT_PRIVATE_SECURITY_GROUP_ID", "")

    # ──────────────────────────────────────────────
    # CIDR conflict detection
    # ──────────────────────────────────────────────

    def _check_cidr_conflict(self, db_vpc_cidr: str, db_vpc_id: str) -> dict:
        """Check if db_vpc_cidr conflicts with an already-peered VPC.

        A conflict exists when another VPC with the same CIDR is already peered.
        The VPC that owns the route (or was peered first) keeps peering; others reach the
        database over a Lattice resource endpoint (same region) or a host route (cross region).
        """
        all_with_cidr = self._get_all_peerings_for_cidr(db_vpc_cidr)

        # Filter to only peerings for OTHER VPCs
        other_vpc_peerings = [p for p in all_with_cidr if p["vpc_id"] != db_vpc_id]

        if not other_vpc_peerings:
            # No other VPC has this CIDR — no conflict
            return {}

        # Another VPC has the same CIDR. Check if THIS VPC owns the route.
        routed_peering = self._get_routed_peering_for_cidr(db_vpc_cidr)
        my_peerings = [p for p in all_with_cidr if p["vpc_id"] == db_vpc_id]

        # If this VPC's peering owns the route, no conflict for this VPC
        if routed_peering and my_peerings:
            if any(p["peering_id"] == routed_peering for p in my_peerings):
                return {}

        # This VPC doesn't own the route — it needs another path
        return other_vpc_peerings[0]

    def _get_routed_peering_for_cidr(self, cidr: str) -> str:
        """Return the peering connection ID that the agent route table routes this CIDR to, or ''."""
        if not self.agent_route_table_id:
            return ""
        try:
            resp = self.ec2.describe_route_tables(RouteTableIds=[self.agent_route_table_id])
            for route in resp["RouteTables"][0].get("Routes", []):
                if route.get("DestinationCidrBlock") == cidr and route.get("VpcPeeringConnectionId"):
                    return route["VpcPeeringConnectionId"]
        except Exception as e:
            logger.warning(f"Failed to check route for {cidr}: {e}")
        return ""

    def _get_all_peerings_for_cidr(self, cidr: str) -> list:
        """Return all active peerings where the accepter VPC has the given CIDR."""
        results = []
        try:
            resp = self.ec2.describe_vpc_peering_connections(
                Filters=[
                    {"Name": "requester-vpc-info.vpc-id", "Values": [self.agent_vpc_id]},
                    {"Name": "status-code", "Values": ["active"]},
                ]
            )
            for conn in resp.get("VpcPeeringConnections", []):
                accepter = conn.get("AccepterVpcInfo", {})
                if accepter.get("CidrBlock") == cidr:
                    results.append({
                        "peering_id": conn["VpcPeeringConnectionId"],
                        "vpc_id": accepter.get("VpcId", ""),
                    })
        except Exception as e:
            logger.warning(f"Failed to list peerings for CIDR {cidr}: {e}")
        return results

    def _find_existing_peering(self, db_vpc_id: str) -> dict:
        """Find an existing active or pending peering to the given VPC."""
        try:
            resp = self.ec2.describe_vpc_peering_connections(
                Filters=[
                    {"Name": "requester-vpc-info.vpc-id", "Values": [self.agent_vpc_id]},
                    {"Name": "status-code", "Values": ["active", "pending-acceptance", "provisioning"]},
                ]
            )
            for conn in resp.get("VpcPeeringConnections", []):
                if conn.get("AccepterVpcInfo", {}).get("VpcId") == db_vpc_id:
                    return {
                        "peering_id": conn["VpcPeeringConnectionId"],
                        "status": conn["Status"]["Code"],
                    }
        except Exception as e:
            logger.warning(f"Failed to check existing peerings: {e}")
        return {}

    # ──────────────────────────────────────────────
    # Main entry point
    # ──────────────────────────────────────────────

    def configure_network(self, db_vpc_id: str, db_security_group_ids: str,
                          db_port: int = 5432, db_account_id: str = "",
                          db_region: str = "", cross_account_session=None,
                          db_endpoint: str = "", db_subnet_ids: str = "",
                          instance_id: str = "", db_arn: str = "",
                          db_member_endpoints=(), owned_host_routes=()) -> dict:
        """Build the agent's network path to a database. `db_member_endpoints` are every
        name the database answers on (cluster, reader and member instance endpoints):
        on the cross-Region CIDR-clash path each gets a `/32` host route.
        `owned_host_routes` are the `/32`s the record already owns (its ``host_routes``):
        a Reconfigure reconciles them instead of forgetting them."""
        from tools.aws.network_registry import network_registry

        if db_vpc_id == self.agent_vpc_id:
            return {"status": "same_vpc", "steps": ["Same VPC — no peering needed"]}

        agent_account = boto3.client("sts").get_caller_identity()["Account"]
        is_cross_account = db_account_id and db_account_id != agent_account
        db_region = db_region or os.getenv("AWS_REGION", "us-east-1")
        agent_region = os.getenv("AWS_REGION", "us-east-1")
        is_cross_region = db_region != agent_region

        steps = []
        steps.append(f"Config: agent_vpc={self.agent_vpc_id}, db_vpc={db_vpc_id}")
        steps.append(f"  agent_account={agent_account}, db_account={db_account_id}")
        steps.append(f"  agent_region={agent_region}, db_region={db_region}")
        steps.append(f"  cross_account={is_cross_account}, cross_region={is_cross_region}")

        # ── Check registry for existing connection ──
        # Peering: check by VPC pair
        existing_peering = network_registry.find_active_peering(db_vpc_id)
        if existing_peering and existing_peering.get("peering_id"):
            # Validate the peering still exists and is active
            peering_valid = False
            try:
                resp = self.ec2.describe_vpc_peering_connections(
                    VpcPeeringConnectionIds=[existing_peering["peering_id"]]
                )
                if resp["VpcPeeringConnections"] and resp["VpcPeeringConnections"][0]["Status"]["Code"] == "active":
                    peering_valid = True
            except Exception:
                pass

            if peering_valid:
                # An active peering only proves the two VPCs are connected. The routes on
                # both sides, the DNS options and the DB-side security-group rule are what
                # carry traffic, and any of them can be absent — an earlier run interrupted
                # after the accept, or a route or rule removed since. So re-run the peering
                # configuration to assert all of them: every step is idempotent, so a
                # healthy path is a no-op and a drifted one is repaired. _configure_peering
                # adopts the active peering and re-registers this instance's reference,
                # which is why no reference is added here.
                steps.append(
                    f"Registered peering {existing_peering['peering_id']} is active; "
                    f"reasserting routes, DNS and the security-group rule"
                )
                db_vpc_cidr = ""
                try:
                    if is_cross_account and cross_account_session:
                        peer_ec2 = cross_account_session.client("ec2", region_name=db_region)
                    else:
                        peer_ec2 = boto3.client("ec2", region_name=db_region)
                    db_vpc_cidr = peer_ec2.describe_vpcs(VpcIds=[db_vpc_id])["Vpcs"][0]["CidrBlock"]
                except Exception as e:
                    steps.append(f"Cannot get DB VPC CIDR: {e}")
                return self._configure_peering(
                    db_vpc_id=db_vpc_id, db_vpc_cidr=db_vpc_cidr,
                    db_security_group_ids=db_security_group_ids, db_port=db_port,
                    db_account_id=db_account_id, db_region=db_region,
                    is_cross_account=is_cross_account, is_cross_region=is_cross_region,
                    cross_account_session=cross_account_session, steps=steps,
                    instance_id=instance_id, db_endpoint=db_endpoint,
                    db_member_endpoints=db_member_endpoints,
                    owned_host_routes=owned_host_routes,
                )
            else:
                steps.append(f"Registered peering {existing_peering['peering_id']} is stale — will recreate")
                network_registry.delete_connection(existing_peering["connection_id"])

        # Lattice: a database already connected over a resource endpoint keeps it. The
        # endpoint is this database's own, so validity is "the endpoint still exists".
        if db_endpoint:
            existing_lt = network_registry.find_active_lattice(db_endpoint, self.agent_vpc_id)
            if existing_lt and existing_lt.get("vpce_id"):
                vpce_valid = False
                try:
                    resp = self.ec2.describe_vpc_endpoints(VpcEndpointIds=[existing_lt["vpce_id"]])
                    if resp["VpcEndpoints"] and resp["VpcEndpoints"][0]["State"] in ("available", "pending"):
                        vpce_valid = True
                except Exception:
                    pass
                if vpce_valid:
                    network_registry.add_reference(existing_lt["connection_id"], instance_id)
                    steps.append(f"Reusing resource endpoint {existing_lt['vpce_id']}")
                    return {
                        "status": "configured", "method": "lattice",
                        "vpce_id": existing_lt["vpce_id"],
                        "resource_configuration_arn": existing_lt.get("resource_configuration_arn", ""),
                        "resource_share_arn": existing_lt.get("resource_share_arn", ""),
                        "steps": steps,
                    }
                steps.append("Registered resource endpoint is gone; will recreate")
                network_registry.delete_connection(existing_lt["connection_id"])

        # ── Get DB VPC CIDR early — needed for conflict check ──
        db_vpc_cidr = ""
        try:
            if is_cross_account and cross_account_session:
                peer_ec2 = cross_account_session.client("ec2", region_name=db_region)
                db_vpc = peer_ec2.describe_vpcs(VpcIds=[db_vpc_id])["Vpcs"][0]
            else:
                db_ec2 = boto3.client("ec2", region_name=db_region)
                db_vpc = db_ec2.describe_vpcs(VpcIds=[db_vpc_id])["Vpcs"][0]
            db_vpc_cidr = db_vpc["CidrBlock"]
            steps.append(f"DB VPC CIDR: {db_vpc_cidr}")
        except Exception as e:
            steps.append(f"Cannot get DB VPC CIDR: {e}")

        # ── Deployment-wide choice for same-region databases ──
        # SAME_REGION_CONNECTIVITY=lattice (the SameRegionConnectivity template parameter)
        # puts every same-region database behind a resource endpoint, so no route is ever
        # added to a database VPC. The default, peering-first, peers and keeps Lattice for
        # the address-range conflict below. Cross-region always peers: a resource endpoint
        # is regional. A database already connected keeps its path either way (above).
        if not is_cross_region and same_region_connectivity() == "lattice":
            steps.append("Deployment policy: same-region databases connect over a Lattice resource endpoint")
            return self._configure_lattice(
                db_vpc_id=db_vpc_id, db_subnet_ids=db_subnet_ids,
                db_security_group_ids=db_security_group_ids, db_port=db_port,
                db_account_id=db_account_id, db_region=db_region, db_endpoint=db_endpoint,
                db_arn=db_arn, cross_account_session=cross_account_session, steps=steps,
                instance_id=instance_id,
            )

        # ── Check for CIDR conflict with existing peerings ──
        conflict = self._check_cidr_conflict(db_vpc_cidr, db_vpc_id) if db_vpc_cidr else {}
        if conflict:
            # Cross-region databases MUST use peering — a Lattice resource endpoint is regional.
            # The VPC-wide route stays with the VPC that holds it; _ensure_agent_routes
            # routes this database's own address through its peering instead.
            if is_cross_region:
                steps.append(
                    f"CIDR conflict: {db_vpc_cidr} already peered to {conflict['vpc_id']} "
                    f"via {conflict['peering_id']}. Cross-region requires peering — will route "
                    f"this database's address through its own peering."
                )
            else:
                steps.append(
                    f"CIDR conflict: {db_vpc_cidr} already peered to {conflict['vpc_id']} "
                    f"via {conflict['peering_id']}. Connecting over a Lattice resource endpoint instead."
                )
                logger.info(f"CIDR conflict detected for {db_vpc_id} ({db_vpc_cidr}), using a Lattice resource endpoint")
                return self._configure_lattice(
                    db_vpc_id=db_vpc_id,
                    db_subnet_ids=db_subnet_ids,
                    db_security_group_ids=db_security_group_ids,
                    db_port=db_port,
                    db_account_id=db_account_id,
                    db_region=db_region,
                    db_endpoint=db_endpoint,
                    db_arn=db_arn,
                    cross_account_session=cross_account_session,
                    steps=steps,
                    instance_id=instance_id,
                )

        # ── No conflict — use VPC peering ──
        return self._configure_peering(
            db_vpc_id=db_vpc_id,
            db_vpc_cidr=db_vpc_cidr,
            db_security_group_ids=db_security_group_ids,
            db_port=db_port,
            db_account_id=db_account_id,
            db_region=db_region,
            is_cross_account=is_cross_account,
            is_cross_region=is_cross_region,
            cross_account_session=cross_account_session,
            steps=steps,
            instance_id=instance_id,
            db_endpoint=db_endpoint,
            db_member_endpoints=db_member_endpoints,
            owned_host_routes=owned_host_routes,
        )

    # ──────────────────────────────────────────────
    # VPC Peering path (existing logic)
    # ──────────────────────────────────────────────

    def _configure_peering(self, db_vpc_id, db_vpc_cidr, db_security_group_ids,
                           db_port, db_account_id, db_region,
                           is_cross_account, is_cross_region,
                           cross_account_session, steps, instance_id="",
                           db_endpoint="", db_member_endpoints=(), owned_host_routes=()) -> dict:

        # Step 1: Find, adopt or create the peering.
        #
        # An active peering found here is one AWS knows about and the registry does not —
        # the state an earlier attempt leaves if it stops between accepting the peering and
        # finishing the job, since routes, DNS, the security-group rule and the registry
        # entry all come afterwards.
        #
        # An existing peering is not proof the path carries traffic, so adopt it and carry
        # on through the remaining steps rather than returning here. They are all idempotent,
        # so re-running them against a healthy connection is a no-op and a partial one is
        # completed.
        existing = self._find_existing_peering(db_vpc_id)
        already_active = bool(existing and existing["status"] == "active")
        if already_active:
            peering_id = existing["peering_id"]
            steps.append(f"Adopting active peering {peering_id} and completing its configuration")
        elif existing and existing["status"] == "pending-acceptance":
            peering_id = existing["peering_id"]
            steps.append(f"Found pending peering {peering_id}")
        else:
            create_kwargs = {
                "VpcId": self.agent_vpc_id,
                "PeerVpcId": db_vpc_id,
                "TagSpecifications": [{
                    "ResourceType": "vpc-peering-connection",
                    "Tags": [
                        {"Key": "Name", "Value": peering_name(db_vpc_id)},
                        {"Key": "ManagedBy", "Value": managed_by_tag()},
                        # Pins the EXACT deployment that owns this peering. ManagedBy is
                        # keyed on the resource prefix, so two installations that share a
                        # prefix (dev and prod, or an earlier generation) carry the same
                        # value and cannot be told apart by it. The agent VPC id is unique
                        # to one installation, so a route-conflict claim can prove a stale
                        # peering is OURS (safe to repoint) rather than another live
                        # deployment's (never auto-claimed). See _classify_conflict_holder.
                        {"Key": agent_vpc_tag_key(), "Value": self.agent_vpc_id},
                    ],
                }],
            }
            if is_cross_account:
                create_kwargs["PeerOwnerId"] = db_account_id
            if is_cross_region:
                create_kwargs["PeerRegion"] = db_region

            steps.append(f"Creating peering: PeerOwnerId={create_kwargs.get('PeerOwnerId','n/a')}, PeerRegion={create_kwargs.get('PeerRegion','n/a')}")
            try:
                resp = self.ec2.create_vpc_peering_connection(**create_kwargs)
                peering_id = resp["VpcPeeringConnection"]["VpcPeeringConnectionId"]
                steps.append(f"Created peering {peering_id}")
            except Exception as e:
                steps.append(f"CREATE FAILED: {e}")
                return {"status": "failed", "method": "peering", "steps": steps, "error": str(e)}

        # Accepting and waiting only apply to a peering we just created or
        # found pending. An adopted one is already active.
        if not already_active:
            # Step 2: Accept peering (with retry for cross-region propagation delay)
            wait_secs = 5 if is_cross_region else 3
            max_retries = 3 if is_cross_region else 1
            time.sleep(wait_secs)  # nosemgrep: arbitrary-sleep - wait for async VPC peering to propagate before accept (background task)
            steps.append("Attempting accept...")

            for attempt in range(max_retries):
                try:
                    if is_cross_account and cross_account_session:
                        peer_ec2 = cross_account_session.client("ec2", region_name=db_region)
                        peer_ec2.accept_vpc_peering_connection(VpcPeeringConnectionId=peering_id)
                        steps.append(f"Accepted via cross-account role in {db_account_id}/{db_region}")
                    elif is_cross_region:
                        regional_ec2 = boto3.client("ec2", region_name=db_region)
                        regional_ec2.accept_vpc_peering_connection(VpcPeeringConnectionId=peering_id)
                        steps.append(f"Accepted cross-region from {db_region}")
                    else:
                        self.ec2.accept_vpc_peering_connection(VpcPeeringConnectionId=peering_id)
                        steps.append(f"Accepted same-account peering {peering_id}")
                    break
                except Exception as e:
                    if "already active" in str(e).lower():
                        steps.append("Already active")
                        break
                    elif "NotFound" in str(e) and attempt < max_retries - 1:
                        steps.append(f"Peering not yet visible in {db_region}, retrying ({attempt + 1}/{max_retries})...")
                        time.sleep(5)  # nosemgrep: arbitrary-sleep - retry backoff for cross-region peering propagation
                    else:
                        steps.append(f"ACCEPT FAILED: {e}")
                        return {"status": "accept_failed", "method": "peering", "peering_id": peering_id, "steps": steps, "error": str(e)}

            # Step 3: Wait for the peering to become active before configuring DNS/routes.
            #
            # Cross-account acceptance is asynchronous on AWS's side and its latency
            # varies: usually a second or two, occasionally minutes, so the budget is
            # generous. On timeout, report what AWS last said rather than only how long
            # we waited: "still provisioning" and "not allowed to look" would otherwise
            # render identically as a timeout, and they need opposite responses.
            budget = 300 if (is_cross_account or is_cross_region) else 60
            steps.append(f"Waiting up to {budget}s for peering to become active...")
            active = False
            last_status, last_error = "unknown", ""
            deadline = time.monotonic() + budget
            while time.monotonic() < deadline:
                try:
                    desc = self.ec2.describe_vpc_peering_connections(
                        VpcPeeringConnectionIds=[peering_id]
                    )
                    last_status = desc["VpcPeeringConnections"][0]["Status"]["Code"]
                    last_error = ""
                    if last_status == "active":
                        active = True
                        steps.append("Peering is active")
                        break
                    # These are terminal. Spending the rest of the budget on a peering
                    # that can never activate only delays the report.
                    if last_status in ("failed", "rejected", "deleted", "expired"):
                        steps.append(f"Peering reached terminal status '{last_status}'")
                        break
                except Exception as e:
                    last_error = str(e)
                time.sleep(3)  # nosemgrep: arbitrary-sleep - poll loop waiting for peering to reach 'active'

            if not active:
                why = f"last status '{last_status}'"
                if last_error:
                    why += f", last describe error: {last_error}"
                steps.append(f"Peering not active after {budget}s ({why})")
                # The peering is deliberately left in place. Step 1 adopts it on the next
                # attempt and finishes the configuration, so a slow activation costs a
                # retry rather than needing the peering deleted by hand.
                return {"status": "failed", "method": "peering", "peering_id": peering_id,
                        "steps": steps,
                        "error": (f"Peering {peering_id} did not become active within {budget}s "
                                  f"({why}). It has been left in place - run Configure again and "
                                  f"it will be adopted and finished.")}

        # Step 4: DNS resolution on both sides
        try:
            self.ec2.modify_vpc_peering_connection_options(
                VpcPeeringConnectionId=peering_id,
                RequesterPeeringConnectionOptions={"AllowDnsResolutionFromRemoteVpc": True},
            )
            steps.append("DNS resolution enabled (requester side)")
        except Exception as e:
            steps.append(f"DNS requester side skipped: {e}")

        try:
            if is_cross_account and cross_account_session:
                peer_ec2 = cross_account_session.client("ec2", region_name=db_region)
                peer_ec2.modify_vpc_peering_connection_options(
                    VpcPeeringConnectionId=peering_id,
                    AccepterPeeringConnectionOptions={"AllowDnsResolutionFromRemoteVpc": True},
                )
                steps.append("DNS resolution enabled (accepter side via cross-account)")
            elif is_cross_region:
                regional_ec2 = boto3.client("ec2", region_name=db_region)
                regional_ec2.modify_vpc_peering_connection_options(
                    VpcPeeringConnectionId=peering_id,
                    AccepterPeeringConnectionOptions={"AllowDnsResolutionFromRemoteVpc": True},
                )
                steps.append("DNS resolution enabled (accepter side via cross-region)")
            else:
                self.ec2.modify_vpc_peering_connection_options(
                    VpcPeeringConnectionId=peering_id,
                    AccepterPeeringConnectionOptions={"AllowDnsResolutionFromRemoteVpc": True},
                )
                steps.append("DNS resolution enabled (accepter side)")
        except Exception as e:
            steps.append(f"DNS accepter side failed: {e}")

        # Step 4: Agent-side route. Without it nothing else here carries traffic, so its
        # failure is the result, not a line in the log for the connectivity check to
        # rediscover as a timeout. The remaining steps still run: they are idempotent,
        # and the registry entry keeps teardown reference-counted.
        agent_route_error = ""
        route_result = {}
        if self.agent_route_table_id and db_vpc_cidr:
            try:
                route_result = self._ensure_agent_routes(
                    db_vpc_id, db_vpc_cidr, peering_id, db_endpoint, steps,
                    hosts=db_member_endpoints, owned=owned_host_routes,
                    instance_id=instance_id) or {}
            except Exception as e:
                agent_route_error = f"Agent route failed: {e}"
                steps.append(agent_route_error)

        # Step 5: DB-side routes. A failure here is FATAL for the path, the same way an
        # agent-route failure is: the DB's return traffic follows the DB-side route, so a
        # route that could not be asserted (most often a RouteConflict: the agent CIDR
        # already points at a different, stale peering left by an incomplete teardown)
        # means replies go the wrong way and the later connectivity check fails with a
        # misleading "timeout expired" that hid the real cause. Record the real reason as
        # the result error so onboarding stops here with it. The remaining steps still run
        # (idempotent) and the registry entry keeps teardown reference-counted.
        db_route_error = ""
        route_conflict = None   # structured detail for the console's Claim-route action
        try:
            if is_cross_account and cross_account_session:
                peer_ec2 = cross_account_session.client("ec2", region_name=db_region)
            else:
                peer_ec2 = boto3.client("ec2", region_name=db_region)
            db_rts = self._get_route_tables_for_vpc(db_vpc_id, ec2_client=peer_ec2)
            for rt_id in db_rts:
                try:
                    self._ensure_route(rt_id, self.agent_vpc_cidr, peering_id, ec2_client=peer_ec2)
                    steps.append(f"DB route: {rt_id} -> {self.agent_vpc_cidr}")
                except RouteConflict as e:
                    # Decide whether the holder of the conflicting route is a dead leftover
                    # (safe to repoint) or someone's live path (never touched automatically).
                    # The peering is described from the DB account, where it is visible.
                    verdict = self._classify_conflict_holder(e.holder, db_vpc_id=db_vpc_id,
                                                              ec2_client=peer_ec2)
                    if verdict["kind"] in (self.CONFLICT_DEAD, self.CONFLICT_OURS):
                        self._claim_conflicting_route(rt_id, e.cidr, peering_id, ec2_client=peer_ec2)
                        steps.append(f"DB route: {rt_id} -> {self.agent_vpc_cidr} "
                                     f"(reclaimed from {e.holder}: {verdict['detail']})")
                        continue
                    # Live or unknown: stop here with the real reason (handled below).
                    e.verdict = verdict
                    raise
        except RouteConflict as e:
            verdict = getattr(e, "verdict", None) or {"kind": self.CONFLICT_UNKNOWN,
                                                      "detail": "it could not be classified"}
            far = (f" Its other end is agent VPC {verdict['far_vpc']} in account {verdict['far_account']}, "
                   f"which is still active." if verdict.get("far_vpc") else "")
            db_route_error = (
                f"Route conflict in the database VPC: {e.route_table_id} already routes the agent "
                f"CIDR {e.cidr} to {e.holder}, not to this onboarding's peering {peering_id}, so "
                f"replies from the database would follow it and never reach the agent. "
                f"That route was not reclaimed automatically because {verdict['detail']}.{far} "
                f"Claiming it would disconnect whatever uses {e.holder} from this database. "
                f"If you are sure it is unused, a space admin can claim the route from the "
                f"console; otherwise remove or repoint it in the database account, then run "
                f"Configure again.")
            route_conflict = {
                "route_table_id": e.route_table_id, "cidr": e.cidr, "holder": e.holder,
                "peering_id": peering_id, "kind": verdict["kind"], "detail": verdict["detail"],
                "far_vpc": verdict.get("far_vpc", ""), "far_account": verdict.get("far_account", ""),
                # The console offers Claim only for the agent's own CIDR (the scope guard
                # enforces the same on the server), never for any other route.
                "claimable": e.cidr == self.agent_vpc_cidr,
            }
            steps.append(f"DB routes failed: {db_route_error}")
        except Exception as e:
            db_route_error = f"DB routes failed: {e}"
            steps.append(db_route_error)

        # Step 6: Security group rules
        try:
            if is_cross_account and cross_account_session:
                sg_ec2 = cross_account_session.client("ec2", region_name=db_region)
            else:
                sg_ec2 = boto3.client("ec2", region_name=db_region)
            for sg_id in db_security_group_ids.split(","):
                sg_id = sg_id.strip()
                if not sg_id:
                    continue
                self._ensure_sg_ingress(sg_id, db_port, self.agent_vpc_cidr, ec2_client=sg_ec2)
                steps.append(f"SG: {sg_id} TCP/{db_port} from {self.agent_vpc_cidr}")
        except Exception as e:
            steps.append(f"SG rules failed: {e}")

        # Register in network registry
        try:
            from tools.aws.network_registry import network_registry, NetworkRegistry
            conn_id = NetworkRegistry.make_connection_id(self.agent_vpc_id, db_vpc_id)
            existing_reg = network_registry.get_connection(conn_id)
            if existing_reg:
                network_registry.update_connection(conn_id, {"status": "active", "peering_id": peering_id})
                # Count the database that triggered this (re)configuration so the
                # connection isn't torn down while this database still depends on it.
                network_registry.add_reference(conn_id, instance_id)
            else:
                network_registry.create_connection({
                    "connection_id": conn_id, "method": "peering",
                    "db_vpc_id": db_vpc_id, "db_vpc_cidr": db_vpc_cidr,
                    "db_region": db_region, "db_account_id": db_account_id,
                    "agent_vpc_id": self.agent_vpc_id, "agent_vpc_cidr": self.agent_vpc_cidr,
                    "peering_id": peering_id,
                    "status": "active", "instance_id": instance_id,
                })
            steps.append("Registered peering in network registry")
        except Exception as e:
            steps.append(f"Registry warning: {e}")

        if agent_route_error:
            return {"status": "failed", "method": "peering", "peering_id": peering_id,
                    "steps": steps, "error": agent_route_error}
        if db_route_error:
            return {"status": "failed", "method": "peering", "peering_id": peering_id,
                    "steps": steps, "error": db_route_error,
                    **({"route_conflict": route_conflict} if route_conflict else {})}
        return {"status": "configured", "method": "peering", "peering_id": peering_id, "steps": steps,
                **route_result}

    def _ensure_agent_routes(self, db_vpc_id, db_vpc_cidr, peering_id, db_endpoint, steps,
                             hosts=(), owned=(), instance_id=""):
        """Route this database's VPC through its peering in the agent route table.

        The VPC-wide route is the normal case (``{"route_mode": "vpc"}``). It is refused
        when the CIDR is already routed to a different live peering: two databases whose
        VPCs share an address range (most templates default to 10.0.0.0/16). Overwriting
        that route would cut off the other database, and leaving it sends this
        database's packets into the wrong VPC. So check whether the existing route
        already reaches this VPC and reuse it if so (``{"route_mode": "reused"}``);
        otherwise add a `/32` host route for every address the database answers on (its
        endpoint, reader endpoint and every member's instance endpoint, `hosts`), which
        longest-prefix match prefers, and leave the VPC-wide route to the other VPC as it
        is. That returns ``{"route_mode": "host", "host_routes": [...], "db_vpc_cidr"}``:
        the record owns those `/32`s; when a member's address moves Tier 1 flags the
        record and the API task repairs them (``tools.aws.route_repair``).

        The safety rules live in ``host_routes.plan``, shared with the repair: an address
        outside the database's VPC CIDR is refused (it would black-hole through the
        peering), an address another registered database in the route holder's VPC uses
        is refused (the `/32` would hijack its traffic), a `/32` pointing at another live
        target is left alone, and not KNOWING whether an address clashes adds nothing
        (fail closed). If nothing can be routed this raises, so Configure fails visibly;
        if only some addresses are refused the rest are routed and each refusal is a ⚠
        step. Only an exact-CIDR collision is recognised; a partially overlapping prefix
        still lands in the plain refusal.

        `owned` are the `/32`s the record already owns (a Reconfigure). They are
        reconciled, never forgotten: on the host path they are planned with the new
        addresses (a departed member's or a now-taken address's route is removed), and
        on a switch to ``vpc``/``reused`` they are deleted, since the VPC-wide route
        reaches the database. Whatever cannot be deleted stays in the returned
        ``host_routes`` so a later teardown still removes it.
        """
        from tools.aws import host_routes
        rt = self.agent_route_table_id
        try:
            self._ensure_route(rt, db_vpc_cidr, peering_id)
            steps.append(f"Agent route: {db_vpc_cidr} -> {peering_id}")
            return {"route_mode": "vpc", **self._release_host_routes(peering_id, instance_id, owned, steps)}
        except RouteConflict as e:
            conflict = e   # the `as` name is unbound when the handler ends
        holder = conflict.holder
        if self._peering_reaches(holder, db_vpc_id):
            steps.append(f"Agent route: {db_vpc_cidr} already reaches {db_vpc_id} via {holder}; reusing it")
            return {"route_mode": "reused", **self._release_host_routes(peering_id, instance_id, owned, steps)}
        names = []
        for h in [db_endpoint, *(hosts or ())]:
            if h and h not in names:
                names.append(h)
        desired, unresolved = host_routes.desired_addresses(names, self._resolve_hosts)
        if not desired:
            raise RuntimeError(f"{conflict} (the database endpoint {db_endpoint or '<unknown>'} "
                               f"could not be resolved to route it individually)")
        for h in unresolved:
            steps.append(f"⚠ {h} could not be resolved; it is not routed until it does "
                         f"(Tier 1 checks it on each run, and the route repair adds it once it does)")
        # Fail CLOSED from here: a host route is only safe once it is KNOWN not to take
        # over another database's address. Not knowing (the holder's VPC cannot be
        # described, the registry cannot be read, a registered endpoint in that VPC
        # cannot be resolved) is a refusal, not a pass.
        taken, gate_reason = None, ""
        other_vpc = self._peer_vpc_of(holder)
        if not other_vpc:
            gate_reason = (f"could not determine which VPC {holder} leads to, so a host route for "
                           f"{db_endpoint} cannot be shown safe; refusing it. Check that {holder} "
                           f"is active and describable, then re-run Configure")
        else:
            try:
                taken = self._endpoint_addresses_in_vpc(other_vpc, exclude_vpc_id=db_vpc_id)
            except RuntimeError as e:
                gate_reason = (f"{e}; a host route for {db_endpoint} cannot be shown safe without "
                               f"it, so it is refused. Re-run Configure once the registry is reachable")
        if taken:
            clash = {ip: taken[ip] for ip in desired if ip in taken}
            if clash:
                # The other database belongs to another tenant: its name and address go to
                # the server log for the operator of this deployment, never into the
                # message, which is stored in this database's config_log.
                logger.warning(
                    "host route refused for %s: %s shares the private address with another "
                    "registered database in %s (%s)", db_endpoint, db_vpc_cidr, other_vpc,
                    ", ".join(f"{ip} = {name}" for ip, name in clash.items()))
        try:
            table = self.ec2.describe_route_tables(RouteTableIds=[rt])["RouteTables"][0].get("Routes", [])
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"{conflict} (the agent route table could not be read: {type(e).__name__})")
        # The record's own /32s are planned too, so a Reconfigure removes the stale ones
        # and keeps tracking the rest. Who else on this peering owns a /32 comes from the
        # registry; not knowing (None) keeps every owned route rather than deleting one.
        other_owned = self._other_owned_host_routes(peering_id, instance_id) if owned else set()
        plan = host_routes.plan(owned, desired, db_vpc_cidr, peering_id, table, taken,
                                other_owned, gate_reason, unresolved=unresolved)
        routable = any(ip in desired for ip, _host in plan.add + plan.replace + plan.keep)
        if routable:
            steps.append(f"{db_vpc_cidr} is already routed to {holder}, another VPC with the same CIDR; "
                         f"routing only this database's addresses through {peering_id}")
        # Applied even when nothing desired is routable: the plan then only removes the
        # record's own stale or hijacking /32s, which must not outlive a failed Configure.
        result = host_routes.apply(self.ec2, rt, plan, peering_id)
        steps.extend(result["steps"])
        if not routable:
            reasons = []
            for _ip, reason in plan.refused:
                if reason not in reasons:
                    reasons.append(reason)
            raise RuntimeError(f"{conflict} ({'; '.join(reasons)})")
        if not any(o["ip"] in desired for o in result["owned"]):
            raise RuntimeError(f"{conflict} (no host route could be created: "
                               f"{'; '.join(result['steps'])})")
        return {"route_mode": "host", "host_routes": result["owned"], "db_vpc_cidr": db_vpc_cidr}

    def _other_owned_host_routes(self, peering_id, instance_id):
        """IPs the other records on `peering_id` own, or None when the registry cannot be
        read (the caller then deletes nothing it is unsure about)."""
        try:
            from tools.aws.dynamodb_client import db_registry
            records = db_registry.list_instances()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not read the registry for host-route ownership: {e}")
            return None
        return {o.get("ip") for r in records
                if r.get("network_peering_id") == peering_id and r.get("instance_id") != instance_id
                for o in r.get("host_routes") or [] if isinstance(o, dict)}

    def _release_host_routes(self, peering_id, instance_id, owned, steps) -> dict:
        """On a Reconfigure that no longer needs host routes, delete the record's own
        `/32`s. ``{}`` when it owned none, else ``{"host_routes": [...still owned]}``."""
        if not owned:
            return {}
        return {"host_routes": self._delete_owned_host_routes(peering_id, instance_id, owned, steps)}

    def _delete_owned_host_routes(self, peering_id, instance_id, host_routes, steps) -> list:
        """Delete this database's own `/32`s over a peering: those that still point at
        the peering and that no other record on it owns. An unreadable registry or
        route table deletes nothing (the routes stay harmless on the shared peering
        until the last reference goes). Returns the `/32`s the record still owns."""
        from tools.aws import host_routes as hr
        if not self.agent_route_table_id or not peering_id:
            return list(host_routes or [])
        other_owned = self._other_owned_host_routes(peering_id, instance_id)
        if other_owned is None:
            steps.append("Could not read the registry, leaving this database's host routes in place")
            return list(host_routes or [])
        try:
            table = self.ec2.describe_route_tables(
                RouteTableIds=[self.agent_route_table_id])["RouteTables"][0].get("Routes", [])
        except Exception as e:  # noqa: BLE001
            steps.append(f"Could not read agent routes, leaving them in place: {e}")
            return list(host_routes or [])
        plan = hr.plan(host_routes, {}, "", peering_id, table, {}, other_owned)
        result = hr.apply(self.ec2, self.agent_route_table_id, plan, peering_id)
        steps.extend(result["steps"])
        return result["owned"]

    def _peering_reaches(self, peering_id, vpc_id) -> bool:
        """True if `peering_id` connects to `vpc_id` on either side. False when it cannot
        be described, which the caller treats as 'leads somewhere else'."""
        if not peering_id:
            return False
        try:
            conns = self.ec2.describe_vpc_peering_connections(
                VpcPeeringConnectionIds=[peering_id])["VpcPeeringConnections"]
        except Exception as e:
            logger.warning(f"Could not describe {peering_id}: {e}")
            return False
        return any(vpc_id in (c.get("RequesterVpcInfo", {}).get("VpcId"),
                              c.get("AccepterVpcInfo", {}).get("VpcId")) for c in conns)

    @staticmethod
    def _ip_in_cidr(ip, cidr) -> bool:
        from tools.aws import host_routes
        return host_routes.ip_in_cidr(ip, cidr)

    def _peer_vpc_of(self, peering_id) -> str:
        """The VPC on the far side of `peering_id` from the agent VPC, or ''."""
        from tools.aws import host_routes
        return host_routes.peer_vpc_of(self.ec2, peering_id, self.agent_vpc_id)

    def _endpoint_addresses_in_vpc(self, vpc_id, exclude_vpc_id="") -> dict:
        """{private_ip: display_name} for every registered database in `vpc_id` (every
        name its members answer on). This is the GATE for a host route, so it fails
        closed: an unreadable registry, or a registered name in that VPC that does not
        resolve, raises RuntimeError rather than contributing nothing."""
        from tools.aws import host_routes
        if not vpc_id or vpc_id == exclude_vpc_id:
            return {}
        try:
            from tools.aws.dynamodb_client import db_registry
            records = db_registry.list_instances()
        except Exception as e:
            logger.warning(f"Could not read the database registry for the address check: {e}")
            raise RuntimeError(f"the database registry could not be read ({type(e).__name__})")
        return host_routes.registered_addresses(records, vpc_id, exclude_vpc_id, self._resolve_hosts)

    @staticmethod
    def _resolve_hosts(endpoint) -> list:
        """IPv4 addresses `endpoint` resolves to, sorted; [] if none or unresolvable."""
        from tools.aws import host_routes
        return host_routes.resolve(endpoint)

    # VPC Lattice path (same-region fallback for CIDR conflicts)
    # ──────────────────────────────────────────────

    def _configure_lattice(self, db_vpc_id, db_subnet_ids, db_security_group_ids,
                           db_port, db_account_id, db_region, db_endpoint, db_arn,
                           cross_account_session, steps, instance_id="") -> dict:
        """Reach a database whose VPC CIDR is already routed to another VPC, without routing.

        PrivateLink VPC resources (built on VPC Lattice): in the database's account a
        RESOURCE GATEWAY sits in the database VPC, a RESOURCE CONFIGURATION names the
        database by its RDS ARN and is shared to this account with RAM; in the agent VPC a
        RESOURCE VPC ENDPOINT connects to it. With private DNS the database's own RDS
        hostname resolves, inside the agent VPC, to the endpoint, so the agent connects by
        the real name and certificate verification is the ordinary verify-full. No route
        in either VPC, no load balancer, and the two VPCs' address ranges never meet.

        One resource gateway per database VPC (reused by every database in it), one
        resource configuration, one share and one endpoint per database. Everything is
        tagged ManagedBy so cleanup and the customer-installed policy can tell ours apart.
        Idempotent: every step looks for what it would create before creating it."""
        from tools.aws.network_registry import network_registry, NetworkRegistry
        if not db_arn:
            return {"status": "failed", "method": "lattice", "steps": steps,
                    "error": "The database record carries no RDS ARN; a resource configuration needs one"}
        if not db_subnet_ids:
            return {"status": "failed", "method": "lattice", "steps": steps,
                    "error": "The database record carries no subnet ids; a resource gateway needs them"}
        agent_account = boto3.client("sts").get_caller_identity()["Account"]
        is_cross_account = bool(db_account_id) and db_account_id != agent_account
        if is_cross_account and not cross_account_session:
            return {"status": "failed", "method": "lattice", "steps": steps,
                    "error": "No cross-account session for the database account"}
        db_session = cross_account_session or boto3
        db_ec2 = db_session.client("ec2", region_name=db_region)
        db_lattice = db_session.client("vpc-lattice", region_name=db_region)
        tag = managed_by_tag()
        subnet_ids = [x.strip() for x in (db_subnet_ids or "").split(",") if x.strip()]
        db_sg_ids = [x.strip() for x in (db_security_group_ids or "").split(",") if x.strip()]

        # 0. Finish any earlier teardown in this VPC that stopped half way, so nothing is
        # created over a row whose leftovers are still out there.
        self._retry_failed_lattice_rows(db_vpc_id, db_session, db_region, steps)
        conn_id = NetworkRegistry.make_connection_id(self.agent_vpc_id, db_vpc_id, db_endpoint, "lattice")
        prior = network_registry.get_connection(conn_id)
        if prior and prior.get("status") == "delete_failed":
            return {"status": "failed", "method": "lattice", "steps": steps,
                    "error": (f"An earlier teardown of this database's Lattice path is incomplete "
                              f"({prior.get('failed_step', '')}: {prior.get('failure_detail', '')}); "
                              f"resolve it in the database account and retry")}

        # 1. Resource gateway in the database VPC, with its own security group.
        gw_sg_id = self._ensure_lattice_gateway_sg(db_ec2, db_vpc_id, tag, steps)
        gateway = self._ensure_resource_gateway(db_lattice, db_vpc_id, subnet_ids, gw_sg_id, tag, steps)
        if not gateway or gateway.get("error"):
            return {"status": "failed", "method": "lattice", "steps": steps,
                    "error": (gateway or {}).get("error") or "Resource gateway could not be created"}
        gateway_id, gateway_arn = gateway["id"], gateway["arn"]
        # The database must admit the gateway: its security group gets one rule, source =
        # the gateway's security group, so no address range of ours appears in it.
        for sg in db_sg_ids:
            self._ensure_sg_ingress_from_sg(db_ec2, sg, db_port, gw_sg_id, tag)
        if db_sg_ids:
            steps.append(f"Database security group(s) admit the resource gateway on {db_port}")

        # 2. Resource configuration naming the database by ARN.
        rcfg = self._ensure_resource_configuration(db_lattice, db_arn, gateway_id, instance_id, tag, steps)
        if not rcfg:
            return {"status": "failed", "method": "lattice", "steps": steps,
                    "error": "Resource configuration could not be created"}
        rcfg_arn = rcfg["arn"]

        # 3. Share it to this account (cross-account only) and accept.
        share_arn = ""
        if is_cross_account:
            share_arn = self._ensure_resource_share(db_session, db_region, rcfg_arn, agent_account, instance_id, tag, steps)
            if not share_arn:
                return {"status": "failed", "method": "lattice", "steps": steps,
                        "error": "The resource configuration could not be shared to this account"}
            # The endpoint can only bind to a share this account can use: wait until the
            # provider side reports this account ASSOCIATED, accepting any invitation.
            share_error = self._confirm_resource_share(db_session, db_region, share_arn, agent_account, steps)
            if share_error:
                return {"status": "failed", "method": "lattice", "steps": steps, "error": share_error}

        # 4. Resource endpoint in the agent VPC, private DNS on.
        endpoint = self._ensure_resource_endpoint(rcfg_arn, db_port, instance_id, tag, steps)
        if not endpoint:
            return {"status": "failed", "method": "lattice", "steps": steps,
                    "error": "Resource VPC endpoint could not be created"}
        if endpoint["state"] not in ("available", "pending"):
            return {"status": "failed", "method": "lattice", "steps": steps,
                    "error": f"Resource VPC endpoint is {endpoint['state']}"}

        # 5. Registry: the gateway is shared by every database in this VPC (reference
        # counted); the configuration, share and endpoint are this database's own.
        gw_conn_id = NetworkRegistry.make_connection_id(self.agent_vpc_id, db_vpc_id, method="lattice-gateway")
        gw_row = network_registry.get_connection(gw_conn_id)
        if gw_row:
            if gw_row.get("status") != "active":
                # A gateway row left by an incomplete or pending teardown: the gateway found
                # (or rebuilt) above is ACTIVE, so adopt it instead of deleting it.
                network_registry.update_connection(gw_conn_id, {
                    "status": "active", "failed_step": "", "failure_detail": "",
                    "resource_gateway_id": gateway_id, "resource_gateway_arn": gateway_arn,
                    "gateway_security_group_id": gw_sg_id,
                })
                steps.append(f"Re-adopted resource gateway {gateway_id} left by an earlier teardown")
            network_registry.add_reference(gw_conn_id, instance_id)
        else:
            network_registry.create_connection({
                "connection_id": gw_conn_id, "method": "lattice-gateway", "db_vpc_id": db_vpc_id,
                "db_region": db_region, "db_account_id": db_account_id, "agent_vpc_id": self.agent_vpc_id,
                "resource_gateway_id": gateway_id, "resource_gateway_arn": gateway_arn,
                "gateway_security_group_id": gw_sg_id, "instance_id": instance_id,
            })
        if network_registry.get_connection(conn_id):
            network_registry.add_reference(conn_id, instance_id)
        else:
            network_registry.create_connection({
                "connection_id": conn_id, "method": "lattice", "db_vpc_id": db_vpc_id,
                "db_region": db_region, "db_account_id": db_account_id, "agent_vpc_id": self.agent_vpc_id,
                "resource_configuration_arn": rcfg_arn, "resource_share_arn": share_arn,
                "vpce_id": endpoint["id"], "instance_id": instance_id,
            })
        steps.append("Connected over a Lattice resource endpoint; the database's own hostname resolves "
                     "to it inside the agent VPC")
        return {
            "status": "configured", "method": "lattice", "vpce_id": endpoint["id"],
            "resource_configuration_arn": rcfg_arn, "resource_gateway_id": gateway_id,
            "resource_share_arn": share_arn, "steps": steps,
        }

    @staticmethod
    def _lattice_name(kind: str, key: str) -> str:
        """A Lattice resource name: lowercase, hyphens, no run of hyphens, <= 40 chars."""
        import hashlib
        digest = hashlib.sha256(key.encode()).hexdigest()[:12]
        base = re.sub(r"[^a-z0-9]+", "-", f"{resource_prefix()}-{kind}-{digest}".lower()).strip("-")
        return re.sub(r"-{2,}", "-", base)[:40].rstrip("-")

    def _ensure_lattice_gateway_sg(self, db_ec2, db_vpc_id, tag, steps) -> str:
        """The security group a database VPC's resource gateway runs with. Egress only
        (the default); the database's own group admits it as a source."""
        name = self._lattice_name("rgw", db_vpc_id)
        resp = db_ec2.describe_security_groups(Filters=[
            {"Name": "group-name", "Values": [name]}, {"Name": "vpc-id", "Values": [db_vpc_id]}])
        if resp.get("SecurityGroups"):
            return resp["SecurityGroups"][0]["GroupId"]
        sg = db_ec2.create_security_group(
            GroupName=name, Description="DBOps Agent: resource gateway", VpcId=db_vpc_id,
            TagSpecifications=[{"ResourceType": "security-group",
                                "Tags": [{"Key": "Name", "Value": name}, {"Key": "ManagedBy", "Value": tag}]}])
        steps.append(f"Created resource gateway security group {sg['GroupId']} in {db_vpc_id}")
        return sg["GroupId"]

    def _ensure_resource_gateway(self, db_lattice, db_vpc_id, subnet_ids, sg_id, tag, steps) -> dict:
        """One resource gateway per database VPC, found by name, created if absent, and
        ACTIVE before anything is attached to it. A gateway that failed or is being
        deleted is returned as ``{"error": ...}``. That includes a same-named gateway that
        is being deleted: Lattice names are unique in an account, so creating another one
        beside it would only fail with AWS's conflict text."""
        name = self._lattice_name("rgw", db_vpc_id)
        found = None
        for item in _paged(db_lattice.list_resource_gateways, "items"):
            if item.get("name") != name:
                continue
            if item.get("status") in ("DELETE_IN_PROGRESS", "DELETE_FAILED"):
                return {"error": f"Resource gateway {item['id']} is {item['status']}; "
                                 "delete it in the database account and retry"}
            found = {"id": item["id"], "arn": item["arn"]}
            steps.append(f"Reusing resource gateway {item['id']} in {db_vpc_id}")
            break
        if not found:
            resp = db_lattice.create_resource_gateway(
                name=name, vpcIdentifier=db_vpc_id, subnetIds=subnet_ids,
                securityGroupIds=[sg_id] if sg_id else [], ipAddressType="IPV4",
                tags={"ManagedBy": tag, "Name": name})
            steps.append(f"Created resource gateway {resp['id']} in {db_vpc_id}")
            found = {"id": resp["id"], "arn": resp["arn"]}
        error = self._wait_gateway_active(db_lattice, found["id"], steps)
        if error:
            return {"error": error}
        return found

    def _wait_gateway_active(self, db_lattice, gateway_id, steps) -> str:
        """Wait (bounded) until the resource gateway is ACTIVE. Returns an error or ""."""
        terminal = ("CREATE_FAILED", "UPDATE_FAILED", "DELETE_IN_PROGRESS", "DELETE_FAILED")

        def probe():
            try:
                status = db_lattice.get_resource_gateway(resourceGatewayIdentifier=gateway_id).get("status", "")
            except Exception as e:
                return False, None, f"unreadable ({_short_error(e)})"
            if status == "ACTIVE":
                return True, None, status
            if status in terminal:
                return False, f"Resource gateway {gateway_id} is {status}; delete it in the database account and retry", status
            return False, None, status

        ok, last, error = _wait_until(probe, GATEWAY_ACTIVE_BUDGET_S, POLL_INTERVAL_S)
        if ok:
            steps.append(f"Resource gateway {gateway_id} is ACTIVE")
            return ""
        return error or (f"Resource gateway {gateway_id} did not become ACTIVE within "
                         f"{GATEWAY_ACTIVE_BUDGET_S} s (last status {last or 'unknown'})")

    def _ensure_resource_configuration(self, db_lattice, db_arn, gateway_id, instance_id, tag, steps) -> dict:
        """One ARN resource configuration per database (the RDS instance or cluster ARN)."""
        name = self._lattice_name("rcfg", db_arn)
        for item in _paged(db_lattice.list_resource_configurations, "items", resourceGatewayIdentifier=gateway_id):
            if item.get("name") == name and item.get("status") not in ("DELETE_IN_PROGRESS", "DELETE_FAILED"):
                steps.append(f"Reusing resource configuration {item['id']}")
                return {"id": item["id"], "arn": item["arn"]}
        resp = db_lattice.create_resource_configuration(
            name=name, type="ARN", resourceGatewayIdentifier=gateway_id,
            resourceConfigurationDefinition={"arnResource": {"arn": db_arn}},
            allowAssociationToShareableServiceNetwork=False,
            tags={"ManagedBy": tag, "Name": name, "InstanceId": instance_id or ""})
        steps.append(f"Created resource configuration {resp['id']} for {db_arn.rsplit(':', 1)[-1]}")
        return {"id": resp["id"], "arn": resp["arn"]}

    def _ensure_resource_share(self, db_session, db_region, rcfg_arn, agent_account, instance_id, tag, steps) -> str:
        """Share the resource configuration with the deployment account through RAM. The
        share is for one principal (this account) and one resource."""
        ram = db_session.client("ram", region_name=db_region)
        name = self._lattice_name("share", rcfg_arn)
        shares = _paged(ram.get_resource_shares, "resourceShares", resourceOwner="SELF", name=name)
        live = [sh for sh in shares if sh.get("status") in ("ACTIVE", "PENDING")]
        if live:
            steps.append(f"Reusing resource share {live[0]['resourceShareArn'].rsplit('/', 1)[-1]}")
            return live[0]["resourceShareArn"]
        resp = ram.create_resource_share(
            name=name, resourceArns=[rcfg_arn], principals=[agent_account],
            allowExternalPrincipals=True,
            tags=[{"key": "ManagedBy", "value": tag}, {"key": "InstanceId", "value": instance_id or ""}])
        arn = resp["resourceShare"]["resourceShareArn"]
        steps.append(f"Shared the resource configuration with account {agent_account}")
        return arn

    def _confirm_resource_share(self, db_session, db_region, share_arn, agent_account, steps) -> str:
        """Wait (bounded) until the share is usable by this account. Returns an error or "".

        Each poll accepts any PENDING invitation this account holds for the share (outside
        one organisation an invitation appears, possibly late; inside one organisation
        with sharing enabled there is none), then asks the PROVIDER side whether this
        account's principal association is ASSOCIATED. Only then can a resource endpoint
        bind to the shared configuration."""
        agent_ram = boto3.client("ram", region_name=db_region)
        db_ram = db_session.client("ram", region_name=db_region)
        accepted = []

        def probe():
            try:
                for inv in _paged(agent_ram.get_resource_share_invitations, "resourceShareInvitations",
                                  resourceShareArns=[share_arn]):
                    if inv.get("status") == "PENDING":
                        agent_ram.accept_resource_share_invitation(
                            resourceShareInvitationArn=inv["resourceShareInvitationArn"])
                        accepted.append(inv["resourceShareInvitationArn"])
                state = "NOT_LISTED"
                for assoc in _paged(db_ram.get_resource_share_associations, "resourceShareAssociations",
                                    associationType="PRINCIPAL", resourceShareArns=[share_arn]):
                    if assoc.get("associatedEntity") == agent_account:
                        state = assoc.get("status", "") or state
            except Exception as e:
                return False, None, f"unreadable ({_short_error(e)})"
            if state == "ASSOCIATED":
                return True, None, state
            if state in ("FAILED", "DISASSOCIATED"):
                return False, (f"The resource share's association with account {agent_account} is {state}; "
                               f"delete the share in the database account and retry"), state
            return False, None, state

        ok, last, error = _wait_until(probe, SHARE_CONFIRM_BUDGET_S, POLL_INTERVAL_S)
        if ok:
            if accepted:
                steps.append(f"Accepted the resource share invitation; the share is associated with account {agent_account}")
            else:
                steps.append(f"No invitation needed (same organisation); the share is associated with account {agent_account}")
            return ""
        return error or (f"The resource share was not accepted by account {agent_account} within "
                         f"{SHARE_CONFIRM_BUDGET_S} s (last state {last or 'unknown'})")

    def _agent_subnet_ids(self) -> list:
        subnets = [s.strip() for s in os.getenv("SUBNET_IDS", "").split(",") if s.strip()]
        if subnets:
            return subnets
        resp = self.ec2.describe_subnets(Filters=[
            {"Name": "vpc-id", "Values": [self.agent_vpc_id]},
            {"Name": "tag:ManagedBy", "Values": [managed_by_tag()]}])
        return [s["SubnetId"] for s in resp.get("Subnets", [])]

    def _ensure_resource_endpoint(self, rcfg_arn, db_port, instance_id, tag, steps) -> dict:
        """One resource VPC endpoint in the agent VPC per database, private DNS enabled so
        the RDS hostname resolves to it."""
        existing = _paged(self.ec2.describe_vpc_endpoints, "VpcEndpoints", "NextToken", Filters=[
            {"Name": "vpc-id", "Values": [self.agent_vpc_id]},
            {"Name": "vpc-endpoint-type", "Values": ["Resource"]},
            {"Name": "vpc-endpoint-state", "Values": ["available", "pending"]},
        ])
        for ep in existing:
            if ep.get("ResourceConfigurationArn") == rcfg_arn:
                steps.append(f"Reusing resource endpoint {ep['VpcEndpointId']}")
                return {"id": ep["VpcEndpointId"], "state": ep.get("State", "")}
        subnets = self._agent_subnet_ids()
        if not subnets:
            steps.append("No agent subnets found for the resource endpoint")
            return {}
        sg_id = self._get_or_create_vpce_sg(db_port)
        resp = self.ec2.create_vpc_endpoint(
            VpcEndpointType="Resource", VpcId=self.agent_vpc_id, ResourceConfigurationArn=rcfg_arn,
            SubnetIds=subnets, SecurityGroupIds=[sg_id] if sg_id else [], PrivateDnsEnabled=True,
            TagSpecifications=[{"ResourceType": "vpc-endpoint",
                                "Tags": [{"Key": "ManagedBy", "Value": tag},
                                         {"Key": "InstanceId", "Value": instance_id or ""}]}])
        ep = resp["VpcEndpoint"]
        steps.append(f"Created resource endpoint {ep['VpcEndpointId']} in the agent VPC (private DNS on)")
        return {"id": ep["VpcEndpointId"], "state": ep.get("State", "pending")}

    def _get_or_create_vpce_sg(self, port):
        """The security group the agent VPC's resource endpoints run with: admits the agent
        VPC on the database port. Shared by every resource endpoint, so the rule for the
        requested port is asserted on every call (found or created), not only on creation:
        a second database on another port needs its own rule."""
        sg_name = security_group_name("vpce-sg")
        resp = self.ec2.describe_security_groups(Filters=[
            {"Name": "group-name", "Values": [sg_name]}, {"Name": "vpc-id", "Values": [self.agent_vpc_id]}])
        if resp.get("SecurityGroups"):
            sg_id = resp["SecurityGroups"][0]["GroupId"]
        else:
            resp = self.ec2.create_security_group(
                GroupName=sg_name, Description="DBOps Agent resource endpoints", VpcId=self.agent_vpc_id,
                TagSpecifications=[{"ResourceType": "security-group",
                                    "Tags": [{"Key": "Name", "Value": sg_name}, {"Key": "ManagedBy", "Value": managed_by_tag()}]}])
            sg_id = resp["GroupId"]
        try:
            self.ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=[{
                "IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                "IpRanges": [{"CidrIp": self.agent_vpc_cidr, "Description": "DB access via resource endpoint"}]}])
        except Exception as e:
            if "Duplicate" not in str(e) and "already exists" not in str(e):
                raise
        return sg_id

    def _ensure_sg_ingress_from_sg(self, ec2, sg_id, port, source_sg_id, tag):
        """Admit `source_sg_id` on `port` in `sg_id` (idempotent)."""
        try:
            ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=[{
                "IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                "UserIdGroupPairs": [{"GroupId": source_sg_id, "Description": tag}]}])
        except Exception as e:
            if "Duplicate" not in str(e) and "already exists" not in str(e):
                raise

    # ──────────────────────────────────────────────
    # Cleanup
    # ──────────────────────────────────────────────

    def cleanup_peering(self, db_vpc_id: str = "", peering_id: str = "",
                        db_region: str = "", cross_account_session=None,
                        instance_id: str = "", host_routes=()) -> dict:
        """Remove VPC peering using registry ref counting.

        `host_routes` are the `/32`s this database owns on the peering (the cross-Region
        CIDR-clash path). While other databases still use the peering, only those are
        deleted, and only the ones no other database on the same peering owns; the last
        reference deletes every route over the peering."""
        from tools.aws.network_registry import network_registry, NetworkRegistry
        steps = []
        db_region = db_region or os.getenv("AWS_REGION", "us-east-1")

        # Check registry ref count
        conn_id = NetworkRegistry.make_connection_id(self.agent_vpc_id, db_vpc_id)
        conn = network_registry.get_connection(conn_id)

        if conn and instance_id:
            updated = network_registry.remove_reference(conn_id, instance_id)
            if updated and updated.get("ref_count", 0) > 0:
                steps.append(f"Removed reference. {updated['ref_count']} other DB(s) still use this peering.")
                if host_routes:
                    self._delete_owned_host_routes(peering_id, instance_id, host_routes, steps)
                return {"steps": steps}
            steps.append("Last reference removed — cleaning up peering")

        if not peering_id:
            # Try to find it
            existing = self._find_existing_peering(db_vpc_id)
            if existing:
                peering_id = existing.get("peering_id", "")

        if not peering_id:
            steps.append("No peering to clean up")
            return {"steps": steps}

        # Delete every agent-side route over this peering: the VPC-wide one, and any
        # per-database host routes added because the VPC CIDR was taken. Deleting by
        # target rather than by CIDR also leaves a same-CIDR route that belongs to
        # another VPC's peering alone.
        if self.agent_route_table_id:
            try:
                resp = self.ec2.describe_route_tables(RouteTableIds=[self.agent_route_table_id])
                ours = [r["DestinationCidrBlock"] for r in resp["RouteTables"][0].get("Routes", [])
                        if r.get("VpcPeeringConnectionId") == peering_id and r.get("DestinationCidrBlock")]
            except Exception as e:
                steps.append(f"Could not read agent routes, leaving them in place: {e}")
                ours = []
            for cidr in ours:
                try:
                    self.ec2.delete_route(RouteTableId=self.agent_route_table_id, DestinationCidrBlock=cidr)
                    steps.append(f"Deleted agent route: {cidr}")
                except Exception as e:
                    if "InvalidRoute.NotFound" not in str(e):
                        steps.append(f"Agent route delete failed: {e}")

        # Delete DB-side routes for the agent CIDR. Prefer the CIDR recorded when the path
        # was built: if the agent CIDR changed since, deleting by the current one would miss
        # the old route and leave exactly the stale leftover that conflicts with the next
        # onboard. Records created before the CIDR was stored fall back to the current one.
        route_cidr = (conn or {}).get("agent_vpc_cidr") or self.agent_vpc_cidr
        try:
            if cross_account_session:
                peer_ec2 = cross_account_session.client("ec2", region_name=db_region)
            else:
                peer_ec2 = boto3.client("ec2", region_name=db_region)
            if db_vpc_id:
                db_rts = self._get_route_tables_for_vpc(db_vpc_id, ec2_client=peer_ec2)
                for rt_id in db_rts:
                    try:
                        peer_ec2.delete_route(
                            RouteTableId=rt_id,
                            DestinationCidrBlock=route_cidr,
                        )
                        steps.append(f"Deleted DB route: {rt_id} -> {route_cidr}")
                    except Exception:
                        pass
        except Exception as e:
            steps.append(f"DB route cleanup: {e}")

        # Delete the peering connection
        try:
            self.ec2.delete_vpc_peering_connection(VpcPeeringConnectionId=peering_id)
            steps.append(f"Deleted peering: {peering_id}")
        except Exception as e:
            steps.append(f"Peering delete failed: {e}")

        # Remove from registry
        if conn:
            network_registry.delete_connection(conn_id)
            steps.append("Removed from network registry")

        return {"steps": steps}

    def cleanup_lattice(self, vpce_id: str = "", db_vpc_id: str = "",
                        cross_account_session=None, db_region: str = "",
                        instance_id: str = "", db_endpoint: str = "",
                        resource_configuration_arn: str = "", resource_share_arn: str = "") -> dict:
        """Remove this database's resource endpoint, configuration and share; remove the
        VPC's resource gateway (then its security group) only when no other database in
        that VPC still uses it.

        Order matters and each delete is confirmed: the endpoint is deleted and WAITED for
        until gone before the configuration it binds to is deleted; the gateway is waited
        for until gone before its security group. NotFound is "done" at every step, so the
        call is idempotent. When a delete is refused or times out the registry row is NOT
        removed: it is kept as ``status=delete_failed`` naming the failing step and the
        resources still left, and a later ``cleanup_lattice`` (or Configure) for the same
        VPC finishes the job. A row is only deleted once everything it names is gone.
        Returns ``{"steps": [...], "complete": bool}``."""
        from tools.aws.network_registry import network_registry, NetworkRegistry
        steps = []
        db_region = db_region or os.getenv("AWS_REGION", "us-east-1")
        db_session = cross_account_session or boto3
        retried_ok = self._retry_failed_lattice_rows(db_vpc_id, db_session, db_region, steps)
        conn_id = NetworkRegistry.make_connection_id(self.agent_vpc_id, db_vpc_id, db_endpoint, "lattice")
        conn = network_registry.get_connection(conn_id)
        if conn and instance_id and conn.get("status") != "delete_failed":
            updated = network_registry.remove_reference(conn_id, instance_id)
            if updated and updated.get("ref_count", 0) > 0:
                steps.append(f"Removed reference. {updated['ref_count']} other record(s) still use this endpoint.")
                return {"steps": steps, "complete": True}
        conn = network_registry.get_connection(conn_id)
        complete = self._delete_database_lattice(
            conn_id, conn, db_vpc_id, db_region,
            vpce_id or (conn or {}).get("vpce_id", ""),
            resource_configuration_arn or (conn or {}).get("resource_configuration_arn", ""),
            resource_share_arn or (conn or {}).get("resource_share_arn", ""),
            db_session, steps)

        # The gateway is shared by every database in the VPC.
        gw_conn_id = NetworkRegistry.make_connection_id(self.agent_vpc_id, db_vpc_id, method="lattice-gateway")
        gw = network_registry.get_connection(gw_conn_id)
        if gw:
            updated = network_registry.remove_reference(gw_conn_id, instance_id) if instance_id else gw
            if updated and updated.get("ref_count", 0) > 0:
                steps.append(f"Resource gateway kept: {updated['ref_count']} other database(s) in {db_vpc_id} use it")
                return {"steps": steps, "complete": complete}
            if not (complete and retried_ok):
                # A configuration still bound to the gateway would make its delete fail;
                # leave it tracked until the database-level leftovers are gone.
                network_registry.update_connection(gw_conn_id, {
                    "status": "delete_failed", "failed_step": "resource_gateway",
                    "failure_detail": "Not attempted: a database's Lattice resources in this VPC are still being removed",
                })
                steps.append("Resource gateway kept until the database's own resources are gone")
                return {"steps": steps, "complete": False}
            complete = self._delete_lattice_gateway(gw_conn_id, gw, db_session, db_region, steps) and complete
        return {"steps": steps, "complete": complete}

    def _retry_failed_lattice_rows(self, db_vpc_id, db_session, db_region, steps) -> bool:
        """Finish every per-database Lattice teardown in this VPC that stopped half way
        (rows in ``delete_failed``). Gateway rows are left to the caller: Configure
        re-adopts a live gateway, cleanup deletes it with the last database. Returns
        whether every such row is now gone."""
        from tools.aws.network_registry import network_registry
        if not db_vpc_id:
            return True
        try:
            rows = network_registry.find_by_vpc(db_vpc_id)
        except Exception as e:
            steps.append(f"Could not read the network registry for {db_vpc_id}: {_short_error(e)}")
            return False
        ok = True
        for row in rows:
            if row.get("method") != "lattice" or row.get("status") != "delete_failed":
                continue
            if row.get("agent_vpc_id") and row["agent_vpc_id"] != self.agent_vpc_id:
                continue
            steps.append(f"Finishing an earlier incomplete teardown (stopped at {row.get('failed_step') or 'unknown step'})")
            ok = self._delete_database_lattice(
                row["connection_id"], row, db_vpc_id, db_region, row.get("vpce_id", ""),
                row.get("resource_configuration_arn", ""), row.get("resource_share_arn", ""),
                db_session, steps) and ok
        return ok

    def _delete_database_lattice(self, conn_id, conn, db_vpc_id, db_region, vpce_id, rcfg_arn, share_arn,
                                 db_session, steps) -> bool:
        """Delete one database's endpoint (and wait until it is gone), configuration and
        share. Deletes the registry row only when all three are confirmed absent;
        otherwise keeps (or writes) it as ``delete_failed`` naming what is left."""
        from tools.aws.network_registry import network_registry
        left = {"vpce_id": vpce_id, "resource_configuration_arn": rcfg_arn, "resource_share_arn": share_arn}

        def fail(step, detail):
            detail = _short_error(detail)
            steps.append(f"Teardown stopped at {step.replace('_', ' ')}: {detail}")
            data = dict(left, status="delete_failed", failed_step=step, failure_detail=detail)
            if not network_registry.get_connection(conn_id):
                network_registry.create_connection(dict(
                    left, connection_id=conn_id, method="lattice", db_vpc_id=db_vpc_id,
                    db_region=db_region, agent_vpc_id=self.agent_vpc_id, status="delete_failed"))
            network_registry.update_connection(conn_id, data)
            return False

        # 1. Endpoint, then wait until it is gone: the configuration cannot go while bound.
        if vpce_id:
            try:
                resp = self.ec2.delete_vpc_endpoints(VpcEndpointIds=[vpce_id]) or {}
                bad = [u for u in resp.get("Unsuccessful") or []
                       if "NotFound" not in str((u.get("Error") or {}).get("Code", ""))]
                if bad:
                    return fail("endpoint", (bad[0].get("Error") or {}).get("Message") or bad[0])
            except Exception as e:
                if "NotFound" not in str(e):
                    return fail("endpoint", e)

            def endpoint_gone():
                try:
                    eps = self.ec2.describe_vpc_endpoints(VpcEndpointIds=[vpce_id]).get("VpcEndpoints") or []
                except Exception as e:
                    if "NotFound" in str(e):
                        return True, None, "gone"
                    return False, None, f"unreadable ({_short_error(e)})"
                state = (eps[0].get("State") or "") if eps else "deleted"
                if state.lower() == "deleted":
                    return True, None, state
                if state.lower() == "failed":
                    return False, f"resource endpoint {vpce_id} is in state failed", state
                return False, None, state

            ok, last, error = _wait_until(endpoint_gone, DELETE_WAIT_BUDGET_S, POLL_INTERVAL_S)
            if not ok:
                return fail("endpoint", error or f"resource endpoint {vpce_id} still {last} after {DELETE_WAIT_BUDGET_S} s")
            steps.append(f"Deleted resource endpoint {vpce_id}")
            left["vpce_id"] = ""

        # 2. Resource configuration (a dependency conflict is a failure, not "done").
        if rcfg_arn:
            try:
                db_session.client("vpc-lattice", region_name=db_region).delete_resource_configuration(
                    resourceConfigurationIdentifier=rcfg_arn)
                steps.append("Deleted the resource configuration")
            except Exception as e:
                if "ResourceNotFound" not in str(e) and "NotFound" not in str(e):
                    return fail("resource_configuration", e)
                steps.append("Resource configuration already gone")
            left["resource_configuration_arn"] = ""

        # 3. The share.
        if share_arn:
            try:
                db_session.client("ram", region_name=db_region).delete_resource_share(resourceShareArn=share_arn)
                steps.append("Deleted the resource share")
            except Exception as e:
                if "UnknownResource" not in str(e) and "DELETED" not in str(e):
                    return fail("resource_share", e)
                steps.append("Resource share already gone")
            left["resource_share_arn"] = ""

        if conn or network_registry.get_connection(conn_id):
            network_registry.delete_connection(conn_id)
        return True

    def _delete_lattice_gateway(self, gw_conn_id, gw, db_session, db_region, steps) -> bool:
        """Delete the VPC's resource gateway, wait until it is gone, then its security
        group. Deletes the gateway row only when both are confirmed absent."""
        from tools.aws.network_registry import network_registry
        gateway_id = gw.get("resource_gateway_id", "")
        sg_id = gw.get("gateway_security_group_id", "")

        def fail(step, detail):
            detail = _short_error(detail)
            steps.append(f"Teardown stopped at {step.replace('_', ' ')}: {detail}")
            network_registry.update_connection(gw_conn_id, {
                "status": "delete_failed", "failed_step": step, "failure_detail": detail,
                "resource_gateway_id": gateway_id, "gateway_security_group_id": sg_id})
            return False

        if gateway_id:
            lattice = db_session.client("vpc-lattice", region_name=db_region)
            try:
                lattice.delete_resource_gateway(resourceGatewayIdentifier=gateway_id)
            except Exception as e:
                if "ResourceNotFound" not in str(e) and "NotFound" not in str(e):
                    return fail("resource_gateway", e)

            def gateway_gone():
                try:
                    status = lattice.get_resource_gateway(resourceGatewayIdentifier=gateway_id).get("status", "")
                except Exception as e:
                    if "ResourceNotFound" in str(e) or "NotFound" in str(e):
                        return True, None, "gone"
                    return False, None, f"unreadable ({_short_error(e)})"
                if status == "DELETE_FAILED":
                    return False, f"resource gateway {gateway_id} is DELETE_FAILED", status
                return False, None, status

            ok, last, error = _wait_until(gateway_gone, DELETE_WAIT_BUDGET_S, POLL_INTERVAL_S)
            if not ok:
                return fail("resource_gateway", error or f"resource gateway {gateway_id} still {last} after {DELETE_WAIT_BUDGET_S} s")
            steps.append(f"Deleted resource gateway {gateway_id}")
            gateway_id = ""

        if sg_id:
            try:
                db_session.client("ec2", region_name=db_region).delete_security_group(GroupId=sg_id)
                steps.append("Deleted the resource gateway security group")
            except Exception as e:
                if "InvalidGroup.NotFound" not in str(e):
                    return fail("gateway_security_group", e)
            sg_id = ""
        network_registry.delete_connection(gw_conn_id)
        return True

    # ──────────────────────────────────────────────
    # Shared helpers
    # ──────────────────────────────────────────────

    # ──────────────────────────────────────────────
    # Route-conflict classification and claim
    # ──────────────────────────────────────────────
    #
    # A DB-side route for the agent CIDR that already points at a peering OTHER than the one
    # being asserted is a leftover from some agent's earlier onboarding of this database (a
    # customer does not route traffic TO the agent CIDR over a peering for anything else).
    # Whether it is safe to repoint depends only on whether that old peering is still a live
    # path for someone:
    #
    #   DEAD           the old peering is gone / not active / its far end no longer exists.
    #                  Nothing uses it. Repointing fixes the stale route and breaks nothing.
    #   OURS           the old peering is active but tagged with THIS installation's agent
    #                  VPC and has no live reference in our registry: our own leftover from
    #                  an incomplete teardown. Safe to repoint.
    #   ALIVE_FOREIGN  the old peering is active and belongs to another installation (no
    #                  tag of ours, or a different agent VPC). Repointing would silently cut
    #                  THAT deployment off from this database. Never done automatically; a
    #                  space admin may claim it explicitly, with that consequence spelled out.
    #   UNKNOWN        it could not be classified (e.g. describing the peering was denied).
    #                  Treated exactly like ALIVE_FOREIGN: fail closed, refuse.
    #
    # Hard scope rule, enforced in _claim_conflicting_route: a claim may ONLY touch a route
    # whose destination is this installation's own agent CIDR. No other customer route is
    # ever replaced or deleted by this code, under any classification.

    CONFLICT_DEAD = "dead"
    CONFLICT_OURS = "ours"
    CONFLICT_ALIVE_FOREIGN = "alive_foreign"
    CONFLICT_UNKNOWN = "unknown"

    def _classify_conflict_holder(self, holder: str, db_vpc_id: str = "", ec2_client=None) -> dict:
        """Classify the target a conflicting agent-CIDR route points at.

        Returns ``{"kind", "detail", "holder", "far_vpc", "far_account"}``. Only a VPC
        peering holder (``pcx-...``) can ever be anything but UNKNOWN: a NAT gateway, TGW,
        ENI or instance target is not a peering of any agent and is never ours to claim.
        `db_vpc_id` lets the OURS check look the peering up in our registry."""
        out = {"kind": self.CONFLICT_UNKNOWN, "detail": "", "holder": holder or "",
               "far_vpc": "", "far_account": ""}
        if not holder or not str(holder).startswith("pcx-"):
            out["detail"] = f"the route's target {holder or '(none)'} is not a VPC peering connection"
            return out
        ec2 = ec2_client or self.ec2
        try:
            resp = ec2.describe_vpc_peering_connections(VpcPeeringConnectionIds=[holder])
            conns = resp.get("VpcPeeringConnections", [])
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "NotFound" in msg or "InvalidVpcPeeringConnectionID" in msg:
                out.update(kind=self.CONFLICT_DEAD, detail=f"peering {holder} no longer exists")
                return out
            # Permission denied, throttling, anything else: we cannot prove it is safe.
            out["detail"] = f"peering {holder} could not be described ({type(e).__name__}); refusing to assume it is unused"
            return out
        if not conns:
            out.update(kind=self.CONFLICT_DEAD, detail=f"peering {holder} no longer exists")
            return out
        conn = conns[0]
        status = (conn.get("Status") or {}).get("Code", "")
        # The requester side is the agent VPC for every peering any agent builds; the
        # accepter is the database VPC. "Far end" here means the agent side of the OLD
        # peering, i.e. whichever installation created it.
        req = conn.get("RequesterVpcInfo") or {}
        out["far_vpc"] = req.get("VpcId", "")
        out["far_account"] = req.get("OwnerId", "")
        if status != "active":
            out.update(kind=self.CONFLICT_DEAD, detail=f"peering {holder} is {status or 'not active'}")
            return out
        tags = {t.get("Key"): t.get("Value") for t in conn.get("Tags", []) or []}
        tagged_vpc = tags.get(agent_vpc_tag_key(), "")
        if tagged_vpc and tagged_vpc == self.agent_vpc_id:
            # Ours by tag. Still only a leftover if nothing of ours references it live. If
            # the registry cannot be read we cannot prove that, so fail closed (not OURS).
            try:
                from tools.aws.network_registry import network_registry
                live = network_registry.find_by_peering_id(holder, db_vpc_id)
            except Exception as e:  # noqa: BLE001
                out["detail"] = f"peering {holder} is tagged as ours but the registry could not be read ({type(e).__name__}); refusing to assume it is unused"
                return out
            if live and int(live.get("ref_count", 0) or 0) > 0:
                out.update(kind=self.CONFLICT_ALIVE_FOREIGN,
                           detail=f"peering {holder} is this installation's and still has {live.get('ref_count')} live reference(s)")
                return out
            out.update(kind=self.CONFLICT_OURS,
                       detail=f"peering {holder} was created by this installation (agent VPC {self.agent_vpc_id}) and has no live reference")
            return out
        who = (f"agent VPC {out['far_vpc']} in account {out['far_account']}" if out["far_vpc"]
               else "another installation")
        out.update(kind=self.CONFLICT_ALIVE_FOREIGN,
                   detail=f"peering {holder} is active and belongs to {who}")
        return out

    def _claim_conflicting_route(self, route_table_id, cidr, peering_id, ec2_client=None) -> None:
        """Repoint the conflicting DB-side route at `peering_id`.

        HARD SCOPE GUARD: refuses, before any AWS call, to touch a route whose destination is
        not this installation's own agent CIDR. The claim exists only to fix the agent's own
        return path; it must never be a way to rewrite any other customer route. The old
        peering is deliberately NOT deleted here (that has wider effects and is the job of
        the reference-counted teardown); only the route is repointed."""
        if cidr != self.agent_vpc_cidr:
            raise PermissionError(
                f"Refusing to claim route {cidr} in {route_table_id}: a claim may only repoint the "
                f"agent's own CIDR {self.agent_vpc_cidr}, never another route.")
        ec2 = ec2_client or self.ec2
        ec2.replace_route(RouteTableId=route_table_id, DestinationCidrBlock=cidr,
                          VpcPeeringConnectionId=peering_id)

    def _existing_route(self, ec2, route_table_id, cidr) -> dict:
        """The route entry for `cidr` in the given route table, or {} if none/unreadable."""
        try:
            resp = ec2.describe_route_tables(RouteTableIds=[route_table_id])
            for route in resp["RouteTables"][0].get("Routes", []):
                if route.get("DestinationCidrBlock") == cidr:
                    return route
        except Exception as e:
            logger.warning(f"Could not read routes in {route_table_id}: {e}")
        return {}

    def _ensure_route(self, route_table_id, cidr, peering_id, ec2_client=None):
        ec2 = ec2_client or self.ec2
        try:
            ec2.create_route(RouteTableId=route_table_id, DestinationCidrBlock=cidr,
                             VpcPeeringConnectionId=peering_id)
            return
        except Exception as e:
            if "RouteAlreadyExists" not in str(e):
                raise
        # A route for this destination already exists. Do NOT blindly overwrite it —
        # this table may be the customer's, and the route may be one they created.
        # Only touch it when it is safe: it is already ours (idempotent re-run), or it
        # is a stale blackhole (e.g. a peering we deleted and recreated).
        route = self._existing_route(ec2, route_table_id, cidr)
        if not route:
            raise RuntimeError(
                f"A route for {cidr} already exists in {route_table_id} but could not be "
                f"read; refusing to overwrite it blindly.")
        target = (route.get("VpcPeeringConnectionId") or route.get("GatewayId")
                  or route.get("NatGatewayId") or route.get("TransitGatewayId")
                  or route.get("NetworkInterfaceId") or route.get("InstanceId") or "")
        if target == peering_id:
            logger.info(f"Route {cidr} already points to {peering_id} in {route_table_id}; leaving as is")
            return
        if route.get("State") == "blackhole":
            ec2.replace_route(RouteTableId=route_table_id, DestinationCidrBlock=cidr,
                              VpcPeeringConnectionId=peering_id)
            logger.info(f"Reclaimed stale blackhole route {cidr} -> {peering_id} in {route_table_id}")
            return
        raise RouteConflict(route_table_id, cidr, target)

    def _get_route_tables_for_vpc(self, vpc_id, ec2_client=None):
        ec2 = ec2_client or self.ec2
        resp = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
        return [rt["RouteTableId"] for rt in resp["RouteTables"]]

    def _ensure_sg_ingress(self, sg_id, port, cidr, ec2_client=None):
        ec2 = ec2_client or self.ec2
        try:
            ec2.authorize_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[{"IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                                "IpRanges": [{"CidrIp": cidr, "Description": managed_by_tag()}]}])
        except Exception as e:
            if "Duplicate" in str(e) or "already exists" in str(e):
                pass
            else:
                raise


network_configurator = NetworkConfigurator()
