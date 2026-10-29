# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""DynamoDB client for investigation records."""

import os
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key
from tools.resource_names import table_name
from tools.aws.ddb_pages import read_all

logger = logging.getLogger("investigations-client")


class InvestigationsRegistry:
    """CRUD operations for investigations in DynamoDB."""

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
                table_name("investigations")
            )
        return self._table

    def create(self, space_id: str, data: dict) -> dict:
        """Create a new investigation."""
        now = datetime.now(timezone.utc).isoformat()
        item = {
            "investigation_id": str(uuid.uuid4()),
            "space_id": space_id,
            "title": data.get("title", data.get("prompt", "")[:100]),
            "status": "running",
            "trigger": data.get("prompt", ""),
            "database_ids": data.get("database_ids", []),
            "started_at": now,
            "started_by": data.get("started_by", ""),
            "findings": [],
            "timeline": [],
        }
        if data.get("quick_start_type"):
            item["quick_start_type"] = data["quick_start_type"]
        if data.get("time_range"):
            item["time_range"] = data["time_range"]
        if data.get("database_names"):
            item["database_names"] = data["database_names"]
        if data.get("model_id"):
            item["model_id"] = data["model_id"]  # pinned model for this investigation
        if data.get("source_session_id"):
            # Chat session that spawned this investigation — lets the chat link
            # to it and reflect its status back into the conversation.
            item["source_session_id"] = data["source_session_id"]
        self.table.put_item(Item=item)
        # Return with None fields for API consistency
        item.setdefault("completed_at", None)
        item.setdefault("summary", None)
        item.setdefault("severity", None)
        item.setdefault("quick_start_type", None)
        return item

    def get(self, investigation_id: str) -> Optional[dict]:
        """Get a single investigation."""
        response = self.table.get_item(Key={"investigation_id": investigation_id})
        return response.get("Item")

    def list_for_space(self, space_id: str) -> list:
        """List all investigations for a space, newest first."""
        try:
            items = read_all(self.table.query, IndexName="space-index",
                             KeyConditionExpression=Key("space_id").eq(space_id))
        except Exception:
            # Fallback to scan if GSI not available
            items = read_all(self.table.scan, FilterExpression="space_id = :sid",
                             ExpressionAttributeValues={":sid": space_id})
        items.sort(key=lambda x: x.get("started_at", ""), reverse=True)
        return items

    def list_running(self) -> list:
        """Every investigation currently in the 'running' state, across all spaces.

        Used by the server-side stale-run sweep (there is no HTTP route for it). A table
        scan filtered to running: the running set is small (one or two at a time), so a
        periodic scan is cheap. Never raises — a read error yields an empty list and the
        sweep simply does nothing this pass."""
        try:
            return read_all(self.table.scan, FilterExpression="#s = :r",
                            ExpressionAttributeNames={"#s": "status"},
                            ExpressionAttributeValues={":r": "running"})
        except Exception as e:  # noqa: BLE001
            logger.warning("list_running failed (%s); the sweep does nothing this pass", e)
            return []

    def claim_run(self, investigation_id: str, claim: str) -> bool:
        """Stamp a stream claim on a RUNNING, UNCLAIMED record (a conditional write).

        Returns True if this caller won the claim, False if the record already carries
        one or is no longer running. This is how a reopen (reload or a second tab) is
        prevented from re-streaming a run that is already in flight: the first streamer
        claims it; a later one sees the claim and refuses to re-stream, showing the
        record's state instead."""
        try:
            self.table.update_item(
                Key={"investigation_id": investigation_id},
                UpdateExpression="SET run_claim = :c, run_claim_at = :now",
                ConditionExpression=(
                    "#s = :running AND attribute_not_exists(run_claim)"),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":c": claim, ":running": "running",
                    ":now": datetime.now(timezone.utc).isoformat()},
            )
            return True
        except Exception as e:  # noqa: BLE001 - a conditional failure means already claimed
            code = (getattr(e, "response", None) or {}).get("Error", {}).get("Code", "")
            if code != "ConditionalCheckFailedException":
                logger.warning("claim_run error for %s (%s)", investigation_id, e)
            return False

    def reset_for_retry(self, investigation_id: str) -> bool:
        """Re-open a FAILED (or cancelled) run for another attempt: back to 'running',
        clear the run claim and the prior summary/severity, so the stream path can claim
        it afresh. A conditional write guards against racing a run that is somehow still
        going. Returns True on success. Only a finished-unsuccessfully run can be retried;
        a 'running' or 'completed' record is left untouched (returns False)."""
        try:
            self.table.update_item(
                Key={"investigation_id": investigation_id},
                UpdateExpression=("SET #s = :running, started_at = :now, updated_at = :now "
                                  "REMOVE run_claim, run_claim_at, summary, severity, completed_at"),
                ConditionExpression="#s IN (:failed, :cancelled)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":running": "running", ":failed": "failed", ":cancelled": "cancelled",
                    ":now": datetime.now(timezone.utc).isoformat()},
            )
            return True
        except Exception as e:  # noqa: BLE001 - a conditional failure means not retryable
            code = (getattr(e, "response", None) or {}).get("Error", {}).get("Code", "")
            if code != "ConditionalCheckFailedException":
                logger.warning("reset_for_retry error for %s (%s)", investigation_id, e)
            return False

    def update_status(self, investigation_id: str, status: str,
                      completed_at: Optional[str] = None) -> None:
        """Update investigation status."""
        expr = "SET #status = :status, updated_at = :now"
        vals = {
            ":status": status,
            ":now": datetime.now(timezone.utc).isoformat(),
        }
        names = {"#status": "status"}
        if completed_at:
            expr += ", completed_at = :completed"
            vals[":completed"] = completed_at
        self.table.update_item(
            Key={"investigation_id": investigation_id},
            UpdateExpression=expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=vals,
        )

    def add_finding(self, investigation_id: str, finding: dict) -> None:
        """Append a finding."""
        if "id" not in finding:
            finding["id"] = str(uuid.uuid4())[:8]
        # Strip None values — DynamoDB doesn't accept them
        clean = {k: v for k, v in finding.items() if v is not None}
        self.table.update_item(
            Key={"investigation_id": investigation_id},
            UpdateExpression="SET findings = list_append(if_not_exists(findings, :empty), :finding)",
            ExpressionAttributeValues={
                ":finding": [clean],
                ":empty": [],
            },
        )

    def set_summary(self, investigation_id: str, summary: str,
                    severity: Optional[str] = None) -> None:
        """Set the investigation summary and overall severity."""
        expr = "SET summary = :summary"
        vals = {":summary": summary or ""}
        if severity:
            expr += ", severity = :severity"
            vals[":severity"] = severity
        self.table.update_item(
            Key={"investigation_id": investigation_id},
            UpdateExpression=expr,
            ExpressionAttributeValues=vals,
        )

    def delete(self, investigation_id: str) -> bool:
        """Delete an investigation."""
        self.table.delete_item(Key={"investigation_id": investigation_id})
        return True


investigations_registry = InvestigationsRegistry()
