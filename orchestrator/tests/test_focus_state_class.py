"""Tests for orchestrator/focus_state.py — FocusSession class."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from orchestrator.focus_state import FocusSession


class TestDictStyleAccess:
    """FocusSession supports dict-style __getitem__, __setitem__, get, update, __contains__."""

    def test_getitem_default_values(self):
        s = FocusSession()
        assert s["active"] is False
        assert s["task"] is None
        assert s["sprint_count"] == 0

    def test_getitem_missing_key_raises(self):
        s = FocusSession()
        with pytest.raises(KeyError):
            _ = s["nonexistent"]

    def test_setitem(self):
        s = FocusSession()
        s["task"] = "Write tests"
        assert s["task"] == "Write tests"

    def test_setitem_new_key(self):
        s = FocusSession()
        s["custom_field"] = 42
        assert s["custom_field"] == 42

    def test_get_with_default(self):
        s = FocusSession()
        assert s.get("nonexistent", "fallback") == "fallback"

    def test_get_existing_key(self):
        s = FocusSession()
        assert s.get("active") is False

    def test_get_none_default(self):
        s = FocusSession()
        assert s.get("missing") is None

    def test_update(self):
        s = FocusSession()
        s.update({"active": True, "task": "Deploy", "sprint_count": 3})
        assert s["active"] is True
        assert s["task"] == "Deploy"
        assert s["sprint_count"] == 3

    def test_contains_existing(self):
        s = FocusSession()
        assert "active" in s
        assert "task" in s

    def test_contains_missing(self):
        s = FocusSession()
        assert "nonexistent" not in s


class TestReset:
    """reset() restores all values to defaults."""

    def test_reset_clears_modifications(self):
        s = FocusSession()
        s["active"] = True
        s["task"] = "Something"
        s["sprint_count"] = 5
        s.reset()
        assert s["active"] is False
        assert s["task"] is None
        assert s["sprint_count"] == 0

    def test_reset_is_idempotent(self):
        s = FocusSession()
        s.reset()
        s.reset()
        assert s["active"] is False


class TestToDict:
    """to_dict() returns a plain dict copy."""

    def test_returns_dict(self):
        s = FocusSession()
        d = s.to_dict()
        assert isinstance(d, dict)

    def test_returns_copy(self):
        s = FocusSession()
        d = s.to_dict()
        d["active"] = True
        # Original should be unchanged
        assert s["active"] is False

    def test_contains_all_defaults(self):
        s = FocusSession()
        d = s.to_dict()
        assert "active" in d
        assert "task" in d
        assert "sprint_count" in d
        assert "audio_source" in d
        assert d["audio_source"] == "endel"


class TestPropertyAccess:
    """Properties expose typed accessors for common fields."""

    def test_active_property(self):
        s = FocusSession()
        assert s.active is False
        s["active"] = True
        assert s.active is True

    def test_task_property(self):
        s = FocusSession()
        assert s.task is None
        s["task"] = "Focus work"
        assert s.task == "Focus work"

    def test_started_property(self):
        s = FocusSession()
        assert s.started is None

    def test_duration_property(self):
        s = FocusSession()
        assert s.duration is None
        s["duration"] = 25
        assert s.duration == 25


class TestRepr:
    """__repr__ shows active state."""

    def test_repr_inactive(self):
        s = FocusSession()
        assert "active=False" in repr(s)

    def test_repr_active(self):
        s = FocusSession()
        s["active"] = True
        s["task"] = "coding"
        r = repr(s)
        assert "active=True" in r
        assert "coding" in r
