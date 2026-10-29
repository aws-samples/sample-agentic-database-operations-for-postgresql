#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""GET /api/agent-role-policy is admin-gated, and the M2M token is confined to /api/learn.

The endpoint returns the deployment's account id and the agent's full permission
set. The button that calls it only appears in the admin console's space-creation panel,
but UI visibility is not authorization: the endpoint is reachable by any token the API
authorizer accepts. That used to include a token minted for the M2M (client-credentials)
client — no user, no MFA — which the single shared authorizer admitted on every route, and
any operator's token, since both SPAs share the web client.

This pins both fixes as static source facts:

  * The handler takes a Request and enforces `is_admin`/`is_space_admin` server-side, and
    resolves the real account id only for super admins.
  * The main API authorizer's audience is the web client only; the M2M client is admitted
    solely by a dedicated learn-route authorizer, so the machine token cannot reach any
    route other than POST /api/learn.

Static reads only (no live API): backend/app.py and infrastructure/api.yaml.

Run:  python3 tests/test_agent_role_policy_authz.py   (or via pytest)
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "backend", "app.py")
API = os.path.join(ROOT, "infrastructure", "api.yaml")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _handler_body():
    text = _read(APP)
    m = re.search(
        r'@app\.get\("/api/agent-role-policy"\)\n\s*def get_agent_role_policy\(([^)]*)\):(.*?)(?=\n@app\.)',
        text, re.S,
    )
    assert m, "could not find the get_agent_role_policy handler"
    return m.group(1), m.group(2)


def test_policy_endpoint_takes_request_and_gates_on_admin():
    sig, body = _handler_body()
    assert "request" in sig and "Request" in sig, \
        "the handler must take a Request so it can read the caller's token"
    assert "is_admin(auth)" in body and "is_space_admin(auth)" in body, \
        "the handler must gate on is_admin/is_space_admin, not just rely on the hidden button"
    assert re.search(r"raise HTTPException\(status_code=403", body), \
        "a non-admin caller must be refused with 403"


def test_policy_endpoint_reveals_account_id_only_to_super_admin():
    _sig, body = _handler_body()
    # The STS account-id resolution must sit behind an is_admin gate; space admins and
    # everyone else get the placeholder.
    assert re.search(r'account_id\s*=\s*"<this-account>"', body), \
        "the account id must default to the placeholder"
    assert re.search(r"if is_admin\(auth\):\s*\n\s*try:\s*\n\s*sts\s*=\s*boto3\.client", body), \
        "the real account id must be resolved only for super admins"


def _api_block(start_marker, end_marker):
    text = _read(API)
    start = text.index(start_marker)
    end = text.index(end_marker, start + len(start_marker))
    return text[start:end]


def test_main_authorizer_audience_is_web_only():
    block = _api_block("\n  JwtAuthorizer:", "\n  LearnJwtAuthorizer:")
    aud = block[block.index("Audience:"):]
    assert "WebClientId" in aud, "the main authorizer must admit the web client"
    assert "M2MClientId" not in aud, (
        "the main authorizer must NOT admit the M2M client — a user-less machine token "
        "would otherwise reach every route"
    )


def test_learn_authorizer_admits_the_m2m_client():
    block = _api_block("\n  LearnJwtAuthorizer:", "\n  ALBIntegration:")
    assert "M2MClientId" in block, \
        "the learn authorizer must admit the M2M client so the learning agent can call it"
    assert "WebClientId" in block, \
        "the learn authorizer should still admit web tokens too"


def test_learn_route_uses_the_learn_authorizer():
    text = _read(API)
    block = text[text.index("\n  LearnRoute:"):]
    block = block[:block.index("Target:") + 60]
    assert "AuthorizerId: !Ref LearnJwtAuthorizer" in block, \
        "the learn route must use the dedicated learn authorizer, not the main one"


def test_only_the_learn_route_uses_the_learn_authorizer():
    # Any other route pointing at the learn authorizer would re-open the M2M surface.
    text = _read(API)
    refs = re.findall(r"AuthorizerId: !Ref LearnJwtAuthorizer", text)
    assert len(refs) == 1, f"expected exactly one route on the learn authorizer, found {len(refs)}"


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
                passed += 1
            except AssertionError as e:
                print(f"  FAIL {name}: {e}")
                failed += 1
            except Exception as e:
                print(f"  ERR  {name}: {type(e).__name__}: {e}")
                failed += 1
    print(f"pass={passed} fail={failed}")
    sys.exit(1 if failed else 0)
