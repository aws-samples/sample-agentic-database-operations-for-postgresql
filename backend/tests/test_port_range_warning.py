# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Configure warns when a database's port is outside DB_PORT_RANGE.

The Tier 2 scan task's security group allows egress to the DB_PORT_RANGE ports only
(plus 5432 for the metrics store), so a database on another port is never deep-scanned.
Configure still succeeds (the agent and Tier 1 reach it), but writes a warning to the
database's config_log naming the port and the range. A malformed DB_PORT_RANGE is
treated as the default rather than failing Configure.

Run:  python3 backend/tests/test_port_range_warning.py   (or via pytest)
"""
import os
import pathlib
import re
import sys

APP_SRC = (pathlib.Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
m = re.search(r"^def _port_range_warning\(.*?(?=^(?:@|def |class )|\Z)", APP_SRC, re.S | re.M)
assert m, "_port_range_warning not found in app.py"
NS = {"os": os, "re": re}
exec(m.group(0), NS)  # noqa: S102 - pinning shipped source
warn = NS["_port_range_warning"]


def _with(value, port):
    old = os.environ.pop("DB_PORT_RANGE", None)
    if value is not None:
        os.environ["DB_PORT_RANGE"] = value
    try:
        return warn(port)
    finally:
        os.environ.pop("DB_PORT_RANGE", None)
        if old is not None:
            os.environ["DB_PORT_RANGE"] = old


def test_default_range_covers_5432_only():
    assert _with(None, 5432) == ""
    w = _with(None, 5433)
    assert w == ("⚠ Port 5433 is outside DB_PORT_RANGE (5432-5432): the daily deep scan cannot reach "
                 "this database. Widen DB_PORT_RANGE and redeploy."), w


def test_inside_a_wider_range():
    assert _with("5432-5440", 5432) == "" and _with("5432-5440", 5440) == "" and _with("5432-5440", "5436") == ""


def test_outside_a_wider_range():
    w = _with("5432-5440", 6432)
    assert "Port 6432" in w and "(5432-5440)" in w


def test_a_bad_value_is_the_default_and_never_raises():
    for bad in ("abc", "5440-5432", "", "5432"):
        assert _with(bad, 5432) == "", bad
        assert "(5432-5432)" in _with(bad, 5433), bad
    assert _with("5432-5440", None) == "" and _with("5432-5440", "x") == "", "no port: nothing to say"


def test_configure_appends_it_to_the_config_log():
    body = re.search(r"^def _configure_single_db\(.*?(?=^(?:@|def |class )|\Z)", APP_SRC, re.S | re.M).group(0)
    assert "port_warning = _port_range_warning(db_port)" in body and "steps.append(port_warning)" in body
    branch = body.split("if not db_vpc_id or not agent_vpc_id:", 1)[1].split("return", 1)[0]
    assert "_port_range_warning(db_port)" in branch, "the unknown-VPC branch warns too"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  ok   {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {e!r}")
    print(f"pass={passed} fail={failed}")
    sys.exit(1 if failed else 0)
