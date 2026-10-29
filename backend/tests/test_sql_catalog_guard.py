# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Sensitive-catalog blocklist tests for tools.db_tools.

get_query_plan denies
queries that reference credential/file/TLS catalogs (pg_shadow, pg_authid,
pg_read_file, pg_read_binary_file, pg_read_server_files, pg_stat_ssl) even though
they are read-only. Read-only SELECT enforcement is preserved for everything else.

Hermetic: strands + boto3 are stubbed and tools.aws.pg_client is faked, so no DB or
AWS access happens (blocked queries never reach execute_query; the allow-path uses a
fake execute_query).

Run:  python3 backend/tests/test_sql_catalog_guard.py   (or via pytest)
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub strands (identity @tool) and boto3 (unused by these code paths).
_strands = types.ModuleType("strands")
_strands.tool = lambda f: f
sys.modules.setdefault("strands", _strands)
sys.modules.setdefault("boto3", types.ModuleType("boto3"))

# Fake the PostgreSQL client so an allowed query has a deterministic result and a
# blocked query would be *visibly* wrong if it ever reached the DB.
_EXECUTED = {"queries": []}
_fake_pg = types.ModuleType("tools.aws.pg_client")


def _fake_execute_query(secret_arn, query, params=None, database=None):
    _EXECUTED["queries"].append(query)
    return [{"ok": 1}]


_fake_pg.execute_query = _fake_execute_query
_fake_pg.test_connection = lambda *a, **k: {"ok": True}
# db_tools now imports this to pin get_query_plan's target database. Return '' here
# so the pin is fail-soft (skipped) and these catalog-guard tests keep exercising the SQL gate.
_fake_pg.lookup_record_database_name = lambda *a, **k: ""
# db_tools imports this shared non-confirming refusal for the target-database pin.
_fake_pg.SECRET_NOT_IN_SPACE_MESSAGE = "credentials are not available in the current space"
sys.modules["tools.aws.pg_client"] = _fake_pg

from tools import db_tools as dbt  # noqa: E402


# ── Unit: _blocked_sensitive_catalog ──

def test_blocklist_flags_sensitive_catalogs():
    assert dbt._blocked_sensitive_catalog("SELECT * FROM pg_shadow") == "pg_shadow"
    assert dbt._blocked_sensitive_catalog("select usename from pg_authid") == "pg_authid"
    assert dbt._blocked_sensitive_catalog("SELECT pg_read_file('/etc/passwd')") == "pg_read_file"
    assert dbt._blocked_sensitive_catalog("SELECT pg_read_binary_file('x')") == "pg_read_binary_file"
    assert dbt._blocked_sensitive_catalog("SELECT * FROM pg_stat_ssl") == "pg_stat_ssl"


def test_blocklist_flags_dynamic_sql_and_file_functions():
    # file/large-object I/O, outbound dblink, and
    # dynamic-SQL exec functions that would otherwise smuggle a blocked read past the
    # literal name check (query_to_xml runs its string argument server-side).
    assert dbt._blocked_sensitive_catalog("SELECT lo_import('/etc/passwd')") == "lo_import"
    assert dbt._blocked_sensitive_catalog("SELECT pg_ls_dir('/')") == "pg_ls_dir"
    assert dbt._blocked_sensitive_catalog(
        "SELECT * FROM dblink('host=x', 'SELECT 1') AS t(v text)") == "dblink"
    # the chr()-obfuscated pg_shadow read still trips on the query_to_xml name itself
    assert dbt._blocked_sensitive_catalog(
        "SELECT query_to_xml(chr(83)||chr(69), true, false, '')") == "query_to_xml"
    # the rest of the *_to_xml dump family + every pg_ls_* lister
    assert dbt._blocked_sensitive_catalog("SELECT database_to_xml(true, true, '')") == "database_to_xml"
    assert dbt._blocked_sensitive_catalog("SELECT table_to_xml('t', true, false, '')") == "table_to_xml"
    assert dbt._blocked_sensitive_catalog("SELECT schema_to_xml('s', true, false, '')") == "schema_to_xml"
    assert dbt._blocked_sensitive_catalog("SELECT pg_ls_logdir()") == "pg_ls_logdir"
    assert dbt._blocked_sensitive_catalog("SELECT pg_ls_waldir()") == "pg_ls_waldir"
    # auth/config file views (safe to block — not used by diagnostics)
    assert dbt._blocked_sensitive_catalog("SELECT * FROM pg_hba_file_rules") == "pg_hba_file_rules"
    assert dbt._blocked_sensitive_catalog("SELECT * FROM pg_file_settings") == "pg_file_settings"


def test_blocklist_allows_diagnostic_catalogs():
    # These are used by legitimate diagnostic tools and MUST stay allowed:
    # blocking them would break get_current_activity / get_db_overview / test_connection.
    assert dbt._blocked_sensitive_catalog("SELECT * FROM pg_stat_activity") == ""
    assert dbt._blocked_sensitive_catalog("SELECT current_setting('server_version')") == ""
    assert dbt._blocked_sensitive_catalog("SELECT inet_server_addr()") == ""


def test_blocklist_allows_normal_tables():
    assert dbt._blocked_sensitive_catalog("SELECT * FROM pg_stat_activity") == ""
    assert dbt._blocked_sensitive_catalog("SELECT count(*) FROM my_orders") == ""
    # substring in a normal identifier must not false-positive (word boundary)
    assert dbt._blocked_sensitive_catalog("SELECT * FROM my_pg_authid_backup_note") == ""


# ── _reject_unsafe_read_sql: the shared guard (run_custom_query was REMOVED) ──
# run_custom_query no longer exists — it was a dead @tool (registered on no agent) that
# exposed arbitrary SQL; it was deleted. get_query_plan is the only LIVE SQL sink and shares
# this guard.

def test_reject_unsafe_read_sql_helper():
    assert dbt._reject_unsafe_read_sql("SELECT 1") is None                          # allowed
    assert dbt._reject_unsafe_read_sql("SELECT 1; SET ROLE admin")                  # multi-statement
    assert dbt._reject_unsafe_read_sql('SELECT * FROM U&"pg_\\0061uthid"')          # unicode escape
    assert dbt._reject_unsafe_read_sql("SELECT * FROM pg_authid")                   # sensitive catalog
    assert dbt._reject_unsafe_read_sql("SELECT set_config('x','y',true)") is None   # set_config is fine


def test_get_query_plan_blocks_multi_statement():
    _EXECUTED["queries"].clear()
    res = dbt.get_query_plan("arn:secret", "SELECT 1; SET ROLE admin")
    assert "error" in res
    assert _EXECUTED["queries"] == []


def test_get_query_plan_blocks_unicode_escape():
    _EXECUTED["queries"].clear()
    res = dbt.get_query_plan("arn:secret", 'SELECT * FROM U&"pg_\\0061uthid"')
    assert "error" in res
    assert _EXECUTED["queries"] == []


def test_get_query_plan_blocks_dml():
    res = dbt.get_query_plan("arn:secret", "DELETE FROM users")
    assert "error" in res


def test_get_query_plan_allows_normal_select():
    _EXECUTED["queries"].clear()
    res = dbt.get_query_plan("arn:secret", "SELECT 1")
    assert "error" not in res
    assert _EXECUTED["queries"], "an allowed SELECT should reach EXPLAIN via execute_query"


# ── get_query_plan: deny sensitive catalogs under EXPLAIN ──

def test_get_query_plan_blocks_sensitive_catalog():
    _EXECUTED["queries"].clear()
    res = dbt.get_query_plan("arn:secret", "SELECT * FROM pg_shadow")
    assert "error" in res and "restricted" in res["error"].lower()
    assert _EXECUTED["queries"] == []


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
