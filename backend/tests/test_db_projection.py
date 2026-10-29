# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Databases-listing field projection for utils.db_projection.

An operator's token reaches GET /api/databases, so the raw registry record (secret ARN,
full cross-space membership) must not be returned to a non-manager. A manager (super
admin / space admin of a member space) still gets the full record. The database's own
placement is deliberately preserved for operators.

Run:  python3 backend/tests/test_db_projection.py   (or via pytest)
"""
import os
import pathlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.db_projection import project_db_for_operator, project_db_for_manager, OPERATOR_DB_FIELDS, MANAGER_EXTRA_FIELDS  # noqa: E402

RECORD = {
    "instance_id": "i-abc",
    "display_name": "orders-prod",
    "engine": "aurora-postgresql",
    "region": "us-east-1",
    "status": "configured",
    "last_health_score": "healthy",
    "database_name": "postgres",
    # DB's own placement — kept for operators (the topology view shows it anyway)
    "account_id": "123456789012",
    "endpoint": "orders.cluster-xyz.us-east-1.rds.amazonaws.com",
    "vpc_id": "vpc-0a1b2c3d",
    "subnet_ids": "subnet-1,subnet-2",
    "security_group_ids": "sg-123",
    # sensitive / tenancy
    "secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:db-ro-AbCdEf",
    "reach_space_id": "space-mine",
    "space_secrets": {
        "space-mine": "arn:aws:secretsmanager:us-east-1:123456789012:secret:db-ro-AbCdEf",
        "space-other-tenant": "arn:aws:secretsmanager:us-east-1:999999999999:secret:db-ro-ZzYyXx",
    },
    "member_space_ids": ["space-mine", "space-other-tenant"],
    "candidate_space_ids": ["space-third-tenant"],
    "tags": {"space_id": "space-mine"},
}


def test_operator_never_receives_the_secret_arn():
    out = project_db_for_operator(RECORD, {"space-mine"})
    assert "secret_arn" not in out
    assert "AbCdEf" not in str(out)


def test_operator_never_receives_any_space_secret():
    # space_secrets is the per-space verified-secret map added with onboarding proof: a
    # second credential pointer, so it goes the way of secret_arn for a non-manager.
    out = project_db_for_operator(RECORD, {"space-mine"})
    assert "space_secrets" not in out
    assert "ZzYyXx" not in str(out) and "AbCdEf" not in str(out)


def test_operator_shape_is_an_allowlist_with_no_tenancy_or_network_internals():
    from utils.db_projection import OPERATOR_DB_FIELDS
    rec = dict(RECORD, reach_space_id="space-other-tenant",
               vpc_id="vpc-1", subnet_ids="s-1", security_group_ids="sg-1", network_method="peering",
               network_peering_id="pcx-1", config_log=["x"], config_error="boom", arn="arn:aws:rds:...",
               tags={"space_id": "space-other-tenant", "engine_version": "16.4", "raw_identifier": "r"})
    out = project_db_for_operator(rec, {"space-mine"})
    for leaked in ("candidate_space_ids", "reach_space_id", "granted_by", "space_secrets", "secret_arn",
                   "network_method", "network_peering_id", "config_log", "config_error", "arn"):
        assert leaked not in out, leaked
    assert "space-other-tenant" not in str(out) and "space-third-tenant" not in str(out)
    assert out["tags"] == {"engine_version": "16.4"}, "only display tags survive"
    assert set(out) - {"tags", "member_space_ids"} <= set(OPERATOR_DB_FIELDS)


def test_manager_keeps_active_secret_only_as_the_reach_provider():
    # A space admin of the reach-provider space gets the active secret_arn (their space
    # supplied it) and the full membership list, but only their own spaces' entries of
    # space_secrets: another tenant's verified secret ARN is that tenant's handle.
    out = project_db_for_manager(RECORD, {"space-mine"})
    assert out["secret_arn"] == RECORD["secret_arn"]
    assert out["member_space_ids"] == ["space-mine"], "only the caller's own space ids"
    assert out["member_count"] == len(RECORD["member_space_ids"]), "but the total count, for 'shared with N'"
    assert out["space_secrets"] == {"space-mine": RECORD["space_secrets"]["space-mine"]}
    assert "ZzYyXx" not in str(out) and "space-other-tenant" not in str(out)


def test_co_member_admin_never_receives_the_other_tenants_active_secret():
    # The admin of a space that JOINED the database is not the reach provider: the active
    # secret belongs to the providing tenant and must not reach them, only their own proof.
    out = project_db_for_manager(RECORD, {"space-other-tenant"})
    assert "secret_arn" not in out and "AbCdEf" not in str(out)
    assert "reach_space_id" not in out, "the reach provider is another tenant's identity"
    assert out["space_secrets"] == {"space-other-tenant": RECORD["space_secrets"]["space-other-tenant"]}
    assert out["member_space_ids"] == ["space-other-tenant"] and "space-mine" not in str(out)


def test_manager_shape_is_an_allowlist_too():
    # A field added to the record later must not reach a co-member admin by default, and
    # nothing in the shape names another tenant.
    rec = dict(RECORD, reach_space_id="space-mine", candidate_space_ids={"space-third-tenant", "space-other-tenant"},
               config_log=["Network configuration starting..."], network_method="peering",
               brand_new_field="leaks?", tags={"engine_version": "16.4", "raw_identifier": "r"})
    out = project_db_for_manager(rec, {"space-other-tenant"})
    assert "brand_new_field" not in out
    assert out["candidate_space_ids"] == ["space-other-tenant"] and "space-third-tenant" not in str(out)
    assert "space-mine" not in str(out), "neither as member nor as reach provider"
    assert out["config_log"] == ["Network configuration starting..."] and out["network_method"] == "peering"
    assert set(out) - {"tags", "member_space_ids", "member_count", "candidate_space_ids", "space_secrets"} \
        <= set(OPERATOR_DB_FIELDS) | set(MANAGER_EXTRA_FIELDS)


def test_member_and_route_fields_reach_the_right_shapes():
    rec = dict(RECORD, writer_instance_id="db-a", members_changed_at="2030-01-01T00:00:00+00:00",
               network_route_mode="host", host_routes=[{"ip": "10.9.0.5", "host": "db-a.example"}],
               db_vpc_cidr="10.9.0.0/16")
    op = project_db_for_operator(rec, {"space-mine"})
    assert op["writer_instance_id"] == "db-a" and op["members_changed_at"].startswith("2030")
    assert "network_route_mode" not in op and "host_routes" not in op and "db_vpc_cidr" not in op
    mgr = project_db_for_manager(rec, {"space-mine"})
    assert mgr["network_route_mode"] == "host" and mgr["host_routes"][0]["ip"] == "10.9.0.5"
    assert mgr["writer_instance_id"] == "db-a"
    assert "db_vpc_cidr" not in mgr, "the DB VPC CIDR stays super-admin only"
def test_membership_is_narrowed_to_the_callers_spaces():
    out = project_db_for_operator(RECORD, {"space-mine"})
    # the other tenant's space id is not disclosed
    assert out["member_space_ids"] == ["space-mine"]
    assert "space-other-tenant" not in str(out)


def test_db_placement_is_preserved_for_the_operator():
    # The operator is a DBA; the database's own location is what they operate on, and the
    # topology view surfaces it regardless. It must survive the projection.
    out = project_db_for_operator(RECORD, {"space-mine"})
    for field in ("account_id", "endpoint", "vpc_id", "subnet_ids", "security_group_ids"):
        assert out[field] == RECORD[field], field
    # ordinary display fields are untouched too
    assert out["status"] == "configured"
    assert out["display_name"] == "orders-prod"
    assert "space_id" not in (out.get("tags") or {}), "the frozen space stamp is not an operator field"


def test_placement_stays_in_the_operator_shape_and_pointers_out_of_it():
    # Guard the intent: placement is operator-visible; credential pointers and the reach
    # provider are never in the operator allowlist.
    for f in ("account_id", "endpoint", "vpc_id", "subnet_ids", "security_group_ids"):
        assert f in OPERATOR_DB_FIELDS
    for f in ("secret_arn", "space_secrets", "reach_space_id", "candidate_space_ids"):
        assert f not in OPERATOR_DB_FIELDS and f not in MANAGER_EXTRA_FIELDS


def test_projection_does_not_mutate_the_input():
    snapshot = dict(RECORD)
    project_db_for_operator(RECORD, {"space-mine"})
    assert RECORD == snapshot, "projection must return a copy, not mutate the record"


def _app_src():
    return (pathlib.Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")


def test_list_databases_applies_the_projection_only_to_non_managers():
    src = _app_src()
    # super admins get the full record (fast path), flagged when a removal OR an onboard stalled
    assert ("if is_admin(auth_header):\n        return [_flag_stale_configuring("
            "_flag_stalled_teardown(inst), inst) for inst in instances]") in src
    assert 'return {**out, "teardown_stalled": True} if teardown_stalled(record or out) else out' in src
    # a manager of a member space gets the manager projection; everyone else the operator one
    assert "manageable = {sid for sid in member_spaces if _can_manage_space(auth_header, sid)}" in src
    assert "project_db_for_manager(inst, manageable)" in src
    assert "project_db_for_operator(inst, accessible)" in src


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
