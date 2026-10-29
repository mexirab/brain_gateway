#!/usr/bin/env python3
"""Off-box dead-man's switch for Jupiter. Runs on Saturn, not Jupiter.

Why it exists: Jupiter runs the whole monitoring stack (Prometheus,
Alertmanager) next to everything it watches. When Jupiter itself goes
down — dockerd crash (2026-10-26, 2.5 h silent), freeze, power loss — the
watcher dies with it and nothing pages. This service is the outside
observer.

How it works:

1. Jupiter's Prometheus has a ``Watchdog`` alert that fires for as long as
   Jupiter can page (it is ingesting data and its Pushover deliveries are
   not failing). Its Alertmanager route POSTs it to ``/heartbeat`` here
   about every 2 minutes with a bearer token. A heartbeat proves the whole
   chain works: Jupiter is up, Prometheus ingests and evaluates rules,
   Alertmanager delivers notifications.
2. Every ``DEADMAN_CHECK_INTERVAL_SECONDS`` this service probes Jupiter
   directly (orchestrator ``/health``, Home Assistant, the SSH port) so a
   page can say *what* is down.
3. No heartbeat for ``DEADMAN_HEARTBEAT_MAX_AGE_SECONDS`` -> one Pushover
   page (priority 1) with the probe results, repeated every
   ``DEADMAN_REPEAT_HOURS`` while it lasts, and a priority-0 "back" notice
   when heartbeats resume.

Probes never page on their own: while heartbeats arrive, Jupiter's own
alerts (OrchestratorDown, ...) own per-service paging, and a second page
from here would only duplicate them.

Jupiter's Prometheus scrapes ``/metrics`` here, so a dead or misconfigured
switch shows up there as ``DeadmanSwitchBroken``.

Only Jupiter's address (``DEADMAN_ALLOW_FROM``) and loopback get past
accept(); the last heartbeat and the alert state are saved in
``$STATE_DIRECTORY`` so a restart doesn't reset the clock or page twice.

Standard library only (Python >= 3.10), so it needs no venv and does not
depend on Docker. Configuration is environment variables; see
``jupiter-deadman.env.example``. Install steps: ``monitoring/deadman/README.md``.

Subcommands:
  serve        run the receiver + checker (what the systemd unit runs)
  probe        probe Jupiter once, print the results, exit 1 if any is down
  test-notify  send one low-priority Pushover test notice and exit
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import http.server
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace

log = logging.getLogger("jupiter-deadman")

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
MIN_TOKEN_LEN = 24
MAX_BODY_BYTES = 64 * 1024
# Only Jupiter (and loopback) get past accept(), and it sends one heartbeat
# and one scrape at a time; 4 leaves headroom without letting a flood of
# slow connections grow threads or memory.
MAX_CONNECTIONS = 4
SOCKET_TIMEOUT_SECONDS = 10  # per read
CONNECTION_DEADLINE_SECONDS = 15  # whole connection, however slowly it trickles
LOG_INTERVAL_SECONDS = 60  # rate limit for repeated warnings
# After a restart with a saved (old) heartbeat, wait this long for a fresh one
# before paging, so starting the switch after planned downtime doesn't page.
STARTUP_GRACE_SECONDS = 180
STATE_FILE_NAME = "state.json"
# Pushover limits: title 250 chars, message 1024 chars.
TITLE_MAX = 250
MESSAGE_MAX = 1024


class ConfigError(Exception):
    pass


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    name: str
    kind: str  # "http" or "tcp"
    url: str = ""
    host: str = ""
    port: int = 0


@dataclass(frozen=True)
class Config:
    listen_addr: str
    listen_port: int
    token: str
    allow_from: frozenset[str]
    max_age: float
    check_interval: float
    probe_timeout: float
    repeat_seconds: float
    probes: tuple[Probe, ...]
    pushover_user: str
    pushover_token: str
    dry_run: bool
    state_dir: str = ""


def _env_number(env: dict[str, str], key: str, default: float, lo: float, hi: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc
    if not lo <= value <= hi:
        raise ConfigError(f"{key} must be between {lo:g} and {hi:g}, got {value:g}")
    return value


def _env_bool(env: dict[str, str], key: str) -> bool:
    return env.get(key, "").strip().lower() in {"1", "true", "yes", "on"}


def parse_probes(spec: str) -> tuple[Probe, ...]:
    """Parse ``name=http://host:port/path,name=tcp://host:port`` entries."""
    probes = []
    seen = set()
    for entry in (e.strip() for e in spec.split(",")):
        if not entry:
            continue
        name, sep, url = entry.partition("=")
        name, url = name.strip(), url.strip()
        if not sep or not name or not url:
            raise ConfigError(f"probe entry {entry!r} is not name=url")
        if not name.replace("_", "").replace("-", "").isalnum():
            raise ConfigError(f"probe name {name!r} may only use letters, digits, _ and -")
        if name in seen:
            raise ConfigError(f"probe name {name!r} is used twice")
        seen.add(name)
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme in ("http", "https"):
            if not parsed.hostname:
                raise ConfigError(f"probe {name!r}: {url!r} has no host")
            try:
                parsed.port  # noqa: B018 — raises on an out-of-range port
            except ValueError as exc:
                raise ConfigError(f"probe {name!r}: bad port in {url!r}") from exc
            probes.append(Probe(name=name, kind="http", url=url))
        elif parsed.scheme == "tcp":
            try:
                port = parsed.port
            except ValueError as exc:
                raise ConfigError(f"probe {name!r}: bad port in {url!r}") from exc
            if not parsed.hostname or not port:
                raise ConfigError(f"probe {name!r}: tcp probes need tcp://host:port")
            probes.append(Probe(name=name, kind="tcp", host=parsed.hostname, port=port))
        else:
            raise ConfigError(f"probe {name!r}: scheme must be http, https or tcp")
    return tuple(probes)


def default_probes(target: str) -> str:
    return (
        f"orchestrator=http://{target}:8888/health,"
        f"home_assistant=http://{target}:8123/manifest.json,"
        f"jupiter_ssh=tcp://{target}:22"
    )


def load_config(env: dict[str, str], *, need_token: bool, need_pushover: bool) -> Config:
    token = env.get("DEADMAN_WEBHOOK_TOKEN", "").strip()
    if need_token and len(token) < MIN_TOKEN_LEN:
        raise ConfigError(
            f"DEADMAN_WEBHOOK_TOKEN must be set and at least {MIN_TOKEN_LEN} characters "
            "(the same value as DEADMAN_WEBHOOK_TOKEN in Jupiter's .env)"
        )

    dry_run = _env_bool(env, "DEADMAN_DRY_RUN")
    user = env.get("PUSHOVER_USER_KEY", "").strip()
    app_token = env.get("PUSHOVER_APP_TOKEN", "").strip()
    if need_pushover and not dry_run and not (user and app_token):
        raise ConfigError("PUSHOVER_USER_KEY and PUSHOVER_APP_TOKEN must be set (or DEADMAN_DRY_RUN=true)")

    target = env.get("DEADMAN_TARGET_HOST", "").strip() or "10.0.0.248"
    probes = parse_probes(env.get("DEADMAN_PROBES", "").strip() or default_probes(target))

    # Who may connect at all (checked at accept time; loopback always may).
    # Defaults to Jupiter itself: Alertmanager's heartbeats and Prometheus's
    # scrapes both arrive from its LAN address (Docker masquerades them).
    allow_raw = env.get("DEADMAN_ALLOW_FROM", "").strip() or target
    allow_from = set()
    for raw in allow_raw.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            ipaddress.ip_address(raw)
        except ValueError as exc:
            raise ConfigError(
                f"DEADMAN_ALLOW_FROM entry {raw!r} is not an IP address "
                "(set DEADMAN_ALLOW_FROM explicitly when DEADMAN_TARGET_HOST is a hostname)"
            ) from exc
        allow_from.add(_normalize_ip(raw))

    # systemd's StateDirectory= sets STATE_DIRECTORY (colon-separated list).
    state_dir = env.get("DEADMAN_STATE_DIR", "").strip() or env.get("STATE_DIRECTORY", "").split(":")[0].strip()

    return Config(
        listen_addr=env.get("DEADMAN_LISTEN_ADDR", "").strip() or "0.0.0.0",
        listen_port=int(_env_number(env, "DEADMAN_LISTEN_PORT", 9466, 1, 65535)),
        token=token,
        allow_from=frozenset(allow_from),
        # Alertmanager re-sends the Watchdog about every 2 min; 10 min rides out
        # a monitoring-stack restart or a CI redeploy without paging.
        max_age=_env_number(env, "DEADMAN_HEARTBEAT_MAX_AGE_SECONDS", 600, 300, 86400),
        # DeadmanSwitchBroken treats a checker silent for 300 s as stuck.
        check_interval=_env_number(env, "DEADMAN_CHECK_INTERVAL_SECONDS", 60, 10, 120),
        probe_timeout=_env_number(env, "DEADMAN_PROBE_TIMEOUT_SECONDS", 5, 1, 30),
        repeat_seconds=_env_number(env, "DEADMAN_REPEAT_HOURS", 4, 0.25, 168) * 3600,
        probes=probes,
        pushover_user=user,
        pushover_token=app_token,
        dry_run=dry_run,
        state_dir=state_dir,
    )


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeResult:
    name: str
    kind: str
    ok: bool
    detail: str


class _NotHTTP(Exception):
    pass


_STATUS_LINE = re.compile(rb"HTTP/\d(?:\.\d)? (\d{3})[ \r]")


def _error_detail(exc: BaseException) -> str:
    # urllib's URLError wraps the real error in .reason; ssl.SSLError's
    # .reason is a short string (or None), so only unwrap exceptions.
    reason = getattr(exc, "reason", None)
    if not isinstance(reason, BaseException):
        reason = exc
    if isinstance(reason, _NotHTTP):
        return "not HTTP"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(reason, ConnectionRefusedError):
        return "refused"
    if isinstance(reason, OSError) and reason.errno is not None:
        return f"{type(reason).__name__} errno {reason.errno}"
    return type(reason).__name__


def _http_status(url: str, timeout: float) -> int:
    """GET ``url`` and return its status code, reading only the status line.

    Raw socket on purpose: one deadline covers the whole exchange (urllib's
    timeout is per read, so a server trickling bytes could hold a probe, and
    with it the page, indefinitely), nothing but the status line is parsed,
    and there are no redirects or proxies. https handshakes are bounded per
    read only.
    """
    deadline = time.monotonic() + timeout

    def remaining() -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("probe deadline")
        return left

    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    host_header = parts.netloc.rsplit("@", 1)[-1]
    request = (
        f"GET {target} HTTP/1.0\r\nHost: {host_header}\r\nUser-Agent: jupiter-deadman\r\nConnection: close\r\n\r\n"
    ).encode()
    sock = socket.create_connection((host, port), timeout=remaining())
    try:
        if parts.scheme == "https":
            sock.settimeout(remaining())
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        sock.settimeout(remaining())
        sock.sendall(request)
        buf = b""
        while b"\r\n" not in buf and len(buf) < 512:
            sock.settimeout(remaining())
            chunk = sock.recv(512 - len(buf))
            if not chunk:
                break
            buf += chunk
    finally:
        sock.close()
    match = _STATUS_LINE.match(buf)
    if not match:
        raise _NotHTTP()
    return int(match.group(1))


def run_probe(probe: Probe, timeout: float) -> ProbeResult:
    """Never raises: anything that goes wrong is a DOWN result. Probes only
    feed the page's text, so they must never stop a page from going out."""
    try:
        if probe.kind == "tcp":
            with socket.create_connection((probe.host, probe.port), timeout=timeout):
                pass
            return ProbeResult(probe.name, probe.kind, True, "port open")
        status = _http_status(probe.url, timeout)
    except Exception as exc:
        return ProbeResult(probe.name, probe.kind, False, _error_detail(exc))
    # Any answer below 500 means the service is running (a 401/404 still
    # proves it is listening); 5xx means it answered but is broken.
    return ProbeResult(probe.name, probe.kind, status < 500, f"http {status}")


def run_probes(probes: tuple[Probe, ...], timeout: float) -> list[ProbeResult]:
    return [run_probe(p, timeout) for p in probes]


# --------------------------------------------------------------------------
# Paging decisions (pure — unit-tested)
# --------------------------------------------------------------------------


@dataclass
class State:
    started_mono: float
    last_heartbeat_mono: float | None = None
    last_heartbeat_wall: float = 0.0
    alerting: bool = False
    alert_since_mono: float = 0.0
    last_page_mono: float = 0.0
    last_check_wall: float = 0.0
    # No first page before this (start-up grace after a restart with saved state).
    not_before_mono: float = 0.0
    probe_ok: dict[str, bool] = field(default_factory=dict)


def heartbeat_age(state: State, now_mono: float) -> float:
    ref = state.last_heartbeat_mono if state.last_heartbeat_mono is not None else state.started_mono
    return max(0.0, now_mono - ref)


def decide(state: State, now_mono: float, max_age: float, repeat_seconds: float) -> str | None:
    """Return "page", "repeat", "resolve" or None for this tick."""
    stale = heartbeat_age(state, now_mono) > max_age
    if state.alerting:
        # Only a real heartbeat resolves (an alert restored after a restart
        # with no heartbeat since must not "resolve" on the start-up clock).
        if not stale and state.last_heartbeat_mono is not None:
            return "resolve"
        if stale and now_mono - state.last_page_mono >= repeat_seconds:
            return "repeat"
        return None
    if stale and now_mono >= state.not_before_mono:
        return "page"
    return None


def outage_start(state: State) -> float:
    """The outage began at the last heartbeat (or at start-up), not at the page."""
    return state.last_heartbeat_mono if state.last_heartbeat_mono is not None else state.started_mono


def apply_sent(state: State, action: str, now_mono: float, since_mono: float | None = None) -> None:
    """Record a notification that was delivered. An undelivered one leaves the
    state alone, so the next tick tries again. ``since_mono`` is the outage
    start as seen when the page was decided (a heartbeat may land while the
    page is being sent)."""
    if action == "page":
        state.alerting = True
        state.alert_since_mono = outage_start(state) if since_mono is None else since_mono
        state.last_page_mono = now_mono
    elif action == "repeat":
        state.last_page_mono = now_mono
    elif action == "resolve":
        state.alerting = False


def diagnose(results: list[ProbeResult]) -> str:
    if not results:
        return "No probes configured, so there is no detail on what is down."
    http = [r for r in results if r.kind == "http"]
    tcp = [r for r in results if r.kind == "tcp"]
    if not any(r.ok for r in results):
        return "Saturn reaches nothing on Jupiter: the box is frozen, powered off, or off the network."
    if http and all(r.ok for r in http):
        return (
            "Jupiter's services answer, so the host and Docker are up: the monitoring stack "
            "(Prometheus/Alertmanager) is down or not ingesting, Jupiter's own Pushover delivery is "
            "failing, or the heartbeat route or token is broken."
        )
    if tcp and all(r.ok for r in tcp) and http and not any(r.ok for r in http):
        return "Jupiter's host answers but its services don't: Docker or the containers are likely down."
    return "Part of Jupiter answers; see the probe list."


def _minutes(seconds: float) -> str:
    return f"{max(1, round(seconds / 60))} min"


def probe_summary(results: list[ProbeResult]) -> str:
    return "; ".join(f"{r.name}: {'up' if r.ok else 'DOWN'} ({r.detail})" for r in results)


def build_message(action: str, state: State, now_mono: float, results: list[ProbeResult]) -> tuple[str, str, int]:
    """Return (title, message, Pushover priority) for a decided action."""
    age = heartbeat_age(state, now_mono)
    probes = probe_summary(results) or "no probes configured"
    runbook = "Runbook: monitoring/README.md -> Dead-man's switch."
    if action == "resolve":
        down_for = now_mono - state.alert_since_mono
        title = "Jupiter dead-man: heartbeat is back"
        message = f"Jupiter's monitoring heartbeat is back after about {_minutes(down_for)} of silence. From Saturn: {probes}."
        return title[:TITLE_MAX], message[:MESSAGE_MAX], 0
    if state.last_heartbeat_mono is None:
        lead = (
            f"No heartbeat from Jupiter's Alertmanager since this service started {_minutes(age)} ago, "
            "so no Jupiter alert can reach you."
        )
    else:
        lead = f"Jupiter's Prometheus/Alertmanager heartbeat stopped {_minutes(age)} ago, so no Jupiter alert can reach you."
    still = " (still down)" if action == "repeat" else ""
    title = f"Jupiter dead-man: no heartbeat for {_minutes(age)}{still}"
    message = f"{lead} {diagnose(results)} From Saturn: {probes}. {runbook}"
    return title[:TITLE_MAX], message[:MESSAGE_MAX], 1


# --------------------------------------------------------------------------
# Pushover
# --------------------------------------------------------------------------


def send_pushover(cfg: Config, title: str, message: str, priority: int) -> bool:
    if cfg.dry_run:
        log.warning("DRY RUN, not sent: priority %d %r: %s", priority, title, message)
        return True
    body = urllib.parse.urlencode(
        {
            "token": cfg.pushover_token,
            "user": cfg.pushover_user,
            "title": title,
            "message": message,
            "priority": str(priority),
        }
    ).encode()
    request = urllib.request.Request(PUSHOVER_URL, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            ok = resp.status == 200 and json.loads(resp.read(4096) or b"{}").get("status") == 1
            status = resp.status
    except urllib.error.HTTPError as exc:
        # Never log the response body: keep credentials and payload out of
        # the journal. The status code is enough to tell a bad key (4xx)
        # from an outage (5xx).
        log.error("Pushover rejected the notification: http %d", exc.code)
        exc.close()
        return False
    except Exception as exc:  # URLError, OSError, http.client.HTTPException, bad JSON ...
        log.error("Pushover unreachable: %s", _error_detail(exc))
        return False
    if not ok:
        log.error("Pushover did not accept the notification: http %d", status)
    return ok


# --------------------------------------------------------------------------
# The running service
# --------------------------------------------------------------------------


def load_saved_state(path: str | None) -> dict:
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable state file (%s)", type(exc).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def _saved_time(data: dict, key: str) -> float:
    value = data.get(key)
    return float(value) if isinstance(value, (int, float)) and value > 0 else 0.0


class App:
    def __init__(self, cfg: Config, clock=time.monotonic, wall_clock=time.time):
        self.cfg = cfg
        self.clock = clock
        self.wall_clock = wall_clock
        self.lock = threading.Lock()
        self._save_lock = threading.Lock()
        self.state = State(started_mono=clock())
        self.heartbeats: dict[str, int] = {
            k: 0 for k in ("ok", "forbidden", "unauthorized", "bad_request", "too_large", "not_watchdog")
        }
        self.notifications: dict[tuple[str, str], int] = {}
        self._last_log: dict[str, float] = {}
        self.state_path = os.path.join(cfg.state_dir, STATE_FILE_NAME) if cfg.state_dir else None
        self._restore(load_saved_state(self.state_path))

    # -- saved state ---------------------------------------------------------
    # Everything lives in memory, but the last heartbeat and the alert state
    # are also saved, so a restart (crash, reboot, upgrade) during an outage
    # neither resets the 10-minute clock nor pages twice.

    def _to_mono(self, wall: float, now_mono: float, now_wall: float) -> float:
        return now_mono - max(0.0, now_wall - wall)

    def _to_wall(self, mono: float, now_mono: float, now_wall: float) -> float:
        return now_wall - (now_mono - mono)

    def _restore(self, saved: dict) -> None:
        now_mono, now_wall = self.state.started_mono, self.wall_clock()
        s = self.state
        heartbeat = _saved_time(saved, "last_heartbeat_wall")
        if heartbeat and heartbeat <= now_wall + 60:
            s.last_heartbeat_wall = heartbeat
            s.last_heartbeat_mono = self._to_mono(heartbeat, now_mono, now_wall)
            s.not_before_mono = now_mono + min(STARTUP_GRACE_SECONDS, self.cfg.max_age)
        if saved.get("alerting") is True:
            s.alerting = True
            s.alert_since_mono = self._to_mono(_saved_time(saved, "alert_since_wall") or now_wall, now_mono, now_wall)
            s.last_page_mono = self._to_mono(_saved_time(saved, "last_page_wall") or now_wall, now_mono, now_wall)
        if heartbeat or s.alerting:
            log.info(
                "restored state: last heartbeat %s, alerting %s",
                f"{now_wall - heartbeat:.0f} s ago" if heartbeat else "never",
                "yes" if s.alerting else "no",
            )

    def _save(self) -> None:
        if not self.state_path:
            return
        with self.lock:
            now_mono, now_wall = self.clock(), self.wall_clock()
            s = self.state
            data = {
                "last_heartbeat_wall": s.last_heartbeat_wall,
                "alerting": s.alerting,
                "alert_since_wall": self._to_wall(s.alert_since_mono, now_mono, now_wall) if s.alerting else 0,
                "last_page_wall": self._to_wall(s.last_page_mono, now_mono, now_wall) if s.alerting else 0,
            }
        tmp = self.state_path + ".tmp"
        try:
            with self._save_lock:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.state_path)
        except OSError as exc:
            if self.rate_limited("save"):
                log.warning("could not save state (%s); a restart would reset the heartbeat clock", type(exc).__name__)

    # -- heartbeats ----------------------------------------------------------

    def rate_limited(self, key: str) -> bool:
        """True at most once per LOG_INTERVAL_SECONDS per key."""
        now_mono = self.clock()
        with self.lock:
            if now_mono - self._last_log.get(key, -LOG_INTERVAL_SECONDS) < LOG_INTERVAL_SECONDS:
                return False
            self._last_log[key] = now_mono
            return True

    def record_heartbeat(self) -> None:
        now_mono = self.clock()
        with self.lock:
            gap = None if self.state.last_heartbeat_mono is None else now_mono - self.state.last_heartbeat_mono
            self.state.last_heartbeat_mono = now_mono
            self.state.last_heartbeat_wall = self.wall_clock()
            self.heartbeats["ok"] += 1
        self._save()
        if gap is None:
            log.info("first heartbeat received from Alertmanager")
        elif gap > self.cfg.max_age:
            log.info("heartbeat resumed after a %.0f s gap", gap)

    def count_rejection(self, kind: str, client_ip: str) -> None:
        with self.lock:
            self.heartbeats[kind] += 1
        if self.rate_limited(f"reject:{kind}"):
            log.warning("rejected heartbeat (%s) from %s (logged at most once a minute per reason)", kind, client_ip)

    # -- checker -------------------------------------------------------------

    def tick(self) -> None:
        # Probes only add detail to the page; nothing they do may stop it.
        try:
            results = run_probes(self.cfg.probes, self.cfg.probe_timeout)
        except Exception:
            log.exception("probes failed")
            results = [ProbeResult(p.name, p.kind, False, "probe error") for p in self.cfg.probes]
        now_mono = self.clock()
        with self.lock:
            for r in results:
                before = self.state.probe_ok.get(r.name)
                if before is not None and before != r.ok:
                    log.info(
                        "probe %s: %s -> %s (%s)",
                        r.name,
                        "up" if before else "DOWN",
                        "up" if r.ok else "DOWN",
                        r.detail,
                    )
                self.state.probe_ok[r.name] = r.ok
            self.state.last_check_wall = self.wall_clock()
            action = decide(self.state, now_mono, self.cfg.max_age, self.cfg.repeat_seconds)
            if action is None:
                return
            since_mono = outage_start(self.state)
            title, message, priority = build_message(action, self.state, now_mono, results)
        sent = send_pushover(self.cfg, title, message, priority)
        result = "dry_run" if (sent and self.cfg.dry_run) else ("ok" if sent else "error")
        with self.lock:
            key = (action, result)
            self.notifications[key] = self.notifications.get(key, 0) + 1
            if sent:
                apply_sent(self.state, action, now_mono, since_mono)
        if sent:
            self._save()
        if sent and not self.cfg.dry_run:  # dry run already logged it
            log.warning("%s notification sent: %s", action, title)

    # -- metrics -------------------------------------------------------------

    def render_metrics(self) -> str:
        with self.lock:
            s = self.state
            lines = [
                "# HELP jupiter_deadman_up 1 while the dead-man's switch process is serving.",
                "# TYPE jupiter_deadman_up gauge",
                "jupiter_deadman_up 1",
                "# HELP jupiter_deadman_dry_run 1 when DEADMAN_DRY_RUN is on (pages are logged, not sent).",
                "# TYPE jupiter_deadman_dry_run gauge",
                f"jupiter_deadman_dry_run {int(self.cfg.dry_run)}",
                "# HELP jupiter_deadman_alerting 1 while the switch considers Jupiter's monitoring silent.",
                "# TYPE jupiter_deadman_alerting gauge",
                f"jupiter_deadman_alerting {int(s.alerting)}",
                "# HELP jupiter_deadman_last_heartbeat_timestamp_seconds Wall time of the last accepted heartbeat (0 = none since start).",
                "# TYPE jupiter_deadman_last_heartbeat_timestamp_seconds gauge",
                f"jupiter_deadman_last_heartbeat_timestamp_seconds {s.last_heartbeat_wall:.3f}",
                "# HELP jupiter_deadman_last_check_timestamp_seconds Wall time the checker last ran.",
                "# TYPE jupiter_deadman_last_check_timestamp_seconds gauge",
                f"jupiter_deadman_last_check_timestamp_seconds {s.last_check_wall:.3f}",
                "# HELP jupiter_deadman_heartbeat_max_age_seconds Heartbeat silence that triggers a page.",
                "# TYPE jupiter_deadman_heartbeat_max_age_seconds gauge",
                f"jupiter_deadman_heartbeat_max_age_seconds {self.cfg.max_age:g}",
                "# HELP jupiter_deadman_heartbeats_total Heartbeat POSTs by outcome.",
                "# TYPE jupiter_deadman_heartbeats_total counter",
            ]
            lines += [
                f'jupiter_deadman_heartbeats_total{{result="{k}"}} {v}' for k, v in sorted(self.heartbeats.items())
            ]
            lines += [
                "# HELP jupiter_deadman_probe_up Last result of each probe of Jupiter (1 = answered).",
                "# TYPE jupiter_deadman_probe_up gauge",
            ]
            lines += [f'jupiter_deadman_probe_up{{probe="{k}"}} {int(v)}' for k, v in sorted(s.probe_ok.items())]
            lines += [
                "# HELP jupiter_deadman_notifications_total Pushover notifications by kind and result.",
                "# TYPE jupiter_deadman_notifications_total counter",
            ]
            lines += [
                f'jupiter_deadman_notifications_total{{kind="{k}",result="{r}"}} {v}'
                for (k, r), v in sorted(self.notifications.items())
            ]
        return "\n".join(lines) + "\n"


def is_watchdog_heartbeat(payload: object) -> bool:
    """True for an Alertmanager webhook body carrying a firing Watchdog."""
    if not isinstance(payload, dict) or payload.get("status") != "firing":
        return False
    alerts = payload.get("alerts")
    if not isinstance(alerts, list):
        return False
    for alert in alerts:
        if not isinstance(alert, dict) or alert.get("status") != "firing":
            continue
        labels = alert.get("labels")
        if isinstance(labels, dict) and labels.get("alertname") == "Watchdog":
            return True
    return False


def _normalize_ip(raw: str) -> str:
    try:
        ip = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return raw
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ip)


def _is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


# Indirection so a test can prove an unauthenticated body is never parsed.
_parse_json = json.loads


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "jupiter-deadman"
    sys_version = ""
    timeout = SOCKET_TIMEOUT_SECONDS
    server: DeadmanHTTPServer

    def log_message(self, format: str, *args: object) -> None:
        pass  # no access log: one line a minute per heartbeat is noise; rejections are logged by App

    def _reply(self, code: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _path(self) -> str:
        return urllib.parse.urlsplit(self.path).path

    def do_GET(self) -> None:
        path = self._path()
        if path == "/metrics":
            self._reply(200, self.server.app.render_metrics(), "text/plain; version=0.0.4; charset=utf-8")
        elif path == "/healthz":
            self._reply(200, "ok\n")
        else:
            self._reply(404, "not found\n")

    def do_POST(self) -> None:
        app = self.server.app
        if self._path() != "/heartbeat":
            self._reply(404, "not found\n")
            return
        # The source-IP allow-list was already applied at accept time
        # (DeadmanHTTPServer.verify_request).
        client_ip = _normalize_ip(self.client_address[0])
        expected = f"Bearer {app.cfg.token}".encode()
        given = self.headers.get("Authorization", "").encode("latin-1", "replace")
        if not hmac.compare_digest(given, expected):
            app.count_rejection("unauthorized", client_ip)
            self._reply(401, "unauthorized\n")
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if length < 0:
            app.count_rejection("bad_request", client_ip)
            self._reply(411, "length required\n")
            return
        if length > MAX_BODY_BYTES:
            app.count_rejection("too_large", client_ip)
            self._reply(413, "too large\n")
            return
        try:
            payload = _parse_json(self.rfile.read(length) or b"null")
        except (ValueError, OSError, RecursionError):
            app.count_rejection("bad_request", client_ip)
            self._reply(400, "bad json\n")
            return
        if not is_watchdog_heartbeat(payload):
            app.count_rejection("not_watchdog", client_ip)
            self._reply(422, "not a firing Watchdog alert\n")
            return
        app.record_heartbeat()
        self._reply(200, "ok\n")


def _abort_connection(sock: socket.socket) -> None:
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)


class DeadmanHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: App):
        self.app = app
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, Handler)

    def verify_request(self, request, client_address) -> bool:
        # Runs right after accept(), before a thread, a slot or a single byte
        # of parsing: anyone not on the allow-list is dropped here, so the
        # rest of the LAN can't tie up the receiver or read /metrics.
        ip = _normalize_ip(client_address[0])
        if ip in self.app.cfg.allow_from or _is_loopback(ip):
            return True
        self.app.count_rejection("forbidden", ip)
        return False

    def process_request(self, request, client_address):
        # Cap concurrent connections: each one is a thread.
        if not self._slots.acquire(blocking=False):
            if self.app.rate_limited("busy"):
                log.warning("all %d connection slots busy; dropped a connection", MAX_CONNECTIONS)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        # The handler's socket timeout is per read; this bounds the whole
        # connection so a peer trickling bytes can't hold a slot.
        timer = threading.Timer(CONNECTION_DEADLINE_SECONDS, _abort_connection, args=(request,))
        timer.daemon = True
        timer.start()
        try:
            super().process_request_thread(request, client_address)
        finally:
            timer.cancel()
            self._slots.release()

    def handle_error(self, request, client_address) -> None:
        # Default prints a traceback per failed connection; one line a minute
        # is enough (dropped or aborted connections are expected).
        if self.app.rate_limited("conn_error"):
            log.warning(
                "connection from %s failed: %s (logged at most once a minute)",
                _normalize_ip(client_address[0]),
                type(sys.exc_info()[1]).__name__,
            )


def serve(cfg: Config) -> int:
    app = App(cfg)
    server = DeadmanHTTPServer((cfg.listen_addr, cfg.listen_port), app)
    stop = threading.Event()
    shutting_down = threading.Event()
    server_died = threading.Event()

    def _serve() -> None:
        try:
            server.serve_forever()
        except Exception:
            log.exception("HTTP server stopped unexpectedly")
        # serve_forever only returns on our own shutdown(); anything else
        # means the receiver died, and systemd should restart the unit.
        if not shutting_down.is_set():
            server_died.set()
            stop.set()

    def _on_signal(signum: int, _frame: object) -> None:
        log.info("signal %d received, stopping", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    threading.Thread(target=_serve, name="http", daemon=True).start()

    log.info(
        "listening on %s:%d (accepting %s + loopback); page after %.0f s without a heartbeat; probes: %s; state: %s%s",
        cfg.listen_addr,
        cfg.listen_port,
        ", ".join(sorted(cfg.allow_from)),
        cfg.max_age,
        ", ".join(p.name for p in cfg.probes) or "none",
        app.state_path or "not saved",
        "; DRY RUN (nothing is sent)" if cfg.dry_run else "",
    )

    while not stop.is_set():
        try:
            app.tick()
        except Exception:
            log.exception("check failed")
        stop.wait(cfg.check_interval)

    shutting_down.set()
    server.shutdown()
    server.server_close()
    return 1 if server_died.is_set() else 0


def cmd_probe(cfg: Config) -> int:
    results = run_probes(cfg.probes, cfg.probe_timeout)
    for r in results:
        print(f"{r.name:16} {'up' if r.ok else 'DOWN':5} {r.detail}")
    print(diagnose(results) if not all(r.ok for r in results) else "All probes answered.")
    return 0 if all(r.ok for r in results) else 1


def cmd_test_notify(cfg: Config) -> int:
    # Sends for real even with DEADMAN_DRY_RUN on: checking the Pushover keys
    # before arming the switch is the point of this command.
    if not (cfg.pushover_user and cfg.pushover_token):
        log.error("config error: PUSHOVER_USER_KEY and PUSHOVER_APP_TOKEN must be set")
        return 2
    ok = send_pushover(
        replace(cfg, dry_run=False),
        "Jupiter dead-man: test notice",
        "Test from the dead-man's switch on Saturn. If you can read this, Saturn can page you.",
        0,
    )
    print("sent" if ok else "FAILED (see the log line above)")
    return 0 if ok else 1


class _JournalFormatter(logging.Formatter):
    """Prefix each line with its syslog priority (<3> error, <4> warning,
    <6> info) so `journalctl -p warning -u jupiter-deadman` filters properly;
    journald strips the prefix."""

    _PRIORITY = {logging.ERROR: 3, logging.WARNING: 4, logging.INFO: 6, logging.DEBUG: 7}

    def format(self, record: logging.LogRecord) -> str:
        priority = self._PRIORITY.get(record.levelno, 3 if record.levelno > logging.ERROR else 6)
        return f"<{priority}>{record.levelname} {super().format(record)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Off-box dead-man's switch for Jupiter (runs on Saturn).")
    parser.add_argument("command", nargs="?", default="serve", choices=("serve", "probe", "test-notify"))
    args = parser.parse_args(argv)
    handler = logging.StreamHandler(sys.stderr)
    # systemd sets JOURNAL_STREAM when stderr goes to the journal; by hand,
    # print plain lines.
    handler.setFormatter(
        _JournalFormatter() if os.environ.get("JOURNAL_STREAM") else logging.Formatter("%(levelname)s %(message)s")
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    try:
        cfg = load_config(
            dict(os.environ),
            need_token=args.command == "serve",
            need_pushover=args.command in ("serve", "test-notify"),
        )
    except ConfigError as exc:
        log.error("config error: %s", exc)
        return 2
    if args.command == "probe":
        return cmd_probe(cfg)
    if args.command == "test-notify":
        return cmd_test_notify(cfg)
    return serve(cfg)


if __name__ == "__main__":
    sys.exit(main())
