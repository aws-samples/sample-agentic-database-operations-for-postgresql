#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Guard: every status colour in the two SPAs resolves through a theme token.

Both SPAs theme themselves the same way: components keep using plain Tailwind
utilities, and `index.css` maps those utilities onto per-theme CSS variables. Add
a theme, and every component follows automatically.

Status colours (amber = needs attention, green = ok, red = failed, blue = info)
were the exception. Tailwind's `-50`/`-100` shades are mixed for white paper, so
on a dark theme `hover:bg-amber-50/50` painted a near-white bar across the row
and `text-amber-700` wrote dark brown on top of it - which is what made the
discovered-databases table unreadable in the dark themes.

The fix was tokens (`--warn-bg`, `--ok-text`, ...) with a mapping per theme. This
test is what keeps it true: a contributor who reaches for a shade nobody has
mapped yet gets a failure here rather than an unreadable row six months later.

What counts as needing a token - an *opaque* colour that assumes a light surface:
  background   -50, -100          a wash behind content
  border       -200, -300         a hairline against a wash
  divide       -100, -200         the same, between table rows
  text         -600 .. -900       dark ink, legible only on a pale background

What is deliberately left literal, and why:
  any `/alpha` shade  already translucent, so it tints whatever is beneath it
  `dark:` variants    only apply on dark themes; harmless where they survive
  -400, -500 text     mid tones that read on both pale surfaces and the dark top bar
  -500, -600 solid    fills that carry white text (badges, dots), same in every theme

Run:  python3 tests/test_theme_tokens.py
"""
import collections
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SPAS = ("admin-console", "frontend")
HUES = ("amber", "yellow", "orange", "green", "emerald", "red", "rose", "blue")

passed = failed = 0


def check(cond: bool, label: str) -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}")


UTILITY = re.compile(
    r"(?P<variant>(?:dark|hover|focus|group-hover|active|disabled):)?"
    r"(?P<prop>bg|text|border|divide|ring)-"
    r"(?P<hue>" + "|".join(HUES) + r")-"
    r"(?P<shade>\d{2,3})"
    r"(?P<alpha>/\d{1,3})?"
)

# Opaque shades that assume a light surface. Anything here must reach a token.
NEEDS_TOKEN = {
    "bg": {"50", "100"},
    "border": {"200", "300"},
    "divide": {"100", "200"},
    "text": {"600", "700", "800", "900"},
}


def needs_token(m: re.Match) -> bool:
    if m.group("alpha"):
        return False                      # translucent: adapts to any surface
    if m.group("variant") == "dark:":
        return False                      # only applies where the surface is dark
    return m.group("shade") in NEEDS_TOKEN.get(m.group("prop"), set())


def selector_for(cls: str) -> str:
    """The CSS selector Tailwind emits for a utility, e.g.

    hover:bg-amber-50/50  ->  .hover\\:bg-amber-50\\/50:hover
    """
    variant, _, base = cls.rpartition(":")
    escaped = base.replace("/", r"\/")
    if variant:
        return "." + variant.replace(":", r"\:") + r"\:" + escaped + f":{variant}"
    return "." + escaped


def is_mapped(css: str, cls: str) -> bool:
    """True when index.css has a rule whose selector list includes this utility.

    Matched textually rather than by parsing: the selector may be followed by a
    comma (grouped), a brace, or ` >` (the `divide-*` child combinator).
    """
    sel = selector_for(cls)
    return re.search(re.escape(sel) + r"\s*(?:,|\{|>)", css) is not None


def used_utilities(app: str) -> collections.Counter:
    found: collections.Counter = collections.Counter()
    for path in sorted((REPO / app / "src").rglob("*.jsx")):
        for m in UTILITY.finditer(path.read_text(encoding="utf-8")):
            found[m.group(0)] += 1
    return found


print("== every opaque status colour reaches a theme token ==")
for app in SPAS:
    css = (REPO / app / "src" / "index.css").read_text(encoding="utf-8")
    gaps = sorted(
        cls for cls in used_utilities(app)
        if needs_token(UTILITY.fullmatch(cls)) and not is_mapped(css, cls)
    )
    check(not gaps, f"{app}: no unmapped status washes"
                    + (f" - found {', '.join(gaps)}" if gaps else ""))

print()
print("== the tokens themselves are defined for every theme ==")
# A mapping that references a token no theme defines silently resolves to nothing,
# which renders as transparent - the failure this catches is an invisible wash.
TOKEN = re.compile(r"var\((--(?:warn|ok|err|info)-[a-z0-9-]+)\)")
THEME_BLOCK = re.compile(r"\[data-theme=\"([a-z-]+)\"\]\s*\{(.*?)\n\}", re.S)

RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
STATUS_DECL = re.compile(r"(--(?:warn|ok|err|info)-[a-z0-9-]+)\s*:")
THEME_SEL = re.compile(r"^\[data-theme=\"([a-z-]+)\"\]$")


def theme_tokens(css: str) -> dict:
    """The status tokens each theme resolves, as the browser cascades them: what ``:root``
    declares (every theme starts from it), plus every rule whose selector list names the
    theme on its own. A mode that only overrides surfaces (Graphite on Dark, Paper on
    Light) inherits the status tokens through a grouped selector or ``:root``, so reading
    each block alone would report them missing when they are not. Accent sub-rules
    (``[data-theme="x"][data-accent="y"]``) are not a theme of their own."""
    base, themes = set(), {}
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)   # a comment is not part of a selector
    for selectors, body in RULE.findall(css):
        declared = set(STATUS_DECL.findall(body))
        for sel in (x.strip() for x in selectors.split(",")):
            if sel == ":root":
                base |= declared
            m = THEME_SEL.match(sel)
            if m:
                themes.setdefault(m.group(1), set()).update(declared)
    return {name: decl | base for name, decl in themes.items()}


for app in SPAS:
    css = (REPO / app / "src" / "index.css").read_text(encoding="utf-8")
    referenced = set(TOKEN.findall(css))
    themes = theme_tokens(css)
    # Light and Dark carry the full palettes; Graphite and Paper are background modes
    # on top of them. "System" follows the OS and is not a block of its own. The
    # invariant that matters is the per-theme token completeness checked below.
    check(len(themes) >= 2, f"{app}: found {len(themes)} themes ({', '.join(sorted(themes))})")
    for theme, declared in sorted(themes.items()):
        missing = sorted(referenced - declared)
        check(not missing, f"{app} / {theme}: defines every status token"
                           + (f" - missing {', '.join(missing)}" if missing else ""))

print()
print("== light gradient stops are themed too ==")
# Gradient utilities set --tw-gradient-* custom properties, not background-color,
# so the mappings above do not reach them. A `from-slate-50 to-white` bar stays
# white on a dark theme while the text on it flips to near-white - which is
# exactly how the report header's title became invisible.
GRADIENT = re.compile(r"\b(?:from|via|to)-(?:white|slate-(?:50|100)|gray-(?:50|100))\b")
for app in SPAS:
    css = (REPO / app / "src" / "index.css").read_text(encoding="utf-8")
    stops = set()
    for path in sorted((REPO / app / "src").rglob("*.jsx")):
        stops.update(GRADIENT.findall(path.read_text(encoding="utf-8")))
    missing = sorted(s for s in stops if s not in css)
    check(not missing, f"{app}: every light gradient stop is themed"
                       + (f" - unthemed: {', '.join(missing)}" if missing
                          else f" ({len(stops) or 'none'} in use)"))

print()
print("== both SPAs stay in step ==")
# They are separate apps with separate stylesheets, and a token added to one and
# not the other is the kind of drift nobody notices until a screenshot looks wrong.
sets = {}
for app in SPAS:
    css = (REPO / app / "src" / "index.css").read_text(encoding="utf-8")
    body = THEME_BLOCK.search(css)
    sets[app] = set(re.findall(r"(--(?:warn|ok|err|info)-[a-z0-9-]+)\s*:", css))
only_admin = sorted(sets["admin-console"] - sets["frontend"])
only_front = sorted(sets["frontend"] - sets["admin-console"])
check(not only_admin and not only_front,
      "the two stylesheets declare the same status tokens"
      + (f" - admin-only: {only_admin}" if only_admin else "")
      + (f" - frontend-only: {only_front}" if only_front else ""))

print()
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
