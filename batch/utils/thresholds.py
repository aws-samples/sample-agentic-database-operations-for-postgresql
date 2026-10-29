# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Batch-side threshold loader — reads the SAME backend/checks/references.yaml
the agent path uses, so the deterministic findings engine and the agentic tools
never fork the numbers.

Severity bands are keyed by human slug in references.yaml (catalog_id is a
provenance field, never a key). This module maps the batch engine's aggregate
model onto those bands.

Fail-soft: the batch image bundles references.yaml (Dockerfile COPY + Makefile
stages it from backend/checks). If it is somehow missing, we fall back to the
built-in defaults below — which mirror the shipped references values — and log a
warning, so a scan never crashes on a packaging gap and never silently reports a
false "healthy". The bundled YAML always wins when present.
"""

import functools
import logging
from pathlib import Path

logger = logging.getLogger("findings-engine.thresholds")

# Safety net — used ONLY when references.yaml is not bundled. Values mirror the
# shipped catalog so a degraded build stays aligned with the agent path.
_FALLBACK = {
    "cache_below": [{"name": "critical", "max": 95}, {"name": "warning", "max": 99}],
    "usage_min": [{"name": "critical", "min": 90}, {"name": "warning", "min": 80}, {"name": "notice", "min": 70}],
    "idle_min": [{"name": "critical", "min": 3600}, {"name": "warning", "min": 900}, {"name": "notice", "min": 300}],
    "xid_absolute": {"warning": 1000000000, "critical": 1500000000, "emergency": 1800000000},
    "bloat_size_bands": [
        {"name": "very_large", "min_bytes": 53687091200},
        {"name": "large", "min_bytes": 1073741824, "max_bytes": 53687091200},
        {"name": "small", "max_bytes": 1073741824},
    ],
    "bloat_levels": {
        "notice": {"small": 10, "large": 5, "very_large": 2},
        "warning": {"small": 20, "large": 10, "very_large": 5},
        "critical": {"small": 50, "large": 20, "very_large": 10},
    },
    "replay_lag_s": [{"name": "critical", "min": 30}, {"name": "warning", "min": 5}],
    "lag_bytes": [{"name": "critical", "min": 10737418240}, {"name": "warning", "min": 1073741824}],
    "recovery_conflicts": [{"name": "warning", "min": 1}],
    "session_age_s": [{"name": "critical", "min": 86400}, {"name": "warning", "min": 3600}],
    "managed_roles": ["rdsadmin", "rdsproxyadmin", "rdsrepladmin", "rdswriteforwarduser",
                      "rds_superuser", "rdstopmgr"],
}


def _search_paths():
    here = Path(__file__).resolve()
    return [
        here.parent.parent / "references.yaml",                        # /app/references.yaml (container)
        here.parents[2] / "backend" / "checks" / "references.yaml",    # repo layout (local dev/tests)
    ]


@functools.lru_cache(maxsize=1)
def _refs() -> dict:
    for p in _search_paths():
        try:
            if p.is_file():
                import yaml
                with open(p, encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
        except Exception as e:  # pragma: no cover - fail-soft
            logger.warning(f"threshold catalog load failed at {p}: {e}")
    logger.warning("references.yaml not bundled — using built-in fallback thresholds "
                   "(batch findings may lag the agent path until the catalog is packaged)")
    return {}


def _thresholds(slug: str) -> dict:
    return (_refs().get(slug) or {}).get("thresholds", {}) or {}


def managed_roles() -> frozenset:
    """AWS-managed role names (references.yaml#role-audit), lowercased. RDS reserves them
    and the customer cannot act on them, so they are never a role-risk finding — the same
    list the agent's security audit (CHK-042) skips."""
    names = _thresholds("role-audit").get("managed_roles") or _FALLBACK["managed_roles"]
    return frozenset(str(n).lower() for n in names)


# ── generic band classifiers ──────────────────────────────────────────────

def _classify_min(bands, value):
    """min bands: value >= min => that band. Returns the highest matching band."""
    if value is None or not bands:
        return None
    best, best_min = None, None
    for b in bands:
        mn = b.get("min")
        if mn is None:
            continue
        if value >= mn and (best_min is None or mn > best_min):
            best, best_min = b["name"], mn
    return best.upper() if best else None


def _classify_below(bands, value):
    """percent_below bands: value < max => that band. Returns the strictest match."""
    if value is None or not bands:
        return None
    best, best_max = None, None
    for b in bands:
        mx = b.get("max")
        if mx is None:
            continue
        if value < mx and (best_max is None or mx < best_max):
            best, best_max = b["name"], mx
    return best.upper() if best else None


def _size_class(total_bytes, size_bands):
    """Which size band a table falls in (very_large / large / small), read from YAML."""
    tb = total_bytes or 0
    best, best_min = "small", -1
    for b in size_bands:
        mn = b.get("min_bytes", 0)
        mx = b.get("max_bytes")
        if tb >= mn and (mx is None or tb < mx) and mn >= best_min:
            best, best_min = b["name"], mn
    return best


# ── check-specific severity (shared numbers, batch-shaped inputs) ──────────

def cache_hit_severity(ratio):
    """database-cache-hit (CHK-032, percent_below): a low hit ratio is worse."""
    bands = _thresholds("database-cache-hit").get("bands") or _FALLBACK["cache_below"]
    return _classify_below(bands, ratio)


def connection_usage_severity(usage_pct):
    """current-activity-snapshot (CHK-037): connection utilisation bands."""
    bands = _thresholds("current-activity-snapshot").get("usage_pct_bands") or _FALLBACK["usage_min"]
    return _classify_min(bands, usage_pct)


def idle_connection_severity(idle_seconds):
    """idle-connections (CHK-013): a plain-idle session's slot-waste severity by age."""
    bands = _thresholds("idle-connections").get("bands") or _FALLBACK["idle_min"]
    return _classify_min(bands, idle_seconds)


def xid_severity(age_xids):
    """xid-wraparound-database (CHK-001): absolute XID-age floors. The batch has the
    raw age (and a pct of the 2.1B hard max); we classify on the raw age against the
    same absolute floors the agent path uses, so the two never disagree. EMERGENCY
    collapses into CRITICAL for the batch's 3-level health score."""
    if age_xids is None:
        return None
    ab = _thresholds("xid-wraparound-database").get("absolute") or _FALLBACK["xid_absolute"]
    if age_xids >= ab.get("critical", _FALLBACK["xid_absolute"]["critical"]):
        return "CRITICAL"
    if age_xids >= ab.get("warning", _FALLBACK["xid_absolute"]["warning"]):
        return "WARNING"
    return None


def replication_lag_severity(seconds):
    """replication-health (CHK-034): a reader's replay lag in seconds."""
    bands = _thresholds("replication-health").get("replay_lag_seconds_bands") or _FALLBACK["replay_lag_s"]
    return _classify_min(bands, seconds)


def replication_bytes_severity(lag_bytes):
    """replication-health (CHK-034): WAL received but not yet replayed, in bytes."""
    bands = _thresholds("replication-health").get("lag_bytes_bands") or _FALLBACK["lag_bytes"]
    return _classify_min(bands, lag_bytes)


def recovery_conflicts_severity(total):
    """replication-health (CHK-034): queries a reader cancelled for recovery conflicts."""
    bands = (_thresholds("replication-health").get("recovery_conflicts_bands")
             or _FALLBACK["recovery_conflicts"])
    return _classify_min(bands, total)


def session_age_severity(seconds):
    """long-lived-sessions (CHK-024): how long a session or query has run."""
    bands = _thresholds("long-lived-sessions").get("bands") or _FALLBACK["session_age_s"]
    return _classify_min(bands, seconds)


def bloat_severity(dead_pct, total_bytes=None, autovacuum_disabled=False):
    """vacuum-and-bloat (CHK-007, size_weighted): larger tables escalate at a lower
    dead-tuple %. autovacuum_enabled=false is CRITICAL regardless of %."""
    thr = _thresholds("vacuum-and-bloat")
    size_bands = thr.get("size_bands") or _FALLBACK["bloat_size_bands"]
    levels = thr.get("levels") or _FALLBACK["bloat_levels"]
    hard = thr.get("hard_criticals") or ["autovacuum_disabled"]
    if autovacuum_disabled and "autovacuum_disabled" in hard:
        return "CRITICAL"
    if dead_pct is None:
        return None
    cls = _size_class(total_bytes, size_bands)
    for level in ("critical", "warning", "notice"):
        floor = (levels.get(level) or {}).get(cls)
        if floor is not None and dead_pct >= floor:
            return level.upper()
    return None
