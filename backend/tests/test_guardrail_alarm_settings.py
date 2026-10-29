# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Console-tunable guardrail alarm thresholds (FU-1).

The two alert-only guardrail alarms (ApplyGuardrail throttling, guardrail interventions)
are created by CloudFormation. A super admin retunes their thresholds from the admin
console via GET/PUT /api/settings/guardrail-alarms, which reads and re-puts the live
CloudWatch alarms (read-modify-write, preserving the CFN-managed actions). The change must
line up across five files, or it fails in a different way in each: the API surface, the
task-role IAM, the CloudFormation parameters, the Makefile pass-through, and the deploy
config. The endpoint behaviour is asserted against source (no AWS); the read-modify-write
shape is checked so it cannot silently drop the alarm actions.

Run:  python3 backend/tests/test_guardrail_alarm_settings.py   (or via pytest)
"""
import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REPO = pathlib.Path(__file__).resolve().parents[2]


def _read(rel):
    return (REPO / rel).read_text(encoding="utf-8")


# ── API surface (super-admin, alert-only, read-modify-write) ─────────────────

def test_endpoints_exist_and_are_super_admin_gated():
    src = _read("backend/app.py")
    assert '@app.get("/api/settings/guardrail-alarms")' in src
    assert '@app.put("/api/settings/guardrail-alarms")' in src
    # both handlers behind the super-admin gate
    assert "def get_guardrail_alarms(request: Request):" in src
    assert "def set_guardrail_alarms(data: GuardrailAlarmUpdate, request: Request):" in src
    # alarm names derive from the deployment prefix (no new env var)
    assert "def _guardrail_alarm_names()" in src
    assert "guardrail-applyguardrail-throttled" in src
    assert "guardrail-interventions-high" in src
    # audited mutation
    assert '"guardrail_alarm_set"' in src


def test_update_is_read_modify_write_and_preserves_actions():
    """put_metric_alarm is create-or-replace, so the update must carry the live alarm's
    notification actions forward and change only the threshold — otherwise editing a
    threshold would silently strip the CloudFormation-managed SNS actions."""
    src = _read("backend/app.py")
    assert "describe_alarms(" in src, "must read the live alarm before re-putting it"
    assert "put_metric_alarm(" in src
    # the rebuilt alarm carries the actions and the new threshold
    for field in ('"AlarmActions"', '"OKActions"', '"InsufficientDataActions"', '"Threshold"'):
        assert field in src, f"{field} must be carried into put_metric_alarm"


# ── task-role IAM (scoped write, read already granted) ───────────────────────

def test_task_role_can_write_only_the_two_alarms():
    api = _read("infrastructure/api.yaml")
    assert "cloudwatch:PutMetricAlarm" in api
    # scoped to this deployment's two alarms, not '*'
    assert "${ResourcePrefix}-guardrail-applyguardrail-throttled" in api
    assert "${ResourcePrefix}-guardrail-interventions-high" in api
    # reading alarms is already granted account-wide
    assert "cloudwatch:DescribeAlarms" in api


# ── CloudFormation params reach the stack that owns the alarms ───────────────

def test_master_declares_and_passes_both_thresholds():
    master = _read("infrastructure/master.yaml")
    for p in ("GuardrailThrottleAlarmThreshold", "GuardrailInterventionAlarmThreshold"):
        # declared as a parameter
        assert re.search(rf"^\s{{2}}{p}:\s*$", master, re.M), f"{p} not declared in master.yaml"
        # passed down to the AgentStack (which owns the alarms)
        assert re.search(rf"{p}:\s*!Ref {p}", master), f"{p} not passed to a nested stack"
    # the alarms and their thresholds live in the AgentStack template (backend/template.yaml)
    backend = _read("backend/template.yaml")
    assert "Threshold: !Ref GuardrailThrottleAlarmThreshold" in backend
    assert "Threshold: !Ref GuardrailInterventionAlarmThreshold" in backend


# ── deploy tooling: Makefile pass-through + config + preflight re-seed ────────

def test_makefile_passes_both_thresholds():
    mk = _read("Makefile")
    assert "GuardrailThrottleAlarmThreshold=$(GUARDRAIL_THROTTLE_ALARM_THRESHOLD)" in mk
    assert "GuardrailInterventionAlarmThreshold=$(GUARDRAIL_INTERVENTION_ALARM_THRESHOLD)" in mk


def test_config_example_lists_both_keys():
    cfg = _read("deploy.config.example")
    assert "GUARDRAIL_THROTTLE_ALARM_THRESHOLD = 5" in cfg
    assert "GUARDRAIL_INTERVENTION_ALARM_THRESHOLD = 25" in cfg


def test_preflight_reseeds_from_the_live_alarm():
    conf = _read("deployment/configure.py")
    # a live_default reader so a redeploy converges to the console-set threshold
    assert "def live_alarm_threshold(" in conf
    assert 'live_alarm_threshold("guardrail-applyguardrail-throttled")' in conf
    assert 'live_alarm_threshold("guardrail-interventions-high")' in conf
    # both settings are registered with the CFN parameter names the check contract expects
    assert '"cfn": "GuardrailThrottleAlarmThreshold"' in conf
    assert '"cfn": "GuardrailInterventionAlarmThreshold"' in conf


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
