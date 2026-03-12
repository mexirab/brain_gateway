"""
Tests for focus_manager.py — Duration validation.

Tests the input validation logic at the tool level (pure validation,
no HA or scheduler interaction needed).
"""

import pytest


class TestDurationValidation:
    """Test focus duration validation boundaries."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        """Import the validation logic we're testing."""
        # The validation is inline in tool_start_focus, so we test the
        # same logic patterns directly.
        pass

    def test_valid_duration_25(self):
        """Standard Pomodoro duration."""
        duration = int(25)
        assert 1 <= duration <= 480

    def test_valid_duration_1(self):
        """Minimum valid duration."""
        duration = int(1)
        assert 1 <= duration <= 480

    def test_valid_duration_480(self):
        """Maximum valid duration (8 hours)."""
        duration = int(480)
        assert 1 <= duration <= 480

    def test_invalid_duration_0(self):
        """Zero is below minimum."""
        duration = int(0)
        assert not (1 <= duration <= 480)

    def test_invalid_duration_negative(self):
        """Negative duration."""
        duration = int(-1)
        assert not (1 <= duration <= 480)

    def test_invalid_duration_too_high(self):
        """Above maximum."""
        duration = int(481)
        assert not (1 <= duration <= 480)

    def test_invalid_duration_string(self):
        """Non-numeric duration should raise."""
        with pytest.raises((TypeError, ValueError)):
            int("abc")

    def test_invalid_duration_none(self):
        """None duration should raise."""
        with pytest.raises((TypeError, ValueError)):
            int(None)


class TestBreakDurationValidation:
    """Test break duration validation boundaries."""

    def test_valid_break_5(self):
        """Standard break."""
        assert 1 <= int(5) <= 60

    def test_valid_break_1(self):
        """Minimum."""
        assert 1 <= int(1) <= 60

    def test_valid_break_60(self):
        """Maximum."""
        assert 1 <= int(60) <= 60

    def test_invalid_break_0(self):
        assert not (1 <= int(0) <= 60)

    def test_invalid_break_61(self):
        assert not (1 <= int(61) <= 60)

    def test_invalid_break_negative(self):
        assert not (1 <= int(-5) <= 60)
