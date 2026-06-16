"""
Tests for jobs_calendar.morning_briefing() — specifically the phone-sync-first
/ Google-fallback source priority and the fall-through guard added to mirror
tool_check_calendar / get_ambient_status.

Covers the two load-bearing branches of the phone/Google decision:
- Phone cache fresh (<24h) but ALL records fail to parse (start='', title='')
  -> WARNING logged + fall through to Google (client.list_events awaited).
- Phone cache fresh with >=1 parseable record -> phone used, Google NOT called.

Runs inside the brain-orchestrator container (full deps available). Skips
gracefully when orchestrator dependencies (chromadb, embedding model, etc.)
are unavailable locally.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _can_import():
    try:
        from orchestrator import jobs_calendar  # noqa: F401

        return True
    except (ImportError, ModuleNotFoundError):
        return False


_skip_no_deps = pytest.mark.skipif(
    not _can_import(),
    reason="jobs_calendar requires chromadb and full orchestrator dependencies",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _skip_without_deps():
    if not _can_import():
        pytest.skip("jobs_calendar deps unavailable")


@pytest.fixture
def reset_phone_cache():
    """Reset shared phone calendar cache before/after each test."""
    from orchestrator import shared

    orig_events = shared._phone_calendar_events
    orig_time = shared._phone_calendar_sync_time
    shared._phone_calendar_events = []
    shared._phone_calendar_sync_time = 0.0
    yield shared
    shared._phone_calendar_events = orig_events
    shared._phone_calendar_sync_time = orig_time


@pytest.fixture
def fresh_phone_sync_time():
    """A sync_time of 'just now' relative to the real clock.

    morning_briefing's age check uses real time.time(), so set sync_time to
    the actual wall clock to keep phone_age well under the 86400s freshness
    window without patching time.time inside the module.
    """
    import time as _time

    return _time.time()


@pytest.fixture
def patch_briefing_deps():
    """Patch the side-effecting collaborators of morning_briefing so the test
    isolates the phone/Google source-selection logic.

    Returns the mocked get_calendar_client factory so tests can configure the
    Google client and assert on its list_events call.
    """

    def _patch(*, google_configured=True, google_success=True, google_events=None):
        mock_client = MagicMock()
        mock_client.is_configured = google_configured

        response = MagicMock()
        response.success = google_success
        response.events = google_events or []
        mock_client.list_events = AsyncMock(return_value=response)

        return (
            mock_client,
            patch("orchestrator.jobs_calendar.get_calendar_client", return_value=mock_client),
            patch("orchestrator.jobs_calendar._announce_voice", new_callable=AsyncMock),
            patch("orchestrator.jobs_calendar._get_weather_forecast", new_callable=AsyncMock, return_value=None),
            patch("orchestrator.jobs_calendar.list_pending_reminders", return_value=[]),
        )

    return _patch


def _phone_event(title, start_str, all_day=False):
    return {"title": title, "start": start_str, "all_day": all_day}


# ---------------------------------------------------------------------------
# Branch A: fresh phone cache, all records unparseable -> Google fallback
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_phone_fresh_all_unparseable_falls_through_to_google(
    reset_phone_cache, fresh_phone_sync_time, patch_briefing_deps, caplog
):
    """The 2026-04-17 iPhone Shortcut bug: fresh phone cache, every record has
    empty start/title -> zero parsed -> WARNING + fall through to Google."""
    shared = reset_phone_cache
    shared._phone_calendar_events = [
        _phone_event("", ""),
        _phone_event("", ""),
        _phone_event("", ""),
    ]
    shared._phone_calendar_sync_time = fresh_phone_sync_time

    mock_client, p_client, p_voice, p_weather, p_reminders = patch_briefing_deps(
        google_configured=True, google_events=[]
    )

    with p_client, p_voice, p_weather, p_reminders:
        from orchestrator import jobs_calendar

        with caplog.at_level(logging.WARNING, logger="orchestrator.jobs_calendar"):
            await jobs_calendar.morning_briefing()

    # Google fallback path taken: list_events was awaited.
    mock_client.list_events.assert_awaited_once()
    # WARNING about the broken phone payload was logged.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("zero parsed" in r.getMessage() and "Falling through to Google" in r.getMessage() for r in warnings), (
        f"Expected fall-through WARNING; got: {[r.getMessage() for r in warnings]}"
    )


# ---------------------------------------------------------------------------
# Branch B: fresh phone cache, >=1 parseable record -> phone used, no Google
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_phone_fresh_with_parseable_record_uses_phone(
    reset_phone_cache, fresh_phone_sync_time, patch_briefing_deps
):
    """Fresh phone cache with at least one parseable record -> phone source is
    used and Google Calendar is NOT consulted."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from orchestrator.shared import TIMEZONE

    shared = reset_phone_cache
    # Build a phone event whose start parses and whose date == today (so the
    # phone branch produces a real event and the parsed count is >= 1).
    today = datetime.now(ZoneInfo(TIMEZONE)).date()
    start_str = datetime(today.year, today.month, today.day, 10, 30).strftime("%b %d, %Y %I:%M %p")
    shared._phone_calendar_events = [_phone_event("Standup", start_str)]
    shared._phone_calendar_sync_time = fresh_phone_sync_time

    mock_client, p_client, p_voice, p_weather, p_reminders = patch_briefing_deps(
        google_configured=True, google_events=[]
    )

    with p_client, p_voice, p_weather, p_reminders:
        from orchestrator import jobs_calendar

        await jobs_calendar.morning_briefing()

    # Phone source used -> Google list_events must NOT be called.
    mock_client.list_events.assert_not_awaited()


# ---------------------------------------------------------------------------
# Guard sanity: unparseable + Google unconfigured -> no Google call, no raise
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_phone_unparseable_google_unconfigured_no_crash(
    reset_phone_cache, fresh_phone_sync_time, patch_briefing_deps, caplog
):
    """All-unparseable phone cache + Google not configured -> falls through,
    skips Google (is_configured False), and still delivers without raising."""
    shared = reset_phone_cache
    shared._phone_calendar_events = [_phone_event("", "")]
    shared._phone_calendar_sync_time = fresh_phone_sync_time

    mock_client, p_client, p_voice, p_weather, p_reminders = patch_briefing_deps(
        google_configured=False, google_events=[]
    )

    with p_client, p_voice, p_weather, p_reminders:
        from orchestrator import jobs_calendar

        with caplog.at_level(logging.WARNING, logger="orchestrator.jobs_calendar"):
            await jobs_calendar.morning_briefing()

    # Google unconfigured -> list_events never awaited.
    mock_client.list_events.assert_not_awaited()
    # But the fall-through warning still fired.
    assert any("zero parsed" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
