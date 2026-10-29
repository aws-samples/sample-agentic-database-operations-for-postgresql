# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Agent Spaces management — DynamoDB client for the spaces table."""

import os
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError
from tools.resource_names import table_name
from tools.aws.ddb_pages import read_all
from tools.aws.teardown_claim import TEARDOWN_CLAIMABLE, teardown_claim_cutoff

logger = logging.getLogger("spaces-client")


class SpacesRegistry:
    """CRUD operations for Agent Spaces in DynamoDB."""

    def __init__(self):
        self._table = None

    @property
    def table(self):
        if self._table is None:
            dynamodb = boto3.resource(
                "dynamodb",
                region_name=os.getenv("AWS_REGION", "us-east-1"),
            )
            self._table = dynamodb.Table(
                table_name("spaces")
            )
        return self._table

    def list_spaces(self) -> list:
        """List all agent spaces (every page: access and counts derive from this list)."""
        return read_all(self.table.scan)

    def get_space(self, space_id: str) -> Optional[dict]:
        """Get a single space by ID. A falsy id has no space and must not reach DynamoDB:
        GetItem rejects an empty key attribute with a ValidationException, which would
        surface as a 500 on any read path that resolves a space id from an unassigned
        record (e.g. a candidate-only DB with no member space)."""
        if not space_id:
            return None
        response = self.table.get_item(Key={"space_id": space_id})
        return response.get("Item")

    def create_space(self, data: dict) -> dict:
        """Create a new agent space."""
        now = datetime.now(timezone.utc).isoformat()
        item = {
            "space_id": str(uuid.uuid4()),
            "name": data["name"],
            "description": data.get("description", ""),
            "primary_account_id": data.get("primary_account_id", ""),
            "secondary_account_ids": data.get("secondary_account_ids", []),
            "regions": data.get("regions", []),
            "iam_role_arn": data.get("iam_role_arn", ""),
            "web_app_role_arn": data.get("web_app_role_arn", ""),
            # The admin who owns the space; credentials and access roles are keyed on it.
            # Set here from the caller's identity and deliberately absent from
            # update_space's allowlist, so it can never be changed afterwards.
            "owner_admin": data.get("owner_admin") or data.get("created_by", ""),
            "secondary_role_names": data.get("secondary_role_names", {}),
            "capabilities": data.get("capabilities", {
                "discovery": True,
                "scanning": True,
                "agent_chat": True,
                "observability": True,
            }),
            # Per-space governance (model-and-knowledge-governance feature). Empty
            # ⇒ fall back to account/env default (models) and today's built-ins
            # only (knowledge); populated via the admin console.
            "model_governance": data.get("model_governance", {}),
            "knowledge_governance": data.get("knowledge_governance", {}),
            "status": "creating",
            "tags": data.get("tags", {}),
            "database_count": 0,
            "created_by": data.get("created_by", ""),
            "created_at": now,
            "updated_at": now,
        }
        self.table.put_item(Item=item)
        return item

    def update_space(self, space_id: str, data: dict) -> Optional[dict]:
        """Update a space configuration."""
        item = self.get_space(space_id)
        if not item:
            return None

        now = datetime.now(timezone.utc).isoformat()
        # NOTE: this list is a field allowlist, NOT an authorization boundary. It says
        # which attributes may be written at all; it does not say by whom — the handlers
        # decide that. primary_account_id / secondary_account_ids / secondary_role_names
        # decide which AWS accounts and roles the deployment assumes into for this space;
        # see PUT /api/spaces/{id} for why the space's own admin owns that decision and
        # what makes it safe.
        allowed_fields = [
            "name", "description", "primary_account_id",
            "secondary_account_ids", "regions",
            # Per-account Region overrides, a map of account id to its Region list;
            # accounts absent from it inherit `regions`. See _regions_for_account in app.py.
            # (Keep this comment free of square brackets: test_discovery_background parses
            # this allowlist up to the first closing bracket.)
            "account_regions",
            "iam_role_arn",
            "web_app_role_arn", "capabilities", "status", "tags",
            "database_count", "last_discovery_at",
            # The discovery lifecycle marker the console polls (running/done/failed/stale).
            # Written by POST /discover and the background _run_discovery; without it here
            # every write is silently dropped and the console spins on a status that never
            # leaves "idle" (PRODUCT-RULES 15/16: a long action must reach a final state).
            "discovery_status",
            "access_configured",
            "provisioning_log",
            "account_statuses", "secondary_role_names",
            "model_governance", "knowledge_governance",
            # What the last discovery found but cannot be onboarded (name, region, reason).
            "unsupported_databases",
        ]

        update_expr_parts = []
        expr_names = {}
        expr_values = {":updated_at": now}
        update_expr_parts.append("#updated_at = :updated_at")
        expr_names["#updated_at"] = "updated_at"

        for key, value in data.items():
            if key in allowed_fields:
                placeholder = f"#{key}"
                val_placeholder = f":{key}"
                update_expr_parts.append(f"{placeholder} = {val_placeholder}")
                expr_names[placeholder] = key
                expr_values[val_placeholder] = value

        if len(update_expr_parts) <= 1:
            return item

        self.table.update_item(
            Key={"space_id": space_id},
            UpdateExpression="SET " + ", ".join(update_expr_parts),
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )
        return self.get_space(space_id)

    def claim_teardown(self, space_id: str) -> bool:
        """Mark an existing space ``deleting`` for its deletion, once (a conditional write,
        evaluated by DynamoDB): refused while another request's deletion holds a live
        claim, taken over when that claim has had no heartbeat for TEARDOWN_STALE_S (its
        process stopped). A ``delete_failed`` space can be claimed again: the Retry.
        Returns whether this call took the claim; False also when the space is gone."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            self.table.update_item(
                Key={"space_id": space_id},
                UpdateExpression="SET #st = :d, #ua = :ts, #tc = :ts",
                ConditionExpression="attribute_exists(space_id) AND " + TEARDOWN_CLAIMABLE,
                ExpressionAttributeNames={"#st": "status", "#tc": "teardown_claimed_at",
                                          "#ua": "updated_at"},
                ExpressionAttributeValues={":d": "deleting", ":ts": now,
                                           ":cutoff": teardown_claim_cutoff().isoformat()},
            )
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def touch_teardown(self, space_id: str) -> None:
        """The space deletion's heartbeat: refresh ``teardown_claimed_at`` while the space
        is still ``deleting``. A no-op once it is gone or has moved on."""
        try:
            self.table.update_item(
                Key={"space_id": space_id},
                UpdateExpression="SET #tc = :ts",
                ConditionExpression="#st = :d",
                ExpressionAttributeNames={"#st": "status", "#tc": "teardown_claimed_at"},
                ExpressionAttributeValues={":d": "deleting",
                                           ":ts": datetime.now(timezone.utc).isoformat()},
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise

    def delete_space(self, space_id: str) -> bool:
        """Delete an agent space."""
        self.table.delete_item(Key={"space_id": space_id})
        return True


spaces_registry = SpacesRegistry()
