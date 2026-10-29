# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""get_query_plan: non-confirming database-mismatch refusal + durable audit trail (MF-39).

get_query_plan is the one tool that accepts BOTH model-authored SQL and a model-chosen
database. Two invariants are pinned here:

  1. When the credential's registered database is known and the model aims the EXPLAIN at a
     third, arbitrary name, the tool refuses with the SHARED non-confirming message
     (pg_client.SECRET_NOT_IN_SPACE_MESSAGE) and does NOT echo the registered or requested
     database name — otherwise the refusal would confirm a co-tenant database's existence.
     execute_query is never reached.

  2. Every attempt — allowed and refused — writes exactly one durable audit event
     (query_plan_run / query_plan_refused) through the SAME governance-store sink the rest
     of the app uses, carrying the actor, the database, and the truncated SQL. So the real
     target is recorded server-side even though the client message is non-confirming.

Hermetic: strands + boto3 stubbed, tools.aws.pg_client and tools.aws.governance_store
faked. No DB, no AWS.

Run:  python3 backend/tests/test_query_plan_audit.py   (or via pytest)
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_strands = types.ModuleType("strands")
_strands.tool = lambda f: f
sys.modules.setdefault("strands", _strands)
sys.modules.setdefault("boto3", types.ModuleType("boto3"))

REGISTERED_DB = "orders_prod"      # the credential's real database — must never be echoed
REFUSAL = ("This database's credentials are not available in the current space. If access "
           "was configured recently, re-select the database; otherwise it may not be "
           "registered in this space.")

_EXECUTED = {"queries": []}
_fake_pg = types.ModuleType("tools.aws.pg_client")


def _fake_execute_query(secret_arn, query, params=None, database=None):
    _EXECUTED["queries"].append((query, database))
    return [{"QUERY PLAN": "Seq Scan"}]


_fake_pg.execute_query = _fake_execute_query
_fake_pg.test_connection = lambda *a, **k: {"ok": True}
_fake_pg.lookup_record_database_name = lambda *a, **k: REGISTERED_DB
_fake_pg.SECRET_NOT_IN_SPACE_MESSAGE = REFUSAL
sys.modules["tools.aws.pg_client"] = _fake_pg

# Capture every governance-store audit write.
_AUDITS = []
_fake_gov = types.ModuleType("tools.aws.governance_store")


def _capture_audit(actor, action, target, scope, before=None, after=None, *, space_id=None, **kw):
    _AUDITS.append({"actor": actor, "action": action, "target": target, "scope": scope,
                    "after": after, "space_id": space_id})


_fake_gov.governance_registry = types.SimpleNamespace(audit=_capture_audit)
sys.modules["tools.aws.governance_store"] = _fake_gov

from tools import db_tools as dbt  # noqa: E402

passed = failed = 0


def check(cond, label):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}")


# ── 1. database mismatch: non-confirming refusal, no execution ──
_EXECUTED["queries"].clear()
_AUDITS.clear()
res = dbt.get_query_plan("arn:secret", "SELECT 1", database="some_other_db")
print("== database mismatch is refused without confirming the co-tenant DB ==")
check(res.get("error") == REFUSAL, f"refusal is the shared non-confirming message ({res.get('error')})")
check(REGISTERED_DB not in res.get("error", ""), "the registered database name is NOT echoed")
check("some_other_db" not in res.get("error", ""), "the requested database name is NOT echoed")
check(_EXECUTED["queries"] == [], "execute_query is never called on a mismatch")
check(len(_AUDITS) == 1 and _AUDITS[0]["action"] == "query_plan_refused",
      f"exactly one query_plan_refused audit event ({[a['action'] for a in _AUDITS]})")
ev = _AUDITS[0]
check(ev["after"].get("database") == "some_other_db" and ev["after"].get("reason") == "database_mismatch",
      "the refused audit records the real requested database + reason server-side")
check("query" in ev["after"] and ev["after"]["query"].startswith("SELECT 1"),
      "the refused audit records the (truncated) SQL")

# ── 2. allowed plan: one audit event, carries actor/database/SQL ──
_EXECUTED["queries"].clear()
_AUDITS.clear()
res = dbt.get_query_plan("arn:secret", "SELECT 42", database=REGISTERED_DB)
print("")
print("== an allowed plan runs and is audited exactly once ==")
check("error" not in res, f"the registered database is allowed ({res.get('error', 'ok')})")
check(len(_EXECUTED["queries"]) >= 1 and _EXECUTED["queries"][0][0].startswith("EXPLAIN"),
      "the EXPLAIN reaches execute_query")
check(len(_AUDITS) == 1 and _AUDITS[0]["action"] == "query_plan_run",
      f"exactly one query_plan_run audit event ({[a['action'] for a in _AUDITS]})")
ev = _AUDITS[0]
check(ev["after"].get("database") == REGISTERED_DB, "the allowed audit records the database")
check(ev["after"].get("query", "").startswith("SELECT 42"), "the allowed audit records the (truncated) SQL")
check(isinstance(ev["actor"], str) and ev["actor"], "the allowed audit records a (non-empty) actor")

# ── 3. 'postgres' maintenance DB is always allowed (fail-soft safety net) ──
_EXECUTED["queries"].clear()
_AUDITS.clear()
res = dbt.get_query_plan("arn:secret", "SELECT 1", database="postgres")
print("")
print("== the maintenance database 'postgres' is allowed ==")
check("error" not in res, "'postgres' is accepted alongside the registered database")
check(len(_AUDITS) == 1 and _AUDITS[0]["action"] == "query_plan_run", "allowed audit written for postgres")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
