#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The model-governance cascade resolves to the model the operator was shown.

Precedence, highest first: an operator's per-message pick, the space default, the
space-admin tier default, the stored account default, then the model named by the
deployment's `AgentModelId` parameter. A default only counts while it survives the
cascade, so a layer that disables a model opts out of it as a default too.

The case most worth guarding is a fresh deployment, where nothing is stored at any
layer. Resolution must land on `AgentModelId` rather than on whichever enabled model
happens to sort first, and every surface must agree on the answer — a console showing
one model while the agent runs another reads as a display glitch and hides the cause.

These tests stub the governance and spaces registries, so they exercise the real
precedence logic without AWS.
"""
import os
import pathlib
import sys
import types

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

FABLE = "global.anthropic.claude-fable-5"
HAIKU = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
OPUS = "global.anthropic.claude-opus-5"

passed = 0
failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def install_stubs(catalog: dict, space: dict = None, admin_records: dict = None):
    """Stub tools.aws.governance_store / spaces_client / space_admins in sys.modules.

    resolve_model imports them lazily inside the function, so replacing the modules
    here is enough — no AWS, no network.
    """
    admin_records = admin_records or {}

    tools = sys.modules.setdefault("tools", types.ModuleType("tools"))
    tools.__path__ = []
    aws_pkg = sys.modules.setdefault("tools.aws", types.ModuleType("tools.aws"))
    aws_pkg.__path__ = []

    gov = types.ModuleType("tools.aws.governance_store")

    class _Gov:
        def get_model_catalog(self):
            return catalog

        def get_admin_governance(self, sub):
            return admin_records.get(sub, {})

    gov.governance_registry = _Gov()
    sys.modules["tools.aws.governance_store"] = gov

    sp = types.ModuleType("tools.aws.spaces_client")

    class _Spaces:
        def get_space(self, space_id):
            return space

    sp.spaces_registry = _Spaces()
    sys.modules["tools.aws.spaces_client"] = sp

    sa = types.ModuleType("tools.aws.space_admins")
    sa.managing_admin_subs = lambda space_id, created_by: set(admin_records)
    sys.modules["tools.aws.space_admins"] = sa


def catalog_with(enabled, account_default=""):
    return {
        "models": {mid: {"enabled": True} for mid in enabled},
        "account_default_model": account_default,
    }


os.environ["AGENT_MODEL_ID"] = HAIKU
from model_resolver import account_default_model, resolve_model  # noqa: E402

ALL = [FABLE, HAIKU, OPUS]

print("══ a fresh install with nothing stored ══")
install_stubs(catalog_with(ALL, account_default=""))
r = resolve_model("", "")
check(r["model_id"] == HAIKU,
      f"resolves to the deployment's AgentModelId, not the first id alphabetically "
      f"(got {r['model_id'].rsplit('.', 1)[-1]})")
check(r["source"] == "deployment", f"source is 'deployment' (got {r['source']!r})")
check(sorted(ALL)[0] == FABLE,
      "sanity: the assertion above is meaningful because Fable does sort first")

print("")
print("══ account_default_model reports where the value came from ══")
val, src = account_default_model(catalog_with(ALL, account_default=""))
check((val, src) == (HAIKU, "deployment"), "nothing stored -> the env default, 'deployment'")
val, src = account_default_model(catalog_with(ALL, account_default=OPUS))
check((val, src) == (OPUS, "account"), "stored choice -> that value, 'account'")
val, src = account_default_model(catalog_with(ALL, account_default="   "))
check((val, src) == (HAIKU, "deployment"), "a blank stored value is treated as unset")

print("")
print("══ precedence, highest first ══")
install_stubs(catalog_with(ALL, account_default=OPUS))
check(resolve_model("", "")["source"] == "account", "stored account default beats the deployment one")
check(resolve_model("", FABLE) == {"model_id": FABLE, "source": "user"},
      "an operator's pick beats every configured default")

space = {"space_id": "s1", "created_by": "sub-a",
         "model_governance": {"default_model": FABLE, "disabled_models": []}}
install_stubs(catalog_with(ALL, account_default=OPUS), space=space)
r = resolve_model("s1", "")
check(r == {"model_id": FABLE, "source": "space"}, "the space default beats the account default")

install_stubs(catalog_with(ALL, account_default=OPUS),
              space={"space_id": "s1", "created_by": "sub-a", "model_governance": {}},
              admin_records={"sub-a": {"default_model": FABLE}})
r = resolve_model("s1", "")
check(r == {"model_id": FABLE, "source": "space-admin"},
      "the space-admin tier default beats the account default")

print("")
print("══ a default only counts while it survives the cascade ══")
install_stubs(catalog_with(ALL, account_default=OPUS),
              space={"space_id": "s1", "created_by": "sub-a",
                     "model_governance": {"disabled_models": [OPUS]}})
r = resolve_model("s1", "")
check(r["model_id"] == HAIKU and r["source"] == "deployment",
      "a space that disables the account default falls through to the deployment default")

install_stubs(catalog_with(ALL, account_default=OPUS),
              space={"space_id": "s1", "created_by": "sub-a",
                     "model_governance": {"disabled_models": [OPUS, HAIKU]}})
r = resolve_model("s1", "")
check(r == {"model_id": FABLE, "source": "cascade"},
      "with both configured defaults disabled, the remaining model wins as 'cascade'")

install_stubs(catalog_with(ALL, account_default=OPUS))
check(resolve_model("", FABLE + "-nonexistent")["source"] != "user",
      "an unknown operator pick is ignored rather than honored")

print("")
print("══ the deployment default is not a way past the account allowlist ══")
install_stubs(catalog_with([FABLE, OPUS], account_default=""))
r = resolve_model("", "")
check(r["model_id"] != HAIKU,
      "AgentModelId is skipped when an admin has not enabled it")
check(r == {"model_id": FABLE, "source": "cascade"},
      "resolution stays inside the enabled set, deterministically")

print("")
print("══ nothing enabled at all: still returns something usable ══")
install_stubs(catalog_with([], account_default=""))
r = resolve_model("", "")
check(r == {"model_id": HAIKU, "source": "env"},
      "an empty catalog yields the env default rather than raising")

print("")
print("══ a broken store never breaks a chat turn ══")
gov = types.ModuleType("tools.aws.governance_store")


class _Boom:
    def get_model_catalog(self):
        raise RuntimeError("table unavailable")


gov.governance_registry = _Boom()
sys.modules["tools.aws.governance_store"] = gov
r = resolve_model("s1", "")
check(r == {"model_id": HAIKU, "source": "env"},
      "a governance-store failure falls back to the env default, fail-soft")

print("")
print("══ a caller-pinned model is still subject to the space cascade ══")
# The investigation create endpoint is behind a read-level gate, so a stored model_id
# is caller-influenced: it is re-checked through the cascade, never trusted as-is. A
# pin a space disabled is coerced to the governed default; a permitted pin is honored.
install_stubs(catalog_with(ALL, account_default=OPUS),
              space={"space_id": "s1", "created_by": "sub-a",
                     "model_governance": {"disabled_models": [FABLE]}})
check(resolve_model("s1", FABLE)["model_id"] != FABLE,
      "a pinned model the space disabled does not survive resolution")
check(resolve_model("s1", HAIKU) == {"model_id": HAIKU, "source": "user"},
      "a pinned model the space permits is still honored")

print("")
print("══ both investigation model paths route through resolve_model ══")
app_src = (BACKEND / "app.py").read_text(encoding="utf-8")
check("resolve_model(inv_space_id, inv_record.get(" in app_src,
      "the streaming path resolves the pinned model through the cascade before use")
check('stream_model = inv_record.get("model_id") or resolved' not in app_src,
      "the stored model id is no longer trusted verbatim at stream time")
check('resolve_model(space_id, data.model_id)["model_id"] != data.model_id' in app_src,
      "start_investigation rejects a model_id the space's governance does not permit")

print("")
print("══ agent model calls default to the deployment region, not us-east-1 ══")
mr_src = (BACKEND / "model_resolver.py").read_text(encoding="utf-8")
bbm = mr_src.split("def build_bedrock_model(", 1)[1].split("\ndef ", 1)[0]
check('os.getenv("BEDROCK_REGION") or os.getenv("AWS_REGION")' in bbm,
      "build_bedrock_model falls back to the deployment region (AWS_REGION) before us-east-1")
check('os.getenv("BEDROCK_REGION", "us-east-1")' not in mr_src,
      "no Bedrock caller in model_resolver hardcodes a us-east-1 fallback that skips AWS_REGION")
print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
