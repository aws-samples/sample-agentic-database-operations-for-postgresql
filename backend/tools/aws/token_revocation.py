# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Per-user "tokens not before": access ends when an admin takes it away.

Authorization reads ``cognito:groups`` from the bearer token, and an access or ID token
stays valid for its whole lifetime (60 minutes) whatever happens to the user in Cognito.
Global sign-out revokes refresh tokens but not a self-contained JWT the edge already
accepts. So a removed operator, a demoted admin or a deleted user kept calling the API
until their token expired.

On every change that takes access away (role change, space-access removal, offboarding,
user deletion) the backend writes the moment of the change for that user, and the
authentication layer refuses any token issued (``iat``) before it. A token issued after the
change carries the user's current groups, so it is accepted.

The record lives in the config table, one item per user ``sub``: ``tokens_not_before:<sub>``.
Reads are cached per user for at most ``CACHE_TTL_S`` (30 s), so a request does not read
the database every time and a revocation takes effect on every task within 30 seconds (at
once on the task that wrote it). A failed read keeps the last known value, or allows when
there is none: the store being unreachable must not lock every user out, the same rule the
in-app JWT check follows for its own infrastructure.
"""

import logging
import os
import time
from datetime import datetime, timezone

import boto3

from tools.resource_names import table_name

logger = logging.getLogger("token-revocation")

KEY_PREFIX = "tokens_not_before:"
CACHE_TTL_S = 30
_CACHE_MAX = 4096

# sub -> (not_before epoch seconds, 0 when none; monotonic expiry)
_cache: dict = {}
_table = None


def _config_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1")) \
            .Table(table_name("config"))
    return _table


def _remember(sub: str, not_before: int) -> None:
    if sub not in _cache and len(_cache) >= _CACHE_MAX:
        now = time.monotonic()
        for k in [k for k, (_, exp) in _cache.items() if exp <= now]:
            del _cache[k]
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
    _cache[sub] = (not_before, time.monotonic() + CACHE_TTL_S)


def revoke_tokens(sub: str, reason: str = "") -> int:
    """Refuse every token issued to ``sub`` before now. Returns the stored timestamp.

    Raises on a store error: the caller must not report the change as complete when the
    user's current sign-in still works."""
    if not sub:
        raise ValueError("a user sub is required to revoke tokens")
    now = int(time.time())
    _config_table().put_item(Item={
        "config_key": f"{KEY_PREFIX}{sub}",
        "not_before": now,
        "reason": reason or "access changed",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    _remember(sub, now)
    return now


def tokens_not_before(sub: str) -> int:
    """The stored timestamp for ``sub`` (0 when none), cached for at most CACHE_TTL_S."""
    if not sub:
        return 0
    hit = _cache.get(sub)
    if hit and hit[1] > time.monotonic():
        return hit[0]
    try:
        item = _config_table().get_item(Key={"config_key": f"{KEY_PREFIX}{sub}"}).get("Item") or {}
        value = int(item.get("not_before") or 0)
    except Exception as e:  # noqa: BLE001 - see the module docstring for the fail mode
        logger.warning("tokens-not-before read failed for %s (%s); using the last known value", sub, e)
        return hit[0] if hit else 0
    _remember(sub, value)
    return value


def token_revoked(claims: dict) -> bool:
    """True when the token was issued before its user's access last changed."""
    sub = (claims or {}).get("sub") or ""
    iat = (claims or {}).get("iat")
    if not sub or iat is None:
        return False
    try:
        issued = int(iat)
    except (TypeError, ValueError):
        return False
    not_before = tokens_not_before(sub)
    return bool(not_before) and issued < not_before
