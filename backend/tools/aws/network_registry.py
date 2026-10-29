# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""DynamoDB registry for network connections (peering and Lattice resource endpoints).

One row per connection, reference-counted by the database records that use it:

  connection_id: deterministic (see make_connection_id)
  method: "peering" (per VPC pair), "lattice" (resource endpoint, per database) or
          "lattice-gateway" (resource gateway, per database VPC)
  db_vpc_id, db_vpc_cidr, db_region, db_account_id, agent_vpc_id
  peering_id: peering connection id (peering only)
  vpce_id, resource_configuration_arn, resource_share_arn (lattice only)
  resource_gateway_id, resource_gateway_arn, gateway_security_group_id (lattice-gateway only)
  status, ref_count, ref_instances, created_at, updated_at
  failed_step, failure_detail (status delete_failed only)

status: "active"; "orphaned" once the last reference is gone; "delete_failed" when a Lattice teardown was refused or timed out part
way — the row then names only the resources still left, `failed_step` says which delete
stopped it (endpoint, resource_configuration, resource_share, resource_gateway or
gateway_security_group) and `failure_detail` the error, and the next cleanup or
Configure for that VPC finishes the job. A row is deleted only once everything it names
is confirmed gone.
"""

import os
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional
import boto3
from boto3.dynamodb.conditions import Key
from tools.resource_names import table_name

logger = logging.getLogger("network-registry")



class NetworkRegistry:

    def __init__(self):
        self._table = None

    @property
    def table(self):
        if self._table is None:
            from botocore.config import Config
            dynamodb = boto3.resource(
                "dynamodb",
                region_name=os.getenv("AWS_REGION", "us-east-1"),
                config=Config(max_pool_connections=50, retries={"max_attempts": 3, "mode": "adaptive"}),
            )
            self._table = dynamodb.Table(table_name("network"))
        return self._table

    @staticmethod
    def make_connection_id(agent_vpc_id: str, db_vpc_id: str, db_endpoint: str = "", method: str = "") -> str:
        """Generate a deterministic connection ID."""
        if method == "lattice" and db_endpoint:
            # Resource endpoint: per database
            key = f"{agent_vpc_id}:{db_endpoint}:lattice"
        elif method == "lattice-gateway":
            # Resource gateway: per database VPC, shared by its databases
            key = f"{agent_vpc_id}:{db_vpc_id}:lattice-gateway"
        else:
            # Peering: per-VPC-pair
            key = f"{agent_vpc_id}:{db_vpc_id}:peering"
        # Not security-sensitive: just a deterministic, collision-tolerant
        # connection-ID derived from non-secret VPC/endpoint identifiers.
        return hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()[:16]

    def get_connection(self, connection_id: str) -> Optional[dict]:
        resp = self.table.get_item(Key={"connection_id": connection_id})
        return resp.get("Item")

    def find_by_vpc(self, db_vpc_id: str) -> list:
        """Find all connections to a given DB VPC."""
        from tools.aws.ddb_pages import read_all
        return read_all(self.table.query, IndexName="vpc-index",
                        KeyConditionExpression=Key("db_vpc_id").eq(db_vpc_id))

    def find_active_peering(self, db_vpc_id: str) -> Optional[dict]:
        """Find an active peering connection to the given VPC."""
        conns = self.find_by_vpc(db_vpc_id)
        for c in conns:
            if c.get("method") == "peering" and c.get("status") == "active":
                return c
        return None

    def find_by_peering_id(self, peering_id: str, db_vpc_id: str) -> Optional[dict]:
        """The connection record for a specific peering to the given DB VPC, or None.

        Uses the vpc-index (every peering this registry tracks is keyed by its DB VPC), so
        no extra index is needed. Used by the route-conflict classifier to tell whether a
        peering tagged as ours still has live references (then it is in use, not a leftover)."""
        if not peering_id or not db_vpc_id:
            return None
        for c in self.find_by_vpc(db_vpc_id):
            if c.get("peering_id") == peering_id:
                return c
        return None

    def find_active_lattice(self, db_endpoint: str, agent_vpc_id: str) -> Optional[dict]:
        """Find an active resource-endpoint connection for the given database."""
        conn_id = self.make_connection_id(agent_vpc_id, "", db_endpoint, "lattice")
        conn = self.get_connection(conn_id)
        if conn and conn.get("status") == "active":
            return conn
        return None

    def create_connection(self, data: dict) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        instance_id = data.get("instance_id", "")
        ref_instances = [instance_id] if instance_id else []
        item = {
            "connection_id": data["connection_id"],
            "method": data.get("method", "peering"),
            "db_vpc_id": data.get("db_vpc_id", ""),
            "db_vpc_cidr": data.get("db_vpc_cidr", ""),
            "db_region": data.get("db_region", ""),
            "db_account_id": data.get("db_account_id", ""),
            "agent_vpc_id": data.get("agent_vpc_id", ""),
            # The agent CIDR the DB-side route was written for, recorded at creation so
            # teardown removes exactly that route even if the agent CIDR changes between
            # generations (deleting by the then-current CIDR would miss the old route and
            # leave the stale leftover that causes a route conflict on the next onboard).
            "agent_vpc_cidr": data.get("agent_vpc_cidr", ""),
            "peering_id": data.get("peering_id", ""),
            "vpce_id": data.get("vpce_id", ""),
            "resource_configuration_arn": data.get("resource_configuration_arn", ""),
            "resource_share_arn": data.get("resource_share_arn", ""),
            "resource_gateway_id": data.get("resource_gateway_id", ""),
            "resource_gateway_arn": data.get("resource_gateway_arn", ""),
            "gateway_security_group_id": data.get("gateway_security_group_id", ""),
            "status": data.get("status", "active"),
            "ref_count": len(ref_instances),
            "ref_instances": ref_instances,
            "created_at": now,
            "updated_at": now,
        }
        self.table.put_item(Item=item)
        return item

    def update_connection(self, connection_id: str, data: dict) -> Optional[dict]:
        now = datetime.now(timezone.utc).isoformat()
        update_parts = ["#updated_at = :updated_at"]
        names = {"#updated_at": "updated_at"}
        values = {":updated_at": now}

        allowed = [
            "status", "peering_id", "vpce_id", "resource_configuration_arn",
            "resource_share_arn", "resource_gateway_id", "resource_gateway_arn",
            "gateway_security_group_id", "ref_count", "ref_instances", "db_vpc_cidr",
            "failed_step", "failure_detail",
        ]
        for field in allowed:
            if field in data:
                safe = f"#{field}"
                update_parts.append(f"{safe} = :{field}")
                names[safe] = field
                values[f":{field}"] = data[field]

        self.table.update_item(
            Key={"connection_id": connection_id},
            UpdateExpression="SET " + ", ".join(update_parts),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return self.get_connection(connection_id)

    def add_reference(self, connection_id: str, instance_id: str) -> Optional[dict]:
        """Add a database reference to this connection. Increments ref_count."""
        if not instance_id:
            return self.get_connection(connection_id)
        conn = self.get_connection(connection_id)
        if not conn:
            return None
        refs = [r for r in conn.get("ref_instances", []) if r]  # filter empty
        if instance_id not in refs:
            refs.append(instance_id)
        return self.update_connection(connection_id, {
            "ref_count": len(refs),
            "ref_instances": refs,
        })

    def remove_reference(self, connection_id: str, instance_id: str) -> Optional[dict]:
        """Remove a database reference. Returns updated connection (or None if deleted)."""
        conn = self.get_connection(connection_id)
        if not conn:
            return None
        refs = [r for r in conn.get("ref_instances", []) if r and r != instance_id]  # filter empty + target
        if not refs:
            # No more references — mark for cleanup
            return self.update_connection(connection_id, {
                "ref_count": 0,
                "ref_instances": [],
                "status": "orphaned",
            })
        return self.update_connection(connection_id, {
            "ref_count": len(refs),
            "ref_instances": refs,
        })

    def delete_connection(self, connection_id: str):
        self.table.delete_item(Key={"connection_id": connection_id})


network_registry = NetworkRegistry()
