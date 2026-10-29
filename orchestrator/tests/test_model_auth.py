"""Tests for orchestrator/model_auth.py — the model-server bearer key on plain httpx clients.

``MODEL_SERVER_AUTH`` is the client-level auth on the shared ``_http`` client
and the service-registry probe clients. Those clients also talk to Home
Assistant, Google, SearXNG and pages fetched for look_it_up, so the key must be
added ONLY for the origin (scheme, host, port) of a configured
OpenAI-compatible model URL, and never for a cloud backend.

No network: requests go to an ``httpx.MockTransport`` (or respx for the
service-registry probes) that records what the server would have seen.
"""

from __future__ import annotations

import base64
import logging
from types import SimpleNamespace

import httpx
import pytest
import respx

from orchestrator import model_auth, shared
from orchestrator.llm_backend import LLMConfig
from orchestrator.metrics import MODEL_AUTH_REJECTED
from orchestrator.model_auth import MODEL_SERVER_AUTH, ModelServerAuth, key_for_url

KEY = "local-brain-key-6f1c2e9d"
FB_KEY = "fallback-key-a91b07"
YAML_KEY = "yaml-key-55d0c3"
BRAIN = "http://10.0.0.195:8080/v1"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg(monkeypatch):
    """Primary = the local brain at BRAIN with KEY; no fallback; no live backends.

    Returns a setter for overriding individual ``shared`` attributes.
    """
    base = {
        "MODEL_BACKEND": "openai_compatible",
        "MODEL_URL": BRAIN,
        "MODEL_API_KEY": KEY,
        "FALLBACK_MODEL_BACKEND": "openai_compatible",
        "FALLBACK_MODEL_URL": "",
        "FALLBACK_MODEL_API_KEY": "",
        "primary_backend": None,
        "fallback_backend": None,
    }
    for k, v in base.items():
        monkeypatch.setattr(shared, k, v)

    def _set(**kw):
        for k, v in kw.items():
            assert k in base, k
            monkeypatch.setattr(shared, k, v)

    return _set


@pytest.fixture
def clock(monkeypatch):
    """Fake monotonic clock for the reject-log rate limit (module-local, so the
    event loop's own time.monotonic is untouched)."""
    now = [10_000.0]
    monkeypatch.setattr(model_auth, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(model_auth, "_last_reject_log", 0.0)
    return now


def _live(backend: str, url: str, api_key: str = "") -> SimpleNamespace:
    """Stand-in for an LLMBackend: only ``.config`` is read."""
    return SimpleNamespace(config=LLMConfig(backend=backend, url=url, model="m", api_key=api_key))


def _recording_client(handler=None, *, sync: bool = False, **kw):
    seen: list[httpx.Request] = []

    def _h(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request) if handler else httpx.Response(200, json={"ok": True})

    cls = httpx.Client if sync else httpx.AsyncClient
    return cls(transport=httpx.MockTransport(_h), auth=MODEL_SERVER_AUTH, **kw), seen


def _rejected(status: int) -> float:
    return MODEL_AUTH_REJECTED.labels(status=str(status))._value.get()


def _auth_warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "orchestrator.model_auth" and r.levelno == logging.WARNING]


def test_singleton_is_an_httpx_auth():
    assert isinstance(MODEL_SERVER_AUTH, ModelServerAuth)
    assert isinstance(MODEL_SERVER_AUTH, httpx.Auth)


# ---------------------------------------------------------------------------
# key_for_url: same origin gets the key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_url,request_url",
    [
        (BRAIN, "http://10.0.0.195:8080/v1/models"),
        (BRAIN, "http://10.0.0.195:8080/metrics"),
        (BRAIN, "http://10.0.0.195:8080/v1/chat/completions"),
        (BRAIN, "http://10.0.0.195:8080/health"),
        (BRAIN, "http://10.0.0.195:8080"),
        ("http://10.0.0.195:8080/v1/", "http://10.0.0.195:8080/metrics"),  # trailing slash
        ("http://10.0.0.195:8080", "http://10.0.0.195:8080/v1/models"),  # no /v1 on the base
        ("HTTP://Helios.LAN:8080/v1", "http://helios.lan:8080/metrics"),  # case in the config
        ("http://helios.lan:8080/v1", "HTTP://HELIOS.LAN:8080/v1/models"),  # case in the request
        ("http://brain.lan/v1", "http://brain.lan:80/v1/models"),  # implicit vs explicit :80
        ("http://brain.lan:80/v1", "http://brain.lan/v1/models"),
        ("https://brain.lan/v1", "https://brain.lan:443/v1/models"),  # implicit vs explicit :443
        ("http://[fd7a:115c:a1e0::1]:8080/v1", "http://[FD7A:115C:A1E0::1]:8080/metrics"),  # IPv6
        ("http://[::1]:8080/v1", "http://[::1]:8080/v1/models"),
    ],
)
def test_key_for_same_origin(cfg, model_url, request_url):
    cfg(MODEL_URL=model_url)
    assert key_for_url(request_url) == KEY
    assert key_for_url(httpx.URL(request_url)) == KEY  # the type ModelServerAuth passes


# ---------------------------------------------------------------------------
# key_for_url: everything else gets nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "request_url",
    [
        "http://10.0.0.195:8081/v1/models",  # other port
        "http://10.0.0.195/v1/models",  # default :80, not :8080
        "https://10.0.0.195:8080/v1/models",  # other scheme
        "http://10.0.0.58:8080/v1/models",  # other host
        "http://10.0.0.1950:8080/v1/models",  # prefix lookalike
        "http://10.0.0.195.evil.com:8080/v1/models",  # suffix lookalike
        "http://10.0.0.195:8080@evil.com/",  # userinfo trick -> host evil.com
        "http://10.0.0.195:8080@evil.com:8080/v1/models",
        "http://evil.com/10.0.0.195:8080/v1",  # model URL in the path
        "http://evil.com/?next=http://10.0.0.195:8080/v1",  # ... or the query
        "http://[fd7a::1]:8080/v1/models",
        "ws://10.0.0.195:8080/v1",  # not http(s)
        "ftp://10.0.0.195:8080/",
        "file:///etc/passwd",
        "//10.0.0.195:8080/v1/models",  # scheme-relative
        "",
        "not a url",
        "http://",
        "http://[::1",  # httpx.InvalidURL
    ],
)
def test_no_key_for_other_origins_or_garbage(cfg, request_url):
    assert key_for_url(request_url) == ""


def test_garbage_type_is_not_a_crash(cfg):
    assert key_for_url(None) == ""  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_model_url", ["", "not a url", "http://[::1", "ftp://10.0.0.195:8080/"])
def test_garbage_model_url_keys_nothing(cfg, bad_model_url):
    cfg(MODEL_URL=bad_model_url)
    assert key_for_url("http://10.0.0.195:8080/v1/models") == ""
    assert key_for_url("http://localhost/") == ""


def test_no_key_configured(cfg):
    cfg(MODEL_API_KEY="")
    assert key_for_url("http://10.0.0.195:8080/v1/models") == ""


# ---------------------------------------------------------------------------
# Cloud backends never get it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["anthropic", "openai", "Anthropic", "OPENAI"])
def test_env_cloud_backend_key_is_never_attached(cfg, backend):
    cfg(MODEL_BACKEND=backend, MODEL_URL="https://api.anthropic.com/v1")
    assert key_for_url("https://api.anthropic.com/v1/messages") == ""
    # Even a cloud backend type pointed at a LAN address doesn't count as local.
    cfg(MODEL_URL=BRAIN)
    assert key_for_url("http://10.0.0.195:8080/v1/models") == ""


def test_backend_name_case_still_counts_as_local(cfg):
    cfg(MODEL_BACKEND="OpenAI_Compatible")
    assert key_for_url("http://10.0.0.195:8080/metrics") == KEY


@pytest.mark.parametrize("slot", ["primary_backend", "fallback_backend"])
async def test_live_cloud_backend_key_not_handed_to_env_model_url(cfg, slot):
    """YAML ``backend: anthropic`` with no YAML key: the live backend's api_key IS
    MODEL_API_KEY, while env MODEL_BACKEND still defaults to openai_compatible.
    The Anthropic key must not be sent to MODEL_URL."""
    cfg(**{slot: _live("anthropic", "https://api.anthropic.com", api_key=KEY)})
    assert key_for_url("http://10.0.0.195:8080/v1/models") == ""
    assert key_for_url("https://api.anthropic.com/v1/messages") == ""

    client, seen = _recording_client()
    async with client:
        await client.get("http://10.0.0.195:8080/v1/models")
        await client.get("https://api.anthropic.com/v1/models")
    assert all("authorization" not in r.headers for r in seen)
    assert all(KEY not in v for r in seen for v in r.headers.values())


def test_live_cloud_backend_with_its_own_key_keeps_env_backstop(cfg):
    """Control for the above: a cloud primary with a DIFFERENT key doesn't stop
    the env MODEL_URL brain from getting MODEL_API_KEY."""
    cfg(primary_backend=_live("anthropic", "https://api.anthropic.com", api_key="ant-key-xyz"))
    assert key_for_url("http://10.0.0.195:8080/v1/models") == KEY
    assert key_for_url("https://api.anthropic.com/v1/messages") == ""


# ---------------------------------------------------------------------------
# Live backend config, fallback
# ---------------------------------------------------------------------------


def test_live_backend_key_wins_over_env(cfg):
    cfg(primary_backend=_live("openai_compatible", BRAIN, api_key=YAML_KEY))
    assert key_for_url("http://10.0.0.195:8080/v1/models") == YAML_KEY


def test_live_backend_elsewhere_and_env_backstop(cfg):
    """YAML moved the primary to another host: that host gets the YAML key, the
    env MODEL_URL keeps MODEL_API_KEY, neither leaks to the other."""
    cfg(primary_backend=_live("openai_compatible", "http://brain2.lan:8080/v1", api_key=YAML_KEY))
    assert key_for_url("http://brain2.lan:8080/v1/models") == YAML_KEY
    assert key_for_url("http://10.0.0.195:8080/v1/models") == KEY
    assert key_for_url("http://brain2.lan:8081/v1/models") == ""


def test_env_fallback_on_its_own_origin_uses_fallback_key(cfg):
    cfg(FALLBACK_MODEL_URL="http://10.0.0.58:8084/v1", FALLBACK_MODEL_API_KEY=FB_KEY)
    assert key_for_url("http://10.0.0.58:8084/v1/models") == FB_KEY
    assert key_for_url("http://10.0.0.195:8080/v1/models") == KEY


def test_live_fallback_key_used_for_fallback_origin(cfg):
    cfg(fallback_backend=_live("openai_compatible", "http://10.0.0.58:8084/v1", api_key=FB_KEY))
    assert key_for_url("http://10.0.0.58:8084/v1/chat/completions") == FB_KEY
    assert key_for_url("http://10.0.0.195:8080/v1/models") == KEY


def test_fallback_without_key_on_another_host_gets_nothing(cfg):
    """The primary's key must not follow the fallback to a different server."""
    cfg(FALLBACK_MODEL_URL="http://10.0.0.58:8084/v1", FALLBACK_MODEL_API_KEY="")
    assert key_for_url("http://10.0.0.58:8084/v1/models") == ""


@pytest.mark.parametrize("live", [False, True])
def test_fallback_on_same_origin_without_key_gets_model_api_key(cfg, live):
    """The live setup: fallback = the same brain, no FALLBACK_MODEL_API_KEY."""
    if live:
        cfg(
            primary_backend=_live("openai_compatible", BRAIN, api_key=KEY),
            fallback_backend=_live("openai_compatible", BRAIN + "/", api_key=""),
        )
    else:
        cfg(FALLBACK_MODEL_URL=BRAIN, FALLBACK_MODEL_API_KEY="")
    assert key_for_url("http://10.0.0.195:8080/v1/chat/completions") == KEY


# ---------------------------------------------------------------------------
# Through a real httpx client
# ---------------------------------------------------------------------------


async def test_client_adds_key_only_for_model_origin(cfg):
    client, seen = _recording_client()
    async with client:
        await client.get("http://10.0.0.195:8080/v1/models")
        await client.get("http://10.0.0.195:8080/metrics")
        await client.get("http://10.0.0.248:8123/api/states")  # Home Assistant
        await client.get("https://www.googleapis.com/calendar/v3/x")
        await client.get("http://searxng:8080/search?q=x")
    assert [r.headers.get("authorization") for r in seen] == [f"Bearer {KEY}", f"Bearer {KEY}", None, None, None]


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer caller-key"},
        {"authorization": "Basic Zm9vOmJhcg=="},
        {"AUTHORIZATION": "Bearer caller-key"},
        {"X-Api-Key": "ant-key"},
        {"x-api-key": "ant-key"},
    ],
)
async def test_existing_auth_header_left_untouched(cfg, headers):
    client, seen = _recording_client()
    async with client:
        await client.get("http://10.0.0.195:8080/v1/models", headers=headers)
    (req,) = seen
    for k, v in headers.items():
        assert req.headers[k] == v
    if not any(k.lower() == "authorization" for k in headers):
        assert "authorization" not in req.headers
    assert all(KEY not in v for v in req.headers.values())


async def test_url_userinfo_still_becomes_basic_auth(cfg):
    """Client-level auth stops httpx turning URL userinfo into Basic auth;
    ModelServerAuth must keep doing it for non-model hosts."""
    client, seen = _recording_client()
    async with client:
        await client.get("http://user:pw@other-host:9000/x")
        await client.get("http://onlyuser@other-host/y")
        await client.get("http://other-host/z")
    assert seen[0].headers["authorization"] == "Basic " + base64.b64encode(b"user:pw").decode()
    assert seen[1].headers["authorization"] == "Basic " + base64.b64encode(b"onlyuser:").decode()
    assert "authorization" not in seen[2].headers


async def test_streaming_request_gets_key(cfg):
    client, seen = _recording_client()
    async with client:
        async with client.stream("POST", f"{BRAIN}/chat/completions", json={"stream": True}) as r:
            await r.aread()
            assert r.status_code == 200
        async with client.stream("GET", "http://10.0.0.248:8123/api/") as r:
            await r.aread()
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert "authorization" not in seen[1].headers


def test_sync_client_and_sync_stream(cfg):
    client, seen = _recording_client(sync=True)
    with client:
        client.get("http://10.0.0.195:8080/v1/models")
        with client.stream("GET", "http://10.0.0.195:8080/metrics") as r:
            r.read()
        client.get("http://10.0.0.58:8080/v1/models")
    assert [r.headers.get("authorization") for r in seen] == [f"Bearer {KEY}", f"Bearer {KEY}", None]


# ---------------------------------------------------------------------------
# 401/403 on a keyed request: metric + rate-limited warning
# ---------------------------------------------------------------------------


def _status_by_host(statuses: dict[str, int]):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(statuses.get(request.url.host, 200))

    return handler


def test_keyed_401_counts_and_logs_once_per_window(cfg, clock, caplog):
    client, _ = _recording_client(_status_by_host({"10.0.0.195": 401}), sync=True)
    before = _rejected(401)
    with client, caplog.at_level(logging.WARNING, logger="orchestrator.model_auth"):
        assert client.get("http://10.0.0.195:8080/v1/models").status_code == 401
        assert _rejected(401) == before + 1
        assert len(_auth_warnings(caplog)) == 1

        clock[0] += 299
        client.get("http://10.0.0.195:8080/metrics")
        assert _rejected(401) == before + 2  # every rejection counted ...
        assert len(_auth_warnings(caplog)) == 1  # ... but logged once per 300 s

        clock[0] += 1  # 300 s after the first log
        client.get("http://10.0.0.195:8080/v1/models")
        assert _rejected(401) == before + 3
        assert len(_auth_warnings(caplog)) == 2

    for rec in _auth_warnings(caplog):
        assert "401" in rec.getMessage()
    assert KEY not in caplog.text


def test_keyed_403_counts_under_its_own_label(cfg, clock, caplog):
    client, _ = _recording_client(_status_by_host({"10.0.0.195": 403}), sync=True)
    b401, b403 = _rejected(401), _rejected(403)
    with client, caplog.at_level(logging.WARNING, logger="orchestrator.model_auth"):
        client.get("http://10.0.0.195:8080/v1/models")
    assert (_rejected(401), _rejected(403)) == (b401, b403 + 1)
    assert len(_auth_warnings(caplog)) == 1
    assert "403" in _auth_warnings(caplog)[0].getMessage()
    assert KEY not in caplog.text


async def test_keyed_401_counts_on_async_client(cfg, clock):
    client, _ = _recording_client(_status_by_host({"10.0.0.195": 401}))
    before = _rejected(401)
    async with client:
        await client.get("http://10.0.0.195:8080/v1/models")
    assert _rejected(401) == before + 1


@pytest.mark.parametrize(
    "url,headers",
    [
        ("http://10.0.0.248:8123/api/states", {}),  # other host, unkeyed
        ("http://10.0.0.195:8081/v1/models", {}),  # other port, unkeyed
        ("http://10.0.0.195:8080/v1/models", {"Authorization": "Bearer caller-key"}),  # caller's own key
    ],
)
def test_unkeyed_rejection_is_not_counted(cfg, clock, caplog, url, headers):
    client, _ = _recording_client(lambda r: httpx.Response(401), sync=True)
    b401, b403 = _rejected(401), _rejected(403)
    with client, caplog.at_level(logging.WARNING, logger="orchestrator.model_auth"):
        assert client.get(url, headers=headers).status_code == 401
    assert (_rejected(401), _rejected(403)) == (b401, b403)
    assert _auth_warnings(caplog) == []


@pytest.mark.parametrize("status", [200, 404, 500, 503])
def test_other_statuses_are_not_rejections(cfg, clock, caplog, status):
    client, _ = _recording_client(lambda r: httpx.Response(status), sync=True)
    b401, b403 = _rejected(401), _rejected(403)
    with client, caplog.at_level(logging.WARNING, logger="orchestrator.model_auth"):
        client.get("http://10.0.0.195:8080/v1/models")
    assert (_rejected(401), _rejected(403)) == (b401, b403)
    assert _auth_warnings(caplog) == []


# ---------------------------------------------------------------------------
# Redirects (the shared client doesn't follow them, but a caller might)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "http://evil.example/steal",
        "http://10.0.0.195.evil.com:8080/v1/models",
        "http://10.0.0.195:8081/v1/models",
        "https://10.0.0.195:8080/v1/models",
    ],
)
async def test_redirect_from_model_server_does_not_carry_key(cfg, location):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "10.0.0.195" and request.url.port == 8080 and request.url.scheme == "http":
            return httpx.Response(302, headers={"Location": location})
        return httpx.Response(200)

    client, seen = _recording_client(handler, follow_redirects=True)
    async with client:
        r = await client.get("http://10.0.0.195:8080/v1/models")
    assert r.status_code == 200
    assert len(seen) == 2
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert str(seen[1].url) == location
    assert "authorization" not in seen[1].headers
    assert all(KEY not in v for v in seen[1].headers.values())


async def test_401_after_redirect_to_other_host_is_not_a_key_mismatch(cfg, clock):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "10.0.0.195":
            return httpx.Response(302, headers={"Location": "http://elsewhere.example/login"})
        return httpx.Response(401)

    before = _rejected(401)
    client, _ = _recording_client(handler, follow_redirects=True)
    async with client:
        r = await client.get("http://10.0.0.195:8080/v1/models")
    assert r.status_code == 401
    assert _rejected(401) == before


async def test_redirect_into_model_origin_does_not_add_key(cfg):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "other-host":
            return httpx.Response(302, headers={"Location": "http://10.0.0.195:8080/v1/models"})
        return httpx.Response(200)

    client, seen = _recording_client(handler, follow_redirects=True)
    async with client:
        await client.get("http://other-host/start")
    assert [r.url.host for r in seen] == ["other-host", "10.0.0.195"]
    assert all("authorization" not in r.headers for r in seen)


async def test_same_origin_redirect_keeps_key(cfg):
    """Control: proves the redirect harness would see a carried key."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1":
            return httpx.Response(301, headers={"Location": "/v1/models"})
        return httpx.Response(200)

    client, seen = _recording_client(handler, follow_redirects=True)
    async with client:
        await client.get("http://10.0.0.195:8080/v1")
    assert [r.url.path for r in seen] == ["/v1", "/v1/models"]
    assert all(r.headers.get("authorization") == f"Bearer {KEY}" for r in seen)


# ---------------------------------------------------------------------------
# Callers: model_manager.check_model_health (shared._http) + service registry
# ---------------------------------------------------------------------------


def _llama_server(expected_key: str):
    """Mimics llama-server --api-key-file: /health is open, everything else 401s
    without the right bearer."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.headers.get("authorization") != f"Bearer {expected_key}":
            return httpx.Response(401, json={"error": "Invalid API Key"})
        return httpx.Response(200, json={"data": []})

    return handler


async def test_check_model_health_sends_key_via_shared_http(cfg, monkeypatch):
    from orchestrator.model_manager import check_model_health

    client, seen = _recording_client(_llama_server(KEY))
    monkeypatch.setattr(shared, "_http", client)
    async with client:
        assert await check_model_health() is True
    assert str(seen[0].url) == "http://10.0.0.195:8080/v1/models"


async def test_check_model_health_key_mismatch_is_counted(cfg, clock, monkeypatch, caplog):
    from orchestrator.model_manager import check_model_health

    client, _ = _recording_client(_llama_server("the-other-key"))
    monkeypatch.setattr(shared, "_http", client)
    before = _rejected(401)
    with caplog.at_level(logging.WARNING, logger="orchestrator.model_auth"):
        async with client:
            assert await check_model_health() is False
    assert _rejected(401) == before + 1
    assert len(_auth_warnings(caplog)) == 1
    assert KEY not in caplog.text


@pytest.fixture
def registry():
    from orchestrator.service_registry import ServiceRegistry

    reg = ServiceRegistry()
    for svc in reg._services.values():
        svc.url = ""
    return reg


async def test_service_registry_model_probe_sends_key(cfg, registry):
    registry._services["model"].url = BRAIN
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get("http://10.0.0.195:8080/v1/models").mock(return_value=httpx.Response(200, json={}))
        assert await registry.check_service("model") is True
    assert route.calls.last.request.headers["authorization"] == f"Bearer {KEY}"


async def test_service_registry_check_all_keys_only_the_model_origin(cfg, registry):
    registry._services["model"].url = BRAIN
    registry._services["vision"].url = BRAIN  # live setup: vision = the brain
    registry._services["ha"].url = "http://10.0.0.248:8123"
    registry._services["searxng"].url = "http://searxng:8080"
    with respx.mock(assert_all_called=True) as mock:
        brain = mock.get("http://10.0.0.195:8080/v1/models").mock(return_value=httpx.Response(200, json={}))
        ha = mock.get("http://10.0.0.248:8123/api/").mock(return_value=httpx.Response(200))
        sx = mock.get("http://searxng:8080/").mock(return_value=httpx.Response(200))
        results = await registry.check_all()
    assert results == {"model": True, "vision": True, "ha": True, "searxng": True}
    assert len(brain.calls) == 2
    assert all(c.request.headers["authorization"] == f"Bearer {KEY}" for c in brain.calls)
    for route in (ha, sx):
        assert all("authorization" not in c.request.headers for c in route.calls)
