"""
Tests for the 2026-10-14 empty-reply fix (branch fix/voice-empty-replies):

- ``run_unified_tool_loop`` retries ONCE when a round returns no text and no
  tool call — with thinking re-enabled on voice — in its own extra round, and
  text-only when the retry follows tool execution;
- ``bgw_llm_empty_reply_total{outcome}`` counts retried / recovered / still_empty;
- the selfcare safety net still fires when the answer arrived on the retry;
- the streaming path emits the recovered text without a leading separator;
- ``cloud_brain._is_empty_assistant`` (the unified-path drop of blank
  assistant turns from client history is covered on every loop path in
  ``test_client_message_strip_paths.py``);
- ``VOICE_THINKING_ENABLED``: what the empty-reply retry changes for each
  setting, and that the switch is read per turn. The voice payload on the
  wire (off = byte-identical to the pre-switch literal, on = 2048 tokens and
  no ``chat_template_kwargs``) is pinned in ``test_llm_backend.py``.
"""

import copy
import logging
from unittest.mock import AsyncMock

import pytest

from orchestrator import shared
from orchestrator.cloud_brain import _is_empty_assistant
from orchestrator.metrics import EMPTY_REPLY_TOTAL
from orchestrator.tests._helpers import metric_value
from orchestrator.tests.fixtures_loop import (
    SequencedBackend,
    content_chunk,
    reply,
    run_loop,
    run_stream_loop,
    stop_chunk,
    tool_call,
    tools_named,
)

TOOLS = tools_named("focus_status", "web_search", "home_assistant", "selfcare_log")

LONG_PROSE = (
    "Here is what I found on that page. The author explains at length how the assistant's "
    "tool-calling protocol works, including a verbatim example of the markup the model emits "
    "when it wants to check the focus timer, which looks like this: "
)
FOCUS_BLOCK = '<tool_call>{"name":"focus_status","arguments":{}}</tool_call>'

# The voice payload with the switch off (the pre-switch literal) and on.
_OLD_VOICE_EXTRA = {"max_tokens": 1024, "chat_template_kwargs": {"enable_thinking": False}}
_THINKING_VOICE_EXTRA = {"max_tokens": 2048}
_LOOP_LOGGER = "orchestrator.unified_loop"
_STILL_EMPTY_WARNING = "[UNIFIED] Empty reply after retry — user got no answer (finish_reason={})"
OUTCOMES = ("retried", "recovered", "still_empty")


def _snapshot():
    return {o: metric_value(EMPTY_REPLY_TOTAL, outcome=o) for o in OUTCOMES}


def _delta(before):
    return {o: metric_value(EMPTY_REPLY_TOTAL, outcome=o) - v for o, v in before.items()}


@pytest.fixture(autouse=True)
def _voice_thinking_off(monkeypatch):
    """Pin VOICE_THINKING_ENABLED to its default (off) so the voice tests
    below can't flip with the host env; the switch-on tests override it.
    The default itself is asserted in test_config.py."""
    monkeypatch.setattr(shared, "VOICE_THINKING_ENABLED", False)


async def _run(call_model, execute_tool=None, messages=None, max_rounds=5, is_voice=False):
    return await run_loop(
        call_model,
        execute_tool or AsyncMock(),
        messages=messages if messages is not None else [{"role": "user", "content": "hello there"}],
        tools=TOOLS,
        max_rounds=max_rounds,
        is_voice=is_voice,
    )


def _recording_call_model(replies):
    """Buffered call_model mock that snapshots extra_body at call time."""
    queue = list(replies)
    seen = []

    async def _call(*args, **kwargs):
        seen.append(copy.deepcopy(kwargs.get("extra_body")))
        return queue.pop(0)

    return AsyncMock(side_effect=_call), seen


async def _turn(path, rounds, is_voice=False):
    """One turn whose model rounds are ``rounds`` = [(text, finish_reason), ...],
    buffered or streamed. Returns (result, extra_body of each round, deltas)."""
    if path == "buffered":
        call_model, seen = _recording_call_model([reply(text, finish_reason=f) for text, f in rounds])
        return await _run(call_model, is_voice=is_voice), seen, None
    backend = SequencedBackend(
        [[content_chunk(text), stop_chunk(f)] if text else [stop_chunk(f)] for text, f in rounds]
    )
    result, deltas = await run_stream_loop(backend, tools=TOOLS, is_voice=is_voice)
    return result, backend.extra_bodies, deltas


def _loop_messages(caplog, level=None):
    return [r.getMessage() for r in caplog.records if r.name == _LOOP_LOGGER and (level is None or r.levelno == level)]


def _retry_logs(caplog):
    return [m for m in _loop_messages(caplog) if "retrying once" in m]


PATHS = pytest.mark.parametrize("path", ["buffered", "stream"])


# ---------------------------------------------------------------------------
# 1-3: retry + counters + round budget
# ---------------------------------------------------------------------------


@PATHS
async def test_empty_then_text_recovers_and_counts(path):
    before = _snapshot()

    result, extras, deltas = await _turn(path, [("", "stop"), ("hello", "stop")])

    assert result == "hello"
    assert len(extras) == 2
    assert _delta(before) == {"retried": 1, "recovered": 1, "still_empty": 0}
    if deltas is not None:
        assert "".join(deltas) == "hello"  # no leading separator on the stream


@pytest.mark.parametrize("is_voice", [False, True], ids=["typed", "voice_switch_off"])
async def test_two_empty_rounds_return_empty_and_count_still_empty(caplog, is_voice):
    call_model = AsyncMock(side_effect=[reply(""), reply(""), reply("never reached")])
    before = _snapshot()

    with caplog.at_level(logging.INFO, logger=_LOOP_LOGGER):
        result = await _run(call_model, is_voice=is_voice)

    assert result == ""
    assert call_model.call_count == 2
    assert _delta(before) == {"retried": 1, "recovered": 0, "still_empty": 1}
    # One wording for both settings: it no longer claims a "thinking" retry.
    assert _loop_messages(caplog, logging.WARNING) == [_STILL_EMPTY_WARNING.format("stop")]


async def test_retry_gets_own_round_when_max_rounds_is_one():
    call_model = AsyncMock(side_effect=[reply(""), reply("hello")])

    result = await _run(call_model, max_rounds=1)

    assert result == "hello"
    assert "complexity" not in result
    assert call_model.call_count == 2


# ---------------------------------------------------------------------------
# 4: post-tool retry is text-only
# ---------------------------------------------------------------------------


async def test_post_tool_retry_ignores_new_tool_calls():
    call_model = AsyncMock(
        side_effect=[
            reply("", tool_calls=[tool_call("web_search", {"query": "weather"}, "c1")]),
            reply(""),
            reply(
                "It is sunny.",
                tool_calls=[tool_call("home_assistant", {"entity_id": "light.room_1", "service": "turn_on"}, "c2")],
            ),
            reply("should not be reached"),
        ]
    )
    execute_tool = AsyncMock(return_value="75F sunny")
    before = _snapshot()

    result = await _run(call_model, execute_tool)

    assert result == "It is sunny."
    execute_tool.assert_called_once_with("web_search", {"query": "weather"})
    assert call_model.call_count == 3
    assert _delta(before) == {"retried": 1, "recovered": 1, "still_empty": 0}


async def test_post_tool_retry_repeating_same_call_not_reexecuted():
    """Same call as round 0 in the retry: still only one execution, text returned."""
    same = tool_call("web_search", {"query": "weather"}, "c1")
    call_model = AsyncMock(side_effect=[reply("", tool_calls=[same]), reply(""), reply("Sunny.", tool_calls=[same])])
    execute_tool = AsyncMock(return_value="75F sunny")

    result = await _run(call_model, execute_tool)

    assert result == "Sunny."
    assert execute_tool.call_count == 1


# ---------------------------------------------------------------------------
# 5: what counts as empty
# ---------------------------------------------------------------------------


async def test_think_only_prefill_counts_as_empty():
    call_model = AsyncMock(side_effect=[reply("<think>\n\n</think>"), reply("hello")])
    before = _snapshot()

    result = await _run(call_model)

    assert result == "hello"
    assert _delta(before)["retried"] == 1


async def test_rejected_tool_call_echo_in_prose_not_retried():
    call_model = AsyncMock(side_effect=[reply(LONG_PROSE + FOCUS_BLOCK), reply("never reached")])
    execute_tool = AsyncMock()
    before = _snapshot()

    result = await _run(call_model, execute_tool)

    assert call_model.call_count == 1
    execute_tool.assert_not_called()
    assert "Here is what I found" in result
    assert _delta(before)["retried"] == 0


async def test_length_truncated_bare_tool_call_not_retried():
    """A bare block cut off by length is rejected by the XML gate; clean text is
    empty, but the '<tool_call>' guard means it must not be retried."""
    call_model = AsyncMock(side_effect=[reply(FOCUS_BLOCK, finish_reason="length"), reply("never")])
    before = _snapshot()

    await _run(call_model)

    assert call_model.call_count == 1
    assert _delta(before)["retried"] == 0


async def test_dropped_tool_call_is_not_retried():
    """A tool-call marker that reached only the reasoning (no parsed call, no
    content) is the 'dropped' fault with its own metric/alert — not an
    EOS-only reply — so it must not be retried (CI caught a double count)."""
    call_model = AsyncMock(side_effect=[reply("", reasoning_content="<tool_call>"), reply("never reached")])
    before = _snapshot()

    result = await _run(call_model)

    assert call_model.call_count == 1
    assert result == ""
    assert _delta(before)["retried"] == 0
    assert _delta(before)["still_empty"] == 0  # covered by the dropped metric/alert instead


# ---------------------------------------------------------------------------
# 6: selfcare safety net after a retry
# ---------------------------------------------------------------------------

MEDS_MESSAGE = [{"role": "user", "content": "I took my meds this morning"}]


async def test_selfcare_safety_net_fires_after_retry():
    call_model = AsyncMock(side_effect=[reply(""), reply("Nice, noted.")])
    execute_tool = AsyncMock(return_value="logged")

    result = await _run(call_model, execute_tool, messages=list(MEDS_MESSAGE))

    assert result == "Nice, noted."
    execute_tool.assert_called_once()
    assert execute_tool.call_args.args[0] == "selfcare_log"
    assert execute_tool.call_args.args[1]["action"] == "medication"


async def test_selfcare_safety_net_fires_without_retry_baseline():
    execute_tool = AsyncMock(return_value="logged")
    await _run(AsyncMock(side_effect=[reply("Nice, noted.")]), execute_tool, messages=list(MEDS_MESSAGE))
    assert execute_tool.call_args.args[0] == "selfcare_log"


async def test_selfcare_safety_net_not_fired_after_tool_round_plus_retry():
    """effective_round must be 1 after a real tool round, so no auto-log."""
    call_model = AsyncMock(
        side_effect=[reply("", tool_calls=[tool_call("web_search", {"query": "x"}, "c1")]), reply(""), reply("Done.")]
    )
    execute_tool = AsyncMock(return_value="ok")

    await _run(call_model, execute_tool, messages=list(MEDS_MESSAGE))

    assert [c.args[0] for c in execute_tool.call_args_list] == ["web_search"]


# ---------------------------------------------------------------------------
# 7: _is_empty_assistant table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("content", ["", "  ", None, [], [{"type": "text", "text": " "}]])
def test_is_empty_assistant_true(content):
    assert _is_empty_assistant({"role": "assistant", "content": content}) is True


def test_is_empty_assistant_missing_content_key():
    assert _is_empty_assistant({"role": "assistant"}) is True


@pytest.mark.parametrize(
    "msg",
    [
        {"role": "assistant", "content": [{"type": "image_url", "image_url": {"url": "x"}}]},
        {"role": "assistant", "content": [{"type": "text", "text": "x"}]},
        {"role": "assistant", "content": [{"type": "text", "text": 123}]},
        {"role": "assistant", "content": ["not a dict"]},
        {"role": "assistant", "content": [{"type": "text", "text": " "}, {"type": "image_url", "image_url": {}}]},
        {"role": "assistant", "content": "", "tool_calls": [tool_call("web_search", {}, "c1")]},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": ""},
        {"role": "assistant", "content": 42},
    ],
)
def test_is_empty_assistant_false(msg):
    assert _is_empty_assistant(msg) is False


# ---------------------------------------------------------------------------
# 8: VOICE_THINKING_ENABLED switch and the empty-reply retry
# ---------------------------------------------------------------------------


async def test_voice_switch_is_read_per_turn_not_at_import(monkeypatch):
    """Flipping shared.VOICE_THINKING_ENABLED changes the very next turn —
    the loop must not have captured the value at import time."""
    monkeypatch.setattr(shared, "VOICE_THINKING_ENABLED", True)
    on_model, on_seen = _recording_call_model([reply("a")])
    await _run(on_model, is_voice=True)

    monkeypatch.setattr(shared, "VOICE_THINKING_ENABLED", False)
    off_model, off_seen = _recording_call_model([reply("b")])
    await _run(off_model, is_voice=True)

    assert on_seen == [_THINKING_VOICE_EXTRA]
    assert off_seen == [_OLD_VOICE_EXTRA]


@PATHS
@pytest.mark.parametrize(
    "thinking, extras, retry_log, mode_log",
    [
        (
            False,
            [_OLD_VOICE_EXTRA, {"max_tokens": 1024}],
            "[UNIFIED] Empty reply (finish_reason=length) — retrying once with thinking",
            "[UNIFIED] Voice mode: max_tokens=1024, thinking=off",
        ),
        (
            True,
            [_THINKING_VOICE_EXTRA, _THINKING_VOICE_EXTRA],
            "[UNIFIED] Empty reply (finish_reason=length) — retrying once",
            "[UNIFIED] Voice mode: max_tokens=2048, thinking=on",
        ),
    ],
    ids=["switch_off_reenables_thinking", "switch_on_plain_resample"],
)
async def test_voice_empty_reply_retry_payload_and_log(
    monkeypatch, caplog, path, thinking, extras, retry_log, mode_log
):
    monkeypatch.setattr(shared, "VOICE_THINKING_ENABLED", thinking)
    before = _snapshot()

    with caplog.at_level(logging.INFO, logger=_LOOP_LOGGER):
        result, seen, deltas = await _turn(path, [("", "length"), ("hello", "stop")], is_voice=True)

    assert result == "hello"
    if deltas is not None:
        assert "".join(deltas) == "hello"
    assert seen == extras
    assert _retry_logs(caplog) == [retry_log]
    assert mode_log in _loop_messages(caplog)
    assert _delta(before) == {"retried": 1, "recovered": 1, "still_empty": 0}


@pytest.mark.parametrize(
    "thinking, extras, retry_log",
    [
        (
            False,
            [_OLD_VOICE_EXTRA, _OLD_VOICE_EXTRA, {"max_tokens": 1024}],
            "[UNIFIED] Empty reply (finish_reason=stop) — retrying once with thinking (text only)",
        ),
        (
            True,
            [_THINKING_VOICE_EXTRA] * 3,
            "[UNIFIED] Empty reply (finish_reason=stop) — retrying once (text only)",
        ),
    ],
    ids=["switch_off", "switch_on"],
)
async def test_voice_payload_stable_across_tool_round_and_post_tool_retry(
    monkeypatch, caplog, thinking, extras, retry_log
):
    """The voice payload rides every round (tool round included); only the
    switch-off retry drops the no-think kwarg."""
    monkeypatch.setattr(shared, "VOICE_THINKING_ENABLED", thinking)
    call_model, seen = _recording_call_model(
        [
            reply("", tool_calls=[tool_call("web_search", {"query": "weather"}, "c1")]),
            reply(""),
            reply("It is sunny."),
        ]
    )
    execute_tool = AsyncMock(return_value="75F sunny")

    with caplog.at_level(logging.INFO, logger=_LOOP_LOGGER):
        result = await _run(call_model, execute_tool, is_voice=True)

    assert result == "It is sunny."
    execute_tool.assert_called_once_with("web_search", {"query": "weather"})
    assert seen == extras
    assert _retry_logs(caplog) == [retry_log]


@PATHS
async def test_voice_switch_on_empty_twice_counts_still_empty_and_warns(monkeypatch, caplog, path):
    monkeypatch.setattr(shared, "VOICE_THINKING_ENABLED", True)
    before = _snapshot()

    with caplog.at_level(logging.INFO, logger=_LOOP_LOGGER):
        result, seen, deltas = await _turn(path, [("", "length"), ("", "length"), ("never reached", "stop")], True)

    assert result == ""
    assert deltas in (None, [])
    assert seen == [_THINKING_VOICE_EXTRA, _THINKING_VOICE_EXTRA]  # two rounds, the third never asked
    assert _delta(before) == {"retried": 1, "recovered": 0, "still_empty": 1}
    assert _loop_messages(caplog, logging.WARNING) == [_STILL_EMPTY_WARNING.format("length")]
    assert "thinking retry" not in caplog.text
    # finish_reason on the probe is what tells "reasoning ate the budget"
    # (length) apart from an EOS-only reply (stop) in Loki.
    probe = "LLM probe: " if path == "buffered" else "LLM probe (stream)"
    probes = [m for m in _loop_messages(caplog) if probe in m]
    assert len(probes) == 2
    assert all("finish=length " in m for m in probes)


@PATHS
@pytest.mark.parametrize("thinking", [False, True], ids=["switch_off", "switch_on"])
async def test_non_voice_ignores_switch(monkeypatch, caplog, path, thinking):
    monkeypatch.setattr(shared, "VOICE_THINKING_ENABLED", thinking)

    with caplog.at_level(logging.INFO, logger=_LOOP_LOGGER):
        result, seen, _ = await _turn(path, [("", "stop"), ("hello", "stop")])

    assert result == "hello"
    assert seen == [None, None]
    assert not any("Voice mode" in m for m in _loop_messages(caplog))
    assert _retry_logs(caplog) == ["[UNIFIED] Empty reply (finish_reason=stop) — retrying once"]
