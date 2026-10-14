"""Voice turns (dashboard mic, HA Assist) get a reduced tool set for latency.

Regression: the pantry tools were missing from that set, so a spoken "what can
I cook with the ground turkey?" got "I don't have access to your pantry" while
the same typed question worked. They must be offered on voice whenever the
pantry is enabled — and never when it is off.
"""

import pytest

from orchestrator import shared
from orchestrator import tool_definitions as td

_FAKE_HA_TOOL = {
    "type": "function",
    "function": {"name": "home_assistant", "description": "x", "parameters": {"type": "object", "properties": {}}},
}


@pytest.fixture
def fresh_cache(monkeypatch):
    # Patch the key AND both caches: monkeypatch restores all three together
    # at teardown, so lists built from the fake HA tool never leak into later
    # tests (restoring only the key would leave them behind with a matching key).
    monkeypatch.setattr(td, "get_ha_tool_definition", lambda: _FAKE_HA_TOOL)
    monkeypatch.setattr(td, "_tools_cache_key", ())
    monkeypatch.setattr(td, "_all_tools_cache", [])
    monkeypatch.setattr(td, "_voice_tools_cache", [])


def _names(tools):
    return {t["function"]["name"] for t in tools}


def test_pantry_tools_offered_on_voice_when_enabled(fresh_cache, monkeypatch):
    monkeypatch.setattr(shared, "PANTRY_ENABLED", True)
    voice = _names(td.get_voice_tools())
    assert {"pantry", "meal_plan"} <= voice
    # Voice stays a subset of the full tool list.
    assert voice <= _names(td.get_all_tools())


def test_pantry_tools_hidden_on_voice_when_disabled(fresh_cache, monkeypatch):
    monkeypatch.setattr(shared, "PANTRY_ENABLED", False)
    voice = _names(td.get_voice_tools())
    assert "pantry" not in voice and "meal_plan" not in voice


def test_voice_tool_names_all_exist():
    # A typo in VOICE_TOOL_NAMES would silently drop a tool from voice.
    known = {t["function"]["name"] for t in td.STATIC_TOOLS} | {"home_assistant"}
    assert known >= td.VOICE_TOOL_NAMES
