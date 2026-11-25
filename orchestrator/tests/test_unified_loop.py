"""
Tests for unified_loop.py — clean_response, parse_xml_tool_calls,
and the run_unified_tool_loop agentic loop.

Pure functions are tested directly. For run_unified_tool_loop, call_model and
execute_tool are mocked (fixtures_loop.run_loop).
"""

import json
from unittest.mock import AsyncMock

import pytest

from orchestrator.tests.fixtures_loop import reply, run_loop, tool_call
from orchestrator.tests.fixtures_loop import tools_named as _tools
from orchestrator.unified_loop import clean_response, parse_xml_tool_calls

# ---------------------------------------------------------------------------
# clean_response
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        pytest.param(
            "<think>internal reasoning here</think>Hello, how can I help?", "Hello, how can I help?", id="think"
        ),
        pytest.param(
            '<tool_call>{"name": "search", "arguments": {}}</tool_call>Here is the result.',
            "Here is the result.",
            id="tool-call",
        ),
        pytest.param(
            "<think>thinking...</think>Some text<tool_call>{}</tool_call> and more.", "Some text and more.", id="both"
        ),
        pytest.param("<think>\nline 1\nline 2\n</think>\nActual response", "Actual response", id="multiline-think"),
        pytest.param("a <think>line1\nline2\nline3</think> b", "a  b", id="multiline-inline"),
        pytest.param("Just a normal response with no tags.", "Just a normal response with no tags.", id="no-tags"),
        pytest.param("", "", id="empty"),
        pytest.param("  hello  ", "hello", id="outer-whitespace"),
        # Attributed sentinels used to leak (fix/streamgate-attributed-tags).
        pytest.param("keep <think reason=1>hidden</think> end", "keep  end", id="attributed-think"),
        pytest.param('a <tool_call id="a">{}</tool_call> b', "a  b", id="attributed-tool-call"),
        pytest.param("a <think>x</think> b", "a  b", id="plain-think"),
        pytest.param("a <think>x</think > b", "a  b", id="space-in-closer"),
        # Lookalikes stay (word boundary), and uppercase is left alone on purpose.
        pytest.param("<thinker>keep</thinker>", "<thinker>keep</thinker>", id="thinker-lookalike"),
        pytest.param("<tool_calls>keep</tool_calls>", "<tool_calls>keep</tool_calls>", id="tool-calls-lookalike"),
        pytest.param("<THINK>x</THINK>", "<THINK>x</THINK>", id="uppercase-kept"),
    ],
)
def test_clean_response(text, expected):
    assert clean_response(text) == expected


# ---------------------------------------------------------------------------
# parse_xml_tool_calls
# ---------------------------------------------------------------------------


class TestParseXmlToolCalls:
    def test_single_valid_tool_call(self):
        content = '<tool_call>{"name": "web_search", "arguments": {"query": "weather"}}</tool_call>'
        result = parse_xml_tool_calls(content)
        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "web_search"
        assert result[0]["id"] == "call_0"
        assert json.loads(result[0]["function"]["arguments"]) == {"query": "weather"}

    def test_multiple_tool_calls(self):
        content = (
            '<tool_call>{"name": "search_memory", "arguments": {"query": "meds"}}</tool_call>'
            '<tool_call>{"name": "check_calendar", "arguments": {}}</tool_call>'
        )
        result = parse_xml_tool_calls(content)
        assert [r["id"] for r in result] == ["call_0", "call_1"]
        assert [r["function"]["name"] for r in result] == ["search_memory", "check_calendar"]

    @pytest.mark.parametrize("content", ["<tool_call>{not valid json}</tool_call>", "", "Just a normal response."])
    def test_nothing_to_parse(self, content):
        assert parse_xml_tool_calls(content) == []

    def test_arguments_as_string_passthrough(self):
        result = parse_xml_tool_calls('<tool_call>{"name": "test", "arguments": "raw_string_args"}</tool_call>')
        assert len(result) == 1
        assert result[0]["function"]["arguments"] == "raw_string_args"

    def test_arguments_as_dict_serialized(self):
        result = parse_xml_tool_calls('<tool_call>{"name": "test", "arguments": {"key": "val"}}</tool_call>')
        assert len(result) == 1
        assert json.loads(result[0]["function"]["arguments"]) == {"key": "val"}

    def test_missing_name_field(self):
        result = parse_xml_tool_calls('<tool_call>{"arguments": {"q": "test"}}</tool_call>')
        assert len(result) == 1
        assert result[0]["function"]["name"] == ""

    def test_mixed_valid_and_invalid(self):
        content = (
            '<tool_call>{"name": "good_tool", "arguments": {}}</tool_call>'
            "<tool_call>broken json here</tool_call>"
            '<tool_call>{"name": "another_good", "arguments": {}}</tool_call>'
        )
        result = parse_xml_tool_calls(content)
        assert [r["function"]["name"] for r in result] == ["good_tool", "another_good"]
        # The invalid entry is skipped but still uses up its index.
        assert [r["id"] for r in result] == ["call_0", "call_2"]


# ---------------------------------------------------------------------------
# run_unified_tool_loop
# ---------------------------------------------------------------------------

TOOLS = _tools("web_search", "search_memory", "home_assistant")


def _user(text):
    return [{"role": "user", "content": text}]


async def test_direct_response_no_tools():
    call_model = AsyncMock(return_value=reply("The weather is sunny today."))
    result = await run_loop(call_model, AsyncMock(), messages=_user("What's the weather?"), tools=TOOLS)
    assert result == "The weather is sunny today."
    call_model.assert_called_once()


async def test_single_tool_call_and_response():
    call_model = AsyncMock(
        side_effect=[
            reply(tool_calls=[tool_call("web_search", {"query": "Austin weather"}, "call_1")]),
            reply("It's 75F and sunny in Austin."),
        ]
    )
    execute_tool = AsyncMock(return_value="Temperature: 75F, Condition: Sunny")
    result = await run_loop(call_model, execute_tool, messages=_user("What's the weather in Austin?"), tools=TOOLS)
    assert result == "It's 75F and sunny in Austin."
    assert call_model.call_count == 2
    execute_tool.assert_called_once_with("web_search", {"query": "Austin weather"})


async def test_terminal_tool_returns_with_summary():
    """Terminal tools (e.g. home_assistant) execute, then the model summarizes."""
    call_model = AsyncMock(
        side_effect=[
            reply(tool_calls=[tool_call("home_assistant", {"entity_id": "light.office"}, "call_1")]),
            reply("Done! I've turned on the office light."),
        ]
    )
    execute_tool = AsyncMock(return_value="Turned on light.office")
    result = await run_loop(call_model, execute_tool, messages=_user("Turn on office light"), tools=TOOLS)
    assert result == "Done! I've turned on the office light."
    execute_tool.assert_called_once_with("home_assistant", {"entity_id": "light.office"})
    assert call_model.call_count == 2  # one to get the call, one to summarize


async def test_duplicate_call_dedup():
    """A call repeated in a later round is skipped and the model is forced to summarize."""
    same = tool_call("web_search", {"query": "test"}, "call_1")
    call_model = AsyncMock(
        side_effect=[
            reply("Let me search.", tool_calls=[same]),
            reply("Searching again.", tool_calls=[same]),
            reply("Here is a summary of the results."),
        ]
    )
    execute_tool = AsyncMock(return_value="Search result: found it")
    result = await run_loop(call_model, execute_tool, messages=_user("Search for test"), tools=TOOLS)
    assert result == "Here is a summary of the results."
    execute_tool.assert_called_once()
    assert call_model.call_count == 3  # round 1, round 2 (duplicate found), forced summary


async def test_unknown_tool_rejected():
    """A tool not in the schema is never executed."""
    call_model = AsyncMock(
        side_effect=[
            reply(tool_calls=[tool_call("evil_tool", {"payload": "hack"}, "call_1")]),
            reply("I don't have that capability."),
        ]
    )
    execute_tool = AsyncMock()
    await run_loop(call_model, execute_tool, messages=_user("Do something bad"), tools=TOOLS)
    execute_tool.assert_not_called()


async def test_max_rounds_hit():
    """Model keeps calling tools (new args each round) until max_rounds runs out."""
    call_model = AsyncMock(
        side_effect=[
            reply(tool_calls=[tool_call("web_search", {"query": f"round_{i}"}, f"call_{i}")]) for i in range(3)
        ]
    )
    execute_tool = AsyncMock(return_value="result")
    result = await run_loop(call_model, execute_tool, messages=_user("Keep searching"), tools=TOOLS, max_rounds=3)
    assert "complexity" in result.lower()
    assert call_model.call_count == 3
    assert execute_tool.call_count == 3


async def test_llm_call_failure():
    call_model = AsyncMock(side_effect=Exception("Connection refused"))
    result = await run_loop(call_model, AsyncMock(), messages=_user("Hello"), tools=TOOLS)
    assert "sorry" in result.lower() or "couldn't" in result.lower()


async def test_tool_execution_failure():
    """execute_tool raises: a generic error goes back to the model, which still answers."""
    call_model = AsyncMock(
        side_effect=[
            reply(tool_calls=[tool_call("web_search", {"query": "fail"}, "call_1")]),
            reply("Sorry, the search failed. Please try again."),
        ]
    )
    execute_tool = AsyncMock(side_effect=RuntimeError("API timeout"))
    messages = _user("Search something")
    result = await run_loop(call_model, execute_tool, messages=messages, tools=TOOLS)
    assert result == "Sorry, the search failed. Please try again."
    execute_tool.assert_called_once()
    (tool_msg,) = [m for m in messages if m.get("role") == "tool"]
    assert tool_msg["content"] == "The web_search tool encountered an error. Please try again."
    assert "API timeout" not in tool_msg["content"]
