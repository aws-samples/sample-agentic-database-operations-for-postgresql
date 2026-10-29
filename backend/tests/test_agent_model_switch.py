# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""One chat agent per (user, session), with the model swapped per turn.

A per-model cache key gave each model its own Agent, each with its own in-memory
conversation. Switching models mid-chat (turn 1 Haiku, turn 2 Sonnet, turn 3 Haiku) left
the Haiku agent never having seen the Sonnet turn, so it answered as if that turn never
happened. The chat agent is now cached per (user, session) and only its model is swapped,
so one conversation history spans every model the user picks.

Source-level pins (building a real strands Agent is not hermetic). Run:
  python3 backend/tests/test_agent_model_switch.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def _func(name):
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^(?:@|def |class |[A-Z_]+ = )|\Z)", SRC, re.S | re.M)
    assert m, f"{name} not found"
    return m.group(0)


body = _func("get_or_create_agent")

print("the chat agent is cached per (user, session), not per model")
check('cache_key = f"{user_id}::{session_id}"' in body, "cache key is user::session with no model")
check("::{model_id}" not in body, "model_id is NOT part of the chat agent cache key")

print("a cache hit swaps only the model so the conversation history is kept")
check("_dbops_model_id" in body and "agent.model = build_model(model_id" in body,
      "on a hit the model is swapped when it changed")
check(body.index("agent.model = build_model") < body.index("return agent"),
      "the model swap happens before returning the cached agent")

print("the temperature-retry eviction targets the same per-session key")
check('agents.pop(f"{user_id}::{session_id}", None)' in SRC,
      "the chat evict drops the per-session agent (matching the cache key)")
check('agents.pop(f"{user_id}::{session_id}::{stream_model}"' not in SRC,
      "no stale per-model evict key remains")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
