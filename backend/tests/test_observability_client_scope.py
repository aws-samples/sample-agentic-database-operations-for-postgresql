# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The observability client factories must fail closed on a falsy identifier.

The agent's read tools (CloudWatch Logs, CloudWatch metrics, Performance Insights,
CloudTrail) reach AWS through a per-database client factory. Each factory used to route
through the space-scoped, fail-closed resolver (session_factory.get_session) only when the
identifier was truthy, and otherwise returned an account-level boto3 client. So a tool call
with db_instance_id="" skipped the space check entirely: query_db_logs would then run a
model-authored Logs Insights query across every /aws/rds/* log group in the DEPLOYMENT
account, which can hold other spaces' PostgreSQL logs.

The fix: a falsy identifier is refused in every factory, so the only way to obtain a client
is to resolve a specific database through get_session (which enforces the caller's space and
fail-closes on an empty/cross-space target). This pins that behaviour:

  * empty string and None are refused (no client, no account-wide read),
  * a real identifier goes through get_session and nowhere else.

Hermetic: boto3 + strands stubbed at import; get_session monkeypatched (no AWS).

Run:  python3 backend/tests/test_observability_client_scope.py   (or via pytest)
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# The client modules import boto3/strands at load; the shared session_factory import is
# done lazily inside each factory, so stub what is needed for import and patch the rest.
# session_factory annotates a return type as boto3.Session at def time, so the stub needs it.
_boto3 = types.ModuleType("boto3")
_boto3.Session = type("Session", (), {})
_boto3.client = lambda *a, **k: None
_boto3.resource = lambda *a, **k: None
sys.modules.setdefault("boto3", _boto3)

# strands.tool is a decorator that must return the wrapped function unchanged.
_strands = types.ModuleType("strands")
_strands.tool = lambda f=None, **kw: (f if f is not None else (lambda g: g))
sys.modules.setdefault("strands", _strands)

from tools.aws import cloudwatch_logs_client as logs  # noqa: E402
from tools.aws import cloudwatch_client as cw  # noqa: E402
from tools.aws import performance_insights_client as pi  # noqa: E402
from tools.aws import cloudtrail_client as ct  # noqa: E402


class _FakeSession:
    """Stands in for the session get_session returns; records the identifier it was made for."""
    def __init__(self, identifier):
        self.identifier = identifier

    def client(self, name):
        return {"service": name, "identifier": self.identifier}


_resolved = []


def _fake_get_session(identifier=None, **kw):
    # Mirrors the real contract: a client is only ever minted for a resolved identifier.
    _resolved.append(identifier)
    return _FakeSession(identifier)


def _fake_get_session_context(session):
    return {"type": "test"}


# cloudwatch_client / performance_insights_client import get_session_context too.
import tools.aws.session_factory as sf  # noqa: E402
sf.get_session = _fake_get_session
sf.get_session_context = _fake_get_session_context


# (factory, name) for the four client factories under test.
FACTORIES = [
    (logs._get_logs_client, "logs"),
    (cw._get_cw_client, "cloudwatch"),
    (pi._get_pi_client, "pi"),
    (ct._get_ct_client, "cloudtrail"),
]


def test_empty_identifier_is_refused():
    for factory, name in FACTORIES:
        for bad in ("", None):
            try:
                factory(identifier=bad)
                raise AssertionError(f"{name}: expected refusal for identifier={bad!r}")
            except PermissionError:
                pass


def test_real_identifier_goes_through_the_resolver():
    _resolved.clear()
    for factory, name in FACTORIES:
        client = factory(identifier="db-in-my-space")
        assert client["identifier"] == "db-in-my-space", f"{name}: client not scoped to the id"
    # Every factory obtained its client via get_session — none fell back to an account client.
    assert _resolved == ["db-in-my-space"] * len(FACTORIES), _resolved


def test_a_cross_space_refusal_from_the_resolver_propagates():
    # get_session fail-closes with PermissionError for a target outside the caller's space;
    # the factory must not swallow it into an unscoped client.
    def _deny(identifier=None, **kw):
        raise PermissionError("not accessible in the current space")
    orig = sf.get_session
    sf.get_session = _deny
    try:
        for factory, name in FACTORIES:
            try:
                factory(identifier="db-in-another-space")
                raise AssertionError(f"{name}: expected the resolver's refusal to propagate")
            except PermissionError:
                pass
    finally:
        sf.get_session = orig


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
