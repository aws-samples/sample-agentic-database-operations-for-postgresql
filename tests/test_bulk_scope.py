#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Selecting N databases puts all N in the chat scope (review #10).

The operator Dashboard's "💬 Add to Scope" button acted on a multi-row selection by
calling `onSelectDatabase(db)` once per row. `onSelectDatabase` (App.jsx
`chatAboutDatabase`) does `setSelectedDatabases([db])` — a REPLACE — so each call threw
away the previous one and only the last selected database survived. The next chat turn
was then scoped to one database instead of all N.

Fix: `handleBulkScope` hands the whole selection over in a single call, and
`chatAboutDatabase` accepts either one database or an array and puts them all in scope.

Source assertions on the shipped JSX plus a reduction simulating the scope that results
from a multi-row selection.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DASH = ROOT / "frontend/src/components/Dashboard.jsx"
APP = ROOT / "frontend/src/App.jsx"

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


dash = DASH.read_text(encoding="utf-8")
app = APP.read_text(encoding="utf-8")

bulk = dash.split("const handleBulkScope", 1)[1].split("setSelectedRows(new Set())", 1)[0] \
       + "setSelectedRows(new Set())"
chat = app.split("const chatAboutDatabase", 1)[1].split("setChatCollapsed(false)", 1)[0]

print("══ Add to Scope hands the whole selection over at once ══")
check("toAdd.forEach(db => onSelectDatabase" not in bulk,
      "handleBulkScope no longer calls onSelectDatabase once per row (the replace-each bug)")
check(re.search(r"onSelectDatabase\(\s*toAdd\s*\)", bulk) is not None,
      "handleBulkScope passes the full selection array in a single onSelectDatabase call")
check("configured.filter(i => selectedRows.has(i.instance_id))" in bulk,
      "the selection is still every configured, checked row")

print("")
print("══ chatAboutDatabase accepts one database or many ══")
check("Array.isArray(db)" in chat and "setSelectedDatabases(Array.isArray(db) ? db : [db])" in chat,
      "chatAboutDatabase sets the scope to the array as-is, or wraps a single database")

print("")
print("══ simulate: three rows selected -> three in scope ══")
# Mirror the shipped logic: toAdd = configured filtered by the selected ids, then
# chatAboutDatabase(toAdd) sets the scope to exactly that array.
configured = [{"instance_id": x} for x in ("a", "b", "c", "d")]
selected = {"a", "b", "c"}
to_add = [i for i in configured if i["instance_id"] in selected]


def chat_about(db):
    return db if isinstance(db, list) else [db]


scope = chat_about(to_add)
check([i["instance_id"] for i in scope] == ["a", "b", "c"],
      "all three selected databases are in scope, not just the last one")
# And a single-row "Chat" still scopes to exactly that one.
single = chat_about(configured[3])
check([i["instance_id"] for i in single] == ["d"],
      "a single-database Chat still scopes to that one database")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
