# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for streaming.remediation_screen — the advisory destructive-SQL caution.

The screen NEVER blocks or edits an answer; it only reports cautions for destructive
statements an answer presents as RUNNABLE (inside a fenced code block). The decisive
property is scope: SQL quoted inline in prose, which is how the agent discusses a
customer's own statements while investigating them, is never flagged. These tests pin
that so a future change cannot start flagging analysis.

Hermetic: pure module, reads the real references.yaml#remediation-safety policy.
Run:  python3 backend/tests/test_remediation_screen.py
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# The streaming package __init__ pulls in event_parser -> tools.types -> strands. The
# screen itself needs none of it (pure stdlib + yaml), so stub strands to import cleanly.
_strands = types.ModuleType("strands")
_strands.tool = lambda f: f
_strands.Agent = object
sys.modules.setdefault("strands", _strands)

from streaming import remediation_screen as rs  # noqa: E402


def _fence(sql, lang="sql"):
    return f"Here is the fix:\n\n```{lang}\n{sql}\n```\n"


# ── what MUST be cautioned (destructive, runnable, unguarded) ───────────────────

def test_bare_delete_is_cautioned():
    out = rs.screen(_fence("DELETE FROM customers;"))
    assert len(out) == 1, out
    assert out[0]["reason"] == "no_where" and out[0]["verb"] == "DELETE"
    assert out[0]["target"] == "customers"
    assert "every row in `customers`" in out[0]["message"]
    assert "SELECT count(*)" in out[0]["message"] and "snapshot" in out[0]["message"]


def test_update_without_where_is_cautioned():
    out = rs.screen(_fence("UPDATE orders SET status = 'void';"))
    assert len(out) == 1 and out[0]["reason"] == "no_where" and out[0]["target"] == "orders", out


def test_drop_is_cautioned():
    out = rs.screen(_fence("DROP TABLE audit;"))
    assert len(out) == 1 and out[0]["reason"] == "no_safe_form" and out[0]["verb"] == "DROP", out
    assert "DROP audit" in out[0]["message"] and "cannot be undone" in out[0]["message"]


def test_truncate_is_cautioned():
    out = rs.screen(_fence("TRUNCATE TABLE events;"))
    assert len(out) == 1 and out[0]["reason"] == "no_safe_form" and out[0]["verb"] == "TRUNCATE", out
    assert "empties the table" in out[0]["message"]


def test_target_fallback_when_unparsable():
    # A DELETE whose target cannot be parsed still cautions, worded generically.
    out = rs.screen(_fence("DELETE customers;"))
    assert len(out) == 1 and out[0]["reason"] == "no_where" and out[0]["target"] == "", out
    assert "every row in the target table" in out[0]["message"]


# ── what MUST NOT be cautioned (analysis, scoped, or not runnable) ──────────────

def test_scoped_delete_is_clean():
    assert rs.screen(_fence("DELETE FROM sessions WHERE expired;")) == []


def test_scoped_update_is_clean():
    assert rs.screen(_fence("UPDATE orders SET status = 'void' WHERE id = 5;")) == []


def test_delete_in_prose_is_never_inspected():
    # The agent discussing a customer's own unguarded delete inline. No fence => no caution,
    # even though the statement has no WHERE. This is the investigate-a-DELETE case.
    prose = "Your slow statement `DELETE FROM orders` is doing a sequential scan on 40M rows."
    assert rs.screen(prose) == []


def test_explain_is_not_a_fix():
    # Leading verb is EXPLAIN (a read-only plan), so the DELETE inside is not a proposed fix.
    assert rs.screen(_fence("EXPLAIN DELETE FROM customers;")) == []


def test_destructive_verb_inside_a_comment_is_ignored():
    block = "SELECT 1;\n-- do NOT run TRUNCATE events here\n/* DROP TABLE audit */"
    assert rs.screen(_fence(block)) == []


def test_select_only_block_is_clean():
    assert rs.screen(_fence("SELECT count(*) FROM customers WHERE active;")) == []


# ── shape: dedup and multiplicity ───────────────────────────────────────────────

def test_distinct_statements_each_caution():
    out = rs.screen(_fence("DELETE FROM customers;\nDELETE FROM orders;\nDROP TABLE audit;"))
    assert len(out) == 3, out
    assert {c["target"] for c in out} == {"customers", "orders", "audit"}


def test_identical_statements_dedupe():
    out = rs.screen(_fence("DELETE FROM customers;\nDELETE FROM customers;"))
    assert len(out) == 1, out


# ── robustness: never raise, never touch the answer ─────────────────────────────

def test_empty_and_none_are_safe():
    assert rs.screen("") == []
    assert rs.screen(None) == []
    assert rs.screen("no code here at all") == []


def test_policy_loads_from_references_yaml():
    # Proves the verbs come from references.yaml#remediation-safety, not a hardcoded set.
    pol = rs._policy()
    assert "DROP" in [v.upper() for v in pol.get("never_runnable", [])]
    assert "DELETE" in [v.upper() for v in pol.get("require_where", [])]


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  ok   {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {e!r}")
    print(f"pass={passed} fail={failed}")
    sys.exit(1 if failed else 0)
