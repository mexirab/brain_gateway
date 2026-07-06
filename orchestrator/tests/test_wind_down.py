"""
Tests for the sleep wind-down ladder (jobs_winddown) and its morning half
(sleep_mode stamp -> morning_briefing short-night adaptation).

Covers:
- T-60 lights rung: scene fan-out, no-scene no-op, DND skip, per-scene error
  isolation.
- T-30 nudge: screens-away text + tomorrow anchor, no-events wording, DND and
  active-routine silent skips, calendar-failure resilience.
- sleep_mode("on") stamps app_state.sleep_started_at; "off" does not.
- morning_briefing: short night -> gentle greeting + weather skipped + stamp
  cleared; stale stamp (>16h) -> normal; no stamp -> normal.

Runs inside the brain-orchestrator container (full deps available). Skips
gracefully when orchestrator dependencies are unavailable locally.
"""

import logging
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _can_import():
    try:
        from orchestrator import jobs_winddown  # noqa: F401

        return True
    except (ImportError, ModuleNotFoundError):
        return False


_skip_no_deps = pytest.mark.skipif(
    not _can_import(),
    reason="jobs_winddown requires full orchestrator dependencies",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _skip_without_deps():
    if not _can_import():
        pytest.skip("jobs_winddown deps unavailable")


@pytest.fixture
def reset_shared():
    """Reset the shared state the ladder reads: DND, phone cache, focus."""
    from orchestrator import shared

    orig_dnd = shared.DND_ACTIVE
    orig_events = shared._phone_calendar_events
    orig_time = shared._phone_calendar_sync_time
    orig_focus = shared.current_focus_session.to_dict()
    shared.DND_ACTIVE = False
    shared._phone_calendar_events = []
    shared._phone_calendar_sync_time = 0.0
    shared.current_focus_session.reset()
    yield shared
    shared.DND_ACTIVE = orig_dnd
    shared._phone_calendar_events = orig_events
    shared._phone_calendar_sync_time = orig_time
    shared.current_focus_session.update(orig_focus)


def _ha_result(success=True, message=""):
    r = MagicMock()
    r.success = success
    r.message = message
    return r


def _tomorrow_event(title, hour=9, minute=0, all_day=False):
    from zoneinfo import ZoneInfo

    from orchestrator.shared import TIMEZONE

    tz = ZoneInfo(TIMEZONE)
    day = datetime.now(tz).date() + timedelta(days=1)
    return {
        "title": title,
        "start": datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz),
        "all_day": all_day,
        "location": "",
    }


# ---------------------------------------------------------------------------
# T-60: lights rung
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_dim_activates_each_configured_scene(reset_shared):
    shared = reset_shared
    ha = MagicMock()
    ha.call_service = AsyncMock(return_value=_ha_result())

    from orchestrator import jobs_winddown

    with (
        patch.object(shared, "WIND_DOWN_SCENE", "scene.living_room_relax, scene.bedroom_dimmed"),
        patch.object(shared, "ha_client", ha),
    ):
        await jobs_winddown.wind_down_dim()

    assert ha.call_service.await_count == 2
    called = [c.args for c in ha.call_service.await_args_list]
    assert ("scene.living_room_relax", "turn_on") in called
    assert ("scene.bedroom_dimmed", "turn_on") in called


@_skip_no_deps
@pytest.mark.asyncio
async def test_dim_noop_without_scene(reset_shared, caplog):
    shared = reset_shared
    ha = MagicMock()
    ha.call_service = AsyncMock()

    from orchestrator import jobs_winddown

    with (
        patch.object(shared, "WIND_DOWN_SCENE", ""),
        patch.object(shared, "ha_client", ha),
        caplog.at_level(logging.INFO, logger="orchestrator.jobs_winddown"),
    ):
        await jobs_winddown.wind_down_dim()

    ha.call_service.assert_not_awaited()
    assert any("No WIND_DOWN_SCENE configured" in r.getMessage() for r in caplog.records)


@_skip_no_deps
@pytest.mark.asyncio
async def test_dim_skipped_under_dnd(reset_shared):
    """scene.turn_on can raise lights that are off — never fire under DND."""
    shared = reset_shared
    shared.DND_ACTIVE = True
    ha = MagicMock()
    ha.call_service = AsyncMock()

    from orchestrator import jobs_winddown

    with (
        patch.object(shared, "WIND_DOWN_SCENE", "scene.bedroom_dimmed"),
        patch.object(shared, "ha_client", ha),
    ):
        await jobs_winddown.wind_down_dim()

    ha.call_service.assert_not_awaited()


@_skip_no_deps
@pytest.mark.asyncio
async def test_dim_scene_error_does_not_stop_remaining_scenes(reset_shared):
    shared = reset_shared
    ha = MagicMock()
    ha.call_service = AsyncMock(side_effect=[RuntimeError("HA down"), _ha_result()])

    from orchestrator import jobs_winddown

    with (
        patch.object(shared, "WIND_DOWN_SCENE", "scene.a,scene.b"),
        patch.object(shared, "ha_client", ha),
    ):
        await jobs_winddown.wind_down_dim()

    assert ha.call_service.await_count == 2


# ---------------------------------------------------------------------------
# T-30: screens-away nudge
# ---------------------------------------------------------------------------


def _nudge_patches(*, events=None, announce_result=None, tomorrow_error=None):
    tomorrow = AsyncMock(return_value=(events or [], "phone"))
    if tomorrow_error:
        tomorrow = AsyncMock(side_effect=tomorrow_error)
    return {
        "voice": patch(
            "orchestrator.jobs_winddown._announce_voice",
            new_callable=AsyncMock,
            return_value=announce_result or {"success": True},
        ),
        "tomorrow": patch("orchestrator.jobs_calendar.get_tomorrow_events", tomorrow),
    }


async def _run_nudge(patches):
    from contextlib import ExitStack

    from orchestrator import jobs_winddown

    with ExitStack() as stack:
        mocks = {name: stack.enter_context(p) for name, p in patches.items()}
        await jobs_winddown.wind_down_nudge()
    return mocks


@_skip_no_deps
@pytest.mark.asyncio
async def test_nudge_includes_tomorrow_anchor(reset_shared):
    mocks = await _run_nudge(_nudge_patches(events=[_tomorrow_event("Dentist", hour=8, minute=30)]))

    text = mocks["voice"].await_args.args[0]
    assert "Screens away" in text
    assert "Dentist" in text and "8:30" in text
    assert "wind down" in text.lower()


@_skip_no_deps
@pytest.mark.asyncio
async def test_nudge_no_events_wording(reset_shared):
    mocks = await _run_nudge(_nudge_patches(events=[]))
    text = mocks["voice"].await_args.args[0]
    assert "Nothing early tomorrow" in text


@_skip_no_deps
@pytest.mark.asyncio
async def test_nudge_all_day_only_counts_as_no_anchor(reset_shared):
    mocks = await _run_nudge(_nudge_patches(events=[_tomorrow_event("Trash day", all_day=True)]))
    text = mocks["voice"].await_args.args[0]
    assert "Nothing early tomorrow" in text


@_skip_no_deps
@pytest.mark.asyncio
async def test_nudge_skipped_under_dnd(reset_shared):
    shared = reset_shared
    shared.DND_ACTIVE = True
    mocks = await _run_nudge(_nudge_patches())
    mocks["voice"].assert_not_awaited()


@_skip_no_deps
@pytest.mark.asyncio
async def test_nudge_skipped_during_routine(reset_shared):
    from orchestrator import routine_manager

    patches = _nudge_patches()
    with patch.object(routine_manager, "_active_session", MagicMock()):
        mocks = await _run_nudge(patches)
    mocks["voice"].assert_not_awaited()


@_skip_no_deps
@pytest.mark.asyncio
async def test_nudge_survives_calendar_failure(reset_shared):
    """A calendar blowup drops the anchor line but the nudge still speaks."""
    mocks = await _run_nudge(_nudge_patches(tomorrow_error=RuntimeError("calendar down")))
    text = mocks["voice"].await_args.args[0]
    assert "Screens away" in text


# ---------------------------------------------------------------------------
# sleep_mode stamps sleep_started_at
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_sleep_mode_on_stamps_sleep_started(reset_shared):
    from orchestrator import tool_handlers

    with (
        patch("orchestrator.state_store.set_app_state") as set_state,
        patch("orchestrator.state_store.set_notification_flag"),
    ):
        await tool_handlers._reg_sleep_mode({"action": "on"})

    set_state.assert_called_once()
    key, value = set_state.call_args.args
    assert key == "sleep_started_at"
    datetime.fromisoformat(value)  # parseable timestamp


@_skip_no_deps
@pytest.mark.asyncio
async def test_sleep_mode_off_does_not_stamp(reset_shared):
    from orchestrator import tool_handlers

    with (
        patch("orchestrator.state_store.set_app_state") as set_state,
        patch("orchestrator.state_store.clear_notification_flag"),
    ):
        await tool_handlers._reg_sleep_mode({"action": "off"})

    set_state.assert_not_called()


# ---------------------------------------------------------------------------
# Morning short-night adaptation
# ---------------------------------------------------------------------------


def _morning_patches(*, sleep_entry, announce_result=None):
    mock_client = MagicMock()
    mock_client.is_configured = False
    return {
        "client": patch("orchestrator.jobs_calendar.get_calendar_client", return_value=mock_client),
        "voice": patch(
            "orchestrator.jobs_calendar._announce_voice",
            new_callable=AsyncMock,
            return_value=announce_result or {"success": True},
        ),
        "weather": patch("orchestrator.jobs_calendar._get_weather_forecast", new_callable=AsyncMock, return_value=None),
        "reminders": patch("orchestrator.jobs_calendar.list_pending_reminders", return_value=[]),
        "get_state": patch(
            "orchestrator.state_store.get_app_state_entry",
            side_effect=lambda key: sleep_entry if key == "sleep_started_at" else None,
        ),
        "delete_state": patch("orchestrator.state_store.delete_app_state"),
        "outcomes": patch("orchestrator.state_store.get_recent_reminder_outcomes", return_value=[]),
    }


def _sleep_entry(hours_ago):
    return {
        "value": (datetime.now() - timedelta(hours=hours_ago)).isoformat(),
        "updated_at": (datetime.now() - timedelta(hours=hours_ago)).isoformat(),
    }


async def _run_morning(patches):
    from contextlib import ExitStack

    from orchestrator import jobs_calendar

    with ExitStack() as stack:
        mocks = {name: stack.enter_context(p) for name, p in patches.items()}
        await jobs_calendar.morning_briefing()
    return mocks


@_skip_no_deps
@pytest.mark.asyncio
async def test_short_night_softens_briefing(reset_shared):
    """Goodnight 5h ago -> gentle greeting, weather skipped, stamp cleared."""
    mocks = await _run_morning(_morning_patches(sleep_entry=_sleep_entry(5)))

    text = mocks["voice"].await_args.args[0]
    assert "Short night" in text
    assert "Good morning" not in text
    mocks["weather"].assert_not_awaited()
    mocks["delete_state"].assert_called_once_with("sleep_started_at")


@_skip_no_deps
@pytest.mark.asyncio
async def test_full_night_stays_normal(reset_shared):
    """Goodnight 8h ago -> normal greeting, weather included, stamp cleared."""
    mocks = await _run_morning(_morning_patches(sleep_entry=_sleep_entry(8)))

    text = mocks["voice"].await_args.args[0]
    assert "Good morning" in text
    mocks["weather"].assert_awaited_once()
    mocks["delete_state"].assert_called_once_with("sleep_started_at")


@_skip_no_deps
@pytest.mark.asyncio
async def test_stale_stamp_ignored(reset_shared):
    """A 20h-old stamp (afternoon timed mute yesterday) is not last night."""
    mocks = await _run_morning(_morning_patches(sleep_entry=_sleep_entry(20)))

    text = mocks["voice"].await_args.args[0]
    assert "Good morning" in text
    mocks["delete_state"].assert_called_once_with("sleep_started_at")


@_skip_no_deps
@pytest.mark.asyncio
async def test_no_stamp_stays_normal(reset_shared):
    mocks = await _run_morning(_morning_patches(sleep_entry=None))
    text = mocks["voice"].await_args.args[0]
    assert "Good morning" in text
    mocks["delete_state"].assert_not_called()
