# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""S3-backed custom knowledge store (Agent Capabilities).

Custom knowledge documents live in S3 (the reports bucket) under a per-scope prefix:
  global : knowledge/_global/custom/
  space  : knowledge/{space_id}/custom/
  user   : knowledge/_user/{user_sub}/{space_id}/custom/

Markdown/text (.md/.txt) is stored as-is. A PDF (.pdf) is stored as the original
plus an extracted `.md` sidecar — the extracted text is what the agent reads
(get_runbooks composes the `.md`/`.txt` bodies). Enablement/disable state stays in
DynamoDB; this module only handles document content, so both mechanisms compose
cleanly (unifying the previous split where some custom knowledge was inline in DDB).

pypdf is imported lazily/guarded so the module loads even if it isn't installed
(PDF upload then returns a clear error; markdown is unaffected).
"""

import os
import io
import re
import logging

import boto3
from tools.resource_names import reports_bucket

logger = logging.getLogger("knowledge-store")

_TEXT_SUFFIXES = (".md", ".sop.md", ".txt")

# Ceiling on a PDF we are willing to parse. The pypdf pin (see requirements.txt)
# carries the upstream fixes for decode-stream RAM amplification, but a size limit
# is the cheaper structural defence and does not depend on staying current: a
# knowledge doc past this size is a mistake, not a runbook.
_MAX_PDF_BYTES = int(os.getenv("MAX_PDF_UPLOAD_BYTES", str(25 * 1024 * 1024)))

_s3 = None


def _bucket() -> str:
    return reports_bucket()


def _client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=os.getenv("AWS_REGION", "us-east-1"))
    return _s3


# ── Prefixes (per governance layer) ──────────────────────────────────────────
def global_prefix() -> str:
    return "knowledge/_global/custom/"


def admin_prefix(admin_sub: str) -> str:
    """Space-admin tier: docs a space admin adds, inherited by every space they manage."""
    return f"knowledge/_admin/{admin_sub}/custom/"
def space_prefix(space_id: str) -> str:
    return f"knowledge/{space_id}/custom/"


def user_prefix(user_sub: str, space_id: str) -> str:
    return f"knowledge/_user/{user_sub}/{space_id}/custom/"


def sanitize_name(name: str) -> str:
    """Path-safe document name (mirrors the existing space-knowledge upload rules)."""
    name = re.sub(r"\.{2,}", ".", name or "").lstrip("./\\")
    name = re.sub(r"[^A-Za-z0-9._-]", "-", name)
    return name.strip("-") or ""


def _is_text(name: str) -> bool:
    return name.lower().endswith(_TEXT_SUFFIXES)


def _extract_pdf(data: bytes) -> str:
    """Extract text from a PDF. Raises a clear error if pypdf is unavailable."""
    if len(data) > _MAX_PDF_BYTES:
        raise ValueError(
            f"PDF is {len(data)} bytes, over the {_MAX_PDF_BYTES}-byte limit. "
            "Split the document, or raise MAX_PDF_UPLOAD_BYTES."
        )
    try:
        from pypdf import PdfReader
    except Exception as e:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "PDF support requires the 'pypdf' package on the backend image."
        ) from e
    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()


# ── CRUD ─────────────────────────────────────────────────────────────────────
def list_docs(prefix: str) -> list:
    """List custom docs under a prefix. PDFs are listed once (the original), not
    their extracted sidecars."""
    out = []
    try:
        resp = _client().list_objects_v2(Bucket=_bucket(), Prefix=prefix)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            name = key[len(prefix):]
            if not name or "/" in name:
                continue
            # Hide the extracted sidecar (foo.pdf.md) — the .pdf represents it.
            if name.endswith(".pdf.md"):
                continue
            out.append({
                "name": name,
                "size": obj.get("Size", 0),
                "last_modified": obj.get("LastModified").isoformat() if obj.get("LastModified") else None,
                "type": "pdf" if name.lower().endswith(".pdf") else "text",
            })
    except Exception as e:
        logger.debug(f"list_docs({prefix}) failed: {e}")
    return out


def get_doc(prefix: str, name: str) -> str:
    """Return the human-viewable content of a doc. For a PDF, return its extracted text."""
    key = prefix + name
    if name.lower().endswith(".pdf"):
        key = key + ".md"  # the extracted sidecar
    obj = _client().get_object(Bucket=_bucket(), Key=key)
    return obj["Body"].read().decode("utf-8", errors="replace")


def put_text(prefix: str, name: str, content: str) -> dict:
    """Store a markdown/text document."""
    if not _is_text(name):
        name = name + ".md"
    key = prefix + name
    _client().put_object(Bucket=_bucket(), Key=key, Body=content.encode("utf-8"),
                         ContentType="text/markdown")
    return {"name": name, "key": key, "type": "text"}


def put_pdf(prefix: str, name: str, data: bytes) -> dict:
    """Store a PDF (original) plus its extracted-text sidecar (`<name>.md`)."""
    if not name.lower().endswith(".pdf"):
        name = name + ".pdf"
    text = _extract_pdf(data)
    c = _client()
    c.put_object(Bucket=_bucket(), Key=prefix + name, Body=data, ContentType="application/pdf")
    c.put_object(Bucket=_bucket(), Key=prefix + name + ".md",
                 Body=text.encode("utf-8"), ContentType="text/markdown")
    return {"name": name, "key": prefix + name, "type": "pdf", "extracted_chars": len(text)}


def delete_doc(prefix: str, name: str) -> None:
    """Delete a doc (and its extracted sidecar for PDFs)."""
    c = _client()
    c.delete_object(Bucket=_bucket(), Key=prefix + name)
    if name.lower().endswith(".pdf"):
        try:
            c.delete_object(Bucket=_bucket(), Key=prefix + name + ".md")
        except Exception:
            pass


def load_bodies(prefix: str) -> list:
    """Load {name, content} for every doc under a prefix — the text the agent reads.
    For PDFs, loads the extracted sidecar. Best-effort (skips unreadable objects)."""
    docs = []
    for meta in list_docs(prefix):
        try:
            docs.append({"name": meta["name"], "content": get_doc(prefix, meta["name"])})
        except Exception as e:
            logger.debug(f"load_bodies: skip {meta['name']}: {e}")
    return docs
