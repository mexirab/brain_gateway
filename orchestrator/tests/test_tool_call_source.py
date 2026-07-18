"""
Tests for the TOOL_CALL_SOURCE instrumentation (metrics.py) and its
classification block in unified_loop.run_unified_tool_loop.

Measures the vLLM<0.20.0 qwen3 reasoning-parser defect (upstream PR #35687):
Qwen3.6 emits <tool_call> inside an UNCLOSED <think> block, vLLM routes the
whole span to the reasoning channel, and the API returns empty tool_calls +
empty content + finish_reason="stop". The turn silently ends mid-task.

The highest-value cases here are:
  - `dropped` firing when the marker exists ONLY in the reasoning channel
    (the actual defect signature — a naive implementation misses it), and
  - _stream_model_round detecting a <tool_call> tag SPLIT ACROSS SSE deltas
    (the tag routinely arrives as "<tool" + "_call>").
A regression in either silently returns the counter to reading zero, which is
worse than having no counter at all.

Counter reads take a before/after delta: the module-level Prometheus registry
persists across tests in a session, so a zero start cannot be assumed.
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
# Counter helpers
# ---------------------------------------------------------------------------

SOURCES = ("native", "xml_fallback", "dropped", "none")


def _counts():
    """Snapshot of every TOOL_CALL_SOURCE label value."""
    from orchestrator.metrics import TOOL_CALL_SOURCE

    return {s: TOOL_CALL_SOURCE.labels(source=s)._value.get() for s in SOURCES}


def _delta(before, after):
    return {s: after[s] - before[s] for s in SOURCES}


# ---------------------------------------------------------------------------
# Loop helpers (mirrors test_unified_loop.py conventions)
# ---------------------------------------------------------------------------

SAMPLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
    },
]


def _make_tool_call(name="web_search", arguments=None, call_id="call_0"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments or {"query": "x"})},
    }


def _resp(content="", tool_calls=None, reasoning_content=None, finish_reason="stop"):
    message = {"content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    return {"choices": [{"message": message, "finish_reason": finish_reason}]}


async def _run_loop(responses, tool_result="tool ok"):
    """Run the buffered loop against a canned list of LLM responses."""
    from orchestrator.unified_loop import run_unified_tool_loop

    mock_call_model = AsyncMock(side_effect=list(responses))
    mock_exec = AsyncMock(return_value=tool_result)

    with (
        patch("orchestrator.orchestrator.call_model", mock_call_model),
        patch("orchestrator.tool_handlers.execute_tool", mock_exec),
    ):
        return await run_unified_tool_loop(
            messages=[{"role": "user", "content": "hi"}],
            system_prompt="sys",
            tools=SAMPLE_TOOLS,
            model_url="http://fake:8080/v1",
            model_name="test-model",
            http_client=None,
            max_rounds=5,
        )


# ---------------------------------------------------------------------------
# 1. Marker regex
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestMarkerRegex:
    def test_plain_open_tag_matches(self):
        from orchestrator.unified_loop import _TOOL_CALL_MARKER_RE

        assert _TOOL_CALL_MARKER_RE.search("before <tool_call> after")

    def test_open_tag_with_attributes_matches(self):
        from orchestrator.unified_loop import _TOOL_CALL_MARKER_RE

        assert _TOOL_CALL_MARKER_RE.search('<tool_call id="x">')

    def test_plural_lookalike_does_not_match(self):
        """`<tool_calls>` is legitimate user-visible prose — see
        test_real_streaming.py::test_clean_keeps_tool_calls_lookalike."""
        from orchestrator.unified_loop import _TOOL_CALL_MARKER_RE

        assert _TOOL_CALL_MARKER_RE.search("<tool_calls>keep</tool_calls>") is None

    def test_no_marker(self):
        from orchestrator.unified_loop import _TOOL_CALL_MARKER_RE

        assert _TOOL_CALL_MARKER_RE.search("just some ordinary text") is None


# ---------------------------------------------------------------------------
# 2 + 3. Classification on the buffered path
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestClassification:
    @pytest.mark.asyncio
    async def test_native_tool_call(self):
        before = _counts()
        await _run_loop([_resp(tool_calls=[_make_tool_call()]), _resp(content="done")])
        d = _delta(before, _counts())
        assert d["native"] == 1

    @pytest.mark.asyncio
    async def test_xml_fallback(self):
        content = '<tool_call>{"name": "web_search", "arguments": {"query": "x"}}</tool_call>'
        before = _counts()
        await _run_loop([_resp(content=content), _resp(content="done")])
        d = _delta(before, _counts())
        assert d["xml_fallback"] == 1

    @pytest.mark.asyncio
    async def test_dropped_marker_in_content(self):
        """Marker present in content but undecodable — no call parsed."""
        before = _counts()
        await _run_loop([_resp(content="<tool_call>web_search\n<arg>x")])
        d = _delta(before, _counts())
        assert d["dropped"] == 1

    @pytest.mark.asyncio
    async def test_none_when_plain_text(self):
        before = _counts()
        await _run_loop([_resp(content="Just a plain answer.")])
        d = _delta(before, _counts())
        assert d["none"] == 1

    @pytest.mark.asyncio
    async def test_dropped_when_marker_only_in_reasoning(self):
        """THE defect signature: empty content, empty tool_calls,
        finish_reason=stop, and the <tool_call> tag stranded in the
        reasoning channel."""
        before = _counts()
        await _run_loop(
            [
                _resp(
                    content="",
                    reasoning_content='I should check.\n<tool_call>\n{"name": "web_search"}',
                    finish_reason="stop",
                )
            ]
        )
        d = _delta(before, _counts())
        assert d["dropped"] == 1
        assert d["none"] == 0

    @pytest.mark.asyncio
    async def test_plural_lookalike_in_content_counts_as_none(self):
        """Talking ABOUT <tool_calls> must not inflate `dropped`."""
        before = _counts()
        await _run_loop([_resp(content="The API returns empty <tool_calls> arrays.")])
        d = _delta(before, _counts())
        assert d["dropped"] == 0
        assert d["none"] == 1


# ---------------------------------------------------------------------------
# 4. _stream_model_round — reasoning marker across deltas
# ---------------------------------------------------------------------------


def _sse(obj):
    return f"data: {json.dumps(obj)}\n\n"


def _reasoning_chunk(text):
    return _sse({"choices": [{"delta": {"reasoning_content": text}, "finish_reason": None}]})


def _content_chunk(text):
    return _sse({"choices": [{"delta": {"content": text}, "finish_reason": None}]})


def _stop_chunk():
    return _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]})


class FakeStreamBackend:
    def __init__(self, lines):
        self._lines = lines

    async def stream_chat_completion(
        self, messages, system="", timeout=180, tools=None, tool_choice="auto", extra_body=None, **kwargs
    ):
        for line in self._lines:
            yield line


async def _round(lines):
    from orchestrator.unified_loop import _stream_model_round

    async def emit(_text):
        return None

    return await _stream_model_round(FakeStreamBackend(lines), [], "sys", None, "auto", 120, None, emit, "TEST")


@_skip_no_deps
class TestStreamRoundReasoningMarker:
    @pytest.mark.asyncio
    async def test_marker_split_across_deltas(self):
        """The tag routinely arrives split ("<tool" + "_call>") — a per-delta
        membership test would miss it and the counter would read zero."""
        result = await _round(
            [
                _reasoning_chunk("I should call "),
                _reasoning_chunk("<tool"),
                _reasoning_chunk("_call>"),
                _stop_chunk(),
                "data: [DONE]\n\n",
            ]
        )
        assert result["reasoning_has_tool_marker"] is True

    @pytest.mark.asyncio
    async def test_marker_whole_in_one_delta(self):
        result = await _round([_reasoning_chunk("<tool_call>{}"), _stop_chunk(), "data: [DONE]\n\n"])
        assert result["reasoning_has_tool_marker"] is True

    @pytest.mark.asyncio
    async def test_plural_lookalike_in_reasoning_is_false(self):
        result = await _round(
            [_reasoning_chunk("the model returned empty <tool_calls>"), _stop_chunk(), "data: [DONE]\n\n"]
        )
        assert result["reasoning_has_tool_marker"] is False

    @pytest.mark.asyncio
    async def test_no_reasoning_at_all_is_false(self):
        result = await _round([_content_chunk("Hello!"), _stop_chunk(), "data: [DONE]\n\n"])
        assert result["reasoning_has_tool_marker"] is False


@_skip_no_deps
@pytest.mark.asyncio
async def test_streaming_path_dropped_uses_precomputed_boolean():
    """End-to-end on the streaming path: reasoning-only marker, no content,
    no tool calls -> `dropped`. Without the `_reasoning_has_tool_marker`
    passthrough this can never fire on the default interactive path."""
    from orchestrator.unified_loop import run_unified_tool_loop

    backend = FakeStreamBackend(
        [_reasoning_chunk("<tool"), _reasoning_chunk("_call>"), _stop_chunk(), "data: [DONE]\n\n"]
    )
    deltas = []

    async def on_delta(text):
        deltas.append(text)

    before = _counts()
    with (
        patch("orchestrator.orchestrator.get_stream_capable_backend", return_value=backend),
        patch("orchestrator.tool_handlers.execute_tool", new_callable=AsyncMock),
    ):
        await run_unified_tool_loop(
            messages=[{"role": "user", "content": "hi"}],
            system_prompt="sys",
            tools=SAMPLE_TOOLS,
            model_url="http://fake:8080/v1",
            model_name="test-model",
            http_client=None,
            max_rounds=3,
            on_delta=on_delta,
        )
    d = _delta(before, _counts())
    assert d["dropped"] == 1
    assert d["none"] == 0


# ---------------------------------------------------------------------------
# 5. Exactly once per round
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestExactlyOncePerRound:
    @pytest.mark.asyncio
    async def test_two_round_turn_counts_each_round_once(self):
        before = _counts()
        await _run_loop([_resp(tool_calls=[_make_tool_call()]), _resp(content="all done")])
        d = _delta(before, _counts())
        assert d == {"native": 1, "xml_fallback": 0, "dropped": 0, "none": 1}

    @pytest.mark.asyncio
    async def test_xml_fallback_does_not_also_bump_native(self):
        content = '<tool_call>{"name": "web_search", "arguments": {"query": "x"}}</tool_call>'
        before = _counts()
        await _run_loop([_resp(content=content), _resp(content="done")])
        d = _delta(before, _counts())
        assert d == {"native": 0, "xml_fallback": 1, "dropped": 0, "none": 1}

    @pytest.mark.asyncio
    async def test_three_rounds_total_three_increments(self):
        before = _counts()
        await _run_loop(
            [
                _resp(tool_calls=[_make_tool_call(call_id="a")]),
                _resp(tool_calls=[_make_tool_call(call_id="b", arguments={"query": "y"})]),
                _resp(content="finished"),
            ]
        )
        d = _delta(before, _counts())
        assert sum(d.values()) == 3
        assert d["native"] == 2
        assert d["none"] == 1


# ---------------------------------------------------------------------------
# 6. Instrumentation failure is contained
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_counter_failure_does_not_break_the_turn(monkeypatch):
    from orchestrator import unified_loop

    def _boom(*_a, **_kw):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(unified_loop.TOOL_CALL_SOURCE, "labels", _boom)

    result = await _run_loop([_resp(content="Still answered.")])
    assert result == "Still answered."


@_skip_no_deps
@pytest.mark.asyncio
async def test_counter_failure_does_not_break_a_tool_round(monkeypatch):
    from orchestrator import unified_loop

    def _boom(*_a, **_kw):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(unified_loop.TOOL_CALL_SOURCE, "labels", _boom)

    result = await _run_loop([_resp(tool_calls=[_make_tool_call()]), _resp(content="tool ran fine")])
    assert result == "tool ran fine"
