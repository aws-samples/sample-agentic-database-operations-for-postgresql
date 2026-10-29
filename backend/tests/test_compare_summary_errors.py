# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A failed compare-summary model call never returns the raw exception text.

``POST /api/reports/compare-summary`` answers 200 with the failure inside the body, so no
error sanitizer sees it. A Bedrock AccessDenied names the deployment's account id, the
task role ARN and the model id; none of them may reach the caller. The detail is logged,
the answer is a fixed sentence carrying only the error code.

The handler is extracted from backend/app.py by source and run against a fake Bedrock
client. Hermetic, no AWS, no FastAPI.

Run:  python3 backend/tests/test_compare_summary_errors.py
"""
import json
import os
import pathlib
import re
import sys
import types

APP_SRC = (pathlib.Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def _extract(name):
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^(?:@|def |class |[A-Z_]+ = )|\Z)", APP_SRC, re.S | re.M)
    assert m, f"{name} not found in app.py"
    return m.group(0)


ACCOUNT = "123456789012"
ROLE_ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/dbops-api-task-role/abc"
MODEL = "us.anthropic.claude-sonnet-4-20250514-v1:0"


class FakeClientError(Exception):
    def __init__(self, code, message):
        super().__init__(f"An error occurred ({code}) when calling the InvokeModel operation: {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


class FakeBedrock:
    def __init__(self, raises=None, text="Health improved."):
        self.raises, self.text = raises, text

    def invoke_model(self, **kw):
        if self.raises:
            raise self.raises
        body = json.dumps({"content": [{"text": self.text}]}).encode()
        return {"body": types.SimpleNamespace(read=lambda: body)}


def build(client):
    logs = []
    ns = {
        "json": json, "os": os, "re": re,
        "boto3": types.SimpleNamespace(client=lambda *a, **k: client),
        "HTTPException": Exception, "Request": object,
        "check_rate_limit": lambda uid: (True, 0), "get_user_id": lambda auth: "u1",
        "compare_reports": lambda *a, **k: {"report1": {}, "report2": {}, "sections": {}, "findings_diff": {}},
        "effective_model_id": lambda: MODEL,
        "helper_model_id": lambda: MODEL,
        "surface_model_kwargs": lambda mid, surface: {"thinking": {"type": "disabled"}},
        "SURFACE_HELPER": "helper",
        "guardrail_invoke_kwargs_from_env": lambda: {},
        "logger": types.SimpleNamespace(error=lambda *a, **k: logs.append(a[0] % a[1:]),
                                        info=lambda *a, **k: None, warning=lambda *a, **k: None),
    }
    exec(_extract("compare_summary"), ns)  # noqa: S102 - pinning shipped source
    return ns["compare_summary"], logs


REQ = types.SimpleNamespace(headers={"Authorization": "Bearer x"})

print("compare-summary failure text")
err = FakeClientError(
    "AccessDeniedException",
    f"User: {ROLE_ARN} is not authorized to perform: bedrock:InvokeModel on resource: "
    f"arn:aws:bedrock:us-east-1:{ACCOUNT}:inference-profile/{MODEL}")
fn, logs = build(FakeBedrock(raises=err))
out = fn(REQ, id1="1", id2="2")
s = out.get("summary", "")
check(ACCOUNT not in s, "the account id is not in the answer")
check("arn:" not in s, "no ARN in the answer")
check(MODEL not in s and "claude" not in s, "the model id is not in the answer")
check("AccessDeniedException" in s, "the answer names the error code")
check(s.startswith("Summary generation failed"), "the answer says it failed")
check(any(ROLE_ARN in line for line in logs), "the detail is logged server-side")

fn, logs = build(FakeBedrock(raises=RuntimeError(f"connect to bedrock-runtime as {ROLE_ARN} timed out")))
s = fn(REQ, id1="1", id2="2")["summary"]
check(ACCOUNT not in s and "arn:" not in s, "a non-AWS error leaks nothing either")
check("RuntimeError" in s, "a non-AWS error is named by its class")

bad = FakeClientError(f"Denied {ACCOUNT} arn:aws:iam::{ACCOUNT}:role/x", "m")
fn, logs = build(FakeBedrock(raises=bad))
s = fn(REQ, id1="1", id2="2")["summary"]
check(ACCOUNT not in s, "an error code that is not a plain code is not echoed")

fn, logs = build(FakeBedrock(text="  Health improved.  "))
check(fn(REQ, id1="1", id2="2") == {"summary": "Health improved."}, "a real summary is returned as before")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
