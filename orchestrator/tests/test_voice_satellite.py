"""Voice satellite bridge (orchestrator/voice_satellite.py) and the changes it
needs elsewhere: reminder_manager._announce_voice flags, cloud_brain's
skip_auto_learn_var, the config validator, log_config.DropPayloadArgsFilter
and the shutdown hook.

No network: the ESPHome device is a fake client recording the events the
bridge sends, STT/HA are respx mocks, the brain is an injected chat callable.
Clocks that drive multi-second waits (Helios wait loop, reconnect backoff) are
replaced inside voice_satellite only, so the tests stay fast and exact.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import contextvars
import inspect
import io
import logging
import os
import re
import textwrap
import time
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, mock_open, patch

import httpx
import pytest
import respx
from aioesphomeapi import APIVersion, DeviceInfo, VoiceAssistantFeature
from aioesphomeapi.core import (
    APIConnectionError,
    BadNameAPIError,
    EncryptionHelloAPIError,
    EncryptionPlaintextAPIError,
    HandshakeAPIError,
    InvalidAuthAPIError,
    InvalidEncryptionKeyAPIError,
    RequiresEncryptionAPIError,
    ResolveAPIError,
    ResolveTimeoutAPIError,
    SocketAPIError,
    SocketClosedAPIError,
    TimeoutAPIError,
)
from httpx import Response
from starlette.responses import JSONResponse

from orchestrator import metrics, shared
from orchestrator import voice_satellite as vs
from orchestrator.cloud_brain import conversation_id_var, skip_auto_learn_var
from orchestrator.voice_satellite import EnergyVad, VadConfig

_KEY = base64.b64encode(bytes(range(32))).decode()
NODE = "atom-echo-test"
SPEAKER = "media_player.kitchen_test"
STT = "http://stt.test:8003"
HA = "http://ha.test:8123"
STATES = f"{HA}/api/states/"
PLAY = f"{HA}/api/services/media_player/play_media"

UP = {"stt": True, "model": True, "tts": True}
DOWN = {"stt": False, "model": False, "tts": False}
MODEL_DOWN = {"stt": True, "model": False, "tts": True}

RUN_START = "VOICE_ASSISTANT_RUN_START"
STT_START = "VOICE_ASSISTANT_STT_START"
VAD_START = "VOICE_ASSISTANT_STT_VAD_START"
VAD_END = "VOICE_ASSISTANT_STT_VAD_END"
RUN_END = "VOICE_ASSISTANT_RUN_END"
ERROR = "VOICE_ASSISTANT_ERROR"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _tone(level: float, ms: int = 50) -> bytes:
    """Zero-mean square-wave little-endian int16 PCM whose RMS is ``level`` (0..1)."""
    sample = min(round(level * 32768), 32767)
    pair = sample.to_bytes(2, "little", signed=True) + (-sample).to_bytes(2, "little", signed=True)
    return pair * (vs.SAMPLE_RATE * ms // 1000 // 2)


def _audio(*segments: tuple[float, int]) -> bytes:
    return b"".join(_tone(level, ms) for level, ms in segments)


# Wake beep → quiet room, ~1 s of speech, then silence long enough to end it.
UTTERANCE = _audio((0.0, 200), (0.1, 1000), (0.0, 1400))
SILENCE_6S = _audio((0.0, 6000))


def _value(metric, labels: dict, suffix: str = "_total") -> float:
    """One sample of one metric (no registry walk)."""
    for m in metric.collect():
        for s in m.samples:
            if s.name.endswith(suffix) and s.labels == labels:
                return s.value
    return 0.0


def _turns(result: str, wake: str = "hey_jess", helios: str = "unchecked", device: str = NODE) -> float:
    labels = {"result": result, "wake_word": wake, "helios": helios, "device": device}
    return _value(metrics.SATELLITE_TURN_TOTAL, labels)


def _turn_seconds_count(helios: str) -> float:
    return _value(metrics.SATELLITE_TURN_SECONDS, {"helios": helios}, suffix="_count")


def _connects(result: str, device: str = NODE) -> float:
    return _value(metrics.SATELLITE_CONNECT_TOTAL, {"result": result, "device": device})


def _connected(device: str = NODE) -> float:
    return _value(metrics.SATELLITE_CONNECTED, {"device": device}, suffix="")


async def _until(cond, steps: int = 1000) -> None:
    for _ in range(steps):
        if cond():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


async def _tick(n: int = 5) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def _say(handle_audio, audio: bytes, chunk: int = 1024) -> None:
    """Stream audio the way the device does (chunks not aligned to VAD frames)."""
    for i in range(0, len(audio), chunk):
        await handle_audio(audio[i : i + chunk])


async def _drain(sat) -> None:
    """Wait for every in-flight Jess turn (not the run watchers)."""
    while True:
        turns = [t for t in sat._tasks if not t.done() and t.get_coro().__name__ == "_turn"]
        if not turns:
            return
        await asyncio.gather(*turns, return_exceptions=True)


class FakeClient:
    """Records what the bridge sends to the device."""

    def __init__(self, timeline: list | None = None) -> None:
        self.events: list[tuple[str, dict | None]] = []
        self.timeline = timeline if timeline is not None else []

    def send_voice_assistant_event(self, event_type, data) -> None:
        self.events.append((event_type.name, data))
        self.timeline.append(event_type.name)

    @property
    def names(self) -> list[str]:
        return [n for n, _ in self.events]

    @property
    def errors(self) -> list[str]:
        return [d["code"] for n, d in self.events if n == ERROR]


class FakeConn(FakeClient):
    """Stands in for aioesphomeapi.APIClient in _session / run_forever."""

    def __init__(self, *, flags=None, api_version=APIVersion(1, 12), connect_exc=None, info=None) -> None:
        super().__init__()
        if flags is None:
            flags = VoiceAssistantFeature.VOICE_ASSISTANT | VoiceAssistantFeature.API_AUDIO
        self._info = info if info is not None else DeviceInfo(voice_assistant_feature_flags=int(flags))
        self.api_version = api_version
        self.connect_exc = connect_exc
        self.on_stop = None
        self.login = None
        self.handlers: dict | None = None
        self.subscribed = asyncio.Event()
        self.unsubscribed = 0
        self.disconnected = 0

    async def connect(self, on_stop=None, login=False):
        self.login = login
        if self.connect_exc is not None:
            raise self.connect_exc
        self.on_stop = on_stop

    async def device_info(self):
        return self._info

    def subscribe_voice_assistant(self, *, handle_start, handle_stop, handle_audio):
        self.handlers = {"start": handle_start, "stop": handle_stop, "audio": handle_audio}
        self.subscribed.set()
        return self._unsub

    def _unsub(self) -> None:
        self.unsubscribed += 1

    async def disconnect(self):
        self.disconnected += 1


class FakeChat:
    """The injected brain call; records messages and the context it ran in."""

    def __init__(self, reply: str = "It is three o'clock.") -> None:
        self.reply = reply
        self.status = 200
        self.exc: Exception | None = None
        self.block: asyncio.Event | None = None
        self.calls: list[list[dict]] = []
        self.ctx: list[tuple[bool, str | None]] = []
        self.timeline: list | None = None

    async def __call__(self, messages):
        self.calls.append([dict(m) for m in messages])
        self.ctx.append((skip_auto_learn_var.get(), conversation_id_var.get()))
        if self.timeline is not None:
            self.timeline.append("chat")
        if self.block is not None:
            await self.block.wait()
        if self.exc is not None:
            raise self.exc
        body = {"choices": [{"index": 0, "message": {"role": "assistant", "content": self.reply}}]}
        return JSONResponse(body, status_code=self.status)


@pytest.fixture
async def make_sat(monkeypatch):
    """Satellite factory with a FakeClient attached; cancels leftover tasks."""
    # _turn_inner marks voice activity; keep that from leaking into other tests.
    monkeypatch.setattr(shared, "_last_voice_activity_at", 0.0)
    made = []

    def _make(*, chat=None, client="fake", **kw):
        params = {
            "chat": chat or FakeChat(),
            "host": "10.0.0.50",
            "port": 6053,
            "api_key": _KEY,
            "node_name": NODE,
            "speaker": SPEAKER,
        }
        params.update(kw)
        s = vs.Satellite(**params)
        s._client = FakeClient() if client == "fake" else client
        made.append(s)
        return s

    yield _make
    for s in made:
        await s._cancel_tasks()


@pytest.fixture
def sat(make_sat):
    return make_sat()


@pytest.fixture
def pipeline(sat, monkeypatch):
    """Helios ready, STT returns words, playback succeeds — all on ``sat``."""
    p = SimpleNamespace(
        status=AsyncMock(return_value=dict(UP)),
        stt=AsyncMock(return_value="what time is it"),
        speak=AsyncMock(return_value="ok"),
    )
    monkeypatch.setattr(sat, "_service_status", p.status)
    monkeypatch.setattr(sat, "_transcribe", p.stt)
    monkeypatch.setattr(sat, "_speak", p.speak)
    return p


@pytest.fixture
async def later(monkeypatch):
    """Fire the bridge's delayed jess-busy error at once; record the delay."""
    loop = asyncio.get_running_loop()
    real = loop.call_later
    delays: list[float] = []

    def fake(delay, callback, *args, **kwargs):
        if getattr(callback, "__name__", "") == "_send_error":
            delays.append(delay)
            return real(0, callback, *args, **kwargs)
        return real(delay, callback, *args, **kwargs)

    monkeypatch.setattr(loop, "call_later", fake)
    return delays


class _Shim:
    """A module stand-in: overridden attributes, everything else from ``real``."""

    def __init__(self, real, **overrides) -> None:
        self._real = real
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def clock(monkeypatch):
    """Fake monotonic clock + sleep for voice_satellite only.

    sleep() advances the clock; ``cancel_at`` makes the Nth sleep raise
    CancelledError, ``block_at`` makes the Nth sleep wait forever.
    """
    real_sleep = asyncio.sleep
    c = SimpleNamespace(now=10_000.0, sleeps=[], cancel_at=None, block_at=None)

    async def fake_sleep(delay, *a, **k):
        c.sleeps.append(delay)
        c.now += delay
        if c.cancel_at is not None and len(c.sleeps) >= c.cancel_at:
            raise asyncio.CancelledError
        if c.block_at is not None and len(c.sleeps) >= c.block_at:
            await asyncio.Event().wait()
        await real_sleep(0)

    monkeypatch.setattr(vs, "asyncio", _Shim(asyncio, sleep=fake_sleep))
    monkeypatch.setattr(vs, "time", _Shim(time, monotonic=lambda: c.now))
    return c


# ---------------------------------------------------------------------------
# EnergyVad
# ---------------------------------------------------------------------------


def _feed(vad: EnergyVad, levels, start_ms: int = 50) -> list[tuple[int, str]]:
    """One level per 50 ms frame from ``start_ms``; the (time, event) pairs."""
    out = []
    for i, level in enumerate(levels):
        now = start_ms + i * 50
        event = vad.feed(level, now)
        if event:
            out.append((now, event))
    return out


# Frame levels (AC RMS) measured on the ATOM Echo S3R in a real room,
# 2026-11-11: silence 0.025-0.09, median 0.042; a spoken question 0.19-0.24.
ROOM = [0.035, 0.042, 0.051, 0.030, 0.045, 0.064, 0.027, 0.042, 0.038, 0.055, 0.044, 0.033]
QUESTION = [0.12, 0.19, 0.235, 0.21, 0.08, 0.17, 0.2, 0.127, 0.15, 0.19, 0.11, 0.068]
# Words with short gaps: never steady, so it can't pass for the room.
TALKING = [0.2] * 6 + [0.02] * 3


def _room(ms: int) -> list[float]:
    n = ms // 50
    return (ROOM * (n // len(ROOM) + 1))[:n]


def _last_frame_ms(n_frames: int) -> int:
    return 50 + (n_frames - 1) * 50


# -- silent-room mechanics: nothing is learned until 300 ms (start-up frames
#    and the 200 ms means that still include them), and until 1.5 s the room
#    level is held in 0.036..0.045. Speech that starts early therefore meets
#    the 0.045 room: speech bar 0.081, end bar 0.06075 -----------------------


def test_vad_speech_start_needs_min_speech_ms():
    vad = EnergyVad()
    # silent 50..200, 0.1 from 250: the 200 ms mean reaches the 0.081 bar at
    # 400 (four loud frames), speech 150 ms later
    events = _feed(vad, [0.0] * 4 + [0.1] * 8)
    assert events == [(550, "speech-start")]
    assert vad.speaking and vad.heard_speech
    assert vad.speech_at == 400


def test_vad_single_click_is_not_speech():
    vad = EnergyVad()
    events = _feed(vad, [0.0] * 4 + [0.3] + [0.0] * 20)
    assert events == []
    assert not vad.heard_speech


@pytest.mark.parametrize("frames", [2, 3])
def test_vad_short_burst_is_not_speech(frames):
    vad = EnergyVad()
    events = _feed(vad, [0.0] * 4 + [0.1] * frames + [0.0] * 20)
    assert events == []
    assert not vad.heard_speech


def test_vad_speech_end_after_silence_ms():
    vad = EnergyVad()
    # loud 250..700; the 200 ms mean last reaches the bar at 700
    events = _feed(vad, [0.0] * 4 + [0.1] * 10 + [0.0] * 40)
    assert events == [(550, "speech-start"), (1900, "speech-end")]
    assert not vad.speaking


def test_vad_softer_words_keep_the_utterance_alive():
    vad = EnergyVad()
    # 0.07 is under the 0.081 speech bar but over the 0.06075 end bar: no
    # "loud moment" after 800, yet the mean over the last 1.2 s keeps the
    # utterance open until silence outweighs the soft words
    levels = [0.0] * 4 + [0.1] * 10 + [0.07] * 40 + [0.0] * 30
    events = _feed(vad, levels)
    last_soft = _last_frame_ms(4 + 10 + 40)
    assert last_soft == 2700
    assert events == [(550, "speech-start"), (2900, "speech-end")]


def test_vad_no_speech_at_5s():
    events = _feed(EnergyVad(), [0.0] * 120)
    assert events[0] == (5000, "no-speech")


def test_vad_no_speech_waits_for_a_burst_to_end():
    # loud at 4950 and 5000: above the (0.03) bar 5000..5100, still deciding
    levels = [0.0] * 98 + [0.1, 0.1] + [0.0] * 6
    events = _feed(EnergyVad(), levels)
    assert events[0] == (5150, "no-speech")


def test_vad_max_utterance_at_15s():
    vad = EnergyVad()
    # talking non-stop from 250 ms; the 200 ms mean reaches the bar at 300
    levels = [0.0] * 4 + (TALKING * 40)[:302]
    events = _feed(vad, levels)
    # the cap counts from the first frame above the bar, not from speech-start
    assert events == [(450, "speech-start"), (15_300, "max-utterance")]
    assert not vad.speaking


# -- the measured room ------------------------------------------------------


def test_vad_noisy_room_question_ends_about_a_second_after_it():
    """2026-11-11 regression: the room sat above the old end bar (0.027), so
    every question ran to the 15 s cap."""
    vad = EnergyVad()
    events = _feed(vad, _room(1500) + QUESTION + _room(5000))
    assert [e for _, e in events] == ["speech-start", "speech-end"]
    last_word = _last_frame_ms(30 + len(QUESTION))
    assert last_word + 1100 <= events[1][0] <= last_word + 1600
    assert 0.025 <= vad.floor <= 0.05


def test_vad_noisy_room_question_straight_after_the_beep():
    vad = EnergyVad()
    events = _feed(vad, QUESTION * 2 + _room(4000))
    assert [e for _, e in events] == ["speech-start", "speech-end"]
    assert events[1][0] <= _last_frame_ms(2 * len(QUESTION)) + 1600


def test_vad_noisy_room_without_speech_is_no_speech():
    vad = EnergyVad()
    events = _feed(vad, _room(6000))
    assert events[0] == (5000, "no-speech")
    assert not vad.heard_speech
    assert 0.025 <= vad.floor <= 0.05


def test_vad_near_silent_mic_start_does_not_turn_the_room_into_speech():
    # the mic can read near-silent for a few frames as it starts
    vad = EnergyVad()
    events = _feed(vad, [0.0] * 4 + _room(6000))
    assert [e for _, e in events][:1] == ["no-speech"]
    assert not vad.heard_speech


def test_vad_noise_blip_does_not_restart_the_end_clock():
    vad = EnergyVad()
    levels = _room(1500) + QUESTION + _room(500) + [0.09] + _room(3000)
    events = _feed(vad, levels)
    assert [e for _, e in events] == ["speech-start", "speech-end"]
    assert events[1][0] <= _last_frame_ms(30 + len(QUESTION)) + 1600


def test_vad_soft_words_between_the_bars_are_not_cut_off_in_a_noisy_room():
    vad = EnergyVad()
    soft = [0.07, 0.078, 0.072, 0.08, 0.074, 0.069, 0.079, 0.075]
    body = _room(1000) + QUESTION + soft + _room(150) + soft
    events = _feed(vad, body + _room(4000))
    assert [e for _, e in events] == ["speech-start", "speech-end"]
    assert events[1][0] >= _last_frame_ms(len(body)) + 800


def test_vad_floor_is_frozen_while_speaking():
    vad = EnergyVad()
    _feed(vad, _room(1500) + QUESTION[:8])
    assert vad.speaking
    before = vad.floor
    _feed(vad, TALKING * 2, start_ms=50 + (30 + 8) * 50)
    assert vad.floor == before


def test_vad_learns_a_quiet_room():
    vad = EnergyVad()
    _feed(vad, [0.004] * 40)  # past the 1.5 s bootstrap
    assert vad.floor == pytest.approx(0.004)
    assert vad.threshold == pytest.approx(0.03)  # the silent-room minimum
    assert vad.end_threshold == pytest.approx(0.015)


def test_vad_background_noise_raises_the_bar():
    vad = EnergyVad()
    _feed(vad, [0.04] * 60)
    assert vad.floor == pytest.approx(0.04)
    assert vad.threshold == pytest.approx(0.072)
    # 0.06 would be speech in a silent room; here it is background
    events = _feed(vad, [0.06] * 40, start_ms=3050)
    assert "speech-start" not in [e for _, e in events]
    assert not vad.heard_speech


def test_vad_room_louder_than_the_cap_is_learned_after_bootstrap():
    vad = EnergyVad()
    room = [0.055, 0.065, 0.06, 0.058] * 15  # 3 s, above the 0.045 cap
    events = _feed(vad, room)
    assert not vad.heard_speech, events
    assert vad.floor > 0.05


def test_vad_long_question_is_not_cut_off():
    """Speech from this device is steady (smoothed p90/p25 ~1.3): nothing may
    re-learn the room from it mid-question (an escape hatch that did cut
    questions over ~3.5 s off)."""
    vad = EnergyVad()
    body = _room(1000) + QUESTION * 8  # ~4.8 s of talking
    events = _feed(vad, body + _room(4000))
    assert [e for _, e in events] == ["speech-start", "speech-end"]
    last_word = _last_frame_ms(len(body))
    assert last_word + 1100 <= events[1][0] <= last_word + 1600


def test_vad_known_limit_loud_steady_sound_runs_to_the_cap():
    """Documented limit: a sound much louder than the tuning room (TV on)
    right after the wake word is taken for speech and recorded to the cap
    (counted as listen_end reason=max_utterance)."""
    vad = EnergyVad()
    events = _feed(vad, [0.11, 0.1, 0.12, 0.105, 0.115] * 70)
    assert [e for _, e in events] == ["speech-start", "max-utterance"]


def test_vad_first_words_cannot_become_the_room():
    """User talks straight after the beep: their first words can't set the room level."""
    vad = EnergyVad()
    assert vad.floor == pytest.approx(0.045)  # before any audio: the bootstrap cap
    _feed(vad, [0.2] * 8)
    assert vad.floor == pytest.approx(0.045)


def test_vad_custom_config_and_peak():
    vad = EnergyVad(VadConfig(smooth_ms=50, min_loud_frames=1, min_speech_ms=50, silence_ms=100, skip_ms=0))
    events = _feed(vad, [0.3, 0.3, 0.0, 0.0, 0.0])
    assert events == [(100, "speech-start"), (200, "speech-end")]
    assert vad.peak == pytest.approx(0.3)
    assert vad.max_level == pytest.approx(0.3)


def test_percentile_helper():
    assert vs._percentile([0.4, 0.1, 0.3, 0.2], 0.25) == 0.2
    assert vs._percentile([0.5], 0.9) == 0.5
    assert vs._percentile(list(range(40)), 0.25) == 10


# -- the values the "listening ended" log line reports ----------------------


def test_vad_quiet_ms_and_tail_mean_follow_the_end_decision():
    vad = EnergyVad()
    _feed(vad, [0.0] * 4 + [0.1] * 6)  # 50..500: loud, not speech yet
    assert (vad.quiet_ms, vad.tail_mean, vad.speech_at) == (0, 0.0, None)
    _feed(vad, [0.1] * 4, start_ms=550)  # speech-start at 550, loud to 700
    assert vad.speaking and vad.quiet_ms == 0
    assert vad.tail_mean == pytest.approx(0.1)
    # quiet from 750; the 200 ms mean last reached the speech bar at 700
    _feed(vad, [0.0] * 6, start_ms=750)
    assert vad.quiet_ms == 300
    assert vad.tail_mean == pytest.approx(0.3 / 9)  # 600..1000: three loud frames, six quiet
    # the tail is the last silence_ms only: at 1850 just the 700 frame is left
    assert _feed(vad, [0.0] * 17, start_ms=1050) == []
    assert vad.tail_mean == pytest.approx(0.1 / 24)
    assert _feed(vad, [0.0], start_ms=1900) == [(1900, "speech-end")]
    assert vad.quiet_ms == vad.cfg.silence_ms
    assert vad.tail_mean < vad.end_threshold < vad.threshold


def test_vad_max_level_is_the_smoothed_level_not_the_raw_peak():
    vad = EnergyVad()
    _feed(vad, [0.0] * 4 + [0.3] + [0.0] * 20)
    assert vad.peak == pytest.approx(0.3)
    assert vad.max_level == pytest.approx(0.3 / 4)  # one loud frame in the 200 ms mean


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "frame, expected",
    [
        pytest.param(b"", 0.0, id="empty"),
        pytest.param(b"\x01", 0.0, id="one-byte"),
        pytest.param(_tone(0.5), 0.5, id="half-scale"),
        pytest.param(
            ((-32768).to_bytes(2, "little", signed=True) + (32767).to_bytes(2, "little", signed=True)) * 400,
            1.0,
            id="full-scale",
        ),
        pytest.param((3000).to_bytes(2, "little", signed=True) * 800, 0.0, id="dc-offset-only"),
        pytest.param(
            b"".join((2000 + d).to_bytes(2, "little", signed=True) for _ in range(400) for d in (3000, -3000)),
            3000 / 32768,
            id="tone-on-dc-offset",
        ),
        pytest.param(_tone(0.25) + b"\x7f", 0.25, id="odd-length"),
        pytest.param(
            (3000).to_bytes(2, "little", signed=True) + (-3000).to_bytes(2, "little", signed=True),
            3000 / 32768,
            id="mixed-sign",
        ),
        pytest.param(bytes(1600), 0.0, id="digital-silence"),
    ],
)
def test_frame_rms(frame, expected):
    assert vs.frame_rms(frame) == pytest.approx(expected, abs=1e-4)


def test_frame_bytes_is_50ms():
    assert vs.FRAME_BYTES == 1600


def test_pcm_to_wav_roundtrip():
    pcm = _audio((0.1, 100), (0.0, 50))
    with wave.open(io.BytesIO(vs.pcm_to_wav(pcm)), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16_000)
        assert w.getnframes() == len(pcm) // 2
        assert w.readframes(w.getnframes()) == pcm


@pytest.mark.parametrize(
    "reply, expected",
    [
        ("See [the forecast](https://weather.example/today) now.", "See the forecast now."),
        ("Check https://example.com/a?b=1 for details.", "Check for details."),
        ("**Bold** and `code` and # heading", "Bold and code and heading"),
        ("line one\n\n- line two", "line one - line two"),
        ("[https://x.example](https://x.example)", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_speakable(reply, expected):
    assert vs.speakable(reply) == expected


def test_speakable_caps_at_a_sentence_end():
    text = "".join(f"This is sentence number {i:03d} of the reply. " for i in range(40))
    out = vs.speakable(text)
    assert vs.MAX_REPLY_CHARS // 2 < len(out) <= vs.MAX_REPLY_CHARS
    assert out.endswith(".")
    assert text.startswith(out)


@pytest.mark.parametrize("end", ["!", "?"])
def test_speakable_caps_at_exclamation_or_question(end):
    text = "".join(f"Sentence {i:03d} is here{end} " for i in range(80))
    out = vs.speakable(text)
    assert out.endswith(end) and len(out) <= vs.MAX_REPLY_CHARS


def test_speakable_hard_cut_without_late_sentence_end():
    assert len(vs.speakable("word " * 400)) == vs.MAX_REPLY_CHARS
    # the only sentence end is in the first half: not worth losing 90% of it
    assert len(vs.speakable("Short. " + "x" * 2000)) == vs.MAX_REPLY_CHARS


def _resp(body: bytes | str | None, status: int = 200):
    return SimpleNamespace(body=body, status_code=status)


@pytest.mark.parametrize(
    "response, expected",
    [
        pytest.param(
            JSONResponse({"choices": [{"message": {"content": " Hi there. "}}]}), "Hi there.", id="jsonresponse"
        ),
        pytest.param({"choices": [{"message": {"content": "Hi."}}]}, "Hi.", id="dict"),
        pytest.param(_resp(b"not json"), "", id="bad-json"),
        pytest.param(_resp(b"\x80\xff"), "", id="bad-utf8"),
        pytest.param(_resp(b"[1, 2]"), "", id="json-list"),
        pytest.param(_resp(b'{"choices": []}'), "", id="no-choices"),
        pytest.param(_resp(b'{"choices": [null]}'), "", id="null-choice"),
        pytest.param(_resp(b'{"choices": "x"}'), "", id="choices-str"),
        pytest.param(_resp(b'{"choices": ["x"]}'), "", id="choice-str"),
        pytest.param(_resp(b'{"choices": [{"message": "x"}]}'), "", id="message-str"),
        pytest.param(_resp(b'{"choices": [{"message": {"content": null}}]}'), "", id="null-content"),
        pytest.param(_resp(b'{"choices": [{"message": {"content": ["a"]}}]}'), "", id="list-content"),
        pytest.param(_resp("str body"), "", id="str-body"),
        pytest.param(object(), "", id="no-body"),
    ],
)
def test_reply_text(response, expected):
    assert vs.reply_text(response) == expected


@pytest.mark.parametrize(
    "phrase, label",
    [
        (None, "button"),
        ("", "button"),
        ("Hey Jess", "hey_jess"),
        ("hey, jess!", "hey_jess"),
        ("Okay Jess", "okay_jess"),
        ("OK Jess", "okay_jess"),
        ("Okay Nabu", "okay_nabu"),
        ("Alexa", "other"),
        ("say my secret", "other"),
    ],
)
def test_wake_word_label(phrase, label):
    assert vs.wake_word_label(phrase) == label


# ---------------------------------------------------------------------------
# one device run, end to end
# ---------------------------------------------------------------------------


async def test_ok_run_event_sequence(sat, pipeline):
    before = _turns("ok", helios="awake")
    hist_before = _turn_seconds_count("awake")

    assert await sat.handle_start("conv-1", 0, None, "Hey Jess") == 0
    await _say(sat.handle_audio, UTTERANCE)
    await _drain(sat)

    assert sat._client.names == [RUN_START, STT_START, VAD_START, RUN_END]
    assert VAD_END not in sat._client.names
    assert _turns("ok", helios="awake") == before + 1
    assert _turn_seconds_count("awake") == hist_before + 1
    assert sat._busy is False and sat._run is None


async def test_run_end_goes_out_before_the_turn_starts(sat, pipeline):
    """RUN_END alone, sent the moment speech ends — STT/brain/speaker come after."""
    timeline = sat._client.timeline

    async def stt(pcm):
        timeline.append("stt")
        return "what time is it"

    async def speak(spoken):
        timeline.append("speak")
        return "ok"

    pipeline.stt.side_effect = stt
    pipeline.speak.side_effect = speak
    sat._chat.timeline = timeline

    await sat.handle_start("c", 0, None, "Hey Jess")
    await _say(sat.handle_audio, UTTERANCE)
    # handle_audio never yields, so the turn task has not run yet
    assert timeline[-1] == RUN_END
    assert pipeline.stt.await_count == 0
    assert sat._busy is True

    await _drain(sat)
    assert timeline == [RUN_START, STT_START, VAD_START, RUN_END, "stt", "chat", "speak"]
    assert VAD_END not in timeline


async def test_full_pipeline_through_stt_and_announce(sat, monkeypatch):
    """Real _transcribe (respx STT) and real _speak (mocked _announce_voice)."""
    from orchestrator import reminder_manager, voice_style

    monkeypatch.setattr(sat, "_service_status", AsyncMock(return_value=dict(UP)))
    monkeypatch.setattr(vs._settings, "stt_url", STT)
    monkeypatch.setattr(shared, "_http", None, raising=False)
    announce = AsyncMock(return_value={"success": True, "speaker": SPEAKER})
    monkeypatch.setattr(reminder_manager, "_announce_voice", announce)
    monkeypatch.setattr(voice_style, "current_reply_style", lambda: "")
    sat._chat.reply = "Sure — [the timer](https://x.example/t) is set for **ten** minutes."

    with respx.mock:
        route = respx.post(f"{STT}/v1/audio/transcriptions").mock(
            return_value=Response(200, json={"text": " set a timer for ten minutes "})
        )
        await sat.handle_start("c", 0, None, "Okay Jess")
        await _say(sat.handle_audio, UTTERANCE)
        await _drain(sat)

    assert route.call_count == 1
    body = route.calls[0].request.read()
    assert b"RIFF" in body and b"whisper-1" in body and b"satellite.wav" in body
    assert sat._chat.calls == [[{"role": "user", "content": "set a timer for ten minutes"}]]
    spoken = "Sure — the timer is set for ten minutes."
    assert announce.await_args.args == (spoken,)
    assert sat._history[-1] == {"role": "assistant", "content": spoken}
    assert sat._client.names == [RUN_START, STT_START, VAD_START, RUN_END]


async def test_late_and_empty_audio_is_ignored(sat, pipeline):
    await sat.handle_audio(_tone(0.1))  # no run yet
    assert sat._client.names == []
    await sat.handle_start("c", 0, None, "Hey Jess")
    await sat.handle_audio(b"")
    assert sat._client.names == []  # an empty chunk is not "first audio"
    await _say(sat.handle_audio, UTTERANCE + _audio((0.1, 2000)))
    await _drain(sat)
    assert sat._client.names.count(RUN_END) == 1
    assert sat._client.names.count(VAD_START) == 1


async def test_audio_is_framed_independently_of_chunk_size(sat):
    await sat.handle_start("c", 0, None, "Hey Jess")
    run = sat._run
    await sat.handle_audio(bytes(1000))
    assert (run.audio_ms, len(run.pending)) == (0, 1000)
    await sat.handle_audio(bytes(1000))
    assert (run.audio_ms, len(run.pending), len(run.pcm)) == (50, 400, 2000)


async def test_no_speech_run(sat, pipeline):
    before = _turns("no_speech", wake="okay_jess")
    await sat.handle_start("c", 0, None, "Okay Jess")
    await _say(sat.handle_audio, SILENCE_6S)
    await _drain(sat)

    assert sat._client.names == [RUN_START, STT_START, RUN_END, ERROR]
    assert sat._client.errors == ["jess-no-speech"]
    assert _turns("no_speech", wake="okay_jess") == before + 1
    assert pipeline.stt.await_count == 0 and sat._chat.calls == []
    assert sat._busy is False
    assert len(sat._turn_starts) == 0  # false wakes don't use up the rate limit


async def test_busy_refuses_a_second_wake(sat, pipeline, later):
    sat._chat.block = asyncio.Event()
    before = _turns("busy", wake="button")

    await sat.handle_start("c1", 0, None, "Hey Jess")
    await _say(sat.handle_audio, UTTERANCE)
    await _until(lambda: sat._chat.calls)  # turn now waiting on the brain

    assert await sat.handle_start("c2", 0, None, None) == 0
    assert sat._run is None  # the refused run owns nothing
    await sat.handle_audio(_tone(0.1))  # its audio is ignored
    await _tick()
    assert sat._client.errors == ["jess-busy"]
    assert later == [0.2]
    assert _turns("busy", wake="button") == before + 1

    sat._chat.block.set()
    await _drain(sat)
    assert sat._busy is False
    assert await sat.handle_start("c3", 0, None, "Hey Jess") == 0
    assert sat._run is not None


async def test_rate_limit_after_six_turns(sat, pipeline, later):
    before = _turns("rate_limited")
    for _ in range(vs.RATE_LIMIT_TURNS):
        await sat.handle_start("c", 0, None, "Hey Jess")
        await _say(sat.handle_audio, UTTERANCE)
        await _drain(sat)
    assert pipeline.speak.await_count == vs.RATE_LIMIT_TURNS

    sat._client.events.clear()
    assert await sat.handle_start("c", 0, None, "Hey Jess") == 0
    await _tick()
    assert sat._run is None
    assert sat._client.errors == ["jess-busy"]
    assert _turns("rate_limited") == before + 1

    # the window slides: turns older than RATE_LIMIT_WINDOW_S stop counting
    old = time.monotonic() - vs.RATE_LIMIT_WINDOW_S - 1
    sat._turn_starts = type(sat._turn_starts)([old] * len(sat._turn_starts))
    await sat.handle_start("c", 0, None, "Hey Jess")
    assert sat._run is not None
    assert len(sat._turn_starts) == 0


async def test_abort_by_device(sat, pipeline):
    before = _turns("aborted")
    await sat.handle_start("c", 0, None, "Hey Jess")
    await _say(sat.handle_audio, _audio((0.0, 200), (0.1, 500)))
    watcher = sat._run.watcher
    await sat.handle_stop(True)
    await _drain(sat)

    await asyncio.sleep(0)  # let the cancellation land
    assert watcher.cancelled()  # no idle watcher holding the run's audio
    assert sat._client.names == [RUN_START, STT_START, VAD_START]  # device ended its own run
    assert _turns("aborted") == before + 1
    assert sat._busy is False and sat._run is None
    assert pipeline.stt.await_count == 0
    await sat.handle_stop(True)  # nothing to stop: no-op
    assert _turns("aborted") == before + 1


async def test_device_stop_with_speech_runs_the_turn(sat, pipeline):
    await sat.handle_start("c", 0, None, "Hey Jess")
    await _say(sat.handle_audio, _audio((0.0, 200), (0.1, 600)))
    await sat.handle_stop(False)
    await _drain(sat)
    assert sat._client.names == [RUN_START, STT_START, VAD_START, RUN_END]
    assert pipeline.speak.await_count == 1


async def test_device_stop_without_speech(sat, pipeline):
    before = _turns("no_speech")
    await sat.handle_start("c", 0, None, "Hey Jess")
    await _say(sat.handle_audio, _audio((0.0, 1000)))
    await sat.handle_stop(False)
    await _drain(sat)
    assert sat._client.names == [RUN_START, STT_START, RUN_END, ERROR]
    assert sat._client.errors == ["jess-no-speech"]
    assert _turns("no_speech") == before + 1
    assert pipeline.stt.await_count == 0


async def test_no_audio_watchdog(sat, pipeline, monkeypatch):
    monkeypatch.setattr(vs, "FIRST_AUDIO_TIMEOUT_S", 0.01)
    before = _turns("no_audio")
    await sat.handle_start("c", 0, None, "Hey Jess")
    await sat._run.watcher
    assert sat._client.names == [RUN_END, ERROR]
    assert sat._client.errors == ["jess-error"]
    assert _turns("no_audio") == before + 1
    assert sat._busy is False and sat._run is None


async def test_wall_timeout_processes_what_was_heard(sat, pipeline, monkeypatch):
    monkeypatch.setattr(vs, "FIRST_AUDIO_TIMEOUT_S", 0.01)
    monkeypatch.setattr(vs, "RUN_WALL_TIMEOUT_S", 0.02)
    await sat.handle_start("c", 0, None, "Hey Jess")
    watcher = sat._run.watcher
    await _say(sat.handle_audio, _audio((0.0, 200), (0.1, 600)))  # never goes quiet
    await watcher
    await _drain(sat)
    assert sat._client.names == [RUN_START, STT_START, VAD_START, RUN_END]
    assert pipeline.speak.await_count == 1


async def test_vad_end_cancels_the_watcher(sat, pipeline):
    await sat.handle_start("c", 0, None, "Hey Jess")
    watcher = sat._run.watcher
    await _say(sat.handle_audio, UTTERANCE)
    await asyncio.gather(watcher, return_exceptions=True)
    assert watcher.cancelled()
    await _drain(sat)


# Talking without a pause from 250 ms: the 15 s cap ends listening at 15.4 s.
NONSTOP_16S = _audio((0.0, 200), (0.1, 16_000))
STILL_TALKING = _audio((0.0, 200), (0.1, 600))  # speech in progress when the run ends
QUIET_1S = _audio((0.0, 1000))


async def test_max_utterance_still_answers_with_what_was_heard(sat, pipeline):
    """At the cap listening stops and the turn runs on the audio so far: an ok
    turn — only listen_end{reason="max_utterance"} shows it was the cap."""
    before = _turns("ok", helios="awake")
    await sat.handle_start("c", 0, None, "Hey Jess")
    run = sat._run
    await _say(sat.handle_audio, NONSTOP_16S)
    await _drain(sat)
    assert sat._client.names == [RUN_START, STT_START, VAD_START, RUN_END]
    assert run.audio_ms == 15_400  # the cap counts from the first frame of speech (400 ms)
    (pcm,) = pipeline.stt.await_args.args
    assert run.audio_ms * vs.SAMPLE_RATE * vs.SAMPLE_WIDTH // 1000 <= len(pcm) < len(NONSTOP_16S)
    assert pipeline.speak.await_count == 1
    assert _turns("ok", helios="awake") == before + 1


# ---------------------------------------------------------------------------
# how listening ended: bgw_satellite_listen_end_total + the log line
# ---------------------------------------------------------------------------


def _listen_ends(device: str = NODE) -> dict[str, float]:
    return {
        r: _value(metrics.SATELLITE_LISTEN_END_TOTAL, {"reason": r, "device": device}) for r in vs.LISTEN_END_REASONS
    }


async def _ends_speech_end(sat):
    await sat.handle_start("c", 0, None, "Hey Jess")
    await _say(sat.handle_audio, UTTERANCE)


async def _ends_max_utterance(sat):
    await sat.handle_start("c", 0, None, "Hey Jess")
    await _say(sat.handle_audio, NONSTOP_16S)


async def _ends_no_speech(sat):
    await sat.handle_start("c", 0, None, "Hey Jess")
    await _say(sat.handle_audio, SILENCE_6S)


async def _ends_device_stop(sat):
    await sat.handle_start("c", 0, None, "Hey Jess")
    await _say(sat.handle_audio, STILL_TALKING)
    await sat.handle_stop(False)


async def _ends_device_stop_quiet(sat):
    await sat.handle_start("c", 0, None, "Hey Jess")
    await _say(sat.handle_audio, QUIET_1S)
    await sat.handle_stop(False)


async def _ends_wall_timeout(sat):
    await sat.handle_start("c", 0, None, "Hey Jess")
    watcher = sat._run.watcher
    await _say(sat.handle_audio, STILL_TALKING)
    await watcher


async def _ends_wall_timeout_quiet(sat):
    await sat.handle_start("c", 0, None, "Hey Jess")
    watcher = sat._run.watcher
    await _say(sat.handle_audio, QUIET_1S)
    await watcher


async def _ends_no_audio(sat):
    await sat.handle_start("c", 0, None, "Hey Jess")
    await sat._run.watcher


async def _ends_aborted(sat):
    await sat.handle_start("c", 0, None, "Hey Jess")
    await _say(sat.handle_audio, STILL_TALKING)
    await sat.handle_stop(True)


async def _ends_duplicate(sat):
    sat._arbiter = vs.WakeArbiter()
    sat._arbiter.claim("atom-echo-other", "hey_jess")  # the other room heard it first
    await sat.handle_start("c", 0, None, "Hey Jess")
    await _say(sat.handle_audio, UTTERANCE)


_LISTEN_END_PATHS = [
    pytest.param(_ends_speech_end, "speech_end", id="speech-end"),
    pytest.param(_ends_max_utterance, "max_utterance", id="max-utterance"),
    pytest.param(_ends_no_speech, "no_speech", id="no-speech"),
    pytest.param(_ends_device_stop, "device_stop", id="device-stop"),
    pytest.param(_ends_device_stop_quiet, "device_stop", id="device-stop-quiet"),
    pytest.param(_ends_wall_timeout, "wall_timeout", id="wall-timeout"),
    pytest.param(_ends_wall_timeout_quiet, "wall_timeout", id="wall-timeout-quiet"),
    pytest.param(_ends_no_audio, "no_audio", id="no-audio"),
    pytest.param(_ends_aborted, "aborted", id="aborted"),
    pytest.param(_ends_duplicate, "duplicate", id="duplicate"),
]

# Everything the line may carry: the node, a fixed reason and numbers — no
# audio, no transcript.
_LISTEN_LOG = re.compile(
    r"\[SATELLITE\] (?P<node>[\w-]+): listening ended \((?P<reason>[a-z-]+)\) audio=(?P<audio>\d+)ms"
    r" floor=(?P<floor>\d\.\d{4}) start=(?P<start>\d\.\d{4}) end=(?P<end>\d\.\d{4})"
    r" peak=(?P<peak>\d\.\d{4}) max=(?P<max>\d\.\d{4}) tail=(?P<tail>\d\.\d{4})"
    r" quiet=(?P<quiet>\d+)ms speech_at=(?P<speech_at>-1|\d+)ms speech=(?P<speech>True|False)"
)


def _listen_logs(caplog) -> list[str]:
    return [m for _, m in _sat_logs(caplog) if "listening ended" in m]


def _listen_log(caplog) -> dict[str, str]:
    (line,) = _listen_logs(caplog)
    match = _LISTEN_LOG.fullmatch(line)
    assert match, line
    return match.groupdict()


def _vad_fields(vad: EnergyVad) -> dict[str, str]:
    """What the line should say, read off the run's own VAD."""
    return {
        "floor": f"{vad.floor:.4f}",
        "start": f"{vad.threshold:.4f}",
        "end": f"{vad.end_threshold:.4f}",
        "peak": f"{vad.peak:.4f}",
        "max": f"{vad.max_level:.4f}",
        "tail": f"{vad.tail_mean:.4f}",
        "quiet": str(vad.quiet_ms),
        "speech_at": str(-1 if vad.speech_at is None else vad.speech_at),
        "speech": str(vad.heard_speech),
    }


@pytest.mark.parametrize("drive, reason", _LISTEN_END_PATHS)
async def test_listen_end_counts_its_own_reason_once(sat, pipeline, clock, caplog, drive, reason):
    caplog.set_level(logging.INFO, logger="orchestrator.voice_satellite")
    before = _listen_ends()
    await drive(sat)
    await _drain(sat)
    # The device's own stop (and stray audio) after we ended the run is not
    # a second ending.
    await sat.handle_stop(False)
    await sat.handle_stop(True)
    await sat.handle_audio(_tone(0.1))
    after = _listen_ends()
    assert {r: after[r] - before[r] for r in after} == {r: float(r == reason) for r in after}
    lines = _listen_logs(caplog)
    assert all(_LISTEN_LOG.fullmatch(line) for line in lines), lines
    expected = [] if reason == "aborted" else [reason.replace("_", "-")]
    assert [_LISTEN_LOG.fullmatch(line)["reason"] for line in lines] == expected


def test_listen_end_paths_cover_every_reason():
    assert {p.values[1] for p in _LISTEN_END_PATHS} == set(vs.LISTEN_END_REASONS)


def test_listen_end_reasons_match_what_the_code_emits():
    """LISTEN_END_REASONS is what satellite_loop pre-seeds: a reason missing
    from it starts its series lazily, and that first increase is invisible."""
    emitted = set()
    for node in ast.walk(ast.parse(inspect.getsource(vs))):
        method_call = isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        if method_call and node.func.attr in ("_end_run", "labels"):
            emitted |= {
                kw.value.value for kw in node.keywords if kw.arg == "reason" and isinstance(kw.value, ast.Constant)
            }
    # handle_audio passes the VAD's ending events on as reason=event
    feed = ast.parse(textwrap.dedent(inspect.getsource(vs.EnergyVad.feed)))
    events = {
        n.value.value
        for n in ast.walk(feed)
        if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
    }
    assert events == {"speech-start", "speech-end", "max-utterance", "no-speech"}
    emitted |= events - {"speech-start"}
    assert {e.replace("-", "_") for e in emitted} == set(vs.LISTEN_END_REASONS)


async def test_listening_ended_log_for_a_question(sat, pipeline, caplog):
    caplog.set_level(logging.DEBUG)
    pipeline.stt.return_value = "my secret pin is 4321"
    sat._chat.reply = "Your secret pin is 4321."
    await sat.handle_start("c", 0, None, "Hey Jess")
    run = sat._run
    await _say(sat.handle_audio, UTTERANCE)
    await _drain(sat)
    f = _listen_log(caplog)
    assert (f["node"], f["reason"], f["audio"]) == (NODE, "speech-end", "2400")
    expected = _vad_fields(run.vad)
    assert {k: f[k] for k in expected} == expected
    # speech from 400 ms (the first frame of the 200 ms mean over the bar);
    # ended once nothing reached the bar for silence_ms and the mean was low
    assert (f["speech_at"], f["quiet"], f["speech"]) == ("400", str(vs.VadConfig().silence_ms), "True")
    assert float(f["tail"]) < float(f["end"]) < float(f["start"])
    assert f["peak"] == f["max"] == "0.1000"
    assert "4321" not in caplog.text


async def test_listening_ended_log_without_speech(sat, pipeline, caplog):
    caplog.set_level(logging.INFO, logger="orchestrator.voice_satellite")
    await sat.handle_start("c", 0, None, "Okay Jess")
    run = sat._run
    # one click (a tap on the desk) in a silent room: not speech
    await _say(sat.handle_audio, _audio((0.0, 1000), (0.3, 50), (0.0, 5000)))
    await _drain(sat)
    f = _listen_log(caplog)
    assert (f["reason"], f["audio"]) == ("no-speech", "5000")
    expected = _vad_fields(run.vad)
    assert {k: f[k] for k in expected} == expected
    assert (f["speech_at"], f["quiet"], f["speech"]) == ("-1", "0", "False")
    # peak = the loudest frame; max = the loudest 200 ms mean (what was compared to the bar)
    assert (f["peak"], f["max"], f["tail"]) == ("0.3000", "0.0750", "0.0000")
    # a digitally silent room: the bars sit at their silent-room minimums
    assert float(f["floor"]) < float(f["end"]) < float(f["start"])


# ---------------------------------------------------------------------------
# the Jess turn
# ---------------------------------------------------------------------------


async def _one_turn(sat, wake="Hey Jess"):
    await sat.handle_start("c", 0, None, wake)
    await _say(sat.handle_audio, UTTERANCE)
    await _drain(sat)


@pytest.mark.parametrize(
    "transcript, result, code",
    [
        pytest.param(None, "stt_failed", "jess-error", id="stt-none"),
        pytest.param("", "empty_transcript", "jess-no-speech", id="stt-empty"),
    ],
)
async def test_stt_outcomes(sat, pipeline, transcript, result, code):
    pipeline.stt.return_value = transcript
    before = _turns(result, helios="awake")
    await _one_turn(sat)
    assert sat._client.names[-1] == ERROR and sat._client.errors == [code]
    assert _turns(result, helios="awake") == before + 1
    assert sat._chat.calls == [] and pipeline.speak.await_count == 0


async def test_chat_exception(sat, pipeline):
    sat._chat.exc = RuntimeError("model fell over")
    before = _turns("chat_failed", helios="awake")
    await _one_turn(sat)
    assert sat._client.errors == ["jess-error"]
    assert _turns("chat_failed", helios="awake") == before + 1
    assert pipeline.speak.await_count == 0 and sat._history == []


async def test_chat_timeout(sat, pipeline, monkeypatch, caplog):
    monkeypatch.setattr(vs, "CHAT_TIMEOUT_S", 0.01)
    sat._chat.block = asyncio.Event()
    caplog.set_level(logging.ERROR, logger="orchestrator.voice_satellite")
    await _one_turn(sat)
    assert sat._client.errors == ["jess-error"]
    assert any("timed out" in r.getMessage() for r in caplog.records)
    assert pipeline.speak.await_count == 0


@pytest.mark.parametrize(
    "status, reply",
    [
        pytest.param(500, "It is three.", id="http-500"),
        pytest.param(200, "", id="empty-reply"),
        pytest.param(200, "https://only.a.link/example", id="unspeakable-reply"),
    ],
)
async def test_chat_unusable_reply(sat, pipeline, status, reply):
    sat._chat.status, sat._chat.reply = status, reply
    before = _turns("chat_failed", helios="awake")
    await _one_turn(sat)
    assert sat._client.errors == ["jess-error"]
    assert _turns("chat_failed", helios="awake") == before + 1
    assert pipeline.speak.await_count == 0


@pytest.mark.parametrize("outcome", ["speaker_unavailable", "tts_failed"])
async def test_playback_failure(sat, pipeline, outcome):
    pipeline.speak.return_value = outcome
    before = _turns(outcome, helios="awake")
    hist_before = _turn_seconds_count("awake")
    await _one_turn(sat)
    assert sat._client.errors == ["jess-error"]
    assert _turns(outcome, helios="awake") == before + 1
    assert _turn_seconds_count("awake") == hist_before  # only ok turns are timed
    assert sat._history == []  # nothing was heard, nothing to follow up on


async def test_turn_crash_is_contained_and_log_safe(sat, pipeline, caplog):
    async def stt(pcm):
        raise RuntimeError("my secret pin is 4321")

    pipeline.stt.side_effect = stt
    caplog.set_level(logging.DEBUG)
    before = _turns("error", helios="awake")
    await _one_turn(sat)
    assert sat._client.errors == ["jess-error"]
    assert _turns("error", helios="awake") == before + 1
    assert sat._busy is False
    crash = [r for r in caplog.records if "turn crashed" in r.getMessage()]
    assert crash and crash[0].levelno == logging.ERROR
    assert "RuntimeError at test_voice_satellite.py:" in crash[0].getMessage()
    assert "4321" not in caplog.text


async def test_turn_logs_carry_no_words(sat, pipeline, caplog):
    pipeline.stt.return_value = "my secret pin is 4321"
    sat._chat.reply = "Your secret pin is 4321."
    caplog.set_level(logging.DEBUG)
    await _one_turn(sat)
    assert pipeline.speak.await_args.args == ("Your secret pin is 4321.",)
    assert "4321" not in caplog.text
    assert any("transcript 21 chars" in r.getMessage() for r in caplog.records)


async def test_history_follow_up_and_spoken_text_stored(sat, pipeline):
    sat._chat.reply = "Sure, **dimmed** [the lamp](http://x.example)."
    await _one_turn(sat)
    assert sat._history == [
        {"role": "user", "content": "what time is it"},
        {"role": "assistant", "content": "Sure, dimmed the lamp."},
    ]
    pipeline.stt.return_value = "and the hall"
    await _one_turn(sat)
    assert sat._chat.calls[1] == [*sat._history[:2], {"role": "user", "content": "and the hall"}]
    first_cid, second_cid = sat._chat.ctx[0][1], sat._chat.ctx[1][1]
    assert first_cid == second_cid and first_cid.startswith(f"satellite:{NODE}:")


async def test_history_resets_after_idle(sat, pipeline):
    await _one_turn(sat)
    sat._last_turn_at -= vs.HISTORY_IDLE_SECONDS + 1
    sat._conv_started = 123
    pipeline.stt.return_value = "new question"
    await _one_turn(sat)
    assert sat._chat.calls[1] == [{"role": "user", "content": "new question"}]
    assert not sat._chat.ctx[1][1].endswith(":123")


async def test_history_is_capped(sat):
    for i in range(7):
        sat._remember(f"q{i}", f"a{i}")
    assert len(sat._history) == vs.HISTORY_MAX_MESSAGES
    assert sat._history[0] == {"role": "user", "content": "q1"}


async def test_turn_context_does_not_leak(sat, pipeline):
    from orchestrator.log_config import request_id_var

    rid = request_id_var.get()
    await _one_turn(sat)
    ((skip, cid),) = sat._chat.ctx
    assert skip is True  # room audio never feeds auto_learn
    assert cid.startswith(f"satellite:{NODE}:")
    # ...but only inside the turn's own task
    assert skip_auto_learn_var.get() is False
    assert conversation_id_var.get() is None
    assert request_id_var.get() == rid


async def test_turn_marks_voice_activity(sat, pipeline):
    await _one_turn(sat)
    assert shared.is_voice_session_active()


# ---------------------------------------------------------------------------
# Helios readiness (_ensure_helios)
# ---------------------------------------------------------------------------


def _statuses(*seq):
    """_service_status stand-in: each dict in turn, then the last one forever."""
    calls = []

    async def fake(names):
        calls.append(names)
        return dict(seq[min(len(calls), len(seq)) - 1])

    fake.calls = calls
    return fake


@pytest.fixture
def helios(monkeypatch):
    from orchestrator import helios_power

    h = SimpleNamespace(
        wake=AsyncMock(return_value={"ok": True}),
        running=AsyncMock(return_value=False),
    )
    monkeypatch.setattr(helios_power, "wake_helios", h.wake)
    monkeypatch.setattr(vs._settings, "helios_wake_enabled", False)
    return h


async def test_helios_all_up(sat, helios, clock, monkeypatch):
    monkeypatch.setattr(sat, "_service_status", _statuses(UP))
    monkeypatch.setattr(sat, "_helios_running", helios.running)
    run = vs._Run(wake_word="hey_jess")
    assert await sat._ensure_helios(run) is None
    assert run.helios == "awake"
    assert clock.sleeps == [] and sat._client.events == []
    assert helios.running.await_count == 0 and helios.wake.await_count == 0


async def test_helios_on_service_reloading_then_ready(sat, helios, clock, monkeypatch):
    status = _statuses(MODEL_DOWN, MODEL_DOWN, UP)
    monkeypatch.setattr(sat, "_service_status", status)
    monkeypatch.setattr(sat, "_helios_running", helios.running)
    run = vs._Run(wake_word="hey_jess")
    assert await sat._ensure_helios(run) is None
    assert run.helios == "woken"
    assert clock.sleeps == [5.0, 5.0]
    assert sat._client.errors == ["jess-waking"]
    # STT answered, so Helios is on: no plug read, no wake
    assert helios.running.await_count == 0 and helios.wake.await_count == 0
    assert status.calls[0] == ("stt", "model", "tts")


async def test_helios_on_service_never_ready(sat, helios, clock, monkeypatch):
    monkeypatch.setattr(sat, "_service_status", _statuses(MODEL_DOWN))
    monkeypatch.setattr(sat, "_helios_running", helios.running)
    run = vs._Run(wake_word="hey_jess")
    assert await sat._ensure_helios(run) == "service_down"
    assert clock.sleeps == [5.0] * int(vs.SERVICE_GRACE_S / 5)
    assert sat._client.errors == ["jess-waking", "jess-error"]
    assert helios.wake.await_count == 0
    assert run.helios == "unchecked"


async def test_helios_powered_by_plug_reading(sat, helios, clock, monkeypatch):
    """Nothing answers yet but the plug says it's on (booting): no wake, just wait."""
    monkeypatch.setattr(vs._settings, "helios_wake_enabled", True)
    helios.running.return_value = True
    monkeypatch.setattr(sat, "_service_status", _statuses(DOWN, UP))
    monkeypatch.setattr(sat, "_helios_running", helios.running)
    run = vs._Run(wake_word="hey_jess")
    assert await sat._ensure_helios(run) is None
    assert run.helios == "woken"
    assert helios.wake.await_count == 0
    assert sat._client.errors == ["jess-waking"]


async def test_helios_asleep_and_wake_disabled(sat, helios, clock, monkeypatch):
    monkeypatch.setattr(sat, "_service_status", _statuses(DOWN))
    monkeypatch.setattr(sat, "_helios_running", helios.running)
    run = vs._Run(wake_word="hey_jess")
    assert await sat._ensure_helios(run) == "helios_asleep"
    assert sat._client.errors == ["jess-asleep"]
    assert clock.sleeps == [] and helios.wake.await_count == 0


async def test_helios_woken(sat, helios, clock, monkeypatch):
    monkeypatch.setattr(vs._settings, "helios_wake_enabled", True)
    monkeypatch.setattr(sat, "_service_status", _statuses(DOWN, DOWN, DOWN, UP))
    monkeypatch.setattr(sat, "_helios_running", helios.running)
    run = vs._Run(wake_word="hey_jess")
    assert await sat._ensure_helios(run) is None
    assert run.helios == "woken"
    assert helios.wake.await_count == 1
    assert clock.sleeps == [5.0, 5.0, 5.0]
    assert sat._client.errors == ["jess-waking"]


async def test_helios_wake_timeout(make_sat, helios, clock, monkeypatch):
    sat = make_sat(helios_timeout_s=20)
    monkeypatch.setattr(vs._settings, "helios_wake_enabled", True)
    monkeypatch.setattr(sat, "_service_status", _statuses(DOWN))
    monkeypatch.setattr(sat, "_helios_running", helios.running)
    run = vs._Run(wake_word="hey_jess")
    assert await sat._ensure_helios(run) == "wake_timeout"
    assert clock.sleeps == [5.0] * 4
    assert sat._client.errors == ["jess-waking", "jess-error"]


async def test_helios_negative_timeout_is_clamped(make_sat, helios, clock, monkeypatch):
    sat = make_sat(helios_timeout_s=-5)
    monkeypatch.setattr(vs._settings, "helios_wake_enabled", True)
    monkeypatch.setattr(sat, "_service_status", _statuses(DOWN))
    monkeypatch.setattr(sat, "_helios_running", helios.running)
    assert await sat._ensure_helios(vs._Run(wake_word="hey_jess")) == "wake_timeout"
    assert clock.sleeps == []


async def test_helios_wake_call_fails(sat, helios, clock, monkeypatch):
    monkeypatch.setattr(vs._settings, "helios_wake_enabled", True)
    helios.wake.return_value = {"ok": False, "error": "HTTPError"}
    monkeypatch.setattr(sat, "_service_status", _statuses(DOWN))
    monkeypatch.setattr(sat, "_helios_running", helios.running)
    assert await sat._ensure_helios(vs._Run(wake_word="hey_jess")) == "wake_failed"
    assert sat._client.errors == ["jess-error"]
    assert clock.sleeps == []


async def test_woken_turn_metric_label(sat, helios, clock, monkeypatch, pipeline):
    monkeypatch.setattr(vs._settings, "helios_wake_enabled", True)
    monkeypatch.setattr(sat, "_service_status", _statuses(DOWN, UP))
    monkeypatch.setattr(sat, "_helios_running", helios.running)
    before = _turns("ok", helios="woken")
    hist_before = _turn_seconds_count("woken")
    await _one_turn(sat)
    assert _turns("ok", helios="woken") == before + 1
    assert _turn_seconds_count("woken") == hist_before + 1
    # the "answer comes later" sound, then the answer — no failure sound
    assert sat._client.errors == ["jess-waking"]


@pytest.mark.parametrize(
    "result, expected",
    [
        pytest.param({"ok": True, "inferred": "running"}, True, id="running"),
        pytest.param({"ok": True, "inferred": "asleep"}, False, id="asleep"),
        pytest.param({"ok": False, "skipped": "disabled"}, False, id="disabled"),
        pytest.param(RuntimeError("boom"), False, id="raises"),
    ],
)
async def test_helios_running(sat, monkeypatch, result, expected):
    from orchestrator import helios_power

    mock = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
    monkeypatch.setattr(helios_power, "helios_power_status", mock)
    assert await sat._helios_running() is expected


async def test_service_status_uses_shared_client_and_3s_probe(sat, monkeypatch):
    from orchestrator.service_registry import services

    seen = []

    async def check(name, client=None, timeout=5.0):
        seen.append((name, client, timeout))
        if name == "tts":
            raise RuntimeError("probe blew up")
        return name == "stt"

    monkeypatch.setattr(services, "check_service", check)
    async with httpx.AsyncClient() as client:
        monkeypatch.setattr(shared, "_http", client, raising=False)
        assert await sat._service_status(("stt", "model", "tts")) == {"stt": True, "model": False, "tts": False}
        assert [(n, c is client, t) for n, c, t in seen] == [
            ("stt", True, 3.0),
            ("model", True, 3.0),
            ("tts", True, 3.0),
        ]
    seen.clear()
    await sat._service_status(("stt",))  # closed shared client: let the registry make its own
    assert seen == [("stt", None, 3.0)]


async def test_check_service_honours_timeout_with_a_shared_client():
    """The satellite probes Helios through the shared client (60 s read,
    10 s connect) with timeout=3.0: that budget must reach the request."""
    from orchestrator.service_registry import ServiceRegistry

    reg = ServiceRegistry()
    reg._services["stt"].url = STT
    seen = []

    def handler(request):
        seen.append(request.extensions["timeout"])
        return Response(200)

    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as client:
        with respx.mock:
            respx.get(f"{STT}/health").mock(side_effect=handler)
            assert await reg.check_service("stt", client=client, timeout=3.0) is True
    assert seen == [{"connect": 3.0, "read": 3.0, "write": 3.0, "pool": 3.0}]


# ---------------------------------------------------------------------------
# STT
# ---------------------------------------------------------------------------


@pytest.fixture
def stt_env(monkeypatch):
    monkeypatch.setattr(vs._settings, "stt_url", STT + "/")
    monkeypatch.setattr(shared, "_http", None, raising=False)


@pytest.mark.parametrize(
    "response, expected",
    [
        pytest.param(Response(200, json={"text": "  turn on the lamp "}), "turn on the lamp", id="words"),
        pytest.param(Response(200, json={"text": ""}), "", id="no-words"),
        pytest.param(Response(200, json={"text": None}), "", id="null-text"),
        pytest.param(Response(200, json=["x"]), "", id="non-dict"),
        pytest.param(Response(500, json={"error": "x"}), None, id="http-500"),
        pytest.param(Response(200, text="<html>"), None, id="not-json"),
        pytest.param(httpx.ConnectError("refused"), None, id="connect-error"),
    ],
)
async def test_transcribe(sat, stt_env, response, expected):
    with respx.mock:
        route = respx.post(f"{STT}/v1/audio/transcriptions")
        if isinstance(response, Exception):
            route.mock(side_effect=response)
        else:
            route.mock(return_value=response)
        assert await sat._transcribe(_tone(0.1, 500)) == expected
        assert route.call_count == 1


async def test_transcribe_without_stt_url(sat, monkeypatch):
    monkeypatch.setattr(vs._settings, "stt_url", "")
    with respx.mock(assert_all_called=False) as r:
        route = r.post(url__regex=r".*")
        assert await sat._transcribe(b"\x00\x00") is None
    assert not route.called


async def test_transcribe_reuses_open_shared_client(sat, stt_env, monkeypatch):
    with respx.mock:
        respx.post(f"{STT}/v1/audio/transcriptions").mock(return_value=Response(200, json={"text": "hi"}))
        async with httpx.AsyncClient() as client:
            monkeypatch.setattr(shared, "_http", client, raising=False)
            assert await sat._transcribe(b"\x00\x00") == "hi"
            assert not client.is_closed  # not ours to close


# ---------------------------------------------------------------------------
# playback (_speak)
# ---------------------------------------------------------------------------


@pytest.fixture
def announce(monkeypatch):
    from orchestrator import reminder_manager, voice_style

    mock = AsyncMock(return_value={"success": True, "speaker": SPEAKER})
    monkeypatch.setattr(reminder_manager, "_announce_voice", mock)
    monkeypatch.setattr(voice_style, "current_reply_style", lambda: "calm and warm")
    return mock


async def test_speak_kwargs(sat, announce):
    assert await sat._speak("It is three.") == "ok"
    announce.assert_awaited_once_with(
        "It is three.",
        speaker=SPEAKER,
        announcement_type="voice_reply",
        ignore_voice_session=True,
        ignore_dnd=True,
        style="calm and warm",
        allow_fallback=False,
        record_history=False,
        progressive=True,
    )


@pytest.mark.parametrize(
    "result, outcome",
    [
        pytest.param({"success": True, "speaker": SPEAKER}, "ok", id="played"),
        pytest.param({"success": True, "suppressed": True, "reason": "dnd_active"}, "tts_failed", id="suppressed"),
        pytest.param(
            {"success": False, "error": "x", "unavailable": [SPEAKER]}, "speaker_unavailable", id="unavailable"
        ),
        pytest.param({"success": False, "error": "HA returned 500"}, "tts_failed", id="ha-error"),
        pytest.param(RuntimeError("tts down"), "tts_failed", id="raises"),
    ],
)
async def test_speak_outcomes(sat, announce, result, outcome):
    if isinstance(result, Exception):
        announce.side_effect = result
    else:
        announce.return_value = result
    assert await sat._speak("Hi.") == outcome


async def test_speak_timeout(sat, announce, monkeypatch):
    monkeypatch.setattr(vs, "ANNOUNCE_TIMEOUT_S", 0.01)

    async def hang(*a, **k):
        await asyncio.Event().wait()

    announce.side_effect = hang
    assert await sat._speak("Hi.") == "tts_failed"


# ---------------------------------------------------------------------------
# reminder_manager._announce_voice: the satellite's new flags
# ---------------------------------------------------------------------------


class _StyleBackend:
    audio_format = "audio/wav"
    file_extension = "wav"

    def __init__(self) -> None:
        self.styles: list[str] = []

    async def synthesize(self, text, voice=None, style=""):
        self.styles.append(style)
        return b"\x00\x01"


@pytest.fixture
def ann(monkeypatch):
    from orchestrator import announcement_routes, reminder_manager

    monkeypatch.setattr(reminder_manager, "HA_URL", HA, raising=False)
    monkeypatch.setattr(reminder_manager, "HA_TOKEN", "tok", raising=False)
    monkeypatch.setattr(reminder_manager, "ORCHESTRATOR_URL", "http://orch.test:8888", raising=False)
    monkeypatch.setattr(reminder_manager, "REMINDER_SPEAKER", "", raising=False)
    monkeypatch.setattr(reminder_manager, "style_for_announcement", lambda t: f"default-for-{t}")
    monkeypatch.setattr(shared, "DND_ACTIVE", False, raising=False)
    monkeypatch.setattr(shared, "is_voice_session_active", lambda *a, **k: False)
    monkeypatch.setattr(shared, "_http", None, raising=False)
    backend = _StyleBackend()
    monkeypatch.setattr(shared, "tts_backend", backend, raising=False)
    rows: list[dict] = []
    monkeypatch.setattr(reminder_manager.state_store, "record_announcement", lambda **kw: rows.append(kw))
    monkeypatch.setattr("os.makedirs", lambda path, exist_ok=False: None)
    monkeypatch.setattr("builtins.open", mock_open())
    routes: dict[str, str] = {}
    monkeypatch.setattr(announcement_routes, "route_for", lambda cat: routes.get((cat or "").lower(), ""))
    return SimpleNamespace(backend=backend, rows=rows, routes=routes)


def _ha(router, states: dict[str, str]):
    """respx router for HA: entity states + play_media. Returns (play route, probed entities)."""
    probed: list[str] = []

    def state(request):
        entity = request.url.path.rsplit("/", 1)[-1]
        probed.append(entity)
        return Response(200, json={"entity_id": entity, "state": states.get(entity, "idle")})

    router.get(url__startswith=STATES).mock(side_effect=state)
    return router.post(PLAY).mock(return_value=Response(200)), probed


def _tts_count(success: str, speaker: str = SPEAKER) -> float:
    return _value(metrics.TTS_ANNOUNCEMENTS_TOTAL, {"type": "voice_reply", "speaker": speaker, "success": success})


async def test_announce_defaults_unchanged():
    import inspect

    from orchestrator.reminder_manager import _announce_voice

    p = inspect.signature(_announce_voice).parameters
    assert (p["ignore_dnd"].default, p["style"].default) == (False, None)
    assert (p["allow_fallback"].default, p["record_history"].default) == (True, True)


async def test_announce_dnd_suppresses_by_default(ann, monkeypatch):
    from orchestrator.reminder_manager import _announce_voice

    monkeypatch.setattr(shared, "DND_ACTIVE", True)
    with respx.mock(assert_all_called=False) as router:
        play, _ = _ha(router, {})
        out = await _announce_voice("hi", speaker=SPEAKER, announcement_type="voice_reply")
    assert out == {"success": True, "suppressed": True, "reason": "dnd_active"}
    assert not play.called and ann.backend.styles == []


async def test_announce_ignore_dnd_plays(ann, monkeypatch):
    from orchestrator.reminder_manager import _announce_voice

    monkeypatch.setattr(shared, "DND_ACTIVE", True)
    with respx.mock as router:
        play, _ = _ha(router, {})
        out = await _announce_voice("hi", speaker=SPEAKER, announcement_type="voice_reply", ignore_dnd=True)
    assert out == {"success": True, "speaker": SPEAKER}
    assert play.call_count == 1


async def test_announce_ignore_dnd_keeps_the_voice_session_guard(ann, monkeypatch):
    from orchestrator.reminder_manager import _announce_voice

    monkeypatch.setattr(shared, "is_voice_session_active", lambda *a, **k: True)
    out = await _announce_voice("hi", speaker=SPEAKER, announcement_type="voice_reply", ignore_dnd=True)
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
    from orchestrator.reminder_manager import _announce_voice

    with respx.mock as router:
        _ha(router, {})
        await _announce_voice("hi", speaker=SPEAKER, announcement_type="voice_reply", style=style)
    assert ann.backend.styles == [expected]


async def test_announce_no_fallback_when_disallowed(ann, monkeypatch):
    from orchestrator import reminder_manager
    from orchestrator.reminder_manager import _announce_voice

    monkeypatch.setattr(vs._settings, "voice_satellite_speaker", SPEAKER)
    ann.routes["reminder"] = "media_player.living_room"
    fallbacks = _value(metrics.ANNOUNCE_FALLBACK_TOTAL, {"type": "voice_reply"})
    dead = _value(metrics.ANNOUNCE_SPEAKER_UNAVAILABLE_TOTAL, {"speaker": SPEAKER})
    with respx.mock(assert_all_called=False) as router:
        play, probed = _ha(router, {SPEAKER: "unavailable"})
        out = await _announce_voice(
            "hi", speaker=SPEAKER, announcement_type="voice_reply", allow_fallback=False, record_history=False
        )
    assert out["success"] is False and out["unavailable"] == [SPEAKER]
    assert probed == [SPEAKER]  # the other room's speaker was never even considered
    assert not play.called and ann.backend.styles == []  # no TTS burned
    assert _value(metrics.ANNOUNCE_FALLBACK_TOTAL, {"type": "voice_reply"}) == fallbacks
    # the satellite speaker is a known speaker: it gets its own dead-speaker series
    assert SPEAKER in reminder_manager._known_speakers()
    assert _value(metrics.ANNOUNCE_SPEAKER_UNAVAILABLE_TOTAL, {"speaker": SPEAKER}) == dead + 1


async def test_announce_falls_back_by_default(ann):
    from orchestrator.reminder_manager import _announce_voice

    ann.routes["reminder"] = "media_player.living_room"
    with respx.mock as router:
        play, _ = _ha(router, {SPEAKER: "unavailable"})
        out = await _announce_voice("hi", speaker=SPEAKER, announcement_type="voice_reply")
    assert out["success"] is True and out["fallback"] is True
    assert out["speaker"] == "media_player.living_room"


async def test_announce_record_history_false_keeps_metrics(ann):
    from orchestrator.reminder_manager import _announce_voice

    ok_before = _tts_count("true")
    with respx.mock as router:
        _ha(router, {})
        out = await _announce_voice("hi", speaker=SPEAKER, announcement_type="voice_reply", record_history=False)
    assert out["success"] is True
    assert ann.rows == []
    assert _tts_count("true") == ok_before + 1


async def test_announce_record_history_false_on_failures(ann, monkeypatch):
    from orchestrator.reminder_manager import _announce_voice

    failed_before = _tts_count("false")
    with respx.mock(assert_all_called=False) as router:
        _ha(router, {SPEAKER: "unavailable"})
        await _announce_voice(
            "hi", speaker=SPEAKER, announcement_type="voice_reply", allow_fallback=False, record_history=False
        )
    monkeypatch.setattr(shared, "tts_backend", None, raising=False)
    await _announce_voice("hi", speaker=SPEAKER, announcement_type="voice_reply", record_history=False)
    assert ann.rows == []
    assert _tts_count("false") == failed_before + 1  # the unavailable one (no-backend has no speaker)


async def test_announce_records_history_by_default(ann):
    from orchestrator.reminder_manager import _announce_voice

    with respx.mock as router:
        _ha(router, {})
        await _announce_voice("hello there", speaker=SPEAKER, announcement_type="voice_reply")
    assert len(ann.rows) == 1
    assert ann.rows[0]["text"] == "hello there" and ann.rows[0]["success"] is True


# ---------------------------------------------------------------------------
# cloud_brain: satellite turns never schedule auto_learn
# ---------------------------------------------------------------------------


def test_schedule_auto_learn_skipped_for_satellite_turns(monkeypatch):
    from orchestrator.cloud_brain import CloudBrain

    jobs = {}

    class Sched:
        def add_job(self, func, trigger, run_date, args, id, replace_existing):  # noqa: A002
            jobs[id] = args

        def remove_job(self, job_id):
            jobs.pop(job_id)

    monkeypatch.setattr(shared, "scheduler", Sched())
    monkeypatch.setattr(shared, "AUTO_LEARN_ENABLED", True)
    monkeypatch.setattr(shared, "AUTO_LEARN_DELAY_MINUTES", 10)
    monkeypatch.setattr(shared, "_auto_learn_conversations", {})
    msgs = [{"role": "user", "content": "my name is actually the TV"}]

    def satellite_turn():
        skip_auto_learn_var.set(True)
        CloudBrain._schedule_auto_learn(CloudBrain, msgs)

    contextvars.copy_context().run(satellite_turn)
    assert jobs == {} and shared._auto_learn_conversations == {}

    CloudBrain._schedule_auto_learn(CloudBrain, msgs)  # any other turn still learns
    assert len(jobs) == 1


def test_skip_auto_learn_var_defaults_off():
    assert contextvars.Context().run(skip_auto_learn_var.get) is False


# ---------------------------------------------------------------------------
# config: validate_voice_satellite_config
# ---------------------------------------------------------------------------


_VALID = {
    "VOICE_SATELLITE_ENABLED": "true",
    "VOICE_SATELLITE_HOST": "10.0.0.50",
    "VOICE_SATELLITE_API_KEY": _KEY,
    "VOICE_SATELLITE_SPEAKER": "media_player.kitchen",
    "STT_URL": "http://stt:8003",
    "TTS_URL": "http://tts:8004",
    "HA_URL": "http://ha:8123",
    "HA_TOKEN": "tok",
}


def _settings(**overrides):
    # Settings built directly (no module reload) — works from /app and from
    # inside orchestrator/; no .env file and no host env leak in.
    from orchestrator.config import Settings

    env = {"MODEL_URL": "http://localhost:8080/v1", "MODEL_NAME": "m", **_VALID}
    for k, v in overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    with patch.dict(os.environ, env, clear=True):
        return Settings(_env_file=None)


def test_config_valid_is_enabled():
    s = _settings()
    assert s.voice_satellite_enabled is True
    assert (s.voice_satellite_host, s.voice_satellite_port) == ("10.0.0.50", 6053)
    assert s.voice_satellite_name == "atom-echo-jess"
    assert s.voice_satellite_helios_timeout_seconds == 180


def test_config_default_is_off():
    assert _settings(VOICE_SATELLITE_ENABLED=None).voice_satellite_enabled is False


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"VOICE_SATELLITE_HOST": "fd00::50"}, id="ipv6"),
        pytest.param({"VOICE_SATELLITE_HOST": " 10.0.0.50 "}, id="host-spaces"),
        pytest.param({"VOICE_SATELLITE_SPEAKER": " media_player.kitchen "}, id="speaker-spaces"),
        pytest.param({"VOICE_SATELLITE_PORT": "65535"}, id="port-max"),
        pytest.param({"VOICE_SATELLITE_NAME": "a" * 40}, id="name-40"),
        pytest.param({"VOICE_SATELLITE_NAME": "echo2"}, id="name-plain"),
        pytest.param({"VOICE_SATELLITE_NAME": "atom_echo"}, id="name-underscore"),
    ],
)
def test_config_accepts(overrides):
    s = _settings(**overrides)
    assert s.voice_satellite_enabled is True
    assert s.voice_satellite_host == s.voice_satellite_host.strip()
    assert s.voice_satellite_speaker == "media_player.kitchen"


@pytest.mark.parametrize(
    "overrides, problem",
    [
        pytest.param({"VOICE_SATELLITE_HOST": "atom-echo-jess.local"}, "VOICE_SATELLITE_HOST", id="mdns-host"),
        pytest.param({"VOICE_SATELLITE_HOST": "echo.lan"}, "VOICE_SATELLITE_HOST", id="dns-host"),
        pytest.param({"VOICE_SATELLITE_HOST": None}, "VOICE_SATELLITE_HOST", id="no-host"),
        pytest.param({"VOICE_SATELLITE_HOST": "10.0.0.256"}, "VOICE_SATELLITE_HOST", id="bad-ip"),
        pytest.param({"VOICE_SATELLITE_PORT": "0"}, "VOICE_SATELLITE_PORT", id="port-0"),
        pytest.param({"VOICE_SATELLITE_PORT": "65536"}, "VOICE_SATELLITE_PORT", id="port-high"),
        pytest.param({"VOICE_SATELLITE_NAME": "Atom-Echo"}, "VOICE_SATELLITE_NAME", id="name-upper"),
        pytest.param({"VOICE_SATELLITE_NAME": "-atom"}, "VOICE_SATELLITE_NAME", id="name-dash-first"),
        pytest.param({"VOICE_SATELLITE_NAME": "a" * 41}, "VOICE_SATELLITE_NAME", id="name-41"),
        pytest.param({"VOICE_SATELLITE_SPEAKER": None}, "VOICE_SATELLITE_SPEAKER", id="no-speaker"),
        pytest.param({"VOICE_SATELLITE_SPEAKER": "light.kitchen"}, "VOICE_SATELLITE_SPEAKER", id="not-media"),
        pytest.param(
            {"VOICE_SATELLITE_SPEAKER": "media_player.Kitchen"}, "VOICE_SATELLITE_SPEAKER", id="speaker-upper"
        ),
        pytest.param({"VOICE_SATELLITE_SPEAKER": "media_player.a/../b"}, "VOICE_SATELLITE_SPEAKER", id="speaker-path"),
        pytest.param(
            {"VOICE_SATELLITE_SPEAKER": "media_player.a,media_player.b"}, "VOICE_SATELLITE_SPEAKER", id="speaker-list"
        ),
        pytest.param({"VOICE_SATELLITE_API_KEY": None}, "VOICE_SATELLITE_API_KEY", id="no-key"),
        pytest.param(
            {"VOICE_SATELLITE_API_KEY": base64.b64encode(bytes(16)).decode()}, "VOICE_SATELLITE_API_KEY", id="key-16"
        ),
        pytest.param({"VOICE_SATELLITE_API_KEY": "not base64 at all!!"}, "VOICE_SATELLITE_API_KEY", id="key-junk"),
        pytest.param({"STT_URL": None}, "STT_URL", id="no-stt"),
        pytest.param({"TTS_URL": None}, "STT_URL", id="no-tts"),
        pytest.param({"HA_URL": None}, "STT_URL", id="no-ha-url"),
        pytest.param({"HA_TOKEN": None}, "STT_URL", id="no-ha-token"),
    ],
)
def test_config_disables(overrides, problem, caplog):
    caplog.set_level(logging.ERROR, logger="orchestrator.config")
    s = _settings(**overrides)
    assert s.voice_satellite_enabled is False
    msgs = [r.getMessage() for r in caplog.records if "voice satellite" in r.getMessage()]
    assert len(msgs) == 1 and problem in msgs[0] and "disabling the voice satellite" in msgs[0]
    assert _KEY not in caplog.text


def test_config_disabled_feature_is_not_validated(caplog):
    caplog.set_level(logging.ERROR, logger="orchestrator.config")
    s = _settings(VOICE_SATELLITE_ENABLED="false", VOICE_SATELLITE_HOST="junk.local", VOICE_SATELLITE_API_KEY=None)
    assert s.voice_satellite_enabled is False
    assert not [r for r in caplog.records if "VOICE_SATELLITE" in r.getMessage()]


@pytest.mark.parametrize("raw, expected", [("1000", 600), ("-5", 0), ("0", 0), ("240", 240)])
def test_config_helios_timeout_clamp(raw, expected):
    s = _settings(VOICE_SATELLITE_HELIOS_TIMEOUT_SECONDS=raw, VOICE_SATELLITE_ENABLED="false")
    assert s.voice_satellite_helios_timeout_seconds == expected


@pytest.mark.parametrize(
    "key, ok",
    [
        pytest.param(_KEY, True, id="valid"),
        pytest.param(None, False, id="none"),
        pytest.param("", False, id="empty"),
        pytest.param(base64.b64encode(bytes(31)).decode(), False, id="31-bytes"),
        pytest.param(base64.b64encode(bytes(33)).decode(), False, id="33-bytes"),
        pytest.param(_KEY + "\n", False, id="trailing-newline"),
        pytest.param("é" * 44, False, id="non-ascii"),
        pytest.param(base64.urlsafe_b64encode(b"\xfb" * 32).decode(), False, id="urlsafe-alphabet"),
    ],
)
def test_voice_satellite_key_ok(key, ok):
    from orchestrator.config import voice_satellite_key_ok

    assert voice_satellite_key_ok(key) is ok


# ---------------------------------------------------------------------------
# log_config: third-party payload loggers
# ---------------------------------------------------------------------------


def _record(name: str, msg: str = "Invalid protobuf message type=%s data=%s", args=(7, b"\x01mic-audio")):
    return logging.LogRecord(name, logging.ERROR, __file__, 1, msg, args, None)


@pytest.mark.parametrize("name", ["aioesphomeapi", "aioesphomeapi.connection", "zeroconf", "zeroconf._engine"])
def test_payload_args_dropped(name):
    from orchestrator.log_config import DropPayloadArgsFilter

    rec = _record(name)
    assert DropPayloadArgsFilter().filter(rec) is True
    assert rec.args is None
    assert rec.getMessage() == "Invalid protobuf message type=%s data=%s [args dropped]"


@pytest.mark.parametrize("name", ["orchestrator.voice_satellite", "aioesphomeapix", "httpx", "root"])
def test_other_loggers_keep_their_args(name):
    from orchestrator.log_config import DropPayloadArgsFilter

    rec = _record(name, "count=%d", (3,))
    assert DropPayloadArgsFilter().filter(rec) is True
    assert rec.getMessage() == "count=3"


def test_payload_record_without_args_is_untouched():
    from orchestrator.log_config import DropPayloadArgsFilter

    rec = _record("aioesphomeapi.connection", "Connection lost", None)
    DropPayloadArgsFilter().filter(rec)
    assert rec.getMessage() == "Connection lost"


def test_configure_logging_drops_payload_args(monkeypatch):
    from orchestrator.log_config import DropPayloadArgsFilter, configure_logging

    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", list(root.handlers))  # restored afterwards
    monkeypatch.setattr(root, "level", root.level)
    configure_logging("INFO")
    configure_logging("INFO")
    (handler,) = root.handlers
    assert sum(isinstance(f, DropPayloadArgsFilter) for f in handler.filters) == 1

    buf = io.StringIO()
    handler.setStream(buf)
    logging.getLogger("aioesphomeapi.connection").error("bad frame data=%s", "SECRET-AUDIO-BYTES")
    assert "bad frame data=%s [args dropped]" in buf.getvalue()
    assert "SECRET-AUDIO-BYTES" not in buf.getvalue()


def test_quiet_libs_include_payload_loggers():
    from orchestrator.log_config import quiet_url_logging_libs

    quiet_url_logging_libs()
    for name in ("httpx", "httpcore", "aioesphomeapi", "zeroconf"):
        assert logging.getLogger(name).level == logging.WARNING


# ---------------------------------------------------------------------------
# connection: _connect_reason, _new_client, _session
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc, reason",
    [
        pytest.param(vs._NoApiAudio(), "no_api_audio", id="no-api-audio"),
        pytest.param(vs._BadKey(), "bad_key", id="bad-key-local"),
        pytest.param(InvalidEncryptionKeyAPIError("x"), "bad_key", id="invalid-key"),
        pytest.param(RequiresEncryptionAPIError("x"), "bad_key", id="requires-encryption"),
        pytest.param(EncryptionPlaintextAPIError("x"), "bad_key", id="device-plaintext"),
        pytest.param(EncryptionHelloAPIError("x"), "bad_key", id="encryption-hello"),
        pytest.param(HandshakeAPIError("Handshake failed"), "unreachable", id="handshake-reset"),
        pytest.param(BadNameAPIError("x", "other-node"), "wrong_device", id="bad-name"),
        pytest.param(TimeoutAPIError("x"), "timeout", id="api-timeout"),
        pytest.param(TimeoutError(), "timeout", id="wait-for-timeout"),
        pytest.param(ResolveTimeoutAPIError("x"), "timeout", id="resolve-timeout"),
        pytest.param(SocketAPIError("x"), "unreachable", id="socket"),
        pytest.param(SocketClosedAPIError("x"), "unreachable", id="socket-closed"),
        pytest.param(ResolveAPIError("x"), "unreachable", id="resolve"),
        pytest.param(ConnectionRefusedError(), "unreachable", id="refused"),
        pytest.param(OSError(113, "No route to host"), "unreachable", id="oserror"),
        pytest.param(APIConnectionError("Connection closed"), "error", id="api-generic"),
        pytest.param(InvalidAuthAPIError("x"), "error", id="invalid-auth"),
        pytest.param(ValueError("x"), "error", id="other"),
    ],
)
def test_connect_reason(exc, reason):
    assert vs._connect_reason(exc) == reason
    assert reason in vs._CONNECT_RESULTS


@pytest.mark.parametrize("key", ["", "c2hvcnQ=", "not base64"])
async def test_new_client_refuses_bad_key(make_sat, key):
    sat = make_sat(api_key=key)
    with pytest.raises(vs._BadKey):
        sat._new_client()
    assert vs._connect_reason(vs._BadKey()) == "bad_key"


async def test_new_client_pins_the_device(make_sat):
    from aioesphomeapi import APIClient

    client = make_sat()._new_client()
    assert isinstance(client, APIClient)
    # aioesphomeapi internals: the only place the pin is visible before connecting
    assert client._params.expected_name == NODE
    assert client._params.noise_psk == _KEY
    assert client._params.addresses == ["10.0.0.50"]


async def test_new_client_uses_the_factory(make_sat):
    marker = object()
    assert make_sat(api_key="", client_factory=lambda: marker)._new_client() is marker


async def _start_session(sat, conn):
    task = asyncio.create_task(sat._session())
    await _until(lambda: conn.subscribed.is_set() or task.done())
    return task


async def test_session_without_api_audio(make_sat):
    conn = FakeConn(flags=VoiceAssistantFeature.VOICE_ASSISTANT | VoiceAssistantFeature.SPEAKER)
    sat = make_sat(client=None, client_factory=lambda: conn)
    with pytest.raises(vs._NoApiAudio):
        await sat._session()
    assert conn.handlers is None and conn.disconnected == 1
    assert sat._client is None and _connected() == 0


async def test_session_legacy_firmware_without_api_audio(make_sat):
    conn = FakeConn(api_version=APIVersion(1, 9), info=DeviceInfo(legacy_voice_assistant_version=2))
    sat = make_sat(client=None, client_factory=lambda: conn)
    with pytest.raises(vs._NoApiAudio):
        await sat._session()


async def test_session_connect_failure_cleans_up(make_sat):
    conn = FakeConn(connect_exc=TimeoutAPIError("x"))
    sat = make_sat(client=None, client_factory=lambda: conn)
    with pytest.raises(TimeoutAPIError):
        await sat._session()
    assert conn.disconnected == 1 and _connected() == 0


async def test_session_connect_hang_times_out(make_sat, monkeypatch):
    monkeypatch.setattr(vs, "CONNECT_TIMEOUT_S", 0.01)
    conn = FakeConn()

    async def hang(on_stop=None, login=False):
        await asyncio.Event().wait()

    conn.connect = hang
    sat = make_sat(client=None, client_factory=lambda: conn)
    with pytest.raises(TimeoutError) as err:
        await sat._session()
    assert vs._connect_reason(err.value) == "timeout"


async def test_session_ok_then_drop(make_sat):
    conn = FakeConn()
    sat = make_sat(client=None, client_factory=lambda: conn)
    ok_before = _connects("ok")
    task = await _start_session(sat, conn)

    assert conn.login is True
    assert _connected() == 1 and _connects("ok") == ok_before + 1
    assert sat._client is conn
    assert conn.handlers == {"start": sat.handle_start, "stop": sat.handle_stop, "audio": sat.handle_audio}

    await conn.on_stop(False)
    assert await task is None
    assert _connected() == 0 and sat._client is None
    assert (conn.unsubscribed, conn.disconnected) == (1, 1)


async def test_session_uses_plain_flags_without_api_version(make_sat):
    info = SimpleNamespace(voice_assistant_feature_flags=int(VoiceAssistantFeature.API_AUDIO))
    conn = FakeConn(api_version=None, info=info)
    sat = make_sat(client=None, client_factory=lambda: conn)
    task = await _start_session(sat, conn)
    assert conn.handlers is not None
    await conn.on_stop(True)
    await task


async def test_session_drop_mid_run(make_sat):
    conn = FakeConn()
    sat = make_sat(client=None, client_factory=lambda: conn)
    task = await _start_session(sat, conn)
    before = _turns("dropped")

    await conn.handlers["start"]("c", 0, None, "Hey Jess")
    watcher = sat._run.watcher
    await _say(conn.handlers["audio"], _audio((0.0, 200), (0.1, 400)))
    await conn.on_stop(False)
    await task

    assert conn.names == [RUN_START, STT_START, VAD_START]  # no RUN_END into a dead link
    assert _turns("dropped") == before + 1
    assert sat._busy is False and sat._run is None
    await asyncio.gather(watcher, return_exceptions=True)
    assert watcher.cancelled()
    assert _connected() == 0


# ---------------------------------------------------------------------------
# run_forever: backoff, log throttling, recovery, cancellation
# ---------------------------------------------------------------------------


def _sat_logs(caplog):
    return [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "orchestrator.voice_satellite"]


async def test_run_forever_backoff_and_log_throttle(make_sat, clock, caplog, monkeypatch):
    monkeypatch.setattr(vs, "FAILURE_SUMMARY_S", 100.0)
    excs = [OSError(113, "No route to host")] * 6 + [InvalidEncryptionKeyAPIError("x")] + [OSError(113, "x")]
    conns = iter([FakeConn(connect_exc=e) for e in excs])
    sat = make_sat(client=None, client_factory=lambda: next(conns))
    clock.cancel_at = len(excs)
    caplog.set_level(logging.DEBUG, logger="orchestrator.voice_satellite")
    unreachable, bad_key = _connects("unreachable"), _connects("bad_key")

    with pytest.raises(asyncio.CancelledError):
        await sat.run_forever()

    assert clock.sleeps == [5.0, 10.0, 20.0, 40.0, 80.0, 120.0, 120.0, 120.0]
    logs = _sat_logs(caplog)
    D, I, W, E = logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR
    assert [lvl for lvl, _ in logs] == [W, D, D, D, D, I, E, W]
    assert "connect to 10.0.0.50:6053 failed (unreachable: OSError)" in logs[0][1]
    assert "still not connected (unreachable) — 6 attempts over 155s" in logs[5][1]
    assert "(bad_key: InvalidEncryptionKeyAPIError)" in logs[6][1]
    assert "(unreachable: OSError)" in logs[7][1]
    assert _connects("unreachable") == unreachable + 7
    assert _connects("bad_key") == bad_key + 1


@pytest.mark.parametrize(
    "exc, level",
    [
        pytest.param(BadNameAPIError("x", "other"), logging.ERROR, id="wrong-device"),
        pytest.param(vs._NoApiAudio(), logging.ERROR, id="no-api-audio"),
        pytest.param(TimeoutAPIError("x"), logging.WARNING, id="timeout"),
    ],
)
async def test_first_failure_level(make_sat, clock, caplog, exc, level):
    sat = make_sat(client=None, client_factory=lambda: FakeConn(connect_exc=exc))
    clock.cancel_at = 1
    caplog.set_level(logging.DEBUG, logger="orchestrator.voice_satellite")
    with pytest.raises(asyncio.CancelledError):
        await sat.run_forever()
    assert [lvl for lvl, _ in _sat_logs(caplog)] == [level]


async def test_run_forever_recovers_and_cancels_turns(make_sat, clock, caplog, monkeypatch):
    good = FakeConn()
    conns = iter([FakeConn(connect_exc=OSError(113, "x")), good])
    chat = FakeChat()
    chat.block = asyncio.Event()
    sat = make_sat(chat=chat, client=None, client_factory=lambda: next(conns, None) or FakeConn(connect_exc=OSError()))
    monkeypatch.setattr(sat, "_service_status", AsyncMock(return_value=dict(UP)))
    monkeypatch.setattr(sat, "_transcribe", AsyncMock(return_value="hello"))
    clock.block_at = 3
    caplog.set_level(logging.INFO, logger="orchestrator.voice_satellite")

    task = asyncio.create_task(sat.run_forever())
    await _until(good.subscribed.is_set)
    assert any("connected after 1 failed attempt(s)" in m and "(last: unreachable)" in m for _, m in _sat_logs(caplog))
    assert (sat._fail_count, sat._fail_reason) == (0, "")

    # a turn in flight on the live connection
    await good.handlers["start"]("c", 0, None, "Hey Jess")
    await _say(good.handlers["audio"], UTTERANCE)
    await _until(lambda: chat.calls)
    turn = next(t for t in sat._tasks if t.get_coro().__name__ == "_turn")

    # the link drops after more than a minute up: quick reconnect, back to WARNING
    clock.now += 61
    await good.on_stop(False)
    await _until(lambda: len(clock.sleeps) >= 3)
    assert clock.sleeps == [5.0, 5.0, 10.0]
    assert not turn.done()  # a dropped link doesn't kill a turn mid-answer

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert turn.cancelled()
    assert sat._tasks == set() and sat._busy is False


async def test_shutdown_cancels_the_bridge(make_sat, monkeypatch):
    from orchestrator import orchestrator as orch

    monkeypatch.setattr(shared, "_auto_learn_conversations", {})
    monkeypatch.setattr(shared, "telegram_task", None, raising=False)
    monkeypatch.setattr(orch, "_http", None)
    conn = FakeConn()
    sat = make_sat(client=None, client_factory=lambda: conn)
    child = sat._spawn(asyncio.Event().wait())
    task = asyncio.create_task(sat.run_forever())
    await _until(conn.subscribed.is_set)
    monkeypatch.setattr(shared, "voice_satellite_task", task, raising=False)

    await orch._shutdown_logic()

    assert task.cancelled() and child.cancelled()
    assert shared.voice_satellite_task is None
    assert _connected() == 0 and conn.disconnected == 1


# ---------------------------------------------------------------------------
# satellite_loop / log_loop_exit
# ---------------------------------------------------------------------------


async def test_satellite_loop_seeds_series_and_builds_from_settings(monkeypatch):
    for attr, value in {
        "voice_satellite_name": "atom-echo-seeded",
        "voice_satellite_host": "10.0.0.77",
        "voice_satellite_port": 6054,
        "voice_satellite_api_key": _KEY,
        "voice_satellite_speaker": SPEAKER,
        "voice_satellite_helios_timeout_seconds": 90,
        "voice_satellite_devices": "",
    }.items():
        monkeypatch.setattr(vs._settings, attr, value)
    built = []

    async def fake_run_forever(self):
        built.append(self)

    monkeypatch.setattr(vs.Satellite, "run_forever", fake_run_forever)
    chat = FakeChat()
    await vs.satellite_loop(chat)

    (s,) = built
    assert (s._host, s._port, s._node, s._speaker, s._helios_timeout_s) == (
        "10.0.0.77",
        6054,
        "atom-echo-seeded",
        SPEAKER,
        90,
    )
    assert s._chat is chat
    names = {smp.labels.get("result") for m in metrics.SATELLITE_CONNECT_TOTAL.collect() for smp in m.samples}
    assert set(vs._CONNECT_RESULTS) <= names
    gauges = {smp.labels["device"]: smp.value for m in metrics.SATELLITE_CONNECTED.collect() for smp in m.samples}
    assert gauges["atom-echo-seeded"] == 0


async def test_satellite_loop_seeds_every_listen_end_reason(monkeypatch):
    """Exact, on a fresh counter (other tests increment the real one)."""
    from prometheus_client import CollectorRegistry, Counter

    fresh = Counter("bgw_satellite_listen_end_total", "test copy", ["reason", "device"], registry=CollectorRegistry())
    monkeypatch.setattr(metrics, "SATELLITE_LISTEN_END_TOTAL", fresh)
    monkeypatch.setattr(vs._settings, "voice_satellite_devices", "")
    monkeypatch.setattr(vs._settings, "voice_satellite_name", "atom-echo-seeded")

    async def fake_run_forever(self):
        return None

    monkeypatch.setattr(vs.Satellite, "run_forever", fake_run_forever)
    await vs.satellite_loop(FakeChat())
    seeded = {
        (s.labels["reason"], s.labels["device"]): s.value
        for m in fresh.collect()
        for s in m.samples
        if s.name.endswith("_total")
    }
    assert seeded == {(r, "atom-echo-seeded"): 0.0 for r in vs.LISTEN_END_REASONS}


async def test_log_loop_exit(caplog):
    caplog.set_level(logging.ERROR, logger="orchestrator.voice_satellite")

    async def boom():
        raise RuntimeError("secret detail")

    async def done():
        return None

    crashed = asyncio.create_task(boom())
    returned = asyncio.create_task(done())
    cancelled = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    cancelled.cancel()
    await asyncio.gather(crashed, returned, cancelled, return_exceptions=True)

    for t in (crashed, returned, cancelled):
        vs.log_loop_exit(t)
    msgs = [m for _, m in _sat_logs(caplog)]
    assert msgs == [
        "[SATELLITE] Bridge task exited unexpectedly: RuntimeError",
        "[SATELLITE] Bridge task exited unexpectedly: returned",
    ]
    assert "secret detail" not in caplog.text


def test_drop_payload_filter_strips_exception_text():
    import sys

    from orchestrator.log_config import DropPayloadArgsFilter

    try:
        raise ValueError("payload bytes here")
    except ValueError:
        exc_info = sys.exc_info()
    rec = logging.LogRecord("aioesphomeapi.connection", logging.ERROR, __file__, 1, "Bad message", None, exc_info)
    assert DropPayloadArgsFilter().filter(rec) is True
    assert rec.exc_info is None and rec.exc_text is None
    assert rec.getMessage() == "Bad message [exc: ValueError]"
    assert "payload bytes" not in logging.Formatter().format(rec)

    other = logging.LogRecord("orchestrator.x", logging.ERROR, __file__, 1, "kept", None, exc_info)
    DropPayloadArgsFilter().filter(other)
    assert other.exc_info is exc_info  # other loggers untouched


# ---------------------------------------------------------------------------
# Several devices: VOICE_SATELLITE_DEVICES, one Satellite each, WakeArbiter
# ---------------------------------------------------------------------------

OTHER = "atom-echo-other"
OTHER_SPEAKER = "media_player.den_test"
TWO_DEVICES = f"{NODE}@10.0.0.50={SPEAKER},{OTHER}@10.0.0.51={OTHER_SPEAKER}"


def test_parse_devices_two_rooms():
    from orchestrator.config import SatelliteDevice, parse_voice_satellite_devices

    raw = f" {NODE} @ 10.0.0.50 = {SPEAKER} ,, {OTHER}@fd00::51={OTHER_SPEAKER}"
    devices, problems = parse_voice_satellite_devices(raw)
    assert problems == []
    assert devices == [
        SatelliteDevice(name=NODE, host="10.0.0.50", speaker=SPEAKER),
        SatelliteDevice(name=OTHER, host="fd00::51", speaker=OTHER_SPEAKER),
    ]


@pytest.mark.parametrize(
    "entry, problem",
    [
        pytest.param("atom-echo-x", "is not name@host=speaker", id="no-at"),
        pytest.param("atom-echo-x@10.0.0.60", "is not name@host=speaker", id="no-speaker"),
        pytest.param("Atom@10.0.0.60=media_player.x", "name that is not", id="name-upper"),
        pytest.param("@10.0.0.60=media_player.x", "name that is not", id="name-empty"),
        pytest.param("x@echo.local=media_player.x", "host that is not", id="mdns-host"),
        pytest.param("x@10.0.0.256=media_player.x", "host that is not", id="bad-ip"),
        pytest.param("x@fe80::1%eth0=media_player.x", "host that is not", id="ipv6-scope-id"),
        pytest.param("x@10.0.0.60=light.x", "speaker that is not", id="not-media"),
        pytest.param("x@10.0.0.60=media_player.a/../b", "speaker that is not", id="speaker-path"),
        pytest.param(f"{NODE}@10.0.0.60=media_player.x", "repeats an earlier device name", id="dup-name"),
        pytest.param("x@10.0.0.50=media_player.x", "repeats an earlier device IP", id="dup-ip"),
    ],
)
def test_parse_devices_skips_a_bad_entry(entry, problem):
    from orchestrator.config import parse_voice_satellite_devices

    devices, problems = parse_voice_satellite_devices(f"{NODE}@10.0.0.50={SPEAKER},{entry}")
    assert [d.name for d in devices] == [NODE]
    assert len(problems) == 1 and problems[0].startswith("entry 2 ") and problem in problems[0]


def test_parse_devices_same_ip_written_differently_is_a_repeat():
    from orchestrator.config import parse_voice_satellite_devices

    devices, problems = parse_voice_satellite_devices("a@fd00::51=media_player.a,b@fd00:0:0::51=media_player.b")
    assert [d.name for d in devices] == ["a"] and "repeats an earlier device IP" in problems[0]


def test_parse_devices_limit():
    from orchestrator.config import MAX_VOICE_SATELLITES, parse_voice_satellite_devices

    raw = ",".join(f"d{i}@10.0.1.{i}=media_player.r{i}" for i in range(MAX_VOICE_SATELLITES + 1))
    devices, problems = parse_voice_satellite_devices(raw)
    assert len(devices) == MAX_VOICE_SATELLITES
    assert problems == [f"entry {MAX_VOICE_SATELLITES + 1} is past the {MAX_VOICE_SATELLITES}-device limit"]


def test_config_device_list_wins_over_the_single_device(caplog):
    caplog.set_level(logging.WARNING, logger="orchestrator.config")
    s = _settings(VOICE_SATELLITE_DEVICES=TWO_DEVICES)
    assert s.voice_satellite_enabled is True
    assert [(d.name, d.host, d.speaker) for d in s.voice_satellites()] == [
        (NODE, "10.0.0.50", SPEAKER),
        (OTHER, "10.0.0.51", OTHER_SPEAKER),
    ]
    assert "VOICE_SATELLITE_HOST/NAME/SPEAKER are ignored" in caplog.text


def test_config_device_list_alone_needs_no_single_device_settings(caplog):
    caplog.set_level(logging.WARNING, logger="orchestrator.config")
    s = _settings(VOICE_SATELLITE_DEVICES=TWO_DEVICES, VOICE_SATELLITE_HOST=None, VOICE_SATELLITE_SPEAKER=None)
    assert s.voice_satellite_enabled is True and len(s.voice_satellites()) == 2
    assert "ignored" not in caplog.text


def test_config_bad_entry_is_skipped_and_logged_by_position(caplog):
    caplog.set_level(logging.ERROR, logger="orchestrator.config")
    s = _settings(VOICE_SATELLITE_DEVICES=f"{TWO_DEVICES},secret-ish@{_KEY}=media_player.x")
    assert s.voice_satellite_enabled is True
    assert [d.name for d in s.voice_satellites()] == [NODE, OTHER]
    msgs = [r.getMessage() for r in caplog.records]
    assert msgs == [
        "[CONFIG] VOICE_SATELLITE_DEVICES entry 3 has a host that is not an IP address; that device is skipped."
    ]
    assert _KEY not in caplog.text  # entries are named by position, never echoed


def test_config_no_valid_device_disables(caplog):
    caplog.set_level(logging.ERROR, logger="orchestrator.config")
    s = _settings(VOICE_SATELLITE_DEVICES="x@echo.local=media_player.x")
    assert s.voice_satellite_enabled is False
    assert "VOICE_SATELLITE_DEVICES has no valid name@host=speaker entry" in caplog.text


@pytest.mark.parametrize(
    "overrides, problem",
    [
        pytest.param({"VOICE_SATELLITE_API_KEY": None}, "VOICE_SATELLITE_API_KEY", id="no-key"),
        pytest.param({"VOICE_SATELLITE_PORT": "0"}, "VOICE_SATELLITE_PORT", id="port-0"),
        pytest.param({"HA_TOKEN": None}, "HA_TOKEN", id="no-ha-token"),
    ],
)
def test_config_device_list_still_needs_the_shared_settings(overrides, problem, caplog):
    caplog.set_level(logging.ERROR, logger="orchestrator.config")
    s = _settings(VOICE_SATELLITE_DEVICES=TWO_DEVICES, **overrides)
    assert s.voice_satellite_enabled is False and problem in caplog.text


def test_config_single_device_is_the_default_list():
    devices = _settings().voice_satellites()
    assert [(d.name, d.host, d.speaker) for d in devices] == [("atom-echo-jess", "10.0.0.50", "media_player.kitchen")]


@pytest.mark.parametrize(
    "overrides, problems, enabled",
    [
        pytest.param({}, 0, True, id="single-ok"),
        pytest.param({"VOICE_SATELLITE_DEVICES": TWO_DEVICES}, 0, True, id="list-ok"),
        pytest.param({"VOICE_SATELLITE_DEVICES": f"{TWO_DEVICES},junk,x@y=z"}, 2, True, id="two-skipped"),
        pytest.param({"VOICE_SATELLITE_DEVICES": "junk"}, 2, False, id="list-all-bad"),
        pytest.param({"VOICE_SATELLITE_HOST": "echo.local"}, 1, False, id="single-bad"),
        pytest.param({"VOICE_SATELLITE_DEVICES": TWO_DEVICES, "HA_TOKEN": None}, 1, False, id="list-shared-bad"),
        pytest.param({"VOICE_SATELLITE_ENABLED": "false", "VOICE_SATELLITE_DEVICES": "junk"}, 0, False, id="off"),
        pytest.param({"VOICE_SATELLITE_CONFIG_PROBLEMS": "5"}, 0, True, id="not-settable"),
    ],
)
def test_config_problem_count(overrides, problems, enabled):
    s = _settings(**overrides)
    assert (s.voice_satellite_config_problems, s.voice_satellite_enabled) == (problems, enabled)


@pytest.mark.parametrize("host", ["fe80::1%eth0", "fe80::1%a\nINJECTED"])
def test_config_single_device_refuses_an_ipv6_scope_id(host, caplog):
    caplog.set_level(logging.ERROR, logger="orchestrator.config")
    assert _settings(VOICE_SATELLITE_HOST=host).voice_satellite_enabled is False
    assert "INJECTED" not in caplog.text


def test_known_speakers_include_every_room(monkeypatch):
    """bgw_announcement_speaker_unavailable_total labels each room's speaker, not "other"."""
    from orchestrator import reminder_manager
    from orchestrator.config import settings

    monkeypatch.setattr(settings, "voice_satellite_devices", TWO_DEVICES)
    monkeypatch.setattr(settings, "voice_satellite_speaker", "media_player.left_over")
    known = reminder_manager._known_speakers()
    # the list wins: the ignored single-device speaker gets no series of its own
    assert {SPEAKER, OTHER_SPEAKER} <= known and "media_player.left_over" not in known
    monkeypatch.setattr(settings, "voice_satellite_devices", "")
    assert "media_player.left_over" in reminder_manager._known_speakers()


def test_wake_arbiter(clock):
    arb = vs.WakeArbiter()
    assert arb.claim(NODE, "hey_jess") is True and arb.holder == NODE
    clock.now += vs.DUPLICATE_WAKE_S - 0.1
    assert arb.claim(NODE, "hey_jess") is True  # the holder again: busy handles that, not the arbiter
    assert arb.claim(OTHER, "hey_jess") is False  # the same "Hey Jess", heard twice
    assert arb.claim(OTHER, "okay_jess") is False  # any wake word: one model can fire on the other's phrase
    assert arb.claim(OTHER, "button") is True  # a press is deliberate
    clock.now += 0.2  # past the window from the FIRST claim — the holder's re-wake didn't extend it
    assert arb.claim(OTHER, "hey_jess") is True and arb.holder == OTHER
    assert arb.claim(NODE, "hey_jess") is False


async def test_duplicate_wake_ends_quietly(sat, pipeline, clock, caplog):
    caplog.set_level(logging.INFO, logger="orchestrator.voice_satellite")
    sat._arbiter = vs.WakeArbiter()
    sat._arbiter.claim(OTHER, "hey_jess")
    before = _turns("duplicate")

    assert await sat.handle_start("c", 0, None, "Hey Jess") == 0
    await _say(sat.handle_audio, UTTERANCE)
    await _drain(sat)

    # Ended at the first audio with RUN_END alone: no error code, so no sound.
    assert sat._client.names == [RUN_START, STT_START, RUN_END]
    assert sat._client.errors == []
    assert sat._chat.calls == [] and pipeline.stt.await_count == 0
    assert _turns("duplicate") == before + 1
    assert sat._busy is False and sat._run is None
    assert f"{NODE}: wake (hey_jess) also heard by {OTHER}, which answers" in caplog.text


@pytest.mark.parametrize(
    "drive, result, code",
    [
        pytest.param("watchdog", "no_audio", "jess-error", id="no-audio"),
        pytest.param("device-stop", "no_speech", "jess-no-speech", id="device-stop"),
    ],
)
async def test_duplicate_that_never_streamed_gets_the_normal_end(sat, pipeline, clock, drive, result, code):
    """RUN_END alone is only safe while the device streams; before that, the
    ERROR is what takes it back to idle — so it gets the normal ending (and
    its sound), and the honest result."""
    sat._arbiter = vs.WakeArbiter()
    sat._arbiter.claim(OTHER, "hey_jess")
    before = (_turns(result), _turns("duplicate"))
    await sat.handle_start("c", 0, None, "Hey Jess")
    if drive == "watchdog":
        await sat._run.watcher  # FIRST_AUDIO_TIMEOUT_S passes with nothing streamed
    else:
        await sat.handle_stop(False)
    assert sat._client.names == [RUN_END, ERROR] and sat._client.errors == [code]
    assert (_turns(result), _turns("duplicate")) == (before[0] + 1, before[1])
    assert sat._busy is False


async def test_busy_device_keeps_the_wake_word(make_sat, pipeline, later):
    """A busy room says "busy" rather than the next room answering the question.
    Real clock: the fake one would run the 4 s no-audio watchdog inside _tick()."""
    arbiter = vs.WakeArbiter()
    busy = make_sat(node_name=OTHER, arbiter=arbiter)
    busy._busy = True  # mid-turn
    other_room = make_sat(arbiter=arbiter)

    await busy.handle_start("c1", 0, None, "Hey Jess")
    await other_room.handle_start("c2", 0, None, "Hey Jess")
    await _tick()
    assert busy._client.errors == ["jess-busy"]
    assert other_room._run is not None and other_room._run.duplicate is True
    await other_room.handle_audio(_tone(0.1))
    assert other_room._client.names == [RUN_START, STT_START, RUN_END] and other_room._client.errors == []


async def test_two_devices_answer_on_their_own_speakers(make_sat, monkeypatch, clock):
    arbiter = vs.WakeArbiter()
    one = make_sat(arbiter=arbiter)
    two = make_sat(node_name=OTHER, speaker=OTHER_SPEAKER, arbiter=arbiter)
    spoken_on = []

    async def announce(text, **kw):
        spoken_on.append(kw["speaker"])
        return {"success": True}

    monkeypatch.setattr("orchestrator.reminder_manager._announce_voice", announce)
    for s in (one, two):
        monkeypatch.setattr(s, "_service_status", AsyncMock(return_value=dict(UP)))
        monkeypatch.setattr(s, "_transcribe", AsyncMock(return_value="what time is it"))
    before = (_turns("ok", helios="awake", device=NODE), _turns("ok", helios="awake", device=OTHER))

    await one.handle_start("c1", 0, None, "Hey Jess")
    await _say(one.handle_audio, UTTERANCE)
    clock.now += vs.DUPLICATE_WAKE_S + 1  # a separate question in the other room
    await two.handle_start("c2", 0, None, "Hey Jess")
    await _say(two.handle_audio, UTTERANCE)
    await _drain(one)
    await _drain(two)

    assert spoken_on == [SPEAKER, OTHER_SPEAKER]
    after = (_turns("ok", helios="awake", device=NODE), _turns("ok", helios="awake", device=OTHER))
    assert (after[0] - before[0], after[1] - before[1]) == (1, 1)


async def test_satellite_loop_runs_every_device(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="orchestrator.voice_satellite")
    monkeypatch.setattr(vs._settings, "voice_satellite_devices", TWO_DEVICES)
    monkeypatch.setattr(vs._settings, "voice_satellite_api_key", _KEY)
    started = []

    async def fake_run_forever(self):
        started.append(self)
        if self._node == OTHER:
            raise RuntimeError("bug in one room")

    monkeypatch.setattr(vs.Satellite, "run_forever", fake_run_forever)
    await vs.satellite_loop(FakeChat())

    assert [(s._node, s._host, s._speaker) for s in started] == [
        (NODE, "10.0.0.50", SPEAKER),
        (OTHER, "10.0.0.51", OTHER_SPEAKER),
    ]
    assert started[0]._arbiter is started[1]._arbiter is not None
    assert _connected(NODE) == 0 and _connected(OTHER) == 0
    assert f"Bridge started for {NODE}, {OTHER}" in caplog.text
    assert f"{OTHER}: device loop exited unexpectedly: RuntimeError" in caplog.text
    assert "bug in one room" not in caplog.text
    seeded = {
        smp.labels["device"]
        for m in metrics.SATELLITE_TURN_TOTAL.collect()
        for smp in m.samples
        if smp.labels.get("result") == "duplicate"
    }
    assert {NODE, OTHER} <= seeded


async def test_satellite_loop_shutdown_waits_for_every_device(monkeypatch):
    """Cancelling the bridge returns only after EVERY device's cleanup — a bare
    gather returned after the first, and shutdown then closed the HTTP client
    under the other room's unwinding turn."""
    monkeypatch.setattr(vs._settings, "voice_satellite_devices", TWO_DEVICES)
    monkeypatch.setattr(vs._settings, "voice_satellite_api_key", _KEY)
    running, cleaned = set(), []

    async def fake_run_forever(self):
        running.add(self._node)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            for _ in range(20 if self._node == OTHER else 1):  # OTHER unwinds slowly
                await asyncio.sleep(0)
            cleaned.append(self._node)
            raise

    monkeypatch.setattr(vs.Satellite, "run_forever", fake_run_forever)
    task = asyncio.create_task(vs.satellite_loop(FakeChat()))
    await _until(lambda: running == {NODE, OTHER})
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sorted(cleaned) == sorted([NODE, OTHER])


async def test_satellite_loop_one_device_failing_keeps_the_other_running(monkeypatch):
    monkeypatch.setattr(vs._settings, "voice_satellite_devices", TWO_DEVICES)
    monkeypatch.setattr(vs._settings, "voice_satellite_api_key", _KEY)
    still_running = asyncio.Event()

    async def fake_run_forever(self):
        if self._node == OTHER:
            raise RuntimeError("x")
        still_running.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(vs.Satellite, "run_forever", fake_run_forever)
    task = asyncio.create_task(vs.satellite_loop(FakeChat()))
    await asyncio.wait_for(still_running.wait(), 1)
    await _tick()
    assert not task.done()  # the good room keeps going
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Several devices: gaps — cross-device state, arbiter edges, loop + config edges
# ---------------------------------------------------------------------------

THIRD = "atom-echo-third"


def _wire(sat, monkeypatch, transcript: str = "what time is it") -> SimpleNamespace:
    """The ``pipeline`` fixture for any satellite: Helios up, STT words, playback ok."""
    p = SimpleNamespace(
        status=AsyncMock(return_value=dict(UP)),
        stt=AsyncMock(return_value=transcript),
        speak=AsyncMock(return_value="ok"),
    )
    monkeypatch.setattr(sat, "_service_status", p.status)
    monkeypatch.setattr(sat, "_transcribe", p.stt)
    monkeypatch.setattr(sat, "_speak", p.speak)
    return p


def _bytes_for(ms: int) -> int:
    return vs.SAMPLE_RATE * vs.SAMPLE_WIDTH * ms // 1000


@pytest.mark.parametrize(
    "entry, problem",
    [
        pytest.param("a@b@10.0.0.60=media_player.x", "host that is not", id="two-at"),
        pytest.param("x@@10.0.0.60=media_player.x", "host that is not", id="at-at"),
        pytest.param("x@=media_player.x", "host that is not", id="empty-host"),
        pytest.param("x@10.0.0.60=media_player.x=y", "speaker that is not", id="two-eq"),
        pytest.param("x@10.0.0.60==media_player.x", "speaker that is not", id="eq-eq"),
        pytest.param("x@10.0.0.60=", "speaker that is not", id="empty-speaker"),
        pytest.param("x=y@10.0.0.60=media_player.x", "name that is not", id="eq-in-name"),
        pytest.param("x=media_player.x@10.0.0.60", "is not name@host=speaker", id="swapped"),
    ],
)
def test_parse_devices_repeated_or_misplaced_separators(entry, problem):
    from orchestrator.config import parse_voice_satellite_devices

    devices, problems = parse_voice_satellite_devices(f"{NODE}@10.0.0.50={SPEAKER},{entry}")
    assert [d.name for d in devices] == [NODE]
    assert len(problems) == 1 and problems[0].startswith("entry 2 ") and problem in problems[0]
    assert "10.0.0.60" not in problems[0]  # named by position, never echoed


@pytest.mark.parametrize(
    "raw, enabled, problems, devices",
    [
        pytest.param("", True, 0, [("atom-echo-jess", "10.0.0.50", "media_player.kitchen")], id="empty"),
        pytest.param("  \t ", True, 0, [("atom-echo-jess", "10.0.0.50", "media_player.kitchen")], id="blank"),
        # Not blank, so the list is used — and it lists nothing: off, loudly.
        pytest.param(" , ,", False, 1, [], id="commas-only"),
    ],
)
def test_config_blank_device_list_falls_back_to_the_single_device(raw, enabled, problems, devices, caplog):
    caplog.set_level(logging.WARNING, logger="orchestrator.config")
    s = _settings(VOICE_SATELLITE_DEVICES=raw)
    assert (s.voice_satellite_enabled, s.voice_satellite_config_problems) == (enabled, problems)
    assert [(d.name, d.host, d.speaker) for d in s.voice_satellites()] == devices
    assert "are ignored" not in caplog.text


@pytest.mark.parametrize(
    "overrides, problems",
    [
        # skipped entries AND a shared-setting failure: both counted
        pytest.param({"VOICE_SATELLITE_DEVICES": f"{TWO_DEVICES},junk", "HA_TOKEN": None}, 2, id="skip-and-shared"),
        pytest.param(
            {"VOICE_SATELLITE_DEVICES": f"{TWO_DEVICES},junk", "VOICE_SATELLITE_API_KEY": None}, 2, id="skip-and-key"
        ),
        pytest.param({"VOICE_SATELLITE_API_KEY": None}, 1, id="single-no-key"),
        pytest.param({"VOICE_SATELLITE_PORT": "0"}, 1, id="single-port"),
        pytest.param({"VOICE_SATELLITE_SPEAKER": None}, 1, id="single-no-speaker"),
        # the env var can't set it, even with the feature off
        pytest.param(
            {"VOICE_SATELLITE_ENABLED": "false", "VOICE_SATELLITE_CONFIG_PROBLEMS": "5"}, 0, id="off-not-settable"
        ),
    ],
)
def test_config_problem_count_more_cases(overrides, problems):
    s = _settings(**overrides)
    assert s.voice_satellite_config_problems == problems
    assert s.voice_satellite_enabled is False


def test_known_speakers_leave_out_a_skipped_entrys_speaker(monkeypatch):
    """Only valid entries become speaker labels: a typo'd entry's text never does."""
    from orchestrator import reminder_manager
    from orchestrator.config import settings

    monkeypatch.setattr(settings, "voice_satellite_devices", f"{TWO_DEVICES},x@echo.local=media_player.typo_room")
    known = reminder_manager._known_speakers()
    assert {SPEAKER, OTHER_SPEAKER} <= known and "media_player.typo_room" not in known


def test_wake_arbiter_window_boundary(clock):
    arb = vs.WakeArbiter()
    start = clock.now
    assert arb.claim(NODE, "hey_jess") is True
    clock.now = start + vs.DUPLICATE_WAKE_S - 0.001
    assert arb.claim(OTHER, "hey_jess") is False  # just inside the window
    clock.now = start + vs.DUPLICATE_WAKE_S
    assert arb.claim(OTHER, "hey_jess") is True  # exactly at it: a new wake word
    assert arb.holder == OTHER


def test_wake_arbiter_zero_window_never_holds(clock):
    arb = vs.WakeArbiter(window_s=0)
    assert arb.claim(NODE, "hey_jess") is True
    assert arb.claim(OTHER, "hey_jess") is True
    assert arb.holder == OTHER


def test_wake_arbiter_button_does_not_take_the_claim(clock):
    arb = vs.WakeArbiter()
    assert arb.claim(NODE, "hey_jess") is True
    clock.now += 1.0
    assert arb.claim(OTHER, "button") is True
    assert arb.holder == NODE  # the press neither steals nor restarts the claim
    clock.now += 1.0
    assert arb.claim(THIRD, "hey_jess") is False  # still NODE's wake word
    assert arb.claim(NODE, "okay_jess") is True
    clock.now += 1.0  # 3 s after NODE's claim, 2 s after the press
    assert arb.claim(THIRD, "hey_jess") is True and arb.holder == THIRD


async def test_duplicate_on_one_device_while_the_other_listens(make_sat, monkeypatch):
    """B's duplicate run starts and ends while A is mid-question: no _run/_busy
    crosses over, each device's counters land on its own label."""
    arbiter = vs.WakeArbiter()
    a = make_sat(arbiter=arbiter)
    b = make_sat(node_name=OTHER, speaker=OTHER_SPEAKER, arbiter=arbiter)
    pa, pb = _wire(a, monkeypatch), _wire(b, monkeypatch)
    split = _bytes_for(800)  # beep + 600 ms of speech: listening, not ended
    before = {
        "ok": (_turns("ok", helios="awake", device=NODE), _turns("ok", helios="awake", device=OTHER)),
        "dup": (_turns("duplicate", device=NODE), _turns("duplicate", device=OTHER)),
        "ends": (_listen_ends(NODE), _listen_ends(OTHER)),
    }

    await a.handle_start("c1", 0, None, "Hey Jess")
    await _say(a.handle_audio, UTTERANCE[:split])
    a_run = a._run
    assert a._client.names == [RUN_START, STT_START, VAD_START] and a._busy is True

    await b.handle_start("c2", 0, None, "Hey Jess")
    assert b._run is not None and b._run.duplicate is True and b._busy is True
    await b.handle_audio(UTTERANCE[:1024])
    assert b._client.names == [RUN_START, STT_START, RUN_END] and b._client.errors == []
    assert b._run is None and b._busy is False
    assert a._run is a_run and a._busy is True and not a_run.done  # A untouched

    await _say(a.handle_audio, UTTERANCE[split:])
    await _drain(a)
    await _drain(b)

    assert a._client.names == [RUN_START, STT_START, VAD_START, RUN_END]
    assert (pa.speak.await_count, pb.speak.await_count) == (1, 0)
    assert (pa.stt.await_count, pb.stt.await_count) == (1, 0)
    assert len(a._chat.calls) == 1 and b._chat.calls == []
    assert (len(a._turn_starts), len(b._turn_starts)) == (1, 0)  # a duplicate uses no turn
    assert a._busy is False and b._busy is False
    ok = (_turns("ok", helios="awake", device=NODE), _turns("ok", helios="awake", device=OTHER))
    dup = (_turns("duplicate", device=NODE), _turns("duplicate", device=OTHER))
    assert (ok[0] - before["ok"][0], ok[1] - before["ok"][1]) == (1, 0)
    assert (dup[0] - before["dup"][0], dup[1] - before["dup"][1]) == (0, 1)
    ends_a, ends_b = _listen_ends(NODE), _listen_ends(OTHER)
    assert {r: ends_a[r] - before["ends"][0][r] for r in ends_a} == {r: float(r == "speech_end") for r in ends_a}
    assert {r: ends_b[r] - before["ends"][1][r] for r in ends_b} == {r: float(r == "duplicate") for r in ends_b}


async def test_rate_limited_device_still_claims(make_sat, monkeypatch, later):
    """Like a busy room: a rate-limited device says "busy" and keeps the wake
    word — the next room doesn't answer instead."""
    arbiter = vs.WakeArbiter()
    limited = make_sat(arbiter=arbiter)
    limited._turn_starts.extend([time.monotonic()] * vs.RATE_LIMIT_TURNS)
    other_room = make_sat(node_name=OTHER, speaker=OTHER_SPEAKER, arbiter=arbiter)
    p_other = _wire(other_room, monkeypatch)
    before = (_turns("rate_limited", device=NODE), _turns("duplicate", device=OTHER))

    await limited.handle_start("c1", 0, None, "Hey Jess")
    await other_room.handle_start("c2", 0, None, "Hey Jess")
    await _tick()
    assert limited._client.errors == ["jess-busy"] and limited._run is None
    assert arbiter.holder == NODE
    await _say(other_room.handle_audio, UTTERANCE)
    await _drain(other_room)
    assert other_room._client.names == [RUN_START, STT_START, RUN_END] and other_room._client.errors == []
    assert p_other.speak.await_count == 0
    assert (_turns("rate_limited", device=NODE), _turns("duplicate", device=OTHER)) == (before[0] + 1, before[1] + 1)


async def test_button_on_another_device_during_a_claim_answers(make_sat, monkeypatch):
    """A press is deliberate: B answers it on B's speaker even inside A's window."""
    arbiter = vs.WakeArbiter()
    a = make_sat(arbiter=arbiter)
    b = make_sat(node_name=OTHER, speaker=OTHER_SPEAKER, arbiter=arbiter)
    for s in (a, b):
        monkeypatch.setattr(s, "_service_status", AsyncMock(return_value=dict(UP)))
        monkeypatch.setattr(s, "_transcribe", AsyncMock(return_value="what time is it"))
    spoken_on = []

    async def announce(text, **kw):
        spoken_on.append(kw["speaker"])
        return {"success": True}

    monkeypatch.setattr("orchestrator.reminder_manager._announce_voice", announce)
    before = (_turns("ok", "hey_jess", "awake", NODE), _turns("ok", "button", "awake", OTHER))

    await a.handle_start("c1", 0, None, "Hey Jess")
    await b.handle_start("c2", 0, None, None)  # button press
    assert b._run is not None and b._run.duplicate is False
    await _say(a.handle_audio, UTTERANCE)
    await _say(b.handle_audio, UTTERANCE)
    await _drain(a)
    await _drain(b)

    assert sorted(spoken_on) == sorted([SPEAKER, OTHER_SPEAKER])
    assert b._client.names == [RUN_START, STT_START, VAD_START, RUN_END]
    after = (_turns("ok", "hey_jess", "awake", NODE), _turns("ok", "button", "awake", OTHER))
    assert (after[0] - before[0], after[1] - before[1]) == (1, 1)
    assert arbiter.holder == NODE


async def test_device_answers_its_own_wake_after_a_duplicate(make_sat, monkeypatch, clock):
    """No duplicate state outlives its run: the next wake past the window is a turn."""
    arbiter = vs.WakeArbiter()
    arbiter.claim(OTHER, "hey_jess")  # the other room took this one
    sat = make_sat(arbiter=arbiter)
    p = _wire(sat, monkeypatch)

    await sat.handle_start("c1", 0, None, "Hey Jess")
    await sat.handle_audio(_tone(0.1))
    assert sat._client.names == [RUN_START, STT_START, RUN_END]

    sat._client.events.clear()
    clock.now += vs.DUPLICATE_WAKE_S
    await sat.handle_start("c2", 0, None, "Hey Jess")
    assert sat._run is not None and sat._run.duplicate is False and arbiter.holder == NODE
    await _say(sat.handle_audio, UTTERANCE)
    await _drain(sat)
    assert sat._client.names == [RUN_START, STT_START, VAD_START, RUN_END]
    assert p.speak.await_count == 1


async def test_duplicate_aborted_before_audio_is_just_aborted(sat, pipeline):
    sat._arbiter = vs.WakeArbiter()
    sat._arbiter.claim(OTHER, "hey_jess")
    before = (_turns("aborted"), _turns("duplicate"))
    await sat.handle_start("c", 0, None, "Hey Jess")
    watcher = sat._run.watcher
    await sat.handle_stop(True)
    await asyncio.gather(watcher, return_exceptions=True)
    assert sat._client.names == []  # the device ended its own run: nothing to send
    assert (_turns("aborted"), _turns("duplicate")) == (before[0] + 1, before[1])
    assert sat._busy is False and sat._run is None and watcher.cancelled()


@pytest.mark.parametrize("refuse", ["busy", "rate_limited"])
async def test_refusing_second_device_stays_quiet(make_sat, monkeypatch, later, refuse):
    """Module docstring: one "Hey Jess" heard by two devices — the first to
    report it answers (or says "busy"), the other ends its run SILENTLY once
    it streams. A second device that is itself busy (its own turn in flight)
    or rate-limited must not play the busy sound in its room while the first
    room answers — and must not clear the busy flag of its own running turn."""
    arbiter = vs.WakeArbiter()
    first = make_sat(arbiter=arbiter)
    _wire(first, monkeypatch)
    second = make_sat(node_name=OTHER, speaker=OTHER_SPEAKER, arbiter=arbiter)
    if refuse == "busy":
        second._busy = True  # its own earlier question is still being answered
    else:
        second._turn_starts.extend([time.monotonic()] * vs.RATE_LIMIT_TURNS)

    await first.handle_start("c1", 0, None, "Hey Jess")
    await second.handle_start("c2", 0, None, "Hey Jess")
    await second.handle_audio(_tone(0.1))
    await _tick()

    assert second._client.errors == []  # no busy sound for another room's wake word
    assert second._client.names[-1:] == [RUN_END]  # its run ended, device back to idle
    assert second._busy is (refuse == "busy")  # its own turn still owns the flag
    await _say(first.handle_audio, UTTERANCE)
    await _drain(first)
    assert first._client.errors == []


async def test_run_device_reraises_cancellation(make_sat, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="orchestrator.voice_satellite")
    sat = make_sat()
    monkeypatch.setattr(sat, "run_forever", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await vs._run_device(sat)
    assert not [m for lvl, m in _sat_logs(caplog) if lvl >= logging.ERROR]


async def test_satellite_loop_without_devices_logs_and_returns(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="orchestrator.voice_satellite")
    monkeypatch.setattr(vs._settings, "voice_satellite_devices", "junk")  # set, but nothing valid
    started = []

    async def fake_run_forever(self):
        started.append(self)

    monkeypatch.setattr(vs.Satellite, "run_forever", fake_run_forever)
    assert await vs.satellite_loop(FakeChat()) is None
    assert started == []
    logs = _sat_logs(caplog)
    assert (logging.ERROR, "[SATELLITE] No voice satellite device is configured") in logs
    assert not any("Bridge started" in m for _, m in logs)


async def test_satellite_loop_seeds_every_series_for_every_device(monkeypatch):
    """Exact, on fresh metrics: each device gets its connected gauge at 0, every
    connect result, every listen-end reason and the duplicate turn series."""
    from prometheus_client import CollectorRegistry, Counter, Gauge

    reg = CollectorRegistry()
    fresh = {
        "SATELLITE_CONNECTED": Gauge("bgw_satellite_connected", "t", ["device"], registry=reg),
        "SATELLITE_CONNECT_TOTAL": Counter("bgw_satellite_connect", "t", ["result", "device"], registry=reg),
        "SATELLITE_LISTEN_END_TOTAL": Counter("bgw_satellite_listen_end", "t", ["reason", "device"], registry=reg),
        "SATELLITE_TURN_TOTAL": Counter(
            "bgw_satellite_turn", "t", ["result", "wake_word", "helios", "device"], registry=reg
        ),
    }
    for name, metric in fresh.items():
        monkeypatch.setattr(metrics, name, metric)
    monkeypatch.setattr(vs._settings, "voice_satellite_devices", TWO_DEVICES)

    async def fake_run_forever(self):
        return None

    monkeypatch.setattr(vs.Satellite, "run_forever", fake_run_forever)
    await vs.satellite_loop(FakeChat())

    def samples(metric, suffix):
        return {
            tuple(s.labels.values()): s.value for m in metric.collect() for s in m.samples if s.name.endswith(suffix)
        }

    devs = (NODE, OTHER)
    assert samples(fresh["SATELLITE_CONNECTED"], "connected") == {(d,): 0.0 for d in devs}
    assert samples(fresh["SATELLITE_CONNECT_TOTAL"], "_total") == {
        (r, d): 0.0 for r in vs._CONNECT_RESULTS for d in devs
    }
    assert samples(fresh["SATELLITE_LISTEN_END_TOTAL"], "_total") == {
        (r, d): 0.0 for r in vs.LISTEN_END_REASONS for d in devs
    }
    assert samples(fresh["SATELLITE_TURN_TOTAL"], "_total") == {
        ("duplicate", "hey_jess", "unchecked", d): 0.0 for d in devs
    }


async def test_connect_counts_land_on_their_own_device(make_sat, clock):
    a = make_sat(client=None, client_factory=lambda: FakeConn(connect_exc=OSError(113, "x")))
    good = FakeConn()
    b = make_sat(node_name=OTHER, client=None, client_factory=lambda: good)
    before = {(r, d): _connects(r, d) for r in ("unreachable", "ok") for d in (NODE, OTHER)}

    clock.cancel_at = 1
    with pytest.raises(asyncio.CancelledError):
        await a.run_forever()
    session = await _start_session(b, good)
    assert (_connected(NODE), _connected(OTHER)) == (0, 1)
    await good.on_stop(False)
    await session

    delta = {k: _connects(*k) - v for k, v in before.items()}
    assert delta == {("unreachable", NODE): 1, ("unreachable", OTHER): 0, ("ok", NODE): 0, ("ok", OTHER): 1}
    assert _connected(OTHER) == 0


def test_startup_exports_the_config_problems_gauge_unconditionally():
    """orchestrator._startup_logic sets bgw_satellite_config_problems at its own
    top level, before (not inside) `if _cfg.voice_satellite_enabled:` — a config
    that DISABLED the feature is exactly the case the alert exists for.
    (Structural: running _startup_logic needs the whole app.)"""
    src_path = os.path.join(os.path.dirname(vs.__file__), "orchestrator.py")
    with open(src_path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    (startup,) = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_startup_logic"]

    def is_gauge_set(stmt):
        call = stmt.value if isinstance(stmt, ast.Expr) else None
        return (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "set"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "SATELLITE_CONFIG_PROBLEMS"
            and len(call.args) == 1
            and ast.unparse(call.args[0]) == "_cfg.voice_satellite_config_problems"
        )

    def is_enabled_if(stmt):
        return isinstance(stmt, ast.If) and ast.unparse(stmt.test) == "_cfg.voice_satellite_enabled"

    body = startup.body
    sets = [i for i, s in enumerate(body) if is_gauge_set(s)]
    gates = [i for i, s in enumerate(body) if is_enabled_if(s)]
    assert len(sets) == 1 and len(gates) == 1 and sets[0] < gates[0]
    gauge = metrics.SATELLITE_CONFIG_PROBLEMS
    assert gauge._labelnames == () and gauge._name == "bgw_satellite_config_problems"
