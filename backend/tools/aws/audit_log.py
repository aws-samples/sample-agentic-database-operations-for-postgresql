# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Admin audit log: the write path, runtime vault settings, projections and verify.

An S3 bucket with Object Lock (COMPLIANCE) is the record of truth and the existing
``<prefix>-audit`` DynamoDB table is the search index. While the vault is on, every
admin event is written once to the bucket first (``IfNoneMatch="*"`` makes each key
write-once) and then to insert-only index rows that carry the object's key, version id
and SHA-256. ``verify_row`` proves an index row against its locked object.

Agent events (``query_plan_*``, category ``agent``) are index-only: they never touch
S3, never read the vault settings and never emit ``VaultOff`` / ``VaultWriteFailures``.
Their row goes to partition ``agent`` with no ``month``, which no console query reads.

``write_event`` never raises: an audit failure must not break the mutation it records.
Every AWS call uses ``AUDIT_CLIENT_CONFIG`` and the per-event / per-request budgets
below, so audit work stays inside the API Gateway timeout.

Stdlib + boto3/botocore only. ``tools.aws.spaces_client`` is imported lazily inside the
space lookup to avoid an import cycle. Nothing in the curated tier1 / batch ``tools/``
subset imports this module.
"""

import base64
import contextvars
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import types
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import boto3
from botocore.config import Config

from tools.resource_names import owner_admin_of, resource_prefix, table_name

logger = logging.getLogger("audit-log")

# ── Action catalog ───────────────────────────────────────────────────────────
# code -> (label, category, target_type, owner-visible diff keys)
ACTIONS = {
    "space_create":            ("Created space", "admin", "space", ("name", "regions", "primary_account_id")),
    "space_update":            ("Changed space details", "admin", "space", ("name", "description", "regions", "capabilities", "status", "tags", "model_governance", "knowledge_governance")),
    "space_delete":            ("Deleted space", "admin", "space", ("name", "result", "databases_detached", "databases_deleted")),
    "space_binding_update":    ("Changed account binding", "admin", "space", ("primary_account_id", "secondary_account_ids", "secondary_role_names")),
    "space_access_configure":  ("Validated account access", "admin", "space", ("secondary_role_names",)),
    "space_discover":          ("Ran discovery", "admin", "space", ("regions", "found")),
    "space_candidate_dismiss": ("Dismissed discovered database", "admin", "database", ()),
    "space_db_onboard":        ("Onboarded database", "admin", "database", ("mode", "result", "batch", "member_count")),
    "space_investigation_delete": ("Deleted investigation", "admin", "space", ()),
    "space_topology_refresh":  ("Refreshed topology metadata", "admin", "space", ()),
    "database_space_remove":   ("Removed database from space", "admin", "database", ("member_count", "teardown")),
    "database_decommission":   ("Decommissioned database", "admin", "database", ("member_count",)),
    # The background teardown's outcome (the request records the action that started it).
    "database_teardown_complete": ("Finished removing database", "admin", "database", ("trigger", "result")),
    "database_teardown_failed": ("Database removal incomplete", "admin", "database", ("trigger", "result")),
    # Written by the Tier 1 collector as actor "system" (see tier1_collector.py).
    "cluster_failover_detected": ("Detected cluster failover", "admin", "database",
                                  ("writer_instance_id", "previous_writer_instance_id")),
    # Written by the API task's route repair (tools/aws/route_repair.py): actor "system"
    # from its background loop, the admin who pressed Repair otherwise.
    "network_host_routes_repaired": ("Repaired cross-Region host routes", "admin", "database",
                                     ("added", "removed")),
    "network_host_routes_repair_refused": ("Refused cross-Region host route repair", "admin", "database",
                                           ("reason",)),
    # A space admin took over a route that pointed the agent CIDR at a foreign peering
    # (POST .../claim-route). before/after name the peering the route pointed to.
    "database_route_claimed":  ("Claimed conflicting route", "admin", "database",
                                ("route_table_id", "cidr", "pointed_to")),
    "space_delete_teardown_complete": ("Finished deleting space", "admin", "space", ("result", "databases_deleted")),
    "space_delete_teardown_failed": ("Space deletion incomplete", "admin", "space", ("result", "databases_deleted", "databases_left")),
    "database_rename":         ("Renamed database", "admin", "database", ("display_name",)),
    "monitoring_config_set":   ("Changed monitoring cadence", "admin", "database", ("tier1_frequency", "tier1_enabled", "tier2_frequency", "tier2_enabled", "retention_days")),
    "space_models_update":     ("Changed space models", "admin", "space", ("enabled", "default_model", "disabled_models")),
    "space_knowledge_disabled": ("Changed space knowledge", "admin", "space", ("disabled_items",)),
    "knowledge_space_upload":  ("Uploaded space document", "admin", "knowledge_doc", ("name",)),
    "knowledge_space_delete":  ("Deleted space document", "admin", "knowledge_doc", ("name",)),
    "space_user_assign":       ("Added operator", "admin", "user", ("auto_created",)),
    "space_user_remove":       ("Removed operator", "admin", "user", ("reason",)),
    "space_user_resend_invite": ("Resent operator invite", "admin", "user", ()),
    "space_user_resend_verification": ("Resent operator verification", "admin", "user", ()),
    "audit_vault_set":         ("Switched audit vault", "admin", "setting", ()),
    "audit_retention_set":     ("Changed audit retention", "admin", "setting", ()),
    # account-level, super admin only; never in a space tab, so no owner-visible keys:
    "user_invite":             ("Invited user", "admin", "user", ()),
    "user_role_set":           ("Set user role", "admin", "user", ()),
    "user_role_change":        ("Changed user role", "admin", "user", ()),
    "user_delete":             ("Deleted user", "admin", "user", ()),
    "user_resend_invite":      ("Resent user invite", "admin", "user", ()),
    "user_resend_verification": ("Resent user verification", "admin", "user", ()),
    "operator_offboard":       ("Offboarded operator", "admin", "user", ()),
    "model_enable":            ("Enabled model", "admin", "model", ()),
    "model_disable":           ("Disabled model", "admin", "model", ()),
    "model_metadata":          ("Changed model details", "admin", "model", ()),
    "account_default_model":   ("Changed account default model", "admin", "model", ()),
    "knowledge_global_upload": ("Uploaded account document", "admin", "knowledge_doc", ()),
    "knowledge_global_delete": ("Deleted account document", "admin", "knowledge_doc", ()),
    "knowledge_admin_tier_upload": ("Uploaded admin-tier document", "admin", "knowledge_doc", ()),
    "knowledge_admin_tier_delete": ("Deleted admin-tier document", "admin", "knowledge_doc", ()),
    "admin_governance_update": ("Changed admin governance", "admin", "governance", ()),
    "email_sender_set":        ("Changed email sender", "admin", "setting", ()),
    "rate_limit_set":          ("Changed rate limit", "admin", "setting", ()),
    "budget_limit_set":        ("Changed budget limit", "admin", "setting", ()),
    "guardrail_alarm_set":     ("Changed guardrail alarm", "admin", "setting", ()),
    "alarm_settings_set":      ("Changed database alarms", "admin", "setting", ()),
    "alarm_test_sent":         ("Sent a test alarm email", "admin", "setting", ()),
    "query_plan_run":          ("Agent ran EXPLAIN", "agent", "database", ()),
    "query_plan_refused":      ("Agent EXPLAIN refused", "agent", "database", ()),
}


def action_label(action: str) -> str:
    entry = ACTIONS.get(action or "")
    return entry[0] if entry else (action or "").replace("_", " ").capitalize()


def action_category(action: str) -> str:
    entry = ACTIONS.get(action or "")
    return entry[1] if entry else "admin"


def action_target_type(action: str) -> str:
    entry = ACTIONS.get(action or "")
    return entry[2] if entry else ""


def _owner_keys(action: str) -> tuple:
    entry = ACTIONS.get(action or "")
    return entry[3] if entry else ()


# ── Request context, clients, clocks ─────────────────────────────────────────
# Per-request actor and request facts, set by the API middleware: sub, email, groups,
# resolve_email, request_id, source_ip and the request-scoped audit `deadline`. Empty on
# the agent runtime and in any non-request caller. The default is read-only.
AUDIT_CONTEXT = contextvars.ContextVar("audit_context", default=types.MappingProxyType({}))

# Two attempts of 1 s connect + 2 s read plus standard-mode backoff: about 7 s per call.
AUDIT_CLIENT_CONFIG = Config(connect_timeout=1, read_timeout=2,
                             retries={"max_attempts": 2, "mode": "standard"})

EVENT_BUDGET_S = 8.0
INDEX_DIFF_MAX = 32 * 1024
VAULT_BODY_MAX = 256 * 1024
SETTINGS_KEY = "audit_settings"
SETTINGS_TTL_S = 30.0
SETTINGS_WARN_EVERY_S = 300.0
RETENTION_MAX_DAYS = 3653
SYSTEM_ACTORS = ("system", "cloudformation", "unknown")
_SECRET_MARKERS = ("password", "secret_value", "secretstring", "token", "private_key", "credential")
_STRING_MAX = 1000
_SPACE_TTL_S = 60.0
_SPACE_CACHE_MAX = 256
_FANOUT_WARN = 50


def _monotonic() -> float:
    return time.monotonic()


def _now() -> datetime:
    return datetime.now(timezone.utc)


_clients = {}
_clients_lock = threading.Lock()


def _client(name: str):
    """Module-owned low-level client (s3 / dynamodb / cloudwatch), created once per process."""
    c = _clients.get(name)
    if c is None:
        with _clients_lock:
            c = _clients.get(name)
            if c is None:
                c = boto3.client(name, region_name=os.getenv("AWS_REGION", "us-east-1"),
                                 config=AUDIT_CLIENT_CONFIG)
                _clients[name] = c
    return c


def dynamodb_client():
    """The audit index client (AUDIT_CLIENT_CONFIG); GovernanceRegistry.query_audit uses it."""
    return _client("dynamodb")


# ── Environment, read once per process ───────────────────────────────────────
AUDIT_BUCKET = ""
AUDIT_VAULT_DEFAULT = "off"
AUDIT_RETENTION_FLOOR = 365
AUDIT_EXTEND_RETENTION = False


def _load_env() -> None:
    global AUDIT_BUCKET, AUDIT_VAULT_DEFAULT, AUDIT_RETENTION_FLOOR, AUDIT_EXTEND_RETENTION
    AUDIT_BUCKET = (os.getenv("AUDIT_BUCKET") or "").strip()
    AUDIT_VAULT_DEFAULT = "on" if (os.getenv("AUDIT_VAULT_DEFAULT") or "").strip().lower() == "on" else "off"
    raw = (os.getenv("AUDIT_RETENTION_FLOOR") or "").strip()
    floor = 365
    if raw:
        try:
            floor = int(raw)
            if not 1 <= floor <= RETENTION_MAX_DAYS:
                raise ValueError(raw)
        except ValueError:
            logger.warning("AUDIT_RETENTION_FLOOR %r is not a whole number of days in "
                           "[1, %d]; using 365", raw, RETENTION_MAX_DAYS)
            floor = 365
    AUDIT_RETENTION_FLOOR = floor
    AUDIT_EXTEND_RETENTION = (os.getenv("AUDIT_EXTEND_RETENTION") or "").strip().lower() == "true"


_load_env()


# ── Context helpers ──────────────────────────────────────────────────────────
def detached_context() -> contextvars.Context:
    """A fresh Context whose AUDIT_CONTEXT is the caller's minus its `deadline`. Use it to
    schedule audited background tasks: they keep the caller's identity but not the
    request's audit deadline."""
    cur = AUDIT_CONTEXT.get() or {}
    ctx = contextvars.copy_context()
    ctx.run(AUDIT_CONTEXT.set, {k: v for k, v in cur.items() if k != "deadline"})
    return ctx


def submit_in_context(pool, fn, *args, **kwargs):
    """`pool.submit` with the submitter's contextvars. A new copy per call: one Context
    cannot be entered by two threads at once."""
    return pool.submit(contextvars.copy_context().run, fn, *args, **kwargs)


# ── DynamoDB attribute values (low-level client) ─────────────────────────────
def _to_av(v):
    if v is None:
        return {"NULL": True}
    if isinstance(v, bool):
        return {"BOOL": v}
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return {"S": str(v)}
        return {"N": repr(v)}
    if isinstance(v, (int, Decimal)):
        return {"N": str(v)}
    if isinstance(v, str):
        return {"S": v}
    if isinstance(v, (bytes, bytearray)):
        return {"B": bytes(v)}
    if isinstance(v, dict):
        return {"M": {str(k): _to_av(x) for k, x in v.items()}}
    if isinstance(v, (list, tuple)):
        return {"L": [_to_av(x) for x in v]}
    if isinstance(v, (set, frozenset)):
        return {"L": [_to_av(x) for x in sorted(v, key=str)]}
    return {"S": str(v)}


def _num(s: str):
    try:
        return int(s)
    except ValueError:
        return float(s)


def _from_av(av):
    if not isinstance(av, dict) or len(av) != 1:
        return av
    (t, v), = av.items()
    if t == "S":
        return v
    if t == "N":
        return _num(v)
    if t == "BOOL":
        return bool(v)
    if t == "NULL":
        return None
    if t == "M":
        return {k: _from_av(x) for k, x in v.items()}
    if t == "L":
        return [_from_av(x) for x in v]
    if t == "SS":
        return sorted(v)
    if t == "NS":
        return sorted(_num(x) for x in v)
    if t in ("B", "BS"):
        return v
    return v


def to_item(d: dict) -> dict:
    return {k: _to_av(v) for k, v in d.items()}


def from_item(item) -> dict:
    return {k: _from_av(v) for k, v in (item or {}).items()}


# ── Runtime vault settings ───────────────────────────────────────────────────
_settings_lock = threading.Lock()
_settings_cache = {"value": None, "at": None}
_settings_warned_at = [None]


def _resolve_settings(item) -> dict:
    """Per-field resolution: an absent item or field falls back to the deploy default."""
    item = item or {}
    floor = AUDIT_RETENTION_FLOOR
    if "vault_enabled" in item:
        vault = bool(item["vault_enabled"])
    else:
        vault = AUDIT_VAULT_DEFAULT == "on"
    retention = floor
    if "retention_days" in item:
        try:
            retention = int(item["retention_days"])
        except (TypeError, ValueError):
            retention = floor
    return {"vault_enabled": vault, "retention_days": retention,
            "effective_retention": max(retention, floor), "floor": floor}


def _warn_settings(err: Exception) -> None:
    now = _monotonic()
    last = _settings_warned_at[0]
    if last is None or now - last >= SETTINGS_WARN_EVERY_S:
        _settings_warned_at[0] = now
        logger.warning("audit settings read failed (%s); using the last known or "
                       "deploy-default vault settings", type(err).__name__)


def current_settings(read: bool = True) -> dict:
    """{vault_enabled, retention_days, effective_retention, floor}, cached 30 s. A read
    error (or read=False, used past the request deadline) gives the last cached value,
    else the deploy defaults."""
    now = _monotonic()
    with _settings_lock:
        cached, at = _settings_cache["value"], _settings_cache["at"]
    if cached is not None and at is not None and now - at < SETTINGS_TTL_S:
        return dict(cached)
    if not read:
        return dict(cached) if cached is not None else _resolve_settings(None)
    try:
        resp = _client("dynamodb").get_item(
            TableName=table_name("config"), Key={"config_key": {"S": SETTINGS_KEY}})
        item = from_item(resp.get("Item")) if resp.get("Item") else None
        value = _resolve_settings(item)
    except Exception as e:  # noqa: BLE001 - settings fall back, never break a write
        _warn_settings(e)
        value = cached if cached is not None else _resolve_settings(None)
    with _settings_lock:
        _settings_cache["value"], _settings_cache["at"] = dict(value), now
    return dict(value)


def invalidate_settings() -> None:
    """Drop the cached settings so the next call re-reads them. The last value is kept
    only as the fallback for a failing read."""
    with _settings_lock:
        _settings_cache["at"] = None


# ── Pending metrics (datapoints deferred past a deadline) ────────────────────
_pending_lock = threading.Lock()
_pending = {}


def add_pending_metrics(**counts) -> None:
    with _pending_lock:
        for k, v in counts.items():
            if v:
                _pending[k] = _pending.get(k, 0) + v


def drain_pending_metrics() -> dict:
    with _pending_lock:
        out = dict(_pending)
        _pending.clear()
    return out


def _bump(metrics: dict, **counts) -> None:
    for k, v in counts.items():
        if v:
            metrics[k] = metrics.get(k, 0) + v


# ── Sanitize ─────────────────────────────────────────────────────────────────
def sanitize(value):
    """Drop secret-named keys, truncate long strings, make sets/Decimals JSON-safe."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            name = str(k)
            if any(m in name.lower() for m in _SECRET_MARKERS):
                continue
            out[name] = sanitize(v)
        return out
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return [sanitize(v) for v in sorted(value, key=str)]
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, str):
        return value[:_STRING_MAX] if len(value) > _STRING_MAX else value
    return value


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


# ── Space lookup (cached 60 s) ───────────────────────────────────────────────
_space_lock = threading.Lock()
_space_cache = {}


def _space_info(space_id: str) -> tuple:
    """(name, owner_admin) of a space, or ("", "") when unknown or on any error."""
    if not space_id:
        return "", ""
    now = _monotonic()
    with _space_lock:
        hit = _space_cache.get(space_id)
    if hit and hit[2] > now:
        return hit[0], hit[1]
    try:
        from tools.aws.spaces_client import spaces_registry
        space = spaces_registry.get_space(space_id) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("audit space lookup failed (%s)", type(e).__name__)
        return "", ""
    info = ((space.get("name") or ""), owner_admin_of(space))
    if space:
        with _space_lock:
            if len(_space_cache) >= _SPACE_CACHE_MAX:
                for k in [k for k, v in _space_cache.items() if v[2] <= now]:
                    _space_cache.pop(k, None)
                while len(_space_cache) >= _SPACE_CACHE_MAX:
                    _space_cache.pop(min(_space_cache, key=lambda k: _space_cache[k][2]), None)
            _space_cache[space_id] = (info[0], info[1], now + _SPACE_TTL_S)
    return info


# ── Record ───────────────────────────────────────────────────────────────────
def _actor_role(actor_sub: str, action: str, ctx) -> str:
    if actor_sub in SYSTEM_ACTORS:
        return "system"
    if action_category(action) == "agent":
        return "operator"
    groups = ctx.get("groups") or ()
    if "admins" in groups:
        return "super_admin"
    if "space-admins" in groups:
        return "space_admin"
    return ""


def _actor_email(actor_sub: str, ctx) -> str:
    if actor_sub in SYSTEM_ACTORS or not ctx.get("sub") or ctx.get("sub") != actor_sub:
        return ""
    email = ctx.get("email") or ""
    if "@" in email:
        return email
    resolver = ctx.get("resolve_email")
    if resolver:
        try:
            found = resolver(actor_sub) or ""
            return found if "@" in found else ""
        except Exception:  # noqa: BLE001 - display only
            return ""
    return ""


def _on_other_owner(actor_role: str, owner_admin: str, actor_sub: str) -> bool:
    return bool(actor_role == "super_admin" and owner_admin and owner_admin != actor_sub)


def build_record(actor, action, target, scope, before=None, after=None, *,
                 space_id=None, space_ids=None, target_label=None, target_type=None,
                 space_name=None, owner_admin=None, ctx=None, now=None, event_id=None) -> dict:
    """The v2 vault record (§4.4)."""
    ctx = ctx if ctx is not None else (AUDIT_CONTEXT.get() or {})
    now = now or _now()
    event_id = event_id or uuid.uuid4().hex
    actor_sub = actor or "unknown"
    category = action_category(action)
    scope = scope or ""
    sid = space_id or (scope[len("space:"):] if scope.startswith("space:") else "")
    agent = category == "agent"
    if space_name is None:
        space_name = "" if agent else _space_info(sid)[0]
    if owner_admin is None:
        owner_admin = "" if agent else _space_info(sid)[1]
    role = _actor_role(actor_sub, action, ctx)
    if space_ids is not None:
        ids = sorted({str(s) for s in space_ids if s})
    else:
        ids = [sid] if sid else []
    target_id = "" if target is None else str(target)
    return {
        "v": 2,
        "event_id": event_id,
        "ts": now.isoformat(timespec="microseconds"),
        "actor_sub": actor_sub,
        "actor_email": _actor_email(actor_sub, ctx),
        "actor_role": role,
        "action": action,
        "category": category,
        "target_type": target_type or action_target_type(action),
        "target_id": target_id,
        "target_label": target_label or target_id,
        "space_id": sid,
        "space_name": space_name or "",
        "space_ids": ids,
        "owner_admin": owner_admin or "",
        "on_other_owner": _on_other_owner(role, owner_admin or "", actor_sub),
        "before": sanitize(before if before is not None else {}),
        "after": sanitize(after if after is not None else {}),
        "request_id": ctx.get("request_id") or "",
        "source_ip": ctx.get("source_ip") or "",
        "scope": scope,
    }


def _search(row: dict) -> str:
    parts = (row.get("actor_email"), row.get("actor_sub"), action_label(row.get("action")),
             row.get("action"), row.get("target_label"), row.get("target_id"),
             row.get("space_name"))
    return " ".join(str(p) for p in parts if p).lower()


def derive_key(ts: str, event_id: str) -> str:
    """The vault key for an event, from its record ts or its index sort key (`iso#sfx`)."""
    iso = (ts or "").split("#", 1)[0]
    d = datetime.fromisoformat(iso)
    return f"audit/{d:%Y/%m/%d}/{d:%Y%m%dT%H%M%S%f}Z-{event_id}.json"


def _encode_body(record: dict) -> bytes:
    body = _json(record).encode()
    if len(body) > VAULT_BODY_MAX:
        record["before"] = {"_truncated": True}
        record["after"] = {"_truncated": True}
        body = _json(record).encode()
    return body


def _capped(diff):
    return {"_truncated": True} if len(_json(diff)) > INDEX_DIFF_MAX else diff


def _put_metrics(metrics: dict) -> None:
    data = [{"MetricName": k, "Value": float(v), "Unit": "Count"}
            for k, v in sorted(metrics.items()) if v]
    if data:
        _client("cloudwatch").put_metric_data(
            Namespace=f"{resource_prefix()}/Audit", MetricData=data)


_misconfig_logged = [False]


def write_event(actor, action, target, scope, before=None, after=None, *,
                force_vault=False, space_id=None, space_ids=None, target_label=None,
                target_type=None, space_name=None, owner_admin=None):
    """Write one audit event: the vault object first (when on), then the index rows.
    Returns the primary row's vault state "stored" | "failed" | "off", or None on an
    unexpected error. Never raises."""
    metrics = {}
    pending_handled = False
    try:
        t0 = _monotonic()
        ctx = AUDIT_CONTEXT.get() or {}
        req = ctx.get("deadline")
        deadline = t0 + EVENT_BUDGET_S if req is None else min(t0 + EVENT_BUDGET_S, req)

        def req_open():
            return req is None or _monotonic() < req

        _bump(metrics, **drain_pending_metrics())
        agent = action_category(action) == "agent"
        st = None if agent else current_settings(read=req_open())
        now = _now()
        event_id = uuid.uuid4().hex
        record = build_record(actor, action, target, scope, before, after,
                              space_id=space_id, space_ids=space_ids,
                              target_label=target_label, target_type=target_type,
                              space_name=space_name, owner_admin=owner_admin,
                              ctx=ctx, now=now, event_id=event_id)
        body = _encode_body(record)
        sha = hashlib.sha256(body)
        key = derive_key(record["ts"], event_id)
        fanout = []
        if not agent and space_ids is not None:
            fanout = [s for s in record["space_ids"] if s != record["space_id"]]
        vault_wanted = (not agent) and bool(st["vault_enabled"] or force_vault)

        # 4b. The request's audit deadline has already passed: log the full record and
        # defer the failure counts to the next event that has budget.
        if not req_open():
            logger.error("AUDIT_RECORD_DROPPED %s", body.decode())
            _bump(metrics, IndexWriteFailures=1 + len(fanout))
            if not agent:
                _bump(metrics, **{"VaultWriteFailures" if vault_wanted else "VaultOff": 1})
            add_pending_metrics(**metrics)
            pending_handled = True
            return "failed"

        # 5. The vault.
        vault, version, retain_until = "off", "", None
        if vault_wanted:
            if not AUDIT_BUCKET:
                vault = "failed"
                if not _misconfig_logged[0]:
                    _misconfig_logged[0] = True
                    logger.error("AUDIT_VAULT_MISCONFIGURED vault is on but AUDIT_BUCKET is empty")
                _bump(metrics, VaultWriteFailures=1)
            else:
                args = {
                    "Bucket": AUDIT_BUCKET, "Key": key, "Body": body,
                    "ContentType": "application/json", "IfNoneMatch": "*",
                    "ChecksumAlgorithm": "SHA256",
                    "ChecksumSHA256": base64.b64encode(sha.digest()).decode(),
                }
                lock_until = None
                if AUDIT_EXTEND_RETENTION and st["effective_retention"] > st["floor"]:
                    # 1 h margin: skew or rounding can never make the remaining days
                    # exceed the IAM upper bound, and they stay >= floor.
                    lock_until = now + timedelta(days=st["effective_retention"]) - timedelta(hours=1)
                    args["ObjectLockMode"] = "COMPLIANCE"
                    args["ObjectLockRetainUntilDate"] = lock_until
                try:
                    resp = _client("s3").put_object(**args)
                    vault = "stored"
                    version = (resp or {}).get("VersionId") or ""
                    retain_until = lock_until or (now + timedelta(days=st["floor"]))
                except Exception as e:  # noqa: BLE001
                    vault = "failed"
                    logger.error("AUDIT_VAULT_WRITE_FAILED action=%s event=%s err=%s",
                                 action, event_id, type(e).__name__)
                    _bump(metrics, VaultWriteFailures=1)
        elif not agent:
            _bump(metrics, VaultOff=1)

        # 6. Index rows (insert-only).
        primary = dict(record)
        primary["before"] = _capped(record["before"])
        primary["after"] = _capped(record["after"])
        primary.update({"ts": f"{record['ts']}#{event_id[:8]}", "sha256": sha.hexdigest(),
                        "vault": vault, "v": 2})
        if vault == "stored":
            primary["s3_key"] = key
            primary["s3_version_id"] = version
            primary["retain_until"] = retain_until.isoformat(timespec="microseconds")
        if agent:
            primary["entity"] = "agent"
        else:
            primary["entity"] = record["scope"] or "account"
            primary["month"] = record["ts"][:7]
        primary["search"] = _search(primary)
        rows = [primary]
        if len(fanout) > _FANOUT_WARN:
            logger.warning("audit fan-out of %d rows for %s", len(fanout), action)
        for sid in fanout:
            name, owner = _space_info(sid)
            row = {k: v for k, v in primary.items() if k != "month"}
            row.update({"entity": f"space:{sid}", "fanout": True, "space_id": sid,
                        "space_name": name, "owner_admin": owner,
                        "on_other_owner": _on_other_owner(record["actor_role"], owner,
                                                          record["actor_sub"])})
            row["search"] = _search(row)
            rows.append(row)

        ddb = None
        table = table_name("audit")
        skipped = 0
        primary_skipped = False
        for i, row in enumerate(rows):
            if i == 0 and not req_open():
                primary_skipped = True
                skipped += 1
                continue
            if i > 0 and _monotonic() >= deadline:
                skipped += 1
                continue
            try:
                ddb = ddb or _client("dynamodb")
                ddb.put_item(TableName=table, Item=to_item(row),
                             ConditionExpression="attribute_not_exists(entity)")
            except Exception as e:  # noqa: BLE001
                logger.error("AUDIT_INDEX_WRITE_FAILED action=%s event=%s s3_key=%s err=%s",
                             action, event_id, primary.get("s3_key", ""), type(e).__name__)
                _bump(metrics, IndexWriteFailures=1)
        if skipped:
            logger.error("AUDIT_INDEX_BUDGET_EXCEEDED event=%s s3_key=%s skipped=%d",
                         event_id, primary.get("s3_key", ""), skipped)
            _bump(metrics, IndexWriteFailures=skipped)
            if primary_skipped:
                # the primary row is missing: keep the event findable in the log
                logger.error("AUDIT_RECORD_DROPPED %s", body.decode())

        # 7. Metrics: one call, or defer past the budget.
        if any(metrics.values()):
            if _monotonic() < deadline:
                pending_handled = True
                try:
                    _put_metrics(metrics)
                except Exception as e:  # noqa: BLE001
                    logger.error("AUDIT_METRIC_FAILED err=%s", type(e).__name__)
            else:
                add_pending_metrics(**metrics)
                pending_handled = True
                logger.error("AUDIT_METRIC_DEFERRED %s", _json(metrics))
        return vault
    except Exception as e:  # noqa: BLE001 - audit must never break a mutation
        logger.error("AUDIT_WRITE_FAILED action=%s err=%s", action, type(e).__name__)
        if not pending_handled and metrics:
            add_pending_metrics(**metrics)
        return None


# ── Projections ──────────────────────────────────────────────────────────────
_NEVER_FOR_OWNER = ("owner_admin", "space_ids", "scope", "request_id", "source_ip",
                    "s3_key", "s3_version_id", "sha256")


def _normalise_v1(row: dict) -> dict:
    """Map a legacy (v1, no `v`) row to v2 field names; v2 rows are copied unchanged."""
    if (row or {}).get("v"):
        return dict(row)
    row = dict(row or {})
    scope = row.get("scope") or ""
    action = row.get("action") or ""
    target = row.get("target")
    target = "" if target is None else str(target)
    out = dict(row)
    out.update({
        "actor_sub": row.get("actor") or "",
        "target_id": target,
        "target_label": target,
        "space_id": scope[len("space:"):] if scope.startswith("space:") else "",
        "space_name": row.get("space_name") or "",
        "actor_email": "",
        "actor_role": "",
        "category": action_category(action),
        "target_type": action_target_type(action),
        "vault": "legacy",
        "on_other_owner": False,
        "fanout": False,
        "event_id": "",
    })
    out.pop("actor", None)
    out.pop("target", None)
    return out


def _display_email(r: dict) -> str:
    email = r.get("actor_email") or ""
    sub = r.get("actor_sub") or ""
    if email or not sub or sub in SYSTEM_ACTORS:
        return email
    resolver = (AUDIT_CONTEXT.get() or {}).get("resolve_email")
    if not resolver:
        return ""
    try:
        found = resolver(sub) or ""
        return found if "@" in found else ""
    except Exception:  # noqa: BLE001 - display only
        return ""


def project_for_super_admin(row: dict) -> dict:
    """Every v2 field except `search`, plus the index keys, label and vault flag."""
    r = _normalise_v1(row)
    out = {k: v for k, v in r.items() if k != "search"}
    out["entity"] = r.get("entity", "")
    out["ts"] = r.get("ts", "")
    out["action_label"] = action_label(r.get("action"))
    out["category"] = r.get("category") or action_category(r.get("action"))
    out["has_vault_copy"] = r.get("vault") == "stored"
    out["actor_email"] = _display_email(r)
    out.setdefault("fanout", False)
    return out


def _owner_diff(action: str, diff) -> dict:
    keys = _owner_keys(action)
    if not keys or not isinstance(diff, dict):
        return {}
    out = {k: diff[k] for k in keys if k in diff}
    if "member_count" in keys and isinstance(diff.get("member_space_ids"), list):
        out["member_count"] = len(diff["member_space_ids"])
    return out


def project_for_owner(row: dict, space_id: str, *, partition_ok: bool = False,
                      space_name=None):
    """Allowlist projection for a space's creator. `space_id`/`space_name` come from the
    request, never from the row. Returns None for a row that belongs to another space
    (unless partition_ok: the caller fetched it from partition `space:<space_id>`)."""
    r = _normalise_v1(row)
    if not partition_ok and r.get("space_id") != space_id and not r.get("fanout"):
        return None
    action = r.get("action") or ""
    email = _display_email(r)
    out = {
        "event_id": r.get("event_id") or "",
        "ts": r.get("ts") or "",
        "actor_email": email,
        "actor_role": r.get("actor_role") or "",
        "action": action,
        "action_label": action_label(action),
        "category": r.get("category") or action_category(action),
        "target_type": r.get("target_type") or "",
        "target_id": r.get("target_id") or "",
        "target_label": r.get("target_label") or r.get("target_id") or "",
        "on_other_owner": bool(r.get("on_other_owner")),
        "vault": r.get("vault") or "",
        "has_vault_copy": r.get("vault") == "stored",
        "fanout": bool(r.get("fanout")),
        "space_id": space_id,
        "space_name": _space_info(space_id)[0] if space_name is None else space_name,
        "before": _owner_diff(action, r.get("before")),
        "after": _owner_diff(action, r.get("after")),
    }
    if not email:
        out["actor_sub"] = r.get("actor_sub") or ""
    return out


# ── Cursor ───────────────────────────────────────────────────────────────────
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
CURSOR_MAX = 2048


def encode_cursor(month, key) -> str:
    raw = json.dumps({"m": month, "k": key}, sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str, *, gsi: bool = False) -> tuple:
    """(m, k) from an opaque cursor. Strict: anything unexpected raises ValueError."""
    if not isinstance(cursor, str) or not cursor or len(cursor) > CURSOR_MAX:
        raise ValueError("Invalid cursor")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        raise ValueError("Invalid cursor") from None
    if not isinstance(data, dict) or set(data) != {"m", "k"}:
        raise ValueError("Invalid cursor")
    m, k = data["m"], data["k"]
    if m is not None and not (isinstance(m, str) and _MONTH_RE.match(m)):
        raise ValueError("Invalid cursor")
    if k is not None:
        want = {"entity", "ts", "month"} if gsi else {"entity", "ts"}
        if not isinstance(k, dict) or set(k) != want or \
                not all(isinstance(v, str) and v for v in k.values()):
            raise ValueError("Invalid cursor")
        if gsi and not _MONTH_RE.match(k["month"]):
            raise ValueError("Invalid cursor")
    return m, k


def cursor_partition_ok(key, partition: str) -> bool:
    """A base-partition cursor must name the queried partition (no cross-partition replay)."""
    return key is None or key.get("entity") == partition


# ── Query bounds and the month walk ──────────────────────────────────────────
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_bound(value):
    """(utc datetime, is_date) for an ISO date or datetime (str/date/datetime), or None.
    Raises ValueError on a bad value."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt, is_date = value, False
    elif isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc), True
    else:
        s = str(value).strip()
        if _DATE_RE.match(s):
            return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc), True
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt, is_date = datetime.fromisoformat(s), False
    dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0), is_date


def ts_bounds(from_ts=None, to_ts=None) -> tuple:
    """(lo, hi, from_dt, to_dt): KeyCondition string bounds for `ts BETWEEN lo AND hi`.
    A date `to` is exclusive at the next midnight; a datetime `to` adds 1 s."""
    lo = hi = None
    f = parse_bound(from_ts)
    t = parse_bound(to_ts)
    if f:
        lo = f"{f[0]:%Y-%m-%dT%H:%M:%S}"
    if t:
        end = t[0] + (timedelta(days=1) if t[1] else timedelta(seconds=1))
        hi = f"{end:%Y-%m-%dT%H:%M:%S}"
    if f and t and f[0] > t[0]:
        raise ValueError("from is after to")
    return lo, hi, (f[0] if f else None), (t[0] if t else None)


def month_of(dt: datetime) -> str:
    return f"{dt:%Y-%m}"


def prev_month(m: str) -> str:
    y, mo = int(m[:4]), int(m[5:7])
    return f"{y - 1:04d}-12" if mo == 1 else f"{y:04d}-{mo - 1:02d}"


def walk_bounds(from_dt=None, to_dt=None, cursor_month=None) -> tuple:
    """(first, last) month of the global walk, newest to oldest, in UTC."""
    now = _now()
    first = month_of(min(to_dt, now)) if to_dt else month_of(now)
    if cursor_month:
        first = cursor_month
    floor = now - timedelta(days=RETENTION_MAX_DAYS)
    last = month_of(max(from_dt, floor)) if from_dt else month_of(floor)
    return first, last


# ── Verify ───────────────────────────────────────────────────────────────────
class AuditRecordNotFound(Exception):
    """The index row does not exist (HTTP 404)."""


class AuditIndexReadError(Exception):
    """The index could not be read (HTTP 502 "Could not read the audit log")."""


class AuditVaultReadError(Exception):
    """The vault could not be read (HTTP 502 "Could not read the audit vault")."""


def _err_code(e: Exception) -> str:
    return str(((getattr(e, "response", None) or {}).get("Error") or {}).get("Code") or "")


def _iso(v):
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).isoformat()
    return v or None


def _parse_dt(v):
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v or "").split("#", 1)[0]
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _missing_or_expired(row: dict, entity: str, ts: str) -> dict:
    ru = row.get("retain_until")
    if ru:
        try:
            if _now() >= _parse_dt(ru):
                logger.info("audit verify: vault copy expired entity=%s ts=%s retain_until=%s",
                            entity, ts, ru)
                return {"status": "expired", "retain_until": ru}
        except ValueError:
            pass
    logger.warning("audit verify: vault copy missing entity=%s ts=%s", entity, ts)
    return {"status": "missing"}


def get_index_row(entity: str, ts: str) -> dict:
    try:
        resp = _client("dynamodb").get_item(
            TableName=table_name("audit"),
            Key={"entity": {"S": entity}, "ts": {"S": ts}})
    except Exception as e:  # noqa: BLE001
        logger.warning("audit verify: index read failed (%s)", type(e).__name__)
        raise AuditIndexReadError(type(e).__name__) from None
    if not resp.get("Item"):
        raise AuditRecordNotFound(entity)
    return from_item(resp["Item"])


def verify_row(entity: str, ts: str) -> dict:
    """Prove the index row (entity, ts) against its vault object (§5.4 steps 1-8).
    Raises AuditRecordNotFound / AuditIndexReadError / AuditVaultReadError; otherwise
    returns {"status", "reasons"?, "retain_until", "lock_mode", "record"?, "s3_key"?,
    "vault"?, "row"} where status is verified | mismatch | missing | expired |
    not_in_vault. The caller projects `record` for owners and drops `row`/`s3_key`."""
    row = get_index_row(entity, ts)
    if not row.get("event_id"):
        return {"status": "not_in_vault", "vault": "legacy", "row": row}
    derived = derive_key(row.get("ts"), row["event_id"])
    reasons = []
    s3 = _client("s3")
    if not row.get("s3_key"):
        if not AUDIT_BUCKET:
            return {"status": "not_in_vault", "vault": row.get("vault") or "", "row": row}
        try:
            s3.head_object(Bucket=AUDIT_BUCKET, Key=derived)
            reasons.append("index_hides_vault_copy")
        except Exception as e:  # noqa: BLE001
            if _err_code(e) in ("404", "NoSuchKey", "NotFound"):
                return {"status": "not_in_vault", "vault": row.get("vault") or "", "row": row}
            logger.warning("audit verify: vault head failed (%s)", type(e).__name__)
            raise AuditVaultReadError(type(e).__name__) from None
    elif row["s3_key"] != derived:
        reasons.append("key_not_derived")
    if not AUDIT_BUCKET:
        raise AuditVaultReadError("AUDIT_BUCKET is empty")
    args = {"Bucket": AUDIT_BUCKET, "Key": derived, "ChecksumMode": "ENABLED"}
    if row.get("s3_key") and row.get("s3_version_id"):
        args["VersionId"] = row["s3_version_id"]
    try:
        obj = s3.get_object(**args)
        body = obj["Body"].read()
    except Exception as e:  # noqa: BLE001
        if _err_code(e) in ("NoSuchKey", "NoSuchVersion"):
            out = _missing_or_expired(row, entity, ts)
            out["row"] = row
            return out
        logger.warning("audit verify: vault read failed (%s)", type(e).__name__)
        raise AuditVaultReadError(type(e).__name__) from None

    digest = hashlib.sha256(body)
    if digest.hexdigest() != row.get("sha256"):
        reasons.append("hash")
    if base64.b64encode(digest.digest()).decode() != obj.get("ChecksumSHA256"):
        reasons.append("s3_checksum")
    try:
        lm = obj.get("LastModified")
        if lm is None or abs((_parse_dt(lm) - _parse_dt(row.get("ts"))).total_seconds()) > 300:
            reasons.append("timestamp")
    except ValueError:
        reasons.append("timestamp")
    try:
        versions = s3.list_object_versions(Bucket=AUDIT_BUCKET, Prefix=derived).get("Versions") or []
    except Exception as e:  # noqa: BLE001
        logger.warning("audit verify: vault listing failed (%s)", type(e).__name__)
        raise AuditVaultReadError(type(e).__name__) from None
    if len([v for v in versions if v.get("Key") == derived]) != 1:
        reasons.append("single_version")
    try:
        record = json.loads(body.decode("utf-8"))
        if not isinstance(record, dict):
            raise ValueError("record is not an object")
    except ValueError:
        record = None
    if record is None or not _fields_match(row, record):
        reasons.append("fields")

    out = {"status": "mismatch" if reasons else "verified",
           "retain_until": _iso(obj.get("ObjectLockRetainUntilDate")),
           "lock_mode": obj.get("ObjectLockMode") or None,
           "record": record, "s3_key": derived, "row": row}
    if reasons:
        out["reasons"] = reasons
        logger.error("AUDIT_INDEX_MISMATCH entity=%s ts=%s reasons=%s",
                     entity, ts, ",".join(reasons))
    return out


def _fields_match(row: dict, record: dict) -> bool:
    if row.get("event_id") != record.get("event_id"):
        return False
    if (row.get("ts") or "").split("#", 1)[0] != record.get("ts"):
        return False
    for f in ("actor_sub", "action", "category", "target_id", "target_type"):
        if row.get(f) != record.get(f):
            return False
    for f in ("before", "after"):
        if row.get(f) != {"_truncated": True} and row.get(f) != record.get(f):
            return False
    if row.get("fanout"):
        return row.get("space_id") in (record.get("space_ids") or [])
    for f in ("space_id", "space_name", "owner_admin", "on_other_owner"):
        if row.get(f) != record.get(f):
            return False
    return True
