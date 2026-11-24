"""Static guards for the compose layout the host firewall relies on.

scripts/container-firewall/jess_docker_input.py (docs/internal/CONTAINER_HOST_REACHABILITY.md)
lets (Docker network, port) pairs through to Jupiter's own services and logs or
rejects the rest. A container's packet to the host arrives on the bridge of its
DEFAULT-ROUTE network, so the allow lines only work, and only stay narrow, while:

- host-access (gateway_nerves_host-access, bridge br-jess-hostacc) holds the
  orchestrator alone and is its default route (its highest gw_priority, > 0);
- host-scrape (monitoring_host-scrape, bridge br-jess-scrape) holds Prometheus
  and blackbox-exporter alone and is the default route of both;
- both are plain, non-internal IPv4 bridges (an internal network gives no
  default route; the script reports IPv6 networks as uncovered) whose fixed
  bridge names start with `br-` and fit Linux's 15-char limit, so the
  `-i br-+` jump covers them;
- the repo config allow-lists nothing else and stays at `mode observe` (the
  owner switches the /etc copy to enforce by hand).

A new member on either network would get the host ports of its allow line. A
member whose default route moves elsewhere has its host traffic arrive on
another bridge, which enforce mode blocks.

The compose tests need a full checkout (GitHub CI runs from one). The documented
local runner mounts only orchestrator/, scripts/ and data/ into the image, so
there they skip and only the config tests run. To run all of them locally, mount
the whole worktree: `docker run --rm -v "$PWD:/repo:ro" -w /repo gateway_nerves-orchestrator ...`.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from functools import cache
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_COMPOSE = _REPO / "docker-compose.yml"
_MON_COMPOSE = _REPO / "monitoring" / "docker-compose.yml"
_FW_DIR = _REPO / "scripts" / "container-firewall"
_SCRIPT = _FW_DIR / "jess_docker_input.py"
_CONF = _FW_DIR / "jess-docker-input.conf"

_BRIDGE_OPT = "com.docker.network.bridge.name"
_HOST_ACCESS = "gateway_nerves_host-access"
_HOST_SCRAPE = "monitoring_host-scrape"

# (compose file, network key, fixed Docker name, bridge name, "<file>:<service>" members)
_NETWORKS = [
    pytest.param(
        _COMPOSE,
        "host-access",
        _HOST_ACCESS,
        "br-jess-hostacc",
        {"docker-compose.yml:orchestrator"},
        id="host-access",
    ),
    pytest.param(
        _MON_COMPOSE,
        "host-scrape",
        _HOST_SCRAPE,
        "br-jess-scrape",
        {"monitoring/docker-compose.yml:prometheus", "monitoring/docker-compose.yml:blackbox-exporter"},
        id="host-scrape",
    ),
]


def _rel(path: Path) -> str:
    return str(path.relative_to(_REPO))


def _needs(*paths: Path):
    missing = [_rel(p) for p in paths if not p.is_file()]
    return pytest.mark.skipif(
        bool(missing),
        reason=f"needs a full repo checkout (missing: {', '.join(missing)})",
    )


_needs_compose = _needs(_COMPOSE, _MON_COMPOSE)
_needs_conf = _needs(_SCRIPT, _CONF)


@cache
def _fw():
    """The firewall script itself, so the config is parsed exactly as the host parses it."""
    spec = importlib.util.spec_from_file_location("jess_docker_input_layout_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # before exec: @dataclass looks the module up there
    spec.loader.exec_module(mod)
    return mod


def _load(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        pytest.fail(f"{_rel(path)} no longer parses as YAML ({exc}); fix it or switch this guard to a text check")
    assert isinstance(data, dict), f"{_rel(path)} did not parse to a mapping"
    return data


def _service_networks(svc: dict) -> dict[str, dict]:
    """A service's networks as {key: per-network config}, for the list and the mapping form."""
    nets = svc.get("networks") or {}
    if isinstance(nets, list):
        return {str(n): {} for n in nets}
    return {str(k): (v or {}) for k, v in nets.items()}


def _gw_priority(cfg: dict) -> int:
    return int(cfg.get("gw_priority") or 0)


def _real_net_name(compose: dict, key: str) -> str:
    """Docker name of a network key: its `name:` (or old `external: {name: ...}`), else the key itself."""
    decl = (compose.get("networks") or {}).get(key) or {}
    ext = decl.get("external")
    return str(decl.get("name") or (ext.get("name") if isinstance(ext, dict) else None) or key)


def _repo_compose_files() -> list[Path]:
    found = []
    for root, dirs, files in os.walk(_REPO):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", ".claude", ".next"}]
        found += [Path(root) / f for f in files if re.fullmatch(r"(docker-)?compose[\w.-]*\.ya?ml", f)]
    assert _COMPOSE in found and _MON_COMPOSE in found, "the compose-file walk missed a known file; fix the walk"
    return sorted(found)


def _compose_docs() -> list[tuple[Path, dict | None, str]]:
    """(path, parsed mapping or None when plain YAML can't load it, raw text) for every repo compose file."""
    out = []
    for path in _repo_compose_files():
        text = path.read_text(encoding="utf-8")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            data = None
        out.append((path, data if isinstance(data, dict) else None, text))
    return out


def _members(defining: Path, key: str, fixed_name: str) -> tuple[set[str], list[str]]:
    """Who is on a fixed-name network across every repo compose file, as "<file>:<service>".

    Counts a service whose network resolves to `fixed_name` (or is `key` in the defining file), and a
    sidecar sharing a member's network namespace (network_mode service:<member> / container:<name>),
    which shares its default route too. Also returns unparseable files that mention the name."""
    members: set[str] = set()
    unparsed: list[str] = []
    containers: set[str] = set()
    docs = _compose_docs()
    for path, compose, text in docs:
        if compose is None:
            if fixed_name in text:
                unparsed.append(_rel(path))
            continue
        services = {n: s for n, s in (compose.get("services") or {}).items() if isinstance(s, dict)}
        on = {
            n
            for n, svc in services.items()
            if any(
                _real_net_name(compose, k) == fixed_name or (path == defining and k == key)
                for k in _service_networks(svc)
            )
        }
        members |= {f"{_rel(path)}:{n}" for n in on}
        containers |= {str(services[n].get("container_name") or n) for n in on}
        modes = {f"service:{n}" for n in on}
        members |= {f"{_rel(path)}:{n}" for n, svc in services.items() if str(svc.get("network_mode") or "") in modes}
    modes = {f"container:{c}" for c in containers}
    for path, compose, _ in docs:
        for n, svc in ((compose or {}).get("services") or {}).items():
            if isinstance(svc, dict) and str(svc.get("network_mode") or "") in modes:
                members.add(f"{_rel(path)}:{n}")
    return members, unparsed


# -- the two dedicated networks ------------------------------------------------


@_needs_compose
@pytest.mark.parametrize("compose_path,key,name,bridge,expected", _NETWORKS)
def test_network_declaration(compose_path, key, name, bridge, expected):
    decl = (_load(compose_path).get("networks") or {}).get(key)
    where = f"{_rel(compose_path)} networks.{key}"
    assert isinstance(decl, dict), f"{where} is missing; the host firewall allow-lists {name!r}"
    assert decl.get("name") == name, (
        f"{where}.name is {decl.get('name')!r}, expected {name!r}: jess-docker-input.conf names the network "
        "by this fixed name, so a rename (or a project-prefixed default) silently drops its allow rule"
    )
    assert str(decl.get("driver") or "bridge") == "bridge", f"{where}.driver must be bridge"
    assert (decl.get("driver_opts") or {}).get(_BRIDGE_OPT) == bridge, (
        f"{where}.driver_opts.{_BRIDGE_OPT} is {(decl.get('driver_opts') or {}).get(_BRIDGE_OPT)!r}, expected "
        f"{bridge!r}: a fixed bridge name keeps the firewall rule stable when the network is recreated"
    )
    assert not decl.get("external"), f"{where} must be created by this compose project, not external"
    assert not decl.get("internal"), (
        f"{where} is internal: an internal network has no gateway, so it can't be its members' default route "
        "and their host traffic would arrive on another bridge (blocked in enforce mode)"
    )
    assert not decl.get("enable_ipv6"), (
        f"{where} enables IPv6: the script then reports it uncovered, and JESS-DOCKER-IN6 has no allow lines"
    )


@_needs_compose
@pytest.mark.parametrize("compose_path,key,name,bridge,expected", _NETWORKS)
def test_network_members(compose_path, key, name, bridge, expected):
    members, unparsed = _members(compose_path, key, name)
    assert members == expected and not unparsed, (
        f"on {name}: {sorted(members)!r} (plus unparseable files naming it: {unparsed!r}); expected exactly "
        f"{sorted(expected)!r}. Every member (and every network_mode sidecar of one) gets the host ports of the "
        "network's allow line in jess-docker-input.conf."
    )


@_needs_compose
@pytest.mark.parametrize("compose_path,key,name,bridge,expected", _NETWORKS)
def test_network_is_members_default_route(compose_path, key, name, bridge, expected):
    services = _load(compose_path).get("services") or {}
    for member in sorted(expected):
        svc_name = member.split(":", 1)[1]
        svc = services.get(svc_name)
        assert isinstance(svc, dict), f"{_rel(compose_path)} has no services.{svc_name}; update this guard"
        assert "network_mode" not in svc, f"services.{svc_name}.network_mode is set; it must use compose networks"
        nets = _service_networks(svc)
        assert key in nets, f"services.{svc_name}.networks {sorted(nets)!r} doesn't include {key}"
        mine = _gw_priority(nets[key])
        others = {k: _gw_priority(v) for k, v in nets.items() if k != key}
        assert mine > 0 and all(mine > p for p in others.values()), (
            f"services.{svc_name}: gw_priority {mine} on {key}, others {others!r}. {key} must have the highest "
            "gw_priority (> 0, strictly above every other network): Docker otherwise picks the default route by "
            "network name, the host traffic arrives on that bridge, and enforce mode blocks it."
        )


@_needs_compose
def test_bridge_names_are_covered_by_the_jump():
    fw = _fw() if _SCRIPT.is_file() else None
    declared: dict[str, list[str]] = {}
    for path, compose, _ in _compose_docs():
        for key, decl in ((compose or {}).get("networks") or {}).items():
            bridge = ((decl or {}).get("driver_opts") or {}).get(_BRIDGE_OPT)
            if bridge:
                declared.setdefault(str(bridge), []).append(f"{_rel(path)} networks.{key}")
    for bridge in ("br-jess-hostacc", "br-jess-scrape"):
        assert len(declared.get(bridge, [])) == 1, (
            f"bridge name {bridge!r} is declared by {declared.get(bridge)!r}; it must be exactly one network"
        )
        assert len(bridge) <= 15, f"{bridge!r} is {len(bridge)} chars; Linux interface names are at most 15"
        assert bridge.startswith("br-"), f"{bridge!r} must start with br- so the `-i br-+` INPUT jump covers it"
        if fw is not None:
            assert fw._IFACE_RE.fullmatch(bridge), f"{bridge!r} fails the script's interface-name check"
            assert any(i.endswith("+") and bridge.startswith(i[:-1]) for _, i in fw.JUMPS), (
                f"no INPUT jump in the script covers {bridge!r} (jumps: {fw.JUMPS!r})"
            )


# -- the repo config -------------------------------------------------------------


@_needs_conf
def test_conf_allows_only_the_dedicated_networks():
    allows = _fw().parse_config(_CONF.read_text(encoding="utf-8")).allows
    nets = {a.network for a in allows}
    dedicated = {_HOST_ACCESS, _HOST_SCRAPE}
    assert nets <= dedicated, (
        f"jess-docker-input.conf allow-lists {sorted(nets - dedicated)!r}. Only {sorted(dedicated)!r} may be "
        "allow-listed: they are the dedicated default-route networks of the only containers that need host "
        "access (test_network_members guards who is on them)."
    )
    assert nets == dedicated, (
        f"jess-docker-input.conf has no allow line for {sorted(dedicated - nets)!r}; compose makes it a "
        "default route for host access, so enforce mode would cut its members off"
    )
    assert len(allows) == len(dedicated), (
        "jess-docker-input.conf has more than one allow line for a dedicated network; keep one line per "
        "network so every host port its members get is in one reviewed place"
    )
    # Pin protocol + ports too: widening a line (SSH, SMB, DNS, UDP, ...) must mean
    # changing this reviewed table, not just the config.
    expected = {
        (_HOST_ACCESS, "tcp"): (8004, 8053, 8123, 8888),
        (_HOST_SCRAPE, "tcp"): (5050, 8004, 9100, 9400, 19999),
    }
    got = {(a.network, a.proto): a.ports for a in allows}
    assert got == expected, (
        f"jess-docker-input.conf allows {got!r}, expected {expected!r}. Every host port here is reachable by "
        "the network's containers; update this table only with a reason in docs/internal/CONTAINER_HOST_REACHABILITY.md"
    )


@_needs_conf
def test_conf_is_observe():
    mode = _fw().parse_config(_CONF.read_text(encoding="utf-8")).mode
    assert mode == "observe", (
        f"the repo jess-docker-input.conf says mode {mode}; it stays at observe as the safe default (a first "
        "install copies it to /etc). The owner switches the /etc copy to enforce by hand."
    )
