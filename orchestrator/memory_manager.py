"""
Memory manager for Brain Gateway.

Handles user corrections to RAG knowledge — search for conflicting facts,
delete outdated ones, store the correction with an audit trail.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import List, Tuple

from orchestrator import shared

logger = logging.getLogger(__name__)

# Similarity threshold for finding conflicting facts (lower = wider net)
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

    # 1. Search for conflicting facts
    conflicts = await asyncio.to_thread(_find_conflicts, search_query)

    # 2. Delete conflicting facts
    deleted_ids = []
    deleted_summaries = []
    for doc_id, doc_text, similarity in conflicts:
        await asyncio.to_thread(shared.collection.delete, ids=[doc_id])
        deleted_ids.append(doc_id)
        deleted_summaries.append(doc_text[:80])
        logger.info("[MEMORY] Deleted conflicting fact: %s (sim=%.2f)", doc_id, similarity)

    # 3. Store the corrected fact (with palace routing, plaintext)
    now = datetime.now()
    doc_id = f"correction_{now.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"

    # Route to wing/room (gated by PALACE_ENABLED)
    wing, room = "", ""
    if shared.PALACE_ENABLED:
        try:
            palace = shared.get_palace()
            wing, room = palace.route_to_room(correction)
        except Exception as e:
            logger.warning("[MEMORY] Palace routing failed (non-fatal): %s", e)

    # Store the correction as plaintext. We intentionally do NOT encrypt
    # here, even though auto_learn facts are encrypted: rag_context() reads
    # directly from the collection without decrypting, and encrypting would
    # mean the correction's text is never recoverable by the LLM on the
    # next query. Keeping corrections plaintext is the simple fix. If we
    # later teach rag_context to decrypt, this can flip back.
    embedding = shared.embedding_model.encode(correction, normalize_embeddings=True).tolist()
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
        embeddings=[embedding],
        metadatas=[metadata],
        ids=[doc_id],
    )

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
    else:
        return f"Added to my memory (no conflicting facts found to replace):\n- {correction}\nCategory: {category}"


def _find_conflicts(search_query: str) -> List[Tuple[str, str, float]]:
    """
    Search ChromaDB for facts that conflict with the search query.

    Returns list of (doc_id, doc_text, cosine_similarity) above threshold.
    """
    embedding = shared.embedding_model.encode(search_query, normalize_embeddings=True).tolist()

    results = shared.collection.query(
        query_embeddings=[embedding],
        n_results=10,
        include=["documents", "metadatas", "distances"],
    )

    conflicts = []
    if not results or not results.get("ids") or not results["ids"][0]:
        return conflicts

    for doc_id, doc_text, distance in zip(
        results["ids"][0],
        results["documents"][0],
        results["distances"][0],
        strict=False,
    ):
        # ChromaDB returns L2 distance; convert to cosine similarity
        cosine_sim = 1.0 - float(distance)
        if cosine_sim >= CONFLICT_THRESHOLD:
            conflicts.append((doc_id, doc_text, cosine_sim))

    return conflicts
