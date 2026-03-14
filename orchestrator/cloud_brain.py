"""
Cloud Brain for Brain Gateway.

Stateless LLM orchestration layer. Handles intent routing, prompt building,
LLM calls, and response streaming. Delegates tool execution and RAG to
the LocalAgent interface.

Today this runs in-process alongside the local agent. In a future phase,
this becomes a separate cloud service that calls the local agent via
HTTP/WebSocket.
"""

import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi.responses import JSONResponse, StreamingResponse

from local_agent import LocalAgent
from mode_router import get_mode_router

logger = logging.getLogger(__name__)


class CloudBrain:
    """
    Stateless LLM orchestration. Calls LocalAgent for infrastructure.

    Extracted from chat_completions() in orchestrator.py. The chat() method
    implements the full hybrid v6 flow:
      User → intent routing → RAG → Helios → ask_orchestrator → Nemotron → Helios → User
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
        # Callback to update Helios idle tracker (set by orchestrator.py)
        self.on_helios_request = None

    async def chat(
        self, messages: List[Dict], stream: bool = False, external_tools: Optional[List] = None, ha_client=None
    ) -> Any:
        """
        Main chat flow — hybrid v6 architecture.

        Returns a FastAPI response (JSONResponse or StreamingResponse).
        """
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

        if stream:
            return self._stream_text(final_content, self._helios_model)

        final_resp["_routing"] = routing_info
        if final_content:
            final_resp["choices"][0]["message"]["content"] = final_content
        return JSONResponse(final_resp)

    # --- Helpers ---

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
