# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bedrock model discovery for model governance.

Lists the inference profiles available in the deployment's account and region and
offers only the LATEST Claude Haiku, Sonnet and Opus, each as its ``global.*`` profile
and its regional (geo) profile for this region (``us.*``, ``eu.*``, …), whichever
Bedrock returns. Cached with a TTL; ``force=True`` bypasses it.

Fail-soft: on a Bedrock error, returns the last successful cache (or an empty
catalog), never raises — so the catalog endpoint degrades to a "stale" indicator
rather than a 500, and request-time resolution keeps working off stored config.

Discovery is the "discovered" layer of the cascade: a model appearing here is only
*visible* to the admin; it does nothing until explicitly enabled (see governance_store).
"""

import os
import re
import time
import logging
from typing import Optional

import boto3

logger = logging.getLogger("model-discovery")

_TTL_SECONDS = int(os.getenv("MODEL_DISCOVERY_TTL", "21600"))  # 6h
_cache = {"models": [], "ts": 0.0, "ok": False}

# Advisory metadata per family (recommended + cost tier), so a new release inherits it.
# NOT a gate — a discovered model is usable once an admin enables it, badged or not.
_SEED_META = {
    "haiku":  {"recommended": True, "cost_tier": "low"},
    "sonnet": {"recommended": True, "cost_tier": "mid"},
    "opus":   {"recommended": True, "cost_tier": "high"},
}


def _seed_for(model_id: str) -> dict:
    info = parse_model_id(model_id)
    return dict(_SEED_META.get(info["family"], {})) if info else {}


def _bedrock():
    return boto3.client(
        "bedrock",
        region_name=os.getenv("BEDROCK_REGION", os.getenv("AWS_REGION", "us-east-1")),
    )


# The three Claude families the catalog offers. Only the LATEST model of each is listed,
# as its global inference profile and its regional (geo) profile for this deployment's
# region, whichever Bedrock returns. "Latest" is derived from the ids Bedrock lists, never
# hard-coded, so the catalog follows new releases without a code change.
FAMILIES = ("haiku", "sonnet", "opus")
FAMILY_ORDER = {f: i for i, f in enumerate(FAMILIES)}
_FAMILY_LABEL = {"haiku": "Claude Haiku", "sonnet": "Claude Sonnet", "opus": "Claude Opus"}


def parse_model_id(model_id: str) -> Optional[dict]:
    """Split an Anthropic inference-profile id into its parts, or None when it is not a
    Haiku / Sonnet / Opus model.

    Handles both naming schemes Bedrock uses: ``<scope>.anthropic.claude-<family>-<v>-<v>-
    <date>-v1:0`` (claude-sonnet-4-5-20250929) and the older ``claude-<v>-<v>-<family>-
    <date>`` (claude-3-7-sonnet-20250219). ``scope`` is the part before ``anthropic.``:
    ``global`` or a geo prefix (``us``, ``eu``, ``apac``, …), '' when absent. ``version``
    is a tuple of the version numbers then the date, so tuples compare newest-last."""
    mid = str(model_id or "")
    if "anthropic.claude-" not in mid:
        return None
    scope, _, rest = mid.partition("anthropic.")
    scope = scope.rstrip(".")
    body = rest[len("claude-"):]
    body = body.split(":", 1)[0]
    tokens = [t for t in body.split("-") if t]
    # Drop the trailing "v1"-style revision marker.
    if tokens and re.fullmatch(r"v\d+", tokens[-1]):
        tokens = tokens[:-1]
    family = next((t for t in tokens if t in FAMILIES), None)
    if not family:
        return None
    date = next((t for t in tokens if re.fullmatch(r"\d{8}", t)), "")
    nums = [int(t) for t in tokens if re.fullmatch(r"\d{1,2}", t)]
    if not nums:
        return None
    # Version numbers padded to a fixed width before the date, so 4 (claude-sonnet-4-
    # 20250514) compares as 4.0.0 and sorts below 4.5, rather than its date outranking 5.
    padded = tuple((nums + [0, 0, 0])[:3])
    return {"scope": scope, "family": family, "base": rest, "version": padded + (int(date or 0),),
            "version_label": ".".join(str(n) for n in nums)}


def _discover() -> list:
    client = _bedrock()

    # Inference profiles are what we actually invoke with (global.* / us.* / eu.* …).
    # Listed in this deployment's region, so the regional profiles returned are this
    # region's geo (eu.* in Ireland, us.* in Virginia), never another geo's.
    profiles = []
    token = None
    while True:
        kwargs = {"maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        resp = client.list_inference_profiles(**kwargs)
        profiles.extend(resp.get("inferenceProfileSummaries", []))
        token = resp.get("nextToken")
        if not token:
            break
    return select_latest(profiles)


def select_latest(profiles: list) -> list:
    """From raw ``inferenceProfileSummaries``, the catalog entries: per family the latest
    base model only, as up to two entries — its ``global`` profile and its regional
    profile(s) — each one Bedrock actually returned (an id is never invented). Ordered
    Haiku, Sonnet, Opus, global before regional."""
    parsed = []
    for p in profiles or []:
        pid = p.get("inferenceProfileId", "")
        info = parse_model_id(pid)
        # Keep to the Claude family — our prompts/SOPs are tuned for Claude tool use.
        if not info:
            continue
        parsed.append((pid, p, info))
    latest: dict = {}
    for pid, _p, info in parsed:
        cur = latest.get(info["family"])
        if cur is None or info["version"] > cur:
            latest[info["family"]] = info["version"]
    models = []
    for pid, p, info in parsed:
        if info["version"] != latest.get(info["family"]):
            continue
        scope_kind = "global" if info["scope"] == "global" else "regional"
        label = f"{_FAMILY_LABEL[info['family']]} {info['version_label']}"
        where = "global" if scope_kind == "global" else (f"regional · {info['scope'].upper()}" if info["scope"] else "regional")
        models.append({
            "model_id": pid,
            "display_name": f"{label} ({where})",
            "provider": "anthropic",
            "discovered": True,
            "family": info["family"],
            "scope": scope_kind,
            "geo": info["scope"],
            "version": info["version_label"],
            **_seed_for(pid),
        })
    models.sort(key=lambda m: (FAMILY_ORDER[m["family"]], m["scope"] != "global", m["model_id"]))
    return models


def is_superseded(model_id: str, latest_models: list) -> bool:
    """True when ``model_id`` is a Haiku / Sonnet / Opus model older than the latest of
    its family in ``latest_models`` (what the catalog lists now)."""
    info = parse_model_id(model_id)
    if not info:
        return False
    for m in latest_models or []:
        cur = parse_model_id(m.get("model_id", ""))
        if cur and cur["family"] == info["family"]:
            return info["version"] < cur["version"]
    return False


def discover_models(force: bool = False) -> dict:
    """Return ``{models: [...], refreshed_at, stale}``; fail-soft to last cache."""
    now = time.time()
    if not force and _cache["ok"] and (now - _cache["ts"] < _TTL_SECONDS):
        return {"models": _cache["models"], "refreshed_at": _cache["ts"], "stale": False}
    try:
        models = _discover()
        _cache.update({"models": models, "ts": now, "ok": True})
        return {"models": models, "refreshed_at": now, "stale": False}
    except Exception as e:
        logger.warning(f"discover_models failed: {e}; serving cache (ok={_cache['ok']})")
        return {"models": _cache["models"], "refreshed_at": _cache["ts"], "stale": True}
