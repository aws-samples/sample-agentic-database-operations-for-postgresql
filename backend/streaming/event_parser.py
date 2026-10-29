# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import ipaddress
import json
import logging
import os
import re
from typing import AsyncIterator
from tools.types import SubAgentResult

logger = logging.getLogger("event-parser")


# ── Trace redaction ─────────────────────────────────────────────────────────
# Every streamed event (the trace, tool inputs and results, and the model's answer text)
# reaches the operator's browser through _emit and _StreamRedactor. What is masked there
# is THIS DEPLOYMENT'S OWN infrastructure identifiers, never the monitored databases'
# diagnostic data:
#   masked  secret ARNs, IAM role and STS assumed-role ARNs, the account id inside any
#           other ARN, addresses inside the deployment's own VPC CIDR (AGENT_VPC_CIDR),
#           and the deployment's own metrics store table names
#   kept    a monitored database's endpoint (operators see it in the members table) and
#           any address outside the deployment's VPC: a client IP in a database's log, a
#           database's own address. Those are the customer's troubleshooting data; masking
#           them turned "authentication failures from 10.20.30.40" into a useless
#           "[redacted-ip]".
# Investigation content (SQL, findings, metrics, table names, reasoning) is untouched.
# This changes what the client sees, not what the agent does.
_SECRET_ARN_RE = re.compile(r"arn:aws:secretsmanager:[A-Za-z0-9:_/\-]+", re.IGNORECASE)
_ROLE_ARN_RE = re.compile(r"arn:aws:iam::\d+:role/[A-Za-z0-9+=,.@_/\-]+", re.IGNORECASE)
# STS assumed-role identity (arn:aws:sts::<account>:assumed-role/<role>/<session>). An
# AWS authorization failure names the caller in this form, so without it the account id
# and task-role name reach the browser. Same placeholder as the IAM role ARN.
_STS_ARN_RE = re.compile(r"arn:aws:sts::\d+:assumed-role/[A-Za-z0-9+=,.@_/\-]+", re.IGNORECASE)
# Any remaining ARN that carries a 12-digit account id (dynamodb table, sns topic, batch
# job, sqs queue, ...). Run AFTER the specific patterns above, which have already replaced
# the secret/role/STS forms with placeholders that do not start with "arn:aws:", so this
# only catches the "account id inside a non-role ARN" case. Account-less ARNs (e.g. S3
# bucket arn:aws:s3:::name) do not match the ':<account>:' segment and are left alone.
_GENERIC_ARN_RE = re.compile(r"arn:aws:[a-z0-9\-]*:[a-z0-9\-]*:\d{12}:[^\s\"']+", re.IGNORECASE)
# A dotted-quad candidate. Whether it is masked is decided by ipaddress membership in the
# deployment's VPC CIDR (_in_agent_vpc), not by this pattern.
_IPV4_RE = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.]*\d)")
# The deployment's VPC CIDR, the same variable and default network_client uses.
_DEFAULT_AGENT_VPC_CIDR = "172.20.0.0/16"


def _agent_vpc_network():
    """The deployment's own VPC CIDR as an ip_network (AGENT_VPC_CIDR, default
    172.20.0.0/16). A malformed value falls back to the default rather than to masking
    nothing, and is logged."""
    raw = (os.getenv("AGENT_VPC_CIDR") or _DEFAULT_AGENT_VPC_CIDR).strip()
    try:
        return ipaddress.ip_network(raw, strict=False)
    except ValueError:
        logger.warning("AGENT_VPC_CIDR %r is not a CIDR; redacting %s", raw, _DEFAULT_AGENT_VPC_CIDR)
        return ipaddress.ip_network(_DEFAULT_AGENT_VPC_CIDR)


def _mask_agent_vpc_ip(m):
    try:
        ip = ipaddress.ip_address(m.group(0))
    except ValueError:
        return m.group(0)  # 999.1.2.3, a version string: not an address
    return "[redacted-ip]" if ip in _agent_vpc_network() else m.group(0)


def _deployment_endpoints() -> list:
    """The deployment's own store identifiers the code knows: the metrics store's two
    DynamoDB table names (TIER1_METRICS_TABLE, TIER2_SCANS_TABLE). The store has no
    network endpoint; a table name is what an AWS error about it would carry outside an
    ARN (an ARN is already caught by the ARN patterns). Longest first, so one name that
    prefixes another is replaced whole."""
    names = {(os.getenv(k) or "").strip() for k in ("TIER1_METRICS_TABLE", "TIER2_SCANS_TABLE")}
    return sorted((n for n in names if n), key=len, reverse=True)


def _redact_text(text):
    """Redact this deployment's infrastructure identifiers from a trace string: secret,
    IAM role and STS ARNs, the account id inside any other ARN, the deployment's own
    metrics store endpoints, and addresses inside the deployment's VPC CIDR.

    The monitored databases' diagnostic data is deliberately NOT redacted: their
    endpoints, the addresses of their clients and their own addresses (outside the
    deployment's VPC), SQL, table names, database usernames, findings and reasoning. The
    trace exists to show the operator the details of their own investigation."""
    if not isinstance(text, str) or not text:
        return text
    text = _SECRET_ARN_RE.sub("[redacted-secret-arn]", text)
    text = _ROLE_ARN_RE.sub("[redacted-role-arn]", text)
    text = _STS_ARN_RE.sub("[redacted-role-arn]", text)
    for ep in _deployment_endpoints():
        text = re.sub(r"(?<![A-Za-z0-9.\-])" + re.escape(ep) + r"(?![A-Za-z0-9\-]|\.[A-Za-z0-9])",
                      "[redacted-endpoint]", text, flags=re.IGNORECASE)
    text = _GENERIC_ARN_RE.sub("[redacted-arn]", text)
    text = _IPV4_RE.sub(_mask_agent_vpc_ip, text)
    return text


def _redact_value(value):
    """Recursively redact infrastructure identifiers in a tool input/result value
    (str / dict / list), preserving structure so the emitted JSON stays valid."""
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


def _emit(obj):
    """Serialise ONE event to an NDJSON line, redacting infrastructure identifiers on
    the way out. Every event is emitted through here, so no call site can forget to
    redact and any event type added later inherits the same rules by default. Only the
    deployment's own identifiers (see _redact_text) are touched; every other field passes
    through unchanged."""
    return json.dumps(_redact_value(obj)) + "\n"


def emit_model_event(model_id: str, reasoning: dict = None) -> str:
    """One NDJSON `model` trace line naming the RESOLVED model actually used for the turn,
    plus, when given, the reasoning controls it runs with (``surface``, ``effort``,
    ``thinking``; see model_resolver.reasoning_summary). Goes through _emit like every
    event, so it rides the same redaction contract — but a Bedrock model id is a model
    NAME, not an infrastructure identifier (no ARN, no account id, no deployment
    endpoint), so _redact_text leaves it intact. Emitted once per turn, before the agent
    events."""
    return _emit({"type": "model", "model_id": model_id, **(reasoning or {})})


class _StreamRedactor:
    """Redact model text as it streams WITHOUT letting an identifier slip through split
    across two deltas.

    Per-delta redaction missed an ARN, endpoint or address whose first half arrived in one chunk
    and second half in the next. Every identifier the redactor targets is whitespace-free,
    so text up to the last whitespace is complete with respect to them and can be
    redacted and emitted; the tail after it is held until more text (or the end of the
    message) arrives. Emission therefore moves word by word rather than token by token,
    which the UI does not notice, and the stream is redacted as a whole."""

    _WS = re.compile(r"\s")

    def __init__(self):
        self._buf = ""

    def feed(self, text: str) -> str:
        if not text:
            return ""
        self._buf += text
        cut = -1
        for m in self._WS.finditer(self._buf):
            cut = m.end()
        if cut <= 0:
            return ""
        out, self._buf = self._buf[:cut], self._buf[cut:]
        return _redact_text(out)

    def flush(self) -> str:
        out, self._buf = self._buf, ""
        return _redact_text(out)


def _map_agent_event(event, emitted_tools: set, emitted_sub_tools: set, tool_names: dict, redactors: dict = None):
    """Map ONE raw Strands Agent event to zero or more NDJSON lines.

    Handles text deltas, tool start/use/result, and nested sub-agent
    (SubAgentResult) events. The terminal `result` event is NOT handled here —
    parse_agent_events finalizes that.

    ``tool_names`` maps toolUseId -> tool name so the tool_result / sub_tool_result
    events can carry the name of the tool they belong to (results arrive as separate
    user-message events with only a toolUseId, so without this map the client can't
    label which tool produced a result).
    """
    if "event" in event and isinstance(event["event"], dict):
        inner_event = event["event"]

        if "contentBlockDelta" in inner_event:
            delta = inner_event["contentBlockDelta"].get("delta", {})
            if "text" in delta:
                if redactors is not None:
                    out = redactors.setdefault("__main__", _StreamRedactor()).feed(delta["text"])
                    if out:
                        yield _emit({"type": "text", "content": out})
                else:
                    yield _emit({"type": "text", "content": delta["text"]})

        if "contentBlockStop" in inner_event and redactors is not None and "__main__" in redactors:
            held = redactors["__main__"].flush()
            if held:
                yield _emit({"type": "text", "content": held})
        if "contentBlockStart" in inner_event:
            start = inner_event["contentBlockStart"].get("start", {})
            if "toolUse" in start:
                tool_use = start["toolUse"]
                tool_id = tool_use.get("toolUseId")
                tool_name = tool_use.get("name")
                if tool_id:
                    tool_names[tool_id] = tool_name
                if tool_id and tool_id not in emitted_tools:
                    emitted_tools.add(tool_id)
                    yield _emit({
                        "type": "tool_start",
                        "name": tool_name,
                        "toolUseId": tool_id,
                    })

    if "message" in event:
        message = event["message"]
        if message.get("role") == "assistant" and "content" in message:
            for content_block in message["content"]:
                if "toolUse" in content_block:
                    tool_use = content_block["toolUse"]
                    if tool_use.get("toolUseId"):
                        tool_names[tool_use["toolUseId"]] = tool_use.get("name")
                    yield _emit({
                        "type": "tool",
                        "name": tool_use.get("name"),
                        "toolUseId": tool_use.get("toolUseId"),
                        "input": tool_use.get("input", {}),
                    })
        # Capture tool results (returned as toolResult in user messages)
        if message.get("role") == "user" and "content" in message:
            for content_block in message["content"]:
                if "toolResult" in content_block:
                    tool_result = content_block["toolResult"]
                    result_content = tool_result.get("content", [])
                    result_text = ""
                    for rc in result_content:
                        if "text" in rc:
                            result_text = rc["text"][:8000]  # Cap at 8000 chars
                            break
                    yield _emit({
                        "type": "tool_result",
                        "name": tool_names.get(tool_result.get("toolUseId")),
                        "toolUseId": tool_result.get("toolUseId"),
                        "status": tool_result.get("status", "success"),
                        "content": result_text,
                    })

    if "tool_stream_event" in event:
        tool_stream = event["tool_stream_event"]
        data = tool_stream.get("data")

        if isinstance(data, SubAgentResult):
            sub_event = data.event
            agent_name = data.agent.name

            if "event" in sub_event and isinstance(sub_event["event"], dict):
                sub_inner = sub_event["event"]
                if "contentBlockDelta" in sub_inner:
                    delta = sub_inner["contentBlockDelta"].get("delta", {})
                    if "text" in delta:
                        content = delta["text"]
                        if redactors is not None:
                            content = redactors.setdefault(agent_name, _StreamRedactor()).feed(content)
                        if content:
                            yield _emit({
                                "type": "sub_text",
                                "agent": agent_name,
                                "content": content,
                            })

            if "message" in sub_event:
                message = sub_event["message"]
                if message.get("role") == "assistant" and "content" in message:
                    for content_block in message["content"]:
                        if "toolUse" in content_block:
                            tool_use = content_block["toolUse"]
                            tool_id = tool_use.get("toolUseId")
                            if tool_id:
                                tool_names[tool_id] = tool_use.get("name")
                            if tool_id and tool_id not in emitted_sub_tools:
                                emitted_sub_tools.add(tool_id)
                                yield _emit({
                                    "type": "sub_tool",
                                    "agent": agent_name,
                                    "name": tool_use.get("name"),
                                    "toolUseId": tool_id,
                                    "input": tool_use.get("input", {}),
                                })
                # Sub-agent tool RESULTS (returned as toolResult in user messages).
                # The sub-agent forwards every event. Emit sub_tool_result (mirrors the
                # top-level tool_result) so the trace carries each nested tool's output,
                # not just its input, and testers can verify it against the diagnostic spec.
                if message.get("role") == "user" and "content" in message:
                    for content_block in message["content"]:
                        if "toolResult" in content_block:
                            tool_result = content_block["toolResult"]
                            result_content = tool_result.get("content", [])
                            result_text = ""
                            for rc in result_content:
                                if "text" in rc:
                                    result_text = rc["text"][:8000]  # cap parity with top-level tool_result
                                    break
                            yield _emit({
                                "type": "sub_tool_result",
                                "agent": agent_name,
                                "name": tool_names.get(tool_result.get("toolUseId")),
                                "toolUseId": tool_result.get("toolUseId"),
                                "status": tool_result.get("status", "success"),
                                "content": result_text,
                            })


async def parse_agent_events(agent_stream: AsyncIterator) -> AsyncIterator[str]:
    """Parse Agent-path streaming events and yield formatted JSON strings."""
    emitted_tools = set()
    emitted_sub_tools = set()
    tool_names = {}
    redactors: dict = {}
    result = None

    def _flush_all():
        for key, r in redactors.items():
            held = r.flush()
            if held:
                yield _emit({"type": "text", "content": held} if key == "__main__"
                            else {"type": "sub_text", "agent": key, "content": held})

    try:
        async for event in agent_stream:
            for line in _map_agent_event(event, emitted_tools, emitted_sub_tools, tool_names, redactors):
                yield line
            if "result" in event:
                result = event["result"]
        for line in _flush_all():
            yield line

        if result:
            if hasattr(result, "stop_reason") and result.stop_reason == "interrupt":
                for interrupt in result.interrupts:
                    yield _emit({
                        "type": "interrupt",
                        "interrupt_id": interrupt.id,
                        "name": interrupt.name,
                        "reason": interrupt.reason,
                    })
            else:
                yield _emit({"type": "result", "response": str(result)})
        else:
            # Stream ended without a result event — still emit completion
            # so the frontend knows the stream is done
            yield _emit({"type": "result", "response": "Investigation complete"})

    except Exception as e:
        for line in _flush_all():
            yield line
        # Redact the error too: psycopg2/AWS errors carry RDS endpoints and ARNs, which
        # must not reach the browser raw.
        yield _emit({"type": "error", "error": str(e)})
