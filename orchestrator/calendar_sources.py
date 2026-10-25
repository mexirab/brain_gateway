"""
calendar_sources — the ONE place Jess reads and writes calendars.

Sources, merged into one list (docs/CALENDAR.md, plan milestone 4):

  caldav    the self-hosted personal calendar (Radicale on Jupiter) — read/write
  exchange  the work calendar, pushed by the iPhone Shortcut to
            /api/calendar/sync — read-only, never written
  phone     legacy all-calendars iPhone push (pre-2026-10 Shortcut) — read-only
  google    Google Calendar — read (and write only when CalDAV is off) while
            CALENDAR_GOOGLE_ENABLED is true; switched off at milestone 5

Replaces four hand-copied "phone sync first, else Google" blocks. Behaviour
that matters:

* Sources are MERGED, not chosen: personal + work show up together.
* A source that is expected but can't be read is reported in
  ``CalendarResponse.failed_sources`` — callers say "I couldn't reach your work
  calendar" instead of presenting a partial list as the whole day.
* The phone push counts as expected only after it has synced at least once;
  then a push older than PHONE_CALENDAR_MAX_AGE_HOURS is "stale" (failed), and
  records-present-but-none-parse is "corrupt" (failed, the 2026-04-17 guard).
* Duplicates (same title + start) collapse, preferring caldav > phone > google.
* Windows use overlap semantics: an event is included if any part of it falls
  in [start, end) — multi-day and in-progress events are not dropped.

``get_calendar()`` returns a facade with the old Google-client surface
(``is_configured``, ``list_events``, ``get_upcoming``, ``create_event``) so the
poller, routines, decide_for_me and email→calendar keep their call shapes.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx

from orchestrator import shared
from orchestrator.caldav_calendar import MAX_DESCRIPTION, MAX_LOCATION, MAX_TITLE, get_caldav_client
from orchestrator.calendar_types import (
    SOURCE_CALDAV,
    SOURCE_EXCHANGE,
    SOURCE_GOOGLE,
    SOURCE_LABELS,
    SOURCE_PHONE,
    CalendarEvent,
    CalendarResponse,
)
from orchestrator.google_calendar import get_calendar_client as get_google_client
from orchestrator.metrics import CALENDAR_SOURCE_FETCH, CALENDAR_SOURCE_LATENCY, CALENDAR_WRITE

logger = logging.getLogger(__name__)

# Dedup preference: lower wins.
_PRIORITY = {SOURCE_CALDAV: 0, SOURCE_EXCHANGE: 1, SOURCE_PHONE: 1, SOURCE_GOOGLE: 2}

# Last read outcome per source, for /health and transition-only logging.
_last_result: dict[str, dict[str, Any]] = {}

# Per-source wall-clock budget for one read. The chat tool / dashboard path
# must not hang on a slow source (httpx timeouts are per phase, not total);
# a source that blows the budget is reported as failed, the rest still merge.
SOURCE_TIMEOUT_SECONDS = 8.0

# Pre-initialise label sets so increase()/ratio panels don't start at "No data".
for _src, _results in (
    (SOURCE_CALDAV, ("ok", "error", "timeout")),
    (SOURCE_GOOGLE, ("ok", "error", "timeout")),
    (SOURCE_EXCHANGE, ("ok", "stale", "corrupt")),
):
    for _res in _results:
        CALENDAR_SOURCE_FETCH.labels(source=_src, result=_res)

# Per-process key for log-only event references: correlates log lines within a
# run without exposing ids derived from titles (phone ids) or raw UIDs.
_LOG_KEY = os.urandom(16)


def log_ref(event_id: str) -> str:
    """Opaque, per-process short reference for an event id — safe to log."""
    return hmac.new(_LOG_KEY, event_id.encode(), "sha256").hexdigest()[:10]


def _tz() -> ZoneInfo:
    return ZoneInfo(shared.TIMEZONE)


def _record(source: str, result: str, started: Optional[float] = None) -> None:
    """Count a read and log only when a source's result CHANGES — poll, ambient
    and the dashboard read every few minutes, so per-read WARNINGs would flood
    Loki and the nightly self-audit."""
    CALENDAR_SOURCE_FETCH.labels(source=source, result=result).inc()
    if started is not None:
        CALENDAR_SOURCE_LATENCY.labels(source=source).observe(time.monotonic() - started)
    previous = _last_result.get(source, {}).get("result")
    _last_result[source] = {"result": result, "at": time.time()}
    if previous != result:
        level = logging.INFO if result == "ok" else logging.WARNING
        logger.log(
            level,
            f"[CALENDAR] Source {source}: {previous or 'first read'} -> {result}",
            extra={"component": "calendar"},
        )


# ---------------------------------------------------------------------------
# Phone (iPhone Shortcut) push
# ---------------------------------------------------------------------------


def parse_phone_datetime(s: str, tz: Optional[ZoneInfo] = None) -> datetime:
    """Parse date strings from iPhone Shortcuts.

    Handles "Mar 4, 2026 at 10:00 AM" (narrow no-break space), ISO, and a
    few other iOS formats. Returns a naive or aware datetime as parsed
    (``tz`` is accepted for call-site compatibility; callers localise).
    """
    if not s or not isinstance(s, str):
        raise ValueError("empty date string")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    cleaned = s.replace(" ", " ").replace(" ", " ").replace(" at ", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    for fmt in ("%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p", "%m/%d/%Y %I:%M %p", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    raise ValueError("unrecognized date format")


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y")
    return bool(v)


def _field(ev: dict, key: str) -> str:
    """iOS Shortcuts sometimes adds a trailing space to dict keys ("calendar ")."""
    v = ev.get(key)
    if v is None:
        v = ev.get(key + " ")
    return v if isinstance(v, str) else ("" if v is None else str(v))


def one_line(text: str) -> str:
    """Collapse all whitespace (incl. newlines) to single spaces. Titles and
    locations from external invites must not be able to fake extra lines in
    check_calendar output or briefings."""
    return " ".join(text.split())


def _neutralize_event_text(ev: CalendarEvent) -> None:
    """Rewrite chat-template / tool-call markup in an event's text, in place.

    Titles, locations and descriptions come from outside invites (work
    Exchange, Google, invites accepted into CalDAV). The brain is protected
    at its two inputs (tool results, client chat history), but spoken
    announcements send titles raw to the TTS model and future readers may
    send them to a model directly, so every reader gets plain text from here
    (2026-10-25 review). `<|im_start|>` / `<tool_call>` never occur in real
    titles, so nothing visible changes.
    """
    from orchestrator.unified_loop import _neutralize_control_markup  # local: unified_loop is a heavy import

    for field in ("title", "location", "description", "calendar_name"):
        value = getattr(ev, field)
        if isinstance(value, str) and "<" in value:
            setattr(ev, field, _neutralize_control_markup(value))


def phone_record_to_event(ev: dict, tz: ZoneInfo, default_source: str) -> CalendarEvent:
    """One pushed record → CalendarEvent. Raises ValueError/TypeError if unusable."""
    if not isinstance(ev, dict):
        raise TypeError("record is not an object")
    all_day = _truthy(ev.get("all_day", False))
    start = parse_phone_datetime(_field(ev, "start"), tz)
    start = start.replace(tzinfo=tz) if start.tzinfo is None else start.astimezone(tz)
    if all_day:
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end = None
    end_s = _field(ev, "end")
    if end_s:
        try:
            end = parse_phone_datetime(end_s, tz)
            end = end.replace(tzinfo=tz) if end.tzinfo is None else end.astimezone(tz)
        except ValueError:
            end = None
    if end is None or end < start:
        end = start + timedelta(days=1) if all_day else start
    elif all_day and end == start:
        end = start + timedelta(days=1)
    title = one_line(_field(ev, "title")) or "(No title)"
    source = ev.get("source") if ev.get("source") in (SOURCE_EXCHANGE, SOURCE_PHONE) else default_source
    ev_id = _field(ev, "id").strip()
    if not ev_id:
        digest = hashlib.sha1(f"{title}|{start.isoformat()}".encode()).hexdigest()[:16]
        ev_id = f"{source}:{digest}"
    return CalendarEvent(
        id=ev_id,
        title=title,
        start=start,
        end=end,
        location=one_line(_field(ev, "location")),
        description="",
        calendar_name=one_line(_field(ev, "calendar")),
        all_day=all_day,
        source=source,
    )


def phone_sync_expected() -> bool:
    """The iPhone push counts as a source once the WORK (Exchange) Shortcut has
    synced. A legacy all-calendars push (source "phone", the pre-2026-10
    Shortcut) is retired: ignored, so a months-old snapshot neither shows stale
    events nor makes every answer say "couldn't reach your phone calendar"."""
    return bool(
        shared.PHONE_CALENDAR_SYNC_ENABLED
        and shared._phone_calendar_sync_time > 0
        and shared._phone_calendar_source == SOURCE_EXCHANGE
    )


def _phone_window(start: datetime, end: datetime, tz: ZoneInfo) -> tuple[list[CalendarEvent], str, str]:
    """Return (events in window, result, source name) for the cached phone push.

    result: ok | stale | corrupt | off
    """
    default_source = SOURCE_EXCHANGE
    if not phone_sync_expected():
        return [], "off", default_source
    age = time.time() - shared._phone_calendar_sync_time
    if age > shared.PHONE_CALENDAR_MAX_AGE_HOURS * 3600:
        return [], "stale", default_source
    records = shared._phone_calendar_events or []
    parsed = 0
    out: list[CalendarEvent] = []
    for rec in records:
        try:
            ev = phone_record_to_event(rec, tz, default_source)
        except (ValueError, TypeError, AttributeError):
            continue
        parsed += 1
        if _overlaps(ev, start, end):
            out.append(ev)
    if records and parsed == 0:
        return [], "corrupt", default_source
    return out, "ok", default_source


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def _overlaps(ev: CalendarEvent, start: datetime, end: datetime) -> bool:
    if ev.start >= end:
        return False
    if ev.end > ev.start:
        return ev.end > start
    return ev.start >= start  # zero-length event: must start inside the window


def _dedup_key(ev: CalendarEvent) -> tuple[str, str, bool]:
    return (one_line(ev.title).lower(), ev.start.replace(second=0, microsecond=0).isoformat(), ev.all_day)


def _sort_key(ev: CalendarEvent) -> tuple[bool, datetime]:
    return (not ev.all_day, ev.start)


def alert_key(ev: CalendarEvent) -> str:
    """Source-independent key for 'already announced' state. Keyed on the dedup
    identity (title + start + all-day), not the source id, so an event that
    exists in two sources during migration — or flips source when one is
    briefly down — isn't announced twice."""
    raw = "|".join(str(x) for x in _dedup_key(ev))
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def caldav_configured() -> bool:
    return get_caldav_client(http_client=shared._http).is_configured


def google_configured() -> bool:
    return bool(shared.CALENDAR_GOOGLE_ENABLED and get_google_client(http_client=shared._http).is_configured)


def is_configured() -> bool:
    """True when at least one calendar source can answer."""
    return caldav_configured() or google_configured() or phone_sync_expected()


def writable() -> bool:
    """True when create_event has somewhere to write (CalDAV, else Google)."""
    return caldav_configured() or google_configured()


async def _timed(source: str, coro: Any, timeout: float) -> CalendarResponse:
    """Run one source read within its budget. Never raises: any exception
    (a malformed event, a timeout, a bug) marks just this source failed."""
    t0 = time.monotonic()
    result = "error"
    try:
        resp = await asyncio.wait_for(coro, timeout=timeout)
        result = "ok" if resp.success else "error"
    except TimeoutError:
        result = "timeout"
        resp = CalendarResponse(success=False, error=f"{source} timed out")
    except Exception as e:  # noqa: BLE001 — isolate one source from the merge
        logger.debug(f"[CALENDAR] {source} read raised {type(e).__name__}", extra={"component": "calendar"})
        resp = CalendarResponse(success=False, error=f"{source} unreachable")
    _record(source, result, t0)
    return resp


async def get_events(start: datetime, end: datetime, timeout: float = SOURCE_TIMEOUT_SECONDS) -> CalendarResponse:
    """All events overlapping [start, end) from every configured source, merged."""
    tz = _tz()
    collected: list[CalendarEvent] = []
    failed: list[str] = []
    answered = 0

    # Local push first (pure, no I/O) so nothing can raise between task creation and gather.
    phone_events, phone_result, phone_source = _phone_window(start, end, tz)
    if phone_result != "off":
        _record(phone_source, phone_result)
        if phone_result == "ok":
            answered += 1
            collected.extend(phone_events)
        else:
            failed.append(phone_source)

    sources: list[tuple[str, Any]] = []
    if caldav_configured():
        sources.append((SOURCE_CALDAV, get_caldav_client(http_client=shared._http).list_window(start, end)))
    if google_configured():
        sources.append((SOURCE_GOOGLE, get_google_client(http_client=shared._http).list_window(start, end)))
    # _timed never raises; gather(return_exceptions) is belt and braces, and a
    # cancelled caller cancels the children with it (no orphaned tasks).
    results = await asyncio.gather(*(_timed(name, coro, timeout) for name, coro in sources), return_exceptions=True)
    for (source, _), resp in zip(sources, results, strict=True):
        if isinstance(resp, BaseException) or not resp.success:
            failed.append(source)
            continue
        answered += 1
        if resp.failed_sources:  # partial: e.g. one of several CalDAV calendars unreadable
            failed.append(source)
        for ev in resp.events:
            ev.source = source
            if _overlaps(ev, start, end):
                collected.append(ev)

    if answered == 0:
        if not failed:
            return CalendarResponse(success=False, error="No calendar is configured")
        return CalendarResponse(success=False, error="Couldn't reach any calendar", failed_sources=failed)

    best: dict[tuple, CalendarEvent] = {}
    for ev in collected:
        _neutralize_event_text(ev)
        key = _dedup_key(ev)
        cur = best.get(key)
        if cur is None or _PRIORITY.get(ev.source, 9) < _PRIORITY.get(cur.source, 9):
            best[key] = ev
    events = sorted(best.values(), key=_sort_key)
    return CalendarResponse(success=True, events=events, failed_sources=failed)


async def events_on(day: date) -> CalendarResponse:
    """Events overlapping one local calendar day."""
    tz = _tz()
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return await get_events(start, start + timedelta(days=1))


async def upcoming(hours: float) -> CalendarResponse:
    """Events overlapping [now, now + hours)."""
    now = datetime.now(_tz())
    return await get_events(now, now + timedelta(hours=hours))


def describe_failures(failed: list[str]) -> str:
    """'' or e.g. "I couldn't reach your work calendar" — for tool/TTS text."""
    if not failed:
        return ""
    names = sorted({SOURCE_LABELS.get(s, s) for s in failed})
    return f"I couldn't reach your {' or '.join(names)} calendar" + ("s" if len(names) > 1 else "")


def event_label(ev: CalendarEvent) -> str:
    """Short source tag for display: 'work' for Exchange, '' otherwise."""
    return "work" if ev.source == SOURCE_EXCHANGE else ""


def event_to_dict(ev: CalendarEvent) -> dict:
    """Dict shape used by the briefings, wind-down and dashboard."""
    return {
        "id": ev.id,
        "title": ev.title,
        "start": ev.start,
        "end": ev.end,
        "all_day": ev.all_day,
        "location": ev.location,
        "calendar": ev.calendar_name or SOURCE_LABELS.get(ev.source, ev.source),
        "source": ev.source,
    }


def health() -> dict:
    """Per-source configuration + last read outcome for /health."""
    age = time.time() - shared._phone_calendar_sync_time if shared._phone_calendar_sync_time > 0 else None
    return {
        "configured": is_configured(),
        "caldav": {"configured": caldav_configured(), "last": _last_result.get(SOURCE_CALDAV)},
        "google": {"configured": google_configured(), "last": _last_result.get(SOURCE_GOOGLE)},
        "phone_sync": {
            "expected": phone_sync_expected(),
            "source": shared._phone_calendar_source or None,
            "last": _last_result.get(SOURCE_EXCHANGE),
            "age_minutes": int(age / 60) if age is not None else None,
            "event_count": len(shared._phone_calendar_events or []),
        },
    }


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def create_event(
    title: str,
    start_time: str,
    duration_minutes: int = 60,
    description: str = "",
    location: str = "",
) -> CalendarResponse:
    """Create an event on the personal calendar (CalDAV), or Google when CalDAV
    is off. The work (Exchange) calendar is never written."""
    tz = _tz()
    try:
        start = datetime.fromisoformat(start_time)
    except (TypeError, ValueError):
        return CalendarResponse(success=False, error="Invalid start_time — use ISO 8601 like 2026-02-21T19:00:00")
    start = start.replace(tzinfo=tz) if start.tzinfo is None else start
    now = datetime.now(tz)
    if not (now - timedelta(days=366) <= start <= now + timedelta(days=5 * 366)):
        return CalendarResponse(success=False, error="start_time must be within the next 5 years")
    try:
        duration = int(duration_minutes)
    except (TypeError, ValueError):
        duration = 60
    duration = min(max(duration, 1), 7 * 24 * 60)
    end = start + timedelta(minutes=duration)
    title = one_line(title or "")[:MAX_TITLE]
    location = one_line(location or "")[:MAX_LOCATION]
    description = (description or "").strip()[:MAX_DESCRIPTION]

    if caldav_configured():
        resp = await get_caldav_client(http_client=shared._http).create_event(
            title, start, end, description=description, location=location
        )
        CALENDAR_WRITE.labels(target=SOURCE_CALDAV, result="ok" if resp.success else "error").inc()
        return resp
    if google_configured():
        resp = await get_google_client(http_client=shared._http).create_event(
            title=title,
            start_time=start.isoformat(),
            duration_minutes=duration,
            description=description,
            location=location,
        )
        CALENDAR_WRITE.labels(target=SOURCE_GOOGLE, result="ok" if resp.success else "error").inc()
        return resp
    CALENDAR_WRITE.labels(target="none", result="unconfigured").inc()
    return CalendarResponse(success=False, error="No writable calendar is configured")


# ---------------------------------------------------------------------------
# Facade with the old Google-client surface
# ---------------------------------------------------------------------------


class CalendarFacade:
    """Drop-in for the old GoogleCalendarClient call shapes, backed by all sources."""

    @property
    def is_configured(self) -> bool:
        return is_configured()

    async def list_events(self, days_ahead: int = 7, calendar_id: Optional[str] = None) -> CalendarResponse:
        now = datetime.now(_tz())
        return await get_events(now, now + timedelta(days=days_ahead))

    async def get_upcoming(self, hours_ahead: int = 2) -> CalendarResponse:
        return await upcoming(hours_ahead)

    async def create_event(
        self,
        title: str,
        start_time: str,
        duration_minutes: int = 60,
        description: str = "",
        location: str = "",
        calendar_id: Optional[str] = None,
    ) -> CalendarResponse:
        return await create_event(title, start_time, duration_minutes, description, location)


_facade = CalendarFacade()


def get_calendar(http_client: Optional[httpx.AsyncClient] = None) -> CalendarFacade:
    """The merged calendar. ``http_client`` is accepted for call-site compatibility."""
    if http_client is not None:
        get_caldav_client(http_client=http_client)
        get_google_client(http_client=http_client)
    return _facade
