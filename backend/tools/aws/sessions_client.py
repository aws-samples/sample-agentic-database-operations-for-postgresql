# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Chat-session index — DynamoDB client for the operator app's Recents.

AgentCore Memory persists the conversation events, but it has no
list-sessions / human-title API. This lightweight index fills that gap: one
row per chat session, keyed by the caller's Cognito sub (actor_id) so a user
only ever sees their own sessions. The AgentCore runtime session id is the
sort key. Rows are upserted by the API on each chat turn (and on rename); the
streaming /invocations path is intentionally left untouched.
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key, Attr
from tools.resource_names import table_name

logger = logging.getLogger("sessions-client")


class SessionRegistry:
    """CRUD operations for the per-user chat-session index in DynamoDB."""

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
                table_name("sessions")
            )
        return self._table

    def list_sessions(self, actor_id: str, space_id: Optional[str] = None) -> list:
        """List a user's chat sessions (most-recently-active first).

        Scoped to the caller via the actor_id partition key, optionally
        filtered to a single Agent Space.
        """
        kwargs = {"KeyConditionExpression": Key("actor_id").eq(actor_id)}
        if space_id:
            kwargs["FilterExpression"] = Attr("space_id").eq(space_id)

        response = self.table.query(**kwargs)
        items = response.get("Items", [])
        while "LastEvaluatedKey" in response:
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
            response = self.table.query(**kwargs)
            items.extend(response.get("Items", []))

        items.sort(key=lambda x: x.get("last_active", ""), reverse=True)
        return items

    def get_session(self, actor_id: str, session_id: str) -> Optional[dict]:
        """Get a single session row for this user."""
        response = self.table.get_item(
            Key={"actor_id": actor_id, "session_id": session_id}
        )
        return response.get("Item")

    def upsert_session(
        self,
        actor_id: str,
        session_id: str,
        title: Optional[str] = None,
        space_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Create or update a session row.

        Always bumps last_active; sets created_at only on first write
        (if_not_exists). title / space_id are written only when provided, so a
        plain activity bump never clobbers an existing title.
        """
        now = datetime.now(timezone.utc).isoformat()
        set_parts = [
            "#created_at = if_not_exists(#created_at, :now)",
            "#last_active = :now",
        ]
        expr_names = {"#created_at": "created_at", "#last_active": "last_active"}
        expr_values = {":now": now}

        if title is not None:
            set_parts.append("#title = :title")
            expr_names["#title"] = "title"
            expr_values[":title"] = title
        if space_id is not None:
            set_parts.append("#space_id = :space_id")
            expr_names["#space_id"] = "space_id"
            expr_values[":space_id"] = space_id

        self.table.update_item(
            Key={"actor_id": actor_id, "session_id": session_id},
            UpdateExpression="SET " + ", ".join(set_parts),
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )
        return self.get_session(actor_id, session_id)

    def get_db_aliases(self, actor_id: str, session_id: str) -> dict:
        """The session's stable ``{alias: instance_id}`` mapping, or ``{}``. Kept
        server-side so an alias (db-1, db-2, ...) names the same database for the whole
        session even as the selection changes between turns."""
        row = self.get_session(actor_id, session_id) or {}
        aliases = row.get("db_aliases")
        return dict(aliases) if isinstance(aliases, dict) else {}

    def put_db_aliases(self, actor_id: str, session_id: str, mapping: dict) -> None:
        """Persist the session's alias mapping (creating the row if needed, bumping
        last_active). Fail-soft: a persistence error must not break the chat turn — the
        turn's own in-scope aliases still resolve; only cross-turn stability degrades."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            self.table.update_item(
                Key={"actor_id": actor_id, "session_id": session_id},
                UpdateExpression=("SET #created_at = if_not_exists(#created_at, :now), "
                                  "#last_active = :now, #db_aliases = :m"),
                ExpressionAttributeNames={"#created_at": "created_at", "#last_active": "last_active",
                                          "#db_aliases": "db_aliases"},
                ExpressionAttributeValues={":now": now, ":m": dict(mapping or {})},
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not persist session db aliases (%s): %s", session_id, type(e).__name__)

    def delete_session(self, actor_id: str, session_id: str) -> bool:
        """Delete a session row for this user."""
        self.table.delete_item(
            Key={"actor_id": actor_id, "session_id": session_id}
        )
        return True


sessions_registry = SessionRegistry()
