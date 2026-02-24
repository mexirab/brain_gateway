"""
Background scheduler jobs: calendar polling and morning briefing.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import shared
from shared import TIMEZONE
from google_calendar import get_calendar_client
from reminder_manager import list_pending_reminders, _announce_voice
from metrics import CALENDAR_POLL_EVENTS_FOUND

logger = logging.getLogger(__name__)


async def poll_calendar():
    """Every N minutes: check for events starting within 2 hours, announce via TTS."""
    tz = ZoneInfo(TIMEZONE)

    client = get_calendar_client()
    if not client or not client.is_configured:
        return

    try:
        response = await client.get_upcoming(hours_ahead=2)
        if not response.success:
            logger.warning(f"[CALENDAR_POLL] Failed: {response.error}")
            return

        now = datetime.now(tz)
        for event in response.events:
            if event.id in shared._notified_events:
                continue
            minutes = int((event.start - now).total_seconds() / 60)
            if minutes < 0:
                continue
            if event.all_day:
                continue

            if minutes <= 120:
                if minutes <= 1:
                    time_str = "now"
                elif minutes < 60:
                    time_str = f"in {minutes} minutes"
                else:
                    hours = minutes // 60
                    remaining = minutes % 60
                    time_str = f"in {hours} hour{'s' if hours > 1 else ''}"
                    if remaining > 0:
                        time_str += f" and {remaining} minutes"

                message = f"Heads up Nadim: {event.title} {time_str}"
                if event.location:
                    message += f" at {event.location}"
                await _announce_voice(message)
                shared._notified_events.add(event.id)
                CALENDAR_POLL_EVENTS_FOUND.inc()
                logger.info(f"[CALENDAR_POLL] Announced: {event.title} {time_str}",
                            extra={"component": "calendar"})

    except Exception as e:
        logger.error(f"[CALENDAR_POLL] Error: {e}")


async def morning_briefing():
    """Morning announcement: today's events summary via TTS."""
    client = get_calendar_client()
    if not client or not client.is_configured:
        return

    try:
        response = await client.list_events(days_ahead=1)
        events = response.events if response.success else []

        parts = ["Good morning Nadim!"]

        if events:
            parts.append(f"You have {len(events)} event{'s' if len(events) > 1 else ''} today.")
            for event in events[:5]:
                if event.all_day:
                    parts.append(f"All day: {event.title}")
                else:
                    time_str = event.start.strftime("%I:%M %p").lstrip("0")
                    parts.append(f"At {time_str}: {event.title}")
        else:
            parts.append("Your calendar is clear today.")

        pending = list_pending_reminders()
        if pending:
            parts.append(f"You also have {len(pending)} reminder{'s' if len(pending) > 1 else ''} pending.")

        await _announce_voice(" ".join(parts))
        logger.info(f"[MORNING_BRIEFING] Delivered: {len(events)} events, {len(pending)} reminders")

    except Exception as e:
        logger.error(f"[MORNING_BRIEFING] Error: {e}")
