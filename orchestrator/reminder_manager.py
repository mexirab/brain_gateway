"""
Reminder Manager for Brain Gateway
Handles voice reminder scheduling, storage, and delivery via Home Assistant.
"""

import os
import re
import yaml
import uuid
import logging
import httpx
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Paths
RAG_BASE = os.environ.get("RAG_BASE", "/home/nadim/rag/nadim_rag")
REMINDERS_YAML = os.path.join(RAG_BASE, "20_routines", "reminders.yaml")
REMINDERS_MD = os.path.join(RAG_BASE, "20_routines", "reminders.md")

# Home Assistant config
HA_URL = os.environ.get("HA_URL", "http://10.0.0.106:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")

# Orchestrator URL (for HA to call back)
ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://10.0.0.186:8888")

# Delivery targets (configurable via env)
REMINDER_SPEAKER = os.environ.get("REMINDER_SPEAKER", "media_player.office_speaker")
MOBILE_NOTIFY = "notify.mobile_app_nadims_iphone"


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
# REMINDER STORAGE
# =============================================================================

def get_reminders() -> Dict[str, Any]:
    """Load reminders from YAML."""
    try:
        with open(REMINDERS_YAML, 'r') as f:
            data = yaml.safe_load(f)
            return data if data else {"reminders": []}
    except FileNotFoundError:
        logger.info(f"Creating new reminders file: {REMINDERS_YAML}")
        return {"reminders": []}
    except Exception as e:
        logger.error(f"Error loading reminders: {e}")
        return {"reminders": []}


def save_reminders(data: Dict[str, Any]) -> bool:
    """Save reminders to YAML and regenerate markdown."""
    try:
        # Ensure directory exists
        os.makedirs(os.path.dirname(REMINDERS_YAML), exist_ok=True)

        with open(REMINDERS_YAML, 'w') as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        _generate_reminders_md(data)
        return True
    except Exception as e:
        logger.error(f"Error saving reminders: {e}")
        return False


def add_reminder(text: str, trigger_time: datetime, target: str = "both",
                 audio_url: str = None) -> Dict[str, Any]:
    """
    Add a new reminder.

    Args:
        text: What to remind about
        trigger_time: When to trigger the reminder
        target: "voice", "phone", or "both"
        audio_url: Pre-generated TTS audio URL

    Returns:
        The created reminder dict with id
    """
    data = get_reminders()

    reminder_id = str(uuid.uuid4())[:8]
    reminder = {
        "id": reminder_id,
        "text": text,
        "time": trigger_time.strftime("%Y-%m-%d %H:%M"),
        "time_display": trigger_time.strftime("%-I:%M %p"),
        "target": target,
        "created": datetime.now().isoformat(),
        "status": "pending",
    }

    if audio_url:
        reminder["audio_url"] = audio_url

    data["reminders"].append(reminder)

    if save_reminders(data):
        logger.info(f"Added reminder {reminder_id}: '{text}' at {trigger_time}")
        return reminder

    return {}


def remove_reminder(reminder_id: str) -> bool:
    """Remove a reminder by ID."""
    data = get_reminders()
    original_len = len(data["reminders"])

    data["reminders"] = [r for r in data["reminders"] if r.get("id") != reminder_id]

    if len(data["reminders"]) < original_len:
        save_reminders(data)
        return True
    return False


def mark_reminder_completed(reminder_id: str) -> bool:
    """Mark a reminder as completed (triggered)."""
    data = get_reminders()

    for reminder in data["reminders"]:
        if reminder.get("id") == reminder_id:
            reminder["status"] = "completed"
            reminder["completed_at"] = datetime.now().isoformat()
            save_reminders(data)
            return True
    return False


def list_pending_reminders() -> List[Dict[str, Any]]:
    """Get all pending reminders."""
    data = get_reminders()
    return [r for r in data["reminders"] if r.get("status") == "pending"]


def _generate_reminders_md(data: Dict[str, Any]) -> None:
    """Regenerate reminders.md from YAML data."""
    lines = ["# Active Reminders", ""]

    pending = [r for r in data.get("reminders", []) if r.get("status") == "pending"]
    completed = [r for r in data.get("reminders", []) if r.get("status") == "completed"]

    if pending:
        lines.append("## Pending")
        lines.append("")
        for r in pending:
            lines.append(f"- **{r.get('time_display', r.get('time', 'Unknown'))}**: {r.get('text', '')}")
            lines.append(f"  - Target: {r.get('target', 'both')} | ID: {r.get('id', 'N/A')}")
        lines.append("")
    else:
        lines.append("*No pending reminders*")
        lines.append("")

    if completed:
        lines.append("## Recently Completed")
        lines.append("")
        # Show only last 10 completed
        for r in completed[-10:]:
            lines.append(f"- ~~{r.get('text', '')}~~ at {r.get('time_display', r.get('time', ''))}")
        lines.append("")

    try:
        with open(REMINDERS_MD, 'w') as f:
            f.write("\n".join(lines))
        logger.info(f"Regenerated {REMINDERS_MD}")
    except Exception as e:
        logger.error(f"Error writing reminders markdown: {e}")


# =============================================================================
# TTS AUDIO GENERATION
# =============================================================================


async def generate_reminder_audio(reminder_text: str, reminder_id: str) -> Optional[str]:
    """
    Pre-generate TTS audio for the reminder.

    Returns the audio URL that can be played by HA at the scheduled time.
    """
    TTS_URL = os.environ.get("TTS_URL", "http://10.0.0.173:8002")
    TTS_VOICE = os.environ.get("TTS_VOICE", "jessica")

    spoken_text = f"Hey Nadim! Quick reminder: {reminder_text}"

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            tts_response = await client.post(
                f"{TTS_URL}/tts",
                json={"text": spoken_text, "voice": TTS_VOICE}
            )

            if tts_response.status_code != 200:
                logger.error(f"TTS generation failed: {tts_response.status_code}")
                return None

            # Save audio with reminder ID for persistence
            audio_dir = "/tmp/brain_audio/reminders"
            os.makedirs(audio_dir, exist_ok=True)
            audio_path = f"{audio_dir}/{reminder_id}.wav"

            with open(audio_path, "wb") as f:
                f.write(tts_response.content)

            # Return URL that HA can access
            audio_url = f"{ORCHESTRATOR_URL}/api/audio/reminders/{reminder_id}.wav"
            logger.info(f"Generated TTS audio: {audio_url}")
            return audio_url

    except Exception as e:
        logger.error(f"Failed to generate TTS audio: {e}")
        return None


# =============================================================================
# REMINDER DELIVERY HELPERS
# =============================================================================

async def _announce_voice(text: str) -> Dict[str, Any]:
    """
    Announce reminder via TTS on all speakers.

    Uses the orchestrator's TTS endpoint to generate Jessica voice audio,
    then plays it on all speakers.
    """
    TTS_URL = os.environ.get("TTS_URL", "http://10.0.0.173:8002")
    TTS_VOICE = os.environ.get("TTS_VOICE", "jessica")

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

            # Play on all speakers
            ha_response = await client.post(
                f"{HA_URL}/api/services/media_player/play_media",
                headers=headers,
                json={
                    "entity_id": REMINDER_SPEAKER,
                    "media_content_id": audio_url,
                    "media_content_type": "audio/wav"
                }
            )

            if ha_response.status_code == 200:
                logger.info(f"Played reminder on {REMINDER_SPEAKER}")
                return {"success": True, "speaker": REMINDER_SPEAKER}
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

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{HA_URL}/api/services/notify/mobile_app_nadims_iphone",
                headers=headers,
                json={
                    "message": text,
                    "title": "Reminder from Jess",
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
