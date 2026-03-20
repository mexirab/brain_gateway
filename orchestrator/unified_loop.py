"""
Unified agentic tool loop: native function calling with XML fallback.

Replaces the two-model Helios→Nemotron flow with a single model that handles
both conversation and tool execution via OpenAI-compatible tool_calls.
"""

import json
import logging
import re
import time
from typing import Any, Dict, List

from metrics import (
    LLM_CALL_COUNT,
    LLM_CALL_ERRORS,
    LLM_CALL_LATENCY,
    TOOL_CALL_COUNT,
    TOOL_CALL_ERRORS,
    TOOL_CALL_LATENCY,
    TOOL_ROUNDS,
)
from shared import MAX_TOOL_ROUNDS

logger = logging.getLogger(__name__)

# Tools that mutate state — return result directly, don't loop
TERMINAL_TOOLS = {
    "start_focus",
    "stop_focus",
    "focus_sprint",
    "set_reminder",
    "cancel_reminder",
    "home_assistant",
    "update_data",
    "create_calendar_event",
    "brain_dump",
    "decompose_task",
    "task_step",
    "start_routine",
    "routine_action",
}


def clean_response(text: str) -> str:
    """Remove <think> and <tool_call> tags from model responses."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.DOTALL)
    return text.strip()


def parse_xml_tool_calls(content: str) -> List[Dict[str, Any]]:
    """Parse <tool_call> XML tags from model content (fallback for older models).

    Returns tool_calls in OpenAI-compatible format.
    """
    tool_calls = []
    # Greedy match between tags to handle nested JSON objects like {"arguments": {"key": "val"}}
    pattern = r"<tool_call>\s*(.*?)\s*</tool_call>"
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
        except json.JSONDecodeError as e:
            logger.warning("[UNIFIED] Failed to parse XML tool call: %s", e)
            continue

    return tool_calls


async def run_unified_tool_loop(
    messages: List[Dict],
    system_prompt: str,
    tools: List[Dict],
    model_url: str,
    model_name: str,
    http_client,
    max_rounds: int = MAX_TOOL_ROUNDS,
    label: str = "UNIFIED",
    is_voice: bool = False,
) -> str:
    """
    Unified agentic tool loop with native function calling.

    Uses tool_choice="auto" for native OpenAI-compatible tool calling.
    Falls back to XML <tool_call> parsing if the model doesn't return
    structured tool_calls (backward compat for older models).

    Args:
        messages: Conversation messages.
        system_prompt: System prompt (unified: personality + tool instructions).
        tools: Tool definitions (all tools, not just ask_orchestrator).
        model_url: LLM endpoint URL.
        model_name: Model name/identifier.
        http_client: Not used directly (call_model resolves backend).
        max_rounds: Maximum tool execution rounds.
        label: Log label.

    Returns:
        Final text response from the model.
    """
    # Import here to avoid circular import (tool_handlers imports shared)
    from orchestrator import call_model
    from tool_handlers import execute_tool

    # Voice mode: reduce max_tokens for shorter responses (faster TTS)
    # Keep thinking enabled — disabling it causes the model to skip tool calls
    voice_extra = None
    if is_voice:
        voice_extra = {"max_tokens": 512}
        logger.info("[%s] Voice mode: max_tokens=512", label)

    # Build allowlist of valid tool names from the tools passed in
    valid_tool_names = {t["function"]["name"] for t in tools if "function" in t}

    executed_calls = set()

    for round_num in range(max_rounds):
        logger.info("[%s] Round %d/%d", label, round_num + 1, max_rounds)

        _llm_t0 = time.time()
        try:
            llm_resp = await call_model(
                model_url,
                model_name,
                messages,
                system=system_prompt,
                tools=tools,
                tool_choice="auto",
                timeout=120,
                extra_body=voice_extra,
            )
            LLM_CALL_COUNT.labels(model=model_name, purpose="unified_loop").inc()
            LLM_CALL_LATENCY.labels(model=model_name, purpose="unified_loop").observe(time.time() - _llm_t0)
        except Exception as e:
            LLM_CALL_ERRORS.labels(model=model_name, error_type=type(e).__name__).inc()
            logger.error("[%s] Call failed: %s", label, e)
            return "Sorry, I couldn't complete that action. Please try again."

        choice = llm_resp.get("choices", [{}])[0]
        message = choice.get("message", {})
        tool_calls = message.get("tool_calls") or []
        content = message.get("content") or ""

        # Fallback: parse XML <tool_call> tags if no native tool_calls
        if not tool_calls and content:
            tool_calls = parse_xml_tool_calls(content)

        # No tool calls — return the text response
        if not tool_calls:
            TOOL_ROUNDS.observe(round_num + 1)
            result = clean_response(content)
            logger.info("[%s] Final response: %s...", label, result[:100])
            return result

        # Filter out duplicate calls (already executed in a previous round)
        new_tool_calls = []
        for tool_call in tool_calls:
            function = tool_call.get("function", {})
            tool_name = function.get("name", "")
            args_str = function.get("arguments", "{}")
            try:
                arguments = json.loads(args_str)
            except json.JSONDecodeError:
                logger.warning("[%s] Malformed arguments for %s: %s", label, tool_name, args_str[:100])
                arguments = {}
            call_key = (tool_name, json.dumps(arguments, sort_keys=True))
            if call_key in executed_calls:
                logger.info("[%s] Skipping repeat tool call: %s", label, tool_name)
            else:
                new_tool_calls.append(tool_call)

        # All calls are repeats — force a final response
        if not new_tool_calls:
            logger.info("[%s] All tool calls are repeats, forcing final response", label)
            messages.append({"role": "assistant", "content": content})
            messages.append(
                {
                    "role": "user",
                    "content": "You already have the results above. Do NOT call any tools again. Summarize the information in a brief, natural response.",
                }
            )
            _final_t0 = time.time()
            try:
                final_resp = await call_model(
                    model_url,
                    model_name,
                    messages,
                    system=system_prompt,
                    timeout=60,
                )
                LLM_CALL_COUNT.labels(model=model_name, purpose="unified_final").inc()
                LLM_CALL_LATENCY.labels(model=model_name, purpose="unified_final").observe(time.time() - _final_t0)
                final_content = final_resp.get("choices", [{}])[0].get("message", {}).get("content", "")
                return clean_response(final_content)
            except Exception as e:
                LLM_CALL_ERRORS.labels(model=model_name, error_type=type(e).__name__).inc()
                logger.error("[%s] Final response failed: %s", label, e)
                return "I found some results but couldn't summarize them. Please try again."

        # Execute new tool calls
        logger.info("[%s] Processing %d tool call(s)", label, len(new_tool_calls))

        # Build assistant message with tool_calls for the conversation
        assistant_msg = {"role": "assistant", "content": content or None}
        # Only include tool_calls that will actually be executed (not skipped duplicates)
        # to avoid dangling tool_calls with no matching tool result messages
        if message.get("tool_calls"):
            executed_ids = {tc.get("id") for tc in new_tool_calls}
            assistant_msg["tool_calls"] = [tc for tc in message["tool_calls"] if tc.get("id") in executed_ids]
        messages.append(assistant_msg)

        tool_results = []
        has_terminal = False
        for tool_call in new_tool_calls:
            function = tool_call.get("function", {})
            tool_name = function.get("name", "")
            args_str = function.get("arguments", "{}")
            try:
                arguments = json.loads(args_str)
            except json.JSONDecodeError:
                arguments = {}

            # Validate tool name against allowlist (prevents hallucinated/injected tools)
            if tool_name not in valid_tool_names:
                logger.warning("[%s] Rejected unknown tool call: %s", label, tool_name[:50])
                tool_results.append((tool_call.get("id", f"call_{round_num}"), tool_name, f"Unknown tool: {tool_name}"))
                continue

            call_key = (tool_name, json.dumps(arguments, sort_keys=True))
            executed_calls.add(call_key)

            TOOL_CALL_COUNT.labels(tool=tool_name).inc()
            _tool_t0 = time.time()
            try:
                result = await execute_tool(tool_name, arguments)
            except Exception as e:
                TOOL_CALL_ERRORS.labels(tool=tool_name).inc()
                logger.error("[%s] Tool %s failed: %s", label, tool_name, e, exc_info=True)
                # Return generic message to model/user — full details logged above
                result = f"The {tool_name} tool encountered an error. Please try again."
            TOOL_CALL_LATENCY.labels(tool=tool_name).observe(time.time() - _tool_t0)
            tool_results.append((tool_call.get("id", f"call_{round_num}"), tool_name, result))

            if tool_name in TERMINAL_TOOLS:
                has_terminal = True

        # Add tool results to conversation
        # Use proper tool role messages if the model sent native tool_calls
        if message.get("tool_calls"):
            for call_id, _tool_name, result in tool_results:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": str(result),
                    }
                )
        else:
            # XML fallback: add results as user message (like nemotron_loop)
            results_text = "\n".join(f"[{name}] {result}" for _, name, result in tool_results)
            messages.append(
                {
                    "role": "user",
                    "content": f"<tool_response>\n{results_text}\n</tool_response>\n\nThe action is complete. Summarize the result in a brief, natural response. Do NOT call any more tools.",
                }
            )

        # For terminal tools, allow one final round for natural language response
        # but prevent further tool calls
        if has_terminal:
            logger.info("[%s] Terminal tool executed, allowing final response round", label)
            # Add instruction to respond naturally without more tool calls
            if message.get("tool_calls"):
                messages.append(
                    {
                        "role": "user",
                        "content": "The action(s) above are complete. Now respond naturally to the user's full request. Do NOT call any more tools.",
                    }
                )
            # Cap remaining rounds to 1 (just generate the response)
            max_rounds = round_num + 2  # current round + 1 more

    # Hit max rounds
    TOOL_ROUNDS.observe(max_rounds)
    logger.warning("[%s] Hit max tool rounds (%d)", label, max_rounds)
    return "I tried to complete that but ran into some complexity. Please try a simpler request."
