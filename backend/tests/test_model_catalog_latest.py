# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The model catalog offers only the latest Claude Haiku, Sonnet and Opus.

Each as its global inference profile and its regional (geo) profile for the region,
whichever Bedrock returns: "latest" is derived from the ids Bedrock lists, never
hard-coded, and no id is invented (a region with no global profile shows the regional
one only). An enabled model a newer release superseded stays listed, enabled, with a
note, so turning it off is one click and nothing in use silently disappears.

Run:  python3 backend/tests/test_model_catalog_latest.py
"""
import os
import pathlib
import re
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.aws import model_discovery as md  # noqa: E402

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def P(*ids):
    return [{"inferenceProfileId": i} for i in ids]


US = P(
    "us.anthropic.claude-3-haiku-20240307-v1:0",
    "us.anthropic.claude-3-5-haiku-20241022-v1:0",
    "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.anthropic.claude-3-7-sonnet-20250219-v1:0",
    "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "global.anthropic.claude-sonnet-4-20250514-v1:0",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-opus-4-20250514-v1:0",
    "us.anthropic.claude-opus-4-1-20250805-v1:0",
    "global.anthropic.claude-opus-4-5-20251101-v1:0",
    "us.anthropic.claude-opus-4-5-20251101-v1:0",
    "us.meta.llama3-2-90b-instruct-v1:0",
    "us.amazon.nova-pro-v1:0",
)

print("== parse_model_id: both naming schemes ==")
a = md.parse_model_id("global.anthropic.claude-sonnet-4-5-20250929-v1:0")
b = md.parse_model_id("us.anthropic.claude-3-7-sonnet-20250219-v1:0")
c = md.parse_model_id("us.anthropic.claude-sonnet-4-20250514-v1:0")
check(a["family"] == "sonnet" and a["scope"] == "global" and a["version_label"] == "4.5", "claude-sonnet-4-5")
check(b["family"] == "sonnet" and b["scope"] == "us" and b["version_label"] == "3.7", "claude-3-7-sonnet")
check(b["version"] < c["version"] < a["version"], "3.7 < 4 < 4.5 (a bare 4 is not outranked by its date)")
d = md.parse_model_id("eu.anthropic.claude-opus-4-6-v1")
check(d and d["version_label"] == "4.6" and d["version"] > md.parse_model_id(
    "eu.anthropic.claude-opus-4-5-20251101-v1:0")["version"], "an undated id parses and compares by version")
for other in ("us.meta.llama3-2-90b-instruct-v1:0", "us.amazon.nova-pro-v1:0", "", None,
              "us.anthropic.claude-instant-v1"):
    check(md.parse_model_id(other) is None, f"not a Haiku/Sonnet/Opus id: {other!r}")

print("== select_latest: per family the latest, global + regional ==")
out = md.select_latest(US)
ids = [m["model_id"] for m in out]
check(ids == ["global.anthropic.claude-haiku-4-5-20251001-v1:0", "us.anthropic.claude-haiku-4-5-20251001-v1:0",
              "global.anthropic.claude-sonnet-4-5-20250929-v1:0", "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
              "global.anthropic.claude-opus-4-5-20251101-v1:0", "us.anthropic.claude-opus-4-5-20251101-v1:0"],
      f"Haiku, Sonnet, Opus; global before regional; nothing older ({ids})")
check(all(m["scope"] in ("global", "regional") for m in out)
      and {m["geo"] for m in out if m["scope"] == "regional"} == {"us"}, "scope and geo per entry")
check(out[1]["display_name"] == "Claude Haiku 4.5 (regional · US)" and out[0]["display_name"].endswith("(global)"),
      "labelled by family, version and where it runs")
check(all(m["recommended"] for m in out) and out[0]["cost_tier"] == "low" and out[4]["cost_tier"] == "high",
      "advisory metadata comes from the family, so a new release inherits it")

EU = [{"inferenceProfileId": i.replace("us.", "eu.")} for i in (p["inferenceProfileId"] for p in US)
      if i.startswith("us.")]
out = md.select_latest(EU)
check([m["model_id"] for m in out] == ["eu.anthropic.claude-haiku-4-5-20251001-v1:0",
                                      "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
                                      "eu.anthropic.claude-opus-4-5-20251101-v1:0"],
      "a region with regional profiles only: no global id is invented")
out = md.select_latest(P("global.anthropic.claude-sonnet-4-5-20250929-v1:0",
                         "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
                         "eu.anthropic.claude-sonnet-4-20250514-v1:0"))
check([m["geo"] for m in out] == ["global", "eu"], "global + the region's own geo (eu in Ireland)")
check(md.select_latest([]) == [] and md.select_latest(P("us.amazon.nova-pro-v1:0")) == [],
      "no Claude models: an empty catalog")
newer = md.select_latest(US + P("global.anthropic.claude-sonnet-4-6-v1", "us.anthropic.claude-sonnet-4-6-v1"))
check([m["model_id"] for m in newer if m["family"] == "sonnet"] ==
      ["global.anthropic.claude-sonnet-4-6-v1", "us.anthropic.claude-sonnet-4-6-v1"],
      "a new release replaces its family's entries without a code change")

print("== is_superseded ==")
latest = md.select_latest(US)
check(md.is_superseded("us.anthropic.claude-sonnet-4-20250514-v1:0", latest), "an older Sonnet is superseded")
check(md.is_superseded("us.anthropic.claude-3-5-haiku-20241022-v1:0", latest), "an older Haiku is superseded")
check(not md.is_superseded("global.anthropic.claude-opus-4-5-20251101-v1:0", latest), "the latest is not")
check(not md.is_superseded("us.amazon.nova-pro-v1:0", latest), "a non-family id is never 'superseded'")
check(not md.is_superseded("us.anthropic.claude-sonnet-4-20250514-v1:0", []), "nothing to compare: not superseded")

print("== discovery lists this region's profiles and fails soft ==")
pages = [{"inferenceProfileSummaries": US[:7], "nextToken": "t"}, {"inferenceProfileSummaries": US[7:]}]
md._bedrock = lambda: types.SimpleNamespace(list_inference_profiles=lambda **k: pages[1 if k.get("nextToken") else 0])
md._cache.update(models=[], ts=0.0, ok=False)
got = md.discover_models(force=True)
check(len(got["models"]) == 6 and got["stale"] is False, "every page is read, the latest six are kept")


def boom():
    raise RuntimeError("AccessDenied")


md._bedrock = boom
got = md.discover_models(force=True)
check(len(got["models"]) == 6 and got["stale"] is True, "a Bedrock error keeps the last catalog, marked stale")

print("== the catalog keeps an enabled superseded model, with a note ==")
APP = (pathlib.Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
src = re.search(r"^def get_model_catalog\(.*?(?=^@app\.)", APP, re.S | re.M).group(0)
ns = {"Request": object, "HTTPException": Exception, "is_admin": lambda a: True,
      "discover_models": lambda: {"models": latest, "refreshed_at": 1.0, "stale": False},
      "account_default_model": lambda c: ("global.anthropic.claude-haiku-4-5-20251001-v1:0", "deployment"),
      "governance_registry": types.SimpleNamespace(get_model_catalog=lambda: {"models": {
          "us.anthropic.claude-sonnet-4-20250514-v1:0": {"enabled": True},
          "us.anthropic.claude-3-haiku-20240307-v1:0": {"enabled": False},
          "us.anthropic.claude-gone-v1": {"enabled": True},
          "global.anthropic.claude-haiku-4-5-20251001-v1:0": {"enabled": True}}})}
exec(src, ns)  # noqa: S102 - pinning shipped source
cat = ns["get_model_catalog"](types.SimpleNamespace(headers={"Authorization": "x"}))
by = {m["model_id"]: m for m in cat["models"]}
old = by.get("us.anthropic.claude-sonnet-4-20250514-v1:0")
check(old and old["enabled"] and old["superseded"] is True and "newer model" in old["note"],
      "an enabled older Sonnet stays listed, enabled, marked superseded")
check("us.anthropic.claude-3-haiku-20240307-v1:0" not in by, "a disabled older model is not listed")
gone = by.get("us.anthropic.claude-gone-v1")
check(gone and gone["superseded"] is False and "No longer offered" in gone["note"],
      "an enabled model Bedrock stopped returning says so")
check(by["global.anthropic.claude-haiku-4-5-20251001-v1:0"]["enabled"] is True, "the latest carries its enablement")
MC = (pathlib.Path(__file__).resolve().parents[2] / "admin-console/src/components/ModelCatalog.jsx").read_text()
check("superseded" in MC and "Global" in MC and "Regional" in MC, "the console badges global, regional and superseded")

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
