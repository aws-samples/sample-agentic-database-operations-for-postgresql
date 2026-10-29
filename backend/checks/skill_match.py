# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deterministic skill selection from an investigation prompt.

Maps a free-text investigation prompt to the single most relevant diagnostic
skill using the check registry's curated ``triggers`` (symptom vocabulary) and
its ``skill`` mapping. Pure stdlib + registry (no boto3/strands) so it is
unit-testable and safe to import anywhere.

Why triggers, not the skill's description prose: scoring skill *descriptions* by
word overlap mis-selects at scale — many skills share generic words ("storage",
"table", "index"), so the correct skill silently loses to a neighbour (observed:
a storage-bloat prompt selected `table-bloat-estimate`/`cache-hit-ratio` instead
of `vacuum-and-bloat`). Curated triggers are specific and largely disjoint, so a
trigger match is a far stronger signal and is deterministic + testable.
"""

import os
import logging

from checks.registry import all_checks, resolve_lookback_hours

logger = logging.getLogger("skill-match")


def select_skill(prompt: str, checks: dict = None):
    """Pick the best skill for a prompt.

    Returns ``(skill_slug, score, runner_up_slug)``. ``score`` is the number of
    trigger phrases that matched (aggregated across every check mapping to the
    same skill). Ties break toward the most specific match (greatest total
    matched-trigger character length), then alphabetical skill slug for
    determinism. Returns ``("", 0, "")`` when nothing matches.

    Matching is case-insensitive substring: a trigger phrase counts if it appears
    anywhere in the prompt.
    """
    if not prompt:
        return ("", 0, "")
    pl = prompt.lower()
    checks = all_checks() if checks is None else checks

    # Aggregate by SKILL (several checks can map to one skill, e.g. wraparound).
    agg = {}  # skill -> [hit_count, specificity]
    for slug, c in checks.items():
        skill = c.get("skill") or slug
        triggers = c.get("triggers") or []
        hits = [t for t in triggers if t and str(t).lower() in pl]
        if hits:
            a = agg.setdefault(skill, [0, 0])
            a[0] += len(hits)
            a[1] += sum(len(str(t)) for t in hits)

    if not agg:
        return ("", 0, "")

    # score desc, specificity desc, slug asc — fully deterministic.
    ranked = sorted(agg.items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0]))
    best_skill, (best_score, _) = ranked[0]
    runner = ranked[1][0] if len(ranked) > 1 else ""
    return (best_skill, best_score, runner)


def load_skill_body(skills_dir: str, skill_slug: str) -> str:
    """Return a skill's SKILL.md body with the YAML frontmatter stripped, or "".

    Reads the shipped skill body from the container."""
    if not skill_slug:
        return ""
    raw = None
    if raw is None:
        md_path = os.path.join(skills_dir, skill_slug, "SKILL.md")
        if not os.path.isfile(md_path):
            return ""
        try:
            raw = open(md_path, encoding="utf-8").read()
        except Exception:
            return ""
    # Frontmatter is delimited by the first two '---' markers.
    parts = raw.split("---", 2)
    return parts[2].strip() if len(parts) >= 3 else raw.strip()


# Category display order for the skill catalog — roughly the investigation flow
# (what's happening now -> performance -> health -> footprint -> optimization ->
# replication -> config). Categories not listed here fall to the end, alphabetical.
_CATALOG_CATEGORY_ORDER = [
    "CURRENT ACTIVITIES",
    "QUERY & I/O PERFORMANCE",
    "MAINTENANCE & HEALTH",
    "DATA FOOTPRINT",
    "OPTIMIZATION",
    "REPLICATION STATUS",
    "SYSTEM CONFIGURATION",
]


def _recommended_window(lookback_texts) -> str:
    """Most-specific recommended window label across a skill's checks, or "".

    Aggregates the raw ``investigation_lookback`` strings of the checks mapping to
    a skill: if any resolve to a fixed window, return the largest as ``Nd``/``Nh``;
    if they are all sentinels (no fixed clock window), return the most-specific
    label (``cumulative`` / ``growth-trend`` / ``point-in-time``). Returns "" when
    no check declares a lookback (nothing to recommend)."""
    texts = [str(t).strip() for t in (lookback_texts or []) if t and str(t).strip()]
    if not texts:
        return ""
    fixed = [h for h in (resolve_lookback_hours(t) for t in texts) if h is not None]
    if fixed:
        mx = max(fixed)
        return f"{mx // 24}d" if mx % 24 == 0 and mx >= 24 else f"{mx}h"
    joined = " ".join(texts).lower()
    if "cumulative" in joined or "stats_reset" in joined:
        return "cumulative"
    if "growth trend" in joined:
        return "growth-trend"
    return "point-in-time"


def build_skill_catalog(allowed_slugs=None, checks: dict = None, max_cues: int = 6) -> str:
    """Render a grouped, human-readable catalog of loadable diagnostic skills.

    Registry-driven: groups by check ``category``, dedupes by ``skill`` (several
    checks can map to one skill — their triggers are unioned), and gives each
    skill a short "use when" cue drawn from its ``triggers``. Intended for the
    investigation SOP / system prompt so the model can pick a skill by slug and
    call ``load_skill(slug)``.

    Args:
        allowed_slugs: optional iterable of skill slugs to include (surface
            scoping). ``None`` => every skill in the registry. Slugs absent from
            the registry are ignored. This is how a caller keeps the catalog to
            one surface (e.g. chat later passes only its chat-surface skills).
        checks: optional pre-loaded registry (for tests); defaults to all_checks().
        max_cues: max trigger phrases shown per skill.

    Returns:
        A markdown string grouped by category, or "" when nothing qualifies.
    """
    checks = all_checks() if checks is None else checks
    if not checks:
        return ""
    allow = set(allowed_slugs) if allowed_slugs is not None else None

    # skill -> {category, display_name, triggers (order-preserving, de-duped)}
    info = {}
    for slug, c in checks.items():
        skill = c.get("skill") or slug
        if allow is not None and skill not in allow:
            continue
        e = info.get(skill)
        if e is None:
            e = info[skill] = {
                "category": c.get("category") or "OTHER",
                "display_name": c.get("display_name") or skill,
                "triggers": [],
                "lookbacks": [],
            }
        for t in (c.get("triggers") or []):
            ts = str(t).strip()
            if ts and ts not in e["triggers"]:
                e["triggers"].append(ts)
        lb = c.get("investigation_lookback")
        if lb:
            e["lookbacks"].append(lb)
    if not info:
        return ""

    by_cat = {}
    for skill, e in info.items():
        by_cat.setdefault(e["category"], []).append((skill, e))

    ordered = [c for c in _CATALOG_CATEGORY_ORDER if c in by_cat]
    ordered += sorted(c for c in by_cat if c not in _CATALOG_CATEGORY_ORDER)

    out = []
    for cat in ordered:
        out.append(f"### {cat}")
        for skill, e in sorted(by_cat[cat], key=lambda kv: kv[0]):
            line = f"- **{skill}** — {e['display_name']}."
            cues = ", ".join(e["triggers"][:max_cues])
            if cues:
                line += f" Use when: {cues}."
            win = _recommended_window(e["lookbacks"])
            if win:
                line += f" Recommended window: {win}."
            out.append(line)
        out.append("")
    return "\n".join(out).strip()
