# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Reasoning controls per surface (model_resolver).

Chat runs at a low effort, investigation at a high one, and the helper calls run with
thinking off at the lowest effort; each control is sent only to models that accept it,
and a rejection learned at runtime is remembered. Stdlib-only: no strands needed.

Run:  python3 backend/tests/test_surface_effort.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import model_resolver as mr  # noqa: E402

HAIKU55 = "global.anthropic.claude-haiku-5-5"
HAIKU45 = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
OPUS47 = "global.anthropic.claude-opus-4-7"
SONNET45 = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
NOVA = "amazon.nova-lite-v1:0"
CLAUDE3 = "anthropic.claude-3-haiku-20240307-v1:0"


def _without_env(*keys):
    class _Ctx:
        def __enter__(self):
            self.saved = {k: os.environ.pop(k, None) for k in keys}
        def __exit__(self, *a):
            for k, v in self.saved.items():
                if v is not None:
                    os.environ[k] = v
    return _Ctx()


def test_effort_support_by_family():
    assert mr.model_supports_effort(HAIKU55)
    assert mr.model_supports_effort(OPUS47)
    assert not mr.model_supports_effort(HAIKU45), "Haiku 4.5 rejects the effort parameter"
    assert not mr.model_supports_effort(SONNET45)
    assert not mr.model_supports_effort(NOVA)


def test_thinking_control_support_by_family():
    assert mr.model_supports_thinking_control(HAIKU55)
    assert mr.model_supports_thinking_control(HAIKU45)
    assert mr.model_supports_thinking_control(SONNET45)
    assert not mr.model_supports_thinking_control(CLAUDE3)
    assert not mr.model_supports_thinking_control(NOVA)


def test_surface_defaults():
    with _without_env("CHAT_EFFORT", "INVESTIGATION_EFFORT", "HELPER_EFFORT"):
        assert mr.surface_model_kwargs(HAIKU55, mr.SURFACE_CHAT) == {"output_config": {"effort": "low"}}
        assert mr.surface_model_kwargs(HAIKU55, mr.SURFACE_INVESTIGATION) == {"output_config": {"effort": "high"}}
        assert mr.surface_model_kwargs(HAIKU55, mr.SURFACE_HELPER) == {
            "thinking": {"type": "disabled"}, "output_config": {"effort": "low"}}


def test_only_accepted_controls_are_sent():
    with _without_env("CHAT_EFFORT", "INVESTIGATION_EFFORT", "HELPER_EFFORT"):
        assert mr.surface_model_kwargs(HAIKU45, mr.SURFACE_CHAT) == {}
        assert mr.surface_model_kwargs(HAIKU45, mr.SURFACE_HELPER) == {"thinking": {"type": "disabled"}}
        assert mr.surface_model_kwargs(NOVA, mr.SURFACE_HELPER) == {}
        assert mr.surface_model_kwargs(NOVA, mr.SURFACE_INVESTIGATION) == {}


def test_env_overrides_effort_and_ignores_invalid_values():
    with _without_env("CHAT_EFFORT", "INVESTIGATION_EFFORT", "HELPER_EFFORT"):
        os.environ["CHAT_EFFORT"] = "medium"
        os.environ["INVESTIGATION_EFFORT"] = "turbo"
        assert mr.surface_effort(mr.SURFACE_CHAT) == "medium"
        assert mr.surface_effort(mr.SURFACE_INVESTIGATION) == "high", "an unknown level falls back"
        os.environ.pop("CHAT_EFFORT")
        os.environ.pop("INVESTIGATION_EFFORT")


def test_unknown_surface_is_an_error():
    try:
        mr.surface_effort("batch")
    except ValueError:
        return
    raise AssertionError("an unknown surface must raise")


def test_runtime_rejection_is_remembered():
    mid = "global.anthropic.claude-future-9"
    assert mr.model_supports_thinking_control(mid)
    mr.mark_thinking_unsupported(mid)
    assert not mr.model_supports_thinking_control(mid)
    mr._THINKING_UNSUPPORTED.discard(mid)
    mr.mark_effort_unsupported(HAIKU55)
    assert not mr.model_supports_effort(HAIKU55)
    mr._EFFORT_UNSUPPORTED.discard(HAIKU55)
    assert mr.model_supports_effort(HAIKU55)


def test_unsupported_reasoning_param_classifies_bedrock_errors():
    assert mr.unsupported_reasoning_param(
        RuntimeError("ValidationException: This model does not support the effort parameter.")) == "effort"
    assert mr.unsupported_reasoning_param(
        RuntimeError("ValidationException: thinking.type: unexpected value")) == "thinking"
    assert mr.unsupported_reasoning_param(RuntimeError("ThrottlingException: slow down")) == ""
    assert mr.unsupported_reasoning_param(
        RuntimeError("ValidationException: temperature is deprecated")) == ""


def test_helper_model_is_the_deployment_default_unless_pinned():
    with _without_env("AGENT_MODEL_ID", "CHAT_FOLLOWUPS_MODEL_ID"):
        os.environ["AGENT_MODEL_ID"] = HAIKU55
        assert mr.helper_model_id() == HAIKU55
        os.environ["CHAT_FOLLOWUPS_MODEL_ID"] = HAIKU45
        assert mr.helper_model_id() == HAIKU45


def test_build_bedrock_model_passes_surface_fields_and_inherits_current_surface():
    # strands is replaced by a recorder so the kwargs the builder sends are inspectable.
    import types
    recorded = {}

    class _Model:
        def __init__(self, **kw):
            recorded.update(kw)

    pkg = types.ModuleType("strands"); models = types.ModuleType("strands.models")
    bedrock = types.ModuleType("strands.models.bedrock"); bedrock.BedrockModel = _Model
    saved = {k: sys.modules.get(k) for k in ("strands", "strands.models", "strands.models.bedrock")}
    sys.modules.update({"strands": pkg, "strands.models": models, "strands.models.bedrock": bedrock})
    try:
        with _without_env("CHAT_EFFORT", "INVESTIGATION_EFFORT", "HELPER_EFFORT",
                          "GUARDRAIL_ID", "GUARDRAIL_VERSION", "GUARDRAIL_ENFORCE"):
            mr.build_bedrock_model(HAIKU55, 100, surface=mr.SURFACE_INVESTIGATION)
            assert recorded["additional_request_fields"] == {"output_config": {"effort": "high"}}
            assert "temperature" not in recorded
            recorded.clear()
            token = mr.CURRENT_SURFACE.set(mr.SURFACE_CHAT)
            try:
                mr.build_bedrock_model(HAIKU55, 100)
            finally:
                mr.CURRENT_SURFACE.reset(token)
            assert recorded["additional_request_fields"] == {"output_config": {"effort": "low"}}, \
                "a sub-agent built without a surface inherits the request's"
            recorded.clear()
            mr.build_bedrock_model(HAIKU45, 100, surface=mr.SURFACE_CHAT)
            assert "additional_request_fields" not in recorded
            assert recorded["temperature"] == 0
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"  FAIL {name}: {e!r}")
    sys.exit(1 if failures else 0)
