"""
Meal & Self-Care Nudges (F-008).

Monitors time since last meal, medication schedule, hydration, and movement.
Nudges via TTS at appropriate intervals. Gentle external signals, not nagging.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from orchestrator import shared
from orchestrator.reminder_manager import _announce_voice, _send_notification

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
        from orchestrator.state_store import get_last_selfcare, get_selfcare_today

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


def record_meal_logged(label: str = "a meal") -> None:
    """Advance the meal nudge gate.

    Called by both the selfcare_log tool and meal_manager.log_meal so
    either code path stops the "no meals logged today" scheduler nudge.
    Keeps _state.last_meal_reported (in-memory, read by _check_meals)
    in sync with the selfcare_log SQLite table (used to restore state
    on startup). Safe to call from sync contexts.
    """
    from orchestrator.state_store import save_selfcare_log

    _state.last_meal_reported = datetime.now()
    save_selfcare_log("meal", label)
    logger.info(f"[SELFCARE] Meal logged: {label}", extra={"component": "selfcare"})


# ---------------------------------------------------------------------------
# Logging actions (tool handler)
# ---------------------------------------------------------------------------


async def log_selfcare(action: str, detail: Optional[str] = None) -> str:
    """Log a self-care action. Called by the selfcare_log tool.

    Persists to SQLite so state survives orchestrator restarts.
    """
    from orchestrator.state_store import save_selfcare_log

    now = datetime.now()

    if action == "meal":
        meal_type = detail or "a meal"
        record_meal_logged(meal_type)
        result = f"Logged — you had {meal_type}."

    elif action == "medication":
        med_name = detail or "medication"
        _state.last_med_confirmation[med_name.lower()] = now
        # Also mark individual meds if a group phrase like "morning meds" was used
        _expand_med_confirmation(med_name, now)
        save_selfcare_log("medication", med_name)
        next_sched = _get_next_med_schedule(med_name)
        logger.info(f"[SELFCARE] Med logged: {med_name}", extra={"component": "selfcare"})
        result = f"Logged. Next dose is {next_sched}." if next_sched else f"Logged — {med_name} taken."

    elif action == "water":
        _state.last_hydration_nudge = now
        save_selfcare_log("water", detail)
        logger.info("[SELFCARE] Hydration logged", extra={"component": "selfcare"})
        result = "Logged — stay hydrated!"

    elif action == "movement":
        _state.last_movement_nudge = now
        _state.sitting_since = now
        save_selfcare_log("movement", detail)
        logger.info("[SELFCARE] Movement logged", extra={"component": "selfcare"})
        result = "Logged — nice to get moving."

    else:
        return f"Unknown action: {action}"

    # Bridge: if an active routine is waiting on a step that matches this
    # selfcare action, advance it. Fire-and-forget so the routine's own TTS
    # doesn't piggyback on the tool response (would arrive before the user
    # hears "Logged — medication taken"). 2026-04-17: user logged meds via
    # selfcare_log at 21:15 but the evening routine stayed stuck on
    # evening_meds and nudged all night.
    asyncio.create_task(_maybe_advance_routine_for_action(action))
    return result


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

    # Apply daily reset BEFORE the is_home gate — otherwise a user who's away
    # past midnight (or an orchestrator restart during quiet hours) leaves the
    # figures stuck at yesterday's timestamps. get_selfcare_status() reads
    # those and surfaces e.g. "you've been sitting for 32 hours".
    _apply_daily_reset(now)

    # Skip nudges when not home
    if shared.PRESENCE_ENABLED:
        try:
            from orchestrator.presence_tracker import get_presence

            if not get_presence().get("is_home", True):
                return
        except Exception:
            pass

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
        from orchestrator.routine_manager import _active_session

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
        from orchestrator.data_manager import get_medications

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
        hour = now.hour
        if hour < 14:
            suggestion = f"Lunch time! You had something around {last_str} — what sounds good for lunch?"
        elif hour < 17:
            suggestion = f"Afternoon snack? Last meal was around {last_str}."
        else:
            suggestion = f"Dinner time! Last meal was around {last_str}."
        return suggestion

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


def _apply_daily_reset(now: datetime) -> None:
    """Zero out overnight-accumulating selfcare state at midnight rollover.

    Clears `last_meal_reported` entirely (so "last meal" becomes "no meals
    logged today" after midnight instead of surfacing a yesterday timestamp).
    Rolls forward sitting_since / hydration / movement timestamps to `now`
    so their ticker restarts at 0 each day instead of carrying overnight
    hours. Safe to call from sync or async contexts; idempotent within a
    single day.
    """
    if _state.sitting_since and _state.sitting_since.date() < now.date():
        _state.sitting_since = now
    if _state.last_hydration_nudge and _state.last_hydration_nudge.date() < now.date():
        _state.last_hydration_nudge = now
    if _state.last_movement_nudge and _state.last_movement_nudge.date() < now.date():
        _state.last_movement_nudge = now
    if _state.last_meal_reported and _state.last_meal_reported.date() < now.date():
        logger.debug("[SELFCARE] Daily reset: clearing last_meal_reported from yesterday")
        _state.last_meal_reported = None


async def get_selfcare_status() -> Dict[str, Any]:
    """Get current self-care status for API or tool."""
    now = datetime.now()
    # Defensive: apply daily reset on read too, so stale overnight values never
    # leak into displayed figures even if the nudge loop has been paused.
    _apply_daily_reset(now)
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
        from orchestrator.data_manager import get_medications

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
        from orchestrator.data_manager import get_medications

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


# ---------------------------------------------------------------------------
# Routine bridge (F-006 selfcare → routine advancement)
# ---------------------------------------------------------------------------


# Word-boundary matching — "med" alone would false-match "premeditated",
# "stretch goals review", etc. Explicit words keep the bridge tight.
_ACTION_KEYWORDS: Dict[str, tuple] = {
    "medication": ("meds", "medication", "medications"),
    "meal": ("meal", "breakfast", "lunch", "dinner", "eat"),
    "water": ("water", "hydrate", "hydration"),
    "movement": ("movement", "stretch", "walk", "exercise"),
}


def _step_matches_selfcare_action(step, action: str) -> bool:
    """Does an active routine step correspond to this selfcare action?

    Matches on id OR label via word-boundary regex (case-insensitive).
    """
    if step is None:
        return False
    keywords = _ACTION_KEYWORDS.get(action)
    if not keywords:
        return False
    sid = (getattr(step, "id", "") or "").lower()
    label = (getattr(step, "label", "") or "").lower()
    haystack = f"{sid} {label}"
    return any(re.search(rf"\b{re.escape(k)}\b", haystack) for k in keywords)


async def _maybe_advance_routine_for_action(action: str) -> None:
    """Advance the active routine if its current step matches `action`."""
    try:
        from orchestrator.routine_manager import _active_session, advance_step

        if _active_session is None:
            return
        idx = _active_session.current_step_index
        if idx >= len(_active_session.steps):
            return
        step = _active_session.steps[idx]
        if not _step_matches_selfcare_action(step, action):
            return
        logger.info(
            f"[SELFCARE] Advancing routine step '{step.id}' — '{action}' logged",
            extra={"component": "selfcare"},
        )
        await advance_step("done")
    except Exception as e:
        # ERROR (not warning): if this fires, the bridge is structurally
        # broken — either the import path changed or the session shape did.
        # Either way it needs to be visible on dashboards, not buried.
        logger.error(f"[SELFCARE] Routine bridge failed: {e}", exc_info=True)
