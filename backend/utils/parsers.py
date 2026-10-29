# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Utility functions for parsing agent markdown output into structured data.

Used by the /complete endpoint to extract findings and sections from
investigation agent output.
"""


def _extract_findings(text):
    """Extract severity-tagged findings from agent output, grouped by database."""
    findings = []
    current_db = ""

    # Isolate the FINDINGS section: everything from the FINDINGS header to the next
    # SECTION header (ROOT CAUSE / RECOMMENDED ACTIONS / SUMMARY), NOT to the next
    # per-database sub-header. The SOP organises each section by database with ###
    # headers at the same level as the ### FINDINGS header, so splitting on every
    # #/##/### severed FINDINGS from its own per-database entries and left it holding
    # only the word "FINDINGS" — the investigation then reported zero findings.
    import re
    _OTHER_SECTIONS = ("ROOT CAUSE", "RECOMMENDED ACTIONS", "RECOMMENDATIONS",
                       "SUMMARY", "EXECUTIVE SUMMARY", "NEXT STEPS")
    header_re = re.compile(r'(?m)^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$')
    headers = list(header_re.finditer(text))
    findings_text = ""
    for i, m in enumerate(headers):
        name = m.group(1).strip().strip("*").strip().upper()
        if name.startswith("FINDINGS"):
            end = len(text)
            for nxt in headers[i + 1:]:
                nxt_name = nxt.group(1).strip().strip("*").strip().upper()
                if any(nxt_name.startswith(o) for o in _OTHER_SECTIONS):
                    end = nxt.start()
                    break
            findings_text = text[m.end():end]
            break

    if not findings_text:
        findings_text = text  # fallback: scan entire text

    for line in findings_text.split("\n"):
        stripped = line.strip()

        # Track database headers — multiple formats the LLM might use:
        # ### database-name
        # ### **database-name**
        # **database-name**  (bold on its own line)
        # #### database-name
        if stripped.startswith("### ") or stripped.startswith("#### "):
            current_db = stripped.lstrip("#").strip().strip("*").strip()
            continue
        # Bold text on its own line (not a finding line)
        if stripped.startswith("**") and stripped.endswith("**") and ":" not in stripped:
            candidate = stripped.strip("*").strip()
            if candidate and len(candidate) < 100:
                current_db = candidate
                continue

        s = stripped.lstrip("- *\u2022")
        # Strip emoji markers (🔴🟡🔵🟢) that the agent sometimes adds
        import re as _re
        s = _re.sub(r'^[\U0001F534\U0001F7E1\U0001F535\U0001F7E2\U0001F7E0\s]+', '', s)
        # Accept bold severity labels too: **CRITICAL**: / **CRITICAL:** as well as plain
        # CRITICAL:. Match on an asterisk-free copy so the leading **…** does not hide the
        # prefix; the title comes from the same copy.
        s_plain = s.replace("*", "").strip()
        for sev in ["CRITICAL", "WARNING", "INFO"]:
            if s_plain.upper().startswith(f"{sev}:"):
                title = s_plain[len(sev) + 1:].strip()
                if title:
                    finding = {
                        "severity": sev.lower(),
                        "title": title[:300],
                        "description": "",
                        "evidence": [],
                        "recommended_actions": [],
                        "source": "correlation",
                    }
                    if current_db:
                        finding["database"] = current_db
                    findings.append(finding)
                break
    return findings


def _extract_sections(text):
    """Extract the four mandatory sections from agent output."""
    sections = {
        "findings_text": "",
        "root_cause": "",
        "recommendations": "",
        "executive_summary": "",
    }
    import re

    def _try_split(pattern):
        parts = re.split(pattern, text)
        found = {}
        for part in parts:
            # Strip bold markers, emojis, numbering, and special chars for matching
            cleaned = re.sub(r'^[^a-zA-Z]+', '', part.strip().strip("*").strip())
            lower = cleaned.lower()
            content = part[part.index("\n") + 1:].strip() if "\n" in part else ""
            if lower.startswith("findings"):
                found["findings_text"] = content
            elif lower.startswith("root cause"):
                found["root_cause"] = content
            elif lower.startswith("recommended actions") or lower.startswith("recommendations"):
                found["recommendations"] = content
            elif lower.startswith("summary"):
                found["executive_summary"] = content
        return found

    # Try level-2 headers first (## ) — preserves ### sub-headers within sections
    result = _try_split(r'\n##\s+')
    if result.get("findings_text") or result.get("root_cause"):
        sections.update(result)
    else:
        # Fallback: try level-3 headers (### )
        result = _try_split(r'\n###\s+')
        sections.update(result)

    return sections
