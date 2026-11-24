"""
Tests for scripts/jupiter-kiosk/make_logins.py, the root-run helper that writes
/etc/jupiter-kiosk/secrets.json (read-only kiosk session cookie for the dashboard + a
view-only Grafana account).

No root, no network, no real .env: the .env parser and the cookie derivation
are pure; the "is that port really docker-proxy?" check runs against a faked
`ss` and a faked /proc; the Grafana admin-API sequence runs against a recorded
_request(); write_secrets() writes into tmp_path with chown faked.
"""

from __future__ import annotations

import base64
import email.message
import hashlib
import hmac
import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import urllib.error
import urllib.request
import urllib.response
from pathlib import Path
from types import SimpleNamespace

import pytest

_HERE = Path(__file__).resolve()
_REPO = _HERE.parents[2]


def _first_existing(*candidates: Path) -> Path | None:
    return next((c for c in candidates if c.is_file()), None)


_FOLDER = next(
    (p for p in (_REPO / "scripts" / "jupiter-kiosk", Path("/app/scripts/jupiter-kiosk")) if p.is_dir()), None
)
if _FOLDER is None or not (_FOLDER / "make_logins.py").is_file():
    pytest.skip("scripts/jupiter-kiosk/make_logins.py not found", allow_module_level=True)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # before exec, like kiosk.py's dataclasses need
    spec.loader.exec_module(mod)
    return mod


ml = _load(_FOLDER / "make_logins.py", "jupiter_kiosk_make_logins")
Refused = ml.Refused


class _Proxy:
    """A module stand-in: the given attributes, everything else from the real module."""

    def __init__(self, real, **override):
        self._real = real
        self.__dict__.update(override)

    def __getattr__(self, name):
        return getattr(self._real, name)


# --------------------------------------------------------------------------------------- parse_env


def test_parse_env_compose_style():
    text = (
        "﻿# Brain Gateway .env\n"
        "\n"
        "export A=1\n"
        "B = two \n"
        "C='single # kept'\n"
        'D="double"\n'
        "E=val # trailing comment\n"
        "F=val#kept\n"
        "G=${A}\n"
        "H=\n"
        'I="mismatched\n'
        "J=a=b\n"
        "no equals sign here\n"
        "   # indented comment\n"
        "K=first\n"
        "K=second\n"
        "L=' padded '\n"
        "M=x  #  spaced comment\n"
    )
    assert ml.parse_env(text) == {
        "A": "1",
        "B": "two",
        "C": "single # kept",
        "D": "double",
        "E": "val",
        "F": "val#kept",
        "G": "${A}",  # no interpolation
        "H": "",
        "I": '"mismatched',
        "J": "a=b",
        "K": "second",
        "L": " padded ",
        "M": "x",
    }


def test_parse_env_bom_before_a_key():
    assert ml.parse_env("﻿DASHBOARD_TOKEN=abc\n") == {"DASHBOARD_TOKEN": "abc"}


def test_parse_env_crlf_and_empty():
    assert ml.parse_env("A=1\r\nB='2'\r\n") == {"A": "1", "B": "2"}
    assert ml.parse_env("") == {}


@pytest.mark.parametrize(
    "line",
    [
        pytest.param('DASHBOARD_TOKEN="v" # rotated 2026-11', id="double"),
        pytest.param("DASHBOARD_TOKEN='v' # rotated 2026-11", id="single"),
    ],
)
def test_parse_env_quoted_value_with_inline_comment(line):
    assert ml.parse_env(line + "\n") == {"DASHBOARD_TOKEN": "v"}


# ------------------------------------------------------------------------------------ kiosk_session

KIOSK_TOKEN = "kiosk-token-0123456789abcdef"
DASH_TOKEN = "test-token-123"


def test_kiosk_session_is_the_frontend_hmac():
    expected = hmac.new(KIOSK_TOKEN.encode(), b"brain-gateway-kiosk-session-v1", hashlib.sha256).hexdigest()
    assert ml.kiosk_session(f"  {KIOSK_TOKEN}\n", DASH_TOKEN) == expected  # trimmed like the frontend's .trim()
    assert re.fullmatch(r"[0-9a-f]{64}", expected)
    assert ml.kiosk_session(KIOSK_TOKEN + "x", DASH_TOKEN) != expected


def test_kiosk_session_is_never_the_full_login():
    # Same key, other label: even a token reused as both would give different cookies (the frontend
    # also refuses equal tokens, and so does kiosk_session()).
    full = hmac.new(KIOSK_TOKEN.encode(), b"brain-gateway-dashboard-session-v1", hashlib.sha256).hexdigest()
    assert ml.kiosk_session(KIOSK_TOKEN, DASH_TOKEN) != full


def test_kiosk_session_value_is_accepted_by_the_kiosk():
    kiosk_path = _FOLDER / "kiosk.py"
    kiosk = _load(kiosk_path, "jupiter_kiosk_for_logins")
    cfg = kiosk.load_config({"screens": [{"name": "right", "url": "https://host.example/", "login": "dashboard"}]})
    value = ml.kiosk_session(KIOSK_TOKEN, DASH_TOKEN)
    assert kiosk.load_secrets({"dashboard_kiosk_session": value}, cfg) == {"dashboard_kiosk_session": value}
    assert kiosk.DASHBOARD_COOKIE == ml.DASHBOARD_COOKIE


def test_kiosk_session_accepts_exactly_24_characters():
    assert re.fullmatch(r"[0-9a-f]{64}", ml.kiosk_session("k" * 24, DASH_TOKEN))


def test_kiosk_session_length_is_checked_after_trimming():
    # The frontend trims KIOSK_TOKEN before its length rule: padding doesn't make 23 characters enough,
    # and a padded 24-character token gives the unpadded token's cookie.
    with pytest.raises(Refused, match="shorter than 24"):
        ml.kiosk_session("  " + "k" * 23 + " \n", DASH_TOKEN)
    assert ml.kiosk_session(" " + "k" * 24 + "\t", DASH_TOKEN) == ml.kiosk_session("k" * 24, DASH_TOKEN)


@pytest.mark.parametrize(
    ("kiosk_token", "dash_token", "message"),
    [
        pytest.param("", DASH_TOKEN, "KIOSK_TOKEN is not set", id="kiosk-empty"),
        pytest.param("   \n", DASH_TOKEN, "KIOSK_TOKEN is not set", id="kiosk-blank"),
        pytest.param("changeme", DASH_TOKEN, "KIOSK_TOKEN is not set", id="kiosk-changeme"),
        pytest.param("k" * 23, DASH_TOKEN, "shorter than 24", id="kiosk-23"),
        pytest.param(KIOSK_TOKEN, KIOSK_TOKEN, "same as DASHBOARD_TOKEN", id="kiosk-equals-dashboard"),
        pytest.param(KIOSK_TOKEN, f" {KIOSK_TOKEN}\n", "same as DASHBOARD_TOKEN", id="equal-after-trim"),
        pytest.param("${KIOSK_SECRET}-0123456789abcdef", DASH_TOKEN, "KIOSK_TOKEN uses", id="kiosk-interpolation"),
        pytest.param(KIOSK_TOKEN, "", "DASHBOARD_TOKEN is unset", id="dashboard-empty"),
        pytest.param(KIOSK_TOKEN, "  changeme  ", "DASHBOARD_TOKEN is unset", id="dashboard-changeme"),
        pytest.param(KIOSK_TOKEN, "abc${X}def", "DASHBOARD_TOKEN uses", id="dashboard-interpolation"),
    ],
)
def test_kiosk_session_refuses(kiosk_token, dash_token, message):
    with pytest.raises(Refused, match=re.escape(message)) as err:
        ml.kiosk_session(kiosk_token, dash_token)
    for token in (kiosk_token.strip(), dash_token.strip()):
        if len(token) > 8:
            assert token not in str(err.value)


def test_constants_match_the_frontend():
    assert ml.KIOSK_SESSION_LABEL == b"brain-gateway-kiosk-session-v1"
    assert ml.MIN_KIOSK_TOKEN_LENGTH == 24
    assert ml.DASHBOARD_COOKIE == "brain_token"
    assert {"", "changeme"} == ml.PLACEHOLDER_TOKENS
    assert not hasattr(ml, "SESSION_LABEL")  # the full-login label has no business here any more
    auth = _first_existing(
        _REPO / "frontend" / "src" / "lib" / "dashboard-auth.ts", Path("/app/frontend_lib/dashboard-auth.ts")
    )
    if auth is None:
        pytest.skip("frontend/src/lib not mounted")
    text = auth.read_text(encoding="utf-8")
    assert f"const KIOSK_SESSION_LABEL = '{ml.KIOSK_SESSION_LABEL.decode()}';" in text
    assert f"export const MIN_KIOSK_TOKEN_LENGTH = {ml.MIN_KIOSK_TOKEN_LENGTH};" in text
    assert f"export const COOKIE_NAME = '{ml.DASHBOARD_COOKIE}';" in text
    assert "new Set(['', 'changeme'])" in text
    assert "hmacHex(token, KIOSK_SESSION_LABEL)" in text  # key = kiosk token, message = label


def test_refused_paths_are_outside_the_kiosk_allow_list():
    lib = _first_existing(
        _REPO / "frontend" / "src" / "lib" / "kiosk-session.ts", Path("/app/frontend_lib/kiosk-session.ts")
    )
    if lib is None:
        pytest.skip("frontend/src/lib not mounted")
    text = lib.read_text(encoding="utf-8")
    assert "export const KIOSK_PAGES: readonly string[] = ['/dashboard'];" in text
    allowed = set(re.findall(r"^  '(/[^']*)',", text, flags=re.M))
    assert "/api/reminders" in allowed  # the parse found the list
    for path in ml.KIOSK_REFUSED_PATHS:  # what check_dashboard_cookie() expects a 403 for
        assert path != "/dashboard"
        assert path.removeprefix("/api/proxy") not in allowed


# ------------------------------------------------------------------------------- port_owner_problem

PROXY_ID = (64768, 1001)  # (st_dev, st_ino) of /usr/bin/docker-proxy
HOST_MNT = "mnt:[4026531841]"


def _ss_line(pid: int, port: int = 3000) -> str:
    return f'LISTEN 0      4096       127.0.0.1:{port}       0.0.0.0:*    users:(("docker-proxy",pid={pid},fd=7))\n'


@pytest.fixture
def ports(monkeypatch):
    world = SimpleNamespace(
        ss_out=_ss_line(4242),
        ss_error=None,
        ss_calls=[],
        proxy=PROXY_ID,
        host_mnt=HOST_MNT,
        procs={"4242": {"uid": 0, "exe": PROXY_ID, "mnt": HOST_MNT}},
    )

    def run(cmd, **kwargs):
        world.ss_calls.append((cmd, kwargs))
        if world.ss_error is not None:
            raise world.ss_error
        return SimpleNamespace(stdout=world.ss_out)

    def fake_stat(path):
        if path == ml.DOCKER_PROXY:
            if world.proxy is None:
                raise FileNotFoundError(path)
            return SimpleNamespace(st_dev=world.proxy[0], st_ino=world.proxy[1], st_uid=0)
        m = re.fullmatch(r"/proc/(\d+)(/exe)?", path)
        if not m or m.group(1) not in world.procs:
            raise FileNotFoundError(path)
        p = world.procs[m.group(1)]
        if m.group(2):
            return SimpleNamespace(st_dev=p["exe"][0], st_ino=p["exe"][1], st_uid=0)
        return SimpleNamespace(st_uid=p["uid"], st_dev=0, st_ino=0)

    def fake_readlink(path):
        if path == "/proc/1/ns/mnt":
            return world.host_mnt
        m = re.fullmatch(r"/proc/(\d+)/ns/mnt", path)
        if not m or m.group(1) not in world.procs:
            raise FileNotFoundError(path)
        return world.procs[m.group(1)]["mnt"]

    monkeypatch.setattr(ml, "subprocess", SimpleNamespace(run=run, SubprocessError=subprocess.SubprocessError))
    monkeypatch.setattr(ml, "os", _Proxy(os, stat=fake_stat, readlink=fake_readlink))
    return world


def test_port_owner_docker_proxy_ok(ports):
    assert ml.port_owner_problem(3000) is None
    ((cmd, kwargs),) = ports.ss_calls
    assert cmd == ["ss", "-Hltnp", "src 127.0.0.1:3000"]
    assert kwargs["check"] is True and kwargs["timeout"] > 0


def test_port_owner_same_pid_listed_twice(ports):
    ports.ss_out = (
        'LISTEN 0 4096 127.0.0.1:3000 0.0.0.0:* users:(("docker-proxy",pid=4242,fd=7),("docker-proxy",pid=4242,fd=8))\n'
    )
    assert ml.port_owner_problem(3000) is None


def test_port_owner_nothing_listening(ports):
    ports.ss_out = ""
    assert ml.port_owner_problem(3000) == "nothing listens there"


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(FileNotFoundError("ss"), id="ss-missing"),
        pytest.param(subprocess.CalledProcessError(1, ["ss"]), id="ss-exit-1"),
        pytest.param(subprocess.TimeoutExpired(["ss"], 10), id="ss-timeout"),
    ],
)
def test_port_owner_ss_failed(ports, error):
    ports.ss_error = error
    assert ml.port_owner_problem(3000) == "ss failed"


@pytest.mark.parametrize(
    ("change", "value"),
    [
        pytest.param("uid", 1000, id="not-root"),
        pytest.param("exe", (64768, 9999), id="other-binary"),
        pytest.param("exe", (2049, 1001), id="same-inode-other-device"),
        pytest.param("mnt", "mnt:[4026532777]", id="container-mount-namespace"),
    ],
)
def test_port_owner_impostor(ports, change, value):
    ports.procs["4242"][change] = value
    assert ml.port_owner_problem(3000) == "held by pid 4242, which is not the host's docker-proxy"


def test_port_owner_one_impostor_among_two(ports):
    ports.ss_out = _ss_line(4242) + _ss_line(77)
    ports.procs["77"] = {"uid": 0, "exe": PROXY_ID, "mnt": "mnt:[4026532777]"}
    assert ml.port_owner_problem(3000) == "held by pid 77, which is not the host's docker-proxy"


def test_port_owner_pid_vanished(ports):
    ports.ss_out = _ss_line(5555)
    assert ml.port_owner_problem(3000) == "pid 5555 vanished or is unreadable"


def test_port_owner_docker_proxy_missing(ports):
    ports.proxy = None
    assert ml.port_owner_problem(3000).startswith("can't check")


def test_require_docker_proxy(ports):
    ml.require_docker_proxy(3000, "Grafana admin password")
    ports.ss_out = ""
    with pytest.raises(Refused, match="127.0.0.1:3000: nothing listens there; not sending the Grafana admin password"):
        ml.require_docker_proxy(3000, "Grafana admin password")


# ---------------------------------------------------------------------------------------- _request


class _FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(monkeypatch, outcome):
    seen = []

    def open_(request, timeout):
        seen.append((request, timeout))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(ml, "_OPENER", SimpleNamespace(open=open_))
    return seen


def test_request_sends_json_and_parses_answer(monkeypatch):
    seen = _opener(monkeypatch, _FakeResponse(b'{"id": 7}'))
    assert ml._request("PUT", ml.GRAFANA + "/api/x", {"Authorization": "Basic Zm9v"}, {"a": 1}) == (200, {"id": 7})
    ((request, timeout),) = seen
    assert request.get_method() == "PUT"
    assert json.loads(request.data) == {"a": 1}
    assert request.get_header("Authorization") == "Basic Zm9v"
    assert request.get_header("Content-type") == "application/json"
    assert timeout > 0


@pytest.mark.parametrize(
    ("body", "expected"),
    [pytest.param(b"", {}, id="empty"), pytest.param(b"<html>", {}, id="not-json")],
)
def test_request_tolerates_odd_bodies(monkeypatch, body, expected):
    _opener(monkeypatch, _FakeResponse(body, 200))
    assert ml._request("GET", ml.GRAFANA + "/api/x", {}) == (200, expected)


def test_request_http_error_and_redirect_return_the_status(monkeypatch):
    _opener(monkeypatch, urllib.error.HTTPError(ml.DASHBOARD + "/dashboard", 302, "Found", {}, None))
    assert ml._request("GET", ml.DASHBOARD + "/dashboard", {}) == (302, {})


def test_request_unreachable_is_refused(monkeypatch):
    _opener(monkeypatch, urllib.error.URLError("connection refused"))
    with pytest.raises(Refused, match=r"^http://127\.0\.0\.1:3000 not reachable \(URLError\)$"):
        ml._request("GET", ml.GRAFANA + "/api/users/lookup?loginOrEmail=x", {})


def test_no_redirect_handler_never_follows():
    assert ml._NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://elsewhere.example/") is None


# ------------------------------------------------------------------------------ check_dashboard_cookie


@pytest.fixture
def dashboard(monkeypatch):
    calls = []
    state = SimpleNamespace(status={"/dashboard": 200, "/chat": 403, "/api/proxy/api/inbox": 403})
    monkeypatch.setattr(ml, "require_docker_proxy", lambda port, what: calls.append(("proxy-check", port)))

    def fake_request(method, url, headers, body=None):
        calls.append((method, url, headers))
        return state.status[url.removeprefix(ml.DASHBOARD)], {}

    monkeypatch.setattr(ml, "_request", fake_request)
    return SimpleNamespace(calls=calls, state=state)


def test_check_dashboard_cookie_ok(dashboard):
    ml.check_dashboard_cookie("ab" * 32)
    cookie = {"Cookie": "brain_token=" + "ab" * 32}
    assert dashboard.calls == [
        ("proxy-check", 3001),
        ("GET", "http://127.0.0.1:3001/dashboard", cookie),
        ("GET", "http://127.0.0.1:3001/chat", cookie),
        ("GET", "http://127.0.0.1:3001/api/proxy/api/inbox", cookie),
    ]
    assert all(method == "GET" for method, *_ in dashboard.calls[1:])  # never a write, even as a probe


@pytest.mark.parametrize(
    ("path", "status", "message"),
    [
        pytest.param("/dashboard", 302, "redirects to /login", id="dashboard-302"),
        pytest.param("/dashboard", 307, "redirects to /login", id="dashboard-307"),
        pytest.param("/dashboard", 502, "HTTP 502", id="dashboard-502"),
        pytest.param("/dashboard", 404, "HTTP 404", id="dashboard-404"),
        pytest.param("/chat", 200, "HTTP 200 for /chat instead of 403", id="chat-open"),
        pytest.param("/chat", 307, "HTTP 307 for /chat instead of 403", id="chat-login-redirect"),
        pytest.param("/api/proxy/api/inbox", 200, "for /api/proxy/api/inbox instead of 403", id="proxy-open"),
        pytest.param("/api/proxy/api/inbox", 401, "for /api/proxy/api/inbox instead of 403", id="proxy-401"),
    ],
)
def test_check_dashboard_cookie_refuses(dashboard, path, status, message):
    dashboard.state.status[path] = status
    with pytest.raises(Refused, match=re.escape(message)) as err:
        ml.check_dashboard_cookie("ab" * 32)
    assert "ab" * 32 not in str(err.value)


def test_check_dashboard_cookie_sends_nothing_when_port_is_not_docker_proxy(monkeypatch):
    sent = []

    def refuse(port, what):
        raise Refused(f"127.0.0.1:{port}: held by pid 77, which is not the host's docker-proxy; not sending the {what}")

    monkeypatch.setattr(ml, "require_docker_proxy", refuse)
    monkeypatch.setattr(ml, "_request", lambda *a, **k: sent.append(a) or (200, {}))
    with pytest.raises(Refused, match=re.escape("127.0.0.1:3001:")):
        ml.check_dashboard_cookie("ab" * 32)
    assert sent == []  # the cookie never reached whoever holds the port


@pytest.mark.parametrize("status", [pytest.param(s, id=str(s)) for s in (301, 302, 303, 307, 308)])
def test_check_dashboard_cookie_wrong_token_stops_at_the_dashboard(dashboard, status):
    # A cookie the frontend doesn't know, answered the way it does: pages redirect to /login, the proxy
    # says 401. The message must name the token mismatch (not "/chat answered 307"), and the cookie
    # goes no further than /dashboard.
    dashboard.state.status.update({"/dashboard": status, "/chat": status, "/api/proxy/api/inbox": 401})
    with pytest.raises(Refused, match="KIOSK_TOKEN in this .env differs"):
        ml.check_dashboard_cookie("ab" * 32)
    assert [c[1] for c in dashboard.calls[1:]] == [ml.DASHBOARD + "/dashboard"]


class _FakeFrontend:
    """The network under the module's real _OPENER (urllib's error processing and its redirect
    handler stay real): answers http_open from `routes` {url: (status, headers)}, 404 for anything
    else, and records every URL asked for."""

    def __init__(self, routes: dict[str, tuple[int, dict[str, str]]]):
        self.routes = routes
        self.requests: list[urllib.request.Request] = []

    def http_open(self, req):
        self.requests.append(req)
        status, headers = self.routes.get(req.full_url, (404, {}))
        msg = email.message.Message()
        for name, value in headers.items():
            msg[name] = value
        response = urllib.response.addinfourl(io.BytesIO(b"<!DOCTYPE html>"), msg, req.full_url, status)
        response.msg = "fake"
        return response


def _frontend(monkeypatch, routes: dict[str, tuple[int, dict[str, str]]]) -> _FakeFrontend:
    monkeypatch.setattr(ml, "require_docker_proxy", lambda port, what: None)
    fake = _FakeFrontend({ml.DASHBOARD + path: answer for path, answer in routes.items()})
    (http,) = [h for h in ml._OPENER.handlers if isinstance(h, urllib.request.HTTPHandler)]
    monkeypatch.setattr(http, "http_open", fake.http_open)  # only the socket is faked
    return fake


LOGIN_REDIRECT = (307, {"Location": "/login?redirect=%2Fdashboard"})


def test_check_dashboard_cookie_through_urllib_read_only_frontend(monkeypatch):
    # The frontend's 403s reach _request as urllib HTTPErrors: they are the answer we want, not a failure.
    fake = _frontend(monkeypatch, {"/dashboard": (200, {}), "/chat": (403, {}), "/api/proxy/api/inbox": (403, {})})
    ml.check_dashboard_cookie("ab" * 32)
    assert [r.full_url for r in fake.requests] == [
        ml.DASHBOARD + "/dashboard",
        ml.DASHBOARD + "/chat",
        ml.DASHBOARD + "/api/proxy/api/inbox",
    ]
    for r in fake.requests:
        assert r.get_method() == "GET"
        assert r.data is None
        assert r.get_header("Cookie") == "brain_token=" + "ab" * 32


@pytest.mark.parametrize(
    ("routes", "message"),
    [
        pytest.param(
            {"/dashboard": LOGIN_REDIRECT, "/login?redirect=%2Fdashboard": (200, {})},  # a login page
            "redirects to /login",
            id="wrong-token",
        ),
        pytest.param(
            {"/dashboard": (200, {}), "/chat": LOGIN_REDIRECT, "/login?redirect=%2Fdashboard": (200, {})},
            "HTTP 307 for /chat instead of 403",
            id="chat-redirects",
        ),
        pytest.param(
            {"/dashboard": (200, {}), "/chat": (200, {})},
            "HTTP 200 for /chat instead of 403",
            id="chat-open",
        ),
    ],
)
def test_check_dashboard_cookie_through_urllib_refuses(monkeypatch, routes, message):
    fake = _frontend(monkeypatch, routes)
    with pytest.raises(Refused, match=re.escape(message)) as err:
        ml.check_dashboard_cookie("ab" * 32)
    assert "ab" * 32 not in str(err.value)
    # A redirect is reported, never followed: the cookie is not re-sent to the Location.
    assert all("/login" not in r.full_url for r in fake.requests)


# ------------------------------------------------------------------------------ ensure_grafana_viewer


@pytest.fixture
def grafana(monkeypatch):
    state = SimpleNamespace(lookup=(200, {"id": 7}), create=(200, {"id": 9}), fail=None, calls=[])

    def fake_require(port, what):
        state.calls.append(("proxy-check", port, what))

    def fake_request(method, url, headers, body=None):
        assert url.startswith(ml.GRAFANA + "/")  # only ever the loopback Grafana
        path = url[len(ml.GRAFANA) :]
        state.calls.append((method, path, body, headers.get("Authorization")))
        if state.fail == (method, path):
            return 500, {}
        if path.startswith("/api/users/lookup"):
            return state.lookup
        if (method, path) == ("POST", "/api/admin/users"):
            return state.create
        return 200, {}

    monkeypatch.setattr(ml, "require_docker_proxy", fake_require)
    monkeypatch.setattr(ml, "_request", fake_request)
    return state


AUTH = "Basic " + base64.b64encode(b"admin:adm1n-pw").decode()


def _requests(state) -> list[tuple[str, str]]:
    return [(c[0], c[1]) for c in state.calls if c[0] != "proxy-check"]


def test_grafana_reset_sets_the_password_last(grafana, capsys):
    password = ml.ensure_grafana_viewer("admin", "adm1n-pw")
    assert grafana.calls[0] == ("proxy-check", 3000, "Grafana admin password")
    assert _requests(grafana) == [
        ("GET", "/api/users/lookup?loginOrEmail=closet-kiosk"),
        ("POST", "/api/admin/users/7/enable"),
        ("PATCH", "/api/orgs/1/users/7"),
        ("PUT", "/api/admin/users/7/permissions"),
        ("PUT", "/api/admin/users/7/password"),
    ]
    bodies = {(c[0], c[1]): c[2] for c in grafana.calls[1:]}
    assert bodies[("PATCH", "/api/orgs/1/users/7")] == {"role": "Viewer"}
    assert bodies[("PUT", "/api/admin/users/7/permissions")] == {"isGrafanaAdmin": False}
    assert bodies[("PUT", "/api/admin/users/7/password")] == {"password": password}
    assert all(c[3] == AUTH for c in grafana.calls[1:])
    out = capsys.readouterr()
    assert "password reset" in out.out
    assert password not in out.out + out.err
    assert "adm1n-pw" not in out.out + out.err


def test_grafana_create_posts_the_user_first(grafana):
    grafana.lookup = (404, {})
    password = ml.ensure_grafana_viewer("admin", "adm1n-pw")
    requests = _requests(grafana)
    assert requests[0] == ("GET", "/api/users/lookup?loginOrEmail=closet-kiosk")
    assert requests[1:] == [
        ("POST", "/api/admin/users"),
        ("PATCH", "/api/orgs/1/users/9"),
        ("PUT", "/api/admin/users/9/permissions"),
    ]  # no enable, no separate password PUT
    create_body = grafana.calls[2][2]
    assert create_body == {"name": "Closet kiosk", "login": "closet-kiosk", "password": password, "OrgId": 1}


def test_grafana_password_shape(grafana):
    first = ml.ensure_grafana_viewer("admin", "adm1n-pw")
    second = ml.ensure_grafana_viewer("admin", "adm1n-pw")
    assert first != second
    assert first.endswith("-Aa1")
    assert len(first) >= 32


@pytest.mark.parametrize(
    "failing",
    [
        pytest.param(("POST", "/api/admin/users/7/enable"), id="enable"),
        pytest.param(("PATCH", "/api/orgs/1/users/7"), id="role"),
        pytest.param(("PUT", "/api/admin/users/7/permissions"), id="permissions"),
    ],
)
def test_grafana_reset_failure_leaves_the_old_password(grafana, failing):
    grafana.fail = failing
    with pytest.raises(Refused, match=r"\(HTTP 500\)"):
        ml.ensure_grafana_viewer("admin", "adm1n-pw")
    assert ("PUT", "/api/admin/users/7/password") not in _requests(grafana)


def test_grafana_lookup_rejected(grafana):
    grafana.lookup = (401, {})
    with pytest.raises(Refused, match="HTTP 401") as err:
        ml.ensure_grafana_viewer("admin", "adm1n-pw")
    assert _requests(grafana) == [("GET", "/api/users/lookup?loginOrEmail=closet-kiosk")]
    assert "adm1n-pw" not in str(err.value)


def test_grafana_create_without_id(grafana):
    grafana.lookup = (404, {})
    grafana.create = (200, {"message": "User created"})
    with pytest.raises(Refused, match="returned no id"):
        ml.ensure_grafana_viewer("admin", "adm1n-pw")
    assert ("PATCH", "/api/orgs/1/users/None") not in _requests(grafana)


def test_grafana_create_failure(grafana):
    grafana.lookup = (404, {})
    grafana.fail = ("POST", "/api/admin/users")
    with pytest.raises(Refused, match="could not create closet-kiosk"):
        ml.ensure_grafana_viewer("admin", "adm1n-pw")
    assert len(_requests(grafana)) == 2


def test_grafana_nothing_sent_when_port_is_not_docker_proxy(monkeypatch):
    sent = []

    def refuse(port, what):
        raise Refused(f"127.0.0.1:{port}: nothing listens there; not sending the {what}")

    monkeypatch.setattr(ml, "require_docker_proxy", refuse)
    monkeypatch.setattr(ml, "_request", lambda *a, **k: sent.append(a) or (200, {}))
    with pytest.raises(Refused):
        ml.ensure_grafana_viewer("admin", "adm1n-pw")
    assert sent == []


# ----------------------------------------------------------------------------------- write_secrets


def test_write_secrets_atomic_0640(tmp_path, monkeypatch, capsys):
    chowned = []
    monkeypatch.setattr(ml, "os", _Proxy(os, chown=lambda path, uid, gid: chowned.append((path, uid, gid))))
    target = tmp_path / "secrets.json"
    data = {"grafana_user": "closet-kiosk", "grafana_password": "s3cret-grafana", "dashboard_kiosk_session": "ab" * 32}
    ml.write_secrets(str(target), 1234, data)
    assert json.loads(target.read_text(encoding="utf-8")) == data
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    ((path, uid, gid),) = chowned
    assert (uid, gid) == (0, 1234)
    assert os.path.basename(path).startswith(".secrets.")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["secrets.json"]  # no temp file left
    out = capsys.readouterr().out
    assert "s3cret-grafana" not in out and "abab" not in out


def test_write_secrets_failure_leaves_old_file(tmp_path, monkeypatch):
    def no_chown(path, uid, gid):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(ml, "os", _Proxy(os, chown=no_chown))
    target = tmp_path / "secrets.json"
    target.write_text('{"old": true}\n', encoding="utf-8")
    with pytest.raises(PermissionError):
        ml.write_secrets(str(target), 1234, {"grafana_password": "new"})
    assert target.read_text(encoding="utf-8") == '{"old": true}\n'
    assert sorted(p.name for p in tmp_path.iterdir()) == ["secrets.json"]


# -------------------------------------------------------------------------------------------- main

_REAL_WRITE_SECRETS = ml.write_secrets  # main_env stubs it; the round-trip test puts it back


@pytest.fixture
def main_env(tmp_path, monkeypatch):
    env_file = tmp_path / "env"
    env_file.write_text(
        f"DASHBOARD_TOKEN={DASH_TOKEN}\nKIOSK_TOKEN={KIOSK_TOKEN}\nGF_SECURITY_ADMIN_PASSWORD='adm1n-pw'\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "etc"
    out_dir.mkdir()
    out = out_dir / "secrets.json"
    steps = []
    monkeypatch.setattr(ml, "os", _Proxy(os, geteuid=lambda: 0))
    monkeypatch.setattr(ml, "grp", SimpleNamespace(getgrnam=lambda name: SimpleNamespace(gr_gid=1234)))
    monkeypatch.setattr(ml, "check_dashboard_cookie", lambda session: steps.append(("dashboard", session)))
    monkeypatch.setattr(
        ml, "ensure_grafana_viewer", lambda user, pw: steps.append(("grafana", user, pw)) or "new-grafana-pw"
    )
    monkeypatch.setattr(ml, "write_secrets", lambda path, gid, data: steps.append(("write", path, gid, data)))
    monkeypatch.setattr(sys, "argv", ["make_logins.py", "--env", str(env_file), "--out", str(out)])
    return SimpleNamespace(env_file=env_file, out=out, steps=steps)


def test_main_order_dashboard_then_grafana_then_file(main_env):
    assert ml.main() == 0
    session = ml.kiosk_session(KIOSK_TOKEN, DASH_TOKEN)
    assert main_env.steps == [
        ("dashboard", session),
        ("grafana", "admin", "adm1n-pw"),
        (
            "write",
            str(main_env.out),
            1234,
            {"grafana_user": "closet-kiosk", "grafana_password": "new-grafana-pw", "dashboard_kiosk_session": session},
        ),
    ]


def test_main_uses_configured_admin_user(main_env):
    main_env.env_file.write_text(
        f"DASHBOARD_TOKEN={DASH_TOKEN}\nKIOSK_TOKEN={KIOSK_TOKEN}\nGF_SECURITY_ADMIN_USER=root-admin\n"
        "GF_SECURITY_ADMIN_PASSWORD=x\n",
        encoding="utf-8",
    )
    assert ml.main() == 0
    assert main_env.steps[1] == ("grafana", "root-admin", "x")


def test_main_dashboard_mismatch_changes_nothing(main_env, monkeypatch, capsys):
    def mismatch(session):
        raise Refused("the dashboard rejects the derived cookie")

    monkeypatch.setattr(ml, "check_dashboard_cookie", mismatch)
    assert ml.main() == 1
    assert main_env.steps == []  # Grafana untouched, no file written
    assert "refused: the dashboard rejects the derived cookie" in capsys.readouterr().err


def test_main_grafana_failure_writes_nothing(main_env, monkeypatch):
    def broken(user, pw):
        raise Refused("could not make closet-kiosk a Viewer in org 1 (HTTP 500)")

    monkeypatch.setattr(ml, "ensure_grafana_viewer", broken)
    assert ml.main() == 1
    assert [s[0] for s in main_env.steps] == ["dashboard"]


@pytest.mark.parametrize(
    "env_text",
    [
        pytest.param(f"DASHBOARD_TOKEN={DASH_TOKEN}\nKIOSK_TOKEN={KIOSK_TOKEN}\n", id="no-admin-password"),
        pytest.param(f"KIOSK_TOKEN={KIOSK_TOKEN}\nGF_SECURITY_ADMIN_PASSWORD=x\n", id="no-dashboard-token"),
        pytest.param(
            f"DASHBOARD_TOKEN=changeme\nKIOSK_TOKEN={KIOSK_TOKEN}\nGF_SECURITY_ADMIN_PASSWORD=x\n",
            id="placeholder-token",
        ),
        pytest.param(f"DASHBOARD_TOKEN={DASH_TOKEN}\nGF_SECURITY_ADMIN_PASSWORD=x\n", id="no-kiosk-token"),
        pytest.param(
            f"DASHBOARD_TOKEN={DASH_TOKEN}\nKIOSK_TOKEN=short\nGF_SECURITY_ADMIN_PASSWORD=x\n", id="short-kiosk-token"
        ),
        pytest.param(
            f"DASHBOARD_TOKEN={KIOSK_TOKEN}\nKIOSK_TOKEN={KIOSK_TOKEN}\nGF_SECURITY_ADMIN_PASSWORD=x\n",
            id="kiosk-equals-dashboard",
        ),
    ],
)
def test_main_refuses_before_any_request(main_env, env_text):
    main_env.env_file.write_text(env_text, encoding="utf-8")
    assert ml.main() == 1
    assert main_env.steps == []


def test_main_env_unreadable(main_env):
    main_env.env_file.unlink()
    assert ml.main() == 1
    assert main_env.steps == []


def test_main_out_dir_missing(main_env, monkeypatch, tmp_path):
    monkeypatch.setattr(
        sys, "argv", ["make_logins.py", "--env", str(main_env.env_file), "--out", str(tmp_path / "x/y")]
    )
    assert ml.main() == 1
    assert main_env.steps == []


def test_main_needs_the_kiosk_group(main_env, monkeypatch):
    def no_group(name):
        raise KeyError(name)

    monkeypatch.setattr(ml, "grp", SimpleNamespace(getgrnam=no_group))
    assert ml.main() == 1
    assert main_env.steps == []


def test_main_needs_root(main_env, monkeypatch, capsys):
    monkeypatch.setattr(ml, "os", _Proxy(os, geteuid=lambda: 1000))
    assert ml.main() == 1
    assert "run as root" in capsys.readouterr().err
    assert main_env.steps == []


def test_main_prints_no_secret_values(main_env, capsys):
    assert ml.main() == 0
    out = capsys.readouterr()
    printed = out.out + out.err
    assert "read-only" in out.out
    for value in (KIOSK_TOKEN, DASH_TOKEN, ml.kiosk_session(KIOSK_TOKEN, DASH_TOKEN), "adm1n-pw", "new-grafana-pw"):
        assert value not in printed


def test_main_file_round_trips_through_the_kiosk(main_env, monkeypatch):
    # The upgrade: the file an older make_logins.py wrote (the full login as dashboard_session) is in
    # place and the kiosk refuses it; make_logins.py replaces the whole file; the kiosk then loads it and
    # sets exactly the read-only cookie. Real write_secrets() and kiosk.py, so the key names can't drift.
    kiosk = _load(_FOLDER / "kiosk.py", "jupiter_kiosk_round_trip")
    cfg = kiosk.load_config(
        {
            "screens": [
                {"name": "left", "url": "https://host.example:3000/d/x", "login": "grafana"},
                {"name": "right", "url": "https://host.example/dashboard", "login": "dashboard"},
            ]
        }
    )
    full_login = hmac.new(DASH_TOKEN.encode(), b"brain-gateway-dashboard-session-v1", hashlib.sha256).hexdigest()
    old = {"grafana_user": "closet-kiosk", "grafana_password": "old-grafana-pw", "dashboard_session": full_login}
    main_env.out.write_text(json.dumps(old), encoding="utf-8")
    with pytest.raises(kiosk.ConfigError, match="still holds dashboard_session"):
        kiosk.load_secrets(kiosk.read_json(str(main_env.out)), cfg)

    monkeypatch.setattr(ml, "write_secrets", _REAL_WRITE_SECRETS)
    monkeypatch.setattr(ml, "os", _Proxy(os, geteuid=lambda: 0, chown=lambda path, uid, gid: None))
    assert ml.main() == 0

    assert full_login not in main_env.out.read_text(encoding="utf-8")
    secrets = kiosk.load_secrets(kiosk.read_json(str(main_env.out)), cfg)
    kiosk_cookie = hmac.new(KIOSK_TOKEN.encode(), b"brain-gateway-kiosk-session-v1", hashlib.sha256).hexdigest()
    assert secrets == {
        "grafana_user": "closet-kiosk",
        "grafana_password": "new-grafana-pw",
        "dashboard_kiosk_session": kiosk_cookie,
    }
    dashboard_screen = next(s for s in cfg.screens if s.login == "dashboard")
    cookies = kiosk.screen_cookies(dashboard_screen, secrets)
    assert [(c["name"], c["value"]) for c in cookies] == [("brain_token", kiosk_cookie)]
