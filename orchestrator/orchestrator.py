"""
Brain Gateway Orchestrator v6 - Hybrid Architecture
- Helios (Qwen3-32B) is the primary conversational assistant (Jessica)
- Nemotron (8B) is the tool orchestrator (HA, RAG, reminders, update_data)
- Flow: User → Helios → (ask_orchestrator) → Nemotron → tools → result → Helios → User
- ChromaDB RAG for personal context
- Home Assistant integration (auto-discovery!)

This module is the "glue" — it wires together the dedicated modules, manages
startup/shutdown, and hosts the chat endpoint. All tool execution, prompts,
routes, and infrastructure logic live in their respective modules.
"""

import os
import logging
import time
import collections
from typing import Any, Dict, List, Optional
from datetime import datetime

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

# Configure logging with ring buffer for self-diagnosis
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from log_buffer import log_ring
logging.getLogger().addHandler(log_ring)

# --- Dedicated module imports (the decoupled architecture) ---

# Infrastructure
from google_calendar import get_calendar_client
import state_store

# Prompts, RAG, helpers
from prompt_builder import (
    rag_context, get_helios_system_prompt,
    get_orchestrator_system_prompt, get_nemotron_system_prompt,
    is_greeting, last_user_text,
)

# Tool definitions (schemas for Nemotron and Helios)
from tool_definitions import HELIOS_TOOLS, get_orchestrator_tools

# Tool execution dispatcher
from tool_handlers import execute_tool, deliver_reminder_job

# Nemotron agentic loop
from nemotron_loop import _run_nemotron_tool_loop, clean_response, parse_tool_calls_from_content

# Helios lifecycle management
from helios_manager import check_helios_health, start_helios, stop_helios, check_helios_idle

# Fast-path for simple device commands (bypasses LLMs)
from fast_path import try_fast_path

# Background scheduler jobs
from background_jobs import poll_calendar, morning_briefing

# REST API routes (infrastructure endpoints)
from api_routes import router as api_router

# Cloud brain + local agent
from local_agent import LocalAgent
from cloud_brain import CloudBrain

# Shared state (singletons initialized at module level in shared.py)
import shared
from shared import (
    ha_client, scheduler, collection,
    NEMOTRON_URL, NEMOTRON_MODEL, HELIOS_URL, HELIOS_MODEL,
    CHROMA_COLLECTION, CHROMA_PERSIST,
    CALENDAR_POLL_INTERVAL, MORNING_BRIEFING_TIME, MORNING_BRIEFING_ENABLED,
    profile,
)

# Load environment
load_dotenv(os.path.expanduser("~/brain_gateway/.env"))

# API authentication
API_TOKEN = os.environ.get("API_TOKEN", "")
if not API_TOKEN:
    raise RuntimeError("API_TOKEN environment variable is required — set it in .env")


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Require Bearer token on all endpoints except public ones."""

    PUBLIC_PATHS = {"/health", "/metrics"}

    async def dispatch(self, request: Request, call_next):
        # CORS preflight must pass through
        if request.method == "OPTIONS":
            return await call_next(request)
        # Public endpoints don't require auth
        if request.url.path in self.PUBLIC_PATHS:
            return await call_next(request)
        # Check Bearer token
        auth = request.headers.get("authorization", "")
        if auth == f"Bearer {API_TOKEN}":
            return await call_next(request)
        return JSONResponse({"error": "Unauthorized"}, status_code=401)


# Maximum request body size (bytes) — rejects oversized payloads before LLM processing
MAX_BODY_SIZE = int(os.environ.get("MAX_BODY_SIZE", 50_000))  # 50KB default


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject request bodies larger than MAX_BODY_SIZE."""

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_BODY_SIZE:
            return JSONResponse(
                {"error": f"Request body too large (max {MAX_BODY_SIZE} bytes)"},
                status_code=413,
            )
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


# LLM backends (initialized in startup_event after _http is ready)
conversation_backend = None
orchestrator_backend = None

# Cloud brain (initialized in startup_event)
cloud_brain: Optional[CloudBrain] = None

# Shared httpx client for connection reuse
_http: httpx.AsyncClient = None


# =============================================================================
# FASTAPI APP
# =============================================================================

app = FastAPI(title="Brain Gateway", version="6.0")

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

async def call_model(url: str, model: str, messages: List[Dict], system: str = "",
                     tools: List = None, tool_choice: str = "auto",
                     timeout: int = 180) -> Dict[str, Any]:
    """Call an LLM endpoint via the appropriate backend.

    Signature unchanged from v6. Backend selection is automatic based on
    which role's URL matches. Falls back to OpenAI-compatible for unknown URLs.

    Args:
        tool_choice: "auto" for native tool calling (Helios), "none" for XML-style (Nemotron)
    """
    backend = _resolve_backend(url, model)
    return await backend.chat_completion(
        messages, system=system, tools=tools,
        tool_choice=tool_choice, timeout=timeout,
    )


async def stream_final_response(url: str, model: str, messages: List[Dict],
                                system: str = "", timeout: int = 180):
    """Stream the final response via the appropriate backend."""
    backend = _resolve_backend(url, model)
    async for chunk in backend.stream_chat_completion(
        messages, system=system, timeout=timeout,
    ):
        yield chunk


def _resolve_backend(url: str, model: str):
    """Pick the backend whose URL matches the call."""
    from llm_backend import LLMConfig, OpenAICompatibleBackend

    if conversation_backend and url == conversation_backend.config.url:
        return conversation_backend
    if orchestrator_backend and url == orchestrator_backend.config.url:
        return orchestrator_backend

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
    Main chat endpoint - Hybrid Architecture v6.

    Delegates to CloudBrain for the full chat flow.
    """
    body = await req.json()
    messages = body.get("messages", [])
    external_tools = body.get("tools")  # HA may send its own tools
    stream = body.get("stream", False)

    return await cloud_brain.chat(
        messages, stream=stream,
        external_tools=external_tools,
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
    global _http, conversation_backend, orchestrator_backend, cloud_brain
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
    conversation_backend = shared.conversation_backend
    orchestrator_backend = shared.orchestrator_backend

    # Load HA entities at startup
    print("[orchestrator] Loading Home Assistant entities...")
    count = await ha_client.refresh_entities()
    shared._ha_tool_cache = None  # Invalidate cache after entity refresh
    shared._ha_tool_cache_time = 0.0
    print(f"[orchestrator] Loaded {count} HA entities")

    # Initialize Google Calendar client
    cal_client = get_calendar_client(http_client=_http)
    if cal_client.is_configured:
        logger.info("[orchestrator] Google Calendar configured — tools enabled")
    else:
        logger.info("[orchestrator] Google Calendar not configured — tools disabled (run google_setup.py)")

    # Initialize LocalAgent + CloudBrain
    local_agent = LocalAgent(
        rag_context_fn=rag_context,
        run_tool_loop_fn=_run_nemotron_tool_loop,
        get_nemotron_system_prompt_fn=get_orchestrator_system_prompt,
        ha_client=ha_client,
        collection=collection,
        scheduler=scheduler,
        profile=profile,
    )
    cloud_brain = CloudBrain(
        local_agent=local_agent,
        call_model_fn=call_model,
        stream_final_response_fn=None,  # CloudBrain uses its own _stream_text()
        get_helios_system_prompt_fn=get_helios_system_prompt,
        get_orchestrator_system_prompt_fn=get_orchestrator_system_prompt,
        check_helios_health_fn=check_helios_health,
        start_helios_fn=start_helios,
        try_fast_path_fn=try_fast_path,
        is_greeting_fn=is_greeting,
        last_user_text_fn=last_user_text,
        clean_response_fn=clean_response,
        parse_tool_calls_fn=parse_tool_calls_from_content,
        helios_tools=HELIOS_TOOLS,
        helios_url=HELIOS_URL,
        helios_model=HELIOS_MODEL,
        nemotron_url=NEMOTRON_URL,
        nemotron_model=NEMOTRON_MODEL,
    )
    cloud_brain.on_helios_request = lambda: setattr(shared, '_last_helios_request', time.time())
    logger.info("[orchestrator] CloudBrain + LocalAgent initialized")

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

    # Schedule Helios idle check (auto-shutdown to save power)
    scheduler.add_job(
        check_helios_idle,
        trigger="interval",
        minutes=5,
        id="helios_idle_check",
        name="Helios idle check",
        replace_existing=True,
    )
    logger.info("[SCHEDULER] Helios idle check every 5 min")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8888)
