"""
Tests for the sleep wind-down ladder (jobs_winddown) and the goodnight stamp
its morning half reads.

Covers:
- T-60 lights rung: scene fan-out, no-scene no-op, DND skip, per-scene error
  isolation, and the dim heartbeat stamped on every path (early returns too).
- T-30 nudge: screens-away text + tomorrow anchor, no-events wording, DND and
  active-routine silent skips, calendar-failure resilience.
- sleep_mode("on") stamps app_state.sleep_started_at (goodnight intent only).

The morning short-night adaptation that reads the stamp is in
test_morning_briefing.
"""

import logging
from contextlib import ExitStack
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from orchestrator import jobs_winddown, routine_manager, tool_handlers
from orchestrator.shared import TIMEZONE


@pytest.fixture
def reset_shared(delivery_reset_shared):
    """DND off, empty phone cache, no focus session (restored afterwards)."""
    return delivery_reset_shared


def _ha_result(success=True, message=""):
    r = MagicMock()
    r.success = success
    r.message = message
    return r


def _tomorrow_event(title, hour=9, minute=0, all_day=False):
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
#
# The heartbeat is stamped at the TOP of wind_down_dim, before every early
# return, so it proves the job FIRED independent of whether it did any work.
# Without that, a scheduler that drops only the dim job leaves the Sleep
# Wind-Down panels green/empty (the scene counter stays silent on the no-op
# nights). Every dim test below therefore also pins the stamp, including the
# no-scene and DND early-return paths.
# ---------------------------------------------------------------------------


async def _run_dim(shared, scene, *, side_effect=None, result=None):
    """Run wind_down_dim with WIND_DOWN_SCENE=scene; returns (ha, heartbeat)."""
    ha = MagicMock()
    ha.call_service = AsyncMock(return_value=result, side_effect=side_effect)
    with (
        patch.object(shared, "WIND_DOWN_SCENE", scene),
        patch.object(shared, "ha_client", ha),
        patch.object(jobs_winddown, "WIND_DOWN_DIM_LAST_RUN") as dim_hb,
    ):
        await jobs_winddown.wind_down_dim()
    return ha, dim_hb


async def test_dim_activates_each_configured_scene(reset_shared):
    ha, dim_hb = await _run_dim(reset_shared, "scene.living_room_relax, scene.bedroom_dimmed", result=_ha_result())

    assert ha.call_service.await_count == 2
    called = [c.args for c in ha.call_service.await_args_list]
    assert ("scene.living_room_relax", "turn_on") in called
    assert ("scene.bedroom_dimmed", "turn_on") in called
    dim_hb.set.assert_called_once()


async def test_dim_noop_without_scene(reset_shared, caplog):
    """No WIND_DOWN_SCENE -> no scene attempted, but the job still fired."""
    with caplog.at_level(logging.INFO, logger="orchestrator.jobs_winddown"):
        ha, dim_hb = await _run_dim(reset_shared, "")

    ha.call_service.assert_not_awaited()
    assert any("No WIND_DOWN_SCENE configured" in r.getMessage() for r in caplog.records)
    dim_hb.set.assert_called_once()


async def test_dim_skipped_under_dnd(reset_shared):
    """scene.turn_on can raise lights that are off — never fire under DND.
    The heartbeat must still advance."""
    reset_shared.DND_ACTIVE = True
    ha, dim_hb = await _run_dim(reset_shared, "scene.bedroom_dimmed")

    ha.call_service.assert_not_awaited()
    dim_hb.set.assert_called_once()


async def test_dim_ignores_non_scene_entities(reset_shared, caplog):
    """A .env typo (switch.garage_door) must not get a nightly turn_on."""
    with caplog.at_level(logging.WARNING, logger="orchestrator.jobs_winddown"):
        ha, _ = await _run_dim(reset_shared, "scene.bedroom_dimmed,switch.garage_door", result=_ha_result())

    assert ha.call_service.await_count == 1
    assert ha.call_service.await_args.args == ("scene.bedroom_dimmed", "turn_on")
    assert any("Ignoring non-scene entry" in r.getMessage() for r in caplog.records)


async def test_dim_scene_error_does_not_stop_remaining_scenes(reset_shared):
    ha, _ = await _run_dim(reset_shared, "scene.a,scene.b", side_effect=[RuntimeError("HA down"), _ha_result()])

    assert ha.call_service.await_count == 2


# ---------------------------------------------------------------------------
# T-30: screens-away nudge
# ---------------------------------------------------------------------------


def _nudge_patches(*, events=None, announce_result=None, tomorrow_error=None):
    tomorrow = AsyncMock(return_value=(events or [], "phone", ""))
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
    with ExitStack() as stack:
        mocks = {name: stack.enter_context(p) for name, p in patches.items()}
        await jobs_winddown.wind_down_nudge()
    return mocks


async def test_nudge_includes_tomorrow_anchor(reset_shared):
    mocks = await _run_nudge(_nudge_patches(events=[_tomorrow_event("Dentist", hour=8, minute=30)]))

    text = mocks["voice"].await_args.args[0]
    assert "Screens away" in text
    assert "Dentist" in text and "8:30" in text
    assert "wind down" in text.lower()


@pytest.mark.parametrize(
    "events",
    [pytest.param([], id="no-events"), pytest.param([_tomorrow_event("Trash day", all_day=True)], id="all-day-only")],
)
async def test_nudge_without_timed_event_says_nothing_early(reset_shared, events):
    mocks = await _run_nudge(_nudge_patches(events=events))
    assert "Nothing early tomorrow" in mocks["voice"].await_args.args[0]


async def test_nudge_skipped_under_dnd(reset_shared):
    reset_shared.DND_ACTIVE = True
    mocks = await _run_nudge(_nudge_patches())
    mocks["voice"].assert_not_awaited()


async def test_nudge_skipped_during_routine(reset_shared):
    patches = _nudge_patches()
    with patch.object(routine_manager, "_active_session", MagicMock()):
        mocks = await _run_nudge(patches)
    mocks["voice"].assert_not_awaited()


async def test_nudge_survives_calendar_failure(reset_shared):
    """A calendar blowup drops the anchor line but the nudge still speaks."""
    mocks = await _run_nudge(_nudge_patches(tomorrow_error=RuntimeError("calendar down")))
    assert "Screens away" in mocks["voice"].await_args.args[0]


# ---------------------------------------------------------------------------
# sleep_mode stamps sleep_started_at (goodnight-intent only)
# ---------------------------------------------------------------------------


async def test_sleep_mode_indefinite_on_attempts_stamp(reset_shared):
    with (
        patch("orchestrator.tool_handlers._stamp_sleep_started") as stamp,
        patch("orchestrator.state_store.set_notification_flag"),
    ):
        await tool_handlers._reg_sleep_mode({"action": "on"})

    stamp.assert_called_once()


async def test_sleep_mode_timed_mute_does_not_stamp(reset_shared):
    """'Be quiet for 2 hours' is a meeting/guests mute, not a goodnight."""
    with (
        patch("orchestrator.tool_handlers._stamp_sleep_started") as stamp,
        patch("orchestrator.state_store.set_notification_flag"),
        patch.object(reset_shared, "scheduler", MagicMock()),
    ):
        await tool_handlers._reg_sleep_mode({"action": "on", "duration_hours": 2})

    stamp.assert_not_called()


async def test_sleep_mode_off_does_not_stamp(reset_shared):
    with (
        patch("orchestrator.tool_handlers._stamp_sleep_started") as stamp,
        patch("orchestrator.state_store.clear_notification_flag"),
    ):
        await tool_handlers._reg_sleep_mode({"action": "off"})

    stamp.assert_not_called()


def test_stamp_only_in_bedtime_window():
    """The stamp helper only fires between 20:00 and 05:00 — an indefinite
    afternoon mute (nap, quiet time) is not a goodnight."""
    with patch("orchestrator.state_store.set_app_state") as set_state:
        assert tool_handlers._stamp_sleep_started(now=datetime(2026, 7, 6, 22, 30)) is True
        assert tool_handlers._stamp_sleep_started(now=datetime(2026, 7, 7, 1, 0)) is True
        assert tool_handlers._stamp_sleep_started(now=datetime(2026, 7, 6, 15, 0)) is False
        assert tool_handlers._stamp_sleep_started(now=datetime(2026, 7, 6, 5, 0)) is False

    assert set_state.call_count == 2
    assert all(c.args[0] == "sleep_started_at" for c in set_state.call_args_list)
