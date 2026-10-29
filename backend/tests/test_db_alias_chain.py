# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The model's database aliases resolve, through the REAL chain, to the right database.

The model-attached guardrail keeps its AWS_SECRET_KEY PII filter, which garbled pieces of
the database-id UUIDs the [DATABASE SCOPE] block used to carry (a real trace showed
"{AWS_SECRET_KEY}09-9099{AWS_SECRET_KEY}" in a delegation). So the scope now names each
database db-1, db-2, ... and /invocations keeps alias -> registry id in
model_resolver.CURRENT_DB_ALIASES. This test pins that the mapping reaches the tools:

    request task (CURRENT_USER_ID, CURRENT_SPACE_ID, CURRENT_DB_ALIASES set as
    /invocations sets them, from surfaces.scope.alias_map over the scope's records)
      -> coordinator delegates to postgres_agent / observability_agent with the alias
      -> the specialist calls its tool, which strands runs in a worker thread
         (asyncio.create_task + asyncio.to_thread, both of which copy contextvars)
      -> db_tools.get_health_summary -> pg_client.get_connection -> db_aliases
         -> resolve_credential -> STS -> Secrets Manager -> psycopg2.connect
      -> db_tools.get_instance_details -> session_factory -> RDS

Two modes. With strands-agents installed (the backend image, or any env with
backend/requirements.txt) the coordinator and both specialists are REAL strands Agents
driven by a scripted model, so strands' own tool executor makes the thread hop. Without
it (the hermetic `make test` interpreter) the same hop is made with the primitives
strands 1.45 uses for it (asyncio.create_task for each tool call, asyncio.to_thread for a
sync tool). Fakes only at the edges either way: boto3 and psycopg2.

Run:  python3 backend/tests/test_db_alias_chain.py
"""
import asyncio
import json
import os
import re
import sys
import threading
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

try:  # real strands first, with the real boto3 it imports
    from strands import Agent
    from strands.models.model import Model
    REAL_STRANDS = True
except Exception:  # noqa: BLE001 - not installed in the hermetic interpreter
    REAL_STRANDS = False

passed = failed = 0


def check(cond, label):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}")


DEPLOYMENT_ACCOUNT = "111111111111"
MEMBER_ACCOUNT = "222222222222"
SPACE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_SPACE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OWNER = "owner-admin-sub"
ROLE = "dbops-agent-f4489458a071"


def _record(rid, name, space=SPACE):
    secret = f"arn:aws:secretsmanager:us-east-1:{MEMBER_ACCOUNT}:secret:dbops/{OWNER}/{name}/ro-AbCdEf"
    return {
        "instance_id": rid, "display_name": name, "status": "configured",
        "account_id": MEMBER_ACCOUNT, "region": "us-east-1", "engine": "aurora-postgresql",
        "endpoint": f"{name}.cluster-abc.us-east-1.rds.amazonaws.com", "port": 5432,
        "database_name": "postgres", "secret_arn": secret, "reach_space_id": space,
        "member_space_ids": {space}, "space_secrets": {space: secret},
        "tags": {"raw_identifier": f"{name}-cluster",
                 "arn": f"arn:aws:rds:us-east-1:{MEMBER_ACCOUNT}:cluster:{name}-cluster"},
    }


ORDERS = _record("9e46bfaa-19d8-4fd5-9909-909918dc8ea8", "orders")
BILLING = _record("0b1c2d3e-4f50-4a6b-8c7d-8e9fa0b1c2d3", "billing")
LEDGER = _record("5a5a5a5a-1111-4222-8333-944444444444", "audit-ledger")  # in the space, not in the turn
RECORDS = {r["instance_id"]: r for r in (ORDERS, BILLING, LEDGER)}
SECRET_HOST = {r["secret_arn"]: r["endpoint"] for r in RECORDS.values()}
SPACE_ROW = {"space_id": SPACE, "owner_admin": OWNER, "secondary_role_names": {MEMBER_ACCOUNT: ROLE}}

CALLS = []  # (edge, detail)
MAIN_THREAD = threading.get_ident()


# ── fake boto3 (the AWS edge) ───────────────────────────────────────────────────
class FakeTable:
    def __init__(self, name):
        self.name = name

    def get_item(self, Key=None, **kw):
        if "space_id" in (Key or {}):
            return {"Item": dict(SPACE_ROW)} if Key["space_id"] == SPACE else {}
        rec = RECORDS.get((Key or {}).get("instance_id"))
        return {"Item": dict(rec)} if rec else {}

    def scan(self, **kw):
        return {"Items": [dict(r) for r in RECORDS.values()] if "instances" in self.name else []}


class FakeSts:
    def get_caller_identity(self):
        return {"Account": DEPLOYMENT_ACCOUNT}

    def assume_role(self, RoleArn, RoleSessionName, DurationSeconds, ExternalId):
        CALLS.append(("sts.assume_role", (RoleArn, ExternalId)))
        return {"Credentials": {"AccessKeyId": "AKIDFAKE", "SecretAccessKey": "fake", "SessionToken": "fake"}}


class FakeSecrets:
    def __init__(self, session):
        self.session = session

    def get_secret_value(self, SecretId):
        CALLS.append(("secretsmanager.get_secret_value", SecretId))
        return {"SecretString": json.dumps({"username": "dbops_ro", "password": "pw",
                                            "host": SECRET_HOST[SecretId], "port": 5432})}


class NotFound(Exception):
    pass


class FakeRds:
    def __init__(self, session):
        self.exceptions = types.SimpleNamespace(DBClusterNotFoundFault=NotFound)

    def describe_db_clusters(self, DBClusterIdentifier):
        CALLS.append(("rds.describe_db_clusters", DBClusterIdentifier))
        return {"DBClusters": [{"DBClusterIdentifier": DBClusterIdentifier, "EngineVersion": "17.7",
                                "DBClusterMembers": [{"DBInstanceIdentifier": f"{DBClusterIdentifier}-1",
                                                      "IsClusterWriter": True}]}]}

    def describe_db_instances(self, DBInstanceIdentifier):
        return {"DBInstances": [{"DBInstanceIdentifier": DBInstanceIdentifier, "DBInstanceClass": "db.r6g.large",
                                 "AvailabilityZone": "us-east-1a", "DBInstanceStatus": "available"}]}


class FakeSession:
    def __init__(self, region_name=None, aws_access_key_id=None, **kw):
        self.region_name = region_name

    def client(self, name, region_name=None, config=None):
        return {"secretsmanager": FakeSecrets, "rds": FakeRds}[name](self)


_boto3 = types.ModuleType("boto3")
_boto3.Session = FakeSession
_boto3.resource = lambda *a, **k: types.SimpleNamespace(Table=FakeTable)
_boto3.client = lambda name, **k: FakeSts()
_real_boto3 = sys.modules.get("boto3")
sys.modules["boto3"] = _boto3
_cond = types.ModuleType("boto3.dynamodb.conditions")
_cond.Key = _cond.Attr = lambda *a, **k: types.SimpleNamespace(
    eq=lambda *x: None, is_in=lambda *x: None, exists=lambda: None)
sys.modules["boto3.dynamodb"] = types.ModuleType("boto3.dynamodb")
sys.modules["boto3.dynamodb.conditions"] = _cond
try:
    import importlib
    importlib.import_module("botocore.config")
    importlib.import_module("botocore.exceptions")
except ImportError:
    _bcc = types.ModuleType("botocore.config")
    _bcc.Config = lambda **k: None
    _bce = types.ModuleType("botocore.exceptions")
    _bce.ClientError = type("ClientError", (Exception,), {})
    sys.modules.update({"botocore": types.ModuleType("botocore"), "botocore.config": _bcc,
                        "botocore.exceptions": _bce})


# ── fake psycopg2 (the database edge) ───────────────────────────────────────────
class FakeCursor:
    description = [("col",)]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        self._rows = [{"conn_pct": 4.0, "current_conn": 8, "max_conn": 200, "xid_pct": 1.0,
                       "xid_age": 2000, "freeze_max_age": 200000000, "cache_hit_ratio": 99.9,
                       "long_queries": 0, "waiting_queries": 0, "needs_vacuum": 0,
                       "total_tables": 10, "blocked_queries": 0}]

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def cursor(self, cursor_factory=None):
        return FakeCursor()

    def set_session(self, **kw):
        pass

    def close(self):
        pass


def _connect(**kw):
    CALLS.append(("pg.connect", {"host": kw.get("host"), "thread": threading.get_ident()}))
    return FakeConn()


_pg = types.ModuleType("psycopg2")
_pg.connect = _connect
_pg.Error = type("Error", (Exception,), {})
_pg.OperationalError = type("OperationalError", (_pg.Error,), {})
_pg.extras = types.ModuleType("psycopg2.extras")
_pg.extras.RealDictCursor = object
sys.modules["psycopg2"] = _pg
sys.modules["psycopg2.extras"] = _pg.extras

if not REAL_STRANDS:
    _strands = types.ModuleType("strands")
    _strands.tool = lambda f=None, **kw: (f if f is not None else (lambda g: g))
    _strands.Agent = object
    sys.modules["strands"] = _strands

os.environ["AWS_REGION"] = "us-east-1"

import model_resolver as mr  # noqa: E402
from surfaces.scope import alias_map, chat_scope_block  # noqa: E402
from tools import db_tools  # noqa: E402
from tools.aws import db_aliases  # noqa: E402
from tools.aws import pg_client as pg  # noqa: E402
from tools.aws import session_factory as sf  # noqa: E402

UUID_SHAPE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
HANDLE = re.compile(r"(arn:aws:secretsmanager:\S+|db-\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def edges(name):
    return [d for e, d in CALLS if e == name]


# ── the specialists ─────────────────────────────────────────────────────────────
if REAL_STRANDS:
    sys.modules["boto3"] = _real_boto3 or _boto3  # strands' own Bedrock import, untouched
    import importlib as _il  # noqa: E402
    # The modules, not the package's same-named tool exports.
    oa_mod = _il.import_module("sub_agents.observability_agent")
    pa_mod = _il.import_module("sub_agents.postgres_agent")
    sys.modules["boto3"] = _boto3

    class ScriptedModel(Model):
        """Calls ONE tool with the handle found in the latest user text, then answers."""

        def __init__(self, plan):
            self.plan = plan  # (tool name, lambda handle: tool input)

        def update_config(self, **kw):
            pass

        def get_config(self):
            return {}

        async def structured_output(self, *a, **k):  # pragma: no cover - unused
            raise NotImplementedError
            yield

        async def stream(self, messages, tool_specs=None, system_prompt=None, **kw):
            last = messages[-1]
            done = any("toolResult" in b for b in last.get("content") or [])
            yield {"messageStart": {"role": "assistant"}}
            if done:
                yield {"contentBlockDelta": {"delta": {"text": "done"}}}
                yield {"contentBlockStop": {}}
                yield {"messageStop": {"stopReason": "end_turn"}}
                return
            text = " ".join(b.get("text", "") for b in last.get("content") or [] if "text" in b)
            name, make = self.plan
            handle = HANDLE.findall(text)[-1]
            yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"t-{name}", "name": name}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(make(handle))}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}

    pa_mod.build_bedrock_model = lambda *a, **k: ScriptedModel(
        ("get_health_summary", lambda h: {"secret_arn": h, "database": "postgres"}))
    oa_mod.build_bedrock_model = lambda *a, **k: ScriptedModel(
        ("get_instance_details", lambda h: {"identifier": h}))

    async def _delegate(specialist, question):
        """A real coordinator Agent that delegates `question` to the specialist tool."""
        tool = {"postgres": pa_mod.postgres_agent, "observability": oa_mod.observability_agent}[specialist]
        name = {"postgres": "postgres_agent", "observability": "observability_agent"}[specialist]
        coord = Agent(model=ScriptedModel((name, lambda h: {"question": question})),
                      tools=[tool], callback_handler=None)
        texts = []
        async for ev in coord.stream_async(f"[DATABASE SCOPE] ...\n\nplease check {question}"):
            texts.append(json.dumps(ev, default=str))
        return "\n".join(texts)
else:
    async def _delegate(specialist, question):
        """The hop strands makes: each tool call is a task; a sync tool goes to a thread."""
        handle = HANDLE.findall(question)[-1]

        async def specialist_tool():
            fn, kw = ((db_tools.get_health_summary, {"secret_arn": handle, "database": "postgres"})
                      if specialist == "postgres" else (db_tools.get_instance_details, {"identifier": handle}))
            try:
                return json.dumps(await asyncio.to_thread(fn, **kw), default=str)
            except Exception as e:  # noqa: BLE001 - strands returns it as a tool error
                return f"Error: {type(e).__name__} - {e}"
        return await asyncio.create_task(specialist_tool())


def turn(records, handle, specialist="postgres", space=SPACE, fresh=True):
    """One agent request, published the way /invocations publishes it."""
    if fresh:
        pg._secrets_cache.clear()
        pg._secret_space_cache.clear()
        sf._db_record_cache.clear()
        sf._space_cache.clear()
        sf._agent_account_id = None
        sf._sts_client = None
    CALLS.clear()

    async def request():
        mr.CURRENT_USER_ID.set("operator-1")
        mr.CURRENT_SPACE_ID.set(space)
        mr.CURRENT_DB_ALIASES.set(None)
        block = chat_scope_block(records, selected=True)
        if block:
            mr.CURRENT_DB_ALIASES.set(alias_map(records))
        return await _delegate(specialist, f"secret_arn={handle}, database postgres")
    return asyncio.run(request())


print(f"mode: {'real strands Agents and executor' if REAL_STRANDS else 'strands hop primitives (strands not installed)'}")
SCOPE = [ORDERS, BILLING]
print(f"scope aliases: {alias_map(SCOPE)}")
block = chat_scope_block(SCOPE, selected=True)
check(not UUID_SHAPE.search(block) and "db-1: billing" in block and "db-2: orders" in block,
      "the scope block names billing db-1 and orders db-2, and carries no UUID")

print("")
print("== db-2 reaches orders through the coordinator, the specialist and the tool thread ==")
out = turn(SCOPE, "db-2")
conns = edges("pg.connect")
check([c["host"] for c in conns] == [ORDERS["endpoint"]], f"dialled orders' endpoint ({conns})")
check(edges("secretsmanager.get_secret_value") == [ORDERS["secret_arn"]], "with orders' secret")
check(edges("sts.assume_role") == [(f"arn:aws:iam::{MEMBER_ACCOUNT}:role/{ROLE}", OWNER)],
      "through the owner's role and ExternalId")
check(bool(conns) and conns[0]["thread"] != MAIN_THREAD, "the tool ran in a worker thread, not the request's")
check(db_aliases.REFUSAL not in out, "no refusal")

print("")
print("== db-1 reaches billing ==")
turn(SCOPE, "db-1")
check([c["host"] for c in edges("pg.connect")] == [BILLING["endpoint"]], "dialled billing's endpoint")

print("")
print("== an alias not in the scope is refused, before any AWS call ==")
out = turn(SCOPE, "db-9")
check(db_aliases.REFUSAL in out, "the refusal names the fix: Use a database alias from [DATABASE SCOPE].")
check(not edges("pg.connect") and not edges("secretsmanager.get_secret_value") and not edges("sts.assume_role"),
      "nothing is read or dialled")

print("")
print("== an alias belongs to its request: another request's db-2 is another database or nothing ==")
turn([LEDGER], "db-1")
check([c["host"] for c in edges("pg.connect")] == [LEDGER["endpoint"]], "a scope of [audit-ledger] makes it db-1")
out = turn([LEDGER], "db-2")
check(db_aliases.REFUSAL in out and not edges("pg.connect"), "and db-2 is not in that request at all")
out = turn([], "db-2", space=OTHER_SPACE)
check(db_aliases.REFUSAL in out and not edges("pg.connect"), "a request in another space with no scope: db-2 resolves to nothing")

print("")
print("== a UUID: in scope still works, out of scope refused; a raw ARN refused ==")
turn(SCOPE, ORDERS["instance_id"])
check([c["host"] for c in edges("pg.connect")] == [ORDERS["endpoint"]], "the in-scope UUID connects")
out = turn(SCOPE, LEDGER["instance_id"])
check(db_aliases.REFUSAL in out and not edges("pg.connect"),
      "a UUID of a database in the space but not in this turn's scope is refused")
out = turn(SCOPE, ORDERS["secret_arn"])
check("secret ARNs are not accepted" in out and not edges("pg.connect") and not edges("secretsmanager.get_secret_value"),
      "a raw secret ARN is refused before any AWS call")

print("")
print("== the observability path: same alias, same request, per-request cache ==")
turn(SCOPE, "db-2", specialist="observability")
check(edges("rds.describe_db_clusters") == ["orders-cluster"], f"db-2 describes orders ({edges('rds.describe_db_clusters')})")
# No cache clearing: the next request names billing db-2; the record cache must not answer orders.
turn([LEDGER, BILLING], "db-2", specialist="observability", fresh=False)
check(edges("rds.describe_db_clusters") == ["billing-cluster"],
      f"in a request where db-2 is billing, it describes billing ({edges('rds.describe_db_clusters')})")
out = turn(SCOPE, "db-7", specialist="observability")
check(db_aliases.REFUSAL in out and not edges("rds.describe_db_clusters"), "an unknown alias is refused there too")

print("")
print("== tool results name in-scope databases by alias, not by UUID ==")


async def _listing():
    mr.CURRENT_USER_ID.set("operator-1")
    mr.CURRENT_SPACE_ID.set(SPACE)
    mr.CURRENT_DB_ALIASES.set(alias_map(SCOPE))
    return await asyncio.to_thread(db_tools.list_monitored_databases)
listed = {d.get("display_name"): d.get("instance_id") for d in asyncio.run(_listing()).get("databases", [])}
check(listed.get("orders") == "db-2" and listed.get("billing") == "db-1",
      f"list_monitored_databases shows the aliases ({listed})")
check(listed.get("audit-ledger") == LEDGER["instance_id"], "a database outside the turn's scope keeps its id")

print("")
print("== outside an agent request nothing changes ==")
check(db_aliases.resolve_handle("db-2") == "db-2", "an alias-shaped value passes through (Tier 1, batch, REST)")
check(mr.CURRENT_DB_ALIASES.get() is None, "no request's mapping leaked into this context")
check(pg.resolve_credential(ORDERS["secret_arn"]) == ORDERS["secret_arn"], "a secret ARN passes through as before")

print("")
print("== /invocations publishes the mapping from the records the block lists ==")
app_src = open(os.path.join(HERE, "..", "app.py"), encoding="utf-8").read()
inv = app_src[app_src.index("async def invocations("):app_src.index("# ─── REST API: Chat Sessions")]
check("CURRENT_DB_ALIASES.set(None)" in inv, "reset at the start of every request")
check("chat_scope_block_stable(" in inv and "CURRENT_DB_ALIASES.set(in_scope_map)" in inv
      and "CURRENT_SESSION_ALIASES.set(session_map)" in inv,
      "chat: stable per-session aliases; this turn's in-scope subset resolves")
check("sessions_registry.put_db_aliases(user_id, session_id, session_map)" in inv,
      "chat: the session's alias mapping is persisted")
check("investigation_scope_block(inv_records)" in inv and "CURRENT_DB_ALIASES.set(alias_map(inv_records))" in inv,
      "investigation: the same records as the block, rebuilt identically on every request")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
