"""Tests for POST /api/tts/synthesize (dashboard chat spoken replies).

Builds a minimal FastAPI app with only routes_vision.router and stubs
``shared.tts_backend`` with a fake — no real TTS, no network. Covers the
400/413/502/503/504 status contract the frontend relies on, the WAV silence
pre-pad, and the metric / voice-activity side effects.
"""

from __future__ import annotations

import io
import wave

import httpx
import pytest
from prometheus_client import REGISTRY

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

_FRAMERATE = 16000
_ORIG_FRAMES = 160  # 10 ms


def _make_wav(frames: int = _ORIG_FRAMES, framerate: int = _FRAMERATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(framerate)
        w.writeframes(b"\x01\x02" * frames)
    return buf.getvalue()


class _FakeBackend:
    """Stand-in for orchestrator.tts_backend.TTSBackend."""

    def __init__(self, audio: bytes = b"ID3fake-mp3", audio_format: str = "audio/mpeg", exc: Exception | None = None):
        self._audio = audio
        self._format = audio_format
        self._exc = exc
        self.calls: list[str] = []

    @property
    def audio_format(self) -> str:
        return self._format

    async def synthesize(self, text: str, voice: str | None = None, style: str = "") -> bytes:
        self.calls.append(text)
        if self._exc is not None:
            raise self._exc
        return self._audio


def _count(result: str) -> float:
    return REGISTRY.get_sample_value("bgw_dashboard_tts_total", {"result": result}) or 0.0


def _latency_count() -> float:
    return REGISTRY.get_sample_value("bgw_voice_tts_seconds_count") or 0.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_voice_activity(monkeypatch):
    from orchestrator import shared

    monkeypatch.setattr(shared, "_last_voice_activity_at", 0.0)
    yield


@pytest.fixture
def set_backend(monkeypatch):
    from orchestrator import shared

    def _set(backend):
        monkeypatch.setattr(shared, "tts_backend", backend, raising=False)
        return backend

    return _set


@pytest.fixture
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator import routes_vision

    app = FastAPI()
    app.include_router(routes_vision.router)
    return TestClient(app)


URL = "/api/tts/synthesize"


def _assert_error(resp, status: int):
    assert resp.status_code == status
    body = resp.json()
    assert body["ok"] is False
    assert isinstance(body.get("error"), str) and body["error"]
    assert "success" not in body


# ---------------------------------------------------------------------------
# 400 — bad request
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        b"{not json",
        b"[1, 2]",
        b'"just a string"',
        b"{}",
        b'{"text": 42}',
        b'{"text": null}',
        b'{"text": ""}',
        b'{"text": "   \\n\\t  "}',
        b'{"other": "hello"}',
    ],
    ids=[
        "invalid-json",
        "list-body",
        "string-body",
        "missing-text",
        "int-text",
        "null-text",
        "empty-text",
        "whitespace-text",
        "wrong-key",
    ],
)
def test_bad_request_returns_400(client, set_backend, raw):
    backend = set_backend(_FakeBackend())
    before = _count("bad_request")
    lat_before = _latency_count()
    resp = client.post(URL, content=raw, headers={"Content-Type": "application/json"})
    _assert_error(resp, 400)
    assert _count("bad_request") == before + 1
    assert _latency_count() == lat_before
    assert backend.calls == []
    from orchestrator import shared

    assert shared.is_voice_session_active() is False


def test_lone_surrogate_returns_400_not_500(client, set_backend):
    backend = set_backend(_FakeBackend())
    before = _count("bad_request")
    resp = client.post(URL, content=b'{"text":"\\ud800"}', headers={"Content-Type": "application/json"})
    _assert_error(resp, 400)
    assert _count("bad_request") == before + 1
    assert backend.calls == []


# ---------------------------------------------------------------------------
# 413 — length caps
# ---------------------------------------------------------------------------


def test_exactly_max_chars_passes(client, set_backend):
    from orchestrator.routes_vision import TTS_SYNTH_MAX_CHARS

    backend = set_backend(_FakeBackend())
    resp = client.post(URL, json={"text": "a" * TTS_SYNTH_MAX_CHARS})
    assert resp.status_code == 200
    assert backend.calls == ["a" * TTS_SYNTH_MAX_CHARS]


def test_over_max_chars_returns_413(client, set_backend):
    from orchestrator.routes_vision import TTS_SYNTH_MAX_CHARS

    backend = set_backend(_FakeBackend())
    before = _count("too_long")
    resp = client.post(URL, json={"text": "a" * (TTS_SYNTH_MAX_CHARS + 1)})
    _assert_error(resp, 413)
    assert _count("too_long") == before + 1
    assert backend.calls == []


def test_char_cap_applies_after_strip(client, set_backend):
    """Surrounding whitespace doesn't count toward the cap."""
    from orchestrator.routes_vision import TTS_SYNTH_MAX_CHARS

    set_backend(_FakeBackend())
    resp = client.post(URL, json={"text": "  " + "a" * TTS_SYNTH_MAX_CHARS + "  "})
    assert resp.status_code == 200


def test_over_max_bytes_under_max_chars_returns_413(client, set_backend):
    from orchestrator.routes_vision import TTS_SYNTH_MAX_BYTES, TTS_SYNTH_MAX_CHARS

    text = "\U0001f600" * 501  # 501 chars, 2004 UTF-8 bytes
    assert len(text) <= TTS_SYNTH_MAX_CHARS
    assert len(text.encode("utf-8")) > TTS_SYNTH_MAX_BYTES
    backend = set_backend(_FakeBackend())
    before = _count("too_long")
    resp = client.post(URL, json={"text": text})
    _assert_error(resp, 413)
    assert _count("too_long") == before + 1
    assert backend.calls == []


def test_exactly_max_bytes_passes(client, set_backend):
    set_backend(_FakeBackend())
    resp = client.post(URL, json={"text": "\U0001f600" * 500})  # 2000 bytes
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Backend failures
# ---------------------------------------------------------------------------


def test_no_backend_returns_503(client, set_backend):
    set_backend(None)
    before = _count("not_configured")
    lat_before = _latency_count()
    resp = client.post(URL, json={"text": "Hello there."})
    _assert_error(resp, 503)
    assert _count("not_configured") == before + 1
    assert _latency_count() == lat_before


def _status_error() -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://tts.test/v1/audio/speech")
    resp = httpx.Response(500, request=req)
    return httpx.HTTPStatusError("server error", request=req, response=resp)


@pytest.mark.parametrize(
    "exc_factory,status,result",
    [
        (lambda: httpx.ConnectError("refused"), 503, "unreachable"),
        (lambda: httpx.ReadTimeout("slow"), 504, "timeout"),
        (lambda: httpx.WriteTimeout("slow"), 504, "timeout"),
        (lambda: httpx.PoolTimeout("slow"), 504, "timeout"),
        (_status_error, 502, "upstream_error"),
        (lambda: RuntimeError("boom"), 502, "upstream_error"),
    ],
    ids=["connect-error", "read-timeout", "write-timeout", "pool-timeout", "http-500", "unexpected"],
)
def test_backend_errors_map_to_status(client, set_backend, exc_factory, status, result):
    from orchestrator import shared

    backend = set_backend(_FakeBackend(exc=exc_factory()))
    before = _count(result)
    ok_before = _count("ok")
    lat_before = _latency_count()
    resp = client.post(URL, json={"text": "Hello there."})
    _assert_error(resp, status)
    assert backend.calls == ["Hello there."]
    assert _count(result) == before + 1
    assert _count("ok") == ok_before
    assert _latency_count() == lat_before, "latency histogram must only observe successes"
    assert shared.is_voice_session_active() is False, "failures must not hold off announcements"


def test_error_body_does_not_leak_exception_text(client, set_backend):
    set_backend(_FakeBackend(exc=RuntimeError("secret-internal-detail")))
    resp = client.post(URL, json={"text": "Hello there."})
    assert "secret-internal-detail" not in resp.text


# ---------------------------------------------------------------------------
# 200 — success
# ---------------------------------------------------------------------------


def test_success_mpeg_passthrough(client, set_backend):
    from orchestrator import shared

    audio = b"ID3\x00fake-mp3-bytes"
    backend = set_backend(_FakeBackend(audio=audio, audio_format="audio/mpeg"))
    ok_before = _count("ok")
    lat_before = _latency_count()

    resp = client.post(URL, json={"text": "  Hello there.  \n"})

    assert resp.status_code == 200
    assert backend.calls == ["Hello there."], "text must be stripped before synthesis"
    assert resp.headers["content-type"].startswith("audio/mpeg")
    assert resp.headers["cache-control"] == "no-store"
    assert resp.content == audio
    assert _count("ok") == ok_before + 1
    assert _latency_count() == lat_before + 1
    assert shared.is_voice_session_active() is True


def test_success_wav_gets_silence_prepad(client, set_backend):
    from orchestrator.routes_vision import TTS_SILENCE_PAD_MS

    src = _make_wav()
    set_backend(_FakeBackend(audio=src, audio_format="audio/wav"))
    resp = client.post(URL, json={"text": "Hello there."})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/wav")
    assert resp.headers["cache-control"] == "no-store"
    with wave.open(io.BytesIO(resp.content), "rb") as out:
        assert out.getframerate() == _FRAMERATE
        assert out.getnchannels() == 1
        assert out.getsampwidth() == 2
        added = out.getnframes() - _ORIG_FRAMES
        frames = out.readframes(out.getnframes())
    expected = int(_FRAMERATE * TTS_SILENCE_PAD_MS / 1000)
    assert added == expected
    if TTS_SILENCE_PAD_MS == 150:
        assert added == int(_FRAMERATE * 0.15)
    # Leading frames are silence, original audio follows intact.
    assert frames[: expected * 2] == b"\x00" * (expected * 2)
    assert frames[expected * 2 :] == b"\x01\x02" * _ORIG_FRAMES


def test_success_wav_corrupt_falls_back_to_raw(client, set_backend):
    """A non-parseable 'WAV' must not 500 — raw bytes are returned."""
    bad = b"not-a-real-wav"
    set_backend(_FakeBackend(audio=bad, audio_format="audio/wav"))
    ok_before = _count("ok")
    resp = client.post(URL, json={"text": "Hello there."})
    assert resp.status_code == 200
    assert resp.content == bad
    assert _count("ok") == ok_before + 1
