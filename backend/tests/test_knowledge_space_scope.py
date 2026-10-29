# SPDX-License-Identifier: Apache-2.0

"""Custom knowledge must be composed for the caller's space, never a model-supplied one.

``compose_runbooks`` loads the per-space/user CUSTOM knowledge docs into the agent's
context. The space id it is handed can be model-influenced, so the load must be scoped to
the caller's resolved space. The failure this pins: when a live agent request's space does
not resolve — e.g. an investigation opened with a bad id — the loader must NOT fall back to
the model-supplied space and pull another tenant's documents into context.

Three properties:
  * a confirmed caller space wins, overriding a divergent model-supplied ``space_id``;
  * an agent request (a user is set) with an UNRESOLVED space composes nothing;
  * a path with no agent context (batch / Tier-1 / unit) trusts the argument, so the
    deep-scan job still loads a space's knowledge.

The cascade itself (``resolve_knowledge``: global -> admin-tier -> space -> user, with
per-layer disable) is a separate function; it is spied here, not exercised. This test is
about WHICH space — or none — ``compose_runbooks`` composes for.

Hermetic: boto3 stubbed, ``resolve_knowledge`` and the built-in loaders spied. No AWS, no
files.

Run:  python3 backend/tests/test_knowledge_space_scope.py   (or via pytest)
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_fake_boto3 = types.ModuleType("boto3")
_fake_boto3.resource = lambda *a, **k: None
_fake_boto3.client = lambda *a, **k: None
sys.modules["boto3"] = _fake_boto3

from tools import runbook_loader as rl  # noqa: E402
import model_resolver as mr  # noqa: E402

SPACE_A = "aaaaaaaa-space-a"
SPACE_B = "bbbbbbbb-space-b"

_resolve_calls = []


def _install_spies():
    """Spy resolve_knowledge (records the space it is composed for) and neutralise the
    built-in/​catalog loaders so the test exercises only the space-gating logic."""
    _resolve_calls.clear()

    def spy_resolve(space_id="", user_id=""):
        _resolve_calls.append(space_id)
        return {"docs": [{"name": "doc", "content": "body"}]}

    rl.resolve_knowledge = spy_resolve
    rl._load_local_runbook = lambda *a, **k: ""
    rl.build_skill_catalog = lambda *a, **k: {}


def _ctx(user="", space=""):
    mr.CURRENT_USER_ID.set(user)
    mr.CURRENT_SPACE_ID.set(space)


def test_confirmed_caller_space_overrides_model_value():
    _install_spies()
    _ctx(user="u1", space=SPACE_A)
    out = rl.compose_runbooks(space_id=SPACE_B, surface="chat")  # model asks for B
    assert _resolve_calls == [SPACE_A], f"expected caller space A, got {_resolve_calls}"
    assert out.get("custom_docs"), "confirmed-space request should load its custom docs"


def test_agent_request_with_unresolved_space_loads_nothing():
    _install_spies()
    _ctx(user="u1", space="")  # live request whose space did not resolve
    out = rl.compose_runbooks(space_id=SPACE_B, surface="investigation")
    assert _resolve_calls == [], f"unresolved agent request must compose nothing, got {_resolve_calls}"
    assert not out.get("custom_docs"), "unresolved agent request must load no custom docs"


def test_batch_path_trusts_the_argument():
    _install_spies()
    _ctx(user="", space="")  # no agent context (batch / Tier-1 / unit)
    out = rl.compose_runbooks(space_id=SPACE_B, surface="investigation")
    assert _resolve_calls == [SPACE_B], f"batch path should trust the passed space, got {_resolve_calls}"
    assert out.get("custom_docs"), "batch path should still load the space's docs"


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
