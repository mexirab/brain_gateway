"""Voice satellite bridge — an ESPHome voice device (ATOM Echo S3R) talks to Jess
directly, without Home Assistant's voice pipeline.

    "Hey Jess" / "Okay Jess"            wake word runs ON the device
      → device beeps, streams 16 kHz mono PCM over the ESPHome native API
      → this module (Jupiter): end-of-speech detection (EnergyVad)
      → STT on Helios (STT_URL)
      → one Jess voice turn (cloud_brain.chat, is_voice=True — the same brain
        call the dashboard's /v1/chat/completions makes)
      → reminder_manager._announce_voice(...) → VoxCPM2 → HA play_media on the
        device's own room speaker (required per device; no fallback to other
        rooms). One play_media per reply; with progressive TTS
        (default) a long reply's first ~8 s play while the rest is synthesized
        into the same stream (audio_stream.py)

Home Assistant only plays the reply; the device must NOT also be in HA's ESPHome
integration (it accepts one voice-assistant subscriber — the two would fight).

Several devices (VOICE_SATELLITE_DEVICES, one per room): one ``Satellite`` —
connection, busy flag, rate limit, follow-up history — per device, all in the
one bridge task; each answers on its own room's speaker. One "Hey Jess" heard
by two devices: the first to report it answers (or says "busy"), the other
ends its run silently once it streams (``WakeArbiter``, result ``duplicate``).

Connection: outbound from the orchestrator to each device (:6053), encrypted
with the device's API key (noise PSK) and pinned to the device's ESPHome node
name (``expected_name``) so a DHCP reshuffle can't point us at another device.
Returning port 0 from ``handle_start`` means "send audio over the API
connection" — no ports are opened here.

Event contract with the firmware (``models/atom-echo-jess.yaml``):

* The device run ends as soon as the utterance is captured, with ``RUN_END``
  ALONE: from STREAMING_MICROPHONE it stops the mic and goes IDLE. (Sending
  ``STT_VAD_END`` first leaves the device in STOP_MICROPHONE when ``RUN_END``
  arrives, which ignores it; the device then sits in AWAITING_RESPONSE and
  never listens again — ESPHome voice_assistant.cpp, 2026.10.) The device is
  straight back to listening for the wake word while Jess thinks; the answer
  plays on the room's Google speaker, not the device.
* Outcomes the device should make audible arrive as ``VOICE_ASSISTANT_ERROR``
  events with one of these codes (``on_error`` maps them to sounds):

  ``jess-no-speech``  nothing (or no words) heard after the wake word
  ``jess-busy``       a turn from this device is still running, or too many
                      turns in a short time (rate limit)
  ``jess-waking``     Helios is asleep: woken, the answer comes when it's up
                      (not a failure — the turn continues)
  ``jess-asleep``     Helios is asleep and waking is off (HELIOS_WAKE_ENABLED)
  ``jess-error``      anything else failed (STT, brain, TTS, speaker)

  An ERROR also reaches the device when its run has already ended; ESPHome
  still fires ``on_error``.

Decisions (docs/VOICE_AND_TTS.md → Voice satellite → Decisions): replies play even under
sleep mode (D4, ``ignore_dnd``; announcements stay muted) and ONLY on the
device's own room speaker (required for every device; no fallback to
other rooms, D10); Helios asleep → wake + answer late, capped
(D5); the reply bypasses the mid-conversation guard that holds reminders off
while you talk (D6); short per-device history, reset after 5 min of silence
(D7); anyone in the room can talk to it (D8) — so these turns never feed
auto_learn (``skip_auto_learn_var``), replies are not stored in
announcement_history (no replay into /chat), and turns are rate-limited.

Privacy (D9): mic audio is held in RAM for one utterance and never written to
disk. The spoken reply goes the announcement way (a WAV in /tmp/brain_audio
served to the Cast speaker by an unguessable URL for up to an hour). Logs
carry lengths, levels, outcomes and the fixed wake-word label — never the
transcript or the reply (``tests/test_no_user_content_in_logs.py``); the
aioesphomeapi/zeroconf loggers are held at WARNING with their arguments
dropped (``log_config.DropPayloadArgsFilter``).

The device check is the noise key: ``expected_name`` only guards against a
DHCP mix-up. Anyone holding the key (lost device, the build box's
secrets.yaml) can pose as the device from the LAN — rotate it if lost. All
devices share ONE key (VOICE_SATELLITE_API_KEY), and ESPHome stores it
readable in flash (no flash encryption by default): someone with physical
access to any one device for a minute can read it over USB and then pose as
any room's device, or talk to every device's API. Rotating means reflashing
every device with the new key.

Known limits: devices are env-configured (≤ 8, one shared API key); energy
VAD — it can't tell the user from a TV or another person talking, and a room
much louder than the tuning room (~0.045 RMS; TV on) can false-start and
record to the 15 s cap (``bgw_satellite_listen_end_total{reason="max_utterance"}``;
level alone can't separate a loud room from this device's fairly steady
speech — a spectral VAD would); no follow-up listening without the wake word;
two devices that both hear one "Hey Jess": the FIRST to report it answers,
not necessarily the nearer one (both beep) — if that one then hears nothing,
nobody answers — and two people saying it in two rooms within
DUPLICATE_WAKE_S get one answer (likewise a TV or game false-waking one room
swallows a real "Hey Jess" in another for that long); a device that reports the wake more than
DUPLICATE_WAKE_S after the other (beep held up on its bus) answers too; two
rooms answering at once queue on the one brain slot and the one TTS worker,
and share the one-reply voice style (voice_style keeps it process-wide); the
turn rate limit is per device, not bridge-wide; a turn that hits
CHAT_TIMEOUT_S may already have run tools (reminder set, light switched)
before the device plays the error sound.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import io
import logging
import math
import os
import re
import sys
import time
import traceback
import wave
from array import array
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

from orchestrator.config import settings as _settings

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2  # 16-bit mono PCM from the device
FRAME_MS = 50
FRAME_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * FRAME_MS // 1000  # 1600

HISTORY_IDLE_SECONDS = 300  # D7: a new conversation after 5 min of silence
HISTORY_MAX_MESSAGES = 12
MAX_REPLY_CHARS = 1200  # spoken portion cap (one play_media per reply, D3)
FIRST_AUDIO_TIMEOUT_S = 4.0  # run started but the device never streamed
RUN_WALL_TIMEOUT_S = 30.0  # wall-clock backstop for one listening run
CHAT_TIMEOUT_S = 300.0  # backstop only; the loop has its own per-round timeouts
ANNOUNCE_TIMEOUT_S = 120.0
STT_TIMEOUT_S = 30.0
CONNECT_TIMEOUT_S = 15.0
SERVICE_GRACE_S = 90.0  # Helios on but a service not up yet (boot, brain reload)
FAILURE_SUMMARY_S = 3600.0  # repeated connect failures: one summary line an hour
BACKOFF_MIN_S = 5.0
BACKOFF_MAX_S = 120.0
RATE_LIMIT_TURNS = 6  # turns per device per RATE_LIMIT_WINDOW_S (TV false-waking it)
RATE_LIMIT_WINDOW_S = 300.0
# A wake word on another device this soon after = the same utterance heard
# twice (Home Assistant's assist pipeline uses a 2 s cooldown the same way;
# 3 s here because the device reports the wake only after its beep, whose
# bus waits can add a second or more).
DUPLICATE_WAKE_S = 3.0
LISTEN_END_REASONS = (
    "speech_end",
    "max_utterance",
    "no_speech",
    "device_stop",
    "wall_timeout",
    "no_audio",
    "aborted",
    "duplicate",
)

ERR_NO_SPEECH = "jess-no-speech"
ERR_BUSY = "jess-busy"
ERR_WAKING = "jess-waking"
ERR_ASLEEP = "jess-asleep"
ERR_FAILED = "jess-error"

# Metric label for the wake word that started a run — a fixed set, never the
# raw phrase string the device sends.
_WAKE_WORD_LABELS = {
    "heyjess": "hey_jess",
    "okayjess": "okay_jess",
    "okjess": "okay_jess",
    "okaynabu": "okay_nabu",
}

ChatFn = Callable[[List[Dict[str, Any]]], Awaitable[Any]]


def wake_word_label(phrase: Optional[str]) -> str:
    """Bounded label for the wake word: hey_jess | okay_jess | okay_nabu | button | other."""
    if not phrase:
        return "button"
    key = re.sub(r"[^a-z]", "", phrase.lower())
    return _WAKE_WORD_LABELS.get(key, "other")


# ---------------------------------------------------------------------------
# End-of-speech detection — energy VAD driven by audio time (deterministic).
#
# The room's quiet level is LEARNED, never assumed. On the ATOM Echo S3R in a
# real room (2026-11-11) silence read 0.025-0.09 RMS per 50 ms frame (median
# 0.042: fans, air, mic hiss) and a spoken question 0.19-0.24. The first
# version was a port of the dashboard's browser-mic VAD and capped the learned
# quiet level at 0.015, so the room itself sat above its "you've stopped" bar:
# every question ran to the 15 s cap, and a room level learned before speech
# could hide a quieter question entirely.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VadConfig:
    """Levels are frame RMS, 0..1 of full scale; times are audio milliseconds."""

    smooth_ms: int = 200  # decide on the mean level over this window, not one frame
    noise_window_ms: int = 2_000  # learn the room's quiet level from this much recent audio…
    noise_percentile: float = 0.25  # …as a low percentile (pauses between words keep it honest)
    # The mic can read near-silent while it starts: those frames (and the
    # smoothed levels that still average them in) are not learned from.
    skip_ms: int = 100
    # The user often talks straight after the beep, so until bootstrap_ms the
    # learned level is held between max_bootstrap_noise × bootstrap_floor_ratio
    # and max_bootstrap_noise: their first words can't become "the room", and
    # near-silent start-up frames can't make the room itself count as speech.
    # 0.045 sits just above the measured room (0.042). Sim, 400 runs each: a
    # quiet voice straight after the beep caught 99% (cap 0.06: 7%). Cost: a
    # room much louder than that (TV on, ~0.075) can false-start in the first
    # 1.5 s and then run to the 15 s cap (bgw_satellite_listen_end_total
    # {reason="max_utterance"}).
    bootstrap_ms: int = 1_500
    max_bootstrap_noise: float = 0.045
    bootstrap_floor_ratio: float = 0.8
    min_noise: float = 0.003  # a silent room: don't let the bars collapse to zero
    # Speech = smoothed level ≥ room × start_ratio, and at least
    # min_start_level (a silent room: one 50 ms click averages to a quarter of
    # its level and must stay under it), with min_loud_frames single frames
    # above the bar in smooth_ms (a click has one), for min_speech_ms.
    start_ratio: float = 1.8
    min_start_level: float = 0.03
    min_loud_frames: int = 2
    min_speech_ms: int = 150
    # Finished = for the last silence_ms nothing reached the speech bar AND the
    # mean level over that time is below room × end_ratio (a mean, so one
    # noise blip doesn't restart the clock). end_ratio well under start_ratio
    # so soft words between the two bars keep the utterance alive. The room
    # level stays frozen while speaking: speech from this device is too steady
    # (smoothed p90/p25 ≈ 1.3) to tell it apart from a loud room by level
    # alone, so any "re-learn while talking" cuts long questions off.
    end_ratio: float = 1.35
    min_end_level: float = 0.015  # end bar floor in a silent room
    silence_ms: int = 1_200
    max_utterance_ms: int = 15_000  # cap: process what we have
    no_speech_ms: int = 5_000  # nothing said this long after the wake word


def _percentile(values: Any, fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


class EnergyVad:
    """Feed one RMS level per 50 ms frame; returns an event or None.

    Events: ``speech-start``, ``speech-end`` (quiet after speech),
    ``max-utterance`` (cap reached while talking), ``no-speech`` (nothing said
    within ``no_speech_ms``). ``floor`` is the room's learned quiet level; it
    is frozen while speech is in progress, so a long sentence can't raise its
    own bar.
    """

    def __init__(self, cfg: Optional[VadConfig] = None) -> None:
        self.cfg = cfg or VadConfig()
        c = self.cfg
        self.floor = c.max_bootstrap_noise  # until the room has been heard
        self.level = 0.0  # smoothed level of the latest frames
        self.max_level = 0.0  # highest smoothed level this run
        self.speaking = False
        self.heard_speech = False
        self.peak = 0.0
        self.speech_at: Optional[int] = None  # audio ms of the first frame of speech
        self._recent: collections.deque[float] = collections.deque(maxlen=max(1, c.smooth_ms // FRAME_MS))
        self._history: collections.deque[float] = collections.deque(maxlen=max(1, c.noise_window_ms // FRAME_MS))
        self._tail: collections.deque[float] = collections.deque(maxlen=max(1, c.silence_ms // FRAME_MS))
        self._above_since: Optional[int] = None
        self._last_loud = 0
        self._started_at = 0
        self._now = 0

    @property
    def threshold(self) -> float:
        """Smoothed level that counts as speech starting."""
        return max(self.cfg.min_start_level, self.floor * self.cfg.start_ratio)

    @property
    def end_threshold(self) -> float:
        """Mean level over the last ``silence_ms`` below which the speaker has stopped."""
        return max(self.cfg.min_end_level, self.floor * self.cfg.end_ratio)

    @property
    def tail_mean(self) -> float:
        """Mean frame level over the last ``silence_ms`` of speech (0 before speech)."""
        return sum(self._tail) / len(self._tail) if self._tail else 0.0

    @property
    def quiet_ms(self) -> int:
        """Audio ms since the smoothed level last reached the speech bar (0 before speech)."""
        return self._now - self._last_loud if self.heard_speech else 0

    def _learn_floor(self, now_ms: int) -> None:
        c = self.cfg
        estimate = _percentile(self._history, c.noise_percentile)
        if now_ms < c.bootstrap_ms:
            cap = c.max_bootstrap_noise
            estimate = min(max(estimate, cap * c.bootstrap_floor_ratio), cap)
        self.floor = max(c.min_noise, estimate)

    def feed(self, rms: float, now_ms: int) -> Optional[str]:
        c = self.cfg
        self._now = now_ms
        self.peak = max(self.peak, rms)
        self._recent.append(rms)
        self.level = sum(self._recent) / len(self._recent)
        self.max_level = max(self.max_level, self.level)
        if not self.speaking:
            if now_ms > c.skip_ms + c.smooth_ms:
                self._history.append(self.level)
                self._learn_floor(now_ms)
            bar = self.threshold
            # A click is one loud frame that smoothing stretches over
            # smooth_ms; speech has several.
            if self.level >= bar and sum(1 for r in self._recent if r >= bar) >= c.min_loud_frames:
                if self._above_since is None:
                    self._above_since = now_ms
                if now_ms - self._above_since >= c.min_speech_ms:
                    self.speaking = True
                    self.heard_speech = True
                    self._started_at = self._above_since
                    self.speech_at = self._above_since
                    self._above_since = None
                    self._last_loud = now_ms
                    return "speech-start"
            else:
                self._above_since = None
            if not self.heard_speech and self._above_since is None and now_ms >= c.no_speech_ms:
                return "no-speech"
            return None
        # Speaking: the floor is frozen.
        self._tail.append(rms)
        if self.level >= self.threshold:
            self._last_loud = now_ms
        if now_ms - self._last_loud >= c.silence_ms and self.tail_mean < self.end_threshold:
            self.speaking = False
            return "speech-end"
        if now_ms - self._started_at >= c.max_utterance_ms:
            self.speaking = False
            return "max-utterance"
        return None


def frame_rms(frame: bytes) -> float:
    """RMS of a little-endian int16 PCM frame around its own mean, scaled to 0..1.

    The mean is removed so a mic's constant DC offset can't read as room
    noise (the firmware also sets ``correct_dc_offset``; this covers devices
    that don't).
    """
    usable = len(frame) - (len(frame) % SAMPLE_WIDTH)
    if usable <= 0:
        return 0.0
    samples = array("h")
    samples.frombytes(frame[:usable])
    if sys.byteorder != "little":
        samples.byteswap()
    n = len(samples)
    mean = sum(samples) / n
    total = 0.0
    for s in samples:
        d = s - mean
        total += d * d
    return math.sqrt(total / n) / 32768.0


def pcm_to_wav(pcm: bytes) -> bytes:
    """Wrap 16 kHz mono int16 PCM in a WAV container for the STT server."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
_MD_CHARS = re.compile(r"[*_#`~>|]+")
_WS = re.compile(r"\s+")


def speakable(reply: str) -> str:
    """Plain text for the spoken reply: link text instead of links, no URLs or
    markdown marks, capped at a sentence end."""
    out = _URL.sub(" ", _MD_LINK.sub(r"\1", reply or ""))
    out = _WS.sub(" ", _MD_CHARS.sub(" ", out)).strip()
    if len(out) <= MAX_REPLY_CHARS:
        return out
    cut = out[:MAX_REPLY_CHARS]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > MAX_REPLY_CHARS // 2 else cut


def reply_text(response: Any) -> str:
    """Assistant text out of cloud_brain.chat's non-streamed JSONResponse."""
    import json

    body = getattr(response, "body", None)
    if isinstance(response, dict):
        data = response
    elif isinstance(body, (bytes, bytearray)):
        try:
            data = json.loads(body)
        except ValueError:
            return ""
    else:
        return ""
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else None
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    return content.strip() if isinstance(content, str) else ""


# ---------------------------------------------------------------------------
# One listening run (wake word → end of speech)
# ---------------------------------------------------------------------------


class WakeArbiter:
    """One spoken wake word, two devices in earshot: only the first answers.

    Shared by every Satellite in the bridge. Any (non-button) wake word on
    another device within ``window_s`` of the current claim is a duplicate; a
    button press (deliberate) never is. A device that refuses (busy,
    rate-limited) still claims: you hear "busy" in the room you're in rather
    than an answer from the next room's speaker. The window runs from the
    claim's FIRST wake — the holder waking again doesn't extend it.
    """

    def __init__(self, window_s: float = DUPLICATE_WAKE_S) -> None:
        self._window = window_s
        self._claimed_at = -math.inf
        self.holder = ""  # device of the current claim (for the log line)

    def claim(self, device: str, wake: str) -> bool:
        """True = this device answers (or refuses) the wake word itself."""
        if wake == "button":
            return True
        now = time.monotonic()
        if now - self._claimed_at < self._window:
            return device == self.holder
        self._claimed_at, self.holder = now, device
        return True


@dataclass
class _Run:
    wake_word: str
    duplicate: bool = False  # another device answers this wake word: end quietly
    owns_busy: bool = True  # False = a duplicate run during this device's own turn
    started: float = field(default_factory=time.monotonic)
    vad: EnergyVad = field(default_factory=EnergyVad)
    pcm: bytearray = field(default_factory=bytearray)
    pending: bytearray = field(default_factory=bytearray)
    audio_ms: int = 0
    first_audio: bool = False
    done: bool = False
    watcher: Optional[asyncio.Task] = None
    helios: str = "unchecked"  # unchecked | awake | woken (metric label)
    stages: Dict[str, int] = field(default_factory=dict)  # stage -> ms


class Satellite:
    """One ESPHome voice device: connection, listening runs and Jess turns."""

    def __init__(
        self,
        *,
        chat: ChatFn,
        host: str,
        port: int,
        api_key: str,
        node_name: str,
        speaker: str,
        helios_timeout_s: int = 180,
        progressive_tts: bool = True,
        arbiter: Optional[WakeArbiter] = None,
        client_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._chat = chat
        self._arbiter = arbiter  # shared by all devices; None = no duplicate check
        self._progressive_tts = progressive_tts
        self._host = host
        self._port = port
        self._api_key = api_key
        self._node = node_name  # ESPHome node name; also the metric label
        self._speaker = speaker
        self._helios_timeout_s = max(0, helios_timeout_s)
        self._client_factory = client_factory
        self._client: Any = None
        self._run: Optional[_Run] = None
        self._busy = False
        self._history: List[Dict[str, str]] = []
        self._last_turn_at = 0.0
        self._conv_started = 0
        self._tasks: set[asyncio.Task] = set()
        self._turn_starts: collections.deque[float] = collections.deque()
        self._fail_reason = ""
        self._fail_count = 0
        self._fail_since = 0.0
        self._fail_summary_at = 0.0

    # -- device events ----------------------------------------------------

    def _send(self, event: str, data: Optional[Dict[str, str]] = None) -> None:
        """Send a VoiceAssistantEventType by name; never raises (device may be gone)."""
        from aioesphomeapi import VoiceAssistantEventType

        client = self._client
        if client is None:
            return
        try:
            client.send_voice_assistant_event(getattr(VoiceAssistantEventType, event), data)
        except Exception as e:  # noqa: BLE001 — a dropped connection must not break the turn
            logger.debug("[SATELLITE] %s: send %s failed (%s)", self._node, event, type(e).__name__)

    def _send_error(self, code: str) -> None:
        self._send("VOICE_ASSISTANT_ERROR", {"code": code, "message": code})

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _release(self, run: _Run) -> None:
        """A listening run ended without a turn: free the device — unless the
        run was a duplicate during this device's own turn, which still runs
        and still owns the flag (its _turn clears it when done)."""
        if run.owns_busy:
            self._busy = False

    def _rate_limited(self) -> bool:
        now = time.monotonic()
        while self._turn_starts and now - self._turn_starts[0] > RATE_LIMIT_WINDOW_S:
            self._turn_starts.popleft()
        return len(self._turn_starts) >= RATE_LIMIT_TURNS

    def _count(self, result: str, wake: str, started: Optional[float] = None, helios: str = "unchecked") -> None:
        from orchestrator.metrics import SATELLITE_TURN_SECONDS, SATELLITE_TURN_TOTAL

        SATELLITE_TURN_TOTAL.labels(result=result, wake_word=wake, helios=helios, device=self._node).inc()
        if started is not None:
            SATELLITE_TURN_SECONDS.labels(helios=helios).observe(max(0.0, time.monotonic() - started))

    async def handle_start(
        self,
        conversation_id: str,
        flags: int,
        audio_settings: Any,
        wake_word_phrase: Optional[str] = None,
        *_extra: Any,
    ) -> Optional[int]:
        wake = wake_word_label(wake_word_phrase)
        # Claimed before the busy check: a busy room keeps the wake word (says
        # "busy") instead of handing the question to the next room (WakeArbiter).
        duplicate = self._arbiter is not None and not self._arbiter.claim(self._node, wake)
        refuse = "busy" if self._busy else "rate_limited" if self._rate_limited() else ""
        # A duplicate is ended silently even when this device would refuse:
        # "busy" here for the next room's question would be noise. (Unless a
        # listening run is somehow still open — that one keeps the device.)
        if refuse and (not duplicate or self._run is not None):
            # Accept, then say "busy" once the start reply has gone out (it is
            # sent from this coroutine's done-callback; an error that overtook
            # it could leave the device streaming into a run nobody owns).
            # Its audio is ignored: there is no current run.
            logger.info("[SATELLITE] %s: wake (%s) refused: %s", self._node, wake, refuse)
            self._count(refuse, wake)
            asyncio.get_running_loop().call_later(0.2, self._send_error, ERR_BUSY)
            return 0
        # Another device just took this wake word: accept the run, then end it
        # silently once the device streams (handle_audio) — RUN_END alone is
        # only safe from STREAMING_MICROPHONE (see the module docstring). On a
        # device whose own turn is still running, the turn keeps the busy flag.
        owns_busy = not self._busy
        self._busy = True
        run = _Run(wake_word=wake, duplicate=duplicate, owns_busy=owns_busy)
        self._run = run
        if duplicate:
            holder = self._arbiter.holder if self._arbiter is not None else ""
            logger.info("[SATELLITE] %s: wake (%s) also heard by %s, which answers", self._node, wake, holder)
        else:
            logger.info("[SATELLITE] %s: run started (wake=%s)", self._node, wake)
        run.watcher = self._spawn(self._watch_run(run))
        return 0  # 0 = stream the audio over the API connection

    async def handle_audio(self, chunk: bytes, _chunk2: Optional[bytes] = None) -> None:
        run = self._run
        if run is None or run.done or not chunk:
            return
        if not run.first_audio:
            run.first_audio = True
            self._send("VOICE_ASSISTANT_RUN_START")
            self._send("VOICE_ASSISTANT_STT_START")
            if run.duplicate:
                self._end_run(run, captured=False, reason="duplicate")
                return
        run.pcm.extend(chunk)
        run.pending.extend(chunk)
        while len(run.pending) >= FRAME_BYTES and not run.done:
            frame = bytes(run.pending[:FRAME_BYTES])
            del run.pending[:FRAME_BYTES]
            run.audio_ms += FRAME_MS
            event = run.vad.feed(frame_rms(frame), run.audio_ms)
            if event == "speech-start":
                self._send("VOICE_ASSISTANT_STT_VAD_START")
            elif event in ("speech-end", "max-utterance"):
                self._end_run(run, captured=True, reason=event)
            elif event == "no-speech":
                self._end_run(run, captured=False, reason=event)

    async def handle_stop(self, abort: bool) -> None:
        run = self._run
        if run is None or run.done:
            return
        if abort:
            # The device cancelled (button, mute, its own timeout).
            run.done = True
            self._run = None
            self._release(run)
            if run.watcher is not None:
                run.watcher.cancel()
            logger.info("[SATELLITE] %s: run aborted by the device", self._node)
            from orchestrator.metrics import SATELLITE_LISTEN_END_TOTAL

            SATELLITE_LISTEN_END_TOTAL.labels(reason="aborted", device=self._node).inc()
            self._count("aborted", run.wake_word)
            return
        # Device ended its stream before our VAD did: use what we have.
        self._end_run(run, captured=run.vad.heard_speech, reason="device-stop")

    async def _watch_run(self, run: _Run) -> None:
        """Wall-clock backstops: no audio at all, or a run that never ends."""
        await asyncio.sleep(FIRST_AUDIO_TIMEOUT_S)
        if not run.done and not run.first_audio:
            logger.warning("[SATELLITE] %s: run started but no audio arrived", self._node)
            self._end_run(run, captured=False, reason="no-audio", result="no_audio")
            return
        remaining = RUN_WALL_TIMEOUT_S - FIRST_AUDIO_TIMEOUT_S
        await asyncio.sleep(max(0.0, remaining))
        if not run.done:
            self._end_run(run, captured=run.vad.heard_speech, reason="wall-timeout")

    def _end_run(self, run: _Run, *, captured: bool, reason: str, result: str = "no_speech") -> None:
        """Stop listening: hand the device back to wake-word mode, then process."""
        if run.done:
            return
        run.done = True
        self._run = None
        if run.watcher is not None and run.watcher is not asyncio.current_task():
            run.watcher.cancel()
        # RUN_END alone — see the module docstring (STT_VAD_END first wedges the device).
        self._send("VOICE_ASSISTANT_RUN_END")
        from orchestrator.metrics import SATELLITE_LISTEN_END_TOTAL

        vad = run.vad
        SATELLITE_LISTEN_END_TOTAL.labels(reason=reason.replace("-", "_"), device=self._node).inc()
        # log-safe: audio ms, RMS levels, counters and a fixed reason string —
        # everything the VAD decided on, so a mis-tune shows in Loki
        logger.info(
            "[SATELLITE] %s: listening ended (%s) audio=%dms floor=%.4f start=%.4f end=%.4f "
            "peak=%.4f max=%.4f tail=%.4f quiet=%dms speech_at=%dms speech=%s",
            self._node,
            reason,
            run.audio_ms,
            vad.floor,
            vad.threshold,
            vad.end_threshold,
            vad.peak,
            vad.max_level,
            vad.tail_mean,
            vad.quiet_ms,
            -1 if vad.speech_at is None else vad.speech_at,
            vad.heard_speech,
        )
        if run.duplicate and run.first_audio:
            # Ended at its first audio (handle_audio): no sound, no turn — the
            # device that claimed the wake word answers. A duplicate that never
            # streamed falls through: the ERROR below is what takes a device
            # that isn't streaming back to idle (RUN_END alone may not).
            self._release(run)
            self._count("duplicate", run.wake_word)
            return
        if not captured:
            self._release(run)
            self._count(result, run.wake_word)
            self._send_error(ERR_NO_SPEECH if result == "no_speech" else ERR_FAILED)
            return
        self._turn_starts.append(time.monotonic())
        self._spawn(self._turn(run))

    # -- the Jess turn ------------------------------------------------------

    async def _turn(self, run: _Run) -> None:
        from orchestrator.log_config import set_request_id

        set_request_id(f"sat-{os.urandom(4).hex()}")  # joins this turn's satellite + chat log lines
        speech_end = time.monotonic()
        try:
            result = await self._turn_inner(run)
        except Exception as e:  # noqa: BLE001 — a turn must never kill the bridge
            # Type + last frame only: exception text / a full traceback can
            # carry the transcript or reply into Loki.
            frame = traceback.extract_tb(e.__traceback__)[-1] if e.__traceback__ else None
            where = f"{os.path.basename(frame.filename)}:{frame.lineno}" if frame else "?"
            logger.error("[SATELLITE] %s: turn crashed: %s at %s", self._node, type(e).__name__, where)
            result = "error"
            self._send_error(ERR_FAILED)
        finally:
            self._busy = False
        self._count(result, run.wake_word, started=speech_end if result == "ok" else None, helios=run.helios)
        stages = " ".join(f"{k}={v}ms" for k, v in run.stages.items())
        # log-safe: fixed result/label strings and stage durations only
        logger.info(
            "[SATELLITE] %s: turn %s (wake=%s helios=%s) %s total=%dms",
            self._node,
            result,
            run.wake_word,
            run.helios,
            stages,
            int((time.monotonic() - speech_end) * 1000),
        )

    async def _turn_inner(self, run: _Run) -> str:
        from orchestrator import shared
        from orchestrator.cloud_brain import skip_auto_learn_var

        # Room audio may not be the owner's words: never feed auto_learn (D8).
        skip_auto_learn_var.set(True)
        # Reminders hold off while the user is talking to Jess (D6).
        shared.mark_voice_activity()

        t = time.monotonic()
        failure = await self._ensure_helios(run)
        run.stages["helios_wait"] = int((time.monotonic() - t) * 1000)
        if failure:
            return failure

        t = time.monotonic()
        transcript = await self._transcribe(bytes(run.pcm))
        run.stages["stt"] = int((time.monotonic() - t) * 1000)
        if transcript is None:
            self._send_error(ERR_FAILED)
            return "stt_failed"
        if not transcript:
            self._send_error(ERR_NO_SPEECH)
            return "empty_transcript"
        logger.info("[SATELLITE] %s: transcript %d chars", self._node, len(transcript))

        t = time.monotonic()
        reply = await self._ask_jess(transcript)
        run.stages["brain"] = int((time.monotonic() - t) * 1000)
        spoken = speakable(reply) if reply else ""
        if not spoken:
            self._send_error(ERR_FAILED)
            return "chat_failed"

        shared.mark_voice_activity()
        t = time.monotonic()
        outcome = await self._speak(spoken)
        run.stages["speak"] = int((time.monotonic() - t) * 1000)
        if outcome != "ok":
            self._send_error(ERR_FAILED)
            return outcome
        # Only a reply that started playing goes into the follow-up history.
        # (A progressive reply whose later sentences failed to synthesize
        # stops early but is remembered whole — bgw_tts_progressive_total.)
        self._remember(transcript, spoken)
        return "ok"

    async def _service_status(self, names: tuple) -> Dict[str, bool]:
        from orchestrator import shared
        from orchestrator.service_registry import services

        client = getattr(shared, "_http", None)  # carries MODEL_SERVER_AUTH for the brain probe
        if client is not None and client.is_closed:
            client = None
        results = await asyncio.gather(
            *(services.check_service(n, client=client, timeout=3.0) for n in names), return_exceptions=True
        )
        return {n: r is True for n, r in zip(names, results, strict=True)}

    async def _helios_running(self) -> bool:
        """Plug reading says Helios is powered. helios_power_status() only reads
        the plug with HELIOS_WAKE_ENABLED, so with wake off this is always False
        and a booting Helios is reported as asleep (no wake is attempted then
        anyway)."""
        from orchestrator import helios_power

        try:
            status = await helios_power.helios_power_status()
        except Exception:  # noqa: BLE001 — never raises by contract; belt and braces
            return False
        return status.get("inferred") == "running"

    async def _ensure_helios(self, run: _Run) -> Optional[str]:
        """STT, brain and TTS all live on Helios (D5).

        All up → go. Nothing answering and the plug says off → asleep: wake it
        (if allowed) and wait up to VOICE_SATELLITE_HELIOS_TIMEOUT_SECONDS.
        Helios on but a service down (booting, brain reloading, a crashed
        unit) → no wake; wait SERVICE_GRACE_S, then fail as service_down.
        """
        needed = ("stt", "model", "tts")
        status = await self._service_status(needed)
        if all(status.values()):
            run.helios = "awake"
            return None
        down = ",".join(n for n in needed if not status[n])
        powered = any(status.values()) or await self._helios_running()
        logger.info("[SATELLITE] %s: not ready: %s (helios %s)", self._node, down, "on" if powered else "off")
        if powered:
            budget, woken = SERVICE_GRACE_S, False
        else:
            if not _settings.helios_wake_enabled:
                self._send_error(ERR_ASLEEP)
                return "helios_asleep"
            from orchestrator import helios_power

            wake = await helios_power.wake_helios()
            if not wake.get("ok"):
                logger.warning("[SATELLITE] %s: Helios wake call failed", self._node)
                self._send_error(ERR_FAILED)
                return "wake_failed"
            budget, woken = float(self._helios_timeout_s), True
        self._send_error(ERR_WAKING)  # device: "the answer comes later" sound
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            await asyncio.sleep(5.0)
            if all((await self._service_status(needed)).values()):
                run.helios = "woken"
                logger.info("[SATELLITE] %s: Helios services ready", self._node)
                return None
        logger.warning("[SATELLITE] %s: Helios services not ready within %.0fs", self._node, budget)
        self._send_error(ERR_FAILED)
        return "wake_timeout" if woken else "service_down"

    async def _transcribe(self, pcm: bytes) -> Optional[str]:
        """WAV → STT /v1/audio/transcriptions. None on failure, "" for no words."""
        if not _settings.stt_url:
            return None
        from orchestrator import shared

        timeout = httpx.Timeout(STT_TIMEOUT_S, connect=5.0)
        client = getattr(shared, "_http", None)
        own = client is None or client.is_closed
        if own:
            client = httpx.AsyncClient(timeout=timeout)
        try:
            resp = await client.post(
                f"{_settings.stt_url.rstrip('/')}/v1/audio/transcriptions",
                files={"file": ("satellite.wav", pcm_to_wav(pcm), "audio/wav")},
                data={"model": "whisper-1"},
                timeout=timeout,
            )
            if resp.status_code != 200:
                logger.warning("[SATELLITE] %s: STT returned %d", self._node, resp.status_code)
                return None
            payload = resp.json()
            words = payload.get("text") if isinstance(payload, dict) else None
            return words.strip() if isinstance(words, str) else ""
        except Exception as e:  # noqa: BLE001
            logger.warning("[SATELLITE] %s: STT failed: %s", self._node, type(e).__name__)
            return None
        finally:
            if own:
                await client.aclose()

    async def _ask_jess(self, transcript: str) -> str:
        """One voice turn through the same brain call as /v1/chat/completions."""
        from orchestrator.cloud_brain import conversation_id_var

        now = time.time()
        if not self._history or now - self._last_turn_at > HISTORY_IDLE_SECONDS:
            self._history = []
            self._conv_started = int(now)
        # Conversation key (auto_learn is skipped for these turns, see _turn_inner).
        conversation_id_var.set(f"satellite:{self._node}:{self._conv_started}")
        messages = [*self._history, {"role": "user", "content": transcript}]
        try:
            response = await asyncio.wait_for(self._chat(messages), timeout=CHAT_TIMEOUT_S)
        except TimeoutError:
            logger.error("[SATELLITE] %s: Jess turn timed out after %.0fs", self._node, CHAT_TIMEOUT_S)
            return ""
        except Exception as e:  # noqa: BLE001
            logger.error("[SATELLITE] %s: Jess turn failed: %s", self._node, type(e).__name__)
            return ""
        status = getattr(response, "status_code", 200)
        if status != 200:
            logger.error("[SATELLITE] %s: Jess turn returned %s", self._node, status)
            return ""
        return reply_text(response)

    def _remember(self, transcript: str, spoken: str) -> None:
        self._history.extend([{"role": "user", "content": transcript}, {"role": "assistant", "content": spoken}])
        if len(self._history) > HISTORY_MAX_MESSAGES:
            del self._history[: len(self._history) - HISTORY_MAX_MESSAGES]
        self._last_turn_at = time.time()

    async def _speak(self, spoken: str) -> str:
        """One play_media on the room's speaker, in the reply's voice style (D1, D3, D4, D6).

        Returns ok | speaker_unavailable | tts_failed.
        """
        from orchestrator.reminder_manager import _announce_voice
        from orchestrator.voice_style import current_reply_style

        try:
            result = await asyncio.wait_for(
                _announce_voice(
                    spoken,
                    speaker=self._speaker,
                    announcement_type="voice_reply",
                    ignore_voice_session=True,
                    ignore_dnd=True,
                    style=current_reply_style(),
                    allow_fallback=False,  # never answer in another room (D10)
                    record_history=False,  # conversation, not an announcement
                    progressive=self._progressive_tts,  # first ~8 s play while the rest is made (D3)
                ),
                timeout=ANNOUNCE_TIMEOUT_S,
            )
        except TimeoutError:
            logger.error("[SATELLITE] %s: reply playback timed out", self._node)
            return "tts_failed"
        except Exception as e:  # noqa: BLE001
            logger.error("[SATELLITE] %s: reply playback failed: %s", self._node, type(e).__name__)
            return "tts_failed"
        if result.get("success") and not result.get("suppressed"):
            return "ok"
        if result.get("unavailable"):
            logger.warning("[SATELLITE] %s: reply not played: room speaker unavailable in HA", self._node)
            return "speaker_unavailable"
        logger.warning("[SATELLITE] %s: reply not played (TTS or HA play_media failed)", self._node)
        return "tts_failed"

    # -- connection ---------------------------------------------------------

    def _new_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        from aioesphomeapi import APIClient

        from orchestrator.config import voice_satellite_key_ok

        # aioesphomeapi treats an empty key as "no encryption": never build one.
        if not voice_satellite_key_ok(self._api_key):
            raise _BadKey()

        return APIClient(
            self._host,
            self._port,
            None,
            client_info="jess-voice-satellite",
            noise_psk=self._api_key,
            expected_name=self._node,
        )

    async def _session(self) -> None:
        """Connect, check the device can stream API audio, subscribe, wait for a drop."""
        from aioesphomeapi import VoiceAssistantFeature

        from orchestrator.metrics import SATELLITE_CONNECTED

        client = self._new_client()
        dropped = asyncio.Event()

        expected_drop = False

        async def _on_stop(expected: bool) -> None:
            nonlocal expected_drop
            expected_drop = expected
            dropped.set()

        unsub: Optional[Callable[[], None]] = None
        try:
            await asyncio.wait_for(client.connect(on_stop=_on_stop, login=True), CONNECT_TIMEOUT_S)
            info = await asyncio.wait_for(client.device_info(), CONNECT_TIMEOUT_S)
            api_version = getattr(client, "api_version", None)
            flags = (
                info.voice_assistant_feature_flags_compat(api_version)
                if api_version is not None
                else getattr(info, "voice_assistant_feature_flags", 0)
            )
            if not flags & VoiceAssistantFeature.API_AUDIO:
                raise _NoApiAudio()
            self._client = client
            unsub = client.subscribe_voice_assistant(
                handle_start=self.handle_start,
                handle_stop=self.handle_stop,
                handle_audio=self.handle_audio,
            )
            SATELLITE_CONNECTED.labels(device=self._node).set(1)
            _count_connect("ok", self._node)
            self._log_connected()
            up_since = time.monotonic()
            await dropped.wait()
            logger.warning(
                "[SATELLITE] %s: connection dropped after %ds (expected=%s)",
                self._node,
                int(time.monotonic() - up_since),
                expected_drop,
            )
        finally:
            SATELLITE_CONNECTED.labels(device=self._node).set(0)
            self._client = None
            run = self._run
            if run is not None and not run.done:
                run.done = True
                self._run = None
                self._release(run)
                if run.watcher is not None:
                    run.watcher.cancel()
                self._count("dropped", run.wake_word)
            if unsub is not None:
                with contextlib.suppress(Exception):
                    unsub()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.disconnect(), 5.0)

    def _log_connected(self) -> None:
        if self._fail_count:
            logger.info(
                "[SATELLITE] %s: connected after %d failed attempt(s) over %ds (last: %s)",
                self._node,
                self._fail_count,
                int(time.monotonic() - self._fail_since),
                self._fail_reason,
            )
        else:
            logger.info("[SATELLITE] %s: connected, listening for runs", self._node)
        self._fail_reason, self._fail_count = "", 0

    def _log_connect_failure(self, reason: str, exc_type: str) -> None:
        """First failure or a new reason: WARNING (ERROR when it needs the owner);
        repeats: DEBUG plus one summary an hour. An unplugged device would
        otherwise add a WARNING every ≤2 min to Loki and the self-audit."""
        now = time.monotonic()
        if reason != self._fail_reason:
            if not self._fail_count:
                self._fail_since = now
            self._fail_reason, self._fail_summary_at = reason, now
            self._fail_count += 1
            level = logging.ERROR if reason in ("bad_key", "wrong_device", "no_api_audio") else logging.WARNING
            logger.log(
                level,
                "[SATELLITE] %s: connect to %s:%d failed (%s: %s); retrying with backoff",
                self._node,
                self._host,
                self._port,
                reason,
                exc_type,
            )
            return
        self._fail_count += 1
        if now - self._fail_summary_at >= FAILURE_SUMMARY_S:
            self._fail_summary_at = now
            logger.info(
                "[SATELLITE] %s: still not connected (%s) — %d attempts over %ds",
                self._node,
                reason,
                self._fail_count,
                int(now - self._fail_since),
            )
        else:
            logger.debug("[SATELLITE] %s: connect failed again (%s)", self._node, reason)

    async def run_forever(self) -> None:
        """Reconnect with backoff; never raises except on cancellation."""
        backoff = BACKOFF_MIN_S
        while True:
            connected_at = time.monotonic()
            try:
                await self._session()
            except asyncio.CancelledError:
                await self._cancel_tasks()
                raise
            except Exception as e:  # noqa: BLE001
                reason = _connect_reason(e)
                _count_connect(reason, self._node)
                self._log_connect_failure(reason, type(e).__name__)
            # A session that stayed up a while earns a quick reconnect.
            if time.monotonic() - connected_at > 60:
                backoff = BACKOFF_MIN_S
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                await self._cancel_tasks()
                raise
            backoff = min(backoff * 2, BACKOFF_MAX_S)

    async def _cancel_tasks(self) -> None:
        """Shutdown: stop in-flight turns/watchers before the HTTP layer goes away."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class _NoApiAudio(Exception):
    """Device firmware can't stream voice audio over the API (needs ESPHome ≥ 2024.x + voice_assistant)."""


class _BadKey(Exception):
    """VOICE_SATELLITE_API_KEY isn't a base64 32-byte key (would mean no encryption)."""


def _connect_reason(exc: BaseException) -> str:
    """Bounded label for a failed connection (bgw_satellite_connect_total)."""
    from aioesphomeapi.core import (
        BadNameAPIError,
        EncryptionHelloAPIError,
        EncryptionPlaintextAPIError,
        HandshakeAPIError,
        InvalidEncryptionKeyAPIError,
        RequiresEncryptionAPIError,
        ResolveAPIError,
        SocketAPIError,
        TimeoutAPIError,
    )

    if isinstance(exc, _NoApiAudio):
        return "no_api_audio"
    # Key/encryption mismatches only (EncryptionErrorAPIError subclasses
    # InvalidEncryptionKeyAPIError). A plain HandshakeAPIError is also raised
    # when the connection resets mid-handshake (device rebooting): unreachable.
    if isinstance(
        exc,
        (
            _BadKey,
            InvalidEncryptionKeyAPIError,
            EncryptionPlaintextAPIError,
            EncryptionHelloAPIError,
            RequiresEncryptionAPIError,
        ),
    ):
        return "bad_key"
    if isinstance(exc, HandshakeAPIError):
        return "unreachable"
    if isinstance(exc, BadNameAPIError):
        return "wrong_device"
    if isinstance(exc, (TimeoutError, TimeoutAPIError)):
        return "timeout"
    if isinstance(exc, (SocketAPIError, ResolveAPIError, OSError)):
        return "unreachable"
    return "error"


def _count_connect(result: str, device: str) -> None:
    from orchestrator.metrics import SATELLITE_CONNECT_TOTAL

    SATELLITE_CONNECT_TOTAL.labels(result=result, device=device).inc()


_CONNECT_RESULTS = ("ok", "timeout", "unreachable", "bad_key", "wrong_device", "no_api_audio", "error")


async def _run_device(sat: Satellite) -> None:
    """One device's reconnect loop. run_forever only ends on cancellation; if it
    ever raised, the other devices must keep running (an exception escaping
    into the TaskGroup would cancel them all), so log it and stop here."""
    try:
        await sat.run_forever()
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — never take the other rooms down
        logger.error("[SATELLITE] %s: device loop exited unexpectedly: %s", sat._node, type(e).__name__)


async def satellite_loop(chat: ChatFn) -> None:
    """Long-running task started from orchestrator startup (like the Telegram
    bot: a forever task, not a scheduler job). Runs every configured device's
    loop side by side; cancelling this task (shutdown) cancels them all and
    waits for each to finish its cleanup (a TaskGroup — a bare gather would
    return when the FIRST device finished, while another room's turn still
    unwinds after shutdown has closed the HTTP client)."""
    from orchestrator.metrics import (
        SATELLITE_CONNECT_TOTAL,
        SATELLITE_CONNECTED,
        SATELLITE_LISTEN_END_TOTAL,
        SATELLITE_TURN_TOTAL,
    )

    devices = _settings.voice_satellites()
    if not devices:
        logger.error("[SATELLITE] No voice satellite device is configured")
        return
    # Series exist from startup, so "never connected" is visible (and alertable).
    for device in devices:
        SATELLITE_CONNECTED.labels(device=device.name).set(0)
        for result in _CONNECT_RESULTS:
            SATELLITE_CONNECT_TOTAL.labels(result=result, device=device.name)
        for reason in LISTEN_END_REASONS:
            SATELLITE_LISTEN_END_TOTAL.labels(reason=reason, device=device.name)
        # The new outcome: its first increase after a restart must be visible.
        SATELLITE_TURN_TOTAL.labels(result="duplicate", wake_word="hey_jess", helios="unchecked", device=device.name)
    if _settings.voice_satellite_progressive_tts:
        from orchestrator import audio_stream

        audio_stream.seed_metrics()
    arbiter = WakeArbiter()
    satellites = [
        Satellite(
            chat=chat,
            host=device.host,
            port=_settings.voice_satellite_port,
            api_key=_settings.voice_satellite_api_key,
            node_name=device.name,
            speaker=device.speaker,
            helios_timeout_s=_settings.voice_satellite_helios_timeout_seconds,
            progressive_tts=_settings.voice_satellite_progressive_tts,
            arbiter=arbiter,
        )
        for device in devices
    ]
    logger.info("[SATELLITE] Bridge started for %s", ", ".join(s._node for s in satellites))
    async with asyncio.TaskGroup() as group:
        for sat in satellites:
            group.create_task(_run_device(sat), name=f"voice-satellite-{sat._node}")


def log_loop_exit(task: asyncio.Task) -> None:
    """Done-callback for the bridge task: an exit that isn't shutdown is a bug."""
    if task.cancelled():
        return
    exc = task.exception()
    logger.error("[SATELLITE] Bridge task exited unexpectedly: %s", type(exc).__name__ if exc else "returned")
