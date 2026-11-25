"""
reminder_manager._announce_voice: speaker liveness, the volume floor, and the
flags the voice satellite added.

Speaker liveness — HA returns 200 for play_media on an `unavailable` Cast
entity, so the announcer GETs each target's state first:
  - `unavailable` and HTTP 404 (`missing`) → skipped as dead
  - anything else (`unknown`, `off`, 500, timeout, junk JSON) → treated live
  - all routed targets dead (non-"manual") → fall back to live reminder
    speakers, `fallback: True`, ANNOUNCE_FALLBACK_TOTAL{type} bumped
  - everything dead → success False + `unavailable`, and TTS is never run
  - invalid speaker strings never reach an HTTP request
  - metric label: configured speaker → entity id, otherwise "other"

Volume floor — the 2026-04-30 incident fix (the morning briefing played at
volume_level=0.10 and was inaudible): with `min_volume`, each speaker's
attributes.volume_level is read and media_player/volume_set POSTed ONLY if it
is missing or below the floor; failures never block play_media.

Satellite flags — ignore_dnd (keeps the voice-session guard), style,
allow_fallback=False (no other room's speaker, no TTS burned), record_history.

HA is mocked with respx — no real network. Setup: ``ha_voice_announce_env``.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from orchestrator import announcement_routes, api_routes, reminder_manager, shared
from orchestrator.config import settings
from orchestrator.metrics import ANNOUNCE_FALLBACK_TOTAL, ANNOUNCE_SPEAKER_UNAVAILABLE_TOTAL, TTS_ANNOUNCEMENTS_TOTAL
from orchestrator.reminder_manager import (
    _announce_voice,
    _filter_live_speakers,
    _known_speakers,
    _speaker_state,
    seed_announcement_metrics,
)
from orchestrator.tests._helpers import metric_value
from orchestrator.tests.fixtures_ha_voice import FakeTTS, ha_state_handler

HA = "http://ha.test:8123"
STATES = f"{HA}/api/states/"
PLAY = f"{HA}/api/services/media_player/play_media"


def _unavail_series(speaker: str) -> float | None:
    """The speaker's bgw_announcement_speaker_unavailable_total value, or None if it has no series."""
    for metric in ANNOUNCE_SPEAKER_UNAVAILABLE_TOTAL.collect():
        for s in metric.samples:
            if s.name.endswith("_total") and s.labels == {"speaker": speaker}:
                return s.value
    return None


def _unavail(speaker: str) -> float:
    return metric_value(ANNOUNCE_SPEAKER_UNAVAILABLE_TOTAL, speaker=speaker)


def _fallback(kind: str) -> float:
    return metric_value(ANNOUNCE_FALLBACK_TOTAL, type=kind)


@pytest.fixture
def env(ha_voice_announce_env):
    return ha_voice_announce_env


def _played(play_route) -> list[str]:
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

    with respx.mock:
        respx.get(f"{STATES}media_player.k").mock(return_value=response)
        async with httpx.AsyncClient() as client:
            assert await _speaker_state(client, {}, "media_player.k") == expected


async def test_speaker_state_timeout_is_unknown(env):

    with respx.mock:
        respx.get(f"{STATES}media_player.k").mock(side_effect=httpx.ReadTimeout("slow"))
        async with httpx.AsyncClient() as client:
            assert await _speaker_state(client, {}, "media_player.k") is None


async def test_speaker_state_url_percent_encodes_entity(env):
    """Entity is quoted with safe='' — a `/` can't add path segments."""

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

    handler, _ = ha_state_handler(
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
    backend, routes = env.backend, env.routes
    routes["reminder"] = "media_player.u,media_player.o"
    handler, _ = ha_state_handler({"media_player.u": "unknown", "media_player.o": "off"})

    with respx.mock:
        respx.get(url__startswith=STATES).mock(side_effect=handler)
        play = respx.post(PLAY).mock(return_value=Response(200, json=[]))
        result = await _announce_voice("hi", announcement_type="reminder")

    assert result["success"] is True
    assert "unavailable" not in result
    assert sorted(_played(play)) == ["media_player.o", "media_player.u"]
    assert len(backend.calls) == 1


async def test_mixed_live_and_dead_reports_unavailable(env):
    backend, routes = env.backend, env.routes
    routes["reminder"] = "media_player.live,media_player.dead"
    handler, _ = ha_state_handler({"media_player.dead": "unavailable"})

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
    backend, routes = env.backend, env.routes
    routes["briefing"] = "media_player.bed"
    # The reminder route shares the dead one — it must not be re-probed.
    routes["reminder"] = "media_player.bed,media_player.kitchen"
    handler, seen = ha_state_handler({"media_player.bed": "unavailable"})
    before = _fallback("briefing")

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
    assert len(backend.calls) == 1


async def test_manual_never_falls_back(env):
    backend, routes = env.backend, env.routes
    routes["reminder"] = "media_player.kitchen"  # live, but caller named a speaker
    handler, seen = ha_state_handler({"media_player.office": "unavailable"})
    before = _fallback("manual")

    with respx.mock:
        respx.get(url__startswith=STATES).mock(side_effect=handler)
        play = respx.post(PLAY).mock(return_value=Response(200, json=[]))
        result = await _announce_voice("hi", speaker="media_player.office", announcement_type="manual")

    assert result["success"] is False
    assert result["unavailable"] == ["media_player.office"]
    assert not play.called
    assert "media_player.kitchen" not in seen
    assert backend.calls == []
    assert _fallback("manual") == before


async def test_everything_dead_returns_failure_without_synthesis(env):
    backend, routes = env.backend, env.routes
    routes["calendar"] = "media_player.a"
    routes["reminder"] = "media_player.b"
    handler, _ = ha_state_handler({"media_player.a": "unavailable", "media_player.b": 404})
    before = _fallback("calendar")

    with respx.mock:
        respx.get(url__startswith=STATES).mock(side_effect=handler)
        play = respx.post(PLAY).mock(return_value=Response(200, json=[]))
        result = await _announce_voice("hi", announcement_type="calendar")

    assert result["success"] is False
    assert result["unavailable"] == ["media_player.a", "media_player.b"]
    assert "unavailable" in result["error"]
    assert not play.called
    assert backend.calls == [], "TTS must not run when nothing can play it"
    assert _fallback("calendar") == before


async def test_invalid_speaker_strings_never_reach_http(env):
    backend, routes = env.backend, env.routes
    requested: list[str] = []

    def handler(request: httpx.Request) -> Response:
        requested.append(str(request.url))
        if request.method == "GET":
            return Response(200, json={"state": "on"})
        return Response(200, json=[])

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
    backend, routes = env.backend, env.routes

    with respx.mock:
        any_route = respx.route(host="ha.test").mock(return_value=Response(200, json={"state": "on"}))
        result = await _announce_voice("hi", speaker="../config,light.x", announcement_type="manual")

    assert result["success"] is False
    assert "No speakers configured" in result["error"]
    assert not any_route.called
    assert backend.calls == []


# ---------------------------------------------------------------------------
# Metric labels + seeding
# ---------------------------------------------------------------------------


async def test_metric_label_configured_vs_other(env):
    backend, routes = env.backend, env.routes
    routes["reminder"] = "media_player.lbl_known"
    handler, _ = ha_state_handler({"media_player.lbl_known": "unavailable", "media_player.lbl_stranger": "unavailable"})
    k0, o0 = _unavail("media_player.lbl_known"), _unavail("other")

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
    backend, routes = env.backend, env.routes
    routes["reminder"] = "media_player.seed_a, media_player.seed_b"
    routes["focus"] = "media_player.seed_c,bad/../entity"

    seed_announcement_metrics()

    for spk in ("media_player.seed_a", "media_player.seed_b", "media_player.seed_c"):
        assert _unavail_series(spk) == 0.0
    assert _unavail_series("other") is not None
    assert _unavail_series("bad/../entity") is None


def test_known_speakers_covers_categories_and_reminder(env):
    backend, routes = env.backend, env.routes
    routes["briefing"] = "media_player.b1"
    routes["reminder"] = "media_player.r1, media_player.r2"

    assert {"media_player.b1", "media_player.r1", "media_player.r2"} <= _known_speakers()


def test_known_speakers_swallows_route_errors(env, monkeypatch):

    def boom(cat):
        raise RuntimeError("yaml broke")

    monkeypatch.setattr(announcement_routes, "route_for", boom)
    assert _known_speakers() == set()


# ---------------------------------------------------------------------------
# /api/announce → 502 when nothing played
# ---------------------------------------------------------------------------


@pytest.fixture
def api_client():

    app = FastAPI()
    app.include_router(api_routes.router)
    return TestClient(app)


def test_api_announce_returns_502_on_failure(api_client, monkeypatch):

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


# ---------------------------------------------------------------------------
# min_volume: the bump-only volume floor
# ---------------------------------------------------------------------------

VOL_SPEAKER = "media_player.bedroom"


@pytest.fixture
def volume_env(env, monkeypatch):
    """One speaker on the legacy REMINDER_SPEAKER route (no panel route)."""
    monkeypatch.setattr(reminder_manager, "REMINDER_SPEAKER", VOL_SPEAKER, raising=False)
    return env


@pytest.mark.parametrize(
    "state, bumps",
    [
        pytest.param({"state": "idle", "attributes": {"volume_level": 0.10}}, True, id="below-floor"),
        # an off speaker reports no volume_level: treated as missing → bump
        pytest.param({"state": "off", "attributes": {}}, True, id="no-volume"),
        pytest.param({"state": "idle", "attributes": {"volume_level": 0.6}}, False, id="already-loud"),
    ],
)
async def test_volume_floor_bumps_only_up_and_before_play(volume_env, state, bumps):
    order: list[str] = []

    def answer(name, response):
        def handler(request):
            order.append(name)
            return response

        return handler

    with respx.mock(base_url=HA, assert_all_called=False) as mock:
        mock.get(f"/api/states/{VOL_SPEAKER}").mock(side_effect=answer("states", Response(200, json=state)))
        vol = mock.post("/api/services/media_player/volume_set").mock(
            side_effect=answer("volume_set", Response(200, json={}))
        )
        mock.post("/api/services/media_player/play_media").mock(
            side_effect=answer("play_media", Response(200, json={}))
        )
        result = await _announce_voice("hi", announcement_type="test", min_volume=0.4)

    assert result["success"] is True
    assert "states" in order and "play_media" in order
    assert vol.called is bumps  # bump-only: never lowers a loud speaker
    if bumps:
        body = vol.calls[0].request.read()
        assert b'"volume_level": 0.4' in body or b'"volume_level":0.4' in body
        assert VOL_SPEAKER.encode() in body
        assert order.index("volume_set") < order.index("play_media")


async def test_min_volume_none_skips_states_and_volume_set(volume_env):
    """min_volume=None → no GET /states, no POST volume_set; only play_media."""

    # min_volume=None skips the volume-floor branch: exactly one GET /states
    # (the liveness pre-check) and no volume_set. volume_set isn't registered,
    # so a stray POST would hit respx's unmocked-route error and fail the play.
    with respx.mock(base_url=HA) as mock:
        states = mock.get(f"/api/states/{VOL_SPEAKER}").mock(return_value=Response(200, json={"state": "idle"}))
        play = mock.post("/api/services/media_player/play_media").mock(return_value=Response(200, json={}))

        result = await _announce_voice("hello", announcement_type="test", min_volume=None)

    assert result["success"] is True
    assert play.called is True
    assert states.call_count == 1, "only the liveness check should GET state when min_volume=None"


async def test_volume_set_500_does_not_block_play(volume_env, caplog):
    """volume_set 500 → warning, but play_media still runs and announcement succeeds."""

    with respx.mock(base_url=HA) as mock, caplog.at_level(logging.WARNING, logger="orchestrator.reminder_manager"):
        mock.get(f"/api/states/{VOL_SPEAKER}").mock(
            return_value=Response(200, json={"state": "idle", "attributes": {"volume_level": 0.10}})
        )
        vol_set = mock.post("/api/services/media_player/volume_set").mock(
            return_value=Response(500, json={"error": "boom"})
        )
        play = mock.post("/api/services/media_player/play_media").mock(return_value=Response(200, json={}))

        result = await _announce_voice("hi", announcement_type="test", min_volume=0.4)

    assert vol_set.called is True
    assert play.called is True, "play_media must still run after volume_set fails"
    assert result["success"] is True
    # Warning must mention the 500 — operator visibility.
    joined = " ".join(rec.getMessage() for rec in caplog.records)
    assert "500" in joined
    assert "VOLUME" in joined or "volume_set" in joined.lower()


async def test_multi_speaker_bumps_each_then_plays_each(volume_env, monkeypatch):
    """
    Multi-speaker (broadcast_speakers = ['a', 'b']):
      - GET /states/<x> per speaker
      - POST volume_set per speaker (both below floor)
      - POST play_media per speaker
    """

    SPK_A = "media_player.kitchen"
    SPK_B = "media_player.office"
    monkeypatch.setattr(reminder_manager, "REMINDER_SPEAKER", f"{SPK_A},{SPK_B}", raising=False)

    with respx.mock(base_url=HA) as mock:
        a_states = mock.get(f"/api/states/{SPK_A}").mock(
            return_value=Response(200, json={"state": "idle", "attributes": {"volume_level": 0.05}})
        )
        b_states = mock.get(f"/api/states/{SPK_B}").mock(
            return_value=Response(200, json={"state": "idle", "attributes": {"volume_level": 0.10}})
        )
        vol_set = mock.post("/api/services/media_player/volume_set").mock(return_value=Response(200, json={}))
        play = mock.post("/api/services/media_player/play_media").mock(return_value=Response(200, json={}))

        result = await _announce_voice("hi", announcement_type="test", min_volume=0.4)

    assert a_states.called and b_states.called
    assert vol_set.call_count == 2, "volume_set must fire once per speaker"
    assert play.call_count == 2, "play_media must fire once per speaker"

    # Distinct entity_ids in the two volume_set calls
    bodies = [vol_set.calls[i].request.read() for i in range(2)]
    joined = b" ".join(bodies)
    assert SPK_A.encode() in joined
    assert SPK_B.encode() in joined

    assert result["success"] is True
    # Both speakers should appear in the success label.
    assert SPK_A in result["speaker"]
    assert SPK_B in result["speaker"]


# ---------------------------------------------------------------------------
# The voice satellite's flags: ignore_dnd, style, allow_fallback, record_history
# ---------------------------------------------------------------------------

SAT_SPEAKER = "media_player.kitchen_test"


@pytest.fixture
def ann(env, monkeypatch):
    """A WAV backend (satellite replies are WAV) and a visible default style."""
    monkeypatch.setattr(reminder_manager, "style_for_announcement", lambda t: f"default-for-{t}")
    env.use(FakeTTS(audio_format="audio/wav"))
    return env


def _ha(router, states: dict[str, str]):
    """respx router for HA: entity states + play_media. Returns (play route, probed entities)."""
    handler, probed = ha_state_handler(states, default="idle")
    router.get(url__startswith=STATES).mock(side_effect=handler)
    return router.post(PLAY).mock(return_value=Response(200)), probed


def _tts_count(success: str, speaker: str = SAT_SPEAKER) -> float:
    return metric_value(TTS_ANNOUNCEMENTS_TOTAL, type="voice_reply", speaker=speaker, success=success)


async def test_announce_dnd_suppresses_by_default(ann, monkeypatch):
    monkeypatch.setattr(shared, "DND_ACTIVE", True)
    with respx.mock(assert_all_called=False) as router:
        play, _ = _ha(router, {})
        out = await _announce_voice("hi", speaker=SAT_SPEAKER, announcement_type="voice_reply")
    assert out == {"success": True, "suppressed": True, "reason": "dnd_active"}
    assert not play.called and ann.backend.styles == []


async def test_announce_ignore_dnd_plays(ann, monkeypatch):
    monkeypatch.setattr(shared, "DND_ACTIVE", True)
    with respx.mock as router:
        play, _ = _ha(router, {})
        out = await _announce_voice("hi", speaker=SAT_SPEAKER, announcement_type="voice_reply", ignore_dnd=True)
    assert out == {"success": True, "speaker": SAT_SPEAKER}
    assert play.call_count == 1


async def test_announce_ignore_dnd_keeps_the_voice_session_guard(ann, monkeypatch):
    monkeypatch.setattr(shared, "is_voice_session_active", lambda *a, **k: True)
    out = await _announce_voice("hi", speaker=SAT_SPEAKER, announcement_type="voice_reply", ignore_dnd=True)
    assert out["suppressed"] is True and out["reason"] == "voice_session_active"


@pytest.mark.parametrize(
    "style, expected",
    [
        pytest.param(None, "default-for-voice_reply", id="default"),
        pytest.param("excited", "excited", id="override"),
        pytest.param("", "", id="explicit-none"),
    ],
)
async def test_announce_style(ann, style, expected):
    with respx.mock as router:
        _ha(router, {})
        await _announce_voice("hi", speaker=SAT_SPEAKER, announcement_type="voice_reply", style=style)
    assert ann.backend.styles == [expected]


async def test_announce_no_fallback_when_disallowed(ann, monkeypatch):
    monkeypatch.setattr(settings, "voice_satellite_speaker", SAT_SPEAKER)
    ann.routes["reminder"] = "media_player.living_room"
    fallbacks = _fallback("voice_reply")
    dead = _unavail(SAT_SPEAKER)
    with respx.mock(assert_all_called=False) as router:
        play, probed = _ha(router, {SAT_SPEAKER: "unavailable"})
        out = await _announce_voice(
            "hi", speaker=SAT_SPEAKER, announcement_type="voice_reply", allow_fallback=False, record_history=False
        )
    assert out["success"] is False and out["unavailable"] == [SAT_SPEAKER]
    assert probed == [SAT_SPEAKER]  # the other room's speaker was never even considered
    assert not play.called and ann.backend.styles == []  # no TTS burned
    assert _fallback("voice_reply") == fallbacks
    # the satellite speaker is a known speaker: it gets its own dead-speaker series
    assert SAT_SPEAKER in reminder_manager._known_speakers()
    assert _unavail(SAT_SPEAKER) == dead + 1


async def test_announce_falls_back_by_default(ann):
    ann.routes["reminder"] = "media_player.living_room"
    with respx.mock as router:
        play, _ = _ha(router, {SAT_SPEAKER: "unavailable"})
        out = await _announce_voice("hi", speaker=SAT_SPEAKER, announcement_type="voice_reply")
    assert out["success"] is True and out["fallback"] is True
    assert out["speaker"] == "media_player.living_room"


async def test_announce_record_history_false_keeps_metrics(ann):
    ok_before = _tts_count("true")
    with respx.mock as router:
        _ha(router, {})
        out = await _announce_voice("hi", speaker=SAT_SPEAKER, announcement_type="voice_reply", record_history=False)
    assert out["success"] is True
    assert ann.rows == []
    assert _tts_count("true") == ok_before + 1


async def test_announce_record_history_false_on_failures(ann, monkeypatch):
    failed_before = _tts_count("false")
    with respx.mock(assert_all_called=False) as router:
        _ha(router, {SAT_SPEAKER: "unavailable"})
        await _announce_voice(
            "hi", speaker=SAT_SPEAKER, announcement_type="voice_reply", allow_fallback=False, record_history=False
        )
    monkeypatch.setattr(shared, "tts_backend", None, raising=False)
    await _announce_voice("hi", speaker=SAT_SPEAKER, announcement_type="voice_reply", record_history=False)
    assert ann.rows == []
    assert _tts_count("false") == failed_before + 1  # the unavailable one (no-backend has no speaker)


async def test_announce_records_history_by_default(ann):
    with respx.mock as router:
        _ha(router, {})
        await _announce_voice("hello there", speaker=SAT_SPEAKER, announcement_type="voice_reply")
    assert len(ann.rows) == 1
    assert ann.rows[0]["text"] == "hello there" and ann.rows[0]["success"] is True
