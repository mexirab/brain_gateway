"""
Tests for selfcare_manager.py (F-008) — meal/med/hydration/movement logging,
nudge timing, quiet hours, and smart suppression.

Requires full orchestrator dependencies (runs inside Docker).
"""

from datetime import datetime, time, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator import data_manager, selfcare_schedule, state_store
from orchestrator.metrics import SELFCARE_LOGGED
from orchestrator.tests._helpers import metric_value


@pytest.fixture(autouse=True)
def reset_state(selfcare_fresh_state):
    """Fresh selfcare_manager state (fixtures_selfcare) and mocked deps."""
    selfcare_manager = selfcare_fresh_state
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


@pytest.fixture
def sm(reset_state):
    return reset_state["module"]


@pytest.fixture
def mock_shared(reset_state):
    return reset_state["mock_shared"]


def _logged(action):
    return metric_value(SELFCARE_LOGGED, action=action)


# ---------------------------------------------------------------------------
# Logging actions + bgw_selfcare_logged_total (one increment per logical log)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "detail", "reply", "state_set"),
    [
        pytest.param("meal", "lunch", "lunch", lambda st: st.last_meal_reported is not None, id="meal"),
        pytest.param(
            "medication", "Adderall", "logged", lambda st: "adderall" in st.last_med_confirmation, id="medication"
        ),
        pytest.param("water", None, "hydrated", lambda st: st.last_hydration_nudge is not None, id="water"),
        pytest.param("movement", None, "moving", lambda st: st.sitting_since is not None, id="movement"),
    ],
)
async def test_log_selfcare(sm, action, detail, reply, state_set):
    """Each action answers, moves its nudge gate, and bumps the counter exactly
    once. The meal path is the trap: log_selfcare('meal') routes through
    record_meal_logged(), the SOLE meal incrementer — two increments would be
    the double-count bug."""
    before = _logged(action)
    result = await sm.log_selfcare(action, detail)
    assert reply in result.lower()
    assert state_set(sm._state)
    assert _logged(action) - before == 1


async def test_log_unknown_action_changes_nothing(sm):
    """Unknown action returns early before any counter touch."""
    actions = ("meal", "medication", "water", "movement")
    before = {a: _logged(a) for a in actions}
    result = await sm.log_selfcare("sleep")
    assert "Unknown" in result
    assert {a: _logged(a) for a in actions} == before


def test_record_meal_logged_helper_increments_once(sm):
    """The other record_*_logged helpers' counters are checked in test_selfcare_bridge."""
    before = _logged("meal")
    sm.record_meal_logged("dinner")
    assert _logged("meal") - before == 1


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
    with (
        patch.object(selfcare_schedule, "category_enabled", return_value=True),
        patch.object(selfcare_schedule, "category_active_hours", return_value=(None, None)),
        patch.object(selfcare_schedule, "category_interval_minutes", side_effect=lambda name, fallback: fallback),
    ):
        yield


@pytest.mark.usefixtures("pin_meal_schedule")
@pytest.mark.parametrize(
    ("last_meal", "now", "expected"),
    [
        pytest.param(None, datetime(2026, 3, 20, 13, 30), "no meals", id="none-after-noon"),
        pytest.param(None, datetime(2026, 3, 20, 10, 0), None, id="none-before-noon"),
        pytest.param(datetime(2026, 3, 20, 8, 0), datetime(2026, 3, 20, 13, 0), "lunch", id="5h-lunch"),
        pytest.param(datetime(2026, 3, 20, 12, 0), datetime(2026, 3, 20, 18, 0), "dinner", id="6h-dinner"),
        pytest.param(datetime(2026, 3, 20, 10, 0), datetime(2026, 3, 20, 15, 0), "snack", id="5h-snack"),
        pytest.param(datetime(2026, 3, 20, 12, 0), datetime(2026, 3, 20, 13, 0), None, id="recent-meal"),
    ],
)
def test_check_meals(sm, last_meal, now, expected):
    """MEAL_NUDGE_HOURS = 4 (fixture): no meal by noon nudges, then a nudge
    named for the time of day once 4 h have passed since the last meal."""
    sm._state.last_meal_reported = last_meal
    result = sm._check_meals(now)
    if expected is None:
        assert result is None
    else:
        assert expected in result.lower()


# ---------------------------------------------------------------------------
# Daily reset
# ---------------------------------------------------------------------------


class TestDailyReset:
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


class TestMovementCheck:
    def test_first_call_no_nudge(self, sm):
        now = datetime(2026, 3, 20, 10, 0)
        result = sm._check_movement(now)
        assert result is None  # initializes timer


# ---------------------------------------------------------------------------
# Quiet hours
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "start", "end", "quiet"),
    [
        pytest.param(time(23, 0), time(22, 0), time(7, 0), True, id="night"),
        pytest.param(time(5, 0), time(22, 0), time(7, 0), True, id="early-morning"),
        pytest.param(time(12, 0), time(22, 0), time(7, 0), False, id="midday"),
        pytest.param(time(14, 0), time(13, 0), time(15, 0), True, id="same-day-inside"),
        pytest.param(time(12, 0), time(13, 0), time(15, 0), False, id="same-day-outside"),
    ],
)
def test_in_quiet_hours(sm, now, start, end, quiet):
    assert sm._in_quiet_hours(now, start, end) is quiet


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class TestStatus:
    async def test_status_empty(self, sm):
        status = await sm.get_selfcare_status()
        assert status["last_meal"] is None
        assert status["meds_confirmed_today"] == {}

    async def test_status_with_data(self, sm):
        await sm.log_selfcare("meal", "sandwich")
        await sm.log_selfcare("medication", "Adderall")
        status = await sm.get_selfcare_status()
        assert status["last_meal"] is not None
        assert "adderall" in status["meds_confirmed_today"]

    async def test_status_clears_stale_yesterday_state_on_read(self, sm):
        """Regression: get_selfcare_status() must apply the daily reset on read
        so stale yesterday timestamps don't surface as e.g. 'sitting 32 hours'
        when the nudge loop has been paused (presence=away) past midnight."""
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
# Meds nudge windows — driven by category_times("meds"), not a hardcoded 8pm
# ---------------------------------------------------------------------------


class TestCheckMedsWindow:
    _MEDS = {"daily": {"morning": [{"name": "Vyvanse"}], "evening": [{"name": "Guanfacine"}]}}

    def _ctx(self, times):
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
        now = datetime(2026, 3, 20, 21, 15)
        with patch.object(selfcare_schedule, "category_enabled", return_value=False):
            assert sm._check_meds(now, now) is None


# ---------------------------------------------------------------------------
# Per-med weekday scheduling — `days` gate (drug-holiday, e.g. no Vyvanse on weekends)
# ---------------------------------------------------------------------------

# 2026-03-20 = Friday, 03-21 = Saturday, 03-22 = Sunday.
_FRIDAY_AM = datetime(2026, 3, 20, 7, 30)
_SATURDAY_AM = datetime(2026, 3, 21, 7, 30)
_SUNDAY_AM = datetime(2026, 3, 22, 7, 30)


class TestCheckMedsWeekdays:
    def _ctx(self, meds):
        return (
            patch.object(selfcare_schedule, "category_enabled", return_value=True),
            patch.object(selfcare_schedule, "category_times", return_value=["07:00", "21:00"]),
            patch.object(data_manager, "get_medications", return_value=meds),
        )

    def test_weekday_only_med_suppressed_on_weekend(self, sm):
        meds = {"daily": {"morning": [{"name": "Vyvanse", "days": ["mon", "tue", "wed", "thu", "fri"]}], "evening": []}}
        a, b, c = self._ctx(meds)
        with a, b, c:
            assert sm._check_meds(_SATURDAY_AM, _SATURDAY_AM) is None
            assert sm._check_meds(_SUNDAY_AM, _SUNDAY_AM) is None

    def test_weekday_only_med_fires_on_weekday(self, sm):
        meds = {"daily": {"morning": [{"name": "Vyvanse", "days": ["mon", "tue", "wed", "thu", "fri"]}], "evening": []}}
        a, b, c = self._ctx(meds)
        with a, b, c:
            assert sm._check_meds(_FRIDAY_AM, _FRIDAY_AM) == "Hey, did you take your Vyvanse?"

    def test_no_days_field_unchanged_behavior(self, sm):
        """Regression guard: a med with no `days` behaves exactly as before —
        nudges every day including weekends."""
        meds = {"daily": {"morning": [{"name": "Vyvanse"}], "evening": []}}
        a, b, c = self._ctx(meds)
        with a, b, c:
            assert sm._check_meds(_SATURDAY_AM, _SATURDAY_AM) == "Hey, did you take your Vyvanse?"

    def test_evening_med_weekday_gate(self, sm):
        meds = {
            "daily": {"morning": [], "evening": [{"name": "Guanfacine", "days": ["mon", "tue", "wed", "thu", "fri"]}]}
        }
        sat_pm = datetime(2026, 3, 21, 21, 15)
        fri_pm = datetime(2026, 3, 20, 21, 15)
        a, b, c = self._ctx(meds)
        with a, b, c:
            assert sm._check_meds(sat_pm, sat_pm) is None
            assert sm._check_meds(fri_pm, fri_pm) == "Hey, did you take your Guanfacine?"

    def test_malformed_days_fails_open(self, sm):
        """A typo / empty / wrong-type `days` must NOT silently drop the reminder
        — it fails open to 'every day' (safe failure for a safety-critical nudge).

        The junk-token cases (['weekdays'], ['M-F'], ['bogus']) are the ones that
        previously failed CLOSED: a non-empty list of unrecognized tokens produced
        a non-empty set that never matched today, suppressing the med every day
        while every display path showed it as 'every day'. Must fail open now."""
        for bad in ([], "friday", ["   "], 5, ["weekdays"], ["M-F"], ["M", "T", "W", "Th", "F"], ["bogus"]):
            meds = {"daily": {"morning": [{"name": "Vyvanse", "days": bad}], "evening": []}}
            # Assert on a weekday AND a weekend — a fail-open med fires on both.
            for day in (_SATURDAY_AM, _FRIDAY_AM):
                a, b, c = self._ctx(meds)
                with a, b, c:
                    assert sm._check_meds(day, day) == "Hey, did you take your Vyvanse?", (bad, day)

    def test_helper_normalizes_case_and_length(self, sm):
        """`days` entries are normalized (lowercase, first 3 chars) so 'Saturday'
        / 'SAT' match Sat."""
        assert sm._med_allowed_today({"days": ["Saturday", "SUN"]}, _SATURDAY_AM) is True
        assert sm._med_allowed_today({"days": ["Mon"]}, _SATURDAY_AM) is False
        assert sm._med_allowed_today({}, _SATURDAY_AM) is True

    def test_partial_junk_days_still_enforces_valid_tokens(self, sm):
        """['mon','bogus'] must still restrict to Monday — the junk is dropped but
        the recognizable weekday is honored (intersection, not all-or-nothing)."""
        assert sm._med_allowed_today({"days": ["mon", "bogus"]}, _FRIDAY_AM) is False
        monday = datetime(2026, 3, 16, 9, 0)
        assert sm._med_allowed_today({"days": ["mon", "bogus"]}, monday) is True

    def test_weekday_mapping_is_locale_independent(self, sm):
        """Today-abbrev comes from datetime.weekday() (index), not strftime('%a'),
        so a non-English LC_TIME can't silently drop every restricted med. Verify
        the index→abbrev mapping is correct for each weekday."""
        # 2026-03-16 is a Monday; walk the whole week.
        for offset, abbr in enumerate(["mon", "tue", "wed", "thu", "fri", "sat", "sun"]):
            day = datetime(2026, 3, 16 + offset, 9, 0)
            assert sm._med_allowed_today({"days": [abbr]}, day) is True, abbr
            other = "sun" if abbr != "sun" else "mon"
            assert sm._med_allowed_today({"days": [other]}, day) is False, abbr


# ---------------------------------------------------------------------------
# Broad-vs-specific med confirmation classification
# ---------------------------------------------------------------------------


class TestIsBroadMedConfirmation:
    """`_is_broad_med_confirmation` decides whether restoring a selfcare_log med
    row should re-arm the generic 'medication' gate. Broad = window-wide
    confirmations (Telegram ✓ Done, routine bridge, grouped 'morning meds');
    specific = a single named drug, which must NOT re-arm the generic gate."""

    @pytest.mark.parametrize(
        "label",
        [
            "telegram:medication nudge",  # Telegram ✓ Done tap (the regression source)
            "routine:meds",  # routine bridge step label
            "morning meds (vyvanse, wellbutrin)",  # grouped confirmation
            "medication",  # bare default detail
        ],
    )
    def test_broad_labels(self, sm, label):
        assert sm._is_broad_med_confirmation(label) is True

    @pytest.mark.parametrize("label", ["vyvanse", "guanfacine", "naltrexone"])
    def test_specific_named_meds_are_not_broad(self, sm, label):
        assert sm._is_broad_med_confirmation(label) is False


# ---------------------------------------------------------------------------
# _restore_state — rebuilds the generic 'medication' gate from today's
# broad confirmations so a Telegram ✓ Done tap survives an orchestrator restart
# ---------------------------------------------------------------------------


class TestRestoreState:
    """Regression coverage for the meds-nudge-restore-gate fix.

    The bug: _restore_state rebuilt only per-label keys, leaving the generic
    'medication' gate (which _check_meds reads FIRST) empty. So a Telegram
    ✓ Done tap's suppression was lost on every restart and the med nudge
    re-fired after each deploy.

    Deps are imported lazily inside _restore_state:
      `from orchestrator.state_store import get_last_selfcare, get_selfcare_today`
      `from orchestrator.data_manager import get_medications`
    so we patch at those module-level names.
    """

    def _run_restore(self, sm, *, today_meds, medications, meds_raises=False):
        """Invoke _restore_state with mocked state_store + data_manager.

        `today_meds` is what get_selfcare_today('medication') returns; the other
        get_last_selfcare(...) calls (meal/water/movement) return None.
        `medications` is what get_medications() returns (or raises if
        meds_raises)."""
        gm = (
            MagicMock(side_effect=RuntimeError("meds config unavailable"))
            if meds_raises
            else MagicMock(return_value=medications)
        )
        with (
            patch.object(state_store, "get_last_selfcare", return_value=None),
            patch.object(state_store, "get_selfcare_today", return_value=today_meds),
            patch.object(data_manager, "get_medications", gm),
        ):
            sm._restore_state()

    def test_broad_telegram_done_row_arms_generic_gate(self, sm):
        """CORE REGRESSION: a broad Telegram ✓ Done row logged this morning must
        set the generic 'medication' key to that timestamp on restore."""
        ts = datetime(2026, 3, 20, 7, 45)  # morning, hour < 12
        self._run_restore(
            sm,
            today_meds=[{"detail": "telegram:medication nudge", "logged_at": ts.isoformat()}],
            medications={"daily": {"morning": [{"name": "Vyvanse"}], "evening": [{"name": "Guanfacine"}]}},
        )
        assert sm._state.last_med_confirmation.get("medication") == ts

    def test_specific_named_med_row_does_not_arm_generic_gate(self, sm):
        """A single named-med row ('guanfacine') keeps per-med suppression but
        must NOT arm the generic 'medication' key (would suppress the window's
        other meds)."""
        ts = datetime(2026, 3, 20, 21, 10)
        self._run_restore(
            sm,
            today_meds=[{"detail": "guanfacine", "logged_at": ts.isoformat()}],
            medications={"daily": {"morning": [{"name": "Vyvanse"}], "evening": [{"name": "Guanfacine"}]}},
        )
        assert "medication" not in sm._state.last_med_confirmation
        assert sm._state.last_med_confirmation.get("guanfacine") == ts

    def test_multiple_broad_rows_generic_holds_latest_timestamp(self, sm):
        early = datetime(2026, 3, 20, 7, 5)
        late = datetime(2026, 3, 20, 9, 30)
        self._run_restore(
            sm,
            today_meds=[
                {"detail": "telegram:medication nudge", "logged_at": early.isoformat()},
                {"detail": "routine:meds", "logged_at": late.isoformat()},
            ],
            medications={"daily": {"morning": [{"name": "Vyvanse"}], "evening": [{"name": "Guanfacine"}]}},
        )
        assert sm._state.last_med_confirmation.get("medication") == late

    def test_configured_med_name_with_marker_substring_stays_specific(self, sm):
        """Guard: a configured med named 'Medsure' contains the 'meds' marker but
        must be treated as SPECIFIC (it's in configured_meds), so its named-med
        row does NOT arm the generic gate."""
        ts = datetime(2026, 3, 20, 7, 45)
        self._run_restore(
            sm,
            today_meds=[{"detail": "Medsure", "logged_at": ts.isoformat()}],
            medications={"daily": {"morning": [{"name": "Medsure"}], "evening": []}},
        )
        assert "medication" not in sm._state.last_med_confirmation
        assert sm._state.last_med_confirmation.get("medsure") == ts

    def test_get_medications_raising_still_completes(self, sm):
        """get_medications raising must not abort restore: configured_meds falls
        back to empty set and the broad row still arms the generic gate."""
        ts = datetime(2026, 3, 20, 7, 45)
        self._run_restore(
            sm,
            today_meds=[{"detail": "telegram:medication nudge", "logged_at": ts.isoformat()}],
            medications=None,
            meds_raises=True,
        )
        # No exception propagated, and the broad row still armed the gate.
        assert sm._state.last_med_confirmation.get("medication") == ts
