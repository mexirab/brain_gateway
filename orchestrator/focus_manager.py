"""
Focus timer (Pomodoro) management: timer, Endel audio, Pi-hole blocking, break delivery.
"""

import logging
import random
from typing import Optional
from datetime import datetime, timedelta

import shared
from shared import (
    ha_client, scheduler, current_focus_session,
    ENDEL_API_URL, ENDEL_MODES, FOCUS_AUDIO_PLAYER, ENDEL_ENABLED,
    profile,
)
import state_store
from pihole_client import get_pihole_client
from reminder_manager import _announce_voice
from metrics import (
    FOCUS_SESSIONS_STARTED, FOCUS_SESSIONS_COMPLETED, FOCUS_SESSIONS_STOPPED_EARLY,
    FOCUS_SESSION_DURATION, FOCUS_ACTIVE, PIHOLE_BLOCKING_TOGGLES,
)

logger = logging.getLogger(__name__)


def resolve_speaker_entity(speaker_name: str) -> Optional[str]:
    """Map friendly speaker names to entity IDs (from user profile)."""
    aliases = profile.speaker_aliases
    name_lower = speaker_name.lower().strip()
    if name_lower in aliases:
        return aliases[name_lower]
    if speaker_name.startswith("media_player."):
        return speaker_name
    for entity in ha_client.get_entities_by_domain("media_player"):
        if name_lower in entity.friendly_name.lower():
            return entity.entity_id
    return None


async def get_endel_focus_url(duration_minutes: int, mode: str = "focus") -> Optional[str]:
    """Fetch Endel HLS playlist and extract direct audio URL for Cast devices."""
    if mode not in ENDEL_MODES:
        mode = "focus"
    hour = datetime.now().hour
    playlist_url = f"{ENDEL_API_URL}?mode={mode}&hour={hour}&hlsjs=1&duration={duration_minutes}"

    try:
        resp = await shared._http.get(playlist_url, timeout=10)
        resp.raise_for_status()

        audio_urls = []
        for line in resp.text.split('\n'):
            line = line.strip()
            if line and not line.startswith('#'):
                audio_urls.append(line)

        if audio_urls:
            logger.info(f"[ENDEL] Extracted {len(audio_urls)} audio URLs from playlist")
            return audio_urls[0]
        else:
            logger.warning("[ENDEL] No audio URLs found in playlist")
            return None
    except Exception as e:
        logger.error(f"[ENDEL] Failed to fetch playlist: {e}")
        return None


async def start_focus_audio(duration_minutes: int, player: str, soundscape: str = "focus") -> bool:
    """Start Endel focus audio on specified media player."""
    if not ENDEL_ENABLED:
        logger.info("[FOCUS] Endel audio disabled via ENDEL_ENABLED=false")
        return False

    url = await get_endel_focus_url(duration_minutes, soundscape)
    if not url:
        return False

    logger.info(f"[FOCUS] Starting Endel {soundscape} audio on {player}: {url}")

    result = await ha_client.call_service(
        player,
        "play_media",
        {"media_content_id": url, "media_content_type": "music"}
    )
    if not result.success:
        logger.error(f"[FOCUS] Failed to start audio on {player}: {result.message}")
    return result.success


async def stop_focus_audio(player: str = None) -> bool:
    """Stop audio playback on media player."""
    player = player or FOCUS_AUDIO_PLAYER
    logger.info(f"[FOCUS] Stopping audio on {player}")
    result = await ha_client.call_service(player, "media_stop", {})
    if not result.success:
        logger.error(f"[FOCUS] Failed to stop audio on {player}: {result.message}")
    return result.success


async def tool_start_focus(task: str, duration: int = 25, break_duration: int = 5,
                           speaker: str = None, soundscape: str = "focus",
                           block_sites: bool = True) -> str:
    """Start a focus timer with voice announcement at end, optional Endel audio, and distraction blocking."""
    # Validate duration bounds
    try:
        duration = int(duration)
        break_duration = int(break_duration)
    except (TypeError, ValueError):
        return "Duration must be a number."
    if duration < 1 or duration > 480:
        return "Focus duration must be between 1 and 480 minutes (8 hours max)."
    if break_duration < 1 or break_duration > 60:
        return "Break duration must be between 1 and 60 minutes."

    if current_focus_session["active"]:
        elapsed = (datetime.now() - current_focus_session["started"]).total_seconds() / 60
        remaining = current_focus_session["duration"] - elapsed
        return f"You're already focusing on '{current_focus_session['task']}' with {remaining:.0f} minutes left. Say 'stop focus' to end early."

    # Resolve speaker
    player = None
    if speaker:
        player = resolve_speaker_entity(speaker)
        if not player:
            return f"I couldn't find a speaker matching '{speaker}'. Try 'office', 'bedroom', or 'kitchen'."
    else:
        player = FOCUS_AUDIO_PLAYER

    # Start Endel focus audio
    audio_started = False
    if soundscape != "none" and player:
        audio_started = await start_focus_audio(duration, player, soundscape)
        if audio_started:
            current_focus_session["audio_player"] = player
            logger.info(f"[FOCUS] Started Endel {soundscape} audio on {player}")
        else:
            logger.warning(f"[FOCUS] Endel {soundscape} audio failed to start on {player}")

    # Enable site blocking
    blocking_enabled = False
    if block_sites:
        pihole = get_pihole_client()
        result = await pihole.enable_focus_blocking()
        if result.success:
            blocking_enabled = True
            PIHOLE_BLOCKING_TOGGLES.labels(action="enable").inc()
            logger.info("[FOCUS] Enabled Pi-hole distraction blocking")
        else:
            logger.warning(f"[FOCUS] Could not enable blocking: {result.message}")

    # Schedule break announcement
    end_time = datetime.now() + timedelta(minutes=duration)
    job_id = f"focus_{datetime.now().strftime('%H%M%S')}"

    scheduler.add_job(
        deliver_focus_break,
        trigger='date',
        run_date=end_time,
        args=[task, break_duration],
        id=job_id,
        replace_existing=True,
    )

    current_focus_session.update({
        "active": True,
        "task": task,
        "started": datetime.now(),
        "duration": duration,
        "break_duration": break_duration,
        "job_id": job_id,
        "audio_player": player if audio_started else None,
        "block_sites": blocking_enabled,
    })
    state_store.save_focus_session(current_focus_session)

    FOCUS_SESSIONS_STARTED.labels(soundscape=soundscape).inc()
    FOCUS_ACTIVE.set(1)
    logger.info(f"[FOCUS] Started {duration}min focus on '{task}', break at {end_time.strftime('%H:%M')}",
                extra={"component": "focus"})

    parts = []
    if audio_started:
        speaker_name = player.replace("media_player.", "").replace("_", " ")
        parts.append(f"Focus timer started with Endel {soundscape} sounds on the {speaker_name}!")
    else:
        parts.append("Focus timer started!")

    parts.append(f"You have {duration} minutes to work on '{task}'.")

    if blocking_enabled:
        parts.append("Distracting sites are blocked.")

    parts.append("I'll let you know when it's break time.")

    return " ".join(parts)


async def tool_stop_focus() -> str:
    """Stop the current focus timer."""
    if not current_focus_session["active"]:
        return "No focus timer is running."

    task = current_focus_session["task"]
    elapsed = (datetime.now() - current_focus_session["started"]).total_seconds() / 60

    if current_focus_session.get("audio_player"):
        await stop_focus_audio(current_focus_session["audio_player"])
        logger.info("[FOCUS] Stopped Endel audio")

    if current_focus_session.get("block_sites"):
        pihole = get_pihole_client()
        result = await pihole.disable_focus_blocking()
        if result.success:
            PIHOLE_BLOCKING_TOGGLES.labels(action="disable").inc()
            logger.info("[FOCUS] Disabled Pi-hole distraction blocking")
        else:
            logger.warning(f"[FOCUS] Could not disable blocking: {result.message}")

    try:
        scheduler.remove_job(current_focus_session["job_id"])
    except Exception:
        pass

    _reset_focus_session()

    FOCUS_SESSIONS_STOPPED_EARLY.inc()
    FOCUS_SESSION_DURATION.observe(elapsed)
    FOCUS_ACTIVE.set(0)
    logger.info(f"[FOCUS] Stopped early after {elapsed:.0f}min on '{task}'",
                extra={"component": "focus"})
    return f"Focus timer stopped. You worked on '{task}' for {elapsed:.0f} minutes. Nice work!"


async def tool_focus_status() -> str:
    """Check current focus timer status."""
    if not current_focus_session["active"]:
        return "No focus timer is running. Say 'start focus on [task]' to begin."

    elapsed = (datetime.now() - current_focus_session["started"]).total_seconds() / 60
    remaining = current_focus_session["duration"] - elapsed

    if remaining <= 0:
        return f"Your focus session on '{current_focus_session['task']}' just ended!"

    return f"You're focusing on '{current_focus_session['task']}'. {remaining:.0f} minutes left, {elapsed:.0f} minutes in."


async def deliver_focus_break(task: str, break_duration: int):
    """Called by scheduler when focus time ends."""
    # Stop Endel audio first
    if current_focus_session.get("audio_player"):
        await stop_focus_audio(current_focus_session["audio_player"])
        logger.info("[FOCUS] Stopped Endel audio before break announcement")

    # Disable site blocking during break
    if current_focus_session.get("block_sites"):
        pihole = get_pihole_client()
        result = await pihole.disable_focus_blocking()
        if result.success:
            PIHOLE_BLOCKING_TOGGLES.labels(action="disable").inc()
            logger.info("[FOCUS] Disabled Pi-hole blocking for break")
        else:
            logger.warning(f"[FOCUS] Could not disable blocking for break: {result.message}")

    messages = [
        f"Great focus session on {task}! Take a {break_duration} minute break. Stretch, grab water, rest your eyes.",
        f"Time's up! You crushed it working on {task}. {break_duration} minute break - you've earned it!",
        f"Focus session complete! Step away from {task} for {break_duration} minutes. Move around, breathe.",
        f"Nice work on {task}! Your brain needs a {break_duration} minute reset. Get up and stretch!",
        f"Pomodoro done! Great job on {task}. Take {break_duration} minutes to recharge."
    ]
    message = random.choice(messages)

    await _announce_voice(message)

    # Record duration before resetting
    planned_duration = current_focus_session.get("duration")

    _reset_focus_session()

    FOCUS_SESSIONS_COMPLETED.inc()
    FOCUS_ACTIVE.set(0)
    if planned_duration:
        FOCUS_SESSION_DURATION.observe(planned_duration)
    logger.info(f"[FOCUS] Break announced for '{task}'", extra={"component": "focus"})


def _reset_focus_session():
    """Reset focus session state to inactive."""
    current_focus_session.update({
        "active": False,
        "task": None,
        "started": None,
        "duration": None,
        "break_duration": None,
        "job_id": None,
        "audio_player": None,
        "block_sites": False,
    })
    state_store.clear_focus_session()
