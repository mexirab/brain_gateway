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
from orchestrator.metrics import SELFCARE_LOGGED
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


# Detail markers that identify a BROAD "took my meds" confirmation (vs. a single
# named med). These come from record_medication_logged's callers: the Telegram
# ✓ Done nudge ("telegram:medication nudge"), the routine bridge ("routine:meds"),
# grouped confirmations ("morning meds (...)"), and the bare default "medication".
# _restore_state uses this to decide whether to rebuild the generic "medication"
# gate that _check_meds reads first.
_BROAD_MED_MARKERS = ("medication", "meds", "nudge", "routine:")


def _is_broad_med_confirmation(label: str) -> bool:
    """True if a selfcare_log med `detail` was a window-wide confirmation rather
    than one named med — so restoring it should re-arm the generic gate."""
    return any(marker in label for marker in _BROAD_MED_MARKERS)


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

        # Restore today's med confirmations. _check_meds gates FIRST on the
        # generic "medication" key (set by record_medication_logged — Telegram
        # ✓ Done taps, the routine bridge, grouped "morning meds" logs). Rebuilding
        # only per-label keys (the old behavior) left that generic gate empty, so
        # every restart re-fired an already-taken med nudge: the "I tapped Done but
        # the meds reminder keeps coming" bug, amplified by frequent deploys.
        #
        # KNOWN GAP: the generic gate only suppresses when its hour is <12
        # (morning) or >=17 (evening), so a broad confirmation logged 12:00–16:59
        # re-arms the key but matches neither window. Harmless in the default
        # 07:00/21:00 schedule (no window is open then); if meds `times` move into
        # that band the duplicate nudge can resurface — a _check_meds limitation to
        # revisit later (gate on the configured window start, not a fixed hour).
        try:
            from orchestrator.data_manager import get_medications

            _daily = get_medications().get("daily", {})
            configured_meds = {
                (m.get("name") or "").lower()
                for window in ("morning", "evening")
                for m in _daily.get(window, [])
                if m.get("name")
            }
        except Exception:
            configured_meds = set()

        today_meds = get_selfcare_today("medication")
        generic_ts: Optional[datetime] = None
        for entry in today_meds:
            med_name = (entry.get("detail") or "medication").lower()
            logged_at = datetime.fromisoformat(entry["logged_at"])
            _state.last_med_confirmation[med_name] = logged_at
            _expand_med_confirmation(med_name, logged_at)
            # Broad confirmations re-arm the generic gate; a single named-med log
            # ("I took my Guanfacine") must NOT, or it would wrongly suppress the
            # window's other meds. Guard on the configured med list so a future
            # drug whose NAME contains a marker (e.g. "meds") is still treated as
            # specific. Keep the latest dose for the window hour-check.
            if (
                med_name not in configured_meds
                and _is_broad_med_confirmation(med_name)
                and (generic_ts is None or logged_at > generic_ts)
            ):
                generic_ts = logged_at
        if generic_ts is not None:
            _state.last_med_confirmation["medication"] = generic_ts

        if today_meds:
            logger.info(
                "[SELFCARE] Restored %d med confirmation(s); generic gate armed=%s%s",
                len(today_meds),
                generic_ts is not None,
                f" (latest {generic_ts.strftime('%-I:%M %p')})" if generic_ts else "",
            )

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
    SELFCARE_LOGGED.labels(action="meal").inc()
    logger.info(f"[SELFCARE] Meal logged: {label}", extra={"component": "selfcare"})


def record_medication_logged(label: str = "medication") -> None:
    """Advance the medication nudge gate.

    Public sync helper so other subsystems (routine_manager completing a
    meds step) can keep the selfcare state in sync without duplicating
    log_selfcare's branching. Sets the generic 'medication' key in addition
    to the label-keyed entry so _check_meds's primary gate fires regardless
    of whether _expand_med_confirmation can infer the window from the label
    (e.g. 'routine:meds' doesn't contain 'morning'/'evening').
    """
    from orchestrator.state_store import save_selfcare_log

    now = datetime.now()
    _state.last_med_confirmation[label.lower()] = now
    _state.last_med_confirmation["medication"] = now
    _expand_med_confirmation(label, now)
    save_selfcare_log("medication", label)
    SELFCARE_LOGGED.labels(action="medication").inc()
    logger.info(f"[SELFCARE] Med logged: {label}", extra={"component": "selfcare"})


def record_hydration_logged(label: str = "water") -> None:
    """Advance the hydration nudge gate."""
    from orchestrator.state_store import save_selfcare_log

    _state.last_hydration_nudge = datetime.now()
    save_selfcare_log("water", label)
    SELFCARE_LOGGED.labels(action="water").inc()
    logger.info(f"[SELFCARE] Hydration logged: {label}", extra={"component": "selfcare"})


def record_movement_logged(label: str = "movement") -> None:
    """Advance the movement nudge gate (resets sitting_since too).

    Called by workout_manager.log_set so lifting a set counts as movement
    and suppresses the "you've been sitting for N minutes" nudge that
    would otherwise fire while the user is actively at the gym.
    """
    from orchestrator.state_store import save_selfcare_log

    now = datetime.now()
    _state.last_movement_nudge = now
    _state.sitting_since = now
    save_selfcare_log("movement", label)
    SELFCARE_LOGGED.labels(action="movement").inc()
    logger.info(f"[SELFCARE] Movement logged: {label}", extra={"component": "selfcare"})


def mark_selfcare_from_routine_step(step) -> None:
    """Update selfcare state when a routine step completes.

    The reverse direction of log_selfcare's routine bridge: when the user
    completes a step via routine_action("done"), keep selfcare state in
    sync so the scheduled selfcare nudge for that action doesn't fire
    later. Silent no-op for steps that don't map to any selfcare action.
    """
    action = _infer_selfcare_action(step)
    if action is None:
        return
    label = f"routine:{getattr(step, 'id', '?')}"
    if action == "medication":
        record_medication_logged(label)
    elif action == "meal":
        record_meal_logged(label)
    elif action == "water":
        record_hydration_logged(label)
    elif action == "movement":
        record_movement_logged(label)


def _infer_selfcare_action(step) -> Optional[str]:
    """Return the selfcare action a step corresponds to, or None."""
    if step is None:
        return None
    for action in _ACTION_KEYWORDS:
        if _step_matches_selfcare_action(step, action):
            return action
    return None


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
        # NOTE: intentionally does NOT set the generic "medication" key here —
        # preserves prior behavior where users logging individual meds ("I took
        # my Guanfacine") only suppress that specific med's nudge, not all
        # window meds. The routine bridge (record_medication_logged) DOES set
        # the generic key because routine step labels like 'routine:meds' can't
        # be mapped to a window by _expand_med_confirmation.
        save_selfcare_log("medication", med_name)
        SELFCARE_LOGGED.labels(action="medication").inc()
        next_sched = _get_next_med_schedule(med_name)
        logger.info(f"[SELFCARE] Med logged: {med_name}", extra={"component": "selfcare"})
        result = f"Logged. Next dose is {next_sched}." if next_sched else f"Logged — {med_name} taken."

    elif action == "water":
        _state.last_hydration_nudge = now
        save_selfcare_log("water", detail)
        SELFCARE_LOGGED.labels(action="water").inc()
        logger.info("[SELFCARE] Hydration logged", extra={"component": "selfcare"})
        result = "Logged — stay hydrated!"

    elif action == "movement":
        _state.last_movement_nudge = now
        _state.sitting_since = now
        save_selfcare_log("movement", detail)
        SELFCARE_LOGGED.labels(action="movement").inc()
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

    # Quiet hours check — pulled from data/selfcare_schedule.yaml so the
    # /settings page can edit them without a restart. Falls through to
    # shared.QUIET_HOURS_* defaults on missing/empty file.
    from orchestrator.selfcare_schedule import is_quiet_day, quiet_hours

    qh = quiet_hours()
    if is_quiet_day(now_tz.isoweekday()):
        quiet_start = _parse_time(qh.get("start") or shared.QUIET_HOURS_START)
        quiet_end = _parse_time(qh.get("end") or shared.QUIET_HOURS_END)
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

    # Priority order: meds > meals > movement > hydration (one per cycle).
    # The kind rides along so phone channels can offer a matching one-tap
    # log action (kind keys match the F-011 selfcare-bridge vocabulary).
    nudge, kind = _check_meds(now, now_tz), "medication"
    if not nudge:
        nudge, kind = _check_meals(now), "meal"
    if not nudge:
        nudge, kind = _check_movement(now), "movement"
    if not nudge:
        nudge, kind = _check_hydration(now), "water"

    if nudge:
        await _dispatch_nudge(kind, nudge)


async def _dispatch_nudge(kind: str, nudge: str) -> None:
    """Deliver one nudge across channels: TTS + HA companion push + Telegram.

    Selfcare nudges never pass through deliver_reminder_job, so the reminder
    push channels (F-011 ntfy / F-013 Pushover / Telegram reminders) don't
    apply here — before this, a med nudge with no HA mobile service was
    voice-only and vanished whenever TTS was down (observed 2026-07-06:
    two silent ConnectError drops before the 07:50 nudge landed).
    """
    await _announce_voice(nudge, announcement_type="selfcare")
    await _send_notification(nudge)
    # Telegram mirror with a one-tap "✓ Done" that logs the action.
    # Fire-and-forget; kind-gated via TELEGRAM_SELFCARE_NUDGES.
    try:
        from orchestrator.telegram_bot import fire_selfcare_nudge

        fire_selfcare_nudge(kind, nudge)
    except Exception as tg_err:
        logger.warning(f"[SELFCARE] Telegram nudge dispatch failed: {tg_err}")
    logger.info(f"[SELFCARE] Nudge ({kind}): {nudge[:60]}", extra={"component": "selfcare"})


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


# Window length after each configured meds time (the check cycle runs ~every
# 15 min). Split so the pre-config windows are preserved EXACTLY on defaults:
# morning was 07:00–10:00 (180 min), evening 20:00–22:00 (120 min).
_MED_MORNING_WINDOW_MINUTES = 180
_MED_EVENING_WINDOW_MINUTES = 120


def _med_window_starts() -> tuple[Optional[int], Optional[int]]:
    """(morning_start, evening_start) in minutes-since-midnight, from the
    configured meds times (Settings → Selfcare `categories.meds.times`) — NOT a
    hardcoded window. A time before noon opens the morning window, at/after noon
    the evening window; earliest wins. Falls back to ["07:00","20:00"] (the old
    hardcoded window starts) so on-defaults behavior is unchanged.

    NOTE: each half-day nudge exists only if a time in that half is configured —
    e.g. configuring only a morning time disables the evening nudge. This is a
    deliberate change from the old always-on 20:00 evening window.
    """
    from orchestrator.selfcare_schedule import category_times

    times = category_times("meds") or ["07:00", "20:00"]
    morning = evening = None
    for t in times:
        try:
            h, m = map(int, str(t).split(":"))
        except (ValueError, AttributeError):
            continue
        mins = h * 60 + m
        if h < 12:
            morning = mins if morning is None else min(morning, mins)
        else:
            evening = mins if evening is None else min(evening, mins)
    return morning, evening


def _med_allowed_today(med: dict, now_tz: datetime) -> bool:
    """Whether `med` should be nudged today given its optional `days` list.

    `days` holds lowercase 3-letter ISO weekday abbrevs (mon..sun). Absence of
    `days` = every day (backward compatible — existing meds are unaffected).

    Fails OPEN: a malformed/empty `days` (typo, wrong type) is treated as "every
    day" and logged, so a data glitch never silently drops a med reminder — the
    safe failure for a safety-critical nudge is to remind, not to skip.
    """
    days = med.get("days")
    if not days:
        return True
    if not isinstance(days, (list, tuple)):
        logger.warning(f"[SELFCARE] med {med.get('name')!r} has non-list days={days!r}; treating as every day")
        return True
    allowed = {str(d).strip().lower()[:3] for d in days if str(d).strip()}
    if not allowed:
        logger.warning(f"[SELFCARE] med {med.get('name')!r} has empty/blank days={days!r}; treating as every day")
        return True
    return now_tz.strftime("%a").lower() in allowed


def _check_meds(now: datetime, now_tz: datetime) -> Optional[str]:
    """Check if any medication is due and not confirmed.

    Windows are driven by `category_times("meds")` (settings-page editable), so
    changing the meds time actually takes effect — previously the 7–10 / 20–22
    windows were hardcoded and ignored the schedule.
    """
    from orchestrator.selfcare_schedule import category_enabled

    if not category_enabled("meds"):
        return None

    try:
        from orchestrator.data_manager import get_medications

        meds_data = get_medications()
        daily = meds_data.get("daily", {})

        now_min = now_tz.hour * 60 + now_tz.minute
        morning_start, evening_start = _med_window_starts()

        # Generic "medication" confirmation covers meds in the current window.
        # De-dup thresholds (morning hour<12 / evening hour>=17) are unchanged so
        # an afternoon log can't suppress the evening nudge.
        generic = _state.last_med_confirmation.get("medication")

        # Modulo-1440 so a late-evening configured time whose window crosses
        # midnight still matches (times are user-editable now).
        if morning_start is not None and (now_min - morning_start) % 1440 < _MED_MORNING_WINDOW_MINUTES:
            if generic and generic.date() == now.date() and generic.hour < 12:
                return None  # generic morning confirmation
            for med in daily.get("morning", []):
                med_name = med.get("name", "")
                if not med_name:
                    continue
                if not _med_allowed_today(med, now_tz):
                    continue  # e.g. a Mon–Fri stimulant on a weekend
                last = _state.last_med_confirmation.get(med_name.lower())
                if last and last.date() == now.date() and last.hour < 12:
                    continue  # confirmed this morning
                return f"Hey, did you take your {med_name}?"

        if evening_start is not None and (now_min - evening_start) % 1440 < _MED_EVENING_WINDOW_MINUTES:
            if generic and generic.date() == now.date() and generic.hour >= 17:
                return None  # generic evening confirmation
            for med in daily.get("evening", []):
                med_name = med.get("name", "")
                if not med_name:
                    continue
                if not _med_allowed_today(med, now_tz):
                    continue  # e.g. a Mon–Fri stimulant on a weekend
                last = _state.last_med_confirmation.get(med_name.lower())
                if last and last.date() == now.date() and last.hour >= 17:
                    continue  # confirmed this evening
                return f"Hey, did you take your {med_name}?"

    except Exception as e:
        logger.warning(f"[SELFCARE] Med check failed: {e}")

    return None


def _within_active_hours(category: str, now: datetime) -> bool:
    """True if the category's active_hours window covers `now`. Missing
    window = always active."""
    from orchestrator.selfcare_schedule import category_active_hours

    start_s, end_s = category_active_hours(category)
    if not start_s or not end_s:
        return True
    try:
        start = _parse_time(start_s)
        end = _parse_time(end_s)
    except Exception:
        return True
    current = now.time()
    if start <= end:
        return start <= current <= end
    # Wraps midnight
    return current >= start or current <= end


def _check_meals(now: datetime) -> Optional[str]:
    """Check if it's been too long since last meal."""
    from orchestrator.selfcare_schedule import category_enabled, category_interval_minutes

    if not category_enabled("meals"):
        return None
    if not _within_active_hours("meals", now):
        return None

    meal_minutes = category_interval_minutes("meals", shared.MEAL_NUDGE_HOURS * 60)
    meal_hours = meal_minutes / 60.0

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
    from orchestrator.selfcare_schedule import category_enabled, category_interval_minutes, load_schedule

    if not category_enabled("water"):
        return None
    if not _within_active_hours("water", now):
        return None

    interval = category_interval_minutes("water", shared.HYDRATION_INTERVAL)
    if _state.last_hydration_nudge is None:
        _state.last_hydration_nudge = now
        return None

    minutes_since = (now - _state.last_hydration_nudge).total_seconds() / 60
    if minutes_since >= interval:
        _state.last_hydration_nudge = now
        msg = (load_schedule()["categories"].get("water", {}).get("message_template") or "").strip()
        return msg or "Water check. Take a few sips."

    return None


def _check_movement(now: datetime) -> Optional[str]:
    """Check if movement nudge is due."""
    from orchestrator.selfcare_schedule import category_enabled, category_interval_minutes

    if not category_enabled("movement"):
        return None
    if not _within_active_hours("movement", now):
        return None

    interval = category_interval_minutes("movement", shared.MOVEMENT_INTERVAL)
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


def evening_meds_status() -> Optional[Dict[str, Any]]:
    """Evening medication adherence for the evening shutdown ritual.

    Returns None when the meds category is disabled or no evening meds are
    scheduled. Otherwise {'names': [...], 'confirmed': bool} using the same
    semantics as _check_meds: a generic 'medication' confirmation today at
    hour >= 17 covers everything; else every individual evening med must
    have its own confirmation today at hour >= 17.
    """
    from orchestrator.selfcare_schedule import category_enabled

    if not category_enabled("meds"):
        return None

    try:
        from orchestrator.data_manager import get_medications

        evening_meds = get_medications().get("daily", {}).get("evening", [])
    except Exception as e:
        logger.warning(f"[SELFCARE] Evening meds lookup failed: {e}")
        return None

    names = [med.get("name", "") for med in evening_meds if med.get("name")]
    if not names:
        return None

    today = datetime.now().date()
    generic = _state.last_med_confirmation.get("medication")
    if generic and generic.date() == today and generic.hour >= 17:
        return {"names": names, "confirmed": True}

    for name in names:
        last = _state.last_med_confirmation.get(name.lower())
        if not (last and last.date() == today and last.hour >= 17):
            return {"names": names, "confirmed": False}
    return {"names": names, "confirmed": True}


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
ACTION_KEYWORDS: Dict[str, tuple] = {
    "medication": ("meds", "medication", "medications", "pill", "pills"),
    "meal": ("meal", "breakfast", "lunch", "dinner", "eat", "snack"),
    "water": ("water", "hydrate", "hydration", "drink"),
    "movement": ("movement", "stretch", "walk", "exercise", "move"),
}
# Back-compat alias — internal callers still import the private name.
_ACTION_KEYWORDS = ACTION_KEYWORDS


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
