# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Documentation verification sub-agent — searches trusted docs to verify
recommendations and find best practices.

Searches are restricted to official documentation sources:
AWS docs, PostgreSQL docs, pganalyze, MySQL docs, etc.
"""

import logging
from typing import AsyncIterator

from strands import Agent, tool
from model_resolver import effective_model_id, build_bedrock_model

from tools.types import SubAgentResult
from tools.web_search import fetch_doc_page, get_web_search_mcp_client

logger = logging.getLogger("docs-agent")


DOCS_AGENT_PROMPT = """# Instructions
You are a documentation research specialist for database systems on AWS. You use a
managed web search tool (an AWS-maintained index) to verify recommendations, find
best practices, and provide authoritative, version-specific guidance.

# Available Tools
- Managed web search (your primary tool): issue ONE focused, specific query. Include the
  engine and version when known (e.g., "Aurora PostgreSQL 16 autovacuum_freeze_max_age").
- fetch_doc_page: fetch the full content of a specific documentation page URL when a search
  snippet isn't detailed enough. Only trusted documentation domains are allowed.

# Trusted Sources (ranked by authority)
1. docs.aws.amazon.com — Official AWS documentation (RDS, Aurora, best practices)
2. postgresql.org/docs — Official PostgreSQL documentation
3. wiki.postgresql.org — PostgreSQL community wiki
4. pganalyze.com/docs — PostgreSQL performance analysis guides
5. repost.aws — AWS community knowledge base

# Workflow
1. Receive a question about database best practices, configuration, or troubleshooting.
2. Issue ONE focused web search (batch related sub-questions into a single query).
3. If the snippets answer it, synthesize the answer with source URLs.
4. If more detail is needed, fetch AT MOST the single most relevant page with fetch_doc_page.
5. Provide the answer with source URLs for verification.

# Rules
- ALWAYS include source URLs so findings can be verified.
- Be version-specific when the engine version is known (PostgreSQL 15 vs 16; Aurora vs RDS).
- Issue ONE focused query, then read at most one page. Do NOT fire many near-duplicate searches.
- If web search returns no usable results, say explicitly "Unverified — could not reach
  documentation" and mark any guidance as unverified. NEVER present recommendations from
  model memory as if they were verified from documentation.
- Keep responses focused and concise — extract the relevant guidance, not entire pages.
"""

# The DuckDuckGo built-in search is disabled; the managed web search tool is the only
# discovery backend. fetch_doc_page (direct trusted-domain fetch) is kept for reading a
# specific cited page.
DOCS_TOOLS = [fetch_doc_page]


@tool
async def docs_agent(question: str) -> AsyncIterator:
    """Search official documentation to verify database recommendations and find best practices.

    This specialist searches trusted sources only — AWS docs, PostgreSQL docs, pganalyze, etc.
    Use it to verify tuning recommendations, find version-specific guidance, or look up
    AWS best practices for RDS/Aurora.

    Include the database engine (e.g., PostgreSQL, MySQL) and version in your question
    for more accurate results.

    Args:
        question: The documentation question, including engine/version context if known

    Yields:
        SubAgentResult events for streaming, final yield is the verified answer with sources
    """
    logger.info(f"docs_agent invoked: {question[:120]}")

    model = build_bedrock_model(effective_model_id(), 8000)

    # Managed AgentCore Web Search (via Gateway MCP) is the ONLY documentation backend —
    # the DuckDuckGo fallback was removed. It is feature-flagged on WEB_SEARCH_GATEWAY_URL
    # (EnableWebSearch=true at deploy). When it isn't configured, degrade cleanly to an
    # explicit "unverified" instead of guessing from model memory or scraping.
    web_client = get_web_search_mcp_client()
    if web_client is None:
        logger.warning("Managed web search not configured (EnableWebSearch=false); returning unverified.")
        yield ("Documentation verification is UNVERIFIED: managed web search is not enabled "
               "for this deployment (deploy with EnableWebSearch=true). I did not consult "
               "external documentation, so treat any guidance as unverified.")
        return

    try:
        with web_client:
            web_tools = web_client.list_tools_sync()
            agent = Agent(
                name="Research Specialist",
                model=model,
                system_prompt=DOCS_AGENT_PROMPT,
                tools=[*web_tools, *DOCS_TOOLS],
                callback_handler=None,
            )
            result = None
            async for event in agent.stream_async(question):
                yield SubAgentResult(agent=agent, event=event)
                if "result" in event:
                    result = event["result"]
            yield str(result) if result else "Documentation search complete"
    except Exception as e:
        logger.error(f"Docs agent (managed web search) failed: {e}", exc_info=True)
        yield f"Documentation search error: {e}"
