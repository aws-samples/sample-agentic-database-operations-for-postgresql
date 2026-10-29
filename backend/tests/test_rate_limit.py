# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Per-caller rate limiting for utils.rate_limit and its wiring.

The limiter is a fairness / runaway guard in front of the model-invoking paths, keyed by
the authenticated caller. It must (a) count requests per caller and deny over the limit,
(b) FAIL OPEN on any backend error so it never blocks a legitimate operator, and (c) be
wired into both model paths for every caller — the operator app has a single role, so no
one, super admins included, is exempt. The behaviour is tested against a
stubbed table (no AWS); the wiring is asserted against source.

Run:  python3 backend/tests/test_rate_limit.py   (or via pytest)
"""
import os
import pathlib
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import utils.rate_limit as rl  # noqa: E402


def _no_store_override():
    """Prime the limiter's store cache to "no override" so limit_per_minute() resolves from
    the env/default without touching DynamoDB (keeps these tests hermetic)."""
    rl._limit_cache = {"value": None, "at": time.time()}

REPO = pathlib.Path(__file__).resolve().parents[2]


def _read(rel):
    return (REPO / rel).read_text(encoding="utf-8")


class FakeTable:
    """Stands in for the DynamoDB table; counts per key, or raises to test fail-open."""

    def __init__(self, raises=None):
        self.counts = {}
        self.raises = raises
        self.calls = []

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames,
                    ExpressionAttributeValues, ReturnValues):
        self.calls.append(Key["id"])
        if self.raises:
            raise self.raises
        k = Key["id"]
        self.counts[k] = self.counts.get(k, 0) + 1
        return {"Attributes": {"count": self.counts[k]}}


def _set_limit(v):
    os.environ["RATE_LIMIT_PER_MINUTE"] = str(v)


# ── behaviour ────────────────────────────────────────────────────────────────

def test_requests_under_the_limit_are_allowed():
    rl._table = FakeTable()
    _set_limit(3)
    for _ in range(3):
        allowed, retry = rl.check_rate_limit("user-1")
        assert allowed is True and retry == 0


def test_requests_over_the_limit_are_denied_with_retry():
    rl._table = FakeTable()
    _set_limit(2)
    assert rl.check_rate_limit("u")[0] is True   # 1
    assert rl.check_rate_limit("u")[0] is True   # 2
    allowed, retry = rl.check_rate_limit("u")     # 3 > 2
    assert allowed is False
    assert 1 <= retry <= 60, retry


def test_each_caller_has_its_own_budget():
    rl._table = FakeTable()
    _set_limit(1)
    assert rl.check_rate_limit("a")[0] is True    # a: 1
    assert rl.check_rate_limit("a")[0] is False   # a: 2 > 1
    # a different caller is unaffected
    assert rl.check_rate_limit("b")[0] is True    # b: 1


def test_fails_open_on_any_backend_error():
    rl._table = FakeTable(raises=RuntimeError("dynamo down"))
    _set_limit(1)
    # even well past the limit, an error means allow
    for _ in range(5):
        assert rl.check_rate_limit("u") == (True, 0)


def test_empty_caller_is_allowed_without_touching_the_table():
    fake = FakeTable(raises=RuntimeError("must not be called"))
    rl._table = fake
    assert rl.check_rate_limit("") == (True, 0)
    assert fake.calls == []


def test_limit_default_floor_and_override():
    _no_store_override()
    for bad in ("", "0", "-5", "abc"):
        _set_limit(bad)
        assert rl.limit_per_minute() == 60, bad
    _set_limit(10)
    assert rl.limit_per_minute() == 10
    os.environ.pop("RATE_LIMIT_PER_MINUTE", None)
    assert rl.limit_per_minute() == 60


# ── store-backed override (FU-2: console-tunable ceiling) ─────────────────────

def test_store_override_wins_over_env_and_default():
    # A console-set ceiling in the governance store takes precedence over the env.
    rl._limit_cache = {"value": 5, "at": time.time()}
    _set_limit(999)
    assert rl.limit_per_minute() == 5
    os.environ.pop("RATE_LIMIT_PER_MINUTE", None)


def test_store_unset_falls_back_to_env_then_default():
    _no_store_override()          # store has no override
    _set_limit(25)
    assert rl.limit_per_minute() == 25
    os.environ.pop("RATE_LIMIT_PER_MINUTE", None)
    assert rl.limit_per_minute() == 60


def test_store_read_is_fail_soft():
    # A broken governance store must not raise or change behaviour — _store_limit returns
    # None and resolution falls through to env/default.
    import tools.aws.governance_store as gs
    orig = gs.governance_registry.get_rate_limit
    gs.governance_registry.get_rate_limit = lambda: (_ for _ in ()).throw(RuntimeError("dynamo down"))
    try:
        rl._limit_cache = {"value": None, "at": 0.0}   # force a fresh read
        assert rl._store_limit() is None                # no exception, no override
        _set_limit(30)
        rl._limit_cache = {"value": None, "at": 0.0}
        assert rl.limit_per_minute() == 30
    finally:
        gs.governance_registry.get_rate_limit = orig
        os.environ.pop("RATE_LIMIT_PER_MINUTE", None)


def test_still_fails_open_even_with_a_store_limit():
    # Store sets a tiny ceiling AND the counter table is down: the request must still be
    # allowed (the guard is never a hard boundary).
    rl._limit_cache = {"value": 1, "at": time.time()}
    rl._table = FakeTable(raises=RuntimeError("dynamo down"))
    for _ in range(5):
        assert rl.check_rate_limit("u") == (True, 0)


def test_settings_endpoint_is_super_admin_gated_and_store_backed():
    src = _read("backend/app.py")
    assert '@app.get("/api/settings/rate-limit")' in src
    assert '@app.put("/api/settings/rate-limit")' in src
    assert "governance_registry.put_rate_limit(" in src
    # both handlers sit behind the super-admin gate
    assert src.count('raise HTTPException(status_code=403, detail="Admin access required")') >= 2
    gs = _read("backend/tools/aws/governance_store.py")
    assert "def get_rate_limit(self)" in gs and "def put_rate_limit(self" in gs


# ── wiring ───────────────────────────────────────────────────────────────────

def _route_body(src: str, decorator: str) -> str:
    """The source of one route: from its decorator to the next ``@app.`` decorator."""
    start = src.index(decorator)
    nxt = src.find("\n@app.", start + len(decorator))
    return src[start: nxt if nxt != -1 else len(src)]


def test_both_model_paths_are_gated_for_everyone():
    src = _read("backend/app.py")
    assert "from utils.rate_limit import check_rate_limit" in src
    # invocations (per user_id) + compare-summary (per get_user_id(auth))
    assert src.count("check_rate_limit(") >= 2, "both model-invoking paths must be gated"
    assert "check_rate_limit(get_user_id(auth))" in src, "compare-summary is not gated"
    # Nobody is exempt. The operator app has a single role, so a super admin's turns
    # count against the same per-caller budget as anyone else's: neither model path may
    # consult an admin role before (or instead of) counting the call.
    for decorator in ('@app.post("/invocations")', '@app.post("/api/reports/compare-summary")'):
        body = _route_body(src, decorator)
        assert "check_rate_limit(" in body, f"{decorator} lost its rate limit"
        assert "is_admin(" not in body[: body.index("check_rate_limit(")], \
            f"{decorator} exempts admins from the rate limit"


def test_compare_summary_has_an_explicit_route_and_tighter_throttle():
    api = _read("infrastructure/api.yaml")
    assert "CompareSummaryRoute:" in api
    assert "'POST /api/reports/compare-summary'" in api
    assert "RouteSettings:" in api, "the stage has no per-route throttle map"


def test_rate_limit_table_exists_and_both_roles_can_reach_it():
    storage = _read("infrastructure/storage.yaml")
    assert "RateLimitTable:" in storage
    assert "AttributeName: expires_at" in storage, "the counter table needs a TTL attribute"
    assert "${ResourcePrefix}-rate-limits" in storage
    # per-table IAM: both the ECS task role and the AgentCore runtime role
    assert "${ResourcePrefix}-rate-limits" in _read("infrastructure/api.yaml")
    assert "${ResourcePrefix}-rate-limits" in _read("backend/template.yaml")
    # registered in the resource-name contract
    assert '"rate_limits"' in _read("backend/tools/resource_names.py")


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
