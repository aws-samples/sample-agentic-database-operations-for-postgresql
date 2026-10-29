# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Per-database metadata collection behind POST /api/spaces/{id}/topology/refresh.

`collect_db_metadata` asks RDS, CloudWatch Logs, CloudWatch and (best effort) EC2
about one database and returns what the registry should store:

    {"tags": {...str values...}, "fields": {...top-level registry fields...}}

Everything the topology builder later surfaces — engine facts, cluster members
and their roles, subnet AZs, replicas, alarm counts, log groups, the VPC CIDR —
comes from here. Each call is fail-soft: a denied or failing API leaves that
facet empty rather than aborting the refresh, and the record is stamped with
`metadata_refreshed_at` so the UI can tell fresh data from never-collected.

Only describe/list calls are made. The cross-account permission boundary grants
rds:Describe* and cloudwatch:DescribeAlarms but no ec2:*, so the VPC CIDR lookup
succeeds for same-account databases and is skipped for the others.
"""

import logging
from datetime import datetime, timezone
from typing import Iterable, List, Optional

logger = logging.getLogger("topology-refresh")

MAX_ALARM_PAGES = 10      # DescribeAlarms pages (100 alarms each) scanned per database
MAX_ALARM_NAMES = 10      # names kept on the record for the inspector


def _short(identifier: str) -> str:
    """ARN or plain identifier -> resource name."""
    if not identifier:
        return ""
    return identifier.rsplit(":", 1)[-1] if identifier.startswith("arn:") else identifier


def _joined(values: Iterable[str]) -> str:
    seen: List[str] = []
    for v in values:
        v = str(v or "").replace(",", " ").strip()
        if v and v not in seen:
            seen.append(v)
    return ",".join(seen)


def _subnet_group_facts(group: Optional[dict]) -> dict:
    """VPC, subnet ids and AZ names from a DBSubnetGroup payload."""
    if not group:
        return {}
    subnets = group.get("Subnets", []) or []
    return {
        "vpc_id": group.get("VpcId", ""),
        "subnet_ids": _joined(s.get("SubnetIdentifier", "") for s in subnets),
        "subnet_azs": _joined(sorted((s.get("SubnetAvailabilityZone") or {}).get("Name", "") for s in subnets)),
    }


def _common_tags(obj: dict) -> dict:
    return {
        "engine_version": obj.get("EngineVersion", ""),
        "multi_az": str(obj.get("MultiAZ", False)),
        "storage_encrypted": str(obj.get("StorageEncrypted", False)),
        "kms_key_id": obj.get("KmsKeyId", ""),
        "pi_enabled": str(obj.get("PerformanceInsightsEnabled", False)),
        "pi_retention": str(obj.get("PerformanceInsightsRetentionPeriod", 0)),
        "log_exports": ",".join(obj.get("EnabledCloudwatchLogsExports", []) or []),
        "deletion_protection": str(obj.get("DeletionProtection", False)),
        "backup_retention": str(obj.get("BackupRetentionPeriod", 0)),
    }


def _cluster_metadata(rds, identifier: str) -> Optional[dict]:
    """Aurora cluster facts, or None when `identifier` is not a cluster."""
    try:
        cluster = rds.describe_db_clusters(DBClusterIdentifier=identifier)["DBClusters"][0]
    except Exception:
        return None

    tags = _common_tags(cluster)
    tags["parameter_group"] = cluster.get("DBClusterParameterGroup", "")
    members = cluster.get("DBClusterMembers", []) or []
    tags["cluster_members"] = _joined(m.get("DBInstanceIdentifier", "") for m in members)
    tags["replicas"] = _joined(_short(r) for r in cluster.get("ReadReplicaIdentifiers", []) or [])
    tags["replica_of"] = _short(cluster.get("ReplicationSourceIdentifier", ""))

    fields = {}
    if cluster.get("ReaderEndpoint"):
        fields["reader_endpoint"] = cluster["ReaderEndpoint"]

    # Member instances: role, stable endpoint, and the class the cluster actually runs on.
    instances = {}
    try:
        resp = rds.describe_db_instances(Filters=[{"Name": "db-cluster-id", "Values": [identifier]}])
        instances = {i["DBInstanceIdentifier"]: i for i in resp.get("DBInstances", [])}
    except Exception as e:
        logger.warning(f"member lookup failed for {identifier}: {e}")
    roles = []
    for m in members:
        mid = m.get("DBInstanceIdentifier", "")
        inst = instances.get(mid, {})
        roles.append({
            "instance_id": mid,
            "is_writer": bool(m.get("IsClusterWriter", False)),
            "endpoint": (inst.get("Endpoint") or {}).get("Address", ""),
            "availability_zone": inst.get("AvailabilityZone", ""),
            "instance_class": inst.get("DBInstanceClass", ""),
        })
    if roles:
        fields["cluster_member_roles"] = roles
    writer = next((r["instance_id"] for r in roles if r["is_writer"]), roles[0]["instance_id"] if roles else "")
    tags["instance_class"] = (instances.get(writer) or {}).get("DBInstanceClass", "")

    # Placement: the cluster carries only the subnet group NAME.
    group_name = cluster.get("DBSubnetGroup", "")
    placement = {}
    if group_name:
        try:
            group = rds.describe_db_subnet_groups(DBSubnetGroupName=group_name)["DBSubnetGroups"][0]
            placement = _subnet_group_facts(group)
        except Exception as e:
            logger.debug(f"subnet group lookup failed for {identifier}: {e}")
    sg_ids = _joined(sg.get("VpcSecurityGroupId", "") for sg in cluster.get("VpcSecurityGroups", []) or [])
    return {"tags": tags, "fields": fields, "placement": placement, "security_group_ids": sg_ids,
            "member_ids": [r["instance_id"] for r in roles]}


def _instance_metadata(rds, identifier: str) -> Optional[dict]:
    try:
        inst = rds.describe_db_instances(DBInstanceIdentifier=identifier)["DBInstances"][0]
    except Exception:
        return None
    tags = _common_tags(inst)
    groups = inst.get("DBParameterGroups", []) or []
    tags["parameter_group"] = groups[0].get("DBParameterGroupName", "") if groups else ""
    tags["instance_class"] = inst.get("DBInstanceClass", "")
    tags["storage_type"] = inst.get("StorageType", "")
    tags["allocated_storage"] = str(inst.get("AllocatedStorage", 0))
    tags["replicas"] = _joined(_short(r) for r in inst.get("ReadReplicaDBInstanceIdentifiers", []) or [])
    tags["replica_of"] = _short(inst.get("ReadReplicaSourceDBInstanceIdentifier", ""))
    # The source's endpoint, which the secret-host rule accepts for a replica (it shares
    # the source's read-only user). Same-Region sources resolve here; collect_db_metadata
    # tries a cross-Region one with the session.
    source = inst.get("ReadReplicaSourceDBInstanceIdentifier", "") or ""
    tags["replica_source_endpoint"] = ""
    if source and not source.startswith("arn:"):
        try:
            src = rds.describe_db_instances(DBInstanceIdentifier=source)["DBInstances"][0]
            tags["replica_source_endpoint"] = (src.get("Endpoint") or {}).get("Address", "") or ""
        except Exception:
            pass
    tags["availability_zone"] = _joined(
        z for z in (inst.get("AvailabilityZone", ""), inst.get("SecondaryAvailabilityZone", "")) if z)
    placement = _subnet_group_facts(inst.get("DBSubnetGroup"))
    sg_ids = _joined(sg.get("VpcSecurityGroupId", "") for sg in inst.get("VpcSecurityGroups", []) or [])
    return {"tags": tags, "fields": {}, "placement": placement, "security_group_ids": sg_ids,
            "member_ids": [], "replica_source": source}


def _log_groups(session, identifier: str) -> str:
    try:
        logs = session.client("logs")
    except Exception:
        return ""
    found: List[str] = []
    for prefix in (f"/aws/rds/cluster/{identifier}/", f"/aws/rds/instance/{identifier}/"):
        try:
            resp = logs.describe_log_groups(logGroupNamePrefix=prefix, limit=10)
            found.extend(lg.get("logGroupName", "") for lg in resp.get("logGroups", []))
        except Exception:
            pass
    return _joined(found)


def _alarms(session, identifier: str, member_ids: List[str]) -> dict:
    """Metric alarms whose dimensions name this database (or one of its members).

    Uses DescribeAlarms, which the cross-account boundary allows, instead of
    DescribeAlarmsForMetric (denied there, and limited to one metric anyway).
    """
    wanted = {identifier, *member_ids}
    try:
        cw = session.client("cloudwatch")
    except Exception:
        return {}
    matched = []
    token = None
    for _ in range(MAX_ALARM_PAGES):
        kwargs = {"AlarmTypes": ["MetricAlarm"], "MaxRecords": 100}
        if token:
            kwargs["NextToken"] = token
        try:
            resp = cw.describe_alarms(**kwargs)
        except Exception as e:
            logger.debug(f"alarm listing failed for {identifier}: {e}")
            return {}
        for alarm in resp.get("MetricAlarms", []):
            dims = alarm.get("Dimensions", []) or []
            if any(d.get("Name") in ("DBInstanceIdentifier", "DBClusterIdentifier") and d.get("Value") in wanted
                   for d in dims):
                matched.append(alarm)
        token = resp.get("NextToken")
        if not token:
            break
    return {
        "alarm_count": str(len(matched)),
        "alarms_in_alarm": str(sum(1 for a in matched if a.get("StateValue") == "ALARM")),
        "alarm_names": _joined(a.get("AlarmName", "") for a in matched[:MAX_ALARM_NAMES]),
    }


def _vpc_cidr(session, vpc_id: str) -> str:
    if not vpc_id:
        return ""
    try:
        ec2 = session.client("ec2")
        vpcs = ec2.describe_vpcs(VpcIds=[vpc_id]).get("Vpcs", [])
        return vpcs[0].get("CidrBlock", "") if vpcs else ""
    except Exception:
        return ""  # denied under the cross-account boundary — expected


def collect_db_metadata(session, identifier: str, existing: Optional[dict] = None) -> Optional[dict]:
    """Gather the registry update for one database. None if RDS does not know it."""
    existing = existing or {}
    rds = session.client("rds")
    core = _cluster_metadata(rds, identifier) or _instance_metadata(rds, identifier)
    if core is None:
        return None

    tags = dict(core["tags"])
    fields = dict(core["fields"])
    placement = core["placement"]

    # A cross-Region replica source is an ARN: resolve it in its own Region, else keep
    # the endpoint already recorded for the same source (discovery's).
    source = core.get("replica_source", "")
    if source and not tags.get("replica_source_endpoint"):
        if source.startswith("arn:") and source.count(":") >= 4:
            try:
                src = session.client("rds", region_name=source.split(":")[3]).describe_db_instances(
                    DBInstanceIdentifier=source)["DBInstances"][0]
                tags["replica_source_endpoint"] = (src.get("Endpoint") or {}).get("Address", "") or ""
            except Exception:
                pass
        old = existing.get("tags") or {}
        if not tags.get("replica_source_endpoint") and old.get("replica_of") == tags.get("replica_of"):
            tags["replica_source_endpoint"] = old.get("replica_source_endpoint", "")

    # Backfill placement the onboarding path did not record (older rows).
    for key in ("vpc_id", "subnet_ids"):
        if placement.get(key) and not existing.get(key):
            fields[key] = placement[key]
    if core["security_group_ids"] and not existing.get("security_group_ids"):
        fields["security_group_ids"] = core["security_group_ids"]
    if placement.get("subnet_azs"):
        tags["subnet_azs"] = placement["subnet_azs"]

    vpc_id = existing.get("vpc_id") or placement.get("vpc_id", "")
    cidr = _vpc_cidr(session, vpc_id)
    if cidr:
        tags["vpc_cidr"] = cidr

    log_groups = _log_groups(session, identifier)
    if log_groups:
        tags["log_groups"] = log_groups
    tags.update(_alarms(session, identifier, core["member_ids"]))
    tags["metadata_refreshed_at"] = datetime.now(timezone.utc).isoformat()
    return {"tags": tags, "fields": fields}
