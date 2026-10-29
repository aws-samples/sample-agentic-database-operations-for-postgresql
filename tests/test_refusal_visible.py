#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A refused chat turn shows a visible message (review #4).

`/invocations` streams NDJSON that the chat UI parses line by line. Three refusals used
to `return` a bare dict instead: an empty prompt, an over-the-cap prompt, and a malformed
investigation marker. FastAPI serialises a bare dict as a plain JSON body, so the chat
client — which only renders `text`/`result` stream lines — showed nothing. All three now
go through `_refusal_stream`, which emits a `text` line (the wording) and a `result` line
(closing the turn), exactly like a normal answer.

Belt and braces on the client: `streamRequest` now flushes the final buffered line when
the reader reports `done`, so even a response whose last line is not newline-terminated
is still rendered rather than dropped.

Source-level assertions (importing app.py needs the full agent runtime); the stream
shape of `_refusal_stream` is exercised separately by the backend guard suite.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "backend" / "app.py"
HOOK = ROOT / "frontend" / "src" / "hooks" / "useStreaming.js"

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


app_src = APP.read_text(encoding="utf-8")
# Isolate the /invocations coroutine body so the assertions below are about this route.
inv = app_src.split("async def invocations(", 1)[1].split("\n@app.", 1)[0]

print("══ the three refusals ride the NDJSON stream, not a bare dict ══")
check('return {"error": "No prompt provided"}' not in inv,
      "the empty-prompt refusal no longer returns a bare dict")
check('return {"error": "Invalid investigation format"}' not in inv,
      "the malformed-investigation refusal no longer returns a bare dict")
check('Message is too large' not in inv,
      "the over-the-cap refusal no longer returns a bare dict")

# Each refusal is now a StreamingResponse(_refusal_stream(...)). Count the refusal-stream
# uses that carry the three user-facing wordings.
for needle, label in [
    ("That message was empty", "empty prompt"),
    ("That message is too long", "over the character cap"),
    ("That investigation request was malformed", "malformed investigation marker"),
]:
    # The wording must appear inside a _refusal_stream( ... ) call in /invocations.
    pat = re.compile(r"_refusal_stream\(\s*(?:f?\")?[^)]*" + re.escape(needle), re.DOTALL)
    check(bool(pat.search(inv)),
          f"the {label} refusal is routed through _refusal_stream")

check(inv.count("_refusal_stream(") >= 5,
      "all refusal paths (3 new + rate-limit + guardrail) go through _refusal_stream")

print("")
print("══ _refusal_stream emits a visible turn (text + result) ══")
rs = app_src.split("async def _refusal_stream(", 1)[1].split("\nasync def ", 1)[0]
check('"type": "text"' in rs and '"type": "result"' in rs,
      "_refusal_stream yields a text line (the message) and a result line (turn close)")

print("")
print("══ the client flushes the final buffered line at end-of-stream ══")
hook_src = HOOK.read_text(encoding="utf-8")
done_branch = hook_src.split("if (done)", 1)[1].split("}", 1)[0] if "if (done)" in hook_src else ""
# The done branch must do more than a bare break: it flushes whatever is left in buffer.
check("buffer" in done_branch and ("emitLine" in done_branch or "onEvent" in done_branch),
      "the done branch flushes the residual buffer instead of discarding it")
check("if (done) break" not in hook_src,
      "the old discard-on-done path is gone")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
