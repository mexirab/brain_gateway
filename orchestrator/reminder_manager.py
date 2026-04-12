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

from orchestrator import state_store
from orchestrator.user_profile import get_profile

logger = logging.getLogger(__name__)

# Home Assistant config — sourced from centralized settings
from orchestrator.config import settings as _settings

HA_URL = _settings.ha_url
HA_TOKEN = _settings.ha_token

# Orchestrator URL (for HA to call back)
ORCHESTRATOR_URL = _settings.orchestrator_url

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


async def _announce_voice(text: str, speaker: str | None = None, announcement_type: str = "unknown") -> Dict[str, Any]:
    """
    Announce via TTS on a speaker (defaults to REMINDER_SPEAKER).

    Generates full audio, saves to disk, serves via HTTP, and plays on an HA media_player.

    Args:
        text: The text to announce.
        speaker: Target speaker entity or room name (defaults to REMINDER_SPEAKER).
        announcement_type: Category for tracking (calendar, reminder, focus, progress, ambient, etc.).
    """
    import time as _time

    from orchestrator import shared

    t0 = _time.time()

    # Do Not Disturb — suppress all announcements when user said goodnight
    if shared.DND_ACTIVE:
        logger.info(f"[DND] Suppressed announcement ({announcement_type}): {text[:60]}")
        return {"success": True, "suppressed": True, "reason": "dnd_active"}

    try:
        backend = shared.tts_backend
        if backend is None:
            _record_announcement(text, announcement_type, None, False, "TTS backend not initialized", None)
            return {"success": False, "error": "TTS backend not initialized"}

        # =====================================================================
        # Cast delivery path — generate audio, serve via HTTP, play on HA media_player
        # =====================================================================
        headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}

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

        # Build speaker list.
        # The caller may pass:
        #   - a single entity_id       -> wrapped as [entity_id]
        #   - a comma-separated string -> split into a list
        #   - the literal "all"        -> alias for REMINDER_SPEAKER (multi-room broadcast)
        #   - None / empty             -> fall through to REMINDER_SPEAKER
        # REMINDER_SPEAKER itself may be comma-separated for multi-room broadcast
        # (avoids Google Home group issues with soundbars).
        def _split_speakers(value: str) -> list[str]:
            return [s.strip() for s in value.split(",") if s.strip()]

        if speaker and speaker.strip().lower() != "all":
            broadcast_speakers = _split_speakers(speaker)
        else:
            broadcast_speakers = _split_speakers(REMINDER_SPEAKER)

        if not broadcast_speakers:
            err = "No speakers configured (REMINDER_SPEAKER is empty)"
            logger.error(err)
            _record_announcement(text, announcement_type, None, False, err, None)
            return {"success": False, "error": err}

        # Cast to all target speakers (don't stop at first success)
        succeeded = []
        last_error = None
        async with httpx.AsyncClient(timeout=30) as client:
            for try_speaker in broadcast_speakers:
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
                        logger.info(f"Played announcement on {try_speaker}")
                        succeeded.append(try_speaker)
                    else:
                        last_error = f"HA returned {ha_response.status_code} for {try_speaker}"
                        logger.warning(f"play_media failed: {last_error}")
                except Exception as speaker_err:
                    last_error = f"Connection error for {try_speaker}: {speaker_err}"
                    logger.warning(f"play_media failed: {last_error}")

        latency_ms = int((_time.time() - t0) * 1000)
        if succeeded:
            speaker_label = ",".join(succeeded)
            _record_announcement(text, announcement_type, speaker_label, True, None, latency_ms)
            return {"success": True, "speaker": speaker_label}

        _record_announcement(
            text,
            announcement_type,
            broadcast_speakers[0] if broadcast_speakers else "unknown",
            False,
            last_error,
            latency_ms,
        )
        return {"success": False, "error": last_error}

    except Exception as e:
        latency_ms = int((_time.time() - t0) * 1000)
        logger.error(f"Voice announcement failed: {e}")
        _record_announcement(text, announcement_type, None, False, str(e), latency_ms)
        return {"success": False, "error": str(e)}


def _record_announcement(
    text: str,
    announcement_type: str,
    speaker: str | None,
    success: bool,
    error: str | None,
    latency_ms: int | None,
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
        )
    except Exception as e:
        logger.warning(f"Failed to record announcement: {e}")

    try:
        from orchestrator.metrics import TTS_ANNOUNCEMENTS_TOTAL, TTS_ERRORS_TOTAL, TTS_LATENCY

        # Sanitize speaker label to prevent Prometheus cardinality explosion from untrusted input
        safe_speaker = re.sub(r"[^a-zA-Z0-9_:.\-]", "_", speaker or "none")[:50]
        TTS_ANNOUNCEMENTS_TOTAL.labels(
            type=announcement_type,
            speaker=safe_speaker,
            success="true" if success else "false",
        ).inc()

        if latency_ms is not None:
            TTS_LATENCY.observe(latency_ms / 1000)

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
