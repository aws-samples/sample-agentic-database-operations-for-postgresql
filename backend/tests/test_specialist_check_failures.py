# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Product rule 17 in chat: a specialist whose checks failed cannot be reported as healthy.

In a real trace every PostgreSQL check failed (PermissionError), the PostgreSQL specialist
answered with a request for a database id it already had, and the coordinator, holding
only the observability specialist's metrics, told the user the database was "running
smoothly". The wrapper of each specialist now counts its tool results from the stream and
puts a fixed marker with every failure (tool and error) at the top of the answer the
coordinator receives; the coordinator's prompts require it to surface that block.

Pinned, through the REAL postgres_agent and observability_agent wrappers with a fake
strands Agent that replays the trace's events:
  * every check failing -> the answer opens with the marker, "NO data", and each error;
  * some failing -> "N of M checks could not run", partial picture;
  * a tool that returns {"error": ...} instead of raising counts as failed;
  * every check succeeding -> the answer is passed through unchanged;
  * the specialist itself crashing -> the marker too, not a bare error string;
  * the chat and investigation prompts, and both specialists' prompts, carry the rule.

Hermetic: strands, psycopg2 and boto3 stubbed; no model, no AWS.
Run:  python3 backend/tests/test_specialist_check_failures.py
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

passed = failed = 0


def check(cond, label):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}")


SCRIPT = {"events": [], "raise": None}


class FakeAgent:
    def __init__(self, name="", **kw):
        self.name = name

    async def stream_async(self, question):
        if SCRIPT["raise"]:
            raise SCRIPT["raise"]
        for ev in SCRIPT["events"]:
            yield ev


_strands = types.ModuleType("strands")
_strands.Agent = FakeAgent
_strands.tool = lambda f=None, **kw: (f if f is not None else (lambda g: g))
sys.modules["strands"] = _strands
for _m in ("psycopg2", "psycopg2.extras", "boto3"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

import model_resolver as mr  # noqa: E402

mr.build_bedrock_model = lambda *a, **k: object()
mr.effective_model_id = lambda *a, **k: "model"

import importlib  # noqa: E402

# sub_agents/__init__ re-exports the tool functions under the module names, so the
# modules are fetched by their dotted path.
co = importlib.import_module("sub_agents.check_outcome")
pga = importlib.import_module("sub_agents.postgres_agent")
oba = importlib.import_module("sub_agents.observability_agent")

pga.build_bedrock_model = oba.build_bedrock_model = mr.build_bedrock_model
pga.effective_model_id = oba.effective_model_id = mr.effective_model_id

DB = "9e46bfaa-19d8-4fd5-9909-909918dc8ea8"
REFUSAL = "Error: PermissionError - Use a database alias from [DATABASE SCOPE]; secret ARNs are not accepted."


def tool_use(uid, name):
    return {"message": {"role": "assistant", "content": [
        {"toolUse": {"toolUseId": uid, "name": name, "input": {"secret_arn": DB}}}]}}


def tool_result(uid, status="success", text="{'rows': 1}", json_payload=None):
    content = [{"json": json_payload}] if json_payload is not None else [{"text": text}]
    return {"message": {"role": "user", "content": [
        {"toolResult": {"toolUseId": uid, "status": status, "content": content}}]}}


def final(text):
    return {"result": text}


def run(wrapper, events=(), raise_=None):
    SCRIPT["events"], SCRIPT["raise"] = list(events), raise_

    async def drain():
        out = []
        async for item in wrapper("health of the database"):
            out.append(item)
        return out

    items = asyncio.run(drain())
    return items[-1] if items else ""


TRACE = [tool_use(f"t{i}", n) for i, n in enumerate(
    ("get_health_summary", "get_replication_status", "get_current_activity", "get_buffer_pool_stats"))]
TRACE += [tool_result(f"t{i}", "error", REFUSAL) for i in range(4)]
TRACE += [final("I need the actual database instance ID. Could you please provide it?")]

print("== every check failed (the trace) ==")
ans = run(pga.postgres_agent, TRACE)
check(ans.startswith(co.CHECKS_FAILED_MARKER), "the answer opens with the marker")
check("None of this specialist's 4 check(s) ran" in ans and "NO data" in ans,
      "it says none of the 4 checks ran and there is no data")
check(all(f"- {n}: {REFUSAL}" in ans for n in
          ("get_health_summary", "get_replication_status", "get_current_activity", "get_buffer_pool_stats")),
      "each failed tool is listed with its error")
check("Do not call the database healthy" in ans, "the coordinator is told not to call it healthy")
check(ans.rstrip().endswith("Could you please provide it?"), "the specialist's own words follow, not replace")

print("")
print("== some checks failed ==")
ans = run(pga.postgres_agent, [tool_use("a", "get_health_summary"), tool_use("b", "get_lock_analysis"),
                               tool_use("c", "current_time"),
                               tool_result("a"), tool_result("b", "error", "Error: timeout"),
                               tool_result("c"), final("Locks look fine.")])
check(ans.startswith(f"{co.CHECKS_FAILED_MARKER} 1 of 2 checks could not run"),
      f"1 of 2 checks (current_time is not a check) ({ans.splitlines()[0]})")
check("partial" in ans.splitlines()[0] and "- get_lock_analysis: Error: timeout" in ans,
      "marked partial, with the failed tool and its error")

print("")
print("== a tool that returns {'error': ...} instead of raising counts as failed ==")
ans = run(oba.observability_agent, [tool_use("x", "get_cpu_utilization"), tool_use("y", "get_db_load"),
                                    tool_result("x", text="{'error': 'AccessDenied on GetMetricData'}"),
                                    tool_result("y", json_payload={"error": "PI not enabled"}),
                                    final("CPU is fine.")])
check(ans.startswith(co.CHECKS_FAILED_MARKER) and "None of this specialist's 2 check(s) ran" in ans,
      "both error payloads are failures")
check("AccessDenied on GetMetricData" in ans and "PI not enabled" in ans, "with their errors")

print("")
print("== every check ran ==")
ok = [tool_use("a", "get_health_summary"), tool_result("a"), final("All checks OK.")]
check(run(pga.postgres_agent, ok) == "All checks OK.", "postgres: the answer is unchanged")
check(run(oba.observability_agent, ok) == "All checks OK.", "observability: the answer is unchanged")
dup = [tool_use("a", "get_health_summary"), tool_result("a", "error", "boom"), tool_result("a", "error", "boom"),
       final("x")]
check("None of this specialist's 1 check(s) ran" in run(pga.postgres_agent, dup),
      "a result seen twice in the stream is counted once")

print("")
print("== the specialist itself could not run ==")
ans = run(pga.postgres_agent, raise_=RuntimeError("model throttled"))
check(ans.startswith(co.CHECKS_FAILED_MARKER) and "PostgreSQL specialist could not run" in ans
      and "model throttled" in ans, "postgres: the marker, naming why")
ans = run(oba.observability_agent, raise_=RuntimeError("model throttled"))
check(ans.startswith(co.CHECKS_FAILED_MARKER) and "observability specialist could not run" in ans,
      "observability: the marker too")

print("")
print("== the prompts carry the rule ==")
here = os.path.dirname(os.path.abspath(__file__))
app_src = open(os.path.join(here, "..", "app.py"), encoding="utf-8").read()
chat = app_src.split('SYSTEM_PROMPT = """', 1)[1].split('"""', 1)[0]
inv = app_src.split('INVESTIGATION_AGENT_PROMPT = """', 1)[1].split('"""', 1)[0]
check(co.CHECKS_FAILED_MARKER in chat and '"running smoothly"' in chat and "partial picture" in chat,
      "chat coordinator: surface the marker, never 'running smoothly' on failed checks")
check(co.CHECKS_FAILED_MARKER in inv and "incomplete" in inv,
      "investigation coordinator: failed checks are findings, the investigation is incomplete")
for name, prompt in (("postgres", pga.POSTGRES_AGENT_PROMPT), ("observability", oba.OBSERVABILITY_AGENT_PROMPT)):
    check("WHEN A CHECK FAILS, SAY SO FIRST" in prompt, f"{name} specialist: failures first")
check("Never ask the orchestrator or the user for a database id" in pga.POSTGRES_AGENT_PROMPT,
      "postgres specialist: a refused id is reported, not asked for again")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
