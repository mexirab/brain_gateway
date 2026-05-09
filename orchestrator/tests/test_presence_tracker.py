"""
Tests for orchestrator/presence_tracker.py::get_presence_prompt_context.

Covers all 6 branches of the function and asserts that the recently
parameterized user_name (was hardcoded "Nadim") is correctly pulled
from shared.profile.user_name.
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


def test_branch_2_home_with_current_room(reset_state):
    """is_home + current_room → '{name} is home, currently in the {room}.'"""
    reset_state.is_home = True
    reset_state.current_room = "office"
    reset_state.last_motion_room = "office"
    reset_state.last_motion_time = datetime.now()

    result = presence_tracker.get_presence_prompt_context()
    assert result == "TestUser is home, currently in the office."


def test_branch_3_home_with_last_motion_only(reset_state):
    """is_home + last_motion_room (no current_room) + last_seen_ago_minutes → 'last seen in {room} {N} minutes ago.'"""
    reset_state.is_home = True
    reset_state.current_room = None  # cleared (e.g. >10min stale)
    reset_state.last_motion_room = "kitchen"
    reset_state.last_motion_time = datetime.now() - timedelta(minutes=15)

    result = presence_tracker.get_presence_prompt_context()
    assert result == "TestUser is home, last seen in the kitchen 15 minutes ago."


def test_branch_4_home_with_nothing_else(reset_state):
    """is_home + no room data → '{name} is home.'"""
    reset_state.is_home = True
    reset_state.current_room = None
    reset_state.last_motion_room = None
    reset_state.last_motion_time = None

    result = presence_tracker.get_presence_prompt_context()
    assert result == "TestUser is home."


def test_branch_5_away_with_minutes(reset_state):
    """away + away_minutes → '{name} is away from home (N minutes).'"""
    reset_state.is_home = False
    reset_state.away_since = datetime.now() - timedelta(minutes=42)

    result = presence_tracker.get_presence_prompt_context()
    assert result == "TestUser is away from home (42 minutes)."


def test_branch_6_away_with_nothing_else(reset_state):
    """away + no away_since → '{name} is away from home.'"""
    reset_state.is_home = False
    reset_state.away_since = None

    result = presence_tracker.get_presence_prompt_context()
    assert result == "TestUser is away from home."


def test_parameterization_uses_profile_user_name(reset_state, monkeypatch):
    """Sanity check: changing profile.user_name flows through to the output."""

    class _OtherProfile:
        user_name = "Alice"

    monkeypatch.setattr(shared, "profile", _OtherProfile(), raising=False)

    reset_state.is_home = True
    reset_state.current_room = "den"
    reset_state.last_motion_time = datetime.now()

    result = presence_tracker.get_presence_prompt_context()
    assert result == "Alice is home, currently in the den."
    assert "Nadim" not in result
