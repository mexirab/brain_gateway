"""
Cloud Brain for Brain Gateway.

Stateless LLM orchestration layer for the v7 unified architecture.
A single model handles both conversation and tool execution. Handles
intent routing, prompt building, LLM calls, and response streaming.
"""

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi.responses import JSONResponse, StreamingResponse

from orchestrator import setup_env, shared, welcome
from orchestrator.metrics import (
    ACTIVE_REQUESTS,
    FAST_PATH_COUNT,
    MODE_ROUTE_COUNT,
    REQUEST_COUNT,
    REQUEST_ERRORS,
    REQUEST_LATENCY,
    VOICE_PIPELINE_LATENCY,
    WELCOME_FIRED,
)
from orchestrator.mode_router import get_mode_router
from orchestrator.routes_setup import is_first_chat, mark_first_chat_done

logger = logging.getLogger(__name__)

# Strong references to fire-and-forget Helios wake tasks. `asyncio.create_task`
# only holds a weak reference, so a detached wake fired from a request handler
# that returns immediately can be GC'd before its first await (the HA POST) and
# silently never run. Hold a ref here and clear it via done-callback. Mirrors
# api_routes._BACKGROUND_TASKS.
_HELIOS_WAKE_TASKS: set = set()


def _maybe_prepend_welcome(text: str, *, is_voice: bool = False) -> str:
    """If this is the user's first chat, prepend the one-time welcome.

    Pulls assistant_name + user_name from `shared.profile`, the LAN IP from
    Settings (set host-side by install.sh), and the integration status from
    setup_env's overrides. Marks first_chat_completed on success so the
    welcome never fires twice.

    Returns the original text unchanged if first chat has already happened,
    or if this is a voice turn (a multi-line markdown welcome read aloud by
    TTS sounds awful and stomps over a short HA voice command).

    Welcome generation is best-effort onboarding polish — any exception
    is logged at ERROR (with stack trace, so the self-audit job catches
    it) and the original response goes through unchanged. The
    `mark_first_chat_done` write is deliberately AFTER the prepend +
    return — losing the mark just means the welcome fires once more, but
    losing a successfully-generated welcome would be worse.
    """
    if is_voice:
        return text
    if not is_first_chat():
        return text
    try:
        from orchestrator.config import settings  # local import: avoid module-load cycle

        prepend = welcome.generate_welcome(
            user_name=getattr(shared.profile, "user_name", None) or None,
            assistant_name=getattr(shared.profile, "assistant_name", "Jess"),
            lan_ip=settings.jess_lan_ip or None,
            env_overrides=setup_env.read_overrides(),
        )
    except Exception:
        WELCOME_FIRED.labels(result="error").inc()
        logger.exception("[WELCOME] Skipped due to error generating welcome")
        return text

    # Generation succeeded — best-effort mark, but never lose the welcome
    # over a transient setup_state.json write failure.
    try:
        mark_first_chat_done()
    except Exception:
        logger.exception("[WELCOME] Mark-done failed; welcome may fire again")

    WELCOME_FIRED.labels(result="prepended").inc()
    logger.info("[WELCOME] First-chat welcome prepended (%d chars)", len(prepend))
    return prepend + text


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
        self,
        messages: List[Dict],
        stream: bool = False,
        external_tools: Optional[List] = None,
        ha_client=None,
        is_voice: bool = False,
    ) -> Any:
        """
        Main chat flow — unified v7. Single model handles conversation + tools.

        Returns a FastAPI response (JSONResponse or StreamingResponse).
        """
        return await self._chat_unified(messages, stream, external_tools, ha_client, is_voice)

    async def _chat_unified(
        self,
        messages: List[Dict],
        stream: bool = False,
        external_tools: Optional[List] = None,
        ha_client=None,
        is_voice: bool = False,
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
            "is_voice": is_voice,
        }

        try:
            return await self._chat_unified_inner(
                messages, stream, external_tools, ha_client, user_text, routing_info, is_voice
            )
        except Exception as e:
            REQUEST_ERRORS.labels(mode="unified", error_type=type(e).__name__).inc()
            raise
        finally:
            elapsed = time.time() - _t0
            ACTIVE_REQUESTS.dec()
            REQUEST_LATENCY.labels(mode="unified").observe(elapsed)
            if is_voice:
                VOICE_PIPELINE_LATENCY.observe(elapsed)

    async def _chat_unified_inner(
        self,
        messages: List[Dict],
        stream: bool,
        external_tools: Optional[List],
        ha_client,
        user_text: str,
        routing_info: Dict,
        is_voice: bool = False,
    ) -> Any:
        """Inner unified flow (separated for clean metrics wrapping)."""
        # --- Vision: detect and process images before the main loop ---
        messages = await self._process_vision(messages, routing_info)

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
                    # Skip the welcome on the fast path too — fast-path
                    # is short HA voice-style commands ("turn on lights"),
                    # not the rich typed-chat first-touch we built the
                    # welcome for. is_voice would catch most of these,
                    # but pass it explicitly for clarity.
                    fast_response_text = _maybe_prepend_welcome(fast_result.response_text, is_voice=True)
                    if stream:
                        return self._stream_text(fast_response_text, "fast-path")
                    return JSONResponse(
                        {
                            "id": f"chatcmpl-fp-{int(time.time())}",
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": "fast-path",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": fast_response_text},
                                    "finish_reason": "stop",
                                }
                            ],
                            "_routing": routing_info,
                        }
                    )
            except Exception as e:
                logger.warning("[FAST-PATH] Error, falling through: %s", e)

        # RAG prefetch — skip for greetings and short voice utterances (< 6
        # words). Short voice turns rarely need RAG and the 2-3k chars of
        # prepended context adds ~600 tokens of prefill latency per turn.
        personal_context = ""
        skip_rag_short_voice = is_voice and len(user_text.split()) < 6
        if not self._is_greeting(user_text) and not skip_rag_short_voice:
            personal_context = await self._rag_search(user_text)
            if personal_context:
                logger.info("[UNIFIED] Pre-fetched RAG context (%d chars)", len(personal_context))
                routing_info["rag_prefetch"] = True
        elif skip_rag_short_voice:
            logger.info("[UNIFIED] Skipping RAG for short voice utterance (%d words)", len(user_text.split()))
            routing_info["rag_skipped"] = "short_voice"

        # Build unified system prompt
        system_prompt = self._get_unified_system_prompt(
            personal_context, mode=intent.mode, intensity=intent.intensity, is_voice=is_voice
        )

        # Voice mode: add conciseness hint to reduce TTS latency
        if is_voice:
            system_prompt += (
                "\n\n[VOICE MODE] The user is speaking via voice assistant. "
                "Keep responses concise (1-3 sentences). No markdown, no bullet points, "
                "no asterisks. Speak naturally as if talking to a friend. "
                "Use periods and commas only — no em-dashes, en-dashes, or semicolons "
                "(they confuse the sentence-split TTS and cause stuttering playback). "
                "Do NOT start with short filler acknowledgments like 'Got it', 'Sure', "
                "'Okay', or 'Alright' — the TTS stutters on very short opening utterances. "
                "Go directly into your answer."
            )
            logger.info("[UNIFIED] Voice mode active, conciseness hint added")

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
                return self._brain_asleep_response(stream, routing_info)

        # Run unified tool loop
        logger.info("[UNIFIED] Processing: %s...", user_text[:100])
        routing_info["mode"] = "unified"
        REQUEST_COUNT.labels(mode="unified").inc()
        model_url = self._model_url
        model_name = self._model_name
        # Voice mode uses a trimmed tool subset — drops ~3.5k tokens of tool
        # schemas from prefill (verbose/debug/typed-only tools aren't reachable
        # by voice). Typed chat gets the full roster. `ask_expert` is
        # deliberately absent from VOICE_TOOL_NAMES because its 30-150s
        # latency is incompatible with a conversational voice turn.
        if is_voice:
            from orchestrator.tool_definitions import get_voice_tools

            tools = get_voice_tools()
        else:
            tools = self._get_all_tools()

        # Observability: prompt-size breakdown per turn. Voice prefill is
        # dominated by tool schemas + system prompt; logging per-turn sizes
        # lets us notice regressions when new tools or prompt sections land.
        try:
            import json as _json

            _sys_chars = len(system_prompt or "")
            # tools is a cached list that only changes when the HA tool cache
            # refreshes — serialize it once per cache generation, not per turn
            _size_cache = getattr(self, "_tools_size_cache", None)
            if _size_cache is None or _size_cache[0] is not tools:
                _size_cache = (tools, len(_json.dumps(tools)))
                self._tools_size_cache = _size_cache
            _tools_chars = _size_cache[1]
            _msgs_chars = sum(len(_json.dumps(m)) for m in messages)
            logger.info(
                "[UNIFIED] Prompt sizes (chars): system=%d tools=%d messages=%d total=%d (voice=%s tools_n=%d)",
                _sys_chars,
                _tools_chars,
                _msgs_chars,
                _sys_chars + _tools_chars + _msgs_chars,
                is_voice,
                len(tools),
            )
        except Exception as _e:
            logger.debug("[UNIFIED] Prompt size diagnostic failed: %s", _e)

        # Strip incoming system messages — the orchestrator builds its own system
        # prompt (with RAG, mode, tools). External callers like HA's llama_conversation
        # send a system message with all entity states; keeping it causes Qwen's Jinja
        # template to fail ("System message must be at the beginning" / duplicate system).
        messages = [m for m in messages if m.get("role") != "system"]

        try:
            result = await self._run_unified_loop(
                messages=messages.copy(),
                system_prompt=system_prompt,
                tools=tools,
                model_url=model_url,
                model_name=model_name,
                http_client=None,  # resolved by call_model
                is_voice=is_voice,
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

        result = _maybe_prepend_welcome(result, is_voice=is_voice)

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
            return self._brain_asleep_response(stream, routing_info)

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

    # --- Vision processing ---

    async def _process_vision(self, messages: List[Dict], routing_info: Dict) -> List[Dict]:
        """Detect images in messages, analyze via vision model, replace with text descriptions.

        Images are sent to the dedicated vision model on Saturn. The text description
        replaces the image content so Helios can reason about it with tools.
        """
        if not shared.VISION_ENABLED:
            return messages

        from orchestrator.vision_handler import analyze_image, extract_images_from_messages

        images = extract_images_from_messages(messages)
        if not images:
            return messages

        logger.info("[VISION] Found %d image(s) in messages, routing to vision model", len(images))
        routing_info["vision_images"] = len(images)

        # Process images and collect descriptions per message index
        processed_msgs = [m.copy() for m in messages]
        descriptions_by_idx: Dict[int, list] = {}

        for img_info in images:
            idx = img_info["msg_index"]
            prompt = img_info["text"] or "Describe this image in detail, including any text visible."
            image_url = img_info["image_url"]

            # Cache the image for follow-up tool calls (key from image data, not prompt)
            cache_key = hashlib.sha256(image_url[:200].encode()).hexdigest()[:12]
            shared._vision_image_cache[cache_key] = image_url
            # Cap cache at 5 entries
            while len(shared._vision_image_cache) > 5:
                oldest = next(iter(shared._vision_image_cache))
                shared._vision_image_cache.pop(oldest, None)

            description = await analyze_image(image_url, prompt)
            descriptions_by_idx.setdefault(idx, []).append(description)

        # Replace multipart content with all image descriptions for that message
        for idx, descs in descriptions_by_idx.items():
            original_text = ""
            for img in images:
                if img["msg_index"] == idx and img["text"]:
                    original_text = img["text"]
                    break
            analysis_parts = [f"[Image {i + 1} Analysis]\n{d}" for i, d in enumerate(descs)]
            replacement = "\n\n".join(analysis_parts)
            if original_text:
                replacement = f"{original_text}\n\n{replacement}"

            processed_msgs[idx] = {
                "role": "user",
                "content": replacement,
            }

        logger.info("[VISION] Processed %d image(s), injected descriptions into messages", len(images))

        # Strip any remaining base64/image content from ALL messages before sending to text-only LLM
        # Open WebUI resends full conversation history including old images
        for i, msg in enumerate(processed_msgs):
            content = msg.get("content")
            if isinstance(content, list):
                text_parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                    elif isinstance(part, dict) and part.get("type") == "image_url":
                        text_parts.append("[Previously shared image]")
                if text_parts:
                    processed_msgs[i] = {
                        "role": msg.get("role", "user"),
                        "content": "\n".join(text_parts),
                    }

        return processed_msgs

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
                    elif isinstance(content, list):
                        # Extract text from multipart content (images + text)
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                first_user = part.get("text", "")[:100]
                                break
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
        from orchestrator.auto_learn import run_auto_learn

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

    def _maybe_wake_helios(self, routing_info: Dict) -> bool:
        """Fire a debounced, fire-and-forget Helios wake if the feature is on.

        Returns True if a wake was dispatched (so the caller can tailor the
        reply), False if the feature is off. The actual HA call runs in a
        detached task — it never blocks this reply, and its own debounce/disable
        guards make a no-op cheap when Helios is already booting. We do NOT await
        it: the 2-minute boot is far longer than any request budget.
        """
        if not shared.HELIOS_WAKE_ENABLED:
            return False
        try:
            from orchestrator.helios_power import wake_helios

            task = asyncio.create_task(wake_helios())
            _HELIOS_WAKE_TASKS.add(task)
            task.add_done_callback(_HELIOS_WAKE_TASKS.discard)
            routing_info["helios_wake"] = "dispatched"
            logger.info("[UNIFIED] Brain asleep + HELIOS_WAKE_ENABLED — dispatched wake")
            return True
        except Exception as e:  # noqa: BLE001 — never let wake break the asleep reply
            logger.warning("[UNIFIED] Helios wake dispatch failed: %s", e)
            routing_info["helios_wake"] = "dispatch_failed"
            return False

    def _brain_asleep_response(self, stream: bool, routing_info: Dict):
        """Graceful 'the model isn't reachable' reply.

        Returned (HTTP 200, in OpenAI chat-completion shape) instead of a bare
        503 so the chat UI renders it as Jess speaking, not an opaque error
        toast. If this code path runs at all, the orchestrator itself is up — so
        the background nervous system (reminders, calendar, nudges) is still
        alive; only live conversation + tools are down (e.g. the GPU box is
        asleep, or a BYO model server is offline). The caller still increments
        the relevant REQUEST_ERRORS metric, so dashboards/alerts see the outage.

        Helios wake-on-demand (PT-C): when HELIOS_WAKE_ENABLED, the model being
        unreachable usually means the GPU box is powered off, so we fire a wake
        (debounced, fire-and-forget — never blocks this reply) and tell the user
        it's booting. The HTTP-200 friendly-reply contract is unchanged.
        """
        woke = self._maybe_wake_helios(routing_info)
        if woke:
            message = (
                "💤 Helios — my GPU box — is asleep, so I can't chat or run tools "
                "right this second. I'm waking it now; give it about two minutes, "
                "then try again.\n\n"
                "Your reminders, calendar, and nudges keep running in the "
                "background the whole time."
            )
        else:
            message = (
                "💤 My conversational brain is offline right now — the model server "
                "isn't reachable, so I can't chat or run tools at the moment.\n\n"
                "Your reminders, calendar, and nudges keep running in the background. "
                "Once the model is back up, try again."
            )
        routing_info["mode"] = "brain_asleep"
        model = self._model_name or "brain-asleep"
        if stream:
            return self._stream_text(message, model)
        return JSONResponse(
            {
                "id": f"chatcmpl-asleep-{int(time.time())}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": message},
                        "finish_reason": "stop",
                    }
                ],
                "_routing": routing_info,
            }
        )
