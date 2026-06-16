"""
Tests for the bump-only volume floor on reminder_manager._announce_voice.

Covers the 2026-04-30 incident fix where the morning briefing played at
volume_level=0.10 and was inaudible. When `min_volume` is supplied,
_announce_voice GETs each speaker's state, reads attributes.volume_level,
and POSTs media_player/volume_set ONLY if current is None or below the floor.
Failures of either the GET or the volume_set must never block play_media.

Mocks HTTP via respx — no real network. Patches module-level HA_URL /
REMINDER_SPEAKER / ORCHESTRATOR_URL on `orchestrator.reminder_manager`
because they're captured at import time. Stubs `shared.tts_backend` with a
fake that returns 4 deterministic bytes so we don't pull in real TTS.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from httpx import Response

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeBackend:
    """Minimal stand-in for orchestrator.tts_backend.TTSBackend."""

    audio_format = "audio/mpeg"
    file_extension = "mp3"

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        return b"\x00\x01\x02\x03"


@pytest.fixture
def announce_env(monkeypatch, tmp_path):
    """
    Pin every module-level knob _announce_voice reads, so respx routes resolve
    against a stable HA_URL and a stable speaker list.

    Yields the (HA_URL, speaker) pair.
    """
    from orchestrator import reminder_manager, shared

    HA_URL = "http://ha.test:8123"
    SPEAKER = "media_player.bedroom"

    monkeypatch.setattr(reminder_manager, "HA_URL", HA_URL, raising=False)
    monkeypatch.setattr(reminder_manager, "HA_TOKEN", "tok", raising=False)
    monkeypatch.setattr(reminder_manager, "ORCHESTRATOR_URL", "http://orch.test:8888", raising=False)
    monkeypatch.setattr(reminder_manager, "REMINDER_SPEAKER", SPEAKER, raising=False)

    # Suppress the DND + voice-session early returns.
    monkeypatch.setattr(shared, "DND_ACTIVE", False, raising=False)
    monkeypatch.setattr(shared, "is_voice_session_active", lambda *a, **k: False)

    # TTS backend stub.
    monkeypatch.setattr(shared, "tts_backend", _FakeBackend(), raising=False)

    # Avoid hitting the real state_store / metrics inside _record_announcement.
    monkeypatch.setattr(
        reminder_manager,
        "_record_announcement",
        lambda *a, **k: None,
        raising=True,
    )

    # Redirect /tmp/brain_audio writes into a tmp dir so we don't litter.
    monkeypatch.setattr("os.makedirs", lambda path, exist_ok=False: None, raising=True)
    # Patch open() only inside reminder_manager so the audio write becomes a
    # no-op.  Easiest path: patch builtins.open scoped via mock_open.
    from unittest.mock import mock_open

    monkeypatch.setattr("builtins.open", mock_open(), raising=True)

    yield HA_URL, SPEAKER


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_min_volume_none_skips_states_and_volume_set(announce_env):
    """min_volume=None → no GET /states, no POST volume_set; only play_media."""
    from orchestrator.reminder_manager import _announce_voice

    HA_URL, SPEAKER = announce_env

    # Only register play_media — if min_volume=None correctly skips the
    # volume-floor branch, the GET /states + volume_set routes shouldn't be
    # needed.  Any unexpected request to an unmocked URL would raise.
    with respx.mock(base_url=HA_URL) as mock:
        play = mock.post("/api/services/media_player/play_media").mock(return_value=Response(200, json={}))

        result = await _announce_voice("hello", announcement_type="test", min_volume=None)

    assert result["success"] is True
    assert play.called is True


@pytest.mark.asyncio
async def test_low_current_volume_triggers_bump(announce_env):
    """current=0.10 < min_volume=0.4 → volume_set called with 0.4 BEFORE play_media."""
    from orchestrator.reminder_manager import _announce_voice

    HA_URL, SPEAKER = announce_env

    call_order: list[str] = []

    def _states_handler(request: httpx.Request) -> Response:
        call_order.append("states")
        return Response(200, json={"attributes": {"volume_level": 0.10}})

    def _vol_set_handler(request: httpx.Request) -> Response:
        call_order.append("volume_set")
        return Response(200, json={})

    def _play_handler(request: httpx.Request) -> Response:
        call_order.append("play_media")
        return Response(200, json={})

    with respx.mock(base_url=HA_URL) as mock:
        mock.get(f"/api/states/{SPEAKER}").mock(side_effect=_states_handler)
        vol_route = mock.post("/api/services/media_player/volume_set").mock(side_effect=_vol_set_handler)
        mock.post("/api/services/media_player/play_media").mock(side_effect=_play_handler)

        result = await _announce_voice("hi", announcement_type="test", min_volume=0.4)

    assert result["success"] is True
    assert vol_route.called
    body = vol_route.calls[0].request.read()
    # Floor and target entity present in JSON body.
    assert b'"volume_level": 0.4' in body or b'"volume_level":0.4' in body
    assert SPEAKER.encode() in body
    # Order: bump must happen BEFORE play_media.
    assert call_order.index("volume_set") < call_order.index("play_media")


@pytest.mark.asyncio
async def test_already_loud_speaker_not_touched(announce_env):
    """current=0.6 > min_volume=0.4 → no volume_set (bump-only contract)."""
    from orchestrator.reminder_manager import _announce_voice

    HA_URL, SPEAKER = announce_env

    # Don't register volume_set — if the bump-only contract is honored, that
    # POST never fires and respx's strict assert_all_called catches a
    # regression where current=0.6 wrongly triggers a lower-the-volume call.
    with respx.mock(base_url=HA_URL) as mock:
        states = mock.get(f"/api/states/{SPEAKER}").mock(
            return_value=Response(200, json={"attributes": {"volume_level": 0.6}})
        )
        play = mock.post("/api/services/media_player/play_media").mock(return_value=Response(200, json={}))

        result = await _announce_voice("hi", announcement_type="test", min_volume=0.4)

    assert result["success"] is True
    assert states.called is True
    assert play.called is True


@pytest.mark.asyncio
async def test_current_volume_none_triggers_bump(announce_env):
    """HA returns no volume_level (off speaker) → treat as missing → bump."""
    from orchestrator.reminder_manager import _announce_voice

    HA_URL, SPEAKER = announce_env

    with respx.mock(base_url=HA_URL) as mock:
        # attributes present but no volume_level key (typical for off speakers).
        mock.get(f"/api/states/{SPEAKER}").mock(return_value=Response(200, json={"attributes": {}}))
        vol_set = mock.post("/api/services/media_player/volume_set").mock(return_value=Response(200, json={}))
        play = mock.post("/api/services/media_player/play_media").mock(return_value=Response(200, json={}))

        result = await _announce_voice("hi", announcement_type="test", min_volume=0.4)

    assert result["success"] is True
    assert vol_set.called is True
    assert play.called is True


@pytest.mark.asyncio
async def test_volume_set_500_does_not_block_play(announce_env, caplog):
    """volume_set 500 → warning, but play_media still runs and announcement succeeds."""
    import logging

    from orchestrator.reminder_manager import _announce_voice

    HA_URL, SPEAKER = announce_env

    with respx.mock(base_url=HA_URL) as mock, caplog.at_level(logging.WARNING, logger="orchestrator.reminder_manager"):
        mock.get(f"/api/states/{SPEAKER}").mock(return_value=Response(200, json={"attributes": {"volume_level": 0.10}}))
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


@pytest.mark.asyncio
async def test_multi_speaker_bumps_each_then_plays_each(announce_env, monkeypatch):
    """
    Multi-speaker (broadcast_speakers = ['a', 'b']):
      - GET /states/<x> per speaker
      - POST volume_set per speaker (both below floor)
      - POST play_media per speaker
    """
    from orchestrator import reminder_manager
    from orchestrator.reminder_manager import _announce_voice

    HA_URL, _ = announce_env
    SPK_A = "media_player.kitchen"
    SPK_B = "media_player.office"
    monkeypatch.setattr(reminder_manager, "REMINDER_SPEAKER", f"{SPK_A},{SPK_B}", raising=False)

    with respx.mock(base_url=HA_URL) as mock:
        a_states = mock.get(f"/api/states/{SPK_A}").mock(
            return_value=Response(200, json={"attributes": {"volume_level": 0.05}})
        )
        b_states = mock.get(f"/api/states/{SPK_B}").mock(
            return_value=Response(200, json={"attributes": {"volume_level": 0.10}})
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
