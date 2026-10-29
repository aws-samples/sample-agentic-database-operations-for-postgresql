# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""What a specialist's checks actually did, carried to the coordinator in its answer.

Nothing reports success when it failed. If a specialist whose tools all errored hands
the coordinator a free-text answer (for example, a request for a database id it has
already been given), the coordinator, holding only the other specialist's data, can call
the database "running smoothly". Prompt text alone does not prevent this.

So the specialist wrapper counts its own tool results from the event stream and, when
any check failed, puts a marker and the failures (tool and error) at the top of the
answer the coordinator receives. The coordinator's prompt requires it to surface that
block. The count comes from the tool results themselves, not from the specialist's
wording, so a specialist that glosses over its errors cannot hide them.

Pure: no strands import, so it is testable on its own.
"""

CHECKS_FAILED_MARKER = "[SPECIALIST CHECKS FAILED]"

# Tools that gather no data about the database: they neither count as a check that ran
# nor as one that failed.
_NOT_A_CHECK = {"current_time"}
_MAX_ERROR_CHARS = 300


def _result_text(tool_result: dict) -> str:
    for block in tool_result.get("content") or []:
        if isinstance(block, dict) and "text" in block:
            return str(block["text"])
        if isinstance(block, dict) and isinstance(block.get("json"), dict):
            return str(block["json"])
    return ""


def _error_of(tool_result: dict) -> str:
    """The error a tool result reports, or '' when it carries data. A raised exception
    arrives as status=error; a tool that catches its own failure returns a dict whose
    only meaning is its "error" key (``{"error": "..."}``)."""
    text = _result_text(tool_result).strip()
    if tool_result.get("status") == "error":
        return text or "the tool failed without a message"
    for block in tool_result.get("content") or []:
        payload = block.get("json") if isinstance(block, dict) else None
        if isinstance(payload, dict) and payload.get("error"):
            return str(payload["error"])
    if text.startswith(("{'error':", '{"error":')):
        return text
    return ""


class CheckOutcomes:
    """Counts a specialist's tool calls and the ones that failed, from its stream."""

    def __init__(self):
        self._names: dict = {}
        self._seen: set = set()
        self.total = 0
        self.failures: list = []  # (tool name, error text)

    def observe(self, event) -> None:
        message = event.get("message") if isinstance(event, dict) else None
        if not isinstance(message, dict):
            return
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            use = block.get("toolUse")
            if isinstance(use, dict) and use.get("toolUseId"):
                self._names[use["toolUseId"]] = use.get("name") or ""
            result = block.get("toolResult")
            if not isinstance(result, dict):
                continue
            use_id = result.get("toolUseId")
            if use_id in self._seen:
                continue
            self._seen.add(use_id)
            name = self._names.get(use_id, "") or "unknown tool"
            if name in _NOT_A_CHECK:
                continue
            self.total += 1
            error = _error_of(result)
            if error:
                self.failures.append((name, error[:_MAX_ERROR_CHARS]))

    def annotate(self, answer: str) -> str:
        """``answer`` unchanged when every check ran; otherwise the marker block first."""
        if not self.failures:
            return answer
        failed = len(self.failures)
        if failed == self.total:
            headline = (f"{CHECKS_FAILED_MARKER} None of this specialist's {self.total} "
                        f"check(s) ran, so it has NO data on the database.")
        else:
            headline = (f"{CHECKS_FAILED_MARKER} {failed} of {self.total} checks could not "
                        f"run; the picture below is partial.")
        seen, lines = set(), []
        for name, error in self.failures:
            if (name, error) in seen:
                continue
            seen.add((name, error))
            lines.append(f"- {name}: {error}")
        return (headline + "\n" + "\n".join(lines)
                + "\nTell the user plainly which checks did not run and why. Do not call the "
                  "database healthy on the strength of the checks that did."
                + "\n\nSpecialist's answer:\n" + (answer or ""))


def failed_specialist_answer(specialist: str, error: Exception) -> str:
    """The answer when the specialist itself could not run (model or framework error)."""
    return (f"{CHECKS_FAILED_MARKER} The {specialist} could not run, so it has NO data on "
            f"the database: {error}"
            "\nTell the user plainly that this check did not run and why.")
