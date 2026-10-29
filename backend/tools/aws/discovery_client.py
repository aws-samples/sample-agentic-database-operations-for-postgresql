# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Cross-account database auto-discovery via RDS API."""

import os
import logging
from typing import Optional
import boto3
from tools.resource_names import discovery_role_arn

logger = logging.getLogger("discovery-client")


def _is_postgres(engine: str) -> bool:
    """Whether an RDS engine identifier is one this agent can analyse.

    RDS reports PostgreSQL as ``postgres`` for instances and ``aurora-postgresql``
    for clusters. Matching on the substring rather than an exact list keeps out the
    engines that share the same APIs but not the same catalogue: the bare ``aurora``
    of legacy Aurora MySQL, plus ``docdb`` and ``neptune`` clusters, which are both
    returned by describe_db_clusters.
    """
    return "postgres" in (engine or "").lower()


def canonical_engine(engine: str) -> str:
    """The record engine for an RDS engine identifier.

    RDS says ``postgres`` for a PostgreSQL instance (and for an RDS Multi-AZ DB cluster)
    and ``aurora-postgresql`` for an Aurora cluster. Records use ``rds-postgresql`` for
    the former, the same name the onboarding refresh writes (resource_explorer_client),
    so the console's engine filter sees one RDS PostgreSQL, not two spellings of it.
    """
    e = (engine or "").lower()
    if e.startswith("aurora"):
        return "aurora-postgresql" if "postgres" in e else e
    return "rds-postgresql" if "postgres" in e else (engine or "")


def _short(identifier: str) -> str:
    """ARN or plain identifier -> resource name."""
    if not identifier:
        return ""
    return identifier.rsplit(":", 1)[-1] if identifier.startswith("arn:") else identifier


def replica_source_endpoint(session, source: str, default_region: str = "", known: Optional[dict] = None) -> str:
    """The endpoint of an RDS read replica's replication source, as RDS reports it.

    `source` is ``ReadReplicaSourceDBInstanceIdentifier``: a plain identifier when the
    source is in the same account and Region, an ARN otherwise. `known` maps identifiers
    already listed in this run to their endpoints, saving a call. Any failure (a source in
    another account, a Region the session cannot reach) returns "", which leaves the
    strict secret-host rule in place for that replica."""
    if not source:
        return ""
    if known and not source.startswith("arn:") and known.get(source):
        return known[source]
    region = source.split(":")[3] if source.startswith("arn:") and source.count(":") >= 4 else default_region
    try:
        rds = session.client("rds", region_name=region) if region else session.client("rds")
        inst = rds.describe_db_instances(DBInstanceIdentifier=source)["DBInstances"][0]
        return (inst.get("Endpoint") or {}).get("Address", "") or ""
    except Exception as e:
        logger.info(f"replica source {source} not resolvable: {type(e).__name__}")
        return ""


def global_cluster_members(rds) -> dict:
    """Aurora Global Database membership: ``{DBClusterArn: {"global_cluster_id", "global_cluster_role"}}``
    for every member cluster (role ``primary`` for the writer cluster, else ``secondary``).
    Raises on an API failure so the caller can leave earlier marks unchanged."""
    marks = {}
    paginator = rds.get_paginator("describe_global_clusters")
    for page in paginator.paginate():
        for gc in page.get("GlobalClusters", []) or []:
            gid = gc.get("GlobalClusterIdentifier", "")
            for m in gc.get("GlobalClusterMembers", []) or []:
                arn = m.get("DBClusterArn", "")
                if arn:
                    marks[arn] = {"global_cluster_id": gid,
                                  "global_cluster_role": "primary" if m.get("IsWriter") else "secondary"}
    return marks


class DbDiscovery:
    """Discovers RDS/Aurora instances across AWS accounts."""

    def __init__(self):
        self._sts_client = None

    @property
    def sts_client(self):
        if self._sts_client is None:
            self._sts_client = boto3.client(
                "sts",
                region_name=os.getenv("AWS_REGION", "us-east-1"),
            )
        return self._sts_client

    def _get_cross_account_session(self, account_id: str, region: str,
                                   role_name: str = None, space_id: str = ""):
        """Assume a role in the target account on behalf of ``space_id``.

        ``space_id`` is required in effect: the deployment presents the id of the admin
        who OWNS that space as ``sts:ExternalId`` (see session_factory.external_id_for_space),
        and the roles this assumes carry a trust policy requiring that admin's id. Without
        it the assume would be refused by the far side, so an empty value is reported here
        instead of being sent and failing as an opaque AccessDenied.
        """
        from tools.aws.session_factory import external_id_for_space
        if role_name:
            role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
        else:
            role_arn = discovery_role_arn(account_id)

        external_id = external_id_for_space(space_id)
        if not external_id:
            logger.warning(
                "Cannot assume %s: no space context, so no sts:ExternalId to present. "
                "The target role's trust policy requires the id of the admin who owns the "
                "space it serves.", role_arn,
            )
            return None

        try:
            creds = self.sts_client.assume_role(
                RoleArn=role_arn,
                RoleSessionName=f"db-discovery-{account_id}-{region}",
                DurationSeconds=900,
                ExternalId=external_id,
            )["Credentials"]
            return boto3.Session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
                region_name=region,
            )
        except Exception as e:
            logger.warning(f"Cannot assume role {role_arn} in {account_id}: {e}")
            return None

    def _resolve_subnet_group(self, rds_client, subnet_group_name: str) -> tuple:
        if not subnet_group_name:
            return "", ""
        try:
            resp = rds_client.describe_db_subnet_groups(DBSubnetGroupName=subnet_group_name)
            group = resp["DBSubnetGroups"][0]
            vpc_id = group.get("VpcId", "")
            subnets = ",".join(s["SubnetIdentifier"] for s in group.get("Subnets", []))
            return vpc_id, subnets
        except Exception as e:
            logger.warning(f"Failed to resolve subnet group {subnet_group_name}: {e}")
            return "", ""

    def _get_vpc_info_from_cluster(self, rds, cluster: dict) -> tuple:
        sg_ids = ",".join(s["VpcSecurityGroupId"] for s in cluster.get("VpcSecurityGroups", []))
        vpc_id, subnet_ids = self._resolve_subnet_group(rds, cluster.get("DBSubnetGroup", ""))
        return vpc_id, subnet_ids, sg_ids

    def _get_vpc_info_from_instance(self, rds, db: dict) -> tuple:
        sg_ids = ",".join(s["VpcSecurityGroupId"] for s in db.get("VpcSecurityGroups", []))
        sg_info = db.get("DBSubnetGroup", {})
        vpc_id = sg_info.get("VpcId", "")
        subnet_group_name = sg_info.get("DBSubnetGroupName", "")
        if not vpc_id and subnet_group_name:
            vpc_id, subnet_ids = self._resolve_subnet_group(rds, subnet_group_name)
        else:
            _, subnet_ids = self._resolve_subnet_group(rds, subnet_group_name)
        return vpc_id, subnet_ids, sg_ids

    def discover_instances_in_account(
        self, account_id: str, regions: Optional[list[str]] = None,
        role_name: str = None, scan_log: list = None, space_id: str = "",
        completion: dict = None,
    ) -> list[dict]:
        """Discover the PostgreSQL RDS/Aurora databases in a single account.

        Every other engine is left out: the analysis tooling reads PostgreSQL
        catalogue views, and the registration path assumes a PostgreSQL endpoint.
        Skipped engines are named in ``scan_log`` so an operator can tell the
        difference between a database this agent will not analyse and one the scan
        could not reach.
        """
        if not regions:
            regions = [os.getenv("AWS_REGION", "us-east-1")]
        if scan_log is None:
            scan_log = []

        current_account = self.sts_client.get_caller_identity()["Account"]
        instances = []

        for region in regions:
            # Get session
            if account_id == current_account:
                session = boto3.Session(region_name=region)
                scan_log.append(f"{account_id}/{region}: local credentials")
            else:
                session = self._get_cross_account_session(
                    account_id, region, role_name=role_name, space_id=space_id
                )
                if not session:
                    scan_log.append(f"{account_id}/{region}: FAILED role '{role_name or 'default'}'")
                    continue
                scan_log.append(f"{account_id}/{region}: assumed role '{role_name}'")

            rds = session.client("rds")
            clusters_found = 0
            instances_found = 0
            skipped_engines = []
            cluster_ids = set()  # Track cluster IDs to skip their member instances
            region_start = len(instances)
            # Did this (account, region) enumerate fully? Tracked separately for clusters
            # and for instances, so retirement only removes a kind of row from a pair
            # whose enumeration of that kind actually completed. A failed call leaves the
            # flag False, and the pair's rows of that kind are kept rather than retired.
            clusters_ok = True
            instances_ok = True

            # 0. Aurora Global Database membership in this Region. Members are listed
            # with a "not supported yet" reason and cannot be onboarded. A failed call
            # leaves earlier marks alone (global_checked False) rather than clearing them.
            global_marks, global_checked = {}, True
            try:
                global_marks = global_cluster_members(rds)
            except Exception as e:
                global_checked = False
                logger.warning(f"Failed to list global clusters in {account_id}/{region}: {e}")
                scan_log.append(f"{account_id}/{region}: global cluster scan error: {e}")

            # 1. Discover ALL clusters (Aurora)
            try:
                paginator = rds.get_paginator("describe_db_clusters")
                for page in paginator.paginate():
                    for cluster in page["DBClusters"]:
                        cluster_id = cluster["DBClusterIdentifier"]
                        engine = cluster.get("Engine", "")
                        # Recorded before the engine test: the set tracks which
                        # identifiers belong to a cluster, whatever its engine.
                        cluster_ids.add(cluster_id)

                        if not _is_postgres(engine):
                            skipped_engines.append(engine or "unknown")
                            continue

                        vpc_id, subnet_ids, sg_ids = self._get_vpc_info_from_cluster(rds, cluster)

                        instances.append({
                            "account_id": account_id,
                            "region": region,
                            "engine": canonical_engine(engine),
                            "identifier": cluster_id,
                            "endpoint": cluster.get("Endpoint", ""),
                            "port": cluster.get("Port", 5432),
                            "database_name": cluster.get("DatabaseName", "postgres"),
                            "engine_version": cluster.get("EngineVersion", ""),
                            "status": cluster.get("Status", ""),
                            "multi_az": cluster.get("MultiAZ", False),
                            "storage_encrypted": cluster.get("StorageEncrypted", False),
                            "cluster_arn": cluster.get("DBClusterArn", ""),
                            "vpc_id": vpc_id,
                            "subnet_ids": subnet_ids,
                            "security_group_ids": sg_ids,
                            "secret_arn": "",
                            "parameter_group": cluster.get("DBClusterParameterGroup", ""),
                            "kms_key_id": cluster.get("KmsKeyId", ""),
                            "pi_enabled": cluster.get("PerformanceInsightsEnabled", False),
                            "pi_retention": cluster.get("PerformanceInsightsRetentionPeriod", 0),
                            "log_exports": cluster.get("EnabledCloudwatchLogsExports", []),
                            "deletion_protection": cluster.get("DeletionProtection", False),
                            "backup_retention": cluster.get("BackupRetentionPeriod", 0),
                            # Cluster describe does not expose an instance class (it is
                            # per member instance); member ids are in cluster_members below.
                            "instance_class": "",
                            "cluster_members": [m.get("DBInstanceIdentifier", "") for m in cluster.get("DBClusterMembers", [])],
                            "global_checked": global_checked,
                            **global_marks.get(cluster.get("DBClusterArn", ""),
                                               {"global_cluster_id": "", "global_cluster_role": ""}),
                        })
                        clusters_found += 1
            except Exception as e:
                clusters_ok = False
                logger.warning(f"Failed to list clusters in {account_id}/{region}: {e}")
                scan_log.append(f"{account_id}/{region}: cluster scan error: {e}")

            # 2. Discover ALL standalone instances (skip Aurora members)
            try:
                paginator = rds.get_paginator("describe_db_instances")
                for page in paginator.paginate():
                    for db in page["DBInstances"]:
                        # Skip Aurora member instances — already covered by cluster
                        if db.get("DBClusterIdentifier"):
                            continue

                        inst_id = db["DBInstanceIdentifier"]
                        engine = db.get("Engine", "")
                        # Tested before the VPC lookups, which cost an API call each.
                        if not _is_postgres(engine):
                            skipped_engines.append(engine or "unknown")
                            continue

                        vpc_id, subnet_ids, sg_ids = self._get_vpc_info_from_instance(rds, db)

                        instances.append({
                            "account_id": account_id,
                            "region": region,
                            "engine": canonical_engine(engine),
                            "identifier": inst_id,
                            "endpoint": db.get("Endpoint", {}).get("Address", ""),
                            "port": db.get("Endpoint", {}).get("Port", 5432),
                            "database_name": db.get("DBName", ""),
                            "engine_version": db.get("EngineVersion", ""),
                            "status": db.get("DBInstanceStatus", ""),
                            "multi_az": db.get("MultiAZ", False),
                            "storage_encrypted": db.get("StorageEncrypted", False),
                            "instance_class": db.get("DBInstanceClass", ""),
                            "instance_arn": db.get("DBInstanceArn", ""),
                            "vpc_id": vpc_id,
                            "subnet_ids": subnet_ids,
                            "security_group_ids": sg_ids,
                            "secret_arn": "",
                            "parameter_group": (db.get("DBParameterGroups", [{}])[0].get("DBParameterGroupName", "") if db.get("DBParameterGroups") else ""),
                            "kms_key_id": db.get("KmsKeyId", ""),
                            "pi_enabled": db.get("PerformanceInsightsEnabled", False),
                            "pi_retention": db.get("PerformanceInsightsRetentionPeriod", 0),
                            "log_exports": db.get("EnabledCloudwatchLogsExports", []),
                            "deletion_protection": db.get("DeletionProtection", False),
                            "backup_retention": db.get("BackupRetentionPeriod", 0),
                            "storage_type": db.get("StorageType", ""),
                            "allocated_storage": db.get("AllocatedStorage", 0),
                            # RDS read replica: its own record, sharing the source's
                            # read-only user. The source endpoint is resolved below.
                            "replica_of": _short(db.get("ReadReplicaSourceDBInstanceIdentifier", "")),
                            "replica_source": db.get("ReadReplicaSourceDBInstanceIdentifier", "") or "",
                            "replica_source_endpoint": "",
                        })
                        instances_found += 1
            except Exception as e:
                instances_ok = False
                logger.warning(f"Failed to list instances in {account_id}/{region}: {e}")
                scan_log.append(f"{account_id}/{region}: instance scan error: {e}")

            # Resolve each read replica's source endpoint: from this Region's listing when
            # the source is here, else (an ARN) by a best-effort describe in its Region.
            listed = {i["identifier"]: i["endpoint"] for i in instances[region_start:] if i.get("endpoint")}
            for inst in instances[region_start:]:
                source = inst.pop("replica_source", "") if "replica_source" in inst else ""
                if source:
                    inst["replica_source_endpoint"] = replica_source_endpoint(session, source, region, listed)

            if completion is not None:
                completion[(account_id, region)] = {"clusters_ok": clusters_ok,
                                                     "instances_ok": instances_ok}

            summary = (f"{account_id}/{region}: {clusters_found} clusters, "
                       f"{instances_found} standalone instances")
            if skipped_engines:
                engines = ", ".join(sorted(set(skipped_engines)))
                summary += f" ({len(skipped_engines)} skipped, not PostgreSQL: {engines})"
            scan_log.append(summary)

        return instances

    def discover_all(self, regions: Optional[list[str]] = None,
                     account_ids: Optional[list[str]] = None,
                     role_names: Optional[dict] = None,
                     space_id: str = "",
                     regions_by_account: Optional[dict] = None):
        """Discover databases across accounts.

        ``regions_by_account`` ({account_id: [regions]}) scans each account in exactly its
        own Regions, including none: an empty list skips the account (e.g. the deployment
        account with no databases). An account absent from the map falls back to
        ``regions``. Without it every account is scanned in ``regions`` (the accounts x
        regions cross-product).

        Returns:
            Tuple of (instances_list, scan_log_list, completion_map). The completion map
            is keyed by (account_id, region) and carries ``clusters_ok``/``instances_ok``:
            only a pair present here with the relevant flag True may retire that kind of
            row, so a partial enumeration failure never deletes live candidates.
        """
        if account_ids:
            accounts = [{"account_id": aid} for aid in account_ids]
        else:
            # The live path always passes the space's accounts (app.py discover_space_
            # databases -> _discovery_scope), so this is only a defensive fallback: scan
            # the deployment account alone. Discovery deliberately never fans out across
            # an AWS Organization — accounts are the ones an admin added to the space.
            current = self.sts_client.get_caller_identity()["Account"]
            accounts = [{"account_id": current}]

        role_names = role_names or {}
        regions_by_account = regions_by_account or {}
        all_instances = []
        scan_log = []
        completion = {}

        for acct in accounts:
            aid = acct["account_id"]
            # An account explicitly listed in regions_by_account is scanned in EXACTLY its
            # Regions, including none: an empty list means "skip this account". Only an
            # account absent from the map falls back to the cross-product `regions`, so a
            # deliberately-empty account is never silently scanned in the union instead.
            if regions_by_account and aid in regions_by_account:
                acct_regions = regions_by_account[aid]
            else:
                acct_regions = regions
            if not acct_regions:
                scan_log.append(f"{aid}: no Regions configured, skipped")
                continue
            found = self.discover_instances_in_account(
                aid, acct_regions, role_name=role_names.get(aid), scan_log=scan_log,
                space_id=space_id, completion=completion,
            )
            all_instances.extend(found)

        return all_instances, scan_log, completion


db_discovery = DbDiscovery()
