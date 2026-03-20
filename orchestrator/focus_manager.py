"""
Focus timer (Pomodoro) management: timer, audio, Pi-hole blocking, check-ins, sprints.

F-004 extends the original focus timer with body doubling features:
periodic check-ins, multi-sprint sessions, flexible audio sources, and session summaries.
"""

import contextlib
import logging
import random
from datetime import datetime, timedelta
from typing import Optional

import shared
import state_store
from metrics import (
    FOCUS_ACTIVE,
    FOCUS_SESSION_DURATION,
    FOCUS_SESSIONS_COMPLETED,
    FOCUS_SESSIONS_STARTED,
    FOCUS_SESSIONS_STOPPED_EARLY,
    PIHOLE_BLOCKING_TOGGLES,
)
from pihole_client import get_pihole_client
from reminder_manager import _announce_voice
from shared import (
    ENDEL_API_URL,
    ENDEL_ENABLED,
    ENDEL_MODES,
    FOCUS_AUDIO_COFFEE_URL,
    FOCUS_AUDIO_LOFI_URL,
    FOCUS_AUDIO_PLAYER,
    current_focus_session,
    ha_client,
    profile,
    scheduler,
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
        for line in resp.text.split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
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
        player, "play_media", {"media_content_id": url, "media_content_type": "music"}
    )
    if not result.success:
        logger.error(f"[FOCUS] Failed to start audio on {player}: {result.message}")
    return result.success


def _is_safe_stream_url(url: str) -> bool:
    """Validate that a stream URL uses http(s) and has a host."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


async def start_focus_stream_audio(url: str, player: str) -> bool:
    """Start a stream URL (lo-fi, coffee shop) on a media player via HA."""
    if not url:
        logger.info("[FOCUS] Stream URL not configured, skipping audio")
        return False
    if not _is_safe_stream_url(url):
        logger.warning(f"[FOCUS] Rejected unsafe stream URL: {url!r}")
        return False
    logger.info(f"[FOCUS] Starting stream audio on {player}")
    result = await ha_client.call_service(
        player, "play_media", {"media_content_id": url, "media_content_type": "music"}
    )
    if not result.success:
        logger.error(f"[FOCUS] Failed to start stream audio on {player}: {result.message}")
    return result.success


async def stop_focus_audio(player: str = None) -> bool:
    """Stop audio playback on media player."""
    player = player or FOCUS_AUDIO_PLAYER
    logger.info(f"[FOCUS] Stopping audio on {player}")
    result = await ha_client.call_service(player, "media_stop", {})
    if not result.success:
        logger.error(f"[FOCUS] Failed to stop audio on {player}: {result.message}")
    return result.success


async def deliver_check_in():
    """Called by APScheduler on interval during an active focus session."""
    try:
        if not current_focus_session["active"]:
            return

        task_desc = current_focus_session.get("task_description") or current_focus_session.get("task")

        if task_desc:
            message = f"How's it going? Still working on {task_desc}? Keep it up."
        else:
            message = "Still in the zone? Keep going — you're doing great."

        logger.info(f"[FOCUS] Check-in: task='{task_desc}'", extra={"component": "focus"})
        await _announce_voice(message)
    except Exception as e:
        logger.error(f"[FOCUS] Check-in delivery failed: {e}", extra={"component": "focus"})


async def _start_audio_for_source(audio_source: str, player: str, duration: int, soundscape: str) -> bool:
    """Start the appropriate audio based on source type."""
    if audio_source == "lofi":
        return await start_focus_stream_audio(FOCUS_AUDIO_LOFI_URL, player)
    elif audio_source == "coffee_shop":
        return await start_focus_stream_audio(FOCUS_AUDIO_COFFEE_URL, player)
    elif audio_source == "silence":
        return False
    else:
        # "endel" or legacy
        return await start_focus_audio(duration, player, soundscape)


async def tool_start_focus(
    task: str,
    duration: int = 25,
    break_duration: int = 5,
    speaker: str = None,
    soundscape: str = "focus",
    block_sites: bool = True,
    check_ins: bool = True,
    check_in_interval: int = 15,
    audio: str = None,
    sprints: int = 1,
) -> str:
    """Start a focus timer with optional body doubling features.

    Backward compatible — existing calls with no new params work identically.
    """
    # Validate duration bounds
    try:
        duration = int(duration)
        break_duration = int(break_duration)
        sprints = int(sprints)
        check_in_interval = int(check_in_interval)
    except (TypeError, ValueError):
        return "Duration must be a number."
    if duration < 1 or duration > 480:
        return "Focus duration must be between 1 and 480 minutes (8 hours max)."
    if break_duration < 1 or break_duration > 60:
        return "Break duration must be between 1 and 60 minutes."
    sprints = max(1, min(sprints, 10))
    check_in_interval = max(5, min(check_in_interval, 120))

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

    # Resolve effective audio source (F-004: audio param overrides soundscape)
    effective_audio = audio if audio in ("endel", "lofi", "coffee_shop", "silence") else "endel"

    # Start audio
    audio_started = False
    if effective_audio != "silence" and player:
        if soundscape == "none" and not audio:
            pass  # user explicitly said no soundscape in legacy mode
        else:
            audio_started = await _start_audio_for_source(effective_audio, player, duration, soundscape)
            if audio_started:
                logger.info(f"[FOCUS] Started {effective_audio} audio on {player}")
            else:
                logger.warning(f"[FOCUS] {effective_audio} audio failed to start on {player}")

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
        trigger="date",
        run_date=end_time,
        args=[task, break_duration],
        id=job_id,
        replace_existing=True,
    )

    # Schedule check-in job (F-004)
    check_in_job_id = None
    if check_ins and check_in_interval > 0:
        check_in_job_id = f"focus_checkin_{datetime.now().strftime('%H%M%S')}"
        scheduler.add_job(
            deliver_check_in,
            trigger="interval",
            minutes=check_in_interval,
            id=check_in_job_id,
            replace_existing=True,
        )
        logger.info(f"[FOCUS] Scheduled check-ins every {check_in_interval}min (job: {check_in_job_id})")

    current_focus_session.update(
        {
            "active": True,
            "task": task,
            "started": datetime.now(),
            "duration": duration,
            "break_duration": break_duration,
            "job_id": job_id,
            "audio_player": player if audio_started else None,
            "block_sites": blocking_enabled,
            # F-004 additions
            "task_description": task,
            "sprint_count": 0,
            "sprints_planned": sprints if sprints > 1 else None,
            "check_in_interval": check_in_interval if check_ins else None,
            "check_in_job_id": check_in_job_id,
            "total_focus_minutes": 0,
            "audio_source": effective_audio,
        }
    )
    state_store.save_focus_session(current_focus_session)

    # Record context for interruption recovery (F-007)
    try:
        import asyncio as _asyncio

        import context_tracker as _ct

        _asyncio.ensure_future(_ct.record_context(description=task, focus_session_id=job_id))
    except Exception as e:
        logger.warning(f"[FOCUS] Context tracking failed: {e}")

    FOCUS_SESSIONS_STARTED.labels(soundscape=effective_audio).inc()
    FOCUS_ACTIVE.set(1)
    logger.info(
        f"[FOCUS] Started {duration}min focus on '{task}', break at {end_time.strftime('%H:%M')}",
        extra={"component": "focus"},
    )

    # Build response
    parts = []
    if audio_started:
        speaker_name = player.replace("media_player.", "").replace("_", " ")
        audio_labels = {"endel": f"Endel {soundscape} sounds", "lofi": "lo-fi", "coffee_shop": "coffee shop sounds"}
        audio_label = audio_labels.get(effective_audio, effective_audio)
        parts.append(f"Focus session started with {audio_label} on the {speaker_name}.")
    else:
        parts.append("Focus session started.")

    if sprints > 1:
        parts.append(f"Working on {task} — {sprints} sprints of {duration} minutes.")
    else:
        parts.append(f"You have {duration} minutes to work on '{task}'.")

    if blocking_enabled:
        parts.append("Distracting sites are blocked.")

    if check_ins and check_in_interval > 0:
        parts.append(f"I'll check in every {check_in_interval} minutes.")
    else:
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
        logger.info("[FOCUS] Stopped audio")

    if current_focus_session.get("block_sites"):
        pihole = get_pihole_client()
        result = await pihole.disable_focus_blocking()
        if result.success:
            PIHOLE_BLOCKING_TOGGLES.labels(action="disable").inc()
            logger.info("[FOCUS] Disabled Pi-hole distraction blocking")
        else:
            logger.warning(f"[FOCUS] Could not disable blocking: {result.message}")

    # Cancel check-in job
    if current_focus_session.get("check_in_job_id"):
        with contextlib.suppress(Exception):
            scheduler.remove_job(current_focus_session["check_in_job_id"])

    with contextlib.suppress(Exception):
        scheduler.remove_job(current_focus_session["job_id"])

    total = current_focus_session.get("total_focus_minutes", 0) + int(elapsed)
    sprint_count = current_focus_session.get("sprint_count", 0)

    _reset_focus_session()

    FOCUS_SESSIONS_STOPPED_EARLY.inc()
    FOCUS_SESSION_DURATION.observe(total or elapsed)
    FOCUS_ACTIVE.set(0)

    # Record partial focus event (F-005) — only if meaningful time spent
    if int(elapsed) >= 5:
        try:
            import progress_tracker

            progress_tracker.record_event("focus_partial", {"minutes": int(elapsed)})
        except Exception as e:
            logger.warning(f"[FOCUS] Progress tracking failed: {e}")

    logger.info(f"[FOCUS] Stopped early after {elapsed:.0f}min on '{task}'", extra={"component": "focus"})

    if sprint_count > 0:
        return f"Focus session stopped. You worked on '{task}' for {elapsed:.0f} minutes this sprint, {total} minutes total across {sprint_count} completed sprints. Good work!"
    return f"Focus timer stopped. You worked on '{task}' for {elapsed:.0f} minutes. Nice work!"


async def tool_focus_status() -> str:
    """Check current focus timer status."""
    if not current_focus_session["active"]:
        return "No focus timer is running. Say 'start focus on [task]' to begin."

    elapsed = (datetime.now() - current_focus_session["started"]).total_seconds() / 60
    remaining = current_focus_session["duration"] - elapsed

    if remaining <= 0:
        return f"Your focus session on '{current_focus_session['task']}' just ended!"

    sprint_info = ""
    sprints_planned = current_focus_session.get("sprints_planned")
    if sprints_planned:
        sprint_num = current_focus_session.get("sprint_count", 0) + 1
        sprint_info = f" Sprint {sprint_num} of {sprints_planned}."

    return (
        f"You're focusing on '{current_focus_session['task']}'. "
        f"{remaining:.0f} minutes left, {elapsed:.0f} minutes in.{sprint_info}"
    )


async def tool_focus_sprint(action: str, duration_minutes: int = None) -> str:
    """Handle sprint transitions: next_sprint, extend, or end_session."""
    if not current_focus_session.get("active"):
        return "No active focus session. Use 'start focus' to begin."

    if action == "end_session":
        summary_text = _build_session_summary()
        if current_focus_session.get("audio_player"):
            await stop_focus_audio(current_focus_session["audio_player"])
        if current_focus_session.get("block_sites"):
            pihole = get_pihole_client()
            await pihole.disable_focus_blocking()
            PIHOLE_BLOCKING_TOGGLES.labels(action="disable").inc()
        for job_key in ("job_id", "check_in_job_id"):
            jid = current_focus_session.get(job_key)
            if jid:
                with contextlib.suppress(Exception):
                    scheduler.remove_job(jid)
        total = current_focus_session.get("total_focus_minutes", 0)
        sprints_done = current_focus_session.get("sprint_count", 0)
        FOCUS_SESSION_DURATION.observe(total or current_focus_session.get("duration", 0))
        FOCUS_SESSIONS_COMPLETED.inc()

        # Record focus completion (F-005)
        try:
            import asyncio as _asyncio

            import progress_tracker

            progress_tracker.record_event("focus_complete", {"minutes": total, "sprints": sprints_done})
            _asyncio.ensure_future(progress_tracker.check_and_announce_streaks())
        except Exception as e:
            logger.warning(f"[FOCUS] Progress tracking failed: {e}")

        _reset_focus_session()
        FOCUS_ACTIVE.set(0)
        await _announce_voice(summary_text)
        return summary_text

    elif action == "extend":
        extra = duration_minutes or 10
        job_id = current_focus_session.get("job_id")
        if not job_id:
            return "No active sprint timer to extend. Start a new sprint first."
        try:
            job = scheduler.get_job(job_id)
            if job:
                new_run_date = job.next_run_time + timedelta(minutes=extra)
                scheduler.reschedule_job(job_id, trigger="date", run_date=new_run_date)
                current_focus_session["duration"] = (current_focus_session.get("duration") or 0) + extra
                state_store.save_focus_session(current_focus_session)
                logger.info(f"[FOCUS] Extended sprint by {extra}min")
                return f"Got it. Added {extra} minutes to your sprint. Keep going!"
            else:
                return "Sprint timer not found — it may have already ended."
        except Exception as e:
            logger.error(f"[FOCUS] Failed to extend sprint: {e}")
            return f"Couldn't extend the sprint: {e}"

    elif action == "next_sprint":
        task = current_focus_session.get("task", "your task")
        sprint_duration = duration_minutes or current_focus_session.get("duration") or 25
        break_duration = current_focus_session.get("break_duration") or 5
        check_in_interval = current_focus_session.get("check_in_interval")
        player = current_focus_session.get("audio_player") or FOCUS_AUDIO_PLAYER
        audio_source = current_focus_session.get("audio_source", "endel")
        sprint_num = current_focus_session.get("sprint_count", 0) + 1

        # Cancel old jobs before re-arming
        for old_key in ("job_id", "check_in_job_id"):
            old_jid = current_focus_session.get(old_key)
            if old_jid:
                with contextlib.suppress(Exception):
                    scheduler.remove_job(old_jid)

        # Re-arm break delivery job
        end_time = datetime.now() + timedelta(minutes=sprint_duration)
        job_id = f"focus_{datetime.now().strftime('%H%M%S')}"
        scheduler.add_job(
            deliver_focus_break,
            trigger="date",
            run_date=end_time,
            args=[task, break_duration],
            id=job_id,
            replace_existing=True,
        )

        # Re-start audio
        audio_started = False
        if audio_source != "silence" and player:
            audio_started = await _start_audio_for_source(audio_source, player, sprint_duration, "focus")

        # Re-enable blocking
        if current_focus_session.get("block_sites"):
            pihole = get_pihole_client()
            result = await pihole.enable_focus_blocking()
            if result.success:
                PIHOLE_BLOCKING_TOGGLES.labels(action="enable").inc()

        # Re-arm check-in job
        check_in_job_id = None
        if check_in_interval and check_in_interval > 0:
            check_in_job_id = f"focus_checkin_{datetime.now().strftime('%H%M%S')}"
            scheduler.add_job(
                deliver_check_in,
                trigger="interval",
                minutes=check_in_interval,
                id=check_in_job_id,
                replace_existing=True,
            )

        current_focus_session.update(
            {
                "started": datetime.now(),
                "duration": sprint_duration,
                "job_id": job_id,
                "check_in_job_id": check_in_job_id,
                "audio_player": player if audio_started else current_focus_session.get("audio_player"),
            }
        )
        state_store.save_focus_session(current_focus_session)
        FOCUS_SESSIONS_STARTED.labels(soundscape=audio_source).inc()

        msg = f"Sprint {sprint_num} starting! {sprint_duration} minutes on {task}. Let's go."
        await _announce_voice(msg)
        return msg

    else:
        return f"Unknown action '{action}'. Use: next_sprint, extend, or end_session."


async def deliver_focus_break(task: str, break_duration: int):
    """Called by scheduler when a sprint ends."""
    # Stop audio
    if current_focus_session.get("audio_player"):
        await stop_focus_audio(current_focus_session["audio_player"])
        logger.info("[FOCUS] Stopped audio before break announcement")

    # Disable blocking during break
    if current_focus_session.get("block_sites"):
        pihole = get_pihole_client()
        result = await pihole.disable_focus_blocking()
        if result.success:
            PIHOLE_BLOCKING_TOGGLES.labels(action="disable").inc()
            logger.info("[FOCUS] Disabled Pi-hole blocking for break")
        else:
            logger.warning(f"[FOCUS] Could not disable blocking for break: {result.message}")

    # Cancel check-in job during break
    if current_focus_session.get("check_in_job_id"):
        with contextlib.suppress(Exception):
            scheduler.remove_job(current_focus_session["check_in_job_id"])
        current_focus_session["check_in_job_id"] = None

    # Accumulate completed sprint time
    sprint_duration = current_focus_session.get("duration") or 0
    current_focus_session["total_focus_minutes"] = current_focus_session.get("total_focus_minutes", 0) + sprint_duration
    sprint_count = current_focus_session.get("sprint_count", 0) + 1
    current_focus_session["sprint_count"] = sprint_count
    sprints_planned = current_focus_session.get("sprints_planned")

    # Multi-sprint: check if session is complete
    if sprints_planned and sprint_count >= sprints_planned:
        summary = _build_session_summary()
        await _announce_voice(summary)
        total = current_focus_session.get("total_focus_minutes", 0)
        _reset_focus_session()
        FOCUS_SESSIONS_COMPLETED.inc()
        FOCUS_ACTIVE.set(0)
        FOCUS_SESSION_DURATION.observe(total)

        # Record focus completion (F-005)
        try:
            import asyncio as _asyncio

            import progress_tracker

            progress_tracker.record_event("focus_complete", {"minutes": total, "sprints": sprint_count})
            _asyncio.ensure_future(progress_tracker.check_and_announce_streaks())
        except Exception as e:
            logger.warning(f"[FOCUS] Progress tracking failed: {e}")

        logger.info(f"[FOCUS] All {sprints_planned} sprints complete for '{task}'", extra={"component": "focus"})
        return

    # Announce break
    messages = [
        f"Sprint {sprint_count} done! Take a {break_duration} minute break. Say 'next sprint' when you're ready.",
        f"Great work on {task}! Sprint {sprint_count} complete. {break_duration} minute break — stretch, grab water.",
        f"Time's up on sprint {sprint_count}! {break_duration} minute break. Say 'next sprint' to continue.",
    ]
    message = random.choice(messages)
    await _announce_voice(message)

    # Legacy single-sprint: reset fully
    if sprints_planned is None:
        planned_duration = current_focus_session.get("duration")
        _reset_focus_session()
        FOCUS_SESSIONS_COMPLETED.inc()
        FOCUS_ACTIVE.set(0)
        if planned_duration:
            FOCUS_SESSION_DURATION.observe(planned_duration)

        # Record focus completion (F-005)
        try:
            import asyncio as _asyncio

            import progress_tracker

            progress_tracker.record_event("focus_complete", {"minutes": planned_duration or 0, "sprints": 1})
            _asyncio.ensure_future(progress_tracker.check_and_announce_streaks())
        except Exception as e:
            logger.warning(f"[FOCUS] Progress tracking failed: {e}")

        logger.info(f"[FOCUS] Break announced for '{task}'", extra={"component": "focus"})
    else:
        # Multi-sprint break: session stays active, timer cleared
        current_focus_session["job_id"] = None
        state_store.save_focus_session(current_focus_session)
        logger.info(
            f"[FOCUS] Sprint {sprint_count}/{sprints_planned} break for '{task}'",
            extra={"component": "focus"},
        )


def _build_session_summary() -> str:
    """Build end-of-session summary string."""
    total = current_focus_session.get("total_focus_minutes", 0)
    count = current_focus_session.get("sprint_count", 0)
    task = current_focus_session.get("task_description") or current_focus_session.get("task", "your task")
    encouragements = [
        "That's solid work.",
        "Nice work today.",
        "You showed up and did the thing. That matters.",
        "Every sprint counts. Good job.",
    ]
    enc = random.choice(encouragements)
    if count <= 1:
        return f"Session complete. {total} minutes on {task}. {enc}"
    return f"Session complete. {total} minutes across {count} sprints on {task}. {enc}"


def _reset_focus_session():
    """Reset focus session state to inactive."""
    current_focus_session.update(
        {
            "active": False,
            "task": None,
            "started": None,
            "duration": None,
            "break_duration": None,
            "job_id": None,
            "audio_player": None,
            "block_sites": False,
            # F-004 additions
            "task_description": None,
            "sprint_count": 0,
            "sprints_planned": None,
            "check_in_interval": None,
            "check_in_job_id": None,
            "total_focus_minutes": 0,
            "audio_source": "endel",
        }
    )
    state_store.clear_focus_session()
