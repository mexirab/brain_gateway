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
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# Configure structured JSON logging
from orchestrator.log_config import configure_logging

configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

from orchestrator.log_buffer import log_ring

logging.getLogger().addHandler(log_ring)

# --- Dedicated module imports (the decoupled architecture) ---

# Infrastructure
# Shared state (singletons initialized at module level in shared.py)
from orchestrator import (
    shared,
    state_store,  # REST API routes (infrastructure endpoints)
)
from orchestrator.api_routes import router as api_router

# Background scheduler jobs
from orchestrator.background_jobs import morning_briefing, poll_calendar, process_emails_for_events
from orchestrator.cloud_brain import CloudBrain

# Fast-path for simple device commands (bypasses LLMs)
from orchestrator.fast_path import try_fast_path
from orchestrator.finance_manager import router as finance_router

# Focus session management
from orchestrator.focus_manager import deliver_focus_break
from orchestrator.google_calendar import get_calendar_client

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
MAX_UPLOAD_SIZE = int(os.environ.get("DOCUMENT_MAX_SIZE_MB", 100)) * 1024 * 1024  # for file uploads

# Paths that accept large uploads (documents, STT audio)
_LARGE_UPLOAD_PATHS = {
    "/api/documents",
    "/api/stt/transcribe",
    "/api/vision/analyze",
    "/api/meals/photo",
    "/api/paperless/upload",
    "/v1/chat/completions",
    "/chat/completions",
}


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject request bodies larger than MAX_BODY_SIZE (or MAX_UPLOAD_SIZE for file uploads)."""

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            path = request.url.path
            limit = MAX_UPLOAD_SIZE if path in _LARGE_UPLOAD_PATHS else MAX_BODY_SIZE
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
    logger.info(
        "[CHAT] Incoming %d messages: %s",
        len(raw_msgs),
        [(m.get("role"), len(str(m.get("content", "")))) for m in raw_msgs[:10]],
    )

    # --- Voice-mode detection ---
    # Two signals, both set is_voice=True:
    #   1) HA Assist pipeline — identified by client IP or "You are 'Al'" prefix
    #   2) OWUI mic — our /v1/audio/transcriptions proxy sets a consume-on-read
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
        logger.info("[CHAT] Voice user query: %s", user_text[:200])
        # Replace messages: drop HA system/history, keep just the latest user query
        body["messages"] = [{"role": "user", "content": user_text}]
    elif shared.consume_voice_flag():
        is_voice = True
        logger.info("[CHAT] Detected OWUI mic voice turn (STT beacon consumed)")

    # Update the sticky voice-activity timestamp (used by reminder_manager to
    # suppress announcements mid-conversation). Covers HA Assist too — its
    # path doesn't go through the STT proxy, so STT-side marking misses it.
    if is_voice:
        shared.mark_voice_activity()

    chat_req = ChatRequest(**body)

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


async def _shutdown_logic():
    """Clean up resources on shutdown (invoked from the lifespan handler)."""
    global _http
    if _http:
        await _http.aclose()
        _http = None
        logger.info("[orchestrator] Closed shared HTTP client")


async def _startup_logic():
    """Initialize services on startup (invoked from the lifespan handler)."""
    global _http, cloud_brain
    _http = httpx.AsyncClient(
        timeout=httpx.Timeout(60, connect=10),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    logger.info("[orchestrator] Initialized shared HTTP client")

    # Initialize persistent state store (SQLite)
    state_store.init_db()
    state_store.clear_stale_notifications(older_than_hours=48)
    state_store.cleanup_old_announcements(keep_days=30)
    state_store.cleanup_old_selfcare(keep_days=90)

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

    # Initialize LLM backends
    shared._http = _http  # ensure shared module has the http client too
    shared.init_backends(_http)

    # Check external service health (non-blocking, logs results)
    from orchestrator.service_registry import check_all_services

    await check_all_services()

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

    # Reload pending reminders from DB and re-schedule. Past-due ones are
    # late-delivered or marked missed (they used to be silently dropped —
    # a reboot at 8:55 ate the 9:00 meds reminder with zero signal).
    reminder_counts = reschedule_pending_reminders_on_startup()

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
            # Seed the dead-man's-switch gauge to "now" so a fresh restart
            # doesn't trip MorningBriefingStale before the first briefing fires
            # (the gauge starts at 0 → time()-0 would look infinitely stale).
            from orchestrator.metrics import MORNING_BRIEFING_LAST_RUN

            MORNING_BRIEFING_LAST_RUN.set_to_current_time()

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
        from orchestrator.jobs_self_audit import run_self_audit

        scheduler.add_job(
            run_self_audit,
            trigger="cron",
            hour=shared.SELF_AUDIT_HOUR_UTC,
            minute=0,
            id="self_audit_daily",
            name="Daily self-audit",
            replace_existing=True,
        )
        logger.info(f"[SCHEDULER] Self-audit daily at {shared.SELF_AUDIT_HOUR_UTC:02d}:00 UTC")

    # Training corpus drain: nightly at 02:30 — appends new user/assistant
    # turns from OWUI + state_store + Claude Code sessions to monthly JSONL
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

    # Helios wake-on-demand (PT-C): keep bgw_helios_plug_watts / bgw_helios_running
    # fresh so dashboards + alerts can trust them. Without this the gauges only
    # update when a human/LLM happens to query power state. Only when enabled —
    # otherwise the HA call would be pointless (and the feature is default-OFF).
    if shared.HELIOS_WAKE_ENABLED:
        from orchestrator.helios_power import helios_power_status

        scheduler.add_job(
            helios_power_status,
            trigger="interval",
            seconds=60,
            id="helios_status_poll",
            name="Helios power-state poll",
            replace_existing=True,
        )
        logger.info("[SCHEDULER] Helios power-state poll every 60s")


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
