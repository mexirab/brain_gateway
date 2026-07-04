"""
Tests for the reminder delivery state machine (deliver_reminder_job and
friends). These paths shipped without coverage and accumulated four
independent silent-failure modes (2026-07-04 audit):

  1. snooze-after-delivery never redelivered (status stayed 'completed')
  2. past-due reminders were silently dropped at startup
  3. DND/voice-session suppression was treated as successful delivery
  4. the TTS retry never detected it WAS the retry -> infinite retries +
     duplicate phone pushes on target="both"

Mocks _announce_voice / _send_notification at the tool_handlers callsites;
uses the real (stopped) AsyncIOScheduler singleton like test_ntfy_feedback.
"""

import contextlib
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_scheduler():
    """Purge reminder_* jobs from the shared scheduler before/after each test."""
    from orchestrator.shared import scheduler

    def _purge():
        for job in list(scheduler.get_jobs()):
            if job.id.startswith("reminder_"):
                with contextlib.suppress(Exception):
                    scheduler.remove_job(job.id)

    _purge()
    yield scheduler
    _purge()


@pytest.fixture
def push_channels_off(monkeypatch):
    """Disable ntfy/pushover so phone delivery rides on _send_notification alone."""
    from orchestrator.config import settings

    monkeypatch.setattr(settings, "ntfy_enabled", False, raising=False)
    monkeypatch.setattr(settings, "pushover_enabled", False, raising=False)
    return settings


def _mk_reminder(reminder_id: str, target: str = "both", minutes_ago: int = 0) -> None:
    from orchestrator import state_store

    trigger = (datetime.now() - timedelta(minutes=minutes_ago)).isoformat()
    state_store.save_reminder(reminder_id, f"test reminder {reminder_id}", trigger, target=target)


def _status(reminder_id: str) -> str:
    from orchestrator import state_store

    return (state_store.get_reminder(reminder_id) or {}).get("status", "MISSING")


VOICE_OK = {"success": True, "speaker": "media_player.test"}
VOICE_FAIL = {"success": False, "error": "ConnectError: boom"}
VOICE_SUPPRESSED_DND = {"success": True, "suppressed": True, "reason": "dnd_active"}
PHONE_OK = {"success": True, "delivered": ["notify.phone"], "errors": []}
PHONE_FAIL = {"success": False, "error": "No mobile notification services configured"}


# ---------------------------------------------------------------------------
# reopen_reminder (state_store unit)
# ---------------------------------------------------------------------------


class TestReopenReminder:
    def test_reopen_resets_status_and_ack(self, tmp_db):
        from orchestrator import state_store

        _mk_reminder("r1")
        state_store.mark_reminder_acked("r1", via="ntfy")
        assert _status("r1") == "completed"

        assert state_store.reopen_reminder("r1") is True
        rem = state_store.get_reminder("r1")
        assert rem["status"] == "pending"
        assert rem["ack_at"] is None
        assert rem["completed_at"] is None

    def test_reopen_unknown_returns_false(self, tmp_db):
        from orchestrator import state_store

        assert state_store.reopen_reminder("nope") is False


# ---------------------------------------------------------------------------
# deliver_reminder_job — suppression is not delivery
# ---------------------------------------------------------------------------


class TestSuppressionNotDelivery:
    @pytest.mark.asyncio
    async def test_dnd_suppression_reschedules_instead_of_completing(self, tmp_db, clean_scheduler):
        from orchestrator import tool_handlers

        _mk_reminder("r1", target="both")
        with (
            patch.object(tool_handlers, "_announce_voice", AsyncMock(return_value=VOICE_SUPPRESSED_DND)),
            patch.object(tool_handlers, "_send_notification", AsyncMock(return_value=PHONE_OK)) as mock_phone,
        ):
            await tool_handlers.deliver_reminder_job("r1")

        # Still pending, nothing pushed, and a redelivery job exists.
        assert _status("r1") == "pending"
        mock_phone.assert_not_called()
        assert clean_scheduler.get_job("reminder_r1_retry") is not None

    @pytest.mark.asyncio
    async def test_suppression_does_not_consume_attempt(self, tmp_db, clean_scheduler):
        from orchestrator import tool_handlers

        _mk_reminder("r1", target="voice")
        with patch.object(tool_handlers, "_announce_voice", AsyncMock(return_value=VOICE_SUPPRESSED_DND)):
            # Even on what would be the final attempt, suppression defers
            # rather than giving up.
            await tool_handlers.deliver_reminder_job("r1", attempt=tool_handlers.MAX_REMINDER_DELIVERY_ATTEMPTS - 1)

        assert _status("r1") == "pending"
        job = clean_scheduler.get_job("reminder_r1_retry")
        assert job is not None
        assert job.kwargs["attempt"] == tool_handlers.MAX_REMINDER_DELIVERY_ATTEMPTS - 1


# ---------------------------------------------------------------------------
# deliver_reminder_job — retry semantics
# ---------------------------------------------------------------------------


class TestRetrySemantics:
    @pytest.mark.asyncio
    async def test_tts_failure_schedules_retry_with_incremented_attempt(
        self, tmp_db, clean_scheduler, push_channels_off
    ):
        from orchestrator import tool_handlers

        _mk_reminder("r1", target="both")
        with (
            patch.object(tool_handlers, "_announce_voice", AsyncMock(return_value=VOICE_FAIL)),
            patch.object(tool_handlers, "_send_notification", AsyncMock(return_value=PHONE_OK)) as mock_phone,
        ):
            await tool_handlers.deliver_reminder_job("r1")

        assert _status("r1") == "pending"  # not consumed yet
        mock_phone.assert_called_once()  # phone went out on attempt 0
        job = clean_scheduler.get_job("reminder_r1_retry")
        assert job is not None
        assert job.kwargs == {"attempt": 1, "late": False, "voice_done": False, "phone_done": True}

    @pytest.mark.asyncio
    async def test_retry_does_not_repush_phone(self, tmp_db, clean_scheduler, push_channels_off):
        """The old code re-ran the whole phone block on every retry -> push spam."""
        from orchestrator import tool_handlers

        _mk_reminder("r1", target="both")
        with (
            patch.object(tool_handlers, "_announce_voice", AsyncMock(return_value=VOICE_OK)),
            patch.object(tool_handlers, "_send_notification", AsyncMock(return_value=PHONE_OK)) as mock_phone,
        ):
            await tool_handlers.deliver_reminder_job("r1", attempt=1, phone_done=True)

        mock_phone.assert_not_called()
        assert _status("r1") == "completed"

    @pytest.mark.asyncio
    async def test_retries_are_finite_and_end_in_degraded_completion(self, tmp_db, clean_scheduler, push_channels_off):
        """Final attempt with voice still failing: no further retry job, and the
        reminder completes degraded because the phone already got it."""
        from orchestrator import tool_handlers

        _mk_reminder("r1", target="both")
        with (
            patch.object(tool_handlers, "_announce_voice", AsyncMock(return_value=VOICE_FAIL)),
            patch.object(tool_handlers, "_send_notification", AsyncMock(return_value=PHONE_FAIL)) as mock_phone,
        ):
            await tool_handlers.deliver_reminder_job(
                "r1", attempt=tool_handlers.MAX_REMINDER_DELIVERY_ATTEMPTS - 1, phone_done=True
            )

        assert clean_scheduler.get_job("reminder_r1_retry") is None
        mock_phone.assert_not_called()  # phone_done -> no re-push, no fallback needed
        assert _status("r1") == "completed"

    @pytest.mark.asyncio
    async def test_total_failure_falls_back_to_phone_then_marks_failed(
        self, tmp_db, clean_scheduler, push_channels_off
    ):
        from orchestrator import tool_handlers

        _mk_reminder("r1", target="voice")
        with (
            patch.object(tool_handlers, "_announce_voice", AsyncMock(return_value=VOICE_FAIL)),
            patch.object(tool_handlers, "_send_notification", AsyncMock(return_value=PHONE_FAIL)) as mock_phone,
        ):
            await tool_handlers.deliver_reminder_job("r1", attempt=tool_handlers.MAX_REMINDER_DELIVERY_ATTEMPTS - 1)

        mock_phone.assert_called_once()  # the last-ditch fallback fired
        assert _status("r1") == "failed"  # loud terminal state, not a zombie 'pending'

    @pytest.mark.asyncio
    async def test_phone_only_failure_is_not_marked_completed(self, tmp_db, clean_scheduler, push_channels_off):
        """Old code: `if voice_ok or target == "phone"` completed phone-only
        reminders without ever checking the push result."""
        from orchestrator import tool_handlers

        _mk_reminder("r1", target="phone")
        with (
            patch.object(tool_handlers, "_announce_voice", AsyncMock(return_value=VOICE_OK)) as mock_voice,
            patch.object(tool_handlers, "_send_notification", AsyncMock(return_value=PHONE_FAIL)),
        ):
            await tool_handlers.deliver_reminder_job("r1")

        mock_voice.assert_not_called()
        assert _status("r1") == "pending"  # retry scheduled instead of false completion
        assert clean_scheduler.get_job("reminder_r1_retry") is not None


# ---------------------------------------------------------------------------
# Startup reload — past-due reminders
# ---------------------------------------------------------------------------


class TestStartupReload:
    def test_future_reminder_rescheduled_normally(self, tmp_db, clean_scheduler):
        from orchestrator import state_store, tool_handlers

        trigger = (datetime.now() + timedelta(hours=1)).isoformat()
        state_store.save_reminder("fut", "future", trigger)

        counts = tool_handlers.reschedule_pending_reminders_on_startup()
        assert counts["scheduled"] == 1
        job = clean_scheduler.get_job("reminder_fut")
        assert job is not None
        assert job.kwargs.get("late", False) is False

    def test_recent_past_due_is_late_delivered(self, tmp_db, clean_scheduler):
        from orchestrator import tool_handlers

        _mk_reminder("late1", minutes_ago=30)

        counts = tool_handlers.reschedule_pending_reminders_on_startup()
        assert counts["late"] == 1
        job = clean_scheduler.get_job("reminder_late1")
        assert job is not None
        assert job.kwargs["late"] is True
        assert _status("late1") == "pending"

    def test_stale_past_due_is_marked_missed(self, tmp_db, clean_scheduler):
        from orchestrator import tool_handlers

        _mk_reminder("old1", minutes_ago=60 * 48)  # two days ago

        counts = tool_handlers.reschedule_pending_reminders_on_startup()
        assert counts["missed"] == 1
        assert clean_scheduler.get_job("reminder_old1") is None
        assert _status("old1") == "missed"

    def test_late_deliveries_are_staggered(self, tmp_db, clean_scheduler):
        from orchestrator import tool_handlers

        _mk_reminder("late_a", minutes_ago=10)
        _mk_reminder("late_b", minutes_ago=20)

        tool_handlers.reschedule_pending_reminders_on_startup()
        run_times = sorted(clean_scheduler.get_job(f"reminder_{rid}").trigger.run_date for rid in ("late_a", "late_b"))
        assert (run_times[1] - run_times[0]).total_seconds() >= 5


# ---------------------------------------------------------------------------
# Late-delivery night guard
# ---------------------------------------------------------------------------


class TestLateNightGuard:
    @pytest.mark.asyncio
    async def test_late_delivery_at_night_skips_speakers(self, tmp_db, clean_scheduler, push_channels_off):
        from orchestrator import tool_handlers

        _mk_reminder("r1", target="both")
        night = datetime(2026, 7, 4, 3, 0, 0)

        class _FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return night.replace(tzinfo=tz) if tz else night

        with (
            patch.object(tool_handlers, "datetime", _FakeDatetime),
            patch.object(tool_handlers, "_announce_voice", AsyncMock(return_value=VOICE_OK)) as mock_voice,
            patch.object(tool_handlers, "_send_notification", AsyncMock(return_value=PHONE_OK)) as mock_phone,
        ):
            await tool_handlers.deliver_reminder_job("r1", late=True)

        mock_voice.assert_not_called()
        mock_phone.assert_called_once()
        assert _status("r1") == "completed"
