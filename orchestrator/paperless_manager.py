"""
Paperless-ngx async upload client (F-012).

Thin bridge — hands files off to Paperless without mirroring any state
locally. `document_vault` remains the store for typed/pasted text notes;
this module is for files that belong in Paperless (scanned receipts,
PDFs, photos of documents).

Never raises: every entry point returns a dict so callers (REST route,
paperless_save tool) can handle failures uniformly. Fire-and-forget from
the caller's perspective; the response reports success + Paperless task_id.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, Optional

import httpx

from orchestrator.config import settings as _settings

logger = logging.getLogger(__name__)

# Per-tag length cap (bytes). Defensive against an LLM that emits a 10MB
# "tag" — keeps outbound body size sane and log lines readable.
_MAX_TAG_LEN = 256


def _default_tags_list() -> list[str]:
    """Parse PAPERLESS_DEFAULT_TAGS (comma-separated) once per call."""
    raw = _settings.paperless_default_tags or ""
    return [t.strip() for t in raw.split(",") if t.strip()]


def _sanitize_body(text: str, limit: int = 300) -> str:
    """Strip control chars (except tab/newline) and cap length.

    Reduces prompt-injection surface when the tool surfaces Paperless's
    error body verbatim into the LLM's context. Not a full defense —
    operator owns the Paperless server — but cheap hygiene.
    """
    if not text:
        return ""
    cleaned = "".join(ch for ch in text if ch in ("\t", "\n") or 0x20 <= ord(ch) < 0x7F)
    return cleaned[:limit]


async def upload_file(
    content: bytes,
    filename: str,
    title: Optional[str] = None,
    correspondent: Optional[str] = None,
    document_type: Optional[str] = None,
    tags: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Push a file to Paperless-ngx for ingestion.

    Paperless's POST /api/documents/post_document/ accepts multipart
    form-data with the file plus optional title/correspondent/document_type
    and repeated `tags` fields (tag NAMES — Paperless auto-creates missing
    ones when the appropriate setting is on; otherwise unknown tags are
    silently dropped server-side).

    Returns one of:
      {"success": True, "task_id": "...", "latency_ms": ...}
      {"success": False, "skipped": True, "reason": "disabled|missing_*"}
      {"success": False, "status_code": int, "body": "..."}
      {"success": False, "error": "TypeName: msg"}

    Never raises. Every exit path increments PAPERLESS_UPLOAD_TOTAL exactly
    once — the invariant that makes Grafana honest. The `reason` label
    distinguishes failure modes (see metrics docstring).
    """
    from orchestrator.metrics import (
        PAPERLESS_UPLOAD_LATENCY,
        PAPERLESS_UPLOAD_TOTAL,
    )

    if not _settings.paperless_enabled:
        PAPERLESS_UPLOAD_TOTAL.labels(result="skipped", reason="disabled").inc()
        return {"success": False, "skipped": True, "reason": "disabled"}

    if not _settings.paperless_url:
        PAPERLESS_UPLOAD_TOTAL.labels(result="skipped", reason="missing_url").inc()
        return {"success": False, "skipped": True, "reason": "missing_url"}
    if not _settings.paperless_api_token:
        PAPERLESS_UPLOAD_TOTAL.labels(result="skipped", reason="missing_token").inc()
        return {"success": False, "skipped": True, "reason": "missing_token"}

    # Merge tool-supplied tags with env-configured defaults; dedup preserving
    # order; cap length per tag so a rogue 10MB "tag" string can't bloat the
    # outbound POST or blow out logs.
    merged_tags: list[str] = []
    seen: set[str] = set()
    for t in list(tags or []) + _default_tags_list():
        if not t:
            continue
        capped = t[:_MAX_TAG_LEN] if isinstance(t, str) else str(t)[:_MAX_TAG_LEN]
        if capped in seen:
            continue
        merged_tags.append(capped)
        seen.add(capped)

    # Paperless accepts the file under the field name `document`.
    files = {"document": (filename, content, "application/octet-stream")}
    # Use dict[str, str | list[str]] (not list[tuple]) — on httpx 0.28+ the
    # list-of-tuples form produces an IteratorByteStream which AsyncClient
    # refuses at send time ("Attempted to send an sync request with an
    # AsyncClient instance"). Dict with list values for repeated keys
    # produces the correct MultipartStream.
    data: dict[str, str | list[str]] = {}
    if title:
        data["title"] = title[:256]
    if correspondent:
        data["correspondent"] = correspondent[:256]
    if document_type:
        data["document_type"] = document_type[:256]
    if merged_tags:
        data["tags"] = merged_tags

    url = f"{_settings.paperless_url.rstrip('/')}/api/documents/post_document/"
    headers = {"Authorization": f"Token {_settings.paperless_api_token}"}
    timeout = _settings.paperless_upload_timeout_seconds

    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, files=files, data=data)
        latency = time.time() - t0
        PAPERLESS_UPLOAD_LATENCY.observe(latency)

        if 200 <= resp.status_code < 300:
            # Paperless returns a task id string (UUID) that the user can
            # poll at /api/tasks/{id}/ for status; we don't block on it.
            # Older versions return `"uuid-string"`; some return
            # `{"id": "uuid-string"}`. Handle both.
            try:
                body_json = resp.json()
            except Exception:
                body_json = resp.text
            if isinstance(body_json, str):
                task_id = body_json
            elif isinstance(body_json, dict):
                task_id = body_json.get("task_id") or body_json.get("id") or resp.text[:60]
            else:
                task_id = resp.text[:60]
            logger.info(f"[PAPERLESS] Uploaded {filename} ({len(content)} bytes) task={str(task_id)[:60]}")
            PAPERLESS_UPLOAD_TOTAL.labels(result="ok", reason="ok").inc()
            return {
                "success": True,
                "task_id": str(task_id),
                "latency_ms": int(latency * 1000),
            }

        # Non-2xx: log sanitized body (no token, no file content)
        body_snippet = _sanitize_body(resp.text)
        reason = "http_4xx" if 400 <= resp.status_code < 500 else "http_5xx"
        logger.warning(f"[PAPERLESS] Upload {filename} returned {resp.status_code}: {body_snippet}")
        PAPERLESS_UPLOAD_TOTAL.labels(result="fail", reason=reason).inc()
        return {
            "success": False,
            "status_code": resp.status_code,
            "body": body_snippet,
        }
    except httpx.TimeoutException as e:
        PAPERLESS_UPLOAD_TOTAL.labels(result="fail", reason="timeout").inc()
        logger.error(f"[PAPERLESS] Upload {filename} timed out: {e}")
        return {"success": False, "error": f"TimeoutException: {e}"}
    except httpx.ConnectError as e:
        PAPERLESS_UPLOAD_TOTAL.labels(result="fail", reason="connect_error").inc()
        logger.error(f"[PAPERLESS] Upload {filename} connect failed: {e}")
        return {"success": False, "error": f"ConnectError: {e}"}
    except Exception as e:
        PAPERLESS_UPLOAD_TOTAL.labels(result="fail", reason="other").inc()
        logger.error(f"[PAPERLESS] Upload {filename} failed: {type(e).__name__}: {e}")
        return {"success": False, "error": f"{type(e).__name__}: {e}"}
