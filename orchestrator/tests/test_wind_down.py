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
async def test_dim_ignores_non_scene_entities(reset_shared, caplog):
    """A .env typo (switch.garage_door) must not get a nightly turn_on."""
    shared = reset_shared
    ha = MagicMock()
    ha.call_service = AsyncMock(return_value=_ha_result())

    from orchestrator import jobs_winddown

    with (
        patch.object(shared, "WIND_DOWN_SCENE", "scene.bedroom_dimmed,switch.garage_door"),
        patch.object(shared, "ha_client", ha),
        caplog.at_level(logging.WARNING, logger="orchestrator.jobs_winddown"),
    ):
        await jobs_winddown.wind_down_dim()

    assert ha.call_service.await_count == 1
    assert ha.call_service.await_args.args == ("scene.bedroom_dimmed", "turn_on")
    assert any("Ignoring non-scene entry" in r.getMessage() for r in caplog.records)


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
# T-60: dim heartbeat (dead-man's-switch)
#
# The heartbeat is stamped at the TOP of wind_down_dim, before every early
# return, so it proves the job FIRED independent of whether it did any work.
# Without that, a scheduler that drops only the dim job leaves the Sleep
# Wind-Down panels green/empty (the scene counter stays silent on the no-op
# nights). These tests pin the stamp to the early-return paths specifically.
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_dim_heartbeat_stamped_on_normal_run(reset_shared):
    shared = reset_shared
    ha = MagicMock()
    ha.call_service = AsyncMock(return_value=_ha_result())

    from orchestrator import jobs_winddown

    with (
        patch.object(shared, "WIND_DOWN_SCENE", "scene.bedroom_dimmed"),
        patch.object(shared, "ha_client", ha),
        patch.object(jobs_winddown, "WIND_DOWN_DIM_LAST_RUN") as dim_hb,
    ):
        await jobs_winddown.wind_down_dim()

    dim_hb.set_to_current_time.assert_called_once()


@_skip_no_deps
@pytest.mark.asyncio
async def test_dim_heartbeat_stamped_even_without_scene(reset_shared):
    """No WIND_DOWN_SCENE -> no scene attempted, but the job still fired."""
    shared = reset_shared
    ha = MagicMock()
    ha.call_service = AsyncMock()

    from orchestrator import jobs_winddown

    with (
        patch.object(shared, "WIND_DOWN_SCENE", ""),
        patch.object(shared, "ha_client", ha),
        patch.object(jobs_winddown, "WIND_DOWN_DIM_LAST_RUN") as dim_hb,
    ):
        await jobs_winddown.wind_down_dim()

    ha.call_service.assert_not_awaited()
    dim_hb.set_to_current_time.assert_called_once()


@_skip_no_deps
@pytest.mark.asyncio
async def test_dim_heartbeat_stamped_even_under_dnd(reset_shared):
    """DND skips the scene fan-out but the heartbeat must still advance."""
    shared = reset_shared
    shared.DND_ACTIVE = True
    ha = MagicMock()
    ha.call_service = AsyncMock()

    from orchestrator import jobs_winddown

    with (
        patch.object(shared, "WIND_DOWN_SCENE", "scene.bedroom_dimmed"),
        patch.object(shared, "ha_client", ha),
        patch.object(jobs_winddown, "WIND_DOWN_DIM_LAST_RUN") as dim_hb,
    ):
        await jobs_winddown.wind_down_dim()

    ha.call_service.assert_not_awaited()
    dim_hb.set_to_current_time.assert_called_once()


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
# sleep_mode stamps sleep_started_at (goodnight-intent only)
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_sleep_mode_indefinite_on_attempts_stamp(reset_shared):
    from orchestrator import tool_handlers

    with (
        patch("orchestrator.tool_handlers._stamp_sleep_started") as stamp,
        patch("orchestrator.state_store.set_notification_flag"),
    ):
        await tool_handlers._reg_sleep_mode({"action": "on"})

    stamp.assert_called_once()


@_skip_no_deps
@pytest.mark.asyncio
async def test_sleep_mode_timed_mute_does_not_stamp(reset_shared):
    """'Be quiet for 2 hours' is a meeting/guests mute, not a goodnight."""
    from orchestrator import shared as _shared
    from orchestrator import tool_handlers

    with (
        patch("orchestrator.tool_handlers._stamp_sleep_started") as stamp,
        patch("orchestrator.state_store.set_notification_flag"),
        patch.object(_shared, "scheduler", MagicMock()),
    ):
        await tool_handlers._reg_sleep_mode({"action": "on", "duration_hours": 2})

    stamp.assert_not_called()


@_skip_no_deps
@pytest.mark.asyncio
async def test_sleep_mode_off_does_not_stamp(reset_shared):
    from orchestrator import tool_handlers

    with (
        patch("orchestrator.tool_handlers._stamp_sleep_started") as stamp,
        patch("orchestrator.state_store.clear_notification_flag"),
    ):
        await tool_handlers._reg_sleep_mode({"action": "off"})

    stamp.assert_not_called()


@_skip_no_deps
def test_stamp_only_in_bedtime_window():
    """The stamp helper only fires between 20:00 and 05:00 — an indefinite
    afternoon mute (nap, quiet time) is not a goodnight."""
    from orchestrator import tool_handlers

    with patch("orchestrator.state_store.set_app_state") as set_state:
        assert tool_handlers._stamp_sleep_started(now=datetime(2026, 7, 6, 22, 30)) is True
        assert tool_handlers._stamp_sleep_started(now=datetime(2026, 7, 7, 1, 0)) is True
        assert tool_handlers._stamp_sleep_started(now=datetime(2026, 7, 6, 15, 0)) is False
        assert tool_handlers._stamp_sleep_started(now=datetime(2026, 7, 6, 5, 0)) is False

    assert set_state.call_count == 2
    assert all(c.args[0] == "sleep_started_at" for c in set_state.call_args_list)


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
