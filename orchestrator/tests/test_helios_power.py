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

import json
import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from httpx import Response

_HA_URL = "http://ha.test:8123"
_SWITCH = "switch.helios_monitoring_plug"
_SENSOR = "sensor.helios_monitoring_plug_current_consumption"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def helios_on(monkeypatch):
    """Enable the feature on the live settings singleton + reset debounce."""
    from orchestrator import helios_power
    from orchestrator.config import settings

    monkeypatch.setattr(settings, "helios_wake_enabled", True, raising=False)
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
def helios_off(monkeypatch):
    from orchestrator import helios_power
    from orchestrator.config import settings

    monkeypatch.setattr(settings, "helios_wake_enabled", False, raising=False)
    helios_power.reset_debounce()
    helios_power.reset_status_state()
    yield settings
    helios_power.reset_debounce()
    helios_power.reset_status_state()


def _metric(counter, **labels) -> float:
    return counter.labels(**labels)._value.get()


# ===========================================================================
# wake_helios
# ===========================================================================


class TestWakeHelios:
    @pytest.mark.asyncio
    async def test_disabled_no_op_no_http(self, helios_off):
        from orchestrator.helios_power import wake_helios
        from orchestrator.metrics import HELIOS_WAKE_TOTAL

        before = _metric(HELIOS_WAKE_TOTAL, result="disabled")
        with respx.mock:  # no routes — any HTTP would raise
            result = await wake_helios()
        assert result["ok"] is False
        assert result["skipped"] == "disabled"
        assert _metric(HELIOS_WAKE_TOTAL, result="disabled") == before + 1

    @pytest.mark.asyncio
    async def test_success_turns_on_plug(self, helios_on):
        from orchestrator.helios_power import wake_helios
        from orchestrator.metrics import HELIOS_WAKE_TOTAL

        before = _metric(HELIOS_WAKE_TOTAL, result="ok")
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
        assert _metric(HELIOS_WAKE_TOTAL, result="ok") == before + 1

    @pytest.mark.asyncio
    async def test_debounce_skips_second_wake(self, helios_on):
        from orchestrator.helios_power import wake_helios
        from orchestrator.metrics import HELIOS_WAKE_TOTAL

        before_ok = _metric(HELIOS_WAKE_TOTAL, result="ok")
        before_deb = _metric(HELIOS_WAKE_TOTAL, result="debounced")
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
        assert _metric(HELIOS_WAKE_TOTAL, result="ok") == before_ok + 1
        assert _metric(HELIOS_WAKE_TOTAL, result="debounced") == before_deb + 1

    @pytest.mark.asyncio
    async def test_zero_debounce_allows_repeat(self, helios_on, monkeypatch):
        from orchestrator.config import settings
        from orchestrator.helios_power import wake_helios

        monkeypatch.setattr(settings, "helios_wake_debounce_seconds", 0, raising=False)
        with respx.mock(base_url=_HA_URL) as mock:
            route = mock.post("/api/services/switch/turn_on").mock(return_value=Response(200, json=[]))
            await wake_helios()
            await wake_helios()
        assert route.call_count == 2

    @pytest.mark.asyncio
    async def test_http_error_swallowed(self, helios_on):
        from orchestrator.helios_power import wake_helios
        from orchestrator.metrics import HELIOS_WAKE_TOTAL

        before = _metric(HELIOS_WAKE_TOTAL, result="error")
        with respx.mock(base_url=_HA_URL) as mock:
            mock.post("/api/services/switch/turn_on").mock(return_value=Response(500, text="boom"))
            result = await wake_helios()
        assert result["ok"] is False
        assert "error" in result
        assert _metric(HELIOS_WAKE_TOTAL, result="error") == before + 1

    @pytest.mark.asyncio
    async def test_connect_error_swallowed_and_not_debounced(self, helios_on):
        """A failed wake must NOT arm the debounce — the next attempt should retry."""
        from orchestrator.helios_power import wake_helios

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
    @pytest.mark.asyncio
    async def test_disabled_no_op(self, helios_off):
        from orchestrator.helios_power import sleep_helios
        from orchestrator.metrics import HELIOS_SLEEP_TOTAL

        before = _metric(HELIOS_SLEEP_TOTAL, result="disabled")
        with respx.mock:
            result = await sleep_helios()
        assert result["ok"] is False
        assert result["skipped"] == "disabled"
        assert _metric(HELIOS_SLEEP_TOTAL, result="disabled") == before + 1

    @pytest.mark.asyncio
    async def test_success_turns_off_plug(self, helios_on):
        from orchestrator.helios_power import sleep_helios
        from orchestrator.metrics import HELIOS_SLEEP_TOTAL

        before = _metric(HELIOS_SLEEP_TOTAL, result="ok")
        with respx.mock(base_url=_HA_URL) as mock:
            route = mock.post("/api/services/switch/turn_off").mock(return_value=Response(200, json=[]))
            result = await sleep_helios()
        assert route.called
        assert result["ok"] is True
        assert result["action"] == "sleep"
        assert json.loads(route.calls[0].request.content) == {"entity_id": _SWITCH}
        assert _metric(HELIOS_SLEEP_TOTAL, result="ok") == before + 1

    @pytest.mark.asyncio
    async def test_error_swallowed(self, helios_on):
        from orchestrator.helios_power import sleep_helios
        from orchestrator.metrics import HELIOS_SLEEP_TOTAL

        before = _metric(HELIOS_SLEEP_TOTAL, result="error")
        with respx.mock(base_url=_HA_URL) as mock:
            mock.post("/api/services/switch/turn_off").mock(side_effect=httpx.TimeoutException("slow"))
            result = await sleep_helios()
        assert result["ok"] is False
        assert "TimeoutException" in result["error"]
        assert _metric(HELIOS_SLEEP_TOTAL, result="error") == before + 1


# ===========================================================================
# helios_power_status
# ===========================================================================


class TestPowerStatus:
    @pytest.mark.asyncio
    async def test_disabled_no_op(self, helios_off):
        from orchestrator.helios_power import helios_power_status

        with respx.mock:
            result = await helios_power_status()
        assert result["ok"] is False
        assert result["skipped"] == "disabled"

    @pytest.mark.asyncio
    async def test_running_when_on_and_high_watts(self, helios_on):
        from orchestrator.helios_power import helios_power_status
        from orchestrator.metrics import HELIOS_PLUG_WATTS

        with respx.mock(base_url=_HA_URL) as mock:
            mock.get(f"/api/states/{_SWITCH}").mock(return_value=Response(200, json={"state": "on"}))
            mock.get(f"/api/states/{_SENSOR}").mock(return_value=Response(200, json={"state": "142.5"}))
            result = await helios_power_status()
        assert result["ok"] is True
        assert result["switch"] == "on"
        assert result["watts"] == 142.5
        assert result["inferred"] == "running"
        assert HELIOS_PLUG_WATTS._value.get() == 142.5

    @pytest.mark.asyncio
    async def test_asleep_when_switch_off(self, helios_on):
        from orchestrator.helios_power import helios_power_status

        with respx.mock(base_url=_HA_URL) as mock:
            mock.get(f"/api/states/{_SWITCH}").mock(return_value=Response(200, json={"state": "off"}))
            mock.get(f"/api/states/{_SENSOR}").mock(return_value=Response(200, json={"state": "0"}))
            result = await helios_power_status()
        assert result["inferred"] == "asleep"
        assert result["switch"] == "off"

    @pytest.mark.asyncio
    async def test_asleep_when_on_but_low_watts(self, helios_on):
        from orchestrator.helios_power import helios_power_status

        with respx.mock(base_url=_HA_URL) as mock:
            mock.get(f"/api/states/{_SWITCH}").mock(return_value=Response(200, json={"state": "on"}))
            mock.get(f"/api/states/{_SENSOR}").mock(return_value=Response(200, json={"state": "3.1"}))
            result = await helios_power_status()
        assert result["inferred"] == "asleep"

    @pytest.mark.asyncio
    async def test_unknown_watts_when_unparseable(self, helios_on):
        from orchestrator.helios_power import helios_power_status

        with respx.mock(base_url=_HA_URL) as mock:
            mock.get(f"/api/states/{_SWITCH}").mock(return_value=Response(200, json={"state": "on"}))
            mock.get(f"/api/states/{_SENSOR}").mock(return_value=Response(200, json={"state": "unavailable"}))
            result = await helios_power_status()
        assert result["watts"] is None
        assert result["inferred"] == "unknown"

    @pytest.mark.asyncio
    async def test_error_swallowed(self, helios_on):
        from orchestrator.helios_power import helios_power_status

        with respx.mock(base_url=_HA_URL) as mock:
            mock.get(f"/api/states/{_SWITCH}").mock(side_effect=httpx.ConnectError("dns"))
            result = await helios_power_status()
        assert result["ok"] is False
        assert "ConnectError" in result["error"]

    @pytest.mark.asyncio
    async def test_status_counter_and_running_gauge(self, helios_on):
        from orchestrator.helios_power import helios_power_status
        from orchestrator.metrics import HELIOS_RUNNING, HELIOS_STATUS_TOTAL

        before_ok = _metric(HELIOS_STATUS_TOTAL, result="ok")
        with respx.mock(base_url=_HA_URL) as mock:
            mock.get(f"/api/states/{_SWITCH}").mock(return_value=Response(200, json={"state": "on"}))
            mock.get(f"/api/states/{_SENSOR}").mock(return_value=Response(200, json={"state": "200"}))
            await helios_power_status()
        assert _metric(HELIOS_STATUS_TOTAL, result="ok") == before_ok + 1
        assert HELIOS_RUNNING._value.get() == 1

        with respx.mock(base_url=_HA_URL) as mock:
            mock.get(f"/api/states/{_SWITCH}").mock(return_value=Response(200, json={"state": "off"}))
            mock.get(f"/api/states/{_SENSOR}").mock(return_value=Response(200, json={"state": "0"}))
            await helios_power_status()
        assert HELIOS_RUNNING._value.get() == 0

    @pytest.mark.asyncio
    async def test_status_counter_error(self, helios_on):
        from orchestrator.helios_power import helios_power_status
        from orchestrator.metrics import HELIOS_STATUS_TOTAL

        before = _metric(HELIOS_STATUS_TOTAL, result="error")
        with respx.mock(base_url=_HA_URL) as mock:
            mock.get(f"/api/states/{_SWITCH}").mock(side_effect=httpx.ConnectError("dns"))
            await helios_power_status()
        assert _metric(HELIOS_STATUS_TOTAL, result="error") == before + 1

    @pytest.mark.asyncio
    async def test_status_counter_disabled(self, helios_off):
        from orchestrator.helios_power import helios_power_status
        from orchestrator.metrics import HELIOS_STATUS_TOTAL

        before = _metric(HELIOS_STATUS_TOTAL, result="disabled")
        with respx.mock:
            await helios_power_status()
        assert _metric(HELIOS_STATUS_TOTAL, result="disabled") == before + 1


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
        import logging

        return [
            r
            for r in caplog.records
            if r.name == "orchestrator.helios_power" and r.levelno >= getattr(logging, min_level)
        ]

    @pytest.mark.asyncio
    async def test_repeated_failures_log_once_not_per_poll(self, helios_on, caplog):
        import logging

        from orchestrator.helios_power import helios_power_status

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

    @pytest.mark.asyncio
    async def test_recovery_logs_once(self, helios_on, caplog):
        import logging

        from orchestrator.helios_power import helios_power_status

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

    @pytest.mark.asyncio
    async def test_plug_on_escalates_to_warning_but_throttled(self, helios_on, caplog):
        import logging

        from orchestrator import helios_power
        from orchestrator.helios_power import helios_power_status

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

    @pytest.mark.asyncio
    async def test_transition_after_recovery_logs_again(self, helios_on, caplog):
        """A new outage after recovery is a new transition — it logs once again."""
        import logging

        from orchestrator.helios_power import helios_power_status

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


class TestConfigValidator:
    def test_enabled_without_ha_disables(self, caplog):
        import logging

        from orchestrator.config import Settings

        env = {
            "HELIOS_WAKE_ENABLED": "true",
            "HA_URL": "",
            "HA_TOKEN": "",
            "API_TOKEN": "x",
            "PIHOLE_PASSWORD": "x",
        }
        with patch.dict(os.environ, env, clear=False), caplog.at_level(logging.ERROR, logger="orchestrator.config"):
            s = Settings()
        assert s.helios_wake_enabled is False
        assert any("HELIOS_WAKE_ENABLED=true" in r.getMessage() for r in caplog.records)

    def test_enabled_with_ha_stays_enabled(self):
        from orchestrator.config import Settings

        env = {
            "HELIOS_WAKE_ENABLED": "true",
            "HA_URL": "http://ha.test:8123",
            "HA_TOKEN": "tok",
            "API_TOKEN": "x",
            "PIHOLE_PASSWORD": "x",
        }
        with patch.dict(os.environ, env, clear=False):
            s = Settings()
        assert s.helios_wake_enabled is True

    def test_negative_debounce_clamped(self):
        from orchestrator.config import Settings

        env = {
            "HELIOS_WAKE_ENABLED": "false",
            "HELIOS_WAKE_DEBOUNCE_SECONDS": "-5",
            "HA_TOKEN": "x",
            "API_TOKEN": "x",
            "PIHOLE_PASSWORD": "x",
        }
        with patch.dict(os.environ, env, clear=False):
            s = Settings()
        assert s.helios_wake_debounce_seconds == 0

    def test_disabled_without_ha_never_raises(self):
        from orchestrator.config import Settings

        env = {
            "HELIOS_WAKE_ENABLED": "false",
            "HA_URL": "",
            "HA_TOKEN": "",
            "API_TOKEN": "x",
            "PIHOLE_PASSWORD": "x",
        }
        with patch.dict(os.environ, env, clear=False):
            s = Settings()
        assert s.helios_wake_enabled is False

    def test_bad_entity_id_disables(self, caplog):
        import logging

        from orchestrator.config import Settings

        env = {
            "HELIOS_WAKE_ENABLED": "true",
            "HA_URL": "http://ha.test:8123",
            "HA_TOKEN": "tok",
            "HELIOS_PLUG_ENTITY": "switch.bad id/with-slash",
            "API_TOKEN": "x",
            "PIHOLE_PASSWORD": "x",
        }
        with patch.dict(os.environ, env, clear=False), caplog.at_level(logging.ERROR, logger="orchestrator.config"):
            s = Settings()
        assert s.helios_wake_enabled is False
        assert any("not a valid" in r.getMessage() for r in caplog.records)

    def test_bad_power_sensor_id_disables(self):
        from orchestrator.config import Settings

        env = {
            "HELIOS_WAKE_ENABLED": "true",
            "HA_URL": "http://ha.test:8123",
            "HA_TOKEN": "tok",
            "HELIOS_PLUG_POWER_SENSOR": "../../etc/passwd",
            "API_TOKEN": "x",
            "PIHOLE_PASSWORD": "x",
        }
        with patch.dict(os.environ, env, clear=False):
            s = Settings()
        assert s.helios_wake_enabled is False


# ===========================================================================
# cloud_brain wake hook
# ===========================================================================


def _bare_cloud_brain():
    """Construct a CloudBrain without running __init__ — the wake hook only
    touches module-level `shared`/`helios_power`, not instance state."""
    from orchestrator.cloud_brain import CloudBrain

    cb = CloudBrain.__new__(CloudBrain)
    cb._model_name = "test-model"
    return cb


class TestBrainAsleepWakeHook:
    @pytest.mark.asyncio
    async def test_maybe_wake_dispatches_when_enabled(self, monkeypatch):
        from orchestrator import shared

        monkeypatch.setattr(shared, "HELIOS_WAKE_ENABLED", True, raising=False)
        cb = _bare_cloud_brain()
        routing: dict = {}
        mock_wake = AsyncMock(return_value={"ok": True})
        with patch("orchestrator.helios_power.wake_helios", mock_wake):
            woke = cb._maybe_wake_helios(routing)
            # Strong ref held while in flight (prevents GC of the detached task)
            from orchestrator import cloud_brain

            assert len(cloud_brain._HELIOS_WAKE_TASKS) == 1
            # Drain the detached task
            import asyncio

            pending = [t for t in asyncio.all_tasks() if not t.done() and t is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        assert woke is True
        assert routing["helios_wake"] == "dispatched"
        mock_wake.assert_called_once()
        # Done-callback cleared the set
        assert len(cloud_brain._HELIOS_WAKE_TASKS) == 0

    @pytest.mark.asyncio
    async def test_maybe_wake_no_op_when_disabled(self, monkeypatch):
        from orchestrator import shared

        monkeypatch.setattr(shared, "HELIOS_WAKE_ENABLED", False, raising=False)
        cb = _bare_cloud_brain()
        routing: dict = {}
        mock_wake = AsyncMock(return_value={"ok": True})
        with patch("orchestrator.helios_power.wake_helios", mock_wake):
            woke = cb._maybe_wake_helios(routing)
        assert woke is False
        assert "helios_wake" not in routing
        mock_wake.assert_not_called()

    @pytest.mark.asyncio
    async def test_asleep_response_message_reflects_wake(self, monkeypatch):
        from orchestrator import shared

        monkeypatch.setattr(shared, "HELIOS_WAKE_ENABLED", True, raising=False)
        cb = _bare_cloud_brain()
        routing: dict = {}
        mock_wake = AsyncMock(return_value={"ok": True})
        with patch("orchestrator.helios_power.wake_helios", mock_wake):
            resp = cb._brain_asleep_response(stream=False, routing_info=routing)
            import asyncio

            pending = [t for t in asyncio.all_tasks() if not t.done() and t is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        payload = json.loads(resp.body)
        content = payload["choices"][0]["message"]["content"]
        assert "waking it now" in content.lower()
        assert routing["mode"] == "brain_asleep"

    @pytest.mark.asyncio
    async def test_asleep_response_default_message_when_disabled(self, monkeypatch):
        from orchestrator import shared

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
    def test_helios_tool_hidden_when_disabled(self, monkeypatch):
        from orchestrator import shared
        from orchestrator.tool_definitions import get_all_tools

        monkeypatch.setattr(shared, "HELIOS_WAKE_ENABLED", False, raising=False)
        names = {t["function"]["name"] for t in get_all_tools()}
        assert "helios_power" not in names

    def test_helios_tool_present_when_enabled(self, monkeypatch):
        from orchestrator import shared
        from orchestrator.tool_definitions import get_all_tools

        monkeypatch.setattr(shared, "HELIOS_WAKE_ENABLED", True, raising=False)
        names = {t["function"]["name"] for t in get_all_tools()}
        assert "helios_power" in names


# ===========================================================================
# tool_handlers dispatch
# ===========================================================================


class TestToolHandler:
    @pytest.mark.asyncio
    async def test_wake_action(self):
        from orchestrator.tool_handlers import _reg_helios_power

        with patch("orchestrator.helios_power.wake_helios", AsyncMock(return_value={"ok": True, "action": "wake"})):
            out = await _reg_helios_power({"action": "wake"})
        assert "waking helios" in out.lower()

    @pytest.mark.asyncio
    async def test_wake_debounced_message(self):
        from orchestrator.tool_handlers import _reg_helios_power

        with patch(
            "orchestrator.helios_power.wake_helios",
            AsyncMock(return_value={"ok": True, "skipped": "debounced", "retry_after_s": 120}),
        ):
            out = await _reg_helios_power({"action": "wake"})
        assert "already waking" in out.lower()

    @pytest.mark.asyncio
    async def test_sleep_action(self):
        from orchestrator.tool_handlers import _reg_helios_power

        with patch("orchestrator.helios_power.sleep_helios", AsyncMock(return_value={"ok": True, "action": "sleep"})):
            out = await _reg_helios_power({"action": "sleep"})
        assert "cut power" in out.lower()

    @pytest.mark.asyncio
    async def test_status_action_default(self):
        from orchestrator.tool_handlers import _reg_helios_power

        with patch(
            "orchestrator.helios_power.helios_power_status",
            AsyncMock(return_value={"ok": True, "switch": "on", "watts": 150.0, "inferred": "running"}),
        ):
            out = await _reg_helios_power({})  # defaults to status
        assert "running" in out.lower()
        assert "150w" in out.lower()

    @pytest.mark.asyncio
    async def test_disabled_message(self):
        from orchestrator.tool_handlers import _reg_helios_power

        with patch(
            "orchestrator.helios_power.wake_helios",
            AsyncMock(return_value={"ok": False, "skipped": "disabled", "reason": "off"}),
        ):
            out = await _reg_helios_power({"action": "wake"})
        assert "turned off" in out.lower()
