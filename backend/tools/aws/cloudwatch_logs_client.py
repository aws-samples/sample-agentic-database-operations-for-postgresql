# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""CloudWatch Logs tools — query RDS error logs, slow query logs, audit logs.

RDS publishes logs to CloudWatch Logs under log groups like:
  /aws/rds/instance/{instance_id}/error
  /aws/rds/instance/{instance_id}/postgresql
  /aws/rds/cluster/{cluster_id}/postgresql
"""

import logging
import time
from datetime import datetime, timedelta, timezone

from strands import tool

logger = logging.getLogger("cw-logs-tools")


def _get_logs_client(identifier: str = None):
    # A falsy identifier must fail closed, never fall through to an account-level client.
    # Without a resolved registry row the space-scoped, fail-closed resolver in
    # session_factory is skipped, and a model-authored query would run against every RDS
    # log group in the deployment account, other spaces' logs included. The only
    # way to obtain a client is to resolve a specific database in the caller's space.
    if not identifier:
        raise PermissionError(
            "A database identifier is required to read logs; refusing an unscoped client."
        )
    from tools.aws.session_factory import get_session
    session = get_session(identifier=identifier)
    return session.client("logs")


@tool
def get_db_error_logs(db_instance_id: str, hours: int = 6,
                      limit: int = 100) -> dict:
    """Fetch recent error log entries for an RDS/Aurora instance from CloudWatch Logs.

    Args:
        db_instance_id: The database alias from [DATABASE SCOPE] (e.g. db-1)
        hours: How many hours back to search (default: 6)
        limit: Maximum log events to return (default: 100)

    Returns:
        Recent error log entries with timestamps
    """
    try:
        logs = _get_logs_client(identifier=db_instance_id)
        # Log groups are named by the RDS identifier, never the registry id or the
        # display name (which a rename changes).
        from tools.aws.session_factory import resolve_target
        rds_id = resolve_target(db_instance_id)["rds_id"]
        end = int(datetime.now(timezone.utc).timestamp() * 1000)
        start = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp() * 1000)

        # Try instance-level, then cluster-level log groups
        log_groups = [
            f"/aws/rds/instance/{rds_id}/postgresql",
            f"/aws/rds/instance/{rds_id}/error",
            f"/aws/rds/cluster/{rds_id}/postgresql",
        ]

        for lg in log_groups:
            try:
                resp = logs.filter_log_events(
                    logGroupName=lg,
                    startTime=start,
                    endTime=end,
                    limit=limit,
                    interleaved=True,
                )
                events = [
                    {
                        "timestamp": datetime.fromtimestamp(
                            e["timestamp"] / 1000, tz=timezone.utc
                        ).isoformat(),
                        "message": e["message"][:1000],
                    }
                    for e in resp.get("events", [])
                ]
                if events:
                    return {
                        "db_instance_id": db_instance_id,
                        "log_group": lg,
                        "hours": hours,
                        "events": events,
                        "count": len(events),
                    }
            except logs.exceptions.ResourceNotFoundException:
                continue

        return {
            "db_instance_id": db_instance_id,
            "events": [],
            "count": 0,
            "message": "No log groups found. Ensure log exports are enabled on the RDS instance.",
        }
    except Exception as e:
        return {"error": str(e)}


@tool
def query_db_logs(db_instance_id: str, query: str,
                  hours: int = 6) -> dict:
    """Run a CloudWatch Logs Insights query against RDS logs.

    Powerful for filtering and aggregating log data. Uses CloudWatch Logs
    Insights query syntax.

    Example queries:
    - "fields @timestamp, @message | filter @message like /ERROR/ | sort @timestamp desc | limit 20"
    - "stats count(*) by bin(5m) | filter @message like /duration/"

    Args:
        db_instance_id: The database alias from [DATABASE SCOPE] (e.g. db-1)
        query: CloudWatch Logs Insights query string
        hours: How many hours back to search (default: 6)

    Returns:
        Query results
    """
    try:
        logs = _get_logs_client(identifier=db_instance_id)
        # Log groups are named by the RDS identifier, never the registry id or the
        # display name (which a rename changes).
        from tools.aws.session_factory import resolve_target
        rds_id = resolve_target(db_instance_id)["rds_id"]
        end = int(datetime.now(timezone.utc).timestamp())
        start = end - (hours * 3600)

        # Find available log groups
        log_groups = []
        for prefix in [
            f"/aws/rds/instance/{rds_id}",
            f"/aws/rds/cluster/{rds_id}",
        ]:
            try:
                resp = logs.describe_log_groups(logGroupNamePrefix=prefix)
                log_groups.extend(
                    [lg["logGroupName"] for lg in resp.get("logGroups", [])]
                )
            except Exception:
                pass

        if not log_groups:
            return {
                "error": "No log groups found for this instance.",
                "hint": "Enable log exports in the RDS instance configuration.",
            }

        resp = logs.start_query(
            logGroupNames=log_groups,
            startTime=start,
            endTime=end,
            queryString=query,
            limit=100,
        )
        query_id = resp["queryId"]

        # Poll for results (max 30 seconds)
        for _ in range(30):
            result = logs.get_query_results(queryId=query_id)
            if result["status"] in ("Complete", "Failed", "Cancelled"):
                break
            time.sleep(1)  # nosemgrep: arbitrary-sleep - poll loop waiting for CloudWatch Logs Insights query to complete

        rows = []
        for r in result.get("results", []):
            row = {f["field"]: f["value"] for f in r}
            rows.append(row)

        return {
            "db_instance_id": db_instance_id,
            "log_groups": log_groups,
            "query": query,
            "status": result["status"],
            "results": rows,
            "count": len(rows),
            "stats": result.get("statistics", {}),
        }
    except Exception as e:
        return {"error": str(e)}
