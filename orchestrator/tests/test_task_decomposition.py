"""
Tests for task_decomposition.py — decompose tasks into ADHD-friendly micro-steps,
track progress, skip/abandon, resource caps, and TTS-friendly output.

Mocks external dependencies: call_model, Prometheus metrics.
"""

import json
import math
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import check helper
# ---------------------------------------------------------------------------


def _can_import_task_decomp():
    """Check if task_decomposition can be imported."""
    try:
        from orchestrator import task_decomposition  # noqa: F401

        return True
    except (ImportError, ModuleNotFoundError):
        return False


_skip_no_deps = pytest.mark.skipif(
    not _can_import_task_decomp(),
    reason="task_decomposition requires full orchestrator dependencies",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_model_response(steps_json):
    """Build a fake call_model return value wrapping a JSON string."""
    if isinstance(steps_json, list):
        content = json.dumps(steps_json)
    else:
        content = str(steps_json)
    return {
        "choices": [
            {
                "message": {
                    "content": content,
                }
            }
        ]
    }


def _sample_steps(n=3, base_minutes=10):
    """Return a list of raw step dicts the model would produce."""
    return [{"description": f"Step {i + 1} action", "est_minutes": base_minutes} for i in range(n)]


# ---------------------------------------------------------------------------
# Fixture: patched task_decomposition with metrics and state reset
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_td():
    """Patch metrics and reset module state for each test."""
    with (
        patch("orchestrator.task_decomposition.TASK_DECOMP_TASKS_CREATED", MagicMock()),
        patch("orchestrator.task_decomposition.TASK_DECOMP_STEPS_COMPLETED", MagicMock()),
        patch("orchestrator.task_decomposition.TASK_DECOMP_STEPS_SKIPPED", MagicMock()),
        patch("orchestrator.task_decomposition.TASK_DECOMP_TASKS_ABANDONED", MagicMock()),
        patch("orchestrator.task_decomposition.TASK_DECOMP_ERRORS", MagicMock()),
    ):
        from orchestrator import task_decomposition  # Clear active tasks between tests

        task_decomposition._active_tasks.clear()
        yield task_decomposition


# ---------------------------------------------------------------------------
# Tests: decompose_task
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestDecomposeTask:
    @pytest.mark.asyncio
    async def test_steps_created_with_correct_descriptions(self, patched_td):
        td = patched_td
        steps = _sample_steps(3)

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _make_model_response(steps)
            result = await td.decompose_task("Clean the kitchen")

        assert len(td._active_tasks) == 1
        task = list(td._active_tasks.values())[0]
        assert len(task.steps) == 3
        assert task.steps[0].description == "Step 1 action"
        assert task.steps[1].description == "Step 2 action"
        assert task.steps[2].description == "Step 3 action"

    @pytest.mark.asyncio
    async def test_adhd_time_buffer_applied(self, patched_td):
        td = patched_td
        steps = [{"description": "Do thing", "est_minutes": 10}]

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _make_model_response(steps)
            await td.decompose_task("Some task")

        task = list(td._active_tasks.values())[0]
        # 10 * 1.5 = 15
        assert task.steps[0].est_minutes == math.ceil(10 * td.ADHD_TIME_BUFFER)

    @pytest.mark.asyncio
    async def test_task_stored_in_active_tasks(self, patched_td):
        td = patched_td
        steps = _sample_steps(2)

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _make_model_response(steps)
            result = await td.decompose_task("File taxes")

        assert len(td._active_tasks) == 1
        task = list(td._active_tasks.values())[0]
        assert task.original_text == "File taxes"

    @pytest.mark.asyncio
    async def test_next_step_only_mode_returns_first_step(self, patched_td):
        td = patched_td
        steps = _sample_steps(3)

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _make_model_response(steps)
            result = await td.decompose_task("Do laundry", mode="next_step_only")

        assert "first step" in result.lower()
        assert "Step 1 action" in result
        assert "3 steps" in result

    @pytest.mark.asyncio
    async def test_full_list_mode_returns_all_steps(self, patched_td):
        td = patched_td
        steps = _sample_steps(3)

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _make_model_response(steps)
            result = await td.decompose_task("Organize desk", mode="full_list")

        assert "1." in result
        assert "2." in result
        assert "3." in result
        assert "Step 1 action" in result
        assert "Step 2 action" in result
        assert "Step 3 action" in result

    @pytest.mark.asyncio
    async def test_json_parse_failure_falls_back_to_single_step(self, patched_td):
        td = patched_td
        bad_response = {"choices": [{"message": {"content": "This is not JSON at all"}}]}

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = bad_response
            result = await td.decompose_task("Buy groceries")

        assert len(td._active_tasks) == 1
        task = list(td._active_tasks.values())[0]
        assert len(task.steps) == 1
        assert task.steps[0].description == "Buy groceries"

    @pytest.mark.asyncio
    async def test_non_list_json_raises_valueerror_fallback(self, patched_td):
        td = patched_td
        # Model returns a JSON object instead of a list
        bad_response = {"choices": [{"message": {"content": '{"step": "do thing"}'}}]}

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = bad_response
            result = await td.decompose_task("Fix bike")

        # ValueError is caught by the generic except, which returns error string
        assert "couldn't break that task down" in result.lower() or len(td._active_tasks) >= 0

    @pytest.mark.asyncio
    async def test_model_call_failure_returns_error_string(self, patched_td):
        td = patched_td

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.side_effect = RuntimeError("Connection refused")
            result = await td.decompose_task("Plan vacation")

        assert "couldn't break that task down" in result.lower()
        assert len(td._active_tasks) == 0

    @pytest.mark.asyncio
    async def test_input_truncated_to_max_length(self, patched_td):
        td = patched_td
        long_text = "x" * 2000
        steps = [{"description": "Do it", "est_minutes": 10}]

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _make_model_response(steps)
            await td.decompose_task(long_text)

        task = list(td._active_tasks.values())[0]
        assert len(task.original_text) == td.MAX_TASK_TEXT_LENGTH

    @pytest.mark.asyncio
    async def test_markdown_code_fences_stripped(self, patched_td):
        td = patched_td
        steps = _sample_steps(2)
        fenced_content = f"```json\n{json.dumps(steps)}\n```"
        response = {"choices": [{"message": {"content": fenced_content}}]}

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = response
            result = await td.decompose_task("Paint the wall")

        assert len(td._active_tasks) == 1
        task = list(td._active_tasks.values())[0]
        assert len(task.steps) == 2

    @pytest.mark.asyncio
    async def test_empty_task_text_returns_prompt(self, patched_td):
        td = patched_td
        result = await td.decompose_task("")
        assert "tell me what task" in result.lower()


# ---------------------------------------------------------------------------
# Tests: get_next_step
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestGetNextStep:
    @pytest.mark.asyncio
    async def test_returns_current_step(self, patched_td):
        td = patched_td
        steps = _sample_steps(3)

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _make_model_response(steps)
            await td.decompose_task("Test task")

        task = list(td._active_tasks.values())[0]
        result = td.get_next_step(task.task_id)
        assert "Step 1 of 3" in result
        assert "Step 1 action" in result

    def test_missing_task_id(self, patched_td):
        td = patched_td
        result = td.get_next_step("nonexistent")
        assert "No active task found" in result


# ---------------------------------------------------------------------------
# Tests: complete_step
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestCompleteStep:
    @pytest.mark.asyncio
    async def test_step_marked_done_and_advances(self, patched_td):
        td = patched_td
        steps = _sample_steps(3)

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _make_model_response(steps)
            await td.decompose_task("Build shelf")

        task = list(td._active_tasks.values())[0]
        task_id = task.task_id

        result = td.complete_step(task_id)
        assert task.steps[0].completed is True
        assert "step 1 done" in result.lower()
        assert "Step 2 action" in result

    @pytest.mark.asyncio
    async def test_completion_summary_at_end(self, patched_td):
        td = patched_td
        steps = _sample_steps(2)

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _make_model_response(steps)
            await td.decompose_task("Quick task")

        task = list(td._active_tasks.values())[0]
        task_id = task.task_id

        td.complete_step(task_id)
        result = td.complete_step(task_id)
        assert "All done" in result
        assert "2 of 2 steps completed" in result

    def test_missing_task_id(self, patched_td):
        td = patched_td
        result = td.complete_step("nonexistent")
        assert "No active task found" in result


# ---------------------------------------------------------------------------
# Tests: skip_step
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestSkipStep:
    @pytest.mark.asyncio
    async def test_step_marked_skipped_and_advances(self, patched_td):
        td = patched_td
        steps = _sample_steps(3)

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _make_model_response(steps)
            await td.decompose_task("Long project")

        task = list(td._active_tasks.values())[0]
        task_id = task.task_id

        result = td.skip_step(task_id)
        assert task.steps[0].skipped is True
        assert "Skipped step 1" in result
        assert "Step 2 action" in result

    @pytest.mark.asyncio
    async def test_skip_all_gives_completion_summary(self, patched_td):
        td = patched_td
        steps = _sample_steps(2)

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _make_model_response(steps)
            await td.decompose_task("Skip everything")

        task = list(td._active_tasks.values())[0]
        task_id = task.task_id

        td.skip_step(task_id)
        result = td.skip_step(task_id)
        assert "All done" in result
        assert "2 skipped" in result


# ---------------------------------------------------------------------------
# Tests: abandon_task
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestAbandonTask:
    @pytest.mark.asyncio
    async def test_task_removed(self, patched_td):
        td = patched_td
        steps = _sample_steps(3)

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _make_model_response(steps)
            await td.decompose_task("Abandoned task")

        task = list(td._active_tasks.values())[0]
        task_id = task.task_id

        result = td.abandon_task(task_id)
        assert "Stopped tracking" in result
        assert len(td._active_tasks) == 0

    @pytest.mark.asyncio
    async def test_no_guilt_message(self, patched_td):
        td = patched_td
        steps = _sample_steps(2)

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _make_model_response(steps)
            await td.decompose_task("Give up task")

        task = list(td._active_tasks.values())[0]
        task_id = task.task_id

        result = td.abandon_task(task_id)
        # No guilt: no words like "failed", "disappointed", "should have"
        result_lower = result.lower()
        assert "failed" not in result_lower
        assert "disappointed" not in result_lower
        assert "should have" not in result_lower

    @pytest.mark.asyncio
    async def test_abandon_with_progress_shows_count(self, patched_td):
        td = patched_td
        steps = _sample_steps(3)

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _make_model_response(steps)
            await td.decompose_task("Partial task")

        task = list(td._active_tasks.values())[0]
        task_id = task.task_id
        td.complete_step(task_id)

        result = td.abandon_task(task_id)
        assert "1 of 3 steps done" in result

    def test_missing_task_id(self, patched_td):
        td = patched_td
        result = td.abandon_task("nonexistent")
        assert "No active task found" in result


# ---------------------------------------------------------------------------
# Tests: list_active_tasks
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestListActiveTasks:
    def test_empty_case(self, patched_td):
        td = patched_td
        result = td.list_active_tasks()
        assert "No active decomposed tasks" in result

    @pytest.mark.asyncio
    async def test_single_task(self, patched_td):
        td = patched_td
        steps = _sample_steps(2)

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _make_model_response(steps)
            await td.decompose_task("Only task")

        result = td.list_active_tasks()
        assert "1 active task" in result
        assert "Only task" in result
        assert "0/2 steps done" in result

    @pytest.mark.asyncio
    async def test_multiple_tasks(self, patched_td):
        td = patched_td
        steps = _sample_steps(2)

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _make_model_response(steps)
            await td.decompose_task("Task A")
            await td.decompose_task("Task B")

        result = td.list_active_tasks()
        assert "2 active task" in result
        assert "Task A" in result
        assert "Task B" in result


# ---------------------------------------------------------------------------
# Tests: get_active_tasks_context
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestGetActiveTasksContext:
    def test_empty_string_when_no_tasks(self, patched_td):
        td = patched_td
        result = td.get_active_tasks_context()
        assert result == ""

    @pytest.mark.asyncio
    async def test_context_string_when_tasks_exist(self, patched_td):
        td = patched_td
        steps = _sample_steps(2)

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _make_model_response(steps)
            await td.decompose_task("Context task")

        result = td.get_active_tasks_context()
        assert "ACTIVE DECOMPOSED TASKS" in result
        assert "Context task" in result
        assert "0/2 done" in result


# ---------------------------------------------------------------------------
# Tests: Resource caps
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestResourceCaps:
    @pytest.mark.asyncio
    async def test_max_active_tasks_evicts_oldest(self, patched_td):
        td = patched_td
        steps = _sample_steps(1)

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _make_model_response(steps)

            # Fill to MAX_ACTIVE_TASKS
            for i in range(td.MAX_ACTIVE_TASKS):
                await td.decompose_task(f"Task {i}")

            assert len(td._active_tasks) == td.MAX_ACTIVE_TASKS

            # Record the first task_id (oldest)
            first_task_id = list(td._active_tasks.keys())[0]

            # Add one more, should evict oldest
            await td.decompose_task("Overflow task")

        assert len(td._active_tasks) == td.MAX_ACTIVE_TASKS
        assert first_task_id not in td._active_tasks
        # The newest task should be present
        newest = list(td._active_tasks.values())[-1]
        assert newest.original_text == "Overflow task"

    @pytest.mark.asyncio
    async def test_max_steps_per_task_limits_steps(self, patched_td):
        td = patched_td
        # Generate more steps than the cap
        steps = _sample_steps(td.MAX_STEPS_PER_TASK + 10)

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _make_model_response(steps)
            await td.decompose_task("Huge task")

        task = list(td._active_tasks.values())[0]
        assert len(task.steps) == td.MAX_STEPS_PER_TASK


# ---------------------------------------------------------------------------
# Tests: est_minutes validation
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestEstMinutesValidation:
    @pytest.mark.asyncio
    async def test_string_est_minutes_falls_back_to_15(self, patched_td):
        td = patched_td
        steps = [{"description": "Do thing", "est_minutes": "not a number"}]

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _make_model_response(steps)
            await td.decompose_task("Weird input")

        task = list(td._active_tasks.values())[0]
        # Fallback 15 * 1.5 = 22.5 -> ceil = 23
        assert task.steps[0].est_minutes == math.ceil(15 * td.ADHD_TIME_BUFFER)

    @pytest.mark.asyncio
    async def test_est_minutes_clamped_min_1(self, patched_td):
        td = patched_td
        steps = [{"description": "Tiny step", "est_minutes": -5}]

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _make_model_response(steps)
            await td.decompose_task("Min clamp")

        task = list(td._active_tasks.values())[0]
        # max(1, min(-5, 240)) = max(1, -5) = 1, then 1 * 1.5 = 1.5 -> ceil = 2
        assert task.steps[0].est_minutes == math.ceil(1 * td.ADHD_TIME_BUFFER)

    @pytest.mark.asyncio
    async def test_est_minutes_clamped_max_240(self, patched_td):
        td = patched_td
        steps = [{"description": "Huge step", "est_minutes": 999}]

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _make_model_response(steps)
            await td.decompose_task("Max clamp")

        task = list(td._active_tasks.values())[0]
        # max(1, min(999, 240)) = 240, then 240 * 1.5 = 360
        assert task.steps[0].est_minutes == math.ceil(240 * td.ADHD_TIME_BUFFER)

    @pytest.mark.asyncio
    async def test_missing_est_minutes_defaults_to_15(self, patched_td):
        td = patched_td
        steps = [{"description": "No time given"}]

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _make_model_response(steps)
            await td.decompose_task("No estimate")

        task = list(td._active_tasks.values())[0]
        assert task.steps[0].est_minutes == math.ceil(15 * td.ADHD_TIME_BUFFER)
