#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Generate backend/.env and frontend/.env from a deployed stack, for local development.

Replaces backend/scripts/setup-env.sh and frontend/setup-env.sh, which between them
queried five separate standalone stacks by hardcoded name. Those stacks no longer
exist: everything now deploys as one nested stack, so this walks the parent's child
stacks and reads their outputs instead of guessing names.

    python3 deployment/generate-env.py [--stack-name dbops] [--region us-east-1]

Only local development needs these files. A deployed backend reads the same settings
from its task definition, and the deployed web apps read config.js.
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys
import urllib.parse

REPO = pathlib.Path(__file__).resolve().parent.parent


def aws(*args: str) -> str:
    """Run an AWS CLI command, returning stdout. Empty string on failure."""
    try:
        return subprocess.run(
            ["aws", *args],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def outputs_of(stack: str, region: str) -> dict:
    raw = aws("cloudformation", "describe-stacks", "--region", region,
              "--stack-name", stack, "--query", "Stacks[0].Outputs", "--output", "json")
    if not raw or raw == "null":
        return {}
    return {o["OutputKey"]: o["OutputValue"] for o in json.loads(raw)}


def parameters_of(stack: str, region: str) -> dict:
    raw = aws("cloudformation", "describe-stacks", "--region", region,
              "--stack-name", stack, "--query", "Stacks[0].Parameters", "--output", "json")
    if not raw or raw == "null":
        return {}
    return {p["ParameterKey"]: p["ParameterValue"] for p in json.loads(raw)}


def child_stacks(parent: str, region: str) -> dict:
    """Logical id -> physical stack id, for each nested stack of the parent."""
    raw = aws("cloudformation", "describe-stack-resources", "--region", region,
              "--stack-name", parent, "--output", "json",
              "--query", "StackResources[?ResourceType=='AWS::CloudFormation::Stack']"
                         ".[LogicalResourceId,PhysicalResourceId]")
    if not raw or raw == "null":
        return {}
    return {logical: physical for logical, physical in json.loads(raw)}


def write_env(path: pathlib.Path, lines: list) -> None:
    existing = path.exists()
    path.write_text("\n".join(lines) + "\n")
    print(f"   {'updated' if existing else 'created'} {path.relative_to(REPO)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stack-name", default=os.environ.get("STACK_NAME", "dbops"),
                    help="Name of the deployed parent stack (default: dbops)")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    args = ap.parse_args()

    stack, region = args.stack_name, args.region

    print(f"🔧 Reading stack '{stack}' in {region}")
    parent = outputs_of(stack, region)
    if not parent:
        print(f"❌ Could not read stack '{stack}' in {region}.", file=sys.stderr)
        print("   Deploy it first:  make deploy", file=sys.stderr)
        print("   Or point at another stack:  make env STACK_NAME=<name>", file=sys.stderr)
        return 1

    params = parameters_of(stack, region)
    prefix = params.get("ResourcePrefix", "dbops")
    model_id = params.get("AgentModelId", "global.anthropic.claude-haiku-5-5")
    app_name = params.get("AppName", "DBOps Agent (Beta)")
    children = child_stacks(stack, region)
    print(f"   prefix: {prefix}")
    print(f"   nested stacks: {', '.join(sorted(children)) or 'none found'}")

    # Read each child's outputs. Keyed by logical id so two children exposing the same
    # output name (both SPA stacks expose CloudFrontDomainName) cannot shadow each other.
    child_out = {name: outputs_of(sid, region) for name, sid in sorted(children.items())}

    def frm(child: str, key: str, default: str = "") -> str:
        return child_out.get(child, {}).get(key, default) or default

    user_pool = frm("CognitoStack", "UserPoolId")
    web_client = frm("CognitoStack", "WebClientId")
    m2m_client = frm("CognitoStack", "M2MClientId")
    cognito_domain = frm("CognitoStack", "CognitoDomain")

    # The M2M client secret is not a stack output by design - secrets do not belong in
    # CloudFormation outputs - so read it from Cognito directly.
    m2m_secret = ""
    if user_pool and m2m_client:
        m2m_secret = aws("cognito-idp", "describe-user-pool-client", "--region", region,
                         "--user-pool-id", user_pool, "--client-id", m2m_client,
                         "--query", "UserPoolClient.ClientSecret", "--output", "text")
        if m2m_secret == "None":
            m2m_secret = ""

    api_url = parent.get("ApiUrl") or frm("APIStack", "ApiUrl")
    runtime_arn = parent.get("AgentRuntimeArn") or frm("AgentStack", "RuntimeArn")

    # The operator app streams chat straight from AgentCore Runtime. That endpoint is
    # derived from the runtime ARN rather than published as a URL.
    deployed_backend_url = ""
    if runtime_arn:
        encoded = urllib.parse.quote(runtime_arn, safe="")
        deployed_backend_url = (
            f"https://bedrock-agentcore.{region}.amazonaws.com"
            f"/runtimes/{encoded}/invocations?qualifier=DEFAULT"
        )

    print()
    write_env(REPO / "backend" / ".env", [
        f"# Generated by deployment/generate-env.py from stack '{stack}'. Local development only.",
        f"AWS_REGION={region}",
        "",
        "# Every physical resource name derives from this prefix - keep it equal to the",
        "# deployed stack's ResourcePrefix or the app will look up tables that do not exist.",
        f"RESOURCE_PREFIX={prefix}",
        "",
        "# Bedrock",
        f"AGENT_MODEL_ID={model_id}",
        f"BEDROCK_REGION={region}",
        "",
        "# Cognito",
        f"USER_POOL_ID={user_pool}",
        f"WEB_CLIENT_ID={web_client}",
        f"M2M_CLIENT_ID={m2m_client}",
        f"COGNITO_DOMAIN={cognito_domain}",
        f"M2M_CLIENT_SECRET={m2m_secret}",
        "",
        "# Storage. DB_CONFIG_TABLE and the other table names are derived from",
        "# RESOURCE_PREFIX by backend/tools/resource_names.py; set one here only to",
        "# override a single table.",
        f"REPORTS_S3_BUCKET={frm('StorageStack', 'ReportsBucketName')}",
        f"NOTIFICATION_TOPIC_ARN={frm('StorageStack', 'NotificationTopicArn')}",
        "",
        "# Metrics store: two DynamoDB tables plus CloudWatch custom metrics. IAM only;",
        "# no credential to write here.",
        f"TIER1_METRICS_TABLE={frm('StorageStack', 'Tier1MetricsTableName')}",
        f"TIER2_SCANS_TABLE={frm('StorageStack', 'Tier2ScansTableName')}",
        "",
        "# VPC",
        f"VPC_ID={frm('VPCStack', 'VpcId')}",
        f"SUBNET_IDS={frm('VPCStack', 'PrivateSubnetIds')}",
        f"SECURITY_GROUP_IDS={frm('VPCStack', 'PrivateSecurityGroupId')}",
        f"PRIVATE_ROUTE_TABLE_ID={frm('VPCStack', 'PrivateRouteTableId')}",
        "",
        "# AWS Batch",
        f"BATCH_JOB_QUEUE={frm('AgentStack', 'BatchJobQueueArn')}",
        f"BATCH_JOB_DEFINITION={frm('AgentStack', 'BatchJobDefinitionArn')}",
        "",
        "# AgentCore",
        f"AGENT_ENDPOINT={runtime_arn}",
    ])

    write_env(REPO / "frontend" / ".env", [
        f"# Generated by deployment/generate-env.py from stack '{stack}'. Local development only.",
        f"VITE_AWS_REGION={region}",
        f"VITE_API_URL={api_url}",
        f"VITE_COGNITO_DOMAIN={cognito_domain}",
        f"VITE_CLIENT_ID={web_client}",
        f"VITE_DEPLOYED_BACKEND_URL={deployed_backend_url}",
        "VITE_LOCAL_BACKEND_URL=http://localhost:8084",
        f"VITE_APP_NAME={app_name}",
    ])

    write_env(REPO / "admin-console" / ".env", [
        f"# Generated by deployment/generate-env.py from stack '{stack}'. Local development only.",
        f"VITE_AWS_REGION={region}",
        f"VITE_API_URL={api_url}",
        f"VITE_COGNITO_DOMAIN={cognito_domain}",
        f"VITE_CLIENT_ID={web_client}",
        f"VITE_WEBAPP_URL={parent.get('DashboardUrl', '')}",
        f"VITE_APP_NAME={app_name}",
    ])

    missing = [n for n, v in [
        ("Cognito user pool", user_pool),
        ("API URL", api_url),
        ("VPC", frm("VPCStack", "VpcId")),
    ] if not v]
    print()
    if missing:
        print("⚠️  Some values were empty: " + ", ".join(missing))
        print("   The stack may still be deploying. Re-run once it completes.")
    else:
        print("✅ Environment files written.")
    print("   Start the backend locally with: make dev")
    return 0


if __name__ == "__main__":
    sys.exit(main())
