# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""get_unused_indexes never surfaces a primary-key or UNIQUE index (review #6).

Dropping a PK or UNIQUE-backing index removes a data-integrity guarantee or breaks replica
identity, so such indexes must NEVER appear as cleanup candidates. The old tool only
filtered names ending in `_pkey`; it never consulted pg_index. The fix joins pg_index and
excludes indisprimary / indisunique and any index backing a constraint (pg_constraint).

Two halves:
  * the shipped SQL carries the exclusions (source assertion);
  * the tool run through a fake execute_query that honours exactly those exclusions over a
    fake catalog (a PK index, a UNIQUE index, a UNIQUE-constraint-backing index, and one
    genuinely unused btree) returns ONLY the genuinely unused one.

Hermetic: strands + boto3 + pg_client are stubbed; no DB, no AWS.

Run:  python3 backend/tests/test_unused_index_safety.py
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub strands so the @tool decorator and boto3 import at module load do nothing.
_strands = types.ModuleType("strands")
_strands.tool = lambda f=None, **k: (f if callable(f) else (lambda g: g))
sys.modules.setdefault("strands", _strands)
sys.modules.setdefault("boto3", types.ModuleType("boto3"))

_fake_pg = types.ModuleType("tools.aws.pg_client")


# A fake catalog of indexes with zero scans. Flags mirror the SQL predicate: an index is a
# candidate only when it is NOT primary, NOT unique, and NOT backing a constraint.
FAKE_CATALOG = [
    {"index_name": "orders_pkey",       "indisprimary": True,  "indisunique": True,  "constraint": True},
    {"index_name": "orders_email_key",  "indisprimary": False, "indisunique": True,  "constraint": True},
    {"index_name": "orders_sku_uniq",   "indisprimary": False, "indisunique": True,  "constraint": False},
    {"index_name": "orders_notes_idx",  "indisprimary": False, "indisunique": False, "constraint": False},
]


def _fake_execute_query(secret_arn, query, params=None, database=None):
    _LAST["query"] = query
    # Apply exactly the exclusions the shipped SQL expresses.
    out = []
    for ix in FAKE_CATALOG:
        if ix["indisprimary"] or ix["indisunique"] or ix["constraint"]:
            continue
        out.append({"schemaname": "public", "table_name": "orders",
                    "index_name": ix["index_name"], "index_size": "8 kB",
                    "index_size_bytes": 8192, "idx_scan": 0, "idx_tup_read": 0,
                    "recommendation": "LOW PRIORITY - Small unused index"})
    return out


_LAST = {"query": ""}
_fake_pg.execute_query = _fake_execute_query
_fake_pg.test_connection = lambda *a, **k: {"ok": True}
_fake_pg.lookup_record_database_name = lambda *a, **k: ""
_fake_pg.SECRET_NOT_IN_SPACE_MESSAGE = "not available in the current space"
sys.modules["tools.aws.pg_client"] = _fake_pg

import tools.db_tools as dbt  # noqa: E402

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


# ── the shipped SQL excludes PK / UNIQUE / constraint-backed indexes ──
import inspect  # noqa: E402
src = inspect.getsource(dbt.get_unused_indexes.__wrapped__) if hasattr(dbt.get_unused_indexes, "__wrapped__") \
    else inspect.getsource(dbt.get_unused_indexes)
# The @tool stub returns the function unchanged, so getsource gives the real body.
print("══ the shipped SQL joins pg_index and excludes PK/UNIQUE/constraint ══")
check("pg_index" in src, "the query joins pg_index")
check("NOT ix.indisprimary" in src, "primary-key indexes are excluded")
check("NOT ix.indisunique" in src, "unique indexes are excluded")
check("pg_constraint" in src and "conindid" in src, "constraint-backing indexes are excluded")
check("NOT LIKE '%%_pkey'" not in src, "the old name-only _pkey filter is gone")

# ── the tool returns only the genuinely unused index ──
print("")
print("══ only the genuinely unused index is a candidate ══")
res = dbt.get_unused_indexes("db-1")
names = [r["index_name"] for r in res["unused_indexes"]]
check(names == ["orders_notes_idx"], f"only the plain unused btree is a candidate (got {names})")
check("orders_pkey" not in names, "the primary-key index is never a candidate")
check("orders_email_key" not in names, "the UNIQUE-constraint index is never a candidate")
check("orders_sku_uniq" not in names, "the standalone UNIQUE index is never a candidate")

# ── the specialist prompt does not describe these as droppable ──
print("")
print("══ the specialist prompt keeps the exclusion claim honest ══")
AGENT = (__import__("pathlib").Path(__file__).resolve().parents[1]
         / "sub_agents/postgres_agent.py").read_text(encoding="utf-8")
check("EXCLUDE primary-key / unique indexes from drop candidates" in AGENT,
      "the prompt states the index tools exclude PK/UNIQUE from drop candidates")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
