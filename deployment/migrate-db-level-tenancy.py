#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Collapse per-space duplicate database records into one record per physical DB.

Background
----------
The registry used to create one ``instances`` record per (space, database): discovery
de-duplicated only within a space, so the same physical database onboarded into N
spaces became N records, N schedules and N scan histories. The DB-level tenancy model
makes the physical database the unit: one record per ARN, with ``member_space_ids``
(the spaces that include it) and ``reach_space_id`` (the member whose cross-account
role + secret actually scan it).

This one-off migration reconciles data written under the old model:

  * Records are grouped by physical identity (top-level ``arn`` or ``tags.arn``).
  * A group with duplicates keeps one **survivor** (a configured record if any) and the
    others are removed. The survivor gains the union of the group's spaces in
    ``member_space_ids``, a ``reach_space_id`` (its own space, else the first), a
    top-level ``arn``, and — if it lacks one — a secret adopted from a duplicate that
    has one (the discarded secret ARN is logged).
  * Every non-survivor's Tier-1/Tier-2 EventBridge schedules are deleted.
  * Single (non-duplicate) records are backfilled with the new fields so the
    membership-based read paths work after deploy.

Scan history (the metrics store's Tier 1 / Tier 2 tables and CloudWatch series) is
keyed by instance_id and is not re-pointed: a removed duplicate's history stays under its
old id, unread (Tier 1 items expire by TTL and CloudWatch points by its retention; Tier 2
summaries have no expiry).

Safety
------
Dry-run by default: it prints exactly what it *would* change and mutates nothing. Pass
``--apply`` to perform the changes. It is idempotent — running it again after a
successful run is a no-op. It touches only this application's own metadata (registry
records, scan history, schedules); it never reads a secret value or a customer
database, and never modifies secrets, IAM roles, spaces or users.

Usage
-----
    # Dry run (default) against a dev installation:
    AWS_PROFILE=my-dev python3 deployment/migrate-db-level-tenancy.py --region us-east-1

    # Apply:
    AWS_PROFILE=my-dev python3 deployment/migrate-db-level-tenancy.py --region us-east-1 --apply

The prefix defaults to ``dbops`` (or $RESOURCE_PREFIX); override the table with
``--instances-table`` if it does not follow the ``<prefix>-instances`` convention.
"""
import argparse
import os
import sys

import boto3


def _prefix() -> str:
    return (os.getenv("RESOURCE_PREFIX") or "").strip() or "dbops"


def _phys_key(rec: dict) -> str:
    """Physical identity of a record: the top-level arn, else the one under tags."""
    return rec.get("arn") or (rec.get("tags") or {}).get("arn", "")


def _space_of(rec: dict) -> str:
    return (rec.get("tags") or {}).get("space_id", "")


def _members(rec: dict) -> set:
    m = rec.get("member_space_ids")
    if m:
        return set(m)
    sid = _space_of(rec)
    return {sid} if sid else set()


def _pick_survivor(group: list) -> dict:
    """Deterministic survivor: a configured record first, then one with a secret,
    then the earliest created, then the lexically smallest id (stable tie-break)."""
    def rank(r):
        return (
            0 if r.get("status") == "configured" else 1,
            0 if r.get("secret_arn") else 1,
            r.get("created_at") or "",
            r.get("instance_id", ""),
        )
    return sorted(group, key=rank)[0]


def _schedule_names(instance_id: str) -> list:
    short = instance_id[:32]
    return [f"db-scan-{short}", f"db-tier1-{short}"]


def main() -> int:
    ap = argparse.ArgumentParser(description="Collapse per-space DB duplicates into one record per physical DB.")
    ap.add_argument("--region", default=os.getenv("AWS_REGION", "us-east-1"))
    ap.add_argument("--instances-table", default=None,
                    help="Override the registry table name (default: <prefix>-instances).")
    ap.add_argument("--apply", action="store_true", help="Perform changes (default: dry run).")
    args = ap.parse_args()

    table_name = args.instances_table or f"{_prefix()}-instances"
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] region={args.region} table={table_name}\n")

    dynamodb = boto3.resource("dynamodb", region_name=args.region)
    table = dynamodb.Table(table_name)
    scheduler = boto3.client("scheduler", region_name=args.region)

    # Scan all records (registries are small).
    records = []
    scan_kwargs = {}
    while True:
        resp = table.scan(**scan_kwargs)
        records.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    print(f"scanned {len(records)} registry records")

    # Group by physical identity. Records with no ARN are grouped alone (backfill only).
    groups = {}
    orphan_no_key = []
    for r in records:
        k = _phys_key(r)
        if k:
            groups.setdefault(k, []).append(r)
        else:
            orphan_no_key.append(r)

    dup_groups = {k: g for k, g in groups.items() if len(g) > 1}
    single_groups = {k: g for k, g in groups.items() if len(g) == 1}
    print(f"physical DBs: {len(groups)} ({len(dup_groups)} duplicated, {len(single_groups)} single); "
          f"{len(orphan_no_key)} record(s) without an ARN\n")

    stats = {"survivors_updated": 0, "records_deleted": 0, "schedules_deleted": 0,
             "singles_backfilled": 0}

    def _backfill(rec: dict, members: set, reach: str) -> None:
        """Set arn/member_space_ids/reach_space_id on a record that is missing them."""
        expr, names, vals = [], {}, {}
        arn = _phys_key(rec)
        if arn and not rec.get("arn"):
            expr.append("#arn = :arn"); names["#arn"] = "arn"; vals[":arn"] = arn
        if members and set(rec.get("member_space_ids") or set()) != members:
            expr.append("member_space_ids = :m"); vals[":m"] = members
        if reach and rec.get("reach_space_id") != reach:
            expr.append("reach_space_id = :r"); vals[":r"] = reach
        if not expr:
            return
        if args.apply:
            table.update_item(
                Key={"instance_id": rec["instance_id"]},
                UpdateExpression="SET " + ", ".join(expr),
                **({"ExpressionAttributeNames": names} if names else {}),
                ExpressionAttributeValues=vals,
            )

    # ── Collapse duplicate groups ──
    for arn, group in sorted(dup_groups.items()):
        survivor = _pick_survivor(group)
        losers = [r for r in group if r["instance_id"] != survivor["instance_id"]]
        members = set()
        for r in group:
            members |= _members(r)
        reach = survivor.get("reach_space_id") or _space_of(survivor) or (sorted(members)[0] if members else "")

        # Adopt a secret from a loser if the survivor has none (autopick; log discards).
        adopted_secret = None
        if not survivor.get("secret_arn"):
            for r in losers:
                if r.get("secret_arn"):
                    adopted_secret = r["secret_arn"]
                    break
        discarded = [r.get("secret_arn") for r in losers
                     if r.get("secret_arn") and r.get("secret_arn") != adopted_secret]

        print(f"DB {arn}")
        print(f"    survivor  {survivor['instance_id']} (status={survivor.get('status')}, "
              f"space={_space_of(survivor) or '-'})")
        print(f"    members   {sorted(members)}  reach={reach}")
        if adopted_secret:
            print("    adopt secret from a duplicate (survivor had none)")
        for s in discarded:
            print(f"    discard duplicate secret {s}")
        for r in losers:
            print(f"    remove    {r['instance_id']} (status={r.get('status')}, "
                  f"space={_space_of(r) or '-'})")

        if args.apply:
            upd = ["member_space_ids = :m", "reach_space_id = :r"]
            names, vals = {}, {":m": members, ":r": reach}
            if arn and not survivor.get("arn"):
                upd.append("#arn = :arn"); names["#arn"] = "arn"; vals[":arn"] = arn
            if adopted_secret:
                upd.append("secret_arn = :sec"); vals[":sec"] = adopted_secret
            table.update_item(
                Key={"instance_id": survivor["instance_id"]},
                UpdateExpression="SET " + ", ".join(upd),
                **({"ExpressionAttributeNames": names} if names else {}),
                ExpressionAttributeValues=vals,
            )
        stats["survivors_updated"] += 1

        for r in losers:
            for name in _schedule_names(r["instance_id"]):
                try:
                    if args.apply:
                        scheduler.delete_schedule(Name=name)
                    stats["schedules_deleted"] += 1
                except scheduler.exceptions.ResourceNotFoundException:
                    pass
                except Exception as e:  # noqa: BLE001 - best-effort cleanup
                    print(f"    ! schedule {name}: {e}")
            if args.apply:
                table.delete_item(Key={"instance_id": r["instance_id"]})
            stats["records_deleted"] += 1
        print()

    # ── Backfill single records so membership reads work post-deploy ──
    for arn, group in single_groups.items():
        rec = group[0]
        members = _members(rec)
        reach = rec.get("reach_space_id") or _space_of(rec) or (sorted(members)[0] if members else "")
        before = (rec.get("arn"), set(rec.get("member_space_ids") or set()), rec.get("reach_space_id"))
        _backfill(rec, members, reach)
        after = (_phys_key(rec) or rec.get("arn"), members, reach)
        if before != after:
            stats["singles_backfilled"] += 1

    if orphan_no_key:
        print(f"note: {len(orphan_no_key)} record(s) have no ARN and were left unchanged "
              f"(nothing to key membership on): "
              f"{[r.get('instance_id') for r in orphan_no_key]}\n")

    print("summary:", ", ".join(f"{k}={v}" for k, v in stats.items()))
    if not args.apply:
        print("\n(dry run — nothing changed. Re-run with --apply to perform these changes.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
