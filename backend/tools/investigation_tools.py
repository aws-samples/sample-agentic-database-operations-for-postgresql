# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Orchestrator-level tool for escalating a chat turn into a full investigation.

Chat is intentionally lightweight — quick answers via tools. When a question is
incident-shaped (a root-cause analysis that needs multiple signals correlated and
would benefit from a persisted report), the orchestrator calls
``recommend_investigation`` to surface a "Start investigation" action to the user.

It does NOT start an investigation. The user decides; the frontend renders the
suggestion as a pill and, on accept, creates the investigation (which runs through
the investigation agent with its runbooks + skills). Keeping the decision with the
user preserves the chat/investigation separation and the investigation's audit
trail (findings, timeline, report).
"""

from strands import tool


@tool
def recommend_investigation(title: str, rationale: str) -> str:
    """Suggest that the user's request warrants a full, structured investigation
    rather than a quick chat answer.

    Call this when the question is incident-shaped: a root-cause / "why did this
    happen" analysis that needs multiple signals correlated (metrics + logs +
    internals + recent changes) and would benefit from a persisted report — for
    example bloat / vacuum health, high CPU or database load, replication lag,
    lock contention, connection spikes, or storage growth.

    Do NOT call this for simple lookups the chat can answer directly (listing
    databases, a single metric, a definition, a status check).

    This surfaces a "Start investigation" action to the user; it does NOT start
    one automatically, and you cannot start one yourself — only the user's click
    does. After calling it, still give a brief chat-level answer. If the user later
    accepts in words ("yes, start it") rather than by clicking, call this again so
    the action is back in view and ask them to click it; never run the
    investigation inline in its place.

    Args:
        title: Short, specific investigation title (e.g. "Table bloat on orders").
        rationale: One sentence on why a full investigation is warranted.

    Returns:
        A confirmation string. Do not call this again for the same issue unless the
        user accepts in words and needs the action back in view.
    """
    return (
        "Investigation suggestion surfaced to the user as a Start investigation action. "
        "Continue with a concise chat-level answer now; if the user clicks it, a "
        "structured investigation runs separately. You cannot start it yourself: if the "
        "user accepts in words instead of clicking, call this again and ask them to click "
        "Start investigation. Otherwise do not call this again for the same issue."
    )
