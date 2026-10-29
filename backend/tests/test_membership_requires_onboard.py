# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A space becomes a member of a database only by onboarding it with a verified secret.

Two invariants make a space a real tenant boundary rather than a label anyone with
discovery rights can attach:

  1. DISCOVERY NEVER GRANTS. `discover_space_databases` writes only `candidate_space_ids`
     (add_candidate); it never calls add_member or promote_candidate, and a record it
     creates seeds a candidate, not a member. Finding a database confers no access to it.
  2. MEMBERSHIP REQUIRES A VERIFIED SECRET. The only writer that adds a space to
     `member_space_ids` after creation is `promote_candidate`, which refuses an empty
     secret. `add_member` has no caller left in the application.

Plus the tenancy predicate every gate reads: a record that only has candidates has NO
members, so the legacy `tags.space_id` fallback cannot smuggle a merely-discovered space in.

Static checks over app.py's source and hermetic checks over the registry / predicate.

Run:  python3 backend/tests/test_membership_requires_onboard.py   (or via pytest)
"""
import os
import pathlib
import re
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
BACKEND = pathlib.Path(__file__).resolve().parents[1]
APP_SRC = (BACKEND / "app.py").read_text(encoding="utf-8")

SPACE_A, SPACE_B = "space-a", "space-b"


def _func_src(name: str) -> str:
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^(?:@|def |class )|\Z)", APP_SRC, re.S | re.M)
    assert m, f"{name} not found in app.py"
    return m.group(0)


# ── invariant 1: discovery never grants ──────────────────────────────────────

def test_discovery_writes_candidates_only():
    # The discovery body moved to _run_discovery (the route only schedules it).
    src = _func_src("_run_discovery")
    assert "add_candidate(" in src, "discovery must propose via add_candidate"
    assert "add_member(" not in src, "discovery must NEVER attach membership"
    assert "promote_candidate(" not in src, "discovery must NEVER promote to member"


def test_add_member_has_no_application_caller():
    # Only the registry definition may mention add_member; nothing in app.py calls it.
    assert "add_member(" not in APP_SRC, "add_member must not be called from the application"


def test_promote_candidate_is_the_only_membership_writer_and_needs_a_secret():
    callers = [ln for ln in APP_SRC.splitlines() if "promote_candidate(" in ln]
    assert callers, "promote_candidate must be used to grant membership"
    for ln in callers:
        # every call passes a secret (third positional arg is the verified secret_arn)
        assert "secret_arn" in ln, f"promote_candidate must receive the verified secret: {ln.strip()}"
    cfg = _func_src("configure_space_database")
    assert "promote_candidate(" in cfg, "onboarding (configure) is where membership is granted"


# ── invariant 2: the registry enforces it where it is written ────────────────

class _FakeTable:
    def __init__(self):
        self.updates = []

    def update_item(self, **kw):
        self.updates.append(kw)


def _registry():
    import importlib
    boto3_stub = types.ModuleType("boto3")
    boto3_stub.resource = lambda *a, **k: types.SimpleNamespace(Table=lambda n: _FakeTable())
    boto3_stub.client = lambda *a, **k: None
    sys.modules["boto3"] = boto3_stub
    sys.modules.setdefault("boto3.dynamodb", types.ModuleType("boto3.dynamodb"))
    cond = types.ModuleType("boto3.dynamodb.conditions")
    cond.Key = lambda *a, **k: types.SimpleNamespace(eq=lambda v: None)
    sys.modules["boto3.dynamodb.conditions"] = cond
    import tools.aws.dynamodb_client as ddb
    importlib.reload(ddb)
    reg = ddb.DbInstanceRegistry()
    reg._table = _FakeTable()  # `table` is a lazy property over this backing field
    return reg


def test_promote_candidate_refuses_missing_secret():
    reg = _registry()
    for bad in ("", None):
        try:
            reg.promote_candidate("db-1", SPACE_A, bad)
        except ValueError:
            pass
        else:
            raise AssertionError("promote_candidate must refuse an empty secret")
    assert reg.table.updates == [], "a refused promotion must write nothing"


def test_promote_candidate_moves_space_from_candidate_to_member_with_proof():
    reg = _registry()
    reg.promote_candidate("db-1", SPACE_A, "arn:secret:a")
    exprs = " ".join(u["UpdateExpression"] for u in reg.table.updates)
    assert "ADD member_space_ids" in exprs and "DELETE candidate_space_ids" in exprs
    assert "space_secrets.#s" in exprs, "the verified secret is recorded as the space's proof"


def test_create_instance_without_secret_seeds_a_candidate_not_a_member():
    reg = _registry()
    put = {}
    reg.table.put_item = lambda Item: put.update(Item)
    reg.create_instance({"display_name": "x", "secret_arn": "", "space_id": SPACE_A, "tags": {"arn": "arn:x"}})
    assert put.get("candidate_space_ids") == {SPACE_A}
    assert "member_space_ids" not in put and "reach_space_id" not in put, \
        "a discovery-created record must not be a member of, nor reached by, the discovering space"


def test_create_instance_with_secret_is_a_genuine_onboard():
    reg = _registry()
    put = {}
    reg.table.put_item = lambda Item: put.update(Item)
    reg.create_instance({"display_name": "x", "secret_arn": "arn:secret:a", "space_id": SPACE_A})
    assert put.get("member_space_ids") == {SPACE_A} and put.get("reach_space_id") == SPACE_A
    assert put.get("space_secrets") == {SPACE_A: "arn:secret:a"}


# ── the predicate every gate reads ───────────────────────────────────────────

def test_candidate_only_record_has_no_members_even_with_legacy_tag():
    from tools.aws.tenancy import record_member_space_ids, record_candidate_space_ids, record_in_space
    rec = {"candidate_space_ids": {SPACE_B}, "tags": {"space_id": SPACE_B}}
    assert record_member_space_ids(rec) == set(), "a merely-discovered record has no members"
    assert record_candidate_space_ids(rec) == {SPACE_B}
    assert record_in_space(rec, SPACE_B) is False


def test_members_win_over_candidates():
    from tools.aws.tenancy import record_member_space_ids
    rec = {"member_space_ids": {SPACE_A}, "candidate_space_ids": {SPACE_B}}
    assert record_member_space_ids(rec) == {SPACE_A}


def test_never_onboarded_record_has_no_members_and_reports_legacy_ones_as_candidates():
    # Leftover of the discovery-attaches model: status still 'discovered' (nobody ever
    # onboarded it) but spaces sit in member_space_ids. Those spaces
    # must not read as members (no access) and must surface as candidates, so the admin
    # console can show Onboard / Dismiss instead of hiding the row entirely.
    from tools.aws.tenancy import record_member_space_ids, record_candidate_space_ids, record_in_space
    rec = {"status": "discovered", "member_space_ids": {SPACE_A}, "candidate_space_ids": {"space-c"}}
    assert record_member_space_ids(rec) == set()
    assert record_in_space(rec, SPACE_A) is False
    assert record_candidate_space_ids(rec) == {SPACE_A, "space-c"}
    # Once onboarded (status moved on), the member set is authoritative again.
    rec["status"] = "configuring"
    assert record_member_space_ids(rec) == {SPACE_A}
    assert record_candidate_space_ids(rec) == {"space-c"}


def test_discovery_and_dismiss_rewrite_legacy_discovered_members_to_candidates():
    # The predicate hides the leftover; discovery and dismiss also fix the stored record.
    disc = _func_src("_run_discovery")
    assert 'space_id in set(existing_rec.get("member_space_ids")' in disc
    assert "remove_member(existing_rec" in disc and "add_candidate(existing_rec" in disc
    dis = _func_src("dismiss_space_candidate")
    assert "remove_member(instance_id, space_id)" in dis


# ── membership is granted only by a successful LOGIN, never a host comparison ──

def test_join_logs_in_with_the_secret_before_granting_membership():
    cfg = _func_src("configure_space_database")
    join = cfg.split('if mode == "join":', 1)[1].split("# ── FIRST ONBOARD", 1)[0]
    assert "_login_with_secret(session, secret_arn, region, instance)" in join
    # ...and the login comes BEFORE the membership write.
    assert join.index("_login_with_secret(") < join.index("promote_candidate(")
    # Reading the secret and comparing its host field is not a proof, so the join path
    # must not rely on it.
    assert "_secret_host(" not in join and "_reject_secret_host_mismatch(" not in join


def test_login_helper_connects_through_the_spaces_own_session_and_checks_the_host():
    src = _func_src("_login_with_secret")
    assert 'session.client("secretsmanager"' in src, "the secret is read through the onboarding space's role"
    assert "psycopg2.connect(" in src and "**ssl_kwargs()" in src, \
        "the login verifies the server certificate like every other connection (db_tls)"
    assert "allowed = _allowed_secret_hosts(instance)" in src and "secret_host not in allowed" in src, \
        "a secret naming another database is refused (the same rule as every runtime path)"
    assert 'host = instance.get("endpoint") or secret_host' in src, \
        "connects to the record's own endpoint (host precedence), never to the secret's host alone"
    assert src.count("HTTPException(status_code=400") >= 3


def test_first_onboard_grants_membership_only_after_connectivity_is_verified():
    cfg = _func_src("configure_space_database")
    first = cfg.split('if mode == "first_onboard":', 1)[1].split("# Step 2", 1)[0]
    assert "promote_candidate(" not in first, "first onboard must not add the member synchronously"
    assert "set_reach_space(instance_id, space_id, secret_arn=secret_arn," in first
    task = _func_src("_configure_single_db")
    tail = task.split("if connected:", 1)[1].split("else:", 1)[0]
    assert "promote_candidate(instance_id, space_id, secret_arn)" in tail, \
        "membership is written where the login succeeded"
    assert "space_id not in _instance_member_spaces(instance)" in tail, \
        "a reconfigure for an existing member leaves its recorded proof alone"


# ── ownership proof: IAM decides, through the owner admin's role ─────────────────
def test_no_string_comparison_decides_whose_secret_this_is():
    """A secret already on the record is fine for a second space of the SAME admin (they
    share a namespace and a role); another admin's secret fails inside IAM when the
    space's role tries to read it. So there is no application-level ownership refusal:
    the proof is the login as the space's owner admin's role, and nothing else."""
    assert "_reject_secret_owned_elsewhere" not in APP_SRC
    cfg = _func_src("configure_space_database")
    assert "_space_session(space, account_id, region)" in cfg, "the read runs as the space's role"
    tc = _func_src("test_database_credentials")
    assert "_space_session(space, account_id, region)" in tc and "boto3.client(\"secretsmanager\"" not in tc, \
        "Test secret reads through the space's role, never the task role"
    sess = _func_src("_space_session")
    assert "ExternalId=owner," in sess and "owner = owner_admin_of(space)" in sess, \
        "the role is assumed with the owner admin's id as sts:ExternalId"


def test_candidates_prefill_only_from_the_same_owner_admin():
    src = _func_src("list_space_candidates")
    assert "owner_admin_of(sp) == owner" in src, "sibling spaces are those with the same owner admin"
    assert "suggested_secret_arn" in src and "can_share_from" not in src
    assert "_can_manage_space(auth, o)" not in src, \
        "the suggestion is computed from ownership, never from what the caller manages"


def test_secret_namespace_and_role_are_keyed_on_the_owner_admin():
    from tools.resource_names import owner_admin_of, agent_role_name, secret_namespace, owner_key
    sp = {"space_id": "s1", "created_by": "11111111-2222-3333-4444-555555555555"}
    assert owner_admin_of(sp) == "11111111-2222-3333-4444-555555555555", "owner defaults to the creator"
    assert owner_admin_of({**sp, "owner_admin": "aaaa"}) == "aaaa"
    assert secret_namespace(owner_admin_of(sp)).endswith("/11111111-2222-3333-4444-555555555555/")
    assert agent_role_name(owner_admin_of(sp)).endswith("-agent-111111112222") and len(agent_role_name(owner_admin_of(sp))) <= 64
    assert owner_key("ABCD-EF") == "abcdef"
    pol = _func_src("agent_space_permissions_policy")
    assert "{resource_prefix()}/{owner_admin or '*'}/*" in pol
    trust = _func_src("agent_role_trust_policy")
    assert '"sts:ExternalId": owner_admin' in trust and "aws:PrincipalArn" in trust


def test_owner_admin_is_set_at_creation_and_never_updatable():
    create = _func_src("create_space")
    assert 'space_data["owner_admin"] = user_id' in create
    assert 'space_data["created_by"] = user_id' in create
    sc = (BACKEND / "tools" / "aws" / "spaces_client.py").read_text(encoding="utf-8")
    allow = sc.split("allowed_fields = [", 1)[1].split("]", 1)[0]
    assert "owner_admin" not in allow and "created_by" not in allow, "neither identity field is writable after creation"


def test_space_is_managed_by_its_creator_or_a_super_admin():
    src = _func_src("_can_manage_space")
    body = src.split('"""')[-1]
    assert "if not space_id:\n        return False" in body, "no space, no management"
    assert "return _created_by_caller(auth_header, space_id) or is_admin(auth_header)" in body, \
        "the creator or a super admin manages; nobody else"
    assert "space_group_name" not in body and "get_user_groups" not in body and "is_space_admin" not in body, \
        "group membership grants use only, never management"
    auth_src = (BACKEND / "utils" / "auth.py").read_text(encoding="utf-8")
    member = auth_src.split("def is_space_member(", 1)[1].split("\ndef ", 1)[0]
    assert '"admins" in groups' in member, "viewing every space stays with the super admin (is_space_member admits them)"
    for fn in ("configure_space_database", "batch_configure_databases"):
        assert "not is_admin(auth" not in _func_src(fn), f"{fn}: no super-admin arbitration inside a space"


def test_onboarding_secret_reads_run_as_the_spaces_own_role_never_the_task_role():
    # The task role can read every <prefix>/* secret in the deployment account, so a read
    # through it proves nothing. Every onboarding-path read goes through _space_session.
    for fn in ("configure_space_database", "test_database_credentials", "_configure_single_db"):
        src = _func_src(fn)
        assert "_space_session(space, account_id, region)" in src, fn
        assert "boto3.Session(region_name=region)" not in src, f"{fn} must not fall back to the task role"
    sess = _func_src("_space_session")
    assert "ExternalId=space_id" in sess and 'space.get("iam_role_arn"' in sess or '"iam_role_arn"' in sess
    assert "_get_cross_account_session(" in sess
    # The connectivity check in the background task reads the secret as the space's own
    # role, not through pg_client's task-role read.
    assert "creds = get_db_credentials(secret_arn)" not in _func_src("_configure_single_db")


def test_admin_role_policy_is_pinned_to_the_owner_admins_secret_namespace():
    pol = _func_src("agent_space_permissions_policy")
    assert "{resource_prefix()}/{owner_admin or '*'}/*" in pol
    cs = _func_src("create_space")
    assert "agent_space_permissions_policy(account_id, user_id)" in cs
    assert "agent_role_trust_policy(account_id, user_id)" in cs, "the deployment-account role requires the owner admin's id as ExternalId"
    assert "owner_admin_of(sp) == user_id" in cs, "an admin's existing role and account bindings are reused for a new space"
    assert "def _space_secret_prefix" in APP_SRC and "secret_namespace(owner_admin_of(space))" in _func_src("_space_secret_prefix")


# ── first-onboard race: promote only on the secret this task logged in with ────

def test_first_onboard_passes_the_submitted_secret_and_rechecks_before_promoting():
    cfg = _func_src("configure_space_database")
    assert 'submitted_secret_arn=secret_arn if mode == "first_onboard" else ""' in cfg
    task = _func_src("_configure_single_db")
    assert "submitted_secret_arn" in task.split(")", 1)[0], "the task takes the submitted secret"
    assert 'secret_arn = submitted_secret_arn or instance.get("secret_arn", "")' in task
    tail = task.split("if connected:", 1)[1]
    assert 'fresh = db_registry.get_instance(instance_id)' in tail
    assert 'fresh.get("reach_space_id") != space_id or fresh.get("secret_arn") != secret_arn' in tail
    assert tail.index('fresh.get("reach_space_id")') < tail.index("promote_candidate("), \
        "the re-check precedes the membership write"
    assert "add_candidate(instance_id, space_id)" in tail, "a lost race returns the space to a candidate"


# ── every member holds its own proof; no grants, no lending ─────────────────────

def test_tenancy_resolvers_every_member_holds_its_own_proof():
    from tools.aws.tenancy import credential_arn_for, spaces_entitled_to_secret, record_holds_secret
    import tools.aws.tenancy as tenancy
    # Admin A's two spaces share one ARN; admin B's space holds another. No grants exist.
    rec = {"status": "configured", "reach_space_id": SPACE_A, "secret_arn": "arn:a",
           "member_space_ids": {SPACE_A, "space-a2", SPACE_B},
           "space_secrets": {SPACE_A: "arn:a", "space-a2": "arn:a", SPACE_B: "arn:b"}}
    assert credential_arn_for(rec, SPACE_A) == "arn:a" and credential_arn_for(rec, "space-a2") == "arn:a"
    assert credential_arn_for(rec, SPACE_B) == "arn:b"
    assert credential_arn_for(rec, "space-x") == "", "a non-member gets no credential"
    assert spaces_entitled_to_secret(rec, "arn:a") == {SPACE_A, "space-a2"}
    assert spaces_entitled_to_secret(rec, "arn:b") == {SPACE_B}
    assert record_holds_secret(rec, "arn:b") and not record_holds_secret(rec, "arn:zzz")
    for gone in ("record_owner_space_ids", "record_granted_by", "credential_space_for"):
        assert not hasattr(tenancy, gone), f"{gone} belongs to the lending model and is gone"
    assert "granted_by" not in APP_SRC and "regrant" not in APP_SRC and "share_database" not in APP_SRC


def test_credential_handle_resolves_per_space_and_refuses_raw_arns():
    # The scope block carries the database alias; db_aliases turns it into the registry id
    # and pg_client resolves that for the calling space.
    pg = (BACKEND / "tools" / "aws" / "pg_client.py").read_text(encoding="utf-8")
    assert "def resolve_credential(handle: str) -> str:" in pg
    assert "arn = credential_arn_for(record, space)" in pg
    assert 'value.startswith("arn:")' in pg and "secret ARNs are not accepted" in pg
    body = pg.split("def get_connection(", 1)[1].split("\ndef ", 1)[0]
    assert "handle = (resolve_handle(secret_arn)" in body and "secret_arn = resolve_credential(handle)" in body
    body = pg.split("def lookup_record_database_name(", 1)[1].split("\ndef ", 1)[0]
    assert "secret_arn = resolve_credential(secret_arn)" in body
    body = pg.split("def resolve_credential(", 1)[1].split("\ndef ", 1)[0]
    assert "value = (resolve_handle(handle)" in body, "the alias is translated before anything else"
    # Resolved exactly once per connection: test_connection hands the handle on to
    # get_connection, and pg_client's own Secrets Manager session says the ARN is already
    # resolved. The behaviour is pinned end to end in test_credential_resolve_once.py.
    body = pg.split("def test_connection(", 1)[1].split("\ndef ", 1)[0]
    assert "resolve_credential(" not in body.split('"""', 2)[2], "test_connection must not resolve twice"
    assert "get_session(secret_arn=secret_arn, credential_resolved=True)" in pg
    sf = (BACKEND / "tools" / "aws" / "session_factory.py").read_text(encoding="utf-8")
    assert "secret_arn = resolve_credential(secret_arn)" in sf
    assert "if secret_arn and not credential_resolved:" in sf
    scope = (BACKEND / "surfaces" / "scope.py").read_text(encoding="utf-8")
    assert 'f"secret_arn={alias}"' in scope and "secret_arn={alias}, " in scope


def test_secret_guard_and_session_resolve_per_space_secrets():
    pg = (BACKEND / "tools" / "aws" / "pg_client.py").read_text(encoding="utf-8")
    assert "spaces_entitled_to_secret(rec, secret_arn)" in pg, "entitlement is by owner/grant, not plain membership"
    assert pg.count('FilterExpression="#sa = :sa"') == 0, "no lookup keys on the active secret alone"
    sf = (BACKEND / "tools" / "aws" / "session_factory.py").read_text(encoding="utf-8")
    assert "record_holds_secret(i, secret_arn)" in sf
    assert 'for sid, arn in (db_record.get("space_secrets") or {}).items():' in sf, \
        "the assumed role is the owner of the credential in use"


# ── onboarding gate admits candidates; everything else stays members-only ────

def test_onboard_gate_admits_member_or_candidate_only():
    src = _func_src("_verify_instance_onboardable")
    assert "_instance_candidate_spaces" in src and "_instance_member_spaces" in src
    assert "status_code=404" in src, "an unrelated instance gets the opaque 404"
    for handler in ("test_database_credentials", "configure_space_database"):
        assert "_verify_instance_onboardable(" in _func_src(handler), f"{handler} must use the onboard gate"


def test_non_onboarding_endpoints_keep_the_members_only_gate():
    # Batch configure re-provisions with stored secrets: members only.
    assert "_verify_instance_in_space(" in _func_src("batch_configure_databases")
    assert "_verify_instance_onboardable(" not in _func_src("batch_configure_databases")


def test_join_path_never_rewrites_active_credential_or_reach():
    cfg = _func_src("configure_space_database")
    join = cfg.split('if mode == "join":', 1)[1].split("# ── FIRST ONBOARD", 1)[0]
    assert "set_reach_space(" not in join, "joining must not change the reach provider"
    assert '"secret_arn"' not in join and "update_instance(" not in join, \
        "joining must not rewrite the active secret or the record's network/enrichment fields"
    assert "promote_candidate(" in join


def test_first_onboard_sets_reach_provider_with_its_secret():
    cfg = _func_src("configure_space_database")
    first = cfg.split('if mode == "first_onboard":', 1)[1]
    assert "set_reach_space(instance_id, space_id, secret_arn=secret_arn," in first


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("FAIL", fn.__name__, "-", e)
    print("\n%d passed, %d failed" % (len(fns) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
