"""
Tests for scripts/jupiter-kiosk/kiosk.py, the closet monitors kiosk on Jupiter
(one fullscreen Chrome per monitor, driven over --remote-debugging-pipe).

Everything runs without X, Chrome, a GPU or root:
  - config / secrets / xrandr / Xorg / env-file parsing are pure functions;
  - the GPU preflight runs against a fake /proc/driver/nvidia + sysfs in tmp_path
    (is_blocked is replaced: a 0:0 char device can't be made without root);
  - the DevTools client runs over real os.pipe() pairs with a fake "Chrome"
    thread on the other end, and launch_chrome() starts a tiny Python process
    in Chrome's place to check the fd 3/4 plumbing;
  - ScreenRunner's page loop runs against a scripted CDP object.

The script lives in a folder with a hyphen and uses dataclasses with
`from __future__ import annotations`, so it is loaded by path and registered in
sys.modules before exec (dataclass resolves annotations through it).
"""

from __future__ import annotations

import base64
import errno
import importlib.util
import ipaddress
import json
import logging
import os
import queue
import re
import socket
import stat
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_HERE = Path(__file__).resolve()
_REPO = _HERE.parents[2]


def _first_existing(*candidates: Path) -> Path | None:
    return next((c for c in candidates if c.is_file()), None)


_KIOSK_PATH = _first_existing(
    _REPO / "scripts" / "jupiter-kiosk" / "kiosk.py",
    Path("/app/scripts/jupiter-kiosk/kiosk.py"),
)
if _KIOSK_PATH is None:
    pytest.skip("scripts/jupiter-kiosk/kiosk.py not found", allow_module_level=True)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # before exec: @dataclass looks the module up by name
    spec.loader.exec_module(mod)
    return mod


kiosk = _load(_KIOSK_PATH, "jupiter_kiosk")
Screen = kiosk.Screen
Output = kiosk.Output
ConfigError = kiosk.ConfigError
CdpError = kiosk.CdpError

EXAMPLE = _KIOSK_PATH.with_name("kiosk.example.json")
XORG_SHIPPED = _KIOSK_PATH.with_name("jupiter-kiosk.xorg.conf")

U_VOICE = "GPU-0a1b2c3d-1111-2222-3333-444455556666"
U_DISPLAY = "GPU-ffffffff-aaaa-bbbb-cccc-dddddddddddd"
VOICE_PCI = "0000:0b:00.0"
DISPLAY_PCI = "0000:0c:00.0"


class _Proxy:
    """A module stand-in: the given attributes, everything else from the real module."""

    def __init__(self, real, **override):
        self._real = real
        self.__dict__.update(override)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _screen(**over) -> dict:
    raw = {"name": "left", "url": "https://host.example/d"}
    raw.update(over)
    return raw


def _cfg(*screens: dict, **top) -> dict:
    data = {"screens": list(screens) if screens else [_screen()]}
    data.update(top)
    return data


@pytest.fixture
def zoneinfo(tmp_path, monkeypatch):
    """A small zoneinfo tree, so the timezone check doesn't depend on the image's tzdata."""
    root = tmp_path / "zoneinfo"
    (root / "America").mkdir(parents=True)
    (root / "America" / "Chicago").write_bytes(b"TZif2")
    (root / "UTC").write_bytes(b"TZif2")
    monkeypatch.setattr(kiosk, "ZONEINFO", str(root))
    return root


# ------------------------------------------------------------------------------------- origin_of


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        pytest.param("https://Host.Example/path?q=1#f", "https://host.example", id="lowercase-host"),
        pytest.param("HTTPS://host.example/", "https://host.example", id="lowercase-scheme"),
        pytest.param("https://host.example:443/x", "https://host.example", id="https-default-port"),
        pytest.param("http://host.example:80/x", "http://host.example", id="http-default-port"),
        pytest.param("https://host.example:3000/d", "https://host.example:3000", id="other-port"),
        pytest.param("http://host.example:443/", "http://host.example:443", id="443-on-http-kept"),
        pytest.param("https://[FD7A:115C::1]:8443/", "https://[fd7a:115c::1]:8443", id="ipv6"),
        pytest.param("https://[::1]/", "https://[::1]", id="ipv6-default-port"),
        pytest.param("chrome-error://chromewebdata/", None, id="chrome-error"),
        pytest.param("about:blank", None, id="about-blank"),
        pytest.param("ftp://host.example/", None, id="ftp"),
        pytest.param("file:///etc/passwd", None, id="file"),
        pytest.param("https:///path", None, id="no-host"),
        pytest.param("https://h:99999/", None, id="port-out-of-range"),
        pytest.param("https://h:abc/", None, id="port-not-a-number"),
        pytest.param("", None, id="empty"),
    ],
)
def test_origin_of(url, expected):
    assert kiosk.origin_of(url) == expected


def test_screen_origin_property():
    assert Screen(name="a", url="https://HOST.example:443/d").origin == "https://host.example"


# ------------------------------------------------------------------------------------ load_config

_LONG_HOST = ".".join(["a" * 63] * 3 + ["a" * 61])  # 253 chars, the longest allowed

REJECTED_CONFIGS = [
    pytest.param([], id="not-object"),
    pytest.param(_cfg(extra=1), id="unknown-top-key"),
    pytest.param(_cfg(chrome="chrome"), id="chrome-relative"),
    pytest.param(_cfg(chrome=5), id="chrome-not-str"),
    pytest.param({}, id="screens-missing"),
    pytest.param({"screens": []}, id="screens-empty"),
    pytest.param({"screens": {"name": "left"}}, id="screens-not-list"),
    pytest.param({"screens": [_screen(name=f"s{i}") for i in range(5)]}, id="five-screens"),
    pytest.param({"screens": ["left"]}, id="screen-not-object"),
    pytest.param(_cfg({"url": "https://host.example/"}), id="name-missing"),
    pytest.param(_cfg(_screen(name=1)), id="name-not-str"),
    pytest.param(_cfg(_screen(name="Left")), id="name-uppercase"),
    pytest.param(_cfg(_screen(name="-left")), id="name-leading-dash"),
    pytest.param(_cfg(_screen(name="a" * 33)), id="name-too-long"),
    pytest.param(_cfg(_screen(name="left\n")), id="name-trailing-newline"),
    pytest.param(_cfg(_screen(name="le ft")), id="name-space"),
    pytest.param(_cfg(_screen(colour="red")), id="unknown-screen-key"),
    pytest.param(_cfg({"name": "left"}), id="url-missing"),
    pytest.param(_cfg(_screen(url=5)), id="url-not-str"),
    pytest.param(_cfg(_screen(url="ftp://host.example/")), id="url-ftp"),
    pytest.param(_cfg(_screen(url="chrome-error://chromewebdata/")), id="url-chrome-error"),
    pytest.param(_cfg(_screen(url="https://h:99999/")), id="url-bad-port"),
    pytest.param(_cfg(_screen(url="https:///d")), id="url-no-host"),
    pytest.param(_cfg(_screen(url="https://user:pw@host.example/")), id="url-user-password"),
    pytest.param(_cfg(_screen(url="https://user@host.example/")), id="url-user-only"),
    pytest.param(_cfg(_screen(url="https://:pw@host.example/")), id="url-password-only"),
    pytest.param(_cfg(_screen(url="http://host.example/", login="grafana")), id="http-grafana-login"),
    pytest.param(_cfg(_screen(url="http://host.example/", login="dashboard")), id="http-dashboard-login"),
    pytest.param(_cfg(_screen(url="http://host.example/", hide_nav=True)), id="http-hide-nav"),
    pytest.param(_cfg(_screen(output=0)), id="output-not-str"),
    pytest.param(_cfg(_screen(output="HDMI 0")), id="output-space"),
    pytest.param(_cfg(_screen(output="HDMI-0\n")), id="output-trailing-newline"),
    pytest.param(_cfg(_screen(output="H" * 33)), id="output-too-long"),
    pytest.param(_cfg(_screen(output="")), id="output-empty"),
    pytest.param(_cfg(_screen(rotate="upside-down")), id="rotate-bad"),
    pytest.param(_cfg(_screen(rotate=["left"])), id="rotate-list"),
    pytest.param(_cfg(_screen(login="oauth")), id="login-bad"),
    pytest.param(_cfg(_screen(login=None)), id="login-null"),
    pytest.param(_cfg(_screen(hide_nav="true")), id="hide-nav-str"),
    pytest.param(_cfg(_screen(hide_nav=1)), id="hide-nav-int"),
    pytest.param(_cfg(_screen(reload_minutes=True)), id="reload-bool"),
    pytest.param(_cfg(_screen(reload_minutes="5")), id="reload-str"),
    pytest.param(_cfg(_screen(reload_minutes=-1)), id="reload-negative"),
    pytest.param(_cfg(_screen(reload_minutes=1441)), id="reload-over-a-day"),
    pytest.param(_cfg(_screen(reload_minutes=float("nan"))), id="reload-nan"),
    pytest.param(_cfg(_screen(reload_minutes=float("inf"))), id="reload-inf"),
    pytest.param(_cfg(_screen(), _screen()), id="duplicate-names"),
    pytest.param(_cfg(_screen(name="a", output="HDMI-0"), _screen(name="b", output="HDMI-0")), id="duplicate-outputs"),
    pytest.param(_cfg(host_map=[]), id="host-map-not-object"),
    pytest.param(_cfg(host_map={"bad host": "1.2.3.4"}), id="host-map-key-space"),
    pytest.param(_cfg(host_map={"-x.example": "1.2.3.4"}), id="host-map-key-leading-dash"),
    pytest.param(_cfg(host_map={"host.example\n": "1.2.3.4"}), id="host-map-key-newline"),
    pytest.param(_cfg(host_map={"a" * 64 + ".example": "1.2.3.4"}), id="host-map-label-64"),
    pytest.param(_cfg(host_map={_LONG_HOST + "a": "1.2.3.4"}), id="host-map-key-254"),
    pytest.param(_cfg(host_map={"host.example/x": "1.2.3.4"}), id="host-map-key-slash"),
    pytest.param(_cfg(host_map={"host.example": 1}), id="host-map-ip-int"),
    pytest.param(_cfg(host_map={"host.example": None}), id="host-map-ip-null"),
    pytest.param(_cfg(host_map={"host.example": "999.1.1.1"}), id="host-map-ip-bad"),
    pytest.param(_cfg(host_map={"host.example": "10.0.0.0/24"}), id="host-map-ip-cidr"),
    pytest.param(_cfg(host_map={"host.example": "100.064.0.1"}), id="host-map-ip-leading-zero"),
    pytest.param(_cfg(host_map={"host.example": "jupiter"}), id="host-map-ip-name"),
    pytest.param(_cfg(timezone=5), id="tz-not-str"),
    pytest.param(_cfg(timezone=""), id="tz-empty"),
    pytest.param(_cfg(timezone="Mars/Olympus"), id="tz-unknown"),
    pytest.param(_cfg(timezone="America"), id="tz-directory"),
    pytest.param(_cfg(timezone="America/../UTC"), id="tz-dotdot"),
    pytest.param(_cfg(timezone="../zoneinfo/UTC"), id="tz-leading-dotdot"),
    pytest.param(_cfg(timezone="/etc/localtime"), id="tz-absolute"),
    pytest.param(_cfg(timezone="America/Chicago\n"), id="tz-trailing-newline"),
    pytest.param(_cfg(timezone="America//Chicago"), id="tz-double-slash"),
]


@pytest.mark.parametrize("data", REJECTED_CONFIGS)
def test_load_config_rejects(data, zoneinfo):
    with pytest.raises(ConfigError):
        kiosk.load_config(data)


def test_load_config_defaults():
    cfg = kiosk.load_config({"screens": [{"name": "left", "url": "https://host.example/d"}]})
    assert cfg.chrome == "/opt/google/chrome/chrome"
    assert cfg.host_map == {}
    assert cfg.timezone is None
    (s,) = cfg.screens
    assert (s.output, s.rotate, s.login, s.reload_minutes, s.hide_nav) == (None, "normal", "none", 0.0, False)
    assert isinstance(cfg.screens, tuple)


def test_load_config_unknown_key_error_names_the_key():
    with pytest.raises(ConfigError, match="hide_navs"):
        kiosk.load_config(_cfg(_screen(hide_navs=True)))


@pytest.mark.parametrize(
    "screen",
    [
        pytest.param(_screen(url="http://host.example/"), id="http-without-login"),
        pytest.param(_screen(reload_minutes=0), id="reload-0"),
        pytest.param(_screen(reload_minutes=1440), id="reload-1440"),
        pytest.param(_screen(reload_minutes=2.5), id="reload-float"),
        pytest.param(_screen(name="a" * 32), id="name-32"),
        pytest.param(_screen(name="0-a"), id="name-leading-digit"),
        pytest.param(_screen(output="H" * 32), id="output-32"),
        pytest.param(_screen(rotate="inverted", login="grafana", hide_nav=False), id="inverted-grafana"),
        pytest.param(_screen(url="https://[fd7a:115c::1]:8443/d", login="dashboard"), id="ipv6-url"),
    ],
)
def test_load_config_accepts(screen):
    cfg = kiosk.load_config(_cfg(screen))
    assert cfg.screens[0].name == screen["name"]
    assert isinstance(cfg.screens[0].reload_minutes, float)


def test_load_config_host_map_normalised():
    cfg = kiosk.load_config(
        _cfg(host_map={"Host.Example": "2001:DB8:0:0::0001", "v4.example": "100.64.0.1", _LONG_HOST: "10.0.0.248"})
    )
    assert cfg.host_map == {"host.example": "2001:db8::1", "v4.example": "100.64.0.1", _LONG_HOST: "10.0.0.248"}


def test_load_config_timezone(zoneinfo):
    assert kiosk.load_config(_cfg(timezone="America/Chicago")).timezone == "America/Chicago"
    assert kiosk.load_config(_cfg(timezone="UTC")).timezone == "UTC"
    assert kiosk.load_config(_cfg(timezone=None)).timezone is None


def test_example_config_loads(zoneinfo):
    if not EXAMPLE.is_file():
        pytest.skip("kiosk.example.json not found")
    cfg = kiosk.load_config(kiosk.read_json(str(EXAMPLE)))
    assert [s.name for s in cfg.screens] == ["left", "right"]
    left, right = cfg.screens
    assert (left.output, left.rotate, left.login) == ("HDMI-0", "left", "grafana")
    assert (right.output, right.rotate, right.login, right.hide_nav) == ("HDMI-1", "right", "dashboard", True)
    assert cfg.timezone == "America/Chicago"
    for host, ip in cfg.host_map.items():
        assert host == host.lower()
        assert ip == str(ipaddress.ip_address(ip))
    # The example needs every secret make_logins.py writes.
    secrets = kiosk.load_secrets({"grafana_user": "u", "grafana_password": "p", "dashboard_session": "a" * 64}, cfg)
    assert set(secrets) == {"grafana_user", "grafana_password", "dashboard_session"}


def test_example_config_with_real_zoneinfo():
    if not EXAMPLE.is_file() or not os.path.isfile("/usr/share/zoneinfo/America/Chicago"):
        pytest.skip("kiosk.example.json or the system tzdata is missing")
    assert kiosk.load_config(kiosk.read_json(str(EXAMPLE))).timezone == "America/Chicago"


# ----------------------------------------------------------------------------------- load_secrets

GOOD_SESSION = "0123456789abcdef" * 4


def _cfg_with(*logins: str):
    screens = [_screen(name=f"s{i}", login=login) for i, login in enumerate(logins)]
    return kiosk.load_config({"screens": screens})


def test_load_secrets_only_what_the_logins_need():
    data = {"grafana_user": "u", "grafana_password": "p", "dashboard_session": GOOD_SESSION, "other": "x"}
    assert kiosk.load_secrets(data, _cfg_with("none")) == {}
    assert kiosk.load_secrets(data, _cfg_with("grafana")) == {"grafana_user": "u", "grafana_password": "p"}
    assert kiosk.load_secrets(data, _cfg_with("dashboard")) == {"dashboard_session": GOOD_SESSION}
    assert set(kiosk.load_secrets(data, _cfg_with("grafana", "dashboard"))) == {
        "grafana_user",
        "grafana_password",
        "dashboard_session",
    }


def test_load_secrets_none_login_needs_no_file_content():
    assert kiosk.load_secrets({}, _cfg_with("none", "none")) == {}


@pytest.mark.parametrize(
    ("logins", "data"),
    [
        pytest.param(("none",), [], id="not-object"),
        pytest.param(("grafana",), {"grafana_user": "u"}, id="grafana-password-missing"),
        pytest.param(("grafana",), {"grafana_user": "u", "grafana_password": ""}, id="grafana-password-empty"),
        pytest.param(("grafana",), {"grafana_user": 5, "grafana_password": "p"}, id="grafana-user-not-str"),
        pytest.param(("dashboard",), {}, id="session-missing"),
        pytest.param(("dashboard",), {"dashboard_session": GOOD_SESSION.upper()}, id="session-uppercase"),
        pytest.param(("dashboard",), {"dashboard_session": GOOD_SESSION[:63]}, id="session-63"),
        pytest.param(("dashboard",), {"dashboard_session": GOOD_SESSION + "0"}, id="session-65"),
        pytest.param(("dashboard",), {"dashboard_session": GOOD_SESSION + "\n"}, id="session-trailing-newline"),
        pytest.param(("dashboard",), {"dashboard_session": "g" * 64}, id="session-not-hex"),
        pytest.param(("dashboard",), {"dashboard_session": 1}, id="session-not-str"),
    ],
)
def test_load_secrets_rejects(logins, data):
    with pytest.raises(ConfigError) as err:
        kiosk.load_secrets(data, _cfg_with(*logins))
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, str) and len(value) > 2:
                assert value not in str(err.value)  # never echo a secret


# -------------------------------------------------------------------------------------- read_json


def test_read_json_ok(tmp_path):
    p = tmp_path / "k.json"
    p.write_text('{"a": [1, 2]}', encoding="utf-8")
    assert kiosk.read_json(str(p)) == {"a": [1, 2]}


def test_read_json_missing(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        kiosk.read_json(str(tmp_path / "nope.json"))


def test_read_json_directory(tmp_path):
    with pytest.raises(ConfigError, match="IsADirectoryError"):
        kiosk.read_json(str(tmp_path))


def test_read_json_bad_json_reports_line_not_content(tmp_path):
    p = tmp_path / "k.json"
    p.write_text('{\n"grafana_password": "hunter2-secret",\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="line") as err:
        kiosk.read_json(str(p))
    assert "hunter2" not in str(err.value)


def test_read_json_not_utf8(tmp_path):
    p = tmp_path / "k.json"
    p.write_bytes(b'{"a": "\xff\xfe"}')
    with pytest.raises(ConfigError, match="UnicodeDecodeError"):
        kiosk.read_json(str(p))


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads mode-000 files")
def test_read_json_unreadable(tmp_path):
    p = tmp_path / "k.json"
    p.write_text("{}", encoding="utf-8")
    p.chmod(0)
    with pytest.raises(ConfigError, match="not readable"):
        kiosk.read_json(str(p))


# ------------------------------------------------------------------------------------------ xrandr

XRANDR = """\
Screen 0: minimum 8 x 8, current 2160 x 1920, maximum 32767 x 32767
HDMI-0 connected primary 1080x1920+0+0 left (normal left inverted right x axis y axis) 527mm x 296mm
   1920x1080     60.00*+  59.94    50.00
   1680x1050     59.95
   1280x720      60.00    59.94    50.00
HDMI-1 connected 1080x1920+1080+0 right (normal left inverted right x axis y axis) 527mm x 296mm
   2560x1440     59.95
   1920x1080     60.00 +  59.94*   50.00
DP-0 disconnected (normal left inverted right x axis y axis)
DP-1 disconnected (normal left inverted right x axis y axis)
DP-5 unknown connection (normal left inverted right x axis y axis)
   1024x768      60.00 +
DP-2 connected (normal left inverted right x axis y axis) 600mm x 340mm
   3840x2160     60.00 +  30.00
   1920x1080i    60.00
"""


def test_parse_xrandr_outputs():
    outs = {o.name: o for o in kiosk.parse_xrandr(XRANDR)}
    assert list(outs) == ["HDMI-0", "HDMI-1", "DP-0", "DP-1", "DP-2"]  # no "Screen", no "unknown connection"
    h0, h1, dp0, dp1, dp2 = outs.values()
    assert (h0.connected, h0.geometry, h0.preferred, h0.first_mode) == (
        True,
        (1080, 1920, 0, 0),
        (1920, 1080),
        (1920, 1080),
    )
    assert (h1.connected, h1.geometry, h1.preferred, h1.first_mode) == (
        True,
        (1080, 1920, 1080, 0),
        (1920, 1080),
        (2560, 1440),
    )
    assert (dp0.connected, dp0.geometry, dp0.first_mode) == (False, None, None)
    # DP-5's "1024x768 +" follows DP-1 but belongs to DP-5, which isn't parsed.
    assert (dp1.connected, dp1.first_mode, dp1.preferred) == (False, None, None)
    assert (dp2.connected, dp2.geometry, dp2.preferred, dp2.first_mode) == (True, None, (3840, 2160), (3840, 2160))


def test_parse_xrandr_unknown_connection_modes_do_not_attach_to_previous_output():
    text = (
        "HDMI-0 connected 1920x1080+0+0 (normal left inverted right x axis y axis) 527mm x 296mm\n"
        "   1920x1080     60.00*\n"
        "DP-5 unknown connection (normal left inverted right x axis y axis)\n"
        "   1024x768      60.00 +\n"
    )
    (h0,) = kiosk.parse_xrandr(text)
    assert h0.preferred is None
    assert h0.first_mode == (1920, 1080)


def test_parse_xrandr_first_preferred_mode_wins():
    (o,) = kiosk.parse_xrandr("HDMI-0 connected (normal)\n   2560x1440     59.95 +\n   1920x1080     60.00 +\n")
    assert o.preferred == (2560, 1440)


def test_parse_xrandr_interlaced_and_empty():
    (o,) = kiosk.parse_xrandr("HDMI-0 connected (normal)\n   1920x1080i    60.00*\n")
    assert o.first_mode == (1920, 1080)
    assert o.geometry is None
    assert kiosk.parse_xrandr("") == []
    assert kiosk.parse_xrandr("Screen 0: minimum 8 x 8\n   1920x1080     60.00*+\n") == []


def _outs(*names_connected: tuple[str, bool]) -> list:
    return [Output(name=n, connected=c, preferred=(1920, 1080)) for n, c in names_connected]


def test_assign_outputs_named_first_then_free_in_xrandr_order():
    screens = (Screen(name="a", url="https://h/"), Screen(name="b", url="https://h/", output="HDMI-0"))
    assigned, unused, warnings = kiosk.assign_outputs(screens, _outs(("HDMI-0", True), ("HDMI-1", True)))
    # "a" comes first but must not take HDMI-0, which "b" names.
    assert [(s.name, o.name) for s, o in assigned] == [("a", "HDMI-1"), ("b", "HDMI-0")]
    assert unused == []
    assert warnings == []


def test_assign_outputs_named_output_not_connected():
    screens = (Screen(name="a", url="https://h/", output="DP-0"),)
    assigned, unused, warnings = kiosk.assign_outputs(screens, _outs(("DP-0", False), ("HDMI-0", True)))
    assert [(s.name, o.name) for s, o in assigned] == [("a", "HDMI-0")]
    assert len(warnings) == 1
    assert "DP-0 is not connected" in warnings[0]


def test_assign_outputs_more_screens_than_monitors():
    screens = tuple(Screen(name=n, url="https://h/") for n in ("a", "b", "c"))
    assigned, unused, warnings = kiosk.assign_outputs(screens, _outs(("HDMI-0", True), ("HDMI-1", True)))
    assert [(s.name, o.name) for s, o in assigned] == [("a", "HDMI-0"), ("b", "HDMI-1")]
    assert warnings == ["screen c: no free connected output; screen skipped"]


def test_assign_outputs_unused_in_xrandr_order():
    screens = (Screen(name="a", url="https://h/", output="HDMI-1"),)
    outputs = _outs(("DP-0", False), ("HDMI-0", True), ("HDMI-1", True), ("DP-2", True))
    assigned, unused, warnings = kiosk.assign_outputs(screens, outputs)
    assert [o.name for _, o in assigned] == ["HDMI-1"]
    assert unused == ["HDMI-0", "DP-2"]  # disconnected DP-0 is not listed


def test_assign_outputs_nothing_connected():
    screens = (Screen(name="a", url="https://h/", output="HDMI-0"), Screen(name="b", url="https://h/"))
    assigned, unused, warnings = kiosk.assign_outputs(screens, _outs(("HDMI-0", False)))
    assert assigned == []
    assert unused == []
    assert len(warnings) == 3  # a: not connected + skipped; b: skipped


def test_assign_outputs_keeps_screen_order():
    screens = (Screen(name="b", url="https://h/", output="DP-2"), Screen(name="a", url="https://h/"))
    assigned, _, _ = kiosk.assign_outputs(screens, _outs(("HDMI-0", True), ("DP-2", True)))
    assert [(s.name, o.name) for s, o in assigned] == [("b", "DP-2"), ("a", "HDMI-0")]


@pytest.mark.parametrize(
    ("rotate", "expected"),
    [("normal", (1920, 1080)), ("inverted", (1920, 1080)), ("left", (1080, 1920)), ("right", (1080, 1920))],
)
def test_rotated_size(rotate, expected):
    assert kiosk.rotated_size((1920, 1080), rotate) == expected


def _closet_layout():
    screens = (
        Screen(name="left", url="https://h/", output="HDMI-0", rotate="left"),
        Screen(name="right", url="https://h/", output="HDMI-1", rotate="right"),
    )
    return kiosk.assign_outputs(screens, kiosk.parse_xrandr(XRANDR))


def test_xrandr_command_closet_layout():
    assigned, unused, _ = _closet_layout()
    assert unused == ["DP-2"]
    assert kiosk.xrandr_command(assigned, unused) == [
        "xrandr",
        "--output",
        "HDMI-0",
        "--auto",
        "--rotate",
        "left",
        "--pos",
        "0x0",
        "--output",
        "HDMI-1",
        "--auto",
        "--rotate",
        "right",
        "--pos",
        "1080x0",
        "--output",
        "DP-2",
        "--off",
    ]


def test_planned_geometry_matches_xrandr_positions():
    assigned, unused, _ = _closet_layout()
    plan = kiosk.planned_geometry(assigned)
    assert plan == {"left": (1080, 1920, 0, 0), "right": (1080, 1920, 1080, 0)}
    cmd = kiosk.xrandr_command(assigned, unused)
    positions = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--pos"]
    assert positions == [f"{x}x{y}" for _, _, x, y in plan.values()]


def test_layout_prefers_preferred_mode_then_first_mode_then_1080p():
    a = Screen(name="a", url="https://h/")
    b = Screen(name="b", url="https://h/", rotate="inverted")
    c = Screen(name="c", url="https://h/")
    assigned = [
        (a, Output(name="DP-0", connected=True, preferred=(2560, 1440), first_mode=(3840, 2160))),
        (b, Output(name="DP-1", connected=True, first_mode=(1280, 720))),
        (c, Output(name="DP-2", connected=True)),
    ]
    assert kiosk.planned_geometry(assigned) == {
        "a": (2560, 1440, 0, 0),
        "b": (1280, 720, 2560, 0),
        "c": (1920, 1080, 3840, 0),
    }
    cmd = kiosk.xrandr_command(assigned, [])
    assert [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--pos"] == ["0x0", "2560x0", "3840x0"]


def test_setup_display_runs_xrandr_and_reads_back_geometry(monkeypatch, caplog):
    after = XRANDR.replace("1080x1920+1080+0", "1080x1920+1090+0")  # X put "right" 10 px further
    commands = []

    def fake_run(cmd, timeout=15):
        commands.append(cmd)
        if cmd == ["xrandr", "--query"]:
            return XRANDR
        if cmd == ["xrandr", "--current"]:
            return after.replace("HDMI-0 connected primary 1080x1920+0+0", "HDMI-0 connected primary")
        return ""

    monkeypatch.setattr(kiosk, "_run", fake_run)
    cfg = kiosk.load_config(
        _cfg(
            _screen(name="left", output="HDMI-0", rotate="left"),
            _screen(name="right", output="HDMI-1", rotate="right"),
        )
    )
    caplog.set_level(logging.INFO, logger="jupiter-kiosk")
    placed = kiosk.setup_display(cfg)
    # HDMI-0 reports no geometry after the change: the plan is used; HDMI-1 reports where it really is.
    assert [(s.name, g) for s, g in placed] == [("left", (1080, 1920, 0, 0)), ("right", (1080, 1920, 1090, 0))]
    assert commands[1][0] == "xrandr" and "--off" in commands[1]
    assert ["xset", "s", "off", "s", "noblank", "-dpms"] in commands
    assert "https://" not in caplog.text


def test_setup_display_without_monitor(monkeypatch):
    monkeypatch.setattr(kiosk, "_run", lambda cmd, timeout=15: "HDMI-0 disconnected (normal)\n")
    with pytest.raises(ConfigError, match="no connected monitor"):
        kiosk.setup_display(kiosk.load_config(_cfg()))


def test_connected_outputs_probe_flag(monkeypatch):
    seen = []

    def fake_run(cmd, timeout=15):
        seen.append(cmd)
        return XRANDR

    monkeypatch.setattr(kiosk, "_run", fake_run)
    assert kiosk.connected_outputs() == frozenset({"HDMI-0", "HDMI-1", "DP-2"})
    assert kiosk.connected_outputs(probe=False) == frozenset({"HDMI-0", "HDMI-1", "DP-2"})
    assert seen == [["xrandr", "--query"], ["xrandr", "--current"]]


def test_wait_for_monitor_polls_until_connected(monkeypatch, caplog):
    answers = iter([frozenset(), frozenset(), frozenset({"HDMI-0"})])
    monkeypatch.setattr(kiosk, "connected_outputs", lambda probe=True: next(answers))
    monkeypatch.setattr(kiosk, "NO_MONITOR_SECONDS", 0.01)
    caplog.set_level(logging.INFO, logger="jupiter-kiosk")
    assert kiosk.wait_for_monitor(threading.Event()) == frozenset({"HDMI-0"})
    assert caplog.text.count("no monitor connected") == 1  # warned once, not per poll
    assert "monitor connected; starting" in caplog.text


def test_wait_for_monitor_stops():
    stop = threading.Event()
    stop.set()
    assert kiosk.wait_for_monitor(stop) is None


# ---------------------------------------------------------------- X server (jupiter-kiosk-x.service)


@pytest.mark.parametrize(
    "display, expected",
    [
        (":0", 0),
        (":0.0", 0),
        (":12", 12),
        (":1.2", 1),
        ("", None),
        ("host:0", None),
        ("unix:0", None),
        (":", None),
        (":0.", None),
        (":1234", None),
        ("/tmp/.X11-unix/X0", None),
        (" :0", None),
        (":0\n", None),
    ],
)
def test_display_number(display, expected):
    if expected is None:
        with pytest.raises(ConfigError, match="local display"):
            kiosk.display_number(display)
    else:
        assert kiosk.display_number(display) == expected


def test_hold_x_abstract_name_refuses_connects_and_second_holders():
    n = _free_display()
    held = kiosk.hold_x_abstract_name(n)
    try:
        assert held.type == socket.SOCK_STREAM  # what xcb connects with; abstract names are per type
        assert not held.get_inheritable()  # Chrome doesn't get it
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with client, pytest.raises(ConnectionRefusedError):
            client.connect(f"\0/tmp/.X11-unix/X{n}")
        with pytest.raises(OSError) as e:
            kiosk.hold_x_abstract_name(n)
        assert e.value.errno == errno.EADDRINUSE
    finally:
        held.close()
    kiosk.hold_x_abstract_name(n).close()  # free again once closed


def test_wait_for_x_polls_until_x_answers(monkeypatch):
    calls = []

    def fake_run(cmd, timeout=15):
        calls.append(cmd)
        if len(calls) < 3:
            raise kiosk.subprocess.CalledProcessError(1, cmd)
        return ""

    monkeypatch.setattr(kiosk, "_run", fake_run)
    stop = _Proxy(threading.Event(), wait=lambda t: False)  # no real sleeping
    assert kiosk.wait_for_x(stop) is True
    assert calls == [["xrandr", "--current"]] * 3


_XRANDR_NO_AUTH = "Authorization required, but no authorization protocol specified\n\nCan't open display :0\n"


@pytest.mark.parametrize(
    "error, kind",
    [
        (FileNotFoundError("xrandr"), "xrandr not installed"),
        (kiosk.subprocess.TimeoutExpired(["xrandr"], 10), "xrandr hung"),
        (kiosk.subprocess.CalledProcessError(1, ["xrandr"], "", "Can't open display :0\n"), "no X server"),
        (kiosk.subprocess.CalledProcessError(1, ["xrandr"], "", _XRANDR_NO_AUTH), "refused the auth cookie"),
        (kiosk.subprocess.CalledProcessError(1, ["xrandr"], "", "No protocol specified\n"), "refused the auth cookie"),
        (kiosk.subprocess.CalledProcessError(1, ["xrandr"]), "no X server"),
        (PermissionError("xrandr"), "PermissionError"),
    ],
)
def test_wait_for_x_gives_up_and_says_why(monkeypatch, caplog, error, kind):
    def fake_run(cmd, timeout=15):
        raise error

    monkeypatch.setattr(kiosk, "_run", fake_run)
    assert kiosk.wait_for_x(threading.Event(), timeout=0) is False
    assert "didn't answer within 0 s" in caplog.text
    assert kind in caplog.text
    assert "Can't open display" not in caplog.text  # our words, not xrandr's output


def test_wait_for_x_stops():
    stop = threading.Event()
    stop.set()
    assert kiosk.wait_for_x(stop) is False


UNIX_HEADER = "Num       RefCount Protocol Flags    Type St Inode Path\n"
# What the host network namespace showed with X in it (2026-11-24), plus an unnamed socket and our
# own held name.
UNIX_EXPOSED = UNIX_HEADER + (
    "0000000000000000: 00000002 00000000 00010000 0001 01 1142426 /tmp/.X11-unix/X0\n"
    "0000000000000000: 00000003 00000000 00000000 0001 03 1148277 /tmp/.X11-unix/X0\n"
    "0000000000000000: 00000002 00000000 00000000 0002 01 1141393 @tmp/nvidia-xdriver-5927123a@@@@@@@@@@\n"
    "0000000000000000: 00000002 00000000 00000000 0002 01 1141394 /tmp/nvidia-xdriver-5927123a\n"
    "0000000000000000: 00000003 00000000 00000000 0001 03 1151127\n"
    "0000000000000000: 00000002 00000000 00010000 0001 01 1234 @/tmp/.X11-unix/X0\n"
    "0000000000000000: 00000003 00000000 00000000 0001 03 15893 /run/systemd/journal/stdout\n"
)


def test_unix_sockets():
    sockets = kiosk.unix_sockets(UNIX_EXPOSED)
    assert sockets[0] == ("01", "/tmp/.X11-unix/X0")
    assert ("03", "/tmp/.X11-unix/X0") in sockets
    assert ("01", "@/tmp/.X11-unix/X0") in sockets
    assert len(sockets) == 6  # the unnamed socket has no path; the header isn't a socket


def test_visible_x_sockets_finds_x_but_not_our_held_name():
    assert kiosk.visible_x_sockets(kiosk.unix_sockets(UNIX_EXPOSED), 0) == [
        "/tmp/.X11-unix/X0",
        "/tmp/nvidia-xdriver-5927123a",
        "@tmp/nvidia-xdriver-5927123a@@@@@@@@@@",
    ]
    assert kiosk.visible_x_sockets([("01", "/tmp/.X11-unix/X1")], 0) == []  # another display's server


# The host network namespace with X in its own (2026-11-24): X's ends of the kiosk's connections
# carry the listener's path (the kernel makes them in the client's namespace), and the held name.
UNIX_ISOLATED = UNIX_HEADER + (
    "0000000000000000: 00000003 00000000 00000000 0001 03 5719952 /tmp/.X11-unix/X0\n"
    "0000000000000000: 00000003 00000000 00000000 0001 03 5731615 /tmp/.X11-unix/X0\n"
    "0000000000000000: 00000002 00000000 00000000 0001 01 1234 @/tmp/.X11-unix/X0\n"
    "0000000000000000: 00000003 00000000 00000000 0001 03 3008124\n"
    "0000000000000000: 00000002 00000000 00010000 0001 01 15893 /run/systemd/journal/stdout\n"
)


def test_visible_x_sockets_ignores_x_ends_of_our_own_connections():
    assert kiosk.visible_x_sockets(kiosk.unix_sockets(UNIX_ISOLATED), 0) == []


NET_DEV_HEAD = (
    "Inter-|   Receive                                                |  Transmit\n"
    " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
)


def test_interface_names():
    text = NET_DEV_HEAD + "    lo:  120 2 0 0 0 0 0 0  120 2 0 0 0 0 0 0\n  enp7s0: 9 9 0 0 0 0 0 0 9 9 0 0 0 0 0 0\n"
    assert kiosk.interface_names(text) == ["lo", "enp7s0"]


@pytest.mark.parametrize(
    "interfaces, expected",
    # Every network namespace has lo: an empty list means the file didn't parse, so not private.
    [(["lo"], True), ([], False), (["lo", "enp7s0"], False), (["lo", "tailscale0", "docker0"], False)],
)
def test_private_network(tmp_path, interfaces, expected):
    dev = tmp_path / "dev"
    dev.write_text(NET_DEV_HEAD + "".join(f"  {i}: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n" for i in interfaces), "utf-8")
    assert kiosk.private_network(str(dev)) is expected


def test_private_network_unreadable_is_not_private(tmp_path):
    assert kiosk.private_network(str(tmp_path / "missing")) is False


def test_private_network_reads_net_dev_at_call_time(tmp_path, monkeypatch):
    assert kiosk.NET_DEV == "/proc/self/net/dev"
    dev = tmp_path / "dev"
    dev.write_text(NET_DEV_HEAD + "  lo: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n", "utf-8")
    monkeypatch.setattr(kiosk, "NET_DEV", str(dev))
    assert kiosk.private_network() is True
    dev.write_text(NET_DEV_HEAD + "  lo: 0\n  enp7s0: 0\n", "utf-8")
    assert kiosk.private_network() is False


# ------------------------------------------------------------------------------------- preflight

XORG_MIN = 'Section "Device"\n    Identifier "display-5080"\n    Driver "nvidia"\n    BusID "PCI:12:0:0"\nEndSection\n'


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(XORG_MIN, "0000:0c:00.0", id="bus-12"),
        pytest.param('BusID "PCI:12@1:0:0"', "0001:0c:00.0", id="with-domain"),
        pytest.param('  BusID   "PCI:1:31:7"', "0000:01:1f.7", id="dev-fn"),
        pytest.param('BusID "PCI:255:0:0"', "0000:ff:00.0", id="bus-255"),
        pytest.param('# BusID "PCI:11:0:0"\nBusID "PCI:12:0:0"\n', "0000:0c:00.0", id="commented-line-ignored"),
        pytest.param('BusID "PCI:12:0:0"\nBusID "PCI:12:0:0"\n', None, id="two-busids-same"),
        pytest.param('BusID "PCI:12:0:0"\nBusID "PCI:11:0:0"\n', None, id="two-busids"),
        pytest.param('Section "Device"\n  Driver "nvidia"\nEndSection\n', None, id="no-busid"),
        pytest.param('BusID "PCI:0c:00:0"', None, id="hex-not-decimal"),
        pytest.param("", None, id="empty"),
    ],
)
def test_xorg_bus_id(text, expected):
    assert kiosk.xorg_bus_id(text) == expected


def test_shipped_xorg_conf_pins_bus_0c():
    if not XORG_SHIPPED.is_file():
        pytest.skip("jupiter-kiosk.xorg.conf not found")
    assert kiosk.xorg_bus_id(XORG_SHIPPED.read_text(encoding="utf-8")) == DISPLAY_PCI


@pytest.mark.parametrize(
    "second",
    [
        pytest.param('BusId "PCI:11:0:0"', id="BusId"),
        pytest.param('busid "PCI:11:0:0"', id="busid"),
        pytest.param('Bus_ID "PCI:11:0:0"', id="Bus_ID"),
    ],
)
def test_xorg_bus_id_counts_every_spelling_of_busid(second):
    text = XORG_MIN + f'Section "Device"\n    Identifier "other"\n    {second}\nEndSection\n'
    assert kiosk.xorg_bus_id(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(f"CUDA_VISIBLE_DEVICES={U_VOICE}\n", [U_VOICE.lower()], id="plain"),
        pytest.param(f"CUDA_VISIBLE_DEVICES='{U_VOICE}'\n", [U_VOICE.lower()], id="single-quoted"),
        pytest.param(f'CUDA_VISIBLE_DEVICES="{U_VOICE}"\n', [U_VOICE.lower()], id="double-quoted"),
        pytest.param(f"  CUDA_VISIBLE_DEVICES = {U_VOICE}  \n", [U_VOICE.lower()], id="spaces"),
        pytest.param(
            f'CUDA_VISIBLE_DEVICES="{U_VOICE}, {U_DISPLAY}"\n', [U_VOICE.lower(), U_DISPLAY.lower()], id="list"
        ),
        pytest.param(f"CUDA_VISIBLE_DEVICES={U_VOICE.upper()}\n", [U_VOICE.lower()], id="upper"),
        pytest.param(
            f"CUDA_VISIBLE_DEVICES={U_VOICE}\nCUDA_VISIBLE_DEVICES={U_DISPLAY}\n", [U_DISPLAY.lower()], id="last-wins"
        ),
        pytest.param(f"CUDA_VISIBLE_DEVICES={U_VOICE}\nCUDA_VISIBLE_DEVICES=1\n", None, id="last-is-index"),
        pytest.param(
            f"CUDA_VISIBLE_DEVICES={U_VOICE}\n# CUDA_VISIBLE_DEVICES=1\n", [U_VOICE.lower()], id="comment-ignored"
        ),
        pytest.param(f"# CUDA_VISIBLE_DEVICES={U_VOICE}\n", None, id="only-a-comment"),
        pytest.param(f"export CUDA_VISIBLE_DEVICES={U_VOICE}\n", None, id="export"),
        pytest.param(
            f"CUDA_VISIBLE_DEVICES={U_VOICE}\nexport CUDA_VISIBLE_DEVICES={U_VOICE}\n", None, id="export-last"
        ),
        pytest.param("CUDA_VISIBLE_DEVICES=1\n", None, id="index"),
        pytest.param("CUDA_VISIBLE_DEVICES=0,1\n", None, id="index-list"),
        pytest.param(f"CUDA_VISIBLE_DEVICES={U_VOICE},1\n", None, id="uuid-and-index"),
        pytest.param(f"CUDA_VISIBLE_DEVICES={U_VOICE},\n", None, id="trailing-comma"),
        pytest.param("CUDA_VISIBLE_DEVICES=\n", None, id="empty-value"),
        pytest.param("CUDA_VISIBLE_DEVICES=''\n", None, id="quoted-empty"),
        pytest.param(f"CUDA_VISIBLE_DEVICES='{U_VOICE}\n", None, id="mismatched-quote"),
        pytest.param(f"CUDA_VISIBLE_DEVICES={U_VOICE[:-1]}\n", None, id="short-uuid"),
        pytest.param(
            f"NOT_CUDA_VISIBLE_DEVICES={U_VOICE}\nCUDA_VISIBLE_DEVICES_OLD={U_VOICE}\n", None, id="other-vars"
        ),
        pytest.param("", None, id="empty-file"),
    ],
)
def test_voice_pins(text, expected):
    assert kiosk.voice_pins(text) == expected


def _nvidia_info(uuid: str, minor: int, bus: str) -> str:
    return (
        "Model: \t\t NVIDIA GeForce RTX 5080\n"
        "IRQ:   \t\t 180\n"
        f"GPU UUID: \t {uuid}\n"
        "Video BIOS: \t 98.03.4b.00.2c\n"
        "Bus Type: \t PCIe\n"
        "DMA Size: \t 52 bits\n"
        "DMA Mask: \t 0xfffffffffffff\n"
        f"Bus Location: \t {bus}\n"
        f"Device Minor: \t {minor}\n"
        "GPU Firmware: \t 580.95.05\n"
        "GPU Excluded:\t No\n"
    )


def test_parse_nvidia_information():
    assert kiosk.parse_nvidia_information(_nvidia_info(U_DISPLAY, 1, DISPLAY_PCI)) == (U_DISPLAY.lower(), 1)
    assert kiosk.parse_nvidia_information("Model: x\n") == (None, None)
    assert kiosk.parse_nvidia_information(f"GPU UUID: {U_VOICE}x\nDevice Minor: 0\n") == (None, 0)


def _fake_proc(root: Path, gpus) -> Path:
    for pci, uuid, minor in gpus:
        (root / pci).mkdir(parents=True)
        (root / pci / "information").write_text(_nvidia_info(uuid, minor, pci), encoding="utf-8")
    return root


def test_nvidia_gpus(tmp_path):
    root = _fake_proc(tmp_path / "gpus", [(VOICE_PCI, U_VOICE, 0), ("0000:0C:00.0", U_DISPLAY, 1)])
    (root / "0000:0d:00.0").mkdir()  # no information file
    (root / "0000:0e:00.0").mkdir()
    (root / "0000:0e:00.0" / "information").write_text("Model: broken\n", encoding="utf-8")
    assert kiosk.nvidia_gpus(str(root)) == {VOICE_PCI: (U_VOICE.lower(), 0), DISPLAY_PCI: (U_DISPLAY.lower(), 1)}
    assert kiosk.nvidia_gpus(str(tmp_path / "missing")) == {}


def test_device_nodes(tmp_path):
    for name in ("card1", "controlD65", "renderD129"):
        (tmp_path / DISPLAY_PCI / "drm" / name).mkdir(parents=True)
    assert kiosk.device_nodes(DISPLAY_PCI, 1, sysfs=str(tmp_path)) == [
        "/dev/nvidia1",
        "/dev/dri/card1",
        "/dev/dri/renderD129",
    ]
    assert kiosk.device_nodes(VOICE_PCI, 0, sysfs=str(tmp_path)) == ["/dev/nvidia0"]  # no drm dir


def test_is_blocked_real_paths(tmp_path):
    f = tmp_path / "f"
    f.write_text("x")
    assert kiosk.is_blocked(str(tmp_path / "missing")) is True
    assert kiosk.is_blocked(str(f)) is False
    assert kiosk.is_blocked(str(tmp_path)) is False
    if os.path.exists("/dev/null") and stat.S_ISCHR(os.stat("/dev/null").st_mode):
        assert kiosk.is_blocked("/dev/null") is False  # char device 1:3


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        pytest.param(SimpleNamespace(st_mode=stat.S_IFCHR, st_rdev=0), True, id="chr-0-0-inaccessible"),
        pytest.param(SimpleNamespace(st_mode=stat.S_IFCHR | 0o660, st_rdev=os.makedev(195, 1)), False, id="chr-nvidia"),
        pytest.param(SimpleNamespace(st_mode=stat.S_IFREG, st_rdev=0), False, id="regular-rdev-0"),
        pytest.param(PermissionError(13, "denied"), False, id="permission-error"),
    ],
)
def test_is_blocked_mocked_stat(monkeypatch, result, expected):
    def fake_stat(path):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(kiosk, "os", _Proxy(os, stat=fake_stat))
    assert kiosk.is_blocked("/dev/dri/card1") is expected


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A fake Jupiter: voice card on bus 0b (minor 0), display card on bus 0c (minor 1)."""
    xorg = tmp_path / "jupiter-kiosk.conf"
    xorg.write_text(XORG_MIN, encoding="utf-8")
    proc = _fake_proc(tmp_path / "gpus", [(VOICE_PCI, U_VOICE, 0), (DISPLAY_PCI, U_DISPLAY, 1)])
    sysfs = tmp_path / "pci"
    for pci, names in ((VOICE_PCI, ("card0", "renderD128")), (DISPLAY_PCI, ("card1", "renderD129"))):
        for name in names:
            (sysfs / pci / "drm" / name).mkdir(parents=True)
    voice = tmp_path / "voxcpm-tts.env"
    voice.write_text(f"VOXCPM_PORT=8004\nCUDA_VISIBLE_DEVICES={U_VOICE}\n", encoding="utf-8")
    chrome = tmp_path / "chrome"
    chrome.write_text("#!/bin/sh\n", encoding="utf-8")
    chrome.chmod(0o755)
    monkeypatch.setattr(kiosk, "XORG_CONF", str(xorg))
    monkeypatch.setattr(kiosk, "VOICE_ENV", str(voice))
    monkeypatch.setattr(kiosk, "NVIDIA_PROC", str(proc))
    monkeypatch.setattr(kiosk, "PCI_SYSFS", str(sysfs))
    # nvidia_gpus()/device_nodes() took NVIDIA_PROC/PCI_SYSFS as default values when the module
    # loaded, so patching the constants alone doesn't redirect them.
    monkeypatch.setattr(kiosk.nvidia_gpus, "__defaults__", (str(proc),))
    monkeypatch.setattr(kiosk.device_nodes, "__defaults__", (str(sysfs),))
    blocked = {"/dev/nvidia0", "/dev/dri/card0", "/dev/dri/renderD128"}  # the unit's InaccessiblePaths=
    monkeypatch.setattr(kiosk, "is_blocked", lambda path: path in blocked)
    cfg = kiosk.load_config({"chrome": str(chrome), "screens": [_screen()]})
    return SimpleNamespace(xorg=xorg, voice=voice, proc=proc, chrome=chrome, blocked=blocked, cfg=cfg)


def _preflight(host, caplog) -> int:
    caplog.set_level(logging.INFO, logger="jupiter-kiosk")
    return kiosk.preflight(host.cfg)


def test_preflight_ok(host, caplog):
    assert _preflight(host, caplog) == 0
    assert "preflight ok: display on 0000:0c:00.0" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_preflight_x_server_needs_a_private_network(host, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="jupiter-kiosk")
    monkeypatch.setattr(kiosk, "private_network", lambda: False)
    assert kiosk.preflight(host.cfg, x_server=True) == 1
    assert "not in a private network namespace" in caplog.text
    assert "preflight ok" not in caplog.text
    caplog.clear()
    assert kiosk.preflight(host.cfg) == 0  # the browser unit keeps the host network: not checked there


def test_preflight_x_server_ok_in_a_private_network(host, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="jupiter-kiosk")
    monkeypatch.setattr(kiosk, "private_network", lambda: True)
    assert kiosk.preflight(host.cfg, x_server=True) == 0
    assert "preflight ok" in caplog.text


def test_preflight_x_server_still_checks_the_gpu(host, caplog, monkeypatch):
    monkeypatch.setattr(kiosk, "private_network", lambda: True)
    host.voice.write_text(f"CUDA_VISIBLE_DEVICES={U_DISPLAY}\n", encoding="utf-8")
    assert kiosk.preflight(host.cfg, x_server=True) == 1
    assert "is a voice card" in caplog.text


def test_preflight_warns_when_voice_card_is_reachable(host, caplog):
    host.blocked.clear()
    assert _preflight(host, caplog) == 0
    assert "voice card's device files are reachable" in caplog.text
    assert "/dev/nvidia0" in caplog.text


@pytest.mark.parametrize(
    "voice_env",
    [
        pytest.param(f"CUDA_VISIBLE_DEVICES={U_DISPLAY}\n", id="display-is-the-voice-card"),
        pytest.param(f"CUDA_VISIBLE_DEVICES={U_VOICE},{U_DISPLAY}\n", id="display-among-voice-cards"),
        pytest.param(f"CUDA_VISIBLE_DEVICES={U_DISPLAY.upper()}\n", id="display-pin-uppercase"),
        pytest.param("VOXCPM_PORT=8004\n", id="no-pin"),
        pytest.param("CUDA_VISIBLE_DEVICES=1\n", id="index-pin"),
        pytest.param(f"export CUDA_VISIBLE_DEVICES={U_VOICE}\n", id="export-pin"),
        pytest.param("", id="empty-env-file"),
    ],
)
def test_preflight_refuses_on_voice_pin(host, caplog, voice_env):
    host.voice.write_text(voice_env, encoding="utf-8")
    assert _preflight(host, caplog) == 1
    assert "preflight ok" not in caplog.text


def test_preflight_voice_env_missing_warns_and_passes(host, caplog):
    host.voice.unlink()
    assert _preflight(host, caplog) == 0
    assert "not found (voice not installed here?)" in caplog.text


def test_preflight_voice_env_unreadable(host, caplog):
    host.voice.unlink()
    host.voice.mkdir()  # IsADirectoryError: an OSError that isn't "not found"
    assert _preflight(host, caplog) == 1
    assert "unreadable" in caplog.text


def test_preflight_display_nodes_hidden(host, caplog):
    host.blocked.add("/dev/dri/renderD129")
    assert _preflight(host, caplog) == 1
    assert "display card's device files are hidden (/dev/dri/renderD129)" in caplog.text


@pytest.mark.parametrize(
    "xorg",
    [
        pytest.param('Section "Device"\n    Driver "nvidia"\nEndSection\n', id="no-busid"),
        pytest.param(XORG_MIN + 'BusID "PCI:11:0:0"\n', id="two-busids"),
    ],
)
def test_preflight_needs_one_busid(host, caplog, xorg):
    host.xorg.write_text(xorg, encoding="utf-8")
    assert _preflight(host, caplog) == 1
    assert "exactly one BusID" in caplog.text


def test_preflight_xorg_conf_missing(host, caplog):
    host.xorg.unlink()
    assert _preflight(host, caplog) == 1
    assert "missing or unreadable" in caplog.text


def test_preflight_no_gpu_at_busid(host, caplog):
    host.xorg.write_text(XORG_MIN.replace("PCI:12:0:0", "PCI:13:0:0"), encoding="utf-8")
    assert _preflight(host, caplog) == 1
    assert "no GPU at 0000:0d:00.0" in caplog.text


def test_preflight_no_gpus(host, caplog):
    for info in host.proc.glob("*/information"):
        info.unlink()
    assert _preflight(host, caplog) == 1
    assert "no NVIDIA GPUs" in caplog.text


def test_preflight_chrome_not_executable(host, caplog):
    host.chrome.chmod(0o644)
    assert _preflight(host, caplog) == 1
    assert "chrome not found or not executable" in caplog.text


def test_preflight_chrome_missing(host, caplog):
    host.chrome.unlink()
    assert _preflight(host, caplog) == 1


# --------------------------------------------------------------------------------- split_messages


def test_split_messages_complete_and_partial():
    buf = bytearray(b'{"id":1,"result":{}}\0{"method":"Inspector.targetCrashed"}\0{"id":2,')
    assert kiosk.split_messages(buf, 0) == [{"id": 1, "result": {}}, {"method": "Inspector.targetCrashed"}]
    assert bytes(buf) == b'{"id":2,'
    start = len(buf)
    buf += b'"result":{"a":1}}\0'
    assert kiosk.split_messages(buf, start) == [{"id": 2, "result": {"a": 1}}]
    assert buf == bytearray()


def test_split_messages_skips_empty_frames():
    buf = bytearray(b'\0\0{"a":1}\0\0')
    assert kiosk.split_messages(buf) == [{"a": 1}]
    assert buf == bytearray()


def test_split_messages_one_byte_at_a_time():
    messages = [{"id": 1, "result": {"x": "a\u00e9\u2028"}}, {"method": "Target.detachedFromTarget"}, {"id": 2}]
    stream = b"\0".join(json.dumps(m).encode() for m in messages) + b"\0"
    buf = bytearray()
    got = []
    for i in range(len(stream)):
        start = len(buf)
        buf += stream[i : i + 1]
        got += kiosk.split_messages(buf, start)
    assert got == messages
    assert buf == bytearray()


def test_split_messages_bad_json_raises_value_error():
    # The reader treats ValueError as a broken connection.
    with pytest.raises(ValueError):
        kiosk.split_messages(bytearray(b"{bad\0"))


# ---------------------------------------------------------------------------------- page decisions

NOW = 10_000.0
GRAFANA = Screen(name="left", url="https://host.example:3000/d/jess-glance?kiosk", login="grafana", reload_minutes=5)
PLAIN = Screen(name="plain", url="https://host.example/d", reload_minutes=5)
DASH = Screen(name="right", url="https://Host.Example/dashboard", login="dashboard", reload_minutes=0)
SCREENS = {"grafana": GRAFANA, "plain": PLAIN, "dash": DASH}
G = "https://host.example:3000"


@pytest.mark.parametrize(
    ("screen", "href", "status", "since_login", "since_retry", "since_load", "expected"),
    [
        pytest.param("grafana", G + "/d/jess-glance", 200, 1e9, 1e9, 0, "none", id="up"),
        pytest.param("grafana", G + "/d/jess-glance", 0, 1e9, 1e9, 0, "none", id="status-unknown-is-up"),
        pytest.param("grafana", G + "/d/jess-glance", 499, 1e9, 1e9, 0, "none", id="4xx-is-up"),
        pytest.param("grafana", G + "/d/jess-glance", 500, 1e9, 1e9, 0, "retry", id="500"),
        pytest.param("grafana", G + "/d/jess-glance", 502, 1e9, 1e9, 0, "retry", id="502"),
        pytest.param("grafana", "https://other.example:3000/d", 200, 1e9, 1e9, 0, "retry", id="wrong-host"),
        pytest.param("grafana", "https://host.example/d", 200, 1e9, 1e9, 0, "retry", id="wrong-port"),
        pytest.param("grafana", "http://host.example:3000/d", 200, 1e9, 1e9, 0, "retry", id="wrong-scheme"),
        pytest.param("grafana", "chrome-error://chromewebdata/", 0, 1e9, 1e9, 0, "retry", id="chrome-error"),
        pytest.param("grafana", "about:blank", 0, 1e9, 1e9, 0, "retry", id="about-blank"),
        pytest.param("grafana", "", 0, 1e9, 1e9, 0, "retry", id="empty-href"),
        pytest.param("grafana", "chrome-error://chromewebdata/", 0, 1e9, 29, 0, "none", id="retry-rate-limited"),
        pytest.param("grafana", "chrome-error://chromewebdata/", 0, 1e9, 30, 0, "retry", id="retry-at-30s"),
        pytest.param("grafana", G + "/login", 200, 1e9, 1e9, 0, "login", id="login"),
        pytest.param("grafana", G + "/login?redirect=%2Fd%2Fx", 200, 1e9, 1e9, 0, "login", id="login-query"),
        pytest.param("grafana", G + "/login/generic_oauth", 200, 1e9, 1e9, 0, "login", id="login-subpath"),
        pytest.param("grafana", G + "/loginx", 200, 1e9, 1e9, 0, "none", id="loginx-not-login"),
        pytest.param("grafana", G + "/d/login", 200, 1e9, 1e9, 0, "none", id="nested-login-not-login"),
        pytest.param("grafana", G + "/login", 200, 299, 1e9, 0, "none", id="login-rate-limited"),
        pytest.param("grafana", G + "/login", 200, 300, 1e9, 0, "login", id="login-at-5min"),
        pytest.param("grafana", G + "/login", 502, 1e9, 1e9, 0, "retry", id="login-page-5xx-retries"),
        pytest.param("grafana", "https://other.example/login", 200, 1e9, 1e9, 0, "retry", id="foreign-login-page"),
        pytest.param("grafana", G + "/login", 200, 1e9, 1e9, 1e9, "login", id="login-beats-reload"),
        pytest.param("grafana", G + "/login", 200, 10, 1e9, 1e9, "none", id="login-page-no-reload"),
        pytest.param("grafana", "https://HOST.EXAMPLE:3000/login", 200, 1e9, 1e9, 0, "login", id="host-case"),
        pytest.param("dash", "https://HOST.example:443/login", 200, 1e9, 1e9, 0, "login", id="dash-host-443"),
        pytest.param("plain", "https://host.example/login", 200, 1e9, 1e9, 0, "none", id="no-login-configured"),
        pytest.param("plain", "https://host.example/login", 200, 1e9, 1e9, 300, "reload", id="no-login-reloads"),
        pytest.param("plain", "https://host.example/d", 200, 1e9, 1e9, 300, "reload", id="reload-due"),
        pytest.param("plain", "https://host.example/d", 200, 1e9, 1e9, 299, "none", id="reload-not-due"),
        pytest.param("dash", "https://host.example/dashboard", 200, 1e9, 1e9, 1e9, "none", id="reload-off"),
    ],
)
def test_next_action(screen, href, status, since_login, since_retry, since_load, expected):
    action = kiosk.next_action(
        SCREENS[screen], href, status, NOW, NOW - since_login, NOW - since_retry, NOW - since_load
    )
    assert action == expected


def test_page_is_up():
    assert kiosk.page_is_up(PLAIN, "https://host.example:443/x", 200)
    assert kiosk.page_is_up(PLAIN, "https://host.example/x", 0)
    assert not kiosk.page_is_up(PLAIN, "https://host.example/x", 503)
    assert not kiosk.page_is_up(PLAIN, "https://evil.example/x", 200)
    assert not kiosk.page_is_up(PLAIN, "chrome-error://chromewebdata/", 0)


def test_grafana_login_expression_quotes_everything():
    user = "closet-kiosk"
    password = "p\"a'ss\\w`${x}`</script>\n<!--\u2028\u2029\u00e9"
    origin = "https://host.example:3000"
    expr = kiosk.grafana_login_expression(user, password, origin)
    args = f"({json.dumps(user)}, {json.dumps(password)}, {json.dumps(origin)})"
    assert expr.endswith(args)
    assert json.loads("[" + args[1:-1] + "]") == [user, password, origin]
    # The function body doesn't depend on the credentials: they only travel as JSON string literals.
    body = expr[: -len(args)]
    other = kiosk.grafana_login_expression("u", "p", "o")
    assert other == body + '("u", "p", "o")'
    assert expr.count(json.dumps(password)) == 1
    rest = expr.replace(json.dumps(password), "")
    for fragment in ("</script>", "p\"a'ss", "\\w`", "<!--"):
        assert fragment not in rest
    for raw in ("\n", "\u2028", "\u2029", "\u00e9"):
        assert raw not in expr  # escaped, so they can't end a JS line or string early


def test_grafana_login_expression_checks_origin_and_path_in_the_page():
    expr = kiosk.grafana_login_expression("u", "p", "https://host.example:3000")
    assert "location.origin !== o" in expr
    assert "location.pathname === '/login'" in expr
    assert "location.pathname.startsWith('/login/')" in expr
    assert "return -1" in expr
    assert "fetch('/login'" in expr  # relative: same origin as the page
    assert "credentials: 'same-origin'" in expr
    assert "method: 'POST'" in expr


COOKIE_KEYS = {"name", "value", "url", "path", "secure", "httpOnly", "sameSite"}


def test_screen_cookies_dashboard_and_display_mode():
    s = Screen(name="right", url="https://Host.Example/dashboard", login="dashboard", hide_nav=True)
    cookies = kiosk.screen_cookies(s, {"dashboard_session": GOOD_SESSION})
    assert [(c["name"], c["value"]) for c in cookies] == [
        ("brain_token", GOOD_SESSION),
        ("__Host-jess_display", "kiosk"),
    ]
    for c in cookies:
        assert set(c) == COOKIE_KEYS  # no "expires" (session cookie), no "domain" (host-only)
        assert c["url"] == "https://host.example/"
        assert (c["path"], c["secure"], c["httpOnly"], c["sameSite"]) == ("/", True, True, "Lax")


def test_screen_cookies_keep_the_port():
    s = Screen(name="x", url="https://host.example:8443/d", hide_nav=True)
    (c,) = kiosk.screen_cookies(s, {})
    assert c["url"] == "https://host.example:8443/"


@pytest.mark.parametrize(
    ("login", "hide_nav", "names"),
    [
        pytest.param("grafana", False, [], id="grafana"),
        pytest.param("none", False, [], id="none"),
        pytest.param("none", True, ["__Host-jess_display"], id="display-only"),
        pytest.param("dashboard", False, ["brain_token"], id="dashboard-only"),
    ],
)
def test_screen_cookies_per_screen(login, hide_nav, names):
    s = Screen(name="x", url="https://host.example/", login=login, hide_nav=hide_nav)
    secrets = {"dashboard_session": GOOD_SESSION} if login == "dashboard" else {}
    assert [c["name"] for c in kiosk.screen_cookies(s, secrets)] == names


def _frontend_lib(name: str) -> Path | None:
    return _first_existing(_REPO / "frontend" / "src" / "lib" / name, Path("/app/frontend_lib") / name)


def test_cookie_names_match_the_frontend():
    assert kiosk.DISPLAY_COOKIE == "__Host-jess_display"
    assert kiosk.DASHBOARD_COOKIE == "brain_token"
    display = _frontend_lib("display-mode.ts")
    if display is None:
        pytest.skip("frontend/src/lib not mounted")
    m = re.search(r"export const DISPLAY_COOKIE = '([^']+)'", display.read_text(encoding="utf-8"))
    assert m and m.group(1) == kiosk.DISPLAY_COOKIE
    assert "value === 'kiosk'" in display.read_text(encoding="utf-8")
    auth = _frontend_lib("dashboard-auth.ts")
    if auth is not None:
        m = re.search(r"export const COOKIE_NAME = '([^']+)'", auth.read_text(encoding="utf-8"))
        assert m and m.group(1) == kiosk.DASHBOARD_COOKIE


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param('["https://h/x", 200]', ("https://h/x", 200), id="ok"),
        pytest.param('["https://h/x", 0]', ("https://h/x", 0), id="status-0"),
        pytest.param("garbage", ("", 0), id="garbage"),
        pytest.param("", ("", 0), id="empty-string"),
        pytest.param(None, ("", 0), id="none"),
        pytest.param(5, ("", 0), id="int"),
        pytest.param(["https://h/x", 200], ("", 0), id="list-not-json-string"),
        pytest.param('["https://h/x", true]', ("https://h/x", 0), id="bool-status"),
        pytest.param('["https://h/x", 200.0]', ("https://h/x", 0), id="float-status"),
        pytest.param('["https://h/x", "200"]', ("https://h/x", 0), id="str-status"),
        pytest.param("[5, 200]", ("", 200), id="href-not-str"),
        pytest.param('["a", "b", "c"]', ("", 0), id="three-items"),
        pytest.param('["a"]', ("", 0), id="one-item"),
        pytest.param("null", ("", 0), id="json-null"),
        pytest.param('{"a": 1}', ("", 0), id="object"),
    ],
)
def test_parse_page_state(value, expected):
    assert kiosk.parse_page_state(value) == expected


# ----------------------------------------------------------------------------------- chrome launch


def test_chrome_args():
    cfg = kiosk.load_config(
        {
            "chrome": "/opt/google/chrome/chrome",
            "screens": [_screen(name="right", url="https://host.example/dashboard?secret=1")],
            "host_map": {"v6.example": "fd7a:115c:a1e0::1", "host.example": "100.64.0.1"},
        }
    )
    screen = cfg.screens[0]
    args = kiosk.chrome_args(cfg, screen, "/run/user/990/jupiter-kiosk/profiles/right", (1080, 1920, 1080, 0))
    assert args[0] == "/opt/google/chrome/chrome"
    assert args[-1] == "about:blank"
    for flag in (
        "--remote-debugging-pipe",
        "--kiosk",
        "--user-data-dir=/run/user/990/jupiter-kiosk/profiles/right",
        "--window-position=1080,0",
        "--window-size=1080,1920",
        "--host-resolver-rules=MAP host.example 100.64.0.1, MAP v6.example [fd7a:115c:a1e0::1]",
        *kiosk.CHROME_FLAGS,
    ):
        assert flag in args
    assert not [a for a in args if a.startswith("--remote-debugging-port")]  # no TCP debugging port
    assert not [a for a in args if "host.example/dashboard" in a]  # the page URL isn't on the command line


def test_chrome_args_without_host_map():
    cfg = kiosk.load_config(_cfg())
    args = kiosk.chrome_args(cfg, cfg.screens[0], "/p", (1920, 1080, 0, 0))
    assert not [a for a in args if a.startswith("--host-resolver-rules")]
    assert args[-1] == "about:blank"


def test_chrome_env(monkeypatch, zoneinfo):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("DISPLAY", ":0")
    env = kiosk.chrome_env(kiosk.load_config(_cfg(timezone="America/Chicago")))
    assert env["TZ"] == "America/Chicago"
    assert env["DISPLAY"] == ":0"
    assert os.environ["TZ"] == "UTC"  # a copy, not os.environ itself
    monkeypatch.delenv("TZ")
    assert "TZ" not in kiosk.chrome_env(kiosk.load_config(_cfg()))


def test_binary_stamp_changes_when_the_binary_is_replaced(tmp_path):
    binary = tmp_path / "chrome"
    binary.write_bytes(b"v1")
    link = tmp_path / "google-chrome"
    link.symlink_to(binary)
    first = kiosk.binary_stamp(str(binary))
    assert first is not None
    assert kiosk.binary_stamp(str(link)) == first  # follows the symlink to the real binary
    new = tmp_path / "chrome.dpkg-new"
    new.write_bytes(b"v2")
    st = binary.stat()
    os.utime(new, ns=(st.st_atime_ns, st.st_mtime_ns))  # same mtime: only the inode tells them apart
    os.replace(new, binary)
    assert kiosk.binary_stamp(str(binary)) != first
    assert kiosk.binary_stamp(str(tmp_path / "missing")) is None


def test_binary_stamp_changes_on_in_place_rewrite(tmp_path):
    binary = tmp_path / "chrome"
    binary.write_bytes(b"v1")
    first = kiosk.binary_stamp(str(binary))
    os.utime(binary, ns=(0, binary.stat().st_mtime_ns + 1_000_000_000))
    assert kiosk.binary_stamp(str(binary)) != first


# --------------------------------------------------------------------------------------------- CDP


class FakeChrome:
    """The far end of --remote-debugging-pipe: reads NUL-terminated commands, answers via a handler."""

    def __init__(self, handler):
        self.to_chrome_r, to_chrome_w = os.pipe()
        from_chrome_r, self._from_chrome_w = os.pipe()
        self._lock = threading.Lock()
        self.handler = handler
        self.received: list[dict] = []
        self.got_message = threading.Event()
        self.state: dict = {}
        self.cdp = kiosk.Cdp(send_fd=to_chrome_w, recv_fd=from_chrome_r)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def send(self, message: dict) -> None:
        with self._lock:
            if self._from_chrome_w is not None:
                os.write(self._from_chrome_w, json.dumps(message).encode() + b"\0")

    def hang_up(self) -> None:
        """Chrome dies: the answers pipe closes."""
        with self._lock:
            if self._from_chrome_w is not None:
                os.close(self._from_chrome_w)
                self._from_chrome_w = None

    def _loop(self) -> None:
        buf = bytearray()
        while True:
            chunk = os.read(self.to_chrome_r, 1 << 16)
            if not chunk:
                return
            start = len(buf)
            buf += chunk
            for message in kiosk.split_messages(buf, start):
                self.received.append(message)
                self.got_message.set()
                self.handler(self, message)

    def shutdown(self) -> None:
        self.hang_up()
        self.cdp.close()
        self._thread.join(2)
        os.close(self.to_chrome_r)


def answer(chrome: FakeChrome, message: dict) -> None:
    method = message.get("method")
    if method == "Echo":
        chrome.send({"id": message["id"], "result": {"params": message["params"], "session": message.get("sessionId")}})
    elif method == "Fail":
        chrome.send({"id": message["id"], "error": {"code": -32000, "message": "refused " + "x" * 500}})
    elif method == "Emit":
        chrome.send({"method": "Inspector.targetCrashed", "params": {}, "sessionId": "S"})
        chrome.send({"id": message["id"], "result": {}})
    elif method == "Flood":
        for i in range(300):
            chrome.send({"method": "Network.dataReceived", "params": {"i": i}})
        chrome.send({"id": message["id"], "result": {"flooded": True}})
    elif method == "Reverse":  # hold two calls, answer the second first
        held = chrome.state.setdefault("held", [])
        held.append(message)
        if len(held) == 2:
            for m in reversed(held):
                chrome.send({"id": m["id"], "result": {"tag": m["params"]["tag"]}})
    # "Silent": no answer


@pytest.fixture
def fake_chrome():
    made: list[FakeChrome] = []

    def make(handler=answer) -> FakeChrome:
        chrome = FakeChrome(handler)
        made.append(chrome)
        return chrome

    yield make
    for chrome in made:
        chrome.shutdown()


def test_cdp_call_round_trip(fake_chrome):
    chrome = fake_chrome()
    assert chrome.cdp.call("Echo", {"a": 1}, session="S1", timeout=2) == {"params": {"a": 1}, "session": "S1"}
    assert chrome.cdp.call("Echo", timeout=2) == {"params": {}, "session": None}
    first, second = chrome.received
    assert first == {"id": 1, "method": "Echo", "params": {"a": 1}, "sessionId": "S1"}
    assert second == {"id": 2, "method": "Echo", "params": {}}  # no sessionId key without a session


def test_cdp_answers_matched_by_id(fake_chrome):
    chrome = fake_chrome()
    results = {}

    def call(tag):
        results[tag] = chrome.cdp.call("Reverse", {"tag": tag}, timeout=3)

    threads = [threading.Thread(target=call, args=(tag,)) for tag in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert results == {"a": {"tag": "a"}, "b": {"tag": "b"}}


def test_cdp_error_answer_raises_bounded_message(fake_chrome):
    chrome = fake_chrome()
    with pytest.raises(CdpError, match="^Fail: refused") as err:
        chrome.cdp.call("Fail", timeout=2)
    assert len(str(err.value)) <= len("Fail: ") + 120


def test_cdp_timeout_removes_pending_and_ignores_late_answer(fake_chrome):
    chrome = fake_chrome()
    started = time.monotonic()
    with pytest.raises(CdpError, match="no answer"):
        chrome.cdp.call("Silent", timeout=0.2)
    assert time.monotonic() - started < 2
    assert chrome.cdp._pending == {}
    chrome.send({"id": 1, "result": {"late": True}})  # the answer to the call that timed out
    assert chrome.cdp.call("Echo", {"n": 2}, timeout=2) == {"params": {"n": 2}, "session": None}


def test_cdp_eof_fails_a_blocked_call_promptly(fake_chrome):
    chrome = fake_chrome()
    outcome = {}

    def blocked():
        started = time.monotonic()
        try:
            chrome.cdp.call("Silent", timeout=20)
        except CdpError as e:
            outcome["error"] = str(e)
        outcome["seconds"] = time.monotonic() - started

    t = threading.Thread(target=blocked)
    t.start()
    assert chrome.got_message.wait(2)
    chrome.hang_up()
    t.join(5)
    assert outcome["error"] == "Silent: connection closed"
    assert outcome["seconds"] < 3
    assert chrome.cdp.closed.wait(2)
    with pytest.raises(CdpError, match="connection closed"):
        chrome.cdp.call("Echo", timeout=2)


def test_cdp_events_go_to_the_queue(fake_chrome):
    chrome = fake_chrome()
    assert chrome.cdp.call("Emit", timeout=2) == {}
    assert chrome.cdp.events.get(timeout=2) == {"method": "Inspector.targetCrashed", "params": {}, "sessionId": "S"}


def test_cdp_event_flood_does_not_block_answers(fake_chrome):
    chrome = fake_chrome()
    assert chrome.cdp.call("Flood", timeout=3) == {"flooded": True}
    assert chrome.cdp.events.qsize() == chrome.cdp.events.maxsize  # the rest were dropped, not waited on


def test_cdp_write_failure():
    to_chrome_r, to_chrome_w = os.pipe()
    from_chrome_r, from_chrome_w = os.pipe()
    cdp = kiosk.Cdp(send_fd=to_chrome_w, recv_fd=from_chrome_r)
    os.close(to_chrome_r)  # nobody reads the commands: EPIPE
    try:
        with pytest.raises(CdpError, match="write failed"):
            cdp.call("Echo", timeout=2)
        assert cdp._pending == {}
    finally:
        os.close(from_chrome_w)
        cdp.close()
    assert cdp.closed.is_set()


def test_cdp_close_after_chrome_exit_ends_the_reader(fake_chrome):
    chrome = fake_chrome()
    chrome.hang_up()
    chrome.cdp.close()
    assert not chrome.cdp._reader.is_alive()
    assert chrome.cdp.closed.is_set()
    assert chrome.cdp._recv.closed


FAKE_CHROME_PROCESS = r"""
import json, os, subprocess, sys

def is_open(fd):
    try:
        os.fstat(fd)
        return True
    except OSError:
        return False

open_fds = [fd for fd in range(256) if is_open(fd)]
child = None
buf = b""
while True:
    chunk = os.read(3, 65536)
    if not chunk:
        break
    buf += chunk
    while b"\0" in buf:
        raw, buf = buf.split(b"\0", 1)
        msg = json.loads(raw)
        result = {"fds": open_fds, "argv": sys.argv[1:], "tz": os.environ.get("TZ"), "pgid": os.getpgid(0)}
        if msg["method"] == "Spawn":
            child = subprocess.Popen(["sleep", "30"], close_fds=True)
            result["child"] = child.pid
        os.write(4, json.dumps({"id": msg["id"], "result": result}).encode() + b"\0")
"""


def _gone(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as f:
            return f.read().rsplit(")", 1)[1].split()[0] == "Z"
    except FileNotFoundError:
        return True


def _open_fds() -> set[str]:
    return set(os.listdir("/proc/self/fd"))


@pytest.mark.skipif(not os.path.exists("/bin/bash"), reason="launch_chrome needs /bin/bash")
def test_launch_chrome_pipe_on_fds_3_and_4(monkeypatch):
    monkeypatch.delenv("KIOSK_DEBUG_CHROME", raising=False)
    before = _open_fds()
    env = dict(os.environ, TZ="America/Chicago")
    proc, cdp = kiosk.launch_chrome([sys.executable, "-I", "-c", FAKE_CHROME_PROCESS, "--kiosk"], env)
    try:
        result = cdp.call("Spawn", timeout=10)
        assert result["fds"] == [0, 1, 2, 3, 4]  # only the pipe ends on 3/4: nothing else leaks in
        assert result["argv"] == ["--kiosk"]
        assert result["tz"] == "America/Chicago"
        assert result["pgid"] == proc.pid  # own process group, so kill_group reaches the renderers
        grandchild = result["child"]
    finally:
        kiosk.kill_group(proc)
        cdp.close()
    assert proc.poll() is not None
    deadline = time.monotonic() + 5
    while not _gone(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _gone(grandchild)
    assert cdp.closed.is_set()
    assert _open_fds() == before  # every pipe end closed again


# ------------------------------------------------------------------------------------ ScreenRunner


class ScriptedCdp:
    """Answers CDP calls from a table; records what was asked."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls: list[tuple[str, dict | None, str | None]] = []
        self.events: queue.Queue = queue.Queue()
        self.closed = threading.Event()

    def call(self, method, params=None, session=None, timeout=None):
        self.calls.append((method, params, session))
        result = self.answers.get(method, {})
        if callable(result):
            result = result(params)
        if isinstance(result, Exception):
            raise result
        return result

    def methods(self) -> list[str]:
        return [m for m, _, _ in self.calls]

    def close(self):
        pass


class FakeProc:
    pid = 0
    returncode = None

    def poll(self):
        return self.returncode


PASSWORD = 'pw-"\\</script>-s3cret'


@pytest.fixture
def runner_factory(tmp_path, monkeypatch, zoneinfo):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    chrome = tmp_path / "chrome"
    chrome.write_bytes(b"v1")

    def make(screen: dict, answers: dict | None = None):
        cfg = kiosk.load_config({"chrome": str(chrome), "screens": [screen]})
        secrets = {"grafana_user": "closet-kiosk", "grafana_password": PASSWORD, "dashboard_session": GOOD_SESSION}
        runner = kiosk.ScreenRunner(cfg, cfg.screens[0], (1080, 1920, 0, 0), secrets, threading.Event())
        runner.cdp = ScriptedCdp(answers or {})
        runner.proc = FakeProc()
        return runner

    return make


GRAFANA_SCREEN = _screen(name="left", url="https://host.example:3000/d/jess-glance", login="grafana")
DASH_SCREEN = _screen(name="right", url="https://host.example/dashboard", login="dashboard", hide_nav=True)


def _login_answers(value) -> dict:
    return {
        "Page.getFrameTree": {"frameTree": {"frame": {"id": "F1"}}},
        "Page.createIsolatedWorld": {"executionContextId": 42},
        "Runtime.evaluate": value if isinstance(value, Exception) else {"result": {"type": "number", "value": value}},
    }


def test_runner_profile_dir_in_runtime_dir(runner_factory, tmp_path):
    runner = runner_factory(GRAFANA_SCREEN)
    assert runner.profile_dir == str(tmp_path / "run" / "jupiter-kiosk" / "profiles" / "left")


def test_runner_grafana_login_in_isolated_world(runner_factory, caplog):
    caplog.set_level(logging.INFO, logger="jupiter-kiosk")
    runner = runner_factory(GRAFANA_SCREEN, _login_answers(200))
    runner._login("S")
    assert runner.cdp.methods() == ["Page.getFrameTree", "Page.createIsolatedWorld", "Runtime.evaluate"]
    _, world_params, world_session = runner.cdp.calls[1]
    assert world_params == {"frameId": "F1", "worldName": "jupiter-kiosk"}
    assert world_session == "S"
    _, params, session = runner.cdp.calls[2]
    assert session == "S"
    assert params == {
        "expression": kiosk.grafana_login_expression("closet-kiosk", PASSWORD, "https://host.example:3000"),
        "contextId": 42,
        "awaitPromise": True,
        "returnByValue": True,
    }
    assert "logged in to Grafana" in caplog.text
    assert "s3cret" not in caplog.text


@pytest.mark.parametrize(
    ("value", "logged"),
    [
        pytest.param(-1, "left the login page before the login; not sent", id="left-page"),
        pytest.param(401, "Grafana login failed (HTTP 401)", id="401"),
        pytest.param(True, "Grafana login failed (no answer)", id="bool"),
        pytest.param(None, "Grafana login failed (no answer)", id="no-value"),
        pytest.param(CdpError("Runtime.evaluate: no answer in 30s"), "Grafana login failed (no answer)", id="timeout"),
    ],
)
def test_runner_grafana_login_outcomes(runner_factory, caplog, value, logged):
    caplog.set_level(logging.INFO, logger="jupiter-kiosk")
    runner = runner_factory(GRAFANA_SCREEN, _login_answers(value))
    runner._login("S")  # a slow server is not a dead browser: no raise
    assert logged in caplog.text
    assert "s3cret" not in caplog.text


def test_runner_grafana_login_timeout_with_dead_pipe_raises(runner_factory):
    runner = runner_factory(GRAFANA_SCREEN, _login_answers(CdpError("Runtime.evaluate: connection closed")))
    runner.cdp.closed.set()
    with pytest.raises(CdpError):
        runner._login("S")


def test_runner_dashboard_login_sets_cookies_again(runner_factory, caplog):
    caplog.set_level(logging.INFO, logger="jupiter-kiosk")
    runner = runner_factory(DASH_SCREEN)
    runner._login("S")
    assert runner.cdp.calls == [
        ("Storage.setCookies", {"cookies": kiosk.screen_cookies(runner.screen, runner.secrets)}, None)
    ]
    assert "setting the session cookie again" in caplog.text
    assert GOOD_SESSION not in caplog.text


def test_runner_tolerant_raises_only_for_a_dead_browser(runner_factory):
    runner = runner_factory(GRAFANA_SCREEN, {"Page.navigate": CdpError("Page.navigate: no answer in 30s")})
    assert runner._tolerant("Page.navigate", {}, "S", 30) is None
    runner.proc.returncode = -9
    with pytest.raises(CdpError):
        runner._tolerant("Page.navigate", {}, "S", 30)


def test_runner_drain_events(runner_factory):
    runner = runner_factory(GRAFANA_SCREEN)
    assert runner._drain_events() is False
    runner.cdp.events.put({"method": "Network.dataReceived"})
    runner.cdp.events.put({"method": "Inspector.targetCrashed"})
    assert runner._drain_events() is True
    assert runner.cdp.events.empty()
    runner.cdp.events.put({"method": "Target.detachedFromTarget"})
    with pytest.raises(CdpError, match="page closed"):
        runner._drain_events()


def test_runner_place_window(runner_factory):
    right_place = {"windowId": 7, "bounds": {"left": 0, "top": 0, "width": 1080, "height": 1920}}
    runner = runner_factory(GRAFANA_SCREEN, {"Browser.getWindowForTarget": right_place})
    runner._place_window("T")
    assert runner.cdp.methods() == ["Browser.getWindowForTarget"]

    wrong_place = {"windowId": 7, "bounds": {"left": 1080, "top": 0, "width": 1080, "height": 1920}}
    runner = runner_factory(GRAFANA_SCREEN, {"Browser.getWindowForTarget": wrong_place})
    runner._place_window("T")
    bounds = [p["bounds"] for m, p, _ in runner.cdp.calls if m == "Browser.setWindowBounds"]
    assert bounds == [
        {"windowState": "normal"},
        {"left": 0, "top": 0, "width": 1080, "height": 1920},
        {"windowState": "fullscreen"},
    ]


def test_runner_note_health_logs_once_down_once_up(runner_factory, caplog):
    caplog.set_level(logging.INFO, logger="jupiter-kiosk")
    runner = runner_factory(GRAFANA_SCREEN)
    up = "https://host.example:3000/d/jess-glance?var-secret=1"
    for href, status in [
        (up, 200),
        ("chrome-error://chromewebdata/", 0),
        ("chrome-error://chromewebdata/", 0),
        (up, 502),
        (up, 200),
        (up, 200),
    ]:
        runner._note_health(href, status)
    assert caplog.text.count("page not loading") == 1
    assert "no connection or wrong address" in caplog.text
    assert caplog.text.count("page loading again") == 1
    assert "var-secret" not in caplog.text and "chromewebdata" not in caplog.text


def _session_answers(states, runner_ref, on_state=None):
    """CDP answers for a whole _session(): page states in order, then stop."""
    remaining = list(states)

    def evaluate(params):
        if params["expression"] != kiosk.PAGE_STATE_JS:
            return {"result": {"value": 200}}  # the Grafana login
        state = remaining.pop(0) if remaining else None
        if on_state is not None:
            on_state(runner_ref[0])
        if isinstance(state, Exception):
            return state
        if not remaining:
            runner_ref[0].stop.set()
        href, status = state if state else ("about:blank", 0)
        return {"result": {"type": "string", "value": json.dumps([href, status])}}

    return {
        "Target.getTargets": {"targetInfos": [{"type": "browser", "targetId": "B"}, {"type": "page", "targetId": "T"}]},
        "Target.attachToTarget": {"sessionId": "S"},
        "Browser.getWindowForTarget": {"windowId": 1, "bounds": {"left": 0, "top": 0, "width": 1080, "height": 1920}},
        "Page.getFrameTree": {"frameTree": {"frame": {"id": "F1"}}},
        "Page.createIsolatedWorld": {"executionContextId": 42},
        "Runtime.evaluate": evaluate,
    }


@pytest.fixture
def session_env(monkeypatch, runner_factory):
    monkeypatch.setattr(kiosk, "CHECK_SECONDS", 0)
    monkeypatch.setattr(kiosk, "ERROR_RETRY_SECONDS", 0)
    monkeypatch.setattr(kiosk, "LOGIN_RETRY_SECONDS", 0)

    def make(screen, states, on_state=None):
        ref = [None]
        runner = runner_factory(screen, _session_answers(states, ref, on_state))
        ref[0] = runner
        cdp, proc = runner.cdp, runner.proc
        launched = []

        def fake_launch(args, env):
            launched.append((args, env))
            return proc, cdp

        monkeypatch.setattr(kiosk, "launch_chrome", fake_launch)
        return runner, cdp, launched

    return make


def test_session_retries_logs_in_and_sets_cookies(session_env):
    runner, cdp, launched = session_env(
        GRAFANA_SCREEN,
        [
            ("chrome-error://chromewebdata/", 0),
            ("https://host.example:3000/login", 200),
            ("https://host.example:3000/d/jess-glance", 200),
        ],
    )
    runner._session()
    assert launched[0][0] == kiosk.chrome_args(runner.cfg, runner.screen, runner.profile_dir, runner.geometry)
    assert os.path.isdir(runner.profile_dir)
    assert stat.S_IMODE(os.stat(runner.profile_dir).st_mode) == 0o700
    methods = cdp.methods()
    assert methods[:4] == [
        "Target.getTargets",
        "Target.attachToTarget",
        "Browser.getWindowForTarget",
        "Inspector.enable",
    ]
    assert "Storage.setCookies" not in methods  # grafana screen without hide_nav: no cookies
    login_eval = [p for m, p, _ in cdp.calls if m == "Runtime.evaluate" and p["expression"] != kiosk.PAGE_STATE_JS]
    assert len(login_eval) == 1
    # initial load, the retry after chrome-error, the load after the login
    assert methods.count("Page.navigate") == 3
    navigations = [p["url"] for m, p, _ in cdp.calls if m == "Page.navigate"]
    assert set(navigations) == {runner.screen.url}


def test_session_dashboard_sets_cookies_before_first_load(session_env):
    runner, cdp, _ = session_env(DASH_SCREEN, [("https://host.example/dashboard", 200)])
    runner._session()
    methods = cdp.methods()
    assert methods.index("Storage.setCookies") < methods.index("Page.navigate")


def test_session_restarts_after_chrome_upgrade(session_env, monkeypatch):
    # start, unchanged, new (dpkg may still be unpacking), still new: restart only on the 2nd sighting
    stamps = iter([(1, 1), (1, 1), (2, 2), (2, 2), (3, 3)])
    monkeypatch.setattr(kiosk, "binary_stamp", lambda path: next(stamps))
    runner, _, _ = session_env(GRAFANA_SCREEN, [("https://host.example:3000/d/jess-glance", 200)] * 10)
    with pytest.raises(CdpError, match="upgraded"):
        runner._session()
    assert next(stamps) == (3, 3)  # exactly four looks: not restarted on the first sighting


def test_session_no_restart_while_the_binary_keeps_changing(session_env, monkeypatch):
    stamps = iter([(1, 1), (2, 2), (3, 3), None, (4, 4)])  # dpkg unpacking: never the same twice
    monkeypatch.setattr(kiosk, "binary_stamp", lambda path: next(stamps, (4, 4)))
    runner, _, _ = session_env(GRAFANA_SCREEN, [("https://host.example:3000/d/jess-glance", 200)] * 4)
    runner._session()  # four page checks, then stop: no restart


def test_session_hung_page_restarts_after_three_failed_reads(session_env):
    runner, cdp, _ = session_env(GRAFANA_SCREEN, [CdpError("Runtime.evaluate: no answer in 10s")] * 5)
    with pytest.raises(CdpError, match="stopped answering"):
        runner._session()
    page_reads = [p for m, p, _ in cdp.calls if m == "Runtime.evaluate"]
    assert len(page_reads) == kiosk.HUNG_CHECKS


def test_session_crash_loop_restarts_browser(session_env):
    def crash(runner):
        runner.cdp.events.put({"method": "Inspector.targetCrashed", "params": {}})

    runner, cdp, _ = session_env(GRAFANA_SCREEN, [("https://host.example:3000/d/jess-glance", 200)] * 10, crash)
    with pytest.raises(CdpError, match="crashed 3 times"):
        runner._session()
    assert cdp.methods().count("Page.navigate") == 1 + kiosk.CRASH_LIMIT - 1  # initial + one per tolerated crash


def test_session_chrome_exit(session_env):
    runner, _, _ = session_env(GRAFANA_SCREEN, [("https://host.example:3000/d/jess-glance", 200)] * 3)
    runner.proc.returncode = 1
    with pytest.raises(CdpError, match=r"chrome exited \(1\)"):
        runner._session()


def test_snapshot_writes_png_mode_600(runner_factory, tmp_path):
    png = b"\x89PNG\r\n\x1a\nfake"
    runner = runner_factory(GRAFANA_SCREEN, {"Page.captureScreenshot": {"data": base64.b64encode(png).decode()}})
    runner._snapshot("S")
    path = tmp_path / "run" / "jupiter-kiosk" / "left.png"
    assert path.read_bytes() == png
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# -------------------------------------------------------------------------------------------- main


@pytest.fixture
def main_env(monkeypatch, tmp_path, zoneinfo):
    installed = []
    monkeypatch.setattr(kiosk, "signal", _Proxy(kiosk.signal, signal=lambda sig, handler: installed.append(sig)))
    monkeypatch.setattr(kiosk.logging, "basicConfig", lambda **kw: None)
    config = tmp_path / "kiosk.json"
    secrets = tmp_path / "secrets.json"
    config.write_text(json.dumps(_cfg(DASH_SCREEN)), encoding="utf-8")
    secrets.write_text(json.dumps({"dashboard_session": GOOD_SESSION}), encoding="utf-8")
    # main() sets XAUTHORITY itself: set-then-delete makes monkeypatch restore "absent" afterwards, so
    # the value doesn't leak into later tests.
    monkeypatch.setenv("XAUTHORITY", "x")
    monkeypatch.delenv("XAUTHORITY")
    return SimpleNamespace(config=config, secrets=secrets, installed=installed)


def _argv(env, *extra) -> list[str]:
    return ["--config", str(env.config), "--secrets", str(env.secrets), *extra]


def test_main_preflight(main_env, monkeypatch):
    seen = []
    monkeypatch.setattr(kiosk, "preflight", lambda cfg, x_server=False: seen.append((cfg, x_server)) or 7)
    assert kiosk.main(_argv(main_env, "--preflight")) == 7
    assert [s.name for s in seen[0][0].screens] == ["right"]
    assert seen[0][1] is False
    assert kiosk.signal.SIGUSR1 in main_env.installed
    assert kiosk.main(_argv(main_env, "--preflight", "--x-server")) == 7
    assert seen[1][1] is True


def test_main_x_server_flag_only_with_preflight(main_env, monkeypatch, caplog):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(kiosk, "run_kiosk", lambda *a: pytest.fail("must not run the kiosk"))
    assert kiosk.main(_argv(main_env, "--x-server")) == 2
    assert "--x-server only goes with --preflight" in caplog.text


def test_main_refuses_without_secrets(main_env, monkeypatch, caplog):
    monkeypatch.setattr(kiosk, "preflight", lambda cfg: pytest.fail("preflight must not run"))
    main_env.secrets.write_text("{}", encoding="utf-8")
    assert kiosk.main(_argv(main_env, "--preflight")) == 2
    assert "dashboard_session" in caplog.text


def test_main_refuses_bad_config(main_env):
    main_env.config.write_text("{", encoding="utf-8")
    assert kiosk.main(_argv(main_env)) == 2


def test_main_needs_display(main_env, monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(kiosk, "wait_for_monitor", lambda stop: pytest.fail("must not touch X"))
    assert kiosk.main(_argv(main_env)) == 2


@pytest.mark.parametrize("display", ["host:0", "unix/:0", "/tmp/.X11-unix/X0", ":x"])
def test_main_refuses_a_display_that_is_not_local(main_env, monkeypatch, caplog, display):
    monkeypatch.setenv("DISPLAY", display)
    monkeypatch.setattr(kiosk, "hold_x_abstract_name", lambda n: pytest.fail("must not get this far"))
    assert kiosk.main(_argv(main_env)) == 2
    assert "DISPLAY must be a local display" in caplog.text


def _free_display() -> int:
    """A display number whose abstract X socket name nobody holds (tests may run in parallel)."""
    for n in range(700, 1000):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.bind(f"\0/tmp/.X11-unix/X{n}")
        except OSError:
            continue
        finally:
            probe.close()
        return n
    pytest.skip("no free abstract X display name")


def test_main_holds_the_abstract_name_while_it_runs(main_env, monkeypatch, tmp_path):
    n = _free_display()
    monkeypatch.setenv("DISPLAY", f":{n}")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("XAUTHORITY", "/somewhere/else")  # always the file start-x.sh gave X
    seen = {}

    def fake_run_kiosk(cfg, secrets, stop, runners, number):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with client, pytest.raises(ConnectionRefusedError):  # held, not listening: xcb falls back to /tmp
            client.connect(f"\0/tmp/.X11-unix/X{n}")
        seen["xauthority"] = os.environ["XAUTHORITY"]
        seen["number"] = number
        return 5

    monkeypatch.setattr(kiosk, "run_kiosk", fake_run_kiosk)
    assert kiosk.main(_argv(main_env)) == 5
    assert seen["xauthority"] == str(tmp_path / "jupiter-kiosk.Xauthority")
    assert seen["number"] == n
    again = kiosk.hold_x_abstract_name(n)  # released on the way out
    again.close()


def test_main_refuses_when_the_abstract_name_is_taken(main_env, monkeypatch, caplog):
    n = _free_display()
    squatter = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    squatter.bind(f"\0/tmp/.X11-unix/X{n}")
    monkeypatch.setenv("DISPLAY", f":{n}.0")
    monkeypatch.setattr(kiosk, "run_kiosk", lambda *a: pytest.fail("must not connect to X"))
    with squatter:
        assert kiosk.main(_argv(main_env)) == 1
    assert "abstract socket name is taken" in caplog.text


def test_main_other_hold_errors_are_not_called_posing(main_env, monkeypatch, caplog):
    monkeypatch.setenv("DISPLAY", ":0")

    def no_fds(number):
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(kiosk, "hold_x_abstract_name", no_fds)
    monkeypatch.setattr(kiosk, "run_kiosk", lambda *a: pytest.fail("must not connect to X"))
    assert kiosk.main(_argv(main_env)) == 1
    assert "can't hold the X display's abstract socket name (Too many open files)" in caplog.text
    assert "posing" not in caplog.text


def test_main_needs_a_runtime_dir_for_the_cookie(main_env, monkeypatch, caplog):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setattr(kiosk, "hold_x_abstract_name", lambda n: pytest.fail("must not get this far"))
    assert kiosk.main(_argv(main_env)) == 2
    assert "can't find the X auth cookie" in caplog.text


@pytest.fixture
def run_env(monkeypatch, tmp_path, zoneinfo):
    """run_kiosk() with X answering, a clean /proc/net/unix and no monitor (so it returns early)."""
    net_unix = tmp_path / "unix"
    net_unix.write_text(UNIX_ISOLATED, "utf-8")
    monkeypatch.setattr(kiosk, "NET_UNIX", str(net_unix))
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(kiosk, "wait_for_x", lambda stop, timeout=60: True)
    monkeypatch.setattr(kiosk, "wait_for_monitor", lambda stop: None)
    cfg = kiosk.load_config(_cfg(DASH_SCREEN))
    return SimpleNamespace(net_unix=net_unix, cfg=cfg, args=(cfg, {}, threading.Event(), [], 0))


def test_run_kiosk_goes_on_when_x_is_isolated(run_env, caplog):
    assert kiosk.run_kiosk(*run_env.args) == 0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_run_kiosk_refuses_when_x_shares_the_host_network(run_env, monkeypatch, caplog):
    run_env.net_unix.write_text(UNIX_EXPOSED, "utf-8")
    monkeypatch.setattr(kiosk, "wait_for_monitor", lambda stop: pytest.fail("must not lay out the screens"))
    assert kiosk.run_kiosk(*run_env.args) == 1
    assert "3 X server socket(s) reachable from the host network namespace" in caplog.text
    assert "nvidia-xdriver" not in caplog.text  # a count, not names


def test_run_kiosk_refuses_without_net_unix(run_env, monkeypatch, caplog):
    run_env.net_unix.unlink()
    assert kiosk.run_kiosk(*run_env.args) == 1
    assert "can't read" in caplog.text


def test_run_kiosk_x_not_answering(run_env, monkeypatch):
    monkeypatch.setattr(kiosk, "wait_for_x", lambda stop, timeout=60: False)  # wait_for_x logs why
    monkeypatch.setattr(kiosk, "wait_for_monitor", lambda stop: pytest.fail("must not lay out the screens"))
    assert kiosk.run_kiosk(*run_env.args) == 1


def test_run_kiosk_stopped_while_waiting_for_x(run_env, monkeypatch, caplog):
    monkeypatch.setattr(kiosk, "wait_for_x", lambda stop, timeout=60: False)
    run_env.args[2].set()
    assert kiosk.run_kiosk(*run_env.args) == 0
    assert "didn't answer" not in caplog.text


# ------------------------------------------------------------------------------------- unit files
# The installed units are hand-copied from these files (README → Install). What keeps X's abstract
# sockets away from the host network namespace is spread over both units and start-x.sh, so the
# combination is pinned here.


def _unit(name: str) -> dict[tuple[str, str], list[str]]:
    """{(section, key): [values in order]} of a systemd unit file; comments and continuations aside."""
    path = _KIOSK_PATH.with_name(name)
    if not path.is_file():
        pytest.skip(f"{name} not found")
    out: dict[tuple[str, str], list[str]] = {}
    section = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, _, value = line.partition("=")
        out.setdefault((section, key.strip()), []).append(value.strip())
    return out


def _words(unit: dict, section: str, key: str) -> list[str]:
    return [w for v in unit.get((section, key), []) for w in v.split()]


X_UNIT = "jupiter-kiosk-x.service"
BROWSER_UNIT = "jupiter-kiosk.service"
VOICE_NODES = {"-/dev/nvidia0", "-/dev/dri/card0", "-/dev/dri/renderD128"}


def test_x_unit_has_its_own_network_namespace_and_the_seat():
    x = _unit(X_UNIT)
    assert x[("Service", "PrivateNetwork")] == ["yes"]
    assert x[("Service", "PrivateTmp")] == ["yes"]
    assert x[("Service", "User")] == ["kiosk"]
    assert x[("Service", "PAMName")] == ["jupiter-kiosk"]
    assert x[("Service", "TTYPath")] == ["/dev/tty1"]
    assert x[("Unit", "Conflicts")] == ["getty@tty1.service"]
    assert x[("Service", "ExecStartPre")] == ["/usr/local/lib/jupiter-kiosk/kiosk.py --preflight --x-server"]
    assert x[("Service", "ExecStart")] == ["/usr/local/lib/jupiter-kiosk/start-x.sh"]
    families = set(_words(x, "Service", "RestrictAddressFamilies"))
    assert families == {"AF_UNIX", "AF_NETLINK"}  # X needs no IP at all
    # Follows the browser unit's stops and restarts, and brings it back after an X restart.
    assert x[("Unit", "PartOf")] == [BROWSER_UNIT]
    assert x[("Unit", "Wants")] == [BROWSER_UNIT]
    assert not [k for k in x if k[0] == "Install"]  # started through the browser unit only


def test_browser_unit_shares_only_x_tmp_and_keeps_the_host_network():
    b = _unit(BROWSER_UNIT)
    # JoinsNamespaceOf= joins every namespace that both units make private: PrivateNetwork= here would
    # put Chrome into X's network namespace (no network at all). Only /tmp is shared.
    assert ("Service", "PrivateNetwork") not in b
    assert ("Service", "NetworkNamespacePath") not in b
    assert b[("Service", "PrivateTmp")] == ["yes"]
    assert b[("Unit", "JoinsNamespaceOf")] == [X_UNIT]
    assert b[("Unit", "BindsTo")] == [X_UNIT]
    assert X_UNIT in _words(b, "Unit", "After")
    assert b[("Service", "PAMName")] == ["jupiter-kiosk"]  # into the user slice (memory + IP allow-list)
    assert ("Service", "TTYPath") not in b  # the seat is X's
    assert b[("Service", "ExecStartPre")] == ["/usr/local/lib/jupiter-kiosk/kiosk.py --preflight"]
    assert b[("Service", "ExecStart")] == ["/usr/local/lib/jupiter-kiosk/kiosk.py"]
    assert b[("Service", "Environment")] == ["DISPLAY=:0"]
    assert b[("Install", "WantedBy")] == ["graphical.target"]


@pytest.mark.parametrize("name", [X_UNIT, BROWSER_UNIT])
def test_both_units_keep_the_exec_time_hardening(name):
    u = _unit(name)
    hidden = set(_words(u, "Service", "InaccessiblePaths"))
    assert hidden >= VOICE_NODES
    assert {"-/mnt", "-/home", "-/root", "-/srv"} <= hidden
    for key, value in (
        ("NoNewPrivileges", "yes"),
        ("CapabilityBoundingSet", ""),
        ("RestrictSUIDSGID", "yes"),
        ("ProtectSystem", "full"),
        ("UMask", "0077"),
        ("Restart", "always"),
    ):
        assert u[("Service", key)] == [value], key
    assert u[("Unit", "StartLimitIntervalSec")] == ["0"]


def test_user_manager_hides_the_voice_card_too():
    path = _KIOSK_PATH.with_name("kiosk-user-manager.conf")
    if not path.is_file():
        pytest.skip("kiosk-user-manager.conf not found")
    words = {
        w
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("InaccessiblePaths=")
        for w in line.partition("=")[2].split()
    }
    assert words >= VOICE_NODES


def test_start_x_runs_xorg_without_listeners_on_the_browser_units_display():
    path = _KIOSK_PATH.with_name("start-x.sh")
    if not path.is_file():
        pytest.skip("start-x.sh not found")
    text = path.read_text(encoding="utf-8")
    code = " ".join(line for line in text.splitlines() if not line.lstrip().startswith("#")).replace("\\", " ")
    assert "xinit" not in code  # the units, not xinit, tie X and kiosk.py together
    assert re.search(r"exec /usr/bin/Xorg :0 vt1 ", code)
    # -noreset: no client holds X open while kiosk.py runs xrandr, so X would otherwise reset (and set
    # every mode again) each time one of them disconnects.
    for flag in ("-noreset", "-nolisten tcp", "-nolisten local", '-auth "$XAUTHORITY"', "-config jupiter-kiosk.conf"):
        assert flag in code
    assert "add :0 MIT-MAGIC-COOKIE-1" in code
    assert f'XAUTHORITY="$runtime/{kiosk.XAUTHORITY_NAME}"' in code  # where kiosk.py looks for it
    display = _unit(BROWSER_UNIT)[("Service", "Environment")][0].partition("=")[2]
    assert display == ":0"


def test_xorg_sideband_socket_stays_in_the_shared_tmp():
    if not XORG_SHIPPED.is_file():
        pytest.skip("jupiter-kiosk.xorg.conf not found")
    settings = re.findall(r'^\s*Option\s+"SidebandSocketPath"\s+"([^"]*)"', XORG_SHIPPED.read_text("utf-8"), re.M)
    assert settings == ["/tmp"]  # Chrome's GL client falls back to it: it must be in PrivateTmp
