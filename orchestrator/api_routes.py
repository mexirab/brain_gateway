"""
Secondary REST API endpoints (health, metrics, memory, reminders, focus, audio, HA).

Domain-specific routes are split into separate modules and included as sub-routers:
- routes_calendar: /api/calendar/*, /api/email-to-calendar/*
- routes_chat: /api/chat/*
- routes_documents: /api/documents/*
- routes_shopping: /api/shopping/*
- routes_pantry: /api/pantry/* (only when PANTRY_ENABLED)
- routes_vision: /api/vision/*, /api/stt/*, /api/tts/*
"""

import contextlib
import logging
import math
import os
import re
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from orchestrator import calendar_sources, reminder_actions, shared
from orchestrator.bearer_auth import bearer_token_matches
from orchestrator.focus_manager import tool_start_focus, tool_stop_focus
from orchestrator.ha_guard import HA_LISTED_DOMAINS, check_ha_call, ha_entity_allowed
from orchestrator.ha_registry import entity_view, get_registry_meta
from orchestrator.metrics import (
    FALLBACK_ONLINE,
    FOCUS_ACTIVE,
    HA_CALL_REFUSED,
    HELIOS_ONLINE,
    REMINDERS_PENDING,
    TEMPERATURE_DELTA,
    TEMPERATURE_GAUGE,
)
from orchestrator.model_manager import check_model_health
from orchestrator.prompt_builder import rag_context
from orchestrator.reminder_manager import _announce_voice, list_pending_reminders, mark_reminder_completed
from orchestrator.routes_calendar import router as calendar_router
from orchestrator.routes_chat import router as chat_router
from orchestrator.routes_documents import router as documents_router
from orchestrator.routes_inbox import router as inbox_router
from orchestrator.routes_meals import router as meals_router
from orchestrator.routes_palace import router as palace_router
from orchestrator.routes_pantry import router as pantry_router
from orchestrator.routes_paperless import router as paperless_router
from orchestrator.routes_self_audit import router as self_audit_router
from orchestrator.routes_shopping import router as shopping_router
from orchestrator.routes_tasks import router as tasks_router
from orchestrator.routes_vision import router as vision_router
from orchestrator.routes_webpush import router as webpush_router
from orchestrator.routes_workout import router as workout_router
from orchestrator.schemas import (
    AnnounceRequest,
    FocusStartRequest,
    HACommandRequest,
    MemoryAddRequest,
    ReminderTriggerRequest,
    error_response,
)
from orchestrator.shared import (
    CALENDAR_POLL_INTERVAL,
    CHROMA_COLLECTION,
    CHROMA_PERSIST,
    ENDEL_ENABLED,
    ENDEL_MODES,
    FALLBACK_MODEL_NAME,
    FALLBACK_MODEL_URL,
    FOCUS_AUDIO_PLAYER,
    HA_TOKEN,
    HA_URL,
    MODEL_NAME,
    MODEL_URL,
    MORNING_BRIEFING_ENABLED,
    MORNING_BRIEFING_TIME,
    collection,
    current_focus_session,
    ha_client,
    profile,
    scheduler,
)
from orchestrator.tool_handlers import deliver_reminder_job

logger = logging.getLogger(__name__)

router = APIRouter()


# F-011 background-task keeper. `asyncio.create_task` only holds a weak
# reference — if the handler returns before the task's first await point
# (e.g. fire-and-forget confirm push on a route that itself does almost
# nothing), the task can be garbage-collected mid-flight and silently
# never run. We hold a strong reference in a module-level set and clear
# it via done-callback. See https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
import asyncio as _asyncio

_BACKGROUND_TASKS: set = set()


def _fire_and_forget(coro) -> None:
    """Schedule `coro` on the event loop with a strong reference held."""
    task = _asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


# Include domain-specific sub-routers
router.include_router(calendar_router)
router.include_router(chat_router)
router.include_router(documents_router)
router.include_router(inbox_router)
router.include_router(webpush_router)
router.include_router(shopping_router)
router.include_router(tasks_router)
router.include_router(vision_router)
router.include_router(palace_router)
router.include_router(self_audit_router)
# Optional feature areas — only mount when enabled (default OFF in the shippable
# build). With the flag off the tool schemas are hidden too (tool_definitions),
# so neither the model nor the dashboard can reach these routes.
if shared.WORKOUTS_ENABLED:
    router.include_router(workout_router)
if shared.MEALS_ENABLED:
    router.include_router(meals_router)
if shared.PANTRY_ENABLED:
    router.include_router(pantry_router)
router.include_router(paperless_router)


@router.get("/health")
async def health(req: Request):
    """Health check endpoint. Minimal response without auth, full details with auth."""
    # Minimal response for unauthenticated requests (monitoring, uptime checks)
    # Same token source as BearerAuthMiddleware (settings also sees the
    # setup_env overlay, which os.environ may not).
    from orchestrator.config import settings

    if not bearer_token_matches(req.headers.get("authorization", ""), settings.api_token):
        return {"ok": True, "version": "7.0", "architecture": "unified"}

    # Full health details for authenticated requests
    scheduled_jobs = len(scheduler.get_jobs())

    # Shared health fields (calendar, RAG, HA, scheduler, focus, endel)
    cal_health = calendar_sources.health()
    common_fields = {
        "calendar": {
            **cal_health,
            "poll_interval_min": CALENDAR_POLL_INTERVAL if cal_health["configured"] else None,
            "morning_briefing": MORNING_BRIEFING_TIME
            if MORNING_BRIEFING_ENABLED and cal_health["configured"]
            else None,
        },
        "rag_collection": CHROMA_COLLECTION,
        "rag_docs": collection.count(),
        "ha_entities": len(ha_client._entities),
        "scheduler": {
            "running": scheduler.running,
            "scheduled_reminders": scheduled_jobs,
            "timezone": str(scheduler.timezone),
        },
        "focus_timer": {
            "active": current_focus_session["active"],
            "task": current_focus_session["task"],
            "remaining_minutes": (
                current_focus_session["duration"]
                - (datetime.now() - current_focus_session["started"]).total_seconds() / 60
            )
            if current_focus_session["active"]
            else None,
            "audio_player": current_focus_session.get("audio_player"),
        }
        if current_focus_session["active"]
        else {"active": False},
        "endel": {
            "enabled": ENDEL_ENABLED,
            "default_player": FOCUS_AUDIO_PLAYER,
            "modes": ENDEL_MODES,
        },
    }

    from orchestrator.tool_definitions import get_all_tools  # late import: avoids circular import at module load

    primary_online = await check_model_health()

    fallback_online = False
    try:
        resp = await shared._http.get(f"{FALLBACK_MODEL_URL}/models", timeout=3.0)
        fallback_online = resp.status_code == 200
    except Exception as e:
        logger.debug("Fallback model health check failed: %s", e)

    tool_names = [t["function"]["name"] for t in get_all_tools()]

    return {
        "ok": True,
        "version": "7.0",
        "architecture": "unified",
        "flow": "User → Orchestrator → Brain (conversation + tools) → User",
        "primary": f"{MODEL_URL} ({MODEL_NAME})",
        "primary_status": "online" if primary_online else "offline (auto-starts on demand)",
        "fallback": f"{FALLBACK_MODEL_URL} ({FALLBACK_MODEL_NAME})",
        "fallback_status": "online" if fallback_online else "offline",
        "tools": tool_names,
        **common_fields,
    }


@router.get("/metrics")
async def metrics_endpoint():
    """Prometheus metrics endpoint."""
    from starlette.responses import Response

    HELIOS_ONLINE.set(1 if await check_model_health() else 0)
    # Scrape fallback model health
    try:
        resp = await shared._http.get(f"{FALLBACK_MODEL_URL}/models", timeout=3.0)
        FALLBACK_ONLINE.set(1 if resp.status_code == 200 else 0)
    except Exception:
        FALLBACK_ONLINE.set(0)
    FOCUS_ACTIVE.set(1 if current_focus_session["active"] else 0)
    REMINDERS_PENDING.set(len(list_pending_reminders()))

    # Scrape temperature sensors for Prometheus
    try:
        # Labels match check_closet_temperature and the dashboards ("kitchen" is
        # the ambient sensor). Only real readings create a series — a missing
        # sensor must show "No data", not 0°F.
        readings = {}
        for location, entity_id in [("closet", profile.closet_temp_sensor), ("kitchen", profile.ambient_temp_sensor)]:
            resp = await shared._http.get(
                f"{HA_URL}/api/states/{entity_id}",
                headers={"Authorization": f"Bearer {HA_TOKEN}"},
                timeout=3.0,
            )
            if resp.status_code == 200:
                try:
                    readings[location] = float(resp.json()["state"])
                except (TypeError, ValueError):
                    continue  # "unavailable" / "unknown"
                TEMPERATURE_GAUGE.labels(location=location).set(readings[location])
        # No reading this scrape: drop the series so it doesn't freeze at the
        # last value (ClosetTemperatureSensorMissing keys on absent()).
        for location in ("closet", "kitchen"):
            if location not in readings:
                with contextlib.suppress(KeyError):
                    TEMPERATURE_GAUGE.remove(location)
        if "closet" in readings and "kitchen" in readings:
            TEMPERATURE_DELTA.set(readings["closet"] - readings["kitchen"])
    except Exception:
        pass  # Don't let temp scrape failures break metrics

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/v1/models")
def list_models():
    """List available models in OpenAI-compatible format."""
    return {
        "object": "list",
        "data": [
            {
                "id": profile.assistant_voice,
                "object": "model",
                "created": 1700000000,
                "owned_by": "brain-gateway",
                "name": f"{profile.assistant_name} (Hybrid)",
            },
            {
                "id": "brain",
                "object": "model",
                "created": 1700000000,
                "owned_by": "brain-gateway",
                "name": "Brain Gateway",
            },
        ],
    }


@router.get("/api/ha/entities")
async def list_ha_entities():
    """List the Home Assistant entities chat and the dashboard may control:
    listed domains only, denied entities (Helios plug, HA_TOOL_DENY_ENTITIES)
    and entities hidden in HA left out — same policy as ha_guard.check_ha_call.
    Each entity carries its HA area and entity category (from the registries,
    cached; null when unavailable) so the /home page can group by room."""
    await ha_client.refresh_entities(max_age_seconds=3)
    registry = await get_registry_meta(ha_client.url, ha_client.token)

    controllable = ha_client.get_all_controllable()
    shown = {
        domain: [
            e for e in entities if ha_entity_allowed(e.entity_id) and not registry.get(e.entity_id, {}).get("hidden")
        ]
        for domain, entities in controllable.items()
        if domain in HA_LISTED_DOMAINS
    }

    return {
        "total": sum(len(v) for v in shown.values()),
        "rooms_available": bool(registry),
        # False when HA couldn't be reached just now: states are the last known.
        "state_ok": ha_client.last_refresh_ok,
        "controllable": {
            domain: [entity_view(e, registry.get(e.entity_id, {})) for e in entities]
            for domain, entities in shown.items()
            if entities
        },
    }


@router.post("/api/ha/command")
async def execute_ha_command(body: HACommandRequest):
    """Run one Home Assistant command: natural-language ``command`` (testing)
    or a structured ``entity_id`` + ``service`` call (dashboard /home controls).
    Both are held to ha_guard.check_ha_call — the chat tool's policy."""
    if body.command is not None:
        result = await ha_client.execute_command(body.command)
    else:
        # The signed-in owner may change device settings (the /home "Device
        # settings" card); chat and the NLP path may not.
        refusal, data = check_ha_call(body.entity_id, body.service, body.data, allow_device_settings=True)
        if refusal:
            HA_CALL_REFUSED.labels(path="api").inc()
            # domain.service only once both are well-formed; never echo raw input.
            well_formed = re.fullmatch(r"[a-z_]+\.[a-z0-9_]{1,128}", body.entity_id or "") and re.fullmatch(
                r"[a-z_]{1,64}", body.service or ""
            )
            call = f"{body.entity_id.split('.', 1)[0]}.{body.service}" if well_formed else "malformed"
            logger.warning(f"[HA] API command refused: {call}")
            return error_response(refusal, status_code=403)
        # HA answers 200 for a service call on an entity it doesn't have, so an
        # unknown id would read as success. Re-check against a state list at
        # most 5 s old (a stream of bogus ids can't force a refresh each).
        if ha_client.get_entity(body.entity_id) is None:
            await ha_client.refresh_entities(max_age_seconds=5)
            if not ha_client.last_refresh_ok:
                return error_response("Home Assistant call failed", status_code=502)
            if ha_client.get_entity(body.entity_id) is None:
                return error_response("Unknown entity", status_code=404)
        result = await ha_client.call_service(body.entity_id, body.service, data)

    if body.command is None and not result.success:
        # call_service's message can carry HA's error body / internal URL.
        logger.warning(f"[HA] API command failed: {body.entity_id.split('.', 1)[0]}.{body.service}")
        return error_response("Home Assistant call failed", status_code=502)
    return {
        "ok": result.success,
        "action": result.action,
        "entity_id": result.entity_id,
        "message": result.message,
    }


@router.post("/api/memory/add")
async def add_memory(body: MemoryAddRequest):
    """Add a memory to RAG."""
    doc_id = f"{body.category}_{datetime.now().timestamp()}"

    metadata = {
        "category": body.category,
        "source": body.source,
        "kind": "chunk",
        "created_at": datetime.now().isoformat(),
    }
    if body.tags:
        metadata["tags"] = ",".join(str(t) for t in body.tags)

    collection.add(
        documents=[body.text.strip()],
        metadatas=[metadata],
        ids=[doc_id],
    )

    return {"ok": True, "id": doc_id}


@router.get("/api/memory/search")
async def search_memory_api(query: str, n: int = 5):
    """Search RAG memory."""
    context = await rag_context(query)
    return JSONResponse({"query": query, "results": context})


@router.get("/api/memory/stats")
def memory_stats():
    """Get RAG statistics."""
    return JSONResponse(
        {
            "collection": CHROMA_COLLECTION,
            "total_documents": collection.count(),
            "persist_path": CHROMA_PERSIST,
        }
    )


@router.post("/api/reminder/trigger")
async def trigger_reminder(body: ReminderTriggerRequest):
    """Manually trigger a reminder (for testing or legacy HA automation callbacks)."""
    logger.info(f"[REMINDER] Manual trigger: {body.reminder_id}")
    await deliver_reminder_job(body.reminder_id)
    return {"ok": True, "reminder_id": body.reminder_id}


@router.get("/api/reminders")
async def get_reminders_api():
    """Pending reminders with scheduler status, plus the last-24h delivery
    log (trust layer: delivered / missed / failed, with ack channel)."""
    from orchestrator import state_store as _ss

    pending = list_pending_reminders()

    scheduled_job_ids = {job.id for job in scheduler.get_jobs()}
    for reminder in pending:
        job_id = f"reminder_{reminder.get('id')}"
        reminder["scheduled"] = job_id in scheduled_job_ids
        # The card renders `time`; rows store `trigger_time` (pre-existing
        # mismatch that made the time column render "Invalid Date").
        reminder.setdefault("time", reminder.get("trigger_time"))

    recent = [
        {
            "id": r["id"],
            "text": r["text"],
            "time": r.get("trigger_time"),
            "status": r["status"],
            "completed_at": r.get("completed_at"),
            "acked_via": r.get("acked_via"),
            "snooze_count": r.get("snooze_count") or 0,
        }
        for r in _ss.get_recent_reminder_outcomes(hours=24)
    ]

    return JSONResponse(
        {
            "count": len(pending),
            "scheduler_jobs": len(scheduled_job_ids),
            "reminders": pending,
            "recent": recent,
        }
    )


@router.post("/api/reminder/complete/{reminder_id}")
async def complete_reminder_api(reminder_id: str):
    """Mark a reminder as completed (triggered)."""
    success = mark_reminder_completed(reminder_id)
    if success:
        return {"ok": True, "reminder_id": reminder_id}
    return JSONResponse({"ok": False, "error": "Reminder not found"}, status_code=404)


# ---------------------------------------------------------------------------
# F-011: ntfy feedback-loop callbacks
# ---------------------------------------------------------------------------
# These two routes are called by phones via ntfy action buttons and therefore
# run WITHOUT a Bearer token — they're HMAC-signature-gated instead. See
# BearerAuthMiddleware.PUBLIC_PREFIXES in orchestrator.py and the security
# model in jess-features/F-011-ntfy-feedback-loop.md.
#
# Method handling: POST is the original ntfy action-button shape. GET is
# added for Pushover's primary `url` field, which Safari opens as a tap.
# Both methods run the same HMAC-gated logic; the response is a tiny HTML
# page for GET (browser) and JSON for POST (machine clients / ntfy).


def _callback_response(
    request: Request,
    payload: Dict[str, Any],
    status_code: int = 200,
    html_headline: str = "Done",
    html_subtext: str = "You can close this tab.",
) -> Any:
    """Return JSON for POST, HTML for GET — so a Safari tap on a Pushover
    primary URL lands on a readable confirmation page instead of raw JSON.

    The HTML page is dark-theme, viewport-scaled, auto-closes nothing
    (Safari won't honor `window.close()` on a user-opened tab anyway).
    """
    from fastapi.responses import HTMLResponse

    if request.method == "GET":
        safe_headline = (html_headline or "").replace("<", "&lt;").replace(">", "&gt;")
        safe_subtext = (html_subtext or "").replace("<", "&lt;").replace(">", "&gt;")
        page = (
            "<!DOCTYPE html><html><head>"
            "<meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>Jess</title>"
            "<style>"
            "html,body{margin:0;padding:0;height:100%}"
            "body{background:#0b0d12;color:#eceef3;"
            "font:600 1.25rem system-ui,-apple-system,sans-serif;"
            "display:flex;flex-direction:column;align-items:center;"
            "justify-content:center;text-align:center;padding:2rem}"
            ".h{font-size:2.5rem;margin-bottom:0.5rem}"
            ".s{font-size:1rem;color:#8a94a8;font-weight:400}"
            "</style></head>"
            f"<body><div class='h'>{safe_headline}</div>"
            f"<div class='s'>{safe_subtext}</div></body></html>"
        )
        return HTMLResponse(page, status_code=status_code)
    # POST path preserves existing JSON contract.
    if status_code == 200:
        return payload
    return JSONResponse(payload, status_code=status_code)


# Accept both POST (ntfy HTTP action button) and GET (Pushover primary-url
# taps → Safari opens the URL, which is a GET). HMAC verification gates
# state mutation identically for both; the response shape differs — JSON
# for POST (machine callers), HTML for GET (browser).
@router.api_route("/api/reminder/ack/{reminder_id}", methods=["GET", "POST"])
async def ntfy_ack_reminder(request: Request, reminder_id: str, sig: str = "", exp: int = 0):
    """Ack a reminder from an ntfy (POST) or Pushover (GET) action. HMAC-gated, no bearer."""
    from orchestrator.config import settings
    from orchestrator.metrics import NTFY_ACK_TOTAL, NTFY_CALLBACK_REJECTED_TOTAL
    from orchestrator.reminder_manager import verify_callback_signature

    # Feature-flag gate: the callback surface should outlive EITHER the ntfy
    # push channel OR the pushover channel, since both send signed URLs that
    # ack/snooze. Only 404 if BOTH channels are off — otherwise e.g. running
    # pushover-only (ntfy disabled) would strand the Done/Snooze taps.
    # Return 404 (not 403) so we don't leak the route's existence to scanners
    # when every channel is off.
    if not (settings.ntfy_enabled or settings.pushover_enabled):
        return _callback_response(
            request,
            {"ok": False, "error": "disabled"},
            status_code=404,
            html_headline="Not available",
            html_subtext="Reminder feedback is off on this server.",
        )

    err = verify_callback_signature(reminder_id, "ack", exp, sig)
    if err:
        NTFY_CALLBACK_REJECTED_TOTAL.labels(reason=err).inc()
        # `expired` is an expected state (user didn't tap in time); don't
        # spam WARNING for it. `bad_signature` stays WARNING — that's a
        # real security signal.
        log_fn = logger.info if err == "expired" else logger.warning
        log_fn(f"[NTFY-ACK] Rejected {reminder_id[:64]!r}: {err}")  # unauthenticated input: capped + repr-escaped
        status = 410 if err == "expired" else 403
        headline = "Expired" if err == "expired" else "Invalid"
        subtext = (
            "This link is older than 30 minutes. The reminder didn't register."
            if err == "expired"
            else "Signature check failed. This link may have been tampered with."
        )
        return _callback_response(
            request,
            {"ok": False, "error": err},
            status_code=status,
            html_headline=headline,
            html_subtext=subtext,
        )

    outcome = await reminder_actions.ack_reminder(reminder_id, via="ntfy")
    if outcome["status"] == "not_found":
        NTFY_CALLBACK_REJECTED_TOTAL.labels(reason="not_found").inc()
        return _callback_response(
            request,
            {"ok": False, "error": "not_found"},
            status_code=404,
            html_headline="Not found",
            html_subtext="That reminder doesn't exist.",
        )

    if outcome["status"] == "already_acked":
        # Idempotent replay — the helper re-fires nothing; don't double-count.
        return _callback_response(
            request,
            {"ok": True, "already_acked": True, "reminder_id": reminder_id},
            html_headline="\u2713 Already done",
            html_subtext="You've acknowledged this one already.",
        )

    text = outcome["reminder"].get("text", "") or ""
    action = outcome["action"]
    NTFY_ACK_TOTAL.labels(inferred_action=action or "none").inc()

    # Visible-confirmation side-channel (F-011 follow-up). Fire-and-forget
    # so a slow/down ntfy server doesn't stretch the ack response time.
    # Gated by settings.ntfy_confirm_enabled; no-op when off.
    # Title stays generic ("Logged") — selfcare action category (medication,
    # meal, water, movement) is medically sensitive and the ntfy topic is
    # open-tailnet, so that detail lives in the body only. See F-011 security
    # review finding.
    from orchestrator.pushover_manager import deliver_pushover_confirm
    from orchestrator.reminder_manager import deliver_ack_confirm

    confirm_title = "\u2713 Logged"
    text_snippet = (text[:100] + "...") if len(text) > 100 else (text or "reminder")
    confirm_msg = f"{text_snippet}\n({action} logged)" if action else text_snippet
    _fire_and_forget(deliver_ack_confirm(confirm_title, confirm_msg, reminder_id))
    _fire_and_forget(deliver_pushover_confirm(confirm_title, confirm_msg, reminder_id))

    return _callback_response(
        request,
        {"ok": True, "reminder_id": reminder_id, "inferred_action": action},
        html_headline="\u2713 Done",
        html_subtext="Reminder acknowledged. You can close this tab.",
    )


@router.api_route("/api/reminder/snooze/{reminder_id}", methods=["GET", "POST"])
async def ntfy_snooze_reminder(request: Request, reminder_id: str, sig: str = "", exp: int = 0, minutes: int = 10):
    """Snooze a reminder from an ntfy (POST) or Pushover (GET) action.

    HMAC-gated, no bearer. Reschedules `deliver_reminder_job` `minutes`
    minutes from now and bumps snooze_count. Capped by
    `NTFY_MAX_SNOOZE_COUNT`. HTML response on GET so a Safari tap on a
    Pushover link lands on a readable confirmation page.
    """
    from orchestrator.config import settings
    from orchestrator.metrics import NTFY_CALLBACK_REJECTED_TOTAL, NTFY_SNOOZE_TOTAL
    from orchestrator.reminder_manager import verify_callback_signature

    # Feature-flag gate: same widened semantics as the ack route. Either push
    # channel keeps the signed callback URLs reachable.
    if not (settings.ntfy_enabled or settings.pushover_enabled):
        return _callback_response(
            request,
            {"ok": False, "error": "disabled"},
            status_code=404,
            html_headline="Not available",
            html_subtext="Reminder feedback is off on this server.",
        )

    # Clamp BEFORE verifying signature: signature binds the minutes value,
    # so we have to clamp first and check against the clamped value.
    if minutes < 1:
        minutes = 1
    if minutes > 120:
        minutes = 120

    err = verify_callback_signature(reminder_id, "snooze", exp, sig, extra=str(minutes))
    if err:
        NTFY_CALLBACK_REJECTED_TOTAL.labels(reason=err).inc()
        log_fn = logger.info if err == "expired" else logger.warning
        log_fn(f"[NTFY-SNOOZE] Rejected {reminder_id[:64]!r}: {err}")  # unauthenticated input: capped + repr-escaped
        status = 410 if err == "expired" else 403
        headline = "Expired" if err == "expired" else "Invalid"
        subtext = "This link is older than 30 minutes." if err == "expired" else "Signature check failed."
        return _callback_response(
            request,
            {"ok": False, "error": err},
            status_code=status,
            html_headline=headline,
            html_subtext=subtext,
        )

    outcome = await reminder_actions.snooze_reminder(reminder_id, minutes, via="ntfy")
    if outcome["status"] == "not_found":
        NTFY_CALLBACK_REJECTED_TOTAL.labels(reason="not_found").inc()
        return _callback_response(
            request,
            {"ok": False, "error": "not_found"},
            status_code=404,
            html_headline="Not found",
            html_subtext="That reminder doesn't exist.",
        )
    if outcome["status"] == "limit":
        current = outcome["snooze_count"]
        NTFY_CALLBACK_REJECTED_TOTAL.labels(reason="over_snoozed").inc()
        return _callback_response(
            request,
            {"ok": False, "error": "max_snoozes_reached", "snooze_count": current},
            status_code=409,
            html_headline="Snooze limit reached",
            html_subtext=f"You've snoozed this {current} times already.",
        )
    if outcome["status"] != "ok":
        return _callback_response(
            request,
            {"ok": False, "error": "reschedule_failed"},
            status_code=500,
            html_headline="Couldn't snooze",
            html_subtext="Something went wrong rescheduling. Check back later.",
        )

    run_at = outcome["run_at"]
    new_count = outcome["snooze_count"]
    NTFY_SNOOZE_TOTAL.inc()

    # Visible-confirmation side-channel (F-011 + F-013). Fire-and-forget on both.
    from orchestrator.pushover_manager import deliver_pushover_confirm
    from orchestrator.reminder_manager import deliver_ack_confirm

    fire_time = run_at.strftime("%-I:%M %p")
    confirm_title = f"\U0001f4a4 Snoozed until {fire_time}"
    confirm_msg = (
        f"{new_count}/{settings.ntfy_max_snooze_count} snoozes used" if new_count is not None else "Rescheduled"
    )
    _fire_and_forget(deliver_ack_confirm(confirm_title, confirm_msg, reminder_id))
    _fire_and_forget(deliver_pushover_confirm(confirm_title, confirm_msg, reminder_id))
    return _callback_response(
        request,
        {
            "ok": True,
            "reminder_id": reminder_id,
            "rescheduled_for": run_at.isoformat(),
            "snooze_count": new_count,
        },
        html_headline=f"\U0001f4a4 Snoozed until {fire_time}",
        html_subtext=f"I'll remind you again at {fire_time}. You can close this tab.",
    )


@router.get("/api/focus")
async def get_focus_status_api():
    """Get current focus timer status (for dashboards/widgets)."""
    if not current_focus_session["active"]:
        return JSONResponse(
            {
                "active": False,
                "task": None,
                "elapsed_minutes": None,
                "remaining_minutes": None,
                "duration": None,
                "break_duration": None,
                "started": None,
            }
        )

    elapsed = (datetime.now() - current_focus_session["started"]).total_seconds() / 60
    remaining = current_focus_session["duration"] - elapsed

    return JSONResponse(
        {
            "active": True,
            "task": current_focus_session["task"],
            "elapsed_minutes": round(elapsed, 1),
            "remaining_minutes": round(max(0, remaining), 1),
            "duration": current_focus_session["duration"],
            "break_duration": current_focus_session["break_duration"],
            "started": current_focus_session["started"].isoformat(),
        }
    )


@router.post("/api/focus/start")
async def start_focus_api(body: FocusStartRequest):
    """Start a focus timer via REST API."""
    result = await tool_start_focus(body.task, body.duration, body.break_duration, body.speaker, body.soundscape)
    return {
        "ok": current_focus_session["active"],
        "message": result,
        "task": body.task,
        "duration": body.duration,
        "break_duration": body.break_duration,
        "speaker": body.speaker,
        "soundscape": body.soundscape,
        "audio_player": current_focus_session.get("audio_player"),
    }


@router.post("/api/focus/stop")
async def stop_focus_api():
    """Stop the current focus timer via REST API."""
    result = await tool_stop_focus()
    return {"ok": True, "message": result}


@router.get("/api/audio/{filename}")
async def serve_audio(filename: str):
    """Serve audio files from /tmp/brain_audio/."""
    # Sanitize filename to prevent path traversal
    safe_name = os.path.basename(filename)
    if safe_name != filename or ".." in filename:
        return JSONResponse({"error": "Invalid filename"}, status_code=400)
    filepath = f"/tmp/brain_audio/{safe_name}"
    if os.path.exists(filepath):
        media_types = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg"}
        ext = os.path.splitext(safe_name)[1].lower()
        media_type = media_types.get(ext, "audio/wav")
        return FileResponse(filepath, media_type=media_type)
    return JSONResponse({"error": "Audio file not found"}, status_code=404)


@router.post("/api/announce")
async def announce_tts(body: AnnounceRequest):
    """Trigger a TTS announcement via the voice system (for dashboard milestones, etc.)."""
    try:
        result = await _announce_voice(body.text, speaker=body.speaker, announcement_type="manual")
        if result.get("suppressed"):
            return {"ok": True, "suppressed": True, "reason": result.get("reason")}
        if not result.get("success"):
            return JSONResponse(
                {"ok": False, "error": result.get("error"), "unavailable": result.get("unavailable", [])},
                status_code=502,
            )
        logger.info(f"[ANNOUNCE] TTS on {body.speaker or 'default'} ({len(body.text)} chars)")
        return {"ok": True, "text": body.text, "speaker": body.speaker or "default"}
    except Exception as e:
        logger.error(f"[ANNOUNCE] Failed: {e}")
        return JSONResponse({"ok": False, "error": "TTS announcement failed"}, status_code=500)


def _numeric_state(state: Any) -> Optional[float]:
    """An HA sensor state as a finite float, or None (unknown, unavailable, text, NaN)."""
    try:
        value = float(state)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


@router.get("/api/temperatures")
async def get_temperatures() -> dict:
    """Get temperature sensor readings from Home Assistant for dashboard widget."""
    sensors = {
        "closet": profile.closet_temp_sensor,
        "ambient": profile.ambient_temp_sensor,
    }
    readings = {}
    for label, entity_id in sensors.items():
        try:
            resp = await shared._http.get(
                f"{HA_URL}/api/states/{entity_id}",
                headers={"Authorization": f"Bearer {HA_TOKEN}"},
                timeout=5.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                value = _numeric_state(data.get("state"))
                if value is None:
                    readings[label] = {"temperature": None, "error": "No reading"}
                    continue
                readings[label] = {
                    "temperature": value,
                    "unit": data.get("attributes", {}).get("unit_of_measurement", "°F"),
                    "friendly_name": data.get("attributes", {}).get("friendly_name", label),
                }
            else:
                readings[label] = {"temperature": None, "error": f"HTTP {resp.status_code}"}
        except Exception as e:
            logger.error(f"[TEMP] Failed to read {label} sensor: {e}")
            readings[label] = {"temperature": None, "error": "Sensor read failed"}

    # Calculate delta if both readings available
    closet_temp = readings.get("closet", {}).get("temperature")
    ambient_temp = readings.get("ambient", {}).get("temperature")
    delta = round(closet_temp - ambient_temp, 1) if closet_temp is not None and ambient_temp is not None else None

    # Estimate monthly AC cost from server heat:
    # ~300W server heat → AC needs ~100W extra to remove it (COP ~3)
    # Scale proportionally with delta: baseline 5°F delta = 100W AC load
    # monthly_cost = (delta/5) * 0.1kW * 24h * 30d * $0.11/kWh
    monthly_cooling_cost = round((delta / 5.0) * 0.1 * 24 * 30 * 0.11, 2) if delta and delta > 0 else None

    # Same bands the closet announcements use (jobs_monitoring), so the card's
    # colours match what Jess actually warns about.
    warning, critical = profile.temp_warning, profile.temp_critical
    return {
        "ok": True,
        "sensors": readings,
        "delta": delta,
        "thresholds": {"warning": warning, "critical": max(warning, critical)},
        "estimated_monthly_cooling_cost": monthly_cooling_cost,
        "timestamp": datetime.now().isoformat(),
    }


# ---------------------------------------------------------------------------
# Selfcare state — what Jess has actually marked off in the database
# ---------------------------------------------------------------------------


@router.get("/api/selfcare/today")
async def get_selfcare_today_endpoint():
    """Return today's selfcare log + last-seen-ever, grouped by action.

    Lets the dashboard verify whether Jess actually recorded meds/meal/water/
    movement (and that the midnight reset is working).
    """
    from orchestrator.state_store import get_last_selfcare, get_selfcare_today

    tracked_actions = ("medication", "meal", "water", "movement")
    try:
        today_rows = get_selfcare_today()  # all actions, today only, DESC
        by_action: Dict[str, list] = {a: [] for a in tracked_actions}
        for row in today_rows:
            action = row.get("action")
            if action in by_action:
                by_action[action].append(
                    {
                        "logged_at": row.get("logged_at"),
                        "detail": row.get("detail"),
                    }
                )

        actions: Dict[str, Dict[str, Any]] = {}
        for action in tracked_actions:
            entries = by_action[action]
            last_ever = get_last_selfcare(action)
            actions[action] = {
                "logged_today": len(entries) > 0,
                "count_today": len(entries),
                "last_today": entries[0]["logged_at"] if entries else None,
                "last_ever": last_ever.isoformat() if last_ever else None,
                "entries": entries,
            }

        return {
            "ok": True,
            "as_of": datetime.now().isoformat(),
            "today_date": datetime.now().strftime("%Y-%m-%d"),
            "actions": actions,
        }
    except Exception as e:
        logger.error("[SELFCARE] /api/selfcare/today failed: %s", e, exc_info=True)
        return JSONResponse(
            {"ok": False, "error": "Selfcare read failed"},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Auto-Learn endpoints
# ---------------------------------------------------------------------------


@router.get("/api/memory/learned")
async def list_learned_facts(category: Optional[str] = None, limit: int = 100):
    """List auto-learned facts (decrypted)."""
    from orchestrator.auto_learn import get_learned_facts

    facts = get_learned_facts(category=category, limit=limit)
    return JSONResponse({"count": len(facts), "facts": facts})


@router.delete("/api/memory/learned/{doc_id}")
async def delete_learned_fact_api(doc_id: str):
    """Delete a single auto-learned fact."""
    from orchestrator.auto_learn import delete_learned_fact

    success = delete_learned_fact(doc_id)
    if success:
        return {"ok": True, "deleted": doc_id}
    return JSONResponse({"ok": False, "error": "Fact not found or delete failed"}, status_code=404)


@router.delete("/api/memory/learned")
async def wipe_learned_facts(confirm: bool = False):
    """Delete ALL auto-learned facts. Requires ?confirm=true."""
    if not confirm:
        return JSONResponse(
            {"ok": False, "error": "Pass ?confirm=true to wipe all learned facts"},
            status_code=400,
        )
    from orchestrator.auto_learn import delete_all_learned_facts

    count = delete_all_learned_facts()
    return {"ok": True, "deleted_count": count}


@router.get("/api/memory/learned/stats")
async def learned_facts_stats():
    """Get auto-learn statistics."""
    from orchestrator.auto_learn import get_learned_stats

    return JSONResponse(get_learned_stats())


@router.post("/api/memory/learned/toggle")
async def toggle_auto_learn():
    """Enable/disable auto-learn at runtime."""
    shared.AUTO_LEARN_ENABLED = not shared.AUTO_LEARN_ENABLED
    state = "enabled" if shared.AUTO_LEARN_ENABLED else "disabled"
    logger.info("[AUTO_LEARN] Toggled to %s", state)
    return {"ok": True, "auto_learn_enabled": shared.AUTO_LEARN_ENABLED}


# ---------------------------------------------------------------------------
# Progress Tracking (F-005)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Announcement History
# ---------------------------------------------------------------------------


@router.get("/api/announcements/history")
async def get_announcements_history(limit: int = 50, type: str = None):
    """Recent announcement history with speaker, success, and latency."""
    from orchestrator.state_store import get_announcement_history

    limit = max(1, min(limit, 500))
    history = get_announcement_history(limit=limit, announcement_type=type)
    return JSONResponse(history)


@router.get("/api/announcements/stats")
async def get_announcements_stats():
    """Announcement statistics: success rates, speaker breakdown, latency."""
    from orchestrator.state_store import get_announcement_stats

    return JSONResponse(get_announcement_stats())


@router.delete("/api/announcements/history")
async def clear_announcements_history():
    """Clear all announcement history."""
    from orchestrator.state_store import clear_announcements

    deleted = clear_announcements()
    logger.info(f"[ANNOUNCE] Cleared {deleted} announcement(s)")
    return JSONResponse({"ok": True, "deleted": deleted})


# ---------------------------------------------------------------------------
# Ambient Awareness (F-010)
# ---------------------------------------------------------------------------


@router.get("/api/ambient/status")
async def get_ambient_status_endpoint():
    """Aggregated ambient status for dashboard and LED."""
    from orchestrator.ambient_manager import get_ambient_status

    status = await get_ambient_status()
    return JSONResponse(status)


@router.get("/api/progress/today")
async def get_progress_today():
    """Today's progress stats."""
    from orchestrator.progress_tracker import get_today_stats

    return JSONResponse(get_today_stats())


@router.get("/api/progress/week")
async def get_progress_week():
    """This week's stats and trend vs prior week."""
    from orchestrator.progress_tracker import get_week_stats

    return JSONResponse(get_week_stats())


@router.get("/api/progress/streaks")
async def get_progress_streaks():
    """Active streaks."""
    from orchestrator.progress_tracker import get_streaks

    return JSONResponse(get_streaks())


# ---------------------------------------------------------------------------
# Presence awareness
# ---------------------------------------------------------------------------


@router.get("/api/presence/status")
async def presence_status():
    """Get current presence state (home/away, room, last motion)."""
    from orchestrator.presence_tracker import get_presence

    return JSONResponse(get_presence())


# ---------------------------------------------------------------------------
# Claude Code activity tracking
# ---------------------------------------------------------------------------


@router.post("/api/claude_code/turn")
async def log_claude_code_turn_endpoint(req: Request):
    """Receive a Claude Code turn from the Stop hook.

    Stores the turn in the rolling SQLite buffer so Jess and the code_agent
    can reference recent Claude Code activity.

    Owner-specific dev surface — gated behind JESS_ADVANCED (the
    `check_claude_activity` tool that consumes this is gated the same way), so a
    clean shippable build doesn't expose the Claude Code activity tracker.
    """
    if not shared.JESS_ADVANCED:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    from orchestrator.claude_code_tracker import log_turn_from_hook

    try:
        payload = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON payload"}, status_code=400)

    try:
        turn_id = log_turn_from_hook(payload)
        return JSONResponse({"ok": True, "id": turn_id})
    except Exception as e:
        logger.error("[CC_TRACKER] Failed to log turn: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/api/claude_code/recent")
async def get_recent_claude_code_activity(minutes: int = 120, limit: int = 20):
    """Fetch recent Claude Code turns for dashboards/debugging.

    Owner-specific dev surface — gated behind JESS_ADVANCED (see the POST
    sibling above)."""
    if not shared.JESS_ADVANCED:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    from orchestrator.state_store import get_claude_code_turns

    minutes = max(1, min(minutes, 10080))  # cap at 1 week
    limit = max(1, min(limit, 100))
    turns = get_claude_code_turns(since_minutes=minutes, limit=limit)
    return JSONResponse({"count": len(turns), "turns": turns})


@router.post("/api/rag/ingest")
async def trigger_rag_ingest():
    """Run the RAG source-file ingest immediately (bypasses the 2-min scheduler).

    Useful for testing and for one-off "I just edited a file and want it live
    right now" cases. Returns the same stats as the scheduled job.
    """
    import asyncio
    import time as _time

    from orchestrator.rag_ingest import _run_ingest_sync

    t0 = _time.time()
    stats = await asyncio.to_thread(_run_ingest_sync)
    return JSONResponse(
        {
            "ok": True,
            "elapsed_seconds": round(_time.time() - t0, 2),
            **stats,
        }
    )


# ---------------------------------------------------------------------------
# Service health
# ---------------------------------------------------------------------------


@router.get("/api/services")
async def get_services_status():
    """Service health status — shows which external services are reachable."""
    from orchestrator.service_registry import services

    return JSONResponse(services.status_summary())


# ---------------------------------------------------------------------------
# Self-audit (F-014) — manual trigger
# ---------------------------------------------------------------------------


@router.post("/api/self_audit/run", response_model=None)
async def trigger_self_audit():
    """Run the daily self-audit on demand. Bearer-gated (default for /api/*).

    Concurrency-protected via a module-level asyncio.Lock in
    `jobs_self_audit` — overlapping calls (cron + manual, or two manual)
    return `result="busy"` with HTTP 409 instead of stacking a second
    LLM call on the primary slot.
    """
    from orchestrator.jobs_self_audit import run_self_audit
    from orchestrator.schemas import SelfAuditRunResponse

    raw = await run_self_audit()
    payload = SelfAuditRunResponse(
        ok=raw.get("result") in {"ok", "partial", "skipped"},
        result=raw.get("result", "unknown"),
        clusters=raw.get("clusters", 0),
        severity_counts=raw.get("severity_counts", {}) or {},
        report_path=raw.get("report_path"),
        reason=raw.get("reason"),
        error=None if raw.get("result") in {"ok", "partial", "skipped"} else raw.get("reason"),
    )
    status_code = 409 if raw.get("result") == "busy" else 200
    return JSONResponse(payload.model_dump(), status_code=status_code)


# ---------------------------------------------------------------------------
# Helios wake-on-demand (PT-C) — manual power control via Home Assistant
# ---------------------------------------------------------------------------


@router.post("/api/helios/wake", response_model=None)
async def helios_wake():
    """Power Helios on (smart-plug turn_on via HA). Bearer-gated.

    Debounced inside `helios_power.wake_helios`; a 200 with
    `{"skipped": "debounced"}` means a recent wake is still in effect. Returns
    409 when the feature is disabled, 502 on an HA error.
    """
    from orchestrator.helios_power import wake_helios

    result = await wake_helios()
    if result.get("ok"):
        return JSONResponse(result, status_code=200)
    status_code = 409 if result.get("skipped") == "disabled" else 502
    return JSONResponse(result, status_code=status_code)


@router.post("/api/helios/sleep", response_model=None)
async def helios_sleep():
    """Power Helios off (smart-plug turn_off via HA — a hard cut). Bearer-gated.

    Returns 409 when the feature is disabled, 502 on an HA error.
    """
    from orchestrator.helios_power import sleep_helios

    result = await sleep_helios()
    if result.get("ok"):
        return JSONResponse(result, status_code=200)
    status_code = 409 if result.get("skipped") == "disabled" else 502
    return JSONResponse(result, status_code=status_code)


@router.get("/api/helios/power", response_model=None)
async def helios_power():
    """Read Helios plug switch state + power draw and infer running/asleep.

    Bearer-gated. Returns 409 when the feature is disabled, 502 on an HA error.
    """
    from orchestrator.helios_power import helios_power_status

    result = await helios_power_status()
    if result.get("ok"):
        return JSONResponse(result, status_code=200)
    status_code = 409 if result.get("skipped") == "disabled" else 502
    return JSONResponse(result, status_code=status_code)
