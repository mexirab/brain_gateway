"""
Interruption Recovery (F-007): context stack and bookmark management.

Tracks what the user was doing so Jess can help them re-orient after
interruptions. Acts as external working memory for ADHD support.
"""

import contextlib
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from orchestrator import shared
from orchestrator.reminder_manager import _announce_voice
from orchestrator.shared import profile

logger = logging.getLogger(__name__)


@dataclass
class ContextBookmark:
    description: str
    detail: Optional[str] = None
    task_id: Optional[str] = None
    focus_session_id: Optional[str] = None
    bookmarked_at: datetime = field(default_factory=datetime.now)
    resumed: bool = False


# Module state
_context_stack: deque = deque(maxlen=shared.CONTEXT_STACK_SIZE)
_interrupted: bool = False
_interrupt_bookmark: Optional[ContextBookmark] = None
_checkin_job_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Passive context recording
# ---------------------------------------------------------------------------


async def record_context(
    description: str,
    detail: Optional[str] = None,
    task_id: Optional[str] = None,
    focus_session_id: Optional[str] = None,
) -> None:
    """Passively record context from focus start, task advance, or routine start."""
    bookmark = ContextBookmark(
        description=description,
        detail=detail,
        task_id=task_id,
        focus_session_id=focus_session_id,
    )
    _context_stack.append(bookmark)
    logger.debug("[CONTEXT] Recorded: '%s'", description[:60])


# ---------------------------------------------------------------------------
# Explicit bookmark (tool handler)
# ---------------------------------------------------------------------------


async def bookmark_context(description: Optional[str] = None) -> Dict[str, Any]:
    """Explicitly bookmark current context for an interruption.

    Auto-fills from active focus session or decomposed task if no description.
    Returns a dict with 'description' and 'checkin_delay' for the tool handler.
    """
    global _interrupted, _interrupt_bookmark, _checkin_job_id

    auto_desc = _infer_current_context()
    effective_desc = description or auto_desc or "what you were working on"

    focus_id = None
    if shared.current_focus_session.get("active"):
        focus_id = shared.current_focus_session.get("job_id")

    task_id = None
    if _context_stack:
        task_id = _context_stack[-1].task_id

    bm = ContextBookmark(
        description=effective_desc,
        detail=auto_desc if description else None,
        task_id=task_id,
        focus_session_id=focus_id,
    )
    _context_stack.append(bm)
    _interrupt_bookmark = bm
    _interrupted = True

    # Cancel any existing check-in
    if _checkin_job_id:
        with contextlib.suppress(Exception):
            shared.scheduler.remove_job(_checkin_job_id)

    # Schedule return check-in
    delay = shared.INTERRUPT_CHECKIN_DELAY
    run_at = datetime.now() + timedelta(minutes=delay)
    _checkin_job_id = f"interrupt_checkin_{datetime.now().strftime('%H%M%S')}"
    shared.scheduler.add_job(
        check_in_after_interrupt,
        trigger="date",
        run_date=run_at,
        id=_checkin_job_id,
        replace_existing=True,
    )
    logger.info("[CONTEXT] Bookmarked: '%s', check-in in %dm", effective_desc, delay)

    return {"description": effective_desc, "checkin_delay": delay}


# ---------------------------------------------------------------------------
# Post-interruption check-in
# ---------------------------------------------------------------------------


async def check_in_after_interrupt() -> None:
    """APScheduler job: TTS resume prompt after interruption delay."""
    global _checkin_job_id
    _checkin_job_id = None

    if not _interrupted or _interrupt_bookmark is None:
        return
    if _interrupt_bookmark.resumed:
        return

    desc = _interrupt_bookmark.description
    message = (
        f"Welcome back, {profile.user_name}. "
        f"You were working on {desc}. "
        f"Want to jump back in? I can start a focus session."
    )
    logger.info("[CONTEXT] Check-in: '%s'", desc[:60])
    await _announce_voice(message, announcement_type="interrupt")


def mark_resumed() -> None:
    """Mark the active interrupt bookmark as resumed."""
    global _interrupted
    if _interrupt_bookmark:
        _interrupt_bookmark.resumed = True
    _interrupted = False


# ---------------------------------------------------------------------------
# Context recall (tool handler)
# ---------------------------------------------------------------------------


async def get_recent_context(count: int = 3) -> List[Dict[str, Any]]:
    """Return last N context entries for 'what was I working on?' queries."""
    entries = list(_context_stack)[-count:]
    entries.reverse()  # most recent first
    result = []
    for bm in entries:
        elapsed = (datetime.now() - bm.bookmarked_at).total_seconds()
        if elapsed < 120:
            when = "just now"
        elif elapsed < 3600:
            when = f"{int(elapsed / 60)} minutes ago"
        elif elapsed < 7200:
            when = "about an hour ago"
        else:
            when = f"{int(elapsed / 3600)} hours ago"
        result.append({"description": bm.description, "when": when, "resumed": bm.resumed})
    return result


# ---------------------------------------------------------------------------
# Prompt context injection
# ---------------------------------------------------------------------------


def get_active_context_summary() -> str:
    """Short context string for system prompt injection."""
    if not _context_stack:
        return ""
    if _interrupted and _interrupt_bookmark and not _interrupt_bookmark.resumed:
        return (
            f"INTERRUPTED CONTEXT: User was working on '{_interrupt_bookmark.description}' "
            f"before an interruption. They may need help re-orienting."
        )
    return ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _infer_current_context() -> Optional[str]:
    """Infer current context from active focus session or most recent stack entry."""
    if shared.current_focus_session.get("active"):
        task = shared.current_focus_session.get("task_description") or shared.current_focus_session.get("task")
        if task:
            return task

    if _context_stack:
        return _context_stack[-1].description

    return None
