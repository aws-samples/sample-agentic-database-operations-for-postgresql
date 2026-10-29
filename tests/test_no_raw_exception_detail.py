#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""No handler returns raw exception text to the client (review #12, rules 18 & 19).

A botocore exception carries the deployment's own identifiers — account id,
assumed-role / STS ARNs, metrics-store endpoints. Returning it as an HTTP `detail`
crosses the identifier line (rule 19) and fails rule 18 (errors say what to do). So no
`raise HTTPException(..., detail=...)` in backend/app.py may put a caught exception into
`detail` RAW: it must either be a fixed message, go through `_server_error(...)`, or be
passed through `_redact_identifiers(...)`.

This scans the AST, so a new handler that reintroduces `detail=str(e)` fails here before
a reviewer ever sees it.

    python3 tests/test_no_raw_exception_detail.py      (from the repository root)
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "backend/app.py"
tree = ast.parse(APP.read_text(encoding="utf-8"))

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def _exception_names(node: ast.AST) -> set:
    """The bound names of every `except ... as NAME` enclosing this node is hard to get
    without parent links, so instead we collect ALL except-handler bound names in the
    module and treat any of them as 'the caught exception'. Good enough: a handler that
    interpolates a variable literally named like an exception binding is exactly what we
    want to catch."""
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
    return names


EXC_NAMES = _exception_names(tree)


def _references_raw_exception(detail: ast.AST) -> bool:
    """True if `detail` puts a caught-exception value in UNREDACTED. `str(e)` or a bare
    `e`, or an f-string that interpolates `e` outside a _redact_identifiers() call."""
    # str(e) or str(exc) directly
    if isinstance(detail, ast.Call) and isinstance(detail.func, ast.Name) and detail.func.id == "str":
        if any(isinstance(a, ast.Name) and a.id in EXC_NAMES for a in detail.args):
            return True
    # a bare exception name
    if isinstance(detail, ast.Name) and detail.id in EXC_NAMES:
        return True
    # an f-string (JoinedStr) that interpolates an exception name directly
    if isinstance(detail, ast.JoinedStr):
        for part in detail.values:
            if isinstance(part, ast.FormattedValue):
                v = part.value
                if isinstance(v, ast.Name) and v.id in EXC_NAMES:
                    return True
                # str(e) inside the f-string
                if (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                        and v.func.id == "str"
                        and any(isinstance(a, ast.Name) and a.id in EXC_NAMES for a in v.args)):
                    return True
    return False


def _is_safe_wrap(detail: ast.AST) -> bool:
    """True if `detail` is _redact_identifiers(...) — an explicitly sanitised value."""
    return (isinstance(detail, ast.Call) and isinstance(detail.func, ast.Name)
            and detail.func.id == "_redact_identifiers")


offenders = []
for node in ast.walk(tree):
    if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
        continue
    call = node.exc
    func = call.func
    is_http = (isinstance(func, ast.Name) and func.id == "HTTPException")
    if not is_http:
        continue
    for kw in call.keywords:
        if kw.arg != "detail":
            continue
        if _is_safe_wrap(kw.value):
            continue
        if _references_raw_exception(kw.value):
            offenders.append(node.lineno)

print("══ no HTTPException detail returns raw exception text ══")
check(not offenders, f"no raw-exception detail= in backend/app.py (offending lines: {offenders})")

# The shared helper exists and logs + returns a fixed message (never the raw text).
src = APP.read_text(encoding="utf-8")
check("def _server_error(" in src, "the shared _server_error helper exists")
import re
helper = re.search(r"def _server_error\(.*?(?=\ndef )", src, re.S).group(0)
check("logger.error(" in helper, "_server_error logs the detail server-side")
check("detail=str(e)" not in helper and "{e}" not in helper,
      "_server_error never puts the raw exception in the detail")

print("")
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
