#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Server-built [DATABASE SCOPE] blocks (surfaces/scope.py).

The agents learn which databases a turn is about, and which credential pointer their SQL
tools may use, from this block. It used to be assembled in the browser from
GET /api/databases — which stopped returning ``secret_arn`` to plain operators, so their
turns carried ``secret_arn=undefined`` and every SQL-backed analysis was refused. The
server now builds it from registry records after the membership checks. These tests pin
the block's content and wording (the agents' prompts quote the field names), the Aurora
role-focus paragraphs, and the guard that keeps an older client's hand-built block from
being doubled.

Pure functions; no AWS, no imports of the app.

Run:  python3 backend/tests/test_surface_scope.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from surfaces.scope import (  # noqa: E402
    alias_map, assign_aliases, chat_scope_block, compose_chat_prompt, has_scope_block,
    investigation_scope_block, role_focus_block, scope_entry,
)

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


ORDERS = {
    "instance_id": "uuid-orders", "display_name": "orders-prod", "engine": "aurora-postgresql",
    "secret_arn": "arn:aws:secretsmanager:eu-west-1:111111111111:secret:dbops/orders-AbCdEf",
    "database_name": "orders", "endpoint": "orders.cluster-x.eu-west-1.rds.amazonaws.com",
    "reader_endpoint": "orders.cluster-ro-x.eu-west-1.rds.amazonaws.com",
    "account_id": "111111111111", "region": "eu-west-1",
}
BILLING = {
    "instance_id": "uuid-billing", "display_name": "billing", "engine": "postgres",
    "secret_arn": "arn:aws:secretsmanager:eu-west-1:111111111111:secret:dbops/billing-XyZ",
}

print("── one database, one line ──")
line = scope_entry(ORDERS, "db-1")
check(line.startswith("- db-1: orders-prod ("), "line opens with the alias and the display name")
for field in ("engine=aurora-postgresql", "region=eu-west-1", "secret_arn=db-1", "database=orders",
              f"endpoint={ORDERS['endpoint']}", f"reader_endpoint={ORDERS['reader_endpoint']}",
              "db_instance_id=db-1"):
    check(field in line, f"carries {field.split('=')[0]}")
check(ORDERS["instance_id"] not in line, "the registry id is not in the line")
line2 = scope_entry(BILLING, "db-2")
check("reader_endpoint" not in line2, "no reader_endpoint field when the record has none")
check("database=postgres" in line2, "database defaults to postgres")
check("endpoint=" in line2, "endpoint field present even when empty")
print("")
print("── aliases: deterministic, one per database ──")
named = assign_aliases([ORDERS, BILLING])
check([(a, r["display_name"]) for a, r in named] == [("db-1", "billing"), ("db-2", "orders-prod")],
      "ordered by display name: billing is db-1, orders-prod db-2")
check(alias_map([BILLING, ORDERS]) == alias_map([ORDERS, BILLING]) == {"db-1": "uuid-billing", "db-2": "uuid-orders"},
      "the same records give the same mapping in any order")
check(alias_map([ORDERS, ORDERS, None]) == {"db-1": "uuid-orders"}, "a repeated record gets one alias; None is skipped")
twin_a = {"instance_id": "b-id", "display_name": "Same"}
twin_b = {"instance_id": "a-id", "display_name": "same"}
check(alias_map([twin_a, twin_b]) == {"db-1": "a-id", "db-2": "b-id"}, "equal names fall back to the registry id")
print("")
print("── chat block ──")
block = chat_scope_block([ORDERS, BILLING], selected=True)
check(block.startswith("[DATABASE SCOPE] The user has selected database(s). Use this context instead of calling list_monitored_databases. Each database is named by its alias"),
      "selected wording matches the operator app's, then explains the aliases")
check(block.rstrip().endswith("[END DATABASE SCOPE]"), "block is closed")
check(block.count("\n- ") == 2, "one line per database")
check("\n- db-1: billing (" in block and "\n- db-2: orders-prod (" in block, "each line is named by its alias")
block_all = chat_scope_block([ORDERS], selected=False)
check("No specific selection — using all database(s)" in block_all, "'all databases' wording")
check(chat_scope_block([], "reader") == "", "no records → no block (even with a focus)")
check(chat_scope_block([None, ORDERS]).count("\n- ") == 1, "None records are skipped")
print("")
print("── role focus ──")
check(role_focus_block("") == "" and role_focus_block(None) == "", "no focus → no paragraph")
w = role_focus_block("writer")
check("[ROLE FOCUS]" in w and "WRITER instance" in w and w.endswith("[END ROLE FOCUS]"), "writer paragraph")
r = role_focus_block("Reader")
check("READER instances" in r and "reader endpoint" in r, "reader paragraph (case-insensitive)")
i = role_focus_block("instance:orders-instance-2")
check('"orders-instance-2"' in i and "ONE instance of the database cluster" in i,
      "instance paragraph names the member (without assuming it is a reader)")
focused = chat_scope_block([ORDERS], "reader")
check("[ROLE FOCUS]" in focused and focused.index("[ROLE FOCUS]") < focused.index("[END DATABASE SCOPE]"),
      "focus paragraph sits inside the scope block, before its end marker")

print("")
print("── investigation block ──")
inv = investigation_scope_block([ORDERS, BILLING])
check(inv.startswith("[DATABASE SCOPE: Investigate ONLY these 2 databases. Each database is named by its alias"),
      "header carries the count and explains the aliases")
check("- db-2: orders-prod, db_identifier=db-2, secret_arn=db-2" in inv, "per-database line shape: the handle is the alias")
check("arn:aws:secretsmanager" not in inv and "arn:aws:secretsmanager" not in chat_scope_block([ORDERS, BILLING]), "no secret ARN ever enters a prompt")
check("account_id=111111111111, region=eu-west-1" in inv, "placement carried")
check("space_id=" not in inv, "the space id (a UUID) is no longer written into the block")
check(investigation_scope_block([{"display_name": "x"}]).count("secret_arn=db-1") == 1,
      "a record without a secret still gets an alias")
check(inv.rstrip().endswith("Do NOT investigate any other databases.]"), "closing instruction")
check(investigation_scope_block([]) == "", "no records → no block")
check("reach the writer" not in inv, "instances carry no member note")
inv_cluster = investigation_scope_block([dict(ORDERS, cluster_member_roles=[{"instance_id": "orders-1", "is_writer": True}])])
check("reach the writer" in inv_cluster and "Endpoint menu" in inv_cluster,
      "a cluster says SQL reaches the writer and where a reader is checked")
check(inv_cluster.rstrip().endswith("Do NOT investigate any other databases.]"), "the note sits inside the block")
print("")
print("── golden: no UUID-shaped string in any scope block ──")
import re as _re_uuid
UUID_SHAPE = _re_uuid.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
REAL = [dict(ORDERS, instance_id="9e46bfaa-19d8-4fd5-9909-909918dc8ea8",
             cluster_member_roles=[{"instance_id": "orders-1", "is_writer": True}]),
        dict(BILLING, instance_id="0b1c2d3e-4f50-4a6b-8c7d-8e9fa0b1c2d3")]
for label, text in (("chat", chat_scope_block(REAL, selected=True)),
                    ("chat, all", chat_scope_block(REAL, selected=False)),
                    ("chat, focus", chat_scope_block(REAL[:1], "instance:orders-1")),
                    ("investigation", investigation_scope_block(REAL)),
                    ("composed", compose_chat_prompt("why slow?", REAL, "writer"))):
    check(not UUID_SHAPE.search(text), f"{label}: no UUID-shaped string ({UUID_SHAPE.findall(text)})")
check(alias_map(REAL) == {"db-1": REAL[1]["instance_id"], "db-2": REAL[0]["instance_id"]},
      "and the mapping kept server-side holds the real ids")
print("")
print("── composing and double-block guard ──")
check(has_scope_block("[DATABASE SCOPE] x") and has_scope_block("prefix\n[DATABASE SCOPE: Investigate"),
      "detects both block styles")
check(not has_scope_block("plain question") and not has_scope_block(""), "plain text has no block")
p = compose_chat_prompt("why slow?", [ORDERS], "writer", selected=True)
check(p.endswith("\n\nwhy slow?") and p.startswith("[DATABASE SCOPE]"), "message follows the block after a blank line")
check(compose_chat_prompt("why slow?", []) == "why slow?", "no records → message verbatim")

print("")

# ── the record's trigger is authoritative when the wire carries no symptom ──────
# Extracted from app.py by source (importing the app pulls in AWS clients).
import pathlib as _pl, re as _re
_src = (_pl.Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
_m = _re.search(r"^_NULLISH_SYMPTOMS.*?(?=^def _scope_records)", _src, _re.S | _re.M)
_ns = {}
exec(_m.group(0), _ns)
_restore = _ns["_restore_investigation_trigger"]
_rec = {"trigger": "Storage growth on db-1"}
_hdr = "[TIME RANGE: Last 2 hours. Check metrics and events for this period.]"
check(_restore(f"{_hdr}\nundefined", _rec) == f"{_hdr}\nStorage growth on db-1",
      "a literal 'undefined' symptom is replaced by the record's trigger")
check(_restore(f"{_hdr}\n", _rec) == f"{_hdr}\nStorage growth on db-1",
      "an empty symptom is replaced by the record's trigger")
check(_restore(f"{_hdr}\nsome issue with db", _rec) == f"{_hdr}\nsome issue with db",
      "a real symptom is left exactly as sent")
check(_restore(f"{_hdr}\nnull", {}) == f"{_hdr}\nnull",
      "no record trigger means nothing is changed")
check("_restore_investigation_trigger(actual_prompt, inv_record)" in _src,
      "the investigation path applies it before building the scope block")

print(f"passed={passed} failed={failed}")
sys.exit(1 if failed else 0)
