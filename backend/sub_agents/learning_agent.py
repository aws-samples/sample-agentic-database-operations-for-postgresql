# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Learning Agent — analyzes historical scan trends and generates recommendations.

Triggered after scan completion via the /api/learn endpoint. Compares metrics
across scans, detects degradation patterns, and generates prioritized
recommendations verified against official documentation.
"""

import logging
from typing import AsyncIterator

from strands import Agent, tool
from model_resolver import effective_model_id, build_bedrock_model

from tools.types import SubAgentResult
from sub_agents.docs_agent import docs_agent

logger = logging.getLogger("learning-agent")


LEARNING_AGENT_PROMPT = """# Instructions
You are a database health trend analyst. You analyze historical scan data to identify
degradation patterns, recurring issues, and generate actionable recommendations.

# Available Tools
- docs_agent: Search official documentation (AWS docs, PostgreSQL docs) to verify
  recommendations before generating them. Always verify significant recommendations.

# Input Format
You receive a JSON context with:
- instance_id, display_name, engine: Database identification
- latest_scan: Most recent scan analysis (health_score, metrics, findings)
- previous_scans: Array of previous scan analyses (up to 4)
- metric_trends: Pre-computed trend data showing metric changes
- finding_recurrence: Map of finding keys to consecutive scan count

# Analysis Protocol
1. Compare latest metrics against previous scans — identify degrading trends
2. Check finding recurrence — findings appearing in 3+ consecutive scans need attention
3. Identify threshold proximity — metrics approaching critical thresholds
4. Use docs_agent to verify key recommendations against official documentation
5. Generate prioritized recommendations

# Output Format
Return a JSON array of recommendations. Each recommendation:
```json
{
  "category": "performance|maintenance|config|security",
  "priority": "critical|high|medium|low",
  "title": "Short actionable title",
  "detail": "What was observed with specific numbers",
  "action": "Specific command or steps to fix",
  "source_url": "Documentation URL if verified via docs_agent",
  "metric_key": "The metric this relates to (if applicable)",
  "finding_key": "area:title_prefix for tracking"
}
```

# Priority Rules
- CRITICAL: Metric crossed critical threshold OR finding recurrence >= 5
- HIGH: Metric degrading consistently OR finding recurrence >= 3
- MEDIUM: Metric approaching warning threshold OR finding recurrence >= 2
- LOW: Informational improvement opportunity

# Rules
- Only generate recommendations for actionable issues
- Always include specific numbers (before/after values, percentages)
- Verify parameter change recommendations via docs_agent
- Do NOT recommend things that are already at optimal values
- Be concise — DBAs are busy
"""


@tool
async def learning_agent(context: str) -> AsyncIterator:
    """Analyze historical scan trends for a database and generate recommendations.

    This specialist analyzes metric trends across multiple scans, identifies
    degradation patterns and recurring issues, and generates prioritized
    recommendations verified against official documentation.

    Args:
        context: JSON string with scan history, metrics, trends, and finding recurrence

    Yields:
        SubAgentResult events for streaming, final yield is the recommendations JSON
    """
    logger.info(f"learning_agent invoked: {context[:120]}")

    try:
        model = build_bedrock_model(effective_model_id(), 8000)

        agent = Agent(
            name="Learning Specialist",
            model=model,
            system_prompt=LEARNING_AGENT_PROMPT,
            tools=[docs_agent],
            callback_handler=None,
        )

        result = None
        async for event in agent.stream_async(context):
            yield SubAgentResult(agent=agent, event=event)
            if "result" in event:
                result = event["result"]

        if result:
            yield str(result)
        else:
            yield "[]"

    except Exception as e:
        logger.error(f"Learning agent failed: {e}", exc_info=True)
        yield f"Learning analysis error: {e}"
