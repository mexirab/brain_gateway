"""
Error counters exist at 0 before the first failure, and the tool registry.

Prometheus increase()/rate() can't see a counter series' first sample, so a
series created lazily by its first .inc() hides the first failure after every
orchestrator restart from HighErrorRate / HighToolErrorRate
(monitoring/prometheus/alert-rules.yml). These tests pin the pre-seeding:

- orchestrator.metrics creates the three fixed bgw_request_errors_total label
  sets and the four bgw_client_markup_neutralized_total {role, position}
  series at 0 (checked exactly in a fresh interpreter, since other test files
  increment them in this process).
- register_tool creates bgw_tool_calls_total / bgw_tool_call_errors_total for
  its tool at 0, so every tool tool_handlers registers has both series.
- Re-registering a tool keeps its count and warns; execute_tool runs sync and
  async handlers and still counts calls and errors (success, handler
  exception, transient error, unknown tool); get_registered_tools is sorted.

The registry is never emptied: tests register uniquely named scratch tools and
remove them afterwards, so the real handlers stay registered for every other
test file. Metrics are process-global: checks are exact only for names this
file owns (cleaned out of the registry and both counters afterwards), deltas
otherwise.
"""

import contextlib
import logging
import os
import subprocess
import sys

import pytest
from prometheus_client import REGISTRY

# tool_handlers is imported at collection time on purpose: it registers every
# handler once, at first import.
from orchestrator import tool_handlers, tool_registry  # noqa: F401
from orchestrator.exceptions import ExternalServiceError
from orchestrator.metrics import TOOL_CALL_COUNT, TOOL_CALL_ERRORS, TOOL_CALL_LATENCY
from orchestrator.tests._helpers import REPO_ROOT
from orchestrator.tool_registry import execute_tool, get_registered_tools, register_tool

PRESEEDED = (
    ("bgw_request_errors_total", {"mode": "unified", "error_type": "model_unavailable"}),
    ("bgw_request_errors_total", {"mode": "unified", "error_type": "tool_loop_failed"}),
    ("bgw_request_errors_total", {"mode": "unified_fallback", "error_type": "fallback_failed"}),
    *(
        ("bgw_client_markup_neutralized_total", {"role": r, "position": p})
        for r in ("user", "assistant")
        for p in ("latest", "history")
    ),
)

TEST_TOOL = "zz_test_tool"
UNKNOWN_TOOL = "zz_test_unknown_tool"
SCRATCH = (TEST_TOOL, UNKNOWN_TOOL, "zz_test_alpha", "zz_test_zebra")


def _calls(tool):
    return REGISTRY.get_sample_value("bgw_tool_calls_total", {"tool": tool})


def _errors(tool):
    return REGISTRY.get_sample_value("bgw_tool_call_errors_total", {"tool": tool})


def _drop_series(tool):
    for metric in (TOOL_CALL_COUNT, TOOL_CALL_ERRORS, TOOL_CALL_LATENCY):
        with contextlib.suppress(KeyError):
            metric.remove(tool)


@pytest.fixture
def scratch_tool():
    """Tool names nobody else uses: absent from the registry and from both
    counters before the test, removed from all of them afterwards."""
    for name in SCRATCH:
        tool_registry._registry.pop(name, None)
        _drop_series(name)
    yield TEST_TOOL
    for name in SCRATCH:
        tool_registry._registry.pop(name, None)
        _drop_series(name)


# ---------------------------------------------------------------------------
# Series pre-seeded at import
# ---------------------------------------------------------------------------


def test_preseeded_series_start_at_zero_after_restart():
    """A fresh interpreter is the state Prometheus scrapes right after a
    restart: the series must be there, at exactly 0, before any request (in
    this process other tests' .labels() calls would hide a missing pre-init)."""
    code = (
        "import json\n"
        "from prometheus_client import REGISTRY\n"
        "import orchestrator.metrics\n"
        f"series = {list(PRESEEDED)!r}\n"
        "print(json.dumps([REGISTRY.get_sample_value(n, l) for n, l in series]))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == str([0.0] * len(PRESEEDED))


# ---------------------------------------------------------------------------
# bgw_tool_calls_total / bgw_tool_call_errors_total
# ---------------------------------------------------------------------------


def test_every_registered_tool_has_both_series():
    registered = get_registered_tools()
    assert "search_memory" in registered
    missing = [t for t in registered if _calls(t) is None or _errors(t) is None]
    assert missing == []


def test_register_tool_creates_series_at_zero(scratch_tool):
    assert _calls(scratch_tool) is None
    assert _errors(scratch_tool) is None

    @register_tool(scratch_tool)
    def handle(arguments):
        return "ok"

    assert tool_registry._registry[scratch_tool] is handle
    assert _calls(scratch_tool) == 0.0
    assert _errors(scratch_tool) == 0.0


async def test_reregistering_replaces_the_handler_keeps_counts_and_warns(scratch_tool, caplog):
    @register_tool(scratch_tool)
    def first(arguments):
        return "first"

    await execute_tool(scratch_tool, {})

    with caplog.at_level(logging.WARNING, logger="orchestrator.tool_registry"):

        @register_tool(scratch_tool)
        def second(arguments):
            return "second"

    assert any(scratch_tool in r.getMessage() for r in caplog.records)
    assert tool_registry._registry[scratch_tool] is second
    assert _calls(scratch_tool) == 1.0
    assert await execute_tool(scratch_tool, {}) == "second"
    assert _calls(scratch_tool) == 2.0


async def test_execute_tool_runs_sync_and_async_handlers(scratch_tool):
    @register_tool(scratch_tool)
    def sync_handle(arguments):
        return f"got {arguments['x']}"

    assert await execute_tool(scratch_tool, {"x": 42}) == "got 42"

    @register_tool(scratch_tool)
    async def async_handle(arguments):
        return f"async {arguments['y']}"

    assert await execute_tool(scratch_tool, {"y": "hello"}) == "async hello"


async def test_execute_tool_counts_calls_and_errors(scratch_tool):
    outcomes = iter([None, ValueError("something broke"), ExternalServiceError("calendar", "timeout")])

    @register_tool(scratch_tool)
    async def handle(arguments):
        exc = next(outcomes)
        if exc:
            raise exc
        return "ok"

    assert await execute_tool(scratch_tool, {}) == "ok"
    assert (_calls(scratch_tool), _errors(scratch_tool)) == (1.0, 0.0)

    out = await execute_tool(scratch_tool, {})
    assert f"Error executing {scratch_tool}" in out and "something broke" in out
    assert (_calls(scratch_tool), _errors(scratch_tool)) == (2.0, 1.0)

    assert "temporarily unavailable" in await execute_tool(scratch_tool, {})
    assert (_calls(scratch_tool), _errors(scratch_tool)) == (3.0, 2.0)


async def test_execute_unknown_tool_counts_call_and_error(scratch_tool):
    out = await execute_tool(UNKNOWN_TOOL, {})
    assert out == f"Unknown tool: {UNKNOWN_TOOL}"
    assert (_calls(UNKNOWN_TOOL), _errors(UNKNOWN_TOOL)) == (1.0, 1.0)


def test_get_registered_tools_is_sorted(scratch_tool):
    for name in ("zz_test_zebra", "zz_test_alpha"):
        register_tool(name)(lambda arguments: "")
    registered = get_registered_tools()
    assert registered == sorted(registered)
    assert {"zz_test_alpha", "zz_test_zebra", "web_search"} <= set(registered)
