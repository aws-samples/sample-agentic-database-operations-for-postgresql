#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The operator app has one role. This pins the places where that is decided.

The rule: in the operator app there are only operators. Access to a space is the space's
operator group in the token and nothing else - not the super-admin role, not the
space-admin role, not having created the space. And whatever an operator sees or can do,
every other operator of that space sees and can do identically, whoever they also are.

It was broken quietly, from below. The operator app and the admin console share one REST
API and one Cognito client, so the API cannot tell the apps apart and told *roles* apart
instead: `GET /api/databases` handed admins the raw registry record (credential pointer
included) and operators a trimmed one. The chat and investigation pages were written and
tested with an admin token, built the agent's [DATABASE SCOPE] block in the browser from
that listing, and therefore worked for admins only - a plain operator's block said
`secret_arn=undefined`, every SQL-backed analysis was refused, and investigations came
back empty. Nobody had checked an admin role in the operator app; the dependency was on
what the caller's own listing happened to contain.

So the contract has two halves, and both are asserted against source (no AWS, no app
import, runs on any python3):

  * the operator app never reads a role-dependent field, never calls the role-projected
    listing, never carries a credential pointer, and sends database *ids* for the agent's
    scope - the server builds the block from the registry after the membership checks;
  * the endpoints that exist for the operator app (`/api/my-spaces`, its databases
    listing, the agent turn) know no roles: they gate on the operator group only, project
    the same shape for everyone, and count every caller against the rate limit.

    python3 tests/test_operator_single_role.py      (from the repository root)
"""
import base64
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from utils.auth import is_operator_of, operator_space_ids, space_group_name  # noqa: E402
from utils.db_projection import project_db_for_operator  # noqa: E402

APP = (ROOT / "backend/app.py").read_text(encoding="utf-8")
TOPOLOGY = (ROOT / "backend/tools/aws/topology_builder.py").read_text(encoding="utf-8")
FRONTEND = sorted(p for p in (ROOT / "frontend/src").rglob("*") if p.suffix in (".js", ".jsx"))

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def route_body(decorator: str) -> str:
    """One route's source: its decorator up to the next `@app.` decorator."""
    start = APP.find(decorator)
    if start == -1:
        return ""
    nxt = APP.find("\n@app.", start + len(decorator))
    return APP[start: nxt if nxt != -1 else len(APP)]


def strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)(^|[^:\"'`])//.*$", r"\1", src)


def bearer(groups) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "u-1", "cognito:groups": groups}).encode())
    return "Bearer x." + payload.decode().rstrip("=") + ".y"


SPACE_A = "aaaaaaaa-1111-2222-3333-444444444444"
SPACE_B = "bbbbbbbb-1111-2222-3333-444444444444"

# ── the operator app knows no roles and carries no credential pointer ─────────────────
print("══ the operator app: same code for every operator ══")
code = {p: strip_js_comments(p.read_text(encoding="utf-8")) for p in FRONTEND}
rel = lambda p: str(p.relative_to(ROOT))  # noqa: E731

offenders = [rel(p) for p, s in code.items() if re.search(r"\b(secret_arn|space_secrets)\b", s)]
check(not offenders, f"no operator-app source reads a credential pointer (found in {offenders})")

offenders = [rel(p) for p, s in code.items() if "[DATABASE SCOPE" in s]
check(not offenders, f"no operator-app source builds the agent's scope block itself (found in {offenders})")

offenders = [rel(p) for p, s in code.items()
             if re.search(r"cognito:groups|\bisAdmin\b|\bis_admin\b|space-admins|['\"]admins['\"]", s)]
check(not offenders, f"no operator-app source branches on a role (found in {offenders})")

offenders = [rel(p) for p, s in code.items() if re.search(r"['\"`]/api/databases\b", s)]
check(not offenders, f"the operator app never calls the role-projected /api/databases (found in {offenders})")

users = [rel(p) for p, s in code.items() if re.search(r"/api/my-spaces/\$\{[^}]+\}/databases", s)]
check({"frontend/src/App.jsx", "frontend/src/components/Dashboard.jsx"} <= set(users),
      f"App.jsx and Dashboard.jsx list databases through the operator listing (users: {users})")

streaming = code[ROOT / "frontend/src/hooks/useStreaming.js"]
check("body.database_ids = opts.databaseIds" in streaming, "the agent turn carries database ids")
chat = code[ROOT / "frontend/src/components/Chat.jsx"]
check("databaseIds: scopeDatabaseIds" in chat and "activeScope.map(d => d.instance_id)" in chat
      and "opts.scopeDatabases !== undefined ? opts.scopeDatabases : databaseScope" in chat,
      "chat sends the selected databases as ids ([] = all configured; a live hand-off pins the report db)")
inv = code[ROOT / "frontend/src/components/InvestigationDetail.jsx"]
check("databaseIds: dbIds" in inv, "an investigation turn sends the record's database ids")
topo = code[ROOT / "frontend/src/components/Topology.jsx"]
check("credentials_configured" in topo, "the Topology page shows whether credentials are on file, not which")

# ── the operator endpoints know no roles ──────────────────────────────────────────────
print("")
print("══ /api/my-spaces: the operator group decides, nothing else ══")
body = route_body('@app.get("/api/my-spaces")')
check(bool(body), "the route exists")
check("operator_space_ids(" in body, "lists the spaces whose operator group is in the token")
for forbidden in ("created_by", "is_creator", "_created_by_caller(", "is_admin(", "is_space_admin(",
                  "is_space_member(", "_can_view_space(", "_accessible_space_ids("):
    check(forbidden not in body, f"does not consult {forbidden.rstrip('(')}")

print("")
print("══ /api/my-spaces/{space_id}/databases: one gate, one shape ══")
body = route_body('@app.get("/api/my-spaces/{space_id}/databases")')
check(bool(body), "the route exists")
check("is_operator_of(auth_header, space_id)" in body, "gated on the space's operator group")
check('status_code=404, detail="Space not found"' in body, "a foreign space is an opaque 404")
check("project_db_for_operator(" in body, "every caller gets the operator projection")
for forbidden in ("project_db_for_manager(", "is_admin(", "is_space_admin(", "_can_manage_space(",
                  "_can_view_space(", "_accessible_space_ids(", "created_by"):
    check(forbidden not in body, f"does not consult {forbidden.rstrip('(')}")

print("")
print("══ the agent turn: the server builds the scope, everyone is rate-limited ══")
body = route_body('@app.post("/invocations")')
check("database_ids" in body and "_scope_records(" in body, "resolves the caller's database ids server-side")
check("chat_scope_block_stable(" in body and "investigation_scope_block(" in body,
      "writes both the chat (stable per-session aliases) and the investigation scope blocks itself")
check("is_admin(" not in body, "no admin role is consulted anywhere in the turn")
check(re.search(r"^\s*allowed, retry_after = check_rate_limit\(user_id\)", body, re.M) is not None,
      "the rate limit counts every caller")
scope_fn = APP[APP.find("def _scope_records("): APP.find("\n@app.post(\"/invocations\")")]
check("_verify_instance_in_space(" in scope_fn, "an id outside the space is the same opaque 404 as any other read")
check('d.get("status") == "configured"' in scope_fn, "an empty list means every configured database of the space")

print("")
print("══ the credential pointer never reaches an operator ══")
check('"secret_arn":' not in TOPOLOGY and '"credentials_configured": bool(db.get("secret_arn"))' in TOPOLOGY,
      "topology carries a boolean, not the Secrets Manager ARN")
record = {"instance_id": "i", "secret_arn": "arn:aws:secretsmanager:x", "space_secrets": {SPACE_A: "arn"},
          "member_space_ids": [SPACE_A, SPACE_B], "status": "configured"}
projected = project_db_for_operator(record, {SPACE_A})
check(not any(f in projected for f in ("secret_arn", "space_secrets", "reach_space_id")), "the operator projection drops the credential pointers and the reach provider")
check(projected["member_space_ids"] == [SPACE_A], "and narrows the membership list to the caller's own spaces")

# ── the access model itself ───────────────────────────────────────────────────────────
print("")
print("══ operator_space_ids / is_operator_of: groups only ══")
t = bearer(["admins", "space-admins", space_group_name(SPACE_A)])
check(operator_space_ids(t) == {SPACE_A}, "a super admin who is also an operator of A is an operator of A only")
check(is_operator_of(t, SPACE_A) and not is_operator_of(t, SPACE_B),
      "the admin role opens no other space in the operator app")
check(operator_space_ids(bearer(["admins"])) == set(), "a super admin with no operator group has no spaces")
check(operator_space_ids(bearer(["space-admins"])) == set(),
      "a space admin with no operator group has no spaces ('space-admins' is a role, not the space 'admins')")
check(not is_operator_of(bearer(["space-admins", "admins"]), "admins"),
      "and no role group can be read as a space named after it")
check(operator_space_ids(bearer([])) == set() and not is_operator_of(bearer([]), SPACE_A),
      "no groups, no spaces")
check(not is_operator_of(bearer(["space-"]), "") and operator_space_ids(bearer(["space-"])) == set(),
      "a bare 'space-' group names no space")
check(operator_space_ids(bearer([space_group_name(SPACE_A), space_group_name(SPACE_B)])) == {SPACE_A, SPACE_B},
      "every operator group counts, and only operator groups")
check(not is_operator_of("", SPACE_A) and operator_space_ids("") == set(), "no token, no spaces")


# ── created_by is an OWNER convenience: it counts only with the space-admins role ────
# The read gates (`_can_view_space` via `_created_by_caller`, and `_accessible_space_ids`)
# used to accept the space's creator without asking whether they still hold the role. A
# creator demoted out of space-admins whose operator seat was also removed could then
# still read the space through the API. The manage gate already required the role; the
# read gates now do too. Pinned by source like the role-free operator routes above.
import re as _re
_app = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")
def _fn(name):
    m = _re.search(rf"^def {name}\(.*?(?=^def |^@app\.)", _app, _re.S | _re.M)
    return m.group(0) if m else ""
_cb = _fn("_created_by_caller")
_cb_body = _cb.split('"""')[-1]   # the code after the docstring
check("if not (is_space_admin(auth_header) or is_admin(auth_header)):" in _cb_body and
      _cb_body.index("is_space_admin(") < _cb_body.index("created_by"),
      "_created_by_caller checks for an admin role before consulting created_by")
_acc = _fn("_accessible_space_ids")
check('creator_ok = "space-admins" in groups' in _acc and "(creator_ok and s.get(\"created_by\") == sub)" in _acc,
      "_accessible_space_ids honours created_by only for a current space admin")
_my = _fn("get_my_spaces")
check("created_by" not in _my, "the operator app's space list never consults created_by")
print("")
print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
