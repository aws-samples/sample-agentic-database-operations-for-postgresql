# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Resolve which SPACE ADMIN manages a space (for the space-admin cascade tier).

The capability cascade is: account (super admin) → space admin → space → operator.
The "space admin tier above a space" = the tier of the space admin who manages that
space, which is its recorded creator while they hold the `space-admins` role (mirrors
`_can_manage_space`: a space is managed by its creator or a super admin, and group
membership grants use only, so another space admin in the space's group adds no tier).

Used at request time by model/knowledge resolution, so results are cached with a
short TTL and every failure is soft (an empty set = "no admin tier", never an
error) — a Cognito hiccup must not break a chat turn.
"""

import os
import time
import logging

import boto3

logger = logging.getLogger("space-admins")

_TTL_SECONDS = 60
_cache: dict = {}  # space_id -> (expires_at, frozenset[sub])


def _cognito():
    return boto3.client("cognito-idp", region_name=os.getenv("AWS_REGION", "us-east-1"))


def _subs_in_group(cognito, user_pool_id: str, group_name: str) -> dict:
    """Map username -> sub for every user in a Cognito group (paginated)."""
    out = {}
    paginator = cognito.get_paginator("list_users_in_group")
    for page in paginator.paginate(UserPoolId=user_pool_id, GroupName=group_name):
        for u in page.get("Users", []):
            sub = next((a["Value"] for a in u.get("Attributes", []) if a["Name"] == "sub"), "")
            if sub:
                out[u["Username"]] = sub
    return out


def managing_admin_subs(space_id: str, created_by: str = "") -> frozenset:
    """Subs of the space admins who manage `space_id` (cached ~60s, fail-soft).

    At most one: `created_by` when that sub is in the deployment's `space-admins`
    group (one group listing). Returns frozenset() on any failure.
    """
    if not space_id:
        return frozenset()
    now = time.time()
    hit = _cache.get(space_id)
    if hit and hit[0] > now:
        return hit[1]

    subs = set()
    try:
        user_pool_id = os.getenv("USER_POOL_ID", "")
        if user_pool_id and created_by:
            space_admins = _subs_in_group(_cognito(), user_pool_id, "space-admins")
            if created_by in set(space_admins.values()):
                subs.add(created_by)
    except Exception as e:
        logger.warning(f"managing_admin_subs({space_id}) failed (fail-soft, no admin tier): {e}")
        return frozenset()

    result = frozenset(subs)
    _cache[space_id] = (now + _TTL_SECONDS, result)
    return result


def invalidate(space_id: str = ""):
    """Drop the cache (one space or all) after a role mutation."""
    if space_id:
        _cache.pop(space_id, None)
    else:
        _cache.clear()
