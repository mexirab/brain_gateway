"""Shared pytest fixtures and helpers for the loop test files.

conftest.py star-imports this module, so every fixture listed in ``__all__``
is available to every test file. Give fixtures names unique across the suite
(prefix with the area) — a clash with another fixtures_*.py module is caught
by conftest at import time. Plain helper functions can be imported directly.

conftest imports this module before any test runs, so orchestrator modules
are imported inside the helpers, never at module level (importing
``orchestrator.shared`` loads the embedding model).

What lives here:
- model replies: ``reply`` / ``tool_call`` (OpenAI chat-completion shapes),
  ``tools_named``;
- the unified loop: ``run_loop`` (buffered, ``call_model`` mocked) and
  ``run_stream_loop`` (a fake stream backend), plus the SSE builders and
  ``FakeStreamBackend`` / ``SequencedBackend``;
- ``CloudBrain`` without ``__init__``: ``make_brain`` (the whole chat path,
  every model/vision/RAG/HA dependency stubbed), ``stream_brain`` (just
  ``_stream_loop_response``) and ``vision_brain`` (just ``_process_vision``),
  with ``stream_response``, ``drain`` and ``patched_vision``;
- ``RecordingScheduler`` (the auto-learn ``add_job`` surface);
- fixtures ``loop_vision_on`` / ``loop_vision_off``.
"""

from __future__ import annotations

import contextlib
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

__all__: list[str] = ["loop_vision_on", "loop_vision_off"]

PRIMARY_URL = "http://primary.invalid/v1"
FALLBACK_URL = "http://fallback.invalid/v1"
LOOP_MODEL_URL = "http://fake:8080/v1"


# ---------------------------------------------------------------------------
# Model replies
# ---------------------------------------------------------------------------


def reply(content="", tool_calls=None, finish_reason="stop", reasoning_content=None) -> dict:
    """A buffered chat-completion response with one choice."""
    message = {"content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    return {"choices": [{"message": message, "finish_reason": finish_reason}]}


def tool_call(name, arguments, call_id="call_0") -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def tools_named(*names) -> list[dict]:
    return [
        {"type": "function", "function": {"name": n, "description": n, "parameters": {"type": "object"}}} for n in names
    ]


# No selfcare_log: its safety net auto-logs on some user messages.
DEFAULT_TOOLS = tools_named("focus_status", "web_search", "search_memory", "home_assistant", "check_calendar")


# ---------------------------------------------------------------------------
# The unified loop
# ---------------------------------------------------------------------------


async def run_loop(call_model, execute_tool=None, *, messages=None, tools=None, max_rounds=5, **kwargs):
    """One buffered ``run_unified_tool_loop`` turn with ``call_model`` and
    ``execute_tool`` mocked. ``messages`` is handed over as-is, so a test can
    inspect what the loop appended to it."""
    from orchestrator.unified_loop import run_unified_tool_loop

    messages = messages if messages is not None else [{"role": "user", "content": "hi"}]
    with (
        patch("orchestrator.orchestrator.call_model", call_model),
        patch("orchestrator.tool_handlers.execute_tool", execute_tool or AsyncMock(return_value="ok")),
    ):
        return await run_unified_tool_loop(
            messages=messages,
            system_prompt="sys",
            tools=DEFAULT_TOOLS if tools is None else tools,
            model_url=LOOP_MODEL_URL,
            model_name="fake",
            http_client=None,
            max_rounds=max_rounds,
            **kwargs,
        )


async def run_stream_loop(backend, *, call_model=None, execute_tool=None, messages=None, tools=None, **kwargs):
    """One real-streaming loop turn against ``backend``; returns (result, deltas)."""
    from orchestrator.unified_loop import run_unified_tool_loop

    deltas: list[str] = []

    async def on_delta(text):
        deltas.append(text)

    with (
        patch("orchestrator.orchestrator.get_stream_capable_backend", return_value=backend),
        patch("orchestrator.orchestrator.call_model", call_model or AsyncMock()),
        patch("orchestrator.tool_handlers.execute_tool", execute_tool or AsyncMock(return_value="ok")),
    ):
        result = await run_unified_tool_loop(
            messages=messages if messages is not None else [{"role": "user", "content": "hi"}],
            system_prompt="sys",
            tools=DEFAULT_TOOLS if tools is None else tools,
            model_url=LOOP_MODEL_URL,
            model_name="fake",
            http_client=None,
            on_delta=on_delta,
            **kwargs,
        )
    return result, deltas


async def run_round(backend):
    """One ``_stream_model_round`` against ``backend``; returns (result, emitted text)."""
    from orchestrator.unified_loop import _stream_model_round

    emitted: list[str] = []

    async def emit(text):
        emitted.append(text)

    result = await _stream_model_round(backend, [], "sys", None, "auto", 120, None, emit, "TEST")
    return result, "".join(emitted)


# --- SSE ---------------------------------------------------------------------

SSE_DONE = "data: [DONE]\n\n"


def sse(obj) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def content_chunk(text) -> str:
    return sse({"choices": [{"delta": {"content": text}, "finish_reason": None}]})


def reasoning_chunk(text) -> str:
    return sse({"choices": [{"delta": {"reasoning_content": text}, "finish_reason": None}]})


def tool_chunk(index, name=None, args=None, call_id=None) -> str:
    fn = {}
    if name:
        fn["name"] = name
    if args:
        fn["arguments"] = args
    tc = {"index": index, "function": fn}
    if call_id:
        tc["id"] = call_id
    return sse({"choices": [{"delta": {"tool_calls": [tc]}, "finish_reason": None}]})


def stop_chunk(reason="stop") -> str:
    return sse({"choices": [{"delta": {}, "finish_reason": reason}]})


class FakeStreamBackend:
    """Yields one canned list of SSE strings per call; optionally raises mid-stream."""

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


class SequencedBackend:
    """A different canned stream per call (per loop round); records each
    round's extra_body as it was at call time."""

    def __init__(self, rounds):
        self._rounds = list(rounds)
        self.calls = 0
        self.extra_bodies = []

    async def stream_chat_completion(self, *args, **kwargs):
        # Snapshot: the loop passes the same dict every round, so a stored
        # reference would hide a later in-place mutation.
        self.extra_bodies.append(copy.deepcopy(kwargs.get("extra_body")))
        lines = self._rounds[min(self.calls, len(self._rounds) - 1)]
        self.calls += 1
        for line in lines:
            yield line


# ---------------------------------------------------------------------------
# CloudBrain without __init__
# ---------------------------------------------------------------------------


def _baseline_router():
    return SimpleNamespace(route=lambda text: SimpleNamespace(mode="baseline", intensity="low", tags=[]))


def make_brain(monkeypatch, loop, *, healthy=True, fallback_url=FALLBACK_URL, real_vision=False):
    """A CloudBrain that runs the real ``chat`` → ``_chat_unified`` →
    ``_chat_unified_inner`` path with the loop replaced by ``loop``.

    Vision is a pass-through that records what it was handed in
    ``brain.vision_seen`` (``real_vision=True`` keeps the real
    ``_process_vision``); auto-learn records into ``brain.auto_learn_seen``;
    intent routing, RAG, the system prompt and the health check are stubbed.
    """
    from orchestrator import cloud_brain, shared
    from orchestrator.cloud_brain import CloudBrain
    from orchestrator.prompt_builder import last_user_text

    brain = CloudBrain.__new__(CloudBrain)
    brain.vision_seen = []
    brain.auto_learn_seen = []
    if not real_vision:

        async def _vision(messages, routing_info):
            brain.vision_seen.append(list(messages))
            return messages

        brain._process_vision = _vision
    brain._mode_router = _baseline_router()
    brain._is_greeting = lambda text: True  # skips the RAG prefetch
    brain._last_user_text = last_user_text
    brain._get_unified_system_prompt = lambda *a, **k: "sys"
    brain._check_model_health = AsyncMock(return_value=healthy)
    brain._start_model_server = AsyncMock(return_value=False)
    brain._model_url = PRIMARY_URL
    brain._model_name = "primary"
    brain._fallback_model_url = fallback_url
    brain._fallback_model_name = "fallback"
    brain._get_all_tools = lambda: []
    brain._run_unified_loop = loop
    brain._schedule_auto_learn = lambda messages: brain.auto_learn_seen.append(list(messages))
    monkeypatch.setattr(cloud_brain, "record_reply_style", lambda mode: None)
    monkeypatch.setattr(cloud_brain, "apply_tone_request", lambda text: "")
    monkeypatch.setattr(cloud_brain, "is_first_chat", lambda: False)
    monkeypatch.setattr(shared, "REAL_STREAMING_ENABLED", True)
    monkeypatch.setattr("orchestrator.tool_definitions.get_voice_tools", lambda: [])
    return brain


def stream_brain(loop, fallback_url=""):
    """The minimum ``_stream_loop_response`` (and ``_chat_unified``'s
    accounting) needs."""
    from orchestrator.cloud_brain import CloudBrain

    brain = CloudBrain.__new__(CloudBrain)
    brain._run_unified_loop = loop
    brain._fallback_model_url = fallback_url
    brain._fallback_model_name = "fb"
    brain._get_all_tools = lambda: []
    brain._schedule_auto_learn = lambda messages: None
    brain._last_user_text = lambda messages: "hi"
    return brain


def stream_response(brain, is_voice=False, started_at=None, messages=None):
    return brain._stream_loop_response(
        messages=messages if messages is not None else [{"role": "user", "content": "hi"}],
        system_prompt="s",
        tools=[],
        model_url="u",
        model_name="m",
        is_voice=is_voice,
        started_at=started_at,
    )


async def drain(response) -> str:
    """The whole body of a StreamingResponse, as one string."""
    return "".join([c if isinstance(c, str) else c.decode() async for c in response.body_iterator])


def vision_brain():
    """The minimum ``CloudBrain._process_vision`` needs."""
    from orchestrator.cloud_brain import CloudBrain

    brain = CloudBrain.__new__(CloudBrain)
    brain._model_name = "fake"
    return brain


@contextlib.contextmanager
def patched_vision(analyze, healthy=True):
    """Vision model health + analyze_image replaced (no model is called)."""
    with (
        patch("orchestrator.vision_handler.check_vision_health", AsyncMock(return_value=healthy)),
        patch("orchestrator.vision_handler.analyze_image", analyze),
    ):
        yield


class RecordingScheduler:
    """Stands in for shared.scheduler: records add_job's args by job id."""

    def __init__(self):
        self.jobs = {}

    def add_job(self, func, trigger, run_date, args, id, replace_existing):  # noqa: A002
        self.jobs[id] = args


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def loop_vision_on(monkeypatch):
    from orchestrator import shared

    monkeypatch.setattr(shared, "VISION_ENABLED", True)
    monkeypatch.setattr(shared, "_vision_image_cache", {})


@pytest.fixture
def loop_vision_off(monkeypatch):
    from orchestrator import shared

    monkeypatch.setattr(shared, "VISION_ENABLED", False)
    monkeypatch.setattr(shared, "_vision_image_cache", {})
