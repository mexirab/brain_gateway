"""Tests for the BYO-aware model liveness probe.

Regression coverage for the 2026-06-14 fix: the old `check_model_health` probed
`/health` (served only by vLLM), so every Ollama / LM-Studio / cloud BYO backend
looked permanently offline and every chat fell through to the brain-asleep reply.
The fix probes the OpenAI-compatible `/v1/models` endpoint and short-circuits
cloud backends.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator import model_manager
from orchestrator.model_manager import _models_probe_url, check_model_health


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://host.docker.internal:11434/v1", "http://host.docker.internal:11434/v1/models"),
        ("http://host.docker.internal:11434/v1/", "http://host.docker.internal:11434/v1/models"),
        ("http://vllm-primary:8000/v1", "http://vllm-primary:8000/v1/models"),
        ("http://x:1/v1/models", "http://x:1/v1/models"),  # already /models — idempotent
        ("https://api.openai.com/v1", "https://api.openai.com/v1/models"),
        ("http://bare-host:9000", "http://bare-host:9000/v1/models"),  # no /v1 suffix
    ],
)
def test_models_probe_url(url, expected):
    assert _models_probe_url(url) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["anthropic", "openai", "ANTHROPIC"])
async def test_cloud_backends_short_circuit_without_probing(backend):
    """Cloud backends can't be SSH-woken and shouldn't be probed unauthenticated;
    assume reachable so chat proceeds (real failures surface downstream)."""
    with (
        patch.object(model_manager.shared, "MODEL_BACKEND", backend),
        patch.object(model_manager.shared, "_http") as http,
    ):
        http.get = AsyncMock(side_effect=AssertionError("cloud backend must not be probed"))
        assert await check_model_health() is True


@pytest.mark.asyncio
async def test_openai_compatible_hits_models_endpoint_and_is_healthy_on_200():
    resp = MagicMock(status_code=200)
    with (
        patch.object(model_manager.shared, "MODEL_BACKEND", "openai_compatible"),
        patch.object(model_manager.shared, "MODEL_URL", "http://host.docker.internal:11434/v1"),
        patch.object(model_manager.shared, "_http") as http,
    ):
        http.get = AsyncMock(return_value=resp)
        assert await check_model_health() is True
        assert http.get.call_args[0][0] == "http://host.docker.internal:11434/v1/models"


@pytest.mark.asyncio
async def test_non_200_is_unhealthy():
    """Ollama 404s /health but serves /v1/models; a non-200 here is a real outage."""
    resp = MagicMock(status_code=404)
    with (
        patch.object(model_manager.shared, "MODEL_BACKEND", "openai_compatible"),
        patch.object(model_manager.shared, "MODEL_URL", "http://host.docker.internal:11434/v1"),
        patch.object(model_manager.shared, "_http") as http,
    ):
        http.get = AsyncMock(return_value=resp)
        assert await check_model_health() is False


@pytest.mark.asyncio
async def test_connect_error_returns_false_so_wake_fires():
    """A sleeping local GPU box → probe raises → False → caller's SSH-wake fires.
    Preserves Helios auto-wake behavior."""
    with (
        patch.object(model_manager.shared, "MODEL_BACKEND", "openai_compatible"),
        patch.object(model_manager.shared, "MODEL_URL", "http://10.0.0.195:8080/v1"),
        patch.object(model_manager.shared, "_http") as http,
    ):
        http.get = AsyncMock(side_effect=Exception("connection refused"))
        assert await check_model_health() is False
