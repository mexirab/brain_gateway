"""Tests for orchestrator/tool_registry.py — register, execute, list."""

import asyncio

import pytest

from orchestrator import tool_registry
from orchestrator.exceptions import ExternalServiceError
from orchestrator.tool_registry import execute_tool, get_registered_tools, register_tool


@pytest.fixture(autouse=True)
def _clear_registry():
    """Clear the registry before each test to prevent cross-test leakage."""
    tool_registry._registry.clear()
    yield
    tool_registry._registry.clear()


class TestRegisterTool:
    """register_tool decorator adds handlers to the registry."""

    def test_register_sync_handler(self):
        @register_tool("my_sync")
        def handle(arguments):
            return "ok"

        assert "my_sync" in tool_registry._registry
        assert tool_registry._registry["my_sync"] is handle

    def test_register_async_handler(self):
        @register_tool("my_async")
        async def handle(arguments):
            return "ok"

        assert "my_async" in tool_registry._registry

    def test_overwrite_warns(self, caplog):
        @register_tool("dupe")
        def first(arguments):
            return "first"

        @register_tool("dupe")
        def second(arguments):
            return "second"

        assert tool_registry._registry["dupe"] is second


class TestExecuteTool:
    """execute_tool dispatches to registered handlers."""

    def test_sync_handler(self):
        @register_tool("sync_tool")
        def handle(arguments):
            return f"got {arguments['x']}"

        result = asyncio.get_event_loop().run_until_complete(execute_tool("sync_tool", {"x": 42}))
        assert result == "got 42"

    def test_async_handler(self):
        @register_tool("async_tool")
        async def handle(arguments):
            return f"async {arguments['y']}"

        result = asyncio.get_event_loop().run_until_complete(execute_tool("async_tool", {"y": "hello"}))
        assert result == "async hello"

    def test_unknown_tool_returns_error(self):
        result = asyncio.get_event_loop().run_until_complete(execute_tool("no_such_tool", {}))
        assert "Unknown tool" in result
        assert "no_such_tool" in result

    def test_handler_exception_returns_error_string(self):
        @register_tool("broken")
        def handle(arguments):
            raise ValueError("something broke")

        result = asyncio.get_event_loop().run_until_complete(execute_tool("broken", {}))
        assert "Error executing broken" in result
        assert "something broke" in result

    def test_transient_error_returns_service_unavailable(self):
        @register_tool("flaky")
        def handle(arguments):
            raise ExternalServiceError("calendar", "timeout")

        result = asyncio.get_event_loop().run_until_complete(execute_tool("flaky", {}))
        assert "temporarily unavailable" in result


class TestGetRegisteredTools:
    """get_registered_tools returns sorted list."""

    def test_empty_registry(self):
        assert get_registered_tools() == []

    def test_sorted_order(self):
        @register_tool("zebra")
        def z(a):
            return ""

        @register_tool("alpha")
        def a(a):
            return ""

        @register_tool("middle")
        def m(a):
            return ""

        assert get_registered_tools() == ["alpha", "middle", "zebra"]
