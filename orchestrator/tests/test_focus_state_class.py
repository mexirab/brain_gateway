"""Tests for orchestrator/focus_state.py — FocusSession class."""

import pytest

from orchestrator.focus_state import FocusSession


class TestDictStyleAccess:
    """FocusSession supports dict-style __getitem__, __setitem__, get, update, __contains__."""

    def test_defaults_and_missing_key(self):
        s = FocusSession()
        assert s["active"] is False
        assert s["task"] is None
        assert s["sprint_count"] == 0
        assert "active" in s and "task" in s
        assert "nonexistent" not in s
        with pytest.raises(KeyError):
            _ = s["nonexistent"]
        assert s.get("nonexistent", "fallback") == "fallback"
        assert s.get("missing") is None
        assert s.get("active") is False

    def test_set_and_update(self):
        s = FocusSession()
        s["task"] = "Write tests"
        s["custom_field"] = 42
        assert s["task"] == "Write tests"
        assert s["custom_field"] == 42
        s.update({"active": True, "task": "Deploy", "sprint_count": 3})
        assert (s["active"], s["task"], s["sprint_count"]) == (True, "Deploy", 3)


class TestResetAndCopy:
    def test_reset_restores_defaults_and_is_idempotent(self):
        s = FocusSession()
        s.update({"active": True, "task": "Something", "sprint_count": 5})
        s.reset()
        assert (s["active"], s["task"], s["sprint_count"]) == (False, None, 0)
        s.reset()
        assert s["active"] is False

    def test_to_dict_is_a_copy_with_all_defaults(self):
        s = FocusSession()
        d = s.to_dict()
        assert isinstance(d, dict)
        assert {"active", "task", "sprint_count", "audio_source"} <= set(d)
        assert d["audio_source"] == "endel"
        d["active"] = True
        assert s["active"] is False  # original unchanged


def test_properties_track_the_dict():
    s = FocusSession()
    assert (s.active, s.task, s.started, s.duration) == (False, None, None, None)
    s.update({"active": True, "task": "Focus work", "duration": 25})
    assert (s.active, s.task, s.duration) == (True, "Focus work", 25)


def test_repr_shows_active_state():
    s = FocusSession()
    assert "active=False" in repr(s)
    s["active"] = True
    s["task"] = "coding"
    assert "active=True" in repr(s)
    assert "coding" in repr(s)
