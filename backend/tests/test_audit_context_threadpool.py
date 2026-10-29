# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A plain `def` endpoint sees the audit context the middleware set.

Most handlers in backend/app.py are plain `def`, so Starlette runs them through
run_in_threadpool, which (anyio, pinned in requirements.txt) copies the caller's
contextvars into the worker thread. Every actor field of an audit event depends on that,
so it is pinned here with a real FastAPI app: the shipped `_audit_request_context`
middleware (extracted from app.py by source, its auth helpers stubbed) and one sync
route that returns what it sees.

Needs fastapi (and httpx for TestClient), which the hermetic `make test` interpreter
does not have: then it prints "skipped: fastapi not installed" as its last line and
exits 0. It runs wherever backend/requirements.txt is installed.

Run:  python3 backend/tests/test_audit_context_threadpool.py
"""
import os
import pathlib
import re
import sys

try:
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
except Exception:  # noqa: BLE001 - fastapi or httpx missing
    print("skipped: fastapi not installed")
    sys.exit(0)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import time  # noqa: E402

from tools.aws import audit_log  # noqa: E402

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


def _middleware_src() -> str:
    m = re.search(r"^async def _audit_request_context\(.*?(?=^(?:@|def |async def |class )|\Z)",
                  APP_SRC, re.S | re.M)
    assert m, "_audit_request_context not found in app.py"
    return m.group(0)


app = FastAPI()
ns = {
    "audit_log": audit_log, "time": time, "Request": Request,
    "decode_jwt_unverified": lambda token: {"email": "super@example.com"},
    "get_user_id": lambda auth: "super-1" if auth else "unknown",
    "get_username": lambda auth: "",
    "get_user_groups": lambda auth: ["admins"] if auth else [],
    "_email_for_sub": lambda sub: "",
}
exec(_middleware_src(), ns)  # noqa: S102 - pinning shipped source
app.middleware("http")(ns["_audit_request_context"])


@app.get("/whoami")
def whoami():
    ctx = audit_log.AUDIT_CONTEXT.get()
    return {"sub": ctx.get("sub"), "email": ctx.get("email"), "groups": ctx.get("groups"),
            "request_id": ctx.get("request_id"), "has_deadline": "deadline" in ctx}


client = TestClient(app)
r = client.get("/whoami", headers={"Authorization": "Bearer x.y.z", "x-dbops-request-id": "req-9"})
body = r.json()
check(r.status_code == 200, "the sync route answers")
check(body["sub"] == "super-1", "a plain def route sees the bearer's sub (run_in_threadpool copies the context)")
check(body["email"] == "super@example.com" and body["groups"] == ["admins"], "and its email and groups")
check(body["request_id"] == "req-9" and body["has_deadline"] is True, "and the request id and the deadline")
check(dict(audit_log.AUDIT_CONTEXT.get()) == {}, "the test thread's own context is untouched")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
