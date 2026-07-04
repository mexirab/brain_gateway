"""
Background jobs: temperature monitoring, ambient awareness, self-care nudges,
routine scaffolding triggers, progress summaries.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from orchestrator import shared, state_store
from orchestrator.metrics import TEMPERATURE_DELTA, TEMPERATURE_GAUGE
from orchestrator.reminder_manager import _announce_voice
from orchestrator.shared import profile

logger = logging.getLogger(__name__)


async def check_closet_temperature():
    """Every 10 minutes: check closet temperature and alert if too hot.

    Thresholds:
    - 80 degrees F: warning (GPU heat building up)
    - 85 degrees F: urgent (risk of thermal throttling)
    """
    from orchestrator.shared import HA_TOKEN, HA_URL

    try:
        resp = await shared._http.get(
            f"{HA_URL}/api/states/{profile.closet_temp_sensor}",
            headers={"Authorization": f"Bearer {HA_TOKEN}"},
            timeout=5.0,
        )
        if resp.status_code != 200:
            return

        raw_state = resp.json().get("state")
        if raw_state in (None, "", "unknown", "unavailable"):
            # HA reports these while a sensor is offline/rebooting — routine,
            # not an error. Skip silently; this job runs every 10 minutes.
            return
        temp = float(raw_state)
        TEMPERATURE_GAUGE.labels(location="closet").set(temp)

        # Also grab ambient for delta tracking
        try:
            resp2 = await shared._http.get(
                f"{HA_URL}/api/states/{profile.ambient_temp_sensor}",
                headers={"Authorization": f"Bearer {HA_TOKEN}"},
                timeout=5.0,
            )
            if resp2.status_code == 200:
                raw_kitchen = resp2.json().get("state")
                if raw_kitchen not in (None, "", "unknown", "unavailable"):
                    kitchen_temp = float(raw_kitchen)
                    TEMPERATURE_GAUGE.labels(location="kitchen").set(kitchen_temp)
                    TEMPERATURE_DELTA.set(temp - kitchen_temp)
        except Exception:
            pass

        # Alert thresholds (only alert once per crossing, persisted via state_store)
        if temp >= 85 and not state_store.is_notified("temp:closet_85"):
            await _announce_voice(
                f"Warning! Server closet temperature is {temp:.0f} degrees. "
                f"That's dangerously hot. Check the ventilation or shut down non-essential nodes.",
                announcement_type="temperature",
            )
            state_store.mark_notified("temp:closet_85")
            logger.warning(f"[TEMP_ALERT] Closet at {temp}°F — URGENT alert sent")

        elif temp >= 80 and not state_store.is_notified("temp:closet_80"):
            await _announce_voice(
                f"Heads up {profile.user_name}. The server closet is at {temp:.0f} degrees. "
                f"That's getting warm. You might want to check the airflow.",
                announcement_type="temperature",
            )
            state_store.mark_notified("temp:closet_80")
            logger.warning(f"[TEMP_ALERT] Closet at {temp}°F — warning alert sent")

        elif temp < 78:
            # Clear alerts when cooled down — allows re-alerting if it heats up again
            cleared = state_store.clear_notifications_by_prefix("temp:")
            if cleared:
                logger.info(f"[TEMP_ALERT] Closet cooled to {temp}°F — alerts cleared")

    except Exception as e:
        logger.error(f"[TEMP_ALERT] Error: {e}")


# ---------------------------------------------------------------------------
# Ambient Awareness (F-010)
# ---------------------------------------------------------------------------


async def ambient_summary():
    """Announce brief ambient status summary via TTS."""
    try:
        # Respect quiet hours
        from datetime import time as _time

        from orchestrator.ambient_manager import build_ambient_summary_text

        tz = ZoneInfo(shared.TIMEZONE)
        now_tz = datetime.now(tz)
        quiet_start = _time(int(shared.QUIET_HOURS_START.split(":")[0]), int(shared.QUIET_HOURS_START.split(":")[1]))
        quiet_end = _time(int(shared.QUIET_HOURS_END.split(":")[0]), int(shared.QUIET_HOURS_END.split(":")[1]))
        current = now_tz.time()
        if quiet_start <= quiet_end:
            if quiet_start <= current <= quiet_end:
                return
        elif current >= quiet_start or current <= quiet_end:
            return

        summary = await build_ambient_summary_text()
        speaker = shared.AMBIENT_SPEAKER or None
        await _announce_voice(summary, speaker=speaker, announcement_type="ambient")
        logger.info("[AMBIENT] Summary announced")
    except Exception as e:
        logger.error(f"[AMBIENT] Summary failed: {e}")


async def update_ambient_led():
    """Update LED status indicator based on current state."""
    try:
        from orchestrator.ambient_manager import get_ambient_status, set_ambient_led

        status = await get_ambient_status()
        await set_ambient_led(status["led_color"])
    except Exception as e:
        logger.error(f"[AMBIENT] LED update failed: {e}")


# ---------------------------------------------------------------------------
# Self-Care Nudges (F-008)
# ---------------------------------------------------------------------------


async def check_selfcare():
    """Every 15 min: check meal, meds, hydration, movement."""
    try:
        from orchestrator.selfcare_manager import check_selfcare as _check

        await _check()
    except Exception as e:
        logger.error(f"[SELFCARE] Check failed: {e}")


# ---------------------------------------------------------------------------
# Routine Scaffolding (F-006)
# ---------------------------------------------------------------------------


async def trigger_routine(routine_id: str):
    """Called by APScheduler at routine trigger time."""
    try:
        from orchestrator.routine_manager import _active_session, start_routine

        if _active_session is not None:
            logger.info(f"[ROUTINE] Skipping scheduled trigger for '{routine_id}' — session already active")
            return
        if shared.current_focus_session.get("active"):
            logger.info(f"[ROUTINE] Skipping scheduled trigger for '{routine_id}' — focus session active")
            return
        logger.info(f"[ROUTINE] Scheduled trigger: {routine_id}")
        result = await start_routine(routine_id, triggered_by="scheduled")
        logger.info(f"[ROUTINE] Started: {result[:80]}")
    except Exception as e:
        logger.error(f"[ROUTINE] Scheduled trigger failed for '{routine_id}': {e}")


async def daily_progress_summary():
    """Announce daily progress stats via TTS at configured time."""
    try:
        from orchestrator import progress_tracker

        summary = await progress_tracker.daily_summary()
        await _announce_voice(summary, announcement_type="progress")
        logger.info("[PROGRESS] Daily summary announced")
    except Exception as e:
        logger.error(f"[PROGRESS] Daily summary failed: {e}")


async def weekly_progress_digest():
    """Announce weekly progress digest via TTS."""
    try:
        from orchestrator import progress_tracker

        summary = await progress_tracker.weekly_summary()
        await _announce_voice(summary, announcement_type="progress")
        logger.info("[PROGRESS] Weekly digest announced")
    except Exception as e:
        logger.error(f"[PROGRESS] Weekly digest failed: {e}")
