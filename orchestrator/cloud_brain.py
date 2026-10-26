"""
Cloud Brain for Brain Gateway.

Stateless LLM orchestration layer for the v7 unified architecture.
A single model handles both conversation and tool execution. Handles
intent routing, prompt building, LLM calls, and response streaming.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi.responses import JSONResponse, StreamingResponse

from orchestrator import setup_env, shared, welcome
from orchestrator.metrics import (
    ACTIVE_REQUESTS,
    AUTO_LEARN_DEFERRED,
    AUTO_LEARN_DROPPED,
    AUTO_LEARN_REARMED,
    CHAT_STREAM_OUTCOME,
    CHAT_TTFT,
    CLIENT_MARKUP_NEUTRALIZED,
    FAST_PATH_COUNT,
    MODE_ROUTE_COUNT,
    REQUEST_COUNT,
    REQUEST_ERRORS,
    REQUEST_LATENCY,
    VISION_REQUEST_COUNT,
    VOICE_PIPELINE_LATENCY,
    WELCOME_FIRED,
)
from orchestrator.mode_router import get_mode_router
from orchestrator.routes_setup import is_first_chat, mark_first_chat_done
from orchestrator.unified_loop import _neutralize_control_markup
from orchestrator.vision_handler import (
    MAX_IMAGES_PER_REQUEST,
    NOT_ANALYZED_DISABLED,
    NOT_ANALYZED_NOT_INLINE,
    NOT_ANALYZED_OVER_CAP,
    PHOTO_BLOCK_RE,
    _is_safe_image_url,
    frame_photo_description,
    image_part_url,
    photo_not_analyzed,
)
from orchestrator.voice_style import apply_tone_request, record_reply_style

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


def _is_empty_assistant(m: dict) -> bool:
    """An assistant history turn with no text (and no tool calls)."""
    if m.get("role") != "assistant" or m.get("tool_calls"):
        return False
    content = m.get("content")
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        for p in content:
            if not isinstance(p, dict) or p.get("type") != "text":
                return False  # defensive: _sanitize_client_history flattens lists first
            t = p.get("text")
            if not isinstance(t, str) or t.strip():
                return False  # odd/non-string text is kept, never crashes
        return True
    return False


def _text_only(content: list) -> tuple[str, int]:
    """The text parts of an OpenAI content list, joined, and how many other parts were dropped."""
    texts: List[str] = []
    dropped = 0
    for part in content:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        else:
            dropped += 1
    return "\n".join(texts).strip(), dropped


def _flatten_content(
    messages: List[Dict], descriptions: Dict[tuple[int, int], str], vision_on: bool
) -> tuple[List[Dict], Dict[str, int]]:
    """The messages with every content list turned into the string the loop is given.

    No image part ever reaches the brain: it would skip the untrusted PHOTO
    CONTENT framing, and llama-server downloads http(s) image URLs itself. In a
    user turn each image becomes a block after the text (where PHOTO_BLOCK_RE
    expects it): the framed description from `descriptions[(msg_index,
    part_index)]`, or a NOT ANALYZED note for a link, vision switched off, or
    an image past the per-request cap. Other roles keep their text only, and
    other part types (input_audio, video_url, ...) are dropped everywhere.
    Returns (messages, counts of each kind of image or part not analyzed).
    """
    counts = {"not_inline": 0, "disabled": 0, "over_cap": 0, "other_parts": 0}
    notes = 0
    out: List[Dict] = []
    for i, msg in enumerate(messages):
        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue
        if msg.get("role") != "user":
            text, dropped = _text_only(content)
            counts["other_parts"] += dropped
            out.append({"role": msg.get("role"), "content": text})
            continue
        texts: List[str] = []
        blocks: List[str] = []
        for j, part in enumerate(content):
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
            elif isinstance(part, dict) and part.get("type") == "image_url":
                n = len(blocks) + 1
                if not _is_safe_image_url(image_part_url(part)):
                    kind, reason = "not_inline", NOT_ANALYZED_NOT_INLINE
                elif not vision_on:
                    kind, reason = "disabled", NOT_ANALYZED_DISABLED
                elif (i, j) in descriptions:
                    blocks.append(frame_photo_description(n, descriptions[(i, j)]))
                    continue
                else:  # extraction took every inline image; the cap dropped this one
                    kind, reason = "over_cap", NOT_ANALYZED_OVER_CAP
                counts[kind] += 1
                # A reason on the first few notes of the request, then a bare
                # note shorter than any image part: a request full of empty
                # image parts can't make the prompt bigger than itself.
                notes += 1
                blocks.append(photo_not_analyzed(n, reason if notes <= MAX_IMAGES_PER_REQUEST else ""))
            else:
                counts["other_parts"] += 1
        text = "\n".join(texts).strip()
        out.append({"role": "user", "content": "\n\n".join(([text] if text else []) + blocks)})
    return out, counts


def _sanitize_client_history(messages: List[Dict]) -> List[Dict]:
    """The client's messages without the turns no loop may be given, and with
    tool-call / chat-template markup in the rest neutralized.

    Runs first in _chat_unified_inner, before anything hands the history to
    vision, a model loop or auto-learn, so the primary loop, real streaming
    and both fallback-model paths all get the cleaned list.
    """
    # Strip incoming system messages — the orchestrator builds its own system
    # prompt (with RAG, mode, tools). External callers like HA's llama_conversation
    # send a system message with all entity states; keeping it causes Qwen's Jinja
    # template to fail ("System message must be at the beginning" / duplicate system).
    # Also strip client-supplied `tool` turns: the orchestrator is the only
    # legitimate producer of tool results, and a forged one is believed by
    # the model as its own verified output (2026-10-02 red-team finding).
    # Assistant turns are kept (they are the client's own transcript) but
    # ChatMessage has no tool_calls field, so forged native calls are
    # already dropped by Pydantic.
    # Only user/assistant turns survive: ChatMessage.role is free text, and the
    # Qwen3.8 template merges a leading `developer` turn into the system prompt
    # (other unknown roles make it raise and the turn fail).
    n_system_tool = sum(1 for m in messages if m.get("role") in ("system", "tool"))
    _n_before = len(messages)
    messages = [m for m in messages if m.get("role") in ("user", "assistant")]
    if n_system_tool:
        logger.info("[UNIFIED] Stripped %d client system/tool message(s)", n_system_tool)
    if _n_before - len(messages) > n_system_tool:
        logger.info(
            "[UNIFIED] Stripped %d client message(s) with another role", _n_before - len(messages) - n_system_tool
        )

    # Assistant turns reach the loop as text only. Their image (or audio,
    # video) parts would go to the multimodal brain without the PHOTO CONTENT
    # framing vision gives user photos — and llama-server downloads http(s)
    # image URLs itself (blind GET SSRF from Helios). Before the empty-turn
    # check, so a turn that was only an image is dropped.
    n_media = n_images = 0
    flat: List[Dict] = []
    for m in messages:
        if m.get("role") != "user" and isinstance(m.get("content"), list):
            n_images += sum(1 for p in m["content"] if isinstance(p, dict) and p.get("type") == "image_url")
            text, dropped = _text_only(m["content"])
            n_media += dropped
            m = {"role": m["role"], "content": text}
        flat.append(m)
    messages = flat
    if n_images:
        # Same SSRF vector as a link in a user turn: visible to the self-audit.
        VISION_REQUEST_COUNT.labels(status="rejected_assistant").inc(n_images)
        logger.warning("[UNIFIED] Dropped %d image part(s) from client assistant turns — not forwarded", n_images)
    if n_media > n_images:
        logger.info("[UNIFIED] Dropped %d other non-text part(s) from client assistant turns", n_media - n_images)

    # Drop empty assistant turns (a failed reply the client kept as a blank
    # bubble). One empty assistant message in the history made the Qwen3.8
    # brain answer with nothing 7/8 times — so a single empty reply
    # snowballed into a silent conversation (2026-10-14).
    _n_before = len(messages)
    messages = [m for m in messages if not _is_empty_assistant(m)]
    if len(messages) != _n_before:
        logger.info("[UNIFIED] Dropped %d empty assistant message(s) from history", _n_before - len(messages))

    # Neutralize control markup in what the client typed or kept, the same
    # rewrite tool results get. A typed `<tool_call>…</tool_call>` assistant
    # turn followed by a `<tool_response>…</tool_response>` user turn renders
    # byte-identical to a real tool round, and `<|im_end|><|im_start|>system`
    # in content is tokenized as real special tokens by the live llama.cpp
    # build — a forged system turn. Outsiders reach this through any text the
    # dashboard resends, e.g. an announcement quoting a work calendar invite
    # title (2026-10-25 review). Jess's own replies never carry these tags
    # (StreamGate / clean_response remove them), so normal history is unchanged.
    # The metric's position label: "latest" is the turn this request adds,
    # "history" the turns the client resends every time (counted again each turn).
    neutralized = {"user": 0, "assistant": 0}
    out = []
    for i, m in enumerate(messages):
        cleaned = _neutralize_turn(m)
        if cleaned is not m:
            neutralized[m["role"]] += 1
            position = "latest" if i == len(messages) - 1 else "history"
            CLIENT_MARKUP_NEUTRALIZED.labels(role=m["role"], position=position).inc()
        out.append(cleaned)
    if any(neutralized.values()):
        logger.info(
            "[UNIFIED] Neutralized control markup in client history (user=%d assistant=%d)",
            neutralized["user"],
            neutralized["assistant"],
        )
    return out


def _neutralize_turn(m: Dict) -> Dict:
    """`m` with control markup in its text rewritten as plain text.

    Returns `m` itself when nothing changed, else a copy (the caller's dicts
    are never edited). Only text is touched — a string part, or any dict
    part's string `text` whatever its `type` — image parts pass through as-is.
    """
    c = m.get("content")
    if isinstance(c, str):
        cleaned = _neutralize_control_markup(c)
        return m if cleaned == c else {**m, "content": cleaned}
    if not isinstance(c, list):
        return m
    parts, changed = [], False
    for p in c:
        if isinstance(p, str):
            t = _neutralize_control_markup(p)
            if t != p:
                p, changed = t, True
        elif isinstance(p, dict) and isinstance(p.get("text"), str):
            t = _neutralize_control_markup(p["text"])
            if t != p["text"]:
                p, changed = {**p, "text": t}, True
        parts.append(p)
    return {**m, "content": parts} if changed else m


# Set per request by the chat endpoint from ChatRequest.conversation_id.
conversation_id_var: ContextVar[Optional[str]] = ContextVar("conversation_id", default=None)

# Photo-analysis blocks appended to a user turn (one definition, shared).
_IMAGE_ANALYSIS_RE = PHOTO_BLOCK_RE


def _user_texts(messages: List[Dict]) -> List[str]:
    """User turns' text, without vision-analysis text (re-generated each turn)."""
    out = []
    for m in messages:
        if m.get("role") != "user":
            continue
        c = m.get("content", "")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
        c = c if isinstance(c, str) else ""
        out.append(_IMAGE_ANALYSIS_RE.sub("", c).strip())
    return out


_AUTO_LEARN_MAX_MESSAGES = 200  # merged-window cap per cached conversation
_AUTO_LEARN_RETRY_MINUTES = 30  # brain unreachable at fire time → try again
_AUTO_LEARN_MAX_ATTEMPTS = 16  # 15 retries ≈ 7.5h (Helios asleep overnight)


def _strip_photo_content(m: Dict) -> Dict:
    """Copy of a message with photo-analysis text replaced by a `[photo]` marker.

    What a photo shows (a letter, a screen, someone else's note) is untrusted
    content, not facts about the user — it must never reach auto-learn and
    become long-term memory. Image parts (vision off / not processed) become
    the marker too, so no base64 is cached either.
    """
    c = m.get("content")
    if isinstance(c, str):
        if not _IMAGE_ANALYSIS_RE.search(c):
            return m
        own = _IMAGE_ANALYSIS_RE.sub("", c).strip()
        return {**m, "content": f"{own}\n\n[photo]" if own else "[photo]"}
    if isinstance(c, list):
        parts = []
        for p in c:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text" and isinstance(p.get("text"), str):
                t = _IMAGE_ANALYSIS_RE.sub("", p["text"]).strip()
                if t:
                    parts.append(t)
            elif p.get("type") == "image_url":
                parts.append("[photo]")
        return {**m, "content": "\n\n".join(parts)}
    return m


def _message_key(m: Dict) -> tuple:
    (text,) = _user_texts([{**m, "role": "user"}])
    return (m.get("role"), text)


def _merge_window(prev: List[Dict], new: List[Dict]) -> Optional[List[Dict]]:
    """Join a rolling window onto the cached history (Telegram sends only the
    last N messages, so earlier turns slide out). Returns prev + the part of
    `new` after their longest overlap (>= 2 messages), or None if they don't
    overlap."""
    pk, nk = [_message_key(m) for m in prev], [_message_key(m) for m in new]
    for n in range(min(len(pk), len(nk)), 1, -1):
        if pk[-n:] == nk[:n]:
            return (prev + new[n:])[-_AUTO_LEARN_MAX_MESSAGES:]
    return None


def _continues_conversation(prev: List[Dict], new: List[Dict]) -> bool:
    """True if `new` is `prev` plus more turns (same user turns, in order)."""
    p, n = _user_texts(prev), _user_texts(new)
    return len(n) >= len(p) and n[: len(p)] == p


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
                messages, stream, external_tools, ha_client, user_text, routing_info, is_voice, started_at=_t0
            )
        except Exception as e:
            REQUEST_ERRORS.labels(mode="unified", error_type=type(e).__name__).inc()
            raise
        finally:
            elapsed = time.time() - _t0
            ACTIVE_REQUESTS.dec()
            # A real-streamed turn returns before the loop has run; its
            # generator counts it as active and records the request and voice
            # latency when the stream ends.
            if routing_info.get("streaming") != "real":
                REQUEST_LATENCY.labels(mode="unified").observe(elapsed)
                if is_voice:
                    VOICE_PIPELINE_LATENCY.labels(path="buffered").observe(elapsed)

    async def _chat_unified_inner(
        self,
        messages: List[Dict],
        stream: bool,
        external_tools: Optional[List],
        ha_client,
        user_text: str,
        routing_info: Dict,
        is_voice: bool = False,
        started_at: Optional[float] = None,
    ) -> Any:
        """Inner unified flow (separated for clean metrics wrapping)."""
        # First, so no path below (the health-check fallback included) can
        # hand a loop a client-supplied system/tool turn or unneutralized
        # markup. `user_text` was read from the raw list: it only drives the
        # mode router, tone request, fast path and RAG query, never model
        # input — anything that sends user text to a model takes it from
        # `messages`.
        messages = _sanitize_client_history(messages)

        # --- Vision: detect and process images before the main loop ---
        messages = await self._process_vision(messages, routing_info)

        # Route intent
        intent = self._mode_router.route(user_text)
        routing_info["intent_mode"] = intent.mode
        routing_info["intent_intensity"] = intent.intensity
        routing_info["intent_tags"] = intent.tags
        MODE_ROUTE_COUNT.labels(mode=intent.mode, intensity=intent.intensity).inc()
        record_reply_style(intent.mode)
        # After record_reply_style (which clears last turn's one-off): a tone
        # request in this message styles this reply, whatever the model does.
        apply_tone_request(user_text)
        # Tags are the matched phrases from the user's message (e.g. distress terms) —
        # mental-state data: never logged, only how many matched.
        logger.info("[MODE_ROUTER] mode=%s intensity=%s tags=%d", intent.mode, intent.intensity, len(intent.tags))

        # Fast-path: simple device commands. Never on a photo turn — the
        # question is about the photo (analyzed or not: vision off, a link).
        if ha_client and not (routing_info.get("vision_images") or routing_info.get("images_not_analyzed")):
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
                    return await self._unified_fallback(messages, system_prompt, stream, routing_info, is_voice)
                logger.error("[UNIFIED] No model available")
                REQUEST_ERRORS.labels(mode="unified", error_type="model_unavailable").inc()
                return self._brain_asleep_response(stream, routing_info)

        # Run unified tool loop
        logger.info("[UNIFIED] Processing user message (%d chars)", len(user_text))
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

        # Real streaming: relay gate-safe tokens while the loop is still
        # running. Skipped for the literal first-ever chat so the one-time
        # welcome prepend keeps its ordering, and killable at runtime via
        # REAL_STREAMING_ENABLED=false (restores chunk-the-finished-string).
        if stream and shared.REAL_STREAMING_ENABLED and not is_first_chat():
            routing_info["streaming"] = "real"
            return self._stream_loop_response(
                messages=messages.copy(),
                system_prompt=system_prompt,
                tools=tools,
                model_url=model_url,
                model_name=model_name,
                is_voice=is_voice,
                started_at=started_at,
            )

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
                return await self._unified_fallback(messages, system_prompt, stream, routing_info, is_voice)
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
        self, messages: List[Dict], system_prompt: str, stream: bool, routing_info: Dict, is_voice: bool = False
    ) -> Any:
        """Fallback to secondary model using the same unified loop.

        is_voice keeps the voice generation settings and tool budgets (the 5 s
        web_search deadline) on the fallback model too; the tool list stays
        the full one, as on the streamed fallback."""
        logger.info("[UNIFIED-FALLBACK] Using fallback model: %s", self._fallback_model_name)
        try:
            result = await self._run_unified_loop(
                messages=messages.copy(),
                system_prompt=system_prompt,
                tools=self._get_all_tools(),
                model_url=self._fallback_model_url,
                model_name=self._fallback_model_name,
                http_client=None,
                is_voice=is_voice,
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
        """Turn every image in the history into text before any loop sees it.

        Images go to the vision model (VISION_MODEL_URL — the Helios brain on the
        live deployment). Each description is neutralized and framed as untrusted
        PHOTO CONTENT, then replaces the image so the text-only loop can use it.
        If the vision model is unreachable no analysis is attempted (no
        VISION_TIMEOUT wait); the turn carries a NOT ANALYZED note instead.
        Always ends with _flatten_content, vision on or off: no image part (a
        link included) is ever forwarded to the brain.
        """
        from orchestrator.vision_handler import (
            UNREACHABLE_PLACEHOLDER,
            analyze_image,
            check_vision_health,
            extract_images_from_messages,
        )

        vision_on = shared.VISION_ENABLED
        images = extract_images_from_messages(messages) if vision_on else []
        descriptions: Dict[tuple[int, int], str] = {}

        if images:
            logger.info("[VISION] Found %d image(s) in messages, routing to vision model", len(images))
            routing_info["vision_images"] = len(images)

            # Probe first: with Helios asleep each analyze_image call would wait out
            # VISION_TIMEOUT before the brain-asleep reply (and its wake) ever ran.
            vision_up = await check_vision_health()
            if not vision_up:
                routing_info["vision_skipped"] = "unreachable"
                # Expected while Helios sleeps: INFO, and counted (no analyze_image call).
                VISION_REQUEST_COUNT.labels(status="skipped_unreachable").inc(len(images))
                logger.info("[VISION] Vision model unreachable — skipping %d image(s)", len(images))

            for img_info in images:
                prompt = img_info["text"] or "Describe this image in detail, including any text visible."
                image_url = img_info["image_url"]

                # Cache the image for follow-up analyze_image tool calls. Key from
                # the WHOLE data URI: canvas JPEGs share their first ~200 chars.
                # Re-insert so the newest image is last (the tool reads the last).
                cache_key = hashlib.sha256(image_url.encode("utf-8", "surrogatepass")).hexdigest()[:16]
                shared._vision_image_cache.pop(cache_key, None)
                shared._vision_image_cache[cache_key] = image_url
                # Cap cache at 5 entries
                while len(shared._vision_image_cache) > 5:
                    oldest = next(iter(shared._vision_image_cache))
                    shared._vision_image_cache.pop(oldest, None)

                if vision_up:
                    # thinking=False: on a busy photo the brain can spend the whole
                    # token budget reasoning and return nothing (seen on pantry scans).
                    description = await analyze_image(image_url, prompt, thinking=False)
                else:
                    description = UNREACHABLE_PLACEHOLDER
                descriptions[(img_info["msg_index"], img_info["part_index"])] = description

            logger.info("[VISION] Processed %d image(s), injected descriptions into messages", len(images))

        # Every content list becomes a string, including history the client
        # resent with old images: the loop only ever gets text.
        processed, counts = _flatten_content(messages, descriptions, vision_on)

        not_analyzed = counts["not_inline"] + counts["disabled"] + counts["over_cap"]
        if not_analyzed:
            routing_info["images_not_analyzed"] = not_analyzed
        if counts["not_inline"]:
            VISION_REQUEST_COUNT.labels(status="rejected_url").inc(counts["not_inline"])
            logger.warning(
                "[VISION] %d image part(s) were links or empty, not inline data — not fetched, not forwarded",
                counts["not_inline"],
            )
        if counts["disabled"]:
            VISION_REQUEST_COUNT.labels(status="disabled").inc(counts["disabled"])
            logger.info("[VISION] Vision disabled — %d image(s) not analyzed", counts["disabled"])
        if counts["over_cap"]:
            VISION_REQUEST_COUNT.labels(status="skipped_cap").inc(counts["over_cap"])
        if counts["other_parts"]:
            logger.info("[VISION] Dropped %d non-text, non-image content part(s)", counts["other_parts"])

        return processed

    # --- Helpers ---

    def _schedule_auto_learn(self, messages: List[Dict]):
        """Cache conversation and schedule auto-learn extraction after inactivity timeout."""
        if not shared.AUTO_LEARN_ENABLED:
            return
        try:
            cid = conversation_id_var.get()
            run_at = datetime.now() + timedelta(minutes=shared.AUTO_LEARN_DELAY_MINUTES)
            cache = shared._auto_learn_conversations
            # Never alias a list the caller may mutate later; photo text never
            # reaches long-term memory (and keys/comparisons use the same view).
            snapshot = [_strip_photo_content(m) for m in messages]

            if cid:
                # The client told us which conversation this is: one key for
                # its whole life. A rolling window (Telegram) is merged onto
                # the cached history; edits/regenerations replace it.
                session_key = hashlib.sha256(f"cid:{cid}".encode()).hexdigest()[:16]
                prev = cache.get(session_key)
                if prev is not None and not _continues_conversation(prev, snapshot):
                    snapshot = _merge_window(prev, snapshot) or snapshot
            else:
                # Fallback (HA Assist, other clients): opening message + date. A turn
                # that doesn't continue the cached conversation is treated as a
                # DIFFERENT conversation that opens the same way, and the cached
                # one is extracted separately. (An edited last turn looks the
                # same; extracting it twice beats losing a real conversation.)
                user_texts = _user_texts(snapshot)
                first_user = user_texts[0][:100] if user_texts else ""
                session_key = hashlib.sha256(f"{first_user}:{datetime.now().date()}".encode()).hexdigest()[:16]
                prev = cache.get(session_key)
                if prev is not None and not _continues_conversation(prev, snapshot):
                    flushed_key = f"{session_key}-{uuid.uuid4().hex[:6]}"
                    cache[flushed_key] = prev
                    shared.scheduler.add_job(
                        self._run_auto_learn_job,
                        trigger="date",
                        run_date=run_at,
                        args=[flushed_key],
                        id=f"auto_learn_{flushed_key}",
                        replace_existing=True,
                    )

            # In-memory only (never disk), capped at 20 conversations.
            cache.pop(session_key, None)  # re-insert = most recent
            if len(cache) >= 20:
                oldest = next(iter(cache))
                cache.pop(oldest, None)
                AUTO_LEARN_DROPPED.labels(reason="evicted").inc()
                logger.info("[AUTO_LEARN] Cache full — dropped the oldest pending conversation")
            cache[session_key] = snapshot

            # (Re)arm this conversation's single extraction job: one run, a
            # delay after the conversation goes quiet, over its full history.
            shared.scheduler.add_job(
                self._run_auto_learn_job,
                trigger="date",
                run_date=run_at,
                args=[session_key],
                id=f"auto_learn_{session_key}",
                replace_existing=True,
            )
        except Exception as e:
            logger.warning("[AUTO_LEARN] Failed to schedule extraction: %s", e)

    @staticmethod
    async def _run_auto_learn_job(session_key: str, attempt: int = 0) -> None:
        """Scheduler callback: run auto-learn and clear conversation cache.

        Each conversation gets one extraction, so if the brain is unreachable
        (Helios asleep) the snapshot is put back and retried later instead of
        being lost.
        """
        from orchestrator.auto_learn import run_auto_learn
        from orchestrator.model_manager import check_model_health

        cache = shared._auto_learn_conversations
        if session_key not in cache:
            return
        if not await check_model_health():
            if attempt + 1 >= _AUTO_LEARN_MAX_ATTEMPTS:
                cache.pop(session_key, None)
                AUTO_LEARN_DROPPED.labels(reason="model_unavailable").inc()
                logger.warning(
                    "[AUTO_LEARN] Model unreachable after %d attempts — conversation not extracted", attempt + 1
                )
                return
            try:
                shared.scheduler.add_job(
                    CloudBrain._run_auto_learn_job,
                    trigger="date",
                    run_date=datetime.now() + timedelta(minutes=_AUTO_LEARN_RETRY_MINUTES),
                    args=[session_key, attempt + 1],
                    id=f"auto_learn_{session_key}",
                    replace_existing=True,
                )
            except Exception as e:
                cache.pop(session_key, None)
                AUTO_LEARN_DROPPED.labels(reason="model_unavailable").inc()
                logger.warning("[AUTO_LEARN] Could not schedule extraction retry: %s", type(e).__name__)
                return
            AUTO_LEARN_DEFERRED.inc()
            logger.info("[AUTO_LEARN] Model unreachable — retrying extraction in %d min", _AUTO_LEARN_RETRY_MINUTES)
            return
        messages = cache.pop(session_key, None)
        if messages:
            try:
                await run_auto_learn(messages)
            finally:
                CloudBrain._rearm_if_orphaned(session_key)

    @staticmethod
    def _rearm_if_orphaned(session_key: str) -> None:
        """Re-arm a conversation that kept going while its extraction ran.

        New turns during the run re-cache the conversation and re-arm its job;
        if that job came due while this one was still running, APScheduler
        skipped it (one instance per job id) and the cached snapshot would sit
        there with no job, never extracted.
        """
        if session_key not in shared._auto_learn_conversations:
            return
        job_id = f"auto_learn_{session_key}"
        try:
            if shared.scheduler.get_job(job_id) is not None:
                return
            shared.scheduler.add_job(
                CloudBrain._run_auto_learn_job,
                trigger="date",
                run_date=datetime.now() + timedelta(minutes=shared.AUTO_LEARN_DELAY_MINUTES),
                args=[session_key],
                id=job_id,
                replace_existing=True,
            )
            AUTO_LEARN_REARMED.inc()
            logger.info("[AUTO_LEARN] Re-armed a conversation that continued during its extraction")
        except Exception as e:
            logger.warning("[AUTO_LEARN] Could not re-arm extraction: %s", type(e).__name__)

    def _stream_loop_response(
        self,
        *,
        messages: List[Dict],
        system_prompt: str,
        tools: List[Dict],
        model_url: str,
        model_name: str,
        is_voice: bool,
        started_at: Optional[float] = None,
    ):
        """Real streaming: run the unified loop in a producer task and relay
        its gate-safe deltas as OpenAI SSE chunks while it executes.

        started_at is when the request reached _chat_unified: the turn records
        bgw_request_duration_seconds (and, for voice, bgw_voice_pipeline_seconds)
        from it when the stream ends, and counts in bgw_active_requests while
        the stream runs.

        Semantics parity with the buffered path:
        - Loop failure with NOTHING emitted yet → try the fallback model
          (buffered), exactly like _chat_unified_inner's except branch.
        - Loop failure AFTER emission → the partial text stands; the stream
          closes cleanly (a retry would double-send).
        - Rounds that fell back to buffered mode emit their text via the
          loop's _finalize, so the client always receives the full answer.
        - Client disconnect cancels the producer (tools mid-flight included);
          same cancellation the buffered handler gets from starlette.
        """
        chunk_id = f"chatcmpl-{int(time.time())}"
        # Bounded so a slow/stalled SSE consumer applies real backpressure to the
        # producer instead of letting it buffer the whole answer in memory.
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        emitted_flag = {"v": False}
        # Snapshot the original messages BEFORE the primary loop mutates the list
        # in place (it appends assistant/tool/user turns). The fallback model must
        # start from the clean request, matching the buffered _unified_fallback
        # path — otherwise it inherits the primary's dangling tool_call/tool
        # messages, which a different fallback backend can choke on.
        original_messages = list(messages)

        async def _on_delta(text: str) -> None:
            emitted_flag["v"] = True
            await queue.put(("delta", text))

        async def producer():
            try:
                result = await self._run_unified_loop(
                    messages=messages,
                    system_prompt=system_prompt,
                    tools=tools,
                    model_url=model_url,
                    model_name=model_name,
                    http_client=None,
                    is_voice=is_voice,
                    on_delta=_on_delta,
                )
                # The loop mutated `messages` in place (assistant/tool/synthetic user
                # turns); learn from what the client actually sent.
                self._schedule_auto_learn(original_messages)
                await queue.put(("final", result))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[UNIFIED-STREAM] Tool loop failed: %s", e)
                REQUEST_ERRORS.labels(mode="unified", error_type="tool_loop_failed").inc()
                if not emitted_flag["v"] and self._fallback_model_url:
                    logger.info("[UNIFIED-STREAM] Nothing emitted — trying fallback model (buffered)")
                    REQUEST_COUNT.labels(mode="unified_fallback").inc()
                    CHAT_STREAM_OUTCOME.labels(outcome="loop_failed_fallback").inc()
                    try:
                        result = await self._run_unified_loop(
                            messages=original_messages,
                            system_prompt=system_prompt,
                            tools=self._get_all_tools(),
                            model_url=self._fallback_model_url,
                            model_name=self._fallback_model_name,
                            http_client=None,
                            is_voice=is_voice,
                        )
                        self._schedule_auto_learn(original_messages)
                        await queue.put(("final", result))
                        return
                    except Exception as fb_err:
                        logger.error("[UNIFIED-STREAM] Fallback also failed: %s", fb_err)
                        REQUEST_ERRORS.labels(mode="unified_fallback", error_type="fallback_failed").inc()
                        CHAT_STREAM_OUTCOME.labels(outcome="loop_failed_error").inc()
                else:
                    CHAT_STREAM_OUTCOME.labels(outcome="loop_failed_error").inc()
                await queue.put(("error", str(e)))

        def _sse(delta: Dict, finish_reason=None) -> str:
            return (
                "data: "
                + json.dumps(
                    {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model_name,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
                    }
                )
                + "\n\n"
            )

        ttft = CHAT_TTFT.labels(voice="true" if is_voice else "false")

        async def generate():
            task = asyncio.create_task(producer())
            emitted = False
            t0 = time.time()
            # _chat_unified stopped counting the turn when it returned this
            # response; count it again while the stream runs. Counted here, not
            # carried over, so a stream that never starts (client gone before
            # the first chunk) can't leave the gauge stuck high.
            ACTIVE_REQUESTS.inc()
            try:
                while True:
                    kind, payload = await queue.get()
                    if kind == "delta":
                        if not emitted:
                            emitted = True
                            ttft.observe(time.time() - t0)
                        yield _sse({"content": payload})
                    elif kind == "final":
                        # Fully-buffered turns (fallback model, non-stream-capable
                        # backend edge cases) produced no deltas — send the whole
                        # answer as one chunk so behavior matches _stream_text.
                        if not emitted and payload:
                            ttft.observe(time.time() - t0)
                            yield _sse({"content": payload})
                        yield _sse({}, finish_reason="stop")
                        yield "data: [DONE]\n\n"
                        return
                    else:  # error
                        if not emitted:
                            yield _sse({"content": "Sorry, something went wrong. Please try again."})
                        yield _sse({}, finish_reason="stop")
                        yield "data: [DONE]\n\n"
                        return
            finally:
                # Cancel AND join the producer so it is deterministically unwound
                # (a mid-flight state-mutating tool finishes cancelling) before the
                # generator returns, and any non-cancel error it raised during
                # teardown is retrieved rather than left as "exception never
                # retrieved".
                if not task.done():
                    task.cancel()
                try:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                finally:
                    # Stream over (answered, failed or client gone): the whole
                    # turn, tool rounds included.
                    elapsed = time.time() - (started_at if started_at is not None else t0)
                    ACTIVE_REQUESTS.dec()
                    REQUEST_LATENCY.labels(mode="unified").observe(elapsed)
                    if is_voice:
                        VOICE_PIPELINE_LATENCY.labels(path="streamed").observe(elapsed)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

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
        if routing_info.get("vision_skipped"):
            message += "\n\nI couldn't look at your photo while it's down — please send it again once I'm back."
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
