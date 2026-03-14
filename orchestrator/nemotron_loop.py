"""
Nemotron agentic tool loop: XML parsing, dedup, multi-round execution.
"""

import json
import logging
import re
from typing import Any, Dict, List

from metrics import TOOL_ROUNDS
from prompt_builder import get_nemotron_system_prompt
from shared import MAX_TOOL_ROUNDS, NEMOTRON_MODEL, NEMOTRON_URL
from tool_definitions import get_orchestrator_tools

logger = logging.getLogger(__name__)

# Tools that mutate state — return result directly, don't let Nemotron loop
TERMINAL_TOOLS = {
    "start_focus",
    "stop_focus",
    "set_reminder",
    "cancel_reminder",
    "home_assistant",
    "update_data",
    "create_calendar_event",
    "brain_dump",
}


def clean_response(text: str) -> str:
    """Remove <think> and <tool_call> tags from Nemotron responses."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.DOTALL)
    return text.strip()


def parse_tool_calls_from_content(content: str) -> List[Dict[str, Any]]:
    """Parse <tool_call> tags from Nemotron's content output.

    Nemotron outputs tool calls as:
    <tool_call>
    {"name": "tool_name", "arguments": {...}}
    </tool_call>
    """
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
        except json.JSONDecodeError as e:
            logger.warning(f"[TOOL_PARSE] Failed to parse tool call: {e}")
            continue

    return tool_calls


async def _run_nemotron_tool_loop(messages: List[Dict], system_prompt: str, label: str = "NEMOTRON") -> str:
    """
    Shared Nemotron agentic tool loop.

    Runs Nemotron in a loop, parsing tool calls, deduplicating across rounds,
    executing tools, and returning the final text result.
    """
    # Import here to avoid circular import (tool_handlers imports from us)
    from orchestrator import call_model
    from tool_handlers import execute_tool

    executed_calls = set()

    for round_num in range(MAX_TOOL_ROUNDS):
        logger.info(f"[{label}] Round {round_num + 1}/{MAX_TOOL_ROUNDS}")

        try:
            llm_resp = await call_model(
                NEMOTRON_URL,
                NEMOTRON_MODEL,
                messages,
                system=system_prompt,
                tools=get_orchestrator_tools(),
                tool_choice="none",
                timeout=60,
            )
        except Exception as e:
            logger.error(f"[{label}] Call failed: {e}")
            return "Sorry, I couldn't complete that action. Please try again."

        choice = llm_resp.get("choices", [{}])[0]
        message = choice.get("message", {})
        tool_calls = message.get("tool_calls", [])
        content = message.get("content") or ""

        if not tool_calls and content:
            tool_calls = parse_tool_calls_from_content(content)

        if not tool_calls:
            TOOL_ROUNDS.observe(round_num + 1)
            result = clean_response(content)
            logger.info(f"[{label}] Final result: {result[:100]}...")
            return result

        # Filter out calls we've already executed
        new_tool_calls = []
        for tool_call in tool_calls:
            function = tool_call.get("function", {})
            tool_name = function.get("name", "")
            args_str = function.get("arguments", "{}")
            try:
                arguments = json.loads(args_str)
            except json.JSONDecodeError:
                arguments = {}
            call_key = (tool_name, json.dumps(arguments, sort_keys=True))
            if call_key in executed_calls:
                logger.info(f"[{label}] Skipping repeat tool call: {tool_name} (already executed)")
            else:
                new_tool_calls.append(tool_call)

        # If all calls are repeats, force a final response
        if not new_tool_calls:
            logger.info(f"[{label}] All tool calls are repeats, forcing final response")
            messages.append({"role": "assistant", "content": content})
            messages.append(
                {
                    "role": "user",
                    "content": "You already have the results above. Do NOT call any tools again. Summarize the information in a brief, natural response.",
                }
            )
            try:
                final_resp = await call_model(
                    NEMOTRON_URL,
                    NEMOTRON_MODEL,
                    messages,
                    system=system_prompt,
                    timeout=60,
                )
                final_content = final_resp.get("choices", [{}])[0].get("message", {}).get("content", "")
                return clean_response(final_content)
            except Exception as e:
                logger.error(f"[{label}] Final response failed: {e}")
                return "I found some results but couldn't summarize them. Please try again."

        # Process new tool calls
        logger.info(f"[{label}] Processing {len(new_tool_calls)} tool call(s)")
        messages.append({"role": "assistant", "content": content})

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

            call_key = (tool_name, json.dumps(arguments, sort_keys=True))
            executed_calls.add(call_key)

            result = await execute_tool(tool_name, arguments)
            tool_results.append(f"[{tool_name}] {result}")

            if tool_name in TERMINAL_TOOLS:
                has_terminal = True

        results_text = "\n".join(tool_results)

        # For state-changing tools, return immediately
        if has_terminal:
            TOOL_ROUNDS.observe(round_num + 1)
            logger.info(f"[{label}] Terminal tool executed, returning result directly")
            return results_text

        # Add tool results for next round
        messages.append(
            {
                "role": "user",
                "content": f"<tool_response>\n{results_text}\n</tool_response>\n\nThe action is complete. Summarize the result in a brief, natural response. Do NOT call any more tools.",
            }
        )

    # Hit max rounds
    TOOL_ROUNDS.observe(MAX_TOOL_ROUNDS)
    logger.warning(f"[{label}] Hit max tool rounds")
    return "I tried to complete that but ran into some complexity. Please try a simpler request."


async def call_nemotron_orchestrator(command: str) -> str:
    """
    Send a command to Nemotron for tool execution.
    Returns the final result after executing any necessary tools.
    """
    logger.info(f"[NEMOTRON] Orchestrating command: {command[:100]}...")
    messages = [{"role": "user", "content": command}]
    system_prompt = get_nemotron_system_prompt()
    return await _run_nemotron_tool_loop(messages, system_prompt, label="NEMOTRON")
