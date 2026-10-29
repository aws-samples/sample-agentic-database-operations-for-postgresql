# SPDX-License-Identifier: Apache-2.0

"""Re-running discovery reflects the current selection instead of accumulating it.

Discovery adds what it finds. Without a matching removal, narrowing the region or account
selection and scanning again leaves the earlier rows in place, so the list grows into a
union of every region ever scanned and an operator cannot tell which entries the current
configuration actually covers.

Removal has to be conservative in two specific ways, and both are the point of this file:

  * an account the scan could not assume into returns no results, which is
    indistinguishable from "that account is empty" unless the reach of the scan is taken
    into account — otherwise one failed assumption deletes every row in that account;
  * a configured database carries credentials and an operator's setup, so a narrowed
    selection must report it rather than discard it silently.

`_rows_this_scan_no_longer_sees` is a pure function over rows and scan results, so this
runs with no AWS, no FastAPI and no import of app.py.

Run:  python3 backend/tests/test_discovery_retires_stale_rows.py   (or via pytest)
"""
import os
import re
import sys
import textwrap
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")


def _load_helper():
    """Extract the helper from app.py and exec it alone.

    app.py builds a FastAPI application and imports strands, boto3 and the whole tool
    tree at module scope, none of which this behaviour depends on.
    """
    with open(APP, "r", encoding="utf-8") as f:
        src = f.read()
    m = re.search(r"\ndef _rows_this_scan_no_longer_sees\(.*?\n    return retire, kept\n",
                  src, re.S)
    assert m, "could not find _rows_this_scan_no_longer_sees in app.py"
    ns = types.SimpleNamespace()
    scope = {}
    exec(textwrap.dedent(m.group(0)), scope)
    ns.fn = scope["_rows_this_scan_no_longer_sees"]
    return ns.fn


retire_for = _load_helper()

ACC = "111111111111"
OTHER_ACC = "222222222222"


def row(name, region, status="discovered", account=ACC, arn=""):
    return {
        "instance_id": f"id-{name}-{region}",
        "display_name": name,
        "region": region,
        "account_id": account,
        "status": status,
        "tags": {"space_id": "space-a", "arn": arn},
    }


def found(name, region, arn=""):
    return {"identifier": name, "region": region, "cluster_arn": arn}


def test_row_still_present_is_kept():
    retire, kept = retire_for([row("db1", "us-east-1")], [found("db1", "us-east-1")],
                              [ACC], ["us-east-1"])
    assert retire == [] and kept == []


def test_row_in_a_deselected_region_is_retired():
    """Drop a region, rescan: rows in the deselected region should not linger."""
    rows = [row("db1", "us-east-1"), row("db2", "eu-west-1")]
    retire, kept = retire_for(rows, [found("db1", "us-east-1")], [ACC], ["us-east-1"])
    assert [r["display_name"] for r in retire] == [], \
        "eu-west-1 was not scanned, so its rows carry no evidence either way"
    retire, kept = retire_for(rows, [found("db1", "us-east-1")],
                              [ACC], ["us-east-1", "eu-west-1"])
    assert [r["display_name"] for r in retire] == ["db2"], retire


def test_a_region_outside_the_scan_is_left_alone():
    """Rows the scan never looked at must survive: absence of evidence is not evidence."""
    rows = [row("db1", "us-east-1"), row("db2", "ap-south-1")]
    retire, kept = retire_for(rows, [found("db1", "us-east-1")], [ACC], ["us-east-1"])
    assert retire == [], retire


def test_an_account_outside_the_scan_is_left_alone():
    """A failed assumption yields no results; it must not read as an empty account."""
    rows = [row("db1", "us-east-1"), row("db2", "us-east-1", account=OTHER_ACC)]
    retire, kept = retire_for(rows, [found("db1", "us-east-1")], [ACC], ["us-east-1"])
    assert [r["display_name"] for r in retire] == [], retire
    # Once that account IS in reach and still returns nothing, its row is retired.
    retire, kept = retire_for(rows, [found("db1", "us-east-1")],
                              [ACC, OTHER_ACC], ["us-east-1"])
    assert [r["display_name"] for r in retire] == ["db2"], retire


def test_a_configured_database_is_never_retired():
    rows = [row("configured-db", "eu-west-1", status="configured")]
    retire, kept = retire_for(rows, [], [ACC], ["eu-west-1"])
    assert retire == [], "a configured database must survive a narrowed selection"
    assert kept == ["configured-db"], kept


def test_config_failed_is_also_protected():
    """Past the discovered stage means an operator has touched it, however it ended."""
    rows = [row("half-done", "eu-west-1", status="config_failed")]
    retire, kept = retire_for(rows, [], [ACC], ["eu-west-1"])
    assert retire == [] and kept == ["half-done"]


def test_a_row_with_no_status_counts_as_discovered():
    rows = [{"instance_id": "x", "display_name": "legacy", "region": "eu-west-1",
             "account_id": ACC, "tags": {}}]
    retire, kept = retire_for(rows, [], [ACC], ["eu-west-1"])
    assert [r["display_name"] for r in retire] == ["legacy"], retire


def test_arn_matches_even_when_the_name_changed():
    """A renamed cluster is the same database, so it is matched by ARN, not by name."""
    arn = "arn:aws:rds:eu-west-1:111111111111:cluster:db1"
    rows = [row("old-name", "eu-west-1", arn=arn)]
    retire, kept = retire_for(rows, [found("new-name", "eu-west-1", arn=arn)],
                              [ACC], ["eu-west-1"])
    assert retire == [], "matching by ARN should keep a renamed cluster"


def test_a_blank_arn_does_not_match_everything():
    """Rows predating ARN capture fall back to name+region rather than matching all."""
    rows = [row("db1", "eu-west-1", arn=""), row("db2", "eu-west-1", arn="")]
    retire, kept = retire_for(rows, [found("db1", "eu-west-1", arn="")],
                              [ACC], ["eu-west-1"])
    assert [r["display_name"] for r in retire] == ["db2"], retire


def test_same_name_in_two_regions_is_distinguished():
    rows = [row("shared", "us-east-1"), row("shared", "eu-west-1")]
    retire, kept = retire_for(rows, [found("shared", "us-east-1")],
                              [ACC], ["us-east-1", "eu-west-1"])
    assert [(r["display_name"], r["region"]) for r in retire] == [("shared", "eu-west-1")], retire


# ── per-pair, per-kind completion gates retirement (#10) ──────────────────────

CLUSTER_ARN = "arn:aws:rds:us-east-1:111111111111:cluster:c1"
DB_ARN = "arn:aws:rds:us-east-1:111111111111:db:i1"


def test_a_pair_whose_scan_failed_entirely_retires_nothing():
    # The region is "scanned" (requested) but has NO completion entry: a transient failure
    # mid-enumeration. Its rows must survive.
    rows = [row("gone-cluster", "us-east-1", arn=CLUSTER_ARN),
            row("gone-inst", "us-east-1", arn=DB_ARN)]
    retire, kept = retire_for(rows, [], [ACC], ["us-east-1"], {})
    assert retire == [], "no completion for the pair means nothing is retired"


def test_cluster_scan_failed_keeps_cluster_rows_but_retires_instances():
    # instances_ok but not clusters_ok: an absent instance is really gone; an absent
    # cluster might just be the failed cluster call.
    rows = [row("gone-cluster", "us-east-1", arn=CLUSTER_ARN),
            row("gone-inst", "us-east-1", arn=DB_ARN)]
    comp = {(ACC, "us-east-1"): {"clusters_ok": False, "instances_ok": True}}
    retire, kept = retire_for(rows, [], [ACC], ["us-east-1"], comp)
    assert [r["display_name"] for r in retire] == ["gone-inst"], retire


def test_instance_scan_failed_keeps_instance_rows_but_retires_clusters():
    rows = [row("gone-cluster", "us-east-1", arn=CLUSTER_ARN),
            row("gone-inst", "us-east-1", arn=DB_ARN)]
    comp = {(ACC, "us-east-1"): {"clusters_ok": True, "instances_ok": False}}
    retire, kept = retire_for(rows, [], [ACC], ["us-east-1"], comp)
    assert [r["display_name"] for r in retire] == ["gone-cluster"], retire


def test_both_ok_retires_both_kinds():
    rows = [row("gone-cluster", "us-east-1", arn=CLUSTER_ARN),
            row("gone-inst", "us-east-1", arn=DB_ARN)]
    comp = {(ACC, "us-east-1"): {"clusters_ok": True, "instances_ok": True}}
    retire, kept = retire_for(rows, [], [ACC], ["us-east-1"], comp)
    assert sorted(r["display_name"] for r in retire) == ["gone-cluster", "gone-inst"], retire


def test_a_legacy_row_with_no_arn_needs_both_kinds_ok():
    rows = [row("legacy", "us-east-1", arn="")]
    partial = {(ACC, "us-east-1"): {"clusters_ok": True, "instances_ok": False}}
    retire, kept = retire_for(rows, [], [ACC], ["us-east-1"], partial)
    assert retire == [], "a kind-unknown row is kept unless both enumerations completed"
    full = {(ACC, "us-east-1"): {"clusters_ok": True, "instances_ok": True}}
    retire, kept = retire_for(rows, [], [ACC], ["us-east-1"], full)
    assert [r["display_name"] for r in retire] == ["legacy"], retire


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
