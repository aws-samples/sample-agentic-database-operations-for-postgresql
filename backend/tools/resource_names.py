# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Physical AWS resource names, derived from a single configurable prefix.

Every DynamoDB table, Lambda function and bucket this application talks to is
named `<prefix>-<suffix>`. The prefix comes from the ``RESOURCE_PREFIX``
environment variable, which CloudFormation sets from its ``ResourcePrefix``
parameter. Deploying with a different prefix therefore produces a completely
independent installation in the same account. See docs/ARCHITECTURE.md#resource-naming.

The suffix tables below are the **contract with the CloudFormation templates**.
``deployment/check-resource-names.py`` asserts that the templates generate exactly
these suffixes, so a mismatch fails a check instead of surfacing later as a
runtime ``ResourceNotFoundException``.

Deliberately stdlib-only: this module is bundled into the Tier 1 collector Lambda
zip and the Batch scan image, both of which carry a curated subset of ``tools/``.
"""
import os

DEFAULT_PREFIX = "dbops"

# key -> (per-name environment override, suffix)
TABLES = {
    "spaces": ("SPACES_TABLE", "spaces"),
    "config": ("CONFIG_TABLE", "config"),
    "audit": ("AUDIT_TABLE", "audit"),
    "user_knowledge": ("USER_KNOWLEDGE_TABLE", "user-knowledge"),
    "network": ("NETWORK_TABLE", "network"),
    "investigations": ("INVESTIGATIONS_TABLE", "investigations"),
    "recommendations": ("RECOMMENDATIONS_TABLE", "recommendations"),
    "sessions": ("SESSIONS_TABLE", "sessions"),
    "instances": ("DB_CONFIG_TABLE", "instances"),
    "rate_limits": ("RATE_LIMIT_TABLE", "rate-limits"),
    # The metrics store (DynamoDB records; the series are CloudWatch metrics).
    "tier1_metrics": ("TIER1_METRICS_TABLE", "tier1-metrics"),
    "tier2_scans": ("TIER2_SCANS_TABLE", "tier2-scans"),
}

FUNCTIONS = {
    "scan_trigger": ("SCAN_TRIGGER_FUNCTION", "scan-trigger"),
    "learning_trigger": ("LEARNING_TRIGGER_FUNCTION", "learning-trigger"),
    "tier1_collector": ("TIER1_COLLECTOR_FUNCTION", "tier1-collector"),
    "alarm_notifier": ("ALARM_NOTIFIER_FUNCTION", "alarm-notifier"),
}


def resource_prefix() -> str:
    """The configured prefix, falling back to the default when unset or blank."""
    return (os.getenv("RESOURCE_PREFIX") or "").strip() or DEFAULT_PREFIX


def _resolve(registry: dict, key: str) -> str:
    try:
        env_key, suffix = registry[key]
    except KeyError:
        raise KeyError(
            f"unknown resource key {key!r}; known keys: {sorted(registry)}"
        ) from None
    # An explicit per-name override always wins, so an operator can point at a
    # resource that does not follow the convention.
    override = (os.getenv(env_key) or "").strip()
    return override or f"{resource_prefix()}-{suffix}"


def table_name(key: str) -> str:
    """DynamoDB table name for a logical key, e.g. ``table_name('spaces')``."""
    return _resolve(TABLES, key)


def function_name(key: str) -> str:
    """Lambda function name for a logical key."""
    return _resolve(FUNCTIONS, key)


def reports_bucket() -> str:
    """Reports bucket, normally supplied by the stack as REPORTS_S3_BUCKET.

    The fallback is best-effort and omits the account suffix that the template
    appends, so it will not resolve to a real bucket - it exists only so local
    development fails with a recognisable name rather than a confusing one.
    """
    return (os.getenv("REPORTS_S3_BUCKET") or "").strip() or f"{resource_prefix()}-reports"


def managed_by_tag() -> str:
    """Value for the ``ManagedBy`` tag on resources this application creates.

    This is load-bearing for isolation, not cosmetic. The network configurator both
    *writes* this tag and *queries* by it (``Filters=[{"Name": "tag:ManagedBy", ...}]``)
    to find the peering connections, routes and security groups it owns. If two
    installations in one account shared a value, each would discover - and could
    modify or delete - the other's networking. Deriving it from the prefix keeps
    installations from seeing each other.
    """
    return f"{resource_prefix()}-agent"


def discovery_role_arn(account_id: str) -> str:
    """ARN of the read-only discovery role the agent assumes in a customer account.

    Must line up with the role name the admin console tells an operator to create for a
    member account, and the ``iam:PassRole``/``sts:AssumeRole`` policies in ``api.yaml``
    and ``backend/template.yaml``, which only permit roles named ``<ResourcePrefix>-*``.
    Two installations therefore cannot assume each other's discovery roles.
    """
    template = (os.getenv("DISCOVERY_ROLE_ARN_TEMPLATE") or "").strip()
    if template:
        return template.format(account_id=account_id)
    return f"arn:aws:iam::{account_id}:role/{resource_prefix()}-discovery"


def owner_admin_of(space: dict) -> str:
    """The admin who owns `space`: the Cognito sub stored as ``owner_admin``, which is set
    once at creation to the creator's id and never updated.

    Credentials and cross-account roles are keyed on THIS id, not on the space id. All of
    one admin's spaces share one role per account and one Secrets Manager namespace, so a
    database onboarded in one of them is added to the others with the same secret; two
    admins' spaces share nothing, and IAM enforces that line. The super admin role grants
    nothing here: a super admin's spaces are keyed on the super admin's own id like
    anyone else's."""
    return ((space or {}).get("owner_admin") or (space or {}).get("created_by") or "").strip()


def owner_key(owner_admin: str) -> str:
    """The short, IAM-safe form of an owner admin id used in names: the first 12 hex
    characters of the sub (a UUID), which is unique within a user pool for any realistic
    population and keeps role names under the 64-character limit."""
    return (owner_admin or "").replace("-", "")[:12].lower()


def agent_role_name(owner_admin: str) -> str:
    """Name of the per-admin access role, identical in the deployment account (where this
    application creates it) and in every customer account (where the admin installs it
    from the policy the console shows): ``<ResourcePrefix>-agent-<owner key>``. Must stay
    under the ``<ResourcePrefix>-*`` pattern the task role's ``sts:AssumeRole`` and
    ``iam:CreateRole`` grants are scoped to."""
    return f"{resource_prefix()}-agent-{owner_key(owner_admin)}"


def secret_namespace(owner_admin: str) -> str:
    """Secrets Manager name prefix under which an admin's spaces keep their database
    secrets: ``<ResourcePrefix>/<owner admin>/``. Every role of that admin reads this path
    and only this path; the wizard names new secrets under it."""
    return f"{resource_prefix()}/{(owner_admin or '').strip()}/"


def security_group_name(suffix: str) -> str:
    """Security group name. Unique per VPC, so prefix it to allow coexistence."""
    return f"{resource_prefix()}-{suffix}"


def peering_name(db_vpc_id: str) -> str:
    """``Name`` tag for a VPC peering connection to a customer database VPC."""
    return f"{resource_prefix()}-{db_vpc_id[:20]}"


def agent_vpc_tag_key() -> str:
    """Key of the tag that records which agent VPC created a network resource:
    ``<ResourcePrefix>:agent_vpc`` = the agent VPC id.

    ``ManagedBy`` isolates installations with DIFFERENT prefixes. Two installations with
    the SAME prefix (dev and prod, or an earlier generation of this one) share it, so a
    stale peering left by one looks identical to the other's. The agent VPC id is unique
    to a single installation, so this tag is what a route-conflict claim uses to prove a
    conflicting peering is this installation's own leftover (safe to repoint) and not
    another live deployment's path (never touched automatically)."""
    return f"{resource_prefix()}:agent_vpc"
