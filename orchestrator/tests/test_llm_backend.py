"""
Tests for orchestrator/llm_backend.py — backend signature compatibility +
format translation. All HTTP is mocked via respx; no real API calls.

Regression background: orchestrator.call_model() invokes
backend.chat_completion(messages, system=..., tools=..., tool_choice=...,
timeout=..., extra_body=...) on EVERY path, but AnthropicBackend and
OpenAIBackend didn't accept `extra_body` — a TypeError on every call. Those
backends are the BYO/cloud-fallback path used while the local GPU box is
asleep, so the whole brain-asleep chat flow was broken. These tests exercise
each backend exactly the way orchestrator.py calls it so a signature drift
fails CI instead of production.

The last section drives the real unified loop into a real
OpenAICompatibleBackend (respx) to pin the voice-turn request body on the
wire for both settings of VOICE_THINKING_ENABLED.
"""

from __future__ import annotations

import inspect
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from httpx import Response

from orchestrator.llm_backend import (
    _BACKENDS,
    AnthropicBackend,
    LLMConfig,
    OpenAIBackend,
    OpenAICompatibleBackend,
)

_MESSAGES = [{"role": "user", "content": "hello"}]
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Get the current time",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]
# Mirrors unified_loop.py's voice_extra with VOICE_THINKING_ENABLED off (the
# default) — the exact payload that hit the missing-kwarg TypeError in production.
_VOICE_EXTRA = {"max_tokens": 1024, "chat_template_kwargs": {"enable_thinking": False}}
# voice_extra with VOICE_THINKING_ENABLED on: thinking left to the server default.
_VOICE_EXTRA_THINKING = {"max_tokens": 2048}


def _call_model_kwargs() -> dict:
    """The exact keyword set orchestrator.call_model passes through."""
    return {
        "system": "You are Jess.",
        "tools": _TOOLS,
        "tool_choice": "auto",
        "timeout": 30,
        "extra_body": dict(_VOICE_EXTRA),
    }


def _config(backend: str, url: str) -> LLMConfig:
    return LLMConfig(backend=backend, url=url, model="test-model", api_key="sk-test", max_tokens=4096)


def _openai_response() -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "test-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


def _anthropic_response(with_tool_use: bool = False) -> dict:
    content = [{"type": "text", "text": "hi"}]
    if with_tool_use:
        content.append({"type": "tool_use", "id": "toolu_1", "name": "get_time", "input": {"tz": "utc"}})
    return {
        "id": "msg_1",
        "type": "message",
        "model": "test-model",
        "content": content,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


# ---------------------------------------------------------------------------
# Signature-level guards (the exact class of bug that shipped)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend_cls", sorted(_BACKENDS.values(), key=lambda c: c.__name__), ids=lambda c: c.__name__)
def test_chat_completion_signature_accepts_call_model_kwargs(backend_cls):
    """Every registered backend must bind the full call_model keyword set."""
    sig = inspect.signature(backend_cls.chat_completion)
    sig.bind(None, _MESSAGES, **_call_model_kwargs())  # raises TypeError on drift


@pytest.mark.parametrize("backend_cls", sorted(_BACKENDS.values(), key=lambda c: c.__name__), ids=lambda c: c.__name__)
def test_chat_completion_signature_tolerates_future_kwargs(backend_cls):
    sig = inspect.signature(backend_cls.chat_completion)
    sig.bind(None, _MESSAGES, **_call_model_kwargs(), some_future_kwarg=True)


@pytest.mark.parametrize("backend_cls", sorted(_BACKENDS.values(), key=lambda c: c.__name__), ids=lambda c: c.__name__)
def test_stream_chat_completion_signature_matches_call_site(backend_cls):
    """orchestrator.stream_final_response calls (messages, system=, timeout=)."""
    sig = inspect.signature(backend_cls.stream_chat_completion)
    sig.bind(None, _MESSAGES, system="sys", timeout=60)


# ---------------------------------------------------------------------------
# End-to-end (mocked) chat_completion per backend, invoked like call_model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_compatible_passes_extra_body_through():
    url = "http://vllm.test/v1"
    async with httpx.AsyncClient() as http:
        backend = OpenAICompatibleBackend(_config("openai_compatible", url), http)
        with respx.mock(base_url=url) as mock:
            route = mock.post("/chat/completions").mock(return_value=Response(200, json=_openai_response()))
            resp = await backend.chat_completion(_MESSAGES, **_call_model_kwargs())

    payload = json.loads(route.calls[0].request.content)
    # vLLM extensions forwarded verbatim on the local path
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["max_tokens"] == 1024
    assert payload["messages"][0] == {"role": "system", "content": "You are Jess."}
    assert resp["choices"][0]["message"]["content"] == "hi"


@pytest.mark.asyncio
async def test_anthropic_accepts_call_model_invocation():
    """Regression: this exact call raised TypeError (unexpected 'extra_body')."""
    url = "https://api.anthropic.test/v1"
    async with httpx.AsyncClient() as http:
        backend = AnthropicBackend(_config("anthropic", url), http)
        with respx.mock(base_url=url) as mock:
            route = mock.post("/messages").mock(return_value=Response(200, json=_anthropic_response()))
            resp = await backend.chat_completion(_MESSAGES, **_call_model_kwargs())

    payload = json.loads(route.calls[0].request.content)
    # Generic keys mapped, vLLM-only keys dropped (Anthropic would 400)
    assert payload["max_tokens"] == 1024
    assert "chat_template_kwargs" not in payload
    # Anthropic-native translation still intact
    assert payload["system"] == "You are Jess."
    assert payload["tools"][0]["input_schema"] == {"type": "object", "properties": {}}
    assert route.calls[0].request.headers["x-api-key"] == "sk-test"
    # Response normalized back to OpenAI shape
    assert resp["choices"][0]["message"]["content"] == "hi"
    assert resp["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_anthropic_maps_stop_and_ignores_none_extra_body():
    url = "https://api.anthropic.test/v1"
    async with httpx.AsyncClient() as http:
        backend = AnthropicBackend(_config("anthropic", url), http)
        with respx.mock(base_url=url) as mock:
            route = mock.post("/messages").mock(return_value=Response(200, json=_anthropic_response()))
            await backend.chat_completion(_MESSAGES, system="s", extra_body={"stop": "END", "top_k": 40})
            await backend.chat_completion(_MESSAGES, system="s", extra_body=None)

    first = json.loads(route.calls[0].request.content)
    assert first["stop_sequences"] == ["END"]
    assert first["top_k"] == 40
    second = json.loads(route.calls[1].request.content)
    assert "stop_sequences" not in second


@pytest.mark.asyncio
async def test_anthropic_tool_use_normalized_to_tool_calls():
    url = "https://api.anthropic.test/v1"
    async with httpx.AsyncClient() as http:
        backend = AnthropicBackend(_config("anthropic", url), http)
        with respx.mock(base_url=url) as mock:
            mock.post("/messages").mock(return_value=Response(200, json=_anthropic_response(with_tool_use=True)))
            resp = await backend.chat_completion(_MESSAGES, **_call_model_kwargs())

    message = resp["choices"][0]["message"]
    assert resp["choices"][0]["finish_reason"] == "tool_calls"
    tc = message["tool_calls"][0]
    assert tc["function"]["name"] == "get_time"
    assert json.loads(tc["function"]["arguments"]) == {"tz": "utc"}


@pytest.mark.asyncio
async def test_openai_accepts_call_model_invocation_and_drops_vllm_keys():
    """Regression: this exact call raised TypeError (unexpected 'extra_body')."""
    url = "https://api.openai.test/v1"
    async with httpx.AsyncClient() as http:
        backend = OpenAIBackend(_config("openai", url), http)
        with respx.mock(base_url=url) as mock:
            route = mock.post("/chat/completions").mock(return_value=Response(200, json=_openai_response()))
            resp = await backend.chat_completion(_MESSAGES, **_call_model_kwargs())

    payload = json.loads(route.calls[0].request.content)
    # Standard OpenAI param mapped; vLLM extension dropped (OpenAI 400s on it)
    assert payload["max_tokens"] == 1024
    assert "chat_template_kwargs" not in payload
    assert payload["tools"] == _TOOLS
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-test"
    assert resp["choices"][0]["message"]["content"] == "hi"


@pytest.mark.asyncio
async def test_unknown_kwargs_warn_but_do_not_raise(caplog):
    import logging

    url = "https://api.openai.test/v1"
    async with httpx.AsyncClient() as http:
        backend = OpenAIBackend(_config("openai", url), http)
        with (
            caplog.at_level(logging.WARNING, logger="orchestrator.llm_backend"),
            respx.mock(base_url=url) as mock,
        ):
            mock.post("/chat/completions").mock(return_value=Response(200, json=_openai_response()))
            await backend.chat_completion(_MESSAGES, system="s", brand_new_flag=1)

    assert any("brand_new_flag" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend_cls, backend_name, url, path",
    [
        (OpenAIBackend, "openai", "https://api.openai.test/v1", "/chat/completions"),
        (AnthropicBackend, "anthropic", "https://api.anthropic.test/v1", "/messages"),
    ],
    ids=["openai", "anthropic"],
)
async def test_cloud_backends_take_thinking_on_voice_extra(backend_cls, backend_name, url, path, caplog):
    """Brain-asleep fallback with VOICE_THINKING_ENABLED on: the 2048 budget
    maps onto the cloud API, nothing is dropped, nothing warns."""
    import logging

    body = _anthropic_response() if backend_cls is AnthropicBackend else _openai_response()
    async with httpx.AsyncClient() as http:
        backend = backend_cls(_config(backend_name, url), http)
        with (
            caplog.at_level(logging.WARNING, logger="orchestrator.llm_backend"),
            respx.mock(base_url=url) as mock,
        ):
            route = mock.post(path).mock(return_value=Response(200, json=body))
            resp = await backend.chat_completion(
                _MESSAGES, **{**_call_model_kwargs(), "extra_body": dict(_VOICE_EXTRA_THINKING)}
            )

    payload = json.loads(route.calls[0].request.content)
    assert payload["max_tokens"] == 2048
    assert "chat_template_kwargs" not in payload
    assert resp["choices"][0]["message"]["content"] == "hi"
    assert not caplog.records


# ---------------------------------------------------------------------------
# Unified loop → OpenAICompatibleBackend: voice payload on the wire
# (VOICE_THINKING_ENABLED). Real loop, real backend, respx-mocked server.
# ---------------------------------------------------------------------------

_LOCAL_URL = "http://llama.test/v1"
_SYSTEM = "You are Jess."


def _local_backend(http) -> OpenAICompatibleBackend:
    return OpenAICompatibleBackend(
        LLMConfig(backend="openai_compatible", url=_LOCAL_URL, model="test-model", max_tokens=4096), http
    )


def _sse_reply(text: str) -> bytes:
    chunks = [
        {"choices": [{"delta": {"content": text}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()


def _local_route(mock, *, stream: bool):
    if stream:
        response = Response(200, content=_sse_reply("hello"), headers={"content-type": "text/event-stream"})
    else:
        response = Response(200, json=_openai_response())
    return mock.post("/chat/completions").mock(return_value=response)


async def _loop_turn(backend, *, is_voice: bool, stream: bool) -> str:
    """One turn of run_unified_tool_loop with every model round resolved to
    `backend` (buffered via call_model, or streamed via on_delta)."""
    from orchestrator.unified_loop import run_unified_tool_loop

    deltas = []

    async def on_delta(text):
        deltas.append(text)

    with (
        patch("orchestrator.orchestrator._resolve_backend", return_value=backend),
        patch("orchestrator.tool_handlers.execute_tool", AsyncMock()),
    ):
        result = await run_unified_tool_loop(
            messages=[dict(m) for m in _MESSAGES],
            system_prompt=_SYSTEM,
            tools=_TOOLS,
            model_url=_LOCAL_URL,
            model_name="test-model",
            http_client=None,
            is_voice=is_voice,
            on_delta=on_delta if stream else None,
        )
    if stream:
        assert "".join(deltas) == result
    return result


async def _direct_request(backend, *, stream: bool, extra_body) -> None:
    """The request the loop would send, issued straight at the backend."""
    kwargs = {"system": _SYSTEM, "tools": _TOOLS, "tool_choice": "auto", "timeout": 120, "extra_body": extra_body}
    if stream:
        async for _ in backend.stream_chat_completion([dict(m) for m in _MESSAGES], **kwargs):
            pass
    else:
        await backend.chat_completion([dict(m) for m in _MESSAGES], **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "streaming"])
async def test_voice_switch_off_wire_body_byte_identical_to_old_literal(monkeypatch, stream):
    from orchestrator import shared

    monkeypatch.setattr(shared, "VOICE_THINKING_ENABLED", False)
    async with httpx.AsyncClient() as http:
        backend = _local_backend(http)
        with respx.mock(base_url=_LOCAL_URL) as mock:
            route = _local_route(mock, stream=stream)
            result = await _loop_turn(backend, is_voice=True, stream=stream)
            await _direct_request(backend, stream=stream, extra_body=dict(_VOICE_EXTRA))

    assert result == ("hello" if stream else "hi")
    assert route.call_count == 2
    loop_body, reference_body = (c.request.content for c in route.calls)
    assert loop_body == reference_body
    payload = json.loads(loop_body)
    assert payload["max_tokens"] == 1024
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "streaming"])
async def test_voice_switch_on_wire_body_has_2048_and_no_template_kwargs(monkeypatch, stream):
    from orchestrator import shared

    monkeypatch.setattr(shared, "VOICE_THINKING_ENABLED", True)
    async with httpx.AsyncClient() as http:
        backend = _local_backend(http)
        with respx.mock(base_url=_LOCAL_URL) as mock:
            route = _local_route(mock, stream=stream)
            await _loop_turn(backend, is_voice=True, stream=stream)

    payload = json.loads(route.calls[0].request.content)
    expected_keys = ["model", "messages", "temperature", "max_tokens"]
    if stream:
        expected_keys.append("stream")
    expected_keys += ["tools", "tool_choice"]
    assert list(payload) == expected_keys
    assert payload["max_tokens"] == 2048
    assert "chat_template_kwargs" not in payload
    assert payload.get("stream", False) is stream


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "streaming"])
async def test_non_voice_wire_body_ignores_voice_switch(monkeypatch, stream):
    """Typed turns keep the backend's own max_tokens and never get a
    chat_template_kwargs override, whichever way the switch is set."""
    from orchestrator import shared

    monkeypatch.setattr(shared, "VOICE_THINKING_ENABLED", True)
    async with httpx.AsyncClient() as http:
        backend = _local_backend(http)
        with respx.mock(base_url=_LOCAL_URL) as mock:
            route = _local_route(mock, stream=stream)
            await _loop_turn(backend, is_voice=False, stream=stream)
            await _direct_request(backend, stream=stream, extra_body=None)

    loop_body, reference_body = (c.request.content for c in route.calls)
    assert loop_body == reference_body
    payload = json.loads(loop_body)
    assert payload["max_tokens"] == 4096
    assert "chat_template_kwargs" not in payload
