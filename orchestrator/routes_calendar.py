"""Calendar and email-to-calendar API routes."""

import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from orchestrator import shared
from orchestrator.metrics import PHONE_CALENDAR_LAST_SYNC, PHONE_CALENDAR_SYNC_REJECTED

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/email-to-calendar/run")
async def run_email_to_calendar():
    """Manually trigger email-to-calendar extraction."""
    from orchestrator.background_jobs import process_emails_for_events

    try:
        await process_emails_for_events()
        return {"ok": True, "message": "Email-to-calendar scan completed"}
    except Exception as e:
        logger.error(f"[EMAIL_TO_CAL] Manual trigger error: {e}")
        return JSONResponse({"error": "Email-to-calendar scan failed"}, status_code=500)


@router.get("/api/calendar/today")
async def calendar_today():
    """Today's events for the dashboard, from every calendar source merged.

    Sources: the self-hosted personal calendar (CalDAV), the work calendar
    pushed from the iPhone (Exchange), and Google while it's still enabled —
    see calendar_sources. ``unavailable`` names sources that should have
    answered but couldn't, so the card can say so instead of showing a
    partial day as complete.
    """
    from zoneinfo import ZoneInfo

    from orchestrator import calendar_sources
    from orchestrator.calendar_types import SOURCE_LABELS

    # Same timezone calendar_sources builds its day window with.
    tz = ZoneInfo(shared.TIMEZONE)
    today = datetime.now(tz).date()
    resp = await calendar_sources.events_on(today)
    unavailable = sorted({SOURCE_LABELS.get(s, s) for s in resp.failed_sources})
    if not resp.success:
        return {"ok": True, "events": [], "source": "none", "count": 0, "unavailable": unavailable}

    events = []
    for ev in resp.events:
        label = calendar_sources.event_label(ev)
        events.append(
            {
                "id": ev.id,
                "title": ev.title,
                "start": ev.start.isoformat(),
                "end": ev.end.isoformat(),
                "location": ev.location or None,
                "description": None,
                "all_day": ev.all_day,
                "calendar": label or (ev.calendar_name or SOURCE_LABELS.get(ev.source, ev.source)),
                "source": ev.source,
            }
        )
    return {"ok": True, "events": events, "source": "merged", "count": len(events), "unavailable": unavailable}


# Limits on what the iPhone Shortcut may push (the body is otherwise stored as-is).
_SYNC_MAX_BYTES = 1_000_000
_SYNC_MAX_EVENTS = 1000
_SYNC_FIELDS = {"title": 300, "start": 64, "end": 64, "location": 300, "calendar": 100, "id": 200}
_SYNC_SOURCES = ("exchange", "phone")


def _clean_sync_record(ev: dict) -> dict:
    """Keep only the fields Jess uses, capped. iOS sometimes adds a trailing
    space to keys ("calendar "), so both spellings are accepted. Descriptions
    and notes are dropped — they're never shown and can be large/sensitive."""
    out: dict = {}
    for key, cap in _SYNC_FIELDS.items():
        v = ev.get(key, ev.get(key + " "))
        if v is None:
            continue
        out[key] = " ".join((v if isinstance(v, str) else str(v)).split())[:cap]
    all_day = ev.get("all_day", ev.get("all_day "))
    if all_day is not None:
        out["all_day"] = all_day if isinstance(all_day, bool) else str(all_day)[:8]
    return out


def _reject(reason: str, message: str, status: int = 400) -> JSONResponse:
    PHONE_CALENDAR_SYNC_REJECTED.labels(reason=reason).inc()
    logger.warning(f"[PHONE_SYNC] Rejected push: {reason}")
    return JSONResponse({"ok": False, "error": message}, status_code=status)


@router.api_route("/api/calendar/sync", methods=["GET", "POST", "PUT"])
async def sync_phone_calendar(req: Request):
    """Receive the work (Exchange) calendar from the iPhone Shortcut, or return status.

    GET: sync status (last sync age, event count, source).
    POST/PUT: a FULL snapshot that replaces the previous one. The Shortcut must
    collect every event into a list and post it ONCE — a post per event inside
    a Repeat loop would leave only the last event, so a bare single-event
    object is rejected (400) rather than silently stored.

    Accepted bodies:
    1. {"source": "exchange", "events": [...]}   -- the work-calendar Shortcut (read by Jess)
    2. {"events": [...]} / [...]                 -- no source: legacy push, stored as "phone"
                                                    and ignored by calendar_sources (retired)
    3. {"events": {"0": {...}, "1": {...}}}      -- iOS dict-of-dicts (converted)

    A snapshot with records but none parseable is rejected (422) and the
    previous snapshot is kept, so a broken Shortcut shows up as a stale sync
    (ExchangeCalendarSyncStale) instead of an empty calendar.
    """
    from orchestrator.calendar_sources import phone_record_to_event

    if req.method == "GET":
        if shared._phone_calendar_sync_time > 0:
            age_min = int((time.time() - shared._phone_calendar_sync_time) / 60)
            sync_age = f"{age_min} minutes ago"
        else:
            sync_age = "never"
        return {
            "ok": True,
            "synced": shared._phone_calendar_sync_time > 0,
            "last_sync": sync_age,
            "event_count": len(shared._phone_calendar_events),
            "source": shared._phone_calendar_source or None,
        }

    try:
        # Read with a running cap: RequestSizeLimitMiddleware only checks
        # Content-Length, and a chunked body would otherwise be buffered whole.
        chunks: list[bytes] = []
        size = 0
        async for chunk in req.stream():
            size += len(chunk)
            if size > _SYNC_MAX_BYTES:
                return _reject("too_large", "body too large", 413)
            chunks.append(chunk)
        raw_body = b"".join(chunks)
        if not raw_body:
            return _reject("empty", "empty body")
        # Log only the size: the body holds calendar titles and locations.
        logger.debug(f"[PHONE_SYNC] Raw body ({len(raw_body)} bytes)")

        try:
            body = json.loads(raw_body)
        except ValueError:
            return _reject("bad_json", "body is not JSON")

        # The work-calendar Shortcut sends "source": "exchange". A body without
        # it is the legacy all-calendars push ("phone"): stored, but not read
        # (calendar_sources treats it as retired) — never mislabelled as work.
        source = "phone"
        if isinstance(body, list):
            events = body
        elif isinstance(body, dict):
            if body.get("source") in _SYNC_SOURCES:
                source = body["source"]
            if "events" not in body:
                return _reject("single_event", 'expected {"events": [...]} with the full list, posted once')
            events = body["events"]
            if isinstance(events, dict):
                if events and all(isinstance(v, dict) for v in events.values()):
                    events = list(events.values())
                else:
                    return _reject("single_event", "expected a list of events, posted once — not one event per request")
        else:
            return _reject("bad_shape", "unexpected body type")

        if not isinstance(events, list):
            return _reject("bad_shape", "events must be a list")
        if len(events) > _SYNC_MAX_EVENTS:
            return _reject("too_many", f"at most {_SYNC_MAX_EVENTS} events per sync", 413)

        cleaned = [_clean_sync_record(ev) for ev in events if isinstance(ev, dict)]
        tz = ZoneInfo(shared.TIMEZONE)
        parsed = 0
        for rec in cleaned:
            try:
                phone_record_to_event(rec, tz, source)
                parsed += 1
            except (ValueError, TypeError, AttributeError):
                continue
        if events and parsed == 0:
            return _reject("unparseable", "no event had a readable start time — snapshot not stored", 422)

        shared._phone_calendar_events = cleaned
        shared._phone_calendar_source = source
        shared._phone_calendar_sync_time = time.time()
        shared._save_phone_calendar()
        if source == "exchange":
            PHONE_CALENDAR_LAST_SYNC.set(shared._phone_calendar_sync_time)
        logger.info(f"[PHONE_SYNC] Stored {source} snapshot: {parsed}/{len(cleaned)} event(s) parseable")

        return {
            "ok": True,
            "source": source,
            "used": source == "exchange",
            "events_received": len(cleaned),
            "events_parsed": parsed,
            "message": f"Synced {len(cleaned)} calendar events",
        }
    except Exception as e:
        logger.error(f"[PHONE_SYNC] Error: {type(e).__name__}")
        return JSONResponse({"error": "Calendar sync failed"}, status_code=500)
