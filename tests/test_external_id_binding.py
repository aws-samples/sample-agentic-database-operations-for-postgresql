#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Every cross-account assumption is bound to the space it was made for.

The account binding on a space — which AWS accounts and roles the deployment assumes into
— is set by whoever administers that space, because they are the person who knows which
accounts belong to their entity. That is only safe if naming an account cannot by itself
reach it.

Two things make it safe, and this file pins both.

The role has to exist on the far side. The deployment assumes `<prefix>-<role>` in the
named account, so a binding does nothing until somebody with IAM rights *in that account*
creates the role.

And the assumption carries `sts:ExternalId` set to the id of the space the request was
authorised for. The trust policy the console hands out requires that id, so a role created
for one space refuses a request made for another, whatever account and role name that
space names. The id is derived server-side from the authorised space context and never
read from a request body — if it were, a caller could present another space's id and the
whole property would be gone.

The failure mode this guards against is quiet. Miss the ExternalId on one call site and
that path breaks with an opaque AccessDenied; miss it on the access check and the console
reports a role as validated that the real paths cannot use. So this reads the call sites
and the generated trust policy rather than exercising a live assume.

Run:  python3 tests/test_external_id_binding.py   (or via pytest)
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APP = os.path.join(ROOT, "backend", "app.py")
SESSION_FACTORY = os.path.join(ROOT, "backend", "tools", "aws", "session_factory.py")
DISCOVERY = os.path.join(ROOT, "backend", "tools", "aws", "discovery_client.py")
TIER1 = os.path.join(ROOT, "backend", "tier1_collector.py")
CONSOLE = os.path.join(ROOT, "admin-console", "src", "components", "SpaceDetail.jsx")

ASSUME_SITES = (APP, SESSION_FACTORY, DISCOVERY, TIER1)


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _assume_calls(text):
    return re.findall(r"assume_role\((?:[^()]|\([^()]*\))*\)", text)


def test_every_assume_role_presents_an_external_id():
    missing = []
    total = 0
    for path in ASSUME_SITES:
        text = _read(path)
        for call in _assume_calls(text):
            total += 1
            if "ExternalId" not in call:
                line = text[:text.index(call)].count("\n") + 1
                missing.append(f"{os.path.basename(path)}:{line}")
    assert total >= 4, f"expected at least four assume_role call sites, found {total}"
    assert not missing, (
        "these assume_role calls present no sts:ExternalId, so the roles they target "
        f"will refuse them: {missing}"
    )


def test_the_access_check_presents_one_too():
    """A check that omits it would validate a role the real paths cannot use."""
    text = _read(APP)
    m = re.search(r'assume_role\([^)]*RoleSessionName="access-check"[^)]*\)', text, re.S)
    assert m, "could not find the access-check assume_role in app.py"
    assert "ExternalId" in m.group(0), \
        "the access check must present the same ExternalId the real paths do"


def test_tier1_refuses_rather_than_assuming_without_one():
    """Tier 1 runs unattended, where an opaque AccessDenied is the worst outcome."""
    text = _read(TIER1)
    m = re.search(r"def _reach_session\(.*?\n(?=\n*def )", text, re.S)
    assert m, "could not find _reach_session in tier1_collector.py"
    body = m.group(0)
    assert "owner_admin: str" in body and "ExternalId=owner_admin" in body, \
        "tier1's reach session presents the reach provider's owner admin"
    assert "space_id" not in body, "the ExternalId is the owner admin, never a space id"
    assert "raise ValueError" in body, \
        "tier1 should refuse a cross-account assume with no owner admin, not attempt it"
    call = text.split("session = _reach_session(", 1)[1].split(")\n", 1)[0]
    assert 'owner_admin=record.get("reach_owner_admin", "")' in call, \
        "the call site passes the reach provider's owner admin"
    assert "creds = _get_db_credentials(session, secret_arn)" in text, \
        "the credential is read through that session"


def test_the_external_id_is_never_taken_from_a_request():
    """The whole property rests on this: it comes from the authorised space, not the caller."""
    text = _read(SESSION_FACTORY)
    m = re.search(r"def external_id_for_space\(.*?\n(?=\n*def )", text, re.S)
    assert m, "external_id_for_space missing from session_factory"
    # Docstring stripped: it discusses request bodies precisely to say the value must not
    # come from one, so matching on prose would flag the explanation as the offence.
    body = re.sub(r'""".*?"""', "", m.group(0), flags=re.S)
    for smell in ("request", "payload", "data.", "body"):
        assert smell not in body, (
            f"external_id_for_space references {smell!r} in code; it must derive from "
            "the authorised space context only"
        )
    assert "space_id" in body, "external_id_for_space should derive from the space id"


def test_the_console_hands_out_a_trust_policy_carrying_the_space_id():
    """The operator pastes this, so the id has to be in it rather than typed by hand.

    Typing it would also defeat the control: a space admin could enter another space's id.
    The console is the single rendering of the role now (the CloudFormation copy is gone),
    so it must carry BOTH the space pin and the roles-only principal narrowing.
    """
    text = _read(CONSOLE)
    policies = re.findall(
        r'\{"Version":"2012-10-17","Statement":\[\{"Effect":"Allow"[^\n]*?sts:AssumeRole[^\n]*?\]\}',
        text,
    )
    assert policies, "could not find the generated trust policy in SpaceDetail.jsx"
    for policy in policies:
        assert "sts:ExternalId" in policy, \
            "the generated trust policy does not condition on sts:ExternalId"
        assert "ownerAdminOf(space)" in policy, \
            "the ExternalId must be the id of the admin who owns the space, taken from the space record"
        # Naming the account as principal admits any identity in it; the ArnLike on
        # aws:PrincipalArn narrows the trust to the agent's roles only.
        assert "aws:PrincipalArn" in policy and "ArnLike" in policy, \
            "the trust policy must narrow the principal to roles via aws:PrincipalArn"
        assert ":role/" in policy, "the principal condition should be roles-only"


def test_the_accepted_risk_no_longer_claims_it_is_absent():
    """The acceptance described this as carried; it is enforced, so the entry must not."""
    text = _read(os.path.join(ROOT, "docs", "ARCHITECTURE.md"))
    stale = [
        "sts:ExternalId is deliberately absent",
        "carries no `sts:ExternalId`",
        "carries no sts:ExternalId",
    ]
    found = [s for s in stale if s in text]
    assert not found, f"ARCHITECTURE.md still documents ExternalId as absent: {found}"


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
