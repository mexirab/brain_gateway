"""
Tests for jobs_calendar.evening_briefing() — the evening shutdown ritual —
and the morning_briefing() pickup of the parked item.

Covers:
- Tomorrow-only event selection (today's events excluded) from the phone cache,
  and the Google fallback path.
- Parking one unfinished thing: active focus task wins over backlog; top open
  backlog task otherwise; persisted via state_store.set_app_state.
- Evening meds line (confirmed vs. unconfirmed).
- DND: parks silently, no announce, no mirror.
- Active routine: parks, no announce, digest mirrored (inbox card + Telegram).

The morning half (parked item announced, cleared only once heard) is in
test_morning_briefing.
"""

import logging
import sys
import time
from contextlib import ExitStack
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from orchestrator import inbox, jobs_calendar, routine_manager, shared
from orchestrator.config import settings
from orchestrator.metrics import EVENING_BRIEFING_OUTCOME_TOTAL
from orchestrator.shared import TIMEZONE
from orchestrator.tests._helpers import metric_value
from orchestrator.tests.calendar_fakes import fake_client, make_event, patched_calendar

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_shared(delivery_reset_shared, tmp_db, monkeypatch):
    """Phone cache empty, DND off, no focus session; the inbox mirror goes to
    tmp_db with Web Push off."""
    monkeypatch.setattr(settings, "webpush_enabled", False, raising=False)
    return delivery_reset_shared


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
        mock_client = fake_client(configured=google_configured, success=google_success, events=google_events)

        patches = {
            "client": patched_calendar(google=mock_client),
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
            # The run heartbeat also persists via set_app_state; keep it out of
            # the parked-item assertions.
            "heartbeat": patch("orchestrator.jobs_calendar.job_heartbeat.stamp"),
        }
        return mock_client, patches

    return _patch


def _tomorrow_phone_event(title, hour=9, minute=0, all_day=False, location=""):
    day = datetime.now(ZoneInfo(TIMEZONE)).date() + timedelta(days=1)
    start_str = datetime(day.year, day.month, day.day, hour, minute).strftime("%b %d, %Y %I:%M %p")
    return {"title": title, "start": start_str, "all_day": all_day, "location": location}


def _today_phone_event(title, hour=10):
    day = datetime.now(ZoneInfo(TIMEZONE)).date()
    start_str = datetime(day.year, day.month, day.day, hour, 0).strftime("%b %d, %Y %I:%M %p")
    return {"title": title, "start": start_str, "all_day": False, "location": ""}


def _google_event(title, day_offset, hour=9, all_day=False, location=""):
    tz = ZoneInfo(TIMEZONE)
    day = datetime.now(tz).date() + timedelta(days=day_offset)
    return make_event(
        title, datetime(day.year, day.month, day.day, hour, 0, tzinfo=tz), all_day=all_day, location=location
    )


async def _run_evening(patches):
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


async def test_phone_tomorrow_event_selected_today_excluded(reset_shared, patch_evening_deps):
    """Phone cache holds today's and tomorrow's events -> only tomorrow's first
    timed event is spoken."""
    shared = reset_shared
    shared._phone_calendar_events = [
        _today_phone_event("Standup today", hour=10),
        _tomorrow_phone_event("Dentist", hour=14),
        _tomorrow_phone_event("Gym", hour=8, minute=30),
    ]
    shared._phone_calendar_sync_time = time.time()

    mock_client, patches = patch_evening_deps(google_configured=False)
    mocks = await _run_evening(patches)

    text = _spoken(mocks)
    assert "Gym" in text and "8:30" in text, f"expected tomorrow's first timed event, got: {text}"
    assert "Standup today" not in text
    assert "Plus 1 more event" in text


async def test_google_fallback_filters_to_tomorrow(reset_shared, patch_evening_deps):
    """No phone push -> Google answers; only tomorrow's events are used."""
    mock_client, patches = patch_evening_deps(
        google_events=[
            _google_event("Today thing", 0, hour=11),
            _google_event("Tomorrow thing", 1, hour=9),
        ]
    )
    mocks = await _run_evening(patches)

    mock_client.list_window.assert_awaited_once()
    text = _spoken(mocks)
    assert "Tomorrow thing" in text
    assert "Today thing" not in text


async def test_tonights_late_event_is_not_tomorrows_first_thing(reset_shared, patch_evening_deps):
    """An event tonight that runs past midnight overlaps tomorrow but must not be
    announced as tomorrow's first thing (or get a leave-by in the past)."""

    tz = ZoneInfo(TIMEZONE)
    today = datetime.now(tz).date()
    late = make_event("Late show", datetime(today.year, today.month, today.day, 23, 0, tzinfo=tz), minutes=120)
    _, patches = patch_evening_deps(google_events=[late, _google_event("Dentist", 1, hour=10)])
    text = _spoken(await _run_evening(patches))
    assert "Dentist" in text and "Late show" not in text


async def test_unreachable_calendar_is_not_a_clear_tomorrow(reset_shared, patch_evening_deps):
    _, patches = patch_evening_deps(google_success=False)
    text = _spoken(await _run_evening(patches))
    assert "Nothing on the calendar tomorrow" not in text
    assert "I couldn't reach your Google calendar, so I can't tell you what's on tomorrow" in text


async def test_evening_never_sends_work_locations_to_maps(reset_shared, patch_evening_deps, monkeypatch):
    """Tomorrow's first thing is a work meeting (pushed from the iPhone, source
    exchange): its invite location must not go to Google Maps."""

    monkeypatch.setattr(shared, "GOOGLE_MAPS_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(shared, "HOME_ADDRESS", "1 Example St", raising=False)
    reset_shared._phone_calendar_events = [_tomorrow_phone_event("Meeting", hour=10, location="Conference Room 4")]
    reset_shared._phone_calendar_sync_time = time.time()
    _, patches = patch_evening_deps(google_configured=False)
    mocks = await _run_evening(patches)
    assert "Meeting" in _spoken(mocks)
    mocks["travel"].assert_not_awaited()


async def test_evening_personal_location_still_gets_travel_time(reset_shared, patch_evening_deps, monkeypatch):
    monkeypatch.setattr(shared, "GOOGLE_MAPS_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(shared, "HOME_ADDRESS", "1 Example St", raising=False)
    _, patches = patch_evening_deps(google_events=[_google_event("Dentist", 1, hour=10, location="2 Example Ave")])
    mocks = await _run_evening(patches)
    mocks["travel"].assert_awaited_once()


async def test_clear_tomorrow_still_delivers(reset_shared, patch_evening_deps):
    """No events at all -> 'nothing on the calendar' but the ritual still runs."""
    _, patches = patch_evening_deps(google_events=[])
    mocks = await _run_evening(patches)
    text = _spoken(mocks)
    assert "Nothing on the calendar tomorrow" in text


# ---------------------------------------------------------------------------
# Parking one unfinished thing
# ---------------------------------------------------------------------------


async def test_parks_top_backlog_task(reset_shared, patch_evening_deps):
    """No focus session -> top open backlog task is parked and spoken."""
    _, patches = patch_evening_deps(open_tasks=[{"text": "email the landlord"}, {"text": "other"}])
    mocks = await _run_evening(patches)

    mocks["set_state"].assert_called_once_with("parked_item", "email the landlord")
    text = _spoken(mocks)
    assert "parking one thing" in text and "email the landlord" in text


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


async def test_nothing_to_park(reset_shared, patch_evening_deps):
    """Empty backlog and no focus -> explicit 'nothing left to park' line."""
    _, patches = patch_evening_deps(open_tasks=[])
    mocks = await _run_evening(patches)
    mocks["set_state"].assert_not_called()
    assert "Nothing left to park" in _spoken(mocks)


# ---------------------------------------------------------------------------
# Meds line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "meds_status,phrase,present",
    [
        pytest.param({"names": ["Naltrexone"], "confirmed": False}, "haven't logged", True, id="unconfirmed"),
        pytest.param({"names": ["Naltrexone"], "confirmed": True}, "meds are logged", True, id="confirmed"),
        pytest.param(None, "meds", False, id="none-says-nothing"),
    ],
)
async def test_meds_line(reset_shared, patch_evening_deps, meds_status, phrase, present):
    _, patches = patch_evening_deps(meds_status=meds_status)
    text = _spoken(await _run_evening(patches))
    assert (phrase in text.lower()) is present


# ---------------------------------------------------------------------------
# DND, active routine, outcome metric
# ---------------------------------------------------------------------------


def _outcome(label):
    return metric_value(EVENING_BRIEFING_OUTCOME_TOTAL, outcome=label)


_OUTCOMES = ("spoken", "skipped_routine", "skipped_dnd", "suppressed", "failed", "error")


def _all_outcomes():
    return {label: _outcome(label) for label in _OUTCOMES}


def _assert_counted_once(before, label):
    after = _all_outcomes()
    deltas = {k: after[k] - before[k] for k in _OUTCOMES}
    assert deltas == {k: (1 if k == label else 0) for k in _OUTCOMES}


async def test_dnd_parks_silently(reset_shared, patch_evening_deps, caplog):
    """DND active -> the parked item is persisted, but nothing is announced,
    no inbox card is posted and nothing is mirrored to Telegram. Counted as
    skipped_dnd only."""
    reset_shared.DND_ACTIVE = True
    before = _all_outcomes()

    _, patches = patch_evening_deps(open_tasks=[{"text": "email the landlord"}])
    with caplog.at_level(logging.INFO, logger="orchestrator.jobs_calendar"):
        mocks = await _run_evening(patches)

    mocks["set_state"].assert_called_once_with("parked_item", "email the landlord")
    mocks["voice"].assert_not_awaited()
    mocks["telegram"].assert_not_called()
    assert inbox.list_pending() == []
    _assert_counted_once(before, "skipped_dnd")
    assert any("DND active" in r.getMessage() for r in caplog.records)


async def test_active_routine_skips_announce_but_mirrors(reset_shared, patch_evening_deps, caplog):
    """A guided routine walkthrough mid-flight -> park, don't talk over it, but
    still mirror the digest off the speakers (the routine never speaks the
    parked item or the leave-by time). Counted as skipped_routine."""

    before = _all_outcomes()
    _, patches = patch_evening_deps(open_tasks=[{"text": "email the landlord"}])
    with (
        patch.object(routine_manager, "_active_session", MagicMock()),
        caplog.at_level(logging.INFO, logger="orchestrator.jobs_calendar"),
    ):
        mocks = await _run_evening(patches)

    mocks["set_state"].assert_called_once_with("parked_item", "email the landlord")
    mocks["voice"].assert_not_awaited()
    cards = inbox.list_pending()
    assert [(c["kind"], c["ref"]) for c in cards] == [("message", "evening_briefing")]
    assert "email the landlord" in cards[0]["text"]
    mocks["telegram"].assert_called_once_with(f"🌙 {cards[0]['text']}")
    _assert_counted_once(before, "skipped_routine")
    assert any("Routine session active" in r.getMessage() for r in caplog.records)


async def test_dnd_wins_over_active_routine(reset_shared, patch_evening_deps):
    """Goodnight said AND a routine still mid-flight -> DND's silent park wins:
    no announce, no inbox card, no Telegram, counted as skipped_dnd only."""

    reset_shared.DND_ACTIVE = True
    before = _all_outcomes()
    _, patches = patch_evening_deps(open_tasks=[{"text": "email the landlord"}])
    with patch.object(routine_manager, "_active_session", MagicMock()):
        mocks = await _run_evening(patches)

    mocks["set_state"].assert_called_once_with("parked_item", "email the landlord")
    mocks["voice"].assert_not_awaited()
    assert inbox.list_pending() == []
    mocks["telegram"].assert_not_called()
    _assert_counted_once(before, "skipped_dnd")


async def test_routine_mirror_inbox_failure_still_telegrams_and_counts_once(reset_shared, patch_evening_deps):
    """On the routine path the mirror is the ONLY delivery: a broken inbox must
    not stop the Telegram copy, and must not also count as an error."""

    before = _all_outcomes()
    _, patches = patch_evening_deps()
    with (
        patch.object(routine_manager, "_active_session", MagicMock()),
        patch.object(inbox, "post_message", AsyncMock(side_effect=RuntimeError("db locked"))),
    ):
        mocks = await _run_evening(patches)

    mocks["voice"].assert_not_awaited()
    mocks["telegram"].assert_called_once()
    assert mocks["telegram"].call_args.args[0].startswith("🌙 ")
    _assert_counted_once(before, "skipped_routine")


@pytest.mark.parametrize(
    "result,label",
    [
        ({"success": True}, "spoken"),
        ({"success": True, "suppressed": True, "reason": "voice_session"}, "suppressed"),
        ({"success": False, "error": "tts down"}, "failed"),
    ],
)
async def test_announce_outcome_counted(reset_shared, patch_evening_deps, result, label):
    before = _all_outcomes()
    _, patches = patch_evening_deps(announce_result=result)
    await _run_evening(patches)
    _assert_counted_once(before, label)


async def test_routine_check_failure_still_speaks(reset_shared, patch_evening_deps, caplog):
    """A broken routine check fails open (speaks) and logs only the type name."""

    class _Broken:
        @property
        def _active_session(self):
            raise RuntimeError("secret detail")

    before = _all_outcomes()
    _, patches = patch_evening_deps()
    with (
        patch.dict(sys.modules, {"orchestrator.routine_manager": _Broken()}),
        patch("orchestrator.routine_manager", _Broken(), create=True),
        caplog.at_level(logging.WARNING, logger="orchestrator.jobs_calendar"),
    ):
        mocks = await _run_evening(patches)

    mocks["voice"].assert_awaited_once()
    _assert_counted_once(before, "spoken")
    warn = [r.getMessage() for r in caplog.records if "Routine check failed" in r.getMessage()]
    assert warn == ["[EVENING_BRIEFING] Routine check failed: RuntimeError"]


async def test_crash_counted_as_error(reset_shared, patch_evening_deps):
    before = _all_outcomes()
    _, patches = patch_evening_deps()
    with patch("orchestrator.jobs_calendar.get_tomorrow_events", AsyncMock(side_effect=ValueError("x"))):
        await _run_evening(patches)
    _assert_counted_once(before, "error")


# ---------------------------------------------------------------------------
# Inbox card + Telegram mirror
# ---------------------------------------------------------------------------


async def test_evening_posts_inbox_card_matching_telegram(reset_shared, patch_evening_deps):
    """The card carries the spoken text without the emoji; Telegram keeps the
    🌙 prefix. One card, kind message, ref evening_briefing."""

    _, patches = patch_evening_deps(open_tasks=[{"text": "sort the mail"}])
    mocks = await _run_evening(patches)

    spoken = _spoken(mocks)
    cards = inbox.list_pending()
    assert len(cards) == 1
    card = cards[0]
    assert card["kind"] == "message" and card["ref"] == "evening_briefing"
    assert card["text"] == spoken
    assert not card["text"].startswith("🌙")
    assert "sort the mail" in card["text"]  # parked item reaches the card
    mocks["telegram"].assert_called_once_with(f"🌙 {spoken}")


async def test_evening_card_posted_even_when_tts_fails(reset_shared, patch_evening_deps):
    _, patches = patch_evening_deps(announce_result={"success": False, "error": "tts down"})
    await _run_evening(patches)
    assert [c["ref"] for c in inbox.list_pending()] == ["evening_briefing"]


async def test_second_evening_supersedes_first_card(reset_shared, patch_evening_deps):
    _, patches = patch_evening_deps()
    await _run_evening(patches)
    first = inbox.list_pending()[0]["id"]
    _, patches = patch_evening_deps()
    await _run_evening(patches)
    pending = inbox.list_pending()
    assert len(pending) == 1 and pending[0]["id"] != first
    assert inbox.get_item(first)["resolution"] == "superseded"
