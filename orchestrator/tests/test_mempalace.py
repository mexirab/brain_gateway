"""
Tests for mempalace.py — routing, dedup, store, search, wakeup context,
and structure queries.

Tests pure functions directly and mocks ChromaDB + embedding model for
integration-like tests.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import check helper
# ---------------------------------------------------------------------------


def _can_import_mempalace():
    try:
        from orchestrator import mempalace  # noqa: F401

        return True
    except (ImportError, ModuleNotFoundError):
        return False


_skip_no_deps = pytest.mark.skipif(
    not _can_import_mempalace(),
    reason="mempalace requires chromadb and full orchestrator dependencies",
)


# ---------------------------------------------------------------------------
# Routing tests (pure logic — no external deps)
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestRouteToRoom:
    """Test the keyword/regex routing logic."""

    def _make_palace(self):
        from orchestrator.mempalace import MemPalace

        # Patch config loading to use test data
        with patch("orchestrator.mempalace.shared") as mock_shared:
            mock_shared.PALACE_YAML_PATH = "/nonexistent"
            mock_shared.PALACE_ENABLED = True
            mock_shared.PALACE_WAKEUP_ENABLED = False
            mock_shared.PALACE_DEDUP_THRESHOLD = 0.85
            mock_shared.AUTO_LEARN_ENCRYPT = False
            palace = MemPalace()

        # Manually set config for testing
        import re

        palace._config = {
            "wings": {
                "personal": {
                    "description": "Personal",
                    "rooms": {
                        "health": {
                            "description": "Health",
                            "keywords": ["medication", "med", "health", "doctor", "adhd", "vyvanse"],
                        },
                        "routines": {
                            "description": "Routines",
                            "keywords": ["routine", "morning", "evening", "habit"],
                        },
                        "finance": {
                            "description": "Finance",
                            "keywords": ["budget", "money", "spend", "ynab"],
                        },
                    },
                },
                "brain_gateway": {
                    "description": "Brain Gateway",
                    "rooms": {
                        "architecture": {
                            "description": "Architecture",
                            "keywords": ["architecture", "design", "flow", "component"],
                        },
                        "debugging": {
                            "description": "Debugging",
                            "keywords": ["bug", "fix", "error", "broken"],
                        },
                    },
                },
                "infrastructure": {
                    "description": "Infrastructure",
                    "rooms": {
                        "cluster": {
                            "description": "Cluster",
                            "keywords": ["helios", "jupiter", "saturn", "gpu"],
                        },
                    },
                },
            },
            "routing_rules": [
                {
                    "pattern": r"\b(helios|jupiter|saturn|gpu)\b",
                    "wing": "infrastructure",
                    "room": "cluster",
                    "_compiled": re.compile(r"\b(helios|jupiter|saturn|gpu)\b", re.IGNORECASE),
                },
                {
                    "pattern": r"\b(medication|vyvanse|adhd)\b",
                    "wing": "personal",
                    "room": "health",
                    "_compiled": re.compile(r"\b(medication|vyvanse|adhd)\b", re.IGNORECASE),
                },
            ],
            "wakeup": {"enabled": False},
        }
        return palace

    def test_regex_routing_matches_first(self):
        palace = self._make_palace()
        wing, room = palace.route_to_room("Helios GPU temperature is high")
        assert wing == "infrastructure"
        assert room == "cluster"

    def test_regex_routing_health(self):
        palace = self._make_palace()
        wing, room = palace.route_to_room("Changed my Vyvanse dose to 40mg")
        assert wing == "personal"
        assert room == "health"

    def test_keyword_fallback_architecture(self):
        palace = self._make_palace()
        wing, room = palace.route_to_room("The architecture of the unified loop design is interesting")
        assert wing == "brain_gateway"
        assert room == "architecture"

    def test_keyword_fallback_finance(self):
        palace = self._make_palace()
        wing, room = palace.route_to_room("My YNAB budget shows I overspent on food")
        assert wing == "personal"
        assert room == "finance"

    def test_project_routing_conjure(self):
        palace = self._make_palace()
        wing, room = palace.route_to_room("Use checkpoint v1.5", project="conjure-api")
        assert wing == "conjure"

    def test_project_routing_brain_gateway(self):
        palace = self._make_palace()
        wing, room = palace.route_to_room("Some random text", project="brain-gateway")
        assert wing == "brain_gateway"

    def test_default_personal(self):
        palace = self._make_palace()
        wing, room = palace.route_to_room("I like pizza on Fridays")
        assert wing == "personal"


# ---------------------------------------------------------------------------
# Where filter builder tests
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestBuildWhereFilter:
    def test_no_filter(self):
        from orchestrator.mempalace import MemPalace

        assert MemPalace._build_where_filter() is None

    def test_wing_only(self):
        from orchestrator.mempalace import MemPalace

        f = MemPalace._build_where_filter(wing="personal")
        assert f == {"wing": "personal"}

    def test_room_only(self):
        from orchestrator.mempalace import MemPalace

        f = MemPalace._build_where_filter(room="health")
        assert f == {"room": "health"}

    def test_wing_and_room(self):
        from orchestrator.mempalace import MemPalace

        f = MemPalace._build_where_filter(wing="personal", room="health")
        assert f == {"$and": [{"wing": "personal"}, {"room": "health"}]}


# ---------------------------------------------------------------------------
# Structure query tests
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestStructureQueries:
    def _make_palace(self):
        from orchestrator.mempalace import MemPalace

        with patch("orchestrator.mempalace.shared") as mock_shared:
            mock_shared.PALACE_YAML_PATH = "/nonexistent"
            mock_shared.PALACE_ENABLED = True
            mock_shared.PALACE_WAKEUP_ENABLED = False
            palace = MemPalace()

        palace._config = {
            "wings": {
                "personal": {
                    "description": "Personal info",
                    "rooms": {
                        "health": {
                            "description": "Health stuff",
                            "keywords": ["health"],
                        },
                        "routines": {
                            "description": "Daily routines",
                            "keywords": ["routine"],
                        },
                    },
                },
                "brain_gateway": {
                    "description": "Brain Gateway project",
                    "rooms": {
                        "architecture": {
                            "description": "System design",
                            "keywords": ["architecture"],
                        },
                    },
                },
            },
            "routing_rules": [],
            "wakeup": {"enabled": False},
        }
        return palace

    def test_list_wings(self):
        palace = self._make_palace()
        wings = palace.list_wings()
        assert "personal" in wings
        assert "brain_gateway" in wings
        assert wings["personal"]["room_count"] == 2
        assert "health" in wings["personal"]["rooms"]

    def test_list_rooms_with_mock_collection(self):
        palace = self._make_palace()
        # Mock the collection to return counts
        mock_coll = MagicMock()
        mock_coll.get.return_value = {"ids": ["a", "b"]}
        with patch.object(palace, "_collection", mock_coll):
            rooms = palace.list_rooms("personal")
        assert "health" in rooms
        assert rooms["health"]["memory_count"] == 2
        assert rooms["health"]["description"] == "Health stuff"

    def test_list_rooms_unknown_wing(self):
        palace = self._make_palace()
        rooms = palace.list_rooms("nonexistent")
        assert rooms == {}


# ---------------------------------------------------------------------------
# Store + dedup tests (mocked ChromaDB)
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestStoreAndDedup:
    def _make_palace_with_mocks(self):
        from orchestrator.mempalace import MemPalace

        with patch("orchestrator.mempalace.shared") as mock_shared:
            mock_shared.PALACE_YAML_PATH = "/nonexistent"
            mock_shared.PALACE_ENABLED = True
            mock_shared.PALACE_WAKEUP_ENABLED = False
            mock_shared.PALACE_DEDUP_THRESHOLD = 0.85
            mock_shared.AUTO_LEARN_ENCRYPT = False
            mock_shared.embedding_model = MagicMock()
            mock_shared.embedding_model.encode.return_value = MagicMock(tolist=MagicMock(return_value=[0.1] * 384))
            palace = MemPalace()

        palace._config = {
            "wings": {
                "personal": {
                    "description": "Personal",
                    "rooms": {
                        "health": {"description": "Health", "keywords": ["health", "medication"]},
                    },
                },
            },
            "routing_rules": [],
            "wakeup": {"enabled": False},
        }

        mock_coll = MagicMock()
        # Default: no duplicates found
        mock_coll.query.return_value = {
            "documents": [[]],
            "distances": [[]],
            "ids": [[]],
        }
        mock_coll.add.return_value = None

        return palace, mock_coll, mock_shared

    def test_store_success(self):
        palace, mock_coll, mock_shared = self._make_palace_with_mocks()

        with (
            patch.object(palace, "_collection", mock_coll),
            patch("orchestrator.mempalace.encrypt_text", side_effect=lambda x: x),
            patch("orchestrator.mempalace.shared", mock_shared),
        ):
            doc_id = asyncio.get_event_loop().run_until_complete(
                palace.store("Takes Vyvanse 40mg daily", wing="personal", room="health")
            )

        assert doc_id is not None
        assert doc_id.startswith("palace_personal_health_")
        mock_coll.add.assert_called_once()

    def test_store_too_short(self):
        palace, mock_coll, mock_shared = self._make_palace_with_mocks()

        with patch.object(palace, "_collection", mock_coll), patch("orchestrator.mempalace.shared", mock_shared):
            doc_id = asyncio.get_event_loop().run_until_complete(palace.store("hi"))

        assert doc_id is None

    def test_store_empty(self):
        palace, mock_coll, mock_shared = self._make_palace_with_mocks()

        with patch.object(palace, "_collection", mock_coll), patch("orchestrator.mempalace.shared", mock_shared):
            doc_id = asyncio.get_event_loop().run_until_complete(palace.store(""))

        assert doc_id is None

    def test_dedup_blocks_duplicate(self):
        palace, mock_coll, mock_shared = self._make_palace_with_mocks()

        # Simulate a near-duplicate (cosine similarity > 0.85 means distance < 0.15)
        mock_coll.query.return_value = {
            "documents": [["Takes Vyvanse 40mg daily"]],
            "distances": [[0.05]],  # cos_sim = 0.95
            "ids": [["palace_existing"]],
        }

        with (
            patch.object(palace, "_collection", mock_coll),
            patch("orchestrator.mempalace.decrypt_text", side_effect=lambda x: x),
            patch("orchestrator.mempalace.encrypt_text", side_effect=lambda x: x),
            patch("orchestrator.mempalace.shared", mock_shared),
        ):
            doc_id = asyncio.get_event_loop().run_until_complete(
                palace.store("Takes Vyvanse 40mg daily", wing="personal", room="health")
            )

        assert doc_id is None  # Blocked by dedup


# ---------------------------------------------------------------------------
# Search tests (mocked ChromaDB)
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestSearch:
    def test_search_returns_results(self):
        from orchestrator.mempalace import MemPalace

        with patch("orchestrator.mempalace.shared") as mock_shared:
            mock_shared.PALACE_YAML_PATH = "/nonexistent"
            mock_shared.PALACE_ENABLED = True
            mock_shared.PALACE_WAKEUP_ENABLED = False
            mock_shared.embedding_model = MagicMock()
            mock_shared.embedding_model.encode.return_value = MagicMock(tolist=MagicMock(return_value=[0.1] * 384))
            palace = MemPalace()

        palace._config = {"wings": {}, "routing_rules": [], "wakeup": {"enabled": False}}

        mock_coll = MagicMock()
        mock_coll.query.return_value = {
            "documents": [["Takes Vyvanse 40mg", "Prefers step-by-step"]],
            "distances": [[0.1, 0.3]],
            "ids": [["palace_1", "palace_2"]],
            "metadatas": [
                [
                    {
                        "wing": "personal",
                        "room": "health",
                        "source": "auto_learn",
                        "category": "health",
                        "confidence": "high",
                        "created_at": "2026-04-12T10:00:00",
                    },
                    {
                        "wing": "jess",
                        "room": "preferences",
                        "source": "manual",
                        "category": "preference",
                        "confidence": "high",
                        "created_at": "2026-04-11T10:00:00",
                    },
                ]
            ],
        }

        with (
            patch.object(palace, "_collection", mock_coll),
            patch("orchestrator.mempalace.decrypt_text", side_effect=lambda x: x),
            patch("orchestrator.mempalace.shared", mock_shared),
        ):
            results = asyncio.get_event_loop().run_until_complete(palace.search("medication"))

        assert len(results) == 2
        assert results[0]["text"] == "Takes Vyvanse 40mg"
        assert results[0]["wing"] == "personal"
        assert results[0]["score"] == 0.9

    def test_search_empty_query(self):
        from orchestrator.mempalace import MemPalace

        with patch("orchestrator.mempalace.shared") as mock_shared:
            mock_shared.PALACE_YAML_PATH = "/nonexistent"
            palace = MemPalace()

        palace._config = {"wings": {}, "routing_rules": [], "wakeup": {"enabled": False}}

        with patch("orchestrator.mempalace.shared", mock_shared):
            results = asyncio.get_event_loop().run_until_complete(palace.search(""))

        assert results == []


# ---------------------------------------------------------------------------
# CRUD tests
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestCRUD:
    def _make_palace(self):
        from orchestrator.mempalace import MemPalace

        with patch("orchestrator.mempalace.shared") as mock_shared:
            mock_shared.PALACE_YAML_PATH = "/nonexistent"
            mock_shared.PALACE_ENABLED = True
            palace = MemPalace()

        palace._config = {"wings": {}, "routing_rules": [], "wakeup": {"enabled": False}}
        return palace

    def test_get_by_id_found(self):
        palace = self._make_palace()
        mock_coll = MagicMock()
        mock_coll.get.return_value = {
            "documents": ["Test memory"],
            "metadatas": [
                {
                    "wing": "personal",
                    "room": "health",
                    "source": "manual",
                    "category": "health",
                    "confidence": "high",
                    "created_at": "2026-04-12",
                    "updated_at": "2026-04-12",
                    "project": "",
                    "session_id": "",
                }
            ],
        }

        with (
            patch.object(palace, "_collection", mock_coll),
            patch("orchestrator.mempalace.decrypt_text", side_effect=lambda x: x),
        ):
            result = palace.get_by_id("palace_personal_health_123")

        assert result is not None
        assert result["text"] == "Test memory"
        assert result["wing"] == "personal"

    def test_get_by_id_not_palace(self):
        palace = self._make_palace()
        assert palace.get_by_id("autolearn_123") is None

    def test_delete_success(self):
        palace = self._make_palace()
        mock_coll = MagicMock()

        with patch.object(palace, "_collection", mock_coll):
            ok = palace.delete("palace_personal_health_123")

        assert ok is True
        mock_coll.delete.assert_called_once_with(ids=["palace_personal_health_123"])

    def test_delete_not_palace(self):
        palace = self._make_palace()
        assert palace.delete("autolearn_123") is False


# ---------------------------------------------------------------------------
# Wakeup context tests
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestWakeupContext:
    def test_wakeup_disabled(self):
        from orchestrator.mempalace import MemPalace

        with patch("orchestrator.mempalace.shared") as mock_shared:
            mock_shared.PALACE_YAML_PATH = "/nonexistent"
            palace = MemPalace()

        palace._config = {"wings": {}, "routing_rules": [], "wakeup": {"enabled": False}}
        assert palace.generate_wakeup_context() == ""

    def test_wakeup_cache(self):
        from orchestrator.mempalace import MemPalace

        with patch("orchestrator.mempalace.shared") as mock_shared:
            mock_shared.PALACE_YAML_PATH = "/nonexistent"
            palace = MemPalace()

        palace._config = {
            "wings": {},
            "routing_rules": [],
            "wakeup": {"enabled": True, "refresh_interval_minutes": 30, "priority_rooms": []},
        }
        import time

        palace._wakeup_cache = "cached content"
        palace._wakeup_cache_time = time.time()
        assert palace.generate_wakeup_context() == "cached content"

    def test_wakeup_invalidation(self):
        from orchestrator.mempalace import MemPalace

        with patch("orchestrator.mempalace.shared") as mock_shared:
            mock_shared.PALACE_YAML_PATH = "/nonexistent"
            palace = MemPalace()

        palace._wakeup_cache = "old"
        palace._wakeup_cache_time = 999.0
        palace._invalidate_wakeup_cache()
        assert palace._wakeup_cache is None
        assert palace._wakeup_cache_time == 0.0
