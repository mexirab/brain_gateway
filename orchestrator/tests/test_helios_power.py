"""
Tests for Helios wake-on-demand + manual sleep (PT-C).

Covers:
  - helios_power.wake_helios / sleep_helios / helios_power_status
    (network-mocked via respx; disabled no-op; debounce; error handling)
  - config.validate_helios_wake_config model_validator (auto-disable + clamp)
  - cloud_brain._maybe_wake_helios + _brain_asleep_response wake hook
    (fires when enabled, no-op when disabled)
  - tool_definitions gating: helios_power exposed only when enabled
  - tool_handlers helios_power dispatch (wake/sleep/status strings)

Mocks settings via monkeypatch on the live module-level singleton (same pattern
as test_pushover_bridge). Mocks HTTP via respx so no real HA calls leak.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import subprocess
import sys
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from httpx import Response

from orchestrator import api_routes, cloud_brain, helios_power, shared
from orchestrator.cloud_brain import CloudBrain
from orchestrator.config import Settings, settings
from orchestrator.helios_power import helios_power_status, poll_helios_power, sleep_helios, wake_helios
from orchestrator.metrics import (
    HELIOS_PLUG_WATTS,
    HELIOS_RUNNING,
    HELIOS_SLEEP_TOTAL,
    HELIOS_STATUS_TOTAL,
    HELIOS_WAKE_TOTAL,
)
from orchestrator.tests._helpers import REPO_ROOT, metric_value
from orchestrator.tool_definitions import get_all_tools
from orchestrator.tool_handlers import _reg_helios_power

_HA_URL = "http://ha.test:8123"
_SWITCH = "switch.helios_monitoring_plug"
_SENSOR = "sensor.helios_monitoring_plug_current_consumption"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _configure(monkeypatch, *, wake: bool, monitor: bool):
    """Set the feature flags (+ HA connection when either is on) on the live
    settings singleton; debounce + status-poll state reset around the test."""
    monkeypatch.setattr(settings, "helios_wake_enabled", wake, raising=False)
    monkeypatch.setattr(settings, "helios_plug_monitor_enabled", monitor, raising=False)
    if wake or monitor:
        monkeypatch.setattr(settings, "ha_url", _HA_URL, raising=False)
        monkeypatch.setattr(settings, "ha_token", "test-token", raising=False)
        monkeypatch.setattr(settings, "helios_plug_entity", _SWITCH, raising=False)
        monkeypatch.setattr(settings, "helios_plug_power_sensor", _SENSOR, raising=False)
        monkeypatch.setattr(settings, "helios_wake_debounce_seconds", 300, raising=False)
    helios_power.reset_debounce()
    helios_power.reset_status_state()
    yield settings
    helios_power.reset_debounce()
    helios_power.reset_status_state()


@pytest.fixture
def helios_on(monkeypatch):
    yield from _configure(monkeypatch, wake=True, monitor=False)


@pytest.fixture
def helios_off(monkeypatch):
    yield from _configure(monkeypatch, wake=False, monitor=False)


@pytest.fixture
def monitor_only(monkeypatch):
    """Read-only plug monitoring ON, wake-on-demand OFF (the vacation-mode setup)."""
    yield from _configure(monkeypatch, wake=False, monitor=True)


def _mock_states(mock, switch: str, watts: str):
    sw = mock.get(f"/api/states/{_SWITCH}").mock(return_value=Response(200, json={"state": switch}))
    pw = mock.get(f"/api/states/{_SENSOR}").mock(return_value=Response(200, json={"state": watts}))
    return sw, pw


# ===========================================================================
# wake_helios
# ===========================================================================


class TestWakeHelios:
    async def test_disabled_no_op_no_http(self, helios_off):

        before = metric_value(HELIOS_WAKE_TOTAL, result="disabled")
        with respx.mock:  # no routes — any HTTP would raise
            result = await wake_helios()
        assert result["ok"] is False
        assert result["skipped"] == "disabled"
        assert metric_value(HELIOS_WAKE_TOTAL, result="disabled") == before + 1

    async def test_success_turns_on_plug(self, helios_on):

        before = metric_value(HELIOS_WAKE_TOTAL, result="ok")
        with respx.mock(base_url=_HA_URL) as mock:
            route = mock.post("/api/services/switch/turn_on").mock(return_value=Response(200, json=[]))
            result = await wake_helios()
        assert route.called
        assert result["ok"] is True
        assert result["action"] == "wake"
        assert result["entity"] == _SWITCH
        # Body targets the configured entity
        body = json.loads(route.calls[0].request.content)
        assert body == {"entity_id": _SWITCH}
        # Authorization header present
        assert route.calls[0].request.headers["authorization"] == "Bearer test-token"
        assert metric_value(HELIOS_WAKE_TOTAL, result="ok") == before + 1

    async def test_debounce_skips_second_wake(self, helios_on):

        before_ok = metric_value(HELIOS_WAKE_TOTAL, result="ok")
        before_deb = metric_value(HELIOS_WAKE_TOTAL, result="debounced")
        with respx.mock(base_url=_HA_URL) as mock:
            route = mock.post("/api/services/switch/turn_on").mock(return_value=Response(200, json=[]))
            first = await wake_helios()
            second = await wake_helios()
        assert first["ok"] is True
        assert second["ok"] is True
        assert second["skipped"] == "debounced"
        assert isinstance(second["retry_after_s"], int)
        # Only ONE real HA call despite two wake() calls
        assert route.call_count == 1
        assert metric_value(HELIOS_WAKE_TOTAL, result="ok") == before_ok + 1
        assert metric_value(HELIOS_WAKE_TOTAL, result="debounced") == before_deb + 1

    async def test_zero_debounce_allows_repeat(self, helios_on, monkeypatch):

        monkeypatch.setattr(settings, "helios_wake_debounce_seconds", 0, raising=False)
        with respx.mock(base_url=_HA_URL) as mock:
            route = mock.post("/api/services/switch/turn_on").mock(return_value=Response(200, json=[]))
            await wake_helios()
            await wake_helios()
        assert route.call_count == 2

    async def test_http_error_swallowed(self, helios_on):

        before = metric_value(HELIOS_WAKE_TOTAL, result="error")
        with respx.mock(base_url=_HA_URL) as mock:
            mock.post("/api/services/switch/turn_on").mock(return_value=Response(500, text="boom"))
            result = await wake_helios()
        assert result["ok"] is False
        assert "error" in result
        assert metric_value(HELIOS_WAKE_TOTAL, result="error") == before + 1

    async def test_connect_error_swallowed_and_not_debounced(self, helios_on):
        """A failed wake must NOT arm the debounce — the next attempt should retry."""

        with respx.mock(base_url=_HA_URL) as mock:
            route = mock.post("/api/services/switch/turn_on")
            route.side_effect = httpx.ConnectError("dns")
            first = await wake_helios()
            # Now succeed — should fire a real call (not debounced)
            route.side_effect = None
            route.mock(return_value=Response(200, json=[]))
            second = await wake_helios()
        assert first["ok"] is False
        assert second["ok"] is True
        assert second.get("skipped") != "debounced"


# ===========================================================================
# sleep_helios
# ===========================================================================


class TestSleepHelios:
    async def test_disabled_no_op(self, helios_off):

        before = metric_value(HELIOS_SLEEP_TOTAL, result="disabled")
        with respx.mock:
            result = await sleep_helios()
        assert result["ok"] is False
        assert result["skipped"] == "disabled"
        assert metric_value(HELIOS_SLEEP_TOTAL, result="disabled") == before + 1

    async def test_success_turns_off_plug(self, helios_on):

        before = metric_value(HELIOS_SLEEP_TOTAL, result="ok")
        with respx.mock(base_url=_HA_URL) as mock:
            route = mock.post("/api/services/switch/turn_off").mock(return_value=Response(200, json=[]))
            result = await sleep_helios()
        assert route.called
        assert result["ok"] is True
        assert result["action"] == "sleep"
        assert json.loads(route.calls[0].request.content) == {"entity_id": _SWITCH}
        assert metric_value(HELIOS_SLEEP_TOTAL, result="ok") == before + 1

    async def test_error_swallowed(self, helios_on):

        before = metric_value(HELIOS_SLEEP_TOTAL, result="error")
        with respx.mock(base_url=_HA_URL) as mock:
            mock.post("/api/services/switch/turn_off").mock(side_effect=httpx.TimeoutException("slow"))
            result = await sleep_helios()
        assert result["ok"] is False
        assert "TimeoutException" in result["error"]
        assert metric_value(HELIOS_SLEEP_TOTAL, result="error") == before + 1


# ===========================================================================
# helios_power_status
# ===========================================================================


class TestPowerStatus:
    async def test_disabled_no_op_no_http(self, helios_off):
        before = metric_value(HELIOS_STATUS_TOTAL, result="disabled")
        with respx.mock:  # no routes — any HTTP would raise
            result = await helios_power_status()
        assert result["ok"] is False
        assert result["skipped"] == "disabled"
        assert metric_value(HELIOS_STATUS_TOTAL, result="disabled") == before + 1

    @pytest.mark.parametrize(
        "switch, raw, watts, inferred",
        [
            pytest.param("on", "142.5", 142.5, "running", id="on-high-watts"),
            pytest.param("off", "0", 0.0, "asleep", id="switch-off"),
            pytest.param("on", "3.1", 3.1, "asleep", id="on-low-watts"),
            pytest.param("on", "unavailable", None, "unknown", id="watts-unparseable"),
        ],
    )
    async def test_reading_sets_result_and_gauges(self, helios_on, switch, raw, watts, inferred):
        running = inferred == "running"
        HELIOS_RUNNING.set(0 if running else 1)  # the read must overwrite it
        before_ok = metric_value(HELIOS_STATUS_TOTAL, result="ok")
        with respx.mock(base_url=_HA_URL) as mock:
            _mock_states(mock, switch, raw)
            result = await helios_power_status()
        assert result["ok"] is True
        assert (result["switch"], result["watts"], result["inferred"]) == (switch, watts, inferred)
        if watts is not None:
            assert metric_value(HELIOS_PLUG_WATTS) == watts
        assert metric_value(HELIOS_RUNNING) == int(running)
        assert metric_value(HELIOS_STATUS_TOTAL, result="ok") == before_ok + 1

    async def test_error_swallowed_and_counted(self, helios_on):
        before = metric_value(HELIOS_STATUS_TOTAL, result="error")
        with respx.mock(base_url=_HA_URL) as mock:
            mock.get(f"/api/states/{_SWITCH}").mock(side_effect=httpx.ConnectError("dns"))
            result = await helios_power_status()
        assert result["ok"] is False
        assert "ConnectError" in result["error"]
        assert metric_value(HELIOS_STATUS_TOTAL, result="error") == before + 1


# ===========================================================================
# poll_helios_power (scheduler entry point) + monitor-only gating
# ===========================================================================


class TestPollHeliosPower:
    async def test_monitor_only_reads_ha_and_sets_gauges(self, monitor_only):

        before = {r: metric_value(HELIOS_STATUS_TOTAL, result=r) for r in ("ok", "error", "disabled")}
        with respx.mock(base_url=_HA_URL) as mock:
            sw, pw = _mock_states(mock, "on", "187.25")
            result = await poll_helios_power()
        assert sw.call_count == 1
        assert pw.call_count == 1
        assert sw.calls[0].request.headers["authorization"] == "Bearer test-token"
        assert result["ok"] is True
        assert result["switch"] == "on"
        assert result["watts"] == 187.25
        assert result["inferred"] == "running"
        assert metric_value(HELIOS_PLUG_WATTS) == 187.25
        assert metric_value(HELIOS_RUNNING) == 1
        # Exactly one ok, nothing else counted
        assert metric_value(HELIOS_STATUS_TOTAL, result="ok") == before["ok"] + 1
        assert metric_value(HELIOS_STATUS_TOTAL, result="error") == before["error"]
        assert metric_value(HELIOS_STATUS_TOTAL, result="disabled") == before["disabled"]

    async def test_monitor_only_never_touches_the_plug(self, monitor_only):
        """Polling is read-only: no switch service calls, only GET /api/states."""

        with respx.mock(base_url=_HA_URL, assert_all_called=False) as mock:
            _mock_states(mock, "off", "0")
            turn_on = mock.post("/api/services/switch/turn_on").mock(return_value=Response(200, json=[]))
            turn_off = mock.post("/api/services/switch/turn_off").mock(return_value=Response(200, json=[]))
            result = await poll_helios_power()
        assert result["inferred"] == "asleep"
        assert not turn_on.called
        assert not turn_off.called
        assert all(c.request.method == "GET" for c in mock.calls)

    async def test_monitor_only_ha_error_counts_error_once(self, monitor_only):

        before = {r: metric_value(HELIOS_STATUS_TOTAL, result=r) for r in ("ok", "error", "disabled")}
        with respx.mock(base_url=_HA_URL) as mock:
            mock.get(f"/api/states/{_SWITCH}").mock(side_effect=httpx.ConnectError("dns"))
            result = await poll_helios_power()
        assert result["ok"] is False
        assert "ConnectError" in result["error"]
        assert metric_value(HELIOS_STATUS_TOTAL, result="error") == before["error"] + 1
        assert metric_value(HELIOS_STATUS_TOTAL, result="ok") == before["ok"]
        assert metric_value(HELIOS_STATUS_TOTAL, result="disabled") == before["disabled"]

    async def test_monitor_only_http_500_counts_error_once(self, monitor_only):

        before_err = metric_value(HELIOS_STATUS_TOTAL, result="error")
        before_ok = metric_value(HELIOS_STATUS_TOTAL, result="ok")
        with respx.mock(base_url=_HA_URL, assert_all_called=False) as mock:
            mock.get(f"/api/states/{_SWITCH}").mock(return_value=Response(200, json={"state": "on"}))
            mock.get(f"/api/states/{_SENSOR}").mock(return_value=Response(500, text="boom"))
            result = await poll_helios_power()
        assert result["ok"] is False
        assert metric_value(HELIOS_STATUS_TOTAL, result="error") == before_err + 1
        assert metric_value(HELIOS_STATUS_TOTAL, result="ok") == before_ok

    async def test_both_flags_off_disabled_no_http(self, helios_off):

        before = {r: metric_value(HELIOS_STATUS_TOTAL, result=r) for r in ("ok", "error", "disabled")}
        with respx.mock as mock:  # no routes — any HTTP would raise
            result = await poll_helios_power()
        assert mock.calls.call_count == 0
        assert result["ok"] is False
        assert result["skipped"] == "disabled"
        assert metric_value(HELIOS_STATUS_TOTAL, result="disabled") == before["disabled"] + 1
        assert metric_value(HELIOS_STATUS_TOTAL, result="ok") == before["ok"]
        assert metric_value(HELIOS_STATUS_TOTAL, result="error") == before["error"]

    async def test_wake_on_monitor_off_still_polls(self, helios_on):
        """Pre-existing behavior: wake alone is enough for the poll to run."""

        with respx.mock(base_url=_HA_URL) as mock:
            _mock_states(mock, "on", "150")
            result = await poll_helios_power()
        assert result["ok"] is True
        assert result["inferred"] == "running"

    async def test_running_drops_to_zero_when_sensor_unavailable(self, monitor_only):
        """Freshness guard: switch ON but sensor 'unavailable' must NOT leave running=1.

        The alert on bgw_helios_running relies on this; a stale 1 would mask a
        dead power sensor. The watts gauge goes NaN, not its last value, so the
        alerts can't read a stale number as "running" or "asleep".
        """

        with respx.mock(base_url=_HA_URL) as mock:
            _mock_states(mock, "on", "210")
            await poll_helios_power()
        assert metric_value(HELIOS_RUNNING) == 1

        with respx.mock(base_url=_HA_URL) as mock:
            _mock_states(mock, "on", "unavailable")
            result = await poll_helios_power()
        assert result["ok"] is True
        assert result["watts"] is None
        assert result["inferred"] == "unknown"
        assert metric_value(HELIOS_RUNNING) == 0
        assert math.isnan(metric_value(HELIOS_PLUG_WATTS))

    async def test_running_drops_to_zero_when_switch_unavailable(self, monitor_only):

        with respx.mock(base_url=_HA_URL) as mock:
            _mock_states(mock, "on", "210")
            await poll_helios_power()
        with respx.mock(base_url=_HA_URL) as mock:
            _mock_states(mock, "unavailable", "unavailable")
            result = await poll_helios_power()
        # Unavailable is not "off": the plug can't say whether Helios sleeps.
        assert result["inferred"] == "unknown"
        assert metric_value(HELIOS_RUNNING) == 0

    @pytest.mark.parametrize("switch", ["unavailable", "unknown", ""])
    async def test_unreadable_switch_is_unknown_even_with_low_watts(self, monitor_only, switch):

        with respx.mock(base_url=_HA_URL) as mock:
            _mock_states(mock, switch, "2.0")
            result = await poll_helios_power()
        assert result["inferred"] == "unknown"

    async def test_switch_off_still_asleep(self, monitor_only):

        with respx.mock(base_url=_HA_URL) as mock:
            _mock_states(mock, "off", "unavailable")
            result = await poll_helios_power()
        assert result["inferred"] == "asleep"
        # An open relay draws 0 W — keep HeliosUnreachablePlugBlind quiet.

        assert metric_value(HELIOS_PLUG_WATTS) == 0.0

    @pytest.mark.parametrize("raw", ["unavailable", "unknown", "", "n/a", None, "nan", "inf", "-inf"])
    async def test_watts_gauge_nan_when_sensor_unreadable(self, monitor_only, raw):

        with respx.mock(base_url=_HA_URL) as mock:
            _mock_states(mock, "on", "150")
            await poll_helios_power()
        assert metric_value(HELIOS_PLUG_WATTS) == 150
        with respx.mock(base_url=_HA_URL) as mock:
            _mock_states(mock, "on", raw)
            result = await poll_helios_power()
        assert result["watts"] is None
        assert math.isnan(metric_value(HELIOS_PLUG_WATTS))

    async def test_watts_gauge_recovers_from_nan(self, monitor_only):

        with respx.mock(base_url=_HA_URL) as mock:
            _mock_states(mock, "on", "unavailable")
            await poll_helios_power()
        assert math.isnan(metric_value(HELIOS_PLUG_WATTS))
        with respx.mock(base_url=_HA_URL) as mock:
            _mock_states(mock, "off", "1.5")
            await poll_helios_power()
        assert metric_value(HELIOS_PLUG_WATTS) == 1.5

    async def test_ha_request_error_leaves_watts_gauge_untouched(self, monitor_only):
        """A failed HA request is covered by the status_total{ok} freshness check."""

        with respx.mock(base_url=_HA_URL) as mock:
            _mock_states(mock, "on", "150")
            await poll_helios_power()
        with respx.mock(base_url=_HA_URL) as mock:
            mock.get(f"/api/states/{_SWITCH}").mock(side_effect=httpx.ConnectError("boom"))
            result = await poll_helios_power()
        assert result["ok"] is False
        assert metric_value(HELIOS_PLUG_WATTS) == 150

    @pytest.mark.parametrize("raw", ["inf", "1e400"])
    async def test_infinite_watts_never_reads_as_running(self, monitor_only, raw):
        """float('inf') >= threshold — it must be dropped before the running check."""

        with respx.mock(base_url=_HA_URL) as mock:
            _mock_states(mock, "on", "150")
            await poll_helios_power()
        assert metric_value(HELIOS_RUNNING) == 1
        with respx.mock(base_url=_HA_URL) as mock:
            _mock_states(mock, "on", raw)
            result = await poll_helios_power()
        assert result["inferred"] == "unknown"
        assert metric_value(HELIOS_RUNNING) == 0

    async def test_both_unreadable_exports_nan_not_zero(self, monitor_only):
        """Only a definite "off" earns the 0 W shortcut; an unavailable switch must not."""

        with respx.mock(base_url=_HA_URL) as mock:
            _mock_states(mock, "unavailable", "unavailable")
            await poll_helios_power()
        assert math.isnan(metric_value(HELIOS_PLUG_WATTS))

    @pytest.mark.parametrize("raw", ["nan", "inf", "garbage"])
    async def test_switch_off_with_nonfinite_sensor_exports_zero(self, monitor_only, raw):

        with respx.mock(base_url=_HA_URL) as mock:
            _mock_states(mock, "off", raw)
            result = await poll_helios_power()
        assert result["inferred"] == "asleep"
        assert metric_value(HELIOS_PLUG_WATTS) == 0.0

    async def test_unavailable_switch_with_live_draw_keeps_watts(self, monitor_only):
        """The "running == 0 but watts >= 30" alert branch needs the real reading."""

        with respx.mock(base_url=_HA_URL) as mock:
            _mock_states(mock, "unavailable", "180.5")
            result = await poll_helios_power()
        assert result["inferred"] == "unknown"
        assert metric_value(HELIOS_RUNNING) == 0
        assert metric_value(HELIOS_PLUG_WATTS) == 180.5


def test_watts_gauge_starts_as_nan_in_a_fresh_process():
    """A fresh orchestrator must not export 0 W (read by the alerts as asleep)."""

    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from prometheus_client import generate_latest; import orchestrator.metrics; "
            "print(generate_latest().decode())",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert "\nbgw_helios_plug_watts NaN\n" in out.stdout


class TestMonitorOnlyKeepsUserSurfaceGated:
    """Monitor-only must not expose plug status/control to the API or the LLM."""

    async def test_status_still_disabled(self, monitor_only):

        before = {r: metric_value(HELIOS_STATUS_TOTAL, result=r) for r in ("ok", "error", "disabled")}
        with respx.mock as mock:
            result = await helios_power_status()
        assert mock.calls.call_count == 0
        assert result["ok"] is False
        assert result["skipped"] == "disabled"
        assert metric_value(HELIOS_STATUS_TOTAL, result="disabled") == before["disabled"] + 1
        assert metric_value(HELIOS_STATUS_TOTAL, result="ok") == before["ok"]

    async def test_wake_and_sleep_still_disabled(self, monitor_only):

        with respx.mock as mock:
            woke = await wake_helios()
            slept = await sleep_helios()
        assert mock.calls.call_count == 0
        assert woke["skipped"] == "disabled"
        assert slept["skipped"] == "disabled"

    async def test_power_route_returns_409(self, monitor_only):

        with respx.mock:
            resp = await api_routes.helios_power()
        assert resp.status_code == 409
        assert json.loads(resp.body)["skipped"] == "disabled"

    # The LLM side: see TestToolGating (monitor-only row).


# ===========================================================================
# Status-poll log noise (transition-only logging while Helios sleeps)
# ===========================================================================


class TestStatusPollLogNoise:
    """The 60s status poll must not ERROR-spam while Helios is asleep.

    Sleeping is the expected state under power tiering — unreachable status
    reads log once per state transition (INFO), recovery logs once, and only
    the plug-was-ON case escalates to WARNING (throttled).
    """

    def _records(self, caplog, min_level):

        return [
            r
            for r in caplog.records
            if r.name == "orchestrator.helios_power" and r.levelno >= getattr(logging, min_level)
        ]

    async def test_repeated_failures_log_once_not_per_poll(self, helios_on, caplog):

        with (
            caplog.at_level(logging.DEBUG, logger="orchestrator.helios_power"),
            respx.mock(base_url=_HA_URL) as mock,
        ):
            mock.get(f"/api/states/{_SWITCH}").mock(side_effect=httpx.ConnectError("dns"))
            for _ in range(10):
                result = await helios_power_status()
                assert result["ok"] is False  # result contract unchanged

        # No ERROR records at all, and exactly ONE record at INFO-or-above
        # (the "appears asleep" transition line) despite 10 failed polls.
        assert self._records(caplog, "ERROR") == []
        visible = self._records(caplog, "INFO")
        assert len(visible) == 1
        assert "asleep" in visible[0].getMessage().lower()

    async def test_recovery_logs_once(self, helios_on, caplog):

        with respx.mock(base_url=_HA_URL) as mock:
            mock.get(f"/api/states/{_SWITCH}").mock(side_effect=httpx.ConnectError("dns"))
            for _ in range(3):
                await helios_power_status()

        with (
            caplog.at_level(logging.INFO, logger="orchestrator.helios_power"),
            respx.mock(base_url=_HA_URL) as mock,
        ):
            mock.get(f"/api/states/{_SWITCH}").mock(return_value=Response(200, json={"state": "off"}))
            mock.get(f"/api/states/{_SENSOR}").mock(return_value=Response(200, json={"state": "0"}))
            await helios_power_status()
            await helios_power_status()  # second success must NOT re-log recovery

        recovered = [r for r in self._records(caplog, "INFO") if "recovered" in r.getMessage().lower()]
        assert len(recovered) == 1
        assert "3" in recovered[0].getMessage()

    async def test_plug_on_escalates_to_warning_but_throttled(self, helios_on, caplog):

        # Establish a healthy read with the plug ON (box should be running)...
        with respx.mock(base_url=_HA_URL) as mock:
            mock.get(f"/api/states/{_SWITCH}").mock(return_value=Response(200, json={"state": "on"}))
            mock.get(f"/api/states/{_SENSOR}").mock(return_value=Response(200, json={"state": "150"}))
            await helios_power_status()

        # ...then fail for many consecutive polls.
        n_polls = helios_power._UNEXPECTED_FAILURE_POLLS + 10
        with (
            caplog.at_level(logging.DEBUG, logger="orchestrator.helios_power"),
            respx.mock(base_url=_HA_URL) as mock,
        ):
            mock.get(f"/api/states/{_SWITCH}").mock(side_effect=httpx.ConnectError("dns"))
            for _ in range(n_polls):
                await helios_power_status()

        assert self._records(caplog, "ERROR") == []
        warnings = self._records(caplog, "WARNING")
        # One transition warning + one escalation at the threshold — NOT one
        # per poll (the old behavior fired every 60s).
        assert 1 <= len(warnings) <= 2
        assert any("consecutive" in r.getMessage() for r in warnings)

    async def test_transition_after_recovery_logs_again(self, helios_on, caplog):
        """A new outage after recovery is a new transition — it logs once again."""

        with caplog.at_level(logging.INFO, logger="orchestrator.helios_power"):
            with respx.mock(base_url=_HA_URL) as mock:
                mock.get(f"/api/states/{_SWITCH}").mock(side_effect=httpx.ConnectError("dns"))
                await helios_power_status()
            with respx.mock(base_url=_HA_URL) as mock:
                mock.get(f"/api/states/{_SWITCH}").mock(return_value=Response(200, json={"state": "off"}))
                mock.get(f"/api/states/{_SENSOR}").mock(return_value=Response(200, json={"state": "0"}))
                await helios_power_status()
            with respx.mock(base_url=_HA_URL) as mock:
                mock.get(f"/api/states/{_SWITCH}").mock(side_effect=httpx.ConnectError("dns"))
                await helios_power_status()

        asleep_lines = [r for r in self._records(caplog, "INFO") if "asleep" in r.getMessage().lower()]
        assert len(asleep_lines) == 2  # once per outage, not once per process lifetime


# ===========================================================================
# config.validate_helios_wake_config
# ===========================================================================


_HA_ENV = {"HA_URL": "http://ha.test:8123", "HA_TOKEN": "tok"}
_MONITOR_ENV = {
    "HELIOS_WAKE_ENABLED": "false",
    "HELIOS_PLUG_MONITOR_ENABLED": "true",
    **_HA_ENV,
    "HELIOS_PLUG_ENTITY": _SWITCH,
    "HELIOS_PLUG_POWER_SENSOR": _SENSOR,
}
_BAD_ENTITY = "switch.bad id/with-slash"
_BAD_SENSOR = "../../etc/passwd"


def _settings_from(env: dict) -> Settings:
    with patch.dict(os.environ, {"API_TOKEN": "x", "PIHOLE_PASSWORD": "x", **env}, clear=False):
        return Settings()


class TestConfigValidator:
    """validate_helios_wake_config: a misconfigured feature switches itself off
    (logged), never raises. ``None`` = that flag isn't checked by the row."""

    @pytest.mark.parametrize(
        "env, wake, monitor, log",
        [
            pytest.param(
                {"HELIOS_WAKE_ENABLED": "true", "HA_URL": "", "HA_TOKEN": ""},
                False,
                None,
                "HELIOS_WAKE_ENABLED=true",
                id="wake-without-ha",
            ),
            pytest.param({"HELIOS_WAKE_ENABLED": "true", **_HA_ENV}, True, None, None, id="wake-with-ha"),
            pytest.param(
                {"HELIOS_WAKE_ENABLED": "false", "HA_URL": "", "HA_TOKEN": ""}, False, None, None, id="off-without-ha"
            ),
            pytest.param(
                {"HELIOS_WAKE_ENABLED": "true", **_HA_ENV, "HELIOS_PLUG_ENTITY": _BAD_ENTITY},
                False,
                None,
                "not a valid",
                id="wake-bad-entity",
            ),
            pytest.param(
                {"HELIOS_WAKE_ENABLED": "true", **_HA_ENV, "HELIOS_PLUG_POWER_SENSOR": _BAD_SENSOR},
                False,
                None,
                None,
                id="wake-bad-sensor",
            ),
            pytest.param(_MONITOR_ENV, False, True, None, id="monitor-valid"),
            pytest.param(
                {**_MONITOR_ENV, "HA_URL": ""}, False, False, "HELIOS_PLUG_MONITOR_ENABLED=true", id="monitor-no-url"
            ),
            pytest.param(
                {**_MONITOR_ENV, "HA_TOKEN": ""},
                False,
                False,
                "HELIOS_PLUG_MONITOR_ENABLED=true",
                id="monitor-no-token",
            ),
            # Monitor alone (wake off) still triggers the entity-id check.
            pytest.param(
                {**_MONITOR_ENV, "HELIOS_PLUG_ENTITY": _BAD_ENTITY},
                False,
                False,
                "not a valid",
                id="monitor-bad-entity",
            ),
            pytest.param(
                {**_MONITOR_ENV, "HELIOS_PLUG_POWER_SENSOR": _BAD_SENSOR},
                False,
                False,
                "not a valid",
                id="monitor-bad-sensor",
            ),
            pytest.param(
                {**_MONITOR_ENV, "HELIOS_WAKE_ENABLED": "true", "HELIOS_PLUG_ENTITY": "switch.bad id"},
                False,
                False,
                None,
                id="both-bad-entity",
            ),
            pytest.param({**_MONITOR_ENV, "HELIOS_WAKE_ENABLED": "true"}, True, True, None, id="both-valid"),
            # Each flag has its own HA guard: no token turns both off.
            pytest.param(
                {**_MONITOR_ENV, "HELIOS_WAKE_ENABLED": "true", "HA_TOKEN": ""}, False, False, None, id="both-no-token"
            ),
        ],
    )
    def test_flags_after_validation(self, env, wake, monitor, log, caplog):
        with caplog.at_level(logging.ERROR, logger="orchestrator.config"):
            s = _settings_from(env)
        if wake is not None:
            assert s.helios_wake_enabled is wake
        if monitor is not None:
            assert s.helios_plug_monitor_enabled is monitor
        if log:
            assert any(log in r.getMessage() for r in caplog.records)

    def test_negative_debounce_clamped(self):
        s = _settings_from({"HELIOS_WAKE_ENABLED": "false", "HELIOS_WAKE_DEBOUNCE_SECONDS": "-5", "HA_TOKEN": "x"})
        assert s.helios_wake_debounce_seconds == 0

    def test_monitor_default_off(self):
        env = dict(_MONITOR_ENV)
        env.pop("HELIOS_PLUG_MONITOR_ENABLED")
        with patch.dict(os.environ, {"API_TOKEN": "x", "PIHOLE_PASSWORD": "x", **env}, clear=False):
            os.environ.pop("HELIOS_PLUG_MONITOR_ENABLED", None)
            s = Settings()
        assert s.helios_plug_monitor_enabled is False


# ===========================================================================
# cloud_brain wake hook
# ===========================================================================


def _bare_cloud_brain():
    """Construct a CloudBrain without running __init__ — the wake hook only
    touches module-level `shared`/`helios_power`, not instance state."""

    cb = CloudBrain.__new__(CloudBrain)
    cb._model_name = "test-model"
    return cb


class TestBrainAsleepWakeHook:
    async def test_maybe_wake_dispatches_when_enabled(self, monkeypatch):

        monkeypatch.setattr(shared, "HELIOS_WAKE_ENABLED", True, raising=False)
        cb = _bare_cloud_brain()
        routing: dict = {}
        mock_wake = AsyncMock(return_value={"ok": True})
        with patch("orchestrator.helios_power.wake_helios", mock_wake):
            woke = cb._maybe_wake_helios(routing)
            # Strong ref held while in flight (prevents GC of the detached task)

            assert len(cloud_brain._HELIOS_WAKE_TASKS) == 1
            # Drain the detached task

            pending = [t for t in asyncio.all_tasks() if not t.done() and t is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        assert woke is True
        assert routing["helios_wake"] == "dispatched"
        mock_wake.assert_called_once()
        # Done-callback cleared the set
        assert len(cloud_brain._HELIOS_WAKE_TASKS) == 0

    async def test_maybe_wake_no_op_when_disabled(self, monkeypatch):

        monkeypatch.setattr(shared, "HELIOS_WAKE_ENABLED", False, raising=False)
        cb = _bare_cloud_brain()
        routing: dict = {}
        mock_wake = AsyncMock(return_value={"ok": True})
        with patch("orchestrator.helios_power.wake_helios", mock_wake):
            woke = cb._maybe_wake_helios(routing)
        assert woke is False
        assert "helios_wake" not in routing
        mock_wake.assert_not_called()

    async def test_asleep_response_message_reflects_wake(self, monkeypatch):

        monkeypatch.setattr(shared, "HELIOS_WAKE_ENABLED", True, raising=False)
        cb = _bare_cloud_brain()
        routing: dict = {}
        mock_wake = AsyncMock(return_value={"ok": True})
        with patch("orchestrator.helios_power.wake_helios", mock_wake):
            resp = cb._brain_asleep_response(stream=False, routing_info=routing)

            pending = [t for t in asyncio.all_tasks() if not t.done() and t is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        payload = json.loads(resp.body)
        content = payload["choices"][0]["message"]["content"]
        assert "waking it now" in content.lower()
        assert routing["mode"] == "brain_asleep"

    async def test_asleep_response_default_message_when_disabled(self, monkeypatch):

        monkeypatch.setattr(shared, "HELIOS_WAKE_ENABLED", False, raising=False)
        cb = _bare_cloud_brain()
        resp = cb._brain_asleep_response(stream=False, routing_info={})
        payload = json.loads(resp.body)
        content = payload["choices"][0]["message"]["content"]
        assert "conversational brain is offline" in content.lower()
        assert "waking it now" not in content.lower()


# ===========================================================================
# tool_definitions gating
# ===========================================================================


class TestToolGating:
    @pytest.mark.parametrize(
        "wake, monitor, exposed",
        [
            pytest.param(False, False, False, id="disabled"),
            pytest.param(True, False, True, id="enabled"),
            pytest.param(False, True, False, id="monitor-only"),
        ],
    )
    def test_helios_tool_only_with_wake_enabled(self, monkeypatch, wake, monitor, exposed):
        monkeypatch.setattr(shared, "HELIOS_WAKE_ENABLED", wake, raising=False)
        monkeypatch.setattr(shared, "HELIOS_PLUG_MONITOR_ENABLED", monitor, raising=False)
        names = {t["function"]["name"] for t in get_all_tools()}
        assert ("helios_power" in names) is exposed


# ===========================================================================
# tool_handlers dispatch
# ===========================================================================


@pytest.mark.parametrize(
    "target, result, args, needles",
    [
        pytest.param("wake_helios", {"ok": True, "action": "wake"}, {"action": "wake"}, ("waking helios",), id="wake"),
        pytest.param(
            "wake_helios",
            {"ok": True, "skipped": "debounced", "retry_after_s": 120},
            {"action": "wake"},
            ("already waking",),
            id="wake-debounced",
        ),
        pytest.param("sleep_helios", {"ok": True, "action": "sleep"}, {"action": "sleep"}, ("cut power",), id="sleep"),
        pytest.param(
            "helios_power_status",
            {"ok": True, "switch": "on", "watts": 150.0, "inferred": "running"},
            {},  # no action: defaults to status
            ("running", "150w"),
            id="status-default",
        ),
        pytest.param(
            "wake_helios",
            {"ok": False, "skipped": "disabled", "reason": "off"},
            {"action": "wake"},
            ("turned off",),
            id="disabled",
        ),
    ],
)
async def test_tool_handler_reply(target, result, args, needles):
    with patch(f"orchestrator.helios_power.{target}", AsyncMock(return_value=result)):
        out = (await _reg_helios_power(args)).lower()
    for needle in needles:
        assert needle in out
