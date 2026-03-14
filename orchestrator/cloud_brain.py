"""
Cloud Brain for Brain Gateway.

Stateless LLM orchestration layer. Handles intent routing, prompt building,
LLM calls, and response streaming. Delegates tool execution and RAG to
the LocalAgent interface.

Today this runs in-process alongside the local agent. In a future phase,
this becomes a separate cloud service that calls the local agent via
HTTP/WebSocket.
"""

import hashlib
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi.responses import JSONResponse, StreamingResponse

import shared
from local_agent import LocalAgent
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
    Stateless LLM orchestration. Calls LocalAgent for infrastructure.

    Supports two architectures via shared.UNIFIED_MODE:
    - v6 hybrid: User → Helios → ask_orchestrator → Nemotron → Helios → User
    - v7 unified: User → single model (conversation + tools) → User
    """

    def __init__(
        self,
        local_agent: LocalAgent,
        call_model_fn,
        stream_final_response_fn,
        get_helios_system_prompt_fn,
        get_orchestrator_system_prompt_fn,
        check_helios_health_fn,
        start_helios_fn,
        try_fast_path_fn,
        is_greeting_fn,
        last_user_text_fn,
        clean_response_fn,
        parse_tool_calls_fn,
        helios_tools: List[Dict],
        helios_url: str,
        helios_model: str,
        nemotron_url: str,
        nemotron_model: str,
        # v7 unified mode additions (optional for backward compat)
        get_unified_system_prompt_fn=None,
        get_all_tools_fn=None,
        check_model_health_fn=None,
        start_model_server_fn=None,
        run_unified_loop_fn=None,
        model_url: str = "",
        model_name: str = "",
        fallback_model_url: str = "",
        fallback_model_name: str = "",
    ):
        """
        Initialize with function references from orchestrator.py.

        This avoids duplicating any logic — we just hold references to the
        existing functions and call them in the right order.
        """
        self.agent = local_agent
        self._call_model = call_model_fn
        self._stream_final_response = stream_final_response_fn
        self._get_helios_system_prompt = get_helios_system_prompt_fn
        self._get_orchestrator_system_prompt = get_orchestrator_system_prompt_fn
        self._check_helios_health = check_helios_health_fn
        self._start_helios = start_helios_fn
        self._try_fast_path = try_fast_path_fn
        self._is_greeting = is_greeting_fn
        self._last_user_text = last_user_text_fn
        self._clean_response = clean_response_fn
        self._parse_tool_calls = parse_tool_calls_fn
        self._helios_tools = helios_tools
        self._helios_url = helios_url
        self._helios_model = helios_model
        self._nemotron_url = nemotron_url
        self._nemotron_model = nemotron_model
        self._mode_router = get_mode_router()
        # v7 unified mode
        self._get_unified_system_prompt = get_unified_system_prompt_fn
        self._get_all_tools = get_all_tools_fn
        self._check_model_health = check_model_health_fn
        self._start_model_server = start_model_server_fn
        self._run_unified_loop = run_unified_loop_fn
        self._model_url = model_url
        self._model_name = model_name
        self._fallback_model_url = fallback_model_url
        self._fallback_model_name = fallback_model_name
        # Callback to update model idle tracker (set by orchestrator.py)
        self.on_helios_request = None
        # Callback to schedule auto-learn (set by orchestrator.py)
        self.on_conversation_update = None

    async def chat(
        self, messages: List[Dict], stream: bool = False, external_tools: Optional[List] = None, ha_client=None
    ) -> Any:
        """
        Main chat flow. Routes to unified (v7) or hybrid (v6) based on UNIFIED_MODE.

        Returns a FastAPI response (JSONResponse or StreamingResponse).
        """
        if shared.UNIFIED_MODE and self._run_unified_loop:
            return await self._chat_unified(messages, stream, external_tools, ha_client)
        return await self._chat_hybrid(messages, stream, external_tools, ha_client)

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
        # Route intent (unchanged from v6)
        intent = self._mode_router.route(user_text)
        routing_info["intent_mode"] = intent.mode
        routing_info["intent_intensity"] = intent.intensity
        routing_info["intent_tags"] = intent.tags
        MODE_ROUTE_COUNT.labels(mode=intent.mode, intensity=intent.intensity).inc()
        logger.info("[MODE_ROUTER] mode=%s intensity=%s tags=%s", intent.mode, intent.intensity, intent.tags)

        # Fast-path: simple device commands (unchanged)
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

        # RAG prefetch (unchanged)
        personal_context = ""
        if not self._is_greeting(user_text):
            personal_context = self.agent.rag_search(user_text)
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
            self._track_helios_request()
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

        # Track activity and schedule learning even on fallback path
        self._track_helios_request()
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

    async def _chat_hybrid(
        self, messages: List[Dict], stream: bool = False, external_tools: Optional[List] = None, ha_client=None
    ) -> Any:
        """Hybrid v6 chat flow — Helios for conversation, Nemotron for tools."""
        user_text = self._last_user_text(messages)

        routing_info = {
            "timestamp": datetime.now().isoformat(),
            "user_query_length": len(user_text),
            "architecture": "hybrid_v6",
            "tool_calls": [],
            "streaming": stream,
        }

        # Route intent
        intent = self._mode_router.route(user_text)
        routing_info["intent_mode"] = intent.mode
        routing_info["intent_intensity"] = intent.intensity
        routing_info["intent_tags"] = intent.tags
        logger.info(f"[MODE_ROUTER] mode={intent.mode} intensity={intent.intensity} tags={intent.tags}")

        # External tools passthrough (e.g., HA voice pipeline)
        if external_tools:
            logger.info(f"[HYBRID] External tools provided ({len(external_tools)}), passing to Nemotron")
            routing_info["mode"] = "passthrough"
            try:
                llm_resp = await self._call_model(
                    self._nemotron_url,
                    self._nemotron_model,
                    messages,
                    system=self._get_orchestrator_system_prompt(mode=intent.mode, intensity=intent.intensity),
                    tools=external_tools,
                    timeout=60,
                )
                llm_resp["_routing"] = routing_info
                return JSONResponse(llm_resp)
            except Exception as e:
                logger.error(f"[HYBRID] Passthrough failed: {e}")
                return JSONResponse({"error": "Service temporarily unavailable"}, status_code=503)

        # Hybrid mode
        routing_info["mode"] = "hybrid"
        logger.info(f"[HYBRID] Processing: {user_text[:100]}... (stream={stream})")

        # Fast-path: simple device commands
        if ha_client:
            try:
                fast_result = await self._try_fast_path(user_text, ha_client)
                if fast_result.handled:
                    routing_info["mode"] = "fast_path"
                    routing_info["fast_path_action"] = fast_result.action
                    routing_info["fast_path_entity"] = fast_result.entity_name
                    logger.info(f"[FAST-PATH] Handled: {fast_result.action} -> {fast_result.entity_name}")
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
                logger.warning(f"[FAST-PATH] Error, falling through to Helios: {e}")

        # 1. RAG prefetch
        personal_context = ""
        if not self._is_greeting(user_text):
            personal_context = self.agent.rag_search(user_text)
            if personal_context:
                logger.info(f"[HYBRID] Pre-fetched RAG context ({len(personal_context)} chars)")
                routing_info["rag_prefetch"] = True

        # 2. Build system prompt
        helios_system = self._get_helios_system_prompt(personal_context, mode=intent.mode, intensity=intent.intensity)

        # 3. Check Helios health, start if needed
        if not await self._check_helios_health():
            logger.info("[HYBRID] Helios offline, attempting to start...")
            started = await self._start_helios()
            if not started:
                logger.warning("[HYBRID] Helios unavailable, falling back to Nemotron")
                routing_info["fallback"] = "nemotron"
                return await self._nemotron_fallback(
                    messages,
                    stream,
                    routing_info,
                    mode=intent.mode,
                    intensity=intent.intensity,
                )

        # 4. Call Helios
        logger.info("[HYBRID] Calling Helios...")
        try:
            helios_resp = await self._call_model(
                self._helios_url,
                self._helios_model,
                messages,
                system=helios_system,
                tools=self._helios_tools,
                timeout=180,
            )
            self._track_helios_request()
        except Exception as e:
            logger.error(f"[HYBRID] Helios call failed: {e}")
            routing_info["fallback"] = "nemotron"
            routing_info["helios_error"] = "call_failed"
            return await self._nemotron_fallback(
                messages,
                stream,
                routing_info,
                mode=intent.mode,
                intensity=intent.intensity,
            )

        # 5. Parse tool calls
        choice = helios_resp.get("choices", [{}])[0]
        message = choice.get("message", {})
        tool_calls = message.get("tool_calls", [])
        content = message.get("content") or ""

        if not tool_calls and content:
            tool_calls = self._parse_tool_calls(content)

        # 6. Direct response (no tools)
        if not tool_calls:
            logger.info("[HYBRID] Helios responded directly (no orchestrator needed)")
            routing_info["helios_direct"] = True
            self._schedule_auto_learn(messages)
            if stream:
                return self._stream_text(self._clean_response(content), self._helios_model)
            if content:
                message["content"] = self._clean_response(content)
            helios_resp["_routing"] = routing_info
            return JSONResponse(helios_resp)

        # 7. Execute ask_orchestrator via local agent
        logger.info("[HYBRID] Helios called orchestrator, delegating to local agent")
        conversation = messages.copy()
        orchestrator_result = ""

        for tool_call in tool_calls:
            function = tool_call.get("function", {})
            tool_name = function.get("name", "")

            if tool_name != "ask_orchestrator":
                logger.warning(f"[HYBRID] Unexpected tool from Helios: {tool_name}")
                continue

            try:
                arguments = json.loads(function.get("arguments", "{}"))
            except json.JSONDecodeError:
                arguments = {}

            command = arguments.get("command", "")
            if not command:
                continue

            logger.info(f"[HYBRID] Orchestrator command: {command[:100]}...")
            routing_info["tool_calls"].append({"tool": "ask_orchestrator", "command": command})

            orchestrator_result = await self.agent.execute_orchestrator(
                command,
                mode=intent.mode,
                intensity=intent.intensity,
            )
            logger.info(f"[HYBRID] Orchestrator result: {orchestrator_result[:200]}...")

            conversation.append(
                {
                    "role": "assistant",
                    "content": f"I used the orchestrator to: {command}",
                }
            )
            conversation.append(
                {
                    "role": "user",
                    "content": (
                        f"Orchestrator result: {orchestrator_result}\n\n"
                        "Please respond naturally to me based on this result. "
                        "Keep it brief and conversational."
                    ),
                }
            )

        # 8. Final Helios response
        logger.info("[HYBRID] Getting final response from Helios...")
        try:
            final_resp = await self._call_model(
                self._helios_url,
                self._helios_model,
                conversation,
                system=helios_system,
                timeout=120,
            )
            self._track_helios_request()
        except Exception as e:
            logger.error(f"[HYBRID] Helios final response failed: {e}")
            if stream:
                return self._stream_text(orchestrator_result, self._nemotron_model)
            return JSONResponse(
                {
                    "choices": [{"message": {"role": "assistant", "content": orchestrator_result}}],
                    "_routing": routing_info,
                }
            )

        final_content = final_resp.get("choices", [{}])[0].get("message", {}).get("content", "")
        final_content = self._clean_response(final_content)

        self._schedule_auto_learn(messages)

        if stream:
            return self._stream_text(final_content, self._helios_model)

        final_resp["_routing"] = routing_info
        if final_content:
            final_resp["choices"][0]["message"]["content"] = final_content
        return JSONResponse(final_resp)

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

    def _track_helios_request(self):
        """Update Helios idle tracker."""
        if self.on_helios_request:
            self.on_helios_request()

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

    async def _nemotron_fallback(
        self, messages: List[Dict], stream: bool, routing_info: Dict, mode: str = "explainer", intensity: str = "low"
    ):
        """Fallback to Nemotron-only mode when Helios is unavailable."""
        logger.info("[FALLBACK] Using Nemotron-only mode")
        result = await self.agent.execute_orchestrator(
            self._last_user_text(messages),
            mode=mode,
            intensity=intensity,
        )
        if stream:
            return self._stream_text(result, self._nemotron_model)
        return JSONResponse(
            {
                "choices": [{"message": {"role": "assistant", "content": result}}],
                "_routing": routing_info,
            }
        )
