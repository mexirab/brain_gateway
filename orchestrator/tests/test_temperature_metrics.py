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
