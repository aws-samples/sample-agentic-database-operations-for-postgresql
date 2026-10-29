# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Error-response sanitization for utils.error_sanitizer.

A 5xx must not carry raw exception text to the client, and a 422 must not echo the
caller's submitted input back. These test the pure helpers the app's exception handlers
delegate to, so no FastAPI app import is needed.

Run:  python3 backend/tests/test_error_sanitizer.py   (or via pytest)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.error_sanitizer import (  # noqa: E402
    http_exception_body,
    sanitize_validation_errors,
    SERVER_ERROR_CODE,
    SERVER_ERROR_MESSAGE,
)

RAW = "boto3 error: arn:aws:secretsmanager:us-east-1:123456789012:secret:db-ro-AbCdEf not found"


def test_5xx_is_generic_and_hides_raw_detail():
    body = http_exception_body(500, RAW)
    assert body["detail"] == SERVER_ERROR_MESSAGE
    assert body["code"] == SERVER_ERROR_CODE
    # the raw text (and anything inside it) is gone
    assert RAW not in str(body) and "123456789012" not in str(body)


def test_503_is_also_generic():
    body = http_exception_body(503, "upstream boom at 10.0.4.7")
    assert body["detail"] == SERVER_ERROR_MESSAGE and body["code"] == SERVER_ERROR_CODE


def test_4xx_detail_is_preserved():
    # Client errors are developer-authored and intentional; keep them.
    assert http_exception_body(400, "content or pdf_base64 required") == {
        "detail": "content or pdf_base64 required"
    }
    assert http_exception_body(404, "Not found") == {"detail": "Not found"}
    # a 4xx carries no typed code (shape unchanged from FastAPI default)
    assert "code" not in http_exception_body(400, "x")


def test_validation_errors_drop_echoed_input():
    raw_errors = [
        {"loc": ["body", "email"], "msg": "value is not a valid email address",
         "type": "value_error.email", "input": "attacker@evil.example",
         "ctx": {"reason": "attacker@evil.example"}},
        {"loc": ["body", "count"], "msg": "input should be a valid integer",
         "type": "int_parsing", "input": "'; DROP TABLE users; --"},
    ]
    cleaned = sanitize_validation_errors(raw_errors)
    # shape preserved: still a list of loc/msg/type
    assert len(cleaned) == 2
    assert cleaned[0]["loc"] == ["body", "email"]
    assert cleaned[0]["msg"] == "value is not a valid email address"
    assert cleaned[0]["type"] == "value_error.email"
    # no entry carries the echoed value: only loc/msg/type keys survive (the "input" and
    # "ctx" keys, which hold what the caller submitted, are dropped)
    for entry in cleaned:
        assert set(entry.keys()) == {"loc", "msg", "type"}, entry
    # and the submitted values themselves appear nowhere
    blob = str(cleaned)
    assert "attacker@evil.example" not in blob
    assert "DROP TABLE" not in blob


def test_validation_errors_tolerate_junk():
    assert sanitize_validation_errors(None) == []
    assert sanitize_validation_errors([]) == []
    assert sanitize_validation_errors(["not-a-dict", 42]) == []


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
