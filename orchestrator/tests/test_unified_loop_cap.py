"""
Tests for _cap_tool_result in unified_loop.py.

Covers: pass-through, None handling, non-string stringification, exact-cap
boundary, truncation with correct footer, warning logging, tool-name in log,
and performance on a 1M-char input.
"""

import logging

from orchestrator.unified_loop import MAX_TOOL_RESULT_CHARS, _cap_tool_result

# ---------------------------------------------------------------------------
# 1. Short string passes through unchanged
# ---------------------------------------------------------------------------


def test_short_string_passthrough(caplog):
    with caplog.at_level(logging.WARNING, logger="orchestrator.unified_loop"):
        result = _cap_tool_result("hello", "anything")

    assert result == "hello"
    assert caplog.records == [], "No warning should be logged for a short result"


# ---------------------------------------------------------------------------
# 2. None becomes empty string
# ---------------------------------------------------------------------------


def test_none_becomes_empty_string():
    result = _cap_tool_result(None, "anything")
    assert result == ""


# ---------------------------------------------------------------------------
# 3. Non-string is stringified
# ---------------------------------------------------------------------------


def test_integer_is_stringified():
    result = _cap_tool_result(42, "anything")
    assert result == "42"


def test_dict_is_stringified():
    result = _cap_tool_result({"ok": True}, "anything")
    # str({"ok": True}) produces the Python repr; just confirm it's a non-empty string
    assert isinstance(result, str)
    assert len(result) > 0


# ---------------------------------------------------------------------------
# 4. String exactly at cap passes through unchanged
# ---------------------------------------------------------------------------


def test_exact_cap_passthrough(caplog):
    at_cap = "x" * MAX_TOOL_RESULT_CHARS
    with caplog.at_level(logging.WARNING, logger="orchestrator.unified_loop"):
        result = _cap_tool_result(at_cap, "anything")

    assert result == at_cap
    assert len(result) == MAX_TOOL_RESULT_CHARS
    assert caplog.records == [], "No warning should be logged at exactly the cap"


# ---------------------------------------------------------------------------
# 5. String over cap is truncated and footer appended correctly
# ---------------------------------------------------------------------------


def test_over_cap_truncated_content():
    """The first MAX_TOOL_RESULT_CHARS chars of input appear unchanged at the start."""
    payload = "A" * 20_000
    result = _cap_tool_result(payload, "any_tool")

    assert result.startswith("A" * MAX_TOOL_RESULT_CHARS)


def test_over_cap_footer_contains_overflow_count():
    """Footer encodes the correct dropped-char count (20000 - 8000 = 12000)."""
    payload = "B" * 20_000
    result = _cap_tool_result(payload, "any_tool")

    overflow = 20_000 - MAX_TOOL_RESULT_CHARS  # 12000
    assert f"[... {overflow} chars truncated" in result


def test_over_cap_total_length():
    """Returned length is 8000 + len(footer), not simply 8000."""
    payload = "C" * 20_000
    result = _cap_tool_result(payload, "any_tool")

    # The result must be longer than MAX_TOOL_RESULT_CHARS because the footer is appended
    assert len(result) > MAX_TOOL_RESULT_CHARS

    # But it must also be much shorter than the raw input
    assert len(result) < len(payload)


def test_over_cap_footer_exact_prefix():
    """The separator and footer bracket appear immediately after the truncated body."""
    payload = "D" * 20_000
    result = _cap_tool_result(payload, "any_tool")

    # The helper inserts '\n\n' then the bracket
    truncated_body = "D" * MAX_TOOL_RESULT_CHARS
    expected_prefix = truncated_body + "\n\n["
    assert result.startswith(expected_prefix)


# ---------------------------------------------------------------------------
# 6. Warning is logged exactly once on truncation
# ---------------------------------------------------------------------------


def test_truncation_logs_exactly_one_warning(caplog):
    payload = "E" * 20_000
    with caplog.at_level(logging.WARNING, logger="orchestrator.unified_loop"):
        _cap_tool_result(payload, "any_tool")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_truncation_log_contains_truncated_keyword(caplog):
    payload = "F" * 20_000
    with caplog.at_level(logging.WARNING, logger="orchestrator.unified_loop"):
        _cap_tool_result(payload, "any_tool")

    assert any("Truncated" in r.getMessage() for r in caplog.records)


def test_truncation_log_contains_char_counts(caplog):
    """Log message must reference both original length (20000) and the cap (8000)."""
    payload = "G" * 20_000
    with caplog.at_level(logging.WARNING, logger="orchestrator.unified_loop"):
        _cap_tool_result(payload, "any_tool")

    msg = caplog.records[0].getMessage()
    assert "20000" in msg
    assert str(MAX_TOOL_RESULT_CHARS) in msg


# ---------------------------------------------------------------------------
# 7. Tool name appears in the log message
# ---------------------------------------------------------------------------


def test_tool_name_in_log_message(caplog):
    payload = "H" * 20_000
    tool = "bloated_test_tool"
    with caplog.at_level(logging.WARNING, logger="orchestrator.unified_loop"):
        _cap_tool_result(payload, tool)

    assert any(tool in r.getMessage() for r in caplog.records)


def test_no_warning_for_short_string_with_named_tool(caplog):
    with caplog.at_level(logging.WARNING, logger="orchestrator.unified_loop"):
        _cap_tool_result("short", "bloated_test_tool")

    assert caplog.records == []


# ---------------------------------------------------------------------------
# 8. Extremely long input completes fast and output is bounded
# ---------------------------------------------------------------------------


def test_large_input_bounded_output():
    """1_000_000-char input must return a result well under ~9000 chars."""
    payload = "Z" * 1_000_000
    result = _cap_tool_result(payload, "stress_tool")

    # Output must be bounded — not approaching input length
    assert len(result) < 9_000


def test_large_input_starts_with_correct_content():
    """First MAX_TOOL_RESULT_CHARS chars of a 1M-char input are preserved."""
    payload = "Y" * 1_000_000
    result = _cap_tool_result(payload, "stress_tool")

    assert result.startswith("Y" * MAX_TOOL_RESULT_CHARS)
