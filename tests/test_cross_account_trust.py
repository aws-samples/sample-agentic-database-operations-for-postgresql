#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The cross-account trust surface, on both sides of the assumption.

A member account grants the deployment access by creating a cross-account role from the
admin console's instructions (SpaceDetail.jsx renders the trust policy an operator pastes).
There is one rendering of that role now: the product no longer ships a CloudFormation copy.

The trust must be narrow on two axes, and this file pins both:

  * It carries `sts:ExternalId = space_id`, so a role created for one space refuses a
    request made for another (the id is derived server-side from the authorised space
    context, never a request body).
  * It conditions on `aws:PrincipalArn` (roles-only, the agent's account). Naming an
    account as principal admits ANY identity in it, IAM users and federated sessions
    included; the condition narrows that to the agent's own roles, so a non-agent
    identity in the deployment account cannot assume the role even knowing the (non-secret)
    space id.

On the calling side, every assume presents an ExternalId and each caller's `sts:AssumeRole`
is scoped by role name (`arn:aws:iam::*:role/${ResourcePrefix}-*`), not `Resource: '*'`.

Run:  python3 tests/test_cross_account_trust.py   (or via pytest)
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONSOLE = os.path.join(ROOT, "admin-console", "src", "components", "SpaceDetail.jsx")
AGENT_TPL = os.path.join(ROOT, "backend", "template.yaml")
API_TPL = os.path.join(ROOT, "infrastructure", "api.yaml")
MASTER_TPL = os.path.join(ROOT, "infrastructure", "master.yaml")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _console_trust_policy():
    """The trust-policy literal the console hands out (the one carrying sts:AssumeRole)."""
    text = _read(CONSOLE)
    m = re.search(
        r'\{"Version":"2012-10-17","Statement":\[\{"Effect":"Allow"[^\n]*?sts:AssumeRole[^\n]*?\}\]\}',
        text,
    )
    assert m, "could not find the generated trust policy in SpaceDetail.jsx"
    return m.group(0)


def test_trust_policy_pins_to_the_space_with_external_id():
    trust = _console_trust_policy()
    assert "sts:ExternalId" in trust, (
        "the trust policy carries no sts:ExternalId — a role created for one space could "
        "be assumed for another"
    )
    assert "ownerAdminOf(space)" in trust, \
        "the ExternalId must be the id of the admin who owns the space, taken from the space record"


def test_trust_policy_narrows_the_principal_to_roles():
    trust = _console_trust_policy()
    assert "aws:PrincipalArn" in trust and "ArnLike" in trust, (
        "the trust policy has no ArnLike condition on aws:PrincipalArn — it trusts every "
        "identity in the management account, not just the agent's roles"
    )
    assert ":role/" in trust, \
        "the principal condition should be roles-only (arn:aws:iam::<account>:role/*)"


def test_every_caller_scopes_assume_role_by_role_name():
    # The claim docs/ARCHITECTURE.md makes about the calling side.
    agent = _read(AGENT_TPL)
    api = _read(API_TPL)
    unscoped = []
    for name, text in (("backend/template.yaml", agent), ("infrastructure/api.yaml", api)):
        for m in re.finditer(r"sts:AssumeRole", text):
            window = text[m.start():m.start() + 400]
            # The action may be listed with others; find the Resource that follows.
            res = re.search(r"Resource:\s*(.+)", window)
            if not res:
                continue
            value = res.group(1).strip()
            if value == "'*'" or value == "*":
                # Trust-policy statements ("Action: sts:AssumeRole" under
                # AssumeRolePolicyDocument) have no Resource; skip anything that is
                # clearly a trust policy by looking backwards.
                before = text[max(0, m.start() - 400):m.start()]
                if "AssumeRolePolicyDocument" in before or "Principal:" in before:
                    continue
                unscoped.append(f"{name} near offset {m.start()}")
    assert not unscoped, (
        "sts:AssumeRole on Resource: '*' — the runtime could assume any role in any "
        f"account: {unscoped}"
    )


def test_the_scoped_resource_uses_the_resource_prefix():
    agent = _read(AGENT_TPL)
    assert re.search(r"Action: sts:AssumeRole\n\s*Resource: !Sub 'arn:aws:iam::\*:role/\$\{ResourcePrefix\}-\*'", agent), \
        "the runtime's sts:AssumeRole should be scoped to arn:aws:iam::*:role/${ResourcePrefix}-*"


def test_the_deployment_publishes_its_assuming_principals():
    # A prefix guess is unreliable because these roles are CloudFormation-named, so the
    # operator has to be able to read the real ARNs to narrow the trust to exact ARNs.
    assert "AssumeRolePrincipalArns" in _read(AGENT_TPL), \
        "the agent stack should output the roles that assume cross-account"
    assert "ApiTaskRoleArn" in _read(API_TPL), \
        "the API stack should output its task role ARN"
    master = _read(MASTER_TPL)
    assert "AssumeRolePrincipalArns" in master, \
        "master.yaml should surface the combined list for the operator"


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
