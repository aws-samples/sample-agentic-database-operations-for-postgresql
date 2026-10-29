# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A ``deleting`` claim cannot strand a database or a space.

A delete claims the record ``status=deleting`` with a conditional write and runs the
teardown in a background task. Background tasks do not survive the process: a deploy,
a task replacement or a crash during a teardown used to leave the claim in place for
good, so nothing could delete, dismiss, onboard or retry the record. Now the claim
records ``teardown_claimed_at``, the running teardown refreshes it (a heartbeat, from its
start and from every Lattice wait), and a claim with no heartbeat for TEARDOWN_STALE_S
can be taken over by the next delete (the Retry). A live claim still refuses a second
delete.

Pinned here: ``teardown_stalled``; the condition DynamoDB evaluates in
``DbInstanceRegistry.claim_teardown`` and ``SpacesRegistry.claim_teardown`` (a fake table
enforces it the way the service would); the heartbeat writes; and ``_wait_until`` calling
the heartbeat on every poll. Hermetic, no AWS.

Run:  python3 backend/tests/test_teardown_claim.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from botocore.exceptions import ClientError  # noqa: E402
from tools.aws.teardown_claim import (  # noqa: E402
    TEARDOWN_CLAIMABLE, TEARDOWN_STALE_S, teardown_claim_cutoff, teardown_stalled)
from tools.aws.dynamodb_client import DbInstanceRegistry  # noqa: E402
from tools.aws.spaces_client import SpacesRegistry  # noqa: E402
from tools.aws import network_client  # noqa: E402

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def ago(s):
    return (datetime.now(timezone.utc) - timedelta(seconds=s)).isoformat()


STALE = 2 * TEARDOWN_STALE_S


class FakeTable:
    """Evaluates the two condition shapes the registries write, as DynamoDB would."""

    def __init__(self, key, rows):
        self.key = key
        self.rows = {r[key]: dict(r) for r in rows}
        self.calls = []

    def _fail(self):
        raise ClientError({"Error": {"Code": "ConditionalCheckFailedException",
                                     "Message": "The conditional request failed"}}, "UpdateItem")

    def update_item(self, **kw):
        self.calls.append(kw)
        row = self.rows.get(kw["Key"][self.key])
        names, vals = kw["ExpressionAttributeNames"], kw["ExpressionAttributeValues"]
        cond = kw["ConditionExpression"]
        if cond == f"attribute_exists({self.key}) AND " + TEARDOWN_CLAIMABLE:
            assert names == {"#st": "status", "#tc": "teardown_claimed_at", "#ua": "updated_at"}
            if row is None:
                self._fail()
            tc = row.get("teardown_claimed_at")
            ok = row.get("status") != vals[":d"] or tc is None or tc < vals[":cutoff"]
            if not ok:
                self._fail()
        elif cond == "#st = :d":
            if row is None or row.get("status") != vals[":d"]:
                self._fail()
        else:
            raise AssertionError(f"unexpected condition {cond!r}")
        for part in kw["UpdateExpression"].removeprefix("SET ").split(", "):
            n, v = part.split(" = ")
            row[names.get(n, n)] = vals[v]


def db_reg(rows):
    reg = DbInstanceRegistry()
    reg._table = FakeTable("instance_id", rows)
    return reg


def space_reg(rows):
    reg = SpacesRegistry()
    reg._table = FakeTable("space_id", rows)
    return reg


print("== teardown_stalled ==")
check(not teardown_stalled({"status": "deleting", "teardown_claimed_at": ago(30)}), "a fresh claim is live")
check(teardown_stalled({"status": "deleting", "teardown_claimed_at": ago(STALE)}), "an old claim is stalled")
check(teardown_stalled({"status": "deleting"}), "a deleting record with no claim time has no heartbeat: stalled")
check(teardown_stalled({"status": "deleting", "updated_at": ago(5)}), "updated_at is not a heartbeat")
check(not teardown_stalled({"status": "delete_failed", "teardown_claimed_at": ago(STALE)}),
      "only a deleting record can be stalled")
check(not teardown_stalled({"status": "deleting", "teardown_claimed_at": "?"}),
      "a claim time that cannot be read is treated as live")
check(not teardown_stalled(None), "no record, nothing stalled")
check(teardown_claim_cutoff(datetime(2030, 1, 1, tzinfo=timezone.utc))
      == datetime(2030, 1, 1, tzinfo=timezone.utc) - timedelta(seconds=TEARDOWN_STALE_S), "the cutoff")
check(TEARDOWN_STALE_S >= 3 * network_client.DELETE_WAIT_BUDGET_S,
      "the cutoff is longer than any Lattice wait (they beat on every poll anyway)")

for label, make, key in (("database", db_reg, "instance_id"), ("space", space_reg, "space_id")):
    print(f"== {label}: claim_teardown ==")
    reg = make([{key: "a", "status": "configured", "updated_at": ago(5)},
                {key: "b", "status": "deleting", "teardown_claimed_at": ago(30), "updated_at": ago(30)},
                {key: "c", "status": "deleting", "teardown_claimed_at": ago(STALE), "updated_at": ago(STALE)},
                {key: "d", "status": "deleting", "updated_at": ago(5)},
                {key: "e", "status": "delete_failed", "updated_at": ago(5)}])
    rows = reg._table.rows
    check(reg.claim_teardown("a") and rows["a"]["status"] == "deleting" and rows["a"]["teardown_claimed_at"],
          "a record that is not deleting is claimed, with the claim time")
    check(reg.claim_teardown("a") is False, "a second claim (double click) is refused")
    check(reg.claim_teardown("b") is False, "a live claim is refused")
    before = rows["c"]["teardown_claimed_at"]
    check(reg.claim_teardown("c") and rows["c"]["teardown_claimed_at"] > before,
          "a stalled claim is taken over and re-stamped")
    check(reg.claim_teardown("c") is False, "and is live again for the next request")
    check(reg.claim_teardown("d"), "a deleting record with no claim time is taken over (updated_at ignored)")
    check(reg.claim_teardown("e"), "delete_failed is claimable (Retry)")
    check(reg.claim_teardown("missing") is False and "missing" not in rows, "a missing record is not created")
    cutoff = reg._table.calls[0]["ExpressionAttributeValues"][":cutoff"]
    check(abs((datetime.fromisoformat(cutoff) - teardown_claim_cutoff()).total_seconds()) < 5,
          "the condition's :cutoff is now minus TEARDOWN_STALE_S")

    print(f"== {label}: touch_teardown (heartbeat) ==")
    rows["b"]["teardown_claimed_at"] = ago(600)
    reg.touch_teardown("b")
    check(not teardown_stalled(rows["b"]) and rows["b"]["teardown_claimed_at"] > ago(5), "refreshes a deleting claim")
    reg.touch_teardown("e")
    check(rows["e"]["status"] == "deleting", "(e was claimed above)")
    rows["e"]["status"] = "configured"
    reg.touch_teardown("e")
    reg.touch_teardown("missing")
    check(rows["e"]["status"] == "configured" and "missing" not in rows,
          "a no-op on a record that moved on or is gone")

print("== the Lattice waits beat ==")
beats = []
polls = iter([(False, "", "deleting")] * 3 + [(True, "", "gone")])
orig_sleep = network_client.time.sleep
network_client.time.sleep = lambda s: None
try:
    with network_client.wait_heartbeat(lambda: beats.append(1)):
        ok, last, err = network_client._wait_until(lambda: next(polls), 300, 5)
    check(ok and last == "gone" and len(beats) == 3, "every poll that waits calls the heartbeat")
    beats.clear()
    polls = iter([(False, "", "x"), (True, "", "y")])
    network_client._wait_until(lambda: next(polls), 300, 5)
    check(not beats, "outside the block no heartbeat is called")

    def boom():
        raise RuntimeError("ddb down")
    polls = iter([(False, "", "x"), (True, "", "y")])
    with network_client.wait_heartbeat(boom):
        ok, *_ = network_client._wait_until(lambda: next(polls), 300, 5)
    check(ok, "a failing heartbeat does not stop the wait")
finally:
    network_client.time.sleep = orig_sleep

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
