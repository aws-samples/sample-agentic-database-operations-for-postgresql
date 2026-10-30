# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""AWS Observability sub-agent — CloudWatch Metrics, Performance Insights,
CloudTrail, and CloudWatch Logs.

Engine-agnostic tools for infrastructure-level monitoring. The orchestrator
delegates metrics/logs/audit questions here.
"""

import logging
from typing import AsyncIterator

from strands import Agent, tool
from model_resolver import effective_model_id, build_bedrock_model
from tools.clock import current_time

from tools.types import SubAgentResult
from sub_agents.check_outcome import CheckOutcomes, failed_specialist_answer
from tools.aws.cloudwatch_client import (
    get_cpu_utilization, get_db_connections, get_freeable_memory,
    get_iops, get_free_storage_space, get_network_throughput,
    get_cloudwatch_alarms, get_replica_lag,
)
from tools.aws.performance_insights_client import (
    get_db_load, get_top_sql, get_top_waits,
)
from tools.aws.cloudtrail_client import get_rds_events, get_rds_modifications
from tools.aws.cloudwatch_logs_client import get_db_error_logs, query_db_logs
from tools.db_tools import get_historical_metrics, get_instance_details

logger = logging.getLogger("observability-agent")


OBSERVABILITY_AGENT_PROMPT = """# Instructions
You are an AWS observability specialist for database instances. You monitor RDS and Aurora
databases using CloudWatch Metrics, Performance Insights, CloudTrail, and CloudWatch Logs.

You receive a question from the orchestrator along with the db_instance_id: the database
alias from [DATABASE SCOPE] (db-1, db-2, ...). Pass it unchanged as db_instance_id (identifier
for get_instance_details, instance_id for get_historical_metrics). Your tools resolve it to the
RDS cluster or instance, its account and its Region, and resolve Aurora cluster members
themselves. Never pass a display name: names can repeat and change.

# Available Tools

## CloudWatch Metrics
- get_cpu_utilization: CPU usage % over time. High CPU (>80%) sustained = investigate queries.
- get_db_connections: Connection count. Compare against max_connections.
- get_freeable_memory: Available memory. Low = shared_buffers pressure or workload growth.
- get_iops: Read/write IOPS and latency. High latency + low IOPS = storage throttling.
- get_free_storage_space: Disk remaining (GB). Not applicable to Aurora (auto-scales).
- get_network_throughput: Network bytes/sec. Spikes may indicate bulk operations or replication.
- get_cloudwatch_alarms: Active alarms for the instance.
- get_replica_lag: Replica lag per reader: AuroraReplicaLag (ms) for Aurora readers, ReplicaLag (s) for RDS read replicas and Multi-AZ DB cluster readers.

## Performance Insights (requires PI enabled on the instance)
- get_db_load: Average Active Sessions by wait type. Load > vCPU count = CPU-constrained.
- get_top_sql: Top SQL by load contribution. Shows which queries consume most resources.
- get_top_waits: Top wait events. Reveals bottlenecks — CPU, I/O, locks, network.

## CloudTrail
- get_rds_events: Recent API events — who did what.
- get_rds_modifications: Parameter changes, reboots, failovers, scaling events.

## CloudWatch Logs
- get_db_error_logs: Recent error/postgresql log entries.
- query_db_logs: Run Logs Insights queries for filtering and aggregation.

## PostgreSQL Internal Metrics (Tier 1 — 5-minute granularity)
- get_historical_metrics: Recent PostgreSQL-internal metric trends that CloudWatch does NOT
  have: cache hit ratio, dead tuple growth, XID age, connection state breakdown (active/idle/
  idle-in-txn), blocked queries, autovacuum activity, per-table bloat, wait events.
  Use this to correlate with CloudWatch data — e.g., "CPU spiked AND cache hit ratio dropped."

## Instance / Capacity (RDS API)
- get_instance_details: instance class, vCPU count, cluster topology, engine version. Call this
  FIRST when analyzing capacity — compare Average Active Sessions (get_db_load) against vCPU count.

# Rules
- Always use tools to get real data. Never guess metrics.
- WHEN A CHECK FAILS, SAY SO FIRST. If a tool returns an error, open your answer with which
  checks could not run and the error each returned, quoted. Never present the checks that did
  run as a full or healthy picture, and never ask for a database id you were already given.
- When asked about CPU, also check get_db_load for context on what's driving the load.
- When asked about performance, check both CloudWatch metrics AND Performance Insights.
- For "what changed" questions, use CloudTrail tools.
- Default to 1 hour of data unless the user specifies a different range.
- Present metrics clearly with current value, trend direction, and severity assessment.
- Severity: CRITICAL (>90% CPU, load > 2x vCPUs), WARNING (>70% CPU, load > vCPUs), OK.
"""

OBSERVABILITY_TOOLS = [
    current_time,
    # CloudWatch Metrics
    get_cpu_utilization, get_db_connections, get_freeable_memory,
    get_iops, get_free_storage_space, get_network_throughput,
    get_cloudwatch_alarms, get_replica_lag,
    # Performance Insights
    get_db_load, get_top_sql, get_top_waits,
    # CloudTrail
    get_rds_events, get_rds_modifications,
    # CloudWatch Logs
    get_db_error_logs, query_db_logs,
    # PostgreSQL Internal Metrics (Tier 1)
    get_historical_metrics,
    # Instance / capacity (RDS API) — relocated from the coordinator (design §14.4)
    get_instance_details,
]


@tool
async def observability_agent(question: str) -> AsyncIterator:
    """Query AWS observability data for database instances — CloudWatch metrics,
    Performance Insights, CloudTrail audit events, and CloudWatch Logs.

    This specialist handles infrastructure-level monitoring questions:
    - CPU utilization, memory, IOPS, connections, storage, network
    - Database load, top SQL by load, wait event analysis
    - Who changed what — parameter modifications, reboots, failovers
    - Error logs and log queries

    The orchestrator MUST include the database alias from [DATABASE SCOPE] (its
    db_instance_id, e.g. db-1; never the display name) in the question.

    Args:
        question: The monitoring question, including the database alias (db_instance_id)

    Yields:
        SubAgentResult events for streaming, final yield is the answer
    """
    logger.info(f"observability_agent invoked: {question[:120]}")

    try:
        model = build_bedrock_model(effective_model_id(), 8000)

        agent = Agent(
            name="AWS Observability Specialist",
            model=model,
            system_prompt=OBSERVABILITY_AGENT_PROMPT,
            tools=OBSERVABILITY_TOOLS,
            callback_handler=None,
        )

        # The coordinator is told, from the tool results themselves, which
        # checks failed, so a failed run is never summarised as a healthy one.
        outcomes = CheckOutcomes()
        result = None
        async for event in agent.stream_async(question):
            yield SubAgentResult(agent=agent, event=event)
            outcomes.observe(event)
            if "result" in event:
                result = event["result"]

        yield outcomes.annotate(str(result) if result else "Analysis complete")

    except Exception as e:
        logger.error(f"Observability agent failed: {e}", exc_info=True)
        yield failed_specialist_answer("AWS observability specialist", e)
