"""
Tests for brain_dump_manager.py — item routing, dedup, input validation,
TTS confirmation, error handling, and max-items cap.

Mocks external dependencies: ChromaDB collection, embedding_model, the
reminder path and backlog_manager.create (so a dump never writes the
session-wide state DB).
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from orchestrator import brain_dump_manager

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def bd():
    """(brain_dump_manager, mock Chroma collection, mock embedding model).

    The collection reports no existing documents (no duplicates). The
    backlog store and progress events are mocked, so a dump never writes the
    session-wide state / progress DBs; ``backlog_manager.create`` returns a
    task (override it to test the failure path)."""
    coll = MagicMock()
    coll.query.return_value = {"documents": [[]], "distances": [[]]}
    emb = MagicMock()
    emb.encode.return_value = np.zeros(768)
    with (
        patch.multiple(
            brain_dump_manager,
            collection=coll,
            embedding_model=emb,
            BRAIN_DUMP_ITEMS_CAPTURED=MagicMock(),
            BRAIN_DUMP_ITEMS_ROUTED=MagicMock(),
            BRAIN_DUMP_RAG_LATENCY=MagicMock(),
            BRAIN_DUMP_DUPLICATES_SKIPPED=MagicMock(),
            BRAIN_DUMP_ERRORS=MagicMock(),
        ),
        patch("orchestrator.backlog_manager.create", return_value={"id": "t1"}),
        patch("orchestrator.progress_tracker.record_event"),
        patch("orchestrator.progress_tracker.check_and_announce_streaks", new_callable=AsyncMock),
    ):
        yield brain_dump_manager, coll, emb


def _mock_reminder():
    return patch(
        "orchestrator.brain_dump_manager._route_to_reminder",
        new_callable=AsyncMock,
        return_value="added as a reminder",
    )


def _upserted(coll):
    """kwargs of the (single) collection.upsert call."""
    coll.upsert.assert_called_once()
    return coll.upsert.call_args.kwargs


# ---------------------------------------------------------------------------
# Routing: reminder / backlog / memory
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("category", "urgency", "route", "priority"),
    [
        pytest.param("reminder", None, "reminder", None, id="reminder"),
        pytest.param("task", "now", "reminder", None, id="task-now"),
        pytest.param("errand", "today", "reminder", None, id="errand-today"),
        # A non-urgent actionable item is a durable to-do: backlog, not a timed
        # reminder and not RAG (where it would rot un-actionable).
        pytest.param("task", "someday", "backlog", "low", id="task-someday"),
        pytest.param("task", "soon", "backlog", "high", id="task-soon"),
        pytest.param("idea", None, "memory", None, id="idea"),
        pytest.param("preference", None, "memory", None, id="preference"),
    ],
)
async def test_routing(bd, category, urgency, route, priority):
    bdm, coll, _ = bd
    item = {"text": "do the thing", "category": category}
    if urgency:
        item["urgency"] = urgency

    with (
        _mock_reminder() as mock_rem,
        patch("orchestrator.backlog_manager.create", return_value={"id": "t1"}) as create,
    ):
        result = await bdm.process_brain_dump([item])

    assert result.items[0].category == category
    assert mock_rem.called is (route == "reminder")
    assert coll.upsert.called is (route == "memory")
    if route == "backlog":
        assert result.items[0].routed_to == "backlog"
        create.assert_called_once_with("do the thing", priority=priority, source="brain_dump")
    else:
        create.assert_not_called()
    if route == "memory":
        assert result.items[0].routed_to == "memory"


async def test_backlog_create_failure_falls_back_to_rag(bd):
    """If the backlog store rejects the item (create returns None), the item
    must not be silently lost — it falls back to RAG so it's at least kept."""
    bdm, coll, _ = bd
    with _mock_reminder(), patch("orchestrator.backlog_manager.create", return_value=None):
        result = await bdm.process_brain_dump([{"text": "sort the garage", "category": "task", "urgency": "someday"}])

    coll.upsert.assert_called_once()
    assert result.items[0].routed_to == "memory"


async def test_route_to_reminder_full_flow(bd):
    """_route_to_reminder with mocked reminder_manager and scheduler."""
    bdm, *_ = bd
    trigger = datetime.now() + timedelta(minutes=5)

    with (
        patch("orchestrator.reminder_manager.add_reminder") as mock_add,
        patch("orchestrator.reminder_manager.parse_time_expression", return_value=(trigger, None)),
        patch("orchestrator.tool_handlers.deliver_reminder_job"),
        patch("orchestrator.shared.scheduler") as mock_sched,
    ):
        item = bdm.CapturedItem(raw_text="call dentist", category="reminder", urgency="now")
        confirmation = await bdm._route_to_reminder(item)

    assert confirmation == "added as a reminder"
    assert item.routed_to == "reminder"
    mock_add.assert_called_once()
    mock_sched.add_job.assert_called_once()


async def test_unknown_category_fallback_to_rag(bd):
    """Even if a category somehow bypasses validation, route_item falls back to RAG."""
    bdm, coll, _ = bd
    await bdm.route_item(bdm.CapturedItem(raw_text="mystery item", category="mystery"))
    coll.upsert.assert_called_once()


# ---------------------------------------------------------------------------
# RAG storage with metadata
# ---------------------------------------------------------------------------


class TestRagStorage:
    async def test_idea_stored_with_correct_metadata(self, bd):
        bdm, coll, _ = bd
        await bdm.process_brain_dump([{"text": "build a weather dashboard", "category": "idea"}])

        meta = _upserted(coll)["metadatas"][0]
        assert meta["category"] == "idea"
        assert meta["source"] == "brain_dump"
        assert meta["kind"] == "chunk"
        assert "created_at" in meta

    async def test_rag_stores_trimmed_text(self, bd):
        bdm, coll, _ = bd
        await bdm.process_brain_dump([{"text": "  extra spaces around  ", "category": "idea"}])
        assert _upserted(coll)["documents"][0] == "extra spaces around"


# ---------------------------------------------------------------------------
# TTS confirmation summary
# ---------------------------------------------------------------------------


class TestTtsConfirmation:
    async def test_empty_input_summary(self, bd):
        bdm, *_ = bd
        result = await bdm.process_brain_dump([])
        assert len(result.items) == 0
        assert result.summary == "Nothing to capture."

    async def test_single_item_summary_format(self, bd):
        bdm, *_ = bd
        result = await bdm.process_brain_dump([{"text": "buy milk", "category": "errand"}])
        assert len(result.items) == 1
        assert result.summary.startswith("Got it")
        assert "buy milk" in result.summary

    async def test_multi_item_summary_format(self, bd):
        bdm, *_ = bd
        items = [
            {"text": "research standing desks", "category": "research"},
            {"text": "I prefer oat milk in coffee", "category": "preference"},
            {"text": "clean out the garage", "category": "task"},
        ]
        result = await bdm.process_brain_dump(items)

        assert len(result.items) == 3
        assert "Captured 3 things" in result.summary
        assert "All sorted" in result.summary
        for n in ("1.", "2.", "3."):
            assert n in result.summary

    async def test_long_text_truncated_in_summary(self, bd):
        bdm, *_ = bd
        long_text = "a" * 200
        result = await bdm.process_brain_dump([{"text": long_text, "category": "idea"}])

        # The display text in summary is truncated to 100 chars + "..."
        assert "..." in result.summary
        assert long_text not in result.summary

    async def test_confirmation_includes_route_label(self, bd):
        bdm, *_ = bd
        result = await bdm.process_brain_dump([{"text": "learn rust", "category": "idea"}])
        assert "saved as an idea" in result.summary


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


class TestDuplicateDetection:
    @pytest.mark.parametrize(
        ("existing", "distance", "text", "duplicate"),
        [
            # cosine distance 0.05 = similarity 0.95
            pytest.param("I prefer oat milk", 0.05, "I prefer oat milk in my coffee", True, id="high-similarity"),
            pytest.param("I like running", 0.5, "build a standing desk", False, id="low-similarity"),
            # Low cosine similarity but a substring match
            pytest.param("I prefer oat milk", 0.5, "I prefer oat milk", True, id="substring"),
        ],
    )
    async def test_is_duplicate(self, bd, existing, distance, text, duplicate):
        bdm, coll, _ = bd
        coll.query.return_value = {"documents": [[existing]], "distances": [[distance]]}
        assert await bdm._is_duplicate(text, np.zeros(768).tolist()) is duplicate

    async def test_duplicate_skipped_in_processing(self, bd):
        bdm, coll, _ = bd
        coll.query.return_value = {"documents": [["I prefer dark mode"]], "distances": [[0.02]]}

        result = await bdm.process_brain_dump([{"text": "I prefer dark mode", "category": "preference"}])

        assert result.items[0].routed_to == "duplicate"
        assert "duplicate" in result.summary.lower() or "already saved" in result.summary.lower()
        coll.upsert.assert_not_called()

    async def test_dedup_check_failure_does_not_crash(self, bd):
        bdm, coll, _ = bd
        coll.query.side_effect = Exception("ChromaDB connection lost")
        # Should return False (not duplicate) on error, not crash
        assert await bdm._is_duplicate("some text", np.zeros(768).tolist()) is False


# ---------------------------------------------------------------------------
# Input validation + max-items cap
# ---------------------------------------------------------------------------


class TestInputValidation:
    @pytest.mark.parametrize(
        ("items", "kept"),
        [
            pytest.param(
                [{"text": "", "category": "idea"}, {"text": "valid item", "category": "idea"}],
                ["valid item"],
                id="empty",
            ),
            pytest.param(
                [
                    {"text": "   ", "category": "idea"},
                    {"text": "\t\n", "category": "task"},
                    {"text": "real item", "category": "idea"},
                ],
                ["real item"],
                id="whitespace",
            ),
            pytest.param(
                [{"category": "idea"}, {"text": "has text", "category": "idea"}], ["has text"], id="no-text-key"
            ),
        ],
    )
    async def test_textless_items_skipped(self, bd, items, kept):
        bdm, *_ = bd
        result = await bdm.process_brain_dump(items)
        assert [i.raw_text for i in result.items] == kept

    async def test_oversized_text_truncated(self, bd):
        bdm, *_ = bd
        result = await bdm.process_brain_dump([{"text": "x" * 5000, "category": "idea"}])
        assert len(result.items) == 1
        assert len(result.items[0].raw_text) == bdm.MAX_TEXT_LENGTH

    @pytest.mark.parametrize(
        "item",
        [
            pytest.param({"text": "something", "category": "banana"}, id="invalid"),
            pytest.param({"text": "random thought"}, id="missing"),
        ],
    )
    async def test_bad_category_becomes_idea(self, bd, item):
        bdm, *_ = bd
        result = await bdm.process_brain_dump([item])
        assert result.items[0].category == "idea"

    async def test_all_valid_categories_accepted(self, bd):
        bdm, *_ = bd
        with _mock_reminder():
            items = [{"text": f"item for {cat}", "category": cat} for cat in bdm.VALID_CATEGORIES]
            result = await bdm.process_brain_dump(items)

        assert {item.category for item in result.items} == bdm.VALID_CATEGORIES

    @pytest.mark.parametrize(
        ("sent", "kept"),
        [pytest.param(25, 20, id="over"), pytest.param(20, 20, id="exact"), pytest.param(5, 5, id="under")],
    )
    async def test_max_items_cap(self, bd, sent, kept):
        bdm, *_ = bd
        result = await bdm.process_brain_dump([{"text": f"item {i}", "category": "idea"} for i in range(sent)])
        assert bdm.MAX_ITEMS == 20
        assert len(result.items) == kept


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    async def test_rag_upsert_failure_does_not_crash(self, bd):
        bdm, coll, _ = bd
        coll.upsert.side_effect = Exception("Disk full")

        result = await bdm.process_brain_dump([{"text": "this should fail gracefully", "category": "idea"}])

        assert len(result.items) == 1
        assert "error" in result.summary.lower() or "could not save" in result.summary.lower()

    async def test_one_failure_doesnt_block_others(self, bd):
        bdm, coll, _ = bd
        calls = []

        def upsert_side_effect(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise Exception("First item fails")

        coll.upsert.side_effect = upsert_side_effect

        result = await bdm.process_brain_dump(
            [{"text": "will fail", "category": "idea"}, {"text": "will succeed", "category": "idea"}]
        )

        assert len(result.items) == 2
        assert coll.upsert.call_count == 2

    async def test_embedding_failure_handled(self, bd):
        bdm, _, emb = bd
        emb.encode.side_effect = RuntimeError("CUDA out of memory")

        result = await bdm.process_brain_dump([{"text": "embed will fail", "category": "idea"}])

        assert len(result.items) == 1
        assert "error" in result.summary.lower() or "could not save" in result.summary.lower()

    async def test_route_item_exception_caught_per_item(self, bd):
        """Routing failure on one item should not prevent processing of others."""
        bdm, *_ = bd
        original_route = bdm.route_item
        calls = []

        async def failing_route(item):
            calls.append(item)
            if len(calls) == 1:
                raise RuntimeError("unexpected routing error")
            return await original_route(item)

        with patch.object(bdm, "route_item", side_effect=failing_route):
            result = await bdm.process_brain_dump(
                [{"text": "first item", "category": "idea"}, {"text": "second item", "category": "idea"}]
            )

        assert len(result.items) == 2
        assert "could not save" in result.summary.lower() or "error" in result.summary.lower()


def test_captured_item_defaults():
    item = brain_dump_manager.CapturedItem(raw_text="test", category="idea")
    assert item.urgency == "someday"  # → low-priority backlog for tasks
    assert item.confidence == 1.0
    assert item.routed_to == ""
    assert item.created_at is not None
