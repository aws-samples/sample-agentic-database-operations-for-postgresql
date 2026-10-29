# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Re-discovery clears a stale onboard failure off a record no space owns.

The instance table is shared per physical database: one row per DB, and spaces attach to
it as members (onboarded) or candidates (merely discovered). A row that NO space has
onboarded can still carry a terminal ``config_failed`` plus the ``config_error`` /
``config_log`` from an earlier onboard attempt by a different or now-deleted space. Because
``list_space_candidates`` projects ``status`` and ``config_error`` straight off the row,
that stale failure would otherwise appear on a brand-new space's Discovery view (repro:
delete every space, create a fresh one, run discovery, and a previously-failed database
shows up already "Configuration failed").

``_stale_candidate_reset`` is the decision discovery applies when it re-proposes an
existing row. This pins its guardrails, which are the whole point:

  * only a row with NO members is reset — a member row is live and owned by another space,
    whose ``config_failed`` (e.g. a failed reconfigure) is that space's real state;
  * only the ``config_failed`` state is reset — an in-flight ``configuring`` or a teardown
    (``deleting`` / ``delete_failed``) is left untouched;
  * only the display fields change — ``network_method`` and everything else survive, so a
    later dismiss can still tear down a path a failed first-onboard built.

``_stale_candidate_reset`` depends only on ``record_member_space_ids`` (a leaf tenancy
predicate), so this runs with no AWS, no FastAPI and no import of app.py's module scope.

Run:  python3 backend/tests/test_discovery_resets_stale_failure.py   (or via pytest)
"""
import os
import re
import sys
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")
sys.path.insert(0, ROOT)
from tools.aws.tenancy import record_member_space_ids  # noqa: E402


def _load_helper():
    with open(APP, "r", encoding="utf-8") as f:
        src = f.read()
    m = re.search(
        r"\ndef _stale_candidate_reset\(.*?\n    return \{\"status\": \"discovered\", "
        r"\"config_error\": \"\", \"config_log\": \[\]\}\n",
        src, re.S)
    assert m, "could not find _stale_candidate_reset in app.py"
    # The function closes over _instance_member_spaces, which in app.py is exactly
    # record_member_space_ids(inst); provide that real predicate under the same name so the
    # extracted copy resolves the shipped membership semantics (a 'discovered' row has no
    # members).
    scope = {"_instance_member_spaces": record_member_space_ids}
    exec(textwrap.dedent(m.group(0)), scope)
    return scope["_stale_candidate_reset"]


reset_for = _load_helper()


def rec(status, members=None, config_error="old timeout", config_log=None,
        network_method="peering"):
    r = {
        "instance_id": "id-1",
        "display_name": "eu-rds-database-1",
        "status": status,
        "config_error": config_error,
        "config_log": config_log if config_log is not None else ["step 1", "step 2"],
        "network_method": network_method,
    }
    if members is not None:
        r["member_space_ids"] = list(members)
    return r


def test_no_member_config_failed_is_reset():
    r = reset_for(rec("config_failed"))
    assert r == {"status": "discovered", "config_error": "", "config_log": []}, r


def test_reset_touches_only_the_display_fields():
    """The reset dict must not carry network_method (or anything else): discovery writes
    exactly these three keys, so a path a failed first-onboard built survives for teardown."""
    r = reset_for(rec("config_failed"))
    assert set(r.keys()) == {"status", "config_error", "config_log"}, r
    assert "network_method" not in r


def test_member_config_failed_is_left_untouched():
    """A live, owned record (a member's failed reconfigure) is that space's real state."""
    assert reset_for(rec("config_failed", members=["space-a"])) is None


def test_empty_member_list_still_counts_as_no_members():
    assert reset_for(rec("config_failed", members=[])) == {
        "status": "discovered", "config_error": "", "config_log": []}


def test_deleting_is_not_reset():
    """A teardown in progress must not be disturbed by discovery."""
    assert reset_for(rec("deleting")) is None


def test_delete_failed_is_not_reset():
    assert reset_for(rec("delete_failed")) is None


def test_configuring_is_not_reset():
    """An onboard in flight keeps its state."""
    assert reset_for(rec("configuring")) is None


def test_already_discovered_is_a_noop():
    assert reset_for(rec("discovered")) is None


def test_configured_is_not_reset():
    assert reset_for(rec("configured", members=["space-a"])) is None


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
