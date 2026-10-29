# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Multi-tenant secret-ownership guard for pg_client.

Locks in _enforce_secret_space_ownership: within an authenticated AGENT request, the
secret_arn a DB tool connects with MUST belong to an instance in the caller's VALIDATED
space (CURRENT_SPACE_ID). A cross-space or unknown secret is refused; an unresolved space
(empty) in an agent request is also refused (fail-closed). Outside the agent request
context (no CURRENT_USER_ID — batch / Tier-1 Lambdas) the guard is a no-op.

Membership is the authority: the guard reads the SET of spaces that include the
instance owning the secret, so a database shared across spaces is reachable from every
member, and a secret whose instance no longer includes the caller's space is refused.

Hermetic: psycopg2 + boto3 stubbed at import; _member_spaces_for_secret monkeypatched
(no AWS).

Run:  python3 backend/tests/test_secret_space_guard.py   (or via pytest)
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# pg_client imports these at module load — stub so no real DB/AWS libs are needed.
for _m in ("psycopg2", "psycopg2.extras", "boto3"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

from tools.aws import pg_client as pg  # noqa: E402
import model_resolver as mr  # noqa: E402

SPACE_A = "aaaaaaaa-space-a"
SPACE_B = "bbbbbbbb-space-b"
# Membership map: secret → set of spaces whose members include the owning instance.
# arn:secret:shared belongs to BOTH spaces (one physical DB shared across spaces).
_MEMBERS = {
    "arn:secret:a": {SPACE_A},
    "arn:secret:b": {SPACE_B},
    "arn:secret:shared": {SPACE_A, SPACE_B},
}

# The real resolver, kept for the full-record tests below before the gate tests stub it.
_real_member_spaces = pg._member_spaces_for_secret
_real_record_lookup = pg._records_holding_secret
# Deterministic membership lookup (no DynamoDB). Returns a SET (the tenancy authority).
pg._member_spaces_for_secret = lambda s: set(_MEMBERS.get(s, set()))


def _ctx(user="", space=""):
    mr.CURRENT_USER_ID.set(user)
    mr.CURRENT_SPACE_ID.set(space)


def test_allows_secret_in_caller_space():
    _ctx(user="u1", space=SPACE_A)
    pg._enforce_secret_space_ownership("arn:secret:a")  # must NOT raise


def test_allows_shared_db_from_each_member_space():
    # One physical DB shared across spaces A and B: BOTH members may connect.
    _ctx(user="u1", space=SPACE_A)
    pg._enforce_secret_space_ownership("arn:secret:shared")  # must NOT raise
    _ctx(user="u2", space=SPACE_B)
    pg._enforce_secret_space_ownership("arn:secret:shared")  # must NOT raise


def test_blocks_cross_space_secret():
    _ctx(user="u1", space=SPACE_A)
    try:
        pg._enforce_secret_space_ownership("arn:secret:b")
        raise AssertionError("expected PermissionError for another space's secret")
    except PermissionError:
        pass


def test_blocks_unknown_secret():
    _ctx(user="u1", space=SPACE_A)
    try:
        pg._enforce_secret_space_ownership("arn:secret:unknown")
        raise AssertionError("expected PermissionError for a secret not in the registry")
    except PermissionError:
        pass


def test_blocks_when_space_unresolved_in_agent_ctx():
    # Agent request (user set) but no resolved space → fail closed.
    _ctx(user="u1", space="")
    try:
        pg._enforce_secret_space_ownership("arn:secret:a")
        raise AssertionError("expected PermissionError when space is empty in an agent request")
    except PermissionError:
        pass


def test_noop_outside_agent_context():
    # No CURRENT_USER_ID → batch / Tier-1 path; the guard must not interfere.
    _ctx(user="", space="")
    pg._enforce_secret_space_ownership("arn:secret:b")  # must NOT raise

# ── the real resolver, on full records ──────────────────────────────────────────
# The gate tests above stub _member_spaces_for_secret; these run the REAL one with only
# the record lookup stubbed, so the projection and the membership rule are exercised
# together. A candidate space, or a never-onboarded record, must yield NO entitled space
# even though the space id sits on the record.
import inspect as _inspect  # noqa: E402


def _with_record(record):
    """Run the real resolver against one registry record, cache cleared."""
    pg._secret_space_cache.clear()
    records = record if isinstance(record, list) else ([record] if record else [])
    pg._records_holding_secret = lambda arn, first_only=False: list(records)
    return _real_member_spaces("arn:secret:x")


def test_real_resolver_entitles_every_member_whose_proof_is_this_secret():
    # Two spaces of one admin hold the same ARN (their shared namespace); a third member
    # holds a different secret and is not entitled to this one.
    rec = {"status": "configured", "secret_arn": "arn:secret:x", "reach_space_id": SPACE_A,
           "member_space_ids": {SPACE_A, SPACE_B, "cccccccc-space-c"},
           "space_secrets": {SPACE_A: "arn:secret:x", SPACE_B: "arn:secret:x", "cccccccc-space-c": "arn:secret:c"}}
    assert _with_record(rec) == {SPACE_A, SPACE_B}


def test_real_resolver_gives_nothing_to_a_member_with_no_recorded_proof():
    # A member without a space_secrets entry connects with nothing: the agent never falls
    # back to the record's active secret, which is monitoring's.
    rec = {"status": "configured", "secret_arn": "arn:secret:x", "reach_space_id": SPACE_A,
           "member_space_ids": {SPACE_A, SPACE_B}, "space_secrets": {SPACE_A: "arn:secret:x"}}
    assert _with_record(rec) == {SPACE_A}


def test_real_resolver_refuses_a_space_that_is_only_a_candidate():
    # SPACE_B was proposed the database by discovery and never onboarded it. It is a
    # candidate, not a member, and must not be entitled to A's credential.
    rec = {"status": "configured", "secret_arn": "arn:secret:x", "reach_space_id": SPACE_A,
           "member_space_ids": {SPACE_A}, "candidate_space_ids": {SPACE_B},
           "space_secrets": {SPACE_A: "arn:secret:x"}}
    assert _with_record(rec) == {SPACE_A}


def test_real_resolver_refuses_every_space_on_a_never_onboarded_record():
    # A record still at status=discovered has no members, whatever ids it carries from
    # discovery (member_space_ids left by an earlier model, or the legacy tags.space_id).
    rec = {"status": "discovered", "secret_arn": "arn:secret:x", "reach_space_id": SPACE_A,
           "member_space_ids": {SPACE_A}, "tags": {"space_id": SPACE_A},
           "space_secrets": {SPACE_A: "arn:secret:x"}}
    assert _with_record(rec) == set()


def test_real_resolver_unions_every_record_holding_the_secret():
    # An RDS read replica shares its source's read-only user, so one secret is held by
    # two records. Each record's entitled spaces count; per-record entitlement is still
    # enforced when the database id is resolved (credential_arn_for).
    primary = {"status": "configured", "secret_arn": "arn:secret:x", "reach_space_id": SPACE_A,
               "member_space_ids": {SPACE_A}, "space_secrets": {SPACE_A: "arn:secret:x"}}
    replica = {"status": "configured", "secret_arn": "arn:secret:x", "reach_space_id": SPACE_B,
               "member_space_ids": {SPACE_B}, "space_secrets": {SPACE_B: "arn:secret:x"}}
    assert _with_record([primary, replica]) == {SPACE_A, SPACE_B}


def test_record_lookup_projects_every_field_the_membership_rule_reads():
    # The rule (tenancy.record_member_space_ids) reads status, member_space_ids,
    # candidate_space_ids and tags. A projection missing any of them hands the rule an
    # incomplete record, and an incomplete record can call a candidate a member.
    src = _inspect.getsource(_real_record_lookup)
    for field in ("member_space_ids", "candidate_space_ids", "#st", "tags", "space_secrets", "reach_space_id"):
        assert field in src, f"_records_holding_secret must project {field}"
    assert '"#st": "status"' in src
    assert "ExclusiveStartKey" in src, "the scan paginates; a secret on a later page must still resolve"



def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", fn.__name__, "-", e or "assertion failed")
    print("\n%d passed, %d failed" % (len(fns) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
