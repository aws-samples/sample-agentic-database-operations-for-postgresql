#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Guard: every path that connects to a customer database picks the host the same way.

Four components open connections to a monitored database - the agent (pg_client),
the batch scan (two call sites), the Tier 1 collector, and the connectivity check
Configure runs. Each resolved the host independently, and they had drifted into
three different orders. With a secret whose `host` named a different database, the
agent and Tier 1 connected to the host in the SECRET while one batch path connected
to the instance's own endpoint: one instance record, two databases, no error.

The rule is duplicated at each site on purpose. batch/ and the Tier 1 zip bundle a
curated subset of tools/, so a shared module would mean editing build-tier1.sh, the
Makefile staging and the distribution pipeline in step. This file is the price of
that duplication: it fails when a copy drifts.

One exception, applied identically everywhere: an RDS read replica (the record's
`tags.replica_of`, written from RDS by discovery and topology refresh, never from user
input) also accepts a secret whose host is its replication source's endpoint
(`tags.replica_source_endpoint`), because the replica's read-only user is the source's.
The connection still dials the replica's own endpoint.

The port follows the record too: the record's port (from RDS) wins, the secret's port is
only a fallback, so a secret with a host but no port no longer sends a connection to 5432.

Run:  python3 tests/test_host_precedence.py
"""
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
passed = failed = 0


def check(cond: bool, label: str) -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}")


# Every file that resolves a target-database host, and how many places it does so.
SITES = {
    "backend/tools/aws/pg_client.py": 1,   # the agent's query path
    "batch/run_scan.py": 3,                # shell script + structured extract + reader scan
    "backend/tier1_collector.py": 1,       # the 5-minute metrics collector
    "backend/app.py": 1,                   # Configure's connectivity check
}

ANCHOR = "# HOST PRECEDENCE (identical in pg_client, run_scan, tier1_collector"
REFUSAL = "is not this instance's endpoint"

print("== the precedence rule is stated at every resolution site ==")
for rel, expected in SITES.items():
    text = (REPO / rel).read_text(encoding="utf-8")
    check(text.count(ANCHOR) == expected,
          f"{rel}: the rule is stated {expected}x (found {text.count(ANCHOR)})")
    check(REFUSAL in text,
          f"{rel}: refuses a secret host that contradicts the endpoint")

print("")
print("== every site applies the read-replica exception the same way ==")
for rel in SITES:
    text = (REPO / rel).read_text(encoding="utf-8")
    check(text.count('replica_source_endpoint') >= SITES[rel] and 'replica_of' in text,
          f"{rel}: accepts the replica's source endpoint only with replica_of set")

print("")
print("== the record's port wins, the secret's port is the fallback ==")
PORT_LINES = {
    "backend/tools/aws/pg_client.py": ['port = int(record.get("port") or creds.get("port") or 5432)'],
    "batch/run_scan.py": ['port = instance_info.get("port") or creds.get("port", 5432)',
                          'port = int(instance_info.get("port") or creds.get("port") or 5432)'],
    "backend/tier1_collector.py": ['port = int(record.get("port") or creds.get("port") or 5432)'],
    "backend/app.py": ['db_port = int(instance.get("port", 5432))',
                       'port = int(instance.get("port") or creds.get("port") or 5432)'],
}
for rel, lines in PORT_LINES.items():
    text = (REPO / rel).read_text(encoding="utf-8")
    for line in lines:
        check(line in text, f"{rel}: {line}")
BAD_PORT = re.compile(r"port\s*=\s*int\(\s*creds\.get\(\s*[\"']port[\"']")
for rel in SITES:
    text = (REPO / rel).read_text(encoding="utf-8")
    check(not BAD_PORT.search(text), f"{rel}: no connection port is read from the secret first")

print("")
print("== the endpoint outranks the secret's host, everywhere ==")
# The record's endpoint must outrank the secret's host, so no resolution line may read
# `creds.get("host")` ahead of the record's endpoint in the same expression.
BAD = re.compile(
    r"host\s*=\s*[^\n]*creds\.get\(\s*[\"']host[\"']\s*\)[^\n]*"
    r"(instance_info|record)\.get\(\s*[\"']endpoint[\"']"
)
for rel in SITES:
    text = (REPO / rel).read_text(encoding="utf-8")
    hits = BAD.findall(text)
    check(not hits, f"{rel}: no assignment puts the secret's host before the endpoint")

# pg_client names its own variables; assert the resolved order explicitly.
pg = (REPO / "backend/tools/aws/pg_client.py").read_text(encoding="utf-8")
check("host = registry_host or secret_host" in pg,
      "pg_client: registry endpoint -> secret host")
check("privatelink" not in pg.lower(),
      "pg_client: no third host rank (the Lattice path keeps the RDS hostname)")

# The diagnostic endpoint has to describe what the runtime does, or it misleads.
app = (REPO / "backend/app.py").read_text(encoding="utf-8")
check('result["connection_method"] = "registry_endpoint"' in app,
      "the test-connection diagnostic reports registry_endpoint, not secret_host, as the "
      "middle rank")
check('result["host_conflict"]' in app,
      "the diagnostic reports a host conflict rather than silently choosing")

print("")
print("== the resolution itself behaves ==")


def resolve(endpoint: str, secret_host: str, tags: dict = None):
    """The shared rule, restated so the intent is executable and not only asserted.
    Two ranks only: a database reached over a Lattice resource endpoint is still
    addressed by its RDS hostname (private DNS resolves it inside the agent VPC), so no
    connection path ever substitutes another name for the endpoint. A read replica also
    accepts its source's endpoint as the secret's host, and still dials its own."""
    tags = tags or {}
    allowed = {endpoint}
    if tags.get("replica_of") and tags.get("replica_source_endpoint"):
        allowed.add(tags["replica_source_endpoint"])
    if endpoint and secret_host and secret_host not in allowed:
        raise ValueError("contradiction")
    return endpoint or secret_host


cases = [
    # endpoint, secret_host, expected ("!" = must refuse)
    ("db1.rds.amazonaws.com", "db1.rds.amazonaws.com", "db1.rds.amazonaws.com"),
    ("db1.rds.amazonaws.com", "", "db1.rds.amazonaws.com"),
    ("", "self-registered.example.com", "self-registered.example.com"),
    # the failure this was written for
    ("db1.rds.amazonaws.com", "db2.rds.amazonaws.com", "!"),
]
REPLICA = {"replica_of": "prim", "replica_source_endpoint": "prim.rds.amazonaws.com"}
replica_cases = [
    # endpoint, secret_host, tags, expected
    ("rep.rds.amazonaws.com", "prim.rds.amazonaws.com", REPLICA, "rep.rds.amazonaws.com"),   # dials the replica
    ("rep.rds.amazonaws.com", "third.rds.amazonaws.com", REPLICA, "!"),
    ("solo.rds.amazonaws.com", "prim.rds.amazonaws.com", {}, "!"),                          # not a replica
    ("rep.rds.amazonaws.com", "prim.rds.amazonaws.com", {"replica_of": "prim"}, "!"),       # no source endpoint: strict
    ("rep.rds.amazonaws.com", "prim.rds.amazonaws.com",
     {"replica_source_endpoint": "prim.rds.amazonaws.com"}, "!"),                          # no replica_of: strict
]
cases = [(ep, sh, None, ex) for ep, sh, ex in cases] + replica_cases
for ep, sh, tags, expect in cases:
    try:
        got = resolve(ep, sh, tags)
        ok = got == expect
        desc = f"-> {got}"
    except ValueError:
        ok = expect == "!"
        desc = "-> refused"
    check(ok, f"endpoint={ep or '-'} secret={sh or '-'} replica={bool(tags)}  {desc}")

# No connection site reads a substitute host from the record: the Lattice path keeps
# the RDS name, so a record field that redirected the connection would be a way to
# point a valid credential at a different server.
for rel in SITES:
    text = (REPO / rel).read_text(encoding="utf-8")
    check("privatelink" not in text.lower(),
          f"{rel}: no substitute-host field is consulted")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
