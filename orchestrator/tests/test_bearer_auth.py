"""Bearer auth: case-insensitive scheme (RFC 7235), constant-time token compare."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.bearer_auth import bearer_token_matches

TOKEN = "test-bearer-token-0123456789"  # nosec B105 — test-only constant


class TestBearerTokenMatches:
    @pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "BeArEr"])
    def test_scheme_is_case_insensitive(self, scheme):
        assert bearer_token_matches(f"{scheme} {TOKEN}", TOKEN)

    def test_extra_spaces_before_token_tolerated(self):
        assert bearer_token_matches(f"Bearer   {TOKEN}", TOKEN)

    @pytest.mark.parametrize(
        "header",
        [
            "",
            "Bearer",
            "Bearer ",
            TOKEN,
            f"Basic {TOKEN}",
            f"Bearerx {TOKEN}",
            f"Bearer {TOKEN}x",
            f"Bearer {TOKEN[:-1]}",
            f"Bearer {TOKEN.upper()}",  # token itself stays case-sensitive
        ],
    )
    def test_rejects_wrong_or_malformed(self, header):
        assert not bearer_token_matches(header, TOKEN)

    def test_empty_expected_token_never_matches(self):
        assert not bearer_token_matches("Bearer ", "")
        assert not bearer_token_matches("Bearer anything", "")

    def test_uses_constant_time_compare(self):
        with patch("orchestrator.bearer_auth.hmac.compare_digest", return_value=True) as cd:
            assert bearer_token_matches("bearer whatever", TOKEN)
        cd.assert_called_once()

    def test_non_ascii_token_does_not_raise(self):
        assert not bearer_token_matches("Bearer tökén", TOKEN)


@pytest.fixture
def client(monkeypatch):
    import orchestrator.orchestrator as orch

    monkeypatch.setattr(orch, "API_TOKEN", TOKEN)
    app = FastAPI()
    app.add_middleware(orch.BearerAuthMiddleware)

    @app.get("/api/private")
    async def private():
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"ok": True}

    return TestClient(app)


class TestBearerAuthMiddleware:
    @pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
    def test_accepts_any_scheme_case(self, client, scheme):
        r = client.get("/api/private", headers={"Authorization": f"{scheme} {TOKEN}"})
        assert r.status_code == 200

    def test_rejects_wrong_token(self, client):
        r = client.get("/api/private", headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401

    def test_rejects_missing_header(self, client):
        assert client.get("/api/private").status_code == 401

    def test_rejects_other_scheme(self, client):
        r = client.get("/api/private", headers={"Authorization": f"Basic {TOKEN}"})
        assert r.status_code == 401

    def test_public_path_needs_no_token(self, client):
        assert client.get("/health").status_code == 200


@pytest.fixture
def health_client(monkeypatch):
    """The real api_routes /health handler on a bare app (no middleware, no
    startup), with every dependency of the authenticated branch stubbed so no
    model/HA/network call can happen."""
    from unittest.mock import AsyncMock, MagicMock

    from orchestrator import api_routes
    from orchestrator.config import settings

    monkeypatch.setattr(settings, "api_token", TOKEN)
    sched = MagicMock()
    sched.get_jobs.return_value = []
    sched.running = True
    sched.timezone = "UTC"
    monkeypatch.setattr(api_routes, "scheduler", sched)
    coll = MagicMock()
    coll.count.return_value = 0
    monkeypatch.setattr(api_routes, "collection", coll)
    monkeypatch.setattr(api_routes, "ha_client", MagicMock(_entities={}))
    monkeypatch.setattr(api_routes, "current_focus_session", {"active": False})
    monkeypatch.setattr(api_routes.calendar_sources, "health", lambda: {"configured": False})
    monkeypatch.setattr(api_routes, "check_model_health", AsyncMock(return_value=False))
    http = MagicMock()
    http.get = AsyncMock(side_effect=RuntimeError("no network in tests"))
    monkeypatch.setattr(api_routes.shared, "_http", http)
    monkeypatch.setattr("orchestrator.tool_definitions.get_all_tools", lambda: [])

    app = FastAPI()
    app.add_api_route("/health", api_routes.health, methods=["GET"])
    return TestClient(app)


MINIMAL_HEALTH = {"ok": True, "version": "7.0", "architecture": "unified"}


class TestHealthDetailGate:
    @pytest.mark.parametrize("scheme", ["Bearer", "bearer"])
    def test_valid_token_any_scheme_case_gets_full_details(self, health_client, scheme):
        r = health_client.get("/health", headers={"Authorization": f"{scheme} {TOKEN}"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert "primary_status" in body and "tools" in body and "scheduler" in body

    @pytest.mark.parametrize("header", [None, "Bearer nope", f"Basic {TOKEN}", "Bearer "])
    def test_wrong_or_missing_token_gets_minimal_body(self, health_client, header):
        headers = {"Authorization": header} if header is not None else {}
        r = health_client.get("/health", headers=headers)
        assert r.status_code == 200
        assert r.json() == MINIMAL_HEALTH

    def test_unset_token_never_unlocks_details(self, health_client, monkeypatch):
        from orchestrator.config import settings

        monkeypatch.setattr(settings, "api_token", "")
        r = health_client.get("/health", headers={"Authorization": "Bearer "})
        assert r.json() == MINIMAL_HEALTH

    def test_gate_reads_settings_not_os_environ(self, health_client, monkeypatch):
        # A different API_TOKEN in os.environ must not unlock details.
        monkeypatch.setenv("API_TOKEN", "env-only-token-xyz")
        r = health_client.get("/health", headers={"Authorization": "Bearer env-only-token-xyz"})
        assert r.json() == MINIMAL_HEALTH
