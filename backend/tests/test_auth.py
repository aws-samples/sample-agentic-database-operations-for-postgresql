# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Authorization role tests for utils.auth.

Covers the security-critical role decisions behind the Super Admin / Space Admin /
Operator model. Pure logic (JWT claim decode) — no AWS or app dependencies.

Run directly:   python3 backend/tests/test_auth.py
Or via pytest:  pytest backend/tests/test_auth.py
"""
import base64
import json
import os
import sys

# Make `utils` importable whether run from repo root or backend/.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import utils.auth as auth_mod  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from utils.auth import (  # noqa: E402
    get_user_id,
    get_username,
    is_admin,
    is_space_admin,
    is_space_member,
    space_group_name,
    is_m2m_principal,
    jwt_verification_enabled,
    jwt_verification_mode,
    jwt_rejection_reason,
    verify_jwt_signature,
    verify_and_log,
)


def _bearer(claims: dict) -> str:
    """Build a 'Bearer x.<payload>.y' token whose middle segment carries claims."""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "Bearer x." + payload + ".y"


SPACE_A = "11111111-1111-1111-1111-111111111111"
SPACE_B = "22222222-2222-2222-2222-222222222222"


def test_super_admin():
    t = _bearer({"sub": "u1", "cognito:groups": ["admins"]})
    assert is_admin(t) is True
    assert is_space_admin(t) is False
    # Super admins pass membership for ANY space.
    assert is_space_member(t, SPACE_A) is True
    assert is_space_member(t, SPACE_B) is True


def test_space_admin_role_marker_only():
    # The role alone (no space group) grants no space membership.
    t = _bearer({"sub": "u2", "cognito:groups": ["space-admins"]})
    assert is_admin(t) is False
    assert is_space_admin(t) is True
    assert is_space_member(t, SPACE_A) is False


def test_space_admin_scoped_to_their_space():
    t = _bearer({"sub": "u3", "cognito:groups": ["space-admins", space_group_name(SPACE_A)]})
    assert is_space_admin(t) is True
    assert is_space_member(t, SPACE_A) is True     # their space
    assert is_space_member(t, SPACE_B) is False    # someone else's


def test_operator_scoped_to_their_space():
    t = _bearer({"sub": "u4", "cognito:groups": ["operators", space_group_name(SPACE_A)]})
    assert is_admin(t) is False
    assert is_space_admin(t) is False
    assert is_space_member(t, SPACE_A) is True
    assert is_space_member(t, SPACE_B) is False


def test_no_groups_denied():
    t = _bearer({"sub": "u5"})
    assert is_admin(t) is False
    assert is_space_admin(t) is False
    assert is_space_member(t, SPACE_A) is False


def test_empty_space_id_never_member():
    t = _bearer({"sub": "u6", "cognito:groups": ["space-admins"]})
    assert is_space_member(t, "") is False


def test_get_username_prefers_cognito_username():
    assert get_username(_bearer({"cognito:username": "alice", "username": "x", "sub": "s"})) == "alice"
    assert get_username(_bearer({"username": "bob", "sub": "s"})) == "bob"
    assert get_username(_bearer({"sub": "s"})) == "s"
    assert get_username("") == ""


def test_get_user_id():
    assert get_user_id(_bearer({"sub": "abc"})) == "abc"
    assert get_user_id("") == "unknown"


# ── M2M principal detection (used by /api/learn authz) ──

def test_m2m_principal_true_for_client_credentials_token():
    # Cognito client-credentials access token: scope present, NO username, NO groups.
    t = _bearer({"token_use": "access", "scope": "agent-platform-id/service", "sub": "clientId"})
    assert is_m2m_principal(t) is True


def test_m2m_principal_false_for_human_access_token():
    # Human access token carries a username (and a user scope).
    t = _bearer({"token_use": "access", "username": "alice", "scope": "aws.cognito.signin.user.admin"})
    assert is_m2m_principal(t) is False


def test_m2m_principal_false_for_id_token():
    t = _bearer({"token_use": "id", "cognito:username": "bob"})
    assert is_m2m_principal(t) is False


def test_m2m_principal_false_when_groups_present():
    # A token bearing group claims is a human identity, not the service principal.
    t = _bearer({"token_use": "access", "scope": "x", "cognito:groups": ["operators"]})
    assert is_m2m_principal(t) is False


def test_m2m_principal_false_without_scope_or_bearer():
    assert is_m2m_principal(_bearer({"token_use": "access"})) is False  # no scope
    assert is_m2m_principal("") is False
    assert is_m2m_principal("Basic abc") is False


# ── LOG-ONLY JWKS verification never rejects ──

def test_jwt_verification_flag_defaults_off():
    os.environ.pop("JWT_SIGNATURE_VERIFICATION", None)
    assert jwt_verification_enabled() is False


def test_jwt_verification_flag_parses_truthy():
    for v in ("log", "log_only", "1", "true", "on"):
        os.environ["JWT_SIGNATURE_VERIFICATION"] = v
        assert jwt_verification_enabled() is True
    os.environ.pop("JWT_SIGNATURE_VERIFICATION", None)


def test_verify_and_log_is_noop_when_disabled():
    os.environ.pop("JWT_SIGNATURE_VERIFICATION", None)
    # Even a garbage token must not raise and must not reject (returns None).
    assert verify_and_log(_bearer({"sub": "x"})) is None
    assert verify_and_log("Bearer not.a.jwt") is None
    assert verify_and_log("") is None


def test_verify_and_log_never_rejects_when_enabled():
    # Flag ON + a token that cannot possibly verify (no real signing key). The
    # log-only path must swallow the mismatch and return None (never raise/reject).
    os.environ["JWT_SIGNATURE_VERIFICATION"] = "log"
    os.environ.setdefault("USER_POOL_ID", "us-east-1_TESTPOOL")
    try:
        assert verify_and_log(_bearer({"sub": "x", "token_use": "access"})) is None
        assert verify_and_log("Bearer garbage") is None
    finally:
        os.environ.pop("JWT_SIGNATURE_VERIFICATION", None)


def test_verify_jwt_signature_returns_result_dict_never_raises():
    # Returns a structured result; verified is None (pyjwt/crypto or key issues) or
    # False (mismatch) — never True for a forged token, and never raises.
    res = verify_jwt_signature("clearly.not.a.jwt")
    assert isinstance(res, dict) and "verified" in res
    assert res["verified"] in (None, False)
    assert verify_jwt_signature("")["verified"] is None


# ── ENFORCING in-app JWT verification ────────────────────────────────
# jwt_rejection_reason decides reject-vs-allow. verify_jwt_signature itself (the PyJWT
# call) is exercised elsewhere; here we PATCH it so the decision logic is tested
# deterministically, regardless of whether PyJWT is installed in the test env.

@contextmanager
def _env(**pairs):
    """Temporarily set/clear env vars, restoring prior values afterwards."""
    prior = {k: os.environ.get(k) for k in pairs}
    for k, v in pairs.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _patched_verify(result):
    """Patch auth_mod.verify_jwt_signature to return `result` (a dict)."""
    orig = auth_mod.verify_jwt_signature
    auth_mod.verify_jwt_signature = lambda token: dict(result)
    try:
        yield
    finally:
        auth_mod.verify_jwt_signature = orig


def test_jwt_verification_mode_resolves():
    for v in ("enforce", "reject", "gate", "ENFORCE"):
        with _env(JWT_SIGNATURE_VERIFICATION=v):
            assert jwt_verification_mode() == "enforce"
    for v in ("log", "log_only", "on", "1", "true", "yes"):
        with _env(JWT_SIGNATURE_VERIFICATION=v):
            assert jwt_verification_mode() == "log"
    for v in ("off", "false", "", "nonsense"):
        with _env(JWT_SIGNATURE_VERIFICATION=v):
            assert jwt_verification_mode() == "off"
    with _env(JWT_SIGNATURE_VERIFICATION=None):
        assert jwt_verification_mode() == "off"


def test_rejection_reason_off_mode_never_rejects():
    with _env(JWT_SIGNATURE_VERIFICATION="off"), _patched_verify({"verified": False, "reason": "bad sig"}):
        assert jwt_rejection_reason(_bearer({"sub": "x"})) is None


def test_rejection_reason_missing_or_non_bearer_header_allows():
    with _env(JWT_SIGNATURE_VERIFICATION="enforce"):
        assert jwt_rejection_reason("") is None
        assert jwt_rejection_reason("Basic abc") is None


def test_enforce_rejects_genuine_failure():
    # A real signature/claim failure (not an infra reason) must be rejected in enforce mode.
    with _env(JWT_SIGNATURE_VERIFICATION="enforce"), _patched_verify({"verified": False, "reason": "bad signature"}):
        assert jwt_rejection_reason(_bearer({"sub": "x"})) == "bad signature"


def test_enforce_fails_open_on_infra_unavailability():
    # Could-not-attempt (JWKS unreachable, PyJWT missing, no pool) => allow; the edge
    # already verified the request, so a key-endpoint blip must not become an auth outage.
    for reason in ("pyjwt-unavailable", "no-user-pool", "no-jwks", "no-token"):
        with _env(JWT_SIGNATURE_VERIFICATION="enforce"), _patched_verify({"verified": None, "reason": reason}):
            assert jwt_rejection_reason(_bearer({"sub": "x"})) is None


def test_enforce_allows_fully_verified_token_when_no_client_ids_configured():
    with _env(JWT_SIGNATURE_VERIFICATION="enforce", WEB_CLIENT_ID=None, M2M_CLIENT_ID=None), \
            _patched_verify({"verified": True, "reason": "ok"}):
        assert jwt_rejection_reason(_bearer({"token_use": "access", "client_id": "anything"})) is None


def test_enforce_rejects_wrong_client_binding():
    # Signature good, but the token is bound to an app client that is not this pool's web/M2M client.
    with _env(JWT_SIGNATURE_VERIFICATION="enforce", WEB_CLIENT_ID="web123", M2M_CLIENT_ID="m2m456"), \
            _patched_verify({"verified": True, "reason": "ok"}):
        reason = jwt_rejection_reason(_bearer({"token_use": "access", "client_id": "someoneelse"}))
        assert reason is not None and "someoneelse" in reason


def test_enforce_allows_matching_client_binding():
    with _env(JWT_SIGNATURE_VERIFICATION="enforce", WEB_CLIENT_ID="web123", M2M_CLIENT_ID="m2m456"), \
            _patched_verify({"verified": True, "reason": "ok"}):
        # access token binds via client_id
        assert jwt_rejection_reason(_bearer({"token_use": "access", "client_id": "web123"})) is None
        # id token binds via aud
        assert jwt_rejection_reason(_bearer({"token_use": "id", "aud": "m2m456"})) is None


def test_log_mode_never_rejects_even_on_genuine_failure():
    with _env(JWT_SIGNATURE_VERIFICATION="log"), _patched_verify({"verified": False, "reason": "bad signature"}):
        assert jwt_rejection_reason(_bearer({"sub": "x"})) is None


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
