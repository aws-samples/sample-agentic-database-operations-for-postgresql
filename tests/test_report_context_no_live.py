#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Report and investigation discussion stay inside their context: no live, no launch (review #3).

CONFIRMED DESIGN: report-mode chat sends only the report text and no database scope. The
gap this fixes is the "Start investigation" affordance leaking into context chats and the
model offering to go live. Asserted against the operator-app source:

  * in report OR investigation context, "Start investigation" is NOT offered (the pill
    callback is withheld), so a context chat cannot launch a live run;
  * the [REPORT CONTEXT] / [INVESTIGATION CONTEXT] prompts instruct the model to answer
    only from the context, not to call a live tool, and not to suggest an investigation —
    a live question gets "open the database in a new chat" instead;
  * a report/investigation turn carries NO database scope (scopeDatabaseIds stays null);
  * wherever "Start investigation" is reachable, it targets the record's OWN databases,
    never the current picker or the whole space.

    python3 tests/test_report_context_no_live.py   (from the repository root)
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHAT = (ROOT / "frontend/src/components/Chat.jsx").read_text(encoding="utf-8")
APP = (ROOT / "frontend/src/App.jsx").read_text(encoding="utf-8")
passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


# ── the "Start investigation" affordance is withheld in context ──
print("══ no Start-investigation action in report/investigation context ══")
check("const inContext = !scopeApplies" in CHAT, "Chat computes whether a report/investigation context is active")
check("const startInvestigation = inContext ? undefined : onStartInvestigation" in CHAT,
      "the Start-investigation callback is withheld in context")
check(CHAT.count("onStartInvestigation={startInvestigation}") == 2
      and "onStartInvestigation={onStartInvestigation}" not in CHAT,
      "every ChatMessage receives the context-gated callback, not the raw one")

# ── the prompts forbid live tools and investigation suggestions ──
print("")
print("══ the context prompts stay inside the context ══")
# Grab the report-context prompt block.
rep = CHAT[CHAT.find("[REPORT CONTEXT]"): CHAT.find("[END REPORT CONTEXT]")]
check("do NOT call any live database tool" in rep, "report prompt: no live database tool")
check("do NOT suggest" in rep and "start an investigation" in rep, "report prompt: do not suggest an investigation")
check("new chat" in rep, "report prompt: a live check needs a new chat")
inv = CHAT[CHAT.find("[INVESTIGATION CONTEXT]"): CHAT.find("[END INVESTIGATION CONTEXT]")]
check("do NOT call any live database tool" in inv, "investigation prompt: no live database tool")
check("do NOT suggest or offer to start another investigation" in inv,
      "investigation prompt: do not suggest another investigation")

# ── a context turn carries no DB scope ──
print("")
print("══ a report/investigation turn carries no database scope ══")
# scopeDatabaseIds is initialised null and only assigned in the ordinary-chat branch.
check(re.search(r"let scopeDatabaseIds = null", CHAT) is not None, "scopeDatabaseIds starts null")
# The report and investigation branches set `prompt = ...` but never assign scopeDatabaseIds.
report_branch = CHAT[CHAT.find("if (chatMode === 'report' && reportContext?.analysis) {"):
                     CHAT.find("} else {", CHAT.find("[INVESTIGATION CONTEXT]"))]
check("scopeDatabaseIds =" not in report_branch,
      "neither the report nor investigation branch sets a database scope")

# ── Start investigation targets the record's own databases ──
print("")
print("══ Start investigation targets the record's own databases ══")
hsi = APP[APP.find("const handleStartInvestigation ="): APP.find("const openInvestigationById")]
check("investigationContext?.database_ids" in hsi,
      "an investigation context targets the record's database_ids")
check("activeReport?.instance_id" in hsi, "a report context targets the report's instance")
check("if (!dbs.length) return null" in hsi,
      "a context whose ids have not loaded does NOT widen to the whole space")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
