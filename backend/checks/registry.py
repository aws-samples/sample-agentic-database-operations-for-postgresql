# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Check registry loader — the router/workflow catalog (registry.yaml).

Keyed by human **slug** (primary identity). `catalog_id` (e.g. CHK-NNN) is a
stable traceability id for following a check across revisions and is NEVER
surfaced to users — use `display_name`. Thresholds live in references.yaml.
Pure YAML + stdlib (no boto3/strands) so it is unit-testable
and safe to import anywhere. All reads are fail-soft (empty registry on error).
"""

import functools
import logging
import re
from pathlib import Path

logger = logging.getLogger("check-registry")

_REGISTRY_PATH = Path(__file__).parent / "registry.yaml"

# Small fallback window (hours) for a check whose lookback is empty/unknown.
_DEFAULT_LOOKBACK_HOURS = 2

# Free-text lookbacks that describe NO fixed clock window — point-in-time catalog
# state, cumulative counters (stats_reset age), growth trends, or on-demand. These
# must be narrated as such, never as an N-hour window.
_NO_WINDOW_MARKERS = (
    "point-in-time",
    "catalog",
    "cumulative",
    "stats_reset",
    "growth trend",
    "on demand",
    "on-demand",
)


@functools.lru_cache(maxsize=1)
def _load() -> dict:
    try:
        import yaml
        with open(_REGISTRY_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("checks", {}) or {}
    except Exception as e:  # pragma: no cover - fail-soft
        logger.warning(f"check registry load failed: {e}")
        return {}


def all_checks() -> dict:
    """All registered checks, keyed by slug."""
    return dict(_load())


def get_check(slug: str) -> dict:
    """One check's entry by slug (empty dict if unknown)."""
    return dict(_load().get(slug, {}))


def display_name(slug: str) -> str:
    """User-facing name for a slug (falls back to the slug)."""
    return _load().get(slug, {}).get("display_name", slug)


def resolve_lookback_hours(investigation_lookback: str):
    """Normalize a check's free-text ``investigation_lookback`` into hours.

    Returns:
        - ``N * 24`` when the text names an N-day window (e.g. "30 days" -> 720);
        - ``N`` when it names an N-hour window;
        - ``None`` for point-in-time / cumulative / stats_reset / growth-trend /
          on-demand lookbacks (no fixed clock window — narrate as such,);
        - a small default (``2``) for empty/unknown text.

    Sentinels are checked BEFORE the day/hour parse so a cumulative lookback that
    also mentions a short IO-context window (e.g. "stats_reset age (cumulative);
    3 days for write-IO context") correctly resolves to ``None``.
    """
    text = (investigation_lookback or "").strip().lower()
    if not text:
        return _DEFAULT_LOOKBACK_HOURS
    for marker in _NO_WINDOW_MARKERS:
        if marker in text:
            return None
    m = re.search(r"(\d+)\s*d(?:ay)?s?\b", text)
    if m:
        return int(m.group(1)) * 24
    m = re.search(r"(\d+)\s*h(?:our)?s?\b", text)
    if m:
        return int(m.group(1))
    return _DEFAULT_LOOKBACK_HOURS


def check_lookback_hours(slug: str):
    """Resolved default lookback (hours | None) for a check by slug."""
    return resolve_lookback_hours(get_check(slug).get("investigation_lookback", ""))
