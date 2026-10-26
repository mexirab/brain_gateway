"""BearerAuthMiddleware public-path exemption can't be reached by path traversal.

uvicorn percent-decodes the request path before the middleware sees it, so
``/api/audio/..%2f..%2fmemory%2flearned`` arrives as
``/api/audio/../../memory/learned``. A plain ``startswith()`` test exempted
that from the bearer check; Starlette's router doesn't collapse ``..`` so it
only 404'd, but any path-normalizing layer behind the middleware would have
made it an auth bypass. The exemption now covers exactly one safe segment
after each public prefix.

Requests are driven as raw ASGI scopes built the way uvicorn builds them
(``raw_path`` as sent, ``path`` = ``unquote(raw_path)``) because httpx — and so
TestClient — collapses dot segments client-side before they reach the app.
"""

from __future__ import annotations

import posixpath
from urllib.parse import unquote

import pytest
from fastapi import FastAPI

TOKEN = "test-bearer-token-0123456789"  # nosec B105 — test-only constant
AUTH = {"authorization": f"Bearer {TOKEN}"}

# Real id/filename shapes produced by the code that mints these URLs (fixed
# literals so parametrized test ids are stable across runs).
REMINDER_IDS = [
    "3f9a1c2e",  # str(uuid4())[:8]: tool_handlers / brain_dump_manager
    "rec_0123456789ab_1767225600",  # recurring_reminders
    "mp-0123456789",  # meal_planner.REMINDER_ID_PREFIX
    "pc-abcdef0123",  # pantry_consumption.FRIDGE_CHECK_PREFIX
]
AUDIO_NAMES = [f"3f9a1c2e.{ext}" for ext in ("wav", "mp3", "ogg")]  # reminder_manager: <uuid8>.<ext>

LEGIT_PUBLIC = (
    ["/health"]
    + [f"/api/audio/{name}" for name in AUDIO_NAMES]
    + [f"/api/reminder/ack/{rid}?sig={'a' * 32}&exp=1767225600" for rid in REMINDER_IDS]
    + [f"/api/reminder/snooze/{rid}?sig={'b' * 32}&exp=1767225600&minutes=10" for rid in REMINDER_IDS]
)

# Request targets exactly as an attacker would send them (curl --path-as-is).
TRAVERSAL = [
    # the reported case + the other two prefixes
    "/api/audio/..%2f..%2fmemory%2flearned",
    "/api/reminder/ack/..%2f..%2f..%2fmemory%2flearned",
    "/api/reminder/snooze/..%2f..%2f..%2fmemory%2flearned",
    "/api/reminder/ack/..%2f..%2fchat%2fhistory?sig=x&exp=1",
    # upper-case escapes, fully encoded dots
    "/api/audio/..%2F..%2Fmemory%2Flearned",
    "/api/audio/%2e%2e%2f%2e%2e%2fmemory%2flearned",
    "/api/audio/%2E%2E/%2E%2E/memory/learned",
    # literal dot segments (never collapsed by the server)
    "/api/audio/../../memory/learned",
    "/api/audio/./../memory/learned",
    "/api/reminder/ack/../../memory/learned",
    "/api/reminder/snooze/./../../memory/learned",
    "/api/audio/..",
    "/api/audio/.",
    "/api/audio/%2e%2e",
    "/api/audio/%2e",
    # a legit-looking first segment followed by traversal
    "/api/audio/abcd1234.wav/../../../memory/learned",
    "/api/audio/abcd1234.wav%2f..%2f..%2f..%2fmemory%2flearned",
    "/api/reminder/ack/abcd1234/../../../memory/learned",
    # double encoding: one decode leaves %2f / %2e for a later decoder
    "/api/audio/%252e%252e%252f%252e%252e%252fmemory%252flearned",
    "/api/audio/..%252f..%252fmemory%252flearned",
    # backslash, NUL, query/fragment smuggled into the path, empty segments
    "/api/audio/..%5c..%5cmemory%5clearned",
    "/api/audio/..\\..\\memory\\learned",
    "/api/audio/abcd1234.wav%00",
    "/api/audio/x%3f/../../memory/learned",
    "/api/audio/x%23/../../memory/learned",
    "/api/audio//memory/learned",
    "/api/audio/",
    "/api/reminder/ack/",
    # non-ASCII look-alikes
    "/api/audio/%ef%bc%8e%ef%bc%8e%ef%bc%8fmemory",
    # decoded \t \r \n: urlsplit drops them from request.url.path, so a check
    # on that string would see "/api/audio/abc" while the router sees the tab
    "/api/au%09dio/abc",
    "/%09api/audio/abc",
    "/api/reminder/a%0dck/abc",
    "/api/audio%0a/abc",
    "/api/audio/abc%09",
    "/health%09",
]


def _decoded_path(target: str) -> str:
    """The ``scope["path"]`` uvicorn would produce for this request target."""
    path = target.split("?", 1)[0]
    return unquote(path) if "%" in path else path


async def _asgi_status(app, target: str, headers: dict | None = None, method: str = "GET") -> tuple[int, bytes]:
    """Send one request to ``app`` the way uvicorn would; return (status, body)."""
    raw_path, _, query = target.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": _decoded_path(target),
        "raw_path": raw_path.encode("latin-1"),
        "root_path": "",
        "query_string": query.encode("latin-1"),
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 8888),
    }
    status: int | None = None
    body = b""

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        nonlocal status, body
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            body += message.get("body", b"")

    await app(scope, receive, send)
    assert status is not None
    return status, body


@pytest.fixture
def app(monkeypatch):
    """The real middleware in front of stand-ins for the public routes plus a
    path-normalizing catch-all — the downstream layer that would turn an
    exempted traversal into a real bypass."""
    import orchestrator.orchestrator as orch

    monkeypatch.setattr(orch, "API_TOKEN", TOKEN)
    app = FastAPI()
    app.add_middleware(orch.BearerAuthMiddleware)

    @app.get("/health")
    async def health():
        return {"route": "health"}

    @app.get("/api/audio/{filename}")
    async def audio(filename: str):
        return {"route": "audio"}

    @app.api_route("/api/reminder/ack/{reminder_id}", methods=["GET", "POST"])
    async def ack(reminder_id: str):
        return {"route": "ack"}

    @app.api_route("/api/reminder/snooze/{reminder_id}", methods=["GET", "POST"])
    async def snooze(reminder_id: str):
        return {"route": "snooze"}

    @app.get("/{rest:path}")
    async def normalizing_catch_all(rest: str):
        return {"route": "private", "resolved": posixpath.normpath("/" + rest)}

    return app


class TestIsPublicPath:
    @pytest.mark.parametrize("target", LEGIT_PUBLIC)
    def test_legit_public_urls_are_exempt(self, target):
        import orchestrator.orchestrator as orch

        assert orch.BearerAuthMiddleware.is_public_path(_decoded_path(target))

    @pytest.mark.parametrize("target", TRAVERSAL)
    def test_traversal_is_not_exempt(self, target):
        import orchestrator.orchestrator as orch

        assert not orch.BearerAuthMiddleware.is_public_path(_decoded_path(target))

    @pytest.mark.parametrize(
        "path",
        ["/healthz", "/health/", "/health/..", "/api/audio", "/api/memory/learned", "/api/reminder/ack"],
    )
    def test_other_paths_are_not_exempt(self, path):
        import orchestrator.orchestrator as orch

        assert not orch.BearerAuthMiddleware.is_public_path(path)

    @pytest.mark.parametrize("rid", REMINDER_IDS + ["0123abcd"])
    @pytest.mark.parametrize("action", ["ack", "snooze"])
    def test_minted_callback_urls_stay_exempt(self, monkeypatch, rid, action):
        """The URL the ntfy/Pushover/Telegram senders actually build."""
        import orchestrator.orchestrator as orch
        from orchestrator import reminder_manager

        monkeypatch.setattr(reminder_manager._settings, "ntfy_hmac_secret", "s" * 32)
        monkeypatch.setattr(reminder_manager._settings, "ntfy_callback_base_url", "https://jess.example")
        url = reminder_manager._build_callback_url(rid, action, {"minutes": "10"} if action == "snooze" else None)
        target = url.removeprefix("https://jess.example")
        assert orch.BearerAuthMiddleware.is_public_path(_decoded_path(target))

    @pytest.mark.parametrize("rid,warns", [("0123abcd", False), ("rec:2026 10", True), ("..", True)])
    def test_minting_an_unexemptable_id_warns(self, monkeypatch, caplog, rid, warns):
        from orchestrator import reminder_manager

        monkeypatch.setattr(reminder_manager._settings, "ntfy_hmac_secret", "s" * 32)
        monkeypatch.setattr(reminder_manager._settings, "ntfy_callback_base_url", "https://jess.example")
        with caplog.at_level("WARNING", logger="orchestrator.reminder_manager"):
            reminder_manager._build_callback_url(rid, "ack")
        assert any("public-path charset" in r.getMessage() for r in caplog.records) is warns


class TestMiddlewareTraversal:
    @pytest.mark.parametrize("target", TRAVERSAL)
    async def test_traversal_without_token_is_401(self, app, target):
        status, body = await _asgi_status(app, target)
        assert status == 401, body
        assert b"private" not in body

    @pytest.mark.parametrize("target", LEGIT_PUBLIC)
    async def test_legit_public_urls_need_no_token(self, app, target):
        status, body = await _asgi_status(app, target)
        assert status == 200, body
        assert b"private" not in body

    @pytest.mark.parametrize("target", [t for t in LEGIT_PUBLIC if t.startswith("/api/reminder/")])
    async def test_post_callback_taps_need_no_token(self, app, target):
        # ntfy action buttons POST; Pushover/Safari taps GET. Both are tokenless.
        status, body = await _asgi_status(app, target, method="POST")
        assert status == 200, body
        route = "ack" if target.startswith("/api/reminder/ack/") else "snooze"
        assert f'"route":"{route}"'.encode() in body

    @pytest.mark.parametrize("target", [t for t in TRAVERSAL if t.startswith("/api/reminder/")])
    async def test_post_traversal_without_token_is_401(self, app, target):
        status, body = await _asgi_status(app, target, method="POST")
        assert status == 401, body

    async def test_catch_all_is_still_private_without_token(self, app):
        status, _ = await _asgi_status(app, "/memory/learned")
        assert status == 401

    @pytest.mark.parametrize("target", TRAVERSAL[:6])
    async def test_token_holder_is_unaffected(self, app, target):
        # With a valid token the middleware lets everything through as before.
        status, _ = await _asgi_status(app, target, headers=AUTH)
        assert status != 401


def _app_matches(app, path: str) -> bool:
    """Whether any route of ``app`` would serve GET ``path`` (Starlette's public
    ``BaseRoute.matches``, which also descends into included routers)."""
    from starlette.routing import Match

    scope = {"type": "http", "method": "GET", "path": path, "root_path": "", "headers": [], "query_string": b""}
    return any(route.matches(scope)[0] != Match.NONE for route in app.router.routes)


EXPECTED_PREFIXES = ("/api/audio/", "/api/reminder/ack/", "/api/reminder/snooze/")


def test_public_prefixes_are_the_reviewed_set():
    """A new tokenless prefix needs the same traversal review as this file."""
    import orchestrator.orchestrator as orch

    assert set(orch.BearerAuthMiddleware.PUBLIC_PREFIXES) == set(EXPECTED_PREFIXES)
    assert {"/health"} == orch.BearerAuthMiddleware.PUBLIC_PATHS


@pytest.mark.parametrize("prefix", EXPECTED_PREFIXES)
def test_real_app_serves_only_one_segment_under_public_prefixes(prefix):
    """The exemption covers ``<prefix><one segment>`` only. If a nested or
    ``{x:path}`` route is ever added under a public prefix this fails: that
    route would 401 for the tokenless callers it was meant for, and widening
    the exemption to fit it needs the same traversal review as this file."""
    import orchestrator.orchestrator as orch

    assert _app_matches(orch.app, prefix + "abcd1234"), "public route missing from the app"
    for rest in ("abcd1234/x", "../../memory/learned", "abcd1234/../../memory/learned"):
        assert not _app_matches(orch.app, prefix + rest), prefix + rest


@pytest.fixture
def real_callback_client(monkeypatch, tmp_db):
    """The real api_routes ack/snooze handlers behind the real middleware, ntfy
    on with a known secret, empty reminder DB (so a verified tap ends in
    ``not_found`` and nothing is acked, rescheduled or pushed)."""
    from fastapi.testclient import TestClient

    import orchestrator.orchestrator as orch
    from orchestrator import api_routes
    from orchestrator.config import settings

    monkeypatch.setattr(orch, "API_TOKEN", TOKEN)
    for k, v in {
        "ntfy_enabled": True,
        "ntfy_hmac_secret": "s" * 40,
        "ntfy_callback_base_url": "https://jess.example",
        "ntfy_ack_exp_seconds": 1800,
    }.items():
        monkeypatch.setattr(settings, k, v, raising=False)
    app = FastAPI()
    app.add_middleware(orch.BearerAuthMiddleware)
    app.include_router(api_routes.router)
    return TestClient(app)


def _minted(action: str, rid: str = "3f9a1c2e") -> str:
    """The callback URL exactly as the ntfy / Pushover senders mint it."""
    from orchestrator import reminder_manager

    if action == "snooze":
        url = reminder_manager._build_callback_url(rid, "snooze", {"minutes": "10"}, signed_extra="10")
    else:
        url = reminder_manager._build_callback_url(rid, "ack")
    return url.removeprefix("https://jess.example")


class TestRealCallbackHandlersBehindMiddleware:
    """End to end: minted URL -> middleware (no token) -> real handler -> HMAC."""

    @pytest.mark.parametrize("action", ["ack", "snooze"])
    def test_minted_post_tap_reaches_handler_and_verifies(self, real_callback_client, action):
        r = real_callback_client.post(_minted(action))
        assert r.status_code == 404
        assert r.json() == {"ok": False, "error": "not_found"}

    @pytest.mark.parametrize("action", ["ack", "snooze"])
    def test_minted_get_tap_reaches_handler(self, real_callback_client, action):
        r = real_callback_client.get(_minted(action))
        assert r.status_code == 404
        assert "Not found" in r.text

    @pytest.mark.parametrize("action", ["ack", "snooze"])
    def test_tampered_tap_is_refused_by_the_hmac_not_the_middleware(self, real_callback_client, action):
        path, _, query = _minted(action).partition("?")
        r = real_callback_client.post(f"{path}?{query.replace('sig=', 'sig=0', 1)}")
        assert r.status_code == 403
        assert r.json()["error"] == "bad_signature"

    def test_snooze_sig_for_other_minutes_is_refused(self, real_callback_client):
        r = real_callback_client.post(_minted("snooze").replace("minutes=10", "minutes=120"))
        assert r.status_code == 403


def _leaf_routes(routes):
    """Every route the app dispatches to. FastAPI >= 0.14x keeps an included
    router as one ``_IncludedRouter`` entry (``original_router``); older
    versions flattened them. Anything else is yielded as-is so the guard below
    fails loudly on a shape it doesn't know."""
    for route in routes:
        child = getattr(route, "original_router", None)
        if child is not None:
            yield from _leaf_routes(child.routes)
        else:
            yield route


def test_real_app_has_no_route_the_middleware_lets_through_unchecked():
    """BearerAuthMiddleware passes every OPTIONS request through (CORS
    preflight) and, as a BaseHTTPMiddleware, never sees websocket scopes. So a
    route accepting OPTIONS, a websocket route or a mounted sub-app would be
    reachable without a token; each needs its own auth story first."""
    from starlette.routing import Route

    import orchestrator.orchestrator as orch

    leaves = list(_leaf_routes(orch.app.router.routes))
    assert len(leaves) > 50, "route walk found too few routes; FastAPI router internals changed?"
    for route in leaves:
        assert isinstance(route, Route), f"{type(route).__name__} {getattr(route, 'path', '?')}"
        assert route.methods is not None and "OPTIONS" not in route.methods, route.path
