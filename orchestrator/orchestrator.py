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

import asyncio
import collections
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from orchestrator.log_config import configure_logging
from orchestrator.metrics import AUTO_LEARN_DROPPED, EMBEDDING_HEALTHY, PHONE_CALENDAR_LAST_SYNC

# Configure structured JSON logging
configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

from orchestrator.log_buffer import log_ring

logging.getLogger().addHandler(log_ring)

# --- Dedicated module imports (the decoupled architecture) ---

# Infrastructure
# Shared state (singletons initialized at module level in shared.py)
from apscheduler.events import EVENT_JOB_MISSED

from orchestrator import (
    calendar_sources,
    job_heartbeat,
    shared,
    state_store,  # REST API routes (infrastructure endpoints)
)
from orchestrator.api_routes import router as api_router

# Background scheduler jobs
from orchestrator.background_jobs import evening_briefing, morning_briefing, poll_calendar, process_emails_for_events
from orchestrator.bearer_auth import bearer_token_matches, is_public_segment
from orchestrator.calendar_sources import get_calendar
from orchestrator.cloud_brain import CloudBrain

# Fast-path for simple device commands (bypasses LLMs)
from orchestrator.fast_path import try_fast_path
from orchestrator.finance_manager import router as finance_router

# Focus session management
from orchestrator.focus_manager import deliver_focus_break

# Model lifecycle management
from orchestrator.model_manager import check_model_health, start_model_server
from orchestrator.pihole_client import get_pihole_client

# Prompts, RAG, helpers
from orchestrator.prompt_builder import (
    get_unified_system_prompt,
    is_greeting,
    last_user_text,
    rag_context,
)

# Settings page (`/api/config/*`)
from orchestrator.routes_config import router as config_router

# First-boot setup wizard (`/api/setup/*`)
from orchestrator.routes_setup import router as setup_router
from orchestrator.shared import (
    CALENDAR_POLL_INTERVAL,
    EVENING_BRIEFING_ENABLED,
    EVENING_BRIEFING_TIME,
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
from orchestrator.tool_definitions import get_all_tools

# Tool execution dispatcher
from orchestrator.tool_handlers import reschedule_pending_reminders_on_startup

# Unified tool loop (v7)
from orchestrator.unified_loop import run_unified_tool_loop

# Load environment
load_dotenv(os.path.expanduser("~/brain_gateway/.env"))

# API authentication — from centralized config
from orchestrator.config import settings as _cfg

API_TOKEN = _cfg.api_token
if not API_TOKEN:
    raise RuntimeError("API_TOKEN environment variable is required — set it in .env or run scripts/setup.sh")


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Require Bearer token on all endpoints except public ones."""

    PUBLIC_PATHS = {"/health"}
    # Prefix-based public paths.
    # - /api/audio/: Google speakers fetch TTS clips without auth
    # - /api/reminder/ack/ and /api/reminder/snooze/: F-011 ntfy action buttons
    #   are called from the user's phone without a bearer token; the route
    #   handler verifies an HMAC-signed URL instead.
    PUBLIC_PREFIXES = (
        "/api/audio/",
        "/api/reminder/ack/",
        "/api/reminder/snooze/",
    )
    # Exact paths that also accept a scoped (non-API_TOKEN) bearer token.
    WEBHOOK_TOKEN_PATHS = frozenset({"/api/self_audit/alert"})

    @classmethod
    def is_public_path(cls, path: str) -> bool:
        """True iff the routed ``path`` needs no bearer token.

        A public prefix exempts exactly ONE segment after it (every route
        behind these prefixes takes one). uvicorn percent-decodes the path, so
        "/api/audio/..%2f..%2fmemory" arrives as "/api/audio/../../memory",
        which a plain startswith() would exempt; ``is_public_segment`` refuses it.
        """
        if path in cls.PUBLIC_PATHS:
            return True
        return any(path.startswith(p) and is_public_segment(path[len(p) :]) for p in cls.PUBLIC_PREFIXES)

    async def dispatch(self, request: Request, call_next):
        # CORS preflight must pass through
        if request.method == "OPTIONS":
            return await call_next(request)
        # Decide on the string the router matches, not request.url.path: that
        # one is rebuilt through urlsplit, which drops a decoded \t, \r or \n
        # ("/api/au%09dio/x" would read as "/api/audio/x").
        path = request.scope["path"]
        # Public endpoints (health, speaker audio, HMAC-signed reminder taps)
        if self.is_public_path(path):
            return await call_next(request)
        # Check Bearer token (scheme case-insensitive, constant-time compare)
        if bearer_token_matches(request.headers.get("authorization", ""), API_TOKEN):
            return await call_next(request)
        # Scoped token: SELF_AUDIT_WEBHOOK_TOKEN (held by Alertmanager) opens
        # exactly one path and nothing else. Empty token never matches.
        if path in self.WEBHOOK_TOKEN_PATHS and bearer_token_matches(
            request.headers.get("authorization", ""), _cfg.self_audit_webhook_token
        ):
            return await call_next(request)
        return JSONResponse({"error": "Unauthorized"}, status_code=401)


# Maximum request body size (bytes) — rejects oversized payloads before LLM processing
MAX_BODY_SIZE = int(os.environ.get("MAX_BODY_SIZE", 1_000_000))  # 1MB default (HA sends all entity states)
MAX_UPLOAD_SIZE = int(os.environ.get("DOCUMENT_MAX_SIZE_MB", 100)) * 1024 * 1024  # for file uploads

# Paths that accept large uploads (documents, STT audio)
_LARGE_UPLOAD_PATHS = {
    "/api/documents",
    "/api/stt/transcribe",
    "/api/vision/analyze",
    "/api/meals/photo",
    "/api/pantry/scan",
    "/api/paperless/upload",
    "/v1/chat/completions",
    "/chat/completions",
}


# Upload routes with their own cap (below MAX_UPLOAD_SIZE) so an oversized body is
# refused on Content-Length instead of being spooled to disk first.
_PATH_UPLOAD_LIMITS = {
    "/api/documents/suggest-name": 16 * 1024 * 1024,
    # Alertmanager webhook: a few alerts' labels/annotations, never more.
    "/api/self_audit/alert": 64 * 1024,
}


# Upload routes that refuse chunked (length-less) bodies.
_LENGTH_REQUIRED_PATHS = {"/api/pantry/scan", "/api/documents/suggest-name", "/api/self_audit/alert"}


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject request bodies larger than MAX_BODY_SIZE (or MAX_UPLOAD_SIZE for file uploads)."""

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        # A chunked body has no Content-Length and would skip the cap entirely.
        # Enforced for the pantry upload (other routes predate this check).
        if not content_length and request.method == "POST" and request.url.path in _LENGTH_REQUIRED_PATHS:
            return JSONResponse({"error": "Content-Length required"}, status_code=411)
        if content_length:
            path = request.url.path
            limit = _PATH_UPLOAD_LIMITS.get(path) or (MAX_UPLOAD_SIZE if path in _LARGE_UPLOAD_PATHS else MAX_BODY_SIZE)
            try:
                if int(content_length) > limit:
                    return JSONResponse(
                        {"error": f"Request body too large (max {limit} bytes)"},
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
        # Remove empty entries to prevent unbounded dict growth
        if not window:
            del _rate_limit_store[client_ip]

        if client_ip not in _rate_limit_store:
            _rate_limit_store[client_ip] = collections.deque()
            window = _rate_limit_store[client_ip]

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """ASGI lifespan — replaces the deprecated @app.on_event hooks.

    Delegates to the same startup/shutdown bodies (defined further down as
    `_startup_logic` / `_shutdown_logic`); they're resolved at call time, after
    the module is fully imported, so definition order doesn't matter.
    """
    await _startup_logic()
    try:
        yield
    finally:
        await _shutdown_logic()


app = FastAPI(
    title="Brain Gateway",
    version="7.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

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
app.include_router(finance_router)
app.include_router(config_router)
app.include_router(setup_router)


# Global exception handler for typed Brain Gateway errors
from orchestrator.exceptions import BrainGatewayError, TransientError


@app.exception_handler(BrainGatewayError)
async def brain_gateway_error_handler(request: Request, exc: BrainGatewayError):
    """Map typed exceptions to appropriate HTTP status codes."""
    status = 503 if isinstance(exc, TransientError) else 400
    # Log full detail server-side; return generic message to client
    logger.error("[API] %s: %s", type(exc).__name__, exc)
    user_msg = "Service temporarily unavailable" if isinstance(exc, TransientError) else "Request failed"
    return JSONResponse({"ok": False, "error": user_msg}, status_code=status)


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
    extra_body: Dict = None,
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
        extra_body=extra_body,
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


def get_stream_capable_backend(url: str, model: str):
    """Return the backend when it supports tool-round streaming, else None.

    Only the OpenAI-compatible backend (vLLM) streams tool_calls deltas; the
    Anthropic/OpenAI fallbacks keep the buffered path — the unified loop
    treats None as "run this round non-streaming".
    """
    from orchestrator.llm_backend import OpenAICompatibleBackend

    backend = _resolve_backend(url, model)
    return backend if isinstance(backend, OpenAICompatibleBackend) else None


def _resolve_backend(url: str, model: str):
    """Pick the backend whose URL matches the call."""
    from orchestrator.llm_backend import LLMConfig, OpenAICompatibleBackend

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
    from orchestrator.schemas import ChatRequest

    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    raw_msgs = body.get("messages", [])
    if not isinstance(raw_msgs, list):
        return JSONResponse({"error": "messages must be an array"}, status_code=400)
    # log-safe: roles and lengths only
    logger.info(
        "[CHAT] Incoming %d messages: %s",
        len(raw_msgs),
        [(m.get("role"), len(str(m.get("content", "")))) for m in raw_msgs[:10]],
    )

    # --- Voice-mode detection ---
    # Two signals, both set is_voice=True:
    #   1) HA Assist pipeline — identified by client IP or "You are 'Al'" prefix
    #   2) OpenAI-style STT client — our /v1/audio/transcriptions proxy sets a consume-on-read
    #      flag; the next chat request within VOICE_FLAG_WINDOW_SEC is voice.
    is_voice = False
    client_ip = req.client.host if req.client else ""
    ha_ip = os.environ.get("HA_URL", "").replace("http://", "").replace("https://", "").split(":")[0]
    first_content = str(raw_msgs[0].get("content", "")) if raw_msgs else ""
    is_ha_request = client_ip == ha_ip or first_content.startswith("You are 'Al'")
    if is_ha_request:
        is_voice = True
        logger.info("[CHAT] Detected HA voice pipeline (client=%s), optimizing", client_ip)
        # Find the LAST user message (HA sends conversation history)
        user_text = ""
        for msg in reversed(raw_msgs):
            content = msg.get("content", "")
            # Handle list-format content from HA
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        parts.append(item.get("text", ""))
                content = " ".join(parts)
            if msg.get("role") == "user" and content.strip():
                user_text = content
                break
        # Check if user query is embedded in the system prompt via {{ user_input }}
        if not user_text:
            sys_content = str(raw_msgs[0].get("content", ""))
            marker = "User request:"
            if marker in sys_content:
                user_text = sys_content.split(marker, 1)[1].strip()
        if not user_text:
            user_text = "Hello"
        logger.info("[CHAT] Voice user query (%d chars)", len(user_text))
        # Replace messages: drop HA system/history, keep just the latest user query
        body["messages"] = [{"role": "user", "content": user_text}]
    elif shared.consume_voice_flag():
        is_voice = True
        logger.info("[CHAT] Detected STT-proxy voice turn (beacon consumed)")

    # Update the sticky voice-activity timestamp (used by reminder_manager to
    # suppress announcements mid-conversation). Covers HA Assist too — its
    # path doesn't go through the STT proxy, so STT-side marking misses it.
    if is_voice:
        shared.mark_voice_activity()

    from orchestrator.cloud_brain import conversation_id_var

    chat_req = ChatRequest(**body)
    conversation_id_var.set(chat_req.conversation_id)

    return await cloud_brain.chat(
        [m.model_dump() for m in chat_req.messages],
        stream=chat_req.stream,
        external_tools=chat_req.tools,
        ha_client=ha_client,
        is_voice=is_voice,
    )


# =============================================================================
# STARTUP / SHUTDOWN
# =============================================================================


_AUTO_LEARN_SHUTDOWN_DROPS_KEY = "auto_learn_dropped_at_shutdown"


async def _shutdown_logic():
    """Clean up resources on shutdown (invoked from the lifespan handler)."""
    global _http

    # auto_learn's pending conversations live in RAM only; make the loss
    # visible. The counter dies with this process, so the count is persisted
    # and added to the metric by the next startup.
    pending = len(shared._auto_learn_conversations)
    if pending:
        logger.info("[AUTO_LEARN] %d pending conversation(s) dropped at shutdown", pending)
        try:
            prior = int(state_store.get_app_state(_AUTO_LEARN_SHUTDOWN_DROPS_KEY) or 0)
            state_store.set_app_state(_AUTO_LEARN_SHUTDOWN_DROPS_KEY, str(prior + pending))
        except Exception as e:
            logger.warning("[AUTO_LEARN] Could not persist shutdown drop count: %s", type(e).__name__)

    # Stop the Telegram long-poll task before the HTTP layer goes away.
    telegram_task = getattr(shared, "telegram_task", None)
    if telegram_task is not None:
        import contextlib

        telegram_task.cancel()
        with contextlib.suppress(BaseException):
            await telegram_task
        shared.telegram_task = None
        logger.info("[orchestrator] Stopped Telegram long-poll task")

    if _http:
        await _http.aclose()
        _http = None
        logger.info("[orchestrator] Closed shared HTTP client")


def _on_job_missed(event) -> None:
    """EVENT_JOB_MISSED listener — a scheduler job was dropped after its
    misfire_grace_time (300s, see shared.py) elapsed, typically because the
    event loop stalled past the grace window.

    One-shot date-trigger jobs (reminder_* delivery, focus-break delivery,
    dnd_auto_unmute) have no next occurrence and no runtime recovery — the
    startup sweep only reschedules at boot — so a miss here is a permanently
    lost action, not a delayed one. Log loud (ERROR) and bump the counter so it
    is alertable instead of leaving APScheduler's own log warning as the only
    trace.
    """
    from orchestrator.metrics import SCHEDULER_JOBS_MISSED, scheduler_job_family

    job_id = getattr(event, "job_id", "<unknown>")
    scheduled = getattr(event, "scheduled_run_time", None)
    family = scheduler_job_family(job_id)
    SCHEDULER_JOBS_MISSED.labels(job_family=family).inc()
    logger.error(
        "[SCHEDULER] Job MISSED (dropped, not delayed): id=%s family=%s scheduled_run_time=%s "
        "— event loop stalled past the 300s misfire grace; one-shot jobs are lost permanently",
        job_id,
        family,
        scheduled.isoformat() if scheduled is not None else "<unknown>",
    )


async def _startup_logic():
    """Initialize services on startup (invoked from the lifespan handler)."""
    global _http, cloud_brain
    _http = httpx.AsyncClient(
        timeout=httpx.Timeout(60, connect=10),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    logger.info("[orchestrator] Initialized shared HTTP client")

    # Embedding self-test: if the model can't encode, every memory feature
    # (search, RAG context, palace writes, auto_learn) fails per call and only
    # shows up as scattered errors — it went unnoticed for 12 days once.
    try:
        # Bounded: a hung encode must not block startup (memory is broken anyway).
        await asyncio.wait_for(
            asyncio.to_thread(lambda: shared.embedding_model.encode("self-test", normalize_embeddings=True)),
            timeout=60,
        )
        EMBEDDING_HEALTHY.set(1)
        logger.info("[EMBEDDING] Self-test OK")
    except Exception as e:
        EMBEDDING_HEALTHY.set(0)
        logger.error("[EMBEDDING] Self-test FAILED — memory search/storage is broken: %s", type(e).__name__)

    # Initialize persistent state store (SQLite)
    state_store.init_db()
    try:
        dropped = int(state_store.get_app_state(_AUTO_LEARN_SHUTDOWN_DROPS_KEY) or 0)
        if dropped:
            AUTO_LEARN_DROPPED.labels(reason="shutdown").inc(dropped)
            state_store.delete_app_state(_AUTO_LEARN_SHUTDOWN_DROPS_KEY)
    except Exception as e:
        logger.warning("[AUTO_LEARN] Could not read shutdown drop count: %s", type(e).__name__)
    state_store.clear_stale_notifications(older_than_hours=48)
    state_store.cleanup_old_announcements(keep_days=30)
    state_store.cleanup_old_selfcare(keep_days=90)
    try:
        from orchestrator import inbox

        inbox.cleanup_old()
    except Exception as e:
        logger.error("[INBOX] Startup cleanup failed: %s", type(e).__name__, exc_info=True)
    try:
        from orchestrator import webpush_manager

        webpush_manager.init()
    except Exception as e:
        logger.error("[WEBPUSH] Startup init failed: %s", type(e).__name__, exc_info=True)

    # Pantry (F-015): a scan mid-vision when the process died can't resume —
    # flip it to failed so the review page offers Retry instead of spinning.
    if shared.PANTRY_ENABLED:
        try:
            from orchestrator.pantry_manager import recover_interrupted_scans

            n = recover_interrupted_scans()
            if n:
                logger.warning("[PANTRY] Marked %d interrupted scan(s) failed (retry from /pantry)", n)
        except Exception as e:
            logger.error("[PANTRY] Scan recovery failed: %s", e, exc_info=True)
        try:
            from orchestrator.meal_planner import recover_interrupted_plans

            n = recover_interrupted_plans()
            if n:
                logger.warning("[MEALPLAN] Marked %d interrupted plan(s) failed", n)
        except Exception as e:
            logger.error("[MEALPLAN] Plan recovery failed: %s", e, exc_info=True)

    # Pre-create dead-speaker metric series (AnnouncementSpeakerUnavailable).
    from orchestrator.reminder_manager import seed_announcement_metrics

    seed_announcement_metrics()

    # First-boot setup wizard state (does not gate startup — informational)
    from orchestrator.routes_setup import is_first_boot

    # Informational only — must never abort startup, hence the broad guard.
    try:
        if is_first_boot():
            logger.info("[SETUP] First boot — setup wizard not yet completed (/api/setup/*)")
        else:
            logger.info("[SETUP] Setup wizard previously completed")
    except Exception:
        logger.warning("[SETUP] first-boot check failed (non-fatal)", exc_info=True)

    # Restore selfcare state from DB (must run after init_db creates tables)
    from orchestrator.selfcare_manager import _restore_state as restore_selfcare

    restore_selfcare()

    # Restore DND (sleep mode) state
    shared.DND_ACTIVE = state_store.is_notified("dnd_active")
    if shared.DND_ACTIVE:
        logger.info("[DND] Restored sleep mode from DB — announcements suppressed")

    # Restore phone calendar events from disk — _load_phone_calendar persists
    # events on every sync but wasn't being called on boot, so a fresh
    # orchestrator started with an empty cache and check_calendar fell through
    # to Google until the iPhone Shortcut posted again.
    shared._load_phone_calendar()

    # Initialize progress tracking DB (F-005)
    from orchestrator import progress_tracker

    progress_tracker.init_db()

    # Finance DB (Quest Board tables) — unconditional: the dashboard finance
    # pages, manual entries and finance_status need the tables even with no
    # budget sync configured. Never raises (logs on failure).
    from orchestrator.finance_manager import setup_finance

    setup_finance()

    # Initialize LLM backends
    shared._http = _http  # ensure shared module has the http client too
    shared.init_backends(_http)

    # Check external service health (non-blocking, logs results)
    from orchestrator.service_registry import check_all_services

    await check_all_services()

    # Load HA entities at startup
    logger.info("[orchestrator] Loading Home Assistant entities...")
    ha_client.set_http_client(_http)
    count = await ha_client.refresh_entities()
    # Entity categories, so the first tool schema already hides device settings
    # (ha_guard.ha_device_setting). Never raises; {} when HA's registry is unreadable.
    from orchestrator.ha_registry import get_registry_meta

    await get_registry_meta(ha_client.url, ha_client.token)
    shared._ha_tool_cache = None  # Invalidate cache after entity refresh
    shared._ha_tool_cache_time = 0.0
    logger.info(f"[orchestrator] Loaded {count} HA entities")

    # Initialize the calendar sources (self-hosted CalDAV, iPhone work push,
    # Google while enabled). See calendar_sources.
    cal_client = get_calendar(http_client=_http)
    _cal = calendar_sources.health()
    logger.info(
        f"[orchestrator] Calendar sources — caldav: {_cal['caldav']['configured']}, "
        f"google: {_cal['google']['configured']}, phone sync: {_cal['phone_sync']['expected']}"
    )
    # Seed from the persisted push so ExchangeCalendarSyncStale ages from the
    # real last sync, not from this restart (0 = never synced).
    # Only a work (Exchange) push arms the alert; a legacy all-calendars
    # push is retired and reads as "never synced" (0).
    PHONE_CALENDAR_LAST_SYNC.set(shared._phone_calendar_sync_time if shared._phone_calendar_source == "exchange" else 0)

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
    logger.info(
        "[orchestrator] CloudBrain initialized (mode=unified_v7, real_streaming=%s, voice_thinking=%s)",
        shared.REAL_STREAMING_ENABLED,
        shared.VOICE_THINKING_ENABLED,
    )

    # Reload pending reminders from DB and re-schedule. Past-due ones are
    # late-delivered or marked missed (they used to be silently dropped —
    # a reboot at 8:55 ate the 9:00 meds reminder with zero signal).
    reminder_counts = reschedule_pending_reminders_on_startup()

    # Prime the open-task gauge from the DB. It's otherwise only written on a
    # task mutation, so after a restart bgw_tasks_open would read 0 (or a stale
    # value) until the next add/complete — this makes it correct immediately.
    try:
        from orchestrator import backlog_manager

        backlog_manager._sync_open_gauge()
        logger.info("[BACKLOG] Open-task gauge synced on startup (%d open)", state_store.open_task_count())
    except Exception as e:
        logger.warning("[BACKLOG] Startup gauge sync failed: %s", e)

    # Restore focus session from DB (survives orchestrator restarts)
    saved_focus = state_store.load_focus_session()
    if saved_focus["active"]:
        end_time = saved_focus["started"] + timedelta(minutes=saved_focus["duration"])
        if end_time <= datetime.now():
            # Session expired while we were down — clean up blocking
            logger.info("[FOCUS] Found expired focus session — cleaning up")
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
                "[FOCUS] Restored active focus session (%.0f min remaining, break at %s)",
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

    # Surface silently-dropped jobs: APScheduler discards any job whose 300s
    # misfire_grace_time lapses during an event-loop stall, emitting only its
    # own log warning — no metric, no alert, and one-shot date jobs are gone for
    # good. Wire the EVENT_JOB_MISSED listener BEFORE start() so no early miss
    # slips through the gap between start and registration.
    scheduler.add_listener(_on_job_missed, EVENT_JOB_MISSED)
    scheduler.start()
    logger.info(f"[SCHEDULER] Started ({sum(reminder_counts.values())} reminders reloaded from DB)")

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
            # Seed the heartbeat from the PERSISTED last run (job_heartbeat), never
            # now(): seeding now() masked a dead job during busy deploy weeks, and
            # leaving it at 0 blinded MorningBriefingStale (which ignores 0) after
            # every restart. 0 only until the first ever run.
            from orchestrator.jobs_calendar import MORNING_BRIEFING_HEARTBEAT_KEY
            from orchestrator.metrics import MORNING_BRIEFING_LAST_RUN

            job_heartbeat.seed(MORNING_BRIEFING_LAST_RUN, MORNING_BRIEFING_HEARTBEAT_KEY)

        # Email-to-calendar autonomy: scan inbox, extract events via LLM,
        # auto-create calendar entries. Disabled by default — flip
        # EMAIL_TO_CALENDAR_ENABLED=true in .env when ready to turn on.
        # The full pipeline (gmail client, LLM extraction, dedup, calendar
        # write) is wired and tested; only the scheduler trigger is gated.
        if shared.EMAIL_TO_CALENDAR_ENABLED:
            scheduler.add_job(
                process_emails_for_events,
                trigger="interval",
                minutes=shared.EMAIL_TO_CALENDAR_INTERVAL,
                id="email_to_calendar",
                name="Email → Calendar auto-import",
                replace_existing=True,
            )
            logger.info(f"[SCHEDULER] Email-to-calendar every {shared.EMAIL_TO_CALENDAR_INTERVAL} min")

    # Evening shutdown ritual — deliberately NOT gated on calendar config:
    # meds check + parking one unfinished thing still deliver without one.
    # Registration is guarded: this runs on EVERY boot (enabled by default),
    # so a malformed EVENING_BRIEFING_TIME must skip the job, not crash-loop
    # the whole orchestrator out of startup.
    if EVENING_BRIEFING_ENABLED:
        try:
            hour, minute = map(int, EVENING_BRIEFING_TIME.split(":"))
            scheduler.add_job(
                evening_briefing,
                trigger="cron",
                hour=hour,
                minute=minute,
                id="evening_briefing",
                name="Evening shutdown ritual",
                replace_existing=True,
            )
            logger.info(f"[SCHEDULER] Evening briefing at {EVENING_BRIEFING_TIME}")
            # Seeded from the persisted last run — same rationale as the morning
            # gauge above (never now(); 0 only until the first ever run).
            from orchestrator.jobs_calendar import EVENING_BRIEFING_HEARTBEAT_KEY
            from orchestrator.metrics import EVENING_BRIEFING_LAST_RUN

            job_heartbeat.seed(EVENING_BRIEFING_LAST_RUN, EVENING_BRIEFING_HEARTBEAT_KEY)
        except Exception as e:
            logger.error(
                f"[SCHEDULER] Evening briefing registration failed (EVENING_BRIEFING_TIME={EVENING_BRIEFING_TIME!r}): {e}"
            )

    # Sleep wind-down ladder: T-60 lights dim + T-30 screens-away nudge ahead
    # of WIND_DOWN_BEDTIME. The try/except guards the bedtime parse and job
    # registration only — an import-time error in jobs_winddown still crashes
    # startup via the module-top background_jobs import, like every jobs_*
    # module. Minute math handles a past-midnight bedtime (rungs land the
    # previous evening).
    if shared.WIND_DOWN_ENABLED:
        try:
            from orchestrator.background_jobs import wind_down_dim, wind_down_nudge

            bed_h, bed_m = map(int, shared.WIND_DOWN_BEDTIME.split(":"))
            if not (0 <= bed_h < 24 and 0 <= bed_m < 60):
                raise ValueError(f"bedtime out of range: {bed_h:02d}:{bed_m:02d}")
            bed_total = bed_h * 60 + bed_m
            rung_times = []
            for offset, job_fn, job_id, job_name in (
                (60, wind_down_dim, "wind_down_dim", "Wind-down: lights dim (T-60)"),
                (30, wind_down_nudge, "wind_down_nudge", "Wind-down: screens-away nudge (T-30)"),
            ):
                t = (bed_total - offset) % 1440
                rung_times.append(f"{t // 60:02d}:{t % 60:02d}")
                scheduler.add_job(
                    job_fn,
                    trigger="cron",
                    hour=t // 60,
                    minute=t % 60,
                    id=job_id,
                    name=job_name,
                    replace_existing=True,
                    # misfire_grace_time=300 comes from the scheduler-wide
                    # job_defaults in shared.py (prod-support M1, 2026-07-06).
                )
            logger.info(
                f"[SCHEDULER] Wind-down ladder: dim at {rung_times[0]}, nudge at {rung_times[1]} "
                f"(bedtime {shared.WIND_DOWN_BEDTIME}, scenes: {shared.WIND_DOWN_SCENE or 'none configured'})"
            )
            # Seed both rungs from the PERSISTED last run (job_heartbeat), never
            # now() — same reasoning as the morning briefing. The dim gauge has no
            # alert; its dashboard panel just needs the real last fire time.
            from orchestrator.jobs_winddown import WIND_DOWN_DIM_HEARTBEAT_KEY, WIND_DOWN_HEARTBEAT_KEY
            from orchestrator.metrics import WIND_DOWN_DIM_LAST_RUN, WIND_DOWN_LAST_RUN

            job_heartbeat.seed(WIND_DOWN_LAST_RUN, WIND_DOWN_HEARTBEAT_KEY)
            job_heartbeat.seed(WIND_DOWN_DIM_LAST_RUN, WIND_DOWN_DIM_HEARTBEAT_KEY)
        except Exception as e:
            logger.error(
                f"[SCHEDULER] Wind-down registration failed (WIND_DOWN_BEDTIME={shared.WIND_DOWN_BEDTIME!r}): {e}"
            )

    # Heartbeats of jobs that ended up NOT scheduled (disabled, calendar not
    # configured, bad time): drop their stamps, so re-enabling later doesn't
    # page on a days-old last run. (Scheduled ones were seeded above.)
    from orchestrator.jobs_calendar import EVENING_BRIEFING_HEARTBEAT_KEY, MORNING_BRIEFING_HEARTBEAT_KEY
    from orchestrator.jobs_winddown import WIND_DOWN_DIM_HEARTBEAT_KEY, WIND_DOWN_HEARTBEAT_KEY

    for _job_id, _hb_key in (
        ("morning_briefing", MORNING_BRIEFING_HEARTBEAT_KEY),
        ("evening_briefing", EVENING_BRIEFING_HEARTBEAT_KEY),
        ("wind_down_nudge", WIND_DOWN_HEARTBEAT_KEY),
        ("wind_down_dim", WIND_DOWN_DIM_HEARTBEAT_KEY),
    ):
        if scheduler.get_job(_job_id) is None:
            job_heartbeat.forget(_hb_key)

    # Initialize routine manager (F-006)
    if shared.ROUTINE_ENABLED:
        from orchestrator.background_jobs import trigger_routine
        from orchestrator.routine_manager import load_routines
        from orchestrator.routines_config import effective_path as _routines_effective_path

        # Prefer the writable shadow at ROUTINES_OVERRIDES_PATH (settings-page edits)
        # over the read-only base at ROUTINES_YAML_PATH. effective_path() handles the fallback.
        _routines_path = _routines_effective_path()
        await load_routines(_routines_path)

        # Schedule routine triggers from YAML
        try:
            import yaml

            with open(_routines_path) as f:
                _routines_data = yaml.safe_load(f) or {}
            for _rid, _rdef in _routines_data.get("routines", {}).items():
                _trigger = _rdef.get("trigger", {})
                if _trigger.get("type") == "scheduled":
                    _t = _trigger["time"]
                    _hour, _minute = map(int, _t.split(":"))
                    _days = _trigger.get("days", ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
                    _dow = ",".join(d[:3].lower() for d in _days)
                    scheduler.add_job(
                        trigger_routine,
                        trigger="cron",
                        hour=_hour,
                        minute=_minute,
                        day_of_week=_dow,
                        args=[_rid],
                        id=f"routine_{_rid}",
                        name=f"Routine trigger: {_rid}",
                        replace_existing=True,
                    )
                    logger.info(f"[SCHEDULER] Routine '{_rid}' scheduled at {_t} ({_dow})")
        except FileNotFoundError:
            logger.warning(f"[ROUTINE] Routines file not found: {_routines_path}")
        except Exception as e:
            logger.warning(f"[ROUTINE] Failed to schedule routine triggers: {e}")

    # Schedule ambient awareness jobs (F-010)
    if shared.AMBIENT_ENABLED:
        from orchestrator.background_jobs import ambient_summary, update_ambient_led

        # Periodic TTS summaries at configured times
        for _time_str in shared.AMBIENT_SUMMARY_TIMES.split(","):
            _time_str = _time_str.strip()
            if not _time_str:
                continue
            try:
                _h, _m = map(int, _time_str.split(":"))
                scheduler.add_job(
                    ambient_summary,
                    trigger="cron",
                    hour=_h,
                    minute=_m,
                    id=f"ambient_summary_{_h:02d}{_m:02d}",
                    name=f"Ambient summary at {_time_str}",
                    replace_existing=True,
                )
            except Exception as e:
                logger.warning(f"[AMBIENT] Failed to schedule summary at {_time_str}: {e}")

        # LED update every 5 min (if entity configured)
        if shared.AMBIENT_LED_ENTITY:
            scheduler.add_job(
                update_ambient_led,
                trigger="interval",
                minutes=5,
                id="ambient_led_update",
                name="Ambient LED update",
                replace_existing=True,
            )
            logger.info(f"[SCHEDULER] Ambient LED update every 5 min on {shared.AMBIENT_LED_ENTITY}")

        logger.info(f"[SCHEDULER] Ambient summaries at {shared.AMBIENT_SUMMARY_TIMES}")

    # Schedule self-care nudge checks (F-008)
    if shared.SELFCARE_ENABLED:
        from orchestrator.background_jobs import check_selfcare

        scheduler.add_job(
            check_selfcare,
            trigger="interval",
            minutes=15,
            id="selfcare_check",
            name="Self-care nudge check",
            replace_existing=True,
        )
        logger.info("[SCHEDULER] Self-care nudges every 15 min")

    # Schedule presence polling
    if shared.PRESENCE_ENABLED:
        from orchestrator.presence_tracker import poll_presence

        async def _presence_poll_with_welcome():
            await poll_presence()
            # Check for welcome home greeting
            try:
                from orchestrator.presence_tracker import check_welcome_home

                if check_welcome_home() and not shared.DND_ACTIVE:
                    from orchestrator.reminder_manager import _announce_voice

                    # Build a brief welcome status
                    from orchestrator.state_store import get_pending_reminders

                    pending = get_pending_reminders()
                    parts = ["Welcome home!"]
                    if pending:
                        parts.append(f"You have {len(pending)} pending reminder{'s' if len(pending) != 1 else ''}.")
                    await _announce_voice(" ".join(parts), announcement_type="greeting")
                    logger.info("[PRESENCE] Welcome home greeting delivered")
            except Exception as e:
                logger.warning(f"[PRESENCE] Welcome home error: {e}")

        scheduler.add_job(
            _presence_poll_with_welcome,
            trigger="interval",
            seconds=shared.PRESENCE_POLL_INTERVAL,
            id="presence_poll",
            name="Presence polling",
            replace_existing=True,
        )
        logger.info(f"[SCHEDULER] Presence polling every {shared.PRESENCE_POLL_INTERVAL}s")

    # Telegram bot — dedicated forever task, NOT a scheduler job (the 50s
    # getUpdates long-poll would pin a scheduler worker the whole time).
    # Cancelled in _shutdown_logic.
    if _cfg.telegram_enabled and _cfg.telegram_bot_token:
        import asyncio as _asyncio_tg

        from orchestrator.telegram_bot import telegram_poll_loop

        shared.telegram_task = _asyncio_tg.create_task(telegram_poll_loop(), name="telegram-poll")
        logger.info("[TELEGRAM] Bot enabled — long-poll task started")
    elif _cfg.telegram_enabled:
        logger.warning("[TELEGRAM] telegram_enabled but TELEGRAM_BOT_TOKEN is empty — bot not started")

    # Schedule progress tracking jobs (F-005)
    if shared.PROGRESS_ENABLED:
        from orchestrator.background_jobs import daily_progress_summary, weekly_progress_digest

        ds_hour, ds_minute = map(int, shared.DAILY_SUMMARY_TIME.split(":"))
        scheduler.add_job(
            daily_progress_summary,
            trigger="cron",
            hour=ds_hour,
            minute=ds_minute,
            id="daily_progress_summary",
            name="Daily progress summary",
            replace_existing=True,
        )

        ws_hour, ws_minute = map(int, shared.WEEKLY_SUMMARY_TIME.split(":"))
        scheduler.add_job(
            weekly_progress_digest,
            trigger="cron",
            day_of_week=shared.WEEKLY_SUMMARY_DAY[:3].lower(),
            hour=ws_hour,
            minute=ws_minute,
            id="weekly_progress_digest",
            name="Weekly progress digest",
            replace_existing=True,
        )
        logger.info(
            f"[SCHEDULER] Progress summary at {shared.DAILY_SUMMARY_TIME} daily, "
            f"digest {shared.WEEKLY_SUMMARY_DAY} {shared.WEEKLY_SUMMARY_TIME}"
        )

    # Weekly task-backlog review (Sunday 10am): surfaces the open list so
    # captured tasks get triaged instead of quietly rotting. Core feature, so
    # scheduled regardless of PROGRESS_ENABLED; stays silent when the list is empty.
    from orchestrator.background_jobs import weekly_backlog_review

    scheduler.add_job(
        weekly_backlog_review,
        trigger="cron",
        day_of_week="sun",
        hour=10,
        minute=0,
        id="weekly_backlog_review",
        name="Weekly task backlog review",
        replace_existing=True,
    )
    logger.info("[SCHEDULER] Weekly backlog review scheduled (Sun 10:00)")

    # Weekly DB maintenance (vacuum + cleanup, Sundays 3am)
    async def _db_maintenance():
        import asyncio

        await asyncio.to_thread(state_store.cleanup_old_announcements, 30)
        await asyncio.to_thread(state_store.cleanup_old_selfcare, 90)
        await asyncio.to_thread(state_store.cleanup_old_claude_code_turns, 7)
        await asyncio.to_thread(state_store.cleanup_old_config_changes, 180)
        await asyncio.to_thread(state_store.vacuum_db)
        # Clean up stale audio files from Cast TTS path
        await asyncio.to_thread(_cleanup_audio_files)
        logger.info("[DB] Weekly maintenance complete (cleanup + vacuum + audio)")

    scheduler.add_job(
        _db_maintenance,
        trigger="cron",
        day_of_week="sun",
        hour=3,
        minute=0,
        id="db_maintenance",
        name="Weekly DB maintenance",
        replace_existing=True,
    )

    # Self-audit (F-014): daily error-log audit. Queries Loki for last 24h of
    # error/warn logs across docker + systemd services, asks Jess to diagnose
    # each cluster, pushes a one-line digest via Pushover, saves a markdown
    # report under SELF_AUDIT_OUTPUT_DIR. Read-only by design — Jess emits
    # text only; the user reviews and runs commands manually.
    # Gated by JESS_ADVANCED (operator feature, requires Loki + Pushover stack).
    if shared.SELF_AUDIT_ENABLED and shared.JESS_ADVANCED:
        from orchestrator.jobs_self_audit import run_self_audit, seed_last_run_gauge

        seed_last_run_gauge()
        scheduler.add_job(
            run_self_audit,
            trigger="cron",
            hour=shared.SELF_AUDIT_HOUR_UTC,
            minute=0,
            # An alert-triggered investigation may hold the audit lock: wait
            # for it instead of dropping the day's audit.
            kwargs={"wait_for_lock": True},
            id="self_audit_daily",
            name="Daily self-audit",
            replace_existing=True,
        )
        # SELF_AUDIT_HOUR_UTC is a misnomer: the cron runs in the scheduler's tz.
        logger.info(f"[SCHEDULER] Self-audit daily at {shared.SELF_AUDIT_HOUR_UTC:02d}:00 {shared.TIMEZONE}")

        # Helios log-silence check (feeds HeliosLogsSilent): cheap Loki count.
        from orchestrator.jobs_self_audit import check_helios_log_silence

        scheduler.add_job(
            check_helios_log_silence,
            trigger="interval",
            minutes=5,
            # First check 30 s after start: the gauge defaults to 0, so waiting
            # the full interval would blank an ongoing HeliosLogsSilent per deploy.
            next_run_time=datetime.now(scheduler.timezone) + timedelta(seconds=30),
            id="helios_log_silence_check",
            name="Helios log-silence check",
            replace_existing=True,
        )
    else:
        from orchestrator.jobs_self_audit import forget_last_run

        forget_last_run()

    # Training corpus drain: nightly at 02:30 — appends new user/assistant
    # turns from state_store + Claude Code sessions to monthly JSONL
    # files under /app/data/training_corpus/. Append-only, content-addressed
    # dedup, no retention cap. Backs future fine-tuning runs.
    # Gated by JESS_ADVANCED — collects user conversation data, privacy hazard
    # for the default shippable build.
    if shared.JESS_ADVANCED:
        from orchestrator.jobs_training_corpus import drain_training_corpus

        scheduler.add_job(
            drain_training_corpus,
            trigger="cron",
            hour=2,
            minute=30,
            id="training_corpus_drain",
            name="Training corpus drain",
            replace_existing=True,
        )
        # Also run a backfill one-shot 30s after startup. Idempotent: the drain
        # dedups by content-addressed id, so re-running is effectively free once
        # the corpus is caught up. Keeps the metric warm in the FastAPI process.
        # Use the scheduler's own tz so the naive-now footgun is avoided.
        scheduler.add_job(
            drain_training_corpus,
            trigger="date",
            run_date=datetime.now(scheduler.timezone) + timedelta(seconds=30),
            id="training_corpus_backfill",
            name="Training corpus backfill (startup)",
            replace_existing=True,
        )
        logger.info("[SCHEDULER] Training corpus drain daily at 02:30 + startup backfill")

    # RAG source file watcher: periodically check ~/rag/nadim_rag for edits
    # and re-ingest changed files into shared.collection. Runs in-process so
    # the updates are immediately visible to the chat pipeline (no restart
    # required, unlike out-of-process ingestion which leaves the daemon's
    # HNSW index stale).
    from orchestrator.rag_ingest import check_and_ingest as _rag_check_and_ingest

    scheduler.add_job(
        _rag_check_and_ingest,
        trigger="interval",
        minutes=2,
        id="rag_ingest_watch",
        name="RAG source file watcher",
        replace_existing=True,
    )

    # Recurring reminders (settings page): every 5 min, materialize one-shot
    # reminders for any rule whose next_fire_at falls within the window. The
    # one-shots then dispatch through the existing deliver_reminder_job path.
    from orchestrator.recurring_reminders import (
        EXPANSION_WINDOW_MINUTES,
        expand_due_reminders,
    )

    scheduler.add_job(
        expand_due_reminders,
        trigger="interval",
        minutes=EXPANSION_WINDOW_MINUTES,
        id="recurring_reminders_expand",
        name="Recurring reminders expansion",
        replace_existing=True,
    )
    logger.info(f"[SCHEDULER] Recurring reminder expansion every {EXPANSION_WINDOW_MINUTES}m")

    # Dashboard inbox + Web Push housekeeping (startup also runs the inbox
    # prune, but the process can stay up for months).
    def _inbox_housekeeping() -> None:
        from orchestrator import inbox, webpush_manager

        try:
            inbox.cleanup_old()
        except Exception as e:
            logger.error("[INBOX] Cleanup failed: %s", type(e).__name__, exc_info=True)
        try:
            webpush_manager.prune_stale_subscriptions()
        except Exception as e:
            logger.error("[WEBPUSH] Subscription prune failed: %s", type(e).__name__, exc_info=True)

    scheduler.add_job(
        _inbox_housekeeping,
        trigger="cron",
        hour=4,
        minute=25,
        id="inbox_housekeeping",
        name="Inbox + Web Push housekeeping",
        replace_existing=True,
    )

    # Pantry (F-015): daily cleanup of review photos left by failed/abandoned
    # scans — they are photos of the inside of the home.
    if shared.PANTRY_ENABLED:
        from orchestrator.pantry_manager import sweep_stale_scans

        scheduler.add_job(
            sweep_stale_scans,
            trigger="cron",
            hour=4,
            minute=20,
            id="pantry_sweep",
            name="Pantry stale-scan photo sweep",
            replace_existing=True,
        )
        logger.info("[SCHEDULER] Pantry stale-scan sweep daily at 04:20")

        # Consumption tracking (F-015 phase 4): daily expiry pass + the weekly
        # fridge-check nudge (ticks every 15 min; sends once on the set day/time).
        from orchestrator.pantry_consumption import fridge_check_tick, run_decay, seed_decay_gauge

        scheduler.add_job(
            run_decay,
            trigger="cron",
            hour=4,
            minute=15,
            id="pantry_decay",
            name="Pantry perishable / prep-batch expiry",
            replace_existing=True,
        )
        scheduler.add_job(
            fridge_check_tick,
            trigger="interval",
            minutes=15,
            id="pantry_fridge_check",
            name="Pantry weekly fridge-check nudge",
            replace_existing=True,
        )
        seed_decay_gauge()  # real last run from app_state, not now()
        logger.info("[SCHEDULER] Pantry decay daily at 04:15; fridge-check tick every 15m")

    # Helios wake-on-demand (PT-C): keep bgw_helios_plug_watts / bgw_helios_running
    # fresh so dashboards + alerts can trust them. Without this the gauges only
    # update when a human/LLM happens to query power state. Runs when wake OR the
    # read-only plug monitor is enabled — HeliosPoweredButUnreachable needs fresh
    # watts even while auto-wake is off (both are default-OFF).
    if shared.HELIOS_WAKE_ENABLED or shared.HELIOS_PLUG_MONITOR_ENABLED:
        from orchestrator.helios_power import poll_helios_power

        scheduler.add_job(
            poll_helios_power,
            trigger="interval",
            seconds=60,
            id="helios_status_poll",
            name="Helios power-state poll",
            replace_existing=True,
        )
        logger.info("[SCHEDULER] Helios power-state poll every 60s")

    # Finance jobs (Actual Budget sync + spending announcements). Gated on the
    # sync being configured: the weekly summary / mid-month warning read the
    # local finance DB and would announce meaningless "$0 of $1000" updates via
    # TTS on an install that never connected a budget.
    from orchestrator.finance_manager import _is_sync_configured

    if _is_sync_configured():
        from orchestrator.background_jobs import (
            midmonth_budget_warning,
            sync_budget_transactions,
            weekly_spending_summary,
        )

        scheduler.add_job(
            sync_budget_transactions,
            trigger="interval",
            minutes=_cfg.actual_sync_interval,
            id="budget_sync",
            name="Actual Budget sync",
            replace_existing=True,
            # First run ~30 s after startup instead of a full interval later.
            next_run_time=datetime.now(scheduler.timezone) + timedelta(seconds=30),
        )
        # "Sunday evening" per the job's docstring.
        scheduler.add_job(
            weekly_spending_summary,
            trigger="cron",
            day_of_week="sun",
            hour=18,
            minute=0,
            id="weekly_spending_summary",
            name="Weekly spending summary",
            replace_existing=True,
        )
        # Once on the 15th (inside the job's own 13th-17th window) rather than
        # daily — the warning has no repeat-suppression, so a daily trigger
        # would re-announce the same overspend five days in a row.
        scheduler.add_job(
            midmonth_budget_warning,
            trigger="cron",
            day=15,
            hour=17,
            minute=0,
            id="midmonth_budget_warning",
            name="Mid-month budget warning",
            replace_existing=True,
        )
        logger.info(
            f"[SCHEDULER] Actual Budget sync every {_cfg.actual_sync_interval} min; "
            "weekly spending summary Sun 18:00; mid-month budget warning on the 15th"
        )

    # Closet temperature monitor: polls an HA sensor, so only when HA is
    # configured — with no HA_URL every poll raises and error-spams the log.
    # A configured-but-missing sensor is fine (non-200 -> silent return).
    if shared.HA_URL and shared.HA_TOKEN and shared.profile.closet_temp_sensor:
        from orchestrator.background_jobs import check_closet_temperature

        scheduler.add_job(
            check_closet_temperature,
            trigger="interval",
            minutes=10,
            id="closet_temp_check",
            name="Closet temperature check",
            replace_existing=True,
        )
        logger.info(f"[SCHEDULER] Closet temperature check every 10 min ({shared.profile.closet_temp_sensor})")


def _cleanup_audio_files(max_age_hours: int = 1) -> None:
    """Remove old TTS audio files from /tmp/brain_audio."""
    import contextlib

    audio_dir = "/tmp/brain_audio"
    if not os.path.isdir(audio_dir):
        return
    cutoff = time.time() - (max_age_hours * 3600)
    count = 0
    for f in os.listdir(audio_dir):
        fp = os.path.join(audio_dir, f)
        with contextlib.suppress(OSError):
            if os.path.getmtime(fp) < cutoff:
                os.remove(fp)
                count += 1
    if count:
        logger.info(f"[CLEANUP] Removed {count} stale audio files from {audio_dir}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8888)
