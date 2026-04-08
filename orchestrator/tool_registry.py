"""
Tool registry for Brain Gateway.

Provides a decorator-based registration system and a unified execute_tool()
function that replaces the if-elif chain in tool_handlers.py.

Usage:
    from tool_registry import register_tool, execute_tool

    @register_tool("my_tool")
    async def handle_my_tool(arguments: dict) -> str:
        return "result"

    result = await execute_tool("my_tool", {"key": "value"})
"""

import logging
import time
from typing import Any, Callable, Dict, List

from metrics import TOOL_CALL_COUNT, TOOL_CALL_ERRORS, TOOL_CALL_LATENCY

logger = logging.getLogger(__name__)

# Module-level registry: tool_name -> handler function
_registry: Dict[str, Callable] = {}


def register_tool(name: str):
    """
    Decorator to register a tool handler.

    The handler must accept a single `arguments: dict` parameter and return a string.
    It can be sync or async — execute_tool handles both.

    Example:
        @register_tool("search_memory")
        async def handle_search_memory(arguments: dict) -> str:
            query = arguments.get("query", "")
            return do_search(query)
    """

    def decorator(func: Callable) -> Callable:
        if name in _registry:
            logger.warning("[TOOL_REGISTRY] Overwriting existing handler for '%s'", name)
        _registry[name] = func
        return func

    return decorator


async def execute_tool(tool_name: str, arguments: Dict[str, Any]) -> str:
    """
    Execute a registered tool by name.

    Wraps every call with:
    - TOOL_CALL_COUNT increment
    - TOOL_CALL_LATENCY observation
    - TOOL_CALL_ERRORS increment on failure
    - Structured logging
    """
    TOOL_CALL_COUNT.labels(tool=tool_name).inc()
    _t0 = time.time()
    logger.info(
        "[TOOL] Executing: %s with args: %s",
        tool_name,
        arguments,
        extra={"component": "tool", "tool_name": tool_name},
    )

    from exceptions import ExternalServiceError, TransientError

    handler = _registry.get(tool_name)
    if not handler:
        TOOL_CALL_ERRORS.labels(tool=tool_name).inc()
        logger.warning("[TOOL] Unknown tool: %s", tool_name)
        return f"Unknown tool: {tool_name}"

    try:
        import asyncio

        if asyncio.iscoroutinefunction(handler):
            result = await handler(arguments)
        else:
            result = handler(arguments)
        return result
    except TransientError as e:
        TOOL_CALL_ERRORS.labels(tool=tool_name).inc()
        svc = e.service if isinstance(e, ExternalServiceError) else "unknown"
        logger.warning(
            "[TOOL] Transient error in %s (%s): %s",
            tool_name,
            svc,
            e,
            extra={"component": "tool", "tool_name": tool_name, "error_type": "transient"},
        )
        return f"Service temporarily unavailable: {e}"
    except Exception as e:
        TOOL_CALL_ERRORS.labels(tool=tool_name).inc()
        logger.error(
            "[TOOL] Error executing %s: %s",
            tool_name,
            e,
            extra={"component": "tool", "tool_name": tool_name, "error_type": type(e).__name__},
        )
        return f"Error executing {tool_name}: {e}"
    finally:
        TOOL_CALL_LATENCY.labels(tool=tool_name).observe(time.time() - _t0)


def get_registered_tools() -> List[str]:
    """Return a sorted list of all registered tool names."""
    return sorted(_registry.keys())


def is_registered(tool_name: str) -> bool:
    """Check if a tool is registered."""
    return tool_name in _registry
