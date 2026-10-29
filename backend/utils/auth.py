# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import base64
import json


def decode_jwt_unverified(token: str) -> dict:
    """Decode a JWT payload WITHOUT verifying its signature.

    The name says what it is: this reads claims and trusts nothing. It is safe only
    because the API Gateway Cognito authorizer verifies the signature at the edge before a
    request reaches the app, and — when enforcement is on — `jwt_rejection_reason` re-verifies
    it in-app. Never use this as an authorization decision on its own.
    """
    try:
        payload_part = token.split(".")[1]
        padding = 4 - len(payload_part) % 4
        if padding != 4:
            payload_part += "=" * padding
        return json.loads(base64.urlsafe_b64decode(payload_part))
    except Exception:
        return {}


# Backward-compatible alias: existing call sites import `decode_jwt`. The clearer name is
# the definition above; this keeps them working without a sweeping rename.
decode_jwt = decode_jwt_unverified


def get_user_id(auth_header: str) -> str:
    """Extract user ID (sub claim) from Authorization header."""
    if auth_header.startswith("Bearer "):
        claims = decode_jwt(auth_header[7:])
        return claims.get("sub", "unknown")
    return "unknown"


def get_user_groups(auth_header: str) -> list:
    """Extract Cognito groups from Authorization header."""
    if auth_header.startswith("Bearer "):
        claims = decode_jwt(auth_header[7:])
        return claims.get("cognito:groups", [])
    return []


def is_admin(auth_header: str) -> bool:
    """Check if the user is a SUPER ADMIN (Cognito 'admins' group).

    Super admins are global: they pass every per-space check and see all spaces.
    This is the gate for account-global actions (user management, model catalog,
    security posture, global knowledge). Space-scoped management is gated by
    can_manage_space (which also admits Space Admins for their own spaces).
    """
    return "admins" in get_user_groups(auth_header)


def is_space_admin(auth_header: str) -> bool:
    """Check if the user holds the SPACE ADMIN role (Cognito 'space-admins' group).

    A Space Admin can use the admin console but only for the spaces they belong to
    (created or were assigned). The role alone grants nothing — it is always paired
    with membership of a specific space's group. See can_manage_space.
    """
    return "space-admins" in get_user_groups(auth_header)


def get_username(auth_header: str) -> str:
    """Extract the Cognito username from the Authorization header.

    Needed for admin_* group operations (which key on Username, not the 'sub').
    Robust across token types: ID tokens carry 'cognito:username', access tokens
    carry 'username'; fall back to 'sub' as a last resort.
    """
    if auth_header.startswith("Bearer "):
        claims = decode_jwt(auth_header[7:])
        return claims.get("cognito:username") or claims.get("username") or claims.get("sub", "")
    return ""


def space_group_name(space_id: str) -> str:
    """Cognito group name that grants access to a space.

    Uses the FULL space_id (not an 8-char prefix) so two spaces can never collide
    on a shared group. Cognito group names allow up to 128 chars; `space-<uuid>`
    is ~42. Single source of truth for the derivation — every caller (assign/
    remove/list space users, my-spaces, membership checks) goes through this.
    """
    return f"space-{space_id}"


# The two role groups. Neither is a space, and neither counts in the operator app.
ROLE_GROUPS = frozenset({"admins", "space-admins"})


def operator_space_ids(auth_header: str) -> set:
    """The spaces the caller is an *operator* of: exactly the ``space-<id>`` groups in
    the token, inverted through ``space_group_name``.

    This is the operator app's whole access model. It deliberately knows nothing about
    roles — a super admin, or a space's creator, who was never added as an operator gets
    an empty set here, the same as anyone else — so every operator-app read that keys on
    it behaves identically for every operator. Membership only; no registry lookup.

    The role groups are excluded by name: ``space-admins`` shares the ``space-`` prefix
    with the operator groups but names a role, not a space."""
    prefix = space_group_name("")
    return {g[len(prefix):] for g in get_user_groups(auth_header)
            if g.startswith(prefix) and len(g) > len(prefix) and g not in ROLE_GROUPS}


def is_operator_of(auth_header: str, space_id: str) -> bool:
    """True if the caller holds the space's operator group. No admin pass-through, no
    creator fallback: the operator app's gate (see ``operator_space_ids``)."""
    return bool(space_id) and space_id in operator_space_ids(auth_header)


def is_space_member(auth_header: str, space_id: str) -> bool:
    """True if the caller is an admin or belongs to the space's Cognito group.

    Mirrors the membership derivation used by GET /api/my-spaces. Admins always
    pass. The edge (API Gateway Cognito authorizer) verifies the token; this only
    reads the already-verified claims.
    """
    groups = get_user_groups(auth_header)
    if "admins" in groups:
        return True
    if not space_id:
        return False
    return space_group_name(space_id) in groups


# ─────────────────────────────────────────────────────────────────────────────
# Security hardening additions. Everything below is
# ADDITIVE — the existing decode/role helpers above are unchanged. These add
#   (1) a machine-to-machine (M2M) principal check used by /api/learn authz, and
#   (2) flag-gated, LOG-ONLY JWKS signature verification (defense-in-depth).
# ─────────────────────────────────────────────────────────────────────────────

import os
import time
import logging
import urllib.request

logger = logging.getLogger("auth")


def is_m2m_principal(auth_header: str) -> bool:
    """True if the bearer is a Cognito client-credentials (M2M) access token.

    The Learning Agent Lambda calls ``POST /api/learn/*`` with such a token: it is an
    OAuth *client_credentials* access token — it carries a ``scope`` (the
    ``agent-platform-id/service`` resource-server scope) and ``token_use == "access"``,
    but has NO user identity (``username`` / ``cognito:username``) and NO
    ``cognito:groups``. Human access tokens always carry a ``username``. We therefore
    treat a *scoped, user-less* access token as the service principal.

    This only reads claims (the API Gateway edge already verifies the signature, same
    trust model as every other check in this module), so a human token cannot be
    forged into an M2M one past the edge. Used to let the automated learning flow run
    (it holds no space membership) WITHOUT opening ``/api/learn`` to arbitrary humans.
    """
    if not auth_header.startswith("Bearer "):
        return False
    claims = decode_jwt(auth_header[7:])
    if not claims:
        return False
    if claims.get("token_use") != "access":
        return False
    if claims.get("username") or claims.get("cognito:username"):
        return False  # a human access token
    if claims.get("cognito:groups"):
        return False  # M2M client-credentials tokens carry no group claims
    scope = claims.get("scope")
    if not scope:
        return False
    # Optional scope-string allowlist: when M2M_ALLOWED_SCOPES is
    # set (comma-separated resource-server scopes, e.g. "agent-platform-id/service"), the token
    # MUST carry at least one of them — so only the intended M2M client counts as the
    # service principal, not any user-less scoped token. Unset => preserves prior behavior
    # (any scope), so this tightening is opt-in and cannot break an existing deployment
    # until the expected scope is configured.
    allowed = os.getenv("M2M_ALLOWED_SCOPES", "").strip()
    if allowed:
        allowed_set = {s.strip() for s in allowed.split(",") if s.strip()}
        token_scopes = set(scope.split())
        if not (token_scopes & allowed_set):
            return False
    return True


# ── LOG-ONLY JWKS signature verification ────────────────────────────────────
# In-app JWT verification, in observe-only form: VERIFY the signature against the
# Cognito user-pool JWKS and LOG mismatches, but NEVER reject a request. Requests are
# authenticated by the API Gateway Cognito authorizer, which no request can skip
# (api.yaml admits the ALB from the VPC Link alone), so this exists to show whether the
# in-app verifier ever disagrees with that edge rather than to gate traffic. Turning it
# into a gate means running it on every route, not just this one, and would reject
# legitimate callers on any disagreement — a clock skew or a key rotation. Controlled by
# JWT_SIGNATURE_VERIFICATION (unset/false => complete no-op). The JWKS key set is
# cached in-process. PyJWT is imported lazily and guarded: if the library (or its
# crypto backend) is unavailable, verification degrades to a logged no-op — it can
# never break a request in log-only mode.

_JWKS_CACHE: dict = {"url": None, "keys": None, "fetched_at": 0.0}
_JWKS_TTL = int(os.getenv("JWKS_CACHE_TTL", "3600"))  # seconds


def jwt_verification_enabled() -> bool:
    """True when LOG-ONLY JWKS verification is switched on via env.

    Accepts ``log`` / ``log_only`` / ``1`` / ``true`` (case-insensitive). Defaults
    OFF so the check is inert until an operator opts in.
    """
    val = (os.getenv("JWT_SIGNATURE_VERIFICATION", "") or "").strip().lower()
    return val in ("log", "log-only", "log_only", "logonly", "1", "true", "yes", "on")


def _cognito_issuer() -> str:
    region = os.getenv("AWS_REGION") or os.getenv("BEDROCK_REGION") or "us-east-1"
    pool = os.getenv("USER_POOL_ID", "")
    return f"https://cognito-idp.{region}.amazonaws.com/{pool}"


def _jwks_url() -> str:
    return f"{_cognito_issuer()}/.well-known/jwks.json"


def _get_jwks(force: bool = False) -> list:
    """Return the Cognito JWKS key list, cached in-process for _JWKS_TTL seconds.

    Never raises — on any fetch error returns whatever is cached (possibly empty).
    """
    url = _jwks_url()
    now = time.time()
    if (not force and _JWKS_CACHE.get("keys") is not None
            and _JWKS_CACHE.get("url") == url
            and now - _JWKS_CACHE.get("fetched_at", 0.0) < _JWKS_TTL):
        return _JWKS_CACHE["keys"]
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310 # nosemgrep: dynamic-urllib-use-detected - fixed https Cognito JWKS URL from AWS region + pool id (server config), not user input; scheme hardcoded https
            data = json.loads(resp.read().decode("utf-8"))
        keys = data.get("keys", []) if isinstance(data, dict) else []
        _JWKS_CACHE.update({"url": url, "keys": keys, "fetched_at": now})
        return keys
    except Exception as e:  # noqa: BLE001 - telemetry only, never breaks a request
        logger.warning("JWKS fetch failed (%s); log-only verification degraded", e)
        return _JWKS_CACHE.get("keys") or []


def verify_jwt_signature(token: str) -> dict:
    """Verify a JWT's signature/claims against the Cognito JWKS.

    Returns a result dict ``{"verified": bool|None, "reason": str}``. ``verified`` is
    ``None`` when verification could not be attempted (PyJWT/crypto missing, no key
    id, empty pool config) — distinct from ``False`` (a genuine signature/claim
    mismatch). NEVER raises and NEVER rejects: this is telemetry, not a gate. Rejecting
    on ``verified is False`` is deliberately not done here; see the module comment above
    for why.
    """
    if not token:
        return {"verified": None, "reason": "no-token"}
    try:
        import jwt  # PyJWT
        from jwt import PyJWKClient
    except Exception:  # pragma: no cover - dependency guard
        return {"verified": None, "reason": "pyjwt-unavailable"}

    pool = os.getenv("USER_POOL_ID", "")
    if not pool:
        return {"verified": None, "reason": "no-user-pool"}

    try:
        header = jwt.get_unverified_header(token)
    except Exception as e:
        return {"verified": False, "reason": f"bad-header: {e}"}
    if not header.get("kid"):
        return {"verified": None, "reason": "no-kid"}

    try:
        # PyJWKClient fetches+caches the JWKS itself; our _get_jwks warms the cache
        # and gives us a signal if the endpoint is unreachable.
        if not _get_jwks():
            return {"verified": None, "reason": "no-jwks"}
        signing_key = PyJWKClient(_jwks_url()).get_signing_key_from_jwt(token).key
        # audience varies (access vs id token); verify signature + issuer + expiry,
        # not audience, since this only observes.
        jwt.decode(
            token, signing_key, algorithms=["RS256"], issuer=_cognito_issuer(),
            options={"verify_aud": False},
        )
        return {"verified": True, "reason": "ok"}
    except Exception as e:  # noqa: BLE001 - a mismatch is the signal we log
        return {"verified": False, "reason": str(e)}


def verify_and_log(auth_header: str) -> None:
    """LOG-ONLY JWKS verification of the request bearer. Flag-gated no-op by default.

    Verifies the bearer's signature and LOGS a mismatch; it NEVER rejects, so it is
    safe to call on the request path. Missing/other-scheme headers are ignored.
    """
    if not jwt_verification_enabled():
        return
    if not auth_header or not auth_header.startswith("Bearer "):
        return
    try:
        res = verify_jwt_signature(auth_header[7:])
        if res.get("verified") is False:
            logger.warning(
                "audit: in-app JWT signature verification FAILED (log-only, not "
                "rejected): %s", res.get("reason"),
            )
    except Exception as e:  # noqa: BLE001 - never let telemetry break a request
        logger.warning("verify_and_log unexpected error (ignored): %s", e)


# ── Enforcing in-app JWT verification ────────────────────────────────
# The log-only path above proves whether the in-app verifier agrees with the edge; this is
# the gate. Modes (JWT_SIGNATURE_VERIFICATION): "off" (no in-app check), "log" (verify and
# log a mismatch, never reject — the value "on"/"1"/"true" also mean log), and "enforce"
# (verify and REJECT a genuine failure). Default deployed value is "enforce".
#
# Why enforce does not lock out legitimate users: every sanctioned request already passed the
# API Gateway Cognito authorizer, so a real token is valid by the time this sees it. Enforce
# therefore only ever rejects a token that reached the app WITHOUT the edge — the bypass this
# finding is about. To keep a key-endpoint outage from turning into an auth outage, enforce
# fails OPEN only on genuine INFRASTRUCTURE unavailability (PyJWT/crypto missing, no pool
# configured, JWKS unreachable) — the edge already verified those requests — while a forged or
# malformed token (bad/absent signature, no key id, bad issuer/expiry, wrong client binding)
# fails CLOSED.

# Reasons from verify_jwt_signature that mean "could not attempt", not "the token is bad".
# A missing key id or bad header is treated as a genuine failure (a valid Cognito token always
# carries a kid), so those are deliberately NOT in this set.
_JWT_INFRA_REASONS = {"pyjwt-unavailable", "no-user-pool", "no-jwks", "no-token"}


def jwt_verification_mode() -> str:
    """Resolve the verification mode from JWT_SIGNATURE_VERIFICATION: 'off' | 'log' | 'enforce'.
    Unset defaults to 'off' (so the check is inert outside a configured deployment)."""
    val = (os.getenv("JWT_SIGNATURE_VERIFICATION", "") or "").strip().lower()
    if val in ("enforce", "reject", "gate"):
        return "enforce"
    if val in ("log", "log-only", "log_only", "logonly", "on", "1", "true", "yes"):
        return "log"
    return "off"


def _allowed_client_ids() -> set:
    """The Cognito app client ids a token may be bound to (web + M2M), from env. Empty when
    unconfigured — in which case the client-id binding check is skipped (fail-open) rather
    than rejecting, so a missing env var can never lock callers out."""
    ids = set()
    for key in ("WEB_CLIENT_ID", "M2M_CLIENT_ID"):
        v = (os.getenv(key) or "").strip()
        if v:
            ids.add(v)
    return ids


def _token_client_ref(claims: dict) -> str:
    """The client id a token is bound to: `aud` on an id token, `client_id` on an access
    token (Cognito access tokens carry client_id, not aud)."""
    if claims.get("token_use") == "id":
        aud = claims.get("aud")
        if isinstance(aud, list):
            return aud[0] if aud else ""
        return aud or ""
    return claims.get("client_id") or ""


def jwt_rejection_reason(auth_header: str):
    """Decide whether the request bearer must be REJECTED.

    Returns a short reason string when the request should be refused (enforce mode + a
    genuine signature/claim failure), else None (allow). Never raises. In 'log' mode it logs
    a genuine failure and returns None; in 'off' mode it is a no-op. A missing/non-Bearer
    header returns None (missing auth is the route's own concern, not a forgery).
    """
    mode = jwt_verification_mode()
    if mode == "off":
        return None
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    token = auth_header[7:]
    res = verify_jwt_signature(token)  # signature + issuer + expiry (verify_aud=False)
    verified, reason = res.get("verified"), res.get("reason", "")

    # Signature is good — now bind the token to a known app client id.
    if verified is True:
        allowed = _allowed_client_ids()
        ref = _token_client_ref(decode_jwt_unverified(token))
        if allowed and ref and ref not in allowed:
            verified, reason = False, f"client-id {ref} not allowed"
        if verified is True:
            return None  # fully verified — allow

    # Could-not-attempt on genuine infrastructure unavailability: fail OPEN (the edge already
    # verified this request); a JWKS/dependency blip must not become an auth outage.
    if verified is None and reason in _JWT_INFRA_REASONS:
        logger.warning("audit: JWT verification could not be attempted (%s); allowing "
                       "(edge already verified)", reason)
        return None

    # Everything else is a genuine failure (bad/absent signature, no kid, bad header, bad
    # issuer/expiry, wrong client binding).
    if mode == "enforce":
        logger.warning("audit: in-app JWT verification REJECTED the request: %s", reason)
        return reason or "jwt-verification-failed"
    logger.warning("audit: in-app JWT verification FAILED (log-only, not rejected): %s", reason)
    return None
