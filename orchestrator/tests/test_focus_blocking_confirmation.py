"""
Tests for focus-mode Pi-hole blocking confirmation.

Regression guard for: tool_start_focus telling the user "Distracting sites are
blocked." whenever PiHoleMultiClient.enable_focus_blocking() returned
success=True — including no-op successes (blocking disabled in config, no
instances, empty focus group, every per-domain PUT rejected).

Covers:
- pihole_client.blocking_confirmed()
- PiHoleMultiClient._apply_all domains_toggled aggregation
- focus_manager.tool_start_focus claim gating
- focus_manager.tool_focus_sprint("next_sprint") re-enable counter gating
"""

import logging
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.metrics import PIHOLE_BLOCKING_TOGGLES
from orchestrator.pihole_client import (
    PiHoleClient,
    PiHoleMultiClient,
    PiHoleResult,
    blocking_confirmed,
)
from orchestrator.tests._helpers import metric_value

# ---------------------------------------------------------------------------
# blocking_confirmed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "confirmed"),
    [
        pytest.param(PiHoleResult(True, "ok", {"domains_toggled": 3}), True, id="toggled"),
        pytest.param(PiHoleResult(True, "Focus blocking disabled in config"), False, id="details-none"),
        pytest.param(PiHoleResult(True, "ok", {"domains_toggled": 0}), False, id="zero-toggled"),
        pytest.param(PiHoleResult(True, "ok", {"succeeded": ["a"]}), False, id="key-missing"),
        pytest.param(PiHoleResult(False, "boom", {"domains_toggled": 5}), False, id="failure-with-toggled"),
    ],
)
def test_blocking_confirmed(result, confirmed):
    assert blocking_confirmed(result) is confirmed


# ---------------------------------------------------------------------------
# PiHoleMultiClient._apply_all aggregation
# ---------------------------------------------------------------------------


def _client(name, result=None, exc=None):
    c = PiHoleClient(url=f"http://{name}", name=name)
    if exc is not None:
        c.set_focus_group_enabled = AsyncMock(side_effect=exc)
    else:
        c.set_focus_group_enabled = AsyncMock(return_value=result)
    return c


def _toggled(name, n):
    return PiHoleResult(
        success=True,
        message=f"[{name}] Focus blocking enabled ({n} domains)",
        details={"instance": name, "group": "focus_blocklist", "enabled": True, "domains_toggled": n},
    )


class TestApplyAllAggregation:
    async def test_sums_toggled_across_instances(self):
        multi = PiHoleMultiClient([_client("a", _toggled("a", 3)), _client("b", _toggled("b", 2))])
        result = await multi.enable_focus_blocking()
        assert result.success is True
        assert result.details["domains_toggled"] == 5
        assert result.details["succeeded"] == ["a", "b"]
        assert blocking_confirmed(result) is True

    async def test_empty_focus_group_is_unconfirmed_success(self):
        empty = PiHoleResult(success=True, message="[a] No domains in focus group to toggle")
        multi = PiHoleMultiClient([_client("a", empty)])
        result = await multi.enable_focus_blocking()
        assert result.success is True
        assert result.details["domains_toggled"] == 0
        assert blocking_confirmed(result) is False

    async def test_all_puts_rejected_is_unconfirmed(self):
        multi = PiHoleMultiClient([_client("a", _toggled("a", 0))])
        result = await multi.enable_focus_blocking()
        assert result.success is True
        assert blocking_confirmed(result) is False

    async def test_disabled_in_config(self):
        c = _client("a", _toggled("a", 3))
        multi = PiHoleMultiClient([c], enabled=False)
        result = await multi.enable_focus_blocking()
        assert result.success is True
        assert result.details is None
        assert blocking_confirmed(result) is False
        c.set_focus_group_enabled.assert_not_called()

    async def test_no_clients(self):
        result = await PiHoleMultiClient([]).enable_focus_blocking()
        assert result.success is True
        assert result.details is None
        assert blocking_confirmed(result) is False

    async def test_partial_failure_with_exception(self):
        multi = PiHoleMultiClient([_client("a", _toggled("a", 2)), _client("b", exc=RuntimeError("down"))])
        result = await multi.enable_focus_blocking()
        assert result.success is True
        assert result.details["domains_toggled"] == 2
        assert result.details["failed"] == ["b"]
        assert "failed on b" in result.message
        assert blocking_confirmed(result) is True

    async def test_all_failed(self):
        multi = PiHoleMultiClient(
            [
                _client("a", PiHoleResult(False, "[a] auth failed")),
                _client("b", exc=RuntimeError("down")),
            ]
        )
        result = await multi.enable_focus_blocking()
        assert result.success is False
        assert result.details["domains_toggled"] == 0
        assert sorted(result.details["failed"]) == ["a", "b"]
        assert blocking_confirmed(result) is False

    async def test_disable_path_also_aggregates(self):
        multi = PiHoleMultiClient([_client("a", _toggled("a", 4))])
        result = await multi.disable_focus_blocking()
        assert result.details["enabled"] is False
        assert result.details["domains_toggled"] == 4


# ---------------------------------------------------------------------------
# focus_manager (fixture ``selfcare_focus`` from fixtures_selfcare)
# ---------------------------------------------------------------------------


def _enable_calls(toggles):
    return [c for c in toggles.labels.call_args_list if c.kwargs.get("action") == "enable"]


# ---------------------------------------------------------------------------
# tool_start_focus claim gating
# ---------------------------------------------------------------------------


class TestStartFocusBlockingClaim:
    async def test_confirmed_blocking_is_claimed(self, selfcare_focus):
        selfcare_focus["pihole"].enable_focus_blocking.return_value = PiHoleResult(
            True,
            "Focus blocking enabled on a",
            {"succeeded": ["a"], "failed": [], "enabled": True, "domains_toggled": 4},
        )
        reply = await selfcare_focus["fm"].tool_start_focus(
            task="Write", duration=25, block_sites=True, audio="silence"
        )
        assert "Distracting sites are blocked." in reply
        assert selfcare_focus["session"]["block_sites"] is True
        assert len(_enable_calls(selfcare_focus["toggles"])) == 1

    async def test_disabled_in_config_not_claimed(self, selfcare_focus, caplog):
        selfcare_focus["pihole"].enable_focus_blocking.return_value = PiHoleResult(
            True, "Focus blocking disabled in config"
        )
        with caplog.at_level(logging.INFO, logger="orchestrator.focus_manager"):
            reply = await selfcare_focus["fm"].tool_start_focus(
                task="Write", duration=25, block_sites=True, audio="silence"
            )
        assert "blocked" not in reply.lower()
        assert selfcare_focus["session"]["block_sites"] is False
        assert _enable_calls(selfcare_focus["toggles"]) == []
        assert any("Site blocking not active" in r.getMessage() and r.levelno == logging.INFO for r in caplog.records)

    async def test_zero_toggled_not_claimed(self, selfcare_focus):
        selfcare_focus["pihole"].enable_focus_blocking.return_value = PiHoleResult(
            True,
            "Focus blocking enabled on x",
            {"succeeded": ["x"], "failed": [], "enabled": True, "domains_toggled": 0},
        )
        reply = await selfcare_focus["fm"].tool_start_focus(
            task="Write", duration=25, block_sites=True, audio="silence"
        )
        assert "blocked" not in reply.lower()
        assert selfcare_focus["session"]["block_sites"] is False
        assert _enable_calls(selfcare_focus["toggles"]) == []

    async def test_failure_not_claimed(self, selfcare_focus, caplog):
        selfcare_focus["pihole"].enable_focus_blocking.return_value = PiHoleResult(
            False,
            "Focus blocking enable failed on all instances: a",
            {"succeeded": [], "failed": ["a"], "enabled": True, "domains_toggled": 0},
        )
        with caplog.at_level(logging.WARNING, logger="orchestrator.focus_manager"):
            reply = await selfcare_focus["fm"].tool_start_focus(
                task="Write", duration=25, block_sites=True, audio="silence"
            )
        assert "blocked" not in reply.lower()
        assert selfcare_focus["session"]["block_sites"] is False
        assert _enable_calls(selfcare_focus["toggles"]) == []
        assert any("Could not enable blocking" in r.getMessage() for r in caplog.records)
        # Session itself still starts — blocking failure is non-fatal
        assert selfcare_focus["session"]["active"] is True

    async def test_block_sites_false_never_calls_pihole(self, selfcare_focus):
        reply = await selfcare_focus["fm"].tool_start_focus(
            task="Write", duration=25, block_sites=False, audio="silence"
        )
        selfcare_focus["pihole"].enable_focus_blocking.assert_not_called()
        assert "blocked" not in reply.lower()


# ---------------------------------------------------------------------------
# tool_focus_sprint next_sprint re-enable gating (real prometheus counter)
# ---------------------------------------------------------------------------


def _active_blocking_session(session):
    session.update(
        {
            "active": True,
            "task": "Code",
            "started": datetime.now(),
            "duration": 25,
            "break_duration": 5,
            "sprint_count": 1,
            "sprints_planned": 3,
            "check_in_interval": None,
            "audio_player": None,
            "audio_source": "silence",
            "block_sites": True,
        }
    )


class TestSprintReEnable:
    @pytest.fixture
    def real_counter(self, selfcare_focus):
        """The real bgw_pihole_blocking_toggles_total counter; yields a reader for action=enable."""
        with patch.object(selfcare_focus["fm"], "PIHOLE_BLOCKING_TOGGLES", PIHOLE_BLOCKING_TOGGLES):
            yield lambda: metric_value(PIHOLE_BLOCKING_TOGGLES, action="enable")

    async def test_noop_success_does_not_increment_and_warns(self, selfcare_focus, real_counter, caplog):
        _active_blocking_session(selfcare_focus["session"])
        selfcare_focus["pihole"].enable_focus_blocking.return_value = PiHoleResult(
            True, "Focus blocking disabled in config"
        )
        before = real_counter()
        with caplog.at_level(logging.WARNING, logger="orchestrator.focus_manager"):
            await selfcare_focus["fm"].tool_focus_sprint(action="next_sprint")
        assert real_counter() == before
        assert any(
            "Could not re-enable blocking for sprint" in r.getMessage() and r.levelno == logging.WARNING
            for r in caplog.records
        )

    async def test_zero_toggled_does_not_increment(self, selfcare_focus, real_counter):
        _active_blocking_session(selfcare_focus["session"])
        selfcare_focus["pihole"].enable_focus_blocking.return_value = PiHoleResult(
            True, "ok", {"succeeded": ["a"], "failed": [], "enabled": True, "domains_toggled": 0}
        )
        before = real_counter()
        await selfcare_focus["fm"].tool_focus_sprint(action="next_sprint")
        assert real_counter() == before

    async def test_confirmed_increments(self, selfcare_focus, real_counter, caplog):
        _active_blocking_session(selfcare_focus["session"])
        selfcare_focus["pihole"].enable_focus_blocking.return_value = PiHoleResult(
            True, "ok", {"succeeded": ["a"], "failed": [], "enabled": True, "domains_toggled": 3}
        )
        before = real_counter()
        with caplog.at_level(logging.WARNING, logger="orchestrator.focus_manager"):
            reply = await selfcare_focus["fm"].tool_focus_sprint(action="next_sprint")
        assert real_counter() == before + 1
        assert not any("Could not re-enable" in r.getMessage() for r in caplog.records)
        assert "Sprint 2" in reply

    async def test_no_block_sites_skips_pihole(self, selfcare_focus, real_counter):
        _active_blocking_session(selfcare_focus["session"])
        selfcare_focus["session"]["block_sites"] = False
        before = real_counter()
        await selfcare_focus["fm"].tool_focus_sprint(action="next_sprint")
        selfcare_focus["pihole"].enable_focus_blocking.assert_not_called()
        assert real_counter() == before
