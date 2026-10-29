# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The audit API in backend/app.py and the admin actions it records.

The handlers, the request-context middleware and the newly audited mutations are
extracted from app.py by source and run against the REAL ``tools.aws.audit_log`` and
``GovernanceRegistry``, with recording fake S3 / DynamoDB / CloudWatch / SNS clients, a
fake clock and unsigned JWTs decoded by the real ``utils.auth``. Requests go through the
real ``_audit_request_context`` middleware and run in a worker thread that inherits the
request context, the way FastAPI's run_in_threadpool does; background tasks run after
the request, past its audit deadline. Hermetic, no AWS, no FastAPI.

Covers design §11 "test_audit_endpoints.py", plus design-review findings 1 (admin:<sub>
verify), 3 (expired), 4 (account-level view), 8 (repeated switch value) and the brief's
A1 rule (a super admin acting on another admin's space is recorded as such).

Run:  python3 backend/tests/test_audit_endpoints.py
"""
import ast
import asyncio
import base64
import contextlib
import functools
import io
import json
import logging
import os
import pathlib
import re
import sys
import types
import urllib.parse
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
for _var in ("AUDIT_TABLE", "CONFIG_TABLE", "AUDIT_BUCKET", "AUDIT_VAULT_DEFAULT",
             "AUDIT_RETENTION_FLOOR", "AUDIT_EXTEND_RETENTION"):
    os.environ.pop(_var, None)
os.environ["RESOURCE_PREFIX"] = "acme"
os.environ["NOTIFICATION_TOPIC_ARN"] = "arn:aws:sns:us-east-1:111111111111:acme-alerts"

from botocore.exceptions import ClientError  # noqa: E402

APP_SRC = (pathlib.Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")

SA = "aaaaaaaa-0000-4000-8000-000000000001"     # owned by owner-a
SB = "bbbbbbbb-0000-4000-8000-000000000002"     # owned by owner-b
SDEL = "dddddddd-0000-4000-8000-000000000004"   # deleted: rows survive, the space row does not


class FakeSpaces:
    def __init__(self):
        self.reset()

    def reset(self):
        self.spaces = {
            SA: {"space_id": SA, "name": "Alpha", "created_by": "owner-a", "owner_admin": "owner-a",
                 "primary_account_id": "111111111111", "regions": ["us-east-1"]},
            SB: {"space_id": SB, "name": "Bravo", "created_by": "owner-b", "owner_admin": "owner-b",
                 "primary_account_id": "111111111111", "regions": ["us-east-1"]},
        }
        self.n = 0

    def get_space(self, sid):
        s = self.spaces.get(sid)
        return dict(s) if s else None

    def list_spaces(self):
        return [dict(s) for s in self.spaces.values()]

    def update_space(self, sid, patch):
        if sid not in self.spaces:
            return None
        self.spaces[sid].update(patch)
        return dict(self.spaces[sid])

    def create_space(self, data):
        self.n += 1
        sid = f"cccccccc-0000-4000-8000-{self.n:012d}"
        self.spaces[sid] = dict(data, space_id=sid)
        return dict(self.spaces[sid])

    def delete_space(self, sid):
        self.spaces.pop(sid, None)

    def claim_teardown(self, sid):
        # Mirrors SpacesRegistry.claim_teardown's conditional write.
        from tools.aws.teardown_claim import teardown_stalled
        sp = self.spaces.get(sid)
        if not sp or (sp.get("status") == "deleting" and not teardown_stalled(sp)):
            return False
        sp.update(status="deleting", teardown_claimed_at=datetime.now(timezone.utc).isoformat())
        return True

    def touch_teardown(self, sid):
        sp = self.spaces.get(sid)
        if sp and sp.get("status") == "deleting":
            sp["teardown_claimed_at"] = datetime.now(timezone.utc).isoformat()


SPACES = FakeSpaces()
_spaces_mod = types.ModuleType("tools.aws.spaces_client")
_spaces_mod.spaces_registry = SPACES
sys.modules["tools.aws.spaces_client"] = _spaces_mod

from tools.aws import audit_log as al  # noqa: E402
from tools.aws.teardown_claim import teardown_stalled  # noqa: E402
from tools.aws import governance_store as gs  # noqa: E402
from tools.resource_names import owner_admin_of  # noqa: E402
from utils import auth as uauth  # noqa: E402

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def cerr(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


# ── fake clock ───────────────────────────────────────────────────────────────
T0 = datetime(2025, 3, 10, 12, 0, 0, 123456, tzinfo=timezone.utc)
clock = {"mono": 1000.0, "now": T0}
al._monotonic = lambda: clock["mono"]
al._now = lambda: clock["now"]
FAKE_TIME = types.SimpleNamespace(monotonic=lambda: clock["mono"], time=lambda: 1.7e9)
CALLS = []


# ── in-memory DynamoDB expression evaluator (Key and Filter expressions) ─────
_MISSING = object()
_FUNCS = ("attribute_not_exists", "attribute_exists", "begins_with", "contains")


class Expr:
    def __init__(self, text, names, values):
        self.toks = re.findall(r"#\w+|:\w+|\w+|<=|>=|<>|[=<>(),]", text or "")
        self.i, self.names, self.values = 0, names or {}, values or {}
        self.ast = self.p_or() if self.toks else None
        assert self.i == len(self.toks), f"unparsed: {self.toks[self.i:]}"

    def peek(self, off=0):
        j = self.i + off
        return self.toks[j] if j < len(self.toks) else ""

    def take(self, want=None):
        t = self.toks[self.i]
        assert want is None or t.upper() == want, (t, want)
        self.i += 1
        return t

    def p_or(self):
        node = self.p_and()
        while self.peek().upper() == "OR":
            self.take()
            node = ("or", node, self.p_and())
        return node

    def p_and(self):
        node = self.p_not()
        while self.peek().upper() == "AND":
            self.take()
            node = ("and", node, self.p_not())
        return node

    def p_not(self):
        if self.peek().upper() == "NOT":
            self.take()
            return ("not", self.p_not())
        return self.p_primary()

    def operand(self):
        t = self.take()
        if t.startswith(":"):
            return ("val", self.values[t])
        return ("path", self.names.get(t, t))

    def p_primary(self):
        t = self.peek()
        if t == "(":
            self.take()
            node = self.p_or()
            self.take(")")
            return node
        if t.lower() in _FUNCS and self.peek(1) == "(":
            name = self.take().lower()
            self.take("(")
            args = [self.operand()]
            while self.peek() == ",":
                self.take()
                args.append(self.operand())
            self.take(")")
            return ("fn", name, args)
        left = self.operand()
        op = self.peek().upper()
        if op == "IN":
            self.take()
            self.take("(")
            items = [self.operand()]
            while self.peek() == ",":
                self.take()
                items.append(self.operand())
            self.take(")")
            return ("in", left, items)
        if op == "BETWEEN":
            self.take()
            lo = self.operand()
            self.take("AND")
            return ("between", left, lo, self.operand())
        self.take()
        return ("cmp", op, left, self.operand())

    def __call__(self, row):
        return True if self.ast is None else self._ev(self.ast, row)

    def _val(self, op, row):
        return row.get(op[1], _MISSING) if op[0] == "path" else op[1]

    def _ev(self, n, row):
        k = n[0]
        if k == "or":
            return self._ev(n[1], row) or self._ev(n[2], row)
        if k == "and":
            return self._ev(n[1], row) and self._ev(n[2], row)
        if k == "not":
            return not self._ev(n[1], row)
        if k == "fn":
            name, args = n[1], n[2]
            if name == "attribute_not_exists":
                return args[0][1] not in row
            if name == "attribute_exists":
                return args[0][1] in row
            v, s = self._val(args[0], row), self._val(args[1], row)
            if name == "begins_with":
                return isinstance(v, str) and v.startswith(s)
            return isinstance(v, (str, list)) and s in v
        if k == "in":
            v = self._val(n[1], row)
            return v is not _MISSING and v in [self._val(x, row) for x in n[2]]
        if k == "between":
            v = self._val(n[1], row)
            return v is not _MISSING and self._val(n[2], row) <= v <= self._val(n[3], row)
        op, a, b = n[1], self._val(n[2], row), self._val(n[3], row)
        if a is _MISSING or b is _MISSING:
            return False
        return {"=": a == b, "<>": a != b, "<": a < b, "<=": a <= b,
                ">": a > b, ">=": a >= b}[op]


# ── fake AWS clients ─────────────────────────────────────────────────────────
class FakeS3:
    def __init__(self):
        self.reset()

    def reset(self):
        self.objects = {}
        self.fail = None
        self.read_fail = None
        self.n = 0

    def put_object(self, **kw):
        CALLS.append(("s3.put_object", kw))
        if self.fail:
            raise cerr(self.fail)
        if kw.get("IfNoneMatch") == "*" and self.objects.get(kw["Key"]):
            raise cerr("PreconditionFailed")
        self.n += 1
        ver = {"VersionId": f"v{self.n}", "Key": kw["Key"], "Body": kw["Body"],
               "LastModified": clock["now"], "ChecksumSHA256": kw.get("ChecksumSHA256"),
               "ObjectLockMode": kw.get("ObjectLockMode", "COMPLIANCE"),
               "ObjectLockRetainUntilDate": kw.get("ObjectLockRetainUntilDate",
                                                   clock["now"] + timedelta(days=365))}
        self.objects.setdefault(kw["Key"], []).append(ver)
        return {"VersionId": ver["VersionId"]}

    def head_object(self, Bucket, Key):
        CALLS.append(("s3.head_object", Key))
        if not self.objects.get(Key):
            raise cerr("404")
        return {}

    def get_object(self, Bucket, Key, VersionId=None, ChecksumMode=None):
        CALLS.append(("s3.get_object", Key))
        if self.read_fail:
            raise cerr(self.read_fail)
        vers = self.objects.get(Key) or []
        if not vers:
            raise cerr("NoSuchKey")
        v = vers[-1] if VersionId is None else next((x for x in vers if x["VersionId"] == VersionId), None)
        if v is None:
            raise cerr("NoSuchVersion")
        return {"Body": io.BytesIO(v["Body"]), "ChecksumSHA256": v["ChecksumSHA256"],
                "LastModified": v["LastModified"], "VersionId": v["VersionId"],
                "ObjectLockMode": v["ObjectLockMode"],
                "ObjectLockRetainUntilDate": v["ObjectLockRetainUntilDate"]}

    def list_object_versions(self, Bucket, Prefix):
        out = []
        for k, vers in self.objects.items():
            if k.startswith(Prefix):
                out.extend({"Key": k, "VersionId": v["VersionId"]} for v in vers)
        return {"Versions": out}


class FakeDDB:
    """The low-level audit-index client (and the settings GetItem audit_log makes)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.rows = []
        self.config = None
        self.get_fail = False
        self.query_fail = False
        self.queries = []

    def put_item(self, TableName, Item, ConditionExpression=None):
        CALLS.append(("ddb.put_item", TableName))
        row = al.from_item(Item)
        assert TableName == "acme-audit", TableName
        if any(r["entity"] == row["entity"] and r["ts"] == row["ts"] for r in self.rows):
            raise cerr("ConditionalCheckFailedException")
        self.rows.append(row)
        return {}

    def get_item(self, TableName, Key):
        key = al.from_item(Key)
        if "config_key" in key:
            CALLS.append(("ddb.get_settings", TableName))
            return {"Item": al.to_item(self.config)} if self.config is not None else {}
        CALLS.append(("ddb.get_item", TableName))
        if self.get_fail:
            raise cerr("InternalServerError")
        for r in self.rows:
            if r["entity"] == key["entity"] and r["ts"] == key["ts"]:
                return {"Item": al.to_item(r)}
        return {}

    def query(self, **p):
        CALLS.append(("ddb.query", p.get("IndexName")))
        self.queries.append(p)
        if self.query_fail:
            raise cerr("ProvisionedThroughputExceededException")
        names = p.get("ExpressionAttributeNames") or {}
        values = {k: al._from_av(v) for k, v in (p.get("ExpressionAttributeValues") or {}).items()}
        kc = Expr(p["KeyConditionExpression"], names, values)
        fe = Expr(p.get("FilterExpression"), names, values)
        idx = p.get("IndexName")
        rows = [r for r in self.rows if not idx or "month" in r]
        cand = [r for r in rows if kc(r)]

        def pos(r):
            return (r["ts"], r["entity"]) if idx else (r["ts"],)
        cand.sort(key=pos, reverse=p.get("ScanIndexForward", True) is False)
        if p.get("ExclusiveStartKey"):
            esk = al.from_item(p["ExclusiveStartKey"])
            cand = [r for r in cand if pos(r) < pos(esk)]
        lim = p.get("Limit", 1000)
        page, more = cand[:lim], len(cand) > lim
        out = {"Items": [al.to_item(r) for r in page if fe(r)], "ScannedCount": len(page)}
        if more:
            last = page[-1]
            lek = {"entity": last["entity"], "ts": last["ts"]}
            if idx:
                lek["month"] = last["month"]
            out["LastEvaluatedKey"] = al.to_item(lek)
        return out


class FakeCW:
    def __init__(self):
        self.calls = []

    def put_metric_data(self, Namespace, MetricData):
        self.calls.append({d["MetricName"]: d["Value"] for d in MetricData})
        return {}


class FakeConfigTable:
    """GovernanceRegistry.config_table (resource API) over the same item FakeDDB serves."""
    name = "acme-config"

    def __init__(self):
        self.fail_update = False

    def get_item(self, Key):
        CALLS.append(("cfg.get_item", Key["config_key"]))
        return {"Item": dict(ddb.config)} if ddb.config is not None else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ReturnValues=None):
        CALLS.append(("cfg.update_item", UpdateExpression))
        if self.fail_update:
            raise cerr("InternalServerError")
        item = dict(ddb.config or {"config_key": Key["config_key"]})
        for field, ref in re.findall(r"(\w+) = (:\w+)", UpdateExpression):
            item[field] = ExpressionAttributeValues[ref]
        ddb.config = item
        return {"Attributes": dict(item)}


class FakeSNS:
    def __init__(self):
        self.published = []
        self.fail = False

    def publish(self, **kw):
        CALLS.append(("sns.publish", kw.get("Subject")))
        if self.fail:
            raise cerr("AuthorizationError")
        self.published.append(kw)
        return {"MessageId": "m1"}


s3, ddb, cw, sns = FakeS3(), FakeDDB(), FakeCW(), FakeSNS()
al._clients.update({"s3": s3, "dynamodb": ddb, "cloudwatch": cw})
GR = gs.GovernanceRegistry()
CFG = FakeConfigTable()
GR._config = CFG

logging.getLogger("audit-log").setLevel(logging.CRITICAL)


class LogCap:
    def __init__(self):
        self.records = []

    def _add(self, level):
        return lambda msg, *a, **k: self.records.append((level, (msg % a) if a else msg))

    def __getattr__(self, level):
        if level in ("info", "warning", "error", "debug"):
            return self._add(level.upper())
        raise AttributeError(level)

    def having(self, text, level=None):
        return [m for lv, m in self.records if text in m and (level is None or lv == level)]


LOG = LogCap()


def reset(vault=True, bucket="acme-vault", floor=365, extend=False):
    CALLS.clear()
    s3.reset()
    ddb.reset()
    cw.calls.clear()
    sns.published.clear()
    sns.fail = False
    CFG.fail_update = False
    SPACES.reset()
    LOG.records.clear()
    clock["mono"], clock["now"] = 1000.0, T0
    al.AUDIT_BUCKET, al.AUDIT_RETENTION_FLOOR = bucket, floor
    al.AUDIT_EXTEND_RETENTION, al.AUDIT_VAULT_DEFAULT = extend, "off"
    al._settings_cache.update({"value": None, "at": None})
    al.drain_pending_metrics()
    al._space_cache.clear()
    al._misconfig_logged[0] = False
    al.AUDIT_CONTEXT.set({})
    if vault is not None:
        ddb.config = {"config_key": "audit_settings", "vault_enabled": vault}


def calls(prefix):
    return [c for c in CALLS if c[0].startswith(prefix)]


def rows_in(entity):
    return [r for r in ddb.rows if r["entity"] == entity]


# ── tokens (unsigned; the real utils.auth decodes them) ──────────────────────
def token(sub, groups, email=None):
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    payload = {"sub": sub, "cognito:groups": list(groups), "exp": 4102444800,
               "email": email or f"{sub}@example.com", "cognito:username": email or f"{sub}@example.com"}
    return "Bearer " + b64({"alg": "none"}) + "." + b64(payload) + ".sig"


SUPER = token("super-1", ["admins"], "super@example.com")
OWNER_A = token("owner-a", ["space-admins", f"space-{SA}"], "owner-a@example.com")
OWNER_B = token("owner-b", ["space-admins", f"space-{SB}"], "owner-b@example.com")
PEER_A = token("peer-1", ["space-admins", f"space-{SA}"])          # in A's group, not its creator
OPER_A = token("oper-1", [f"space-{SA}"])                          # an operator of A
EXOWNER_A = token("owner-a", [f"space-{SA}"])                      # A's creator, admin role removed


class Req:
    def __init__(self, auth):
        self.headers = {"Authorization": auth, "x-dbops-request-id": "req-77",
                        "x-dbops-client-ip": "198.51.100.7"}
        self.method = "GET"


class FakeHTTPException(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


class FakeBackgroundTasks:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *a, **kw):
        self.tasks.append((fn, a, kw))

    def run(self):
        for fn, a, kw in self.tasks:
            fn(*a, **kw)


# ── extraction ───────────────────────────────────────────────────────────────
def _extract(name):
    m = re.search(rf"^(?:async )?def {re.escape(name)}\(.*?(?=^(?:@|def |async def |class )|\Z)",
                  APP_SRC, re.S | re.M)
    assert m, f"{name} not found in app.py"
    return m.group(0)


def _audit_constants():
    """The module-level constants of the "REST API: Audit" section (after its header)."""
    block = APP_SRC.split("# ─── REST API: Audit", 1)[1].split("\ndef _audit_entity_ok(", 1)[0]
    return block.split("\n", 1)[1]


HANDLERS = (
    "_audit_entity_ok", "_audit_ts_ok", "_audit_space_id_ok", "_audit_query_kwargs",
    "_run_audit_query", "_space_audit_reader", "list_audit_events", "list_space_audit_events",
    "get_audit_meta", "_audit_verify", "verify_audit_event", "verify_space_audit_event",
    "_audit_settings_view", "_notify_vault_off", "get_audit_settings", "set_audit_settings",
    "_created_by_caller", "_can_manage_space", "_audit_request_context",
)


class LastMemberError(Exception):
    pass


class FakeDBRegistry:
    def __init__(self, rows=()):
        self.rows = {r["instance_id"]: dict(r) for r in rows}
        self.calls = []

    def get_instance(self, iid):
        r = self.rows.get(iid)
        return dict(r) if r else None

    def list_instances(self, *a, **k):
        return [dict(r) for r in self.rows.values()]

    def find_by_arn(self, arn):
        # The ARN index discovery checks before creating a record.
        return next((dict(r) for r in self.rows.values()
                     if (r.get("arn") or (r.get("tags") or {}).get("arn")) == arn), None)

    def update_instance(self, iid, patch):
        self.calls.append(("update_instance", iid))
        if iid in self.rows:
            self.rows[iid].update(patch)

    def set_reach_space(self, iid, sid, secret_arn="", owner_admin=""):
        self.rows[iid]["reach_space_id"] = sid
        if secret_arn:
            self.rows[iid]["secret_arn"] = secret_arn

    def promote_candidate(self, iid, sid, secret_arn):
        r = self.rows[iid]
        r["member_space_ids"] = set(r.get("member_space_ids") or ()) | {sid}
        r["candidate_space_ids"] = set(r.get("candidate_space_ids") or ()) - {sid}
        r.setdefault("space_secrets", {})[sid] = secret_arn

    def add_candidate(self, iid, sid):
        r = self.rows[iid]
        r["candidate_space_ids"] = set(r.get("candidate_space_ids") or ()) | {sid}

    def set_space_secret(self, iid, sid, arn):
        self.rows[iid].setdefault("space_secrets", {})[sid] = arn

    def remove_candidate(self, iid, sid):
        r = self.rows[iid]
        r["candidate_space_ids"] = set(r.get("candidate_space_ids") or ()) - {sid}

    def remove_member(self, iid, sid, keep_one=False):
        r = self.rows[iid]
        m = set(r.get("member_space_ids") or ())
        if keep_one and (sid not in m or len(m) <= 1):
            # The real conditional DELETE refuses to empty the set.
            raise LastMemberError(iid, sid)
        r["member_space_ids"] = m - {sid}

    def delete_instance(self, iid):
        self.rows.pop(iid, None)
    def remove_space_secret(self, iid, sid):
        (self.rows[iid].get("space_secrets") or {}).pop(sid, None)
    def claim_teardown(self, iid):
        r = self.rows.get(iid)
        if not r or (r.get("status") == "deleting" and not teardown_stalled(r)):
            return False
        r["status"] = "deleting"
        r["teardown_claimed_at"] = datetime.now(timezone.utc).isoformat()
        return True

    def touch_teardown(self, iid):
        r = self.rows.get(iid)
        if r and r.get("status") == "deleting":
            r["teardown_claimed_at"] = datetime.now(timezone.utc).isoformat()


class FakeSession:
    def __init__(self, endpoint):
        self.endpoint = endpoint

    def client(self, name, region_name=None):
        if name == "secretsmanager":
            ep = self.endpoint
            return types.SimpleNamespace(get_secret_value=lambda SecretId: {"SecretString": json.dumps(
                {"host": ep, "username": "ro", "password": "x", "dbname": "postgres"})})
        return types.SimpleNamespace()


_pg_mod = types.ModuleType("tools.aws.pg_client")
_pg_mod._lookup_endpoint_from_registry = lambda arn, host: (host, None)
sys.modules["tools.aws.pg_client"] = _pg_mod
_psycopg = types.ModuleType("psycopg2")
_psycopg.connect = lambda **kw: types.SimpleNamespace(close=lambda: None)
sys.modules["psycopg2"] = _psycopg

NET = {"result": {"status": "configured", "method": "peering", "steps": []}}


def _boto_client(name, *a, **k):
    if name == "sns":
        return sns
    if name == "sts":
        return types.SimpleNamespace(get_caller_identity=lambda: {"Account": "111111111111"})
    if name == "iam":
        return types.SimpleNamespace(
            create_role=lambda **kw: {"Role": {"Arn": f"arn:aws:iam::111111111111:role/{kw['RoleName']}"}},
            put_role_policy=lambda **kw: None)
    if name == "ec2":
        return types.SimpleNamespace(describe_vpcs=lambda **kw: (_ for _ in ()).throw(RuntimeError("no ec2")))
    raise AssertionError(f"unexpected boto3 client {name}")


def build(db_rows=(), extra=()):
    """A namespace with the real handlers, helpers, auth and audit modules."""
    reg = FakeDBRegistry(db_rows)
    ns = {
        "audit_log": al, "governance_registry": GR, "spaces_registry": SPACES, "db_registry": reg,
        "HTTPException": FakeHTTPException, "Request": object, "BaseModel": object,
        "LastMemberError": LastMemberError,
        # request-body annotations, evaluated when the extracted defs run
        **{n: object for n in ("AuditSettingsUpdate", "SpaceCreate", "SpaceUpdate", "MonitoringConfig",
                               "BulkMonitoringConfig", "RenameDatabaseRequest", "ConfigureDatabaseRequest",
                               "BatchConfigureRequest", "BackgroundTasks")},
        "Optional": __import__("typing").Optional, "Query": lambda default=None, **kw: default,
        "logger": LOG, "re": re, "os": os, "json": json, "time": FAKE_TIME,
        "datetime": datetime, "timezone": timezone,
        "boto3": types.SimpleNamespace(client=_boto_client),
        "is_admin": uauth.is_admin, "is_space_admin": uauth.is_space_admin,
        "get_user_id": uauth.get_user_id, "get_username": uauth.get_username,
        "get_user_groups": uauth.get_user_groups, "decode_jwt_unverified": uauth.decode_jwt_unverified,
        "_email_for_sub": lambda sub: {"owner-a": "owner-a@example.com"}.get(sub, ""),
        "owner_admin_of": owner_admin_of,
        "_space_secret_prefix": lambda space: f"acme/{owner_admin_of(space)}/",
        "_instance_member_spaces": lambda inst: set((inst or {}).get("member_space_ids") or ()),
        "_instance_candidate_spaces": lambda inst: set((inst or {}).get("candidate_space_ids") or ()),
        "_instance_in_space": lambda inst, sid: sid in set((inst or {}).get("member_space_ids") or ()),
        "_space_session": lambda space, acct, region: FakeSession("db.example.internal"),
        "_reject_secret_host_mismatch": lambda *a, **k: None,
        "_auto_schedule_on_configure": lambda *a, **k: None,
        "_refresh_space_db_count": lambda *a, **k: None,
        "_redact_identifiers": lambda s: s,
        "ssl_kwargs": lambda: {},
        "network_configurator": types.SimpleNamespace(
            agent_vpc_id="vpc-agent", configure_network=lambda **kw: dict(NET["result"])),
        "db_discovery": types.SimpleNamespace(_get_cross_account_session=lambda *a, **k: None),
        "resource_explorer": types.SimpleNamespace(),
        "investigations_registry": types.SimpleNamespace(
            get=lambda iid: {"investigation_id": iid, "space_id": SA}, delete=lambda iid: None),
        "teardown_stalled": teardown_stalled, "TEARDOWN_HEARTBEAT_S": 60,
        "wait_heartbeat": lambda beat: contextlib.nullcontext(),
    }

    def verify_in_space(iid, sid, auth=""):
        inst = reg.get_instance(iid)
        if not inst or sid not in set(inst.get("member_space_ids") or ()):
            raise FakeHTTPException(404, "Instance not found")
        return inst

    def verify_onboardable(iid, sid, auth=""):
        inst = reg.get_instance(iid)
        if not inst or sid not in (set(inst.get("member_space_ids") or ()) | set(inst.get("candidate_space_ids") or ())):
            raise FakeHTTPException(404, "Instance not found")
        return inst
    ns["_verify_instance_in_space"] = verify_in_space
    ns["_verify_instance_onboardable"] = verify_onboardable
    exec(_audit_constants(), ns)  # noqa: S102 - pinning shipped source
    for name in HANDLERS + tuple(extra):
        exec(_extract(name), ns)  # noqa: S102
    return ns, reg


def call(ns, auth, fn, *args, **kwargs):
    """Run a plain-def handler as FastAPI does: through the real middleware, in a worker
    thread that inherits the request's context (asyncio.to_thread copies it, as
    run_in_threadpool does). Returns ("ok", value) or ("err", status, detail)."""
    req = Req(auth)
    out = {}

    async def call_next(request):
        try:
            out["v"] = ("ok", await asyncio.to_thread(functools.partial(fn, *args, request=req, **kwargs)))
        except FakeHTTPException as e:
            out["v"] = ("err", e.status_code, e.detail)
        return "response"

    asyncio.run(ns["_audit_request_context"](req, call_next))
    return out["v"]


def ok(res):
    return res[1] if res[0] == "ok" else None


def status(res):
    return 200 if res[0] == "ok" else res[1]


def detail(res):
    return res[2] if res[0] == "err" else None


def as_user(sub, groups, email):
    """Set the main thread's audit context directly (for seeding events)."""
    al.AUDIT_CONTEXT.set({"sub": sub, "groups": list(groups), "email": email,
                          "request_id": "seed", "source_ip": ""})


# ── seeding ──────────────────────────────────────────────────────────────────
def tick(seconds=1):
    clock["now"] = clock["now"] + timedelta(seconds=seconds)
    clock["mono"] += 0.01


def seed(actor, groups, action, target, scope, before=None, after=None, **kw):
    as_user(actor, groups, f"{actor}@example.com")
    tick()
    out = GR.audit(actor, action, target, scope, before, after, **kw)
    al.AUDIT_CONTEXT.set({})
    return out


def put_row(entity, ts, month=True, **fields):
    r = {"entity": entity, "ts": ts, "v": 2, "category": "admin", "action": "space_update",
         "actor_sub": "owner-a", "event_id": "e" * 32, "vault": "off", "space_id": "",
         "target_id": "t", "search": "owner-a"}
    if month:
        r["month"] = ts[:7]
    r.update(fields)
    ddb.rows.append(r)
    return r


# ── middleware ───────────────────────────────────────────────────────────────
def test_middleware_sets_and_resets_the_context():
    print("middleware")
    reset()
    ns, _ = build()
    req = Req(SUPER)
    seen = {}

    async def nxt(request):
        seen.update(dict(al.AUDIT_CONTEXT.get()))
        seen["thread"] = await asyncio.to_thread(lambda: dict(al.AUDIT_CONTEXT.get()))
        return "r"

    async def run():
        r = await ns["_audit_request_context"](req, nxt)
        return r, dict(al.AUDIT_CONTEXT.get())
    r, after = asyncio.run(run())
    check(r == "r", "the middleware returns call_next's response")
    check(seen["sub"] == "super-1" and seen["email"] == "super@example.com"
          and seen["groups"] == ["admins"], "sub, email and groups come from the bearer")
    check(seen["request_id"] == "req-77" and seen["source_ip"] == "198.51.100.7",
          "request id and client ip come from the API Gateway headers")
    check(seen["deadline"] == 1000.0 + 20.0, "deadline = monotonic() + 20 s")
    check(seen["resolve_email"] is ns["_email_for_sub"], "resolve_email is _email_for_sub")
    check(seen["thread"]["sub"] == "super-1", "a worker thread started from the request inherits it")
    check(after == {}, "the context is reset after the request")
    seen.clear()
    req = Req("")
    asyncio.run(ns["_audit_request_context"](req, nxt))
    check(seen["sub"] == "unknown" and seen["groups"] == [] and seen["email"] == "",
          "a request with no bearer gets an empty identity")


def test_middleware_order():
    mids = re.findall(r'^@app\.middleware\("http"\)\nasync def (\w+)\(', APP_SRC, re.M)
    check(mids == ["_audit_request_context", "_enforce_jwt_signature"],
          "two http middlewares, the audit context defined first (Starlette runs the last "
          "registered outermost, so JWT enforcement is the outer one)")
    i = APP_SRC.index("async def _audit_request_context(")
    j = APP_SRC.index('@app.middleware("http")\nasync def _enforce_jwt_signature(')
    check(not re.search(r"^(?:def |class |@|async def )", APP_SRC[i + 1:j], re.M),
          "_audit_request_context is defined directly above _enforce_jwt_signature")
    check(APP_SRC.index("    CORSMiddleware,") < i, "CORS is added before both")
    body = _extract("_audit_request_context")
    check(body.index("AUDIT_CONTEXT.set(") < body.index("await call_next(request)")
          < body.index("AUDIT_CONTEXT.reset(token)"), "set before call_next, reset in finally")
    check("finally:" in body, "the reset is in a finally")


# ── read authorization ───────────────────────────────────────────────────────
def test_read_authz_matrix():
    print("read authorization")
    reset()
    ns, _ = build()
    seed("owner-a", ["space-admins"], "space_update", SA, f"space:{SA}", {"name": "A0"}, {"name": "Alpha"})
    seed("super-1", ["admins"], "space_discover", SA, f"space:{SA}", None, {"regions": ["us-east-1"], "found": 2})
    fn = ns["list_space_audit_events"]
    res = call(ns, SUPER, fn, SA)
    rows = (ok(res) or {}).get("rows", [])
    check(status(res) == 200 and len(rows) == 2, "a super admin reads another admin's space")
    check(all("entity" in r and "request_id" in r for r in rows), "super admins get the full projection")
    res = call(ns, OWNER_A, fn, SA)
    rows = (ok(res) or {}).get("rows", [])
    check(status(res) == 200 and len(rows) == 2, "the creator reads their own space")
    banned = ("request_id", "source_ip", "owner_admin", "s3_key", "s3_version_id", "sha256",
              "scope", "space_ids", "entity", "search")
    check(rows and all(not any(b in r for b in banned) for r in rows),
          "the creator gets the owner projection (no request facts, vault coordinates or owner)")
    check(all(r["space_name"] == "Alpha" and r["space_id"] == SA for r in rows),
          "space id and name come from the path space")
    disc = next(r for r in rows if r["action"] == "space_discover")
    check(disc["actor_role"] == "super_admin" and disc["on_other_owner"] is True,
          "the owner sees a super admin's action on their space flagged")
    msg = "Only this space's creator or a super admin can read its audit log"
    for who, label in ((PEER_A, "a space admin in the group who did not create it"),
                       (OPER_A, "an operator of the space"),
                       (EXOWNER_A, "the creator after their admin role was removed"),
                       (OWNER_B, "another space's owner")):
        res = call(ns, who, fn, SA)
        check(status(res) == 403 and detail(res) == msg, f"403 for {label}")
    check(LOG.having("audit: read denied", "INFO"), "a denial is logged at INFO")
    res = call(ns, OWNER_A, ns["list_audit_events"])
    check(status(res) == 403 and detail(res) == "Admin access required",
          "the global log is super admin only")
    res = call(ns, SUPER, fn, "not-a-uuid")
    check(status(res) == 400 and detail(res) == "Invalid space id", "a malformed space id is a 400")


def test_deleted_space_reads():
    print("deleted space")
    reset()
    ns, _ = build()
    put_row(f"space:{SDEL}", "2025-03-09T10:00:00.000001+00:00#00000001", space_id=SDEL,
            space_name="Gone", action="space_delete")
    put_row(f"space:{SDEL}", "2025-03-08T10:00:00.000001+00:00#00000002", month=False,
            space_id=SDEL, space_name="Gone", fanout=True, action="database_rename")
    ddb.queries.clear()
    res = call(ns, SUPER, ns["list_audit_events"], space_id=SDEL)
    rows = (ok(res) or {}).get("rows", [])
    check(len(rows) == 2 and {r["action"] for r in rows} == {"space_delete", "database_rename"},
          "GET /api/audit?space_id=<deleted> returns the space's rows, fan-out included")
    check(ddb.queries and all(not q.get("IndexName") for q in ddb.queries),
          "... from the base partition, with no month walk")
    res = call(ns, SUPER, ns["list_space_audit_events"], SDEL)
    check(len((ok(res) or {}).get("rows", [])) == 2, "and through the space endpoint")
    check(all(r["space_name"] == "Gone" for r in ok(res)["rows"]), "space_name comes from the stored rows")
    gone = token("owner-del", ["space-admins", f"space-{SDEL}"])
    res = call(ns, gone, ns["list_space_audit_events"], SDEL)
    check(status(res) == 403, "a creator cannot read a deleted space (no space row)")


# ── query mapping ────────────────────────────────────────────────────────────
def test_query_mapping_and_validation():
    print("query mapping")
    reset()
    ns, _ = build()
    seed("owner-a", ["space-admins"], "space_update", SA, f"space:{SA}", None, {"name": "Alpha"})
    g = ns["list_audit_events"]
    ddb.queries.clear()
    res = call(ns, SUPER, g, actor="owner-a", action="space_update", q="  ALPHA ")
    q = ddb.queries[0]
    fe, vals = q["FilterExpression"], {k: al._from_av(v) for k, v in q["ExpressionAttributeValues"].items()}
    check("attribute_not_exists(#category) AND NOT (#action IN (:qpr, :qpf))" in fe
          and "#category = :admin" in fe, "the admin-category clause is always present")
    check("#actor_sub = :a" in fe and vals[":a"] == "owner-a", "a sub actor filters actor_sub")
    check("#action = :act" in fe and vals[":act"] == "space_update", "action filter")
    check("contains(#search, :q)" in fe and vals[":q"] == "alpha", "q is trimmed and lower-cased")
    check(q.get("IndexName") == "month-index", "no space_id walks month-index")
    check(len(ok(res)["rows"]) == 1, "the filtered row is found")
    ddb.queries.clear()
    call(ns, SUPER, g, actor="Owner-A@Example.com")
    vals = {k: al._from_av(v) for k, v in ddb.queries[0]["ExpressionAttributeValues"].items()}
    check("contains(#search, :email)" in ddb.queries[0]["FilterExpression"]
          and vals[":email"] == "owner-a@example.com", "an email actor matches search, case-insensitively")
    res = call(ns, SUPER, g, q="x" * 300)
    check(status(res) == 200, "an over-long q is truncated, not refused")
    bad = [({"cursor": "x" * 2049}, "Invalid cursor"),
           ({"cursor": "not-a-cursor"}, "Invalid cursor"),
           ({"from_": "yesterday"}, "Invalid date"),
           ({"from_": "2025-03-10", "to": "2025-03-01"}, "Invalid date"),
           ({"actor": "bad actor!"}, "Invalid actor"),
           ({"actor": "a" * 65}, "Invalid actor"),
           ({"action": "Space-Update"}, "Invalid action"),
           ({"space_id": "not-a-space"}, "Invalid space id")]
    for kw, msg in bad:
        res = call(ns, SUPER, g, **kw)
        check(status(res) == 400 and detail(res) == msg, f"400 {msg!r} for {sorted(kw)}")
        check(str(list(kw.values())[0]) not in str(detail(res)), "... without echoing the input")
    res = call(ns, SUPER, g)
    check(set(ok(res)) == {"rows", "has_more", "next_before", "total", "scanned_to"}
          and ok(res)["total"] is None, "the response shape, total null")


def test_limit_clamp_and_paging():
    reset()
    ns, _ = build()
    for i in range(250):
        put_row(f"space:{SB}", f"2025-03-01T00:{i // 60:02d}:{i % 60:02d}.000001+00:00#{i:08x}", space_id=SB)
    s = ns["list_space_audit_events"]
    res = call(ns, SUPER, s, SB, limit=500)
    check(len(ok(res)["rows"]) == 200 and ok(res)["has_more"] is True, "limit is clamped to 200")
    res = call(ns, SUPER, s, SB, limit=0)
    check(len(ok(res)["rows"]) == 1, "limit is clamped to at least 1")
    res = call(ns, SUPER, s, SB)
    first = ok(res)
    check(len(first["rows"]) == 50, "the default page is 50")
    res = call(ns, SUPER, s, SB, cursor=first["next_before"])
    second = ok(res)
    check(second and second["rows"][0]["ts"] < first["rows"][-1]["ts"], "the cursor resumes after the page")
    res = call(ns, SUPER, s, SA, cursor=first["next_before"])
    check(status(res) == 400 and detail(res) == "Invalid cursor",
          "a cursor from another space's partition is refused")
    ddb.query_fail = True
    res = call(ns, SUPER, s, SB)
    check(status(res) == 502 and detail(res) == "Could not read the audit log", "a store error is a 502")
    check(LOG.having("audit: read failed", "WARNING"), "... logged at WARNING with the class")


def test_date_bounds():
    reset()
    ns, _ = build()
    put_row(f"space:{SA}", "2025-03-10T23:59:59.999999+00:00#00000001", space_id=SA)
    put_row(f"space:{SA}", "2025-03-11T00:00:00.000000+00:00#00000002", space_id=SA)
    res = call(ns, SUPER, ns["list_space_audit_events"], SA, to="2025-03-10")
    got = [r["ts"][:26] for r in ok(res)["rows"]]
    check(got == ["2025-03-10T23:59:59.999999"],
          "a date `to` includes 23:59:59.999999 and excludes the next midnight")
    reset()
    put_row(f"space:{SA}", "2025-03-10T10:00:00.500000+00:00#00000003", space_id=SA)
    put_row(f"space:{SA}", "2025-03-10T10:00:01.000000+00:00#00000004", space_id=SA)
    res = call(ns, SUPER, ns["list_space_audit_events"], SA, to="2025-03-10T10:00:00Z")
    check([r["ts"][:26] for r in ok(res)["rows"]] == ["2025-03-10T10:00:00.500000"],
          "a datetime `to` includes to + 0.5 s")


def test_global_walk_stops_at_130_calls():
    reset()
    ns, _ = build()
    saved = al.RETENTION_MAX_DAYS
    al.RETENTION_MAX_DAYS = 6000     # a walk floor further back than 130 months
    try:
        ddb.queries.clear()
        res = call(ns, SUPER, ns["list_audit_events"])
        page = ok(res)
        check(len(ddb.queries) == 130, f"the walk stops at 130 Query calls ({len(ddb.queries)})")
        check(page["has_more"] is True and page["next_before"] and page["scanned_to"],
              "... returning has_more, a resume cursor and scanned_to")
        ddb.queries.clear()
        res = call(ns, SUPER, ns["list_audit_events"], cursor=page["next_before"])
        first_month = al._from_av(ddb.queries[0]["ExpressionAttributeValues"][":m"])
        check(status(res) == 200 and first_month < page["scanned_to"], "the cursor resumes the walk further back")
    finally:
        al.RETENTION_MAX_DAYS = saved


def test_account_level_view_includes_admin_partitions():
    print("account-level view (finding 4)")
    reset()
    ns, _ = build()
    seed("owner-a", ["space-admins"], "knowledge_admin_tier_upload", "runbook.md", "admin:owner-a",
         None, {"name": "runbook.md"})
    seed("super-1", ["admins"], "rate_limit_set", "60", "account", {"per_minute": 30}, {"per_minute": 60})
    seed("owner-a", ["space-admins"], "space_update", SA, f"space:{SA}", None, {"name": "Alpha"})
    ddb.queries.clear()
    res = call(ns, SUPER, ns["list_audit_events"], space_id="account")
    ents = sorted(r["entity"] for r in ok(res)["rows"])
    check(ents == ["account", "admin:owner-a"], f"account and admin:<sub> rows, no space: row ({ents})")
    check(ddb.queries[0].get("IndexName") == "month-index"
          and "NOT begins_with(#entity, :space)" in ddb.queries[0]["FilterExpression"],
          "answered by the month walk excluding space: partitions")


# ── meta ─────────────────────────────────────────────────────────────────────
def test_meta():
    print("meta")
    reset(vault=True)
    ns, _ = build()
    res = call(ns, SUPER, ns["get_audit_meta"])
    m = ok(res)
    codes = {a["code"] for a in m["actions"]}
    check("space_update" in codes and "query_plan_run" not in codes and "query_plan_refused" not in codes,
          "admin-category actions only")
    check(all(set(a) == {"code", "label", "target_type"} for a in m["actions"]), "each action is {code, label, target_type}")
    check(m["vault_enabled"] is True and m["effective_retention_days"] == 365
          and m["floor_days"] == 365 and m["bucket_configured"] is True, "super admins get the floor and bucket flag")
    res = call(ns, OWNER_A, ns["get_audit_meta"])
    check(status(res) == 200 and "floor_days" not in ok(res) and "bucket_configured" not in ok(res),
          "space admins get the catalog and vault state only")
    res = call(ns, OPER_A, ns["get_audit_meta"])
    check(status(res) == 403, "operators are refused")


# ── verify ───────────────────────────────────────────────────────────────────
def test_verify_validation_and_results():
    print("verify")
    reset(vault=True)
    ns, _ = build()
    seed("owner-a", ["space-admins"], "space_update", SA, f"space:{SA}", {"name": "A0"}, {"name": "Alpha"})
    row = rows_in(f"space:{SA}")[0]
    ts = row["ts"]
    check("+00:00#" in ts, "the index ts carries + and #")
    qs = urllib.parse.urlencode({"entity": f"space:{SA}", "ts": ts})     # the console's URLSearchParams
    parsed = {k: v[0] for k, v in urllib.parse.parse_qs(qs).items()}
    v = ns["verify_audit_event"]
    res = call(ns, SUPER, v, entity=parsed["entity"], ts=parsed["ts"])
    check(ok(res) and ok(res)["status"] == "verified", "an encoded ts round-trips and verifies")
    check("s3_key" in ok(res) and ok(res)["record"]["request_id"] == "seed" and "row" not in ok(res),
          "super admins get s3_key and the full record; the raw row is dropped")
    raw = urllib.parse.urlsplit(f"https://x/api/audit/verify?entity=space:{SA}&ts={ts}").query
    raw_ts = urllib.parse.parse_qs(raw)["ts"][0]
    res = call(ns, SUPER, v, entity=f"space:{SA}", ts=raw_ts)
    check(status(res) == 400 and detail(res) == "Invalid record reference",
          "the same ts sent raw (+ decoded to a space, # cut) is a 400, not a 404")
    CALLS.clear()
    res = call(ns, SUPER, v, entity=f"space:{SA}", ts="2" * 2000)
    check(status(res) == 400 and not calls("ddb.get_item"), "a 2000-char ts is a 400 with no DynamoDB call")
    for ent in ("space:not-a-uuid", "foo", "admin:", "admin:" + "a" * 65, "space:", "account ", "agent\n"):
        res = call(ns, SUPER, v, entity=ent, ts=ts)
        check(status(res) == 400 and detail(res) == "Invalid record reference", f"entity {ent!r} is a 400")
    for ent in ("agent", "account", f"space:{SB}"):
        res = call(ns, SUPER, v, entity=ent, ts=ts)
        check(status(res) == 404 and detail(res) == "Audit record not found",
              f"entity {ent!r} is accepted (404 for a row it does not hold)")
    seed("owner-a", ["space-admins"], "knowledge_admin_tier_upload", "runbook.md", "admin:owner-a",
         None, {"name": "runbook.md"})
    arow = rows_in("admin:owner-a")[0]
    res = call(ns, SUPER, v, entity="admin:owner-a", ts=arow["ts"])
    check(ok(res) and ok(res)["status"] == "verified", "F1: an admin:<sub> row is accepted and verified")
    seed("super-1", ["admins"], "rate_limit_set", "60", "account", None, {"per_minute": 60})
    res = call(ns, SUPER, v, entity="account", ts=rows_in("account")[0]["ts"])
    check(ok(res) and ok(res)["status"] == "verified", "an account row verifies")
    res = call(ns, OWNER_A, v, entity=f"space:{SA}", ts=ts)
    check(status(res) == 403, "the global verify is super admin only")
    ddb.get_fail = True
    res = call(ns, SUPER, v, entity=f"space:{SA}", ts=ts)
    check(status(res) == 502 and detail(res) == "Could not read the audit log", "a GetItem ClientError is a 502")
    ddb.get_fail = False
    s3.read_fail = "AccessDenied"
    res = call(ns, SUPER, v, entity=f"space:{SA}", ts=ts)
    check(status(res) == 502 and detail(res) == "Could not read the audit vault", "any other S3 error is a 502")
    s3.read_fail = None
    s3.objects.clear()
    res = call(ns, SUPER, v, entity=f"space:{SA}", ts=ts)
    check(ok(res) == {"status": "missing"}, "an absent object inside its retention is missing")
    clock["now"] = T0 + timedelta(days=400)
    res = call(ns, SUPER, v, entity=f"space:{SA}", ts=ts)
    check(ok(res) and ok(res)["status"] == "expired" and ok(res)["retain_until"] == row["retain_until"],
          "F3: past retain_until the same absence is expired, passed through")
    res = call(ns, OWNER_A, ns["verify_space_audit_event"], SA, ts=ts)
    check(ok(res) and ok(res)["status"] == "expired", "F3: expired also through the owner endpoint")


def test_owner_verify():
    reset(vault=True)
    ns, _ = build()
    seed("owner-a", ["space-admins"], "space_update", SA, f"space:{SA}", {"name": "A0"}, {"name": "Alpha"})
    seed("super-1", ["admins"], "database_decommission", "db-1", "account",
         {"member_space_ids": [SA, SB]}, None, space_ids=[SA, SB])
    own = [r for r in rows_in(f"space:{SA}") if r["action"] == "space_update"][0]
    fan = [r for r in rows_in(f"space:{SA}") if r.get("fanout")][0]
    v = ns["verify_space_audit_event"]
    res = call(ns, OWNER_A, v, SA, ts=own["ts"])
    out = ok(res)
    check(out and out["status"] == "verified" and "s3_key" not in out and "row" not in out,
          "the creator verifies their row; no vault key")
    check(out and not any(k in out["record"] for k in ("request_id", "source_ip", "owner_admin", "sha256")),
          "the record is the owner projection")
    res = call(ns, OWNER_A, v, SA, ts=fan["ts"])
    out = ok(res)
    check(out and out["status"] == "verified" and out["record"]["space_id"] == SA,
          "a fan-out row verifies through the owner path (record space_id is the path space)")
    check(out and out["record"]["before"] == {"member_count": 2} and "space_ids" not in out["record"],
          "... member_space_ids becomes member_count; space_ids never reaches an owner")
    res = call(ns, PEER_A, v, SA, ts=own["ts"])
    check(status(res) == 403, "a non-creator space admin cannot verify")
    res = call(ns, OWNER_A, v, "bad", ts=own["ts"])
    check(status(res) == 400 and detail(res) == "Invalid space id", "a bad path id is a 400")
    CALLS.clear()
    res = call(ns, PEER_A, v, SA, ts="nope")
    check(status(res) == 400 and not calls("ddb"), "a bad ts is a 400 before the gate or any AWS call")
    res = call(ns, OWNER_B, v, SB, ts=own["ts"])
    check(status(res) == 404, "entity is forced to the path space (A's row is not in B)")


# ── settings ─────────────────────────────────────────────────────────────────
def put_settings(ns, auth, **body):
    data = types.SimpleNamespace(vault_enabled=body.get("vault_enabled"),
                                 retention_days=body.get("retention_days"),
                                 confirm=body.get("confirm"))
    return call(ns, auth, ns["set_audit_settings"], data)


def index_of(prefix, pred=lambda c: True):
    for i, c in enumerate(CALLS):
        if c[0].startswith(prefix) and pred(c):
            return i
    return -1


def test_settings_get_and_authz():
    print("settings")
    reset(vault=None)
    ns, _ = build()
    res = call(ns, SUPER, ns["get_audit_settings"])
    check(ok(res) == {"vault_enabled": False, "retention_days": 365, "effective_retention_days": 365,
                      "floor_days": 365, "bucket": "acme-vault", "updated_by_email": "",
                      "updated_at": None, "configured": False},
          "GET with no item: deploy defaults, configured false")
    for who in (OWNER_A, OPER_A):
        check(status(call(ns, who, ns["get_audit_settings"])) == 403, "GET is super admin only")
        check(status(put_settings(ns, who, retention_days=400)) == 403, "PUT is super admin only")


def test_settings_validation():
    reset(vault=True)
    ns, _ = build()
    res = put_settings(ns, SUPER)
    check(status(res) == 400 and detail(res) == "Nothing to change", "neither field: 400")
    for bad in (364, 3654, 400.5, True):
        res = put_settings(ns, SUPER, retention_days=bad)
        check(status(res) == 400 and detail(res) == "Retention must be between 365 and 3653 days",
              f"retention {bad!r}: 400")
    for phrase in (None, "turn off", "turn off the audit vault"):
        res = put_settings(ns, SUPER, vault_enabled=False, confirm=phrase)
        check(status(res) == 400 and detail(res) == "Type the confirmation phrase to turn the vault off",
              f"off with confirm {phrase!r}: 400")
    check(not calls("cfg.update_item") and not calls("s3.put_object"), "no validation failure writes anything")


def test_vault_off_switch():
    reset(vault=True)
    ns, _ = build()
    res = put_settings(ns, SUPER, vault_enabled=False, confirm="  Turn Off Audit Vault ")
    check(status(res) == 200 and ok(res)["vault_enabled"] is False and ok(res)["configured"] is True,
          "off with the phrase (trimmed, any case): 200 with the GET shape")
    rec = [r for r in ddb.rows if r["action"] == "audit_vault_set"]
    check(len(rec) == 1 and rec[0]["vault"] == "stored" and rec[0]["target_type"] == "setting"
          and rec[0]["target_id"] == "audit_vault" and rec[0]["entity"] == "account",
          "the switch-off is recorded in the vault (target audit_vault, type setting, scope account)")
    i_s3 = index_of("s3.put_object")
    i_cfg = index_of("cfg.update_item", lambda c: "vault_enabled" in c[1])
    check(0 <= i_s3 < i_cfg, "the vault record lands BEFORE the flag flips")
    check(len(sns.published) == 1 and sns.published[0]["Subject"] == "Audit vault switched off"
          and sns.published[0]["TopicArn"].endswith(":acme-alerts"), "the off switch notifies the topic")
    msg = sns.published[0]["Message"]
    check("super@example.com" in msg and "instance_id" not in msg and not msg.lstrip().startswith("{")
          and not re.search(r"(\$|USD)\s*\d", msg), "plain text naming the actor, no instance_id, no cost")
    CALLS.clear()
    seed("owner-a", ["space-admins"], "space_update", SA, f"space:{SA}", None, {"name": "x"})
    check(not calls("s3.put_object") and rows_in(f"space:{SA}")[-1]["vault"] == "off",
          "while off the next event writes no vault object")


def access_token(sub, groups, username):
    """An access token: a generated `username`, no email claim."""
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    payload = {"sub": sub, "cognito:groups": list(groups), "exp": 4102444800, "username": username}
    return "Bearer " + b64({"alg": "none"}) + "." + b64(payload) + ".sig"


def test_vault_off_notice_names_the_email_for_an_access_token():
    reset(vault=True)
    ns, _ = build()
    ns["_email_for_sub"] = lambda sub: {"super-1": "super@example.com"}.get(sub, "")
    res = put_settings(ns, access_token("super-1", ["admins"], "a1b2c3d4-generated"),
                       vault_enabled=False, confirm="turn off audit vault")
    check(status(res) == 200 and len(sns.published) == 1, "an access token can switch the vault off")
    msg = sns.published[0]["Message"] if sns.published else ""
    check("super@example.com" in msg and "a1b2c3d4-generated" not in msg,
          "the notice names the actor's email (from the sub), not the generated username")
    reset(vault=True)
    ns, _ = build()
    ns["_email_for_sub"] = lambda sub: ""
    put_settings(ns, access_token("super-1", ["admins"], "a1b2c3d4-generated"),
                 vault_enabled=False, confirm="turn off audit vault")
    msg = sns.published[0]["Message"] if sns.published else ""
    check("super-1" in msg and "a1b2c3d4-generated" not in msg,
          "with no email on record the notice names the sub")


def test_vault_off_refused_when_not_vaulted():
    reset(vault=True)
    ns, _ = build()
    s3.fail = "AccessDenied"
    res = put_settings(ns, SUPER, vault_enabled=False, confirm="turn off audit vault")
    check(status(res) == 502 and detail(res) == "Could not record the change in the vault; the vault is still on",
          "a vault-off record that could not be vaulted: 502")
    check(not calls("cfg.update_item") and ddb.config["vault_enabled"] is True and not sns.published,
          "... the flag is unchanged and put_audit_settings is never called")
    s3.fail = None
    CFG.fail_update = True
    res = put_settings(ns, SUPER, vault_enabled=False, confirm="turn off audit vault")
    check(status(res) == 502 and detail(res) == "Could not save the audit settings; the vault is still on",
          "a failed switch write: 502, the vault is still on")
    check(ddb.config["vault_enabled"] is True and any(r["action"] == "audit_vault_set" for r in ddb.rows),
          "... and the vaulted record of the attempt stays")


def test_sns_failure_still_200():
    reset(vault=True)
    ns, _ = build()
    sns.fail = True
    res = put_settings(ns, SUPER, vault_enabled=False, confirm="turn off audit vault")
    check(status(res) == 200 and ddb.config["vault_enabled"] is False, "an SNS failure still returns 200")
    check(LOG.having("vault-off notification failed", "WARNING"), "... logged at WARNING")


def test_vault_on_switch():
    reset(vault=False)
    ns, _ = build()
    res = put_settings(ns, SUPER, vault_enabled=True)
    check(status(res) == 200 and ok(res)["vault_enabled"] is True, "on: 200")
    i_cfg = index_of("cfg.update_item", lambda c: "vault_enabled" in c[1])
    i_s3 = index_of("s3.put_object")
    check(0 <= i_cfg < i_s3, "on: the flag flips first, then the change is vaulted")
    rec = [r for r in ddb.rows if r["action"] == "audit_vault_set"]
    check(rec and rec[0]["vault"] == "stored" and rec[0]["after"] == {"vault_enabled": True}, "the switch-on is vaulted")
    check(not sns.published, "switching on does not notify")
    CALLS.clear()
    seed("owner-a", ["space-admins"], "space_update", SA, f"space:{SA}", None, {"name": "x"})
    check(0 <= index_of("s3.put_object") < index_of("ddb.put_item"), "while on the next event writes S3 before DynamoDB")


def test_repeated_switch_value_is_not_a_change():
    print("repeated switch value (finding 8)")
    reset(vault=True)
    ns, _ = build()
    n_rows = len(ddb.rows)
    res = put_settings(ns, SUPER, vault_enabled=True)
    check(status(res) == 200 and set(ok(res)) >= {"vault_enabled", "retention_days", "configured"},
          "vault_enabled equal to the stored value: 200 with the GET shape")
    check(not calls("cfg.update_item") and len(ddb.rows) == n_rows and not sns.published,
          "... no write, no audit event, no SNS")
    reset(vault=False)
    res = put_settings(ns, SUPER, vault_enabled=False)
    check(status(res) == 200 and not calls("cfg.update_item") and not ddb.rows,
          "repeating off needs no phrase and changes nothing")
    reset(vault=True)
    res = put_settings(ns, SUPER, vault_enabled=True, retention_days=365)
    check(status(res) == 200 and not calls("cfg.update_item") and not ddb.rows,
          "an unchanged retention with a repeated switch: nothing to do")


def test_retention_changes():
    reset(vault=True, extend=True)
    ns, _ = build()
    res = put_settings(ns, SUPER, retention_days=400)
    check(status(res) == 200 and ok(res)["retention_days"] == 400 and ok(res)["effective_retention_days"] == 400,
          "retention above the floor is saved")
    check(ddb.config["vault_enabled"] is True, "a retention-only PUT leaves vault_enabled true")
    ev = [r for r in ddb.rows if r["action"] == "audit_retention_set"]
    check(ev and ev[0]["before"] == {"retention_days": 365}
          and ev[0]["after"] == {"retention_days": 400, "effective_retention_days": 400}
          and ev[0]["target_id"] == "audit_retention", "audit_retention_set with before/after")
    CALLS.clear()
    seed("owner-a", ["space-admins"], "space_update", SA, f"space:{SA}", None, {"name": "x"})
    put = calls("s3.put_object")[-1][1]
    want = clock["now"] + timedelta(days=400) - timedelta(hours=1)
    check(put.get("ObjectLockMode") == "COMPLIANCE" and put.get("ObjectLockRetainUntilDate") == want,
          "the next event is locked for now + 400 days (minus the 1 h margin)")
    put_settings(ns, SUPER, retention_days=365)
    CALLS.clear()
    seed("owner-a", ["space-admins"], "space_update", SA, f"space:{SA}", None, {"name": "y"})
    put = calls("s3.put_object")[-1][1]
    check("ObjectLockMode" not in put and "ObjectLockRetainUntilDate" not in put,
          "at the floor no lock headers are sent")
    CFG.fail_update = True
    res = put_settings(ns, SUPER, retention_days=500)
    check(status(res) == 502 and detail(res) == "Could not save the audit settings", "a failed retention write: 502")


def test_combined_put():
    reset(vault=True)
    ns, _ = build()
    res = put_settings(ns, SUPER, retention_days=500, vault_enabled=False, confirm="turn off audit vault")
    check(status(res) == 200 and ok(res)["retention_days"] == 500 and ok(res)["vault_enabled"] is False,
          "retention + off + phrase: both fields set")
    acts = [r["action"] for r in ddb.rows]
    check(acts == ["audit_retention_set", "audit_vault_set"], f"two events, retention first ({acts})")


# ── newly audited actions ────────────────────────────────────────────────────
def test_space_create_and_update():
    print("newly audited actions")
    reset(vault=True)
    ns, _ = build(extra=("create_space", "update_space"))
    ns.update({"agent_role_name": lambda o: f"acme-agent-{o}", "agent_role_trust_policy": lambda *a: {},
               "agent_space_permissions_policy": lambda *a: {}, "managed_by_tag": lambda: "acme-agent",
               "resource_prefix": lambda: "acme", "_enroll_user_in_space_group": lambda *a: None})
    body = {"name": "Gamma", "description": "", "primary_account_id": "", "regions": ["us-east-1"],
            "iam_role_arn": "", "capabilities": {}, "tags": {}}
    res = call(ns, OWNER_A, ns["create_space"], types.SimpleNamespace(model_dump=lambda: dict(body)))
    sid = ok(res)["space_id"]
    ev = [r for r in rows_in(f"space:{sid}") if r["action"] == "space_create"]
    check(len(ev) == 1 and ev[0]["after"] == {"name": "Gamma", "regions": ["us-east-1"],
                                              "primary_account_id": "111111111111"},
          "create_space writes space_create with name, regions and account")
    check(ev and ev[0]["owner_admin"] == "owner-a" and ev[0]["on_other_owner"] is False
          and ev[0]["actor_email"] == "owner-a@example.com", "... owned by its creator")

    fields = ("name", "description", "primary_account_id", "secondary_account_ids", "secondary_role_names",
              "regions", "iam_role_arn", "web_app_role_arn", "capabilities", "status", "tags",
              "model_governance", "knowledge_governance")

    def upd(**kw):
        return types.SimpleNamespace(model_dump=lambda: {f: kw.get(f) for f in fields})

    def acts(sid):
        return [r["action"] for r in rows_in(f"space:{sid}")]
    reset(vault=True)
    call(ns, SUPER, ns["update_space"], SA, upd(name="Alpha 2"))
    check(acts(SA) == ["space_update"], "a name change emits space_update")
    ev = rows_in(f"space:{SA}")[0]
    check(ev["before"] == {"name": "Alpha"} and ev["after"] == {"name": "Alpha 2"},
          "before/after hold the changed fields only")
    check(ev["actor_role"] == "super_admin" and ev["on_other_owner"] is True and ev["owner_admin"] == "owner-a",
          "A1: a super admin's space_update on another admin's space is marked")
    reset(vault=True)
    call(ns, OWNER_A, ns["update_space"], SA, upd(primary_account_id="222222222222"))
    check(acts(SA) == ["space_binding_update"], "a binding change emits space_binding_update only")
    reset(vault=True)
    call(ns, OWNER_A, ns["update_space"], SA, upd(primary_account_id="222222222222", regions=["eu-west-1"]))
    check(sorted(acts(SA)) == ["space_binding_update", "space_update"], "both together emit both")
    check(rows_in(f"space:{SA}")[-1]["on_other_owner"] is False, "the owner acting on their own space is not flagged")
    reset(vault=True)
    call(ns, OWNER_A, ns["update_space"], SA, upd(name="Alpha"))
    check(acts(SA) == [], "an unchanged value emits nothing")


def test_discover_dismiss_investigation():
    reset(vault=True)
    ns, reg = build(db_rows=[{"instance_id": "db-c", "candidate_space_ids": {SA}, "member_space_ids": set(),
                              "display_name": "orders"}],
                    extra=("_run_discovery", "dismiss_space_candidate", "delete_investigation",
                           "_index_one_db", "_index_existing_dbs", "_match_discovered_db",
                           "_rows_this_scan_no_longer_sees", "_discovery_rds_tags",
                           "_unsupported_reason", "_discovery_unsupported_reason", "_cap_unsupported",
                           "_regions_for_account", "_regions_by_account"))
    ns.setdefault("UNSUPPORTED_PER_REGION_MAX", 20)
    ns.setdefault("_redact_identifiers", lambda s: s)
    reg.create_instance = lambda d: dict(d, instance_id="db-new")
    # discover_all takes the per-account scan plan; completion is keyed on exactly those pairs.
    ns["db_discovery"] = types.SimpleNamespace(discover_all=lambda **kw: (
        [{"identifier": "inv", "region": "us-east-1", "account_id": "111111111111", "instance_arn": "arn:rds:inv"}], [],
        {(a, r): {"clusters_ok": True, "instances_ok": True}
         for a, rs in (kw.get("regions_by_account") or {}).items() for r in rs}))
    # Discovery now runs in the background; the audit event is written by _run_discovery.
    as_user("owner-a", ["admins"], "owner-a@example.com")
    ns["_run_discovery"](SA, "owner-a")
    ev = [r for r in rows_in(f"space:{SA}") if r["action"] == "space_discover"]
    check(len(ev) == 1 and ev[0]["after"] == {"regions": ["us-east-1"], "found": 1},
          "discovery writes space_discover with regions and found")
    res = call(ns, OWNER_A, ns["dismiss_space_candidate"], SA, "db-c", background_tasks=FakeBackgroundTasks())
    ev = [r for r in rows_in(f"space:{SA}") if r["action"] == "space_candidate_dismiss"]
    check(ok(res) == {"message": "Dismissed", "removed_record": True} and len(ev) == 1
          and ev[0]["target_id"] == "db-c" and ev[0]["target_type"] == "database"
          and ev[0]["target_label"] == "orders", "dismissing a candidate writes space_candidate_dismiss")
    res = call(ns, SUPER, ns["delete_investigation"], SA, "inv-9")
    ev = [r for r in rows_in(f"space:{SA}") if r["action"] == "space_investigation_delete"]
    check(status(res) == 200 and len(ev) == 1 and ev[0]["target_id"] == "inv-9",
          "A1: deleting an investigation writes space_investigation_delete")
    check(ev and ev[0]["actor_role"] == "super_admin" and ev[0]["on_other_owner"] is True,
          "... a super admin's delete in another admin's space is marked")


def _mon(**kw):
    vals = {"tier1_frequency": "5min", "tier1_enabled": None, "tier2_frequency": None,
            "tier2_enabled": None, "retention_days": None}
    vals.update(kw)
    ns_ = types.SimpleNamespace(**vals)
    ns_.dict = lambda exclude_none=False: {k: v for k, v in vals.items()
                                            if not (exclude_none and v is None) and k != "instance_ids"} \
        if exclude_none else dict(vals)
    return ns_


def test_monitoring_and_rename_fan_out():
    shared = {"instance_id": "db-s", "member_space_ids": {SA, SB}, "display_name": "orders",
              "tier1_frequency": "1min"}
    reset(vault=True)
    ns, reg = build(db_rows=[shared, dict(shared, instance_id="db-t")],
                    extra=("update_monitoring_config", "bulk_update_monitoring", "_audit_monitoring_set",
                           "_monitoring_set_result", "rename_database"))
    ns.update({"TIER1_FREQUENCIES": {"5min": 5}, "TIER2_FREQUENCIES": {},
               "_apply_monitoring_config": lambda iid, c, name="": {"results": {}, "failed": {}, "applied": dict(c)},
               "_tier1_cadence_block": lambda *a: "", "_tier1_cadence_check": lambda *a: ("", ""),
               "_tier1_pause_warning": lambda *a: ""})
    call(ns, SUPER, ns["update_monitoring_config"], "db-s", _mon())
    ev = [r for r in ddb.rows if r["action"] == "monitoring_config_set"]
    ents = sorted(r["entity"] for r in ev)
    check(ents == ["account", f"space:{SA}", f"space:{SB}"],
          "PUT /api/databases/{iid}/monitoring writes monitoring_config_set, fanned out to the member spaces")
    check(ev and ev[0]["before"]["tier1_frequency"] == "1min" and ev[0]["after"] == {"tier1_frequency": "5min"},
          "... with the admin endpoint's before/after shape")
    fan = [r for r in ev if r.get("fanout")]
    check(all(r["on_other_owner"] is True and r["owner_admin"] in ("owner-a", "owner-b") for r in fan),
          "each fan-out row carries its own space's owner")
    reset(vault=True)
    bulk = _mon()
    bulk.instance_ids = ["db-s", "db-t", "db-missing"]
    call(ns, SUPER, ns["bulk_update_monitoring"], bulk)
    prim = [r for r in ddb.rows if r["action"] == "monitoring_config_set" and r["entity"] == "account"]
    check(sorted(r["target_id"] for r in prim) == ["db-s", "db-t"], "bulk writes one event per existing database")
    reset(vault=True)
    call(ns, SUPER, ns["rename_database"], "db-s", types.SimpleNamespace(display_name="orders-v2"))
    ents = sorted(r["entity"] for r in ddb.rows if r["action"] == "database_rename")
    check(ents == ["account", f"space:{SA}", f"space:{SB}"], "database_rename fans out to the member spaces")


def test_decommission_by_a_super_admin_is_marked():
    reset(vault=True)
    ns, reg = build(db_rows=[{"instance_id": "db-s", "member_space_ids": {SA, SB}}],
                    extra=("delete_database",))
    ns.update({"_can_manage_instance": lambda *a: False, "_begin_teardown": lambda *a, **k: True,
               "_untag_space_from_database": lambda *a: (set(), True)})
    res = call(ns, SUPER, ns["delete_database"], "db-s", decommission=True,
               background_tasks=FakeBackgroundTasks())
    ev = [r for r in ddb.rows if r["action"] == "database_decommission"]
    fan = [r for r in ev if r.get("fanout")]
    check(status(res) == 200 and len(ev) == 3 and len(fan) == 2, "decommission is fanned out to both members")
    check(all(r["actor_role"] == "super_admin" and r["on_other_owner"] is True for r in fan),
          "A1: each member space sees a super admin acting on their space")


# ── teardown: the request returns, the background run is audited ─────────────


TEARDOWN = ("delete_database", "dismiss_space_candidate", "delete_space", "_untag_space_from_database",
            "_reassign_reach_if_needed", "_teardown_database", "_notify_teardown_failed",
            "_teardown_heartbeat", "_background_teardown", "_begin_teardown", "_background_delete_space")


def _lattice_row(**over):
    r = {"instance_id": "db-l", "member_space_ids": {SA}, "reach_space_id": SA, "status": "configured",
         "network_method": "lattice", "vpc_id": "vpc-db", "region": "us-east-1",
         "account_id": "111111111111", "endpoint": "db.example.internal", "vpce_id": "vpce-1",
         "display_name": "orders", "space_secrets": {SA: SECRET}, "secret_arn": SECRET}
    r.update(over)
    return r


def _teardown_ns(rows, outcomes):
    """`outcomes`: the cleanup_lattice results, in order. Each cleanup moves the fake clock
    45 s, as a real Lattice teardown (wait for the endpoint, then the gateway) does."""
    ns, reg = build(db_rows=rows, extra=TEARDOWN)
    seq = list(outcomes)
    lattice_calls = []
    def cleanup_lattice(**kw):
        lattice_calls.append(kw)
        clock["mono"] += 45.0
        return seq.pop(0)
    ns.update({
        "network_configurator": types.SimpleNamespace(agent_vpc_id="vpc-agent", cleanup_lattice=cleanup_lattice),
        "_can_manage_instance": lambda auth, iid: True,
        "_get_scheduler_client": lambda: (_ for _ in ()).throw(RuntimeError("no scheduler")),
        "_schedule_name": lambda iid: f"t2-{iid}", "_tier1_schedule_name": lambda iid: f"t1-{iid}",
        "_delete_db_alarms": lambda iid: None,
        "SPACE_TEARDOWN_WAIT_S": 600, "SPACE_TEARDOWN_POLL_S": 5,
    })
    return ns, reg, lattice_calls


DONE = {"steps": ["Deleted resource endpoint vpce-1", "Resource gateway rgw-1 is gone"], "complete": True}


LEFT = {"steps": ["Teardown stopped at resource configuration: ConflictException"], "complete": False}


def acts_of(action):
    return [r for r in ddb.rows if r["action"] == action and not r.get("fanout")]


def test_lattice_teardown_outlives_the_request_deadline():
    print("teardown runs after the response (finding: audit deadline)")
    reset(vault=True)
    ns, reg, lat = _teardown_ns([_lattice_row()], [DONE])
    bg = FakeBackgroundTasks()
    t0 = clock["mono"]
    res = call(ns, OWNER_A, ns["delete_database"], "db-l", space_id=SA, background_tasks=bg)
    check(ok(res) == {"message": "Deleting", "status": "deleting", "cleanup": []} and clock["mono"] == t0,
          "the request returns Deleting without running the teardown")
    check(reg.rows["db-l"]["status"] == "deleting" and not lat, "the record is claimed deleting; nothing torn down yet")
    ev = acts_of("database_space_remove")
    check(len(ev) == 1 and ev[0]["vault"] == "stored" and ev[0]["after"]["teardown"] is True,
          "the removal is audited inside the request")
    check(len(bg.tasks) == 1 and getattr(bg.tasks[0][0], "__name__", "") == "run"
          and bg.tasks[0][1][0] is ns["_background_teardown"], "scheduled as ctx.run(_background_teardown)")
    bg.run()
    check(clock["mono"] - t0 >= 45.0 and "db-l" not in reg.rows, "the background teardown ran 45 s and deleted the record")
    done = acts_of("database_teardown_complete")
    check(len(done) == 1 and done[0]["vault"] == "stored" and done[0]["after"]["trigger"] == "database_space_remove"
          and done[0]["after"]["cleanup"] == DONE["steps"], "its completion is audited with the cleanup steps")
    check(done and done[0]["actor_sub"] == "owner-a" and done[0]["request_id"] == "req-77",
          "the completion record carries the caller")
    check(not LOG.having("AUDIT_RECORD_DROPPED"), "nothing is dropped although the teardown outlives the 20 s deadline")


def test_teardown_double_click_is_a_no_op():
    reset(vault=True)
    ns, reg, lat = _teardown_ns([_lattice_row()], [DONE])
    bg = FakeBackgroundTasks()
    call(ns, OWNER_A, ns["delete_database"], "db-l", space_id=SA, background_tasks=bg)
    bg2 = FakeBackgroundTasks()
    res = call(ns, OWNER_A, ns["delete_database"], "db-l", space_id=SA, background_tasks=bg2)
    check(ok(res) and ok(res)["status"] == "deleting" and not bg2.tasks, "a second delete while deleting schedules nothing")
    check(len(acts_of("database_space_remove")) == 1, "and writes no second removal record")
    bg.run()
    check(len(lat) == 1, "one teardown ran")


def test_teardown_failure_then_retry():
    reset(vault=True)
    ns, reg, lat = _teardown_ns([_lattice_row()], [LEFT, DONE])
    bg = FakeBackgroundTasks()
    call(ns, OWNER_A, ns["delete_database"], "db-l", space_id=SA, background_tasks=bg)
    bg.run()
    row = reg.rows.get("db-l") or {}
    check(row.get("status") == "delete_failed" and row.get("config_log") == LEFT["steps"]
          and "Retry" in row.get("config_error", ""), "a refused step keeps the record as delete_failed with its steps")
    fail = acts_of("database_teardown_failed")
    check(len(fail) == 1 and fail[0]["after"]["result"] == "failed" and fail[0]["after"]["cleanup"] == LEFT["steps"],
          "the failure is audited with the steps")
    notes = [m for m in sns.published if m.get("Subject") == "Database teardown incomplete"]
    check(len(notes) == 1 and "orders" in notes[0]["Message"] and LEFT["steps"][0] in notes[0]["Message"],
          "and a notification names the database and what is left")
    bg = FakeBackgroundTasks()
    res = call(ns, OWNER_A, ns["delete_database"], "db-l", space_id=SA, background_tasks=bg)
    check(ok(res) and ok(res)["status"] == "deleting" and reg.rows["db-l"]["status"] == "deleting",
          "deleting it again (Retry) claims it again")
    bg.run()
    check("db-l" not in reg.rows and len(lat) == 2 and len(acts_of("database_teardown_complete")) == 1,
          "the retry finishes the teardown and deletes the record")


def test_dismiss_and_space_delete_run_in_the_background():
    reset(vault=True)
    cand = _lattice_row(instance_id="db-c", member_space_ids=set(), candidate_space_ids={SA},
                        status="config_failed", space_secrets={})
    ns, reg, lat = _teardown_ns([cand], [DONE])
    bg = FakeBackgroundTasks()
    res = call(ns, OWNER_A, ns["dismiss_space_candidate"], SA, "db-c", background_tasks=bg)
    check(ok(res) == {"message": "Deleting", "status": "deleting", "removed_record": True} and not lat,
          "dismissing a failed Lattice onboard returns at once")
    check(SA in reg.rows["db-c"]["candidate_space_ids"], "the row stays in the candidates (Deleting…) until it is gone")
    bg.run()
    check("db-c" not in reg.rows and acts_of("database_teardown_complete")[0]["after"]["trigger"] == "space_candidate_dismiss",
          "the background run removes it and audits the completion")
    reset(vault=True)
    ns, reg, lat = _teardown_ns([_lattice_row()], [LEFT, DONE])
    ns["iam_client"] = types.SimpleNamespace(delete_role=lambda n: {"status": "deleted"})
    ns["_role_deletable_for_admin"] = lambda n, owner: True
    bg = FakeBackgroundTasks()
    res = call(ns, OWNER_A, ns["delete_space"], SA, background_tasks=bg)
    check(ok(res) and ok(res)["status"] == "deleting" and SPACES.get_space(SA)["status"] == "deleting" and not lat,
          "delete space returns at once and keeps the space row as deleting")
    check(acts_of("space_delete")[0]["after"]["result"] == "teardown_started", "space_delete records teardown_started")
    bg2 = FakeBackgroundTasks()
    res = call(ns, OWNER_A, ns["delete_space"], SA, background_tasks=bg2)
    check(ok(res)["status"] == "deleting" and not bg2.tasks, "a second delete of a deleting space is a no-op")
    bg.run()
    check(SPACES.get_space(SA) and SPACES.get_space(SA)["status"] == "delete_failed"
          and reg.rows["db-l"]["status"] == "delete_failed", "a database left behind keeps the space as delete_failed")
    check(acts_of("space_delete_teardown_failed")[0]["after"]["databases_left"] == ["db-l"],
          "space_delete_teardown_failed names it")
    bg = FakeBackgroundTasks()
    call(ns, OWNER_A, ns["delete_space"], SA, background_tasks=bg)
    bg.run()
    check(SPACES.get_space(SA) is None and "db-l" not in reg.rows
          and len(acts_of("space_delete_teardown_complete")) == 1, "deleting the space again finishes the job")
    check(not LOG.having("AUDIT_RECORD_DROPPED"), "no record dropped on any of these paths")
    SPACES.reset()


# ── configure: actor plumbing, modes, results ────────────────────────────────
CONFIGURE = ("configure_space_database", "_background_configure_network", "_configure_single_db",
             "batch_configure_databases", "_background_batch_configure", "_unsupported_reason",
             "_teardown_reason",
             "_replica_tags", "_allowed_secret_hosts", "_secret_host_refusal", "_port_range_warning",
             "_secret_namespace_refusal")
SECRET = "arn:aws:secretsmanager:us-east-1:111111111111:secret:acme/owner-a/ro"


def _candidate(**over):
    r = {"instance_id": "db-1", "candidate_space_ids": {SA}, "member_space_ids": set(), "tags": {},
         "endpoint": "db.example.internal", "account_id": "111111111111", "region": "us-east-1",
         "vpc_id": "vpc-1", "port": 5432, "display_name": "orders"}
    r.update(over)
    return r


def onboard_events(sid=SA):
    return [r for r in rows_in(f"space:{sid}") if r["action"] == "space_db_onboard"]


def test_configure_records_the_caller():
    reset(vault=True)
    NET["result"] = {"status": "configured", "method": "peering", "steps": []}
    ns, reg = build(db_rows=[_candidate()], extra=CONFIGURE)
    bg = FakeBackgroundTasks()
    res = call(ns, OWNER_A, ns["configure_space_database"], SA, "db-1", background_tasks=bg,
               data=types.SimpleNamespace(secret_arn=SECRET, database_name=None))
    check(ok(res) and ok(res)["status"] == "configuring" and len(bg.tasks) == 1, "first onboard schedules the task")
    fn, a, kw = bg.tasks[0]
    check(getattr(fn, "__name__", "") == "run" and a[0] is ns["_background_configure_network"]
          and kw.get("actor_sub") == "owner-a", "scheduled as ctx.run(_background_configure_network, actor_sub=...)")
    clock["mono"] += 30.0    # the background run outlives the request's 20 s deadline
    bg.run()
    ev = onboard_events()
    check(len(ev) == 1, "exactly one event for the attempt")
    e = ev[0] if ev else {}
    check(e.get("actor_sub") == "owner-a" and e.get("actor_email") == "owner-a@example.com"
          and e.get("request_id") == "req-77", "the event records the caller, not 'system'")
    check(e.get("after", {}).get("mode") == "first_onboard" and e.get("after", {}).get("result") == "configured"
          and e.get("after", {}).get("batch") is False and e.get("after", {}).get("member_space_ids") == [SA],
          "mode first_onboard, result configured, batch false, now a member")
    check(e.get("vault") == "stored" and not LOG.having("AUDIT_RECORD_DROPPED"), "not dropped past the request deadline")
    # Reconfigure: the space is now a member and the reach provider.
    reset(vault=True)
    reg.rows["db-1"]["status"] = "configured"
    bg = FakeBackgroundTasks()
    res = call(ns, OWNER_A, ns["configure_space_database"], SA, "db-1", background_tasks=bg, data=None)
    bg.run()
    ev = onboard_events()
    check(len(ev) == 1 and ev[0]["after"]["mode"] == "reconfigure" and ev[0]["after"]["result"] == "configured",
          "a reconfigure of a member emits mode reconfigure")
    # A failed network setup.
    reset(vault=True)
    NET["result"] = {"status": "failed", "error": "no route", "steps": []}
    bg = FakeBackgroundTasks()
    call(ns, OWNER_A, ns["configure_space_database"], SA, "db-1", background_tasks=bg, data=None)
    bg.run()
    ev = onboard_events()
    check(len(ev) == 1 and ev[0]["after"]["result"] == "config_failed", "a failed attempt records config_failed")
    # An unexpected exception.
    reset(vault=True)
    ns["network_configurator"] = types.SimpleNamespace(
        agent_vpc_id="vpc-agent", configure_network=lambda **kw: (_ for _ in ()).throw(KeyError("x")))
    bg = FakeBackgroundTasks()
    call(ns, OWNER_A, ns["configure_space_database"], SA, "db-1", background_tasks=bg, data=None)
    bg.run()
    ev = onboard_events()
    check(len(ev) == 1 and ev[0]["after"]["result"] == "error: KeyError", "an exception records error: <Class>")
    NET["result"] = {"status": "configured", "method": "peering", "steps": []}


def test_configure_lost_race():
    reset(vault=True)
    ns, reg = build(db_rows=[_candidate()], extra=CONFIGURE)
    ns["network_configurator"] = types.SimpleNamespace(agent_vpc_id="vpc-agent", configure_network=lambda **kw: (
        reg.rows["db-1"].update({"reach_space_id": SB}) or {"status": "configured", "method": "peering", "steps": []}))
    bg = FakeBackgroundTasks()
    call(ns, OWNER_A, ns["configure_space_database"], SA, "db-1", background_tasks=bg,
         data=types.SimpleNamespace(secret_arn=SECRET, database_name=None))
    bg.run()
    ev = onboard_events()
    check(len(ev) == 1 and ev[0]["after"]["result"] == "lost_race", "another space onboarding first records lost_race")


def test_batch_configure_by_a_super_admin():
    print("batch configure (real ThreadPoolExecutor)")
    reset(vault=True)
    rows = [{"instance_id": f"db-{i}", "member_space_ids": {SB}, "reach_space_id": SB, "secret_arn": SECRET,
             "vpc_id": "vpc-db", "endpoint": "db.example.internal",
             "account_id": "111111111111", "region": "us-east-1", "tags": {}} for i in range(3)]
    ns, reg = build(db_rows=rows, extra=CONFIGURE)
    bg = FakeBackgroundTasks()
    res = call(ns, SUPER, ns["batch_configure_databases"], SB,
               types.SimpleNamespace(instance_ids=[r["instance_id"] for r in rows]), background_tasks=bg)
    check(ok(res) and ok(res)["queued"] == 3, "the batch is queued")
    fn, a, kw = bg.tasks[0]
    check(getattr(fn, "__name__", "") == "run" and a[0] is ns["_background_batch_configure"]
          and kw.get("actor_sub") == "super-1", "scheduled as ctx.run(_background_batch_configure, actor_sub=...)")
    clock["mono"] += 30.0
    bg.run()
    ev = onboard_events(SB)
    check(len(ev) == 3 and sorted(r["target_id"] for r in ev) == ["db-0", "db-1", "db-2"], "one event per instance")
    check(all(r["after"]["batch"] is True and r["after"]["mode"] == "reconfigure"
              and r["after"]["result"] == "configured" for r in ev), "batch true, mode reconfigure")
    check(all(r["actor_email"] == "super@example.com" and r["actor_role"] == "super_admin"
              and r["on_other_owner"] is True and r["request_id"] == "req-77" for r in ev),
          "A1: every worker's event carries the super admin, marked on another admin's space")
    check(all(r["vault"] == "stored" for r in ev) and not LOG.having("AUDIT_RECORD_DROPPED"),
          "none is dropped although the run outlives the request deadline")


# ── source guards ────────────────────────────────────────────────────────────
def test_pool_and_background_rules():
    print("source guards")
    tree = ast.parse(APP_SRC)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_background_batch_configure")
    attr_calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)]
    check(not [c for c in attr_calls if c.func.attr == "submit"],
          "_background_batch_configure (and process_cidr_group) has no bare .submit(")
    sic = [c for c in attr_calls if c.func.attr == "submit_in_context"
           and isinstance(c.func.value, ast.Name) and c.func.value.id == "audit_log"]
    check(len(sic) == 3, f"all three pools go through audit_log.submit_in_context ({len(sic)})")
    inner = [c for c in sic if isinstance(c.args[1], ast.Name) and c.args[1].id == "_configure_single_db"]

    def kw(c, name):
        return next((k.value for k in c.keywords if k.arg == name), None)
    check(len(inner) == 2 and all(isinstance(kw(c, "actor_sub"), ast.Name) and kw(c, "actor_sub").id == "actor_sub"
                                  and isinstance(kw(c, "batch"), ast.Constant) and kw(c, "batch").value is True
                                  for c in inner), "the two inner submits pass actor_sub=actor_sub, batch=True")
    direct = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
              and c.func.id == "_configure_single_db"]
    check(direct and all(kw(c, "actor_sub") is not None and kw(c, "batch") is not None for c in direct),
          "the sequential cross-region call passes them too")
    adds = [c for c in ast.walk(tree) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            and c.func.attr == "add_task"]
    sched = [c for c in adds if any(isinstance(a, ast.Name) and a.id in ("_background_configure_network",
                                                                         "_background_batch_configure")
                                    for a in c.args)]
    check(len(sched) == 2 and all(isinstance(c.args[0], ast.Attribute) and c.args[0].attr == "run"
                                  and isinstance(c.args[0].value, ast.Name) and c.args[0].value.id == "ctx"
                                  for c in sched), "both configure add_task calls pass ctx.run first")
    for name in ("configure_space_database", "batch_configure_databases"):
        body = _extract(name)
        check("ctx = audit_log.detached_context()" in body and "actor_sub=get_user_id(" in body,
              f"{name} schedules through detached_context with the caller's sub")
    check("threading.Thread(" not in APP_SRC, "app.py starts no bare Thread")
    single = _extract("_configure_single_db")
    check(single.count('"space_db_onboard"') == 1 and 'audit("system"' not in single
          and "finally:\n        _audit_configure(audit_result)" in single,
          "_configure_single_db emits one event, from _audit_configure in the finally")


def test_no_cost_figures_in_audit_text():
    sec = APP_SRC.split("# ─── REST API: Audit", 1)[1].split("# ─── REST API: Recommendations", 1)[0]
    check(not re.search(r"(\$|USD)\s*\d", sec), "no price figure in the audit endpoints' text")


def _run():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001 - report and continue
            check(False, f"{t.__name__} raised {type(e).__name__}: {e}")
    print(f"pass={passed} fail={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
