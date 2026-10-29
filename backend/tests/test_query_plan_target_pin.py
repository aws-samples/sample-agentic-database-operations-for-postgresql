# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""get_query_plan target-database pin + connection identity/audit.

get_query_plan is the one tool that accepts BOTH model-authored SQL and a model-chosen
database. It must pin the target to the record this credential belongs to: allow the
record's own database and the standard `postgres` maintenance database, and refuse a third,
arbitrary name so the model cannot aim an EXPLAIN at an unrelated co-tenant database on the
same instance. The pin is fail-soft (a record with no registered database_name keeps the
prior behaviour). Every DB connection is also tagged with the caller's identity
(`application_name`) and each accepted/refused EXPLAIN is written to the governance store.

Hermetic: strands + boto3 stubbed, tools.aws.pg_client faked, so no DB or AWS access.

Run:  python3 backend/tests/test_query_plan_target_pin.py   (or via pytest)
"""
import os
import pathlib
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REPO = pathlib.Path(__file__).resolve().parents[2]

# Stub strands (identity @tool) and boto3 (unused by these paths).
_strands = types.ModuleType("strands")
_strands.tool = lambda f: f
sys.modules.setdefault("strands", _strands)
sys.modules.setdefault("boto3", types.ModuleType("boto3"))

# Fake pg_client: capture what reaches EXPLAIN, and make the record's database_name tunable.
_STATE = {"queries": [], "record_db": ""}
_fake_pg = types.ModuleType("tools.aws.pg_client")


def _fake_execute_query(secret_arn, query, params=None, database=None):
    _STATE["queries"].append((query, database))
    return [{"ok": 1}]


_fake_pg.execute_query = _fake_execute_query
_fake_pg.test_connection = lambda *a, **k: {"ok": True}
_fake_pg.lookup_record_database_name = lambda secret_arn: _STATE["record_db"]
# The shared non-confirming refusal db_tools now imports for the mismatch branch. Keep it
# verbatim with pg_client.SECRET_NOT_IN_SPACE_MESSAGE so the assertion below is meaningful.
SECRET_NOT_IN_SPACE_MESSAGE = (
    "This database's credentials are not available in the current space. If "
    "access was configured recently, re-select the database; otherwise it may "
    "not be registered in this space."
)
_fake_pg.SECRET_NOT_IN_SPACE_MESSAGE = SECRET_NOT_IN_SPACE_MESSAGE
sys.modules["tools.aws.pg_client"] = _fake_pg

from tools import db_tools as dbt  # noqa: E402

# Capture audit calls instead of touching the governance store.
_AUDITS = []
dbt._audit_query_plan = lambda q, d, allowed, reason="": _AUDITS.append(
    {"database": d, "allowed": allowed, "reason": reason})


def _reset(record_db=""):
    _STATE["queries"].clear()
    _STATE["record_db"] = record_db
    _AUDITS.clear()


def _read(rel):
    return (REPO / rel).read_text(encoding="utf-8")


# ── the pin (behavioural) ────────────────────────────────────────────────────

def test_refuses_a_database_other_than_the_records_or_postgres():
    _reset(record_db="orders")
    res = dbt.get_query_plan("arn:secret", "SELECT 1", database="hr_payroll")
    # Non-confirming refusal: the shared message, and it must NOT echo the registered or
    # requested database name (that would confirm the co-tenant DB's existence/name).
    assert res.get("error") == SECRET_NOT_IN_SPACE_MESSAGE
    assert "orders" not in res["error"] and "hr_payroll" not in res["error"]
    assert _STATE["queries"] == [], "a mismatched target must never reach EXPLAIN"
    assert _AUDITS and _AUDITS[-1]["allowed"] is False
    assert _AUDITS[-1]["reason"] == "database_mismatch"


def test_allows_the_records_own_database():
    _reset(record_db="orders")
    res = dbt.get_query_plan("arn:secret", "SELECT 1", database="orders")
    assert "error" not in res
    assert _STATE["queries"], "the record's own database must reach EXPLAIN"
    assert _AUDITS[0]["allowed"] is True


def test_allows_the_postgres_maintenance_database():
    _reset(record_db="orders")
    res = dbt.get_query_plan("arn:secret", "SELECT 1", database="postgres")
    assert "error" not in res
    assert _STATE["queries"], "'postgres' (catalog reads) must stay allowed"


def test_pin_is_case_insensitive():
    _reset(record_db="Orders")
    res = dbt.get_query_plan("arn:secret", "SELECT 1", database="orders")
    assert "error" not in res, "the record match must not be defeated by case"


def test_fail_soft_when_record_has_no_database_name():
    # An unknown/blank record database_name keeps the prior behaviour (no block).
    _reset(record_db="")
    res = dbt.get_query_plan("arn:secret", "SELECT 1", database="anything")
    assert "error" not in res
    assert _STATE["queries"], "with no registered database_name the pin must not block"


def test_unsafe_sql_is_still_refused_and_audited():
    _reset(record_db="orders")
    res = dbt.get_query_plan("arn:secret", "SELECT 1; DROP TABLE t", database="orders")
    assert "error" in res
    assert _STATE["queries"] == []
    assert _AUDITS and _AUDITS[-1]["allowed"] is False and _AUDITS[-1]["reason"] == "unsafe_sql"


# ── connection identity + resolver + audit (source) ──────────────────────────

def test_connection_is_tagged_with_caller_identity():
    src = _read("backend/tools/aws/pg_client.py")
    assert "application_name=_application_name()" in src
    assert "def _application_name()" in src
    assert "CURRENT_USER_ID" in src and "CURRENT_SPACE_ID" in src
    assert "def lookup_record_database_name(" in src


def test_query_plan_audits_through_the_governance_store():
    src = _read("backend/tools/db_tools.py")
    assert "def _audit_query_plan(" in src
    assert "governance_registry.audit(" in src
    assert '"query_plan_run"' in src and '"query_plan_refused"' in src


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", fn.__name__, "-", e or "assertion failed")
    print("\n%d passed, %d failed" % (len(fns) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
