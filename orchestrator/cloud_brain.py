"""
Cloud Brain for Brain Gateway.

Stateless LLM orchestration layer for the v7 unified architecture.
A single model handles both conversation and tool execution. Handles
intent routing, prompt building, LLM calls, and response streaming.
"""

import hashlib
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi.responses import JSONResponse, StreamingResponse

import shared
from metrics import (
    ACTIVE_REQUESTS,
    FAST_PATH_COUNT,
    MODE_ROUTE_COUNT,
    REQUEST_COUNT,
    REQUEST_ERRORS,
    REQUEST_LATENCY,
)
from mode_router import get_mode_router

logger = logging.getLogger(__name__)


class CloudBrain:
    """
    Stateless LLM orchestration for the v7 unified architecture.

    Single model handles conversation + tools in one agentic loop.
    """

    def __init__(
        self,
        call_model_fn,
        try_fast_path_fn,
        is_greeting_fn,
        last_user_text_fn,
        rag_search_fn,
        get_unified_system_prompt_fn,
        get_all_tools_fn,
        check_model_health_fn,
        start_model_server_fn,
        run_unified_loop_fn,
        model_url: str,
        model_name: str,
        fallback_model_url: str = "",
        fallback_model_name: str = "",
    ):
        """
        Initialize with function references from orchestrator.py.

        This avoids duplicating any logic — we just hold references to the
        existing functions and call them in the right order.
        """
        self._call_model = call_model_fn
        self._try_fast_path = try_fast_path_fn
        self._is_greeting = is_greeting_fn
        self._last_user_text = last_user_text_fn
        self._rag_search = rag_search_fn
        self._mode_router = get_mode_router()
        self._get_unified_system_prompt = get_unified_system_prompt_fn
        self._get_all_tools = get_all_tools_fn
        self._check_model_health = check_model_health_fn
        self._start_model_server = start_model_server_fn
        self._run_unified_loop = run_unified_loop_fn
        self._model_url = model_url
        self._model_name = model_name
        self._fallback_model_url = fallback_model_url
        self._fallback_model_name = fallback_model_name
        # Callback to schedule auto-learn (set by orchestrator.py)
        self.on_conversation_update = None

    async def chat(
        self, messages: List[Dict], stream: bool = False, external_tools: Optional[List] = None, ha_client=None
    ) -> Any:
        """
        Main chat flow — unified v7. Single model handles conversation + tools.

        Returns a FastAPI response (JSONResponse or StreamingResponse).
        """
        return await self._chat_unified(messages, stream, external_tools, ha_client)

    async def _chat_unified(
        self, messages: List[Dict], stream: bool = False, external_tools: Optional[List] = None, ha_client=None
    ) -> Any:
        """Unified v7 chat flow — single model handles conversation + tools."""
        _t0 = time.time()
        ACTIVE_REQUESTS.inc()
        user_text = self._last_user_text(messages)

        routing_info = {
            "timestamp": datetime.now().isoformat(),
            "user_query_length": len(user_text),
            "architecture": "unified_v7",
            "tool_calls": [],
            "streaming": stream,
        }

        try:
            return await self._chat_unified_inner(messages, stream, external_tools, ha_client, user_text, routing_info)
        except Exception as e:
            REQUEST_ERRORS.labels(mode="unified", error_type=type(e).__name__).inc()
            raise
        finally:
            ACTIVE_REQUESTS.dec()
            REQUEST_LATENCY.labels(mode="unified").observe(time.time() - _t0)

    async def _chat_unified_inner(
        self,
        messages: List[Dict],
        stream: bool,
        external_tools: Optional[List],
        ha_client,
        user_text: str,
        routing_info: Dict,
    ) -> Any:
        """Inner unified flow (separated for clean metrics wrapping)."""
        # Route intent
        intent = self._mode_router.route(user_text)
        routing_info["intent_mode"] = intent.mode
        routing_info["intent_intensity"] = intent.intensity
        routing_info["intent_tags"] = intent.tags
        MODE_ROUTE_COUNT.labels(mode=intent.mode, intensity=intent.intensity).inc()
        logger.info("[MODE_ROUTER] mode=%s intensity=%s tags=%s", intent.mode, intent.intensity, intent.tags)

        # Fast-path: simple device commands
        if ha_client:
            try:
                fast_result = await self._try_fast_path(user_text, ha_client)
                if fast_result.handled:
                    routing_info["mode"] = "fast_path"
                    routing_info["fast_path_action"] = fast_result.action
                    routing_info["fast_path_entity"] = fast_result.entity_name
                    REQUEST_COUNT.labels(mode="fast_path").inc()
                    FAST_PATH_COUNT.labels(action=fast_result.action or "unknown").inc()
                    logger.info("[FAST-PATH] Handled: %s -> %s", fast_result.action, fast_result.entity_name)
                    if stream:
                        return self._stream_text(fast_result.response_text, "fast-path")
                    return JSONResponse(
                        {
                            "id": f"chatcmpl-fp-{int(time.time())}",
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": "fast-path",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": fast_result.response_text},
                                    "finish_reason": "stop",
                                }
                            ],
                            "_routing": routing_info,
                        }
                    )
            except Exception as e:
                logger.warning("[FAST-PATH] Error, falling through: %s", e)

        # RAG prefetch
        personal_context = ""
        if not self._is_greeting(user_text):
            personal_context = self._rag_search(user_text)
            if personal_context:
                logger.info("[UNIFIED] Pre-fetched RAG context (%d chars)", len(personal_context))
                routing_info["rag_prefetch"] = True

        # Build unified system prompt
        system_prompt = self._get_unified_system_prompt(personal_context, mode=intent.mode, intensity=intent.intensity)

        # Check model health, start if needed
        if not await self._check_model_health():
            logger.info("[UNIFIED] Model offline, attempting to start...")
            started = await self._start_model_server()
            if not started:
                # Try fallback model
                if self._fallback_model_url:
                    logger.warning("[UNIFIED] Primary unavailable, using fallback")
                    routing_info["fallback"] = "fallback_model"
                    REQUEST_COUNT.labels(mode="unified_fallback").inc()
                    return await self._unified_fallback(messages, system_prompt, stream, routing_info)
                logger.error("[UNIFIED] No model available")
                REQUEST_ERRORS.labels(mode="unified", error_type="model_unavailable").inc()
                return JSONResponse({"error": "Model unavailable"}, status_code=503)

        # Run unified tool loop
        logger.info("[UNIFIED] Processing: %s...", user_text[:100])
        routing_info["mode"] = "unified"
        REQUEST_COUNT.labels(mode="unified").inc()
        model_url = self._model_url
        model_name = self._model_name
        tools = self._get_all_tools()

        try:
            result = await self._run_unified_loop(
                messages=messages.copy(),
                system_prompt=system_prompt,
                tools=tools,
                model_url=model_url,
                model_name=model_name,
                http_client=None,  # resolved by call_model
            )
        except Exception as e:
            logger.error("[UNIFIED] Tool loop failed: %s", e)
            REQUEST_ERRORS.labels(mode="unified", error_type="tool_loop_failed").inc()
            if self._fallback_model_url:
                routing_info["fallback"] = "fallback_model"
                REQUEST_COUNT.labels(mode="unified_fallback").inc()
                return await self._unified_fallback(messages, system_prompt, stream, routing_info)
            return JSONResponse({"error": "Service temporarily unavailable"}, status_code=503)

        self._schedule_auto_learn(messages)

        if stream:
            return self._stream_text(result, model_name)
        return JSONResponse(
            {
                "id": f"chatcmpl-{int(time.time())}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": result},
                        "finish_reason": "stop",
                    }
                ],
                "_routing": routing_info,
            }
        )

    async def _unified_fallback(
        self, messages: List[Dict], system_prompt: str, stream: bool, routing_info: Dict
    ) -> Any:
        """Fallback to secondary model using the same unified loop."""
        logger.info("[UNIFIED-FALLBACK] Using fallback model: %s", self._fallback_model_name)
        try:
            result = await self._run_unified_loop(
                messages=messages.copy(),
                system_prompt=system_prompt,
                tools=self._get_all_tools(),
                model_url=self._fallback_model_url,
                model_name=self._fallback_model_name,
                http_client=None,
            )
        except Exception as e:
            logger.error("[UNIFIED-FALLBACK] Fallback also failed: %s", e)
            REQUEST_ERRORS.labels(mode="unified_fallback", error_type="fallback_failed").inc()
            return JSONResponse({"error": "All models unavailable"}, status_code=503)

        self._schedule_auto_learn(messages)

        if stream:
            return self._stream_text(result, self._fallback_model_name)
        return JSONResponse(
            {
                "id": f"chatcmpl-fb-{int(time.time())}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": self._fallback_model_name,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": result},
                        "finish_reason": "stop",
                    }
                ],
                "_routing": routing_info,
            }
        )

    # --- Helpers ---

    def _schedule_auto_learn(self, messages: List[Dict]):
        """Cache conversation and schedule auto-learn extraction after inactivity timeout."""
        if not shared.AUTO_LEARN_ENABLED:
            return
        try:
            # Generate session key from first user message + date
            first_user = ""
            for m in messages:
                if m.get("role") == "user":
                    content = m.get("content", "")
                    if isinstance(content, str):
                        first_user = content[:100]
                    break
            msg_count = sum(1 for m in messages if m.get("role") == "user")
            session_key = hashlib.sha256(f"{first_user}:{datetime.now().date()}:{msg_count}".encode()).hexdigest()[:16]

            # Cache in-memory (never to disk) — cap at 20 entries to prevent memory leaks
            if len(shared._auto_learn_conversations) >= 20:
                oldest = next(iter(shared._auto_learn_conversations))
                shared._auto_learn_conversations.pop(oldest, None)
            shared._auto_learn_conversations[session_key] = messages

            # Schedule/reschedule extraction job
            job_id = f"auto_learn_{session_key}"

            import contextlib

            with contextlib.suppress(Exception):
                shared.scheduler.remove_job(job_id)

            from datetime import timedelta

            run_at = datetime.now() + timedelta(minutes=shared.AUTO_LEARN_DELAY_MINUTES)
            shared.scheduler.add_job(
                self._run_auto_learn_job,
                trigger="date",
                run_date=run_at,
                args=[session_key],
                id=job_id,
                replace_existing=True,
            )
        except Exception as e:
            logger.warning("[AUTO_LEARN] Failed to schedule extraction: %s", e)

    @staticmethod
    async def _run_auto_learn_job(session_key: str):
        """Scheduler callback: run auto-learn and clear conversation cache."""
        from auto_learn import run_auto_learn

        messages = shared._auto_learn_conversations.pop(session_key, None)
        if messages:
            await run_auto_learn(messages)

    def _stream_text(self, text: str, model: str):
        """Stream a text response in SSE format."""
        chunk_id = f"chatcmpl-{int(time.time())}"

        async def generate():
            chunk_size = 80
            for i in range(0, len(text), chunk_size):
                chunk_text = text[i : i + chunk_size]
                chunk_data = {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": chunk_text},
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk_data)}\n\n"
            final_chunk = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(final_chunk)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
