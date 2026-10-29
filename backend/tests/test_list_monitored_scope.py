# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tool-layer multi-tenant ownership tests for list_monitored_databases.

Two properties of the list_monitored_databases agent tool:
  1. secret_arn is STRIPPED from every returned record, so the model never receives a
     credential pointer for any tenant, and
  2. results are SPACE-SCOPED to the caller's space when it is known (published on
     model_resolver.CURRENT_SPACE_ID by /invocations); when UNKNOWN it FAILS CLOSED and
     returns nothing — never a cross-tenant dump.

Hermetic: strands + boto3 stubbed, tools.aws.dynamodb_client faked — no AWS access.

Run:  python3 backend/tests/test_list_monitored_scope.py   (or via pytest)
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_strands = types.ModuleType("strands")
_strands.tool = lambda f: f
sys.modules.setdefault("strands", _strands)
sys.modules.setdefault("boto3", types.ModuleType("boto3"))

# db_tools also imports tools.aws.pg_client at module load — fake it.
_fake_pg = types.ModuleType("tools.aws.pg_client")
_fake_pg.execute_query = lambda *a, **k: []
_fake_pg.test_connection = lambda *a, **k: {"ok": True}
_fake_pg.lookup_record_database_name = lambda *a, **k: ""  # imported by db_tools
_fake_pg.SECRET_NOT_IN_SPACE_MESSAGE = "not available in the current space"  # imported by db_tools
sys.modules["tools.aws.pg_client"] = _fake_pg

SPACE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SPACE_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

_INSTANCES = [
    {"instance_id": "i-a1", "status": "configured", "secret_arn": "arn:aws:secretsmanager:us-east-1:1:secret:a1",
     "endpoint": "a1.rds.amazonaws.com", "member_space_ids": {SPACE_A}},
    {"instance_id": "i-a2", "status": "monitored", "secret_arn": "arn:aws:secretsmanager:us-east-1:1:secret:a2",
     "member_space_ids": {SPACE_A}},
    {"instance_id": "i-b1", "status": "configured", "secret_arn": "arn:aws:secretsmanager:us-east-1:2:secret:b1",
     "member_space_ids": {SPACE_B}},
    {"instance_id": "i-disc", "status": "discovered", "secret_arn": "arn:aws:secretsmanager:us-east-1:1:secret:d",
     "candidate_space_ids": {SPACE_A}},
]

_fake_ddb = types.ModuleType("tools.aws.dynamodb_client")
_fake_ddb.db_registry = types.SimpleNamespace(list_instances=lambda *a, **k: list(_INSTANCES))
sys.modules["tools.aws.dynamodb_client"] = _fake_ddb

from tools import db_tools as dbt  # noqa: E402
import model_resolver as mr  # noqa: E402


def _ids(result):
    return sorted(d["instance_id"] for d in result["databases"])


def _no_secret_arns(result):
    return all("secret_arn" not in d for d in result["databases"])


def test_unscoped_fails_closed_returns_nothing():
    # An unscoped context (no resolved space, e.g. an /invocations
    # call that omitted space_id) must NOT enumerate any tenant's databases. The tool
    # returns an empty list — not every monitored row.
    mr.CURRENT_SPACE_ID.set("")
    res = dbt.list_monitored_databases()
    assert _ids(res) == [], "unscoped must return no databases (fail-closed)"
    assert res["count"] == 0
    assert _no_secret_arns(res)


def test_scoped_secret_arn_stripped_and_discovered_excluded():
    # secret_arn stripped from every record + 'discovered' excluded, within a space.
    mr.CURRENT_SPACE_ID.set(SPACE_A)
    try:
        res = dbt.list_monitored_databases()
        assert _ids(res) == ["i-a1", "i-a2"]  # i-disc (discovered) excluded
        assert _no_secret_arns(res), "secret_arn must be stripped from every record"
        assert any(d.get("endpoint") == "a1.rds.amazonaws.com" for d in res["databases"])
    finally:
        mr.CURRENT_SPACE_ID.set("")


def test_scoped_to_space_when_known():
    mr.CURRENT_SPACE_ID.set(SPACE_A)
    try:
        res = dbt.list_monitored_databases()
        assert _ids(res) == ["i-a1", "i-a2"]  # space B excluded
        assert _no_secret_arns(res)
        assert res["count"] == 2
    finally:
        mr.CURRENT_SPACE_ID.set("")


def test_scope_instances_helper_fails_closed_when_unscoped():
    # The shared _scope_instances_to_space helper (used by
    # get_report_analysis + get_historical_metrics) filters to the resolved space and
    # returns nothing when unscoped — so a forged/absent space can't reach another tenant.
    rows = [
        {"instance_id": "i-a1", "member_space_ids": {SPACE_A}},
        {"instance_id": "i-b1", "member_space_ids": {SPACE_B}},
    ]
    mr.CURRENT_SPACE_ID.set(SPACE_A)
    try:
        assert [r["instance_id"] for r in dbt._scope_instances_to_space(rows)] == ["i-a1"]
    finally:
        mr.CURRENT_SPACE_ID.set("")
    assert dbt._scope_instances_to_space(rows) == []  # unscoped → fail closed


def test_scoped_space_b_isolated_from_space_a():
    mr.CURRENT_SPACE_ID.set(SPACE_B)
    try:
        res = dbt.list_monitored_databases()
        assert _ids(res) == ["i-b1"]
        assert _no_secret_arns(res)
    finally:
        mr.CURRENT_SPACE_ID.set("")


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
