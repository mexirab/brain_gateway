"""
Tests for focus_manager.py — F-004 Body Doubling & Focus Sessions.

Covers backward compatibility, multi-sprint sessions, audio source routing,
check-ins, sprint transitions, session summaries, and input validation.

Mocks: ha_client, pihole, scheduler, _announce_voice, state_store.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import guard
# ---------------------------------------------------------------------------


def _can_import_focus_manager():
    try:
        from orchestrator import focus_manager  # noqa: F401

        return True
    except (ImportError, ModuleNotFoundError):
        return False


_skip_no_deps = pytest.mark.skipif(
    not _can_import_focus_manager(),
    reason="focus_manager requires full orchestrator dependencies",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_pihole_result(success=True, message="ok"):
    from orchestrator.pihole_client import PiHoleResult

    return PiHoleResult(success=success, message=message)


@pytest.fixture
def mock_pihole():
    pihole = AsyncMock()
    pihole.enable_focus_blocking = AsyncMock(return_value=_make_pihole_result())
    pihole.disable_focus_blocking = AsyncMock(return_value=_make_pihole_result())
    return pihole


@pytest.fixture
def mock_ha():
    ha = AsyncMock()
    result = MagicMock()
    result.success = True
    result.message = "ok"
    ha.call_service = AsyncMock(return_value=result)
    return ha


@pytest.fixture
def mock_scheduler():
    sched = MagicMock()
    sched.add_job = MagicMock()
    sched.remove_job = MagicMock()
    sched.get_job = MagicMock(return_value=None)
    sched.reschedule_job = MagicMock()
    return sched


@pytest.fixture
def mock_state_store():
    store = MagicMock()
    store.save_focus_session = MagicMock()
    store.clear_focus_session = MagicMock()
    return store


@pytest.fixture
def mock_announce():
    return AsyncMock()


def _default_session():
    """Return a clean inactive session dict."""
    return {
        "active": False,
        "task": None,
        "started": None,
        "duration": None,
        "break_duration": None,
        "job_id": None,
        "audio_player": None,
        "block_sites": False,
        "task_description": None,
        "sprint_count": 0,
        "sprints_planned": None,
        "check_in_interval": None,
        "check_in_job_id": None,
        "total_focus_minutes": 0,
        "audio_source": "endel",
    }


@pytest.fixture
def patched_focus(mock_pihole, mock_ha, mock_scheduler, mock_state_store, mock_announce):
    """Patch all external deps and provide a clean session dict."""
    from orchestrator import focus_manager, shared

    session = _default_session()
    original_session = shared.current_focus_session.copy()

    with (
        patch.object(shared, "current_focus_session", session),
        patch.object(shared, "ha_client", mock_ha),
        patch.object(shared, "scheduler", mock_scheduler),
        patch("orchestrator.focus_manager.ha_client", mock_ha),
        patch("orchestrator.focus_manager.scheduler", mock_scheduler),
        patch("orchestrator.focus_manager.get_pihole_client", return_value=mock_pihole),
        patch("orchestrator.focus_manager._announce_voice", mock_announce),
        patch("orchestrator.focus_manager.state_store", mock_state_store),
        patch("orchestrator.focus_manager.current_focus_session", session),
        patch("orchestrator.focus_manager.FOCUS_AUDIO_PLAYER", "media_player.office"),
        patch("orchestrator.focus_manager.FOCUS_AUDIO_LOFI_URL", "http://lofi.stream/listen"),
        patch("orchestrator.focus_manager.FOCUS_AUDIO_COFFEE_URL", "http://coffee.stream/listen"),
        patch("orchestrator.focus_manager.ENDEL_ENABLED", True),
        patch("orchestrator.focus_manager.FOCUS_SESSIONS_STARTED", MagicMock()),
        patch("orchestrator.focus_manager.FOCUS_SESSIONS_COMPLETED", MagicMock()),
        patch("orchestrator.focus_manager.FOCUS_SESSIONS_STOPPED_EARLY", MagicMock()),
        patch("orchestrator.focus_manager.FOCUS_SESSION_DURATION", MagicMock()),
        patch("orchestrator.focus_manager.FOCUS_ACTIVE", MagicMock()),
        patch("orchestrator.focus_manager.PIHOLE_BLOCKING_TOGGLES", MagicMock()),
    ):
        yield {
            "session": session,
            "pihole": mock_pihole,
            "ha": mock_ha,
            "scheduler": mock_scheduler,
            "state_store": mock_state_store,
            "announce": mock_announce,
            "focus_manager": focus_manager,
        }

    shared.current_focus_session.update(original_session)


# ---------------------------------------------------------------------------
# 1. tool_start_focus backward compat
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestStartFocusBackwardCompat:
    @pytest.mark.asyncio
    async def test_legacy_call_no_new_params(self, patched_focus):
        """Existing calls with no F-004 params still work."""
        fm = patched_focus["focus_manager"]
        result = await fm.tool_start_focus(task="Write report", duration=25)

        assert "Focus session started" in result
        session = patched_focus["session"]
        assert session["active"] is True
        assert session["task"] == "Write report"
        assert session["duration"] == 25
        # Legacy single sprint: sprints_planned should be None
        assert session["sprints_planned"] is None
        assert session["sprint_count"] == 0

    @pytest.mark.asyncio
    async def test_legacy_response_mentions_task(self, patched_focus):
        """Legacy response includes task name and duration."""
        fm = patched_focus["focus_manager"]
        result = await fm.tool_start_focus(task="Read book", duration=30)
        assert "Read book" in result
        assert "30 minutes" in result

    @pytest.mark.asyncio
    async def test_already_active_returns_warning(self, patched_focus):
        """Starting a session while one is active returns a warning."""
        session = patched_focus["session"]
        session["active"] = True
        session["task"] = "Existing task"
        session["started"] = datetime.now()
        session["duration"] = 25

        fm = patched_focus["focus_manager"]
        result = await fm.tool_start_focus(task="New task")
        assert "already focusing" in result.lower()


# ---------------------------------------------------------------------------
# 2. tool_start_focus with sprints
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestStartFocusWithSprints:
    @pytest.mark.asyncio
    async def test_multi_sprint_sets_planned(self, patched_focus):
        """sprints > 1 sets sprints_planned in session."""
        fm = patched_focus["focus_manager"]
        result = await fm.tool_start_focus(task="Study", duration=25, sprints=3)

        session = patched_focus["session"]
        assert session["sprints_planned"] == 3
        assert session["active"] is True

    @pytest.mark.asyncio
    async def test_multi_sprint_response_mentions_sprints(self, patched_focus):
        """Response mentions sprint count for multi-sprint sessions."""
        fm = patched_focus["focus_manager"]
        result = await fm.tool_start_focus(task="Study", duration=25, sprints=3)
        assert "3 sprints" in result

    @pytest.mark.asyncio
    async def test_check_in_job_scheduled(self, patched_focus):
        """Check-in interval job is scheduled when check_ins=True."""
        fm = patched_focus["focus_manager"]
        await fm.tool_start_focus(task="Code", duration=25, check_ins=True, check_in_interval=10)

        session = patched_focus["session"]
        assert session["check_in_interval"] == 10
        assert session["check_in_job_id"] is not None
        # Scheduler should have been called for both break job and check-in job
        assert patched_focus["scheduler"].add_job.call_count == 2

    @pytest.mark.asyncio
    async def test_check_ins_disabled(self, patched_focus):
        """check_ins=False means no check-in job."""
        fm = patched_focus["focus_manager"]
        await fm.tool_start_focus(task="Code", duration=25, check_ins=False)

        session = patched_focus["session"]
        assert session["check_in_interval"] is None
        assert session["check_in_job_id"] is None
        # Only the break job should be scheduled
        assert patched_focus["scheduler"].add_job.call_count == 1


# ---------------------------------------------------------------------------
# 3. tool_start_focus with audio sources
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestStartFocusAudioSources:
    @pytest.mark.asyncio
    async def test_lofi_audio(self, patched_focus):
        """audio='lofi' starts stream URL."""
        fm = patched_focus["focus_manager"]
        with patch.object(fm, "start_focus_stream_audio", new_callable=AsyncMock, return_value=True) as mock_stream:
            result = await fm.tool_start_focus(task="Draw", audio="lofi")
            mock_stream.assert_called_once_with("http://lofi.stream/listen", "media_player.office")
            assert "lo-fi" in result

    @pytest.mark.asyncio
    async def test_coffee_shop_audio(self, patched_focus):
        """audio='coffee_shop' starts coffee stream URL."""
        fm = patched_focus["focus_manager"]
        with patch.object(fm, "start_focus_stream_audio", new_callable=AsyncMock, return_value=True) as mock_stream:
            result = await fm.tool_start_focus(task="Write", audio="coffee_shop")
            mock_stream.assert_called_once_with("http://coffee.stream/listen", "media_player.office")
            assert "coffee shop" in result

    @pytest.mark.asyncio
    async def test_silence_audio(self, patched_focus):
        """audio='silence' skips audio start entirely."""
        fm = patched_focus["focus_manager"]
        with (
            patch.object(fm, "start_focus_stream_audio", new_callable=AsyncMock) as mock_stream,
            patch.object(fm, "start_focus_audio", new_callable=AsyncMock) as mock_endel,
        ):
            result = await fm.tool_start_focus(task="Meditate", audio="silence")
            mock_stream.assert_not_called()
            mock_endel.assert_not_called()

        session = patched_focus["session"]
        assert session["audio_source"] == "silence"

    @pytest.mark.asyncio
    async def test_endel_audio_default(self, patched_focus):
        """No audio param defaults to endel."""
        fm = patched_focus["focus_manager"]
        with patch.object(fm, "start_focus_audio", new_callable=AsyncMock, return_value=True) as mock_endel:
            result = await fm.tool_start_focus(task="Focus", duration=25)
            mock_endel.assert_called_once()

        session = patched_focus["session"]
        assert session["audio_source"] == "endel"


# ---------------------------------------------------------------------------
# 4. deliver_check_in
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestDeliverCheckIn:
    @pytest.mark.asyncio
    async def test_check_in_with_task(self, patched_focus):
        """Check-in announces task description via TTS."""
        session = patched_focus["session"]
        session["active"] = True
        session["task_description"] = "writing documentation"

        fm = patched_focus["focus_manager"]
        await fm.deliver_check_in()

        patched_focus["announce"].assert_called_once()
        msg = patched_focus["announce"].call_args[0][0]
        assert "writing documentation" in msg

    @pytest.mark.asyncio
    async def test_check_in_no_task(self, patched_focus):
        """Check-in with no task_description uses generic message."""
        session = patched_focus["session"]
        session["active"] = True
        session["task_description"] = None
        session["task"] = None

        fm = patched_focus["focus_manager"]
        await fm.deliver_check_in()

        patched_focus["announce"].assert_called_once()
        msg = patched_focus["announce"].call_args[0][0]
        assert "zone" in msg.lower() or "going" in msg.lower()

    @pytest.mark.asyncio
    async def test_check_in_inactive_noop(self, patched_focus):
        """Check-in does nothing when session is inactive."""
        session = patched_focus["session"]
        session["active"] = False

        fm = patched_focus["focus_manager"]
        await fm.deliver_check_in()

        patched_focus["announce"].assert_not_called()


# ---------------------------------------------------------------------------
# 5. deliver_focus_break multi-sprint (more sprints remaining)
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestDeliverFocusBreakMultiSprint:
    @pytest.mark.asyncio
    async def test_increments_sprint_count(self, patched_focus):
        """Completing a sprint increments sprint_count."""
        session = patched_focus["session"]
        session.update(
            {
                "active": True,
                "task": "Study",
                "duration": 25,
                "sprint_count": 0,
                "sprints_planned": 3,
                "total_focus_minutes": 0,
                "audio_player": None,
                "block_sites": False,
                "check_in_job_id": None,
            }
        )

        fm = patched_focus["focus_manager"]
        await fm.deliver_focus_break("Study", 5)

        assert session["sprint_count"] == 1

    @pytest.mark.asyncio
    async def test_accumulates_total_minutes(self, patched_focus):
        """Total focus minutes accumulate across sprints."""
        session = patched_focus["session"]
        session.update(
            {
                "active": True,
                "task": "Study",
                "duration": 25,
                "sprint_count": 1,
                "sprints_planned": 4,
                "total_focus_minutes": 25,
                "audio_player": None,
                "block_sites": False,
                "check_in_job_id": None,
            }
        )

        fm = patched_focus["focus_manager"]
        await fm.deliver_focus_break("Study", 5)

        assert session["total_focus_minutes"] == 50

    @pytest.mark.asyncio
    async def test_stays_active_between_sprints(self, patched_focus):
        """Session stays active during break between sprints."""
        session = patched_focus["session"]
        session.update(
            {
                "active": True,
                "task": "Study",
                "duration": 25,
                "sprint_count": 0,
                "sprints_planned": 3,
                "total_focus_minutes": 0,
                "audio_player": None,
                "block_sites": False,
                "check_in_job_id": None,
            }
        )

        fm = patched_focus["focus_manager"]
        await fm.deliver_focus_break("Study", 5)

        # Session should still be active (mid-session break)
        assert session["active"] is True
        # Job ID cleared for break
        assert session["job_id"] is None


# ---------------------------------------------------------------------------
# 6. deliver_focus_break all sprints complete
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestDeliverFocusBreakAllComplete:
    @pytest.mark.asyncio
    async def test_builds_summary_and_resets(self, patched_focus):
        """All sprints complete: summary announced, session reset."""
        session = patched_focus["session"]
        session.update(
            {
                "active": True,
                "task": "Study",
                "task_description": "Study math",
                "duration": 25,
                "sprint_count": 2,  # will become 3 after increment
                "sprints_planned": 3,
                "total_focus_minutes": 50,
                "audio_player": None,
                "block_sites": False,
                "check_in_job_id": None,
            }
        )

        fm = patched_focus["focus_manager"]
        with (
            patch.object(fm, "random")
            if hasattr(fm, "random")
            else patch("orchestrator.focus_manager.random") as mock_rand
        ):
            mock_rand.choice = lambda x: x[0]
            await fm.deliver_focus_break("Study", 5)

        # Session should be reset
        assert session["active"] is False
        # Announce should have been called with summary
        patched_focus["announce"].assert_called_once()
        msg = patched_focus["announce"].call_args[0][0]
        assert "Session complete" in msg
        assert "75" in msg  # 50 + 25 = 75 minutes total


# ---------------------------------------------------------------------------
# 7. deliver_focus_break legacy single-sprint
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestDeliverFocusBreakLegacy:
    @pytest.mark.asyncio
    async def test_legacy_resets_fully(self, patched_focus):
        """Legacy single-sprint (sprints_planned=None) resets session fully."""
        session = patched_focus["session"]
        session.update(
            {
                "active": True,
                "task": "Write report",
                "duration": 25,
                "sprint_count": 0,
                "sprints_planned": None,
                "total_focus_minutes": 0,
                "audio_player": None,
                "block_sites": False,
                "check_in_job_id": None,
            }
        )

        fm = patched_focus["focus_manager"]
        await fm.deliver_focus_break("Write report", 5)

        # Session should be fully reset (legacy behavior)
        assert session["active"] is False
        assert session["task"] is None


# ---------------------------------------------------------------------------
# 8. tool_focus_sprint next_sprint
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestFocusSprintNextSprint:
    @pytest.mark.asyncio
    async def test_next_sprint_rearms_timer(self, patched_focus):
        """next_sprint re-arms break timer and audio."""
        session = patched_focus["session"]
        session.update(
            {
                "active": True,
                "task": "Code",
                "duration": 25,
                "break_duration": 5,
                "sprint_count": 1,
                "sprints_planned": 3,
                "check_in_interval": 10,
                "audio_player": "media_player.office",
                "audio_source": "lofi",
                "block_sites": True,
                "job_id": None,
                "check_in_job_id": None,
            }
        )

        fm = patched_focus["focus_manager"]
        with patch.object(fm, "_start_audio_for_source", new_callable=AsyncMock, return_value=True):
            result = await fm.tool_focus_sprint(action="next_sprint")

        assert "Sprint 2" in result
        assert "25 minutes" in result
        # Scheduler should have break job + check-in job
        assert patched_focus["scheduler"].add_job.call_count >= 2
        # Session should have new job_id
        assert session["job_id"] is not None

    @pytest.mark.asyncio
    async def test_next_sprint_no_active_session(self, patched_focus):
        """next_sprint with no active session returns error."""
        fm = patched_focus["focus_manager"]
        result = await fm.tool_focus_sprint(action="next_sprint")
        assert "No active" in result


# ---------------------------------------------------------------------------
# 9. tool_focus_sprint extend
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestFocusSprintExtend:
    @pytest.mark.asyncio
    async def test_extend_reschedules(self, patched_focus):
        """extend reschedules the break job forward."""
        session = patched_focus["session"]
        session.update(
            {
                "active": True,
                "task": "Code",
                "duration": 25,
                "job_id": "focus_123",
            }
        )

        mock_job = MagicMock()
        mock_job.next_run_time = datetime.now() + timedelta(minutes=10)
        patched_focus["scheduler"].get_job = MagicMock(return_value=mock_job)

        fm = patched_focus["focus_manager"]
        result = await fm.tool_focus_sprint(action="extend", duration_minutes=15)

        assert "15 minutes" in result
        patched_focus["scheduler"].reschedule_job.assert_called_once()
        assert session["duration"] == 40  # 25 + 15

    @pytest.mark.asyncio
    async def test_extend_default_10(self, patched_focus):
        """extend with no duration_minutes defaults to 10."""
        session = patched_focus["session"]
        session.update(
            {
                "active": True,
                "task": "Code",
                "duration": 25,
                "job_id": "focus_123",
            }
        )

        mock_job = MagicMock()
        mock_job.next_run_time = datetime.now() + timedelta(minutes=10)
        patched_focus["scheduler"].get_job = MagicMock(return_value=mock_job)

        fm = patched_focus["focus_manager"]
        result = await fm.tool_focus_sprint(action="extend")

        assert "10 minutes" in result
        assert session["duration"] == 35  # 25 + 10

    @pytest.mark.asyncio
    async def test_extend_no_job_found(self, patched_focus):
        """extend when scheduler job is gone returns error."""
        session = patched_focus["session"]
        session.update(
            {
                "active": True,
                "task": "Code",
                "duration": 25,
                "job_id": "focus_123",
            }
        )
        patched_focus["scheduler"].get_job = MagicMock(return_value=None)

        fm = patched_focus["focus_manager"]
        result = await fm.tool_focus_sprint(action="extend")
        assert "not found" in result.lower() or "ended" in result.lower()


# ---------------------------------------------------------------------------
# 10. tool_focus_sprint end_session
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestFocusSprintEndSession:
    @pytest.mark.asyncio
    async def test_end_session_builds_summary(self, patched_focus):
        """end_session builds summary and resets."""
        session = patched_focus["session"]
        session.update(
            {
                "active": True,
                "task": "Code",
                "task_description": "refactoring",
                "duration": 25,
                "sprint_count": 2,
                "sprints_planned": 4,
                "total_focus_minutes": 50,
                "audio_player": "media_player.office",
                "block_sites": True,
                "job_id": "focus_123",
                "check_in_job_id": "checkin_123",
            }
        )

        fm = patched_focus["focus_manager"]
        with patch("orchestrator.focus_manager.random") as mock_rand:
            mock_rand.choice = lambda x: x[0]
            result = await fm.tool_focus_sprint(action="end_session")

        assert "Session complete" in result
        assert session["active"] is False
        patched_focus["announce"].assert_called_once()

    @pytest.mark.asyncio
    async def test_end_session_unknown_action(self, patched_focus):
        """Unknown action returns error."""
        session = patched_focus["session"]
        session["active"] = True

        fm = patched_focus["focus_manager"]
        result = await fm.tool_focus_sprint(action="invalid_action")
        assert "Unknown action" in result


# ---------------------------------------------------------------------------
# 11. tool_stop_focus with sprints
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestStopFocusWithSprints:
    @pytest.mark.asyncio
    async def test_stop_reports_total_across_sprints(self, patched_focus):
        """Stopping mid-session reports total across all completed sprints."""
        session = patched_focus["session"]
        session.update(
            {
                "active": True,
                "task": "Study",
                "started": datetime.now() - timedelta(minutes=10),
                "duration": 25,
                "sprint_count": 2,
                "sprints_planned": 4,
                "total_focus_minutes": 50,
                "audio_player": None,
                "block_sites": False,
                "job_id": "focus_123",
                "check_in_job_id": None,
            }
        )

        fm = patched_focus["focus_manager"]
        result = await fm.tool_stop_focus()

        assert "Study" in result
        assert "2 completed sprints" in result  # sprint_count=2 completed
        # Total should include accumulated + current elapsed
        assert "total" in result.lower()

    @pytest.mark.asyncio
    async def test_stop_no_active_session(self, patched_focus):
        """Stopping with no active session returns message."""
        fm = patched_focus["focus_manager"]
        result = await fm.tool_stop_focus()
        assert "No focus timer" in result

    @pytest.mark.asyncio
    async def test_stop_legacy_single_sprint(self, patched_focus):
        """Legacy single sprint stop doesn't mention sprint count."""
        session = patched_focus["session"]
        session.update(
            {
                "active": True,
                "task": "Write",
                "started": datetime.now() - timedelta(minutes=15),
                "duration": 25,
                "sprint_count": 0,
                "sprints_planned": None,
                "total_focus_minutes": 0,
                "audio_player": None,
                "block_sites": False,
                "job_id": "focus_123",
                "check_in_job_id": None,
            }
        )

        fm = patched_focus["focus_manager"]
        result = await fm.tool_stop_focus()

        assert "Write" in result
        assert "sprint" not in result.lower()


# ---------------------------------------------------------------------------
# 12. _build_session_summary
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestBuildSessionSummary:
    def test_single_sprint_summary(self, patched_focus):
        """Single sprint summary omits sprint count."""
        session = patched_focus["session"]
        session.update(
            {
                "task_description": "writing tests",
                "task": "writing tests",
                "sprint_count": 1,
                "total_focus_minutes": 25,
            }
        )

        fm = patched_focus["focus_manager"]
        with patch("orchestrator.focus_manager.random") as mock_rand:
            mock_rand.choice = lambda x: x[0]
            result = fm._build_session_summary()

        assert "Session complete" in result
        assert "25 minutes" in result
        assert "writing tests" in result
        # Single sprint should NOT say "across N sprints"
        assert "sprints" not in result

    def test_multi_sprint_summary(self, patched_focus):
        """Multi-sprint summary includes sprint count."""
        session = patched_focus["session"]
        session.update(
            {
                "task_description": "studying",
                "task": "studying",
                "sprint_count": 3,
                "total_focus_minutes": 75,
            }
        )

        fm = patched_focus["focus_manager"]
        with patch("orchestrator.focus_manager.random") as mock_rand:
            mock_rand.choice = lambda x: x[0]
            result = fm._build_session_summary()

        assert "Session complete" in result
        assert "75 minutes" in result
        assert "3 sprints" in result
        assert "studying" in result

    def test_summary_falls_back_to_task(self, patched_focus):
        """Summary uses 'task' when 'task_description' is None."""
        session = patched_focus["session"]
        session.update(
            {
                "task_description": None,
                "task": "fallback task",
                "sprint_count": 1,
                "total_focus_minutes": 30,
            }
        )

        fm = patched_focus["focus_manager"]
        with patch("orchestrator.focus_manager.random") as mock_rand:
            mock_rand.choice = lambda x: x[0]
            result = fm._build_session_summary()

        assert "fallback task" in result


# ---------------------------------------------------------------------------
# 13. Input validation
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestInputValidation:
    @pytest.mark.asyncio
    async def test_sprints_clamped_min(self, patched_focus):
        """sprints < 1 is clamped to 1."""
        fm = patched_focus["focus_manager"]
        await fm.tool_start_focus(task="Test", sprints=0)

        session = patched_focus["session"]
        # sprints=1 means sprints_planned=None (single sprint, legacy behavior)
        assert session["sprints_planned"] is None

    @pytest.mark.asyncio
    async def test_sprints_clamped_max(self, patched_focus):
        """sprints > 10 is clamped to 10."""
        fm = patched_focus["focus_manager"]
        await fm.tool_start_focus(task="Test", sprints=20)

        session = patched_focus["session"]
        assert session["sprints_planned"] == 10

    @pytest.mark.asyncio
    async def test_check_in_interval_clamped_min(self, patched_focus):
        """check_in_interval < 5 is clamped to 5."""
        fm = patched_focus["focus_manager"]
        await fm.tool_start_focus(task="Test", check_ins=True, check_in_interval=2)

        session = patched_focus["session"]
        assert session["check_in_interval"] == 5

    @pytest.mark.asyncio
    async def test_check_in_interval_clamped_max(self, patched_focus):
        """check_in_interval > 120 is clamped to 120."""
        fm = patched_focus["focus_manager"]
        await fm.tool_start_focus(task="Test", check_ins=True, check_in_interval=200)

        session = patched_focus["session"]
        assert session["check_in_interval"] == 120

    @pytest.mark.asyncio
    async def test_invalid_duration_string(self, patched_focus):
        """Non-numeric duration returns error."""
        fm = patched_focus["focus_manager"]
        result = await fm.tool_start_focus(task="Test", duration="abc")
        assert "number" in result.lower()

    @pytest.mark.asyncio
    async def test_duration_too_high(self, patched_focus):
        """Duration > 480 returns error."""
        fm = patched_focus["focus_manager"]
        result = await fm.tool_start_focus(task="Test", duration=500)
        assert "480" in result

    @pytest.mark.asyncio
    async def test_duration_zero(self, patched_focus):
        """Duration 0 returns error."""
        fm = patched_focus["focus_manager"]
        result = await fm.tool_start_focus(task="Test", duration=0)
        assert "between" in result.lower()
