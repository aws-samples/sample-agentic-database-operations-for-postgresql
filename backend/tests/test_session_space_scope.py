# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Database target resolution must be scoped to the caller's space, by membership.

``session_factory.get_session`` takes a name from the caller and mints AWS credentials
for whichever account the registry says that name lives in. The caller never holds those
credentials, so this is a privilege-granting step, not a lookup — and it is the single
point every observability tool (CloudWatch, Performance Insights, CloudTrail, CloudWatch
Logs) passes through.

Under DB-level tenancy the physical database is one registry row, and ``member_space_ids``
is the authority: the set of spaces that include it. Discovery de-duplicates globally by
ARN, so a database shared across spaces is one row with several members, not one row per
space. ``tags.space_id`` is a legacy single-owner field, stamped once at first discovery
and never updated; authorization MUST read membership, not that frozen field.

Properties asserted here:

  * A caller whose space is NOT a member cannot resolve the database — the isolation fix.
  * A caller in ANY member space resolves the shared database — so the agent honours a
    cross-space share for every member, not only the space that first discovered it.
  * A space removed from a database is refused on the agent path even though the frozen
    ``tags.space_id`` still names it — removal revokes everywhere.

Fail modes mirror ``pg_client._enforce_secret_space_ownership`` exactly (see
test_secret_space_guard.py), including the pass-through outside an agent request that
batch, Tier-1 and the REST handlers rely on.

Hermetic: boto3 stubbed at import; no AWS.

Run:  python3 backend/tests/test_session_space_scope.py   (or via pytest)
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

SPACE_A = "aaaaaaaa-space-a"
SPACE_B = "bbbbbbbb-space-b"
SPACE_C = "cccccccc-space-c"

# DB-level tenancy: one row per physical database, membership in member_space_ids.
#  * acme-prod-db  — shared across A and B (member_space_ids=[A,B]); one row, not two.
#  * finance-db    — held only by A.
#  * detached-db   — first discovered by B (legacy tag still names B) but since removed
#                    from B; now held only by A. Membership, not the stale tag, decides.
#  * a UUID-keyed row so the identifier-shape branch is exercised.
ROWS = [
    {"instance_id": "uuid-shared", "display_name": "acme-prod-db", "account_id": "111111111111",
     "region": "eu-west-1", "secret_arn": "arn:secret:shared",
     "instance_arn": "arn:aws:rds:eu-west-1:111111111111:db:acme-prod-db",
     "member_space_ids": [SPACE_A, SPACE_B], "tags": {"space_id": SPACE_A}},
    {"instance_id": "uuid-f", "display_name": "finance-db", "account_id": "222222222222",
     "region": "us-east-1", "secret_arn": "arn:secret:f",
     "member_space_ids": [SPACE_A], "tags": {"space_id": SPACE_A}},
    {"instance_id": "uuid-detached", "display_name": "detached-db", "account_id": "444444444444",
     "region": "us-east-1", "secret_arn": "arn:secret:d",
     "member_space_ids": [SPACE_A], "tags": {"space_id": SPACE_B}},
    # A genuinely UUID-shaped instance id. The tools that accept a display name are also
    # handed an instance id, so the lookup has to recognise one.
    {"instance_id": "d46faa2b-b7cd-4009-9fe5-8b59e6c19c07", "display_name": "uuid-keyed-db",
     "account_id": "333333333333", "region": "eu-west-1", "secret_arn": "arn:secret:u",
     "member_space_ids": [SPACE_A], "tags": {"space_id": SPACE_A}},
]


class FakeTable:
    def __init__(self, name):
        self.name = name

    def get_item(self, Key=None, **kw):
        iid = (Key or {}).get("instance_id")
        for r in ROWS:
            if r["instance_id"] == iid:
                return {"Item": r}
        return {}

    def scan(self, **kw):
        vals = kw.get("ExpressionAttributeValues", {})
        names = kw.get("ExpressionAttributeNames", {})
        field = next(iter(names.values()), "")
        want = vals.get(":val")
        return {"Items": [r for r in ROWS if r.get(field) == want]}


class FakeResource:
    def Table(self, name):
        return FakeTable(name)


class FakeSession:
    def __init__(self, **kw):
        self.kw = kw


_fake_boto3 = types.ModuleType("boto3")
_fake_boto3.resource = lambda *a, **k: FakeResource()
_fake_boto3.client = lambda *a, **k: None
_fake_boto3.Session = FakeSession
sys.modules["boto3"] = _fake_boto3

from tools.aws import session_factory as sf  # noqa: E402
import model_resolver as mr  # noqa: E402


def _ctx(user="", space=""):
    mr.CURRENT_USER_ID.set(user)
    mr.CURRENT_SPACE_ID.set(space)
    sf._db_record_cache.clear()
    sf._space_cache.clear()


def _expect_refused(**kw):
    try:
        sf._lookup_db_record(**kw)
    except PermissionError:
        return
    raise AssertionError(f"expected PermissionError for {kw}")


def test_resolves_a_database_in_the_callers_space():
    _ctx(user="u1", space=SPACE_A)
    rec = sf._lookup_db_record(identifier="finance-db")
    assert rec["instance_id"] == "uuid-f", rec


def test_refuses_a_database_in_another_space():
    _ctx(user="u1", space=SPACE_C)
    _expect_refused(identifier="finance-db")


def test_refuses_when_space_unresolved_in_agent_ctx():
    _ctx(user="u1", space="")
    _expect_refused(identifier="finance-db")


def test_noop_outside_agent_context():
    # Batch / Tier-1 / REST: no CURRENT_USER_ID. Must resolve as before.
    _ctx(user="", space="")
    rec = sf._lookup_db_record(identifier="finance-db")
    assert rec["instance_id"] == "uuid-f"


def test_shared_database_resolves_for_every_member_space():
    # DB-level tenancy: ONE row, member_space_ids=[A,B]. BOTH members resolve it.
    # The earlier agent admitted only the space named in the frozen tags.space_id.
    _ctx(user="u1", space=SPACE_A)
    assert sf._lookup_db_record(identifier="acme-prod-db")["instance_id"] == "uuid-shared"
    _ctx(user="u2", space=SPACE_B)
    assert sf._lookup_db_record(identifier="acme-prod-db")["instance_id"] == "uuid-shared"


def test_a_non_member_space_cannot_reach_the_shared_database():
    _ctx(user="u1", space=SPACE_C)
    _expect_refused(identifier="acme-prod-db")


def test_detached_space_is_refused_even_though_legacy_tag_still_names_it():
    # , removal revokes on the agent path: detached-db's frozen tags.space_id is
    # still SPACE_B, but B was removed from member_space_ids, so B must be refused.
    _ctx(user="u1", space=SPACE_B)
    _expect_refused(identifier="detached-db")


def test_remaining_member_still_resolves_the_detached_db():
    _ctx(user="u1", space=SPACE_A)
    assert sf._lookup_db_record(identifier="detached-db")["instance_id"] == "uuid-detached"


def test_secret_arn_lookup_is_scoped_too():
    _ctx(user="u1", space=SPACE_C)
    _expect_refused(secret_arn="arn:secret:f")


def test_instance_id_lookup_is_scoped_too():
    _ctx(user="u1", space=SPACE_C)
    _expect_refused(instance_id="uuid-f")


def test_unknown_database_is_refused_the_same_way():
    # Must not distinguish "does not exist" from "not yours".
    _ctx(user="u1", space=SPACE_A)
    _expect_refused(identifier="no-such-db")


def test_cache_does_not_leak_across_spaces():
    # A member (A) resolves the shared DB and populates the cache. A non-member (C) must
    # still be refused — the cache is keyed by space, so it never serves A's row to C.
    _ctx(user="u1", space=SPACE_A)
    assert sf._lookup_db_record(identifier="acme-prod-db")["instance_id"] == "uuid-shared"
    mr.CURRENT_USER_ID.set("u3")
    mr.CURRENT_SPACE_ID.set(SPACE_C)          # deliberately NOT clearing the cache
    _expect_refused(identifier="acme-prod-db")


def test_get_session_refuses_cross_space():
    _ctx(user="u1", space=SPACE_C)
    sf._get_agent_account_id = lambda: "999999999999"
    try:
        sf.get_session(identifier="finance-db")
    except PermissionError:
        return
    raise AssertionError("get_session must refuse a database outside the caller's space")


def test_get_session_still_works_in_space():
    _ctx(user="u1", space=SPACE_A)
    sf._get_agent_account_id = lambda: "222222222222"   # same account as finance-db
    session = sf.get_session(identifier="finance-db")
    assert session.kw.get("region_name") == "us-east-1", session.kw


UUID_ID = "d46faa2b-b7cd-4009-9fe5-8b59e6c19c07"


def test_uuid_shaped_identifier_resolves_as_an_instance_id():
    """The tools take an `identifier` documented as a display name, and are handed both.

    A UUID scanned against `display_name` matches nothing, which the space check then has
    to answer — and a target that resolved to nothing is indistinguishable from one in
    another space. Recognising the shape keeps a legitimate request from being refused.
    """
    _ctx(user="u1", space=SPACE_A)
    record = sf._lookup_db_record(identifier=UUID_ID)
    assert record.get("display_name") == "uuid-keyed-db", record


def test_uuid_shaped_identifier_is_still_space_scoped():
    """Recognising the shape must not become a way around the space check."""
    _ctx(user="u2", space=SPACE_B)
    _expect_refused(identifier=UUID_ID)


def test_a_display_name_is_never_read_as_an_instance_id():
    """Only the UUID shape reroutes. An engine label or a real name stays a name."""
    _ctx(user="u1", space=SPACE_A)
    assert sf._lookup_db_record(identifier="finance-db").get("instance_id") == "uuid-f"
    _expect_refused(identifier="rds-postgresql")


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
                passed += 1
            except AssertionError as e:
                print(f"  FAIL {name}: {e}")
                failed += 1
            except Exception as e:
                print(f"  ERR  {name}: {type(e).__name__}: {e}")
                failed += 1
    print(f"pass={passed} fail={failed}")
    sys.exit(1 if failed else 0)
