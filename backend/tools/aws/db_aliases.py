# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Per-request database aliases: the handle the model uses for a database.

The model-attached guardrail keeps its ``AWS_SECRET_KEY`` PII filter (it stops the agent
repeating an AWS secret key found in customer data, query results or logs). That filter
also matches pieces of the database-id UUIDs, so if the ``[DATABASE SCOPE]`` block handed
UUIDs to the model it would replace them and the ids the coordinator passed to its
specialists would arrive garbled (``{AWS_SECRET_KEY}09-9099{AWS_SECRET_KEY}``). So the
scope keeps UUIDs out of the model's view entirely and the model never sees a
UUID: the scope names each database ``db-1``, ``db-2``, ... and /invocations keeps the
alias -> registry id mapping server-side, in ``model_resolver.CURRENT_DB_ALIASES``.

``resolve_handle`` is the one place a tool handle is translated back. Every resolver of
a model-supplied database handle calls it first (``pg_client.resolve_credential`` and
``get_connection``, ``session_factory._lookup_db_record``, and the db_tools that read the
registry by id). Inside an agent request:

  * an alias in the request's mapping resolves to its registry id;
  * any other alias is refused: "Use a database alias from [DATABASE SCOPE]." An alias
    from another request or another space is simply not in this request's mapping;
  * a raw UUID is still accepted for now, only when it is in scope: when the request has
    a mapping, the UUID must be one of its ids; without one (a prompt that carried its
    own scope block), the callers' existing space checks decide, as before;
  * anything else (a display name, an ARN) passes through to the caller's own rules,
    which refuse a raw ARN and resolve a display name within the caller's space.

Outside an agent request (Tier 1, the batch scan, the REST handlers) nothing changes:
the value is returned as given.
"""
import logging
import re

logger = logging.getLogger("db-aliases")

ALIAS_RE = re.compile(r"^db-[1-9][0-9]{0,4}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
REFUSAL = "Use a database alias from [DATABASE SCOPE]."


def _request_context():
    """``(user, aliases)`` of the current agent request; ``('', None)`` outside one.
    ``aliases`` is THIS turn's in-scope mapping (the one that resolves).
    Imported lazily, as pg_client does, so a context without model_resolver works."""
    try:
        from model_resolver import CURRENT_DB_ALIASES, CURRENT_USER_ID
        return CURRENT_USER_ID.get() or "", CURRENT_DB_ALIASES.get()
    except Exception:  # noqa: BLE001 - no request context
        return "", None


def _session_aliases():
    """The whole session's alias -> id mapping (stable across turns), or None. Used only
    to tell a known-but-out-of-scope alias from an unknown one."""
    try:
        from model_resolver import CURRENT_SESSION_ALIASES
        return CURRENT_SESSION_ALIASES.get()
    except Exception:  # noqa: BLE001
        return None


def scoped_ids():
    """The registry ids this turn is scoped to — the request's alias-map values — or
    None outside an agent request / when no server-built scope was sent. Lets a tool that
    would otherwise list every database in the space honor the user's current selection
    (e.g. 2 of 10 picked) instead, so the answer covers only the chosen databases and each
    one still carries its alias rather than a raw id."""
    user, aliases = _request_context()
    if not user or not aliases:
        return None
    ids = [rid for rid in aliases.values() if rid]
    return ids or None


def is_alias(value) -> bool:
    return isinstance(value, str) and bool(ALIAS_RE.match(value.strip().lower()))


def alias_for(registry_id):
    """The request's alias for ``registry_id``, or the id unchanged when it has none
    (no agent request, no server-built scope, or a database outside it). Used where a
    tool result would otherwise hand the model a registry id."""
    if not isinstance(registry_id, str) or not registry_id:
        return registry_id
    user, aliases = _request_context()
    if not user or not aliases:
        return registry_id
    for alias, rid in aliases.items():
        if rid == registry_id:
            return alias
    return registry_id


def resolve_handle(value):
    """The registry id behind a model-supplied database handle (see the module doc).
    Raises ``PermissionError(REFUSAL)`` for an alias or UUID outside the request's scope.
    Non-string and empty values are returned unchanged."""
    if not isinstance(value, str) or not value.strip():
        return value
    user, aliases = _request_context()
    if not user:
        return value
    v = value.strip()
    if is_alias(v):
        rid = (aliases or {}).get(v.lower())
        if rid:
            return rid
        # Stable per-session aliases: an alias assigned earlier in this session but not in
        # THIS turn's selection names a database the user did not pick now. Say so,
        # distinctly from an alias that was never assigned, so the model does not query
        # the wrong database or treat it as a typo.
        session = _session_aliases()
        if session and v.lower() in session:
            logger.warning("audit: database alias %s is not in the current scope (user=%s)", v, user)
            raise PermissionError(
                f"{v.lower()} is not in the current scope. It was used earlier in this chat but is "
                "not among the databases selected for this turn; ask the user to select it again, "
                "or use an alias from the current [DATABASE SCOPE].")
        logger.warning("audit: database alias %s is not in this request's scope (user=%s)", v, user)
        raise PermissionError(REFUSAL)
    if aliases is not None and _UUID_RE.match(v):
        if v not in set(aliases.values()):
            logger.warning("audit: database id outside this request's scope refused (user=%s)", user)
            raise PermissionError(REFUSAL)
        return v
    return value
