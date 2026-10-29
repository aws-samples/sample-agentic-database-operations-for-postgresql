# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The single source of the transaction-ID (XID) wraparound denominator.

Every surface that reports "how close is this database to a transaction-ID wraparound"
must divide ``age(datfrozenxid)`` by the SAME number, or the dashboard, the agent's
answer and the HTML report disagree about the one check whose whole job is that
question. PostgreSQL wraps at 2^31-1 transactions, so that is the denominator, and the
Batch findings engine recovers the raw age from the stored percentage with the same
constant.

This module is staged into the Batch image and the Tier 1 collector zip alongside the
other curated ``tools`` files (deployment/build-images.sh, backend/Makefile's
build-batch target, and backend/scripts/build-tier1.sh); keep those in step if it moves.
The matching literal in batch/postgres_health_insights.sh (a shell report that cannot
import Python) is pinned to this value by tests/test_xid_denominator.py.
"""

# PostgreSQL's transaction-ID space is 2^31; wraparound danger is measured against the
# maximum age before the counter wraps, 2^31 - 1.
XID_WRAPAROUND_MAX = 2_147_483_647


def xid_age_pct(age) -> float:
    """``age(datfrozenxid)`` as a percentage of the wraparound maximum, rounded to 2 dp.
    Returns None when ``age`` is not a number."""
    try:
        return round(100.0 * float(age) / XID_WRAPAROUND_MAX, 2)
    except (TypeError, ValueError):
        return None
