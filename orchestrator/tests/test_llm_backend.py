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
"""

from __future__ import annotations

import inspect
import json

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
# Mirrors unified_loop.py's voice_extra — the exact payload that hit the
# missing-kwarg TypeError in production.
_VOICE_EXTRA = {"max_tokens": 1024, "chat_template_kwargs": {"enable_thinking": False}}


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
        with caplog.at_level(logging.WARNING, logger="orchestrator.llm_backend"):
            with respx.mock(base_url=url) as mock:
                mock.post("/chat/completions").mock(return_value=Response(200, json=_openai_response()))
                await backend.chat_completion(_MESSAGES, system="s", brand_new_flag=1)

    assert any("brand_new_flag" in r.getMessage() for r in caplog.records)
