"""
Brain Gateway Orchestrator v6 - Hybrid Architecture
- Helios (120B) is the primary conversational assistant (Jessica)
- Nemotron (8B) is the tool orchestrator (HA, RAG, reminders, update_data)
- Flow: User → Helios → (ask_orchestrator) → Nemotron → tools → result → Helios → User
- ChromaDB RAG for personal context
- Home Assistant integration (auto-discovery!)
"""

import os
import re
import json
import logging
import time
from typing import Any, Dict, List
from datetime import datetime

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

# Configure structured JSON logging
from log_config import configure_logging, set_request_id
configure_logging()
logger = logging.getLogger(__name__)

# Prometheus metrics
from metrics import (
    REQUEST_COUNT, REQUEST_LATENCY, REQUEST_ERRORS, ACTIVE_REQUESTS,
    LLM_CALL_COUNT, LLM_CALL_LATENCY, LLM_CALL_ERRORS,
    MODE_ROUTE_COUNT, FAST_PATH_COUNT, FAST_PATH_BYPASS, BUILD_INFO,
)

# Shared state
import shared
from shared import (
    NEMOTRON_URL, NEMOTRON_MODEL, HELIOS_URL, HELIOS_MODEL,
    ha_client, scheduler, current_focus_session, collection,
    CHROMA_COLLECTION,
    CALENDAR_POLL_INTERVAL, MORNING_BRIEFING_TIME, MORNING_BRIEFING_ENABLED,
    EMAIL_POLL_INTERVAL, EMAIL_POLL_ENABLED,
    EMAIL_TO_CALENDAR_ENABLED, EMAIL_TO_CALENDAR_INTERVAL,
)

# Module imports
from fast_path import try_fast_path
from mode_router import get_mode_router
from google_calendar import get_calendar_client
from google_gmail import get_gmail_client
from prompt_builder import (
    is_greeting, last_user_text, rag_context,
    get_helios_system_prompt, get_orchestrator_system_prompt,
)
from tool_definitions import HELIOS_TOOLS
from helios_manager import check_helios_health, start_helios
from nemotron_loop import (
    call_nemotron_orchestrator, _run_nemotron_tool_loop,
    clean_response, parse_tool_calls_from_content,
)
from background_jobs import (
    poll_calendar, morning_briefing, poll_email, process_emails_for_events,
    sync_ynab_transactions, weekly_spending_summary, midmonth_budget_warning,
    check_closet_temperature,
)
from api_routes import router as api_router
from finance_manager import router as finance_router, setup_finance, _is_ynab_configured, YNAB_SYNC_INTERVAL

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Brain Gateway", version="5.0")

_cors_origins = os.getenv("CORS_ORIGINS", "http://localhost:3001").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors_origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount secondary endpoints
app.include_router(api_router)
app.include_router(finance_router)


# ---------------------------------------------------------------------------
# LLM call helpers
# ---------------------------------------------------------------------------

async def call_model(url: str, model: str, messages: List[Dict], system: str = "",
                     tools: List = None, tool_choice: str = "auto", timeout: int = 180) -> Dict[str, Any]:
    """Call an LLM endpoint.

    Args:
        tool_choice: "auto" for native tool calling (Helios), "none" for XML-style (Nemotron)
    """
    final_messages = messages.copy()
    if system:
        final_messages.insert(0, {"role": "system", "content": system})

    payload = {
        "model": model,
        "messages": final_messages,
        "temperature": 0.3,
        "max_tokens": 4096,
    }

    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice

    _model_label = "helios" if "195" in url else "nemotron"
    _llm_t0 = time.time()
    try:
        r = await shared._http.post(f"{url}/chat/completions", json=payload, timeout=timeout)
        r.raise_for_status()
        _elapsed = time.time() - _llm_t0
        LLM_CALL_COUNT.labels(model=_model_label, purpose="call").inc()
        LLM_CALL_LATENCY.labels(model=_model_label, purpose="call").observe(_elapsed)
        logger.info(f"[LLM] {_model_label} responded in {_elapsed:.1f}s",
                    extra={"component": "llm", "model": _model_label,
                           "latency_ms": int(_elapsed * 1000)})
        data = r.json()
        # Clean up Qwen3 thinking from response
        for choice in data.get("choices", []):
            msg = choice.get("message", {})
            msg.pop("reasoning_content", None)
            content = msg.get("content")
            if content and "<think>" in content:
                content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL)
                msg["content"] = content.strip()
        return data
    except httpx.TimeoutException:
        LLM_CALL_ERRORS.labels(model=_model_label, error_type="timeout").inc()
        raise
    except httpx.HTTPStatusError:
        LLM_CALL_ERRORS.labels(model=_model_label, error_type="http_error").inc()
        raise
    except Exception:
        LLM_CALL_ERRORS.labels(model=_model_label, error_type="connection_error").inc()
        raise


async def stream_final_response(url: str, model: str, messages: List[Dict], system: str = "", timeout: int = 180):
    """Stream the final response from Nemotron (after tool calls are done)."""
    final_messages = messages.copy()
    if system:
        final_messages.insert(0, {"role": "system", "content": system})

    payload = {
        "model": model,
        "messages": final_messages,
        "temperature": 0.3,
        "max_tokens": 4096,
        "stream": True,
    }

    async with shared._http.stream("POST", f"{url}/chat/completions", json=payload, timeout=timeout) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line:
                continue
            if line.startswith("data: "):
                yield f"{line}\n\n"


def _stream_text_response(text: str, model: str):
    """Helper to stream a text response in SSE format."""
    chunk_id = f"chatcmpl-{int(time.time())}"

    async def generate():
        chunk_size = 80
        for i in range(0, len(text), chunk_size):
            chunk_text = text[i:i+chunk_size]
            chunk_data = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {"content": chunk_text},
                    "finish_reason": None
                }]
            }
            yield f"data: {json.dumps(chunk_data)}\n\n"
        final_chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
        }
        yield f"data: {json.dumps(final_chunk)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


async def _nemotron_fallback(messages: List[Dict], stream: bool, routing_info: Dict,
                             mode: str = "explainer", intensity: str = "low"):
    """Fallback to Nemotron-only mode when Helios is unavailable."""
    logger.info("[FALLBACK] Using Nemotron-only mode")

    conversation = messages.copy()
    system_prompt = get_orchestrator_system_prompt(mode=mode, intensity=intensity)
    result = await _run_nemotron_tool_loop(conversation, system_prompt, label="FALLBACK")

    if stream:
        return _stream_text_response(result, NEMOTRON_MODEL)
    return JSONResponse({
        "choices": [{"message": {"role": "assistant", "content": result}}],
        "_routing": routing_info,
    })


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_event():
    """Initialize services on startup."""
    shared._http = httpx.AsyncClient(
        timeout=httpx.Timeout(60, connect=10),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    logger.info("[orchestrator] Initialized shared HTTP client")

    # Load HA entities at startup
    print("[orchestrator] Loading Home Assistant entities...")
    count = await ha_client.refresh_entities()
    shared._ha_tool_cache = None
    shared._ha_tool_cache_time = 0.0
    print(f"[orchestrator] Loaded {count} HA entities")

    # Load phone calendar events from disk (survives restarts)
    shared._load_phone_calendar()

    # Initialize Google Calendar client
    cal_client = get_calendar_client(http_client=shared._http)
    if cal_client.is_configured:
        logger.info("[orchestrator] Google Calendar configured — tools enabled")
    else:
        logger.info("[orchestrator] Google Calendar not configured — tools disabled (run google_setup.py)")

    # Initialize Gmail client
    gmail_client = get_gmail_client(http_client=shared._http)
    if gmail_client.is_configured:
        logger.info("[orchestrator] Gmail configured — tools enabled")
    else:
        logger.info("[orchestrator] Gmail not configured — tools disabled (run google_setup.py)")

    # Initialize finance database
    setup_finance()
    logger.info("[orchestrator] Finance database initialized")

    BUILD_INFO.info({"version": "6.2", "architecture": "hybrid"})
    scheduler.start()
    logger.info("[SCHEDULER] Started (in-memory, no reminders to reload)")

    # Schedule proactive calendar polling
    if cal_client.is_configured:
        scheduler.add_job(
            poll_calendar,
            trigger="interval",
            minutes=CALENDAR_POLL_INTERVAL,
            id="calendar_poll",
            name="Calendar event polling",
            replace_existing=True,
        )
        logger.info(f"[SCHEDULER] Calendar polling every {CALENDAR_POLL_INTERVAL} min")

        if MORNING_BRIEFING_ENABLED:
            hour, minute = map(int, MORNING_BRIEFING_TIME.split(":"))
            scheduler.add_job(
                morning_briefing,
                trigger="cron",
                hour=hour,
                minute=minute,
                id="morning_briefing",
                name="Morning briefing",
                replace_existing=True,
            )
            logger.info(f"[SCHEDULER] Morning briefing at {MORNING_BRIEFING_TIME}")

    # Schedule email polling
    if gmail_client.is_configured and EMAIL_POLL_ENABLED:
        scheduler.add_job(
            poll_email,
            trigger="interval",
            minutes=EMAIL_POLL_INTERVAL,
            id="email_poll",
            name="Email polling",
            replace_existing=True,
        )
        logger.info(f"[SCHEDULER] Email polling every {EMAIL_POLL_INTERVAL} min")

    # Schedule email-to-calendar extraction
    if gmail_client.is_configured and cal_client.is_configured and EMAIL_TO_CALENDAR_ENABLED:
        scheduler.add_job(
            process_emails_for_events,
            trigger="interval",
            minutes=EMAIL_TO_CALENDAR_INTERVAL,
            id="email_to_calendar",
            name="Email-to-calendar extraction",
            replace_existing=True,
        )
        logger.info(f"[SCHEDULER] Email-to-calendar every {EMAIL_TO_CALENDAR_INTERVAL} min")

    # Schedule YNAB transaction sync
    if _is_ynab_configured():
        scheduler.add_job(
            sync_ynab_transactions,
            trigger="interval",
            minutes=YNAB_SYNC_INTERVAL,
            id="ynab_sync",
            name="YNAB transaction sync",
            replace_existing=True,
        )
        logger.info(f"[SCHEDULER] YNAB sync every {YNAB_SYNC_INTERVAL} min")

    # Schedule weekly spending summary (Sunday 6 PM)
    scheduler.add_job(
        weekly_spending_summary,
        trigger="cron",
        day_of_week="sun",
        hour=18,
        minute=0,
        id="weekly_spending_summary",
        name="Weekly spending summary TTS",
        replace_existing=True,
    )
    logger.info("[SCHEDULER] Weekly spending summary: Sunday 6 PM")

    # Schedule mid-month budget warning (daily at noon, only fires day 13-17)
    scheduler.add_job(
        midmonth_budget_warning,
        trigger="cron",
        hour=12,
        minute=0,
        id="midmonth_budget_warning",
        name="Mid-month budget warning TTS",
        replace_existing=True,
    )
    logger.info("[SCHEDULER] Mid-month budget warning: daily noon (active day 13-17)")

    # Schedule closet temperature monitoring
    scheduler.add_job(
        check_closet_temperature,
        trigger="interval",
        minutes=10,
        id="closet_temp_check",
        name="Closet temperature monitoring",
        replace_existing=True,
    )
    logger.info("[SCHEDULER] Closet temperature monitoring every 10 min")


@app.on_event("shutdown")
async def shutdown_event():
    """Clean up resources on shutdown."""
    if shared._http:
        await shared._http.aclose()
        shared._http = None
        logger.info("[orchestrator] Closed shared HTTP client")


# ---------------------------------------------------------------------------
# Main chat endpoint
# ---------------------------------------------------------------------------

@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    """
    Main chat endpoint - Hybrid Architecture v6.

    Flow: User → Helios (conversation) → ask_orchestrator → Nemotron (tools) → Helios → User
    """
    ACTIVE_REQUESTS.inc()
    _req_t0 = time.time()
    _req_mode = "hybrid"
    rid = set_request_id()

    body = await req.json()
    messages = body.get("messages", [])
    messages = [m for m in messages if m.get("role") != "system"]
    external_tools = body.get("tools")
    stream = body.get("stream", False)
    user_text = last_user_text(messages)

    routing_info = {
        "timestamp": datetime.now().isoformat(),
        "request_id": rid,
        "user_query_length": len(user_text),
        "architecture": "hybrid_v6",
        "tool_calls": [],
        "streaming": stream,
    }

    # Route user intent
    intent = get_mode_router().route(user_text)
    routing_info["intent_mode"] = intent.mode
    routing_info["intent_intensity"] = intent.intensity
    routing_info["intent_tags"] = intent.tags
    MODE_ROUTE_COUNT.labels(mode=intent.mode, intensity=intent.intensity).inc()
    logger.info(f"[MODE_ROUTER] mode={intent.mode} intensity={intent.intensity} tags={intent.tags}",
                extra={"component": "mode_router", "mode": intent.mode, "intensity": intent.intensity})

    # External tools passthrough (e.g., from HA voice pipeline)
    if external_tools:
        logger.info(f"[HYBRID] External tools provided ({len(external_tools)}), passing to Nemotron")
        _req_mode = "passthrough"
        routing_info["mode"] = "passthrough"
        try:
            llm_resp = await call_model(
                NEMOTRON_URL, NEMOTRON_MODEL, messages,
                system=get_orchestrator_system_prompt(mode=intent.mode, intensity=intent.intensity),
                tools=external_tools,
                timeout=60,
            )
            REQUEST_COUNT.labels(mode="passthrough").inc()
            REQUEST_LATENCY.labels(mode="passthrough").observe(time.time() - _req_t0)
            ACTIVE_REQUESTS.dec()
            llm_resp["_routing"] = routing_info
            return JSONResponse(llm_resp)
        except Exception as e:
            REQUEST_ERRORS.labels(mode="passthrough", error_type=type(e).__name__).inc()
            ACTIVE_REQUESTS.dec()
            logger.error(f"[HYBRID] Passthrough failed: {e}")
            return JSONResponse({"error": str(e)}, status_code=503)

    # === HYBRID MODE ===
    routing_info["mode"] = "hybrid"
    logger.info(f"[HYBRID] Processing: {user_text[:100]}... (stream={stream})")

    # Fast-path: intercept simple device commands before any LLM call
    try:
        fast_result = await try_fast_path(user_text, ha_client)
        if fast_result.handled:
            _req_mode = "fast_path"
            routing_info["mode"] = "fast_path"
            routing_info["fast_path_action"] = fast_result.action
            routing_info["fast_path_entity"] = fast_result.entity_name
            FAST_PATH_COUNT.labels(action=fast_result.action or "unknown").inc()
            REQUEST_COUNT.labels(mode="fast_path").inc()
            REQUEST_LATENCY.labels(mode="fast_path").observe(time.time() - _req_t0)
            ACTIVE_REQUESTS.dec()
            logger.info(f"[FAST-PATH] Handled: {fast_result.action} -> {fast_result.entity_name}",
                        extra={"component": "fast_path"})
            if stream:
                return _stream_text_response(fast_result.response_text, "fast-path")
            return JSONResponse({
                "id": f"chatcmpl-fp-{int(time.time())}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "fast-path",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": fast_result.response_text},
                    "finish_reason": "stop",
                }],
                "_routing": routing_info,
            })
    except Exception as e:
        FAST_PATH_BYPASS.inc()
        logger.warning(f"[FAST-PATH] Error, falling through to Helios: {e}")

    # 1. Pre-fetch RAG context (skip for greetings)
    personal_context = ""
    if not is_greeting(user_text):
        personal_context = rag_context(user_text)
        if personal_context:
            logger.info(f"[HYBRID] Pre-fetched RAG context ({len(personal_context)} chars)")
            routing_info["rag_prefetch"] = True

    # 2. Build Helios system prompt
    helios_system = get_helios_system_prompt(personal_context, mode=intent.mode, intensity=intent.intensity)

    # 3. Check if Helios is available, start if needed
    if not await check_helios_health():
        logger.info("[HYBRID] Helios offline, attempting to start...")
        started = await start_helios()
        if not started:
            _req_mode = "fallback"
            logger.warning("[HYBRID] Helios unavailable, falling back to Nemotron")
            routing_info["fallback"] = "nemotron"
            result = await _nemotron_fallback(messages, stream, routing_info,
                                              mode=intent.mode, intensity=intent.intensity)
            REQUEST_COUNT.labels(mode="fallback").inc()
            REQUEST_LATENCY.labels(mode="fallback").observe(time.time() - _req_t0)
            ACTIVE_REQUESTS.dec()
            return result

    # 4. Call Helios
    logger.info("[HYBRID] Calling Helios...")
    try:
        helios_resp = await call_model(
            HELIOS_URL, HELIOS_MODEL, messages,
            system=helios_system,
            tools=HELIOS_TOOLS,
            timeout=180,
        )
        shared._last_helios_request = time.time()
    except Exception as e:
        logger.error(f"[HYBRID] Helios call failed: {e}",
                     extra={"component": "hybrid", "error_type": type(e).__name__})
        _req_mode = "fallback"
        routing_info["fallback"] = "nemotron"
        routing_info["helios_error"] = str(e)
        result = await _nemotron_fallback(messages, stream, routing_info,
                                          mode=intent.mode, intensity=intent.intensity)
        REQUEST_COUNT.labels(mode="fallback").inc()
        REQUEST_LATENCY.labels(mode="fallback").observe(time.time() - _req_t0)
        ACTIVE_REQUESTS.dec()
        return result

    # 5. Check for tool calls (ask_orchestrator)
    choice = helios_resp.get("choices", [{}])[0]
    message = choice.get("message", {})
    tool_calls = message.get("tool_calls", [])
    content = message.get("content") or ""

    if not tool_calls and content:
        tool_calls = parse_tool_calls_from_content(content)

    # 6. If no tool calls, return Helios response directly
    if not tool_calls:
        logger.info("[HYBRID] Helios responded directly (no orchestrator needed)",
                    extra={"component": "hybrid"})
        routing_info["helios_direct"] = True
        REQUEST_COUNT.labels(mode="hybrid").inc()
        REQUEST_LATENCY.labels(mode="hybrid").observe(time.time() - _req_t0)
        ACTIVE_REQUESTS.dec()

        if stream:
            return _stream_text_response(clean_response(content), HELIOS_MODEL)
        else:
            if content:
                message["content"] = clean_response(content)
            helios_resp["_routing"] = routing_info
            return JSONResponse(helios_resp)

    # 7. Execute ask_orchestrator via Nemotron
    logger.info("[HYBRID] Helios called orchestrator, delegating to Nemotron")
    conversation = messages.copy()

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

        orchestrator_result = await call_nemotron_orchestrator(command)
        logger.info(f"[HYBRID] Orchestrator result: {orchestrator_result[:200]}...")

        conversation.append({
            "role": "assistant",
            "content": f"I used the orchestrator to: {command}"
        })
        conversation.append({
            "role": "user",
            "content": f"Orchestrator result: {orchestrator_result}\n\nPlease respond naturally to me based on this result. Keep it brief and conversational."
        })

    # 8. Get final response from Helios
    logger.info("[HYBRID] Getting final response from Helios...")
    try:
        final_resp = await call_model(
            HELIOS_URL, HELIOS_MODEL, conversation,
            system=helios_system,
            timeout=120,
        )
        shared._last_helios_request = time.time()
    except Exception as e:
        logger.error(f"[HYBRID] Helios final response failed: {e}",
                     extra={"component": "hybrid", "error_type": type(e).__name__})
        REQUEST_COUNT.labels(mode="hybrid").inc()
        REQUEST_LATENCY.labels(mode="hybrid").observe(time.time() - _req_t0)
        ACTIVE_REQUESTS.dec()
        if stream:
            return _stream_text_response(orchestrator_result, NEMOTRON_MODEL)
        return JSONResponse({
            "choices": [{"message": {"role": "assistant", "content": orchestrator_result}}],
            "_routing": routing_info,
        })

    final_content = final_resp.get("choices", [{}])[0].get("message", {}).get("content", "")
    final_content = clean_response(final_content)

    REQUEST_COUNT.labels(mode="hybrid").inc()
    REQUEST_LATENCY.labels(mode="hybrid").observe(time.time() - _req_t0)
    ACTIVE_REQUESTS.dec()

    if stream:
        return _stream_text_response(final_content, HELIOS_MODEL)

    final_resp["_routing"] = routing_info
    if final_content:
        final_resp["choices"][0]["message"]["content"] = final_content
    return JSONResponse(final_resp)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8888)
