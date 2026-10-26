"""
Memory manager for Brain Gateway.

Handles user corrections to RAG knowledge — search for conflicting facts,
delete outdated ones, store the correction with an audit trail.
"""

import asyncio
import contextlib
import json
import logging
import uuid
from datetime import datetime
from typing import List, Sequence, Tuple

import numpy as np

from orchestrator import shared
from orchestrator.auto_learn import maybe_decrypt
from orchestrator.metrics import MEMORY_FACTS_REPLACED, MEMORY_REPLACE_SKIPPED

logger = logging.getLogger(__name__)

# Similarity threshold for finding conflicting facts (lower = wider net).
# Measured on the live embedder: a real correction ("favorite color is blue" →
# "green") scores ~0.62-0.90 against the old fact, different facts about the
# user's past 0.16-0.53. A fact is replaced only when the search query AND the
# new text both clear it, and only fact docs are candidates — the search used
# to run over the whole collection, document chunks and vault entries included.
CONFLICT_THRESHOLD = 0.60


async def update_memory(correction: str, search_query: str, category: str = "general") -> str:
    """
    Update or correct a fact in memory.

    1. Search ChromaDB for facts matching search_query
    2. Delete conflicting facts above similarity threshold
    3. Store the corrected fact with audit metadata
    4. Return confirmation of what changed

    Args:
        correction: The correct/updated information to store.
        search_query: What to search for to find outdated fact(s).
        category: Fact category (identity, preference, health, routine, project, technical, general).

    Returns:
        Human-readable confirmation of what was updated.
    """
    if not correction.strip():
        return "No correction provided."

    if not search_query.strip():
        # Use the correction itself as the search query
        search_query = correction

    correction_embedding = await asyncio.to_thread(
        lambda: shared.embedding_model.encode(correction, normalize_embeddings=True).tolist()
    )

    # 1. Search for conflicting facts
    conflicts, kept_similar = await asyncio.to_thread(_find_conflicts, search_query, correction, correction_embedding)

    # 2. Delete conflicting facts
    deleted_ids = []
    deleted_summaries = []
    for doc_id, doc_text, similarity in conflicts:
        await asyncio.to_thread(shared.collection.delete, ids=[doc_id])
        deleted_ids.append(doc_id)
        deleted_summaries.append(doc_text[:80])
        logger.info("[MEMORY] Deleted conflicting fact: %s (sim=%.2f)", doc_id, similarity)
    if deleted_ids:
        MEMORY_FACTS_REPLACED.inc(len(deleted_ids))

    # 3. Store the corrected fact (with palace routing, plaintext)
    now = datetime.now()
    doc_id = f"correction_{now.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"

    # Route to wing/room (gated by PALACE_ENABLED)
    wing, room = "", ""
    if shared.PALACE_ENABLED:
        try:
            palace = shared.get_palace()
            wing, room = palace.route_fact(correction, category)
        except Exception as e:
            logger.warning("[MEMORY] Palace routing failed (non-fatal): %s", e)

    # Store the correction as plaintext. We intentionally do NOT encrypt
    # here, even though auto_learn facts are encrypted: rag_context() reads
    # directly from the collection without decrypting, and encrypting would
    # mean the correction's text is never recoverable by the LLM on the
    # next query. Keeping corrections plaintext is the simple fix. If we
    # later teach rag_context to decrypt, this can flip back.
    metadata = {
        "source": "user_correction",
        "category": category,
        "updated_at": now.isoformat(),
        "replaced_ids": json.dumps(deleted_ids) if deleted_ids else "[]",
        "kind": "chunk",
        "wing": wing,
        "room": room,
        "encrypted": "false",
    }

    await asyncio.to_thread(
        shared.collection.upsert,
        documents=[correction],
        embeddings=[correction_embedding],
        metadatas=[metadata],
        ids=[doc_id],
    )

    if shared.PALACE_ENABLED:
        with contextlib.suppress(Exception):
            # Writes here bypass palace.store(): refresh the identity block now,
            # not up to 30 min later (a corrected fact must stop showing at once).
            shared.get_palace().invalidate_wakeup_cache()

    logger.info(
        "[MEMORY] Stored correction: %s (replaced %d facts, category=%s)",
        doc_id,
        len(deleted_ids),
        category,
    )

    # 4. Build confirmation
    if deleted_ids:
        old_summary = "; ".join(f'"{s}..."' for s in deleted_summaries)
        return (
            f"Updated my memory. Replaced {len(deleted_ids)} outdated fact(s):\n"
            f"- Old: {old_summary}\n"
            f"- New: {correction}\n"
            f"Category: {category}"
        )
    if kept_similar:
        # Similar to the search but not to the new text: kept, so the model can
        # check with the user rather than claim the old fact is gone.
        return (
            f"Added to my memory:\n- {correction}\nCategory: {category}\n"
            f"Kept {kept_similar} similar older fact(s) that the new text doesn't clearly replace — "
            "both are now stored; ask the user if one is outdated."
        )
    return f"Added to my memory (no conflicting facts found to replace):\n- {correction}\nCategory: {category}"


def _find_conflicts(
    search_query: str, correction: str, correction_embedding: Sequence[float]
) -> Tuple[List[Tuple[str, str, float]], int]:
    """
    Find stored facts that the correction supersedes.

    Candidates are fact docs (auto-learn, earlier corrections) close to the
    search query; each must also be close to the correction text itself, so a
    broad query can't sweep away facts the new text isn't about.

    Returns ([(doc_id, readable_text, cosine_similarity)], kept) — similarity
    is the lower of the two; `kept` counts query matches held back because the
    new text isn't about them.
    """
    try:
        if search_query.strip() == correction.strip():
            embedding = list(correction_embedding)
        else:
            embedding = shared.embedding_model.encode(search_query, normalize_embeddings=True).tolist()
        results = shared.collection.query(
            query_embeddings=[embedding],
            n_results=10,
            where={"source": {"$in": list(shared.MEMORY_FACT_SOURCES)}},
            include=["documents", "metadatas", "distances", "embeddings"],
        )
    except Exception as e:
        # Storing the correction still beats failing the save over a lookup.
        logger.warning("[MEMORY] Conflict search failed, adding without replacing: %s", type(e).__name__)
        return [], 0

    conflicts: List[Tuple[str, str, float]] = []
    kept = 0
    if not results or not results.get("ids") or not results["ids"][0]:
        return conflicts, kept

    correction_vec = np.asarray(correction_embedding, dtype=float)
    for doc_id, doc_text, meta, distance, doc_embedding in zip(
        results["ids"][0],
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
        results["embeddings"][0],
        strict=False,
    ):
        # ChromaDB returns squared L2 distance; cos_sim = 1 - dist/2
        query_sim = 1.0 - float(distance) / 2.0
        if query_sim < CONFLICT_THRESHOLD:
            continue
        # Both vectors are normalized, so the dot product is the cosine.
        new_sim = float(np.dot(np.asarray(doc_embedding, dtype=float), correction_vec))
        if new_sim < CONFLICT_THRESHOLD:
            MEMORY_REPLACE_SKIPPED.inc()
            kept += 1
            continue
        conflicts.append((doc_id, maybe_decrypt(doc_text or "", meta), min(query_sim, new_sim)))

    return conflicts, kept
