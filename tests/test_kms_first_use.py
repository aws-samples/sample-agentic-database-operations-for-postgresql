# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A first deploy into a fresh account does not race the AWS managed KMS keys.

The tables (and the SNS topic) encrypt with AWS managed keys - aws/dynamodb, aws/sns -
which do not exist in an account that has never used them. The first resource to need
one creates it, and a resource created in the same instant can be refused with "KMS key
... does not exist" before the new key propagates: a first deploy failed on one table
190 ms after the key was created while the other eleven succeeded. Two guards:

  * the stack deploy creates both keys up front with DescribeKey on the predefined
    alias (documented KMS behaviour), best effort, before CloudFormation runs;
  * storage.yaml creates one table first and the other eleven DependsOn it, so the key
    exists before they ask for it even without the first guard.

Run:  python3 tests/test_kms_first_use.py
"""
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
passed = failed = 0


def check(cond, label):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}")


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_multi_constructor("!", lambda loader, suffix, node: None)

print("== the tables are created one first, then the rest ==")
doc = yaml.load((ROOT / "infrastructure/storage.yaml").read_text(encoding="utf-8"), Loader=_Loader)  # nosec B506
tables = {k: v for k, v in doc["Resources"].items() if v.get("Type") == "AWS::DynamoDB::Table"}
check(len(tables) == 12, f"twelve tables ({len(tables)})")
first = [k for k, v in tables.items() if not v.get("DependsOn")]
check(first == ["InstancesTable"], f"exactly one table has no dependency: the first ({first})")
for k, v in tables.items():
    if k == "InstancesTable":
        continue
    deps = v.get("DependsOn")
    deps = deps if isinstance(deps, list) else [deps]
    check("InstancesTable" in deps, f"{k} waits for InstancesTable")
kms_tables = [k for k, v in tables.items() if (v.get("Properties") or {}).get("SSESpecification", {}).get("SSEEnabled")]
check(set(kms_tables) == set(tables), "every table encrypts with the managed key (the reason for the order)")

print("")
print("== the stack deploy creates the managed keys before CloudFormation runs ==")
mk = (ROOT / "Makefile").read_text(encoding="utf-8")
recipe = mk.split("\ndeploy-stack-run:", 1)[1].split("\n\n", 1)[0]
warm = recipe.find("aws kms describe-key")
cfn = recipe.find("aws cloudformation deploy")
check(warm != -1 and cfn != -1 and warm < cfn, "DescribeKey runs before the first cloudformation deploy")
block = recipe[warm - 200: warm + 200]
check("alias/aws/dynamodb" in block and "alias/aws/sns" in block, "for both keys the stack uses")
check("|| true" in recipe[warm: warm + 200], "best effort: a principal without kms:DescribeKey still deploys")
check(re.search(r"describe-key --key-id \"\$\$a\" --region \$\(AWS_REGION\)", recipe) is not None,
      "in the deployment's own region")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
