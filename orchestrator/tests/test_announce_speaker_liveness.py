"""
Tests for the speaker-liveness pre-check in reminder_manager._announce_voice.

HA returns 200 for play_media on an `unavailable` Cast entity, so the
announcer now GETs each target's state first:
  - `unavailable` and HTTP 404 (`missing`) → skipped as dead
  - anything else (`unknown`, `off`, 500, timeout, junk JSON) → treated live
  - all routed targets dead (non-"manual") → fall back to live reminder
    speakers, `fallback: True`, ANNOUNCE_FALLBACK_TOTAL{type} bumped
  - everything dead → success False + `unavailable`, and TTS is never run
  - invalid speaker strings never reach an HTTP request
  - metric label: configured speaker → entity id, otherwise "other"

HA is mocked with respx — no real network.
"""

from __future__ import annotations

from unittest.mock import mock_open

import httpx
import pytest
import respx
from httpx import Response

HA = "http://ha.test:8123"
STATES = f"{HA}/api/states/"
PLAY = f"{HA}/api/services/media_player/play_media"


class _CountingBackend:
    audio_format = "audio/mpeg"
    file_extension = "mp3"

    def __init__(self) -> None:
        self.calls = 0

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        self.calls += 1
        return b"\x00\x01"


def _sample(counter, labels: dict) -> float | None:
    """Read one counter's `_total` sample without walking the global registry.

    The registry walk also runs the process collector, which reads /proc via
    builtins.open — patched to mock_open by the `env` fixture.
    """
    for metric in counter.collect():
        for s in metric.samples:
            if s.name.endswith("_total") and s.labels == labels:
                return s.value
    return None


def _unavail_series(speaker: str) -> float | None:
    from orchestrator.metrics import ANNOUNCE_SPEAKER_UNAVAILABLE_TOTAL

    return _sample(ANNOUNCE_SPEAKER_UNAVAILABLE_TOTAL, {"speaker": speaker})


def _unavail(speaker: str) -> float:
    return _unavail_series(speaker) or 0.0


def _fallback(kind: str) -> float:
    from orchestrator.metrics import ANNOUNCE_FALLBACK_TOTAL

    return _sample(ANNOUNCE_FALLBACK_TOTAL, {"type": kind}) or 0.0


@pytest.fixture
def env(monkeypatch):
    """Pin HA URL, stub TTS, and route config. Returns (backend, routes dict)."""
    from orchestrator import announcement_routes, reminder_manager, shared

    monkeypatch.setattr(reminder_manager, "HA_URL", HA, raising=False)
    monkeypatch.setattr(reminder_manager, "HA_TOKEN", "tok", raising=False)
    monkeypatch.setattr(reminder_manager, "ORCHESTRATOR_URL", "http://orch.test:8888", raising=False)
    monkeypatch.setattr(reminder_manager, "REMINDER_SPEAKER", "", raising=False)
    monkeypatch.setattr(shared, "DND_ACTIVE", False, raising=False)
    monkeypatch.setattr(shared, "is_voice_session_active", lambda *a, **k: False)
    monkeypatch.setattr(shared, "_http", None, raising=False)
    backend = _CountingBackend()
    monkeypatch.setattr(shared, "tts_backend", backend, raising=False)
    monkeypatch.setattr(reminder_manager, "_record_announcement", lambda *a, **k: None)
    monkeypatch.setattr("os.makedirs", lambda path, exist_ok=False: None)
    monkeypatch.setattr("builtins.open", mock_open())

    routes: dict[str, str] = {}
    monkeypatch.setattr(announcement_routes, "route_for", lambda cat: routes.get((cat or "").lower(), ""))
    return backend, routes


def _state_router(states: dict[str, object]):
    """respx side_effect: map entity → state string, int status, or exception."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> Response:
        entity = request.url.path.rsplit("/", 1)[-1]
        seen.append(entity)
        val = states.get(entity, "on")
        if isinstance(val, Exception):
            raise val
        if isinstance(val, int):
            return Response(val)
        return Response(200, json={"entity_id": entity, "state": val, "attributes": {}})

    return handler, seen


def _played(play_route) -> list[str]:
    import json

    return [json.loads(c.request.read())["entity_id"] for c in play_route.calls]


# ---------------------------------------------------------------------------
# _speaker_state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response, expected",
    [
        (Response(200, json={"state": "unavailable"}), "unavailable"),
        (Response(404), "missing"),
        (Response(200, json={"state": "unknown"}), "unknown"),
        (Response(200, json={"state": "off"}), "off"),
        (Response(500), None),
        (Response(200, json=["not", "a", "dict"]), None),
        (Response(200, json={"attributes": {}}), None),
        (Response(200, text="<html>not json</html>"), None),
    ],
)
async def test_speaker_state_classification(env, response, expected):
    from orchestrator.reminder_manager import _speaker_state

    with respx.mock:
        respx.get(f"{STATES}media_player.k").mock(return_value=response)
        async with httpx.AsyncClient() as client:
            assert await _speaker_state(client, {}, "media_player.k") == expected


async def test_speaker_state_timeout_is_unknown(env):
    from orchestrator.reminder_manager import _speaker_state

    with respx.mock:
        respx.get(f"{STATES}media_player.k").mock(side_effect=httpx.ReadTimeout("slow"))
        async with httpx.AsyncClient() as client:
            assert await _speaker_state(client, {}, "media_player.k") is None


async def test_speaker_state_url_percent_encodes_entity(env):
    """Entity is quoted with safe='' — a `/` can't add path segments."""
    from orchestrator.reminder_manager import _speaker_state

    raw_paths: list[bytes] = []

    def handler(request: httpx.Request) -> Response:
        raw_paths.append(request.url.raw_path)
        return Response(200, json={"state": "on"})

    with respx.mock:
        respx.route(host="ha.test").mock(side_effect=handler)
        async with httpx.AsyncClient() as client:
            await _speaker_state(client, {}, "media_player.x/../../config")

    assert raw_paths == [b"/api/states/media_player.x%2F..%2F..%2Fconfig"]


async def test_filter_live_speakers_splits_dead_and_unknowable(env):
    from orchestrator.reminder_manager import _filter_live_speakers

    handler, _ = _state_router(
        {
            "media_player.a": "unavailable",
            "media_player.b": 404,
            "media_player.c": "unknown",
            "media_player.d": "off",
            "media_player.e": 500,
            "media_player.f": httpx.ConnectTimeout("x"),
        }
    )
    spk = [f"media_player.{c}" for c in "abcdef"]
    with respx.mock:
        respx.get(url__startswith=STATES).mock(side_effect=handler)
        async with httpx.AsyncClient() as client:
            live, dead = await _filter_live_speakers(client, {}, spk)

    assert live == ["media_player.c", "media_player.d", "media_player.e", "media_player.f"]
    assert dead == [("media_player.a", "unavailable"), ("media_player.b", "missing")]


# ---------------------------------------------------------------------------
# _announce_voice end-to-end (HA mocked)
# ---------------------------------------------------------------------------


async def test_unknown_and_off_speakers_still_play(env):
    backend, routes = env
    routes["reminder"] = "media_player.u,media_player.o"
    handler, _ = _state_router({"media_player.u": "unknown", "media_player.o": "off"})

    from orchestrator.reminder_manager import _announce_voice

    with respx.mock:
        respx.get(url__startswith=STATES).mock(side_effect=handler)
        play = respx.post(PLAY).mock(return_value=Response(200, json=[]))
        result = await _announce_voice("hi", announcement_type="reminder")

    assert result["success"] is True
    assert "unavailable" not in result
    assert sorted(_played(play)) == ["media_player.o", "media_player.u"]
    assert backend.calls == 1


async def test_mixed_live_and_dead_reports_unavailable(env):
    backend, routes = env
    routes["reminder"] = "media_player.live,media_player.dead"
    handler, _ = _state_router({"media_player.dead": "unavailable"})

    from orchestrator.reminder_manager import _announce_voice

    with respx.mock:
        respx.get(url__startswith=STATES).mock(side_effect=handler)
        play = respx.post(PLAY).mock(return_value=Response(200, json=[]))
        result = await _announce_voice("hi", announcement_type="reminder")

    assert result["success"] is True
    assert result["speaker"] == "media_player.live"
    assert result["unavailable"] == ["media_player.dead"]
    assert "fallback" not in result
    assert _played(play) == ["media_player.live"]


async def test_all_routed_dead_falls_back_to_live_reminder_speakers(env):
    backend, routes = env
    routes["briefing"] = "media_player.bed"
    # The reminder route shares the dead one — it must not be re-probed.
    routes["reminder"] = "media_player.bed,media_player.kitchen"
    handler, seen = _state_router({"media_player.bed": "unavailable"})
    before = _fallback("briefing")

    from orchestrator.reminder_manager import _announce_voice

    with respx.mock:
        respx.get(url__startswith=STATES).mock(side_effect=handler)
        play = respx.post(PLAY).mock(return_value=Response(200, json=[]))
        result = await _announce_voice("morning", announcement_type="briefing")

    assert result["success"] is True
    assert result["fallback"] is True
    assert result["speaker"] == "media_player.kitchen"
    assert result["unavailable"] == ["media_player.bed"]
    assert _played(play) == ["media_player.kitchen"]
    assert seen.count("media_player.bed") == 1, "already-tried dead speaker must not be re-probed"
    assert _fallback("briefing") - before == 1
    assert backend.calls == 1


async def test_manual_never_falls_back(env):
    backend, routes = env
    routes["reminder"] = "media_player.kitchen"  # live, but caller named a speaker
    handler, seen = _state_router({"media_player.office": "unavailable"})
    before = _fallback("manual")

    from orchestrator.reminder_manager import _announce_voice

    with respx.mock:
        respx.get(url__startswith=STATES).mock(side_effect=handler)
        play = respx.post(PLAY).mock(return_value=Response(200, json=[]))
        result = await _announce_voice("hi", speaker="media_player.office", announcement_type="manual")

    assert result["success"] is False
    assert result["unavailable"] == ["media_player.office"]
    assert not play.called
    assert "media_player.kitchen" not in seen
    assert backend.calls == 0
    assert _fallback("manual") == before


async def test_everything_dead_returns_failure_without_synthesis(env):
    backend, routes = env
    routes["calendar"] = "media_player.a"
    routes["reminder"] = "media_player.b"
    handler, _ = _state_router({"media_player.a": "unavailable", "media_player.b": 404})
    before = _fallback("calendar")

    from orchestrator.reminder_manager import _announce_voice

    with respx.mock:
        respx.get(url__startswith=STATES).mock(side_effect=handler)
        play = respx.post(PLAY).mock(return_value=Response(200, json=[]))
        result = await _announce_voice("hi", announcement_type="calendar")

    assert result["success"] is False
    assert result["unavailable"] == ["media_player.a", "media_player.b"]
    assert "unavailable" in result["error"]
    assert not play.called
    assert backend.calls == 0, "TTS must not run when nothing can play it"
    assert _fallback("calendar") == before


async def test_invalid_speaker_strings_never_reach_http(env):
    backend, routes = env
    requested: list[str] = []

    def handler(request: httpx.Request) -> Response:
        requested.append(str(request.url))
        if request.method == "GET":
            return Response(200, json={"state": "on"})
        return Response(200, json=[])

    from orchestrator.reminder_manager import _announce_voice

    speaker = "media_player.x/../../config, ../config,light.bedroom,Media_Player.Up,media_player.ok"
    with respx.mock:
        respx.route(host="ha.test").mock(side_effect=handler)
        result = await _announce_voice("hi", speaker=speaker, announcement_type="manual")

    assert result["success"] is True
    assert result["speaker"] == "media_player.ok"
    assert requested, "the valid speaker should have been probed and played"
    for url in requested:
        assert "config" not in url and "light" not in url and "Up" not in url, url


async def test_all_invalid_speakers_make_no_request(env):
    backend, routes = env
    from orchestrator.reminder_manager import _announce_voice

    with respx.mock:
        any_route = respx.route(host="ha.test").mock(return_value=Response(200, json={"state": "on"}))
        result = await _announce_voice("hi", speaker="../config,light.x", announcement_type="manual")

    assert result["success"] is False
    assert "No speakers configured" in result["error"]
    assert not any_route.called
    assert backend.calls == 0


# ---------------------------------------------------------------------------
# Metric labels + seeding
# ---------------------------------------------------------------------------


async def test_metric_label_configured_vs_other(env):
    backend, routes = env
    routes["reminder"] = "media_player.lbl_known"
    handler, _ = _state_router({"media_player.lbl_known": "unavailable", "media_player.lbl_stranger": "unavailable"})
    k0, o0 = _unavail("media_player.lbl_known"), _unavail("other")

    from orchestrator.reminder_manager import _announce_voice

    with respx.mock:
        respx.get(url__startswith=STATES).mock(side_effect=handler)
        respx.post(PLAY).mock(return_value=Response(200, json=[]))
        await _announce_voice("hi", speaker="media_player.lbl_known", announcement_type="manual")
        await _announce_voice("hi", speaker="media_player.lbl_stranger", announcement_type="manual")

    assert _unavail("media_player.lbl_known") - k0 == 1
    assert _unavail("other") - o0 == 1
    assert _unavail_series("media_player.lbl_stranger") is None, (
        "arbitrary caller-supplied entity must not get its own series"
    )


def test_seed_announcement_metrics_creates_zero_series(env):
    backend, routes = env
    routes["reminder"] = "media_player.seed_a, media_player.seed_b"
    routes["focus"] = "media_player.seed_c,bad/../entity"

    from orchestrator.reminder_manager import seed_announcement_metrics

    seed_announcement_metrics()

    for spk in ("media_player.seed_a", "media_player.seed_b", "media_player.seed_c"):
        assert _unavail_series(spk) == 0.0
    assert _unavail_series("other") is not None
    assert _unavail_series("bad/../entity") is None


def test_known_speakers_covers_categories_and_reminder(env):
    backend, routes = env
    routes["briefing"] = "media_player.b1"
    routes["reminder"] = "media_player.r1, media_player.r2"

    from orchestrator.reminder_manager import _known_speakers

    assert {"media_player.b1", "media_player.r1", "media_player.r2"} <= _known_speakers()


def test_known_speakers_swallows_route_errors(env, monkeypatch):
    from orchestrator import announcement_routes
    from orchestrator.reminder_manager import _known_speakers

    def boom(cat):
        raise RuntimeError("yaml broke")

    monkeypatch.setattr(announcement_routes, "route_for", boom)
    assert _known_speakers() == set()


# ---------------------------------------------------------------------------
# /api/announce → 502 when nothing played
# ---------------------------------------------------------------------------


@pytest.fixture
def api_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.api_routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_api_announce_returns_502_on_failure(api_client, monkeypatch):
    from orchestrator import api_routes

    async def fake(text, speaker=None, announcement_type="unknown"):
        assert announcement_type == "manual"
        return {"success": False, "error": "All target speakers unavailable", "unavailable": ["media_player.x"]}

    monkeypatch.setattr(api_routes, "_announce_voice", fake)
    r = api_client.post("/api/announce", json={"text": "hi", "speaker": "media_player.x"})
    assert r.status_code == 502
    body = r.json()
    assert body["ok"] is False
    assert body["unavailable"] == ["media_player.x"]
    assert "success" not in body


def test_api_announce_ok_and_suppressed_paths(api_client, monkeypatch):
    from orchestrator import api_routes

    async def ok(text, speaker=None, announcement_type="unknown"):
        return {"success": True, "speaker": "media_player.x"}

    monkeypatch.setattr(api_routes, "_announce_voice", ok)
    r = api_client.post("/api/announce", json={"text": "hi"})
    assert r.status_code == 200 and r.json()["ok"] is True

    async def suppressed(text, speaker=None, announcement_type="unknown"):
        return {"success": True, "suppressed": True, "reason": "dnd_active"}

    monkeypatch.setattr(api_routes, "_announce_voice", suppressed)
    r = api_client.post("/api/announce", json={"text": "hi"})
    assert r.status_code == 200 and r.json() == {"ok": True, "suppressed": True, "reason": "dnd_active"}
