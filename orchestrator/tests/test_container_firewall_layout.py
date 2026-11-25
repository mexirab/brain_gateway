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

import pytest

from orchestrator.tests.fixtures_ops import COMPOSE as _COMPOSE
from orchestrator.tests.fixtures_ops import (
    FIREWALL_DIR,
    compose_docs,
    jess_docker_input,
    load_mapping,
    needs,
    network_members,
    rel,
    service_networks,
)
from orchestrator.tests.fixtures_ops import MON_COMPOSE as _MON_COMPOSE

_SCRIPT = FIREWALL_DIR / "jess_docker_input.py"
_CONF = FIREWALL_DIR / "jess-docker-input.conf"

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

_needs_compose = needs(_COMPOSE, _MON_COMPOSE)
_needs_conf = needs(_SCRIPT, _CONF)


def _gw_priority(cfg: dict) -> int:
    return int(cfg.get("gw_priority") or 0)


# -- the two dedicated networks ------------------------------------------------


@_needs_compose
@pytest.mark.parametrize("compose_path,key,name,bridge,expected", _NETWORKS)
def test_network_declaration(compose_path, key, name, bridge, expected):
    decl = (load_mapping(compose_path).get("networks") or {}).get(key)
    where = f"{rel(compose_path)} networks.{key}"
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
    members, unparsed = network_members(compose_path, key, name)
    assert members == expected and not unparsed, (
        f"on {name}: {sorted(members)!r} (plus unparseable files naming it: {unparsed!r}); expected exactly "
        f"{sorted(expected)!r}. Every member (and every network_mode sidecar of one) gets the host ports of the "
        "network's allow line in jess-docker-input.conf."
    )


@_needs_compose
@pytest.mark.parametrize("compose_path,key,name,bridge,expected", _NETWORKS)
def test_network_is_members_default_route(compose_path, key, name, bridge, expected):
    services = load_mapping(compose_path).get("services") or {}
    for member in sorted(expected):
        svc_name = member.split(":", 1)[1]
        svc = services.get(svc_name)
        assert isinstance(svc, dict), f"{rel(compose_path)} has no services.{svc_name}; update this guard"
        assert "network_mode" not in svc, f"services.{svc_name}.network_mode is set; it must use compose networks"
        nets = service_networks(svc)
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
    fw = jess_docker_input() if _SCRIPT.is_file() else None
    declared: dict[str, list[str]] = {}
    for path, compose, _ in compose_docs():
        for key, decl in ((compose or {}).get("networks") or {}).items():
            bridge = ((decl or {}).get("driver_opts") or {}).get(_BRIDGE_OPT)
            if bridge:
                declared.setdefault(str(bridge), []).append(f"{rel(path)} networks.{key}")
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
    allows = jess_docker_input().parse_config(_CONF.read_text(encoding="utf-8")).allows
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
    mode = jess_docker_input().parse_config(_CONF.read_text(encoding="utf-8")).mode
    assert mode == "observe", (
        f"the repo jess-docker-input.conf says mode {mode}; it stays at observe as the safe default (a first "
        "install copies it to /etc). The owner switches the /etc copy to enforce by hand."
    )
