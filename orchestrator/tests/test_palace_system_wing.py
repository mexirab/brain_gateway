"""Tests for the reserved `system` palace wing and related self-monitoring cleanups.

Covers:
- data/palace.yaml defines wing `system` / room `audit` with no keywords, so
  `MemPalace.store(wing="system", room="audit")` keeps it (previously fell back
  to "personal") and `route_to_room()` never auto-files user text there.
  These tests load the REAL repo palace.yaml so the config itself is guarded.
- POST /api/palace/store rejects the reserved `system` wing with 403.
- reminder_manager._send_notification with no mobile services logs at DEBUG,
  not WARNING.
"""

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Real palace.yaml loading
# ---------------------------------------------------------------------------

_PALACE_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "data" / "palace.yaml",  # repo checkout / CI
    Path("/app/data_repo/palace.yaml"),  # throwaway test container mount
    Path("/app/config/palace.yaml"),  # orchestrator image layout
]


def _real_palace_yaml() -> Path:
    for p in _PALACE_CANDIDATES:
        if p.is_file():
            return p
    pytest.fail(f"repo data/palace.yaml not found in any of {_PALACE_CANDIDATES}")


def _make_real_palace():
    """Build a MemPalace from the real palace.yaml with ChromaDB + embeddings mocked."""
    from orchestrator.mempalace import MemPalace

    mock_shared = MagicMock()
    mock_shared.PALACE_YAML_PATH = str(_real_palace_yaml())
    mock_shared.PALACE_ENABLED = True
    mock_shared.PALACE_WAKEUP_ENABLED = False
    mock_shared.PALACE_DEDUP_THRESHOLD = 0.85
    mock_shared.AUTO_LEARN_ENCRYPT = False
    mock_shared.embedding_model = MagicMock()
    mock_shared.embedding_model.encode.return_value = MagicMock(tolist=MagicMock(return_value=[0.1] * 384))

    with patch("orchestrator.mempalace.shared", mock_shared):
        palace = MemPalace()

    mock_coll = MagicMock()
    mock_coll.query.return_value = {"documents": [[]], "distances": [[]], "ids": [[]], "metadatas": [[]]}
    mock_coll.add.return_value = None
    return palace, mock_coll, mock_shared


# ---------------------------------------------------------------------------
# palace.yaml config guard
# ---------------------------------------------------------------------------


class TestSystemWingConfig:
    def test_real_yaml_defines_system_audit(self):
        palace, _, _ = _make_real_palace()
        assert palace.is_known_wing("system")
        rooms = palace._config["wings"]["system"]["rooms"]
        assert "audit" in rooms

    def test_system_audit_has_no_keywords(self):
        palace, _, _ = _make_real_palace()
        rooms = palace._config["wings"]["system"]["rooms"]
        for room_name, room in rooms.items():
            assert not room.get("keywords"), f"system/{room_name} must have no routing keywords"

    def test_no_routing_rule_targets_system(self):
        palace, _, _ = _make_real_palace()
        assert palace._routing_rules, "real palace.yaml should have routing rules"
        assert all(r.get("wing") != "system" for r in palace._routing_rules)

    def test_existing_wings_still_present(self):
        palace, _, _ = _make_real_palace()
        assert {"personal", "jess", "brain_gateway", "infrastructure"} <= palace.known_wings()


# ---------------------------------------------------------------------------
# store() keeps wing=system / room=audit
# ---------------------------------------------------------------------------


class TestStoreSystemWing:
    async def test_store_system_audit_keeps_wing_and_room(self):
        palace, mock_coll, mock_shared = _make_real_palace()
        with (
            patch.object(palace, "_collection", mock_coll),
            patch("orchestrator.mempalace.encrypt_text", side_effect=lambda x: x),
            patch("orchestrator.mempalace.shared", mock_shared),
        ):
            doc_id = await palace.store(
                "Self-audit 2026-10-21: 3 clusters, 0 critical, services orchestrator",
                wing="system",
                room="audit",
                source="self_audit",
            )

        assert doc_id is not None
        assert doc_id.startswith("palace_system_audit_")
        mock_coll.add.assert_called_once()
        meta = mock_coll.add.call_args.kwargs["metadatas"][0]
        assert meta["wing"] == "system"
        assert meta["room"] == "audit"

    async def test_store_system_does_not_log_unknown_wing_fallback(self, caplog):
        palace, mock_coll, mock_shared = _make_real_palace()
        with (
            caplog.at_level(logging.WARNING, logger="orchestrator.mempalace"),
            patch.object(palace, "_collection", mock_coll),
            patch("orchestrator.mempalace.encrypt_text", side_effect=lambda x: x),
            patch("orchestrator.mempalace.shared", mock_shared),
        ):
            await palace.store("Self-audit facts line for the day", wing="system", room="audit")

        assert not [r for r in caplog.records if "unknown wing" in r.getMessage()]

    async def test_unknown_wing_still_falls_back_to_personal(self):
        palace, mock_coll, mock_shared = _make_real_palace()
        with (
            patch.object(palace, "_collection", mock_coll),
            patch("orchestrator.mempalace.encrypt_text", side_effect=lambda x: x),
            patch("orchestrator.mempalace.shared", mock_shared),
        ):
            doc_id = await palace.store("Something filed under a bogus wing", wing="nonexistent_wing")

        assert doc_id is not None
        meta = mock_coll.add.call_args.kwargs["metadatas"][0]
        assert meta["wing"] == "personal"

    async def test_store_system_unknown_room_dropped(self):
        palace, mock_coll, mock_shared = _make_real_palace()
        with (
            patch.object(palace, "_collection", mock_coll),
            patch("orchestrator.mempalace.encrypt_text", side_effect=lambda x: x),
            patch("orchestrator.mempalace.shared", mock_shared),
        ):
            await palace.store("Some operational note text", wing="system", room="bogus")

        meta = mock_coll.add.call_args.kwargs["metadatas"][0]
        assert meta["wing"] == "system"
        assert meta["room"] == ""


# ---------------------------------------------------------------------------
# route_to_room() never picks system
# ---------------------------------------------------------------------------


class TestRoutingNeverPicksSystem:
    @pytest.mark.parametrize(
        "text",
        [
            "audit system error",
            "system audit audit audit system system",
            "The self-audit found a system error in the audit log",
            "audit",
            "",
            "xyzzy plugh nothing matches here",
            "Daily self-audit facts: date, counts, services",
        ],
    )
    def test_ordinary_text_not_routed_to_system(self, text):
        palace, _, _ = _make_real_palace()
        wing, _room = palace.route_to_room(text)
        assert wing != "system"

    def test_unmatched_text_defaults_to_personal(self):
        palace, _, _ = _make_real_palace()
        assert palace.route_to_room("audit audit audit") == ("personal", "")

    def test_every_configured_keyword_routes_outside_system(self):
        palace, _, _ = _make_real_palace()
        keywords = {
            kw
            for wing in palace._config["wings"].values()
            for room in wing.get("rooms", {}).values()
            for kw in room.get("keywords", [])
        }
        assert keywords
        for kw in keywords:
            wing, _ = palace.route_to_room(f"audit system {kw} audit")
            assert wing != "system", kw

    async def test_store_without_wing_never_lands_in_system(self):
        palace, mock_coll, mock_shared = _make_real_palace()
        with (
            patch.object(palace, "_collection", mock_coll),
            patch("orchestrator.mempalace.encrypt_text", side_effect=lambda x: x),
            patch("orchestrator.mempalace.shared", mock_shared),
        ):
            await palace.store("audit system error audit facts summary")

        meta = mock_coll.add.call_args.kwargs["metadatas"][0]
        assert meta["wing"] != "system"


# ---------------------------------------------------------------------------
# POST /api/palace/store reserved-wing gate
# ---------------------------------------------------------------------------


@pytest.fixture
def palace_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator import routes_palace, shared

    monkeypatch.setattr(shared, "PALACE_ENABLED", True)
    fake_palace = MagicMock()
    fake_palace.store = AsyncMock(return_value="palace_personal_health_20261021_abcd1234")
    monkeypatch.setattr(shared, "get_palace", lambda: fake_palace)

    app = FastAPI()
    app.include_router(routes_palace.router)
    return TestClient(app), fake_palace


class TestPalaceStoreRoute:
    @pytest.mark.parametrize("wing", ["system", " System ", "SYSTEM", "\tsystem\n"])
    def test_reserved_wing_rejected(self, palace_client, wing):
        client, fake_palace = palace_client
        r = client.post("/api/palace/store", json={"text": "fake audit fact", "wing": wing, "room": "audit"})
        assert r.status_code == 403
        assert "reserved" in r.json()["error"]
        fake_palace.store.assert_not_called()

    def test_personal_wing_still_stores(self, palace_client):
        client, fake_palace = palace_client
        r = client.post("/api/palace/store", json={"text": "likes oat milk", "wing": "personal"})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "id": "palace_personal_health_20261021_abcd1234"}
        fake_palace.store.assert_awaited_once()
        assert fake_palace.store.call_args.kwargs["wing"] == "personal"

    def test_no_wing_still_stores(self, palace_client):
        client, fake_palace = palace_client
        r = client.post("/api/palace/store", json={"text": "auto-routed memory"})
        assert r.status_code == 200
        fake_palace.store.assert_awaited_once()

    def test_wing_containing_system_substring_not_blocked(self, palace_client):
        client, fake_palace = palace_client
        r = client.post("/api/palace/store", json={"text": "some note", "wing": "systems_design"})
        assert r.status_code == 200
        fake_palace.store.assert_awaited_once()

    def test_missing_text_400(self, palace_client):
        client, fake_palace = palace_client
        r = client.post("/api/palace/store", json={"wing": "personal"})
        assert r.status_code == 400
        assert r.json() == {"error": "text is required"}
        fake_palace.store.assert_not_called()

    def test_missing_text_with_reserved_wing_400(self, palace_client):
        # Text check runs first; order preserved from before the change.
        client, fake_palace = palace_client
        r = client.post("/api/palace/store", json={"text": "  ", "wing": "system"})
        assert r.status_code == 400
        fake_palace.store.assert_not_called()

    def test_duplicate_returns_409(self, palace_client):
        client, fake_palace = palace_client
        fake_palace.store.return_value = None
        r = client.post("/api/palace/store", json={"text": "dup memory", "wing": "personal"})
        assert r.status_code == 409
        assert r.json()["ok"] is False

    def test_palace_disabled_503(self, palace_client, monkeypatch):
        from orchestrator import shared

        client, fake_palace = palace_client
        monkeypatch.setattr(shared, "PALACE_ENABLED", False)
        r = client.post("/api/palace/store", json={"text": "x memory", "wing": "system"})
        assert r.status_code == 503
        fake_palace.store.assert_not_called()


# ---------------------------------------------------------------------------
# reminder_manager._send_notification with no mobile services
# ---------------------------------------------------------------------------


class TestSendNotificationUnconfigured:
    async def test_returns_error_and_logs_no_warning(self, monkeypatch, caplog):
        from orchestrator import reminder_manager

        monkeypatch.setattr(reminder_manager, "MOBILE_NOTIFY_SERVICES", [])
        with caplog.at_level(logging.DEBUG):
            result = await reminder_manager._send_notification("take a break")

        assert result == {"success": False, "error": "No mobile notification services configured"}
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        debug = [r for r in caplog.records if r.levelno == logging.DEBUG and r.name == reminder_manager.logger.name]
        assert any("not configured" in r.getMessage() for r in debug)

    async def test_no_http_call_when_unconfigured(self, monkeypatch):
        from orchestrator import reminder_manager

        monkeypatch.setattr(reminder_manager, "MOBILE_NOTIFY_SERVICES", [])
        with patch("httpx.AsyncClient") as mock_client:
            await reminder_manager._send_notification("hello")
        mock_client.assert_not_called()
