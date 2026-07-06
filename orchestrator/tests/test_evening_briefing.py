"""
Tests for jobs_calendar.evening_briefing() — the evening shutdown ritual —
and the morning_briefing() pickup of the parked item.

Covers:
- Tomorrow-only event selection (today's events excluded) from the phone cache,
  and the Google fallback path.
- Parking one unfinished thing: active focus task wins over backlog; top open
  backlog task otherwise; persisted via state_store.set_app_state.
- Evening meds line (confirmed vs. unconfirmed).
- DND: parks silently, no announce, no Telegram mirror.
- Morning pickup: parked item announced, cleared only on successful announce.

Runs inside the brain-orchestrator container (full deps available). Skips
gracefully when orchestrator dependencies are unavailable locally.
"""

import logging
from datetime import datetime, timedelta
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
def reset_shared():
    """Reset the shared state evening_briefing reads: phone cache, DND, focus."""
    from orchestrator import shared

    orig_events = shared._phone_calendar_events
    orig_time = shared._phone_calendar_sync_time
    orig_dnd = shared.DND_ACTIVE
    orig_focus = shared.current_focus_session.to_dict()
    shared._phone_calendar_events = []
    shared._phone_calendar_sync_time = 0.0
    shared.DND_ACTIVE = False
    shared.current_focus_session.reset()
    yield shared
    shared._phone_calendar_events = orig_events
    shared._phone_calendar_sync_time = orig_time
    shared.DND_ACTIVE = orig_dnd
    shared.current_focus_session.update(orig_focus)


@pytest.fixture
def fresh_phone_sync_time():
    import time as _time

    return _time.time()


@pytest.fixture
def patch_evening_deps():
    """Patch the side-effecting collaborators of evening_briefing.

    Returns (mock_client, patches: list) — enter all patches via
    contextlib.ExitStack or `with` chaining in the test.
    """

    def _patch(
        *,
        google_configured=True,
        google_success=True,
        google_events=None,
        meds_status=None,
        open_tasks=None,
        announce_result=None,
    ):
        mock_client = MagicMock()
        mock_client.is_configured = google_configured
        response = MagicMock()
        response.success = google_success
        response.events = google_events or []
        mock_client.list_events = AsyncMock(return_value=response)

        patches = {
            "client": patch("orchestrator.jobs_calendar.get_calendar_client", return_value=mock_client),
            "voice": patch(
                "orchestrator.jobs_calendar._announce_voice",
                new_callable=AsyncMock,
                return_value=announce_result or {"success": True},
            ),
            "travel": patch("orchestrator.jobs_calendar.get_travel_time", new_callable=AsyncMock, return_value=None),
            "meds": patch("orchestrator.selfcare_manager.evening_meds_status", return_value=meds_status),
            "telegram": patch("orchestrator.telegram_bot.fire_system_message"),
            "list_tasks": patch("orchestrator.state_store.list_tasks", return_value=open_tasks or []),
            "set_state": patch("orchestrator.state_store.set_app_state"),
        }
        return mock_client, patches

    return _patch


def _tomorrow_phone_event(title, hour=9, minute=0, all_day=False, location=""):
    from zoneinfo import ZoneInfo

    from orchestrator.shared import TIMEZONE

    day = datetime.now(ZoneInfo(TIMEZONE)).date() + timedelta(days=1)
    start_str = datetime(day.year, day.month, day.day, hour, minute).strftime("%b %d, %Y %I:%M %p")
    return {"title": title, "start": start_str, "all_day": all_day, "location": location}


def _today_phone_event(title, hour=10):
    from zoneinfo import ZoneInfo

    from orchestrator.shared import TIMEZONE

    day = datetime.now(ZoneInfo(TIMEZONE)).date()
    start_str = datetime(day.year, day.month, day.day, hour, 0).strftime("%b %d, %Y %I:%M %p")
    return {"title": title, "start": start_str, "all_day": False, "location": ""}


def _google_event(title, day_offset, hour=9, all_day=False, location=""):
    from zoneinfo import ZoneInfo

    from orchestrator.shared import TIMEZONE

    tz = ZoneInfo(TIMEZONE)
    day = datetime.now(tz).date() + timedelta(days=day_offset)
    ev = MagicMock()
    ev.title = title
    ev.start = datetime(day.year, day.month, day.day, hour, 0, tzinfo=tz)
    ev.all_day = all_day
    ev.location = location
    return ev


async def _run_evening(patches):
    from contextlib import ExitStack

    from orchestrator import jobs_calendar

    with ExitStack() as stack:
        mocks = {name: stack.enter_context(p) for name, p in patches.items()}
        await jobs_calendar.evening_briefing()
    return mocks


def _spoken(mocks):
    assert mocks["voice"].await_count == 1, "expected exactly one announce"
    return mocks["voice"].await_args.args[0]


# ---------------------------------------------------------------------------
# Tomorrow-event selection
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_phone_tomorrow_event_selected_today_excluded(reset_shared, fresh_phone_sync_time, patch_evening_deps):
    """Phone cache holds today's and tomorrow's events -> only tomorrow's first
    timed event is spoken; Google is not consulted."""
    shared = reset_shared
    shared._phone_calendar_events = [
        _today_phone_event("Standup today", hour=10),
        _tomorrow_phone_event("Dentist", hour=14),
        _tomorrow_phone_event("Gym", hour=8, minute=30),
    ]
    shared._phone_calendar_sync_time = fresh_phone_sync_time

    mock_client, patches = patch_evening_deps()
    mocks = await _run_evening(patches)

    text = _spoken(mocks)
    assert "Gym" in text and "8:30" in text, f"expected tomorrow's first timed event, got: {text}"
    assert "Standup today" not in text
    assert "Plus 1 more event" in text
    mock_client.list_events.assert_not_awaited()


@_skip_no_deps
@pytest.mark.asyncio
async def test_google_fallback_filters_to_tomorrow(reset_shared, patch_evening_deps):
    """Stale phone cache -> Google fallback, filtered to tomorrow's date."""
    mock_client, patches = patch_evening_deps(
        google_events=[
            _google_event("Today thing", 0, hour=11),
            _google_event("Tomorrow thing", 1, hour=9),
        ]
    )
    mocks = await _run_evening(patches)

    mock_client.list_events.assert_awaited_once()
    text = _spoken(mocks)
    assert "Tomorrow thing" in text
    assert "Today thing" not in text


@_skip_no_deps
@pytest.mark.asyncio
async def test_clear_tomorrow_still_delivers(reset_shared, patch_evening_deps):
    """No events at all -> 'nothing on the calendar' but the ritual still runs."""
    _, patches = patch_evening_deps(google_events=[])
    mocks = await _run_evening(patches)
    text = _spoken(mocks)
    assert "Nothing on the calendar tomorrow" in text


# ---------------------------------------------------------------------------
# Parking one unfinished thing
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_parks_top_backlog_task(reset_shared, patch_evening_deps):
    """No focus session -> top open backlog task is parked and spoken."""
    _, patches = patch_evening_deps(open_tasks=[{"text": "email the landlord"}, {"text": "other"}])
    mocks = await _run_evening(patches)

    mocks["set_state"].assert_called_once_with("parked_item", "email the landlord")
    text = _spoken(mocks)
    assert "parking one thing" in text and "email the landlord" in text


@_skip_no_deps
@pytest.mark.asyncio
async def test_active_focus_task_wins_over_backlog(reset_shared, patch_evening_deps):
    """An active focus session's task is parked instead of the backlog top."""
    shared = reset_shared
    shared.current_focus_session["active"] = True
    shared.current_focus_session["task_description"] = "the OAuth flow"

    _, patches = patch_evening_deps(open_tasks=[{"text": "email the landlord"}])
    mocks = await _run_evening(patches)

    mocks["set_state"].assert_called_once_with("parked_item", "the OAuth flow")
    text = _spoken(mocks)
    assert "middle of the OAuth flow" in text


@_skip_no_deps
@pytest.mark.asyncio
async def test_nothing_to_park(reset_shared, patch_evening_deps):
    """Empty backlog and no focus -> explicit 'nothing left to park' line."""
    _, patches = patch_evening_deps(open_tasks=[])
    mocks = await _run_evening(patches)
    mocks["set_state"].assert_not_called()
    assert "Nothing left to park" in _spoken(mocks)


# ---------------------------------------------------------------------------
# Meds line
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_meds_unconfirmed_line(reset_shared, patch_evening_deps):
    _, patches = patch_evening_deps(meds_status={"names": ["Naltrexone"], "confirmed": False})
    mocks = await _run_evening(patches)
    assert "haven't logged" in _spoken(mocks)


@_skip_no_deps
@pytest.mark.asyncio
async def test_meds_confirmed_line(reset_shared, patch_evening_deps):
    _, patches = patch_evening_deps(meds_status={"names": ["Naltrexone"], "confirmed": True})
    mocks = await _run_evening(patches)
    assert "meds are logged" in _spoken(mocks)


@_skip_no_deps
@pytest.mark.asyncio
async def test_meds_none_says_nothing(reset_shared, patch_evening_deps):
    _, patches = patch_evening_deps(meds_status=None)
    mocks = await _run_evening(patches)
    text = _spoken(mocks)
    assert "meds" not in text.lower()


# ---------------------------------------------------------------------------
# DND: park silently
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_dnd_parks_silently(reset_shared, patch_evening_deps, caplog):
    """DND active -> the parked item is persisted but nothing is announced or
    mirrored to Telegram."""
    shared = reset_shared
    shared.DND_ACTIVE = True

    _, patches = patch_evening_deps(open_tasks=[{"text": "email the landlord"}])
    with caplog.at_level(logging.INFO, logger="orchestrator.jobs_calendar"):
        mocks = await _run_evening(patches)

    mocks["set_state"].assert_called_once_with("parked_item", "email the landlord")
    mocks["voice"].assert_not_awaited()
    mocks["telegram"].assert_not_called()
    assert any("DND active" in r.getMessage() for r in caplog.records)


@_skip_no_deps
@pytest.mark.asyncio
async def test_active_routine_parks_silently(reset_shared, patch_evening_deps, caplog):
    """A guided routine walkthrough mid-flight -> park, but don't talk over it."""
    from orchestrator import routine_manager

    _, patches = patch_evening_deps(open_tasks=[{"text": "email the landlord"}])
    with (
        patch.object(routine_manager, "_active_session", MagicMock()),
        caplog.at_level(logging.INFO, logger="orchestrator.jobs_calendar"),
    ):
        mocks = await _run_evening(patches)

    mocks["set_state"].assert_called_once_with("parked_item", "email the landlord")
    mocks["voice"].assert_not_awaited()
    mocks["telegram"].assert_not_called()
    assert any("Routine session active" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Telegram mirror
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_telegram_mirror_fires(reset_shared, patch_evening_deps):
    _, patches = patch_evening_deps()
    mocks = await _run_evening(patches)
    mocks["telegram"].assert_called_once()
    assert mocks["telegram"].call_args.args[0].startswith("🌙 ")


# ---------------------------------------------------------------------------
# Morning pickup of the parked item
# ---------------------------------------------------------------------------


def _parked_entry(value, hours_ago=10):
    return {"value": value, "updated_at": (datetime.now() - timedelta(hours=hours_ago)).isoformat()}


def _morning_patches(*, parked_entry, announce_result):
    mock_client = MagicMock()
    mock_client.is_configured = False
    return {
        "client": patch("orchestrator.jobs_calendar.get_calendar_client", return_value=mock_client),
        "voice": patch(
            "orchestrator.jobs_calendar._announce_voice",
            new_callable=AsyncMock,
            return_value=announce_result,
        ),
        "weather": patch("orchestrator.jobs_calendar._get_weather_forecast", new_callable=AsyncMock, return_value=None),
        "reminders": patch("orchestrator.jobs_calendar.list_pending_reminders", return_value=[]),
        # morning_briefing reads two app_state keys (sleep_started_at for the
        # short-night check, parked_item for the pickup) — key the mock off
        # the argument so the sleep check sees "no stamp".
        "get_state": patch(
            "orchestrator.state_store.get_app_state_entry",
            side_effect=lambda key: parked_entry if key == "parked_item" else None,
        ),
        "delete_state": patch("orchestrator.state_store.delete_app_state"),
        "outcomes": patch("orchestrator.state_store.get_recent_reminder_outcomes", return_value=[]),
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
async def test_morning_offers_parked_item_and_clears_on_success(reset_shared):
    mocks = await _run_morning(
        _morning_patches(parked_entry=_parked_entry("the OAuth flow"), announce_result={"success": True})
    )

    text = mocks["voice"].await_args.args[0]
    assert "Last night you parked: the OAuth flow" in text
    mocks["delete_state"].assert_called_once_with("parked_item")


@_skip_no_deps
@pytest.mark.asyncio
async def test_morning_keeps_parked_item_when_announce_fails(reset_shared):
    mocks = await _run_morning(
        _morning_patches(parked_entry=_parked_entry("the OAuth flow"), announce_result={"success": False})
    )
    mocks["delete_state"].assert_not_called()


@_skip_no_deps
@pytest.mark.asyncio
async def test_morning_keeps_parked_item_when_announce_suppressed(reset_shared):
    """A suppressed announce (DND / active voice session) reports success=True
    but nothing was spoken — the parked item must survive to the next run."""
    mocks = await _run_morning(
        _morning_patches(
            parked_entry=_parked_entry("the OAuth flow"),
            announce_result={"success": True, "suppressed": True},
        )
    )
    mocks["delete_state"].assert_not_called()


@_skip_no_deps
@pytest.mark.asyncio
async def test_morning_drops_stale_parked_item(reset_shared):
    """A parked item older than 36h (evening job disabled/failing) is dropped,
    not announced as 'last night'."""
    mocks = await _run_morning(
        _morning_patches(
            parked_entry=_parked_entry("the OAuth flow", hours_ago=72),
            announce_result={"success": True},
        )
    )
    text = mocks["voice"].await_args.args[0]
    assert "parked" not in text.lower()
    mocks["delete_state"].assert_called_once_with("parked_item")


@_skip_no_deps
@pytest.mark.asyncio
async def test_morning_no_parked_item_no_mention(reset_shared):
    mocks = await _run_morning(_morning_patches(parked_entry=None, announce_result={"success": True}))
    text = mocks["voice"].await_args.args[0]
    assert "parked" not in text.lower()
    mocks["delete_state"].assert_not_called()
