"""
Tests for routine_manager.py (F-006) — routine session lifecycle, step
advancement, nudge delivery, skip guards, pause/resume, and calendar context.

Mocks TTS, HA, calendar, and scheduler to isolate routine logic.
Requires full orchestrator dependencies (runs inside Docker).
"""

import logging
from unittest.mock import AsyncMock, patch

import pytest


def _counter_value(counter, **labels):
    """Read a labelled Prometheus counter sample, treating an unseen
    label-set as 0 (a child only materializes after the first .labels(...)).
    """
    try:
        return counter.labels(**labels)._value.get()
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Import guard — skip if orchestrator deps not available locally
# ---------------------------------------------------------------------------


def _can_import():
    try:
        from orchestrator import routine_manager  # noqa: F401

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

    from orchestrator import routine_manager

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


# ---------------------------------------------------------------------------
# _greeting_word boundary tests
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestGreetingWord:
    @pytest.mark.parametrize(
        "hour,expected",
        [
            (3, "Evening"),  # before 4am → Evening
            (4, "Morning"),  # morning starts
            (11, "Morning"),  # last hour of morning
            (12, "Afternoon"),  # afternoon starts
            (16, "Afternoon"),  # last hour of afternoon
            (17, "Evening"),  # evening starts
            (21, "Evening"),
            (23, "Evening"),
            (0, "Evening"),  # midnight is still Evening
        ],
    )
    def test_greeting_word_boundaries(self, rm, hour, expected):
        assert rm._greeting_word(hour) == expected


# ---------------------------------------------------------------------------
# _deliver_nudge — auto-end safety net
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestDeliverNudgeAutoEnd:
    """Covers the post-cap safety net added after the 2026-04-17 overnight bug.

    Previously: past cap, if step not skippable / auto-skip disabled, the nudge
    job just kept firing template-2 ("I'll move past X soon") forever.
    Now: past cap, auto-skip if possible, else advance_step("stop") cleanly.
    """

    @pytest.mark.asyncio
    async def test_under_cap_announces_normally(self, rm, mock_voice, monkeypatch):
        """nudge_count <= nudge_max → normal nudge fires, session continues."""
        await rm.start_routine("morning")
        mock_voice.reset_mock()

        monkeypatch.setattr(rm.shared, "ROUTINE_NUDGE_MAX", 3, raising=False)
        monkeypatch.setattr(rm.shared, "ROUTINE_AUTO_SKIP", True, raising=False)

        with patch.object(rm, "advance_step", new_callable=AsyncMock) as mock_advance:
            await rm._deliver_nudge()

        assert rm._active_session is not None
        assert rm._active_session.nudge_count == 1
        mock_voice.assert_awaited_once()
        # Normal nudge path should NOT trigger advance_step
        mock_advance.assert_not_called()

    @pytest.mark.asyncio
    async def test_past_cap_skippable_auto_skips(self, rm, mock_voice, monkeypatch):
        """Past cap + skippable + ROUTINE_AUTO_SKIP=True → advance_step('skip')."""
        await rm.start_routine("morning")
        # Advance past non-skippable meds to the shower step (skippable=True)
        await rm.advance_step("done")
        assert rm._active_session.current_step_index == 1
        assert rm._active_session.steps[1].skippable is True

        monkeypatch.setattr(rm.shared, "ROUTINE_NUDGE_MAX", 2, raising=False)
        monkeypatch.setattr(rm.shared, "ROUTINE_AUTO_SKIP", True, raising=False)
        rm._active_session.nudge_count = 2  # next nudge will push count > 2

        with patch.object(rm, "advance_step", new_callable=AsyncMock) as mock_advance:
            await rm._deliver_nudge()

        mock_advance.assert_awaited_once_with("skip")

    @pytest.mark.asyncio
    async def test_past_cap_non_skippable_stops(self, rm, mock_voice, monkeypatch, caplog):
        """Past cap + step.skippable=False → advance_step('stop'), WARNING logged."""
        # Start evening routine: single step evening_meds with skippable=False
        await rm.start_routine("evening")
        current_step = rm._active_session.steps[0]
        assert current_step.skippable is False

        monkeypatch.setattr(rm.shared, "ROUTINE_NUDGE_MAX", 2, raising=False)
        monkeypatch.setattr(rm.shared, "ROUTINE_AUTO_SKIP", True, raising=False)
        rm._active_session.nudge_count = 2  # next nudge will push count > 2

        with (
            patch.object(rm, "advance_step", new_callable=AsyncMock) as mock_advance,
            caplog.at_level(logging.WARNING, logger="orchestrator.routine_manager"),
        ):
            await rm._deliver_nudge()

        mock_advance.assert_awaited_once_with("stop")
        # WARNING log mentions "Auto-ending" and the step id
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("Auto-ending" in r.getMessage() and "evening_meds" in r.getMessage() for r in warnings), (
            f"Expected 'Auto-ending'+step-id WARNING; got: {[r.getMessage() for r in warnings]}"
        )

    @pytest.mark.asyncio
    async def test_past_cap_auto_skip_disabled_stops(self, rm, mock_voice, monkeypatch):
        """Past cap + skippable=True + ROUTINE_AUTO_SKIP=False → advance_step('stop')."""
        await rm.start_routine("morning")
        await rm.advance_step("done")  # move to shower (skippable=True)
        assert rm._active_session.steps[1].skippable is True

        monkeypatch.setattr(rm.shared, "ROUTINE_NUDGE_MAX", 2, raising=False)
        monkeypatch.setattr(rm.shared, "ROUTINE_AUTO_SKIP", False, raising=False)
        rm._active_session.nudge_count = 2

        with patch.object(rm, "advance_step", new_callable=AsyncMock) as mock_advance:
            await rm._deliver_nudge()

        mock_advance.assert_awaited_once_with("stop")

    @pytest.mark.asyncio
    async def test_no_active_session_is_noop(self, rm, mock_voice):
        """_active_session=None → no announcement, no advance."""
        rm._active_session = None
        with patch.object(rm, "advance_step", new_callable=AsyncMock) as mock_advance:
            await rm._deliver_nudge()
        mock_voice.assert_not_called()
        mock_advance.assert_not_called()

    @pytest.mark.asyncio
    async def test_paused_session_is_noop(self, rm, mock_voice):
        """paused=True → no announcement, no advance, nudge_count unchanged."""
        await rm.start_routine("morning")
        rm._active_session.paused = True
        mock_voice.reset_mock()
        count_before = rm._active_session.nudge_count

        with patch.object(rm, "advance_step", new_callable=AsyncMock) as mock_advance:
            await rm._deliver_nudge()

        mock_voice.assert_not_called()
        mock_advance.assert_not_called()
        assert rm._active_session.nudge_count == count_before


# ---------------------------------------------------------------------------
# advance_step("done") → selfcare bridge
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestAdvanceStepSelfcareBridge:
    """Covers the reverse-direction bridge added 2026-04-17: completing a
    routine step via advance_step('done') calls mark_selfcare_from_routine_step
    so the scheduled selfcare nudge for that action doesn't fire later.

    Regression guard: before this bridge, completing evening_meds via the
    routine still left the selfcare gate open, so check_selfcare would fire
    'did you take your Guanfacine' nudges at the next 15-min cycle.
    """

    @pytest.mark.asyncio
    async def test_done_fires_selfcare_bridge_with_step(self, rm):
        """advance_step('done') calls mark_selfcare_from_routine_step(step)
        with the completed step, not the next one."""
        await rm.start_routine("evening")  # single step: evening_meds
        completed_step = rm._active_session.steps[0]
        assert completed_step.id == "evening_meds"

        # Patch selfcare_manager import target used by advance_step.
        # Patch _record_routine_progress too since evening routine is
        # one-step and advance_step('done') will flow into _complete_routine
        with (
            patch("orchestrator.selfcare_manager.mark_selfcare_from_routine_step") as mock_mark,
            patch.object(rm, "_record_routine_progress"),
        ):
            await rm.advance_step("done")

        mock_mark.assert_called_once()
        called_step = mock_mark.call_args.args[0]
        assert called_step is completed_step
        assert called_step.id == "evening_meds"

    @pytest.mark.asyncio
    async def test_skip_does_not_fire_selfcare_bridge(self, rm):
        """advance_step('skip') must NOT call the selfcare bridge — a skipped
        step means the user explicitly didn't take the meds/eat/etc."""
        await rm.start_routine("morning")
        # Move to shower (skippable=True) so skip is allowed
        with patch("orchestrator.selfcare_manager.mark_selfcare_from_routine_step"):
            await rm.advance_step("done")  # past meds

        with patch("orchestrator.selfcare_manager.mark_selfcare_from_routine_step") as mock_mark:
            await rm.advance_step("skip")

        mock_mark.assert_not_called()

    @pytest.mark.asyncio
    async def test_bridge_exception_does_not_propagate(self, rm, caplog):
        """If mark_selfcare_from_routine_step raises, advance_step still
        returns normally (the routine continues) and logs at ERROR with
        exc_info=True so dashboards pick it up."""
        await rm.start_routine("morning")  # on meds step

        def _boom(step):
            raise RuntimeError("selfcare exploded")

        with (
            patch("orchestrator.selfcare_manager.mark_selfcare_from_routine_step", side_effect=_boom),
            caplog.at_level(logging.ERROR, logger="orchestrator.routine_manager"),
        ):
            result = await rm.advance_step("done")

        # advance_step must still return a string announcement (didn't raise)
        assert isinstance(result, str)
        assert len(result) > 0
        # Routine state advanced past meds despite the bridge failure
        assert rm._active_session.current_step_index == 1
        assert "meds" in rm._active_session.completed_steps

        # ERROR logged with exc_info
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert any("Selfcare bridge failed" in r.getMessage() for r in errors), (
            f"Expected 'Selfcare bridge failed' ERROR; got: {[r.getMessage() for r in errors]}"
        )
        assert any(r.exc_info is not None for r in errors), "Expected exc_info on ERROR record"


# ---------------------------------------------------------------------------
# Prometheus metrics — counter increments at the right call sites
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestRoutineMetrics:
    """Asserts the bgw_routine_* counters increment exactly where the source
    increments them. Counters are process-global and monotonic, so each test
    reads a before/after delta for its specific label-set rather than an
    absolute value.
    """

    @pytest.mark.asyncio
    async def test_started_increments(self, rm):
        from orchestrator.metrics import ROUTINE_STARTED

        before = _counter_value(ROUTINE_STARTED, routine="morning", triggered_by="user")
        await rm.start_routine("morning")
        after = _counter_value(ROUTINE_STARTED, routine="morning", triggered_by="user")
        assert after - before == 1

    @pytest.mark.asyncio
    async def test_steps_advanced_done_increments(self, rm):
        from orchestrator.metrics import ROUTINE_STEPS_ADVANCED

        await rm.start_routine("morning")
        before = _counter_value(ROUTINE_STEPS_ADVANCED, routine="morning", action="done")
        await rm.advance_step("done")  # complete meds
        after = _counter_value(ROUTINE_STEPS_ADVANCED, routine="morning", action="done")
        assert after - before == 1

    @pytest.mark.asyncio
    async def test_steps_advanced_skip_increments(self, rm):
        from orchestrator.metrics import ROUTINE_STEPS_ADVANCED

        await rm.start_routine("morning")
        await rm.advance_step("done")  # past non-skippable meds
        before = _counter_value(ROUTINE_STEPS_ADVANCED, routine="morning", action="skip")
        await rm.advance_step("skip")  # skip shower (skippable)
        after = _counter_value(ROUTINE_STEPS_ADVANCED, routine="morning", action="skip")
        assert after - before == 1

    @pytest.mark.asyncio
    async def test_completed_increments_on_final_step(self, rm):
        from orchestrator.metrics import ROUTINE_COMPLETED

        before = _counter_value(ROUTINE_COMPLETED, routine="evening")
        await rm.start_routine("evening")  # single-step routine
        with patch.object(rm, "_record_routine_progress"):
            await rm.advance_step("done")  # final step -> _complete_routine
        after = _counter_value(ROUTINE_COMPLETED, routine="evening")
        assert after - before == 1
        assert rm._active_session is None

    @pytest.mark.asyncio
    async def test_auto_ended_increments_on_non_skippable_past_cap(self, rm, monkeypatch):
        """The load-bearing one: non-skippable step past cap -> ROUTINE_AUTO_ENDED
        increments once, then advance_step('stop')."""
        from orchestrator.metrics import ROUTINE_AUTO_ENDED

        await rm.start_routine("evening")  # single non-skippable step
        step = rm._active_session.steps[0]
        assert step.skippable is False

        monkeypatch.setattr(rm.shared, "ROUTINE_NUDGE_MAX", 2, raising=False)
        monkeypatch.setattr(rm.shared, "ROUTINE_AUTO_SKIP", True, raising=False)
        rm._active_session.nudge_count = 2  # next nudge pushes count past cap

        before = _counter_value(ROUTINE_AUTO_ENDED, routine="evening", step=step.id)
        with patch.object(rm, "advance_step", new_callable=AsyncMock) as mock_advance:
            await rm._deliver_nudge()
        after = _counter_value(ROUTINE_AUTO_ENDED, routine="evening", step=step.id)

        assert after - before == 1
        mock_advance.assert_awaited_once_with("stop")

    @pytest.mark.asyncio
    async def test_auto_skipped_increments_on_skippable_past_cap(self, rm, monkeypatch):
        """Skippable step past cap with auto-skip enabled -> ROUTINE_AUTO_SKIPPED
        increments once, then advance_step('skip')."""
        from orchestrator.metrics import ROUTINE_AUTO_SKIPPED

        await rm.start_routine("morning")
        await rm.advance_step("done")  # move to shower (skippable=True)
        step = rm._active_session.steps[1]
        assert step.skippable is True

        monkeypatch.setattr(rm.shared, "ROUTINE_NUDGE_MAX", 2, raising=False)
        monkeypatch.setattr(rm.shared, "ROUTINE_AUTO_SKIP", True, raising=False)
        rm._active_session.nudge_count = 2

        before = _counter_value(ROUTINE_AUTO_SKIPPED, routine="morning", step=step.id)
        with patch.object(rm, "advance_step", new_callable=AsyncMock) as mock_advance:
            await rm._deliver_nudge()
        after = _counter_value(ROUTINE_AUTO_SKIPPED, routine="morning", step=step.id)

        assert after - before == 1
        mock_advance.assert_awaited_once_with("skip")

    @pytest.mark.asyncio
    async def test_under_cap_does_not_touch_terminal_counters(self, rm, monkeypatch):
        """A normal nudge under the cap must NOT increment auto-ended/auto-skipped."""
        from orchestrator.metrics import ROUTINE_AUTO_ENDED, ROUTINE_AUTO_SKIPPED

        await rm.start_routine("evening")
        step = rm._active_session.steps[0]
        monkeypatch.setattr(rm.shared, "ROUTINE_NUDGE_MAX", 3, raising=False)
        monkeypatch.setattr(rm.shared, "ROUTINE_AUTO_SKIP", True, raising=False)

        ended_before = _counter_value(ROUTINE_AUTO_ENDED, routine="evening", step=step.id)
        skipped_before = _counter_value(ROUTINE_AUTO_SKIPPED, routine="evening", step=step.id)
        with patch.object(rm, "advance_step", new_callable=AsyncMock):
            await rm._deliver_nudge()
        assert _counter_value(ROUTINE_AUTO_ENDED, routine="evening", step=step.id) == ended_before
        assert _counter_value(ROUTINE_AUTO_SKIPPED, routine="evening", step=step.id) == skipped_before
