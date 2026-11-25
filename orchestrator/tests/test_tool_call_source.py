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

from unittest.mock import AsyncMock

import pytest

from orchestrator import unified_loop
from orchestrator.metrics import TOOL_CALL_SOURCE
from orchestrator.tests._helpers import metric_value
from orchestrator.tests.fixtures_loop import (
    SSE_DONE,
    FakeStreamBackend,
    content_chunk,
    reasoning_chunk,
    reply,
    run_loop,
    run_round,
    run_stream_loop,
    stop_chunk,
    tool_call,
    tools_named,
)

SOURCES = ("native", "xml_fallback", "dropped", "none")
TOOLS = tools_named("web_search")
XML_CALL = '<tool_call>{"name": "web_search", "arguments": {"query": "x"}}</tool_call>'


def _counts():
    return {s: metric_value(TOOL_CALL_SOURCE, source=s) for s in SOURCES}


def _delta(before):
    after = _counts()
    return {s: after[s] - before[s] for s in SOURCES}


def _call(call_id="call_0", query="x"):
    return tool_call("web_search", {"query": query}, call_id)


async def _run(responses):
    """The buffered loop against a canned list of LLM responses."""
    return await run_loop(AsyncMock(side_effect=list(responses)), AsyncMock(return_value="tool ok"), tools=TOOLS)


# ---------------------------------------------------------------------------
# 1. Marker regex
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, matches",
    [
        pytest.param("before <tool_call> after", True, id="plain"),
        pytest.param('<tool_call id="x">', True, id="attributes"),
        # `<tool_calls>` is legitimate user-visible prose (see the clean_response table).
        pytest.param("<tool_calls>keep</tool_calls>", False, id="plural-lookalike"),
        pytest.param("just some ordinary text", False, id="no-marker"),
    ],
)
def test_marker_regex(text, matches):
    assert bool(unified_loop._TOOL_CALL_MARKER_RE.search(text)) is matches


# ---------------------------------------------------------------------------
# 2 + 3. Classification on the buffered path, exactly once per round
# ---------------------------------------------------------------------------


async def test_dropped_marker_in_content():
    """Marker present in content but undecodable — no call parsed."""
    before = _counts()
    await _run([reply("<tool_call>web_search\n<arg>x")])
    assert _delta(before)["dropped"] == 1


async def test_dropped_when_marker_only_in_reasoning():
    """THE defect signature: empty content, empty tool_calls, finish_reason=stop,
    and the <tool_call> tag stranded in the reasoning channel."""
    before = _counts()
    await _run([reply("", reasoning_content='I should check.\n<tool_call>\n{"name": "web_search"}')])
    d = _delta(before)
    assert d["dropped"] == 1
    assert d["none"] == 0


async def test_plural_lookalike_in_content_counts_as_none():
    """Talking ABOUT <tool_calls> must not inflate `dropped`."""
    before = _counts()
    await _run([reply("The API returns empty <tool_calls> arrays.")])
    d = _delta(before)
    assert d["dropped"] == 0
    assert d["none"] == 1


async def test_two_round_turn_counts_each_round_once():
    before = _counts()
    await _run([reply(tool_calls=[_call()]), reply("all done")])
    assert _delta(before) == {"native": 1, "xml_fallback": 0, "dropped": 0, "none": 1}


async def test_xml_fallback_does_not_also_bump_native():
    before = _counts()
    await _run([reply(XML_CALL), reply("done")])
    assert _delta(before) == {"native": 0, "xml_fallback": 1, "dropped": 0, "none": 1}


async def test_three_rounds_total_three_increments():
    before = _counts()
    await _run([reply(tool_calls=[_call("a")]), reply(tool_calls=[_call("b", "y")]), reply("finished")])
    d = _delta(before)
    assert sum(d.values()) == 3
    assert d["native"] == 2
    assert d["none"] == 1


# ---------------------------------------------------------------------------
# 4. _stream_model_round — reasoning marker across deltas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lines, expected",
    [
        # The tag routinely arrives split ("<tool" + "_call>") — a per-delta
        # membership test would miss it and the counter would read zero.
        pytest.param(
            [reasoning_chunk("I should call "), reasoning_chunk("<tool"), reasoning_chunk("_call>")], True, id="split"
        ),
        pytest.param([reasoning_chunk("<tool_call>{}")], True, id="whole"),
        pytest.param([reasoning_chunk("the model returned empty <tool_calls>")], False, id="plural-lookalike"),
        pytest.param([content_chunk("Hello!")], False, id="no-reasoning"),
    ],
)
async def test_stream_round_reasoning_marker(lines, expected):
    result, _ = await run_round(FakeStreamBackend([*lines, stop_chunk(), SSE_DONE]))
    assert result["reasoning_has_tool_marker"] is expected


async def test_streaming_path_dropped_uses_precomputed_boolean():
    """End-to-end on the streaming path: reasoning-only marker, no content,
    no tool calls -> `dropped`. Without the `_reasoning_has_tool_marker`
    passthrough this can never fire on the default interactive path."""
    backend = FakeStreamBackend([reasoning_chunk("<tool"), reasoning_chunk("_call>"), stop_chunk(), SSE_DONE])
    before = _counts()
    await run_stream_loop(backend, execute_tool=AsyncMock(), tools=TOOLS, max_rounds=3)
    d = _delta(before)
    assert d["dropped"] == 1
    assert d["none"] == 0


# ---------------------------------------------------------------------------
# 5. Instrumentation failure is contained
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "responses, answer",
    [
        pytest.param([reply("Still answered.")], "Still answered.", id="text-turn"),
        pytest.param([reply(tool_calls=[_call()]), reply("tool ran fine")], "tool ran fine", id="tool-round"),
    ],
)
async def test_counter_failure_does_not_break_the_turn(monkeypatch, responses, answer):
    def _boom(*_a, **_kw):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(unified_loop.TOOL_CALL_SOURCE, "labels", _boom)
    assert await _run(responses) == answer
