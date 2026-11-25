"""
Tests for orchestrator/presence_tracker.py::get_presence_prompt_context.

Covers all 6 branches of the function; the user name in every one comes
from shared.profile.user_name (it was hardcoded once).
"""

from datetime import datetime, timedelta

import pytest

from orchestrator import presence_tracker, shared


@pytest.fixture
def reset_state(monkeypatch):
    """
    Snapshot _state, install a fake profile with user_name='TestUser', and
    enable presence. Reverts everything on teardown so tests don't leak
    into each other (or into other test files).
    """
    original_state = presence_tracker._state
    presence_tracker._state = presence_tracker.PresenceState()

    monkeypatch.setattr(shared, "PRESENCE_ENABLED", True, raising=False)

    class _FakeProfile:
        user_name = "TestUser"

    monkeypatch.setattr(shared, "profile", _FakeProfile(), raising=False)

    yield presence_tracker._state

    presence_tracker._state = original_state


def test_branch_1_presence_disabled_returns_empty(monkeypatch):
    """PRESENCE_ENABLED=False short-circuits to empty string regardless of state."""
    monkeypatch.setattr(shared, "PRESENCE_ENABLED", False, raising=False)
    assert presence_tracker.get_presence_prompt_context() == ""


@pytest.mark.parametrize(
    "state, expected",
    [
        pytest.param(
            {"is_home": True, "current_room": "office", "last_motion_room": "office", "motion_min_ago": 0},
            "TestUser is home, currently in the office.",
            id="home-current-room",
        ),
        # current_room cleared (e.g. >10 min stale): last motion + minutes ago
        pytest.param(
            {"is_home": True, "current_room": None, "last_motion_room": "kitchen", "motion_min_ago": 15},
            "TestUser is home, last seen in the kitchen 15 minutes ago.",
            id="home-last-motion",
        ),
        pytest.param(
            {"is_home": True, "current_room": None, "last_motion_room": None, "motion_min_ago": None},
            "TestUser is home.",
            id="home-no-room",
        ),
        pytest.param({"is_home": False, "away_min": 42}, "TestUser is away from home (42 minutes).", id="away-minutes"),
        pytest.param({"is_home": False, "away_min": None}, "TestUser is away from home.", id="away"),
    ],
)
def test_prompt_context_branches(reset_state, state, expected):
    """The name comes from shared.profile.user_name (was hardcoded)."""
    now = datetime.now()
    reset_state.is_home = state["is_home"]
    for attr in ("current_room", "last_motion_room"):
        if attr in state:
            setattr(reset_state, attr, state[attr])
    if "motion_min_ago" in state:
        minutes = state["motion_min_ago"]
        reset_state.last_motion_time = None if minutes is None else now - timedelta(minutes=minutes)
    if "away_min" in state:
        minutes = state["away_min"]
        reset_state.away_since = None if minutes is None else now - timedelta(minutes=minutes)
    assert presence_tracker.get_presence_prompt_context() == expected
