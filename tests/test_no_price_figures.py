#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Prices live in one place: the README's "Indicative costs" section.

A price written into a setting prompt, a template description or a code comment goes stale
silently and disagrees with the next one. So no price figure may appear in Python, YAML,
JS/JSX, shell, deploy.config.example, or any Markdown file, except between the README's
`## Indicative costs` heading and the next `## ` heading, where every row links the AWS
pricing page it came from. The budget input validation messages ("between 1 and
1,000,000 USD") are limits, not prices, and are allowed by exact text. The pcsr/ folder,
build output, dependencies and agent scratch space are out of scope.

A price figure is: `$` followed by a number and a rate or a decimal ($0.02, $18/month,
$7 per 1,000, $15 a month), an approximate amount (~$18, about $15), or an amount in USD
(USD 15, 15 USD). A shell positional parameter ($1) is none of these.

Run:  python3 tests/test_no_price_figures.py   (from the repository root)
"""
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "node_modules", "dist", "build", "__pycache__", ".agents", "pcsr", ".venv", "venv",
             ".pytest_cache", "cdk.out",
             # build output (gitignored): generated from the templates checked here
             "global-s3-assets", "regional-s3-assets",
             # Each deployment environment's build output (staged templates, built web
             # apps): generated, gitignored and minified. And the old configs' backup.
             ".state", "config-backup"}
SUFFIXES = {".py", ".yaml", ".yml", ".js", ".jsx", ".sh", ".md"}
EXTRA_FILES = {"deploy.config.example"}
SELF = pathlib.Path(__file__).resolve()

D = "\\$"   # built up so this file's own source does not match its patterns
PRICE = re.compile(
    D + r"\s?\d[\d,]*\.\d+"                                                  # $0.02
    + "|" + D + r"\s?\d[\d,]*\s?(/|per\b|a\s+(month|year)\b|an\s+hour\b|each\b|/mo\b)"  # $18/month, $7 per
    + r"|(~|\babout |\broughly |\baround )" + D + r"\s?\d"                   # ~$18, about $15
    + r"|\bUSD\s?\d|\d[\d,.]*\s?USD\b",                                        # USD 15, 15 USD
    re.I,
)
ALLOWED_LINES = (
    # Budget input validation: a limit, not a price.
    ("backend/app.py", "1 and 1,000,000 USD"),
    ("admin-console/src/components/Security.jsx", "1 and 1,000,000 USD"),
)

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = pathlib.Path(dirpath) / name
            if path.resolve() == SELF:
                continue
            if path.suffix in SUFFIXES or name in EXTRA_FILES:
                yield path


def readme_cost_section_lines(text):
    """Line numbers (1-based) inside README's ## Indicative costs section."""
    lines = text.splitlines()
    inside, out = False, set()
    for i, ln in enumerate(lines, 1):
        if ln.startswith("## "):
            inside = ln.strip() == "## Indicative costs"
        if inside:
            out.add(i)
    return out


print("══ no price figure outside the README's Indicative costs section ══")
hits = []
for path in files():
    rel = path.relative_to(ROOT).as_posix()
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    allowed_lines = readme_cost_section_lines(text) if rel == "README.md" else set()
    for n, line in enumerate(text.splitlines(), 1):
        if n in allowed_lines or not PRICE.search(line):
            continue
        if any(rel == f and snippet in line for f, snippet in ALLOWED_LINES):
            continue
        hits.append(f"{rel}:{n}: {line.strip()[:140]}")
check(not hits, "no price figures elsewhere" + ("".join(f"\n         {h}" for h in hits) if hits else ""))

print("")
print("══ the README section exists and every row cites its pricing page ══")
readme = (ROOT / "README.md").read_text(encoding="utf-8")
section = readme.split("\n## Indicative costs\n", 1)
check(len(section) == 2, "README has a '## Indicative costs' section")
body = section[1].split("\n## ", 1)[0] if len(section) == 2 else ""
check("Prices are indicative list prices for us-east-1 as of 2 October 2026." in body,
      "it states the Region and the date")
rows = [ln for ln in body.splitlines() if ln.startswith("|") and not re.match(r"^\|\s*-", ln)][1:]
check(len(rows) >= 5, f"it holds the cost table ({len(rows)} rows)")
missing = [r[:60] for r in rows if "https://aws.amazon.com/" not in r]
check(not missing, f"every row links an AWS pricing page {missing}")
for needle in ("VPC Lattice resource endpoint", "audit vault", "web search"):
    check(needle.lower() in body.lower(), f"it has a row for the {needle}")
check("\n## Troubleshooting" in readme.split("\n## Indicative costs\n", 1)[-1][:20000],
      "the section sits before Troubleshooting")

print("")
print("══ the patterns catch what they should ══")
for sample, expect in (("about $15 a month", True), ("~$18/mo", True), ("$0.02 per hour", True),
                       ("~$7 per 1,000 queries", True), ("about USD 15 per database", True),
                       ("echo $1 $2", False), ("costs extra", False), ("${AWS::Region}", False)):
    check(bool(PRICE.search(sample)) == expect, f"{sample!r} -> {'price' if expect else 'not a price'}")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
