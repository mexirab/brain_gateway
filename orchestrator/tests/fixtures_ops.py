"""Shared pytest fixtures and plain helpers for the ops test files.

conftest.py star-imports this module, so every fixture listed in ``__all__``
is available to every test file. Give fixtures names unique across the suite
(prefix with the area) — a clash with another fixtures_*.py module is caught
by conftest at import time. Plain helper functions can be imported directly.

Helpers here (imported by name, never fixtures):

- ``jess_docker_input()`` / ``kiosk_module()``: the host scripts, loaded once
  per session and shared by the files that test them;
- the compose-layout guard helpers used by test_searxng_hardening.py and
  test_container_firewall_layout.py;
- the Alertmanager route walk used by test_alert_routing.py and
  test_deploy_drift.py;
- ``Proxy`` / ``first_existing`` for the kiosk tests.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from orchestrator.tests._helpers import REPO_ROOT, load_script

__all__: list[str] = []

# ---------------------------------------------------------------------------
# Host scripts, loaded once per session
# ---------------------------------------------------------------------------
# These module objects (and compose_docs() below) are ONE copy shared by every
# test file: change them only through monkeypatch (which restores), never by
# assigning to them directly, or the change leaks into later files.

FIREWALL_DIR = REPO_ROOT / "scripts" / "container-firewall"
KIOSK_DIR = REPO_ROOT / "scripts" / "jupiter-kiosk"


@cache
def jess_docker_input() -> ModuleType:
    """scripts/container-firewall/jess_docker_input.py (tests monkeypatch it; monkeypatch restores)."""
    return load_script(FIREWALL_DIR / "jess_docker_input.py", "jess_docker_input_under_test")


@cache
def kiosk_module() -> ModuleType:
    """scripts/jupiter-kiosk/kiosk.py, shared by the kiosk and make_logins tests."""
    return load_script(KIOSK_DIR / "kiosk.py", "jupiter_kiosk")


class Proxy:
    """A module stand-in: the given attributes, everything else from the real module."""

    def __init__(self, real, **override):
        self._real = real
        self.__dict__.update(override)

    def __getattr__(self, name):
        return getattr(self._real, name)


def first_existing(*candidates: Path) -> Path | None:
    return next((c for c in candidates if c.is_file()), None)


# ---------------------------------------------------------------------------
# Compose layout guards (searxng hardening, container -> host firewall)
# ---------------------------------------------------------------------------

COMPOSE = REPO_ROOT / "docker-compose.yml"
MON_COMPOSE = REPO_ROOT / "monitoring" / "docker-compose.yml"


def rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def needs(*paths: Path):
    """skipif marker for guards that need files only a full checkout has."""
    missing = [rel(p) for p in paths if not p.is_file()]
    return pytest.mark.skipif(bool(missing), reason=f"needs a full repo checkout (missing: {', '.join(missing)})")


def load_mapping(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        pytest.fail(f"{rel(path)} no longer parses as YAML ({exc}); fix it or switch this guard to a text check")
    assert isinstance(data, dict), f"{rel(path)} did not parse to a mapping"
    return data


def service_networks(svc: dict) -> dict[str, dict]:
    """A service's networks as {key: per-network config}, for the list and the mapping form."""
    nets = svc.get("networks") or {}
    if isinstance(nets, list):
        return {str(n): {} for n in nets}
    return {str(k): (v or {}) for k, v in nets.items()}


def real_net_name(compose: dict, key: str) -> str:
    """Docker name of a network key: its `name:` (or old `external: {name: ...}`), else the key itself."""
    decl = (compose.get("networks") or {}).get(key) or {}
    ext = decl.get("external")
    return str(decl.get("name") or (ext.get("name") if isinstance(ext, dict) else None) or key)


@cache
def compose_docs() -> tuple[tuple[Path, dict | None, str], ...]:
    """(path, parsed mapping or None when plain YAML can't load it, raw text) for every repo compose file
    (main, monitoring, grist, actual, radicale, HA, ...). None for files using compose's own tags
    (!reset / !override): callers fall back to the raw text."""
    found = []
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", ".claude", ".next"}]
        found += [Path(root) / f for f in files if re.fullmatch(r"(docker-)?compose[\w.-]*\.ya?ml", f)]
    assert COMPOSE in found and MON_COMPOSE in found, "the compose-file walk missed a known file; fix the walk"
    out = []
    for path in sorted(found):
        text = path.read_text(encoding="utf-8")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            data = None
        out.append((path, data if isinstance(data, dict) else None, text))
    return tuple(out)


def network_members(defining: Path, key: str, fixed_name: str) -> tuple[set[str], list[str]]:
    """Who is on a fixed-name network across every repo compose file, as "<file>:<service>".

    Counts a service whose network resolves to `fixed_name` (or is `key` in the defining file), and a
    sidecar sharing a member's network namespace (network_mode service:<member> in the same file, or
    container:<member's container> in any file). Also returns unparseable files that mention the name."""
    members: set[str] = set()
    unparsed: list[str] = []
    containers: set[str] = set()
    docs = compose_docs()
    for path, compose, text in docs:
        if compose is None:
            if fixed_name in text:
                unparsed.append(rel(path))
            continue
        services = {n: s for n, s in (compose.get("services") or {}).items() if isinstance(s, dict)}
        on = {
            n
            for n, svc in services.items()
            if any(
                real_net_name(compose, k) == fixed_name or (path == defining and k == key)
                for k in service_networks(svc)
            )
        }
        members |= {f"{rel(path)}:{n}" for n in on}
        containers |= {str(services[n].get("container_name") or n) for n in on}
        modes = {f"service:{n}" for n in on}
        members |= {f"{rel(path)}:{n}" for n, svc in services.items() if str(svc.get("network_mode") or "") in modes}
    modes = {f"container:{c}" for c in containers}
    for path, compose, _ in docs:
        for n, svc in ((compose or {}).get("services") or {}).items():
            if isinstance(svc, dict) and str(svc.get("network_mode") or "") in modes:
                members.add(f"{rel(path)}:{n}")
    return members, unparsed


# ---------------------------------------------------------------------------
# Alertmanager route walk (test_alert_routing.py documents the semantics)
# ---------------------------------------------------------------------------

MON = REPO_ROOT / "monitoring"
AM_TEMPLATE = MON / "alertmanager" / "alertmanager.yml.template"
PROM_CONFIG = MON / "prometheus" / "prometheus.yml.template"
_PROM_DIR_IN_CONTAINER = "/etc/prometheus/"  # monitoring/prometheus is mounted here
_LOKI_RULES = MON / "loki" / "rules"

_MATCHER_RE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*(?:"((?:[^"\\]|\\.)*)"|([^"\s,{}]*))\s*$')
_PLAIN_NAMES_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\|[A-Za-z_][A-Za-z0-9_]*)*$")
_OPTIONAL_BLOCK_RE = re.compile(r"^[ \t]*# BEGIN .*?^[ \t]*# END [^\n]*\n", re.M | re.S)


def rule_files() -> list[Path]:
    """Every Prometheus rule file (`rule_files` in prometheus.yml.template) and Loki ruler file."""
    files: list[Path] = []
    for entry in yaml.safe_load(PROM_CONFIG.read_text()).get("rule_files") or []:
        assert entry.startswith(_PROM_DIR_IN_CONTAINER), f"rule_files entry outside {_PROM_DIR_IN_CONTAINER}: {entry}"
        found = sorted((MON / "prometheus").glob(entry[len(_PROM_DIR_IN_CONTAINER) :]))
        assert found, f"rule_files entry {entry} matches no file under monitoring/prometheus/"
        files += found
    if _LOKI_RULES.is_dir():
        files += sorted(p for p in _LOKI_RULES.rglob("*") if p.is_file() and not p.name.startswith("."))
    return files


def load_rules() -> dict[str, list[dict[str, str]]]:
    """alertname -> static labels (alertname included) of each rule with that name."""
    rules: dict[str, list[dict[str, str]]] = {}
    for path in rule_files():
        for group in (yaml.safe_load(path.read_text()) or {}).get("groups") or []:
            for rule in group.get("rules") or []:
                if "alert" in rule:
                    labels = {k: str(v) for k, v in (rule.get("labels") or {}).items()}
                    rules.setdefault(rule["alert"], []).append({**labels, "alertname": rule["alert"]})
    return rules


def _unescape(value: str) -> str:
    # Alertmanager unescapes only \" \\ \n; other escapes stay regex escapes.
    return re.sub(r'\\(["\\n])', lambda m: "\n" if m.group(1) == "n" else m.group(1), value)


def parse_matcher(text: str) -> tuple[str, str, str]:
    m = _MATCHER_RE.match(text)
    if not m:
        raise ValueError(f'this test only reads one label="value" matcher per list entry, not {text!r}')
    name, op, quoted, bare = m.groups()
    return name, op, _unescape(quoted) if quoted is not None else bare


def route_matchers(route: dict) -> list[tuple[str, str, str]]:
    out = [parse_matcher(m) for m in route.get("matchers") or []]
    # Deprecated forms, still accepted by Alertmanager.
    out += [(k, "=", str(v)) for k, v in (route.get("match") or {}).items()]
    out += [(k, "=~", str(v)) for k, v in (route.get("match_re") or {}).items()]
    return out


def inhibit_matchers(inhibit: dict, side: str) -> list[tuple[str, str, str]]:
    out = [parse_matcher(m) for m in inhibit.get(f"{side}_matchers") or []]
    out += [(k, "=", str(v)) for k, v in (inhibit.get(f"{side}_match") or {}).items()]
    out += [(k, "=~", str(v)) for k, v in (inhibit.get(f"{side}_match_re") or {}).items()]
    return out


def _matches(matchers: list[tuple[str, str, str]], labels: dict[str, str]) -> bool:
    for name, op, value in matchers:
        actual = labels.get(name, "")  # Alertmanager: a missing label is ""
        if op == "=":
            ok = actual == value
        elif op == "!=":
            ok = actual != value
        elif op == "=~":
            ok = re.fullmatch(value, actual) is not None
        else:
            ok = re.fullmatch(value, actual) is None
        if not ok:
            return False
    return True


def match_route(route: dict, labels: dict[str, str], parent_receiver: str | None) -> list[str]:
    """Receivers for `labels` under `route` — Alertmanager's Route.Match()."""
    if not _matches(route_matchers(route), labels):
        return []
    receiver = route.get("receiver") or parent_receiver
    found: list[str] = []
    for child in route.get("routes") or []:
        hit = match_route(child, labels, receiver)
        found += hit
        if hit and not child.get("continue", False):
            break
    return found or [receiver]


def am_config(strip_optional: bool = False) -> dict:
    """The Alertmanager template: with every optional BEGIN/END block, or with all of them stripped."""
    text = AM_TEMPLATE.read_text()
    if strip_optional:
        text = _OPTIONAL_BLOCK_RE.sub("", text)
    return yaml.safe_load(text)


def routing(config: dict) -> dict[str, list[list[str]]]:
    """alertname -> receivers reached, one list per rule with that name."""
    root = config["route"]
    return {
        name: [match_route(root, labels, None) for labels in variants]
        for name, variants in sorted(load_rules().items())
    }


def all_routes(route: dict) -> Iterator[dict]:
    yield route
    for child in route.get("routes") or []:
        yield from all_routes(child)


def named_alertnames(matchers: list[tuple[str, str, str]]) -> set[str]:
    """Alertnames a positive alertname matcher lists (plain names / A|B|C only)."""
    names: set[str] = set()
    for name, op, value in matchers:
        if name != "alertname" or op not in ("=", "=~"):
            continue
        if op == "=~" and not _PLAIN_NAMES_RE.match(value):
            raise ValueError(f"alertname regex {value!r} isn't a plain A|B|C list — this test can't check it")
        names |= set(value.split("|"))
    return names
