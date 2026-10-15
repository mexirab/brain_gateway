"""Tests for GET /api/calendar/range — the dashboard /calendar page's data."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator import routes_calendar, shared
from orchestrator.calendar_types import CalendarResponse
from orchestrator.tests.calendar_fakes import make_event

TZ = ZoneInfo("America/Chicago")


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(shared, "TIMEZONE", "America/Chicago", raising=False)
    monkeypatch.setattr(shared, "CALENDAR_WEB_URL", "https://cal.example.test/.web/", raising=False)
    get_events = AsyncMock(return_value=CalendarResponse(success=True, events=[]))
    monkeypatch.setattr("orchestrator.calendar_sources.get_events", get_events)
    routes_calendar._range_cache.clear()
    app = FastAPI()
    app.include_router(routes_calendar.router)
    client = TestClient(app)
    client.get_events = get_events
    return client


def test_default_is_seven_days_from_local_today(api):
    before = datetime.now(TZ).date()
    body = api.get("/api/calendar/range").json()
    today = datetime.now(TZ).date()
    assert body["start"] in (before.isoformat(), today.isoformat())  # midnight-safe
    today = datetime.fromisoformat(body["start"]).date()
    assert body["ok"] is True and body["days"] == 7
    assert body["timezone"] == "America/Chicago"
    (start, end), _ = api.get_events.call_args
    assert start == datetime(today.year, today.month, today.day, tzinfo=TZ)
    assert end - start == timedelta(days=7)


def test_window_and_merged_shape(api):
    day = datetime.now(TZ).date() + timedelta(days=3)
    work = make_event("Standup", datetime(day.year, day.month, day.day, 9, tzinfo=TZ), source="exchange")
    home = make_event("Dentist", datetime(day.year, day.month, day.day, 15, tzinfo=TZ), source="caldav")
    api.get_events.return_value = CalendarResponse(success=True, events=[work, home], failed_sources=["google"])
    body = api.get(f"/api/calendar/range?start={day.isoformat()}&days=14").json()
    (start, end), _ = api.get_events.call_args
    assert start.date() == day and end - start == timedelta(days=14)
    assert body["count"] == 2 and body["source"] == "merged"
    assert body["unavailable"] == ["Google"]
    assert body["web_url"] == "https://cal.example.test/.web/"
    by = {e["title"]: e for e in body["events"]}
    assert by["Standup"]["calendar"] == "work" and by["Standup"]["source"] == "exchange"
    assert set(by["Dentist"]) == {
        "id",
        "title",
        "start",
        "end",
        "location",
        "description",
        "all_day",
        "calendar",
        "source",
    }


def test_nothing_answered(api):
    api.get_events.return_value = CalendarResponse(success=False, error="x", failed_sources=["caldav"])
    body = api.get("/api/calendar/range").json()
    assert body["source"] == "none" and body["events"] == [] and body["unavailable"] == ["personal"]


@pytest.mark.parametrize(
    "query",
    [
        "start=2026-13-01",
        "start=yesterday",
        "start=20261015",
        "start=2026-W42-3",
        "days=0",
        "days=43",
        "days=lots",
        "days=7.5",
        "days=-3",
        "start=1999-01-01",
        "start=2099-01-01",
    ],
)
def test_bad_input_is_400_and_never_reads(api, query):
    r = api.get(f"/api/calendar/range?{query}")
    assert r.status_code == 400 and r.json()["ok"] is False
    api.get_events.assert_not_called()


def test_repeat_request_is_served_from_cache(api):
    api.get("/api/calendar/range?days=7")
    api.get("/api/calendar/range?days=7")
    assert api.get_events.await_count == 1
    api.get("/api/calendar/range?days=14")
    assert api.get_events.await_count == 2


def test_today_failure_shape_unchanged(api):
    api.get_events.return_value = CalendarResponse(success=False, error="x", failed_sources=["caldav"])
    assert api.get("/api/calendar/today").json() == {
        "ok": True,
        "events": [],
        "source": "none",
        "count": 0,
        "unavailable": ["personal"],
        "web_url": "https://cal.example.test/.web/",
    }


def test_today_route_unchanged_after_refactor(api):
    api.get_events.return_value = CalendarResponse(success=True, events=[])
    body = api.get("/api/calendar/today").json()
    assert body == {
        "ok": True,
        "events": [],
        "source": "merged",
        "count": 0,
        "unavailable": [],
        "web_url": "https://cal.example.test/.web/",
    }


class _Clock:
    """Stand-in for routes_calendar's ``time`` module (only monotonic is read)."""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def monotonic(self) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch):
    # Patch the module reference, not time.monotonic globally — the event
    # loop under TestClient reads the real clock.
    c = _Clock()
    monkeypatch.setattr(routes_calendar, "time", c)
    return c


def test_cache_expires_after_ttl(api, clock):
    api.get("/api/calendar/range?days=7")
    clock.t += routes_calendar._RANGE_CACHE_TTL - 0.5
    api.get("/api/calendar/range?days=7")
    assert api.get_events.await_count == 1  # still fresh
    clock.t += 1.0
    api.get("/api/calendar/range?days=7")
    assert api.get_events.await_count == 2  # expired -> re-read
    api.get("/api/calendar/range?days=7")
    assert api.get_events.await_count == 2  # re-read answer is cached again


def test_cache_clears_when_full_and_stays_bounded(api, clock):
    cap = routes_calendar._RANGE_CACHE_MAX
    for n in range(1, cap + 1):
        api.get(f"/api/calendar/range?days={n}")
    assert len(routes_calendar._range_cache) == cap
    assert api.get_events.await_count == cap

    api.get("/api/calendar/range?days=33")  # one more distinct key -> clear, then store
    assert len(routes_calendar._range_cache) == 1
    assert api.get_events.await_count == cap + 1

    api.get("/api/calendar/range?days=1")  # evicted by the clear -> re-read
    assert api.get_events.await_count == cap + 2
    api.get("/api/calendar/range?days=33")  # survived the clear -> hit
    assert api.get_events.await_count == cap + 2


def test_cache_key_includes_start(api):
    today = datetime.now(TZ).date()
    api.get(f"/api/calendar/range?start={today.isoformat()}&days=7")
    api.get(f"/api/calendar/range?start={(today + timedelta(days=7)).isoformat()}&days=7")
    assert api.get_events.await_count == 2


@pytest.mark.parametrize(
    ("start", "days", "start_offset_h", "end_offset_h", "elapsed_h"),
    [
        # Spring forward Sun 2026-03-08 (CST -6 -> CDT -5): a 23-hour day.
        ("2026-03-02", 7, -6, -5, 7 * 24 - 1),
        # Fall back Sun 2026-11-01 (CDT -5 -> CST -6): a 25-hour day.
        ("2026-10-26", 7, -5, -6, 7 * 24 + 1),
        # Week starting the day after the change: no shift inside it.
        ("2026-03-09", 7, -5, -5, 7 * 24),
    ],
)
def test_dst_week_window_runs_local_midnight_to_local_midnight(
    api, start, days, start_offset_h, end_offset_h, elapsed_h
):
    api.get(f"/api/calendar/range?start={start}&days={days}")
    (w_start, w_end), _ = api.get_events.call_args
    first = datetime.fromisoformat(start).date()
    assert (w_start.date(), w_start.hour, w_start.minute) == (first, 0, 0)
    assert (w_end.date(), w_end.hour, w_end.minute) == (first + timedelta(days=days), 0, 0)
    assert w_start.utcoffset() == timedelta(hours=start_offset_h)
    assert w_end.utcoffset() == timedelta(hours=end_offset_h)
    # Same-tzinfo subtraction is wall-clock; compare real instants in UTC.
    utc = ZoneInfo("UTC")
    assert w_end.astimezone(utc) - w_start.astimezone(utc) == timedelta(hours=elapsed_h)


@pytest.mark.parametrize(
    "resp",
    [
        CalendarResponse(success=False, error="x", failed_sources=["caldav"]),
        CalendarResponse(success=True, events=[], failed_sources=["exchange"]),
    ],
)
def test_failed_or_partial_answers_are_not_cached(api, resp):
    """Retry after a failure must really re-read, not replay the failure."""
    api.get_events.return_value = resp
    api.get("/api/calendar/range")
    api.get("/api/calendar/range")
    assert api.get_events.await_count == 2
