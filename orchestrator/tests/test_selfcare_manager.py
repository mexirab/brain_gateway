"""
Tests for selfcare_manager.py (F-008) — meal/med/hydration/movement logging,
nudge timing, quiet hours, and smart suppression.

Requires full orchestrator dependencies (runs inside Docker).
"""

from datetime import datetime, time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _can_import():
    try:
        import selfcare_manager  # noqa: F401

        return True
    except (ImportError, ModuleNotFoundError):
        return False


_skip_no_deps = pytest.mark.skipif(
    not _can_import(),
    reason="selfcare_manager requires full orchestrator dependencies",
)


@pytest.fixture(autouse=True)
def reset_state():
    """Reset selfcare_manager state and mock deps."""
    if not _can_import():
        pytest.skip("deps unavailable")

    import selfcare_manager
    from selfcare_manager import SelfCareState

    selfcare_manager._state = SelfCareState()

    with (
        patch.object(selfcare_manager, "_announce_voice", new_callable=AsyncMock),
        patch.object(selfcare_manager, "shared") as mock_shared,
    ):
        mock_shared.SELFCARE_ENABLED = True
        mock_shared.MEAL_NUDGE_HOURS = 4
        mock_shared.HYDRATION_INTERVAL = 90
        mock_shared.MOVEMENT_INTERVAL = 90
        mock_shared.QUIET_HOURS_START = "22:00"
        mock_shared.QUIET_HOURS_END = "07:00"
        mock_shared.TIMEZONE = "America/Chicago"
        mock_shared.current_focus_session = {"active": False}
        mock_shared.profile = MagicMock()
        mock_shared.profile.user_name = "Nadim"

        yield {"module": selfcare_manager, "mock_shared": mock_shared}

    selfcare_manager._state = SelfCareState()
    patch.stopall()


@pytest.fixture
def sm(reset_state):
    return reset_state["module"]


@pytest.fixture
def mock_shared(reset_state):
    return reset_state["mock_shared"]


# ---------------------------------------------------------------------------
# Logging actions
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestLogSelfcare:
    @pytest.mark.asyncio
    async def test_log_meal(self, sm):
        result = await sm.log_selfcare("meal", "lunch")
        assert "lunch" in result
        assert sm._state.last_meal_reported is not None

    @pytest.mark.asyncio
    async def test_log_medication(self, sm):
        result = await sm.log_selfcare("medication", "Adderall")
        assert "Logged" in result
        assert "adderall" in sm._state.last_med_confirmation

    @pytest.mark.asyncio
    async def test_log_water(self, sm):
        result = await sm.log_selfcare("water")
        assert "hydrated" in result.lower()

    @pytest.mark.asyncio
    async def test_log_movement(self, sm):
        result = await sm.log_selfcare("movement")
        assert "moving" in result.lower()
        assert sm._state.sitting_since is not None

    @pytest.mark.asyncio
    async def test_log_unknown(self, sm):
        result = await sm.log_selfcare("sleep")
        assert "Unknown" in result


# ---------------------------------------------------------------------------
# Meal checks
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestMealCheck:
    def test_no_meal_after_noon(self, sm):
        now = datetime(2026, 3, 20, 13, 30)
        result = sm._check_meals(now)
        assert result is not None
        assert "no meals" in result.lower()

    def test_no_nudge_before_noon(self, sm):
        now = datetime(2026, 3, 20, 10, 0)
        result = sm._check_meals(now)
        assert result is None

    def test_nudge_after_hours_since_meal(self, sm, mock_shared):
        mock_shared.MEAL_NUDGE_HOURS = 4
        sm._state.last_meal_reported = datetime(2026, 3, 20, 8, 0)
        now = datetime(2026, 3, 20, 13, 0)  # 5 hours later
        result = sm._check_meals(now)
        assert result is not None
        assert "hours" in result

    def test_no_nudge_recent_meal(self, sm, mock_shared):
        mock_shared.MEAL_NUDGE_HOURS = 4
        sm._state.last_meal_reported = datetime(2026, 3, 20, 12, 0)
        now = datetime(2026, 3, 20, 13, 0)  # 1 hour later
        result = sm._check_meals(now)
        assert result is None


# ---------------------------------------------------------------------------
# Hydration checks
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestHydrationCheck:
    def test_first_call_no_nudge(self, sm):
        now = datetime(2026, 3, 20, 10, 0)
        result = sm._check_hydration(now)
        assert result is None  # initializes timer

    def test_nudge_after_interval(self, sm, mock_shared):
        mock_shared.HYDRATION_INTERVAL = 90
        sm._state.last_hydration_nudge = datetime(2026, 3, 20, 8, 0)
        now = datetime(2026, 3, 20, 10, 0)  # 120 min later
        result = sm._check_hydration(now)
        assert result is not None
        assert "water" in result.lower()


# ---------------------------------------------------------------------------
# Movement checks
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestMovementCheck:
    def test_first_call_no_nudge(self, sm):
        now = datetime(2026, 3, 20, 10, 0)
        result = sm._check_movement(now)
        assert result is None  # initializes timer

    def test_nudge_after_sitting(self, sm, mock_shared):
        mock_shared.MOVEMENT_INTERVAL = 90
        sm._state.sitting_since = datetime(2026, 3, 20, 8, 0)
        sm._state.last_movement_nudge = None
        now = datetime(2026, 3, 20, 10, 0)  # 120 min sitting
        result = sm._check_movement(now)
        assert result is not None
        assert "sitting" in result.lower()


# ---------------------------------------------------------------------------
# Quiet hours
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestQuietHours:
    def test_in_quiet_hours_night(self, sm):
        assert sm._in_quiet_hours(time(23, 0), time(22, 0), time(7, 0)) is True

    def test_in_quiet_hours_early_morning(self, sm):
        assert sm._in_quiet_hours(time(5, 0), time(22, 0), time(7, 0)) is True

    def test_not_in_quiet_hours(self, sm):
        assert sm._in_quiet_hours(time(12, 0), time(22, 0), time(7, 0)) is False

    def test_same_day_range(self, sm):
        # Non-wrapping: 13:00-15:00
        assert sm._in_quiet_hours(time(14, 0), time(13, 0), time(15, 0)) is True
        assert sm._in_quiet_hours(time(12, 0), time(13, 0), time(15, 0)) is False


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestStatus:
    @pytest.mark.asyncio
    async def test_status_empty(self, sm):
        status = await sm.get_selfcare_status()
        assert status["last_meal"] is None
        assert status["meds_confirmed_today"] == {}

    @pytest.mark.asyncio
    async def test_status_with_data(self, sm):
        await sm.log_selfcare("meal", "sandwich")
        await sm.log_selfcare("medication", "Adderall")
        status = await sm.get_selfcare_status()
        assert status["last_meal"] is not None
        assert "adderall" in status["meds_confirmed_today"]
