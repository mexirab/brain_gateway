"""
Tests for the selfcare -> routine bridge in selfcare_manager.py (F-006/F-008).

Covers:
  - _step_matches_selfcare_action: word-boundary regex over step id + label.
  - _maybe_advance_routine_for_action: fire-and-forget bridge that advances
    the active routine step when a matching selfcare action is logged.
  - _infer_selfcare_action: maps a RoutineStep back to a selfcare action label.
  - record_medication_logged / record_hydration_logged / record_movement_logged:
    public sync helpers that update module state + persist a selfcare_log row
    so sibling subsystems (routine_manager, workout_manager) can advance the
    selfcare gate without duplicating log_selfcare's branching.
  - mark_selfcare_from_routine_step: dispatcher called from advance_step("done")
    so completing a routine step also marks the matching selfcare gate.

Does NOT exercise log_selfcare end-to-end because the bridge is dispatched
via asyncio.create_task, which races under pytest. The bridge function is
tested directly via `await`.

Context: on 2026-04-17 the user logged meds at 21:15 via selfcare_log but the
evening routine stayed stuck on evening_meds and nudged all night. This test
module locks in the behavior that prevents a regression of that bug.
"""

import logging
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# Import guard — skip if orchestrator deps not available locally
# ---------------------------------------------------------------------------


def _can_import():
    try:
        from orchestrator import selfcare_manager  # noqa: F401

        return True
    except (ImportError, ModuleNotFoundError):
        return False


_skip_no_deps = pytest.mark.skipif(
    not _can_import(),
    reason="selfcare_manager requires full orchestrator dependencies",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sc():
    """Return the selfcare_manager module."""
    if not _can_import():
        pytest.skip("deps unavailable")
    from orchestrator import selfcare_manager

    return selfcare_manager


@pytest.fixture
def rm():
    """Return the routine_manager module with state reset."""
    if not _can_import():
        pytest.skip("deps unavailable")
    from orchestrator import routine_manager

    routine_manager._active_session = None
    yield routine_manager
    routine_manager._active_session = None


def _make_step(sc_module, step_id: str, label: str, skippable: bool = True):
    """Build a RoutineStep via the real dataclass from routine_manager."""
    from orchestrator.routine_manager import RoutineStep

    return RoutineStep(id=step_id, label=label, skippable=skippable)


def _make_session(rm_module, steps, current_index: int = 0):
    """Build and install a fake RoutineSession on routine_manager."""
    from orchestrator.routine_manager import RoutineSession

    rm_module._active_session = RoutineSession(
        routine_id="evening",
        display_name="Evening Routine",
        started_at=datetime.now(),
        current_step_index=current_index,
        steps=list(steps),
    )
    return rm_module._active_session


# ---------------------------------------------------------------------------
# _step_matches_selfcare_action — pure function, word-boundary regex
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestStepMatchesSelfcareAction:
    def test_evening_meds_matches_medication(self, sc):
        step = _make_step(sc, "evening_meds", "Take your evening meds")
        assert sc._step_matches_selfcare_action(step, "medication") is True

    def test_breakfast_matches_meal(self, sc):
        step = _make_step(sc, "breakfast", "Eat breakfast")
        assert sc._step_matches_selfcare_action(step, "meal") is True

    def test_premeditated_does_not_match_medication(self, sc):
        """Word-boundary guard: 'med' substring inside 'premeditated' must NOT match."""
        step = _make_step(sc, "premeditated_plan", "premeditated review")
        assert sc._step_matches_selfcare_action(step, "medication") is False

    def test_stretch_matches_movement(self, sc):
        step = _make_step(sc, "stretch_goals", "Stretch goals review")
        assert sc._step_matches_selfcare_action(step, "movement") is True

    def test_shower_does_not_match_meal(self, sc):
        step = _make_step(sc, "shower", "Shower")
        assert sc._step_matches_selfcare_action(step, "meal") is False

    def test_none_step_returns_false(self, sc):
        assert sc._step_matches_selfcare_action(None, "medication") is False

    def test_unknown_action_returns_false(self, sc):
        step = _make_step(sc, "unknown", "?")
        assert sc._step_matches_selfcare_action(step, "unknown_action") is False


# ---------------------------------------------------------------------------
# _maybe_advance_routine_for_action — the bridge
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestMaybeAdvanceRoutineForAction:
    @pytest.mark.asyncio
    async def test_no_active_routine_is_noop(self, sc, rm):
        """No active routine → silently returns, advance_step not called."""
        assert rm._active_session is None
        with patch.object(rm, "advance_step", new_callable=AsyncMock) as mock_advance:
            await sc._maybe_advance_routine_for_action("medication")
        mock_advance.assert_not_called()

    @pytest.mark.asyncio
    async def test_matching_step_advances_with_done(self, sc, rm, caplog):
        """Active routine + current step matches → advance_step('done') at INFO."""
        step = _make_step(sc, "evening_meds", "Take your evening meds", skippable=False)
        _make_session(rm, [step])

        with (
            patch.object(rm, "advance_step", new_callable=AsyncMock) as mock_advance,
            caplog.at_level(logging.INFO, logger="orchestrator.selfcare_manager"),
        ):
            await sc._maybe_advance_routine_for_action("medication")

        mock_advance.assert_awaited_once_with("done")
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert any("Advancing routine step" in r.getMessage() for r in infos), (
            f"Expected 'Advancing routine step' INFO log; got: {[r.getMessage() for r in infos]}"
        )

    @pytest.mark.asyncio
    async def test_non_matching_step_is_noop(self, sc, rm):
        """Active routine but current step doesn't match → advance_step not called."""
        step = _make_step(sc, "shower", "Shower")
        _make_session(rm, [step])

        with patch.object(rm, "advance_step", new_callable=AsyncMock) as mock_advance:
            await sc._maybe_advance_routine_for_action("medication")

        mock_advance.assert_not_called()

    @pytest.mark.asyncio
    async def test_advance_step_exception_is_swallowed(self, sc, rm, caplog):
        """advance_step raises → bridge catches, logs ERROR with exc_info, no propagation."""
        step = _make_step(sc, "evening_meds", "Take your evening meds", skippable=False)
        _make_session(rm, [step])

        async def _boom(*args, **kwargs):
            raise RuntimeError("routine exploded")

        with (
            patch.object(rm, "advance_step", new=_boom),
            caplog.at_level(logging.ERROR, logger="orchestrator.selfcare_manager"),
        ):
            # Must NOT raise
            await sc._maybe_advance_routine_for_action("medication")

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert any("Routine bridge failed" in r.getMessage() for r in errors), (
            f"Expected 'Routine bridge failed' ERROR; got: {[r.getMessage() for r in errors]}"
        )
        # exc_info attached (LogRecord.exc_info is a 3-tuple when exc_info=True)
        assert any(r.exc_info is not None for r in errors), "Expected exc_info on ERROR record"

    @pytest.mark.asyncio
    async def test_current_index_past_end_is_noop(self, sc, rm):
        """current_step_index >= len(steps) → silently returns, no advance."""
        step = _make_step(sc, "evening_meds", "Take your evening meds")
        _make_session(rm, [step], current_index=5)  # past the end

        with patch.object(rm, "advance_step", new_callable=AsyncMock) as mock_advance:
            await sc._maybe_advance_routine_for_action("medication")

        mock_advance.assert_not_called()


# ---------------------------------------------------------------------------
# _infer_selfcare_action — RoutineStep → selfcare action label
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestInferSelfcareAction:
    """Reverse-direction mapping used by mark_selfcare_from_routine_step.

    Unlike _step_matches_selfcare_action (which takes an action and returns a
    bool), this inspects a step and returns the single action it corresponds
    to, or None. Iteration order of _ACTION_KEYWORDS matters — tests pin
    behavior, not order."""

    def test_evening_meds_maps_to_medication(self, sc):
        step = _make_step(sc, "evening_meds", "Take your evening meds")
        assert sc._infer_selfcare_action(step) == "medication"

    def test_meds_maps_to_medication(self, sc):
        step = _make_step(sc, "meds", "Take your meds")
        assert sc._infer_selfcare_action(step) == "medication"

    def test_breakfast_maps_to_meal(self, sc):
        step = _make_step(sc, "breakfast", "Eat breakfast")
        assert sc._infer_selfcare_action(step) == "meal"

    def test_hydrate_maps_to_water(self, sc):
        step = _make_step(sc, "hydrate", "Drink water")
        assert sc._infer_selfcare_action(step) == "water"

    def test_stretch_maps_to_movement(self, sc):
        step = _make_step(sc, "stretch", "Stretch out")
        assert sc._infer_selfcare_action(step) == "movement"

    def test_shower_maps_to_none(self, sc):
        """Shower doesn't match any selfcare keyword → None."""
        step = _make_step(sc, "shower", "Shower")
        assert sc._infer_selfcare_action(step) is None

    def test_none_step_returns_none(self, sc):
        assert sc._infer_selfcare_action(None) is None


# ---------------------------------------------------------------------------
# record_medication_logged — public sync helper
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestRecordMedicationLogged:
    """The generic-key fix: routine labels like 'routine:meds' don't contain
    'morning' or 'evening', so _expand_med_confirmation can't infer the
    window. record_medication_logged must set the generic 'medication' key
    unconditionally so _check_meds's primary gate fires regardless."""

    def test_sets_label_and_generic_keys(self, sc):
        """Both label-keyed and generic 'medication' keys must be set."""
        with patch("orchestrator.state_store.save_selfcare_log") as mock_save:
            sc.record_medication_logged("routine:meds")

        # Label key (lowercased)
        assert "routine:meds" in sc._state.last_med_confirmation
        # Generic key — the fix
        assert "medication" in sc._state.last_med_confirmation
        # Both timestamps near-now
        now = datetime.now()
        for key in ("routine:meds", "medication"):
            ts = sc._state.last_med_confirmation[key]
            assert abs((now - ts).total_seconds()) < 5

        mock_save.assert_called_once_with("medication", "routine:meds")

    def test_label_is_lowercased(self, sc):
        """Label is stored under its lowercased form to match _check_meds lookup."""
        with patch("orchestrator.state_store.save_selfcare_log"):
            sc.record_medication_logged("Routine:EVENING_Meds")

        assert "routine:evening_meds" in sc._state.last_med_confirmation
        assert "Routine:EVENING_Meds" not in sc._state.last_med_confirmation

    def test_evening_meds_expands_to_individual_meds(self, sc):
        """When the label mentions 'evening meds', _expand_med_confirmation
        should add entries for each individual evening med (e.g. guanfacine)."""
        fake_meds = {
            "daily": {
                "morning": [{"name": "Adderall"}],
                "evening": [{"name": "Guanfacine"}, {"name": "Melatonin"}],
            }
        }
        with (
            patch("orchestrator.state_store.save_selfcare_log"),
            patch("orchestrator.data_manager.get_medications", return_value=fake_meds),
        ):
            # NOTE: label must contain 'evening' AND 'med' for the expander to fire
            sc.record_medication_logged("routine:evening_meds")

        # Generic key set
        assert "medication" in sc._state.last_med_confirmation
        # Individual evening meds set via _expand_med_confirmation
        assert "guanfacine" in sc._state.last_med_confirmation
        assert "melatonin" in sc._state.last_med_confirmation
        # Morning meds NOT touched
        assert "adderall" not in sc._state.last_med_confirmation

    def test_persists_via_save_selfcare_log(self, sc):
        """save_selfcare_log is called exactly once with ('medication', label)."""
        with patch("orchestrator.state_store.save_selfcare_log") as mock_save:
            sc.record_medication_logged("Adderall")
        mock_save.assert_called_once_with("medication", "Adderall")

    def test_default_label_is_medication(self, sc):
        with patch("orchestrator.state_store.save_selfcare_log") as mock_save:
            sc.record_medication_logged()
        assert "medication" in sc._state.last_med_confirmation
        mock_save.assert_called_once_with("medication", "medication")


# ---------------------------------------------------------------------------
# record_hydration_logged — public sync helper
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestRecordHydrationLogged:
    def test_sets_last_hydration_nudge(self, sc):
        assert sc._state.last_hydration_nudge is None
        with patch("orchestrator.state_store.save_selfcare_log") as mock_save:
            sc.record_hydration_logged("glass of water")

        assert sc._state.last_hydration_nudge is not None
        assert abs((datetime.now() - sc._state.last_hydration_nudge).total_seconds()) < 5
        mock_save.assert_called_once_with("water", "glass of water")


# ---------------------------------------------------------------------------
# record_movement_logged — public sync helper
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestRecordMovementLogged:
    """Called by workout_manager.log_set so lifting a set counts as movement
    and suppresses the 'you've been sitting for N minutes' nudge."""

    def test_sets_both_movement_and_sitting(self, sc):
        """Must set BOTH last_movement_nudge AND sitting_since — the movement
        nudge gate uses whichever is more recent."""
        with patch("orchestrator.state_store.save_selfcare_log") as mock_save:
            sc.record_movement_logged("set:Bench Press")

        now = datetime.now()
        assert sc._state.last_movement_nudge is not None
        assert sc._state.sitting_since is not None
        assert abs((now - sc._state.last_movement_nudge).total_seconds()) < 5
        assert abs((now - sc._state.sitting_since).total_seconds()) < 5
        mock_save.assert_called_once_with("movement", "set:Bench Press")

    def test_timestamps_are_identical(self, sc):
        """Both timestamps come from the same datetime.now() call — test that
        they are equal (not merely close) to prove the single-call pattern."""
        with patch("orchestrator.state_store.save_selfcare_log"):
            sc.record_movement_logged("walk")

        assert sc._state.last_movement_nudge == sc._state.sitting_since


# ---------------------------------------------------------------------------
# mark_selfcare_from_routine_step — dispatcher
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestMarkSelfcareFromRoutineStep:
    """The reverse-direction bridge: advance_step('done') calls this with the
    completed step so selfcare state stays in sync with routine completion.
    Silent no-op for steps that don't map to any selfcare action."""

    def test_none_step_is_noop(self, sc):
        """step=None → silent no-op, no helper called."""
        with (
            patch.object(sc, "record_medication_logged") as mock_med,
            patch.object(sc, "record_meal_logged") as mock_meal,
            patch.object(sc, "record_hydration_logged") as mock_water,
            patch.object(sc, "record_movement_logged") as mock_move,
        ):
            sc.mark_selfcare_from_routine_step(None)

        mock_med.assert_not_called()
        mock_meal.assert_not_called()
        mock_water.assert_not_called()
        mock_move.assert_not_called()

    def test_medication_step_calls_record_medication_logged(self, sc):
        step = _make_step(sc, "evening_meds", "Take your evening meds")
        with (
            patch.object(sc, "record_medication_logged") as mock_med,
            patch.object(sc, "record_meal_logged") as mock_meal,
            patch.object(sc, "record_hydration_logged") as mock_water,
            patch.object(sc, "record_movement_logged") as mock_move,
        ):
            sc.mark_selfcare_from_routine_step(step)

        mock_med.assert_called_once_with("routine:evening_meds")
        mock_meal.assert_not_called()
        mock_water.assert_not_called()
        mock_move.assert_not_called()

    def test_meal_step_calls_record_meal_logged(self, sc):
        step = _make_step(sc, "breakfast", "Eat breakfast")
        with (
            patch.object(sc, "record_medication_logged") as mock_med,
            patch.object(sc, "record_meal_logged") as mock_meal,
            patch.object(sc, "record_hydration_logged") as mock_water,
            patch.object(sc, "record_movement_logged") as mock_move,
        ):
            sc.mark_selfcare_from_routine_step(step)

        mock_meal.assert_called_once_with("routine:breakfast")
        mock_med.assert_not_called()
        mock_water.assert_not_called()
        mock_move.assert_not_called()

    def test_water_step_calls_record_hydration_logged(self, sc):
        step = _make_step(sc, "hydrate", "Drink water")
        with (
            patch.object(sc, "record_medication_logged") as mock_med,
            patch.object(sc, "record_meal_logged") as mock_meal,
            patch.object(sc, "record_hydration_logged") as mock_water,
            patch.object(sc, "record_movement_logged") as mock_move,
        ):
            sc.mark_selfcare_from_routine_step(step)

        mock_water.assert_called_once_with("routine:hydrate")
        mock_med.assert_not_called()
        mock_meal.assert_not_called()
        mock_move.assert_not_called()

    def test_movement_step_calls_record_movement_logged(self, sc):
        step = _make_step(sc, "stretch", "Stretch out")
        with (
            patch.object(sc, "record_medication_logged") as mock_med,
            patch.object(sc, "record_meal_logged") as mock_meal,
            patch.object(sc, "record_hydration_logged") as mock_water,
            patch.object(sc, "record_movement_logged") as mock_move,
        ):
            sc.mark_selfcare_from_routine_step(step)

        mock_move.assert_called_once_with("routine:stretch")
        mock_med.assert_not_called()
        mock_meal.assert_not_called()
        mock_water.assert_not_called()

    def test_unmatched_step_is_noop(self, sc):
        """Step with no match in _ACTION_KEYWORDS → no helper called."""
        step = _make_step(sc, "shower", "Shower")
        with (
            patch.object(sc, "record_medication_logged") as mock_med,
            patch.object(sc, "record_meal_logged") as mock_meal,
            patch.object(sc, "record_hydration_logged") as mock_water,
            patch.object(sc, "record_movement_logged") as mock_move,
        ):
            sc.mark_selfcare_from_routine_step(step)

        mock_med.assert_not_called()
        mock_meal.assert_not_called()
        mock_water.assert_not_called()
        mock_move.assert_not_called()
