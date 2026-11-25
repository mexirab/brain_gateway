"""
Tests for feature-flag tool gating in tool_definitions.get_all_tools().

The JESS_ADVANCED gate layers over the per-tool EXPERT_ENABLED flag:
- ask_expert appears only if BOTH JESS_ADVANCED and EXPERT_ENABLED are true
- query_budget, finance_status, check_claude_activity require JESS_ADVANCED only
(code_agent was removed 2026-10-21; it must never come back as a tool.)
The optional workout / meal tools follow WORKOUTS_ENABLED / MEALS_ENABLED
(default off), independently; core selfcare_log survives both gates.
"""

import pytest

from orchestrator import shared, tool_definitions
from orchestrator.tool_registry import is_registered

ADVANCED_ONLY = {"query_budget", "finance_status", "check_claude_activity"}
WORKOUT_TOOLS = {"generate_workout", "log_set", "workout_status", "modify_workout", "workout_setup"}


def _tool_names(**flags):
    for flag, value in flags.items():
        setattr(shared, flag, value)
    return {t.get("function", {}).get("name") for t in tool_definitions.get_all_tools()}


@pytest.fixture(autouse=True)
def _restore_flags(monkeypatch):
    # Every flag a test may flip is restored by monkeypatch afterwards.
    for flag in ("JESS_ADVANCED", "EXPERT_ENABLED", "WORKOUTS_ENABLED", "MEALS_ENABLED"):
        monkeypatch.setattr(shared, flag, getattr(shared, flag))


@pytest.mark.parametrize(
    "advanced, expert, has_expert, has_advanced",
    [
        pytest.param(False, False, False, False, id="all-off"),
        pytest.param(True, True, True, True, id="all-on"),
        # The JESS_ADVANCED gate layers over the per-tool flag, it doesn't replace it.
        pytest.param(False, True, False, False, id="expert-without-advanced"),
        pytest.param(True, False, False, True, id="advanced-without-expert"),
    ],
)
def test_advanced_gate_layers_over_expert_flag(advanced, expert, has_expert, has_advanced):
    names = _tool_names(JESS_ADVANCED=advanced, EXPERT_ENABLED=expert)
    assert ("ask_expert" in names) is has_expert
    assert (names >= ADVANCED_ONLY) is has_advanced
    assert ADVANCED_ONLY.isdisjoint(names) is not has_advanced


def test_tool_count_delta_is_exactly_four():
    """All-flags-on minus all-flags-off exposes exactly the 4 advanced tools."""
    off = _tool_names(JESS_ADVANCED=False, EXPERT_ENABLED=False)
    on = _tool_names(JESS_ADVANCED=True, EXPERT_ENABLED=True)
    assert on - off == ADVANCED_ONLY | {"ask_expert"}
    assert len(on) - len(off) == 4


def test_code_agent_is_gone(monkeypatch):
    """The code agent (a model with write_file/run_command) was removed; no flag
    combination may bring a `code_agent` tool back into the schema."""
    monkeypatch.setattr(shared, "CODE_AGENT_ENABLED", True, raising=False)
    assert "code_agent" not in _tool_names(JESS_ADVANCED=True, EXPERT_ENABLED=True)
    from orchestrator import tool_handlers  # noqa: F401  (registers every handler)

    assert not is_registered("code_agent")


@pytest.mark.parametrize(
    "workouts, meals",
    [(False, False), (True, True), (True, False), (False, True)],
    ids=["both-off", "both-on", "workouts-only", "meals-only"],
)
def test_workouts_and_meals_gate_independently(workouts, meals):
    names = _tool_names(WORKOUTS_ENABLED=workouts, MEALS_ENABLED=meals)
    assert (names >= WORKOUT_TOOLS) is workouts
    assert WORKOUT_TOOLS.isdisjoint(names) is not workouts
    assert ("log_meal" in names) is meals
    # Core selfcare_log stays available regardless of the optional features.
    assert "selfcare_log" in names
