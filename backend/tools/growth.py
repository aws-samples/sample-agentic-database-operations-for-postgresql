# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deterministic growth assessment from the Tier 1 metrics series.

Turns the Tier 1 history (get_historical_metrics) into a hard ``is_growing``
boolean + evidence, so growth is COMPUTED in code rather than eyeballed and
extrapolated by the model (this is what kills the "~1.2 GB/month" fabrication at
the source). Pure and stdlib-only — unit-
testable without boto3.
"""

# A change is only "growth" if it clears a noise floor over the whole window.
SIZE_GROWTH_PCT = 5.0    # database_size_mb grew > 5% across the window
DEAD_PCT_DELTA = 5.0     # worst-table dead_tuple_pct rose > 5 points across the window


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def compute_growth(metrics: list) -> dict:
    """Assess growth from the Tier 1 series (rows newest-first, as
    get_historical_metrics returns).

    Returns a dict with a hard ``is_growing`` boolean + the evidence that justifies
    it. Fail-soft: <2 points -> not growing ("insufficient history").
    """
    rows = [m for m in (metrics or []) if isinstance(m, dict)]
    if len(rows) < 2:
        return {"is_growing": False, "reason": "insufficient history (<2 data points)",
                "size_change_pct": None, "dead_pct_delta": None, "points": len(rows)}

    latest, oldest = rows[0], rows[-1]
    size_new, size_old = _num(latest.get("database_size_mb")), _num(oldest.get("database_size_mb"))
    dead_new, dead_old = _num(latest.get("dead_tuple_pct")), _num(oldest.get("dead_tuple_pct"))

    size_change_pct = None
    if size_new is not None and size_old not in (None, 0):
        size_change_pct = round((size_new - size_old) / size_old * 100.0, 2)
    dead_delta = None
    if dead_new is not None and dead_old is not None:
        dead_delta = round(dead_new - dead_old, 2)

    size_growing = size_change_pct is not None and size_change_pct > SIZE_GROWTH_PCT
    dead_growing = dead_delta is not None and dead_delta > DEAD_PCT_DELTA
    is_growing = bool(size_growing or dead_growing)

    reasons = []
    if size_growing:
        reasons.append(f"database size +{size_change_pct}% across the window")
    if dead_growing:
        reasons.append(f"worst-table dead tuples +{dead_delta} pts across the window")
    if not is_growing:
        reasons.append("database size and dead-tuple % are flat across the window (stable)")

    return {
        "is_growing": is_growing,
        "reason": "; ".join(reasons),
        "size_change_pct": size_change_pct,
        "dead_pct_delta": dead_delta,
        "points": len(rows),
        "window": {"from": oldest.get("scan_time"), "to": latest.get("scan_time")},
    }
