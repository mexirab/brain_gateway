"""Tests for monitoring/deadman/jupiter_deadman.py — the off-box dead-man's
switch that runs on Saturn and pages when Jupiter's Watchdog heartbeat stops.

The script is stdlib-only and lives outside orchestrator/, so the orchestrator
test container (which mounts only orchestrator/, scripts/ and data/) skips this
file. Run it on the host:
    python3 -m pytest --noconftest orchestrator/tests/test_jupiter_deadman.py
"""

from __future__ import annotations

import contextlib
import http.client
import http.server
import importlib.util
import json
import logging
import os
import re
import signal
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "monitoring" / "deadman" / "jupiter_deadman.py"
_ALERT_RULES = Path(__file__).resolve().parents[2] / "monitoring" / "prometheus" / "alert-rules.yml"

pytestmark = pytest.mark.skipif(not _SCRIPT.is_file(), reason="needs monitoring/deadman/ (host-only test)")

TOKEN = "k" * 32
POLL = 0.05  # serve_forever poll interval: shutdown() waits up to one poll


def _closed_port() -> int:
    """A loopback port nothing listens on (bound, then released)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ipv6_loopback() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6) as s:
            s.bind(("::1", 0))
    except OSError:
        return False
    return True


@pytest.fixture(scope="module")
def dm():
    spec = importlib.util.spec_from_file_location("jupiter_deadman", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["jupiter_deadman"] = mod  # dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


def _cfg(dm, **overrides):
    env = {
        "DEADMAN_WEBHOOK_TOKEN": TOKEN,
        "DEADMAN_DRY_RUN": "true",
        "DEADMAN_PROBES": "",
    }
    env.update(overrides)
    return dm.load_config(env, need_token=True, need_pushover=True)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def test_defaults(dm):
    cfg = _cfg(dm)
    assert cfg.listen_port == 9466
    assert cfg.max_age == 600
    assert cfg.repeat_seconds == 4 * 3600
    assert [p.name for p in cfg.probes] == ["orchestrator", "home_assistant", "jupiter_ssh"]
    assert cfg.probes[0].url == "http://10.0.0.248:8888/health"
    assert (cfg.probes[2].host, cfg.probes[2].port) == ("10.0.0.248", 22)
    # Fail closed: only Jupiter may connect unless told otherwise.
    assert cfg.allow_from == frozenset({"10.0.0.248"})
    assert cfg.state_dir == ""


def test_target_host_moves_default_probes(dm):
    cfg = _cfg(dm, DEADMAN_TARGET_HOST="192.0.2.9")
    assert all("192.0.2.9" in (p.url or p.host) for p in cfg.probes)
    assert cfg.allow_from == frozenset({"192.0.2.9"})


def test_hostname_target_needs_explicit_allow_list(dm):
    with pytest.raises(dm.ConfigError, match="DEADMAN_ALLOW_FROM"):
        _cfg(dm, DEADMAN_TARGET_HOST="jupiter.lan")
    cfg = _cfg(dm, DEADMAN_TARGET_HOST="jupiter.lan", DEADMAN_ALLOW_FROM="10.0.0.248")
    assert cfg.allow_from == frozenset({"10.0.0.248"})


def test_state_dir_from_systemd(dm):
    assert _cfg(dm, STATE_DIRECTORY="/var/lib/jupiter-deadman:/other").state_dir == "/var/lib/jupiter-deadman"
    assert _cfg(dm, STATE_DIRECTORY="/x", DEADMAN_STATE_DIR="/y").state_dir == "/y"


@pytest.mark.parametrize("token", ["", "short", "x" * 23])
def test_short_or_missing_token_refused(dm, token):
    with pytest.raises(dm.ConfigError, match="DEADMAN_WEBHOOK_TOKEN"):
        _cfg(dm, DEADMAN_WEBHOOK_TOKEN=token)


def test_token_not_needed_for_probe(dm):
    cfg = dm.load_config({}, need_token=False, need_pushover=False)
    assert cfg.token == ""


def test_pushover_required_unless_dry_run(dm):
    with pytest.raises(dm.ConfigError, match="PUSHOVER"):
        _cfg(dm, DEADMAN_DRY_RUN="false")
    cfg = _cfg(dm, DEADMAN_DRY_RUN="false", PUSHOVER_USER_KEY="u", PUSHOVER_APP_TOKEN="a")
    assert not cfg.dry_run


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("DEADMAN_HEARTBEAT_MAX_AGE_SECONDS", "240"),  # below the 300 s floor (2-min cadence)
        ("DEADMAN_LISTEN_PORT", "70000"),
        ("DEADMAN_CHECK_INTERVAL_SECONDS", "soon"),
        ("DEADMAN_CHECK_INTERVAL_SECONDS", "300"),  # DeadmanSwitchBroken would call it stuck
        ("DEADMAN_ALLOW_FROM", "jupiter.lan"),
    ],
)
def test_bad_values_refused(dm, key, value):
    with pytest.raises(dm.ConfigError):
        _cfg(dm, **{key: value})


def test_allow_from_normalized(dm):
    cfg = _cfg(dm, DEADMAN_ALLOW_FROM=" 10.0.0.248 , ::1, ::ffff:10.0.0.9 ")
    assert cfg.allow_from == frozenset({"10.0.0.248", "::1", "10.0.0.9"})


@pytest.mark.parametrize(
    "spec",
    [
        "noequals",
        "a=ftp://host/x",
        "a=tcp://host",  # no port
        "a=http://",  # no host
        "a=http://h:99999/x",  # port out of range
        "a=http://h:1/x,a=http://h:2/y",  # duplicate name
        "bad name=http://h/x",
    ],
)
def test_bad_probe_specs(dm, spec):
    with pytest.raises(dm.ConfigError):
        dm.parse_probes(spec)


def test_probe_spec_parses_kinds(dm):
    probes = dm.parse_probes("web=https://h:8443/x, ssh=tcp://h:22,")
    assert [(p.name, p.kind) for p in probes] == [("web", "http"), ("ssh", "tcp")]


# --------------------------------------------------------------------------
# Paging decisions
# --------------------------------------------------------------------------

MAX_AGE = 600
REPEAT = 4 * 3600


def test_fresh_start_waits_one_window(dm):
    s = dm.State(started_mono=1000)
    assert dm.decide(s, 1000 + MAX_AGE, MAX_AGE, REPEAT) is None
    assert dm.decide(s, 1000 + MAX_AGE + 1, MAX_AGE, REPEAT) == "page"


def test_page_repeat_resolve_cycle(dm):
    s = dm.State(started_mono=0, last_heartbeat_mono=100)
    now = 100 + MAX_AGE + 30
    assert dm.decide(s, now, MAX_AGE, REPEAT) == "page"
    dm.apply_sent(s, "page", now)
    assert s.alerting and s.alert_since_mono == 100  # outage began at the last heartbeat
    assert dm.decide(s, now + 60, MAX_AGE, REPEAT) is None  # no page storm
    assert dm.decide(s, now + REPEAT, MAX_AGE, REPEAT) == "repeat"
    dm.apply_sent(s, "repeat", now + REPEAT)
    assert dm.decide(s, now + REPEAT + 60, MAX_AGE, REPEAT) is None
    s.last_heartbeat_mono = now + REPEAT + 90
    assert dm.decide(s, now + REPEAT + 100, MAX_AGE, REPEAT) == "resolve"
    dm.apply_sent(s, "resolve", now + REPEAT + 100)
    assert not s.alerting
    assert dm.decide(s, now + REPEAT + 160, MAX_AGE, REPEAT) is None


def test_undelivered_page_is_retried(dm):
    s = dm.State(started_mono=0, last_heartbeat_mono=0)
    now = MAX_AGE + 1
    assert dm.decide(s, now, MAX_AGE, REPEAT) == "page"
    # send failed -> apply_sent not called -> next tick pages again
    assert dm.decide(s, now + 60, MAX_AGE, REPEAT) == "page"


def test_start_up_grace_holds_the_first_page(dm):
    s = dm.State(started_mono=0, last_heartbeat_mono=-5000, not_before_mono=180)
    assert dm.decide(s, 100, MAX_AGE, REPEAT) is None
    assert dm.decide(s, 180, MAX_AGE, REPEAT) == "page"


def test_restored_alert_without_heartbeat_does_not_resolve(dm):
    s = dm.State(started_mono=0, alerting=True, alert_since_mono=-900, last_page_mono=-300)
    assert dm.decide(s, 60, MAX_AGE, REPEAT) is None  # start-up clock is not a heartbeat
    s.last_heartbeat_mono = 70
    assert dm.decide(s, 80, MAX_AGE, REPEAT) == "resolve"


def test_page_keeps_outage_start_seen_at_decision(dm):
    s = dm.State(started_mono=0, last_heartbeat_mono=100)
    since = dm.outage_start(s)
    s.last_heartbeat_mono = 800  # a heartbeat landed while the page was being sent
    dm.apply_sent(s, "page", 790, since)
    assert s.alert_since_mono == 100


def _results(dm, *specs):
    return [dm.ProbeResult(name, kind, ok, "x") for name, kind, ok in specs]


def test_diagnosis(dm):
    assert "frozen, powered off" in dm.diagnose(
        _results(dm, ("orch", "http", False), ("ha", "http", False), ("ssh", "tcp", False))
    )
    assert "Docker or the containers" in dm.diagnose(
        _results(dm, ("orch", "http", False), ("ha", "http", False), ("ssh", "tcp", True))
    )
    assert "monitoring stack" in dm.diagnose(
        _results(dm, ("orch", "http", True), ("ha", "http", True), ("ssh", "tcp", True))
    )
    assert "Part of Jupiter" in dm.diagnose(
        _results(dm, ("orch", "http", False), ("ha", "http", True), ("ssh", "tcp", True))
    )
    assert "No probes" in dm.diagnose([])


def test_messages(dm):
    results = _results(dm, ("orchestrator", "http", False), ("jupiter_ssh", "tcp", True))
    never = dm.State(started_mono=0)
    title, msg, prio = dm.build_message("page", never, 900, results)
    assert prio == 1
    assert title == "Jupiter dead-man: no heartbeat for 15 min"
    assert "since this service started" in msg
    assert "orchestrator: DOWN (x)" in msg and "jupiter_ssh: up (x)" in msg
    assert "Runbook" in msg

    stopped = dm.State(started_mono=0, last_heartbeat_mono=300)
    title, msg, prio = dm.build_message("repeat", stopped, 900, results)
    assert prio == 1 and title.endswith("(still down)")
    assert "heartbeat stopped 10 min ago" in msg

    stopped.alert_since_mono = 300
    title, msg, prio = dm.build_message("resolve", stopped, 1500, results)
    assert prio == 0 and "back" in title and "20 min" in msg


def test_message_respects_pushover_limits(dm):
    many = [dm.ProbeResult(f"probe_{i:02d}", "http", False, "timeout" * 5) for i in range(40)]
    title, msg, _ = dm.build_message("page", dm.State(started_mono=0), 10_000, many)
    assert len(title) <= 250 and len(msg) <= 1024


@pytest.mark.parametrize(
    ("payload", "ok"),
    [
        ({"status": "firing", "alerts": [{"status": "firing", "labels": {"alertname": "Watchdog"}}]}, True),
        ({"status": "resolved", "alerts": [{"status": "resolved", "labels": {"alertname": "Watchdog"}}]}, False),
        ({"status": "firing", "alerts": [{"status": "firing", "labels": {"alertname": "NodeDown"}}]}, False),
        ({"status": "firing", "alerts": "nope"}, False),
        ({"status": "firing", "alerts": [None, {"status": "firing", "labels": "x"}]}, False),
        ([], False),
        (None, False),
    ],
)
def test_is_watchdog_heartbeat(dm, payload, ok):
    assert dm.is_watchdog_heartbeat(payload) is ok


# --------------------------------------------------------------------------
# The HTTP receiver
# --------------------------------------------------------------------------


@pytest.fixture
def server(dm):
    def _start(server_cls=None, addr="127.0.0.1", **overrides):
        cfg = _cfg(dm, DEADMAN_LISTEN_ADDR=addr, **overrides)
        app = dm.App(cfg)
        srv = (server_cls or dm.DeadmanHTTPServer)((addr, 0), app)
        t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": POLL}, daemon=True)
        t.start()
        started.append(srv)
        return app, srv.server_address[1]

    started = []
    yield _start
    for srv in started:
        srv.shutdown()
        srv.server_close()


WATCHDOG = {
    "version": "4",
    "status": "firing",
    "receiver": "deadman",
    "alerts": [{"status": "firing", "labels": {"alertname": "Watchdog", "severity": "none"}}],
}


def _post(port, body, token=TOKEN, path="/heartbeat", headers=None, host="127.0.0.1"):
    conn = http.client.HTTPConnection(host, port, timeout=5)
    hdrs = {"Content-Type": "application/json"}
    if token is not None:
        hdrs["Authorization"] = f"Bearer {token}"
    hdrs.update(headers or {})
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    conn.request("POST", path, body=data, headers=hdrs)
    resp = conn.getresponse()
    resp.read()
    conn.close()
    return resp.status


def _get(port, path, host="127.0.0.1"):
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read().decode()
    conn.close()
    return resp.status, body


def _post_eventually(port, body, within=3.0):
    """POST, retrying while the receiver still drops connections (slots busy)."""
    end = time.monotonic() + within
    while True:
        try:
            return _post(port, body)
        except (OSError, http.client.HTTPException):
            if time.monotonic() > end:
                raise
            time.sleep(0.05)


def _dropped_unanswered(sock) -> bool:
    """True when the server closed the connection without a single byte."""
    try:
        return sock.recv(100) == b""
    except ConnectionResetError:
        return True


def test_valid_heartbeat_recorded(server):
    app, port = server()
    assert app.state.last_heartbeat_mono is None
    assert _post(port, WATCHDOG) == 200
    assert app.state.last_heartbeat_mono is not None
    status, metrics = _get(port, "/metrics")
    assert status == 200
    assert 'jupiter_deadman_heartbeats_total{result="ok"} 1' in metrics
    assert "jupiter_deadman_dry_run 1" in metrics
    assert "jupiter_deadman_last_heartbeat_timestamp_seconds 0.000" not in metrics


@pytest.mark.parametrize(
    ("kwargs", "status", "counter"),
    [
        ({"token": None}, 401, "unauthorized"),
        ({"token": "k" * 31}, 401, "unauthorized"),
        ({"token": TOKEN + "x"}, 401, "unauthorized"),
        ({"body": b"{not json"}, 400, "bad_request"),
        ({"body": {"status": "firing", "alerts": []}}, 422, "not_watchdog"),
        ({"path": "/other"}, 404, None),
    ],
)
def test_rejected_heartbeats(server, kwargs, status, counter):
    app, port = server()
    body = kwargs.pop("body", WATCHDOG)
    assert _post(port, body, **kwargs) == status
    assert app.state.last_heartbeat_mono is None
    if counter:
        assert app.heartbeats[counter] == 1


def test_oversized_body_refused_unread(server, dm):
    app, port = server()
    # Declared length over the cap: refused from the header alone.
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(
            (
                "POST /heartbeat HTTP/1.1\r\nHost: x\r\n"
                f"Authorization: Bearer {TOKEN}\r\nContent-Length: {dm.MAX_BODY_BYTES + 1}\r\n\r\n"
            ).encode()
        )
        assert b" 413 " in s.recv(200)
    assert app.heartbeats["too_large"] == 1


def test_allow_list_applied_at_accept(dm):
    app = dm.App(_cfg(dm))  # allow-list defaults to 10.0.0.248
    srv = dm.DeadmanHTTPServer(("127.0.0.1", 0), app)
    try:
        assert srv.verify_request(None, ("10.0.0.248", 5000))
        assert srv.verify_request(None, ("::ffff:10.0.0.248", 5000, 0, 0))
        assert srv.verify_request(None, ("127.0.0.1", 5000))  # loopback: the owner's curl on Saturn
        assert not srv.verify_request(None, ("10.0.0.77", 5000))
        assert not srv.verify_request(None, ("192.0.2.1", 5000))
        assert app.heartbeats["forbidden"] == 2
    finally:
        srv.server_close()


def test_missing_length_refused(server):
    app, port = server()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(f"POST /heartbeat HTTP/1.0\r\nAuthorization: Bearer {TOKEN}\r\n\r\n".encode())
        assert b" 411 " in s.recv(200)
    assert app.heartbeats["bad_request"] == 1


def test_deeply_nested_body_is_a_bad_request(server):
    app, port = server()
    assert _post(port, b"[" * 60000) == 400
    assert app.heartbeats["bad_request"] == 1


def test_connection_deadline_frees_trickling_slots(server, dm, monkeypatch):
    monkeypatch.setattr(dm, "CONNECTION_DEADLINE_SECONDS", 0.5)
    app, port = server()
    hogs = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(dm.MAX_CONNECTIONS)]
    try:
        for h in hogs:
            h.sendall(b"POST /heartbeat HTTP/1.1\r\n")  # and then nothing more
        time.sleep(1.5)  # past the per-connection deadline
        assert _post(port, WATCHDOG) == 200
    finally:
        for h in hogs:
            h.close()


def test_unauthenticated_body_not_parsed(server, monkeypatch, dm):
    app, port = server()
    calls = []
    monkeypatch.setattr(dm, "_parse_json", lambda *a, **k: calls.append(a) or {})
    assert _post(port, WATCHDOG, token="wrong" * 6) == 401
    assert calls == []


def test_healthz_and_unknown_get(server):
    _, port = server()
    assert _get(port, "/healthz") == (200, "ok\n")
    assert _get(port, "/nope")[0] == 404


def test_query_string_does_not_change_routing(server):
    app, port = server()
    assert _get(port, "/metrics?x=1")[0] == 200
    assert _post(port, WATCHDOG, path="/heartbeat?from=alertmanager") == 200
    assert app.heartbeats["ok"] == 1


def test_fifth_concurrent_connection_is_dropped(server, dm, caplog):
    app, port = server()
    # Four idle connections hold every slot (their handlers wait for a request line).
    hogs = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(dm.MAX_CONNECTIONS)]
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as extra:
            assert _dropped_unanswered(extra)
        assert f"all {dm.MAX_CONNECTIONS} connection slots busy" in caplog.text
    finally:
        for h in hogs:
            h.close()
    # Hung-up clients give their slots back well before the 15 s deadline.
    assert _post_eventually(port, WATCHDOG) == 200
    assert app.heartbeats["ok"] == 1


def test_handler_errors_release_their_slots(server, dm, monkeypatch, caplog):
    app, port = server()
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("metrics broke")

    monkeypatch.setattr(app, "render_metrics", broken)
    # More handler failures than slots: a slot leaked per error would leave
    # none, and this loop would never reach the count. (A connection that
    # lands before the previous thread released its slot is dropped as busy
    # and simply retried.)
    end = time.monotonic() + 5
    while len(calls) <= dm.MAX_CONNECTIONS:
        assert time.monotonic() < end, f"only {len(calls)} handler runs: slots leaked"
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"GET /metrics HTTP/1.0\r\n\r\n")
            assert _dropped_unanswered(s)
    assert _post_eventually(port, WATCHDOG) == 200
    failures = [r.getMessage() for r in caplog.records if "failed:" in r.getMessage()]
    assert failures == ["connection from 127.0.0.1 failed: RuntimeError (logged at most once a minute)"]
    assert "Traceback" not in caplog.text and "metrics broke" not in caplog.text


class _NoThreads(socketserver.ThreadingMixIn):
    """Sits between DeadmanHTTPServer and ThreadingMixIn in the MRO: the
    first ``fail`` thread starts raise, as under memory/thread exhaustion."""

    fail = 0

    def process_request(self, request, client_address):
        if _NoThreads.fail > 0:
            _NoThreads.fail -= 1
            raise RuntimeError("can't start new thread")
        super().process_request(request, client_address)


def test_failed_thread_start_releases_its_slot(server, dm):
    class _Server(dm.DeadmanHTTPServer, _NoThreads):
        pass

    _NoThreads.fail = dm.MAX_CONNECTIONS + 1
    app, port = server(server_cls=_Server)
    for _ in range(dm.MAX_CONNECTIONS + 1):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            assert _dropped_unanswered(s)
    assert _NoThreads.fail == 0
    assert _post(port, WATCHDOG) == 200  # no retry needed: released before the next accept
    assert app.heartbeats["ok"] == 1


def test_handle_error_logs_once_a_minute(dm, caplog):
    clock = _Clock(1000.0)
    app = dm.App(_cfg(dm), clock=clock, wall_clock=_Clock(1_800_000_000.0))
    srv = dm.DeadmanHTTPServer(("127.0.0.1", 0), app)

    def fail():
        try:
            raise ConnectionResetError("peer said something private")
        except ConnectionResetError:
            srv.handle_error(None, ("::ffff:10.0.0.248", 5000, 0, 0))

    try:
        with caplog.at_level(logging.WARNING, logger="jupiter-deadman"):
            fail()
            fail()
            clock.t += dm.LOG_INTERVAL_SECONDS - 1
            fail()
            clock.t += 1
            fail()
    finally:
        srv.server_close()
    lines = [r.getMessage() for r in caplog.records]
    assert lines == ["connection from 10.0.0.248 failed: ConnectionResetError (logged at most once a minute)"] * 2


def test_allow_list_mapped_and_scoped_addresses(dm):
    app = dm.App(_cfg(dm, DEADMAN_ALLOW_FROM="fe80::1"))
    srv = dm.DeadmanHTTPServer(("127.0.0.1", 0), app)
    try:
        # A dual-stack "::" listener sees IPv4 loopback as ::ffff:127.0.0.1.
        assert srv.verify_request(None, ("::ffff:127.0.0.1", 5000, 0, 0))
        assert srv.verify_request(None, ("::1", 5000, 0, 0))
        assert srv.verify_request(None, ("fe80::1%eth0", 5000, 0, 2))  # scope id ignored
        assert not srv.verify_request(None, ("fe80::2%eth0", 5000, 0, 2))
        assert app.heartbeats["forbidden"] == 1
    finally:
        srv.server_close()


@pytest.mark.skipif(not _ipv6_loopback(), reason="no IPv6 loopback here")
def test_ipv6_listener_and_probes(server, dm):
    app, port = server(addr="::1")
    assert _post(port, WATCHDOG, host="::1") == 200
    assert _get(port, "/healthz", host="::1") == (200, "ok\n")
    assert app.heartbeats == {**dict.fromkeys(app.heartbeats, 0), "ok": 1}
    # Probe URLs with IPv6 literals (bracketed host, Host header) work too.
    probes = dm.parse_probes(f"web=http://[::1]:{port}/healthz,port=tcp://[::1]:{port}")
    results = dm.run_probes(probes, timeout=2)
    assert [(r.ok, r.detail) for r in results] == [(True, "http 200"), (True, "port open")]


@pytest.mark.parametrize(
    ("length", "status", "counter"),
    [("-1", 411, "bad_request"), ("12abc", 411, "bad_request"), ("0", 422, "not_watchdog")],
)
def test_content_length_edge_cases(server, length, status, counter):
    app, port = server()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(
            f"POST /heartbeat HTTP/1.0\r\nAuthorization: Bearer {TOKEN}\r\nContent-Length: {length}\r\n\r\n".encode()
        )
        assert f" {status} ".encode() in s.recv(200)
    assert app.heartbeats[counter] == 1 and app.state.last_heartbeat_mono is None


def test_body_of_exactly_the_cap_is_accepted(server, dm):
    app, port = server()
    body = json.dumps(WATCHDOG).encode()
    body += b" " * (dm.MAX_BODY_BYTES - len(body))
    assert len(body) == dm.MAX_BODY_BYTES
    assert _post(port, body) == 200
    assert app.heartbeats["ok"] == 1


def test_non_ascii_authorization_is_a_plain_401(server):
    app, port = server()
    # Raw UTF-8 in the header: must be a counted 401, not a crashed handler.
    status = _post(port, WATCHDOG, token=None, headers={"Authorization": f"Bearer {TOKEN}é".encode()})
    assert status == 401 and app.heartbeats["unauthorized"] == 1


def test_stalled_body_times_out_as_a_bad_request(server, dm, monkeypatch):
    monkeypatch.setattr(dm.Handler, "timeout", 0.3)
    app, port = server()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(
            f"POST /heartbeat HTTP/1.0\r\nAuthorization: Bearer {TOKEN}\r\nContent-Length: 100\r\n\r\n{{".encode()
        )  # promises 100 bytes, sends 1, then stalls
        assert b" 400 " in s.recv(200)
    assert app.heartbeats["bad_request"] == 1 and app.state.last_heartbeat_mono is None


# --------------------------------------------------------------------------
# The checker (tick) with probes + Pushover stubbed
# --------------------------------------------------------------------------


class _Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def ticking(dm, monkeypatch):
    sent = []
    outcome = {"ok": True}
    clock, wall = _Clock(1000.0), _Clock(1_800_000_000.0)
    monkeypatch.setattr(
        dm, "run_probes", lambda probes, timeout: [dm.ProbeResult("orchestrator", "http", False, "timeout")]
    )

    def fake_send(cfg, title, message, priority):
        sent.append((title, priority))
        return outcome["ok"]

    monkeypatch.setattr(dm, "send_pushover", fake_send)

    def make(**overrides):
        cfg = _cfg(dm, DEADMAN_DRY_RUN="false", PUSHOVER_USER_KEY="u", PUSHOVER_APP_TOKEN="a", **overrides)
        return dm.App(cfg, clock=clock, wall_clock=wall)

    return make, sent, outcome, clock, wall


def _advance(clock, wall, seconds):
    clock.t += seconds
    wall.t += seconds


def test_tick_pages_repeats_then_resolves(ticking):
    make, sent, _, clock, wall = ticking
    app = make()
    app.record_heartbeat()
    _advance(clock, wall, 300)
    app.tick()
    assert sent == []
    _advance(clock, wall, 400)  # 700 s silent > 600
    app.tick()
    app.tick()
    assert len(sent) == 1 and sent[0][1] == 1 and "no heartbeat for 12 min" in sent[0][0]
    _advance(clock, wall, 4 * 3600)
    app.tick()
    assert len(sent) == 2 and sent[1][0].endswith("(still down)")
    app.record_heartbeat()
    app.tick()
    assert len(sent) == 3 and sent[2][1] == 0
    metrics = app.render_metrics()
    assert 'jupiter_deadman_notifications_total{kind="page",result="ok"} 1' in metrics
    assert 'jupiter_deadman_notifications_total{kind="repeat",result="ok"} 1' in metrics
    assert 'jupiter_deadman_notifications_total{kind="resolve",result="ok"} 1' in metrics
    assert 'jupiter_deadman_probe_up{probe="orchestrator"} 0' in metrics
    assert "jupiter_deadman_alerting 0" in metrics


def test_tick_retries_failed_send(ticking):
    make, sent, outcome, clock, wall = ticking
    app = make()
    _advance(clock, wall, 700)
    outcome["ok"] = False
    app.tick()
    assert not app.state.alerting
    outcome["ok"] = True
    app.tick()
    assert app.state.alerting and len(sent) == 2
    assert 'kind="page",result="error"} 1' in app.render_metrics()


def test_broken_probes_never_block_the_page(ticking, dm, monkeypatch):
    make, sent, _, clock, wall = ticking

    def explode(probes, timeout):
        raise http.client.BadStatusLine("SSH-2.0-OpenSSH")

    monkeypatch.setattr(dm, "run_probes", explode)
    app = make()
    _advance(clock, wall, 700)
    app.tick()
    assert len(sent) == 1 and app.state.alerting


def test_state_survives_a_restart(ticking, tmp_path):
    make, sent, _, clock, wall = ticking
    app = make(DEADMAN_STATE_DIR=str(tmp_path))
    app.record_heartbeat()
    _advance(clock, wall, 700)
    app.tick()
    assert len(sent) == 1
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["alerting"] is True and saved["last_heartbeat_wall"] == pytest.approx(wall.t - 700)

    # Restart mid-outage: no second page (already paged), clock not reset.
    _advance(clock, wall, 120)
    app2 = make(DEADMAN_STATE_DIR=str(tmp_path))
    assert app2.state.alerting
    assert app2.state.last_heartbeat_mono == pytest.approx(clock.t - 820)
    app2.tick()
    assert len(sent) == 1
    # The heartbeat comes back after the restart: one "back" notice.
    app2.record_heartbeat()
    app2.tick()
    assert len(sent) == 2 and sent[1][1] == 0
    assert json.loads((tmp_path / "state.json").read_text())["alerting"] is False


def test_restart_after_long_stop_waits_for_a_heartbeat(ticking, tmp_path, dm):
    make, sent, _, clock, wall = ticking
    make(DEADMAN_STATE_DIR=str(tmp_path)).record_heartbeat()
    _advance(clock, wall, 86400)  # switch stopped for a day (planned Jupiter downtime)
    app = make(DEADMAN_STATE_DIR=str(tmp_path))
    app.tick()
    assert sent == []  # start-up grace: a heartbeat is due within ~2 min
    _advance(clock, wall, dm.STARTUP_GRACE_SECONDS)
    app.tick()
    assert len(sent) == 1


def test_unreadable_state_is_ignored(ticking, tmp_path):
    make, *_ = ticking
    (tmp_path / "state.json").write_text("{not json")
    app = make(DEADMAN_STATE_DIR=str(tmp_path))
    assert app.state.last_heartbeat_mono is None and not app.state.alerting


@pytest.mark.parametrize(
    "content",
    [
        "[]",
        '"just a string"',
        '{"last_heartbeat_wall": "1799999000", "alerting": "yes"}',  # wrong types
        '{"last_heartbeat_wall": -5, "alerting": 1}',  # 1 is not true
    ],
)
def test_malformed_state_values_are_ignored(ticking, tmp_path, content):
    make, *_ = ticking
    (tmp_path / "state.json").write_text(content)
    app = make(DEADMAN_STATE_DIR=str(tmp_path))
    assert app.state.last_heartbeat_mono is None and app.state.last_heartbeat_wall == 0
    assert not app.state.alerting and app.state.not_before_mono == 0


@pytest.mark.parametrize(("ahead", "restored"), [(3600, False), (30, True)])
def test_saved_heartbeat_from_the_future(ticking, tmp_path, ahead, restored):
    """Clock stepped back since the save: an hour ahead is not trusted; a few
    seconds ahead is restored as "just now", never as younger than now."""
    make, _, _, clock, wall = ticking
    (tmp_path / "state.json").write_text(json.dumps({"last_heartbeat_wall": wall.t + ahead}))
    app = make(DEADMAN_STATE_DIR=str(tmp_path))
    if restored:
        assert app.state.last_heartbeat_mono == clock.t
    else:
        assert app.state.last_heartbeat_mono is None and app.state.last_heartbeat_wall == 0


def test_unwritable_state_dir_never_breaks_heartbeats(ticking, tmp_path, dm, caplog):
    make, _, _, clock, wall = ticking
    missing = tmp_path / "gone"
    app = make(DEADMAN_STATE_DIR=str(missing))
    with caplog.at_level(logging.WARNING, logger="jupiter-deadman"):
        app.record_heartbeat()
        app.record_heartbeat()
        clock.t += dm.LOG_INTERVAL_SECONDS
        app.record_heartbeat()
    assert app.heartbeats["ok"] == 3 and app.state.last_heartbeat_wall == wall.t
    assert sum("could not save state" in r.getMessage() for r in caplog.records) == 2
    assert not missing.exists()


def test_dry_run_tick_pages_once_and_counts_dry_run(dm, monkeypatch, caplog):
    """Dry run uses the real send_pushover: logged, not sent, and the alert
    state still advances so the log doesn't repeat the page every minute."""
    monkeypatch.setattr(dm, "run_probes", lambda probes, timeout: [])
    clock, wall = _Clock(1000.0), _Clock(1_800_000_000.0)
    app = dm.App(_cfg(dm), clock=clock, wall_clock=wall)  # DEADMAN_DRY_RUN=true
    _advance(clock, wall, 700)
    with caplog.at_level(logging.WARNING, logger="jupiter-deadman"):
        app.tick()
        _advance(clock, wall, 60)
        app.tick()
    assert app.state.alerting
    metrics = app.render_metrics()
    assert 'jupiter_deadman_notifications_total{kind="page",result="dry_run"} 1' in metrics
    assert "jupiter_deadman_alerting 1" in metrics
    assert f"jupiter_deadman_last_check_timestamp_seconds {wall.t:.3f}" in metrics
    messages = [r.getMessage() for r in caplog.records]
    assert sum(m.startswith("DRY RUN, not sent: priority 1") for m in messages) == 1
    assert not any("notification sent" in m for m in messages)


def test_probe_changes_logged_once_per_transition(dm, monkeypatch, caplog):
    states = iter([True, True, False, False, True])

    def probes(_probes, _timeout):
        ok = next(states)
        return [dm.ProbeResult("orchestrator", "http", ok, "http 200" if ok else "timeout")]

    monkeypatch.setattr(dm, "run_probes", probes)
    app = dm.App(_cfg(dm), clock=_Clock(), wall_clock=_Clock(1_800_000_000.0))
    app.record_heartbeat()  # fresh heartbeat: no paging in this test
    with caplog.at_level(logging.INFO, logger="jupiter-deadman"):
        for _ in range(5):
            app.tick()
    changes = [r.getMessage() for r in caplog.records if r.getMessage().startswith("probe ")]
    assert changes == ["probe orchestrator: up -> DOWN (timeout)", "probe orchestrator: DOWN -> up (http 200)"]


def test_rejections_logged_once_a_minute_per_reason(dm, caplog):
    clock = _Clock()
    app = dm.App(_cfg(dm), clock=clock, wall_clock=_Clock(1_800_000_000.0))
    with caplog.at_level(logging.WARNING, logger="jupiter-deadman"):
        for _ in range(3):
            app.count_rejection("unauthorized", "10.0.0.248")
        app.count_rejection("not_watchdog", "10.0.0.248")  # its own budget
        clock.t += dm.LOG_INTERVAL_SECONDS - 1
        app.count_rejection("unauthorized", "10.0.0.248")
        clock.t += 1
        app.count_rejection("unauthorized", "10.0.0.248")
    reasons = [re.search(r"\((\w+)\)", r.getMessage())[1] for r in caplog.records]
    assert reasons == ["unauthorized", "not_watchdog", "unauthorized"]
    assert app.heartbeats["unauthorized"] == 5 and app.heartbeats["not_watchdog"] == 1


# --------------------------------------------------------------------------
# /metrics contract
# --------------------------------------------------------------------------


def test_metrics_text_is_well_formed(ticking, dm):
    make, _, _, clock, wall = ticking
    app = make()
    app.count_rejection("forbidden", "192.0.2.1")
    _advance(clock, wall, 700)
    app.tick()  # adds a probe series and a notification series
    text = app.render_metrics()
    assert text.endswith("\n")
    types: dict[str, str] = {}
    samples = 0
    for line in text.splitlines():
        if line.startswith("# HELP "):
            continue
        typed = re.fullmatch(r"# TYPE (\w+) (gauge|counter)", line)
        if typed:
            types[typed[1]] = typed[2]
            continue
        sample = re.fullmatch(r'(\w+)(?:\{\w+="[^"\\\n]*"(?:,\w+="[^"\\\n]*")*\})? (\S+)', line)
        assert sample, f"not exposition format: {line!r}"
        assert sample[1] in types, f"sample before its TYPE: {line!r}"
        assert types[sample[1]] != "counter" or sample[1].endswith("_total"), line
        float(sample[2])
        samples += 1
    assert {"jupiter_deadman_up", "jupiter_deadman_probe_up", "jupiter_deadman_notifications_total"} <= set(types)
    assert samples >= 14  # 6 gauges + 6 heartbeat results + 1 probe + 1 notification


@pytest.mark.skipif(not _ALERT_RULES.is_file(), reason="needs monitoring/prometheus/alert-rules.yml")
def test_alert_rules_only_use_exported_metrics(dm):
    """DeadmanSwitchBroken reads the switch's own series: a rename on either
    side would silently disarm it (absent series never fire)."""
    used = set(re.findall(r"\bjupiter_deadman_\w+", _ALERT_RULES.read_text()))
    assert used, "the jupiter_deadman rules no longer read any switch metric"
    exported = set(re.findall(r"^# TYPE (\w+)", dm.App(_cfg(dm)).render_metrics(), re.M))
    assert used <= exported, used - exported


# --------------------------------------------------------------------------
# Probes against a local server
# --------------------------------------------------------------------------


class _Fixed(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        code = int(self.path.strip("/"))
        self.send_response(code)
        if 300 <= code < 400:
            self.send_header("Location", "http://192.0.2.1/elsewhere")  # must not be followed
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def web():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Fixed)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": POLL}, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


@pytest.mark.parametrize(("code", "ok"), [(200, True), (302, True), (401, True), (404, True), (503, False)])
def test_http_probe_status(dm, web, code, ok):
    probe = dm.Probe(name="p", kind="http", url=f"http://127.0.0.1:{web}/{code}")
    result = dm.run_probe(probe, timeout=3)
    assert result.ok is ok and result.detail == f"http {code}"


def test_probes_refused(dm):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed again when the block exits
    for probe in (
        dm.Probe(name="t", kind="tcp", host="127.0.0.1", port=port),
        dm.Probe(name="h", kind="http", url=f"http://127.0.0.1:{port}/health"),
    ):
        result = dm.run_probe(probe, timeout=2)
        assert not result.ok and result.detail == "refused"


@pytest.fixture
def raw_server():
    """A TCP server whose per-connection behaviour the test supplies."""
    servers = []

    def _start(behaviour):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)

        def loop():
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                threading.Thread(target=behaviour, args=(conn,), daemon=True).start()

        threading.Thread(target=loop, daemon=True).start()
        servers.append(listener)
        return listener.getsockname()[1]

    yield _start
    for listener in servers:
        listener.close()


def test_non_http_answer_is_down_not_an_exception(dm, raw_server):
    def ssh_banner(conn):
        conn.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")
        conn.close()

    port = raw_server(ssh_banner)
    result = dm.run_probe(dm.Probe(name="p", kind="http", url=f"http://127.0.0.1:{port}/"), timeout=2)
    assert (result.ok, result.detail) == (False, "not HTTP")


def test_trickling_server_hits_the_total_deadline(dm, raw_server):
    def trickle(conn):
        with contextlib.suppress(OSError):
            for byte in b"HTTP/1.0 200 OK\r\n":
                conn.sendall(bytes([byte]))
                time.sleep(0.3)
        conn.close()

    port = raw_server(trickle)
    started = time.monotonic()
    result = dm.run_probe(dm.Probe(name="p", kind="http", url=f"http://127.0.0.1:{port}/"), timeout=1)
    assert (result.ok, result.detail) == (False, "timeout")
    assert time.monotonic() - started < 2


def test_status_line_split_across_reads(dm, raw_server):
    def split(conn):
        with contextlib.suppress(OSError):
            conn.recv(1024)
            conn.sendall(b"HTTP/1.1 ")
            time.sleep(0.05)
            conn.sendall(b"204 No Content\r\n\r\n")
        conn.close()

    port = raw_server(split)
    result = dm.run_probe(dm.Probe(name="p", kind="http", url=f"http://127.0.0.1:{port}/"), timeout=2)
    assert (result.ok, result.detail) == (True, "http 204")


def test_https_probe_of_a_plain_http_port_fails_tls_cleanly(dm, raw_server):
    def plain_http(conn):
        with contextlib.suppress(OSError):
            conn.recv(4096)  # the TLS ClientHello
            conn.sendall(b"HTTP/1.0 400 Bad Request\r\n\r\n")
            conn.settimeout(2)
            while conn.recv(4096):  # drain until the client hangs up, so close() sends no RST
                pass
        conn.close()

    port = raw_server(plain_http)
    result = dm.run_probe(dm.Probe(name="p", kind="http", url=f"https://127.0.0.1:{port}/"), timeout=2)
    assert not result.ok
    # Used to read "str": ssl.SSLError.reason is OpenSSL's reason string.
    assert re.fullmatch(r"SSL\w*Error errno \d+", result.detail), result.detail


def _openssl_error(cls, errno, reason):
    exc = cls(errno, reason.lower())
    exc.reason = reason  # what the ssl module sets on errors raised by OpenSSL
    return exc


@pytest.mark.parametrize(
    ("make_exc", "detail"),
    [
        (
            lambda: _openssl_error(ssl.SSLCertVerificationError, 1, "CERTIFICATE_VERIFY_FAILED"),
            "SSLCertVerificationError errno 1",
        ),
        (
            lambda: urllib.error.URLError(_openssl_error(ssl.SSLCertVerificationError, 1, "CERTIFICATE_VERIFY_FAILED")),
            "SSLCertVerificationError errno 1",
        ),
        (lambda: _openssl_error(ssl.SSLError, 1, "WRONG_VERSION_NUMBER"), "SSLError errno 1"),
        (lambda: urllib.error.URLError(ConnectionRefusedError(111, "refused")), "refused"),
        (lambda: urllib.error.URLError(TimeoutError()), "timeout"),
        (lambda: urllib.error.URLError("unknown url type: ftp"), "URLError"),  # reason is a str
        (lambda: http.client.RemoteDisconnected("closed"), "RemoteDisconnected"),
    ],
)
def test_error_detail_names_the_failure(dm, make_exc, detail):
    assert dm._error_detail(make_exc()) == detail


class _FakePushover(http.server.BaseHTTPRequestHandler):
    reply = (200, b'{"status":1}')
    seen: list = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _FakePushover.seen.append(urllib.parse.parse_qs(body.decode()))
        code, payload = _FakePushover.reply
        self.send_response(code)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.mark.parametrize(
    ("reply", "ok"),
    [((200, b'{"status":1}'), True), ((200, b'{"status":0}'), False), ((400, b"{}"), False), ((200, b"<html>"), False)],
)
def test_send_pushover_against_a_stub(dm, monkeypatch, reply, ok):
    srv = http.server.HTTPServer(("127.0.0.1", 0), _FakePushover)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": POLL}, daemon=True).start()
    _FakePushover.reply, _FakePushover.seen = reply, []
    monkeypatch.setattr(dm, "PUSHOVER_URL", f"http://127.0.0.1:{srv.server_address[1]}/1/messages.json")
    try:
        cfg = _cfg(dm, DEADMAN_DRY_RUN="false", PUSHOVER_USER_KEY="ukey", PUSHOVER_APP_TOKEN="atok")
        assert dm.send_pushover(cfg, "T", "M", 1) is ok
    finally:
        srv.shutdown()
        srv.server_close()
    fields = _FakePushover.seen[0]
    assert fields["token"] == ["atok"] and fields["user"] == ["ukey"] and fields["priority"] == ["1"]
    assert fields["title"] == ["T"] and fields["message"] == ["M"]


def test_send_pushover_unreachable(dm, monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(dm, "PUSHOVER_URL", f"http://127.0.0.1:{port}/")
    cfg = _cfg(dm, DEADMAN_DRY_RUN="false", PUSHOVER_USER_KEY="u", PUSHOVER_APP_TOKEN="a")
    assert dm.send_pushover(cfg, "T", "M", 1) is False


def test_dry_run_sends_nothing(dm, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network used in dry run")

    monkeypatch.setattr(dm.urllib.request, "urlopen", boom)
    assert dm.send_pushover(_cfg(dm), "t", "m", 1) is True


def test_test_notify_ignores_dry_run_but_needs_keys(dm, monkeypatch):
    seen = []
    monkeypatch.setattr(dm, "send_pushover", lambda cfg, *a: seen.append(cfg.dry_run) or True)
    assert dm.cmd_test_notify(_cfg(dm)) == 2  # dry run, no keys
    assert seen == []
    assert dm.cmd_test_notify(_cfg(dm, PUSHOVER_USER_KEY="u", PUSHOVER_APP_TOKEN="a")) == 0
    assert seen == [False]  # sent for real despite DEADMAN_DRY_RUN=true


# --------------------------------------------------------------------------
# Commands and the serve() loop
# --------------------------------------------------------------------------


def test_cmd_probe_exit_codes(dm, web, capsys):
    up = f"web=http://127.0.0.1:{web}/204,port=tcp://127.0.0.1:{web}"
    assert dm.cmd_probe(_cfg(dm, DEADMAN_PROBES=up)) == 0
    out = capsys.readouterr().out
    assert re.search(r"^web\s+up\s+http 204$", out, re.M) and out.endswith("All probes answered.\n")

    down = f"web=http://127.0.0.1:{web}/503,gone=tcp://127.0.0.1:{_closed_port()}"
    assert dm.cmd_probe(_cfg(dm, DEADMAN_PROBES=down)) == 1
    out = capsys.readouterr().out
    assert re.search(r"^web\s+DOWN\s+http 503$", out, re.M)
    assert re.search(r"^gone\s+DOWN\s+refused$", out, re.M)
    assert "Saturn reaches nothing on Jupiter" in out


def _run_script(*args, **env):
    # A clean environment: no DEADMAN_*/PUSHOVER_* from the caller, and
    # DEADMAN_PROBES always set so nothing probes the real Jupiter.
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env},
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_main_exit_codes(web):
    gone = f"gone=tcp://127.0.0.1:{_closed_port()}"
    r = _run_script("serve", DEADMAN_PROBES=gone)  # no token
    assert r.returncode == 2 and r.stderr.startswith("ERROR config error: DEADMAN_WEBHOOK_TOKEN"), r.stderr
    r = _run_script("probe", DEADMAN_PROBES=gone)
    assert r.returncode == 1 and "DOWN" in r.stdout, r.stdout + r.stderr
    r = _run_script("probe", DEADMAN_PROBES=f"web=http://127.0.0.1:{web}/200")
    assert r.returncode == 0 and "All probes answered." in r.stdout, r.stdout + r.stderr


def _wait_healthy(port, proc, within=10.0):
    end = time.monotonic() + within
    while time.monotonic() < end:
        if proc.poll() is not None:
            pytest.fail(f"serve exited early ({proc.returncode}): {proc.stderr.read()}")
        try:
            if _get(port, "/healthz")[0] == 200:
                return
        except (OSError, http.client.HTTPException):
            time.sleep(0.05)
    pytest.fail("serve never answered /healthz")


def test_serve_exits_0_on_sigterm(tmp_path):
    """The real process, as systemd runs it: journal-prefixed logs, a heartbeat
    end to end (saved to the state dir), then SIGTERM -> clean exit 0."""
    port = _closed_port()
    proc = subprocess.Popen(
        [sys.executable, str(_SCRIPT), "serve"],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DEADMAN_WEBHOOK_TOKEN": TOKEN,
            "DEADMAN_DRY_RUN": "true",
            "DEADMAN_LISTEN_ADDR": "127.0.0.1",
            "DEADMAN_LISTEN_PORT": str(port),
            "DEADMAN_ALLOW_FROM": "192.0.2.1",
            "DEADMAN_PROBES": f"gone=tcp://127.0.0.1:{_closed_port()}",
            "DEADMAN_STATE_DIR": str(tmp_path),
            "JOURNAL_STREAM": "8:12345",  # what systemd sets when stderr is the journal
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_healthy(port, proc)
        assert _post(port, WATCHDOG) == 200
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 0, err
    assert "<6>INFO listening on 127.0.0.1:" in err
    assert "<6>INFO first heartbeat received from Alertmanager" in err
    assert "<6>INFO signal 15 received, stopping" in err
    assert "Traceback" not in err
    assert json.loads((tmp_path / "state.json").read_text())["last_heartbeat_wall"] > 0


def test_serve_exits_1_when_the_http_thread_dies(dm, monkeypatch, caplog):
    """A dead receiver must stop the unit (exit 1, systemd restarts it) rather
    than leave a checker that can never get a heartbeat. A failing check is
    logged and does not end the loop by itself."""

    def selector_broke(self):
        raise RuntimeError("selector broke")

    def check_broke(self):
        raise RuntimeError("check broke")

    # service_actions runs inside serve_forever's loop, so the real
    # serve_forever raises (and still marks itself shut down for shutdown()).
    monkeypatch.setattr(dm.DeadmanHTTPServer, "service_actions", selector_broke)
    monkeypatch.setattr(dm.App, "tick", check_broke)
    cfg = _cfg(
        dm,
        DEADMAN_LISTEN_ADDR="127.0.0.1",
        DEADMAN_LISTEN_PORT=str(_closed_port()),
        DEADMAN_PROBES=f"gone=tcp://127.0.0.1:{_closed_port()}",
    )
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    # Hang guard: if serve() never notices, SIGTERM it (its own handler) -> 0, and the assert fails.
    guard = threading.Timer(10, os.kill, args=(os.getpid(), signal.SIGTERM))
    guard.daemon = True
    guard.start()
    try:
        rc = dm.serve(cfg)
    finally:
        guard.cancel()
        for sig, handler in saved.items():
            signal.signal(sig, handler)
    assert rc == 1
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert {r.getMessage() for r in errors} == {"check failed", "HTTP server stopped unexpectedly"}


@pytest.mark.parametrize(
    ("level", "prefix"),
    [
        (logging.DEBUG, "<7>DEBUG"),
        (logging.INFO, "<6>INFO"),
        (logging.WARNING, "<4>WARNING"),
        (logging.ERROR, "<3>ERROR"),
        (logging.CRITICAL, "<3>CRITICAL"),
    ],
)
def test_journal_formatter_priorities(dm, level, prefix):
    record = logging.LogRecord("jupiter-deadman", level, __file__, 1, "probe %s down", ("x",), None)
    assert dm._JournalFormatter().format(record) == f"{prefix} probe x down"
