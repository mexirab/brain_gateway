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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.tests.fixtures_memory import real_palace_yaml


@pytest.fixture
def real_palace(memory_palace):
    """MemPalace from the REAL repo palace.yaml; Chroma + embedder faked."""
    return memory_palace(yaml_path=real_palace_yaml())


def _stored_meta(palace) -> dict:
    return palace._collection.add.call_args.kwargs["metadatas"][0]


# ---------------------------------------------------------------------------
# palace.yaml config guard
# ---------------------------------------------------------------------------


class TestSystemWingConfig:
    def test_real_yaml_defines_system_audit_without_keywords(self, real_palace):
        assert real_palace.is_known_wing("system")
        rooms = real_palace._config["wings"]["system"]["rooms"]
        assert "audit" in rooms
        for room_name, room in rooms.items():
            assert not room.get("keywords"), f"system/{room_name} must have no routing keywords"

    def test_no_routing_rule_targets_system(self, real_palace):
        assert real_palace._routing_rules, "real palace.yaml should have routing rules"
        assert all(r.get("wing") != "system" for r in real_palace._routing_rules)

    def test_existing_wings_still_present(self, real_palace):
        assert {"personal", "jess", "brain_gateway", "infrastructure"} <= real_palace.known_wings()


# ---------------------------------------------------------------------------
# store() keeps wing=system / room=audit
# ---------------------------------------------------------------------------


class TestStoreSystemWing:
    async def test_store_system_audit_keeps_wing_and_room(self, real_palace, caplog):
        with caplog.at_level(logging.WARNING, logger="orchestrator.mempalace"):
            doc_id = await real_palace.store(
                "Self-audit 2026-10-21: 3 clusters, 0 critical, services orchestrator",
                wing="system",
                room="audit",
                source="self_audit",
            )

        assert doc_id is not None
        assert doc_id.startswith("palace_system_audit_")
        real_palace._collection.add.assert_called_once()
        meta = _stored_meta(real_palace)
        assert (meta["wing"], meta["room"]) == ("system", "audit")
        # No "unknown wing → personal" fallback warning for the reserved wing.
        assert not [r for r in caplog.records if "unknown wing" in r.getMessage()]

    async def test_unknown_wing_still_falls_back_to_personal(self, real_palace):
        doc_id = await real_palace.store("Something filed under a bogus wing", wing="nonexistent_wing")
        assert doc_id is not None
        assert _stored_meta(real_palace)["wing"] == "personal"

    async def test_store_system_unknown_room_dropped(self, real_palace):
        await real_palace.store("Some operational note text", wing="system", room="bogus")
        meta = _stored_meta(real_palace)
        assert (meta["wing"], meta["room"]) == ("system", "")


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
    def test_ordinary_text_not_routed_to_system(self, real_palace, text):
        wing, _room = real_palace.route_to_room(text)
        assert wing != "system"

    def test_unmatched_text_defaults_to_personal(self, real_palace):
        assert real_palace.route_to_room("audit audit audit") == ("personal", "")

    def test_every_configured_keyword_routes_outside_system(self, real_palace):
        keywords = {
            kw
            for wing in real_palace._config["wings"].values()
            for room in wing.get("rooms", {}).values()
            for kw in room.get("keywords", [])
        }
        assert keywords
        for kw in keywords:
            wing, _ = real_palace.route_to_room(f"audit system {kw} audit")
            assert wing != "system", kw

    async def test_store_without_wing_never_lands_in_system(self, real_palace):
        await real_palace.store("audit system error audit facts summary")
        assert _stored_meta(real_palace)["wing"] != "system"


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


class TestPalaceStoreBodyValidation:
    @pytest.mark.parametrize("raw", ["[]", '["text"]', '"x"', "5", "null", "true"])
    def test_non_dict_body_400(self, palace_client, raw):
        client, fake_palace = palace_client
        r = client.post("/api/palace/store", content=raw, headers={"content-type": "application/json"})
        assert r.status_code == 400
        assert "error" in r.json()
        fake_palace.store.assert_not_called()

    @pytest.mark.parametrize("field", ["wing", "room", "source", "category", "confidence", "project", "session_id"])
    @pytest.mark.parametrize("value", [["system"], {"x": 1}, 5, 1.5, True, []])
    def test_non_string_field_400(self, palace_client, field, value):
        client, fake_palace = palace_client
        r = client.post("/api/palace/store", json={"text": "some memory", field: value})
        assert r.status_code == 400
        assert field in r.json()["error"]
        fake_palace.store.assert_not_called()

    def test_list_wing_system_cannot_bypass_reserved_check(self, palace_client):
        client, fake_palace = palace_client
        r = client.post("/api/palace/store", json={"text": "fake audit fact", "wing": ["system"], "room": "audit"})
        assert r.status_code == 400
        fake_palace.store.assert_not_called()

    @pytest.mark.parametrize("field", ["wing", "room", "source", "category", "confidence", "project", "session_id"])
    def test_null_field_still_stores(self, palace_client, field):
        client, fake_palace = palace_client
        r = client.post("/api/palace/store", json={"text": "auto-routed memory", field: None})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        fake_palace.store.assert_awaited_once()

    def test_string_fields_still_store(self, palace_client):
        client, fake_palace = palace_client
        body = {
            "text": "note",
            "wing": "personal",
            "room": "health",
            "source": "api",
            "category": "fact",
            "confidence": "high",
            "project": "jess",
            "session_id": "s1",
        }
        r = client.post("/api/palace/store", json=body)
        assert r.status_code == 200
        fake_palace.store.assert_awaited_once()


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


def test_store_malformed_json_is_400(palace_client):
    client, palace = palace_client
    resp = client.post("/api/palace/store", content=b"{not json", headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    palace.store.assert_not_called()


def test_store_null_fields_get_defaults(palace_client):
    client, palace = palace_client
    resp = client.post(
        "/api/palace/store", json={"text": "a memory", "source": None, "category": None, "room": None, "wing": None}
    )
    assert resp.status_code == 200
    kwargs = palace.store.call_args.kwargs
    assert kwargs["source"] == "api" and kwargs["category"] == "general" and kwargs["room"] == ""
    assert kwargs.get("wing") in (None, "")  # a null wing auto-routes
