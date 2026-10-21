"""
Tests for JESS_ADVANCED tool gating in tool_definitions.get_all_tools().

The gate layers over the per-tool EXPERT_ENABLED flag:
- ask_expert appears only if BOTH JESS_ADVANCED and EXPERT_ENABLED are true
- query_budget, finance_status, check_claude_activity require JESS_ADVANCED only
(code_agent was removed 2026-10-21; it must never come back as a tool.)
"""

from orchestrator import shared, tool_definitions

ADVANCED_TOOLS = {
    "ask_expert",
    "query_budget",
    "finance_status",
    "check_claude_activity",
}


def _tool_names(tools):
    return {t.get("function", {}).get("name") for t in tools}


def test_advanced_off_all_per_tool_off_hides_all_four(monkeypatch):
    """JESS_ADVANCED=False with per-tool flags off → none of the 4 advanced tools."""
    monkeypatch.setattr(shared, "JESS_ADVANCED", False)
    monkeypatch.setattr(shared, "EXPERT_ENABLED", False)

    names = _tool_names(tool_definitions.get_all_tools())

    assert ADVANCED_TOOLS.isdisjoint(names), (
        f"Expected none of {ADVANCED_TOOLS} when JESS_ADVANCED=False; found: {ADVANCED_TOOLS & names}"
    )


def test_advanced_on_all_per_tool_on_exposes_all_four(monkeypatch):
    """JESS_ADVANCED=True + per-tool flags on → all 4 advanced tools present."""
    monkeypatch.setattr(shared, "JESS_ADVANCED", True)
    monkeypatch.setattr(shared, "EXPERT_ENABLED", True)

    names = _tool_names(tool_definitions.get_all_tools())

    missing = ADVANCED_TOOLS - names
    assert not missing, f"Expected all advanced tools present; missing: {missing}"


def test_advanced_off_but_expert_enabled_still_hides_ask_expert(monkeypatch):
    """JESS_ADVANCED=False, EXPERT_ENABLED=True → ask_expert still hidden: the
    JESS_ADVANCED gate layers over the per-tool flag, it doesn't replace it."""
    monkeypatch.setattr(shared, "JESS_ADVANCED", False)
    monkeypatch.setattr(shared, "EXPERT_ENABLED", True)

    assert "ask_expert" not in _tool_names(tool_definitions.get_all_tools())


def test_advanced_on_but_expert_disabled_still_hides_ask_expert(monkeypatch):
    """JESS_ADVANCED=True, EXPERT_ENABLED=False → ask_expert still hidden."""
    monkeypatch.setattr(shared, "JESS_ADVANCED", True)
    monkeypatch.setattr(shared, "EXPERT_ENABLED", False)

    assert "ask_expert" not in _tool_names(tool_definitions.get_all_tools())


def test_tool_count_delta_is_exactly_four(monkeypatch):
    """All-flags-on minus all-flags-off should expose exactly 4 extra tools."""
    monkeypatch.setattr(shared, "JESS_ADVANCED", False)
    monkeypatch.setattr(shared, "EXPERT_ENABLED", False)
    off_count = len(tool_definitions.get_all_tools())

    monkeypatch.setattr(shared, "JESS_ADVANCED", True)
    monkeypatch.setattr(shared, "EXPERT_ENABLED", True)
    on_count = len(tool_definitions.get_all_tools())

    assert on_count - off_count == 4, (
        f"Expected delta of 4 advanced tools; got {on_count} - {off_count} = {on_count - off_count}"
    )


def test_code_agent_is_gone(monkeypatch):
    """The code agent (a model with write_file/run_command) was removed; no flag
    combination may bring a `code_agent` tool back into the schema."""
    monkeypatch.setattr(shared, "JESS_ADVANCED", True)
    monkeypatch.setattr(shared, "EXPERT_ENABLED", True)
    monkeypatch.setattr(shared, "CODE_AGENT_ENABLED", True, raising=False)
    assert "code_agent" not in _tool_names(tool_definitions.get_all_tools())
    from orchestrator import tool_handlers  # noqa: F401  (registers every handler)
    from orchestrator.tool_registry import is_registered

    assert not is_registered("code_agent")


# ---------------------------------------------------------------------------
# Optional feature areas: workouts_enabled / meals_enabled (default OFF).
# Source: orchestrator/tool_definitions.py get_all_tools() +
# WORKOUT_TOOL_NAMES / MEAL_TOOL_NAMES.
# ---------------------------------------------------------------------------

WORKOUT_TOOLS = {"generate_workout", "log_set", "workout_status", "modify_workout", "workout_setup"}


def test_workouts_off_hides_all_workout_tools(monkeypatch):
    monkeypatch.setattr(shared, "WORKOUTS_ENABLED", False)
    names = _tool_names(tool_definitions.get_all_tools())
    assert WORKOUT_TOOLS.isdisjoint(names), f"found: {WORKOUT_TOOLS & names}"


def test_workouts_on_exposes_all_workout_tools(monkeypatch):
    monkeypatch.setattr(shared, "WORKOUTS_ENABLED", True)
    names = _tool_names(tool_definitions.get_all_tools())
    assert not (WORKOUT_TOOLS - names), f"missing: {WORKOUT_TOOLS - names}"


def test_meals_off_hides_log_meal(monkeypatch):
    monkeypatch.setattr(shared, "MEALS_ENABLED", False)
    assert "log_meal" not in _tool_names(tool_definitions.get_all_tools())


def test_meals_on_exposes_log_meal(monkeypatch):
    monkeypatch.setattr(shared, "MEALS_ENABLED", True)
    assert "log_meal" in _tool_names(tool_definitions.get_all_tools())


def test_workouts_and_meals_gate_independently(monkeypatch):
    monkeypatch.setattr(shared, "WORKOUTS_ENABLED", True)
    monkeypatch.setattr(shared, "MEALS_ENABLED", False)
    names = _tool_names(tool_definitions.get_all_tools())
    assert names >= WORKOUT_TOOLS, f"workouts missing: {WORKOUT_TOOLS - names}"
    assert "log_meal" not in names


def test_core_selfcare_log_survives_meal_and_workout_gate(monkeypatch):
    """Gating workouts/meals must NOT remove core selfcare_log — meal / movement
    self-care tracking stays available regardless of the optional features."""
    monkeypatch.setattr(shared, "WORKOUTS_ENABLED", False)
    monkeypatch.setattr(shared, "MEALS_ENABLED", False)
    assert "selfcare_log" in _tool_names(tool_definitions.get_all_tools())
