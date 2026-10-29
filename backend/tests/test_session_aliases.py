# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Database aliases are stable for a whole chat session (#24).

Aliases (db-1, db-2, ...) were assigned per request, so when the selection changed
between turns db-2 could name a different database than it did two turns earlier — and
the model sees earlier turns' scope blocks. The alias -> database mapping is now
persistent per session: an alias is assigned the first time a database appears and never
reassigned or reused. A database that leaves the scope keeps its alias, which then
resolves to "not in the current scope".

Pins the pure assignment (surfaces.scope.stable_aliases / chat_scope_block_stable) and
the resolver's out-of-scope refusal (tools.aws.db_aliases). Hermetic, no AWS.

Run:  python3 backend/tests/test_session_aliases.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# db_aliases imports model_resolver lazily; both are pure here.
import model_resolver as mr  # noqa: E402
from surfaces import scope  # noqa: E402
from tools.aws import db_aliases  # noqa: E402

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def db(iid, name):
    return {"instance_id": iid, "display_name": name, "engine": "aurora-postgresql",
            "endpoint": f"{name}.example", "database_name": "postgres"}


ORDERS = db("id-orders", "orders")
BILLING = db("id-billing", "billing")
AUDIT = db("id-audit", "audit")

print("the first turn assigns aliases in a deterministic order")
named1, map1 = scope.stable_aliases([ORDERS, BILLING], {})
check(map1 == {"db-1": "id-billing", "db-2": "id-orders"}, f"billing=db-1, orders=db-2 by name order ({map1})")

print("a later turn with a DIFFERENT selection keeps each database's alias")
# Turn 2 selects only orders plus a new database (audit). orders must stay db-2.
named2, map2 = scope.stable_aliases([AUDIT, ORDERS], map1)
check(map2["db-2"] == "id-orders", "orders is still db-2")
check(map2["db-1"] == "id-billing", "billing keeps db-1 even though it is not selected this turn")
check(map2["db-3"] == "id-audit", "the new database audit takes the next free alias db-3")
in_scope2 = {a: r["instance_id"] for a, r in named2}
check(in_scope2 == {"db-2": "id-orders", "db-3": "id-audit"}, f"only this turn's selection is in scope ({in_scope2})")
check([a for a, _ in named2] == ["db-2", "db-3"], "the block lists the in-scope databases in alias order")

print("an alias is never reused: a removed database does not free its number")
# Turn 3 selects a brand-new database; it must be db-4, not db-1 (billing's retired slot).
NEW = db("id-new", "zeta")
_, map3 = scope.stable_aliases([NEW], map2)
check(map3["db-4"] == "id-new" and map3["db-1"] == "id-billing", "a new database is db-4, db-1 stays billing")

print("the resolver: an in-scope alias resolves; a removed one is refused clearly")
mr.CURRENT_USER_ID.set("op-1")
mr.CURRENT_DB_ALIASES.set(in_scope2)          # this turn: orders (db-2), audit (db-3)
mr.CURRENT_SESSION_ALIASES.set(map2)          # the whole session: db-1..db-3
try:
    check(db_aliases.resolve_handle("db-2") == "id-orders", "db-2 resolves to orders (in scope)")
    # billing (db-1) left the scope this turn: a distinct 'not in the current scope' refusal.
    try:
        db_aliases.resolve_handle("db-1")
        check(False, "db-1 (out of scope) must be refused")
    except PermissionError as e:
        check("not in the current scope" in str(e), f"db-1 is refused as out-of-scope ({e})")
    # an alias never assigned in this session: the generic refusal.
    try:
        db_aliases.resolve_handle("db-9")
        check(False, "db-9 (never assigned) must be refused")
    except PermissionError as e:
        check(str(e) == db_aliases.REFUSAL, "an unknown alias gets the generic refusal")
finally:
    mr.CURRENT_USER_ID.set("")
    mr.CURRENT_DB_ALIASES.set(None)
    mr.CURRENT_SESSION_ALIASES.set(None)

print("the stable block carries no UUID and names the stable aliases")
block, in_scope, full = scope.chat_scope_block_stable([AUDIT, ORDERS], map1, selected=True)
check("db-2: orders" in block and "db-3: audit" in block, "the block uses the stable aliases")
check("id-orders" not in block and "id-billing" not in block, "no registry UUID appears in the block")

print("scoped_ids reflects the turn's selection, so a tool can honor it over the whole space")
check(db_aliases.scoped_ids() is None, "no request context -> None (list the whole space, unchanged)")
mr.CURRENT_USER_ID.set("op-1")
mr.CURRENT_DB_ALIASES.set({"db-2": "id-orders", "db-3": "id-audit"})
try:
    check(sorted(db_aliases.scoped_ids()) == ["id-audit", "id-orders"],
          "with a 2-of-N selection, scoped_ids is exactly those two registry ids")
finally:
    mr.CURRENT_USER_ID.set("")
    mr.CURRENT_DB_ALIASES.set(None)
check(db_aliases.scoped_ids() is None, "cleared context -> None again")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
