"""
Regression tests for jobs_calendar._event_exists_on_calendar (email-to-calendar
dedup).

The dedup check used a fixed cal.list_events(days_ahead=1), so any emailed
event more than one day out was never found in the window and got re-created
as a duplicate on every scan. The window must be computed from the extracted
event's start date.

The fake calendar below mimics the real list_events contract: it only returns
events inside [now, now + days_ahead), so these tests fail with the old
fixed-1-day window.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from orchestrator.google_calendar import CalendarEvent, CalendarResponse
from orchestrator.jobs_calendar import _event_exists_on_calendar
from orchestrator.shared import TIMEZONE

_TZ = ZoneInfo(TIMEZONE)


class _FakeCalendar:
    """list_events honoring the [now, now + days_ahead) window semantics."""

    def __init__(self, events, success: bool = True):
        self._events = events
        self._success = success
        self.calls: list[int] = []  # days_ahead per call

    async def list_events(self, days_ahead: int = 7, calendar_id: str = "primary"):
        self.calls.append(days_ahead)
        if not self._success:
            return CalendarResponse(success=False, error="boom")
        now = datetime.now(_TZ)
        horizon = now + timedelta(days=days_ahead)
        visible = [e for e in self._events if now <= e.start < horizon]
        return CalendarResponse(success=True, events=visible)


def _event(title: str, start: datetime) -> CalendarEvent:
    return CalendarEvent(id="ev1", title=title, start=start, end=start + timedelta(hours=1))


@pytest.mark.asyncio
async def test_dedup_finds_event_several_days_out():
    """The core regression: an emailed event 5 days out already on the
    calendar must be detected (old fixed days_ahead=1 window missed it)."""
    start = datetime.now(_TZ).replace(microsecond=0) + timedelta(days=5)
    cal = _FakeCalendar([_event("Dentist Appointment", start)])

    assert await _event_exists_on_calendar(cal, "dentist appointment", start) is True
    # The queried window actually covers the event's date.
    assert len(cal.calls) == 1
    assert cal.calls[0] >= 6  # 5 days out + 1 to reach through that day


@pytest.mark.asyncio
async def test_dedup_same_day_event_still_covered():
    start = datetime.now(_TZ) + timedelta(hours=2)
    cal = _FakeCalendar([_event("Dentist", start)])

    assert await _event_exists_on_calendar(cal, "Dentist", start) is True
    assert cal.calls[0] >= 1


@pytest.mark.asyncio
async def test_dedup_substring_title_match_either_direction():
    start = datetime.now(_TZ) + timedelta(days=10)
    cal = _FakeCalendar([_event("Dentist Appointment - Dr. Lee", start)])

    assert await _event_exists_on_calendar(cal, "dentist appointment", start) is True


@pytest.mark.asyncio
async def test_dedup_no_match_returns_false():
    start = datetime.now(_TZ) + timedelta(days=3)
    cal = _FakeCalendar([_event("Totally Unrelated", start)])

    assert await _event_exists_on_calendar(cal, "Dentist", start) is False


@pytest.mark.asyncio
async def test_dedup_same_title_different_day_returns_false():
    start = datetime.now(_TZ) + timedelta(days=3)
    other_day = start - timedelta(days=1)  # inside the queried window, wrong day
    cal = _FakeCalendar([_event("Dentist", other_day)])

    assert await _event_exists_on_calendar(cal, "Dentist", start) is False


@pytest.mark.asyncio
async def test_dedup_list_failure_returns_false():
    start = datetime.now(_TZ) + timedelta(days=2)
    cal = _FakeCalendar([], success=False)

    assert await _event_exists_on_calendar(cal, "Dentist", start) is False


@pytest.mark.asyncio
async def test_dedup_naive_start_does_not_crash():
    """Extracted starts are normally tz-aware, but a naive one must still
    produce a sane window instead of raising."""
    naive_start = datetime.now() + timedelta(days=4)
    aware = naive_start.replace(tzinfo=_TZ)
    cal = _FakeCalendar([_event("Checkup", aware)])

    assert await _event_exists_on_calendar(cal, "Checkup", naive_start) is True
    assert cal.calls[0] >= 5
