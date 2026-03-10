"""
Reminder Manager for Brain Gateway
Handles voice reminder scheduling, storage, and delivery via Home Assistant.
"""

import os
import re
import uuid
import logging
import httpx
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

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
MOBILE_NOTIFY = _profile.mobile_notify_service


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
    match = re.match(r'in\s+(\d+)\s*(?:min(?:utes?)?|m)\b', time_str)
    if match:
        minutes = int(match.group(1))
        target = now + timedelta(minutes=minutes)
        return (target, None)

    # Handle "in X hours"
    match = re.match(r'in\s+(\d+)\s*(?:hours?|h)\b', time_str)
    if match:
        hours = int(match.group(1))
        target = now + timedelta(hours=hours)
        return (target, None)

    # Handle "in X hours and Y minutes"
    match = re.match(r'in\s+(\d+)\s*(?:hours?|h)\s*(?:and\s+)?(\d+)\s*(?:min(?:utes?)?|m)', time_str)
    if match:
        hours = int(match.group(1))
        minutes = int(match.group(2))
        target = now + timedelta(hours=hours, minutes=minutes)
        return (target, None)

    # Handle "at 3pm" / "at 3:30pm" / "at 3:30 pm"
    match = re.match(r'(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)', time_str)
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
    match = re.match(r'(?:at\s+)?(\d{1,2}):(\d{2})(?!\s*[ap]m)', time_str)
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
# IN-MEMORY REMINDER STORAGE
# =============================================================================

_reminders: Dict[str, Dict[str, Any]] = {}


def add_reminder(reminder_id: str, text: str, trigger_time: datetime, target: str = "both") -> Dict[str, Any]:
    """Add a new reminder to in-memory storage."""
    reminder = {
        "id": reminder_id,
        "text": text,
        "time": trigger_time.strftime("%Y-%m-%d %H:%M"),
        "time_display": trigger_time.strftime("%-I:%M %p"),
        "target": target,
        "created": datetime.now().isoformat(),
        "status": "pending",
    }
    _reminders[reminder_id] = reminder
    logger.info(f"Added reminder {reminder_id}: '{text}' at {trigger_time}")
    return reminder


def get_reminder(reminder_id: str) -> Optional[Dict[str, Any]]:
    """Get a single reminder by ID."""
    return _reminders.get(reminder_id)


def remove_reminder(reminder_id: str) -> bool:
    """Remove a reminder by ID."""
    return _reminders.pop(reminder_id, None) is not None


def mark_reminder_completed(reminder_id: str) -> bool:
    """Mark a reminder as completed."""
    reminder = _reminders.get(reminder_id)
    if reminder:
        reminder["status"] = "completed"
        reminder["completed_at"] = datetime.now().isoformat()
        return True
    return False


def list_pending_reminders() -> List[Dict[str, Any]]:
    """Get all pending reminders."""
    return [r for r in _reminders.values() if r.get("status") == "pending"]


# =============================================================================
# REMINDER DELIVERY HELPERS
# =============================================================================

async def _announce_voice(text: str, speaker: str | None = None) -> Dict[str, Any]:
    """
    Announce via TTS on a speaker (defaults to REMINDER_SPEAKER).

    Uses the orchestrator's TTS endpoint to generate voice audio,
    then plays it on all speakers.
    """
    TTS_URL = os.environ.get("TTS_URL", "http://10.0.0.173:8002")
    TTS_VOICE = os.environ.get("TTS_VOICE", _profile.assistant_voice)

    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            # Generate TTS audio
            tts_response = await client.post(
                f"{TTS_URL}/tts",
                json={"text": text, "voice": TTS_VOICE}
            )

            if tts_response.status_code != 200:
                return {"success": False, "error": "TTS generation failed"}

            # Save audio with UUID
            audio_id = str(uuid.uuid4())[:8]
            audio_dir = "/tmp/brain_audio"
            os.makedirs(audio_dir, exist_ok=True)
            audio_path = f"{audio_dir}/{audio_id}.wav"

            with open(audio_path, "wb") as f:
                f.write(tts_response.content)

            audio_url = f"{ORCHESTRATOR_URL}/api/audio/{audio_id}.wav"

            # Play on speaker
            target_speaker = speaker or REMINDER_SPEAKER
            ha_response = await client.post(
                f"{HA_URL}/api/services/media_player/play_media",
                headers=headers,
                json={
                    "entity_id": target_speaker,
                    "media_content_id": audio_url,
                    "media_content_type": "audio/wav"
                }
            )

            if ha_response.status_code == 200:
                logger.info(f"Played reminder on {target_speaker}")
                return {"success": True, "speaker": target_speaker}
            else:
                return {"success": False, "error": f"HA returned {ha_response.status_code}"}

    except Exception as e:
        logger.error(f"Voice announcement failed: {e}")
        return {"success": False, "error": str(e)}


async def _send_notification(text: str) -> Dict[str, Any]:
    """Send a mobile push notification via HA Companion App."""
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json"
    }

    if not MOBILE_NOTIFY:
        logger.warning("No mobile_notify_service configured, skipping notification")
        return {"success": False, "error": "No mobile notification service configured"}

    # Extract the service path from the full service name
    # "notify.mobile_app_nadims_iphone" → "notify/mobile_app_nadims_iphone"
    service_path = MOBILE_NOTIFY.replace(".", "/", 1)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{HA_URL}/api/services/{service_path}",
                headers=headers,
                json={
                    "message": text,
                    "title": _profile.notification_title,
                    "data": {
                        "push": {
                            "sound": "default",
                            "interruption-level": "time-sensitive"
                        }
                    }
                }
            )

            if response.status_code == 200:
                logger.info(f"Sent mobile notification: {text[:50]}...")
                return {"success": True}
            else:
                return {"success": False, "error": f"HA returned {response.status_code}"}

    except Exception as e:
        logger.error(f"Mobile notification failed: {e}")
        return {"success": False, "error": str(e)}
