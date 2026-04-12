"""
Secondary REST API endpoints (health, metrics, memory, reminders, focus, audio, HA).

Domain-specific routes are split into separate modules and included as sub-routers:
- routes_calendar: /api/calendar/*, /api/email-to-calendar/*
- routes_chat: /api/chat/*
- routes_documents: /api/documents/*
- routes_shopping: /api/shopping/*
- routes_vision: /api/vision/*, /api/stt/*, /api/tts/*
"""

import logging
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from orchestrator import shared
from orchestrator.focus_manager import tool_start_focus, tool_stop_focus
from orchestrator.google_calendar import get_calendar_client
from orchestrator.metrics import (
    FALLBACK_ONLINE,
    FOCUS_ACTIVE,
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
from orchestrator.routes_shopping import router as shopping_router
from orchestrator.routes_vision import router as vision_router
from orchestrator.schemas import (
    AnnounceRequest,
    FocusStartRequest,
    HACommandRequest,
    MemoryAddRequest,
    ReminderTriggerRequest,
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

# Include domain-specific sub-routers
router.include_router(calendar_router)
router.include_router(chat_router)
router.include_router(documents_router)
router.include_router(shopping_router)
router.include_router(vision_router)


@router.get("/health")
async def health(req: Request):
    """Health check endpoint. Minimal response without auth, full details with auth."""
    # Minimal response for unauthenticated requests (monitoring, uptime checks)
    api_token = os.environ.get("API_TOKEN", "")
    auth = req.headers.get("authorization", "")
    if auth != f"Bearer {api_token}":
        return {"ok": True, "version": "7.0", "architecture": "unified"}

    # Full health details for authenticated requests
    scheduled_jobs = len(scheduler.get_jobs())

    # Shared health fields (calendar, RAG, HA, scheduler, focus, endel)
    cal_client = get_calendar_client()
    common_fields = {
        "calendar": {
            "configured": cal_client.is_configured,
            "poll_interval_min": CALENDAR_POLL_INTERVAL if cal_client.is_configured else None,
            "morning_briefing": MORNING_BRIEFING_TIME
            if MORNING_BRIEFING_ENABLED and cal_client.is_configured
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
        for location, entity_id in [("closet", profile.closet_temp_sensor), ("ambient", profile.ambient_temp_sensor)]:
            resp = await shared._http.get(
                f"{HA_URL}/api/states/{entity_id}",
                headers={"Authorization": f"Bearer {HA_TOKEN}"},
                timeout=3.0,
            )
            if resp.status_code == 200:
                temp = float(resp.json()["state"])
                TEMPERATURE_GAUGE.labels(location=location).set(temp)
        # Calculate delta
        closet = TEMPERATURE_GAUGE.labels(location="closet")._value.get()
        ambient = TEMPERATURE_GAUGE.labels(location="ambient")._value.get()
        if closet and ambient:
            TEMPERATURE_DELTA.set(closet - ambient)
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
    """List all discovered Home Assistant entities (debug endpoint)."""
    await ha_client.refresh_entities()

    controllable = ha_client.get_all_controllable()

    return {
        "total": len(ha_client._entities),
        "controllable": {
            domain: [{"entity_id": e.entity_id, "friendly_name": e.friendly_name, "state": e.state} for e in entities]
            for domain, entities in controllable.items()
        },
    }


@router.post("/api/ha/command")
async def execute_ha_command(body: HACommandRequest):
    """Execute a Home Assistant command directly (for testing)."""
    result = await ha_client.execute_command(body.command)

    return {
        "ok": result.success,
        "action": result.action,
        "entity_id": result.entity_id,
        "message": result.message,
        "details": result.details,
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
    context = rag_context(query)
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
    """List all pending reminders with scheduler status."""
    pending = list_pending_reminders()

    scheduled_job_ids = {job.id for job in scheduler.get_jobs()}
    for reminder in pending:
        job_id = f"reminder_{reminder.get('id')}"
        reminder["scheduled"] = job_id in scheduled_job_ids

    return JSONResponse({"count": len(pending), "scheduler_jobs": len(scheduled_job_ids), "reminders": pending})


@router.post("/api/reminder/complete/{reminder_id}")
async def complete_reminder_api(reminder_id: str):
    """Mark a reminder as completed (triggered)."""
    success = mark_reminder_completed(reminder_id)
    if success:
        return {"ok": True, "reminder_id": reminder_id}
    return JSONResponse({"ok": False, "error": "Reminder not found"}, status_code=404)


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
        logger.info(f"[ANNOUNCE] TTS on {body.speaker or 'default'}: {body.text[:80]}")
        return {"ok": True, "text": body.text, "speaker": body.speaker or "default"}
    except Exception as e:
        logger.error(f"[ANNOUNCE] Failed: {e}")
        return JSONResponse({"ok": False, "error": "TTS announcement failed"}, status_code=500)


@router.get("/api/temperatures")
async def get_temperatures():
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
                readings[label] = {
                    "temperature": float(data["state"]),
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
    delta = round(closet_temp - ambient_temp, 1) if closet_temp and ambient_temp else None

    # Estimate monthly AC cost from server heat:
    # ~300W server heat → AC needs ~100W extra to remove it (COP ~3)
    # Scale proportionally with delta: baseline 5°F delta = 100W AC load
    # monthly_cost = (delta/5) * 0.1kW * 24h * 30d * $0.11/kWh
    monthly_cooling_cost = round((delta / 5.0) * 0.1 * 24 * 30 * 0.11, 2) if delta and delta > 0 else None

    return {
        "sensors": readings,
        "delta": delta,
        "estimated_monthly_cooling_cost": monthly_cooling_cost,
        "timestamp": datetime.now().isoformat(),
    }


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
# Service health
# ---------------------------------------------------------------------------


@router.get("/api/services")
async def get_services_status():
    """Service health status — shows which external services are reachable."""
    from orchestrator.service_registry import services

    return JSONResponse(services.status_summary())
