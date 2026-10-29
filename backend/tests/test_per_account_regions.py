# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Per-account Region overrides, and the one place they could delete data.

A space's `regions` is the default; `account_regions` ({account_id: [regions]}) lets each
account be scanned in its own Regions instead of the accounts x regions cross-product.

The dangerous consumer is the post-scan retirement pass: it may only retire rows in
(account, region) pairs the scan actually visited. With per-account Regions the old
cross-product would mark a pair the scan never visited as scanned, and every row there as
"no longer seen" - retiring live databases. These tests pin that the exact scanned pairs
are used, and that discover_all hands each account ITS Regions.

Hermetic: functions are extracted from app.py / discovery_client.py by source. No AWS.

Run:  python3 backend/tests/test_per_account_regions.py
"""
import os
import pathlib
import re
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
APP_SRC = (ROOT / "app.py").read_text(encoding="utf-8")
DISC_SRC = (ROOT / "tools/aws/discovery_client.py").read_text(encoding="utf-8")
passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def _extract(src, name):
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^(?:@|def |class |[A-Z_]+ = )|\Z)", src, re.S | re.M)
    assert m, f"{name} not found"
    return m.group(0)


ns = {"os": os}
for fn in ("_regions_for_account", "_regions_by_account", "_rows_this_scan_no_longer_sees"):
    exec(_extract(APP_SRC, fn), ns)  # noqa: S102 - pinning shipped source
regions_for = ns["_regions_for_account"]
regions_by = ns["_regions_by_account"]
retire_fn = ns["_rows_this_scan_no_longer_sees"]

print("an account inherits the space default unless it has its own override")
space = {"regions": ["us-east-1", "eu-west-1"], "account_regions": {"222": ["eu-west-1"]}}
check(regions_for(space, "111") == ["us-east-1", "eu-west-1"], "no override -> space default")
check(regions_for(space, "222") == ["eu-west-1"], "override -> its own list")
check(regions_for({"regions": ["us-east-1"], "account_regions": {"222": []}}, "222") == [],
      "an EXPLICIT empty override scans nowhere (the single-list console makes empty intentional)")
check(regions_for({"regions": ["us-east-1"], "account_regions": {"333": ["eu-west-1"]}}, "222") == ["us-east-1"],
      "an ABSENT account still inherits the space default (legacy spaces and newly bound accounts)")
check(regions_for({"regions": ["us-east-1", "us-east-1", "eu-west-1"]}, "111") == ["us-east-1", "eu-west-1"],
      "duplicates are removed, order kept")
check(regions_for({}, "111") == [os.getenv("AWS_REGION", "us-east-1")], "no regions anywhere -> deployment Region")
check(regions_for({"regions": ["us-east-1"], "account_regions": "garbage"}, "111") == ["us-east-1"],
      "a malformed account_regions is ignored, not fatal")
check(regions_by(space, ["111", "222", ""]) == {"111": ["us-east-1", "eu-west-1"], "222": ["eu-west-1"]},
      "_regions_by_account maps every (non-empty) account")

print("retirement considers only the pairs the scan actually visited")
# Account 111 scanned in us-east-1 + eu-west-1; account 222 scanned ONLY in eu-west-1.
rows = [
    {"display_name": "a-use1", "region": "us-east-1", "status": "discovered", "tags": {"account_id": "111", "arn": "arn:aws:rds:us-east-1:111:db:a"}},
    {"display_name": "b-euw1", "region": "eu-west-1", "status": "discovered", "tags": {"account_id": "222", "arn": "arn:aws:rds:eu-west-1:222:db:b"}},
    # The live database the cross-product would have wrongly retired: account 222 in
    # us-east-1 was NOT scanned this run, so its absence from the results means nothing.
    {"display_name": "c-use1-LIVE", "region": "us-east-1", "status": "discovered", "tags": {"account_id": "222", "arn": "arn:aws:rds:us-east-1:222:db:c"}},
]
found = []   # the scan saw nothing (everything scanned is gone)
pairs = {("111", "us-east-1"), ("111", "eu-west-1"), ("222", "eu-west-1")}
completion = {p: {"clusters_ok": True, "instances_ok": True} for p in pairs}

retire, kept = retire_fn(rows, found, ["111", "222"], ["us-east-1", "eu-west-1"],
                         completion=completion, scanned_pairs=pairs)
names = sorted(r["display_name"] for r in retire)
check(names == ["a-use1", "b-euw1"], f"only rows in SCANNED pairs are retired ({names})")
check(all(r["display_name"] != "c-use1-LIVE" for r in retire),
      "a row in an unscanned (account, region) pair is NEVER retired")

print("without scanned_pairs the old cross-product behaviour is unchanged")
retire_old, _ = retire_fn(rows, found, ["111", "222"], ["us-east-1", "eu-west-1"],
                          completion={(a, r): {"clusters_ok": True, "instances_ok": True}
                                      for a in ("111", "222") for r in ("us-east-1", "eu-west-1")})
check(sorted(r["display_name"] for r in retire_old) == ["a-use1", "b-euw1", "c-use1-LIVE"],
      "legacy callers (same Regions for every account) retire across the cross-product as before")

print("discover_all scans each account in ITS Regions")
# Build a DbDiscovery with the real discover_all but a recording discover_instances_in_account.
disc_ns = {}
# Stub module deps the client imports at top.
for mod in ("boto3", "botocore", "botocore.exceptions"):
    if mod not in sys.modules:
        sys.modules[mod] = types.ModuleType(mod)
sys.modules["botocore.exceptions"].ClientError = Exception
m = re.search(r"    def discover_all\(self.*?return all_instances, scan_log, completion\n", DISC_SRC, re.S)
assert m, "discover_all not found"
body = "class D:\n" + m.group(0) + "\n"
exec(body, {"Optional": __import__("typing").Optional, "logger": types.SimpleNamespace(warning=lambda *a, **k: None)}, disc_ns)  # noqa: S102
D = disc_ns["D"]
calls = []
d = D()
d.discover_instances_in_account = lambda aid, regions, **k: (calls.append((aid, list(regions))), [])[1]
d.sts_client = types.SimpleNamespace(get_caller_identity=lambda: {"Account": "111"})
d.discover_all(regions=["us-east-1", "eu-west-1"], account_ids=["111", "222", "333"],
               regions_by_account={"222": ["eu-west-1"]})
by = dict(calls)
check(by["111"] == ["us-east-1", "eu-west-1"], "account without override scanned in the default Regions")
check(by["222"] == ["eu-west-1"], "account with override scanned ONLY in its Regions")
check(by["333"] == ["us-east-1", "eu-west-1"], "another account without override -> default")
calls.clear()
d.discover_all(regions=["us-east-1"], account_ids=["111"])
check(dict(calls)["111"] == ["us-east-1"], "no regions_by_account -> unchanged behaviour")

# An account explicitly mapped to [] (e.g. the deployment account with no databases) is
# skipped, not scanned in the union — the whole point of making empty mean "nowhere".
calls.clear()
d.discover_all(regions=["us-east-1", "eu-west-1"], account_ids=["111", "222"],
               regions_by_account={"111": [], "222": ["eu-west-1"]})
by = dict(calls)
check("111" not in by, "an account explicitly mapped to [] is skipped, not scanned in the union")
check(by.get("222") == ["eu-west-1"], "the account with Regions is still scanned")

print("the space registry allows account_regions to be written")
reg_src = (ROOT / "tools/aws/spaces_client.py").read_text(encoding="utf-8")
# The allowlist spans lines and its comments contain brackets, so bound it by the
# statement that follows it rather than by the first closing bracket.
update_space_src = reg_src.split("def update_space", 1)[1]
allow = update_space_src.split("allowed_fields = [", 1)[1].split("\n        ]", 1)[0]
check('"account_regions"' in allow, "account_regions is in update_space's allowlist (else writes are silently dropped)")
check('"account_regions"' in _extract(APP_SRC, "update_space") or 'account_regions' in APP_SRC.split("_DETAIL_FIELDS = (")[1].split(")")[0],
      "account_regions changes are audited as a space_update")



# ── Scoped discovery: the account scope can only narrow, never reach outside the space ──
print("discovery scope narrows to the space's accounts and rejects outsiders")


class _HTTPExc(Exception):
    def __init__(self, status_code, detail=""):
        self.status_code, self.detail = status_code, detail


scope_ns = {"HTTPException": _HTTPExc, "Optional": __import__("typing").Optional}
exec(_extract(APP_SRC, "_discovery_scope"), scope_ns)  # noqa: S102
scope = scope_ns["_discovery_scope"]
sp = {"primary_account_id": "111", "secondary_account_ids": ["222", "333"]}
check(scope(sp, None) == ["111", "222", "333"], "no scope -> every account of the space")
check(scope(sp, []) == ["111", "222", "333"], "empty scope -> every account")
check(scope(sp, ["333"]) == ["333"], "a subset scopes to just those accounts")
check(scope(sp, ["333", "111", "333"]) == ["111", "333"], "order follows the space, duplicates dropped")
try:
    scope(sp, ["999"])
    check(False, "an account outside the space must be rejected")
except _HTTPExc as e:
    check(e.status_code == 400 and "999" in e.detail, "an account outside the space is a 400 naming it")

print("a scoped run cannot retire rows in the accounts it skipped")
# Scope = account 111 only. Account 222's rows are in UNSCANNED pairs -> never retired,
# even though the scan 'found' nothing. This is the property that makes scoping safe.
rows2 = [
    {"display_name": "gone-111", "region": "us-east-1", "status": "discovered", "tags": {"account_id": "111", "arn": "arn:aws:rds:us-east-1:111:db:g"}},
    {"display_name": "live-222", "region": "us-east-1", "status": "discovered", "tags": {"account_id": "222", "arn": "arn:aws:rds:us-east-1:222:db:l"}},
]
pairs2 = {("111", "us-east-1")}
retire2, _ = retire_fn(rows2, [], ["111"], ["us-east-1"],
                       completion={p: {"clusters_ok": True, "instances_ok": True} for p in pairs2},
                       scanned_pairs=pairs2)
check([r["display_name"] for r in retire2] == ["gone-111"], "only the scanned account's vanished row is retired; the skipped account's row is kept")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
