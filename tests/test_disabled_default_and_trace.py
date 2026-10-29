#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A disabled model is never shown as the default, and the resolved model id rides the trace.

Review #14, two halves:

  * A model that is the stored account default but has since been DISABLED is not in
    force (request resolution skips it), so ``account_default_model`` must not report it
    as the default either — it falls through to the model a request will actually use
    (``AgentModelId`` if enabled, else the first enabled model). The admin console reads
    that same helper and re-loads after a toggle, so the shown default corrects itself
    without a manual refresh (rule 15).

  * The resolved model id is emitted as one ``model`` trace event per turn. A Bedrock
    model id is a model NAME, not an infrastructure identifier (no ARN, no account id,
    no deployment endpoint), so the trace redactor leaves it intact.

Pure/stdlib — the resolver and the stream redactor run without AWS.
"""
import json
import os
import pathlib
import sys
import types

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

# streaming.event_parser imports `from tools.types import SubAgentResult`, which pulls
# in the strands agent framework. The redactor and the model-event emitter under test
# need neither, so stub strands (@tool is the identity) before importing.
_strands = types.ModuleType("strands")
_strands.tool = lambda f=None, **kw: (f if f is not None else (lambda g: g))
_strands.Agent = object
sys.modules.setdefault("strands", _strands)

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


def catalog(enabled, disabled=(), account_default=""):
    models = {mid: {"enabled": True} for mid in enabled}
    for mid in disabled:
        models[mid] = {"enabled": False}
    return {"models": models, "account_default_model": account_default}


os.environ["AGENT_MODEL_ID"] = HAIKU
from model_resolver import account_default_model  # noqa: E402
from streaming import emit_model_event  # noqa: E402
from streaming.event_parser import _redact_text  # noqa: E402

print("══ a disabled stored default is not shown as the default ══")
val, src = account_default_model(catalog(enabled=[HAIKU, FABLE], disabled=[OPUS], account_default=OPUS))
check((val, src) == (HAIKU, "deployment"),
      "a disabled stored default falls through to AgentModelId (enabled), tagged 'deployment'")

val, src = account_default_model(catalog(enabled=[FABLE], disabled=[OPUS, HAIKU], account_default=OPUS))
check((val, src) == (FABLE, "fallback"),
      "stored default AND AgentModelId both disabled -> first enabled model, tagged 'fallback'")

val, src = account_default_model(catalog(enabled=[HAIKU, OPUS], account_default=OPUS))
check((val, src) == (OPUS, "account"),
      "an enabled stored default is still reported as the account default")

val, src = account_default_model(catalog(enabled=[HAIKU], account_default=""))
check((val, src) == (HAIKU, "deployment"),
      "nothing stored -> the deployment default")

# A bare catalog with no enablement info keeps the historical behaviour (no regression
# for an older/empty governance record).
val, src = account_default_model({"account_default_model": OPUS})
check((val, src) == (OPUS, "account"),
      "a catalog with no 'models' map still reports its stored default (no enablement info)")

print("")
print("══ the admin console reads the shared helper and re-loads after a toggle ══")
cat_src = (BACKEND / "app.py").read_text(encoding="utf-8")
seg = cat_src.split('@app.get("/api/models/catalog")', 1)[1].split("@app.", 1)[0]
check("account_default_model(catalog)" in seg,
      "/api/models/catalog derives the default via the shared account_default_model helper")
check('"account_default_source": default_source' in seg,
      "the endpoint returns the source so the console can label a deployment/fallback default")

jsx = (pathlib.Path(__file__).resolve().parent.parent
       / "admin-console/src/components/ModelCatalog.jsx").read_text(encoding="utf-8")
toggle = jsx.split("const toggle =", 1)[1].split("const setDefault", 1)[0]
check("await load()" in toggle,
      "disabling a model re-loads the catalog, so the shown default corrects itself (no manual refresh)")

print("")
print("══ the resolved model id rides the trace, un-redacted ══")
line = emit_model_event(HAIKU)
obj = json.loads(line)
check(obj == {"type": "model", "model_id": HAIKU},
      "emit_model_event yields one NDJSON 'model' line naming the resolved id")
check(_redact_text(HAIKU) == HAIKU,
      "the redactor leaves a Bedrock model id intact (it is a model name, not infrastructure)")
check(_redact_text(OPUS) == OPUS and _redact_text(FABLE) == FABLE,
      "other model ids pass the redactor unchanged too")

app_src = (BACKEND / "app.py").read_text(encoding="utf-8")
check("yield emit_model_event(stream_model, model_reasoning)" in app_src,
      "the stream prepends the resolved model (stream_model) as a trace event, once per turn")
check("model_reasoning = reasoning_summary(" in app_src
      and "SURFACE_INVESTIGATION if is_investigation else SURFACE_CHAT" in app_src,
      "the reasoning controls are computed in the request, for the request's surface")

print("")
print("══ the model line also names the reasoning controls the turn runs with ══")
from model_resolver import reasoning_summary, SURFACE_CHAT, SURFACE_INVESTIGATION, SURFACE_HELPER  # noqa: E402
for _k in ("CHAT_EFFORT", "INVESTIGATION_EFFORT", "HELPER_EFFORT"):
    os.environ.pop(_k, None)
H55, NOVA = "global.anthropic.claude-haiku-5-5", "amazon.nova-lite-v1:0"
check(reasoning_summary(H55, SURFACE_CHAT) == {"surface": "chat", "effort": "low", "thinking": "model default"},
      "chat on Haiku 5.5: effort low, thinking left to the model")
check(reasoning_summary(H55, SURFACE_INVESTIGATION)["effort"] == "high", "investigation runs at effort high")
check(reasoning_summary(H55, SURFACE_HELPER)["thinking"] == "disabled", "helper calls turn thinking off")
check(reasoning_summary(HAIKU, SURFACE_CHAT) == {"surface": "chat", "effort": None, "thinking": "model default"},
      "Haiku 4.5 takes no effort parameter, so none is reported")
check(reasoning_summary(NOVA, SURFACE_CHAT)["thinking"] == "not supported", "a model without the control says so")
obj = json.loads(emit_model_event(H55, reasoning_summary(H55, SURFACE_CHAT)))
check(obj == {"type": "model", "model_id": H55, "surface": "chat", "effort": "low", "thinking": "model default"},
      "the model line carries surface, effort and thinking")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
