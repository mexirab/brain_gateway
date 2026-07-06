"""
Background jobs: calendar polling, morning + evening briefings, email polling,
email-to-calendar event extraction.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from orchestrator import shared, state_store
from orchestrator.google_calendar import get_calendar_client
from orchestrator.google_gmail import get_gmail_client
from orchestrator.metrics import (
    CALENDAR_POLL_EVENTS_FOUND,
    EMAIL_TO_CALENDAR_EMAILS_SCANNED,
    EMAIL_TO_CALENDAR_EVENTS_CREATED,
    EVENING_BRIEFING_LAST_RUN,
    MORNING_BRIEFING_LAST_RUN,
)
from orchestrator.reminder_manager import _announce_voice, list_pending_reminders
from orchestrator.shared import TIMEZONE, profile
from orchestrator.travel_time import get_travel_time

logger = logging.getLogger(__name__)

_LLM_URL = shared.MODEL_URL
_LLM_MODEL = shared.MODEL_NAME

# NWS Weather API (free, no key needed, US only)
_NWS_FORECAST_URL = None  # resolved lazily from lat/lon


async def _get_weather_forecast() -> str | None:
    """Fetch today's weather forecast from the National Weather Service API.

    Uses the user's home address coordinates. Returns a spoken-friendly
    summary or None on failure.
    """
    global _NWS_FORECAST_URL

    try:
        # Lazy resolve: geocode the grid endpoint once
        if _NWS_FORECAST_URL is None:
            lat = float(os.environ.get("WEATHER_LAT", "0"))
            lon = float(os.environ.get("WEATHER_LON", "0"))

            if lat == 0.0 and lon == 0.0 and shared.profile.home_address:
                try:
                    async with httpx.AsyncClient(timeout=10) as client:
                        r = await client.get(
                            "https://nominatim.openstreetmap.org/search",
                            params={"q": shared.profile.home_address, "format": "json", "limit": 1},
                            headers={"User-Agent": "BrainGateway/1.0 (personal assistant)"},
                        )
                        if r.status_code == 200:
                            results = r.json()
                            if results:
                                lat, lon = float(results[0]["lat"]), float(results[0]["lon"])
                except (httpx.HTTPError, httpx.TimeoutException, ValueError) as e:
                    logger.debug("[WEATHER] Geocoding failed: %s", e)

            if lat == 0.0 and lon == 0.0:
                return None

            # Get NWS grid point
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                r = await client.get(
                    f"https://api.weather.gov/points/{lat},{lon}",
                    headers={"User-Agent": "BrainGateway/1.0 (personal assistant)"},
                )
                if r.status_code == 200:
                    _NWS_FORECAST_URL = r.json()["properties"]["forecast"]
                else:
                    logger.warning(f"[WEATHER] NWS points failed: {r.status_code}")
                    return None

        # Fetch forecast
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(
                _NWS_FORECAST_URL,
                headers={"User-Agent": "BrainGateway/1.0 (personal assistant)"},
            )
            if r.status_code != 200:
                return None

            periods = r.json()["properties"]["periods"]
            # Get today's daytime forecast (first period)
            if periods:
                p = periods[0]
                temp = p["temperature"]
                unit = p["temperatureUnit"]
                short = p["shortForecast"]
                return f"Weather today: {short}, high of {temp} degrees {unit}."

    except (httpx.HTTPError, httpx.TimeoutException, KeyError, IndexError) as e:
        logger.warning(f"[WEATHER] Forecast fetch failed: {e}")
    except Exception as e:
        logger.error(f"[WEATHER] Unexpected error: {e}", exc_info=True)

    return None


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
                        result = await _announce_voice(message, announcement_type="calendar")
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
                        result = await _announce_voice(message, announcement_type="calendar")
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

                        result = await _announce_voice(message, announcement_type="calendar")
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
                result = await _announce_voice(message, announcement_type="calendar")
                if result.get("success"):
                    state_store.mark_notified(cal_key)
                    CALENDAR_POLL_EVENTS_FOUND.inc()
                    logger.info(f"[CALENDAR_POLL] Announced: {event.title} {time_str}", extra={"component": "calendar"})
                else:
                    logger.error(
                        f"[CALENDAR_POLL] TTS FAILED for '{event.title}': {result.get('error')} — will retry next poll",
                        extra={"component": "calendar"},
                    )

    except (httpx.HTTPError, httpx.TimeoutException) as e:
        logger.warning(f"[CALENDAR_POLL] Calendar API unavailable: {e}")
    except Exception as e:
        logger.error(f"[CALENDAR_POLL] Unexpected error: {e}", exc_info=True)


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


def build_missed_recap(undelivered: list) -> str:
    """One TTS-friendly sentence owning up to reminders that never reached
    the user (status missed/failed in the last 24h).

    Plain and factual, no guilt: name up to three, point at the dashboard
    for the rest. Pure function so the trust-layer recap is unit-testable
    without the whole briefing.
    """
    n = len(undelivered)
    texts = [str(r.get("text", "")).strip() for r in undelivered[:3]]
    named = ", ".join(t for t in texts if t)
    sentence = f"Heads up: {n} reminder{'s' if n != 1 else ''} didn't reach you in the last day"
    if named:
        sentence += f": {named}"
    if n > 3:
        sentence += f", and {n - 3} more"
    sentence += ". They're listed on the dashboard."
    return sentence


async def morning_briefing():
    """Morning announcement: today's events from all calendars via TTS.

    Sources (in priority order):
    1. Phone calendar sync (consolidated: Gmail + iCloud + Work)
    2. Google Calendar API (fallback if phone hasn't synced)
    """
    tz = ZoneInfo(TIMEZONE)
    today = datetime.now(tz).date()

    # Dead-man's-switch heartbeat: proves the scheduler fired the daily job.
    # Stamped at entry (not after delivery) so the signal is "the job ran",
    # independent of whether there were events. Watched by MorningBriefingStale.
    MORNING_BRIEFING_LAST_RUN.set_to_current_time()

    try:
        # Build unified event list from available sources
        briefing_events = []

        # Source 1: Phone calendar sync (preferred — has ALL calendars)
        phone_age = (
            time.time() - shared._phone_calendar_sync_time if shared._phone_calendar_sync_time > 0 else float("inf")
        )
        phone_parsed_count = 0  # how many phone records had a valid start time
        phone_fresh = bool(shared._phone_calendar_events) and phone_age < 86400  # synced within 24h
        if phone_fresh:
            for ev in shared._phone_calendar_events:
                try:
                    start_str = ev.get("start", "")
                    start = _parse_phone_datetime(start_str, tz)
                    phone_parsed_count += 1
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

        # Defensive: if the phone cache is fresh and has records but NONE
        # parsed (e.g. the iPhone Shortcut is posting empty payloads — observed
        # 2026-04-17 where all records had title='' and start=''), treat the
        # sync as broken and fall through to Google rather than announcing
        # "your calendar is clear" off a corrupted source. Mirrors the same
        # guard in tool_check_calendar / get_ambient_status.
        if phone_fresh and phone_parsed_count == 0:
            logger.warning(
                f"[MORNING_BRIEFING] Phone cache has {len(shared._phone_calendar_events)} records but zero parsed — "
                "likely broken iPhone Shortcut payload. Falling through to Google."
            )

        if phone_fresh and phone_parsed_count > 0:
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

        # Auto-clear DND (sleep mode) on morning briefing (only during morning hours)
        if shared.DND_ACTIVE and 5 <= datetime.now(tz).hour <= 11:
            shared.DND_ACTIVE = False
            state_store.clear_notification_flag("dnd_active")
            logger.info("[DND] Auto-cleared sleep mode for morning briefing")

        # Build announcement
        parts = [f"Good morning {profile.user_name}!"]

        # Weather forecast
        weather = await _get_weather_forecast()
        if weather:
            parts.append(weather)

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

        # Evening-ritual pickup: offer back the one thing parked last night
        # (see evening_briefing). Cleared only after a successfully SPOKEN
        # announce so a failed or suppressed TTS run doesn't silently eat the
        # parked item. A failed lookup must never sink the briefing, and a
        # stale item (evening job disabled/failing for days) must not be
        # passed off as "last night" — drop it instead.
        parked = None
        try:
            parked_entry = state_store.get_app_state_entry("parked_item")
        except Exception as parked_err:
            logger.warning(f"[MORNING_BRIEFING] Parked-item lookup failed: {parked_err}")
            parked_entry = None
        if parked_entry:
            try:
                age_h = (datetime.now() - datetime.fromisoformat(parked_entry["updated_at"])).total_seconds() / 3600
            except (ValueError, TypeError):
                age_h = None
            if age_h is not None and age_h <= 36:
                parked = parked_entry["value"]
            else:
                logger.info(f"[MORNING_BRIEFING] Dropping stale parked item ({age_h and round(age_h)}h old)")
                state_store.delete_app_state("parked_item")
        if parked:
            parts.append(f"Last night you parked: {parked}. It's ready when you are.")

        pending = list_pending_reminders()
        if pending:
            parts.append(f"You also have {len(pending)} reminder{'s' if len(pending) > 1 else ''} pending.")

        # Trust layer: own up to anything that didn't reach the user in the
        # last 24h instead of letting it vanish silently. Failure to build
        # the recap must never sink the whole briefing.
        try:
            undelivered = [
                r for r in state_store.get_recent_reminder_outcomes(hours=24) if r.get("status") in ("missed", "failed")
            ]
        except Exception as trust_err:
            logger.warning(f"[MORNING_BRIEFING] Missed-recap lookup failed: {trust_err}")
            undelivered = []
        if undelivered:
            recap = build_missed_recap(undelivered)
            parts.append(recap)
            # Mirror the recap to Telegram so it's actionable away from the
            # speakers. Fire-and-forget with a strong task ref (a bare
            # create_task can be GC'd mid-flight); no-ops when the bot is off.
            try:
                from orchestrator.telegram_bot import fire_system_message

                lines = "\n".join(f"• {r['text']}" for r in undelivered[:10])
                fire_system_message(f"⚠️ Reminders that didn't reach you in the last day:\n{lines}")
            except Exception as tg_err:
                logger.warning(f"[MORNING_BRIEFING] Telegram recap dispatch failed: {tg_err}")

        # `min_volume` floors the speaker at MORNING_BRIEFING_MIN_VOLUME
        # before play_media — defeats the "speaker still at sleep-sound
        # volume" failure mode (see 2026-04-30 incident: briefing played at
        # volume_level=0.10 because the bedroom_pair was still on the
        # overnight fireplace track).
        min_vol = shared.MORNING_BRIEFING_MIN_VOLUME if shared.MORNING_BRIEFING_MIN_VOLUME > 0 else None
        # Pass speaker=None so _announce_voice consults the Speakers panel
        # via announcement_routes.route_for("briefing"), which itself falls
        # back to MORNING_BRIEFING_SPEAKER when nothing is configured.
        result = await _announce_voice(
            " ".join(parts),
            speaker=None,
            announcement_type="briefing",
            min_volume=min_vol,
        )
        if parked and result.get("success") and not result.get("suppressed"):
            state_store.delete_app_state("parked_item")
        logger.info(f"[MORNING_BRIEFING] Delivered: {len(briefing_events)} events, {len(pending)} reminders")

    except Exception as e:
        logger.error(f"[MORNING_BRIEFING] Error: {e}")


async def evening_briefing():
    """Evening shutdown ritual: the mirror of the morning briefing.

    Tomorrow's first event (+ leave-by time when it has a physical location),
    evening meds check, and parking one unfinished thing (F-007) so the
    morning briefing can offer it back.

    Event sources mirror morning_briefing: phone calendar sync first (with the
    same zero-parsed guard), Google Calendar API fallback. Runs fine with no
    calendar at all — meds + parking still deliver.
    """
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    tomorrow = (now + timedelta(days=1)).date()

    # Dead-man's-switch heartbeat, same contract as the morning gauge:
    # stamped at entry so the signal is "the job ran". EveningBriefingStale.
    EVENING_BRIEFING_LAST_RUN.set_to_current_time()

    try:
        # Build tomorrow's event list (phone-first, Google fallback)
        events = []
        phone_age = (
            time.time() - shared._phone_calendar_sync_time if shared._phone_calendar_sync_time > 0 else float("inf")
        )
        phone_parsed_count = 0
        phone_fresh = bool(shared._phone_calendar_events) and phone_age < 86400
        if phone_fresh:
            for ev in shared._phone_calendar_events:
                try:
                    start = _parse_phone_datetime(ev.get("start", ""), tz)
                    phone_parsed_count += 1
                    if start.tzinfo is None:
                        start = start.replace(tzinfo=tz)
                    if start.date() != tomorrow:
                        continue
                    events.append(
                        {
                            "title": ev.get("title", "(No title)"),
                            "start": start,
                            "all_day": ev.get("all_day", False),
                            "location": ev.get("location", ""),
                        }
                    )
                except (ValueError, TypeError):
                    continue

        # Same guard as morning_briefing: fresh-but-corrupted phone cache must
        # not read as "clear tomorrow" — fall through to Google instead.
        if phone_fresh and phone_parsed_count == 0:
            logger.warning(
                f"[EVENING_BRIEFING] Phone cache has {len(shared._phone_calendar_events)} records but zero parsed — "
                "likely broken iPhone Shortcut payload. Falling through to Google."
            )

        if not (phone_fresh and phone_parsed_count > 0):
            client = get_calendar_client()
            if client and client.is_configured:
                response = await client.list_events(days_ahead=2)
                if response.success:
                    for event in response.events:
                        start = event.start
                        if start.tzinfo is None:
                            start = start.replace(tzinfo=tz)
                        if start.date() != tomorrow:
                            continue
                        events.append(
                            {
                                "title": event.title,
                                "start": start,
                                "all_day": event.all_day,
                                "location": event.location,
                            }
                        )
            else:
                logger.info("[EVENING_BRIEFING] No calendar source available")

        events.sort(key=lambda e: (e["all_day"], e["start"]))
        timed = [e for e in events if not e["all_day"]]

        parts = [f"Alright {profile.user_name}, wrapping up the day."]

        # Tomorrow's first event + leave-by time
        first = timed[0] if timed else (events[0] if events else None)
        if first is None:
            parts.append("Nothing on the calendar tomorrow.")
        elif first["all_day"]:
            parts.append(f"Tomorrow, all day: {first['title']}.")
        else:
            time_str = first["start"].strftime("%I:%M %p").lstrip("0")
            parts.append(f"Tomorrow's first thing: {first['title']} at {time_str}.")
            if first["location"] and shared.GOOGLE_MAPS_API_KEY and shared.HOME_ADDRESS:
                travel = await get_travel_time(shared.HOME_ADDRESS, first["location"], first["start"])
                if travel:
                    drive_min = travel.duration_in_traffic_minutes
                    leave_at = first["start"] - timedelta(minutes=drive_min + shared.TRAVEL_TIME_BUFFER)
                    leave_str = leave_at.strftime("%I:%M %p").lstrip("0")
                    parts.append(f"It's about a {drive_min} minute drive, so plan to leave around {leave_str}.")
        if len(events) > 1:
            more = len(events) - 1
            parts.append(f"Plus {more} more event{'s' if more > 1 else ''} tomorrow.")

        # Evening meds check — a failed lookup must never sink the ritual.
        try:
            from orchestrator.selfcare_manager import evening_meds_status

            meds = evening_meds_status()
        except Exception as meds_err:
            logger.warning(f"[EVENING_BRIEFING] Meds status lookup failed: {meds_err}")
            meds = None
        if meds is not None:
            if meds["confirmed"]:
                parts.append("Evening meds are logged — nice.")
            else:
                parts.append("Evening meds check: you haven't logged them yet.")

        # Park one unfinished thing (F-007): active focus task first, else the
        # top open backlog task. Persisted in app_state so the morning
        # briefing can offer it back even across a restart.
        parked = None
        parked_from_focus = False
        if shared.current_focus_session.get("active"):
            parked = shared.current_focus_session.get("task_description") or shared.current_focus_session.get("task")
            parked_from_focus = bool(parked)
        if not parked:
            try:
                open_tasks = state_store.list_tasks("open")
            except Exception as task_err:
                logger.warning(f"[EVENING_BRIEFING] Backlog lookup failed: {task_err}")
                open_tasks = []
            if open_tasks:
                parked = open_tasks[0]["text"]
        if parked:
            try:
                state_store.set_app_state("parked_item", parked)
            except Exception as park_err:
                logger.warning(f"[EVENING_BRIEFING] Failed to persist parked item: {park_err}")
                parked = None
        if parked:
            if parked_from_focus:
                parts.append(f"You were in the middle of {parked} — I've parked it for tomorrow.")
            else:
                parts.append(f"I'm parking one thing for the morning: {parked}.")
            parts.append("It'll be waiting — you're done for today.")
        else:
            parts.append("Nothing left to park. You're done for today.")

        text = " ".join(parts)

        # If the user already said goodnight, park silently — no speakers, no
        # phone buzz. The morning briefing still offers the parked item back.
        if shared.DND_ACTIVE:
            logger.info("[EVENING_BRIEFING] DND active — parked silently, skipping announce")
            return

        # Same silent treatment if a guided routine walkthrough is mid-flight
        # (the evening routine auto-triggers at 21:00 and covers meds +
        # tomorrow's calendar itself) — don't talk over its step nudges.
        try:
            from orchestrator import routine_manager

            if routine_manager._active_session is not None:
                logger.info("[EVENING_BRIEFING] Routine session active — parked silently, skipping announce")
                return
        except Exception:
            pass

        await _announce_voice(text, speaker=None, announcement_type="briefing")

        # Mirror to Telegram so the ritual also lands away from the speakers.
        # Fire-and-forget with a strong task ref; no-ops when the bot is off.
        try:
            from orchestrator.telegram_bot import fire_system_message

            fire_system_message(f"🌙 {text}")
        except Exception as tg_err:
            logger.warning(f"[EVENING_BRIEFING] Telegram dispatch failed: {tg_err}")

        logger.info(
            f"[EVENING_BRIEFING] Delivered: {len(events)} tomorrow events, "
            f"meds={'n/a' if meds is None else meds['confirmed']}, parked={'yes' if parked else 'no'}"
        )

    except Exception as e:
        logger.error(f"[EVENING_BRIEFING] Error: {e}")


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
        from orchestrator.orchestrator import call_model

        created_count = 0
        created_keys: list = []  # (title_lower, date) of events created this run
        for msg in new_msgs:
            state_store.mark_notified(f"e2c:{msg.id}")
            EMAIL_TO_CALENDAR_EMAILS_SCANNED.inc()

            # Skip very short emails (unlikely to contain event details)
            body = msg.body_text or msg.snippet
            if len(body) < 30:
                continue

            # Ask model to extract events
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

            # One calendar fetch per email (was one Google API call per
            # extracted event). Events we create during this run are tracked
            # in created_keys so within-batch duplicates still dedup. The window
            # is sized to cover the furthest extracted event (see #33).
            listing = await cal.list_events(days_ahead=_dedup_prefetch_days(events, now.date()), calendar_id="primary")
            day_events = listing.events if listing.success else []

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

                # Check for duplicates: look for a title match on that day
                if _event_exists(day_events, created_keys, title, ev_start):
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
                    created_keys.append((title.lower(), ev_start.date()))
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
        lines = [line for line in lines if not line.strip().startswith("```")]
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


def _dedup_prefetch_days(events: list, today) -> int:
    """days_ahead for the dedup pre-fetch, sized to cover the furthest extracted
    event. list_events queries [now, now + days_ahead); a fixed days_ahead=1
    missed any event >1 day out and re-created it as a duplicate every scan
    (#33). +1 reaches through the event's own day; floor of 1 covers same-day
    or clock-skewed-past events. Unparseable start times are ignored."""
    event_dates = []
    for ev in events:
        with contextlib.suppress(ValueError, AttributeError):
            event_dates.append(datetime.fromisoformat(ev.get("start_time", "").strip()).date())
    return max(((max(event_dates) - today).days + 1) if event_dates else 1, 1)


def _event_exists(day_events: list, created_keys: list, title: str, start: datetime) -> bool:
    """Check if a similar event exists in the pre-fetched list or was created this run."""
    title_lower = title.lower()
    for existing in day_events:
        # Check same day and similar title (substring match either direction)
        if existing.start.date() != start.date():
            continue
        existing_title = existing.title.lower()
        if title_lower in existing_title or existing_title in title_lower:
            return True
    for created_title, created_date in created_keys:
        if created_date != start.date():
            continue
        if title_lower in created_title or created_title in title_lower:
            return True
    return False
