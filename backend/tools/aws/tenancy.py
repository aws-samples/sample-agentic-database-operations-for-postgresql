# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared tenancy predicates: which spaces include a physical-database record, and
which credential each of them connects with.

Under DB-level tenancy the physical database is the unit. `member_space_ids` is the set
of spaces that include it, maintained by `add_member` / `remove_member`, and
`space_secrets` holds, for every member, the Secrets Manager ARN that space proved at
onboarding. There is one kind of member: every member holds a secret. Several members
may hold the same ARN (all of one admin's spaces share one secret namespace), and that
is the only way a credential is ever shared.

This lives in its own leaf module so every reader, the REST layer, the agent tool
resolver, the SQL secret guard, the DB listing filter and the topology builder, shares
one definition.
"""


def record_member_space_ids(record: dict) -> set:
    """The set of space ids that include this physical-DB record (the tenant boundary).

    A record whose `status` is still `discovered` has never been onboarded by anyone, so
    it has no members: membership is granted only by onboarding with a verified secret,
    which moves the status on. Any space listed on such a record is reported as a
    *candidate* instead (see `record_candidate_space_ids`), never as a member."""
    if not record or _never_onboarded(record):
        return set()
    return set(record.get("member_space_ids") or [])


def record_candidate_space_ids(record: dict) -> set:
    """Spaces that have discovered this record but not onboarded it. Never used for
    authorization; the admin console's Discovery step reads it. For a never-onboarded
    record this also folds in any member set, so a space listed there sees the database
    as a proposal it can onboard or dismiss rather than as an invisible row."""
    if not record:
        return set()
    out = set(record.get("candidate_space_ids") or [])
    if _never_onboarded(record):
        out |= set(record.get("member_space_ids") or [])
    return out


def _never_onboarded(record: dict) -> bool:
    return record.get("status") == "discovered"


def record_in_space(record: dict, space_id: str) -> bool:
    """True if `space_id` is one of the record's member spaces."""
    return bool(space_id) and space_id in record_member_space_ids(record)


def credential_arn_for(record: dict, space_id: str) -> str:
    """The secret ARN an agent turn running in `space_id` connects with: the one that
    space proved at onboarding (`space_secrets[space_id]`), else '' (not a member). The
    record's active `secret_arn` is what monitoring uses; the agent never falls back to
    it, so a space without a recorded proof connects with nothing."""
    if not record or not space_id or not record_in_space(record, space_id):
        return ""
    return (record.get("space_secrets") or {}).get(space_id) or ""


def record_holds_secret(record: dict, secret_arn: str) -> bool:
    """True if `secret_arn` is this record's active credential or any member's proof."""
    if not record or not secret_arn:
        return False
    if record.get("secret_arn") == secret_arn:
        return True
    return secret_arn in set((record.get("space_secrets") or {}).values())


def spaces_entitled_to_secret(record: dict, secret_arn: str) -> set:
    """Member spaces whose recorded proof is `secret_arn`: the spaces whose agent turns may
    connect with it. Empty when the record does not hold it."""
    if not record_holds_secret(record, secret_arn):
        return set()
    secrets = record.get("space_secrets") or {}
    members = record_member_space_ids(record)
    return {s for s in members if secrets.get(s) == secret_arn}
