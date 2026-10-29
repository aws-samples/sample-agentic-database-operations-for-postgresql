# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""CloudWatch Metrics tools — CPU utilization, IOPS, connections, memory, disk, network,
replica lag.

These tools are engine-agnostic and work with any RDS/Aurora instance.
They query CloudWatch metrics using the `cloudwatch` boto3 client.
"""

import logging
from datetime import datetime, timedelta, timezone

from strands import tool

logger = logging.getLogger("cloudwatch-tools")


def _get_cw_client(identifier: str = None):
    """Get a CloudWatch client via the space-scoped, cross-account session.

    A falsy identifier fails closed rather than returning an account-level client: the
    space resolver would otherwise be skipped and metrics read outside the caller's
    space. A client is only ever obtained by resolving a specific database.
    """
    if not identifier:
        raise PermissionError(
            "A database identifier is required for CloudWatch metrics; refusing an unscoped client."
        )
    from tools.aws.session_factory import get_session, get_session_context
    session = get_session(identifier=identifier)
    ctx = get_session_context(session)
    logger.info(f"CW client for {identifier}: context={ctx}")
    return session.client("cloudwatch")


def _error_context(identifier: str) -> dict:
    """The session context for an error result. The resolution that failed (an ambiguous
    name, a database outside the space) must not raise again from the error path."""
    from tools.aws.session_factory import get_session, get_session_context
    try:
        return get_session_context(get_session(identifier=identifier))
    except Exception:  # noqa: BLE001 - the error result already says what failed
        return {"type": "unresolved"}


def _resolve_instance_ids(identifier: str) -> list[dict]:
    """Resolve an identifier to role-bearing cluster members, writer first.

    Returns a list of dicts ``{"id", "is_writer", "role"}`` sorted writer-first.
    The role is derived from the ``IsClusterWriter`` flag on the cluster describe. A
    standalone/instance
    target — or an identifier we cannot resolve — is treated as its own primary
    (a single ``writer`` dict) so callers always have an unambiguous anchor.
    """
    from tools.aws.session_factory import get_session, resolve_target
    # The handle is the registry id (or a display name); RDS knows the database only by
    # its own identifier, which the registry record carries.
    rds_id = resolve_target(identifier)["rds_id"]
    session = get_session(identifier=identifier)
    rds = session.client("rds")
    # Try as cluster first
    try:
        resp = rds.describe_db_clusters(DBClusterIdentifier=rds_id)
        members = resp["DBClusters"][0].get("DBClusterMembers", [])
        if members:
            members.sort(key=lambda m: 0 if m.get("IsClusterWriter") else 1)
            resolved = [
                {
                    "id": m["DBInstanceIdentifier"],
                    "is_writer": bool(m.get("IsClusterWriter")),
                    "role": "writer" if m.get("IsClusterWriter") else "reader",
                }
                for m in members
            ]
            logger.info(f"Resolved cluster {rds_id} -> members: {resolved}")
            return resolved
    except Exception as e:
        logger.info(f"Not a cluster ({rds_id}): {e}")
    # Try as instance
    try:
        resp = rds.describe_db_instances(DBInstanceIdentifier=rds_id)
        if resp["DBInstances"]:
            iid = resp["DBInstances"][0]["DBInstanceIdentifier"]
            return [{"id": iid, "is_writer": True, "role": "writer"}]
    except Exception:
        pass
    return [{"id": rds_id, "is_writer": True, "role": "writer"}]


def _primary_instance_id(members: list[dict]) -> str:
    """The writer's id (the anchor) among resolved members; fall back to first."""
    for m in members:
        if m.get("is_writer"):
            return m["id"]
    return members[0]["id"] if members else ""


def _data_window(points):
    """Observed data window from returned metric datapoints.

    Returns ``{"start", "end", "span_minutes", "point_count"}`` derived from the
    actual datapoints' ISO ``timestamp`` fields, or ``None`` when there are none.
    This is the OBSERVED span — distinct from the REQUESTED lookback — so a run
    that only produced a few minutes of data is never narrated as hours.
    """
    if not points:
        return None
    stamps = sorted(p["timestamp"] for p in points if p.get("timestamp"))
    if not stamps:
        return None
    start, end = stamps[0], stamps[-1]
    try:
        span_minutes = round(
            (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60
        )
    except Exception:
        span_minutes = None
    return {
        "start": start,
        "end": end,
        "span_minutes": span_minutes,
        "point_count": len(points),
    }


def _get_rds_metrics(db_instance_id: str, metric_name: str,
                     period: int = 300, hours: int = 1,
                     stat: str = "Average", identifier: str = None) -> list:
    """Fetch a single RDS metric from CloudWatch."""
    cw = _get_cw_client(identifier=identifier or db_instance_id)
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)

    resp = cw.get_metric_statistics(
        Namespace="AWS/RDS",
        MetricName=metric_name,
        Dimensions=[{"Name": "DBInstanceIdentifier", "Value": db_instance_id}],
        StartTime=start,
        EndTime=end,
        Period=period,
        Statistics=[stat],
    )
    points = resp.get("Datapoints", [])
    points.sort(key=lambda p: p["Timestamp"])
    return [
        {"timestamp": p["Timestamp"].isoformat(), "value": round(p[stat], 4)}
        for p in points
    ]


@tool
def get_cpu_utilization(db_instance_id: str, hours: int = 1,
                        period: int = 300) -> dict:
    """Get CPU utilization percentage for an RDS/Aurora instance from CloudWatch.

    Accepts either an RDS instance ID or an Aurora cluster ID (will resolve
    to member instances automatically).

    Args:
        db_instance_id: The database alias from [DATABASE SCOPE] (e.g. db-1)
        hours: How many hours of data to fetch (default: 1)
        period: Data point interval in seconds (default: 300 = 5 min)

    Returns:
        CPU utilization data points with timestamps and percentages
    """
    try:
        from tools.aws.session_factory import get_session, get_session_context
        ctx = get_session_context(get_session(identifier=db_instance_id))
        members = _resolve_instance_ids(db_instance_id)
        all_results = []
        for m in members:
            points = _get_rds_metrics(m["id"], "CPUUtilization",
                                      period=period, hours=hours,
                                      identifier=db_instance_id)
            summary = {}
            if points:
                vals = [p["value"] for p in points]
                summary = {
                    "current": vals[-1],
                    "average": round(sum(vals) / len(vals), 2),
                    "max": round(max(vals), 2),
                    "min": round(min(vals), 2),
                }
            all_results.append({
                "db_instance_id": m["id"],
                "role": m["role"],
                "is_writer": m["is_writer"],
                "metric": "CPUUtilization",
                "unit": "Percent",
                "period_seconds": period,
                "requested_hours": hours,
                "data_points": points,
                "data_window": _data_window(points),
                "summary": summary,
            })
        if len(all_results) == 1:
            return {**all_results[0], "_context": ctx}
        return {
            "instances": all_results,
            "cluster_id": db_instance_id,
            "primary_instance": _primary_instance_id(members),
            "_context": ctx,
        }
    except Exception as e:
        ctx = _error_context(db_instance_id)
        resolved = []
        try:
            resolved = [m["id"] for m in _resolve_instance_ids(db_instance_id)]
        except Exception:
            pass
        return {
            "error": f"get_cpu_utilization failed: {str(e)}",
            "db_instance_id": db_instance_id,
            "resolved_instances": resolved,
            "session_context": ctx,
        }


@tool
def get_db_connections(db_instance_id: str, hours: int = 1,
                       period: int = 300) -> dict:
    """Get the number of database connections for an RDS/Aurora instance.

    Accepts either an RDS instance ID or an Aurora cluster ID.

    Args:
        db_instance_id: The database alias from [DATABASE SCOPE] (e.g. db-1)
        hours: How many hours of data to fetch (default: 1)
        period: Data point interval in seconds (default: 300)

    Returns:
        Connection count data points
    """
    try:
        members = _resolve_instance_ids(db_instance_id)
        all_results = []
        for m in members:
            points = _get_rds_metrics(m["id"], "DatabaseConnections",
                                      period=period, hours=hours,
                                      identifier=db_instance_id)
            summary = {}
            if points:
                vals = [p["value"] for p in points]
                summary = {"current": vals[-1], "max": max(vals), "min": min(vals)}
            all_results.append({
                "db_instance_id": m["id"], "role": m["role"], "is_writer": m["is_writer"],
                "metric": "DatabaseConnections", "requested_hours": hours,
                "unit": "Count", "data_points": points,
                "data_window": _data_window(points), "summary": summary,
            })
        if len(all_results) == 1:
            return all_results[0]
        return {"instances": all_results, "cluster_id": db_instance_id,
                "primary_instance": _primary_instance_id(members)}
    except Exception as e:
        return {"error": str(e)}


@tool
def get_freeable_memory(db_instance_id: str, hours: int = 1,
                        period: int = 300) -> dict:
    """Get freeable memory for an RDS/Aurora instance.

    Accepts either an RDS instance ID or an Aurora cluster ID.

    Args:
        db_instance_id: The database alias from [DATABASE SCOPE] (e.g. db-1)
        hours: How many hours of data to fetch (default: 1)
        period: Data point interval in seconds (default: 300)

    Returns:
        Freeable memory in bytes with human-readable conversion
    """
    try:
        members = _resolve_instance_ids(db_instance_id)
        all_results = []
        for m in members:
            points = _get_rds_metrics(m["id"], "FreeableMemory",
                                      period=period, hours=hours,
                                      identifier=db_instance_id)
            for p in points:
                p["value_mb"] = round(p["value"] / (1024 * 1024), 1)
            summary = {}
            if points:
                vals = [p["value_mb"] for p in points]
                summary = {"current_mb": vals[-1], "min_mb": min(vals), "max_mb": max(vals)}
            all_results.append({
                "db_instance_id": m["id"], "role": m["role"], "is_writer": m["is_writer"],
                "metric": "FreeableMemory", "requested_hours": hours,
                "unit": "Bytes (MB shown)", "data_points": points,
                "data_window": _data_window(points), "summary": summary,
            })
        if len(all_results) == 1:
            return all_results[0]
        return {"instances": all_results, "cluster_id": db_instance_id,
                "primary_instance": _primary_instance_id(members)}
    except Exception as e:
        return {"error": str(e)}


@tool
def get_iops(db_instance_id: str, hours: int = 1, period: int = 300) -> dict:
    """Get read/write IOPS and latency for an RDS/Aurora instance.

    Accepts either an RDS instance ID or an Aurora cluster ID.

    Args:
        db_instance_id: The database alias from [DATABASE SCOPE] (e.g. db-1)
        hours: How many hours of data to fetch (default: 1)
        period: Data point interval in seconds (default: 300)

    Returns:
        Read IOPS, Write IOPS, Read Latency, Write Latency data
    """
    try:
        members = _resolve_instance_ids(db_instance_id)
        all_results = []
        for m in members:
            series = {}
            for metric_name in ["ReadIOPS", "WriteIOPS", "ReadLatency", "WriteLatency"]:
                series[metric_name] = _get_rds_metrics(m["id"], metric_name,
                                                        period=period, hours=hours,
                                                        identifier=db_instance_id)
            # Observed window from whichever IOPS series has points (write, else read).
            iops_points = series["WriteIOPS"] or series["ReadIOPS"]
            all_results.append({
                "db_instance_id": m["id"], "role": m["role"], "is_writer": m["is_writer"],
                "requested_hours": hours,
                "read_iops": series["ReadIOPS"], "write_iops": series["WriteIOPS"],
                "read_latency_seconds": series["ReadLatency"],
                "write_latency_seconds": series["WriteLatency"],
                "data_window": _data_window(iops_points),
            })
        if len(all_results) == 1:
            return all_results[0]
        return {"instances": all_results, "cluster_id": db_instance_id,
                "primary_instance": _primary_instance_id(members)}
    except Exception as e:
        return {"error": str(e)}


@tool
def get_free_storage_space(db_instance_id: str, hours: int = 6,
                           period: int = 900) -> dict:
    """Get free storage space for an RDS instance (not applicable to Aurora serverless).

    Accepts either an RDS instance ID or an Aurora cluster ID.

    Args:
        db_instance_id: The database alias from [DATABASE SCOPE] (e.g. db-1)
        hours: How many hours of data to fetch (default: 6)
        period: Data point interval in seconds (default: 900)

    Returns:
        Free storage space in bytes/GB
    """
    try:
        members = _resolve_instance_ids(db_instance_id)
        all_results = []
        for m in members:
            points = _get_rds_metrics(m["id"], "FreeStorageSpace",
                                      period=period, hours=hours,
                                      identifier=db_instance_id)
            for p in points:
                p["value_gb"] = round(p["value"] / (1024 ** 3), 2)
            summary = {}
            if points:
                vals = [p["value_gb"] for p in points]
                summary = {"current_gb": vals[-1], "min_gb": min(vals)}
            all_results.append({
                "db_instance_id": m["id"], "role": m["role"], "is_writer": m["is_writer"],
                "metric": "FreeStorageSpace", "requested_hours": hours,
                "data_points": points, "data_window": _data_window(points),
                "summary": summary,
            })
        if len(all_results) == 1:
            return all_results[0]
        return {"instances": all_results, "cluster_id": db_instance_id,
                "primary_instance": _primary_instance_id(members)}
    except Exception as e:
        return {"error": str(e)}


@tool
def get_network_throughput(db_instance_id: str, hours: int = 1,
                           period: int = 300) -> dict:
    """Get network receive/transmit throughput for an RDS/Aurora instance.

    Accepts either an RDS instance ID or an Aurora cluster ID.

    Args:
        db_instance_id: The database alias from [DATABASE SCOPE] (e.g. db-1)
        hours: How many hours of data to fetch (default: 1)
        period: Data point interval in seconds (default: 300)

    Returns:
        Network throughput in bytes/second
    """
    try:
        members = _resolve_instance_ids(db_instance_id)
        all_results = []
        for m in members:
            rx = _get_rds_metrics(m["id"], "NetworkReceiveThroughput",
                                  period=period, hours=hours,
                                  identifier=db_instance_id)
            tx = _get_rds_metrics(m["id"], "NetworkTransmitThroughput",
                                  period=period, hours=hours,
                                  identifier=db_instance_id)
            all_results.append({
                "db_instance_id": m["id"], "role": m["role"], "is_writer": m["is_writer"],
                "requested_hours": hours,
                "receive_bytes_per_sec": rx, "transmit_bytes_per_sec": tx,
                "data_window": _data_window(rx or tx),
            })
        if len(all_results) == 1:
            return all_results[0]
        return {"instances": all_results, "cluster_id": db_instance_id,
                "primary_instance": _primary_instance_id(members)}
    except Exception as e:
        return {"error": str(e)}


def _replica_lag_targets(identifier: str) -> list[dict]:
    """Members of ``identifier`` with the replica-lag metric each one publishes.

    One describe decides it: an Aurora cluster's readers publish ``AuroraReplicaLag``
    (milliseconds; storage-level replication), an RDS Multi-AZ DB cluster's readers and
    an RDS read replica publish ``ReplicaLag`` (seconds; streaming replication). Writers
    and standalone primaries have no replica lag (``metric`` None). Writer first."""
    from tools.aws.session_factory import get_session, resolve_target
    rds_id = resolve_target(identifier)["rds_id"]
    rds = get_session(identifier=identifier).client("rds")
    try:
        cluster = rds.describe_db_clusters(DBClusterIdentifier=rds_id)["DBClusters"][0]
        members = list(cluster.get("DBClusterMembers") or [])
        if members:
            aurora = (cluster.get("Engine") or "").startswith("aurora")
            metric, unit = ("AuroraReplicaLag", "Milliseconds") if aurora else ("ReplicaLag", "Seconds")
            members.sort(key=lambda m: 0 if m.get("IsClusterWriter") else 1)
            return [{"id": m["DBInstanceIdentifier"], "is_writer": bool(m.get("IsClusterWriter")),
                     "role": "writer" if m.get("IsClusterWriter") else "reader",
                     "metric": None if m.get("IsClusterWriter") else metric,
                     "unit": None if m.get("IsClusterWriter") else unit}
                    for m in members]
    except Exception as e:
        logger.info(f"Not a cluster ({identifier}): {e}")
    try:
        inst = rds.describe_db_instances(DBInstanceIdentifier=rds_id)["DBInstances"][0]
        source = inst.get("ReadReplicaSourceDBInstanceIdentifier") or ""
        if source:
            return [{"id": inst["DBInstanceIdentifier"], "is_writer": False, "role": "replica",
                     "metric": "ReplicaLag", "unit": "Seconds", "replica_of": source}]
        return [{"id": inst["DBInstanceIdentifier"], "is_writer": True, "role": "writer",
                 "metric": None, "unit": None}]
    except Exception:
        pass
    return [{"id": rds_id, "is_writer": True, "role": "writer", "metric": None, "unit": None}]


@tool
def get_replica_lag(db_instance_id: str, hours: int = 1, period: int = 300) -> dict:
    """Get replica lag for the readers of an Aurora cluster, an RDS Multi-AZ DB cluster,
    or an RDS read replica.

    Aurora readers report ``AuroraReplicaLag`` (milliseconds) and never appear in
    pg_stat_replication; RDS read replicas and Multi-AZ DB cluster readers report
    ``ReplicaLag`` (seconds). Writers and standalone primaries are listed with no metric.

    Args:
        db_instance_id: The database alias from [DATABASE SCOPE] (e.g. db-1)
        hours: How many hours of data to fetch (default: 1)
        period: Data point interval in seconds (default: 300)

    Returns:
        Per member: role, is_writer, metric, unit, data points, observed window, summary
    """
    try:
        from tools.aws.session_factory import get_session, get_session_context
        ctx = get_session_context(get_session(identifier=db_instance_id))
        targets = _replica_lag_targets(db_instance_id)
        all_results = []
        for t in targets:
            result = {"db_instance_id": t["id"], "role": t["role"], "is_writer": t["is_writer"],
                      "metric": t["metric"], "unit": t["unit"], "period_seconds": period,
                      "requested_hours": hours}
            if not t["metric"]:
                result.update({"note": "writer: no replica lag", "data_points": [],
                               "data_window": None, "summary": {}})
            else:
                points = _get_rds_metrics(t["id"], t["metric"], period=period, hours=hours,
                                          identifier=db_instance_id)
                summary = {}
                if points:
                    vals = [p["value"] for p in points]
                    summary = {"current": vals[-1], "average": round(sum(vals) / len(vals), 2),
                               "max": round(max(vals), 2)}
                result.update({"data_points": points, "data_window": _data_window(points),
                               "summary": summary})
                if t.get("replica_of"):
                    result["replica_of"] = t["replica_of"]
            all_results.append(result)
        if len(all_results) == 1:
            return {**all_results[0], "_context": ctx}
        return {"instances": all_results, "cluster_id": db_instance_id,
                "primary_instance": _primary_instance_id(targets), "_context": ctx}
    except Exception as e:
        return {"error": f"get_replica_lag failed: {str(e)}", "db_instance_id": db_instance_id}


@tool
def get_cloudwatch_alarms(db_instance_id: str) -> dict:
    """Get all CloudWatch alarms related to an RDS/Aurora instance.

    Args:
        db_instance_id: The database alias from [DATABASE SCOPE] (e.g. db-1)

    Returns:
        List of alarms with state, metric, and threshold info
    """
    try:
        cw = _get_cw_client(identifier=db_instance_id)
        from tools.aws.session_factory import resolve_target
        rds_id = resolve_target(db_instance_id)["rds_id"]
        # CPU alarms are per instance: a cluster's are on its members' dimensions.
        alarms = []
        for m in _resolve_instance_ids(db_instance_id):
            resp = cw.describe_alarms_for_metric(
                Namespace="AWS/RDS",
                MetricName="CPUUtilization",
                Dimensions=[{"Name": "DBInstanceIdentifier", "Value": m["id"]}],
            )
            for a in resp.get("MetricAlarms", []):
                if a["AlarmName"] not in [x["AlarmName"] for x in alarms]:
                    alarms.append(a)

        # Also check for any alarms with this database's RDS name in the name
        all_alarms = cw.describe_alarms(
            AlarmNamePrefix=rds_id,
            MaxRecords=50,
        )
        for a in all_alarms.get("MetricAlarms", []):
            if a["AlarmName"] not in [x["AlarmName"] for x in alarms]:
                alarms.append(a)

        return {
            "db_instance_id": db_instance_id,
            "alarms": [
                {
                    "name": a["AlarmName"],
                    "state": a["StateValue"],
                    "metric": a.get("MetricName"),
                    "threshold": a.get("Threshold"),
                    "comparison": a.get("ComparisonOperator"),
                    "period": a.get("Period"),
                    "eval_periods": a.get("EvaluationPeriods"),
                    "description": a.get("AlarmDescription", ""),
                }
                for a in alarms
            ],
            "count": len(alarms),
        }
    except Exception as e:
        return {"error": str(e)}
