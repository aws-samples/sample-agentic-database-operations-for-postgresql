#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Guard: the Lattice path builds exactly what a database needs, shares it, cleans it up.

When a same-region database VPC's address range is already routed to another peered
VPC, the agent does not peer it. It builds a VPC Lattice resource gateway in the
database VPC (one per VPC, shared by its databases), a resource configuration naming
the database by ARN, shares that to the deployment account with RAM, accepts the share,
and creates a resource VPC endpoint in the agent VPC with private DNS on. Nothing is
routed: no route in either VPC, no load balancer. The database is still addressed by
its RDS hostname, so TLS stays verify-full.

The deployment-wide switch SAME_REGION_CONNECTIVITY=lattice makes that the rule for
every same-region database; cross-region always peers.

These tests drive the real NetworkConfigurator and the real NetworkRegistry with fake
AWS clients and a fake DynamoDB table - no AWS, no network. They look at what was
created, with which arguments, and what the registry holds afterwards.

Run:  python3 tests/test_lattice_path.py
"""
import importlib
import os
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

AGENT_ACCOUNT = "111111111111"
DB_ACCOUNT = "222222222222"
AGENT_VPC = "vpc-agent0000000001"
AGENT_CIDR = "172.20.0.0/16"
AGENT_RT = "rtb-agent000000001"
AGENT_SUBNETS = "subnet-agent01,subnet-agent02"
OTHER_VPC = "vpc-other000000001"
DB_VPC = "vpc-db000000000001"
DB_CIDR = "10.1.0.0/16"
DB_SG = "sg-db00000000001"
DB_SUBNETS = "subnet-db01,subnet-db02"
EP1 = "db1.cluster-abc.us-east-1.rds.amazonaws.com"
EP2 = "db2.cluster-def.us-east-1.rds.amazonaws.com"
ARN1 = f"arn:aws:rds:us-east-1:{DB_ACCOUNT}:cluster:db1"
ARN2 = f"arn:aws:rds:us-east-1:{DB_ACCOUNT}:cluster:db2"

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


# ── fakes ────────────────────────────────────────────────────────────────────────
#
# Defaults model today's synchronous world (everything ACTIVE at once, deletes immediate,
# one page). Knobs turn on the asynchronous and failing behaviour the later sections need:
#   page_size              every list/describe scan returns pages of this size
#   create_status          a new gateway's first status (e.g. CREATE_IN_PROGRESS)
#   polls_until_active     get_resource_gateway calls before it turns ACTIVE (None = never)
#   polls_until_gone       describe/get calls after a delete before the resource is gone
#   fail_once              {"api_name": "error text"}: the next call to that API raises once
# Dependencies are enforced like AWS does: a configuration bound to an endpoint, a gateway
# carrying a configuration, and a security group used by a gateway refuse deletion.

TIMELINE = []   # (who, api) across every fake, so tests can assert cross-account order


class Recorder:
    who = "?"

    def __init__(self):
        self.calls = []
        self.page_size = 0
        self.fail_once = {}

    def _rec(self, name, kw):
        self.calls.append((name, kw))
        TIMELINE.append((self.who, name))
        err = self.fail_once.pop(name, None)
        if err:
            raise Exception(err)

    def names(self):
        return [n for n, _ in self.calls]

    def args(self, name):
        return [kw for n, kw in self.calls if n == name]

    def _page(self, items, kw, token_key="nextToken"):
        """Slice `items` by page_size, following `token_key`."""
        if not self.page_size:
            return items, None
        start = int(kw.get(token_key) or 0)
        end = start + self.page_size
        return items[start:end], (str(end) if end < len(items) else None)


class FakeTime:
    """Deterministic clock: every bounded wait finishes instantly."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = 0

    def monotonic(self):
        return self.now

    def sleep(self, secs):
        self.sleeps += 1
        self.now += secs


def _rule_key(perm):
    return (perm.get("IpProtocol"), perm.get("FromPort"), perm.get("ToPort"),
            tuple(sorted(r.get("CidrIp", "") for r in perm.get("IpRanges", []))),
            tuple(sorted(p.get("GroupId", "") for p in perm.get("UserIdGroupPairs", []))))


class FakeEc2(Recorder):
    """One EC2 per account. Holds the security groups and endpoints it was asked to make."""

    def __init__(self, account, peerings=(), routes=()):
        super().__init__()
        self.who = f"ec2:{account}"
        self.account = account
        self.peerings = list(peerings)
        self.routes = list(routes)
        self.sgs = {}            # id -> {"name", "vpc", "ingress": [...]}
        self.endpoints = {}      # id -> {...}
        self.deleting = {}       # endpoint id -> polls left before it is gone
        self.polls_until_gone = 0
        self.sg_in_use = lambda gid: False
        self._n = 0

    def _nid(self, prefix):
        self._n += 1
        return f"{prefix}-{self.account[:4]}{self._n:08d}"

    def describe_vpc_peering_connections(self, **kw):
        self._rec("describe_vpc_peering_connections", kw)
        return {"VpcPeeringConnections": self.peerings}

    def describe_route_tables(self, **kw):
        self._rec("describe_route_tables", kw)
        return {"RouteTables": [{"RouteTableId": AGENT_RT, "Routes": self.routes}]}

    def describe_vpcs(self, **kw):
        self._rec("describe_vpcs", kw)
        return {"Vpcs": [{"VpcId": kw["VpcIds"][0], "CidrBlock": DB_CIDR}]}

    def describe_subnets(self, **kw):
        self._rec("describe_subnets", kw)
        return {"Subnets": []}

    def describe_security_groups(self, **kw):
        self._rec("describe_security_groups", kw)
        want = {f["Name"]: f["Values"] for f in kw.get("Filters", [])}
        out = [{"GroupId": gid, "GroupName": sg["name"], "VpcId": sg["vpc"]}
               for gid, sg in self.sgs.items()
               if sg["name"] in want.get("group-name", [sg["name"]]) and sg["vpc"] in want.get("vpc-id", [sg["vpc"]])]
        return {"SecurityGroups": out}

    def create_security_group(self, **kw):
        self._rec("create_security_group", kw)
        gid = self._nid("sg")
        self.sgs[gid] = {"name": kw["GroupName"], "vpc": kw["VpcId"], "ingress": [],
                         "tags": {t["Key"]: t["Value"] for ts in kw.get("TagSpecifications", []) for t in ts["Tags"]}}
        return {"GroupId": gid}

    def authorize_security_group_ingress(self, **kw):
        self._rec("authorize_security_group_ingress", kw)
        sg = self.sgs.setdefault(kw["GroupId"], {"name": "", "vpc": "", "ingress": []})
        have = {_rule_key(p) for p in sg["ingress"]}
        for perm in kw["IpPermissions"]:
            if _rule_key(perm) in have:
                raise Exception("An error occurred (InvalidPermission.Duplicate) when calling the "
                                "AuthorizeSecurityGroupIngress operation: the specified rule already exists")
        sg["ingress"].extend(kw["IpPermissions"])
        return {}

    def delete_security_group(self, **kw):
        self._rec("delete_security_group", kw)
        if kw["GroupId"] not in self.sgs:
            raise Exception("InvalidGroup.NotFound")
        if self.sg_in_use(kw["GroupId"]):
            raise Exception("DependencyViolation: resource has a dependent object")
        self.sgs.pop(kw["GroupId"], None)
        return {}

    def describe_vpc_endpoints(self, **kw):
        self._rec("describe_vpc_endpoints", kw)
        if "VpcEndpointIds" in kw:
            out = []
            for i in kw["VpcEndpointIds"]:
                if i in self.deleting:
                    if self.deleting[i] <= 0:
                        self.deleting.pop(i)
                        self.endpoints.pop(i, None)
                        continue
                    self.deleting[i] -= 1
                if i in self.endpoints:
                    out.append(self.endpoints[i])
            if not out:
                raise Exception("InvalidVpcEndpointId.NotFound")
            return {"VpcEndpoints": out}
        page, token = self._page(list(self.endpoints.values()), kw, "NextToken")
        return {"VpcEndpoints": page, **({"NextToken": token} if token else {})}

    def create_vpc_endpoint(self, **kw):
        self._rec("create_vpc_endpoint", kw)
        eid = self._nid("vpce")
        ep = {"VpcEndpointId": eid, "State": "pending", "VpcEndpointType": kw.get("VpcEndpointType"),
              "ResourceConfigurationArn": kw.get("ResourceConfigurationArn"), "VpcId": kw.get("VpcId")}
        self.endpoints[eid] = ep
        return {"VpcEndpoint": ep}

    def delete_vpc_endpoints(self, **kw):
        self._rec("delete_vpc_endpoints", kw)
        for i in kw["VpcEndpointIds"]:
            if self.polls_until_gone and i in self.endpoints:
                self.endpoints[i] = dict(self.endpoints[i], State="deleting")
                self.deleting[i] = self.polls_until_gone
            else:
                self.endpoints.pop(i, None)
        return {"Unsuccessful": []}

    def __getattr__(self, name):
        # Anything else is recorded so the tests can prove it was NOT called (routes).
        def _call(**kw):
            self._rec(name, kw)
            return {}
        return _call


class FakeLattice(Recorder):
    def __init__(self, account):
        super().__init__()
        self.who = f"lattice:{account}"
        self.account = account
        self.gateways = {}
        self.configs = {}
        self.create_status = "ACTIVE"
        self.polls_until_active = 0
        self.polls_until_gone = 0
        self._activating = {}    # gateway id -> polls left
        self._deleting = {}      # gateway id -> polls left
        self.endpoint_bound = lambda rcfg_arn: False
        self._n = 0

    def _nid(self, prefix):
        self._n += 1
        return f"{prefix}-{self._n:012d}"

    def list_resource_gateways(self, **kw):
        self._rec("list_resource_gateways", kw)
        page, token = self._page(list(self.gateways.values()), kw)
        return {"items": page, **({"nextToken": token} if token else {})}

    def create_resource_gateway(self, **kw):
        self._rec("create_resource_gateway", kw)
        if any(g["name"] == kw["name"] for g in self.gateways.values()):
            # Lattice names are unique in an account, whatever the other one's status.
            raise Exception("ConflictException: a resource gateway with this name already exists")
        gid = self._nid("rgw")
        item = {"id": gid, "arn": f"arn:aws:vpc-lattice:us-east-1:{self.account}:resourcegateway/{gid}",
                "name": kw["name"], "status": self.create_status, "vpcIdentifier": kw["vpcIdentifier"],
                "securityGroupIds": kw.get("securityGroupIds", [])}
        self.gateways[gid] = item
        if self.create_status != "ACTIVE":
            self._activating[gid] = self.polls_until_active
        return dict(item)

    def get_resource_gateway(self, **kw):
        self._rec("get_resource_gateway", kw)
        gid = kw["resourceGatewayIdentifier"]
        if gid in self._deleting:
            if self._deleting[gid] <= 0:
                self._deleting.pop(gid)
                self.gateways.pop(gid, None)
            else:
                self._deleting[gid] -= 1
        if gid not in self.gateways:
            raise Exception("ResourceNotFoundException: resource gateway not found")
        if gid in self._activating and self._activating[gid] is not None:
            if self._activating[gid] <= 0:
                self._activating.pop(gid)
                self.gateways[gid]["status"] = "ACTIVE"
            else:
                self._activating[gid] -= 1
        return dict(self.gateways[gid])

    def delete_resource_gateway(self, **kw):
        self._rec("delete_resource_gateway", kw)
        gid = kw["resourceGatewayIdentifier"]
        if gid not in self.gateways:
            raise Exception("ResourceNotFoundException: resource gateway not found")
        if any(c["resourceGatewayId"] == gid for c in self.configs.values()):
            raise Exception("ConflictException: the resource gateway has resource configurations")
        if self.polls_until_gone:
            self.gateways[gid]["status"] = "DELETE_IN_PROGRESS"
            self._deleting[gid] = self.polls_until_gone
        else:
            self.gateways.pop(gid, None)
        return {}

    def list_resource_configurations(self, **kw):
        self._rec("list_resource_configurations", kw)
        items = [c for c in self.configs.values()
                 if c["resourceGatewayId"] == kw.get("resourceGatewayIdentifier", c["resourceGatewayId"])]
        page, token = self._page(items, kw)
        return {"items": page, **({"nextToken": token} if token else {})}

    def create_resource_configuration(self, **kw):
        self._rec("create_resource_configuration", kw)
        cid = self._nid("rcfg")
        item = {"id": cid, "arn": f"arn:aws:vpc-lattice:us-east-1:{self.account}:resourceconfiguration/{cid}",
                "name": kw["name"], "status": "ACTIVE", "resourceGatewayId": kw["resourceGatewayIdentifier"]}
        self.configs[cid] = item
        return item

    def delete_resource_configuration(self, **kw):
        self._rec("delete_resource_configuration", kw)
        arn = kw["resourceConfigurationIdentifier"]
        if self.endpoint_bound(arn):
            raise Exception("ConflictException: the resource configuration has an endpoint association")
        for cid, c in list(self.configs.items()):
            if c["arn"] == arn or cid == arn:
                del self.configs[cid]
        return {}


class FakeRam(Recorder):
    """One RAM for both accounts: the share the database account creates is the
    invitation the deployment account sees, and its principal association is what the
    provider side reports.

    mode: "invite" (another organisation: a PENDING invitation, ASSOCIATING until it is
    accepted), "same_org" (no invitation; ASSOCIATED after `assoc_after` polls), "never"
    (no invitation, never associated). `invite_after`: invitation listings before the
    invitation becomes visible (a late invitation)."""

    def __init__(self):
        super().__init__()
        self.who = "ram"
        self.shares = {}
        self.invites = {}
        self.assoc = {}          # share arn -> {"status", "polls"}
        self.mode = "invite"
        self.invite_after = 0
        self.assoc_after = 1
        self._inv_polls = {}
        self._n = 0

    def get_resource_shares(self, **kw):
        self._rec("get_resource_shares", kw)
        items = [s for s in self.shares.values() if s["name"] == kw.get("name", s["name"])]
        page, token = self._page(items, kw)
        return {"resourceShares": page, **({"nextToken": token} if token else {})}

    def create_resource_share(self, **kw):
        self._rec("create_resource_share", kw)
        self._n += 1
        arn = f"arn:aws:ram:us-east-1:{DB_ACCOUNT}:resource-share/{self._n:08d}"
        share = {"resourceShareArn": arn, "name": kw["name"], "status": "ACTIVE",
                 "resourceArns": kw["resourceArns"], "principals": kw["principals"]}
        self.shares[arn] = share
        if self.mode == "invite":
            self.invites[arn] = {"resourceShareInvitationArn": arn.replace("resource-share", "resource-share-invitation"),
                                 "resourceShareArn": arn, "status": "PENDING"}
        self.assoc[arn] = {"status": "ASSOCIATING", "polls": 0}
        return {"resourceShare": share}

    def delete_resource_share(self, **kw):
        self._rec("delete_resource_share", kw)
        self.shares.pop(kw["resourceShareArn"], None)
        return {}

    def get_resource_share_invitations(self, **kw):
        self._rec("get_resource_share_invitations", kw)
        out = []
        for a in kw["resourceShareArns"]:
            self._inv_polls[a] = self._inv_polls.get(a, 0) + 1
            if a in self.invites and self._inv_polls[a] > self.invite_after:
                out.append(self.invites[a])
        return {"resourceShareInvitations": out}

    def accept_resource_share_invitation(self, **kw):
        self._rec("accept_resource_share_invitation", kw)
        for arn, inv in self.invites.items():
            if inv["resourceShareInvitationArn"] == kw["resourceShareInvitationArn"]:
                inv["status"] = "ACCEPTED"
                self.assoc[arn]["status"] = "ASSOCIATED"
        return {}

    def get_resource_share_associations(self, **kw):
        self._rec("get_resource_share_associations", kw)
        out = []
        for a in kw["resourceShareArns"]:
            st = self.assoc.get(a)
            if not st:
                continue
            st["polls"] += 1
            if self.mode == "same_org" and st["polls"] >= self.assoc_after:
                st["status"] = "ASSOCIATED"
            out.append({"resourceShareArn": a, "associatedEntity": AGENT_ACCOUNT,
                        "associationType": "PRINCIPAL", "status": st["status"]})
        page, token = self._page(out, kw)
        return {"resourceShareAssociations": page, **({"nextToken": token} if token else {})}


class FakeSts:
    def get_caller_identity(self):
        return {"Account": AGENT_ACCOUNT}


class FakeSession:
    """A boto3 session (or the boto3 module) for one account."""

    def __init__(self, ec2, lattice, ram):
        self._clients = {"ec2": ec2, "vpc-lattice": lattice, "ram": ram, "sts": FakeSts()}

    def client(self, service, **kw):
        return self._clients[service]


class FakeTable:
    """Just enough DynamoDB for NetworkRegistry: get/put/delete, SET updates, and the
    vpc-index query find_by_vpc uses."""

    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        item = self.items.get(Key["connection_id"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.items[Item["connection_id"]] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(Key["connection_id"], None)

    def query(self, **kw):
        if kw.get("IndexName") == "vpc-index":
            # Key("db_vpc_id").eq(x) -> boto3 condition; read the value back out of it.
            vpc = kw["KeyConditionExpression"].get_expression()["values"][1]
            return {"Items": [dict(i) for i in self.items.values() if i.get("db_vpc_id") == vpc]}
        return {"Items": []}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues):
        item = self.items[Key["connection_id"]]
        assert UpdateExpression.startswith("SET ")
        for part in UpdateExpression[4:].split(", "):
            name, value = [x.strip() for x in part.split("=")]
            item[ExpressionAttributeNames[name]] = ExpressionAttributeValues[value]


# ── harness ──────────────────────────────────────────────────────────────────────

def build(conflict=True, mode=None):
    """A configurator wired to fakes. `conflict`: another VPC with the DB VPC's CIDR is
    already peered and routed, so the same-region path must not peer."""
    os.environ["AGENT_VPC_ID"] = AGENT_VPC
    os.environ["AGENT_VPC_CIDR"] = AGENT_CIDR
    os.environ["AGENT_PRIVATE_ROUTE_TABLE_ID"] = AGENT_RT
    os.environ["SUBNET_IDS"] = AGENT_SUBNETS
    os.environ["AWS_REGION"] = "us-east-1"
    os.environ["RESOURCE_PREFIX"] = "dbops"
    if mode is None:
        os.environ.pop("SAME_REGION_CONNECTIVITY", None)
    else:
        os.environ["SAME_REGION_CONNECTIVITY"] = mode

    peerings, routes = [], []
    if conflict:
        peerings = [{"VpcPeeringConnectionId": "pcx-other", "Status": {"Code": "active"},
                     "RequesterVpcInfo": {"VpcId": AGENT_VPC, "CidrBlock": AGENT_CIDR},
                     "AccepterVpcInfo": {"VpcId": OTHER_VPC, "CidrBlock": DB_CIDR}}]
        routes = [{"DestinationCidrBlock": DB_CIDR, "VpcPeeringConnectionId": "pcx-other"}]

    TIMELINE.clear()
    ram = FakeRam()
    agent = FakeSession(FakeEc2(AGENT_ACCOUNT, peerings, routes), FakeLattice(AGENT_ACCOUNT), ram)
    db = FakeSession(FakeEc2(DB_ACCOUNT), FakeLattice(DB_ACCOUNT), ram)
    db._clients["ec2"].sgs[DB_SG] = {"name": "db-sg", "vpc": DB_VPC, "ingress": []}
    a_ec2, d_ec2, d_lat = agent._clients["ec2"], db._clients["ec2"], db._clients["vpc-lattice"]
    # AWS's dependency rules: an endpoint pins its configuration; a gateway pins its group.
    d_lat.endpoint_bound = lambda arn: any(ep["ResourceConfigurationArn"] == arn for ep in a_ec2.endpoints.values())
    d_ec2.sg_in_use = lambda gid: any(gid in g.get("securityGroupIds", []) for g in d_lat.gateways.values())

    reg_mod = importlib.import_module("tools.aws.network_registry")
    table = FakeTable()
    reg_mod.network_registry._table = table

    nc_mod = importlib.import_module("tools.aws.network_client")
    nc_mod.boto3 = agent                       # the module's own boto3 is the agent account
    nc_mod.time = FakeTime()                   # bounded waits run on a fake clock
    nc = nc_mod.NetworkConfigurator()
    nc._ec2 = a_ec2
    return nc, agent, db, table, reg_mod.network_registry


def configure(nc, db, endpoint, arn, instance_id, port=5432):
    return nc.configure_network(
        db_vpc_id=DB_VPC, db_security_group_ids=DB_SG, db_port=port, db_account_id=DB_ACCOUNT,
        db_region="us-east-1", cross_account_session=db, db_endpoint=endpoint,
        db_subnet_ids=DB_SUBNETS, instance_id=instance_id, db_arn=arn,
    )


def teardown(nc, db, res, endpoint, instance_id):
    return nc.cleanup_lattice(vpce_id=res["vpce_id"], db_vpc_id=DB_VPC, cross_account_session=db,
                              db_region="us-east-1", instance_id=instance_id, db_endpoint=endpoint,
                              resource_configuration_arn=res["resource_configuration_arn"],
                              resource_share_arn=res["resource_share_arn"])


def rows(table, method):
    return [r for r in table.items.values() if r["method"] == method]


def order(*apis):
    """Index of the first occurrence of each api in the cross-fake timeline."""
    names = [n for _, n in TIMELINE]
    return [names.index(a) if a in names else -1 for a in apis]


# ── 1. a same-region address clash builds the Lattice path, routes nothing ───────
print("══ same-region CIDR clash: resource gateway, configuration, share, endpoint; no route ══")
nc, agent, db, table, reg = build(conflict=True)
res = configure(nc, db, EP1, ARN1, "inst-1")
a_ec2, d_ec2, d_lat, ram = agent._clients["ec2"], db._clients["ec2"], db._clients["vpc-lattice"], agent._clients["ram"]

check(res["status"] == "configured" and res["method"] == "lattice", f"result is a configured lattice connection ({res.get('error', '')})")
check("create_vpc_peering_connection" not in a_ec2.names(), "no peering is created for the clashing VPC")
check("create_route" not in a_ec2.names() and "create_route" not in d_ec2.names(),
      "no route is added in either VPC: the address ranges never meet")
check("create_load_balancer" not in str(d_ec2.names()), "no load balancer anywhere")

gws = d_lat.args("create_resource_gateway")
check(len(gws) == 1 and gws[0]["vpcIdentifier"] == DB_VPC and gws[0]["subnetIds"] == DB_SUBNETS.split(","),
      "one resource gateway, in the database VPC, on the database's subnets")
gw_sg = [gid for gid, sg in d_ec2.sgs.items() if sg["name"].startswith("dbops-rgw-")]
check(len(gw_sg) == 1 and gws[0]["securityGroupIds"] == gw_sg
      and d_ec2.sgs[gw_sg[0]]["tags"].get("ManagedBy") == "dbops-agent",
      "the gateway runs with its own ManagedBy-tagged security group")
check(gws[0]["tags"].get("ManagedBy") == "dbops-agent", "the gateway is tagged ManagedBy on create")

db_ingress = d_ec2.sgs[DB_SG]["ingress"]
check(len(db_ingress) == 1 and db_ingress[0]["FromPort"] == 5432
      and db_ingress[0].get("UserIdGroupPairs", [{}])[0].get("GroupId") == gw_sg[0]
      and not db_ingress[0].get("IpRanges"),
      "the database's security group admits the gateway's group on 5432, no address range of ours")

cfgs = d_lat.args("create_resource_configuration")
check(len(cfgs) == 1 and cfgs[0]["type"] == "ARN"
      and cfgs[0]["resourceConfigurationDefinition"] == {"arnResource": {"arn": ARN1}}
      and cfgs[0]["resourceGatewayIdentifier"] in d_lat.gateways,
      "one ARN resource configuration naming the database's RDS ARN, on that gateway")
check(cfgs[0]["tags"].get("ManagedBy") == "dbops-agent", "the configuration is tagged ManagedBy on create")

shares = ram.args("create_resource_share")
rcfg_arn = list(d_lat.configs.values())[0]["arn"]
check(len(shares) == 1 and shares[0]["resourceArns"] == [rcfg_arn] and shares[0]["principals"] == [AGENT_ACCOUNT],
      "the configuration alone is shared, to the deployment account alone")
check(shares[0]["allowExternalPrincipals"] is True, "the share admits an account outside the organisation")
check(any(t["key"] == "ManagedBy" and t["value"] == "dbops-agent" for t in shares[0]["tags"]),
      "the share is tagged ManagedBy on create (RAM has no tag-on-create key, so this is the only tag path)")
check(ram.args("accept_resource_share_invitation") and
      list(ram.invites.values())[0]["status"] == "ACCEPTED", "the deployment account accepts the invitation")

eps = a_ec2.args("create_vpc_endpoint")
check(len(eps) == 1 and eps[0]["VpcEndpointType"] == "Resource" and eps[0]["VpcId"] == AGENT_VPC
      and eps[0]["ResourceConfigurationArn"] == rcfg_arn and eps[0]["SubnetIds"] == AGENT_SUBNETS.split(","),
      "one resource VPC endpoint in the agent VPC, bound to the shared configuration")
check(eps[0]["PrivateDnsEnabled"] is True,
      "private DNS is on: the RDS hostname resolves to the endpoint, so TLS stays verify-full")
check(res["vpce_id"] in a_ec2.endpoints and res["resource_configuration_arn"] == rcfg_arn
      and res["resource_share_arn"] == list(ram.shares)[0],
      "the result carries the ids teardown needs: endpoint, configuration ARN, share ARN")

lt, gwrows = rows(table, "lattice"), rows(table, "lattice-gateway")
check(len(lt) == 1 and lt[0]["ref_instances"] == ["inst-1"] and lt[0]["vpce_id"] == res["vpce_id"],
      "the registry holds the database's own endpoint row, referenced by this instance")
check(len(gwrows) == 1 and gwrows[0]["ref_count"] == 1 and gwrows[0]["resource_gateway_id"] in d_lat.gateways
      and gwrows[0]["gateway_security_group_id"] == gw_sg[0],
      "and the VPC's gateway row, reference-counted, with the gateway and its group")

# ── 2. a second database in the same VPC shares the gateway, gets its own endpoint ──
print("")
print("══ a second database in the same VPC reuses the gateway ══")
res2 = configure(nc, db, EP2, ARN2, "inst-2")
check(res2["status"] == "configured" and res2["method"] == "lattice", "second database connects over Lattice too")
check(len(d_lat.args("create_resource_gateway")) == 1, "no second resource gateway: the VPC's one is reused")
check(len(d_lat.args("create_resource_configuration")) == 2 and len(ram.args("create_resource_share")) == 2
      and len(a_ec2.args("create_vpc_endpoint")) == 2,
      "but its own configuration, share and endpoint")
check(res2["vpce_id"] != res["vpce_id"], "the two databases have distinct endpoints")
gwrows = rows(table, "lattice-gateway")
check(len(gwrows) == 1 and gwrows[0]["ref_count"] == 2 and sorted(gwrows[0]["ref_instances"]) == ["inst-1", "inst-2"],
      "the gateway row now counts both databases")
check(len(rows(table, "lattice")) == 2, "two endpoint rows")

# ── 3. re-running Configure for a connected database reuses, creates nothing ──────
print("")
print("══ Configure again on a connected database: reuse, no new resources ══")
before = (len(a_ec2.args("create_vpc_endpoint")), len(d_lat.args("create_resource_configuration")), len(ram.args("create_resource_share")))
res1b = configure(nc, db, EP1, ARN1, "inst-1")
after = (len(a_ec2.args("create_vpc_endpoint")), len(d_lat.args("create_resource_configuration")), len(ram.args("create_resource_share")))
check(res1b["method"] == "lattice" and res1b["vpce_id"] == res["vpce_id"], "the same endpoint is returned")
check(before == after, "nothing new is created")

# ── 4. teardown: the first database's own pieces go; the gateway stays for the second ──
print("")
print("══ deleting one database removes its endpoint, share and configuration; the gateway stays ══")
out = teardown(nc, db, res, EP1, "inst-1")
check(res["vpce_id"] not in a_ec2.endpoints and res2["vpce_id"] in a_ec2.endpoints,
      "only the deleted database's endpoint is removed")
check(res["resource_share_arn"] not in ram.shares and res2["resource_share_arn"] in ram.shares,
      "only its share is removed")
check(res["resource_configuration_arn"] not in [c["arn"] for c in d_lat.configs.values()]
      and res2["resource_configuration_arn"] in [c["arn"] for c in d_lat.configs.values()],
      "only its resource configuration is removed")
check(not d_lat.args("delete_resource_gateway") and len(d_lat.gateways) == 1,
      "the gateway is kept: the other database in the VPC still uses it")
gwrows = rows(table, "lattice-gateway")
check(len(rows(table, "lattice")) == 1 and gwrows[0]["ref_count"] == 1 and gwrows[0]["ref_instances"] == ["inst-2"],
      "the registry drops the endpoint row and decrements the gateway")
check(any("kept" in s for s in out["steps"]), "the steps say why the gateway stayed")
check(out["complete"] is True, "and the teardown reports itself complete")

print("")
print("══ deleting the last database in the VPC removes the gateway and its group ══")
out = teardown(nc, db, res2, EP2, "inst-2")
check(out["complete"] is True, "the last teardown reports itself complete")
check(not a_ec2.endpoints and not ram.shares and not d_lat.configs, "no endpoints, shares or configurations remain")
check(not d_lat.gateways, "the resource gateway is deleted")
check(not any(sg["name"].startswith("dbops-rgw-") for sg in d_ec2.sgs.values()), "and its security group")
check(DB_SG in d_ec2.sgs, "the database's own security group is left alone")
check(not table.items, "the registry is empty")

# ── 4b. teardown under failure: leftovers stay tracked until confirmed gone ───────
print("")
print("══ teardown waits for the endpoint to be gone before deleting its configuration ══")
nc, agent, db, table, reg = build(conflict=True)
a_ec2, d_ec2, d_lat, ram = agent._clients["ec2"], db._clients["ec2"], db._clients["vpc-lattice"], agent._clients["ram"]
r1 = configure(nc, db, EP1, ARN1, "inst-1")
a_ec2.polls_until_gone = 3
TIMELINE.clear()
out = teardown(nc, db, r1, EP1, "inst-1")
polls = [n for w, n in TIMELINE if n == "describe_vpc_endpoints"]
i_del_ep, i_del_cfg = order("delete_vpc_endpoints", "delete_resource_configuration")
last_poll = max(i for i, (_, n) in enumerate(TIMELINE) if n == "describe_vpc_endpoints")
check(out["complete"] is True and len(polls) >= 3, f"the endpoint delete is polled until gone ({len(polls)} polls)")
check(i_del_ep < last_poll < i_del_cfg,
      "the configuration is deleted only after the last endpoint poll saw it gone")
check(not d_lat.configs and not ram.shares and not a_ec2.endpoints and not table.items,
      "everything is gone and the registry is empty")

print("")
print("══ a refused configuration delete keeps the row; a later teardown finishes ══")
nc, agent, db, table, reg = build(conflict=True)
a_ec2, d_ec2, d_lat, ram = agent._clients["ec2"], db._clients["ec2"], db._clients["vpc-lattice"], agent._clients["ram"]
r1 = configure(nc, db, EP1, ARN1, "inst-1")
d_lat.fail_once["delete_resource_configuration"] = "ConflictException: resource configuration has dependent associations"
out = teardown(nc, db, r1, EP1, "inst-1")
lt = rows(table, "lattice")
check(out["complete"] is False, "the result says the teardown is incomplete")
check(r1["vpce_id"] not in a_ec2.endpoints, "the endpoint is gone")
check(len(lt) == 1 and lt[0]["status"] == "delete_failed" and lt[0]["failed_step"] == "resource_configuration"
      and "ConflictException" in lt[0]["failure_detail"],
      "the database's row is kept as delete_failed at resource_configuration, with the error")
check(lt and lt[0]["resource_configuration_arn"] == r1["resource_configuration_arn"]
      and lt[0]["resource_share_arn"] == r1["resource_share_arn"] and lt[0]["vpce_id"] == "",
      "the row still names the configuration and the share (not orphaned untracked), not the deleted endpoint")
check(r1["resource_share_arn"] in ram.shares, "the share was not deleted ahead of its configuration")
gwr = rows(table, "lattice-gateway")
check(len(gwr) == 1 and gwr[0]["status"] == "delete_failed" and d_lat.gateways,
      "the gateway is kept, tracked, while a configuration may still hang on it")
check(any("stopped at resource configuration" in s for s in out["steps"]), "the steps name the failing step")
out2 = teardown(nc, db, r1, EP1, "inst-1")
check(out2["complete"] is True, "the second teardown completes")
check(not d_lat.configs and not ram.shares and not d_lat.gateways
      and not any(sg["name"].startswith("dbops-rgw-") for sg in d_ec2.sgs.values()),
      "configuration, share, gateway and its group are all gone")
check(not table.items, "and only now are the registry rows deleted")

print("")
print("══ the gateway is waited for before its security group is deleted ══")
nc, agent, db, table, reg = build(conflict=True)
a_ec2, d_ec2, d_lat, ram = agent._clients["ec2"], db._clients["ec2"], db._clients["vpc-lattice"], agent._clients["ram"]
r1 = configure(nc, db, EP1, ARN1, "inst-1")
d_lat.polls_until_gone = 3
TIMELINE.clear()
out = teardown(nc, db, r1, EP1, "inst-1")
gw_polls = [i for i, (_, n) in enumerate(TIMELINE) if n == "get_resource_gateway"]
i_sg = order("delete_security_group")[0]
check(out["complete"] is True and len(gw_polls) >= 3, f"the gateway delete is polled until gone ({len(gw_polls)} polls)")
check(i_sg > max(gw_polls), "the security group is deleted after the gateway is confirmed gone")
check(not d_lat.gateways and not table.items, "gateway and rows gone")

print("")
print("══ a refused gateway delete keeps the gateway row; Configure re-adopts the gateway ══")
nc, agent, db, table, reg = build(conflict=True)
a_ec2, d_ec2, d_lat, ram = agent._clients["ec2"], db._clients["ec2"], db._clients["vpc-lattice"], agent._clients["ram"]
r1 = configure(nc, db, EP1, ARN1, "inst-1")
gw_id = list(d_lat.gateways)[0]
d_lat.fail_once["delete_resource_gateway"] = "ValidationException: cannot delete the resource gateway now"
out = teardown(nc, db, r1, EP1, "inst-1")
gwr = rows(table, "lattice-gateway")
check(out["complete"] is False and not rows(table, "lattice"), "the database's own row is gone; the result is incomplete")
check(len(gwr) == 1 and gwr[0]["status"] == "delete_failed" and gwr[0]["failed_step"] == "resource_gateway"
      and gwr[0]["ref_count"] == 0, "the gateway row is kept as delete_failed at resource_gateway with no references")
check(gw_id in d_lat.gateways and any(sg["name"].startswith("dbops-rgw-") for sg in d_ec2.sgs.values()),
      "the gateway's security group is NOT deleted while the gateway exists")
r2 = configure(nc, db, EP2, ARN2, "inst-2")
gwr = rows(table, "lattice-gateway")
check(r2["status"] == "configured" and len(d_lat.args("create_resource_gateway")) == 1,
      "a later Configure in the same VPC connects and creates no second gateway")
check(gwr[0]["status"] == "active" and gwr[0]["resource_gateway_id"] == gw_id and gwr[0]["ref_instances"] == ["inst-2"]
      and gwr[0]["failed_step"] == "", "the still-existing gateway is re-adopted: active, referenced by the new database")

print("")
print("══ an endpoint that never finishes deleting keeps the row at the endpoint step ══")
nc, agent, db, table, reg = build(conflict=True)
a_ec2, d_ec2, d_lat, ram = agent._clients["ec2"], db._clients["ec2"], db._clients["vpc-lattice"], agent._clients["ram"]
r1 = configure(nc, db, EP1, ARN1, "inst-1")
a_ec2.polls_until_gone = 10 ** 6
out = teardown(nc, db, r1, EP1, "inst-1")
lt = rows(table, "lattice")
check(out["complete"] is False and len(lt) == 1 and lt[0]["status"] == "delete_failed"
      and lt[0]["failed_step"] == "endpoint" and "300 s" in lt[0]["failure_detail"],
      "the row is kept as delete_failed at endpoint, with the timeout")
check(not d_lat.args("delete_resource_configuration") and d_lat.configs,
      "the configuration is not touched while the endpoint may still be bound")
res_again = configure(nc, db, EP1, ARN1, "inst-1")
check(res_again["status"] == "failed" and "incomplete" in res_again["error"]
      and len(a_ec2.args("create_vpc_endpoint")) == 1,
      "Configure retries the teardown first and refuses to build over a still-incomplete row")

# ── 4c. the shared endpoint security group admits every database port ─────────────
print("")
print("══ the shared resource-endpoint security group admits each database's port ══")
nc, agent, db, table, reg = build(conflict=True)
a_ec2 = agent._clients["ec2"]
configure(nc, db, EP1, ARN1, "inst-1", port=5432)
configure(nc, db, EP2, ARN2, "inst-2", port=5433)
vpce_sg = [sg for sg in a_ec2.sgs.values() if sg["name"].endswith("vpce-sg")]
ports = sorted(p["FromPort"] for p in vpce_sg[0]["ingress"]) if vpce_sg else []
check(len(vpce_sg) == 1 and ports == [5432, 5433], f"one shared group with both port rules ({ports})")
check(all(p["IpRanges"][0]["CidrIp"] == AGENT_CIDR for p in vpce_sg[0]["ingress"]), "both admit the agent VPC only")
before = len(vpce_sg[0]["ingress"])
nc._get_or_create_vpce_sg(5432)
check(len(vpce_sg[0]["ingress"]) == before, "re-asserting an existing port tolerates the duplicate and adds nothing")

# ── 4d. the resource gateway must be ACTIVE before anything is attached ────────────
print("")
print("══ Configure waits for the resource gateway to be ACTIVE ══")
nc, agent, db, table, reg = build(conflict=True)
d_lat = db._clients["vpc-lattice"]
d_lat.create_status, d_lat.polls_until_active = "CREATE_IN_PROGRESS", 3
TIMELINE.clear()
res = configure(nc, db, EP1, ARN1, "inst-1")
gets = [i for i, (_, n) in enumerate(TIMELINE) if n == "get_resource_gateway"]
i_cfg = order("create_resource_configuration")[0]
check(res["status"] == "configured", f"configured once the gateway turns ACTIVE ({res.get('error', '')})")
check(len(gets) == 4 and i_cfg > max(gets), f"the configuration is created only after the gateway is ACTIVE ({len(gets)} polls)")

nc, agent, db, table, reg = build(conflict=True)
d_lat = db._clients["vpc-lattice"]
d_lat.create_status, d_lat.polls_until_active = "CREATE_IN_PROGRESS", None
res = configure(nc, db, EP1, ARN1, "inst-1")
check(res["status"] == "failed" and "did not become ACTIVE within 300 s" in res["error"]
      and "CREATE_IN_PROGRESS" in res["error"], f"a stuck gateway fails with the budget and last status: {res.get('error')}")
check(not d_lat.args("create_resource_configuration") and not table.items, "no configuration is created, nothing registered")

nc, agent, db, table, reg = build(conflict=True)
d_lat = db._clients["vpc-lattice"]
name = nc._lattice_name("rgw", DB_VPC)
d_lat.gateways["rgw-broken"] = {"id": "rgw-broken", "arn": "arn:aws:vpc-lattice:us-east-1:2:resourcegateway/rgw-broken",
                                "name": name, "status": "CREATE_FAILED", "vpcIdentifier": DB_VPC}
res = configure(nc, db, EP1, ARN1, "inst-1")
check(res["status"] == "failed" and res["error"] ==
      "Resource gateway rgw-broken is CREATE_FAILED; delete it in the database account and retry",
      f"a reused gateway in CREATE_FAILED fails with a clear message: {res.get('error')}")
check(not d_lat.args("create_resource_configuration"), "and nothing is attached to it")

for status in ("DELETE_IN_PROGRESS", "DELETE_FAILED"):
    nc, agent, db, table, reg = build(conflict=True)
    d_lat = db._clients["vpc-lattice"]
    d_lat.gateways["rgw-going"] = {"id": "rgw-going", "arn": "arn:aws:vpc-lattice:us-east-1:2:resourcegateway/rgw-going",
                                   "name": nc._lattice_name("rgw", DB_VPC), "status": status, "vpcIdentifier": DB_VPC}
    res = configure(nc, db, EP1, ARN1, "inst-1")
    check(res["status"] == "failed" and res["error"] ==
          f"Resource gateway rgw-going is {status}; delete it in the database account and retry",
          f"a same-named gateway in {status} fails with the clear message, not AWS's conflict: {res.get('error')}")
    check(not d_lat.args("create_resource_gateway") and not d_lat.args("create_resource_configuration"),
          f"no same-named gateway is created beside the {status} one, nothing attached")

# ── 4e. the share must be usable by the deployment account before the endpoint ─────
print("")
print("══ Configure confirms the share before creating the endpoint ══")
nc, agent, db, table, reg = build(conflict=True)
ram, a_ec2 = agent._clients["ram"], agent._clients["ec2"]
ram.invite_after = 2      # the invitation shows up on the 3rd listing
TIMELINE.clear()
res = configure(nc, db, EP1, ARN1, "inst-1")
i_acc, i_ep = order("accept_resource_share_invitation", "create_vpc_endpoint")
inv_polls = len(ram.args("get_resource_share_invitations"))
assocs = ram.args("get_resource_share_associations")
check(res["status"] == "configured" and inv_polls >= 3, f"a late invitation is waited for and accepted ({inv_polls} listings)")
check(0 <= i_acc < i_ep, "the endpoint is created only after the invitation is accepted")
check(assocs and all(a["associationType"] == "PRINCIPAL" for a in assocs),
      "the provider side is asked for the PRINCIPAL association")
check(any("Accepted the resource share invitation" in s for s in res["steps"]), "the steps say an invitation was accepted")

nc, agent, db, table, reg = build(conflict=True)
ram = agent._clients["ram"]
ram.mode, ram.assoc_after = "same_org", 2
res = configure(nc, db, EP1, ARN1, "inst-1")
check(res["status"] == "configured" and not ram.args("accept_resource_share_invitation"),
      "same organisation: no invitation, nothing accepted, still connected")
check(any("No invitation needed (same organisation)" in s for s in res["steps"]), "the steps say no invitation was needed")

nc, agent, db, table, reg = build(conflict=True)
ram, a_ec2 = agent._clients["ram"], agent._clients["ec2"]
ram.mode = "never"
res = configure(nc, db, EP1, ARN1, "inst-1")
check(res["status"] == "failed" and res["error"].startswith(
      f"The resource share was not accepted by account {AGENT_ACCOUNT} within 120 s"),
      f"never associated: a clear timeout ({res.get('error')})")
check(not a_ec2.args("create_vpc_endpoint"), "and no endpoint is created")

# ── 4f. every list call in the Lattice path follows nextToken ─────────────────────
print("")
print("══ re-running Configure finds every existing resource on page 2 ══")
nc, agent, db, table, reg = build(conflict=True)
a_ec2, d_lat, ram = agent._clients["ec2"], db._clients["vpc-lattice"], agent._clients["ram"]
res = configure(nc, db, EP1, ARN1, "inst-1")
# Filler ahead of ours, so ours is on page 2 of every listing.
gw_real = dict(d_lat.gateways)
d_lat.gateways = {"rgw-filler": {"id": "rgw-filler", "arn": "x", "name": "someone-else", "status": "ACTIVE"}, **gw_real}
gw_id = list(gw_real)[0]
cfg_real = dict(d_lat.configs)
d_lat.configs = {"rcfg-filler": {"id": "rcfg-filler", "arn": "y", "name": "other", "status": "ACTIVE",
                                 "resourceGatewayId": gw_id}, **cfg_real}
sh_real = dict(ram.shares)
ram.shares = {"filler": {"resourceShareArn": "z", "name": list(sh_real.values())[0]["name"], "status": "DELETED"}, **sh_real}
ep_real = dict(a_ec2.endpoints)
a_ec2.endpoints = {"vpce-filler": {"VpcEndpointId": "vpce-filler", "State": "available",
                                   "ResourceConfigurationArn": "other"}, **ep_real}
for f in (a_ec2, d_lat, ram):
    f.page_size = 1
table.items.clear()          # force the full build path, not the registry shortcut
before = {k: len(v) for k, v in {"gw": d_lat.args("create_resource_gateway"),
                                  "cfg": d_lat.args("create_resource_configuration"),
                                  "share": ram.args("create_resource_share"),
                                  "ep": a_ec2.args("create_vpc_endpoint")}.items()}
res2 = configure(nc, db, EP1, ARN1, "inst-1")
after = {"gw": len(d_lat.args("create_resource_gateway")), "cfg": len(d_lat.args("create_resource_configuration")),
         "share": len(ram.args("create_resource_share")), "ep": len(a_ec2.args("create_vpc_endpoint"))}
check(res2["status"] == "configured" and before == after,
      f"gateway, configuration, share and endpoint on page 2 are all reused ({before} -> {after})")
check(res2["vpce_id"] == res["vpce_id"], "the same endpoint is returned")
check(any(kw.get("nextToken") for kw in d_lat.args("list_resource_gateways"))
      and any(kw.get("nextToken") for kw in d_lat.args("list_resource_configurations"))
      and any(kw.get("nextToken") for kw in ram.args("get_resource_shares"))
      and any(kw.get("NextToken") for kw in a_ec2.args("describe_vpc_endpoints")),
      "every listing was asked for its second page")

# ── 5. the deployment-wide switch ────────────────────────────────────────────────
print("")
print("══ SAME_REGION_CONNECTIVITY decides when Lattice is used ══")


def route_taken(mode, conflict, cross_region):
    """Which private path configure_network hands off to, for a fresh configurator."""
    nc, agent, db, table, _ = build(conflict=conflict, mode=mode)
    taken = []
    nc._configure_lattice = lambda **kw: taken.append("lattice") or {"status": "configured", "method": "lattice", "steps": []}
    nc._configure_peering = lambda **kw: taken.append("peering") or {"status": "configured", "method": "peering", "steps": []}
    nc.configure_network(db_vpc_id=DB_VPC, db_security_group_ids=DB_SG, db_port=5432, db_account_id=DB_ACCOUNT,
                         db_region="eu-west-1" if cross_region else "us-east-1", cross_account_session=db,
                         db_endpoint=EP1, db_subnet_ids=DB_SUBNETS, instance_id="inst-x", db_arn=ARN1)
    return taken


check(route_taken(None, conflict=False, cross_region=False) == ["peering"], "default, no clash, same region: peering")
check(route_taken(None, conflict=True, cross_region=False) == ["lattice"], "default, clash, same region: Lattice")
check(route_taken(None, conflict=True, cross_region=True) == ["peering"], "default, clash, cross region: peering (host route)")
check(route_taken("lattice", conflict=False, cross_region=False) == ["lattice"], "lattice mode, no clash, same region: Lattice")
check(route_taken("lattice", conflict=True, cross_region=False) == ["lattice"], "lattice mode, clash, same region: Lattice")
check(route_taken("lattice", conflict=False, cross_region=True) == ["peering"], "lattice mode, cross region: still peering")
check(route_taken("LATTICE", conflict=False, cross_region=False) == ["lattice"], "the value is case-insensitive")
check(route_taken("nonsense", conflict=False, cross_region=False) == ["peering"], "an unrecognised value falls back to peering-first")

# ── 6. the template carries the switch end to end ─────────────────────────────────
print("")
print("══ the switch is a template parameter wired to the task environment ══")
ROOT = BACKEND.parent
master = (ROOT / "infrastructure/master.yaml").read_text(encoding="utf-8")
api = (ROOT / "infrastructure/api.yaml").read_text(encoding="utf-8")
param = master.split("\n  SameRegionConnectivity:\n    Type: String", 1)
check(len(param) == 2 and "AllowedValues: ['peering-first', 'lattice']" in param[1][:400]
      and "Default: 'peering-first'" in param[1][:400],
      "master.yaml declares SameRegionConnectivity with the two values, defaulting to peering-first")
check("SameRegionConnectivity: !Ref SameRegionConnectivity" in master, "master.yaml passes it to the API stack")
check("- Name: SAME_REGION_CONNECTIVITY\n              Value: !Ref SameRegionConnectivity" in api,
      "api.yaml puts it in the task environment as SAME_REGION_CONNECTIVITY")
check("DbPortRange: !Ref DbPortRange" in master and master.count("DbPortRange: !Ref DbPortRange") == 2,
      "master.yaml passes DbPortRange to the VPC stack and the API stack")
check("- Name: DB_PORT_RANGE\n              Value: !Ref DbPortRange" in api,
      "api.yaml puts it in the task environment as DB_PORT_RANGE (Configure's port warning)")
for action in ("vpc-lattice:CreateResourceGateway", "vpc-lattice:CreateResourceConfiguration",
               "ram:CreateResourceShare", "ram:AcceptResourceShareInvitation", "ram:GetResourceShareAssociations",
               "vpc-lattice:GetResourceGateway", "ec2:CreateVpcEndpoint",
               "ec2:CreateNetworkInterface"):
    check(action in api, f"the task role may {action}")
for gone in ("elasticloadbalancing:", "CreateVpcEndpointServiceConfiguration", "privatelink"):
    check(gone not in api.lower() if gone == "privatelink" else gone not in api, f"the task role no longer mentions {gone}")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
