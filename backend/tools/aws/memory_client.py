# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Read-side helper for AgentCore Memory — chat rehydration.

The agent writes conversation events to AgentCore Memory (short-term) via the
Strands session manager. This helper reads them back so the operator app can
rehydrate a reopened chat with its past turns.

Design notes:
- MEMORY_ID is read dynamically (not captured at import) so the same image
  behaves correctly whether or not the env var is injected, and so it's easy to
  exercise in tests.
- Caller isolation is structural: events are namespaced by actor_id, and every
  read passes the caller's Cognito sub as actor_id, so a user can only read
  their own conversations.
- Never raises: a memory backend error must not turn a chat reopen into a 500.
  It degrades to an empty transcript.
"""

import os
import re
import json
import logging

logger = logging.getLogger("memory-reader")


def _memory_id() -> str:
    return os.getenv("MEMORY_ID", "")


def _region() -> str:
    return os.getenv("AWS_REGION", "us-east-1")


class MemoryReader:
    """Reads a session's conversation from AgentCore Memory."""

    def __init__(self):
        self._dp = None

    @property
    def enabled(self) -> bool:
        return bool(_memory_id())

    @property
    def dp(self):
        """Low-level bedrock-agentcore data-plane client (record list/search/delete)."""
        if self._dp is None:
            import boto3
            self._dp = boto3.client("bedrock-agentcore", region_name=_region())
        return self._dp

    def _list_session_events(self, memory_id: str, actor_id: str, session_id: str,
                             max_events: int = 1000) -> list:
        """All events for a session, oldest-first. Paginates ListEvents.

        ``ListEvents`` returns most-recent-first; ``eventId`` carries a zero-padded
        monotonic sequence prefix, so sorting ascending yields chronological order.
        """
        events = []
        token = None
        while len(events) < max_events:
            kwargs = dict(
                memoryId=memory_id, actorId=actor_id, sessionId=session_id, maxResults=100
            )
            if token:
                kwargs["nextToken"] = token
            resp = self.dp.list_events(**kwargs)
            events.extend(resp.get("events", []) or [])
            token = resp.get("nextToken")
            if not token:
                break
        events.sort(key=lambda e: e.get("eventId") or "")
        return events

    def get_messages(self, actor_id: str, session_id: str, k: int = 100) -> list:
        """Return a session's conversation as ordered ``[{role, content}]``.

        Oldest message first, ready for chat display. Reconstructs the *pure*
        conversation — the user's questions and the agent's text answers — exactly
        as the live chat showed them. Returns an empty list when memory is not
        configured, the identifiers are missing, or nothing is stored.

        Filtering (so a rehydrated transcript matches the live UI, not the raw
        event log):
        - Agent/session **state blobs** (non-conversational payloads) are skipped.
        - **Tool turns** — assistant ``toolUse`` and user ``toolResult`` messages —
          are dropped; those are agent mechanics shown live in the Trace panel,
          never as chat bubbles.
        - The operator app's injected **context wrappers** (``[DATABASE SCOPE]`` /
          ``[REPORT CONTEXT]`` / ``[INVESTIGATION CONTEXT]``) are stripped from the
          stored user prompt so only the user's real text remains.

        Each stored turn is a JSON envelope written by the Strands AgentCore
        session manager:
        ``{"message": {"role", "content": [blocks]}, "metadata": {...}, ...}``.
        """
        memory_id = _memory_id()
        if not memory_id or not actor_id or not session_id:
            return []
        try:
            events = self._list_session_events(memory_id, actor_id, session_id)
        except Exception as e:  # noqa: BLE001 — memory must never break a reopen
            logger.warning("memory read failed for %s/%s: %s", actor_id, session_id, e)
            return []

        messages = []
        for ev in events:
            for payload in ev.get("payload", []) or []:
                conv = payload.get("conversational") if isinstance(payload, dict) else None
                if not conv:
                    continue  # agent/session state blob — not part of the conversation
                stored = (conv.get("content") or {}).get("text", "") or ""
                inner = _unwrap_stored_message(stored)
                if inner is None:
                    # Defensive fallback: not the known envelope → treat as plain text.
                    role = (conv.get("role") or "").upper()
                    clean = _strip_injected_context(stored)
                    if clean and role in ("USER", "ASSISTANT"):
                        messages.append({"role": role.lower(), "content": clean})
                    continue

                role = (inner.get("role") or conv.get("role") or "").upper()
                if role not in ("USER", "ASSISTANT"):
                    continue
                blocks = inner.get("content") or []
                # Keep the assistant's answer text even when the SAME message also
                # carries toolUse/toolResult blocks — a streamed turn interleaves the
                # answer with its tool calls. Extract only the text blocks below; a
                # pure-tool message yields empty text and is dropped by the check after
                # the join. Extracting per block — rather than dropping any message that
                # carries a tool block — keeps the analysis in a reopened or refreshed
                # chat, instead of leaving only a later tool-free reply.
                text = "\n\n".join(
                    b["text"] for b in blocks
                    if isinstance(b, dict) and isinstance(b.get("text"), str) and b["text"].strip()
                ).strip()
                if not text:
                    continue
                if role == "USER":
                    text = _strip_injected_context(text)
                    if not text:
                        continue
                    messages.append({"role": "user", "content": text})
                else:
                    msg = {"role": "assistant", "content": text}
                    # Rebuild the investigation-suggestion card: it is persisted as
                    # a recommend_investigation toolUse block, so a rehydrated chat
                    # can show the same card the live stream did (the skill pill is
                    # NOT persisted and is intentionally not restored).
                    sugg = _suggestion_blocks(blocks)
                    if sugg:
                        msg["blocks"] = sugg
                    messages.append(msg)

        return messages[-k:] if k and len(messages) > k else messages

    # ── Long-term memory records (Phase C) ──────────────────────────────────
    # Records the agent extracted about the user (facts + preferences). The
    # Cognito sub is baked into the namespace, so reads/deletes are structurally
    # scoped to the caller. Summaries (per-session) are intentionally excluded —
    # the Recents list already surfaces conversations.
    def _namespaces_for(self, actor_id: str) -> list:
        return [f"/facts/{actor_id}/", f"/preferences/{actor_id}/"]

    def list_memories(self, actor_id: str, query: str = None, top_k: int = 20) -> list:
        """List (or semantically search) the caller's long-term memories.

        With ``query`` set, results are relevance-ranked across the caller's
        namespaces; otherwise they're listed newest-first. Returns an empty list
        when memory is unconfigured or on any error (never raises).
        """
        memory_id = _memory_id()
        if not memory_id or not actor_id:
            return []
        top_k = max(1, min(int(top_k or 20), 100))
        out = []
        for ns in self._namespaces_for(actor_id):
            try:
                if query:
                    resp = self.dp.retrieve_memory_records(
                        memoryId=memory_id,
                        namespace=ns,
                        searchCriteria={"searchQuery": query, "topK": top_k},
                        maxResults=top_k,
                    )
                else:
                    resp = self.dp.list_memory_records(
                        memoryId=memory_id, namespace=ns, maxResults=top_k
                    )
                for r in resp.get("memoryRecordSummaries", []):
                    out.append(self._record(r))
            except Exception as e:  # noqa: BLE001 — memory must never break the page
                logger.warning("memory list failed for %s: %s", ns, e)
        if query:
            out.sort(key=lambda x: x.get("score") or 0.0, reverse=True)
            return out[:top_k]
        out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        return out

    def delete_memory(self, actor_id: str, record_id: str) -> bool:
        """Delete one of the caller's memory records.

        Verifies ownership first (the caller's sub must appear in the record's
        namespaces) so a user can only delete their own memories. Returns False
        when not found / not owned / on error.
        """
        memory_id = _memory_id()
        if not memory_id or not actor_id or not record_id:
            return False
        try:
            resp = self.dp.get_memory_record(memoryId=memory_id, memoryRecordId=record_id)
            record = resp.get("memoryRecord") or resp.get("memoryRecordSummary") or resp
            namespaces = record.get("namespaces") or []
            if not any(actor_id in (ns or "") for ns in namespaces):
                logger.warning("memory delete denied (not owner) for record %s", record_id)
                return False
            self.dp.delete_memory_record(memoryId=memory_id, memoryRecordId=record_id)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("memory delete failed for %s: %s", record_id, e)
            return False

    def _record(self, r: dict) -> dict:
        """Normalise a MemoryRecordSummary into a UI-friendly dict.

        The User-Preference strategy stores a structured object as the record
        text — ``{"context", "preference", "categories"}`` — so we surface the
        human-readable ``preference`` and expose ``categories`` as tags. Facts /
        summaries store plain prose and pass through unchanged.
        """
        namespaces = r.get("namespaces") or []
        joined = " ".join(namespaces)
        kind = "preference" if "/preferences/" in joined else "summary" if "/summaries/" in joined else "fact"
        created = r.get("createdAt")
        content_text = _extract_text(r.get("content"))
        categories = []
        parsed = _loads_obj(content_text)
        if parsed is not None and ("preference" in parsed or "categories" in parsed):
            pref = parsed.get("preference") or parsed.get("context") or ""
            if pref:
                content_text = pref
            cats = parsed.get("categories")
            if isinstance(cats, list):
                categories = [str(c).strip() for c in cats if c and str(c).strip()]
        return {
            "id": r.get("memoryRecordId"),
            "content": content_text,
            "type": kind,
            "categories": categories,
            "score": r.get("score"),
            "created_at": created.isoformat() if hasattr(created, "isoformat") else (created or None),
            "strategy_id": r.get("memoryStrategyId"),
        }


def _extract_text(content) -> str:
    """Pull display text out of an AgentCore conversational ``content`` value."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        # AgentCore conversational payload: {"text": "..."}
        return content.get("text") or content.get("content") or ""
    return str(content)


def _loads_obj(text: str):
    """Parse a JSON object string into a dict, or None if it isn't one."""
    if not text or not text.lstrip().startswith("{"):
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _suggestion_blocks(blocks: list) -> list:
    """Reconstruct investigation-suggestion card(s) from an assistant turn's
    stored recommend_investigation toolUse block(s). Returns a list of
    ``{type, title, rationale}`` (empty when the turn had no suggestion)."""
    out = []
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        tu = b.get("toolUse")
        if isinstance(tu, dict) and tu.get("name") == "recommend_investigation":
            inp = tu.get("input") if isinstance(tu.get("input"), dict) else {}
            out.append({
                "type": "investigation_suggestion",
                "title": (inp.get("title") or "").strip(),
                "rationale": (inp.get("rationale") or "").strip(),
            })
    return out


def _unwrap_stored_message(text: str):
    """Return the inner Strands message dict from a stored conversational turn.

    The Strands AgentCore session manager persists each turn as a JSON string:
    ``{"message": {"role", "content": [blocks]}, "metadata": {...}, ...}``. Return
    the inner ``message`` dict (``{role, content:[blocks]}``), or None when ``text``
    isn't that envelope (so the caller can fall back to plain-text handling).
    """
    obj = _loads_obj(text)
    if obj is None:
        return None
    msg = obj.get("message")
    if isinstance(msg, dict) and "content" in msg:
        return msg
    if "role" in obj and "content" in obj:
        return obj
    return None


# Context wrappers the operator app injects into the prompt before sending it to
# the agent. The live chat only ever rendered the user's raw text, but the agent
# persists the wrapped prompt — so strip the wrappers on read to keep a rehydrated
# transcript identical to what the user saw. Mirrors the markers built in
# frontend/src/components/Chat.jsx (handleSend).
_CONTEXT_WRAPPER_RE = re.compile(
    r"^\s*\[(?:DATABASE SCOPE|REPORT CONTEXT|INVESTIGATION CONTEXT)\]"
    r".*?"
    r"\[END (?:DATABASE SCOPE|REPORT CONTEXT|INVESTIGATION CONTEXT)\]\s*"
    r"(?:User (?:question about this report|follow-up question):\s*)?",
    re.DOTALL,
)
_INVESTIGATION_PREFIX_RE = re.compile(r"^\s*\[INVESTIGATION:[^\]]*\]\s*")


def _strip_injected_context(text: str) -> str:
    """Remove operator-app context wrappers from a stored user prompt."""
    if not text:
        return ""
    text = _CONTEXT_WRAPPER_RE.sub("", text)
    text = _INVESTIGATION_PREFIX_RE.sub("", text)
    return text.strip()


memory_reader = MemoryReader()
