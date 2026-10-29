# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Model & knowledge governance store.

Backs the model-and-knowledge-governance feature with the pieces that are NOT
space-scoped (per-space model/knowledge governance lives on the space item via
SpacesRegistry):

- Account model catalog / allowlist + account default  → config item `model_catalog`
- Per-user knowledge layer (which inherited custom docs the user disables)
                                                        → `<ResourcePrefix>-user-knowledge`
- Governance audit trail                                → `<ResourcePrefix>-audit`

Custom knowledge *documents* (account/space/user) all live in S3 (see
tools.aws.knowledge_store); DynamoDB only tracks enablement (the per-layer
`disabled_items` sets). Shipped built-ins are always-on/read-only and are not
tracked here. Custom skills are not supported yet (built-in skills only).

Mirrors the SpacesRegistry client style (lazy boto3 table handles). All reads are
fail-soft (return an empty default on error) so a governance-store hiccup never
breaks a chat turn — resolution falls back to the env default.
"""

import os
import logging
from datetime import datetime, timezone

import boto3
from tools.aws import audit_log
from tools.resource_names import table_name

logger = logging.getLogger("governance-store")

_MODEL_CATALOG_KEY = "model_catalog"
_MODEL_META_FIELDS = ("recommended", "cost_tier", "display_name", "verified", "permitted")
_EMAIL_SENDER_KEY = "email_sender"
_RATE_LIMIT_KEY = "rate_limit"
# Space-admin cascade tier: one config item per space admin (keyed by their sub),
# applied to every space they manage. Cascade semantics are disable-based —
# anything enabled above flows down unless a layer disables it — so the record
# holds `disabled_models` and knowledge `disabled_items` (their ADDED docs live
# in S3 under knowledge_store.admin_prefix(sub)).
_ADMIN_GOVERNANCE_PREFIX = "admin_governance:"
# Invite lifecycle marker: one item per invited email, recording that the
# credentials email was actually dispatched (Cognito itself has no such state).
_INVITE_SENT_PREFIX = "invite_sent:"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GovernanceRegistry:
    """Account catalog, per-user knowledge, and audit for model/knowledge governance."""

    def __init__(self):
        self._resource = None
        self._config = None
        self._user_knowledge = None

    @property
    def _dynamodb(self):
        if self._resource is None:
            self._resource = boto3.resource(
                "dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1")
            )
        return self._resource

    @property
    def config_table(self):
        if self._config is None:
            self._config = self._dynamodb.Table(table_name("config"))
        return self._config

    @property
    def user_knowledge_table(self):
        if self._user_knowledge is None:
            self._user_knowledge = self._dynamodb.Table(
                table_name("user_knowledge")
            )
        return self._user_knowledge

    # ── Model catalog (account allowlist + default) ──────────────────────────
    def get_model_catalog(self) -> dict:
        try:
            item = self.config_table.get_item(
                Key={"config_key": _MODEL_CATALOG_KEY}
            ).get("Item")
        except Exception as e:
            logger.warning(f"get_model_catalog failed: {e}")
            return {"config_key": _MODEL_CATALOG_KEY, "account_default_model": "", "models": {}}
        if item:
            return item
        # No catalog stored yet (a fresh install): the deployment's model starts enabled,
        # so the console shows the model requests actually use. Nothing is written until
        # an admin changes the catalog, which then stores this entry with their change.
        # No account default is stored, so the console reports it as the deployment's.
        deployed = os.getenv("AGENT_MODEL_ID", "").strip()
        models = {deployed: {"enabled": True}} if deployed else {}
        return {"config_key": _MODEL_CATALOG_KEY, "account_default_model": "", "models": models}

    def put_model_catalog(self, catalog: dict) -> dict:
        catalog["config_key"] = _MODEL_CATALOG_KEY
        catalog["updated_at"] = _now()
        self.config_table.put_item(Item=catalog)
        return catalog

    def set_model_enabled(self, model_id: str, enabled: bool, actor: str, metadata: dict = None) -> dict:
        """Enable/disable a model in the account allowlist (+ optional metadata)."""
        catalog = self.get_model_catalog()
        models = catalog.get("models", {})
        entry = dict(models.get(model_id, {}))
        before = dict(entry)
        entry["enabled"] = bool(enabled)
        if metadata:
            entry.update({k: v for k, v in metadata.items() if k in _MODEL_META_FIELDS})
        entry["updated_by"] = actor
        entry["updated_at"] = _now()
        models[model_id] = entry
        catalog["models"] = models
        self.put_model_catalog(catalog)
        self.audit(actor, "model_enable" if enabled else "model_disable",
                   model_id, "account", before, entry)
        return catalog

    def set_model_metadata(self, model_id: str, metadata: dict, actor: str) -> dict:
        """Update advisory metadata (recommended/cost_tier/verified/permitted) for a model."""
        catalog = self.get_model_catalog()
        models = catalog.get("models", {})
        entry = dict(models.get(model_id, {}))
        before = dict(entry)
        entry.update({k: v for k, v in (metadata or {}).items() if k in _MODEL_META_FIELDS})
        entry["updated_by"] = actor
        entry["updated_at"] = _now()
        models[model_id] = entry
        catalog["models"] = models
        self.put_model_catalog(catalog)
        self.audit(actor, "model_metadata", model_id, "account", before, entry)
        return catalog

    def set_account_default_model(self, model_id: str, actor: str) -> dict:
        catalog = self.get_model_catalog()
        before = catalog.get("account_default_model", "")
        catalog["account_default_model"] = model_id
        self.put_model_catalog(catalog)
        self.audit(actor, "account_default_model", model_id, "account", before, model_id)
        return catalog

    # ── Email sender (account SES identity for Cognito invite emails) ────────
    # Source of truth for the deployment's outbound email sender. The Cognito
    # pool's EmailConfiguration is applied FROM this record at runtime (see
    # tools.aws.email_sender); storing it here means a stack update that resets
    # the pool can be reconciled back to the admin's chosen sender.
    def get_email_sender(self) -> dict:
        try:
            item = self.config_table.get_item(
                Key={"config_key": _EMAIL_SENDER_KEY}
            ).get("Item")
        except Exception as e:
            logger.warning(f"get_email_sender failed: {e}")
            item = None
        return item or {
            "config_key": _EMAIL_SENDER_KEY,
            "from_address": "", "from_name": "", "source_arn": "", "region": "",
            "configured": False,
        }

    def put_email_sender(self, sender: dict, actor: str) -> dict:
        """Persist the account email sender (+ audit). `sender` carries
        from_address / from_name / source_arn / region."""
        before = self.get_email_sender()
        item = {
            "config_key": _EMAIL_SENDER_KEY,
            "from_address": (sender.get("from_address") or "").strip().lower(),
            "from_name": (sender.get("from_name") or "").strip(),
            "source_arn": sender.get("source_arn", ""),
            "region": sender.get("region", ""),
            "updated_by": actor,
            "updated_at": _now(),
        }
        item["configured"] = bool(item["from_address"])
        self.config_table.put_item(Item=item)
        self.audit(actor, "email_sender_set", item["from_address"], "account",
                   {k: before.get(k) for k in ("from_address", "source_arn", "region")},
                   {k: item.get(k) for k in ("from_address", "source_arn", "region")})
        return item

    # ── Per-caller rate limit (account-wide ceiling for the model paths) ─────
    # A single account-wide integer: the per-minute request ceiling the runaway
    # guard (utils.rate_limit) applies to each authenticated caller. Stored here so
    # a super admin can retune it from the console without a redeploy; the limiter
    # reads it with a short cache and falls back to the env default if unset or on
    # any store error (the guard is fail-open, never a security boundary). An unset
    # record means "use the deployment default", not zero.
    def get_rate_limit(self) -> dict:
        try:
            item = self.config_table.get_item(
                Key={"config_key": _RATE_LIMIT_KEY}
            ).get("Item")
        except Exception as e:
            logger.warning(f"get_rate_limit failed: {e}")
            item = None
        return item or {"config_key": _RATE_LIMIT_KEY, "per_minute": None, "configured": False}

    def put_rate_limit(self, per_minute: int, actor: str) -> dict:
        """Persist the per-caller per-minute ceiling (+ audit). A floor of 1 keeps a
        misconfigured value from blocking every caller."""
        before = self.get_rate_limit()
        value = max(1, int(per_minute))
        item = {
            "config_key": _RATE_LIMIT_KEY,
            "per_minute": value,
            "configured": True,
            "updated_by": actor,
            "updated_at": _now(),
        }
        self.config_table.put_item(Item=item)
        self.audit(actor, "rate_limit_set", str(value), "account",
                   {"per_minute": before.get("per_minute")}, {"per_minute": value})
        return item

    # ── Alarm settings (email alarms on the collected metrics) ───────────────
    # One config item `alarm_settings`: enabled (all alarms on/off) and alarms
    # ({alarm id: bool}). Absent means the defaults (on, each alarm at its catalogue
    # default; tools.aws.db_alarms.resolve_settings). Reads raise, so the settings
    # endpoints can tell "could not read" from "never set".
    def get_alarm_settings(self):
        from tools.aws.db_alarms import SETTINGS_KEY
        return self.config_table.get_item(Key={"config_key": SETTINGS_KEY}).get("Item")

    def put_alarm_settings(self, enabled: bool, alarms: dict, actor: str) -> dict:
        from tools.aws.db_alarms import SETTINGS_KEY
        item = {"config_key": SETTINGS_KEY, "enabled": bool(enabled),
                "alarms": {k: bool(v) for k, v in (alarms or {}).items()},
                "updated_by": actor or "unknown", "updated_at": _now()}
        self.config_table.put_item(Item=item)
        return item

    # ── Audit vault settings (runtime switch + retention) ────────────────────
    # One config item `audit_settings`: vault_enabled, retention_days, updated_by,
    # updated_at. Absent fields resolve to the deploy defaults (audit_log
    # .current_settings), so the item is first written by the first console change.
    # Unlike the readers above these raise: the settings endpoints map a store error
    # to 502, and put does not audit (the handler controls the order).
    def get_audit_settings(self):
        """The `audit_settings` item, or None when it is absent. Errors raise."""
        return self.config_table.get_item(
            Key={"config_key": audit_log.SETTINGS_KEY}).get("Item")

    def put_audit_settings(self, changes: dict, actor: str) -> dict:
        """Partial update of the changed fields only (never a whole-item write), so a
        retention-only save can never switch the vault off by omission. `changes` keys
        must be a non-empty subset of {vault_enabled, retention_days}. Errors raise."""
        allowed = {"vault_enabled", "retention_days"}
        if not isinstance(changes, dict) or not changes or not set(changes) <= allowed:
            raise ValueError("changes must be a non-empty subset of "
                             "{vault_enabled, retention_days}")
        values = {":a": actor or "unknown", ":t": _now()}
        sets = []
        for field in sorted(changes):
            value = changes[field]
            values[f":{field}"] = bool(value) if field == "vault_enabled" else int(value)
            sets.append(f"{field} = :{field}")
        resp = self.config_table.update_item(
            Key={"config_key": audit_log.SETTINGS_KEY},
            UpdateExpression="SET " + ", ".join(sets) + ", updated_by = :a, updated_at = :t",
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
        audit_log.invalidate_settings()
        return (resp or {}).get("Attributes") or {}

    # ── Invite-sent markers (credentials email actually dispatched) ──────────
    # Cognito has no "invite sent" state (FORCE_CHANGE_PASSWORD covers the whole
    # span from creation to first login), so the UI can't distinguish "waiting on
    # the admin to send" from "sent, waiting on the user". One marker item per
    # email records the send; cleared when the user is deleted.
    def mark_invite_sent(self, email: str, actor: str) -> None:
        try:
            self.config_table.put_item(Item={
                "config_key": f"{_INVITE_SENT_PREFIX}{(email or '').lower()}",
                "sent_at": _now(), "sent_by": actor or "unknown",
            })
        except Exception as e:
            logger.warning(f"mark_invite_sent({email}) failed: {e}")

    def clear_invite_sent(self, email: str) -> None:
        try:
            self.config_table.delete_item(
                Key={"config_key": f"{_INVITE_SENT_PREFIX}{(email or '').lower()}"})
        except Exception as e:
            logger.warning(f"clear_invite_sent({email}) failed: {e}")

    def invite_sent_map(self, emails: list) -> set:
        """Subset of `emails` (lowercased) with a recorded invite send. One
        BatchGetItem per 100 addresses; fail-soft (empty set on error)."""
        sent = set()
        keys = [{"config_key": f"{_INVITE_SENT_PREFIX}{e.lower()}"} for e in emails if e]
        try:
            for i in range(0, len(keys), 100):
                resp = self._dynamodb.batch_get_item(
                    RequestItems={self.config_table.name: {"Keys": keys[i:i + 100]}})
                for item in resp.get("Responses", {}).get(self.config_table.name, []):
                    sent.add(item["config_key"][len(_INVITE_SENT_PREFIX):])
        except Exception as e:
            logger.warning(f"invite_sent_map failed: {e}")
        return sent

    # ── Space-admin tier (per-admin disables for models + knowledge) ─────────
    def get_admin_governance(self, admin_sub: str) -> dict:
        """The space-admin tier record for one admin (fail-soft empty default)."""
        try:
            item = self.config_table.get_item(
                Key={"config_key": f"{_ADMIN_GOVERNANCE_PREFIX}{admin_sub}"}
            ).get("Item")
        except Exception as e:
            logger.warning(f"get_admin_governance({admin_sub}) failed: {e}")
            item = None
        return item or {
            "config_key": f"{_ADMIN_GOVERNANCE_PREFIX}{admin_sub}",
            "disabled_models": [],
            "disabled_items": [],
            "default_model": "",
        }

    def put_admin_governance(self, admin_sub: str, data: dict, actor: str) -> dict:
        """Persist an admin's tier record (+ audit). Only known fields are stored;
        fields absent from `data` keep their prior value."""
        before = self.get_admin_governance(admin_sub)
        item = {
            "config_key": f"{_ADMIN_GOVERNANCE_PREFIX}{admin_sub}",
            "disabled_models": list(data.get("disabled_models",
                                             before.get("disabled_models") or [])),
            "disabled_items": list(data.get("disabled_items",
                                            before.get("disabled_items") or [])),
            "default_model": data.get("default_model",
                                      before.get("default_model") or ""),
            "updated_by": actor,
            "updated_at": _now(),
        }
        self.config_table.put_item(Item=item)
        self.audit(actor, "admin_governance_update", admin_sub, f"admin:{admin_sub}",
                   {k: before.get(k) for k in ("disabled_models", "disabled_items", "default_model")},
                   {k: item.get(k) for k in ("disabled_models", "disabled_items", "default_model")})
        return item

    # ── Per-user knowledge (which inherited custom docs the user disables) ───
    # The user's OWN docs live in S3 (knowledge_store user prefix); DynamoDB only
    # records the disable set for this (user, space).
    def get_user_knowledge(self, user_sub: str, space_id: str) -> dict:
        try:
            item = self.user_knowledge_table.get_item(
                Key={"user_sub": user_sub, "space_id": space_id}
            ).get("Item")
        except Exception as e:
            logger.warning(f"get_user_knowledge failed: {e}")
            item = None
        return item or {
            "user_sub": user_sub,
            "space_id": space_id,
            "disabled_items": [],
        }

    def put_user_knowledge(self, user_sub: str, space_id: str, data: dict) -> dict:
        item = {
            "user_sub": user_sub,
            "space_id": space_id,
            "disabled_items": data.get("disabled_items", []),
            "updated_at": _now(),
        }
        self.user_knowledge_table.put_item(Item=item)
        return item

    # ── Audit ────────────────────────────────────────────────────────────────
    def audit(self, actor: str, action: str, target: str, scope: str,
              before=None, after=None, *, space_id=None, space_ids=None,
              target_label=None, target_type=None, space_name=None,
              owner_admin=None, force_vault=False):
        """Record an audit event through audit_log.write_event (vault first when on,
        then insert-only index rows). Never raises (audit must not break a mutation).
        Returns "stored" | "failed" | "off", or None on an unexpected error; callers
        other than the vault switch ignore it. `space_ids=` (explicit) fans an admin
        event out to each listed space's partition; `space_name=`/`owner_admin=` are
        for events whose space row is already gone."""
        return audit_log.write_event(
            actor, action, target, scope, before, after,
            space_id=space_id, space_ids=space_ids, target_label=target_label,
            target_type=target_type, space_name=space_name, owner_admin=owner_admin,
            force_vault=force_vault)

    def query_audit(self, partition, *, index=None, from_ts=None, to_ts=None,
                    filters=None, limit=50, cursor=None, exclude_space_partitions=False) -> dict:
        """Newest-first page of raw index rows (no Scan).

        Base partition (`partition`, e.g. "space:<id>"), or the month walk over the
        sparse `month-index` when `index` is set or `exclude_space_partitions` is True
        (the "account-level only" view: every admin partition but `space:*`). The
        admin-category filter always applies, so legacy v1 `query_plan_*` rows never
        surface. `filters` keys: actor (sub, or an email matched through `search`),
        action, q. Stops at `limit` rows, the partition/walk end, 10 Query responses
        with ScannedCount > 0, 130 Query calls, or 10 s of wall clock; the last three
        return has_more with a resume cursor. Raises ValueError for a bad cursor or
        bound (HTTP 400) and lets DynamoDB errors propagate (HTTP 502).

        Returns {"rows", "has_more", "next_before", "scanned_to"}."""
        walk = bool(index) or bool(exclude_space_partitions)
        index_name = index or ("month-index" if walk else None)
        limit = max(1, int(limit))
        lo, hi, from_dt, to_dt = audit_log.ts_bounds(from_ts, to_ts)
        m, k = audit_log.decode_cursor(cursor, gsi=walk) if cursor else (None, None)
        if walk:
            if k is not None and (k["month"] != m or (exclude_space_partitions
                                                      and k["entity"].startswith("space:"))):
                raise ValueError("Invalid cursor")
        else:
            if not partition:
                raise ValueError("partition is required without an index")
            if m is not None or not audit_log.cursor_partition_ok(k, partition):
                raise ValueError("Invalid cursor")

        names = {"#ts": "ts", "#action": "action", "#category": "category"}
        values = {":qpr": "query_plan_run", ":qpf": "query_plan_refused", ":admin": "admin"}
        if walk:
            names["#month"] = "month"
            key_cond = "#month = :m"
        else:
            names["#entity"] = "entity"
            values[":p"] = partition
            key_cond = "#entity = :p"
        if lo and hi:
            key_cond += " AND #ts BETWEEN :lo AND :hi"
            values.update({":lo": lo, ":hi": hi})
        elif lo:
            key_cond += " AND #ts >= :lo"
            values[":lo"] = lo
        elif hi:
            key_cond += " AND #ts <= :hi"
            values[":hi"] = hi
        else:
            names.pop("#ts")

        clauses = ["((attribute_not_exists(#category) AND NOT (#action IN (:qpr, :qpf)))"
                   " OR #category = :admin)"]
        f = filters or {}
        actor = (f.get("actor") or "").strip()
        if actor:
            if "@" in actor:
                names["#search"] = "search"
                values[":email"] = actor.lower()
                clauses.append("contains(#search, :email)")
            else:
                names["#actor_sub"] = "actor_sub"
                values[":a"] = actor
                clauses.append("#actor_sub = :a")
        if f.get("action"):
            values[":act"] = f["action"]
            clauses.append("#action = :act")
        q = (f.get("q") or "").strip().lower()[:100]
        if q:
            names["#search"] = "search"
            values[":q"] = q
            clauses.append("contains(#search, :q)")
        if exclude_space_partitions:
            names["#entity"] = "entity"
            values[":space"] = "space:"
            clauses.append("NOT begins_with(#entity, :space)")
        filter_expr = " AND ".join(clauses)

        if walk:
            first, last = audit_log.walk_bounds(from_dt, to_dt, m)
            if first < last:
                return {"rows": [], "has_more": False, "next_before": None, "scanned_to": None}
            cur_month = first
        else:
            cur_month = None
        esk = k
        client = audit_log.dynamodb_client()
        table = table_name("audit")
        start = audit_log._monotonic()
        rows, calls, pages, scanned_to = [], 0, 0, None

        def done(has_more, resume):
            return {"rows": rows, "has_more": has_more,
                    "next_before": audit_log.encode_cursor(*resume) if has_more else None,
                    "scanned_to": scanned_to}

        while True:
            params = {
                "TableName": table, "KeyConditionExpression": key_cond,
                "FilterExpression": filter_expr, "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": audit_log.to_item(
                    dict(values, **({":m": cur_month} if walk else {}))),
                "ScanIndexForward": False, "Limit": 100,
            }
            if index_name:
                params["IndexName"] = index_name
            if esk:
                params["ExclusiveStartKey"] = audit_log.to_item(esk)
            resp = client.query(**params)
            calls += 1
            if int(resp.get("ScannedCount") or 0) > 0:
                pages += 1
            items = [audit_log.from_item(i) for i in resp.get("Items") or []]
            lek = audit_log.from_item(resp["LastEvaluatedKey"]) if resp.get("LastEvaluatedKey") else None
            need = limit - len(rows)
            if len(items) > need:
                # Truncated mid-page: resume after the last RETURNED row, never at the
                # page's LastEvaluatedKey (that would skip the rest of this page).
                rows.extend(items[:need])
                last_row = rows[-1]
                key = {"entity": last_row.get("entity", ""), "ts": last_row.get("ts", "")}
                if walk:
                    key["month"] = last_row.get("month") or cur_month
                scanned_to = cur_month if walk else key["ts"]
                return done(True, (cur_month, key))
            rows.extend(items)
            if lek:
                if walk:
                    lek = {"entity": lek.get("entity", ""), "ts": lek.get("ts", ""),
                           "month": lek.get("month") or cur_month}
                    scanned_to = cur_month
                else:
                    lek = {"entity": lek.get("entity", ""), "ts": lek.get("ts", "")}
                    scanned_to = lek["ts"]
                resume = (cur_month, lek)
            elif walk:
                scanned_to = cur_month
                prev = audit_log.prev_month(cur_month)
                resume = (prev, None) if prev >= last else None
            else:
                if items:
                    scanned_to = items[-1].get("ts")
                resume = None
            if resume is None:
                return done(False, None)
            if (len(rows) >= limit or pages >= 10 or calls >= 130
                    or audit_log._monotonic() - start >= 10.0):
                return done(True, resume)
            cur_month, esk = resume


governance_registry = GovernanceRegistry()
