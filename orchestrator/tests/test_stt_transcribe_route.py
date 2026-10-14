"""Tests for POST /api/stt/transcribe (dashboard mic button + hands-free call).

Builds a minimal FastAPI app with only routes_vision.router and swaps
``shared._http`` for an ``httpx.AsyncClient`` on a ``MockTransport`` — no real
STT, no network. Covers the 413/502/503/504 status contract the frontend
relies on, the metric / voice-activity side effects, and the sanitisation of
the upload forwarded to the STT server.
"""

from __future__ import annotations

import email
import email.policy

import httpx
import pytest
from prometheus_client import REGISTRY

STT_BASE = "http://stt.test"
URL = "/api/stt/transcribe"
AUDIO = b"\x1aE\xdf\xa3fake-webm-audio"


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _count(result: str) -> float:
    return REGISTRY.get_sample_value("bgw_dashboard_stt_total", {"result": result}) or 0.0


class _Upstream:
    """Records requests; responds with a canned response or raises."""

    def __init__(self, response: httpx.Response | None = None, exc: Exception | None = None):
        self.response = response if response is not None else httpx.Response(200, json={"text": "hello there"})
        self.exc = exc
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        return self.response


def _parse_multipart(request: httpx.Request) -> dict[str, dict]:
    """Return {field_name: {"filename", "content_type", "payload"}}."""
    raw = (
        b"Content-Type: "
        + request.headers["content-type"].encode()
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + request.content
    )
    msg = email.message_from_bytes(raw, policy=email.policy.HTTP)
    parts: dict[str, dict] = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        parts[name] = {
            "filename": part.get_filename(),
            "content_type": part.get_content_type() if part.get("content-type") else None,
            "payload": part.get_payload(decode=True),
        }
    return parts


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_voice_activity(monkeypatch):
    from orchestrator import shared

    monkeypatch.setattr(shared, "_last_voice_activity_at", 0.0)
    monkeypatch.setattr(shared, "_last_voice_at", 0.0)
    yield


@pytest.fixture(autouse=True)
def _stt_url(monkeypatch):
    from orchestrator import routes_vision

    monkeypatch.setattr(routes_vision, "STT_URL", STT_BASE)


@pytest.fixture
def upstream(monkeypatch):
    from orchestrator import shared

    def _set(response: httpx.Response | None = None, exc: Exception | None = None) -> _Upstream:
        up = _Upstream(response=response, exc=exc)
        monkeypatch.setattr(shared, "_http", httpx.AsyncClient(transport=httpx.MockTransport(up)), raising=False)
        return up

    return _set


@pytest.fixture
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator import routes_vision

    app = FastAPI()
    app.include_router(routes_vision.router)
    return TestClient(app)


def _post(client, data: bytes = AUDIO, content_type: str = "audio/webm", filename: str = "clip.webm"):
    return client.post(URL, files={"file": (filename, data, content_type)})


def _assert_error(resp, status: int):
    assert resp.status_code == status
    body = resp.json()
    assert body["ok"] is False
    assert isinstance(body.get("error"), str) and body["error"]
    assert "success" not in body


def _assert_no_voice_activity():
    from orchestrator import shared

    assert shared.is_voice_session_active() is False
    assert shared._last_voice_at == 0.0
    assert shared._last_voice_activity_at == 0.0


# ---------------------------------------------------------------------------
# Pre-upstream rejections
# ---------------------------------------------------------------------------


def test_too_large_returns_413_and_skips_upstream(client, upstream, monkeypatch):
    from orchestrator import routes_vision

    monkeypatch.setattr(routes_vision, "MAX_AUDIO_UPLOAD", 16)
    up = upstream()
    before = _count("too_large")

    resp = _post(client, data=b"x" * 17)

    _assert_error(resp, 413)
    assert up.requests == []
    assert _count("too_large") == before + 1
    _assert_no_voice_activity()


def test_exactly_max_size_is_accepted(client, upstream, monkeypatch):
    from orchestrator import routes_vision

    monkeypatch.setattr(routes_vision, "MAX_AUDIO_UPLOAD", 16)
    up = upstream()

    resp = _post(client, data=b"x" * 16)

    assert resp.status_code == 200
    assert len(up.requests) == 1


def test_not_configured_returns_503_and_skips_upstream(client, upstream, monkeypatch):
    from orchestrator import routes_vision

    monkeypatch.setattr(routes_vision, "STT_URL", "")
    up = upstream()
    before = _count("not_configured")

    resp = _post(client)

    _assert_error(resp, 503)
    assert up.requests == []
    assert _count("not_configured") == before + 1
    _assert_no_voice_activity()


# ---------------------------------------------------------------------------
# Upstream failures
# ---------------------------------------------------------------------------

_SECRET = "secret-detail-xyz"


@pytest.mark.parametrize(
    ("kwargs", "status", "result"),
    [
        ({"exc": httpx.ConnectError(f"refused {_SECRET}")}, 503, "unreachable"),
        ({"exc": httpx.ConnectTimeout(f"connect timeout {_SECRET}")}, 503, "unreachable"),
        ({"exc": httpx.ReadTimeout(f"read timeout {_SECRET}")}, 504, "timeout"),
        ({"response": httpx.Response(500, text=f"boom {_SECRET}")}, 502, "upstream_error"),
        ({"response": httpx.Response(200, text=f"<html>{_SECRET}</html>")}, 502, "upstream_error"),
        ({"response": httpx.Response(200, json=["hello", _SECRET])}, 502, "upstream_error"),
        ({"exc": RuntimeError(f"unexpected {_SECRET}")}, 502, "upstream_error"),
    ],
    ids=["connect_error", "connect_timeout", "read_timeout", "http_500", "non_json", "json_list", "runtime_error"],
)
def test_upstream_failures_map_to_status_and_metric(client, upstream, kwargs, status, result):
    up = upstream(**kwargs)
    before = _count(result)

    resp = _post(client)

    _assert_error(resp, status)
    assert len(up.requests) == 1
    assert _SECRET not in resp.text
    assert _count(result) == before + 1
    _assert_no_voice_activity()


# ---------------------------------------------------------------------------
# Success paths
# ---------------------------------------------------------------------------


def test_ok_passes_through_and_marks_voice_activity(client, upstream):
    from orchestrator import shared

    up = upstream(httpx.Response(200, json={"text": "hello there"}))
    before = _count("ok")

    resp = _post(client)

    assert resp.status_code == 200
    assert resp.json() == {"text": "hello there"}
    assert str(up.requests[0].url) == f"{STT_BASE}/v1/audio/transcriptions"
    assert _count("ok") == before + 1
    assert shared.is_voice_session_active() is True
    assert shared._last_voice_at > 0
    # The beacon is consumable by the next chat turn exactly once.
    assert shared.consume_voice_flag() is True
    assert shared.consume_voice_flag() is False


def test_ok_passes_extra_fields_through_unchanged(client, upstream):
    payload = {"text": "hello there", "language": "en", "duration": 1.5}
    upstream(httpx.Response(200, json=payload))

    resp = _post(client)

    assert resp.status_code == 200
    assert resp.json() == payload


@pytest.mark.parametrize(
    "payload",
    [{"text": "  "}, {}, {"text": ""}, {"text": None}, {"text": 42}],
    ids=["whitespace", "missing", "empty", "null", "non_string"],
)
def test_empty_transcript_passes_through_without_voice_activity(client, upstream, payload):
    upstream(httpx.Response(200, json=payload))
    before_empty = _count("empty")
    before_ok = _count("ok")

    resp = _post(client)

    assert resp.status_code == 200
    assert resp.json() == payload
    assert _count("empty") == before_empty + 1
    assert _count("ok") == before_ok
    _assert_no_voice_activity()


# ---------------------------------------------------------------------------
# Forwarded upload sanitisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("client_type", "expected_type", "expected_name"),
    [
        ("audio/webm;codecs=opus", "audio/webm", "audio.webm"),
        ("audio/mp4", "audio/mp4", "audio.m4a"),
        ("AUDIO/OGG; codecs=opus", "audio/ogg", "audio.ogg"),
        ("audio/wav", "audio/wav", "audio.wav"),
        ("text/html", "application/octet-stream", "audio.webm"),
    ],
)
def test_forwarded_upload_type_and_name_are_sanitised(client, upstream, client_type, expected_type, expected_name):
    up = upstream()

    resp = _post(client, content_type=client_type)

    assert resp.status_code == 200
    parts = _parse_multipart(up.requests[0])
    assert parts["file"]["filename"] == expected_name
    assert parts["file"]["content_type"] == expected_type
    assert parts["file"]["payload"] == AUDIO
    assert parts["model"]["payload"] == b"whisper-1"


def test_client_filename_never_reaches_upstream(client, upstream):
    up = upstream()

    resp = _post(client, filename="../../etc/passwd", content_type="audio/webm")

    assert resp.status_code == 200
    body = up.requests[0].content
    assert b"passwd" not in body
    assert b"../" not in body
    assert _parse_multipart(up.requests[0])["file"]["filename"] == "audio.webm"


def test_client_content_type_params_never_reach_upstream(client, upstream):
    up = upstream()

    resp = _post(client, content_type='audio/webm; x-evil="1"')

    assert resp.status_code == 200
    assert b"x-evil" not in up.requests[0].content
