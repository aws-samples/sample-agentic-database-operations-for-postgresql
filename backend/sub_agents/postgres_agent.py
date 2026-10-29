# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""PostgreSQL sub-agent — wraps all PG-specific analysis tools.

The orchestrator invokes this as a single @tool. Internally it creates
a dedicated Agent with the full set of PostgreSQL health/optimization
tools and streams results back via SubAgentResult.
"""

import logging
from typing import AsyncIterator

from strands import Agent, tool
from model_resolver import effective_model_id, build_bedrock_model
from strands_tools import current_time

from tools.types import SubAgentResult
from sub_agents.check_outcome import CheckOutcomes, failed_specialist_answer
from tools.db_tools import (
    # Section 1: Database Overview
    check_db_connectivity, get_db_overview,
    # Section 2: System Configuration
    get_db_configuration, get_security_audit, get_setting_overrides, get_extension_inventory,
    # Section 3: Current Activity
    get_current_activity, get_connection_analysis, get_workload_characterization,
    get_workload_profile,
    # Section 4: Replication Status
    get_replication_status, get_wal_status, get_logical_replication,
    # Section 5: Data Footprint
    get_storage_analysis, get_checkpoint_stats, get_bgwriter_stats,
    get_autovacuum_utilization, get_connection_pooling_efficiency,
    get_buffer_pool_stats, get_lock_analysis,
    # Section 6: Query & I/O Performance
    get_io_statistics, get_query_performance, get_query_plan,
    get_transaction_stats, get_function_performance, get_wait_event_analysis,
    get_session_duration_analysis, get_prepared_statement_analysis,
    get_temp_file_queries,
    # Section 7: Maintenance & Health
    get_maintenance_status, get_sequence_exhaustion, get_constraint_validation,
    get_materialized_view_freshness, get_vacuum_stats_health, get_insert_autovacuum_lag,
    get_stale_statistics,
    # Section 8: Optimization
    get_index_statistics, get_toast_status, get_partitioning_analysis,
    get_bloat_analysis, get_unused_indexes, get_duplicate_indexes,
    get_idle_connections, get_oversized_datatypes, get_unused_tables,
    get_fk_without_indexes, get_trigger_analysis, get_sequential_scan_tables,
    # Section 9: Health Summary
    get_health_summary,
    # Growth / Tier-1 trend (relocated from the coordinator — design §14.4)
    get_historical_metrics, assess_growth,
)

logger = logging.getLogger("postgres-agent")


POSTGRES_AGENT_PROMPT = """# Instructions
You are a PostgreSQL database health and optimization specialist. You analyze PostgreSQL
databases running on AWS (Aurora PostgreSQL and RDS PostgreSQL) using a comprehensive
set of diagnostic tools.

You receive a question or task from the orchestrator along with context such as the
secret_arn (the database alias from the scope, e.g. db-1; pass it exactly as given) and database name. Use your tools to gather data and provide analysis.

# Tool Organization (by section)
Section 1 - Overview: get_db_overview
Section 2 - Configuration: get_db_configuration, get_security_audit, get_setting_overrides,
            get_extension_inventory
Section 3 - Activity: get_current_activity, get_connection_analysis, get_workload_characterization,
            get_workload_profile
Section 4 - Replication: get_replication_status, get_wal_status, get_logical_replication
Section 5 - Data Footprint: get_storage_analysis, get_checkpoint_stats, get_bgwriter_stats,
            get_autovacuum_utilization, get_connection_pooling_efficiency, get_buffer_pool_stats,
            get_lock_analysis
Section 6 - Query & I/O: get_io_statistics, get_query_performance, get_query_plan,
            get_transaction_stats, get_function_performance, get_wait_event_analysis,
            get_session_duration_analysis, get_prepared_statement_analysis, get_temp_file_queries
Section 7 - Maintenance: get_maintenance_status, get_sequence_exhaustion,
            get_constraint_validation, get_materialized_view_freshness, get_vacuum_stats_health,
            get_stale_statistics
Section 8 - Optimization: get_index_statistics, get_toast_status, get_partitioning_analysis,
            get_bloat_analysis, get_unused_indexes, get_duplicate_indexes, get_idle_connections,
            get_oversized_datatypes, get_unused_tables, get_fk_without_indexes,
            get_trigger_analysis, get_sequential_scan_tables
Section 9 - Summary: get_health_summary
Growth/Trend (Tier 1): get_historical_metrics (PG-internal 5-min trends), assess_growth
            (deterministic is_growing verdict — relocated from the coordinator)

# Rules
- Always use tools to get real data. Never guess database metrics.
- WHEN A CHECK FAILS, SAY SO FIRST. If a tool returns an error, open your answer with which
  checks could not run and the error each returned, quoted. Never present the checks that did
  run as a full or healthy picture. Never ask the orchestrator or the user for a database id or
  credential: the secret_arn you were given is the one in scope, so a refusal of it is an error
  to report, not a missing input.
- For "is storage growing / how fast / will it fill up" questions, call assess_growth and TRUST
  its is_growing verdict; never eyeball or extrapolate growth yourself. For bloat sizing, trust
  get_vacuum_stats_health's est_wasted — NEVER derive wasted space from total DB size or
  (dead_pct x total size), and state a bloat root cause only from get_vacuum_stats_health
  (it reports autovacuum_enabled / reloptions).
- USE THE STRUCTURED TOOL, DON'T HAND-ROLL SQL for these — they already return the data:
  * Dead tuples + per-table autovacuum status + reloptions (autovacuum_enabled=DISABLED) and
    est_wasted -> get_vacuum_stats_health. Do NOT run a custom reloptions/pg_class query to
    "find" disabled tables; the tool lists them.
  * Snapshot holders / vacuum blockers (backend_xmin, xmin age in XIDs) -> get_current_activity's
    `snapshot_holders` field (it excludes this session and reports xmin_age_xids via age(backend_xmin)).
    NEVER hand-roll a pg_stat_activity query for backend_xmin, and NEVER cast it with
    ::text::bigint — that cast is invalid. Apply the strict rule: a blocker requires
    xmin_age_xids > 1,000,000 AND dead tuples existing somewhere.
  * Idle-in-transaction sessions -> get_current_activity's `idle_in_transaction` field.
- DO NOT re-run a check with cosmetic variants hoping for different output — the data does not
  change. If a tool errors, fix the intent or pick the right structured tool; do NOT fire more
  guessed variants.
- SCHEMA-QUALIFY EVERY TABLE. Emit `schema.table` exactly as it appears in the tool row
  (every tool returns schema_name/schemaname alongside the table). NEVER infer one table's
  schema from another — disabled/other tables often live in a DIFFERENT schema than the
  bloated one, so guessing produces ALTER statements that fail. If a tool row lacks a schema,
  say the schema is unknown rather than assuming it.
- VACUUM RECLAMATION WORDING (be precise): plain VACUUM / autovacuum return dead space to the
  table's free space map for REUSE — they do NOT shrink the on-disk file or reduce table size.
  Never say the table "shrinks" or size "is reduced on subsequent scans". Only VACUUM FULL
  (AccessExclusiveLock — windowed) or pg_repack reduce on-disk size; describe est_wasted as
  "freed for reuse within the table".
- AURORA PARAMETER CHANGES: on Aurora (and RDS) PostgreSQL, `ALTER SYSTEM` + pg_reload_conf()
  do NOT work — cluster/instance-level settings are changed via the DB cluster/instance
  PARAMETER GROUP. Never recommend ALTER SYSTEM for Aurora/RDS; point to the parameter group.
  (Per-table reloptions like autovacuum_enabled ARE set with ALTER TABLE — that is fine.)
- TOOL SELECTION GUIDE:
  * Individual section tools: Use for targeted live analysis ("check locks", "show unused indexes").
  * For a broad "check everything" ask, call the relevant section tools; there is no single
    full-analysis tool — comprehensive periodic analysis is handled by the batch scan.
  * There is NO raw-SQL escape hatch — you have ONLY the structured tools above; use them. For
    index review use get_unused_indexes / get_duplicate_indexes / get_index_statistics (they already
    EXCLUDE primary-key / unique indexes from drop candidates); a bloat/vacuum question is fully
    answered by get_vacuum_stats_health (dead %, size, est_wasted, autovacuum status, reloptions).
  * If NO structured tool answers the request, do NOT hand-roll or fabricate SQL and do NOT guess —
    reply that the request is beyond your current tooling and recommend the user share it as feedback
    so a structured tool can be added. (Hand-rolled index/usage verdicts produced unsafe advice like
    "drop the primary-key index" — that is why raw SQL is disabled and this rule is hard.)
  * Do NOT re-run a query with different schema or database values to "find" data — tool results are
    schema-qualified, and the database argument is the PostgreSQL dbname (usually 'postgres'), NEVER the
    instance or cluster identifier.
- When showing data, use markdown tables for readability.
- Classify findings by severity: CRITICAL, WARNING, INFO, OK.
- For query optimization suggestions, always explain the reasoning.
- When asked about performance degradation, systematically check:
  1. Configuration changes (get_db_configuration)
  2. Current activity and locks (get_current_activity, get_lock_analysis)
  3. Cache hit ratios (get_buffer_pool_stats, get_io_statistics)
  4. Top queries (get_query_performance)
  5. Maintenance status / bloat (get_vacuum_stats_health, get_bloat_analysis)
- Be concise. DBAs are busy.
- All responses should be markdown formatted.
- ALWAYS LABEL DATA SOURCES: When presenting data, prefix with the source:
  * [pg_stat_activity] — current live sessions (point-in-time snapshot of what's running NOW)
  * [pg_stat_statements] — cumulative stats since last reset (lifetime totals, NOT current activity)
  * [pg_stat_user_tables] — cumulative table stats (dead tuples, vacuum counts, etc.)
  When the orchestrator asks "what is the database doing right now?", answer ONLY from
  pg_stat_activity. Do NOT mix in pg_stat_statements data as if it's current activity.
  pg_stat_statements shows what queries have EVER run, not what's running now.
"""

PG_TOOLS = [
    current_time,
    check_db_connectivity,
    # Section 1
    get_db_overview,
    # Section 2
    get_db_configuration, get_security_audit, get_setting_overrides, get_extension_inventory,
    # Section 3
    get_current_activity, get_connection_analysis, get_workload_characterization,
    get_workload_profile,
    # Section 4
    get_replication_status, get_wal_status, get_logical_replication,
    # Section 5
    get_storage_analysis, get_checkpoint_stats, get_bgwriter_stats,
    get_autovacuum_utilization, get_connection_pooling_efficiency,
    get_buffer_pool_stats, get_lock_analysis,
    # Section 6
    get_io_statistics, get_query_performance, get_query_plan,
    get_transaction_stats, get_function_performance, get_wait_event_analysis,
    get_session_duration_analysis, get_prepared_statement_analysis,
    get_temp_file_queries,
    # Section 7
    get_maintenance_status, get_sequence_exhaustion, get_constraint_validation,
    get_materialized_view_freshness, get_vacuum_stats_health, get_insert_autovacuum_lag,
    get_stale_statistics,
    # Section 8
    get_index_statistics, get_toast_status, get_partitioning_analysis,
    get_bloat_analysis, get_unused_indexes, get_duplicate_indexes,
    get_idle_connections, get_oversized_datatypes, get_unused_tables,
    get_fk_without_indexes, get_trigger_analysis, get_sequential_scan_tables,
    # Section 9
    get_health_summary,
    # Growth / Tier-1 trend (relocated from the coordinator — design §14.4)
    get_historical_metrics, assess_growth,
]


@tool
async def postgres_agent(question: str) -> AsyncIterator:
    """Analyze PostgreSQL databases — health checks, performance diagnostics,
    optimization recommendations, security audits, and maintenance status.

    This specialist agent handles all PostgreSQL-specific analysis including:
    - Health summaries and targeted section analysis
    - Query performance, locks, wait events, I/O stats
    - Index optimization, bloat detection, unused objects
    - Replication status, WAL analysis
    - Security audits, configuration review
    - Custom SQL queries (read-only)

    The orchestrator MUST include the database alias from [DATABASE SCOPE] (its
    secret_arn, e.g. db-1) and the database name in the question so this agent can
    connect to the correct database.

    Args:
        question: The analysis question, including the database alias (secret_arn) and database

    Yields:
        SubAgentResult events for streaming, final yield is the answer
    """
    logger.info(f"postgres_agent invoked: {question[:120]}")

    try:
        model = build_bedrock_model(effective_model_id(), 8000)

        agent = Agent(
            name="PostgreSQL Specialist",
            model=model,
            system_prompt=POSTGRES_AGENT_PROMPT,
            tools=PG_TOOLS,
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
        logger.error(f"PostgreSQL agent failed: {e}", exc_info=True)
        yield failed_specialist_answer("PostgreSQL specialist", e)
