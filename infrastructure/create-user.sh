#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Create a console user in the deployed Cognito user pool.
#
#   Usage: make cognito-user            (from the repository root)
#          STACK_NAME=dbops ./create-user.sh
#
# Reads everything from the one nested parent stack.

set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="${STACK_NAME:-dbops}"

echo "👤 Create a console user"
echo ""

OUTPUTS_JSON="$(aws cloudformation describe-stacks \
  --region "$AWS_REGION" --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs' --output json 2>/dev/null || echo 'null')"

if [ "$OUTPUTS_JSON" = "null" ]; then
  echo "❌ Could not read stack '$STACK_NAME' in $AWS_REGION."
  echo "   Deploy it first:  make deploy (from the repository root)"
  echo "   Or point at a different stack:  make cognito-user STACK_NAME=<name>"
  exit 1
fi

out() {
  printf '%s' "$OUTPUTS_JSON" | python3 -c "
import json, sys
key = sys.argv[1]
for o in json.load(sys.stdin) or []:
    if o['OutputKey'] == key:
        print(o['OutputValue']); break
" "$1"
}

USER_POOL_ID="$(out UserPoolId)"
WEB_APP_URL="$(out DashboardUrl)"
ADMIN_URL="$(out AdminConsoleUrl)"

if [ -z "$USER_POOL_ID" ]; then
  echo "❌ Stack '$STACK_NAME' has no UserPoolId output — is it fully deployed?"
  exit 1
fi

echo "   Stack:     $STACK_NAME"
echo "   User pool: $USER_POOL_ID"
echo ""

read -p "Email: " USER_EMAIL
if [ -z "$USER_EMAIL" ]; then
  echo "❌ Email is required."
  exit 1
fi

read -p "Full name (optional): " USER_NAME

read -s -p "Password (min 8 chars, upper+lower+number): " USER_PASSWORD
echo ""
if [ -z "$USER_PASSWORD" ]; then
  echo "❌ Password is required."
  exit 1
fi

echo ""
echo "Select role:"
echo "  1) Admin    — manage Agent Spaces and use the admin console"
echo "  2) Operator — use the operator app only"
read -p "Role [1/2]: " ROLE_CHOICE

case "$ROLE_CHOICE" in
  1) GROUP_NAME="admins" ;;
  2) GROUP_NAME="operators" ;;
  *) echo "❌ Invalid choice. Use 1 or 2."; exit 1 ;;
esac

echo ""
echo "🔐 Creating user..."

# email_verified=true matters: the pool uses email as the sign-in alias, and an
# unverified address leaves the user stuck in a verification flow it never receives
# (this path sets the password directly and suppresses the invite email).
USER_ATTRS=(Name=email,Value="$USER_EMAIL" Name=email_verified,Value=true)
if [ -n "$USER_NAME" ]; then
  USER_ATTRS+=(Name=name,Value="$USER_NAME")
fi

aws cognito-idp admin-create-user \
  --region "$AWS_REGION" \
  --user-pool-id "$USER_POOL_ID" \
  --username "$USER_EMAIL" \
  --user-attributes "${USER_ATTRS[@]}" \
  --message-action SUPPRESS \
  > /dev/null

# --permanent skips the FORCE_CHANGE_PASSWORD challenge, so the user can sign in
# with exactly the password entered above.
aws cognito-idp admin-set-user-password \
  --region "$AWS_REGION" \
  --user-pool-id "$USER_POOL_ID" \
  --username "$USER_EMAIL" \
  --password "$USER_PASSWORD" \
  --permanent

aws cognito-idp admin-add-user-to-group \
  --region "$AWS_REGION" \
  --user-pool-id "$USER_POOL_ID" \
  --username "$USER_EMAIL" \
  --group-name "$GROUP_NAME"

echo ""
echo "✅ User created: $USER_EMAIL (role: $GROUP_NAME)"
echo ""
if [ "$GROUP_NAME" = "admins" ] && [ -n "$ADMIN_URL" ]; then
  echo "🌐 Admin console: $ADMIN_URL"
fi
if [ -n "$WEB_APP_URL" ]; then
  echo "🌐 Operator app:  $WEB_APP_URL"
else
  echo "   Operator app:  http://localhost:3000 (local dev)"
fi
