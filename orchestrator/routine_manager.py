"""
Context-Aware Routine Scaffolding (F-006).

Walks users through morning/evening routines step by step via TTS.
One step at a time, nudges on silence, adapts to calendar pressure.
"""

import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import shared
from reminder_manager import _announce_voice
from shared import profile

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class RoutineStep:
    id: str
    label: str
    est_minutes: int = 5
    skippable: bool = True
    ha_action: Optional[Dict[str, Any]] = None
    fallback_label: Optional[str] = None
    fallback_threshold_minutes: Optional[int] = None
    include_calendar_summary: bool = False
    calendar_days_ahead: int = 0


@dataclass
class RoutineSession:
    routine_id: str
    display_name: str
    started_at: datetime
    current_step_index: int = 0
    step_started_at: Optional[datetime] = None
    skipped_steps: List[str] = field(default_factory=list)
    completed_steps: List[str] = field(default_factory=list)
    paused: bool = False
    nudge_count: int = 0
    nudge_job_id: Optional[str] = None
    speaker: Optional[str] = None
    steps: List[RoutineStep] = field(default_factory=list)
    nudge_delay_minutes: int = 10


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_active_session: Optional[RoutineSession] = None
_routines: Dict[str, Dict] = {}

# Nudge message templates (indexed by nudge count 1/2/3+)
_NUDGE_TEMPLATES = [
    "Still on {label}? No pressure, just checking.",
    "Still working on {label}? Take your time, or say 'skip'.",
    "I'll move past {label} soon unless you say otherwise.",
]


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


async def load_routines(yaml_path: str) -> None:
    """Load routine definitions from YAML. Called at startup."""
    global _routines
    try:
        import yaml

        with open(yaml_path) as f:
            data = yaml.safe_load(f) or {}
        _routines = data.get("routines", {})
        count = len(_routines)
        logger.info(f"[ROUTINE] Loaded {count} routine(s) from {yaml_path}")
    except FileNotFoundError:
        logger.warning(f"[ROUTINE] Routines file not found: {yaml_path}")
        _routines = {}
    except Exception as e:
        logger.warning(f"[ROUTINE] Failed to load routines: {e}")
        _routines = {}


def _parse_steps(step_defs: List[Dict]) -> List[RoutineStep]:
    """Parse step dicts from YAML into RoutineStep objects."""
    steps = []
    for s in step_defs:
        steps.append(
            RoutineStep(
                id=s.get("id", f"step_{len(steps)}"),
                label=s.get("label", "Next step"),
                est_minutes=s.get("est_minutes", 5),
                skippable=s.get("skippable", True),
                ha_action=s.get("ha_action"),
                fallback_label=s.get("fallback_label"),
                fallback_threshold_minutes=s.get("fallback_threshold_minutes"),
                include_calendar_summary=s.get("include_calendar_summary", False),
                calendar_days_ahead=s.get("calendar_days_ahead", 0),
            )
        )
    return steps


# ---------------------------------------------------------------------------
# Core session logic
# ---------------------------------------------------------------------------


async def start_routine(routine_id: str, triggered_by: str = "user") -> str:
    """Start a routine session. Returns TTS-friendly status."""
    global _active_session

    if _active_session is not None:
        return (
            f"There's already an active routine ({_active_session.display_name}). Say 'stop routine' to end it first."
        )

    routine_def = _routines.get(routine_id)
    if not routine_def:
        available = ", ".join(_routines.keys()) if _routines else "none configured"
        return f"Unknown routine '{routine_id}'. Available: {available}."

    # Don't interrupt focus sessions with scheduled triggers
    if triggered_by == "scheduled" and shared.current_focus_session.get("active"):
        logger.info(f"[ROUTINE] Skipping scheduled '{routine_id}' — focus session active")
        return f"Skipping scheduled {routine_id} routine — focus session is active."

    steps = _parse_steps(routine_def.get("steps", []))
    if not steps:
        return f"Routine '{routine_id}' has no steps defined."

    display_name = routine_def.get("display_name", routine_id.title())
    speaker = routine_def.get("speaker")
    nudge_delay = routine_def.get("nudge_delay_minutes", 10)

    _active_session = RoutineSession(
        routine_id=routine_id,
        display_name=display_name,
        started_at=datetime.now(),
        current_step_index=0,
        step_started_at=datetime.now(),
        speaker=speaker,
        steps=steps,
        nudge_delay_minutes=nudge_delay,
    )

    # Record context for interruption recovery (F-007)
    try:
        import asyncio as _asyncio

        import context_tracker as _ct

        _asyncio.ensure_future(_ct.record_context(description=f"{display_name} routine"))
    except Exception as e:
        logger.warning(f"[ROUTINE] Context tracking failed: {e}")

    # Get calendar context for time awareness
    buffer_minutes, next_event = await _get_calendar_buffer()

    # Announce first step
    step = steps[0]
    is_late = buffer_minutes is not None and buffer_minutes < 60
    announcement = _build_step_announcement(
        step=step,
        buffer_minutes=buffer_minutes,
        next_event_title=next_event,
        is_first=True,
        is_last=len(steps) == 1,
        is_late=is_late,
        steps_remaining=len(steps),
    )
    await _announce_voice(announcement, speaker=speaker, announcement_type="routine")

    # Fire HA action for first step
    await _fire_step_ha_action(step)

    # Schedule nudge
    _schedule_nudge(nudge_delay)

    logger.info(
        f"[ROUTINE] Started '{routine_id}' ({len(steps)} steps, triggered_by={triggered_by})",
        extra={"component": "routine"},
    )

    return announcement


async def advance_step(action: str = "done") -> str:
    """Handle routine actions: done, skip, pause, resume, stop, status."""
    global _active_session

    if action in ("pause",):
        return await _pause_routine()
    if action in ("resume",):
        return await _resume_routine()
    if action in ("stop",):
        return await _stop_routine()
    if action in ("status", "what's next"):
        return await get_routine_status()

    if _active_session is None:
        return "No routine is active. Say 'start morning routine' or 'start evening routine'."

    if _active_session.paused:
        return "Routine is paused. Say 'resume routine' to continue."

    step = _active_session.steps[_active_session.current_step_index]

    if action == "skip":
        if not step.skippable:
            return f"Can't skip this one — {step.label} is important. Let me know when you're done."
        _active_session.skipped_steps.append(step.id)
        logger.info(f"[ROUTINE] Skipped step: {step.id}", extra={"component": "routine"})
    else:
        # done / next / finished
        _active_session.completed_steps.append(step.id)
        logger.info(f"[ROUTINE] Completed step: {step.id}", extra={"component": "routine"})

    # Cancel current nudge
    _cancel_nudge()
    _active_session.nudge_count = 0

    # Move to next step
    _active_session.current_step_index += 1

    if _active_session.current_step_index >= len(_active_session.steps):
        return await _complete_routine()

    # Announce next step
    next_step = _active_session.steps[_active_session.current_step_index]
    _active_session.step_started_at = datetime.now()

    buffer_minutes, next_event = await _get_calendar_buffer()
    is_late = buffer_minutes is not None and buffer_minutes < 60
    is_last = _active_session.current_step_index == len(_active_session.steps) - 1
    steps_remaining = len(_active_session.steps) - _active_session.current_step_index

    announcement = _build_step_announcement(
        step=next_step,
        buffer_minutes=buffer_minutes,
        next_event_title=next_event,
        is_first=False,
        is_last=is_last,
        is_late=is_late,
        steps_remaining=steps_remaining,
    )
    await _announce_voice(announcement, speaker=_active_session.speaker, announcement_type="routine")

    # Fire HA action
    await _fire_step_ha_action(next_step)

    # Schedule nudge
    _schedule_nudge(_active_session.nudge_delay_minutes)

    return announcement


async def _pause_routine() -> str:
    global _active_session
    if _active_session is None:
        return "No routine is active."
    _active_session.paused = True
    _cancel_nudge()
    msg = "Routine paused. Say 'resume routine' when you're ready."
    await _announce_voice(msg, speaker=_active_session.speaker, announcement_type="routine")
    return msg


async def _resume_routine() -> str:
    global _active_session
    if _active_session is None:
        return "No routine is active."
    if not _active_session.paused:
        return "Routine isn't paused."
    _active_session.paused = False
    step = _active_session.steps[_active_session.current_step_index]
    msg = f"Routine resumed. You're on: {step.label}. Let me know when you're done."
    await _announce_voice(msg, speaker=_active_session.speaker, announcement_type="routine")
    _schedule_nudge(_active_session.nudge_delay_minutes)
    return msg


async def _stop_routine() -> str:
    global _active_session
    if _active_session is None:
        return "No routine is active."

    completed = len(_active_session.completed_steps)
    total = len(_active_session.steps)
    name = _active_session.display_name

    _cancel_nudge()

    # Record progress
    _record_routine_progress()

    msg = f"{name} stopped. {completed} of {total} steps done."
    await _announce_voice(msg, speaker=_active_session.speaker, announcement_type="routine")

    _active_session = None
    return msg


async def _complete_routine() -> str:
    """All steps done — announce completion and clean up."""
    global _active_session

    completed = len(_active_session.completed_steps)
    skipped = len(_active_session.skipped_steps)
    total = len(_active_session.steps)
    name = _active_session.display_name

    # Build calendar summary for completion
    cal_summary = ""
    with contextlib.suppress(Exception):
        cal_summary = await _get_calendar_summary_text(days_ahead=0)

    parts = [f"{name} done."]
    if completed == total:
        parts.append("All steps complete.")
    else:
        parts.append(f"{completed} done, {skipped} skipped.")

    if cal_summary:
        parts.append(cal_summary)

    parts.append("Have a good one.")
    msg = " ".join(parts)

    await _announce_voice(msg, speaker=_active_session.speaker, announcement_type="routine")

    # Record progress
    _record_routine_progress()

    logger.info(
        f"[ROUTINE] Completed '{_active_session.routine_id}' ({completed}/{total} done, {skipped} skipped)",
        extra={"component": "routine"},
    )

    _active_session = None
    return msg


def _record_routine_progress():
    """Record routine completion in progress tracker (F-005)."""
    if _active_session is None:
        return
    try:
        import asyncio

        import progress_tracker

        completed = len(_active_session.completed_steps)
        progress_tracker.record_event(
            "routine_done",
            {
                "routine_id": _active_session.routine_id,
                "steps_completed": completed,
                "steps_skipped": len(_active_session.skipped_steps),
            },
        )
        asyncio.ensure_future(progress_tracker.check_and_announce_streaks())
    except Exception as e:
        logger.warning(f"[ROUTINE] Progress tracking failed: {e}")


async def get_routine_status() -> str:
    """Get current routine status."""
    if _active_session is None:
        return "No routine is active. Say 'start morning routine' or 'start evening routine'."

    step = _active_session.steps[_active_session.current_step_index]
    idx = _active_session.current_step_index + 1
    total = len(_active_session.steps)
    completed = len(_active_session.completed_steps)
    status = "paused" if _active_session.paused else "active"

    return (
        f"{_active_session.display_name} ({status}): step {idx} of {total} — {step.label}. "
        f"{completed} steps done so far."
    )


# ---------------------------------------------------------------------------
# Step announcement building
# ---------------------------------------------------------------------------


def _build_step_announcement(
    step: RoutineStep,
    buffer_minutes: Optional[int],
    next_event_title: Optional[str],
    is_first: bool,
    is_last: bool,
    is_late: bool,
    steps_remaining: int,
) -> str:
    """Build TTS-friendly step announcement."""
    name = profile.user_name
    label = step.label

    # Use fallback label if running late
    if (
        is_late
        and step.fallback_label
        and step.fallback_threshold_minutes
        and buffer_minutes is not None
        and buffer_minutes < step.fallback_threshold_minutes
    ):
        label = step.fallback_label

    if is_first:
        if is_late and next_event_title:
            return (
                f"Morning, {name}. You're running a bit behind — "
                f"{next_event_title} is coming up. "
                f"First up: {label}. That's the priority."
            )
        return f"Morning, {name}. First up: {label}. Let me know when you're done."

    if is_last:
        return f"Almost done. Last thing: {label}."

    # Middle step
    prefix = "Got it."
    if buffer_minutes is not None and next_event_title:
        return f"{prefix} Next: {label}. About {buffer_minutes} minutes before {next_event_title}."

    return f"{prefix} Next: {label}."


# ---------------------------------------------------------------------------
# Nudge scheduling
# ---------------------------------------------------------------------------


async def _deliver_nudge() -> None:
    """Called by APScheduler when nudge timer fires."""
    global _active_session
    if _active_session is None or _active_session.paused:
        return

    step = _active_session.steps[_active_session.current_step_index]
    _active_session.nudge_count += 1

    nudge_max = shared.ROUTINE_NUDGE_MAX

    # Auto-skip after max nudges if configured
    if _active_session.nudge_count > nudge_max and shared.ROUTINE_AUTO_SKIP and step.skippable:
        logger.info(f"[ROUTINE] Auto-skipping step '{step.id}' after {nudge_max} nudges")
        await advance_step("skip")
        return

    # Pick nudge template
    idx = min(_active_session.nudge_count - 1, len(_NUDGE_TEMPLATES) - 1)
    message = _NUDGE_TEMPLATES[idx].format(label=step.label)

    await _announce_voice(message, speaker=_active_session.speaker, announcement_type="routine")
    logger.info(
        f"[ROUTINE] Nudge {_active_session.nudge_count} for step '{step.id}'",
        extra={"component": "routine"},
    )


def _schedule_nudge(delay_minutes: int) -> None:
    """Schedule a recurring nudge job for the current step."""
    global _active_session
    if _active_session is None:
        return

    job_id = f"routine_nudge_{datetime.now().strftime('%H%M%S')}"
    _active_session.nudge_job_id = job_id

    shared.scheduler.add_job(
        _deliver_nudge,
        trigger="interval",
        minutes=delay_minutes,
        id=job_id,
        name="Routine step nudge",
        replace_existing=True,
    )


def _cancel_nudge() -> None:
    """Cancel the current nudge job."""
    global _active_session
    if _active_session is None or not _active_session.nudge_job_id:
        return
    with contextlib.suppress(Exception):
        shared.scheduler.remove_job(_active_session.nudge_job_id)
    _active_session.nudge_job_id = None


# ---------------------------------------------------------------------------
# HA actions
# ---------------------------------------------------------------------------


async def _fire_step_ha_action(step: RoutineStep) -> None:
    """Fire Home Assistant action for a step (e.g., turn on lights)."""
    if not step.ha_action:
        return
    try:
        from ha_integration import ha_client

        entity_id = step.ha_action.get("entity_id", "")
        service = step.ha_action.get("service", "")
        data = step.ha_action.get("data", {})
        if entity_id and service:
            result = await ha_client.call_service(entity_id, service, data)
            if result.success:
                logger.info(f"[ROUTINE] HA action: {service} on {entity_id}", extra={"component": "routine"})
            else:
                logger.warning(f"[ROUTINE] HA action failed: {result.message}")
    except Exception as e:
        logger.warning(f"[ROUTINE] HA action error: {e}")


# ---------------------------------------------------------------------------
# Calendar awareness
# ---------------------------------------------------------------------------


async def _get_calendar_buffer() -> tuple:
    """Get minutes until next calendar event. Returns (minutes, title) or (None, None)."""
    try:
        from zoneinfo import ZoneInfo

        from google_calendar import get_calendar_client

        client = get_calendar_client(http_client=shared._http)
        if not client or not client.is_configured:
            return (None, None)

        response = await client.get_upcoming(hours_ahead=4)
        if not response.success or not response.events:
            return (None, None)

        tz = ZoneInfo(shared.TIMEZONE)
        now = datetime.now(tz)
        for event in response.events:
            if event.all_day:
                continue
            minutes = int((event.start - now).total_seconds() / 60)
            if minutes > 0:
                return (minutes, event.title)

        return (None, None)
    except Exception as e:
        logger.warning(f"[ROUTINE] Calendar buffer check failed: {e}")
        return (None, None)


async def _get_calendar_summary_text(days_ahead: int = 0) -> str:
    """Get a brief calendar summary for step announcements."""
    try:
        from google_calendar import get_calendar_client

        client = get_calendar_client(http_client=shared._http)
        if not client or not client.is_configured:
            return ""

        hours = 12 if days_ahead == 0 else 36
        response = await client.get_upcoming(hours_ahead=hours)
        if not response.success or not response.events:
            return "Clear schedule ahead."

        events = [e for e in response.events if not e.all_day][:3]
        if not events:
            return "Clear schedule ahead."

        parts = []
        for e in events:
            time_str = e.start.strftime("%-I:%M %p") if hasattr(e.start, "strftime") else str(e.start)
            parts.append(f"{e.title} at {time_str}")

        return ", ".join(parts) + "."
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Prompt context injection
# ---------------------------------------------------------------------------


def get_active_routine_context() -> str:
    """Return active routine context for system prompt injection."""
    if _active_session is None:
        return ""

    step = _active_session.steps[_active_session.current_step_index]
    idx = _active_session.current_step_index + 1
    total = len(_active_session.steps)
    status = "PAUSED" if _active_session.paused else "ACTIVE"

    return (
        f"ACTIVE ROUTINE: {_active_session.display_name} ({status}, step {idx}/{total})\n"
        f'Current step: {step.label} — user can say "done", "skip", "pause routine", or "how am I on time"\n'
        f"When user says done/next/finished/skip during a routine, use the routine_action tool."
    )
