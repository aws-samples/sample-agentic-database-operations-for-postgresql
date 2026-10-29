# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for deterministic skill selection (checks.skill_match).

Locks in the fix for the "wrong skill at scale" bug: description-word overlap
selected the wrong skill (e.g. a storage-bloat prompt -> table-bloat-estimate /
cache-hit-ratio). Registry-trigger matching must select the correct skill.

Pure logic (registry YAML + filesystem) — no AWS/app deps.

Run:  python3 backend/tests/test_skill_match.py   (or via pytest)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from checks.skill_match import select_skill, load_skill_body, build_skill_catalog  # noqa: E402

_SKILLS_DIR = os.path.join(os.path.dirname(__file__), "..", "skills")


def test_storage_bloat_prompt_selects_vacuum_and_bloat():
    # The exact failing case from the 08072026-2 UAT trace.
    prompt = ("Storage growth on aurora-pg-database-1 - Storage is increasing; need to "
              "correlate table bloat, vacuum health, dead tuples, and storage over time")
    skill, score, runner = select_skill(prompt)
    assert skill == "vacuum-and-bloat", f"got {skill!r} (runner-up {runner!r})"
    assert score >= 2  # multiple trigger hits, decisive


def test_cache_prompt_selects_cache_hit_ratio():
    prompt = "cache hit ratio is low, buffer cache misses, reads going to disk after data grew"
    skill, _, _ = select_skill(prompt)
    assert skill == "cache-hit-ratio", f"got {skill!r}"


def test_lock_prompt_selects_lock_contention():
    prompt = "lock contention with blocked queries and a blocking chain, queries stuck"
    skill, _, _ = select_skill(prompt)
    assert skill == "lock-contention", f"got {skill!r}"


def test_connection_prompt_selects_connection_health():
    prompt = "connection exhaustion — too many idle connections wasting slots"
    skill, _, _ = select_skill(prompt)
    assert skill == "connection-health", f"got {skill!r}"


def test_irrelevant_prompt_matches_nothing():
    skill, score, runner = select_skill("the weather is pleasant today")
    assert skill == "" and score == 0 and runner == ""


def test_empty_prompt_matches_nothing():
    assert select_skill("") == ("", 0, "")
    assert select_skill(None) == ("", 0, "")


def test_selection_is_deterministic():
    p = "table bloat and dead tuples, autovacuum not keeping up"
    assert select_skill(p) == select_skill(p)


def test_load_skill_body_strips_frontmatter():
    body = load_skill_body(_SKILLS_DIR, "vacuum-and-bloat")
    assert body, "expected non-empty body"
    # Frontmatter keys must not leak into the injected body.
    assert "surface:" not in body
    assert "allowed-tools:" not in body
    # Real content is present.
    assert "Vacuum" in body or "vacuum" in body


def test_load_skill_body_missing_returns_empty():
    assert load_skill_body(_SKILLS_DIR, "no-such-skill") == ""
    assert load_skill_body(_SKILLS_DIR, "") == ""


# ── build_skill_catalog (the skills_catalog get_runbooks serves + load_skill scoping) ──

# Synthetic registry: two checks map to one skill (dedupe + trigger union), a
# second skill in another category (grouping/order). Keeps structure tests
# independent of registry churn.
_SYNTH = {
    "chk-a": {"skill": "alpha-skill", "category": "MAINTENANCE & HEALTH",
              "display_name": "Alpha", "triggers": ["aaa", "bloat"]},
    "chk-a2": {"skill": "alpha-skill", "category": "MAINTENANCE & HEALTH",
               "display_name": "Alpha (table)", "triggers": ["bbb", "bloat"]},
    "chk-b": {"skill": "beta-skill", "category": "OPTIMIZATION",
              "display_name": "Beta", "triggers": ["ccc"]},
}


def test_catalog_real_registry_has_all_categories():
    cat = build_skill_catalog()
    for header in ["CURRENT ACTIVITIES", "QUERY & I/O PERFORMANCE", "MAINTENANCE & HEALTH",
                   "DATA FOOTPRINT", "OPTIMIZATION", "REPLICATION STATUS", "SYSTEM CONFIGURATION"]:
        assert f"### {header}" in cat, f"missing category {header!r}"


def test_catalog_real_registry_lists_known_skills():
    cat = build_skill_catalog()
    for slug in ["vacuum-and-bloat", "index-hygiene", "cache-hit-ratio", "replication-health"]:
        assert f"**{slug}**" in cat, f"missing skill {slug!r}"


def test_catalog_index_hygiene_under_optimization():
    cat = build_skill_catalog()
    blocks, cur = {}, None
    for line in cat.splitlines():
        if line.startswith("### "):
            cur = line[4:].strip()
            blocks[cur] = []
        elif cur is not None:
            blocks[cur].append(line)
    assert any("**index-hygiene**" in l for l in blocks.get("OPTIMIZATION", []))


def test_catalog_includes_use_when_cues():
    cat = build_skill_catalog()
    line = next(l for l in cat.splitlines() if "**vacuum-and-bloat**" in l)
    assert "Use when:" in line and "bloat" in line


def test_catalog_allowed_slugs_scopes_surface():
    # Surface scoping: only the permitted skill (and its category) appears — the
    # same guard load_skill uses so a surface can't reach another surface's skills.
    cat = build_skill_catalog(allowed_slugs={"vacuum-and-bloat"})
    assert "**vacuum-and-bloat**" in cat
    assert "**index-hygiene**" not in cat
    assert cat.count("### ") == 1


def test_catalog_synthetic_groups_and_dedupes():
    cat = build_skill_catalog(checks=_SYNTH)
    assert cat.index("### MAINTENANCE & HEALTH") < cat.index("### OPTIMIZATION")
    assert cat.count("**alpha-skill**") == 1  # deduped across its two checks
    line = next(l for l in cat.splitlines() if "**alpha-skill**" in l)
    assert "aaa" in line and "bbb" in line and "bloat" in line  # triggers unioned


def test_catalog_empty_registry_returns_empty():
    assert build_skill_catalog(checks={}) == ""


def test_catalog_max_cues_caps_triggers():
    checks = {"c": {"skill": "s", "category": "OPTIMIZATION", "display_name": "S",
                    "triggers": ["t1", "t2", "t3", "t4"]}}
    line = next(l for l in build_skill_catalog(checks=checks, max_cues=2).splitlines()
                if "**s**" in l)
    assert "t1" in line and "t2" in line and "t3" not in line


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", fn.__name__, "-", e or "assertion failed")
    print("\n%d passed, %d failed" % (len(fns) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
