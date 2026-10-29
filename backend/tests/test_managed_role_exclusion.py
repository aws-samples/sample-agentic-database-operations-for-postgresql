# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""CHK-042 excludes AWS-managed roles from the superuser count and risk flag.

rdsadmin and the other RDS-managed service roles are superusers a customer cannot touch
(RDS reserves the names). Flagging them as high-risk was noise that inflated the superuser
count toward its threshold and buried the customer's own superusers. The audit now skips
managed roles for both the count and the per-role risk flag, marks them managed_role for
transparency, and still flags customer superusers and RLS-bypassing login roles. Nothing is
removed from the database, only from the findings.

Hermetic: strands/boto3 stubbed, pg_client faked and routed by query so only the role rows
are controlled (every other audit query returns empty).

Run:  python3 backend/tests/test_managed_role_exclusion.py   (or via pytest)
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_strands = types.ModuleType("strands")
_strands.tool = lambda f: f
sys.modules.setdefault("strands", _strands)
sys.modules.setdefault("boto3", types.ModuleType("boto3"))

_ROLES = {"rows": []}

_fake_pg = types.ModuleType("tools.aws.pg_client")
def _routed(secret_arn, query, params=None, database=None):
    return [dict(r) for r in _ROLES["rows"]] if "from pg_roles" in query.lower() else []
_fake_pg.execute_query = _routed
_fake_pg.test_connection = lambda *a, **k: {"ok": True}
_fake_pg.lookup_record_database_name = lambda *a, **k: ""
_fake_pg.SECRET_NOT_IN_SPACE_MESSAGE = "not available in the current space"
sys.modules["tools.aws.pg_client"] = _fake_pg

from tools import db_tools as dbt  # noqa: E402


def _role(name, super_=False, login=False, bypassrls=False):
    return {"role_name": name, "is_superuser": super_, "can_login": login, "bypasses_rls": bypassrls}


def _audit(rows):
    _ROLES["rows"] = rows
    out = dbt.get_security_audit("db-1")
    return out, {r["role_name"]: r for r in out["roles"]}


def test_managed_superuser_is_not_flagged():
    out, by = _audit([_role("rdsadmin", super_=True, login=True, bypassrls=True)])
    assert by["rdsadmin"]["managed_role"] is True
    assert by["rdsadmin"]["risk"] == "", "a managed role must carry no risk flag"
    assert out["roles_status"] == "OK", "rdsadmin alone is not a finding"


def test_customer_superuser_is_still_flagged():
    out, by = _audit([_role("app_admin", super_=True, login=True, bypassrls=True)])
    assert by["app_admin"]["managed_role"] is False
    assert by["app_admin"]["risk"] == "bypasses_rls+login"
    assert out["roles_status"] == "WARNING"


def test_managed_roles_do_not_count_toward_the_superuser_threshold():
    # Four managed superusers (> max_superusers=3) must NOT trip the count.
    rows = [_role(n, super_=True, login=True) for n in
            ("rdsadmin", "rdsproxyadmin", "rdsrepladmin", "rdswriteforwarduser")]
    out, by = _audit(rows)
    assert all(by[n]["managed_role"] for n in by)
    assert out["roles_status"] == "OK", "managed superusers are excluded from the count"


def test_customer_superusers_still_trip_the_threshold():
    # Four customer superusers (login off so no RLS-bypass risk) still exceed max_superusers.
    rows = [_role(f"svc{i}", super_=True, login=False) for i in range(4)]
    out, _ = _audit(rows)
    assert out["roles_status"] == "WARNING", "customer superusers over the threshold still warn"


def test_mixed_managed_and_customer():
    rows = [_role("rdsadmin", super_=True, login=True, bypassrls=True),
            _role("reporting", super_=True, login=False),
            _role("readonly", super_=False, login=True)]
    out, by = _audit(rows)
    assert by["rdsadmin"]["managed_role"] is True and by["rdsadmin"]["risk"] == ""
    assert by["reporting"]["managed_role"] is False
    # One customer superuser, no RLS-bypass login role => under threshold, no risk => OK.
    assert out["roles_status"] == "OK"



# ── Batch scan: the deterministic findings engine skips the same managed roles ──────────
# The scan's role rows label every superuser HIGH; the engine must not turn rdsadmin (or any
# role in references.yaml#role-audit managed_roles) into a "High-risk role" finding, while a
# customer superuser still gets one.

def _batch_findings(roles):
    import importlib.util
    from pathlib import Path
    utils = Path(__file__).resolve().parents[2] / "batch" / "utils"
    sys.path.insert(0, str(utils))
    try:
        spec = importlib.util.spec_from_file_location("findings_engine_under_test", utils / "findings_engine.py")
        fe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fe)
    finally:
        sys.path.remove(str(utils))
    _, _, findings = fe.derive_findings({"security": {"roles": roles}})
    return [f["title"] for f in findings if f["category"] == "Security"]


def test_batch_findings_skip_managed_roles():
    titles = _batch_findings([
        {"name": "rdsadmin", "is_superuser": True, "can_login": True, "risk": "HIGH"},
        {"name": "rdswriteforwarduser", "is_superuser": True, "can_login": True, "risk": "HIGH"},
        {"name": "RDSADMIN", "is_superuser": True, "can_login": True, "risk": "HIGH"},
    ])
    assert titles == [], f"managed roles are never a role-risk finding ({titles})"


def test_batch_findings_still_flag_customer_superusers():
    titles = _batch_findings([
        {"name": "rdsadmin", "is_superuser": True, "can_login": True, "risk": "HIGH"},
        {"name": "app_admin", "is_superuser": True, "can_login": True, "risk": "HIGH"},
    ])
    assert titles == ["High-risk role: app_admin"], titles


def test_batch_managed_list_matches_the_agent_audit():
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "batch" / "utils"))
    import thresholds
    refs = dbt._check_references().get("role-audit", {}).get("thresholds", {})
    assert thresholds.managed_roles() == {str(m).lower() for m in refs.get("managed_roles", [])}, \
        "batch and agent read one managed-roles list"

if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"  FAIL {name}: {e!r}")
                failed += 1
    print(f"pass={passed} fail={failed}")
    sys.exit(1 if failed else 0)
