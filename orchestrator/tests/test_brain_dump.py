"""
Tests for brain_dump_manager.py — item routing, dedup, input validation,
TTS confirmation, error handling, and max-items cap.

Mocks external dependencies: ChromaDB collection, embedding_model,
scheduler, and reminder functions.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Import check helper
# ---------------------------------------------------------------------------


def _can_import_brain_dump():
    """Check if brain_dump_manager can be imported."""
    try:
        import brain_dump_manager  # noqa: F401

        return True
    except (ImportError, ModuleNotFoundError):
        return False


_skip_no_deps = pytest.mark.skipif(
    not _can_import_brain_dump(),
    reason="brain_dump_manager requires chromadb and full orchestrator dependencies",
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_embedding_model():
    """Mock the sentence-transformer embedding model."""
    model = MagicMock()
    model.encode.return_value = np.zeros(768)
    return model


@pytest.fixture
def mock_collection():
    """Mock the ChromaDB collection."""
    coll = MagicMock()
    # Default: no existing documents (no duplicates)
    coll.query.return_value = {
        "documents": [[]],
        "distances": [[]],
    }
    coll.upsert = MagicMock()
    return coll


@pytest.fixture
def mock_scheduler():
    """Mock the APScheduler scheduler."""
    sched = MagicMock()
    sched.add_job = MagicMock()
    return sched


@pytest.fixture
def patched_brain_dump(mock_collection, mock_embedding_model, mock_scheduler):
    """Patch shared state and reminder dependencies for brain_dump_manager."""
    with (
        patch("brain_dump_manager.collection", mock_collection),
        patch("brain_dump_manager.embedding_model", mock_embedding_model),
        patch("brain_dump_manager.BRAIN_DUMP_ITEMS_CAPTURED", MagicMock()),
        patch("brain_dump_manager.BRAIN_DUMP_ITEMS_ROUTED", MagicMock()),
        patch("brain_dump_manager.BRAIN_DUMP_RAG_LATENCY", MagicMock()),
        patch("brain_dump_manager.BRAIN_DUMP_DUPLICATES_SKIPPED", MagicMock()),
        patch("brain_dump_manager.BRAIN_DUMP_ERRORS", MagicMock()),
    ):
        import brain_dump_manager

        yield brain_dump_manager, mock_collection, mock_embedding_model, mock_scheduler


# ---------------------------------------------------------------------------
# Tests: Single item brain dump
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestSingleItemDump:
    @pytest.mark.asyncio
    async def test_single_reminder_item(self, patched_brain_dump):
        bdm, coll, emb, sched = patched_brain_dump

        with (
            patch("brain_dump_manager._route_to_reminder", new_callable=AsyncMock) as mock_rem,
        ):
            mock_rem.return_value = "added as a reminder"

            result = await bdm.process_brain_dump(
                [
                    {"text": "remember to call dentist", "category": "reminder"},
                ]
            )

        assert len(result.items) == 1
        assert result.items[0].raw_text == "remember to call dentist"
        assert result.items[0].category == "reminder"
        assert result.summary.startswith("Got it")

    @pytest.mark.asyncio
    async def test_single_idea_stored_in_rag(self, patched_brain_dump):
        bdm, coll, emb, sched = patched_brain_dump

        result = await bdm.process_brain_dump(
            [
                {"text": "app idea for tracking water intake", "category": "idea"},
            ]
        )

        assert len(result.items) == 1
        assert result.items[0].category == "idea"
        coll.upsert.assert_called_once()
        call_args = coll.upsert.call_args
        metadata = call_args[1]["metadatas"][0] if "metadatas" in call_args[1] else call_args[0][1][0]
        # Verify metadata has correct category and source
        assert metadata["category"] == "idea"
        assert metadata["source"] == "brain_dump"

    @pytest.mark.asyncio
    async def test_single_item_tts_summary(self, patched_brain_dump):
        bdm, coll, emb, sched = patched_brain_dump

        result = await bdm.process_brain_dump(
            [
                {"text": "buy more coffee beans", "category": "errand"},
            ]
        )

        assert len(result.items) == 1
        # Single item summary starts with "Got it"
        assert "Got it" in result.summary


# ---------------------------------------------------------------------------
# Tests: Multi-item brain dump
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestMultiItemDump:
    @pytest.mark.asyncio
    async def test_three_items_processed(self, patched_brain_dump):
        bdm, coll, emb, sched = patched_brain_dump

        items = [
            {"text": "research standing desks", "category": "research"},
            {"text": "I prefer oat milk in coffee", "category": "preference"},
            {"text": "clean out the garage", "category": "task"},
        ]
        result = await bdm.process_brain_dump(items)

        assert len(result.items) == 3
        # Multi-item summary should say "Captured 3 things"
        assert "Captured 3 things" in result.summary
        assert "All sorted" in result.summary

    @pytest.mark.asyncio
    async def test_multi_item_numbered_summary(self, patched_brain_dump):
        bdm, coll, emb, sched = patched_brain_dump

        items = [
            {"text": "item one", "category": "idea"},
            {"text": "item two", "category": "idea"},
            {"text": "item three", "category": "idea"},
        ]
        result = await bdm.process_brain_dump(items)

        # Summary should contain numbered list
        assert "1." in result.summary
        assert "2." in result.summary
        assert "3." in result.summary


# ---------------------------------------------------------------------------
# Tests: Category classification
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestCategorization:
    @pytest.mark.asyncio
    async def test_task_category(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump
        result = await bdm.process_brain_dump([{"text": "finish report", "category": "task"}])
        assert result.items[0].category == "task"

    @pytest.mark.asyncio
    async def test_reminder_category(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump
        with patch("brain_dump_manager._route_to_reminder", new_callable=AsyncMock, return_value="added as a reminder"):
            result = await bdm.process_brain_dump([{"text": "call doctor at 3pm", "category": "reminder"}])
        assert result.items[0].category == "reminder"

    @pytest.mark.asyncio
    async def test_idea_category(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump
        result = await bdm.process_brain_dump([{"text": "build a garden bed", "category": "idea"}])
        assert result.items[0].category == "idea"

    @pytest.mark.asyncio
    async def test_preference_category(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump
        result = await bdm.process_brain_dump([{"text": "I like dark mode", "category": "preference"}])
        assert result.items[0].category == "preference"

    @pytest.mark.asyncio
    async def test_invalid_category_defaults_to_idea(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump
        result = await bdm.process_brain_dump([{"text": "some thought", "category": "nonsense"}])
        assert result.items[0].category == "idea"

    @pytest.mark.asyncio
    async def test_missing_category_defaults_to_idea(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump
        result = await bdm.process_brain_dump([{"text": "random thought"}])
        assert result.items[0].category == "idea"


# ---------------------------------------------------------------------------
# Tests: Reminder routing
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestReminderRouting:
    @pytest.mark.asyncio
    async def test_reminder_category_routes_to_reminder(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump

        with patch("brain_dump_manager._route_to_reminder", new_callable=AsyncMock) as mock_rem:
            mock_rem.return_value = "added as a reminder"
            result = await bdm.process_brain_dump(
                [
                    {"text": "take meds at noon", "category": "reminder"},
                ]
            )

        mock_rem.assert_called_once()
        assert result.items[0].category == "reminder"

    @pytest.mark.asyncio
    async def test_urgent_task_routes_to_reminder(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump

        with patch("brain_dump_manager._route_to_reminder", new_callable=AsyncMock) as mock_rem:
            mock_rem.return_value = "added as a reminder"
            result = await bdm.process_brain_dump(
                [
                    {"text": "submit expense report", "category": "task", "urgency": "now"},
                ]
            )

        mock_rem.assert_called_once()

    @pytest.mark.asyncio
    async def test_today_errand_routes_to_reminder(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump

        with patch("brain_dump_manager._route_to_reminder", new_callable=AsyncMock) as mock_rem:
            mock_rem.return_value = "added as a reminder"
            result = await bdm.process_brain_dump(
                [
                    {"text": "pick up dry cleaning", "category": "errand", "urgency": "today"},
                ]
            )

        mock_rem.assert_called_once()

    @pytest.mark.asyncio
    async def test_someday_task_goes_to_rag_not_reminder(self, patched_brain_dump):
        bdm, coll, *_ = patched_brain_dump

        with patch("brain_dump_manager._route_to_reminder", new_callable=AsyncMock) as mock_rem:
            result = await bdm.process_brain_dump(
                [
                    {"text": "reorganize bookshelf", "category": "task", "urgency": "someday"},
                ]
            )

        mock_rem.assert_not_called()
        coll.upsert.assert_called_once()

    @pytest.mark.asyncio
    async def test_route_to_reminder_full_flow(self, patched_brain_dump):
        """Test _route_to_reminder with mocked reminder_manager and scheduler."""
        bdm, *_ = patched_brain_dump

        from datetime import datetime, timedelta

        mock_trigger = datetime.now() + timedelta(minutes=5)

        with (
            patch("reminder_manager.add_reminder") as mock_add,
            patch("reminder_manager.parse_time_expression", return_value=(mock_trigger, None)),
            patch("tool_handlers.deliver_reminder_job"),
            patch("shared.scheduler") as mock_sched,
        ):
            item = bdm.CapturedItem(raw_text="call dentist", category="reminder", urgency="now")
            confirmation = await bdm._route_to_reminder(item)

        assert confirmation == "added as a reminder"
        assert item.routed_to == "reminder"
        mock_add.assert_called_once()
        mock_sched.add_job.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: RAG storage with metadata
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestRagStorage:
    @pytest.mark.asyncio
    async def test_idea_stored_with_correct_metadata(self, patched_brain_dump):
        bdm, coll, emb, _ = patched_brain_dump

        result = await bdm.process_brain_dump(
            [
                {"text": "build a weather dashboard", "category": "idea"},
            ]
        )

        coll.upsert.assert_called_once()
        call_kwargs = coll.upsert.call_args
        # Get metadatas from either positional or keyword args
        if call_kwargs[1]:
            meta = call_kwargs[1]["metadatas"][0]
        else:
            meta = call_kwargs[0][1][0]

        assert meta["category"] == "idea"
        assert meta["source"] == "brain_dump"
        assert meta["kind"] == "chunk"
        assert "created_at" in meta

    @pytest.mark.asyncio
    async def test_preference_stored_in_rag(self, patched_brain_dump):
        bdm, coll, emb, _ = patched_brain_dump

        result = await bdm.process_brain_dump(
            [
                {"text": "I prefer almond milk", "category": "preference"},
            ]
        )

        coll.upsert.assert_called_once()
        assert result.items[0].routed_to == "memory"

    @pytest.mark.asyncio
    async def test_rag_stores_trimmed_text(self, patched_brain_dump):
        bdm, coll, emb, _ = patched_brain_dump

        result = await bdm.process_brain_dump(
            [
                {"text": "  extra spaces around  ", "category": "idea"},
            ]
        )

        coll.upsert.assert_called_once()
        call_kwargs = coll.upsert.call_args
        if call_kwargs[1]:
            stored_doc = call_kwargs[1]["documents"][0]
        else:
            stored_doc = call_kwargs[0][0][0]
        assert stored_doc == "extra spaces around"


# ---------------------------------------------------------------------------
# Tests: TTS confirmation summary
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestTtsConfirmation:
    @pytest.mark.asyncio
    async def test_empty_input_summary(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump
        result = await bdm.process_brain_dump([])
        assert result.summary == "Nothing to capture."

    @pytest.mark.asyncio
    async def test_single_item_summary_format(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump
        result = await bdm.process_brain_dump([{"text": "buy milk", "category": "errand"}])
        assert result.summary.startswith("Got it")
        assert "buy milk" in result.summary

    @pytest.mark.asyncio
    async def test_multi_item_summary_format(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump
        items = [
            {"text": "item a", "category": "idea"},
            {"text": "item b", "category": "task"},
        ]
        result = await bdm.process_brain_dump(items)
        assert "Captured 2 things" in result.summary
        assert "All sorted" in result.summary

    @pytest.mark.asyncio
    async def test_long_text_truncated_in_summary(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump

        long_text = "a" * 200
        result = await bdm.process_brain_dump([{"text": long_text, "category": "idea"}])

        # The display text in summary should be truncated to 100 chars + "..."
        assert "..." in result.summary
        # Full 200-char text should NOT appear in summary
        assert long_text not in result.summary

    @pytest.mark.asyncio
    async def test_confirmation_includes_route_label(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump
        result = await bdm.process_brain_dump([{"text": "learn rust", "category": "idea"}])
        assert "saved as an idea" in result.summary


# ---------------------------------------------------------------------------
# Tests: Duplicate detection
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestDuplicateDetection:
    @pytest.mark.asyncio
    async def test_high_similarity_is_duplicate(self, patched_brain_dump):
        bdm, coll, emb, _ = patched_brain_dump

        # Simulate existing doc with high cosine similarity (distance 0.05 = sim 0.95)
        coll.query.return_value = {
            "documents": [["I prefer oat milk"]],
            "distances": [[0.05]],
        }

        embedding = np.zeros(768).tolist()
        result = await bdm._is_duplicate("I prefer oat milk in my coffee", embedding)
        assert result is True

    @pytest.mark.asyncio
    async def test_low_similarity_is_not_duplicate(self, patched_brain_dump):
        bdm, coll, emb, _ = patched_brain_dump

        coll.query.return_value = {
            "documents": [["I like running"]],
            "distances": [[0.5]],  # cosine distance 0.5 = similarity 0.5
        }

        embedding = np.zeros(768).tolist()
        result = await bdm._is_duplicate("build a standing desk", embedding)
        assert result is False

    @pytest.mark.asyncio
    async def test_substring_match_is_duplicate(self, patched_brain_dump):
        bdm, coll, emb, _ = patched_brain_dump

        # Low cosine similarity but substring match
        coll.query.return_value = {
            "documents": [["I prefer oat milk"]],
            "distances": [[0.5]],
        }

        embedding = np.zeros(768).tolist()
        result = await bdm._is_duplicate("I prefer oat milk", embedding)
        assert result is True

    @pytest.mark.asyncio
    async def test_duplicate_skipped_in_processing(self, patched_brain_dump):
        bdm, coll, emb, _ = patched_brain_dump

        # First call for dedup check returns a match
        coll.query.return_value = {
            "documents": [["I prefer dark mode"]],
            "distances": [[0.02]],  # very high similarity
        }

        result = await bdm.process_brain_dump(
            [
                {"text": "I prefer dark mode", "category": "preference"},
            ]
        )

        assert result.items[0].routed_to == "duplicate"
        assert "duplicate" in result.summary.lower() or "already saved" in result.summary.lower()
        # upsert should NOT be called for duplicates
        coll.upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_dedup_check_failure_does_not_crash(self, patched_brain_dump):
        bdm, coll, emb, _ = patched_brain_dump

        # query raises an exception
        coll.query.side_effect = Exception("ChromaDB connection lost")

        embedding = np.zeros(768).tolist()
        result = await bdm._is_duplicate("some text", embedding)
        # Should return False (not duplicate) on error, not crash
        assert result is False


# ---------------------------------------------------------------------------
# Tests: Input validation
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestInputValidation:
    @pytest.mark.asyncio
    async def test_empty_items_list(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump
        result = await bdm.process_brain_dump([])
        assert len(result.items) == 0
        assert result.summary == "Nothing to capture."

    @pytest.mark.asyncio
    async def test_empty_text_skipped(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump
        result = await bdm.process_brain_dump(
            [
                {"text": "", "category": "idea"},
                {"text": "valid item", "category": "idea"},
            ]
        )
        assert len(result.items) == 1
        assert result.items[0].raw_text == "valid item"

    @pytest.mark.asyncio
    async def test_whitespace_only_text_skipped(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump
        result = await bdm.process_brain_dump(
            [
                {"text": "   ", "category": "idea"},
                {"text": "\t\n", "category": "task"},
                {"text": "real item", "category": "idea"},
            ]
        )
        assert len(result.items) == 1
        assert result.items[0].raw_text == "real item"

    @pytest.mark.asyncio
    async def test_missing_text_key_skipped(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump
        result = await bdm.process_brain_dump(
            [
                {"category": "idea"},  # no "text" key
                {"text": "has text", "category": "idea"},
            ]
        )
        assert len(result.items) == 1

    @pytest.mark.asyncio
    async def test_oversized_text_truncated(self, patched_brain_dump):
        bdm, coll, *_ = patched_brain_dump

        long_text = "x" * 5000
        result = await bdm.process_brain_dump([{"text": long_text, "category": "idea"}])

        assert len(result.items) == 1
        assert len(result.items[0].raw_text) == bdm.MAX_TEXT_LENGTH

    @pytest.mark.asyncio
    async def test_invalid_category_normalized(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump
        result = await bdm.process_brain_dump(
            [
                {"text": "something", "category": "banana"},
            ]
        )
        assert result.items[0].category == "idea"

    @pytest.mark.asyncio
    async def test_all_valid_categories_accepted(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump

        with patch("brain_dump_manager._route_to_reminder", new_callable=AsyncMock, return_value="added as a reminder"):
            items = [{"text": f"item for {cat}", "category": cat} for cat in bdm.VALID_CATEGORIES]
            result = await bdm.process_brain_dump(items)

        categories = {item.category for item in result.items}
        assert categories == bdm.VALID_CATEGORIES


# ---------------------------------------------------------------------------
# Tests: Max items cap
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestMaxItemsCap:
    @pytest.mark.asyncio
    async def test_over_20_items_truncated(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump

        items = [{"text": f"item {i}", "category": "idea"} for i in range(25)]
        result = await bdm.process_brain_dump(items)

        assert len(result.items) == bdm.MAX_ITEMS  # 20

    @pytest.mark.asyncio
    async def test_exactly_20_items_not_truncated(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump

        items = [{"text": f"item {i}", "category": "idea"} for i in range(20)]
        result = await bdm.process_brain_dump(items)

        assert len(result.items) == 20

    @pytest.mark.asyncio
    async def test_under_20_items_not_truncated(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump

        items = [{"text": f"item {i}", "category": "idea"} for i in range(5)]
        result = await bdm.process_brain_dump(items)

        assert len(result.items) == 5


# ---------------------------------------------------------------------------
# Tests: Error handling — RAG upsert failure
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_rag_upsert_failure_does_not_crash(self, patched_brain_dump):
        bdm, coll, emb, _ = patched_brain_dump

        coll.upsert.side_effect = Exception("Disk full")

        result = await bdm.process_brain_dump(
            [
                {"text": "this should fail gracefully", "category": "idea"},
            ]
        )

        # Should still return a result, not raise
        assert len(result.items) == 1
        assert "error" in result.summary.lower() or "could not save" in result.summary.lower()

    @pytest.mark.asyncio
    async def test_one_failure_doesnt_block_others(self, patched_brain_dump):
        bdm, coll, emb, _ = patched_brain_dump

        call_count = 0

        def upsert_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("First item fails")
            # Subsequent calls succeed

        coll.upsert.side_effect = upsert_side_effect

        items = [
            {"text": "will fail", "category": "idea"},
            {"text": "will succeed", "category": "idea"},
        ]
        result = await bdm.process_brain_dump(items)

        # Both items should be in the result
        assert len(result.items) == 2
        # Second item should have succeeded
        assert coll.upsert.call_count == 2

    @pytest.mark.asyncio
    async def test_embedding_failure_handled(self, patched_brain_dump):
        bdm, coll, emb, _ = patched_brain_dump

        emb.encode.side_effect = RuntimeError("CUDA out of memory")

        result = await bdm.process_brain_dump(
            [
                {"text": "embed will fail", "category": "idea"},
            ]
        )

        assert len(result.items) == 1
        assert "error" in result.summary.lower() or "could not save" in result.summary.lower()

    @pytest.mark.asyncio
    async def test_route_item_exception_caught_per_item(self, patched_brain_dump):
        """Routing failure on one item should not prevent processing of others."""
        bdm, *_ = patched_brain_dump

        original_route = bdm.route_item
        call_count = 0

        async def failing_route(item):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("unexpected routing error")
            return await original_route(item)

        with patch.object(bdm, "route_item", side_effect=failing_route):
            items = [
                {"text": "first item", "category": "idea"},
                {"text": "second item", "category": "idea"},
            ]
            result = await bdm.process_brain_dump(items)

        assert len(result.items) == 2
        # First item should have error confirmation, second should succeed
        assert "could not save" in result.summary.lower() or "error" in result.summary.lower()


# ---------------------------------------------------------------------------
# Tests: route_item dispatching logic
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestRouteItem:
    @pytest.mark.asyncio
    async def test_reminder_category_dispatches_to_reminder(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump

        with patch("brain_dump_manager._route_to_reminder", new_callable=AsyncMock) as mock_rem:
            mock_rem.return_value = "added as a reminder"
            item = bdm.CapturedItem(raw_text="call mom", category="reminder")
            result = await bdm.route_item(item)

        mock_rem.assert_called_once()
        assert result == "added as a reminder"

    @pytest.mark.asyncio
    async def test_task_with_now_urgency_dispatches_to_reminder(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump

        with patch("brain_dump_manager._route_to_reminder", new_callable=AsyncMock) as mock_rem:
            mock_rem.return_value = "added as a reminder"
            item = bdm.CapturedItem(raw_text="send email", category="task", urgency="now")
            result = await bdm.route_item(item)

        mock_rem.assert_called_once()

    @pytest.mark.asyncio
    async def test_idea_dispatches_to_rag(self, patched_brain_dump):
        bdm, coll, *_ = patched_brain_dump

        item = bdm.CapturedItem(raw_text="build a robot", category="idea")
        result = await bdm.route_item(item)

        assert "saved as an idea" in result
        coll.upsert.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_category_fallback_to_rag(self, patched_brain_dump):
        """Even if a category somehow bypasses validation, route_item falls back to RAG."""
        bdm, coll, *_ = patched_brain_dump

        # Manually create an item with an unexpected category
        item = bdm.CapturedItem(raw_text="mystery item", category="mystery")
        result = await bdm.route_item(item)

        # Should still store in RAG as fallback
        coll.upsert.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: CapturedItem dataclass defaults
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestCapturedItemDefaults:
    def test_default_urgency(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump
        item = bdm.CapturedItem(raw_text="test", category="idea")
        assert item.urgency == "someday"

    def test_default_confidence(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump
        item = bdm.CapturedItem(raw_text="test", category="idea")
        assert item.confidence == 1.0

    def test_default_routed_to(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump
        item = bdm.CapturedItem(raw_text="test", category="idea")
        assert item.routed_to == ""

    def test_created_at_set(self, patched_brain_dump):
        bdm, *_ = patched_brain_dump
        item = bdm.CapturedItem(raw_text="test", category="idea")
        assert item.created_at is not None
