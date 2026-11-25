"""Voice satellite bridge (orchestrator/voice_satellite.py) and the changes it
needs elsewhere: cloud_brain's skip_auto_learn_var, the config validator,
log_config.DropPayloadArgsFilter and the shutdown hook. (The
reminder_manager._announce_voice flags it adds are in
test_announce_speaker_liveness.py.)

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
import sys
import textwrap
import time
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from aioesphomeapi import APIClient, APIVersion, DeviceInfo, VoiceAssistantFeature
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
from prometheus_client import CollectorRegistry, Counter, Gauge
from starlette.responses import JSONResponse

from orchestrator import helios_power, metrics, reminder_manager, shared, voice_style
from orchestrator import voice_satellite as vs
from orchestrator.cloud_brain import CloudBrain, conversation_id_var, skip_auto_learn_var
from orchestrator.config import (
    MAX_VOICE_SATELLITES,
    SatelliteDevice,
    Settings,
    parse_voice_satellite_devices,
    settings,
    voice_satellite_key_ok,
)
from orchestrator.log_config import (
    DropPayloadArgsFilter,
    configure_logging,
    quiet_url_logging_libs,
    request_id_var,
)
from orchestrator.service_registry import ServiceRegistry, services
from orchestrator.tests._helpers import metric_value
from orchestrator.voice_satellite import EnergyVad, VadConfig

_KEY = base64.b64encode(bytes(range(32))).decode()
NODE = "atom-echo-test"
SPEAKER = "media_player.kitchen_test"
STT = "http://stt.test:8003"

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


def _turns(result: str, wake: str = "hey_jess", helios: str = "unchecked", device: str = NODE) -> float:
    return metric_value(metrics.SATELLITE_TURN_TOTAL, result=result, wake_word=wake, helios=helios, device=device)


def _turn_seconds_count(helios: str) -> float:
    return metric_value(metrics.SATELLITE_TURN_SECONDS, "_count", helios=helios)


def _connects(result: str, device: str = NODE) -> float:
    return metric_value(metrics.SATELLITE_CONNECT_TOTAL, result=result, device=device)


def _connected(device: str = NODE) -> float:
    return metric_value(metrics.SATELLITE_CONNECTED, device=device)


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


def _wire(sat, monkeypatch, transcript: str = "what time is it") -> SimpleNamespace:
    """Helios ready, STT returns words, playback succeeds — on any satellite."""
    p = SimpleNamespace(
        status=AsyncMock(return_value=dict(UP)),
        stt=AsyncMock(return_value=transcript),
        speak=AsyncMock(return_value="ok"),
    )
    monkeypatch.setattr(sat, "_service_status", p.status)
    monkeypatch.setattr(sat, "_transcribe", p.stt)
    monkeypatch.setattr(sat, "_speak", p.speak)
    return p


@pytest.fixture
def pipeline(sat, monkeypatch):
    """``_wire`` on the ``sat`` fixture."""
    return _wire(sat, monkeypatch)


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
    return {r: metric_value(metrics.SATELLITE_LISTEN_END_TOTAL, reason=r, device=device) for r in vs.LISTEN_END_REASONS}


async def _ends_speech_end(sat):
    await sat.handle_start("c", 0, None, "Hey Jess")
    await _say(sat.handle_audio, UTTERANCE)


async def _ends_max_utterance(sat):
    await sat.handle_start("c", 0, None, "Hey Jess")
    await _say(sat.handle_audio, NONSTOP_16S)


async def _ends_no_speech(sat):
    await sat.handle_start("c", 0, None, "Okay Jess")
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


_ANSWERED = [RUN_START, STT_START, VAD_START, RUN_END]
_NO_SPEECH = [RUN_START, STT_START, RUN_END, ERROR]

# How a run ends: the listen_end reason, what the device was sent, the turn's
# result + wake label, the error code it heard, and whether Jess answered.
_LISTEN_END_PATHS = [
    pytest.param(_ends_speech_end, "speech_end", _ANSWERED, ("ok", "hey_jess"), [], True, id="speech-end"),
    pytest.param(_ends_max_utterance, "max_utterance", _ANSWERED, ("ok", "hey_jess"), [], True, id="max-utterance"),
    pytest.param(
        _ends_no_speech, "no_speech", _NO_SPEECH, ("no_speech", "okay_jess"), ["jess-no-speech"], False, id="no-speech"
    ),
    pytest.param(_ends_device_stop, "device_stop", _ANSWERED, ("ok", "hey_jess"), [], True, id="device-stop"),
    pytest.param(
        _ends_device_stop_quiet,
        "device_stop",
        _NO_SPEECH,
        ("no_speech", "hey_jess"),
        ["jess-no-speech"],
        False,
        id="device-stop-quiet",
    ),
    pytest.param(_ends_wall_timeout, "wall_timeout", _ANSWERED, ("ok", "hey_jess"), [], True, id="wall-timeout"),
    pytest.param(
        _ends_wall_timeout_quiet,
        "wall_timeout",
        _NO_SPEECH,
        ("no_speech", "hey_jess"),
        ["jess-no-speech"],
        False,
        id="wall-timeout-quiet",
    ),
    pytest.param(
        _ends_no_audio, "no_audio", [RUN_END, ERROR], ("no_audio", "hey_jess"), ["jess-error"], False, id="no-audio"
    ),
    # The device ended its own run: nothing goes back to it.
    pytest.param(
        _ends_aborted, "aborted", [RUN_START, STT_START, VAD_START], ("aborted", "hey_jess"), [], False, id="aborted"
    ),
    # Another room answers: RUN_END alone, no error code (so no sound).
    pytest.param(
        _ends_duplicate,
        "duplicate",
        [RUN_START, STT_START, RUN_END],
        ("duplicate", "hey_jess"),
        [],
        False,
        id="duplicate",
    ),
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


@pytest.mark.parametrize("drive, reason, events, turn, errors, answered", _LISTEN_END_PATHS)
async def test_listen_end_counts_its_own_reason_once(
    sat, pipeline, clock, caplog, drive, reason, events, turn, errors, answered
):
    caplog.set_level(logging.INFO, logger="orchestrator.voice_satellite")
    result, wake = turn
    helios = "awake" if answered else "unchecked"
    before, turns_before = _listen_ends(), _turns(result, wake=wake, helios=helios)
    await drive(sat)
    await _drain(sat)
    # The device's own stop (and stray audio) after we ended the run is not
    # a second ending, and sends the device nothing more.
    await sat.handle_stop(False)
    await sat.handle_stop(True)
    await sat.handle_audio(_tone(0.1))
    after = _listen_ends()
    assert {r: after[r] - before[r] for r in after} == {r: float(r == reason) for r in after}
    assert sat._client.names == events and sat._client.errors == errors
    assert _turns(result, wake=wake, helios=helios) == turns_before + 1
    assert pipeline.speak.await_count == int(answered)
    if not answered:  # nothing heard: no STT, no brain, and no turn used up from the rate limit
        assert pipeline.stt.await_count == 0 and sat._chat.calls == [] and len(sat._turn_starts) == 0
    assert sat._busy is False and sat._run is None
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

    h = SimpleNamespace(
        wake=AsyncMock(return_value={"ok": True}),
        running=AsyncMock(return_value=False),
    )
    monkeypatch.setattr(helios_power, "wake_helios", h.wake)
    monkeypatch.setattr(vs._settings, "helios_wake_enabled", False)
    # Default layout: brain, STT and TTS on one host (Helios), so any of them
    # answering proves Helios is on. Off-Helios TTS tests override tts_url
    # (JUPITER_TTS). Another box = the orchestrator's own host or a differing IP (_on_brain_host).
    monkeypatch.setattr(vs._settings, "model_url", "http://10.0.0.195:8080/v1")
    monkeypatch.setattr(vs._settings, "stt_url", "http://10.0.0.195:8003")
    monkeypatch.setattr(vs._settings, "tts_url", "http://10.0.0.195:8004")
    return h


TTS_ONLY = {"stt": False, "model": False, "tts": True}
# The live layout: the TTS on Jupiter's RTX 5080, reached through Docker's host-gateway alias.
JUPITER_TTS = "http://host.docker.internal:8004"


def _case(id, statuses, *, tts=None, wake=False, plug=False, timeout=None, wake_result=None, **expect):  # noqa: A002
    """One _ensure_helios case. ``expect``: result (required), helios (run label),
    wakes / plug_reads (call counts), errors (codes sent), sleeps (the 5 s waits),
    quiet (nothing sent to the device). A key left out isn't checked."""
    setup = SimpleNamespace(statuses=statuses, tts=tts, wake=wake, plug=plug, timeout=timeout, wake_result=wake_result)
    return pytest.param(setup, expect, id=id)


_GRACE = [5.0] * int(vs.SERVICE_GRACE_S / 5)

_HELIOS_CASES = [
    _case("all-up", (UP,), result=None, helios="awake", sleeps=[], quiet=True, wakes=0, plug_reads=0),
    # STT answered, so Helios is on: no plug read, no wake — wait for the brain.
    _case(
        "brain-reloading-then-ready",
        (MODEL_DOWN, MODEL_DOWN, UP),
        result=None,
        helios="woken",
        sleeps=[5.0, 5.0],
        errors=["jess-waking"],
        wakes=0,
        plug_reads=0,
    ),
    _case(
        "brain-never-ready",
        (MODEL_DOWN,),
        result="service_down",
        helios="unchecked",
        sleeps=_GRACE,
        errors=["jess-waking", "jess-error"],
        wakes=0,
    ),
    # Nothing answers yet but the plug says it's on (booting): no wake, just wait.
    _case(
        "booting-by-plug-reading",
        (DOWN, UP),
        wake=True,
        plug=True,
        result=None,
        helios="woken",
        errors=["jess-waking"],
        wakes=0,
    ),
    _case("asleep-wake-disabled", (DOWN,), result="helios_asleep", errors=["jess-asleep"], sleeps=[], wakes=0),
    _case(
        "woken",
        (DOWN, DOWN, DOWN, UP),
        wake=True,
        result=None,
        helios="woken",
        wakes=1,
        sleeps=[5.0, 5.0, 5.0],
        errors=["jess-waking"],
    ),
    _case(
        "wake-timeout",
        (DOWN,),
        wake=True,
        timeout=20,
        result="wake_timeout",
        sleeps=[5.0] * 4,
        errors=["jess-waking", "jess-error"],
    ),
    _case("negative-timeout-clamped", (DOWN,), wake=True, timeout=-5, result="wake_timeout", sleeps=[]),
    _case(
        "wake-call-fails",
        (DOWN,),
        wake=True,
        wake_result={"ok": False, "error": "HTTPError"},
        result="wake_failed",
        errors=["jess-error"],
        sleeps=[],
    ),
    # The TTS on Jupiter answers while Helios sleeps: that must not count as
    # Helios being on — wake it instead of failing as service_down.
    _case(
        "tts-elsewhere-asleep-wakes",
        (TTS_ONLY, UP),
        tts=JUPITER_TTS,
        wake=True,
        result=None,
        helios="woken",
        plug_reads=1,
        wakes=1,
        errors=["jess-waking"],
    ),
    _case(
        "tts-elsewhere-asleep-wake-off",
        (TTS_ONLY,),
        tts=JUPITER_TTS,
        result="helios_asleep",
        errors=["jess-asleep"],
        sleeps=[],
        plug_reads=1,
        wakes=0,
    ),
    # Helios asleep AND the Jupiter TTS down: a wake can't make the turn work
    # (and would leave Helios on), so fail as service_down at once. With wake
    # off too: "error", not "asleep" — waking Helios by hand wouldn't help.
    *(
        _case(
            f"tts-elsewhere-down-asleep-wake-{'on' if w else 'off'}",
            (DOWN,),
            tts=JUPITER_TTS,
            wake=w,
            result="service_down",
            wakes=0,
            errors=["jess-error"],
            sleeps=[],
        )
        for w in (True, False)
    ),
    # Plug says on (booting), Jupiter TTS down too: Helios is powered, so no
    # fail-fast — wait, and go once all answer.
    _case(
        "tts-elsewhere-down-booting",
        (DOWN, UP),
        tts=JUPITER_TTS,
        wake=True,
        plug=True,
        result=None,
        helios="woken",
        plug_reads=1,
        wakes=0,
        errors=["jess-waking"],
        sleeps=[5.0],
    ),
    # The most common case with the TTS off Helios: brain reloading, STT up.
    # STT alone proves Helios is on: grace, no wake, even with wake enabled.
    _case(
        "tts-elsewhere-brain-reloading",
        (MODEL_DOWN, UP),
        tts=JUPITER_TTS,
        wake=True,
        result=None,
        helios="woken",
        wakes=0,
        plug_reads=0,
        sleeps=[5.0],
    ),
    # Brain + STT up, Jupiter TTS down: Helios is on, so no wake — the full
    # grace wait, then service_down (not the asleep-and-TTS-down fail-fast).
    _case(
        "tts-elsewhere-down-helios-on",
        ({"stt": True, "model": True, "tts": False},),
        tts=JUPITER_TTS,
        wake=True,
        result="service_down",
        wakes=0,
        plug_reads=0,
        sleeps=_GRACE,
        errors=["jess-waking", "jess-error"],
    ),
    # Old layout (TTS on Helios): TTS answering still means Helios is on.
    _case("tts-on-helios-proves-power", (TTS_ONLY, UP), wake=True, result=None, wakes=0, plug_reads=0),
]


@pytest.mark.parametrize("setup, expect", _HELIOS_CASES)
async def test_ensure_helios(make_sat, helios, clock, monkeypatch, setup, expect):
    sat = make_sat() if setup.timeout is None else make_sat(helios_timeout_s=setup.timeout)
    if setup.tts:
        monkeypatch.setattr(vs._settings, "tts_url", setup.tts)
    monkeypatch.setattr(vs._settings, "helios_wake_enabled", setup.wake)
    helios.running.return_value = setup.plug
    if setup.wake_result is not None:
        helios.wake.return_value = setup.wake_result
    status = _statuses(*setup.statuses)
    monkeypatch.setattr(sat, "_service_status", status)
    monkeypatch.setattr(sat, "_helios_running", helios.running)
    run = vs._Run(wake_word="hey_jess")

    assert await sat._ensure_helios(run) == expect["result"]
    assert status.calls[0] == ("stt", "model", "tts")
    checks = {
        "helios": run.helios,
        "wakes": helios.wake.await_count,
        "plug_reads": helios.running.await_count,
        "errors": sat._client.errors,
        "sleeps": clock.sleeps,
    }
    assert {k: checks[k] for k in expect if k in checks} == {k: v for k, v in expect.items() if k in checks}
    if expect.get("quiet"):
        assert sat._client.events == []


async def test_helios_asleep_tts_box_down_turn_metric_and_log(sat, helios, clock, monkeypatch, pipeline, caplog):
    """The fail-fast path through a whole turn: counted as service_down (helios
    unchecked), no STT / brain / playback, one warning naming only the service."""
    monkeypatch.setattr(vs._settings, "tts_url", JUPITER_TTS)
    monkeypatch.setattr(vs._settings, "helios_wake_enabled", True)
    pipeline.status.return_value = dict(DOWN)
    monkeypatch.setattr(sat, "_helios_running", helios.running)
    caplog.set_level(logging.INFO, logger="orchestrator.voice_satellite")
    before = _turns("service_down")
    hist_before = _turn_seconds_count("unchecked")
    await _one_turn(sat)
    assert _turns("service_down") == before + 1
    assert _turn_seconds_count("unchecked") == hist_before  # only ok turns are timed
    assert sat._client.names[-1] == ERROR and sat._client.errors == ["jess-error"]
    assert pipeline.stt.await_count == 0 and sat._chat.calls == [] and pipeline.speak.await_count == 0
    assert helios.wake.await_count == 0 and sat._busy is False
    warn = [r for r in caplog.records if "not waking it" in r.getMessage()]
    assert len(warn) == 1 and warn[0].levelno == logging.WARNING
    assert warn[0].getMessage() == f"[SATELLITE] {NODE}: tts down off Helios; not waking it"


@pytest.mark.parametrize(
    "name, model_url, url, expected",
    [
        ("stt", "http://10.0.0.195:8080/v1", "http://10.0.0.195:8003", True),
        ("stt", "http://10.0.0.195:8080/v1", "http://10.0.0.58:8003", False),
        ("tts", "http://10.0.0.195:8080/v1", "http://10.0.0.58:8004", False),
        ("tts", "http://10.0.0.195:8080/v1", "http://10.0.0.195:8004", True),
        # Names can't prove another box: compose DNS (one machine), name vs IP.
        ("stt", "http://vllm-primary:8000/v1", "http://parakeet-stt:8003", True),
        ("tts", "http://helios.lan:8080/v1", "http://10.0.0.58:8004", True),
        ("tts", "http://[fd00::1]:8080/v1", "http://[fd00::2]:8004", False),
        # The orchestrator's own host (Jupiter) is never the power-tiered brain box.
        ("tts", "http://10.0.0.195:8080/v1", "http://host.docker.internal:8004", False),
        ("tts", "http://10.0.0.195:8080/v1", "http://HOST.docker.internal:8004", False),
        ("tts", "http://10.0.0.195:8080/v1", "http://localhost:8004", False),
        ("tts", "http://10.0.0.195:8080/v1", "http://127.0.0.1:8004", False),
        ("tts", "http://vllm-primary:8000/v1", "http://host.docker.internal:8004", False),
        ("stt", "http://localhost:8080/v1", "http://10.0.0.195:8003", False),
        # Single-box install: everything on the orchestrator's host.
        ("tts", "http://localhost:8080/v1", "http://host.docker.internal:8004", True),
        ("tts", "http://127.0.0.1:8080/v1", "http://[::1]:8004", True),
        # Empty URLs keep the old behaviour (counted, so no fail-fast).
        ("stt", "", "http://10.0.0.195:8003", True),
        ("stt", "http://10.0.0.195:8080/v1", "", True),
        ("tts", "", "http://host.docker.internal:8004", True),
    ],
    ids=[
        "stt-same-ip",
        "stt-other-ip",
        "tts-other-ip",
        "tts-helios",
        "compose-names",
        "name-vs-ip",
        "ipv6-differ",
        "tts-jupiter-host-gateway",
        "host-gateway-uppercase",
        "tts-localhost",
        "tts-loopback-ip",
        "compose-brain-vs-host-gateway",
        "brain-local-stt-remote",
        "single-box-names",
        "single-box-loopback-ips",
        "no-model-url",
        "no-stt-url",
        "no-model-url-host-gateway",
    ],
)
def test_on_brain_host(monkeypatch, name, model_url, url, expected):
    monkeypatch.setattr(vs._settings, "model_url", model_url)
    monkeypatch.setattr(vs._settings, f"{name}_url", url)
    assert vs.Satellite._on_brain_host(name) is expected
    assert vs.Satellite._on_brain_host("model") is True


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

    mock = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
    monkeypatch.setattr(helios_power, "helios_power_status", mock)
    assert await sat._helios_running() is expected


async def test_service_status_uses_shared_client_and_3s_probe(sat, monkeypatch):

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
# cloud_brain: satellite turns never schedule auto_learn
# ---------------------------------------------------------------------------


def test_schedule_auto_learn_skipped_for_satellite_turns(monkeypatch):

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

    assert voice_satellite_key_ok(key) is ok


# ---------------------------------------------------------------------------
# log_config: third-party payload loggers
# ---------------------------------------------------------------------------


def _record(name: str, msg: str = "Invalid protobuf message type=%s data=%s", args=(7, b"\x01mic-audio")):
    return logging.LogRecord(name, logging.ERROR, __file__, 1, msg, args, None)


@pytest.mark.parametrize("name", ["aioesphomeapi", "aioesphomeapi.connection", "zeroconf", "zeroconf._engine"])
def test_payload_args_dropped(name):

    rec = _record(name)
    assert DropPayloadArgsFilter().filter(rec) is True
    assert rec.args is None
    assert rec.getMessage() == "Invalid protobuf message type=%s data=%s [args dropped]"


@pytest.mark.parametrize("name", ["orchestrator.voice_satellite", "aioesphomeapix", "httpx", "root"])
def test_other_loggers_keep_their_args(name):

    rec = _record(name, "count=%d", (3,))
    assert DropPayloadArgsFilter().filter(rec) is True
    assert rec.getMessage() == "count=3"


def test_payload_record_without_args_is_untouched():

    rec = _record("aioesphomeapi.connection", "Connection lost", None)
    DropPayloadArgsFilter().filter(rec)
    assert rec.getMessage() == "Connection lost"


def test_configure_logging_drops_payload_args(monkeypatch):

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


async def test_satellite_loop_builds_from_settings(monkeypatch):
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
    assert s._chat is chat  # series seeding: test_satellite_loop_seeds_every_series_for_every_device


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
        # repeated or misplaced separators
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
def test_parse_devices_skips_a_bad_entry(entry, problem):

    devices, problems = parse_voice_satellite_devices(f"{NODE}@10.0.0.50={SPEAKER},{entry}")
    assert [d.name for d in devices] == [NODE]
    assert len(problems) == 1 and problems[0].startswith("entry 2 ") and problem in problems[0]
    assert "10.0.0.6" not in problems[0]  # named by position, never echoed


def test_parse_devices_same_ip_written_differently_is_a_repeat():

    devices, problems = parse_voice_satellite_devices("a@fd00::51=media_player.a,b@fd00:0:0::51=media_player.b")
    assert [d.name for d in devices] == ["a"] and "repeats an earlier device IP" in problems[0]


def test_parse_devices_limit():

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
        # skipped entries AND a shared-setting failure: both counted
        pytest.param(
            {"VOICE_SATELLITE_DEVICES": f"{TWO_DEVICES},junk", "HA_TOKEN": None}, 2, False, id="skip-and-shared"
        ),
        pytest.param(
            {"VOICE_SATELLITE_DEVICES": f"{TWO_DEVICES},junk", "VOICE_SATELLITE_API_KEY": None},
            2,
            False,
            id="skip-and-key",
        ),
        pytest.param({"VOICE_SATELLITE_API_KEY": None}, 1, False, id="single-no-key"),
        pytest.param({"VOICE_SATELLITE_PORT": "0"}, 1, False, id="single-port"),
        pytest.param({"VOICE_SATELLITE_SPEAKER": None}, 1, False, id="single-no-speaker"),
        # the env var can't set it, even with the feature off
        pytest.param(
            {"VOICE_SATELLITE_ENABLED": "false", "VOICE_SATELLITE_CONFIG_PROBLEMS": "5"},
            0,
            False,
            id="off-not-settable",
        ),
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


@pytest.mark.parametrize("refuse", ["busy", "rate_limited"])
async def test_refusing_device_keeps_the_wake_word(make_sat, monkeypatch, later, refuse):
    """A busy or rate-limited room says "busy" and keeps the wake word rather
    than the next room answering the question. Real clock: the fake one would
    run the 4 s no-audio watchdog inside _tick()."""
    arbiter = vs.WakeArbiter()
    first = make_sat(arbiter=arbiter)
    if refuse == "busy":
        first._busy = True  # mid-turn
    else:
        first._turn_starts.extend([time.monotonic()] * vs.RATE_LIMIT_TURNS)
    other_room = make_sat(node_name=OTHER, speaker=OTHER_SPEAKER, arbiter=arbiter)
    p_other = _wire(other_room, monkeypatch)
    before = (_turns(refuse, device=NODE), _turns("duplicate", device=OTHER))

    await first.handle_start("c1", 0, None, "Hey Jess")
    await other_room.handle_start("c2", 0, None, "Hey Jess")
    await _tick()
    assert first._client.errors == ["jess-busy"] and first._run is None
    assert arbiter.holder == NODE
    assert other_room._run is not None and other_room._run.duplicate is True
    await _say(other_room.handle_audio, UTTERANCE)
    await _drain(other_room)
    assert other_room._client.names == [RUN_START, STT_START, RUN_END] and other_room._client.errors == []
    assert p_other.speak.await_count == 0
    assert (_turns(refuse, device=NODE), _turns("duplicate", device=OTHER)) == (before[0] + 1, before[1] + 1)


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


def _bytes_for(ms: int) -> int:
    return vs.SAMPLE_RATE * vs.SAMPLE_WIDTH * ms // 1000


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


def test_known_speakers_leave_out_a_skipped_entrys_speaker(monkeypatch):
    """Only valid entries become speaker labels: a typo'd entry's text never does."""

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


# ---------------------------------------------------------------------------
# Voice match (VOICE_MATCH_DEVICES): the bridge side. voice_match.py itself is
# tested in test_voice_match.py; here the matcher is a stub.
# ---------------------------------------------------------------------------


class FakeMatcher:
    """Stands in for voice_match.VoiceMatcher: a fixed score, or an exception."""

    def __init__(self, score: float = 0.6, exc: Exception | None = None, delay: float = 0.0) -> None:
        self.value = score
        self.exc = exc
        self.delay = delay
        self.scored: list[int] = []

    async def score_async(self, pcm: bytes, timeout: float) -> float:
        self.scored.append(len(pcm))
        if self.delay:
            await asyncio.wait_for(asyncio.sleep(self.delay), timeout)
        if self.exc is not None:
            raise self.exc
        return self.value


def _voice_match(result: str, device: str = NODE) -> float:
    return metric_value(metrics.SATELLITE_VOICE_MATCH_TOTAL, result=result, device=device)


def _score_count(device: str = NODE) -> float:
    return metric_value(metrics.SATELLITE_VOICE_MATCH_SCORE, "_count", device=device)


def _ready(device: str = NODE) -> float:
    return metric_value(metrics.SATELLITE_VOICE_MATCH_READY, device=device)


def _ready_series(device: str) -> list:
    return [
        s for f in metrics.SATELLITE_VOICE_MATCH_READY.collect() for s in f.samples if s.labels.get("device") == device
    ]


async def _one_turn(sat) -> None:
    await sat.handle_start("c", 0, None, "Hey Jess")
    await _say(sat.handle_audio, UTTERANCE)
    await _drain(sat)


def _vm_sat(make_sat, monkeypatch, matcher, **kw):
    sat = make_sat(voice_match=matcher, voice_match_threshold=0.35, **kw)
    return sat, _wire(sat, monkeypatch)


async def test_voice_match_recognized_voice_gets_the_normal_turn(make_sat, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="orchestrator.voice_satellite")
    matcher = FakeMatcher(0.62)
    sat, p = _vm_sat(make_sat, monkeypatch, matcher)
    before = (_turns("ok", helios="awake"), _voice_match("recognized"), _score_count())

    await _one_turn(sat)

    assert matcher.scored and p.stt.await_count == 1 and p.speak.await_count == 1
    assert (_turns("ok", helios="awake"), _voice_match("recognized"), _score_count()) == tuple(b + 1 for b in before)
    assert _ready() == 1
    assert "voice match recognized score=0.62" in caplog.text
    assert "voice_match=" in caplog.text  # stage timing on the turn line


async def test_voice_match_unknown_voice_ends_silently(make_sat, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="orchestrator.voice_satellite")
    sat, p = _vm_sat(make_sat, monkeypatch, FakeMatcher(0.05))
    before = (_turns("not_recognized"), _voice_match("not_recognized"))

    await _one_turn(sat)

    # Before Helios, STT and the brain; no sound: the device is already back on the wake word.
    assert p.status.await_count == 0 and p.stt.await_count == 0 and p.speak.await_count == 0
    assert sat._chat.calls == []
    assert sat._client.names == [RUN_START, STT_START, VAD_START, RUN_END]
    assert (_turns("not_recognized"), _voice_match("not_recognized")) == (before[0] + 1, before[1] + 1)
    assert sat._busy is False and sat._history == []
    assert "voice match not recognized score=0.05" in caplog.text


async def test_voice_match_threshold_is_inclusive(make_sat, monkeypatch):
    sat, p = _vm_sat(make_sat, monkeypatch, FakeMatcher(0.35))
    await _one_turn(sat)
    assert p.speak.await_count == 1


async def test_voice_match_contested_wake_is_counted_apart(make_sat, monkeypatch, caplog):
    """Two rooms heard one "Hey Jess"; the voice-match room claimed it and
    didn't recognize the voice (maybe the owner, farther away): turned away,
    but counted as contested so the lock-out case is visible — even when a
    newer wake (the owner trying again) started a new claim meanwhile."""
    caplog.set_level(logging.INFO, logger="orchestrator.voice_satellite")
    arbiter = vs.WakeArbiter()
    sat, p = _vm_sat(make_sat, monkeypatch, FakeMatcher(0.2), arbiter=arbiter)
    before = (_voice_match("contested"), _turns("not_recognized"))
    await sat.handle_start("c", 0, None, "Hey Jess")
    assert arbiter.claim(OTHER, "hey_jess") is False  # the other room heard it too
    arbiter._claimed_at -= 10  # later: the owner says it again in the other room — a new claim
    assert arbiter.claim(OTHER, "hey_jess") is True
    await _say(sat.handle_audio, UTTERANCE)
    await _drain(sat)
    assert (_voice_match("contested"), _turns("not_recognized")) == (before[0] + 1, before[1] + 1)
    assert p.speak.await_count == 0
    assert "voice match contested score=0.20" in caplog.text


async def test_voice_match_uncontested_unknown_voice_is_plain_not_recognized(make_sat, monkeypatch):
    sat, _ = _vm_sat(make_sat, monkeypatch, FakeMatcher(0.2), arbiter=vs.WakeArbiter())
    before = (_voice_match("contested"), _voice_match("not_recognized"))
    await _one_turn(sat)
    assert (_voice_match("contested"), _voice_match("not_recognized")) == (before[0], before[1] + 1)


def test_arbiter_contested_flag_belongs_to_its_claim():
    arbiter = vs.WakeArbiter(window_s=60)
    assert arbiter.claim(NODE, "hey_jess")
    first = arbiter.last_claim
    assert not arbiter.was_contested(first)
    assert not arbiter.claim(OTHER, "hey_jess")
    assert arbiter.was_contested(first)
    assert arbiter.claim(NODE, "hey_jess") and arbiter.last_claim == first  # the holder again: same claim
    arbiter._claimed_at -= 120  # the window passed: a new claim starts clean
    assert arbiter.claim(OTHER, "hey_jess")
    assert arbiter.last_claim != first and not arbiter.was_contested(arbiter.last_claim)
    assert arbiter.was_contested(first)  # the old claim keeps its flag
    assert arbiter.claim(NODE, "button") and arbiter.last_claim is None
    assert not arbiter.was_contested(None)


async def test_voice_match_too_short_is_turned_away_and_keeps_ready(make_sat, monkeypatch):
    from orchestrator.voice_match import VoiceMatchUnavailable

    sat, p = _vm_sat(make_sat, monkeypatch, FakeMatcher(exc=VoiceMatchUnavailable("too_short")))
    metrics.SATELLITE_VOICE_MATCH_READY.labels(device=NODE).set(1)
    before = _voice_match("too_short")
    await _one_turn(sat)
    assert _voice_match("too_short") == before + 1 and p.speak.await_count == 0
    assert _ready() == 1


async def test_voice_match_turned_away_turns_do_not_use_the_rate_limit(make_sat, monkeypatch):
    """A TV waking the mic over and over must not lock the owner out."""
    matcher = FakeMatcher(0.05)
    sat, p = _vm_sat(make_sat, monkeypatch, matcher)
    for _ in range(vs.RATE_LIMIT_TURNS + 2):
        await _one_turn(sat)
    assert len(sat._turn_starts) == 0
    matcher.value = 0.7
    before = _turns("ok", helios="awake")
    await _one_turn(sat)
    assert _turns("ok", helios="awake") == before + 1 and p.speak.await_count == 1


async def test_voice_match_scores_the_speech_not_the_silence(make_sat, monkeypatch):
    matcher = FakeMatcher(0.6)
    sat, p = _vm_sat(make_sat, monkeypatch, matcher)
    await _one_turn(sat)
    (captured,) = [len(call.args[0]) for call in p.stt.await_args_list]
    assert 0 < matcher.scored[0] < captured  # trimmed to the VAD's speech span


@pytest.mark.parametrize(
    ("reason", "result", "ready"),
    [
        pytest.param("no_voiceprints", "unavailable", 0, id="no-voiceprints"),
        pytest.param("model_output", "unavailable", 0, id="model-output"),
        pytest.param("busy", "failed", 1, id="busy-pool"),
    ],
)
async def test_voice_match_unavailable_fails_open(make_sat, monkeypatch, caplog, reason, result, ready):
    from orchestrator.voice_match import VoiceMatchUnavailable

    caplog.set_level(logging.INFO, logger="orchestrator.voice_satellite")
    sat, p = _vm_sat(make_sat, monkeypatch, FakeMatcher(exc=VoiceMatchUnavailable(reason)))
    metrics.SATELLITE_VOICE_MATCH_READY.labels(device=NODE).set(1)
    before = (_turns("ok", helios="awake"), _voice_match(result))

    await _one_turn(sat)

    assert p.speak.await_count == 1  # the turn went ahead unchecked
    assert (_turns("ok", helios="awake"), _voice_match(result)) == (before[0] + 1, before[1] + 1)
    assert _ready() == ready  # a busy pool says nothing about readiness
    assert f"voice match {result} ({reason}); turn goes ahead unchecked" in caplog.text


async def test_voice_match_error_fails_open_without_the_message(make_sat, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="orchestrator.voice_satellite")
    sat, p = _vm_sat(make_sat, monkeypatch, FakeMatcher(exc=RuntimeError("secret detail")))
    before = _voice_match("failed")
    await _one_turn(sat)
    assert p.speak.await_count == 1 and _voice_match("failed") == before + 1
    assert "voice match failed (RuntimeError)" in caplog.text and "secret detail" not in caplog.text


async def test_voice_match_timeout_fails_open(make_sat, monkeypatch):
    monkeypatch.setattr(vs, "VOICE_MATCH_TIMEOUT_S", 0.01)
    sat, p = _vm_sat(make_sat, monkeypatch, FakeMatcher(0.05, delay=0.5))
    before = _voice_match("failed")
    await _one_turn(sat)
    assert p.speak.await_count == 1 and _voice_match("failed") == before + 1


async def test_voice_match_fault_warns_once_per_reason(make_sat, monkeypatch, caplog):
    """A broken filter in a TV room is many turns an hour: one WARNING per
    change of reason, INFO after (the counter carries the rate)."""
    from orchestrator.voice_match import VoiceMatchUnavailable

    caplog.set_level(logging.INFO, logger="orchestrator.voice_satellite")
    matcher = FakeMatcher(exc=VoiceMatchUnavailable("no_voiceprints"))
    sat, _ = _vm_sat(make_sat, monkeypatch, matcher)
    for _ in range(3):
        await _one_turn(sat)
    levels = [r.levelname for r in caplog.records if "voice match unavailable" in r.getMessage()]
    assert levels == ["WARNING", "INFO", "INFO"]
    matcher.exc = None  # fixed: the next fault warns again
    await _one_turn(sat)
    matcher.exc = VoiceMatchUnavailable("no_voiceprints")
    await _one_turn(sat)
    levels = [r.levelname for r in caplog.records if "voice match unavailable" in r.getMessage()]
    assert levels[-1] == "WARNING"


async def test_voice_match_retry_counts_a_likely_false_reject(make_sat, monkeypatch):
    matcher = FakeMatcher(0.1)
    sat, _ = _vm_sat(make_sat, monkeypatch, matcher)
    retry = metrics.SATELLITE_VOICE_MATCH_RETRY_TOTAL
    before = metric_value(retry, device=NODE)
    await _one_turn(sat)  # turned away
    matcher.value = 0.7
    await _one_turn(sat)  # recognized soon after
    assert metric_value(retry, device=NODE) == before + 1
    await _one_turn(sat)  # recognized again: no turned-away question before it
    assert metric_value(retry, device=NODE) == before + 1
    monkeypatch.setattr(vs, "RETRY_WINDOW_S", -1.0)  # outside the window
    matcher.value = 0.1
    await _one_turn(sat)
    matcher.value = 0.7
    await _one_turn(sat)
    assert metric_value(retry, device=NODE) == before + 1


async def test_device_without_voice_match_is_not_checked(sat, pipeline):
    before = sum(_voice_match(r) for r in vs.VOICE_MATCH_RESULTS)
    await _one_turn(sat)
    assert sum(_voice_match(r) for r in vs.VOICE_MATCH_RESULTS) == before
    assert pipeline.speak.await_count == 1


NOVM = "atom-echo-novm"


async def test_voice_match_off_exports_no_ready_series(monkeypatch):
    """An unlabelled gauge would export 0 with the feature off and fire
    VoiceMatchFailing on every install."""
    assert metrics.SATELLITE_VOICE_MATCH_READY._labelnames == ("device",)
    monkeypatch.setattr(vs._settings, "voice_satellite_enabled", True)
    monkeypatch.setattr(vs._settings, "voice_satellite_devices", f"{NOVM}@10.0.0.52={SPEAKER}")
    monkeypatch.setattr(vs._settings, "voice_satellite_api_key", _KEY)
    monkeypatch.setattr(vs._settings, "voice_match_devices", "")

    async def fake_run_forever(self):
        return None

    monkeypatch.setattr(vs.Satellite, "run_forever", fake_run_forever)
    await vs.satellite_loop(FakeChat())  # returns: no upkeep task when off
    assert _ready_series(NOVM) == []


async def _loop_with_voice_match(monkeypatch, devices_on: str, preload_result):
    from orchestrator import voice_match

    monkeypatch.setattr(vs._settings, "voice_satellite_enabled", True)
    monkeypatch.setattr(vs._settings, "voice_satellite_devices", TWO_DEVICES)
    monkeypatch.setattr(vs._settings, "voice_satellite_api_key", _KEY)
    monkeypatch.setattr(vs._settings, "voice_match_devices", devices_on)
    calls = []

    def fake_preload(self):
        calls.append(1)
        return preload_result

    monkeypatch.setattr(voice_match.VoiceMatcher, "preload", fake_preload)
    started = []

    async def fake_run_forever(self):
        started.append(self)
        await asyncio.Event().wait()

    monkeypatch.setattr(vs.Satellite, "run_forever", fake_run_forever)
    task = asyncio.create_task(vs.satellite_loop(FakeChat()))
    for _ in range(200):
        if calls and len(started) == 2:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)  # let the upkeep set the gauges
    return task, started, calls


async def _stop(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_satellite_loop_voice_match_only_on_listed_devices(monkeypatch, caplog):
    from orchestrator import voice_match

    caplog.set_level(logging.INFO, logger="orchestrator.voice_satellite")
    task, started, calls = await _loop_with_voice_match(monkeypatch, OTHER, None)
    try:
        by_node = {s._node: s for s in started}
        assert by_node[NODE]._voice_match is None
        assert isinstance(by_node[OTHER]._voice_match, voice_match.VoiceMatcher)
        assert by_node[OTHER]._voice_match_threshold == vs._settings.voice_match_threshold
        assert _ready(OTHER) == 1
        assert f"Voice match on for {OTHER}" in caplog.text
    finally:
        await _stop(task)


async def test_satellite_loop_voice_match_not_ready_is_logged_not_fatal(monkeypatch, caplog):
    task, started, calls = await _loop_with_voice_match(monkeypatch, f"{NODE},{OTHER}", "model_missing")
    try:
        assert len(started) == 2 and all(s._voice_match is not None for s in started)  # fail open per turn
        assert _ready(NODE) == 0 and _ready(OTHER) == 0
        assert "Voice match not ready (model_missing)" in caplog.text
        assert not task.done()  # the bridge keeps running
    finally:
        await _stop(task)


async def test_voice_match_upkeep_rechecks_and_logs_on_change(monkeypatch, caplog):
    """The ready gauge follows `fetch` / `enroll` without anyone speaking."""
    caplog.set_level(logging.INFO, logger="orchestrator.voice_satellite")
    monkeypatch.setattr(vs, "VOICE_MATCH_RECHECK_S", 0.0)
    results = ["model_missing", "model_missing", None, None]

    class Matcher:
        async def preload_async(self, timeout: float):
            if not results:
                await asyncio.Event().wait()
            return results.pop(0)

    task = asyncio.create_task(vs._voice_match_upkeep(Matcher(), frozenset({NODE})))
    await _until(lambda: not results)
    await _tick(20)
    assert _ready(NODE) == 1
    assert caplog.text.count("Voice match not ready (model_missing)") == 1
    assert caplog.text.count(f"Voice match on for {NODE}") == 1
    await _stop(task)


async def test_voice_match_upkeep_survives_an_error(monkeypatch):
    monkeypatch.setattr(vs, "VOICE_MATCH_RECHECK_S", 0.0)
    results: list = [RuntimeError("x"), None]

    class Matcher:
        async def preload_async(self, timeout: float):
            if not results:
                await asyncio.Event().wait()
            r = results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

    task = asyncio.create_task(vs._voice_match_upkeep(Matcher(), frozenset({NODE})))
    await _until(lambda: not results)
    await _tick(20)
    assert _ready(NODE) == 1 and not task.done()
    await _stop(task)


async def test_voice_match_upkeep_busy_pool_is_not_not_ready(monkeypatch, caplog, tmp_path):
    """A recheck that finds the pool full of questions says nothing about
    readiness: no ERROR line, the gauge stays as it was."""
    from orchestrator import voice_match

    caplog.set_level(logging.INFO, logger="orchestrator.voice_satellite")
    real = voice_match.VoiceMatcher(str(tmp_path / "m.onnx"), str(tmp_path / "voice_match" / "p.json"))
    real._in_flight = voice_match.MAX_IN_FLIGHT
    metrics.SATELLITE_VOICE_MATCH_READY.labels(device=NODE).set(1)
    task = asyncio.create_task(vs._voice_match_upkeep(real, frozenset({NODE})))
    await _tick(20)
    assert _ready(NODE) == 1
    assert "not ready" not in caplog.text
    await _stop(task)


def test_config_voice_match_devices_must_be_configured_devices(caplog):
    s = _settings(
        VOICE_SATELLITE_HOST=None,
        VOICE_SATELLITE_SPEAKER=None,
        VOICE_SATELLITE_DEVICES=TWO_DEVICES,
        VOICE_MATCH_DEVICES=f"{OTHER},atom-echo-typo",
    )
    assert s.voice_satellite_enabled
    assert s.voice_match_device_set() == frozenset({OTHER})
    assert s.voice_satellite_config_problems == 1
    assert "VOICE_MATCH_DEVICES entry 2 is not a configured device" in caplog.text
    assert "atom-echo-typo" not in caplog.text


@pytest.mark.parametrize("value", ["0", "1", "1.5", "-0.2", "0,3", "nan", "abc"])
def test_config_voice_match_threshold_bad_values_fall_back(value):
    """A bad value must never stop the orchestrator starting (optional feature);
    it counts as a config problem only when voice match is asked for."""
    on = _settings(VOICE_MATCH_THRESHOLD=value, VOICE_MATCH_DEVICES="atom-echo-jess")
    assert on.voice_match_threshold == 0.35 and on.voice_satellite_config_problems == 1
    assert on.voice_satellite_enabled  # the satellite itself keeps working
    off = _settings(VOICE_MATCH_THRESHOLD=value)
    assert off.voice_match_threshold == 0.35 and off.voice_satellite_config_problems == 0
    bridge_off = _settings(VOICE_MATCH_THRESHOLD=value, VOICE_SATELLITE_ENABLED="false")
    assert bridge_off.voice_match_threshold == 0.35  # the CLI uses it with the bridge off


def test_config_voice_match_threshold_empty_is_the_default():
    s = _settings(VOICE_MATCH_THRESHOLD="", VOICE_MATCH_DEVICES="atom-echo-jess")
    assert s.voice_match_threshold == 0.35 and s.voice_satellite_config_problems == 0


def test_config_problems_add_up():
    s = _settings(
        VOICE_SATELLITE_HOST=None,
        VOICE_SATELLITE_SPEAKER=None,
        VOICE_SATELLITE_DEVICES=f"{TWO_DEVICES},bad entry",
        VOICE_MATCH_DEVICES=OTHER,
        VOICE_MATCH_THRESHOLD="7",
    )
    assert s.voice_satellite_config_problems == 2  # the skipped device + the threshold


def test_config_voice_match_threshold_default_and_valid():
    assert _settings().voice_match_threshold == 0.35
    assert _settings(VOICE_MATCH_THRESHOLD="0.4").voice_match_threshold == 0.4


def test_config_voice_match_voiceprints_must_sit_in_the_backup_excluded_dir(caplog):
    s = _settings(VOICE_MATCH_DEVICES="atom-echo-jess", VOICE_MATCH_VOICEPRINTS_PATH="/app/data/prints.json")
    assert s.voice_match_device_set() == frozenset()
    assert s.voice_satellite_config_problems == 1
    assert "must be in a directory named voice_match" in caplog.text
    ok = _settings(VOICE_MATCH_DEVICES="atom-echo-jess", VOICE_MATCH_VOICEPRINTS_PATH="/srv/voice_match/p.json")
    assert ok.voice_match_device_set() == frozenset({"atom-echo-jess"})


def test_config_voice_match_off_by_default_and_off_with_the_bridge():
    assert _settings().voice_match_device_set() == frozenset()
    off = _settings(VOICE_SATELLITE_ENABLED="false", VOICE_MATCH_DEVICES="atom-echo-jess")
    assert off.voice_match_device_set() == frozenset()
    single = _settings(VOICE_MATCH_DEVICES="atom-echo-jess")  # the single-device default name
    assert single.voice_match_device_set() == frozenset({"atom-echo-jess"})


# -- voice match: wake arbitration, the shared matcher, what a turned-away turn skips ----------

THIRD = "atom-echo-third"
THIRD_SPEAKER = "media_player.hall_test"


async def test_voice_match_button_press_is_never_contested(make_sat, monkeypatch):
    """A button press is deliberate, never one wake word heard twice: even
    right after this room's contested "Hey Jess", an unknown voice on the
    button is plain not_recognized."""
    arbiter = vs.WakeArbiter()
    sat, p = _vm_sat(make_sat, monkeypatch, FakeMatcher(0.2), arbiter=arbiter)
    assert arbiter.claim(NODE, "hey_jess") and not arbiter.claim(OTHER, "hey_jess")
    assert arbiter.was_contested(arbiter.last_claim)
    before = (_voice_match("contested"), _voice_match("not_recognized"), _turns("not_recognized", wake="button"))

    await sat.handle_start("c", 0, None, None)  # the button
    await _say(sat.handle_audio, UTTERANCE)
    await _drain(sat)

    after = (_voice_match("contested"), _voice_match("not_recognized"), _turns("not_recognized", wake="button"))
    assert after == (before[0], before[1] + 1, before[2] + 1)
    assert p.speak.await_count == 0


async def test_voice_match_a_button_press_elsewhere_does_not_contest_this_wake(make_sat, monkeypatch):
    arbiter = vs.WakeArbiter()
    sat, _ = _vm_sat(make_sat, monkeypatch, FakeMatcher(0.2), arbiter=arbiter)
    before = (_voice_match("contested"), _voice_match("not_recognized"))
    await sat.handle_start("c", 0, None, "Hey Jess")
    assert arbiter.claim(OTHER, "button")  # someone presses the other room's button meanwhile
    await _say(sat.handle_audio, UTTERANCE)
    await _drain(sat)
    assert (_voice_match("contested"), _voice_match("not_recognized")) == (before[0], before[1] + 1)


async def test_voice_match_duplicate_run_is_never_scored(make_sat, monkeypatch):
    """The room that lost the wake word ends silently at its first audio: no
    score and no voice-match count, even with voice match on there too."""
    arbiter = vs.WakeArbiter()
    matcher = FakeMatcher(0.7)
    first, p1 = _vm_sat(make_sat, monkeypatch, matcher, arbiter=arbiter)
    second, p2 = _vm_sat(
        make_sat, monkeypatch, matcher, arbiter=arbiter, node_name=OTHER, speaker=OTHER_SPEAKER, host="10.0.0.51"
    )
    before = (sum(_voice_match(r, OTHER) for r in vs.VOICE_MATCH_RESULTS), _turns("duplicate", device=OTHER))

    await first.handle_start("c", 0, None, "Hey Jess")
    await second.handle_start("c", 0, None, "Hey Jess")
    await _say(first.handle_audio, UTTERANCE)
    await _say(second.handle_audio, UTTERANCE)
    await asyncio.gather(_drain(first), _drain(second))

    assert len(matcher.scored) == 1 and p1.speak.await_count == 1
    assert p2.status.await_count == 0 and p2.speak.await_count == 0
    assert sum(_voice_match(r, OTHER) for r in vs.VOICE_MATCH_RESULTS) == before[0]
    assert _turns("duplicate", device=OTHER) == before[1] + 1


async def test_voice_match_rooms_score_at_once_on_the_shared_matcher(make_sat, monkeypatch, tmp_path, caplog):
    """Two rooms' questions run side by side on the real matcher's two threads
    and both get a verdict; a third at the same moment finds the pool full and
    goes ahead unchecked at once (fail open) — it never queues behind them."""
    import threading

    import numpy as np

    from orchestrator import voice_match

    caplog.set_level(logging.INFO, logger="orchestrator.voice_satellite")
    owner = np.full(voice_match.EMBED_DIM, 1 / 16, dtype=np.float32)  # a unit vector
    prints = tmp_path / "voice_match" / "voiceprints.json"
    voice_match.save_voiceprints(str(prints), {"owner": owner})
    matcher = voice_match.VoiceMatcher(str(tmp_path / "voice_match" / "missing.onnx"), str(prints))
    both = threading.Barrier(2, timeout=5)

    def embed(samples):
        both.wait()  # returns only while the other room's question is being scored too
        return owner

    monkeypatch.setattr(matcher, "embed", embed)
    rooms = [
        _vm_sat(make_sat, monkeypatch, matcher),
        _vm_sat(make_sat, monkeypatch, matcher, node_name=OTHER, speaker=OTHER_SPEAKER, host="10.0.0.51"),
        _vm_sat(make_sat, monkeypatch, matcher, node_name=THIRD, speaker=THIRD_SPEAKER, host="10.0.0.52"),
    ]
    names = (NODE, OTHER, THIRD)
    before = [(_voice_match("recognized", n), _voice_match("failed", n)) for n in names]

    for sat, _ in rooms:  # no arbiter: three separate questions at once
        await sat.handle_start("c", 0, None, "Hey Jess")
    for sat, _ in rooms:
        await _say(sat.handle_audio, UTTERANCE)
    await asyncio.gather(*(_drain(sat) for sat, _ in rooms))

    after = [(_voice_match("recognized", n), _voice_match("failed", n)) for n in names]
    assert after == [
        (before[0][0] + 1, before[0][1]),
        (before[1][0] + 1, before[1][1]),
        (before[2][0], before[2][1] + 1),
    ]
    assert [p.speak.await_count for _, p in rooms] == [1, 1, 1]  # every room was answered
    assert f"{THIRD}: voice match failed (busy)" in caplog.text


async def test_voice_match_turned_away_question_does_not_hold_reminders_off(make_sat, monkeypatch):
    """Reminders wait while someone talks to Jess (D6); a TV that false-woke
    the mic is not someone talking to Jess."""
    matcher = FakeMatcher(0.05)
    sat, _ = _vm_sat(make_sat, monkeypatch, matcher)
    await _one_turn(sat)
    assert not shared.is_voice_session_active()
    matcher.value = 0.7
    await _one_turn(sat)
    assert shared.is_voice_session_active()


class _ContextMatcher(FakeMatcher):
    """Records the auto_learn flag the scoring runs under."""

    def __init__(self, score: float) -> None:
        super().__init__(score)
        self.skip_seen: list[bool] = []

    async def score_async(self, pcm: bytes, timeout: float) -> float:
        self.skip_seen.append(skip_auto_learn_var.get())
        return await super().score_async(pcm, timeout)


@pytest.mark.parametrize("score", [pytest.param(0.7, id="recognized"), pytest.param(0.05, id="turned-away")])
async def test_voice_match_turn_still_never_feeds_auto_learn(make_sat, monkeypatch, score):
    matcher = _ContextMatcher(score)
    sat, _ = _vm_sat(make_sat, monkeypatch, matcher)
    await _one_turn(sat)
    assert matcher.skip_seen == [True]  # set before voice match runs
    assert [skip for skip, _ in sat._chat.ctx] == ([True] if score >= 0.35 else [])
    assert skip_auto_learn_var.get() is False  # set in the turn's own task, not leaked out


async def test_voice_match_verdict_never_reaches_the_chat_call(make_sat, monkeypatch):
    """What a voice turn may do must not depend on who is speaking: a
    recognized turn sends the brain exactly what an unchecked room sends."""
    plain = make_sat()
    _wire(plain, monkeypatch)
    matched, _ = _vm_sat(make_sat, monkeypatch, FakeMatcher(0.83))
    await _one_turn(plain)
    await _one_turn(matched)
    assert matched._chat.calls == plain._chat.calls == [[{"role": "user", "content": "what time is it"}]]
    assert "0.83" not in repr(matched._chat.calls)


async def test_voice_match_logs_carry_no_voiceprint_id_or_transcript(make_sat, monkeypatch, tmp_path, caplog):
    """Runtime twin of the static log guard, through the real matcher and its
    voiceprint file: recognized, not recognized, an invalid file and a deleted
    one log scores, lengths and reasons — never the id, transcript or reply."""
    import json

    import numpy as np

    from orchestrator import voice_match

    caplog.set_level(logging.DEBUG)
    eye = np.eye(voice_match.EMBED_DIM, dtype=np.float32)
    prints = tmp_path / "voice_match" / "voiceprints.json"
    voice_match.save_voiceprints(str(prints), {"zqx-owner": eye[0], "zqx-guest": eye[1]})
    matcher = voice_match.VoiceMatcher(str(tmp_path / "voice_match" / "missing.onnx"), str(prints))
    probe = {"emb": eye[0]}
    monkeypatch.setattr(matcher, "embed", lambda samples: probe["emb"])
    sat = make_sat(chat=FakeChat("zqx private reply"), voice_match=matcher, voice_match_threshold=0.35)
    p = _wire(sat, monkeypatch, transcript="zqx secret question words")

    await _one_turn(sat)  # recognized
    probe["emb"] = eye[2]
    await _one_turn(sat)  # not recognized
    bad = {"version": 1, "model_sha256": voice_match.MODEL_SHA256, "voiceprints": {"ZQX Owner": [0.1] * 256}}
    prints.write_text(json.dumps(bad))
    await _one_turn(sat)  # unavailable: voiceprints_invalid (fails open)
    prints.unlink()
    await _one_turn(sat)  # unavailable: no_voiceprints (fails open)

    assert p.speak.await_count == 3
    assert "voice match recognized score=1.00" in caplog.text and "voice match not recognized" in caplog.text
    assert "(voiceprints_invalid)" in caplog.text and "(no_voiceprints)" in caplog.text
    assert "zqx" not in caplog.text.lower()


SEED_A, SEED_B, SEED_OFF = "atom-echo-seed-a", "atom-echo-seed-b", "atom-echo-seed-off"


def _device_series(metric, device: str) -> list:
    return [s for f in metric.collect() for s in f.samples if s.labels.get("device") == device]


class _Rounds:
    """vs.asyncio stand-in: the upkeep's between-rounds sleep parks until the
    test lets the next round run. Tests cancel the upkeep only while it is
    parked: a cancel that lands as a preload finishes is the bug in
    test_voice_match_upkeep_stops_when_cancelled_as_a_preload_finishes."""

    def __init__(self) -> None:
        self.rounds = 0  # finished upkeep rounds
        self._go = asyncio.Event()

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, delay, *args):
        self.rounds += 1
        self._go.clear()
        await self._go.wait()

    def next(self) -> None:
        self._go.set()


async def _settle(cond, what: str = "condition") -> None:
    """Poll with real sleeps: the matcher works on its own threads."""
    for _ in range(300):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{what} never became true")


async def test_satellite_loop_seeds_voice_match_series_and_shares_one_matcher(monkeypatch):
    """Every voice-match room has its series from startup (the first increase
    after a restart is visible) and all share one matcher (one pool, one
    model in RAM); a room without voice match exports none of them."""
    from orchestrator import voice_match

    devices = f"{SEED_A}@10.0.0.60={SPEAKER},{SEED_B}@10.0.0.61={OTHER_SPEAKER},{SEED_OFF}@10.0.0.62={THIRD_SPEAKER}"
    monkeypatch.setattr(vs._settings, "voice_satellite_enabled", True)
    monkeypatch.setattr(vs._settings, "voice_satellite_devices", devices)
    monkeypatch.setattr(vs._settings, "voice_satellite_api_key", _KEY)
    monkeypatch.setattr(vs._settings, "voice_match_devices", f"{SEED_A},{SEED_B}")
    monkeypatch.setattr(voice_match.VoiceMatcher, "preload", lambda self: "model_missing")
    rounds = _Rounds()
    monkeypatch.setattr(vs, "asyncio", rounds)
    started = []

    async def fake_run_forever(self):
        started.append(self)
        await asyncio.Event().wait()

    monkeypatch.setattr(vs.Satellite, "run_forever", fake_run_forever)
    task = asyncio.create_task(vs.satellite_loop(FakeChat()))
    try:
        await _settle(lambda: len(started) == 3 and rounds.rounds == 1, "devices up and one upkeep round")
        by_node = {s._node: s for s in started}
        assert by_node[SEED_A]._voice_match is not None
        assert by_node[SEED_A]._voice_match is by_node[SEED_B]._voice_match
        assert by_node[SEED_OFF]._voice_match is None
        for name in (SEED_A, SEED_B):
            totals = _device_series(metrics.SATELLITE_VOICE_MATCH_TOTAL, name)
            assert {s.labels["result"] for s in totals if s.name.endswith("_total")} == set(vs.VOICE_MATCH_RESULTS)
            assert _device_series(metrics.SATELLITE_VOICE_MATCH_RETRY_TOTAL, name)
            assert _ready_series(name)
            turns = _device_series(metrics.SATELLITE_TURN_TOTAL, name)
            assert any(s.labels["result"] == "not_recognized" for s in turns)
        for metric in (
            metrics.SATELLITE_VOICE_MATCH_TOTAL,
            metrics.SATELLITE_VOICE_MATCH_RETRY_TOTAL,
            metrics.SATELLITE_VOICE_MATCH_READY,
            metrics.SATELLITE_VOICE_MATCH_SCORE,
        ):
            assert _device_series(metric, SEED_OFF) == []
        turns_off = _device_series(metrics.SATELLITE_TURN_TOTAL, SEED_OFF)
        assert not any(s.labels["result"] == "not_recognized" for s in turns_off)
    finally:
        await _stop(task)


async def test_voice_match_upkeep_follows_a_deleted_voiceprint_file(monkeypatch, tmp_path, caplog):
    """The real matcher under the upkeep loop: ready once the voiceprints load,
    not ready again soon after the file is deleted — with nobody speaking."""
    import numpy as np

    from orchestrator import voice_match

    caplog.set_level(logging.INFO, logger="orchestrator.voice_satellite")
    rounds = _Rounds()
    monkeypatch.setattr(vs, "asyncio", rounds)
    prints = tmp_path / "voice_match" / "voiceprints.json"
    voice_match.save_voiceprints(str(prints), {"owner": np.full(voice_match.EMBED_DIM, 1 / 16, dtype=np.float32)})
    matcher = voice_match.VoiceMatcher(str(tmp_path / "voice_match" / "m.onnx"), str(prints))
    monkeypatch.setattr(matcher, "_load_session", lambda: object())  # the model is test_voice_match's business
    device = "atom-echo-upkeep"

    task = asyncio.create_task(vs._voice_match_upkeep(matcher, frozenset({device})))
    try:
        await _settle(lambda: rounds.rounds == 1, "first upkeep round")
        assert _ready(device) == 1 and _ready_series(device)
        prints.unlink()
        rounds.next()
        await _settle(lambda: rounds.rounds == 2, "second upkeep round")
        assert _ready(device) == 0
        assert "Voice match not ready (no_voiceprints)" in caplog.text
    finally:
        await _stop(task)  # parked between rounds: a clean cancel


class _HeldPool:
    """Stands in for the matcher's ThreadPoolExecutor: the test decides when a
    call finishes (everything else is the real VoiceMatcher._run path)."""

    def __init__(self) -> None:
        self.calls: list = []

    def submit(self, fn, *args):
        import concurrent.futures

        future = concurrent.futures.Future()
        self.calls.append((future, fn, args))
        return future


async def test_voice_match_upkeep_stops_when_cancelled_as_a_preload_finishes(tmp_path):
    """Shutdown cancels the bridge in the moment an upkeep round's preload
    result has come back from the pool but the upkeep hasn't resumed yet."""
    import contextlib

    from orchestrator import voice_match

    matcher = voice_match.VoiceMatcher(str(tmp_path / "m.onnx"), str(tmp_path / "voice_match" / "p.json"))
    pool = _HeldPool()
    matcher._executor = pool
    task = asyncio.create_task(vs._voice_match_upkeep(matcher, frozenset({"atom-echo-race"})))
    try:
        await _until(lambda: pool.calls)  # the upkeep is waiting on its preload
        ((future, fn, args),) = pool.calls
        future.set_result(fn(*args))  # the preload finishes ("model_missing") ...
        await asyncio.sleep(0)  # ... its result reaches the event loop ...
        task.cancel()  # ... and shutdown cancels the bridge before the upkeep resumes
        done, _ = await asyncio.wait({task}, timeout=1)
        assert task in done and task.cancelled()
    finally:
        task.cancel()  # a dropped first cancel left it in the between-rounds sleep
        with contextlib.suppress(BaseException):
            await task


@pytest.mark.parametrize(
    "listed",
    [
        pytest.param(f" {NODE} , {OTHER} ", id="spaces"),
        pytest.param(f"{OTHER},{NODE},{OTHER}", id="repeat"),
        pytest.param(f",{NODE},,{OTHER},", id="empty-entries"),
    ],
)
def test_config_voice_match_devices_tolerates_spaces_repeats_and_empties(caplog, listed):
    s = _settings(
        VOICE_SATELLITE_HOST=None,
        VOICE_SATELLITE_SPEAKER=None,
        VOICE_SATELLITE_DEVICES=TWO_DEVICES,
        VOICE_MATCH_DEVICES=listed,
    )
    assert s.voice_match_device_set() == frozenset({NODE, OTHER})
    assert s.voice_satellite_config_problems == 0
    assert "VOICE_MATCH_DEVICES" not in caplog.text


def test_config_voice_match_single_device_by_its_configured_name(caplog):
    s = _settings(VOICE_SATELLITE_NAME="atom-echo-kitchen", VOICE_MATCH_DEVICES="atom-echo-kitchen")
    assert s.voice_match_device_set() == frozenset({"atom-echo-kitchen"})
    assert s.voice_satellite_config_problems == 0
    stale = _settings(VOICE_SATELLITE_NAME="atom-echo-kitchen", VOICE_MATCH_DEVICES="atom-echo-jess")
    assert stale.voice_match_device_set() == frozenset()  # the default name is not this device
    assert stale.voice_satellite_config_problems == 1
    assert "VOICE_MATCH_DEVICES entry 1 is not a configured device" in caplog.text


def test_config_voice_match_on_a_broken_bridge_is_off_and_not_counted_twice():
    s = _settings(VOICE_SATELLITE_API_KEY=None, VOICE_MATCH_DEVICES="atom-echo-jess")
    assert not s.voice_satellite_enabled and s.voice_match_device_set() == frozenset()
    assert s.voice_satellite_config_problems == 1  # the key; voice match adds nothing on top


def test_config_voice_match_on_a_skipped_device_counts_both_problems():
    """The device entry is bad AND its room is then unfiltered: two problems."""
    s = _settings(
        VOICE_SATELLITE_HOST=None,
        VOICE_SATELLITE_SPEAKER=None,
        VOICE_SATELLITE_DEVICES=f"{NODE}@10.0.0.50={SPEAKER},{OTHER}@echo.local={OTHER_SPEAKER}",
        VOICE_MATCH_DEVICES=OTHER,
    )
    assert [d.name for d in s.voice_satellites()] == [NODE]
    assert s.voice_match_device_set() == frozenset()
    assert s.voice_satellite_config_problems == 2


@pytest.mark.parametrize(("value", "expected"), [(" 0.5 ", 0.5), ("0.999", 0.999), ("1e-2", 0.01)])
def test_config_voice_match_threshold_accepts_padded_and_written_out_numbers(value, expected):
    s = _settings(VOICE_MATCH_THRESHOLD=value, VOICE_MATCH_DEVICES="atom-echo-jess")
    assert s.voice_match_threshold == pytest.approx(expected)
    assert s.voice_satellite_config_problems == 0
