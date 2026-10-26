"""Bearer auth: case-insensitive scheme (RFC 7235), constant-time token compare,
the one-segment charset for tokenless paths, and CORS preflight pass-through."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.bearer_auth import bearer_token_matches, is_public_segment

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


class TestIsPublicSegment:
    """The one segment a tokenless caller may put after a public prefix."""

    @pytest.mark.parametrize(
        "segment",
        [
            "3f9a1c2e",  # uuid4()[:8] reminder id
            "0f8e7d6c-5b4a-4392-8170-6f5e4d3c2b1a",  # full uuid4
            "rec_0123456789ab_1767225600",  # recurring rule occurrence
            "mp-0123456789",  # meal-plan cooking reminder
            "pc-abcdef0123",  # pantry fridge check
            "3f9a1c2e.wav",  # speaker audio clip
            "ABC",
            "a.b.c",
            "_",
            "-",
        ],
    )
    def test_accepts_minted_ids_and_filenames(self, segment):
        assert is_public_segment(segment)

    @pytest.mark.parametrize(
        "segment",
        [
            "",
            ".",
            "..",
            "/",
            "a/b",
            "../x",
            "a\\b",
            "%2e%2e",  # still-encoded: a later decoder would see ".."
            "a%2fb",
            "a b",
            "a\tb",
            "abc\n",  # a trailing newline slips past a `^...$` regex, not fullmatch
            "\nabc",
            "a\rb",
            "a\x00",
            "a?b",
            "a#b",
            "a;b",  # path parameters
            "a:b",
            "a+b",
            "a~b",
            "a@b",
            "caf\u00e9",
            "\uff0e\uff0e",  # fullwidth full stops
            "\u0661\u0662",  # Arabic-Indic digits: \d would accept them, [0-9] must not
            "\u212a",  # Kelvin sign: [A-Za-z] under re.IGNORECASE would accept it
        ],
    )
    def test_rejects_traversal_separators_and_non_ascii(self, segment):
        assert not is_public_segment(segment)


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


ORIGIN = "http://dashboard.test:3001"


@pytest.fixture
def cors_client(monkeypatch):
    """Middleware stack in the real app's order: CORSMiddleware is added first,
    so it sits INSIDE the bearer check and a preflight only reaches it through
    the middleware's OPTIONS pass-through."""
    from fastapi.middleware.cors import CORSMiddleware

    import orchestrator.orchestrator as orch

    monkeypatch.setattr(orch, "API_TOKEN", TOKEN)
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[ORIGIN],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(orch.BearerAuthMiddleware)
    calls: list[str] = []

    @app.get("/api/private")
    async def private():
        calls.append("private")
        return {"ok": True}

    c = TestClient(app)
    c.calls = calls
    return c


class TestOptionsPassThrough:
    def test_cors_preflight_needs_no_token(self, cors_client):
        r = cors_client.options(
            "/api/private",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == ORIGIN
        assert cors_client.calls == []

    def test_preflight_from_other_origin_is_refused_by_cors(self, cors_client):
        # Passes the bearer check (it is a preflight); CORSMiddleware refuses it.
        r = cors_client.options(
            "/api/private", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"}
        )
        assert r.status_code == 400
        assert "access-control-allow-origin" not in r.headers
        assert cors_client.calls == []

    @pytest.mark.parametrize(
        "headers",
        [
            {},
            {"Origin": ORIGIN},
            {"Access-Control-Request-Method": "POST"},
            {"Origin": ORIGIN, "Access-Control-Request-Headers": "authorization"},
        ],
        ids=["bare", "origin-only", "acrm-only", "no-acrm"],
    )
    def test_options_that_is_not_a_preflight_needs_the_token(self, cors_client, headers):
        r = cors_client.options("/api/private", headers=headers)
        assert r.status_code == 401
        assert cors_client.calls == []

    def test_non_preflight_options_with_token_reaches_the_router(self, cors_client):
        # The route has no OPTIONS method, so the router answers 405.
        r = cors_client.options("/api/private", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 405
        assert cors_client.calls == []

    def test_other_methods_still_need_the_token(self, cors_client):
        assert cors_client.get("/api/private", headers={"Origin": ORIGIN}).status_code == 401
        assert cors_client.calls == []


@pytest.fixture
def options_route_client(monkeypatch):
    """cors_client's stack, but the route ALSO accepts OPTIONS. With the
    GET-only route above, a tokenless OPTIONS that slipped past CORS would just
    405 and leave ``calls`` empty; here it would run the handler."""
    from fastapi.middleware.cors import CORSMiddleware

    import orchestrator.orchestrator as orch

    monkeypatch.setattr(orch, "API_TOKEN", TOKEN)
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=[ORIGIN], allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(orch.BearerAuthMiddleware)
    calls: list[str] = []

    @app.api_route("/api/private", methods=["GET", "OPTIONS"])
    async def private():
        calls.append("private")
        return {"ok": True}

    c = TestClient(app)
    c.calls = calls
    return c


class TestTokenlessPreflightNeverRunsARoute:
    """What the pass-through relies on: whatever it lets through tokenless,
    CORSMiddleware answers itself. Includes present-but-empty header values,
    which both checks treat as present."""

    @pytest.mark.parametrize(
        "headers",
        [
            {"Origin": ORIGIN, "Access-Control-Request-Method": "GET"},
            {"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
            {"Origin": "null", "Access-Control-Request-Method": "GET"},
            {"Origin": "", "Access-Control-Request-Method": "GET"},
            {"Origin": ORIGIN, "Access-Control-Request-Method": ""},
        ],
        ids=["allowed", "other-origin", "null-origin", "empty-origin", "empty-acrm"],
    )
    def test_preflight_shaped_options_is_answered_by_cors(self, options_route_client, headers):
        r = options_route_client.options("/api/private", headers=headers)
        assert r.status_code in (200, 400), r.text
        assert options_route_client.calls == []

    def test_options_route_runs_with_the_token(self, options_route_client):
        # The route is reachable, so the assertion above has teeth.
        r = options_route_client.options("/api/private", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200
        assert options_route_client.calls == ["private"]


@pytest.fixture
def real_app_client(monkeypatch):
    """The real orchestrator app and middleware stack (no lifespan: TestClient
    is not entered, so nothing starts), with a fresh rate-limit window."""
    import orchestrator.orchestrator as orch

    monkeypatch.setattr(orch, "API_TOKEN", TOKEN)
    monkeypatch.setattr(orch, "_rate_limit_store", {})
    return TestClient(orch.app), orch._cors_origins[0].strip()


class TestRealAppCors:
    def test_dashboard_preflight_still_works(self, real_app_client):
        client, origin = real_app_client
        r = client.options(
            "/api/memory/search",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == origin
        assert r.headers["access-control-allow-credentials"] == "true"

    def test_bare_options_needs_the_token(self, real_app_client):
        client, _ = real_app_client
        assert client.options("/api/memory/search").status_code == 401


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
