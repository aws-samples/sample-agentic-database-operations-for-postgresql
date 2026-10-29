#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tier 2 scan task network isolation.

The Fargate scan task holds the monitored database's password (PGPASSWORD) and the
cross-account role. It used to share the private-workload security group (all traffic to
0.0.0.0/0) with the API task and the agent runtime, and requested a public IP. This pins:

  * a DEDICATED scan security group whose egress is limited to 443 (AWS APIs), 5432
    (Postgres over the per-DB peering/PrivateLink path) and 53 (DNS) — no all-traffic rule;
  * the Batch compute environment uses that group, not the shared one;
  * the job definition sets AssignPublicIp: DISABLED;
  * no metrics-store cluster exists any more: the scan writes its result to DynamoDB and
    CloudWatch over 443, so nothing in the account accepts 5432 from the scan group;
  * master.yaml wires the group to the agent stack.

Static checks against the templates (no AWS).

Run:  python3 tests/test_scan_egress_isolation.py   (from the repository root)
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VPC = (ROOT / "infrastructure/vpc.yaml").read_text(encoding="utf-8")
MASTER = (ROOT / "infrastructure/master.yaml").read_text(encoding="utf-8")
BACKEND = (ROOT / "backend/template.yaml").read_text(encoding="utf-8")

problems = 0


def check(cond, msg):
    global problems
    print(("  ok   " if cond else "  ❌ ") + msg)
    if not cond:
        problems += 1


def _block(text, header):
    """Return the indented YAML block under a `Header:` line (until the next same-or-less
    indented key)."""
    m = re.search(rf"^(\s*){re.escape(header)}:\s*$", text, re.M)
    if not m:
        return ""
    indent = len(m.group(1))
    lines = text[m.end():].splitlines()
    out = []
    for ln in lines:
        if ln.strip() and (len(ln) - len(ln.lstrip())) <= indent:
            break
        out.append(ln)
    return "\n".join(out)


print("── a dedicated, egress-scoped scan security group exists ──")
sg = _block(VPC, "ScanSecurityGroup")
check(bool(sg), "vpc.yaml declares ScanSecurityGroup")
check("FromPort: 443" in sg and "FromPort: 5432" in sg and "FromPort: 53" in sg,
      "its egress covers 443 (AWS), 5432 (Postgres) and 53 (DNS)")
check("IpProtocol: -1" not in sg,
      "it has NO all-traffic (-1) egress rule")
check("SecurityGroupIngress" not in sg,
      "it has no ingress (the scan only makes outbound connections)")
check("ScanSecurityGroupId" in VPC and "ScanSG" in VPC,
      "the group id is exported")
print("")
print("── a database on another port: DbPortRange adds one conditional rule ──")
check("HasCustomDbPortRange: !Not [!Equals [!Ref DbPortRange, '5432-5432']]" in VPC,
      "the extra rule exists only when DbPortRange is not the default")
check("- !If\n          - HasCustomDbPortRange" in sg
      and "FromPort: !Select [0, !Split ['-', !Ref DbPortRange]]" in sg
      and "ToPort: !Select [1, !Split ['-', !Ref DbPortRange]]" in sg
      and "- !Ref AWS::NoValue" in sg,
      "it opens exactly the from-to range of DbPortRange, else nothing")
check(sg.count("FromPort: 5432") == 1 and "ToPort: 5432" in sg,
      "5432 stays open unconditionally (the metrics store needs it)")
check("DbPortRange:\n    Type: String\n    Default: '5432-5432'" in VPC,
      "vpc.yaml declares DbPortRange, default 5432-5432")

print("")
print("── the Batch task uses the scan group and no public IP ──")
ce = _block(BACKEND, "BatchComputeEnvironment")
check("ScanSecurityGroupId" in ce and "!Ref SecurityGroupIds" not in ce,
      "the compute environment uses the dedicated scan group, not the shared one")
check("AssignPublicIp: DISABLED" in BACKEND and "AssignPublicIp: ENABLED" not in BACKEND,
      "the job definition disables the public IP")
check("ScanSecurityGroupId:" in BACKEND, "backend/template.yaml declares the ScanSecurityGroupId param")

print("")
print("── the store needs no network path; master wires the group to the agent stack ──")
check(not (ROOT / "infrastructure/aurora-store.yaml").exists(),
      "there is no Aurora metrics-store template (the store is DynamoDB + CloudWatch)")
check("AuroraStack" not in MASTER, "master.yaml declares no Aurora stack")
check(MASTER.count("ScanSecurityGroupId: !GetAtt VPCStack.Outputs.ScanSecurityGroupId") >= 1,
      "master.yaml passes the scan group to the agent stack")

print("")
if problems:
    print(f"FAIL — {problems} problem(s)")
    sys.exit(1)
print("PASS")
