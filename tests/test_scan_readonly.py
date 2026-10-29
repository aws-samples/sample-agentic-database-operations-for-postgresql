#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The shell scan leg runs in a read-only session and never assigns a transaction ID.

The batch scan opens raw ``psql`` sessions against the customer's database. Those sessions
inherit only the least-privilege ``dbops_ro`` role for write protection. This guard adds a
second, independent control and keeps it honest:

  1. ``run_scan.py`` sets ``default_transaction_read_only=on`` in ``PGOPTIONS`` alongside the
     statement timeout, so every transaction the shell leg opens is read-only at the session
     level regardless of the role it connects as (defense-in-depth).

  2. A read-only session refuses to *assign* a transaction ID, so the classic XID-wraparound
     probes ``txid_current()`` and ``pg_current_xact_id()`` (both of which assign an XID as a
     side effect) would error under (1). Every executed XID probe must instead read the XID
     frontier through the non-assigning snapshot form
     ``txid_snapshot_xmax(txid_current_snapshot())``, which works on all supported PostgreSQL
     versions and does not assign an XID.

Checking the source text rather than running the script is deliberate: the script only runs
against a live database, so a hermetic test cannot exercise it. This asserts the two
properties that make it read-only-safe so a future edit cannot silently reintroduce a write
or an XID-assigning call.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "batch/postgres_health_insights.sh"
RUN_SCAN = ROOT / "batch/run_scan.py"

passed = 0
failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


run_scan = RUN_SCAN.read_text(encoding="utf-8")
script = SCRIPT.read_text(encoding="utf-8")
lines = script.splitlines()

print("══ run_scan.py opens the shell psql leg as a read-only session ══")
pgoptions = [ln for ln in run_scan.splitlines() if 'env["PGOPTIONS"]' in ln]
# There may be more than one PGOPTIONS assignment (the scan leg, plus the post-failure
# diagnostic probes). EVERY one must be read-only so no session can write to the customer DB.
check(len(pgoptions) >= 1, f"at least one PGOPTIONS assignment (found {len(pgoptions)})")
check(all("default_transaction_read_only=on" in ln for ln in pgoptions),
      "every PGOPTIONS assignment sets default_transaction_read_only=on")
scan_opt = [ln for ln in pgoptions if "statement_timeout=300000" in ln]
check(len(scan_opt) == 1,
      "the scan leg's PGOPTIONS keeps default_transaction_read_only=on with statement_timeout=300000")

print("")
print("══ no executed query assigns a transaction ID ══")
# An assigning call is only a problem when PostgreSQL actually runs it. It runs it when it
# reaches the server: either inside a psql -c "..." argument, or inside a SQL heredoc column
# list (the generate_xid_query / transaction-stats "... AS current_xid" sites). Comments (#)
# and status echoes are inert. Assert the assigning forms survive only on inert lines.
ASSIGNING = re.compile(r"\b(?:txid_current|pg_current_xact_id)\s*\(\s*\)")
offenders = []
for n, ln in enumerate(lines, 1):
    if not ASSIGNING.search(ln):
        continue
    stripped = ln.lstrip()
    is_comment = stripped.startswith("#")
    is_echo = "echo " in ln and "psql" not in ln
    if not (is_comment or is_echo):
        offenders.append((n, ln.strip()))
check(not offenders,
      f"every surviving txid_current()/pg_current_xact_id() is a comment or echo "
      f"(offenders: {offenders})")

print("")
print("══ XID probes read the frontier without assigning (snapshot form) ══")
snap = script.count("txid_snapshot_xmax(txid_current_snapshot())")
check(snap >= 5, f"the non-assigning snapshot form is used at every XID site (found {snap})")

# The two capture points that shell out to psql -c must use the snapshot form.
xid_capture = [ln for ln in lines if re.search(r"(START_XID|END_XID)=", ln)]
check(len(xid_capture) == 2, f"both START_XID and END_XID capture lines present (found {len(xid_capture)})")
check(all("txid_snapshot_xmax(txid_current_snapshot())" in ln for ln in xid_capture),
      "START_XID and END_XID shell out to the snapshot form")
check(not any(re.search(r'-c\s+"SELECT\s+(?:txid_current|pg_current_xact_id)', ln) for ln in lines),
      "no psql -c executes a bare txid_current()/pg_current_xact_id()")

# The heredoc SQL that projects a current_xid column must use the snapshot form too.
xid_cols = [ln for ln in lines if re.search(r"\bas\s+current_xid\b", ln, re.IGNORECASE)]
check(len(xid_cols) >= 3, f"current_xid projection sites present (found {len(xid_cols)})")
check(all("txid_snapshot_xmax(txid_current_snapshot())" in ln for ln in xid_cols),
      "every 'AS current_xid' projection uses the snapshot form")

print("")
print("══ the scan job cannot touch the admin audit log ══")
# The scan runs customer-facing SQL; it records nothing in the audit index and has no
# business near the vault. Neither its execution role nor its job role may name the
# audit table or the audit vault bucket.
import yaml  # noqa: E402


class _Loader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation's short-form tags (!Ref, !Sub, ...)."""


def _keep(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_Loader.add_multi_constructor("!", _keep)
runtime = yaml.load((ROOT / "backend/template.yaml").read_text(encoding="utf-8"),  # nosec B506
                    Loader=_Loader)
for role in ("BatchJobRole", "BatchExecutionRole"):
    res = (runtime.get("Resources") or {}).get(role)
    check(isinstance(res, dict), f"{role} is declared")
    if not isinstance(res, dict):
        continue
    resources = []
    for pol in (res.get("Properties") or {}).get("Policies") or []:
        for st in ((pol or {}).get("PolicyDocument") or {}).get("Statement") or []:
            for branch in (st if isinstance(st, list) else [st]):
                if isinstance(branch, dict):
                    r = branch.get("Resource")
                    resources += [x for x in (r if isinstance(r, list) else [r]) if isinstance(x, str)]
    check(bool(resources), f"{role}: its grants were read ({len(resources)})")
    audit = [r for r in resources
             if r.endswith("-audit") or "-audit/" in r or "-audit-" in r or "AuditBucket" in r]
    check(not audit, f"{role}: no grant on the audit table or the audit vault bucket ({audit})")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
