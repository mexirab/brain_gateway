"""
Error counters exist at 0 before the first failure.

Prometheus increase()/rate() can't see a counter series' first sample, so a
series created lazily by its first .inc() hides the first failure after every
orchestrator restart from HighErrorRate / HighToolErrorRate
(monitoring/prometheus/alert-rules.yml). These tests pin the pre-seeding:

- orchestrator.metrics creates the three fixed bgw_request_errors_total label
  sets at 0 (checked exactly in a fresh interpreter, since other test files
  increment them in this process).
- register_tool creates bgw_tool_calls_total / bgw_tool_call_errors_total for
  its tool at 0, so every tool tool_handlers registers has both series.
- Re-registering a tool keeps its count; execute_tool still counts calls and
  errors (success, handler exception, transient error, unknown tool).

Metrics are process-global: checks are exact only for names this file owns
(cleaned out of the registry and both counters afterwards), deltas otherwise.
"""

import contextlib
import os
import subprocess
import sys

import pytest
from prometheus_client import REGISTRY

# tool_handlers is imported at collection time on purpose: it registers every
# handler once, at first import (same reason as test_streamed_request_latency).
from orchestrator import tool_handlers, tool_registry  # noqa: F401
from orchestrator.exceptions import ExternalServiceError
from orchestrator.metrics import TOOL_CALL_COUNT, TOOL_CALL_ERRORS, TOOL_CALL_LATENCY
from orchestrator.tool_registry import execute_tool, register_tool

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

PRESEEDED_REQUEST_ERRORS = (
    {"mode": "unified", "error_type": "model_unavailable"},
    {"mode": "unified", "error_type": "tool_loop_failed"},
    {"mode": "unified_fallback", "error_type": "fallback_failed"},
)

TEST_TOOL = "zz_test_tool"
UNKNOWN_TOOL = "zz_test_unknown_tool"


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
    """A tool name nobody else uses: absent from the registry and from both
    counters before the test, removed from all of them afterwards."""
    for name in (TEST_TOOL, UNKNOWN_TOOL):
        tool_registry._registry.pop(name, None)
        _drop_series(name)
    yield TEST_TOOL
    for name in (TEST_TOOL, UNKNOWN_TOOL):
        tool_registry._registry.pop(name, None)
        _drop_series(name)


# ---------------------------------------------------------------------------
# bgw_request_errors_total
# ---------------------------------------------------------------------------


def test_request_errors_fixed_types_exist_in_process():
    for labels in PRESEEDED_REQUEST_ERRORS:
        value = REGISTRY.get_sample_value("bgw_request_errors_total", labels)
        assert value is not None, labels
        assert value >= 0.0, labels


def test_request_errors_fixed_types_start_at_zero_after_restart():
    """A fresh interpreter is the state Prometheus scrapes right after a
    restart: the series must be there, at exactly 0, before any request."""
    code = (
        "import json\n"
        "from prometheus_client import REGISTRY\n"
        "import orchestrator.metrics\n"
        f"labels = {list(PRESEEDED_REQUEST_ERRORS)!r}\n"
        "print(json.dumps([REGISTRY.get_sample_value('bgw_request_errors_total', l) for l in labels]))\n"
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
    assert proc.stdout.strip().splitlines()[-1] == "[0.0, 0.0, 0.0]"


# ---------------------------------------------------------------------------
# bgw_tool_calls_total / bgw_tool_call_errors_total
# ---------------------------------------------------------------------------


def test_every_registered_tool_has_both_series():
    registered = tool_registry.get_registered_tools()
    assert "search_memory" in registered
    missing = [t for t in registered if _calls(t) is None or _errors(t) is None]
    assert missing == []


def test_register_tool_creates_series_at_zero(scratch_tool):
    assert _calls(scratch_tool) is None
    assert _errors(scratch_tool) is None

    @register_tool(scratch_tool)
    def handle(arguments):
        return "ok"

    assert _calls(scratch_tool) == 0.0
    assert _errors(scratch_tool) == 0.0


async def test_reregistering_keeps_existing_counts(scratch_tool):
    @register_tool(scratch_tool)
    def first(arguments):
        return "first"

    await execute_tool(scratch_tool, {})

    @register_tool(scratch_tool)
    def second(arguments):
        return "second"

    assert _calls(scratch_tool) == 1.0
    assert await execute_tool(scratch_tool, {}) == "second"
    assert _calls(scratch_tool) == 2.0


async def test_execute_tool_counts_calls_and_errors(scratch_tool):
    outcomes = iter([None, ValueError("boom"), ExternalServiceError("calendar", "timeout")])

    @register_tool(scratch_tool)
    async def handle(arguments):
        exc = next(outcomes)
        if exc:
            raise exc
        return "ok"

    assert await execute_tool(scratch_tool, {}) == "ok"
    assert (_calls(scratch_tool), _errors(scratch_tool)) == (1.0, 0.0)

    assert "Error executing" in await execute_tool(scratch_tool, {})
    assert (_calls(scratch_tool), _errors(scratch_tool)) == (2.0, 1.0)

    assert "temporarily unavailable" in await execute_tool(scratch_tool, {})
    assert (_calls(scratch_tool), _errors(scratch_tool)) == (3.0, 2.0)


async def test_execute_unknown_tool_counts_call_and_error(scratch_tool):
    out = await execute_tool(UNKNOWN_TOOL, {})
    assert out == f"Unknown tool: {UNKNOWN_TOOL}"
    assert (_calls(UNKNOWN_TOOL), _errors(UNKNOWN_TOOL)) == (1.0, 1.0)
