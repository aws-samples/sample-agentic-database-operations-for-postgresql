#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The VPC address range is defined once and followed everywhere.

The range 172.20.0.0/16 must come from a single parameter, not be repeated as independent
literals across vpc.yaml, master.yaml and api.yaml. If the security groups carried their
own copies, changing the range would leave them trusting a stale value, producing a
deployment that comes up clean and then cannot reach itself.

Two things are asserted here.

First, that wiring every site to the parameter is a no-op on stacks already deployed:
every default is byte-identical to the literal it stands in for. A changeset proves that
for one config file at one moment; this proves it for every config state, including the
common one where deploy.config predates these keys and all five values fall through to the
defaults.

Second, that the coincidence cannot come back: no CIDR literal survives outside a
parameter default, and each parameter is pattern-constrained so a typo is rejected at
validation rather than discovered at rollback.
"""
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "deployment"))

import yaml  # noqa: E402

from configure import (  # noqa: E402
    NETWORK_SETTINGS,
    subnets_inside_vpc,
    valid_cidr,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The literals that were in the tree before the rewiring. If any of these change, an
# existing stack would see a real resource change, so they are pinned deliberately.
PRE_REWRITE = {
    "VpcCidr": "172.20.0.0/16",
    "PublicSubnet1Cidr": "172.20.1.0/24",
    "PublicSubnet2Cidr": "172.20.2.0/24",
    "PrivateSubnet1Cidr": "172.20.10.0/24",
    "PrivateSubnet2Cidr": "172.20.20.0/24",
}

passed = 0
failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


class _Loader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation's short-form tags (!Ref, !Sub, ...)."""


_Loader.add_multi_constructor("!", lambda loader, suffix, node: None)


def parameters(rel):
    doc = yaml.load((ROOT / rel).read_text(encoding="utf-8"),  # nosec B506 - _Loader subclasses SafeLoader, mapping CloudFormation tags to None
                    Loader=_Loader)
    return doc.get("Parameters") or {}


master = parameters("infrastructure/master.yaml")
vpc = parameters("infrastructure/vpc.yaml")
api = parameters("infrastructure/api.yaml")

print("══ the defaults match the literals they replaced, so existing stacks see no change ══")
for cfn, want in PRE_REWRITE.items():
    check(str(master.get(cfn, {}).get("Default", "")) == want,
          f"master.yaml {cfn} still defaults to {want}")
    check(str(vpc.get(cfn, {}).get("Default", "")) == want,
          f"vpc.yaml {cfn} still defaults to {want}")
check(str(api.get("AgentVpcCidr", {}).get("Default", "")) == PRE_REWRITE["VpcCidr"],
      "api.yaml AgentVpcCidr still defaults to the VPC range")

print("")
print("══ master.yaml hands all five down to the VPC stack ══")
master_text = (ROOT / "infrastructure/master.yaml").read_text(encoding="utf-8")
vpc_block = re.search(r"^  VPCStack:.*?(?=^  \w|^\w)", master_text, re.S | re.M)
check(vpc_block is not None, "VPCStack block is findable")
if vpc_block:
    body = vpc_block.group(0)
    for cfn in PRE_REWRITE:
        check(f"{cfn}: !Ref {cfn}" in body, f"VPCStack receives {cfn}")

print("")
print("══ the security groups follow the parameter instead of a literal ══")
api_text = (ROOT / "infrastructure/api.yaml").read_text(encoding="utf-8")
# Exactly one CIDR-based ingress rule is expected: ECS to the interface VPC endpoints on
# 443. The ALB's ingress is deliberately NOT a CIDR rule — see the next block. If a future
# change reintroduces a CIDR rule here, this count catches it and the ALB assertions below
# explain why that matters.
check(api_text.count("CidrIp: !Ref AgentVpcCidr") == 1,
      "the remaining CIDR ingress rule (ECS to VPC endpoints) references AgentVpcCidr")
check("CidrIp: 172.20" not in api_text, "no literal CidrIp survives")
check("AgentVpcCidr: !Ref VpcCidr" in master_text,
      "master.yaml passes the VPC range to the API stack by reference")

print("")
print("══ the ALB admits only the API Gateway VPC Link ══")
# Why this is a CIDR test and not just an SG test: the backend's in-app group checks
# (is_admin and friends in backend/utils/auth.py) read JWT claims WITHOUT verifying the
# signature. The API Gateway Cognito authorizer is what verifies it. That delegation is
# only sound while API Gateway is the sole route to the ALB, so widening this ingress
# back to the VPC CIDR would let any VPC workload — the Batch scan container included —
# present unverified admin claims — see docs/ARCHITECTURE.md#accepted-threats.
check("  VpcLinkSecurityGroup:" in api_text,
      "a dedicated VpcLinkSecurityGroup exists")
alb_sg = re.search(r"^  ALBSecurityGroup:.*?(?=^  \w)", api_text, re.S | re.M)
check(alb_sg is not None, "ALBSecurityGroup block is findable")
if alb_sg:
    alb_body = alb_sg.group(0)
    check("SourceSecurityGroupId: !Ref VpcLinkSecurityGroup" in alb_body,
          "ALB ingress is sourced from the VPC Link security group")
    check("CidrIp: !Ref AgentVpcCidr" not in alb_body,
          "ALB ingress no longer admits the whole VPC CIDR")
vpclink = re.search(r"^  VpcLink:.*?(?=^  \w)", api_text, re.S | re.M)
check(vpclink is not None, "VpcLink block is findable")
if vpclink:
    check("- !Ref VpcLinkSecurityGroup" in vpclink.group(0),
          "the VPC Link uses the dedicated group, not the shared private one")
check("SecurityGroupIds: !Ref SecurityGroupIds" not in api_text,
      "the VPC Link no longer takes the shared private security group parameter")

print("")
print("══ no CIDR literal survives outside a parameter default ══")
for rel in ("infrastructure/master.yaml", "infrastructure/vpc.yaml", "infrastructure/api.yaml"):
    stray = [
        (n, line.strip())
        for n, line in enumerate((ROOT / rel).read_text(encoding="utf-8").splitlines(), 1)
        if "172.20." in line and not line.strip().startswith("Default:")
    ]
    check(not stray, f"{rel}: every occurrence is a parameter default"
                     + ("" if not stray else f" (stray: {stray})"))

print("")
print("══ deploy.config.example agrees with the templates ══")
example = (ROOT / "deploy.config.example").read_text(encoding="utf-8")
for key, spec in NETWORK_SETTINGS.items():
    m = re.search(rf"^{key}\s*=\s*(\S+)\s*$", example, re.M)
    check(m is not None and m.group(1) == spec["default"],
          f"{key} = {spec['default']}")

print("")
print("══ every CIDR is pattern-constrained, so a typo fails validation not rollback ══")
for cfn in PRE_REWRITE:
    check("AllowedPattern" in master.get(cfn, {}), f"master.yaml {cfn}")
    check("AllowedPattern" in vpc.get(cfn, {}), f"vpc.yaml {cfn}")
check("AllowedPattern" in api.get("AgentVpcCidr", {}), "api.yaml AgentVpcCidr")

print("")
print("══ the AllowedPattern accepts the defaults it is meant to accept ══")
for cfn, want in PRE_REWRITE.items():
    pat = master[cfn]["AllowedPattern"]
    check(re.match(pat, want) is not None, f"{cfn} pattern accepts {want}")

print("")
print("══ valid_cidr rejects what CloudFormation would reject later ══")
vpc_check = valid_cidr(16, 24)
check(vpc_check("172.20.0.0/16") == "", "a well-formed private /16 is accepted")
check(vpc_check("10.0.0.0/8") != "", "a /8 is too wide for the allowed range")
check(vpc_check("172.20.0.0/28") != "", "a /28 is too narrow for a VPC here")
check(vpc_check("172.20.0.1/16") != "", "a host bit set is rejected, not silently masked")
check(vpc_check("8.8.8.0/24") != "", "a public range is rejected")
check(vpc_check("not-a-cidr") != "", "garbage is rejected")
check(vpc_check("172.20.0.0") != "", "a bare address with no prefix is rejected")
subnet_check = valid_cidr(16, 28)
check(subnet_check("172.20.1.0/24") == "", "a /24 subnet is accepted")
check(subnet_check("172.20.1.0/28") == "", "a /28 subnet is accepted")

print("")
print("══ a subnet outside the VPC range is caught before anything is created ══")
good = {key: spec["default"] for key, spec in NETWORK_SETTINGS.items()}
check(subnets_inside_vpc(good) == [], "the shipped defaults are self-consistent")

moved_vpc = dict(good, VPC_CIDR="10.50.0.0/16")
check(len(subnets_inside_vpc(moved_vpc)) == 4,
      "moving the VPC and leaving the subnets behind reports all four")

one_off = dict(good, PRIVATE_SUBNET2_CIDR="10.99.0.0/24")
problems = subnets_inside_vpc(one_off)
check(len(problems) == 1 and "PRIVATE_SUBNET2_CIDR" in problems[0],
      "a single stray subnet is named specifically")

consistent = {
    "VPC_CIDR": "10.50.0.0/16",
    "PUBLIC_SUBNET1_CIDR": "10.50.1.0/24",
    "PUBLIC_SUBNET2_CIDR": "10.50.2.0/24",
    "PRIVATE_SUBNET1_CIDR": "10.50.10.0/24",
    "PRIVATE_SUBNET2_CIDR": "10.50.20.0/24",
}
check(subnets_inside_vpc(consistent) == [], "a wholly relocated network is accepted")
check(subnets_inside_vpc({}) == [], "an empty set reports nothing rather than crashing")
check(subnets_inside_vpc({"VPC_CIDR": "garbage"}) == [],
      "an unparseable VPC defers to the field validator instead of raising")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
