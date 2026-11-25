"""
Tests for routine_manager.py (F-006) — routine session lifecycle, step
advancement, nudge delivery, skip guards, pause/resume, and calendar context.

Mocks TTS, HA, calendar, and scheduler to isolate routine logic.
Requires full orchestrator dependencies (runs inside Docker).
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator import routine_manager, shared
from orchestrator.metrics import (
    ROUTINE_AUTO_ENDED,
    ROUTINE_AUTO_SKIPPED,
    ROUTINE_COMPLETED,
    ROUTINE_STARTED,
    ROUTINE_STEPS_ADVANCED,
)
from orchestrator.routine_manager import RoutineStep
from orchestrator.routine_manager import _fire_step_ha_action as _REAL_FIRE_STEP
from orchestrator.tests._helpers import metric_value

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
    """Reset routine_manager state and mock voice/calendar for each test.

    _fire_step_ha_action is mocked here; TestFireStepHaAction calls the real
    one (_REAL_FIRE_STEP, imported before the patch)."""
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


class TestStartRoutine:
    async def test_start_morning(self, rm, mock_voice):
        before = metric_value(ROUTINE_STARTED, routine="morning", triggered_by="user")
        result = await rm.start_routine("morning")
        assert metric_value(ROUTINE_STARTED, routine="morning", triggered_by="user") - before == 1
        assert "Take your meds" in result
        assert rm._active_session is not None
        assert rm._active_session.routine_id == "morning"
        mock_voice.assert_called()

    async def test_start_unknown_routine(self, rm):
        result = await rm.start_routine("workout")
        assert "Unknown" in result

    async def test_double_start_rejected(self, rm):
        await rm.start_routine("morning")
        result = await rm.start_routine("evening")
        assert "already" in result.lower()

    async def test_skip_during_focus(self, rm):
        with patch.object(rm, "shared") as mock_shared:
            mock_shared.current_focus_session = {"active": True, "task": "coding"}
            result = await rm.start_routine("morning", triggered_by="scheduled")
        assert "focus" in result.lower()
        assert rm._active_session is None


# ---------------------------------------------------------------------------
# Step advancement
# ---------------------------------------------------------------------------


class TestAdvanceStep:
    async def test_done_advances(self, rm):
        await rm.start_routine("morning")
        before = metric_value(ROUTINE_STEPS_ADVANCED, routine="morning", action="done")
        result = await rm.advance_step("done")
        assert metric_value(ROUTINE_STEPS_ADVANCED, routine="morning", action="done") - before == 1
        assert "Shower" in result
        assert rm._active_session.current_step_index == 1
        assert "meds" in rm._active_session.completed_steps

    async def test_skip_skippable_step(self, rm):
        await rm.start_routine("morning")
        await rm.advance_step("done")  # past meds
        before = metric_value(ROUTINE_STEPS_ADVANCED, routine="morning", action="skip")
        await rm.advance_step("skip")  # skip shower
        assert "shower" in rm._active_session.skipped_steps
        assert metric_value(ROUTINE_STEPS_ADVANCED, routine="morning", action="skip") - before == 1

    async def test_skip_non_skippable_rejected(self, rm):
        await rm.start_routine("morning")
        result = await rm.advance_step("skip")  # meds not skippable
        assert "can't skip" in result.lower()
        assert rm._active_session.current_step_index == 0

    async def test_complete_all_steps(self, rm):
        before = metric_value(ROUTINE_COMPLETED, routine="morning")
        await rm.start_routine("morning")
        await rm.advance_step("done")  # meds
        await rm.advance_step("done")  # shower
        with patch.object(rm, "_record_routine_progress"):
            result = await rm.advance_step("done")  # breakfast (last)
        assert "done" in result.lower()
        assert rm._active_session is None
        assert metric_value(ROUTINE_COMPLETED, routine="morning") - before == 1

    async def test_no_active_session(self, rm):
        result = await rm.advance_step("done")
        assert "no routine" in result.lower()


# ---------------------------------------------------------------------------
# Pause / Resume / Stop
# ---------------------------------------------------------------------------


class TestPauseResumeStop:
    async def test_pause(self, rm):
        await rm.start_routine("morning")
        result = await rm.advance_step("pause")
        assert "paused" in result.lower()
        assert rm._active_session.paused is True

    async def test_resume(self, rm):
        await rm.start_routine("morning")
        await rm.advance_step("pause")
        result = await rm.advance_step("resume")
        assert "resumed" in result.lower()
        assert rm._active_session.paused is False

    async def test_advance_while_paused(self, rm):
        await rm.start_routine("morning")
        await rm.advance_step("pause")
        result = await rm.advance_step("done")
        assert "paused" in result.lower()

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


class TestStatus:
    async def test_status_active(self, rm):
        await rm.start_routine("morning")
        result = await rm.get_routine_status()
        assert "step 1 of 3" in result
        assert "Take your meds" in result

    async def test_status_no_session(self, rm):
        result = await rm.get_routine_status()
        assert "no routine" in result.lower()


# ---------------------------------------------------------------------------
# Prompt context
# ---------------------------------------------------------------------------


class TestPromptContext:
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


class TestDeliverNudgeAutoEnd:
    """Covers the post-cap safety net added after the 2026-04-17 overnight bug.

    Previously: past cap, if step not skippable / auto-skip disabled, the nudge
    job just kept firing template-2 ("I'll move past X soon") forever.
    Now: past cap, auto-skip if possible, else advance_step("stop") cleanly.
    """

    async def test_under_cap_announces_normally(self, rm, mock_voice, monkeypatch):
        """nudge_count <= nudge_max → normal nudge fires, session continues."""
        await rm.start_routine("morning")
        mock_voice.reset_mock()

        monkeypatch.setattr(rm.shared, "ROUTINE_NUDGE_MAX", 3, raising=False)
        monkeypatch.setattr(rm.shared, "ROUTINE_AUTO_SKIP", True, raising=False)
        terminal = (ROUTINE_AUTO_ENDED, ROUTINE_AUTO_SKIPPED)
        before = [metric_value(c, routine="morning", step="meds") for c in terminal]

        with patch.object(rm, "advance_step", new_callable=AsyncMock) as mock_advance:
            await rm._deliver_nudge()

        assert rm._active_session is not None
        assert rm._active_session.nudge_count == 1
        mock_voice.assert_awaited_once()
        # Normal nudge path should NOT trigger advance_step or the terminal counters
        mock_advance.assert_not_called()
        assert [metric_value(c, routine="morning", step="meds") for c in terminal] == before

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
        before = metric_value(ROUTINE_AUTO_SKIPPED, routine="morning", step="shower")

        with patch.object(rm, "advance_step", new_callable=AsyncMock) as mock_advance:
            await rm._deliver_nudge()

        mock_advance.assert_awaited_once_with("skip")
        assert metric_value(ROUTINE_AUTO_SKIPPED, routine="morning", step="shower") - before == 1

    async def test_past_cap_non_skippable_stops(self, rm, mock_voice, monkeypatch, caplog):
        """Past cap + step.skippable=False → advance_step('stop'), WARNING logged."""
        # Start evening routine: single step evening_meds with skippable=False
        await rm.start_routine("evening")
        current_step = rm._active_session.steps[0]
        assert current_step.skippable is False

        monkeypatch.setattr(rm.shared, "ROUTINE_NUDGE_MAX", 2, raising=False)
        monkeypatch.setattr(rm.shared, "ROUTINE_AUTO_SKIP", True, raising=False)
        rm._active_session.nudge_count = 2  # next nudge will push count > 2
        before = metric_value(ROUTINE_AUTO_ENDED, routine="evening", step="evening_meds")

        with (
            patch.object(rm, "advance_step", new_callable=AsyncMock) as mock_advance,
            caplog.at_level(logging.WARNING, logger="orchestrator.routine_manager"),
        ):
            await rm._deliver_nudge()

        mock_advance.assert_awaited_once_with("stop")
        # The load-bearing counter: a stuck non-skippable step is visible in Grafana.
        assert metric_value(ROUTINE_AUTO_ENDED, routine="evening", step="evening_meds") - before == 1
        # WARNING log mentions "Auto-ending" and the step id
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("Auto-ending" in r.getMessage() and "evening_meds" in r.getMessage() for r in warnings), (
            f"Expected 'Auto-ending'+step-id WARNING; got: {[r.getMessage() for r in warnings]}"
        )

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

    async def test_no_active_session_is_noop(self, rm, mock_voice):
        """_active_session=None → no announcement, no advance."""
        rm._active_session = None
        with patch.object(rm, "advance_step", new_callable=AsyncMock) as mock_advance:
            await rm._deliver_nudge()
        mock_voice.assert_not_called()
        mock_advance.assert_not_called()

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


class TestAdvanceStepSelfcareBridge:
    """Covers the reverse-direction bridge added 2026-04-17: completing a
    routine step via advance_step('done') calls mark_selfcare_from_routine_step
    so the scheduled selfcare nudge for that action doesn't fire later.

    Regression guard: before this bridge, completing evening_meds via the
    routine still left the selfcare gate open, so check_selfcare would fire
    'did you take your Guanfacine' nudges at the next 15-min cycle.
    """

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
# _fire_step_ha_action — shared.ha_client + DND gate
# ---------------------------------------------------------------------------


class TestFireStepHaAction:
    @pytest.fixture
    def ha(self, monkeypatch):
        client = SimpleNamespace(call_service=AsyncMock(return_value=SimpleNamespace(success=True, message="ok")))
        monkeypatch.setattr(shared, "ha_client", client)
        monkeypatch.setattr(shared, "DND_ACTIVE", False)
        return client

    def _step(self, ha_action):
        return RoutineStep(id="lights", label="Lights on", ha_action=ha_action)

    async def test_calls_shared_ha_client_when_dnd_off(self, ha):
        await _REAL_FIRE_STEP(
            self._step({"entity_id": "light.kitchen", "service": "turn_on", "data": {"brightness": 50}})
        )
        ha.call_service.assert_awaited_once_with("light.kitchen", "turn_on", {"brightness": 50})

    async def test_data_defaults_to_empty_dict(self, ha):
        await _REAL_FIRE_STEP(self._step({"entity_id": "light.kitchen", "service": "turn_on"}))
        ha.call_service.assert_awaited_once_with("light.kitchen", "turn_on", {})

    async def test_skipped_under_dnd(self, ha, monkeypatch):
        monkeypatch.setattr(shared, "DND_ACTIVE", True)
        await _REAL_FIRE_STEP(self._step({"entity_id": "light.kitchen", "service": "turn_on"}))
        ha.call_service.assert_not_awaited()

    @pytest.mark.parametrize(
        "action",
        [None, {}, {"entity_id": "light.kitchen"}, {"service": "turn_on"}, {"entity_id": "", "service": "turn_on"}],
    )
    async def test_no_call_without_complete_action(self, ha, action):
        await _REAL_FIRE_STEP(self._step(action))
        ha.call_service.assert_not_awaited()

    async def test_ha_exception_is_swallowed(self, ha):
        ha.call_service.side_effect = RuntimeError("HA down")
        await _REAL_FIRE_STEP(self._step({"entity_id": "light.kitchen", "service": "turn_on"}))  # no raise
