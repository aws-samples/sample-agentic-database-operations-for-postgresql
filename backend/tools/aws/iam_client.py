# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""IAM client — manage roles for Agent Spaces."""

import os
import logging
import boto3

logger = logging.getLogger("iam-client")


class IAMClient:
    def __init__(self):
        self._iam = None
        self._region = os.getenv("AWS_REGION", "us-east-1")

    @property
    def iam(self):
        if self._iam is None:
            self._iam = boto3.client("iam", region_name=self._region)
        return self._iam

    def delete_role(self, role_name: str) -> dict:
        """Delete a role and its inline policies."""
        try:
            policies = self.iam.list_role_policies(RoleName=role_name)
            for p in policies.get("PolicyNames", []):
                self.iam.delete_role_policy(RoleName=role_name, PolicyName=p)
            self.iam.delete_role(RoleName=role_name)
            return {"status": "deleted"}
        except Exception as e:
            return {"error": str(e)}


iam_client = IAMClient()
