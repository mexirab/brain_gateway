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
from contextlib import ExitStack
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator import routine_manager
from orchestrator.metrics import SELFCARE_LOGGED
from orchestrator.routine_manager import RoutineSession, RoutineStep
from orchestrator.tests._helpers import metric_value

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sc(selfcare_fresh_state):
    """The selfcare_manager module, with fresh state (fixtures_selfcare)."""
    return selfcare_fresh_state


@pytest.fixture
def rm():
    """The routine_manager module with no active session."""
    routine_manager._active_session = None
    yield routine_manager
    routine_manager._active_session = None


def _make_step(step_id: str, label: str, skippable: bool = True):
    return RoutineStep(id=step_id, label=label, skippable=skippable)


def _make_session(rm_module, steps, current_index: int = 0):
    """Build and install a RoutineSession on routine_manager."""
    rm_module._active_session = RoutineSession(
        routine_id="evening",
        display_name="Evening Routine",
        started_at=datetime.now(),
        current_step_index=current_index,
        steps=list(steps),
    )
    return rm_module._active_session


def _logged(action):
    return metric_value(SELFCARE_LOGGED, action=action)


# ---------------------------------------------------------------------------
# _step_matches_selfcare_action — pure function, word-boundary regex
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("step_id", "label", "action", "matches"),
    [
        pytest.param("evening_meds", "Take your evening meds", "medication", True, id="meds"),
        pytest.param("breakfast", "Eat breakfast", "meal", True, id="breakfast"),
        # Word-boundary guard: 'med' inside 'premeditated' must NOT match.
        pytest.param("premeditated_plan", "premeditated review", "medication", False, id="premeditated"),
        pytest.param("stretch_goals", "Stretch goals review", "movement", True, id="stretch"),
        pytest.param("shower", "Shower", "meal", False, id="shower-not-meal"),
        pytest.param("unknown", "?", "unknown_action", False, id="unknown-action"),
    ],
)
def test_step_matches_selfcare_action(sc, step_id, label, action, matches):
    assert sc._step_matches_selfcare_action(_make_step(step_id, label), action) is matches


def test_step_matches_none_step_is_false(sc):
    assert sc._step_matches_selfcare_action(None, "medication") is False


# ---------------------------------------------------------------------------
# _maybe_advance_routine_for_action — the bridge
# ---------------------------------------------------------------------------


class TestMaybeAdvanceRoutineForAction:
    async def test_no_active_routine_is_noop(self, sc, rm):
        """No active routine → silently returns, advance_step not called."""
        assert rm._active_session is None
        with patch.object(rm, "advance_step", new_callable=AsyncMock) as mock_advance:
            await sc._maybe_advance_routine_for_action("medication")
        mock_advance.assert_not_called()

    async def test_matching_step_advances_with_done(self, sc, rm, caplog):
        """Active routine + current step matches → advance_step('done') at INFO."""
        step = _make_step("evening_meds", "Take your evening meds", skippable=False)
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

    async def test_non_matching_step_is_noop(self, sc, rm):
        """Active routine but current step doesn't match → advance_step not called."""
        _make_session(rm, [_make_step("shower", "Shower")])

        with patch.object(rm, "advance_step", new_callable=AsyncMock) as mock_advance:
            await sc._maybe_advance_routine_for_action("medication")

        mock_advance.assert_not_called()

    async def test_advance_step_exception_is_swallowed(self, sc, rm, caplog):
        """advance_step raises → bridge catches, logs ERROR with exc_info, no propagation."""
        _make_session(rm, [_make_step("evening_meds", "Take your evening meds", skippable=False)])

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

    async def test_current_index_past_end_is_noop(self, sc, rm):
        """current_step_index >= len(steps) → silently returns, no advance."""
        _make_session(rm, [_make_step("evening_meds", "Take your evening meds")], current_index=5)

        with patch.object(rm, "advance_step", new_callable=AsyncMock) as mock_advance:
            await sc._maybe_advance_routine_for_action("medication")

        mock_advance.assert_not_called()


# ---------------------------------------------------------------------------
# _infer_selfcare_action — RoutineStep → selfcare action label
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        pytest.param(("evening_meds", "Take your evening meds"), "medication", id="evening-meds"),
        pytest.param(("meds", "Take your meds"), "medication", id="meds"),
        pytest.param(("breakfast", "Eat breakfast"), "meal", id="breakfast"),
        pytest.param(("hydrate", "Drink water"), "water", id="hydrate"),
        pytest.param(("stretch", "Stretch out"), "movement", id="stretch"),
        pytest.param(("shower", "Shower"), None, id="shower-none"),
        pytest.param(None, None, id="no-step"),
    ],
)
def test_infer_selfcare_action(sc, step, expected):
    """Reverse-direction mapping used by mark_selfcare_from_routine_step: the
    single action a step corresponds to, or None (pins behavior, not order)."""
    assert sc._infer_selfcare_action(_make_step(*step) if step else None) == expected


# ---------------------------------------------------------------------------
# record_medication_logged — public sync helper
# ---------------------------------------------------------------------------


class TestRecordMedicationLogged:
    """The generic-key fix: routine labels like 'routine:meds' don't contain
    'morning' or 'evening', so _expand_med_confirmation can't infer the
    window. record_medication_logged must set the generic 'medication' key
    unconditionally so _check_meds's primary gate fires regardless."""

    def test_sets_label_and_generic_keys(self, sc):
        """Both label-keyed and generic 'medication' keys must be set; one row,
        one metric increment."""
        before = _logged("medication")
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
        assert _logged("medication") - before == 1

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

    def test_default_label_is_medication(self, sc):
        with patch("orchestrator.state_store.save_selfcare_log") as mock_save:
            sc.record_medication_logged()
        assert "medication" in sc._state.last_med_confirmation
        mock_save.assert_called_once_with("medication", "medication")


# ---------------------------------------------------------------------------
# record_hydration_logged / record_movement_logged — public sync helpers
# ---------------------------------------------------------------------------


def test_record_hydration_logged_sets_last_hydration_nudge(sc):
    assert sc._state.last_hydration_nudge is None
    before = _logged("water")
    with patch("orchestrator.state_store.save_selfcare_log") as mock_save:
        sc.record_hydration_logged("glass of water")

    assert sc._state.last_hydration_nudge is not None
    assert abs((datetime.now() - sc._state.last_hydration_nudge).total_seconds()) < 5
    mock_save.assert_called_once_with("water", "glass of water")
    assert _logged("water") - before == 1


class TestRecordMovementLogged:
    """Called by workout_manager.log_set so lifting a set counts as movement
    and suppresses the 'you've been sitting for N minutes' nudge."""

    def test_sets_both_movement_and_sitting(self, sc):
        """Must set BOTH last_movement_nudge AND sitting_since — the movement
        nudge gate uses whichever is more recent."""
        before = _logged("movement")
        with patch("orchestrator.state_store.save_selfcare_log") as mock_save:
            sc.record_movement_logged("set:Bench Press")

        now = datetime.now()
        assert sc._state.last_movement_nudge is not None
        assert sc._state.sitting_since is not None
        assert abs((now - sc._state.last_movement_nudge).total_seconds()) < 5
        assert abs((now - sc._state.sitting_since).total_seconds()) < 5
        mock_save.assert_called_once_with("movement", "set:Bench Press")
        assert _logged("movement") - before == 1

    def test_timestamps_are_identical(self, sc):
        """Both timestamps come from the same datetime.now() call — test that
        they are equal (not merely close) to prove the single-call pattern."""
        with patch("orchestrator.state_store.save_selfcare_log"):
            sc.record_movement_logged("walk")

        assert sc._state.last_movement_nudge == sc._state.sitting_since


# ---------------------------------------------------------------------------
# mark_selfcare_from_routine_step — dispatcher
# ---------------------------------------------------------------------------

_RECORDERS = (
    "record_medication_logged",
    "record_meal_logged",
    "record_hydration_logged",
    "record_movement_logged",
)


@pytest.mark.parametrize(
    ("step", "called"),
    [
        pytest.param(None, None, id="no-step"),
        pytest.param(("evening_meds", "Take your evening meds"), "record_medication_logged", id="meds"),
        pytest.param(("breakfast", "Eat breakfast"), "record_meal_logged", id="meal"),
        pytest.param(("hydrate", "Drink water"), "record_hydration_logged", id="water"),
        pytest.param(("stretch", "Stretch out"), "record_movement_logged", id="movement"),
        pytest.param(("shower", "Shower"), None, id="unmatched"),
    ],
)
def test_mark_selfcare_from_routine_step(sc, step, called):
    """The reverse-direction bridge: advance_step('done') calls this with the
    completed step; exactly the matching helper runs with 'routine:<id>', and
    steps that map to no selfcare action are a silent no-op."""
    step_obj = _make_step(*step) if step else None
    with ExitStack() as stack:
        mocks = {name: stack.enter_context(patch.object(sc, name)) for name in _RECORDERS}
        sc.mark_selfcare_from_routine_step(step_obj)

    for name, mock in mocks.items():
        if name == called:
            mock.assert_called_once_with(f"routine:{step_obj.id}")
        else:
            mock.assert_not_called()
