"""
LLM Backend Abstraction Layer.

Translates between the internal OpenAI-compatible format used by all call sites
and the actual provider API format (OpenAI-compatible, Anthropic, OpenAI official).

Call sites do NOT change. They still call call_model() with OpenAI-format messages.
The backend handles format translation transparently.
"""

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class LLMConfig:
    """Configuration for a single LLM endpoint."""

    backend: str  # "openai_compatible", "anthropic", "openai"
    url: str  # base URL (e.g., "http://10.0.0.195:8080/v1")
    model: str  # model name
    api_key: str = ""  # for cloud APIs
    temperature: float = 0.3
    max_tokens: int = 4096


class LLMBackend(ABC):
    """Abstract base for LLM API backends."""

    def __init__(self, config: LLMConfig, http_client: httpx.AsyncClient):
        self.config = config
        self._http = http_client

    @abstractmethod
    async def chat_completion(
        self,
        messages: List[Dict],
        system: str = "",
        tools: Optional[List] = None,
        tool_choice: str = "auto",
        timeout: int = 180,
    ) -> Dict[str, Any]:
        """
        Non-streaming chat completion.

        All backends accept OpenAI-format messages and return OpenAI-format response.
        Translation to/from provider-native format is handled internally.
        """
        ...

    @abstractmethod
    async def stream_chat_completion(
        self,
        messages: List[Dict],
        system: str = "",
        timeout: int = 180,
    ) -> AsyncIterator[str]:
        """
        Streaming chat completion. Yields SSE lines in OpenAI format.
        No tools during streaming (streaming is only for final user-facing response).
        """
        ...

    async def health_check(self) -> bool:
        """Check if the backend is reachable."""
        try:
            r = await self._http.get(f"{self.config.url}/models", timeout=5)
            return r.status_code == 200
        except Exception:
            return False


class OpenAICompatibleBackend(LLMBackend):
    """
    Backend for OpenAI-compatible servers (vLLM, llama.cpp, Ollama).
    This is the current behavior — essentially a thin wrapper.
    """

    async def chat_completion(self, messages, system="", tools=None, tool_choice="auto", timeout=180):
        final_messages = messages.copy()
        if system:
            final_messages.insert(0, {"role": "system", "content": system})

        payload = {
            "model": self.config.model,
            "messages": final_messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        headers = {}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        r = await self._http.post(
            f"{self.config.url}/chat/completions",
            json=payload,
            timeout=timeout,
            headers=headers,
        )
        r.raise_for_status()
        return r.json()

    async def stream_chat_completion(self, messages, system="", timeout=180):
        final_messages = messages.copy()
        if system:
            final_messages.insert(0, {"role": "system", "content": system})

        payload = {
            "model": self.config.model,
            "messages": final_messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": True,
        }

        headers = {}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        async with self._http.stream(
            "POST",
            f"{self.config.url}/chat/completions",
            json=payload,
            timeout=timeout,
            headers=headers,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line and line.startswith("data: "):
                    yield f"{line}\n\n"


class AnthropicBackend(LLMBackend):
    """
    Backend for the Anthropic Messages API.

    Handles format translation:
    - system is a top-level param, not a message
    - tools use input_schema instead of parameters
    - tool_use content blocks instead of tool_calls in message
    - Different streaming format (content_block_delta events)
    - Response normalized back to OpenAI format for call sites
    """

    def _convert_tools_to_anthropic(self, tools: List[Dict]) -> List[Dict]:
        """Convert OpenAI-format tools to Anthropic format."""
        anthropic_tools = []
        for tool in tools:
            if tool.get("type") != "function":
                continue
            fn = tool["function"]
            anthropic_tools.append(
                {
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                }
            )
        return anthropic_tools

    def _convert_messages_to_anthropic(self, messages: List[Dict]) -> List[Dict]:
        """
        Convert OpenAI-format messages to Anthropic format.

        Key differences:
        - No "system" role messages (handled as top-level param)
        - tool_calls in assistant messages become tool_use content blocks
        - tool role messages become user messages with tool_result content blocks
        """
        result = []
        for msg in messages:
            role = msg.get("role", "")

            if role == "system":
                continue  # handled as top-level param

            if role == "assistant":
                content = msg.get("content", "")
                tool_calls = msg.get("tool_calls", [])

                if tool_calls:
                    blocks = []
                    if content:
                        blocks.append({"type": "text", "text": content})
                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        args = fn.get("arguments", "{}")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                args = {}
                        blocks.append(
                            {
                                "type": "tool_use",
                                "id": tc.get("id", ""),
                                "name": fn.get("name", ""),
                                "input": args,
                            }
                        )
                    result.append({"role": "assistant", "content": blocks})
                else:
                    result.append({"role": "assistant", "content": content or ""})

            elif role == "tool":
                result.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.get("tool_call_id", ""),
                                "content": msg.get("content", ""),
                            }
                        ],
                    }
                )

            else:
                result.append({"role": role, "content": msg.get("content", "")})

        return result

    def _normalize_response(self, anthropic_resp: Dict) -> Dict:
        """Convert Anthropic response to OpenAI-format response dict."""
        content_blocks = anthropic_resp.get("content", [])
        text_parts = []
        tool_calls = []

        for block in content_blocks:
            if block["type"] == "text":
                text_parts.append(block["text"])
            elif block["type"] == "tool_use":
                tool_calls.append(
                    {
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block["input"]),
                        },
                    }
                )

        message = {
            "role": "assistant",
            "content": "\n".join(text_parts) if text_parts else None,
        }
        if tool_calls:
            message["tool_calls"] = tool_calls

        return {
            "id": anthropic_resp.get("id", ""),
            "object": "chat.completion",
            "model": anthropic_resp.get("model", ""),
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }
            ],
            "usage": anthropic_resp.get("usage", {}),
        }

    async def chat_completion(self, messages, system="", tools=None, tool_choice="auto", timeout=180):
        anthropic_messages = self._convert_messages_to_anthropic(messages)

        payload = {
            "model": self.config.model,
            "messages": anthropic_messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = self._convert_tools_to_anthropic(tools)
            # Anthropic doesn't have tool_choice="none" the same way.
            # For Claude as orchestrator, always use auto (native tool calling).
            payload["tool_choice"] = {"type": "auto"}

        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        r = await self._http.post(
            f"{self.config.url}/messages",
            json=payload,
            timeout=timeout,
            headers=headers,
        )
        r.raise_for_status()
        return self._normalize_response(r.json())

    async def stream_chat_completion(self, messages, system="", timeout=180):
        """Stream from Anthropic and yield OpenAI-format SSE lines."""
        anthropic_messages = self._convert_messages_to_anthropic(messages)
        payload = {
            "model": self.config.model,
            "messages": anthropic_messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "stream": True,
        }
        if system:
            payload["system"] = system

        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        chunk_id = f"chatcmpl-{int(time.time())}"

        async with self._http.stream(
            "POST",
            f"{self.config.url}/messages",
            json=payload,
            timeout=timeout,
            headers=headers,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type", "")

                if event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        oai_chunk = {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "model": self.config.model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": delta["text"]},
                                    "finish_reason": None,
                                }
                            ],
                        }
                        yield f"data: {json.dumps(oai_chunk)}\n\n"

                elif event_type == "message_stop":
                    final = {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "model": self.config.model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                    yield f"data: {json.dumps(final)}\n\n"
                    yield "data: [DONE]\n\n"

    async def health_check(self) -> bool:
        """Anthropic doesn't have a /models endpoint; just return True if api_key is set."""
        return bool(self.config.api_key)


class OpenAIBackend(LLMBackend):
    """
    Backend for the official OpenAI API.

    Nearly identical to OpenAICompatibleBackend but always uses Bearer auth.
    """

    async def chat_completion(self, messages, system="", tools=None, tool_choice="auto", timeout=180):
        final_messages = messages.copy()
        if system:
            final_messages.insert(0, {"role": "system", "content": system})

        payload = {
            "model": self.config.model,
            "messages": final_messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        headers = {"Authorization": f"Bearer {self.config.api_key}"}

        r = await self._http.post(
            f"{self.config.url}/chat/completions",
            json=payload,
            timeout=timeout,
            headers=headers,
        )
        r.raise_for_status()
        return r.json()

    async def stream_chat_completion(self, messages, system="", timeout=180):
        final_messages = messages.copy()
        if system:
            final_messages.insert(0, {"role": "system", "content": system})

        payload = {
            "model": self.config.model,
            "messages": final_messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": True,
        }

        headers = {"Authorization": f"Bearer {self.config.api_key}"}

        async with self._http.stream(
            "POST",
            f"{self.config.url}/chat/completions",
            json=payload,
            timeout=timeout,
            headers=headers,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line and line.startswith("data: "):
                    yield f"{line}\n\n"


# ---------------------------------------------------------------------------
# Factory + Registry
# ---------------------------------------------------------------------------

_BACKENDS = {
    "openai_compatible": OpenAICompatibleBackend,
    "anthropic": AnthropicBackend,
    "openai": OpenAIBackend,
}


def create_backend(config: LLMConfig, http_client: httpx.AsyncClient) -> LLMBackend:
    """Create a backend instance from config."""
    cls = _BACKENDS.get(config.backend)
    if not cls:
        raise ValueError(f"Unknown LLM backend: '{config.backend}'. Must be one of: {list(_BACKENDS.keys())}")
    return cls(config, http_client)
