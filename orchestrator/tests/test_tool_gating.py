"""
Tests for JESS_ADVANCED tool gating in tool_definitions.get_all_tools().

The gate layers over the existing CODE_AGENT_ENABLED / EXPERT_ENABLED flags:
- code_agent appears only if BOTH JESS_ADVANCED and CODE_AGENT_ENABLED are true
- ask_expert appears only if BOTH JESS_ADVANCED and EXPERT_ENABLED are true
- query_budget, finance_status, check_claude_activity require JESS_ADVANCED only

Source: orchestrator/tool_definitions.py:1110-1138
"""

from orchestrator import shared, tool_definitions


ADVANCED_TOOLS = {
    "code_agent",
    "ask_expert",
    "query_budget",
    "finance_status",
    "check_claude_activity",
}


def _tool_names(tools):
    return {t.get("function", {}).get("name") for t in tools}


def test_advanced_off_all_per_tool_off_hides_all_five(monkeypatch):
    """JESS_ADVANCED=False with per-tool flags off → none of the 5 advanced tools."""
    monkeypatch.setattr(shared, "JESS_ADVANCED", False)
    monkeypatch.setattr(shared, "CODE_AGENT_ENABLED", False)
    monkeypatch.setattr(shared, "EXPERT_ENABLED", False)

    names = _tool_names(tool_definitions.get_all_tools())

    assert ADVANCED_TOOLS.isdisjoint(names), (
        f"Expected none of {ADVANCED_TOOLS} when JESS_ADVANCED=False; "
        f"found: {ADVANCED_TOOLS & names}"
    )


def test_advanced_on_all_per_tool_on_exposes_all_five(monkeypatch):
    """JESS_ADVANCED=True + per-tool flags on → all 5 advanced tools present."""
    monkeypatch.setattr(shared, "JESS_ADVANCED", True)
    monkeypatch.setattr(shared, "CODE_AGENT_ENABLED", True)
    monkeypatch.setattr(shared, "EXPERT_ENABLED", True)

    names = _tool_names(tool_definitions.get_all_tools())

    missing = ADVANCED_TOOLS - names
    assert not missing, f"Expected all advanced tools present; missing: {missing}"


def test_advanced_off_but_code_agent_enabled_still_hides_code_agent(monkeypatch):
    """JESS_ADVANCED=False, CODE_AGENT_ENABLED=True → code_agent still hidden.

    The JESS_ADVANCED gate layers over the per-tool flag — it doesn't replace
    it. Both must be true for code_agent to show up.
    """
    monkeypatch.setattr(shared, "JESS_ADVANCED", False)
    monkeypatch.setattr(shared, "CODE_AGENT_ENABLED", True)
    monkeypatch.setattr(shared, "EXPERT_ENABLED", False)

    names = _tool_names(tool_definitions.get_all_tools())

    assert "code_agent" not in names, (
        "code_agent must remain hidden when JESS_ADVANCED=False even if "
        "CODE_AGENT_ENABLED=True"
    )


def test_advanced_on_but_code_agent_disabled_still_hides_code_agent(monkeypatch):
    """JESS_ADVANCED=True, CODE_AGENT_ENABLED=False → code_agent still hidden.

    Per-tool flag is still required even with JESS_ADVANCED on.
    """
    monkeypatch.setattr(shared, "JESS_ADVANCED", True)
    monkeypatch.setattr(shared, "CODE_AGENT_ENABLED", False)
    monkeypatch.setattr(shared, "EXPERT_ENABLED", False)

    names = _tool_names(tool_definitions.get_all_tools())

    assert "code_agent" not in names, (
        "code_agent must remain hidden when CODE_AGENT_ENABLED=False even if "
        "JESS_ADVANCED=True"
    )


def test_tool_count_delta_is_exactly_five(monkeypatch):
    """All-flags-on minus all-flags-off should expose exactly 5 extra tools."""
    monkeypatch.setattr(shared, "JESS_ADVANCED", False)
    monkeypatch.setattr(shared, "CODE_AGENT_ENABLED", False)
    monkeypatch.setattr(shared, "EXPERT_ENABLED", False)
    off_count = len(tool_definitions.get_all_tools())

    monkeypatch.setattr(shared, "JESS_ADVANCED", True)
    monkeypatch.setattr(shared, "CODE_AGENT_ENABLED", True)
    monkeypatch.setattr(shared, "EXPERT_ENABLED", True)
    on_count = len(tool_definitions.get_all_tools())

    assert on_count - off_count == 5, (
        f"Expected delta of 5 advanced tools; got {on_count} - {off_count} = "
        f"{on_count - off_count}"
    )
