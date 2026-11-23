"""
Movement seen by the phone resets the sitting clock (selfcare_manager.
_refresh_movement_from_health): HA iPhone-app Apple Health steps / Watch
stand-hours sensors, read every check_selfcare cycle. Also the sitting clock
not counting the night (_sitting_start).

Requires full orchestrator dependencies (runs inside Docker).
"""

import logging
import time as _time
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

STEPS = "sensor.test_phone_health_steps"
STAND = "sensor.test_phone_stand_hours"


def _can_import():
    try:
        from orchestrator import selfcare_manager  # noqa: F401

        return True
    except (ImportError, ModuleNotFoundError):
        return False


pytestmark = pytest.mark.skipif(not _can_import(), reason="needs orchestrator deps")


def _metric(name, labels):
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, labels) or 0.0


def _ha_iso(local_naive: datetime) -> str:
    """A local naive time as HA would send it (UTC ISO with offset)."""
    return local_naive.astimezone().astimezone(UTC).isoformat()


class FakeHA:
    """Per-entity HA state responses for shared._http.get."""

    def __init__(self):
        self.states = {}
        self.status = {}

    def set(self, entity, state, changed=None, reported=None, raw_changed=None):
        body = {"entity_id": entity, "state": state}
        if changed is not None:
            body["last_changed"] = _ha_iso(changed)
            body["last_updated"] = body["last_changed"]
        if raw_changed is not None:
            body["last_changed"] = raw_changed
        if reported is not None:
            body["last_reported"] = _ha_iso(reported)
        self.states[entity] = body

    async def get(self, url, headers=None, timeout=None):
        entity = url.rsplit("/", 1)[-1]
        resp = MagicMock()
        resp.status_code = self.status.get(entity, 200 if entity in self.states else 404)
        resp.json.return_value = self.states.get(entity, {})
        return resp


@pytest.fixture
def env():
    from orchestrator import selfcare_manager as sm
    from orchestrator import selfcare_schedule
    from orchestrator.selfcare_manager import SelfCareState

    sm._state = SelfCareState()
    sm._health = sm._HealthMovementState()
    ha = FakeHA()
    with (
        patch.object(sm, "shared") as mock_shared,
        patch.object(sm, "_resolve_inbox") as resolve_inbox,
        patch("orchestrator.state_store.save_selfcare_log") as save_log,
        patch.object(selfcare_schedule, "category_enabled", return_value=True),
        patch.object(selfcare_schedule, "category_active_hours", return_value=("09:00", "18:00")),
    ):
        mock_shared.MOVEMENT_STEPS_ENTITY = STEPS
        mock_shared.MOVEMENT_STAND_ENTITY = ""
        mock_shared.MOVEMENT_STEPS_THRESHOLD = 150
        mock_shared.HA_URL = "http://ha.test:8123"
        mock_shared.HA_TOKEN = "t" * 40
        mock_shared._http.get = AsyncMock(side_effect=ha.get)
        yield {"sm": sm, "shared": mock_shared, "ha": ha, "save_log": save_log, "resolve": resolve_inbox}
    sm._state = SelfCareState()
    sm._health = sm._HealthMovementState()


T0 = datetime(2026, 11, 13, 10, 0)


async def _read(sm, at):
    await sm._refresh_movement_from_health(at)


class TestStepsDetection:
    async def test_first_read_only_sets_baseline(self, env):
        sm, ha = env["sm"], env["ha"]
        sm._state.sitting_since = T0 - timedelta(hours=1)
        ha.set(STEPS, "4000", changed=T0 - timedelta(minutes=2))
        await _read(sm, T0)
        assert sm._state.sitting_since == T0 - timedelta(hours=1)
        env["save_log"].assert_not_called()

    async def test_steps_over_threshold_reset_clock_to_last_changed(self, env):
        sm, ha = env["sm"], env["ha"]
        sm._state.sitting_since = T0 - timedelta(hours=1)
        ha.set(STEPS, "4000", changed=T0 - timedelta(minutes=2))
        await _read(sm, T0)
        walked = T0 + timedelta(minutes=8)
        ha.set(STEPS, "4300", changed=walked)
        before = _metric("bgw_movement_auto_detected_total", {"source": "steps"})
        await _read(sm, T0 + timedelta(minutes=15))
        assert sm._state.sitting_since == walked
        assert sm._state.last_movement_nudge == walked
        env["save_log"].assert_called_once_with("movement", "health:steps")
        env["resolve"].assert_called_once_with("movement")
        assert _metric("bgw_movement_auto_detected_total", {"source": "steps"}) == before + 1

    async def test_steps_below_threshold_change_nothing(self, env):
        sm, ha = env["sm"], env["ha"]
        start = T0 - timedelta(hours=1)
        sm._state.sitting_since = start
        ha.set(STEPS, "4000", changed=T0)
        await _read(sm, T0)
        ha.set(STEPS, "4100", changed=T0 + timedelta(minutes=5))
        await _read(sm, T0 + timedelta(minutes=15))
        assert sm._state.sitting_since == start
        env["save_log"].assert_not_called()

    @pytest.mark.parametrize(
        ("threshold", "gained", "moved"),
        [
            pytest.param(150, 149, False, id="149-of-150"),
            pytest.param(150, 150, True, id="150-of-150"),
            pytest.param(500, 300, False, id="300-of-500"),
        ],
    )
    async def test_threshold_boundary(self, env, threshold, gained, moved):
        sm, ha = env["sm"], env["ha"]
        env["shared"].MOVEMENT_STEPS_THRESHOLD = threshold
        start = T0 - timedelta(hours=1)
        sm._state.sitting_since = start
        ha.set(STEPS, "4000", changed=T0)
        await _read(sm, T0)
        walked = T0 + timedelta(minutes=8)
        ha.set(STEPS, str(4000 + gained), changed=walked)
        await _read(sm, T0 + timedelta(minutes=15))
        assert sm._state.sitting_since == (walked if moved else start)

    async def test_slow_trickle_never_adds_up(self, env):
        """The baseline moves every read, so 100 steps per check never counts."""
        sm, ha = env["sm"], env["ha"]
        start = T0 - timedelta(hours=1)
        sm._state.sitting_since = start
        for i in range(5):
            ha.set(STEPS, str(4000 + 100 * i), changed=T0 + timedelta(minutes=15 * i))
            await _read(sm, T0 + timedelta(minutes=15 * i))
        assert sm._state.sitting_since == start

    async def test_last_changed_clamped_to_baseline_read_window(self, env):
        sm, ha = env["sm"], env["ha"]
        sm._state.sitting_since = T0 - timedelta(hours=2)
        ha.set(STEPS, "4000", changed=T0)
        await _read(sm, T0)
        # A last_changed older than the baseline read (odd HA timestamp) is
        # held at the baseline read; a future one is held at now.
        ha.set(STEPS, "4500", changed=T0 - timedelta(minutes=30))
        await _read(sm, T0 + timedelta(minutes=15))
        assert sm._state.sitting_since == T0
        ha.set(STEPS, "5000", changed=T0 + timedelta(hours=5))
        now = T0 + timedelta(minutes=30)
        await _read(sm, now)
        assert sm._state.sitting_since == now

    async def test_missing_last_changed_uses_now(self, env):
        sm, ha = env["sm"], env["ha"]
        sm._state.sitting_since = T0 - timedelta(hours=1)
        ha.set(STEPS, "4000")
        await _read(sm, T0)
        ha.set(STEPS, "4400")
        now = T0 + timedelta(minutes=15)
        await _read(sm, now)
        assert sm._state.sitting_since == now

    async def test_older_than_logged_movement_is_ignored(self, env):
        """A manual log newer than the phone's movement time wins."""
        sm, ha = env["sm"], env["ha"]
        ha.set(STEPS, "4000", changed=T0)
        await _read(sm, T0)
        sm._state.sitting_since = T0 + timedelta(minutes=12)  # user said "I stretched"
        ha.set(STEPS, "4500", changed=T0 + timedelta(minutes=5))
        before = _metric("bgw_movement_auto_detected_total", {"source": "steps"})
        await _read(sm, T0 + timedelta(minutes=15))
        assert sm._state.sitting_since == T0 + timedelta(minutes=12)
        env["save_log"].assert_not_called()
        assert _metric("bgw_movement_auto_detected_total", {"source": "steps"}) == before


class TestNewDay:
    async def test_midnight_drop_rebaselines_then_walking_counts(self, env):
        sm, ha = env["sm"], env["ha"]
        late = datetime(2026, 11, 12, 23, 50)
        ha.set(STEPS, "9000", changed=late)
        await _read(sm, late)
        # After midnight HA still holds yesterday's total (changed yesterday).
        morning = datetime(2026, 11, 13, 0, 5)
        sm._state.sitting_since = morning
        await _read(sm, morning)
        # The phone sends today's small count: a new day's total under the threshold.
        ha.set(STEPS, "40", changed=morning + timedelta(minutes=10))
        await _read(sm, morning + timedelta(minutes=15))
        assert sm._state.sitting_since == morning
        # Real walking after that counts.
        ha.set(STEPS, "400", changed=morning + timedelta(minutes=25))
        await _read(sm, morning + timedelta(minutes=30))
        assert sm._state.sitting_since == morning + timedelta(minutes=25)

    async def test_morning_walk_counts_when_the_first_report_comes_after_it(self, env):
        """Dog walk before the phone's first report of the day: the new day's
        total is all new steps, not a 'midnight reset' to re-baseline on."""
        sm, ha = env["sm"], env["ha"]
        late = datetime(2026, 11, 12, 22, 0)
        ha.set(STEPS, "9000", changed=late)
        await _read(sm, late)
        dawn = datetime(2026, 11, 13, 6, 0)
        sm._state.sitting_since = datetime(2026, 11, 13, 0, 5)
        await _read(sm, dawn)  # still yesterday's total
        report = datetime(2026, 11, 13, 7, 10)
        ha.set(STEPS, "2500", changed=report)  # first report of the day, after the walk
        await _read(sm, datetime(2026, 11, 13, 7, 15))
        assert sm._state.sitting_since == report
        env["save_log"].assert_called_once_with("movement", "health:steps")


class TestOvernightRestartAndStuckBaseline:
    async def test_ha_restart_overnight_is_not_a_walk_and_doesnt_poison_the_day(self, env):
        """HA restores yesterday's total with last_changed = restart time."""
        sm, ha = env["sm"], env["ha"]
        ha.set(STEPS, "9000", changed=datetime(2026, 11, 12, 23, 40))
        await _read(sm, datetime(2026, 11, 12, 23, 50))
        ha.set(STEPS, "9000", changed=datetime(2026, 11, 13, 3, 0))  # restart
        sm._state.sitting_since = datetime(2026, 11, 13, 0, 5)
        await _read(sm, datetime(2026, 11, 13, 3, 5))
        assert sm._state.sitting_since == datetime(2026, 11, 13, 0, 5)  # no fake walk
        env["save_log"].assert_not_called()
        ha.set(STEPS, "300", changed=datetime(2026, 11, 13, 7, 30))  # first real report
        await _read(sm, datetime(2026, 11, 13, 7, 45))
        ha.set(STEPS, "2000", changed=datetime(2026, 11, 13, 11, 40))
        await _read(sm, datetime(2026, 11, 13, 11, 45))
        assert sm._state.sitting_since == datetime(2026, 11, 13, 11, 40)
        ha.set(STEPS, "3500", changed=datetime(2026, 11, 13, 12, 5))
        await _read(sm, datetime(2026, 11, 13, 12, 15))
        assert sm._state.sitting_since == datetime(2026, 11, 13, 12, 5)  # the noon walk counts

    async def test_whole_box_reboot_overnight_no_fake_walk_and_recovers(self, env):
        """HA and the orchestrator share a box: after a reboot at 03:00 the
        first read ever is yesterday's total with a today last_changed. No
        walk is faked from it, and today's walking is credited by midday."""
        sm, ha = env["sm"], env["ha"]
        reset = datetime(2026, 11, 13, 0, 5)
        sm._state.sitting_since = reset
        ha.set(STEPS, "9000", changed=datetime(2026, 11, 13, 3, 0))  # restored by HA
        await _read(sm, datetime(2026, 11, 13, 3, 5))  # fresh process: first read
        ha.set(STEPS, "300", changed=datetime(2026, 11, 13, 7, 30))
        await _read(sm, datetime(2026, 11, 13, 7, 45))
        assert sm._state.sitting_since == reset
        ha.set(STEPS, "2000", changed=datetime(2026, 11, 13, 11, 40))
        await _read(sm, datetime(2026, 11, 13, 11, 45))
        ha.set(STEPS, "3500", changed=datetime(2026, 11, 13, 12, 5))
        await _read(sm, datetime(2026, 11, 13, 12, 15))
        assert sm._state.sitting_since == datetime(2026, 11, 13, 12, 5)

    async def test_stand_hours_after_an_overnight_restart(self, env):
        sm, ha, shared = env["sm"], env["ha"], env["shared"]
        shared.MOVEMENT_STEPS_ENTITY = ""
        shared.MOVEMENT_STAND_ENTITY = STAND
        ha.set(STAND, "12", changed=datetime(2026, 11, 12, 21, 0))
        await _read(sm, datetime(2026, 11, 12, 23, 50))
        ha.set(STAND, "12", changed=datetime(2026, 11, 13, 3, 0))
        await _read(sm, datetime(2026, 11, 13, 3, 5))
        ha.set(STAND, "1", changed=datetime(2026, 11, 13, 9, 10))
        await _read(sm, datetime(2026, 11, 13, 9, 15))
        sm._state.sitting_since = datetime(2026, 11, 13, 9, 15)
        ha.set(STAND, "2", changed=datetime(2026, 11, 13, 10, 5))
        await _read(sm, datetime(2026, 11, 13, 10, 15))
        assert sm._state.sitting_since == datetime(2026, 11, 13, 10, 5)

    async def test_a_drop_confirmed_by_a_second_report_repairs_the_baseline(self, env):
        sm, ha = env["sm"], env["ha"]
        start = T0 - timedelta(hours=1)
        sm._state.sitting_since = start
        ha.set(STEPS, "9000", changed=T0)  # a baseline that is too high
        await _read(sm, T0)
        ha.set(STEPS, "300", changed=T0 + timedelta(minutes=10))
        await _read(sm, T0 + timedelta(minutes=15))
        await _read(sm, T0 + timedelta(minutes=30))  # same report again: not a confirmation
        assert sm._health.baseline["steps"][0] == 9000
        ha.set(STEPS, "600", changed=T0 + timedelta(minutes=40))  # a second, different report
        await _read(sm, T0 + timedelta(minutes=45))
        assert sm._health.baseline["steps"][0] == 600
        assert sm._state.sitting_since == start  # the repair itself is no walk
        ha.set(STEPS, "1000", changed=T0 + timedelta(minutes=55))
        await _read(sm, T0 + timedelta(minutes=60))
        assert sm._state.sitting_since == T0 + timedelta(minutes=55)

    async def test_new_day_steps_reported_late_dont_move_the_clock_inside_movement_hours(self, env):
        """First report of the day lands at 10:20 with steps walked at an
        unknown time since midnight: no credit inside movement hours (no row,
        no resolved card), so the nudge due at 10:30 still comes."""
        sm, ha = env["sm"], env["ha"]
        ha.set(STEPS, "9000", changed=datetime(2026, 11, 12, 22, 0))
        await _read(sm, datetime(2026, 11, 12, 23, 50))
        await _read(sm, datetime(2026, 11, 13, 10, 0))  # still yesterday's value
        sm._state.sitting_since = datetime(2026, 11, 13, 0, 5)
        sm._state.last_movement_nudge = datetime(2026, 11, 13, 0, 5)
        ha.set(STEPS, "800", changed=datetime(2026, 11, 13, 10, 20))
        await _read(sm, datetime(2026, 11, 13, 10, 25))
        assert sm._state.sitting_since == datetime(2026, 11, 13, 0, 5)
        env["save_log"].assert_not_called()
        env["resolve"].assert_not_called()
        # The new baseline is today's count, so a real walk after it counts.
        assert sm._health.baseline["steps"][0] == 800
        from orchestrator import selfcare_schedule

        with (
            patch.object(selfcare_schedule, "category_interval_minutes", return_value=90),
            patch.object(sm, "_within_active_hours", return_value=True),
        ):
            assert sm._check_movement(datetime(2026, 11, 13, 10, 35)) is not None


class TestBadReadings:
    @pytest.mark.parametrize(
        ("state", "result"),
        [
            pytest.param("unavailable", "unavailable", id="unavailable"),
            pytest.param("unknown", "unavailable", id="unknown"),
            pytest.param("", "unavailable", id="empty"),
            pytest.param(None, "unavailable", id="null"),
            pytest.param("lots", "bad_value", id="text"),
            pytest.param("nan", "bad_value", id="nan"),
            pytest.param("-5", "bad_value", id="negative"),
            pytest.param("1e300", "bad_value", id="spike"),
            pytest.param(4500, "bad_value", id="not-a-string"),
        ],
    )
    async def test_bad_state_is_no_evidence(self, env, state, result):
        sm, ha = env["sm"], env["ha"]
        start = T0 - timedelta(hours=1)
        sm._state.sitting_since = start
        ha.set(STEPS, "4000", changed=T0)
        await _read(sm, T0)
        ha.set(STEPS, state, changed=T0 + timedelta(minutes=5))
        before = _metric("bgw_movement_health_read_total", {"kind": "steps", "result": result})
        await _read(sm, T0 + timedelta(minutes=15))
        assert _metric("bgw_movement_health_read_total", {"kind": "steps", "result": result}) == before + 1
        assert sm._state.sitting_since == start
        # The old baseline survives: the next good read still compares to 4000.
        ha.set(STEPS, "4300", changed=T0 + timedelta(minutes=25))
        await _read(sm, T0 + timedelta(minutes=30))
        assert sm._state.sitting_since == T0 + timedelta(minutes=25)

    async def test_dip_then_recovery_within_a_day_is_not_movement(self, env):
        sm, ha = env["sm"], env["ha"]
        start = T0 - timedelta(hours=1)
        sm._state.sitting_since = start
        ha.set(STEPS, "4000", changed=T0)
        await _read(sm, T0)
        for i, value in enumerate(("50", "4000", "0", "4020"), start=1):
            ha.set(STEPS, value, changed=T0 + timedelta(minutes=15 * i - 1))
            await _read(sm, T0 + timedelta(minutes=15 * i))
        assert sm._state.sitting_since == start
        env["save_log"].assert_not_called()

    @pytest.mark.parametrize(
        ("status", "result"),
        [
            pytest.param(404, "not_found", id="404"),
            pytest.param(401, "http_error", id="401"),
            pytest.param(500, "http_error", id="500"),
        ],
    )
    async def test_http_status_results(self, env, status, result):
        sm, ha = env["sm"], env["ha"]
        ha.set(STEPS, "4000")
        ha.status[STEPS] = status
        before = _metric("bgw_movement_health_read_total", {"kind": "steps", "result": result})
        await _read(sm, T0)
        assert _metric("bgw_movement_health_read_total", {"kind": "steps", "result": result}) == before + 1
        assert sm._health.baseline == {}

    async def test_connection_error_is_unreachable(self, env):
        sm = env["sm"]
        env["shared"]._http.get = AsyncMock(side_effect=OSError("boom"))
        before = _metric("bgw_movement_health_read_total", {"kind": "steps", "result": "unreachable"})
        await _read(sm, T0)
        assert _metric("bgw_movement_health_read_total", {"kind": "steps", "result": "unreachable"}) == before + 1

    @pytest.mark.parametrize(
        "body",
        [pytest.param(ValueError("not json"), id="not-json"), pytest.param(["4000"], id="list")],
    )
    async def test_unparseable_body_is_bad_value(self, env, body):
        """A 200 that isn't an HA state object (a proxy's HTML page, a list)."""
        sm = env["sm"]
        resp = MagicMock(status_code=200)
        if isinstance(body, Exception):
            resp.json.side_effect = body
        else:
            resp.json.return_value = body
        env["shared"]._http.get = AsyncMock(return_value=resp)
        before = _metric("bgw_movement_health_read_total", {"kind": "steps", "result": "bad_value"})
        await _read(sm, T0)
        assert _metric("bgw_movement_health_read_total", {"kind": "steps", "result": "bad_value"}) == before + 1
        assert sm._health.baseline == {}

    @pytest.mark.parametrize(
        "raw",
        [pytest.param("2026-11-13T10:08:00", id="naive"), pytest.param("yesterday", id="garbage")],
    )
    async def test_unusable_last_changed_counts_as_now(self, env, raw):
        """A timestamp without an offset (unknown TZ) or garbage is no time
        information: the movement is placed at the read, not at its wall-clock value."""
        sm, ha = env["sm"], env["ha"]
        sm._state.sitting_since = T0 - timedelta(hours=1)
        ha.set(STEPS, "4000", changed=T0)
        await _read(sm, T0)
        ha.set(STEPS, "4300", raw_changed=raw)
        now = T0 + timedelta(minutes=15)
        await _read(sm, now)
        assert sm._state.sitting_since == now

    async def test_out_of_range_timestamp_doesnt_block_the_other_sensor(self, env):
        sm, ha, shared = env["sm"], env["ha"], env["shared"]
        shared.MOVEMENT_STAND_ENTITY = STAND
        sm._state.sitting_since = T0 - timedelta(hours=1)
        ha.set(STEPS, "4000", raw_changed="0001-01-01T00:00:00+23:59")
        ha.set(STAND, "4", changed=T0)
        await _read(sm, T0)
        assert set(sm._health.baseline) == {"steps", "stand"}
        ha.set(STAND, "5", changed=T0 + timedelta(minutes=9))
        await _read(sm, T0 + timedelta(minutes=15))
        assert sm._state.sitting_since == T0 + timedelta(minutes=9)

    async def test_age_gauge_follows_last_reported(self, env):
        sm, ha = env["sm"], env["ha"]
        changed = T0 - timedelta(hours=3)  # sat still: the count hasn't changed
        reported = T0 - timedelta(minutes=4)  # but the phone did report
        ha.set(STEPS, "4000", changed=changed, reported=reported)
        await _read(sm, T0)
        got = _metric("bgw_movement_health_last_update_timestamp_seconds", {"kind": "steps"})
        assert got == pytest.approx(reported.astimezone().timestamp())

    async def test_age_gauge_ignores_a_future_report(self, env):
        sm, ha = env["sm"], env["ha"]
        ha.set(STEPS, "4000", changed=T0, reported=T0)
        await _read(sm, T0)
        good = _metric("bgw_movement_health_last_update_timestamp_seconds", {"kind": "steps"})
        ha.set(STEPS, "4000", changed=T0, reported=datetime.now() + timedelta(days=2))
        await _read(sm, T0 + timedelta(minutes=15))
        assert _metric("bgw_movement_health_last_update_timestamp_seconds", {"kind": "steps"}) == good


class TestFailureLogging:
    async def test_warns_once_per_outage_and_says_when_it_recovers(self, env, caplog):
        sm, ha = env["sm"], env["ha"]
        ha.status[STEPS] = 404
        ha.set(STEPS, "4000")
        with caplog.at_level(logging.INFO, logger="orchestrator.selfcare_manager"):
            await _read(sm, T0)
            await _read(sm, T0 + timedelta(minutes=15))
            ha.status[STEPS] = 200
            await _read(sm, T0 + timedelta(minutes=30))
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "not_found" in warnings[0].getMessage()
        assert any("reading again" in r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
        # Never the entity id (it carries the phone's name) or the value.
        for record in caplog.records:
            assert STEPS not in record.getMessage()
            assert "4000" not in record.getMessage()

    async def test_warns_again_when_it_fails_a_different_way(self, env, caplog):
        sm, ha = env["sm"], env["ha"]
        ha.set(STEPS, "4000")
        env["shared"]._http.get = AsyncMock(side_effect=TimeoutError(f"GET {STEPS}"))
        with caplog.at_level(logging.WARNING, logger="orchestrator.selfcare_manager"):
            await _read(sm, T0)
            env["shared"]._http.get = AsyncMock(side_effect=ha.get)
            ha.status[STEPS] = 404
            await _read(sm, T0 + timedelta(minutes=15))
        messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(messages) == 2
        assert "unreachable" in messages[0] and "TimeoutError" in messages[0]
        assert "not_found" in messages[1]
        assert all(STEPS not in m for m in messages)

    async def test_flapping_the_same_way_warns_at_most_hourly(self, env, caplog):
        sm, ha = env["sm"], env["ha"]
        ha.set(STEPS, "4000")
        with caplog.at_level(logging.WARNING, logger="orchestrator.selfcare_manager"):
            for i in range(4):
                ha.status[STEPS] = 500 if i % 2 == 0 else 200
                await _read(sm, T0 + timedelta(minutes=15 * i))
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    async def test_same_failure_warns_again_after_an_hour(self, env, caplog):
        """The hourly repeat limit must not silence a flapping sensor for good."""
        from types import SimpleNamespace

        sm, ha = env["sm"], env["ha"]
        ha.set(STEPS, "4000")
        clock = [1000.0]
        fake_time = SimpleNamespace(monotonic=lambda: clock[0], time=_time.time)
        with (
            patch.object(sm, "_time", fake_time),
            caplog.at_level(logging.WARNING, logger="orchestrator.selfcare_manager"),
        ):
            for offset, status in ((0, 500), (900, 200), (1800, 500), (3700, 200), (3800, 500)):
                clock[0] = 1000.0 + offset
                ha.status[STEPS] = status
                await _read(sm, T0 + timedelta(seconds=offset))
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2  # at 0 s and 3800 s; the 1800 s repeat is held

    async def test_a_crashed_read_is_logged_by_type_only(self, env, caplog):
        sm = env["sm"]
        with (
            patch.object(sm, "_read_movement_sensor", side_effect=RuntimeError(f"secret {STEPS}")),
            caplog.at_level(logging.WARNING, logger="orchestrator.selfcare_manager"),
        ):
            await _read(sm, T0)
        messages = [r.getMessage() for r in caplog.records]
        assert any("crashed (RuntimeError)" in m for m in messages)
        assert all(STEPS not in m for m in messages)


class TestStandHours:
    async def test_stand_hour_increase_counts(self, env):
        sm, ha, shared = env["sm"], env["ha"], env["shared"]
        shared.MOVEMENT_STEPS_ENTITY = ""
        shared.MOVEMENT_STAND_ENTITY = STAND
        sm._state.sitting_since = T0 - timedelta(hours=1)
        ha.set(STAND, "4", changed=T0)
        await _read(sm, T0)
        stood = T0 + timedelta(minutes=10)
        ha.set(STAND, "5", changed=stood)
        await _read(sm, T0 + timedelta(minutes=15))
        assert sm._state.sitting_since == stood
        env["save_log"].assert_called_once_with("movement", "health:stand")

    async def test_stand_hours_above_24_is_bad_value(self, env):
        sm, ha, shared = env["sm"], env["ha"], env["shared"]
        shared.MOVEMENT_STEPS_ENTITY = ""
        shared.MOVEMENT_STAND_ENTITY = STAND
        ha.set(STAND, "25", changed=T0)
        before = _metric("bgw_movement_health_read_total", {"kind": "stand", "result": "bad_value"})
        await _read(sm, T0)
        assert _metric("bgw_movement_health_read_total", {"kind": "stand", "result": "bad_value"}) == before + 1

    async def test_both_sensors_newest_movement_wins(self, env):
        sm, ha, shared = env["sm"], env["ha"], env["shared"]
        shared.MOVEMENT_STAND_ENTITY = STAND
        sm._state.sitting_since = T0 - timedelta(hours=1)
        ha.set(STEPS, "4000", changed=T0)
        ha.set(STAND, "4", changed=T0)
        await _read(sm, T0)
        ha.set(STEPS, "4400", changed=T0 + timedelta(minutes=4))
        ha.set(STAND, "5", changed=T0 + timedelta(minutes=9))
        await _read(sm, T0 + timedelta(minutes=15))
        assert sm._state.sitting_since == T0 + timedelta(minutes=9)
        env["save_log"].assert_called_once_with("movement", "health:stand")


class TestGatingAndDebounce:
    async def test_not_configured_makes_no_ha_call(self, env):
        sm, shared = env["sm"], env["shared"]
        shared.MOVEMENT_STEPS_ENTITY = ""
        shared.MOVEMENT_STAND_ENTITY = ""
        await _read(sm, T0)
        shared._http.get.assert_not_called()

    async def test_no_ha_credentials_makes_no_ha_call(self, env):
        sm, shared = env["sm"], env["shared"]
        shared.HA_TOKEN = ""
        await _read(sm, T0)
        shared._http.get.assert_not_called()

    async def test_movement_category_off_makes_no_ha_call(self, env):
        from orchestrator import selfcare_schedule

        sm, shared = env["sm"], env["shared"]
        with patch.object(selfcare_schedule, "category_enabled", return_value=False):
            await _read(sm, T0)
        shared._http.get.assert_not_called()

    async def test_rows_debounced_but_clock_still_moves(self, env):
        sm, ha = env["sm"], env["ha"]
        sm._state.sitting_since = T0 - timedelta(hours=1)
        ha.set(STEPS, "1000", changed=T0)
        await _read(sm, T0)
        for i, steps in enumerate((1500, 2000, 2500), start=1):
            changed = T0 + timedelta(minutes=15 * i - 2)
            ha.set(STEPS, str(steps), changed=changed)
            await _read(sm, T0 + timedelta(minutes=15 * i))
            assert sm._state.sitting_since == changed
            assert sm._state.last_movement_nudge == changed
        # Detections at +15 and +30 min write one row; +45 is past 30 min → second row.
        assert env["save_log"].call_count == 2


class TestRecordMovementAt:
    def test_default_is_now(self, env):
        sm = env["sm"]
        before = datetime.now()
        sm.record_movement_logged("voice:stretch")
        assert sm._state.sitting_since >= before

    def test_at_never_moves_the_clock_backwards(self, env):
        sm = env["sm"]
        later = T0 + timedelta(minutes=30)
        sm._state.last_movement_nudge = later
        sm._state.sitting_since = later
        sm.record_movement_logged("health:steps", at=T0 + timedelta(minutes=20))
        assert sm._state.sitting_since == later
        assert sm._state.last_movement_nudge == later


class TestSittingClockStartsWithTheDay:
    """The midnight reset parks sitting_since at ~00:00; time before the
    movement hours start must not count (the "546 minutes" first nudge)."""

    def _ctx(self, sm):
        from orchestrator import selfcare_schedule

        return (
            patch.object(selfcare_schedule, "category_interval_minutes", return_value=90),
            patch.object(sm, "_within_active_hours", return_value=True),
        )

    def test_no_nudge_at_nine_after_the_midnight_reset(self, env):
        sm = env["sm"]
        reset = datetime(2026, 11, 13, 0, 5)
        sm._state.sitting_since = reset
        sm._state.last_movement_nudge = reset
        a, b = self._ctx(sm)
        with a, b:
            assert sm._check_movement(datetime(2026, 11, 13, 9, 10)) is None

    def test_first_nudge_counts_from_the_start_of_movement_hours(self, env):
        sm = env["sm"]
        reset = datetime(2026, 11, 13, 0, 5)
        sm._state.sitting_since = reset
        sm._state.last_movement_nudge = reset
        a, b = self._ctx(sm)
        with a, b:
            msg = sm._check_movement(datetime(2026, 11, 13, 10, 35))
        assert msg is not None and "about 95 minutes" in msg

    def test_movement_inside_the_window_still_counts_from_the_movement(self, env):
        sm = env["sm"]
        sm._state.sitting_since = datetime(2026, 11, 13, 11, 0)
        sm._state.last_movement_nudge = datetime(2026, 11, 13, 11, 0)
        a, b = self._ctx(sm)
        with a, b:
            msg = sm._check_movement(datetime(2026, 11, 13, 12, 40))
        assert msg is not None and "about 100 minutes" in msg

    def test_window_that_wraps_midnight_counts_from_last_nights_start(self, env):
        from orchestrator import selfcare_schedule

        sm = env["sm"]
        sm._state.sitting_since = datetime(2026, 11, 12, 20, 0)
        with patch.object(selfcare_schedule, "category_active_hours", return_value=("22:00", "06:00")):
            start = sm._sitting_start(datetime(2026, 11, 13, 2, 0))
        assert start == datetime(2026, 11, 12, 22, 0)

    @pytest.mark.parametrize(
        "window",
        [pytest.param((None, None), id="none"), pytest.param(("09:00", None), id="start-only")],
    )
    def test_no_full_window_keeps_the_old_behaviour(self, env, window):
        from orchestrator import selfcare_schedule

        sm = env["sm"]
        sm._state.sitting_since = datetime(2026, 11, 13, 0, 5)
        with patch.object(selfcare_schedule, "category_active_hours", return_value=window):
            assert sm._sitting_start(datetime(2026, 11, 13, 9, 30)) == datetime(2026, 11, 13, 0, 5)

    async def test_status_before_the_window_says_zero(self, env):
        sm = env["sm"]
        sm._state.sitting_since = datetime.now().replace(hour=0, minute=5)
        fixed = datetime.now().replace(hour=7, minute=30, second=0, microsecond=0)
        with patch.object(sm, "datetime", _frozen(fixed)):
            status = await sm.get_selfcare_status()
        assert status["sitting_minutes"] == 0

    async def test_status_inside_the_window_counts_from_its_start(self, env):
        """Dashboard / tool figure after the midnight reset: from 09:00, not 00:05."""
        sm = env["sm"]
        sm._state.sitting_since = datetime(2026, 11, 13, 0, 5)
        with patch.object(sm, "datetime", _frozen(datetime(2026, 11, 13, 10, 30))):
            status = await sm.get_selfcare_status()
        assert status["sitting_minutes"] == 90


def _frozen(fixed: datetime):
    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is None else fixed.astimezone(tz)

    return _FrozenDT


class TestCheckSelfcareEndToEnd:
    """check_selfcare reads the phone first, then decides the movement nudge.
    The module clock is frozen at a weekday afternoon, so the tests don't
    depend on when (or in which TZ) they run."""

    NOW = datetime(2026, 11, 13, 14, 0)

    def _gates(self, sm):
        from orchestrator import selfcare_schedule

        return (
            patch.object(sm, "datetime", _frozen(self.NOW)),
            patch.object(selfcare_schedule, "quiet_hours", return_value={}),
            patch.object(selfcare_schedule, "is_quiet_day", return_value=False),
            patch.object(selfcare_schedule, "category_interval_minutes", return_value=90),
            patch.object(sm, "_within_active_hours", return_value=True),
            patch.object(sm, "_check_meds", return_value=None),
            patch.object(sm, "_check_meals", return_value=None),
            patch.object(sm, "_check_hydration", return_value=None),
        )

    def _setup(self, sm, shared, focus=False):
        shared.SELFCARE_ENABLED = True
        shared.PRESENCE_ENABLED = False
        shared.TIMEZONE = "America/Chicago"
        shared.QUIET_HOURS_START = "00:00"
        shared.QUIET_HOURS_END = "00:00"
        shared.MOVEMENT_INTERVAL = 90
        shared.current_focus_session = {"active": focus}
        start = self.NOW - timedelta(minutes=120)
        sm._state.sitting_since = start
        sm._state.last_movement_nudge = start
        sm._health.baseline["steps"] = (4000.0, self.NOW.date(), self.NOW - timedelta(minutes=15))
        return start

    async def _run(self, sm, ha, steps_now, extra=()):
        ha.set(STEPS, str(steps_now), changed=self.NOW - timedelta(minutes=1))
        with ExitStack() as stack:
            for gate in (*self._gates(sm), *extra):
                stack.enter_context(gate)
            dispatch = stack.enter_context(patch.object(sm, "_dispatch_nudge", new_callable=AsyncMock))
            await sm.check_selfcare()
        return dispatch

    async def test_movement_on_phone_suppresses_the_nudge(self, env):
        sm, ha = env["sm"], env["ha"]
        self._setup(sm, env["shared"])
        dispatch = await self._run(sm, ha, 4400)
        dispatch.assert_not_called()
        assert sm._state.sitting_since == self.NOW - timedelta(minutes=1)

    async def test_no_movement_still_nudges_at_90_min(self, env):
        sm, ha = env["sm"], env["ha"]
        self._setup(sm, env["shared"])
        dispatch = await self._run(sm, ha, 4020)
        dispatch.assert_awaited_once()
        assert dispatch.await_args.args[0] == "movement"

    async def test_movement_during_focus_session_still_counts(self, env):
        sm, ha = env["sm"], env["ha"]
        start = self._setup(sm, env["shared"], focus=True)
        dispatch = await self._run(sm, ha, 4400)
        dispatch.assert_not_called()  # focus gate: no nudges at all
        assert sm._state.sitting_since > start

    @pytest.mark.parametrize("gate", ["away", "quiet", "routine"])
    async def test_movement_counts_behind_every_gate(self, env, gate):
        """The phone read runs before the away / quiet-hours / routine gates,
        so walking while they hold the nudges still resets the clock."""
        from orchestrator import selfcare_schedule

        sm, ha, shared = env["sm"], env["ha"], env["shared"]
        start = self._setup(sm, shared)
        if gate == "away":
            shared.PRESENCE_ENABLED = True
            extra = (patch("orchestrator.presence_tracker.get_presence", return_value={"is_home": False}),)
        elif gate == "quiet":
            extra = (
                patch.object(selfcare_schedule, "is_quiet_day", return_value=True),
                patch.object(selfcare_schedule, "quiet_hours", return_value={"start": "00:00", "end": "23:59"}),
            )
        else:
            extra = (patch("orchestrator.routine_manager._active_session", object()),)
        dispatch = await self._run(sm, ha, 4400, extra)
        dispatch.assert_not_called()
        assert sm._state.sitting_since == self.NOW - timedelta(minutes=1)
        assert sm._state.sitting_since > start

    async def test_sensor_failure_never_blocks_nudges(self, env):
        sm, ha = env["sm"], env["ha"]
        self._setup(sm, env["shared"])
        with patch.object(sm, "_refresh_movement_from_health", side_effect=RuntimeError("x")):
            dispatch = await self._run(sm, ha, 4000)
        dispatch.assert_awaited_once()


class TestNonUtcTimezone:
    """The live container runs TZ=America/Chicago; CI runs UTC. Exercise the
    aware→local conversion with a real offset."""

    async def test_utc_timestamp_lands_on_local_wall_clock(self, env, monkeypatch):
        sm, ha = env["sm"], env["ha"]
        monkeypatch.setenv("TZ", "America/Chicago")
        _time.tzset()
        try:
            sm._state.sitting_since = T0 - timedelta(hours=1)
            ha.set(STEPS, "4000", raw_changed="2026-11-13T16:00:00+00:00")  # 10:00 CST
            await _read(sm, T0)
            ha.set(STEPS, "4300", raw_changed="2026-11-13T16:08:00+00:00")  # 10:08 CST
            await _read(sm, T0 + timedelta(minutes=15))
            assert sm._state.sitting_since == T0 + timedelta(minutes=8)
        finally:
            monkeypatch.undo()
            _time.tzset()


class TestConfigValidator:
    def _settings(self, **kw):
        from orchestrator.config import Settings

        return Settings(_env_file=None, **kw)

    def test_valid_ids_kept(self):
        s = self._settings(movement_steps_entity=STEPS, movement_stand_entity=f" {STAND} ")
        assert s.movement_steps_entity == STEPS
        assert s.movement_stand_entity == STAND

    @pytest.mark.parametrize(
        "bad",
        [
            pytest.param("light.kitchen", id="other-domain"),
            pytest.param("sensor.steps/../../api/services", id="path"),
            pytest.param("sensor.Steps", id="uppercase"),
            pytest.param("sensor.", id="empty-name"),
        ],
    )
    def test_bad_ids_dropped(self, bad):
        s = self._settings(movement_steps_entity=bad)
        assert s.movement_steps_entity == ""

    @pytest.mark.parametrize("value", [pytest.param(0, id="zero"), pytest.param(99999, id="huge")])
    def test_out_of_range_threshold_resets_to_default(self, value):
        assert self._settings(movement_steps_threshold=value).movement_steps_threshold == 150

    @pytest.mark.parametrize(
        "value",
        [pytest.param("", id="empty"), pytest.param("abc", id="text"), pytest.param("150.5", id="decimal")],
    )
    def test_unparseable_threshold_falls_back_instead_of_failing_startup(self, value):
        assert self._settings(movement_steps_threshold=value).movement_steps_threshold == 150

    def test_threshold_from_string(self):
        assert self._settings(movement_steps_threshold=" 300 ").movement_steps_threshold == 300

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(19, 150, id="19"),
            pytest.param(20, 20, id="20"),
            pytest.param(5000, 5000, id="5000"),
            pytest.param(5001, 150, id="5001"),
        ],
    )
    def test_threshold_bounds(self, value, expected):
        assert self._settings(movement_steps_threshold=value).movement_steps_threshold == expected

    def test_read_from_env(self, monkeypatch):
        """The .env path the container uses, incl. a blank `MOVEMENT_STEPS_THRESHOLD=` line."""
        monkeypatch.setenv("MOVEMENT_STEPS_ENTITY", STEPS)
        monkeypatch.setenv("MOVEMENT_STAND_ENTITY", STAND)
        monkeypatch.setenv("MOVEMENT_STEPS_THRESHOLD", "")
        s = self._settings()
        assert (s.movement_steps_entity, s.movement_stand_entity) == (STEPS, STAND)
        assert s.movement_steps_threshold == 150

    def test_shared_aliases_match_settings(self):
        """Every other test mocks `shared`, so a typo'd alias would only show in production."""
        from orchestrator import shared

        assert shared.settings.movement_steps_entity == shared.MOVEMENT_STEPS_ENTITY
        assert shared.settings.movement_stand_entity == shared.MOVEMENT_STAND_ENTITY
        assert shared.settings.movement_steps_threshold == shared.MOVEMENT_STEPS_THRESHOLD

    def test_defaults_off(self):
        s = self._settings()
        assert s.movement_steps_entity == "" and s.movement_stand_entity == ""
        assert s.movement_steps_threshold == 150
