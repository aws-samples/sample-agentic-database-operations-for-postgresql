#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The permission policy the console hands out for a member-account role stays least-privilege.

The admin console renders the cross-account role's permission set as a JSON block an
operator pastes onto a role they create by hand (`secondaryAccountPolicy` in
SpaceDetail.jsx). This is the single rendering of that role: the product no longer ships a
CloudFormation copy, so the console block is the one place the permissions live and the one
place that can drift wider.

This pins the properties that keep it safe: every mutating action is conditioned on the
`ManagedBy` tag (so the role can only touch resources this product created), `ec2:CreateTags`
is limited to tag-on-create so the tag cannot be stamped onto a pre-existing resource to
unlock the mutating statements, the network-interface actions VPC Lattice needs are allowed
only on calls an AWS service makes for the role (`aws:ViaAWSService`), and no statement
grants a service-level wildcard.

Note GET /api/agent-role-policy is a DIFFERENT, narrower policy — the per-space access role
the application provisions itself, which carries no network permissions.

Run:  python3 tests/test_secondary_role_policy.py   (or via pytest)
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONSOLE = os.path.join(ROOT, "admin-console", "src", "components", "SpaceDetail.jsx")

# Statements that act on resources this product does not create and cannot tag. Each is
# documented inline in the console block. Adding to this list should be a conscious decision.
ALLOWED_UNCONDITIONED = {
    "RDSDiscovery", "SecretsAccess", "SecretsAccessScoped",
    "CloudWatchMetrics", "PerformanceInsights", "CloudTrail", "CloudWatchLogs",
    "NetworkDiscovery", "ResourceExplorer",
    # The documented residuals:
    "NetworkRouteWrites",          # customer's own route tables
    "NetworkSecurityGroupRules",   # the database's own security groups
    "NetworkPeeringAccept",        # peering is tagged in the agent account
    # The VPC leg of ec2:CreateSecurityGroup. The action authorises both the new
    # security group and the VPC it lands in; a VPC cannot carry a request tag, so this
    # leg cannot be conditioned on ManagedBy. It is inert without the tagged
    # security-group leg (NetworkResourceCreate), so it does not widen what can be created.
    "NetworkSecurityGroupCreateInVpc",
    # LatticeGatewayNetworkInterfaces is NOT here: it carries aws:ViaAWSService (see
    # test_lattice_eni_statement_requires_via_aws_service). The ENIs carry no request tag
    # of ours, so ManagedBy could not condition it; the service condition does instead.
}

MUTATING = re.compile(
    r":(Create|Delete|Modify|Attach|Detach|Authorize|Revoke|Accept|Replace|Register|"
    r"Deregister|Put|Set|Add|Update)"
)


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _console_policy():
    """The policy object returned by secondaryAccountPolicy in SpaceDetail.jsx."""
    text = _read(CONSOLE)
    # The function takes the prefix and the member account id (the secret read is pinned
    # to the account the role lives in); only the returned object is parsed here.
    m = re.search(r"function secondaryAccountPolicy\(prefix(?:, [^)]*)?\) \{\n  return (\{.*?\n  \})\n\}",
                  text, re.S)
    assert m, "could not locate secondaryAccountPolicy's returned object"
    raw = m.group(1)
    # The literal uses template placeholders for the prefix; neutralise them so it parses.
    raw = re.sub(r"`[^`]*`", '"__PREFIX__"', raw)
    return json.loads(raw)


def _console_statements():
    """{Sid: (frozenset(actions), has_condition)} from the JSON literal in SpaceDetail.jsx."""
    out = {}
    for st in _console_policy()["Statement"]:
        actions = st["Action"]
        if isinstance(actions, str):
            actions = [actions]
        out[st["Sid"]] = (frozenset(actions), "Condition" in st)
    return out


def test_mutating_statements_are_conditioned():
    for sid, (actions, conditioned) in sorted(_console_statements().items()):
        if sid in ALLOWED_UNCONDITIONED:
            continue
        mutating = sorted(a for a in actions if MUTATING.search(a))
        if mutating:
            assert conditioned, (
                f"statement {sid} mutates without a condition: {mutating}. "
                "Either condition it on the ManagedBy tag or add it to "
                "ALLOWED_UNCONDITIONED with a reason."
            )


def test_lattice_eni_statement_requires_via_aws_service():
    # Tested in a dev account: VPC Lattice's service-linked role creates the gateway's and
    # the endpoint's interfaces; the role calling the ENI APIs directly is denied under
    # this condition. Pinned exactly, so it cannot loosen or drift to another key.
    st = next(s for s in _console_policy()["Statement"] if s["Sid"] == "LatticeGatewayNetworkInterfaces")
    assert st.get("Condition") == {"Bool": {"aws:ViaAWSService": "true"}}, st.get("Condition")
    assert sorted(st["Action"]) == sorted([
        "ec2:CreateNetworkInterface", "ec2:CreateNetworkInterfacePermission", "ec2:DeleteNetworkInterface",
        "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses", "ec2:ModifyNetworkInterfaceAttribute"])


def test_lattice_service_linked_role_is_creatable_and_only_that_one():
    # On the first Lattice resource in an account, VPC Lattice creates
    # AWSServiceRoleForVpcLattice with the caller's permissions (CloudTrail in the dev
    # test: CreateServiceLinkedRole by the caller, invokedBy vpc-lattice.amazonaws.com).
    # Granted on the member role and the task role, pinned to that one SLR.
    st = next(s for s in _console_policy()["Statement"] if s["Sid"] == "LatticeServiceLinkedRole")
    assert st["Action"] == "iam:CreateServiceLinkedRole"
    assert st["Resource"] == "arn:aws:iam::*:role/aws-service-role/vpc-lattice.amazonaws.com/AWSServiceRoleForVpcLattice"
    assert st["Condition"] == {"StringEquals": {"iam:AWSServiceName": "vpc-lattice.amazonaws.com"}}
    api = _read(os.path.join(ROOT, "infrastructure", "api.yaml"))
    m = re.search(r"- iam:CreateServiceLinkedRole\n\s+Resource:\n\s+- !Sub '([^']*vpc-lattice[^']*)'\n"
                  r"\s+Condition:\n\s+StringEquals:\n\s+iam:AWSServiceName: (\S+)", api)
    assert m, "task role grants iam:CreateServiceLinkedRole for VPC Lattice"
    assert m.group(1) == ("arn:aws:iam::${AWS::AccountId}:role/aws-service-role/"
                          "vpc-lattice.amazonaws.com/AWSServiceRoleForVpcLattice")
    assert m.group(2) == "vpc-lattice.amazonaws.com"


def test_the_managed_by_tag_is_the_condition_key():
    text = _read(CONSOLE)
    assert "aws:RequestTag/ManagedBy" in text, "creates should require the tag"
    assert "aws:ResourceTag/ManagedBy" in text, "mutations should require the tag"


def test_create_tags_cannot_be_used_to_self_grant():
    # Without a CreateAction condition, the role could stamp ManagedBy onto any
    # pre-existing customer resource and then mutate it under the ResourceTag statement.
    # Both tagging paths must be pinned to tag-on-create: ec2:CreateTags (EC2 leg) and
    # vpc-lattice:TagResource (resource gateways and configurations). RAM has no
    # standalone tagging grant at all: the share is tagged by CreateResourceShare itself.
    text = _read(CONSOLE)
    stmts = _console_statements()
    assert "ec2:CreateAction" in text, (
        "ec2:CreateTags must be limited to tag-on-create via ec2:CreateAction, "
        "or the ManagedBy gate is bypassable"
    )
    assert "vpc-lattice:CreateAction" in text, (
        "vpc-lattice:TagResource must be limited to tag-on-create via "
        "vpc-lattice:CreateAction, or the ManagedBy gate is bypassable on resource "
        "gateways and configurations"
    )
    assert not any("ram:TagResource" in actions for actions, _ in stmts.values()), (
        "ram:TagResource has no tag-on-create condition key; granting it lets the role "
        "stamp ManagedBy onto a customer's share and delete it"
    )
    assert "elasticloadbalancing:" not in text, (
        "the PrivateLink fallback is gone; no load-balancer permission should remain"
    )


def test_resource_share_is_pinned_to_lattice_configurations():
    # The share grants another account access to whatever it names. Pinning the resource
    # type keeps the role from sharing anything but a Lattice resource configuration.
    text = _read(CONSOLE)
    assert '"ram:RequestedResourceType": "vpc-lattice:ResourceConfiguration"' in text


def test_global_database_discovery_is_allowed_everywhere_discovery_runs():
    # Discovery marks Aurora Global Database members (describe_global_clusters) so they
    # can be listed as not supported. Every role discovery runs under needs the action:
    # the member role (console), the task role and the agent-role boundary (api.yaml),
    # the per-space access role (app.py) and the backend's discovery role (template.yaml).
    stmts = _console_statements()
    assert "rds:DescribeGlobalClusters" in stmts["RDSDiscovery"][0], "console member policy"
    api = _read(os.path.join(ROOT, "infrastructure", "api.yaml"))
    assert api.count("- rds:DescribeGlobalClusters") == 2, "task role and AgentRoleBoundary in api.yaml"
    assert '"rds:DescribeGlobalClusters"' in _read(os.path.join(ROOT, "backend", "app.py")), \
        "per-space access role policy"
    assert "- rds:DescribeGlobalClusters" in _read(os.path.join(ROOT, "backend", "template.yaml")), \
        "backend DiscoveryAccess"


def test_share_association_is_readable_for_the_share_confirmation():
    # Configure confirms the provider side reports the deployment account ASSOCIATED
    # before it builds the endpoint: read-only, on the member role and the task role.
    assert "ram:GetResourceShareAssociations" in _console_statements()["NetworkDiscovery"][0]
    assert "- ram:GetResourceShareAssociations" in _read(os.path.join(ROOT, "infrastructure", "api.yaml"))


# The policy text each MEMBER_POLICY_VERSION stands for (sha256 of the parsed policy,
# sorted keys, placeholders neutralised). Changing secondaryAccountPolicy changes the hash:
# bump MEMBER_POLICY_VERSION in SpaceDetail.jsx AND backend/app.py, and add the new
# version's hash here. Older versions stay listed so a version is never reused.
POLICY_VERSION_HASHES = {
    "1": "a695bfc1a06fc85ebaa46558426df70afd746bbb2f267cb072c19629abeab3bc",
    "2": "3814515cf508a4fe8526ad87aecf0366bed1bc12d81a24bfe993ed1a3a0cbdbe",
}


def _policy_hash():
    import hashlib
    return hashlib.sha256(json.dumps(_console_policy(), sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _js_const(name):
    m = re.search(rf"^const {name} = '([^']*)'", _read(CONSOLE), re.M)
    assert m, f"{name} not found in SpaceDetail.jsx"
    return m.group(1)


def test_policy_change_needs_a_version_bump():
    version = _js_const("MEMBER_POLICY_VERSION")
    assert version in POLICY_VERSION_HASHES, (
        f"MEMBER_POLICY_VERSION {version} has no pinned hash; add "
        f"{version!r}: {_policy_hash()!r} to POLICY_VERSION_HASHES")
    assert POLICY_VERSION_HASHES[version] == _policy_hash(), (
        f"secondaryAccountPolicy changed but MEMBER_POLICY_VERSION is still {version}. "
        "Bump it in SpaceDetail.jsx and backend/app.py, and pin the new hash "
        f"{_policy_hash()!r} here.")
    assert len(set(POLICY_VERSION_HASHES.values())) == len(POLICY_VERSION_HASHES), \
        "two versions pin the same policy text"


def test_console_and_server_agree_on_the_version():
    app = _read(os.path.join(ROOT, "backend", "app.py"))
    m = re.search(r'^MEMBER_POLICY_VERSION = "([^"]*)"', app, re.M)
    assert m, "MEMBER_POLICY_VERSION not found in backend/app.py"
    assert m.group(1) == _js_const("MEMBER_POLICY_VERSION"), \
        "the account check compares the role's tag with the server's value; it must match the console's"
    assert _js_const("POLICY_VERSION_TAG") == "DBOpsPolicyVersion"
    assert 'MEMBER_POLICY_VERSION_TAG = "DBOpsPolicyVersion"' in app


def test_commands_tag_the_role_with_the_version_after_the_policy():
    text = _read(CONSOLE)
    body = text.split("function memberRolePolicyUpdateCommand(", 1)[1].split("\n}\n", 1)[0]
    assert "put-role-policy" in body and "tag-role" in body
    assert body.index("put-role-policy") < body.index("&& aws iam tag-role"), \
        "the version tag is written only once the policy is applied"
    assert "Key=${POLICY_VERSION_TAG},Value=${MEMBER_POLICY_VERSION}" in body
    create = text.split("function memberRoleCliCommand(", 1)[1].split("\n}\n", 1)[0]
    assert "memberRolePolicyUpdateCommand(" in create, "the create command ends with the same update"


def test_role_reads_only_agent_role_tags():
    st = next(s for s in _console_policy()["Statement"] if s["Sid"] == "ReadOwnRoleTags")
    assert st["Action"] == "iam:ListRoleTags"
    text = _read(CONSOLE)
    assert '"Resource": `arn:aws:iam::*:role/${prefix}-agent-*`' in text
    assert not any(a.startswith("iam:") and a not in ("iam:ListRoleTags", "iam:CreateServiceLinkedRole")
                   for actions, _ in _console_statements().values() for a in actions)


def test_no_statement_grants_a_service_wildcard():
    for sid, (actions, _) in _console_statements().items():
        for a in actions:
            assert not a.endswith(":*"), f"{sid} grants a service wildcard: {a}"


def test_task_role_lattice_ram_mutations_are_tag_gated():
    # The API task role's mutating Lattice/RAM actions live in the ECSTaskNetworkCreate
    # managed policy, tag-gated the same way the member policy is, so the role can only
    # create, delete, tag or share resources this product made.
    api = _read(os.path.join(ROOT, "infrastructure", "api.yaml"))
    inline, sep, managed = api.partition("ECSTaskNetworkCreate:")
    assert sep, "ECSTaskNetworkCreate managed policy not found"
    # No standalone RAM tagging grant anywhere (no tag-on-create key; it would let the role
    # stamp ManagedBy onto a customer share and delete it). The share is tagged on create.
    assert "- ram:TagResource" not in api, "ram:TagResource must not be granted to the task role"
    for action in ("vpc-lattice:CreateResourceGateway", "vpc-lattice:DeleteResourceGateway",
                   "vpc-lattice:CreateResourceConfiguration", "vpc-lattice:DeleteResourceConfiguration",
                   "ram:CreateResourceShare", "ram:DeleteResourceShare", "vpc-lattice:TagResource"):
        assert f"- {action}" not in inline, f"{action} must not sit unconditioned in the inline AppAccess policy"
        assert f"- {action}" in managed, f"{action} should live in the tag-gated managed policy"
    assert "aws:RequestTag/ManagedBy" in managed and "aws:ResourceTag/ManagedBy" in managed, \
        "creates gate on RequestTag, deletes on ResourceTag"
    assert "ram:RequestedResourceType: vpc-lattice:ResourceConfiguration" in managed, \
        "the RAM share create must be pinned to a Lattice resource configuration"
    assert "vpc-lattice:CreateAction" in managed, \
        "vpc-lattice:TagResource must be pinned to tag-on-create"


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
