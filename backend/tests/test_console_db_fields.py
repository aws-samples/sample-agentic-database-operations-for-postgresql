# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Every database field the admin console reads is one the manager projection returns.

A space admin's GET /api/databases is ``project_db_for_manager``, an allowlist: a field
it does not return renders as an empty cell, not an error, so a missing field looks like
missing data about the database. ``admin-console/src/components/dbRecordFields.js``
declares what the console reads off a record (and which fields only a /candidates row
carries). This test:

* collects every ``db.<field>`` and ``db.tags?.<key>`` the console reads in
  SpaceDetail.jsx (where records from /api/databases are rendered) and fails when one is
  not declared;
* projects a fully populated record and fails when a declared record field or tag is not
  in the projection.

Hermetic: reads source text and runs the real projection.

Run:  python3 backend/tests/test_console_db_fields.py
"""
import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.db_projection import project_db_for_manager  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
COMPONENTS = ROOT / "admin-console/src/components"
passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def js_list(src, name):
    m = re.search(rf"export const {name} = \[(.*?)\]", src, re.S)
    assert m, name
    return re.findall(r"'([a-z0-9_]+)'", m.group(1))


DECL = (COMPONENTS / "dbRecordFields.js").read_text(encoding="utf-8")
RECORD, TAGS, CANDIDATE = (js_list(DECL, n) for n in ("DB_RECORD_FIELDS", "DB_RECORD_TAGS", "CANDIDATE_ONLY_FIELDS"))
check(RECORD and TAGS and CANDIDATE, f"the declared lists ({len(RECORD)} record, {len(TAGS)} tags, "
                                     f"{len(CANDIDATE)} candidate-only)")

print("== every field the console reads is declared ==")
JSX = (COMPONENTS / "SpaceDetail.jsx").read_text(encoding="utf-8")
read = set(re.findall(r"\bdb\??\.([a-z_][a-z0-9_]*)", JSX))
tags_read = set(re.findall(r"\bdb\.tags\??\.([a-z_][a-z0-9_]*)", JSX)) | \
    set(re.findall(r"\bd\.tags\??\.([a-z_][a-z0-9_]*)", JSX))
check(len(read) >= 20, f"collected the fields SpaceDetail reads ({len(read)})")
undeclared = sorted(read - set(RECORD) - set(CANDIDATE))
check(not undeclared, f"each is a declared record field or a candidate-only field (undeclared: {undeclared})")
check(not sorted(tags_read - set(TAGS)), f"each tag read is declared ({sorted(tags_read - set(TAGS))})")
check(not (set(RECORD) & set(CANDIDATE)), "no field is both")

print("== the manager projection returns every declared record field ==")
full = {f: f"value-{f}" for f in RECORD}
full.update({"member_space_ids": {"space-a", "space-b"}, "reach_space_id": "space-a", "secret_arn": "arn:secret",
             "cluster_member_roles": [{"instance_id": "m1", "is_writer": True}], "config_log": ["x"],
             "route_repair_needed": {"member": "m1.example", "address": "10.0.1.9"},
             "tags": {t: f"tag-{t}" for t in TAGS}})
proj = project_db_for_manager(full, {"space-a"})
missing = sorted(f for f in RECORD if f not in proj)
check(not missing, f"every declared field is returned (missing: {missing})")
missing_tags = sorted(t for t in TAGS if t not in (proj.get("tags") or {}))
check(not missing_tags, f"every declared tag is returned (missing: {missing_tags})")
check(proj["member_space_ids"] == ["space-a"] and proj["member_count"] == 2,
      "membership stays narrowed to the caller's spaces")

print("== the guard catches a field the projection drops ==")
check("route_repair_needed" in RECORD and "route_repair_needed" in proj,
      "route_repair_needed (the console's Route repair row) is projected")
dropped = project_db_for_manager({**full, "a_new_field": 1}, {"space-a"})
check("a_new_field" not in dropped, "a field not on the allowlist is not returned (so it must be declared and added)")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
