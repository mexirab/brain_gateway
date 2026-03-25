"""
Reminder Manager for Brain Gateway
Handles voice reminder scheduling, storage, and delivery via Home Assistant.
"""

import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx

import state_store
from user_profile import get_profile

logger = logging.getLogger(__name__)

# Home Assistant config
HA_URL = os.environ.get("HA_URL", "http://10.0.0.106:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")

# Orchestrator URL (for HA to call back)
ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://10.0.0.248:8888")

# Delivery targets (configurable via env and profile)
_profile = get_profile()
REMINDER_SPEAKER = os.environ.get("REMINDER_SPEAKER", _profile.default_speaker)
# Support both single service (backward compat) and list of services
_NOTIFY_SERVICE_RE = re.compile(r"^[a-zA-Z0-9_]+\.[a-zA-Z0-9_]+$")
_MAX_NOTIFY_SERVICES = 10
_raw_services = _profile.mobile_notify_services or (
    [_profile.mobile_notify_service] if _profile.mobile_notify_service else []
)
MOBILE_NOTIFY_SERVICES: list[str] = [
    s for s in _raw_services[:_MAX_NOTIFY_SERVICES] if isinstance(s, str) and _NOTIFY_SERVICE_RE.match(s)
]


# =============================================================================
# TIME PARSING
# =============================================================================


def parse_time_expression(time_str: str) -> Tuple[Optional[datetime], Optional[str]]:
    """
    Parse a time expression into a datetime.

    Supports:
    - "in X minutes" / "in X min"
    - "in X hours" / "in X hour"
    - "at 3pm" / "at 3:30pm" / "at 15:00"
    - "HH:MM" (24-hour format)

    Returns: (datetime, error_message)
    """
    time_str = time_str.strip().lower()
    now = datetime.now()

    # Handle "in X minutes"
    match = re.match(r"in\s+(\d+)\s*(?:min(?:utes?)?|m)\b", time_str)
    if match:
        minutes = int(match.group(1))
        target = now + timedelta(minutes=minutes)
        return (target, None)

    # Handle "in X hours"
    match = re.match(r"in\s+(\d+)\s*(?:hours?|h)\b", time_str)
    if match:
        hours = int(match.group(1))
        target = now + timedelta(hours=hours)
        return (target, None)

    # Handle "in X hours and Y minutes"
    match = re.match(r"in\s+(\d+)\s*(?:hours?|h)\s*(?:and\s+)?(\d+)\s*(?:min(?:utes?)?|m)", time_str)
    if match:
        hours = int(match.group(1))
        minutes = int(match.group(2))
        target = now + timedelta(hours=hours, minutes=minutes)
        return (target, None)

    # Handle "at 3pm" / "at 3:30pm" / "at 3:30 pm"
    match = re.match(r"(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", time_str)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2)) if match.group(2) else 0
        period = match.group(3)

        if period == "pm" and hour != 12:
            hour += 12
        elif period == "am" and hour == 12:
            hour = 0

        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        # If the time has already passed today, schedule for tomorrow
        if target <= now:
            target += timedelta(days=1)

        return (target, None)

    # Handle 24-hour format "HH:MM" or "at HH:MM"
    match = re.match(r"(?:at\s+)?(\d{1,2}):(\d{2})(?!\s*[ap]m)", time_str)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))

        if hour > 23 or minute > 59:
            return (None, f"Invalid time: {time_str}")

        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        # If the time has already passed today, schedule for tomorrow
        if target <= now:
            target += timedelta(days=1)

        return (target, None)

    return (None, f"Could not parse time: '{time_str}'. Try 'in 5 minutes', 'at 3pm', or '14:30'.")


def format_time_friendly(dt: datetime) -> str:
    """Format a datetime into a friendly spoken format."""
    now = datetime.now()

    # Calculate difference
    diff = dt - now

    if diff.total_seconds() < 60:
        return "less than a minute from now"
    elif diff.total_seconds() < 3600:
        minutes = int(diff.total_seconds() / 60)
        return f"in {minutes} minute{'s' if minutes != 1 else ''}"
    elif diff.total_seconds() < 7200:
        hours = int(diff.total_seconds() / 3600)
        minutes = int((diff.total_seconds() % 3600) / 60)
        if minutes > 0:
            return f"in {hours} hour and {minutes} minute{'s' if minutes != 1 else ''}"
        return f"in {hours} hour"
    else:
        # Format as actual time
        return f"at {dt.strftime('%-I:%M %p')}"


# =============================================================================
# PERSISTENT REMINDER STORAGE (SQLite via state_store)
# =============================================================================


def add_reminder(reminder_id: str, text: str, trigger_time: datetime, target: str = "both") -> Dict[str, Any]:
    """Add a new reminder to persistent storage."""
    state_store.save_reminder(reminder_id, text, trigger_time.isoformat(), target)
    reminder = {
        "id": reminder_id,
        "text": text,
        "time": trigger_time.strftime("%Y-%m-%d %H:%M"),
        "time_display": trigger_time.strftime("%-I:%M %p"),
        "target": target,
        "created": datetime.now().isoformat(),
        "status": "pending",
    }
    logger.info(f"Added reminder {reminder_id}: '{text}' at {trigger_time}")
    return reminder


def get_reminder(reminder_id: str) -> Optional[Dict[str, Any]]:
    """Get a single reminder by ID."""
    return state_store.get_reminder(reminder_id)


def remove_reminder(reminder_id: str) -> bool:
    """Remove a reminder by ID."""
    return state_store.delete_reminder(reminder_id)


def mark_reminder_completed(reminder_id: str) -> bool:
    """Mark a reminder as completed."""
    return state_store.complete_reminder(reminder_id)


def list_pending_reminders() -> List[Dict[str, Any]]:
    """Get all pending reminders."""
    return state_store.get_pending_reminders()


# =============================================================================
# REMINDER DELIVERY HELPERS
# =============================================================================


FALLBACK_SPEAKER = os.environ.get("FALLBACK_SPEAKER", "media_player.dining_room_pair")


def _resolve_snapcast_fifos(speaker: str | None) -> list[str]:
    """
    Map a speaker name to Snapcast named pipe path(s).

    Accepts:
    - Room names: "office", "bedroom", "living", "kitchen"
    - HA entity IDs: "media_player.office_max" → extracts first token → "office"
    - "all" / None / unrecognized → returns ALL room pipes (broadcast)

    Returns a list of FIFO paths. For a specific room, returns one path.
    For "all" or default, returns all room pipes so every client hears it
    regardless of which stream they're subscribed to.
    """
    import shared

    base = shared.SNAPCAST_FIFO_BASE  # e.g. /tmp/snapcast
    rooms = ["office", "bedroom", "living", "kitchen"]
    all_pipes = [f"{base}/{r}" for r in rooms]

    if not speaker:
        return all_pipes

    # Direct room name match
    room = speaker.lower().strip()
    if room == "all":
        return all_pipes
    if room in rooms:
        return [f"{base}/{room}"]

    # Extract from HA entity_id (e.g. "media_player.office_max" → "office")
    if "." in room:
        room = room.split(".", 1)[1]  # "office_max"
    first_word = room.split("_")[0]
    if first_word in rooms:
        return [f"{base}/{first_word}"]

    # Fallback: broadcast to all rooms
    return all_pipes


async def _announce_voice(text: str, speaker: str | None = None, announcement_type: str = "unknown") -> Dict[str, Any]:
    """
    Announce via TTS on a speaker (defaults to REMINDER_SPEAKER).

    When SNAPCAST_ENABLED is true, streams TTS sentence-by-sentence directly
    to the Snapcast named pipe for the target room (~1-2s to first audio).

    When SNAPCAST_ENABLED is false, uses the existing Cast path: generates full
    audio, saves to disk, serves via HTTP, and plays on an HA media_player.

    Args:
        text: The text to announce.
        speaker: Target speaker entity or room name (defaults to REMINDER_SPEAKER).
        announcement_type: Category for tracking (calendar, reminder, focus, progress, ambient, etc.).
    """
    import time as _time

    import shared

    t0 = _time.time()

    # Do Not Disturb — suppress all announcements when user said goodnight
    if shared.DND_ACTIVE:
        logger.info(f"[DND] Suppressed announcement ({announcement_type}): {text[:60]}")
        return {"success": True, "suppressed": True, "reason": "dnd_active"}

    try:
        backend = shared.tts_backend
        if backend is None:
            _record_announcement(text, announcement_type, None, False, "TTS backend not initialized", None, False)
            return {"success": False, "error": "TTS backend not initialized"}

        # =====================================================================
        # Snapcast streaming path (low latency, sentence-by-sentence)
        # =====================================================================
        if shared.SNAPCAST_ENABLED and hasattr(backend, "synthesize_to_snapcast"):
            # Room-targeted TTS: if no specific speaker requested, use detected room
            effective_speaker = speaker
            if not effective_speaker and shared.PRESENCE_TARGETED_TTS:
                try:
                    from presence_tracker import get_presence

                    p = get_presence()
                    if p.get("current_room"):
                        effective_speaker = p["current_room"]
                        logger.info(f"[PRESENCE] Targeting TTS to {effective_speaker}")
                except Exception:
                    pass
            fifo_paths = _resolve_snapcast_fifos(effective_speaker)
            target_label = effective_speaker or "all"
            try:
                result = await backend.synthesize_to_snapcast(text, fifo_paths)
                latency_ms = int((_time.time() - t0) * 1000)
                logger.info(
                    f"Streamed announcement to {len(fifo_paths)} Snapcast pipe(s) "
                    f"({result.get('bytes_written', 0)} bytes, {latency_ms}ms)"
                )
                _record_announcement(text, announcement_type, f"snapcast:{target_label}", True, None, latency_ms, False)
                return {
                    "success": True,
                    "speaker": f"snapcast:{target_label}",
                    "bytes_written": result.get("bytes_written", 0),
                }
            except FileNotFoundError:
                error = f"Snapcast FIFOs not found: {fifo_paths}"
                logger.error(error)
                # Fall through to Cast path as fallback
                logger.info("Falling back to Cast delivery path")
            except Exception as e:
                error = f"Snapcast stream failed: {e}"
                logger.error(error)
                # Fall through to Cast path as fallback
                logger.info("Falling back to Cast delivery path")

        # =====================================================================
        # Cast delivery path (existing behavior — full file + HA media_player)
        # =====================================================================
        headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
        fallback_used = False

        # Generate audio via backend
        audio_bytes = await backend.synthesize(text)

        # Save audio with UUID
        audio_id = str(uuid.uuid4())[:8]
        audio_dir = "/tmp/brain_audio"
        os.makedirs(audio_dir, exist_ok=True)
        ext = backend.file_extension
        audio_path = f"{audio_dir}/{audio_id}.{ext}"

        with open(audio_path, "wb") as f:
            f.write(audio_bytes)

        audio_url = f"{ORCHESTRATOR_URL}/api/audio/{audio_id}.{ext}"

        # Build speaker list: primary, then fallback if different
        target_speaker = speaker or REMINDER_SPEAKER
        speakers_to_try = [target_speaker]
        if FALLBACK_SPEAKER and target_speaker != FALLBACK_SPEAKER:
            speakers_to_try.append(FALLBACK_SPEAKER)

        # Try each speaker until one succeeds
        last_error = None
        async with httpx.AsyncClient(timeout=30) as client:
            for try_speaker in speakers_to_try:
                try:
                    ha_response = await client.post(
                        f"{HA_URL}/api/services/media_player/play_media",
                        headers=headers,
                        json={
                            "entity_id": try_speaker,
                            "media_content_id": audio_url,
                            "media_content_type": backend.audio_format,
                        },
                    )

                    if ha_response.status_code == 200:
                        if try_speaker != target_speaker:
                            logger.warning(f"Primary speaker {target_speaker} failed, used fallback {try_speaker}")
                            fallback_used = True
                        latency_ms = int((_time.time() - t0) * 1000)
                        logger.info(f"Played announcement on {try_speaker}")
                        _record_announcement(
                            text, announcement_type, try_speaker, True, None, latency_ms, fallback_used
                        )
                        return {"success": True, "speaker": try_speaker}
                    else:
                        last_error = f"HA returned {ha_response.status_code} for {try_speaker}"
                        logger.warning(f"play_media failed: {last_error}")
                except Exception as speaker_err:
                    last_error = f"Connection error for {try_speaker}: {speaker_err}"
                    logger.warning(f"play_media failed: {last_error}")

        latency_ms = int((_time.time() - t0) * 1000)
        _record_announcement(text, announcement_type, target_speaker, False, last_error, latency_ms, fallback_used)
        return {"success": False, "error": last_error}

    except Exception as e:
        latency_ms = int((_time.time() - t0) * 1000)
        logger.error(f"Voice announcement failed: {e}")
        _record_announcement(text, announcement_type, None, False, str(e), latency_ms, False)
        return {"success": False, "error": str(e)}


def _record_announcement(
    text: str,
    announcement_type: str,
    speaker: str | None,
    success: bool,
    error: str | None,
    latency_ms: int | None,
    fallback_used: bool,
) -> None:
    """Record announcement to DB and metrics (fire-and-forget)."""
    try:
        state_store.record_announcement(
            text=text,
            announcement_type=announcement_type,
            speaker=speaker,
            success=success,
            error=error,
            latency_ms=latency_ms,
            fallback_used=fallback_used,
        )
    except Exception as e:
        logger.warning(f"Failed to record announcement: {e}")

    try:
        from metrics import TTS_ANNOUNCEMENTS_TOTAL, TTS_ERRORS_TOTAL, TTS_FALLBACK_TOTAL, TTS_LATENCY

        # Sanitize speaker label to prevent Prometheus cardinality explosion from untrusted input
        safe_speaker = re.sub(r"[^a-zA-Z0-9_:.\-]", "_", speaker or "none")[:50]
        TTS_ANNOUNCEMENTS_TOTAL.labels(
            type=announcement_type,
            speaker=safe_speaker,
            success="true" if success else "false",
        ).inc()

        if latency_ms is not None:
            TTS_LATENCY.observe(latency_ms / 1000)

        if fallback_used:
            TTS_FALLBACK_TOTAL.inc()

        if not success and error:
            if "HA returned" in error:
                error_type = "ha_error"
            elif "Connection" in error:
                error_type = "connection"
            else:
                error_type = "tts_error"
            TTS_ERRORS_TOTAL.labels(error_type=error_type).inc()
    except Exception:
        pass


_raw_webui_url = os.environ.get("WEBUI_URL", "")
WEBUI_URL = _raw_webui_url if re.match(r"^https?://", _raw_webui_url) else ""
if _raw_webui_url and not WEBUI_URL:
    logger.warning(f"[SECURITY] WEBUI_URL rejected (not http/https): {_raw_webui_url!r}")


async def _send_notification(text: str) -> Dict[str, Any]:
    """Send a mobile push notification to all configured phones via HA Companion App.

    Includes a deep link to Open WebUI so tapping the notification opens the
    chat interface. Works with both iOS and Android Companion Apps.
    Fans out to all services in MOBILE_NOTIFY_SERVICES concurrently.
    """
    headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}

    if not MOBILE_NOTIFY_SERVICES:
        logger.warning("No mobile_notify_services configured, skipping notification")
        return {"success": False, "error": "No mobile notification services configured"}

    # Build notification data with deep link for both platforms
    notification_data: Dict[str, Any] = {
        # iOS (HA Companion)
        "push": {"sound": "default", "interruption-level": "time-sensitive"},
    }
    if WEBUI_URL:
        # iOS: opens URL when notification is tapped
        notification_data["url"] = WEBUI_URL
        # Android: opens URL when notification is tapped
        notification_data["clickAction"] = WEBUI_URL

    payload = {
        "message": text,
        "title": _profile.notification_title,
        "data": notification_data,
    }

    async def _post_one(client: httpx.AsyncClient, service: str) -> tuple[str, bool, str | None]:
        service_path = service.replace(".", "/", 1)
        try:
            response = await client.post(
                f"{HA_URL}/api/services/{service_path}",
                headers=headers,
                json=payload,
            )
            if response.status_code == 200:
                return service, True, None
            return service, False, f"{service}: HA returned {response.status_code}"
        except Exception as e:
            return service, False, f"{service}: {e}"

    successes = []
    errors = []

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            results = await asyncio.gather(*[_post_one(client, s) for s in MOBILE_NOTIFY_SERVICES])
        for svc, ok, err in results:
            if ok:
                successes.append(svc)
            else:
                errors.append(err)

    except Exception as e:
        logger.error(f"Mobile notification failed: {e}")
        return {"success": False, "error": str(e)}

    if successes:
        logger.info(f"Sent notification to {len(successes)} phone(s): {text[:50]}...")
    if errors:
        logger.warning(f"Notification failed for: {errors}")

    return {"success": len(successes) > 0, "delivered": successes, "errors": errors}
