# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""session_factory's space-row cache expires and is bounded.

The agent runtime reads a space's row (owner admin, secondary role names) on every
connection, so ``_lookup_space`` caches it. Role names change in the backend process,
which cannot invalidate this cache, so entries expire after ``_DB_RECORD_CACHE_TTL``
seconds, the same bound the DB record cache uses; and the cache holds at most
``_SPACE_CACHE_MAX`` rows so a long-lived runtime cannot grow it without limit. This
runs the real ``_lookup_space`` against a fake ``boto3.resource`` that counts
``get_item`` calls, with ``time.time`` under the test's control. Hermetic, no AWS.

Run:  python3 backend/tests/test_space_cache_bounds.py
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.aws import session_factory as sf  # noqa: E402

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


class FakeTable:
    def __init__(self, rows):
        self.rows, self.calls, self.fail = rows, [], False

    def get_item(self, Key):
        self.calls.append(Key["space_id"])
        if self.fail:
            raise RuntimeError("throttled")
        row = self.rows.get(Key["space_id"])
        return {"Item": dict(row)} if row else {}


ROWS = {f"s{i}": {"space_id": f"s{i}", "owner_admin": f"admin-{i}"} for i in range(400)}
table = FakeTable(ROWS)
sf.boto3 = types.SimpleNamespace(resource=lambda *a, **k: types.SimpleNamespace(Table=lambda name: table))
clock = {"now": 1_000_000.0}
sf.time = types.SimpleNamespace(time=lambda: clock["now"])
TTL, MAX = sf._DB_RECORD_CACHE_TTL, sf._SPACE_CACHE_MAX


def reset():
    sf._space_cache.clear()
    table.calls.clear()
    table.fail = False


print("== a hit within the TTL makes no call; an expired entry is re-fetched ==")
reset()
check(sf._lookup_space("s1").get("owner_admin") == "admin-1", "first lookup reads the row")
check(table.calls == ["s1"], "one get_item")
clock["now"] += TTL - 1
check(sf._lookup_space("s1").get("owner_admin") == "admin-1", "a lookup inside the TTL answers from the cache")
check(table.calls == ["s1"], "and makes no call")
ROWS["s1"]["owner_admin"] = "admin-1-renamed"
clock["now"] += 2
check(sf._lookup_space("s1").get("owner_admin") == "admin-1-renamed", "after the TTL the changed row is seen")
check(table.calls == ["s1", "s1"], "because the expired entry was re-fetched")
check(sf.external_id_for_space("s1") == "admin-1-renamed", "external_id_for_space follows the fresh row")

print("")
print("== empty and failed lookups are not cached ==")
reset()
check(sf._lookup_space("missing") == {} and sf._lookup_space("missing") == {}, "an unknown space is empty")
check(table.calls == ["missing", "missing"] and "missing" not in sf._space_cache, "and asked again each time")
table.fail = True
check(sf._lookup_space("s2") == {}, "a failed read answers empty")
check("s2" not in sf._space_cache, "and is not cached")
table.fail = False
check(sf._lookup_space("s2").get("owner_admin") == "admin-2" and "s2" in sf._space_cache,
      "the next read succeeds and is cached")
check(sf._lookup_space("") == {} and sf._lookup_space("  ") == {}, "an empty id reads nothing")
check("" not in sf._space_cache and table.calls.count("") == 0, "and is never looked up or cached")

print("")
print(f"== the cache never holds more than {MAX} rows ==")
reset()
peak = 0
for i in range(MAX + 100):
    clock["now"] += 1
    sf._lookup_space(f"s{i}")
    peak = max(peak, len(sf._space_cache))
check(peak == MAX, f"filling it with {MAX + 100} spaces peaks at {MAX}")
check("s0" not in sf._space_cache and f"s{MAX + 99}" in sf._space_cache,
      "the oldest-expiring entries were dropped, the newest kept")
calls = len(table.calls)
sf._lookup_space(f"s{MAX + 99}")
check(len(table.calls) == calls, "a kept entry still answers from the cache")

reset()
for i in range(MAX):
    sf._lookup_space(f"s{i}")
clock["now"] += TTL + 1
sf._lookup_space("s300")
check(len(sf._space_cache) == 1 and "s300" in sf._space_cache,
      "when full, every expired entry is dropped before anything live")

reset()
for i in range(MAX):
    clock["now"] += 1
    sf._lookup_space(f"s{i}")
clock["now"] += TTL - MAX // 2  # the first half has expired, the second half is live
sf._lookup_space("s300")
check(len(sf._space_cache) <= MAX and "s300" in sf._space_cache, "a full cache with some expired rows makes room")
check(all(exp > clock["now"] for _, exp in sf._space_cache.values()), "by dropping only the expired rows")
check(f"s{MAX - 1}" in sf._space_cache, "and keeping the live ones")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
