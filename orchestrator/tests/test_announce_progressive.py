"""
reminder_manager._announce_voice(progressive=True): the reply plays from an
audio_stream.py stream URL while the rest is still being synthesized.

  - long WAV reply → play_media gets ORCHESTRATOR_URL/api/audio/s-<24>.wav,
    media_content_type audio/wav, and mark_cast() once a speaker took it
  - no speaker accepted play_media (HTTP error / connection error) →
    abort("not_played"); the caller cancelled mid-play_media →
    abort("cancelled") and the CancelledError propagates
  - short reply, non-WAV backend, progressive=False → the one-clip file path
    with a secrets.token_urlsafe(18) name
  - the volume floor still runs before play_media on the stream path

HA is mocked with respx; TTS is a fake WAV backend. No real network.
"""

from __future__ import annotations

import asyncio
import json
import re
import struct
from unittest.mock import mock_open

import httpx
import pytest
import respx
from httpx import Response

from orchestrator import audio_stream

HA = "http://ha.test:8123"
ORCH = "http://orch.test:8888"
SPEAKER = "media_player.test_room"
PLAY = f"{HA}/api/services/media_player/play_media"
VOLUME = f"{HA}/api/services/media_player/volume_set"
STREAM_URL = re.compile(rf"^{re.escape(ORCH)}/api/audio/s-[A-Za-z0-9_-]{{24}}\.wav$")
CLIP_URL = re.compile(rf"^{re.escape(ORCH)}/api/audio/[A-Za-z0-9_-]{{24}}\.(wav|mp3)$")

LONG = (
    "The library opens at nine in the morning and closes at six in the evening. "
    "Bring a light jacket, since the evening can turn cool near the water. "
    "Prices went up by 3.5 percent over the last quarter, mostly in transport. "
    "The recipe needs two cups of flour, one egg, a pinch of salt, and some patience. "
    "After that the dough rests for an hour, and then it goes into a hot oven."
)
SHORT = "Okay, the kitchen light is on now."


def _wav(seconds: float, rate: int = 8000) -> bytes:
    pcm = b"\x01\x00" * int(seconds * rate)
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    body = b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(pcm)) + pcm
    return b"RIFF" + struct.pack("<I", len(body)) + body


class _WavBackend:
    audio_format = "audio/wav"
    file_extension = "wav"

    def __init__(self, hold: bool = True) -> None:
        self.calls: list[str] = []
        # LONG's later chunks wait until the test lets them (the producer's work)
        self.hold = set(audio_stream.split_for_tts(LONG)[1:]) if hold else set()
        self.gate = asyncio.Event()

    async def synthesize(self, text: str, voice: str | None = None, style: str = "") -> bytes:
        self.calls.append(text)
        if text in self.hold:
            await self.gate.wait()
        return _wav(9.0)


class _Mp3Backend(_WavBackend):
    audio_format = "audio/mpeg"
    file_extension = "mp3"

    async def synthesize(self, text: str, voice: str | None = None, style: str = "") -> bytes:
        self.calls.append(text)
        return b"ID3 fake mp3"


@pytest.fixture(autouse=True)
async def _streams(monkeypatch):
    audio_stream._STREAMS.clear()
    yield
    tasks = list(audio_stream._TASKS)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    audio_stream._STREAMS.clear()


@pytest.fixture
def env(monkeypatch):
    """Pin HA / orchestrator URLs, stub history + routing. Returns a dict the
    test fills with the backend; spies on mark_cast/abort and the counter."""
    from orchestrator import announcement_routes, reminder_manager, shared

    monkeypatch.setattr(reminder_manager, "HA_URL", HA, raising=False)
    monkeypatch.setattr(reminder_manager, "HA_TOKEN", "tok", raising=False)
    monkeypatch.setattr(reminder_manager, "ORCHESTRATOR_URL", ORCH, raising=False)
    monkeypatch.setattr(reminder_manager, "REMINDER_SPEAKER", "", raising=False)
    monkeypatch.setattr(shared, "DND_ACTIVE", False, raising=False)
    monkeypatch.setattr(shared, "is_voice_session_active", lambda *a, **k: False)
    monkeypatch.setattr(shared, "_http", None, raising=False)
    monkeypatch.setattr(reminder_manager, "_record_announcement", lambda *a, **k: None)
    monkeypatch.setattr(announcement_routes, "route_for", lambda cat: "")

    calls: list[tuple] = []
    orig_mark, orig_abort = audio_stream.AudioStream.mark_cast, audio_stream.AudioStream.abort

    def mark_cast(self):
        calls.append(("mark_cast",))
        return orig_mark(self)

    def abort(self, result):
        calls.append(("abort", result))
        return orig_abort(self, result)

    monkeypatch.setattr(audio_stream.AudioStream, "mark_cast", mark_cast)
    monkeypatch.setattr(audio_stream.AudioStream, "abort", abort)
    counted: list[str] = []
    monkeypatch.setattr(audio_stream, "_count", counted.append)

    def use(backend):
        monkeypatch.setattr(shared, "tts_backend", backend, raising=False)
        return backend

    return {"calls": calls, "counted": counted, "use": use}


@pytest.fixture
def no_file_writes(monkeypatch):
    """The one-clip path writes /tmp/brain_audio/<name>; make it a no-op."""
    monkeypatch.setattr("os.makedirs", lambda path, exist_ok=False: None)
    monkeypatch.setattr("builtins.open", mock_open())


def _ha(mock: respx.MockRouter, play=None):
    mock.get(url__startswith=f"{HA}/api/states/").mock(
        return_value=Response(200, json={"state": "idle", "attributes": {"volume_level": 0.1}})
    )
    if play is None:
        play = Response(200, json=[])
    route = mock.post(PLAY)
    if isinstance(play, Response):
        route.mock(return_value=play)
    else:
        route.mock(side_effect=play)
    mock.post(VOLUME).mock(return_value=Response(200, json=[]))
    return route


def _played(route) -> list[dict]:
    return [json.loads(c.request.read()) for c in route.calls]


async def _announce(text: str, **kw):
    from orchestrator.reminder_manager import _announce_voice

    kw.setdefault("progressive", True)
    return await _announce_voice(
        text,
        speaker=SPEAKER,
        announcement_type="voice_reply",
        ignore_dnd=True,
        style="",
        allow_fallback=False,
        record_history=False,
        **kw,
    )


async def test_long_reply_plays_stream_url_and_marks_cast(env):
    backend = env["use"](_WavBackend())
    at_play: dict = {}

    def ok(request):
        (stream,) = audio_stream._STREAMS.values()
        at_play["chunks_done"] = stream.chunks_done
        return Response(200, json=[])

    with respx.mock(assert_all_called=False) as mock:
        play = _ha(mock, play=ok)
        result = await _announce(LONG)
    assert result["success"] is True and result["speaker"] == SPEAKER
    (body,) = _played(play)
    assert STREAM_URL.match(body["media_content_id"]), body["media_content_id"]
    assert body["media_content_type"] == "audio/wav"
    assert body["entity_id"] == SPEAKER
    assert env["calls"] == [("mark_cast",)]
    # Only the first chunk was synthesized before play_media, not the whole reply.
    chunks = audio_stream.split_for_tts(LONG)
    assert len(chunks) >= 2
    assert at_play == {"chunks_done": 1} and backend.calls[0] == chunks[0]
    stream = audio_stream.lookup(body["media_content_id"].rsplit("/", 1)[1])
    assert stream is not None and stream.speaker_key == SPEAKER and not stream.done


async def test_stream_keeps_filling_after_announce_returns(env):
    backend = env["use"](_WavBackend())
    with respx.mock(assert_all_called=False) as mock:
        play = _ha(mock)
        await _announce(LONG)
    stream = audio_stream.lookup(_played(play)[0]["media_content_id"].rsplit("/", 1)[1])
    backend.gate.set()
    await stream.producer
    assert stream.result == "complete" and stream.chunks_done == stream.chunks_total
    assert backend.calls == audio_stream.split_for_tts(LONG)


@pytest.mark.parametrize(
    "play",
    [
        pytest.param(Response(500), id="http-500"),
        pytest.param(httpx.ConnectError("refused"), id="connect-error"),
    ],
)
async def test_play_media_failure_aborts_not_played(env, play):
    env["use"](_WavBackend())
    with respx.mock(assert_all_called=False) as mock:
        _ha(mock, play=play)
        result = await _announce(LONG)
    assert result["success"] is False
    assert env["calls"] == [("abort", "not_played")]
    assert env["counted"] == ["not_played"]
    (stream,) = audio_stream._STREAMS.values()
    assert stream.done
    await asyncio.gather(stream.producer, return_exceptions=True)
    assert stream.producer.cancelled()
    assert env["counted"] == ["not_played"]


async def test_cancelled_during_play_media_aborts_cancelled(env):
    env["use"](_WavBackend())
    entered = asyncio.Event()

    async def hang(request):
        entered.set()
        await asyncio.Event().wait()

    with respx.mock(assert_all_called=False) as mock:
        _ha(mock, play=hang)
        task = asyncio.create_task(_announce(LONG))
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert env["calls"] == [("abort", "cancelled")]
    assert env["counted"] == ["cancelled"]


async def test_first_chunk_tts_failure_is_reported_like_one_clip(env):
    class _Broken(_WavBackend):
        async def synthesize(self, text, voice=None, style=""):
            raise httpx.ReadTimeout("slow")

    env["use"](_Broken())
    with respx.mock(assert_all_called=False) as mock:
        play = _ha(mock)
        result = await _announce(LONG)
    assert result["success"] is False and "ReadTimeout" in result["error"]
    assert play.calls.call_count == 0
    assert env["calls"] == [] and env["counted"] == []


async def test_short_reply_uses_one_clip_with_token_name(env, no_file_writes):
    backend = env["use"](_WavBackend(hold=False))
    with respx.mock(assert_all_called=False) as mock:
        play = _ha(mock)
        result = await _announce(SHORT)
    assert result["success"] is True
    (body,) = _played(play)
    assert CLIP_URL.match(body["media_content_id"]), body["media_content_id"]
    assert not audio_stream.is_stream_name(body["media_content_id"].rsplit("/", 1)[1])
    assert body["media_content_type"] == "audio/wav"
    assert backend.calls == [SHORT]
    assert env["calls"] == [] and env["counted"] == [] and audio_stream._STREAMS == {}


async def test_non_wav_backend_uses_one_clip_and_counts_not_wav(env, no_file_writes):
    backend = env["use"](_Mp3Backend())
    with respx.mock(assert_all_called=False) as mock:
        play = _ha(mock)
        result = await _announce(LONG)
    assert result["success"] is True
    (body,) = _played(play)
    assert CLIP_URL.match(body["media_content_id"]) and body["media_content_id"].endswith(".mp3")
    assert body["media_content_type"] == "audio/mpeg"
    assert backend.calls == [LONG]
    assert env["counted"] == ["not_wav"]


async def test_progressive_off_keeps_one_clip(env, no_file_writes):
    backend = env["use"](_WavBackend(hold=False))
    with respx.mock(assert_all_called=False) as mock:
        play = _ha(mock)
        result = await _announce(LONG, progressive=False)
    assert result["success"] is True
    (body,) = _played(play)
    assert CLIP_URL.match(body["media_content_id"])
    assert backend.calls == [LONG]
    assert env["calls"] == [] and env["counted"] == [] and audio_stream._STREAMS == {}


async def test_volume_floor_runs_before_stream_play(env):
    env["use"](_WavBackend())
    with respx.mock(assert_all_called=False) as mock:
        play = _ha(mock)
        result = await _announce(LONG, min_volume=0.5)
        volume_calls = [c for c in mock.calls if c.request.url.path.endswith("/volume_set")]
        order = [c.request.url.path.rsplit("/", 1)[1] for c in mock.calls if c.request.method == "POST"]
    assert result["success"] is True
    assert len(volume_calls) == 1 and json.loads(volume_calls[0].request.read())["volume_level"] == 0.5
    assert order == ["volume_set", "play_media"]
    assert STREAM_URL.match(_played(play)[0]["media_content_id"])


async def test_next_reply_on_same_speaker_supersedes_the_last(env, no_file_writes):
    env["use"](_WavBackend())
    with respx.mock(assert_all_called=False) as mock:
        play = _ha(mock)
        await _announce(LONG)
        first = audio_stream.lookup(_played(play)[0]["media_content_id"].rsplit("/", 1)[1])
        assert first is not None and not first.done
        await _announce(SHORT)  # "Okay." must not wait behind the old reply
    assert first.done and first.result == "superseded"
