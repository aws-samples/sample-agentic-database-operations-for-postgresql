# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Cross-account session factory for agent tools.

When the agent operates on a database in a secondary account, all AWS API calls
need to use the cross-account role. This module looks up the database record,
finds the role name from the space config, assumes the role, and returns a
boto3 Session.
"""

import os
import re
import time
import logging
import boto3
from tools.resource_names import table_name as resolve_table_name
from tools.aws.tenancy import record_in_space, record_holds_secret

logger = logging.getLogger("session-factory")

_agent_account_id = None
_sts_client = None
# Cache: DB record lookups (identifier → account_id, region, space_id)
# These don't change between tool calls within a session.
# Keyed by (caller space, lookup value) — NOT by the lookup value alone. The same
# display_name legitimately resolves to a different row in a different space, so a
# space-blind key would hand one space's record to a caller in another.
#
# Entries are (record, expiry) and expire after _DB_RECORD_CACHE_TTL seconds. Membership
# changes (add_member / remove_member) happen in the backend process, but this cache lives
# in the agent-runtime process, so it cannot be invalidated synchronously across the two.
# The TTL bounds how long a stale membership decision can survive here — a revoked space
# keeps agent access for at most the TTL, then the record is re-fetched and re-checked.
_DB_RECORD_CACHE_TTL = int(os.getenv("DB_RECORD_CACHE_TTL", "300"))
_db_record_cache: dict[str, tuple[dict, float]] = {}


def _caller_context() -> tuple[str, str]:
    """``(user, space)`` for the current agent request, or ``('', '')`` outside one.

    /invocations publishes both on ContextVars after validating the space against the
    caller's group membership. An empty user means this is not an agent request — the
    batch and Tier-1 paths, and the REST handlers, which carry their own ownership
    checks (``_verify_instance_in_space``).
    """
    try:
        from model_resolver import CURRENT_SPACE_ID, CURRENT_USER_ID
        return CURRENT_USER_ID.get() or "", CURRENT_SPACE_ID.get() or ""
    except Exception:
        return "", ""


def _get_sts():
    global _sts_client
    if _sts_client is None:
        _sts_client = boto3.client("sts", region_name=os.getenv("AWS_REGION", "us-east-1"))
    return _sts_client


def _get_agent_account_id():
    global _agent_account_id
    if _agent_account_id is None:
        _agent_account_id = _get_sts().get_caller_identity()["Account"]
    return _agent_account_id


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def external_id_for_space(space_id: str) -> str:
    """The ``sts:ExternalId`` this deployment presents when assuming a role for a space:
    the id of the admin who OWNS the space (``owner_admin`` on the space row).

    The single reason this is safe is that it is derived from the space the request has
    already been authorised against, never from anything the caller supplied. A request
    made for one of admin A's spaces always presents A's id, so a role whose trust policy
    requires admin B's id refuses it, whatever account and role name were bound. The far
    side (the customer's trust policy) decides which admin's spaces a role answers to.
    Empty when the space is unknown, which every caller treats as "cannot assume"."""
    space = _lookup_space(space_id)
    return ((space or {}).get("owner_admin") or (space or {}).get("created_by") or "").strip()


# space_id -> (space row, expiry). Same TTL as the DB record cache, for the same reason:
# role names change in the backend process and cannot be invalidated here, so the TTL
# bounds how long a stale row survives. Bounded so a long-lived runtime that sees many
# spaces cannot grow it without limit.
_SPACE_CACHE_MAX = 256
_space_cache: dict = {}


def _cache_space(space_id: str, space: dict, now: float) -> None:
    """Insert into `_space_cache`, keeping it at most `_SPACE_CACHE_MAX` entries: when
    full, expired entries go first, then the ones closest to expiry."""
    if space_id not in _space_cache and len(_space_cache) >= _SPACE_CACHE_MAX:
        for sid in [s for s, (_, exp) in _space_cache.items() if exp <= now]:
            del _space_cache[sid]
        while len(_space_cache) >= _SPACE_CACHE_MAX:
            del _space_cache[min(_space_cache, key=lambda s: _space_cache[s][1])]
    _space_cache[space_id] = (space, now + _DB_RECORD_CACHE_TTL)


def _lookup_space(space_id: str) -> dict:
    """The space row, cached for _DB_RECORD_CACHE_TTL seconds: role names and owner
    change only through the admin console, and the agent runtime reads them on every
    connection. Empty or failed lookups are not cached."""
    space_id = (space_id or "").strip()
    if not space_id:
        return {}
    now = time.time()
    hit = _space_cache.get(space_id)
    if hit and hit[1] > now:
        return hit[0]
    try:
        table = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1")) \
            .Table(resolve_table_name("spaces"))
        space = table.get_item(Key={"space_id": space_id}).get("Item", {}) or {}
    except Exception as e:
        logger.warning(f"Space lookup failed for {space_id}: {e}")
        return {}
    if space:
        _cache_space(space_id, space, now)
    return space


def _fetch_candidates(secret_arn: str = None, instance_id: str = None,
                      identifier: str = None) -> list[dict]:
    """Every registry row matching the lookup, before any authorization decision.

    ``identifier`` is documented as a display name, but the tools that take it are also
    handed an instance id — the agent passes whichever it is holding, and both look like
    a name from the tool signature. A UUID-shaped identifier is therefore resolved as an
    instance id, since no display name is a UUID: an RDS identifier cannot contain the
    two consecutive hyphens a UUID has, so the two key spaces cannot collide.

    Fail-soft: a DynamoDB failure yields no candidates rather than an exception, so a
    transient registry problem degrades to "target not resolved".
    """
    if not instance_id and identifier and _UUID_RE.match(identifier.strip()):
        instance_id, identifier = identifier.strip(), None
    try:
        table_name = resolve_table_name("instances")
        dynamodb = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1"))
        table = dynamodb.Table(table_name)

        if instance_id:
            item = table.get_item(Key={"instance_id": instance_id}).get("Item")
            return [item] if item else []

        if secret_arn:
            # Active credential OR any owner's proof (per-space secrets live in the
            # `space_secrets` map, which a FilterExpression cannot search by value).
            items, kw = [], {}
            while True:
                page = table.scan(**kw)
                items.extend(i for i in page.get("Items", []) if record_holds_secret(i, secret_arn))
                if "LastEvaluatedKey" not in page:
                    break
                kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
            return items
        elif identifier:
            # Every page: a filtered scan returns only the matches within each page, so
            # stopping at the first one misses rows (and hides a duplicate name).
            items, kw = [], {
                "FilterExpression": "#dn = :val",
                "ExpressionAttributeNames": {"#dn": "display_name"},
                "ExpressionAttributeValues": {":val": identifier},
            }
            while True:
                page = table.scan(**kw)
                items.extend(page.get("Items", []) or [])
                if "LastEvaluatedKey" not in page:
                    break
                kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
            return items
        else:
            return []
    except Exception as e:
        logger.warning(f"DB record lookup failed: {e}")
        return []


def _lookup_db_record(secret_arn: str = None, instance_id: str = None,
                      identifier: str = None) -> dict:
    """Resolve a database record from the registry, scoped to the caller's space.

    The registry is one table holding every space's rows, and the lookup keys a caller
    can supply (``display_name``, ``secret_arn``) are NOT unique across it — the same
    physical database is deliberately registered once per space that has access to it
    (see the per-space de-duplication in the discovery handler). So an unfiltered scan
    can match rows belonging to several spaces, and taking the first of them would both
    leak across tenants and resolve non-deterministically for a legitimate caller.

    Every observability tool (CloudWatch, Performance Insights, CloudTrail, CloudWatch
    Logs) funnels through here, so this is the one place the check belongs. It mirrors
    ``pg_client._enforce_secret_space_ownership``, which does the same for the SQL
    path — same fail modes, pinned by the same style of test:

      * agent request, space resolves, row belongs to it   → allow
      * agent request, space resolves, row belongs elsewhere → refuse
      * agent request, no resolved space                   → refuse (fail closed)
      * not an agent request (no CURRENT_USER_ID)          → pass through; batch,
        Tier-1 and the REST handlers carry their own ownership checks

    A row that does not exist and a row in another space are deliberately answered the
    same way, so the refusal does not confirm that an out-of-space database is real.
    """
    user, space = _caller_context()
    agent_request = bool(user)

    # A database alias from the scope (db-1) becomes its registry id BEFORE the cache
    # key is formed: the same alias names a different database in another request.
    from tools.aws.db_aliases import resolve_handle
    instance_id = resolve_handle(instance_id)
    identifier = resolve_handle(identifier)

    lookup = secret_arn or instance_id or identifier or ""
    cache_key = f"{space}|{lookup}" if lookup else ""
    if cache_key:
        cached = _db_record_cache.get(cache_key)
        if cached and cached[1] > time.time():
            return cached[0]

    if agent_request and not space:
        logger.warning(
            "audit: database resolution refused — no resolved space in an agent request "
            "(user=%s, lookup=%s)", user, lookup or "none",
        )
        raise PermissionError("This database is not accessible in the current space.")

    candidates = _fetch_candidates(secret_arn=secret_arn, instance_id=instance_id,
                                   identifier=identifier)

    if agent_request:
        # Membership is the tenancy authority: a caller may resolve a database if their
        # space is one of its member spaces, so a database shared across spaces works in
        # the agent for every member, not only the space that first discovered it.
        owned = [c for c in candidates if record_in_space(c, space)]
        if not owned:
            # The caller is told the same thing either way, so a refusal never confirms
            # that an out-of-space database exists. The LOG separates them, because
            # "nothing matched this lookup" and "it belongs to another space" need
            # different fixes and only one of them is a tenancy event.
            if candidates:
                logger.warning(
                    "audit: cross-space database access denied — lookup=%s matched %d "
                    "row(s), none in requested_space=%s (user=%s)",
                    lookup or "none", len(candidates), space, user,
                )
            else:
                logger.warning(
                    "database target not resolved — lookup=%s matched no registry row "
                    "(space=%s, user=%s). Not a tenancy refusal: check the lookup key "
                    "and that the instance is registered.",
                    lookup or "none", space, user,
                )
            raise PermissionError("This database is not accessible in the current space.")
        candidates = owned
        if identifier and not instance_id and not secret_arn \
                and len({c.get("instance_id") for c in candidates}) > 1:
            # A display name is a label, not an identity: two databases in one space may
            # share it (same RDS name in two Regions or accounts, or a rename). Picking
            # the first would read the other database's metrics, so refuse and name the
            # handle that is unique.
            raise LookupError(
                f"More than one database in this space is named \"{identifier}\". Pass the "
                f"database's alias from [DATABASE SCOPE] instead of its name.")

    record = candidates[0] if candidates else {}
    if record and cache_key:
        _db_record_cache[cache_key] = (record, time.time() + _DB_RECORD_CACHE_TTL)
    return record


def rds_identifier(record: dict, fallback: str = "") -> str:
    """The RDS cluster or instance identifier of a registry record: what CloudWatch,
    Performance Insights, CloudTrail and the log groups know the database by.

    Never the display name, which a rename changes freely. Discovery stores the RDS name
    as ``tags.raw_identifier``; the ARN's last segment is the same name. ``fallback`` (the
    caller's handle) is used only when the handle resolved to no record, so a tool called
    outside an agent request with an RDS identifier keeps working."""
    if not record:
        return fallback
    tags = record.get("tags") or {}
    raw = (tags.get("raw_identifier") or "").strip()
    if raw:
        return raw
    arn = (tags.get("arn") or "").strip()
    for kind in (":cluster:", ":db:"):
        if kind in arn:
            return arn.rsplit(kind, 1)[1]
    return record.get("display_name") or fallback


def resolve_target(handle: str) -> dict:
    """One database, resolved for the observability tools: ``{"record", "rds_id",
    "account_id", "region"}``.

    ``handle`` is the database alias from [DATABASE SCOPE] (``db-1``), which
    ``_lookup_db_record`` turns into the registry id for this request; the registry id
    itself and a display name are still accepted (a name is refused when it names more
    than one database in the space).
    The record decides the account and Region, so a database in another Region is read
    there and never in the deployment's Region. Authorization is ``_lookup_db_record``'s
    (the caller's space)."""
    record = _lookup_db_record(identifier=handle) if handle else {}
    return {
        "record": record,
        "rds_id": rds_identifier(record, handle),
        "account_id": (record or {}).get("account_id", ""),
        "region": (record or {}).get("region") or os.getenv("AWS_REGION", "us-east-1"),
    }


def _lookup_role_name(account_id: str, space_id: str) -> str:
    """The customer-installed role name a space uses in `account_id`, or ''."""
    return ((_lookup_space(space_id).get("secondary_role_names") or {}).get(account_id, "") or "").strip()


def get_session(secret_arn: str = None, instance_id: str = None,
                identifier: str = None, region: str = None, *,
                credential_resolved: bool = False) -> boto3.Session:
    """Get a boto3 Session for the database's account and region.

    Raises ``PermissionError`` when called from an agent request for a database outside
    the caller's validated space — see ``_lookup_db_record``. The caller supplies only a
    name; the credentials are minted here from the registry, which makes this a
    privilege-granting step rather than a lookup, and therefore the right place for the
    authorization decision.

    ``credential_resolved=True`` says ``secret_arn`` is already the ARN that
    ``pg_client.resolve_credential`` returned for this request (pg_client's own
    connection path), so it is not resolved a second time. Resolving it again would
    treat our own resolved ARN as model input and refuse it. Tools never pass this:
    a value that came from the model is always resolved, so a raw ARN from the model
    is still refused.
    """
    # The model's tools hand in the database id as the credential handle (never an ARN);
    # resolve it to the credential the calling space is entitled to so the role assumed
    # below is that credential's owner. ARNs still arrive from the monitoring engine.
    if secret_arn and not credential_resolved:
        from tools.aws.pg_client import resolve_credential
        secret_arn = resolve_credential(secret_arn)
    db_record = _lookup_db_record(secret_arn=secret_arn, instance_id=instance_id,
                                   identifier=identifier)
    if not db_record:
        return boto3.Session(region_name=region or os.getenv("AWS_REGION", "us-east-1"))

    db_account_id = db_record.get("account_id", "")
    db_region = region or db_record.get("region", os.getenv("AWS_REGION", "us-east-1"))

    # Same account — just use the right region
    agent_account = _get_agent_account_id()
    if not db_account_id or db_account_id == agent_account:
        return boto3.Session(region_name=db_region)

    # Cross-account — assume the role of the space whose CREDENTIAL this call uses. For
    # the monitoring engine (and any caller handing in the record's active secret) that
    # is the reach provider. For an agent turn the scope block carries the calling
    # space's own secret, or the one its granting owner lent it, so the role is that
    # owner's: a space never reaches a database under another space's role unless that
    # space chose to lend its credential. Legacy records fall back to the frozen tag.
    space_id = ""
    if secret_arn:
        for sid, arn in (db_record.get("space_secrets") or {}).items():
            if arn == secret_arn:
                space_id = sid
                break
    if not space_id:
        space_id = db_record.get("reach_space_id", "")
    role_name = _lookup_role_name(db_account_id, space_id) if space_id else ""
    if not role_name:
        # FAIL CLOSED: a cross-account database with no reachable role must raise, not
        # fall back to a deployment-account session. A fallback would run the subsequent
        # AWS calls against the DEPLOYMENT account instead of the target, so a refused or
        # unconfigured cross-account reach would return empty or wrong-account data with
        # no signal that access was missing.
        logger.warning(f"No role for account {db_account_id} in space {space_id}")
        raise PermissionError(
            f"The agent has no configured cross-account role to reach account "
            f"{db_account_id} for this space. Configure cross-account access for the space, "
            f"then retry."
        )

    role_arn = f"arn:aws:iam::{db_account_id}:role/{role_name}"
    try:
        creds = _get_sts().assume_role(
            RoleArn=role_arn,
            RoleSessionName=f"agent-{db_account_id}-{db_region}",
            DurationSeconds=900,
            ExternalId=external_id_for_space(space_id),
        )["Credentials"]
        logger.info(f"Assumed role {role_name} in {db_account_id}/{db_region}")
        session = boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=db_region,
        )
        # Tag session with context for trace visibility
        session._cross_account_context = {
            "account_id": db_account_id,
            "role": role_name,
            "region": db_region,
            "type": "cross-account",
        }
        return session
    except Exception as e:
        # FAIL CLOSED: a denied or failed cross-account assume must raise, not fall
        # back to a deployment-account session. The role may be missing, its trust may not
        # name this deployment/space (ExternalId), or the space admin may have revoked
        # access; in every case the honest outcome is an error naming the account, not
        # silent wrong-account results. The full cause is logged for diagnosis.
        logger.error(f"Failed to assume role {role_arn}: {e}")
        raise PermissionError(
            f"Could not assume the cross-account role in account {db_account_id} "
            f"(region {db_region}). The role may be missing, its trust policy may not name "
            f"this deployment and space, or access was revoked."
        ) from e


def get_session_context(session: boto3.Session) -> dict:
    """Get cross-account context from a session, if any."""
    return getattr(session, "_cross_account_context", {"type": "local"})
