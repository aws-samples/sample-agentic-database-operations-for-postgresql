# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Email alarms on the collected metrics.

One CloudWatch alarm per monitored database per enabled alarm, on the Tier 1 series in
``<prefix>/Metrics``. Alarms only notify: a state change goes through an EventBridge rule
to the alarm notifier Lambda (``backend/alarm_notifier.py``), which emails the
deployment's notification address. Nothing an alarm does changes a database.

What this module owns:

- ``CATALOGUE``: which alarms exist, what each means, its default, and how it reads its
  series. Thresholds come from ``backend/checks/references.yaml`` wherever that file
  states one (the bands the findings use), so an alarm and a finding never disagree;
  the two without a reference band say why they use the number they do.
- ``desired_alarms(records, settings)``: the alarms that should exist, built only from
  registry records and the catalogue. No caller supplies a metric query, dimension,
  threshold or action: the server builds every alarm, and its only action is the event
  the notifier reads (product rule 29).
- ``reconcile(records, settings)``: put the desired alarms and delete this install's
  alarms that should no longer exist (a removed database, a switched-off alarm, alarms
  off). Every alarm is named ``<prefix>-db-<alarm id>-<instance id>``; reconcile only
  ever touches that prefix.
- ``choose_channel(...)``: SES (HTML) when a verified sender can reach the recipient,
  else the notification topic (plain text). Shared by the console and the notifier.

Imported by the notifier Lambda, so module import is stdlib + boto3 only; PyYAML is
imported lazily where thresholds are resolved (the API task).
"""
from __future__ import annotations

import logging
import os
from typing import Iterable, Optional

logger = logging.getLogger("db-alarms")

SETTINGS_KEY = "alarm_settings"
_NAME_INFIX = "-db-"

# ── The catalogue ─────────────────────────────────────────────────────────────
# metric: the Tier 1 metric name (``metrics_store.TIER1_NUMERIC_METRICS``, or the
#   collector's ``collection_failed`` status). stat: the CloudWatch statistic per period.
# scope: "writer" (the database as a whole: the Role=writer series for a cluster, the
#   member itself for a single instance), "readers" (Role=reader, the worst reader per
#   period; a single instance only when it is a read replica) or "database" (the
#   InstanceId-only status series). datapoints/periods: M of N Tier 1 intervals.
# missing: TreatMissingData. Every alarm treats a gap as not breaching, so a paused or
#   new database never alarms on absence; "collection_failed" is published as 1 on a
#   failed run, so a broken collection is a value, not a gap.
CATALOGUE = (
    {
        "id": "collection_failed",
        "label": "Tier 1 collection failing",
        "severity": "critical",
        "default": True,
        "metric": "collection_failed", "stat": "Minimum", "scope": "database",
        "comparison": "GreaterThanOrEqualToThreshold", "datapoints": 3, "periods": 3,
        "meaning": "The Tier 1 collector could not collect this database three runs in a row, so its "
                   "metrics, its other alarms and failover detection are not being updated.",
        "next_steps": ["Open the database in the admin console: Monitoring › Collection shows the "
                       "collector's last error.",
                       "Check that the read-only secret still works (Test connection) and that the "
                       "database is running."],
    },
    {
        "id": "xid_wraparound",
        "label": "Transaction ID wraparound risk",
        "severity": "critical",
        "default": True,
        "metric": "xid_age_pct", "stat": "Maximum", "scope": "writer",
        "comparison": "GreaterThanOrEqualToThreshold", "datapoints": 1, "periods": 1,
        "meaning": "The oldest unfrozen transaction ID on the instance has passed the warning floor. "
                   "If it keeps growing, PostgreSQL eventually stops accepting writes to protect "
                   "itself.",
        "next_steps": ["Ask the agent which tables drive the age and what is blocking freeze "
                       "(long transactions, replication slots, autovacuum settings).",
                       "Plan a VACUUM (FREEZE) of the oldest tables before the age reaches the "
                       "critical floor."],
    },
    {
        "id": "connection_saturation",
        "label": "Connections near max_connections",
        "severity": "warning",
        "default": True,
        "metric": "connections_used_pct", "stat": "Maximum", "scope": "writer",
        "comparison": "GreaterThanOrEqualToThreshold", "datapoints": 2, "periods": 3,
        "meaning": "Client connections are using most of max_connections. New connections are "
                   "refused once the limit is reached.",
        "next_steps": ["Ask the agent which applications and clients hold the connections, and how "
                       "many are idle or idle in transaction.",
                       "A connection pooler (RDS Proxy, PgBouncer) is the usual fix."],
    },
    {
        "id": "long_transaction",
        "label": "Long-running transaction",
        "severity": "warning",
        "default": True,
        "metric": "longest_transaction_sec", "stat": "Maximum", "scope": "writer",
        "comparison": "GreaterThanOrEqualToThreshold", "datapoints": 1, "periods": 1,
        "meaning": "A client transaction has been open long enough to hold back vacuum and "
                   "transaction ID freeze for the whole instance.",
        "next_steps": ["Ask the agent for the current activity: who holds the transaction, what it "
                       "runs and whether it is idle in transaction.",
                       "End it from the application, or terminate the backend if it is abandoned."],
    },
    {
        "id": "blocked_queries",
        "label": "Sustained lock contention",
        "severity": "warning",
        "default": True,
        "metric": "blocked_queries", "stat": "Minimum", "scope": "writer",
        "comparison": "GreaterThanOrEqualToThreshold", "datapoints": 3, "periods": 3,
        "meaning": "Queries have been waiting on locks at every Tier 1 sample for three intervals "
                   "running: a blocking chain, not a momentary wait.",
        "next_steps": ["Ask the agent for the lock analysis: the blocking session, its client and "
                       "application, and what it holds."],
    },
    {
        "id": "deadlocks",
        "label": "Deadlocks",
        "severity": "warning",
        "default": True,
        "metric": "deadlocks_delta", "stat": "Sum", "scope": "writer",
        "comparison": "GreaterThanThreshold", "datapoints": 1, "periods": 1,
        "meaning": "PostgreSQL detected a deadlock and cancelled one of the transactions involved.",
        "next_steps": ["Turn on log_lock_waits if it is off, then ask the agent to read the "
                       "deadlock details from the PostgreSQL log.",
                       "The usual fix is a consistent lock order in the application."],
    },
    {
        "id": "replica_lag",
        "label": "Replica lag",
        "severity": "warning",
        "default": True,
        "metric": "replica_lag_ms", "stat": "Maximum", "scope": "readers",
        "comparison": "GreaterThanOrEqualToThreshold", "datapoints": 3, "periods": 3,
        "meaning": "A reader is behind its writer for three intervals running, so reads from it "
                   "return stale data.",
        "next_steps": ["Ask the agent about replication on that reader: replay delay, recovery "
                       "conflicts, long queries with hot_standby_feedback.",
                       "Check the writer's WAL volume for a burst."],
    },
    {
        "id": "cache_hit_ratio",
        "label": "Low buffer cache hit ratio",
        "severity": "warning",
        "default": True,
        "metric": "cache_hit_ratio", "stat": "Average", "scope": "writer",
        "comparison": "LessThanThreshold", "datapoints": 3, "periods": 3,
        "meaning": "Most reads are going to storage instead of memory, for three intervals running: "
                   "the working set no longer fits.",
        "next_steps": ["Ask the agent which tables drive the misses and whether a recent query "
                       "change caused large scans.",
                       "A larger instance class (more memory) or better indexes are the usual fixes."],
    },
    {
        "id": "rollback_ratio",
        "label": "High rollback ratio",
        "severity": "warning",
        "default": False,
        "metric": "rollback_ratio_pct", "stat": "Average", "scope": "writer",
        "comparison": "GreaterThanOrEqualToThreshold", "datapoints": 3, "periods": 3,
        "meaning": "A large share of transactions are rolling back, usually application errors or "
                   "conflicts retrying.",
        "next_steps": ["Check the application logs for errors; ask the agent whether deadlocks or "
                       "serialization failures are involved."],
    },
)
BY_ID = {a["id"]: a for a in CATALOGUE}
SEVERITIES = ("critical", "warning")


def _references() -> dict:
    """The check references (thresholds). Lazily imported: the notifier never needs it."""
    try:
        from tools.db_tools import _check_references
        return _check_references()
    except Exception:  # noqa: BLE001 - outside the backend package (a test, the notifier)
        import yaml
        from pathlib import Path
        p = Path(__file__).resolve().parents[2] / "checks" / "references.yaml"
        return yaml.safe_load(p.read_text()) or {}


def _band(bands: list, name: str, key: str):
    for b in bands or []:
        if b.get("name") == name:
            return b.get(key)
    raise KeyError(name)


# Connections: references.yaml states it as an observability band ("warning: > 80%" of
# max, idle-connections), not a numeric band, so the number lives here with that pointer.
CONNECTION_WARNING_PCT = 80.0
# Lock contention: references.yaml bands blocked-query WAIT SECONDS (warning 10 s), which
# Tier 1 does not sample. Three consecutive samples with any query waiting is a wait of at
# least two whole intervals (10 minutes at the default cadence), well past that band.
BLOCKED_SUSTAINED = 1


def thresholds() -> dict:
    """``{alarm id: threshold}`` in the metric's own unit, from references.yaml."""
    from tools.xid import XID_WRAPAROUND_MAX
    refs = _references()
    t = {}
    t["collection_failed"] = 1.0
    xid_floor = refs["xid-wraparound-database"]["thresholds"]["absolute"]["warning"]
    t["xid_wraparound"] = round(100.0 * xid_floor / XID_WRAPAROUND_MAX, 2)
    t["connection_saturation"] = CONNECTION_WARNING_PCT
    t["long_transaction"] = float(_band(refs["long-running-transactions"]["thresholds"]["txn_seconds_bands"],
                                        "warning", "min"))
    t["blocked_queries"] = float(BLOCKED_SUSTAINED)
    t["deadlocks"] = float(_band(refs["lock-contention"]["thresholds"]["deadlock_bands"], "warning", "min"))
    t["replica_lag"] = 1000.0 * _band(refs["replication-health"]["thresholds"]["replay_lag_seconds_bands"],
                                      "warning", "min")
    t["cache_hit_ratio"] = float(_band(refs["database-cache-hit"]["thresholds"]["bands"], "critical", "max"))
    t["rollback_ratio"] = float(_band(refs["transaction-health"]["thresholds"]["bands"], "critical", "min"))
    return t


def threshold_text(alarm_id: str, value: float) -> str:
    """The threshold as a person reads it."""
    from tools.xid import XID_WRAPAROUND_MAX
    if alarm_id == "collection_failed":
        return "3 failed runs in a row"
    if alarm_id == "xid_wraparound":
        xids = value / 100.0 * XID_WRAPAROUND_MAX
        return f"≥ {value:g}% of the wraparound limit (about {xids / 1e9:.1f} billion transaction IDs)"
    if alarm_id == "connection_saturation":
        return f"≥ {value:g}% of max_connections in 2 of 3 intervals"
    if alarm_id == "long_transaction":
        return f"a transaction open ≥ {value / 60:g} minutes"
    if alarm_id == "blocked_queries":
        return "queries waiting on locks at 3 samples in a row"
    if alarm_id == "deadlocks":
        return "any deadlock"
    if alarm_id == "replica_lag":
        return f"a reader ≥ {value / 1000:g} s behind for 3 intervals"
    if alarm_id == "cache_hit_ratio":
        return f"< {value:g}% for 3 intervals"
    if alarm_id == "rollback_ratio":
        return f"≥ {value:g}% of transactions rolled back for 3 intervals"
    return f"{value:g}"


# ── Settings ──────────────────────────────────────────────────────────────────

def resolve_settings(stored: Optional[dict]) -> dict:
    """``{"enabled": bool, "alarms": {id: bool}}`` from the stored config item. Absent
    means the defaults: alarms on, every alarm at its catalogue default."""
    stored = stored or {}
    per = stored.get("alarms") if isinstance(stored.get("alarms"), dict) else {}
    return {
        "enabled": bool(stored.get("enabled", True)),
        "alarms": {a["id"]: bool(per.get(a["id"], a["default"])) for a in CATALOGUE},
    }


# ── Naming ────────────────────────────────────────────────────────────────────

def _prefix() -> str:
    try:
        from tools.resource_names import resource_prefix
        return resource_prefix()
    except Exception:  # noqa: BLE001
        return (os.getenv("RESOURCE_PREFIX") or "dbops").strip() or "dbops"


def name_prefix() -> str:
    return f"{_prefix()}{_NAME_INFIX}"


def alarm_name(alarm_id: str, instance_id: str) -> str:
    return f"{name_prefix()}{alarm_id}-{instance_id}"


def parse_alarm_name(name: str) -> Optional[tuple]:
    """``(alarm id, instance id)`` for one of this install's alarms, else None."""
    p = name_prefix()
    if not str(name or "").startswith(p):
        return None
    rest = name[len(p):]
    for a in CATALOGUE:
        if rest.startswith(a["id"] + "-") and len(rest) > len(a["id"]) + 1:
            return a["id"], rest[len(a["id"]) + 1:]
    return None


# ── Desired alarms ────────────────────────────────────────────────────────────

_TIER1_MINUTES = {"5min": 5, "10min": 10, "15min": 15, "30min": 30}


def _period_seconds(record: dict) -> int:
    """One Tier 1 interval: the database's cadence (5 minutes when unset or legacy)."""
    return 60 * _TIER1_MINUTES.get(record.get("tier1_frequency") or "", 5)


def _member(record: dict) -> tuple:
    """A single instance's own member id and role, as the collector tags it."""
    tags = record.get("tags") or {}
    arn = record.get("arn") or tags.get("arn", "")
    mid = arn.rsplit(":", 1)[-1] if arn else record.get("instance_id", "")
    return mid, ("reader" if tags.get("replica_of") else "writer")


def _dimensions(record: dict, scope: str) -> Optional[list]:
    """The series an alarm reads, from the record alone; None when the database has no
    such series (readers on a database without any)."""
    iid = record["instance_id"]
    base = [{"Name": "InstanceId", "Value": iid}]
    if scope == "database":
        return base
    roles = record.get("cluster_member_roles") or []
    if roles:
        if scope == "writer":
            return base + [{"Name": "Role", "Value": "writer"}]
        if any(not m.get("is_writer") for m in roles):
            return base + [{"Name": "Role", "Value": "reader"}]
        return None
    mid, role = _member(record)
    if scope == "writer" or role == "reader":
        return base + [{"Name": "Member", "Value": mid}]
    return None


def is_monitored(record: dict) -> bool:
    """Alarms exist only for a database that is configured and collecting."""
    return record.get("status") == "configured" and record.get("tier1_enabled") is not False


def desired_alarms(records: Iterable[dict], settings: dict, limits: Optional[dict] = None,
                   namespace: Optional[str] = None) -> dict:
    """``{alarm name: PutMetricAlarm kwargs}`` for every monitored database and enabled
    alarm. ``limits`` defaults to ``thresholds()``."""
    s = resolve_settings(settings)
    if not s["enabled"]:
        return {}
    limits = limits if limits is not None else thresholds()
    if namespace is None:
        from tools.aws.metrics_store import cw_namespace
        namespace = cw_namespace()
    out = {}
    for rec in records or []:
        if not rec.get("instance_id") or not is_monitored(rec):
            continue
        period = _period_seconds(rec)
        name_of = rec.get("display_name") or rec["instance_id"]
        for a in CATALOGUE:
            if not s["alarms"].get(a["id"]):
                continue
            dims = _dimensions(rec, a["scope"])
            if dims is None:
                continue
            name = alarm_name(a["id"], rec["instance_id"])
            out[name] = {
                "AlarmName": name,
                # Read by people in the CloudWatch console; the email comes from the
                # notifier, which looks the database up by id.
                "AlarmDescription": f"{a['label']} on {name_of} "
                                    f"({threshold_text(a['id'], limits[a['id']])}). Managed by "
                                    f"{_prefix()}: Admin Console › Settings › Alarms.",
                "Namespace": namespace,
                "MetricName": a["metric"],
                "Dimensions": dims,
                "Statistic": a["stat"],
                "Period": period,
                "EvaluationPeriods": a["periods"],
                "DatapointsToAlarm": a["datapoints"],
                "Threshold": float(limits[a["id"]]),
                "ComparisonOperator": a["comparison"],
                "TreatMissingData": "notBreaching",
                # No AlarmActions/OKActions: CloudWatch sends every state change to
                # EventBridge regardless, and the rule on this install's alarm-name prefix
                # invokes the notifier. Nothing else is ever acted on.
            }
    return out


# ── Reconcile ─────────────────────────────────────────────────────────────────

_COMPARED = ("Namespace", "MetricName", "Statistic", "Period", "EvaluationPeriods",
             "DatapointsToAlarm", "Threshold", "ComparisonOperator", "TreatMissingData",
             "AlarmDescription")


def _same(want: dict, have: dict) -> bool:
    for k in _COMPARED:
        if want.get(k) != have.get(k):
            return False
    dims = lambda d: sorted((x["Name"], x["Value"]) for x in d or [])  # noqa: E731
    return (dims(want.get("Dimensions")) == dims(have.get("Dimensions"))
            and not have.get("AlarmActions") and not have.get("OKActions"))


def existing_alarms(cw) -> dict:
    """This install's database alarms: ``{name: MetricAlarm}``."""
    out = {}
    token = None
    while True:
        kwargs = {"AlarmNamePrefix": name_prefix(), "AlarmTypes": ["MetricAlarm"], "MaxRecords": 100}
        if token:
            kwargs["NextToken"] = token
        resp = cw.describe_alarms(**kwargs)
        for a in resp.get("MetricAlarms", []):
            out[a["AlarmName"]] = a
        token = resp.get("NextToken")
        if not token:
            return out


def reconcile(cw, records: Iterable[dict], settings: dict, limits: Optional[dict] = None,
              namespace: Optional[str] = None, only_instance: Optional[str] = None) -> dict:
    """Make this install's alarms match ``desired_alarms``. ``only_instance`` limits the
    pass to one database (its alarms are put or removed; nothing else is touched).
    Returns ``{"put": n, "deleted": n, "unchanged": n, "errors": [..]}``; one failing
    alarm does not stop the rest."""
    want = desired_alarms(records, settings, limits, namespace)
    have = existing_alarms(cw)
    if only_instance:
        suffix = f"-{only_instance}"
        want = {k: v for k, v in want.items() if k.endswith(suffix)}
        have = {k: v for k, v in have.items() if k.endswith(suffix)}
    put = deleted = unchanged = 0
    errors = []
    for name, spec in want.items():
        if name in have and _same(spec, have[name]):
            unchanged += 1
            continue
        try:
            cw.put_metric_alarm(**spec)
            put += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {type(e).__name__}")
    stale = [n for n in have if n not in want and parse_alarm_name(n)]
    for i in range(0, len(stale), 100):   # DeleteAlarms takes up to 100 names
        chunk = stale[i:i + 100]
        try:
            cw.delete_alarms(AlarmNames=chunk)
            deleted += len(chunk)
        except Exception as e:  # noqa: BLE001
            errors.append(f"delete {len(chunk)} alarm(s): {type(e).__name__}")
    if errors:
        logger.warning("alarm reconcile: %d error(s): %s", len(errors), "; ".join(errors[:5]))
    return {"put": put, "deleted": deleted, "unchanged": unchanged, "errors": errors}


def delete_for_instance(cw, instance_id: str) -> int:
    """Remove every alarm of one database (it is being removed). Idempotent."""
    names = [n for n in existing_alarms(cw) if n.endswith(f"-{instance_id}") and parse_alarm_name(n)]
    for i in range(0, len(names), 100):
        cw.delete_alarms(AlarmNames=names[i:i + 100])
    return len(names)


def firing(cw) -> list:
    """This install's alarms currently in ALARM: ``[{alarm id, instance id, since}]``."""
    out = []
    token = None
    while True:
        kwargs = {"AlarmNamePrefix": name_prefix(), "AlarmTypes": ["MetricAlarm"],
                  "StateValue": "ALARM", "MaxRecords": 100}
        if token:
            kwargs["NextToken"] = token
        resp = cw.describe_alarms(**kwargs)
        for a in resp.get("MetricAlarms", []):
            parsed = parse_alarm_name(a["AlarmName"])
            if parsed:
                ts = a.get("StateUpdatedTimestamp")
                out.append({"alarm_id": parsed[0], "instance_id": parsed[1],
                            "since": ts.isoformat() if hasattr(ts, "isoformat") else str(ts or "")})
        token = resp.get("NextToken")
        if not token:
            return out


# ── Delivery ──────────────────────────────────────────────────────────────────

def choose_channel(sender: dict, sender_verified: bool, sending_enabled: bool, production: bool,
                   recipient: str, recipient_verified: bool, topic_arn: str) -> dict:
    """How an alarm email goes out. SES (HTML) needs a verified sender on an account that
    can send, and either production access or a verified recipient (the SES sandbox).
    Otherwise the notification topic, which reaches the address subscribed at deploy
    time as plain text. ``{"channel": "ses"|"sns"|"none", "reason": str}``."""
    if recipient and (sender or {}).get("from_address") and sender_verified and sending_enabled \
            and (production or recipient_verified):
        return {"channel": "ses", "reason": ""}
    if not recipient:
        reason = "No notification address is set (NOTIFICATION_EMAIL at deploy time)."
        return {"channel": "sns" if topic_arn else "none", "reason": reason}
    if not (sender or {}).get("from_address"):
        reason = "No email sender is set up (Settings › Email), so alarms go out as plain text."
    elif not sender_verified or not sending_enabled:
        reason = "The email sender is not verified yet, so alarms go out as plain text."
    else:
        reason = ("SES is in the sandbox and the notification address is not a verified identity, "
                  "so alarms go out as plain text.")
    return {"channel": "sns" if topic_arn else "none", "reason": reason}
