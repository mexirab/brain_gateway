"""
Tests for context_tracker.py (F-007) — context recording, bookmarking,
recall, check-in scheduling, and prompt injection.

Requires full orchestrator dependencies (runs inside Docker).
"""

from unittest.mock import AsyncMock, patch

import pytest


def _can_import():
    try:
        from orchestrator import context_tracker  # noqa: F401

        return True
    except (ImportError, ModuleNotFoundError):
        return False


_skip_no_deps = pytest.mark.skipif(
    not _can_import(),
    reason="context_tracker requires full orchestrator dependencies",
)


@pytest.fixture(autouse=True)
def reset_state():
    """Reset context_tracker module state between tests."""
    if not _can_import():
        pytest.skip("deps unavailable")

    from orchestrator import context_tracker

    context_tracker._context_stack.clear()
    context_tracker._interrupted = False
    context_tracker._interrupt_bookmark = None
    context_tracker._checkin_job_id = None

    with (
        patch.object(context_tracker, "_announce_voice", new_callable=AsyncMock),
        patch.object(context_tracker, "shared") as mock_shared,
    ):
        mock_shared.current_focus_session = {"active": False}
        mock_shared.INTERRUPT_CHECKIN_DELAY = 5
        mock_shared.CONTEXT_STACK_SIZE = 10
        mock_shared.scheduler = patch("orchestrator.context_tracker.shared.scheduler").start()
        mock_shared.profile.user_name = "Nadim"

        yield {"module": context_tracker, "mock_shared": mock_shared}

    context_tracker._context_stack.clear()
    context_tracker._interrupted = False
    context_tracker._interrupt_bookmark = None
    context_tracker._checkin_job_id = None
    patch.stopall()


@pytest.fixture
def ct(reset_state):
    return reset_state["module"]


@pytest.fixture
def mock_shared(reset_state):
    return reset_state["mock_shared"]


# ---------------------------------------------------------------------------
# Passive recording
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestRecordContext:
    @pytest.mark.asyncio
    async def test_record_adds_to_stack(self, ct):
        await ct.record_context("Working on OAuth flow")
        assert len(ct._context_stack) == 1
        assert ct._context_stack[0].description == "Working on OAuth flow"

    @pytest.mark.asyncio
    async def test_record_with_detail(self, ct):
        await ct.record_context("OAuth flow", detail="token refresh", task_id="t1")
        assert ct._context_stack[0].detail == "token refresh"
        assert ct._context_stack[0].task_id == "t1"

    @pytest.mark.asyncio
    async def test_stack_rolls_over(self, ct):
        for i in range(15):
            await ct.record_context(f"Task {i}")
        # maxlen is set at module import time, may be 10
        assert len(ct._context_stack) <= 10


# ---------------------------------------------------------------------------
# Explicit bookmark
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestBookmarkContext:
    @pytest.mark.asyncio
    async def test_bookmark_with_description(self, ct, mock_shared):
        result = await ct.bookmark_context("PR review")
        assert result["description"] == "PR review"
        assert result["checkin_delay"] == 5
        assert ct._interrupted is True
        assert ct._interrupt_bookmark is not None
        mock_shared.scheduler.add_job.assert_called_once()

    @pytest.mark.asyncio
    async def test_bookmark_auto_fills_from_focus(self, ct, mock_shared):
        mock_shared.current_focus_session = {
            "active": True,
            "task": "Dashboard UI",
            "task_description": "Dashboard UI",
            "job_id": "j1",
        }
        result = await ct.bookmark_context()
        assert result["description"] == "Dashboard UI"

    @pytest.mark.asyncio
    async def test_bookmark_auto_fills_from_stack(self, ct):
        await ct.record_context("Email draft")
        result = await ct.bookmark_context()
        assert result["description"] == "Email draft"

    @pytest.mark.asyncio
    async def test_bookmark_default_description(self, ct):
        result = await ct.bookmark_context()
        assert result["description"] == "what you were working on"

    @pytest.mark.asyncio
    async def test_multiple_bookmarks_replace_checkin(self, ct, mock_shared):
        await ct.bookmark_context("Task A")
        await ct.bookmark_context("Task B")
        assert ct._interrupt_bookmark.description == "Task B"
        # Should have cancelled the first job
        assert mock_shared.scheduler.remove_job.call_count >= 1


# ---------------------------------------------------------------------------
# Recall
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestRecallContext:
    @pytest.mark.asyncio
    async def test_recall_empty(self, ct):
        result = await ct.get_recent_context()
        assert result == []

    @pytest.mark.asyncio
    async def test_recall_returns_most_recent_first(self, ct):
        await ct.record_context("Task A")
        await ct.record_context("Task B")
        await ct.record_context("Task C")
        result = await ct.get_recent_context(3)
        assert result[0]["description"] == "Task C"
        assert result[2]["description"] == "Task A"

    @pytest.mark.asyncio
    async def test_recall_respects_count(self, ct):
        for i in range(5):
            await ct.record_context(f"Task {i}")
        result = await ct.get_recent_context(2)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_recall_has_when_field(self, ct):
        await ct.record_context("Test task")
        result = await ct.get_recent_context(1)
        assert "when" in result[0]
        assert result[0]["when"] == "just now"


# ---------------------------------------------------------------------------
# Prompt context
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestPromptContext:
    def test_no_context_returns_empty(self, ct):
        assert ct.get_active_context_summary() == ""

    @pytest.mark.asyncio
    async def test_interrupted_context(self, ct):
        await ct.bookmark_context("OAuth flow")
        summary = ct.get_active_context_summary()
        assert "INTERRUPTED" in summary
        assert "OAuth flow" in summary

    @pytest.mark.asyncio
    async def test_resumed_context_returns_empty(self, ct):
        await ct.bookmark_context("OAuth flow")
        ct.mark_resumed()
        assert ct.get_active_context_summary() == ""


# ---------------------------------------------------------------------------
# Mark resumed
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestMarkResumed:
    @pytest.mark.asyncio
    async def test_mark_resumed(self, ct):
        await ct.bookmark_context("Task X")
        assert ct._interrupted is True
        ct.mark_resumed()
        assert ct._interrupted is False
        assert ct._interrupt_bookmark.resumed is True
