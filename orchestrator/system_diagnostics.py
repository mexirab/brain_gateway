"""
System diagnostics for Jess self-diagnosis.

Provides the check_system tool handler that lets Jess answer questions like
"did you send me my morning briefing?" by querying logs, state, scheduler,
and metrics — all in-process, no Docker socket needed.
"""

import logging
from datetime import datetime
from typing import Optional

from orchestrator import shared, state_store
from orchestrator.log_buffer import log_ring

logger = logging.getLogger(__name__)


async def check_system(query: str) -> str:
    """
    Main dispatcher for system self-diagnosis queries.

    Called by the primary model tool loop when user asks about system behavior.
    """
    handlers = {
        "morning_briefing": _check_morning_briefing,
        "calendar_poll": _check_calendar_poll,
        "reminders": _check_reminders,
        "focus_timer": _check_focus_timer,
        "temperature": _check_temperature,
        "system_health": _check_system_health,
        "recent_errors": _check_recent_errors,
    }

    handler = handlers.get(query, _check_system_health)
    try:
        return await handler()
    except Exception as e:
        logger.error(f"[DIAGNOSTICS] Error in check_system({query}): {e}")
        return f"Could not check {query}: internal error."


async def _check_morning_briefing() -> str:
    """Check if the morning briefing ran today."""
    entries = log_ring.search("[MORNING_BRIEFING]", limit=10)

    # Check scheduler for next run
    scheduler_info = _get_scheduler_job("morning_briefing")

    if not entries:
        lines = ["No morning briefing activity found in recent logs."]
        if scheduler_info:
            lines.append(f"Next scheduled run: {scheduler_info}")
        else:
            lines.append("Morning briefing job is not scheduled.")
        return "\n".join(lines)

    lines = ["Morning briefing log entries (most recent first):"]
    for e in entries[:5]:
        lines.append(f"  [{e['time']}] {e['message'][:150]}")

    if scheduler_info:
        lines.append(f"\nNext scheduled run: {scheduler_info}")

    return "\n".join(lines)


async def _check_calendar_poll() -> str:
    """Check recent calendar polling activity."""
    entries = log_ring.search("[CALENDAR_POLL]", limit=10)

    scheduler_info = _get_scheduler_job("calendar_poll")

    if not entries:
        lines = ["No calendar polling activity found in recent logs."]
        if scheduler_info:
            lines.append(f"Next poll: {scheduler_info}")
        return "\n".join(lines)

    lines = ["Calendar polling log entries (most recent first):"]
    for e in entries[:5]:
        lines.append(f"  [{e['time']}] {e['message'][:150]}")

    if scheduler_info:
        lines.append(f"\nNext poll: {scheduler_info}")

    return "\n".join(lines)


async def _check_reminders() -> str:
    """Check pending reminders and recent reminder activity."""
    pending = state_store.get_pending_reminders()

    lines = []
    if pending:
        lines.append(f"{len(pending)} pending reminder(s):")
        for rem in pending:
            lines.append(f"  - '{rem['text']}' at {rem['trigger_time']} (id: {rem['id']})")
    else:
        lines.append("No pending reminders.")

    # Check recent reminder log activity
    entries = log_ring.search("[REMINDER]", limit=5)
    if entries:
        lines.append("\nRecent reminder activity:")
        for e in entries[:3]:
            lines.append(f"  [{e['time']}] {e['message'][:150]}")

    return "\n".join(lines)


async def _check_focus_timer() -> str:
    """Check current or recent focus timer status."""
    session = shared.current_focus_session

    if session.get("active"):
        elapsed = (datetime.now() - session["started"]).total_seconds() / 60
        remaining = session["duration"] - elapsed
        return (
            f"Active focus session: '{session['task']}'\n"
            f"Started: {session['started'].strftime('%I:%M %p')}\n"
            f"Duration: {session['duration']} min\n"
            f"Elapsed: {elapsed:.0f} min, Remaining: {remaining:.0f} min\n"
            f"Audio: {'playing' if session.get('audio_player') else 'off'}\n"
            f"Site blocking: {'on' if session.get('block_sites') else 'off'}"
        )

    # No active session — check recent log activity
    entries = log_ring.search("[FOCUS]", limit=5)
    if entries:
        lines = ["No active focus session. Recent focus activity:"]
        for e in entries[:3]:
            lines.append(f"  [{e['time']}] {e['message'][:150]}")
        return "\n".join(lines)

    return "No active focus session and no recent focus activity."


async def _check_temperature() -> str:
    """Check temperature sensor readings and alert history."""
    # Query HA sensors directly
    closet_temp = None
    kitchen_temp = None

    for sensor in shared.ha_client.get_entities_by_domain("sensor"):
        if sensor.entity_id == "sensor.closet_temperature":
            closet_temp = sensor.state
        elif sensor.entity_id == "sensor.kitchen_temperature":
            kitchen_temp = sensor.state

    lines = []
    if closet_temp is not None:
        lines.append(f"Server closet: {closet_temp}°F")
    if kitchen_temp is not None:
        lines.append(f"Kitchen ambient: {kitchen_temp}°F")
    if closet_temp and kitchen_temp:
        try:
            delta = float(closet_temp) - float(kitchen_temp)
            lines.append(f"Heat delta: {delta:+.1f}°F")
        except (ValueError, TypeError):
            pass

    # Check recent temperature alerts
    entries = log_ring.search("[TEMP_ALERT]", limit=5)
    if entries:
        lines.append("\nRecent temperature alerts:")
        for e in entries[:3]:
            lines.append(f"  [{e['time']}] {e['message'][:150]}")
    else:
        lines.append("\nNo temperature alerts in recent logs.")

    return "\n".join(lines) if lines else "Could not read temperature sensors."


async def _check_system_health() -> str:
    """Check overall system health: models, scheduler, entities."""
    lines = ["System Health Report:"]

    # Primary model
    try:
        resp = await shared._http.get(f"{shared.MODEL_URL}/models", timeout=5)
        lines.append(
            f"  Primary model ({shared.MODEL_NAME}): "
            f"{'online' if resp.status_code == 200 else f'error ({resp.status_code})'}"
        )
    except Exception:
        lines.append(f"  Primary model ({shared.MODEL_NAME}): offline")

    # Fallback model
    if shared.FALLBACK_MODEL_URL:
        try:
            resp = await shared._http.get(f"{shared.FALLBACK_MODEL_URL}/models", timeout=5)
            lines.append(
                f"  Fallback model ({shared.FALLBACK_MODEL_NAME}): "
                f"{'online' if resp.status_code == 200 else f'error ({resp.status_code})'}"
            )
        except Exception:
            lines.append(f"  Fallback model ({shared.FALLBACK_MODEL_NAME}): offline")

    # HA entities
    entity_count = sum(
        len(shared.ha_client.get_entities_by_domain(d))
        for d in ["light", "switch", "fan", "climate", "cover", "scene", "sensor", "media_player"]
    )
    lines.append(f"  Home Assistant: {entity_count} entities loaded")

    # RAG collection
    try:
        rag_count = shared.collection.count()
        lines.append(f"  RAG documents: {rag_count}")
    except Exception:
        lines.append("  RAG: unavailable")

    # Scheduler jobs
    jobs = shared.scheduler.get_jobs()
    lines.append(f"  Scheduler: {len(jobs)} active jobs")
    for job in jobs[:5]:
        next_run = job.next_run_time.strftime("%I:%M %p") if job.next_run_time else "manual"
        lines.append(f"    - {job.name or job.id} (next: {next_run})")

    # Recent errors
    errors = log_ring.errors(limit=5)
    if errors:
        lines.append(f"\n  Recent errors ({len(errors)}):")
        for e in errors[:3]:
            lines.append(f"    [{e['time']}] {e['message'][:120]}")
    else:
        lines.append("\n  No recent errors.")

    return "\n".join(lines)


async def _check_recent_errors() -> str:
    """Check recent error log entries."""
    errors = log_ring.errors(limit=20)

    if not errors:
        return "No errors found in recent logs. Everything looks clean."

    lines = [f"Found {len(errors)} recent error(s):"]
    for e in errors:
        lines.append(f"  [{e['time']}] [{e['level']}] {e['message'][:150]}")

    return "\n".join(lines)


def _get_scheduler_job(job_id: str) -> Optional[str]:
    """Get next run time for a scheduler job."""
    try:
        job = shared.scheduler.get_job(job_id)
        if job and job.next_run_time:
            return job.next_run_time.strftime("%Y-%m-%d %I:%M %p")
        elif job:
            return "scheduled (no next run time)"
    except Exception:
        pass
    return None
