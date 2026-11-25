"""
Tests for task_decomposition.py — decompose tasks into ADHD-friendly micro-steps,
track progress, skip/abandon, resource caps, and TTS-friendly output.

Mocks external dependencies: call_model, Prometheus metrics.
"""

import json
import math
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator import task_decomposition

# ---------------------------------------------------------------------------
# Helpers + fixtures
# ---------------------------------------------------------------------------


def _make_model_response(steps_json):
    """Build a fake call_model return value wrapping a JSON string."""
    content = json.dumps(steps_json) if isinstance(steps_json, list) else str(steps_json)
    return {"choices": [{"message": {"content": content}}]}


def _sample_steps(n=3, base_minutes=10):
    """Return a list of raw step dicts the model would produce."""
    return [{"description": f"Step {i + 1} action", "est_minutes": base_minutes} for i in range(n)]


@pytest.fixture
def patched_td():
    """task_decomposition with its metrics mocked and no active tasks."""
    with patch.multiple(
        task_decomposition,
        TASK_DECOMP_TASKS_CREATED=MagicMock(),
        TASK_DECOMP_STEPS_COMPLETED=MagicMock(),
        TASK_DECOMP_STEPS_SKIPPED=MagicMock(),
        TASK_DECOMP_TASKS_ABANDONED=MagicMock(),
        TASK_DECOMP_ERRORS=MagicMock(),
    ):
        task_decomposition._active_tasks.clear()
        yield task_decomposition
        task_decomposition._active_tasks.clear()


@pytest.fixture
def decompose(patched_td):
    """``await decompose(text, steps, **kw)`` runs decompose_task with the model
    answering ``steps`` (a list → JSON, a string → sent as-is). Returns
    (reply, newest active task or None)."""

    async def run(text, steps=None, **kwargs):
        response = _make_model_response(_sample_steps() if steps is None else steps)
        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock, return_value=response):
            reply = await patched_td.decompose_task(text, **kwargs)
        tasks = list(patched_td._active_tasks.values())
        return reply, (tasks[-1] if tasks else None)

    return run


# ---------------------------------------------------------------------------
# decompose_task
# ---------------------------------------------------------------------------


class TestDecomposeTask:
    async def test_steps_created_with_correct_descriptions(self, patched_td, decompose):
        _, task = await decompose("Clean the kitchen", _sample_steps(3))

        assert len(patched_td._active_tasks) == 1
        assert task.original_text == "Clean the kitchen"
        assert [s.description for s in task.steps] == ["Step 1 action", "Step 2 action", "Step 3 action"]

    async def test_next_step_only_mode_returns_first_step(self, decompose):
        result, _ = await decompose("Do laundry", _sample_steps(3), mode="next_step_only")
        assert "first step" in result.lower()
        assert "Step 1 action" in result
        assert "3 steps" in result

    async def test_full_list_mode_returns_all_steps(self, decompose):
        result, _ = await decompose("Organize desk", _sample_steps(3), mode="full_list")
        for i in (1, 2, 3):
            assert f"{i}." in result
            assert f"Step {i} action" in result

    async def test_json_parse_failure_falls_back_to_single_step(self, patched_td, decompose):
        _, task = await decompose("Buy groceries", "This is not JSON at all")
        assert len(patched_td._active_tasks) == 1
        assert [s.description for s in task.steps] == ["Buy groceries"]

    async def test_non_list_json_is_an_error(self, patched_td, decompose):
        """A JSON object instead of a list is refused: apology, nothing stored."""
        result, _ = await decompose("Fix bike", '{"step": "do thing"}')
        assert "couldn't break that task down" in result.lower()
        assert patched_td._active_tasks == {}

    async def test_model_call_failure_returns_error_string(self, patched_td):
        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock, side_effect=RuntimeError("down")):
            result = await patched_td.decompose_task("Plan vacation")

        assert "couldn't break that task down" in result.lower()
        assert len(patched_td._active_tasks) == 0

    async def test_input_truncated_to_max_length(self, patched_td, decompose):
        _, task = await decompose("x" * 2000, [{"description": "Do it", "est_minutes": 10}])
        assert len(task.original_text) == patched_td.MAX_TASK_TEXT_LENGTH

    async def test_markdown_code_fences_stripped(self, decompose):
        _, task = await decompose("Paint the wall", f"```json\n{json.dumps(_sample_steps(2))}\n```")
        assert len(task.steps) == 2

    async def test_empty_task_text_returns_prompt(self, patched_td):
        result = await patched_td.decompose_task("")
        assert "tell me what task" in result.lower()


# ---------------------------------------------------------------------------
# est_minutes: clamp to 1..240, default 15, then the ADHD time buffer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("step", "base_minutes"),
    [
        pytest.param({"est_minutes": 10}, 10, id="10"),
        pytest.param({"est_minutes": "not a number"}, 15, id="text-falls-back"),
        pytest.param({"est_minutes": -5}, 1, id="clamped-min"),
        pytest.param({"est_minutes": 999}, 240, id="clamped-max"),
        pytest.param({}, 15, id="missing"),
    ],
)
async def test_est_minutes(patched_td, decompose, step, base_minutes):
    _, task = await decompose("Some task", [{"description": "Do thing", **step}])
    assert task.steps[0].est_minutes == math.ceil(base_minutes * patched_td.ADHD_TIME_BUFFER)


# ---------------------------------------------------------------------------
# get_next_step / complete_step / skip_step / abandon_task
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("func", ["get_next_step", "complete_step", "abandon_task"])
def test_missing_task_id(patched_td, func):
    assert "No active task found" in getattr(patched_td, func)("nonexistent")


async def test_get_next_step_returns_current_step(patched_td, decompose):
    _, task = await decompose("Test task", _sample_steps(3))
    result = patched_td.get_next_step(task.task_id)
    assert "Step 1 of 3" in result
    assert "Step 1 action" in result


class TestCompleteStep:
    async def test_step_marked_done_and_advances(self, patched_td, decompose):
        _, task = await decompose("Build shelf", _sample_steps(3))

        result = patched_td.complete_step(task.task_id)
        assert task.steps[0].completed is True
        assert "step 1 done" in result.lower()
        assert "Step 2 action" in result

    async def test_completion_summary_at_end(self, patched_td, decompose):
        _, task = await decompose("Quick task", _sample_steps(2))

        patched_td.complete_step(task.task_id)
        result = patched_td.complete_step(task.task_id)
        assert "All done" in result
        assert "2 of 2 steps completed" in result


class TestSkipStep:
    async def test_step_marked_skipped_and_advances(self, patched_td, decompose):
        _, task = await decompose("Long project", _sample_steps(3))

        result = patched_td.skip_step(task.task_id)
        assert task.steps[0].skipped is True
        assert "Skipped step 1" in result
        assert "Step 2 action" in result

    async def test_skip_all_gives_completion_summary(self, patched_td, decompose):
        _, task = await decompose("Skip everything", _sample_steps(2))

        patched_td.skip_step(task.task_id)
        result = patched_td.skip_step(task.task_id)
        assert "All done" in result
        assert "2 skipped" in result


class TestAbandonTask:
    async def test_task_removed_without_guilt(self, patched_td, decompose):
        _, task = await decompose("Abandoned task", _sample_steps(3))

        result = patched_td.abandon_task(task.task_id)
        assert "Stopped tracking" in result
        assert len(patched_td._active_tasks) == 0
        # No guilt: no words like "failed", "disappointed", "should have"
        for word in ("failed", "disappointed", "should have"):
            assert word not in result.lower()

    async def test_abandon_with_progress_shows_count(self, patched_td, decompose):
        _, task = await decompose("Partial task", _sample_steps(3))
        patched_td.complete_step(task.task_id)

        result = patched_td.abandon_task(task.task_id)
        assert "1 of 3 steps done" in result


# ---------------------------------------------------------------------------
# list_active_tasks / get_active_tasks_context
# ---------------------------------------------------------------------------


class TestListActiveTasks:
    def test_empty_case(self, patched_td):
        assert "No active decomposed tasks" in patched_td.list_active_tasks()
        assert patched_td.get_active_tasks_context() == ""

    async def test_single_task(self, patched_td, decompose):
        await decompose("Only task", _sample_steps(2))

        result = patched_td.list_active_tasks()
        assert "1 active task" in result
        assert "Only task" in result
        assert "0/2 steps done" in result

        context = patched_td.get_active_tasks_context()
        assert "ACTIVE DECOMPOSED TASKS" in context
        assert "Only task" in context
        assert "0/2 done" in context

    async def test_multiple_tasks(self, patched_td, decompose):
        await decompose("Task A", _sample_steps(2))
        await decompose("Task B", _sample_steps(2))

        result = patched_td.list_active_tasks()
        assert "2 active task" in result
        assert "Task A" in result
        assert "Task B" in result


# ---------------------------------------------------------------------------
# Resource caps
# ---------------------------------------------------------------------------


class TestResourceCaps:
    async def test_max_active_tasks_evicts_oldest(self, patched_td, decompose):
        for i in range(patched_td.MAX_ACTIVE_TASKS):
            await decompose(f"Task {i}", _sample_steps(1))
        assert len(patched_td._active_tasks) == patched_td.MAX_ACTIVE_TASKS
        first_task_id = next(iter(patched_td._active_tasks))

        _, newest = await decompose("Overflow task", _sample_steps(1))

        assert len(patched_td._active_tasks) == patched_td.MAX_ACTIVE_TASKS
        assert first_task_id not in patched_td._active_tasks
        assert newest.original_text == "Overflow task"

    async def test_max_steps_per_task_limits_steps(self, patched_td, decompose):
        _, task = await decompose("Huge task", _sample_steps(patched_td.MAX_STEPS_PER_TASK + 10))
        assert len(task.steps) == patched_td.MAX_STEPS_PER_TASK
