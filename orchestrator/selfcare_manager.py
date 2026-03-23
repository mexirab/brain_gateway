"""
Meal & Self-Care Nudges (F-008).

Monitors time since last meal, medication schedule, hydration, and movement.
Nudges via TTS at appropriate intervals. Gentle external signals, not nagging.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import shared
from reminder_manager import _announce_voice, _send_notification

logger = logging.getLogger(__name__)


@dataclass
class SelfCareState:
    last_meal_reported: Optional[datetime] = None
    last_hydration_nudge: Optional[datetime] = None
    last_movement_nudge: Optional[datetime] = None
    last_med_confirmation: Dict[str, datetime] = field(default_factory=dict)
    sitting_since: Optional[datetime] = None


# Module state (restored from DB on import)
_state = SelfCareState()


def _restore_state() -> None:
    """Restore selfcare state from persistent storage on startup."""
    try:
        from state_store import get_last_selfcare, get_selfcare_today

        last_meal = get_last_selfcare("meal")
        if last_meal:
            _state.last_meal_reported = last_meal
            logger.info(f"[SELFCARE] Restored last meal: {last_meal.strftime('%-I:%M %p')}")

        last_water = get_last_selfcare("water")
        if last_water:
            _state.last_hydration_nudge = last_water

        last_movement = get_last_selfcare("movement")
        if last_movement:
            _state.last_movement_nudge = last_movement
            _state.sitting_since = last_movement

        # Restore today's med confirmations
        today_meds = get_selfcare_today("medication")
        for entry in today_meds:
            med_name = (entry.get("detail") or "medication").lower()
            logged_at = datetime.fromisoformat(entry["logged_at"])
            _state.last_med_confirmation[med_name] = logged_at
            _expand_med_confirmation(med_name, logged_at)

    except Exception as e:
        logger.warning(f"[SELFCARE] Failed to restore state from DB: {e}")


# Called from orchestrator.py startup_event() after state_store.init_db()


# ---------------------------------------------------------------------------
# Logging actions (tool handler)
# ---------------------------------------------------------------------------


async def log_selfcare(action: str, detail: Optional[str] = None) -> str:
    """Log a self-care action. Called by the selfcare_log tool.

    Persists to SQLite so state survives orchestrator restarts.
    """
    from state_store import save_selfcare_log

    now = datetime.now()

    if action == "meal":
        _state.last_meal_reported = now
        meal_type = detail or "a meal"
        save_selfcare_log("meal", meal_type)
        logger.info(f"[SELFCARE] Meal logged: {meal_type}", extra={"component": "selfcare"})
        return f"Logged — you had {meal_type}."

    elif action == "medication":
        med_name = detail or "medication"
        _state.last_med_confirmation[med_name.lower()] = now
        # Also mark individual meds if a group phrase like "morning meds" was used
        _expand_med_confirmation(med_name, now)
        save_selfcare_log("medication", med_name)
        next_sched = _get_next_med_schedule(med_name)
        logger.info(f"[SELFCARE] Med logged: {med_name}", extra={"component": "selfcare"})
        if next_sched:
            return f"Logged. Next dose is {next_sched}."
        return f"Logged — {med_name} taken."

    elif action == "water":
        _state.last_hydration_nudge = now
        save_selfcare_log("water", detail)
        logger.info("[SELFCARE] Hydration logged", extra={"component": "selfcare"})
        return "Logged — stay hydrated!"

    elif action == "movement":
        _state.last_movement_nudge = now
        _state.sitting_since = now
        save_selfcare_log("movement", detail)
        logger.info("[SELFCARE] Movement logged", extra={"component": "selfcare"})
        return "Logged — nice to get moving."

    return f"Unknown action: {action}"


# ---------------------------------------------------------------------------
# Periodic check (background job)
# ---------------------------------------------------------------------------


async def check_selfcare() -> None:
    """Called by APScheduler every 15 min. Delivers at most one nudge per cycle."""
    if not shared.SELFCARE_ENABLED:
        return

    now = datetime.now()
    tz = ZoneInfo(shared.TIMEZONE)
    now_tz = datetime.now(tz)

    # Quiet hours check
    quiet_start = _parse_time(shared.QUIET_HOURS_START)
    quiet_end = _parse_time(shared.QUIET_HOURS_END)
    if _in_quiet_hours(now_tz.time(), quiet_start, quiet_end):
        return

    # Don't nudge during active focus session (will nudge after)
    if shared.current_focus_session.get("active"):
        return

    # Don't nudge during active routine
    try:
        from routine_manager import _active_session

        if _active_session is not None:
            return
    except ImportError:
        pass

    # Priority order: meds > meals > movement > hydration (one per cycle)
    nudge = _check_meds(now, now_tz)
    if not nudge:
        nudge = _check_meals(now)
    if not nudge:
        nudge = _check_movement(now)
    if not nudge:
        nudge = _check_hydration(now)

    if nudge:
        await _announce_voice(nudge, announcement_type="selfcare")
        await _send_notification(nudge)
        logger.info(f"[SELFCARE] Nudge: {nudge[:60]}", extra={"component": "selfcare"})


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_meds(now: datetime, now_tz: datetime) -> Optional[str]:
    """Check if any medication is due and not confirmed."""
    try:
        from data_manager import get_medications

        meds_data = get_medications()
        daily = meds_data.get("daily", {})

        current_hour = now_tz.hour

        # Generic "medication" confirmation covers meds in the current window
        generic = _state.last_med_confirmation.get("medication")

        # Morning meds: window 7:00-10:00
        if 7 <= current_hour < 10:
            if generic and generic.date() == now.date() and generic.hour < 12:
                return None  # generic morning confirmation
            for med in daily.get("morning", []):
                med_name = med.get("name", "")
                if not med_name:
                    continue
                last = _state.last_med_confirmation.get(med_name.lower())
                if last and last.date() == now.date() and last.hour < 12:
                    continue  # confirmed this morning
                return f"Hey, did you take your {med_name}?"

        # Evening meds: window 20:00-22:00
        if 20 <= current_hour < 22:
            if generic and generic.date() == now.date() and generic.hour >= 17:
                return None  # generic evening confirmation
            for med in daily.get("evening", []):
                med_name = med.get("name", "")
                if not med_name:
                    continue
                last = _state.last_med_confirmation.get(med_name.lower())
                if last and last.date() == now.date() and last.hour >= 17:
                    continue  # confirmed this evening
                return f"Hey, did you take your {med_name}?"

    except Exception as e:
        logger.warning(f"[SELFCARE] Med check failed: {e}")

    return None


def _check_meals(now: datetime) -> Optional[str]:
    """Check if it's been too long since last meal."""
    meal_hours = shared.MEAL_NUDGE_HOURS

    # Don't nudge before 9am or after 9pm
    if now.hour < 9 or now.hour > 21:
        return None

    if _state.last_meal_reported is None:
        # No meal logged today — nudge after 12pm
        if now.hour >= 12:
            return f"It's {now.strftime('%-I:%M %p')} and no meals logged today. Grab something — even a snack."
        return None

    hours_since = (now - _state.last_meal_reported).total_seconds() / 3600
    if hours_since >= meal_hours:
        last_str = _state.last_meal_reported.strftime("%-I:%M %p")
        return (
            f"It's been about {int(hours_since)} hours since you last ate (around {last_str}). "
            f"Time for a snack or meal."
        )

    return None


def _check_hydration(now: datetime) -> Optional[str]:
    """Check if hydration nudge is due."""
    interval = shared.HYDRATION_INTERVAL
    if _state.last_hydration_nudge is None:
        _state.last_hydration_nudge = now
        return None

    minutes_since = (now - _state.last_hydration_nudge).total_seconds() / 60
    if minutes_since >= interval:
        _state.last_hydration_nudge = now
        return "Water check. Take a few sips."

    return None


def _check_movement(now: datetime) -> Optional[str]:
    """Check if movement nudge is due."""
    interval = shared.MOVEMENT_INTERVAL
    if _state.sitting_since is None:
        _state.sitting_since = now
        return None

    minutes_sitting = (now - _state.sitting_since).total_seconds() / 60
    if minutes_sitting >= interval and (
        _state.last_movement_nudge is None or (now - _state.last_movement_nudge).total_seconds() / 60 >= interval
    ):
        _state.last_movement_nudge = now
        return f"You've been sitting for about {int(minutes_sitting)} minutes. Stand up and stretch."

    return None


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


async def get_selfcare_status() -> Dict[str, Any]:
    """Get current self-care status for API or tool."""
    now = datetime.now()
    status = {}

    if _state.last_meal_reported:
        hours = (now - _state.last_meal_reported).total_seconds() / 3600
        status["last_meal"] = {"time": _state.last_meal_reported.strftime("%-I:%M %p"), "hours_ago": round(hours, 1)}
    else:
        status["last_meal"] = None

    status["meds_confirmed_today"] = {
        name: ts.strftime("%-I:%M %p") for name, ts in _state.last_med_confirmation.items() if ts.date() == now.date()
    }

    if _state.sitting_since:
        minutes = (now - _state.sitting_since).total_seconds() / 60
        status["sitting_minutes"] = int(minutes)
    else:
        status["sitting_minutes"] = None

    return status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _expand_med_confirmation(detail: str, when: datetime) -> None:
    """If detail is a group phrase (e.g. 'morning meds'), also confirm each
    individual medication in that schedule window.  Also handles detail strings
    that mention specific meds by name (e.g. 'morning meds (Vyvanse, Wellbutrin)')."""
    try:
        from data_manager import get_medications

        meds_data = get_medications()
        daily = meds_data.get("daily", {})
        detail_lower = detail.lower()

        # "morning meds" → confirm all morning meds; same for "evening meds"
        for window in ("morning", "evening"):
            if window in detail_lower and "med" in detail_lower:
                for med in daily.get(window, []):
                    name = med.get("name", "").lower()
                    if name:
                        _state.last_med_confirmation[name] = when

        # Also check if any individual med name appears in the detail string
        for window_meds in daily.values():
            if not isinstance(window_meds, list):
                continue
            for med in window_meds:
                name = med.get("name", "").lower()
                if name and name in detail_lower:
                    _state.last_med_confirmation[name] = when

    except Exception:
        pass


def _get_next_med_schedule(med_name: str) -> Optional[str]:
    """Determine the next medication schedule for a given med."""
    try:
        from data_manager import get_medications

        meds_data = get_medications()
        daily = meds_data.get("daily", {})

        for sched, meds in daily.items():
            for med in meds:
                if med.get("name", "").lower() == med_name.lower():
                    if sched == "morning":
                        return "evening"
                    elif sched == "evening":
                        return "tomorrow morning"
    except Exception:
        pass
    return None


def _parse_time(time_str: str) -> time:
    """Parse HH:MM string to time object."""
    try:
        h, m = map(int, time_str.split(":"))
        return time(h, m)
    except Exception:
        return time(22, 0)


def _in_quiet_hours(current: time, start: time, end: time) -> bool:
    """Check if current time is within quiet hours (handles midnight wrap)."""
    if start <= end:
        return start <= current <= end
    # Wraps midnight (e.g., 22:00 to 07:00)
    return current >= start or current <= end
