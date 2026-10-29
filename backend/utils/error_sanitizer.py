# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Sanitize error responses so they cannot leak internal detail to a client.

Two hazards, both reachable by any authenticated caller:

  * A 5xx handler that returns ``str(e)`` in ``detail`` hands the raw exception text
    (which routinely carries ARNs, database hosts, and stack context) straight to the
    browser. Client errors (4xx) are different: their ``detail`` is developer-authored
    and intentional ("content or pdf_base64 required"), so it is preserved.
  * The default 422 body echoes the caller's submitted value back in each error's
    ``input`` field (and sometimes ``ctx``). The field location, message and type are
    useful to a client and safe; the echoed value is not.

These are pure functions with no framework dependency so they can be unit-tested
without importing the FastAPI app.
"""

SERVER_ERROR_CODE = "internal_error"
SERVER_ERROR_MESSAGE = "Internal server error"


def http_exception_body(status_code: int, detail):
    """Response body for an HTTPException.

    5xx collapses to a generic message plus a typed code, so raw exception text never
    reaches the client (the real detail is logged server-side by the caller). Anything
    below 500 keeps its intentional, developer-authored ``detail`` unchanged, so client
    error messages are not degraded.
    """
    if status_code >= 500:
        return {"detail": SERVER_ERROR_MESSAGE, "code": SERVER_ERROR_CODE}
    return {"detail": detail}


def sanitize_validation_errors(errors):
    """Drop echoed request input from validation errors, keeping the shape.

    Returns a list of ``{loc, msg, type}`` entries, preserving FastAPI's ``detail``-is-a-
    list shape so existing clients keep rendering field errors, but without the ``input``
    value the caller submitted (and any ``ctx``, which can carry it too).
    """
    cleaned = []
    for err in errors or []:
        if not isinstance(err, dict):
            continue
        cleaned.append({
            "loc": err.get("loc"),
            "msg": err.get("msg"),
            "type": err.get("type"),
        })
    return cleaned
