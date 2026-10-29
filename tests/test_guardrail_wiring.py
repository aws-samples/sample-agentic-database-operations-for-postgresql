#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A guardrail must reach every path that sends content to a model, in both directions.

Two guardrails, one per direction, because a Converse request carries the whole replayed
conversation and a guardrail assesses the request it is handed:

  * the MODEL guardrail is attached to every Converse call and screens responses only,
    with every input strength at NONE, so replayed history is never re-assessed;
  * the INPUT guardrail is applied by `model_resolver.screen_user_input` to one user turn
    at a time, and carries the prompt-attack filter.

`model_resolver.build_bedrock_model` derives the model guardrail purely from GUARDRAIL_ID
and GUARDRAIL_VERSION, and every agent and sub-agent is constructed through it, so a
variable that fails to arrive silently disarms the whole agent path. The AgentCore runtime
serves /invocations — chat and all four sub-agents — and needs both pairs; the ECS task
serves the REST surface. A runtime missing the variables and an endpoint reporting the
control as on are indistinguishable from inside the process that does have them.

Two paths bypass build_bedrock_model entirely and call bedrock.invoke_model directly: the
report comparison summary and the Batch scan's analysis leg. Both carry database-derived
text.

This is a wiring test, not a behaviour test. It reads the templates and the source,
because the failure mode is a variable that never arrives, which a mocked model call
cannot catch. The screening behaviour itself is covered by
backend/tests/test_input_guardrail_screen.py.

Run:  python3 tests/test_guardrail_wiring.py   (or via pytest)
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

AGENT_TPL = os.path.join(ROOT, "backend", "template.yaml")
API_TPL = os.path.join(ROOT, "infrastructure", "api.yaml")
MASTER_TPL = os.path.join(ROOT, "infrastructure", "master.yaml")
GUARDRAIL_TPL = os.path.join(ROOT, "infrastructure", "guardrail.yaml")
APP = os.path.join(ROOT, "backend", "app.py")
RUN_SCAN = os.path.join(ROOT, "batch", "run_scan.py")
RESOLVER = os.path.join(ROOT, "backend", "model_resolver.py")

GUARDRAIL_VARS = ("GUARDRAIL_ID", "GUARDRAIL_VERSION", "GUARDRAIL_ENFORCE")
INPUT_GUARDRAIL_VARS = ("GUARDRAIL_INPUT_ID", "GUARDRAIL_INPUT_VERSION")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _runtime_env_block(text):
    """The AgentRuntime's EnvironmentVariables mapping, as raw text."""
    m = re.search(r"\n  AgentRuntime:\n(.*?)(?=\n  [A-Za-z]+:\n)", text, re.S)
    assert m, "could not locate the AgentRuntime resource"
    env = re.search(r"EnvironmentVariables:\n(.*?)(?=\n      [A-Za-z]|\Z)", m.group(1), re.S)
    assert env, "AgentRuntime has no EnvironmentVariables block"
    return env.group(1)


def test_runtime_receives_every_guardrail_variable():
    env = _runtime_env_block(_read(AGENT_TPL))
    for var in GUARDRAIL_VARS + INPUT_GUARDRAIL_VARS:
        assert f"{var}:" in env, (
            f"{var} missing from the AgentRuntime environment — the runtime that serves "
            "chat and the sub-agents would run unguarded"
        )


def test_agent_template_declares_the_parameters():
    text = _read(AGENT_TPL)
    for p in ("GuardrailId", "GuardrailVersion", "GuardrailEnforce",
              "InputGuardrailId", "InputGuardrailVersion"):
        assert re.search(rf"\n  {p}:\n    Type: String", text), f"{p} parameter missing"


def test_master_threads_both_guardrails_into_the_agent_stack():
    text = _read(MASTER_TPL)
    agent = re.search(r"\n  AgentStack:\n(.*?)(?=\n  # =|\n  [A-Za-z]+:\n    Type:)", text, re.S)
    assert agent, "could not locate AgentStack"
    body = agent.group(1)
    # GuardrailId carries the MODEL guardrail: the one attached to Converse must be the
    # response-screening one, or replayed history is assessed on every request.
    assert "GuardrailId: !GetAtt GuardrailStack.Outputs.ModelGuardrailId" in body, \
        "AgentStack's GuardrailId must come from the model (response-screening) guardrail"
    assert "GuardrailVersion: !GetAtt GuardrailStack.Outputs.ModelGuardrailVersion" in body, \
        "AgentStack's GuardrailVersion must come from the model guardrail"
    assert "InputGuardrailId: !GetAtt GuardrailStack.Outputs.InputGuardrailId" in body, \
        "AgentStack does not receive the input-screening guardrail"
    assert "InputGuardrailVersion: !GetAtt GuardrailStack.Outputs.InputGuardrailVersion" in body, \
        "AgentStack does not receive the input guardrail version"
    assert "GuardrailEnforce:" in body, "AgentStack does not receive GuardrailEnforce"


def test_model_guardrail_screens_output_only():
    """Every input strength NONE, and no prompt-attack filter.

    This is the property that keeps a session usable: an input filter here would re-read
    the whole replayed conversation on every request, so one turn above the threshold
    would block every later request regardless of its own content.
    """
    text = _read(GUARDRAIL_TPL)
    m = re.search(r"\n  AgentModelGuardrail:\n(.*?)(?=\n  [A-Za-z]+:\n    Type:)", text, re.S)
    assert m, "AgentModelGuardrail missing — the model has no response-only guardrail"
    body = m.group(1)
    filters = re.search(r"FiltersConfig:\n(.*?)(?=\n      [A-Za-z])", body, re.S)
    assert filters, "AgentModelGuardrail has no content filters"
    strengths = re.findall(r"InputStrength: (\S+)", filters.group(1))
    assert strengths, "no InputStrength entries found"
    assert all(s == "NONE" for s in strengths), (
        f"the model-attached guardrail must not assess input, found: {sorted(set(strengths))}"
    )
    assert "PROMPT_ATTACK" not in body, (
        "PROMPT_ATTACK is input-only, so it belongs on the input screen, not on the "
        "guardrail attached to the model"
    )


def test_input_guardrail_keeps_the_prompt_attack_filter():
    """The screen is where prompt-attack detection has to live, since it is input-only."""
    text = _read(GUARDRAIL_TPL)
    m = re.search(r"\n  AgentGuardrail:\n(.*?)(?=\n  [A-Za-z]+:\n    Type:)", text, re.S)
    assert m, "AgentGuardrail missing"
    body = m.group(1)
    assert "PROMPT_ATTACK" in body, "the input screen lost prompt-attack detection"
    assert re.search(r"InputStrength: !Ref GuardrailPromptAttackStrength", body), \
        "prompt-attack strength should stay operator-controlled on the input screen"


def test_enforcement_is_gated_on_enable_guardrail_everywhere():
    # guardrail.yaml emits EMPTY ids when EnableGuardrail=false, and the application
    # refuses the call when enforcement is on but the ids are unresolvable. So passing
    # enforcement ungated turns EnableGuardrail=false into "no model calls at all".
    text = _read(MASTER_TPL)
    assert re.search(r"GuardrailEnabled: !Equals \[!Ref EnableGuardrail, 'true'\]", text), \
        "master.yaml needs a GuardrailEnabled condition"
    enforces = re.findall(r"GuardrailEnforce: (.+)", text)
    assert len(enforces) >= 2, \
        f"expected GuardrailEnforce passed to both the agent and API stacks, found {len(enforces)}"
    for value in enforces:
        assert "!If [GuardrailEnabled" in value, \
            f"GuardrailEnforce must be gated on GuardrailEnabled, got: {value.strip()}"


def test_guardrail_outputs_are_empty_when_disabled():
    # The premise the gating relies on, for both guardrails.
    text = _read(GUARDRAIL_TPL)
    outputs = re.search(r"\nOutputs:\n(.*)\Z", text, re.S)
    assert outputs, "guardrail.yaml has no Outputs section"
    # Leading newline restored: slicing the section consumed the one before the first key.
    body = "\n" + outputs.group(1)
    for out in ("InputGuardrailId", "InputGuardrailVersion",
                "ModelGuardrailId", "ModelGuardrailVersion"):
        m = re.search(rf"\n  {out}:\n(.*?)(?=\n  [A-Za-z]|\Z)", body, re.S)
        assert m, f"{out} output missing"
        assert "!If [GuardrailEnabled" in m.group(1), \
            f"{out} output should be conditional on GuardrailEnabled"


def test_both_runtimes_can_apply_a_guardrail():
    # Attaching a guardrail needs bedrock:ApplyGuardrail in addition to the invoke
    # actions. Without it, threading the variables turns every call into AccessDenied.
    agent = _read(AGENT_TPL)
    assert agent.count("bedrock:ApplyGuardrail") >= 2, (
        "both the AgentCore runtime role and the Batch task role need "
        "bedrock:ApplyGuardrail (found "
        f"{agent.count('bedrock:ApplyGuardrail')})"
    )
    assert "bedrock:ApplyGuardrail" in _read(API_TPL), "the ECS task role lost ApplyGuardrail"


def test_direct_invoke_model_calls_attach_the_guardrail():
    # Anything calling bedrock.invoke_model directly bypasses build_bedrock_model, so it
    # must attach the guardrail itself or it becomes the one unguarded path.
    for path, helper in ((APP, "guardrail_invoke_kwargs_from_env"),
                         (RUN_SCAN, "_guardrail_kwargs")):
        text = _read(path)
        for call in re.finditer(r"\.invoke_model\(", text):
            window = text[call.start():call.start() + 900]
            assert helper in window, (
                f"{os.path.basename(path)}: an invoke_model call near offset "
                f"{call.start()} does not attach the guardrail via {helper}()"
            )


def test_batch_job_definition_carries_the_variables():
    # run_scan cannot import model_resolver (the batch image ships a curated file set),
    # so it reads the environment directly and the job definition must supply it.
    text = _read(AGENT_TPL)
    jobdef = re.search(r"\n  BatchJobDefinition:\n(.*?)(?=\n  [A-Za-z]+:\n    Type:)", text, re.S)
    assert jobdef, "could not locate BatchJobDefinition"
    for var in GUARDRAIL_VARS:
        assert f"Name: {var}" in jobdef.group(1), \
            f"{var} missing from the Batch job definition environment"


def test_batch_helper_stays_import_free():
    # If this ever imports model_resolver the batch image build breaks — the file set in
    # batch/Dockerfile does not include it.
    text = _read(RUN_SCAN)
    assert "import model_resolver" not in text and "from model_resolver" not in text, \
        "batch/run_scan.py must not import model_resolver; the batch image lacks it"
    assert "_guardrail_kwargs" in text


def test_raw_helper_matches_the_strands_helper_rule():
    # Both must be both-or-nothing and fail closed under enforcement.
    text = _read(RESOLVER)
    m = re.search(r"def guardrail_invoke_kwargs_from_env.*?(?=\ndef |\Z)", text, re.S)
    assert m, "guardrail_invoke_kwargs_from_env missing from model_resolver"
    body = m.group(0)
    assert "guardrailIdentifier" in body and "guardrailVersion" in body
    assert "guardrail_enforcement_enabled()" in body, "raw helper must honour GUARDRAIL_ENFORCE"
    assert "GuardrailConfigError" in body, "raw helper must fail closed under enforcement"


def test_guardrail_cloudwatch_alarms_are_wired():
    """The input screen's ApplyGuardrail throttle and intervention are alarmed.

    ApplyGuardrail throttles on an account-wide quota, so a throttle alarm is the
    operator's early warning to raise the quota before screening degrades; an intervention
    alarm surfaces a possible abuse spike. Both are gated on an input guardrail being
    configured and notify the deployment SNS topic, and their thresholds are configurable
    parameters (seeded now, retunable live later).
    """
    text = _read(AGENT_TPL)
    for p in ("GuardrailThrottleAlarmThreshold", "GuardrailInterventionAlarmThreshold"):
        assert re.search(rf"\n  {p}:\n    Type: Number", text), f"{p} parameter missing"
    assert re.search(r"\n  HasInputGuardrail: !Not \[!Equals \[!Ref InputGuardrailId, ''\]\]", text), \
        "HasInputGuardrail condition missing"
    for name, metric in (("GuardrailThrottleAlarm", "InvocationThrottles"),
                          ("GuardrailInterventionAlarm", "InvocationsIntervened")):
        m = re.search(rf"\n  {name}:\n(.*?)(?=\n  [A-Za-z]+:\n    Type:)", text, re.S)
        assert m, f"{name} missing"
        body = m.group(1)
        assert "Type: AWS::CloudWatch::Alarm" in body, f"{name} is not a CloudWatch alarm"
        assert "Condition: HasInputGuardrail" in body, f"{name} must be gated on an input guardrail"
        assert "Namespace: AWS/Bedrock/Guardrails" in body, f"{name} has the wrong namespace"
        assert f"MetricName: {metric}" in body, f"{name} watches the wrong metric"
        assert "Value: ApplyGuardrail" in body, f"{name} must scope to the ApplyGuardrail operation"
        assert "NotificationTopicArn" in body, f"{name} does not notify the SNS topic"


def test_pii_entity_list_is_pinned_and_keeps_the_secret_key_filter():
    # AWS_SECRET_KEY stays on both guardrails: it stops the agent repeating an AWS secret
    # key found in customer data, query results or logs. It used to garble the database
    # UUIDs in the scope block; the fix is per-request aliases (surfaces/scope.py), never
    # dropping the filter.
    text = _read(GUARDRAIL_TPL)
    pinned = ["NAME", "EMAIL", "PHONE", "ADDRESS", "US_SOCIAL_SECURITY_NUMBER",
              "CREDIT_DEBIT_CARD_NUMBER", "PASSWORD", "AWS_SECRET_KEY"]
    for name in ("AgentGuardrail", "AgentModelGuardrail"):
        m = re.search(rf"\n  {name}:\n(.*?)(?=\n  [A-Za-z]+:\n    Type:|\Z)", text, re.S)
        assert m, f"{name} missing"
        pii = m.group(1).split("PiiEntitiesConfig:", 1)[1]
        entities = re.findall(r"- Type: (\w+)\n\s+Action: (.+)", pii)
        assert [e for e, _ in entities] == pinned, f"{name} PII entities changed: {entities}"
        assert all(a.strip() == "!Ref GuardrailPiiAction" for _, a in entities), f"{name}: {entities}"
    m = re.search(r"\n  GuardrailPiiAction:\n(.*?)(?=\n  [A-Za-z]+:\n)", text, re.S)
    assert m and "Default: 'ANONYMIZE'" in m.group(1), "the PII action must default to ANONYMIZE"


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
