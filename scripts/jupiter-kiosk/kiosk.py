#!/usr/bin/env python3
"""Closet monitors kiosk on Jupiter: one fullscreen Chrome per monitor.

Runs as jupiter-kiosk.service, the unprivileged `kiosk` user, against the X server of
jupiter-kiosk-x.service (start-x.sh -> Xorg) on the RTX 5080 that has the two HDMI ports (PCI bus
0c). The voice (voxcpm-tts) runs on the other 5080. /etc/X11/jupiter-kiosk.conf pins the X server to
the display card, both units' InaccessiblePaths= hide the voice card's device files, and
`--preflight` (ExecStartPre of both units) refuses to start if the display card is the one
/etc/voxcpm-tts.env pins, or if the masks hide the wrong card.

X and this process are in different network namespaces. The X unit has its own (PrivateNetwork=), so
the abstract sockets the NVIDIA X driver always binds can't be reached from the host's; this unit
stays in the host's, because Chrome loads the pages over Jupiter's Tailscale address. They share
only the units' private /tmp (JoinsNamespaceOf=), where the X socket and the driver's filesystem
sideband socket live. `--preflight --x-server` refuses to start X outside a private network
namespace (systemd skips PrivateNetwork= without a word if this unit's namespace was set up first).

What it does:
  0. Holds the X display's abstract socket name (@/tmp/.X11-unix/X0) in the host network namespace,
     bound but not listening. X clients (xrandr, Chrome) try that name before the /tmp socket, so
     whatever else in the host namespace bound it first would get them: the screens' content and
     the keyboard of a browser holding the dashboard session. Held, a connect is refused and the
     client falls back to /tmp. Already taken: refuse to start. Then waits for X to answer, and
     checks that no X socket is visible from this network namespace.
  1. Waits until a monitor is connected (no restart loop while the monitors are off). Then xrandr
     turns each configured output to its rotation (portrait = left/right), lines the screens up left
     to right, switches unconfigured outputs off, and turns screen blanking off.
  2. Starts one Chrome per screen (own profile, --kiosk at that screen's position) and drives it over
     --remote-debugging-pipe: Chrome DevTools Protocol on fds 3/4, so only this process can talk to
     it. No debugging port is opened.
  3. Every CHECK_SECONDS it reads the page address and HTTP status:
     - a login page: log in. Grafana through a view-only account (POST /login from the page itself);
       the Jess dashboard by setting its read-only kiosk session cookie (it opens /dashboard and the
       GETs that page makes, nothing else: frontend/src/lib/kiosk-session.ts);
     - a Chrome error page, or HTTP 5xx (server down or still starting, e.g. Tailscale Serve's 502):
       load the page again every ERROR_RETRY_SECONDS;
     - every reload_minutes: reload.
     A dead, hung or crash-looping Chrome is restarted with backoff.
  4. Every OUTPUT_CHECK_SECONDS it compares the connected monitors with the ones it laid out, and
     exits on a change so systemd restarts it with a fresh layout.

Files:
  /etc/jupiter-kiosk/kiosk.json    screens (name, output, rotate, url, login, reload_minutes,
                                   hide_nav), the chrome path and host_map. No secrets. See
                                   kiosk.example.json.
  /etc/jupiter-kiosk/secrets.json  root:kiosk 0640, written by make_logins.py: grafana_user,
                                   grafana_password, dashboard_kiosk_session.

Logs (journal, SyslogIdentifier=jupiter-kiosk): screen names, states, HTTP status codes and error
kinds only. Never page text, cookies, passwords or URLs. A page that keeps failing logs once when it
goes down and once when it comes back.

SIGUSR1 saves a PNG of each screen to $XDG_RUNTIME_DIR/jupiter-kiosk/ (mode 600), so a remote check
can see what the screens show. That is whatever is on screen, personal data included.

Standard library only; Python 3.11+.
"""

from __future__ import annotations

import argparse
import base64
import errno
import fcntl
import ipaddress
import json
import logging
import os
import queue
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from urllib.parse import urlsplit

CONFIG_PATH = "/etc/jupiter-kiosk/kiosk.json"
SECRETS_PATH = "/etc/jupiter-kiosk/secrets.json"
XORG_CONF = "/etc/X11/jupiter-kiosk.conf"
VOICE_ENV = "/etc/voxcpm-tts.env"
NVIDIA_PROC = "/proc/driver/nvidia/gpus"
PCI_SYSFS = "/sys/bus/pci/devices"
NET_DEV = "/proc/self/net/dev"  # the network interfaces of this process's network namespace
NET_UNIX = "/proc/self/net/unix"  # every AF_UNIX socket in this network namespace, abstract ones included
XAUTHORITY_NAME = "jupiter-kiosk.Xauthority"  # in $XDG_RUNTIME_DIR, written by start-x.sh
X_SOCKET_DIR = "/tmp/.X11-unix"
DISPLAY_RE = re.compile(r":(\d{1,3})(?:\.\d{1,3})?")  # fullmatch: "$" would let a trailing newline through

CHECK_SECONDS = 20  # how often each screen's page is looked at
LOGIN_RETRY_SECONDS = 300  # at most one login attempt per screen per 5 min (wrong password = no hammering)
ERROR_RETRY_SECONDS = 30  # server down: try the page again this often
CDP_TIMEOUT = 20.0
HUNG_CHECKS = 3  # this many failed page reads in a row = restart Chrome
CRASH_LIMIT = 3  # this many page crashes within CRASH_WINDOW_SECONDS = restart Chrome (e.g. after an apt upgrade)
CRASH_WINDOW_SECONDS = 300
STABLE_RUN_SECONDS = 600  # a Chrome that lived this long resets the restart backoff
NO_MONITOR_SECONDS = 30  # no monitor connected: look again this often instead of exiting
OUTPUT_CHECK_SECONDS = 60  # monitor plugged in or out: exit so systemd restarts with a fresh layout
X_START_SECONDS = 60  # jupiter-kiosk-x.service has no readiness signal: wait this long for X to answer

ROTATIONS = ("normal", "left", "right", "inverted")
LOGINS = ("none", "grafana", "dashboard")
DASHBOARD_COOKIE = "brain_token"  # frontend/src/lib/dashboard-auth.ts COOKIE_NAME
DISPLAY_COOKIE = "__Host-jess_display"  # frontend/src/lib/display-mode.ts: "kiosk" hides the sidebar + tab bar
DEFAULT_PORTS = {"https": 443, "http": 80}
CONFIG_KEYS = frozenset({"chrome", "screens", "host_map", "timezone"})
TZ_RE = re.compile(r"^[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*$")
ZONEINFO = "/usr/share/zoneinfo"
SCREEN_KEYS = frozenset({"name", "url", "output", "rotate", "login", "reload_minutes", "hide_nav"})
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
OUTPUT_RE = re.compile(r"^[A-Za-z0-9-]{1,32}$")
HOST_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
UUID_RE = re.compile(r"^GPU-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

CHROME_FLAGS = (
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    "--noerrdialogs",
    "--disable-infobars",
    "--disable-session-crashed-bubble",
    "--disable-features=Translate,MediaRouter,OptimizationHints,DialMediaRouteProvider",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--disable-extensions",
    "--disable-default-apps",
    "--disable-breakpad",
    "--metrics-recording-only",
    "--deny-permission-prompts",
    "--mute-audio",
    "--log-level=3",
    "--disable-crash-reporter",
    "--disk-cache-size=67108864",  # the profile lives in tmpfs, which counts against the slice's memory
)
PAGE_STATE_JS = (
    "JSON.stringify([location.href, ((performance.getEntriesByType('navigation')[0]) || {}).responseStatus || 0])"
)

log = logging.getLogger("jupiter-kiosk")


class ConfigError(ValueError):
    """kiosk.json or secrets.json is unusable."""


class CdpError(RuntimeError):
    """The DevTools connection failed: Chrome died, hung, or refused a command."""


# ----------------------------------------------------------------------------------------- config


def origin_of(url: str) -> str | None:
    """scheme://host[:port], lowercased, default port dropped; None if not an http(s) URL with a host.

    Both the configured URL and Chrome's location.href go through this, so `HOST:443` and `host`
    compare equal.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in DEFAULT_PORTS or not parts.hostname:
        return None
    host = parts.hostname  # urlsplit lowercases it
    if ":" in host:
        host = f"[{host}]"
    if port is None or port == DEFAULT_PORTS[parts.scheme]:
        return f"{parts.scheme}://{host}"
    return f"{parts.scheme}://{host}:{port}"


@dataclass(frozen=True)
class Screen:
    name: str
    url: str
    output: str | None = None
    rotate: str = "normal"
    login: str = "none"
    reload_minutes: float = 0.0
    hide_nav: bool = False

    @property
    def origin(self) -> str:
        return origin_of(self.url) or ""


@dataclass(frozen=True)
class Config:
    chrome: str
    screens: tuple[Screen, ...]
    host_map: dict[str, str] = field(default_factory=dict)
    timezone: str | None = None  # IANA name for Chrome's TZ (Jupiter itself runs on UTC)


def _reject_unknown(raw: dict, allowed: frozenset[str], where: str) -> None:
    unknown = sorted(str(k) for k in raw if k not in allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)} (typo?)")


def load_config(data: object) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("kiosk.json must be an object")
    _reject_unknown(data, CONFIG_KEYS, "kiosk.json")
    chrome = data.get("chrome", "/opt/google/chrome/chrome")  # the binary, not the wrapper script
    if not isinstance(chrome, str) or not chrome.startswith("/"):
        raise ConfigError("chrome must be an absolute path")
    raw_screens = data.get("screens")
    if not isinstance(raw_screens, list) or not 1 <= len(raw_screens) <= 4:
        raise ConfigError("screens must be a list of 1-4 screens")
    screens = []
    for i, raw in enumerate(raw_screens):
        if not isinstance(raw, dict):
            raise ConfigError(f"screen {i} must be an object")
        screens.append(_load_screen(i, raw))
    names = [s.name for s in screens]
    if len(set(names)) != len(names):
        raise ConfigError("screen names must be unique")
    outputs = [s.output for s in screens if s.output]
    if len(set(outputs)) != len(outputs):
        raise ConfigError("two screens name the same output")
    raw_map = data.get("host_map", {})
    if not isinstance(raw_map, dict):
        raise ConfigError("host_map must be an object")
    host_map = {}
    for host, ip in raw_map.items():
        if not isinstance(host, str) or not HOST_RE.fullmatch(host):
            raise ConfigError("host_map keys must be host names")
        if not isinstance(ip, str):
            raise ConfigError(f"host_map[{host}] must be an IP address string")
        try:
            host_map[host.lower()] = str(ipaddress.ip_address(ip))
        except ValueError:
            raise ConfigError(f"host_map[{host}] must be an IP address") from None
    timezone = data.get("timezone")
    if timezone is not None and (
        not isinstance(timezone, str)
        or not TZ_RE.fullmatch(timezone)
        or ".." in timezone
        or not os.path.isfile(os.path.join(ZONEINFO, timezone))
    ):
        raise ConfigError(f"timezone must be an IANA name under {ZONEINFO}, e.g. America/Chicago")
    return Config(chrome=chrome, screens=tuple(screens), host_map=host_map, timezone=timezone)


def _load_screen(i: int, raw: dict) -> Screen:
    name = raw.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ConfigError(f"screen {i}: name must match {NAME_RE.pattern}")
    _reject_unknown(raw, SCREEN_KEYS, f"screen {name}")
    url = raw.get("url")
    if not isinstance(url, str) or origin_of(url) is None:
        raise ConfigError(f"screen {name}: url must be an http(s) URL with a valid host and port")
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise ConfigError(f"screen {name}: no user:password@ in the url (kiosk.json holds no secrets)")
    output = raw.get("output")
    if output is not None and (not isinstance(output, str) or not OUTPUT_RE.fullmatch(output)):
        raise ConfigError(f"screen {name}: output must be an xrandr output name")
    rotate = raw.get("rotate", "normal")
    if rotate not in ROTATIONS:
        raise ConfigError(f"screen {name}: rotate must be one of {', '.join(ROTATIONS)}")
    login = raw.get("login", "none")
    if login not in LOGINS:
        raise ConfigError(f"screen {name}: login must be one of {', '.join(LOGINS)}")
    hide_nav = raw.get("hide_nav", False)
    if not isinstance(hide_nav, bool):
        raise ConfigError(f"screen {name}: hide_nav must be true or false")
    if (login != "none" or hide_nav) and parts.scheme != "https":
        raise ConfigError(f"screen {name}: logins and kiosk cookies only go over https")
    reload_minutes = raw.get("reload_minutes", 0)
    if (
        isinstance(reload_minutes, bool)
        or not isinstance(reload_minutes, int | float)
        or not 0 <= reload_minutes <= 1440
    ):
        raise ConfigError(f"screen {name}: reload_minutes must be 0-1440")
    return Screen(
        name=name,
        url=url,
        output=output,
        rotate=rotate,
        login=login,
        reload_minutes=float(reload_minutes),
        hide_nav=hide_nav,
    )


def load_secrets(data: object, cfg: Config) -> dict[str, str]:
    """Only the secrets the configured screens need; refuse to start without them."""
    if not isinstance(data, dict):
        raise ConfigError("secrets.json must be an object")
    logins = {s.login for s in cfg.screens}
    needed: list[str] = []
    if "grafana" in logins:
        needed += ["grafana_user", "grafana_password"]
    if "dashboard" in logins:
        needed.append("dashboard_kiosk_session")
    if "dashboard_session" in data:
        # The master cookie that older make_logins.py wrote: it opens the whole dashboard. Don't run
        # with it lying around; make_logins.py rewrites the file without it.
        raise ConfigError(
            "secrets.json still holds dashboard_session, the full dashboard login: re-run make_logins.py, "
            "which replaces it with the read-only kiosk session"
        )
    out = {}
    for key in needed:
        value = data.get(key)
        if not isinstance(value, str) or not value:
            raise ConfigError(f"secrets.json is missing {key} (run make_logins.py)")
        out[key] = value
    if "dashboard_kiosk_session" in out and not HEX64_RE.fullmatch(out["dashboard_kiosk_session"]):
        raise ConfigError("secrets.json dashboard_kiosk_session must be 64 hex characters (run make_logins.py)")
    return out


def read_json(path: str) -> object:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{path} not found") from None
    except PermissionError:
        raise ConfigError(f"{path} is not readable by this user") from None
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path} is not valid JSON (line {e.lineno})") from None
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"{path} can't be read ({type(e).__name__})") from None


# ---------------------------------------------------------------------------------------- display


@dataclass
class Output:
    name: str
    connected: bool
    geometry: tuple[int, int, int, int] | None = None  # width, height, x, y with rotation applied
    preferred: tuple[int, int] | None = None  # preferred mode, unrotated
    first_mode: tuple[int, int] | None = None


_OUTPUT_RE = re.compile(r"^(\S+) (connected|disconnected)(?: primary)?(?: (\d+)x(\d+)\+(-?\d+)\+(-?\d+))?")
_MODE_RE = re.compile(r"^\s+(\d+)x(\d+)i?\s+(.*)$")


def parse_xrandr(text: str) -> list[Output]:
    outputs: list[Output] = []
    current: Output | None = None
    for line in text.splitlines():
        m = _OUTPUT_RE.match(line)
        if m:
            geometry = None
            if m.group(3):
                geometry = (int(m.group(3)), int(m.group(4)), int(m.group(5)), int(m.group(6)))
            current = Output(name=m.group(1), connected=m.group(2) == "connected", geometry=geometry)
            outputs.append(current)
            continue
        if not line[:1].isspace():
            current = None  # "Screen 0: ...", "DP-1 unknown connection ...": its mode lines belong to nobody
            continue
        mode = _MODE_RE.match(line)
        if mode and current is not None:
            size = (int(mode.group(1)), int(mode.group(2)))
            if current.first_mode is None:
                current.first_mode = size
            if current.preferred is None and "+" in mode.group(3):
                current.preferred = size
    return outputs


def assign_outputs(
    screens: tuple[Screen, ...], outputs: list[Output]
) -> tuple[list[tuple[Screen, Output]], list[str], list[str]]:
    """Match screens to connected outputs: named outputs first, then the free ones in xrandr order.

    Returns (assigned pairs in screen order, connected outputs left unused, warnings).
    """
    connected = [o for o in outputs if o.connected]
    by_name = {o.name: o for o in connected}
    taken = {s.output for s in screens if s.output in by_name}
    spare = [o for o in connected if o.name not in taken]
    warnings: list[str] = []
    assigned: list[tuple[Screen, Output]] = []
    for screen in screens:
        if screen.output in by_name:
            assigned.append((screen, by_name[screen.output]))
            continue
        if screen.output:
            warnings.append(f"screen {screen.name}: output {screen.output} is not connected; using the next free one")
        if not spare:
            warnings.append(f"screen {screen.name}: no free connected output; screen skipped")
            continue
        assigned.append((screen, spare.pop(0)))
    used = {o.name for _, o in assigned}
    unused = [o.name for o in connected if o.name not in used]
    return assigned, unused, warnings


def rotated_size(size: tuple[int, int], rotate: str) -> tuple[int, int]:
    width, height = size
    return (height, width) if rotate in ("left", "right") else (width, height)


def xrandr_command(assigned: list[tuple[Screen, Output]], unused: list[str]) -> list[str]:
    cmd = ["xrandr"]
    x = 0
    for screen, out in assigned:
        width, _ = rotated_size(out.preferred or out.first_mode or (1920, 1080), screen.rotate)
        cmd += ["--output", out.name, "--auto", "--rotate", screen.rotate, "--pos", f"{x}x0"]
        x += width
    for name in unused:
        cmd += ["--output", name, "--off"]
    return cmd


def planned_geometry(assigned: list[tuple[Screen, Output]]) -> dict[str, tuple[int, int, int, int]]:
    """Where each screen should end up (width, height, x, y) if xrandr did what it was told."""
    geometry = {}
    x = 0
    for screen, out in assigned:
        width, height = rotated_size(out.preferred or out.first_mode or (1920, 1080), screen.rotate)
        geometry[screen.name] = (width, height, x, 0)
        x += width
    return geometry


def _run(cmd: list[str], timeout: float = 15) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout).stdout


def connected_outputs(probe: bool = True) -> frozenset[str]:
    """Names of connected outputs. probe=False reads the X server's current state without re-probing
    the monitors (used for the periodic check, so it can't make a screen flicker)."""
    text = _run(["xrandr", "--query" if probe else "--current"])
    return frozenset(o.name for o in parse_xrandr(text) if o.connected)


def wait_for_monitor(stop: threading.Event) -> frozenset[str] | None:
    """Block until at least one monitor is connected; None if asked to stop first."""
    warned = False
    while not stop.is_set():
        connected = connected_outputs()
        if connected:
            if warned:
                log.info("monitor connected; starting")
            return connected
        if not warned:
            log.warning("no monitor connected on the display card; looking again every %d s", NO_MONITOR_SECONDS)
            warned = True
        stop.wait(NO_MONITOR_SECONDS)
    return None


# ------------------------------------------------------------------- X server (jupiter-kiosk-x.service)


def display_number(display: str) -> int:
    """The number of a local display (":0", ":0.0"). Anything else, a TCP display included, is refused:
    the checks below only cover the local socket."""
    m = DISPLAY_RE.fullmatch(display)
    if not m:
        raise ConfigError("DISPLAY must be a local display like :0 (jupiter-kiosk.service sets it)")
    return int(m.group(1))


def hold_x_abstract_name(number: int) -> socket.socket:
    """Bind @/tmp/.X11-unix/X<number> in this network namespace and never listen (module docstring, step 0).

    A connect to it is refused, so xcb falls back to the /tmp socket. SOCK_STREAM because abstract names
    are per socket type and xcb connects with SOCK_STREAM. OSError (EADDRINUSE) if something holds it.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(f"\0{X_SOCKET_DIR}/X{number}")
    except OSError:
        sock.close()
        raise
    return sock


def x_failure_kind(error: BaseException) -> str:
    """Why an xrandr call failed, in our words (its output stays out of the log)."""
    if isinstance(error, subprocess.TimeoutExpired):
        return "xrandr hung"
    if isinstance(error, subprocess.CalledProcessError):
        text = error.stderr if isinstance(error.stderr, str) else ""
        if "uthoriz" in text or "No protocol specified" in text:
            return "X refused the auth cookie (XAUTHORITY isn't the file start-x.sh wrote)"
        return "no X server on the display"
    if isinstance(error, FileNotFoundError):
        return "xrandr not installed"
    return type(error).__name__


def wait_for_x(stop: threading.Event, timeout: float = X_START_SECONDS) -> bool:
    """True once the X server answers; False if it doesn't within timeout (logged), or if asked to stop."""
    deadline = time.monotonic() + timeout
    while not stop.is_set():
        try:
            _run(["xrandr", "--current"], timeout=10)
            return True
        except (OSError, subprocess.SubprocessError) as e:
            if time.monotonic() >= deadline:
                log.error(
                    "the X server (jupiter-kiosk-x.service) didn't answer within %d s: %s",
                    int(timeout),
                    x_failure_kind(e),
                )
                return False
        stop.wait(1)
    return False


def unix_sockets(text: str) -> list[tuple[str, str]]:
    """(St, Path) of each named socket in /proc/net/unix. St "01" = unconnected (a listener, or a bound
    datagram socket), "03" = connected. Abstract names start with "@"; unnamed sockets are left out."""
    sockets = []
    for line in text.splitlines()[1:]:
        fields = line.split(None, 7)
        if len(fields) == 8:
            sockets.append((fields[5], fields[7].strip()))
    return sockets


def visible_x_sockets(sockets: list[tuple[str, str]], number: int) -> list[str]:
    """X server sockets among the sockets of this (the host's) network namespace. There should be none:
    X has a network namespace of its own.

    Not counted: our held abstract name (@/tmp/.X11-unix/X<n>), and connected sockets named after the
    X socket. The kernel makes the server end of every accepted connection in the CLIENT's network
    namespace, so X's end of each kiosk.py / Chrome connection shows up here with the listener's path.
    Only X's listener (unconnected) means X shares this namespace. Every nvidia-xdriver socket counts:
    X binds them itself, and the GL clients' ends of them are unnamed.
    """
    listener = f"{X_SOCKET_DIR}/X{number}"
    return sorted({p for st, p in sockets if "nvidia-xdriver" in p or (p == listener and st == "01")})


def interface_names(text: str) -> list[str]:
    """Interface names from /proc/net/dev (two header lines, then "name: counters...")."""
    return [line.split(":", 1)[0].strip() for line in text.splitlines()[2:] if ":" in line]


def private_network(path: str | None = None) -> bool:
    """True if this process sees loopback and nothing else, i.e. a private network namespace. Every
    network namespace has lo, so an empty or unreadable list counts as NOT private (fail closed)."""
    try:
        with open(path or NET_DEV, encoding="utf-8") as f:
            names = interface_names(f.read())
    except OSError:
        return False
    return names == ["lo"]


def setup_display(cfg: Config) -> list[tuple[Screen, tuple[int, int, int, int]]]:
    outputs = parse_xrandr(_run(["xrandr", "--query"]))
    assigned, unused, warnings = assign_outputs(cfg.screens, outputs)
    for w in warnings:
        log.warning(w)
    if not assigned:
        raise ConfigError("no connected monitor on the display card")
    _run(xrandr_command(assigned, unused))
    _run(["xset", "s", "off", "s", "noblank", "-dpms"])
    after = {o.name: o for o in parse_xrandr(_run(["xrandr", "--current"]))}
    plan = planned_geometry(assigned)
    placed = []
    for screen, out in assigned:
        current = after.get(out.name)
        geometry = (current.geometry if current else None) or plan[screen.name]
        placed.append((screen, geometry))
        width, height, x, y = geometry
        log.info(
            "screen %s on output %s: %dx%d at %d,%d (rotate %s)",
            screen.name,
            out.name,
            width,
            height,
            x,
            y,
            screen.rotate,
        )
    return placed


# -------------------------------------------------------------------------------------- preflight


def xorg_bus_id(text: str) -> str | None:
    """PCI address from the Device section's BusID in sysfs form ("0000:0c:00.0").

    BusID is decimal: "PCI:12:0:0", or "PCI:12@0:0:0" with a domain. Only one BusID may appear, so the
    check can't look at one card while X uses another. Keywords are matched the way Xorg reads them
    (case-insensitive, underscores ignored: BusId, busid, Bus_ID), and a BusID whose value doesn't parse
    still counts, so it can't slip past the "exactly one" rule.
    """
    values = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        keyword, _, rest = line.partition(" ") if " " in line else line.partition("\t")
        if keyword.replace("_", "").lower() == "busid":
            values.append(re.fullmatch(r'"PCI:(\d+)(?:@(\d+))?:(\d+):(\d+)"', rest.strip()))
    if len(values) != 1 or values[0] is None:
        return None
    bus, domain, dev, fn = values[0].groups()
    return f"{int(domain or 0):04x}:{int(bus):02x}:{int(dev):02x}.{int(fn)}"


def voice_pins(text: str) -> list[str] | None:
    """GPU UUIDs the voice is pinned to (the last CUDA_VISIBLE_DEVICES in the env file, as systemd
    reads it). None when there's no pin or it isn't a UUID list: an index or a missing pin means
    the voice may use any card, so the caller must refuse."""
    found = re.findall(r"^[ \t]*(export[ \t]+)?CUDA_VISIBLE_DEVICES[ \t]*=[ \t]*(.*?)[ \t]*$", text, re.MULTILINE)
    if not found:
        return None
    exported, value = found[-1]
    if exported:  # systemd's EnvironmentFile= has no `export`: it would skip the line, so no pin
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    entries = [e.strip() for e in value.split(",")]
    if not entries or not all(UUID_RE.fullmatch(e) for e in entries):
        return None
    return [e.lower() for e in entries]


def parse_nvidia_information(text: str) -> tuple[str | None, int | None]:
    """(GPU UUID, device minor) from /proc/driver/nvidia/gpus/<pci>/information."""
    uuid = re.search(r"^GPU UUID:\s*(GPU-[0-9a-fA-F-]{36})\s*$", text, re.MULTILINE)
    minor = re.search(r"^Device Minor:\s*(\d+)\s*$", text, re.MULTILINE)
    return (uuid.group(1).lower() if uuid else None, int(minor.group(1)) if minor else None)


def nvidia_gpus(root: str = NVIDIA_PROC) -> dict[str, tuple[str, int]]:
    """{pci address: (uuid, minor)}. Read from /proc, so it works with device files hidden (nvidia-smi doesn't)."""
    gpus = {}
    try:
        names = os.listdir(root)
    except OSError:
        return {}
    for pci in names:
        try:
            with open(os.path.join(root, pci, "information"), encoding="utf-8") as f:
                uuid, minor = parse_nvidia_information(f.read())
        except OSError:
            continue
        if uuid is not None and minor is not None:
            gpus[pci.lower()] = (uuid, minor)
    return gpus


def device_nodes(pci: str, minor: int, sysfs: str = PCI_SYSFS) -> list[str]:
    """A card's device files: /dev/nvidiaN plus its /dev/dri card and render nodes."""
    nodes = [f"/dev/nvidia{minor}"]
    try:
        drm = sorted(os.listdir(os.path.join(sysfs, pci, "drm")))
    except OSError:
        drm = []
    nodes += [f"/dev/dri/{name}" for name in drm if name.startswith(("card", "renderD"))]
    return nodes


def is_blocked(path: str) -> bool:
    """True if the path is hidden by InaccessiblePaths= (a 0:0 device node over it) or doesn't exist."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return stat.S_ISCHR(st.st_mode) and st.st_rdev == 0


def preflight(cfg: Config, x_server: bool = False) -> int:
    """ExecStartPre: never put the display on the voice card; fail with one clear line.

    x_server (jupiter-kiosk-x.service): also refuse unless this process is in a private network
    namespace. systemd skips PrivateNetwork= without a word when the unit joins namespaces that
    jupiter-kiosk.service set up first, and X would then bind its abstract sockets in the host's.
    """
    if x_server and not private_network():
        log.error(
            "not in a private network namespace: PrivateNetwork= of jupiter-kiosk-x.service didn't apply "
            "(was jupiter-kiosk.service started on its own?); refusing to start X"
        )
        return 1
    try:
        with open(XORG_CONF, encoding="utf-8") as f:
            display_pci = xorg_bus_id(f.read())
    except OSError:
        log.error("%s is missing or unreadable", XORG_CONF)
        return 1
    if display_pci is None:
        log.error("%s needs exactly one BusID line: without it X takes every GPU, the voice card included", XORG_CONF)
        return 1
    gpus = nvidia_gpus()
    if not gpus:
        log.error("no NVIDIA GPUs in %s: is the driver loaded?", NVIDIA_PROC)
        return 1
    if display_pci not in gpus:
        log.error("no GPU at %s (BusID in %s): did a card move?", display_pci, XORG_CONF)
        return 1
    display_uuid, display_minor = gpus[display_pci]
    try:
        with open(VOICE_ENV, encoding="utf-8") as f:
            pins = voice_pins(f.read())
        voice_installed = True
    except FileNotFoundError:
        pins, voice_installed = [], False
        log.warning("%s not found (voice not installed here?): nothing to keep the display away from", VOICE_ENV)
    except OSError:
        log.error("%s is unreadable: can't check that the display card isn't the voice card", VOICE_ENV)
        return 1
    if voice_installed and pins is None:
        log.error(
            "can't tell which card the voice uses: CUDA_VISIBLE_DEVICES in %s must list GPU UUIDs; refusing to start",
            VOICE_ENV,
        )
        return 1
    if display_uuid in (pins or []):
        log.error("the display card (%s) is a voice card in %s: refusing to start", display_pci, VOICE_ENV)
        return 1
    blocked = [p for p in device_nodes(display_pci, display_minor) if is_blocked(p)]
    if blocked:
        log.error(
            "the display card's device files are hidden (%s): InaccessiblePaths in this kiosk unit "
            "(jupiter-kiosk.service or jupiter-kiosk-x.service) names the wrong card",
            " ".join(blocked),
        )
        return 1
    for pci, (uuid, minor) in sorted(gpus.items()):
        if uuid in (pins or []):
            reachable = [p for p in device_nodes(pci, minor) if not is_blocked(p)]
            if reachable:
                log.warning(
                    "the voice card's device files are reachable from the kiosk (%s): add them to "
                    "InaccessiblePaths in jupiter-kiosk.service and jupiter-kiosk-x.service",
                    " ".join(reachable),
                )
    if not os.access(cfg.chrome, os.X_OK):
        log.error("chrome not found or not executable: %s", cfg.chrome)
        return 1
    log.info("preflight ok: display on %s, %d screen(s) configured", display_pci, len(cfg.screens))
    return 0


# -------------------------------------------------------------------------------------------- CDP


class _Pending:
    __slots__ = ("done", "error", "result")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: dict = {}
        self.error: dict | None = None


class Cdp:
    """Chrome DevTools Protocol over --remote-debugging-pipe: NUL-terminated JSON both ways."""

    def __init__(self, send_fd: int, recv_fd: int) -> None:
        self._send = os.fdopen(send_fd, "wb", buffering=0)
        self._recv = os.fdopen(recv_fd, "rb", buffering=0)
        self._write_lock = threading.Lock()
        self._id_lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, _Pending] = {}
        self.events: queue.Queue[dict] = queue.Queue(maxsize=256)
        self.closed = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, name="cdp-reader", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        buf = bytearray()
        try:
            while True:
                chunk = self._recv.read(1 << 16)
                if not chunk:
                    break
                start = len(buf)
                buf += chunk
                for message in split_messages(buf, start):
                    self._dispatch(message)
        except (OSError, ValueError, TypeError, AttributeError):
            pass  # a dead pipe or a frame that isn't a JSON object: treat both as a closed connection
        finally:
            self.closed.set()
            with self._id_lock:
                pending, self._pending = self._pending, {}
            for p in pending.values():
                p.error = {"message": "connection closed"}
                p.done.set()
            # Only this thread closes the read end, so its fd number can't be reused under a read().
            with suppress(OSError):
                self._recv.close()

    def _dispatch(self, message: dict) -> None:
        if "id" in message:
            with self._id_lock:
                p = self._pending.pop(message["id"], None)
            if p is not None:
                p.result = message.get("result") or {}
                p.error = message.get("error")
                p.done.set()
            return
        with suppress(queue.Full):  # events are hints (crash, detach); the periodic page check catches the rest
            self.events.put_nowait(message)

    def call(
        self, method: str, params: dict | None = None, session: str | None = None, timeout: float = CDP_TIMEOUT
    ) -> dict:
        with self._id_lock:
            if self.closed.is_set():  # checked under the lock the reader takes after setting it
                raise CdpError(f"{method}: connection closed")
            self._next_id += 1
            message_id = self._next_id
            pending = self._pending[message_id] = _Pending()
        message: dict = {"id": message_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        try:
            with self._write_lock:
                self._send.write(json.dumps(message).encode() + b"\0")
        except (OSError, ValueError):
            with self._id_lock:
                self._pending.pop(message_id, None)
            raise CdpError(f"{method}: write failed") from None
        if not pending.done.wait(timeout):
            with self._id_lock:
                self._pending.pop(message_id, None)
            raise CdpError(f"{method}: no answer in {timeout:.0f}s")
        if pending.error is not None:
            raise CdpError(f"{method}: {str(pending.error.get('message', 'error'))[:120]}")
        return pending.result

    def close(self) -> None:
        """Close the write end; the reader sees EOF once Chrome is gone and closes the read end."""
        with suppress(OSError), self._write_lock:
            self._send.close()
        self._reader.join(timeout=2)


def split_messages(buf: bytearray, search_from: int = 0) -> list[dict]:
    """Pop every complete NUL-terminated JSON message off the front of buf (in place)."""
    messages = []
    pos = buf.find(b"\0", max(search_from - 1, 0))
    while pos != -1:
        raw = bytes(buf[:pos])
        del buf[: pos + 1]
        if raw:
            messages.append(json.loads(raw))
        pos = buf.find(b"\0")
    return messages


def kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM Chrome's process group, then SIGKILL whatever is left (renderers outlive a crashed leader)."""
    pgid = proc.pid  # start_new_session: Chrome leads its own group
    with suppress(OSError):
        os.killpg(pgid, signal.SIGTERM)
    with suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=10)
    with suppress(OSError):
        os.killpg(pgid, signal.SIGKILL)
    with suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)


def launch_chrome(args: list[str], env: dict[str, str] | None = None) -> tuple[subprocess.Popen, Cdp]:
    """Start Chrome with the DevTools pipe on fds 3 (commands in) and 4 (answers out).

    A bash trampoline moves the pipe ends onto 3 and 4, so no preexec_fn is needed (this process
    runs threads, where preexec_fn is unsafe). Not /bin/sh: dash only takes single-digit fd numbers,
    and the child's ends are parked at >= 10 (close-on-exec here; pass_fds hands them to the child).
    """
    opened: list[int] = []
    keep: set[int] = set()
    proc: subprocess.Popen | None = None
    try:
        to_chrome_r, to_chrome_w = os.pipe()
        opened += [to_chrome_r, to_chrome_w]
        from_chrome_r, from_chrome_w = os.pipe()
        opened += [from_chrome_r, from_chrome_w]
        child_r = fcntl.fcntl(to_chrome_r, fcntl.F_DUPFD_CLOEXEC, 10)  # >= 10: can't collide with 3 or 4
        opened.append(child_r)
        child_w = fcntl.fcntl(from_chrome_w, fcntl.F_DUPFD_CLOEXEC, 10)
        opened.append(child_w)
        script = f'exec "$@" 3<&{child_r} 4>&{child_w} {child_r}<&- {child_w}>&-'
        # Chrome's stderr can carry page URLs, so it is dropped unless KIOSK_DEBUG_CHROME=1 (troubleshooting).
        chrome_stderr = None if os.environ.get("KIOSK_DEBUG_CHROME") == "1" else subprocess.DEVNULL
        proc = subprocess.Popen(
            ["/bin/bash", "-c", script, "bash", *args],
            pass_fds=(child_r, child_w),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=chrome_stderr,
            env=env,
            start_new_session=True,
        )
        keep = {to_chrome_w, from_chrome_r}  # owned by Cdp from here on, even if it fails half-way
        return proc, Cdp(send_fd=to_chrome_w, recv_fd=from_chrome_r)
    except BaseException:
        if proc is not None:
            kill_group(proc)
        raise
    finally:
        for fd in opened:
            if fd not in keep:
                with suppress(OSError):
                    os.close(fd)


def chrome_env(cfg: Config) -> dict[str, str]:
    """Chrome's environment: ours, plus TZ so the pages show local time (Jupiter runs on UTC)."""
    env = dict(os.environ)
    if cfg.timezone:
        env["TZ"] = cfg.timezone
    return env


def chrome_args(cfg: Config, screen: Screen, profile_dir: str, geometry: tuple[int, int, int, int]) -> list[str]:
    width, height, x, y = geometry
    args = [
        cfg.chrome,
        "--remote-debugging-pipe",
        f"--user-data-dir={profile_dir}",
        "--kiosk",
        f"--window-position={x},{y}",
        f"--window-size={width},{height}",
        *CHROME_FLAGS,
    ]
    if cfg.host_map:
        rules = []
        for host, ip in sorted(cfg.host_map.items()):
            target = f"[{ip}]" if ipaddress.ip_address(ip).version == 6 else ip
            rules.append(f"MAP {host} {target}")
        args.append("--host-resolver-rules=" + ", ".join(rules))
    args.append("about:blank")
    return args


# ---------------------------------------------------------------------------------- page decisions


def binary_stamp(path: str) -> tuple[int, int] | None:
    """(inode, mtime) of the real Chrome binary: an apt upgrade replaces it, and a browser started from
    the old files breaks as new renderers start. None if it can't be read (then no restart is forced)."""
    try:
        st = os.stat(os.path.realpath(path))
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns)


def page_is_up(screen: Screen, href: str, status: int) -> bool:
    """On the configured origin with a non-5xx answer (0 = unknown, e.g. a cached or SPA page)."""
    return origin_of(href) == screen.origin and status < 500


def next_action(
    screen: Screen, href: str, status: int, now: float, last_login: float, last_retry: float, last_load: float
) -> str:
    """What the page needs now: 'login', 'retry', 'reload' or 'none'. Pure, for the tests."""
    if not page_is_up(screen, href, status):
        # chrome-error:// (unreachable), about:blank, another site, or a 5xx such as Tailscale Serve's
        # 502 while Grafana is still starting: load the page again, at most every ERROR_RETRY_SECONDS.
        return "retry" if now - last_retry >= ERROR_RETRY_SECONDS else "none"
    path = urlsplit(href).path
    if screen.login != "none" and (path == "/login" or path.startswith("/login/")):
        return "login" if now - last_login >= LOGIN_RETRY_SECONDS else "none"
    if screen.reload_minutes and now - last_load >= screen.reload_minutes * 60:
        return "reload"
    return "none"


def grafana_login_expression(user: str, password: str, origin: str) -> str:
    """JS for the Grafana page: same-origin POST /login, so the session cookie lands normally.

    It runs in an isolated world (the page's own scripts can't hook its fetch or JSON), and it checks
    in the page that it's still on the login page of the configured origin, so a navigation between
    the Python-side check and this call can't send the password anywhere else. -1 = not sent.
    """
    return (
        "(async (u, p, o) => { "
        "if (location.origin !== o || !(location.pathname === '/login' || location.pathname.startsWith('/login/'))) "
        "return -1; "
        "const r = await fetch('/login', {method: 'POST', credentials: 'same-origin', "
        "headers: {'Content-Type': 'application/json', 'Accept': 'application/json'}, "
        "body: JSON.stringify({user: u, password: p})}); return r.status; })"
        f"({json.dumps(user)}, {json.dumps(password)}, {json.dumps(origin)})"
    )


def screen_cookies(screen: Screen, secrets: dict[str, str]) -> list[dict]:
    """Cookies the screen's browser profile needs: the dashboard session and/or display mode.

    Session cookies (no expiry), set again at every browser start and whenever the dashboard asks for
    a login. Chrome still writes session cookies to the profile's cookie database, so the profiles live
    in the kiosk user's runtime dir (tmpfs, cleared when the kiosk stops): nothing reaches the disk.
    """

    def cookie(name: str, value: str) -> dict:
        return {
            "name": name,
            "value": value,
            "url": screen.origin + "/",  # host-only cookie for exactly this origin
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "Lax",
        }

    cookies = []
    if screen.login == "dashboard":
        cookies.append(cookie(DASHBOARD_COOKIE, secrets["dashboard_kiosk_session"]))
    if screen.hide_nav:
        cookies.append(cookie(DISPLAY_COOKIE, "kiosk"))
    return cookies


def parse_page_state(value: object) -> tuple[str, int]:
    """(href, HTTP status) from PAGE_STATE_JS's JSON string; ("", 0) for anything unexpected."""
    try:
        href, status = json.loads(value) if isinstance(value, str) else ("", 0)
    except (ValueError, TypeError):
        return "", 0
    if not isinstance(href, str):
        href = ""
    if isinstance(status, bool) or not isinstance(status, int):
        status = 0
    return href, status


# ----------------------------------------------------------------------------------------- runner


class ScreenRunner(threading.Thread):
    def __init__(
        self,
        cfg: Config,
        screen: Screen,
        geometry: tuple[int, int, int, int],
        secrets: dict[str, str],
        stop: threading.Event,
    ) -> None:
        super().__init__(name=f"screen-{screen.name}", daemon=True)
        self.cfg = cfg
        self.screen = screen
        self.geometry = geometry
        self.secrets = secrets
        self.stop = stop
        self.wake = threading.Event()
        self.snapshot_wanted = threading.Event()
        self.proc: subprocess.Popen | None = None
        self.cdp: Cdp | None = None
        self.down = False  # page currently not loading (logged once on the way down, once on recovery)
        # tmpfs (/run/user/<uid>, gone when the kiosk stops): cookies, cache and crash data never hit disk.
        base = os.environ.get("XDG_RUNTIME_DIR") or os.path.join(os.environ.get("HOME") or "/tmp", ".cache")
        self.profile_dir = os.path.join(base, "jupiter-kiosk", "profiles", screen.name)

    def run(self) -> None:
        backoff = 5.0
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self._session()
            except CdpError as e:
                log.warning("screen %s: browser restart (%s)", self.screen.name, e)
            except Exception as e:  # keep the screen alive whatever happened; error kind only
                log.error("screen %s: unexpected %s; restarting the browser", self.screen.name, type(e).__name__)
            finally:
                self._kill()
            if self.stop.is_set():
                break
            if time.monotonic() - started > STABLE_RUN_SECONDS:
                backoff = 5.0
            self.stop.wait(backoff)
            backoff = min(backoff * 2, 120.0)

    def _session(self) -> None:
        os.makedirs(self.profile_dir, mode=0o700, exist_ok=True)
        chrome_version = binary_stamp(self.cfg.chrome)
        self.proc, self.cdp = launch_chrome(
            chrome_args(self.cfg, self.screen, self.profile_dir, self.geometry), chrome_env(self.cfg)
        )
        target_id, session = self._attach()
        self._place_window(target_id)
        self.cdp.call("Inspector.enable", session=session)
        self._set_cookies()
        log.info("screen %s: browser up", self.screen.name)
        self._navigate(session)
        last_load = last_retry = time.monotonic()
        last_login = -1e9
        failed_reads = 0
        crashes: list[float] = []
        upgrade_seen: tuple[int, int] | None = None
        while not self.stop.is_set():
            if self.proc.poll() is not None:
                raise CdpError(f"chrome exited ({self.proc.returncode})")
            stamp = binary_stamp(self.cfg.chrome)
            if stamp != chrome_version:
                if stamp is not None and stamp == upgrade_seen:
                    raise CdpError("chrome was upgraded; starting the new version")
                upgrade_seen = stamp  # restart on the next check if it stays put: dpkg may still be unpacking
            if self._drain_events():
                now = time.monotonic()
                crashes = [t for t in crashes if now - t < CRASH_WINDOW_SECONDS] + [now]
                if len(crashes) >= CRASH_LIMIT:
                    raise CdpError(f"page crashed {len(crashes)} times in {CRASH_WINDOW_SECONDS // 60} min")
                log.warning("screen %s: page crashed; loading it again", self.screen.name)
                self._navigate(session)
                last_load = last_retry = now
            if self.snapshot_wanted.is_set():
                self.snapshot_wanted.clear()
                self._snapshot(session)
            try:
                href, status = self._page_state(session)
                failed_reads = 0
            except CdpError:
                if self.cdp.closed.is_set():
                    raise
                failed_reads += 1
                if failed_reads >= HUNG_CHECKS:
                    raise CdpError("page stopped answering") from None
                self._sleep()
                continue
            self._note_health(href, status)
            now = time.monotonic()
            action = next_action(self.screen, href, status, now, last_login, last_retry, last_load)
            if action == "login":
                last_login = now
                self._login(session)
                self._navigate(session)
                last_load = now
            elif action == "retry":
                last_retry = now
                self._navigate(session)
                last_load = now
            elif action == "reload":
                self._tolerant("Page.reload", {"ignoreCache": False}, session, CDP_TIMEOUT)
                last_load = now
            self._sleep()

    def _sleep(self) -> None:
        self.wake.wait(CHECK_SECONDS)
        self.wake.clear()

    def _note_health(self, href: str, status: int) -> None:
        up = page_is_up(self.screen, href, status)
        if not up and not self.down:
            why = f"HTTP {status}" if origin_of(href) == self.screen.origin else "no connection or wrong address"
            log.warning(
                "screen %s: page not loading (%s); retrying every %d s", self.screen.name, why, ERROR_RETRY_SECONDS
            )
        elif up and self.down:
            log.info("screen %s: page loading again", self.screen.name)
        self.down = not up

    def _attach(self) -> tuple[str, str]:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            targets = self.cdp.call("Target.getTargets").get("targetInfos", [])
            page = next((t for t in targets if t.get("type") == "page"), None)
            if page:
                session = self.cdp.call("Target.attachToTarget", {"targetId": page["targetId"], "flatten": True})
                return page["targetId"], session["sessionId"]
            if self.stop.wait(0.5):
                break
        raise CdpError("no page target")

    def _place_window(self, target_id: str) -> None:
        """--window-position normally does it; move the window if Chrome put it elsewhere."""
        width, height, x, y = self.geometry
        window = self.cdp.call("Browser.getWindowForTarget", {"targetId": target_id})
        b = window.get("bounds", {})
        if (b.get("left"), b.get("top"), b.get("width"), b.get("height")) == (x, y, width, height):
            return
        wid = window["windowId"]
        self.cdp.call("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
        self.cdp.call(
            "Browser.setWindowBounds",
            {"windowId": wid, "bounds": {"left": x, "top": y, "width": width, "height": height}},
        )
        self.cdp.call("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "fullscreen"}})

    def _drain_events(self) -> bool:
        crashed = False
        while True:
            try:
                event = self.cdp.events.get_nowait()
            except queue.Empty:
                return crashed
            method = event.get("method")
            if method == "Inspector.targetCrashed":
                crashed = True
            elif method == "Target.detachedFromTarget":
                raise CdpError("page closed")

    def _page_state(self, session: str) -> tuple[str, int]:
        result = self.cdp.call(
            "Runtime.evaluate", {"expression": PAGE_STATE_JS, "returnByValue": True}, session=session, timeout=10
        )
        return parse_page_state(result.get("result", {}).get("value"))

    def _tolerant(self, method: str, params: dict, session: str | None, timeout: float) -> dict | None:
        """A call whose timeout means "slow server", not "dead browser": None on timeout, raise if the pipe died."""
        try:
            return self.cdp.call(method, params, session=session, timeout=timeout)
        except CdpError:
            if self.cdp.closed.is_set() or self.proc.poll() is not None:
                raise
            return None

    def _navigate(self, session: str) -> None:
        # Page.navigate answers once the load commits or fails; an unanswering host keeps it pending.
        # Then the next checks see a non-loading page and retry: no browser restart for a slow server.
        self._tolerant("Page.navigate", {"url": self.screen.url}, session, timeout=30)

    def _set_cookies(self) -> None:
        cookies = screen_cookies(self.screen, self.secrets)
        if cookies:
            self.cdp.call("Storage.setCookies", {"cookies": cookies})

    def _login(self, session: str) -> None:
        if self.screen.login == "dashboard":
            log.warning(
                "screen %s: dashboard asked for a login; setting the session cookie again "
                "(if this repeats, KIOSK_TOKEN changed: re-run make_logins.py)",
                self.screen.name,
            )
            self._set_cookies()
            return
        frame = self.cdp.call("Page.getFrameTree", session=session)["frameTree"]["frame"]["id"]
        world = self.cdp.call(
            "Page.createIsolatedWorld", {"frameId": frame, "worldName": "jupiter-kiosk"}, session=session
        )
        expression = grafana_login_expression(
            self.secrets["grafana_user"], self.secrets["grafana_password"], self.screen.origin
        )
        result = self._tolerant(
            "Runtime.evaluate",
            {
                "expression": expression,
                "contextId": world["executionContextId"],
                "awaitPromise": True,
                "returnByValue": True,
            },
            session,
            30,
        )
        status = (result or {}).get("result", {}).get("value")
        if status == 200:
            log.info("screen %s: logged in to Grafana", self.screen.name)
        elif status == -1:
            log.info("screen %s: page left the login page before the login; not sent", self.screen.name)
        else:
            log.warning(
                "screen %s: Grafana login failed (%s); next try in %d min",
                self.screen.name,
                f"HTTP {status}" if isinstance(status, int) and not isinstance(status, bool) else "no answer",
                LOGIN_RETRY_SECONDS // 60,
            )

    def _snapshot(self, session: str) -> None:
        try:
            folder = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "jupiter-kiosk")
            os.makedirs(folder, mode=0o700, exist_ok=True)
            data = self.cdp.call("Page.captureScreenshot", {"format": "png"}, session=session, timeout=30).get(
                "data", ""
            )
            path = os.path.join(folder, f"{self.screen.name}.png")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(base64.b64decode(data))
            log.info("screen %s: snapshot saved", self.screen.name)
        except (CdpError, OSError, ValueError) as e:
            if isinstance(e, CdpError) and self.cdp.closed.is_set():
                raise
            log.warning("screen %s: snapshot failed (%s)", self.screen.name, type(e).__name__)

    def _kill(self) -> None:
        if self.proc is not None:  # Chrome first: once it's gone the pipes can close without races
            kill_group(self.proc)
            self.proc = None
        if self.cdp is not None:
            self.cdp.close()
            self.cdp = None


# ------------------------------------------------------------------------------------------- main


def run_kiosk(
    cfg: Config, secrets: dict[str, str], stop: threading.Event, runners: list[ScreenRunner], number: int
) -> int:
    """Everything after display `number`'s abstract name is held: wait for X, check its isolation, lay
    out, run the screens."""
    if not wait_for_x(stop):
        return 0 if stop.is_set() else 1
    try:
        with open(NET_UNIX, encoding="utf-8", errors="replace") as f:
            exposed = visible_x_sockets(unix_sockets(f.read()), number)
    except OSError:
        log.error("can't read %s to check that X has its own network namespace", NET_UNIX)
        return 1
    if exposed:
        log.error(
            "%d X server socket(s) reachable from the host network namespace: jupiter-kiosk-x.service must "
            "run with PrivateNetwork=yes; refusing to start",
            len(exposed),
        )
        return 1
    try:
        connected = wait_for_monitor(stop)
        if connected is None:
            return 0
        placed = setup_display(cfg)
    except (ConfigError, OSError, subprocess.SubprocessError) as e:
        log.error("display setup failed: %s", e if isinstance(e, ConfigError) else type(e).__name__)
        return 1

    runners.extend(ScreenRunner(cfg, screen, geometry, secrets, stop) for screen, geometry in placed)
    for r in runners:
        r.start()
    rc = 0
    next_check = time.monotonic() + OUTPUT_CHECK_SECONDS
    while not stop.wait(5):
        dead = [r.screen.name for r in runners if not r.is_alive()]
        if dead:
            log.error("screen runner(s) stopped: %s", ", ".join(dead))
            rc = 1
            break
        if time.monotonic() >= next_check:
            next_check = time.monotonic() + OUTPUT_CHECK_SECONDS
            try:
                now_connected = connected_outputs(probe=False)
            except (OSError, subprocess.SubprocessError):
                continue
            if now_connected != connected:
                log.info(
                    "monitors changed (%d connected, was %d); restarting to lay them out again",
                    len(now_connected),
                    len(connected),
                )
                rc = 3
                break
    log.info("stopping")
    stop.set()
    for r in runners:
        r.wake.set()
    for r in runners:
        r.join(timeout=20)
    return rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Closet monitors kiosk (run by jupiter-kiosk.service)")
    parser.add_argument("--preflight", action="store_true", help="check config, secrets and GPU pinning, then exit")
    parser.add_argument(
        "--x-server",
        action="store_true",
        help="with --preflight, for jupiter-kiosk-x.service: also require a private network namespace",
    )
    parser.add_argument("--config", default=CONFIG_PATH)
    parser.add_argument("--secrets", default=SECRETS_PATH)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)

    # Handlers first, so an early SIGUSR1 can't kill the process; they only set Events.
    stop = threading.Event()
    runners: list[ScreenRunner] = []

    def on_stop(_signum: int, _frame: object) -> None:
        stop.set()
        for r in runners:
            r.wake.set()

    def on_snapshot(_signum: int, _frame: object) -> None:
        for r in runners:
            r.snapshot_wanted.set()
            r.wake.set()

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, on_stop)
    signal.signal(signal.SIGUSR1, on_snapshot)

    try:
        cfg = load_config(read_json(args.config))
        secrets = load_secrets(read_json(args.secrets), cfg)
    except ConfigError as e:
        log.error("%s", e)
        return 2
    if args.preflight:
        return preflight(cfg, x_server=args.x_server)
    if args.x_server:
        log.error("--x-server only goes with --preflight")
        return 2
    try:
        number = display_number(os.environ.get("DISPLAY", ""))
    except ConfigError as e:
        log.error("%s", e)
        return 2
    runtime = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("HOME")
    if not runtime:
        log.error("neither XDG_RUNTIME_DIR nor HOME is set: can't find the X auth cookie")
        return 2
    # Always the file start-x.sh gave X (it sets XAUTHORITY the same way), whatever came from outside.
    os.environ["XAUTHORITY"] = os.path.join(runtime, XAUTHORITY_NAME)
    try:
        held = hold_x_abstract_name(number)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            log.error(
                "the X display's abstract socket name is taken in the host network namespace: something "
                "may be posing as the X server; refusing to connect"
            )
        else:
            log.error("can't hold the X display's abstract socket name (%s)", e.strerror or type(e).__name__)
        return 1
    with held:
        return run_kiosk(cfg, secrets, stop, runners, number)


if __name__ == "__main__":
    sys.exit(main())
