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
        from orchestrator import selfcare_manager  # noqa: F401

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

    from orchestrator import selfcare_manager
    from orchestrator.selfcare_manager import SelfCareState

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


@pytest.fixture
def pin_meal_schedule():
    """Pin selfcare_schedule accessors so meal checks depend only on the
    injected `now`, not the deployed data/selfcare_schedule.yaml (which on a
    configured box can disable meal nudges or narrow active_hours, flaking
    these assertions). category_active_hours -> (None, None) = always active;
    category_interval_minutes -> the fallback the caller passes."""
    from orchestrator import selfcare_schedule

    with (
        patch.object(selfcare_schedule, "category_enabled", return_value=True),
        patch.object(selfcare_schedule, "category_active_hours", return_value=(None, None)),
        patch.object(selfcare_schedule, "category_interval_minutes", side_effect=lambda name, fallback: fallback),
    ):
        yield


@_skip_no_deps
@pytest.mark.usefixtures("pin_meal_schedule")
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

    def test_nudge_after_hours_since_meal_lunch(self, sm, mock_shared):
        mock_shared.MEAL_NUDGE_HOURS = 4
        sm._state.last_meal_reported = datetime(2026, 3, 20, 8, 0)
        now = datetime(2026, 3, 20, 13, 0)  # 5 hours later, 1pm
        result = sm._check_meals(now)
        assert result is not None
        assert "lunch" in result.lower()

    def test_nudge_after_hours_since_meal_dinner(self, sm, mock_shared):
        mock_shared.MEAL_NUDGE_HOURS = 4
        sm._state.last_meal_reported = datetime(2026, 3, 20, 12, 0)
        now = datetime(2026, 3, 20, 18, 0)  # 6 hours later, 6pm
        result = sm._check_meals(now)
        assert result is not None
        assert "dinner" in result.lower()

    def test_nudge_afternoon_snack(self, sm, mock_shared):
        mock_shared.MEAL_NUDGE_HOURS = 4
        sm._state.last_meal_reported = datetime(2026, 3, 20, 10, 0)
        now = datetime(2026, 3, 20, 15, 0)  # 5 hours later, 3pm
        result = sm._check_meals(now)
        assert result is not None
        assert "snack" in result.lower()

    def test_no_nudge_recent_meal(self, sm, mock_shared):
        mock_shared.MEAL_NUDGE_HOURS = 4
        sm._state.last_meal_reported = datetime(2026, 3, 20, 12, 0)
        now = datetime(2026, 3, 20, 13, 0)  # 1 hour later
        result = sm._check_meals(now)
        assert result is None


# ---------------------------------------------------------------------------
# Daily reset
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestDailyReset:
    @pytest.mark.asyncio
    async def test_meal_reset_on_new_day(self, sm, mock_shared):
        """last_meal_reported should reset to None at day boundary."""
        mock_shared.PRESENCE_ENABLED = False
        sm._state.last_meal_reported = datetime(2026, 3, 19, 20, 0)  # yesterday 8pm
        sm._state.sitting_since = datetime(2026, 3, 19, 20, 0)
        sm._state.last_hydration_nudge = datetime(2026, 3, 19, 20, 0)
        sm._state.last_movement_nudge = datetime(2026, 3, 19, 20, 0)

        with (
            patch.object(sm, "_check_meds", return_value=None),
            patch.object(sm, "_check_meals", wraps=sm._check_meals) as mock_meals,
            patch.object(sm, "_check_movement", return_value=None),
            patch.object(sm, "_check_hydration", return_value=None),
            patch.object(sm, "_send_notification", new_callable=AsyncMock),
        ):
            await sm.check_selfcare()

        # After check_selfcare runs on a new day, meal state should be cleared
        assert sm._state.last_meal_reported is None


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

    @pytest.mark.asyncio
    async def test_status_clears_stale_yesterday_state_on_read(self, sm):
        """Regression: get_selfcare_status() must apply the daily reset on read
        so stale yesterday timestamps don't surface as e.g. 'sitting 32 hours'
        when the nudge loop has been paused (presence=away) past midnight."""
        from datetime import datetime, timedelta

        yesterday = datetime.now() - timedelta(hours=26)
        sm._state.last_meal_reported = yesterday
        sm._state.sitting_since = yesterday

        status = await sm.get_selfcare_status()

        assert status["last_meal"] is None, "last_meal should reset to None after midnight rollover"
        assert status["sitting_minutes"] == 0, "sitting_minutes should zero out at midnight rollover"
        assert sm._state.last_meal_reported is None
        assert sm._state.sitting_since is not None
        assert sm._state.sitting_since.date() == datetime.now().date()


# ---------------------------------------------------------------------------
# Prometheus metric — SELFCARE_LOGGED increments exactly once per logical log
# ---------------------------------------------------------------------------


def _selfcare_value(action):
    """Read the bgw_selfcare_logged_total sample for an action, 0 if unseen."""
    from orchestrator.metrics import SELFCARE_LOGGED

    try:
        return SELFCARE_LOGGED.labels(action=action)._value.get()
    except Exception:
        return 0.0


@_skip_no_deps
class TestSelfcareMetric:
    """One increment per logical self-care log, no double-counting.

    The meal path is the trap: log_selfcare('meal') routes through
    record_meal_logged(), which is the SOLE incrementer for meals — so a meal
    log must bump the counter exactly once, not twice (the bug would be if
    both log_selfcare and record_meal_logged incremented).
    """

    @pytest.mark.asyncio
    async def test_log_meal_increments_once(self, sm):
        before = _selfcare_value("meal")
        await sm.log_selfcare("meal", "lunch")
        after = _selfcare_value("meal")
        assert after - before == 1, "meal must increment exactly once (no double-count via record_meal_logged)"

    @pytest.mark.asyncio
    async def test_log_medication_increments_once(self, sm):
        before = _selfcare_value("medication")
        await sm.log_selfcare("medication", "Adderall")
        after = _selfcare_value("medication")
        assert after - before == 1

    @pytest.mark.asyncio
    async def test_log_water_increments_once(self, sm):
        before = _selfcare_value("water")
        await sm.log_selfcare("water")
        after = _selfcare_value("water")
        assert after - before == 1

    @pytest.mark.asyncio
    async def test_log_movement_increments_once(self, sm):
        before = _selfcare_value("movement")
        await sm.log_selfcare("movement")
        after = _selfcare_value("movement")
        assert after - before == 1

    @pytest.mark.asyncio
    async def test_log_unknown_does_not_increment(self, sm):
        # Unknown action returns early before any counter touch.
        before_meal = _selfcare_value("meal")
        before_med = _selfcare_value("medication")
        await sm.log_selfcare("sleep")
        assert _selfcare_value("meal") == before_meal
        assert _selfcare_value("medication") == before_med

    def test_record_meal_logged_helper_increments_once(self, sm):
        before = _selfcare_value("meal")
        sm.record_meal_logged("dinner")
        after = _selfcare_value("meal")
        assert after - before == 1

    def test_record_medication_logged_helper_increments_once(self, sm):
        before = _selfcare_value("medication")
        sm.record_medication_logged("routine:meds")
        after = _selfcare_value("medication")
        assert after - before == 1

    def test_record_hydration_logged_helper_increments_once(self, sm):
        before = _selfcare_value("water")
        sm.record_hydration_logged("glass")
        after = _selfcare_value("water")
        assert after - before == 1

    def test_record_movement_logged_helper_increments_once(self, sm):
        before = _selfcare_value("movement")
        sm.record_movement_logged("set:squat")
        after = _selfcare_value("movement")
        assert after - before == 1


# ---------------------------------------------------------------------------
# Meds nudge windows — driven by category_times("meds"), not a hardcoded 8pm
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestCheckMedsWindow:
    _MEDS = {"daily": {"morning": [{"name": "Vyvanse"}], "evening": [{"name": "Guanfacine"}]}}

    def _ctx(self, times):
        from orchestrator import data_manager, selfcare_schedule

        return (
            patch.object(selfcare_schedule, "category_enabled", return_value=True),
            patch.object(selfcare_schedule, "category_times", return_value=times),
            patch.object(data_manager, "get_medications", return_value=self._MEDS),
        )

    def test_evening_nudge_fires_at_configured_9pm(self, sm):
        now = datetime(2026, 3, 20, 21, 15)  # inside the configured 21:00 window
        a, b, c = self._ctx(["07:00", "21:00"])
        with a, b, c:
            assert sm._check_meds(now, now) == "Hey, did you take your Guanfacine?"

    def test_no_evening_nudge_at_old_hardcoded_8pm(self, sm):
        """Regression: 8pm is BEFORE the configured 9pm — the old hardcoded
        20:00-22:00 window would (wrongly) have fired here."""
        now = datetime(2026, 3, 20, 20, 0)
        a, b, c = self._ctx(["07:00", "21:00"])
        with a, b, c:
            assert sm._check_meds(now, now) is None

    def test_morning_nudge_at_configured_time(self, sm):
        now = datetime(2026, 3, 20, 7, 30)
        a, b, c = self._ctx(["07:00", "21:00"])
        with a, b, c:
            assert sm._check_meds(now, now) == "Hey, did you take your Vyvanse?"

    def test_confirmed_evening_med_not_renudged(self, sm):
        now = datetime(2026, 3, 20, 21, 15)
        sm._state.last_med_confirmation["guanfacine"] = datetime(2026, 3, 20, 21, 5)
        a, b, c = self._ctx(["07:00", "21:00"])
        with a, b, c:
            assert sm._check_meds(now, now) is None

    def test_disabled_meds_category_no_nudge(self, sm):
        from orchestrator import selfcare_schedule

        now = datetime(2026, 3, 20, 21, 15)
        with patch.object(selfcare_schedule, "category_enabled", return_value=False):
            assert sm._check_meds(now, now) is None
