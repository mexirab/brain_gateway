"""Temperature sensor handling: the 10-min closet job and the /metrics scrape."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from prometheus_client import CollectorRegistry, Gauge


def _resp(status: int, state=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = {"state": state}
    return r


# ---------------------------------------------------------------------------
# jobs_monitoring.check_closet_temperature — missing-sensor warning
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_warned():
    from orchestrator import jobs_monitoring

    jobs_monitoring._missing_sensor_warned.clear()
    yield
    jobs_monitoring._missing_sensor_warned.clear()


async def test_closet_404_warns_once(reset_warned, caplog):
    from orchestrator import jobs_monitoring, shared

    http = MagicMock()
    http.get = AsyncMock(return_value=_resp(404))
    with (
        patch.object(shared, "_http", http),
        caplog.at_level(logging.WARNING, logger="orchestrator.jobs_monitoring"),
    ):
        await jobs_monitoring.check_closet_temperature()
        await jobs_monitoring.check_closet_temperature()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "not found" in r.getMessage()]
    assert len(warnings) == 1
    assert http.get.await_count == 2


async def test_closet_500_does_not_warn(reset_warned, caplog):
    from orchestrator import jobs_monitoring, shared

    http = MagicMock()
    http.get = AsyncMock(return_value=_resp(500))
    with (
        patch.object(shared, "_http", http),
        caplog.at_level(logging.WARNING, logger="orchestrator.jobs_monitoring"),
    ):
        await jobs_monitoring.check_closet_temperature()

    assert not [r for r in caplog.records if "not found" in r.getMessage()]


# ---------------------------------------------------------------------------
# api_routes.metrics_endpoint — temperature block
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_temp_gauges():
    reg = CollectorRegistry()
    temp = Gauge("bgw_test_temperature_f", "t", ["location"], registry=reg)
    delta = Gauge("bgw_test_temperature_delta_f", "d", registry=reg)
    with (
        patch("orchestrator.api_routes.TEMPERATURE_GAUGE", temp),
        patch("orchestrator.api_routes.TEMPERATURE_DELTA", delta),
    ):
        yield temp, delta, reg


def _http_by_sensor(closet_resp, kitchen_resp):
    from orchestrator.shared import profile

    closet_id = profile.closet_temp_sensor
    ambient_id = profile.ambient_temp_sensor
    assert closet_id != ambient_id

    async def get(url, **kwargs):
        if url.endswith(f"/api/states/{closet_id}"):
            return closet_resp
        if url.endswith(f"/api/states/{ambient_id}"):
            return kitchen_resp
        return _resp(200)  # fallback-model probe

    http = MagicMock()
    http.get = AsyncMock(side_effect=get)
    return http


def _series(reg, name):
    """{location: value} for the samples that actually exist in the registry."""
    out = {}
    for metric in reg.collect():
        for s in metric.samples:
            if s.name == name:
                out[s.labels.get("location")] = s.value
    return out


async def _call_metrics(http):
    from orchestrator import api_routes, shared

    with (
        patch.object(shared, "_http", http),
        patch("orchestrator.api_routes.check_model_health", AsyncMock(return_value=False)),
        patch("orchestrator.api_routes.list_pending_reminders", return_value=[]),
    ):
        return await api_routes.metrics_endpoint()


async def test_metrics_sets_both_locations_and_delta(fresh_temp_gauges):
    temp, delta, reg = fresh_temp_gauges
    resp = await _call_metrics(_http_by_sensor(_resp(200, "82.5"), _resp(200, "71.0")))

    assert resp.status_code == 200
    series = _series(reg, "bgw_test_temperature_f")
    assert series == {"closet": 82.5, "kitchen": 71.0}
    assert "ambient" not in series
    assert delta._value.get() == pytest.approx(11.5)


async def test_metrics_unavailable_sensor_creates_no_series(fresh_temp_gauges):
    temp, delta, reg = fresh_temp_gauges
    resp = await _call_metrics(_http_by_sensor(_resp(200, "80.0"), _resp(200, "unavailable")))

    assert resp.status_code == 200
    series = _series(reg, "bgw_test_temperature_f")
    assert series == {"closet": 80.0}  # no kitchen series (not 0°F)
    assert delta._value.get() == 0.0  # delta untouched without both readings


async def test_metrics_404_sensor_creates_no_series(fresh_temp_gauges):
    temp, delta, reg = fresh_temp_gauges
    resp = await _call_metrics(_http_by_sensor(_resp(404), _resp(200, "70.0")))

    assert resp.status_code == 200
    assert _series(reg, "bgw_test_temperature_f") == {"kitchen": 70.0}


# ---------------------------------------------------------------------------
# jobs_monitoring.check_closet_temperature — thresholds come from the profile
# ---------------------------------------------------------------------------


@pytest.fixture
def closet_alerts(reset_warned):
    """Run the closet job at a given temperature with 90/95 profile thresholds."""
    from orchestrator import jobs_monitoring, shared, state_store

    state_store.clear_notifications_by_prefix("temp:")
    announce = AsyncMock(return_value={"success": True})

    async def run(temp: float, *, quiet: bool = False, warning: float = 90.0, critical: float = 95.0):
        http = MagicMock()
        http.get = AsyncMock(return_value=_resp(200, str(temp)))
        with (
            patch.object(shared, "_http", http),
            patch.object(jobs_monitoring, "_announce_voice", announce),
            patch.object(jobs_monitoring, "_in_quiet_hours_now", return_value=quiet),
            patch.object(jobs_monitoring.profile, "temp_warning", warning),
            patch.object(jobs_monitoring.profile, "temp_critical", critical),
        ):
            await jobs_monitoring.check_closet_temperature()

    yield run, announce
    state_store.clear_notifications_by_prefix("temp:")


async def test_normal_80s_closet_is_quiet(closet_alerts):
    run, announce = closet_alerts
    for t in (80.0, 85.0, 89.9):
        await run(t)
    announce.assert_not_awaited()


async def test_warning_speaks_once_per_crossing(closet_alerts):
    run, announce = closet_alerts
    await run(91.0)
    await run(92.0)
    assert announce.await_count == 1
    assert "91 degrees" in announce.await_args.args[0]


async def test_critical_speaks(closet_alerts):
    run, announce = closet_alerts
    await run(96.0)
    assert announce.await_count == 1
    assert "dangerously hot" in announce.await_args.args[0]


async def test_rearms_only_after_cooling_below_margin(closet_alerts):
    from orchestrator import jobs_monitoring

    run, announce = closet_alerts
    await run(91.0)
    await run(88.0)  # 90 - 3 = 87: not cool enough to re-arm
    await run(91.0)
    assert announce.await_count == 1
    await run(90.0 - jobs_monitoring.TEMP_CLEAR_MARGIN_F - 0.1)  # cooled: re-armed
    await run(91.0)
    assert announce.await_count == 2


async def test_critical_then_cooling_warns_only_once(closet_alerts):
    run, announce = closet_alerts
    await run(96.0)
    await run(92.0)  # back in the warning band: no extra "getting warm"
    assert announce.await_count == 1


async def test_warning_held_during_quiet_hours_then_spoken(closet_alerts):
    run, announce = closet_alerts
    await run(91.0, quiet=True)
    announce.assert_not_awaited()
    await run(91.0)  # quiet hours over: the held warning goes out now
    assert announce.await_count == 1


async def test_critical_speaks_during_quiet_hours(closet_alerts):
    run, announce = closet_alerts
    await run(96.0, quiet=True)
    assert announce.await_count == 1


async def test_suppressed_announcement_is_retried(closet_alerts):
    run, announce = closet_alerts
    announce.return_value = {"success": True, "suppressed": True, "reason": "dnd_active"}
    await run(91.0)
    announce.return_value = {"success": True}
    await run(91.0)
    assert announce.await_count == 2  # the first one wasn't heard, so it wasn't "used up"


async def test_inverted_thresholds_do_not_flap(closet_alerts):
    run, announce = closet_alerts
    for _ in range(4):  # critical 85 < warning 90: treated as 90/90
        await run(86.0, warning=90.0, critical=85.0)
    announce.assert_not_awaited()
    await run(91.0, warning=90.0, critical=85.0)
    await run(91.0, warning=90.0, critical=85.0)
    assert announce.await_count == 1


async def test_metrics_drops_closet_series_when_reading_stops(fresh_temp_gauges):
    _temp, _delta, reg = fresh_temp_gauges
    await _call_metrics(_http_by_sensor(_resp(200, "84.0"), _resp(200, "70.0")))
    assert _series(reg, "bgw_test_temperature_f")["closet"] == 84.0
    await _call_metrics(_http_by_sensor(_resp(404), _resp(200, "70.0")))
    assert "closet" not in _series(reg, "bgw_test_temperature_f")  # not frozen at 84
