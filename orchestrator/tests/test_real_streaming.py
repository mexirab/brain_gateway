"""
Tests for real token streaming: StreamGate, _stream_model_round delta
assembly, on_delta threading through run_unified_tool_loop, and the
cloud_brain SSE relay generator.

clean_response's own table lives in test_unified_loop.py.
"""

import json
import random
import re
from unittest.mock import AsyncMock

import pytest

from orchestrator.metrics import CHAT_STREAM_OUTCOME
from orchestrator.tests._helpers import metric_value
from orchestrator.tests.fixtures_loop import (
    SSE_DONE,
    FakeStreamBackend,
    SequencedBackend,
    content_chunk,
    reasoning_chunk,
    run_round,
    run_stream_loop,
    stop_chunk,
    stream_brain,
    stream_response,
    tool_chunk,
)
from orchestrator.unified_loop import StreamGate, clean_response

# ---------------------------------------------------------------------------
# StreamGate
# ---------------------------------------------------------------------------


def _whole(text):
    g = StreamGate()
    return g.feed(text) + g.flush()


def _per_char(text):
    g = StreamGate()
    return "".join(g.feed(c) for c in text) + g.flush()


@pytest.mark.parametrize(
    "text, expected",
    [
        pytest.param("Hello world", "Hello world", id="passthrough"),
        pytest.param("Hi <think>secret reasoning</think> there", "Hi  there", id="think"),
        pytest.param(
            'Checking. <tool_call>{"name": "check_calendar", "arguments": {}}</tool_call>', "Checking. ", id="tool-call"
        ),
        pytest.param("3 < 5 and x <y>", "3 < 5 and x <y>", id="literal-angle-brackets"),
        pytest.param("a < b", "a < b", id="bare-lt"),
        pytest.param("keep <think reason=1>hidden</think> end", "keep  end", id="attributed-think"),
        pytest.param('a <tool_call id="a">{}</tool_call> b', "a  b", id="attributed-tool-call"),
        pytest.param('before <tool_call id="1">{"name":"x"}</tool_call> after', "before  after", id="attributed-tc-2"),
        pytest.param("before <think foo=bar>secret reasoning</think> after", "before  after", id="attributed-think-2"),
        pytest.param("a <think>x</think> b", "a  b", id="plain-think"),
        pytest.param("a <think>l1\nl2</think> b", "a  b", id="multiline-think"),
        pytest.param("<thinker>keep</thinker>", "<thinker>keep</thinker>", id="thinker-lookalike"),
        pytest.param("<tool_calls>keep</tool_calls>", "<tool_calls>keep</tool_calls>", id="tool-calls-lookalike"),
        # Hold-back cap: an over-long unterminated tag is emitted, not swallowed.
        pytest.param("<think " + "a" * 300, "<think " + "a" * 300, id="holdback-cap"),
    ],
)
def test_gate_output_whole_and_char_by_char(text, expected):
    """Same output whether the text arrives in one delta or one character per delta."""
    assert _whole(text) == expected
    assert _per_char(text) == expected


def test_gate_holdback_flushes_when_not_a_tag():
    g = StreamGate()
    assert g.feed("a <t") == "a "
    assert g.flush() == "<t"


def test_gate_unterminated_think_swallowed():
    g = StreamGate()
    assert g.feed("<think>secret never ends") == ""
    assert g.flush() == ""


def test_gate_closing_tag_split_across_feeds():
    g = StreamGate()
    assert g.feed("<think>abc</thi") + g.feed("nk>after") + g.flush() == "after"


def test_gate_partial_open_tag_resolving_to_text():
    g = StreamGate()
    assert g.feed("<to") + g.feed("day> is nice") + g.flush() == "<today> is nice"


# The random fuzzer caps at ~14 tokens, so it never generates a tag whose
# attributes exceed the regex bound / hold-back cap (256/320). That's exactly
# where a naive cap diverges: whole-input regex-matches and suppresses, while a
# chunked feed trips the cap and EMITS (leaks) the block. These deterministic
# cases lock whole == char-by-char at and beyond the boundary.
@pytest.mark.parametrize(
    "text",
    [
        pytest.param("<think " + "a" * 300 + ">hidden</think> tail", id="over-bound-with-closer"),
        pytest.param("<think " + "a" * 400 + ">x</think>END", id="over-cap"),
        pytest.param("<think " + "a" * 256 + ">b</think>Z", id="at-bound"),
        pytest.param("<think " + "a" * 257 + ">b</think>Z", id="just-over-bound"),
        pytest.param("<think " + "a" * 500, id="unterminated-flood"),
        pytest.param("<tool_call " + "b" * 300 + ">{}</tool_call> ok", id="tool-call-over-bound"),
    ],
)
def test_gate_long_attribute_tag_chunk_invariant(text):
    assert _whole(text) == _per_char(text)


# ---------------------------------------------------------------------------
# _stream_model_round — delta assembly against a fake backend
# ---------------------------------------------------------------------------


async def test_round_streams_content():
    backend = FakeStreamBackend([content_chunk("Hel"), content_chunk("lo!"), stop_chunk(), SSE_DONE])
    result, emitted = await run_round(backend)
    assert emitted == "Hello!"
    assert result["content"] == "Hello!"
    assert result["tool_calls"] == []
    assert result["finish_reason"] == "stop"
    assert result["error"] is None


async def test_round_assembles_split_tool_call():
    backend = FakeStreamBackend(
        [
            tool_chunk(0, name="check_calendar", call_id="call_abc"),
            tool_chunk(0, args='{"days'),
            tool_chunk(0, args='": 1}'),
            stop_chunk(),
        ]
    )
    result, emitted = await run_round(backend)
    assert emitted == ""
    (tc,) = result["tool_calls"]
    assert tc["id"] == "call_abc"
    assert tc["function"]["name"] == "check_calendar"
    assert json.loads(tc["function"]["arguments"]) == {"days": 1}


async def test_round_skips_reasoning_deltas():
    backend = FakeStreamBackend([reasoning_chunk("thinking hard..."), content_chunk("Answer."), stop_chunk()])
    result, emitted = await run_round(backend)
    assert emitted == "Answer."
    assert result["reasoning_len"] == len("thinking hard...")


async def test_round_error_drops_incomplete_tool_calls():
    """A stream that dies mid-tool-JSON must not hand back half-baked args."""
    backend = FakeStreamBackend(
        [tool_chunk(0, name="set_reminder", args='{"text": "unfini'), content_chunk("x")], raise_after=1
    )
    result, _ = await run_round(backend)
    assert result["error"] is not None
    assert result["tool_calls"] == []


async def test_round_error_before_emission():
    result, emitted = await run_round(FakeStreamBackend([content_chunk("hi")], raise_after=0))
    assert result["error"] is not None
    assert result["emitted"] == 0
    assert emitted == ""


# ---------------------------------------------------------------------------
# run_unified_tool_loop with on_delta
# ---------------------------------------------------------------------------


async def test_loop_streams_plain_answer():
    backend = SequencedBackend([[content_chunk("Just "), content_chunk("hi."), stop_chunk()]])
    result, deltas = await run_stream_loop(backend)
    assert result == "Just hi."
    assert "".join(deltas) == "Just hi."


async def test_loop_tool_round_then_streamed_answer():
    execute_tool = AsyncMock(return_value="calendar is clear")
    backend = SequencedBackend(
        [
            [tool_chunk(0, name="check_calendar", call_id="c1"), tool_chunk(0, args="{}"), stop_chunk()],
            [content_chunk("Nothing on tomorrow."), stop_chunk()],
        ]
    )
    result, deltas = await run_stream_loop(backend, execute_tool=execute_tool)
    assert result == "Nothing on tomorrow."
    assert "".join(deltas) == "Nothing on tomorrow."
    execute_tool.assert_awaited_once_with("check_calendar", {})


async def test_loop_preamble_then_tools_gets_separator():
    """Content emitted before a tool round + more content after -> the two
    segments are joined with a paragraph break in the stream."""
    backend = SequencedBackend(
        [
            [
                content_chunk("Let me check."),
                tool_chunk(0, name="check_calendar", call_id="c1"),
                tool_chunk(0, args="{}"),
                stop_chunk(),
            ],
            [content_chunk("All clear."), stop_chunk()],
        ]
    )
    result, deltas = await run_stream_loop(backend)
    assert "".join(deltas) == "Let me check.\n\nAll clear."
    assert result == "All clear."


async def test_loop_xml_tool_call_never_leaks_to_stream():
    xml = '<tool_call>{"name": "check_calendar", "arguments": {}}</tool_call>'
    backend = SequencedBackend([[content_chunk(xml), stop_chunk()], [content_chunk("Done via XML."), stop_chunk()]])
    execute_tool = AsyncMock(return_value="ok")
    _, deltas = await run_stream_loop(backend, execute_tool=execute_tool)
    joined = "".join(deltas)
    assert "tool_call" not in joined
    assert "Done via XML." in joined
    execute_tool.assert_awaited_once()


async def test_stream_failure_before_emission_retries_buffered_and_counts():
    """A stream that errors before any token reaches the client counts as
    pre_emission_retry and falls back to a buffered round, whose answer still
    reaches the client via on_delta."""
    before = metric_value(CHAT_STREAM_OUTCOME, outcome="pre_emission_retry")
    call_model = AsyncMock(return_value={"choices": [{"message": {"content": "Buffered answer.", "tool_calls": None}}]})
    backend = FakeStreamBackend([content_chunk("x")], raise_after=0)
    result, deltas = await run_stream_loop(backend, call_model=call_model)
    assert result == "Buffered answer."
    assert "".join(deltas) == "Buffered answer."
    call_model.assert_awaited_once()
    assert metric_value(CHAT_STREAM_OUTCOME, outcome="pre_emission_retry") == before + 1


async def test_died_mid_emission_increments_outcome_counter():
    """A stream that dies AFTER emitting increments the died_mid_emission
    outcome (the silent-truncation signal the ChatStreamTruncating alert reads)."""
    before = metric_value(CHAT_STREAM_OUTCOME, outcome="died_mid_emission")
    # Emit one chunk (i=0), then raise at i=1: death after emission, the branch
    # that returns the partial answer.
    backend = FakeStreamBackend([content_chunk("Partial answer"), content_chunk(" more")], raise_after=1)
    _, deltas = await run_stream_loop(backend)
    assert "Partial answer" in "".join(deltas)
    assert metric_value(CHAT_STREAM_OUTCOME, outcome="died_mid_emission") == before + 1


# ---------------------------------------------------------------------------
# cloud_brain._stream_loop_response SSE relay
# ---------------------------------------------------------------------------


async def _collect_sse(response):
    events = []
    async for raw in response.body_iterator:
        for line in raw.split("\n"):
            line = line.strip()
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                events.append(("done", None))
            else:
                choice = json.loads(data)["choices"][0]
                if choice.get("finish_reason") == "stop":
                    events.append(("stop", None))
                else:
                    events.append(("content", choice["delta"].get("content", "")))
    return events


async def _relay(loop_fn, **kw):
    return await _collect_sse(stream_response(stream_brain(loop_fn), messages=[], **kw))


async def test_relay_streams_deltas_then_stop():
    async def loop_fn(**kwargs):
        await kwargs["on_delta"]("Hel")
        await kwargs["on_delta"]("lo")
        return "Hello"

    assert await _relay(loop_fn) == [("content", "Hel"), ("content", "lo"), ("stop", None), ("done", None)]


async def test_relay_buffered_result_sent_once():
    """A loop that emitted nothing (fully buffered) -> whole answer in one chunk."""

    async def loop_fn(**kwargs):
        return "Whole answer"

    assert await _relay(loop_fn) == [("content", "Whole answer"), ("stop", None), ("done", None)]


async def test_relay_error_without_emission_apologizes():
    async def loop_fn(**kwargs):
        raise RuntimeError("boom")

    events = await _relay(loop_fn)
    assert events[0][0] == "content" and "Sorry" in events[0][1]
    assert events[-2:] == [("stop", None), ("done", None)]


async def test_relay_error_after_emission_closes_cleanly():
    """Partial text already sent -> no apology appended, clean stop."""

    async def loop_fn(**kwargs):
        await kwargs["on_delta"]("Partial ")
        raise RuntimeError("boom")

    assert await _relay(loop_fn) == [("content", "Partial "), ("stop", None), ("done", None)]


async def test_relay_fallback_snapshots_messages_and_passes_is_voice():
    """Loop failure with nothing emitted -> the fallback model's result streams
    as one chunk (parity with the buffered _unified_fallback): it runs on the
    ORIGINAL messages (not the primary loop's in-place appends), carries
    is_voice, and bumps loop_failed_fallback."""
    before = metric_value(CHAT_STREAM_OUTCOME, outcome="loop_failed_fallback")
    captured = {}

    async def loop_fn(**kwargs):
        if kwargs.get("on_delta") is not None:
            # primary: pollute the caller's list, then die before emitting
            kwargs["messages"].append({"role": "assistant", "content": "polluted"})
            raise RuntimeError("primary died")
        captured["messages"] = list(kwargs["messages"])
        captured["is_voice"] = kwargs.get("is_voice")
        return "fb"

    resp = stream_response(
        stream_brain(loop_fn, fallback_url="http://fb"), is_voice=True, messages=[{"role": "user", "content": "hi"}]
    )
    assert await _collect_sse(resp) == [("content", "fb"), ("stop", None), ("done", None)]
    assert captured["messages"] == [{"role": "user", "content": "hi"}]
    assert captured["is_voice"] is True
    assert metric_value(CHAT_STREAM_OUTCOME, outcome="loop_failed_fallback") == before + 1


# ---------------------------------------------------------------------------
# FUZZ TESTS (seeded random — no hypothesis dependency; deterministic; <5s)
#
# Property 1 (chunk-invariance): for any input, feeding a StreamGate in
#   arbitrary random chunk boundaries yields the SAME output as one feed(),
#   which also equals a single feed()+flush() of the whole string.
# Property 2 (no-leak): clean_response(s) never contains a complete
#   <think ...>...</think> or <tool_call ...>...</tool_call> pair.
# ---------------------------------------------------------------------------

# Alphabet biased toward the interesting structural characters plus whole
# tokens, so random strings frequently produce (partial) sentinel tags.
_FUZZ_TOKENS = [
    "<",
    ">",
    "/",
    " ",
    "\n",
    "a",
    "b",
    "x",
    "z",
    "think",
    "tool_call",
    "thinker",
    "tool_calls",
    "reason=1",
    'id="a"',
    "foo=bar",
    "hello",
    "3 < 5",
    "secret",
]


def _random_fuzz_string(rng, max_tokens=14):
    return "".join(rng.choice(_FUZZ_TOKENS) for _ in range(rng.randint(0, max_tokens)))


def _feed_in_random_chunks(s, rng):
    g = StreamGate()
    out = []
    i = 0
    while i < len(s):
        step = rng.randint(1, max(1, len(s) - i))
        out.append(g.feed(s[i : i + step]))
        i += step
    out.append(g.flush())
    return "".join(out)


def test_fuzz_stream_gate_chunk_invariance():
    rng = random.Random(1337)
    for _ in range(400):
        s = _random_fuzz_string(rng)
        whole = _whole(s)
        chunked = _feed_in_random_chunks(s, rng)
        assert chunked == whole, f"chunk-invariance violation for {s!r}: chunked={chunked!r} whole={whole!r}"


def test_fuzz_clean_response_never_leaks_complete_pair():
    think_pair = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL)
    tool_pair = re.compile(r"<tool_call\b[^>]*>.*?</tool_call\s*>", re.DOTALL)
    rng = random.Random(2024)
    for _ in range(400):
        s = _random_fuzz_string(rng)
        cleaned = clean_response(s)
        assert not think_pair.search(cleaned), f"leaked <think> pair from {s!r}: {cleaned!r}"
        assert not tool_pair.search(cleaned), f"leaked <tool_call> pair from {s!r}: {cleaned!r}"
