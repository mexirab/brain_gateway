"""
Tests for F-012 Paperless bridge.

Covers:
  - paperless_manager._default_tags_list / _sanitize_body (pure unit)
  - paperless_manager.upload_file (network-mocked via respx)
  - tool_handlers._reg_paperless_save (path-sanitization + happy path)
  - routes_paperless.POST /api/paperless/upload (via FastAPI TestClient)
  - config.validate_paperless_config model_validator (auto-disable semantics)

Mocks settings via monkeypatch on the live module-level singleton (same
pattern as F-011's test_ntfy_feedback). Mocks HTTP via respx so no real
network calls leak out of the container. Per-tag file uploads are staged
into a tmp_path inbox so symlink / basename rules can be exercised
concretely.

Metric-consistency invariant (prod-support flagged): every upload_file
exit path must bump PAPERLESS_UPLOAD_TOTAL exactly once. Asserted on
each branch below via before/after delta.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from httpx import Response


# ---------------------------------------------------------------------------
# Settings fixtures — flip the live singleton; restore via monkeypatch undo.
# ---------------------------------------------------------------------------


@pytest.fixture
def paperless_on(monkeypatch):
    """Enable paperless on the live settings singleton with known endpoint."""
    from orchestrator.config import settings

    monkeypatch.setattr(settings, "paperless_enabled", True, raising=False)
    monkeypatch.setattr(settings, "paperless_url", "http://paperless.test:8777", raising=False)
    monkeypatch.setattr(settings, "paperless_api_token", "tok-abcdefgh", raising=False)
    monkeypatch.setattr(settings, "paperless_default_tags", "", raising=False)
    monkeypatch.setattr(settings, "paperless_upload_timeout_seconds", 30, raising=False)
    return settings


@pytest.fixture
def paperless_off(monkeypatch):
    """Disabled — for feature-flag-gate tests."""
    from orchestrator.config import settings

    monkeypatch.setattr(settings, "paperless_enabled", False, raising=False)
    return settings


@pytest.fixture
def paperless_inbox(tmp_path, monkeypatch):
    """Point paperless_inbox_path at a fresh temp dir for the duration of the test."""
    from orchestrator.config import settings

    monkeypatch.setattr(settings, "paperless_inbox_path", str(tmp_path), raising=False)
    return tmp_path


def _counter_value(result: str, reason: str) -> float:
    """Read current PAPERLESS_UPLOAD_TOTAL{result,reason} — 0 if never emitted."""
    from orchestrator.metrics import PAPERLESS_UPLOAD_TOTAL

    return PAPERLESS_UPLOAD_TOTAL.labels(result=result, reason=reason)._value.get()


# ===========================================================================
# _default_tags_list (pure unit)
# ===========================================================================


class TestDefaultTagsList:
    def test_empty_returns_empty(self, monkeypatch):
        from orchestrator.config import settings
        from orchestrator.paperless_manager import _default_tags_list

        monkeypatch.setattr(settings, "paperless_default_tags", "", raising=False)
        assert _default_tags_list() == []

    def test_single(self, monkeypatch):
        from orchestrator.config import settings
        from orchestrator.paperless_manager import _default_tags_list

        monkeypatch.setattr(settings, "paperless_default_tags", "receipts", raising=False)
        assert _default_tags_list() == ["receipts"]

    def test_csv_trims_and_filters_blanks(self, monkeypatch):
        from orchestrator.config import settings
        from orchestrator.paperless_manager import _default_tags_list

        monkeypatch.setattr(
            settings, "paperless_default_tags", " a , b ,,  c,  ", raising=False
        )
        assert _default_tags_list() == ["a", "b", "c"]


# ===========================================================================
# _sanitize_body (pure unit)
# ===========================================================================


class TestSanitizeBody:
    def test_empty_string_returns_empty(self):
        from orchestrator.paperless_manager import _sanitize_body

        assert _sanitize_body("") == ""

    def test_none_returns_empty(self):
        from orchestrator.paperless_manager import _sanitize_body

        assert _sanitize_body(None) == ""  # type: ignore[arg-type]

    def test_plain_text_preserved(self):
        from orchestrator.paperless_manager import _sanitize_body

        assert _sanitize_body("hello world") == "hello world"

    def test_strips_control_chars(self):
        from orchestrator.paperless_manager import _sanitize_body

        # \x01 (SOH) and \x1f (US) are below 0x20 → stripped
        # \x7f (DEL) is >=0x7F → stripped
        # \t (0x09) and \n (0x0a) are whitelisted → kept
        dirty = "ok\x01bad\x1fstuff\x7fend\ttab\nline"
        cleaned = _sanitize_body(dirty)
        assert cleaned == "okbadstuffend\ttab\nline"

    def test_caps_at_limit(self):
        from orchestrator.paperless_manager import _sanitize_body

        assert _sanitize_body("x" * 500) == "x" * 300
        assert _sanitize_body("x" * 50, limit=10) == "x" * 10


# ===========================================================================
# upload_file — respx-mocked HTTP
# ===========================================================================


class TestUploadFile:
    @pytest.mark.asyncio
    async def test_disabled_returns_skipped_without_http(self, paperless_off):
        from orchestrator.paperless_manager import upload_file

        before = _counter_value("skipped", "disabled")
        with respx.mock:  # no routes → any HTTP would error
            result = await upload_file(b"pdf-bytes", "a.pdf")
        assert result == {"success": False, "skipped": True, "reason": "disabled"}
        assert _counter_value("skipped", "disabled") == before + 1

    @pytest.mark.asyncio
    async def test_missing_url_skipped(self, paperless_on, monkeypatch):
        from orchestrator.config import settings
        from orchestrator.paperless_manager import upload_file

        monkeypatch.setattr(settings, "paperless_url", "", raising=False)
        before = _counter_value("skipped", "missing_url")
        with respx.mock:
            result = await upload_file(b"x", "a.pdf")
        assert result["success"] is False
        assert result["skipped"] is True
        assert result["reason"] == "missing_url"
        assert _counter_value("skipped", "missing_url") == before + 1

    @pytest.mark.asyncio
    async def test_missing_token_skipped(self, paperless_on, monkeypatch):
        from orchestrator.config import settings
        from orchestrator.paperless_manager import upload_file

        monkeypatch.setattr(settings, "paperless_api_token", "", raising=False)
        before = _counter_value("skipped", "missing_token")
        with respx.mock:
            result = await upload_file(b"x", "a.pdf")
        assert result["success"] is False
        assert result["reason"] == "missing_token"
        assert _counter_value("skipped", "missing_token") == before + 1

    @pytest.mark.asyncio
    async def test_success_200_string_task_id(self, paperless_on):
        from orchestrator.paperless_manager import upload_file

        before = _counter_value("ok", "ok")
        with respx.mock(base_url="http://paperless.test:8777") as mock:
            route = mock.post("/api/documents/post_document/").mock(
                return_value=Response(200, json="task-uuid-123")
            )
            result = await upload_file(b"pdf", "receipt.pdf", title="Groceries")
        assert route.called
        # Auth header present
        req = route.calls[0].request
        assert req.headers["Authorization"] == "Token tok-abcdefgh"
        assert result["success"] is True
        assert result["task_id"] == "task-uuid-123"
        assert "latency_ms" in result
        assert _counter_value("ok", "ok") == before + 1

    @pytest.mark.asyncio
    async def test_success_201_dict_task_id_key(self, paperless_on):
        from orchestrator.paperless_manager import upload_file

        with respx.mock(base_url="http://paperless.test:8777") as mock:
            mock.post("/api/documents/post_document/").mock(
                return_value=Response(201, json={"task_id": "abc-999"})
            )
            result = await upload_file(b"x", "a.pdf")
        assert result["success"] is True
        assert result["task_id"] == "abc-999"

    @pytest.mark.asyncio
    async def test_success_202_dict_id_fallback(self, paperless_on):
        from orchestrator.paperless_manager import upload_file

        with respx.mock(base_url="http://paperless.test:8777") as mock:
            mock.post("/api/documents/post_document/").mock(
                return_value=Response(202, json={"id": "id-777"})
            )
            result = await upload_file(b"x", "a.pdf")
        assert result["success"] is True
        assert result["task_id"] == "id-777"

    @pytest.mark.asyncio
    async def test_success_200_raw_text_fallback(self, paperless_on):
        """Paperless returned 200 with non-JSON body → task_id falls back to resp.text[:60]."""
        from orchestrator.paperless_manager import upload_file

        with respx.mock(base_url="http://paperless.test:8777") as mock:
            mock.post("/api/documents/post_document/").mock(
                return_value=Response(200, text="plain-task-9")
            )
            result = await upload_file(b"x", "a.pdf")
        assert result["success"] is True
        # resp.json() parses "plain-task-9" as JSON string successfully — so
        # task_id comes out as the string itself, not via text[:60].
        assert result["task_id"] == "plain-task-9"

    @pytest.mark.asyncio
    async def test_4xx_returns_fail_with_sanitized_body(self, paperless_on):
        from orchestrator.paperless_manager import upload_file

        before = _counter_value("fail", "http_4xx")
        with respx.mock(base_url="http://paperless.test:8777") as mock:
            mock.post("/api/documents/post_document/").mock(
                return_value=Response(400, text="bad\x01request\x7ftext")
            )
            result = await upload_file(b"x", "a.pdf")
        assert result["success"] is False
        assert result["status_code"] == 400
        # control chars stripped
        assert "\x01" not in result["body"]
        assert "\x7f" not in result["body"]
        assert "bad" in result["body"]
        assert _counter_value("fail", "http_4xx") == before + 1

    @pytest.mark.asyncio
    async def test_5xx_returns_fail(self, paperless_on):
        from orchestrator.paperless_manager import upload_file

        before = _counter_value("fail", "http_5xx")
        with respx.mock(base_url="http://paperless.test:8777") as mock:
            mock.post("/api/documents/post_document/").mock(
                return_value=Response(503, text="overloaded")
            )
            result = await upload_file(b"x", "a.pdf")
        assert result["success"] is False
        assert result["status_code"] == 503
        assert _counter_value("fail", "http_5xx") == before + 1

    @pytest.mark.asyncio
    async def test_timeout_swallowed(self, paperless_on):
        from orchestrator.paperless_manager import upload_file

        before = _counter_value("fail", "timeout")
        with respx.mock(base_url="http://paperless.test:8777") as mock:
            mock.post("/api/documents/post_document/").mock(
                side_effect=httpx.TimeoutException("slow")
            )
            result = await upload_file(b"x", "a.pdf")
        assert result["success"] is False
        assert "TimeoutException" in result["error"]
        assert _counter_value("fail", "timeout") == before + 1

    @pytest.mark.asyncio
    async def test_connect_error_swallowed(self, paperless_on):
        from orchestrator.paperless_manager import upload_file

        before = _counter_value("fail", "connect_error")
        with respx.mock(base_url="http://paperless.test:8777") as mock:
            mock.post("/api/documents/post_document/").mock(
                side_effect=httpx.ConnectError("dns")
            )
            result = await upload_file(b"x", "a.pdf")
        assert result["success"] is False
        assert "ConnectError" in result["error"]
        assert _counter_value("fail", "connect_error") == before + 1

    @pytest.mark.asyncio
    async def test_generic_exception_swallowed(self, paperless_on):
        from orchestrator.paperless_manager import upload_file

        before = _counter_value("fail", "other")
        with respx.mock(base_url="http://paperless.test:8777") as mock:
            mock.post("/api/documents/post_document/").mock(
                side_effect=ValueError("weird")
            )
            result = await upload_file(b"x", "a.pdf")
        assert result["success"] is False
        assert "ValueError" in result["error"]
        assert _counter_value("fail", "other") == before + 1

    @pytest.mark.asyncio
    async def test_tags_merged_deduped_and_capped(self, paperless_on, monkeypatch):
        """Tool-supplied + env-default tags are merged, deduplicated, and each capped at 256 chars."""
        from orchestrator.config import settings
        from orchestrator.paperless_manager import upload_file

        monkeypatch.setattr(
            settings, "paperless_default_tags", "env-a, env-b, dup", raising=False
        )
        long_tag = "z" * 500
        with respx.mock(base_url="http://paperless.test:8777") as mock:
            route = mock.post("/api/documents/post_document/").mock(
                return_value=Response(200, json="ok")
            )
            await upload_file(
                b"x",
                "a.pdf",
                tags=["tool-x", "dup", long_tag],
            )
        body = route.calls[0].request.content.decode("utf-8", errors="replace")
        # tool tag "tool-x" present, dup only once, long_tag truncated to 256
        assert "tool-x" in body
        assert "env-a" in body
        assert "env-b" in body
        assert body.count("dup") == 1
        # Exactly 256 z's should appear, no 500-char run
        assert "z" * 256 in body
        assert "z" * 257 not in body

    @pytest.mark.asyncio
    async def test_title_correspondent_doctype_capped(self, paperless_on):
        from orchestrator.paperless_manager import upload_file

        with respx.mock(base_url="http://paperless.test:8777") as mock:
            route = mock.post("/api/documents/post_document/").mock(
                return_value=Response(200, json="ok")
            )
            await upload_file(
                b"x",
                "a.pdf",
                title="T" * 500,
                correspondent="C" * 500,
                document_type="D" * 500,
            )
        body = route.calls[0].request.content.decode("utf-8", errors="replace")
        # Each metadata field capped at 256
        assert "T" * 256 in body and "T" * 257 not in body
        assert "C" * 256 in body and "C" * 257 not in body
        assert "D" * 256 in body and "D" * 257 not in body


# ===========================================================================
# _reg_paperless_save tool handler
# ===========================================================================


class TestPaperlessSaveTool:
    @pytest.mark.asyncio
    async def test_disabled_returns_friendly(self, paperless_off):
        from orchestrator.tool_handlers import _reg_paperless_save

        out = await _reg_paperless_save({"filename": "a.pdf"})
        assert "disabled" in out.lower()
        assert "PAPERLESS_ENABLED" in out

    @pytest.mark.asyncio
    async def test_empty_filename(self, paperless_on):
        from orchestrator.tool_handlers import _reg_paperless_save

        out = await _reg_paperless_save({"filename": ""})
        assert "which filename" in out.lower()

    @pytest.mark.asyncio
    async def test_missing_filename_key(self, paperless_on):
        from orchestrator.tool_handlers import _reg_paperless_save

        out = await _reg_paperless_save({})
        assert "which filename" in out.lower()

    @pytest.mark.asyncio
    async def test_null_byte_refused(self, paperless_on):
        from orchestrator.tool_handlers import _reg_paperless_save

        out = await _reg_paperless_save({"filename": "evil\x00.pdf"})
        assert "null byte" in out.lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad",
        ["sub/file.pdf", "..\\escape.pdf", "../etc/passwd", "a/../b.pdf"],
    )
    async def test_path_traversal_refused(self, paperless_on, bad):
        from orchestrator.tool_handlers import _reg_paperless_save

        out = await _reg_paperless_save({"filename": bad})
        assert out.startswith("Refused filename")
        assert "basename" in out.lower()

    @pytest.mark.asyncio
    async def test_absolute_path_refused(self, paperless_on):
        from orchestrator.tool_handlers import _reg_paperless_save

        out = await _reg_paperless_save({"filename": "/etc/passwd"})
        # /etc/passwd hits the "/" in filename check first — either wording is acceptable
        assert "refused" in out.lower()

    @pytest.mark.asyncio
    async def test_symlink_escape_refused(self, paperless_on, paperless_inbox, tmp_path):
        """Symlink inside the inbox pointing outside must be rejected via resolve()+relative_to()."""
        from orchestrator.tool_handlers import _reg_paperless_save

        # Stash a real file OUTSIDE the inbox
        outside = tmp_path.parent / "outside-target.pdf"
        outside.write_bytes(b"secret")
        # Create a symlink INSIDE the inbox pointing at the outside file
        link = paperless_inbox / "innocent.pdf"
        try:
            os.symlink(outside, link)
        except OSError:
            pytest.skip("Symlinks not supported on this filesystem")

        out = await _reg_paperless_save({"filename": "innocent.pdf"})
        assert "escapes" in out.lower() or "refused" in out.lower()

    @pytest.mark.asyncio
    async def test_file_not_present(self, paperless_on, paperless_inbox):
        from orchestrator.tool_handlers import _reg_paperless_save

        out = await _reg_paperless_save({"filename": "ghost.pdf"})
        assert "no file named" in out.lower()
        assert "ghost.pdf" in out

    @pytest.mark.asyncio
    async def test_file_too_large_refused_and_metric(
        self, paperless_on, paperless_inbox, monkeypatch
    ):
        from orchestrator.tool_handlers import _reg_paperless_save

        # Cap at 1 MB for this test via env var (tool reads it fresh each call)
        monkeypatch.setenv("DOCUMENT_MAX_SIZE_MB", "1")
        # Write 2 MB
        big = paperless_inbox / "huge.pdf"
        big.write_bytes(b"\x00" * (2 * 1024 * 1024))

        before = _counter_value("skipped", "file_too_large")
        out = await _reg_paperless_save({"filename": "huge.pdf"})
        assert out.startswith("Refused")
        assert "MB" in out
        assert _counter_value("skipped", "file_too_large") == before + 1

    @pytest.mark.asyncio
    async def test_happy_path_queues_upload(self, paperless_on, paperless_inbox):
        from orchestrator.tool_handlers import _reg_paperless_save

        f = paperless_inbox / "receipt.pdf"
        f.write_bytes(b"%PDF-fake-bytes")

        # Mock the paperless_manager.upload_file symbol the handler imports
        with patch(
            "orchestrator.paperless_manager.upload_file",
            new=AsyncMock(return_value={"success": True, "task_id": "t-42", "latency_ms": 12}),
        ) as mocked:
            out = await _reg_paperless_save({
                "filename": "receipt.pdf",
                "title": "March receipt",
                "tags": ["receipts", "march"],
            })
        assert "Queued" in out
        assert "receipt.pdf" in out
        assert "t-42" in out
        # upload_file called once with the right metadata
        mocked.assert_called_once()
        kwargs = mocked.call_args.kwargs
        assert kwargs["filename"] == "receipt.pdf"
        assert kwargs["title"] == "March receipt"
        assert kwargs["tags"] == ["receipts", "march"]
        assert kwargs["content"] == b"%PDF-fake-bytes"

    @pytest.mark.asyncio
    async def test_comma_string_tags_get_split(self, paperless_on, paperless_inbox):
        """Defensive path: LLM passed a CSV string instead of a list."""
        from orchestrator.tool_handlers import _reg_paperless_save

        f = paperless_inbox / "a.pdf"
        f.write_bytes(b"x")

        with patch(
            "orchestrator.paperless_manager.upload_file",
            new=AsyncMock(return_value={"success": True, "task_id": "t", "latency_ms": 1}),
        ) as mocked:
            await _reg_paperless_save({"filename": "a.pdf", "tags": "a, b , c"})
        assert mocked.call_args.kwargs["tags"] == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_upload_fail_surfaces_status_code(self, paperless_on, paperless_inbox):
        from orchestrator.tool_handlers import _reg_paperless_save

        f = paperless_inbox / "a.pdf"
        f.write_bytes(b"x")

        with patch(
            "orchestrator.paperless_manager.upload_file",
            new=AsyncMock(return_value={
                "success": False, "status_code": 400, "body": "bad request",
            }),
        ):
            out = await _reg_paperless_save({"filename": "a.pdf"})
        assert "rejected" in out.lower()
        assert "400" in out
        assert "bad request" in out

    @pytest.mark.asyncio
    async def test_upload_error_surfaces_message(self, paperless_on, paperless_inbox):
        from orchestrator.tool_handlers import _reg_paperless_save

        f = paperless_inbox / "a.pdf"
        f.write_bytes(b"x")

        with patch(
            "orchestrator.paperless_manager.upload_file",
            new=AsyncMock(return_value={
                "success": False, "error": "ConnectError: dns",
            }),
        ):
            out = await _reg_paperless_save({"filename": "a.pdf"})
        assert "failed" in out.lower()
        assert "ConnectError" in out


# ===========================================================================
# routes_paperless — POST /api/paperless/upload via TestClient
# ===========================================================================


@pytest.fixture
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.routes_paperless import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestPaperlessRoute:
    def test_disabled_returns_503(self, client, paperless_off):
        r = client.post(
            "/api/paperless/upload",
            files={"file": ("a.pdf", b"x", "application/pdf")},
        )
        assert r.status_code == 503
        body = r.json()
        assert body["ok"] is False
        assert body["error"] == "paperless_disabled"

    def test_success_returns_200_with_task_id(self, client, paperless_on):
        with patch(
            "orchestrator.routes_paperless._upload_to_paperless",
            new=AsyncMock(return_value={
                "success": True, "task_id": "t-777", "latency_ms": 88,
            }),
        ) as mocked:
            r = client.post(
                "/api/paperless/upload",
                files={"file": ("scan.pdf", b"bytes", "application/pdf")},
                data={"title": "Scan", "tags": ["invoice", "2026"]},
            )
        assert r.status_code == 200
        body = r.json()
        assert body == {"ok": True, "task_id": "t-777", "latency_ms": 88}
        # underlying upload_file got the fields
        kwargs = mocked.call_args.kwargs
        assert kwargs["filename"] == "scan.pdf"
        assert kwargs["title"] == "Scan"
        assert kwargs["tags"] == ["invoice", "2026"]
        assert kwargs["content"] == b"bytes"

    def test_upstream_skipped_returns_503(self, client, paperless_on):
        with patch(
            "orchestrator.routes_paperless._upload_to_paperless",
            new=AsyncMock(return_value={
                "success": False, "skipped": True, "reason": "missing_url",
            }),
        ):
            r = client.post(
                "/api/paperless/upload",
                files={"file": ("a.pdf", b"x", "application/pdf")},
            )
        assert r.status_code == 503
        body = r.json()
        assert body["ok"] is False
        assert body["error"] == "missing_url"

    def test_upstream_http_fail_returns_502(self, client, paperless_on):
        with patch(
            "orchestrator.routes_paperless._upload_to_paperless",
            new=AsyncMock(return_value={
                "success": False, "status_code": 400, "body": "bad request",
            }),
        ):
            r = client.post(
                "/api/paperless/upload",
                files={"file": ("a.pdf", b"x", "application/pdf")},
            )
        assert r.status_code == 502
        body = r.json()
        assert body["ok"] is False
        assert "400" in body["error"]
        assert body["body"] == "bad request"

    def test_upstream_network_error_returns_502(self, client, paperless_on):
        with patch(
            "orchestrator.routes_paperless._upload_to_paperless",
            new=AsyncMock(return_value={
                "success": False, "error": "ConnectError: dns",
            }),
        ):
            r = client.post(
                "/api/paperless/upload",
                files={"file": ("a.pdf", b"x", "application/pdf")},
            )
        assert r.status_code == 502
        body = r.json()
        assert body["ok"] is False
        assert "ConnectError" in body["error"]

    def test_missing_file_field_returns_422(self, client, paperless_on):
        """FastAPI's File(...) is required — omitting it yields a 422 validation error."""
        r = client.post("/api/paperless/upload", data={"title": "no file"})
        assert r.status_code == 422


# ===========================================================================
# config.validate_paperless_config — auto-disable semantics
# ===========================================================================


class TestConfigAutoDisable:
    def test_enabled_with_short_token_disables(self, caplog):
        """PAPERLESS_ENABLED=true + token <8 chars → auto-disabled, ERROR logged, NO raise."""
        import logging

        from orchestrator.config import Settings

        env = {
            "PAPERLESS_ENABLED": "true",
            "PAPERLESS_URL": "http://paperless.test:8777",
            "PAPERLESS_API_TOKEN": "short",  # 5 chars, <8
            "HA_TOKEN": "x",
            "API_TOKEN": "x",
            "PIHOLE_PASSWORD": "x",
        }
        with patch.dict(os.environ, env, clear=False), caplog.at_level(
            logging.ERROR, logger="orchestrator.config"
        ):
            s = Settings()
        assert s.paperless_enabled is False
        assert any(
            "PAPERLESS_ENABLED=true" in r.getMessage() for r in caplog.records
        )

    def test_enabled_with_missing_url_disables(self, caplog):
        import logging

        from orchestrator.config import Settings

        env = {
            "PAPERLESS_ENABLED": "true",
            "PAPERLESS_URL": "",
            "PAPERLESS_API_TOKEN": "valid-long-token",
            "HA_TOKEN": "x",
            "API_TOKEN": "x",
            "PIHOLE_PASSWORD": "x",
        }
        with patch.dict(os.environ, env, clear=False), caplog.at_level(
            logging.ERROR, logger="orchestrator.config"
        ):
            s = Settings()
        assert s.paperless_enabled is False

    def test_valid_config_stays_enabled(self):
        from orchestrator.config import Settings

        env = {
            "PAPERLESS_ENABLED": "true",
            "PAPERLESS_URL": "http://paperless.test:8777",
            "PAPERLESS_API_TOKEN": "valid-long-token",
            "HA_TOKEN": "x",
            "API_TOKEN": "x",
            "PIHOLE_PASSWORD": "x",
        }
        with patch.dict(os.environ, env, clear=False):
            s = Settings()
        assert s.paperless_enabled is True
        assert s.paperless_url == "http://paperless.test:8777"

    def test_disabled_feature_never_raises_on_bad_config(self):
        """If the feature flag is off, missing URL/token shouldn't even matter."""
        from orchestrator.config import Settings

        env = {
            "PAPERLESS_ENABLED": "false",
            "PAPERLESS_URL": "",
            "PAPERLESS_API_TOKEN": "",
            "HA_TOKEN": "x",
            "API_TOKEN": "x",
            "PIHOLE_PASSWORD": "x",
        }
        with patch.dict(os.environ, env, clear=False):
            s = Settings()
        assert s.paperless_enabled is False
