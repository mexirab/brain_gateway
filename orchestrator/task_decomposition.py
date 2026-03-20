"""
Task Decomposition Engine (F-003): breaks big tasks into ADHD-friendly micro-steps.

The model does the decomposition — this module captures the structured result,
tracks progress through steps, and builds TTS-friendly responses.
"""

import json
import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import shared
from metrics import (
    TASK_DECOMP_ERRORS,
    TASK_DECOMP_STEPS_COMPLETED,
    TASK_DECOMP_STEPS_SKIPPED,
    TASK_DECOMP_TASKS_ABANDONED,
    TASK_DECOMP_TASKS_CREATED,
)

logger = logging.getLogger(__name__)

# ADHD time buffer — model estimates multiplied by this factor
ADHD_TIME_BUFFER = 1.5

# Resource caps
MAX_ACTIVE_TASKS = 10
MAX_STEPS_PER_TASK = 20
MAX_TASK_TEXT_LENGTH = 1000


@dataclass
class MicroStep:
    index: int
    description: str  # concrete, actionable text
    est_minutes: int  # with ADHD buffer applied
    completed: bool = False
    skipped: bool = False


@dataclass
class DecomposedTask:
    task_id: str
    original_text: str
    steps: List[MicroStep]
    current_step_index: int = 0
    created_at: datetime = field(default_factory=datetime.now)
    mode: str = "next_step_only"  # "full_list" or "next_step_only"


# Module state — in-memory only, no persistence
_active_tasks: Dict[str, DecomposedTask] = {}


async def decompose_task(task_text: str, mode: str = "next_step_only", context: str = "") -> str:
    """Use the unified model to break a task into micro-steps.

    Calls the LLM with a tight prompt to produce structured steps,
    then stores the result and returns a TTS-friendly summary.
    """
    from orchestrator import call_model

    if not task_text:
        return "Please tell me what task you'd like me to break down."

    # Sanitize input
    task_text = task_text[:MAX_TASK_TEXT_LENGTH]
    context = context[:MAX_TASK_TEXT_LENGTH] if context else ""

    system_prompt = """You are a task decomposition assistant. Break the given task into concrete micro-steps.

Rules:
- Each step must be a single concrete action (start with a verb)
- Keep steps small enough to complete in 5-30 minutes each
- Estimate minutes for each step (be realistic, not optimistic)
- Return ONLY valid JSON — no markdown, no explanation, no extra text

Return format:
[{"description": "Step description starting with a verb", "est_minutes": 15}, ...]"""

    user_msg = f"Break down this task into micro-steps: {task_text}"
    if context:
        user_msg += f"\n\nContext: {context}"

    messages = [{"role": "user", "content": user_msg}]

    try:
        response = await call_model(
            shared.MODEL_URL,
            shared.MODEL_NAME,
            messages,
            system=system_prompt,
            timeout=60,
        )

        content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
        # Strip markdown code fences if present
        content = content.strip()
        if content.startswith("```"):
            first_newline = content.find("\n")
            content = content[first_newline + 1 :] if first_newline != -1 else content[3:]
        if content.endswith("```"):
            content = content[:-3].rstrip()
        content = content.strip()

        raw_steps = json.loads(content)
        if not isinstance(raw_steps, list):
            raise ValueError(f"Expected list, got {type(raw_steps).__name__}")
    except json.JSONDecodeError:
        TASK_DECOMP_ERRORS.inc()
        logger.warning("[TASK_DECOMP] Failed to parse model response as JSON: %s", content[:200])
        # Fallback: create a single step from the task
        raw_steps = [{"description": task_text, "est_minutes": 30}]
    except Exception as e:
        TASK_DECOMP_ERRORS.inc()
        logger.error("[TASK_DECOMP] Model call failed: %s", e)
        return "Sorry, I couldn't break that task down right now. Try again in a moment."

    # Build micro-steps with ADHD time buffer
    steps = []
    for i, raw in enumerate(raw_steps[:MAX_STEPS_PER_TASK]):
        if not isinstance(raw, dict):
            continue
        desc = raw.get("description", "")
        if not isinstance(desc, str) or not desc.strip():
            continue
        desc = desc.strip()
        try:
            raw_minutes = max(1, min(int(raw.get("est_minutes", 15)), 240))
        except (ValueError, TypeError):
            raw_minutes = 15
        buffered_minutes = math.ceil(raw_minutes * ADHD_TIME_BUFFER)
        steps.append(MicroStep(index=i, description=desc, est_minutes=buffered_minutes))

    if not steps:
        return "I couldn't generate steps for that task. Could you describe it differently?"

    # Evict oldest task if at cap
    if len(_active_tasks) >= MAX_ACTIVE_TASKS:
        oldest_id = next(iter(_active_tasks))
        _active_tasks.pop(oldest_id)
        logger.warning("[TASK_DECOMP] Active task cap reached, evicted oldest task %s", oldest_id)

    task_id = str(uuid.uuid4())[:8]
    task = DecomposedTask(
        task_id=task_id,
        original_text=task_text,
        steps=steps,
        mode=mode,
    )
    _active_tasks[task_id] = task
    TASK_DECOMP_TASKS_CREATED.inc()

    logger.info(
        "[TASK_DECOMP] Created task %s with %d steps (mode=%s)",
        task_id,
        len(steps),
        mode,
        extra={"component": "task_decomp"},
    )

    return _format_task_response(task)


def get_next_step(task_id: str) -> str:
    """Return only the next uncompleted step."""
    task = _active_tasks.get(task_id)
    if not task:
        return f"No active task found with ID {task_id}."

    step = _find_next_step(task)
    if not step:
        return _format_completion_summary(task)

    total = len(task.steps)
    return f"Step {step.index + 1} of {total}: {step.description} (about {step.est_minutes} min)"


def complete_step(task_id: str) -> str:
    """Mark the current step done, return next step or completion summary."""
    task = _active_tasks.get(task_id)
    if not task:
        return f"No active task found with ID {task_id}."

    step = _find_next_step(task)
    if not step:
        return _format_completion_summary(task)

    step.completed = True
    task.current_step_index = step.index + 1
    TASK_DECOMP_STEPS_COMPLETED.inc()

    # Record task step completion (F-005)
    try:
        import progress_tracker

        progress_tracker.record_event("task_done", {})
    except Exception as e:
        logger.warning(f"[TASK_DECOMP] Progress tracking failed: {e}")

    logger.info(
        "[TASK_DECOMP] Task %s step %d completed",
        task_id,
        step.index + 1,
        extra={"component": "task_decomp"},
    )

    next_step = _find_next_step(task)
    if not next_step:
        return _format_completion_summary(task)

    total = len(task.steps)
    completed_count = sum(1 for s in task.steps if s.completed)
    return (
        f"Nice, step {step.index + 1} done! "
        f"Next up — step {next_step.index + 1} of {total}: {next_step.description} "
        f"(about {next_step.est_minutes} min). "
        f"{completed_count} of {total} steps complete so far."
    )


def skip_step(task_id: str) -> str:
    """Skip the current step without marking it complete."""
    task = _active_tasks.get(task_id)
    if not task:
        return f"No active task found with ID {task_id}."

    step = _find_next_step(task)
    if not step:
        return _format_completion_summary(task)

    step.skipped = True
    task.current_step_index = step.index + 1
    TASK_DECOMP_STEPS_SKIPPED.inc()

    logger.info(
        "[TASK_DECOMP] Task %s step %d skipped",
        task_id,
        step.index + 1,
        extra={"component": "task_decomp"},
    )

    next_step = _find_next_step(task)
    if not next_step:
        return _format_completion_summary(task)

    total = len(task.steps)
    return (
        f"Skipped step {step.index + 1}. "
        f"Next — step {next_step.index + 1} of {total}: {next_step.description} "
        f"(about {next_step.est_minutes} min)."
    )


def abandon_task(task_id: str) -> str:
    """Stop tracking a task. No guilt messaging."""
    task = _active_tasks.pop(task_id, None)
    if not task:
        return f"No active task found with ID {task_id}."

    TASK_DECOMP_TASKS_ABANDONED.inc()
    completed_count = sum(1 for s in task.steps if s.completed)

    logger.info(
        "[TASK_DECOMP] Task %s abandoned (%d/%d steps done)",
        task_id,
        completed_count,
        len(task.steps),
        extra={"component": "task_decomp"},
    )

    if completed_count > 0:
        return f"Stopped tracking '{task.original_text}'. You got {completed_count} of {len(task.steps)} steps done."
    return f"Stopped tracking '{task.original_text}'."


def list_active_tasks() -> str:
    """Return all in-progress decomposed tasks as a TTS-friendly string."""
    if not _active_tasks:
        return "No active decomposed tasks right now."

    lines = [f"You have {len(_active_tasks)} active task(s):"]
    for task in _active_tasks.values():
        completed = sum(1 for s in task.steps if s.completed)
        total = len(task.steps)
        next_step = _find_next_step(task)
        status = f"{completed}/{total} steps done"
        lines.append(f"\n- {task.original_text} ({status}, ID: {task.task_id})")
        if next_step:
            lines.append(f"  Next: {next_step.description}")

    return "\n".join(lines)


def get_active_tasks_context() -> str:
    """Return a short context block for injection into the system prompt.

    Called by prompt_builder to let the model know about active tasks.
    """
    if not _active_tasks:
        return ""

    lines = ["ACTIVE DECOMPOSED TASKS:"]
    for task in _active_tasks.values():
        completed = sum(1 for s in task.steps if s.completed)
        total = len(task.steps)
        next_step = _find_next_step(task)
        lines.append(f'- "{task.original_text}" (ID: {task.task_id}, {completed}/{total} done)')
        if next_step:
            lines.append(f"  Next step: {next_step.description}")

    return "\n".join(lines)


# -- Internal helpers ----------------------------------------------------------


def _find_next_step(task: DecomposedTask) -> Optional[MicroStep]:
    """Find the next step that is neither completed nor skipped."""
    for step in task.steps:
        if not step.completed and not step.skipped:
            return step
    return None


def _format_task_response(task: DecomposedTask) -> str:
    """Format the initial decomposition response based on mode."""
    total_minutes = sum(s.est_minutes for s in task.steps)

    if task.mode == "full_list":
        lines = [
            f"Here's the plan for '{task.original_text}' ({len(task.steps)} steps, about {total_minutes} min total):"
        ]
        for step in task.steps:
            lines.append(f"{step.index + 1}. {step.description} — {step.est_minutes} min")
        lines.append(f"\nTask ID: {task.task_id}")
        lines.append("Say 'done' to advance through steps, or 'what's next' to see the current step.")
        return "\n".join(lines)
    else:
        # next_step_only mode
        first = task.steps[0]
        return (
            f"I've broken '{task.original_text}' into {len(task.steps)} steps "
            f"(about {total_minutes} min total). "
            f"Here's your first step: {first.description} (about {first.est_minutes} min). "
            f"Task ID: {task.task_id}. "
            f"Say 'done' when you finish this step."
        )


def _format_completion_summary(task: DecomposedTask) -> str:
    """Build a summary when all steps are done or skipped."""
    completed = sum(1 for s in task.steps if s.completed)
    skipped = sum(1 for s in task.steps if s.skipped)
    total = len(task.steps)

    # Clean up completed task
    _active_tasks.pop(task.task_id, None)

    # Check for streak milestones (F-005)
    try:
        import asyncio

        import progress_tracker

        asyncio.ensure_future(progress_tracker.check_and_announce_streaks())
    except Exception:
        pass

    parts = [f"All done with '{task.original_text}'!"]
    parts.append(f"{completed} of {total} steps completed")
    if skipped:
        parts.append(f"({skipped} skipped)")
    return " — ".join(parts) + "."
