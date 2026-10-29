#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Finish a stack delete that failed because the agent runtime had not let go of its
# network interfaces yet.
#
# Why this exists
# ---------------
# Deleting an AWS::BedrockAgentCore::Runtime frequently exceeds CloudFormation's
# stabilization window, so the resource is reported as:
#
#   AgentRuntime | Request timed out while deleting AWS::BedrockAgentCore::Runtime
#                  (HandlerErrorCode: NotStabilized)
#
# The runtime really is deleted - it disappears from list-agent-runtimes - but the
# ENIs it placed in the private subnets stay attached for a while afterwards. They are
# interface type 'agentic_ai' with a service-managed 'ela-attach-*' attachment, which
# means they cannot be detached or deleted by hand:
#
#   Network interface 'eni-...' is currently in use.
#
# While they exist, the subnets and their security group have dependencies, so the VPC
# stack cannot delete and the whole teardown ends in DELETE_FAILED. The fix is simply
# to wait for the service to release them and delete the stack again - which is what
# this script does, so an operator does not have to work any of that out.
#
# Usage:  retry-stack-delete.sh <stack-name> <region> [max-wait-minutes]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./confirm.sh
. "$HERE/confirm.sh"

STACK="${1:?stack name required}"
REGION="${2:?region required}"
MAX_MIN="${3:-30}"

status=$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo GONE)
if [ "$status" = "GONE" ]; then
    echo "   stack $STACK is already gone"
    exit 0
fi

# Establish WHY the delete has not finished before assuming it is the interface problem.
# A delete that never started - because the API call failed, credentials expired, or the
# connection dropped - looks identical from here, and waiting half an hour for interfaces
# to release makes no sense when nothing is deleting them. That happened: a
# "Could not connect to the endpoint URL" on delete-stack was reported as the agent
# runtime holding interfaces, and the wait could never have succeeded.
case "$status" in
    DELETE_IN_PROGRESS|DELETE_FAILED)
        : ;;  # a delete has been attempted, so the interface wait below is the right move
    *)
        # No delete underway means this would START one, so it has to be confirmed here.
        # `make destroy` confirms before it ever gets this far and passes DESTROY_CONFIRMED;
        # run directly against a healthy stack, this path would otherwise delete with no prompt.
        ACCOUNT=$(account_or_die) || exit 1
        require_confirmation \
"   Stack '$STACK' is $status - no delete is underway, so this would START one.

       account  $ACCOUNT
       region   $REGION
       stack    $STACK

   The whole stack and everything it owns will be deleted, including the
   Tier 1 metrics table and both web apps." \
            "'$STACK@$ACCOUNT'" "$STACK@$ACCOUNT" || exit 1
        echo "   stack $STACK is $status - no delete is in progress, so this is not the"
        echo "   network-interface problem. Requesting the delete now."
        if ! aws cloudformation delete-stack --stack-name "$STACK" --region "$REGION"; then
            echo ""
            echo "❌ The delete request failed again. CloudFormation was not reached, so"
            echo "   nothing has been deleted - check credentials and connectivity."
            exit 1
        fi
        if aws cloudformation wait stack-delete-complete --stack-name "$STACK" --region "$REGION" 2>/dev/null; then
            echo "🗑️  Stack deleted."
            exit 0
        fi
        echo "   still not finished; continuing to the interface check below."
        ;;
esac

# Collect the private subnets still owned by the stack, so we watch the right ones.
subnets=$(aws cloudformation describe-stack-resources --stack-name "$STACK" --region "$REGION" \
    --query "StackResources[?ResourceType=='AWS::EC2::Subnet'].PhysicalResourceId" \
    --output text 2>/dev/null | tr '\t' ',' | sed 's/,$//')

if [ -z "$subnets" ]; then
    # Nested stacks hide their resources from the parent; find them via the VPC stack.
    vpc_stack=$(aws cloudformation list-stacks --region "$REGION" \
        --query "StackSummaries[?contains(StackName,'${STACK}-VPCStack') && StackStatus!='DELETE_COMPLETE'].StackId" \
        --output text 2>/dev/null | head -1)
    if [ -n "$vpc_stack" ]; then
        subnets=$(aws cloudformation describe-stack-resources --stack-name "$vpc_stack" --region "$REGION" \
            --query "StackResources[?ResourceType=='AWS::EC2::Subnet'].PhysicalResourceId" \
            --output text 2>/dev/null | tr '\t' ',' | sed 's/,$//')
    fi
fi

if [ -n "$subnets" ]; then
    echo "   waiting for the agent runtime to release its network interfaces"
    echo "   (subnets: $subnets)"
    waited=0
    while [ "$waited" -lt "$MAX_MIN" ]; do
        left=$(aws ec2 describe-network-interfaces --region "$REGION" \
            --filters "Name=subnet-id,Values=$subnets" "Name=interface-type,Values=agentic_ai" \
            --query 'length(NetworkInterfaces)' --output text 2>/dev/null)
        case "$left" in ''|None) left=0 ;; esac
        if [ "$left" = "0" ]; then
            echo "   released after ${waited}m"
            break
        fi
        printf '   %sm: %s interface(s) still attached\n' "$waited" "$left"
        sleep 60
        waited=$((waited + 1))
    done
    if [ "${left:-0}" != "0" ]; then
        echo ""
        echo "⚠️  Still attached after ${MAX_MIN}m."
        echo ""
        echo "   These interfaces are managed by the AgentCore service. They cannot be"
        echo "   detached or deleted directly - the API rejects it with 'currently in use'."
        echo "   They have been observed to persist for over an hour after the runtime is"
        echo "   deleted, so waiting longer may not help."
        echo ""
        echo "   Everything except the VPC is already deleted, and what remains - a VPC,"
        echo "   its private subnets, a security group, an IAM role, a log group and a"
        echo "   small Lambda - does not accrue hourly charges. Two options:"
        echo ""
        echo "   1. Run 'make destroy' again later, once the interfaces are released."
        echo ""
        echo "   2. Delete the stack now and leave the VPC behind, so you are not left"
        echo "      with a stack stuck in DELETE_FAILED:"
        echo ""
        echo "        aws cloudformation delete-stack --stack-name $STACK \\"
        echo "          --region $REGION --retain-resources VPCStack"
        echo ""
        echo "      Then remove the VPC yourself once the interfaces are gone. Confirm"
        echo "      with: aws ec2 describe-network-interfaces --region $REGION \\"
        echo "              --filters Name=interface-type,Values=agentic_ai"
        exit 1
    fi
fi

echo "   deleting stack $STACK again"
aws cloudformation delete-stack --stack-name "$STACK" --region "$REGION"
if aws cloudformation wait stack-delete-complete --stack-name "$STACK" --region "$REGION" 2>/dev/null; then
    echo "🗑️  Stack deleted."
    exit 0
fi

echo ""
echo "⚠️  The delete still did not finish. Inspect what is blocking it with:"
echo "     aws cloudformation describe-stack-events --stack-name $STACK --region $REGION \\"
echo "       --query 'StackEvents[?ResourceStatus==\`DELETE_FAILED\`].[LogicalResourceId,ResourceStatusReason]'"
exit 1
