"""
Tests for log_buffer.py — In-memory log ring buffer.

Tests the LogRingBuffer used for Jess self-diagnosis.
"""

import logging
import pytest
from log_buffer import LogRingBuffer


@pytest.fixture
def buffer():
    buf = LogRingBuffer(capacity=10)
    buf.setFormatter(logging.Formatter("%(message)s"))
    return buf


def _emit(buf, msg, level=logging.INFO):
    """Helper to emit a log record into the buffer."""
    record = logging.LogRecord(
        name="test", level=level, pathname="", lineno=0,
        msg=msg, args=(), exc_info=None,
    )
    buf.emit(record)


class TestLogRingBuffer:
    def test_emit_stores_entry(self, buffer):
        _emit(buffer, "Hello world")
        assert len(buffer.buffer) == 1
        assert buffer.buffer[0]["message"] == "Hello world"

    def test_capacity_limit(self):
        buf = LogRingBuffer(capacity=3)
        buf.setFormatter(logging.Formatter("%(message)s"))
        for i in range(5):
            _emit(buf, f"msg {i}")
        assert len(buf.buffer) == 3
        # Oldest messages should be dropped
        assert buf.buffer[0]["message"] == "msg 2"

    def test_search_finds_matching(self, buffer):
        _emit(buffer, "[MORNING_BRIEFING] Delivered at 7:02 AM")
        _emit(buffer, "[CALENDAR_POLL] Checked 3 events")
        _emit(buffer, "[MORNING_BRIEFING] No events found")
        results = buffer.search("[MORNING_BRIEFING]")
        assert len(results) == 2

    def test_search_case_insensitive(self, buffer):
        _emit(buffer, "[MORNING_BRIEFING] Test")
        results = buffer.search("[morning_briefing]")
        assert len(results) == 1

    def test_search_limit(self, buffer):
        for i in range(10):
            _emit(buffer, f"[TEST] Entry {i}")
        results = buffer.search("[TEST]", limit=3)
        assert len(results) == 3

    def test_search_most_recent_first(self, buffer):
        _emit(buffer, "[TAG] First")
        _emit(buffer, "[TAG] Second")
        results = buffer.search("[TAG]")
        assert results[0]["message"] == "[TAG] Second"

    def test_errors_filters_levels(self, buffer):
        _emit(buffer, "Info message", logging.INFO)
        _emit(buffer, "Error message", logging.ERROR)
        _emit(buffer, "Critical message", logging.CRITICAL)
        _emit(buffer, "Warning message", logging.WARNING)
        errors = buffer.errors()
        assert len(errors) == 2

    def test_recent_returns_most_recent(self, buffer):
        for i in range(10):
            _emit(buffer, f"msg {i}")
        recent = buffer.recent(limit=3)
        assert len(recent) == 3
        assert recent[0]["message"] == "msg 9"

    def test_search_no_matches(self, buffer):
        _emit(buffer, "Hello world")
        results = buffer.search("NONEXISTENT")
        assert len(results) == 0

    def test_entry_has_required_fields(self, buffer):
        _emit(buffer, "Test entry")
        entry = buffer.buffer[0]
        assert "time" in entry
        assert "level" in entry
        assert "message" in entry
