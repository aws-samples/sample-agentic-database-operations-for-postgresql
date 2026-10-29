# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""``update_instance`` writes what it is given, or refuses; it never drops a field.

A write that is silently ignored looks like success to the caller, to the admin console
and to the audit trail while the record is unchanged. ``update_instance`` used to filter
its input against an allowlist and discard the rest without a word, which is how a
setting the console accepted, validated and "saved" never reached the table. The
allowlist stays (per-space sets and maps have their own atomic operations), but a field
off the list is now an error.

Behavioural, through the real client with a fake table: the write is observed as the
UpdateExpression the table receives, not inferred from the source. A second check scans
every literal field name ``app.py`` passes to ``update_instance`` and asserts each is on
the list, so the callers and the list cannot drift again.

Run:  python3 backend/tests/test_update_instance_fields.py
"""
import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.aws.dynamodb_client import DbInstanceRegistry, UPDATABLE_FIELDS  # noqa: E402

APP_SRC = (pathlib.Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")


class FakeTable:
    """Applies SET expressions to an in-memory row, the way DynamoDB would."""

    def __init__(self, rows):
        self.rows = {r["instance_id"]: dict(r) for r in rows}
        self.calls = []

    def get_item(self, Key):
        return {"Item": dict(self.rows[Key["instance_id"]])} if Key["instance_id"] in self.rows else {}

    def update_item(self, **kw):
        self.calls.append(kw)
        row = self.rows[kw["Key"]["instance_id"]]
        expr = kw["UpdateExpression"]
        assert expr.startswith("SET "), expr
        names, values = kw["ExpressionAttributeNames"], kw["ExpressionAttributeValues"]
        for part in expr[4:].split(", "):
            lhs, rhs = [x.strip() for x in part.split("=")]
            row[names[lhs]] = values[rhs]


passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def registry(rows):
    reg = DbInstanceRegistry()
    reg._table = FakeTable(rows)
    return reg


print("== a field on the list is written and read back ==")
reg = registry([{"instance_id": "db", "display_name": "orders", "tier1_enabled": False}])
out = reg.update_instance("db", {"retention_days": 45, "tier1_enabled": True})
check(reg._table.rows["db"]["retention_days"] == 45, "retention_days lands on the row")
check(reg._table.rows["db"]["tier1_enabled"] is True, "a second field in the same call lands too")
check(out["retention_days"] == 45, "the returned record reflects the write")
check("updated_at" in reg._table.rows["db"], "updated_at is stamped")

print("== a field off the list is an error, and nothing is written ==")
reg = registry([{"instance_id": "db", "display_name": "orders"}])
try:
    reg.update_instance("db", {"display_name": "x", "not_a_field": 1})
    check(False, "an unknown field raises ValueError")
except ValueError as e:
    check("not_a_field" in str(e), "an unknown field raises ValueError naming it")
check(reg._table.calls == [] and reg._table.rows["db"]["display_name"] == "orders",
      "the whole call is refused: the valid field in the same dict is not written either")

print("== per-space sets and maps are not writable through update_instance ==")
for f in ("member_space_ids", "candidate_space_ids", "space_secrets", "granted_by"):
    check(f not in UPDATABLE_FIELDS, f"{f} has its own atomic operation and is not on the list")
    try:
        registry([{"instance_id": "db"}]).update_instance("db", {f: set()})
        check(False, f"writing {f} here raises")
    except ValueError:
        check(True, f"writing {f} here raises")

print("== the cluster-member and host-route fields are updatable ==")
for f in ("writer_instance_id", "members_changed_at", "network_route_mode", "host_routes", "db_vpc_cidr",
          "reader_endpoint", "cluster_member_roles"):
    check(f in UPDATABLE_FIELDS, f"{f} is on the list")
print("== every literal field app.py passes to update_instance is on the list ==")
literal_keys = set()
for m in re.finditer(r"update_instance\(\s*[^,]+,\s*\{(.*?)\}\s*\)", APP_SRC, re.S):
    literal_keys |= set(re.findall(r'"(\w+)":', m.group(1)))
# Variable dicts built up before the call (`db_registry.update_instance(instance_id, X)`):
# collect X's keys from the same top-level function body, so a same-named dict that goes
# to another registry (spaces) is not mistaken for one of ours.
functions = re.split(r"(?m)^(?=(?:@|def |async def ))", APP_SRC)
for body in functions:
    for var in set(re.findall(r"db_registry\.update_instance\(\s*[^,]+,\s*([A-Za-z_]\w*)\s*\)", body)):
        for m in re.finditer(r'\b' + re.escape(var) + r'\["(\w+)"\]\s*=', body):
            literal_keys.add(m.group(1))
        for m in re.finditer(r'\b' + re.escape(var) + r'\s*=\s*\{(.*?)\}', body, re.S):
            literal_keys |= set(re.findall(r'"(\w+)":', m.group(1)))
check(len(literal_keys) >= 15, f"{len(literal_keys)} distinct field names found at the call sites")
off_list = sorted(literal_keys - set(UPDATABLE_FIELDS))
check(not off_list, f"every one of them is updatable (off the list: {off_list or 'none'})")
check("tls_mode" not in literal_keys and "tls_mode" not in UPDATABLE_FIELDS,
      "there is no stored TLS mode: every connection verifies, nothing on the record can weaken it")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
