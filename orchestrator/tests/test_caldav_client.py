"""Tests for orchestrator.caldav_calendar — the self-hosted CalDAV client.

ICS parsing/writing is pure; the HTTP paths are mocked with respx against the
same multistatus shapes Radicale 3.8.3 returns (verified live 2026-10-14).
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from orchestrator import caldav_calendar
from orchestrator.caldav_calendar import CalDAVCalendarClient, build_vevent_ics, parse_vevents

TZ = ZoneInfo("America/Chicago")
BASE = "http://radicale:5232"


def _vcal(*vevents: str) -> str:
    body = "".join(f"BEGIN:VEVENT\r\n{v}END:VEVENT\r\n" for v in vevents)
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n{body}END:VCALENDAR\r\n"


# ---------------------------------------------------------------------------
# parse_vevents
# ---------------------------------------------------------------------------


def test_parses_expanded_utc_instances_with_recurrence_ids():
    ics = _vcal(
        "UID:rec-1\r\nRECURRENCE-ID:20261012T140000Z\r\nDTSTART:20261012T140000Z\r\n"
        "DTEND:20261012T143000Z\r\nSUMMARY:standup\r\n",
        "UID:rec-1\r\nRECURRENCE-ID:20261014T140000Z\r\nDTSTART:20261014T160000Z\r\n"
        "DTEND:20261014T163000Z\r\nSUMMARY:standup moved\r\n",
    )
    events, unexpanded = parse_vevents(ics, TZ, calendar_name="Personal")
    assert unexpanded == 0
    assert [e.title for e in events] == ["standup", "standup moved"]
    assert events[0].start == datetime(2026, 10, 12, 9, 0, tzinfo=TZ)
    assert events[1].start.hour == 11  # the moved instance, in local time
    assert events[0].id != events[1].id  # UID + RECURRENCE-ID keeps instances distinct
    assert events[0].id.startswith("caldav:rec-1:")
    assert events[0].source == "caldav" and events[0].calendar_name == "Personal"


def test_all_day_date_value():
    events, _ = parse_vevents(
        _vcal("UID:a\r\nDTSTART;VALUE=DATE:20261016\r\nDTEND;VALUE=DATE:20261017\r\nSUMMARY:allday\r\n"), TZ
    )
    assert events[0].all_day is True
    assert events[0].start == datetime(2026, 10, 16, tzinfo=TZ)
    assert events[0].end - events[0].start == timedelta(days=1)


def test_tzid_and_floating_times():
    events, _ = parse_vevents(
        _vcal(
            "UID:t\r\nDTSTART;TZID=America/New_York:20261020T100000\r\nDURATION:PT45M\r\nSUMMARY:east\r\n",
            "UID:f\r\nDTSTART:20261020T100000\r\nSUMMARY:floating\r\n",
            "UID:w\r\nDTSTART;TZID=Not/AZone:20261020T100000\r\nSUMMARY:bad tz\r\n",
        ),
        TZ,
    )
    east, floating, bad = events
    assert east.start == datetime(2026, 10, 20, 9, 0, tzinfo=TZ)  # 10:00 ET = 09:00 CT
    assert east.end - east.start == timedelta(minutes=45)
    assert floating.start == datetime(2026, 10, 20, 10, 0, tzinfo=TZ)  # floating = local
    assert floating.end == floating.start
    assert bad.start == datetime(2026, 10, 20, 10, 0, tzinfo=TZ)  # unknown TZID treated as local


def test_unfolding_and_text_unescaping():
    ics = _vcal(
        "UID:u\r\nDTSTART:20261020T150000Z\r\nSUMMARY:Lunch\\, then a very long title that the server\r\n"
        "  folded onto a second line\r\nLOCATION:Room 1\\; Building B\\nSecond line\r\n"
    )
    ev = parse_vevents(ics, TZ)[0][0]
    assert ev.title == "Lunch, then a very long title that the server folded onto a second line"
    assert ev.location == "Room 1; Building B Second line"  # newlines collapsed on read


def test_skips_cancelled_missing_dtstart_and_nested_valarm_props():
    ics = _vcal(
        "UID:c\r\nDTSTART:20261020T150000Z\r\nSTATUS:CANCELLED\r\nSUMMARY:gone\r\n",
        "UID:n\r\nSUMMARY:no start\r\n",
        "UID:v\r\nDTSTART:20261020T150000Z\r\nSUMMARY:keep\r\n"
        "BEGIN:VALARM\r\nSUMMARY:alarm text\r\nTRIGGER:-PT10M\r\nEND:VALARM\r\n",
        "UID:m\r\nDTSTART:garbage\r\nSUMMARY:malformed\r\n",
    )
    events, _ = parse_vevents(ics, TZ)
    assert [e.title for e in events] == ["keep"]


def test_directory_tzid_and_overflowing_duration_are_skipped_not_raised():
    events, _ = parse_vevents(
        _vcal(
            "UID:d\r\nDTSTART;TZID=America:20261020T100000\r\nSUMMARY:dir tz\r\n",
            "UID:o\r\nDTSTART:20261020T150000Z\r\nDURATION:P99999999W\r\nSUMMARY:overflow\r\n",
        ),
        TZ,
    )
    assert [e.title for e in events] == ["dir tz"]
    assert events[0].start == datetime(2026, 10, 20, 10, 0, tzinfo=TZ)


def test_counts_unexpanded_rrules():
    events, unexpanded = parse_vevents(
        _vcal("UID:r\r\nDTSTART:20261020T150000Z\r\nRRULE:FREQ=DAILY\r\nSUMMARY:daily\r\n"), TZ
    )
    assert unexpanded == 1 and len(events) == 1


# ---------------------------------------------------------------------------
# build_vevent_ics
# ---------------------------------------------------------------------------


def test_build_escapes_crlf_so_no_property_injection():
    start = datetime(2026, 10, 20, 15, 0, tzinfo=TZ)
    ics = build_vevent_ics(
        "x@jess", "Dentist\r\nATTENDEE:mailto:evil@example.com", start, start + timedelta(hours=1), location="A;B,C"
    )
    lines = ics.split("\r\n")
    assert not any(line.startswith("ATTENDEE") for line in lines)
    assert "SUMMARY:Dentist\\nATTENDEE:mailto:evil@example.com" in ics
    assert "LOCATION:A\\;B\\,C" in ics
    assert "DTSTART:20261020T200000Z" in ics  # UTC, no VTIMEZONE needed
    # Round-trips through our own parser.
    ev = parse_vevents(ics, TZ)[0][0]
    assert ev.title == "Dentist ATTENDEE:mailto:evil@example.com"  # one line on read
    assert ev.start == start


def test_build_drops_other_line_separators_and_controls():
    start = datetime(2026, 10, 20, 15, 0, tzinfo=TZ)
    ics = build_vevent_ics("x@jess", "a\u2028X-EVIL:1\u2029Y\x0bZ\x00", start, start)
    assert "\u2028" not in ics and "\u2029" not in ics and "\x0b" not in ics and "\x00" not in ics
    assert [ln for ln in ics.splitlines() if ln.startswith("X-EVIL")] == []


def test_build_folds_long_lines_at_75_octets_utf8_safe():
    start = datetime(2026, 10, 20, 15, 0, tzinfo=TZ)
    title = "Café ☕ " * 20
    ics = build_vevent_ics("x@jess", title, start, start)
    for line in ics.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
    assert parse_vevents(ics, TZ)[0][0].title == title.strip()[: caldav_calendar.MAX_TITLE].strip()


# ---------------------------------------------------------------------------
# Client (respx)
# ---------------------------------------------------------------------------


PROPFIND_207 = """<?xml version="1.0"?>
<multistatus xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
 <response><href>/owner/</href><propstat><prop><resourcetype><collection/></resourcetype></prop></propstat></response>
 <response><href>/owner/personal/</href><propstat><prop>
   <resourcetype><collection/><C:calendar/></resourcetype><displayname>Personal</displayname></prop></propstat></response>
 <response><href>/owner/family/</href><propstat><prop>
   <resourcetype><collection/><C:calendar/></resourcetype><displayname>Family</displayname></prop></propstat></response>
 <response><href>/owner/contacts/</href><propstat><prop>
   <resourcetype><collection/></resourcetype><displayname>Contacts</displayname></prop></propstat></response>
 <response><href>/someone-else/cal/</href><propstat><prop>
   <resourcetype><collection/><C:calendar/></resourcetype></prop></propstat></response>
</multistatus>"""


def _report_207(*vevents: str) -> str:
    data = _vcal(*vevents).replace("\r", "&#13;")
    return (
        '<?xml version="1.0"?><multistatus xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        f"<response><href>/owner/x/e.ics</href><propstat><prop><C:calendar-data>{data}</C:calendar-data>"
        "</prop><status>HTTP/1.1 200 OK</status></propstat></response></multistatus>"
    )


@pytest.fixture
def client(monkeypatch):
    from orchestrator import shared

    for k, v in {
        "CALDAV_ENABLED": True,
        "CALDAV_URL": BASE,
        "CALDAV_USERNAME": "jess",
        "CALDAV_PASSWORD": "pw",
        "CALDAV_CALENDAR_HOME": "/owner/",
        "CALDAV_CALENDARS": "",
        "CALDAV_DEFAULT_CALENDAR": "",
        "TIMEZONE": "America/Chicago",
    }.items():
        monkeypatch.setattr(shared, k, v, raising=False)
    return CalDAVCalendarClient()


def test_unconfigured_without_password(monkeypatch, client):
    from orchestrator import shared

    monkeypatch.setattr(shared, "CALDAV_PASSWORD", "")
    assert CalDAVCalendarClient().is_configured is False
    assert client.is_configured is True


@respx.mock
async def test_list_window_discovers_only_own_calendars_and_merges(client):
    respx.request("PROPFIND", f"{BASE}/owner/").mock(return_value=httpx.Response(207, text=PROPFIND_207))
    personal = respx.request("REPORT", f"{BASE}/owner/personal/").mock(
        return_value=httpx.Response(207, text=_report_207("UID:a\r\nDTSTART:20261020T150000Z\r\nSUMMARY:mine\r\n"))
    )
    family = respx.request("REPORT", f"{BASE}/owner/family/").mock(
        return_value=httpx.Response(207, text=_report_207("UID:b\r\nDTSTART:20261020T170000Z\r\nSUMMARY:fam\r\n"))
    )
    start = datetime(2026, 10, 20, tzinfo=TZ)
    resp = await client.list_window(start, start + timedelta(days=1))
    assert resp.success
    assert sorted(e.title for e in resp.events) == ["fam", "mine"]
    assert personal.called and family.called
    body = personal.calls[0].request.content.decode()
    assert "<C:expand" in body and 'start="20261020T050000Z"' in body
    # Never queried a calendar outside the configured home.
    assert all("someone-else" not in str(c.request.url) for c in respx.calls)
    assert all(c.request.headers["Authorization"].startswith("Basic ") for c in respx.calls)


@respx.mock
async def test_calendars_filter(monkeypatch, client):
    from orchestrator import shared

    monkeypatch.setattr(shared, "CALDAV_CALENDARS", "Family")
    respx.request("PROPFIND", f"{BASE}/owner/").mock(return_value=httpx.Response(207, text=PROPFIND_207))
    cals = await CalDAVCalendarClient().calendars()
    assert cals == [("/owner/family/", "Family")]


@respx.mock
async def test_all_reports_failing_is_failure(client):
    respx.request("PROPFIND", f"{BASE}/owner/").mock(return_value=httpx.Response(207, text=PROPFIND_207))
    respx.request("REPORT").mock(return_value=httpx.Response(500))
    start = datetime(2026, 10, 20, tzinfo=TZ)
    resp = await client.list_window(start, start + timedelta(days=1))
    assert resp.success is False


@respx.mock
async def test_unreachable_server_is_failure_not_exception(client):
    respx.request("PROPFIND", f"{BASE}/owner/").mock(side_effect=httpx.ConnectError("refused"))
    start = datetime(2026, 10, 20, tzinfo=TZ)
    resp = await client.list_window(start, start + timedelta(days=1))
    assert resp.success is False and resp.error == "CalDAV server unreachable"


@respx.mock
async def test_create_event_puts_new_ics_with_if_none_match(monkeypatch, client):
    from orchestrator import shared

    monkeypatch.setattr(shared, "CALDAV_DEFAULT_CALENDAR", "personal")
    respx.request("PROPFIND", f"{BASE}/owner/").mock(return_value=httpx.Response(207, text=PROPFIND_207))
    put = respx.put(url__regex=rf"{BASE}/owner/personal/[0-9a-f-]+\.ics").mock(return_value=httpx.Response(201))
    c = CalDAVCalendarClient()
    start = datetime(2026, 10, 20, 15, 0, tzinfo=TZ)
    resp = await c.create_event("Dentist", start, start + timedelta(hours=1), location="Main St")
    assert resp.success and resp.events[0].title == "Dentist"
    req = put.calls[0].request
    assert req.headers["If-None-Match"] == "*"
    assert req.headers["Content-Type"].startswith("text/calendar")
    assert b"SUMMARY:Dentist" in req.content


@respx.mock
async def test_create_event_refuses_unknown_default_calendar(monkeypatch, client):
    from orchestrator import shared

    monkeypatch.setattr(shared, "CALDAV_DEFAULT_CALENDAR", "nope")
    respx.request("PROPFIND", f"{BASE}/owner/").mock(return_value=httpx.Response(207, text=PROPFIND_207))
    start = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    resp = await CalDAVCalendarClient().create_event("x", start, start)
    assert resp.success is False and "CALDAV_DEFAULT_CALENDAR" in resp.error


@respx.mock
async def test_create_event_server_refusal(monkeypatch, client):
    respx.request("PROPFIND", f"{BASE}/owner/").mock(return_value=httpx.Response(207, text=PROPFIND_207))
    respx.put(url__regex=rf"{BASE}/owner/.*").mock(return_value=httpx.Response(403))
    start = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    resp = await client.create_event("x", start, start)
    assert resp.success is False and "403" in resp.error
