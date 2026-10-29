#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The API task role's EC2 network create/delete grants stay tag-gated on ManagedBy.

The deployment-account task role (ECSTaskRole, the AppAccess policy in
infrastructure/api.yaml) builds the agent's cross-account networking: VPC peering,
resource endpoints and the security groups that front them. Those create/delete actions
used to sit on a single unconditioned `Resource: '*'` statement. This mirrors the member
-account role already shipped in SpaceDetail.jsx's secondaryAccountPolicy (pinned by
tests/test_secondary_role_policy.py): every create of a taggable resource is conditioned
on aws:RequestTag/ManagedBy, every delete on aws:ResourceTag/ManagedBy, and ec2:CreateTags
is limited to tag-on-create so the gate cannot be self-granted.

The runtime (backend/tools/aws/network_client.py) stamps ManagedBy in the same create
call for every one of these resources, so the RequestTag condition cannot block live
onboarding. This test is a drift guard: it fails if the statement is ever flattened back
to an unconditioned combined block.

Documented residuals left UNCONDITIONED (and asserted present): the VPC leg of
CreateSecurityGroup (a VPC cannot carry a request tag), CreateRoute/DeleteRoute/
ReplaceRoute (a route is not a taggable resource), AuthorizeSecurityGroupIngress/
RevokeSecurityGroupIngress (contained by the explicit ALB/VPC-Link Deny),
AcceptVpcPeeringConnection and ModifyVpcPeeringConnectionOptions (accept/options side),
and the Describe* reads. The ENI statement (aws:ViaAWSService) is pinned separately by
tests/test_task_role_eni_guard.py.

Reads infrastructure/api.yaml with the CloudFormation tags kept. Hermetic, no AWS.

Run:  python3 tests/test_task_role_network_policy.py   (from the repository root)
"""
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
TAG = {"!Sub": "${ResourcePrefix}-agent"}   # managed_by_tag() rendered in CloudFormation

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


class _TagLoader(yaml.SafeLoader):
    pass


def _tagged(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {"!" + suffix: value}


_TagLoader.add_multi_constructor("!", _tagged)
DOC = yaml.load((ROOT / "infrastructure/api.yaml").read_text(encoding="utf-8"), Loader=_TagLoader)  # nosec B506


def as_list(v):
    return [] if v is None else (v if isinstance(v, list) else [v])


def _task_role_statements():
    """Every Statement governing the ECSTaskRole: its inline AppAccess policy plus the
    ECSTaskNetworkCreate managed policy. The two EC2 create statements live in that managed
    policy because the role's inline policies are at the IAM 10,240-byte aggregate limit; a
    managed policy carries them in a budget of its own."""
    resources = DOC["Resources"]
    role = resources["ECSTaskRole"]["Properties"]
    app = next(p for p in role["Policies"] if p["PolicyName"] == "AppAccess")
    stmts = list(app["PolicyDocument"]["Statement"])
    net = resources.get("ECSTaskNetworkCreate")
    if net:
        stmts.extend(net["Properties"]["PolicyDocument"]["Statement"])
    return stmts


STMTS = _task_role_statements()

# The create statements are only effective if their managed policy is actually attached to
# the role — guard against it being added but left unattached.
_net = DOC["Resources"].get("ECSTaskNetworkCreate", {})
_net_roles = (_net.get("Properties", {}) or {}).get("Roles", []) or []
check(_net.get("Type") == "AWS::IAM::ManagedPolicy"
      and any(isinstance(r, dict) and r.get("!Ref") == "ECSTaskRole" for r in _net_roles),
      "ECSTaskNetworkCreate managed policy is attached to the ECSTaskRole")


def _request_tag(st):
    return (st.get("Condition", {}) or {}).get("StringEquals", {}).get("aws:RequestTag/ManagedBy")


def _resource_tag(st):
    return (st.get("Condition", {}) or {}).get("StringEquals", {}).get("aws:ResourceTag/ManagedBy")


def _stmts_with(action):
    return [st for st in STMTS if action in as_list(st.get("Action"))]


def _all_actions():
    return [a for st in STMTS for a in as_list(st.get("Action")) if isinstance(a, str)]


print("══ the tag value is always !Sub '${ResourcePrefix}-agent', never a literal ══")
for st in STMTS:
    for key in ("aws:RequestTag/ManagedBy", "aws:ResourceTag/ManagedBy"):
        val = (st.get("Condition", {}) or {}).get("StringEquals", {}).get(key)
        if val is not None:
            check(val == TAG, f"{key} on {as_list(st.get('Action'))} is the prefixed tag ({val})")

print("")
print("══ peering/endpoint creates are split: a tag-conditioned taggable leg + an unconditioned non-taggable leg ══")
# CreateVpcPeeringConnection also acts on the requester VPC, and CreateVpcEndpoint on the
# VPC/subnets/SGs — none of which can carry a request tag. So each create needs a
# tag-conditioned leg (the taggable resource it produces) PLUS an unconditioned leg for the
# non-taggable resources it is also authorized against, exactly like CreateSecurityGroup.
for action, taggable in (
    ("ec2:CreateVpcPeeringConnection", "arn:aws:ec2:*:*:vpc-peering-connection/*"),
    ("ec2:CreateVpcEndpoint", "arn:aws:ec2:*:*:vpc-endpoint/*"),
):
    sts = _stmts_with(action)
    tagged = [st for st in sts if _request_tag(st) == TAG]
    unconditioned = [st for st in sts if "Condition" not in st]
    check(len(tagged) == 1, f"{action} has exactly one tag-conditioned leg")
    check(bool(tagged) and taggable in as_list(tagged[0].get("Resource")),
          f"{action} tag-conditioned leg covers {taggable}")
    check(len(unconditioned) >= 1, f"{action} has an unconditioned non-taggable leg")
    for st in unconditioned:
        check("arn:aws:ec2:*:*:vpc/*" in as_list(st.get("Resource")),
              f"{action} unconditioned leg covers the vpc/* resource")

# CreateSecurityGroup is split into a tagged security-group leg and an unconditioned VPC leg.
sg_creates = _stmts_with("ec2:CreateSecurityGroup")
sg_leg = [st for st in sg_creates if st.get("Resource") == "arn:aws:ec2:*:*:security-group/*"]
vpc_leg = [st for st in sg_creates if st.get("Resource") == "arn:aws:ec2:*:*:vpc/*"]
check(len(sg_leg) == 1 and _request_tag(sg_leg[0]) == TAG,
      "ec2:CreateSecurityGroup security-group leg conditioned on aws:RequestTag/ManagedBy")

print("")
print("══ the CreateSecurityGroup VPC leg is present and UNCONDITIONED (documented residual) ══")
check(len(vpc_leg) == 1, "ec2:CreateSecurityGroup has a vpc/* leg")
check(bool(vpc_leg) and "Condition" not in vpc_leg[0],
      "the vpc/* leg carries no Condition (a VPC cannot carry a request tag)")

print("")
print("══ every delete of a taggable network resource is gated on aws:ResourceTag/ManagedBy ══")
for action in ("ec2:DeleteSecurityGroup", "ec2:DeleteVpcEndpoints", "ec2:DeleteVpcPeeringConnection"):
    sts = _stmts_with(action)
    check(len(sts) == 1 and _resource_tag(sts[0]) == TAG,
          f"{action} conditioned on aws:ResourceTag/ManagedBy")

print("")
print("══ ec2:CreateTags cannot be used to self-grant (tag-on-create only) ══")
tag_sts = _stmts_with("ec2:CreateTags")
check(len(tag_sts) == 1, "exactly one statement grants ec2:CreateTags")
if tag_sts:
    st = tag_sts[0]
    cond = st.get("Condition", {}) or {}
    check(_request_tag(st) == TAG, "ec2:CreateTags requires aws:RequestTag/ManagedBy")
    create_action = cond.get("StringLike", {}).get("ec2:CreateAction")
    check(as_list(create_action) == ["CreateSecurityGroup", "CreateVpcPeeringConnection", "CreateVpcEndpoint"],
          f"ec2:CreateTags is pinned to ec2:CreateAction tag-on-create ({create_action})")

print("")
print("══ the documented unconditioned residuals are present and NOT tag-conditioned ══")
RESIDUAL_UNCONDITIONED = [
    "ec2:CreateRoute", "ec2:DeleteRoute", "ec2:ReplaceRoute",
    "ec2:AuthorizeSecurityGroupIngress", "ec2:RevokeSecurityGroupIngress",
    "ec2:AcceptVpcPeeringConnection", "ec2:ModifyVpcPeeringConnectionOptions",
    "ec2:DescribeRouteTables", "ec2:DescribeVpcs", "ec2:DescribeSubnets",
    "ec2:DescribeSecurityGroups", "ec2:DescribeVpcPeeringConnections",
    "ec2:DescribeVpcEndpoints",
]
for action in RESIDUAL_UNCONDITIONED:
    sts = _stmts_with(action)
    check(len(sts) >= 1, f"{action} is present")
    for st in sts:
        check(_request_tag(st) is None and _resource_tag(st) is None,
              f"{action} is not tag-conditioned (documented residual)")

print("")
print("══ no taggable create AND delete share one unconditioned Resource:'*' block ══")
# The regression this guards against: the old flattened block carried both
# ec2:CreateSecurityGroup and ec2:DeleteSecurityGroup on '*' with no Condition.
for st in STMTS:
    acts = set(as_list(st.get("Action")))
    taggable_create = acts & {"ec2:CreateSecurityGroup", "ec2:CreateVpcPeeringConnection",
                              "ec2:CreateVpcEndpoint"}
    taggable_delete = acts & {"ec2:DeleteSecurityGroup", "ec2:DeleteVpcEndpoints",
                              "ec2:DeleteVpcPeeringConnection"}
    if taggable_create or taggable_delete:
        unconditioned = "Condition" not in st
        wildcard = st.get("Resource") == "*"
        check(not (unconditioned and wildcard),
              f"no taggable create/delete sits unconditioned on '*' ({sorted(acts)[:3]}...)")

print("")
print("══ no EC2 service wildcard in this policy ══")
check(not any(a == "ec2:*" or a == "*" for a in _all_actions()),
      "no ec2:* or * action grant")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
