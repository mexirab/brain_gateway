"""Voice match for the voice satellite: is this question in an enrolled voice?

Why: a TV or a game in the room false-wakes a satellite mic ("Hey Jess" is a
small on-device model), the bridge then records the TV's dialogue as the
question, and Jess answers it. With voice match on for a device
(VOICE_MATCH_DEVICES), the bridge scores the captured question against the
enrolled voiceprints right after it stops listening, before STT, the brain or
a Helios wake. Below VOICE_MATCH_THRESHOLD the turn ends silently.

What it is NOT: a security control. It is a TV filter: a recording of an
enrolled voice passes, and a similar voice can. D8 (anyone in the room can
talk to a satellite) stays the security assumption for every room, and the
controls on what a voice turn may do stay where they are (ha_guard,
ha_intent, the voice tool subset). The verdict never reaches the chat call:
never make a tool depend on who is speaking. It also does not help the mic
hear where a question ends over a loud TV (the energy VAD decides that,
before voice match runs).

While it works, a device with voice match answers enrolled voices only:
guests and other household members need their own voiceprint (`enroll`) or a
device without voice match. The owner chose per-room: the TV room on, others
off.

Measured once (2026-11-25, one owner, one TV show, one mic, read-aloud
speech, model below, leave-one-out): owner median 0.67, owner talking over
the TV 0.68, TV alone 0.02, highest 0.30 (on 1.5 s pieces). At the default
threshold 0.35: owner 98-100 % / owner over TV 92-100 % / TV 0 % on 2-3 s of
speech; on 1.5 s pieces 95 % / 84 % / 0 %. Short questions score lower; a TV
voice that sounds like an enrolled one was not tested. Tune with
bgw_satellite_voice_match_score; a turned-away question that is followed by
a recognized one within RETRY_WINDOW_S counts as a likely false reject
(bgw_satellite_voice_match_retry_total).

Model: WeSpeaker ResNet34-LM (VoxCeleb), by the WeNet community,
huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM, CC-BY-4.0, ONNX,
26 MB, pinned by revision and sha256 below. Not baked into the image and not
fetched at run time: the operator downloads it once (`python -m
orchestrator.voice_match fetch`) to VOICE_MATCH_MODEL_PATH; every load hashes
the bytes it loads. Features are the Kaldi fbank WeSpeaker trained on (80
bins, 25/10 ms, hamming window, no dither, int16-scale samples) minus the
per-utterance mean, via kaldi-native-fbank. Runs on the orchestrator's CPU
(~40-80 ms a question) in the matcher's own 2-thread pool, never the event
loop's shared one: a hung call can't starve the rest of the orchestrator.

Voiceprints: VOICE_MATCH_VOICEPRINTS_PATH, JSON, mode 600 in a 700 dir:
  {"version": 1, "model_sha256": "...", "voiceprints": {"<id>": [256 floats]}}
One unit vector per person = the mean embedding of their enrollment clips,
recorded on a satellite (`record`; a phone mic sounds different). The id is a
label the operator picks; it is never logged. A voiceprint is a summary of a
voice, not audio, but still biometric: it stays on this box, is never served
by an API, and the whole voice_match/ directory is left out of the nightly
state backup (scripts/backup_state.py; config refuses a voiceprint path
outside a directory of that name) — after a restore, enroll again. A
voiceprint made with another model is refused (model_sha256), so a model
change means re-enrolling.

Enrollment clips are the one exception to D9 (mic audio never written to
disk): `record` writes raw voice to voice_match/clips/ (mode 600, bare clip
names only, so they stay inside the backup exclusion) and `enroll` deletes
them once the voiceprint is saved unless --keep-clips. Raw voice is more
sensitive than a voiceprint: it is enough to clone a voice.

Failure = fail OPEN: model or voiceprints missing/unreadable, the scoring
errors, times out or finds the pool still busy → the turn goes ahead
unchecked, counted as result=unavailable|failed (quiet alert
VoiceMatchFailing). A broken TV filter must not lock the owner out of the
room's mic. A capture too short to compare (too_short) is a verdict about the
audio, not a fault: it is turned away like an unknown voice.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import sys
import tempfile
import threading
import time
import wave
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

MODEL_REPO = "Wespeaker/wespeaker-voxceleb-resnet34-LM"
MODEL_REVISION = "f0c48c298fd835726c27956a5d617bad7115627e"
MODEL_FILE = "voxceleb_resnet34_LM.onnx"
MODEL_URL = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{MODEL_FILE}"
MODEL_SHA256 = "7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068"
MODEL_MAX_BYTES = 64 * 1024 * 1024
EMBED_DIM = 256
ONNX_THREADS = 2  # keep a scoring run from taking every core on the orchestrator box
# Own worker pool, not the event loop's shared one: a scoring call that hangs
# (stalled disk, wedged model) must not use up the threads the rest of the
# orchestrator runs on. More calls than this in flight -> fail open at once.
MAX_IN_FLIGHT = 2
FETCH_DEADLINE_S = 600  # whole model download

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
LEAD_MS = 200  # audio kept before the VAD's first speech frame
TAIL_MS = 300  # audio kept of the trailing quiet
MIN_SPAN_MS = 1000  # a trimmed span shorter than this is scored as the whole capture
MAX_SCORE_S = 20  # a capture is ≤ 15 s; anything longer is cut
MIN_FBANK_FRAMES = 30  # ~0.3 s: below this there is no voice to compare

VOICEPRINTS_VERSION = 1
VOICEPRINTS_MAX_BYTES = 256 * 1024
VOICEPRINT_ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
MAX_VOICEPRINTS = 8

ENROLL_SEGMENT_S = 3.0
ENROLL_HOP_S = 1.5
ENROLL_SPEECH_RMS = 0.06  # loudest 100 ms of a segment (0-1); a satellite room reads ~0.03-0.045
ENROLL_MIN_CLIPS = 2  # leave-one-out quality check needs two
ENROLL_MIN_SEGMENTS = 8  # ~12 s of speech in all
ENROLL_GOOD_SCORE = 0.5
MAX_WAV_SECONDS = 120

CLIPS_DIR = "clips"  # enrollment recordings, next to the voiceprint file (excluded from backups)

# Fixed reason strings (log-safe) for VoiceMatchUnavailable. too_short is a
# verdict about the audio, not a fault: the bridge treats it as not recognized.
REASONS = (
    "model_missing",
    "model_checksum",
    "model_load",
    "model_output",
    "features_missing",
    "no_voiceprints",
    "voiceprints_invalid",
    "voiceprints_stale",
    "too_short",
    "busy",
)


class VoiceMatchUnavailable(Exception):
    """Voice match can't give a score; `reason` is one of REASONS."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def pcm_to_samples(pcm: bytes) -> np.ndarray:
    """16-bit mono PCM → float32 samples on the int16 scale (what WeSpeaker expects)."""
    usable = len(pcm) - len(pcm) % SAMPLE_WIDTH
    return np.frombuffer(pcm[:usable], dtype="<i2").astype(np.float32)


def fbank(samples: np.ndarray) -> np.ndarray:
    """Kaldi fbank as WeSpeaker computes it, minus the utterance mean (CMN)."""
    try:
        import kaldi_native_fbank as knf
    except ImportError as e:
        raise VoiceMatchUnavailable("features_missing") from e
    opts = knf.FbankOptions()
    opts.frame_opts.samp_freq = SAMPLE_RATE
    opts.frame_opts.frame_length_ms = 25
    opts.frame_opts.frame_shift_ms = 10
    opts.frame_opts.dither = 0.0
    opts.frame_opts.window_type = "hamming"
    opts.mel_opts.num_bins = 80
    opts.energy_floor = 0.0
    fb = knf.OnlineFbank(opts)
    fb.accept_waveform(SAMPLE_RATE, samples.tolist())
    fb.input_finished()
    if fb.num_frames_ready < MIN_FBANK_FRAMES:
        raise VoiceMatchUnavailable("too_short")
    feats = np.stack([fb.get_frame(i) for i in range(fb.num_frames_ready)]).astype(np.float32)
    return feats - feats.mean(axis=0, keepdims=True)


def speech_span(pcm: bytes, speech_at_ms: Optional[int], quiet_ms: int, audio_ms: int) -> bytes:
    """The part of a capture the VAD called speech, with a little margin.

    The capture starts at the device's beep and ends after ~1.2 s of quiet;
    scoring the silence around the question dilutes the voice. A span shorter
    than MIN_SPAN_MS (or no speech start) is scored as the whole capture."""
    if speech_at_ms is None:
        return pcm
    start = max(0, speech_at_ms - LEAD_MS)
    end = min(audio_ms, audio_ms - max(0, quiet_ms - TAIL_MS))
    if end - start < MIN_SPAN_MS:
        return pcm
    per_ms = SAMPLE_RATE * SAMPLE_WIDTH // 1000
    return pcm[start * per_ms : end * per_ms]


def _unit(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    if not math.isfinite(norm) or norm == 0.0:
        raise ValueError("zero or non-finite vector")
    return vec / norm


# -- voiceprint file -------------------------------------------------------------


def load_voiceprints(path: str) -> Dict[str, np.ndarray]:
    """Read and validate the voiceprint file. Raises VoiceMatchUnavailable."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read(VOICEPRINTS_MAX_BYTES + 1)  # bounded: no size-check/read race
        if len(text) > VOICEPRINTS_MAX_BYTES:
            raise VoiceMatchUnavailable("voiceprints_invalid")
        data = json.loads(text)
    except FileNotFoundError as e:
        raise VoiceMatchUnavailable("no_voiceprints") from e
    except (OSError, ValueError, RecursionError) as e:
        raise VoiceMatchUnavailable("voiceprints_invalid") from e
    if not isinstance(data, dict) or data.get("version") != VOICEPRINTS_VERSION:
        raise VoiceMatchUnavailable("voiceprints_invalid")
    if data.get("model_sha256") != MODEL_SHA256:
        raise VoiceMatchUnavailable("voiceprints_stale")
    raw = data.get("voiceprints")
    if not isinstance(raw, dict) or len(raw) > MAX_VOICEPRINTS:
        raise VoiceMatchUnavailable("voiceprints_invalid")
    prints: Dict[str, np.ndarray] = {}
    for pid, values in raw.items():
        if not isinstance(pid, str) or not VOICEPRINT_ID_RE.fullmatch(pid):
            raise VoiceMatchUnavailable("voiceprints_invalid")
        if (
            not isinstance(values, list)
            or len(values) != EMBED_DIM
            or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in values)
        ):
            raise VoiceMatchUnavailable("voiceprints_invalid")
        try:
            vec = np.asarray(values, dtype=np.float32)
        except (OverflowError, ValueError) as e:  # an integer past float range
            raise VoiceMatchUnavailable("voiceprints_invalid") from e
        if vec.shape != (EMBED_DIM,) or not np.all(np.isfinite(vec)):
            raise VoiceMatchUnavailable("voiceprints_invalid")
        try:
            prints[pid] = _unit(vec)
        except ValueError as e:
            raise VoiceMatchUnavailable("voiceprints_invalid") from e
    return prints


def save_voiceprints(path: str, prints: Dict[str, np.ndarray]) -> None:
    """Atomic write (tmp + fsync + rename), file 600, directory 700."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    payload = {
        "version": VOICEPRINTS_VERSION,
        "model_sha256": MODEL_SHA256,
        "voiceprints": {pid: [round(float(x), 6) for x in vec] for pid, vec in sorted(prints.items())},
    }
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".voiceprints-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# -- the matcher -----------------------------------------------------------------


class VoiceMatcher:
    """Scores audio against the enrolled voiceprints. Thread-safe; callers run
    score() in a worker thread (it is CPU work). Loads the model lazily and
    re-reads the voiceprint file whenever it changes on disk."""

    def __init__(self, model_path: str, voiceprints_path: str, threads: int = ONNX_THREADS) -> None:
        self._model_path = model_path
        self._prints_path = voiceprints_path
        self._threads = max(1, threads)
        self._lock = threading.Lock()
        self._session: Any = None
        self._prints: Optional[np.ndarray] = None
        self._prints_key: Optional[Tuple[int, int]] = None
        self._executor = concurrent.futures.ThreadPoolExecutor(MAX_IN_FLIGHT, thread_name_prefix="voice-match")
        # Its own lock, never held across I/O: the event loop takes it (_run),
        # while _lock is held by a worker for a whole model load.
        self._slots_lock = threading.Lock()
        self._in_flight = 0

    def _load_session(self) -> Any:
        with self._lock:
            if self._session is not None:
                return self._session
            if not os.path.isfile(self._model_path):
                raise VoiceMatchUnavailable("model_missing")
            try:
                with open(self._model_path, "rb") as f:
                    blob = f.read(MODEL_MAX_BYTES + 1)
            except OSError as e:
                raise VoiceMatchUnavailable("model_load") from e
            # Hash the bytes that get loaded (no re-open between check and use).
            if len(blob) > MODEL_MAX_BYTES or hashlib.sha256(blob).hexdigest() != MODEL_SHA256:
                raise VoiceMatchUnavailable("model_checksum")
            try:
                import onnxruntime as ort

                opts = ort.SessionOptions()
                opts.intra_op_num_threads = self._threads
                opts.inter_op_num_threads = 1
                # No busy-waiting worker threads between questions.
                opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
                self._session = ort.InferenceSession(blob, sess_options=opts, providers=["CPUExecutionProvider"])
            except Exception as e:  # noqa: BLE001 — any load failure = unavailable, never a crash
                raise VoiceMatchUnavailable("model_load") from e
            return self._session

    def _voiceprints(self) -> np.ndarray:
        try:
            st = os.stat(self._prints_path)
        except FileNotFoundError as e:
            raise VoiceMatchUnavailable("no_voiceprints") from e
        except OSError as e:
            raise VoiceMatchUnavailable("voiceprints_invalid") from e
        key = (st.st_mtime_ns, st.st_size)
        with self._lock:
            if self._prints is not None and self._prints_key == key:
                return self._prints
        prints = load_voiceprints(self._prints_path)
        if not prints:
            raise VoiceMatchUnavailable("no_voiceprints")
        matrix = np.stack(list(prints.values()))
        with self._lock:
            self._prints, self._prints_key = matrix, key
        return matrix

    def embed(self, samples: np.ndarray) -> np.ndarray:
        """Unit-length 256-number embedding of int16-scale samples."""
        feats = fbank(samples)
        session = self._load_session()
        out = session.run(["embs"], {"feats": feats[None]})[0][0]
        try:
            return _unit(np.asarray(out, dtype=np.float32))
        except ValueError as e:
            raise VoiceMatchUnavailable("model_output") from e

    def score(self, pcm: bytes) -> float:
        """Best cosine similarity (−1..1) of this audio to any voiceprint.
        Raises VoiceMatchUnavailable when it can't score."""
        prints = self._voiceprints()  # cheap; checked first so a missing file never runs the model
        samples = pcm_to_samples(pcm)[: MAX_SCORE_S * SAMPLE_RATE]
        emb = self.embed(samples)
        return float(np.max(prints @ emb))

    def preload(self) -> Optional[str]:
        """Load the model and voiceprints now; returns a reason string if not ready."""
        try:
            self._load_session()
            self._voiceprints()
        except VoiceMatchUnavailable as e:
            return e.reason
        return None

    def _release_slot(self, _future: Any = None) -> None:
        with self._slots_lock:
            self._in_flight -= 1

    async def _run(self, fn: Any, *args: Any, timeout: float) -> Any:
        """Run fn on this matcher's own pool; refuse (busy) while MAX_IN_FLIGHT
        earlier calls are still running, e.g. hung past their timeout. The
        slot is released by a done-callback, so it comes back whether the call
        finishes, fails, or is cancelled while still queued."""
        with self._slots_lock:
            if self._in_flight >= MAX_IN_FLIGHT:
                raise VoiceMatchUnavailable("busy")
            self._in_flight += 1
        try:
            work = self._executor.submit(fn, *args)
        except BaseException:
            self._release_slot()
            raise
        work.add_done_callback(self._release_slot)
        # asyncio.timeout, not wait_for: on 3.11 wait_for can return the result and
        # drop a cancel that lands as the call completes (the upkeep loop would
        # then never stop, and shutdown waits for it).
        async with asyncio.timeout(timeout):
            return await asyncio.wrap_future(work)

    async def score_async(self, pcm: bytes, timeout: float) -> float:
        """score() on the matcher's own pool, bounded by `timeout`."""
        return await self._run(self.score, pcm, timeout=timeout)

    async def preload_async(self, timeout: float) -> Optional[str]:
        return await self._run(self.preload, timeout=timeout)


# -- enrollment ------------------------------------------------------------------


def read_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1 or w.getsampwidth() != SAMPLE_WIDTH:
            raise ValueError(f"{os.path.basename(path)}: needs 16 kHz mono 16-bit (record it on the satellite)")
        if w.getnframes() > MAX_WAV_SECONDS * SAMPLE_RATE:
            raise ValueError(f"{os.path.basename(path)}: longer than {MAX_WAV_SECONDS} s")
        return pcm_to_samples(w.readframes(w.getnframes()))


def _has_speech(seg: np.ndarray) -> bool:
    win = SAMPLE_RATE // 10
    loudest = 0.0
    for i in range(0, len(seg) - win + 1, win):
        chunk = seg[i : i + win]
        loudest = max(loudest, float(np.sqrt(np.mean((chunk - chunk.mean()) ** 2))))
    return loudest / 32768 >= ENROLL_SPEECH_RMS


def speech_segments(samples: np.ndarray) -> List[np.ndarray]:
    """Question-sized pieces of a clip that contain speech."""
    seg, hop = int(ENROLL_SEGMENT_S * SAMPLE_RATE), int(ENROLL_HOP_S * SAMPLE_RATE)
    return [samples[s : s + seg] for s in range(0, len(samples) - seg + 1, hop) if _has_speech(samples[s : s + seg])]


def build_voiceprint(matcher: VoiceMatcher, clips: Sequence[np.ndarray]) -> Tuple[np.ndarray, List[float]]:
    """Voiceprint = unit mean of every speech segment's embedding.

    Also returns, per clip, the median score of its segments against a
    voiceprint made from the OTHER clips: a quality check that doesn't
    flatter (a clip always matches a print that contains itself)."""
    if len(clips) < ENROLL_MIN_CLIPS:
        raise ValueError(f"needs at least {ENROLL_MIN_CLIPS} clips")
    per_clip = [[matcher.embed(s) for s in speech_segments(c)] for c in clips]
    total = sum(len(e) for e in per_clip)
    if total < ENROLL_MIN_SEGMENTS or any(not e for e in per_clip):
        raise ValueError(
            f"not enough speech: {total} speech segment(s), needs {ENROLL_MIN_SEGMENTS} and some in every clip"
        )
    voiceprint = _unit(np.mean([e for embs in per_clip for e in embs], axis=0))
    quality = []
    for i, embs in enumerate(per_clip):
        others = _unit(np.mean([e for j, o in enumerate(per_clip) if j != i for e in o], axis=0))
        quality.append(float(np.median([float(e @ others) for e in embs])))
    return voiceprint, quality


# -- recording (enrollment clips) --------------------------------------------------


async def record_clip(host: str, port: int, api_key: str, seconds: float, expected_name: str) -> bytes:
    """Record `seconds` of a satellite's mic as its voice-assistant client.

    The bridge must not hold this device (only one voice subscriber): turn the
    satellite off (VOICE_SATELLITE_ENABLED=false + recreate) first. Starts when
    the device starts a run (its button or the wake word); ends the run with
    RUN_END alone, the only safe end while the device streams."""
    from aioesphomeapi import APIClient, VoiceAssistantEventType

    client = APIClient(host, port, None, client_info="jess-voice-match", noise_psk=api_key, expected_name=expected_name)
    await asyncio.wait_for(client.connect(login=True), 15)
    pcm = bytearray()
    need = int(seconds * SAMPLE_RATE) * SAMPLE_WIDTH
    state = {"run": False, "streaming": False}
    done = asyncio.Event()

    def send(event: str) -> None:
        # The device may be gone; the wait below times out then.
        with contextlib.suppress(Exception):
            client.send_voice_assistant_event(getattr(VoiceAssistantEventType, event), None)

    async def handle_start(*_args: Any) -> int:
        if not state["run"]:
            state["run"] = True
            print("recording...", flush=True)
        return 0

    async def handle_audio(chunk: bytes, _chunk2: Optional[bytes] = None) -> None:
        if not state["run"] or done.is_set() or not chunk:
            return
        if not state["streaming"]:
            state["streaming"] = True
            send("VOICE_ASSISTANT_RUN_START")
            send("VOICE_ASSISTANT_STT_START")
        pcm.extend(chunk)
        if len(pcm) >= need:
            send("VOICE_ASSISTANT_RUN_END")
            done.set()

    async def handle_stop(_abort: bool) -> None:
        if state["run"]:
            done.set()

    unsub = client.subscribe_voice_assistant(
        handle_start=handle_start, handle_stop=handle_stop, handle_audio=handle_audio
    )
    try:
        print(f"ready: press the mic's button (or say the wake word); records {seconds:g} s", flush=True)
        await asyncio.wait_for(done.wait(), 300 + seconds)
    finally:
        await asyncio.sleep(0.5)
        unsub()
        await client.disconnect()
    return bytes(pcm[:need])


def _write_wav(path: str, pcm: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f, wave.open(f, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)


def fetch_model(path: str) -> str:
    """Download the pinned model to `path` (atomic, checksum-verified)."""
    import httpx

    if os.path.isfile(path) and sha256_file(path) == MODEL_SHA256:
        return "already there, checksum ok"
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".model-", suffix=".tmp")
    h, size = hashlib.sha256(), 0
    deadline = time.monotonic() + FETCH_DEADLINE_S
    try:
        with os.fdopen(fd, "wb") as f, httpx.stream("GET", MODEL_URL, follow_redirects=True, timeout=60) as r:
            r.raise_for_status()
            for block in r.iter_bytes():
                size += len(block)
                if size > MODEL_MAX_BYTES:
                    raise ValueError("model download larger than expected")
                if time.monotonic() > deadline:
                    raise TimeoutError(f"model download took longer than {FETCH_DEADLINE_S} s")
                h.update(block)
                f.write(block)
            f.flush()
            os.fsync(f.fileno())
        if h.hexdigest() != MODEL_SHA256:
            raise ValueError("downloaded model checksum mismatch")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return f"downloaded {size} bytes, checksum ok"


# -- CLI ---------------------------------------------------------------------------


def voiceprints_path_ok(path: str) -> bool:
    """Voiceprints (and the clips next to them) must sit in a directory named
    voice_match: scripts/backup_state.py leaves those out of the backup."""
    return os.path.basename(os.path.dirname(os.path.abspath(path))) == "voice_match"


def clips_dir(voiceprints_path: str) -> str:
    """Where `record` puts enrollment clips: next to the voiceprints, so they
    share the voice_match/ backup exclusion. Created 700."""
    path = os.path.join(os.path.dirname(os.path.abspath(voiceprints_path)), CLIPS_DIR)
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


CLIP_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")


def _clip_path(name: str, voiceprints_path: str) -> str:
    """A bare clip name → its file in the clips dir; anything else is refused."""
    stem = name[:-4] if name.endswith(".wav") else name
    if not CLIP_NAME_RE.fullmatch(stem):
        raise ValueError("a clip is named by a bare name (letters, digits, _ or -), not a path")
    return os.path.join(clips_dir(voiceprints_path), stem + ".wav")


def main(argv: Optional[Sequence[str]] = None) -> int:
    import httpx

    from orchestrator.config import settings

    parser = argparse.ArgumentParser(prog="python -m orchestrator.voice_match", description="Voice match tools")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch", help="download the pinned speaker model")
    rec = sub.add_parser("record", help="record an enrollment clip on a satellite (the bridge must be off)")
    rec.add_argument("device", help="the satellite's node name, as in VOICE_SATELLITE_DEVICES")
    rec.add_argument("clip", help="a name for the clip, e.g. me1 (saved in the voice_match clips dir)")
    rec.add_argument("--seconds", type=float, default=20.0)
    enr = sub.add_parser("enroll", help="build (or replace) a voiceprint from clips")
    enr.add_argument("id", help="a label for this person, e.g. owner (never logged)")
    enr.add_argument("clips", nargs="+", help="clip names from `record`")
    enr.add_argument("--keep-clips", action="store_true", help="keep the recordings (default: delete them)")
    rem = sub.add_parser("remove", help="delete a voiceprint")
    rem.add_argument("id")
    sub.add_parser("list", help="list voiceprint ids")
    sco = sub.add_parser("score", help="score clips against the voiceprints")
    sco.add_argument("clips", nargs="+")
    args = parser.parse_args(argv)

    model_path, prints_path = settings.voice_match_model_path, settings.voice_match_voiceprints_path
    if args.cmd in ("record", "enroll") and not voiceprints_path_ok(prints_path):
        print(
            "VOICE_MATCH_VOICEPRINTS_PATH must be in a directory named voice_match (kept out of the "
            "backup); clips and voiceprints are not written anywhere else",
            file=sys.stderr,
        )
        return 2
    try:
        return _run_cli(args, settings, model_path, prints_path)
    except VoiceMatchUnavailable as e:
        print(f"voice match unavailable: {e.reason}", file=sys.stderr)
        return 1
    except TimeoutError:
        hint = " (is the bridge still holding that device? turn it off first)" if args.cmd == "record" else ""
        print(f"timed out{hint}", file=sys.stderr)
        return 1
    except (ValueError, OSError, EOFError, wave.Error, httpx.HTTPError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


def _run_cli(args: argparse.Namespace, settings: Any, model_path: str, prints_path: str) -> int:
    if args.cmd == "fetch":
        print(fetch_model(model_path))
        return 0
    if args.cmd == "record":
        device = next((d for d in settings.voice_satellites() if d.name == args.device), None)
        if device is None or not device.host:
            print("no such device in VOICE_SATELLITE_DEVICES (or VOICE_SATELLITE_HOST/NAME)", file=sys.stderr)
            return 2
        out = _clip_path(args.clip, prints_path)
        if os.path.exists(out):
            print("a clip with that name exists; pick another name", file=sys.stderr)
            return 2
        seconds = max(3.0, min(40.0, args.seconds))
        pcm = asyncio.run(
            record_clip(
                device.host, settings.voice_satellite_port, settings.voice_satellite_api_key, seconds, device.name
            )
        )
        if len(pcm) < 3 * SAMPLE_RATE * SAMPLE_WIDTH:
            print("less than 3 s recorded; nothing saved", file=sys.stderr)
            return 1
        _write_wav(out, pcm)
        print(f"saved clip {os.path.basename(out)} ({len(pcm) / (SAMPLE_RATE * SAMPLE_WIDTH):.1f} s)")
        return 0

    try:
        existing = load_voiceprints(prints_path)
    except VoiceMatchUnavailable as e:
        # Start fresh only when there's nothing usable to lose: no file, or prints
        # from another model. A file that can't be read (permissions, hand-edited)
        # is never overwritten — enrolling one person must not delete the others.
        if e.reason == "no_voiceprints" or (e.reason == "voiceprints_stale" and args.cmd == "enroll"):
            existing = {}
        else:
            print(f"voiceprint file unusable ({e.reason}); fix or remove {prints_path} first", file=sys.stderr)
            return 1
    if args.cmd == "list":
        print("\n".join(sorted(existing)) or "(no voiceprints)")
        return 0
    if args.cmd == "remove":
        if args.id not in existing:
            print("no such voiceprint", file=sys.stderr)
            return 1
        del existing[args.id]
        save_voiceprints(prints_path, existing)
        print(f"removed; {len(existing)} left")
        return 0

    matcher = VoiceMatcher(model_path, prints_path)
    paths = [_clip_path(c, prints_path) for c in args.clips]
    if args.cmd == "score":
        threshold = settings.voice_match_threshold
        for path in paths:
            s = matcher.score(read_wav(path).astype("<i2").tobytes())
            print(f"{os.path.basename(path)}: {s:.2f} {'recognized' if s >= threshold else 'not recognized'}")
        return 0

    # enroll
    if not VOICEPRINT_ID_RE.fullmatch(args.id):
        print("id: lowercase letters, digits, _ or -, up to 32", file=sys.stderr)
        return 2
    if args.id not in existing and len(existing) >= MAX_VOICEPRINTS:
        print(f"already {MAX_VOICEPRINTS} voiceprints; remove one first", file=sys.stderr)
        return 1
    clips = [read_wav(p) for p in paths]
    try:
        voiceprint, quality = build_voiceprint(matcher, clips)
    except ValueError as e:
        print(f"not enrolled: {e}", file=sys.stderr)
        return 1
    existing[args.id] = voiceprint
    save_voiceprints(prints_path, existing)
    print("each clip against a voiceprint from the others (1 = same voice):")
    for path, q in zip(paths, quality, strict=True):
        flag = "" if q >= ENROLL_GOOD_SCORE else "   <- low: re-record this one?"
        print(f"  {os.path.basename(path)}: {q:.2f}{flag}")
    print(f"enrolled; {len(existing)} voiceprint(s)")
    if args.keep_clips:
        print("clips kept: they are raw recordings of a voice — delete them when done")
    else:
        for path in paths:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
        print("clips deleted (raw voice recordings are not kept)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
