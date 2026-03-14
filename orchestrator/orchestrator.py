"""
Brain Gateway Orchestrator v7 - Unified Architecture
- Single model (Qwen3.5-27B on Helios) handles both conversation and tools
- Flow: User → Orchestrator → Model (conversation + tools) → User
- ChromaDB RAG for personal context
- Home Assistant integration (auto-discovery!)

This module is the "glue" — it wires together the dedicated modules, manages
startup/shutdown, and hosts the chat endpoint. All tool execution, prompts,
routes, and infrastructure logic live in their respective modules.
"""

import collections
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# Configure structured JSON logging
from log_config import configure_logging

configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

from log_buffer import log_ring

logging.getLogger().addHandler(log_ring)

# --- Dedicated module imports (the decoupled architecture) ---

# Infrastructure
# Shared state (singletons initialized at module level in shared.py)
import shared
import state_store

# REST API routes (infrastructure endpoints)
from api_routes import router as api_router

# Background scheduler jobs
from background_jobs import morning_briefing, poll_calendar
from cloud_brain import CloudBrain

# Fast-path for simple device commands (bypasses LLMs)
from fast_path import try_fast_path

# Focus session management
from focus_manager import deliver_focus_break
from google_calendar import get_calendar_client

# Model lifecycle management
from model_manager import check_model_health, start_model_server
from pihole_client import get_pihole_client

# Prompts, RAG, helpers
from prompt_builder import (
    get_unified_system_prompt,
    is_greeting,
    last_user_text,
    rag_context,
)
from shared import (
    CALENDAR_POLL_INTERVAL,
    FALLBACK_MODEL_NAME,
    FALLBACK_MODEL_URL,
    MODEL_NAME,
    MODEL_URL,
    MORNING_BRIEFING_ENABLED,
    MORNING_BRIEFING_TIME,
    current_focus_session,
    ha_client,
    scheduler,
)

# Tool definitions (schemas for unified mode)
from tool_definitions import get_all_tools

# Tool execution dispatcher
from tool_handlers import deliver_reminder_job

# Unified tool loop (v7)
from unified_loop import run_unified_tool_loop

# Load environment
load_dotenv(os.path.expanduser("~/brain_gateway/.env"))

# API authentication
API_TOKEN = os.environ.get("API_TOKEN", "")
if not API_TOKEN:
    raise RuntimeError("API_TOKEN environment variable is required — set it in .env")


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Require Bearer token on all endpoints except public ones."""

    PUBLIC_PATHS = {"/health", "/metrics"}
    PUBLIC_PREFIXES = ("/api/audio/",)  # Google speakers fetch audio without auth

    async def dispatch(self, request: Request, call_next):
        # CORS preflight must pass through
        if request.method == "OPTIONS":
            return await call_next(request)
        # Public endpoints don't require auth
        if request.url.path in self.PUBLIC_PATHS:
            return await call_next(request)
        # Prefix-based public paths (audio served to speakers)
        if any(request.url.path.startswith(p) for p in self.PUBLIC_PREFIXES):
            return await call_next(request)
        # Check Bearer token
        auth = request.headers.get("authorization", "")
        if auth == f"Bearer {API_TOKEN}":
            return await call_next(request)
        return JSONResponse({"error": "Unauthorized"}, status_code=401)


# Maximum request body size (bytes) — rejects oversized payloads before LLM processing
MAX_BODY_SIZE = int(os.environ.get("MAX_BODY_SIZE", 1_000_000))  # 1MB default (HA sends all entity states)


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject request bodies larger than MAX_BODY_SIZE."""

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_BODY_SIZE:
                    return JSONResponse(
                        {"error": f"Request body too large (max {MAX_BODY_SIZE} bytes)"},
                        status_code=413,
                    )
            except ValueError:
                return JSONResponse({"error": "Invalid Content-Length"}, status_code=400)
        return await call_next(request)


# Simple sliding-window rate limiter (per-IP, in-memory)
RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", 60))  # seconds
RATE_LIMIT_MAX = int(os.environ.get("RATE_LIMIT_MAX", 30))  # requests per window
_rate_limit_store: Dict[str, collections.deque] = {}


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP sliding window rate limiter."""

    EXEMPT_PATHS = {"/health", "/metrics"}

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self.EXEMPT_PATHS:
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = time.time()

        if client_ip not in _rate_limit_store:
            _rate_limit_store[client_ip] = collections.deque()

        window = _rate_limit_store[client_ip]
        # Purge entries older than the window
        while window and window[0] < now - RATE_LIMIT_WINDOW:
            window.popleft()

        if len(window) >= RATE_LIMIT_MAX:
            return JSONResponse(
                {"error": "Rate limit exceeded. Try again later."},
                status_code=429,
            )

        window.append(now)
        return await call_next(request)


# Cloud brain (initialized in startup_event)
cloud_brain: Optional[CloudBrain] = None

# Shared httpx client for connection reuse
_http: httpx.AsyncClient = None


# =============================================================================
# FASTAPI APP
# =============================================================================

app = FastAPI(title="Brain Gateway", version="7.0")

_cors_origins = os.getenv("CORS_ORIGINS", "http://localhost:3001").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors_origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(BearerAuthMiddleware)
app.add_middleware(RequestSizeLimitMiddleware)
app.add_middleware(RateLimitMiddleware)

# Mount the infrastructure API routes (health, metrics, HA, memory, reminders, focus, etc.)
app.include_router(api_router)


# =============================================================================
# LLM BACKEND RESOLUTION (unique to orchestrator.py)
# =============================================================================


async def call_model(
    url: str,
    model: str,
    messages: List[Dict],
    system: str = "",
    tools: List = None,
    tool_choice: str = "auto",
    timeout: int = 180,
) -> Dict[str, Any]:
    """Call an LLM endpoint via the appropriate backend.

    Backend selection is automatic based on which configured URL matches.
    Falls back to OpenAI-compatible for unknown URLs.
    """
    backend = _resolve_backend(url, model)
    return await backend.chat_completion(
        messages,
        system=system,
        tools=tools,
        tool_choice=tool_choice,
        timeout=timeout,
    )


async def stream_final_response(url: str, model: str, messages: List[Dict], system: str = "", timeout: int = 180):
    """Stream the final response via the appropriate backend."""
    backend = _resolve_backend(url, model)
    async for chunk in backend.stream_chat_completion(
        messages,
        system=system,
        timeout=timeout,
    ):
        yield chunk


def _resolve_backend(url: str, model: str):
    """Pick the backend whose URL matches the call."""
    from llm_backend import LLMConfig, OpenAICompatibleBackend

    if shared.primary_backend and url == shared.primary_backend.config.url:
        return shared.primary_backend
    if shared.fallback_backend and url == shared.fallback_backend.config.url:
        return shared.fallback_backend

    # Fallback: create a one-off OpenAI-compatible backend
    logger.warning(f"[LLM] No configured backend for {url}, using OpenAI-compatible fallback")
    fallback_config = LLMConfig(backend="openai_compatible", url=url, model=model)
    return OpenAICompatibleBackend(fallback_config, _http)


# =============================================================================
# CHAT ENDPOINT (the brain — delegates to CloudBrain)
# =============================================================================


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    """
    Main chat endpoint - Unified Architecture v7.

    Delegates to CloudBrain for the full chat flow.
    """
    from schemas import ChatRequest

    body = await req.json()
    # Debug: log incoming message roles/lengths to diagnose HA integration issues
    raw_msgs = body.get("messages", [])
    logger.info(
        "[CHAT] Incoming %d messages: %s",
        len(raw_msgs),
        [(m.get("role"), len(str(m.get("content", "")))) for m in raw_msgs[:10]],
    )
    # Debug: log structure of HA's single system message
    if len(raw_msgs) == 1 and raw_msgs[0].get("role") == "system":
        content = str(raw_msgs[0].get("content", ""))
        logger.info("[CHAT] System msg head: %s", content[:500])
        logger.info("[CHAT] System msg tail: ...%s", content[-500:])
    # Debug: log all body keys (maybe user query is outside messages)
    logger.info("[CHAT] Body keys: %s", list(body.keys()))
    chat_req = ChatRequest(**body)

    return await cloud_brain.chat(
        [m.model_dump() for m in chat_req.messages],
        stream=chat_req.stream,
        external_tools=chat_req.tools,
        ha_client=ha_client,
    )


# =============================================================================
# STARTUP / SHUTDOWN
# =============================================================================


@app.on_event("shutdown")
async def shutdown_event():
    """Clean up resources on shutdown."""
    global _http
    if _http:
        await _http.aclose()
        _http = None
        logger.info("[orchestrator] Closed shared HTTP client")


@app.on_event("startup")
async def startup_event():
    """Initialize services on startup."""
    global _http, cloud_brain
    _http = httpx.AsyncClient(
        timeout=httpx.Timeout(60, connect=10),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    logger.info("[orchestrator] Initialized shared HTTP client")

    # Initialize persistent state store (SQLite)
    state_store.init_db()
    state_store.clear_stale_notifications(older_than_hours=48)

    # Initialize LLM backends
    shared._http = _http  # ensure shared module has the http client too
    shared.init_backends(_http)

    # Load HA entities at startup
    logger.info("[orchestrator] Loading Home Assistant entities...")
    count = await ha_client.refresh_entities()
    shared._ha_tool_cache = None  # Invalidate cache after entity refresh
    shared._ha_tool_cache_time = 0.0
    logger.info(f"[orchestrator] Loaded {count} HA entities")

    # Initialize Google Calendar client
    cal_client = get_calendar_client(http_client=_http)
    if cal_client.is_configured:
        logger.info("[orchestrator] Google Calendar configured — tools enabled")
    else:
        logger.info("[orchestrator] Google Calendar not configured — tools disabled (run google_setup.py)")

    # Initialize CloudBrain (v7 unified)
    cloud_brain = CloudBrain(
        call_model_fn=call_model,
        try_fast_path_fn=try_fast_path,
        is_greeting_fn=is_greeting,
        last_user_text_fn=last_user_text,
        rag_search_fn=rag_context,
        get_unified_system_prompt_fn=get_unified_system_prompt,
        get_all_tools_fn=get_all_tools,
        check_model_health_fn=check_model_health,
        start_model_server_fn=start_model_server,
        run_unified_loop_fn=run_unified_tool_loop,
        model_url=MODEL_URL,
        model_name=MODEL_NAME,
        fallback_model_url=FALLBACK_MODEL_URL,
        fallback_model_name=FALLBACK_MODEL_NAME,
    )
    logger.info("[orchestrator] CloudBrain initialized (mode=unified_v7)")

    # Reload pending reminders from DB and re-schedule
    pending = state_store.get_pending_reminders()
    reloaded = 0
    for rem in pending:
        try:
            trigger = datetime.fromisoformat(rem["trigger_time"])
            if trigger > datetime.now():
                scheduler.add_job(
                    deliver_reminder_job,
                    trigger="date",
                    run_date=trigger,
                    args=[rem["id"]],
                    id=f"reminder_{rem['id']}",
                    replace_existing=True,
                )
                reloaded += 1
        except Exception as e:
            logger.warning(f"[STATE] Failed to reload reminder {rem.get('id')}: {e}")
    if reloaded:
        logger.info(f"[STATE] Reloaded {reloaded} pending reminders from DB")

    # Restore focus session from DB (survives orchestrator restarts)
    saved_focus = state_store.load_focus_session()
    if saved_focus["active"]:
        end_time = saved_focus["started"] + timedelta(minutes=saved_focus["duration"])
        if end_time <= datetime.now():
            # Session expired while we were down — clean up blocking
            logger.info("[FOCUS] Found expired focus session '%s' — cleaning up", saved_focus["task"])
            if saved_focus.get("block_sites"):
                pihole = get_pihole_client()
                result = await pihole.disable_focus_blocking()
                if result.success:
                    logger.info("[FOCUS] Disabled leftover Pi-hole blocking from expired session")
                else:
                    logger.warning("[FOCUS] Could not disable leftover blocking: %s", result.message)
            state_store.clear_focus_session()
            logger.info("[FOCUS] Cleared expired focus session from DB")
        else:
            # Session still active — restore in-memory state and re-schedule break
            current_focus_session.update(saved_focus)
            job_id = f"focus_restored_{datetime.now().strftime('%H%M%S')}"
            current_focus_session["job_id"] = job_id
            remaining = (end_time - datetime.now()).total_seconds() / 60
            scheduler.add_job(
                deliver_focus_break,
                trigger="date",
                run_date=end_time,
                args=[saved_focus["task"], saved_focus["break_duration"]],
                id=job_id,
                replace_existing=True,
            )
            logger.info(
                "[FOCUS] Restored active focus session '%s' (%.0f min remaining, break at %s)",
                saved_focus["task"],
                remaining,
                end_time.strftime("%H:%M"),
            )

    # Defensive: always ensure Pi-hole blocking is off if no active focus session
    if not current_focus_session["active"]:
        pihole = get_pihole_client()
        result = await pihole.disable_focus_blocking()
        if result.success:
            logger.info("[FOCUS] Defensive startup: ensured Pi-hole blocking is disabled")
        else:
            logger.warning("[FOCUS] Defensive startup: could not disable blocking: %s", result.message)

    scheduler.start()
    logger.info(f"[SCHEDULER] Started ({reloaded} reminders reloaded from DB)")

    # Schedule proactive calendar polling (if calendar is configured)
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8888)
