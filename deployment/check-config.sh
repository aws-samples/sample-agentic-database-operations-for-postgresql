#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Report and validate the effective deployment settings.
#
# Why this exists
# ---------------
# Settings live in deploy.config, which `make deploy` writes on its first run and then
# reuses silently. That is convenient but easy to get wrong in ways that only show up
# 30 minutes into a deploy, or after it:
#
#   - deploy.config already exists from someone else's run, so `make deploy` skips all
#     prompts and quietly uses their admin email. Cognito then mails the temporary
#     password to the wrong person and nobody can sign in.
#   - the saved AWS profile points at a different account than the credentials in the
#     current shell, so resources land somewhere unexpected.
#   - the resource prefix does not satisfy the pattern the templates enforce, and
#     CloudFormation rejects it after the artifacts have already been staged.
#
# So this validates the settings up front and prints exactly what will be used. It is
# run by `make preflight` (to report) and by `make deploy-stack` (to fail fast).
#
# Reads its values from the environment, which the Makefile exports, so there is one
# source of truth for the settings rather than a second parser for deploy.config.
#
# Usage:  check-config.sh [--quiet]
#         Exit 0 = ready to deploy, 1 = something needs fixing.
set -uo pipefail

QUIET=0
# --settings-only validates the values without calling AWS. Useful for checking a
# config file on a machine with no credentials, and for the tests, which exercise the
# validation rules and must not depend on whether a profile happens to be exported.
SETTINGS_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --quiet) QUIET=1 ;;
        --settings-only) SETTINGS_ONLY=1 ;;
    esac
done

CONFIG_FILE="${CONFIG_FILE:-deploy.config}"
problems=0
warnings=0

say()  { [ "$QUIET" = "0" ] && printf '%s\n' "$1"; return 0; }
bad()  { printf '   ❌ %s\n' "$1" >&2; problems=$((problems + 1)); }
warn() { printf '   ⚠️  %s\n' "$1" >&2; warnings=$((warnings + 1)); }

if [ -f "$CONFIG_FILE" ]; then
    say "   config file.......... ✅ $CONFIG_FILE"
else
    say "   config file.......... ➖ none yet — 'make deploy' will prompt and create it"
fi

# ── the values that must be right ────────────────────────────────────────────
prefix="${RESOURCE_PREFIX:-}"
if [ -z "$prefix" ]; then
    bad "RESOURCE_PREFIX is empty"
elif ! printf '%s' "$prefix" | grep -Eq '^[a-z][a-z0-9-]{1,20}$'; then
    bad "RESOURCE_PREFIX '$prefix' is invalid. The templates require 2-21 characters,
      lowercase letters, digits and hyphens, starting with a letter. It prefixes S3
      bucket names and Lambda function names, which is why it is restricted."
else
    say "   resource prefix...... ✅ $prefix"
fi

region="${AWS_REGION:-}"
if [ -z "$region" ]; then
    bad "AWS_REGION is empty"
else
    say "   region............... ✅ $region"
    if [ "$region" != "us-east-1" ]; then
        warn "Region is $region. us-east-1 is recommended: the optional CloudFront WAF is
      only valid there, and AgentCore is not available in every region."
    fi
fi

email="${INITIAL_ADMIN_EMAIL:-}"
if [ -z "$email" ]; then
    bad "INITIAL_ADMIN_EMAIL is not set. Cognito mails the first temporary password
      there, so without it nobody can sign in. Run 'make config' to set it."
elif ! printf '%s' "$email" | grep -Eq '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$'; then
    bad "INITIAL_ADMIN_EMAIL '$email' does not look like an email address."
else
    say "   admin email.......... ✅ $email"
    # The trap this check exists for: a config inherited from another run.
    if [ -f "$CONFIG_FILE" ] && [ "${CONFIRM_ADMIN_EMAIL:-0}" != "1" ]; then
        say "                            ↳ the temporary password goes here — check it is you"
    fi
fi

say "   admin display name... ✅ ${INITIAL_ADMIN_NAME:-Administrator}"
say "   stack name........... ✅ ${STACK_NAME:-$prefix}"

# ── the account actually being deployed into ─────────────────────────────────
# Detected, never configured. deploy.config holds no profile, so there is only one
# source for this - the shell, or the default credential chain - and nothing to
# reconcile. All this has to do is state where the commands will land, and fail clearly
# when nothing resolves.
if [ "$SETTINGS_ONLY" = "1" ]; then
    acct="skipped"
else
    acct=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
fi
if [ "$acct" = "skipped" ]; then
    say "   target account....... ➖ not checked (--settings-only)"
elif [ -z "$acct" ] || [ "$acct" = "None" ]; then
    if [ -n "${AWS_PROFILE:-}" ]; then
        bad "Profile '$AWS_PROFILE' does not resolve to usable credentials - it is most
      likely expired. Refresh it and re-run."
    else
        # No profile set, and the default chain came up empty. List what is configured,
        # so the answer is on screen rather than something to go looking for.
        avail=$(aws configure list-profiles 2>/dev/null | tr '\n' ' ')
        if [ -n "$avail" ]; then
            bad "No AWS profile is set and the default credential chain has no usable
      credentials. Profiles configured on this machine: $avail
      Point your shell at one: export AWS_PROFILE=<name>, or run 'aws configure'."
        else
            bad "No AWS credentials found, and no named profiles are configured. Run
      'aws configure' (or sign in to your SSO session) and re-run."
        fi
    fi
else
    if [ -n "${AWS_PROFILE:-}" ]; then
        say "   target account....... ✅ $acct (profile $AWS_PROFILE)"
    else
        say "   target account....... ✅ $acct (default credential chain, no profile set)"
    fi
fi

# A profile saved by an older version is inert now, but it still reads like configuration.
# Say that it is ignored, so nobody trusts it when deciding which account they are in.
if [ -f "$CONFIG_FILE" ] && grep -qE '^[[:space:]]*AWS_PROFILE[[:space:]]*=[[:space:]]*[^[:space:]]' "$CONFIG_FILE"; then
    say "                            ↳ $CONFIG_FILE also names a profile; it is IGNORED"
    say "                              (your shell decides). 'make config' removes it."
fi

# ── optional settings, reported so their absence is a choice, not a surprise ──
if [ -n "${EMAIL_SENDER_ADDRESS:-}" ]; then
    say "   invitation sender.... ✅ $EMAIL_SENDER_ADDRESS (must be a verified SES identity)"
else
    say "   invitation sender.... ➖ unset — set it later in the admin console"
fi
if [ -n "${NOTIFICATION_EMAIL:-}" ]; then
    say "   alert email.......... ✅ $NOTIFICATION_EMAIL"
else
    say "   alert email.......... ➖ unset — no alerts for critical findings"
fi

if [ "$problems" -gt 0 ]; then
    echo "" >&2
    echo "   $problems setting(s) need fixing. Run 'make config' to set them" >&2
    echo "   interactively, or edit $CONFIG_FILE directly." >&2
    exit 1
fi
exit 0
