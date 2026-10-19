"""Reminder Done / Snooze — the one implementation every channel calls.

Callers own their transport and auth: the F-011/F-013 HMAC callback routes
(``api_routes``), the Telegram callback handler (allow-listed chat), and the
dashboard inbox routes (bearer). They map the returned ``status`` onto their
own response shape. Before this module the Telegram handler was a hand copy
of the HTTP routes and had already drifted (no ack-latency metric).

Both functions resolve the matching dashboard inbox card, so a tap on any
channel clears it everywhere.
"""

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from orchestrator import shared, state_store

logger = logging.getLogger(__name__)

SNOOZE_MIN_MINUTES = 1
SNOOZE_MAX_MINUTES = 120


def clamp_snooze_minutes(minutes: int) -> int:
    return max(SNOOZE_MIN_MINUTES, min(SNOOZE_MAX_MINUTES, int(minutes)))


def _cancel_retry(reminder_id: str) -> None:
    """Drop a pending TTS-failure retry so the reminder doesn't fire again."""
    retry_job_id = f"reminder_{reminder_id}_retry"
    scheduler = shared.scheduler
    try:
        if scheduler and scheduler.get_job(retry_job_id):
            scheduler.remove_job(retry_job_id)
            logger.info("[REMINDER] Cancelled retry job for %s", reminder_id)
    except Exception as job_err:
        logger.warning("[REMINDER] Failed to cancel retry job for %s: %s", reminder_id, type(job_err).__name__)


def _resolve_inbox(reminder_id: str, resolution: str, via: str) -> None:
    try:
        from orchestrator import inbox

        inbox.resolve_reminder(reminder_id, resolution=resolution, via=via)
    except Exception as e:
        logger.warning("[REMINDER] Inbox resolve failed for %s: %s", reminder_id, type(e).__name__)


def _observe_ack_latency(row: Dict[str, Any]) -> None:
    from orchestrator.metrics import REMINDER_ACK_LATENCY

    with contextlib.suppress(Exception):
        trig = row.get("trigger_time")
        if trig:
            elapsed = (datetime.now() - datetime.fromisoformat(trig)).total_seconds()
            if elapsed >= 0:
                REMINDER_ACK_LATENCY.observe(elapsed)


async def _pantry_done(row: Dict[str, Any]) -> None:
    """Done on a meal-plan cooking reminder = "cooked it" (F-015 phase 4).
    Off the event loop: the cook is a BEGIN IMMEDIATE + fuzzy matching."""
    try:
        from orchestrator.pantry_consumption import on_reminder_done

        await asyncio.to_thread(on_reminder_done, row)
    except Exception as e:  # the ack itself must still succeed
        logger.error("[REMINDER] Pantry cook hook failed: %s", type(e).__name__, exc_info=True)


def _selfcare_bridge(reminder_id: str, row: Dict[str, Any]) -> Optional[str]:
    """Log the selfcare action a reminder's text implies (water, meds, ...).
    Returns the action, or None. A bridge failure is loud but the ack stands."""
    from orchestrator.reminder_manager import selfcare_action_for_reminder

    try:
        action = selfcare_action_for_reminder(row)
    except Exception as e:
        logger.error("[REMINDER] Selfcare inference failed for %s: %s", reminder_id, type(e).__name__, exc_info=True)
        return None
    if not action:
        return None
    try:
        from orchestrator import selfcare_manager

        selfcare_manager.record_selfcare(action, f"reminder:{(row.get('text') or '')[:80]}")
    except Exception as e:
        logger.error("[REMINDER] Selfcare bridge failed for %s: %s", reminder_id, type(e).__name__, exc_info=True)
    return action


async def ack_reminder(reminder_id: str, via: str) -> Dict[str, Any]:
    """Mark a reminder done.

    Returns ``{"status": "not_found"}``, ``{"status": "already_acked", "reminder": row}``
    (idempotent replay: nothing re-fires), or
    ``{"status": "ok", "reminder": row, "action": <selfcare action or None>}``.
    """
    # SQLite off the loop: a WAL checkpoint or a pantry BEGIN IMMEDIATE can
    # make these wait on a lock.
    row = await asyncio.to_thread(state_store.mark_reminder_acked, reminder_id, via)
    if row is None:
        return {"status": "not_found"}
    if row.get("already_acked"):
        logger.info("[REMINDER] %s already acked; idempotent replay (via=%s)", reminder_id, via)
        await asyncio.to_thread(_resolve_inbox, reminder_id, "done", via)
        return {"status": "already_acked", "reminder": row}

    _cancel_retry(reminder_id)
    _observe_ack_latency(row)
    await _pantry_done(row)
    # On the loop: the recorders mutate selfcare_manager's in-memory gates.
    action = _selfcare_bridge(reminder_id, row)
    await asyncio.to_thread(_resolve_inbox, reminder_id, "done", via)
    logger.info("[REMINDER] %s acked via=%s selfcare=%s", reminder_id, via, action or "none")
    return {"status": "ok", "reminder": row, "action": action}


async def snooze_reminder(reminder_id: str, minutes: int, via: str) -> Dict[str, Any]:
    """Re-deliver a reminder ``minutes`` from now (clamped to 1–120).

    Returns ``{"status": "not_found"}``, ``{"status": "limit", "snooze_count": n}``
    (NTFY_MAX_SNOOZE_COUNT reached), ``{"status": "error"}`` (reschedule failed),
    or ``{"status": "ok", "run_at": datetime, "minutes": m, "snooze_count": n}``.
    """
    from orchestrator.config import settings

    minutes = clamp_snooze_minutes(minutes)
    reminder = await asyncio.to_thread(state_store.get_reminder, reminder_id)
    if reminder is None:
        return {"status": "not_found"}
    current = reminder.get("snooze_count") or 0
    if current >= settings.ntfy_max_snooze_count:
        return {"status": "limit", "snooze_count": current}

    from orchestrator.tool_handlers import deliver_reminder_job

    run_at = datetime.now(ZoneInfo(shared.TIMEZONE)) + timedelta(minutes=minutes)
    try:
        shared.scheduler.add_job(
            deliver_reminder_job,
            trigger="date",
            run_date=run_at,
            args=[reminder_id],
            id=f"reminder_{reminder_id}",
            replace_existing=True,
        )
    except Exception as sch_err:
        logger.error("[REMINDER] Reschedule failed for %s: %s", reminder_id, type(sch_err).__name__, exc_info=True)
        return {"status": "error"}

    # Delivery already marked this reminder 'completed', and
    # deliver_reminder_job skips anything non-pending — without this reset the
    # snoozed job fires into the guard and the reminder never comes back
    # (while the user holds a "Snoozed until 3:10" confirmation).
    await asyncio.to_thread(state_store.reopen_reminder, reminder_id)
    # The snooze supersedes any pending TTS-failure retry; kill it so the
    # reminder doesn't fire twice (once at +2min from the retry, once at the
    # snoozed time).
    _cancel_retry(reminder_id)
    new_count = await asyncio.to_thread(state_store.increment_snooze_count, reminder_id)
    await asyncio.to_thread(_resolve_inbox, reminder_id, "snoozed", via)
    logger.info("[REMINDER] %s snoozed %dm via=%s (count=%s)", reminder_id, minutes, via, new_count)
    return {"status": "ok", "run_at": run_at, "minutes": minutes, "snooze_count": new_count}
