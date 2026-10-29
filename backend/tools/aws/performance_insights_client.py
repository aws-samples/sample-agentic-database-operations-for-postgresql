# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Performance Insights tools — DB load, top wait events, top SQL, top hosts/users.

These tools are engine-agnostic and work with any RDS/Aurora instance that has
Performance Insights enabled. They use the `pi` boto3 client.
"""

import logging
from datetime import datetime, timedelta, timezone

from strands import tool

from tools.aws.cloudwatch_client import _data_window

logger = logging.getLogger("pi-tools")


def _get_pi_client(identifier: str = None):
    """Get a PI client via the space-scoped, cross-account session.

    A falsy identifier fails closed rather than returning an account-level client: the
    space resolver would otherwise be skipped and Performance Insights read outside the
    caller's space. A client is only ever obtained by resolving a specific database.
    """
    if not identifier:
        raise PermissionError(
            "A database identifier is required for Performance Insights; refusing an unscoped client."
        )
    from tools.aws.session_factory import get_session, get_session_context
    session = get_session(identifier=identifier)
    ctx = get_session_context(session)
    logger.info(f"PI client for {identifier}: context={ctx}")
    return session.client("pi")


def _error_context(identifier: str) -> dict:
    """The session context for an error result. The resolution that failed (an ambiguous
    name, a database outside the space) must not raise again from the error path."""
    from tools.aws.session_factory import get_session, get_session_context
    try:
        return get_session_context(get_session(identifier=identifier))
    except Exception:  # noqa: BLE001 - the error result already says what failed
        return {"type": "unresolved"}


def _get_resource_arn(db_instance_id: str, identifier: str = None) -> str:
    """Build the DbiResourceId that PI expects.
    PI needs the DbiResourceId, not the instance identifier.
    We look it up via RDS describe.
    
    Args:
        db_instance_id: The member instance ID to look up
        identifier: The original cluster/instance identifier for cross-account session resolution
    """
    from tools.aws.session_factory import get_session
    session = get_session(identifier=identifier or db_instance_id)
    rds = session.client("rds")
    try:
        resp = rds.describe_db_instances(DBInstanceIdentifier=db_instance_id)
        dbi = resp["DBInstances"][0]
        resource_id = dbi["DbiResourceId"]
        logger.info(f"Resolved PI resource: {db_instance_id} -> {resource_id}")
        return resource_id
    except Exception as e:
        logger.warning(f"Failed to resolve DbiResourceId for {db_instance_id}: {e}")
        # Fallback: try as-is (user may have passed DbiResourceId directly)
        return db_instance_id


def _resolve_instance_ids(identifier: str) -> list[dict]:
    """Resolve a cluster ID to role-bearing members (writer first), or return as-is.

    Returns ``[{"id", "is_writer", "role"}, ...]`` — writer first — mirroring the
    CloudWatch client's shape; ``_pick_target`` chooses the member PI reads. A standalone/instance or unresolvable target is its own
    ``writer`` primary.
    """
    from tools.aws.session_factory import get_session, get_session_context, resolve_target
    # The handle is the registry id (or a display name); RDS knows the database only by
    # its own identifier, which the registry record carries.
    rds_id = resolve_target(identifier)["rds_id"]
    session = get_session(identifier=identifier)
    ctx = get_session_context(session)
    logger.info(f"PI _resolve_instance_ids({identifier} -> {rds_id}): session={ctx}")
    rds = session.client("rds")
    # Try as cluster first
    try:
        resp = rds.describe_db_clusters(DBClusterIdentifier=rds_id)
        members = resp["DBClusters"][0].get("DBClusterMembers", [])
        if members:
            # Sort writer first
            members.sort(key=lambda m: 0 if m.get("IsClusterWriter") else 1)
            resolved = [
                {
                    "id": m["DBInstanceIdentifier"],
                    "is_writer": bool(m.get("IsClusterWriter")),
                    "role": "writer" if m.get("IsClusterWriter") else "reader",
                }
                for m in members
            ]
            logger.info(f"PI resolved cluster {identifier} -> {resolved}")
            return resolved
    except Exception as e:
        logger.warning(f"PI cluster resolve failed for {identifier}: {e}")
    # Try as instance
    try:
        resp = rds.describe_db_instances(DBInstanceIdentifier=rds_id)
        if resp["DBInstances"]:
            iid = resp["DBInstances"][0]["DBInstanceIdentifier"]
            logger.info(f"PI resolved instance {identifier} -> {iid}")
            return [{"id": iid, "is_writer": True, "role": "writer"}]
    except Exception as e:
        logger.warning(f"PI instance resolve failed for {identifier}: {e}")
    logger.warning(f"PI could not resolve {identifier} — returning as-is")
    return [{"id": rds_id, "is_writer": True, "role": "writer"}]


def _role_focus() -> str:
    """The request's role focus ('' | 'writer' | 'reader' | 'instance:<id>'), read
    lazily like pg_client._role_focus; '' where model_resolver is absent."""
    try:
        from model_resolver import CURRENT_ROLE_FOCUS
        return (CURRENT_ROLE_FOCUS.get() or "").strip().lower()
    except Exception:
        return ""


def _pick_target(instance_ids: list, member: str = "", cluster: str = ""):
    """``(target, note)``: which member Performance Insights reads.

    1. An explicit ``member`` (a tool's ``member_instance_id``) must be one of the
       RDS-resolved members; otherwise ``(None, error text)``.
    2. Else the conversation's role focus: ``instance:<id>`` is that member (the writer,
       with a note, when it is not a current member); ``reader`` is the first reader by
       id, with a note that the reader endpoint round-robins; ``writer`` or none is the
       writer.
    Roles come from the live ``describe_db_clusters``, so they hold right after a
    failover."""
    writer = next((i for i in instance_ids if i.get("is_writer")), instance_ids[0])
    by_id = {i["id"]: i for i in instance_ids}
    if member:
        if member in by_id:
            return by_id[member], ""
        return None, f"member {member} is not in cluster {cluster}"
    focus = _role_focus()
    if focus.startswith("instance:"):
        mid = focus[len("instance:"):]
        if mid in by_id:
            return by_id[mid], ""
        return writer, f"focused instance {mid} is not a current member; showing the writer"
    if focus == "reader":
        readers = sorted((i for i in instance_ids if not i.get("is_writer")), key=lambda i: i["id"])
        if readers:
            return readers[0], (f"the reader endpoint round-robins; Performance Insights shows "
                                f"{readers[0]['id']}")
        return writer, "the cluster has no reader; showing the writer"
    return writer, ""


def _window_from_metric_list(metric_list):
    """Observed data window across a PI ``MetricList`` response.

    Dedupes datapoint timestamps across every metric query, then delegates to the
    shared ``_data_window`` so the OBSERVED span is reported the same way as for
    CloudWatch metrics (distinct from the requested lookback)."""
    stamps = {
        dp["Timestamp"].isoformat()
        for mq in (metric_list or [])
        for dp in mq.get("DataPoints", [])
        if dp.get("Timestamp") is not None
    }
    return _data_window([{"timestamp": t} for t in sorted(stamps)])


@tool
def get_db_load(db_instance_id: str, hours: int = 1,
                period: int = 300, member_instance_id: str = "") -> dict:
    """Get database load (Average Active Sessions) from Performance Insights.

    Shows overall DB load and CPU load over time. A load > vCPU count means
    the database is CPU-constrained.

    Args:
        db_instance_id: The database alias from [DATABASE SCOPE] (e.g. db-1)
        hours: How many hours of data to fetch (default: 1)
        period: Data point interval in seconds (default: 300)
        member_instance_id: For a DB cluster, the member instance to read (must be a
            current member). Empty follows the conversation's focus: an instance focus
            reads that instance, a reader focus the first reader, otherwise the writer.

    Returns:
        DB load and DB load CPU time series, the member read (db_instance_id, role) and
        a focus_note when the choice needs explaining
    """
    try:
        pi = _get_pi_client(identifier=db_instance_id)
        instance_ids = _resolve_instance_ids(db_instance_id)
        target, note = _pick_target(instance_ids, member_instance_id, db_instance_id)
        if target is None:
            return {"error": note, "db_instance_id": db_instance_id,
                    "members": [i["id"] for i in instance_ids]}
        target_id = target["id"]
        resource_id = _get_resource_arn(target_id, identifier=db_instance_id)
        logger.info(f"get_db_load: cluster={db_instance_id}, target={target_id}, resource_id={resource_id}")
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)

        resp = pi.get_resource_metrics(
            ServiceType="RDS",
            Identifier=resource_id,
            MetricQueries=[
                {"Metric": "db.load.avg"},
                {"Metric": "db.load.avg", "GroupBy": {"Group": "db.wait_event_type"}},
            ],
            StartTime=start,
            EndTime=end,
            PeriodInSeconds=period,
        )

        results = []
        for mq in resp.get("MetricList", []):
            metric_key = mq.get("Key", {})
            data_points = []
            for dp in mq.get("DataPoints", []):
                data_points.append({
                    "timestamp": dp["Timestamp"].isoformat(),
                    "value": round(dp.get("Value", 0), 4),
                })
            results.append({
                "metric": metric_key.get("Metric"),
                "dimensions": metric_key.get("Dimensions", {}),
                "data_points": data_points,
            })

        return {
            "db_instance_id": target_id,
            "role": target["role"],
            "is_writer": target["is_writer"],
            "cluster_id": db_instance_id if target_id != db_instance_id else None,
            "requested_hours": hours,
            "data_window": _window_from_metric_list(resp.get("MetricList", [])),
            "metrics": results,
            **({"focus_note": note} if note else {}),
        }
    except Exception as e:
        ctx = _error_context(db_instance_id)
        instance_ids = []
        rid = "unresolved"
        try:
            instance_ids = _resolve_instance_ids(db_instance_id)
            if instance_ids:
                picked, _note = _pick_target(instance_ids, member_instance_id, db_instance_id)
                rid = _get_resource_arn((picked or instance_ids[0])["id"], identifier=db_instance_id)
        except Exception:
            pass
        return {
            "error": f"PI get_db_load failed: {str(e)}",
            "db_instance_id": db_instance_id,
            "resolved_instances": [i["id"] for i in instance_ids],
            "session_context": ctx,
            "resource_id_used": rid,
        }


@tool
def get_top_sql(db_instance_id: str, hours: int = 1,
                period: int = 300, limit: int = 10, member_instance_id: str = "") -> dict:
    """Get top SQL statements by database load from Performance Insights.

    Args:
        db_instance_id: The database alias from [DATABASE SCOPE] (e.g. db-1)
        hours: How many hours of data to fetch (default: 1)
        period: Data point interval in seconds (default: 300)
        limit: Number of top SQL statements to return (default: 10)
        member_instance_id: For a DB cluster, the member instance to read (must be a
            current member). Empty follows the conversation's focus.

    Returns:
        Top SQL statements ranked by load contribution, and the member read
    """
    try:
        pi = _get_pi_client(identifier=db_instance_id)
        instance_ids = _resolve_instance_ids(db_instance_id)
        target, note = _pick_target(instance_ids, member_instance_id, db_instance_id)
        if target is None:
            return {"error": note, "db_instance_id": db_instance_id,
                    "members": [i["id"] for i in instance_ids]}
        target_id = target["id"]
        resource_id = _get_resource_arn(target_id, identifier=db_instance_id)
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)

        resp = pi.get_resource_metrics(
            ServiceType="RDS",
            Identifier=resource_id,
            MetricQueries=[
                {
                    "Metric": "db.load.avg",
                    "GroupBy": {"Group": "db.sql", "Limit": limit},
                },
            ],
            StartTime=start,
            EndTime=end,
            PeriodInSeconds=period,
        )

        sql_entries = []
        for mq in resp.get("MetricList", []):
            dims = mq.get("Key", {}).get("Dimensions", {})
            if "db.sql.statement" in dims or "db.sql.id" in dims:
                total_load = sum(
                    dp.get("Value", 0) for dp in mq.get("DataPoints", [])
                )
                sql_entries.append({
                    "sql_id": dims.get("db.sql.id", ""),
                    "sql_text": dims.get("db.sql.statement", "")[:500],
                    "db_name": dims.get("db.sql.db_id", ""),
                    "total_load": round(total_load, 4),
                    "avg_load": round(
                        total_load / max(len(mq.get("DataPoints", [])), 1), 4
                    ),
                })

        sql_entries.sort(key=lambda x: x["total_load"], reverse=True)

        return {
            "db_instance_id": target_id,
            "role": target["role"],
            "is_writer": target["is_writer"],
            "requested_hours": hours,
            "data_window": _window_from_metric_list(resp.get("MetricList", [])),
            "top_sql": sql_entries[:limit],
            **({"focus_note": note} if note else {}),
        }
    except Exception as e:
        ctx = _error_context(db_instance_id)
        return {
            "error": f"PI get_top_sql failed: {str(e)}",
            "db_instance_id": db_instance_id,
            "session_context": ctx,
        }
@tool
def get_top_waits(db_instance_id: str, hours: int = 1,
                  period: int = 300, member_instance_id: str = "") -> dict:
    """Get top wait events by database load from Performance Insights.

    Wait events reveal what the database is spending time on — CPU, I/O,
    lock waits, network, etc.

    Args:
        db_instance_id: The database alias from [DATABASE SCOPE] (e.g. db-1)
        hours: How many hours of data to fetch (default: 1)
        period: Data point interval in seconds (default: 300)
        member_instance_id: For a DB cluster, the member instance to read (must be a
            current member). Empty follows the conversation's focus.

    Returns:
        Wait events ranked by load contribution, and the member read
    """
    try:
        pi = _get_pi_client(identifier=db_instance_id)
        instance_ids = _resolve_instance_ids(db_instance_id)
        target, note = _pick_target(instance_ids, member_instance_id, db_instance_id)
        if target is None:
            return {"error": note, "db_instance_id": db_instance_id,
                    "members": [i["id"] for i in instance_ids]}
        target_id = target["id"]
        resource_id = _get_resource_arn(target_id, identifier=db_instance_id)
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)

        resp = pi.get_resource_metrics(
            ServiceType="RDS",
            Identifier=resource_id,
            MetricQueries=[
                {
                    "Metric": "db.load.avg",
                    "GroupBy": {"Group": "db.wait_event", "Limit": 20},
                },
            ],
            StartTime=start,
            EndTime=end,
            PeriodInSeconds=period,
        )

        waits = []
        for mq in resp.get("MetricList", []):
            dims = mq.get("Key", {}).get("Dimensions", {})
            if "db.wait_event.name" in dims:
                total_load = sum(
                    dp.get("Value", 0) for dp in mq.get("DataPoints", [])
                )
                waits.append({
                    "wait_event": dims.get("db.wait_event.name", ""),
                    "wait_type": dims.get("db.wait_event.type", ""),
                    "total_load": round(total_load, 4),
                    "avg_load": round(
                        total_load / max(len(mq.get("DataPoints", [])), 1), 4
                    ),
                })

        waits.sort(key=lambda x: x["total_load"], reverse=True)

        return {
            "db_instance_id": target_id,
            "role": target["role"],
            "is_writer": target["is_writer"],
            "requested_hours": hours,
            "data_window": _window_from_metric_list(resp.get("MetricList", [])),
            "top_waits": waits,
            **({"focus_note": note} if note else {}),
        }
    except Exception as e:
        ctx = _error_context(db_instance_id)
        return {
            "error": f"PI get_top_waits failed: {str(e)}",
            "db_instance_id": db_instance_id,
            "session_context": ctx,
        }
