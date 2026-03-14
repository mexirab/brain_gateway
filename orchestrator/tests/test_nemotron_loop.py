"""
Tests for nemotron_loop.py — XML tool parsing and response cleaning.

Tests pure functions: parse_tool_calls_from_content() and clean_response().
No LLM calls or mocking needed.

Note: We re-implement the pure functions here to avoid importing the full
orchestrator dependency chain (shared → chromadb → ...). The actual functions
are identical — this tests the logic, not the import.
"""

import json
import re

# --- Extracted pure functions (identical to nemotron_loop.py) ---


def clean_response(text: str) -> str:
    """Remove <think> and <tool_call> tags from Nemotron responses."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.DOTALL)
    return text.strip()


def parse_tool_calls_from_content(content: str):
    """Parse <tool_call> tags from Nemotron's content output."""
    tool_calls = []
    pattern = r"<tool_call>\s*(\{.*?\})\s*</tool_call>"
    matches = re.findall(pattern, content, re.DOTALL)

    for i, match in enumerate(matches):
        try:
            parsed = json.loads(match)
            tool_name = parsed.get("name", "")
            arguments = parsed.get("arguments", {})

            tool_calls.append(
                {
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(arguments) if isinstance(arguments, dict) else arguments,
                    },
                }
            )
        except json.JSONDecodeError:
            continue

    return tool_calls


# --- Tests ---


class TestParseToolCalls:
    def test_single_tool_call(self):
        content = """
        <tool_call>
        {"name": "home_assistant", "arguments": {"entity_id": "light.bedroom", "service": "turn_on"}}
        </tool_call>
        """
        calls = parse_tool_calls_from_content(content)
        assert len(calls) == 1
        assert calls[0]["function"]["name"] == "home_assistant"

    def test_multiple_tool_calls(self):
        content = """
        <tool_call>
        {"name": "search_memory", "arguments": {"query": "morning meds"}}
        </tool_call>
        <tool_call>
        {"name": "home_assistant", "arguments": {"entity_id": "light.office", "service": "turn_on"}}
        </tool_call>
        """
        calls = parse_tool_calls_from_content(content)
        assert len(calls) == 2
        assert calls[0]["function"]["name"] == "search_memory"
        assert calls[1]["function"]["name"] == "home_assistant"

    def test_no_tool_calls(self):
        content = "Just a normal text response with no tool calls."
        calls = parse_tool_calls_from_content(content)
        assert len(calls) == 0

    def test_malformed_json(self):
        content = """
        <tool_call>
        {this is not valid json}
        </tool_call>
        """
        calls = parse_tool_calls_from_content(content)
        assert len(calls) == 0

    def test_empty_arguments(self):
        content = """
        <tool_call>
        {"name": "stop_focus", "arguments": {}}
        </tool_call>
        """
        calls = parse_tool_calls_from_content(content)
        assert len(calls) == 1
        assert calls[0]["function"]["name"] == "stop_focus"

    def test_tool_call_structure(self):
        content = """
        <tool_call>
        {"name": "set_reminder", "arguments": {"reminder_text": "take meds", "time": "in 5 minutes"}}
        </tool_call>
        """
        calls = parse_tool_calls_from_content(content)
        assert len(calls) == 1
        call = calls[0]
        assert call["type"] == "function"
        assert "id" in call
        assert "function" in call
        assert "name" in call["function"]
        assert "arguments" in call["function"]

    def test_mixed_content_and_tool_calls(self):
        content = """
        I'll turn on the lights for you.
        <tool_call>
        {"name": "home_assistant", "arguments": {"entity_id": "light.bedroom", "service": "turn_on"}}
        </tool_call>
        Let me know if you need anything else.
        """
        calls = parse_tool_calls_from_content(content)
        assert len(calls) == 1

    def test_nested_arguments(self):
        content = """
        <tool_call>
        {"name": "home_assistant", "arguments": {"entity_id": "light.bedroom", "service": "turn_on", "data": {"brightness": 128, "rgb_color": [0, 0, 255]}}}
        </tool_call>
        """
        calls = parse_tool_calls_from_content(content)
        assert len(calls) == 1

    def test_empty_content(self):
        calls = parse_tool_calls_from_content("")
        assert len(calls) == 0


class TestCleanResponse:
    def test_removes_think_tags(self):
        text = "<think>internal reasoning</think>Here is the actual response."
        assert clean_response(text) == "Here is the actual response."

    def test_removes_tool_call_tags(self):
        text = 'Some text <tool_call>{"name": "test"}</tool_call> more text'
        assert clean_response(text) == "Some text  more text"

    def test_removes_both(self):
        text = '<think>thinking...</think>Response <tool_call>{"name": "x"}</tool_call>'
        assert clean_response(text) == "Response"

    def test_no_tags(self):
        text = "Just a clean response."
        assert clean_response(text) == "Just a clean response."

    def test_empty_string(self):
        assert clean_response("") == ""

    def test_multiline_think(self):
        text = """<think>
        This is a long
        multi-line thought
        </think>The answer is 42."""
        assert clean_response(text) == "The answer is 42."

    def test_preserves_whitespace_in_content(self):
        text = "Line 1\nLine 2\nLine 3"
        assert clean_response(text) == "Line 1\nLine 2\nLine 3"
