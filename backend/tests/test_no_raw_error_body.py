# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A handler that returns an error in its RESPONSE DICT does not leak our identifiers.

The AST guard in tests/test_no_raw_exception_detail.py covers
`raise HTTPException(detail=...)`, but a handler can also hand the caught exception back
in a RETURNED dict body (`return {"ok": False, ..., "error": str(e)}`), which that scan
does not see. Those dict-returning sites must go through `_redact_identifiers(...)`, which
now delegates to the streaming path's redactor so a secret / IAM / STS ARN, the account id
inside any other ARN, the deployment's own metrics-store table names and an address inside the
deployment's VPC are all masked — while a monitored database's own diagnostic data is kept.

This drives a representative handler (`test_database_credentials`) with a stubbed
dependency that raises an exception whose message embeds every one of those identifiers,
and asserts none of them survive in the returned `error` field, and that the message is
still non-empty (actionable).

The functions are lifted out of app.py by source and exec'd with stubs: no AWS, no
FastAPI, no import of app.py's module scope.

Run:  python3 backend/tests/test_no_raw_error_body.py   (or via pytest)
"""
import json
import os
import pathlib
import re
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# event_parser imports tools.types, which imports Agent from strands; stub it so the
# lazy `from streaming.event_parser import _redact_text` inside _redact_identifiers works.
_strands = types.ModuleType("strands")
_strands.tool = lambda f: f
_strands.Agent = object
sys.modules.setdefault("strands", _strands)

APP_SRC = (pathlib.Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")

# The deployment's own identifiers must be masked. Pin the env the redactor reads so the
# endpoint and VPC-IP branches are exercised deterministically.
STORE = "dbops-tier2-scans"
os.environ["TIER1_METRICS_TABLE"] = "dbops-tier1-metrics"
os.environ["TIER2_SCANS_TABLE"] = STORE
os.environ.pop("AGENT_VPC_CIDR", None)  # default 172.20.0.0/16

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
    def __init__(self, status_code=None, detail=None):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _extract(name):
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^(?:@|def |class )|\Z)", APP_SRC, re.S | re.M)
    assert m, f"{name} not found in app.py"
    return m.group(0)


ACCOUNT = "222233334444"
SECRET_ARN = f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:dbops/admin-1/pg/db1/readonly-AbCdef"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/dbops-agent-shop-abc123"
STS_ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/dbops-agent-shop-abc123/access-check"
GENERIC_ARN = f"arn:aws:dynamodb:us-east-1:{ACCOUNT}:table/dbops-spaces"
VPC_IP = "172.20.4.9"  # inside the deployment's own VPC CIDR -> masked

# A botocore-shaped message that embeds every identifier the redactor must strip.
LEAKY_MESSAGE = (
    f"An error occurred (AccessDenied) when calling AssumeRole: {STS_ARN} is not "
    f"authorized to perform sts:AssumeRole on {ROLE_ARN}; it also could not reach "
    f"{STORE} or read {SECRET_ARN} or {GENERIC_ARN} from {VPC_IP}."
)

ns = {
    "json": json, "os": os, "re": re, "HTTPException": HTTPException,
    "logger": types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None,
                                    error=lambda *a, **k: None),
}
exec(_extract("_redact_identifiers"), ns)  # noqa: S102 - pinning shipped source

tns = dict(ns)
tns.update({
    "_can_manage_space": lambda auth, sid: True,
    "spaces_registry": types.SimpleNamespace(
        get_space=lambda sid: {"space_id": sid, "owner_admin": "admin-1"}),
    "_verify_instance_onboardable": lambda iid, sid, auth="": {
        "instance_id": iid, "region": "us-east-1", "account_id": ACCOUNT},
    "_secret_namespace_refusal": lambda space, arn: "",   # name is fine; reach the assume stage
    "_space_secret_prefix": lambda space: "dbops/admin-1/",
    "ConfigureDatabaseRequest": object, "Request": object,
})


def _space_session_raises(space, account_id, region):
    raise RuntimeError(LEAKY_MESSAGE)


tns["_space_session"] = _space_session_raises
exec(re.sub(r"^@app\.post\([^\n]*\)\n", "", _extract("test_database_credentials"), flags=re.M), tns)  # noqa: S102

req = types.SimpleNamespace(headers={"Authorization": "Bearer x"})
data = types.SimpleNamespace(secret_arn=SECRET_ARN, database_name=None)
out = tns["test_database_credentials"]("sp-1", "db-1", data, req)

print("== the assume-role failure body is redacted ==")
check(out["ok"] is False and out["stage"] == "assume_role", f"the handler returns the assume_role stage ({out})")
err = out["error"]
check(bool(err.strip()), "the error is non-empty (still actionable)")
check(SECRET_ARN not in err and "[redacted-secret-arn]" in err, "the secret ARN is masked")
check(ROLE_ARN not in err, "the IAM role ARN is masked")
check(STS_ARN not in err, "the STS assumed-role ARN is masked")
check(GENERIC_ARN not in err, "the DynamoDB ARN (account id inside a generic ARN) is masked")
check(ACCOUNT not in err, f"the 12-digit account id does not survive anywhere ({err})")
check(STORE not in err, "the deployment's own metrics-store table name is masked")
check(VPC_IP not in err, "an address inside the deployment's VPC is masked")

print("")
print("== _redact_identifiers keeps a monitored database's diagnostic data ==")
# The redactor must not strip the customer's own endpoint/host or an out-of-VPC client IP:
# the operator needs those to act on the error.
MON = "prod.cluster-xyz.eu-west-1.rds.amazonaws.com"
kept = ns["_redact_identifiers"](f"could not connect to {MON} from client 10.1.2.3")
check(MON in kept and "10.1.2.3" in kept, f"monitored endpoint and out-of-VPC client IP are kept ({kept})")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
