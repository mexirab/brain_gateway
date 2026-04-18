"""
Tests for the selfcare -> routine bridge in selfcare_manager.py (F-006/F-008).

Covers:
  - _step_matches_selfcare_action: word-boundary regex over step id + label.
  - _maybe_advance_routine_for_action: fire-and-forget bridge that advances
    the active routine step when a matching selfcare action is logged.

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
