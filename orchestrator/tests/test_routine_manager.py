"""
Tests for routine_manager.py (F-006) — routine session lifecycle, step
advancement, nudge delivery, skip guards, pause/resume, and calendar context.

Mocks TTS, HA, calendar, and scheduler to isolate routine logic.
Requires full orchestrator dependencies (runs inside Docker).
"""

from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import guard — skip if orchestrator deps not available locally
# ---------------------------------------------------------------------------


def _can_import():
    try:
        import routine_manager  # noqa: F401

        return True
    except (ImportError, ModuleNotFoundError):
        return False


_skip_no_deps = pytest.mark.skipif(
    not _can_import(),
    reason="routine_manager requires chromadb and full orchestrator dependencies",
)


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

SAMPLE_ROUTINES = {
    "routines": {
        "morning": {
            "display_name": "Morning Routine",
            "trigger": {"type": "scheduled", "time": "07:00"},
            "speaker": "media_player.bedroom_pair",
            "nudge_delay_minutes": 10,
            "steps": [
                {"id": "meds", "label": "Take your meds", "est_minutes": 2, "skippable": False},
                {"id": "shower", "label": "Shower", "est_minutes": 15, "skippable": True},
                {
                    "id": "breakfast",
                    "label": "Eat breakfast",
                    "est_minutes": 20,
                    "skippable": True,
                    "fallback_label": "Grab something quick",
                    "fallback_threshold_minutes": 30,
                },
            ],
        },
        "evening": {
            "display_name": "Evening Routine",
            "trigger": {"type": "scheduled", "time": "21:00"},
            "speaker": "media_player.bedroom_pair",
            "nudge_delay_minutes": 15,
            "steps": [
                {"id": "evening_meds", "label": "Take your evening meds", "est_minutes": 2, "skippable": False},
            ],
        },
    }
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def setup_routine_manager():
    """Reset routine_manager state and mock voice/calendar for each test."""
    if not _can_import():
        pytest.skip("orchestrator deps not available")

    import routine_manager

    routine_manager._active_session = None
    routine_manager._routines = SAMPLE_ROUTINES["routines"]

    with (
        patch.object(routine_manager, "_announce_voice", new_callable=AsyncMock) as mock_voice,
        patch.object(routine_manager, "_get_calendar_buffer", new_callable=AsyncMock, return_value=(None, None)),
        patch.object(routine_manager, "_fire_step_ha_action", new_callable=AsyncMock),
        patch.object(routine_manager, "_schedule_nudge"),
        patch.object(routine_manager, "_cancel_nudge"),
    ):
        yield {"module": routine_manager, "mock_voice": mock_voice}

    routine_manager._active_session = None
    routine_manager._routines = {}


@pytest.fixture
def rm(setup_routine_manager):
    return setup_routine_manager["module"]


@pytest.fixture
def mock_voice(setup_routine_manager):
    return setup_routine_manager["mock_voice"]


# ---------------------------------------------------------------------------
# Start routine
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestStartRoutine:
    @pytest.mark.asyncio
    async def test_start_morning(self, rm, mock_voice):
        result = await rm.start_routine("morning")
        assert "Take your meds" in result
        assert rm._active_session is not None
        assert rm._active_session.routine_id == "morning"
        mock_voice.assert_called()

    @pytest.mark.asyncio
    async def test_start_unknown_routine(self, rm):
        result = await rm.start_routine("workout")
        assert "Unknown" in result

    @pytest.mark.asyncio
    async def test_double_start_rejected(self, rm):
        await rm.start_routine("morning")
        result = await rm.start_routine("evening")
        assert "already" in result.lower()

    @pytest.mark.asyncio
    async def test_skip_during_focus(self, rm):
        with patch.object(rm, "shared") as mock_shared:
            mock_shared.current_focus_session = {"active": True, "task": "coding"}
            result = await rm.start_routine("morning", triggered_by="scheduled")
        assert "focus" in result.lower()
        assert rm._active_session is None


# ---------------------------------------------------------------------------
# Step advancement
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestAdvanceStep:
    @pytest.mark.asyncio
    async def test_done_advances(self, rm):
        await rm.start_routine("morning")
        result = await rm.advance_step("done")
        assert "Shower" in result
        assert rm._active_session.current_step_index == 1
        assert "meds" in rm._active_session.completed_steps

    @pytest.mark.asyncio
    async def test_skip_skippable_step(self, rm):
        await rm.start_routine("morning")
        await rm.advance_step("done")  # past meds
        result = await rm.advance_step("skip")  # skip shower
        assert "shower" in rm._active_session.skipped_steps

    @pytest.mark.asyncio
    async def test_skip_non_skippable_rejected(self, rm):
        await rm.start_routine("morning")
        result = await rm.advance_step("skip")  # meds not skippable
        assert "can't skip" in result.lower()
        assert rm._active_session.current_step_index == 0

    @pytest.mark.asyncio
    async def test_complete_all_steps(self, rm):
        await rm.start_routine("morning")
        await rm.advance_step("done")  # meds
        await rm.advance_step("done")  # shower
        with patch.object(rm, "_record_routine_progress"):
            result = await rm.advance_step("done")  # breakfast (last)
        assert "done" in result.lower()
        assert rm._active_session is None

    @pytest.mark.asyncio
    async def test_no_active_session(self, rm):
        result = await rm.advance_step("done")
        assert "no routine" in result.lower()


# ---------------------------------------------------------------------------
# Pause / Resume / Stop
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestPauseResumeStop:
    @pytest.mark.asyncio
    async def test_pause(self, rm):
        await rm.start_routine("morning")
        result = await rm.advance_step("pause")
        assert "paused" in result.lower()
        assert rm._active_session.paused is True

    @pytest.mark.asyncio
    async def test_resume(self, rm):
        await rm.start_routine("morning")
        await rm.advance_step("pause")
        result = await rm.advance_step("resume")
        assert "resumed" in result.lower()
        assert rm._active_session.paused is False

    @pytest.mark.asyncio
    async def test_advance_while_paused(self, rm):
        await rm.start_routine("morning")
        await rm.advance_step("pause")
        result = await rm.advance_step("done")
        assert "paused" in result.lower()

    @pytest.mark.asyncio
    async def test_stop(self, rm):
        await rm.start_routine("morning")
        await rm.advance_step("done")
        with patch.object(rm, "_record_routine_progress"):
            result = await rm.advance_step("stop")
        assert "stopped" in result.lower()
        assert rm._active_session is None


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestStatus:
    @pytest.mark.asyncio
    async def test_status_active(self, rm):
        await rm.start_routine("morning")
        result = await rm.get_routine_status()
        assert "step 1 of 3" in result
        assert "Take your meds" in result

    @pytest.mark.asyncio
    async def test_status_no_session(self, rm):
        result = await rm.get_routine_status()
        assert "no routine" in result.lower()


# ---------------------------------------------------------------------------
# Nudge delivery
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestNudge:
    @pytest.mark.asyncio
    async def test_nudge_increments_count(self, rm, mock_voice):
        await rm.start_routine("morning")
        mock_voice.reset_mock()
        # Re-enable _deliver_nudge (it was patched via _schedule_nudge mock)
        # Call the real nudge function directly
        with patch.object(rm, "_cancel_nudge"), patch.object(rm, "_schedule_nudge"):
            await rm._deliver_nudge()
        assert rm._active_session.nudge_count == 1

    @pytest.mark.asyncio
    async def test_nudge_when_paused(self, rm, mock_voice):
        await rm.start_routine("morning")
        rm._active_session.paused = True
        mock_voice.reset_mock()
        await rm._deliver_nudge()
        # Should not announce when paused
        assert rm._active_session.nudge_count == 0


# ---------------------------------------------------------------------------
# Prompt context
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestPromptContext:
    @pytest.mark.asyncio
    async def test_context_when_active(self, rm):
        await rm.start_routine("morning")
        ctx = rm.get_active_routine_context()
        assert "Morning Routine" in ctx
        assert "step 1/3" in ctx

    def test_context_when_inactive(self, rm):
        ctx = rm.get_active_routine_context()
        assert ctx == ""
