# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""get_db_configuration reports the INSTANCE value, not this session's override (CHK-041).

The read-only client sets statement_timeout on its own connection, so pg_settings reports
setting=30000, source=session for it, with the instance value (0) sitting in reset_val.
The tool used to surface `setting`, so it reported the agent's own 30s and hid the
instance's "no timeout". It now derives `effective_value` from reset_val for any
session/client-sourced parameter, and CHK-041 judges that effective value — so a value this
session happened to set can neither be reported as the database's config nor create or hide
a safety finding.

Hermetic: strands/boto3 stubbed, pg_client faked, deployment detection stubbed.

Run:  python3 backend/tests/test_config_effective_value.py   (or via pytest)
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_strands = types.ModuleType("strands")
_strands.tool = lambda f: f
sys.modules.setdefault("strands", _strands)
sys.modules.setdefault("boto3", types.ModuleType("boto3"))

# One synthetic pg_settings result. autovacuum carries a (synthetic) source=session so the
# test can prove CHK-041 reads the effective value and not `setting`; the others are realistic.
PARAMS = [
    {"name": "statement_timeout", "setting": "30000", "source": "session", "reset_val": "0", "unit": "ms"},
    {"name": "idle_in_transaction_session_timeout", "setting": "0", "source": "default", "reset_val": "0"},
    {"name": "autovacuum", "setting": "off", "source": "session", "reset_val": "on"},
    {"name": "ssl", "setting": "off", "source": "configuration file", "reset_val": "off"},
    {"name": "shared_preload_libraries", "setting": "pg_stat_statements",
     "source": "configuration file", "reset_val": "pg_stat_statements"},
]

_fake_pg = types.ModuleType("tools.aws.pg_client")
_fake_pg.execute_query = lambda *a, **k: [dict(p) for p in PARAMS]
_fake_pg.test_connection = lambda *a, **k: {"ok": True}
_fake_pg.lookup_record_database_name = lambda *a, **k: ""
_fake_pg.SECRET_NOT_IN_SPACE_MESSAGE = "not available in the current space"
sys.modules["tools.aws.pg_client"] = _fake_pg

from tools import db_tools as dbt  # noqa: E402

dbt._detect_deployment = lambda *a, **k: "RDS"  # isolate the test to the parameter logic


def _config():
    return dbt.get_db_configuration.__wrapped__("db-1") if hasattr(dbt.get_db_configuration, "__wrapped__") \
        else dbt.get_db_configuration("db-1")


def _by_name(cfg):
    return {p["name"]: p for p in cfg["parameters"]}


def test_session_override_reports_the_instance_value():
    params = _by_name(_config())
    st = params["statement_timeout"]
    assert st["effective_value"] == "0", f"instance value (reset_val) must win: {st}"
    assert st["overridden_by_session"] is True
    assert st["setting"] == "30000", "the raw session value is still present for transparency"


def test_non_overridden_param_passes_setting_through():
    params = _by_name(_config())
    idle = params["idle_in_transaction_session_timeout"]
    assert idle["effective_value"] == "0" and idle["overridden_by_session"] is False
    spl = params["shared_preload_libraries"]
    assert spl["effective_value"] == "pg_stat_statements" and spl["overridden_by_session"] is False


def test_instance_level_critical_is_still_detected():
    cfg = _config()
    assert cfg["status"] == "CRITICAL", cfg.get("status")
    assert any("ssl=off" in f for f in cfg["config_findings"]), cfg["config_findings"]


def test_a_session_override_cannot_forge_a_critical():
    # autovacuum shows setting=off from the (synthetic) session source, but the instance has
    # it on (reset_val). Judging the effective value, it must NOT be flagged.
    cfg = _config()
    assert not any("autovacuum" in f.lower() for f in cfg["config_findings"]), \
        f"a session-local value must not create an instance finding: {cfg['config_findings']}"


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
