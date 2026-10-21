"""
Paperless-ngx async upload client (F-012).

Thin bridge — hands files off to Paperless without mirroring any state
locally. `document_vault` remains the store for typed/pasted text notes;
this module is for files that belong in Paperless (scanned receipts,
PDFs, photos of documents).

Never raises: every entry point returns a dict so callers (REST route,
paperless_save tool) can handle failures uniformly. Fire-and-forget from
the caller's perspective; the response reports success + Paperless task_id.

Read side (paperless_search / paperless_read tools): `search_documents()`
and `get_document()` are GET-only — this module never edits or deletes in
Paperless. Document text is OCR output of the user's papers: callers must
treat it as untrusted data, and nothing here logs a query, title or content.
"""

from __future__ import annotations

import asyncio
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
            logger.info(f"[PAPERLESS] Uploaded ({len(content)} bytes) task={task_id}")
            PAPERLESS_UPLOAD_TOTAL.labels(result="ok", reason="ok").inc()
            return {
                "success": True,
                "task_id": str(task_id),
                "latency_ms": int(latency * 1000),
            }

        # Non-2xx: log sanitized body (no token, no file content)
        body_snippet = _sanitize_body(resp.text)
        reason = "http_4xx" if 400 <= resp.status_code < 500 else "http_5xx"
        logger.warning(f"[PAPERLESS] Upload returned {resp.status_code}: {body_snippet}")
        PAPERLESS_UPLOAD_TOTAL.labels(result="fail", reason=reason).inc()
        return {
            "success": False,
            "status_code": resp.status_code,
            "body": body_snippet,
        }
    except httpx.TimeoutException as e:
        PAPERLESS_UPLOAD_TOTAL.labels(result="fail", reason="timeout").inc()
        logger.error(f"[PAPERLESS] Upload timed out: {e}")
        return {"success": False, "error": f"TimeoutException: {e}"}
    except httpx.ConnectError as e:
        PAPERLESS_UPLOAD_TOTAL.labels(result="fail", reason="connect_error").inc()
        logger.error(f"[PAPERLESS] Upload connect failed: {e}")
        return {"success": False, "error": f"ConnectError: {e}"}
    except Exception as e:
        PAPERLESS_UPLOAD_TOTAL.labels(result="fail", reason="other").inc()
        logger.error(f"[PAPERLESS] Upload failed: {type(e).__name__}: {e}")
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


# -- Read side (paperless_search / paperless_read) ---------------------------

# Max OCR characters returned per paperless_read call. The unified loop caps a
# tool result at 8000 chars; leave room for the header + framing lines.
READ_CHUNK_CHARS = 6000
_MAX_SEARCH_RESULTS = 10
_MAX_QUERY_LEN = 300
_MAX_TAGS = 8
_LIST_FIELDS = "id,title,created,correspondent,document_type,tags"
# Interactive chat waits on these: fail fast when Paperless is down rather
# than reuse the 30 s upload timeout (up to 4 calls per tool call).
_READ_TIMEOUT = httpx.Timeout(10.0, connect=3.0)


def _read_skip_reason() -> Optional[str]:
    if not _settings.paperless_enabled:
        return "disabled"
    if not _settings.paperless_url:
        return "missing_url"
    if not _settings.paperless_api_token:
        return "missing_token"
    return None


def _base_url() -> str:
    return _settings.paperless_url.rstrip("/")


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Token {_settings.paperless_api_token}", "Accept": "application/json"}


def _read_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_READ_TIMEOUT, headers=_auth_headers())


def _status_reason(code: int) -> str:
    if 400 <= code < 500:
        return "http_4xx"
    if code >= 500:
        return "http_5xx"
    return "http_other"  # e.g. a 3xx redirect to a login page — httpx doesn't follow it


def _short_date(value: Any) -> str:
    # Paperless `created` is "2026-03-01" (2.x) or a full ISO datetime (1.x).
    return str(value)[:10] if value else ""


def _int_or_none(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


async def _resolve_names(client: httpx.AsyncClient, endpoint: str, ids: set[int]) -> dict[int, str]:
    """Map tag/correspondent/document-type ids → names. Best effort: a failed
    lookup just leaves ids unnamed rather than failing the whole read."""
    if not ids:
        return {}
    try:
        resp = await client.get(
            f"{_base_url()}/api/{endpoint}/",
            params={"id__in": ",".join(str(i) for i in sorted(ids)), "page_size": len(ids)},
        )
        body = resp.json() if resp.status_code == 200 else {}
        results = body.get("results") if isinstance(body, dict) else None
        return {
            r["id"]: str(r.get("name", ""))[:100]
            for r in results or []
            if isinstance(r, dict) and isinstance(r.get("id"), int)
        }
    except Exception:  # noqa: BLE001 — best effort by design
        return {}


async def _describe(client: httpx.AsyncClient, docs: list[dict]) -> list[dict]:
    """Replace correspondent/document_type/tag ids with their names."""
    tag_ids: set[int] = set()
    corr_ids: set[int] = set()
    type_ids: set[int] = set()
    for d in docs:
        tags = d.get("tags") if isinstance(d.get("tags"), list) else []
        tag_ids.update(t for t in tags if _int_or_none(t) is not None)
        if _int_or_none(d.get("correspondent")) is not None:
            corr_ids.add(d["correspondent"])
        if _int_or_none(d.get("document_type")) is not None:
            type_ids.add(d["document_type"])
    tags_by_id, corrs, types = await asyncio.gather(
        _resolve_names(client, "tags", tag_ids),
        _resolve_names(client, "correspondents", corr_ids),
        _resolve_names(client, "document_types", type_ids),
    )
    out = []
    for d in docs:
        doc_tags = d.get("tags") if isinstance(d.get("tags"), list) else []
        out.append(
            {
                "id": _int_or_none(d.get("id")),
                "title": str(d.get("title") or "")[:200],
                "created": _short_date(d.get("created")),
                "correspondent": corrs.get(_int_or_none(d.get("correspondent")), ""),
                "document_type": types.get(_int_or_none(d.get("document_type")), ""),
                "tags": [tags_by_id[t] for t in doc_tags if _int_or_none(t) in tags_by_id][:_MAX_TAGS],
            }
        )
    return out


def _failure(op: str, reason: str, exc: Optional[Exception] = None) -> Dict[str, Any]:
    from orchestrator.metrics import PAPERLESS_READ_TOTAL

    PAPERLESS_READ_TOTAL.labels(op=op, result=reason).inc()
    # Type name only: an httpx error string carries the request URL, and a
    # search URL carries the user's query.
    logger.warning("[PAPERLESS] %s failed: %s", op, type(exc).__name__ if exc else reason)
    return {"success": False, "error": reason}


def _exc_reason(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connect_error"
    return "other"


async def search_documents(query: str, limit: int = 5) -> Dict[str, Any]:
    """Full-text search of the Paperless archive (title + OCR content).

    Returns {"success": True, "count": total_matches, "results": [{id, title,
    created, correspondent, document_type, tags}]} or {"success": False,
    "skipped"|"status_code"|"error": ...}. Never raises; every exit path bumps
    PAPERLESS_READ_TOTAL{op="search"} exactly once.
    """
    from orchestrator.metrics import PAPERLESS_READ_TOTAL

    skip = _read_skip_reason()
    if skip:
        PAPERLESS_READ_TOTAL.labels(op="search", result=skip).inc()
        return {"success": False, "skipped": True, "reason": skip}

    try:
        query = str(query or "").strip()[:_MAX_QUERY_LEN]
        limit = max(1, min(int(limit or 5), _MAX_SEARCH_RESULTS))
        params = {"query": query, "page_size": limit, "fields": _LIST_FIELDS}
        async with _read_client() as client:
            resp = await client.get(f"{_base_url()}/api/documents/", params=params)
            if resp.status_code != 200:
                PAPERLESS_READ_TOTAL.labels(op="search", result=_status_reason(resp.status_code)).inc()
                logger.warning("[PAPERLESS] search returned HTTP %s", resp.status_code)
                return {"success": False, "status_code": resp.status_code}
            body = resp.json()
            if not isinstance(body, dict) or not isinstance(body.get("results", []), list):
                return _failure("search", "bad_body")
            docs = [d for d in body.get("results", []) if isinstance(d, dict)][:limit]
            results = await _describe(client, docs)
        count = body.get("count")
        count = count if _int_or_none(count) is not None else len(results)
    except Exception as exc:  # noqa: BLE001 — never raises; metric must fire once
        return _failure("search", _exc_reason(exc), exc)

    PAPERLESS_READ_TOTAL.labels(op="search", result="ok").inc()
    logger.info("[PAPERLESS] search ok (%d of %d matches)", len(results), count)
    return {"success": True, "count": count, "results": results}


async def get_document(doc_id: int, offset: int = 0) -> Dict[str, Any]:
    """Fetch one document's metadata + a READ_CHUNK_CHARS slice of its OCR text.

    Returns {"success": True, "document": {..., "content": slice,
    "content_length": n, "offset": o, "next_offset": int|None}}, or
    {"success": False, "not_found": True} / "skipped" / "status_code" / "error".
    Never raises; bumps PAPERLESS_READ_TOTAL{op="get"} exactly once.
    """
    from orchestrator.metrics import PAPERLESS_READ_TOTAL

    skip = _read_skip_reason()
    if skip:
        PAPERLESS_READ_TOTAL.labels(op="get", result=skip).inc()
        return {"success": False, "skipped": True, "reason": skip}

    try:
        doc_id = int(doc_id)
        async with _read_client() as client:
            resp = await client.get(f"{_base_url()}/api/documents/{doc_id}/")
            if resp.status_code == 404:
                PAPERLESS_READ_TOTAL.labels(op="get", result="not_found").inc()
                return {"success": False, "not_found": True}
            if resp.status_code != 200:
                PAPERLESS_READ_TOTAL.labels(op="get", result=_status_reason(resp.status_code)).inc()
                logger.warning("[PAPERLESS] get returned HTTP %s", resp.status_code)
                return {"success": False, "status_code": resp.status_code}
            raw = resp.json()
            if not isinstance(raw, dict):
                return _failure("get", "bad_body")
            (doc,) = await _describe(client, [raw])
        content = str(raw.get("content") or "")
        offset = max(0, min(int(offset or 0), len(content)))
    except Exception as exc:  # noqa: BLE001 — never raises; metric must fire once
        return _failure("get", _exc_reason(exc), exc)

    end = offset + READ_CHUNK_CHARS
    doc.update(
        {
            "content": content[offset:end],
            "content_length": len(content),
            "offset": offset,
            "next_offset": end if end < len(content) else None,
        }
    )
    PAPERLESS_READ_TOTAL.labels(op="get", result="ok").inc()
    logger.info("[PAPERLESS] get ok (%d chars of %d)", len(doc["content"]), len(content))
    return {"success": True, "document": doc}
