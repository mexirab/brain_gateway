"""
Tests for real token streaming: StreamGate, _stream_model_round delta
assembly, on_delta threading through run_unified_tool_loop, and the
cloud_brain SSE relay generator.

Runs inside the brain-orchestrator container (full deps available). Skips
gracefully when orchestrator dependencies are unavailable locally.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest


def _can_import():
    try:
        from orchestrator import unified_loop  # noqa: F401

        return True
    except (ImportError, ModuleNotFoundError):
        return False


_skip_no_deps = pytest.mark.skipif(
    not _can_import(),
    reason="unified_loop requires full orchestrator dependencies",
)


@pytest.fixture(autouse=True)
def _skip_without_deps():
    if not _can_import():
        pytest.skip("unified_loop deps unavailable")


# ---------------------------------------------------------------------------
# StreamGate
# ---------------------------------------------------------------------------


def _gate():
    from orchestrator.unified_loop import StreamGate

    return StreamGate()


@_skip_no_deps
def test_gate_passthrough():
    g = _gate()
    assert g.feed("Hello world") + g.flush() == "Hello world"


@_skip_no_deps
def test_gate_suppresses_think_block_char_by_char():
    """The worst case: every delta is a single character."""
    g = _gate()
    out = "".join(g.feed(c) for c in "Hi <think>secret reasoning</think> there")
    out += g.flush()
    assert out == "Hi  there"
    assert "secret" not in out


@_skip_no_deps
def test_gate_suppresses_xml_tool_call():
    g = _gate()
    text = 'Checking. <tool_call>{"name": "check_calendar", "arguments": {}}</tool_call>'
    out = g.feed(text) + g.flush()
    assert out == "Checking. "
    assert "check_calendar" not in out


@_skip_no_deps
def test_gate_holdback_flushes_when_not_a_tag():
    g = _gate()
    assert g.feed("a <t") == "a "
    assert g.flush() == "<t"


@_skip_no_deps
def test_gate_literal_angle_bracket_emitted():
    g = _gate()
    out = g.feed("3 < 5 and x <y>") + g.flush()
    assert out == "3 < 5 and x <y>"


@_skip_no_deps
def test_gate_unterminated_think_swallowed():
    g = _gate()
    assert g.feed("<think>never ends") == ""
    assert g.flush() == ""


@_skip_no_deps
def test_gate_closing_tag_split_across_feeds():
    g = _gate()
    out = g.feed("<think>abc</thi")
    out += g.feed("nk>after")
    out += g.flush()
    assert out == "after"


@_skip_no_deps
def test_gate_partial_open_tag_resolving_to_text():
    g = _gate()
    out = g.feed("<to")
    out += g.feed("day> is nice")
    out += g.flush()
    assert out == "<today> is nice"


# ---------------------------------------------------------------------------
# _stream_model_round — delta assembly against a fake backend
# ---------------------------------------------------------------------------


def _sse(obj) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _content_chunk(text):
    return _sse({"choices": [{"delta": {"content": text}, "finish_reason": None}]})


def _reasoning_chunk(text):
    return _sse({"choices": [{"delta": {"reasoning_content": text}, "finish_reason": None}]})


def _tool_chunk(index, name=None, args=None, call_id=None):
    fn = {}
    if name:
        fn["name"] = name
    if args:
        fn["arguments"] = args
    tc = {"index": index, "function": fn}
    if call_id:
        tc["id"] = call_id
    return _sse({"choices": [{"delta": {"tool_calls": [tc]}, "finish_reason": None}]})


def _stop_chunk():
    return _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]})


class FakeStreamBackend:
    """Yields a canned list of SSE strings; optionally raises mid-stream."""

    def __init__(self, lines, raise_after=None):
        self._lines = lines
        self._raise_after = raise_after

    async def stream_chat_completion(
        self, messages, system="", timeout=180, tools=None, tool_choice="auto", extra_body=None, **kwargs
    ):
        for i, line in enumerate(self._lines):
            if self._raise_after is not None and i >= self._raise_after:
                raise RuntimeError("stream broke")
            yield line


async def _run_round(backend):
    from orchestrator.unified_loop import _stream_model_round

    emitted = []

    async def emit(text):
        emitted.append(text)

    result = await _stream_model_round(backend, [], "sys", None, "auto", 120, None, emit, "TEST")
    return result, "".join(emitted)


@_skip_no_deps
@pytest.mark.asyncio
async def test_round_streams_content():
    backend = FakeStreamBackend([_content_chunk("Hel"), _content_chunk("lo!"), _stop_chunk(), "data: [DONE]\n\n"])
    result, emitted = await _run_round(backend)
    assert emitted == "Hello!"
    assert result["content"] == "Hello!"
    assert result["tool_calls"] == []
    assert result["finish_reason"] == "stop"
    assert result["error"] is None


@_skip_no_deps
@pytest.mark.asyncio
async def test_round_assembles_split_tool_call():
    backend = FakeStreamBackend(
        [
            _tool_chunk(0, name="check_calendar", call_id="call_abc"),
            _tool_chunk(0, args='{"days'),
            _tool_chunk(0, args='": 1}'),
            _stop_chunk(),
        ]
    )
    result, emitted = await _run_round(backend)
    assert emitted == ""
    assert len(result["tool_calls"]) == 1
    tc = result["tool_calls"][0]
    assert tc["id"] == "call_abc"
    assert tc["function"]["name"] == "check_calendar"
    assert json.loads(tc["function"]["arguments"]) == {"days": 1}


@_skip_no_deps
@pytest.mark.asyncio
async def test_round_skips_reasoning_deltas():
    backend = FakeStreamBackend([_reasoning_chunk("thinking hard..."), _content_chunk("Answer."), _stop_chunk()])
    result, emitted = await _run_round(backend)
    assert emitted == "Answer."
    assert result["reasoning_len"] == len("thinking hard...")


@_skip_no_deps
@pytest.mark.asyncio
async def test_round_error_drops_incomplete_tool_calls():
    """A stream that dies mid-tool-JSON must not hand back half-baked args."""
    backend = FakeStreamBackend(
        [_tool_chunk(0, name="set_reminder", args='{"text": "unfini'), _content_chunk("x")],
        raise_after=1,
    )
    result, emitted = await _run_round(backend)
    assert result["error"] is not None
    assert result["tool_calls"] == []


@_skip_no_deps
@pytest.mark.asyncio
async def test_round_error_before_emission():
    backend = FakeStreamBackend([_content_chunk("hi")], raise_after=0)
    result, emitted = await _run_round(backend)
    assert result["error"] is not None
    assert result["emitted"] == 0
    assert emitted == ""


# ---------------------------------------------------------------------------
# run_unified_tool_loop with on_delta
# ---------------------------------------------------------------------------


async def _run_loop(backend, tools=None, call_model_mock=None, execute_tool_mock=None):
    from orchestrator.unified_loop import run_unified_tool_loop

    deltas = []

    async def on_delta(text):
        deltas.append(text)

    tools = tools or [{"type": "function", "function": {"name": "check_calendar", "parameters": {}}}]
    with (
        patch("orchestrator.orchestrator.get_stream_capable_backend", return_value=backend),
        patch("orchestrator.orchestrator.call_model", call_model_mock or AsyncMock()),
        patch("orchestrator.tool_handlers.execute_tool", execute_tool_mock or AsyncMock(return_value="ok")),
    ):
        result = await run_unified_tool_loop(
            messages=[{"role": "user", "content": "hi"}],
            system_prompt="sys",
            tools=tools,
            model_url="http://fake:8080/v1",
            model_name="fake",
            http_client=None,
            on_delta=on_delta,
        )
    return result, deltas


class SequencedBackend:
    """Returns a different canned stream per call (per loop round)."""

    def __init__(self, rounds):
        self._rounds = list(rounds)
        self.calls = 0

    async def stream_chat_completion(self, *args, **kwargs):
        lines = self._rounds[min(self.calls, len(self._rounds) - 1)]
        self.calls += 1
        for line in lines:
            yield line


@_skip_no_deps
@pytest.mark.asyncio
async def test_loop_streams_plain_answer():
    backend = SequencedBackend([[_content_chunk("Just "), _content_chunk("hi."), _stop_chunk()]])
    result, deltas = await _run_loop(backend)
    assert result == "Just hi."
    assert "".join(deltas) == "Just hi."


@_skip_no_deps
@pytest.mark.asyncio
async def test_loop_tool_round_then_streamed_answer():
    execute_tool = AsyncMock(return_value="calendar is clear")
    backend = SequencedBackend(
        [
            [_tool_chunk(0, name="check_calendar", call_id="c1"), _tool_chunk(0, args="{}"), _stop_chunk()],
            [_content_chunk("Nothing on tomorrow."), _stop_chunk()],
        ]
    )
    result, deltas = await _run_loop(backend, execute_tool_mock=execute_tool)
    assert result == "Nothing on tomorrow."
    assert "".join(deltas) == "Nothing on tomorrow."
    execute_tool.assert_awaited_once_with("check_calendar", {})


@_skip_no_deps
@pytest.mark.asyncio
async def test_loop_preamble_then_tools_gets_separator():
    """Content emitted before a tool round + more content after -> the two
    segments are joined with a paragraph break in the stream."""
    backend = SequencedBackend(
        [
            [
                _content_chunk("Let me check."),
                _tool_chunk(0, name="check_calendar", call_id="c1"),
                _tool_chunk(0, args="{}"),
                _stop_chunk(),
            ],
            [_content_chunk("All clear."), _stop_chunk()],
        ]
    )
    result, deltas = await _run_loop(backend)
    assert "".join(deltas) == "Let me check.\n\nAll clear."
    assert result == "All clear."


@_skip_no_deps
@pytest.mark.asyncio
async def test_loop_xml_tool_call_never_leaks_to_stream():
    xml = '<tool_call>{"name": "check_calendar", "arguments": {}}</tool_call>'
    backend = SequencedBackend(
        [
            [_content_chunk(xml), _stop_chunk()],
            [_content_chunk("Done via XML."), _stop_chunk()],
        ]
    )
    execute_tool = AsyncMock(return_value="ok")
    result, deltas = await _run_loop(backend, execute_tool_mock=execute_tool)
    joined = "".join(deltas)
    assert "tool_call" not in joined
    assert "Done via XML." in joined
    execute_tool.assert_awaited_once()


@_skip_no_deps
@pytest.mark.asyncio
async def test_loop_stream_failure_falls_back_to_buffered():
    """A stream that dies before emitting anything retries buffered — and the
    buffered answer still reaches the client via on_delta."""

    class BrokenBackend:
        async def stream_chat_completion(self, *args, **kwargs):
            raise RuntimeError("connection refused")
            yield  # pragma: no cover — makes this an async generator

    call_model = AsyncMock(return_value={"choices": [{"message": {"content": "Buffered answer.", "tool_calls": None}}]})
    result, deltas = await _run_loop(BrokenBackend(), call_model_mock=call_model)
    assert result == "Buffered answer."
    assert "".join(deltas) == "Buffered answer."
    call_model.assert_awaited_once()


@_skip_no_deps
@pytest.mark.asyncio
async def test_loop_without_on_delta_unchanged():
    """No on_delta -> the loop never touches the streaming machinery."""
    from orchestrator.unified_loop import run_unified_tool_loop

    call_model = AsyncMock(return_value={"choices": [{"message": {"content": "Classic.", "tool_calls": None}}]})
    with (
        patch("orchestrator.orchestrator.call_model", call_model),
        patch("orchestrator.tool_handlers.execute_tool", AsyncMock()),
    ):
        result = await run_unified_tool_loop(
            messages=[{"role": "user", "content": "hi"}],
            system_prompt="sys",
            tools=[{"type": "function", "function": {"name": "check_calendar", "parameters": {}}}],
            model_url="http://fake:8080/v1",
            model_name="fake",
            http_client=None,
        )
    assert result == "Classic."


# ---------------------------------------------------------------------------
# cloud_brain._stream_loop_response SSE relay
# ---------------------------------------------------------------------------


def _make_brain(loop_fn, fallback_url=""):
    from orchestrator.cloud_brain import CloudBrain

    brain = CloudBrain.__new__(CloudBrain)
    brain._run_unified_loop = loop_fn
    brain._fallback_model_url = fallback_url
    brain._fallback_model_name = "fb"
    brain._get_all_tools = lambda: []
    brain._schedule_auto_learn = lambda messages: None
    return brain


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
                parsed = json.loads(data)
                choice = parsed["choices"][0]
                if choice.get("finish_reason") == "stop":
                    events.append(("stop", None))
                else:
                    events.append(("content", choice["delta"].get("content", "")))
    return events


@_skip_no_deps
@pytest.mark.asyncio
async def test_relay_streams_deltas_then_stop():
    async def loop_fn(**kwargs):
        await kwargs["on_delta"]("Hel")
        await kwargs["on_delta"]("lo")
        return "Hello"

    resp = _make_brain(loop_fn)._stream_loop_response(
        messages=[], system_prompt="s", tools=[], model_url="u", model_name="m", is_voice=False
    )
    events = await _collect_sse(resp)
    assert events == [("content", "Hel"), ("content", "lo"), ("stop", None), ("done", None)]


@_skip_no_deps
@pytest.mark.asyncio
async def test_relay_buffered_result_sent_once():
    """A loop that emitted nothing (fully buffered) -> whole answer in one chunk."""

    async def loop_fn(**kwargs):
        return "Whole answer"

    resp = _make_brain(loop_fn)._stream_loop_response(
        messages=[], system_prompt="s", tools=[], model_url="u", model_name="m", is_voice=False
    )
    events = await _collect_sse(resp)
    assert events == [("content", "Whole answer"), ("stop", None), ("done", None)]


@_skip_no_deps
@pytest.mark.asyncio
async def test_relay_error_without_emission_apologizes():
    async def loop_fn(**kwargs):
        raise RuntimeError("boom")

    resp = _make_brain(loop_fn)._stream_loop_response(
        messages=[], system_prompt="s", tools=[], model_url="u", model_name="m", is_voice=False
    )
    events = await _collect_sse(resp)
    assert events[0][0] == "content" and "Sorry" in events[0][1]
    assert events[-2:] == [("stop", None), ("done", None)]


@_skip_no_deps
@pytest.mark.asyncio
async def test_relay_error_after_emission_closes_cleanly():
    """Partial text already sent -> no apology appended, clean stop."""

    async def loop_fn(**kwargs):
        await kwargs["on_delta"]("Partial ")
        raise RuntimeError("boom")

    resp = _make_brain(loop_fn)._stream_loop_response(
        messages=[], system_prompt="s", tools=[], model_url="u", model_name="m", is_voice=False
    )
    events = await _collect_sse(resp)
    assert events == [("content", "Partial "), ("stop", None), ("done", None)]


@_skip_no_deps
@pytest.mark.asyncio
async def test_relay_error_without_emission_uses_fallback():
    """Loop failure with nothing emitted -> fallback model result streams as
    one chunk (parity with the buffered path's _unified_fallback)."""
    calls = {"n": 0}

    async def loop_fn(**kwargs):
        calls["n"] += 1
        if "on_delta" in kwargs and kwargs.get("on_delta") is not None:
            raise RuntimeError("primary died")
        return "Fallback says hi"

    resp = _make_brain(loop_fn, fallback_url="http://fb")._stream_loop_response(
        messages=[], system_prompt="s", tools=[], model_url="u", model_name="m", is_voice=False
    )
    events = await _collect_sse(resp)
    assert events == [("content", "Fallback says hi"), ("stop", None), ("done", None)]
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Review-fix coverage: stream-outcome counter + fallback messages/is_voice
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_died_mid_emission_increments_outcome_counter():
    """A stream that dies AFTER emitting increments the died_mid_emission
    outcome (the silent-truncation signal the ChatStreamTruncating alert reads)."""
    from orchestrator.metrics import CHAT_STREAM_OUTCOME

    child = CHAT_STREAM_OUTCOME.labels(outcome="died_mid_emission")
    before = child._value.get()
    # Emit one chunk (i=0), then raise at i=1 → emitted>0 with an error = death
    # after emission, the branch that returns the partial answer.
    backend = FakeStreamBackend([_content_chunk("Partial answer"), _content_chunk(" more")], raise_after=1)
    result, deltas = await _run_loop(backend)
    assert "Partial answer" in "".join(deltas)
    assert child._value.get() == before + 1


@_skip_no_deps
@pytest.mark.asyncio
async def test_pre_emission_retry_increments_outcome_counter():
    """A stream that errors before any token reaches the client counts as
    pre_emission_retry and falls back to a buffered round."""
    from orchestrator.metrics import CHAT_STREAM_OUTCOME

    child = CHAT_STREAM_OUTCOME.labels(outcome="pre_emission_retry")
    before = child._value.get()
    backend = FakeStreamBackend([_content_chunk("x")], raise_after=0)
    call_model = AsyncMock(return_value={"choices": [{"message": {"content": "buffered", "tool_calls": None}}]})
    result, _deltas = await _run_loop(backend, call_model_mock=call_model)
    assert result == "buffered"
    assert child._value.get() == before + 1


@_skip_no_deps
@pytest.mark.asyncio
async def test_relay_fallback_snapshots_messages_and_passes_is_voice():
    """The in-producer buffered fallback runs on the ORIGINAL messages (not the
    primary loop's in-place appends) and carries is_voice — parity with the
    buffered _unified_fallback, and it bumps loop_failed_fallback."""
    from orchestrator.metrics import CHAT_STREAM_OUTCOME

    child = CHAT_STREAM_OUTCOME.labels(outcome="loop_failed_fallback")
    before = child._value.get()
    captured = {}

    async def loop_fn(**kwargs):
        if kwargs.get("on_delta") is not None:
            # primary: pollute the caller's list, then die before emitting
            kwargs["messages"].append({"role": "assistant", "content": "polluted"})
            raise RuntimeError("primary died")
        captured["messages"] = list(kwargs["messages"])
        captured["is_voice"] = kwargs.get("is_voice")
        return "fb"

    original = [{"role": "user", "content": "hi"}]
    resp = _make_brain(loop_fn, fallback_url="http://fb")._stream_loop_response(
        messages=original, system_prompt="s", tools=[], model_url="u", model_name="m", is_voice=True
    )
    events = await _collect_sse(resp)
    assert events == [("content", "fb"), ("stop", None), ("done", None)]
    # fallback saw the clean 1-message request, not the primary's appended turn
    assert captured["messages"] == [{"role": "user", "content": "hi"}]
    assert captured["is_voice"] is True
    assert child._value.get() == before + 1


# ---------------------------------------------------------------------------
# Attributed sentinel-tag hardening (fix/streamgate-attributed-tags)
#
# clean_response + StreamGate must suppress sentinel tags that carry
# attributes (`<think reason=1>`, `<tool_call id="a">`), which previously
# leaked because only the literal bare tags were matched. These tests pin the
# deterministic behaviour and then fuzz for chunk-invariance and leaks.
# ---------------------------------------------------------------------------


def _clean():
    from orchestrator.unified_loop import clean_response

    return clean_response


# --- 1. clean_response deterministic cases ---------------------------------


@_skip_no_deps
def test_clean_strips_attributed_think():
    cr = _clean()
    assert cr("keep <think reason=1>hidden</think> end") == "keep  end"


@_skip_no_deps
def test_clean_strips_attributed_tool_call():
    cr = _clean()
    assert cr('a <tool_call id="a">{}</tool_call> b') == "a  b"


@_skip_no_deps
def test_clean_strips_plain_think():
    cr = _clean()
    assert cr("a <think>x</think> b") == "a  b"


@_skip_no_deps
def test_clean_keeps_thinker_lookalike():
    cr = _clean()
    # <thinker> must NOT be treated as <think> (word boundary).
    assert cr("<thinker>keep</thinker>") == "<thinker>keep</thinker>"


@_skip_no_deps
def test_clean_keeps_tool_calls_lookalike():
    cr = _clean()
    assert cr("<tool_calls>keep</tool_calls>") == "<tool_calls>keep</tool_calls>"


@_skip_no_deps
def test_clean_strips_multiline_think_dotall():
    cr = _clean()
    assert cr("a <think>line1\nline2\nline3</think> b") == "a  b"


@_skip_no_deps
def test_clean_is_case_sensitive_uppercase_not_stripped():
    cr = _clean()
    # Documented intentional: uppercase sentinels are left alone.
    assert cr("<THINK>x</THINK>") == "<THINK>x</THINK>"


@_skip_no_deps
def test_clean_allows_whitespace_in_closer():
    cr = _clean()
    assert cr("a <think>x</think > b") == "a  b"


# --- 2. StreamGate whole-input parity with clean_response ------------------


@_skip_no_deps
@pytest.mark.parametrize(
    "text,expected",
    [
        ("keep <think reason=1>hidden</think> end", "keep  end"),
        ('a <tool_call id="a">{}</tool_call> b', "a  b"),
        ("a <think>x</think> b", "a  b"),
        ("<thinker>keep</thinker>", "<thinker>keep</thinker>"),
        ("<tool_calls>keep</tool_calls>", "<tool_calls>keep</tool_calls>"),
        ("a <think>l1\nl2</think> b", "a  b"),
    ],
)
def test_gate_whole_input_matches_expected(text, expected):
    g = _gate()
    assert g.feed(text) + g.flush() == expected


# --- 3. StreamGate streaming: attributed blocks split char-by-char ---------


@_skip_no_deps
def test_gate_attributed_tool_call_char_by_char():
    g = _gate()
    text = 'before <tool_call id="1">{"name":"x"}</tool_call> after'
    out = "".join(g.feed(c) for c in text) + g.flush()
    assert out == "before  after"
    assert "tool_call" not in out
    assert '"name"' not in out


@_skip_no_deps
def test_gate_attributed_think_char_by_char():
    g = _gate()
    text = "before <think foo=bar>secret reasoning</think> after"
    out = "".join(g.feed(c) for c in text) + g.flush()
    assert out == "before  after"
    assert "secret" not in out


# --- 4. lookalikes / bare '<' pass through char-by-char --------------------


@_skip_no_deps
@pytest.mark.parametrize(
    "text",
    [
        "<thinker>keep</thinker>",
        "<tool_calls>keep</tool_calls>",
        "3 < 5",
        "a < b",
    ],
)
def test_gate_lookalikes_pass_through_char_by_char(text):
    g = _gate()
    out = "".join(g.feed(c) for c in text) + g.flush()
    assert out == text


# --- 5. Hold-back cap: over-long unterminated tag is emitted, not swallowed -


@_skip_no_deps
def test_gate_holdback_cap_whole_input_emits():
    g = _gate()
    text = "<think " + "a" * 300  # no '>' ever
    out = g.feed(text) + g.flush()
    assert out == text, "over-long unterminated <think ... run must be emitted, not swallowed"


@_skip_no_deps
def test_gate_holdback_cap_char_by_char_emits():
    g = _gate()
    text = "<think " + "a" * 300  # no '>' ever
    out = "".join(g.feed(c) for c in text) + g.flush()
    assert out == text, "over-long unterminated <think ... run must be emitted, not swallowed"


# --- 5b. Long-attribute tags stay chunk-invariant (cap boundary) -----------
# The random fuzzer caps at ~14 tokens, so it never generates a tag whose
# attributes exceed the regex bound / hold-back cap (256/320). That's exactly
# where a naive cap diverges: whole-input regex-matches and suppresses, while a
# chunked feed trips the cap and EMITS (leaks) the block. These deterministic
# cases lock whole == char-by-char at and beyond the boundary.


@_skip_no_deps
def test_gate_long_attribute_tag_chunk_invariant():
    from orchestrator.unified_loop import StreamGate

    def whole(s):
        g = StreamGate()
        return g.feed(s) + g.flush()

    def per_char(s):
        g = StreamGate()
        return "".join(g.feed(c) for c in s) + g.flush()

    cases = [
        "<think " + "a" * 300 + ">hidden</think> tail",  # > bound attrs, has closer
        "<think " + "a" * 400 + ">x</think>END",  # > cap attrs
        "<think " + "a" * 256 + ">b</think>Z",  # exactly at bound
        "<think " + "a" * 257 + ">b</think>Z",  # just over bound
        "<think " + "a" * 500,  # unterminated flood, no '>'
        "<tool_call " + "b" * 300 + ">{}</tool_call> ok",
    ]
    for s in cases:
        w, c = whole(s), per_char(s)
        assert w == c, f"cap-boundary divergence for {s[:30]!r}...: whole={w!r} chunk={c!r}"


# --- 6. Unterminated REAL open tag stays suppressed (never leaks) ----------


@_skip_no_deps
def test_gate_unterminated_real_tag_never_leaks():
    g = _gate()
    fed = g.feed("<think>secret")
    flushed = g.flush()
    assert fed == ""
    assert flushed == ""
    assert "secret" not in (fed + flushed)


# ---------------------------------------------------------------------------
# FUZZ TESTS (seeded random — no hypothesis dependency; deterministic; <5s)
#
# Property 1 (chunk-invariance): for any input, feeding a StreamGate in
#   arbitrary random chunk boundaries yields the SAME output as one feed(),
#   which also equals a single feed()+flush() of the whole string.
# Property 2 (no-leak): clean_response(s) never contains a complete
#   <think ...>...</think> or <tool_call ...>...</tool_call> pair.
# ---------------------------------------------------------------------------

import random  # noqa: E402
import re as _re  # noqa: E402

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
    n = rng.randint(0, max_tokens)
    return "".join(rng.choice(_FUZZ_TOKENS) for _ in range(n))


def _feed_in_random_chunks(gate_factory, s, rng):
    g = gate_factory()
    out = []
    i = 0
    n = len(s)
    while i < n:
        step = rng.randint(1, max(1, n - i))
        out.append(g.feed(s[i : i + step]))
        i += step
    out.append(g.flush())
    return "".join(out)


def _feed_whole(gate_factory, s):
    g = gate_factory()
    return g.feed(s) + g.flush()


@_skip_no_deps
def test_fuzz_stream_gate_chunk_invariance():
    from orchestrator.unified_loop import StreamGate

    rng = random.Random(1337)
    for _ in range(400):
        s = _random_fuzz_string(rng)
        whole = _feed_whole(StreamGate, s)
        # Re-seed the chunking RNG deterministically per-string is unnecessary;
        # the outer rng drives everything and the fixed seed keeps it stable.
        chunked = _feed_in_random_chunks(StreamGate, s, rng)
        assert chunked == whole, f"chunk-invariance violation for {s!r}: chunked={chunked!r} whole={whole!r}"


@_skip_no_deps
def test_fuzz_clean_response_never_leaks_complete_pair():
    from orchestrator.unified_loop import clean_response

    think_pair = _re.compile(r"<think\b[^>]*>.*?</think\s*>", _re.DOTALL)
    tool_pair = _re.compile(r"<tool_call\b[^>]*>.*?</tool_call\s*>", _re.DOTALL)
    rng = random.Random(2024)
    for _ in range(400):
        s = _random_fuzz_string(rng)
        cleaned = clean_response(s)
        assert not think_pair.search(cleaned), f"leaked <think> pair from {s!r}: {cleaned!r}"
        assert not tool_pair.search(cleaned), f"leaked <tool_call> pair from {s!r}: {cleaned!r}"
