#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""In-app JWT signature enforcement.

Authorization used to trust JWT claims that were verified only at the API Gateway / AgentCore
edge; the app decoded the bearer WITHOUT re-checking the signature. This pins the defense in
depth that closes the gap:

  * app.py runs an http middleware that re-verifies every request's bearer and REJECTS a
    genuine failure (via utils.auth.jwt_rejection_reason);
  * the ECS task role carries an explicit Deny on ec2 ingress authorize/revoke for the two
    groups that make "API Gateway is the only way in" true (the ALB group and the VPC Link
    group), so the SG-widening bypass cannot re-open;
  * the verifier's inputs (USER_POOL_ID, WEB_CLIENT_ID, M2M_CLIENT_ID) and the mode
    (JWT_SIGNATURE_VERIFICATION) reach both the API task and the /invocations runtime;
  * the mode defaults to 'enforce' in every template that declares it, and master.yaml wires
    it to both the API and agent stacks.

Static checks against the source (no AWS, no imports of the app).

Run:  python3 tests/test_jwt_enforcement.py   (from the repository root)
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = (ROOT / "backend/app.py").read_text(encoding="utf-8")
AUTH = (ROOT / "backend/utils/auth.py").read_text(encoding="utf-8")
API = (ROOT / "infrastructure/api.yaml").read_text(encoding="utf-8")
BACKEND = (ROOT / "backend/template.yaml").read_text(encoding="utf-8")
MASTER = (ROOT / "infrastructure/master.yaml").read_text(encoding="utf-8")

problems = 0


def check(cond, msg):
    global problems
    print(("  ok   " if cond else "  \u274c ") + msg)
    if not cond:
        problems += 1


print("── the app enforces JWT signatures on every route ──")
check('@app.middleware("http")' in APP and "_enforce_jwt_signature" in APP,
      "app.py registers an http middleware for in-app JWT enforcement")
check("jwt_rejection_reason(request.headers.get(\"Authorization\"" in APP
      or "jwt_rejection_reason(request.headers.get('Authorization'" in APP,
      "the middleware consults jwt_rejection_reason for the request bearer")
check("status_code=401" in APP,
      "a rejected bearer gets a 401")
# The decision function exists and enforce is a real mode (not just log-only).
check("def jwt_rejection_reason(" in AUTH and "def jwt_verification_mode(" in AUTH,
      "utils.auth exposes jwt_rejection_reason + jwt_verification_mode")
check('== "enforce"' in AUTH and "_JWT_INFRA_REASONS" in AUTH,
      "enforce mode exists and fails open only on the infra-reason set")

print("")
print("── the SG-widening bypass is denied at the IAM layer ──")
# An explicit Deny statement naming the two protected groups for ingress authorize/revoke.
# api.yaml holds more than one Deny (the database alarms carry one too): pick the ec2 one.
denies = [m.group(1) for m in re.finditer(r"Effect:\s*Deny(.*?)(?=\n\s{14}- Effect:|\n\s{14}#|\Z)", API, re.S)]
deny_txt = next((d for d in denies if "ec2:AuthorizeSecurityGroupIngress" in d), "")
check(bool(deny_txt)
      and "ec2:AuthorizeSecurityGroupIngress" in deny_txt
      and "ec2:RevokeSecurityGroupIngress" in deny_txt,
      "api.yaml has an explicit Deny on ec2 ingress authorize/revoke")
check("ALBSecurityGroup.GroupId" in deny_txt and "VpcLinkSecurityGroup.GroupId" in deny_txt,
      "the Deny targets exactly the ALB group and the VPC Link group")

print("")
print("── the verifier's inputs + mode reach both compute paths ──")
for var in ("USER_POOL_ID", "WEB_CLIENT_ID", "M2M_CLIENT_ID", "JWT_SIGNATURE_VERIFICATION"):
    check(f"Name: {var}" in API, f"api.yaml ECS task env carries {var}")
    check(f"{var}:" in BACKEND, f"backend/template.yaml runtime env carries {var}")

print("")
print("── enforce is the default everywhere the mode is declared ──")
# api.yaml + backend/template.yaml + master.yaml each declare JwtSignatureVerification
# with Default 'enforce'.
for name, text in (("api.yaml", API), ("backend/template.yaml", BACKEND), ("master.yaml", MASTER)):
    m = re.search(r"JwtSignatureVerification:\s*\n(?:\s+.*\n){0,6}?\s+Default:\s*'?enforce'?",
                  text)
    check(bool(m), f"{name} declares JwtSignatureVerification Default enforce")
check(MASTER.count("JwtSignatureVerification: !Ref JwtSignatureVerification") >= 2,
      "master.yaml passes the mode to both the API and agent stacks")

print("")
if problems:
    print(f"FAIL — {problems} problem(s)")
    sys.exit(1)
print("PASS")
