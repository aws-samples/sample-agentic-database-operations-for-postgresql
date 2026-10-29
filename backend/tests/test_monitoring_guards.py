# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Monitoring config guards: a cluster's Tier 1 cadence fits one run.

A cluster's Tier 1 run collects every member and can last up to the collector's
timeout. EventBridge Scheduler starts the next run whether or not the last one finished,
so a cadence shorter than that timeout could overlap runs. The API refuses such a
cadence for a cluster (the console disables it, from ``tier1_allowed``); a single
instance keeps every cadence.

The handlers are extracted from backend/app.py by source and run against a fake
registry and a captured ``_apply_monitoring_config``. Hermetic, no AWS, no FastAPI.

Run:  python3 backend/tests/test_monitoring_guards.py
"""
import pathlib
import re
import sys
import types

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP_SRC = (ROOT / "backend/app.py").read_text(encoding="utf-8")
passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def _extract(name: str) -> str:
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^(?:@|def |class |[A-Z_]+ = )|\Z)", APP_SRC, re.S | re.M)
    assert m, f"{name} not found in app.py"
    return m.group(0)


def _const(name: str) -> str:
    m = re.search(rf"^{name} = .*?$", APP_SRC, re.M)
    assert m, name
    return m.group(0)


class FakeHTTPException(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


CLUSTER = {"instance_id": "db-orders", "display_name": "orders", "status": "configured",
           "arn": "arn:aws:rds:us-east-1:111111111111:cluster:orders",
           "cluster_member_roles": [{"instance_id": "orders-1", "is_writer": True}]}
SINGLE = {"instance_id": "db-inv", "display_name": "inventory", "status": "configured",
          "arn": "arn:aws:rds:us-east-1:111111111111:db:inventory"}


SLOW_CLUSTER = dict(CLUSTER, instance_id="db-fast", tier1_frequency="1min")


def build():
    applied = []
    rows = {r["instance_id"]: dict(r) for r in (CLUSTER, SINGLE, SLOW_CLUSTER)}
    ns = {"HTTPException": FakeHTTPException, "Request": object, "MonitoringConfig": object,
          "BulkMonitoringConfig": object,
          "db_registry": types.SimpleNamespace(get_instance=lambda iid: dict(rows[iid]) if iid in rows else None),
          "_monitoring_edit_block": lambda auth, inst: "",
          "is_admin": lambda auth: True,
          "_apply_monitoring_config": lambda iid, cfg, name="": (
              applied.append((iid, cfg)),
              {"results": {}, "failed": {}, "applied": dict(cfg)})[1],
          "_audit_monitoring_set": lambda *a, **k: None,
          "JSONResponse": lambda status_code=200, content=None: ("JSONResponse", status_code, content),
          "_instance_member_spaces": lambda inst: set(inst.get("member_space_ids") or ())}
    for c in ("TIER1_FREQUENCIES", "TIER1_RUN_TIMEOUT_S", "TIER1_MIN_FREQUENCY"):
        exec(_const(c), ns)  # noqa: S102
    # TIER2_FREQUENCIES spans two lines.
    exec(re.search(r"^TIER2_FREQUENCIES = \{.*?\}", APP_SRC, re.S | re.M).group(0), ns)  # noqa: S102
    for fn in ("_is_cluster_record", "_effective_tier1_frequency",
               "_tier1_cadence_block", "_tier1_cadence_check", "_tier1_pause_warning",
               "_monitoring_view", "_monitoring_set_result",
               "admin_set_monitoring_config", "update_monitoring_config", "bulk_update_monitoring"):
        exec(_extract(fn), ns)  # noqa: S102 - pinning shipped source
    return ns, applied


def cfg(**kw):
    vals = {"tier1_frequency": None, "tier1_enabled": None, "tier2_frequency": None,
            "tier2_enabled": None, "retention_days": None, "instance_ids": None}
    vals.update(kw)
    o = types.SimpleNamespace(**vals)
    o.dict = lambda exclude_none=False: {k: v for k, v in vals.items()
                                         if k != "instance_ids" and not (exclude_none and v is None)}
    return o


REQ = types.SimpleNamespace(headers={"Authorization": "Bearer x"})


def status(fn, *a, **k):
    try:
        return 200, fn(*a, **k)
    except FakeHTTPException as e:
        return e.status_code, e.detail


print("== the collector timeout the API reasons about is the template's ==")


class _L(yaml.SafeLoader):
    pass


_L.add_multi_constructor("!", lambda loader, suffix, node: None)
tpl = yaml.load((ROOT / "backend/template.yaml").read_text(encoding="utf-8"), Loader=_L)  # nosec B506
ns, _ = build()
check(tpl["Resources"]["Tier1CollectorFunction"]["Properties"]["Timeout"] == ns["TIER1_RUN_TIMEOUT_S"],
      f"TIER1_RUN_TIMEOUT_S == the Lambda timeout ({ns['TIER1_RUN_TIMEOUT_S']} s)")

print("== 1min and 2min are no longer offered ==")
ns, _ = build()
check(list(ns["TIER1_FREQUENCIES"]) == ["5min", "10min", "15min", "30min"],
      "TIER1_FREQUENCIES drops 1min and 2min")
check(ns["TIER1_MIN_FREQUENCY"] == "5min", "the shortest offered cadence is 5min")
for freq in ("1min", "2min"):
    for route in ("admin_set_monitoring_config", "update_monitoring_config"):
        ns, applied = build()
        code, detail = status(ns[route], "db-orders", cfg(tier1_frequency=freq), REQ)
        check(code == 400 and "Invalid tier1_frequency" in detail and "5min" in detail and not applied,
              f"{route}: {freq} on a cluster is 400 with the offered options, nothing applied")
        ns, applied = build()
        code, detail = status(ns[route], "db-inv", cfg(tier1_frequency=freq), REQ)
        check(code == 400 and "Invalid tier1_frequency" in detail and not applied,
              f"{route}: {freq} on a single instance is rejected too (not just clusters)")
ns, applied = build()
code, detail = status(ns["bulk_update_monitoring"], cfg(tier1_frequency="1min", instance_ids=["db-inv"]), REQ)
check(code == 400 and "Invalid tier1_frequency" in detail, "bulk rejects 1min up front")

print("== every offered cadence fits a cluster run (closes the overlap gap, #8) ==")
for freq in ("5min", "10min", "15min", "30min"):
    ns, applied = build()
    code, _ = status(ns["admin_set_monitoring_config"], "db-orders", cfg(tier1_frequency=freq), REQ)
    check(code == 200 and applied == [("db-orders", {"tier1_frequency": freq})],
          f"{freq} on a cluster is applied")
ns, applied = build()
code, _ = status(ns["admin_set_monitoring_config"], "db-orders", cfg(tier1_enabled=False), REQ)
check(code == 200, "a change without a cadence is not judged by it")

print("== a legacy 1min/2min stored value resolves to 5min on read ==")
ns, _ = build()
eff, note = ns["_effective_tier1_frequency"]("1min")
check(eff == "5min" and bool(note) and "5min" in note, "a stored 1min resolves to 5min with an explanatory note")
eff, note = ns["_effective_tier1_frequency"]("5min")
check(eff == "5min" and note == "", "an offered value passes through with no note")
eff, note = ns["_effective_tier1_frequency"](None)
check(eff == "5min" and note == "", "an unset value defaults to 5min with no note")
view = ns["_monitoring_view"](dict(CLUSTER, tier1_frequency="2min"))
check(view["tier1_frequency"] == "5min" and bool(view["tier1_frequency_note"]),
      "the view shows 5min and a note for a legacy stored cadence")
view = ns["_monitoring_view"](dict(CLUSTER, tier1_frequency="10min"))
check(view["tier1_frequency"] == "10min" and view["tier1_frequency_note"] == "",
      "an offered stored cadence shows unchanged, with no note")

print("== a save heals a legacy stored cadence to 5min (source) ==")
apply_src = _extract("_apply_monitoring_config")
check("stored_freq not in TIER1_FREQUENCIES" in apply_src
      and "_effective_tier1_frequency(stored_freq)" in apply_src,
      "_apply_monitoring_config heals a legacy stored cadence when a save touches Tier 1")

print("== the console no longer offers 1min/2min ==")
for comp in ("Monitoring.jsx", "SpaceDetail.jsx"):
    src = (ROOT / "admin-console/src/components" / comp).read_text(encoding="utf-8")
    opts = src.split("TIER1_OPTIONS = [", 1)[1].split("]", 1)[0]
    check("'1min'" not in opts and "'2min'" not in opts and "'5min'" in opts,
          f"{comp}: TIER1_OPTIONS drops 1min/2min, keeps 5min")
check("tier1_frequency_note" in (ROOT / "admin-console/src/components/Monitoring.jsx").read_text(encoding="utf-8"),
      "Monitoring.jsx surfaces the legacy-coercion note")

print("== the view tells the console which cadences a database accepts ==")
ns, _ = build()
check(ns["_monitoring_view"](CLUSTER)["tier1_allowed"] == ["5min", "10min", "15min", "30min"], "a cluster")
check(ns["_monitoring_view"](SINGLE)["tier1_allowed"] == list(ns["TIER1_FREQUENCIES"]), "a single instance")
JSX = (ROOT / "admin-console/src/components/Monitoring.jsx").read_text(encoding="utf-8")
check("db.tier1_allowed" in JSX and "disabled={off}" in JSX, "the console disables the refused cadences")

print("== pausing Tier 1 on a database that depends on it: allowed, with a warning ==")
HOST_SINGLE = dict(SINGLE, network_route_mode="host")
for iid, rec, words in (("db-orders", CLUSTER, ("failover detection",)),
                        ("db-hostinst", HOST_SINGLE, ("route checks",)),
                        ("db-both", dict(CLUSTER, network_route_mode="host"), ("failover detection", "route checks"))):
    ns, applied = build()
    rows = {"db-orders": CLUSTER, "db-hostinst": HOST_SINGLE, "db-both": dict(CLUSTER, network_route_mode="host"),
            "db-inv": SINGLE}
    ns["db_registry"] = types.SimpleNamespace(get_instance=lambda i: dict(rows[i]) if i in rows else None)
    code, out = status(ns["admin_set_monitoring_config"], iid, cfg(tier1_enabled=False), REQ)
    check(code == 200 and applied and applied[0][1] == {"tier1_enabled": False},
          f"{iid}: pausing stays allowed and is applied")
    check(all(w in out.get("warning", "") for w in words) and "stop" in out["warning"],
          f"{iid}: the response carries the warning ({', '.join(words)})")
    check(ns["_monitoring_view"](rec)["tier1_pause_warning"] == out["warning"],
          f"{iid}: the console's dialog text is the same as the API's")
ns, applied = build()
code, out = status(ns["admin_set_monitoring_config"], "db-inv", cfg(tier1_enabled=False), REQ)
check(code == 200 and "warning" not in out and ns["_monitoring_view"](SINGLE)["tier1_pause_warning"] == "",
      "a single instance on a VPC-wide route: no warning")
ns, applied = build()
code, out = status(ns["admin_set_monitoring_config"], "db-orders", cfg(tier1_enabled=True), REQ)
check("warning" not in out, "resuming carries no warning")
code, out = status(ns["update_monitoring_config"], "db-orders", cfg(tier1_enabled=False), REQ)
check("failover detection" in out.get("warning", ""), "the legacy route carries it too")
code, out = status(ns["bulk_update_monitoring"], cfg(tier1_enabled=False, instance_ids=["db-orders", "db-inv"]), REQ)
check(set(out.get("warnings", {})) == {"db-orders"}, "bulk: a warning per database that needs one")
check("PauseTier1Dialog" in JSX and "db.tier1_pause_warning ? setConfirmPause(true)" in JSX
      and 'role="alertdialog"' in JSX, "the console confirms before pausing")

print("== a scheduler failure answers non-2xx with per-tier state (#7) ==")
for route in ("admin_set_monitoring_config", "update_monitoring_config"):
    ns, applied = build()
    # Tier 1's schedule change failed; Tier 2's succeeded and was stored.
    ns["_apply_monitoring_config"] = lambda iid, c, name="": {
        "results": {"tier1": "error: AccessDeniedException", "tier2": "updated: rate(6 hours)"},
        "failed": {"tier1": "AccessDeniedException"},
        "applied": {"tier2_frequency": "6h"}}
    code, out = status(ns[route], "db-inv", cfg(tier1_frequency="10min", tier2_frequency="6h"), REQ)
    check(out[0] == "JSONResponse" and out[1] == 502, f"{route}: a scheduler failure answers 502")
    body = out[2]
    check(body["failed"] == {"tier1": "AccessDeniedException"}, f"{route}: the failed tier is named")
    check(body["applied"] == {"tier2_frequency": "6h"}, f"{route}: only the succeeded tier is in applied")
    check("Could not apply Tier 1" in body["detail"] and "AccessDeniedException" in body["detail"],
          f"{route}: the detail says which tier and why")
# The console reads per-tier state: keeps old values for a failed tier, applies the rest.
check("e.body" in JSX and "body.applied" in JSX and "body.failed" in JSX
      and "resetTier1" in JSX and "resetTier2" in JSX,
      "the console applies the succeeded tier and resets the failed one from the error body")
API = (ROOT / "admin-console/src/hooks/useApi.js").read_text(encoding="utf-8")
check("err.body = body" in API, "the API wrapper carries the error body so per-tier state survives")

print("== the Collection view: collector notes on the record ==")
ns, _ = build()
since = "2030-01-01T00:00:00+00:00"
check(ns["_monitoring_view"](dict(CLUSTER, member_sync_failures=1, member_sync_failing_since=since))
      ["member_sync_failing_since"] is None, "one failed member sync is not shown")
check(ns["_monitoring_view"](dict(CLUSTER, member_sync_failures=2, member_sync_failing_since=since))
      ["member_sync_failing_since"] == since, "two in a row: 'member sync failing since' is shown")
note = {"at": since, "message": "The metrics store write failed. Check the collector's permissions on the Tier 1 table."}
check(ns["_monitoring_view"](dict(CLUSTER, tier1_store_error=note))["tier1_store_error"] == note,
      "a store schema problem is shown")
check(ns["_monitoring_view"](CLUSTER)["tier1_store_error"] is None, "and nothing when there is none")
COLL = (ROOT / "admin-console/src/components/MonitoringCollection.jsx").read_text(encoding="utf-8")
check("Member sync failing since" in COLL and "d.tier1_store_error?.message" in COLL,
      "the Collection view renders both notes")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
