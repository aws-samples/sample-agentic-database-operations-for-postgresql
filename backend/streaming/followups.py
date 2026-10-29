# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Follow-up question suggestions for the chat (the pills under an answer).

After the agent's answer closes with its ``result`` line, one small model call
proposes up to three short follow-up questions grounded in the exchange, and the
stream gains exactly one more NDJSON line::

    {"type": "suggestions", "items": ["…", "…", "…"]}

When the answer is itself a question to the operator — a clarifying turn, a list
of options, or an offer such as "shall I start an investigation?" — follow-up
*questions* would only echo the assistant's own questions back in the operator's
voice, and clicking one would send the assistant's question to the assistant. Such
turns get follow-up *replies* instead: short answers the operator could give
("Queries are slower than usual", "Yes, start the investigation"). The line carries
``"kind": "questions" | "replies"`` so the UI can render them accordingly.
One exception: when the turn called ``recommend_investigation``, a "Start investigation"
button is already on screen, so the pills never propose starting or accepting the
investigation — they cover the alternatives (a quick check to run here instead).

The frontend attaches the items to the assistant message it just committed and
renders them as pills under the answer; clicking one sends it as the next turn.

This is a post-hoc step rather than an agent tool on purpose:

- it never adds a turn to the agent loop, so the answer streams exactly as before;
- it does not depend on the orchestrator remembering to call a tool;
- it is fail-soft end to end — a timeout, a throttle, a guardrail refusal or an
  unparseable reply simply means no ``suggestions`` line, never an error.

The suggestion call runs on the deployment's default model (``helper_model_id``),
not on the model the operator picked for the turn: it is a utility call whose prompt
and token budget are tuned once against one model, and it sends the helper-surface
reasoning controls (thinking off, lowest effort) so the whole budget goes to the
answer. ``CHAT_FOLLOWUPS_MODEL_ID`` pins a different model, and ``CHAT_FOLLOWUPS=off``
disables the feature.
"""
import asyncio
import functools
import json
import logging
import os
import re
from typing import AsyncIterator, Iterable, List

from streaming.event_parser import _emit
from streaming import remediation_screen
from tools.aws.memory_client import _strip_injected_context

logger = logging.getLogger("followups")

MAX_ITEMS = 3
MAX_ITEM_CHARS = 120
MIN_ITEM_CHARS = 8
# Keep the call cheap: the exchange is trimmed before it is sent.
MAX_PROMPT_CHARS = 1500
MAX_ANSWER_CHARS = 4000
# Per-attempt read timeout and Bedrock attempt count (boto3 counts the first try, so this
# is one try plus two retries). The call runs after the answer, under whatever load the turn
# left, and produces a few hundred tokens at most, so a wide per-attempt window is cheap;
# the step cap below bounds the operator's total wait across all attempts.
CALL_TIMEOUT_S = 15.0
CALL_MAX_ATTEMPTS = 3
STEP_TIMEOUT_S = 30.0
# Output budget. Three one-line questions as a JSON array need well under 200 tokens;
# the headroom keeps a slightly wordier model from being cut mid-array.
MAX_TOKENS = 400

_PROMPT = """You suggest follow-up questions for an operator chatting with a database
operations assistant. Given the operator's last question and the assistant's answer,
propose {n} short follow-up questions the operator would most plausibly ask next.

Rules:
- Each suggestion is one line, at most 12 words, phrased in the operator's voice.
- Stay specific to the databases, metrics and findings named in the answer.
- Prefer next steps that deepen the answer (why, show, compare, explain, which tables).
- The assistant is READ-ONLY: it connects with a read-only user and cannot run VACUUM,
  ANALYZE, REINDEX, kill sessions, change parameters, create indexes, apply fixes, or
  modify anything. Never propose asking it to do those. "How would I fix X?" or
  "What should I change?" (advice, not action) is fine.
- Do not repeat the original question or anything the answer already covers.{investigation_rule}
- Return ONLY a JSON array of strings, no prose, no markdown.

Operator question:
{question}

Assistant answer:
{answer}"""

_REPLY_PROMPT = """The assistant asked the operator for information — a clarifying question, a list of
options to choose from, or an offer that needs a yes or no. Propose {n} short REPLIES the
operator could send back, so they can answer with one click.

Rules:
- Each reply is one line, at most 12 words, in the operator's voice, and ANSWERS the
  assistant's question. Never a question back to the assistant.
- Cover the assistant's options, one reply per option, most likely first.{investigation_rule}
- The assistant is READ-ONLY and cannot run VACUUM, ANALYZE, REINDEX, kill sessions, change
  parameters or apply any fix; never propose a reply that asks it to.
- Stay specific to the databases and symptoms named in the exchange.
- Return ONLY a JSON array of strings, no prose, no markdown.

Operator message:
{question}

Assistant question:
{answer}"""

# The investigation rule differs by whether the turn already put a "Start investigation"
# button on screen (the orchestrator's recommend_investigation call). Without the
# button, accepting an offer is a natural one-click reply; with it, a pill saying the
# same thing would only duplicate the button, so the pills cover the alternatives.
_INVESTIGATION_RULES = {
    "questions": {
        False: "",
        True: ("\n- Do not propose starting, running or accepting an investigation; a Start "
               "investigation button is already on screen for this issue."),
    },
    "replies": {
        False: ("\n  If it offered to start an investigation, include an acceptance such as "
                "\"Yes, start the investigation\".\n- If the operator reported a problem, one "
                "reply may be \"Just start a full investigation\"."),
        True: ("\n- A Start investigation button is already on screen for this issue. Do NOT "
               "propose starting, running or accepting an investigation; propose only the "
               "alternatives the assistant offered, such as one quick check to run here instead."),
    },
}

# Backstop for the rule above: a pill that accepts or starts the investigation while
# the button is on screen is dropped even if the model proposed it.
_ACCEPTS_INVESTIGATION = re.compile(
    r"^(?:yes|yeah|sure|ok(?:ay)?|go ahead|please do|do it)\b"
    r"|\b(?:start|run|launch|begin|kick[ -]?off|go ahead with|proceed with|open)\b[^.?!]{0,40}\binvestigat"
    r"|\binvestigat\w*\b[^.?!]{0,30}\b(?:now|please|right away)\b",
    re.IGNORECASE,
)


def drop_investigation_accepts(items: List[str]) -> List[str]:
    """Remove pills that would start or accept an investigation (see _ACCEPTS_INVESTIGATION)."""
    return [s for s in items if not _ACCEPTS_INVESTIGATION.search(s or "")]


# The agent reaches every database as a read-only user (the dbops_ro role), so a pill
# that asks it to change something can only end in a refusal. The prompts say so; this is
# the backstop for when the model proposes one anyway. It matches an imperative or a
# request to act ("run vacuum", "can you kill", "please reindex", "let's create an
# index", "apply the fix") but leaves advice-seeking phrasings alone ("how would I fix",
# "what should I change", "which index would help", "should I run vacuum?").
_MUTATING_VERBS = (
    r"vacuum|analy[sz]e the|reindex|re-index|kill|terminate|cancel|restart|reboot|failover|"
    r"drop|truncate|delete|insert|update|alter|create (?:an? )?(?:index|extension|table|user|role)|"
    r"add (?:an? )?index|rebuild|set [a-z_]+ ?=|change (?:the )?(?:setting|parameter|config)|"
    r"increase|decrease|tune|resize|scale|apply (?:the |this |that )?(?:fix|change|recommendation)|"
    r"fix (?:it|this|that|them)|implement|enable|disable|turn (?:on|off)|rotate|grant|revoke|"
    r"upgrade|patch|clean ?up|purge|clear the"
)
_ASKS_FOR_MUTATION = re.compile(
    r"^(?:please |just |go ahead and |let'?s |now )*(?:" + _MUTATING_VERBS + r")\b"
    r"|^(?:can|could|would|will) you (?:please )?(?:go ahead and )?(?:" + _MUTATING_VERBS + r")\b"
    r"|^(?:run|execute|perform|do|trigger|schedule) (?:an? |the )?(?:" + _MUTATING_VERBS + r"|vacuum\w*|analyze|reindex|manual)",
    re.IGNORECASE,
)


def drop_mutating_requests(items: List[str]) -> List[str]:
    """Remove pills that ask the read-only agent to change the database (see _ASKS_FOR_MUTATION)."""
    return [s for s in items if not _ASKS_FOR_MUTATION.search((s or "").strip())]

# Cues that the assistant is asking the operator something (beyond a trailing "?").
_CLARIFYING_CUES = re.compile(
    r"\b(could you|can you (?:tell|share|describe|confirm|let)|please (?:describe|share|tell|confirm|let me know)|"
    r"let me know|to help you|which of (?:these|the following)|would you like|do you want|"
    r"a bit more detail|more detail about|be more specific|what (?:exactly|specifically))\b",
    re.IGNORECASE,
)


def is_clarifying(answer: str) -> bool:
    """True when the answer is a question to the operator rather than an answer.

    Signals: the closing line ends with "?"; at least two lines end with "?" (a
    list of questions, as in a symptom checklist); or a request cue such as
    "could you", "would you like", "to help you". Deliberately conservative — a
    normal answer that happens to contain one rhetorical question is not one.
    """
    text = (answer or "").strip()
    if not text:
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines and lines[-1].endswith("?"):
        return True
    if sum(1 for ln in lines if ln.rstrip("*_ ").endswith("?")) >= 2:
        return True
    return bool(_CLARIFYING_CUES.search(text))


def suggestion_kind(answer: str) -> str:
    """``"replies"`` for a clarifying turn, else ``"questions"``."""
    return "replies" if is_clarifying(answer) else "questions"


def enabled() -> bool:
    return os.getenv("CHAT_FOLLOWUPS", "on").strip().lower() not in ("off", "0", "false", "no")


def _bullet_free(line: str) -> str:
    return re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip().strip('"\u201c\u201d\'').strip()


def parse_suggestions(text: str, limit: int = MAX_ITEMS) -> List[str]:
    """Extract the suggestion list from a model reply.

    Accepts a bare JSON array, an array embedded in prose or a code fence, or —
    as a last resort — one suggestion per line. Items are trimmed, de-bulleted,
    de-duplicated case-insensitively, length-bounded and capped at ``limit``.
    Anything unusable yields ``[]`` rather than an exception.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    candidates: Iterable = []
    m = re.search(r"\[[\s\S]*?\]", text)
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list):
                candidates = arr
        except ValueError:
            candidates = []
    if not candidates:
        candidates = text.splitlines()
    out: List[str] = []
    seen = set()
    for c in candidates:
        if not isinstance(c, str):
            continue
        s = _bullet_free(c)
        # Skip fences, structural JSON lines and anything too short to be a question.
        if s.startswith(("```", "{", "}", "[", "]")) or len(s) < MIN_ITEM_CHARS:
            continue
        if len(s) > MAX_ITEM_CHARS:
            s = s[: MAX_ITEM_CHARS - 1].rstrip() + "…"
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def build_prompt(user_prompt: str, answer: str, n: int = MAX_ITEMS, kind: str = None,
                 offered_investigation: bool = False) -> str:
    """The suggestion prompt for one exchange, with the operator app's injected
    context wrappers stripped from the question and both sides trimmed. ``kind``
    defaults to what the answer's shape calls for (see suggestion_kind);
    ``offered_investigation`` says a Start investigation button is already on screen
    for this turn (see _INVESTIGATION_RULES)."""
    question = _strip_injected_context(user_prompt or "").strip()
    if len(question) > MAX_PROMPT_CHARS:
        question = question[:MAX_PROMPT_CHARS].rstrip() + "…"
    body = (answer or "").strip()
    if len(body) > MAX_ANSWER_CHARS:
        # Keep the head and the tail: findings lead, recommendations close.
        half = MAX_ANSWER_CHARS // 2
        body = body[:half].rstrip() + "\n…\n" + body[-half:].lstrip()
    resolved_kind = "replies" if (kind or suggestion_kind(answer)) == "replies" else "questions"
    template = _REPLY_PROMPT if resolved_kind == "replies" else _PROMPT
    return template.format(n=n, question=question or "(not available)", answer=body,
                           investigation_rule=_INVESTIGATION_RULES[resolved_kind][bool(offered_investigation)])


_client = None


def _bedrock():
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config
        _client = boto3.client(
            "bedrock-runtime",
            region_name=(os.getenv("BEDROCK_REGION") or os.getenv("AWS_REGION") or "us-east-1"),
            # Adaptive mode backs off and retries on ThrottlingException.
            config=Config(read_timeout=CALL_TIMEOUT_S, connect_timeout=3,
                          retries={"max_attempts": CALL_MAX_ATTEMPTS, "mode": "adaptive"}),
        )
    return _client


def _drop_rejected_param(body: dict, exc: Exception, model_id: str, mark_temperature, mark_effort,
                         mark_thinking, which_reasoning_param) -> str:
    """Remove from ``body`` the optional param a Bedrock error rejects, record the
    rejection for the model, and name the param. ``""`` when the error is about
    something else or the param is not in the body."""
    s = str(exc).lower()
    if "temperature" in body and "temperature" in s:
        body.pop("temperature")
        mark_temperature(model_id)
        return "temperature"
    param = which_reasoning_param(exc)
    if param == "effort" and "output_config" in body:
        body.pop("output_config")
        mark_effort(model_id)
        return "output_config.effort"
    if param == "thinking" and "thinking" in body:
        body.pop("thinking")
        mark_thinking(model_id)
        return "thinking"
    return ""


def generate_followups(user_prompt: str, answer: str, model_id: str = "",
                       offered_investigation: bool = False) -> List[str]:
    """Ask the model for follow-ups. Returns ``[]`` on any failure.

    ``model_id`` is the turn's model and is accepted for the caller's convenience only:
    the call runs on ``helper_model_id()`` (the deployment default, or the
    ``CHAT_FOLLOWUPS_MODEL_ID`` pin), see the module docstring.
    """
    if not answer or not answer.strip():
        return []
    try:
        # Same guardrail rules as the other raw bedrock-runtime callers: the answer
        # text is database-derived, so this call is screened like the agent paths.
        from model_resolver import (
            SURFACE_HELPER, guardrail_invoke_kwargs_from_env, helper_model_id,
            mark_effort_unsupported, mark_temperature_unsupported, mark_thinking_unsupported,
            model_supports_temperature, surface_model_kwargs, unsupported_reasoning_param,
        )
        effective_model = helper_model_id()
        if not effective_model:
            return []
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": build_prompt(
                user_prompt, answer, offered_investigation=offered_investigation)}],
        }
        # Claude Opus 4.7+ and the 5 line reject a temperature with a ValidationException;
        # send it only where the model accepts it (the same decision build_bedrock_model
        # makes for the agents).
        if model_supports_temperature(effective_model):
            body["temperature"] = 0.4
        # Helper-surface reasoning controls (thinking off, lowest effort), only the ones
        # this model accepts.
        body.update(surface_model_kwargs(effective_model, SURFACE_HELPER))

        def _invoke(b):
            return _bedrock().invoke_model(
                modelId=effective_model, contentType="application/json",
                accept="application/json", body=json.dumps(b),
                **guardrail_invoke_kwargs_from_env(),
            )

        # A family the static capability markers do not cover may reject one of the
        # optional params. Record the rejection so later builds omit the param, drop it,
        # and retry; each param is retried at most once (mirrors the agent path in app.py).
        for _ in range(3):
            try:
                response = _invoke(body)
                break
            except Exception as e:  # noqa: BLE001
                dropped = _drop_rejected_param(body, e, effective_model, mark_temperature_unsupported,
                                               mark_effort_unsupported, mark_thinking_unsupported,
                                               unsupported_reasoning_param)
                if not dropped:
                    raise
                logger.warning("follow-up suggestions: %s rejected %s; retrying without it",
                               effective_model, dropped)
        result = json.loads(response["body"].read())
        text = "".join(
            part.get("text", "") for part in result.get("content", []) if isinstance(part, dict)
        )
        if not text.strip() and result.get("stop_reason") == "max_tokens":
            # The model spent the whole output budget before producing any text (a
            # reasoning model thinking inside the budget). Named in the log so the
            # missing pills are traceable to the budget rather than to a refusal.
            logger.warning("follow-up suggestions skipped: %s consumed max_tokens=%d with no text "
                           "(output_tokens=%s)", effective_model, MAX_TOKENS,
                           (result.get("usage") or {}).get("output_tokens"))
            return []
        return parse_suggestions(text)
    except Exception as e:  # noqa: BLE001 — suggestions must never fail the turn
        # The turn still succeeds; a failure here removes the pills, so it is a warning.
        logger.warning("follow-up suggestions skipped: %s", e)
        return []


# Markers the operator app injects for a report- or investigation-discussion turn. Such a
# turn stays inside its own content (no live connection, no investigation launch), so the
# model's generic follow-ups are wrong here — they have offered live checks and "start an
# investigation". The client supplies its own report-/investigation-grounded follow-ups
# for these turns, so the server emits none.
_CONTEXT_MARKERS = ("[REPORT CONTEXT]", "[INVESTIGATION CONTEXT]")


def is_context_prompt(prompt: str) -> bool:
    """True when the turn is a report- or investigation-discussion turn (the client
    injected a context block). These turns get client-side, context-grounded follow-ups
    instead of the model's generic ones."""
    p = prompt or ""
    return any(m in p for m in _CONTEXT_MARKERS)


def suggestions_line(items: List[str], kind: str = "questions") -> str:
    """The NDJSON line for a suggestion set (redacted like every other event)."""
    return _emit({"type": "suggestions", "kind": kind, "items": list(items)})


def caution_line(items: Iterable[dict]) -> str:
    """The NDJSON line for destructive-SQL cautions (redacted like every other event)."""
    return _emit({"type": "caution", "kind": "destructive_sql", "items": list(items)})


async def with_followups(lines: AsyncIterator[str], user_prompt: str, model_id: str,
                         generate=generate_followups) -> AsyncIterator[str]:
    """Pass a parsed NDJSON stream through unchanged, then append one
    ``suggestions`` line once the turn has closed with ``result``.

    The answer is reassembled from the ``text`` events on the way past. Turns that
    end in an interrupt or an error, or that produce no text, get no suggestions.
    A turn in which the orchestrator called recommend_investigation has a Start
    investigation button on screen, so its pills must not duplicate it: the
    generator is told so, and any accept/start pill it still proposes is dropped.
    The model call is synchronous boto3, so it runs in a worker thread to keep the
    event loop free for other streams.
    """
    answer_parts: List[str] = []
    closed = False
    suppressed = False
    offered = False
    async for line in lines:
        yield line
        try:
            obj = json.loads(line)
        except (TypeError, ValueError):
            continue
        kind = obj.get("type") if isinstance(obj, dict) else None
        if kind == "text":
            answer_parts.append(obj.get("content") or "")
        elif kind == "result":
            closed = True
        elif kind in ("interrupt", "error"):
            suppressed = True
        elif kind == "tool" and obj.get("name") == "recommend_investigation":
            offered = True
    answer = "".join(answer_parts).strip()
    # Advisory destructive-SQL caution, emitted BEFORE the follow-up gates on purpose: it
    # is a safety annotation on the answer's content, not a follow-up suggestion, so it must
    # not depend on the follow-up toggle (CHAT_FOLLOWUPS) or on context mode. Only fenced
    # "run this" blocks are inspected, so investigating a DELETE is unaffected. Fail-soft:
    # any error means no caution line, never a raise.
    if closed and not suppressed and answer:
        try:
            cautions = remediation_screen.screen(answer)
            if cautions:
                yield caution_line(cautions)
        except Exception as e:  # noqa: BLE001
            logger.info("remediation caution skipped: %s", e)
    if not (closed and not suppressed and enabled()):
        return
    # Report/investigation discussion gets its follow-ups from the client, grounded in the
    # report it holds; the model's generic ones can wrongly offer a live check or starting
    # an investigation, so none are emitted here.
    if is_context_prompt(user_prompt):
        return
    if not answer:
        return
    loop = asyncio.get_event_loop()
    # The keyword is only passed when it matters, so a plain three-argument
    # generator (the tests' stand-ins) keeps working.
    call = (functools.partial(generate, user_prompt, answer, model_id, offered_investigation=True)
            if offered else functools.partial(generate, user_prompt, answer, model_id))
    try:
        items = await asyncio.wait_for(
            loop.run_in_executor(None, call),
            timeout=STEP_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        # Named so a timeout is distinguishable from a model refusal in the logs.
        logger.warning("follow-up suggestions skipped: timed out after %.0fs", STEP_TIMEOUT_S)
        return
    except Exception as e:  # noqa: BLE001
        logger.warning("follow-up suggestions skipped: %s", e)
        return
    if offered:
        items = drop_investigation_accepts(items or [])
    items = drop_mutating_requests(items or [])
    if items:
        yield suggestions_line(items, suggestion_kind(answer))
