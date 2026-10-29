# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Session tokens are bounded, revoked on logout, and refused once access is taken away.

The web Cognito client used to take Cognito's defaults: a 30-day refresh token, and no
server-side revocation on logout. Both SPAs keep their tokens (refresh included) in
localStorage, so a captured refresh token could be exchanged for new tokens for weeks,
and "logging out" only cleared the browser copy. There was also no sign-out when an
admin changed a user's role, so a token minted under the old role kept working until it
expired.

This guards the three fixes, all statically (no AWS, no browser):
  1. the client bounds token lifetimes (refresh via a tunable param, access/id 60 min) and
     enables revocation, and the param is wired through master.yaml;
  2. both SPAs revoke the refresh token via /oauth2/revoke on logout;
  3. every change that takes access away ends the current sign-in through one helper
     (_end_current_sign_in: token cutoff + global sign-out); removal from a space skips the
     sign-out, and adding a user to a space touches nothing.

Run:  python3 tests/test_token_lifecycle.py
"""
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
passed = failed = 0


def check(cond, label):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}")


def read(rel):
    return (REPO / rel).read_text(encoding="utf-8")


print("== the web client bounds token lifetimes and enables revocation ==")
cognito = read("infrastructure/cognito.yaml")
# Scope to the WebClient resource so M2MClient below cannot satisfy these by accident.
web = cognito.split("WebClient:", 1)[1].split("\n  M2MClient:", 1)[0]
check("EnableTokenRevocation: true" in web,
      "WebClient enables token revocation (so /oauth2/revoke + global sign-out work)")
check("RefreshTokenValidity: !Ref RefreshTokenValidityDays" in web,
      "refresh token validity is the tunable RefreshTokenValidityDays param")
check("AccessTokenValidity: 60" in web and "IdTokenValidity: 60" in web,
      "access and id tokens are held to 60 minutes")
check(re.search(r"TokenValidityUnits:\s*\n\s*AccessToken: minutes\s*\n\s*IdToken: minutes\s*\n\s*RefreshToken: days", web) is not None,
      "TokenValidityUnits pins access/id to minutes and refresh to days")
check(re.search(r"RefreshTokenValidityDays:\s*\n\s*Type: Number\s*\n\s*Default: 7", cognito) is not None,
      "cognito.yaml declares RefreshTokenValidityDays (Number, default 7)")

print("")
print("== the refresh-token param is wired through master.yaml ==")
master = read("infrastructure/master.yaml")
check(re.search(r"RefreshTokenValidityDays:\s*\n\s*Type: Number\s*\n\s*Default: 7", master) is not None,
      "master.yaml declares RefreshTokenValidityDays (default 7)")
check("RefreshTokenValidityDays: !Ref RefreshTokenValidityDays" in master,
      "master.yaml passes it to the Cognito nested stack")
# The full config contract (Makefile override + deploy.config.example + configure.py) is
# already enforced by deployment/check-config-defaults.py; not re-asserted here.

print("")
print("== both SPAs revoke the refresh token on logout ==")
for app in ("frontend", "admin-console"):
    src = read(f"{app}/src/hooks/useAuth.js")
    logout = src.split("const logout", 1)[1].split("\n  }", 1)[0]
    check("revokeRefreshToken()" in logout,
          f"{app}: logout calls revokeRefreshToken() before clearing localStorage")
    check("/oauth2/revoke" in src and "keepalive: true" in src,
          f"{app}: revoke posts to /oauth2/revoke (keepalive so navigation cannot cancel it)")

print("")
print("== taking access away ends the current sign-in; adding a space does not ==")
app_py = read("backend/app.py")
# One global sign-out in the whole backend, inside _end_current_sign_in, which also writes
# the per-user "tokens not before" cutoff (behaviour: backend/tests/test_token_not_before.py).
# The call needs the matching IAM grant on the API task role, or it fails as
# AccessDenied and the role change leaves the old tokens valid (seen in a deployment).
api_yaml = (REPO / "infrastructure" / "api.yaml").read_text()
check("cognito-idp:AdminUserGlobalSignOut" in api_yaml,
      "the API task role is granted cognito-idp:AdminUserGlobalSignOut")
total = app_py.count("admin_user_global_sign_out(")
check(total == 1, f"admin_user_global_sign_out is called exactly once in the backend (found {total})")
end = app_py.split("def _end_current_sign_in(", 1)[1].split("\n\n\n", 1)[0]
check("admin_user_global_sign_out(" in end and "token_revocation.revoke_tokens(" in end,
      "_end_current_sign_in writes the token cutoff and signs the user out")


def body(fn):
    return app_py.split(f"def {fn}(", 1)[1].split("\n@app.", 1)[0].split("\ndef ", 1)[0]


for fn in ("_set_role", "delete_cognito_user", "delete_operator"):
    b = body(fn)
    check("_end_current_sign_in(" in b and "sign_out=False" not in b,
          f"{fn} ends the user's current sign-in, sign-out included")
check("_end_current_sign_in(" in body("remove_space_user") and "sign_out=False" in body("remove_space_user"),
      "remove_space_user refuses the old tokens WITHOUT a sign-out (other spaces keep working)")
check("_end_current_sign_in(" not in body("assign_space_user"),
      "assign_space_user (granting a space) does not touch the user's sign-in")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
