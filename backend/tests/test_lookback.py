# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for per-check investigation-window resolution (checks.registry).

The check registry's free-text ``investigation_lookback`` is
normalized into a structured default lookback (hours), with sentinels for
point-in-time / cumulative windows that carry NO fixed clock span (narrated as
such, never as an N-hour window).

Pure logic (registry YAML + stdlib) — no AWS/app deps.

Run:  python3 backend/tests/test_lookback.py   (or via pytest)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from checks.registry import resolve_lookback_hours, check_lookback_hours  # noqa: E402


# ── resolve_lookback_hours: N-day windows → N*24 hours ──

def test_thirty_days_resolves_to_720h():
    assert resolve_lookback_hours("30 days (Period=5min) for MaximumUsedTransactionIDs trend") == 720
    assert resolve_lookback_hours("30 days (Period=5min)") == 720
    assert resolve_lookback_hours("30d") == 720  # shorthand


def test_ninety_days_resolves_to_2160h():
    assert resolve_lookback_hours("90 days (CloudTrail deployment/failover events)") == 2160
    assert resolve_lookback_hours("90 days (migration/deployment events)") == 2160


def test_three_days_resolves_to_72h():
    assert resolve_lookback_hours("3 days (write-throughput / load correlation)") == 72
    assert resolve_lookback_hours("3 days (read-IO context)") == 72
    assert resolve_lookback_hours("3d") == 72


def test_seven_days_resolves_to_168h():
    assert resolve_lookback_hours("7 days (replication events)") == 168


# ── sentinels: no fixed clock window → None ──

def test_point_in_time_is_none():
    assert resolve_lookback_hours("point-in-time (catalog state)") is None
    assert resolve_lookback_hours("point-in-time (live)") is None
    assert resolve_lookback_hours("point-in-time (cumulative pg_stat_statements)") is None


def test_stats_reset_is_none():
    assert resolve_lookback_hours("stats_reset age (cumulative)") is None


def test_cumulative_sentinel_beats_incidental_day_hint():
    # "3 days for write-IO context" is a secondary hint; the check's lookback is
    # cumulative (stats_reset) → NO fixed window.
    assert resolve_lookback_hours("stats_reset age (usage is cumulative); 3 days for write-IO context") is None


def test_growth_trend_is_none():
    assert resolve_lookback_hours("growth trend") is None


def test_on_demand_is_none():
    assert resolve_lookback_hours("on demand") is None


# ── empty / unknown → small default ──

def test_empty_and_unknown_return_default():
    assert resolve_lookback_hours("") == 2
    assert resolve_lookback_hours(None) == 2
    assert resolve_lookback_hours("   ") == 2
    assert resolve_lookback_hours("no numbers here") == 2


# ── check_lookback_hours: convenience over the live registry ──

def test_check_lookback_hours_reads_registry():
    # Derived from registry.yaml investigation_lookback values.
    assert check_lookback_hours("xid-wraparound-database") == 720   # "30 days ..."
    assert check_lookback_hours("invalid-indexes") == 2160          # "90 days ..."
    assert check_lookback_hours("lock-contention") is None          # "point-in-time (live)"
    # A check with NO investigation_lookback field falls back to the default.
    assert check_lookback_hours("vacuum-and-bloat") == 2
    # An unknown slug is empty → default.
    assert check_lookback_hours("no-such-check") == 2


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", fn.__name__, "-", e or "assertion failed")
    print("\n%d passed, %d failed" % (len(fns) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
