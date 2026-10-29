# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""CHK-044 TLS verdict (tools.db_tools._severity_ssl).

The SSL check used to grade only the live connection mix (pg_stat_ssl), so an instance
that ALLOWS plaintext read "OK" whenever no plaintext session happened to be connected.
The verdict now leads with the instance's enforcement, rds.force_ssl: force_ssl off is a
finding regardless of the live mix. These tests pin that, and the unknown-enforcement
fallback (off RDS / unreadable parameter) to the old live-mix behavior.

Hermetic: strands/boto3 stubbed, pg_client faked, so importing db_tools touches no DB.

Run:  python3 backend/tests/test_ssl_enforcement.py   (or via pytest)
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_strands = types.ModuleType("strands")
_strands.tool = lambda f: f
sys.modules.setdefault("strands", _strands)
sys.modules.setdefault("boto3", types.ModuleType("boto3"))

_fake_pg = types.ModuleType("tools.aws.pg_client")
_fake_pg.execute_query = lambda *a, **k: []
_fake_pg.test_connection = lambda *a, **k: {"ok": True}
_fake_pg.lookup_record_database_name = lambda *a, **k: ""
_fake_pg.SECRET_NOT_IN_SPACE_MESSAGE = "not available in the current space"
sys.modules["tools.aws.pg_client"] = _fake_pg

from tools import db_tools as dbt  # noqa: E402

sev = dbt._severity_ssl


# ── force_ssl OFF: a finding regardless of the live mix ─────────────────────────

def test_force_ssl_off_with_no_plaintext_is_still_a_warning():
    # The decisive case: nobody is connected in plaintext right now, but the instance
    # ALLOWS it. Old logic said OK; it must now warn.
    assert sev("off", 10, 0) == "WARNING"
    assert sev("0", 5, 0) == "WARNING"


def test_force_ssl_off_with_plaintext_present_is_critical():
    assert sev("off", 3, 2) == "CRITICAL"
    assert sev("0", 0, 1) == "CRITICAL"


# ── force_ssl ON: enforced ──────────────────────────────────────────────────────

def test_force_ssl_on_all_encrypted_is_ok():
    assert sev("on", 12, 0) == "OK"
    assert sev("1", 4, 0) == "OK"
    assert sev("true", 1, 0) == "OK"


def test_force_ssl_on_but_plaintext_leaks_is_flagged():
    # Should not happen when enforced, but if a plaintext session exists, still flag it.
    assert sev("on", 10, 1) == "WARNING"
    assert sev("on", 1, 5) == "CRITICAL"


# ── enforcement UNKNOWN (off RDS, or parameter unreadable): live-mix fallback ────

def test_unknown_enforcement_falls_back_to_live_mix():
    assert sev(None, 10, 0) == "OK"            # none connected in plaintext
    assert sev(None, 10, 1) == "WARNING"       # some plaintext, encrypted dominant
    assert sev(None, 1, 5) == "CRITICAL"       # plaintext dominant
    assert sev("", 2, 0) == "OK"               # empty string reads as unknown, not "off"


def test_case_and_whitespace_tolerant():
    assert sev(" ON ", 3, 0) == "OK"
    assert sev("Off", 3, 0) == "WARNING"


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"  FAIL {name}: {e!r}")
                failed += 1
    print(f"pass={passed} fail={failed}")
    sys.exit(1 if failed else 0)
