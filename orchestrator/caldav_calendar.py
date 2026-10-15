"""
CalDAV calendar client — the self-hosted personal calendar (Radicale on Jupiter).

Reads use a CalDAV ``calendar-query`` REPORT with ``<C:expand>``, so the server
expands recurring events (including moved/overridden instances) into plain
UTC instances and this module never has to evaluate RRULEs. Verified against
Radicale 3.8.3 on 2026-10-14. Writes PUT a fresh ``.ics`` (UTC times, so no
VTIMEZONE block) with ``If-None-Match: *`` so an event can never overwrite
another.

Deliberately dependency-free: a small RFC 5545 reader/writer (line unfolding,
text escaping, DATE/DATE-TIME/TZID handling) instead of ``icalendar``. The
expanded server output is simple and trusted (our own Radicale), and keeping
the image unchanged keeps this a code-only change.

Never logs event titles, locations, descriptions or hrefs — counts, status
codes and durations only (see test_no_user_content_in_logs.py).

Usage:
    client = get_caldav_client(http_client=_http)
    resp = await client.list_window(start, end)
"""

from __future__ import annotations

import logging
import re
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from typing import Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from orchestrator import shared
from orchestrator.calendar_types import SOURCE_CALDAV, CalendarEvent, CalendarResponse

logger = logging.getLogger(__name__)

_DAV = "{DAV:}"
_CALDAV = "{urn:ietf:params:xml:ns:caldav}"
_TIMEOUT = 10.0
_DISCOVERY_TTL = 600.0  # seconds to cache the calendar list

# Caps on what Jess writes into a calendar (also guards the tool path).
MAX_TITLE = 200
MAX_LOCATION = 300
MAX_DESCRIPTION = 2000

_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<D:propfind xmlns:D="DAV:"><D:prop><D:resourcetype/><D:displayname/></D:prop></D:propfind>'
)


def _report_body(start: datetime, end: datetime) -> str:
    s = start.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    e = end.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        f'<D:prop><C:calendar-data><C:expand start="{s}" end="{e}"/></C:calendar-data></D:prop>'
        '<C:filter><C:comp-filter name="VCALENDAR"><C:comp-filter name="VEVENT">'
        f'<C:time-range start="{s}" end="{e}"/>'
        "</C:comp-filter></C:comp-filter></C:filter></C:calendar-query>"
    )


# ---------------------------------------------------------------------------
# Minimal RFC 5545 reader
# ---------------------------------------------------------------------------


def _unfold(text: str) -> list[str]:
    """Undo RFC 5545 line folding (CRLF + single space/tab continues a line)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n[ \t]", "", text)
    return [ln for ln in text.split("\n") if ln]


def _split_prop(line: str) -> tuple[str, dict, str]:
    """Split 'NAME;P1=a;P2="b:c":value' into (NAME, {P1: a, P2: b:c}, value)."""
    in_quote = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quote = not in_quote
        elif ch == ":" and not in_quote:
            head, value = line[:i], line[i + 1 :]
            break
    else:
        return line.upper(), {}, ""
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def _unescape(value: str) -> str:
    out = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_dt(value: str, params: dict, tz: ZoneInfo) -> tuple[datetime, bool]:
    """Return (aware datetime, is_all_day) for a DTSTART/DTEND/RECURRENCE-ID value."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        d = datetime.strptime(value[:8], "%Y%m%d")
        return d.replace(tzinfo=tz), True
    if value.endswith("Z"):
        return datetime.strptime(value[:-1], "%Y%m%dT%H%M%S").replace(tzinfo=UTC).astimezone(tz), False
    naive = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    zone = tz
    tzid = params.get("TZID")
    if tzid:
        try:
            zone = ZoneInfo(tzid)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            # unknown/Windows TZID (or one naming a tzdata directory, e.g.
            # "America", which raises IsADirectoryError): treat as local
            zone = tz
    return naive.replace(tzinfo=zone).astimezone(tz), False


_DURATION_RE = re.compile(r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def _parse_duration(value: str) -> Optional[timedelta]:
    m = _DURATION_RE.match(value.strip())
    if not m:
        return None
    sign, w, d, h, mi, s = m.groups()
    delta = timedelta(weeks=int(w or 0), days=int(d or 0), hours=int(h or 0), minutes=int(mi or 0), seconds=int(s or 0))
    return -delta if sign == "-" else delta


def parse_vevents(ics: str, tz: ZoneInfo, calendar_name: str = "") -> tuple[list[CalendarEvent], int]:
    """Parse VEVENTs out of (server-expanded) iCalendar text.

    Returns (events, unexpanded) where ``unexpanded`` counts VEVENTs that still
    carried an RRULE (the server didn't expand — only the first instance is
    used for those). Cancelled instances and events without a DTSTART are
    skipped. Malformed events are skipped, never raised.
    """
    events: list[CalendarEvent] = []
    unexpanded = 0
    props: Optional[dict] = None
    depth = 0  # nesting inside VEVENT (VALARM etc.)
    for line in _unfold(ics):
        name, params, value = _split_prop(line)
        if name == "BEGIN":
            if value.upper() == "VEVENT" and props is None:
                props, depth = {}, 0
            elif props is not None:
                depth += 1
            continue
        if name == "END":
            if props is not None and depth > 0:
                depth -= 1
                continue
            if value.upper() == "VEVENT" and props is not None:
                try:
                    ev, had_rrule = _build_event(props, tz, calendar_name)
                    if had_rrule:
                        unexpanded += 1
                    if ev is not None:
                        events.append(ev)
                except (ValueError, KeyError, OverflowError, TypeError):
                    pass  # malformed event (bad date, absurd DURATION): skip it, never sink the calendar
                props = None
            continue
        if props is not None and depth == 0 and name not in props:
            props[name] = (params, value)
    return events, unexpanded


def _build_event(props: dict, tz: ZoneInfo, calendar_name: str) -> tuple[Optional[CalendarEvent], bool]:
    had_rrule = "RRULE" in props
    if "DTSTART" not in props:
        return None, had_rrule
    if props.get("STATUS", ({}, ""))[1].strip().upper() == "CANCELLED":
        return None, had_rrule
    start, all_day = _parse_dt(props["DTSTART"][1], props["DTSTART"][0], tz)
    if "DTEND" in props:
        end, _ = _parse_dt(props["DTEND"][1], props["DTEND"][0], tz)
    elif "DURATION" in props and _parse_duration(props["DURATION"][1]) is not None:
        end = start + _parse_duration(props["DURATION"][1])
    else:
        end = start + timedelta(days=1) if all_day else start
    uid = props.get("UID", ({}, ""))[1].strip() or uuid.uuid4().hex
    rid = props.get("RECURRENCE-ID", ({}, ""))[1].strip()
    event_id = f"caldav:{uid}" + (f":{rid}" if rid else "")
    return (
        CalendarEvent(
            id=event_id,
            title=" ".join(_unescape(props.get("SUMMARY", ({}, ""))[1]).split()) or "(No title)",
            start=start,
            end=end,
            location=" ".join(_unescape(props.get("LOCATION", ({}, ""))[1]).split()),
            description=_unescape(props.get("DESCRIPTION", ({}, ""))[1]).strip(),
            calendar_name=calendar_name,
            all_day=all_day,
            source=SOURCE_CALDAV,
        ),
        had_rrule,
    )


# ---------------------------------------------------------------------------
# Minimal RFC 5545 writer
# ---------------------------------------------------------------------------


# Line separators other than CR/LF that some parsers (Python str.splitlines,
# for one) treat as line breaks, plus C0/C1 controls. Dropped on write.
_CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x85\u2028\u2029]")


def _escape(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = _CONTROL_RE.sub("", value)
    value = value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return value.replace("\n", "\\n")


def _fold(line: str) -> str:
    """Fold a content line at 75 octets (UTF-8 safe), RFC 5545 §3.1."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    chunks, cur, limit = [], b"", 75
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > limit:
            chunks.append(cur.decode("utf-8"))
            cur, limit = b"", 74  # continuation lines start with a space
        cur += b
    chunks.append(cur.decode("utf-8"))
    return "\r\n ".join(chunks)


def build_vevent_ics(
    uid: str, title: str, start: datetime, end: datetime, location: str = "", description: str = ""
) -> str:
    """Build a single-VEVENT VCALENDAR with UTC times. Inputs are escaped and
    capped; CR/LF can't inject extra properties because every newline becomes
    the literal two characters backslash-n."""

    def utc(dt: datetime) -> str:
        return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Brain Gateway//Jess//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{utc(datetime.now(UTC))}",
        f"DTSTART:{utc(start)}",
        f"DTEND:{utc(end)}",
        f"SUMMARY:{_escape(title[:MAX_TITLE])}",
    ]
    if location:
        lines.append(f"LOCATION:{_escape(location[:MAX_LOCATION])}")
    if description:
        lines.append(f"DESCRIPTION:{_escape(description[:MAX_DESCRIPTION])}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(ln) for ln in lines) + "\r\n"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class CalDAVCalendarClient:
    """Read/write client for the self-hosted CalDAV server."""

    def __init__(self, http_client: Optional[httpx.AsyncClient] = None):
        self._http = http_client
        self.base_url = (shared.CALDAV_URL or "").rstrip("/")
        self.username = shared.CALDAV_USERNAME
        self._password = shared.CALDAV_PASSWORD
        url_path = urlsplit(self.base_url).path.rstrip("/") if self.base_url else ""
        home = shared.CALDAV_CALENDAR_HOME or (f"{url_path}/{self.username}/" if self.username else "")
        self.home = "/" + home.strip("/") + "/" if home else ""
        self.wanted = [c.strip().strip("/") for c in (shared.CALDAV_CALENDARS or "").split(",") if c.strip()]
        self.default_name = (shared.CALDAV_DEFAULT_CALENDAR or "").strip().strip("/")
        self.is_configured = bool(shared.CALDAV_ENABLED and self.base_url and self.username and self._password)
        parts = urlsplit(self.base_url) if self.base_url else None
        self._origin = f"{parts.scheme}://{parts.netloc}" if parts else ""
        self._calendars: list[tuple[str, str]] = []  # (href, display name)
        self._calendars_at = 0.0
        self._warned_unexpanded = False
        self._warned_default = False

    # -- HTTP ----------------------------------------------------------------

    def _url(self, href: str) -> str:
        return self._origin + href if href.startswith("/") else f"{self.base_url}/{href}"

    async def _request(self, method: str, href: str, body: str = "", headers: Optional[dict] = None) -> httpx.Response:
        hdrs = {"Content-Type": "application/xml; charset=utf-8"}
        hdrs.update(headers or {})
        kwargs = dict(
            content=body.encode("utf-8") if body else None,
            headers=hdrs,
            auth=(self.username, self._password),
            timeout=_TIMEOUT,
            follow_redirects=False,
        )
        if self._http:
            return await self._http.request(method, self._url(href), **kwargs)
        async with httpx.AsyncClient() as client:
            return await client.request(method, self._url(href), **kwargs)

    # -- Discovery -----------------------------------------------------------

    async def calendars(self, force: bool = False) -> list[tuple[str, str]]:
        """(href, display name) of the readable calendars under the home, cached."""
        if not force and self._calendars and time.time() - self._calendars_at < _DISCOVERY_TTL:
            return self._calendars
        resp = await self._request("PROPFIND", self.home, _PROPFIND_BODY, {"Depth": "1"})
        if resp.status_code != 207:
            raise httpx.HTTPStatusError(f"PROPFIND {resp.status_code}", request=resp.request, response=resp)
        found = []
        for r in ET.fromstring(resp.content).iter(f"{_DAV}response"):
            href = (r.findtext(f"{_DAV}href") or "").strip()
            # Only calendars directly under our own home — never follow a
            # server-supplied href elsewhere.
            if not href.startswith(self.home) or href.rstrip("/") == self.home.rstrip("/"):
                continue
            rtype = r.find(f".//{_DAV}resourcetype")
            if rtype is None or rtype.find(f"{_CALDAV}calendar") is None:
                continue
            name = (r.findtext(f".//{_DAV}displayname") or "").strip()
            seg = href.rstrip("/").rsplit("/", 1)[-1]
            if self.wanted and seg not in self.wanted and name not in self.wanted:
                continue
            found.append((href if href.endswith("/") else href + "/", name or seg))
        found.sort(key=lambda c: c[1].lower())
        self._calendars, self._calendars_at = found, time.time()
        return found

    async def _write_target(self) -> Optional[tuple[str, str]]:
        cals = await self.calendars()
        if not cals:
            return None
        if self.default_name:
            for href, name in cals:
                if href.rstrip("/").rsplit("/", 1)[-1] == self.default_name or name == self.default_name:
                    return href, name
            return None  # configured default not found: refuse rather than guess
        if len(cals) > 1 and not self._warned_default:
            logger.info(
                f"[CALDAV] {len(cals)} calendars and no CALDAV_DEFAULT_CALENDAR — new events go to the first "
                "(alphabetical). Set CALDAV_DEFAULT_CALENDAR to choose.",
                extra={"component": "calendar"},
            )
            self._warned_default = True
        return cals[0]

    # -- Read ----------------------------------------------------------------

    async def list_window(self, start: datetime, end: datetime) -> CalendarResponse:
        """All event instances overlapping [start, end) across the readable calendars."""
        if not self.is_configured:
            return CalendarResponse(success=False, error="CalDAV calendar not configured")
        tz = ZoneInfo(shared.TIMEZONE)
        try:
            cals = await self.calendars()
            events: list[CalendarEvent] = []
            unexpanded = 0
            failed = 0
            body = _report_body(start, end)
            for href, name in cals:
                resp = await self._request("REPORT", href, body, {"Depth": "1"})
                if resp.status_code != 207:
                    failed += 1
                    if resp.status_code == 404:
                        self._calendars_at = 0.0  # calendar deleted/renamed: rediscover next read
                    logger.debug(
                        f"[CALDAV] REPORT returned {resp.status_code} for one calendar",
                        extra={"component": "calendar"},
                    )
                    continue
                for r in ET.fromstring(resp.content).iter(f"{_DAV}response"):
                    data = r.findtext(f".//{_CALDAV}calendar-data")
                    if not data:
                        continue
                    evs, n = parse_vevents(data, tz, calendar_name=name)
                    events.extend(evs)
                    unexpanded += n
            if unexpanded and not self._warned_unexpanded:
                logger.warning(
                    f"[CALDAV] Server returned {unexpanded} unexpanded recurring event(s) — "
                    "only first instances are visible",
                    extra={"component": "calendar"},
                )
                self._warned_unexpanded = True
            if cals and failed == len(cals):
                return CalendarResponse(success=False, error="CalDAV calendars could not be read")
            # Some (not all) calendars failed: return what we have, but flag it so
            # the merge reports the personal calendar as incomplete.
            partial = [SOURCE_CALDAV] if failed else []
            return CalendarResponse(success=True, events=events, failed_sources=partial)
        except (httpx.HTTPError, ET.ParseError) as e:
            logger.debug(f"[CALDAV] Read failed: {type(e).__name__}", extra={"component": "calendar"})
            return CalendarResponse(success=False, error="CalDAV server unreachable")

    # -- Write ---------------------------------------------------------------

    async def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        description: str = "",
        location: str = "",
    ) -> CalendarResponse:
        if not self.is_configured:
            return CalendarResponse(success=False, error="CalDAV calendar not configured")
        try:
            target = await self._write_target()
            if target is None:
                return CalendarResponse(
                    success=False,
                    error="No writable calendar found (create one, or check CALDAV_DEFAULT_CALENDAR)",
                )
            href, name = target
            uid = f"{uuid.uuid4()}@jess"
            ics = build_vevent_ics(uid, title, start, end, location=location, description=description)
            resp = await self._request(
                "PUT",
                f"{href}{uid.split('@')[0]}.ics",
                ics,
                {"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"},
            )
            if resp.status_code not in (200, 201, 204):
                logger.warning(f"[CALDAV] PUT returned {resp.status_code}", extra={"component": "calendar"})
                return CalendarResponse(success=False, error=f"Calendar server refused the event ({resp.status_code})")
            event = CalendarEvent(
                id=f"caldav:{uid}",
                title=title[:MAX_TITLE],
                start=start,
                end=end,
                location=location[:MAX_LOCATION],
                description=description[:MAX_DESCRIPTION],
                calendar_name=name,
                source=SOURCE_CALDAV,
            )
            return CalendarResponse(success=True, events=[event])
        except (httpx.HTTPError, ET.ParseError) as e:
            logger.warning(f"[CALDAV] Write failed: {type(e).__name__}", extra={"component": "calendar"})
            return CalendarResponse(success=False, error="CalDAV server unreachable")


_client: Optional[CalDAVCalendarClient] = None


def get_caldav_client(http_client: Optional[httpx.AsyncClient] = None) -> CalDAVCalendarClient:
    """Get or create the global CalDAV client."""
    global _client
    if _client is None:
        _client = CalDAVCalendarClient(http_client=http_client)
    elif http_client and _client._http is None:
        _client._http = http_client
    return _client
