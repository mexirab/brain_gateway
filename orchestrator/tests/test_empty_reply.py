"""
Tests for the 2026-10-14 empty-reply fix (branch fix/voice-empty-replies):

- ``run_unified_tool_loop`` retries ONCE when a round returns no text and no
  tool call — with thinking re-enabled on voice — in its own extra round, and
  text-only when the retry follows tool execution;
- ``bgw_llm_empty_reply_total{outcome}`` counts retried / recovered / still_empty;
- the selfcare safety net still fires when the answer arrived on the retry;
- the streaming path emits the recovered text without a leading separator;
- ``cloud_brain._is_empty_assistant`` + the unified-path drop of blank
  assistant turns from client history;
- ``VOICE_THINKING_ENABLED``: off (default) keeps the pre-switch voice payload
  byte-for-byte; on sends ``{"max_tokens": 2048}`` with no
  ``chat_template_kwargs``, and the empty-reply retry is a plain resample.

Mocking style mirrors ``test_loop_hardening.py`` (buffered) and
``test_real_streaming.py`` (fake stream backend).
"""

import copy
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator import shared
from orchestrator.cloud_brain import _is_empty_assistant
from orchestrator.metrics import EMPTY_REPLY_TOTAL
from orchestrator.unified_loop import run_unified_tool_loop

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TOOLS = [
    {"type": "function", "function": {"name": name, "description": name, "parameters": {"type": "object"}}}
    for name in ("focus_status", "web_search", "home_assistant", "selfcare_log")
]

LONG_PROSE = (
    "Here is what I found on that page. The author explains at length how the assistant's "
    "tool-calling protocol works, including a verbatim example of the markup the model emits "
    "when it wants to check the focus timer, which looks like this: "
)
FOCUS_BLOCK = '<tool_call>{"name":"focus_status","arguments":{}}</tool_call>'


def _reply(content="", tool_calls=None, finish_reason="stop"):
    message = {"content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message, "finish_reason": finish_reason}]}


def _native(name, arguments, call_id):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _count(outcome: str) -> float:
    return EMPTY_REPLY_TOTAL.labels(outcome=outcome)._value.get()


def _snapshot():
    return {o: _count(o) for o in ("retried", "recovered", "still_empty")}


def _delta(before):
    return {o: _count(o) - v for o, v in before.items()}


@pytest.fixture(autouse=True)
def _voice_thinking_off(monkeypatch):
    """Pin VOICE_THINKING_ENABLED to its default (off) so the voice tests
    below can't flip with the host env; the switch-on tests override it.
    The default itself is asserted in test_config.py."""
    monkeypatch.setattr(shared, "VOICE_THINKING_ENABLED", False)


async def _run(call_model, execute_tool, messages=None, max_rounds=5, is_voice=False):
    messages = messages if messages is not None else [{"role": "user", "content": "hello there"}]
    with (
        patch("orchestrator.orchestrator.call_model", call_model),
        patch("orchestrator.tool_handlers.execute_tool", execute_tool),
    ):
        return await run_unified_tool_loop(
            messages=messages,
            system_prompt="sys",
            tools=TOOLS,
            model_url="http://fake:8080/v1",
            model_name="fake",
            http_client=None,
            max_rounds=max_rounds,
            is_voice=is_voice,
        )


# ---------------------------------------------------------------------------
# 1-3: buffered retry + counters + round budget
# ---------------------------------------------------------------------------


async def test_empty_then_text_recovers_and_counts():
    call_model = AsyncMock(side_effect=[_reply(""), _reply("hello")])
    execute_tool = AsyncMock()
    before = _snapshot()

    result = await _run(call_model, execute_tool)

    assert result == "hello"
    assert call_model.call_count == 2
    assert _delta(before) == {"retried": 1, "recovered": 1, "still_empty": 0}
    execute_tool.assert_not_called()


async def test_voice_retry_drops_no_think_kwarg_but_keeps_max_tokens():
    call_model = AsyncMock(side_effect=[_reply(""), _reply("hello")])

    result = await _run(call_model, AsyncMock(), is_voice=True)

    assert result == "hello"
    first = call_model.call_args_list[0].kwargs["extra_body"]
    retry = call_model.call_args_list[1].kwargs["extra_body"]
    assert first["chat_template_kwargs"] == {"enable_thinking": False}
    assert first["max_tokens"] == 1024
    assert "chat_template_kwargs" not in retry
    assert retry["max_tokens"] == 1024


async def test_non_voice_has_no_extra_body_on_either_round():
    call_model = AsyncMock(side_effect=[_reply(""), _reply("hello")])
    await _run(call_model, AsyncMock())
    assert [c.kwargs["extra_body"] for c in call_model.call_args_list] == [None, None]


async def test_two_empty_rounds_return_empty_and_count_still_empty():
    call_model = AsyncMock(side_effect=[_reply(""), _reply(""), _reply("never reached")])
    before = _snapshot()

    result = await _run(call_model, AsyncMock())

    assert result == ""
    assert call_model.call_count == 2
    assert _delta(before) == {"retried": 1, "recovered": 0, "still_empty": 1}


async def test_retry_gets_own_round_when_max_rounds_is_one():
    call_model = AsyncMock(side_effect=[_reply(""), _reply("hello")])

    result = await _run(call_model, AsyncMock(), max_rounds=1)

    assert result == "hello"
    assert "complexity" not in result
    assert call_model.call_count == 2


# ---------------------------------------------------------------------------
# 4: post-tool retry is text-only
# ---------------------------------------------------------------------------


async def test_post_tool_retry_ignores_new_tool_calls():
    call_model = AsyncMock(
        side_effect=[
            _reply("", tool_calls=[_native("web_search", {"query": "weather"}, "c1")]),
            _reply(""),
            _reply(
                "It is sunny.",
                tool_calls=[_native("home_assistant", {"entity_id": "light.room_1", "service": "turn_on"}, "c2")],
            ),
            _reply("should not be reached"),
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
    same = _native("web_search", {"query": "weather"}, "c1")
    call_model = AsyncMock(side_effect=[_reply("", tool_calls=[same]), _reply(""), _reply("Sunny.", tool_calls=[same])])
    execute_tool = AsyncMock(return_value="75F sunny")

    result = await _run(call_model, execute_tool)

    assert result == "Sunny."
    assert execute_tool.call_count == 1


# ---------------------------------------------------------------------------
# 5: what counts as empty
# ---------------------------------------------------------------------------


async def test_think_only_prefill_counts_as_empty():
    call_model = AsyncMock(side_effect=[_reply("<think>\n\n</think>"), _reply("hello")])
    before = _snapshot()

    result = await _run(call_model, AsyncMock())

    assert result == "hello"
    assert _delta(before)["retried"] == 1


async def test_rejected_tool_call_echo_in_prose_not_retried():
    call_model = AsyncMock(side_effect=[_reply(LONG_PROSE + FOCUS_BLOCK), _reply("never reached")])
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
    call_model = AsyncMock(side_effect=[_reply(FOCUS_BLOCK, finish_reason="length"), _reply("never")])
    before = _snapshot()

    await _run(call_model, AsyncMock())

    assert call_model.call_count == 1
    assert _delta(before)["retried"] == 0


# ---------------------------------------------------------------------------
# 6: selfcare safety net after a retry
# ---------------------------------------------------------------------------


async def test_selfcare_safety_net_fires_after_retry():
    call_model = AsyncMock(side_effect=[_reply(""), _reply("Nice, noted.")])
    execute_tool = AsyncMock(return_value="logged")
    messages = [{"role": "user", "content": "I took my meds this morning"}]

    result = await _run(call_model, execute_tool, messages=messages)

    assert result == "Nice, noted."
    execute_tool.assert_called_once()
    assert execute_tool.call_args.args[0] == "selfcare_log"
    assert execute_tool.call_args.args[1]["action"] == "medication"


async def test_selfcare_safety_net_fires_without_retry_baseline():
    call_model = AsyncMock(side_effect=[_reply("Nice, noted.")])
    execute_tool = AsyncMock(return_value="logged")
    messages = [{"role": "user", "content": "I took my meds this morning"}]

    await _run(call_model, execute_tool, messages=messages)

    assert execute_tool.call_args.args[0] == "selfcare_log"


async def test_selfcare_safety_net_not_fired_after_tool_round_plus_retry():
    """effective_round must be 1 after a real tool round, so no auto-log."""
    call_model = AsyncMock(
        side_effect=[
            _reply("", tool_calls=[_native("web_search", {"query": "x"}, "c1")]),
            _reply(""),
            _reply("Done."),
        ]
    )
    execute_tool = AsyncMock(return_value="ok")
    messages = [{"role": "user", "content": "I took my meds this morning"}]

    await _run(call_model, execute_tool, messages=messages)

    assert [c.args[0] for c in execute_tool.call_args_list] == ["web_search"]


# ---------------------------------------------------------------------------
# 7: streaming path
# ---------------------------------------------------------------------------


def _sse(obj) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _content_chunk(text):
    return _sse({"choices": [{"delta": {"content": text}, "finish_reason": None}]})


def _stop_chunk():
    return _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]})


class SequencedBackend:
    def __init__(self, rounds):
        self._rounds = list(rounds)
        self.calls = 0
        self.extra_bodies = []

    async def stream_chat_completion(self, *args, **kwargs):
        # Snapshot at call time: the loop passes the same dict every round,
        # so a stored reference would hide a later in-place mutation.
        self.extra_bodies.append(copy.deepcopy(kwargs.get("extra_body")))
        lines = self._rounds[min(self.calls, len(self._rounds) - 1)]
        self.calls += 1
        for line in lines:
            yield line


async def _run_stream(backend, is_voice=False):
    deltas = []

    async def on_delta(text):
        deltas.append(text)

    with (
        patch("orchestrator.orchestrator.get_stream_capable_backend", return_value=backend),
        patch("orchestrator.orchestrator.call_model", AsyncMock()),
        patch("orchestrator.tool_handlers.execute_tool", AsyncMock(return_value="ok")),
    ):
        result = await run_unified_tool_loop(
            messages=[{"role": "user", "content": "hi"}],
            system_prompt="sys",
            tools=TOOLS,
            model_url="http://fake:8080/v1",
            model_name="fake",
            http_client=None,
            on_delta=on_delta,
            is_voice=is_voice,
        )
    return result, deltas


async def test_stream_empty_round_then_text_no_separator():
    backend = SequencedBackend([[_content_chunk(""), _stop_chunk()], [_content_chunk("hello"), _stop_chunk()]])
    before = _snapshot()

    result, deltas = await _run_stream(backend)

    assert result == "hello"
    assert "".join(deltas) == "hello"
    assert backend.calls == 2
    assert _delta(before) == {"retried": 1, "recovered": 1, "still_empty": 0}


async def test_stream_voice_retry_drops_no_think_kwarg():
    backend = SequencedBackend([[_stop_chunk()], [_content_chunk("hello"), _stop_chunk()]])

    result, deltas = await _run_stream(backend, is_voice=True)

    assert result == "hello"
    assert backend.extra_bodies[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "chat_template_kwargs" not in (backend.extra_bodies[1] or {})
    assert backend.extra_bodies[1]["max_tokens"] == 1024


# ---------------------------------------------------------------------------
# 8: _is_empty_assistant table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    ["", "  ", None, [], [{"type": "text", "text": " "}]],
)
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
        {"role": "assistant", "content": "", "tool_calls": [_native("web_search", {}, "c1")]},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": ""},
        {"role": "assistant", "content": 42},
    ],
)
def test_is_empty_assistant_false(msg):
    assert _is_empty_assistant(msg) is False


# ---------------------------------------------------------------------------
# 9: cloud_brain drops blank assistant turns before the loop
# ---------------------------------------------------------------------------


def _make_brain(loop_mock):
    from orchestrator.cloud_brain import CloudBrain

    brain = CloudBrain.__new__(CloudBrain)
    brain._process_vision = AsyncMock(side_effect=lambda messages, routing_info: messages)
    brain._mode_router = SimpleNamespace(route=lambda text: SimpleNamespace(mode="baseline", intensity="low", tags=[]))
    brain._is_greeting = lambda text: True
    brain._get_unified_system_prompt = lambda *a, **k: "sys"
    brain._check_model_health = AsyncMock(return_value=True)
    brain._model_url = "http://fake:8080/v1"
    brain._model_name = "fake"
    brain._fallback_model_url = ""
    brain._get_all_tools = lambda: []
    brain._run_unified_loop = loop_mock
    brain._schedule_auto_learn = lambda messages: None
    return brain


async def test_cloud_brain_drops_empty_assistant_turn(caplog):
    loop_mock = AsyncMock(return_value="fine")
    brain = _make_brain(loop_mock)
    client_messages = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "hello?"},
    ]

    with patch("orchestrator.cloud_brain.is_first_chat", return_value=False), caplog.at_level(logging.INFO):
        await brain._chat_unified_inner(list(client_messages), False, None, None, "hello?", {"tool_calls": []}, False)

    seen = loop_mock.call_args.kwargs["messages"]
    assert [m["role"] for m in seen] == ["user", "user"]
    assert [m["content"] for m in seen] == ["first question", "hello?"]
    assert "Dropped 1 empty assistant message(s)" in caplog.text


async def test_cloud_brain_keeps_tool_call_and_nonempty_assistant_turns(caplog):
    loop_mock = AsyncMock(return_value="fine")
    brain = _make_brain(loop_mock)
    client_messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}]

    with patch("orchestrator.cloud_brain.is_first_chat", return_value=False), caplog.at_level(logging.INFO):
        await brain._chat_unified_inner(list(client_messages), False, None, None, "hi", {"tool_calls": []}, False)

    assert loop_mock.call_args.kwargs["messages"] == client_messages
    assert "Dropped" not in caplog.text


async def test_dropped_tool_call_is_not_retried():
    """A tool-call marker that reached only the reasoning (no parsed call, no
    content) is the 'dropped' fault with its own metric/alert — not an
    EOS-only reply — so it must not be retried (CI caught a double count)."""
    reply = _reply("")
    reply["choices"][0]["message"]["reasoning_content"] = "<tool_call>"
    call_model = AsyncMock(side_effect=[reply, _reply("never reached")])
    before = _snapshot()

    result = await _run(call_model, AsyncMock())

    assert call_model.call_count == 1
    assert result == ""
    assert _delta(before)["retried"] == 0
    assert _delta(before)["still_empty"] == 0  # covered by the dropped metric/alert instead


# ---------------------------------------------------------------------------
# 10: VOICE_THINKING_ENABLED switch (voice payload + empty-reply retry)
# ---------------------------------------------------------------------------

# The voice payload as it was before the switch existed. Off (the default) must
# reproduce it exactly — same keys, values AND order, so the request body the
# brain sees is byte-identical.
_OLD_VOICE_EXTRA = {"max_tokens": 1024, "chat_template_kwargs": {"enable_thinking": False}}
_OLD_VOICE_EXTRA_JSON = '{"max_tokens": 1024, "chat_template_kwargs": {"enable_thinking": false}}'
_THINKING_VOICE_EXTRA = {"max_tokens": 2048}

_LOOP_LOGGER = "orchestrator.unified_loop"


def _recording_call_model(replies):
    """Buffered call_model mock that snapshots extra_body at call time."""
    queue = list(replies)
    seen = []

    async def _call(*args, **kwargs):
        seen.append(copy.deepcopy(kwargs.get("extra_body")))
        return queue.pop(0)

    return AsyncMock(side_effect=_call), seen


def _loop_messages(caplog, level=None):
    return [r.getMessage() for r in caplog.records if r.name == _LOOP_LOGGER and (level is None or r.levelno == level)]


def _retry_logs(caplog):
    return [m for m in _loop_messages(caplog) if "retrying once" in m]


def _stop_reason_chunk(reason):
    return _sse({"choices": [{"delta": {}, "finish_reason": reason}]})


# -- (1) switch off: payload identical to the old literal ---------------------


async def test_voice_switch_off_buffered_payload_identical_to_old_literal(caplog):
    call_model, seen = _recording_call_model([_reply("hello")])

    with caplog.at_level(logging.INFO, logger=_LOOP_LOGGER):
        result = await _run(call_model, AsyncMock(), is_voice=True)

    assert result == "hello"
    assert seen == [_OLD_VOICE_EXTRA]
    assert list(seen[0]) == ["max_tokens", "chat_template_kwargs"]
    assert json.dumps(seen[0]) == _OLD_VOICE_EXTRA_JSON
    assert "[UNIFIED] Voice mode: max_tokens=1024, thinking=off" in _loop_messages(caplog)


async def test_voice_switch_off_stream_payload_identical_to_old_literal(caplog):
    backend = SequencedBackend([[_content_chunk("hello"), _stop_chunk()]])

    with caplog.at_level(logging.INFO, logger=_LOOP_LOGGER):
        result, deltas = await _run_stream(backend, is_voice=True)

    assert result == "hello"
    assert "".join(deltas) == "hello"
    assert backend.extra_bodies == [_OLD_VOICE_EXTRA]
    assert json.dumps(backend.extra_bodies[0]) == _OLD_VOICE_EXTRA_JSON
    assert "[UNIFIED] Voice mode: max_tokens=1024, thinking=off" in _loop_messages(caplog)


# -- (2) switch on: thinking left on, 2048-token budget -----------------------


async def test_voice_switch_on_buffered_payload_has_no_template_kwargs(monkeypatch, caplog):
    monkeypatch.setattr(shared, "VOICE_THINKING_ENABLED", True)
    call_model, seen = _recording_call_model([_reply("hello")])

    with caplog.at_level(logging.INFO, logger=_LOOP_LOGGER):
        result = await _run(call_model, AsyncMock(), is_voice=True)

    assert result == "hello"
    assert seen == [_THINKING_VOICE_EXTRA]
    assert "chat_template_kwargs" not in seen[0]
    logs = _loop_messages(caplog)
    assert "[UNIFIED] Voice mode: max_tokens=2048, thinking=on" in logs
    assert not any("thinking=off" in m for m in logs)


async def test_voice_switch_on_stream_payload_and_probe_logs_finish_reason(monkeypatch, caplog):
    monkeypatch.setattr(shared, "VOICE_THINKING_ENABLED", True)
    backend = SequencedBackend([[_content_chunk("hello"), _stop_chunk()]])

    with caplog.at_level(logging.INFO, logger=_LOOP_LOGGER):
        result, deltas = await _run_stream(backend, is_voice=True)

    assert result == "hello"
    assert "".join(deltas) == "hello"
    assert backend.extra_bodies == [_THINKING_VOICE_EXTRA]
    logs = _loop_messages(caplog)
    assert "[UNIFIED] Voice mode: max_tokens=2048, thinking=on" in logs
    probes = [m for m in logs if "LLM probe (stream)" in m]
    assert len(probes) == 1
    assert "finish=stop " in probes[0]


async def test_voice_switch_is_read_per_turn_not_at_import(monkeypatch):
    """Flipping shared.VOICE_THINKING_ENABLED changes the very next turn —
    the loop must not have captured the value at import time."""
    monkeypatch.setattr(shared, "VOICE_THINKING_ENABLED", True)
    on_model, on_seen = _recording_call_model([_reply("a")])
    await _run(on_model, AsyncMock(), is_voice=True)

    monkeypatch.setattr(shared, "VOICE_THINKING_ENABLED", False)
    off_model, off_seen = _recording_call_model([_reply("b")])
    await _run(off_model, AsyncMock(), is_voice=True)

    assert on_seen == [_THINKING_VOICE_EXTRA]
    assert off_seen == [_OLD_VOICE_EXTRA]


# -- (3) empty reply → one retry; switch decides what the retry changes -------


@pytest.mark.parametrize(
    "thinking, extras, retry_log",
    [
        (
            False,
            [_OLD_VOICE_EXTRA, {"max_tokens": 1024}],
            "[UNIFIED] Empty reply (finish_reason=length) — retrying once with thinking",
        ),
        (
            True,
            [_THINKING_VOICE_EXTRA, _THINKING_VOICE_EXTRA],
            "[UNIFIED] Empty reply (finish_reason=length) — retrying once",
        ),
    ],
    ids=["switch_off_reenables_thinking", "switch_on_plain_resample"],
)
async def test_voice_empty_reply_retry_payload_and_log(monkeypatch, caplog, thinking, extras, retry_log):
    monkeypatch.setattr(shared, "VOICE_THINKING_ENABLED", thinking)
    call_model, seen = _recording_call_model([_reply("", finish_reason="length"), _reply("hello")])
    before = _snapshot()

    with caplog.at_level(logging.INFO, logger=_LOOP_LOGGER):
        result = await _run(call_model, AsyncMock(), is_voice=True)

    assert result == "hello"
    assert seen == extras
    assert _retry_logs(caplog) == [retry_log]
    assert _delta(before) == {"retried": 1, "recovered": 1, "still_empty": 0}


async def test_voice_switch_on_stream_retry_resends_same_extra_body(monkeypatch, caplog):
    monkeypatch.setattr(shared, "VOICE_THINKING_ENABLED", True)
    backend = SequencedBackend([[_stop_reason_chunk("length")], [_content_chunk("hello"), _stop_chunk()]])
    before = _snapshot()

    with caplog.at_level(logging.INFO, logger=_LOOP_LOGGER):
        result, deltas = await _run_stream(backend, is_voice=True)

    assert result == "hello"
    assert "".join(deltas) == "hello"
    assert backend.extra_bodies == [_THINKING_VOICE_EXTRA, _THINKING_VOICE_EXTRA]
    retry = _retry_logs(caplog)
    assert len(retry) == 1
    assert "with thinking" not in retry[0]
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
            _reply("", tool_calls=[_native("web_search", {"query": "weather"}, "c1")]),
            _reply(""),
            _reply("It is sunny."),
        ]
    )
    execute_tool = AsyncMock(return_value="75F sunny")

    with caplog.at_level(logging.INFO, logger=_LOOP_LOGGER):
        result = await _run(call_model, execute_tool, is_voice=True)

    assert result == "It is sunny."
    execute_tool.assert_called_once_with("web_search", {"query": "weather"})
    assert seen == extras
    assert _retry_logs(caplog) == [retry_log]


# -- (4) switch on + empty twice: still_empty + reworded WARNING --------------


async def test_voice_switch_on_empty_twice_counts_still_empty_and_warns(monkeypatch, caplog):
    monkeypatch.setattr(shared, "VOICE_THINKING_ENABLED", True)
    call_model, seen = _recording_call_model(
        [_reply("", finish_reason="length"), _reply("", finish_reason="length"), _reply("never reached")]
    )
    before = _snapshot()

    with caplog.at_level(logging.INFO, logger=_LOOP_LOGGER):
        result = await _run(call_model, AsyncMock(), is_voice=True)

    assert result == ""
    assert call_model.call_count == 2
    assert seen == [_THINKING_VOICE_EXTRA, _THINKING_VOICE_EXTRA]
    assert _delta(before) == {"retried": 1, "recovered": 0, "still_empty": 1}
    assert _loop_messages(caplog, logging.WARNING) == [
        "[UNIFIED] Empty reply after retry — user got no answer (finish_reason=length)"
    ]
    assert "thinking retry" not in caplog.text
    # finish_reason on the probe is what tells "reasoning ate the budget"
    # (length) apart from an EOS-only reply (stop) in Loki.
    probes = [m for m in _loop_messages(caplog) if "LLM probe: " in m]
    assert len(probes) == 2
    assert all("finish=length " in m for m in probes)


async def test_voice_switch_on_stream_empty_twice_counts_still_empty(monkeypatch, caplog):
    monkeypatch.setattr(shared, "VOICE_THINKING_ENABLED", True)
    backend = SequencedBackend([[_stop_reason_chunk("length")], [_stop_reason_chunk("length")]])
    before = _snapshot()

    with caplog.at_level(logging.INFO, logger=_LOOP_LOGGER):
        result, deltas = await _run_stream(backend, is_voice=True)

    assert result == ""
    assert deltas == []
    assert backend.calls == 2
    assert backend.extra_bodies == [_THINKING_VOICE_EXTRA, _THINKING_VOICE_EXTRA]
    assert _delta(before) == {"retried": 1, "recovered": 0, "still_empty": 1}
    assert _loop_messages(caplog, logging.WARNING) == [
        "[UNIFIED] Empty reply after retry — user got no answer (finish_reason=length)"
    ]
    probes = [m for m in _loop_messages(caplog) if "LLM probe (stream)" in m]
    assert len(probes) == 2
    assert all("finish=length " in m for m in probes)


async def test_switch_off_empty_twice_warning_text_is_reworded(caplog):
    """The still-empty WARNING no longer claims a 'thinking' retry (it isn't
    one with the switch on); the wording is shared by both settings."""
    call_model = AsyncMock(side_effect=[_reply(""), _reply("")])

    with caplog.at_level(logging.INFO, logger=_LOOP_LOGGER):
        await _run(call_model, AsyncMock(), is_voice=True)

    assert _loop_messages(caplog, logging.WARNING) == [
        "[UNIFIED] Empty reply after retry — user got no answer (finish_reason=stop)"
    ]


# -- (5) non-voice turns ignore the switch ------------------------------------


@pytest.mark.parametrize("thinking", [False, True], ids=["switch_off", "switch_on"])
async def test_non_voice_buffered_ignores_switch(monkeypatch, caplog, thinking):
    monkeypatch.setattr(shared, "VOICE_THINKING_ENABLED", thinking)
    call_model, seen = _recording_call_model([_reply(""), _reply("hello")])

    with caplog.at_level(logging.INFO, logger=_LOOP_LOGGER):
        result = await _run(call_model, AsyncMock())

    assert result == "hello"
    assert seen == [None, None]
    assert not any("Voice mode" in m for m in _loop_messages(caplog))
    assert _retry_logs(caplog) == ["[UNIFIED] Empty reply (finish_reason=stop) — retrying once"]


@pytest.mark.parametrize("thinking", [False, True], ids=["switch_off", "switch_on"])
async def test_non_voice_stream_ignores_switch(monkeypatch, caplog, thinking):
    monkeypatch.setattr(shared, "VOICE_THINKING_ENABLED", thinking)
    backend = SequencedBackend([[_content_chunk("hello"), _stop_chunk()]])

    with caplog.at_level(logging.INFO, logger=_LOOP_LOGGER):
        result, _ = await _run_stream(backend)

    assert result == "hello"
    assert backend.extra_bodies == [None]
    assert not any("Voice mode" in m for m in _loop_messages(caplog))
