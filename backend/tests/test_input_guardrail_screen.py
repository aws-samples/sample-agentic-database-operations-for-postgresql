# SPDX-License-Identifier: Apache-2.0

"""A user turn is screened on its own, and a refused turn never reaches the agent.

The property under test is about WHAT gets assessed, not how strict the assessment is. A
Converse request carries the whole replayed conversation, and a guardrail assesses the
request it is handed, so an input filter attached to the model re-reads every earlier turn
on every request. One turn above the threshold then blocks every later request in the
session whatever it says, and no rewording clears it, because the text driving the
decision sits in the history rather than in the new message.

`screen_user_input` assesses exactly one turn, which keeps the decision proportional to
what the user typed, and hands the caller a refusal it can act on BEFORE the turn reaches
conversation memory. That ordering is the part that matters: a refused turn that is never
persisted cannot take part in the input that later turns are judged on.

The response-side half is asserted separately in tests/test_guardrail_wiring.py, which
pins the model-attached guardrail to output strengths only.

Hermetic: the Bedrock client is stubbed; no AWS calls, no strands, no boto3 import.

Run:  python3 backend/tests/test_input_guardrail_screen.py   (or via pytest)
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import model_resolver as mr  # noqa: E402

GUARDRAIL_VARS = ("GUARDRAIL_INPUT_ID", "GUARDRAIL_INPUT_VERSION", "GUARDRAIL_ENFORCE")


class FakeClient:
    """Stands in for bedrock-runtime, recording how it was called."""

    def __init__(self, response=None, raises=None, raise_times=None):
        self.response = response or {"action": "NONE"}
        self.raises = raises
        # If set, raise `raises` only for the first N calls, then succeed (flaky service).
        self.raise_times = raise_times
        self.calls = []

    def apply_guardrail(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises and (self.raise_times is None or len(self.calls) <= self.raise_times):
            raise self.raises
        return self.response


def _setup(client=None, **env):
    for var in GUARDRAIL_VARS:
        os.environ.pop(var, None)
    os.environ.update(env)
    mr._input_guardrail_client = client
    mr._SCREEN_BACKOFF_BASE = 0  # keep the throttle-retry tests instant
    return client


def _configured(client=None, **extra):
    return _setup(client, GUARDRAIL_INPUT_ID="gid-1", GUARDRAIL_INPUT_VERSION="3", **extra)


def test_clean_turn_is_allowed():
    client = _configured(FakeClient({"action": "NONE"}))
    assert mr.screen_user_input("analyze query performance") == (False, "")
    assert len(client.calls) == 1


def test_request_assesses_only_the_supplied_turn():
    """No history, no system prompt — one text block holding just this turn."""
    client = _configured(FakeClient({"action": "NONE"}))
    mr.screen_user_input("analyze query performance")
    assert client.calls[0] == {
        "guardrailIdentifier": "gid-1",
        "guardrailVersion": "3",
        "source": "INPUT",
        "content": [{"text": {"text": "analyze query performance"}}],
    }, client.calls[0]


def test_blocked_turn_reports_the_guardrail_wording():
    """The wording belongs to the guardrail resource (BlockedInputMessaging), not here."""
    client = _configured(FakeClient({
        "action": "GUARDRAIL_INTERVENED",
        "outputs": [{"text": "Blocked by policy. Please rephrase."}],
        "assessments": [{"contentPolicy": {}}],
    }))
    assert mr.screen_user_input("...") == (True, "Blocked by policy. Please rephrase.")
    assert len(client.calls) == 1


def test_block_without_a_message_falls_back():
    _configured(FakeClient({"action": "GUARDRAIL_INTERVENED", "outputs": []}))
    blocked, message = mr.screen_user_input("...")
    assert blocked is True
    assert message == mr.BLOCKED_INPUT_FALLBACK


def test_empty_turn_is_not_sent_for_assessment():
    client = _configured(FakeClient({"action": "GUARDRAIL_INTERVENED"}))
    assert mr.screen_user_input("   ") == (False, "")
    assert mr.screen_user_input("") == (False, "")
    assert client.calls == [], "whitespace should not cost an API call"


def test_unconfigured_screen_is_fail_soft():
    """No guardrail and no enforcement: byte-identical to having no screen at all."""
    client = _setup(FakeClient({"action": "GUARDRAIL_INTERVENED"}))
    assert mr.screen_user_input("anything") == (False, "")
    assert client.calls == []


def test_unconfigured_screen_is_fail_closed_under_enforcement():
    _setup(FakeClient(), GUARDRAIL_ENFORCE="on")
    try:
        mr.screen_user_input("anything")
    except mr.GuardrailConfigError:
        return
    raise AssertionError("enforcement ON with no input guardrail must refuse the turn")


def test_half_configured_screen_counts_as_absent():
    """An id with no version is not a usable guardrail, so it is treated as unset."""
    _setup(FakeClient(), GUARDRAIL_INPUT_ID="gid-1")
    assert mr.input_guardrail_from_env() == ("", "")
    _setup(FakeClient(), GUARDRAIL_INPUT_VERSION="3")
    assert mr.input_guardrail_from_env() == ("", "")


def _throttle():
    return RuntimeError("ThrottlingException: Rate exceeded")


def test_transient_throttle_is_retried_then_allowed():
    """A brief throttle clears on retry, and the turn goes through normally."""
    client = _configured(FakeClient({"action": "NONE"}, raises=_throttle(), raise_times=1))
    assert mr.screen_user_input("analyze query performance") == (False, "")
    assert len(client.calls) == 2, "should retry the throttle once, then succeed"


def test_sustained_throttle_fails_closed_under_enforcement():
    """A throttle that outlasts the retry budget is refused under GUARDRAIL_ENFORCE."""
    client = _configured(FakeClient(raises=_throttle()), GUARDRAIL_ENFORCE="on")
    blocked, message = mr.screen_user_input("analyze query performance")
    assert blocked is True
    # a soft, retryable message — NOT the policy-block wording
    assert message == mr.GUARDRAIL_UNAVAILABLE_MESSAGE
    assert message != mr.BLOCKED_INPUT_FALLBACK
    assert len(client.calls) == mr._SCREEN_MAX_ATTEMPTS, "exhausts the retry budget first"


def test_sustained_throttle_fails_open_without_enforcement():
    """Default mode is unchanged: a sustained throttle is allowed through after retries."""
    client = _configured(FakeClient(raises=_throttle()))
    assert mr.screen_user_input("analyze query performance") == (False, "")
    assert len(client.calls) == mr._SCREEN_MAX_ATTEMPTS


def test_non_throttle_error_allows_the_turn_even_under_enforcement():
    """A non-throttle failure is a one-off blip, not the quota-exhaustion vector: stay
    fail-open and do not retry, so a single odd error cannot become an outage."""
    client = _configured(FakeClient(raises=RuntimeError("boom")), GUARDRAIL_ENFORCE="on")
    assert mr.screen_user_input("analyze query performance") == (False, "")
    assert len(client.calls) == 1, "non-throttle errors are not retried"


def test_screen_never_logs_the_turn_text(capsys=None):
    """The turn is what just failed a safety check; only policy names are logged."""
    import io
    import logging

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("model-resolver")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        secret = "zzunmistakabletokenzz"
        _configured(FakeClient({
            "action": "GUARDRAIL_INTERVENED",
            "outputs": [{"text": "Blocked."}],
            "assessments": [{"contentPolicy": {}}, {"sensitiveInformationPolicy": {}}],
        }))
        mr.screen_user_input(secret)
        logged = stream.getvalue()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    assert secret not in logged, f"the turn text reached the log: {logged}"
    assert "content" in logged and "sensitiveInformation" in logged, logged


def test_chat_path_refuses_before_building_the_agent():
    """Source-level: the refusal returns from the chat branch, ahead of the agent.

    Asserted against the source rather than by driving FastAPI, because the ordering IS
    the fix — a screen that ran after the agent was invoked would already have persisted
    the turn.
    """
    with open(os.path.join(ROOT, "app.py"), "r", encoding="utf-8") as f:
        src = f.read()
    screen_at = src.find("screen_user_input(payload.user_text or payload.prompt)")
    assert screen_at != -1, "the chat path does not screen the incoming turn"
    # search for the refusal AFTER the chat screen (the investigation branch also has one,
    # earlier in the file)
    refusal_at = src.find("_refusal_stream(block_message)", screen_at)
    assert refusal_at != -1, "a blocked chat turn does not return a refusal"
    stream_at = src.find("get_or_create_agent(session_id, user_id, stream_model)")
    assert screen_at < refusal_at < stream_at, (
        "the screen must run, and refuse, before the chat agent is built"
    )


def test_chat_screens_the_user_text_not_the_app_wrapper():
    """The chat path screens the caller's own question (user_text), not the app-assembled
    report/investigation context in prompt.

    In report/investigation discussion the client prepends a trusted context block with
    imperatives ("answer ONLY from this", "do NOT call live tools"). Screening that whole
    blob made the input guardrail read our own instructions as a prompt-injection attempt
    and blocked every such turn. Screening user_text (with a fall back to prompt) judges
    what the user typed. Asserted at the source because the choice of WHAT is screened is
    the fix.
    """
    with open(os.path.join(ROOT, "app.py"), "r", encoding="utf-8") as f:
        src = f.read()
    assert "screen_user_input(payload.user_text or payload.prompt)" in src, (
        "the chat screen must prefer the user's own text, falling back to the full prompt"
    )
    assert "user_text: Optional[str]" in src, "InvocationRequest must carry the user's own text"


def test_investigation_path_is_screened():
    """The investigation turn is screened too, before its agent is built.

    The prompt after the [INVESTIGATION:id] marker arrives over the wire like any other
    turn, so it gets the same input-guardrail check the chat path uses, and a refusal is
    returned before the investigation agent is constructed.
    """
    with open(os.path.join(ROOT, "app.py"), "r", encoding="utf-8") as f:
        src = f.read()
    assert "screen_user_input(payload.user_text or payload.prompt)" in src, "chat turn is not screened"
    inv_screen = src.find("screen_user_input(actual_prompt)")
    assert inv_screen != -1, "the investigation turn is not screened"
    inv_agent = src.find("get_or_create_investigation_agent(investigation_id, stream_model")
    assert inv_agent != -1 and inv_screen < inv_agent, (
        "the investigation screen must run before its agent is built"
    )


def test_invocations_caps_prompt_length():
    """A single turn is length-capped before branching, so no one message can burn the
    ApplyGuardrail quota; the cap covers both chat and investigation."""
    with open(os.path.join(ROOT, "app.py"), "r", encoding="utf-8") as f:
        src = f.read()
    assert "def _max_prompt_chars(" in src, "no per-turn prompt cap helper"
    assert "MAX_PROMPT_CHARS" in src, "the cap is not configurable"
    cap_at = src.find("len(payload.prompt) > max_prompt_chars")
    assert cap_at != -1, "the cap is not enforced in /invocations"
    branch_at = src.find('payload.prompt.startswith("[INVESTIGATION:")')
    assert branch_at != -1 and cap_at < branch_at, (
        "the cap must run before the chat/investigation branch so it covers both"
    )


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
