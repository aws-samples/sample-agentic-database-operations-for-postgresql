# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""When a teardown claim (``status=deleting``) is stalled. A leaf module (no AWS
imports), shared by the database and space registries, the API and the topology view."""

from datetime import datetime, timedelta, timezone
from typing import Optional


# A teardown claim (``status=deleting``) records when it was taken in
# ``teardown_claimed_at``, and the running teardown refreshes that time as a heartbeat
# (at least every TEARDOWN_HEARTBEAT_S, from its start and from every Lattice wait). A
# claim not refreshed for TEARDOWN_STALE_S belongs to a process that stopped (a deploy,
# a task replacement, a crash): a later delete takes it over and runs the teardown again,
# which is safe because every step treats NotFound as done. A live teardown beats at
# least once per wait poll (5 s) and per database; the cutoff, three whole Lattice wait
# budgets (3 x 300 s), leaves a wide margin over any stretch between beats.
TEARDOWN_HEARTBEAT_S = 60
TEARDOWN_STALE_S = 900


def teardown_claim_cutoff(now: Optional[datetime] = None) -> datetime:
    """The time before which a ``deleting`` claim is stale."""
    return (now or datetime.now(timezone.utc)) - timedelta(seconds=TEARDOWN_STALE_S)


def teardown_stalled(record: Optional[dict], now: Optional[datetime] = None) -> bool:
    """Whether a database or space record is ``deleting`` under a stale claim: its
    teardown stopped with the process that ran it, and deleting it again (Retry) takes the
    claim over. A ``deleting`` record without ``teardown_claimed_at`` has no heartbeat and
    is stalled. A claim time that cannot be read is treated as live."""
    if (record or {}).get("status") != "deleting":
        return False
    stamp = record.get("teardown_claimed_at")
    if not stamp:
        return True
    try:
        claimed = datetime.fromisoformat(str(stamp))
    except ValueError:
        return False
    if claimed.tzinfo is None:
        claimed = claimed.replace(tzinfo=timezone.utc)
    return claimed < teardown_claim_cutoff(now)


# DynamoDB's side of teardown_stalled, for the conditional claim writes: not deleting,
# or deleting under a claim older than :cutoff (ISO strings of one format sort by time),
# or deleting with no claim time at all.
TEARDOWN_CLAIMABLE = "(#st <> :d OR #tc < :cutoff OR attribute_not_exists(#tc))"
