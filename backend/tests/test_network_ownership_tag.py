# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Network resources are tagged with the exact agent VPC that owns them, and the registry
records the agent CIDR a DB-side route was written for.

``ManagedBy`` isolates installations with DIFFERENT prefixes only; two installations with the
same prefix (dev and prod, or an earlier generation) share it and cannot be told apart. The
``<prefix>:agent_vpc`` tag pins a peering to one installation, which is what lets a
route-conflict claim prove a stale peering is OURS rather than another live deployment's.

Teardown deletes the DB-side route by the CIDR recorded at creation (falling back to the
current CIDR for records made before it was stored), so a CIDR change between generations
cannot leave the stale route that conflicts with the next onboard.

Hermetic: source is pinned by regex and the pure helpers are exercised. No AWS.

Run:  python3 backend/tests/test_network_ownership_tag.py
"""
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
NET_SRC = (ROOT / "tools/aws/network_client.py").read_text(encoding="utf-8")
REG_SRC = (ROOT / "tools/aws/network_registry.py").read_text(encoding="utf-8")
passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


from tools.resource_names import agent_vpc_tag_key, managed_by_tag, resource_prefix  # noqa: E402

print("the ownership tag key is prefix-scoped and distinct from ManagedBy")
os.environ.pop("RESOURCE_PREFIX", None)
check(agent_vpc_tag_key() == f"{resource_prefix()}:agent_vpc", f"key is <prefix>:agent_vpc ({agent_vpc_tag_key()})")
check(agent_vpc_tag_key() != "ManagedBy" and agent_vpc_tag_key() != managed_by_tag(),
      "it is a separate tag, not a reuse of ManagedBy")
os.environ["RESOURCE_PREFIX"] = "acme"
check(agent_vpc_tag_key() == "acme:agent_vpc", "the key follows the resource prefix")
os.environ.pop("RESOURCE_PREFIX", None)

print("a created peering carries the agent VPC ownership tag")
create = re.search(r'"ResourceType": "vpc-peering-connection",\s*"Tags": \[(.*?)\]', NET_SRC, re.S).group(1)
check('"Key": "ManagedBy"' in create, "ManagedBy is still set (prefix isolation is unchanged)")
check("agent_vpc_tag_key()" in create and "self.agent_vpc_id" in create,
      "the peering is tagged <prefix>:agent_vpc = this agent VPC id")

print("the registry records the agent CIDR the DB-side route was written for")
check('"agent_vpc_cidr": data.get("agent_vpc_cidr", "")' in REG_SRC,
      "create_connection stores agent_vpc_cidr")
reg_call = re.search(r"network_registry\.create_connection\(\{(.*?)\}\)", NET_SRC, re.S).group(1)
check('"agent_vpc_cidr": self.agent_vpc_cidr' in reg_call,
      "the peering path passes the CIDR it used into the record")

print("teardown deletes the DB-side route by the recorded CIDR, falling back to the current one")
cleanup = re.search(r"def cleanup_peering\(.*?(?=\n    def cleanup_lattice)", NET_SRC, re.S).group(0)
check('route_cidr = (conn or {}).get("agent_vpc_cidr") or self.agent_vpc_cidr' in cleanup,
      "route_cidr prefers the record's CIDR and falls back when absent (older records)")
check("DestinationCidrBlock=route_cidr" in cleanup, "delete_route uses route_cidr")
check("DestinationCidrBlock=self.agent_vpc_cidr" not in cleanup.split("Delete DB-side routes")[1].split("Delete the peering connection")[0],
      "the DB-side delete no longer hardcodes the current CIDR")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
