"""
Brain Dump Manager: captures, categorizes, and routes brain dump items.

The model does the parsing/classification — this module just routes each
item to the right destination (RAG, reminders, etc.) and builds a
TTS-friendly confirmation.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List

from orchestrator.metrics import (
    BRAIN_DUMP_DUPLICATES_SKIPPED,
    BRAIN_DUMP_ERRORS,
    BRAIN_DUMP_ITEMS_CAPTURED,
    BRAIN_DUMP_ITEMS_ROUTED,
    BRAIN_DUMP_RAG_LATENCY,
)
from orchestrator.shared import collection, embedding_model

logger = logging.getLogger(__name__)

# Limits
MAX_ITEMS = 20
MAX_TEXT_LENGTH = 2000
DEDUP_THRESHOLD = 0.85

# Valid categories
VALID_CATEGORIES = {"task", "reminder", "idea", "errand", "preference", "research"}


@dataclass
class CapturedItem:
    raw_text: str
    category: str  # task, reminder, idea, errand, preference, research
    routed_to: str = ""
    urgency: str = "someday"
    confidence: float = 1.0
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class BrainDumpResult:
    items: List[CapturedItem]
    summary: str  # TTS-friendly confirmation


# Categories that get stored in RAG
RAG_CATEGORIES = {"idea", "preference", "research", "errand", "task"}

# Categories that get routed to reminders (when urgency is "now" or "today")
REMINDER_CATEGORIES = {"task", "errand", "reminder"}


async def route_item(item: CapturedItem) -> str:
    """Route a single captured item to its destination.

    Returns a short confirmation string.
    """
    if item.category == "reminder" or (item.category in REMINDER_CATEGORIES and item.urgency in ("now", "today")):
        return await _route_to_reminder(item)

    if item.category in RAG_CATEGORIES:
        return await _route_to_rag(item)

    # Fallback: store in RAG
    return await _route_to_rag(item)


async def _route_to_reminder(item: CapturedItem) -> str:
    """Route item to the reminder system."""
    from orchestrator.reminder_manager import add_reminder, parse_time_expression
    from orchestrator.shared import scheduler
    from orchestrator.tool_handlers import deliver_reminder_job

    time_map = {
        "now": "in 5 minutes",
        "today": "in 2 hours",
        "soon": "in 4 hours",
        "someday": "tomorrow at 9am",
    }
    time_str = time_map.get(item.urgency, "tomorrow at 9am")

    trigger_time, error = parse_time_expression(time_str)
    if error:
        logger.warning(
            "[BRAIN_DUMP] Could not parse time for reminder: %s",
            error,
            extra={"component": "brain_dump"},
        )
        # Fall back to RAG storage
        return await _route_to_rag(item)

    reminder_id = str(uuid.uuid4())[:8]
    add_reminder(reminder_id, item.raw_text, trigger_time, "both")

    scheduler.add_job(
        deliver_reminder_job,
        trigger="date",
        run_date=trigger_time,
        args=[reminder_id],
        id=f"reminder_{reminder_id}",
        replace_existing=True,
    )

    item.routed_to = "reminder"
    BRAIN_DUMP_ITEMS_ROUTED.labels(destination="reminder").inc()
    logger.info(
        "[BRAIN_DUMP] Routed to reminder (id=%s)",
        reminder_id,
        extra={"component": "brain_dump"},
    )
    return "added as a reminder"


async def _is_duplicate(text: str, embedding: list) -> bool:
    """Check if a similar brain dump item already exists in RAG."""
    try:
        results = await asyncio.to_thread(
            collection.query,
            query_embeddings=[embedding],
            n_results=3,
            where={"source": "brain_dump"},
            include=["documents", "distances"],
        )

        docs = results.get("documents", [[]])[0]
        dists = results.get("distances", [[]])[0]

        for doc, dist in zip(docs, dists, strict=False):
            if doc is None:
                continue
            cos_sim = 1.0 - float(dist)
            if cos_sim > DEDUP_THRESHOLD:
                return True
            # Substring match
            existing = doc.lower().strip()
            new = text.lower().strip()
            if new in existing or existing in new:
                return True

    except Exception as e:
        logger.warning(
            "[BRAIN_DUMP] Dedup check failed: %s",
            e,
            extra={"component": "brain_dump"},
        )

    return False


async def _route_to_rag(item: CapturedItem) -> str:
    """Store item in ChromaDB RAG with dedup check."""
    import time

    _t0 = time.time()
    doc_id = f"brain_dump_{item.category}_{uuid.uuid4().hex[:12]}"
    now = datetime.now()

    metadata = {
        "category": item.category,
        "source": "brain_dump",
        "kind": "chunk",
        "urgency": item.urgency,
        "created_at": now.isoformat(),
    }

    try:
        embedding = await asyncio.to_thread(
            lambda: embedding_model.encode(item.raw_text, normalize_embeddings=True).tolist()
        )

        # Dedup check
        if await _is_duplicate(item.raw_text, embedding):
            BRAIN_DUMP_DUPLICATES_SKIPPED.inc()
            logger.info(
                "[BRAIN_DUMP] Duplicate skipped (category=%s)",
                item.category,
                extra={"component": "brain_dump"},
            )
            item.routed_to = "duplicate"
            return "already saved (duplicate skipped)"

        await asyncio.to_thread(
            lambda: collection.upsert(
                documents=[item.raw_text.strip()],
                metadatas=[metadata],
                ids=[doc_id],
                embeddings=[embedding],
            )
        )
    except Exception as e:
        BRAIN_DUMP_ERRORS.labels(operation="rag_upsert").inc()
        logger.error(
            "[BRAIN_DUMP] RAG upsert failed: %s",
            e,
            extra={"component": "brain_dump", "error_type": type(e).__name__},
        )
        return "could not save (error)"

    BRAIN_DUMP_RAG_LATENCY.observe(time.time() - _t0)
    item.routed_to = "memory"

    route_labels = {
        "preference": "saved to your preferences",
        "idea": "saved as an idea",
        "research": "saved as a research task",
        "errand": "added to your errands",
        "task": "saved as a task",
    }
    label = route_labels.get(item.category, "saved to memory")
    BRAIN_DUMP_ITEMS_ROUTED.labels(destination="rag").inc()
    logger.info(
        "[BRAIN_DUMP] Routed to RAG (category=%s)",
        item.category,
        extra={"component": "brain_dump"},
    )
    return label


async def process_brain_dump(items_raw: List[Dict[str, Any]]) -> BrainDumpResult:
    """Process a list of brain dump items from the model's tool call.

    Each item dict has: text, category, and optionally urgency.
    Routes each item and builds a TTS-friendly summary.
    """
    # Cap items to prevent abuse
    items_raw = items_raw[:MAX_ITEMS]

    captured: List[CapturedItem] = []

    for raw in items_raw:
        text = raw.get("text", "").strip()
        if not text:
            continue

        # Truncate long text
        text = text[:MAX_TEXT_LENGTH]

        # Validate category
        category = raw.get("category", "idea")
        if category not in VALID_CATEGORIES:
            category = "idea"

        item = CapturedItem(
            raw_text=text,
            category=category,
            urgency=raw.get("urgency", "someday"),
        )
        captured.append(item)
        BRAIN_DUMP_ITEMS_CAPTURED.labels(category=category).inc()

    # Route each item with per-item error handling
    confirmations: List[str] = []
    for item in captured:
        try:
            confirmation = await route_item(item)
        except Exception as e:
            BRAIN_DUMP_ERRORS.labels(operation="route").inc()
            logger.error(
                "[BRAIN_DUMP] Failed to route item (category=%s): %s",
                item.category,
                e,
                extra={"component": "brain_dump", "error_type": type(e).__name__},
            )
            confirmation = "could not save (error)"

        # Truncate text in TTS confirmation
        display_text = item.raw_text[:100] + "..." if len(item.raw_text) > 100 else item.raw_text
        confirmations.append(f"{display_text} — {confirmation}")

    # Record progress event (F-005)
    routed_count = sum(1 for c in confirmations if "error" not in c and "duplicate" not in c.lower())
    if routed_count > 0:
        try:
            import asyncio

            from orchestrator import progress_tracker

            progress_tracker.record_event("brain_dump", {"count": routed_count})
            asyncio.ensure_future(progress_tracker.check_and_announce_streaks())
        except Exception as e:
            logger.warning(f"[BRAIN_DUMP] Progress tracking failed: {e}")

    # Build TTS-friendly summary
    if len(captured) == 0:
        summary = "Nothing to capture."
    elif len(captured) == 1:
        summary = f"Got it — {confirmations[0]}."
    else:
        numbered = "\n".join(f"{i}. {c}" for i, c in enumerate(confirmations, 1))
        summary = f"Captured {len(captured)} things:\n{numbered}\nAll sorted."

    return BrainDumpResult(
        items=captured,
        summary=summary,
    )
