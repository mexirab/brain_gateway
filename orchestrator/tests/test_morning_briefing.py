"""
Tests for jobs_calendar.morning_briefing() — calendar sources are merged
(calendar_sources) and a broken source is named in the briefing instead of
reading as a clear day; the routine-collision gate and outcome metrics; the
pickup of the item the evening briefing parked; and the short-night
adaptation after a goodnight (sleep_mode stamp, see test_wind_down).
"""

import logging
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from orchestrator import inbox, jobs_calendar, routine_manager
from orchestrator.config import settings
from orchestrator.metrics import MORNING_BRIEFING_OUTCOME_TOTAL
from orchestrator.routine_manager import RoutineSession
from orchestrator.shared import TIMEZONE
from orchestrator.tests._helpers import metric_value
from orchestrator.tests.calendar_fakes import fake_client, make_event, no_calendar_patch, patched_calendar

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_yield_sleep():
    """morning_briefing yields with ``await asyncio.sleep(1)`` before its
    routine check. Replace the module's ``asyncio`` reference (not the global
    asyncio.sleep, which the event loop/pytest-asyncio also use) so no test
    pays the real second. Also pin the routine check's inputs to "nothing
    running" so the source-selection tests can't be skipped by a stray
    session/job left in module state."""
    fake_asyncio = SimpleNamespace(sleep=AsyncMock(return_value=None))
    sched = MagicMock()
    sched.get_jobs.return_value = []
    with (
        patch.object(jobs_calendar, "asyncio", fake_asyncio),
        patch.object(routine_manager, "_active_session", None),
        patch.object(jobs_calendar.shared, "scheduler", sched),
    ):
        yield fake_asyncio


@pytest.fixture
def patch_briefing_deps():
    """Patch the side-effecting collaborators of morning_briefing. The REAL
    calendar_sources merge runs (incl. the phone cache in shared); only the
    Google/CalDAV network clients are faked. Returns (google_client, patches)."""

    def _patch(*, google_configured=True, google_success=True, google_events=None):
        google = fake_client(configured=google_configured, success=google_success, events=google_events)
        return (
            google,
            patched_calendar(google=google),
            patch("orchestrator.jobs_calendar._announce_voice", new_callable=AsyncMock, return_value={"success": True}),
            patch("orchestrator.jobs_calendar._get_weather_forecast", new_callable=AsyncMock, return_value=None),
            patch("orchestrator.jobs_calendar.list_pending_reminders", return_value=[]),
        )

    return _patch


def _phone_event(title, start_str, all_day=False):
    return {"title": title, "start": start_str, "all_day": all_day}


def _today_at(hour, minute=0):
    tz = ZoneInfo(TIMEZONE)
    today = datetime.now(tz).date()
    return datetime(today.year, today.month, today.day, hour, minute, tzinfo=tz)


# ---------------------------------------------------------------------------
# Calendar sources are merged; a broken source is named, never read as "clear"
# ---------------------------------------------------------------------------


async def test_corrupt_work_push_is_named_and_google_events_still_spoken(
    delivery_phone_cache, patch_briefing_deps, caplog
):
    """The 2026-04-17 iPhone Shortcut bug (fresh push, every record empty):
    the push is excluded and NAMED in the briefing, and the other source's
    events are still announced."""
    shared = delivery_phone_cache
    shared._phone_calendar_events = [_phone_event("", ""), _phone_event("", "")]
    shared._phone_calendar_sync_time = time.time()

    google, p_cal, p_voice, p_weather, p_reminders = patch_briefing_deps(
        google_events=[make_event("Dentist", _today_at(15))]
    )
    with p_cal, p_voice as voice, p_weather, p_reminders, caplog.at_level(logging.WARNING):
        await jobs_calendar.morning_briefing()

    google.list_window.assert_awaited_once()
    text = voice.await_args.args[0]
    assert "Dentist" in text
    assert "couldn't reach your work calendar" in text
    assert any("corrupt" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


async def test_work_and_google_events_are_merged(delivery_phone_cache, patch_briefing_deps):
    shared = delivery_phone_cache
    start_str = _today_at(10, 30).strftime("%b %d, %Y %I:%M %p")
    shared._phone_calendar_events = [_phone_event("Standup", start_str)]
    shared._phone_calendar_sync_time = time.time()

    _, p_cal, p_voice, p_weather, p_reminders = patch_briefing_deps(google_events=[make_event("Gym", _today_at(18))])
    with p_cal, p_voice as voice, p_weather, p_reminders:
        await jobs_calendar.morning_briefing()

    text = voice.await_args.args[0]
    assert "2 events today" in text
    assert text.index("Standup") < text.index("Gym")
    assert "couldn't reach" not in text


async def test_only_source_broken_says_so_instead_of_clear(delivery_phone_cache, patch_briefing_deps):
    """Corrupt push + nothing else configured -> never 'your calendar is clear'."""
    shared = delivery_phone_cache
    shared._phone_calendar_events = [_phone_event("", "")]
    shared._phone_calendar_sync_time = time.time()

    google, p_cal, p_voice, p_weather, p_reminders = patch_briefing_deps(google_configured=False)
    with p_cal, p_voice as voice, p_weather, p_reminders:
        await jobs_calendar.morning_briefing()

    google.list_window.assert_not_awaited()
    text = voice.await_args.args[0]
    assert "calendar is clear" not in text
    assert "I couldn't reach your work calendar, so I can't tell you what's on today" in text


async def test_legacy_phone_push_is_ignored_not_reported(delivery_phone_cache, patch_briefing_deps):
    """A pre-2026-10 all-calendars push (no "exchange" source) is retired: its
    events aren't read and it never produces a "couldn't reach" caveat."""
    shared = delivery_phone_cache
    shared._phone_calendar_source = ""
    shared._phone_calendar_events = [_phone_event("Old", _today_at(10).strftime("%b %d, %Y %I:%M %p"))]
    shared._phone_calendar_sync_time = time.time() - 90 * 86400

    _, p_cal, p_voice, p_weather, p_reminders = patch_briefing_deps(google_events=[])
    with p_cal, p_voice as voice, p_weather, p_reminders:
        await jobs_calendar.morning_briefing()

    text = voice.await_args.args[0]
    assert "Old" not in text and "couldn't reach" not in text
    assert "Your calendar is clear today." in text


# ---------------------------------------------------------------------------
# Routine-collision gate: don't talk over a guided routine
# ---------------------------------------------------------------------------


_OUTCOMES = ("spoken", "skipped_routine", "suppressed", "failed")


def _outcome(label):
    return metric_value(MORNING_BRIEFING_OUTCOME_TOTAL, outcome=label)


def _session(*, paused=False, age_hours=0.0):
    # jobs_calendar's own clock, so a _pin_hour'd test sees a fresh session:
    # with the real clock, a run between 00:00 and 04:00 made the session look
    # 3+ h older than the pinned 07:00 and the routine gate ignored it.
    return RoutineSession(
        routine_id="morning",
        display_name="Morning",
        started_at=jobs_calendar.datetime.now() - timedelta(hours=age_hours),
        paused=paused,
    )


def _job(job_id, seconds_ahead):
    job = MagicMock()
    job.id = job_id
    job.next_run_time = datetime.now(ZoneInfo(TIMEZONE)) + timedelta(seconds=seconds_ahead)
    return job


@pytest.fixture
def briefing_env(delivery_phone_cache, tmp_db, monkeypatch):
    """Isolate morning_briefing from disk/HA/Telegram. Phone cache empty and
    no calendar configured -> 'calendar is clear'. Exposes mocks for
    assertions; seed per test:

    - ``parked`` / ``parked_age_hours``: the item the evening briefing parked
    - ``sleep_hours_ago``: when sleep_mode stamped sleep_started_at (None = no stamp)
    - ``undelivered``: recent missed/failed reminders for the recap
    """
    monkeypatch.setattr(jobs_calendar.shared, "DND_ACTIVE", False, raising=False)
    # Briefings mirror into the dashboard inbox (tmp_db); never Web Push here.
    monkeypatch.setattr(settings, "webpush_enabled", False, raising=False)
    env = SimpleNamespace(
        parked=None,
        parked_age_hours=0,
        sleep_hours_ago=None,
        undelivered=[],
        voice=AsyncMock(return_value={"success": True}),
        weather=AsyncMock(return_value=None),
        fire=MagicMock(),
        delete=MagicMock(),
    )

    def _get_entry(key):
        if key == "parked_item" and env.parked is not None:
            at = datetime.now() - timedelta(hours=env.parked_age_hours)
            return {"value": env.parked, "updated_at": at.isoformat()}
        if key == "sleep_started_at" and env.sleep_hours_ago is not None:
            at = (datetime.now() - timedelta(hours=env.sleep_hours_ago)).isoformat()
            return {"value": at, "updated_at": at}
        return None

    with (
        no_calendar_patch(),
        patch("orchestrator.jobs_calendar._announce_voice", env.voice),
        patch("orchestrator.jobs_calendar._get_weather_forecast", env.weather),
        patch("orchestrator.jobs_calendar.list_pending_reminders", return_value=[]),
        patch.object(jobs_calendar.state_store, "get_app_state_entry", side_effect=_get_entry),
        patch.object(jobs_calendar.state_store, "delete_app_state", env.delete),
        patch.object(
            jobs_calendar.state_store, "get_recent_reminder_outcomes", side_effect=lambda hours=24: env.undelivered
        ),
        patch("orchestrator.telegram_bot.fire_system_message", env.fire),
    ):
        yield env


async def test_active_fresh_session_skips_announce_and_mirrors(briefing_env):
    briefing_env.parked = "the tax form"
    before = _outcome("skipped_routine")
    with patch.object(routine_manager, "_active_session", _session()):
        await jobs_calendar.morning_briefing()

    briefing_env.voice.assert_not_awaited()
    briefing_env.fire.assert_called_once()
    msg = briefing_env.fire.call_args.args[0]
    assert msg.startswith("☀️")
    assert "the tax form" in msg  # parked item reaches the user via the mirror
    assert _outcome("skipped_routine") == before + 1
    briefing_env.delete.assert_not_called()  # parked item stays parked


@pytest.mark.parametrize("sess_kwargs", [{"paused": True}, {"age_hours": 4}], ids=["paused", "stale_4h"])
async def test_paused_or_stale_session_still_speaks(briefing_env, sess_kwargs):
    before = _outcome("spoken")
    with patch.object(routine_manager, "_active_session", _session(**sess_kwargs)):
        await jobs_calendar.morning_briefing()

    briefing_env.voice.assert_awaited_once()
    briefing_env.fire.assert_not_called()
    assert _outcome("spoken") == before + 1


@pytest.mark.parametrize(
    "job_id,seconds_ahead,expect_skip",
    [
        ("routine_morning", 60, True),
        ("routine_nudge_123", 60, False),
        ("routine_morning", 600, False),
        ("routine_morning", -30, False),
        ("calendar_poll", 60, False),
    ],
    ids=["trigger_60s", "nudge_60s", "trigger_10min", "trigger_past", "unrelated_job"],
)
async def test_imminent_routine_job(briefing_env, job_id, seconds_ahead, expect_skip):
    jobs_calendar.shared.scheduler.get_jobs.return_value = [_job(job_id, seconds_ahead)]
    await jobs_calendar.morning_briefing()

    if expect_skip:
        briefing_env.voice.assert_not_awaited()
        briefing_env.fire.assert_called_once()
    else:
        briefing_env.voice.assert_awaited_once()
        briefing_env.fire.assert_not_called()


async def test_same_tick_race_session_set_during_yield(briefing_env, _no_real_yield_sleep):
    """Both jobs fire in one scheduler pass: the routine sets its session only
    once the briefing yields. The yield must happen before the check."""

    async def _routine_runs_during_yield(_secs):
        routine_manager._active_session = _session()

    _no_real_yield_sleep.sleep.side_effect = _routine_runs_during_yield
    assert routine_manager._active_session is None
    await jobs_calendar.morning_briefing()

    _no_real_yield_sleep.sleep.assert_awaited_once()
    briefing_env.voice.assert_not_awaited()
    briefing_env.fire.assert_called_once()
    assert briefing_env.fire.call_args.args[0].startswith("☀️")


async def test_skip_day_mirrors_recap_and_digest_to_inbox(briefing_env):
    """Routine-skip day with a missed recap: two inbox cards (missed_recap +
    morning_briefing) and two Telegram messages. Card copy has no emoji prefix
    and the digest is one line per part; the recap lives only in the ⚠️
    message and its own card, never in the ☀️ digest."""
    briefing_env.parked = "the library book"
    briefing_env.undelivered = [
        {"text": "water the plants", "status": "missed"},
        {"text": "call the office", "status": "failed"},
    ]
    with patch.object(routine_manager, "_active_session", _session()):
        await jobs_calendar.morning_briefing()

    cards = {c["ref"]: c for c in inbox.list_pending()}
    assert set(cards) == {"missed_recap", "morning_briefing"}
    assert all(c["kind"] == "message" for c in cards.values())

    recap_card = cards["missed_recap"]["text"]
    assert recap_card == "Reminders that didn't reach you in the last day:\n• water the plants\n• call the office"

    digest_card = cards["morning_briefing"]["text"]
    assert not digest_card.startswith(("☀️", "⚠️"))
    lines = digest_card.split("\n")
    assert len(lines) >= 2  # newline-joined parts, not one paragraph
    assert lines[0].startswith("Good morning")
    assert any("the library book" in ln for ln in lines)
    assert "water the plants" not in digest_card  # recap has its own card

    # Telegram format unchanged: emoji-prefixed, digest space-joined.
    msgs = [c.args[0] for c in briefing_env.fire.call_args_list]
    assert len(msgs) == 2
    assert msgs[0] == f"⚠️ {recap_card}"
    assert msgs[1] == "☀️ " + " ".join(lines)
    assert "\n" not in msgs[1]


async def test_spoken_day_posts_only_recap_card(briefing_env):
    """Briefing spoken: the digest is not mirrored (no morning_briefing card),
    but the missed recap still gets its card."""
    briefing_env.undelivered = [{"text": "water the plants", "status": "missed"}]
    await jobs_calendar.morning_briefing()

    briefing_env.voice.assert_awaited_once()
    assert [c["ref"] for c in inbox.list_pending()] == ["missed_recap"]


async def test_skip_day_without_recap_posts_only_digest(briefing_env):
    with patch.object(routine_manager, "_active_session", _session()):
        await jobs_calendar.morning_briefing()

    assert [c["ref"] for c in inbox.list_pending()] == ["morning_briefing"]
    briefing_env.fire.assert_called_once()


def _pin_hour(monkeypatch, hour):
    """Freeze jobs_calendar's clock at ``hour`` today. morning_briefing clears
    sleep mode only between 05:00 and 11:59, so DND tests must not depend on
    when the suite happens to run (this one failed every morning)."""
    real = datetime

    class _Fixed(real):
        @classmethod
        def now(cls, tz=None):
            return real.now(tz).replace(hour=hour, minute=0, second=0, microsecond=0)

    monkeypatch.setattr(jobs_calendar, "datetime", _Fixed)


async def test_skip_day_under_dnd_posts_cards_without_push(briefing_env, monkeypatch):
    """Sleep mode still on (briefing before 05:00): cards wait in the tray,
    the phone isn't buzzed."""
    _pin_hour(monkeypatch, 4)
    monkeypatch.setattr(jobs_calendar.shared, "DND_ACTIVE", True)
    notify = MagicMock()
    monkeypatch.setattr(inbox, "notify", notify)
    briefing_env.undelivered = [{"text": "water the plants", "status": "missed"}]
    with patch.object(routine_manager, "_active_session", _session()):
        await jobs_calendar.morning_briefing()

    assert {c["ref"] for c in inbox.list_pending()} == {"missed_recap", "morning_briefing"}
    notify.assert_not_called()


async def test_morning_hours_clear_dnd_then_push(briefing_env, monkeypatch):
    """At 07:00 the briefing ends sleep mode first, so its cards do push."""
    _pin_hour(monkeypatch, 7)
    monkeypatch.setattr(jobs_calendar.shared, "DND_ACTIVE", True)
    notify = MagicMock()
    monkeypatch.setattr(inbox, "notify", notify)
    briefing_env.undelivered = [{"text": "water the plants", "status": "missed"}]
    with patch.object(routine_manager, "_active_session", _session()):
        await jobs_calendar.morning_briefing()

    assert jobs_calendar.shared.DND_ACTIVE is False
    assert notify.call_count == 2


async def test_spoken_day_recap_is_spoken(briefing_env):
    """Non-skip day: the recap still goes into the spoken text (regression
    guard for the `recap = None` init)."""
    briefing_env.undelivered = [{"text": "water the plants", "status": "failed"}]
    await jobs_calendar.morning_briefing()

    assert "water the plants" in briefing_env.voice.call_args.args[0]


# ---------------------------------------------------------------------------
# Outcome metric + pickup of the item the evening briefing parked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result,label,cleared",
    [
        pytest.param({"success": True}, "spoken", True, id="spoken-clears"),
        pytest.param({"success": False}, "failed", False, id="failed-keeps"),
        # DND / live voice session: success+suppressed — nothing was heard.
        pytest.param(
            {"success": True, "suppressed": True, "reason": "dnd_active"}, "suppressed", False, id="suppressed-keeps"
        ),
    ],
)
async def test_announce_outcome_counted_and_parked_item_cleared_only_when_heard(briefing_env, result, label, cleared):
    briefing_env.parked = "the tax form"
    briefing_env.voice.return_value = result
    before = {k: _outcome(k) for k in _OUTCOMES}

    await jobs_calendar.morning_briefing()

    assert {k: _outcome(k) - before[k] for k in _OUTCOMES} == {k: int(k == label) for k in _OUTCOMES}
    assert "Last night you parked: the tax form" in briefing_env.voice.await_args.args[0]
    if cleared:
        briefing_env.delete.assert_called_once_with("parked_item")
    else:
        briefing_env.delete.assert_not_called()


async def test_stale_parked_item_is_dropped_not_announced(briefing_env):
    """A parked item older than 36h (evening job disabled/failing) is dropped,
    not announced as 'last night'."""
    briefing_env.parked, briefing_env.parked_age_hours = "the OAuth flow", 72
    await jobs_calendar.morning_briefing()

    assert "parked" not in briefing_env.voice.await_args.args[0].lower()
    briefing_env.delete.assert_called_once_with("parked_item")


async def test_no_parked_item_no_mention(briefing_env):
    await jobs_calendar.morning_briefing()

    assert "parked" not in briefing_env.voice.await_args.args[0].lower()
    briefing_env.delete.assert_not_called()


def test_outcome_labels_preinitialized():
    labels = {s.labels.get("outcome") for m in MORNING_BRIEFING_OUTCOME_TOTAL.collect() for s in m.samples}
    assert set(_OUTCOMES) <= labels


def test_routine_check_swallows_scheduler_errors(caplog):
    jobs_calendar.shared.scheduler.get_jobs.side_effect = RuntimeError("scheduler down")
    with caplog.at_level(logging.WARNING, logger="orchestrator.jobs_calendar"):
        assert jobs_calendar._routine_active_or_imminent() is False
    assert any("Routine check failed" in r.getMessage() for r in caplog.records)


def test_routine_check_naive_next_run_time_is_swallowed():
    """A tz-naive next_run_time can't be compared with the aware 'now' —
    must degrade to False (speak), not raise."""
    job = MagicMock()
    job.id = "routine_morning"
    job.next_run_time = datetime.now() + timedelta(seconds=60)
    jobs_calendar.shared.scheduler.get_jobs.return_value = [job]
    assert jobs_calendar._routine_active_or_imminent() is False


# ---------------------------------------------------------------------------
# Short-night adaptation (sleep_mode stamps sleep_started_at at goodnight)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hours_ago,short,weather,cleared",
    [
        # Goodnight 5h ago -> gentle greeting, weather skipped, stamp cleared.
        pytest.param(5, True, False, True, id="short-night"),
        # Goodnight 8h ago -> normal greeting, weather included, stamp cleared.
        pytest.param(8, False, True, True, id="full-night"),
        # A 20h-old stamp (afternoon timed mute yesterday) is not last night.
        pytest.param(20, False, None, True, id="stale-stamp-ignored"),
        pytest.param(None, False, None, False, id="no-stamp"),
    ],
)
async def test_short_night_softens_briefing(briefing_env, hours_ago, short, weather, cleared):
    briefing_env.sleep_hours_ago = hours_ago
    await jobs_calendar.morning_briefing()

    text = briefing_env.voice.await_args.args[0]
    if short:
        assert "Short night" in text
        assert "Good morning" not in text
    else:
        assert "Good morning" in text
    if weather is not None:
        assert briefing_env.weather.await_count == (1 if weather else 0)
    if cleared:
        briefing_env.delete.assert_called_once_with("sleep_started_at")
    else:
        briefing_env.delete.assert_not_called()
