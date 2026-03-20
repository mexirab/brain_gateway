"""
Tests for unified_loop.py — clean_response, parse_xml_tool_calls,
and the run_unified_tool_loop agentic loop.

Tests pure functions directly. For run_unified_tool_loop, we mock
call_model and execute_tool to avoid the full orchestrator import chain.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import check helper
# ---------------------------------------------------------------------------


def _can_import_unified_loop():
    """Check if unified_loop can be imported (requires metrics + shared)."""
    try:
        import unified_loop  # noqa: F401

        return True
    except (ImportError, ModuleNotFoundError):
        return False


_skip_no_deps = pytest.mark.skipif(
    not _can_import_unified_loop(),
    reason="unified_loop requires metrics/shared and full orchestrator dependencies",
)


# ---------------------------------------------------------------------------
# Tests: clean_response
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestCleanResponse:
    def test_strips_think_tags(self):
        from unified_loop import clean_response

        text = "<think>internal reasoning here</think>Hello, how can I help?"
        assert clean_response(text) == "Hello, how can I help?"

    def test_strips_tool_call_tags(self):
        from unified_loop import clean_response

        text = '<tool_call>{"name": "search", "arguments": {}}</tool_call>Here is the result.'
        assert clean_response(text) == "Here is the result."

    def test_strips_both_tags(self):
        from unified_loop import clean_response

        text = "<think>thinking...</think>Some text<tool_call>{}</tool_call> and more."
        assert clean_response(text) == "Some text and more."

    def test_multiline_think_tag(self):
        from unified_loop import clean_response

        text = "<think>\nline 1\nline 2\n</think>\nActual response"
        assert clean_response(text) == "Actual response"

    def test_no_tags_passthrough(self):
        from unified_loop import clean_response

        text = "Just a normal response with no tags."
        assert clean_response(text) == "Just a normal response with no tags."

    def test_empty_string(self):
        from unified_loop import clean_response

        assert clean_response("") == ""

    def test_whitespace_stripped(self):
        from unified_loop import clean_response

        assert clean_response("  hello  ") == "hello"


# ---------------------------------------------------------------------------
# Tests: parse_xml_tool_calls
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestParseXmlToolCalls:
    def test_single_valid_tool_call(self):
        from unified_loop import parse_xml_tool_calls

        content = '<tool_call>{"name": "web_search", "arguments": {"query": "weather"}}</tool_call>'
        result = parse_xml_tool_calls(content)
        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "web_search"
        assert result[0]["id"] == "call_0"
        parsed_args = json.loads(result[0]["function"]["arguments"])
        assert parsed_args == {"query": "weather"}

    def test_multiple_tool_calls(self):
        from unified_loop import parse_xml_tool_calls

        content = (
            '<tool_call>{"name": "search_memory", "arguments": {"query": "meds"}}</tool_call>'
            '<tool_call>{"name": "check_calendar", "arguments": {}}</tool_call>'
        )
        result = parse_xml_tool_calls(content)
        assert len(result) == 2
        assert result[0]["id"] == "call_0"
        assert result[1]["id"] == "call_1"
        assert result[0]["function"]["name"] == "search_memory"
        assert result[1]["function"]["name"] == "check_calendar"

    def test_malformed_json_skipped(self):
        from unified_loop import parse_xml_tool_calls

        content = "<tool_call>{not valid json}</tool_call>"
        result = parse_xml_tool_calls(content)
        assert result == []

    def test_empty_content(self):
        from unified_loop import parse_xml_tool_calls

        assert parse_xml_tool_calls("") == []

    def test_no_tool_call_tags(self):
        from unified_loop import parse_xml_tool_calls

        assert parse_xml_tool_calls("Just a normal response.") == []

    def test_arguments_as_string_passthrough(self):
        from unified_loop import parse_xml_tool_calls

        content = '<tool_call>{"name": "test", "arguments": "raw_string_args"}</tool_call>'
        result = parse_xml_tool_calls(content)
        assert len(result) == 1
        assert result[0]["function"]["arguments"] == "raw_string_args"

    def test_arguments_as_dict_serialized(self):
        from unified_loop import parse_xml_tool_calls

        content = '<tool_call>{"name": "test", "arguments": {"key": "val"}}</tool_call>'
        result = parse_xml_tool_calls(content)
        assert len(result) == 1
        parsed = json.loads(result[0]["function"]["arguments"])
        assert parsed == {"key": "val"}

    def test_missing_name_field(self):
        from unified_loop import parse_xml_tool_calls

        content = '<tool_call>{"arguments": {"q": "test"}}</tool_call>'
        result = parse_xml_tool_calls(content)
        assert len(result) == 1
        assert result[0]["function"]["name"] == ""

    def test_mixed_valid_and_invalid(self):
        from unified_loop import parse_xml_tool_calls

        content = (
            '<tool_call>{"name": "good_tool", "arguments": {}}</tool_call>'
            "<tool_call>broken json here</tool_call>"
            '<tool_call>{"name": "another_good", "arguments": {}}</tool_call>'
        )
        result = parse_xml_tool_calls(content)
        assert len(result) == 2
        assert result[0]["function"]["name"] == "good_tool"
        assert result[1]["function"]["name"] == "another_good"
        # IDs should be 0 and 1 (invalid entry skipped, counter still increments via enumerate)
        assert result[0]["id"] == "call_0"
        assert result[1]["id"] == "call_2"


# ---------------------------------------------------------------------------
# Tests: run_unified_tool_loop
# ---------------------------------------------------------------------------

# Helpers
SAMPLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "Search memory",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "home_assistant",
            "description": "Control smart home",
            "parameters": {"type": "object", "properties": {"entity_id": {"type": "string"}}},
        },
    },
]


def _make_llm_response(content="", tool_calls=None):
    """Build a minimal OpenAI-compatible chat completion response."""
    message = {"content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}]}


def _make_tool_call(name, arguments, call_id="call_0"):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments),
        },
    }


@_skip_no_deps
class TestRunUnifiedToolLoop:
    """Test run_unified_tool_loop with mocked call_model and execute_tool."""

    @pytest.mark.asyncio
    async def test_direct_response_no_tools(self):
        """Model returns text with no tool calls — returned directly."""
        mock_call_model = AsyncMock(return_value=_make_llm_response("The weather is sunny today."))

        with (
            patch("orchestrator.call_model", mock_call_model),
            patch("tool_handlers.execute_tool", new_callable=AsyncMock),
        ):
            from unified_loop import run_unified_tool_loop

            result = await run_unified_tool_loop(
                messages=[{"role": "user", "content": "What's the weather?"}],
                system_prompt="You are helpful.",
                tools=SAMPLE_TOOLS,
                model_url="http://fake:8080/v1",
                model_name="test-model",
                http_client=None,
                max_rounds=5,
            )

        assert result == "The weather is sunny today."
        mock_call_model.assert_called_once()

    @pytest.mark.asyncio
    async def test_single_tool_call_and_response(self):
        """Model calls one tool, gets result, then returns text."""
        tool_call = _make_tool_call("web_search", {"query": "Austin weather"}, "call_1")

        # Round 1: model requests tool call
        resp_with_tool = _make_llm_response(content="", tool_calls=[tool_call])
        # Round 2: model returns final text after receiving tool result
        resp_final = _make_llm_response("It's 75F and sunny in Austin.")

        mock_call_model = AsyncMock(side_effect=[resp_with_tool, resp_final])
        mock_execute_tool = AsyncMock(return_value="Temperature: 75F, Condition: Sunny")

        with (
            patch("orchestrator.call_model", mock_call_model),
            patch("tool_handlers.execute_tool", mock_execute_tool),
        ):
            from unified_loop import run_unified_tool_loop

            result = await run_unified_tool_loop(
                messages=[{"role": "user", "content": "What's the weather in Austin?"}],
                system_prompt="You are helpful.",
                tools=SAMPLE_TOOLS,
                model_url="http://fake:8080/v1",
                model_name="test-model",
                http_client=None,
                max_rounds=5,
            )

        assert "75F" in result or "sunny" in result.lower()
        assert mock_call_model.call_count == 2
        mock_execute_tool.assert_called_once_with("web_search", {"query": "Austin weather"})

    @pytest.mark.asyncio
    async def test_terminal_tool_returns_with_summary(self):
        """Terminal tools (e.g. home_assistant) execute, then model summarizes."""
        tool_call = _make_tool_call("home_assistant", {"entity_id": "light.office"}, "call_1")
        resp_with_tool = _make_llm_response(content="", tool_calls=[tool_call])
        resp_summary = _make_llm_response("Done! I've turned on the office light.")

        mock_call_model = AsyncMock(side_effect=[resp_with_tool, resp_summary])
        mock_execute_tool = AsyncMock(return_value="Turned on light.office")

        with (
            patch("orchestrator.call_model", mock_call_model),
            patch("tool_handlers.execute_tool", mock_execute_tool),
        ):
            from unified_loop import run_unified_tool_loop

            result = await run_unified_tool_loop(
                messages=[{"role": "user", "content": "Turn on office light"}],
                system_prompt="You are helpful.",
                tools=SAMPLE_TOOLS,
                model_url="http://fake:8080/v1",
                model_name="test-model",
                http_client=None,
                max_rounds=5,
            )

        assert "office light" in result.lower()
        mock_execute_tool.assert_called_once_with("home_assistant", {"entity_id": "light.office"})
        # Two LLM calls: one to get tool call, one to summarize
        assert mock_call_model.call_count == 2

    @pytest.mark.asyncio
    async def test_duplicate_call_dedup(self):
        """Repeated tool call is skipped and model is forced to summarize."""
        tool_call = _make_tool_call("web_search", {"query": "test"}, "call_1")

        # Round 1: tool call
        resp_round1 = _make_llm_response(content="Let me search.", tool_calls=[tool_call])
        # Round 2: same tool call again (duplicate)
        resp_round2 = _make_llm_response(content="Searching again.", tool_calls=[tool_call])
        # Round 3: forced summary response (no tools)
        resp_final = _make_llm_response("Here is a summary of the results.")

        mock_call_model = AsyncMock(side_effect=[resp_round1, resp_round2, resp_final])
        mock_execute_tool = AsyncMock(return_value="Search result: found it")

        with (
            patch("orchestrator.call_model", mock_call_model),
            patch("tool_handlers.execute_tool", mock_execute_tool),
        ):
            from unified_loop import run_unified_tool_loop

            result = await run_unified_tool_loop(
                messages=[{"role": "user", "content": "Search for test"}],
                system_prompt="You are helpful.",
                tools=SAMPLE_TOOLS,
                model_url="http://fake:8080/v1",
                model_name="test-model",
                http_client=None,
                max_rounds=5,
            )

        assert "summary" in result.lower()
        # execute_tool called once (second call was deduped)
        mock_execute_tool.assert_called_once()
        # 3 LLM calls: round1, round2 (detected dup), forced summary
        assert mock_call_model.call_count == 3

    @pytest.mark.asyncio
    async def test_unknown_tool_rejected(self):
        """Tool not in allowlist gets rejected with error message."""
        tool_call = _make_tool_call("evil_tool", {"payload": "hack"}, "call_1")

        # Round 1: model tries to call unknown tool
        resp_round1 = _make_llm_response(content="", tool_calls=[tool_call])
        # Round 2: model sees error and responds with text
        resp_final = _make_llm_response("I don't have that capability.")

        mock_call_model = AsyncMock(side_effect=[resp_round1, resp_final])
        mock_execute_tool = AsyncMock()

        with (
            patch("orchestrator.call_model", mock_call_model),
            patch("tool_handlers.execute_tool", mock_execute_tool),
        ):
            from unified_loop import run_unified_tool_loop

            await run_unified_tool_loop(
                messages=[{"role": "user", "content": "Do something bad"}],
                system_prompt="You are helpful.",
                tools=SAMPLE_TOOLS,
                model_url="http://fake:8080/v1",
                model_name="test-model",
                http_client=None,
                max_rounds=5,
            )

        # execute_tool should NOT have been called for the unknown tool
        mock_execute_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_max_rounds_hit(self):
        """Model keeps calling tools until max_rounds is exhausted."""
        # Every round returns a tool call with different args to avoid dedup
        responses = []
        for i in range(3):
            tc = _make_tool_call("web_search", {"query": f"round_{i}"}, f"call_{i}")
            responses.append(_make_llm_response(content="", tool_calls=[tc]))

        mock_call_model = AsyncMock(side_effect=responses)
        mock_execute_tool = AsyncMock(return_value="result")

        with (
            patch("orchestrator.call_model", mock_call_model),
            patch("tool_handlers.execute_tool", mock_execute_tool),
        ):
            from unified_loop import run_unified_tool_loop

            result = await run_unified_tool_loop(
                messages=[{"role": "user", "content": "Keep searching"}],
                system_prompt="You are helpful.",
                tools=SAMPLE_TOOLS,
                model_url="http://fake:8080/v1",
                model_name="test-model",
                http_client=None,
                max_rounds=3,
            )

        assert "complexity" in result.lower() or "simpler" in result.lower()
        assert mock_call_model.call_count == 3
        assert mock_execute_tool.call_count == 3

    @pytest.mark.asyncio
    async def test_xml_fallback(self):
        """Model returns <tool_call> XML in content instead of native tool_calls."""
        xml_content = '<tool_call>{"name": "search_memory", "arguments": {"query": "meds"}}</tool_call>'

        # Round 1: XML tool call in content (no native tool_calls)
        resp_round1 = _make_llm_response(content=xml_content, tool_calls=None)
        # Round 2: model returns final text (XML results added as user message)
        resp_final = _make_llm_response("Your medication is Adderall 20mg.")

        mock_call_model = AsyncMock(side_effect=[resp_round1, resp_final])
        mock_execute_tool = AsyncMock(return_value="Medication: Adderall 20mg daily")

        with (
            patch("orchestrator.call_model", mock_call_model),
            patch("tool_handlers.execute_tool", mock_execute_tool),
        ):
            from unified_loop import run_unified_tool_loop

            result = await run_unified_tool_loop(
                messages=[{"role": "user", "content": "What meds am I on?"}],
                system_prompt="You are helpful.",
                tools=SAMPLE_TOOLS,
                model_url="http://fake:8080/v1",
                model_name="test-model",
                http_client=None,
                max_rounds=5,
            )

        assert "Adderall" in result
        mock_execute_tool.assert_called_once_with("search_memory", {"query": "meds"})

    @pytest.mark.asyncio
    async def test_llm_call_failure(self):
        """call_model raises exception — returns error message."""
        mock_call_model = AsyncMock(side_effect=Exception("Connection refused"))

        with (
            patch("orchestrator.call_model", mock_call_model),
            patch("tool_handlers.execute_tool", new_callable=AsyncMock),
        ):
            from unified_loop import run_unified_tool_loop

            result = await run_unified_tool_loop(
                messages=[{"role": "user", "content": "Hello"}],
                system_prompt="You are helpful.",
                tools=SAMPLE_TOOLS,
                model_url="http://fake:8080/v1",
                model_name="test-model",
                http_client=None,
                max_rounds=5,
            )

        assert "sorry" in result.lower() or "couldn't" in result.lower()

    @pytest.mark.asyncio
    async def test_tool_execution_failure(self):
        """execute_tool raises exception — error message fed back to model."""
        tool_call = _make_tool_call("web_search", {"query": "fail"}, "call_1")

        resp_with_tool = _make_llm_response(content="", tool_calls=[tool_call])
        resp_final = _make_llm_response("Sorry, the search failed. Please try again.")

        mock_call_model = AsyncMock(side_effect=[resp_with_tool, resp_final])
        mock_execute_tool = AsyncMock(side_effect=RuntimeError("API timeout"))

        with (
            patch("orchestrator.call_model", mock_call_model),
            patch("tool_handlers.execute_tool", mock_execute_tool),
        ):
            from unified_loop import run_unified_tool_loop

            result = await run_unified_tool_loop(
                messages=[{"role": "user", "content": "Search something"}],
                system_prompt="You are helpful.",
                tools=SAMPLE_TOOLS,
                model_url="http://fake:8080/v1",
                model_name="test-model",
                http_client=None,
                max_rounds=5,
            )

        # Model should still produce a response (error was caught and fed back)
        assert result is not None
        assert len(result) > 0
        mock_execute_tool.assert_called_once()
