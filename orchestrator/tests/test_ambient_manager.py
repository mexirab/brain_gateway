"""
Tests for ambient_manager.get_ambient_status() calendar handling, and
set_ambient_led.

The merge of calendar sources (personal CalDAV, work push, Google) is tested
in test_calendar_sources.py; here calendar_sources.upcoming is faked and we
check what ambient does with the answer:
- all-day and already-started events are excluded from "next event"
- schedule_density: <=15min => busy, <=60 => light, else clear
- a source answered with nothing => events_remaining=0, next=None, clear
- no source answered => schedule_density=unknown (never a false "clear")
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from orchestrator import ambient_manager, shared
from orchestrator.calendar_types import CalendarResponse
from orchestrator.tests.calendar_fakes import make_event

TZ = ZoneInfo("America/Chicago")
FIXED_NOW = datetime(2026, 4, 17, 10, 0, 0, tzinfo=TZ)


class _FakeDatetime:
    """Only ``datetime.now(tz)`` is patched; the rest is the real class."""

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return FIXED_NOW.replace(tzinfo=None)
        return FIXED_NOW.astimezone(tz)


@pytest.fixture
def patched_now(monkeypatch):
    monkeypatch.setattr(shared, "TIMEZONE", "America/Chicago", raising=False)
    with patch("orchestrator.ambient_manager.datetime", _FakeDatetime):
        yield FIXED_NOW


def _upcoming(*events, success=True, failed=()):
    return patch(
        "orchestrator.calendar_sources.upcoming",
        new_callable=AsyncMock,
        return_value=CalendarResponse(success=success, events=list(events), failed_sources=list(failed)),
    )


def _ev(title, minutes_from_now, all_day=False):
    return make_event(title, FIXED_NOW + timedelta(minutes=minutes_from_now), all_day=all_day)


async def _status():
    return await ambient_manager.get_ambient_status()


async def test_next_event_and_count(patched_now):
    with _upcoming(_ev("Standup", 30), _ev("Review", 240)):
        status = await _status()
    assert status["events_remaining"] == 2
    assert status["next_event"]["title"] == "Standup"
    assert status["next_event"]["minutes_away"] == 30
    assert status["schedule_density"] == "light"


async def test_all_day_and_already_started_excluded(patched_now):
    with _upcoming(_ev("Holiday", -600, all_day=True), _ev("In progress", -20), _ev("Later", 120)):
        status = await _status()
    assert status["events_remaining"] == 1
    assert status["next_event"]["title"] == "Later"
    assert status["schedule_density"] == "clear"


async def test_schedule_density_busy_when_next_within_15_min(patched_now):
    with _upcoming(_ev("Call", 10)):
        status = await _status()
    assert status["schedule_density"] == "busy"


async def test_no_events_but_a_source_answered_is_clear(patched_now):
    with _upcoming():
        status = await _status()
    assert status["events_remaining"] == 0
    assert status["next_event"] is None
    assert status["schedule_density"] == "clear"


async def test_partial_failure_still_reports_answering_sources(patched_now):
    with _upcoming(_ev("Standup", 30), failed=["exchange"]):
        status = await _status()
    assert status["events_remaining"] == 1


async def test_partial_failure_with_nothing_upcoming_is_unknown_not_clear(patched_now):
    with _upcoming(failed=["exchange"]):
        status = await _status()
    assert status["schedule_density"] == "unknown"


async def test_no_source_answered_is_unknown(patched_now):
    with _upcoming(success=False, failed=["caldav", "exchange"]):
        status = await _status()
    assert status["schedule_density"] == "unknown"
    assert "next_event" not in status


async def test_calendar_exception_is_unknown_not_crash(patched_now):
    with patch("orchestrator.calendar_sources.upcoming", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
        status = await _status()
    assert status["schedule_density"] == "unknown"


# ---------------------------------------------------------------------------
# set_ambient_led — shared.ha_client
# ---------------------------------------------------------------------------


@pytest.fixture
def led_ha(monkeypatch):
    client = SimpleNamespace(call_service=AsyncMock(return_value=SimpleNamespace(success=True, message="ok")))
    monkeypatch.setattr(shared, "ha_client", client)
    return client


async def test_set_ambient_led_calls_shared_ha_client(led_ha, monkeypatch):
    monkeypatch.setattr(shared, "AMBIENT_LED_ENTITY", "light.desk_led")
    await ambient_manager.set_ambient_led("red")
    led_ha.call_service.assert_awaited_once_with("light.desk_led", "turn_on", ambient_manager._LED_COLORS["red"])


async def test_set_ambient_led_unknown_color_falls_back_to_green(led_ha, monkeypatch):
    monkeypatch.setattr(shared, "AMBIENT_LED_ENTITY", "light.desk_led")
    await ambient_manager.set_ambient_led("chartreuse")
    led_ha.call_service.assert_awaited_once_with("light.desk_led", "turn_on", ambient_manager._LED_COLORS["green"])


@pytest.mark.parametrize("entity", ["", None])
async def test_set_ambient_led_noop_when_entity_unset(led_ha, monkeypatch, entity):
    monkeypatch.setattr(shared, "AMBIENT_LED_ENTITY", entity)
    await ambient_manager.set_ambient_led("red")
    led_ha.call_service.assert_not_awaited()


async def test_set_ambient_led_swallows_ha_errors(led_ha, monkeypatch):
    monkeypatch.setattr(shared, "AMBIENT_LED_ENTITY", "light.desk_led")
    led_ha.call_service.side_effect = RuntimeError("HA down")
    await ambient_manager.set_ambient_led("red")  # must not raise
