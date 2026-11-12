"""Progressive TTS: one speaker URL whose audio is still being made.

A Cast speaker plays one URL. Synthesizing a whole reply before play_media
made the listener wait ~0.4 s + ~16 ms per character (VoxCPM2, measured
2026-11-11: 8.6 s for a 450-character answer) before hearing anything.
Here the reply is split into sentence chunks; once the first START_BUFFER_S
of audio exists the speaker is pointed at a stream URL, and later chunks are
appended to the same open-ended WAV while the speaker plays.

What the speaker does (Nest Hub Max, Default Media Receiver, 2026-11-11):

- WAV with RIFF and data sizes 0xFFFFFFFF, sent chunked, plays while it
  downloads — but only once about 7-8 s of audio is buffered (or the stream
  has ended). 3, 5, 6 and 6.7 s first pieces sat in "buffering" until more
  arrived (once 6.7 s did start, once it didn't); 8 s started at once. It is
  seconds of audio, not bytes: 3 s as 48 kHz stereo (576 KB) stalled too.
  Stream type LIVE changes nothing. So play_media waits for START_BUFFER_S.
- Once playing it runs down to ~0.7 s of buffer before stalling; it resumes
  when enough arrives again (no audio lost, just a pause). VoxCPM2 makes
  audio ~3x faster than it is spoken, so after an 8 s head start the later
  chunks stay ahead.
- Chunked MP3 (with or without the Xing frame, BUFFERED or LIVE) waits for
  the whole download — no gain, so WAV only. A backend that returns anything
  but WAV (ElevenLabs, OpenAI) keeps the one-clip path.
- The speaker fetches once, with "Range: bytes=0-". Every request is answered
  200 from byte 0 (an open-ended stream has no valid 206 for a later offset).
  While it buffers it keeps the connection open.

So a reply under ~8 s of speech gains nothing (ONE_CLIP_MAX_CHARS keeps it on
the one-clip path), and a long reply starts after ~2.5-3.5 s of synthesis
instead of all of it. Other speaker models may buffer differently — measure
(play a stream, poll the HA media_player state) before relying on it.

Outcomes (bgw_tts_progressive_total{result}, counted once per stream, after
the caller reports whether a speaker took play_media):
complete; chunk_failed / format_mismatch / too_long / deadline / stalled /
never_read = the speaker stopped early or never played it although the
announcement reported success ("cut short"); abandoned = the speaker hung up
with audio left (stopped, or another cast replaced it); superseded = a newer
reply on the same speaker; not_played = no speaker accepted play_media or the
announcement failed before it; cancelled = the caller was cancelled (its
timeout) or shutdown; not_wav = the backend can't stream (one clip instead).

Stream URLs (/api/audio/s-<24 url-safe chars>.wav) are tokenless — the
speaker can't send a bearer — so the name is a 144-bit secret, a stream is
gone STREAM_TTL_S after it finishes (whether or not something still reads
it), and at most MAX_READERS read it at once. log_config redacts the token
from access lines. Nothing here logs reply text, only lengths and counts.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import struct
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator, Coroutine, Dict, List, Optional

from starlette.responses import StreamingResponse

if TYPE_CHECKING:
    from orchestrator.tts_backend import TTSBackend

logger = logging.getLogger(__name__)

START_BUFFER_S = 8.0  # audio buffered before play_media (the speaker won't play steadily on less)
ONE_CLIP_MAX_CHARS = 200  # ~11 s of speech: shorter replies would barely pass START_BUFFER_S
FIRST_CHUNK_MIN = 150  # chars (~8 s of speech): whole sentences up to at least this
FIRST_CHUNK_MAX = 240  # a first chunk longer than this is cut at a sentence/clause break
CHUNK_MAX = 320  # later chunks: ~5.5 s to synthesize, ~18 s of speech
MAX_CHUNKS = 40  # more = not a spoken reply; the caller uses one clip

STREAM_TTL_S = 300.0  # a finished stream stays readable this long (the speaker may re-fetch)
MAX_STREAMS = 8
MAX_READERS = 3
MAX_STREAM_BYTES = 24 * 1024 * 1024  # ~8 min of 24 kHz mono 16-bit
PRODUCER_DEADLINE_S = 240.0  # whole reply synthesized within this, or the stream ends there
READ_IDLE_TIMEOUT_S = 30.0  # a reader waiting this long for the next chunk gives up (stalled)
FIRST_READ_TIMEOUT_S = 15.0  # play_media accepted but the speaker never fetched: never_read
ABANDON_GRACE_S = 3.0  # speaker stopped reading mid-stream: stop synthesizing after this
UNDERRUN_HEADROOM_S = 0.7  # the speaker stalls with less than this left to play
READ_CHUNK_BYTES = 64 * 1024

RESULTS = (
    "complete",
    "chunk_failed",
    "format_mismatch",
    "too_long",
    "deadline",
    "stalled",
    "never_read",
    "abandoned",
    "superseded",
    "not_played",
    "cancelled",
    "not_wav",
)
CUT_SHORT = frozenset({"chunk_failed", "format_mismatch", "too_long", "deadline", "stalled", "never_read"})

_SENTENCE_BREAK = re.compile(r"(?<=[.!?…])\s+|(?<=[.!?…][\"'”’)\]])\s+")
_CLAUSE_BREAK = re.compile(r"(?<=[,;:—–])\s+")
_SPACE = re.compile(r"\s+")
_NAME_RE = re.compile(r"s-([A-Za-z0-9_-]{24})\.wav")


# ---------------------------------------------------------------------------
# Text → chunks
# ---------------------------------------------------------------------------


def _breaks(pattern: re.Pattern[str], text: str, lo: int, hi: int) -> List[int]:
    return [m.start() for m in pattern.finditer(text) if lo <= m.start() <= hi]


def _split_at(text: str, at: int) -> tuple[str, str]:
    return text[:at].strip(), text[at:].strip()


def _cut(text: str, limit: int, floor: int) -> tuple[str, str]:
    """Split ``text`` at or before ``limit``: the last sentence break after
    ``floor``, else clause break, else space, else hard at ``limit`` (a run
    with no spaces). Returns (head, rest); rest is "" when the text fits."""
    if len(text) <= limit:
        return text, ""
    for pattern in (_SENTENCE_BREAK, _CLAUSE_BREAK, _SPACE):
        found = _breaks(pattern, text, floor, limit)
        if found:
            return _split_at(text, found[-1])
    return text[:limit], text[limit:].strip()


def _cut_first(text: str) -> tuple[str, str]:
    """The first chunk should be FIRST_CHUNK_MIN..FIRST_CHUNK_MAX chars (about
    START_BUFFER_S of speech) so one synthesis starts the speaker. Any break
    in that range beats a sentence break below it, which would leave the
    speaker short of audio and cost a second synthesis before play."""
    if len(text) <= FIRST_CHUNK_MAX:
        return text, ""
    for pattern in (_SENTENCE_BREAK, _CLAUSE_BREAK, _SPACE):
        found = _breaks(pattern, text, FIRST_CHUNK_MIN, FIRST_CHUNK_MAX)
        if found:
            return _split_at(text, found[-1])
    return _cut(text, FIRST_CHUNK_MAX, FIRST_CHUNK_MIN // 2)


def split_for_tts(text: str) -> List[str]:
    """Chunks to synthesize in order: a first chunk worth ~START_BUFFER_S of
    speech, then groups of whole sentences up to CHUNK_MAX. No chunk is
    longer than CHUNK_MAX. Joined with spaces they are ``text`` with
    whitespace collapsed (nothing dropped, nothing reordered) — except where
    a run of CHUNK_MAX+ characters without a space had to be cut hard."""
    text = " ".join((text or "").split())
    if not text:
        return []
    sentences = [s for s in _SENTENCE_BREAK.split(text) if s]

    first, i = sentences[0], 1
    while len(first) < FIRST_CHUNK_MIN and i < len(sentences):
        first, i = f"{first} {sentences[i]}", i + 1
    first, spill = _cut_first(first)
    rest = ([spill] if spill else []) + sentences[i:]

    chunks = [first]
    cur = ""
    for sentence in rest:
        if cur and len(cur) + 1 + len(sentence) > CHUNK_MAX:
            chunks.append(cur)
            cur = ""
        cur = f"{cur} {sentence}" if cur else sentence
        while len(cur) > CHUNK_MAX:  # one sentence longer than the cap
            head, cur = _cut(cur, CHUNK_MAX, CHUNK_MAX // 3)
            chunks.append(head)
    if cur:
        chunks.append(cur)
    return chunks


# ---------------------------------------------------------------------------
# WAV
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WavPcm:
    channels: int
    rate: int
    bits: int
    pcm: bytes

    @property
    def fmt(self) -> tuple[int, int, int]:
        return (self.channels, self.rate, self.bits)


def parse_wav(data: bytes) -> Optional[WavPcm]:
    """PCM samples + format of a RIFF/WAVE file, or None if it isn't plain PCM."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    pos, fmt = 12, None
    while pos + 8 <= len(data):
        cid = data[pos : pos + 4]
        (size,) = struct.unpack_from("<I", data, pos + 4)
        body = pos + 8
        if cid == b"fmt " and size >= 16 and body + 16 <= len(data):
            tag, channels, rate, _byte_rate, _align, bits = struct.unpack_from("<HHIIHH", data, body)
            if tag != 1 or channels not in (1, 2) or bits not in (16, 24, 32) or not 8000 <= rate <= 96000:
                return None
            fmt = (channels, rate, bits)
        elif cid == b"data":
            if fmt is None:
                return None
            pcm = data[body : min(len(data), body + size)]
            block = fmt[0] * fmt[2] // 8
            pcm = pcm[: len(pcm) - len(pcm) % block]
            return WavPcm(fmt[0], fmt[1], fmt[2], pcm)
        pos = body + size + (size & 1)
    return None


def open_ended_header(channels: int, rate: int, bits: int) -> bytes:
    """44-byte PCM WAV header whose sizes say "keep reading" (0xFFFFFFFF)."""
    block = channels * bits // 8
    return (
        b"RIFF"
        + struct.pack("<I", 0xFFFFFFFF)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * block, block, bits)
        + b"data"
        + struct.pack("<I", 0xFFFFFFFF - 36)
    )


# ---------------------------------------------------------------------------
# Streams
# ---------------------------------------------------------------------------


class AudioStream:
    """A growing WAV in memory; every reader gets it from byte 0."""

    def __init__(self, speaker_key: str, chunks_total: int) -> None:
        self.token = secrets.token_urlsafe(18)
        self.speaker_key = speaker_key
        self.chunks_total = chunks_total
        self.chunks_done = 0
        self.created = time.monotonic()
        self.finished_at: Optional[float] = None
        self.result = ""
        self.readers = 0
        self.first_read_at: Optional[float] = None
        self.min_headroom: Optional[float] = None
        self.producer: Optional[asyncio.Task] = None
        self.byte_rate = 0
        self._cast: Optional[bool] = None  # did a speaker accept play_media? None = not told yet
        self._read_wait_over = False  # FIRST_READ_TIMEOUT_S passed after play_media
        self._reported = False
        self._buf = bytearray()
        self._changed = asyncio.Event()

    @property
    def filename(self) -> str:
        return f"s-{self.token}.wav"

    @property
    def done(self) -> bool:
        return self.finished_at is not None

    @property
    def size(self) -> int:
        return len(self._buf)

    @property
    def audio_seconds(self) -> float:
        return max(0, len(self._buf) - 44) / self.byte_rate if self.byte_rate else 0.0

    def expired(self, now: float) -> bool:
        return self.finished_at is not None and now - self.finished_at >= STREAM_TTL_S

    def headroom(self, now: float) -> Optional[float]:
        """Seconds of audio the speaker still has to play (roughly: it plays
        from its first read; Cast's own start-up delay makes this pessimistic)."""
        if self.first_read_at is None:
            return None
        return self.audio_seconds - (now - self.first_read_at)

    def begin(self, first: WavPcm) -> None:
        """Write the open-ended header and the first chunk's samples."""
        self.byte_rate = first.rate * first.channels * first.bits // 8
        self.append(open_ended_header(*first.fmt) + first.pcm)
        self.chunks_done = 1

    def append(self, data: bytes) -> bool:
        """Add audio; False (and nothing added) once done or over the byte cap."""
        if self.done or len(self._buf) + len(data) > MAX_STREAM_BYTES:
            return False
        now = time.monotonic()
        room = self.headroom(now)
        if room is not None:
            if self.min_headroom is None or room < self.min_headroom:
                self.min_headroom = room
            if room < UNDERRUN_HEADROOM_S:
                _count_underrun()
        self._buf += data
        self._wake()
        return True

    def finish(self, result: str) -> None:
        """Synthesis is over (``result`` says how). Counted once it's also
        known whether a speaker played it (see _maybe_report)."""
        if self.done:
            return
        self.finished_at = time.monotonic()
        self.result = result
        self._wake()
        self._maybe_report()

    def cancel(self, result: str) -> None:
        """Stop synthesizing; what's already in the buffer stays readable."""
        task = self.producer
        if task is not None and not task.done():
            task.cancel()
        self.finish(result)

    def abort(self, result: str) -> None:
        """Nobody will play this (no speaker accepted it, or the announcement
        failed or was cancelled before play_media): stop, and count it as
        ``result`` even if synthesis had already finished."""
        task = self.producer
        if task is not None and not task.done():
            task.cancel()
        if self.finished_at is None:
            self.finished_at = time.monotonic()
        self.result = result
        self._cast = False
        self._wake()
        self._maybe_report()

    def mark_cast(self) -> None:
        """A speaker accepted play_media. Watch that it actually fetches."""
        self._cast = True
        self._maybe_report()
        _spawn(self._watch_first_read())

    def _maybe_report(self) -> None:
        """Count the outcome exactly once, when synthesis is over AND it's
        known whether a speaker played it: abort() (no), or play_media
        accepted and then a first read or FIRST_READ_TIMEOUT_S."""
        if self._reported or not self.done or self._cast is None:
            return
        if self._cast and self.first_read_at is None and not self._read_wait_over:
            return
        self._report()

    def _report(self) -> None:
        self._reported = True
        _count(self.result)
        level = logging.WARNING if self.result in CUT_SHORT else logging.INFO
        logger.log(
            level,
            "[TTS-STREAM] finished %s: %d/%d chunks, %.1fs of audio, %.2fs, reader=%s, min_headroom=%s",
            self.result,
            self.chunks_done,
            self.chunks_total,
            self.audio_seconds,
            (self.finished_at or time.monotonic()) - self.created,
            "yes" if self.first_read_at is not None else "no",
            f"{self.min_headroom:.1f}s" if self.min_headroom is not None else "-",
        )

    def _wake(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        # Check and take the reader slot with no await in between, so
        # concurrent requests can't all pass the cap.
        if self.readers >= MAX_READERS:
            return
        self.readers += 1
        sent = 0
        try:
            while True:
                if sent < len(self._buf):
                    if self.first_read_at is None:
                        self.first_read_at = time.monotonic()
                        self._maybe_report()
                    piece = bytes(self._buf[sent : sent + READ_CHUNK_BYTES])
                    sent += len(piece)
                    yield piece
                    continue
                if self.done:
                    return
                changed = self._changed
                try:
                    await asyncio.wait_for(changed.wait(), READ_IDLE_TIMEOUT_S)
                except TimeoutError:
                    logger.warning("[TTS-STREAM] no new audio for %.0fs; ending the stream", READ_IDLE_TIMEOUT_S)
                    self.cancel("stalled")
                    return
        finally:
            self.readers -= 1
            if not self.done and self.readers == 0:
                _spawn(self._abandon_if_unread())

    async def _abandon_if_unread(self) -> None:
        """The speaker hung up mid-reply (stopped, or another cast replaced it):
        stop burning the single TTS worker on audio nobody will hear."""
        await asyncio.sleep(ABANDON_GRACE_S)
        if not self.done and self.readers == 0:
            room = self.headroom(time.monotonic())
            # Gone after running out of audio = it gave up waiting for us.
            ran_dry = room is not None and room < 1.0
            logger.info("[TTS-STREAM] speaker stopped reading; rest of the reply not synthesized")
            self.cancel("stalled" if ran_dry else "abandoned")

    async def _watch_first_read(self) -> None:
        """HA answers 200 to play_media even when the speaker never fetches."""
        await asyncio.sleep(FIRST_READ_TIMEOUT_S)
        if self.first_read_at is not None:
            return
        self._read_wait_over = True
        logger.warning("[TTS-STREAM] speaker never fetched the stream (%.0fs after play_media)", FIRST_READ_TIMEOUT_S)
        if not self.done:
            self.cancel("never_read")
            return
        if self.result == "complete" or self.result in CUT_SHORT:
            self.result = "never_read"  # the real problem: nothing was played at all
        self._maybe_report()


class ClosingStreamingResponse(StreamingResponse):
    """StreamingResponse that always closes its body iterator. Starlette
    doesn't: when the client hangs up while the send is blocked (a speaker
    that stopped reading), the generator stays parked at ``yield`` until the
    cyclic GC runs — so the reader count, the abandon check and the TTL all
    waited on garbage collection."""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            aclose = getattr(self.body_iterator, "aclose", None)
            if aclose is not None:
                await aclose()


def response(stream: AudioStream) -> StreamingResponse:
    return ClosingStreamingResponse(stream.iter_bytes(), media_type="audio/wav", headers={"Cache-Control": "no-store"})


_STREAMS: Dict[str, AudioStream] = {}
_TASKS: set[asyncio.Task] = set()


def _spawn(coro: Coroutine[Any, Any, None]) -> asyncio.Task:
    task = asyncio.ensure_future(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


def _count(result: str) -> None:
    try:
        from orchestrator.metrics import TTS_PROGRESSIVE_TOTAL

        TTS_PROGRESSIVE_TOTAL.labels(result=result).inc()
    except Exception:  # noqa: BLE001
        logger.debug("[TTS-STREAM] metric update failed", exc_info=True)


def _count_underrun() -> None:
    try:
        from orchestrator.metrics import TTS_PROGRESSIVE_UNDERRUNS_TOTAL

        TTS_PROGRESSIVE_UNDERRUNS_TOTAL.inc()
    except Exception:  # noqa: BLE001
        logger.debug("[TTS-STREAM] metric update failed", exc_info=True)


def seed_metrics() -> None:
    try:
        from orchestrator.metrics import TTS_PROGRESSIVE_TOTAL

        for result in RESULTS:
            TTS_PROGRESSIVE_TOTAL.labels(result=result)
    except Exception:  # noqa: BLE001
        logger.debug("[TTS-STREAM] metric seeding failed", exc_info=True)


def _prune(now: float) -> None:
    # A reader still sending keeps its own reference; dropping the entry only
    # stops new requests, which is what the TTL promises.
    for token, stream in list(_STREAMS.items()):
        if stream.expired(now):
            del _STREAMS[token]
    while len(_STREAMS) >= MAX_STREAMS:
        oldest = min(_STREAMS.values(), key=lambda s: s.created)
        oldest.cancel("superseded")
        del _STREAMS[oldest.token]


def prune_expired() -> None:
    """Drop finished streams past STREAM_TTL_S (the audio_cleanup job; else
    they'd stay in RAM until the next reply)."""
    now = time.monotonic()
    for token, stream in list(_STREAMS.items()):
        if stream.expired(now):
            del _STREAMS[token]


def lookup(filename: str) -> Optional[AudioStream]:
    """The live or recently finished stream behind an /api/audio/ filename."""
    m = _NAME_RE.fullmatch(filename)
    if not m:
        return None
    stream = _STREAMS.get(m.group(1))
    if stream is None:
        return None
    if stream.expired(time.monotonic()):
        _STREAMS.pop(stream.token, None)
        return None
    return stream


def is_stream_name(filename: str) -> bool:
    return bool(_NAME_RE.fullmatch(filename))


def supersede(speaker_key: str) -> None:
    """A new reply is about to play on this speaker: stop synthesizing the
    old one (Cast plays one URL; its remaining chunks would hold the single
    TTS worker while the new reply waits behind them)."""
    for other in list(_STREAMS.values()):
        if other.speaker_key == speaker_key and not other.done:
            other.cancel("superseded")


def shutdown() -> None:
    """Orchestrator shutdown: stop every producer before the HTTP client closes
    (else they'd fail on the closed client and log a false chunk_failed)."""
    for stream in list(_STREAMS.values()):
        if not stream.done:
            stream.cancel("cancelled")


async def start(backend: TTSBackend, text: str, *, style: str, speaker_key: str) -> Optional[AudioStream]:
    """Synthesize the first START_BUFFER_S of the reply and return a stream
    that keeps filling in the background, or None when the one-clip path
    should be used instead (a short reply, a single chunk, or a backend that
    doesn't return WAV). Raises what the first synthesis raises, like the
    one-clip path would. The caller must then call ``mark_cast()`` or
    ``abort()`` on the stream."""
    supersede(speaker_key)  # also for a short reply: "stop" → "Okay." must not wait behind the old one
    if getattr(backend, "audio_format", "") != "audio/wav":
        _count("not_wav")
        return None
    if len(text) <= ONE_CLIP_MAX_CHARS:
        return None
    chunks = split_for_tts(text)
    if not 2 <= len(chunks) <= MAX_CHUNKS:
        return None

    t0 = time.monotonic()
    first = parse_wav(await _synthesize(backend, chunks[0], style))
    if first is None:
        _count("not_wav")
        logger.warning("[TTS-STREAM] TTS reply isn't plain PCM WAV; using one clip")
        return None
    stream = AudioStream(speaker_key, len(chunks))
    stream.begin(first)
    # The speaker only plays steadily once ~START_BUFFER_S is buffered, so
    # starting it earlier would just leave it "buffering" (or blip and stall).
    n = 1
    try:
        while n < len(chunks) and stream.audio_seconds < START_BUFFER_S:
            failure = await _add_chunk(stream, backend, chunks, n, first.fmt, style)
            n += 1
            if failure:
                stream.finish(failure)  # play what there is
                break
    except asyncio.CancelledError:
        # The caller never gets this stream to abort() it: count it here.
        stream.abort("cancelled")
        raise
    if not stream.done and n >= len(chunks):
        stream.finish("complete")

    _prune(time.monotonic())
    _STREAMS[stream.token] = stream
    if not stream.done:
        stream.producer = _spawn(_produce(stream, backend, chunks, n, first.fmt, style, t0))
    logger.info(
        "[TTS-STREAM] started: %d chunks, %d chars; speaker starts on %d chunk(s) = %.1fs of audio after %.2fs",
        len(chunks),
        sum(len(c) for c in chunks),
        n,
        stream.audio_seconds,
        time.monotonic() - t0,
    )
    return stream


def _chunk_timeout(chunk: str) -> float:
    """~3x the expected synthesis time (0.4 s + 16 ms/char), at least 10 s: a
    hung TTS fails this chunk instead of leaving the speaker waiting."""
    return max(10.0, 3 * (0.4 + 0.016 * len(chunk)))


async def _synthesize(backend: TTSBackend, chunk: str, style: str) -> bytes:
    return await asyncio.wait_for(backend.synthesize(chunk, style=style), _chunk_timeout(chunk))


async def _add_chunk(
    stream: AudioStream, backend: TTSBackend, chunks: List[str], n: int, fmt: tuple[int, int, int], style: str
) -> Optional[str]:
    """Synthesize chunks[n] onto the stream. None = added; else the result
    the stream should finish with."""
    try:
        audio = parse_wav(await _synthesize(backend, chunks[n], style))
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        status = getattr(getattr(e, "response", None), "status_code", None)
        logger.warning(
            "[TTS-STREAM] chunk %d/%d failed: %s%s",
            n + 1,
            len(chunks),
            type(e).__name__,
            f" {status}" if status else "",
        )
        return "chunk_failed"
    if audio is None or audio.fmt != fmt:
        logger.warning("[TTS-STREAM] chunk %d/%d has a different audio format; stopping", n + 1, len(chunks))
        return "format_mismatch"
    if stream.done:
        return stream.result
    if not stream.append(audio.pcm):
        logger.warning("[TTS-STREAM] reply hit the %d-byte cap; stopping", MAX_STREAM_BYTES)
        return "too_long"
    stream.chunks_done = n + 1
    return None


async def _produce(
    stream: AudioStream,
    backend: TTSBackend,
    chunks: List[str],
    first_n: int,
    fmt: tuple[int, int, int],
    style: str,
    t0: float,
) -> None:
    result = "chunk_failed"  # anything unexpected = the reply was cut short
    try:
        async with asyncio.timeout(max(1.0, PRODUCER_DEADLINE_S - (time.monotonic() - t0))):
            for n in range(first_n, len(chunks)):
                if stream.done:
                    return
                failure = await _add_chunk(stream, backend, chunks, n, fmt, style)
                if failure:
                    result = failure
                    return
        result = "complete"
    except TimeoutError:
        logger.warning("[TTS-STREAM] reply not finished within %.0fs; stopping", PRODUCER_DEADLINE_S)
        result = "deadline"
    except asyncio.CancelledError:
        result = "cancelled"  # shutdown; cancel()/abort() have already finished the stream otherwise
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("[TTS-STREAM] producer failed: %s", type(e).__name__)
    finally:
        stream.finish(result)
