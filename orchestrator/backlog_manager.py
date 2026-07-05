"""
Task backlog — a durable to-do list.

Distinct from task_decomposition.py (which breaks ONE active task into
in-memory micro-steps and is lost on restart): this is the persistent list of
things the user wants to do over time. Capture-first (low friction), no-guilt
removal, and — per the one-thing-at-a-time principle — the surfacing path
(`pick_next`) returns a SINGLE task, not a list, to avoid choice paralysis.

Storage: state_store `tasks` table. Fuzzy matching (rapidfuzz) lets the user
complete/drop a task by describing it ("the dentist thing") instead of an id.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from rapidfuzz import fuzz, process

from orchestrator import state_store
from orchestrator.metrics import TASKS_COMPLETED, TASKS_CREATED, TASKS_DROPPED, TASKS_OPEN

logger = logging.getLogger(__name__)

MAX_TASK_TEXT_LENGTH = 500
# A fuzzy ref must clear this to count as a confident match.
_FUZZY_MATCH_THRESHOLD = 70
# If the 2nd-best match is within this of the best, it's ambiguous — ask.
_FUZZY_AMBIGUITY_MARGIN = 8

_PRIORITY_ALIASES = {
    "high": "high",
    "urgent": "high",
    "important": "high",
    "asap": "high",
    "normal": "normal",
    "medium": "normal",
    "med": "normal",
    "": "normal",
    "low": "low",
    "someday": "low",
    "later": "low",
    "whenever": "low",
}


def _norm_priority(p: str) -> str:
    return _PRIORITY_ALIASES.get((p or "").strip().lower(), "normal")


def _sync_open_gauge() -> None:
    try:
        TASKS_OPEN.set(state_store.open_task_count())
    except Exception as e:  # noqa: BLE001 — metrics must never break the tool
        logger.warning("Failed to sync open-task gauge: %s", e)


def create(
    text: str,
    *,
    priority: str = "normal",
    source: str = "chat",
    notes: Optional[str] = None,
    due_date: Optional[str] = None,
) -> Optional[dict]:
    """Persist a new task (store + metrics + gauge). Returns the task dict, or
    None if the text was empty. Shared by the voice tool and the REST route."""
    text = (text or "").strip()
    if not text:
        return None
    if len(text) > MAX_TASK_TEXT_LENGTH:
        text = text[:MAX_TASK_TEXT_LENGTH].rstrip()

    prio = _norm_priority(priority)
    task_id = str(uuid.uuid4())[:8]
    state_store.add_task(task_id, text, priority=prio, source=source, notes=notes, due_date=due_date)
    TASKS_CREATED.labels(source=source).inc()
    _sync_open_gauge()
    logger.info("[BACKLOG] Added task %s (%s): %s", task_id, prio, text[:60])
    return state_store.get_task(task_id)


def complete_by_id(task_id: str) -> bool:
    """Mark a task done by id (store + metrics + gauge). Shared by tool + REST."""
    if state_store.complete_task(task_id):
        TASKS_COMPLETED.inc()
        _sync_open_gauge()
        logger.info("[BACKLOG] Completed task %s", task_id)
        return True
    return False


def drop_by_id(task_id: str) -> bool:
    """Drop a task by id (store + metrics + gauge). Shared by tool + REST."""
    if state_store.drop_task(task_id):
        TASKS_DROPPED.inc()
        _sync_open_gauge()
        logger.info("[BACKLOG] Dropped task %s", task_id)
        return True
    return False


def add_task(
    text: str,
    *,
    priority: str = "normal",
    source: str = "chat",
    notes: Optional[str] = None,
    due_date: Optional[str] = None,
) -> str:
    """Add a task to the backlog. Returns a short TTS-friendly confirmation."""
    task = create(text, priority=priority, source=source, notes=notes, due_date=due_date)
    if task is None:
        return "What's the task? Tell me what to add and I'll put it on your list."

    open_n = state_store.open_task_count()
    tail = "" if open_n <= 1 else f" ({open_n} on your list now)"
    prio_note = " — flagged high" if task["priority"] == "high" else ""
    return f"Got it, added to your list{prio_note}: {task['text']}.{tail}"


def list_open(limit: int = 12) -> str:
    """Show open tasks (high/oldest first). Bounded so it never becomes a wall."""
    tasks = state_store.list_tasks("open")
    if not tasks:
        return "Your list is clear — nothing on the backlog."
    lines = [f"You have {len(tasks)} thing{'s' if len(tasks) != 1 else ''} on your list:"]
    for t in tasks[:limit]:
        flag = "❗" if t["priority"] == "high" else ("· " if t["priority"] == "normal" else "◦ ")
        lines.append(f"{flag}{t['text']}")
    if len(tasks) > limit:
        lines.append(f"…and {len(tasks) - limit} more.")
    return "\n".join(lines)


def pick_next() -> str:
    """Surface ONE task to do now — the anti-choice-paralysis path."""
    tasks = state_store.list_tasks("open")
    if not tasks:
        return "Your list is clear — nothing you need to pick up right now. Nice."
    top = tasks[0]
    rest = len(tasks) - 1
    tail = "" if rest == 0 else f" The other {rest} can wait."
    lead = "Start here" if top["priority"] != "high" else "This one first"
    return f"{lead}: {top['text']}.{tail}"


def _resolve(ref: str) -> tuple[Optional[dict], Optional[str]]:
    """Resolve a user ref (task id or fuzzy text) to a single open task.

    Returns (task, error_message). Exactly one is non-None.
    """
    ref = (ref or "").strip()
    if not ref:
        return None, "Which task? Name it or describe it."
    tasks = state_store.list_tasks("open")
    if not tasks:
        return None, "Your list is already clear — nothing to update."

    # Exact id first.
    by_id = state_store.get_task(ref)
    if by_id and by_id["status"] == "open":
        return by_id, None

    # Fuzzy match against the open task texts.
    choices = {t["id"]: t["text"] for t in tasks}
    matches = process.extract(ref, choices, scorer=fuzz.WRatio, limit=2)
    if not matches or matches[0][1] < _FUZZY_MATCH_THRESHOLD:
        return None, f"I couldn't find a task matching '{ref}'. Try 'what's on my list' to see them."
    best_text, best_score, best_id = matches[0]
    if len(matches) > 1 and (best_score - matches[1][1]) < _FUZZY_AMBIGUITY_MARGIN:
        return None, (f"That could be a couple of things: '{best_text}' or '{matches[1][0]}'. Which one?")
    return state_store.get_task(best_id), None


def match_open(text: str) -> Optional[dict]:
    """Return the open backlog task that confidently matches `text`, else None.

    Non-interactive (no ambiguity prompt) — used to auto-link a decomposition to
    the backlog task it's breaking down.
    """
    task, err = _resolve(text)
    return task if err is None else None


def complete(ref: str) -> str:
    """Mark a task done by id or fuzzy description. No-guilt, celebratory."""
    task, err = _resolve(ref)
    if err:
        return err
    if complete_by_id(task["id"]):
        left = state_store.open_task_count()
        tail = " That's your list clear! 🎉" if left == 0 else f" {left} left."
        return f"Done: {task['text']}.{tail}"
    return f"Looks like '{task['text']}' was already off your list."


def drop(ref: str) -> str:
    """Drop a task (no guilt) by id or fuzzy description."""
    task, err = _resolve(ref)
    if err:
        return err
    if drop_by_id(task["id"]):
        return f"Dropped '{task['text']}' — no worries, it's off your list."
    return f"'{task['text']}' wasn't on your active list."


def backlog_context() -> str:
    """Compact open-task summary for the system prompt (so the model knows the
    backlog without a tool call). Empty string when the list is clear."""
    tasks = state_store.list_tasks("open")
    if not tasks:
        return ""
    top = ", ".join(t["text"] for t in tasks[:5])
    more = f" (+{len(tasks) - 5} more)" if len(tasks) > 5 else ""
    return f"Open tasks ({len(tasks)}): {top}{more}"


def weekly_review_summary() -> Optional[str]:
    """TTS-friendly weekly backlog nudge (keeps the list from becoming a
    graveyard — the ADHD failure mode where captured tasks quietly rot).
    Returns None when the list is empty (nothing to say)."""
    from datetime import datetime

    tasks = state_store.list_tasks("open")
    if not tasks:
        return None
    n = len(tasks)
    high = sum(1 for t in tasks if t["priority"] == "high")
    oldest = min(tasks, key=lambda t: t["created_at"])
    try:
        age_days = (datetime.now() - datetime.fromisoformat(oldest["created_at"])).days
    except (ValueError, TypeError):
        age_days = 0

    lead = f"Weekly check-in: you've got {n} thing{'s' if n != 1 else ''} on your list"
    hi = f", {high} flagged high" if high else ""
    age = ""
    if age_days >= 3:
        age = f" The oldest — '{oldest['text']}' — has been there {age_days} day{'s' if age_days != 1 else ''}."
    return f"{lead}{hi}.{age} Want to knock one out, or clear anything that's gone stale?"
