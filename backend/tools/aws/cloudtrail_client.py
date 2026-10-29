# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""CloudTrail tools — look up recent API events for RDS instances.

Useful for answering "who changed what" — parameter group modifications,
reboots, failovers, scaling events, etc.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from strands import tool

logger = logging.getLogger("cloudtrail-tools")


def _get_ct_client(identifier: str = None):
    # Fail closed on a falsy identifier rather than returning an account-level client: the
    # space-scoped resolver would otherwise be skipped and CloudTrail read outside the
    # caller's space. A client is only ever obtained by resolving a specific database.
    if not identifier:
        raise PermissionError(
            "A database identifier is required for CloudTrail lookups; refusing an unscoped client."
        )
    from tools.aws.session_factory import get_session
    session = get_session(identifier=identifier)
    return session.client("cloudtrail")


def _rds_name(handle: str) -> str:
    """CloudTrail names the database by its RDS identifier, never the registry id or the
    display name (which a rename changes): resolve the handle through the registry."""
    from tools.aws.session_factory import resolve_target
    return resolve_target(handle)["rds_id"]


@tool
def get_rds_events(db_instance_id: str, hours: int = 24,
                   max_results: int = 50) -> dict:
    """Look up recent CloudTrail API events for an RDS/Aurora instance.

    Shows who made changes — parameter modifications, reboots, failovers,
    scaling, snapshot operations, etc.

    Args:
        db_instance_id: The database alias from [DATABASE SCOPE] (e.g. db-1)
        hours: How many hours back to search (default: 24)
        max_results: Maximum events to return (default: 50)

    Returns:
        Recent API events with user, action, and timestamp
    """
    try:
        ct = _get_ct_client(identifier=db_instance_id)
        rds_id = _rds_name(db_instance_id)
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)

        resp = ct.lookup_events(
            LookupAttributes=[
                {"AttributeKey": "ResourceName", "AttributeValue": rds_id},
            ],
            StartTime=start,
            EndTime=end,
            MaxResults=min(max_results, 50),
        )

        events = []
        for ev in resp.get("Events", []):
            # CloudTrailEvent is a JSON string; parse it to read the real sourceIPAddress.
            source_ip = ""
            raw_detail = ev.get("CloudTrailEvent", "")
            if raw_detail:
                try:
                    source_ip = json.loads(raw_detail).get("sourceIPAddress", "") or ""
                except (ValueError, TypeError):
                    source_ip = ""
            events.append({
                "event_time": ev["EventTime"].isoformat(),
                "event_name": ev.get("EventName", ""),
                "username": ev.get("Username", ""),
                "event_source": ev.get("EventSource", ""),
                "source_ip": source_ip,
                "resources": [
                    {"type": r.get("ResourceType"), "name": r.get("ResourceName")}
                    for r in ev.get("Resources", [])
                ],
            })

        return {
            "db_instance_id": db_instance_id,
            "hours": hours,
            "events": events,
            "count": len(events),
        }
    except Exception as e:
        return {"error": str(e)}


@tool
def get_rds_modifications(db_instance_id: str, hours: int = 72) -> dict:
    """Look up recent RDS modification events — parameter changes, scaling,
    reboots, failovers, and configuration changes.

    Args:
        db_instance_id: The database alias from [DATABASE SCOPE] (e.g. db-1)
        hours: How many hours back to search (default: 72)

    Returns:
        Filtered list of modification events
    """
    try:
        ct = _get_ct_client(identifier=db_instance_id)
        rds_id = _rds_name(db_instance_id)
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)

        # RDS modification event names
        modify_events = {
            "ModifyDBInstance", "ModifyDBCluster", "RebootDBInstance",
            "FailoverDBCluster", "ModifyDBParameterGroup",
            "ModifyDBClusterParameterGroup", "ApplyPendingMaintenanceAction",
            "StartDBInstance", "StopDBInstance", "CreateDBSnapshot",
            "DeleteDBInstance", "ModifyDBSubnetGroup",
        }

        resp = ct.lookup_events(
            LookupAttributes=[
                {"AttributeKey": "ResourceName", "AttributeValue": rds_id},
            ],
            StartTime=start,
            EndTime=end,
            MaxResults=50,
        )

        modifications = []
        for ev in resp.get("Events", []):
            if ev.get("EventName") in modify_events:
                modifications.append({
                    "event_time": ev["EventTime"].isoformat(),
                    "event_name": ev.get("EventName", ""),
                    "username": ev.get("Username", ""),
                    "event_source": ev.get("EventSource", ""),
                })

        return {
            "db_instance_id": db_instance_id,
            "hours": hours,
            "modifications": modifications,
            "count": len(modifications),
        }
    except Exception as e:
        return {"error": str(e)}
