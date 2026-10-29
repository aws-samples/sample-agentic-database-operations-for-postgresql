# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Account email sender (Amazon SES) for Cognito invite / verification emails.

Why this exists
---------------
A Cognito user pool sends the invitation email (temporary password) when an admin
invites a user. By default the pool uses ``COGNITO_DEFAULT`` — Cognito's built-in
mailer — which sends from an unaligned address (``no-reply@verificationemail.com``),
is capped at 50 emails/day, and gives ZERO delivery visibility (no bounce, no
metric). That is fine for the very first/bootstrap admin (created by the
CloudFormation custom resource) but not for onboarding real users. This module lets
an admin configure a verified SES identity so invitations for the *second user
onward* are delivered reliably and branded.

Ownership model (read before editing)
--------------------------------------
``EmailConfiguration`` is a POOL-WIDE Cognito setting and is owned HERE, at RUNTIME
— NOT in CloudFormation. ``infrastructure/cognito.yaml`` intentionally leaves the
pool on ``COGNITO_DEFAULT`` so the first admin can always be bootstrapped with zero
setup. When an admin saves a verified sender we switch the pool to ``DEVELOPER`` +
SES via ``apply_to_pool``. Because a later stack update could reset the pool back to
``COGNITO_DEFAULT`` (the template omits ``EmailConfiguration``), ``reconcile`` re-applies
the stored sender on demand — the status read and the invite path both call it, so
the pool self-heals.

UpdateUserPool footgun (the reason apply_to_pool is careful)
------------------------------------------------------------
``cognito-idp:UpdateUserPool`` REPLACES the pool configuration: any writable field
you omit reverts to its default. Naively calling ``update_user_pool(EmailConfiguration=...)``
would silently wipe the password policy, invite-only flag, and invite template.
``apply_to_pool`` therefore re-reads the pool with ``DescribeUserPool`` and re-passes
every existing writable field alongside the new ``EmailConfiguration``. It also strips
``AdminCreateUserConfig.UnusedAccountValidityDays`` (which conflicts with the
``TemporaryPasswordValidityDays`` that DescribeUserPool injects into the password
policy and would otherwise make the update fail).

All functions are fail-soft where a failure must not break a chat/admin turn;
``apply_to_pool`` is the exception — the caller surfaces its error to the admin.
"""

import logging
import os

import boto3

logger = logging.getLogger("email-sender")


def _region() -> str:
    return os.getenv("AWS_REGION", "us-east-1")


def _sesv2():
    return boto3.client("sesv2", region_name=_region())


def _cognito():
    return boto3.client("cognito-idp", region_name=_region())


_ACCOUNT_ID = None


def _account_id() -> str:
    global _ACCOUNT_ID
    if _ACCOUNT_ID is None:
        _ACCOUNT_ID = boto3.client("sts", region_name=_region()).get_caller_identity()["Account"]
    return _ACCOUNT_ID


def identity_arn(address: str) -> str:
    """SES identity ARN for an email-address identity in this account/region."""
    return f"arn:aws:ses:{_region()}:{_account_id()}:identity/{address}"


# ── SES state ────────────────────────────────────────────────────────────────
def identity_status(address: str) -> dict:
    """{'exists': bool, 'verified': bool} for an SES email identity (fail-soft)."""
    if not address:
        return {"exists": False, "verified": False}
    ses = _sesv2()
    try:
        r = ses.get_email_identity(EmailIdentity=address)
        return {"exists": True, "verified": bool(r.get("VerifiedForSendingStatus"))}
    except ses.exceptions.NotFoundException:
        return {"exists": False, "verified": False}
    except Exception as e:
        logger.warning(f"identity_status({address}) failed: {e}")
        return {"exists": False, "verified": False, "error": str(e)}


def verified_identities_map() -> dict:
    """address -> verified? for every SES EMAIL_ADDRESS identity in this account/region.

    ONE paginated ListEmailIdentities call, used to batch the per-user verification
    checks on user listings (replacing a GetEmailIdentity per pending user — the
    N+1 that made the Users tab slow).

    Returns None when the listing FAILS (e.g. the task role lacks
    ses:ListEmailIdentities) so callers can fall back to per-user
    GetEmailIdentity checks. Returning {} here instead would silently render
    every pending user as unverified — a verified recipient would look
    'pending' forever (the exact bug this signature guards against)."""
    out = {}
    try:
        paginator = _sesv2().get_paginator("list_email_identities")
        for page in paginator.paginate():
            for ident in page.get("EmailIdentities", []):
                if ident.get("IdentityType") != "EMAIL_ADDRESS":
                    continue
                status = ident.get("VerificationStatus", "")
                # VerificationStatus is authoritative; SendingEnabled is the fallback
                # (an email identity only sends once verified).
                verified = (status == "SUCCESS") if status else bool(ident.get("SendingEnabled"))
                out[ident.get("IdentityName", "")] = verified
    except Exception as e:
        logger.warning(f"verified_identities_map failed (callers fall back to per-user checks): {e}")
        return None
    return out


def account_status() -> dict:
    """{'sending_enabled': bool, 'production_access': bool} for this SES account."""
    try:
        r = _sesv2().get_account()
        return {
            "sending_enabled": bool(r.get("SendingEnabled")),
            "production_access": bool(r.get("ProductionAccessEnabled")),
        }
    except Exception as e:
        logger.warning(f"account_status failed: {e}")
        return {"sending_enabled": False, "production_access": False, "error": str(e)}


def trigger_verification(address: str) -> None:
    """Start (or RE-start) SES verification for an address — sends AWS's email.

    SES verification links expire after 24 hours; a recipient who misses the
    window leaves the identity behind in a PENDING/FAILED state. Deleting the
    Cognito user does NOT delete the SES identity, so a naive "create, and treat
    AlreadyExists as done" silently sends nothing on re-invite (the exact
    deadlock: forever 'Pending email verification', Send invite forever
    disabled). SESv2's only re-send path is delete + recreate, so when the
    identity exists but is NOT verified we recreate it to fire a fresh email.
    A verified identity is left untouched."""
    if not address:
        return
    ses = _sesv2()
    try:
        ses.create_email_identity(EmailIdentity=address)
        logger.info("Started SES verification for %s", address)
    except ses.exceptions.AlreadyExistsException:
        try:
            r = ses.get_email_identity(EmailIdentity=address)
            if r.get("VerifiedForSendingStatus"):
                return  # already verified — nothing to re-send
            ses.delete_email_identity(EmailIdentity=address)
            ses.create_email_identity(EmailIdentity=address)
            logger.info("Re-started SES verification for %s (previous attempt %s)",
                        address, r.get("VerificationStatus", "UNKNOWN"))
        except Exception as e:
            logger.warning(f"re-trigger_verification({address}) failed: {e}")
            raise
    except Exception as e:
        logger.warning(f"trigger_verification({address}) failed: {e}")
        raise


def status(config: dict, user_pool_id: str = "", apply_error: str = "") -> dict:
    """Combine the stored sender config with live SES + pool state into a readiness view.

    - ``sender_verified`` = configured AND SES identity verified AND account sending enabled.
    - ``pool_active`` = the pool's EmailConfiguration is actually DEVELOPER+this sender.
    - ``ready`` (drives the UI "active" state AND the invite gate) = ``sender_verified``
      AND ``pool_active`` — so we never report "active" (or let an invite through)
      while the pool is still on COGNITO_DEFAULT and would send from ``no-reply``.

    Sandbox is reported separately as a warning — it does not reduce ``ready``, but
    invitations to *unverified* recipients won't deliver until production access is
    granted. ``apply_error`` carries the last failed pool-apply reason (if any).
    """
    addr = (config or {}).get("from_address", "")
    ident = identity_status(addr) if addr else {"exists": False, "verified": False}
    acct = account_status()
    sender_verified = bool(addr and ident.get("verified") and acct.get("sending_enabled"))
    pool_active = bool(addr and user_pool_id and pool_uses_sender(user_pool_id, addr))
    return {
        "configured": bool(addr),
        "from_address": addr,
        "from_name": (config or {}).get("from_name", ""),
        "identity_exists": ident.get("exists", False),
        "identity_verified": ident.get("verified", False),
        "sending_enabled": acct.get("sending_enabled", False),
        "production_access": acct.get("production_access", False),
        "sandbox": not acct.get("production_access", False),
        "sender_verified": sender_verified,
        "pool_active": pool_active,
        "apply_error": apply_error or "",
        "ready": bool(sender_verified and pool_active),
    }


# ── Cognito pool EmailConfiguration ───────────────────────────────────────────
# Fields DescribeUserPool returns that UpdateUserPool also accepts. We re-pass these
# so the update can never drop them. Immutable / read-only fields returned by
# describe (Id, Arn, Name, Schema, UsernameAttributes, UsernameConfiguration,
# CreationDate, ...) are deliberately excluded — UpdateUserPool rejects them.
_POOL_UPDATE_FIELDS = (
    "Policies",
    "DeletionProtection",
    "LambdaConfig",
    "AutoVerifiedAttributes",
    "VerificationMessageTemplate",
    "SmsAuthenticationMessage",
    "UserAttributeUpdateSettings",
    "DeviceConfiguration",
    "SmsConfiguration",
    "UserPoolTags",
    "AdminCreateUserConfig",
    "UserPoolAddOns",
    "AccountRecoverySetting",
    # MfaConfiguration is writable and IS returned by DescribeUserPool, so it has to be
    # re-passed or the update takes the pool from MFA required to MFA OFF — an
    # authentication downgrade with no error and nothing in the response to show it.
    # cognito.yaml declares MfaConfiguration: 'ON'.
    # tests/test_pool_update_preserves_mfa.py pins its presence here.
    "MfaConfiguration",
    "UserPoolTier",
)

# NOT in the list above, deliberately:
#   EnabledMfas / SoftwareTokenMfaConfiguration — UpdateUserPool does not accept either
#     and DescribeUserPool does not return them. Which FACTORS are enabled is owned by
#     Set/GetUserPoolMfaConfig, so apply_to_pool round-trips that separately below.
#     Listing them here would look like coverage but be skipped by the
#     `val in (None, "")` guard, since describe never supplies a value.
#   EmailVerificationMessage / EmailVerificationSubject / SmsVerificationMessage —
#     writable and returned, but Cognito rejects them alongside
#     VerificationMessageTemplate (already re-passed above), the same class of conflict
#     as AdminCreateUserConfig.UnusedAccountValidityDays.


def _email_config(source_arn: str, from_address: str, from_name: str = "") -> dict:
    frm = f"{from_name} <{from_address}>" if from_name else from_address
    return {"EmailSendingAccount": "DEVELOPER", "SourceArn": source_arn, "From": frm}


# MFA factor configuration lives behind Get/SetUserPoolMfaConfig, NOT UpdateUserPool.
# Only these keys round-trip cleanly between the two calls.
_MFA_ROUNDTRIP_KEYS = (
    "MfaConfiguration",
    "SoftwareTokenMfaConfiguration",
    "SmsMfaConfiguration",
    "EmailMfaConfiguration",
)


def _get_mfa_config(cog, user_pool_id: str) -> dict:
    """The pool's MFA configuration, or {} if it cannot be read.

    Fail-soft: an older deployment whose task role predates
    cognito-idp:GetUserPoolMfaConfig must still be able to set its email sender.
    """
    try:
        resp = cog.get_user_pool_mfa_config(UserPoolId=user_pool_id)
        return {k: resp[k] for k in _MFA_ROUNDTRIP_KEYS if resp.get(k) not in (None, "")}
    except Exception as e:
        logger.warning("Could not read MFA config for pool %s: %s", user_pool_id, e)
        return {}


def _restore_mfa_config(cog, user_pool_id: str, before: dict) -> None:
    """Re-assert the MFA configuration captured before an UpdateUserPool call.

    Belt-and-braces: ``MfaConfiguration`` is in ``_POOL_UPDATE_FIELDS`` so the scalar is
    preserved, and which factors are enabled sits outside UpdateUserPool's surface so it
    should survive untouched. Both are asserted rather than assumed because the failure
    mode is silent — a pool reading "MFA required" with no enrolled factor, or MFA off
    while the posture endpoint reports it on.

    Fail-soft by design: never raise. Preserving the field is what prevents the
    downgrade; a missing cognito-idp:SetUserPoolMfaConfig permission on an existing
    installation must not stop email sending from being configured.
    """
    mode = (before or {}).get("MfaConfiguration", "")
    if not mode or mode == "OFF":
        return  # nothing to protect
    try:
        after = _get_mfa_config(cog, user_pool_id)
        if after == before:
            return  # preserved by the update, no write needed
        cog.set_user_pool_mfa_config(UserPoolId=user_pool_id, **before)
        logger.warning(
            "Re-asserted MFA configuration (%s) on pool %s after UpdateUserPool", mode, user_pool_id
        )
    except Exception as e:
        logger.error(
            "MFA configuration may have been altered on pool %s and could not be "
            "re-asserted (%s). Expected MfaConfiguration=%s.", user_pool_id, e, mode
        )


def apply_to_pool(user_pool_id: str, from_address: str, from_name: str = "") -> None:
    """Point the pool's EmailConfiguration at the SES sender, preserving every other
    pool setting (see the UpdateUserPool footgun note in the module docstring).
    Raises on failure so the caller can report it."""
    cog = _cognito()
    pool = cog.describe_user_pool(UserPoolId=user_pool_id)["UserPool"]

    # Which MFA FACTORS are enabled is not part of UpdateUserPool's surface at all, so
    # re-passing MfaConfiguration cannot carry it. Capture it first and re-assert after,
    # so an apply can never leave the pool on "MFA required" with no factor to satisfy it.
    mfa_before = _get_mfa_config(cog, user_pool_id)

    kwargs = {"UserPoolId": user_pool_id}
    for key in _POOL_UPDATE_FIELDS:
        val = pool.get(key)
        if val in (None, ""):
            continue
        if key == "AdminCreateUserConfig" and isinstance(val, dict):
            # UnusedAccountValidityDays conflicts with PasswordPolicy
            # .TemporaryPasswordValidityDays (which describe injects) — strip it.
            val = {k: v for k, v in val.items() if k != "UnusedAccountValidityDays"}
        kwargs[key] = val

    kwargs["EmailConfiguration"] = _email_config(identity_arn(from_address), from_address, from_name)
    cog.update_user_pool(**kwargs)
    logger.info("Applied SES EmailConfiguration (%s) to pool %s", from_address, user_pool_id)
    _restore_mfa_config(cog, user_pool_id, mfa_before)


def pool_uses_sender(user_pool_id: str, from_address: str) -> bool:
    """True if the pool is already configured to send via this SES sender."""
    try:
        pool = _cognito().describe_user_pool(UserPoolId=user_pool_id)["UserPool"]
        ec = pool.get("EmailConfiguration", {}) or {}
        return (
            ec.get("EmailSendingAccount") == "DEVELOPER"
            and ec.get("SourceArn") == identity_arn(from_address)
        )
    except Exception as e:
        logger.warning(f"pool_uses_sender failed: {e}")
        return False


def reconcile(user_pool_id: str, config: dict) -> str:
    """If a verified sender is stored but the pool isn't using it (fresh config, or a
    stack update reset EmailConfiguration to COGNITO_DEFAULT), apply it. Never raises;
    returns '' on success/no-op or the error message so the caller can surface it
    (e.g. the missing iam:CreateServiceLinkedRole on first switch to SES)."""
    try:
        addr = (config or {}).get("from_address", "")
        if not addr or not user_pool_id:
            return ""
        if identity_status(addr).get("verified") and not pool_uses_sender(user_pool_id, addr):
            apply_to_pool(user_pool_id, addr, (config or {}).get("from_name", ""))
            logger.info("Reconciled pool %s EmailConfiguration to SES sender %s", user_pool_id, addr)
        return ""
    except Exception as e:
        logger.warning(f"reconcile failed: {e}")
        return str(e)
