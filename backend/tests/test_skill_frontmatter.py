# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Guard: every SKILL.md must have valid YAML frontmatter.

A single malformed frontmatter (e.g. an unquoted colon in `description:`) makes
the Strands AgentSkills plugin fail to load skills — silently, at runtime — which
is what broke progressive disclosure / the skill "pill" in UAT. This test fails
loudly in CI instead, for the whole skill catalog.

Run:  python3 backend/tests/test_skill_frontmatter.py   (or via pytest)
"""
import glob
import os

import yaml

_SKILLS_DIR = os.path.join(os.path.dirname(__file__), "..", "skills")
_VALID_SURFACES = {"chat", "investigation", "both"}


def _skill_files():
    return sorted(glob.glob(os.path.join(_SKILLS_DIR, "*", "SKILL.md")))


def test_there_are_skills():
    assert _skill_files(), "no SKILL.md files found"


def test_every_frontmatter_is_valid_yaml():
    failures = []
    for md in _skill_files():
        name = os.path.basename(os.path.dirname(md))
        raw = open(md, encoding="utf-8").read()
        parts = raw.split("---", 2)
        if len(parts) < 3:
            failures.append(f"{name}: missing --- frontmatter block")
            continue
        try:
            fm = yaml.safe_load(parts[1])
        except Exception as e:  # noqa: BLE001
            failures.append(f"{name}: YAML error: {str(e).splitlines()[0]}")
            continue
        if not isinstance(fm, dict):
            failures.append(f"{name}: frontmatter is not a mapping")
            continue
        if not fm.get("name"):
            failures.append(f"{name}: missing 'name'")
        if not fm.get("description"):
            failures.append(f"{name}: missing 'description'")
        surface = str(((fm.get("metadata") or {}).get("surface", "investigation"))).lower()
        if surface not in _VALID_SURFACES:
            failures.append(f"{name}: invalid surface {surface!r}")
    assert not failures, "Invalid SKILL.md frontmatter:\n  " + "\n  ".join(failures)


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", fn.__name__, "-", e)
    print("\n%d passed, %d failed" % (len(fns) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    sys.exit(_run())
