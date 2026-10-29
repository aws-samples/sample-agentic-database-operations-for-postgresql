# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Bind time is exactly as strict as runtime about a secret's host.

Every runtime connection path (pg_client, run_scan, the Tier 1 collector, Configure's
connectivity check) refuses a secret whose `host` is not exactly the record's endpoint.
The bind-time checks used to be looser and also accepted the reader endpoint, so a secret
could be saved that every later connection then refused. Now the three bind-time checks,
`_reject_secret_host_mismatch` (Configure's persist path), `_login_with_secret` (the join
proof) and the Test-credentials handler, all go through `_allowed_secret_hosts`: exactly
the record's endpoint (for Aurora the cluster endpoint), or no host at all. The refusal
names the endpoint to use. (The one exception, an RDS read replica accepting its source's
endpoint, is pinned in test_replica_shared_secret.py.)

The functions are lifted out of app.py by source and exec'd with a stub HTTPException and
fake sessions: no AWS, no FastAPI, no import of app.py's module scope.

Run:  python3 backend/tests/test_configure_secret_host.py   (or via pytest)
"""
import json
import os
import pathlib
import re
import sys
import types

APP_SRC = (pathlib.Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")

passed = failed = 0


def check(cond, label):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}")


class HTTPException(Exception):
    """Minimal stand-in so the test does not depend on FastAPI being importable."""
    def __init__(self, status_code=None, detail=None):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _extract(name):
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^(?:@|def |class )|\Z)", APP_SRC, re.S | re.M)
    assert m, f"{name} not found in app.py"
    return m.group(0)


SECRET_ARN = "arn:aws:secretsmanager:us-east-1:111111111111:secret:dbops/admin-1/x-AbCdef"
REGION = "us-east-1"
ENDPOINT = "db1.cluster-abc.us-east-1.rds.amazonaws.com"
READER = "db1.cluster-ro-abc.us-east-1.rds.amazonaws.com"
OTHER = "victim.xyz.us-east-1.rds.amazonaws.com"
RECORD = {"instance_id": "db-1", "endpoint": ENDPOINT, "reader_endpoint": READER, "port": 5432,
          "region": REGION, "account_id": "111111111111", "database_name": "app", "tags": {}}
REFUSAL = (f"Set it to {ENDPOINT} (for Aurora, the cluster endpoint), or remove host from the secret.")

connects = []
ns = {"json": json, "HTTPException": HTTPException, "os": os, "re": re,
      "logger": types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
      "ssl_kwargs": lambda: {"sslmode": "verify-full"}}
for fn in ("_secret_host", "_allowed_secret_hosts", "_secret_host_refusal", "_reject_secret_host_mismatch",
           "_login_with_secret"):
    exec(_extract(fn), ns)  # noqa: S102 - pinning shipped source
reject, login = ns["_reject_secret_host_mismatch"], ns["_login_with_secret"]
sys.modules["psycopg2"] = types.SimpleNamespace(
    connect=lambda **kw: connects.append(kw) or types.SimpleNamespace(close=lambda: None))


class _FakeSM:
    def __init__(self, secret_string=None, raise_exc=None):
        self._s = secret_string
        self._raise = raise_exc

    def get_secret_value(self, SecretId=None):
        if self._raise:
            raise self._raise
        return {"SecretString": self._s}


class _FakeSession:
    def __init__(self, sm):
        self._sm = sm

    def client(self, name, region_name=None, **kw):
        return self._sm


def _session(host=None, raise_exc=None, no_string=False):
    if raise_exc:
        return _FakeSession(_FakeSM(raise_exc=raise_exc))
    body = None if no_string else json.dumps(
        {"username": "u", "password": "p", **({"host": host} if host else {})})
    return _FakeSession(_FakeSM(secret_string=body))


def _raised(fn, *args):
    try:
        fn(*args)
        return None
    except HTTPException as e:
        return e


print("== Configure's persist path: exactly the endpoint ==")
check(_raised(reject, _session(host=ENDPOINT), SECRET_ARN, REGION, RECORD) is None,
      "secret host == endpoint -> allowed")
e = _raised(reject, _session(host=READER), SECRET_ARN, REGION, RECORD)
check(e is not None and e.status_code == 400, "secret host == reader endpoint -> refused (400), as at runtime")
check(e is not None and e.detail == f"This secret's host is {READER}. {REFUSAL}",
      f"the refusal names the endpoint to use: {getattr(e, 'detail', '')}")
e = _raised(reject, _session(host=OTHER), SECRET_ARN, REGION, RECORD)
check(e is not None and e.status_code == 400 and ENDPOINT in e.detail,
      "secret host names a different database -> 400 naming the endpoint")

print("")
print("== the check is skipped where runtime also cannot enforce it ==")
check(_raised(reject, _session(host=OTHER), SECRET_ARN, REGION, {"endpoint": "", "tags": {}}) is None,
      "no known endpoint on the record yet -> skipped (not blocked)")
check(_raised(reject, _session(host=None), SECRET_ARN, REGION, RECORD) is None,
      "secret carries no host -> allowed (the record's endpoint is used)")
check(_raised(reject, _session(no_string=True), SECRET_ARN, REGION, RECORD) is None,
      "secret has no SecretString -> skipped")
check(_raised(reject, _session(raise_exc=RuntimeError("AccessDenied")), SECRET_ARN, REGION, RECORD) is None,
      "secret unreadable -> skipped, not blocked (scan re-checks at scan time)")

print("")
print("== the join proof (_login_with_secret): same rule ==")
connects.clear()
check(_raised(login, _session(host=ENDPOINT), SECRET_ARN, REGION, RECORD) is None and connects
      and connects[-1]["host"] == ENDPOINT, "endpoint host -> logs in to the endpoint")
connects.clear()
e = _raised(login, _session(host=READER), SECRET_ARN, REGION, RECORD)
check(e is not None and e.status_code == 400 and REFUSAL in e.detail and not connects,
      "reader-endpoint host -> refused before any login, naming the endpoint")
connects.clear()
check(_raised(login, _session(host=None), SECRET_ARN, REGION, RECORD) is None and connects
      and connects[-1]["host"] == ENDPOINT, "no host -> logs in to the record's endpoint")

print("")
print("== Test credentials: a reader host is a host_mismatch ==")
tns = dict(ns)
tns.update({
    "_can_manage_space": lambda auth, sid: True,
    "spaces_registry": types.SimpleNamespace(get_space=lambda sid: {"space_id": sid, "owner_admin": "admin-1"}),
    "_verify_instance_onboardable": lambda iid, sid, auth="": dict(RECORD),
    "_space_session": lambda space, acct, region: tns["_SESSION"],
    "resource_prefix": lambda: "dbops",
    "_space_secret_prefix": lambda space: "dbops/admin-1/",
    "ConfigureDatabaseRequest": object, "Request": object,
})
exec(_extract("_secret_namespace_refusal"), tns)  # noqa: S102 - pinning shipped source
exec(re.sub(r"^@app\.post\([^\n]*\)\n", "", _extract("test_database_credentials"), flags=re.M), tns)  # noqa: S102
req = types.SimpleNamespace(headers={"Authorization": "Bearer x"})
data = types.SimpleNamespace(secret_arn=SECRET_ARN, database_name=None)
tns["_SESSION"] = _session(host=READER)
out = tns["test_database_credentials"]("sp-1", "db-1", data, req)
check(out["ok"] is False and out["stage"] == "host_mismatch" and out["error"] == f"This secret's host is {READER}. {REFUSAL}",
      f"reader host -> host_mismatch naming the endpoint ({out.get('error', '')})")
tns["_SESSION"] = _session(host=ENDPOINT)
out = tns["test_database_credentials"]("sp-1", "db-1", data, req)
check(out["ok"] is True and out["host"] == ENDPOINT, "endpoint host -> secret validated")
tns["_SESSION"] = _session(host=None)
out = tns["test_database_credentials"]("sp-1", "db-1", data, req)
check(out["ok"] is True and out["host"] == ENDPOINT and out["port"] == 5432,
      "no host -> validated against the record's endpoint and port")

print("")
print("== a secret outside the owner admin's folder is refused before any AWS call ==")
# The case a user hit: a secret created before secrets were kept per admin
# (`dbops/<engine>/<db>/readonly`) passes a plain `dbops/` prefix check but sits outside
# `dbops/<owner admin>/`, so IAM refused it with a bare AccessDenied at fetch_secret.
OLD_ARN = "arn:aws:secretsmanager:us-east-1:111111111111:secret:dbops/aurora-postgresql/db1/readonly-xjmoUY"
refuse = tns["_secret_namespace_refusal"]
space = {"space_id": "sp-1", "owner_admin": "admin-1"}
msg = refuse(space, OLD_ARN)
check(msg and "'dbops/aurora-postgresql/db1/readonly'" in msg and "'dbops/admin-1/'" in msg,
      f"old-named secret -> refusal names the secret and the folder ({msg})")
check(refuse(space, SECRET_ARN) == "", "secret inside dbops/admin-1/ -> no refusal")
check(refuse(space, "arn:aws:secretsmanager:us-east-1:1:secret:dbops/admin-1/ro") == "",
      "an ARN without the random suffix keeps its name intact (no cut at the last dash)")
check(refuse(space, "arn:aws:secretsmanager:us-east-1:1:secret:dbops/admin-1/my-db-ro-AbCdEf") == "",
      "dashes inside the name are kept; only the 6-char suffix is stripped")
check(refuse(space, "arn:aws:secretsmanager:us-east-1:1:secret:dbops/admin-10/x-AbCdef") != "",
      "a sibling folder sharing the prefix (admin-10 vs admin-1) is refused, not prefix-matched")

sessions_asked = []
tns["_space_session"] = lambda space, acct, region: sessions_asked.append(acct) or tns["_SESSION"]
tns["_SESSION"] = _session(host=ENDPOINT)
out = tns["test_database_credentials"]("sp-1", "db-1", types.SimpleNamespace(secret_arn=OLD_ARN, database_name=None), req)
check(out["ok"] is False and out["stage"] == "secret_namespace" and out["expected_prefix"] == "dbops/admin-1/",
      f"Test credentials -> stage secret_namespace with the expected folder ({out})")
check(not sessions_asked, "no role is assumed and no secret is read for a refused name")

src = _extract("configure_space_database")
supplied_branch = src.split("secret_arn = supplied", 1)[1]
check("_secret_namespace_refusal(space, secret_arn)" in supplied_branch.split("_space_session(", 1)[0]
      and "status_code=400, detail=refusal" in supplied_branch.split("_space_session(", 1)[0],
      "onboard refuses the same name with a 400 before it assumes the space's role")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
