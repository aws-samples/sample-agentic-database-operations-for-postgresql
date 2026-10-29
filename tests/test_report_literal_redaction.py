#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tier 2 report-context literal redaction.

The query text in a Tier 2 scan is the monitored database's OWN application SQL, observed
via pg_stat_activity/pg_stat_statements. An app that inlines values instead of binding
parameters can put a customer value (email, SSN, card number, API key) into that text. The
durable, model-facing ``report-context.txt`` masks those literal VALUES while keeping the
query SHAPE; the human-readable HTML report is left raw (the DBA's own view of their own DB).

Property: `_redact_sql_literals` blanks value-like single-quoted literals and leaves the
query shape, numeric-free short words and prose apostrophes intact.

Hermetic: boto3 and tools.resource_names are stubbed so batch/run_scan imports without AWS.

Run:  python3 tests/test_report_literal_redaction.py   (from the repository root)
"""
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "batch"))

# batch/run_scan imports boto3 and tools.resource_names at module load; stub both.
sys.modules.setdefault("boto3", types.ModuleType("boto3"))
_fake_rn = types.ModuleType("tools.resource_names")
_fake_rn.reports_bucket = lambda: "fake-bucket"
_fake_rn.table_name = lambda *a, **k: "fake-table"
_tools_pkg = types.ModuleType("tools")
sys.modules.setdefault("tools", _tools_pkg)
sys.modules["tools.resource_names"] = _fake_rn
# run_scan also takes its TLS settings from tools.aws.db_tls; stub it the same way.
_fake_aws = types.ModuleType("tools.aws")
_fake_tls = types.ModuleType("tools.aws.db_tls")
_fake_tls.ssl_kwargs = lambda *a, **k: {"sslmode": "verify-full"}
sys.modules.setdefault("tools.aws", _fake_aws)
sys.modules["tools.aws.db_tls"] = _fake_tls
# run_scan imports the shared XID-wraparound denominator (tools.xid); stub it.
_fake_xid = types.ModuleType("tools.xid")
_fake_xid.XID_WRAPAROUND_MAX = 2_147_483_647
sys.modules["tools.xid"] = _fake_xid

import run_scan  # noqa: E402

r = run_scan._redact_sql_literals
problems = 0


def check(cond, msg):
    global problems
    print(("  ok   " if cond else "  ❌ ") + msg)
    if not cond:
        problems += 1


print("── value-like literals are masked (values gone, shape kept) ──")
check(r("SELECT * FROM users WHERE email = 'jane.doe@acme.com'")
      == "SELECT * FROM users WHERE email = '?'", "email literal is masked")
check(r("SELECT * FROM cards WHERE pan = '4111111111111111'")
      == "SELECT * FROM cards WHERE pan = '?'", "card-number literal is masked")
check(r("UPDATE accounts SET api_key = 'sk-live-9f83aa20bc77' WHERE id = 42")  # gitleaks:allow (fake key)
      == "UPDATE accounts SET api_key = '?' WHERE id = 42", "long key masked, numeric id kept (shape)")
check("WHERE ssn = '?'" in r("SELECT 1 FROM t WHERE ssn = '123-45-6789'"),
      "SSN-like literal is masked")

print("")
print("── the query SHAPE and ordinary text survive ──")
check(r("SELECT id, status FROM orders WHERE customer_id = $1 AND status = $2")
      == "SELECT id, status FROM orders WHERE customer_id = $1 AND status = $2",
      "a parameterized query is unchanged (no literals to mask)")
check(r("status = 'ok'") == "status = 'ok'",
      "a short, digit-free quoted word (a real enum value) is left intact")
check(r("the table's rows and the index's size") == "the table's rows and the index's size",
      "prose apostrophes are not treated as a quoted literal to blank")

print("")
print("── wired into the durable/model copy, not the HTML report ──")
src = (ROOT / "batch/run_scan.py").read_text(encoding="utf-8")
check("_redact_sql_literals(text_content)" in src,
      "report-context.txt is redacted before upload")
# the HTML body is uploaded raw (not passed through the redactor)
check("_redact_sql_literals(html_content)" not in src,
      "the human-readable HTML report is left raw for the DBA")

print("")
print("── every stored section's query text is masked at write time (deep_sections) ──")
import deep_sections  # noqa: E402

SECRET = "SELECT * FROM users WHERE email = 'alice@example.com' AND token = 'sk-live-9f83aa20bc77'"  # gitleaks:allow (fake token)


class FakeCursor:
    """Answers the deep-section queries with rows carrying a value-bearing query text;
    anything else (catalog views this test does not model) returns no rows."""
    def __init__(self):
        self.sql = []
        self._rows = []

    def execute(self, sql):
        self.sql.append(sql)
        if "FROM pg_stat_statements" in sql:
            self._rows = [{"query": SECRET, "calls": 10, "total_exec_time": 1200.0, "mean_exec_time": 120.0,
                           "wal_bytes": 4 * 1024 * 1024, "temp_blks_written": 7, "user": "app"}]
        elif "pg_blocking_pids" in sql:
            self._rows = [{"blocked_pid": 1, "blocking_pid": 2, "blocked_query": SECRET, "blocking_query": SECRET}]
        elif "query_start < now() - interval" in sql:
            self._rows = [{"pid": 3, "user": "app", "query": SECRET, "running_sec": 90}]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


cur = FakeCursor()
secs = deep_sections.extract_sections(cur, {"activity": {"connection_utilization_pct": 40.0}}, r)
check(set(secs) == {"overview", "configuration", "security", "activity", "replication", "footprint",
                    "performance", "maintenance", "optimization"}, f"the nine sections ({sorted(secs)})")
check(all(sql.lstrip().upper().startswith(("SELECT", "WITH")) for sql in cur.sql),
      "every deep-section statement is a SELECT")


def strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from strings(v)


leaks = [t for t in strings(secs) if "alice@example.com" in t or "sk-live" in t]
check(not leaks, f"no literal value survives in any stored section ({len(leaks)} leak(s))")
perf = secs["performance"]
check(perf.get("top_queries_by_time") and "email = '?'" in perf["top_queries_by_time"][0]["query"],
      "top queries keep their shape with the literals masked")
check(secs["activity"]["long_running_queries"][0]["query"].count("'?'") == 2
      and secs["activity"]["blocking"][0]["blocking_query"].count("'?'") == 2,
      "long-running and blocking query text is masked too")
m = deep_sections.tier2_metrics(secs, {"activity": {"connection_utilization_pct": 40.0}}, "warning",
                                [{"severity": "WARNING"}, {"severity": "CRITICAL"}])
check(m["findings_total"] == 2 and m["findings_critical"] == 1 and m["connection_utilization_pct"] == 40.0
      and m["top_query_wal_mb"] == 4.0, "tier2_metrics: the daily numerics")
check(all(isinstance(v, float) for v in m.values()), "every published value is a number (missing → omitted)")
import ast  # noqa: E402
_tree = ast.parse((ROOT / "backend/tools/aws/metrics_store.py").read_text(encoding="utf-8"))
TIER1 = next(ast.literal_eval(n.value) for n in _tree.body if isinstance(n, ast.Assign)
             and any(getattr(t, "id", "") == "TIER1_NUMERIC_METRICS" for t in n.targets))
overlap = set(m) & set(TIER1)
check(not overlap, f"no Tier 2 metric reuses a Tier 1 series name ({sorted(overlap)})")
src = (ROOT / "batch/run_scan.py").read_text(encoding="utf-8")
check("extract_sections(cur, data, _redact_sql_literals)" in src, "run_scan passes the literal mask to the extraction")
print("")
if problems:
    print(f"FAIL — {problems} problem(s)")
    sys.exit(1)
print("PASS")
