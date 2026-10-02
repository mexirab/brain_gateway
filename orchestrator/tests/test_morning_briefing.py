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
            patch("orchestrator.jobs_calendar._announce_voice", new_callable=AsyncMock, return_value={"success": True}),
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
def briefing_env(reset_phone_cache, monkeypatch):
    """Isolate morning_briefing from disk/HA/Telegram. Phone cache empty and
    Google unconfigured -> 'calendar is clear'. Exposes mocks for assertions;
    `parked` / `undelivered` are mutable so each test can seed them."""
    from types import SimpleNamespace

    from orchestrator import jobs_calendar

    monkeypatch.setattr(jobs_calendar.shared, "DND_ACTIVE", False, raising=False)
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

    client = MagicMock()
    client.is_configured = False
    with (
        patch("orchestrator.jobs_calendar.get_calendar_client", return_value=client),
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
