"""
Tests for jobs_calendar.morning_briefing() — calendar sources are merged
(calendar_sources) and a broken source is named in the briefing instead of
reading as a clear day; plus the routine-collision gate and outcome metrics.

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


@pytest.fixture(autouse=True)
def _no_real_yield_sleep():
    """morning_briefing yields with ``await asyncio.sleep(1)`` before its
    routine check. Replace the module's ``asyncio`` reference (not the global
    asyncio.sleep, which the event loop/pytest-asyncio also use) so no test
    pays the real second. Also pin the routine check's inputs to "nothing
    running" so the source-selection tests can't be skipped by a stray
    session/job left in module state."""
    from types import SimpleNamespace

    from orchestrator import jobs_calendar, routine_manager

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
def reset_phone_cache():
    """Reset shared phone calendar cache before/after each test."""
    from orchestrator import shared

    orig_events = shared._phone_calendar_events
    orig_time = shared._phone_calendar_sync_time
    orig_source = shared._phone_calendar_source
    shared._phone_calendar_events = []
    shared._phone_calendar_sync_time = 0.0
    shared._phone_calendar_source = "exchange"  # the work-calendar Shortcut
    yield shared
    shared._phone_calendar_events = orig_events
    shared._phone_calendar_sync_time = orig_time
    shared._phone_calendar_source = orig_source


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
    """Patch the side-effecting collaborators of morning_briefing. The REAL
    calendar_sources merge runs (incl. the phone cache in shared); only the
    Google/CalDAV network clients are faked. Returns (google_client, patches)."""

    def _patch(*, google_configured=True, google_success=True, google_events=None):
        from orchestrator.tests.calendar_fakes import fake_client, patched_calendar

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
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from orchestrator.shared import TIMEZONE

    tz = ZoneInfo(TIMEZONE)
    today = datetime.now(tz).date()
    return datetime(today.year, today.month, today.day, hour, minute, tzinfo=tz)


# ---------------------------------------------------------------------------
# Calendar sources are merged; a broken source is named, never read as "clear"
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.asyncio
async def test_corrupt_work_push_is_named_and_google_events_still_spoken(
    reset_phone_cache, fresh_phone_sync_time, patch_briefing_deps, caplog
):
    """The 2026-04-17 iPhone Shortcut bug (fresh push, every record empty):
    the push is excluded and NAMED in the briefing, and the other source's
    events are still announced."""
    from orchestrator.tests.calendar_fakes import make_event

    shared = reset_phone_cache
    shared._phone_calendar_events = [_phone_event("", ""), _phone_event("", "")]
    shared._phone_calendar_sync_time = fresh_phone_sync_time

    google, p_cal, p_voice, p_weather, p_reminders = patch_briefing_deps(
        google_events=[make_event("Dentist", _today_at(15))]
    )
    with p_cal, p_voice as voice, p_weather, p_reminders:
        from orchestrator import jobs_calendar

        with caplog.at_level(logging.WARNING):
            await jobs_calendar.morning_briefing()

    google.list_window.assert_awaited_once()
    text = voice.await_args.args[0]
    assert "Dentist" in text
    assert "couldn't reach your work calendar" in text
    assert any("corrupt" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


@_skip_no_deps
@pytest.mark.asyncio
async def test_work_and_google_events_are_merged(reset_phone_cache, fresh_phone_sync_time, patch_briefing_deps):
    from orchestrator.tests.calendar_fakes import make_event

    shared = reset_phone_cache
    start_str = _today_at(10, 30).strftime("%b %d, %Y %I:%M %p")
    shared._phone_calendar_events = [_phone_event("Standup", start_str)]
    shared._phone_calendar_sync_time = fresh_phone_sync_time

    _, p_cal, p_voice, p_weather, p_reminders = patch_briefing_deps(google_events=[make_event("Gym", _today_at(18))])
    with p_cal, p_voice as voice, p_weather, p_reminders:
        from orchestrator import jobs_calendar

        await jobs_calendar.morning_briefing()

    text = voice.await_args.args[0]
    assert "2 events today" in text
    assert text.index("Standup") < text.index("Gym")
    assert "couldn't reach" not in text


@_skip_no_deps
@pytest.mark.asyncio
async def test_only_source_broken_says_so_instead_of_clear(
    reset_phone_cache, fresh_phone_sync_time, patch_briefing_deps
):
    """Corrupt push + nothing else configured -> never 'your calendar is clear'."""
    shared = reset_phone_cache
    shared._phone_calendar_events = [_phone_event("", "")]
    shared._phone_calendar_sync_time = fresh_phone_sync_time

    google, p_cal, p_voice, p_weather, p_reminders = patch_briefing_deps(google_configured=False)
    with p_cal, p_voice as voice, p_weather, p_reminders:
        from orchestrator import jobs_calendar

        await jobs_calendar.morning_briefing()

    google.list_window.assert_not_awaited()
    text = voice.await_args.args[0]
    assert "calendar is clear" not in text
    assert "I couldn't reach your work calendar, so I can't tell you what's on today" in text


# ---------------------------------------------------------------------------
# Routine-collision gate: don't talk over a guided routine
# ---------------------------------------------------------------------------


def _outcome(label):
    from orchestrator.metrics import MORNING_BRIEFING_OUTCOME_TOTAL

    return MORNING_BRIEFING_OUTCOME_TOTAL.labels(outcome=label)._value.get()


def _session(*, paused=False, age_hours=0.0):
    from datetime import datetime, timedelta

    from orchestrator.routine_manager import RoutineSession

    return RoutineSession(
        routine_id="morning",
        display_name="Morning",
        started_at=datetime.now() - timedelta(hours=age_hours),
        paused=paused,
    )


def _job(job_id, seconds_ahead):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from orchestrator.shared import TIMEZONE

    job = MagicMock()
    job.id = job_id
    job.next_run_time = datetime.now(ZoneInfo(TIMEZONE)) + timedelta(seconds=seconds_ahead)
    return job


@pytest.fixture
def briefing_env(reset_phone_cache, tmp_db, monkeypatch):
    """Isolate morning_briefing from disk/HA/Telegram. Phone cache empty and
    Google unconfigured -> 'calendar is clear'. Exposes mocks for assertions;
    `parked` / `undelivered` are mutable so each test can seed them."""
    from types import SimpleNamespace

    from orchestrator import jobs_calendar
    from orchestrator.config import settings

    monkeypatch.setattr(jobs_calendar.shared, "DND_ACTIVE", False, raising=False)
    # Briefings mirror into the dashboard inbox (tmp_db); never Web Push here.
    monkeypatch.setattr(settings, "webpush_enabled", False, raising=False)
    env = SimpleNamespace(
        parked=None,
        undelivered=[],
        voice=AsyncMock(return_value={"success": True}),
        fire=MagicMock(),
        delete=MagicMock(),
    )

    def _get_entry(key):
        if key == "parked_item" and env.parked is not None:
            from datetime import datetime

            return {"value": env.parked, "updated_at": datetime.now().isoformat()}
        return None

    from orchestrator.tests.calendar_fakes import no_calendar_patch

    with (
        no_calendar_patch(),
        patch("orchestrator.jobs_calendar._announce_voice", env.voice),
        patch("orchestrator.jobs_calendar._get_weather_forecast", new_callable=AsyncMock, return_value=None),
        patch("orchestrator.jobs_calendar.list_pending_reminders", return_value=[]),
        patch.object(jobs_calendar.state_store, "get_app_state_entry", side_effect=_get_entry),
        patch.object(jobs_calendar.state_store, "delete_app_state", env.delete),
        patch.object(
            jobs_calendar.state_store, "get_recent_reminder_outcomes", side_effect=lambda hours=24: env.undelivered
        ),
        patch("orchestrator.telegram_bot.fire_system_message", env.fire),
    ):
        yield env


@_skip_no_deps
async def test_active_fresh_session_skips_announce_and_mirrors(briefing_env):
    from orchestrator import jobs_calendar, routine_manager

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


@_skip_no_deps
@pytest.mark.parametrize("sess_kwargs", [{"paused": True}, {"age_hours": 4}], ids=["paused", "stale_4h"])
async def test_paused_or_stale_session_still_speaks(briefing_env, sess_kwargs):
    from orchestrator import jobs_calendar, routine_manager

    before = _outcome("spoken")
    with patch.object(routine_manager, "_active_session", _session(**sess_kwargs)):
        await jobs_calendar.morning_briefing()

    briefing_env.voice.assert_awaited_once()
    briefing_env.fire.assert_not_called()
    assert _outcome("spoken") == before + 1


@_skip_no_deps
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
    from orchestrator import jobs_calendar

    jobs_calendar.shared.scheduler.get_jobs.return_value = [_job(job_id, seconds_ahead)]
    await jobs_calendar.morning_briefing()

    if expect_skip:
        briefing_env.voice.assert_not_awaited()
        briefing_env.fire.assert_called_once()
    else:
        briefing_env.voice.assert_awaited_once()
        briefing_env.fire.assert_not_called()


@_skip_no_deps
async def test_same_tick_race_session_set_during_yield(briefing_env, _no_real_yield_sleep):
    """Both jobs fire in one scheduler pass: the routine sets its session only
    once the briefing yields. The yield must happen before the check."""
    from orchestrator import jobs_calendar, routine_manager

    async def _routine_runs_during_yield(_secs):
        routine_manager._active_session = _session()

    _no_real_yield_sleep.sleep.side_effect = _routine_runs_during_yield
    assert routine_manager._active_session is None
    await jobs_calendar.morning_briefing()

    _no_real_yield_sleep.sleep.assert_awaited_once()
    briefing_env.voice.assert_not_awaited()
    briefing_env.fire.assert_called_once()
    assert briefing_env.fire.call_args.args[0].startswith("☀️")


@_skip_no_deps
async def test_skip_day_recap_only_in_warning_mirror(briefing_env):
    from orchestrator import jobs_calendar, routine_manager

    briefing_env.undelivered = [{"text": "water the plants", "status": "missed"}]
    recap = jobs_calendar.build_missed_recap(briefing_env.undelivered)
    with patch.object(routine_manager, "_active_session", _session()):
        await jobs_calendar.morning_briefing()

    msgs = [c.args[0] for c in briefing_env.fire.call_args_list]
    assert len(msgs) == 2
    warn = [m for m in msgs if m.startswith("⚠️")]
    sun = [m for m in msgs if m.startswith("☀️")]
    assert len(warn) == 1 and len(sun) == 1
    assert "water the plants" in warn[0]
    assert recap not in sun[0]
    assert "water the plants" not in sun[0]
    assert "Good morning" in sun[0]


@_skip_no_deps
async def test_skip_day_mirrors_recap_and_digest_to_inbox(briefing_env):
    """Routine-skip day with a missed recap: two inbox cards (missed_recap +
    morning_briefing). Card copy has no emoji prefix and the digest is one
    line per part; the Telegram text keeps its emoji + one-paragraph format."""
    from orchestrator import inbox, jobs_calendar, routine_manager

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
    assert msgs[0] == f"⚠️ {recap_card}"
    assert msgs[1] == "☀️ " + " ".join(lines)
    assert "\n" not in msgs[1]


@_skip_no_deps
async def test_spoken_day_posts_only_recap_card(briefing_env):
    """Briefing spoken: the digest is not mirrored (no morning_briefing card),
    but the missed recap still gets its card."""
    from orchestrator import inbox, jobs_calendar

    briefing_env.undelivered = [{"text": "water the plants", "status": "missed"}]
    await jobs_calendar.morning_briefing()

    briefing_env.voice.assert_awaited_once()
    assert [c["ref"] for c in inbox.list_pending()] == ["missed_recap"]


@_skip_no_deps
async def test_skip_day_without_recap_posts_only_digest(briefing_env):
    from orchestrator import inbox, jobs_calendar, routine_manager

    with patch.object(routine_manager, "_active_session", _session()):
        await jobs_calendar.morning_briefing()

    assert [c["ref"] for c in inbox.list_pending()] == ["morning_briefing"]
    briefing_env.fire.assert_called_once()


@_skip_no_deps
async def test_skip_day_under_dnd_posts_cards_without_push(briefing_env, monkeypatch):
    """Sleep mode at 07:00: cards wait in the tray, the phone isn't buzzed."""
    from orchestrator import inbox, jobs_calendar, routine_manager

    monkeypatch.setattr(jobs_calendar.shared, "DND_ACTIVE", True)
    notify = MagicMock()
    monkeypatch.setattr(inbox, "notify", notify)
    briefing_env.undelivered = [{"text": "water the plants", "status": "missed"}]
    with patch.object(routine_manager, "_active_session", _session()):
        await jobs_calendar.morning_briefing()

    assert {c["ref"] for c in inbox.list_pending()} == {"missed_recap", "morning_briefing"}
    notify.assert_not_called()


@_skip_no_deps
async def test_spoken_day_recap_is_spoken(briefing_env):
    """Non-skip day: the recap still goes into the spoken text (regression
    guard for the `recap = None` init)."""
    from orchestrator import jobs_calendar

    briefing_env.undelivered = [{"text": "water the plants", "status": "failed"}]
    await jobs_calendar.morning_briefing()

    spoken = briefing_env.voice.call_args.args[0]
    assert "water the plants" in spoken


@_skip_no_deps
async def test_spoken_success_counts_spoken_and_clears_parked(briefing_env):
    from orchestrator import jobs_calendar

    briefing_env.parked = "the tax form"
    s0, f0 = _outcome("spoken"), _outcome("failed")
    await jobs_calendar.morning_briefing()

    assert _outcome("spoken") == s0 + 1
    assert _outcome("failed") == f0
    briefing_env.delete.assert_called_once_with("parked_item")


@_skip_no_deps
async def test_announce_failure_counts_failed(briefing_env):
    from orchestrator import jobs_calendar

    briefing_env.parked = "the tax form"
    briefing_env.voice.return_value = {"success": False}
    s0, f0 = _outcome("spoken"), _outcome("failed")
    await jobs_calendar.morning_briefing()

    assert _outcome("failed") == f0 + 1
    assert _outcome("spoken") == s0
    briefing_env.delete.assert_not_called()


@_skip_no_deps
async def test_suppressed_announce_counts_suppressed(briefing_env):
    """DND / live voice session: _announce_voice returns success+suppressed — not "spoken"."""
    from orchestrator import jobs_calendar

    briefing_env.parked = "the tax form"
    briefing_env.voice.return_value = {"success": True, "suppressed": True, "reason": "dnd_active"}
    s0, x0 = _outcome("spoken"), _outcome("suppressed")
    await jobs_calendar.morning_briefing()

    assert _outcome("suppressed") == x0 + 1
    assert _outcome("spoken") == s0
    briefing_env.delete.assert_not_called()


@_skip_no_deps
def test_outcome_labels_preinitialized():
    from orchestrator.metrics import MORNING_BRIEFING_OUTCOME_TOTAL

    labels = {s.labels.get("outcome") for m in MORNING_BRIEFING_OUTCOME_TOTAL.collect() for s in m.samples}
    assert {"spoken", "skipped_routine", "suppressed", "failed"} <= labels


@_skip_no_deps
def test_routine_check_swallows_scheduler_errors(caplog):
    from orchestrator import jobs_calendar

    jobs_calendar.shared.scheduler.get_jobs.side_effect = RuntimeError("scheduler down")
    with caplog.at_level(logging.WARNING, logger="orchestrator.jobs_calendar"):
        assert jobs_calendar._routine_active_or_imminent() is False
    assert any("Routine check failed" in r.getMessage() for r in caplog.records)


@_skip_no_deps
def test_routine_check_naive_next_run_time_is_swallowed():
    """A tz-naive next_run_time can't be compared with the aware 'now' —
    must degrade to False (speak), not raise."""
    from datetime import datetime, timedelta

    from orchestrator import jobs_calendar

    job = MagicMock()
    job.id = "routine_morning"
    job.next_run_time = datetime.now() + timedelta(seconds=60)
    jobs_calendar.shared.scheduler.get_jobs.return_value = [job]
    assert jobs_calendar._routine_active_or_imminent() is False


@_skip_no_deps
@pytest.mark.asyncio
async def test_legacy_phone_push_is_ignored_not_reported(reset_phone_cache, fresh_phone_sync_time, patch_briefing_deps):
    """A pre-2026-10 all-calendars push (no "exchange" source) is retired: its
    events aren't read and it never produces a "couldn't reach" caveat."""
    shared = reset_phone_cache
    shared._phone_calendar_source = ""
    shared._phone_calendar_events = [_phone_event("Old", _today_at(10).strftime("%b %d, %Y %I:%M %p"))]
    shared._phone_calendar_sync_time = fresh_phone_sync_time - 90 * 86400

    _, p_cal, p_voice, p_weather, p_reminders = patch_briefing_deps(google_events=[])
    with p_cal, p_voice as voice, p_weather, p_reminders:
        from orchestrator import jobs_calendar

        await jobs_calendar.morning_briefing()

    text = voice.await_args.args[0]
    assert "Old" not in text and "couldn't reach" not in text
    assert "Your calendar is clear today." in text
