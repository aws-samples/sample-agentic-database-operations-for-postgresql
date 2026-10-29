#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Golden-case harness for the diagnostic checks.

Runs the REAL db_tools against a live PostgreSQL/Aurora database (read-only) and
prints a per-tool + per-check "test pack" so a human can score each check against
five criteria (facts correct, severity correct, schema-qualified, deterministic,
actionable). It exercises the actual tool code path — the same SQL and the same
references.yaml-driven severity the agent uses — not a reimplementation.

It imports db_tools behind a lightweight `strands` shim, so it needs only
psycopg2 + boto3 + pyyaml (not the full agent runtime).

Connectivity: the tools resolve the DB host from the Secrets Manager secret. When
running through an SSM port-forward, pass --host-override / --port-override to
redirect the connection at the tunnel while still using the secret's credentials.

Usage:
    python3 golden_harness.py --secret-arn <ARN> --database postgres
    python3 golden_harness.py --secret-arn <ARN> --database postgres \\
        --host-override 127.0.0.1 --port-override 15432
    python3 golden_harness.py --secret-arn <ARN> --only get_vacuum_stats_health --json

Read-only by design. Never writes to the target database.
"""

import argparse
import inspect
import json
import sys
import time
import types
from pathlib import Path

# ── strands shim ──────────────────────────────────────────────────────────────
# db_tools decorates every tool with @tool from strands. For a standalone harness
# we don't want the full agent runtime, so inject a shim module whose `tool` is an
# identity decorator (works as @tool and @tool(...)). __getattr__ covers any other
# name a transitively-imported module might pull from strands.
if "strands" not in sys.modules:
    _shim = types.ModuleType("strands")

    def _identity_tool(func=None, **_kwargs):
        if callable(func):
            return func
        return lambda f: f

    _shim.tool = _identity_tool
    _shim.__getattr__ = lambda name: _identity_tool  # any other import → harmless callable
    sys.modules["strands"] = _shim

# backend/ must be importable so `tools.*` resolves (this file lives in backend/checks/).
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

_REGISTRY_YAML = Path(__file__).resolve().parent / "registry.yaml"

# Tools listed in registry test_tools that are NOT DB-backed db_tools (they hit
# CloudWatch / the metrics store / delegate to another agent) — skipped by the harness.
_NON_DB_TOOLS = {"observability_agent", "get_historical_metrics", "get_workload_profile"}


def _load_registry() -> dict:
    import yaml
    with open(_REGISTRY_YAML, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("checks", {}) or {}


def _patch_host(host_override, port_override):
    """Redirect the DB endpoint (for SSM port-forward runs) while keeping the
    secret's real username/password. Patches the credential resolver in place."""
    import tools.aws.pg_client as pgc
    real = pgc.get_db_credentials

    def patched(secret_arn: str) -> dict:
        creds = dict(real(secret_arn))
        if host_override:
            creds["host"] = host_override
        if port_override:
            creds["port"] = port_override
        return creds

    pgc.get_db_credentials = patched


def _runnable_tools(registry: dict):
    """db_tools functions bound by at least one check, callable with just
    (secret_arn[, database]). Returns [(tool_name, fn), ...] in registry order."""
    import tools.db_tools as db

    wanted, seen = [], set()
    for entry in registry.values():
        for tname in (entry.get("test_tools") or []):
            if tname in _NON_DB_TOOLS or tname in seen:
                continue
            seen.add(tname)
            fn = getattr(db, tname, None)
            if fn is None or not callable(fn):
                continue
            params = set(inspect.signature(fn).parameters)
            extra = params - {"secret_arn", "database"}
            if "secret_arn" in params and not extra:
                wanted.append((tname, fn))
    return wanted


def _severity_histogram(result):
    """Tally the common verdict fields across a tool's output for a quick read on
    whether severity is being applied. Looks at top-level and one level of list rows."""
    keys = ("status", "health", "severity", "risk", "vacuum_status", "assessment")
    hist = {}

    def tally(d):
        if not isinstance(d, dict):
            return
        for k in keys:
            if k in d and isinstance(d[k], (str, bool)):
                hist[str(d[k])] = hist.get(str(d[k]), 0) + 1

    if isinstance(result, dict):
        tally(result)
        for v in result.values():
            if isinstance(v, list):
                for row in v[:200]:
                    tally(row)
            elif isinstance(v, dict):
                tally(v)
    return hist


def _shape(result):
    """Compact description of a result: list-valued keys with counts, scalars inline."""
    if not isinstance(result, dict):
        return f"<{type(result).__name__}>"
    parts = []
    for k, v in result.items():
        if isinstance(v, list):
            parts.append(f"{k}[{len(v)}]")
        elif isinstance(v, dict):
            parts.append(f"{k}{{{len(v)}}}")
        elif isinstance(v, (str, int, float, bool)) or v is None:
            s = str(v)
            parts.append(f"{k}={s[:40]}")
    return ", ".join(parts)


def _sample_row(result):
    """First list row found, truncated — a spot-check that facts look sane."""
    if isinstance(result, dict):
        for v in result.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                row = {k: (str(val)[:60]) for k, val in list(v[0].items())[:8]}
                return row
    return None


def main():
    ap = argparse.ArgumentParser(description="Golden-case harness for diagnostic checks")
    ap.add_argument("--secret-arn", required=True, help="Secrets Manager ARN with DB credentials")
    ap.add_argument("--database", default="postgres", help="Database name (default: postgres)")
    ap.add_argument("--host-override", default=None, help="Redirect host (SSM port-forward)")
    ap.add_argument("--port-override", type=int, default=None, help="Redirect port (SSM port-forward)")
    ap.add_argument("--only", default=None, help="Run just one tool by name")
    ap.add_argument("--json", action="store_true", help="Dump full JSON per tool")
    args = ap.parse_args()

    if args.host_override or args.port_override:
        _patch_host(args.host_override, args.port_override)

    registry = _load_registry()
    tools = _runnable_tools(registry)
    if args.only:
        tools = [(n, f) for n, f in tools if n == args.only]
        if not tools:
            print(f"No runnable tool named {args.only!r}")
            return 2

    print("=" * 78)
    print(f"GOLDEN-CASE HARNESS — {len(tools)} tools · db={args.database} · "
          f"{'tunnel ' + args.host_override if args.host_override else 'direct'}")
    print("=" * 78)

    results, errors = {}, {}
    for name, fn in tools:
        t0 = time.time()
        try:
            res = fn(secret_arn=args.secret_arn, database=args.database)
            dt = time.time() - t0
            results[name] = res
            hist = _severity_histogram(res)
            print(f"\n▶ {name}  ({dt:.2f}s)")
            print(f"    shape: {_shape(res)}")
            if hist:
                print(f"    verdicts: {hist}")
            sample = _sample_row(res)
            if sample:
                print(f"    sample: {sample}")
            if args.json:
                print(json.dumps(res, indent=2, default=str)[:4000])
        except Exception as e:
            dt = time.time() - t0
            errors[name] = f"{type(e).__name__}: {e}"
            print(f"\n✗ {name}  ({dt:.2f}s)  ERROR: {type(e).__name__}: {str(e)[:200]}")

    # ── per-check view (catalog_id · display_name · bound tool · verdict) ──
    print("\n" + "=" * 78)
    print("PER-CHECK VIEW (for scoring)")
    print("=" * 78)
    for slug, e in registry.items():
        bound = next((t for t in (e.get("test_tools") or [])
                      if t in results or t in errors), None)
        verdict = ""
        if bound in results:
            h = _severity_histogram(results[bound])
            verdict = f"{h}" if h else "(facts only)"
        elif bound in errors:
            verdict = "ERROR"
        print(f"  {e.get('catalog_id','?'):>8}  {e.get('display_name','?')[:38]:<38} "
              f"{(bound or '-'):<28} {verdict}")

    # ── summary ──
    print("\n" + "=" * 78)
    print(f"SUMMARY: {len(results)} clean, {len(errors)} errored, of {len(tools)} tools")
    if errors:
        print("ERRORS:")
        for n, msg in errors.items():
            print(f"  ✗ {n}: {msg[:160]}")
    print("=" * 78)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
