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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.pihole_client import (
    PiHoleClient,
    PiHoleMultiClient,
    PiHoleResult,
    blocking_confirmed,
)

# ---------------------------------------------------------------------------
# blocking_confirmed
# ---------------------------------------------------------------------------


class TestBlockingConfirmed:
    def test_success_with_toggled_domains(self):
        assert blocking_confirmed(PiHoleResult(True, "ok", {"domains_toggled": 3})) is True

    def test_success_details_none(self):
        assert blocking_confirmed(PiHoleResult(True, "Focus blocking disabled in config")) is False

    def test_success_zero_toggled(self):
        assert blocking_confirmed(PiHoleResult(True, "ok", {"domains_toggled": 0})) is False

    def test_success_details_missing_key(self):
        assert blocking_confirmed(PiHoleResult(True, "ok", {"succeeded": ["a"]})) is False

    def test_failure_even_with_toggled(self):
        assert blocking_confirmed(PiHoleResult(False, "boom", {"domains_toggled": 5})) is False


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
# focus_manager fixtures
# ---------------------------------------------------------------------------


def _default_session():
    return {
        "active": False,
        "task": None,
        "started": None,
        "duration": None,
        "break_duration": None,
        "job_id": None,
        "audio_player": None,
        "block_sites": False,
        "task_description": None,
        "sprint_count": 0,
        "sprints_planned": None,
        "check_in_interval": None,
        "check_in_job_id": None,
        "total_focus_minutes": 0,
        "audio_source": "endel",
    }


@pytest.fixture
def focus_env():
    """Patch focus_manager's external deps. PIHOLE_BLOCKING_TOGGLES is patched
    with a MagicMock so tests can assert on .labels(...).inc()."""
    from orchestrator import focus_manager, shared

    session = _default_session()
    original_session = shared.current_focus_session.to_dict()

    pihole = AsyncMock()
    pihole.enable_focus_blocking = AsyncMock()
    pihole.disable_focus_blocking = AsyncMock(return_value=PiHoleResult(True, "ok"))

    ha = AsyncMock()
    ha_result = MagicMock(success=True, message="ok")
    ha.call_service = AsyncMock(return_value=ha_result)

    sched = MagicMock()
    sched.get_job = MagicMock(return_value=None)

    toggles = MagicMock()

    with (
        patch.object(shared, "current_focus_session", session),
        patch.object(shared, "ha_client", ha),
        patch.object(shared, "scheduler", sched),
        patch("orchestrator.focus_manager.ha_client", ha),
        patch("orchestrator.focus_manager.scheduler", sched),
        patch("orchestrator.focus_manager.get_pihole_client", return_value=pihole),
        patch("orchestrator.focus_manager._announce_voice", AsyncMock()),
        patch("orchestrator.focus_manager.state_store", MagicMock()),
        patch("orchestrator.focus_manager.current_focus_session", session),
        patch("orchestrator.focus_manager.FOCUS_AUDIO_PLAYER", "media_player.office"),
        patch("orchestrator.focus_manager.FOCUS_SESSIONS_STARTED", MagicMock()),
        patch("orchestrator.focus_manager.FOCUS_SESSIONS_COMPLETED", MagicMock()),
        patch("orchestrator.focus_manager.FOCUS_SESSIONS_STOPPED_EARLY", MagicMock()),
        patch("orchestrator.focus_manager.FOCUS_SESSION_DURATION", MagicMock()),
        patch("orchestrator.focus_manager.FOCUS_ACTIVE", MagicMock()),
        patch("orchestrator.focus_manager.PIHOLE_BLOCKING_TOGGLES", toggles),
        patch.object(focus_manager, "_start_audio_for_source", new_callable=AsyncMock, return_value=False),
    ):
        yield {"fm": focus_manager, "session": session, "pihole": pihole, "toggles": toggles}

    shared.current_focus_session.update(original_session)


def _enable_calls(toggles):
    return [c for c in toggles.labels.call_args_list if c.kwargs.get("action") == "enable"]


# ---------------------------------------------------------------------------
# tool_start_focus claim gating
# ---------------------------------------------------------------------------


class TestStartFocusBlockingClaim:
    async def test_confirmed_blocking_is_claimed(self, focus_env):
        focus_env["pihole"].enable_focus_blocking.return_value = PiHoleResult(
            True,
            "Focus blocking enabled on a",
            {"succeeded": ["a"], "failed": [], "enabled": True, "domains_toggled": 4},
        )
        reply = await focus_env["fm"].tool_start_focus(task="Write", duration=25, block_sites=True, audio="silence")
        assert "Distracting sites are blocked." in reply
        assert focus_env["session"]["block_sites"] is True
        assert len(_enable_calls(focus_env["toggles"])) == 1

    async def test_disabled_in_config_not_claimed(self, focus_env, caplog):
        focus_env["pihole"].enable_focus_blocking.return_value = PiHoleResult(True, "Focus blocking disabled in config")
        with caplog.at_level(logging.INFO, logger="orchestrator.focus_manager"):
            reply = await focus_env["fm"].tool_start_focus(task="Write", duration=25, block_sites=True, audio="silence")
        assert "blocked" not in reply.lower()
        assert focus_env["session"]["block_sites"] is False
        assert _enable_calls(focus_env["toggles"]) == []
        assert any("Site blocking not active" in r.getMessage() and r.levelno == logging.INFO for r in caplog.records)

    async def test_zero_toggled_not_claimed(self, focus_env):
        focus_env["pihole"].enable_focus_blocking.return_value = PiHoleResult(
            True,
            "Focus blocking enabled on x",
            {"succeeded": ["x"], "failed": [], "enabled": True, "domains_toggled": 0},
        )
        reply = await focus_env["fm"].tool_start_focus(task="Write", duration=25, block_sites=True, audio="silence")
        assert "blocked" not in reply.lower()
        assert focus_env["session"]["block_sites"] is False
        assert _enable_calls(focus_env["toggles"]) == []

    async def test_failure_not_claimed(self, focus_env, caplog):
        focus_env["pihole"].enable_focus_blocking.return_value = PiHoleResult(
            False,
            "Focus blocking enable failed on all instances: a",
            {"succeeded": [], "failed": ["a"], "enabled": True, "domains_toggled": 0},
        )
        with caplog.at_level(logging.WARNING, logger="orchestrator.focus_manager"):
            reply = await focus_env["fm"].tool_start_focus(task="Write", duration=25, block_sites=True, audio="silence")
        assert "blocked" not in reply.lower()
        assert focus_env["session"]["block_sites"] is False
        assert _enable_calls(focus_env["toggles"]) == []
        assert any("Could not enable blocking" in r.getMessage() for r in caplog.records)
        # Session itself still starts — blocking failure is non-fatal
        assert focus_env["session"]["active"] is True

    async def test_block_sites_false_never_calls_pihole(self, focus_env):
        reply = await focus_env["fm"].tool_start_focus(task="Write", duration=25, block_sites=False, audio="silence")
        focus_env["pihole"].enable_focus_blocking.assert_not_called()
        assert "blocked" not in reply.lower()


# ---------------------------------------------------------------------------
# tool_focus_sprint next_sprint re-enable gating (real prometheus counter)
# ---------------------------------------------------------------------------


def _active_blocking_session(session):
    from datetime import datetime

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
    def real_counter(self, focus_env):
        from orchestrator.metrics import PIHOLE_BLOCKING_TOGGLES

        with patch("orchestrator.focus_manager.PIHOLE_BLOCKING_TOGGLES", PIHOLE_BLOCKING_TOGGLES):
            yield PIHOLE_BLOCKING_TOGGLES.labels(action="enable")

    async def test_noop_success_does_not_increment_and_warns(self, focus_env, real_counter, caplog):
        _active_blocking_session(focus_env["session"])
        focus_env["pihole"].enable_focus_blocking.return_value = PiHoleResult(True, "Focus blocking disabled in config")
        before = real_counter._value.get()
        with caplog.at_level(logging.WARNING, logger="orchestrator.focus_manager"):
            await focus_env["fm"].tool_focus_sprint(action="next_sprint")
        assert real_counter._value.get() == before
        assert any(
            "Could not re-enable blocking for sprint" in r.getMessage() and r.levelno == logging.WARNING
            for r in caplog.records
        )

    async def test_zero_toggled_does_not_increment(self, focus_env, real_counter):
        _active_blocking_session(focus_env["session"])
        focus_env["pihole"].enable_focus_blocking.return_value = PiHoleResult(
            True, "ok", {"succeeded": ["a"], "failed": [], "enabled": True, "domains_toggled": 0}
        )
        before = real_counter._value.get()
        await focus_env["fm"].tool_focus_sprint(action="next_sprint")
        assert real_counter._value.get() == before

    async def test_confirmed_increments(self, focus_env, real_counter, caplog):
        _active_blocking_session(focus_env["session"])
        focus_env["pihole"].enable_focus_blocking.return_value = PiHoleResult(
            True, "ok", {"succeeded": ["a"], "failed": [], "enabled": True, "domains_toggled": 3}
        )
        before = real_counter._value.get()
        with caplog.at_level(logging.WARNING, logger="orchestrator.focus_manager"):
            reply = await focus_env["fm"].tool_focus_sprint(action="next_sprint")
        assert real_counter._value.get() == before + 1
        assert not any("Could not re-enable" in r.getMessage() for r in caplog.records)
        assert "Sprint 2" in reply

    async def test_no_block_sites_skips_pihole(self, focus_env, real_counter):
        _active_blocking_session(focus_env["session"])
        focus_env["session"]["block_sites"] = False
        before = real_counter._value.get()
        await focus_env["fm"].tool_focus_sprint(action="next_sprint")
        focus_env["pihole"].enable_focus_blocking.assert_not_called()
        assert real_counter._value.get() == before
