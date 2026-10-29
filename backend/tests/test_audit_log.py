# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The admin audit log: vault write path, settings, projections, cursor, query, verify.

Runs the real ``tools.aws.audit_log`` and ``GovernanceRegistry`` against recording fake
S3 / DynamoDB (low-level) / CloudWatch clients and a fake clock, so the order of calls,
the request and event budgets, the deferred metrics and the stop rules of the paged
query are all observable. A small in-memory evaluator runs the real Key/Filter
expressions over the fake table. Hermetic, no AWS.

Run:  python3 backend/tests/test_audit_log.py
"""
import ast
import base64
import concurrent.futures
import hashlib
import io
import json
import logging
import os
import pathlib
import re
import sys
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
REPO = pathlib.Path(__file__).resolve().parents[2]

for _var in ("AUDIT_TABLE", "CONFIG_TABLE", "AUDIT_BUCKET", "AUDIT_VAULT_DEFAULT",
             "AUDIT_RETENTION_FLOOR", "AUDIT_EXTEND_RETENTION"):
    os.environ.pop(_var, None)
os.environ["RESOURCE_PREFIX"] = "acme"

from botocore.exceptions import ClientError  # noqa: E402

# Spaces registry fake, imported lazily by audit_log._space_info.
SPACES = {
    "sA": {"space_id": "sA", "name": "Alpha", "owner_admin": "ownerA"},
    "sB": {"space_id": "sB", "name": "Bravo", "owner_admin": "ownerB"},
    "sC": {"space_id": "sC", "name": "Charlie", "owner_admin": "super1"},
    "sD": {"space_id": "sD", "name": "Delta", "owner_admin": "ownerD"},
}
_spaces_mod = types.ModuleType("tools.aws.spaces_client")
_spaces_mod.spaces_registry = types.SimpleNamespace(get_space=lambda sid: SPACES.get(sid))
sys.modules["tools.aws.spaces_client"] = _spaces_mod

from tools.aws import audit_log as al  # noqa: E402
from tools.aws import governance_store as gs  # noqa: E402

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

CALLS = []


# ── in-memory DynamoDB expression evaluator ─────────────────────────────────
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
        assert not t.startswith("#") or t in self.names, f"undeclared name {t}"
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
        if op in ("=", "<>", "<", "<=", ">", ">="):
            self.take()
            return ("cmp", op, left, self.operand())
        raise AssertionError(f"bad expression near {t}")

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


# ── fake clients ─────────────────────────────────────────────────────────────
class FakeS3:
    def __init__(self):
        self.reset()

    def reset(self):
        self.objects = {}   # key -> [version dict]
        self.fail = None
        self.advance = 0.0
        self.n = 0

    def put_object(self, **kw):
        CALLS.append(("s3.put_object", kw))
        clock["mono"] += self.advance
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

    def _find(self, key, version=None):
        vers = self.objects.get(key) or []
        if not vers:
            raise cerr("NoSuchKey")
        if version is None:
            return vers[-1]
        for v in vers:
            if v["VersionId"] == version:
                return v
        raise cerr("NoSuchVersion")

    def head_object(self, Bucket, Key):
        CALLS.append(("s3.head_object", Key))
        if self.fail:
            raise cerr(self.fail)
        if not self.objects.get(Key):
            raise cerr("404")
        return {}

    def get_object(self, Bucket, Key, VersionId=None, ChecksumMode=None):
        CALLS.append(("s3.get_object", {"Key": Key, "VersionId": VersionId,
                                        "ChecksumMode": ChecksumMode}))
        if self.fail:
            raise cerr(self.fail)
        v = self._find(Key, VersionId)
        return {"Body": io.BytesIO(v["Body"]), "ChecksumSHA256": v["ChecksumSHA256"],
                "LastModified": v["LastModified"], "VersionId": v["VersionId"],
                "ObjectLockMode": v["ObjectLockMode"],
                "ObjectLockRetainUntilDate": v["ObjectLockRetainUntilDate"]}

    def list_object_versions(self, Bucket, Prefix):
        CALLS.append(("s3.list_object_versions", Prefix))
        out = []
        for k, vers in self.objects.items():
            if k.startswith(Prefix):
                out.extend({"Key": k, "VersionId": v["VersionId"]} for v in vers)
        return {"Versions": out}


class FakeDDB:
    def __init__(self):
        self.reset()

    def reset(self):
        self.tables = {}
        self.config = None          # the audit_settings item (python dict) or None
        self.config_fail = False
        self.put_fail = False
        self.get_fail = False
        self.put_advance = 0.0
        self.query_advance = 0.0
        self.queries = []

    def put_item(self, TableName, Item, ConditionExpression=None):
        CALLS.append(("ddb.put_item", TableName, ConditionExpression))
        clock["mono"] += self.put_advance
        if self.put_fail:
            raise cerr("ProvisionedThroughputExceededException")
        row = al.from_item(Item)
        rows = self.tables.setdefault(TableName, [])
        if ConditionExpression == "attribute_not_exists(entity)" and any(
                r["entity"] == row["entity"] and r["ts"] == row["ts"] for r in rows):
            raise cerr("ConditionalCheckFailedException")
        rows.append(row)
        return {}

    def get_item(self, TableName, Key):
        CALLS.append(("ddb.get_item", TableName))
        key = al.from_item(Key)
        if "config_key" in key:
            if self.config_fail:
                raise cerr("InternalServerError")
            return {"Item": al.to_item(self.config)} if self.config is not None else {}
        if self.get_fail:
            raise cerr("InternalServerError")
        for r in self.tables.get(TableName, []):
            if r["entity"] == key["entity"] and r["ts"] == key["ts"]:
                return {"Item": al.to_item(r)}
        return {}

    def query(self, **p):
        CALLS.append(("ddb.query", p.get("IndexName")))
        self.queries.append(p)
        clock["mono"] += self.query_advance
        names = p.get("ExpressionAttributeNames") or {}
        values = {k: al._from_av(v) for k, v in (p.get("ExpressionAttributeValues") or {}).items()}
        used = " ".join([p.get("KeyConditionExpression", ""), p.get("FilterExpression", "")])
        for n in names:
            assert n in used, f"unused ExpressionAttributeName {n}"
        for v in values:
            assert re.search(re.escape(v) + r"\b", used), f"unused ExpressionAttributeValue {v}"
        kc = Expr(p["KeyConditionExpression"], names, values)
        fe = Expr(p.get("FilterExpression"), names, values)
        idx = p.get("IndexName")
        rows = [r for r in self.tables.get(p["TableName"], []) if not idx or "month" in r]
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
        out["Count"] = len(out["Items"])
        if more:
            last = page[-1]
            lek = {"entity": last["entity"], "ts": last["ts"]}
            if idx:
                lek["month"] = last["month"]
            out["LastEvaluatedKey"] = al.to_item(lek)
        return out


class FakeCW:
    def __init__(self):
        self.reset()

    def reset(self):
        self.calls = []
        self.fail = False

    def put_metric_data(self, Namespace, MetricData):
        CALLS.append(("cw.put_metric_data", Namespace))
        self.calls.append((Namespace, {d["MetricName"]: d["Value"] for d in MetricData}))
        if self.fail:
            raise cerr("Throttling")
        return {}


s3, ddb, cw = FakeS3(), FakeDDB(), FakeCW()
al._clients.update({"s3": s3, "dynamodb": ddb, "cloudwatch": cw})


class LogCap(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))

    def having(self, text, level=None):
        return [m for lv, m in self.records if text in m and (level is None or lv == level)]


LOG = LogCap()
logging.getLogger("audit-log").addHandler(LOG)
logging.getLogger("audit-log").setLevel(logging.DEBUG)
logging.getLogger("audit-log").propagate = False

AUDIT = "acme-audit"


def reset(vault=None, bucket="acme-vault", floor=365, extend=False, default="off"):
    CALLS.clear()
    s3.reset()
    ddb.reset()
    cw.reset()
    LOG.records.clear()
    clock["mono"], clock["now"] = 1000.0, T0
    al.AUDIT_BUCKET, al.AUDIT_RETENTION_FLOOR = bucket, floor
    al.AUDIT_EXTEND_RETENTION, al.AUDIT_VAULT_DEFAULT = extend, default
    al._settings_cache.update({"value": None, "at": None})
    al._settings_warned_at[0] = None
    al.drain_pending_metrics()
    al._space_cache.clear()
    al._misconfig_logged[0] = False
    al.AUDIT_CONTEXT.set({})
    if vault is not None:
        ddb.config = {"config_key": "audit_settings", "vault_enabled": vault}


def rows(table=AUDIT):
    return ddb.tables.get(table, [])


def calls(prefix):
    return [c for c in CALLS if c[0].startswith(prefix)]


def sent_metrics():
    out = {}
    for _, d in cw.calls:
        for k, v in d.items():
            out[k] = out.get(k, 0) + v
    return out


def ctx(sub="admin1", groups=("space-admins",), email="admin1@example.com", **extra):
    d = {"sub": sub, "email": email, "groups": list(groups), "request_id": "req-1",
         "source_ip": "198.51.100.7"}
    d.update(extra)
    return d


def event(action="space_user_assign", target="op1", scope="space:sA", before=None,
          after=None, actor="admin1", **kw):
    return al.write_event(actor, action, target, scope, before, after, **kw)


# ═════════════════════════════════════════════════════════════════════════════
print("== names come from tools.resource_names (RESOURCE_PREFIX=acme) ==")
reset(vault=False)
event()
check(any(c[0] == "ddb.put_item" and c[1] == "acme-audit" for c in CALLS), "index rows go to acme-audit")
check(any(c[0] == "ddb.get_item" and c[1] == "acme-config" for c in CALLS), "settings are read from acme-config")
check(cw.calls and cw.calls[0][0] == "acme/Audit", "metrics use namespace acme/Audit")
src = (REPO / "backend/tools/aws/audit_log.py").read_text()
check("-audit\"" not in src and "-audit'" not in src, "no hard-coded '<prefix>-audit' fallback in audit_log")

print("")
print("== write order, write-once and checksums (vault on) ==")
reset(vault=True)
tok = al.AUDIT_CONTEXT.set(ctx(email="Admin1@Example.com"))
res = event(after={"auto_created": True})
al.AUDIT_CONTEXT.reset(tok)
check(res == "stored", "write_event returns 'stored'")
order = [c[0] for c in CALLS if c[0] in ("s3.put_object", "ddb.put_item")]
check(order == ["s3.put_object", "ddb.put_item"], "S3 is written before DynamoDB")
put = calls("s3.put_object")[0][1]
check(put["IfNoneMatch"] == "*", "PutObject carries IfNoneMatch='*'")
check(put["ChecksumAlgorithm"] == "SHA256", "PutObject carries ChecksumAlgorithm=SHA256")
check(put["ChecksumSHA256"] == base64.b64encode(hashlib.sha256(put["Body"]).digest()).decode(),
      "ChecksumSHA256 matches the body")
check(put["Bucket"] == "acme-vault" and put["ContentType"] == "application/json", "bucket and content type")
row = rows()[0]
check(row["s3_version_id"] == "v1", "the row stores the returned VersionId")
check(row["s3_key"] == put["Key"], "the row stores the vault key")
check(row["sha256"] == hashlib.sha256(put["Body"]).hexdigest(), "the row stores the body's SHA-256 (hex)")
check(all(c[2] == "attribute_not_exists(entity)" for c in calls("ddb.put_item")),
      "index PutItem is insert-only (ConditionExpression)")
check(row["vault"] == "stored" and row["v"] == 2, "row vault=stored, v=2")
record = json.loads(put["Body"])
check(record["event_id"] == row["event_id"] and len(record["event_id"]) == 32, "event_id is a uuid4 hex")
check(row["ts"] == f"{record['ts']}#{record['event_id'][:8]}", "index sort key is ts#event_id[:8]")
check("search" in row and "search" not in record, "search lives in the index only")
check("admin1@example.com" in row["search"] and "added operator" in row["search"], "search haystack is lower-cased")

print("")
print("== failures never raise ==")
reset(vault=True)
s3.fail = "AccessDenied"
res = event()
row = rows()[0]
check(res == "failed" and row["vault"] == "failed", "an S3 failure gives vault=failed")
check(not row.get("s3_key"), "and an empty s3_key")
check(sent_metrics().get("VaultWriteFailures") == 1, "and a VaultWriteFailures datapoint")
check(LOG.having("AUDIT_VAULT_WRITE_FAILED", "ERROR"), "logged AUDIT_VAULT_WRITE_FAILED")
reset(vault=True)
ddb.put_fail = True
res = event()
check(res == "stored", "a DynamoDB failure does not raise")
check(sent_metrics().get("IndexWriteFailures") == 1, "and emits IndexWriteFailures")
check(LOG.having("AUDIT_INDEX_WRITE_FAILED", "ERROR"), "logged AUDIT_INDEX_WRITE_FAILED")
reset(vault=False)
cw.fail = True
res = event()
check(res == "off", "a metric failure does not raise")
reset(vault=True)
_real_build = al.build_record


def _broken_build(*a, **k):
    raise KeyError("boom")


al.build_record = _broken_build  # an unexpected error inside write_event
res = event()
al.build_record = _real_build
check(res is None and LOG.having("AUDIT_WRITE_FAILED", "ERROR"), "an unexpected error returns None and logs AUDIT_WRITE_FAILED")

print("")
print("== lock headers and retain_until (finding 3) ==")
reset(vault=True, extend=True)
ddb.config["retention_days"] = 400
event()
put = calls("s3.put_object")[0][1]
check(put.get("ObjectLockMode") == "COMPLIANCE", "lock headers sent with AUDIT_EXTEND_RETENTION and effective > floor")
want = T0 + timedelta(days=400) - timedelta(hours=1)
check(put.get("ObjectLockRetainUntilDate") == want, "retain-until is now + effective - 1 h")
check(rows()[0]["retain_until"] == want.isoformat(timespec="microseconds"),
      "stored row's retain_until is the ObjectLockRetainUntilDate sent")
reset(vault=True, extend=True)
ddb.config["retention_days"] = 365
event()
put = calls("s3.put_object")[0][1]
check("ObjectLockMode" not in put and "ObjectLockRetainUntilDate" not in put, "no lock headers at the floor")
check(rows()[0]["retain_until"] == (T0 + timedelta(days=365)).isoformat(timespec="microseconds"),
      "stored row without lock headers has retain_until = now + floor days")
reset(vault=True, extend=False)
ddb.config["retention_days"] = 3000
event()
put = calls("s3.put_object")[0][1]
check("ObjectLockMode" not in put, "without AUDIT_EXTEND_RETENTION (runtime) lock headers are never sent")
check(rows()[0]["retain_until"] == (T0 + timedelta(days=365)).isoformat(timespec="microseconds"),
      "and retain_until is still now + floor")
reset(vault=False)
event()
check("retain_until" not in rows()[0], "an index-only row carries no retain_until")

print("")
print("== retain-until margin ==")
reset(vault=True, extend=True)
ddb.config["retention_days"] = 3653
event()
lock = calls("s3.put_object")[0][1]["ObjectLockRetainUntilDate"]
check(lock == T0 + timedelta(days=3653) - timedelta(hours=1), "3653 days: exactly now + 3653 d - 1 h")
check(lock < T0 + timedelta(days=3653), "strictly below now + 3653 days")
reset(vault=True, extend=True)
ddb.config["retention_days"] = 366
event()
lock = calls("s3.put_object")[0][1]["ObjectLockRetainUntilDate"]
check(lock >= T0 + timedelta(days=365), "floor + 1: at least now + floor days")

print("")
print("== vault off, force_vault ==")
reset(vault=False)
res = event()
check(res == "off" and not calls("s3."), "vault off: no S3 call, returns 'off'")
check(rows()[0]["vault"] == "off" and not rows()[0].get("s3_key"), "row vault=off")
m = sent_metrics()
check(m.get("VaultOff") == 1 and "VaultWriteFailures" not in m, "a VaultOff datapoint and no VaultWriteFailures")
check("sha256" in rows()[0], "sha256 is present even when the vault is off")
reset(vault=False)
res = event("audit_vault_set", "audit_settings", "account", {"vault_enabled": True},
            {"vault_enabled": False}, force_vault=True)
check(res == "stored" and len(calls("s3.put_object")) == 1, "force_vault=True writes S3 while off")

print("")
print("== misconfiguration (vault on, AUDIT_BUCKET empty) ==")
reset(vault=True, bucket="")
r1, r2 = event(), event()
check(r1 == "failed" and r2 == "failed" and not calls("s3."), "vault=failed and no S3 call")
check(len(LOG.having("AUDIT_VAULT_MISCONFIGURED", "ERROR")) == 1, "AUDIT_VAULT_MISCONFIGURED logged once per process")
check(sent_metrics().get("VaultWriteFailures") == 2, "each event counts a VaultWriteFailures")

print("")
print("== settings: per-field resolution, cache, fallback ==")
reset(default="on", floor=365)
s = al.current_settings()
check(s == {"vault_enabled": True, "retention_days": 365, "effective_retention": 365, "floor": 365},
      "absent item gives the env defaults")
reset(default="on")
ddb.config = {"config_key": "audit_settings", "retention_days": 500}
s = al.current_settings()
check(s["vault_enabled"] is True and s["retention_days"] == 500, "retention-only item: vault from AUDIT_VAULT_DEFAULT")
reset(default="on")
ddb.config = {"config_key": "audit_settings", "vault_enabled": False}
s = al.current_settings()
check(s["vault_enabled"] is False and s["retention_days"] == 365, "vault-only item: retention = floor")
reset(floor=730)
ddb.config = {"config_key": "audit_settings", "retention_days": 400}
s = al.current_settings()
check(s["effective_retention"] == 730 and s["retention_days"] == 400, "effective = max(item, raised floor)")
reset(floor=365)
ddb.config = {"config_key": "audit_settings", "retention_days": 800}
check(al.current_settings()["effective_retention"] == 800, "effective = item when above the floor")
reset()
ddb.config = {"config_key": "audit_settings", "vault_enabled": True}
al.current_settings()
ddb.config["vault_enabled"] = False
clock["mono"] += 29
check(al.current_settings()["vault_enabled"] is True, "cached for 30 s")
check(len(calls("ddb.get_item")) == 1, "one GetItem inside the window")
al.invalidate_settings()
check(al.current_settings()["vault_enabled"] is False, "invalidate_settings() forces a re-read")
clock["mono"] += 31
ddb.config_fail = True
check(al.current_settings()["vault_enabled"] is False, "a read error falls back to the last cached value")
clock["mono"] += 31
al.current_settings()
check(len(LOG.having("audit settings read failed", "WARNING")) == 1, "WARNING at most once per 5 min")
clock["mono"] += 301
al.current_settings()
check(len(LOG.having("audit settings read failed", "WARNING")) == 2, "and again after 5 min")
reset(default="off")
ddb.config_fail = True
check(al.current_settings()["vault_enabled"] is False, "a read error with no cache gives the defaults")

print("")
print("== GovernanceRegistry settings accessors ==")


class FakeConfigTable:
    def __init__(self):
        self.items, self.updates = {}, []

    def get_item(self, Key):
        it = self.items.get(Key["config_key"])
        return {"Item": dict(it)} if it else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ReturnValues=None):
        self.updates.append(UpdateExpression)
        item = self.items.setdefault(Key["config_key"], {"config_key": Key["config_key"]})
        assert UpdateExpression.startswith("SET ")
        for part in UpdateExpression[4:].split(","):
            name, ref = [x.strip() for x in part.split("=")]
            item[name] = ExpressionAttributeValues[ref]
        return {"Attributes": dict(item)}


reg = gs.GovernanceRegistry()
reg._config = FakeConfigTable()
check(reg.get_audit_settings() is None, "get_audit_settings() is None while the item is absent")
reg._config.items["audit_settings"] = {"config_key": "audit_settings", "vault_enabled": True}
reg.put_audit_settings({"retention_days": 400}, "super1")
check(len(reg._config.updates) == 1, "put_audit_settings issues one UpdateItem")
expr = reg._config.updates[0]
fields = sorted(p.split("=")[0].strip() for p in expr[4:].split(","))
check(fields == ["retention_days", "updated_at", "updated_by"], f"it SETs only retention_days/updated_by/updated_at ({expr})")
item = reg.get_audit_settings()
check(item["vault_enabled"] is True and item["retention_days"] == 400, "vault_enabled survives a retention-only save")
check(item["updated_by"] == "super1", "updated_by recorded")
for bad in ({}, {"vault_enabled": True, "other": 1}, {"floor": 1}):
    try:
        reg.put_audit_settings(bad, "super1")
        check(False, f"{bad!r} raises ValueError")
    except ValueError:
        check(True, f"{bad!r} raises ValueError")


def _boom(**kw):
    raise cerr("InternalServerError")


reg._config.update_item = _boom
try:
    reg.put_audit_settings({"vault_enabled": False}, "super1")
    check(False, "a DynamoDB error raises from put_audit_settings")
except ClientError:
    check(True, "a DynamoDB error raises from put_audit_settings")

print("")
print("== GovernanceRegistry.audit delegates (positional signature unchanged) ==")
reset(vault=True)
r = gs.GovernanceRegistry().audit("admin1", "space_user_assign", "op1", "space:sA", None, {"auto_created": True})
check(r == "stored" and len(rows()) == 1, "audit(actor, action, target, scope, before, after) writes and returns the vault state")
reset(vault=False)
r = gs.governance_registry.audit("admin1", "database_rename", "db1", "account", {}, {},
                                 space_ids=["sA", "sB"], target_label="Orders")
check(r == "off" and len(rows()) == 3, "kwargs pass through (fan-out to two spaces)")
check(rows()[0]["target_label"] == "Orders", "target_label passes through")

print("")
print("== budget: 8 s per event, no request deadline ==")
reset(vault=True)
ddb.put_advance = 3.0
res = event("database_rename", "db1", "space:sA", {}, {"display_name": "x"},
            space_ids=["sA", "sB", "sC", "sD"])
ents = [r["entity"] for r in rows()]
check(res == "stored" and ents[0] == "space:sA", "the primary row is always written")
check(ents == ["space:sA", "space:sB", "space:sC"], "fan-out rows after 8 s are skipped")
check(LOG.having("AUDIT_INDEX_BUDGET_EXCEEDED", "ERROR") and "skipped=1" in LOG.having("AUDIT_INDEX_BUDGET_EXCEEDED")[0],
      "skipped rows are counted and logged")
check(not cw.calls and LOG.having("AUDIT_METRIC_DEFERRED", "ERROR"), "past the budget the metric is deferred")
ddb.put_advance = 0.0
event()
check(len(cw.calls) == 1 and cw.calls[0][1].get("IndexWriteFailures") == 1,
      "the next event sends the deferred count in its single put_metric_data")

print("")
print("== request deadline (20 s) ==")
reset(vault=True)
tok = al.AUDIT_CONTEXT.set(ctx(deadline=clock["mono"] + 20.0))
event()
event()
CALLS.clear()
clock["mono"] += 21
res = event(after={"auto_created": True})
dropped = LOG.having("AUDIT_RECORD_DROPPED", "ERROR")
check(res == "failed", "an event past the request deadline returns 'failed'")
check(not CALLS, "and makes no S3, DynamoDB or CloudWatch call")
check(len(dropped) == 1, "one AUDIT_RECORD_DROPPED line")
rec = json.loads(dropped[0].split(" ", 1)[1])
check(rec["action"] == "space_user_assign" and rec["after"] == {"auto_created": True} and len(rec["event_id"]) == 32,
      "containing the full JSON record")
al.AUDIT_CONTEXT.reset(tok)
event()
check(len(cw.calls) == 1, "the next event with budget makes one put_metric_data")
check(cw.calls[0][1].get("VaultWriteFailures") == 1 and cw.calls[0][1].get("IndexWriteFailures") == 1,
      "carrying the deferred VaultWriteFailures / IndexWriteFailures")

reset(vault=True)
tok = al.AUDIT_CONTEXT.set(ctx(deadline=clock["mono"] + 20.0))
clock["mono"] += 15
s3.advance = 6.0
res = event()
al.AUDIT_CONTEXT.reset(tok)
over = LOG.having("AUDIT_INDEX_BUDGET_EXCEEDED", "ERROR")
check(res == "stored" and not rows(), "an S3 PUT ending past the deadline skips the primary row")
check(over and calls("s3.put_object")[0][1]["Key"] in over[0], "AUDIT_INDEX_BUDGET_EXCEEDED names the s3_key")
check(LOG.having("AUDIT_RECORD_DROPPED", "ERROR"), "and AUDIT_RECORD_DROPPED is logged as well")

print("")
print("== detached_context ==")
reset(vault=True)
tok = al.AUDIT_CONTEXT.set(ctx(groups=("admins",), deadline=clock["mono"] + 20.0))
dctx = al.detached_context()
al.AUDIT_CONTEXT.reset(tok)
seen = dctx.run(al.AUDIT_CONTEXT.get)
check(all(k in seen for k in ("sub", "email", "groups", "request_id", "source_ip")), "keeps sub/email/groups/request_id/source_ip")
check("deadline" not in seen, "drops deadline")
clock["mono"] += 60
res = dctx.run(event)
check(res == "stored" and len(rows()) == 1 and rows()[0]["actor_role"] == "super_admin",
      "an event inside it past the request deadline is written normally")
check(rows()[0]["request_id"] == "req-1", "with the caller's request facts")

print("")
print("== submit_in_context ==")
reset()
tok = al.AUDIT_CONTEXT.set(ctx(sub="subX"))
with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
    got = al.submit_in_context(pool, lambda: al.AUDIT_CONTEXT.get()).result()
    bare = pool.submit(lambda: al.AUDIT_CONTEXT.get()).result()
    futs = [al.submit_in_context(pool, lambda: al.AUDIT_CONTEXT.get()["sub"]) for _ in range(5)]
    try:
        many = [f.result() for f in futs]
        err = None
    except RuntimeError as e:
        many, err = [], e
al.AUDIT_CONTEXT.reset(tok)
check(got.get("sub") == "subX", "a worker submitted through it sees the submitter's context")
check(dict(bare) == {}, "a bare pool.submit sees {}")
check(err is None and many == ["subX"] * 5, "five concurrent submits from one thread do not raise")

print("")
print("== keys, months and fan-out ==")
reset(vault=True)
tok = al.AUDIT_CONTEXT.set(ctx(sub="super1", groups=("admins",)))
event("database_decommission", "db1", "space:sA", {"member_space_ids": ["sA", "sB", "sC"]}, None,
      actor="super1", space_ids=["sA", "sB", "sC"])
al.AUDIT_CONTEXT.reset(tok)
key = calls("s3.put_object")[0][1]["Key"]
check(re.match(r"^audit/\d{4}/\d{2}/\d{2}/\d{8}T\d{12}Z-[0-9a-f]{32}\.json$", key) is not None, f"key format ({key})")
check(key.startswith("audit/2025/03/10/20250310T120000123456Z-"), "key embeds the UTC date and time")
prim = rows()[0]
check(al.derive_key(prim["ts"], prim["event_id"]) == key, "derive_key(ts, event_id) round-trips to the written key")
check(prim.get("month") == "2025-03" and all("month" not in r for r in rows()[1:]), "month only on the primary row")
fan = {r["entity"]: r for r in rows()[1:]}
check(set(fan) == {"space:sB", "space:sC"}, "fan-out rows for the other spaces only")
b = fan["space:sB"]
check(b["fanout"] is True and b["space_id"] == "sB" and b["space_name"] == "Bravo" and b["owner_admin"] == "ownerB",
      "a fan-out row carries its own space_id/space_name/owner_admin")
check("Alpha" not in json.dumps(b["space_name"]) and b["owner_admin"] != "ownerA",
      "B's row contains no A name or owner")
check(b["on_other_owner"] is True and fan["space:sC"]["on_other_owner"] is False,
      "on_other_owner is recomputed per space (sC is owned by the actor)")
check(prim["space_name"] == "Alpha" and prim["owner_admin"] == "ownerA" and prim["on_other_owner"] is True,
      "the primary row has the primary space's fields")
check(all(r["ts"] == prim["ts"] for r in rows()), "fan-out rows share the primary ts")
reset(vault=False)
event("space_user_assign", "op1", "space:sA")
check(len(rows()) == 1, "a bare scope never fans out")

print("")
print("== sanitizer ==")
from decimal import Decimal  # noqa: E402
s = al.sanitize({"password": "x", "db_secret_value": "y", "SecretString": "z", "api_token": "t",
                 "private_key": "k", "aws_credentials": "c", "secret_arn": "arn:aws:secretsmanager:x",
                 "long": "a" * 1500, "set": {"b", "a"}, "n": Decimal("3"), "f": Decimal("1.5"),
                 "nested": {"Password": "p", "ok": 1}})
check(set(s) == {"secret_arn", "long", "set", "n", "f", "nested"}, "secret-named keys are dropped, ARNs kept")
check(len(s["long"]) == 1000, "strings over 1000 chars are truncated")
check(s["set"] == ["a", "b"] and s["n"] == 3 and isinstance(s["n"], int) and s["f"] == 1.5, "set->sorted list, Decimal->int/float")
check(s["nested"] == {"ok": 1}, "nested secret keys are dropped too")
reset(vault=True)
event(after={"blob": ["x" * 999] * 40})
check(rows()[0]["after"] == {"_truncated": True}, "an index diff over 32 KB becomes {'_truncated': true}")
check(json.loads(calls("s3.put_object")[0][1]["Body"])["after"]["blob"][0] == "x" * 999, "the vault keeps the full value")
reset(vault=True)
event(after={"blob": ["x" * 999] * 300})
check(json.loads(calls("s3.put_object")[0][1]["Body"])["after"] == {"_truncated": True}, "over 256 KB the vault body is truncated")

print("")
print("== actor_role and on_other_owner truth tables ==")
for actor, groups, action, want in (
        ("system", ("admins",), "space_db_onboard", "system"),
        ("cloudformation", (), "email_sender_set", "system"),
        ("unknown", ("admins",), "space_update", "system"),
        ("admin1", ("space-admins",), "query_plan_run", "operator"),
        ("admin1", ("admins", "space-admins"), "space_update", "super_admin"),
        ("admin1", ("space-admins",), "space_update", "space_admin"),
        ("admin1", (), "space_update", "")):
    rec = al.build_record(actor, action, "t", "account", None, None, ctx=ctx(sub=actor, groups=groups))
    check(rec["actor_role"] == want, f"{actor} {list(groups)} {action} -> {want!r}")
for role_groups, owner, actor, want in (
        (("admins",), "ownerA", "super1", True),
        (("admins",), "super1", "super1", False),
        (("admins",), "", "super1", False),
        (("space-admins",), "ownerA", "admin1", False)):
    rec = al.build_record(actor, "space_update", "sA", "space:sA", None, None,
                          owner_admin=owner, ctx=ctx(sub=actor, groups=role_groups))
    check(rec["on_other_owner"] is want, f"on_other_owner role={role_groups} owner={owner!r} actor={actor} -> {want}")

print("")
print("== timestamps ==")
rec = al.build_record("a", "space_update", "t", "account", ctx={},
                      now=datetime(2025, 1, 2, 3, 4, 5, 0, tzinfo=timezone.utc))
check(rec["ts"] == "2025-01-02T03:04:05.000000+00:00", "ts has 6 fraction digits when microsecond == 0")

print("")
print("== agent events are index-only (finding 2) ==")
reset(vault=True)
tok = al.AUDIT_CONTEXT.set(ctx(groups=("space-admins",)))
res = al.write_event("op9", "query_plan_run", "orders", "account", None,
                     {"database": "orders", "query": "select 1"}, space_id="sA")
al.AUDIT_CONTEXT.reset(tok)
check(not calls("s3."), "with the vault on, query_plan_run makes no S3 call")
check(not any(c[0] == "ddb.get_item" for c in CALLS), "and reads no vault settings")
m = sent_metrics()
check("VaultOff" not in m and "VaultWriteFailures" not in m, "and emits no VaultOff / VaultWriteFailures")
check(len(rows()) == 1, "exactly one row")
r = rows()[0]
check(r["entity"] == "agent" and "month" not in r and r["vault"] == "off" and res == "off",
      "entity=agent, no month, vault=off")
check(r["category"] == "agent" and r["actor_role"] == "operator" and r["space_id"] == "sA",
      "category agent, actor_role operator even for a space-admin bearer, space_id kept")
check(not r.get("fanout") and r["space_name"] == "", "no fan-out and no space lookup")
reset(vault=True)
ddb.put_fail = True
al.write_event("op9", "query_plan_refused", "orders", "account", None, {}, space_id="sA")
check(sent_metrics() == {"IndexWriteFailures": 1}, "only IndexWriteFailures can be emitted for an agent event")
reset(vault=False)
al.write_event("op9", "query_plan_run", "orders", "account", None, {}, space_id="sA", space_ids=["sA", "sB"],
               force_vault=True)
check(len(rows()) == 1 and not calls("s3.") and not cw.calls, "force_vault/space_ids never vault or fan out an agent event")

print("")
print("== actor_email resolution (A2) ==")
rec = al.build_record("admin1", "space_update", "t", "account", ctx=ctx(email="a@x.io"))
check(rec["actor_email"] == "a@x.io", "token email first")
rec = al.build_record("admin1", "space_update", "t", "account",
                      ctx=ctx(email="generated-id", resolve_email=lambda s: "r@x.io"))
check(rec["actor_email"] == "r@x.io", "then the resolver when the email has no @")


def _raise(_s):
    raise RuntimeError("cognito down")


rec = al.build_record("admin1", "space_update", "t", "account", ctx=ctx(email="", resolve_email=_raise))
check(rec["actor_email"] == "", "a resolver that raises gives ''")
rec = al.build_record("system", "space_db_onboard", "t", "account", ctx=ctx(email="a@x.io"))
check(rec["actor_email"] == "", "a 'system' actor inside a request gets no email")
rec = al.build_record("other", "space_update", "t", "account", ctx=ctx(email="a@x.io"))
check(rec["actor_email"] == "", "an actor other than the bearer gets no email")

print("")
print("== projections ==")
v2 = {"v": 2, "entity": "space:sA", "ts": "2025-03-10T12:00:00.000000+00:00#abcd1234", "event_id": "e" * 32,
      "actor_sub": "admin1", "actor_email": "a@x.io", "actor_role": "space_admin", "action": "space_db_onboard",
      "category": "admin", "target_type": "database", "target_id": "db1", "target_label": "Orders",
      "space_id": "sA", "space_name": "Old name", "space_ids": ["sA", "sB"], "owner_admin": "ownerA",
      "on_other_owner": False, "request_id": "r", "source_ip": "1.2.3.4", "scope": "space:sA",
      "s3_key": "audit/k", "s3_version_id": "v1", "sha256": "ff", "vault": "stored", "search": "x",
      "before": {"member_space_ids": ["sA", "sB"], "secret_arn": "arn"},
      "after": {"mode": "first_onboard", "result": "ok", "batch": False, "member_space_ids": ["sA"], "extra": 1}}
p = al.project_for_owner(v2, "sA")
for f in ("request_id", "source_ip", "s3_key", "s3_version_id", "sha256", "space_ids", "owner_admin", "scope", "search"):
    check(f not in p, f"owner projection drops {f}")
check(p["before"] == {"member_count": 2} and p["after"] == {"mode": "first_onboard", "result": "ok",
                                                            "batch": False, "member_count": 1},
      "per-action diff allowlist; member_space_ids -> member_count")
check(p["space_name"] == "Alpha" and p["space_id"] == "sA", "space_id/space_name come from the path space")
check(p["has_vault_copy"] is True and "actor_sub" not in p, "has_vault_copy; actor_sub only when email empty")
check(al.project_for_owner(dict(v2, space_id="sZ"), "sA") is None, "a row of another space is dropped")
check(al.project_for_owner(dict(v2, space_id="", fanout=False), "sA", partition_ok=True) is not None,
      "partition_ok=True keeps a record whose space_id is ''")
check(al.project_for_owner(dict(v2, space_id="sA", fanout=True, entity="space:sA"), "sA")["fanout"] is True,
      "a fan-out row is kept")
check(al.project_for_owner(dict(v2, action="mystery"), "sA")["before"] == {}, "an unknown action shows no diff")
sp = al.project_for_super_admin(v2)
check("search" not in sp and sp["entity"] == "space:sA" and sp["ts"].endswith("#abcd1234"),
      "super-admin projection: everything but search, plus entity/ts")
check(sp["action_label"] == "Onboarded database" and sp["has_vault_copy"] is True and sp["s3_key"] == "audit/k",
      "with action_label and has_vault_copy")
v1 = {"entity": "space:sA", "ts": "2024-05-01T10:00:00+00:00#1234abcd", "actor": "old-admin",
      "action": "space_user_assign", "target": "op7", "scope": "space:sA", "before": {}, "after": {"auto_created": True}}
p = al.project_for_owner(v1, "sA")
check(p["actor_sub"] == "old-admin" and p["target_id"] == "op7" and p["target_label"] == "op7",
      "v1 owner projection maps actor/target")
check(p["vault"] == "legacy" and p["space_id"] == "sA" and p["after"] == {"auto_created": True}, "v1 row is legacy, space from path")
sp = al.project_for_super_admin(v1)
check(sp["actor_sub"] == "old-admin" and sp["target_id"] == "op7" and sp["vault"] == "legacy"
      and sp["entity"] == "space:sA" and sp["ts"] == v1["ts"] and sp["space_id"] == "sA",
      "v1 super-admin projection maps the same and includes entity/ts")
tok = al.AUDIT_CONTEXT.set({"resolve_email": lambda s: "old@x.io"})
check(al.project_for_owner(v1, "sA")["actor_email"] == "old@x.io", "an empty email is filled through resolve_email (display only)")
al.AUDIT_CONTEXT.reset(tok)
check("actor_email" not in v1, "and never written back")

print("")
print("== cursor ==")
c = al.encode_cursor("2025-03", {"entity": "account", "ts": "t#1", "month": "2025-03"})
check(al.decode_cursor(c, gsi=True) == ("2025-03", {"entity": "account", "ts": "t#1", "month": "2025-03"}),
      "round-trip")
check(al.decode_cursor(al.encode_cursor(None, None)) == (None, None), "null m/k round-trip")


def rejects(cur, gsi=False):
    try:
        al.decode_cursor(cur, gsi=gsi)
        return False
    except ValueError:
        return True


def b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


check(rejects(c[:-3] + "!!!"), "a tampered cursor is rejected")
check(rejects(b64({"m": None, "k": {"entity": "a", "ts": "b", "x": "c"}})), "k with extra keys is rejected")
check(rejects(b64({"m": "2025-3", "k": None})), "a bad month is rejected")
check(rejects(b64({"m": None, "k": None, "z": 1})), "an extra top-level key is rejected")
check(rejects(b64([1, 2])) and rejects("x" * 3000) and rejects(""), "non-objects, over-long and empty cursors are rejected")
check(rejects(b64({"m": None, "k": {"entity": "a", "ts": "b"}}), gsi=True), "a GSI cursor needs k.month")
reg = gs.GovernanceRegistry()
try:
    reg.query_audit("space:sA", cursor=b64({"m": None, "k": {"entity": "space:sB", "ts": "x"}}))
    check(False, "a cursor from another partition is rejected")
except ValueError:
    check(True, "a cursor from another partition is rejected")

print("")
print("== query_audit: truncation and paging ==")
reset()


def seed(entity, ts, month=None, **extra):
    r = {"entity": entity, "ts": ts, "v": 2, "category": "admin", "action": "space_update",
         "actor_sub": "admin1", "search": "admin1 changed space details"}
    if month:
        r["month"] = month
    r.update(extra)
    ddb.tables.setdefault(AUDIT, []).append(r)
    return r


for i in range(100):
    seed("space:sA", f"2025-03-01T00:00:{i:02d}.000000+00:00#{i:08x}")
seen, cur, pages = [], None, 0
while True:
    page = reg.query_audit("space:sA", limit=30, cursor=cur)
    seen.extend(r["ts"] for r in page["rows"])
    pages += 1
    if not page["has_more"]:
        break
    cur = page["next_before"]
check(len(seen) == 100 and len(set(seen)) == 100, "100 matching rows, limit=30: all 100 exactly once")
check(seen == sorted(seen, reverse=True), "newest first")
check(ddb.queries[0]["Limit"] == 100 and ddb.queries[0]["ScanIndexForward"] is False, "Query(Limit=100) newest-first")
m0, k0 = al.decode_cursor(reg.query_audit("space:sA", limit=30)["next_before"])
check(m0 is None and k0["ts"] == seen[29], "a mid-page truncation resumes at the last returned row, not the LEK")
check("IndexName" not in ddb.queries[0], "the space partition is a base-table query")

print("")
print("== query_audit: month walk ==")
reset()
clock["now"] = datetime(2025, 6, 15, tzinfo=timezone.utc)
seed("account", "2025-03-05T00:00:00.000000+00:00#00000001", "2025-03")
seed("account", "2025-02-05T00:00:00.000000+00:00#00000002", "2025-02")
res = reg.query_audit(None, index="month-index", to_ts="2025-03-10")
months = [q["ExpressionAttributeValues"][":m"]["S"] for q in ddb.queries]
check(months[0] == "2025-03", "to=2025-03-10: the first Query is on month 2025-03")
check([r["ts"][:7] for r in res["rows"]] == ["2025-03", "2025-02"], "the walk moves to the previous month")
check(all(q["IndexName"] == "month-index" for q in ddb.queries), "the walk uses month-index")
ddb.queries.clear()
reg.query_audit(None, index="month-index", to_ts="2030-01-01", limit=1)
check(ddb.queries[0]["ExpressionAttributeValues"][":m"]["S"] == "2025-06", "a future to starts at the current month")
ddb.queries.clear()
reg.query_audit(None, index="month-index", to_ts="2025-03-10", cursor=al.encode_cursor("2024-12", None), limit=1)
check(ddb.queries[0]["ExpressionAttributeValues"][":m"]["S"] == "2024-12", "a cursor m starts there regardless of to")
ddb.queries.clear()
res = reg.query_audit(None, index="month-index", limit=1)
check(len(res["rows"]) == 1 and res["has_more"], "limit reached at a month end: has_more")
mm, kk = al.decode_cursor(res["next_before"], gsi=True)
check(kk is None and mm == "2025-02", "{'m': prev, 'k': null} resumes at the start of the previous month")
res2 = reg.query_audit(None, index="month-index", cursor=res["next_before"], limit=5)
check([r["ts"][:7] for r in res2["rows"]] == ["2025-02"], "and finds that month's rows")
ddb.queries.clear()
res = reg.query_audit(None, index="month-index", from_ts="2025-01-01")
check(ddb.queries[-1]["ExpressionAttributeValues"][":m"]["S"] == "2025-01" and not res["has_more"],
      "the walk ends at month(from)")
ddb.queries.clear()
res = reg.query_audit(None, index="month-index")
check(not res["has_more"] and len(ddb.queries) > 10, "empty months (ScannedCount 0) do not count towards the 10-page rule")
check(ddb.queries[-1]["ExpressionAttributeValues"][":m"]["S"] == al.month_of(clock["now"] - timedelta(days=3653)),
      "with no from the walk ends at month(now - 3653 days)")

print("")
print("== query_audit: date bounds ==")
reset()
seed("space:sA", "2025-03-11T00:00:00.000000+00:00#00000001")
seed("space:sA", "2025-03-10T23:59:59.999999+00:00#00000002")
seed("space:sA", "2025-03-10T10:00:00.500000+00:00#00000003")
seed("space:sA", "2025-03-10T10:00:01.000000+00:00#00000004")
got = [r["ts"][-1] for r in reg.query_audit("space:sA", from_ts="2025-03-10", to_ts="2025-03-10")["rows"]]
check("1" not in got and "2" in got, "date to: to+1 day T00:00:00.000000 excluded, T23:59:59.999999 included")
got = [r["ts"][-1] for r in reg.query_audit("space:sA", to_ts="2025-03-10T10:00:00Z")["rows"]]
check("3" in got and "4" not in got, "datetime to: a row at to + 0.5 s is included, to + 1 s is not")
for bad in (("2025-13-01", None), ("x", None), ("2025-03-11", "2025-03-10")):
    try:
        reg.query_audit("space:sA", from_ts=bad[0], to_ts=bad[1])
        check(False, f"bad bounds {bad} raise ValueError")
    except ValueError:
        check(True, f"bad bounds {bad} raise ValueError")

print("")
print("== query_audit: filters, legacy agent rows, account view (finding 4) ==")
reset()
ddb.tables[AUDIT] = [
    {"entity": "account", "ts": "2025-03-01T00:00:01+00:00#00000001", "actor": "op1",
     "action": "query_plan_run", "target": "db", "scope": "account"},
    {"entity": "account", "ts": "2025-03-01T00:00:02+00:00#00000002", "actor": "adm",
     "action": "space_user_assign", "target": "op", "scope": "account"}]
got = [r["action"] for r in reg.query_audit("account")["rows"]]
check(got == ["space_user_assign"], "the FilterExpression drops a v1 query_plan_run row and keeps a v1 admin row")
fe = ddb.queries[0]["FilterExpression"]
check("attribute_not_exists(#category)" in fe and ":qpr" in fe and ":qpf" in fe, "the admin-category clause is always present")
reset()
seed("account", "2025-03-03T00:00:00.000000+00:00#00000001", "2025-03", actor_sub="subA", search="a@x.io subA")
seed("account", "2025-03-02T00:00:00.000000+00:00#00000002", "2025-03", actor_sub="subB", action="user_invite",
     search="b@x.io subB invited user")
check([r["actor_sub"] for r in reg.query_audit("account", filters={"actor": "subA"})["rows"]] == ["subA"], "actor sub filter")
check([r["actor_sub"] for r in reg.query_audit("account", filters={"actor": "B@X.io"})["rows"]] == ["subB"],
      "actor email filter matches search case-insensitively")
check([r["actor_sub"] for r in reg.query_audit("account", filters={"action": "user_invite"})["rows"]] == ["subB"], "action filter")
check([r["actor_sub"] for r in reg.query_audit("account", filters={"q": "INVITED"})["rows"]] == ["subB"], "q filter (lower-cased)")
reset()
clock["now"] = datetime(2025, 3, 20, tzinfo=timezone.utc)
seed("admin:sub-123", "2025-03-03T00:00:00.000000+00:00#00000001", "2025-03", action="admin_governance_update")
seed("account", "2025-03-02T00:00:00.000000+00:00#00000002", "2025-03", action="rate_limit_set")
seed("space:sA", "2025-03-01T00:00:00.000000+00:00#00000003", "2025-03", action="space_update")
res = reg.query_audit("account", exclude_space_partitions=True)
ents = [r["entity"] for r in res["rows"]]
check(ents == ["admin:sub-123", "account"], "the account view's month walk includes admin:<sub> and excludes space: rows")
check(ddb.queries[0].get("IndexName") == "month-index" and "NOT begins_with(#entity, :space)" in ddb.queries[0]["FilterExpression"],
      "it runs on month-index with NOT begins_with(entity, 'space:')")

print("")
print("== query_audit: stop rules (finding 7) ==")
reset()
for i in range(1200):
    seed("space:sA", f"2025-03-01T{i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}.000000+00:00#{i:08x}")
res = reg.query_audit("space:sA", filters={"q": "no-such-text"})
check(len(ddb.queries) == 10 and res["has_more"], "10 Query responses with ScannedCount > 0 stop the read")
check(res["next_before"] and res["scanned_to"], "with a resume cursor and scanned_to")
_, kk = al.decode_cursor(res["next_before"])
check(kk["ts"] == res["scanned_to"], "the cursor resumes where the scan stopped")
reset()
clock["now"] = datetime(2025, 6, 15, tzinfo=timezone.utc)
ddb.query_advance = 3.0
res = reg.query_audit(None, index="month-index")
check(len(ddb.queries) == 4, "a 10 s wall-clock budget stops the walk (4 calls at 3 s each)")
check(res["has_more"] and res["next_before"] and res["scanned_to"] == "2025-03", "returning has_more, a cursor and scanned_to")
mm, kk = al.decode_cursor(res["next_before"], gsi=True)
check(mm == "2025-02" and kk is None, "the cursor resumes at the next month")

print("")
print("== verify_row ==")


def stored_event(**kw):
    reset(vault=True)
    al.write_event(kw.pop("actor", "admin1"), kw.pop("action", "space_user_assign"), kw.pop("target", "op1"),
                   kw.pop("scope", "space:sA"), None, {"auto_created": True}, **kw)
    return rows()[0]


r = stored_event()
v = al.verify_row(r["entity"], r["ts"])
check(v["status"] == "verified" and "reasons" not in v, "an untouched event is verified")
check(v["s3_key"] == r["s3_key"] and v["record"]["event_id"] == r["event_id"] and v["lock_mode"] == "COMPLIANCE",
      "with s3_key, the record and lock_mode")
gets = calls("s3.get_object")
check(gets[0][1]["VersionId"] == "v1" and gets[0][1]["ChecksumMode"] == "ENABLED", "GetObject by version, ChecksumMode=ENABLED")


def mismatch(mutate, reason):
    r = stored_event()
    mutate(r)
    v = al.verify_row(r["entity"], r["ts"])
    check(v["status"] == "mismatch" and reason in v.get("reasons", []), f"mismatch: {reason}")
    check(LOG.having("AUDIT_INDEX_MISMATCH", "ERROR"), f"  {reason} logged AUDIT_INDEX_MISMATCH")


mismatch(lambda r: r.update(sha256="0" * 64), "hash")
mismatch(lambda r: s3.objects[r["s3_key"]][0].update(ChecksumSHA256="AAAA"), "s3_checksum")
mismatch(lambda r: s3.objects[r["s3_key"]][0].update(LastModified=T0 + timedelta(seconds=301)), "timestamp")
mismatch(lambda r: s3.objects[r["s3_key"]].append(dict(s3.objects[r["s3_key"]][0], VersionId="v9")), "single_version")
mismatch(lambda r: r.update(actor_sub="someone-else"), "fields")
mismatch(lambda r: r.update(s3_key="audit/elsewhere.json"), "key_not_derived")


def hide(r):
    r.update(vault="off")
    r.pop("s3_key")
    r.pop("s3_version_id")


mismatch(hide, "index_hides_vault_copy")
r = stored_event()
s3.objects[r["s3_key"]] = []
check(al.verify_row(r["entity"], r["ts"])["status"] == "missing", "missing for NoSuchKey")
r = stored_event()
r["s3_version_id"] = "v404"
check(al.verify_row(r["entity"], r["ts"])["status"] == "missing", "missing for NoSuchVersion")
check(LOG.having("vault copy missing", "WARNING"), "missing is logged at WARNING")
r = stored_event()
s3.objects[r["s3_key"]] = []
clock["now"] = T0 + timedelta(days=366)
v = al.verify_row(r["entity"], r["ts"])
check(v["status"] == "expired" and v["retain_until"] == r["retain_until"], "absent object, retain_until past -> expired")
check(LOG.having("expired", "INFO") and not LOG.having("vault copy missing"), "expired is logged at INFO, not WARNING")
r = stored_event()
s3.objects[r["s3_key"]] = []
clock["now"] = T0 + timedelta(days=100)
check(al.verify_row(r["entity"], r["ts"])["status"] == "missing", "absent object, retain_until in the future -> missing")
r = stored_event()
s3.objects[r["s3_key"]] = []
r.pop("retain_until")
clock["now"] = T0 + timedelta(days=4000)
check(al.verify_row(r["entity"], r["ts"])["status"] == "missing", "absent object, no retain_until -> missing")
reset(vault=False)
event()
r = rows()[0]
v = al.verify_row(r["entity"], r["ts"])
check(v["status"] == "not_in_vault" and v["vault"] == "off", "not_in_vault for an off row with no object")
reset(vault=True)
s3.fail = "AccessDenied"
event()
s3.fail = None
r = rows()[0]
check(al.verify_row(r["entity"], r["ts"])["status"] == "not_in_vault", "not_in_vault for a failed row with no object")
reset()
ddb.tables[AUDIT] = [dict(v1)]
check(al.verify_row(v1["entity"], v1["ts"]) == {"status": "not_in_vault", "vault": "legacy", "row": v1},
      "a legacy row is not_in_vault (legacy)")
for exc, setup in ((al.AuditRecordNotFound, lambda: None), (al.AuditIndexReadError, lambda: setattr(ddb, "get_fail", True))):
    reset()
    setup()
    try:
        al.verify_row("account", "2025-03-10T12:00:00.000000+00:00#00000000")
        check(False, f"{exc.__name__} raised")
    except exc:
        check(True, f"{exc.__name__} raised")
r = stored_event()
s3.fail = "AccessDenied"
try:
    al.verify_row(r["entity"], r["ts"])
    check(False, "an S3 AccessDenied raises AuditVaultReadError")
except al.AuditVaultReadError:
    check(True, "an S3 AccessDenied raises AuditVaultReadError")

# fan-out rows through the owner path
reset(vault=True)
tok = al.AUDIT_CONTEXT.set(ctx(sub="super1", groups=("admins",)))
al.write_event("super1", "database_rename", "db1", "account", {"display_name": "a"}, {"display_name": "b"},
               space_ids=["sA", "sB"])
al.AUDIT_CONTEXT.reset(tok)
fb = [r for r in rows() if r["entity"] == "space:sB"][0]
v = al.verify_row("space:sB", fb["ts"])
check(v["status"] == "verified", "a fan-out row verifies")
po = al.project_for_owner(v["record"], "sB", partition_ok=True, space_name="Bravo")
check(po is not None and po["space_id"] == "sB" and po["after"] == {"display_name": "b"},
      "the record projects for the owner with partition_ok (its own space_id is '')")
fb["space_id"] = "sZ"
v = al.verify_row("space:sB", fb["ts"])
check(v["status"] == "mismatch" and v["reasons"] == ["fields"], "a fan-out row whose space is not in record.space_ids mismatches")

print("")
print("== drift guard: every literal .audit( / .write_event( action under backend/ is in ACTIONS ==")


def literal_actions(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return literal_actions(node.body) + literal_actions(node.orelse)
    return []


found, unknown = 0, []
for path in sorted((REPO / "backend").rglob("*.py")):
    rel = path.relative_to(REPO).as_posix()
    if "/tests/" in rel or "__pycache__" in rel:
        continue
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("audit", "write_event"):
            arg = n.args[1] if len(n.args) >= 2 else next((k.value for k in n.keywords if k.arg == "action"), None)
            for a in literal_actions(arg) if arg is not None else []:
                found += 1
                if a not in al.ACTIONS:
                    unknown.append(f"{rel}:{n.lineno} {a}")
check(found >= 37, f"found the literal audit actions ({found})")
check(not unknown, f"all are catalogued {unknown}")
t1_actions = set()
for n in ast.walk(ast.parse((REPO / "backend/tier1_collector.py").read_text(encoding="utf-8"))):
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "write_event":
        t1_actions.update(literal_actions(n.args[1]))
check(t1_actions == {"cluster_failover_detected"},
      f"the Tier 1 collector's write_event actions are seen by the guard ({sorted(t1_actions)})")
rr_actions = set()
for n in ast.walk(ast.parse((REPO / "backend/tools/aws/route_repair.py").read_text(encoding="utf-8"))):
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "audit":
        rr_actions.update(literal_actions(n.args[1]))
check(rr_actions == {"network_host_routes_repaired", "network_host_routes_repair_refused"},
      f"the API task's route repair audits its repairs and refusals ({sorted(rr_actions)})")
for code in ("cluster_failover_detected", "network_host_routes_repaired", "network_host_routes_repair_refused"):
    check(code in al.ACTIONS and al.ACTIONS[code][1] == "admin" and al.ACTIONS[code][2] == "database",
          f"{code} is catalogued as an admin database event")
check(all(len(v) == 4 and v[1] in ("admin", "agent") for v in al.ACTIONS.values()), "every catalog entry is (label, category, type, keys)")
check(al.ACTIONS["space_investigation_delete"] == ("Deleted investigation", "admin", "space", ()),
      "space_investigation_delete is catalogued for FEAT-003")
check(al.action_label("space_db_share") == "Space db share" and al.action_category("space_db_share") == "admin",
      "an unknown code renders as a capitalised label, category admin")

print("")
print("== packaging and call sites ==")
tree = ast.parse(src)
mods = set()
for n in ast.walk(tree):
    if isinstance(n, ast.Import):
        mods.update(a.name for a in n.names)
    elif isinstance(n, ast.ImportFrom):
        mods.add(n.module or "")
check(not any(x in m for m in mods for x in ("tier1_collector", "metrics_store", "db_tls")),
      "audit_log imports nothing from tier1_collector / metrics_store / db_tls")
check(mods <= {"base64", "contextvars", "hashlib", "json", "logging", "math", "os", "re", "threading", "time",
               "types", "uuid", "datetime", "decimal", "boto3", "botocore.config", "tools.resource_names",
               "tools.aws.spaces_client"}, f"stdlib + boto3/botocore + resource_names only ({sorted(mods)})")
top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
check(not any(getattr(n, "module", "") == "tools.aws.spaces_client" for n in top), "spaces_client is imported lazily")
for rel in ("backend/tools/aws/metrics_store.py", "backend/tools/aws/db_tls.py"):
    check("audit_log" not in (REPO / rel).read_text() and "governance_store" not in (REPO / rel).read_text(),
          f"{rel} does not import audit_log / governance_store")
# The Tier 1 collector writes system admin events (failover, host-route repair), so its
# zip ships audit_log and what audit_log imports.
tier1_build = (REPO / "backend/scripts/build-tier1.sh").read_text()
for f in ("audit_log.py", "spaces_client.py", "teardown_claim.py", "host_routes.py"):
    check(f"cp tools/aws/{f} " in tier1_build, f"build-tier1.sh ships tools/aws/{f}")
check("governance_store" not in (REPO / "backend/tier1_collector.py").read_text(),
      "the Tier 1 collector does not import governance_store")
dbt = ast.parse((REPO / "backend/tools/db_tools.py").read_text())
fn = next(n for n in ast.walk(dbt) if isinstance(n, ast.FunctionDef) and n.name == "_audit_query_plan")
call = next(n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "audit")
kw = {k.arg: k.value for k in call.keywords}
check("space_id" in kw and isinstance(kw["space_id"], ast.Call) and getattr(kw["space_id"].func, "id", "") == "_current_space_id",
      "db_tools._audit_query_plan passes space_id=_current_space_id()")
check(isinstance(call.args[3], ast.Constant) and call.args[3].value == "account", "and keeps scope 'account'")

print("")
print("== the email-sender seed function writes the same v2 event (storage.yaml) ==")
# The seed is inline CloudFormation code that cannot import audit_log, so it repeats the
# record shape, the serialisation, the vault key and the index row. Run it against fakes
# and compare with what audit_log itself would produce: a divergence would make verify
# flag the deploy-time record as a mismatch.
import subprocess  # noqa: E402
import yaml  # noqa: E402


class _CfnLoader(yaml.SafeLoader):
    pass


def _cfn_tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_CfnLoader.add_multi_constructor("!", _cfn_tag)
_storage = yaml.load((REPO / "infrastructure/storage.yaml").read_text(), Loader=_CfnLoader)  # nosec B506
_seed_props = _storage["Resources"]["EmailSenderSeedFunction"]["Properties"]
SEED_SRC = _seed_props["Code"]["ZipFile"]
_seed_env = _seed_props["Environment"]["Variables"]
check(_seed_env.get("AUDIT_BUCKET") == "AuditBucket" and _seed_env.get("AUDIT_VAULT_DEFAULT") == "AuditVault",
      "seed env: AUDIT_BUCKET = !Ref AuditBucket, AUDIT_VAULT_DEFAULT = !Ref AuditVault")
check(_seed_env.get("AUDIT_RETENTION_FLOOR") == "AuditRetentionDays",
      "seed env: AUDIT_RETENTION_FLOOR = !Ref AuditRetentionDays (finding 3)")

SEED_EVENT = {"RequestType": "Create", "StackId": "stack", "RequestId": "req",
              "LogicalResourceId": "EmailSenderSeed", "ResponseURL": "https://example.invalid",
              "ResourceProperties": {"Address": " Ops@Example.com ", "Name": "Ops", "Region": "eu-west-1"}}
_SEED_ENV_KEYS = ("CONFIG_TABLE", "AUDIT_TABLE", "AUDIT_BUCKET", "AUDIT_VAULT_DEFAULT", "AUDIT_RETENTION_FLOOR")
SEED_BUCKET = "acme-audit-123456789012-0123456789ab"


def run_seed(settings=None, vault_default="on", floor="365", s3_error=None, index_error=None):
    """Exec the inline seed with a fake boto3; return (calls, responses)."""
    calls, responses = [], []

    class Table:
        def __init__(self, name):
            self.name = name

        def get_item(self, Key):
            calls.append(("get_item", self.name, Key))
            if Key.get("config_key") == "audit_settings" and settings is not None:
                return {"Item": dict(settings, config_key="audit_settings")}
            return {}

        def put_item(self, **kw):
            calls.append(("put_item", self.name, kw))
            if index_error and self.name == "acme-audit":
                raise index_error

    class S3:
        def put_object(self, **kw):
            calls.append(("s3.put_object", "", kw))
            if s3_error:
                raise s3_error
            return {"VersionId": "ver-1"}

    fake = types.ModuleType("boto3")
    fake.resource = lambda name: types.SimpleNamespace(Table=Table)
    fake.client = lambda name: S3()
    saved_mod = sys.modules.get("boto3")
    saved_env = {k: os.environ.get(k) for k in _SEED_ENV_KEYS}
    saved_level = logging.getLogger().level
    os.environ.update({"CONFIG_TABLE": "acme-config", "AUDIT_TABLE": "acme-audit",
                       "AUDIT_BUCKET": SEED_BUCKET, "AUDIT_VAULT_DEFAULT": vault_default,
                       "AUDIT_RETENTION_FLOOR": floor})
    sys.modules["boto3"] = fake
    logging.disable(logging.CRITICAL)
    try:
        ns = {"__name__": "index"}
        exec(compile(SEED_SRC, "storage.yaml:EmailSenderSeedFunction", "exec"), ns)  # nosec B102
        ns["_respond"] = lambda event, context, status, data=None, reason="": responses.append((status, data or {}))
        ns["handler"](dict(SEED_EVENT), None)
    finally:
        logging.disable(logging.NOTSET)
        sys.modules["boto3"] = saved_mod
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        logging.getLogger().setLevel(saved_level)
    return calls, responses


def _index_puts(calls):
    return [c[2] for c in calls if c[0] == "put_item" and c[1] == "acme-audit"]


def _vault_puts(calls):
    return [c[2] for c in calls if c[0] == "s3.put_object"]


def _first(calls, pred):
    return next((i for i, c in enumerate(calls) if pred(c)), -1)


calls, responses = run_seed()  # no console value yet; deploy default 'on'
puts, rows = _vault_puts(calls), _index_puts(calls)
check(responses == [("SUCCESS", {"Status": "seeded", "FromAddress": "ops@example.com"})],
      f"the resource succeeds and seeds the sender ({responses})")
cfg = [c[2]["Item"] for c in calls if c[0] == "put_item" and c[1] == "acme-config"]
check(len(cfg) == 1 and cfg[0]["config_key"] == "email_sender" and cfg[0]["from_address"] == "ops@example.com",
      "the config item is written as before")
check(len(puts) == 1 and len(rows) == 1, f"one vault object and one index row ({len(puts)}, {len(rows)})")
i_switch = _first(calls, lambda c: c[0] == "get_item" and c[2].get("config_key") == "audit_settings")
i_vault = _first(calls, lambda c: c[0] == "s3.put_object")
i_row = _first(calls, lambda c: c[0] == "put_item" and c[1] == "acme-audit")
check(0 <= i_switch < i_vault < i_row,
      "the switch is read before the vault write, and the vault is written before the index row")
if puts and rows:
    put, row = puts[0], rows[0]["Item"]
    body = put["Body"]
    record = json.loads(body)
    check(put.get("IfNoneMatch") == "*", "the vault PUT is write-once (IfNoneMatch='*')")
    check(put.get("ChecksumAlgorithm") == "SHA256"
          and put.get("ChecksumSHA256") == base64.b64encode(hashlib.sha256(body).digest()).decode(),
          "it carries ChecksumAlgorithm + ChecksumSHA256 of the body")
    check("ObjectLockMode" not in put and "ObjectLockRetainUntilDate" not in put,
          "no lock headers: the object gets the bucket default (the floor)")
    check(put.get("Bucket") == SEED_BUCKET and put.get("ContentType") == "application/json",
          "to AUDIT_BUCKET, as JSON")
    check(bool(re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$", record.get("ts", ""))),
          f"the record ts has audit_log's format ({record.get('ts')})")
    check(put.get("Key") == al.derive_key(record["ts"], record["event_id"]),
          f"the key follows audit_log.derive_key ({put.get('Key')})")
    want = al.build_record("cloudformation", "email_sender_set", "ops@example.com", "account",
                           record.get("before"), record.get("after"), ctx={},
                           now=al._parse_dt(record["ts"]), event_id=record["event_id"],
                           space_name="", owner_admin="")
    check(record == want, f"the record equals audit_log.build_record's (diff {sorted(set(record) ^ set(want))})")
    check(body == al._json(record).encode(), "and is serialised exactly as audit_log serialises it")
    check(record["actor_sub"] == "cloudformation" and record["actor_role"] == "system"
          and record["category"] == "admin" and record["target_type"] == "setting",
          "actor cloudformation / system, category admin, target_type setting")
    check(record["after"] == {"from_address": "ops@example.com", "source_arn": "", "region": "eu-west-1"},
          "the diff carries the sender fields")
    check(rows[0].get("ConditionExpression") == "attribute_not_exists(entity)", "the index row is insert-only")
    check(row["entity"] == "account" and row["ts"] == f"{record['ts']}#{record['event_id'][:8]}"
          and row["month"] == record["ts"][:7] and row["v"] == 2 and row["event_id"] == record["event_id"],
          "v2 index row: entity account, ts iso#sfx8, month, v, event_id")
    check(row["sha256"] == hashlib.sha256(body).hexdigest() and row["vault"] == "stored"
          and row["s3_key"] == put["Key"] and row["s3_version_id"] == "ver-1",
          "it carries sha256, vault stored, s3_key and s3_version_id")
    check(row.get("retain_until") == (al._parse_dt(record["ts"]) + timedelta(days=365)).isoformat(timespec="microseconds"),
          f"a stored row carries retain_until = now + floor (finding 3) ({row.get('retain_until')})")
    check(row["search"] == al._search(row), "the search text matches audit_log._search")
    check(al._fields_match(row, record), "verify's field comparison accepts the row against its record")

calls, responses = run_seed(floor="30")
rows = _index_puts(calls)
if rows:
    r = rows[0]["Item"]
    check(r.get("retain_until") == (al._parse_dt(r["ts"]) + timedelta(days=30)).isoformat(timespec="microseconds"),
          "retain_until follows AUDIT_RETENTION_FLOOR")

for label, kwargs, want_vault in (
    ("console switch off", {"settings": {"vault_enabled": False}}, False),
    ("no console value, deploy default off", {"vault_default": "off"}, False),
    ("console switch on overrides deploy default off",
     {"settings": {"vault_enabled": True}, "vault_default": "off"}, True),
):
    calls, responses = run_seed(**kwargs)
    rows = _index_puts(calls)
    check(bool(_vault_puts(calls)) == want_vault, f"{label}: vault written = {want_vault}")
    if not want_vault and rows:
        r = rows[0]["Item"]
        check(r["vault"] == "off" and "s3_key" not in r and "retain_until" not in r and r["month"],
              f"{label}: the index row says vault off, with no s3_key or retain_until")
    check(bool(responses) and responses[0][0] == "SUCCESS", f"{label}: the resource succeeds")

calls, responses = run_seed(s3_error=ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject"))
rows = _index_puts(calls)
check(bool(rows) and rows[0]["Item"]["vault"] == "failed" and "s3_key" not in rows[0]["Item"],
      "a refused vault PUT degrades to vault failed on the index row")
check(bool(responses) and responses[0][0] == "SUCCESS", "and never fails the resource")
calls, responses = run_seed(index_error=ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"))
check(bool(responses) and responses[0][0] == "SUCCESS", "an index write failure never fails the resource either")

# Finding 9: check-resource-names.py parses the seed's handler for the config `item`
# and the 'action' literal, so the vault record and index row use other names.
_handler = next(n for n in ast.walk(ast.parse(SEED_SRC))
                if isinstance(n, ast.FunctionDef) and n.name == "handler")
_assigned = {t.id for n in ast.walk(_handler) if isinstance(n, ast.Assign)
             for t in n.targets if isinstance(t, ast.Name)}
check({"item", "record", "row"} <= _assigned,
      "the handler names the config dict item, the vault record record and the index row row")
_actions = [v.value for n in ast.walk(_handler) if isinstance(n, ast.Dict)
            for k, v in zip(n.keys, n.values)
            if isinstance(k, ast.Constant) and k.value == "action" and isinstance(v, ast.Constant)]
check(_actions == ["email_sender_set"], f"the one 'action' literal is inside handler ({_actions})")
_rn = subprocess.run([sys.executable, str(REPO / "deployment/check-resource-names.py")],
                     cwd=str(REPO), capture_output=True, text=True, check=False)
check(_rn.returncode == 0, f"deployment/check-resource-names.py passes (exit {_rn.returncode})")


print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
