# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The security-posture endpoint must not claim a control it has not checked.

Most of the thirteen rows describe architectural facts and are static by nature. `mfa` is
different: `cognito.yaml` declares `MfaConfiguration: 'ON'`, but `UpdateUserPool` resets
any writable field a caller omits, so the pool's MFA state can change without anyone
intending it. A static "enabled" there would be right on a fresh deploy and keep looking
right afterwards regardless — the worst shape a posture row can take, because a silently
failed control and a dashboard vouching for it compound.

Two properties are pinned here:

  * The mfa row is derived from the live pool, and an unreadable pool reports "unknown"
    rather than "enabled" — a reassuring default is the same failure, quieter.
  * Every row declares a `basis`: "measured" when the status came from the live resource
    or this task's configuration, "declared" when it is a property of the architecture
    that nothing verifies at runtime. A reviewer should be able to tell evidence from
    assertion without reading the source.

Source-level, so it stays hermetic and fails on the shape rather than on mocked AWS.

Run:  python3 backend/tests/test_security_posture_basis.py   (or via pytest)
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")

VALID_BASIS = ("measured", "declared")


def _read():
    with open(APP, "r", encoding="utf-8") as f:
        return f.read()


def _controls_block():
    src = _read()
    m = re.search(r"\n    controls = \[(.*?)\n    \]\n", src, re.S)
    assert m, "could not locate the controls list in get_security_posture()"
    return m.group(1)


def _rows():
    """[(key, row_text)] for each control."""
    body = _controls_block()
    parts = re.split(r'\n        \{"key": ', body)[1:]
    out = []
    for p in parts:
        key = re.match(r'"([a-z_0-9]+)"', p).group(1)
        out.append((key, p))
    return out


def test_every_control_declares_a_basis():
    missing = [k for k, txt in _rows() if '"basis"' not in txt]
    assert not missing, f"controls without a basis: {missing}"


def test_basis_values_are_valid():
    bad = []
    for k, txt in _rows():
        m = re.search(r'"basis": "([a-z]+)"', txt)
        if m and m.group(1) not in VALID_BASIS:
            bad.append((k, m.group(1)))
    assert not bad, f"invalid basis values: {bad}"


def test_mfa_is_measured_not_asserted():
    rows = dict(_rows())
    assert "mfa" in rows, "the mfa control disappeared"
    mfa = rows["mfa"]
    assert '"status": "enabled"' not in mfa, \
        "the mfa row must not hardcode enabled — it was provably false after an " \
        "email-sender apply"
    assert "mfa_state" in mfa, "the mfa row should take its status from the live pool read"
    assert '"basis": "measured"' in mfa


def test_the_pool_read_exists_and_fails_to_unknown():
    src = _read()
    m = re.search(r"def _describe_pool_mfa\(\).*?(?=\n@app\.|\ndef )", src, re.S)
    assert m, "_describe_pool_mfa is missing"
    body = m.group(0)
    assert "get_user_pool_mfa_config" in body, "should read the pool's live MFA config"
    assert '"unknown"' in body, \
        "an unreadable pool must report unknown, never enabled"
    # The failure path must not fall back to a reassuring answer.
    except_block = body.split("except Exception")[-1]
    assert '"enabled"' not in except_block, \
        "the failure path must not claim the control is enabled"


def test_off_state_points_at_the_likely_cause():
    # If MFA is off while cognito.yaml declares ON, something reset it. Saying so turns a
    # posture row into a lead.
    src = _read()
    m = re.search(r"def _describe_pool_mfa\(\).*?(?=\n@app\.|\ndef )", src, re.S)
    body = m.group(0)
    assert "UpdateUserPool" in body, \
        "the off state should name UpdateUserPool as the thing that resets it"


def _describe_audit_vault_src():
    m = re.search(r"def _describe_audit_vault\(\).*?(?=\n@app\.|\ndef )", _read(), re.S)
    assert m, "_describe_audit_vault is missing"
    return m.group(0)


def test_audit_logging_is_measured():
    # The audit log row used to declare "enabled" for a DynamoDB trail anyone with the
    # task role could edit. It now reports the vault switch and the bucket's lock as read.
    rows = dict(_rows())
    row = rows.get("audit_logging")
    assert row, "the audit_logging control disappeared"
    assert '"basis": "measured"' in row and '"category": "configurable"' in row
    assert '"status": audit_state["status"]' in row, "the status comes from the live read"
    assert '"status": "enabled"' not in row, "the row must not hardcode enabled"
    assert '"parameter": "AuditVault / AuditRetentionDays"' in row
    assert '"label": "Immutable admin audit log"' in row
    body = _describe_audit_vault_src()
    statuses = set(re.findall(r'"status": "([a-z_]+)"', body))
    assert statuses == {"enabled", "off", "unknown"}, f"exactly three states, no partial: {statuses}"
    assert "partial" not in body
    assert "get_object_lock_configuration" in body and '"COMPLIANCE"' in body, \
        "enabled requires the bucket's lock to be read and to be COMPLIANCE"
    assert ("Audit log: index only, not tamper-proof. A super admin can switch the vault "
            "on in Settings → Audit.") in re.sub(r'"\s*\n\s*"', "", body), "verbatim off detail"
    except_block = body.split("except Exception")[-1]
    assert '"unknown"' in except_block and '"enabled"' not in except_block, \
        "a read error reports unknown, never enabled"
    empty = body.split("if not bucket:", 1)[1].split("try:", 1)[0]
    assert '"unknown"' in empty and "logger.error" in empty, "an empty AUDIT_BUCKET is unknown, logged at ERROR"


def test_audit_vault_helper_behaves_as_stated():
    # Run the shipped helper against fakes: off, enabled, a non-COMPLIANCE lock, a read
    # error, and an empty bucket.
    import types
    src = _describe_audit_vault_src()
    state = {"vault": True, "lock": {"ObjectLockEnabled": "Enabled",
                                     "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE"}}},
             "fail": False, "logged": []}

    class S3:
        def get_object_lock_configuration(self, Bucket):
            if state["fail"]:
                raise RuntimeError("AccessDenied")
            return {"ObjectLockConfiguration": state["lock"]}

    al = types.SimpleNamespace(
        AUDIT_BUCKET="acme-vault",
        current_settings=lambda: {"vault_enabled": state["vault"], "effective_retention": 400})
    log = types.SimpleNamespace(error=lambda *a, **k: state["logged"].append("ERROR"),
                                warning=lambda *a, **k: state["logged"].append("WARNING"))
    ns = {"audit_log": al, "logger": log, "os": os,
          "boto3": types.SimpleNamespace(client=lambda *a, **k: S3())}
    exec(src, ns)  # noqa: S102 - pinning shipped source
    f = ns["_describe_audit_vault"]
    out = f()
    assert out["status"] == "enabled" and "acme-vault" in out["detail"] and "400 days" in out["detail"]
    state["lock"] = {"ObjectLockEnabled": "Enabled", "Rule": {"DefaultRetention": {"Mode": "GOVERNANCE"}}}
    assert f()["status"] == "unknown", "a GOVERNANCE lock is not reported as enabled"
    state["lock"] = {}
    assert f()["status"] == "unknown"
    state["fail"] = True
    assert f()["status"] == "unknown" and state["logged"][-1] == "WARNING"
    state["vault"] = False
    assert f()["status"] == "off", "the switch alone decides off; no lock read is needed"
    state["fail"] = False
    out = f()
    assert out["status"] == "off" and out["detail"].startswith("Audit log: index only, not tamper-proof.")
    al.AUDIT_BUCKET = ""
    assert f()["status"] == "unknown" and state["logged"][-1] == "ERROR"


def test_readonly_db_access_admits_it_is_unverified():
    rows = dict(_rows())
    detail = rows["readonly_db_access"]
    assert '"basis": "declared"' in detail
    assert "not measured" in detail or "nothing here verifies" in detail, \
        "this row asserts a property of an operator-provisioned database role that no " \
        "code checks; it should say so"


def test_guardrail_row_does_not_overclaim_tool_results():
    # A Bedrock guardrail evaluates the prompt and the model output. Claiming it covers
    # everything reaching the model would misdescribe tool-sourced text.
    rows = dict(_rows())
    detail = rows["prompt_guardrail"]
    assert "tool result" in detail, \
        "the guardrail row should be explicit that tool-result text is fenced separately"


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
                passed += 1
            except AssertionError as e:
                print(f"  FAIL {name}: {e}")
                failed += 1
            except Exception as e:
                print(f"  ERR  {name}: {type(e).__name__}: {e}")
                failed += 1
    print(f"pass={passed} fail={failed}")
    sys.exit(1 if failed else 0)
