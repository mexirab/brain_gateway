"""
Sleep wind-down ladder (F-time-blindness, ROADMAP Tier-2 #4).

Two scheduled rungs ahead of the WIND_DOWN_BEDTIME anchor (default 22:30):

- T-60 (21:30): dim the house via the configured HA scene(s). Silent — it
  lands at the same moment as the evening shutdown ritual, which is the
  ladder's spoken tomorrow-preview anchor.
- T-30 (22:00): a short screens-away nudge with a one-line tomorrow anchor
  (no leave-by repeat — the evening briefing already did the full preview).

The morning half lives elsewhere: the sleep_mode tool stamps
app_state.sleep_started_at on "goodnight", and morning_briefing softens
itself when the night ran under WIND_DOWN_SHORT_NIGHT_HOURS.
"""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from orchestrator import shared
from orchestrator.metrics import (
    WIND_DOWN_DIM_LAST_RUN,
    WIND_DOWN_LAST_RUN,
    WIND_DOWN_SCENE_RESULT,
)
from orchestrator.reminder_manager import _announce_voice
from orchestrator.shared import TIMEZONE, profile

logger = logging.getLogger(__name__)


def _configured_scenes() -> list[str]:
    """Parse WIND_DOWN_SCENE, keeping only scene.* entities.

    A .env typo must not turn into a nightly turn_on of an arbitrary domain
    (switch.garage_door, lock.*, …) — drop and warn instead.
    """
    scenes = []
    for entry in (s.strip() for s in shared.WIND_DOWN_SCENE.split(",")):
        if not entry:
            continue
        if entry.startswith("scene."):
            scenes.append(entry)
        else:
            logger.warning(f"[WIND_DOWN] Ignoring non-scene entry in WIND_DOWN_SCENE: {entry}")
    return scenes


async def wind_down_dim():
    """T-60 rung: activate the configured wind-down scene(s). Silent.

    Skipped under DND — scene.turn_on can raise lights that are already off,
    which is the opposite of what an early goodnight asked for.
    """
    # Stamp the heartbeat first, before any early return: this proves the job
    # body ran even on the nights it legitimately does no work (DND, or no
    # scene configured). Without it, a scheduler that drops ONLY the dim job
    # leaves every Sleep Wind-Down panel green/empty. No stale alert watches
    # this — a lights rung that fails to dim is self-evident in the house — but
    # the dashboard panel needs a signal that separates "fired" from "acted".
    WIND_DOWN_DIM_LAST_RUN.set_to_current_time()

    scenes = _configured_scenes()
    if not scenes:
        logger.info("[WIND_DOWN] No WIND_DOWN_SCENE configured — skipping lights rung")
        return
    if shared.DND_ACTIVE:
        logger.info("[WIND_DOWN] DND active — skipping lights rung")
        return

    # Entry log: without it, a misfired/dropped job and a broken HA call are
    # indistinguishable when debugging "lights didn't dim last night".
    logger.info(f"[WIND_DOWN] Lights rung firing: {len(scenes)} scene(s)")
    for scene in scenes:
        try:
            result = await shared.ha_client.call_service(scene, "turn_on")
            if result.success:
                WIND_DOWN_SCENE_RESULT.labels(scene=scene, result="ok").inc()
                logger.info(f"[WIND_DOWN] Scene activated: {scene}")
            else:
                WIND_DOWN_SCENE_RESULT.labels(scene=scene, result="failed").inc()
                logger.warning(f"[WIND_DOWN] Scene activation failed: {scene}: {result.message}")
        except Exception as e:
            WIND_DOWN_SCENE_RESULT.labels(scene=scene, result="error").inc()
            logger.warning(f"[WIND_DOWN] Scene activation error: {scene}: {e}")


async def wind_down_nudge():
    """T-30 rung: screens-away nudge + one-line tomorrow anchor.

    Same silent-skip rules as the evening briefing: DND and an active guided
    routine session both mean "don't talk right now".
    """
    WIND_DOWN_LAST_RUN.set_to_current_time()

    try:
        if shared.DND_ACTIVE:
            logger.info("[WIND_DOWN] DND active — skipping nudge")
            return
        try:
            from orchestrator import routine_manager

            if routine_manager._active_session is not None:
                logger.info("[WIND_DOWN] Routine session active — skipping nudge")
                return
        except Exception:
            pass

        # One-line tomorrow anchor (shared source logic with evening_briefing;
        # no travel-time call — the 21:30 ritual already gave the leave-by).
        # A calendar failure must never sink the nudge.
        anchor = "Nothing early tomorrow."
        try:
            from orchestrator.jobs_calendar import get_tomorrow_events

            tz = ZoneInfo(TIMEZONE)
            events, _source = await get_tomorrow_events(tz, log_tag="WIND_DOWN")
            timed = [e for e in events if not e["all_day"]]
            if timed:
                first = timed[0]
                time_str = first["start"].strftime("%I:%M %p").lstrip("0")
                anchor = f"Tomorrow's first thing is {first['title']} at {time_str} — you're covered."
        except Exception as cal_err:
            logger.warning(f"[WIND_DOWN] Tomorrow lookup failed: {cal_err}")
            anchor = ""

        text = f"Screens away, {profile.user_name}. {anchor} Time to wind down.".replace("  ", " ")
        result = await _announce_voice(text, speaker=None, announcement_type="briefing")

        if result.get("suppressed"):
            outcome = f"suppressed({result.get('reason', '?')})"
        elif result.get("success"):
            outcome = "delivered"
        else:
            outcome = f"failed({result.get('error', '?')})"
        logger.info(f"[WIND_DOWN] Nudge {outcome}")

    except Exception as e:
        logger.error(f"[WIND_DOWN] Error: {e}")
