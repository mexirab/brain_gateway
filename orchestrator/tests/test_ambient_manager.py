"""
Tests for ambient_manager.get_ambient_status() — specifically the
phone-sync-first / Google-fallback source priority added on 2026-04-17 to
mirror tool_check_calendar.

Covers:
- Phone fresh + parseable + in 12h window -> uses phone
- Phone cache present but none parse -> falls through to Google (warning logged)
- Phone cache empty -> falls through to Google
- Phone cache stale (>24h) -> falls through to Google
- all_day phone events excluded
- Phone events outside 12h window excluded
- schedule_density: <=15min => busy, <=60 => light, else clear
- Empty upcoming from either source => events_remaining=0, next=None, clear
- Phone broken AND Google not configured => schedule_density=unknown

Runs inside the brain-orchestrator container (full deps available).
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest


def _can_import():
    try:
        from orchestrator import ambient_manager  # noqa: F401

        return True
    except (ImportError, ModuleNotFoundError):
        return False


_skip_no_deps = pytest.mark.skipif(
    not _can_import(),
    reason="ambient_manager requires full orchestrator dependencies",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TZ_NAME = "America/Chicago"
TZ = ZoneInfo(TZ_NAME)

# A fixed "now" for deterministic minutes_away calculations.
# Picked mid-day so we have room for 12h-window edge cases.
FIXED_NOW = datetime(2026, 4, 17, 10, 0, 0, tzinfo=TZ)


class _FakeDatetime:
    """Stand-in for the datetime class imported into ambient_manager.

    Only ``datetime.now(tz)`` is patched; everything else (constructors,
    ``fromisoformat``, etc.) falls through to the real datetime class so
    downstream imports (like jobs_calendar._parse_phone_datetime) keep
    working when they import datetime freshly.
    """

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return FIXED_NOW.replace(tzinfo=None)
        return FIXED_NOW.astimezone(tz)


@pytest.fixture(autouse=True)
def _skip_without_deps():
    if not _can_import():
        pytest.skip("ambient_manager deps unavailable")


@pytest.fixture
def reset_phone_cache():
    """Reset shared phone cache before/after each test."""
    from orchestrator import shared

    orig_events = shared._phone_calendar_events
    orig_time = shared._phone_calendar_sync_time
    shared._phone_calendar_events = []
    shared._phone_calendar_sync_time = 0.0
    yield shared
    shared._phone_calendar_events = orig_events
    shared._phone_calendar_sync_time = orig_time


@pytest.fixture
def patched_now():
    """Patch ambient_manager.datetime so now() returns FIXED_NOW."""
    with patch("orchestrator.ambient_manager.datetime", _FakeDatetime):
        yield FIXED_NOW


@pytest.fixture
def fresh_phone_sync_time():
    """A sync_time of 'just now' relative to the real clock.

    The age check uses real time.time(), so we set sync_time to the actual
    wall clock. Patching time.time inside the module is more invasive than
    needed for these tests.
    """
    import time as _time

    return _time.time()


@pytest.fixture
def patch_google_client():
    """Factory for patching get_calendar_client with a configurable mock."""

    def _patch(*, is_configured=False, success=True, events=None):
        mock_client = MagicMock()
        mock_client.is_configured = is_configured

        response = MagicMock()
        response.success = success
        response.events = events or []
        mock_client.get_upcoming = AsyncMock(return_value=response)

        return patch(
            "orchestrator.google_calendar.get_calendar_client",
            return_value=mock_client,
        )

    return _patch


def _google_event(title, start, all_day=False):
    """Build a minimal fake CalendarEvent-like object."""
    ev = MagicMock()
    ev.title = title
    ev.start = start
    ev.all_day = all_day
    return ev


def _phone_event(title, start_str, all_day=False):
    return {"title": title, "start": start_str, "all_day": all_day}


# ---------------------------------------------------------------------------
# Test: phone fresh + parseable + in window -> phone wins
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_phone_fresh_and_parseable_uses_phone(
    reset_phone_cache, patched_now, fresh_phone_sync_time, patch_google_client, caplog
):
    """Phone sync fresh + parseable + events in 12h window -> use phone."""
    shared = reset_phone_cache
    # Two phone events: one at 10:30am (30m away), one at 2pm (4h away).
    # Both inside the 12h window from 10am FIXED_NOW.
    shared._phone_calendar_events = [
        _phone_event("Standup", "Apr 17, 2026 at 10:30 AM"),
        _phone_event("Review", "Apr 17, 2026 at 2:00 PM"),
    ]
    shared._phone_calendar_sync_time = fresh_phone_sync_time

    with patch_google_client(is_configured=True, events=[]):
        from orchestrator import ambient_manager

        with caplog.at_level("INFO", logger="orchestrator.ambient_manager"):
            status = await ambient_manager.get_ambient_status()

    assert status["events_remaining"] == 2
    assert status["next_event"] is not None
    assert status["next_event"]["title"] == "Standup"
    # 30 minutes away -> light
    assert status["schedule_density"] == "light"
    assert status["next_event"]["minutes_away"] == 30
    # Should have logged that we're using phone
    assert any("Using phone sync" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Test: phone cache has records but NONE parse -> fall through to Google
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_phone_records_present_but_none_parse_falls_through(
    reset_phone_cache, patched_now, fresh_phone_sync_time, patch_google_client, caplog
):
    """The 2026-04-17 iPhone Shortcut bug: empty start strings."""
    shared = reset_phone_cache
    # All records have empty start -> _parse_phone_datetime raises ValueError
    shared._phone_calendar_events = [
        _phone_event("", ""),
        _phone_event("", ""),
        _phone_event("", ""),
    ]
    shared._phone_calendar_sync_time = fresh_phone_sync_time

    # Google has one event 1h15m away (75min) -> clear
    google_start = FIXED_NOW + timedelta(minutes=75)
    with patch_google_client(
        is_configured=True,
        events=[_google_event("Google-Only Event", google_start)],
    ):
        from orchestrator import ambient_manager

        with caplog.at_level("WARNING", logger="orchestrator.ambient_manager"):
            status = await ambient_manager.get_ambient_status()

    assert status["events_remaining"] == 1
    assert status["next_event"]["title"] == "Google-Only Event"
    # 75 min away -> neither busy (<=15) nor light (<=60) -> clear
    assert status["schedule_density"] == "clear"
    # Warning about zero parsed should be logged
    assert any(
        "zero parsed" in rec.message and "falling back to google" in rec.message.lower() for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Test: phone cache empty -> fall through to Google
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_phone_cache_empty_falls_through_to_google(reset_phone_cache, patched_now, patch_google_client):
    """Never synced -> phone cache empty -> Google fallback."""
    shared = reset_phone_cache
    shared._phone_calendar_events = []
    shared._phone_calendar_sync_time = 0.0  # never synced

    google_start = FIXED_NOW + timedelta(hours=3)
    with patch_google_client(
        is_configured=True,
        events=[_google_event("Google Event", google_start)],
    ):
        from orchestrator import ambient_manager

        status = await ambient_manager.get_ambient_status()

    assert status["events_remaining"] == 1
    assert status["next_event"]["title"] == "Google Event"


# ---------------------------------------------------------------------------
# Test: phone cache stale (>24h) -> fall through to Google
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_phone_cache_stale_falls_through_to_google(reset_phone_cache, patched_now, patch_google_client):
    """Phone synced >24h ago -> treated as stale, Google fallback."""
    import time as _time

    shared = reset_phone_cache
    # Records look valid but the sync timestamp is 25h old
    shared._phone_calendar_events = [
        _phone_event("Old but parseable", "Apr 17, 2026 at 10:30 AM"),
    ]
    shared._phone_calendar_sync_time = _time.time() - (25 * 3600)

    google_start = FIXED_NOW + timedelta(hours=2)
    with patch_google_client(
        is_configured=True,
        events=[_google_event("From Google", google_start)],
    ):
        from orchestrator import ambient_manager

        status = await ambient_manager.get_ambient_status()

    # Only the Google event should appear — not the stale phone one
    assert status["events_remaining"] == 1
    assert status["next_event"]["title"] == "From Google"


# ---------------------------------------------------------------------------
# Test: phone all_day events excluded
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_phone_all_day_events_excluded(
    reset_phone_cache, patched_now, fresh_phone_sync_time, patch_google_client
):
    """all_day phone events should not appear in `upcoming`."""
    shared = reset_phone_cache
    shared._phone_calendar_events = [
        _phone_event("All Day Birthday", "Apr 17, 2026 at 12:00 AM", all_day=True),
        _phone_event("Timed Meeting", "Apr 17, 2026 at 11:00 AM"),
    ]
    shared._phone_calendar_sync_time = fresh_phone_sync_time

    with patch_google_client(is_configured=False):
        from orchestrator import ambient_manager

        status = await ambient_manager.get_ambient_status()

    # Only the timed one should count
    assert status["events_remaining"] == 1
    assert status["next_event"]["title"] == "Timed Meeting"


# ---------------------------------------------------------------------------
# Test: phone event outside 12h window excluded
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_phone_event_outside_12h_window_excluded(
    reset_phone_cache, patched_now, fresh_phone_sync_time, patch_google_client
):
    """14h-out phone event should be excluded (window_end = now+12h)."""
    shared = reset_phone_cache
    # 10am + 14h = midnight + 2h -> outside 12h window
    shared._phone_calendar_events = [
        _phone_event("Next Day Early Meeting", "Apr 18, 2026 at 12:00 AM"),
    ]
    shared._phone_calendar_sync_time = fresh_phone_sync_time

    with patch_google_client(is_configured=False):
        from orchestrator import ambient_manager

        status = await ambient_manager.get_ambient_status()

    # Phone had one record that parsed, but it was outside the window.
    # Because at least one record parsed, source = "phone" (even though
    # the filtered list is empty). events_remaining should be 0.
    assert status["events_remaining"] == 0
    assert status["next_event"] is None
    assert status["schedule_density"] == "clear"


# ---------------------------------------------------------------------------
# Test: <=15min -> busy
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_schedule_density_busy_when_next_within_15_min(
    reset_phone_cache, patched_now, fresh_phone_sync_time, patch_google_client
):
    shared = reset_phone_cache
    # 10am + 10min = 10:10am -> 10 minutes away -> busy
    shared._phone_calendar_events = [
        _phone_event("Imminent", "Apr 17, 2026 at 10:10 AM"),
    ]
    shared._phone_calendar_sync_time = fresh_phone_sync_time

    with patch_google_client(is_configured=False):
        from orchestrator import ambient_manager

        status = await ambient_manager.get_ambient_status()

    assert status["schedule_density"] == "busy"
    assert status["next_event"]["minutes_away"] == 10


# ---------------------------------------------------------------------------
# Test: 30min -> light
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_schedule_density_light_at_30_min(
    reset_phone_cache, patched_now, fresh_phone_sync_time, patch_google_client
):
    shared = reset_phone_cache
    shared._phone_calendar_events = [
        _phone_event("Half hour out", "Apr 17, 2026 at 10:30 AM"),
    ]
    shared._phone_calendar_sync_time = fresh_phone_sync_time

    with patch_google_client(is_configured=False):
        from orchestrator import ambient_manager

        status = await ambient_manager.get_ambient_status()

    assert status["schedule_density"] == "light"
    assert status["next_event"]["minutes_away"] == 30


# ---------------------------------------------------------------------------
# Test: no events from either source but one succeeded -> clear
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_no_events_from_either_source_but_google_ok(reset_phone_cache, patched_now, patch_google_client):
    """Phone cache empty; Google returns empty event list successfully."""
    shared = reset_phone_cache
    shared._phone_calendar_events = []
    shared._phone_calendar_sync_time = 0.0

    with patch_google_client(is_configured=True, success=True, events=[]):
        from orchestrator import ambient_manager

        status = await ambient_manager.get_ambient_status()

    assert status["events_remaining"] == 0
    assert status["next_event"] is None
    assert status["schedule_density"] == "clear"


# ---------------------------------------------------------------------------
# Test: phone broken AND Google not configured -> unknown
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_phone_broken_and_google_not_configured_is_unknown(
    reset_phone_cache, patched_now, fresh_phone_sync_time, patch_google_client
):
    """Neither source usable -> schedule_density='unknown'."""
    shared = reset_phone_cache
    # Broken phone cache (records but none parseable)
    shared._phone_calendar_events = [
        _phone_event("", ""),
        _phone_event("", ""),
    ]
    shared._phone_calendar_sync_time = fresh_phone_sync_time

    with patch_google_client(is_configured=False):
        from orchestrator import ambient_manager

        status = await ambient_manager.get_ambient_status()

    assert status["schedule_density"] == "unknown"
    # no events_remaining set when source is None
    assert "events_remaining" not in status
    # No next_event either (only set inside the source-not-None branch)
    assert status.get("next_event") is None or "next_event" not in status


# ---------------------------------------------------------------------------
# Test: Google excludes all_day as well
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_google_all_day_events_excluded(reset_phone_cache, patched_now, patch_google_client):
    """When falling through to Google, all_day events are excluded there too."""
    shared = reset_phone_cache
    shared._phone_calendar_events = []

    timed_start = FIXED_NOW + timedelta(hours=1)
    allday_start = FIXED_NOW.replace(hour=0, minute=0)
    with patch_google_client(
        is_configured=True,
        events=[
            _google_event("All Day Thing", allday_start, all_day=True),
            _google_event("Real Meeting", timed_start),
        ],
    ):
        from orchestrator import ambient_manager

        status = await ambient_manager.get_ambient_status()

    assert status["events_remaining"] == 1
    assert status["next_event"]["title"] == "Real Meeting"
