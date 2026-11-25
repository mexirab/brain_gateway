"""Shared pytest fixtures for the ha_voice test files.

conftest.py star-imports this module, so every fixture listed in ``__all__``
is available to every test file. Give fixtures names unique across the suite
(prefix with the area) — a clash with another fixtures_*.py module is caught
by conftest at import time. Plain helper functions can be imported directly.

Fixtures:
- ``ha_voice_announce_env`` — pins everything ``reminder_manager._announce_voice``
  reads (HA URL/token, speakers, DND, routes, TTS backend, history, file writes);
- ``ha_voice_ha_client`` — factory for a real ``HomeAssistantClient`` on an
  ``httpx.MockTransport`` (``FakeHAServer``), closed at teardown;
- ``ha_voice_route_client`` — TestClient over ``routes_vision.router`` (the
  /api/tts, /api/stt and /v1/audio routes) with voice activity reset.

Helpers (import directly): ``FakeTTS``, ``build_wav``, ``ha_state_handler``,
``FakeHAServer``, ``EntityStub``, ``make_entity``, ``assert_route_error``.
"""

from __future__ import annotations

import asyncio
import json
import struct
from types import SimpleNamespace
from unittest.mock import mock_open

import httpx
import pytest

__all__: list[str] = ["ha_voice_announce_env", "ha_voice_ha_client", "ha_voice_route_client"]

HA_URL = "http://ha.test:8123"
ORCH_URL = "http://orch.test:8888"


# ---------------------------------------------------------------------------
# TTS + WAV
# ---------------------------------------------------------------------------


class FakeTTS:
    """Stand-in for tts_backend.TTSBackend: records each text and style note,
    returns ``audio`` (or raises ``exc``)."""

    def __init__(self, audio: bytes = b"\x00\x01", audio_format: str = "audio/mpeg", exc: Exception | None = None):
        self.audio = audio
        self.audio_format = audio_format
        self.file_extension = "wav" if "wav" in audio_format else "mp3"
        self.exc = exc
        self.calls: list[str] = []
        self.styles: list[str] = []

    async def synthesize(self, text: str, voice: str | None = None, style: str = "") -> bytes:
        self.calls.append(text)
        self.styles.append(style)
        if self.exc is not None:
            raise self.exc
        return self.audio


def build_wav(pcm: bytes, *, channels=1, rate=8000, bits=16, tag=1, extra=b"", data_size=None, fmt_size=16) -> bytes:
    """A RIFF/WAVE file around ``pcm``; every header field can be bent for parser tests."""
    block = channels * bits // 8
    fmt = struct.pack("<HHIIHH", tag, channels, rate, rate * block, block, bits) + b"\x00" * (fmt_size - 16)
    body = (
        b"WAVE"
        + b"fmt "
        + struct.pack("<I", fmt_size)
        + fmt
        + extra
        + b"data"
        + struct.pack("<I", len(pcm) if data_size is None else data_size)
        + pcm
    )
    return b"RIFF" + struct.pack("<I", len(body)) + body


# ---------------------------------------------------------------------------
# reminder_manager._announce_voice
# ---------------------------------------------------------------------------


def ha_state_handler(states: dict | None = None, *, default: str = "on", attributes: dict | None = None):
    """respx side_effect for GET {HA}/api/states/<entity>.

    ``states`` maps entity → a state string, an HTTP status (int) or an
    exception to raise; other entities answer ``default``. Returns
    ``(handler, seen)`` where ``seen`` lists the probed entity ids in order.
    """
    states = states or {}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        entity = request.url.path.rsplit("/", 1)[-1]
        seen.append(entity)
        val = states.get(entity, default)
        if isinstance(val, Exception):
            raise val
        if isinstance(val, int):
            return httpx.Response(val)
        return httpx.Response(200, json={"entity_id": entity, "state": val, "attributes": dict(attributes or {})})

    return handler, seen


@pytest.fixture
def ha_voice_announce_env(monkeypatch):
    """Pin every module-level knob ``_announce_voice`` reads. No network, no files.

    Returns a namespace:
    - ``routes``: category → speaker string, what ``announcement_routes.route_for``
      answers (unset → "", i.e. the ``REMINDER_SPEAKER`` fallback, "" here);
    - ``rows``: kwargs of every ``state_store.record_announcement`` (history);
    - ``backend``: the TTS backend in use (a ``FakeTTS`` until ``use()`` swaps it);
    - ``use(backend)``: install another TTS backend, returns it.
    """
    from orchestrator import announcement_routes, reminder_manager, shared

    monkeypatch.setattr(reminder_manager, "HA_URL", HA_URL, raising=False)
    monkeypatch.setattr(reminder_manager, "HA_TOKEN", "tok", raising=False)
    monkeypatch.setattr(reminder_manager, "ORCHESTRATOR_URL", ORCH_URL, raising=False)
    monkeypatch.setattr(reminder_manager, "REMINDER_SPEAKER", "", raising=False)
    monkeypatch.setattr(shared, "DND_ACTIVE", False, raising=False)
    monkeypatch.setattr(shared, "is_voice_session_active", lambda *a, **k: False)
    monkeypatch.setattr(shared, "_http", None, raising=False)
    rows: list[dict] = []
    monkeypatch.setattr(reminder_manager.state_store, "record_announcement", lambda **kw: rows.append(kw))
    # The one-clip path writes /tmp/brain_audio/<name>: swallow the write in
    # reminder_manager only (a module-level `open` shadows the builtin there);
    # the real makedirs of /tmp/brain_audio is harmless.
    monkeypatch.setattr(reminder_manager, "open", mock_open(), raising=False)
    routes: dict[str, str] = {}
    monkeypatch.setattr(announcement_routes, "route_for", lambda cat: routes.get((cat or "").lower(), ""))

    env = SimpleNamespace(routes=routes, rows=rows, backend=None)

    def use(backend):
        monkeypatch.setattr(shared, "tts_backend", backend, raising=False)
        env.backend = backend
        return backend

    env.use = use
    use(FakeTTS())
    return env


# ---------------------------------------------------------------------------
# Home Assistant
# ---------------------------------------------------------------------------


def make_entity(entity_id: str, name: str | None = "", state: str = "off", attributes: dict | None = None):
    from orchestrator.ha_integration import Entity

    return Entity(
        entity_id=entity_id,
        domain=entity_id.split(".", 1)[0],
        friendly_name="" if name is None else name,
        state=state,
        attributes=attributes if attributes is not None else {},
    )


class EntityStub:
    """The slice of HomeAssistantClient the guard / intent gate read: entities by id and domain."""

    def __init__(self, entities):
        self._entities = {e.entity_id: e for e in entities}

    def get_entity(self, entity_id):
        return self._entities.get(entity_id)

    def get_entities_by_domain(self, domain):
        return [e for e in self._entities.values() if e.domain == domain]


class FakeHAServer:
    """MockTransport handler behind a real HomeAssistantClient.

    GET /api/states answers ``states`` (``(entity_id, friendly_name)`` pairs or
    raw state dicts); POST /api/services/... is recorded in ``service_calls``.
    ``fail`` raises ConnectError, ``status`` answers that HTTP status instead,
    ``delay`` holds every answer; ``calls`` counts requests.
    """

    def __init__(self, states, *, delay: float = 0.0):
        self.states = states
        self.delay = delay
        self.fail = False
        self.status = 200
        self.calls = 0
        self.service_calls: list[tuple[str, dict]] = []

    def _state_json(self):
        return [
            s if isinstance(s, dict) else {"entity_id": s[0], "state": "off", "attributes": {"friendly_name": s[1]}}
            for s in self.states
        ]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise httpx.ConnectError("boom", request=request)
        if self.status != 200:
            return httpx.Response(self.status)
        if request.url.path == "/api/states":
            return httpx.Response(200, json=self._state_json())
        if request.url.path.startswith("/api/services/"):
            self.service_calls.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json=[])
        return httpx.Response(404)


@pytest.fixture
async def ha_voice_ha_client():
    """Factory: ``await make(states, populate=True, delay=0.0)`` → ``(client, fake)``.

    ``client`` is a real HomeAssistantClient whose HTTP goes to a ``FakeHAServer``;
    ``populate`` runs one forced refresh first. Every client is closed at teardown.
    """
    from orchestrator.ha_integration import HomeAssistantClient

    made: list = []

    async def make(states, *, populate: bool = True, delay: float = 0.0):
        fake = FakeHAServer(states, delay=delay)
        client = HomeAssistantClient(
            url=HA_URL,
            token="t",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        )
        made.append(client)
        if populate:
            await client.refresh_entities(force=True)
        return client, fake

    yield make
    for client in made:
        await client._http.aclose()


# ---------------------------------------------------------------------------
# routes_vision: /api/tts/synthesize, /api/stt/transcribe, /v1/audio/speech
# ---------------------------------------------------------------------------


@pytest.fixture
def ha_voice_route_client(monkeypatch):
    """TestClient over routes_vision.router alone, with no voice activity on record."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator import routes_vision, shared

    monkeypatch.setattr(shared, "_last_voice_activity_at", 0.0, raising=False)
    monkeypatch.setattr(shared, "_last_voice_at", 0.0, raising=False)
    app = FastAPI()
    app.include_router(routes_vision.router)
    return TestClient(app)


def assert_route_error(resp, status: int) -> None:
    """The dashboard routes' error contract: {"ok": false, "error": "<text>"}, no "success"."""
    assert resp.status_code == status
    body = resp.json()
    assert body["ok"] is False
    assert isinstance(body.get("error"), str) and body["error"]
    assert "success" not in body
