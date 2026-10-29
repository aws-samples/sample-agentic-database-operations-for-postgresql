# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Topology builder — constructs a resource graph from existing DynamoDB data.

No AWS API calls needed. All data comes from the database registry and
network registry populated during Phase 4 configuration.
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional

from tools.aws.dynamodb_client import db_registry
from tools.aws.teardown_claim import teardown_stalled
from tools.aws.tenancy import record_in_space
from tools.aws.network_registry import NetworkRegistry
from tools.aws.network_client import network_configurator
from tools.aws.s3_client import report_storage
from tools.aws.recommendations_client import recommendations_registry

logger = logging.getLogger("topology-builder")

network_registry = NetworkRegistry()

# AWS services the agent connects to for each database
AGENT_SERVICES = [
    {"id": "svc-cloudwatch", "type": "aws_service", "label": "CloudWatch Metrics",
     "service": "cloudwatch", "icon": "cloudwatch",
     "metadata": {"description": "CPU, IOPS, connections, storage metrics"}},
    {"id": "svc-pi", "type": "aws_service", "label": "Performance Insights",
     "service": "pi", "icon": "performance_insights",
     "metadata": {"description": "DB load, top SQL, wait events"}},
    {"id": "svc-cloudtrail", "type": "aws_service", "label": "CloudTrail",
     "service": "cloudtrail", "icon": "cloudtrail",
     "metadata": {"description": "API calls, config changes, audit trail"}},
    {"id": "svc-cwlogs", "type": "aws_service", "label": "CloudWatch Logs",
     "service": "logs", "icon": "cloudwatch_logs",
     "metadata": {"description": "Database error logs, slow query logs"}},
    {"id": "svc-bedrock", "type": "aws_service", "label": "Amazon Bedrock",
     "service": "bedrock", "icon": "bedrock",
     "metadata": {"description": "AI model for analysis and recommendations"}},
]

# Sub-agents within the orchestrator
SUB_AGENTS = [
    {
        "id": "agent-orchestrator",
        "type": "sub_agent",
        "label": "Orchestrator Agent",
        "agent_type": "orchestrator",
        "metadata": {
            "description": "Handles chat interactions. Routes questions to specialist agents and correlates findings across all data sources.",
            "model": "Claude (Amazon Bedrock)",
        },
        "connects_to": ["svc-bedrock"],
        "tools": [
            {"name": "current_time", "description": "Get the current UTC time for time-based queries."},
            {"name": "list_monitored_databases", "description": "List all registered database instances with configuration, health scores, and connection details."},
            {"name": "get_report_analysis", "description": "Read the AI-generated analysis from the most recent health scan report."},
            {"name": "observability_agent", "description": "Delegate infrastructure monitoring — CloudWatch metrics, Performance Insights, CloudTrail, logs."},
            {"name": "postgres_agent", "description": "Delegate PostgreSQL internal analysis — queries, locks, bloat, indexes, replication, configuration."},
            {"name": "docs_agent", "description": "Search official documentation to verify recommendations and find version-specific best practices."},
        ],
    },
    {
        "id": "agent-investigation",
        "type": "sub_agent",
        "label": "Investigation Agent",
        "agent_type": "investigation",
        "metadata": {
            "description": "Conducts structured multi-step root cause analysis. Systematically checks metrics, Performance Insights, CloudTrail, and database internals, then correlates findings into actionable recommendations.",
            "model": "Claude (Amazon Bedrock)",
        },
        "connects_to": ["svc-bedrock"],
        "tools": [
            {"name": "current_time", "description": "Get the current UTC time."},
            {"name": "list_monitored_databases", "description": "List all registered database instances."},
            {"name": "get_report_analysis", "description": "Read the latest health scan report analysis."},
            {"name": "observability_agent", "description": "Delegate CloudWatch metrics, Performance Insights, CloudTrail, logs."},
            {"name": "postgres_agent", "description": "Delegate PostgreSQL internal analysis — health, queries, locks, bloat."},
            {"name": "docs_agent", "description": "Verify recommendations against official AWS and PostgreSQL documentation."},
        ],
    },
    {
        "id": "agent-observability",
        "type": "sub_agent",
        "label": "Observability Agent",
        "agent_type": "observability",
        "metadata": {
            "description": "AWS infrastructure monitoring specialist. Queries CloudWatch, Performance Insights, CloudTrail, and CloudWatch Logs to analyze database health from the outside.",
            "model": "Claude (Amazon Bedrock)",
        },
        "connects_to": ["svc-cloudwatch", "svc-pi", "svc-cloudtrail", "svc-cwlogs"],
        "tools": [
            {"name": "get_cpu_utilization", "description": "Get CPU utilization percentage over a time range from CloudWatch."},
            {"name": "get_db_connections", "description": "Get active database connection count over time."},
            {"name": "get_freeable_memory", "description": "Get available memory (freeable) in bytes over time."},
            {"name": "get_iops", "description": "Get read/write IOPS metrics from CloudWatch."},
            {"name": "get_free_storage_space", "description": "Get available storage space in bytes."},
            {"name": "get_network_throughput", "description": "Get network receive/transmit throughput."},
            {"name": "get_replica_lag", "description": "Get replica lag (AuroraReplicaLag / ReplicaLag) per reader."},
            {"name": "get_cloudwatch_alarms", "description": "List active CloudWatch alarms for a database instance."},
            {"name": "get_db_load", "description": "Get database load (Average Active Sessions) from Performance Insights."},
            {"name": "get_top_sql", "description": "Get top SQL statements by load from Performance Insights."},
            {"name": "get_top_waits", "description": "Get top wait events from Performance Insights."},
            {"name": "get_rds_events", "description": "Get recent RDS events from CloudTrail (failovers, reboots, etc)."},
            {"name": "get_rds_modifications", "description": "Get recent RDS configuration changes from CloudTrail."},
            {"name": "get_db_error_logs", "description": "Get recent database error log entries from CloudWatch Logs."},
            {"name": "query_db_logs", "description": "Run a custom CloudWatch Logs Insights query against database logs."},
        ],
    },
    {
        "id": "agent-postgres",
        "type": "sub_agent",
        "label": "PostgreSQL Agent",
        "agent_type": "postgres",
        "metadata": {
            "description": "PostgreSQL internal specialist. Connects directly to databases via SQL to analyze query performance, locks, bloat, indexes, replication, and configuration.",
            "model": "Claude (Amazon Bedrock)",
        },
        "connects_to": [],
        "tools": [
            {"name": "check_db_connectivity", "description": "Test database connectivity and return connection details."},
            {"name": "get_db_overview", "description": "Database version, size, uptime, extensions, and basic info."},
            {"name": "get_db_configuration", "description": "Key PostgreSQL configuration parameters and their values."},
            {"name": "get_security_audit", "description": "Audit database security — roles, permissions, SSL, password policies."},
            {"name": "get_setting_overrides", "description": "Show non-default configuration settings and their sources."},
            {"name": "get_current_activity", "description": "Currently running queries with duration, state, and wait events."},
            {"name": "get_connection_analysis", "description": "Connection count breakdown by state, user, application, and database."},
            {"name": "get_workload_characterization", "description": "Read/write ratio, transaction rates, and workload patterns."},
            {"name": "get_replication_status", "description": "Replication lag, WAL position, and replica status."},
            {"name": "get_wal_status", "description": "WAL generation rate, archiving status, and retention."},
            {"name": "get_logical_replication", "description": "Logical replication slots, subscriptions, and publication status."},
            {"name": "get_storage_analysis", "description": "Table sizes, database size, tablespace usage, and growth trends."},
            {"name": "get_checkpoint_stats", "description": "Checkpoint frequency, duration, and buffer write stats."},
            {"name": "get_bgwriter_stats", "description": "Background writer activity — buffers written, clean percentage."},
            {"name": "get_autovacuum_utilization", "description": "Autovacuum worker activity, dead tuples, and vacuum scheduling."},
            {"name": "get_connection_pooling_efficiency", "description": "Connection pool utilization and efficiency metrics."},
            {"name": "get_buffer_pool_stats", "description": "Shared buffer hit ratios for tables and indexes."},
            {"name": "get_lock_analysis", "description": "Active locks, blocked queries, lock waits, and deadlock potential."},
            {"name": "get_io_statistics", "description": "Table and index I/O stats — heap reads, index reads, cache hits."},
            {"name": "get_query_performance", "description": "Top queries by total time, calls, mean time from pg_stat_statements."},
            {"name": "get_query_plan", "description": "EXPLAIN ANALYZE for a specific query to show execution plan."},
            {"name": "get_transaction_stats", "description": "Transaction commit/rollback rates, deadlocks, conflicts."},
            {"name": "get_function_performance", "description": "Function call counts, total time, and self time."},
            {"name": "get_wait_event_analysis", "description": "Wait event breakdown — I/O, lock, CPU, buffer pin waits."},
            {"name": "get_session_duration_analysis", "description": "Session age distribution and long-running session detection."},
            {"name": "get_prepared_statement_analysis", "description": "Prepared statement usage and plan cache efficiency."},
            {"name": "get_temp_file_queries", "description": "Queries generating temporary files — sort/hash spills to disk."},
            {"name": "get_maintenance_status", "description": "Last vacuum/analyze times, dead tuple ratios per table."},
            {"name": "get_sequence_exhaustion", "description": "Sequence usage percentage and exhaustion risk."},
            {"name": "get_constraint_validation", "description": "Invalid or not-validated constraints that need attention."},
            {"name": "get_materialized_view_freshness", "description": "Materialized view refresh times and staleness."},
            {"name": "get_vacuum_stats_health", "description": "Vacuum health — transaction ID age, wraparound risk, freeze progress."},
            {"name": "get_index_statistics", "description": "Index usage rates, sizes, and scan counts."},
            {"name": "get_toast_status", "description": "TOAST table sizes and out-of-line storage analysis."},
            {"name": "get_partitioning_analysis", "description": "Partition sizes, row distribution, and partition pruning effectiveness."},
            {"name": "get_bloat_analysis", "description": "Table and index bloat estimation — wasted space, bloat ratio."},
            {"name": "get_unused_indexes", "description": "Indexes with zero scans that waste storage and slow writes."},
            {"name": "get_duplicate_indexes", "description": "Duplicate or overlapping indexes that can be consolidated."},
            {"name": "get_idle_connections", "description": "Idle connections consuming resources without activity."},
            {"name": "get_oversized_datatypes", "description": "Columns using larger datatypes than necessary."},
            {"name": "get_unused_tables", "description": "Tables with no sequential or index scans."},
            {"name": "get_fk_without_indexes", "description": "Foreign keys missing indexes — causes slow joins and deletes."},
            {"name": "get_trigger_analysis", "description": "Trigger inventory, execution frequency, and performance impact."},
            {"name": "get_sequential_scan_tables", "description": "Tables with high sequential scan ratios — missing index candidates."},
            {"name": "get_health_summary", "description": "Aggregated health score with severity-ranked findings across all sections."},
        ],
    },
    {
        "id": "agent-docs",
        "type": "sub_agent",
        "label": "Research Agent",
        "agent_type": "docs",
        "metadata": {
            "description": "Research specialist. Searches official AWS docs, PostgreSQL docs, and trusted sources to verify recommendations and find version-specific best practices.",
            "model": "Claude (Amazon Bedrock)",
        },
        "connects_to": ["svc-bedrock"],
        "tools": [
            {"name": "web_search", "description": "Managed AgentCore Web Search (via the Gateway MCP connector) — an AWS-operated web index; requires EnableWebSearch=true."},
            {"name": "fetch_doc_page", "description": "Fetch and extract text content from a trusted documentation page URL."},
        ],
    },
    {
        "id": "agent-learning",
        "type": "sub_agent",
        "label": "Learning Agent",
        "agent_type": "learning",
        "metadata": {
            "description": "Trend analyst. Analyzes historical scan data to detect degradation patterns, recurring issues, and generates prioritized recommendations verified against documentation.",
            "model": "Claude (Amazon Bedrock)",
        },
        "connects_to": ["svc-bedrock"],
        "tools": [
            {"name": "docs_agent", "description": "Verify recommendations against official AWS and PostgreSQL documentation."},
        ],
    },
]


def _split(value) -> list:
    """Comma-joined registry strings (or lists) -> clean list."""
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v]
    return [p for p in (s.strip() for s in str(value).split(",")) if p]


def classify_reachability(db: dict, conn: Optional[dict], agent_vpc_id: str) -> dict:
    """Derive how the agent reaches one database from what onboarding recorded.

    Returns {"method", "status", "reachability", "detail"} where reachability is
    one of reachable | pending | blocked | unknown. This never probes the network;
    it reports the state the network registry and the database record already
    hold, so a green edge means "the connection we set up is active", not "a
    packet got through just now".
    """
    vpc_id = db.get("vpc_id", "")
    method = db.get("network_method", "") or (conn or {}).get("method", "")
    db_status = db.get("status", "")

    if db_status == "config_failed":
        return {"method": method or "unknown", "status": "failed", "reachability": "blocked",
                "detail": db.get("config_error", "") or "Network configuration failed"}
    if db_status == "configuring":
        return {"method": method or "unknown", "status": "creating", "reachability": "pending",
                "detail": "Network configuration in progress"}
    if db_status == "deleting" and teardown_stalled(db):
        # Its teardown stopped with the process that ran it: Retry finishes it.
        return {"method": method or "unknown", "status": "delete_failed", "reachability": "blocked",
                "detail": "Teardown incomplete: the removal stopped; retry the removal"}
    if db_status == "deleting":
        return {"method": method or "unknown", "status": "deleting", "reachability": "pending",
                "detail": "Being removed"}
    if db_status == "delete_failed":
        return {"method": method or "unknown", "status": "delete_failed", "reachability": "blocked",
                "detail": "Teardown incomplete: " + (db.get("config_error", "") or "retry the removal")}
    if not vpc_id:
        return {"method": "unknown", "status": "unknown", "reachability": "unknown",
                "detail": "No VPC recorded for this database"}
    if agent_vpc_id and vpc_id == agent_vpc_id:
        return {"method": "same_vpc", "status": "active", "reachability": "reachable",
                "detail": "Database shares the agent's VPC"}
    if conn:
        status = conn.get("status", "unknown")
        reach = {"active": "reachable", "creating": "pending", "failed": "blocked",
                 "orphaned": "blocked", "delete_failed": "blocked"}.get(status, "unknown")
        detail = {
            "active": f"{'Lattice resource endpoint' if method == 'lattice' else 'VPC peering'} connection active",
            "creating": "Connection is still being created",
            "failed": "Connection setup failed",
            "orphaned": "Connection exists but no database references it",
            "delete_failed": f"Teardown incomplete: {conn.get('failed_step') or 'unknown step'}",
        }.get(status, "Connection state not recorded")
        return {"method": method or conn.get("method", "unknown"), "status": status,
                "reachability": reach, "detail": detail}
    if method:
        return {"method": method, "status": "unknown", "reachability": "unknown",
                "detail": "Network method recorded but no connection entry found"}
    return {"method": "unknown", "status": "unknown", "reachability": "unknown",
            "detail": "No network connection recorded"}


def build_topology(space_id: str) -> dict:
    """Build a topology graph for a space from DynamoDB data.

    Additive schema: nodes/edges/stats keep their existing shape and the
    payload gains `coverage` and `warnings`, database nodes gain network and
    cluster facets, and two edge types are added — `in_region` (vpc -> region)
    and `reaches` (agent-vpc -> database, one per database, carrying the
    reachability classification).
    """
    nodes = []
    edges = []
    warnings = []
    seen_vpcs = set()
    seen_accounts = set()
    seen_regions = set()
    edge_keys = set()  # dedup edges

    def add_edge(edge: dict) -> None:
        key = f"{edge['source']}|{edge['type']}|{edge['target']}"
        if key not in edge_keys:
            edge_keys.add(key)
            edges.append(edge)

    # Get all databases for this space
    all_dbs = db_registry.list_instances()
    space_dbs = [
        d for d in all_dbs
        if record_in_space(d, space_id)  # membership, not the frozen legacy field
        and d.get("status") not in ("discovered", None)
    ]

    # Agent VPC node
    agent_vpc_id = network_configurator.agent_vpc_id
    agent_vpc_cidr = network_configurator.agent_vpc_cidr
    agent_region = os.getenv("AWS_REGION", "us-east-1")
    if agent_vpc_id:
        nodes.append({
            "id": "agent-vpc",
            "type": "agent_vpc",
            "label": "DBOps Agent",
            "region": agent_region,
            "metadata": {
                "vpc_id": agent_vpc_id,
                "cidr": agent_vpc_cidr or "",
            },
        })
    else:
        warnings.append({"code": "agent_vpc_unknown",
                         "message": "AGENT_VPC_ID is not set; connectivity edges cannot be drawn."})

    # Add sub-agent nodes
    for agent in SUB_AGENTS:
        nodes.append({
            "id": agent["id"],
            "type": agent["type"],
            "label": agent["label"],
            "agent_type": agent.get("agent_type", ""),
            "metadata": agent.get("metadata", {}),
            "tools": agent.get("tools", []),
        })
        # Orchestrator connects to the Agent VPC
        if agent["agent_type"] == "orchestrator":
            if agent_vpc_id:
                add_edge({"source": "agent-vpc", "target": agent["id"], "type": "runs", "label": "runs"})
        else:
            # Other agents are delegated from orchestrator
            add_edge({"source": "agent-orchestrator", "target": agent["id"],
                      "type": "delegates", "label": "delegates to"})
        # Connect agents to their AWS services
        for svc_id in agent.get("connects_to", []):
            add_edge({"source": agent["id"], "target": svc_id, "type": "uses_service", "label": "uses"})

    # Add AWS service nodes
    for svc in AGENT_SERVICES:
        nodes.append({**svc})

    # Load network connections upfront. Peering is per VPC pair, so it is keyed by the
    # database VPC; a Lattice resource endpoint is per database, keyed by its
    # deterministic connection id; the resource gateway is VPC-level state shared by the
    # VPC's Lattice databases.
    peering_by_vpc, lattice_by_conn, gateway_by_vpc = {}, {}, {}
    try:
        from tools.aws.ddb_pages import read_all
        connections = read_all(network_registry.table.scan)
        for conn in connections:
            db_vpc_id = conn.get("db_vpc_id", "")
            method = conn.get("method", "peering")
            if method == "lattice":
                lattice_by_conn[conn.get("connection_id", "")] = conn
            elif method == "lattice-gateway":
                if db_vpc_id:
                    gateway_by_vpc[db_vpc_id] = conn
            elif db_vpc_id:
                peering_by_vpc[db_vpc_id] = conn
    except Exception as e:
        logger.warning(f"Failed to load network connections: {e}")
        warnings.append({"code": "network_registry_unavailable",
                         "message": "Network connection registry could not be read; "
                                    "connection status is unknown."})

    # Load report counts and recommendation counts per database
    report_counts = {}
    rec_counts = {}
    for db in space_dbs:
        db_id = db["instance_id"]
        aid = db.get("account_id", "unknown")
        try:
            reports = report_storage.list_reports(account_id=aid, instance_id=db_id)
            report_counts[db_id] = len([r for r in reports if r.get("type") == "html"])
        except Exception:
            report_counts[db_id] = 0
        try:
            recs = recommendations_registry.list_for_instance(db_id)
            active = [r for r in recs if r.get("status") in ("open", "acknowledged", "in_progress")]
            rec_counts[db_id] = {"total": len(recs), "active": len(active)}
        except Exception:
            rec_counts[db_id] = {"total": 0, "active": 0}

    reach_counts = {"reachable": 0, "pending": 0, "blocked": 0, "unknown": 0}
    lattice_vpcs = {}      # db VPC -> resource endpoint ids of its Lattice databases
    lattice_joined = 0
    with_metadata = 0
    refreshed = 0
    with_network = 0

    # Build database, VPC, and account nodes
    for db in space_dbs:
        db_id = db["instance_id"]
        vpc_id = db.get("vpc_id", "")
        account_id = db.get("account_id", "")
        region = db.get("region", "")
        network_method = db.get("network_method", "")
        tags = db.get("tags", {}) or {}
        # This database's own connection row: its Lattice endpoint row when it reaches
        # over Lattice, else the VPC's peering row.
        lattice_row = None
        if vpc_id and db.get("endpoint") and agent_vpc_id:
            lattice_row = lattice_by_conn.get(NetworkRegistry.make_connection_id(
                agent_vpc_id, vpc_id, db["endpoint"], "lattice"))
        via_lattice = network_method == "lattice" or (bool(lattice_row) and network_method != "peering")
        if via_lattice:
            conn = lattice_row
            if conn:
                lattice_joined += 1
            if vpc_id and vpc_id != agent_vpc_id:
                lattice_vpcs.setdefault(vpc_id, []).append((conn or {}).get("vpce_id") or db.get("vpce_id", ""))
        else:
            conn = peering_by_vpc.get(vpc_id) if vpc_id else None

        members = db.get("cluster_member_roles") or []
        if not members and tags.get("cluster_members"):
            members = [{"instance_id": m, "is_writer": False, "endpoint": ""}
                       for m in _split(tags.get("cluster_members"))]
        writer = next((m.get("instance_id", "") for m in members if m.get("is_writer")), "")
        readers = [m.get("instance_id", "") for m in members if not m.get("is_writer")]

        reach = classify_reachability(db, conn, agent_vpc_id)
        reach_counts[reach["reachability"]] += 1
        if tags.get("engine_version"):
            with_metadata += 1
        if tags.get("metadata_refreshed_at"):
            refreshed += 1
        if vpc_id:
            with_network += 1
        else:
            warnings.append({"code": "no_vpc", "node_id": f"db-{db_id}",
                             "message": f"{db.get('display_name', db_id)}: no VPC recorded; "
                                        "re-run Configure to enrich it."})
        if not tags.get("engine_version"):
            warnings.append({"code": "metadata_missing", "node_id": f"db-{db_id}",
                             "message": f"{db.get('display_name', db_id)}: RDS metadata has never "
                                        "been collected; use Refresh."})
        if reach["reachability"] == "blocked":
            warnings.append({"code": "connection_blocked", "node_id": f"db-{db_id}",
                             "message": f"{db.get('display_name', db_id)}: {reach['detail']}."})

        # Database node
        nodes.append({
            "id": f"db-{db_id}",
            "type": "database",
            "label": db.get("display_name", db_id),
            "engine": db.get("engine", ""),
            "status": db.get("status", ""),
            "health": db.get("last_health_score") or "unknown",
            "region": region,
            "account_id": account_id,
            "metadata": {
                "endpoint": db.get("endpoint", ""),
                "reader_endpoint": db.get("reader_endpoint", ""),
                "port": int(db.get("port", 5432)),
                "vpc_id": vpc_id,
                "network_method": network_method,
                "database_name": db.get("database_name", ""),
                "engine_version": tags.get("engine_version", ""),
                "multi_az": tags.get("multi_az", ""),
                "storage_encrypted": tags.get("storage_encrypted", ""),
                "instance_class": tags.get("instance_class", ""),
                "parameter_group": tags.get("parameter_group", ""),
                "pi_enabled": tags.get("pi_enabled", ""),
                "pi_retention": tags.get("pi_retention", ""),
                "log_exports": tags.get("log_exports", ""),
                "deletion_protection": tags.get("deletion_protection", ""),
                "backup_retention": tags.get("backup_retention", ""),
                "storage_type": tags.get("storage_type", ""),
                "allocated_storage": tags.get("allocated_storage", ""),
                "kms_key_id": tags.get("kms_key_id", ""),
                "cluster_members": tags.get("cluster_members", ""),
                "log_groups": tags.get("log_groups", ""),
                "alarm_count": tags.get("alarm_count", ""),
                "report_count": report_counts.get(db_id, 0),
                "rec_total": rec_counts.get(db_id, {}).get("total", 0),
                "rec_active": rec_counts.get(db_id, {}).get("active", 0),
                # Whether a verified credential is on file — never the pointer itself.
                # The graph is read by operators, and a Secrets Manager ARN is a
                # credential handle the databases listing already withholds from them.
                "credentials_configured": bool(db.get("secret_arn")),
                "last_scan_at": db.get("last_scan_at", ""),
                # Network placement (recorded at onboarding, AZs by Refresh).
                "subnet_ids": _split(db.get("subnet_ids")),
                "subnet_azs": _split(tags.get("subnet_azs")),
                "security_group_ids": _split(db.get("security_group_ids")),
                # Cluster shape and replication.
                "is_cluster": bool(members) or db.get("engine", "").startswith("aurora"),
                "writer_instance": writer,
                "reader_instances": readers,
                "cluster_member_roles": members,
                "replica_of": tags.get("replica_of", ""),
                "replicas": _split(tags.get("replicas")),
                # Monitoring facets collected by Refresh.
                "alarms_in_alarm": tags.get("alarms_in_alarm", ""),
                "alarm_names": _split(tags.get("alarm_names")),
                "metadata_refreshed_at": tags.get("metadata_refreshed_at", ""),
                # How the agent reaches this database (see classify_reachability).
                "reachability": reach["reachability"],
                "reach_method": reach["method"],
                "reach_status": reach["status"],
                "reach_detail": reach["detail"],
                "config_error": db.get("config_error", ""),
                # This database's own connection row (its endpoint, for Lattice).
                "connection_id": (conn or {}).get("connection_id", ""),
                "vpce_id": (conn or {}).get("vpce_id", "") or db.get("vpce_id", ""),
                "resource_configuration_arn": (conn or {}).get("resource_configuration_arn", ""),
            },
        })

        # VPC node (deduplicated)
        if vpc_id and vpc_id not in seen_vpcs:
            seen_vpcs.add(vpc_id)
            # CIDR from the network connection, else the value Refresh recorded.
            cidr = ((peering_by_vpc.get(vpc_id) or {}).get("db_vpc_cidr", "")
                    or (conn or {}).get("db_vpc_cidr", "") or tags.get("vpc_cidr", ""))
            if vpc_id == agent_vpc_id and not cidr:
                cidr = agent_vpc_cidr or ""
            nodes.append({
                "id": f"vpc-{vpc_id}",
                "type": "vpc",
                "label": f"{vpc_id}" + (f" ({cidr})" if cidr else ""),
                "region": region,
                "account_id": account_id,
                "metadata": {"vpc_id": vpc_id, "cidr": cidr,
                             "is_agent_vpc": bool(agent_vpc_id) and vpc_id == agent_vpc_id},
            })

        # Edge: database → VPC
        if vpc_id:
            add_edge({"source": f"db-{db_id}", "target": f"vpc-{vpc_id}",
                      "type": "hosted_in", "label": "hosted in"})

        # Account node (deduplicated)
        if account_id and account_id not in seen_accounts:
            seen_accounts.add(account_id)
            nodes.append({
                "id": f"account-{account_id}",
                "type": "account",
                "label": f"Account {account_id}",
                "metadata": {"account_id": account_id},
            })

        # Edge: VPC → Account
        if vpc_id and account_id:
            add_edge({"source": f"vpc-{vpc_id}", "target": f"account-{account_id}",
                      "type": "belongs_to", "label": "in account"})

        # Region node (deduplicated) and VPC → Region edge
        if region and region not in seen_regions:
            seen_regions.add(region)
            nodes.append({
                "id": f"region-{region}",
                "type": "region",
                "label": region,
                "metadata": {"region": region},
            })
        if vpc_id and region:
            add_edge({"source": f"vpc-{vpc_id}", "target": f"region-{region}",
                      "type": "in_region", "label": "in region"})

        # Network connection: Agent VPC → DB VPC (one edge per VPC)
        if agent_vpc_id and vpc_id:
            if vpc_id == agent_vpc_id:
                add_edge({"source": "agent-vpc", "target": f"vpc-{vpc_id}", "type": "same_vpc",
                          "label": "Same VPC", "metadata": {"status": "active"}})
            elif via_lattice:
                pass  # the VPC-level Lattice edge is built from the gateway row below
            elif conn:
                add_edge({"source": "agent-vpc", "target": f"vpc-{vpc_id}", "type": "peering",
                          "label": "VPC Peering",
                          "metadata": {"status": conn.get("status", "unknown"),
                                       "connection_id": conn.get("connection_id", ""),
                                       "peering_id": conn.get("peering_id", "")}})
            elif network_method:
                # No registry entry but DB has a network method recorded
                add_edge({
                    "source": "agent-vpc",
                    "target": f"vpc-{vpc_id}",
                    "type": network_method,
                    "label": "VPC Peering" if network_method == "peering" else "Lattice resource endpoint",
                    "metadata": {
                        "status": "unknown",
                        "peering_id": db.get("network_peering_id", ""),
                        "vpce_id": db.get("vpce_id", ""),
                    },
                })

        # Reachability: Agent VPC → database (one edge per database)
        if agent_vpc_id:
            add_edge({
                "source": "agent-vpc",
                "target": f"db-{db_id}",
                "type": "reaches",
                "label": reach["reachability"],
                "metadata": {
                    "method": reach["method"],
                    "status": reach["status"],
                    "reachability": reach["reachability"],
                    "detail": reach["detail"],
                    **({"connection_id": conn.get("connection_id", ""),
                        "vpce_id": conn.get("vpce_id", ""),
                        "resource_configuration_arn": conn.get("resource_configuration_arn", "")}
                       if conn and conn.get("method") == "lattice" else {}),
                },
            })

    # Network connection over Lattice: Agent VPC -> DB VPC, one edge per VPC, carrying the
    # VPC's resource gateway state and every endpoint of that VPC's databases.
    if agent_vpc_id:
        for vpc_id, vpce_ids in lattice_vpcs.items():
            gw = gateway_by_vpc.get(vpc_id) or {}
            ids = sorted({v for v in vpce_ids if v})
            meta = {"status": gw.get("status", "unknown"),
                    "connection_id": gw.get("connection_id", ""),
                    "resource_gateway_id": gw.get("resource_gateway_id", ""),
                    "vpce_ids": ids}
            if len(ids) == 1:
                meta["vpce_id"] = ids[0]
            add_edge({"source": "agent-vpc", "target": f"vpc-{vpc_id}", "type": "lattice",
                      "label": "Lattice resource endpoint", "metadata": meta})

    # Stats
    stats = {
        "total_nodes": len(nodes),
        "total_edges": len(edges),
        "databases": len([n for n in nodes if n["type"] == "database"]),
        "vpcs": len([n for n in nodes if n["type"] in ("vpc", "agent_vpc")]),
        "accounts": len(seen_accounts),
        "regions": len(seen_regions),
        "services": len(AGENT_SERVICES),
        "agents": len(SUB_AGENTS),
        "peering_connections": len([e for e in edges if e["type"] == "peering"]),
        "lattice_connections": lattice_joined,
        "same_vpc_connections": len([e for e in edges if e["type"] == "same_vpc"]),
    }

    total = len(space_dbs)
    coverage = {
        "databases": total,
        "with_network": with_network,
        "with_metadata": with_metadata,
        "refreshed": refreshed,
        **reach_counts,
    }

    return {
        "space_id": space_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": stats,
        "coverage": coverage,
        "warnings": warnings,
        "nodes": nodes,
        "edges": edges,
    }


def get_node_detail(space_id: str, node_id: str) -> dict:
    """Get detailed info for a specific node, including drill-down data."""
    topology = build_topology(space_id)
    node = next((n for n in topology["nodes"] if n["id"] == node_id), None)
    if not node:
        return {"error": "Node not found"}

    connected_edges = [e for e in topology["edges"] if e["source"] == node_id or e["target"] == node_id]
    connected_node_ids = set()
    for e in connected_edges:
        connected_node_ids.add(e["source"])
        connected_node_ids.add(e["target"])
    connected_node_ids.discard(node_id)
    connected_nodes = [n for n in topology["nodes"] if n["id"] in connected_node_ids]

    return {
        "node": node,
        "connected_nodes": connected_nodes,
        "connected_edges": connected_edges,
    }
