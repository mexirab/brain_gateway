"""Calendar and email-to-calendar API routes."""

import logging
import re
import time
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import shared
from google_calendar import get_calendar_client
from shared import profile

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/email-to-calendar/run")
async def run_email_to_calendar():
    """Manually trigger email-to-calendar extraction."""
    from background_jobs import process_emails_for_events

    try:
        await process_emails_for_events()
        return {"ok": True, "message": "Email-to-calendar scan completed"}
    except Exception as e:
        logger.error(f"[EMAIL_TO_CAL] Manual trigger error: {e}")
        return JSONResponse({"error": "Email-to-calendar scan failed"}, status_code=500)


@router.get("/api/calendar/today")
async def calendar_today():
    """Get today's calendar events for the dashboard.

    Merges events from two sources:
    1. Phone calendar sync (iPhone Shortcut -- Outlook + Google + iCloud)
    2. Google Calendar API (fallback if phone sync is stale/missing)

    Phone sync is preferred when fresh (<24h old) since it aggregates all
    iPhone calendars. Google Calendar is used as fallback. Events are
    deduplicated by title + start time to avoid duplicates when Google
    events appear in both sources.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(profile.timezone)
    today = datetime.now(tz).date()
    merged: list[dict] = []
    seen: set[str] = set()  # "title|start_iso" for dedup
    source = "none"

    def _parse_phone_datetime(s: str) -> datetime:
        """Parse date strings from iPhone Shortcuts.

        Handles formats like:
        - "Mar 4, 2026 at 10:00\u202fAM"  (narrow no-break space before AM/PM)
        - "Mar 4, 2026 at 1:00 PM"         (regular space)
        - "2026-03-04T10:00:00"            (ISO format)
        """
        if not s:
            raise ValueError("empty date string")
        # Try ISO first
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            pass
        # Normalize unicode spaces and "at" keyword
        cleaned = s.replace("\u202f", " ").replace("\u00a0", " ").replace(" at ", " ")
        # Remove extra whitespace
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        # Try common iOS Shortcut formats
        for fmt in (
            "%b %d, %Y %I:%M %p",  # "Mar 4, 2026 1:00 PM"
            "%B %d, %Y %I:%M %p",  # "March 4, 2026 1:00 PM"
            "%m/%d/%Y %I:%M %p",  # "03/04/2026 1:00 PM"
            "%b %d, %Y",  # "Mar 4, 2026" (all-day)
        ):
            try:
                return datetime.strptime(cleaned, fmt)
            except ValueError:
                continue
        raise ValueError(f"unrecognized date format: {s!r}")

    # Source 1: Phone calendar sync (has ALL calendars)
    phone_age = time.time() - shared._phone_calendar_sync_time if shared._phone_calendar_sync_time > 0 else float("inf")
    if shared._phone_calendar_events and phone_age < 86400:
        source = "phone"
        for ev in shared._phone_calendar_events:
            try:
                start_str = ev.get("start", "")
                start = _parse_phone_datetime(start_str)
                if start.tzinfo is None:
                    start = start.replace(tzinfo=tz)
                if start.date() != today:
                    continue

                end_str = ev.get("end", "")
                end = None
                if end_str:
                    try:
                        end = _parse_phone_datetime(end_str)
                        if end.tzinfo is None:
                            end = end.replace(tzinfo=tz)
                    except ValueError:
                        pass

                title = ev.get("title", "(No title)")
                dedup_key = f"{title.lower().strip()}|{start.isoformat()}"
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                # Handle trailing-space key from iOS ("calendar " vs "calendar")
                cal_name = ev.get("calendar") or ev.get("calendar ") or ""

                merged.append(
                    {
                        "id": ev.get("id", f"phone_{len(merged)}"),
                        "title": title.strip(),
                        "start": start.isoformat(),
                        "end": end.isoformat() if end else start.isoformat(),
                        "location": ev.get("location") or None,
                        "description": ev.get("description") or None,
                        "all_day": ev.get("all_day", False),
                        "calendar": cal_name.strip(),
                        "source": "phone",
                    }
                )
            except (ValueError, TypeError) as exc:
                logger.warning(f"[CALENDAR] Skipping phone event: {exc} — raw: {ev}")
                continue
    else:
        # Source 2: Google Calendar API (fallback)
        source = "google"
        cal = get_calendar_client()
        if cal and cal.is_configured:
            result = await cal.list_events(days_ahead=1)
            if result.success:
                for e in result.events:
                    title = e.title
                    dedup_key = f"{title.lower()}|{e.start.isoformat()}"
                    seen.add(dedup_key)
                    merged.append(
                        {
                            "id": e.id,
                            "title": title,
                            "start": e.start.isoformat(),
                            "end": e.end.isoformat(),
                            "location": e.location or None,
                            "description": e.description or None,
                            "all_day": e.all_day,
                            "calendar": "Google",
                            "source": "google",
                        }
                    )

    # Sort by start time (all-day events first, then by time)
    merged.sort(key=lambda e: (0 if e.get("all_day") else 1, e["start"]))

    return {"events": merged, "source": source, "count": len(merged)}


@router.api_route("/api/calendar/sync", methods=["GET", "POST", "PUT"])
async def sync_phone_calendar(req: Request):
    """Receive consolidated calendar events from iPhone Shortcut, or return status.

    GET: Returns sync status (last sync time, event count).
    POST/PUT: Receives calendar events from iPhone Shortcut.

    Accepts multiple body formats for flexibility with iOS Shortcuts:
    1. {"events": [...]}           -- wrapped in events key
    2. [...]                       -- bare list at top level
    3. {"events": {"0": {...}}}    -- iOS dict-of-dicts (auto-converted)
    4. {"events": {single event}}  -- single event dict (auto-wrapped)
    """
    # GET or no body -> return status
    if req.method == "GET":
        sync_age = ""
        if shared._phone_calendar_sync_time > 0:
            age_min = int((time.time() - shared._phone_calendar_sync_time) / 60)
            sync_age = f"{age_min} minutes ago"
        else:
            sync_age = "never"
        return {
            "synced": shared._phone_calendar_sync_time > 0,
            "last_sync": sync_age,
            "event_count": len(shared._phone_calendar_events),
        }

    # POST/PUT -> receive events
    # iOS Shortcuts sends one event per request inside a Repeat loop.
    # Accumulate events arriving within 60s as a single batch.
    try:
        raw_body = await req.body()
        if not raw_body:
            return JSONResponse({"error": "empty body"}, status_code=400)

        body = await req.json()

        # Normalize: accept multiple input shapes from iOS Shortcuts
        if isinstance(body, list):
            events = body
        elif isinstance(body, dict):
            events = body.get("events", body)
            if isinstance(events, dict):
                if all(isinstance(v, dict) for v in events.values()):
                    events = list(events.values())
                else:
                    events = [events]
        else:
            return JSONResponse({"error": f"unexpected body type: {type(body).__name__}"}, status_code=400)

        if not isinstance(events, list):
            events = [events]

        # If last sync was >60s ago, start a new batch; otherwise append
        now = time.time()
        if now - shared._phone_calendar_sync_time > 60:
            shared._phone_calendar_events = events
            logger.info(f"[PHONE_SYNC] New batch started with {len(events)} event(s)")
        else:
            shared._phone_calendar_events.extend(events)
            logger.info(f"[PHONE_SYNC] Appended {len(events)} event(s), total now {len(shared._phone_calendar_events)}")

        shared._phone_calendar_sync_time = now
        shared._save_phone_calendar()

        return {
            "ok": True,
            "events_received": len(shared._phone_calendar_events),
            "message": f"Synced {len(shared._phone_calendar_events)} calendar events",
        }
    except Exception as e:
        logger.error(f"[PHONE_SYNC] Error: {e}")
        return JSONResponse({"error": "Calendar sync failed"}, status_code=500)
