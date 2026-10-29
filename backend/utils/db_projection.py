# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Field projection for the databases listing.

`GET /api/databases` filters by membership but otherwise returned the raw registry
record, so an operator (whose token reaches the operator app) received the database's
credential pointer and the full cross-space membership list, not just the display and
placement fields the operator app uses.

A super admin gets the full record. A space admin of a member space gets the MANAGER
shape (`project_db_for_manager`): an allowlist of what the admin console needs, with every
space id set and credential pointer narrowed to the caller's own spaces. Everyone else
gets the OPERATOR shape (`project_db_for_operator`), a smaller allowlist: the database's
identity, placement and display state, plus which of the caller's own spaces include it.
Credential pointers, other tenants' space ids, the reach provider and network internals
are never in either unless named, so a field added to the record later is hidden by
default rather than leaked by default.

Deliberately NOT removed: the database's own placement (``account_id``, ``endpoint``,
``vpc_id``, ``subnet_ids``, ``security_group_ids``). That describes the database the
operator runs (not the deployment), the topology view already surfaces it to operators
by design, and it confers no capability on its own — assuming a role or configuring is
gated server-side by IAM and the write endpoints, not by knowing an account or VPC id.
"""

# The OPERATOR shape is an allowlist: a field reaches an operator (and the agent's own
# listing tool) only if it is named here. Subtracting fields from the raw record leaked
# a new one every time the record grew (reach_space_id, tags.space_id ...); naming what
# is included cannot. Everything here is either the database's own identity and
# placement (which the topology view shows operators anyway) or the operator app's
# display state. No credential pointer, no other tenant's space id, no network internals.
OPERATOR_DB_FIELDS = (
    "instance_id", "display_name", "engine", "region", "account_id", "endpoint",
    "reader_endpoint", "port", "vpc_id", "subnet_ids", "security_group_ids",
    "database_name", "status", "last_health_score",
    "last_scan_at", "tier1_enabled", "tier1_frequency", "tier2_enabled", "tier2_frequency",
    "schedule_enabled", "schedule_frequency", "cluster_member_roles",
    "writer_instance_id", "members_changed_at",
)
# The only tags an operator surface reads; the rest stay behind.
OPERATOR_TAG_FIELDS = ("engine_version", "instance_class", "multi_az", "cluster_members")

# The MANAGER shape (a space admin of a member space, not a super admin) is the operator
# shape plus what the admin console needs to run the database: its network state and
# configuration log, the membership COUNT (shown as "shared with N other spaces") but
# only the caller's OWN space ids, and only the caller's own credential pointers. Another
# tenant's space id, candidate id, reach-provider id or secret ARN is that tenant's
# business. Also an allowlist, for the same reason as above.
MANAGER_EXTRA_FIELDS = (
    "network_method", "network_peering_id", "vpce_id",
    "config_log", "config_error", "created_at", "updated_at", "arn", "retention_days",
    "network_route_mode", "host_routes",
    # Tier 1's host-route drift flag and the API task's refusal on it: the console shows
    # "Route repair needed" / "Route repair refused: <reason>" with a Repair button.
    "route_repair_needed",
    # An unresolved DB-side route conflict from the last configure (route table, agent
    # CIDR, the peering holding it): the console offers a space admin an explicit Claim
    # route. Names only this database's own network path, like network_peering_id.
    "route_conflict",
)
MANAGER_TAG_FIELDS = OPERATOR_TAG_FIELDS + (
    "arn", "raw_identifier", "source", "storage_encrypted", "kms_key_id", "parameter_group",
    "pi_enabled", "resource_relationships",
    # An RDS read replica's source (from RDS): the console lists it under its primary.
    "replica_of",
)


def project_db_for_operator(inst: dict, accessible) -> dict:
    """The operator shape of a registry record (see OPERATOR_DB_FIELDS).

    ``accessible`` is the set of space ids the caller can see; the only tenancy fact
    returned is which of THOSE spaces include the database (``member_space_ids``)."""
    proj = {k: inst[k] for k in OPERATOR_DB_FIELDS if k in inst}
    tags = inst.get("tags") or {}
    if isinstance(tags, dict):
        kept = {k: tags[k] for k in OPERATOR_TAG_FIELDS if k in tags}
        if kept:
            proj["tags"] = kept
    mine = set(accessible or [])
    members = set(inst.get("member_space_ids") or [])
    proj["member_space_ids"] = sorted(members & mine)
    return proj


def project_db_for_manager(inst: dict, manageable) -> dict:
    """The manager shape (see MANAGER_EXTRA_FIELDS): for a space admin who manages one of
    the database's member spaces but is not a super admin.

    ``manageable`` is the set of space ids the caller manages. Space id sets are narrowed
    to those; ``member_count`` carries the total so the console can say "shared with N
    other spaces" without naming them. ``space_secrets`` is narrowed to the caller's
    spaces, and the active ``secret_arn`` is returned only when one of those spaces is
    the reach provider that supplied it; ``reach_space_id`` only when it is theirs."""
    mine = set(manageable or set())
    proj = {k: inst[k] for k in OPERATOR_DB_FIELDS + MANAGER_EXTRA_FIELDS if k in inst}
    tags = inst.get("tags") or {}
    if isinstance(tags, dict):
        kept = {k: tags[k] for k in MANAGER_TAG_FIELDS if k in tags}
        if kept:
            proj["tags"] = kept
    members = set(inst.get("member_space_ids") or [])
    proj["member_space_ids"] = sorted(members & mine)
    proj["member_count"] = len(members)
    proj["candidate_space_ids"] = sorted(set(inst.get("candidate_space_ids") or []) & mine)
    secrets = inst.get("space_secrets") or {}
    if isinstance(secrets, dict):
        proj["space_secrets"] = {k: v for k, v in secrets.items() if k in mine}
    reach = inst.get("reach_space_id") or ""
    if reach in mine:
        proj["reach_space_id"] = reach
        if inst.get("secret_arn"):
            proj["secret_arn"] = inst["secret_arn"]
    return proj
