# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The "Add secondary cloud source" wizard knows whether the owner's role is already there.

Credentials are keyed on the owner admin, so an admin has ONE access role per account,
``<prefix>-agent-<owner key>``, shared by all their spaces. POST
/api/spaces/{id}/accounts/check answers, read-only, whether that role is usable in an
account: it assumes it with the owner's id as sts:ExternalId (what configure-access
presents) and, as the role, reads its own DBOpsPolicyVersion tag.

Pinned through the real functions (extracted from backend/app.py) with fake STS and IAM:
  * ready: assumable and tagged with the current version; the caller's other spaces
    listing the account are named (and only the caller's own);
  * outdated: an older tag, no tag, or tags unreadable (a policy before ListRoleTags);
  * missing: the far side refuses and nothing shows the role exists;
  * denied: the far side refuses but another of the owner's spaces validated the role
    (fix the trust), or the deployment's own policy refuses (fix the name, or the
    deployment); the message says what to do;
  * any other STS error is a 502 that says to try again, never a state;
  * the gate is _can_manage_space, the id is 12 digits, and nothing is written.

Run:  python3 backend/tests/test_account_check.py
"""
import os
import pathlib
import re
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
APP_SRC = (ROOT / "backend/app.py").read_text(encoding="utf-8")
passed = failed = 0

os.environ["RESOURCE_PREFIX"] = "dbops"
from tools.resource_names import owner_admin_of, agent_role_name, resource_prefix  # noqa: E402


def check(cond, label):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}")


def _extract(name):
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^(?:@|def |async def |class |[A-Z_]+ = )|\Z)",
                  APP_SRC, re.S | re.M)
    assert m, f"{name} not found in app.py"
    return m.group(0)


def _const(name):
    m = re.search(rf'^{name} = "([^"]*)"', APP_SRC, re.M)
    assert m, name
    return m.group(1)


VERSION = _const("MEMBER_POLICY_VERSION")
ADMIN_A = "aaaa1111-2222-3333-4444-555566667777"
ADMIN_B = "bbbb1111-2222-3333-4444-555566667777"
ACCT = "222222222222"
ROLE_A = agent_role_name(ADMIN_A)


class ClientError(Exception):
    def __init__(self, code, message):
        super().__init__(f"An error occurred ({code}) when calling the operation: {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


TRUST_DENIED = ClientError("AccessDenied", "User: arn:aws:sts::111111111111:assumed-role/task/x is "
                           f"not authorized to perform: sts:AssumeRole on resource: arn:aws:iam::{ACCT}:role/r")
IDENTITY_DENIED = ClientError("AccessDenied", "User: arn:aws:sts::111111111111:assumed-role/task/x is "
                              "not authorized to perform: sts:AssumeRole on resource: arn:aws:iam::"
                              f"{ACCT}:role/r because no identity-based policy allows the sts:AssumeRole action")


class FakeAWS:
    """`assume` is an exception to raise or None; `tags` a list, or an exception for ListRoleTags."""
    def __init__(self, assume=None, tags=None):
        self.assume, self.tags = assume, tags if tags is not None else []
        self.calls = []

    def client(self, service, **kw):
        aws = self

        class STS:
            def assume_role(self, **k):
                aws.calls.append(("sts.assume_role", k))
                if aws.assume:
                    raise aws.assume
                return {"Credentials": {"AccessKeyId": "AK", "SecretAccessKey": "SK", "SessionToken": "ST"}}

        class IAM:
            def list_role_tags(self, **k):
                aws.calls.append(("iam.list_role_tags", {**k, "creds": kw.get("aws_session_token")}))
                if isinstance(aws.tags, Exception):
                    raise aws.tags
                return {"Tags": aws.tags}

            def __getattr__(self, name):
                aws.calls.append((f"iam.{name}", {}))
                raise AssertionError(f"the check must not call iam.{name}")

        return {"sts": STS, "iam": IAM}[service]()


class HTTPException(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


SPACE = {"space_id": "s-1", "name": "Payments", "owner_admin": ADMIN_A, "primary_account_id": "111111111111",
         "secondary_account_ids": []}


def build(aws, spaces, caller=ADMIN_A, can_manage=True):
    ns = {
        "os": os, "re": re, "boto3": aws, "HTTPException": HTTPException, "Request": object,
        "AccountCheck": object, "Optional": None,
        "logger": types.SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None),
        "owner_admin_of": owner_admin_of, "agent_role_name": agent_role_name,
        "resource_prefix": resource_prefix,
        "spaces_registry": types.SimpleNamespace(
            list_spaces=lambda: list(spaces),
            get_space=lambda sid: next((s for s in spaces if s["space_id"] == sid), None),
            update_space=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no writes"))),
        "_can_manage_space": lambda auth, sid: can_manage,
        "get_user_id": lambda auth: caller,
        "MEMBER_POLICY_VERSION": VERSION, "MEMBER_POLICY_VERSION_TAG": _const("MEMBER_POLICY_VERSION_TAG"),
        "_ROLE_NAME_RE": re.compile(r"^[\w+=,.@-]{1,64}$"),
    }
    exec(_extract("_check_member_account"), ns)  # noqa: S102
    exec(_extract("check_space_account"), ns)  # noqa: S102
    return ns


def run(aws, spaces=None, caller=ADMIN_A, account=ACCT, role_name=None, can_manage=True):
    spaces = spaces if spaces is not None else [SPACE]
    ns = build(aws, spaces, caller, can_manage)
    req = types.SimpleNamespace(headers={"Authorization": "Bearer x"})
    data = types.SimpleNamespace(account_id=account, role_name=role_name)
    try:
        return ns["check_space_account"]("s-1", data, req)
    except HTTPException as e:
        return e


def other(name, owner=ADMIN_A, status="valid", role=None):
    sp = {"space_id": f"o-{name}", "name": name, "owner_admin": owner, "secondary_account_ids": [ACCT],
          "account_statuses": {ACCT: status}}
    if role:
        sp["secondary_role_names"] = {ACCT: role}
    return sp


print("== ready: the role is there with the current policy ==")
aws = FakeAWS(tags=[{"Key": "ManagedBy", "Value": "dbops-agent"}, {"Key": "DBOpsPolicyVersion", "Value": VERSION}])
res = run(aws, [SPACE, other("Orders"), other("Billing"), other("Theirs", owner=ADMIN_B)])
check(res["state"] == "ready", f"state is ready ({res.get('state')})")
check(res["role_name"] == ROLE_A == "dbops-agent-aaaa11112222", f"the shared role name ({res['role_name']})")
check(res["spaces"] == ["Billing", "Orders"], f"the caller's other spaces that list it, names only ({res['spaces']})")
assume = next(k for c, k in aws.calls if c == "sts.assume_role")
check(assume["RoleArn"] == f"arn:aws:iam::{ACCT}:role/{ROLE_A}" and assume["ExternalId"] == ADMIN_A,
      "it assumes the owner's role with the owner's id as ExternalId")
tags_call = next(k for c, k in aws.calls if c == "iam.list_role_tags")
check(tags_call["RoleName"] == ROLE_A and tags_call["creds"] == "ST", "the tags are read AS the role, on its own name")
check([c for c, _ in aws.calls] == ["sts.assume_role", "iam.list_role_tags"], "nothing else is called")

print("")
print("== a super admin checking another admin's space sees no space names ==")
res = run(aws, [SPACE, other("Orders")], caller="super-1")
check(res["state"] == "ready" and res["spaces"] == [], f"state ready, no names ({res['spaces']})")

print("")
print("== outdated: older tag, no tag, tags unreadable ==")
res = run(FakeAWS(tags=[{"Key": "DBOpsPolicyVersion", "Value": "0"}]))
check(res["state"] == "outdated" and res["role_policy_version"] == "0", f"an older tag is outdated ({res['state']})")
check("older than" in res["message"], "the message says the permissions are older")
res = run(FakeAWS(tags=[{"Key": "ManagedBy", "Value": "dbops-agent"}]))
check(res["state"] == "outdated" and res["role_policy_version"] is None, "a missing tag is outdated")
res = run(FakeAWS(tags=ClientError("AccessDenied", "not authorized to perform: iam:ListRoleTags")))
check(res["state"] == "outdated", "a policy without ListRoleTags (before versions) is outdated")

print("")
print("== missing: refused, and nothing shows the role exists ==")
res = run(FakeAWS(assume=TRUST_DENIED), [SPACE, other("Orders", status="failed")])
check(res["state"] == "missing", f"state is missing ({res.get('state')})")
check(ACCT in res["message"], "the message names the account")
res = run(FakeAWS(assume=TRUST_DENIED), [SPACE, other("Elsewhere", owner=ADMIN_B)])
check(res["state"] == "missing", "another admin's validated space is no evidence for this owner's role")

print("")
print("== denied: the role is known to exist but its trust refuses ==")
res = run(FakeAWS(assume=TRUST_DENIED), [SPACE, other("Orders")])
check(res["state"] == "denied" and res.get("fix") == "trust", f"denied, fix the trust ({res.get('state')}, {res.get('fix')})")
check("Orders" in res["message"] and "trust policy" in res["message"] and ADMIN_A in res["message"],
      f"the message says where it was validated and how to fix it ({res['message']})")
res = run(FakeAWS(assume=TRUST_DENIED), [SPACE, other("Orders", role="dbops-agent-custom")])
check(res["state"] == "missing", "a different role name validated elsewhere is no evidence for this one")
res = run(FakeAWS(assume=TRUST_DENIED), [SPACE, other("Orders")], caller="super-1")
check(res["state"] == "denied" and "Orders" not in res["message"], "a super admin is not shown the owner's space name")

print("")
print("== denied: the deployment's own policy refuses ==")
res = run(FakeAWS(assume=IDENTITY_DENIED), role_name="TeamRole-dbops")
check(res["state"] == "denied" and res.get("fix") == "name", f"a name outside dbops-* is denied, fix the name ({res.get('fix')})")
check("dbops-" in res["message"] and "starts with" in res["message"], f"the message says the name must start with dbops- ({res['message']})")
res = run(FakeAWS(assume=IDENTITY_DENIED))
check(res["state"] == "denied" and res.get("fix") == "deployment", "a blocked assume on the right name points at the deployment account")
check("service control policy" in res["message"], "the message names the likely cause")

print("")
print("== any other error is a 502, not a state ==")
res = run(FakeAWS(assume=ClientError("Throttling", "Rate exceeded")))
check(isinstance(res, HTTPException) and res.status_code == 502 and "Try again" in res.detail,
      f"502 that says to try again ({getattr(res, 'detail', res)})")

print("")
print("== the gate and the inputs ==")
res = run(FakeAWS(), can_manage=False)
check(isinstance(res, HTTPException) and res.status_code == 403, "a caller who cannot manage the space gets 403")
res = run(FakeAWS(), account="12345")
check(isinstance(res, HTTPException) and res.status_code == 400 and "12 digits" in res.detail, "a short id is refused")
res = run(FakeAWS(), role_name="bad name!")
check(isinstance(res, HTTPException) and res.status_code == 400, "an invalid role name is refused")
aws = FakeAWS(tags=[{"Key": "DBOpsPolicyVersion", "Value": VERSION}])
res = run(aws, role_name="dbops-agent-strict-name")
assume = next(k for c, k in aws.calls if c == "sts.assume_role")
check(res["state"] == "ready" and assume["RoleArn"].endswith("role/dbops-agent-strict-name"),
      "a custom name is checked as typed")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
