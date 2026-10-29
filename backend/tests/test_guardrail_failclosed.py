# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Guardrail fail-closed tests for model_resolver.

Guardrail config resolution FAILS CLOSED (refuses the LLM invocation) when
GUARDRAIL_ID/VERSION are missing — but ONLY
when enforcement is explicitly enabled (GUARDRAIL_ENFORCE), so the default behavior is
byte-identical to today (fail-soft) and the flip can't cause an outage. When the
guardrail IS configured, behavior is unchanged regardless of the flag.

Hermetic: model_resolver is stdlib-only at import (strands is imported lazily inside
build_bedrock_model, which these tests don't call).

Run:  python3 backend/tests/test_guardrail_failclosed.py   (or via pytest)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import model_resolver as mr  # noqa: E402


def _clear_env():
    for k in ("GUARDRAIL_ID", "GUARDRAIL_VERSION", "GUARDRAIL_ENFORCE"):
        os.environ.pop(k, None)


def test_enforcement_flag_defaults_off():
    _clear_env()
    assert mr.guardrail_enforcement_enabled() is False


def test_enforcement_flag_parses_truthy():
    for v in ("1", "true", "on", "required", "closed"):
        os.environ["GUARDRAIL_ENFORCE"] = v
        assert mr.guardrail_enforcement_enabled() is True
    _clear_env()


def test_missing_config_failsoft_by_default():
    # Default (enforcement OFF) + no guardrail configured => {} (today's behavior).
    _clear_env()
    assert mr.guardrail_kwargs_from_env() == {}


def test_missing_config_failsclosed_when_enforced():
    # Enforcement ON + no guardrail configured => refuse (raise), not silently off.
    _clear_env()
    os.environ["GUARDRAIL_ENFORCE"] = "1"
    try:
        raised = False
        try:
            mr.guardrail_kwargs_from_env()
        except mr.GuardrailConfigError:
            raised = True
        assert raised is True
    finally:
        _clear_env()


def test_configured_returns_kwargs_regardless_of_flag():
    # When configured, behave exactly as today whether or not enforcement is on.
    _clear_env()
    os.environ["GUARDRAIL_ID"] = "gr-123"
    os.environ["GUARDRAIL_VERSION"] = "DRAFT"
    try:
        kw_off = mr.guardrail_kwargs_from_env()
        os.environ["GUARDRAIL_ENFORCE"] = "1"
        kw_on = mr.guardrail_kwargs_from_env()
        for kw in (kw_off, kw_on):
            assert kw.get("guardrail_id") == "gr-123"
            assert kw.get("guardrail_version") == "DRAFT"
            assert kw.get("guardrail_redact_output") is True
    finally:
        _clear_env()


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
