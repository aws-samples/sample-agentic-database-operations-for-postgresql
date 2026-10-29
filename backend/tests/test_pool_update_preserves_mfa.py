# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""UpdateUserPool must never downgrade the pool's MFA configuration.

`cognito-idp:UpdateUserPool` REPLACES the pool configuration: any writable field the
caller omits reverts to its default. `MfaConfiguration` is writable and IS returned by
DescribeUserPool, so leaving it out of the re-passed set takes the pool from MFA required
to MFA OFF on every `apply_to_pool` — with no error, while
`infrastructure/cognito.yaml` declares `MfaConfiguration: 'ON'`.

Which FACTORS are enabled (TOTP) is NOT part of UpdateUserPool's surface at all — it is
owned by Get/SetUserPoolMfaConfig — so `apply_to_pool` round-trips that separately. A
fix that only adds `EnabledMfas` to the re-passed field list does nothing: it is not an
UpdateUserPool parameter and DescribeUserPool never returns it, so the existing
`val in (None, "")` guard skips it.

Hermetic: boto3 stubbed at import; no AWS.

Run:  python3 backend/tests/test_pool_update_preserves_mfa.py   (or via pytest)
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

POOL_ID = "us-east-1_TESTPOOL"
_MFA_ON = {
    "MfaConfiguration": "ON",
    "SoftwareTokenMfaConfiguration": {"Enabled": True},
}


class FakeCognito:
    """Records what apply_to_pool sends, and models the UpdateUserPool footgun."""

    def __init__(self, mfa=None, fail_get=False, fail_set=False, drop_mfa_on_update=True):
        self.pool = {
            "Id": POOL_ID,
            "Name": "immutable-do-not-send",
            "Policies": {"PasswordPolicy": {"MinimumLength": 12}},
            "AdminCreateUserConfig": {
                "AllowAdminCreateUserOnly": True,
                "UnusedAccountValidityDays": 7,
            },
            "MfaConfiguration": (mfa or {}).get("MfaConfiguration", "OFF"),
            "EmailConfiguration": {"EmailSendingAccount": "COGNITO_DEFAULT"},
        }
        self.mfa = dict(mfa or {})
        self.fail_get = fail_get
        self.fail_set = fail_set
        self.drop_mfa_on_update = drop_mfa_on_update
        self.update_kwargs = None
        self.set_mfa_calls = []

    def describe_user_pool(self, UserPoolId):
        return {"UserPool": dict(self.pool)}

    def update_user_pool(self, **kwargs):
        self.update_kwargs = kwargs
        # The real API resets anything writable that was not re-passed.
        if "MfaConfiguration" not in kwargs:
            self.pool["MfaConfiguration"] = "OFF"
            self.mfa = {"MfaConfiguration": "OFF"}
        elif self.drop_mfa_on_update:
            # Worst case: the scalar is preserved but the factor config is cleared.
            self.mfa = {"MfaConfiguration": kwargs["MfaConfiguration"]}
        return {}

    def get_user_pool_mfa_config(self, UserPoolId):
        if self.fail_get:
            raise RuntimeError("AccessDeniedException: GetUserPoolMfaConfig")
        return dict(self.mfa)

    def set_user_pool_mfa_config(self, UserPoolId, **kwargs):
        if self.fail_set:
            raise RuntimeError("AccessDeniedException: SetUserPoolMfaConfig")
        self.set_mfa_calls.append(kwargs)
        self.mfa = dict(kwargs)
        self.pool["MfaConfiguration"] = kwargs.get("MfaConfiguration", "OFF")
        return {}


sys.modules.setdefault("boto3", types.ModuleType("boto3"))

from tools.aws import email_sender as es  # noqa: E402

es.identity_arn = lambda addr: f"arn:aws:ses:us-east-1:111111111111:identity/{addr}"


def _apply(cog):
    es._cognito = lambda: cog
    es.apply_to_pool(POOL_ID, "noreply@example.com", "Ops")
    return cog


def test_mfa_configuration_is_repassed():
    assert "MfaConfiguration" in es._POOL_UPDATE_FIELDS, \
        "MfaConfiguration must be re-passed or UpdateUserPool resets it to OFF"


def test_enabled_mfas_is_not_in_the_field_list():
    # Not an UpdateUserPool parameter and never returned by DescribeUserPool, so
    # including it would be silently skipped and give false confidence.
    assert "EnabledMfas" not in es._POOL_UPDATE_FIELDS
    assert "SoftwareTokenMfaConfiguration" not in es._POOL_UPDATE_FIELDS


def test_verification_message_fields_stay_out():
    # They conflict with VerificationMessageTemplate, which IS re-passed.
    for f in ("EmailVerificationMessage", "EmailVerificationSubject", "SmsVerificationMessage"):
        assert f not in es._POOL_UPDATE_FIELDS, f


def test_apply_sends_mfa_configuration():
    cog = _apply(FakeCognito(mfa=_MFA_ON))
    assert cog.update_kwargs.get("MfaConfiguration") == "ON"


def test_apply_never_leaves_mfa_off():
    cog = _apply(FakeCognito(mfa=_MFA_ON))
    assert cog.pool["MfaConfiguration"] == "ON"
    assert cog.mfa.get("SoftwareTokenMfaConfiguration") == {"Enabled": True}, \
        "TOTP must still be an enabled factor after an email-sender apply"


def test_factor_config_is_reasserted_when_the_update_clears_it():
    cog = _apply(FakeCognito(mfa=_MFA_ON, drop_mfa_on_update=True))
    assert cog.set_mfa_calls, "expected SetUserPoolMfaConfig to restore the factor config"
    assert cog.set_mfa_calls[-1]["MfaConfiguration"] == "ON"


def test_no_write_when_the_update_preserved_everything():
    cog = _apply(FakeCognito(mfa=_MFA_ON, drop_mfa_on_update=False))
    assert not cog.set_mfa_calls, "no MFA write needed when the update preserved it"


def test_pool_without_mfa_is_left_alone():
    cog = _apply(FakeCognito(mfa={"MfaConfiguration": "OFF"}))
    assert not cog.set_mfa_calls


def test_apply_still_succeeds_without_mfa_permissions():
    # An existing installation whose task role predates Get/SetUserPoolMfaConfig must
    # still be able to configure its email sender.
    cog = _apply(FakeCognito(mfa=_MFA_ON, fail_get=True))
    assert cog.update_kwargs is not None
    cog = _apply(FakeCognito(mfa=_MFA_ON, fail_set=True))
    assert cog.update_kwargs is not None


def test_immutable_fields_are_not_sent():
    cog = _apply(FakeCognito(mfa=_MFA_ON))
    for forbidden in ("Id", "Name"):
        assert forbidden not in cog.update_kwargs, forbidden


def test_unused_account_validity_days_still_stripped():
    cog = _apply(FakeCognito(mfa=_MFA_ON))
    assert "UnusedAccountValidityDays" not in cog.update_kwargs["AdminCreateUserConfig"]


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
