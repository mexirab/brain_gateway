"""
Regression tests for email-to-calendar dedup (jobs_calendar).

Two pieces work together after the perf rebase:
  - _dedup_prefetch_days(events, today): sizes the ONE calendar fetch to cover
    the furthest extracted event. A fixed days_ahead=1 missed any event >1 day
    out and re-created it as a duplicate on every scan (#33).
  - _event_exists(day_events, created_keys, title, start): matches an extracted
    event against the pre-fetched list and events created earlier this run.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from orchestrator.google_calendar import CalendarEvent
from orchestrator.jobs_calendar import _dedup_prefetch_days, _event_exists
from orchestrator.shared import TIMEZONE

_TZ = ZoneInfo(TIMEZONE)


def _event(title: str, start: datetime) -> CalendarEvent:
    return CalendarEvent(id="ev1", title=title, start=start, end=start + timedelta(hours=1))


def _extracted(start: datetime) -> dict:
    return {"title": "x", "start_time": start.isoformat()}


# ---------------------------------------------------------------------------
# _dedup_prefetch_days — the #33 window-sizing regression
# ---------------------------------------------------------------------------


class TestPrefetchWindow:
    def test_far_out_event_widens_window(self):
        today = date(2026, 7, 4)
        start = datetime(2026, 7, 9, 10, 0, tzinfo=_TZ)  # 5 days out
        # 5 days out + 1 to reach through that day; old fixed=1 missed it.
        assert _dedup_prefetch_days([_extracted(start)], today) >= 6

    def test_window_covers_furthest_of_many(self):
        today = date(2026, 7, 4)
        events = [
            _extracted(datetime(2026, 7, 5, 9, 0, tzinfo=_TZ)),
            _extracted(datetime(2026, 7, 20, 9, 0, tzinfo=_TZ)),  # furthest
            _extracted(datetime(2026, 7, 6, 9, 0, tzinfo=_TZ)),
        ]
        assert _dedup_prefetch_days(events, today) >= 17

    def test_same_day_floors_at_one(self):
        today = date(2026, 7, 4)
        start = datetime(2026, 7, 4, 23, 0, tzinfo=_TZ)
        assert _dedup_prefetch_days([_extracted(start)], today) >= 1

    def test_past_or_unparseable_never_below_one(self):
        today = date(2026, 7, 4)
        assert _dedup_prefetch_days([], today) == 1
        assert _dedup_prefetch_days([{"start_time": "not-a-date"}], today) == 1
        assert _dedup_prefetch_days([{"title": "no start key"}], today) == 1
        # A past event floors at 1 rather than going negative.
        past = datetime(2026, 6, 1, 9, 0, tzinfo=_TZ)
        assert _dedup_prefetch_days([_extracted(past)], today) == 1


# ---------------------------------------------------------------------------
# _event_exists — matching against the pre-fetched list + this-run creations
# ---------------------------------------------------------------------------


class TestEventExists:
    def test_same_day_title_match(self):
        start = datetime(2026, 7, 9, 10, 0, tzinfo=_TZ)
        day_events = [_event("Dentist Appointment", start)]
        assert _event_exists(day_events, [], "dentist appointment", start) is True

    def test_substring_match_either_direction(self):
        start = datetime(2026, 7, 9, 10, 0, tzinfo=_TZ)
        # existing title contains the extracted title
        assert _event_exists([_event("Dentist Appointment - Dr. Lee", start)], [], "dentist appointment", start)
        # extracted title contains the existing title
        assert _event_exists([_event("Dentist", start)], [], "dentist appointment tomorrow", start)

    def test_no_title_match_returns_false(self):
        start = datetime(2026, 7, 9, 10, 0, tzinfo=_TZ)
        assert _event_exists([_event("Totally Unrelated", start)], [], "Dentist", start) is False

    def test_same_title_different_day_returns_false(self):
        start = datetime(2026, 7, 9, 10, 0, tzinfo=_TZ)
        other_day = _event("Dentist", start - timedelta(days=1))
        assert _event_exists([other_day], [], "Dentist", start) is False

    def test_created_this_run_dedups(self):
        start = datetime(2026, 7, 9, 10, 0, tzinfo=_TZ)
        created_keys = [("dentist appointment", start.date())]
        # Not on the calendar yet, but created earlier in this same batch.
        assert _event_exists([], created_keys, "Dentist Appointment", start) is True

    def test_created_this_run_different_day_returns_false(self):
        start = datetime(2026, 7, 9, 10, 0, tzinfo=_TZ)
        created_keys = [("dentist", (start - timedelta(days=2)).date())]
        assert _event_exists([], created_keys, "Dentist", start) is False
