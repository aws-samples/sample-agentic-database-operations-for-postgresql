# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Server-side ``[DATABASE SCOPE]`` blocks.
The orchestrator and investigation agents learn *which* databases a turn is about from a
text block at the top of the prompt: display name, engine, Region, the target database
name and the endpoints, each database named by a short per-request ALIAS (``db-1``,
``db-2``, ...). The alias is the handle every tool takes (in the fields named
``secret_arn`` and ``db_instance_id``); the request keeps the alias -> registry id
mapping server-side (``model_resolver.CURRENT_DB_ALIASES``, set by /invocations from
``alias_map`` over the same records) and the tools translate it back
(``tools/aws/db_aliases.py``). The model never sees a registry id, a UUID or a credential
pointer: the model guardrail's ``AWS_SECRET_KEY`` filter, which stays on, would garble
pieces of any UUID, so the block carries none.
The block is built here, on the server, and this is the only shape that keeps the
operator app the same for everyone: the caller names databases by **id**, the server
verifies each id belongs to the caller's space, and the credential itself is resolved per
calling space inside ``pg_client.resolve_credential`` at connection time. Nothing about
the turn depends on what the caller's own listing happened to contain. These functions
are pure (records in, text out); the membership check lives with the other gates in
``app.py`` (``_scope_records``).
The field names are the ones the agents' prompts quote, so the two must stay matched.
"""
import re
from typing import Iterable, List, Optional

# Aurora role focus values accepted from any surface. ``instance:<db-instance-id>`` is
# validated separately (see ``/invocations``), the others are a closed set.
ROLE_FOCUS_VALUES = ("", "writer", "reader")
# An RDS DB instance identifier as a focus or member selector: letters, digits and
# hyphens, 1-63 characters (lower-cased first). A selector, never a host: the endpoint
# always comes from the registry's cluster_member_roles.
MEMBER_ID_RE = re.compile(r"[a-z0-9-]{1,63}")


def assign_aliases(records: Iterable[dict]) -> List[tuple]:
    """``[(alias, record), ...]`` for the databases in scope: ``db-1``, ``db-2``, ... in a
    deterministic order (display name, case-insensitive, then registry id), so the same
    set of records always gets the same aliases, in whatever order the caller listed
    them. A record listed twice gets one alias. This is the ONE place aliases are
    assigned: the scope blocks and ``alias_map`` both call it."""
    seen, recs = set(), []
    for r in records or []:
        if not r:
            continue
        rid = r.get("instance_id", "")
        if rid in seen:
            continue
        seen.add(rid)
        recs.append(r)
    recs.sort(key=lambda r: ((r.get("display_name") or r.get("instance_id", "")).lower(),
                             r.get("instance_id", "")))
    return [(f"db-{i}", r) for i, r in enumerate(recs, 1)]


def alias_map(records: Iterable[dict]) -> dict:
    """The request's ``{alias: registry id}`` mapping for ``records``, matching the
    aliases the scope block shows. Kept server-side (``CURRENT_DB_ALIASES``), never in
    model-visible text."""
    return {alias: r.get("instance_id", "") for alias, r in assign_aliases(records)}


_ALIAS_INDEX = re.compile(r"^db-([1-9][0-9]{0,4})$")


def stable_aliases(records: Iterable[dict], existing: Optional[dict] = None):
    """Per-SESSION stable aliases. Returns ``(named, mapping)``:

    * ``named`` is ``[(alias, record)]`` for the in-scope records, each carrying the alias
      it already holds in this session, in alias order;
    * ``mapping`` is the full session ``{alias: id}`` after adding any databases that
      appear for the first time.

    A database keeps its alias for the whole session: an alias is assigned the first time
    a database appears and is never reassigned to a different database or reused for a
    retired one, so ``db-2`` always names the same database across turns even when the
    selection changes. New databases take the next free ``db-N`` after the highest already
    used, in a deterministic order (display name, then id) so a batch is itself stable."""
    existing = {a: rid for a, rid in (existing or {}).items() if _ALIAS_INDEX.match(a) and rid}
    by_id = {}
    for a, rid in existing.items():
        by_id.setdefault(rid, a)
    seen, recs = set(), []
    for r in records or []:
        if not r:
            continue
        rid = r.get("instance_id", "")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        recs.append(r)
    new = sorted((r for r in recs if r.get("instance_id", "") not in by_id),
                 key=lambda r: ((r.get("display_name") or r.get("instance_id", "")).lower(),
                                r.get("instance_id", "")))
    nxt = max((int(_ALIAS_INDEX.match(a).group(1)) for a in existing), default=0) + 1
    mapping = dict(existing)
    for r in new:
        alias = f"db-{nxt}"
        rid = r.get("instance_id", "")
        mapping[alias] = rid
        by_id[rid] = alias
        nxt += 1
    named = sorted(((by_id[r.get("instance_id", "")], r) for r in recs),
                   key=lambda ar: int(ar[0].split("-")[1]))
    return named, mapping


def _scope_block_from_named(named: List[tuple], role_focus: str, selected: bool) -> str:
    """Build the chat ``[DATABASE SCOPE]`` block from a pre-assigned ``[(alias, record)]``
    list (so the caller controls the aliases, e.g. the stable per-session ones)."""
    rows = [scope_entry(r, alias) for alias, r in named]
    if not rows:
        return ""
    lead = "The user has selected" if selected else "No specific selection — using all"
    focus_record = named[0][1] if len(named) == 1 else None
    focus = role_focus_block(role_focus, focus_record)
    if not focus and any(r.get("cluster_member_roles") for _, r in named):
        focus = MEMBER_NOTE
    return (
        f"[DATABASE SCOPE] {lead} database(s). Use this context instead of calling "
        f"list_monitored_databases. {ALIAS_NOTE}\n" + "\n".join(rows)
        + focus
        + "\n[END DATABASE SCOPE]"
    )


def scope_entry(inst: dict, alias: str) -> str:
    """One database line of the chat-style scope block, from a registry record.

    ``secret_arn`` and ``db_instance_id`` both carry the database's ALIAS for this
    request, never a Secrets Manager ARN or a registry id: the specialist tools pass it
    through unchanged and the resolvers turn it, server-side, into the registry id and
    the credential the calling space is entitled to. The model therefore never sees an
    ARN or a UUID, so none can appear in text, traces or memory, and the guardrail's
    secret-key filter has nothing of ours to garble. The keys keep their historical
    names so the tool wiring and prompts stay unchanged."""
    name = inst.get("display_name") or alias
    parts = [
        f"engine={inst.get('engine', '')}",
        f"region={inst.get('region') or ''}",
        f"secret_arn={alias}",
        f"database={inst.get('database_name') or 'postgres'}",
        f"endpoint={inst.get('endpoint') or ''}",
    ]
    if inst.get("reader_endpoint"):
        parts.append(f"reader_endpoint={inst['reader_endpoint']}")
    roles = inst.get("cluster_member_roles") or []
    if roles:
        # Current roles: the Tier 1 collector re-reads them from RDS on every run.
        ordered = sorted(roles, key=lambda m: (not m.get("is_writer"), m.get("instance_id", "")))
        parts.append("members=" + ",".join(
            f"{'writer' if m.get('is_writer') else 'reader'}:{m.get('instance_id', '')}" for m in ordered))
    parts.append(f"db_instance_id={alias}")
    return f"- {alias}: {name} ({', '.join(parts)})"


ALIAS_NOTE = ("Each database is named by its alias (db-1, db-2, ...). Pass the alias "
              "wherever a tool or a specialist asks for secret_arn, db_instance_id, instance_id "
              "or identifier.")


FOLLOWS = ("Tier 1 history (get_historical_metrics), deep and reader scans "
           "(get_report_analysis) and Performance Insights follow this focus.")

# Appended to the scope block when a cluster is in scope and the operator has not
# focused a member: says which member the alias reaches and how a member is chosen,
# so the model neither guesses from the connection nor reports member targeting as
# impossible. The selector is the operator's, not the model's.
MEMBER_NOTE = (
    "\n[MEMBERS] The operator has the cluster as a whole in focus (the Endpoint menu in the "
    "database picker is on \"Cluster\"). Metrics, Tier 1 history and stored scans cover "
    "every member, but a SQL connection reaches one instance: every SQL tool on a cluster "
    "alias connects through the cluster endpoint, which is the writer, so pg_stat_* figures "
    "are the writer's. No tool argument changes that; the operator picks a member in the "
    "Endpoint menu (Writer, or Reader for one reader instance). When a question needs a "
    "reader's own pg_stat_* figures, give the writer's result, say it is the writer's, and "
    "ask the operator to choose that reader in the Endpoint menu and ask again.\n[END MEMBERS]"
)


def role_focus_block(role_focus: str, record: Optional[dict] = None) -> str:
    """The ``[ROLE FOCUS]`` paragraph for a DB cluster writer/reader/instance focus (Aurora, or an
    RDS Multi-AZ DB cluster, whose readers are streaming standbys) ('' when
    there is none). Text is the operator app's, kept verbatim so every surface steers the
    agent identically. `record` (the one database in scope) names an instance focus's
    CURRENT role, which a failover can change."""
    focus = (role_focus or "").strip().lower()
    if focus.startswith("instance:"):
        member = focus[len("instance:"):]
        roles = {m.get("instance_id"): m for m in (record or {}).get("cluster_member_roles") or []}
        if record is not None and member not in roles:
            return (
                f"\n[ROLE FOCUS] The user focused this conversation on \"{member}\", which is not a "
                f"current member of the database cluster in scope; analyse the cluster endpoint and "
                f"say so.\n[END ROLE FOCUS]"
            )
        role = ("writer" if roles[member].get("is_writer") else "reader") if member in roles else ""
        current = f", currently the {role}" if role else ""
        lead = ("writer symptoms (vacuum, WAL, write IOPS, lock contention)" if role == "writer"
                else "reader symptoms (replica lag, read load, connection distribution)")
        return (
            f"\n[ROLE FOCUS] The user focused this conversation on ONE instance of the database "
            f"cluster in scope: \"{member}\"{current}. Analyze that member only: filter "
            f"role/instance-labeled metrics to this instance by name, lead with {lead}. "
            f"SQL-internal views (pg_stat_*) come from THIS instance's own endpoint — "
            f"instance-specific claims are safe for this member. {FOLLOWS}"
            f"\n[END ROLE FOCUS]"
        )
    if focus == "reader":
        return (
            "\n[ROLE FOCUS] The user focused this conversation on the READER instances of the database "
            "cluster in scope (Aurora or RDS Multi-AZ DB cluster). Analyze reader-role members: filter role-labeled metrics to "
            "role=reader, lead with reader symptoms (replica lag, read load, connection "
            "distribution). SQL-internal views (pg_stat_*) come from a READER connection via the "
            "cluster reader endpoint (round-robin when multiple readers exist — say so if "
            "instance-specific claims matter). " + FOLLOWS + "\n[END ROLE FOCUS]"
        )
    if focus == "writer":
        return (
            "\n[ROLE FOCUS] The user focused this conversation on the WRITER instance of the database "
            "cluster in scope. Analyze the writer member: filter role-labeled metrics to "
            "role=writer, lead with writer symptoms (vacuum, WAL, write IOPS, lock contention). "
            "SQL-internal views (pg_stat_*) come from the WRITER connection. " + FOLLOWS
            + "\n[END ROLE FOCUS]"
        )
    return ""


# The investigation surface has no Endpoint menu: its SQL tools always reach the writer,
# and a reader's own pg_stat_* figures are checked in the chat with the Endpoint menu.
INVESTIGATION_MEMBER_NOTE = (
    "\nSQL tools on a cluster alias reach the writer; a reader's own pg_stat_* figures are "
    "not available in an investigation. Report them as not verified and name the chat's "
    "Endpoint menu in the database picker (Reader) as the way to check them."
)


def chat_scope_block(records: Iterable[dict], role_focus: str = "", selected: bool = True) -> str:
    """The chat surface's scope block for ``records`` (registry rows, credential pointer
    included). ``selected`` distinguishes an explicit pick from the "all databases"
    default, as the web app's wording does. Empty when there are no records."""
    return _scope_block_from_named(assign_aliases(records), role_focus, selected)


def chat_scope_block_stable(records: Iterable[dict], existing: Optional[dict], role_focus: str = "",
                            selected: bool = True):
    """The chat scope block using STABLE per-session aliases. Returns
    ``(block, in_scope_map, session_map)``: the block text, this turn's ``{alias: id}``
    (what resolves), and the full session mapping to persist (and to recognise a
    known-but-out-of-scope alias)."""
    named, mapping = stable_aliases(records, existing)
    block = _scope_block_from_named(named, role_focus, selected)
    in_scope = {alias: r.get("instance_id", "") for alias, r in named}
    return block, in_scope, mapping


def investigation_scope_block(records: Iterable[dict]) -> str:
    """The investigation surface's scope block: the databases the run may touch, each
    named by its alias (``assign_aliases``, so a later request of the same investigation
    rebuilds the same aliases from the same records). The space is not written into the
    block: ``get_runbooks`` on this surface reads the request's validated space itself."""
    rows = []
    for alias, r in assign_aliases(records):
        name = r.get("display_name") or alias
        rows.append(
            f"- {alias}: {name}, db_identifier={alias}, secret_arn={alias}, "
            f"engine={r.get('engine') or 'postgresql'}, database={r.get('database_name') or 'postgres'}, "
            f"account_id={r.get('account_id') or 'N/A'}, region={r.get('region') or 'us-east-1'}"
        )
    if not rows:
        return ""
    recs = list(records)
    members = INVESTIGATION_MEMBER_NOTE if any(r.get("cluster_member_roles") for r in recs) else ""
    return (
        f"[DATABASE SCOPE: Investigate ONLY these {len(rows)} databases. {ALIAS_NOTE}\n"
        + "\n".join(rows) + members + "\nDo NOT investigate any other databases.]"
    )


def has_scope_block(prompt: str) -> bool:
    """True when the caller already embedded a scope block (an older web app build), so
    the server must not add a second one."""
    return "[DATABASE SCOPE" in (prompt or "")


def compose_chat_prompt(message: str, records: Optional[List[dict]], role_focus: str = "",
                        selected: bool = True) -> str:
    """The full chat turn: scope block (when there are records) followed by the message."""
    block = chat_scope_block(records or [], role_focus, selected)
    return f"{block}\n\n{message}" if block else message
