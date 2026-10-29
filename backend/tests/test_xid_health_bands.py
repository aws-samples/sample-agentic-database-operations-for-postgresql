# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""XID-wraparound severity tests for tools.db_tools.get_health_summary.

get_health_summary classifies transaction-ID age with the shared reference bands
(references.yaml#xid-wraparound-database) via _severity_from_xid_bands — the same
policy get_maintenance_status and get_transaction_stats apply — not its own
percentage-of-autovacuum_freeze_max_age cutoffs. So a database in routine
autovacuum territory is no longer reported as an XID emergency, while a database
genuinely near the wraparound budget still is.

Hermetic: strands + boto3 are stubbed and tools.aws.pg_client is faked, so no DB
or AWS access happens; get_health_summary reads a single fake metrics row.

Run:  python3 backend/tests/test_xid_health_bands.py   (or via pytest)
"""
import os
import pathlib
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_strands = types.ModuleType("strands")
_strands.tool = lambda f: f
sys.modules.setdefault("strands", _strands)
sys.modules.setdefault("boto3", types.ModuleType("boto3"))

# Fake the PostgreSQL client: get_health_summary runs one query; we hand it a single
# metrics row we control, so the only thing under test is how it classifies XID age.
_ROW = {"row": [{}]}
_fake_pg = types.ModuleType("tools.aws.pg_client")
_fake_pg.execute_query = lambda secret_arn, query, params=None, database=None: _ROW["row"]
_fake_pg.test_connection = lambda *a, **k: {"ok": True}
_fake_pg.lookup_record_database_name = lambda *a, **k: ""  # imported by db_tools
_fake_pg.SECRET_NOT_IN_SPACE_MESSAGE = "not available in the current space"  # imported by db_tools
sys.modules["tools.aws.pg_client"] = _fake_pg

from tools import db_tools as dbt  # noqa: E402

FREEZE = 200_000_000  # autovacuum_freeze_max_age default


def _thresholds():
    return dbt._check_references().get("xid-wraparound-database", {}).get("thresholds", {})


def _metrics(xid_age, freeze_max_age=FREEZE):
    """A healthy metrics row apart from the XID fields under test."""
    pct = round(100.0 * xid_age / freeze_max_age, 1) if freeze_max_age else 0
    return {
        "conn_pct": 5.0, "current_conn": 5, "max_conn": 100,
        "xid_pct": pct, "xid_age": xid_age, "freeze_max_age": freeze_max_age,
        "cache_hit_ratio": 99.5,
        "long_queries": 0, "waiting_queries": 0,
        "needs_vacuum": 0, "total_tables": 50,
        "blocked_queries": 0,
    }


def _xid_findings(summary):
    return [f for f in summary.get("findings", []) if f.get("area") == "XID"]


# ── the shared classifier, against the real references.yaml bands ──

def test_reference_bands_load():
    thr = _thresholds()
    assert thr.get("relative") and thr.get("absolute"), "xid-wraparound-database thresholds present"


def test_relative_watch_is_not_emergency():
    # ~85% of autovacuum_freeze_max_age: routine autovacuum territory, and exactly what
    # the old code called an EMERGENCY (>80%). The reference bands make this WATCH.
    sev = dbt._severity_from_xid_bands(_thresholds(), int(0.85 * FREEZE), FREEZE)
    assert sev == "WATCH", sev


def test_absolute_floor_is_emergency_even_when_relative_is_calm():
    # A large freeze_max_age keeps the relative tier low, but the absolute 1.8B floor
    # still catches genuine wraparound risk.
    sev = dbt._severity_from_xid_bands(_thresholds(), 1_800_000_000, 2_000_000_000)
    assert sev == "EMERGENCY", sev


def test_none_age_is_ok():
    assert dbt._severity_from_xid_bands(_thresholds(), None, FREEZE) == "OK"


# ── get_health_summary end to end (fake single metrics row) ──

def test_health_summary_healthy_xid_has_no_emergency():
    _ROW["row"] = [_metrics(int(0.85 * FREEZE))]   # ~85% of freeze — was a false EMERGENCY
    summary = dbt.get_health_summary("arn:secret", "postgres")
    assert _xid_findings(summary) == [], _xid_findings(summary)
    assert summary.get("overall_score") == "ok", summary.get("overall_score")


def test_health_summary_flags_real_emergency():
    _ROW["row"] = [_metrics(1_800_000_000)]        # at the absolute wraparound floor
    summary = dbt.get_health_summary("arn:secret", "postgres")
    xid = _xid_findings(summary)
    assert len(xid) == 1 and xid[0]["severity"] == "EMERGENCY", xid
    assert summary.get("overall_score") == "critical"


# ── the divergent, dead findings engine stays deleted ──

def test_dead_findings_engine_absent():
    backend = pathlib.Path(__file__).resolve().parents[1]
    assert not (backend / "utils" / "findings_engine.py").exists(), \
        "backend/utils/findings_engine.py must stay deleted (divergent dead copy)"


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
