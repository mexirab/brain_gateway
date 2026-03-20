"""
Background scheduler jobs: calendar polling, morning briefing, email polling,
email-to-calendar event extraction, YNAB transaction sync.
"""

import json
import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import shared
import state_store
from google_calendar import get_calendar_client
from google_gmail import get_gmail_client
from metrics import (
    CALENDAR_POLL_EVENTS_FOUND,
    EMAIL_TO_CALENDAR_EMAILS_SCANNED,
    EMAIL_TO_CALENDAR_EVENTS_CREATED,
    GMAIL_API_CALLS,
    GMAIL_API_ERRORS,
    TEMPERATURE_DELTA,
    TEMPERATURE_GAUGE,
)
from reminder_manager import _announce_voice, list_pending_reminders
from shared import TIMEZONE, profile

_LLM_URL = shared.MODEL_URL
_LLM_MODEL = shared.MODEL_NAME

from travel_time import get_travel_time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tiered countdown nudges (F-002)
# ---------------------------------------------------------------------------

# Message templates keyed by tier threshold (minutes before event).
# {name} = user name, {title} = event title, {prep} = contextual prep hint
_TIER_MESSAGES = {
    60: "{name}, you have {title} in about an hour.",
    30: "{name}, {title} in 30 minutes. Start wrapping up what you're doing.",
    15: "{name}, {title} in 15 minutes. Time to transition — save your work, grab water.",
    5: "{name}, {title} starts in 5 minutes. {prep}",
}

# Fallback single-announcement message (used when tiered alerts are disabled)
_SINGLE_MESSAGE = "Heads up {name}: {title} {time_str}"


def _get_prep_hint(event) -> str:
    """Generate contextual transition scaffolding for the 5-minute tier."""
    desc = (event.description or "").lower()
    loc = (event.location or "").lower()

    # Video call links
    if any(kw in desc or kw in loc for kw in ("zoom.us", "teams.microsoft", "meet.google", "webex")):
        return "Pull up the meeting link."
    # Physical location
    if event.location and not any(kw in loc for kw in ("http", "zoom", "teams", "meet.google")):
        return f"Head out to {event.location}."
    # Agenda mention
    if "agenda" in desc:
        return "Check the agenda."
    return "Take a breath, you've got this."


def _is_focus_related(event) -> bool:
    """Check if the active focus session is related to this event (skip nudges)."""
    session = shared.current_focus_session
    if not session["active"] or not session.get("task"):
        return False
    # Simple substring match between focus task and event title
    task = session["task"].lower()
    title = event.title.lower()
    return task in title or title in task


async def poll_calendar():
    """Every N minutes: check for events starting within 2 hours, announce via TTS.

    Supports two modes (controlled by CALENDAR_TIERED_ALERTS env var):
    - Tiered: escalating announcements at 60/30/15/5 minutes (ADHD-friendly)
    - Single: one announcement per event (legacy behavior)

    For events with physical locations, uses Google Maps Directions API to
    calculate travel time with real-time traffic and announces "leave by" times.
    """
    tz = ZoneInfo(TIMEZONE)
    tiered = shared.CALENDAR_TIERED_ALERTS
    tiers = sorted(shared.CALENDAR_ALERT_TIERS, reverse=True)  # e.g. [60, 30, 15, 5]

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
            if event.all_day:
                continue
            minutes = int((event.start - now).total_seconds() / 60)
            if minutes < 0 or minutes > 120:
                continue

            # --- Travel-time-aware announcement for events with locations ---
            travel_key = f"travel:{event.id}"
            has_physical_location = (
                event.location and shared.GOOGLE_MAPS_API_KEY and not state_store.is_notified(travel_key)
            )

            if has_physical_location:
                travel = await get_travel_time(shared.HOME_ADDRESS, event.location, event.start)
                if travel:
                    drive_min = travel.duration_in_traffic_minutes
                    leave_by_min = minutes - drive_min - shared.TRAVEL_TIME_BUFFER

                    if leave_by_min <= 0 and not state_store.is_notified(f"cal:{event.id}:leave_now"):
                        message = (
                            f"{profile.user_name}, you should leave now for {event.title}. "
                            f"It's a {drive_min} minute drive to {event.location}."
                        )
                        result = await _announce_voice(message)
                        if result.get("success"):
                            state_store.mark_notified(travel_key)
                            state_store.mark_notified(f"cal:{event.id}:leave_now")
                            CALENDAR_POLL_EVENTS_FOUND.inc()
                            logger.info(
                                f"[CALENDAR_POLL] LEAVE NOW: {event.title} ({drive_min} min drive)",
                                extra={"component": "calendar"},
                            )
                        else:
                            logger.error(
                                f"[CALENDAR_POLL] TTS FAILED for '{event.title}': {result.get('error')} — will retry next poll",
                                extra={"component": "calendar"},
                            )
                        continue

                    elif leave_by_min <= 45 and not state_store.is_notified(travel_key):
                        message = (
                            f"Heads up {profile.user_name}: You need to leave in "
                            f"{leave_by_min} minutes for {event.title}. "
                            f"It's a {drive_min} minute drive to {event.location}."
                        )
                        result = await _announce_voice(message)
                        if result.get("success"):
                            state_store.mark_notified(travel_key)
                            CALENDAR_POLL_EVENTS_FOUND.inc()
                            logger.info(
                                f"[CALENDAR_POLL] Leave in {leave_by_min} min: {event.title} ({drive_min} min drive)",
                                extra={"component": "calendar"},
                            )
                        else:
                            logger.error(
                                f"[CALENDAR_POLL] TTS FAILED for '{event.title}': {result.get('error')} — will retry next poll",
                                extra={"component": "calendar"},
                            )
                        continue

            # --- Tiered countdown announcements ---
            if tiered:
                # Smart suppression: skip if focus session is related to this event
                if _is_focus_related(event):
                    logger.debug(f"[CALENDAR_POLL] Suppressed nudge for '{event.title}' — related focus session active")
                    continue

                # Find the best matching tier: the largest tier that we've
                # actually reached (minutes <= tier_min), not yet announced.
                # Iterate smallest-to-largest so we pick the closest tier first,
                # avoiding stale "in about an hour" when the event is 28 min away.
                best_tier = None
                for tier_min in sorted(tiers):
                    if minutes > tier_min:
                        continue  # not yet reached this tier
                    tier_key = f"cal:{event.id}:{tier_min}"
                    if not state_store.is_notified(tier_key):
                        best_tier = tier_min
                        break  # closest un-announced tier

                if best_tier is not None:
                    tier_key = f"cal:{event.id}:{best_tier}"
                    template = _TIER_MESSAGES.get(
                        best_tier,
                        "{name}, {title} in " + str(best_tier) + " minutes.",
                    )
                    if template:
                        prep = _get_prep_hint(event) if best_tier <= 5 else ""
                        message = template.format(name=profile.user_name, title=event.title, prep=prep)
                        if event.location and best_tier > 5:
                            message += f" at {event.location}"

                        result = await _announce_voice(message)
                        if result.get("success"):
                            state_store.mark_notified(tier_key)
                            # Also mark all larger tiers as notified (catch-up)
                            for t in tiers:
                                if t > best_tier:
                                    state_store.mark_notified(f"cal:{event.id}:{t}")
                            CALENDAR_POLL_EVENTS_FOUND.inc()
                            logger.info(
                                f"[CALENDAR_POLL] Tier {best_tier}min: {event.title} (actual: {minutes}min away)",
                                extra={"component": "calendar"},
                            )
                        else:
                            logger.error(
                                f"[CALENDAR_POLL] TTS FAILED for '{event.title}': {result.get('error')} — will retry next poll",
                                extra={"component": "calendar"},
                            )

            else:
                # --- Legacy single announcement ---
                cal_key = f"cal:{event.id}"
                if state_store.is_notified(cal_key):
                    continue

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

                message = _SINGLE_MESSAGE.format(name=profile.user_name, title=event.title, time_str=time_str)
                if event.location:
                    message += f" at {event.location}"
                result = await _announce_voice(message)
                if result.get("success"):
                    state_store.mark_notified(cal_key)
                    CALENDAR_POLL_EVENTS_FOUND.inc()
                    logger.info(f"[CALENDAR_POLL] Announced: {event.title} {time_str}", extra={"component": "calendar"})
                else:
                    logger.error(
                        f"[CALENDAR_POLL] TTS FAILED for '{event.title}': {result.get('error')} — will retry next poll",
                        extra={"component": "calendar"},
                    )

    except Exception as e:
        logger.error(f"[CALENDAR_POLL] Error: {e}", exc_info=True)


def _parse_phone_datetime(s: str, tz=None) -> datetime:
    """Parse date strings from iPhone Shortcuts.

    Handles: "Mar 4, 2026 at 10:00\u202fAM", ISO format, etc.
    """
    if not s:
        raise ValueError("empty date string")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    cleaned = s.replace("\u202f", " ").replace("\u00a0", " ").replace(" at ", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    for fmt in (
        "%b %d, %Y %I:%M %p",
        "%B %d, %Y %I:%M %p",
        "%m/%d/%Y %I:%M %p",
        "%b %d, %Y",
    ):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognized date format: {s!r}")


async def morning_briefing():
    """Morning announcement: today's events from all calendars via TTS.

    Sources (in priority order):
    1. Phone calendar sync (consolidated: Gmail + iCloud + Work)
    2. Google Calendar API (fallback if phone hasn't synced)
    """
    tz = ZoneInfo(TIMEZONE)
    today = datetime.now(tz).date()

    try:
        # Build unified event list from available sources
        briefing_events = []

        # Source 1: Phone calendar sync (preferred — has ALL calendars)
        phone_age = (
            time.time() - shared._phone_calendar_sync_time if shared._phone_calendar_sync_time > 0 else float("inf")
        )
        if shared._phone_calendar_events and phone_age < 86400:  # synced within 24h
            for ev in shared._phone_calendar_events:
                try:
                    start_str = ev.get("start", "")
                    start = _parse_phone_datetime(start_str, tz)
                    if start.tzinfo is None:
                        start = start.replace(tzinfo=tz)
                    if start.date() != today:
                        continue
                    is_all_day = ev.get("all_day", False)
                    briefing_events.append(
                        {
                            "title": ev.get("title", "(No title)"),
                            "start": start,
                            "all_day": is_all_day,
                            "calendar": ev.get("calendar") or ev.get("calendar ") or "",
                            "location": ev.get("location", ""),
                        }
                    )
                except (ValueError, TypeError):
                    continue
            logger.info(f"[MORNING_BRIEFING] Using phone calendar ({len(briefing_events)} today's events)")
        else:
            # Source 2: Google Calendar API (fallback)
            client = get_calendar_client()
            if client and client.is_configured:
                response = await client.list_events(days_ahead=1)
                if response.success:
                    for event in response.events:
                        briefing_events.append(
                            {
                                "title": event.title,
                                "start": event.start,
                                "all_day": event.all_day,
                                "calendar": "Google",
                                "location": event.location,
                            }
                        )
                logger.info(f"[MORNING_BRIEFING] Using Google Calendar fallback ({len(briefing_events)} events)")
            else:
                logger.info("[MORNING_BRIEFING] No calendar source available")

        # Sort by time (all-day first, then by start time)
        briefing_events.sort(key=lambda e: (not e["all_day"], e["start"]))

        # Build announcement
        parts = [f"Good morning {profile.user_name}!"]

        if briefing_events:
            parts.append(f"You have {len(briefing_events)} event{'s' if len(briefing_events) > 1 else ''} today.")
            for ev in briefing_events[:8]:
                if ev["all_day"]:
                    parts.append(f"All day: {ev['title']}")
                else:
                    time_str = ev["start"].strftime("%I:%M %p").lstrip("0")
                    parts.append(f"At {time_str}: {ev['title']}")
        else:
            parts.append("Your calendar is clear today.")

        pending = list_pending_reminders()
        if pending:
            parts.append(f"You also have {len(pending)} reminder{'s' if len(pending) > 1 else ''} pending.")

        await _announce_voice(" ".join(parts), speaker=shared.MORNING_BRIEFING_SPEAKER)
        logger.info(
            f"[MORNING_BRIEFING] Delivered on {shared.MORNING_BRIEFING_SPEAKER}: {len(briefing_events)} events, {len(pending)} reminders"
        )

    except Exception as e:
        logger.error(f"[MORNING_BRIEFING] Error: {e}")


async def poll_email():
    """Every N minutes: check for new unread emails, announce important ones via TTS."""
    client = get_gmail_client()
    if not client or not client.is_configured:
        return

    GMAIL_API_CALLS.labels(operation="poll").inc()

    try:
        # Find unread emails from the last hour, skip non-primary tabs
        query = "is:unread newer_than:1h -category:promotions -category:social -category:forums -category:updates"
        response = await client.list_messages(query=query, max_results=5)

        if not response.success:
            GMAIL_API_ERRORS.labels(operation="poll").inc()
            logger.warning(f"[EMAIL_POLL] Failed: {response.error}")
            return

        new_count = 0
        for msg in response.messages:
            email_key = f"email:{msg.id}"
            if state_store.is_notified(email_key):
                continue

            # Extract sender name (strip email address for TTS)
            sender = msg.sender.split("<")[0].strip().strip('"')
            if not sender:
                sender = msg.sender

            announcement = f"New email from {sender}: {msg.subject}"
            await _announce_voice(announcement)
            state_store.mark_notified(email_key)
            new_count += 1
            logger.info(f"[EMAIL_POLL] Announced: {msg.subject} from {sender}", extra={"component": "gmail"})

        if new_count:
            logger.info(f"[EMAIL_POLL] Announced {new_count} new emails")

    except Exception as e:
        GMAIL_API_ERRORS.labels(operation="poll").inc()
        logger.error(f"[EMAIL_POLL] Error: {e}")


_EVENT_EXTRACTION_PROMPT = """\
You are a calendar event extractor. Analyze the email below and extract any events, appointments, reservations, flights, or meetings that have a specific date and time.

Return ONLY a JSON array. Each element must have:
- "title": short event title (e.g. "Flight to NYC", "Dentist Appointment", "Dinner at Uchi")
- "start_time": ISO 8601 datetime string (e.g. "2026-03-15T14:30:00")
- "duration_minutes": estimated duration in minutes (default 60)
- "location": location if mentioned, empty string otherwise
- "description": one-line source reference (e.g. "From: United Airlines confirmation")

If there are NO events with a specific date and time, return an empty array: []

Do NOT extract:
- Vague mentions without dates/times ("let's meet soon")
- Marketing or promotional events
- Subscription renewals or billing dates (unless it's a scheduled appointment)

EMAIL SUBJECT: {subject}
FROM: {sender}
DATE: {date}

EMAIL BODY:
{body}

JSON ARRAY:"""


async def process_emails_for_events():
    """Scan recent emails for events/appointments and auto-add to calendar."""
    gmail = get_gmail_client()
    cal = get_calendar_client()

    if not gmail or not gmail.is_configured:
        return
    if not cal or not cal.is_configured:
        return

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)

    try:
        # Get emails from the last 24 hours, skip promos/social/forums
        query = "newer_than:1d -category:promotions -category:social -category:forums -category:updates"
        response = await gmail.list_messages(query=query, max_results=15)

        if not response.success:
            logger.warning(f"[EMAIL_TO_CAL] Gmail query failed: {response.error}")
            return

        if not response.messages:
            return

        # Filter out already-processed emails
        new_msgs = [m for m in response.messages if not state_store.is_notified(f"e2c:{m.id}")]
        if not new_msgs:
            return

        logger.info(f"[EMAIL_TO_CAL] Scanning {len(new_msgs)} new emails for events")

        # Deferred import to avoid circular dependency
        from orchestrator import call_model

        created_count = 0
        for msg in new_msgs:
            state_store.mark_notified(f"e2c:{msg.id}")
            EMAIL_TO_CALENDAR_EMAILS_SCANNED.inc()

            # Skip very short emails (unlikely to contain event details)
            body = msg.body_text or msg.snippet
            if len(body) < 30:
                continue

            # Ask Nemotron to extract events
            prompt = _EVENT_EXTRACTION_PROMPT.format(
                subject=msg.subject,
                sender=msg.sender,
                date=msg.date.strftime("%Y-%m-%d %H:%M"),
                body=body[:1500],  # Limit body to keep prompt manageable
            )

            try:
                llm_resp = await call_model(
                    _LLM_URL,
                    _LLM_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    timeout=30,
                )
                raw = llm_resp.get("choices", [{}])[0].get("message", {}).get("content", "")
            except Exception as e:
                logger.warning(f"[EMAIL_TO_CAL] LLM call failed for '{msg.subject}': {e}")
                continue

            # Parse JSON from response
            events = _parse_event_json(raw)
            if not events:
                continue

            # Check calendar for duplicates and create missing events
            for ev in events:
                title = ev.get("title", "").strip()
                start_time = ev.get("start_time", "").strip()
                if not title or not start_time:
                    continue

                # Validate the start time is in the future
                try:
                    ev_start = datetime.fromisoformat(start_time)
                    if ev_start.tzinfo is None:
                        ev_start = ev_start.replace(tzinfo=tz)
                    if ev_start < now:
                        continue
                except ValueError:
                    continue

                # Check for duplicates: list events on that day and look for title match
                if await _event_exists_on_calendar(cal, title, ev_start):
                    logger.info(f"[EMAIL_TO_CAL] Already on calendar: {title}")
                    continue

                # Create the event
                duration = ev.get("duration_minutes", 60)
                location = ev.get("location", "")
                description = ev.get("description", "")
                if description:
                    description = f"[Auto-added from email] {description}"
                else:
                    description = f"[Auto-added from email] {msg.subject}"

                result = await cal.create_event(
                    title=title,
                    start_time=start_time,
                    duration_minutes=duration,
                    description=description,
                    location=location,
                )

                if result.success:
                    created_count += 1
                    EMAIL_TO_CALENDAR_EVENTS_CREATED.inc()
                    logger.info(f"[EMAIL_TO_CAL] Created: {title} at {start_time}", extra={"component": "email_to_cal"})
                else:
                    logger.warning(f"[EMAIL_TO_CAL] Failed to create: {title} — {result.error}")

        # Clean up old notification tracking entries (>48h)
        state_store.clear_stale_notifications(older_than_hours=48)

        if created_count:
            logger.info(f"[EMAIL_TO_CAL] Created {created_count} events from emails")

    except Exception as e:
        logger.error(f"[EMAIL_TO_CAL] Error: {e}")


def _parse_event_json(raw: str) -> list:
    """Parse a JSON array from LLM output, handling markdown fences."""
    raw = raw.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        lines = raw.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        raw = "\n".join(lines).strip()

    # Find the JSON array
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1:
        return []

    try:
        events = json.loads(raw[start : end + 1])
        if isinstance(events, list):
            return events
    except json.JSONDecodeError:
        pass
    return []


async def _event_exists_on_calendar(cal, title: str, start: datetime) -> bool:
    """Check if a similar event already exists on the calendar around that time."""
    response = await cal.list_events(days_ahead=1, calendar_id="primary")
    if not response.success:
        return False

    title_lower = title.lower()
    for existing in response.events:
        # Check same day and similar title (substring match either direction)
        if existing.start.date() != start.date():
            continue
        existing_title = existing.title.lower()
        if title_lower in existing_title or existing_title in title_lower:
            return True
    return False


async def sync_ynab_transactions():
    """Background job: sync transactions from YNAB."""
    from finance_manager import _is_ynab_configured, ynab_sync_transactions

    if not _is_ynab_configured():
        return

    try:
        result = await ynab_sync_transactions()
        if result.get("synced", 0) > 0:
            logger.info(f"[YNAB_POLL] Synced {result['synced']} transactions")
    except Exception as e:
        logger.error(f"[YNAB_POLL] Error: {e}")


async def weekly_spending_summary():
    """Sunday evening: announce weekly spending summary via TTS."""
    from finance_manager import (
        _ensure_budget_period,
        _get_level_info,
        get_db,
    )

    try:
        with get_db() as conn:
            ym = _ensure_budget_period(conn)
            budget = dict(conn.execute("SELECT * FROM budget_periods WHERE year_month = ?", (ym,)).fetchone())
            _config = dict(conn.execute("SELECT * FROM finance_config WHERE id = 1").fetchone())
            game = dict(conn.execute("SELECT * FROM game_state WHERE id = 1").fetchone())

            spent = budget["discretionary_spent"]
            limit = budget["discretionary_budget"]
            remaining = max(0, limit - spent)
            pct = (spent / limit * 100) if limit > 0 else 0
            level_info = _get_level_info(game["level"])

        parts = [f"Hey {profile.user_name}, here's your weekly spending update."]

        if spent > limit:
            overspend = spent - limit
            parts.append(f"You're over budget by ${overspend:.0f}.")
            parts.append("Time to tighten up for the rest of the month!")
        elif pct >= 75:
            parts.append(
                f"You've spent ${spent:.0f} of your ${limit:.0f} budget. "
                f"That's {pct:.0f} percent with only ${remaining:.0f} left."
            )
            parts.append("Getting close! Keep an eye on it this week.")
        elif pct >= 50:
            parts.append(f"You've spent ${spent:.0f} of ${limit:.0f}. ${remaining:.0f} remaining. You're on track!")
        else:
            parts.append(f"Only ${spent:.0f} spent out of ${limit:.0f}. ${remaining:.0f} left. Looking great!")

        parts.append(
            f"You're Level {game['level']}, {level_info['title']}, "
            f"with {game['total_xp']} total XP "
            f"and a {game['streak_months']} month streak."
        )

        await _announce_voice(" ".join(parts))
        logger.info(f"[WEEKLY_SUMMARY] Delivered: ${spent:.2f}/{limit:.2f} ({pct:.0f}%)")

    except Exception as e:
        logger.error(f"[WEEKLY_SUMMARY] Error: {e}")


async def midmonth_budget_warning():
    """Mid-month check: if over 60% of discretionary spent, announce warning via TTS."""
    from finance_manager import _ensure_budget_period, get_db

    tz = ZoneInfo(TIMEZONE)
    today = datetime.now(tz)

    # Only fire the actual warning between the 13th and 17th
    if today.day < 13 or today.day > 17:
        return

    try:
        with get_db() as conn:
            ym = _ensure_budget_period(conn)
            budget = dict(conn.execute("SELECT * FROM budget_periods WHERE year_month = ?", (ym,)).fetchone())

            spent = budget["discretionary_spent"]
            limit = budget["discretionary_budget"]
            if limit <= 0:
                return

            pct = spent / limit * 100
            remaining = max(0, limit - spent)

        if pct < 60:
            logger.info(f"[MIDMONTH] Budget at {pct:.0f}% — no warning needed")
            return

        if pct >= 100:
            overspend = spent - limit
            message = (
                f"Heads up {profile.user_name}. You're already over your monthly budget "
                f"by ${overspend:.0f} and we're only halfway through the month. "
                f"Future you is taking damage!"
            )
        elif pct >= 80:
            message = (
                f"Budget warning {profile.user_name}. You've used {pct:.0f} percent of your "
                f"monthly budget with half the month still to go. "
                f"Only ${remaining:.0f} left. Be careful!"
            )
        else:
            message = (
                f"Mid-month check. You've spent {pct:.0f} percent of your budget. "
                f"${remaining:.0f} left for the rest of the month. Keep it steady!"
            )

        await _announce_voice(message)
        logger.info(f"[MIDMONTH] Warning delivered: {pct:.0f}% spent")

    except Exception as e:
        logger.error(f"[MIDMONTH] Error: {e}")


async def check_closet_temperature():
    """Every 10 minutes: check closet temperature and alert if too hot.

    Thresholds:
    - 80°F: warning (GPU heat building up)
    - 85°F: urgent (risk of thermal throttling)
    """
    from shared import HA_TOKEN, HA_URL

    try:
        resp = await shared._http.get(
            f"{HA_URL}/api/states/{profile.closet_temp_sensor}",
            headers={"Authorization": f"Bearer {HA_TOKEN}"},
            timeout=5.0,
        )
        if resp.status_code != 200:
            return

        temp = float(resp.json()["state"])
        TEMPERATURE_GAUGE.labels(location="closet").set(temp)

        # Also grab ambient for delta tracking
        try:
            resp2 = await shared._http.get(
                f"{HA_URL}/api/states/{profile.ambient_temp_sensor}",
                headers={"Authorization": f"Bearer {HA_TOKEN}"},
                timeout=5.0,
            )
            if resp2.status_code == 200:
                kitchen_temp = float(resp2.json()["state"])
                TEMPERATURE_GAUGE.labels(location="kitchen").set(kitchen_temp)
                TEMPERATURE_DELTA.set(temp - kitchen_temp)
        except Exception:
            pass

        # Alert thresholds (only alert once per crossing, persisted via state_store)
        if temp >= 85 and not state_store.is_notified("temp:closet_85"):
            await _announce_voice(
                f"Warning! Server closet temperature is {temp:.0f} degrees. "
                f"That's dangerously hot. Check the ventilation or shut down non-essential nodes."
            )
            state_store.mark_notified("temp:closet_85")
            logger.warning(f"[TEMP_ALERT] Closet at {temp}°F — URGENT alert sent")

        elif temp >= 80 and not state_store.is_notified("temp:closet_80"):
            await _announce_voice(
                f"Heads up {profile.user_name}. The server closet is at {temp:.0f} degrees. "
                f"That's getting warm. You might want to check the airflow."
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
# Progress Tracking (F-005)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Self-Care Nudges (F-008)
# ---------------------------------------------------------------------------


async def check_selfcare():
    """Every 15 min: check meal, meds, hydration, movement."""
    try:
        from selfcare_manager import check_selfcare as _check

        await _check()
    except Exception as e:
        logger.error(f"[SELFCARE] Check failed: {e}")


# ---------------------------------------------------------------------------
# Routine Scaffolding (F-006)
# ---------------------------------------------------------------------------


async def trigger_routine(routine_id: str):
    """Called by APScheduler at routine trigger time."""
    try:
        from routine_manager import _active_session, start_routine

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
        import progress_tracker

        summary = await progress_tracker.daily_summary()
        await _announce_voice(summary)
        logger.info("[PROGRESS] Daily summary announced")
    except Exception as e:
        logger.error(f"[PROGRESS] Daily summary failed: {e}")


async def weekly_progress_digest():
    """Announce weekly progress digest via TTS."""
    try:
        import progress_tracker

        summary = await progress_tracker.weekly_summary()
        await _announce_voice(summary)
        logger.info("[PROGRESS] Weekly digest announced")
    except Exception as e:
        logger.error(f"[PROGRESS] Weekly digest failed: {e}")
