"""Tests for audio_stream.py — progressive TTS replies on a Cast speaker.

Covers:
- split_for_tts: nothing dropped or reordered, chunk size caps, first chunk
  sized for START_BUFFER_S, hard cuts of space-less runs, decimals intact;
- parse_wav / open_ended_header: plain PCM only, odd bytes trimmed, extra
  chunks skipped, open-ended sizes;
- start(): one-clip fallbacks (not WAV, short, one chunk, too many), the
  pre-roll up to START_BUFFER_S, first-synthesis errors, supersede;
- the outcome counter bgw_tts_progressive_total: exactly once per stream,
  only once synthesis is over AND the cast outcome is known;
- iter_bytes: from byte 0, concurrent readers, MAX_READERS, TTL, _prune;
- GET /api/audio/{name}: 404 / 429 / streamed bytes / ordinary files, and a
  speaker hang-up through the app's real BaseHTTPMiddleware stack;
- ClosingStreamingResponse closes the body iterator when the send fails.

No real TTS or network: a fake backend returns WAVs of a chosen duration.
Timeouts are patched to tens of milliseconds.
"""

from __future__ import annotations

import asyncio
import os
import random
import struct

import pytest

from orchestrator import audio_stream as a_s
from orchestrator.audio_stream import (
    CHUNK_MAX,
    FIRST_CHUNK_MAX,
    FIRST_CHUNK_MIN,
    MAX_READERS,
    MAX_STREAMS,
    ONE_CLIP_MAX_CHARS,
    STREAM_TTL_S,
    AudioStream,
    open_ended_header,
    parse_wav,
    split_for_tts,
)

RATE = 8000  # mono 16-bit: 16 000 bytes per second of audio (small buffers, fast tests)
BYTES_PER_S = RATE * 2
TOKEN = "Ab3_-x9Kq2LmN0pQr7StUv8w"  # 24 url-safe chars, the shape of secrets.token_urlsafe(18)
LONG = (
    "The library opens at nine in the morning and closes at six in the evening. "
    "Bring a light jacket, since the evening can turn cool near the water. "
    "Prices went up by 3.5 percent over the last quarter, mostly in transport. "
    "The recipe needs two cups of flour, one egg, a pinch of salt, and some patience."
)
assert len(LONG) > ONE_CLIP_MAX_CHARS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_wav(pcm: bytes, *, channels=1, rate=RATE, bits=16, tag=1, extra=b"", data_size=None, fmt_size=16) -> bytes:
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


def wav_bytes(seconds: float, *, fill: int = 1, rate=RATE, channels=1, bits=16) -> bytes:
    block = channels * bits // 8
    n = int(round(seconds * rate)) * block
    return build_wav(bytes([fill % 256]) * n, channels=channels, rate=rate, bits=bits)


class WavBackend:
    """Fake TTS: call i returns ``plan[i]`` (seconds of WAV audio | bytes |
    an exception to raise | "hang"), default ``seconds``. Calls from
    ``gate_from`` on wait for ``gate`` first. Audio of call i is filled with
    byte i+1, so order is checkable."""

    audio_format = "audio/wav"
    file_extension = "wav"

    def __init__(self, seconds: float = 9.0, plan: dict | None = None, gate_from: int | None = None):
        self.seconds = seconds
        self.plan = dict(plan or {})
        self.gate_from = gate_from
        self.gate = asyncio.Event()
        self.calls: list[str] = []
        self.styles: list[str] = []

    async def synthesize(self, text: str, voice: str | None = None, style: str = "") -> bytes:
        i = len(self.calls)
        self.calls.append(text)
        self.styles.append(style)
        if self.gate_from is not None and i >= self.gate_from:
            await self.gate.wait()
        step = self.plan.get(i, self.seconds)
        if isinstance(step, str) and step == "hang":
            await asyncio.Event().wait()
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, bytes):
            return step
        return wav_bytes(step, fill=i + 1)


class Mp3Backend(WavBackend):
    audio_format = "audio/mpeg"
    file_extension = "mp3"


def fixed_chunks(monkeypatch, n: int) -> list[str]:
    chunks = [f"Chunk number {i} of the reply." for i in range(n)]
    monkeypatch.setattr(a_s, "split_for_tts", lambda text: list(chunks))
    return chunks


async def until(cond, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        assert loop.time() < end, "condition not met in time"
        await asyncio.sleep(0.005)


async def read_all(stream: AudioStream) -> bytes:
    out = bytearray()
    async for piece in stream.iter_bytes():
        out += piece
    return bytes(out)


async def read_first_then_hang_up(stream: AudioStream) -> bytes:
    gen = stream.iter_bytes()
    first = await gen.__anext__()
    await gen.aclose()
    return first


async def settle(stream: AudioStream) -> None:
    """Let a cancelled/finished producer unwind."""
    if stream.producer is not None:
        await asyncio.gather(stream.producer, return_exceptions=True)


@pytest.fixture(autouse=True)
async def _isolate(monkeypatch):
    """Empty stream table per test; cancel every background task after it.
    (Depends on monkeypatch so a patched _count is still active while the
    cancelled producers unwind.)"""
    a_s._STREAMS.clear()
    yield
    tasks = list(a_s._TASKS)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    a_s._STREAMS.clear()


@pytest.fixture
def counts(monkeypatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(a_s, "_count", seen.append)
    return seen


# ---------------------------------------------------------------------------
# split_for_tts
# ---------------------------------------------------------------------------


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _assert_invariants(text: str, chunks: list[str]) -> None:
    collapsed = _collapse(text)
    assert all(chunks), "empty chunk"
    assert all(c == c.strip() and "  " not in c for c in chunks)
    assert " ".join(chunks) == collapsed, "text dropped, reordered or re-spaced"
    assert all(len(c) <= CHUNK_MAX for c in chunks)
    # The first chunk is whole sentences until FIRST_CHUNK_MIN, cut back to
    # FIRST_CHUNK_MAX — so a text under FIRST_CHUNK_MAX can still be two chunks.
    if len(collapsed) < FIRST_CHUNK_MIN:
        assert chunks == [collapsed]
    else:
        assert FIRST_CHUNK_MIN <= len(chunks[0]) <= FIRST_CHUNK_MAX, len(chunks[0])


PROSE = [
    "The library opens at nine in the morning and closes at six.",
    "Bring a light jacket, since the evening can turn cool near the water.",
    "Prices went up by 3.5 percent over the last quarter, mostly in transport.",
    "That's a good question!",
    "Do you want the short version or the long one?",
    "Wait… let me check that again.",
    'She said "the train leaves at noon." Then she left.',
    "Okay.",
    "The recipe needs two cups of flour, one egg, a pinch of salt, and some patience; the rest is simple.",
    "(This part is optional.)",
    "Version 2.10 replaced version 2.9 last spring: nothing else changed.",
]


@pytest.mark.parametrize("n_sentences", [1, 3, 4, 6, 11, 22, 40], ids=lambda n: f"{n}-sentences")
def test_split_prose_invariants(n_sentences):
    text = "  \n".join(PROSE[i % len(PROSE)] for i in range(n_sentences))
    chunks = split_for_tts(text)
    _assert_invariants(text, chunks)
    # Later chunks are groups of whole sentences (no sentence here exceeds CHUNK_MAX).
    for chunk in chunks[1:]:
        assert chunk[-1] in '.!?…")', chunk[-12:]


def test_split_random_prose_invariants():
    rng = random.Random(20261111)
    words = [
        "the",
        "a",
        "light",
        "jacket",
        "water",
        "train",
        "morning",
        "flour",
        "egg",
        "salt",
        "route",
        "answer",
        "short",
        "long",
        "check",
        "list",
        "3.5",
        "1,200",
        "noon",
    ]
    ends = [".", "!", "?", "…", '."', ".)", "?'"]
    for trial in range(400):
        sentences = []
        for _ in range(rng.randint(1, 14)):
            n = rng.choice([1, 2, 5, 12, 25, 40, 70])
            ws = [rng.choice(words) for _ in range(n)]
            for j in range(2, n, rng.randint(3, 9)):
                ws[j] += rng.choice([",", ";", ":", " —"])
            sentences.append(" ".join(ws) + rng.choice(ends))
        text = "".join(s + rng.choice([" ", "  ", "\n", "\t ", " \n\n"]) for s in sentences)
        chunks = split_for_tts(text)
        try:
            _assert_invariants(text, chunks)
        except AssertionError as e:
            raise AssertionError(f"trial {trial}: {e}") from None


@pytest.mark.parametrize("text", ["", "   ", "\n\t "], ids=["empty", "spaces", "whitespace"])
def test_split_blank_is_empty(text):
    assert split_for_tts(text) == []


def test_split_text_under_first_chunk_max_can_be_two_chunks():
    """Whole sentences up to FIRST_CHUNK_MIN, the rest after — even when the
    whole text would fit FIRST_CHUNK_MAX (start() then streams 2 chunks)."""
    text = " ".join(PROSE[:4])
    assert FIRST_CHUNK_MIN < len(text) <= FIRST_CHUNK_MAX
    assert split_for_tts(text) == [" ".join(PROSE[:3]), PROSE[3]]


def test_split_none_is_empty():
    assert split_for_tts(None) == []  # type: ignore[arg-type]


def test_first_chunk_keeps_whole_sentences_when_they_fit():
    s1 = "The first sentence is a plain one that runs for a little while before it stops here."
    s2 = "The second sentence follows it and also runs a while, then stops right here."
    rest = " ".join(PROSE[:6])
    chunks = split_for_tts(f"{s1} {s2} {rest}")
    assert chunks[0] == f"{s1} {s2}"


def test_short_first_sentence_then_long_one_cuts_at_clause_in_range():
    s1 = "Sure, here it is."
    s2 = ", ".join(f"part {i} of the answer keeps going along the path" for i in range(9)) + "."
    text = f"{s1} {s2} {PROSE[0]}"
    chunks = split_for_tts(text)
    _assert_invariants(text, chunks)
    assert chunks[0] != s1, "cut at the sentence break below FIRST_CHUNK_MIN"
    assert FIRST_CHUNK_MIN <= len(chunks[0]) <= FIRST_CHUNK_MAX
    assert chunks[0].endswith(","), "a clause break in range should win over a plain space"


def test_short_first_sentence_then_long_one_without_commas_cuts_at_space():
    s1 = "Sure thing."
    s2 = " ".join(["word"] * 90) + "."
    text = f"{s1} {s2}"
    chunks = split_for_tts(text)
    _assert_invariants(text, chunks)
    assert FIRST_CHUNK_MIN <= len(chunks[0]) <= FIRST_CHUNK_MAX
    assert text[len(chunks[0])] == " ", "cut must land on a space"
    assert not chunks[0].endswith(".")


def test_space_less_run_is_hard_cut():
    text = "x" * 1000
    chunks = split_for_tts(text)
    assert all(chunks) and all(len(c) <= CHUNK_MAX for c in chunks)
    assert len(chunks[0]) <= FIRST_CHUNK_MAX
    assert "".join(chunks) == text


def test_space_less_run_inside_prose_is_hard_cut():
    text = "Here is the code you asked for. " + "y" * 700 + " That is all of it."
    chunks = split_for_tts(text)
    assert all(chunks) and all(len(c) <= CHUNK_MAX for c in chunks)
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")


def test_decimal_point_is_not_a_sentence_break():
    text = " ".join(f"Step {i} raised the level by 3.5 points and then held it there for a while." for i in range(12))
    chunks = split_for_tts(text)
    _assert_invariants(text, chunks)
    assert not any(c.endswith("3.") or c.startswith("5 ") for c in chunks)
    assert sum(c.count("3.5") for c in chunks) == 12


def test_satellite_sized_reply_fits_max_chunks():
    text = " ".join(PROSE[i % len(PROSE)] for i in range(30))[:1200]
    assert 2 <= len(split_for_tts(text)) <= a_s.MAX_CHUNKS


# ---------------------------------------------------------------------------
# parse_wav / open_ended_header
# ---------------------------------------------------------------------------


def test_parse_valid_pcm():
    pcm = bytes(range(200)) * 10
    w = parse_wav(build_wav(pcm, channels=2, rate=24000, bits=16))
    assert w is not None
    assert w.fmt == (2, 24000, 16)
    assert w.pcm == pcm


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"RIFF", id="riff-only"),
        pytest.param(b"hello world, not audio at all", id="garbage"),
        pytest.param(b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb" * 50, id="mp3"),
        pytest.param(wav_bytes(0.1)[:20], id="cut-in-fmt"),
        pytest.param(wav_bytes(0.1)[:40], id="cut-before-data"),
        pytest.param(b"RIFX" + wav_bytes(0.1)[4:], id="big-endian"),
        pytest.param(build_wav(b"\x00" * 64, tag=3, bits=32), id="float"),
        pytest.param(build_wav(b"\x00" * 64, tag=0xFFFE), id="extensible"),
        pytest.param(build_wav(b"\x00" * 64, bits=8), id="8-bit"),
        pytest.param(build_wav(b"\x00" * 64, channels=6), id="6-channels"),
        pytest.param(build_wav(b"\x00" * 64, rate=4000), id="rate-too-low"),
    ],
)
def test_parse_rejects(data):
    assert parse_wav(data) is None


def test_parse_data_before_fmt_rejected():
    good = wav_bytes(0.01)
    # RIFF WAVE, then data chunk, then fmt chunk
    fmt_chunk = good[12:36]
    data_chunk = good[36:]
    assert parse_wav(good[:12] + data_chunk + fmt_chunk) is None


@pytest.mark.parametrize(
    "channels, pcm_len, expected",
    [(1, 1001, 1000), (2, 1003, 1000), (1, 1, 0)],
    ids=["mono-odd", "stereo-partial-frame", "one-byte"],
)
def test_parse_trims_to_block_align(channels, pcm_len, expected):
    w = parse_wav(build_wav(b"\x01" * pcm_len, channels=channels))
    assert w is not None and len(w.pcm) == expected


def test_parse_truncated_data_is_partial_and_aligned():
    full = wav_bytes(0.1)
    w = parse_wav(full[:-3])  # header says more than is there
    assert w is not None and len(w.pcm) == len(full) - 44 - 4


def test_parse_skips_list_chunk_with_pad_byte():
    pcm = b"\x02\x03" * 100
    extra = b"LIST" + struct.pack("<I", 5) + b"INFOx" + b"\x00"  # odd size + pad
    w = parse_wav(build_wav(pcm, extra=extra))
    assert w is not None and w.pcm == pcm


def test_parse_fmt_chunk_of_18_bytes():
    pcm = b"\x04\x05" * 50
    w = parse_wav(build_wav(pcm, fmt_size=18))
    assert w is not None and w.pcm == pcm and w.fmt == (1, RATE, 16)


def test_parse_open_ended_data_size():
    pcm = b"\x06\x07" * 300 + b"\x08"  # odd tail is trimmed
    w = parse_wav(build_wav(pcm, data_size=0xFFFFFFFF))
    assert w is not None and w.pcm == pcm[:-1]


@pytest.mark.parametrize("channels, rate, bits", [(1, 24000, 16), (2, 48000, 16), (1, 16000, 24), (2, 44100, 32)])
def test_open_ended_header_round_trip(channels, rate, bits):
    header = open_ended_header(channels, rate, bits)
    assert len(header) == 44
    assert struct.unpack_from("<I", header, 4)[0] == 0xFFFFFFFF
    block = channels * bits // 8
    assert struct.unpack_from("<IH", header, 28) == (rate * block, block)
    pcm = bytes(range(256)) * block * 3
    w = parse_wav(header + pcm)
    assert w is not None
    assert w.fmt == (channels, rate, bits)
    assert w.pcm == pcm


# ---------------------------------------------------------------------------
# start(): one-clip fallbacks and the pre-roll
# ---------------------------------------------------------------------------


async def test_start_non_wav_backend_counts_not_wav(counts):
    backend = Mp3Backend()
    assert await a_s.start(backend, LONG, style="", speaker_key="k") is None
    assert counts == ["not_wav"]
    assert backend.calls == []


@pytest.mark.parametrize("length", [10, ONE_CLIP_MAX_CHARS], ids=["short", "at-limit"])
async def test_start_short_text_is_one_clip(counts, length):
    backend = WavBackend()
    assert await a_s.start(backend, "a" * length, style="", speaker_key="k") is None
    assert counts == [] and backend.calls == []


async def test_start_single_chunk_is_one_clip(counts):
    text = ("Just one sentence that is longer than the one clip limit but shorter than the first chunk cap " * 3)[
        :FIRST_CHUNK_MAX
    ].strip()
    assert len(text) > ONE_CLIP_MAX_CHARS and len(split_for_tts(text)) == 1
    backend = WavBackend()
    assert await a_s.start(backend, text, style="", speaker_key="k") is None
    assert counts == [] and backend.calls == []


async def test_start_too_many_chunks_is_one_clip(monkeypatch, counts):
    fixed_chunks(monkeypatch, a_s.MAX_CHUNKS + 1)
    backend = WavBackend()
    assert await a_s.start(backend, LONG, style="", speaker_key="k") is None
    assert counts == [] and backend.calls == []


async def test_start_first_chunk_not_pcm_counts_not_wav(monkeypatch, counts):
    fixed_chunks(monkeypatch, 3)
    backend = WavBackend(plan={0: b"ID3 not really a wav"})
    assert await a_s.start(backend, LONG, style="", speaker_key="k") is None
    assert counts == ["not_wav"]
    assert a_s._STREAMS == {}


async def test_start_preroll_reaches_start_buffer_then_produces_rest():
    text = " ".join(PROSE[i % len(PROSE)] for i in range(24))
    n_chunks = len(split_for_tts(text))
    assert n_chunks >= 5
    backend = WavBackend(seconds=3.0, gate_from=3)  # 3 s each: 3 chunks = 9 s >= 8 s
    stream = await a_s.start(backend, text, style="calm", speaker_key="k")
    assert stream is not None
    assert len(backend.calls) == 3
    assert stream.chunks_done == 3 and stream.chunks_total == n_chunks
    assert stream.audio_seconds == pytest.approx(9.0)
    assert not stream.done and stream.producer is not None
    assert a_s.lookup(stream.filename) is stream

    backend.gate.set()
    await stream.producer
    assert stream.done and stream.result == "complete"
    assert stream.chunks_done == n_chunks
    assert backend.styles == ["calm"] * n_chunks
    assert backend.calls == split_for_tts(text)
    # PCM in chunk order, after one open-ended header
    w = parse_wav(bytes(stream._buf))
    assert w is not None
    assert w.pcm == b"".join(bytes([i + 1]) * 3 * BYTES_PER_S for i in range(n_chunks))


async def test_start_long_first_chunk_needs_no_preroll(monkeypatch):
    fixed_chunks(monkeypatch, 3)
    backend = WavBackend(seconds=9.0, gate_from=1)
    stream = await a_s.start(backend, LONG, style="", speaker_key="k")
    assert stream is not None and len(backend.calls) == 1 and stream.chunks_done == 1


async def test_start_preroll_can_finish_everything(monkeypatch, counts):
    fixed_chunks(monkeypatch, 2)
    stream = await a_s.start(WavBackend(seconds=3.0), LONG, style="", speaker_key="k")
    assert stream is not None and stream.done and stream.result == "complete"
    assert stream.producer is None and stream.chunks_done == 2
    assert counts == []  # cast outcome not known yet


async def test_start_first_synthesis_error_propagates(monkeypatch, counts):
    fixed_chunks(monkeypatch, 3)
    backend = WavBackend(plan={0: RuntimeError("tts down")})
    with pytest.raises(RuntimeError):
        await a_s.start(backend, LONG, style="", speaker_key="k")
    assert a_s._STREAMS == {} and counts == []


async def test_start_first_synthesis_hang_times_out(monkeypatch, counts):
    fixed_chunks(monkeypatch, 3)
    monkeypatch.setattr(a_s, "_chunk_timeout", lambda chunk: 0.05)
    with pytest.raises(TimeoutError):
        await a_s.start(WavBackend(plan={0: "hang"}), LONG, style="", speaker_key="k")
    assert a_s._STREAMS == {}


async def test_start_supersedes_live_stream_on_same_speaker(monkeypatch, counts):
    fixed_chunks(monkeypatch, 4)
    old_backend = WavBackend(gate_from=1)
    old = await a_s.start(old_backend, LONG, style="", speaker_key="media_player.a")
    other = await a_s.start(WavBackend(gate_from=1), LONG, style="", speaker_key="media_player.b")
    old.mark_cast()
    await read_first_then_hang_up(old)

    new = await a_s.start(WavBackend(gate_from=1), LONG, style="", speaker_key="media_player.a")
    assert new is not None and new is not old
    assert old.done and old.result == "superseded"
    await settle(old)
    assert old.producer.cancelled()
    assert counts == ["superseded"]
    assert not other.done, "another speaker's stream is left alone"


async def test_short_reply_still_supersedes(monkeypatch, counts):
    fixed_chunks(monkeypatch, 4)
    old = await a_s.start(WavBackend(gate_from=1), LONG, style="", speaker_key="k")
    assert await a_s.start(WavBackend(), "Okay.", style="", speaker_key="k") is None
    assert old.done and old.result == "superseded"


async def test_non_wav_backend_still_supersedes(monkeypatch, counts):
    fixed_chunks(monkeypatch, 4)
    old = await a_s.start(WavBackend(gate_from=1), LONG, style="", speaker_key="k")
    assert await a_s.start(Mp3Backend(), LONG, style="", speaker_key="k") is None
    assert old.result == "superseded"


# ---------------------------------------------------------------------------
# Outcome counting: exactly once, after synthesis AND the cast outcome
# ---------------------------------------------------------------------------


async def _live(monkeypatch, n=3, **backend_kw) -> tuple[AudioStream, WavBackend]:
    fixed_chunks(monkeypatch, n)
    backend = WavBackend(**({"gate_from": 1} | backend_kw))
    stream = await a_s.start(backend, LONG, style="", speaker_key="k")
    assert stream is not None and not stream.done
    return stream, backend


async def test_complete_after_cast_read_and_synthesis(monkeypatch, counts):
    stream, backend = await _live(monkeypatch)
    stream.mark_cast()
    reader = asyncio.create_task(read_all(stream))
    await until(lambda: stream.first_read_at is not None)
    assert counts == []  # still synthesizing
    backend.gate.set()
    await stream.producer
    data = await reader
    assert counts == ["complete"]
    assert data == bytes(stream._buf)


async def test_complete_when_synthesis_ends_before_cast(monkeypatch, counts):
    stream, backend = await _live(monkeypatch)
    backend.gate.set()
    await stream.producer
    assert stream.result == "complete" and counts == []
    stream.mark_cast()
    assert counts == []  # waiting for the speaker to fetch
    await read_all(stream)
    assert counts == ["complete"]


async def test_complete_when_preroll_did_everything(monkeypatch, counts):
    fixed_chunks(monkeypatch, 2)
    stream = await a_s.start(WavBackend(seconds=3.0), LONG, style="", speaker_key="k")
    stream.mark_cast()
    assert counts == []
    await read_first_then_hang_up(stream)
    assert counts == ["complete"]


async def test_speaker_fetching_before_play_media_returns(monkeypatch, counts):
    """The speaker can fetch before HA answers play_media: counted at mark_cast."""
    fixed_chunks(monkeypatch, 2)
    stream = await a_s.start(WavBackend(seconds=3.0), LONG, style="", speaker_key="k")
    await read_all(stream)
    assert counts == []
    stream.mark_cast()
    assert counts == ["complete"]


async def test_not_played_via_abort_stops_producer(monkeypatch, counts):
    stream, _ = await _live(monkeypatch)
    stream.abort("not_played")
    assert counts == ["not_played"]
    await settle(stream)
    assert stream.producer.cancelled()
    assert stream.result == "not_played" and counts == ["not_played"]


async def test_not_played_after_synthesis_complete(monkeypatch, counts):
    fixed_chunks(monkeypatch, 2)
    stream = await a_s.start(WavBackend(seconds=3.0), LONG, style="", speaker_key="k")
    assert stream.result == "complete"
    stream.abort("not_played")
    assert counts == ["not_played"]


async def test_cancelled_via_abort(monkeypatch, counts):
    stream, _ = await _live(monkeypatch)
    stream.abort("cancelled")
    await settle(stream)
    assert counts == ["cancelled"]


async def test_shutdown_while_playing_counts_cancelled(monkeypatch, counts):
    stream, _ = await _live(monkeypatch)
    stream.mark_cast()
    reader = asyncio.create_task(read_all(stream))
    await until(lambda: stream.first_read_at is not None)
    a_s.shutdown()
    await settle(stream)
    await reader  # the reader ends once the stream is finished
    assert counts == ["cancelled"]


async def test_shutdown_before_cast_counted_once_by_caller_abort(monkeypatch, counts):
    stream, _ = await _live(monkeypatch)
    a_s.shutdown()
    assert counts == []  # cast outcome unknown
    stream.abort("cancelled")  # what _announce_voice does when it's cancelled
    await settle(stream)
    assert counts == ["cancelled"]


async def test_chunk_failed_when_later_chunk_raises(monkeypatch, counts):
    stream, backend = await _live(monkeypatch, n=4, plan={2: RuntimeError("tts down")})
    stream.mark_cast()
    reader = asyncio.create_task(read_all(stream))
    backend.gate.set()
    await stream.producer
    await reader
    assert counts == ["chunk_failed"]
    assert stream.chunks_done == 2


async def test_chunk_failed_when_later_chunk_hangs(monkeypatch, counts):
    monkeypatch.setattr(a_s, "_chunk_timeout", lambda chunk: 0.05)
    stream, _ = await _live(monkeypatch, n=3, gate_from=None, plan={1: "hang"})
    stream.mark_cast()
    await read_first_then_hang_up(stream)
    await stream.producer
    assert stream.result == "chunk_failed" and counts == ["chunk_failed"]


async def test_chunk_failed_during_preroll_plays_what_there_is(monkeypatch, counts):
    fixed_chunks(monkeypatch, 4)
    backend = WavBackend(seconds=3.0, plan={1: RuntimeError("tts down")})
    stream = await a_s.start(backend, LONG, style="", speaker_key="k")
    assert stream is not None and stream.done and stream.result == "chunk_failed"
    assert stream.producer is None and stream.chunks_done == 1
    stream.mark_cast()
    await read_all(stream)
    assert counts == ["chunk_failed"]


@pytest.mark.parametrize(
    "bad",
    [pytest.param(wav_bytes(9.0, rate=16000), id="other-rate"), pytest.param(b"not a wav", id="not-wav")],
)
async def test_format_mismatch(monkeypatch, counts, bad):
    stream, _ = await _live(monkeypatch, gate_from=None, plan={1: bad})
    stream.mark_cast()
    await read_all(stream)
    await settle(stream)
    assert counts == ["format_mismatch"]
    assert stream.chunks_done == 1


async def test_too_long(monkeypatch, counts):
    monkeypatch.setattr(a_s, "MAX_STREAM_BYTES", 44 + 9 * BYTES_PER_S + 10)
    stream, _ = await _live(monkeypatch, gate_from=None)
    stream.mark_cast()
    await read_all(stream)
    await settle(stream)
    assert counts == ["too_long"]


async def test_deadline(monkeypatch, counts):
    monkeypatch.setattr(a_s, "PRODUCER_DEADLINE_S", 0.0)  # floor in _produce is 1 s
    stream, _ = await _live(monkeypatch, gate_from=None, plan={1: "hang"})
    stream.mark_cast()
    await read_first_then_hang_up(stream)
    await asyncio.wait_for(stream.producer, 5)
    assert counts == ["deadline"]


async def test_never_read_while_producing(monkeypatch, counts):
    monkeypatch.setattr(a_s, "FIRST_READ_TIMEOUT_S", 0.03)
    stream, _ = await _live(monkeypatch)
    stream.mark_cast()
    await until(lambda: counts)
    assert counts == ["never_read"] and stream.result == "never_read"
    await settle(stream)
    assert stream.producer.cancelled()
    assert counts == ["never_read"]


async def test_never_read_after_synthesis_finished(monkeypatch, counts):
    monkeypatch.setattr(a_s, "FIRST_READ_TIMEOUT_S", 0.03)
    fixed_chunks(monkeypatch, 2)
    stream = await a_s.start(WavBackend(seconds=3.0), LONG, style="", speaker_key="k")
    assert stream.result == "complete"
    stream.mark_cast()
    await until(lambda: counts)
    assert counts == ["never_read"]


async def test_never_read_replaces_cut_short_result(monkeypatch, counts):
    monkeypatch.setattr(a_s, "FIRST_READ_TIMEOUT_S", 0.03)
    fixed_chunks(monkeypatch, 4)
    stream = await a_s.start(WavBackend(seconds=3.0, plan={1: RuntimeError()}), LONG, style="", speaker_key="k")
    assert stream.result == "chunk_failed"
    stream.mark_cast()
    await until(lambda: counts)
    assert counts == ["never_read"]


async def test_superseded_and_never_read_stays_superseded(monkeypatch, counts):
    monkeypatch.setattr(a_s, "FIRST_READ_TIMEOUT_S", 0.03)
    stream, _ = await _live(monkeypatch)
    stream.mark_cast()
    a_s.supersede("k")
    assert counts == []  # a read may still come
    await until(lambda: counts)
    assert counts == ["superseded"]


async def test_late_first_read_after_never_read_is_not_recounted(monkeypatch, counts):
    monkeypatch.setattr(a_s, "FIRST_READ_TIMEOUT_S", 0.03)
    stream, _ = await _live(monkeypatch)
    stream.mark_cast()
    await until(lambda: counts)
    await read_all(stream)  # the buffered audio is still readable
    assert counts == ["never_read"]


async def test_abandoned_when_reader_hangs_up(monkeypatch, counts):
    monkeypatch.setattr(a_s, "ABANDON_GRACE_S", 0.03)
    stream, _ = await _live(monkeypatch)
    stream.mark_cast()
    await read_first_then_hang_up(stream)
    assert stream.readers == 0 and counts == []
    await until(lambda: counts)
    assert counts == ["abandoned"] and stream.result == "abandoned"
    await settle(stream)
    assert stream.producer.cancelled()


async def test_refetch_within_grace_is_not_abandoned(monkeypatch, counts):
    monkeypatch.setattr(a_s, "ABANDON_GRACE_S", 0.05)
    stream, backend = await _live(monkeypatch)
    stream.mark_cast()
    await read_first_then_hang_up(stream)
    reader = asyncio.create_task(read_all(stream))  # the speaker re-fetches
    await asyncio.sleep(0.1)
    assert not stream.done and counts == []
    backend.gate.set()
    await stream.producer
    await reader
    assert counts == ["complete"]


async def test_hang_up_after_running_dry_is_stalled(monkeypatch, counts):
    monkeypatch.setattr(a_s, "ABANDON_GRACE_S", 0.03)
    stream, _ = await _live(monkeypatch)
    stream.mark_cast()
    await read_first_then_hang_up(stream)
    stream.first_read_at -= 60  # it has played all 9 s and run out
    await until(lambda: counts)
    assert counts == ["stalled"]


async def test_stalled_when_reader_waits_too_long(monkeypatch, counts):
    monkeypatch.setattr(a_s, "READ_IDLE_TIMEOUT_S", 0.03)
    stream, _ = await _live(monkeypatch)  # chunk 2 never arrives
    stream.mark_cast()
    data = await asyncio.wait_for(read_all(stream), 3)
    assert data == bytes(stream._buf)
    assert counts == ["stalled"] and stream.result == "stalled"
    await settle(stream)
    assert stream.producer.cancelled()
    assert counts == ["stalled"]


async def test_counted_once_whatever_happens_after(monkeypatch, counts):
    fixed_chunks(monkeypatch, 2)
    stream = await a_s.start(WavBackend(seconds=3.0), LONG, style="", speaker_key="k")
    stream.mark_cast()
    await read_all(stream)
    assert counts == ["complete"]
    stream.finish("chunk_failed")
    stream.cancel("superseded")
    stream.abort("not_played")
    stream.mark_cast()
    a_s.supersede("k")
    a_s.shutdown()
    await read_all(stream)
    assert counts == ["complete"]


async def test_real_counter_incremented_once(monkeypatch):
    from orchestrator.metrics import TTS_PROGRESSIVE_TOTAL

    def value(result: str) -> float:
        for metric in TTS_PROGRESSIVE_TOTAL.collect():
            for s in metric.samples:
                if s.name.endswith("_total") and s.labels == {"result": result}:
                    return s.value
        return 0.0

    before = value("not_played")
    stream, _ = await _live(monkeypatch)
    stream.abort("not_played")
    await settle(stream)
    stream.abort("not_played")
    assert value("not_played") == before + 1


def test_seed_metrics_creates_every_label():
    from orchestrator.metrics import TTS_PROGRESSIVE_TOTAL

    a_s.seed_metrics()
    labels = {s.labels["result"] for m in TTS_PROGRESSIVE_TOTAL.collect() for s in m.samples}
    assert set(a_s.RESULTS) <= labels


async def test_cancel_during_preroll_is_counted(monkeypatch, counts):
    """The caller (its announce timeout) cancelled while start() was still
    making the pre-roll: the begun stream is counted cancelled, once."""
    fixed_chunks(monkeypatch, 4)
    backend = WavBackend(seconds=3.0, plan={1: "hang"})  # chunk 0 = 3 s < START_BUFFER_S, chunk 1 hangs
    task = asyncio.create_task(a_s.start(backend, LONG, style="", speaker_key="k"))
    await until(lambda: len(backend.calls) == 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert counts == ["cancelled"]


# ---------------------------------------------------------------------------
# iter_bytes, lookup, TTL, _prune
# ---------------------------------------------------------------------------


async def test_reader_gets_everything_from_byte_zero(monkeypatch):
    stream, backend = await _live(monkeypatch, n=3)
    reader = asyncio.create_task(read_all(stream))
    await until(lambda: stream.first_read_at is not None)
    backend.gate.set()
    await stream.producer
    data = await reader
    assert data[:4] == b"RIFF" and struct.unpack_from("<I", data, 4)[0] == 0xFFFFFFFF
    w = parse_wav(data)
    assert w is not None
    assert w.pcm == b"".join(bytes([i + 1]) * 9 * BYTES_PER_S for i in range(3))


async def test_two_concurrent_readers_get_everything(monkeypatch):
    stream, backend = await _live(monkeypatch, n=3)
    readers = [asyncio.create_task(read_all(stream)) for _ in range(2)]
    await until(lambda: stream.readers == 2)
    backend.gate.set()
    await stream.producer
    a, b = await asyncio.gather(*readers)
    assert a == b == bytes(stream._buf)
    assert stream.readers == 0


async def test_late_reader_of_finished_stream_starts_at_zero(monkeypatch):
    stream, backend = await _live(monkeypatch, n=3)
    backend.gate.set()
    await stream.producer
    assert await read_all(stream) == bytes(stream._buf)


async def test_max_readers_enforced_when_readers_start_together(monkeypatch):
    stream, _ = await _live(monkeypatch)
    gens = [stream.iter_bytes() for _ in range(MAX_READERS + 1)]
    results = await asyncio.gather(*(g.__anext__() for g in gens), return_exceptions=True)
    got = [r for r in results if isinstance(r, bytes)]
    refused = [r for r in results if isinstance(r, StopAsyncIteration)]
    assert len(got) == MAX_READERS and len(refused) == 1
    assert stream.readers == MAX_READERS
    for g in gens:
        await g.aclose()
    assert stream.readers == 0


async def test_reader_slot_is_freed_on_hang_up(monkeypatch):
    stream, _ = await _live(monkeypatch)
    gens = [stream.iter_bytes() for _ in range(MAX_READERS)]
    for g in gens:
        await g.__anext__()
    await gens[0].aclose()
    late = stream.iter_bytes()
    assert isinstance(await late.__anext__(), bytes)
    assert stream.readers == MAX_READERS
    for g in gens[1:] + [late]:
        await g.aclose()


async def test_lookup_names():
    stream = AudioStream("k", 2)
    a_s._STREAMS[stream.token] = stream
    assert a_s.lookup(stream.filename) is stream
    assert len(stream.token) == 24 and a_s.is_stream_name(stream.filename)
    for name in (
        f"s-{TOKEN}.wav",  # unknown
        f"s-{TOKEN}.mp3",
        f"s-{TOKEN[:-1]}.wav",
        f"x-{TOKEN}.wav",
        f"{TOKEN}.wav",
        f"s-{stream.token}.wav/x",
    ):
        assert a_s.lookup(name) is None, name


async def test_lookup_after_ttl_is_gone_even_with_a_reader(monkeypatch):
    fixed_chunks(monkeypatch, 2)
    stream = await a_s.start(WavBackend(seconds=3.0), LONG, style="", speaker_key="k")
    assert stream.done
    gen = stream.iter_bytes()
    first = await gen.__anext__()
    assert stream.readers == 1
    stream.finished_at -= STREAM_TTL_S + 1
    assert a_s.lookup(stream.filename) is None
    assert stream.token not in a_s._STREAMS
    rest = b"".join([piece async for piece in gen])  # the attached reader keeps its reference
    assert first + rest == bytes(stream._buf)


async def test_lookup_live_stream_never_expires(monkeypatch):
    stream, _ = await _live(monkeypatch)
    stream.created -= 10 * STREAM_TTL_S
    assert a_s.lookup(stream.filename) is stream


async def test_prune_evicts_oldest_beyond_max_streams(monkeypatch):
    old = []
    for i in range(MAX_STREAMS):
        s = AudioStream(f"media_player.room_{i}", 3)
        s.created -= 100 - i
        a_s._STREAMS[s.token] = s
        old.append(s)
    fixed_chunks(monkeypatch, 3)
    new = await a_s.start(WavBackend(gate_from=1), LONG, style="", speaker_key="media_player.new")
    assert len(a_s._STREAMS) == MAX_STREAMS
    assert old[0].token not in a_s._STREAMS and old[0].result == "superseded"
    assert all(s.token in a_s._STREAMS for s in old[1:])
    assert a_s._STREAMS[new.token] is new


async def test_prune_drops_expired_before_evicting_live(monkeypatch):
    live = []
    for i in range(MAX_STREAMS):
        s = AudioStream(f"media_player.room_{i}", 3)
        s.created -= 100 - i
        a_s._STREAMS[s.token] = s
        live.append(s)
    expired = live[5]
    expired.finish("complete")
    expired.finished_at -= STREAM_TTL_S + 1
    fixed_chunks(monkeypatch, 3)
    await a_s.start(WavBackend(gate_from=1), LONG, style="", speaker_key="media_player.new")
    assert expired.token not in a_s._STREAMS
    assert live[0].token in a_s._STREAMS and not live[0].done


# ---------------------------------------------------------------------------
# GET /api/audio/{filename}
# ---------------------------------------------------------------------------


def _scope(path: str, spec: str = "2.3", range_header: bool = True) -> dict:
    headers = [(b"host", b"orch.test:8888")] + ([(b"range", b"bytes=0-")] if range_header else [])
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("10.0.0.50", 40000),
        "server": ("orch.test", 8888),
    }


async def asgi_get(
    app,
    path: str,
    *,
    hang_up: asyncio.Event | None = None,
    stall_after: int | None = None,
    range_header: bool = True,
):
    """GET ``path`` the way uvicorn 0.54 drives an app (ASGI spec 2.3; after a
    disconnect, receive() says so and send() is a silent no-op).
    ``stall_after``: once this many body bytes went out, send() blocks until
    ``hang_up`` — a speaker that stopped reading (TCP flow control)."""
    hang_up = hang_up or asyncio.Event()
    start: dict = {}
    body = bytearray()
    requested = False

    async def receive():
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await hang_up.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if hang_up.is_set():
            return
        if message["type"] == "http.response.start":
            start.update(message)
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))
            if stall_after is not None and len(body) >= stall_after:
                await hang_up.wait()

    await app(_scope(path, range_header=range_header), receive, send)
    headers = {k.decode().lower(): v.decode() for k, v in start.get("headers", [])}
    return start.get("status"), headers, bytes(body)


async def test_route_unknown_stream_is_404():
    from orchestrator.api_routes import serve_audio

    resp = await serve_audio(f"s-{TOKEN}.wav")
    assert resp.status_code == 404


async def test_route_expired_stream_is_404(monkeypatch):
    from orchestrator.api_routes import serve_audio

    fixed_chunks(monkeypatch, 2)
    stream = await a_s.start(WavBackend(seconds=3.0), LONG, style="", speaker_key="k")
    stream.finished_at -= STREAM_TTL_S + 1
    assert (await serve_audio(stream.filename)).status_code == 404


async def test_route_reader_cap_is_429(monkeypatch):
    from orchestrator.api_routes import serve_audio

    stream, _ = await _live(monkeypatch)
    gens = [stream.iter_bytes() for _ in range(MAX_READERS)]
    for g in gens:
        await g.__anext__()
    resp = await serve_audio(stream.filename)
    assert resp.status_code == 429
    for g in gens:
        await g.aclose()


async def test_route_streams_live_reply(monkeypatch):
    from orchestrator.api_routes import serve_audio

    stream, backend = await _live(monkeypatch, n=3)
    resp = await serve_audio(stream.filename)
    assert isinstance(resp, a_s.ClosingStreamingResponse)
    assert resp.status_code == 200 and resp.media_type == "audio/wav"
    assert resp.headers["cache-control"] == "no-store"
    task = asyncio.create_task(asgi_get(resp, "/"))
    await until(lambda: stream.first_read_at is not None)
    backend.gate.set()
    await stream.producer
    status, headers, body = await asyncio.wait_for(task, 3)
    assert status == 200 and headers["content-type"] == "audio/wav"
    assert body == bytes(stream._buf)
    assert len(parse_wav(body).pcm) == 3 * 9 * BYTES_PER_S
    assert stream.readers == 0


@pytest.mark.parametrize("ext, media_type", [("wav", "audio/wav"), ("mp3", "audio/mpeg")])
async def test_route_still_serves_one_clip_files(ext, media_type):
    from orchestrator.api_routes import serve_audio

    name = f"{TOKEN}.{ext}"
    path = f"/tmp/brain_audio/{name}"
    os.makedirs("/tmp/brain_audio", exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"clip-bytes")
    try:
        resp = await serve_audio(name)
        assert resp.status_code == 200 and resp.media_type == media_type
        status, _, body = await asgi_get(resp, "/", range_header=False)
        assert status == 200 and body == b"clip-bytes"
    finally:
        os.remove(path)


async def test_route_clip_name_starting_s_dash_is_a_file():
    """A one-clip token can start with "s-"; it's 24 chars, not s-+24, so
    it's served from disk, not looked up as a stream."""
    from orchestrator.api_routes import serve_audio

    name = f"s-{TOKEN[:22]}.wav"
    assert not a_s.is_stream_name(name)
    path = f"/tmp/brain_audio/{name}"
    os.makedirs("/tmp/brain_audio", exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x")
    try:
        assert (await serve_audio(name)).status_code == 200
    finally:
        os.remove(path)


@pytest.fixture
def full_app(monkeypatch):
    """The orchestrator's own middleware stack (CORS + 3x BaseHTTPMiddleware
    incl. bearer auth, same order) in front of the real api_routes router."""
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    import orchestrator.orchestrator as orch
    from orchestrator import api_routes

    monkeypatch.setattr(orch, "API_TOKEN", "test-bearer-token-0123456789")
    monkeypatch.setattr(orch, "_rate_limit_store", {})
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3001"], allow_methods=["*"])
    app.add_middleware(orch.BearerAuthMiddleware)
    app.add_middleware(orch.RequestSizeLimitMiddleware)
    app.add_middleware(orch.RateLimitMiddleware)
    app.include_router(api_routes.router)
    return app


async def test_app_serves_stream_without_token(monkeypatch, full_app):
    fixed_chunks(monkeypatch, 2)
    stream = await a_s.start(WavBackend(seconds=3.0), LONG, style="", speaker_key="k")
    status, headers, body = await asyncio.wait_for(asgi_get(full_app, f"/api/audio/{stream.filename}"), 5)
    assert status == 200 and headers["content-type"] == "audio/wav"
    assert body == bytes(stream._buf)
    assert stream.readers == 0


async def test_app_unknown_stream_is_404_not_401(full_app):
    status, _, _ = await asgi_get(full_app, f"/api/audio/s-{TOKEN}.wav")
    assert status == 404


@pytest.mark.parametrize(
    "seconds, stall_after",
    [
        pytest.param(30.0, 1, id="stops-reading"),  # send blocked, pipeline full
        pytest.param(9.0, None, id="waiting-for-audio"),  # reader awaiting the next chunk
    ],
)
async def test_app_hang_up_mid_stream_frees_reader_and_abandons(monkeypatch, full_app, counts, seconds, stall_after):
    monkeypatch.setattr(a_s, "ABANDON_GRACE_S", 0.03)
    stream, _ = await _live(monkeypatch, seconds=seconds)
    stream.mark_cast()
    hang_up = asyncio.Event()
    task = asyncio.create_task(
        asgi_get(full_app, f"/api/audio/{stream.filename}", hang_up=hang_up, stall_after=stall_after)
    )
    await until(lambda: stream.first_read_at is not None)
    await asyncio.sleep(0.02)  # let the pipeline fill / the reader reach its wait
    hang_up.set()
    await asyncio.wait_for(task, 3)
    await until(lambda: stream.readers == 0, timeout=2)
    await until(lambda: counts, timeout=2)
    assert counts == ["abandoned"]


# ---------------------------------------------------------------------------
# ClosingStreamingResponse
# ---------------------------------------------------------------------------


async def test_closing_response_closes_iterator_when_send_raises(monkeypatch):
    """ASGI 2.4 server: send raises OSError once the client is gone."""
    from starlette.requests import ClientDisconnect

    stream, _ = await _live(monkeypatch, seconds=30.0)
    bodies = 0

    async def send(message):
        nonlocal bodies
        if message["type"] == "http.response.body":
            bodies += 1
            if bodies > 1:
                raise OSError("connection reset")

    async def receive():
        await asyncio.Event().wait()

    with pytest.raises(ClientDisconnect):
        await a_s.response(stream)(_scope("/", spec="2.4"), receive, send)
    assert stream.readers == 0


async def test_closing_response_closes_iterator_when_send_blocked_and_client_leaves(monkeypatch):
    """ASGI 2.3 server: the send is blocked (speaker stopped reading), then
    the disconnect arrives; the generator was parked at its yield."""
    stream, _ = await _live(monkeypatch, seconds=30.0)
    gone = asyncio.Event()

    async def send(message):
        if message["type"] == "http.response.body":
            await asyncio.Event().wait()  # never drains

    async def receive():
        await gone.wait()
        return {"type": "http.disconnect"}

    task = asyncio.create_task(a_s.response(stream)(_scope("/"), receive, send))
    await until(lambda: stream.readers == 1)
    gone.set()
    await asyncio.wait_for(task, 3)
    assert stream.readers == 0
