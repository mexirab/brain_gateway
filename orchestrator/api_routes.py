"""
Secondary REST API endpoints (health, metrics, memory, reminders, focus, audio, HA).
"""

import os
import time
import logging
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, FileResponse

from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

import shared
from shared import (
    ha_client, scheduler, current_focus_session, collection,
    HELIOS_URL, HELIOS_MODEL, NEMOTRON_URL, NEMOTRON_MODEL,
    CHROMA_COLLECTION, CHROMA_PERSIST,
    ENDEL_ENABLED, FOCUS_AUDIO_PLAYER, ENDEL_MODES,
    CALENDAR_POLL_INTERVAL, MORNING_BRIEFING_TIME, MORNING_BRIEFING_ENABLED,
)
from prompt_builder import rag_context
from helios_manager import check_helios_health
from focus_manager import tool_start_focus, tool_stop_focus
from tool_handlers import deliver_reminder_job
from google_calendar import get_calendar_client
from reminder_manager import list_pending_reminders, mark_reminder_completed
from metrics import (
    HELIOS_ONLINE, FOCUS_ACTIVE, REMINDERS_PENDING, HELIOS_IDLE_SECONDS,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health():
    """Health check endpoint."""
    helios_online = await check_helios_health()

    scheduled_jobs = len(scheduler.get_jobs())

    idle_timeout = int(os.environ.get("HELIOS_IDLE_TIMEOUT", 1800))
    if shared._last_helios_request > 0:
        idle_time = int(time.time() - shared._last_helios_request)
        idle_info = f"{idle_time}s (timeout: {idle_timeout}s)"
    else:
        idle_info = "no requests yet"

    return {
        "ok": True,
        "version": "6.2",
        "architecture": "hybrid",
        "flow": "User → Helios (conversation) → Nemotron (tools) → Helios → User",
        "primary": f"{HELIOS_URL} ({HELIOS_MODEL})",
        "primary_status": "online" if helios_online else "offline (auto-starts on demand)",
        "helios_idle": idle_info,
        "orchestrator": f"{NEMOTRON_URL} ({NEMOTRON_MODEL})",
        "helios_tools": ["ask_orchestrator"],
        "nemotron_tools": ["home_assistant", "search_memory", "update_data", "set_reminder", "cancel_reminder", "start_focus", "stop_focus", "focus_status", "check_calendar", "create_calendar_event"],
        "calendar": {
            "configured": get_calendar_client().is_configured,
            "poll_interval_min": CALENDAR_POLL_INTERVAL if get_calendar_client().is_configured else None,
            "morning_briefing": MORNING_BRIEFING_TIME if MORNING_BRIEFING_ENABLED and get_calendar_client().is_configured else None,
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
                current_focus_session["duration"] -
                (datetime.now() - current_focus_session["started"]).total_seconds() / 60
            ) if current_focus_session["active"] else None,
            "audio_player": current_focus_session.get("audio_player"),
        } if current_focus_session["active"] else {"active": False},
        "endel": {
            "enabled": ENDEL_ENABLED,
            "default_player": FOCUS_AUDIO_PLAYER,
            "modes": ENDEL_MODES,
        },
    }


@router.get("/metrics")
async def metrics_endpoint():
    """Prometheus metrics endpoint."""
    from starlette.responses import Response
    HELIOS_ONLINE.set(1 if await check_helios_health() else 0)
    FOCUS_ACTIVE.set(1 if current_focus_session["active"] else 0)
    REMINDERS_PENDING.set(len(list_pending_reminders()))
    if shared._last_helios_request > 0:
        HELIOS_IDLE_SECONDS.set(time.time() - shared._last_helios_request)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/v1/models")
def list_models():
    """List available models in OpenAI-compatible format."""
    return {
        "object": "list",
        "data": [
            {
                "id": "jessica",
                "object": "model",
                "created": 1700000000,
                "owned_by": "brain-gateway",
                "name": "Jessica (Hybrid)",
            },
            {
                "id": "brain",
                "object": "model",
                "created": 1700000000,
                "owned_by": "brain-gateway",
                "name": "Brain Gateway",
            },
        ]
    }


@router.get("/api/ha/entities")
async def list_ha_entities():
    """List all discovered Home Assistant entities (debug endpoint)."""
    await ha_client.refresh_entities()

    controllable = ha_client.get_all_controllable()

    return {
        "total": len(ha_client._entities),
        "controllable": {
            domain: [
                {"entity_id": e.entity_id, "friendly_name": e.friendly_name, "state": e.state}
                for e in entities
            ]
            for domain, entities in controllable.items()
        }
    }


@router.post("/api/ha/command")
async def execute_ha_command(req: Request):
    """Execute a Home Assistant command directly (for testing)."""
    body = await req.json()
    command = body.get("command", "")

    if not command:
        return JSONResponse({"error": "No command provided"}, status_code=400)

    result = await ha_client.execute_command(command)

    return {
        "success": result.success,
        "action": result.action,
        "entity_id": result.entity_id,
        "message": result.message,
        "details": result.details,
    }


@router.post("/api/memory/add")
async def add_memory(req: Request):
    """Add a memory to RAG."""
    body = await req.json()
    text = body.get("text", "").strip()
    category = body.get("category", "general")
    source = body.get("source", "manual")
    tags = body.get("tags", [])

    if not text:
        return JSONResponse({"error": "No text provided"}, status_code=400)

    doc_id = f"{category}_{datetime.now().timestamp()}"

    metadata = {
        "category": category,
        "source": source,
        "kind": "chunk",
        "created_at": datetime.now().isoformat(),
    }
    if tags and isinstance(tags, list):
        metadata["tags"] = ",".join(str(t) for t in tags)

    collection.add(
        documents=[text],
        metadatas=[metadata],
        ids=[doc_id],
    )

    return JSONResponse({"ok": True, "id": doc_id})


@router.get("/api/memory/search")
async def search_memory_api(query: str, n: int = 5):
    """Search RAG memory."""
    context = rag_context(query)
    return JSONResponse({"query": query, "results": context})


@router.get("/api/memory/stats")
def memory_stats():
    """Get RAG statistics."""
    return JSONResponse({
        "collection": CHROMA_COLLECTION,
        "total_documents": collection.count(),
        "persist_path": CHROMA_PERSIST,
    })


@router.post("/api/reminder/trigger")
async def trigger_reminder(req: Request):
    """Manually trigger a reminder (for testing or legacy HA automation callbacks)."""
    try:
        body = await req.json()
    except:
        body = {}

    reminder_id = body.get("reminder_id")
    if not reminder_id:
        return JSONResponse({"error": "Missing reminder_id"}, status_code=400)

    logger.info(f"[REMINDER] Manual trigger: {reminder_id}")

    await deliver_reminder_job(reminder_id)

    return JSONResponse({"success": True, "reminder_id": reminder_id})


@router.get("/api/reminders")
async def get_reminders_api():
    """List all pending reminders with scheduler status."""
    pending = list_pending_reminders()

    scheduled_job_ids = {job.id for job in scheduler.get_jobs()}
    for reminder in pending:
        job_id = f"reminder_{reminder.get('id')}"
        reminder["scheduled"] = job_id in scheduled_job_ids

    return JSONResponse({
        "count": len(pending),
        "scheduler_jobs": len(scheduled_job_ids),
        "reminders": pending
    })


@router.post("/api/reminder/complete/{reminder_id}")
async def complete_reminder_api(reminder_id: str):
    """Mark a reminder as completed (triggered)."""
    success = mark_reminder_completed(reminder_id)
    if success:
        return JSONResponse({"success": True, "reminder_id": reminder_id})
    return JSONResponse({"error": "Reminder not found"}, status_code=404)


@router.get("/api/focus")
async def get_focus_status_api():
    """Get current focus timer status (for dashboards/widgets)."""
    if not current_focus_session["active"]:
        return JSONResponse({
            "active": False,
            "task": None,
            "elapsed_minutes": None,
            "remaining_minutes": None,
            "duration": None,
            "break_duration": None,
            "started": None,
        })

    elapsed = (datetime.now() - current_focus_session["started"]).total_seconds() / 60
    remaining = current_focus_session["duration"] - elapsed

    return JSONResponse({
        "active": True,
        "task": current_focus_session["task"],
        "elapsed_minutes": round(elapsed, 1),
        "remaining_minutes": round(max(0, remaining), 1),
        "duration": current_focus_session["duration"],
        "break_duration": current_focus_session["break_duration"],
        "started": current_focus_session["started"].isoformat(),
    })


@router.post("/api/focus/start")
async def start_focus_api(req: Request):
    """Start a focus timer via REST API."""
    try:
        body = await req.json()
    except:
        body = {}

    task = body.get("task", "focus session")
    duration = body.get("duration", 25)
    break_duration = body.get("break_duration", 5)
    speaker = body.get("speaker")
    soundscape = body.get("soundscape", "focus")

    result = await tool_start_focus(task, duration, break_duration, speaker, soundscape)
    return JSONResponse({
        "success": current_focus_session["active"],
        "message": result,
        "task": task,
        "duration": duration,
        "break_duration": break_duration,
        "speaker": speaker,
        "soundscape": soundscape,
        "audio_player": current_focus_session.get("audio_player"),
    })


@router.post("/api/focus/stop")
async def stop_focus_api():
    """Stop the current focus timer via REST API."""
    result = await tool_stop_focus()
    return JSONResponse({
        "success": True,
        "message": result,
    })


@router.get("/api/audio/{filename}")
async def serve_audio(filename: str):
    """Serve audio files from /tmp/brain_audio/."""
    filepath = f"/tmp/brain_audio/{filename}"
    if os.path.exists(filepath):
        return FileResponse(filepath, media_type="audio/wav")
    return JSONResponse({"error": "Audio file not found"}, status_code=404)
