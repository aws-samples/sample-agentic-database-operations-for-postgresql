# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""PostgreSQL connection manager using AWS Secrets Manager for credentials."""

import os
import json
import re
import time
import logging
import psycopg2
import psycopg2.extras
import boto3
from tools.resource_names import table_name as resolve_table_name
from tools.aws.tenancy import record_holds_secret, spaces_entitled_to_secret
from tools.aws.db_tls import ssl_kwargs

logger = logging.getLogger("pg-client")

# secret_arn -> (secret_dict, expiry_epoch). TTL-bounded so a rotated secret is
# picked up within SECRETS_CACHE_TTL rather than served stale until process restart.
SECRETS_CACHE_TTL = int(os.getenv("SECRETS_CACHE_TTL", "300"))  # seconds
_secrets_cache: dict[str, tuple] = {}


def _get_secrets_client(secret_arn: str = None):
    """Get a Secrets Manager client, using cross-account session if needed.

    `secret_arn` is already resolved (get_db_credentials is only handed the ARN
    get_connection resolved), so the session factory must not resolve it again: inside
    an agent request a second resolve sees an ARN and refuses it as model input."""
    from tools.aws.session_factory import get_session
    session = get_session(secret_arn=secret_arn, credential_resolved=True)
    from botocore.config import Config
    return session.client(
        "secretsmanager",
        config=Config(max_pool_connections=50, retries={"max_attempts": 3, "mode": "adaptive"}),
    )


def get_db_credentials(secret_arn: str) -> dict:
    """Fetch database credentials from Secrets Manager (cached with a TTL).

    `secret_arn` is a RESOLVED credential (resolve_credential's output), never a handle
    from the model."""
    cached = _secrets_cache.get(secret_arn)
    if cached and cached[1] > time.time():
        return cached[0]

    client = _get_secrets_client(secret_arn)
    response = client.get_secret_value(SecretId=secret_arn)
    secret = json.loads(response["SecretString"])
    _secrets_cache[secret_arn] = (secret, time.time() + SECRETS_CACHE_TTL)
    return secret


# secret_arn -> (owning_space_ids_set, expiry). Backs the multi-tenant ownership guard so
# it costs at most one registry scan per secret per TTL, not one per query. TTL-bounded: a
# membership change is reflected within SECRETS_CACHE_TTL.
_secret_space_cache: dict[str, tuple] = {}


def _record_holding_secret(secret_arn: str):
    """The first registry record whose active credential or any owner's proof is
    `secret_arn` (see _records_holding_secret; a read replica and its source can share
    one secret, so prefer resolving by database id where it matters)."""
    held = _records_holding_secret(secret_arn, first_only=True)
    return held[0] if held else None


def _records_holding_secret(secret_arn: str, first_only: bool = False) -> list:
    """Every registry record whose active credential or any owner's proof is `secret_arn`.
    Per-space secrets live in the `space_secrets` map, which a FilterExpression cannot
    search by value, so the table (small: one row per physical database) is scanned with
    a projection and matched client-side."""
    table_name = resolve_table_name("instances")
    dynamodb = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1"))
    table = dynamodb.Table(table_name)
    # Every field the membership rule reads (tenancy.record_member_space_ids: status,
    # member_space_ids, candidate_space_ids, tags) plus the credential and endpoint fields.
    # A projection that leaves one out hands the rule an incomplete record, and an
    # incomplete record can report a candidate, or a space on a never-onboarded record,
    # as a member.
    kw = {"ProjectionExpression": "instance_id, secret_arn, space_secrets, member_space_ids, "
                                  "candidate_space_ids, reach_space_id, #st, tags, endpoint, reader_endpoint, "
                                  "port",
          "ExpressionAttributeNames": {"#st": "status"}}
    found = []
    while True:
        resp = table.scan(**kw)
        for item in resp.get("Items", []):
            if record_holds_secret(item, secret_arn):
                found.append(item)
                if first_only:
                    return found
        if "LastEvaluatedKey" not in resp:
            return found
        kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _member_spaces_for_secret(secret_arn: str) -> set:
    """The spaces whose agent turns may connect with `secret_arn`: the space that owns it
    and the spaces that owner granted (tenancy.spaces_entitled_to_secret), evaluated on
    the full record so a candidate space or a never-onboarded record yields nothing.
    Empty when no record holds the secret. Cached for SECRETS_CACHE_TTL."""
    cached = _secret_space_cache.get(secret_arn)
    if cached and cached[1] > time.time():
        return cached[0]
    spaces: set = set()
    try:
        # Entitled = the owner of this secret and the spaces it granted: a space's
        # agent turn connects only with its own credential or one lent to it, never
        # with a co-member's. A read replica and its source may hold the same secret,
        # so the union over every holder (per-record entitlement is still enforced by
        # resolve_credential's credential_arn_for).
        for rec in _records_holding_secret(secret_arn):
            spaces |= spaces_entitled_to_secret(rec, secret_arn)
    except Exception as e:
        logger.warning(f"space-for-secret lookup failed: {e}")
        spaces = set()
    _secret_space_cache[secret_arn] = (spaces, time.time() + SECRETS_CACHE_TTL)
    return spaces


# A refusal that never confirms whether an out-of-scope database exists or what it is
# named. Shared by the tenancy guard (_enforce_secret_space_ownership) and the query-plan
# target-database pin (db_tools.get_query_plan) so neither path reveals a co-tenant
# database's existence or name — the two cases are indistinguishable to the caller, and the
# real target is recorded server-side (audit log) instead.
SECRET_NOT_IN_SPACE_MESSAGE = (
    "This database's credentials are not available in the current space. If "
    "access was configured recently, re-select the database; otherwise it may "
    "not be registered in this space."
)


def _enforce_secret_space_ownership(secret_arn: str) -> None:
    """Multi-tenant guard: when invoked from an authenticated AGENT
    request, `secret_arn` MUST belong to an instance in the caller's VALIDATED space.

    The agent's DB tools take secret_arn straight from the prompt's [DATABASE SCOPE] block,
    so without this a caller could paste another tenant's secret_arn and query their DB (the
    chat gate only validates space_id, and the task role can read every secret under the
    deployment's configured prefix).
    Fail-closed: an unknown secret (not in the registry) or a mismatch is refused. No-op
    outside the agent request context — batch / Tier-1 Lambdas never set CURRENT_USER_ID, and
    the REST endpoints enforce their own _verify_instance_in_space ownership check."""
    try:
        from model_resolver import CURRENT_SPACE_ID, CURRENT_USER_ID
        user = CURRENT_USER_ID.get()
        space = CURRENT_SPACE_ID.get()
    except Exception:
        return  # contextvars unavailable → not an agent request
    if not user:
        return  # not an agent request (batch / Tier-1) → guarded elsewhere
    spaces = _member_spaces_for_secret(secret_arn)
    if not space or space not in spaces:
        # Same refusal either way, so it never confirms that an out-of-space database
        # exists. The log separates them: no owning row at all usually means the secret
        # was never registered against this database, or the agent supplied an ARN it
        # assembled rather than one from the registry — neither is a tenancy event, and
        # both need a different fix from a genuine cross-space attempt.
        if spaces:
            logger.warning(
                "audit: cross-space secret access denied — secret member_spaces=%s "
                "requested_space=%s user=%s", sorted(spaces), space or "none", user,
            )
        else:
            logger.warning(
                "secret not resolved — no registered database owns this secret "
                "(requested_space=%s, user=%s). Not a tenancy refusal: check that access "
                "is configured for the database and that the ARN came from the registry.",
                space or "none", user,
            )
        raise PermissionError(SECRET_NOT_IN_SPACE_MESSAGE)


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _agent_space() -> tuple:
    """(user, space) of the current agent request, or ('', '') outside one (batch, Tier 1,
    REST handlers never set CURRENT_USER_ID)."""
    try:
        from model_resolver import CURRENT_SPACE_ID, CURRENT_USER_ID
        return CURRENT_USER_ID.get() or "", CURRENT_SPACE_ID.get() or ""
    except Exception:
        return "", ""


def resolve_credential(handle: str) -> str:
    """Turn the credential HANDLE a tool was given into the secret ARN to connect with.

    The model never sees a secret ARN: the `[DATABASE SCOPE]` block carries the database
    id as the credential handle, and the specialist tools pass it straight through in the
    parameter still named `secret_arn`. Here, in one place, the id is resolved to the
    credential the CALLING SPACE is entitled to (its own secret, or the one its granting
    owner lent it; see tenancy.credential_arn_for). An agent request that hands in a real
    ARN is refused: an ARN in a prompt can only have been assembled or leaked, never
    issued. Outside an agent request (the scan job, the Tier 1 collector, the REST
    handlers) ARNs pass through unchanged and a database id resolves to the record's
    active credential, so the monitoring engine is untouched.

    Inside an agent request the handle is the database ALIAS from the scope (``db-1``),
    translated to its registry id first by ``db_aliases.resolve_handle``, which refuses
    an alias or a UUID outside the request's scope."""
    from tools.aws.db_aliases import resolve_handle
    value = (resolve_handle(handle) or "").strip()
    user, space = _agent_space()
    if _UUID_RE.match(value):
        from tools.aws.dynamodb_client import db_registry
        from tools.aws.tenancy import credential_arn_for
        record = db_registry.get_instance(value) or {}
        if user:
            arn = credential_arn_for(record, space)
            if not arn:
                logger.warning("audit: no credential for space %s on database %s (user=%s)", space or "none", value, user)
                raise PermissionError("This database is not accessible in the current space.")
            return arn
        return record.get("secret_arn") or ""
    if user and value.startswith("arn:"):
        logger.warning("audit: agent request supplied a raw secret ARN; refused (space=%s user=%s)", space or "none", user)
        raise PermissionError("Use a database alias from [DATABASE SCOPE]; secret ARNs are not accepted.")
    return value


def _record_for_connection(handle: str, secret_arn: str, secret_host: str) -> dict:
    """The registry record a connection is for. A database id (every agent call) names it
    directly. A secret ARN can be held by several records (an RDS read replica shares
    its source's read-only user), so with more than one holder the one whose endpoint
    the secret names is taken, and anything else is refused as ambiguous rather than
    guessed: guessing could dial the source for a replica's query."""
    if _UUID_RE.match(handle or ""):
        try:
            from tools.aws.dynamodb_client import db_registry
            record = db_registry.get_instance(handle)
            if record:
                return record
        except Exception as e:
            logger.warning(f"registry lookup by id failed: {e}")
    try:
        holders = _records_holding_secret(secret_arn)
    except Exception as e:
        logger.warning(f"Failed to look up endpoint from registry: {e}")
        holders = []
    if len(holders) > 1:
        named = [h for h in holders if secret_host and h.get("endpoint") == secret_host]
        if len(named) == 1:
            return named[0]
        raise ValueError("This secret is used by several databases; pass the database id.")
    if holders:
        return holders[0]
    return _lookup_record_from_registry(secret_arn, secret_host)


def get_connection(secret_arn: str, database: str = None):
    """Create a psycopg2 connection using Secrets Manager credentials.

    Aurora role routing: when the current chat request carries a READER focus
    (model_resolver.CURRENT_ROLE_FOCUS), the connection targets the cluster's
    reader endpoint from the registry — so pg_stat_* views genuinely reflect a
    reader instead of silently reporting the writer. Fail-soft: a missing reader
    endpoint keeps the normal (writer) connection."""
    # The database alias (agent request) becomes its registry id once, here, so the
    # record lookup below keys on the id the credential was resolved for.
    from tools.aws.db_aliases import resolve_handle
    handle = (resolve_handle(secret_arn) or "").strip()
    secret_arn = resolve_credential(handle)
    _enforce_secret_space_ownership(secret_arn)
    creds = get_db_credentials(secret_arn)
    secret_host = creds.get("host") or ""

    record = _record_for_connection(handle, secret_arn, secret_host)
    registry_host = record.get("endpoint")
    # PORT: the record's port (from RDS) wins; the secret's port is only a fallback.
    port = int(record.get("port") or creds.get("port") or 5432)
    # An RDS read replica shares its source's read-only user, so a secret naming the
    # source's endpoint (replica_source_endpoint, recorded from RDS by discovery, never
    # from user input) is accepted for the replica. The connection still dials the
    # replica's own endpoint below.
    allowed_hosts = {registry_host}
    tags = record.get("tags") or {}
    if tags.get("replica_of") and tags.get("replica_source_endpoint"):
        allowed_hosts.add(tags["replica_source_endpoint"])
    # HOST PRECEDENCE (identical in pg_client, run_scan, tier1_collector and the
    # Configure check - see tests/test_host_precedence.py):
    #   1. the instance's endpoint - read from the RDS API by discovery, so it is
    #      authoritative for WHICH database this record refers to; over a Lattice
    #      resource endpoint this same name resolves to the endpoint inside the agent VPC
    #   2. the secret's host - hand-written, so only a last resort when the record
    #      carries no endpoint
    # The instance endpoint is authoritative for which database this record refers to,
    # so a secret whose host disagrees is refused rather than followed. Two databases
    # sharing a read-only password must not let one silently redirect the other's
    # traffic and report on the wrong database.
    if registry_host and secret_host and secret_host not in allowed_hosts:
        raise ValueError(
            f"This secret's host ({secret_host}) is not this instance's endpoint "
            f"({registry_host}). The secret appears to belong to a different database. "
            f"Set it to {registry_host} (for Aurora, the cluster endpoint), or remove host "
            f"from the secret."
        )
    host = registry_host or secret_host

    focus = _role_focus()
    record_id = record.get("instance_id", "")
    if focus == "reader":
        reader_host = _lookup_reader_endpoint(secret_arn, record_id)
        if reader_host:
            # The reader endpoint is dialled only where this database's network path
            # reaches it (the same gate as Tier 1 and the reader scan).
            _gate_focus(record, {"endpoint": reader_host, "is_writer": False}, "The reader endpoint")
            logger.info("Role focus=reader: connecting via cluster reader endpoint")
            host = reader_host
    elif focus.startswith("instance:"):
        member_id = focus[len("instance:"):]
        member_host = _lookup_member_endpoint(secret_arn, member_id, record_id)
        if member_host:
            member = next((m for m in record.get("cluster_member_roles") or []
                           if m.get("instance_id") == member_id), None) or {"endpoint": member_host}
            gated = _gate_focus(record, {**member, "endpoint": member_host}, f"Member {member_id}")
            logger.info(f"Role focus=instance: connecting via member endpoint ({member_id})")
            host = gated
        else:
            logger.warning(f"Instance focus '{member_id}' not found in registry; staying on default endpoint")

    if not host:
        raise ValueError(
            f"No database host found in secret or instance registry for {secret_arn}"
        )

    conn = psycopg2.connect(
        host=host,
        port=port,
        dbname=database or creds.get("dbname", "postgres"),
        user=creds.get("username"),
        password=creds.get("password"),
        # Identify the connection to the TARGET database as this product acting for a
        # specific operator/space, so the target's pg_stat_activity and logs attribute the
        # query to the caller rather than to an anonymous client. Postgres truncates
        # application_name to NAMEDATALEN-1, so it is kept short; fail-soft to a plain label.
        application_name=_application_name(),
        # TLS with certificate verification against the RDS CA bundle (db_tls): the
        # credential travels over this connection, and a connection that lands on the
        # wrong host, through a route clash or anything else, fails the handshake before
        # the password is sent. verify-full, always. Fails closed without the bundle.
        **ssl_kwargs(),
        connect_timeout=10,
        options="-c statement_timeout=30000",  # 30s query timeout
    )
    conn.set_session(readonly=True, autocommit=True)
    return conn


class MemberNotReachable(ValueError):
    """A role focus names a member (or the reader endpoint) that this database's network
    path does not reach; carries the reason the members view shows."""


def _gate_focus(record: dict, member: dict, label: str) -> str:
    """The host to dial for a focused member, through ``host_routes.dial_gate`` (the gate
    Tier 1 and the reader scan use), or ``MemberNotReachable`` with its known reason
    instead of a dial that would time out (or, on a host-route path, head for the VPC
    holding the broad CIDR route). The writer always has a host: the gate falls back to
    the cluster endpoint."""
    from tools.aws import host_routes
    host, reason = host_routes.dial_gate(record, member)
    if host is None:
        why = reason[len("skipped: "):] if reason.startswith("skipped: ") else reason
        raise MemberNotReachable(
            f"{label} is not reachable on this database's network path ({why}). Clear the "
            f"endpoint focus to use the cluster endpoint, or pick a member the Members tab "
            f"shows as collected.")
    return host


def _application_name() -> str:
    """A short connection label identifying the caller (Cognito sub) and space for the
    target database's activity views/logs. Fail-soft to a plain product label when the
    request context is absent (e.g. batch). Kept under Postgres' 63-char limit."""
    user = space = ""
    try:
        from model_resolver import CURRENT_USER_ID, CURRENT_SPACE_ID
        user = (CURRENT_USER_ID.get() or "").strip()
        space = (CURRENT_SPACE_ID.get() or "").strip()
    except Exception:
        pass
    label = "dbops-agent"
    if user:
        label += f"/u:{user}"
    if space:
        label += f"/s:{space}"
    return label[:63]


def lookup_record_database_name(secret_arn: str) -> str:
    """The registered ``database_name`` for the instance owning this secret ('' if unknown).

    Used to pin a model-chosen target database to the record this credential belongs to
   . Fail-soft: any lookup problem returns '' so the caller keeps its prior
    behaviour rather than blocking on a registry hiccup."""
    secret_arn = resolve_credential(secret_arn)
    try:
        table_name = resolve_table_name("instances")
        dynamodb = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1"))
        table = dynamodb.Table(table_name)
        held = _record_holding_secret(secret_arn)
        if not held:
            return ""
        item = table.get_item(Key={"instance_id": held["instance_id"]},
                              ProjectionExpression="database_name").get("Item") or {}
        return (item.get("database_name") or "").strip()
    except Exception as e:
        logger.warning(f"database_name lookup failed (not pinning target db): {e}")
        return ""


def _role_focus() -> str:
    """The request's Aurora role focus ('' | 'writer' | 'reader'), fail-soft.
    Imported lazily: pg_client is also packaged into contexts (batch) where
    model_resolver may be absent — those always run with no role focus."""
    try:
        from model_resolver import CURRENT_ROLE_FOCUS
        return CURRENT_ROLE_FOCUS.get() or ""
    except Exception:
        return ""


def _lookup_reader_endpoint(secret_arn: str, instance_id: str = "") -> str:
    """The cluster reader endpoint for the instance owning this secret ('' if none)."""
    item = _lookup_role_fields(secret_arn, instance_id)
    return (item.get("reader_endpoint") or "") if item else ""


def _lookup_member_endpoint(secret_arn: str, member_id: str, instance_id: str = "") -> str:
    """The per-instance endpoint for ONE cluster member ('' if unknown).

    The member id is a validated selector into the registry's
    cluster_member_roles list — the host always comes from the registry,
    never from caller input."""
    item = _lookup_role_fields(secret_arn, instance_id)
    for m in (item.get("cluster_member_roles") or []) if item else []:
        if m.get("instance_id") == member_id and m.get("endpoint"):
            return m["endpoint"]
    return ""


def _lookup_role_fields(secret_arn: str, instance_id: str = "") -> dict:
    """Registry row (reader_endpoint + cluster_member_roles) for this secret, fail-soft.
    `instance_id` (the record the connection resolved to) wins over a by-secret match."""
    try:
        table_name = resolve_table_name("instances")
        dynamodb = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1"))
        table = dynamodb.Table(table_name)
        # The record that holds this ARN as its active credential OR as any owner's
        # proof (per-space secrets live in a map, so match client-side).
        if not instance_id:
            held = _record_holding_secret(secret_arn)
            if not held:
                return {}
            instance_id = held["instance_id"]
        item = table.get_item(Key={"instance_id": instance_id},
                              ProjectionExpression="reader_endpoint, cluster_member_roles").get("Item")
        return item or {}
    except Exception as e:
        logger.warning(f"role-field lookup failed (staying on default endpoint): {e}")
        return {}


def _lookup_record_from_registry(secret_arn: str, host: str = "") -> dict:
    """The registry record for a connection: by secret first (the record whose active
    credential or any owner's proof this ARN is), else by endpoint host. ``{}`` when
    nothing matches or the registry is unreachable; callers fall back to the secret."""
    try:
        table_name = resolve_table_name("instances")
        dynamodb = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1"))
        table = dynamodb.Table(table_name)

        held = _record_holding_secret(secret_arn)
        items = [held] if held else []

        # Fall back to matching by endpoint/host if secret_arn didn't match
        if not items and host:
            from tools.aws.ddb_pages import read_all
            items = read_all(table.scan, FilterExpression="#ep = :ep",
                             ExpressionAttributeNames={"#ep": "endpoint"},
                             ExpressionAttributeValues={":ep": host},
                             ProjectionExpression="endpoint, port")

        logger.info(f"DynamoDB lookup: found {len(items)} items")
        if items:
            return items[0] or {}
        logger.warning(f"No instance found in DynamoDB for secret_arn={secret_arn} or host={host}")
    except Exception as e:
        logger.warning(f"Failed to look up endpoint from registry: {e}")
    return {}


def _lookup_endpoint_from_registry(secret_arn: str, host: str = ""):
    """Look up database endpoint from DynamoDB instance registry by secret_arn or host."""
    record = _lookup_record_from_registry(secret_arn, host)
    if record:
        ep = record.get("endpoint")
        port = int(record.get("port", 5432))
        logger.info(f"Resolved endpoint: {ep}:{port}")
        return ep, port
    return None, 5432


def execute_query(secret_arn: str, query: str, params: tuple = None,
                  database: str = None) -> list[dict]:
    """Execute a read-only query and return results as list of dicts."""
    conn = None
    try:
        conn = get_connection(secret_arn, database)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            if cur.description:
                return [dict(row) for row in cur.fetchall()]
            return []
    except psycopg2.OperationalError as e:
        # A common agent mistake is passing the instance/cluster identifier as the
        # database name. Turn the raw "database does not exist" error into a clear,
        # actionable message so the caller fixes the arg instead of retrying with
        # guessed names (a frequent source of redundant tool calls).
        msg = str(e)
        if database and "does not exist" in msg and "database" in msg.lower():
            raise RuntimeError(
                f"Database {database!r} does not exist on this instance. Use the "
                f"PostgreSQL database name (usually 'postgres'), not the instance or "
                f"cluster identifier. Do not retry with guessed database names."
            ) from e
        logger.error(f"Query error: {e}")
        raise
    except psycopg2.Error as e:
        logger.error(f"Query error: {e}")
        raise
    finally:
        if conn:
            conn.close()


def test_connection(secret_arn: str, database: str = None) -> dict:
    """Test database connectivity and return basic info, including the ROLE of the
    member the connection landed on (``writer`` or ``reader``, from
    ``pg_is_in_recovery``). The role is what tells the caller which member a cluster
    alias currently reaches; the server's own address is not returned because an IP
    inside the database's VPC identifies nothing the caller can act on.

    `secret_arn` is the tool's credential handle, passed through unresolved:
    execute_query -> get_connection resolves it exactly once. Resolving it here as well
    handed get_connection an ARN, which an agent request refuses as model input."""
    try:
        rows = execute_query(
            secret_arn,
            "SELECT version() AS version, current_database() AS database, "
            "current_user AS user, "
            "CASE WHEN pg_is_in_recovery() THEN 'reader' ELSE 'writer' END AS role",
            database=database,
        )
        return {"connected": True, **rows[0]}
    except Exception as e:
        return {"connected": False, "error": str(e)}
