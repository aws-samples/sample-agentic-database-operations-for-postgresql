# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Advisory screen that flags destructive SQL an answer presents as runnable.

The agent connects read-only (it cannot execute changes), so the risk this guards is
narrow: the agent *presenting* a reckless, runnable statement that a human then copies
into a writable session. This screen does NOT block or edit the answer. It returns
cautions that the stream appends as one extra NDJSON line, which the UI shows beside the
answer.

The scoping rule is the whole point: only fenced code blocks (the "copy this and run it"
affordance) are inspected. SQL the agent quotes INLINE in prose — how it discusses a
customer's own statements while investigating them — is never flagged. So investigating a
DELETE is unaffected; a caution appears only for a runnable, unguarded destructive
statement in a fenced block.

What is flagged comes from references.yaml#remediation-safety (data, not code here):
  * never_runnable verbs (DROP, TRUNCATE): always cautioned in a run block;
  * require_where verbs (DELETE, UPDATE): cautioned only when the statement has no WHERE.

Pure/stdlib (no strands, no app import) so it is unit-testable on its own.
"""
import re
from pathlib import Path

# A fenced code block: ```lang\n ... ``` or ``` \n ... ```. The language tag, if any,
# is on the fence line and excluded from the captured body.
_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)

# Best-effort target (table) per verb. Advisory only — a miss falls back to no name.
_TARGET = {
    "DELETE": re.compile(r"\bFROM\s+(?:ONLY\s+)?([\w.\"]+)", re.I),
    "UPDATE": re.compile(r"\bUPDATE\s+(?:ONLY\s+)?([\w.\"]+)", re.I),
    "DROP": re.compile(r"\bDROP\s+\w+\s+(?:IF\s+EXISTS\s+)?([\w.\"]+)", re.I),
    "TRUNCATE": re.compile(r"\bTRUNCATE\s+(?:TABLE\s+)?(?:ONLY\s+)?([\w.\"]+)", re.I),
}

_WHERE = re.compile(r"\bWHERE\b", re.I)
_WORD = re.compile(r"[A-Za-z_]+")

_POLICY = None


def _policy() -> dict:
    """Load references.yaml#remediation-safety.thresholds, cached. Empty on any read
    failure, which fails SOFT (no caution) rather than raising — this is an advisory."""
    global _POLICY
    if _POLICY is None:
        try:
            import yaml
            path = Path(__file__).resolve().parents[1] / "checks" / "references.yaml"
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            _POLICY = (data.get("remediation-safety", {}) or {}).get("thresholds", {}) or {}
        except Exception:  # noqa: BLE001 - advisory: never let config break the stream
            _POLICY = {}
    return _POLICY


def _strip_comments(sql: str) -> str:
    """Drop SQL comments so a verb/WHERE inside a comment is never read as code and a
    `;` inside a comment never splits a statement."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)   # block comments
    sql = re.sub(r"--[^\n]*", " ", sql)                # line comments
    return sql


def _statements(block: str):
    for part in _strip_comments(block).split(";"):
        part = part.strip()
        if part:
            yield part


def _leading_verb(stmt: str) -> str:
    """First SQL keyword, uppercased. `EXPLAIN DELETE ...` reads as EXPLAIN (a read-only
    plan, never flagged); `WITH ... DELETE` reads as WITH (not flagged — advisory accepts
    that rare miss rather than parsing CTEs)."""
    m = _WORD.search(stmt)
    return m.group(0).upper() if m else ""


def _has_where(stmt: str) -> bool:
    """True if a WHERE appears anywhere. A WHERE only inside a subquery counts here, so
    the screen errs toward NOT cautioning — the safe direction for an advisory (fewer
    false alarms); the bare `DELETE FROM t` case it exists to catch has no WHERE at all."""
    return bool(_WHERE.search(stmt))


def _target(stmt: str, verb: str) -> str:
    rx = _TARGET.get(verb)
    if not rx:
        return ""
    m = rx.search(stmt)
    return m.group(1).strip().rstrip(",;") if m else ""


def _message(verb: str, reason: str, target: str) -> str:
    """The operator-facing caution. States the effect and what to do (never a raw flag),
    and that the agent cannot run it — the statement is the human's decision."""
    if reason == "no_where":
        where = f"`{target}`" if target else "the target table"
        return (f"This `{verb}` has no WHERE clause, so it affects every row in {where}. "
                "The agent cannot run it. Before anyone does: add a predicate that names "
                "exactly the rows to change, run `SELECT count(*)` with that same predicate "
                "to confirm the count, take a snapshot, and test on a non-production copy first.")
    head = f"{verb} {target}".strip()
    if verb == "TRUNCATE":
        effect = "empties the table of all rows and cannot be rolled back once committed"
    else:  # DROP
        effect = "removes the object and all its data, and cannot be undone without a backup"
    return (f"This `{head}` {effect}. The agent cannot run it. Confirm nothing depends on it, "
            "take a snapshot, and test on a non-production copy first.")


def screen(answer: str) -> list:
    """Return a list of caution dicts for destructive statements the answer presents as
    runnable (in fenced code blocks). Empty when there are none. Never raises on bad
    input and never touches the answer text."""
    policy = _policy()
    never = {v.upper() for v in policy.get("never_runnable", [])}
    needs_where = {v.upper() for v in policy.get("require_where", [])}
    if not (never or needs_where):
        return []

    out, seen = [], set()
    for block in _FENCE.findall(answer or ""):
        for stmt in _statements(block):
            verb = _leading_verb(stmt)
            if verb in never:
                reason = "no_safe_form"
            elif verb in needs_where and not _has_where(stmt):
                reason = "no_where"
            else:
                continue
            target = _target(stmt, verb)
            key = (verb, reason, target)
            if key in seen:
                continue
            seen.add(key)
            out.append({"verb": verb, "reason": reason, "target": target,
                        "message": _message(verb, reason, target)})
    return out
