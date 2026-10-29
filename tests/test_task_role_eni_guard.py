#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The task role cannot touch the network interfaces in the agent's own subnets.

The deployment-account task role is granted network-interface actions on '*' because
VPC Lattice creates a resource gateway's interfaces in a DATABASE VPC with the caller's
permissions. Nothing of ours deletes, modifies or unassigns addresses on an interface in
the agent's private subnets, and those interfaces are the ALB, the interface VPC
endpoints and the tasks themselves. An explicit Deny pins that:

  * Deny ec2:DeleteNetworkInterface, ec2:ModifyNetworkInterfaceAttribute and
    ec2:UnassignPrivateIpAddresses (exactly those three);
  * on network-interface/* in this Region and account;
  * when ec2:Subnet is one of the agent's private subnets, built from the SubnetIds
    parameter;
  * and aws:ViaAWSService is false, so the Deny hits the role's own direct calls and
    never an AWS service acting for it (resource-endpoint interfaces added and removed
    by CreateVpcEndpoint / DeleteVpcEndpoints in those subnets).

And the code never calls the ENI APIs itself.

Also pins how the ENI Allow is narrowed (see ENI_ALLOW_CONDITIONED below and the dev
experiment recorded for it). Text checks against infrastructure/api.yaml, no PyYAML.

Run:  python3 tests/test_task_role_eni_guard.py   (from the repository root)
"""
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
API = (ROOT / "infrastructure/api.yaml").read_text(encoding="utf-8")

# Whether the dev-sandbox experiment showed aws:ViaAWSService=true works for the
# resource gateway's interfaces (see docs/ARCHITECTURE.md, member-role paragraph).
ENI_ALLOW_CONDITIONED = True   # dev experiment, 2026-10-05: ENIs made by AWSServiceRoleForVpcLattice; direct calls denied

ENI_MUTATIONS = [
    "ec2:CreateNetworkInterface", "ec2:CreateNetworkInterfacePermission", "ec2:DeleteNetworkInterface",
    "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses", "ec2:ModifyNetworkInterfaceAttribute",
]

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def statements(text):
    """Split the template's policy statements: each starts at '- Effect:' and runs to the
    next '- Effect:' at the same indent (or a shallower line)."""
    out = []
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s*)- Effect: (Allow|Deny)\s*$", ln)
        if not m:
            continue
        indent = len(m.group(1))
        body = [ln]
        for nxt in lines[i + 1:]:
            if nxt.strip() and not nxt.strip().startswith("#"):
                cur = len(nxt) - len(nxt.lstrip())
                if cur < indent or (cur == indent and nxt.lstrip().startswith("- ")):
                    break
            body.append(nxt)
        out.append((m.group(2), "\n".join(body)))
    return out


def actions(stmt):
    return re.findall(r"^\s*- ((?:ec2|vpc-lattice|ram|rds|iam):[A-Za-z*]+)\s*$", stmt, re.M)


print("══ the task role denies its own ENI changes in the agent subnets ══")
denies = [s for eff, s in statements(API) if eff == "Deny" and "ec2:DeleteNetworkInterface" in s]
check(len(denies) == 1, "exactly one Deny names ec2:DeleteNetworkInterface")
deny = denies[0] if denies else ""
check(sorted(actions(deny)) == sorted(["ec2:DeleteNetworkInterface", "ec2:ModifyNetworkInterfaceAttribute",
                                       "ec2:UnassignPrivateIpAddresses"]),
      f"it denies exactly delete / modify / unassign ({actions(deny)})")
check("Resource: !Sub 'arn:aws:ec2:${AWS::Region}:${AWS::AccountId}:network-interface/*'" in deny,
      "on network interfaces in this Region and account")
check(re.search(r"ec2:Subnet: !Split \[',', !Join \['', \['arn:aws:ec2:\*:\*:subnet/', "
                r"!Join \[',arn:aws:ec2:\*:\*:subnet/', !Ref SubnetIds\]\]\]\]", deny) is not None
      and "ArnLike:" in deny,
      "ec2:Subnet matches the agent's private subnets, built from the SubnetIds parameter")
check(re.search(r"Bool:\s*\n\s*aws:ViaAWSService: 'false'", deny) is not None,
      "only the role's direct calls: aws:ViaAWSService is false")
check("SubnetIds:\n    Type: List<AWS::EC2::Subnet::Id>" in API, "SubnetIds is the private-subnet list parameter")

print("")
print("══ the code never calls the ENI APIs itself ══")
grep = subprocess.run(
    ["git", "grep", "-nE", r"(create|delete|modify)_network_interface|assign_private_ip|unassign_private_ip",
     "--", "backend", "batch"], cwd=ROOT, capture_output=True, text=True)
check(grep.returncode == 1 and not grep.stdout.strip(),
      f"no ENI create/delete/modify/assign call in backend/ or batch/ {grep.stdout.strip()[:200]}")

print("")
print("══ the ENI Allow ══")
allows = [s for eff, s in statements(API) if eff == "Allow" and "- ec2:CreateNetworkInterface\n" in s]
check(len(allows) == 1, "exactly one Allow grants ec2:CreateNetworkInterface")
allow = allows[0] if allows else ""
check(set(ENI_MUTATIONS) <= set(actions(allow)), "it carries the six ENI mutation actions")
if ENI_ALLOW_CONDITIONED:
    check(re.search(r"Condition:\s*\n\s*Bool:\s*\n\s*aws:ViaAWSService: 'true'", allow) is not None,
          "the ENI mutations are allowed only when an AWS service calls for the role")
    check(sorted(actions(allow)) == sorted(ENI_MUTATIONS), "and that Allow holds nothing else")
    check("ec2:DescribeNetworkInterfaces" not in allow, "DescribeNetworkInterfaces stays with the Lattice Allow")
else:
    check("aws:ViaAWSService" not in allow,
          "the ENI mutations stay unconditioned (documented residual: the condition was tested and does not hold)")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
