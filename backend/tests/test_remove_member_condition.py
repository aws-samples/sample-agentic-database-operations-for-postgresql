# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""``remove_member(keep_one=True)`` is a CONDITIONAL delete, decided by DynamoDB.

Whether a departing space is the database's last member must not be decided from a
membership set the handler read a moment earlier: two spaces leaving a two-member
database at once would both see two members, both untag, and leave a record with no
member and a live network path. With ``keep_one`` the DELETE carries a condition on the
set's size and contents that DynamoDB evaluates at write time; when it fails the client
raises ``LastMemberError`` and the caller tears the database down instead.

Hermetic: a fake table stands in for DynamoDB and enforces the condition the way the
service would. Run:  python3 backend/tests/test_remove_member_condition.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from botocore.exceptions import ClientError  # noqa: E402

from tools.aws.dynamodb_client import DbInstanceRegistry, LastMemberError  # noqa: E402


class FakeTable:
    """Honours `size(member_space_ids) > :one AND contains(member_space_ids, :sid)`."""

    def __init__(self, rows):
        self.rows = {r["instance_id"]: dict(r) for r in rows}
        self.calls = []

    def update_item(self, **kw):
        self.calls.append(kw)
        row = self.rows[kw["Key"]["instance_id"]]
        members = set(row.get("member_space_ids") or [])
        if "ConditionExpression" in kw:
            vals = kw["ExpressionAttributeValues"]
            assert kw["ConditionExpression"] == "size(member_space_ids) > :one AND contains(member_space_ids, :sid)"
            if not (len(members) > vals[":one"] and vals[":sid"] in members):
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException",
                                             "Message": "The conditional request failed"}}, "UpdateItem")
        if kw["UpdateExpression"].startswith("DELETE member_space_ids"):
            members -= kw["ExpressionAttributeValues"][":s"]
            row["member_space_ids"] = members
        elif kw["UpdateExpression"].startswith("SET tags"):
            row["tags"] = kw["ExpressionAttributeValues"][":t"]

    def get_item(self, Key):
        return {"Item": self.rows.get(Key["instance_id"])}


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


print("== two concurrent removals from a two-member database ==")
reg = registry([{"instance_id": "db", "member_space_ids": {"a", "b"}}])
reg.remove_member("db", "a", keep_one=True)
check(reg._table.rows["db"]["member_space_ids"] == {"b"}, "the first removal passes")
try:
    reg.remove_member("db", "b", keep_one=True)
    check(False, "the second raises LastMemberError")
except LastMemberError as e:
    check(e.instance_id == "db" and e.space_id == "b", "the second raises LastMemberError naming the pair")
check(reg._table.rows["db"]["member_space_ids"] == {"b"}, "and the set is NOT emptied")

print("== a space that is not a member cannot be 'removed' into an empty set either ==")
reg = registry([{"instance_id": "db", "member_space_ids": {"a", "b"}}])
try:
    reg.remove_member("db", "zz", keep_one=True)
    check(False, "a non-member raises")
except LastMemberError:
    check(True, "a non-member raises")

print("== the condition is on the write, not on a value read earlier ==")
reg = registry([{"instance_id": "db", "member_space_ids": {"a", "b"}}])
reg.remove_member("db", "a", keep_one=True)
kw = reg._table.calls[-1]
check("ConditionExpression" in kw and ":one" in kw["ExpressionAttributeValues"], "the DELETE carries the condition")

print("== without keep_one the set may legitimately empty (discovery housekeeping) ==")
reg = registry([{"instance_id": "db", "member_space_ids": {"a"}}])
reg.remove_member("db", "a")
check(reg._table.rows["db"]["member_space_ids"] == set(), "unconditional removal empties the set")
check("ConditionExpression" not in reg._table.calls[0], "and carries no condition")

print("== other DynamoDB errors are not swallowed as LastMemberError ==")


class Boom(FakeTable):
    def update_item(self, **kw):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}}, "UpdateItem")


reg = DbInstanceRegistry()
reg._table = Boom([{"instance_id": "db", "member_space_ids": {"a", "b"}}])
try:
    reg.remove_member("db", "a", keep_one=True)
    check(False, "a throttle propagates as ClientError")
except ClientError:
    check(True, "a throttle propagates as ClientError")
except LastMemberError:
    check(False, "a throttle propagates as ClientError, not LastMemberError")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
