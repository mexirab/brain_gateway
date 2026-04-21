"""Paperless-ngx REST proxy (F-012).

Single route: POST /api/paperless/upload accepts a multipart file +
optional metadata fields and forwards to Paperless via the
paperless_manager client. No local copy is persisted on Helios; Paperless
owns the file after it returns a task id.

Upload size cap is enforced upstream by RequestSizeLimitMiddleware (see
`_LARGE_UPLOAD_PATHS` in orchestrator.py — this path is added there).
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse

from orchestrator.config import settings
from orchestrator.paperless_manager import upload_file as _upload_to_paperless

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/paperless/upload")
async def paperless_upload(
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    correspondent: Optional[str] = Form(None),
    document_type: Optional[str] = Form(None),
    tags: Optional[List[str]] = Form(None),
):
    """Upload a file to Paperless-ngx for OCR + tagging.

    Multipart form fields:
      file (required)          - the file bytes
      title (optional)         - document title; Paperless infers if omitted
      correspondent (optional) - sender/author name
      document_type (optional) - doc type name (e.g. 'invoice')
      tags (optional, repeat)  - one `tags` field per tag name

    Returns 200 with task_id on success, 503 when the feature is disabled,
    or 502 when Paperless rejected or was unreachable.
    """
    if not settings.paperless_enabled:
        return JSONResponse(
            {"ok": False, "error": "paperless_disabled"},
            status_code=503,
        )

    content = await file.read()
    filename = file.filename or "upload.bin"

    result = await _upload_to_paperless(
        content=content,
        filename=filename,
        title=title,
        correspondent=correspondent,
        document_type=document_type,
        tags=tags,
    )

    if result.get("success"):
        return JSONResponse(
            {
                "ok": True,
                "task_id": result.get("task_id"),
                "latency_ms": result.get("latency_ms"),
            }
        )

    # Disabled-at-runtime or missing config mid-flight — surface as 503 to
    # distinguish from Paperless returning an HTTP error.
    if result.get("skipped"):
        return JSONResponse(
            {"ok": False, "error": result.get("reason", "skipped")},
            status_code=503,
        )

    # Everything else (Paperless non-2xx, network error) is a bad gateway.
    return JSONResponse(
        {
            "ok": False,
            "error": result.get("error") or f"status {result.get('status_code')}",
            "body": result.get("body"),
        },
        status_code=502,
    )
