# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Per-caller rate limiting for the model-invoking paths.

The REST surface sits behind a single deployment-wide API Gateway throttle, and the
AgentCore runtime that serves ``/invocations`` has no gateway throttle at all. Neither is
per-caller, so one authenticated operator (a runaway client loop, a stolen token, or one
user monopolising the shared AI budget) can consume the model capacity and starve everyone
else. This adds a per-caller counter in front of the model-invoking paths.

Design notes:

  * It keys off the caller's identity (Cognito sub), so it bounds an *authenticated*
    caller. It is a fairness / runaway guard, not a defence against an anonymous flood
    (that is the WAF layer, which the AgentCore data plane does not have; documented).
  * It **fails open**. The counter is not a security boundary, and it must never block a
    legitimate operator because its own backing store had a hiccup. Any error allows the
    request. The real controls (read-only DB access, per-space scoping, model governance,
    the account model quota) hold regardless.
  * The default limit is deliberately generous (60 requests/minute per caller, i.e. one
    per second sustained), far above human interactive pace even during an incident, so a
    real DBA is never throttled; only a runaway loop or script reaches it. Tunable via the
    ``RATE_LIMIT_PER_MINUTE`` environment variable.

Implementation: a DynamoDB item per (caller, 60-second window), incremented atomically with
an ``ADD`` so concurrent requests cannot race, with a TTL that drops the window shortly
after it closes.
"""
import logging
import os
import time

import boto3

from tools.resource_names import table_name

logger = logging.getLogger("dbops-agent")

_DEFAULT_LIMIT = 60
_WINDOW_SECONDS = 60
_STORE_TTL_SECONDS = 30  # cache the console-set ceiling briefly to avoid a read per turn

_table = None
# Cached account-wide override from the governance store. `value` is the resolved int
# ceiling or None ("no override, use env/default"); `at` is when it was last read.
_limit_cache = {"value": None, "at": 0.0}


def _get_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(table_name("rate_limits"))
    return _table


def _store_limit():
    """The console-configured per-caller ceiling from the governance store, cached for a
    few seconds. Returns an int (>=1) or None when unset. FAIL-SOFT: any error (store
    hiccup, no creds in a test) returns None so the caller falls back to env/default and a
    store problem never changes the limiter's behaviour."""
    now = time.time()
    if now - _limit_cache["at"] < _STORE_TTL_SECONDS:
        return _limit_cache["value"]
    value = None
    try:
        # Imported lazily so importing this module never drags in the DynamoDB client
        # (and to avoid an import cycle with the app layer).
        from tools.aws.governance_store import governance_registry
        raw = governance_registry.get_rate_limit().get("per_minute")
        if raw is not None:
            parsed = int(raw)
            if parsed >= 1:
                value = parsed
    except Exception as e:  # noqa: BLE001 - fail soft; env/default still applies
        logger.debug("rate limit store unavailable (%s); using env/default", e)
        value = None
    _limit_cache["value"] = value
    _limit_cache["at"] = now
    return value


def default_limit() -> int:
    """The deployment default ceiling (env RATE_LIMIT_PER_MINUTE or the built-in), i.e. the
    value used when nothing is set in the console store. Shown in the settings UI."""
    raw = os.getenv("RATE_LIMIT_PER_MINUTE", "")
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    return val if val >= 1 else _DEFAULT_LIMIT


def limit_per_minute() -> int:
    """The per-caller ceiling. Resolution order: the console-set value in the governance
    store, then the RATE_LIMIT_PER_MINUTE env, then the generous default. A floor of 1
    keeps a misconfigured value from blocking everything; a non-positive/invalid value
    falls back to the default."""
    stored = _store_limit()
    if stored is not None:
        return stored
    return default_limit()


def check_rate_limit(caller_id: str) -> tuple:
    """Count one request for ``caller_id`` in the current 60-second window.

    Returns ``(allowed, retry_after_seconds)``. FAILS OPEN: on a missing caller id or any
    backend error, returns ``(True, 0)`` so the request proceeds — the limiter never blocks
    a caller because of its own infrastructure problem.
    """
    if not caller_id:
        return True, 0
    limit = limit_per_minute()
    now = int(time.time())
    window = now // _WINDOW_SECONDS
    key = f"{caller_id}|{window}"
    # Expire a couple of windows after this one closes, so the table self-cleans.
    expires_at = (window + 2) * _WINDOW_SECONDS
    try:
        resp = _get_table().update_item(
            Key={"id": key},
            UpdateExpression="ADD #c :one SET expires_at = if_not_exists(expires_at, :exp)",
            ExpressionAttributeNames={"#c": "count"},
            ExpressionAttributeValues={":one": 1, ":exp": expires_at},
            ReturnValues="UPDATED_NEW",
        )
        count = int(resp["Attributes"]["count"])
    except Exception as e:  # noqa: BLE001 - fail open on ANY error
        logger.warning("rate limiter unavailable (%s); allowing the request (fail-open)", e)
        return True, 0
    if count > limit:
        retry_after = _WINDOW_SECONDS - (now % _WINDOW_SECONDS)
        return False, max(1, retry_after)
    return True, 0
