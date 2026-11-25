"""
Tests for mempalace.py — regex/project routing, where filters, structure
queries, store + dedup, search, CRUD and the disabled wakeup block.

ChromaDB and the embedder are faked by the ``memory_palace`` fixture
(fixtures_memory.py). Whole-word keyword routing, route_fact and the wakeup
block's limits, ordering and cache live in test_memory_recall.py.
"""

import re

import pytest

from orchestrator.mempalace import MemPalace

ROUTING_CONFIG = {
    "wings": {
        "personal": {
            "description": "Personal",
            "rooms": {
                "health": {
                    "description": "Health",
                    "keywords": ["medication", "med", "health", "doctor", "adhd", "vyvanse"],
                },
                "routines": {"description": "Routines", "keywords": ["routine", "morning", "evening", "habit"]},
                "finance": {"description": "Finance", "keywords": ["budget", "money", "spend"]},
            },
        },
        "brain_gateway": {
            "description": "Brain Gateway",
            "rooms": {
                "architecture": {
                    "description": "Architecture",
                    "keywords": ["architecture", "design", "flow", "component"],
                },
                "debugging": {"description": "Debugging", "keywords": ["bug", "fix", "error", "broken"]},
            },
        },
        "infrastructure": {
            "description": "Infrastructure",
            "rooms": {"cluster": {"description": "Cluster", "keywords": ["helios", "jupiter", "saturn", "gpu"]}},
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

EMPTY_CONFIG = {"wings": {}, "routing_rules": [], "wakeup": {"enabled": False}}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,project,wing,room",
    [
        pytest.param("Helios GPU temperature is high", "", "infrastructure", "cluster", id="regex-first"),
        pytest.param("Changed my Vyvanse dose to 40mg", "", "personal", "health", id="regex-health"),
        pytest.param(
            "The architecture of the unified loop design is interesting",
            "",
            "brain_gateway",
            "architecture",
            id="keyword-architecture",
        ),
        pytest.param("My budget shows I overspent on food", "", "personal", "finance", id="keyword-finance"),
        pytest.param("Use checkpoint v1.5", "conjure-api", "conjure", None, id="project-conjure"),
        pytest.param("Some random text", "brain-gateway", "brain_gateway", None, id="project-brain-gateway"),
        pytest.param("I like pizza on Fridays", "", "personal", None, id="default-personal"),
    ],
)
def test_route_to_room(memory_palace, text, project, wing, room):
    palace = memory_palace(ROUTING_CONFIG)
    got_wing, got_room = palace.route_to_room(text, project=project)
    assert got_wing == wing
    if room is not None:
        assert got_room == room


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        pytest.param({}, None, id="none"),
        pytest.param({"wing": "personal"}, {"wing": "personal"}, id="wing"),
        pytest.param({"room": "health"}, {"room": "health"}, id="room"),
        pytest.param(
            {"wing": "personal", "room": "health"}, {"$and": [{"wing": "personal"}, {"room": "health"}]}, id="both"
        ),
    ],
)
def test_build_where_filter(kwargs, expected):
    assert MemPalace._build_where_filter(**kwargs) == expected


# ---------------------------------------------------------------------------
# Structure queries
# ---------------------------------------------------------------------------

STRUCTURE_CONFIG = {
    "wings": {
        "personal": {
            "description": "Personal info",
            "rooms": {
                "health": {"description": "Health stuff", "keywords": ["health"]},
                "routines": {"description": "Daily routines", "keywords": ["routine"]},
            },
        },
        "brain_gateway": {
            "description": "Brain Gateway project",
            "rooms": {"architecture": {"description": "System design", "keywords": ["architecture"]}},
        },
    },
    "routing_rules": [],
    "wakeup": {"enabled": False},
}


def test_list_wings(memory_palace):
    wings = memory_palace(STRUCTURE_CONFIG).list_wings()
    assert "personal" in wings
    assert "brain_gateway" in wings
    assert wings["personal"]["room_count"] == 2
    assert "health" in wings["personal"]["rooms"]


def test_list_rooms_counts_from_collection(memory_palace):
    palace = memory_palace(STRUCTURE_CONFIG)
    palace._collection.get.return_value = {"ids": ["a", "b"]}
    rooms = palace.list_rooms("personal")
    assert rooms["health"]["memory_count"] == 2
    assert rooms["health"]["description"] == "Health stuff"


def test_list_rooms_unknown_wing(memory_palace):
    assert memory_palace(STRUCTURE_CONFIG).list_rooms("nonexistent") == {}


# ---------------------------------------------------------------------------
# Store + dedup
# ---------------------------------------------------------------------------

HEALTH_CONFIG = {
    "wings": {
        "personal": {
            "description": "Personal",
            "rooms": {"health": {"description": "Health", "keywords": ["health", "medication"]}},
        },
    },
    "routing_rules": [],
    "wakeup": {"enabled": False},
}


async def test_store_success(memory_palace):
    palace = memory_palace(HEALTH_CONFIG)
    doc_id = await palace.store("Takes Vyvanse 40mg daily", wing="personal", room="health")
    assert doc_id is not None
    assert doc_id.startswith("palace_personal_health_")
    palace._collection.add.assert_called_once()


@pytest.mark.parametrize("text", [pytest.param("hi", id="too-short"), pytest.param("", id="empty")])
async def test_store_refuses_short_text(memory_palace, text):
    palace = memory_palace(HEALTH_CONFIG)
    assert await palace.store(text) is None
    palace._collection.add.assert_not_called()


async def test_dedup_blocks_duplicate(memory_palace):
    palace = memory_palace(HEALTH_CONFIG)
    # ChromaDB returns squared L2 distance; cos_sim = 1 - dist/2 for normalized
    # vectors, so dist=0.05 → 0.975, above the 0.85 dedup threshold.
    palace._collection.query.return_value = {
        "documents": [["Takes Vyvanse 40mg daily"]],
        "distances": [[0.05]],
        "ids": [["palace_existing"]],
        "metadatas": [[{"wing": "personal", "room": "health"}]],
    }
    assert await palace.store("Takes Vyvanse 40mg daily", wing="personal", room="health") is None
    palace._collection.add.assert_not_called()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


async def test_search_returns_results(memory_palace):
    palace = memory_palace(EMPTY_CONFIG)
    palace._collection.query.return_value = {
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
    results = await palace.search("medication")
    assert len(results) == 2
    assert results[0]["text"] == "Takes Vyvanse 40mg"
    assert results[0]["wing"] == "personal"
    # Squared L2 distance 0.1 on unit vectors → cos 0.95 (see commit d0d33fd).
    assert results[0]["score"] == 0.95


async def test_search_empty_query(memory_palace):
    palace = memory_palace(EMPTY_CONFIG)
    assert await palace.search("") == []
    palace._collection.query.assert_not_called()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_get_by_id_found(memory_palace):
    palace = memory_palace(EMPTY_CONFIG)
    palace._collection.get.return_value = {
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
    result = palace.get_by_id("palace_personal_health_123")
    assert result is not None
    assert result["text"] == "Test memory"
    assert result["wing"] == "personal"


def test_get_by_id_not_palace(memory_palace):
    assert memory_palace(EMPTY_CONFIG).get_by_id("autolearn_123") is None


def test_delete_success(memory_palace):
    palace = memory_palace(EMPTY_CONFIG)
    assert palace.delete("palace_personal_health_123") is True
    palace._collection.delete.assert_called_once_with(ids=["palace_personal_health_123"])


def test_delete_not_palace(memory_palace):
    palace = memory_palace(EMPTY_CONFIG)
    assert palace.delete("autolearn_123") is False
    palace._collection.delete.assert_not_called()


# ---------------------------------------------------------------------------
# Wakeup context (enabled path: test_memory_recall.py TestWakeupContext)
# ---------------------------------------------------------------------------


def test_wakeup_disabled(memory_palace):
    assert memory_palace(EMPTY_CONFIG).generate_wakeup_context() == ""
