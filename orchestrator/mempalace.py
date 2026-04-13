"""
MemPalace: Structured memory system for Brain Gateway.

Organizes memories into wings (projects/domains) and rooms (topics).
Built on top of the existing ChromaDB + embedding infrastructure.
Reuses encryption from auto_learn.py.

Usage:
    from orchestrator.shared import get_palace
    palace = get_palace()
    palace.store("Nadim prefers step-by-step", wing="personal", room="preferences")
    results = palace.search("preferences")
"""

import asyncio
import hashlib
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import yaml

from orchestrator import shared
from orchestrator.auto_learn import decrypt_text, encrypt_text
from orchestrator.metrics import (
    PALACE_MEMORIES_TOTAL,
    PALACE_SEARCH_LATENCY,
    PALACE_SEARCHES_TOTAL,
    PALACE_STORES_TOTAL,
)

logger = logging.getLogger(__name__)


class MemPalace:
    """Structured memory palace backed by ChromaDB."""

    def __init__(self):
        self._config: Optional[Dict] = None
        self._routing_rules: List[Dict] = []
        self._wakeup_cache: Optional[str] = None
        self._wakeup_cache_time: float = 0.0
        # Cache a reference to the shared chromadb collection as a plain
        # instance attribute (not a @property). `shared.collection` is
        # set once at module import time and never reassigned, so caching
        # it here is safe. A plain attribute is also patchable via
        # `unittest.mock.patch.object(palace, "_collection", mock)` in
        # tests, which a read-only @property would reject with
        # "has no deleter" on teardown.
        self._collection = shared.collection
        self._load_config()

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    def _load_config(self):
        """Load palace structure from YAML config."""
        import os

        path = shared.PALACE_YAML_PATH
        if not os.path.exists(path):
            logger.warning("[PALACE] Config not found at %s — using defaults", path)
            self._config = {"wings": {}, "routing_rules": [], "wakeup": {}}
            return

        try:
            with open(path) as f:
                self._config = yaml.safe_load(f) or {}
            self._routing_rules = self._config.get("routing_rules", [])
            # Precompile regex patterns
            for rule in self._routing_rules:
                try:
                    rule["_compiled"] = re.compile(rule["pattern"], re.IGNORECASE)
                except re.error as e:
                    logger.warning("[PALACE] Bad routing regex '%s': %s", rule.get("pattern"), e)
            logger.info(
                "[PALACE] Loaded config: %d wings, %d routing rules",
                len(self._config.get("wings", {})),
                len(self._routing_rules),
            )
        except Exception as e:
            logger.error("[PALACE] Failed to load config: %s", e)
            self._config = {"wings": {}, "routing_rules": [], "wakeup": {}}

    def is_known_wing(self, wing: str) -> bool:
        """Return True iff `wing` is a configured wing name."""
        if not wing:
            return False
        return wing in self._config.get("wings", {})

    def known_wings(self) -> set:
        """Return the set of configured wing names."""
        return set(self._config.get("wings", {}).keys())

    # ------------------------------------------------------------------
    # Store
    # ------------------------------------------------------------------

    async def store(
        self,
        text: str,
        wing: str = "",
        room: str = "",
        source: str = "manual",
        category: str = "general",
        confidence: str = "high",
        project: str = "",
        session_id: str = "",
    ) -> Optional[str]:
        """
        Store a memory in the palace.

        Auto-routes to wing/room if not provided.
        Returns doc_id if stored, None if duplicate or error.
        """
        text = text.strip()
        if not text or len(text) < 5:
            return None

        # Auto-route if wing not specified
        if not wing:
            wing, room = self.route_to_room(text, project=project)

        # Validate wing exists (or default). Logging the fallback helps
        # catch cases where a caller passes an unknown wing string.
        wings = self._config.get("wings", {})
        if wing not in wings:
            if wing:
                logger.warning("[PALACE] store() unknown wing %r → falling back to 'personal'", wing)
            wing = "personal"
        if room and wing in wings:
            rooms = wings[wing].get("rooms", {})
            if room not in rooms:
                logger.debug("[PALACE] store() unknown room %r in wing %s → dropping", room, wing)
                room = ""

        # Dedup check
        if await self.is_duplicate(text, wing=wing, room=room):
            logger.info("[PALACE] Duplicate skipped (wing=%s, room=%s)", wing, room)
            return None

        now = datetime.now()
        text_hash = hashlib.sha256(text.encode()).hexdigest()[:8]
        doc_id = f"palace_{wing}_{room or 'unrouted'}_{now.strftime('%Y%m%d%H%M%S')}_{text_hash}"

        # Encrypt before storing
        encrypted_text = encrypt_text(text)

        # Embed the plaintext
        embedding = await asyncio.to_thread(
            lambda: shared.embedding_model.encode(text, normalize_embeddings=True).tolist()
        )

        metadata: Dict[str, Any] = {
            "wing": wing,
            "room": room or "",
            "source": source,
            "category": category,
            "confidence": confidence,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "project": project,
            "session_id": session_id,
            "encrypted": "true" if shared.AUTO_LEARN_ENCRYPT else "false",
        }

        try:
            await asyncio.to_thread(
                self._collection.add,
                documents=[encrypted_text],
                embeddings=[embedding],
                metadatas=[metadata],
                ids=[doc_id],
            )
        except Exception as e:
            logger.error("[PALACE] Store failed: %s", e)
            return None

        PALACE_STORES_TOTAL.labels(wing=wing, room=room or "unrouted").inc()
        self._invalidate_wakeup_cache()
        logger.info("[PALACE] Stored memory (wing=%s, room=%s, source=%s, id=%s)", wing, room, source, doc_id)
        return doc_id

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        wing: str = "",
        room: str = "",
        n_results: int = 5,
    ) -> List[Dict]:
        """
        Semantic search across the palace.

        Optional wing/room filters narrow the search scope.
        Returns list of dicts with id, text, wing, room, score, metadata.
        """
        PALACE_SEARCHES_TOTAL.inc()
        t0 = time.time()

        query = query.strip()
        if not query:
            return []

        # Soft-fail: unknown wing → drop the filter and log. This prevents a
        # prompt-injected LLM from narrowing to a room that doesn't exist
        # and silently getting zero results (which would mask a bug), and
        # prevents arbitrary strings from reaching ChromaDB.
        if wing and not self.is_known_wing(wing):
            logger.warning("[PALACE] search() ignoring unknown wing: %r", wing)
            wing = ""

        # Build where filter
        where_filter = self._build_where_filter(wing, room)

        try:
            embedding = await asyncio.to_thread(
                lambda: shared.embedding_model.encode(query, normalize_embeddings=True).tolist()
            )

            kwargs: Dict[str, Any] = {
                "query_embeddings": [embedding],
                "n_results": n_results,
                "include": ["documents", "metadatas", "distances"],
            }
            if where_filter:
                kwargs["where"] = where_filter

            results = await asyncio.to_thread(lambda: self._collection.query(**kwargs))
        except Exception as e:
            logger.error("[PALACE] Search failed: %s", e)
            PALACE_SEARCH_LATENCY.observe(time.time() - t0)
            return []

        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        dists = results.get("distances", [[]])[0]
        ids = results.get("ids", [[]])[0]

        memories = []
        for doc_id, doc, meta, dist in zip(ids, docs, metas, dists, strict=False):
            if doc is None:
                continue
            # ChromaDB returns squared L2 distance; cos_sim = 1 - dist/2 for
            # normalized vectors (see prompt_builder.rag_context for context).
            cos_sim = 1.0 - float(dist) / 2.0
            # Only decrypt if the chunk was actually stored encrypted. Detection
            # is metadata-driven (encrypted="true") with a format-based fallback
            # (Fernet v0 tokens start with "gAAAAAB"). Plaintext RAG chunks,
            # correction docs, and file markers bypass decrypt_text entirely,
            # which would otherwise return a "[encrypted — decryption failed]"
            # placeholder for legitimate plaintext data.
            is_encrypted = str(meta.get("encrypted", "")).lower() == "true" or doc.startswith("gAAAAAB")
            text = decrypt_text(doc) if is_encrypted else doc
            memories.append(
                {
                    "id": doc_id,
                    "text": text,
                    "wing": meta.get("wing", ""),
                    "room": meta.get("room", ""),
                    "source": meta.get("source", ""),
                    "category": meta.get("category", ""),
                    "confidence": meta.get("confidence", ""),
                    "score": round(cos_sim, 3),
                    "created_at": meta.get("created_at", ""),
                }
            )

        PALACE_SEARCH_LATENCY.observe(time.time() - t0)
        logger.info("[PALACE] Search '%s' returned %d results (%.2fs)", query[:50], len(memories), time.time() - t0)
        return memories

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def get_by_id(self, doc_id: str) -> Optional[Dict]:
        """Get a single memory by ID."""
        if not doc_id.startswith("palace_"):
            return None
        try:
            result = self._collection.get(ids=[doc_id], include=["documents", "metadatas"])
            docs = result.get("documents", [])
            metas = result.get("metadatas", [])
            if not docs:
                return None
            raw = docs[0]
            meta = metas[0] if metas else {}
            is_encrypted = str((meta or {}).get("encrypted", "")).lower() == "true" or raw.startswith("gAAAAAB")
            text = decrypt_text(raw) if is_encrypted else raw
            return {
                "id": doc_id,
                "text": text,
                "wing": meta.get("wing", ""),
                "room": meta.get("room", ""),
                "source": meta.get("source", ""),
                "category": meta.get("category", ""),
                "confidence": meta.get("confidence", ""),
                "created_at": meta.get("created_at", ""),
                "updated_at": meta.get("updated_at", ""),
                "project": meta.get("project", ""),
                "session_id": meta.get("session_id", ""),
            }
        except Exception as e:
            logger.error("[PALACE] get_by_id failed for %s: %s", doc_id, e)
            return None

    def delete(self, doc_id: str) -> bool:
        """Delete a memory by ID."""
        if not doc_id.startswith("palace_"):
            return False
        try:
            self._collection.delete(ids=[doc_id])
            self._invalidate_wakeup_cache()
            logger.info("[PALACE] Deleted memory: %s", doc_id)
            return True
        except Exception as e:
            logger.error("[PALACE] Delete failed for %s: %s", doc_id, e)
            return False

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def route_to_room(self, text: str, project: str = "") -> Tuple[str, str]:
        """
        Auto-route text to a wing/room using regex rules then keyword fallback.

        Returns (wing, room) tuple. Defaults to ("personal", "") if no match.
        """
        text_lower = text.lower()

        # 1. Try regex routing rules
        for rule in self._routing_rules:
            compiled = rule.get("_compiled")
            if compiled and compiled.search(text_lower):
                return rule.get("wing", "personal"), rule.get("room", "")

        # 2. Try project-based routing
        if project:
            project_lower = project.lower()
            if "conjure" in project_lower:
                return "conjure", ""
            if "brain" in project_lower or "gateway" in project_lower:
                return "brain_gateway", ""

        # 3. Keyword fallback — score each wing/room by keyword hits
        wings = self._config.get("wings", {})
        best_wing = "personal"
        best_room = ""
        best_score = 0

        for wing_name, wing_data in wings.items():
            rooms = wing_data.get("rooms", {})
            for room_name, room_data in rooms.items():
                keywords = room_data.get("keywords", [])
                score = sum(1 for kw in keywords if kw in text_lower)
                if score > best_score:
                    best_score = score
                    best_wing = wing_name
                    best_room = room_name

        return best_wing, best_room

    # ------------------------------------------------------------------
    # Dedup
    # ------------------------------------------------------------------

    async def is_duplicate(
        self,
        text: str,
        wing: str = "",
        room: str = "",
    ) -> bool:
        """Check if a semantically similar memory already exists."""
        try:
            embedding = await asyncio.to_thread(
                lambda: shared.embedding_model.encode(text, normalize_embeddings=True).tolist()
            )

            kwargs: Dict[str, Any] = {
                "query_embeddings": [embedding],
                "n_results": 3,
                "include": ["documents", "distances"],
            }
            where_filter = self._build_where_filter(wing, room)
            if where_filter:
                kwargs["where"] = where_filter

            results = await asyncio.to_thread(lambda: self._collection.query(**kwargs))

            docs = results.get("documents", [[]])[0]
            dists = results.get("distances", [[]])[0]

            for doc, dist in zip(docs, dists, strict=False):
                if doc is None:
                    continue
                cos_sim = 1.0 - float(dist) / 2.0
                if cos_sim > shared.PALACE_DEDUP_THRESHOLD:
                    return True
                # Substring match on decrypted text
                existing = decrypt_text(doc).lower().strip()
                new = text.lower().strip()
                if new in existing or existing in new:
                    return True

        except Exception as e:
            logger.warning("[PALACE] Dedup check failed: %s", e)

        return False

    # ------------------------------------------------------------------
    # Wakeup context
    # ------------------------------------------------------------------

    def generate_wakeup_context(self) -> str:
        """
        Generate a compressed identity block (~170 tokens) from priority rooms.

        Cached and refreshed every 30 minutes or on new store.
        """
        wakeup_cfg = self._config.get("wakeup", {})
        if not wakeup_cfg.get("enabled", True):
            return ""

        # Check cache
        refresh_interval = wakeup_cfg.get("refresh_interval_minutes", 30) * 60
        if self._wakeup_cache and (time.time() - self._wakeup_cache_time) < refresh_interval:
            return self._wakeup_cache

        priority_rooms = wakeup_cfg.get("priority_rooms", [])
        max_tokens = wakeup_cfg.get("max_tokens", 170)
        # Rough chars-per-token estimate
        max_chars = max_tokens * 4

        lines = []
        for room_path in priority_rooms:
            parts = room_path.split("/")
            if len(parts) != 2:
                continue
            wing, room = parts

            try:
                where_filter: Dict[str, Any] = {"$and": [{"wing": wing}, {"room": room}]}
                results = self._collection.get(
                    where=where_filter,
                    limit=5,
                    include=["documents", "metadatas"],
                )
                docs = results.get("documents", [])
                for doc in docs:
                    if doc:
                        text = decrypt_text(doc)
                        lines.append(f"- {text}")
            except Exception as e:
                logger.debug("[PALACE] Wakeup context fetch failed for %s: %s", room_path, e)

        if not lines:
            self._wakeup_cache = ""
            self._wakeup_cache_time = time.time()
            return ""

        # Truncate to max_chars
        context = "\n".join(lines)
        if len(context) > max_chars:
            context = context[:max_chars].rsplit("\n", 1)[0]

        self._wakeup_cache = context
        self._wakeup_cache_time = time.time()
        return context

    def _invalidate_wakeup_cache(self):
        """Invalidate the wakeup context cache (called after store/delete)."""
        self._wakeup_cache = None
        self._wakeup_cache_time = 0.0

    # ------------------------------------------------------------------
    # Structure queries
    # ------------------------------------------------------------------

    def list_wings(self) -> Dict[str, Any]:
        """Return the palace wing structure with descriptions."""
        wings = self._config.get("wings", {})
        result = {}
        for wing_name, wing_data in wings.items():
            rooms = wing_data.get("rooms", {})
            result[wing_name] = {
                "description": wing_data.get("description", ""),
                "room_count": len(rooms),
                "rooms": list(rooms.keys()),
            }
        return result

    def list_rooms(self, wing: str) -> Dict[str, Any]:
        """Return rooms in a wing with descriptions and memory counts."""
        wings = self._config.get("wings", {})
        wing_data = wings.get(wing, {})
        rooms = wing_data.get("rooms", {})

        result = {}
        for room_name, room_data in rooms.items():
            # Get count for this room
            count = 0
            try:
                where_filter: Dict[str, Any] = {"$and": [{"wing": wing}, {"room": room_name}]}
                res = self._collection.get(where=where_filter, include=[])
                count = len(res.get("ids", []))
            except Exception:
                pass

            result[room_name] = {
                "description": room_data.get("description", ""),
                "keywords": room_data.get("keywords", []),
                "memory_count": count,
            }

        return result

    def room_stats(self) -> Dict[str, Any]:
        """Get memory counts by wing and room."""
        total = self._collection.count()
        PALACE_MEMORIES_TOTAL.set(total)

        wings = self._config.get("wings", {})
        by_wing: Dict[str, int] = {}

        for wing_name in wings:
            try:
                res = self._collection.get(where={"wing": wing_name}, include=[])
                by_wing[wing_name] = len(res.get("ids", []))
            except Exception:
                by_wing[wing_name] = 0

        return {
            "total": total,
            "by_wing": by_wing,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_where_filter(wing: str = "", room: str = "") -> Optional[Dict]:
        """Build a ChromaDB where filter from wing/room."""
        conditions = []
        if wing:
            conditions.append({"wing": wing})
        if room:
            conditions.append({"room": room})

        if not conditions:
            return None
        if len(conditions) == 1:
            return conditions[0]
        return {"$and": conditions}
