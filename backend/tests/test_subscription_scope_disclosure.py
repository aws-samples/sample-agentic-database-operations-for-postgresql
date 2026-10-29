# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""get_logical_replication discloses an unreadable pg_subscription.

pg_subscription is superuser-only, so the read-only monitoring role is routinely denied.
The tool used to swallow the denial and set subscriptions=[] with a soft note that
"publications and slots above still work" -- indistinguishable from "there are no
subscriptions, all clear", which would hide a disabled subscription nobody could see. The
tool now discloses the gap: subscriptions_scope=unreadable and subscriptions_status=
INCONCLUSIVE, with a note that says subscription health is UNKNOWN. When the read succeeds,
scope=complete and a disabled subscription is a WARNING.

Hermetic: strands/boto3 stubbed, pg_client faked and routed by query.

Run:  python3 backend/tests/test_subscription_scope_disclosure.py   (or via pytest)
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_strands = types.ModuleType("strands")
_strands.tool = lambda f: f
sys.modules.setdefault("strands", _strands)
sys.modules.setdefault("boto3", types.ModuleType("boto3"))

# Routed fake: pg_subscription either raises (denied) or returns canned rows; every other
# query in get_logical_replication returns empty so only the subscription path is exercised.
_SUB = {"mode": "deny", "rows": []}


def _routed(secret_arn, query, params=None, database=None):
    if "pg_subscription" in query.lower():
        if _SUB["mode"] == "deny":
            raise RuntimeError("permission denied for table pg_subscription")
        return [dict(r) for r in _SUB["rows"]]
    return []


_fake_pg = types.ModuleType("tools.aws.pg_client")
_fake_pg.execute_query = _routed
_fake_pg.test_connection = lambda *a, **k: {"ok": True}
_fake_pg.lookup_record_database_name = lambda *a, **k: ""
_fake_pg.SECRET_NOT_IN_SPACE_MESSAGE = "not available in the current space"
sys.modules["tools.aws.pg_client"] = _fake_pg

from tools import db_tools as dbt  # noqa: E402


def test_denial_is_disclosed_not_swallowed():
    _SUB["mode"] = "deny"
    out = dbt.get_logical_replication("db-1")
    assert out["subscriptions"] == []
    assert out["subscriptions_scope"] == "unreadable"
    assert out["subscriptions_status"] == "INCONCLUSIVE"
    note = out["subscriptions_note"].upper()
    assert "UNKNOWN" in note, "the note must say subscription health is unknown"
    assert "SUPERUSER" in note, "and name the privilege that would settle it"


def test_denial_does_not_read_as_all_clear():
    # The whole point: an unreadable result must not look like "no subscriptions".
    _SUB["mode"] = "deny"
    out = dbt.get_logical_replication("db-1")
    assert out.get("subscriptions_scope") != "complete"
    assert "status" not in [k for k in out if k == "subscriptions" and out[k]]  # no healthy rows


def test_readable_subscriptions_report_scope_complete():
    _SUB["mode"] = "ok"
    _SUB["rows"] = [{"subname": "sub_a", "subenabled": True},
                    {"subname": "sub_b", "subenabled": False}]
    out = dbt.get_logical_replication("db-1")
    assert out["subscriptions_scope"] == "complete"
    assert "subscriptions_status" not in out, "a complete read is not INCONCLUSIVE"
    by = {r["subname"]: r for r in out["subscriptions"]}
    assert by["sub_a"]["status"] == "OK"
    assert by["sub_b"]["status"] == "WARNING", "a disabled subscription is a WARNING"


def test_empty_but_readable_is_complete_not_inconclusive():
    _SUB["mode"] = "ok"
    _SUB["rows"] = []
    out = dbt.get_logical_replication("db-1")
    assert out["subscriptions"] == [] and out["subscriptions_scope"] == "complete"
    assert "subscriptions_status" not in out, "genuinely no subscriptions is a clear result"


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"  FAIL {name}: {e!r}")
                failed += 1
    print(f"pass={passed} fail={failed}")
    sys.exit(1 if failed else 0)
