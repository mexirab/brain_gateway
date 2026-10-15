"""Tests for the callers of calendar_sources: the poller, email→calendar,
the check/create calendar tools, the dashboard /api/calendar/today route, the
CalDAV config validator and calendar_sources.health().

Network clients are faked; all event data is generic.
"""

import logging
import os
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator import calendar_sources, jobs_calendar, routes_calendar, shared, tool_handlers
from orchestrator.calendar_types import CalendarEvent, CalendarResponse
from orchestrator.tests.calendar_fakes import fake_client, patched_calendar

TZ_NAME = "America/Chicago"
TZ = ZoneInfo(TZ_NAME)


@pytest.fixture(autouse=True)
def _tz(monkeypatch):
    monkeypatch.setattr(shared, "TIMEZONE", TZ_NAME, raising=False)


def _ev(title, start, source, *, location="", all_day=False, minutes=60, ev_id=None):
    end = start + (timedelta(days=1) if all_day else timedelta(minutes=minutes))
    return CalendarEvent(
        id=ev_id or f"{source}-uid-{title}",
        title=title,
        start=start,
        end=end,
        location=location,
        all_day=all_day,
        source=source,
    )


# ---------------------------------------------------------------------------
# jobs_calendar.poll_calendar
# ---------------------------------------------------------------------------


class _Poll:
    """Harness: a fake merged calendar + in-memory notified store + fake TTS."""

    def __init__(self, monkeypatch, *, tiered=False, maps_key=""):
        self.notified: set[str] = set()
        self.facade = MagicMock(is_configured=True)
        self.facade.get_upcoming = AsyncMock()
        self.voice = AsyncMock(return_value={"success": True})
        self.travel = AsyncMock(return_value=None)
        monkeypatch.setattr(jobs_calendar, "get_calendar", lambda *a, **k: self.facade)
        monkeypatch.setattr(jobs_calendar, "_announce_voice", self.voice)
        monkeypatch.setattr(jobs_calendar, "get_travel_time", self.travel)
        monkeypatch.setattr(jobs_calendar.state_store, "is_notified", lambda k: k in self.notified)
        monkeypatch.setattr(jobs_calendar.state_store, "mark_notified", lambda k: self.notified.add(k))
        monkeypatch.setattr(shared, "CALENDAR_TIERED_ALERTS", tiered, raising=False)
        monkeypatch.setattr(shared, "CALENDAR_ALERT_TIERS", [60, 30, 15, 5], raising=False)
        monkeypatch.setattr(shared, "GOOGLE_MAPS_API_KEY", maps_key, raising=False)
        monkeypatch.setattr(shared, "HOME_ADDRESS", "1 Example St", raising=False)
        monkeypatch.setattr(shared, "TRAVEL_TIME_BUFFER", 10, raising=False)
        monkeypatch.setattr(shared, "current_focus_session", {"active": False}, raising=False)

    def serve(self, *events):
        self.facade.get_upcoming.return_value = CalendarResponse(success=True, events=list(events))

    async def poll(self, *events):
        self.serve(*events)
        await jobs_calendar.poll_calendar()


def _soon(minutes=40):
    return datetime.now(TZ).replace(second=0, microsecond=0) + timedelta(minutes=minutes)


@pytest.mark.parametrize("tiered", [False, True])
async def test_poll_does_not_reannounce_same_event_from_another_source(monkeypatch, tiered):
    h = _Poll(monkeypatch, tiered=tiered)
    start = _soon(40)
    await h.poll(_ev("Team sync", start, "google", ev_id="google-abc"))
    assert h.voice.await_count == 1
    # Same event, now served by CalDAV with a different id (migration / source flip).
    await h.poll(_ev("Team sync", start, "caldav", ev_id="caldav-xyz"))
    assert h.voice.await_count == 1


async def test_poll_announces_distinct_events_separately(monkeypatch):
    h = _Poll(monkeypatch)
    start = _soon(40)
    await h.poll(_ev("Team sync", start, "google"), _ev("Other thing", start + timedelta(minutes=5), "caldav"))
    assert h.voice.await_count == 2


async def test_poll_failed_tts_is_retried_next_poll(monkeypatch):
    h = _Poll(monkeypatch)
    h.voice.return_value = {"success": False, "error": "speaker down"}
    ev = _ev("Team sync", _soon(40), "caldav")
    await h.poll(ev)
    h.voice.return_value = {"success": True}
    await h.poll(ev)
    assert h.voice.await_count == 2
    assert h.notified  # marked only after the successful announce


async def test_poll_exchange_location_never_sent_to_maps(monkeypatch):
    h = _Poll(monkeypatch, maps_key="test-key")
    await h.poll(_ev("Work meeting", _soon(40), "exchange", location="Conference Room 4"))
    h.travel.assert_not_awaited()
    assert h.voice.await_count == 1  # still gets the normal countdown announcement


async def test_poll_personal_location_does_use_travel_time(monkeypatch):
    h = _Poll(monkeypatch, maps_key="test-key")
    await h.poll(_ev("Appointment", _soon(40), "caldav", location="2 Example Ave"))
    h.travel.assert_awaited_once()


async def test_poll_leave_now_keyed_on_alert_key(monkeypatch):
    """A leave-now alert for the google copy suppresses the caldav copy's alert."""
    h = _Poll(monkeypatch, maps_key="test-key")
    h.travel.return_value = MagicMock(duration_in_traffic_minutes=45)
    start = _soon(40)
    await h.poll(_ev("Appointment", start, "google", location="2 Example Ave"))
    assert h.voice.await_count == 1
    assert "leave now" in h.voice.await_args.args[0]
    await h.poll(_ev("Appointment", start, "caldav", location="2 Example Ave", ev_id="other"))
    # No second leave-now and no second Maps lookup for the caldav copy. (The
    # poller does fall through to the normal countdown once travel is marked —
    # pre-existing behaviour, not asserted here.)
    assert h.travel.await_count == 1
    assert sum("leave now" in c.args[0] for c in h.voice.await_args_list) == 1


async def test_poll_logs_opaque_refs_not_titles_or_ids(monkeypatch, caplog):
    h = _Poll(monkeypatch)
    ev = _ev("Generic title", _soon(40), "caldav", ev_id="raw-uid-123")
    with caplog.at_level(logging.DEBUG, logger="orchestrator.jobs_calendar"):
        await h.poll(ev)
    text = "\n".join(r.getMessage() for r in caplog.records if r.name == "orchestrator.jobs_calendar")
    assert calendar_sources.log_ref(calendar_sources.alert_key(ev)) in text
    assert "Generic title" not in text
    assert "raw-uid-123" not in text


async def test_poll_skips_all_day_and_out_of_window(monkeypatch):
    h = _Poll(monkeypatch)
    now = datetime.now(TZ)
    await h.poll(
        _ev("All day", now.replace(hour=0, minute=0), "caldav", all_day=True),
        _ev("Past", now - timedelta(minutes=30), "caldav"),
        _ev("Far", now + timedelta(minutes=200), "caldav"),
    )
    h.voice.assert_not_awaited()


async def test_poll_failed_read_announces_nothing(monkeypatch):
    h = _Poll(monkeypatch)
    h.facade.get_upcoming.return_value = CalendarResponse(success=False, error="down")
    await jobs_calendar.poll_calendar()
    h.voice.assert_not_awaited()


# ---------------------------------------------------------------------------
# jobs_calendar.process_emails_for_events
# ---------------------------------------------------------------------------


def _gmail():
    g = MagicMock(is_configured=True)
    g.list_messages = AsyncMock(return_value=MagicMock(success=True, messages=[]))
    return g


async def test_email_to_calendar_skips_when_nothing_writable(monkeypatch):
    g = _gmail()
    monkeypatch.setattr(jobs_calendar, "get_gmail_client", lambda *a, **k: g)
    monkeypatch.setattr(calendar_sources, "writable", lambda: False)
    await jobs_calendar.process_emails_for_events()
    g.list_messages.assert_not_awaited()


async def test_email_to_calendar_scans_when_writable(monkeypatch):
    g = _gmail()
    monkeypatch.setattr(jobs_calendar, "get_gmail_client", lambda *a, **k: g)
    monkeypatch.setattr(calendar_sources, "writable", lambda: True)
    await jobs_calendar.process_emails_for_events()
    g.list_messages.assert_awaited_once()


def test_writable_tracks_caldav_or_google():
    with patched_calendar(caldav=fake_client(configured=True)):
        assert calendar_sources.writable() is True
    with patched_calendar(google=fake_client(configured=True)):
        assert calendar_sources.writable() is True
    with patched_calendar(google=fake_client(configured=True), google_enabled=False):
        assert calendar_sources.writable() is False
    with patched_calendar():
        assert calendar_sources.writable() is False


# ---------------------------------------------------------------------------
# config.Settings.validate_caldav_config
# ---------------------------------------------------------------------------


def _settings(**kw):
    from orchestrator.config import Settings

    clean = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CALDAV_", "PHONE_CALENDAR_")) and k not in ("caldav_url", "caldav_enabled")
    }
    with patch.dict(os.environ, clean, clear=True):
        return Settings(_env_file=None, **kw)


def test_caldav_full_config_stays_enabled(caplog):
    with caplog.at_level(logging.ERROR, logger="orchestrator.config"):
        s = _settings(caldav_url="https://cal.example.test/", caldav_username="u", caldav_password="p")
    assert s.caldav_enabled is True
    assert not [r for r in caplog.records if "CALDAV" in r.getMessage()]


@pytest.mark.parametrize(
    "kw",
    [
        {"caldav_url": "https://cal.example.test/"},
        {"caldav_username": "u", "caldav_password": "p"},
        {"caldav_url": "https://cal.example.test/", "caldav_username": "u"},
    ],
)
def test_caldav_partial_config_disables(caplog, kw):
    with caplog.at_level(logging.ERROR, logger="orchestrator.config"):
        s = _settings(**kw)
    assert s.caldav_enabled is False
    assert any("CALDAV_URL, CALDAV_USERNAME and CALDAV_PASSWORD" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("url", ["ftp://cal.example.test/", "cal.example.test", "file:///etc/passwd"])
def test_caldav_non_http_url_disables(caplog, url):
    with caplog.at_level(logging.ERROR, logger="orchestrator.config"):
        s = _settings(caldav_url=url, caldav_username="u", caldav_password="p")
    assert s.caldav_enabled is False
    assert any("must start with http" in r.getMessage() for r in caplog.records)


def test_caldav_nothing_set_is_silent(caplog):
    with caplog.at_level(logging.ERROR, logger="orchestrator.config"):
        s = _settings()
    assert s.caldav_enabled is True  # default; client is_configured is what gates use
    assert not [r for r in caplog.records if "CALDAV" in r.getMessage()]


def test_caldav_explicitly_disabled_partial_is_silent(caplog):
    with caplog.at_level(logging.ERROR, logger="orchestrator.config"):
        s = _settings(caldav_enabled=False, caldav_url="https://cal.example.test/")
    assert s.caldav_enabled is False
    assert not [r for r in caplog.records if "CALDAV" in r.getMessage()]


@pytest.mark.parametrize("hours,expected", [(0, 26.0), (-5, 26.0), (12, 12.0)])
def test_phone_calendar_max_age_clamps(hours, expected):
    assert _settings(phone_calendar_max_age_hours=hours).phone_calendar_max_age_hours == expected


# ---------------------------------------------------------------------------
# tool_handlers.tool_check_calendar / tool_create_calendar_event
# ---------------------------------------------------------------------------


def _patch_get_events(resp):
    return patch.object(calendar_sources, "get_events", new_callable=AsyncMock, return_value=resp)


async def test_check_calendar_tags_work_events_only():
    start = datetime(2026, 10, 20, 9, 0, tzinfo=TZ)
    resp = CalendarResponse(
        success=True,
        events=[_ev("Work meeting", start, "exchange"), _ev("Gym", start + timedelta(hours=8), "caldav")],
    )
    with _patch_get_events(resp):
        out = await tool_handlers.tool_check_calendar(days_ahead=7)
    lines = out.splitlines()
    work = next(ln for ln in lines if "Work meeting" in ln)
    gym = next(ln for ln in lines if "Gym" in ln)
    assert work.endswith("[work]")
    assert "[" not in gym
    assert "(2 events)" in out
    assert "Note:" not in out


async def test_check_calendar_partial_failure_note():
    start = datetime(2026, 10, 20, 9, 0, tzinfo=TZ)
    resp = CalendarResponse(success=True, events=[_ev("Gym", start, "caldav")], failed_sources=["exchange"])
    with _patch_get_events(resp):
        out = await tool_handlers.tool_check_calendar(days_ahead=3)
    assert "Gym" in out
    assert out.rstrip().endswith("(Note: I couldn't reach your work calendar, so this may be incomplete.)")


async def test_check_calendar_partial_failure_note_on_empty_day():
    resp = CalendarResponse(success=True, events=[], failed_sources=["caldav"])
    with _patch_get_events(resp):
        out = await tool_handlers.tool_check_calendar(days_ahead=1)
    assert out.startswith("No events on the calendar for today.")
    assert "couldn't reach your personal calendar" in out


async def test_check_calendar_all_failed_names_sources():
    resp = CalendarResponse(success=False, error="Couldn't reach any calendar", failed_sources=["caldav", "exchange"])
    with _patch_get_events(resp):
        out = await tool_handlers.tool_check_calendar()
    assert out == "Calendar error: I couldn't reach your personal or work calendars."


async def test_check_calendar_nothing_connected():
    resp = CalendarResponse(success=False, error="No calendar is configured")
    with _patch_get_events(resp):
        out = await tool_handlers.tool_check_calendar()
    assert out == "Calendar is not available — no calendar is connected yet."


@pytest.mark.parametrize("arg,days", [(0, 1), (-3, 1), (500, 60), ("abc", 7), (None, 7), ("14", 14)])
async def test_check_calendar_clamps_days_ahead(arg, days):
    with _patch_get_events(CalendarResponse(success=True, events=[])) as ge:
        out = await tool_handlers.tool_check_calendar(days_ahead=arg)
    start, end = ge.await_args.args
    assert end - start == timedelta(days=days)
    assert start.hour == 0 and start.minute == 0
    expected = "today" if days == 1 else f"next {days} days"
    assert expected in out


async def test_check_calendar_event_formats():
    start = datetime(2026, 10, 20, 9, 0, tzinfo=TZ)
    resp = CalendarResponse(
        success=True,
        events=[
            _ev("Holiday", start.replace(hour=0), "caldav", all_day=True),
            _ev("Call", start, "caldav", location="Room 1"),
            _ev("Instant", start, "caldav", minutes=0),
        ],
    )
    with _patch_get_events(resp):
        out = await tool_handlers.tool_check_calendar(days_ahead=2)
    assert "- Holiday (all day, Tuesday Oct 20)" in out
    assert "- Call — Tuesday Oct 20, 09:00 AM to 10:00 AM" in out
    assert "  Location: Room 1" in out
    assert "- Instant — Tuesday Oct 20, 09:00 AM\n" in out + "\n"


async def test_create_calendar_event_success_text():
    start = datetime(2026, 10, 20, 19, 0, tzinfo=TZ)
    ev = _ev("Dinner", start, "caldav")
    with patch.object(
        calendar_sources,
        "create_event",
        new_callable=AsyncMock,
        return_value=CalendarResponse(success=True, events=[ev]),
    ) as ce:
        out = await tool_handlers.tool_create_calendar_event("Dinner", "2026-10-20T19:00:00", location="Cafe")
    assert out == "Created event: Dinner on Tuesday Oct 20, 07:00 PM at Cafe"
    assert ce.await_args.kwargs["title"] == "Dinner"


async def test_create_calendar_event_failure_text():
    with patch.object(
        calendar_sources,
        "create_event",
        new_callable=AsyncMock,
        return_value=CalendarResponse(success=False, error="No writable calendar is configured"),
    ):
        out = await tool_handlers.tool_create_calendar_event("Dinner", "2026-10-20T19:00:00")
    assert out == "Failed to create event: No writable calendar is configured"


@pytest.mark.parametrize(
    "title,start,expect", [("", "2026-10-20T19:00:00", "Missing event title."), ("x", "", "Missing event start")]
)
async def test_create_calendar_event_missing_fields(title, start, expect):
    with patch.object(calendar_sources, "create_event", new_callable=AsyncMock) as ce:
        out = await tool_handlers.tool_create_calendar_event(title, start)
    assert out.startswith(expect)
    ce.assert_not_awaited()


# ---------------------------------------------------------------------------
# routes_calendar.calendar_today
# ---------------------------------------------------------------------------


@pytest.fixture
def api():
    app = FastAPI()
    app.include_router(routes_calendar.router)
    return TestClient(app)


def _patch_events_on(resp):
    return patch.object(calendar_sources, "events_on", new_callable=AsyncMock, return_value=resp)


def test_today_merged_shape_and_work_label(api):
    start = datetime(2026, 10, 20, 9, 0, tzinfo=TZ)
    work = _ev("Work meeting", start, "exchange")
    work.calendar_name = "Calendar"  # whatever iOS called it — the card still says "work"
    personal = _ev("Gym", start + timedelta(hours=8), "caldav", location="")
    personal.calendar_name = ""  # no collection name -> falls back to the source label
    resp = CalendarResponse(success=True, events=[work, personal], failed_sources=["google"])
    with _patch_events_on(resp):
        body = api.get("/api/calendar/today").json()
    assert body["ok"] is True and "success" not in body
    assert body["source"] == "merged" and body["count"] == 2
    assert body["unavailable"] == ["Google"]
    w, p = body["events"]
    assert w["calendar"] == "work" and w["source"] == "exchange"
    assert p["calendar"] == "personal" and p["source"] == "caldav"
    assert p["location"] is None and p["description"] is None
    assert w["start"] == start.isoformat()
    assert set(w) == {"id", "title", "start", "end", "location", "description", "all_day", "calendar", "source"}


def test_today_all_failed_is_none_with_labels(api):
    resp = CalendarResponse(success=False, error="x", failed_sources=["exchange", "caldav", "caldav"])
    with _patch_events_on(resp):
        body = api.get("/api/calendar/today").json()
    assert body == {
        "ok": True,
        "events": [],
        "source": "none",
        "count": 0,
        "unavailable": ["personal", "work"],
        "web_url": body["web_url"],
    }


def test_today_nothing_configured(api):
    with _patch_events_on(CalendarResponse(success=False, error="No calendar is configured")):
        body = api.get("/api/calendar/today").json()
    assert body["source"] == "none" and body["unavailable"] == [] and body["ok"] is True


def test_today_unknown_source_label_passthrough(api):
    with _patch_events_on(CalendarResponse(success=True, events=[], failed_sources=["mystery"])):
        body = api.get("/api/calendar/today").json()
    assert body["unavailable"] == ["mystery"] and body["source"] == "merged" and body["count"] == 0


# ---------------------------------------------------------------------------
# calendar_sources.health()
# ---------------------------------------------------------------------------


async def test_health_after_reads_reports_last_results(monkeypatch):
    monkeypatch.setattr(calendar_sources, "_last_result", {})
    monkeypatch.setattr(shared, "_phone_calendar_source", "exchange", raising=False)
    monkeypatch.setattr(shared, "_phone_calendar_sync_time", time.time() - 600, raising=False)
    monkeypatch.setattr(
        shared, "_phone_calendar_events", [{"title": "Standup", "start": "2026-10-20T09:00:00"}], raising=False
    )
    caldav = fake_client(success=False)
    with patched_calendar(caldav=caldav):
        await calendar_sources.events_on(datetime(2026, 10, 20).date())
        h = calendar_sources.health()
    assert set(h) == {"configured", "caldav", "google", "phone_sync"}
    assert set(h["phone_sync"]) == {"expected", "source", "last", "age_minutes", "event_count"}
    assert h["configured"] is True
    assert h["caldav"]["configured"] is True and h["caldav"]["last"]["result"] == "error"
    assert h["google"] == {"configured": False, "last": None}
    ps = h["phone_sync"]
    assert ps["expected"] is True and ps["source"] == "exchange"
    assert ps["last"]["result"] == "ok" and ps["event_count"] == 1
    assert 9 <= ps["age_minutes"] <= 11


def test_health_nothing_configured(monkeypatch):
    monkeypatch.setattr(calendar_sources, "_last_result", {})
    monkeypatch.setattr(shared, "_phone_calendar_source", "", raising=False)
    monkeypatch.setattr(shared, "_phone_calendar_sync_time", 0.0, raising=False)
    monkeypatch.setattr(shared, "_phone_calendar_events", [], raising=False)
    with patched_calendar():
        h = calendar_sources.health()
    assert h["configured"] is False
    assert h["phone_sync"] == {"expected": False, "source": None, "last": None, "age_minutes": None, "event_count": 0}
