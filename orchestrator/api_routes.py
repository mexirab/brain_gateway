"""
Secondary REST API endpoints (health, metrics, memory, reminders, focus, audio, HA).
"""

import logging
import os
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

import shared
from focus_manager import tool_start_focus, tool_stop_focus
from google_calendar import get_calendar_client
from metrics import (
    FALLBACK_ONLINE,
    FOCUS_ACTIVE,
    HELIOS_ONLINE,
    REMINDERS_PENDING,
    TEMPERATURE_DELTA,
    TEMPERATURE_GAUGE,
)
from model_manager import check_model_health
from prompt_builder import rag_context
from reminder_manager import _announce_voice, list_pending_reminders, mark_reminder_completed
from schemas import (
    AnnounceRequest,
    FocusStartRequest,
    HACommandRequest,
    MemoryAddRequest,
    ReminderTriggerRequest,
)
from shared import (
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
from tool_handlers import deliver_reminder_job

logger = logging.getLogger(__name__)

router = APIRouter()


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

    from tool_definitions import get_all_tools  # late import: avoids circular import at module load

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


@router.post("/api/email-to-calendar/run")
async def run_email_to_calendar():
    """Manually trigger email-to-calendar extraction."""
    from background_jobs import process_emails_for_events

    try:
        await process_emails_for_events()
        return {"ok": True, "message": "Email-to-calendar scan completed"}
    except Exception as e:
        logger.error(f"[EMAIL_TO_CAL] Manual trigger error: {e}")
        return JSONResponse({"error": "Email-to-calendar scan failed"}, status_code=500)


@router.post("/api/announce")
async def announce_tts(body: AnnounceRequest):
    """Trigger a TTS announcement via the voice system (for dashboard milestones, etc.)."""
    try:
        await _announce_voice(body.text, speaker=body.speaker)
        logger.info(f"[ANNOUNCE] TTS on {body.speaker or 'default'}: {body.text[:80]}")
        return {"ok": True, "text": body.text, "speaker": body.speaker or "default"}
    except Exception as e:
        logger.error(f"[ANNOUNCE] Failed: {e}")
        return JSONResponse({"ok": False, "error": "TTS announcement failed"}, status_code=500)


@router.get("/api/calendar/today")
async def calendar_today():
    """Get today's calendar events for the dashboard.

    Merges events from two sources:
    1. Phone calendar sync (iPhone Shortcut — Outlook + Google + iCloud)
    2. Google Calendar API (fallback if phone sync is stale/missing)

    Phone sync is preferred when fresh (<24h old) since it aggregates all
    iPhone calendars. Google Calendar is used as fallback. Events are
    deduplicated by title + start time to avoid duplicates when Google
    events appear in both sources.
    """
    import re
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(profile.timezone)
    today = datetime.now(tz).date()
    merged: list[dict] = []
    seen: set[str] = set()  # "title|start_iso" for dedup
    source = "none"

    def _parse_phone_datetime(s: str) -> datetime:
        """Parse date strings from iPhone Shortcuts.

        Handles formats like:
        - "Mar 4, 2026 at 10:00\u202fAM"  (narrow no-break space before AM/PM)
        - "Mar 4, 2026 at 1:00 PM"         (regular space)
        - "2026-03-04T10:00:00"            (ISO format)
        """
        if not s:
            raise ValueError("empty date string")
        # Try ISO first
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            pass
        # Normalize unicode spaces and "at" keyword
        cleaned = s.replace("\u202f", " ").replace("\u00a0", " ").replace(" at ", " ")
        # Remove extra whitespace
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        # Try common iOS Shortcut formats
        for fmt in (
            "%b %d, %Y %I:%M %p",  # "Mar 4, 2026 1:00 PM"
            "%B %d, %Y %I:%M %p",  # "March 4, 2026 1:00 PM"
            "%m/%d/%Y %I:%M %p",  # "03/04/2026 1:00 PM"
            "%b %d, %Y",  # "Mar 4, 2026" (all-day)
        ):
            try:
                return datetime.strptime(cleaned, fmt)
            except ValueError:
                continue
        raise ValueError(f"unrecognized date format: {s!r}")

    # Source 1: Phone calendar sync (has ALL calendars)
    phone_age = time.time() - shared._phone_calendar_sync_time if shared._phone_calendar_sync_time > 0 else float("inf")
    if shared._phone_calendar_events and phone_age < 86400:
        source = "phone"
        for ev in shared._phone_calendar_events:
            try:
                start_str = ev.get("start", "")
                start = _parse_phone_datetime(start_str)
                if start.tzinfo is None:
                    start = start.replace(tzinfo=tz)
                if start.date() != today:
                    continue

                end_str = ev.get("end", "")
                end = None
                if end_str:
                    try:
                        end = _parse_phone_datetime(end_str)
                        if end.tzinfo is None:
                            end = end.replace(tzinfo=tz)
                    except ValueError:
                        pass

                title = ev.get("title", "(No title)")
                dedup_key = f"{title.lower().strip()}|{start.isoformat()}"
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                # Handle trailing-space key from iOS ("calendar " vs "calendar")
                cal_name = ev.get("calendar") or ev.get("calendar ") or ""

                merged.append(
                    {
                        "id": ev.get("id", f"phone_{len(merged)}"),
                        "title": title.strip(),
                        "start": start.isoformat(),
                        "end": end.isoformat() if end else start.isoformat(),
                        "location": ev.get("location") or None,
                        "description": ev.get("description") or None,
                        "all_day": ev.get("all_day", False),
                        "calendar": cal_name.strip(),
                        "source": "phone",
                    }
                )
            except (ValueError, TypeError) as exc:
                logger.warning(f"[CALENDAR] Skipping phone event: {exc} — raw: {ev}")
                continue
    else:
        # Source 2: Google Calendar API (fallback)
        source = "google"
        cal = get_calendar_client()
        if cal and cal.is_configured:
            result = await cal.list_events(days_ahead=1)
            if result.success:
                for e in result.events:
                    title = e.title
                    dedup_key = f"{title.lower()}|{e.start.isoformat()}"
                    seen.add(dedup_key)
                    merged.append(
                        {
                            "id": e.id,
                            "title": title,
                            "start": e.start.isoformat(),
                            "end": e.end.isoformat(),
                            "location": e.location or None,
                            "description": e.description or None,
                            "all_day": e.all_day,
                            "calendar": "Google",
                            "source": "google",
                        }
                    )

    # Sort by start time (all-day events first, then by time)
    merged.sort(key=lambda e: (0 if e.get("all_day") else 1, e["start"]))

    return {"events": merged, "source": source, "count": len(merged)}


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
    from auto_learn import get_learned_facts

    facts = get_learned_facts(category=category, limit=limit)
    return JSONResponse({"count": len(facts), "facts": facts})


@router.delete("/api/memory/learned/{doc_id}")
async def delete_learned_fact_api(doc_id: str):
    """Delete a single auto-learned fact."""
    from auto_learn import delete_learned_fact

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
    from auto_learn import delete_all_learned_facts

    count = delete_all_learned_facts()
    return {"ok": True, "deleted_count": count}


@router.get("/api/memory/learned/stats")
async def learned_facts_stats():
    """Get auto-learn statistics."""
    from auto_learn import get_learned_stats

    return JSONResponse(get_learned_stats())


@router.post("/api/memory/learned/toggle")
async def toggle_auto_learn():
    """Enable/disable auto-learn at runtime."""
    shared.AUTO_LEARN_ENABLED = not shared.AUTO_LEARN_ENABLED
    state = "enabled" if shared.AUTO_LEARN_ENABLED else "disabled"
    logger.info("[AUTO_LEARN] Toggled to %s", state)
    return {"ok": True, "auto_learn_enabled": shared.AUTO_LEARN_ENABLED}


@router.api_route("/api/calendar/sync", methods=["GET", "POST", "PUT"])
async def sync_phone_calendar(req: Request):
    """Receive consolidated calendar events from iPhone Shortcut, or return status.

    GET: Returns sync status (last sync time, event count).
    POST/PUT: Receives calendar events from iPhone Shortcut.

    Accepts multiple body formats for flexibility with iOS Shortcuts:
    1. {"events": [...]}           — wrapped in events key
    2. [...]                       — bare list at top level
    3. {"events": {"0": {...}}}    — iOS dict-of-dicts (auto-converted)
    4. {"events": {single event}}  — single event dict (auto-wrapped)
    """
    # GET or no body → return status
    if req.method == "GET":
        sync_age = ""
        if shared._phone_calendar_sync_time > 0:
            age_min = int((time.time() - shared._phone_calendar_sync_time) / 60)
            sync_age = f"{age_min} minutes ago"
        else:
            sync_age = "never"
        return {
            "synced": shared._phone_calendar_sync_time > 0,
            "last_sync": sync_age,
            "event_count": len(shared._phone_calendar_events),
        }

    # POST/PUT → receive events
    # iOS Shortcuts sends one event per request inside a Repeat loop.
    # Accumulate events arriving within 60s as a single batch.
    try:
        raw_body = await req.body()
        if not raw_body:
            return JSONResponse({"error": "empty body"}, status_code=400)

        body = await req.json()

        # Normalize: accept multiple input shapes from iOS Shortcuts
        if isinstance(body, list):
            events = body
        elif isinstance(body, dict):
            events = body.get("events", body)
            if isinstance(events, dict):
                if all(isinstance(v, dict) for v in events.values()):
                    events = list(events.values())
                else:
                    events = [events]
        else:
            return JSONResponse({"error": f"unexpected body type: {type(body).__name__}"}, status_code=400)

        if not isinstance(events, list):
            events = [events]

        # If last sync was >60s ago, start a new batch; otherwise append
        now = time.time()
        if now - shared._phone_calendar_sync_time > 60:
            shared._phone_calendar_events = events
            logger.info(f"[PHONE_SYNC] New batch started with {len(events)} event(s)")
        else:
            shared._phone_calendar_events.extend(events)
            logger.info(f"[PHONE_SYNC] Appended {len(events)} event(s), total now {len(shared._phone_calendar_events)}")

        shared._phone_calendar_sync_time = now
        shared._save_phone_calendar()

        return {
            "ok": True,
            "events_received": len(shared._phone_calendar_events),
            "message": f"Synced {len(shared._phone_calendar_events)} calendar events",
        }
    except Exception as e:
        logger.error(f"[PHONE_SYNC] Error: {e}")
        return JSONResponse({"error": "Calendar sync failed"}, status_code=500)
