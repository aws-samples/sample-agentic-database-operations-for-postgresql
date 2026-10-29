# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Shared hard confirmation for everything in this directory. Sourced, not executed.
#
# Why the guard lives here rather than in the Makefile
# ----------------------------------------------------
# These scripts are reachable directly, not only through `make`. With the prompt in a
# Makefile target and the deletion in the script, anything that invoked a script
# directly - a person reading the directory listing, a copied command from a runbook, an
# agent - would delete a whole installation with no prompt at all:
#
#     DELETE=1 check-orphans.sh dbops dbops us-east-1     # no confirmation, ever
#
# A guard that only protects one caller is not a guard. So it travels with the action:
# every destructive script here sources this file and cannot proceed without it.

# require_confirmation <what-it-does> <token-label> <expected-token>
#
# Prints a description of the damage, then demands the token be typed exactly. The token
# is deliberately awkward - "<name>@<account>" - because the mistake this exists to stop
# was running a delete against the RIGHT stack name in the WRONG account. A stack name
# alone cannot express "here, and nowhere else": every account uses the same one.
require_confirmation() {
    _summary="$1"
    _label="$2"
    _expected="$3"

    # Already confirmed upstream in the same command? Only honoured for a value that
    # matches this exact target, so it cannot be set once and reused elsewhere.
    if [ "${DESTROY_CONFIRMED:-}" = "$_expected" ]; then
        echo "   (confirmed: $_expected)"
        return 0
    fi

    echo ""
    echo "╔══════════════════════════════════════════════════════════════════════╗"
    echo "║  DESTRUCTIVE OPERATION                                               ║"
    echo "╚══════════════════════════════════════════════════════════════════════╝"
    echo ""
    printf '%s\n' "$_summary"
    echo ""
    echo "   This cannot be undone. Nothing has been deleted yet."
    echo ""
    printf "   To proceed, type %s exactly (anything else aborts): " "$_label"

    if ! IFS= read -r _answer; then
        echo ""
        echo "   No input - aborted. Nothing was deleted."
        return 1
    fi

    if [ "$_answer" != "$_expected" ]; then
        echo ""
        echo "   That does not match '$_expected' - aborted. Nothing was deleted."
        return 1
    fi
    echo ""
    return 0
}

# account_or_die - resolve the account the credentials point at, refusing to guess.
# Every message here names the account, because the stack name does not identify a
# target on its own.
account_or_die() {
    _acct=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
    if [ -z "$_acct" ] || [ "$_acct" = "None" ]; then
        echo "❌ No usable AWS credentials - refusing to delete anything." >&2
        return 1
    fi
    printf '%s' "$_acct"
}
