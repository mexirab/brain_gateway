"""Every alert rule must notify someone, or be listed here as Grafana-only.

Alertmanager routes by an alertname ALLOW-LIST (monitoring/alertmanager/
alertmanager.yml.template): a rule that no matcher names falls through to the
root receiver "null" and notifies nobody, whatever its `severity` label says.
That is how DiskSpaceLow, TTSDown/STTDown and four other rules sat silent until
2026-10-27, and how every UPS* alert did until 2026-10-21.

This test reads every Prometheus rule (monitoring/prometheus/alert-rules.yml)
and Loki ruler rule (monitoring/loki/rules/), walks the Alertmanager route
tree the way Alertmanager does (first match wins unless `continue: true`;
regex matchers are anchored), and fails when an alert reaches no notifying
receiver and is not in GRAFANA_ONLY below. The template is evaluated with
every optional BEGIN/END block present (the fullest render). Only the rule's
static labels are known here, so a route matching on a label the query
produces at runtime can't be judged — routes in this file match alertname.

Print the whole table (alert -> receivers) on the host:
    python3 orchestrator/tests/test_alert_routing.py

Pure Python + PyYAML, no docker or amtool; skips where monitoring/ isn't
mounted (the throwaway orchestrator-image container mounts only orchestrator/).
Host run: `python3 -m pytest --noconftest orchestrator/tests/test_alert_routing.py`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_MON = _REPO / "monitoring"
_TEMPLATE = _MON / "alertmanager" / "alertmanager.yml.template"
_PROM_RULES = _MON / "prometheus" / "alert-rules.yml"
_LOKI_RULES = _MON / "loki" / "rules"

# Receivers that reach no human: "null" drops the alert, and the
# self-investigator webhook only feeds the orchestrator's read-only diagnosis.
# Every other receiver (pushover, pushover-quiet, a dead-man's-switch webhook
# that pages from another box) counts as notifying.
NON_NOTIFYING = frozenset({"null", "self-investigator"})

# Alerts that deliberately notify nobody (Prometheus / Grafana only), each with
# its reason. Adding a name here is a decision, not a way to make CI pass.
GRAFANA_ONLY = {
    "UranusDown": "known-dead test box, excluded from NodeDown; exists so uranus stays visible",
    "AlertmanagerUnreachable": "fires when Prometheus can't reach Alertmanager, so routing it there is circular",
    "HighRequestLatency": "slow-turn trend at a few turns a day; dashboard signal (monitoring/README.md)",
    "AppDown": "every tier=app target (actual, grist, paperless, radicale) has its own routed *Down alert",
    "RoutineAutoEnded": "the force-end is the designed recovery for an ignored step; ~2 a week, not a fault",
}

_MATCHER_RE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"\s*$')
_PLAIN_NAMES_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\|[A-Za-z_][A-Za-z0-9_]*)*$")

pytestmark = pytest.mark.skipif(
    not (_TEMPLATE.is_file() and _PROM_RULES.is_file()),
    reason="monitoring/ not mounted",
)


def _load_rules() -> dict[str, dict[str, str]]:
    """alertname -> static labels (alertname included), from every rule file."""
    files = [_PROM_RULES]
    if _LOKI_RULES.is_dir():
        files += sorted(p for p in _LOKI_RULES.rglob("*") if p.suffix in (".yml", ".yaml"))
    rules: dict[str, dict[str, str]] = {}
    for path in files:
        for group in (yaml.safe_load(path.read_text()) or {}).get("groups") or []:
            for rule in group.get("rules") or []:
                if "alert" in rule:
                    labels = {k: str(v) for k, v in (rule.get("labels") or {}).items()}
                    rules[rule["alert"]] = {**labels, "alertname": rule["alert"]}
    return rules


def _parse_matcher(text: str) -> tuple[str, str, str]:
    m = _MATCHER_RE.match(text)
    if not m:
        raise ValueError(f'can\'t parse matcher {text!r} — quote the value: label="value"')
    name, op, value = m.groups()
    return name, op, re.sub(r"\\(.)", r"\1", value)


def _route_matchers(route: dict) -> list[tuple[str, str, str]]:
    out = [_parse_matcher(m) for m in route.get("matchers") or []]
    # Deprecated forms, still accepted by Alertmanager.
    out += [(k, "=", str(v)) for k, v in (route.get("match") or {}).items()]
    out += [(k, "=~", str(v)) for k, v in (route.get("match_re") or {}).items()]
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


def _match_route(route: dict, labels: dict[str, str], parent_receiver: str | None) -> list[str]:
    """Receivers for `labels` under `route` — Alertmanager's Route.Match()."""
    if not _matches(_route_matchers(route), labels):
        return []
    receiver = route.get("receiver") or parent_receiver
    found: list[str] = []
    for child in route.get("routes") or []:
        hit = _match_route(child, labels, receiver)
        found += hit
        if hit and not child.get("continue", False):
            break
    return found or [receiver]


def _config() -> dict:
    return yaml.safe_load(_TEMPLATE.read_text())


def routing_table() -> dict[str, list[str]]:
    """alertname -> receivers it reaches, for every rule in the repo."""
    root = _config()["route"]
    return {name: _match_route(root, labels, None) for name, labels in sorted(_load_rules().items())}


def _notifies(receivers: list[str]) -> bool:
    return any(r not in NON_NOTIFYING for r in receivers)


def _named_alertnames(matchers: list[tuple[str, str, str]]) -> set[str]:
    """Alertnames a positive alertname matcher lists (plain names / A|B|C only)."""
    names: set[str] = set()
    for name, op, value in matchers:
        if name != "alertname" or op not in ("=", "=~"):
            continue
        if op == "=~" and not _PLAIN_NAMES_RE.match(value):
            raise ValueError(f"alertname regex {value!r} isn't a plain A|B|C list — this test can't check it")
        names |= set(value.split("|"))
    return names


def _all_routes(route: dict):
    yield route
    for child in route.get("routes") or []:
        yield from _all_routes(child)


# --------------------------------------------------------------------------


def test_rules_and_routes_were_found():
    rules = _load_rules()
    assert len(rules) > 50, "rule files parsed to almost nothing — the test itself is broken"
    assert "HeliosHardwareFault" in rules, "Loki ruler rules not read"
    assert "null" in {r["name"] for r in _config()["receivers"]}


def test_route_walk_matches_alertmanager_semantics():
    # Guards the evaluator itself: continue, first-match-wins, inheritance,
    # anchored regex, and the root fallback.
    tree = {
        "receiver": "null",
        "routes": [
            {"matchers": ['alertname=~"A|B"'], "receiver": "copy", "continue": True},
            {"matchers": ['alertname=~"B|C"'], "receiver": "loud"},
            {"matchers": ['alertname="C"'], "receiver": "never"},
            {"matchers": ['alertname="D"'], "routes": [{"matchers": ['severity!="x"']}]},
        ],
    }
    assert _match_route(tree, {"alertname": "A"}, None) == ["copy"]
    assert _match_route(tree, {"alertname": "B"}, None) == ["copy", "loud"]
    assert _match_route(tree, {"alertname": "C"}, None) == ["loud"]
    assert _match_route(tree, {"alertname": "D"}, None) == ["null"]
    assert _match_route(tree, {"alertname": "AB"}, None) == ["null"]  # anchored
    assert _match_route(tree, {"alertname": "E"}, None) == ["null"]


def test_every_alert_notifies_someone_or_is_grafana_only():
    silent = sorted(
        f"{name} -> {receivers}"
        for name, receivers in routing_table().items()
        if not _notifies(receivers) and name not in GRAFANA_ONLY
    )
    assert not silent, (
        "These alerts reach no notifying receiver, so they notify NOBODY. Name each in a matcher in "
        "monitoring/alertmanager/alertmanager.yml.template (pushover = pages, pushover-quiet = doesn't), "
        "or add it to GRAFANA_ONLY in this file with a reason:\n  " + "\n  ".join(silent)
    )


def test_grafana_only_alerts_exist_and_stay_unrouted():
    table = routing_table()
    missing = sorted(n for n in GRAFANA_ONLY if n not in table)
    assert not missing, f"GRAFANA_ONLY names no rule defines (renamed or deleted? drop them): {missing}"
    routed = sorted(n for n in GRAFANA_ONLY if _notifies(table[n]))
    assert not routed, f"GRAFANA_ONLY alerts that a route now notifies — drop them from GRAFANA_ONLY: {routed}"


def test_every_alertname_in_routes_and_inhibits_exists():
    # A typo in an allow-list regex routes nothing and fails silently.
    rules = set(_load_rules())
    config = _config()
    named: set[str] = set()
    for route in _all_routes(config["route"]):
        named |= _named_alertnames(_route_matchers(route))
    for inhibit in config.get("inhibit_rules") or []:
        for key in ("source_matchers", "target_matchers"):
            named |= _named_alertnames([_parse_matcher(m) for m in inhibit.get(key) or []])
    unknown = sorted(named - rules)
    assert not unknown, f"Alertmanager names alerts that no rule defines (typo or stale): {unknown}"


if __name__ == "__main__":
    width = max(len(n) for n in _load_rules())
    for alert, receivers in routing_table().items():
        mark = (
            "" if _notifies(receivers) else ("  (Grafana-only)" if alert in GRAFANA_ONLY else "  <-- NOTIFIES NOBODY")
        )
        print(f"{alert:<{width}}  {', '.join(receivers)}{mark}")
    sys.exit(0)
