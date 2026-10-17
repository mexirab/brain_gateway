"""GET /api/temperatures (dashboard temperature card) and its _numeric_state helper."""

import logging
import math
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator import api_routes, shared
from orchestrator.api_routes import _numeric_state, get_temperatures

# ---------------------------------------------------------------------------
# _numeric_state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (72, 72.0),
        (72.5, 72.5),
        (0, 0.0),
        (-3.5, -3.5),
        ("72.4", 72.4),
        ("0", 0.0),
        (" 68.0 ", 68.0),
        ("-1", -1.0),
    ],
)
def test_numeric_state_numbers(state, expected):
    assert _numeric_state(state) == expected


@pytest.mark.parametrize(
    "state",
    [
        None,
        "",
        "unknown",
        "unavailable",
        "abc",
        "nan",
        "NaN",
        "inf",
        "-inf",
        "Infinity",
        float("nan"),
        float("inf"),
        [],
        {},
    ],
)
def test_numeric_state_non_numeric_is_none(state):
    assert _numeric_state(state) is None


def test_numeric_state_returns_finite_float():
    value = _numeric_state("1e3")
    assert isinstance(value, float)
    assert math.isfinite(value)
    assert value == 1000.0


# ---------------------------------------------------------------------------
# get_temperatures
# ---------------------------------------------------------------------------

CLOSET = "sensor.test_closet_temperature"
AMBIENT = "sensor.test_ambient_temperature"


def _resp(status: int, state=None, unit="°F", name=None):
    r = MagicMock()
    r.status_code = status
    attrs = {"unit_of_measurement": unit}
    if name:
        attrs["friendly_name"] = name
    r.json.return_value = {"state": state, "attributes": attrs}
    return r


def _http_by_entity(responses: dict):
    """Fake shared._http whose get() answers per entity id (value may be an Exception)."""

    async def _get(url, **kwargs):
        for entity, outcome in responses.items():
            if url.endswith(f"/api/states/{entity}"):
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        raise AssertionError(f"unexpected URL {url}")

    http = MagicMock()
    http.get = AsyncMock(side_effect=_get)
    return http


@pytest.fixture
def temp_profile():
    """Pin sensor ids + thresholds on the profile get_temperatures reads."""
    prof = api_routes.profile
    with (
        patch.object(prof, "closet_temp_sensor", CLOSET),
        patch.object(prof, "ambient_temp_sensor", AMBIENT),
        patch.object(prof, "temp_warning", 90.0),
        patch.object(prof, "temp_critical", 95.0),
    ):
        yield prof


async def _call(responses):
    with patch.object(shared, "_http", _http_by_entity(responses)):
        return await get_temperatures()


async def test_both_readings(temp_profile):
    out = await _call({CLOSET: _resp(200, "84.3", name="Closet"), AMBIENT: _resp(200, "72.1")})

    assert out["ok"] is True
    assert out["sensors"]["closet"]["temperature"] == 84.3
    assert out["sensors"]["closet"]["unit"] == "°F"
    assert out["sensors"]["closet"]["friendly_name"] == "Closet"
    assert out["sensors"]["ambient"]["temperature"] == 72.1
    assert out["sensors"]["ambient"]["friendly_name"] == "ambient"  # falls back to label
    assert out["delta"] == 12.2
    assert out["estimated_monthly_cooling_cost"] is not None and out["estimated_monthly_cooling_cost"] > 0
    assert out["thresholds"] == {"warning": 90.0, "critical": 95.0}
    assert "timestamp" in out


@pytest.mark.parametrize("bad_state", ["unavailable", "unknown", "", None, "nan", "inf", "abc"])
async def test_one_unavailable_no_delta(temp_profile, bad_state, caplog):
    with caplog.at_level(logging.DEBUG, logger="orchestrator.api_routes"):
        out = await _call({CLOSET: _resp(200, "84.3"), AMBIENT: _resp(200, bad_state)})

    assert out["ok"] is True
    assert out["sensors"]["ambient"] == {"temperature": None, "error": "No reading"}
    assert out["sensors"]["closet"]["temperature"] == 84.3
    assert out["delta"] is None
    assert out["estimated_monthly_cooling_cost"] is None
    # A non-numeric HA state is normal (sensor offline) — not an ERROR.
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_zero_reading_still_produces_delta(temp_profile):
    out = await _call({CLOSET: _resp(200, "0"), AMBIENT: _resp(200, "-2.5")})

    assert out["sensors"]["closet"]["temperature"] == 0.0
    assert out["delta"] == 2.5


async def test_zero_ambient_reading_still_produces_delta(temp_profile):
    out = await _call({CLOSET: _resp(200, "5.0"), AMBIENT: _resp(200, 0)})

    assert out["delta"] == 5.0


async def test_negative_delta_has_no_cooling_cost(temp_profile):
    out = await _call({CLOSET: _resp(200, "70"), AMBIENT: _resp(200, "75")})

    assert out["delta"] == -5.0
    assert out["estimated_monthly_cooling_cost"] is None


async def test_swapped_thresholds_clamp_critical_to_warning(temp_profile):
    with (
        patch.object(temp_profile, "temp_warning", 90.0),
        patch.object(temp_profile, "temp_critical", 85.0),
    ):
        out = await _call({CLOSET: _resp(200, "80"), AMBIENT: _resp(200, "70")})

    assert out["thresholds"] == {"warning": 90.0, "critical": 90.0}


async def test_http_non_200(temp_profile):
    out = await _call({CLOSET: _resp(404), AMBIENT: _resp(200, "70")})

    assert out["ok"] is True
    assert out["sensors"]["closet"] == {"temperature": None, "error": "HTTP 404"}
    assert out["sensors"]["ambient"]["temperature"] == 70.0
    assert out["delta"] is None
    assert "thresholds" in out


async def test_exception_path(temp_profile, caplog):
    with caplog.at_level(logging.ERROR, logger="orchestrator.api_routes"):
        out = await _call({CLOSET: RuntimeError("boom"), AMBIENT: _resp(200, "70")})

    assert out["ok"] is True
    assert out["sensors"]["closet"] == {"temperature": None, "error": "Sensor read failed"}
    assert out["sensors"]["ambient"]["temperature"] == 70.0
    assert out["delta"] is None
    assert [r for r in caplog.records if r.levelno == logging.ERROR]


async def test_both_failed(temp_profile):
    out = await _call({CLOSET: _resp(500), AMBIENT: _resp(200, "unavailable")})

    assert out["ok"] is True
    assert out["sensors"]["closet"]["temperature"] is None
    assert out["sensors"]["ambient"]["temperature"] is None
    assert out["delta"] is None
    assert out["thresholds"] == {"warning": 90.0, "critical": 95.0}
