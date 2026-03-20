"""
Ambient Awareness Mode (F-010).

Aggregates system state for passive display and periodic TTS summaries.
LED status indicator via HA, ambient dashboard mode support.
"""

import logging
from datetime import datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo

import shared

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Aggregated status
# ---------------------------------------------------------------------------


async def get_ambient_status() -> Dict[str, Any]:
    """Aggregated status for display, TTS, and LED."""
    tz = ZoneInfo(shared.TIMEZONE)
    now = datetime.now(tz)
    status: Dict[str, Any] = {"timestamp": now.isoformat()}

    # Schedule density + next event
    try:
        from google_calendar import get_calendar_client

        client = get_calendar_client(http_client=shared._http)
        if client and client.is_configured:
            response = await client.get_upcoming(hours_ahead=12)
            if response.success:
                events = [e for e in response.events if not e.all_day]
                status["events_remaining"] = len(events)

                if events:
                    next_ev = events[0]
                    minutes_away = int((next_ev.start - now).total_seconds() / 60)
                    status["next_event"] = {
                        "title": next_ev.title,
                        "start": str(next_ev.start),
                        "minutes_away": max(0, minutes_away),
                    }
                    if minutes_away <= 15:
                        status["schedule_density"] = "busy"
                    elif minutes_away <= 60:
                        status["schedule_density"] = "light"
                    else:
                        status["schedule_density"] = "clear"
                else:
                    status["next_event"] = None
                    status["schedule_density"] = "clear"
                    status["events_remaining"] = 0
    except Exception as e:
        logger.warning(f"[AMBIENT] Calendar check failed: {e}")
        status["schedule_density"] = "unknown"

    # Focus session
    status["focus_active"] = shared.current_focus_session.get("active", False)
    if status["focus_active"]:
        status["focus_task"] = shared.current_focus_session.get("task")

    # Routine
    try:
        from routine_manager import _active_session

        status["routine_active"] = _active_session is not None
        if _active_session:
            status["routine_name"] = _active_session.display_name
    except ImportError:
        status["routine_active"] = False

    # Pending reminders
    try:
        from reminder_manager import list_pending_reminders

        pending = list_pending_reminders()
        status["pending_reminders"] = pending.get("count", 0)
    except Exception:
        status["pending_reminders"] = 0

    # Self-care overdue items
    try:
        from selfcare_manager import get_selfcare_status

        sc = await get_selfcare_status()
        overdue = []
        if (sc.get("last_meal") is None and now.hour >= 12) or (
            sc.get("last_meal") and sc["last_meal"].get("hours_ago", 0) >= shared.MEAL_NUDGE_HOURS
        ):
            overdue.append("meal")
        if not sc.get("meds_confirmed_today"):
            if 7 <= now.hour < 10:
                overdue.append("morning meds")
            elif 20 <= now.hour < 22:
                overdue.append("evening meds")
        status["selfcare_overdue"] = overdue
    except Exception:
        status["selfcare_overdue"] = []

    # Active task context
    try:
        from context_tracker import _context_stack

        if _context_stack:
            status["active_task"] = _context_stack[-1].description
        else:
            status["active_task"] = None
    except Exception:
        status["active_task"] = None

    # Compute LED color
    status["led_color"] = _compute_led_color(status)

    return status


def _compute_led_color(status: Dict[str, Any]) -> str:
    """Determine LED color from ambient status."""
    if status.get("focus_active"):
        return "blue"
    if status.get("routine_active"):
        return "purple"

    next_event = status.get("next_event")
    if next_event:
        minutes = next_event.get("minutes_away", 999)
        if minutes <= 15:
            return "red"
        if minutes <= 60:
            return "yellow"

    return "green"


# ---------------------------------------------------------------------------
# TTS summary
# ---------------------------------------------------------------------------


async def build_ambient_summary_text() -> str:
    """Build a 2-3 sentence TTS summary from ambient status."""
    status = await get_ambient_status()
    tz = ZoneInfo(shared.TIMEZONE)
    now = datetime.now(tz)
    time_str = now.strftime("%-I %p")

    parts = [f"It's {time_str}."]

    # Events
    remaining = status.get("events_remaining", 0)
    next_event = status.get("next_event")
    if remaining == 0:
        parts.append("No more events today.")
    elif remaining == 1 and next_event:
        parts.append(f"One event left: {next_event['title']} in {next_event['minutes_away']} minutes.")
    elif next_event:
        parts.append(f"{remaining} events left. Next: {next_event['title']} in {next_event['minutes_away']} minutes.")

    # Active task
    active_task = status.get("active_task")
    if active_task:
        parts.append(f"Top task: {active_task}.")

    # Pending reminders
    pending = status.get("pending_reminders", 0)
    if pending > 0:
        parts.append(f"{pending} pending reminder{'s' if pending != 1 else ''}.")

    # Self-care
    overdue = status.get("selfcare_overdue", [])
    if overdue:
        parts.append(f"Overdue: {', '.join(overdue)}.")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# LED control
# ---------------------------------------------------------------------------


_LED_COLORS = {
    "green": {"rgb_color": [0, 255, 0], "brightness": 80},
    "yellow": {"rgb_color": [255, 200, 0], "brightness": 120},
    "red": {"rgb_color": [255, 0, 0], "brightness": 150},
    "blue": {"rgb_color": [0, 100, 255], "brightness": 100},
    "purple": {"rgb_color": [150, 0, 255], "brightness": 100},
}


async def set_ambient_led(color: str) -> None:
    """Set LED indicator via HA service call."""
    entity_id = shared.AMBIENT_LED_ENTITY
    if not entity_id:
        return

    color_data = _LED_COLORS.get(color, _LED_COLORS["green"])

    try:
        from ha_integration import ha_client

        result = await ha_client.call_service(entity_id, "turn_on", color_data)
        if result.success:
            logger.debug(f"[AMBIENT] LED set to {color}")
        else:
            logger.warning(f"[AMBIENT] LED update failed: {result.message}")
    except Exception as e:
        logger.warning(f"[AMBIENT] LED error: {e}")
