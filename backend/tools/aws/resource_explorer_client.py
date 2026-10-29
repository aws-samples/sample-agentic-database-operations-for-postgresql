# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Resource Explorer client — fast cross-region database discovery.

Uses AWS Resource Explorer 2 to find RDS/Aurora databases across all regions
in a single API call. Requires Resource Explorer to be enabled with an
AGGREGATOR index.
"""

import os
import logging
import boto3

logger = logging.getLogger("resource-explorer")


def _cluster_engine(engine: str) -> str:
    """The record engine for a DB cluster: Aurora clusters keep ``aurora-postgresql``;
    an RDS Multi-AZ DB cluster (RDS engine ``postgres``) is RDS PostgreSQL."""
    return "aurora-postgresql" if (engine or "").lower().startswith("aurora") else "rds-postgresql"


class ResourceExplorerClient:
    """Discover databases using AWS Resource Explorer."""

    def __init__(self):
        self._client = None
        self._region = os.getenv("AWS_REGION", "us-east-1")

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client(
                "resource-explorer-2", region_name=self._region
            )
        return self._client

    def enable_local_index(self, region: str = None) -> dict:
        """Create a LOCAL index in the specified region."""
        region = region or self._region
        try:
            client = boto3.client("resource-explorer-2", region_name=region)
            resp = client.create_index(Type="LOCAL")
            return {"status": "created", "region": region, "arn": resp.get("Arn", "")}
        except client.exceptions.ConflictException:
            return {"status": "already_exists", "region": region}
        except Exception as e:
            return {"status": "error", "region": region, "error": str(e)}

    def enable_aggregator(self, region: str = None) -> dict:
        """Create an AGGREGATOR index in the specified region."""
        region = region or self._region
        try:
            client = boto3.client("resource-explorer-2", region_name=region)
            try:
                client.create_index(Type="LOCAL")
            except Exception:
                pass
            client.update_index_type(
                Arn=self._get_index_arn(region),
                Type="AGGREGATOR"
            )
            return {"status": "created", "region": region}
        except Exception as e:
            if "already" in str(e).lower() or "AGGREGATOR" in str(e):
                return {"status": "already_exists", "region": region}
            return {"status": "error", "region": region, "error": str(e)}

    def _get_index_arn(self, region: str) -> str:
        """Get the index ARN for a region."""
        client = boto3.client("resource-explorer-2", region_name=region)
        resp = client.get_index()
        return resp.get("Arn", "")

    def setup_for_regions(self, regions: list) -> dict:
        """Enable Resource Explorer LOCAL indexes in specified regions
        and ensure an AGGREGATOR exists in the primary region."""
        results = {"regions": {}, "aggregator": None}
        for region in regions:
            result = self.enable_local_index(region)
            results["regions"][region] = result
        results["aggregator"] = self.enable_aggregator(self._region)
        return results

    def _enrich_cluster(self, rds, arn: str, region: str) -> dict:
        """Get full details for a DB cluster: Aurora PostgreSQL, or an RDS Multi-AZ DB
        cluster (engine ``postgres``)."""
        try:
            cluster_id = arn.split(":")[-1]
            resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
            cluster = resp["DBClusters"][0]

            # Skip non-PostgreSQL clusters
            engine = cluster.get("Engine", "")
            if "postgres" not in engine:
                return {"skip": True, "reason": f"Not PostgreSQL: {engine}"}

            # VPC info
            vpc_id, subnet_ids, sg_ids = "", "", ""
            subnet_group = cluster.get("DBSubnetGroup", "")
            if subnet_group:
                try:
                    sg_resp = rds.describe_db_subnet_groups(DBSubnetGroupName=subnet_group)
                    group = sg_resp["DBSubnetGroups"][0]
                    vpc_id = group.get("VpcId", "")
                    subnet_ids = ",".join(s["SubnetIdentifier"] for s in group.get("Subnets", []))
                except Exception:
                    pass

            sg_ids = ",".join(sg["VpcSecurityGroupId"] for sg in cluster.get("VpcSecurityGroups", []))

            secret_arn = ""
            master_secret = cluster.get("MasterUserSecret", {})
            if master_secret:
                secret_arn = master_secret.get("SecretArn", "")

            members = [
                {"instance_id": m["DBInstanceIdentifier"], "is_writer": m.get("IsClusterWriter", False)}
                for m in cluster.get("DBClusterMembers", [])
            ]
            # Per-instance endpoints (stable per instance; role can flip on
            # failover). Lets the operator focus chat on ONE specific member
            # deterministically instead of the round-robin reader endpoint.
            try:
                inst_resp = rds.describe_db_instances(
                    Filters=[{"Name": "db-cluster-id", "Values": [cluster_id]}]
                )
                instances = {i["DBInstanceIdentifier"]: i for i in inst_resp.get("DBInstances", [])}
                for m in members:
                    inst = instances.get(m["instance_id"], {})
                    m["endpoint"] = (inst.get("Endpoint") or {}).get("Address", "")
                    m["availability_zone"] = inst.get("AvailabilityZone", "")
                    m["instance_class"] = inst.get("DBInstanceClass", "")
            except Exception as e:
                logger.warning(f"member endpoint lookup failed for {cluster_id}: {e}")

            raw_id = cluster["DBClusterIdentifier"]

            return {
                "identifier": raw_id,
                # describe_db_clusters also returns RDS Multi-AZ DB clusters (Engine
                # "postgres"): those are RDS PostgreSQL with two readable standbys, not
                # Aurora, so they take the RDS engine value _enrich_instance uses.
                "engine": _cluster_engine(engine),
                "engine_version": cluster.get("EngineVersion", ""),
                "endpoint": cluster.get("Endpoint", ""),
                "reader_endpoint": cluster.get("ReaderEndpoint", ""),
                "port": cluster.get("Port", 5432),
                "database_name": cluster.get("DatabaseName", "postgres"),
                "vpc_id": vpc_id,
                "subnet_ids": subnet_ids,
                "security_group_ids": sg_ids,
                "secret_arn": secret_arn,
                "status": cluster.get("Status", ""),
                "multi_az": cluster.get("MultiAZ", False),
                "storage_encrypted": cluster.get("StorageEncrypted", False),
                "cluster_arn": arn,
                "region": region,
                "members": members,
                "resource_relationships": {
                    "type": "cluster",
                    "vpc": vpc_id,
                    "subnets": subnet_ids.split(",") if subnet_ids else [],
                    "security_groups": sg_ids.split(",") if sg_ids else [],
                    "instances": members,
                },
            }
        except Exception as e:
            logger.error(f"Failed to enrich cluster {arn}: {e}")
            return {"error": str(e), "arn": arn}

    def _enrich_instance(self, rds, arn: str, region: str) -> dict:
        """Get full details for a standalone RDS instance."""
        try:
            instance_id = arn.split(":")[-1]
            resp = rds.describe_db_instances(DBInstanceIdentifier=instance_id)
            db = resp["DBInstances"][0]

            # Skip Aurora members (covered by cluster enrichment)
            if db.get("DBClusterIdentifier"):
                return {"skip": True, "reason": "Aurora member — use cluster ARN"}

            # Skip non-PostgreSQL instances
            engine = db.get("Engine", "")
            if engine != "postgres":
                return {"skip": True, "reason": f"Not PostgreSQL: {engine}"}

            vpc_id = ""
            subnet_ids = ""
            subnet_group = db.get("DBSubnetGroup", {})
            if subnet_group:
                vpc_id = subnet_group.get("VpcId", "")
                subnet_ids = ",".join(s["SubnetIdentifier"] for s in subnet_group.get("Subnets", []))

            sg_ids = ",".join(sg["VpcSecurityGroupId"] for sg in db.get("VpcSecurityGroups", []))

            secret_arn = ""
            master_secret = db.get("MasterUserSecret", {})
            if master_secret:
                secret_arn = master_secret.get("SecretArn", "")

            raw_id = db["DBInstanceIdentifier"]
            # RDS read replica: the source as RDS reports it (never user input). Its
            # endpoint is resolved here when the source is in this Region; otherwise ""
            # and the caller keeps discovery's value for the same source.
            source = db.get("ReadReplicaSourceDBInstanceIdentifier", "") or ""
            source_endpoint = ""
            if source:
                try:
                    src = rds.describe_db_instances(DBInstanceIdentifier=source)["DBInstances"][0]
                    source_endpoint = (src.get("Endpoint") or {}).get("Address", "") or ""
                except Exception as e:
                    logger.info(f"replica source {source} not resolvable here: {type(e).__name__}")

            return {
                "identifier": raw_id,
                "replica_of": source.rsplit(":", 1)[-1] if source.startswith("arn:") else source,
                "replica_source_endpoint": source_endpoint,
                "engine": "rds-postgresql",
                "engine_version": db.get("EngineVersion", ""),
                "endpoint": db.get("Endpoint", {}).get("Address", ""),
                "port": db.get("Endpoint", {}).get("Port", 5432),
                "database_name": db.get("DBName", "postgres"),
                "instance_class": db.get("DBInstanceClass", ""),
                "vpc_id": vpc_id,
                "subnet_ids": subnet_ids,
                "security_group_ids": sg_ids,
                "secret_arn": secret_arn,
                "status": db.get("DBInstanceStatus", ""),
                "multi_az": db.get("MultiAZ", False),
                "storage_encrypted": db.get("StorageEncrypted", False),
                "instance_arn": arn,
                "region": region,
                "resource_relationships": {
                    "type": "instance",
                    "vpc": vpc_id,
                    "subnets": subnet_ids.split(",") if subnet_ids else [],
                    "security_groups": sg_ids.split(",") if sg_ids else [],
                },
            }
        except Exception as e:
            logger.error(f"Failed to enrich instance {arn}: {e}")
            return {"error": str(e), "arn": arn}


resource_explorer = ResourceExplorerClient()
