"""
Expert-tier reasoner delegate (Qwen3-32B Thinking on Saturn 3090).

Exposes the `ask_expert` tool to the primary model via the unified loop.
One-shot, blocking, stateless. The primary passes a self-contained question;
this handler ships it to llama-server on Saturn, waits for the full response
(thinking + final answer), and returns the final answer as a plain string.

Phase A findings baked in:
- llama.cpp with `--jinja` on Qwen3 separates `message.content` (final) from
  `message.reasoning_content` (the <think> trace) automatically. No regex
  stripping needed.
- Latency is 30-150s per call in practice. The timeout is 180s. The primary
  is prompted to warn the user before invoking.
- There is no "thinking budget" lever in llama-server for Qwen3; `max_tokens`
  only truncates, which yields empty `content`. So we set max_tokens high
  enough (8000) that real reasoning completes.

Handler state (module-level):
- `_circuit_failures` — consecutive failure counter
- `_circuit_opened_at` — monotonic timestamp of breaker trip, or None
The breaker matches the project pattern: after N failures (default 3) it
opens, returning a short-circuit response for `EXPERT_CIRCUIT_BREAKER_COOLDOWN_SECONDS`
before half-open retrying on the next call.
"""

from __future__ import annotations

import logging
import time

from orchestrator import shared
from orchestrator.metrics import (
    EXPERT_CALL_COUNT,
    EXPERT_CALL_LATENCY,
    EXPERT_CIRCUIT_OPEN,
    EXPERT_REASONING_TOKENS,
)
from orchestrator.tool_registry import register_tool

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are an expert reasoner. The user has a hard question that benefits "
    "from careful, step-by-step thinking. Answer directly and concisely. Do "
    "not ask clarifying questions — use your best interpretation of the "
    "question as given. If the question has multiple plausible interpretations, "
    "pick the most likely and note the assumption at the end. Keep the final "
    "answer focused on what was asked; detailed reasoning belongs in your "
    "thinking phase, not in the final text."
)

_DISABLED_MSG = "Expert model is disabled. Set EXPERT_ENABLED=true and EXPERT_MODEL_URL in .env to enable."
_UNREACHABLE_MSG = "Expert model is temporarily unavailable. Answering directly."
_CIRCUIT_OPEN_MSG = (
    "Expert model circuit breaker is open after repeated failures — answering directly for the next couple of minutes."
)

# Circuit breaker state (module-level; handler is single-process)
_circuit_failures: int = 0
_circuit_opened_at: float | None = None


def _circuit_is_open() -> bool:
    """Check circuit state, auto-close on cooldown expiry."""
    global _circuit_opened_at, _circuit_failures
    if _circuit_opened_at is None:
        return False
    if time.monotonic() - _circuit_opened_at >= shared.EXPERT_CIRCUIT_BREAKER_COOLDOWN_SECONDS:
        # Half-open: let the next call try
        logger.info("[expert] circuit breaker cooldown elapsed, half-open")
        _circuit_opened_at = None
        _circuit_failures = 0
        EXPERT_CIRCUIT_OPEN.set(0)
        return False
    return True


def _record_failure() -> None:
    global _circuit_failures, _circuit_opened_at
    _circuit_failures += 1
    if _circuit_failures >= shared.EXPERT_CIRCUIT_BREAKER_FAILURES and _circuit_opened_at is None:
        _circuit_opened_at = time.monotonic()
        EXPERT_CIRCUIT_OPEN.set(1)
        logger.warning(
            "[expert] circuit breaker OPENED after %d consecutive failures",
            _circuit_failures,
        )


def _record_success() -> None:
    global _circuit_failures, _circuit_opened_at
    _circuit_failures = 0
    if _circuit_opened_at is not None:
        _circuit_opened_at = None
        EXPERT_CIRCUIT_OPEN.set(0)
        logger.info("[expert] circuit breaker closed after successful call")


@register_tool("ask_expert")
async def handle_ask_expert(arguments: dict) -> str:
    """Delegate a hard reasoning task to the expert model.

    Arguments:
      question (str, required): the hard question. Self-contained — the
        expert has no memory of this conversation and no tools.
    """
    if not shared.EXPERT_ENABLED or not shared.EXPERT_MODEL_URL:
        EXPERT_CALL_COUNT.labels(result="disabled").inc()
        return _DISABLED_MSG

    if _circuit_is_open():
        EXPERT_CALL_COUNT.labels(result="circuit_open").inc()
        return _CIRCUIT_OPEN_MSG

    question = (arguments.get("question") or "").strip()
    if not question:
        EXPERT_CALL_COUNT.labels(result="error").inc()
        return "Expert call failed: `question` is required and must be non-empty."

    logger.info("[expert] ask_expert invoked (%d chars): %s", len(question), question[:120])
    t0 = time.time()

    try:
        # Local import to avoid circular dependency with orchestrator.orchestrator
        from orchestrator.orchestrator import call_model

        resp = await call_model(
            shared.EXPERT_MODEL_URL,
            shared.EXPERT_MODEL_NAME,
            messages=[{"role": "user", "content": question}],
            system=_SYSTEM_PROMPT,
            tools=None,
            tool_choice="none",
            timeout=shared.EXPERT_TIMEOUT_SECONDS,
        )
    except Exception as e:  # noqa: BLE001
        elapsed = time.time() - t0
        EXPERT_CALL_LATENCY.observe(elapsed)
        EXPERT_CALL_COUNT.labels(result="error").inc()
        _record_failure()
        logger.exception("[expert] call failed after %.1fs", elapsed)
        return f"{_UNREACHABLE_MSG} (error: {type(e).__name__})"

    elapsed = time.time() - t0
    EXPERT_CALL_LATENCY.observe(elapsed)

    try:
        message = resp["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        EXPERT_CALL_COUNT.labels(result="error").inc()
        _record_failure()
        logger.error("[expert] malformed response: %r", resp)
        return f"{_UNREACHABLE_MSG} (malformed response)"

    # llama.cpp --jinja separates these cleanly; no <think> stripping needed.
    content = (message.get("content") or "").strip()
    reasoning = message.get("reasoning_content") or ""
    usage = resp.get("usage") or {}
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    # Reasoning tokens are (approximately) completion_tokens minus the final
    # content length in tokens. We don't have a token count for `content`
    # here, so we record the completion-token total as an upper bound and
    # let the dashboard read it as "reasoning+final" — still useful.
    if completion_tokens:
        EXPERT_REASONING_TOKENS.observe(completion_tokens)

    if not content:
        # Truncation (ran out of max_tokens mid-thinking) is the classic
        # failure mode for Qwen3 when max_tokens is set too low. Count as an
        # error so the circuit breaker can trip on a pattern of these.
        EXPERT_CALL_COUNT.labels(result="error").inc()
        _record_failure()
        logger.warning(
            "[expert] empty content after %.1fs — completion_tokens=%d, "
            "reasoning_chars=%d. Likely max_tokens truncation.",
            elapsed,
            completion_tokens,
            len(reasoning),
        )
        return (
            "The expert ran out of tokens while thinking. This usually means "
            "the question needs a simpler framing or a higher EXPERT_MAX_TOKENS."
        )

    EXPERT_CALL_COUNT.labels(result="success").inc()
    _record_success()
    logger.info(
        "[expert] success in %.1fs (%d prompt + %d completion tokens, reasoning %d chars, final %d chars)",
        elapsed,
        prompt_tokens,
        completion_tokens,
        len(reasoning),
        len(content),
    )
    return content
