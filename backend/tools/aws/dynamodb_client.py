# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""DynamoDB client for database instance registry."""

import os
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional
import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from tools.resource_names import table_name
from tools.aws.ddb_pages import read_all
from tools.aws.teardown_claim import TEARDOWN_CLAIMABLE, teardown_claim_cutoff

logger = logging.getLogger("dynamodb-client")


# The fields update_instance may write. Everything per-space (member_space_ids,
# candidate_space_ids, space_secrets) is managed by its own atomic operation
# and is deliberately absent. A caller passing anything else gets a ValueError.
UPDATABLE_FIELDS = (
    "display_name", "account_id", "region", "engine", "secret_arn",
    "endpoint", "port", "database_name", "vpc_id", "subnet_ids",
    "security_group_ids", "status", "schedule_enabled",
    "schedule_frequency", "tags", "network_method", "network_peering_id",
    "vpce_id", "resource_configuration_arn", "resource_share_arn",
    "config_log", "config_error",
    # Set by _configure_peering when a route table already points the agent CIDR at a
    # live foreign peering; cleared by claim_conflicting_route or a later successful
    # configure. The admin console's "Claim route" button keys off it.
    "route_conflict",
    "tier1_frequency", "tier1_enabled", "tier2_frequency", "tier2_enabled",
    "retention_days",
    # DB-level tenancy: top-level physical-DB identity + reach-provider space.
    "arn", "reach_space_id",
    # Aurora role/endpoint facets (chat role focus): the cluster reader
    # endpoint and the per-member {instance_id, endpoint, is_writer,
    # availability_zone, instance_class} list.
    "reader_endpoint", "cluster_member_roles",
    # The writer Tier 1 last saw (only Tier 1 writes it: the failover key) and
    # when the members last changed.
    "writer_instance_id", "members_changed_at",
    # The cross-Region host-route path: how the agent reaches the DB VPC
    # (vpc | reused | host), the /32s this record owns, and the DB VPC CIDR.
    "network_route_mode", "host_routes", "db_vpc_cidr",
    # When onboarding marked this record `configuring`: the background provision's
    # liveness marker, read by _configure_is_stale to heal a strand (mirrors the
    # discovery `started_at` and the teardown `teardown_claimed_at`).
    "configuring_at",
)


class LastMemberError(Exception):
    """Raised by ``remove_member(keep_one=True)`` when the space is the database's last
    member (or is not a member at all): the caller must tear the database down rather than
    leave a record with no members and a live network path."""

    def __init__(self, instance_id: str, space_id: str):
        super().__init__(f"{space_id} is the last member of {instance_id}")
        self.instance_id = instance_id
        self.space_id = space_id


class DbInstanceRegistry:
    """Manages database instance configurations in DynamoDB."""

    def __init__(self):
        self._table = None

    @property
    def table(self):
        if self._table is None:
            from botocore.config import Config
            dynamodb = boto3.resource(
                "dynamodb",
                region_name=os.getenv("AWS_REGION", "us-east-1"),
                config=Config(max_pool_connections=50, retries={"max_attempts": 3, "mode": "adaptive"}),
            )
            self._table = dynamodb.Table(
                table_name("instances")
            )
        return self._table

    def list_instances(self, account_id: Optional[str] = None) -> list[dict]:
        """List all registered database instances, optionally filtered by account."""
        # Every page: discovery de-duplicates, retires and tears down against this list,
        # so a missing page would mean a second record for a database already registered.
        if account_id:
            return read_all(self.table.query, IndexName="account-index",
                            KeyConditionExpression=Key("account_id").eq(account_id))
        return read_all(self.table.scan)

    def get_instance(self, instance_id: str) -> Optional[dict]:
        """Get a single database instance by ID."""
        response = self.table.get_item(Key={"instance_id": instance_id})
        return response.get("Item")

    def find_by_arn(self, arn: str) -> Optional[dict]:
        """Return the single registry record for a physical DB by its ARN, or None.

        DB-level tenancy: one record per physical DB. Discovery uses this to attach a
        space to the existing record instead of minting a per-space duplicate.
        """
        if not arn:
            return None
        response = self.table.query(
            IndexName="arn-index",
            KeyConditionExpression=Key("arn").eq(arn),
        )
        items = response.get("Items", [])
        return items[0] if items else None

    def create_instance(self, data: dict) -> dict:
        """Register a new database instance. `space_id` names the discovering space."""
        now = datetime.now(timezone.utc).isoformat()
        tags = data.get("tags", {}) or {}
        # Physical-DB identity: prefer an explicit top-level arn, else the one carried in
        # tags (where discovery stamps it). Stored top-level so it can back arn-index.
        arn = data.get("arn") or tags.get("arn", "")
        space_id = data.get("space_id", "")
        item = {
            "instance_id": str(uuid.uuid4()),
            "display_name": data["display_name"],
            "account_id": data.get("account_id", ""),
            "region": data.get("region", os.getenv("AWS_REGION", "us-east-1")),
            "engine": data.get("engine", "aurora-postgresql"),
            "secret_arn": data["secret_arn"],
            "endpoint": data.get("endpoint", ""),
            "port": data.get("port", 5432),
            "database_name": data.get("database_name", "postgres"),
            "vpc_id": data.get("vpc_id", ""),
            "subnet_ids": data.get("subnet_ids", ""),
            "security_group_ids": data.get("security_group_ids", ""),
            "status": "configured",
            "last_scan_at": None,
            "last_health_score": None,
            "schedule_enabled": False,
            "schedule_frequency": None,
            "tags": tags,
            "created_at": now,
            "updated_at": now,
        }
        if arn:
            item["arn"] = arn
        # DB-level tenancy fields. `member_space_ids` is the set of spaces that include
        # this DB (the tenant boundary every gate reads); `reach_space_id` is the one member
        # whose cross-account ExternalId role + this DB's active secret are used to reach
        # and scan it. `candidate_space_ids` is the set of spaces that have merely
        # discovered it.
        #
        # Which set the creating space lands in depends on whether it has PROVED access:
        # a record created WITH a secret is a genuine onboard, so the space is a member
        # and the reach provider; a record created WITHOUT one (discovery) only proposes
        # the DB to the space. Finding a database never confers access to it.
        if space_id:
            if item["secret_arn"]:
                item["member_space_ids"] = {space_id}
                item["reach_space_id"] = space_id
                item["space_secrets"] = {space_id: item["secret_arn"]}
            else:
                item["candidate_space_ids"] = {space_id}
        self.table.put_item(Item=item)
        return item

    def add_member(self, instance_id: str, space_id: str) -> None:
        """Attach a space to a physical DB's membership (idempotent set ADD)."""
        if not space_id:
            return
        self.table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression="ADD member_space_ids :s SET updated_at = :ts",
            ExpressionAttributeValues={":s": {space_id}, ":ts": datetime.now(timezone.utc).isoformat()},
        )

    def remove_member(self, instance_id: str, space_id: str, keep_one: bool = False) -> None:
        """Detach a space from a physical DB's membership (set DELETE).

        With ``keep_one`` the DELETE is conditional on the set holding MORE than this one
        space, so two spaces leaving a two-member database at the same moment cannot both
        succeed and leave an empty set with no teardown: the second raises
        ``LastMemberError`` and its caller takes the physical teardown path instead. The
        condition is evaluated by DynamoDB, not from a membership set read earlier.
        Without ``keep_one`` (discovery housekeeping on never-onboarded records) the set may
        legitimately empty."""
        if not space_id:
            return
        kwargs = dict(
            Key={"instance_id": instance_id},
            UpdateExpression="DELETE member_space_ids :s SET updated_at = :ts",
            ExpressionAttributeValues={":s": {space_id}, ":ts": datetime.now(timezone.utc).isoformat()},
        )
        if keep_one:
            kwargs["ConditionExpression"] = "size(member_space_ids) > :one AND contains(member_space_ids, :sid)"
            kwargs["ExpressionAttributeValues"].update({":one": 1, ":sid": space_id})
        try:
            self.table.update_item(**kwargs)
        except ClientError as e:
            if keep_one and e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise LastMemberError(instance_id, space_id) from e
            raise

    # ── Candidates: discovered, not yet onboarded ─────────────────────────────
    # `candidate_space_ids` is the set of spaces whose discovery has FOUND this database
    # but that have not yet onboarded it. Discovery only ever writes here; it never
    # writes `member_space_ids`. A space moves from candidate to member solely through a
    # verified onboard (configure with a secret readable via that space's role, whose host
    # matches this database), so finding a database confers no access to it. Read only by
    # the admin console's Discovery step; every authorization gate reads members alone.

    def add_candidate(self, instance_id: str, space_id: str) -> None:
        """Propose this DB to a space (idempotent set ADD). Grants nothing."""
        if not space_id:
            return
        self.table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression="ADD candidate_space_ids :s SET updated_at = :ts",
            ExpressionAttributeValues={":s": {space_id}, ":ts": datetime.now(timezone.utc).isoformat()},
        )

    def remove_candidate(self, instance_id: str, space_id: str) -> None:
        """Withdraw a proposal (set DELETE; the attribute vanishes when empty)."""
        if not space_id:
            return
        self.table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression="DELETE candidate_space_ids :s SET updated_at = :ts",
            ExpressionAttributeValues={":s": {space_id}, ":ts": datetime.now(timezone.utc).isoformat()},
        )

    def promote_candidate(self, instance_id: str, space_id: str, secret_arn: str) -> None:
        """Candidate -> member in one write, recording the secret the space verified.

        This is the ONLY path that adds a space to `member_space_ids` after creation, so
        the invariant "membership requires a verified secret" is enforced where it is
        written rather than relied on at each caller."""
        if not space_id or not secret_arn:
            raise ValueError("promote_candidate requires a space_id and a verified secret_arn")
        now = datetime.now(timezone.utc).isoformat()
        self.table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression="ADD member_space_ids :s DELETE candidate_space_ids :s "
                             "SET space_secrets = if_not_exists(space_secrets, :empty), updated_at = :ts",
            ExpressionAttributeValues={":s": {space_id}, ":empty": {}, ":ts": now},
        )
        self.table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression="SET space_secrets.#s = :sa, updated_at = :ts",
            ExpressionAttributeNames={"#s": space_id},
            ExpressionAttributeValues={":sa": secret_arn, ":ts": now},
        )

    def set_reach_space(self, instance_id: str, space_id: str, secret_arn: str = "",
                        owner_admin: str = "") -> None:
        """Set the reach-provider space (whose admin's role scans this DB) and, alongside
        it, that admin's id as `reach_owner_admin`: the ``sts:ExternalId`` the Tier 1
        collector and the Tier 2 scan present. They read only this table, so the id is
        denormalised here from the trusted space row rather than taken from a job
        parameter.

        When `secret_arn` is given it becomes the record's ACTIVE credential at the same
        time, so a reach handoff to a surviving member swaps to that member's own verified
        secret atomically rather than leaving the departed provider's secret in place."""
        now = datetime.now(timezone.utc).isoformat()
        expr = "SET reach_space_id = :r, reach_owner_admin = :oa, updated_at = :ts"
        values = {":r": space_id, ":oa": owner_admin or "", ":ts": now}
        if secret_arn:
            expr += ", secret_arn = :sa"
            values[":sa"] = secret_arn
        self.table.update_item(Key={"instance_id": instance_id}, UpdateExpression=expr,
                               ExpressionAttributeValues=values)

    def set_space_secret(self, instance_id: str, space_id: str, secret_arn: str) -> None:
        """Record the secret `space_id` verified for this DB (map SET, idempotent)."""
        if not space_id or not secret_arn:
            return
        self.table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression="SET space_secrets = if_not_exists(space_secrets, :empty), updated_at = :ts",
            ExpressionAttributeValues={":empty": {}, ":ts": datetime.now(timezone.utc).isoformat()},
        )
        self.table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression="SET space_secrets.#s = :sa, updated_at = :ts",
            ExpressionAttributeNames={"#s": space_id},
            ExpressionAttributeValues={":sa": secret_arn, ":ts": datetime.now(timezone.utc).isoformat()},
        )

    def remove_space_secret(self, instance_id: str, space_id: str) -> None:
        """Drop `space_id`'s verified secret (no-op when absent)."""
        if not space_id:
            return
        try:
            self.table.update_item(
                Key={"instance_id": instance_id},
                UpdateExpression="REMOVE space_secrets.#s SET updated_at = :ts",
                ExpressionAttributeNames={"#s": space_id},
                ExpressionAttributeValues={":ts": datetime.now(timezone.utc).isoformat()},
                ConditionExpression="attribute_exists(space_secrets)",
            )
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            pass

    def update_instance(self, instance_id: str, data: dict) -> Optional[dict]:
        """Update a database instance configuration.

        Only the fields in ``UPDATABLE_FIELDS`` may be written here; the per-space sets
        and maps (membership, candidates, secrets, grants) have their own atomic
        operations. A field that is not on the list raises ``ValueError`` rather than
        being dropped: a silently ignored write looks like success to the caller and to
        the audit trail while nothing changed."""
        unknown = sorted(set(data) - set(UPDATABLE_FIELDS))
        if unknown:
            raise ValueError(f"update_instance: field(s) not updatable here: {', '.join(unknown)}")
        item = self.get_instance(instance_id)
        if not item:
            return None

        now = datetime.now(timezone.utc).isoformat()
        update_expr_parts = ["#updated_at = :updated_at"]
        expr_names = {"#updated_at": "updated_at"}
        expr_values = {":updated_at": now}

        for field in UPDATABLE_FIELDS:
            if field in data:
                safe_name = f"#{field}"
                safe_value = f":{field}"
                update_expr_parts.append(f"{safe_name} = {safe_value}")
                expr_names[safe_name] = field
                expr_values[safe_value] = data[field]

        self.table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression="SET " + ", ".join(update_expr_parts),
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )
        return self.get_instance(instance_id)

    def claim_teardown(self, instance_id: str) -> bool:
        """Mark a database ``deleting`` for its background teardown, once.

        The write is conditional (DynamoDB evaluates it): the record must exist and must
        not already be ``deleting`` under a live claim. A second delete of the same
        database (a double click, two admins) therefore finds the claim taken and starts
        no second teardown. A ``delete_failed`` record can be claimed again: that is the
        Retry. So can a ``deleting`` one whose claim has had no heartbeat for
        TEARDOWN_STALE_S (see teardown_stalled): its teardown died with its process.
        Returns whether this call took the claim."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            self.table.update_item(
                Key={"instance_id": instance_id},
                UpdateExpression="SET #st = :d, config_error = :e, #ua = :ts, #tc = :ts",
                ConditionExpression="attribute_exists(instance_id) AND " + TEARDOWN_CLAIMABLE,
                ExpressionAttributeNames={"#st": "status", "#tc": "teardown_claimed_at",
                                          "#ua": "updated_at"},
                ExpressionAttributeValues={":d": "deleting", ":e": "", ":ts": now,
                                           ":cutoff": teardown_claim_cutoff().isoformat()},
            )
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def touch_teardown(self, instance_id: str) -> None:
        """The running teardown's heartbeat: refresh ``teardown_claimed_at`` while the
        record is still ``deleting``. A no-op once the record is gone or has moved on."""
        try:
            self.table.update_item(
                Key={"instance_id": instance_id},
                UpdateExpression="SET #tc = :ts",
                ConditionExpression="#st = :d",
                ExpressionAttributeNames={"#st": "status", "#tc": "teardown_claimed_at"},
                ExpressionAttributeValues={":d": "deleting",
                                           ":ts": datetime.now(timezone.utc).isoformat()},
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise

    def delete_instance(self, instance_id: str) -> bool:
        """Remove a database instance."""
        self.table.delete_item(Key={"instance_id": instance_id})
        return True

    def update_scan_result(self, instance_id: str, health_score: str):
        """Update last scan timestamp and health score."""
        now = datetime.now(timezone.utc).isoformat()
        self.table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression="SET last_scan_at = :ts, last_health_score = :score, updated_at = :ts",
            ExpressionAttributeValues={
                ":ts": now,
                ":score": health_score,
            },
        )


db_registry = DbInstanceRegistry()
