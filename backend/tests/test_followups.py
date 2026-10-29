# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Follow-up suggestion tests for streaming.followups.

Covers the reply parser (JSON array, fenced array, line fallback, dedupe and
bounds), the prompt builder (context wrappers stripped, long answers trimmed),
and the stream wrapper (a `suggestions` line follows `result` only; interrupts,
errors, empty answers and CHAT_FOLLOWUPS=off produce none; the pass-through is
byte-for-byte). No network: the model call is injected.

Hermetic: strands is stubbed (tools.types imports Agent from it).
Run:  python3 backend/tests/test_followups.py   (or via pytest)
"""
import asyncio
import json
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_strands = types.ModuleType("strands")
_strands.tool = lambda f: f
_strands.Agent = object
sys.modules.setdefault("strands", _strands)

from streaming import followups as fu  # noqa: E402


# ── parse_suggestions ──────────────────────────────────────────────────────────

def test_parse_plain_json_array():
    text = '["Why is replica lag rising?", "Show CPU for the last hour", "Which query is slowest?"]'
    assert fu.parse_suggestions(text) == [
        "Why is replica lag rising?", "Show CPU for the last hour", "Which query is slowest?",
    ]


def test_parse_array_inside_prose_and_fence():
    text = 'Sure, here you go:\n```json\n["Compare the two reports", "What changed since yesterday?"]\n```'
    assert fu.parse_suggestions(text) == ["Compare the two reports", "What changed since yesterday?"]


def test_parse_line_fallback_strips_bullets_and_quotes():
    text = '- "Show replica lag for 24 hours"\n* Why does the ETL overlap?\n3) Fix the missing index\n'
    assert fu.parse_suggestions(text) == [
        "Show replica lag for 24 hours", "Why does the ETL overlap?", "Fix the missing index",
    ]


def test_parse_dedupes_bounds_and_caps():
    items = ["Same question?", "same question?", "x", "y" * 300, "Another one", "One too many"]
    out = fu.parse_suggestions(json.dumps(items))
    assert len(out) == fu.MAX_ITEMS
    assert out[0] == "Same question?"            # case-insensitive dedupe kept the first
    assert "x" not in out                        # too short
    assert out[1].endswith("…") and len(out[1]) <= fu.MAX_ITEM_CHARS


def test_parse_garbage_is_empty():
    assert fu.parse_suggestions("") == []
    assert fu.parse_suggestions(None) == []
    assert fu.parse_suggestions("[1, 2, 3]") == []
    assert fu.parse_suggestions("{\"not\": \"a list\"}") == []


# ── build_prompt ───────────────────────────────────────────────────────────────

def test_prompt_strips_injected_scope_and_trims_answer():
    wrapped = ("[DATABASE SCOPE] The user has selected database(s)...\n- orders (engine=postgres)\n"
               "[END DATABASE SCOPE]\n\nWhy is orders slow?")
    long_answer = "A" * 3000 + "MIDDLE" + "Z" * 3000
    p = fu.build_prompt(wrapped, long_answer)
    assert "Why is orders slow?" in p
    assert "[DATABASE SCOPE]" not in p and "engine=postgres" not in p
    assert "MIDDLE" not in p and "\n…\n" in p   # head + tail kept, middle dropped
    assert "propose 3 short follow-up questions" in p


# ── clarifying turns → replies ─────────────────────────────────────────────────

CLARIFYING = ("I understand there's a problem with your Aurora PostgreSQL database. To help you "
              "effectively, I need a bit more detail.\n\nCould you describe the issue? For example:\n"
              "- **Performance**: Is the database slow, or are queries taking longer than usual?\n"
              "- **Errors**: Are you seeing error messages or failed connections?\n"
              "- **Resources**: Is CPU, memory, storage, or connections high?\n\n"
              "Once you give me specifics, I can route the right diagnostic tools to investigate.")


def test_clarifying_turn_is_detected_and_a_plain_answer_is_not():
    assert fu.is_clarifying(CLARIFYING)
    assert fu.is_clarifying("Shall I start a full investigation on billing-aurora?")
    assert fu.is_clarifying("Two options here: check the replica, or the ETL job.\nWhich would you like me to look at?")
    assert not fu.is_clarifying("billing-aurora is the one to look at: replica lag 1.8s, CPU 94% sustained.")
    assert not fu.is_clarifying("Why does this happen? Autovacuum cannot keep up with the churn on public.orders.")
    assert not fu.is_clarifying("")
    assert fu.suggestion_kind(CLARIFYING) == "replies" and fu.suggestion_kind("Done.") == "questions"


def test_reply_prompt_asks_for_answers_not_questions():
    p = fu.build_prompt("there is a problem with the db", CLARIFYING)
    assert "REPLIES the" in p and "Never a question back to the assistant" in p
    assert "propose 3 short follow-up questions" not in p
    assert "Assistant question:" in p and "Could you describe the issue?" in p
    # An ordinary answer keeps the follow-up-question prompt.
    assert "propose 3 short follow-up questions" in fu.build_prompt("q", "billing-aurora is the one to look at.")


def test_stream_tags_replies_for_a_clarifying_turn():
    stream = [_line({"type": "text", "content": CLARIFYING}), _line({"type": "result", "response": "r"})]
    out = _run(stream, lambda *a: ["Queries are slower than usual", "Connections are failing", "Just start a full investigation"])
    last = json.loads(out[-1])
    assert last["kind"] == "replies" and last["items"][0] == "Queries are slower than usual"


OFFER = ("I've surfaced a **Start investigation** action for you.\n\n"
         "**Would you like to start the investigation?** If you'd prefer a quick answer here "
         "instead, let me know and I can pull a single metric.")


def test_prompt_rule_flips_when_a_start_button_is_on_screen():
    # Without the button, accepting the offer is a natural one-click reply...
    p = fu.build_prompt("queries are slow", OFFER)
    assert "Yes, start the investigation" in p and "already on screen" not in p
    # ...with it, the pills must cover the alternatives instead.
    p = fu.build_prompt("queries are slow", OFFER, offered_investigation=True)
    assert "already on screen" in p and "Yes, start the investigation" not in p
    # The follow-up-question template gets the rule too.
    p = fu.build_prompt("q", "billing-aurora is the one to look at.", offered_investigation=True)
    assert "propose 3 short follow-up questions" in p and "already on screen" in p
    assert "already on screen" not in fu.build_prompt("q", "billing-aurora is the one to look at.")


def test_accept_pills_are_dropped_when_the_turn_offered_an_investigation():
    kept = ["Just check CPU and I/O here", "Show me the top slow queries instead"]
    dropped = ["Yes, start the investigation", "Start the full investigation now", "Go ahead with the investigation",
               "Sure, run it", "Open the investigation, please"]
    assert fu.drop_investigation_accepts(kept + dropped) == kept
    stream = [
        _line({"type": "tool_start", "name": "recommend_investigation", "toolUseId": "t1"}),
        _line({"type": "tool", "name": "recommend_investigation", "toolUseId": "t1",
               "input": {"title": "Slow queries", "rationale": "needs correlation"}}),
        _line({"type": "tool_result", "name": "recommend_investigation", "toolUseId": "t1", "status": "success", "content": "ok"}),
        _line({"type": "text", "content": OFFER}),
        _line({"type": "result", "response": "r"}),
    ]
    seen = {}

    def gen(prompt, answer, model, **kw):
        seen.update(kw)
        return ["Yes, start the investigation", "Just check CPU and I/O here"]

    out = _run(stream, gen)
    assert seen == {"offered_investigation": True}                # the generator was told
    assert out[:-1] == stream
    assert json.loads(out[-1]) == {"type": "suggestions", "kind": "replies", "items": ["Just check CPU and I/O here"]}
    # A turn whose pills were ALL acceptances adds no line at all.
    assert _run(stream, lambda *a, **kw: ["Yes, start the investigation"]) == stream
    # Without the tool call the same offer text keeps its acceptance pill (no kwarg passed).
    plain = stream[3:]
    out = _run(plain, lambda prompt, answer, model: ["Yes, start the investigation", "Just check CPU first"])
    assert json.loads(out[-1])["items"] == ["Yes, start the investigation", "Just check CPU first"]


def test_pills_never_ask_the_read_only_agent_to_change_anything():
    # The prompts say the agent is read-only in both variants...
    assert "READ-ONLY" in fu.build_prompt("q", "orders is bloated.")
    assert "READ-ONLY" in fu.build_prompt("q", OFFER)
    # ...and the backstop drops any request to act while keeping advice-seeking pills.
    keep = ["How would I fix the bloat on orders?", "What should I change in autovacuum settings?",
            "Which index would help this query?", "Should I run vacuum on orders tonight?",
            "Show me the top slow queries", "Why is CPU high on billing-aurora?",
            "Compare this with yesterday's scan", "Is deleting old rows the cause of the bloat?",
            "What does the update pattern look like?"]
    drop = ["Run VACUUM ANALYZE on the orders table", "Can you kill the idle sessions?",
            "Please reindex the bloated indexes", "Let's create an index on orders(customer_id)",
            "Apply the fix now", "Kill the blocking query", "Increase work_mem to 64MB",
            "Vacuum the bloated tables", "Go ahead and terminate PID 4432",
            "Set autovacuum_vacuum_scale_factor=0.02", "Restart the database",
            "Drop the unused index idx_old", "Fix it", "Enable pg_stat_statements",
            "Run a manual vacuum on orders"]
    assert fu.drop_mutating_requests(keep + drop) == keep
    # Applied on the stream for every turn, not only when an investigation was offered.
    stream = [_line({"type": "text", "content": "orders is bloated."}), _line({"type": "result", "response": "r"})]
    out = _run(stream, lambda prompt, answer, model: ["Run VACUUM on orders", "Why is orders bloated?"])
    assert json.loads(out[-1])["items"] == ["Why is orders bloated?"]
    assert _run(stream, lambda prompt, answer, model: ["Run VACUUM on orders"]) == stream


# ── with_followups ─────────────────────────────────────────────────────────────

def _line(obj):
    return json.dumps(obj) + "\n"


async def _agen(lines):
    for line in lines:
        yield line


def _run(lines, gen, prompt="Which db first?", model="m"):
    async def go():
        return [l async for l in fu.with_followups(_agen(lines), prompt, model, generate=gen)]
    return asyncio.new_event_loop().run_until_complete(go())


def test_suggestions_follow_result_and_passthrough_is_unchanged():
    stream = [
        _line({"type": "text", "content": "billing-aurora "}),
        _line({"type": "tool_start", "name": "get_cloudwatch_metrics", "toolUseId": "t1"}),
        _line({"type": "text", "content": "is the one to look at."}),
        _line({"type": "result", "response": "done"}),
    ]
    seen = {}

    def gen(prompt, answer, model):
        seen.update(prompt=prompt, answer=answer, model=model)
        return ["Why is it slow?", "Show its replica lag"]

    out = _run(stream, gen)
    assert out[:-1] == stream                                     # pass-through byte-for-byte
    last = json.loads(out[-1])
    assert last == {"type": "suggestions", "kind": "questions", "items": ["Why is it slow?", "Show its replica lag"]}
    assert seen["answer"] == "billing-aurora is the one to look at."   # reassembled from text events
    assert seen["model"] == "m" and seen["prompt"] == "Which db first?"


def test_no_suggestions_on_interrupt_error_or_empty_answer():
    calls = []

    def gen(*a):
        calls.append(a)
        return ["never"]

    interrupted = [_line({"type": "text", "content": "x" * 20}), _line({"type": "interrupt", "interrupt_id": "i"})]
    errored = [_line({"type": "text", "content": "x" * 20}), _line({"type": "error", "error": "boom"}),
               _line({"type": "result", "response": "r"})]
    empty = [_line({"type": "result", "response": "Investigation complete"})]
    for s in (interrupted, errored, empty):
        assert _run(s, gen) == s
    assert calls == []


def test_generator_failure_or_empty_result_adds_nothing():
    stream = [_line({"type": "text", "content": "some answer text"}), _line({"type": "result", "response": "r"})]

    def boom(*a):
        raise RuntimeError("throttled")

    assert _run(stream, boom) == stream
    assert _run(stream, lambda *a: []) == stream


def test_disabled_by_env():
    stream = [_line({"type": "text", "content": "some answer text"}), _line({"type": "result", "response": "r"})]
    os.environ["CHAT_FOLLOWUPS"] = "off"
    try:
        assert _run(stream, lambda *a: ["x" * 20]) == stream
    finally:
        os.environ.pop("CHAT_FOLLOWUPS", None)


def test_caution_emitted_even_when_followups_off():
    # The destructive-SQL caution is a safety annotation, not a follow-up, so it must fire
    # even with CHAT_FOLLOWUPS=off (which suppresses suggestions).
    answer = "Here is the fix:\n\n```sql\nDELETE FROM customers;\n```\n"
    stream = [_line({"type": "text", "content": answer}), _line({"type": "result", "response": "r"})]
    os.environ["CHAT_FOLLOWUPS"] = "off"
    try:
        out = _run(stream, lambda *a: ["x" * 20])
    finally:
        os.environ.pop("CHAT_FOLLOWUPS", None)
    kinds = [json.loads(l).get("type") for l in out]
    assert kinds.count("caution") == 1, kinds
    assert "suggestions" not in kinds, kinds   # follow-ups stay off; the caution is independent
    caution = next(json.loads(l) for l in out if json.loads(l).get("type") == "caution")
    assert caution["items"] and caution["items"][0]["verb"] == "DELETE", caution


def test_no_caution_for_scoped_or_prose_sql():
    # A scoped delete in a block and an unguarded delete quoted in prose: neither is a
    # runnable, unguarded fix, so no caution line is added.
    answer = "Your `DELETE FROM orders` is slow.\n\n```sql\nDELETE FROM sessions WHERE expired;\n```\n"
    stream = [_line({"type": "text", "content": answer}), _line({"type": "result", "response": "r"})]
    out = _run(stream, lambda *a: [])   # generate returns nothing -> no suggestions either
    assert "caution" not in [json.loads(l).get("type") for l in out], out


def test_suggestions_line_is_redacted():
    line = fu.suggestions_line(["Check arn:aws:secretsmanager:us-east-1:123:secret:x", "ok question"])
    assert "secretsmanager" not in line and "[redacted-secret-arn]" in line
    assert json.loads(line)["type"] == "suggestions"


def test_is_context_prompt_detects_report_and_investigation_markers():
    assert fu.is_context_prompt("[REPORT CONTEXT] health report ... [END REPORT CONTEXT]\n\nUser: why?")
    assert fu.is_context_prompt("[INVESTIGATION CONTEXT] ... [END INVESTIGATION CONTEXT]\n\nUser: and?")
    assert not fu.is_context_prompt("What is the health of my fleet?")
    assert not fu.is_context_prompt("")


def test_report_context_turn_gets_no_server_suggestions():
    # A report-discussion turn carries the [REPORT CONTEXT] block; the client supplies the
    # report-grounded follow-ups, so the server must emit no suggestions line (and must not
    # even call the generator, which could produce live/investigation wording).
    stream = [_line({"type": "text", "content": "The report shows 53% dead tuples."}),
              _line({"type": "result", "response": "r"})]
    calls = []

    def gen(*a, **kw):
        calls.append(a)
        return ["Yes, start a full investigation on aurora-pg-database-1"]

    report_prompt = ("[REPORT CONTEXT] health report for aurora-pg-database-1 ...\n"
                     "[END REPORT CONTEXT]\n\nUser question about this report: why so many dead tuples?")
    out = _run(stream, gen, prompt=report_prompt)
    assert out == stream, "the stream passes through with no extra suggestions line"
    assert calls == [], "the generic follow-up generator is never called for a context turn"


def test_investigation_context_turn_gets_no_server_suggestions():
    stream = [_line({"type": "text", "content": "The investigation found lock contention."}),
              _line({"type": "result", "response": "r"})]
    calls = []
    inv_prompt = ("[INVESTIGATION CONTEXT] investigation 'Slow orders' ...\n"
                  "[END INVESTIGATION CONTEXT]\n\nUser follow-up question: what next?")
    out = _run(stream, lambda *a, **kw: (calls.append(a), ["Start another investigation"])[1], prompt=inv_prompt)
    assert out == stream and calls == []


def test_ordinary_chat_still_gets_server_suggestions():
    # Sanity: a non-context turn is unaffected — the generator runs and a suggestions line
    # is appended.
    stream = [_line({"type": "text", "content": "billing-aurora is the one to look at."}),
              _line({"type": "result", "response": "r"})]
    out = _run(stream, lambda *a, **kw: ["Why is billing-aurora busy?"])
    assert any(json.loads(l).get("type") == "suggestions" for l in out), \
        "ordinary chat still receives the server's follow-ups"


# ── generate_followups: temperature handling per model family ──────────────────
# Haiku 5.5 (the 5 line, and Opus 4.7+) reject `temperature` with a ValidationException;
# the call must omit it for those families and still return pills.

class _FakeBedrock:
    def __init__(self, reject_temperature=False, reject_effort=False, reject_thinking=False,
                 response=None):
        self.bodies = []
        self.model_ids = []
        self.reject_temperature = reject_temperature
        self.reject_effort = reject_effort
        self.reject_thinking = reject_thinking
        self.response = response or {"content": [{"type": "text", "text": '["Why is it slow?"]'}],
                                     "stop_reason": "end_turn"}

    def invoke_model(self, modelId, body, **kw):
        b = json.loads(body)
        self.bodies.append(b)
        self.model_ids.append(modelId)
        if self.reject_temperature and "temperature" in b:
            raise RuntimeError("ValidationException: `temperature` is deprecated for this model.")
        if self.reject_effort and "output_config" in b:
            raise RuntimeError("ValidationException: This model does not support the effort parameter.")
        if self.reject_thinking and "thinking" in b:
            raise RuntimeError("ValidationException: thinking is not supported by this model.")

        class _Body:
            def read(self_inner):
                return json.dumps(self.response).encode()
        return {"body": _Body()}


def _generate_with(fake, model_id, turn_model=""):
    """Run generate_followups with ``model_id`` as the deployment default (the helper
    model) and ``turn_model`` as the turn's own model."""
    import model_resolver as mr
    orig = fu._bedrock
    fu._bedrock = lambda: fake
    env = {k: os.environ.get(k) for k in ("AGENT_MODEL_ID", "CHAT_FOLLOWUPS_MODEL_ID")}
    os.environ["AGENT_MODEL_ID"] = model_id
    os.environ.pop("CHAT_FOLLOWUPS_MODEL_ID", None)
    try:
        return fu.generate_followups("what indexes can I drop?", "Drop idx_a.", turn_model)
    finally:
        fu._bedrock = orig
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for s in (mr._TEMPERATURE_UNSUPPORTED, mr._EFFORT_UNSUPPORTED, mr._THINKING_UNSUPPORTED):
            s.discard(model_id)


def test_haiku_5_gets_no_temperature_and_returns_pills():
    fake = _FakeBedrock(reject_temperature=True)
    items = _generate_with(fake, "eu.anthropic.claude-haiku-5-5")
    assert items == ["Why is it slow?"], f"pills come back on Haiku 5.5 ({items})"
    assert "temperature" not in fake.bodies[0], "temperature is not sent to the 5 line"
    assert len(fake.bodies) == 1, "no wasted retry"


def test_haiku_4_5_keeps_its_temperature():
    fake = _FakeBedrock()
    _generate_with(fake, "eu.anthropic.claude-haiku-4-5-20251001-v1:0")
    assert fake.bodies[0].get("temperature") == 0.4, "models that accept it keep temperature 0.4"


def test_bedrock_client_retries_and_has_a_workable_timeout():
    # The call runs after the answer, under load: the client retries throttles and allows
    # a realistic read window, and the step cap bounds the operator's total wait.
    assert fu.CALL_MAX_ATTEMPTS >= 2, "throttles need at least one retry"
    assert fu.CALL_TIMEOUT_S >= 12, "read timeout must survive a slow first token"
    assert fu.STEP_TIMEOUT_S >= fu.CALL_TIMEOUT_S, "the step cap cannot be shorter than one attempt"
    fu._client = None
    captured = {}
    import boto3 as _b
    real = _b.client
    def fake_client(name, **kw):
        captured["config"] = kw.get("config")
        return object()
    _b.client = fake_client
    try:
        fu._bedrock()
    finally:
        _b.client = real
        fu._client = None
    cfg = captured["config"]
    assert cfg.retries["max_attempts"] == fu.CALL_MAX_ATTEMPTS
    assert cfg.retries["mode"] == "adaptive", "adaptive mode backs off on ThrottlingException"
    assert cfg.read_timeout == fu.CALL_TIMEOUT_S


def test_timeout_is_logged_as_a_warning_not_swallowed():
    # A timed-out suggestion step leaves a WARNING naming the cause.
    import logging
    records = []
    h = logging.Handler(); h.emit = lambda r: records.append(r)
    fu.logger.addHandler(h)
    try:
        orig = fu.STEP_TIMEOUT_S
        fu.STEP_TIMEOUT_S = 0.05
        def slow(*a, **kw):
            import time; time.sleep(0.5); return ["x"]
        stream = [_line({"type": "text", "content": "answer"}), _line({"type": "result", "response": "r"})]
        out = _run(stream, slow)
        fu.STEP_TIMEOUT_S = orig
    finally:
        fu.logger.removeHandler(h)
    assert not any(json.loads(l).get("type") == "suggestions" for l in out), "no pills on timeout"
    warn = [r for r in records if r.levelno == logging.WARNING and "timed out" in r.getMessage()]
    assert warn, f"a timeout must log a WARNING naming it (got {[r.getMessage() for r in records]})"


def test_unknown_family_rejecting_temperature_is_retried_without_it():
    fake = _FakeBedrock(reject_temperature=True)
    items = _generate_with(fake, "eu.anthropic.claude-future-9")
    assert items == ["Why is it slow?"], "a family the markers miss still gets pills"
    assert "temperature" in fake.bodies[0] and "temperature" not in fake.bodies[1], \
        "first call sends it, the retry drops it"


# ── generate_followups: helper model and reasoning controls ─────────────────────
# The pills run on the deployment default model, never the turn's model, with thinking
# off and the lowest effort where the model accepts those controls, so a reasoning model
# spends the whole budget on the answer.

def test_pills_run_on_the_deployment_default_not_the_turn_model():
    fake = _FakeBedrock()
    _generate_with(fake, "eu.anthropic.claude-haiku-5-5", turn_model="eu.anthropic.claude-opus-5")
    assert fake.model_ids == ["eu.anthropic.claude-haiku-5-5"]


def test_followups_model_pin_overrides_the_default():
    fake = _FakeBedrock()
    orig = fu._bedrock
    fu._bedrock = lambda: fake
    prev = os.environ.get("CHAT_FOLLOWUPS_MODEL_ID")
    os.environ["CHAT_FOLLOWUPS_MODEL_ID"] = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
    try:
        fu.generate_followups("q", "answer", "eu.anthropic.claude-haiku-5-5")
    finally:
        fu._bedrock = orig
        if prev is None:
            os.environ.pop("CHAT_FOLLOWUPS_MODEL_ID", None)
        else:
            os.environ["CHAT_FOLLOWUPS_MODEL_ID"] = prev
    assert fake.model_ids == ["eu.anthropic.claude-haiku-4-5-20251001-v1:0"]


def test_haiku_5_gets_thinking_off_and_low_effort():
    fake = _FakeBedrock()
    _generate_with(fake, "eu.anthropic.claude-haiku-5-5")
    b = fake.bodies[0]
    assert b["thinking"] == {"type": "disabled"}
    assert b["output_config"] == {"effort": "low"}
    assert b["max_tokens"] == fu.MAX_TOKENS


def test_haiku_4_5_gets_thinking_off_but_no_effort():
    # Haiku 4.5 accepts the thinking block and rejects the effort parameter.
    fake = _FakeBedrock()
    _generate_with(fake, "eu.anthropic.claude-haiku-4-5-20251001-v1:0")
    b = fake.bodies[0]
    assert b["thinking"] == {"type": "disabled"}
    assert "output_config" not in b


def test_non_anthropic_model_gets_no_reasoning_controls():
    fake = _FakeBedrock()
    _generate_with(fake, "amazon.nova-lite-v1:0")
    b = fake.bodies[0]
    assert "thinking" not in b and "output_config" not in b


def test_model_rejecting_effort_is_retried_without_it_and_learned():
    import model_resolver as mr
    fake = _FakeBedrock(reject_effort=True)
    items = _generate_with(fake, "eu.anthropic.claude-haiku-5-5")
    assert items == ["Why is it slow?"]
    assert "output_config" in fake.bodies[0] and "output_config" not in fake.bodies[1]
    assert fake.bodies[1]["thinking"] == {"type": "disabled"}, "the other control stays"
    mr._EFFORT_UNSUPPORTED.discard("eu.anthropic.claude-haiku-5-5")


def test_model_rejecting_thinking_is_retried_without_it():
    fake = _FakeBedrock(reject_thinking=True)
    items = _generate_with(fake, "eu.anthropic.claude-haiku-5-5")
    assert items == ["Why is it slow?"]
    assert "thinking" in fake.bodies[0] and "thinking" not in fake.bodies[1]


def test_empty_text_at_max_tokens_is_a_named_warning():
    # A reasoning model that spends the whole budget thinking returns no text with
    # stop_reason max_tokens; the pills are skipped with a warning that names the budget.
    import logging
    records = []
    h = logging.Handler(); h.emit = lambda r: records.append(r)
    fu.logger.addHandler(h)
    try:
        fake = _FakeBedrock(response={"content": [{"type": "thinking", "thinking": ""}],
                                      "stop_reason": "max_tokens",
                                      "usage": {"output_tokens": 400}})
        items = _generate_with(fake, "eu.anthropic.claude-haiku-5-5")
    finally:
        fu.logger.removeHandler(h)
    assert items == []
    warn = [r.getMessage() for r in records if r.levelno == logging.WARNING]
    assert any("max_tokens" in m and "no text" in m for m in warn), warn


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {e!r}")
    sys.exit(1 if failures else 0)
