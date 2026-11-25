"""
Tests for F-012 Paperless bridge.

Covers:
  - paperless_manager._default_tags_list / _sanitize_body (pure unit)
  - paperless_manager.upload_file (network-mocked via respx)
  - tool_handlers._reg_paperless_save (path-sanitization + happy path)
  - routes_paperless.POST /api/paperless/upload (via FastAPI TestClient)
  - config.validate_paperless_config model_validator (auto-disable semantics)

Settings are flipped on the live singleton (``memory_paperless_on``,
fixtures_memory.py). HTTP is mocked via respx so no real network calls leak
out of the container. Per-test file uploads are staged into a tmp_path inbox
so symlink / basename rules can be exercised concretely.

Metric-consistency invariant (prod-support flagged): every upload_file
exit path must bump PAPERLESS_UPLOAD_TOTAL exactly once. Asserted on
each branch below via before/after delta.
"""

from __future__ import annotations

import logging
import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from orchestrator.metrics import PAPERLESS_UPLOAD_TOTAL
from orchestrator.paperless_manager import _default_tags_list, _sanitize_body, upload_file
from orchestrator.tests._helpers import metric_value
from orchestrator.tests.fixtures_memory import PAPERLESS_BASE, make_settings
from orchestrator.tool_handlers import _reg_paperless_save

UPLOAD_PATH = "/api/documents/post_document/"


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


def _uploads(result: str, reason: str) -> float:
    return metric_value(PAPERLESS_UPLOAD_TOTAL, result=result, reason=reason)


# ===========================================================================
# _default_tags_list / _sanitize_body (pure unit)
# ===========================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        pytest.param("", [], id="empty"),
        pytest.param("receipts", ["receipts"], id="single"),
        pytest.param(" a , b ,,  c,  ", ["a", "b", "c"], id="csv-trims-and-drops-blanks"),
    ],
)
def test_default_tags_list(monkeypatch, raw, expected):
    from orchestrator.config import settings

    monkeypatch.setattr(settings, "paperless_default_tags", raw, raising=False)
    assert _default_tags_list() == expected


@pytest.mark.parametrize(
    "raw,kwargs,expected",
    [
        pytest.param("", {}, "", id="empty"),
        pytest.param(None, {}, "", id="none"),
        pytest.param("hello world", {}, "hello world", id="plain"),
        # \x01 (SOH) and \x1f (US) are below 0x20 → stripped; \x7f (DEL) is
        # >=0x7F → stripped; \t (0x09) and \n (0x0a) are whitelisted → kept.
        pytest.param("ok\x01bad\x1fstuff\x7fend\ttab\nline", {}, "okbadstuffend\ttab\nline", id="control-chars"),
        pytest.param("x" * 500, {}, "x" * 300, id="default-cap-300"),
        pytest.param("x" * 50, {"limit": 10}, "x" * 10, id="custom-cap"),
    ],
)
def test_sanitize_body(raw, kwargs, expected):
    assert _sanitize_body(raw, **kwargs) == expected


# ===========================================================================
# upload_file — respx-mocked HTTP
# ===========================================================================


class TestUploadFile:
    @pytest.mark.parametrize(
        "attr,value,reason",
        [
            pytest.param("paperless_enabled", False, "disabled", id="disabled"),
            pytest.param("paperless_url", "", "missing_url", id="missing-url"),
            pytest.param("paperless_api_token", "", "missing_token", id="missing-token"),
        ],
    )
    async def test_skipped_without_http(self, memory_paperless_on, monkeypatch, attr, value, reason):
        monkeypatch.setattr(memory_paperless_on, attr, value, raising=False)
        before = _uploads("skipped", reason)
        with respx.mock:  # no routes → any HTTP would error
            result = await upload_file(b"pdf-bytes", "a.pdf")
        assert result == {"success": False, "skipped": True, "reason": reason}
        assert _uploads("skipped", reason) == before + 1

    async def test_success_200_string_task_id(self, memory_paperless_on):
        before = _uploads("ok", "ok")
        with respx.mock(base_url=PAPERLESS_BASE) as mock:
            route = mock.post(UPLOAD_PATH).mock(return_value=Response(200, json="task-uuid-123"))
            result = await upload_file(b"pdf", "receipt.pdf", title="Groceries")
        assert route.called
        assert route.calls[0].request.headers["Authorization"] == "Token tok-abcdefgh"
        assert result["success"] is True
        assert result["task_id"] == "task-uuid-123"
        assert "latency_ms" in result
        assert _uploads("ok", "ok") == before + 1

    @pytest.mark.parametrize(
        "response,task_id",
        [
            pytest.param(Response(201, json={"task_id": "abc-999"}), "abc-999", id="201-task-id-key"),
            pytest.param(Response(202, json={"id": "id-777"}), "id-777", id="202-id-fallback"),
            # A non-JSON-looking body: resp.json() still parses "plain-task-9"
            # as a JSON string, so task_id is the string itself.
            pytest.param(Response(200, text="plain-task-9"), "plain-task-9", id="200-raw-text"),
        ],
    )
    async def test_success_task_id_shapes(self, memory_paperless_on, response, task_id):
        with respx.mock(base_url=PAPERLESS_BASE) as mock:
            mock.post(UPLOAD_PATH).mock(return_value=response)
            result = await upload_file(b"x", "a.pdf")
        assert result["success"] is True
        assert result["task_id"] == task_id

    async def test_4xx_returns_fail_with_sanitized_body(self, memory_paperless_on):
        before = _uploads("fail", "http_4xx")
        with respx.mock(base_url=PAPERLESS_BASE) as mock:
            mock.post(UPLOAD_PATH).mock(return_value=Response(400, text="bad\x01request\x7ftext"))
            result = await upload_file(b"x", "a.pdf")
        assert result["success"] is False
        assert result["status_code"] == 400
        # control chars stripped
        assert "\x01" not in result["body"]
        assert "\x7f" not in result["body"]
        assert "bad" in result["body"]
        assert _uploads("fail", "http_4xx") == before + 1

    @pytest.mark.parametrize(
        "mock_kwargs,reason,field,expected",
        [
            pytest.param({"return_value": Response(503, text="overloaded")}, "http_5xx", "status_code", 503, id="5xx"),
            pytest.param(
                {"side_effect": httpx.TimeoutException("slow")}, "timeout", "error", "TimeoutException", id="timeout"
            ),
            pytest.param(
                {"side_effect": httpx.ConnectError("dns")}, "connect_error", "error", "ConnectError", id="connect-error"
            ),
            pytest.param({"side_effect": ValueError("weird")}, "other", "error", "ValueError", id="other-exception"),
        ],
    )
    async def test_failure_swallowed_and_counted(self, memory_paperless_on, mock_kwargs, reason, field, expected):
        before = _uploads("fail", reason)
        with respx.mock(base_url=PAPERLESS_BASE) as mock:
            mock.post(UPLOAD_PATH).mock(**mock_kwargs)
            result = await upload_file(b"x", "a.pdf")
        assert result["success"] is False
        if field == "status_code":
            assert result["status_code"] == expected
        else:
            assert expected in result["error"]
        assert _uploads("fail", reason) == before + 1

    async def test_tags_merged_deduped_and_capped(self, memory_paperless_on, monkeypatch):
        """Tool-supplied + env-default tags are merged, deduplicated, and each capped at 256 chars."""
        monkeypatch.setattr(memory_paperless_on, "paperless_default_tags", "env-a, env-b, dup", raising=False)
        long_tag = "z" * 500
        with respx.mock(base_url=PAPERLESS_BASE) as mock:
            route = mock.post(UPLOAD_PATH).mock(return_value=Response(200, json="ok"))
            await upload_file(b"x", "a.pdf", tags=["tool-x", "dup", long_tag])
        body = route.calls[0].request.content.decode("utf-8", errors="replace")
        # tool tag "tool-x" present, dup only once, long_tag truncated to 256
        assert "tool-x" in body
        assert "env-a" in body
        assert "env-b" in body
        assert body.count("dup") == 1
        # Exactly 256 z's should appear, no 500-char run
        assert "z" * 256 in body
        assert "z" * 257 not in body

    async def test_title_correspondent_doctype_capped(self, memory_paperless_on):
        with respx.mock(base_url=PAPERLESS_BASE) as mock:
            route = mock.post(UPLOAD_PATH).mock(return_value=Response(200, json="ok"))
            await upload_file(b"x", "a.pdf", title="T" * 500, correspondent="C" * 500, document_type="D" * 500)
        body = route.calls[0].request.content.decode("utf-8", errors="replace")
        # Each metadata field capped at 256
        assert "T" * 256 in body and "T" * 257 not in body
        assert "C" * 256 in body and "C" * 257 not in body
        assert "D" * 256 in body and "D" * 257 not in body


# ===========================================================================
# _reg_paperless_save tool handler
# ===========================================================================


def _queued(result=None):
    """Patch paperless_manager.upload_file (the symbol the handler imports)."""
    return patch(
        "orchestrator.paperless_manager.upload_file",
        new=AsyncMock(return_value=result or {"success": True, "task_id": "t", "latency_ms": 1}),
    )


class TestPaperlessSaveTool:
    async def test_disabled_returns_friendly(self, paperless_off):
        out = await _reg_paperless_save({"filename": "a.pdf"})
        assert "disabled" in out.lower()
        assert "PAPERLESS_ENABLED" in out

    @pytest.mark.parametrize("args", [pytest.param({"filename": ""}, id="empty"), pytest.param({}, id="missing-key")])
    async def test_no_filename_asks_which(self, memory_paperless_on, args):
        out = await _reg_paperless_save(args)
        assert "which filename" in out.lower()

    async def test_null_byte_refused(self, memory_paperless_on):
        out = await _reg_paperless_save({"filename": "evil\x00.pdf"})
        assert "null byte" in out.lower()

    @pytest.mark.parametrize(
        "bad",
        ["sub/file.pdf", "..\\escape.pdf", "../etc/passwd", "a/../b.pdf"],
    )
    async def test_path_traversal_refused(self, memory_paperless_on, bad):
        out = await _reg_paperless_save({"filename": bad})
        assert out.startswith("Refused filename")
        assert "basename" in out.lower()

    async def test_absolute_path_refused(self, memory_paperless_on):
        out = await _reg_paperless_save({"filename": "/etc/passwd"})
        # /etc/passwd hits the "/" in filename check first — either wording is acceptable
        assert "refused" in out.lower()

    async def test_symlink_escape_refused(self, memory_paperless_on, paperless_inbox, tmp_path):
        """Symlink inside the inbox pointing outside must be rejected via resolve()+relative_to()."""
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

    async def test_file_not_present(self, memory_paperless_on, paperless_inbox):
        out = await _reg_paperless_save({"filename": "ghost.pdf"})
        assert "no file named" in out.lower()
        assert "ghost.pdf" in out

    async def test_file_too_large_refused_and_metric(self, memory_paperless_on, paperless_inbox, monkeypatch):
        # Cap at 1 MB for this test via env var (tool reads it fresh each call)
        monkeypatch.setenv("DOCUMENT_MAX_SIZE_MB", "1")
        big = paperless_inbox / "huge.pdf"
        big.write_bytes(b"\x00" * (2 * 1024 * 1024))

        before = _uploads("skipped", "file_too_large")
        out = await _reg_paperless_save({"filename": "huge.pdf"})
        assert out.startswith("Refused")
        assert "MB" in out
        assert _uploads("skipped", "file_too_large") == before + 1

    async def test_happy_path_queues_upload(self, memory_paperless_on, paperless_inbox):
        (paperless_inbox / "receipt.pdf").write_bytes(b"%PDF-fake-bytes")

        with _queued({"success": True, "task_id": "t-42", "latency_ms": 12}) as mocked:
            out = await _reg_paperless_save(
                {"filename": "receipt.pdf", "title": "March receipt", "tags": ["receipts", "march"]}
            )
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

    async def test_comma_string_tags_get_split(self, memory_paperless_on, paperless_inbox):
        """Defensive path: LLM passed a CSV string instead of a list."""
        (paperless_inbox / "a.pdf").write_bytes(b"x")
        with _queued() as mocked:
            await _reg_paperless_save({"filename": "a.pdf", "tags": "a, b , c"})
        assert mocked.call_args.kwargs["tags"] == ["a", "b", "c"]

    @pytest.mark.parametrize(
        "result,needles",
        [
            pytest.param(
                {"success": False, "status_code": 400, "body": "bad request"},
                ("rejected", "400", "bad request"),
                id="http-fail",
            ),
            pytest.param({"success": False, "error": "ConnectError: dns"}, ("failed", "ConnectError"), id="error"),
        ],
    )
    async def test_upload_failure_surfaces_details(self, memory_paperless_on, paperless_inbox, result, needles):
        (paperless_inbox / "a.pdf").write_bytes(b"x")
        with _queued(result):
            out = await _reg_paperless_save({"filename": "a.pdf"})
        for needle in needles:
            assert needle.lower() in out.lower()


# ===========================================================================
# routes_paperless — POST /api/paperless/upload via TestClient
# ===========================================================================


@pytest.fixture
def client():
    from orchestrator.routes_paperless import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _upload_returns(result):
    return patch("orchestrator.routes_paperless._upload_to_paperless", new=AsyncMock(return_value=result))


class TestPaperlessRoute:
    def test_disabled_returns_503(self, client, paperless_off):
        r = client.post("/api/paperless/upload", files={"file": ("a.pdf", b"x", "application/pdf")})
        assert r.status_code == 503
        body = r.json()
        assert body["ok"] is False
        assert body["error"] == "paperless_disabled"

    def test_success_returns_200_with_task_id(self, client, memory_paperless_on):
        with _upload_returns({"success": True, "task_id": "t-777", "latency_ms": 88}) as mocked:
            r = client.post(
                "/api/paperless/upload",
                files={"file": ("scan.pdf", b"bytes", "application/pdf")},
                data={"title": "Scan", "tags": ["invoice", "2026"]},
            )
        assert r.status_code == 200
        assert r.json() == {"ok": True, "task_id": "t-777", "latency_ms": 88}
        # underlying upload_file got the fields
        kwargs = mocked.call_args.kwargs
        assert kwargs["filename"] == "scan.pdf"
        assert kwargs["title"] == "Scan"
        assert kwargs["tags"] == ["invoice", "2026"]
        assert kwargs["content"] == b"bytes"

    @pytest.mark.parametrize(
        "result,status,error_part,extra",
        [
            pytest.param(
                {"success": False, "skipped": True, "reason": "missing_url"}, 503, "missing_url", {}, id="skipped-503"
            ),
            pytest.param(
                {"success": False, "status_code": 400, "body": "bad request"},
                502,
                "400",
                {"body": "bad request"},
                id="http-fail-502",
            ),
            pytest.param({"success": False, "error": "ConnectError: dns"}, 502, "ConnectError", {}, id="network-502"),
        ],
    )
    def test_upstream_failure(self, client, memory_paperless_on, result, status, error_part, extra):
        with _upload_returns(result):
            r = client.post("/api/paperless/upload", files={"file": ("a.pdf", b"x", "application/pdf")})
        assert r.status_code == status
        body = r.json()
        assert body["ok"] is False
        assert error_part in body["error"]
        for key, value in extra.items():
            assert body[key] == value

    def test_missing_file_field_returns_422(self, client, memory_paperless_on):
        """FastAPI's File(...) is required — omitting it yields a 422 validation error."""
        r = client.post("/api/paperless/upload", data={"title": "no file"})
        assert r.status_code == 422


# ===========================================================================
# config.validate_paperless_config — auto-disable semantics
# ===========================================================================


@pytest.mark.parametrize(
    "env,enabled",
    [
        # PAPERLESS_ENABLED=true + token <8 chars → auto-disabled, ERROR logged, NO raise.
        pytest.param(
            {"PAPERLESS_ENABLED": "true", "PAPERLESS_URL": PAPERLESS_BASE, "PAPERLESS_API_TOKEN": "short"},
            False,
            id="short-token",
        ),
        pytest.param(
            {"PAPERLESS_ENABLED": "true", "PAPERLESS_URL": "", "PAPERLESS_API_TOKEN": "valid-long-token"},
            False,
            id="missing-url",
        ),
        pytest.param(
            {"PAPERLESS_ENABLED": "true", "PAPERLESS_URL": PAPERLESS_BASE, "PAPERLESS_API_TOKEN": "valid-long-token"},
            True,
            id="valid",
        ),
        # Feature off: missing URL/token don't even matter.
        pytest.param(
            {"PAPERLESS_ENABLED": "false", "PAPERLESS_URL": "", "PAPERLESS_API_TOKEN": ""}, False, id="disabled"
        ),
    ],
)
def test_config_auto_disable(caplog, env, enabled):
    with caplog.at_level(logging.ERROR, logger="orchestrator.config"):
        s = make_settings(**env)
    assert s.paperless_enabled is enabled
    if enabled:
        assert s.paperless_url == PAPERLESS_BASE
    if env["PAPERLESS_API_TOKEN"] == "short":
        assert any("PAPERLESS_ENABLED=true" in r.getMessage() for r in caplog.records)
