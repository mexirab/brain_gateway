"""Static guards for coder-net (docker-compose.yml), the coder boxes' network.

The boxes run model-written shell commands, so their network is the safety
boundary (docs/internal/CODER_PLAN.md, measured in CODER_SPIKE.md section 1):

- internal + gateway_mode_ipv4=isolated: no route out and no host address on
  the bridge. A plain internal network still reaches Jupiter's SSH, Pi-hole
  DNS (a way to the internet), SMB, Home Assistant and Netdata through its
  bridge address.
- No IPv6, fixed name (boxes join it with `docker run --network <name>`),
  fixed subnet outside Docker's default pools, fixed bridge name.
- The orchestrator is its only compose member, at the address the coder gate
  serves on (config.coder_gate_address). Anything else on it would be
  reachable from every box.

Needs a full checkout, like test_searxng_hardening.py; skips otherwise.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_COMPOSE = _REPO / "docker-compose.yml"
_NET = "coder-net"

pytestmark = pytest.mark.skipif(not _COMPOSE.is_file(), reason="needs a full repo checkout (docker-compose.yml)")


def _compose() -> dict:
    data = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _decl() -> dict:
    decl = (_compose().get("networks") or {}).get(_NET)
    assert isinstance(decl, dict), "coder-net is gone from docker-compose.yml"
    return decl


def test_network_is_internal_and_isolated():
    decl = _decl()
    assert decl.get("internal") is True
    assert decl.get("enable_ipv6") is False
    assert not decl.get("external")
    opts = decl.get("driver_opts") or {}
    assert opts.get("com.docker.network.bridge.gateway_mode_ipv4") == "isolated"
    assert opts.get("com.docker.network.bridge.name") == "br-jesscoder"
    assert decl.get("driver", "bridge") == "bridge"


def _default(value: str, var: str) -> str:
    """`${VAR:-default}` -> default, checking the variable name (the orchestrator
    reads the same one, so compose and the gate can't drift)."""
    m = re.fullmatch(r"\$\{(\w+):-([^}]+)\}", str(value))
    assert m, f"expected ${{{var}:-...}}, got {value!r}"
    assert m.group(1) == var
    return m.group(2)


def _setting_default(name: str) -> str:
    from orchestrator.config import Settings

    return Settings.model_fields[name].default


def test_fixed_name_and_layout():
    decl = _decl()
    assert decl.get("name") == "gateway_nerves_coder-net"
    configs = (decl.get("ipam") or {}).get("config") or []
    assert len(configs) == 1
    subnet = ipaddress.IPv4Network(_default(configs[0]["subnet"], "CODER_SUBNET"))
    box_range = ipaddress.IPv4Network(_default(configs[0]["ip_range"], "CODER_BOX_RANGE"))
    gate = ipaddress.IPv4Address(_setting_default("coder_gate_address"))
    assert str(subnet) == _setting_default("coder_subnet")
    assert box_range.subnet_of(subnet)
    assert gate in subnet and gate not in box_range, "boxes could be handed the gate address"


def test_orchestrator_is_the_only_member():
    services = _compose().get("services") or {}
    members = []
    for name, svc in services.items():
        nets = svc.get("networks") or []
        keys = nets.keys() if isinstance(nets, dict) else nets
        if _NET in keys:
            members.append(name)
        # A sidecar in the orchestrator's network namespace would sit on coder-net too.
        assert svc.get("network_mode") not in ("service:orchestrator", "container:brain-orchestrator"), name
    assert members == ["orchestrator"]


def test_orchestrator_address_matches_gate_setting():
    nets = _compose()["services"]["orchestrator"].get("networks") or {}
    assert isinstance(nets, dict), "the orchestrator needs a fixed coder-net address (mapping form)"
    address = (nets.get(_NET) or {}).get("ipv4_address")
    assert _default(address, "CODER_GATE_ADDRESS") == _setting_default("coder_gate_address")


def test_no_other_compose_project_declares_it():
    for path in _REPO.glob("*/docker-compose*.yml"):
        text = path.read_text(encoding="utf-8")
        assert "coder-net" not in text, f"{path.relative_to(_REPO)} mentions coder-net"
