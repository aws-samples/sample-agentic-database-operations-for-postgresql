# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Discovery de-dup cannot join a space to another tenant's database.

`POST /api/spaces/{id}/discover` attaches the caller's space to an existing physical-DB
record when it re-discovers one, instead of minting a duplicate. The match MUST NOT be a
heuristic that grants an authorization fact across tenants:

  * ARN is the only authoritative key. It is globally unique, so an ARN match can never
    span two accounts.
  * RDS identifiers are unique PER ACCOUNT, not globally. So the name fallback is
    account-scoped (account_id, display_name, region) and is used only when the discovered
    resource has no ARN of its own, and only against existing records that themselves carry
    no ARN. A `prod-postgres` in account B can therefore never attach to account A's record.

`_index_existing_dbs` / `_index_one_db` / `_match_discovered_db` are pure functions over
dicts, so this runs with no AWS, no FastAPI and no import of app.py's module scope.

Run:  python3 backend/tests/test_discovery_tenant_dedup.py   (or via pytest)
"""
import os
import re
import sys
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")


def _load_helpers():
    with open(APP, "r", encoding="utf-8") as f:
        src = f.read()
    m = re.search(
        r"\ndef _index_one_db\(.*?\n    return by_name\.get\(\(account_id, identifier, region\)\)\n",
        src, re.S)
    assert m, "could not find the discovery de-dup helpers in app.py"
    scope = {}
    exec(textwrap.dedent(m.group(0)), scope)
    return scope["_index_existing_dbs"], scope["_match_discovered_db"]


index_existing, match_db = _load_helpers()

ACC_A = "111111111111"
ACC_B = "222222222222"
REGION = "us-east-1"
ARN_A = f"arn:aws:rds:{REGION}:{ACC_A}:cluster:prod-postgres"


def rec(name, region=REGION, account=ACC_A, arn="", iid=None):
    return {"instance_id": iid or f"id-{name}-{region}-{account}",
            "display_name": name, "region": region, "account_id": account,
            "tags": {"arn": arn}}


def test_arn_match_attaches():
    by_arn, by_name = index_existing([rec("prod-postgres", arn=ARN_A)])
    got = match_db(by_arn, by_name, ARN_A, ACC_A, "prod-postgres", REGION)
    assert got and got["account_id"] == ACC_A, "an ARN match must attach"


def test_cross_account_name_collision_with_arn_does_not_match():
    """The attack: account B names an instance like account A's and discovers it."""
    by_arn, by_name = index_existing([rec("prod-postgres", account=ACC_A, arn=ARN_A)])
    arn_b = f"arn:aws:rds:{REGION}:{ACC_B}:cluster:prod-postgres"
    got = match_db(by_arn, by_name, arn_b, ACC_B, "prod-postgres", REGION)
    assert got is None, "a different account's ARN must never match account A's record"


def test_cross_account_name_collision_no_arn_does_not_match():
    """Even with no ARN on the discovered side, a different account cannot name-match."""
    by_arn, by_name = index_existing([rec("prod-postgres", account=ACC_A, arn="")])
    got = match_db(by_arn, by_name, "", ACC_B, "prod-postgres", REGION)
    assert got is None, "name fallback is account-scoped; account B must miss account A"


def test_same_account_arnless_name_fallback_attaches():
    """Legacy / instance-vs-cluster-ARN edge WITHIN one account still de-dups by name."""
    by_arn, by_name = index_existing([rec("prod-postgres", account=ACC_A, arn="")])
    got = match_db(by_arn, by_name, "", ACC_A, "prod-postgres", REGION)
    assert got and got["account_id"] == ACC_A, "same-account ARN-less name match must attach"


def test_arn_bearing_record_not_matched_by_name():
    """A record that HAS an ARN is only reachable by ARN, never by the name fallback."""
    by_arn, by_name = index_existing([rec("prod-postgres", account=ACC_A, arn=ARN_A)])
    got = match_db(by_arn, by_name, "", ACC_A, "prod-postgres", REGION)
    assert got is None, "ARN-bearing records live in the ARN index only, not the name map"


def test_discovered_with_own_arn_never_falls_back_to_name():
    """If the discovered resource has an ARN that misses, create new rather than name-join."""
    by_arn, by_name = index_existing([rec("prod-postgres", account=ACC_A, arn="")])
    got = match_db(by_arn, by_name, "arn:aws:rds:us-east-1:111111111111:cluster:other",
                   ACC_A, "prod-postgres", REGION)
    assert got is None, "a discovered ARN that misses must not attach by name"


def test_same_name_two_accounts_indexed_separately():
    by_arn, by_name = index_existing([
        rec("shared", account=ACC_A, arn=""),
        rec("shared", account=ACC_B, arn=""),
    ])
    a = match_db(by_arn, by_name, "", ACC_A, "shared", REGION)
    b = match_db(by_arn, by_name, "", ACC_B, "shared", REGION)
    assert a["account_id"] == ACC_A and b["account_id"] == ACC_B, "each account keeps its own row"


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
