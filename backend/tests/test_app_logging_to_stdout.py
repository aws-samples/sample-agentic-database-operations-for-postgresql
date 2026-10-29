# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The app logs to STDOUT.

The AgentCore runtime forwards the container's stdout to CloudWatch and drops stderr, so
application logging must be configured with stream=sys.stdout; basicConfig's default is
stderr. Pins the basicConfig call by source. Hermetic: app.py is not imported (it pulls in
strands/boto3 at module load).

Run:  python3 backend/tests/test_app_logging_to_stdout.py   (or via pytest)
"""
import os
import re
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app.py")


def _basic_config_call() -> str:
    src = open(APP, encoding="utf-8").read()
    m = re.search(r"logging\.basicConfig\((.*?)\)\n", src, re.S)
    assert m, "app.py must configure logging with logging.basicConfig(...)"
    return m.group(1)


def test_logging_goes_to_stdout():
    args = _basic_config_call()
    assert re.search(r"stream\s*=\s*sys\.stdout", args), \
        "basicConfig must pass stream=sys.stdout; the runtime does not capture stderr"


def test_logging_config_is_forced():
    # basicConfig is a no-op when a library has already configured logging; force=True
    # replaces any handler installed at import.
    args = _basic_config_call()
    assert re.search(r"force\s*=\s*True", args), "basicConfig must use force=True"


def test_sys_is_imported():
    src = open(APP, encoding="utf-8").read()
    assert re.search(r"^import sys$", src, re.M), "app.py must import sys for stream=sys.stdout"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {e!r}")
    sys.exit(1 if failures else 0)
