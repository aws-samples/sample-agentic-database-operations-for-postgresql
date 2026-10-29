# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Trace-redaction tests for streaming.event_parser.

The stream redactor masks THIS DEPLOYMENT'S identifiers in everything streamed to the
operator's browser (trace, tool inputs and results, answer text): secret, role and STS
ARNs, the account id inside any other ARN, the metrics store table names and addresses
inside the deployment's VPC CIDR (AGENT_VPC_CIDR). The monitored databases' diagnostic
data is kept: their endpoints, their clients' addresses and their own addresses.

The earlier rule masked every RDS endpoint and every RFC 1918 address. A real trace showed
the cost: "authentication failures from [redacted-ip]" about a client of the customer's
own database. The tests that pinned that rule now pin the narrower one.

Hermetic: strands is stubbed (tools.types imports Agent from it).

Run:  python3 backend/tests/test_trace_redaction.py   (or via pytest)
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_strands = types.ModuleType("strands")
_strands.tool = lambda f: f
_strands.Agent = object
sys.modules.setdefault("strands", _strands)

from streaming import event_parser as ep  # noqa: E402

SECRET = "arn:aws:secretsmanager:us-east-1:123456789012:secret:db-ro-AbCdEf"
ROLE = "arn:aws:iam::123456789012:role/dbops-agent-shop-abc123"
# The deployment's own metrics store: its two DynamoDB table names, as the agent runtime
# gets them (the store has no network endpoint).
STORE = "dbops-tier1-metrics"
STORE_RO = "dbops-tier2-scans"
# A monitored database: the customer's own, shown to operators in the members table.
MONITORED = "prod-cluster.cluster-xyz.us-east-1.rds.amazonaws.com"
os.environ["TIER1_METRICS_TABLE"] = STORE
os.environ["TIER2_SCANS_TABLE"] = STORE_RO
os.environ.pop("AGENT_VPC_CIDR", None)  # the default, 172.20.0.0/16


def test_redacts_secret_role_and_store_endpoint_in_text():
    txt = f"connecting to {STORE} using {SECRET} via {ROLE}"
    out = ep._redact_text(txt)
    assert SECRET not in out and "[redacted-secret-arn]" in out
    assert ROLE not in out and "[redacted-role-arn]" in out
    assert STORE not in out and "[redacted-endpoint]" in out


def test_metrics_store_endpoints_are_masked_case_insensitively():
    out = ep._redact_text(f"table {STORE} throttled, scans {STORE_RO}, upper {STORE.upper()}.")
    assert "dbops-tier" not in out.lower(), out
    assert out.count("[redacted-endpoint]") == 3, out
    assert out.endswith("[redacted-endpoint].") and "throttled" in out


def test_a_monitored_database_endpoint_is_kept():
    for txt in (f"connecting to {MONITORED}:5432", f"host={MONITORED}",
                "orders-1.abc123.us-east-1.rds.amazonaws.com is the writer"):
        assert ep._redact_text(txt) == txt, txt
    # a name that merely contains the store endpoint as a suffix is not the store
    other = "x" + STORE
    assert ep._redact_text(other) == other


def test_redacts_nested_values():
    val = {"secret_arn": SECRET, "nested": {"endpoint": STORE, "db": MONITORED,
                                             "notes": ["role=" + ROLE, "ok"]}}
    out = ep._redact_value(val)
    assert out["secret_arn"] == "[redacted-secret-arn]"
    assert out["nested"]["endpoint"] == "[redacted-endpoint]"
    assert out["nested"]["db"] == MONITORED
    assert out["nested"]["notes"][0] == "role=[redacted-role-arn]"
    assert out["nested"]["notes"][1] == "ok"


def test_leaves_ordinary_content_untouched():
    assert ep._redact_text("cache hit ratio is 99.8%") == "cache hit ratio is 99.8%"
    assert ep._redact_value({"count": 5, "name": "orders"}) == {"count": 5, "name": "orders"}
    # non-str scalars pass through unchanged
    assert ep._redact_value(42) == 42
    assert ep._redact_value(None) is None


# ── part (d): the redactor also masks account ids in non-role ARNs and the
#    deployment's own addresses, but must NOT touch investigation content. ──

def test_redacts_account_id_in_non_role_arn():
    # A DynamoDB / SNS / Batch ARN carries the account id even though it is not a role.
    for arn in (
        "arn:aws:dynamodb:us-east-1:123456789012:table/dbops-instances",
        "arn:aws:sns:us-east-1:123456789012:dbops-scan-complete",
        "arn:aws:batch:us-east-1:123456789012:job/abc123",
    ):
        out = ep._redact_text(f"denied on {arn}")
        assert "123456789012" not in out and "[redacted-arn]" in out, arn


def test_an_agent_vpc_address_is_masked():
    for ip in ("172.20.10.7", "172.20.0.1", "172.20.255.254"):
        out = ep._redact_text(f"connect from {ip}:5432 failed")
        assert ip not in out and "[redacted-ip]" in out, ip


def test_a_database_log_client_ip_is_kept():
    # The trace that prompted this: a client of the customer's database failing md5 auth.
    line = ('2026-10-06 04:56:00 UTC:10.20.30.40(49316):postgres@postgres:[11581]:FATAL:  '
            'password authentication failed for user "postgres"')
    assert ep._redact_text(line) == line
    # other private ranges outside the deployment's VPC, and the database's own address
    for ip in ("10.0.4.7", "172.16.3.9", "172.21.0.5", "192.168.1.20"):
        txt = f"client_addr={ip}"
        assert ep._redact_text(txt) == txt, ip


def test_a_custom_agent_vpc_cidr_is_honoured():
    os.environ["AGENT_VPC_CIDR"] = "10.20.0.0/16"
    try:
        out = ep._redact_text("from 10.20.30.40 and 172.20.10.7")
        assert out == "from [redacted-ip] and 172.20.10.7", out
    finally:
        os.environ.pop("AGENT_VPC_CIDR", None)
    # a malformed value falls back to the default range rather than masking nothing
    os.environ["AGENT_VPC_CIDR"] = "not-a-cidr"
    try:
        assert ep._redact_text("172.20.10.7") == "[redacted-ip]"
    finally:
        os.environ.pop("AGENT_VPC_CIDR", None)


def test_account_less_arn_and_public_numbers_are_left_alone():
    # An S3 bucket ARN has no account segment, so there is nothing to leak; leave it.
    assert ep._redact_text("arn:aws:s3:::my-reports-bucket") == "arn:aws:s3:::my-reports-bucket"
    # A version-like number outside the deployment's VPC must not be masked.
    assert ep._redact_text("postgres 14.2.1.0 build") == "postgres 14.2.1.0 build"
    assert ep._redact_text("8.8.8.8 is public") == "8.8.8.8 is public"
    # nor a longer dotted number that only contains an agent-VPC-shaped run
    assert ep._redact_text("oid 172.20.1.5.9") == "oid 172.20.1.5.9"
    assert ep._redact_text("999.20.1.5") == "999.20.1.5"


def test_investigation_content_is_preserved_deliberately():
    # The push-back: table names, SQL and database usernames are diagnostic content the
    # operator needs to see, not infrastructure identifiers. They pass through untouched.
    sql = "SELECT * FROM public.orders WHERE tenant_id = 5 -- run by dbops_ro"
    assert ep._redact_text(sql) == sql
    val = {"query": sql, "table": "public.orders", "role": "dbops_ro", "rows": 1200}
    assert ep._redact_value(val) == val


STS = "arn:aws:sts::123456789012:assumed-role/dbops-task/session-abc"


def test_redacts_sts_assumed_role_arn():
    # The form an AWS authorization failure names the caller in - it carries the account
    # id and the task-role name, so it must not reach the browser.
    out = ep._redact_text(f"User: {STS} is not authorized to perform secretsmanager:GetSecretValue")
    assert STS not in out and "[redacted-role-arn]" in out


def test_emit_redacts_recursively_and_stays_ndjson():
    line = ep._emit({"type": "interrupt", "reason": f"denied for {STS} on {SECRET}", "id": 3})
    assert line.endswith("\n")
    obj = __import__("json").loads(line)
    assert STS not in line and SECRET not in line
    assert "[redacted-role-arn]" in obj["reason"] and "[redacted-secret-arn]" in obj["reason"]
    # non-identifier fields are untouched — the trace stays informative
    assert obj["type"] == "interrupt" and obj["id"] == 3


def test_every_event_routes_through_the_redacting_serialiser():
    # The interrupt branch (and any event type added later) must not be able to bypass
    # redaction: every yield goes through _emit, and nothing serialises json.dumps directly.
    import pathlib
    src = pathlib.Path(ep.__file__).read_text(encoding="utf-8")
    yields = [ln.strip() for ln in src.splitlines() if ln.strip().startswith("yield ")]
    assert yields, "expected yield statements in the parser"
    for y in yields:
        assert y == "yield line" or y.startswith("yield _emit("), y
    assert "yield json.dumps(" not in src, "no event may be serialised without _emit"


def test_test_connection_error_fields_are_redacted():
    import pathlib
    app_src = (pathlib.Path(ep.__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    for field in ("secret_error", "registry_error", "connection_error"):
        assert f'result["{field}"] = str(e)' not in app_src, f"{field} returns a raw str(e)"
        assert f'result["{field}"] = _redact_identifiers(str(e))' in app_src, field


def test_batch_scan_does_not_log_the_secret_arn():
    import pathlib
    rs = (pathlib.Path(ep.__file__).resolve().parents[2] / "batch" / "run_scan.py").read_text(encoding="utf-8")
    assert 'logger.info(f"Fetching credentials from {secret_arn}")' not in rs
    assert "Fetching credentials from {secret_arn}" not in rs


def _app_src():
    import pathlib
    return (pathlib.Path(ep.__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")


def test_config_error_is_redacted_before_persisting():
    # part (b): the Configure failure path must not store raw str(e) into the
    # registry, where it is served to space members.
    src = _app_src()
    assert '"config_error": str(e)' not in src, "config_error persists raw str(e)"
    assert '"config_error": last_error,' not in src, "config_error persists raw last_error"
    assert '"config_error": network_result.get("error", ""),' not in src, \
        "config_error persists the raw network error"
    # each site now routes through the redactor
    assert '_redact_identifiers(last_error)' in src
    assert '_redact_identifiers(network_result.get("error", ""))' in src
    assert '_safe_err = _redact_identifiers(str(e))' in src


def test_app_registers_the_sanitizing_exception_handlers():
    # parts (a) and (c): the handlers are wired, and delegate to the tested helpers.
    src = _app_src()
    assert "@app.exception_handler(StarletteHTTPException)" in src, "no 5xx-sanitizing handler"
    assert "@app.exception_handler(RequestValidationError)" in src, "no 422 handler"
    assert "http_exception_body(exc.status_code, exc.detail)" in src
    assert "sanitize_validation_errors(exc.errors())" in src


def test_stream_redactor_catches_an_identifier_split_across_deltas():
    # Per-delta redaction let "arn:aws:secretsmanager:us-east-1:1234" + "56789012:secret:x-AbC"
    # through as two harmless halves. The stream redactor holds the unfinished word.
    from streaming.event_parser import _StreamRedactor
    r = _StreamRedactor()
    out = r.feed("The secret is arn:aws:secretsmanager:us-east-1:1234")
    out += r.feed("56789012:secret:dbops/x-AbCdEf and the store ")
    out += r.feed("dbops-tier1-me")
    out += r.feed("trics is up")
    out += r.flush()
    assert "arn:aws:secretsmanager" not in out and "123456789012" not in out
    assert "dbops-tier1" not in out
    assert "[redacted-secret-arn]" in out and "[redacted-endpoint]" in out
    assert out.endswith("is up"), "ordinary text is emitted intact and in order"


def test_stream_redactor_masks_an_agent_vpc_ip_split_across_chunks():
    from streaming.event_parser import _StreamRedactor
    r = _StreamRedactor()
    out = r.feed("the agent connected from 172.2")
    out += r.feed("0.10.7 and the client 10.20.")
    out += r.feed("30.40 failed auth")
    out += r.flush()
    assert out == "the agent connected from [redacted-ip] and the client 10.20.30.40 failed auth", out


def test_stream_redactor_emits_word_by_word_and_flushes_the_tail():
    from streaming.event_parser import _StreamRedactor
    r = _StreamRedactor()
    assert r.feed("hello") == ""            # no whitespace yet: held
    assert r.feed(" wor") == "hello "       # the completed word goes out
    assert r.feed("ld") == ""
    assert r.flush() == "world"


def test_parse_agent_events_wires_the_stream_redactor_and_replay_is_redacted():
    import pathlib as _pl
    src = (_pl.Path(__file__).resolve().parents[1] / "streaming" / "event_parser.py").read_text()
    assert "redactors.setdefault(\"__main__\", _StreamRedactor()).feed(" in src
    assert "redactors.setdefault(agent_name, _StreamRedactor()).feed(" in src
    assert "for line in _flush_all():" in src
    app = (_pl.Path(__file__).resolve().parents[1] / "app.py").read_text()
    assert '"messages": _redact_value(messages)' in app, "session replay is redacted on the way out"


def test_the_agent_runtime_gets_the_deployment_vpc_cidr_and_store_endpoint():
    # The redactor runs in the AgentCore runtime (it serves /invocations), so that is the
    # environment that must carry the VPC range and the store table names it masks.
    import pathlib as _pl, re as _re
    root = _pl.Path(__file__).resolve().parents[2]
    agent = (root / "backend" / "template.yaml").read_text(encoding="utf-8")
    runtime = agent[agent.index("EnvironmentVariables:"):agent.index("JWT_SIGNATURE_VERIFICATION:")]
    assert "AGENT_VPC_CIDR: !Ref AgentVpcCidr" in runtime
    assert "TIER1_METRICS_TABLE: !Ref Tier1MetricsTable" in runtime
    assert "TIER2_SCANS_TABLE: !Ref Tier2ScansTable" in runtime
    assert "\n  AgentVpcCidr:\n" in agent
    master = (root / "infrastructure" / "master.yaml").read_text(encoding="utf-8")
    stack = _re.search(r"^  AgentStack:.*?(?=^  \w)", master, _re.S | _re.M).group(0)
    assert "AgentVpcCidr: !Ref VpcCidr" in stack


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", fn.__name__, "-", e or "assertion failed")
    print("\n%d passed, %d failed" % (len(fns) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
