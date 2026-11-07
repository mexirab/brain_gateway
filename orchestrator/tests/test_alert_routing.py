"""Every alert rule must notify someone, or be listed here as Grafana-only.

Alertmanager routes by an alertname ALLOW-LIST (monitoring/alertmanager/
alertmanager.yml.template): a rule that no matcher names falls through to the
root receiver "null" and notifies nobody, whatever its `severity` label says.
That is how DiskSpaceLow, TTSDown/STTDown and four other rules sat silent until
2026-10-27, and how every UPS* alert did until 2026-10-21.

This test reads every Prometheus rule file (`rule_files` in
monitoring/prometheus/prometheus.yml.template) and Loki ruler file
(monitoring/loki/rules/), walks the Alertmanager route tree the way
Alertmanager does (first match wins unless `continue: true`; regex matchers
are anchored; a missing label is ""), and fails when an alert reaches no human
receiver and is not in GRAFANA_ONLY below. The template is walked twice: with
every optional BEGIN/END block (the fullest render) and with all of them
stripped (what generate-configs.sh renders without the optional tokens).

Known limits: only a rule's static labels are known, so route matchers may
use only labels every rule sets statically (enforced below); inhibit rules and
mute/active time intervals are not modelled, so an alert that is routed but
always inhibited still passes.

Print the whole table (alert -> receivers) on the host:
    python3 orchestrator/tests/test_alert_routing.py

Pure Python + PyYAML, no docker or amtool; skips where monitoring/ isn't
mounted (the throwaway orchestrator-image container mounts only orchestrator/).
Host run: `python3 -m pytest --noconftest orchestrator/tests/test_alert_routing.py`.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_MON = _REPO / "monitoring"
_TEMPLATE = _MON / "alertmanager" / "alertmanager.yml.template"
_PROM_CONFIG = _MON / "prometheus" / "prometheus.yml.template"
_PROM_DIR_IN_CONTAINER = "/etc/prometheus/"  # monitoring/prometheus is mounted here
_LOKI_RULES = _MON / "loki" / "rules"

# Receivers with configs that still reach no human: the self-investigator
# webhook only feeds the orchestrator's read-only diagnosis. Receivers with no
# *_configs at all ("null") are found from the template.
MACHINE_ONLY = frozenset({"self-investigator"})

# Heartbeat receivers and the only alerts allowed to reach them. The dead-man's
# switch pages from another box when Watchdog heartbeats STOP, and ignores any
# other alert — so Watchdog must reach it and nothing else, and anything else
# routed there would be silently dropped.
HEARTBEAT_ONLY = {"deadman": frozenset({"Watchdog"})}

# Alerts that deliberately notify nobody (Prometheus / Grafana only), each with
# its reason. Adding a name here is a decision, not a way to make CI pass.
GRAFANA_ONLY = {
    "UranusDown": "known-dead test box, excluded from NodeDown; exists so uranus stays visible",
    "AlertmanagerUnreachable": "fires when Prometheus can't reach Alertmanager, so routing it there is circular",
    "HighRequestLatency": "slow-turn trend at a few turns a day; dashboard signal (monitoring/README.md)",
    "AppDown": "every tier=app target (actual, grist, paperless, radicale) has its own routed *Down alert",
    "RoutineAutoEnded": "the force-end is the designed recovery for an ignored step, not a fault",
}

_MATCHER_RE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*(?:"((?:[^"\\]|\\.)*)"|([^"\s,{}]*))\s*$')
_PLAIN_NAMES_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\|[A-Za-z_][A-Za-z0-9_]*)*$")
_OPTIONAL_BLOCK_RE = re.compile(r"^[ \t]*# BEGIN .*?^[ \t]*# END [^\n]*\n", re.M | re.S)

pytestmark = pytest.mark.skipif(not _MON.is_dir(), reason="monitoring/ not mounted")


def _rule_files() -> list[Path]:
    files: list[Path] = []
    for entry in yaml.safe_load(_PROM_CONFIG.read_text()).get("rule_files") or []:
        assert entry.startswith(_PROM_DIR_IN_CONTAINER), f"rule_files entry outside {_PROM_DIR_IN_CONTAINER}: {entry}"
        found = sorted((_MON / "prometheus").glob(entry[len(_PROM_DIR_IN_CONTAINER) :]))
        assert found, f"rule_files entry {entry} matches no file under monitoring/prometheus/"
        files += found
    if _LOKI_RULES.is_dir():
        files += sorted(p for p in _LOKI_RULES.rglob("*") if p.is_file() and not p.name.startswith("."))
    return files


def _load_rules() -> dict[str, list[dict[str, str]]]:
    """alertname -> static labels (alertname included) of each rule with that name."""
    rules: dict[str, list[dict[str, str]]] = {}
    for path in _rule_files():
        for group in (yaml.safe_load(path.read_text()) or {}).get("groups") or []:
            for rule in group.get("rules") or []:
                if "alert" in rule:
                    labels = {k: str(v) for k, v in (rule.get("labels") or {}).items()}
                    rules.setdefault(rule["alert"], []).append({**labels, "alertname": rule["alert"]})
    return rules


def _unescape(value: str) -> str:
    # Alertmanager unescapes only \" \\ \n; other escapes stay regex escapes.
    return re.sub(r'\\(["\\n])', lambda m: "\n" if m.group(1) == "n" else m.group(1), value)


def _parse_matcher(text: str) -> tuple[str, str, str]:
    m = _MATCHER_RE.match(text)
    if not m:
        raise ValueError(f'this test only reads one label="value" matcher per list entry, not {text!r}')
    name, op, quoted, bare = m.groups()
    return name, op, _unescape(quoted) if quoted is not None else bare


def _route_matchers(route: dict) -> list[tuple[str, str, str]]:
    out = [_parse_matcher(m) for m in route.get("matchers") or []]
    # Deprecated forms, still accepted by Alertmanager.
    out += [(k, "=", str(v)) for k, v in (route.get("match") or {}).items()]
    out += [(k, "=~", str(v)) for k, v in (route.get("match_re") or {}).items()]
    return out


def _inhibit_matchers(inhibit: dict, side: str) -> list[tuple[str, str, str]]:
    out = [_parse_matcher(m) for m in inhibit.get(f"{side}_matchers") or []]
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


def _config(strip_optional: bool = False) -> dict:
    text = _TEMPLATE.read_text()
    if strip_optional:
        text = _OPTIONAL_BLOCK_RE.sub("", text)
    return yaml.safe_load(text)


def _human_receivers(config: dict) -> set[str]:
    """Receivers that reach a person: have *_configs, not machine or heartbeat."""
    return {
        r["name"]
        for r in config["receivers"]
        if any(k.endswith("_configs") for k in r) and r["name"] not in MACHINE_ONLY | HEARTBEAT_ONLY.keys()
    }


def routing(config: dict) -> dict[str, list[list[str]]]:
    """alertname -> receivers reached, one list per rule with that name."""
    root = config["route"]
    return {
        name: [_match_route(root, labels, None) for labels in variants]
        for name, variants in sorted(_load_rules().items())
    }


def _heartbeat_alerts() -> frozenset[str]:
    return frozenset().union(*HEARTBEAT_ONLY.values())


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


def _all_routes(route: dict) -> Iterator[dict]:
    yield route
    for child in route.get("routes") or []:
        yield from _all_routes(child)


# --------------------------------------------------------------------------


def test_rules_and_routes_were_found():
    assert _TEMPLATE.is_file() and _PROM_CONFIG.is_file(), "alertmanager/prometheus template moved — update this test"
    rules = _load_rules()
    assert len(rules) > 50, "rule files parsed to almost nothing — the test itself is broken"
    assert "HeliosHardwareFault" in rules, "Loki ruler rules not read"
    assert "OrchestratorDown" in rules, "Prometheus rules not read"
    assert _human_receivers(_config()) >= {"pushover", "pushover-quiet"}


def test_route_walk_matches_alertmanager_semantics():
    # Guards the evaluator itself: continue, first-match-wins, inheritance,
    # anchored regex, the root fallback and Alertmanager's escape handling.
    tree = {
        "receiver": "null",
        "routes": [
            {"matchers": ['alertname=~"A|B"'], "receiver": "copy", "continue": True},
            {"matchers": ['alertname=~"B|C"'], "receiver": "loud"},
            {"matchers": ['alertname="C"'], "receiver": "never"},
            {"matchers": ['alertname="D"'], "routes": [{"matchers": ['severity!="x"']}]},
            {"matchers": ["alertname=E"], "receiver": "bare"},
            {"matchers": ['alertname=~"F\\d"'], "receiver": "digit"},
        ],
    }
    assert _match_route(tree, {"alertname": "A"}, None) == ["copy"]
    assert _match_route(tree, {"alertname": "B"}, None) == ["copy", "loud"]
    assert _match_route(tree, {"alertname": "C"}, None) == ["loud"]
    assert _match_route(tree, {"alertname": "D"}, None) == ["null"]
    assert _match_route(tree, {"alertname": "AB"}, None) == ["null"]  # anchored
    assert _match_route(tree, {"alertname": "E"}, None) == ["bare"]
    assert _match_route(tree, {"alertname": "F1"}, None) == ["digit"]  # \d stays a regex escape
    assert _match_route(tree, {"alertname": "Fd"}, None) == ["null"]
    assert _match_route(tree, {"alertname": "G"}, None) == ["null"]
    assert _parse_matcher(r'x="a\"b\\c"') == ("x", "=", 'a"b\\c')


def test_route_matchers_only_use_labels_known_statically():
    # Labels the query produces at runtime (node, job, tool...) are unknown
    # here and would read as "", so a negative matcher on one always "passes".
    rules = _load_rules()
    static = set.intersection(*(set(labels) for variants in rules.values() for labels in variants))
    for strip in (False, True):
        for route in _all_routes(_config(strip)["route"]):
            used = {name for name, _, _ in _route_matchers(route)}
            assert used <= static, (
                f"route matches on {sorted(used - static)}, which not every rule sets statically — "
                "this test can't judge it; match on alertname or extend the test"
            )


def test_every_alert_notifies_someone_or_is_grafana_only():
    for strip in (False, True):
        config = _config(strip)
        human = _human_receivers(config)
        silent = sorted(
            f"{name} -> {receivers}"
            for name, per_rule in routing(config).items()
            if name not in GRAFANA_ONLY and name not in _heartbeat_alerts()
            for receivers in per_rule
            if not human & set(receivers)
        )
        render = "with optional blocks stripped" if strip else "with every optional block"
        assert not silent, (
            f"These alerts reach no human receiver ({render}), so they notify NOBODY. Name each in a matcher "
            "in monitoring/alertmanager/alertmanager.yml.template (pushover = pages, pushover-quiet = doesn't) "
            "outside the BEGIN/END blocks, or add it to GRAFANA_ONLY in this file with a reason:\n  "
            + "\n  ".join(silent)
        )


def test_heartbeat_receivers_get_only_their_heartbeat():
    config = _config()
    names = {r["name"] for r in config["receivers"]}
    for name, per_rule in routing(config).items():
        for receivers in per_rule:
            for hb, allowed in HEARTBEAT_ONLY.items():
                if hb in receivers:
                    assert name in allowed, f"{name} is routed to heartbeat receiver {hb!r}, which drops it"
                if name in allowed and hb in names:
                    assert receivers == [hb], f"{name} must reach only {hb!r}, not {receivers}"


def test_grafana_only_alerts_exist_and_stay_unrouted():
    table = routing(_config())
    missing = sorted(n for n in GRAFANA_ONLY if n not in table)
    assert not missing, f"GRAFANA_ONLY names no rule defines (renamed or deleted? drop them): {missing}"
    human = _human_receivers(_config())
    routed = sorted(n for n in GRAFANA_ONLY if any(human & set(r) for r in table[n]))
    assert not routed, f"GRAFANA_ONLY alerts that a route now notifies — drop them from GRAFANA_ONLY: {routed}"


def test_every_route_receiver_is_defined():
    for strip in (False, True):
        config = _config(strip)
        names = {r["name"] for r in config["receivers"]}
        used = {route["receiver"] for route in _all_routes(config["route"]) if "receiver" in route}
        assert used <= names, f"routes name undefined receivers: {sorted(used - names)}"


def test_every_alertname_in_routes_and_inhibits_exists():
    # A typo in an allow-list regex routes nothing and fails silently.
    rules = set(_load_rules())
    config = _config()
    named: set[str] = set()
    for route in _all_routes(config["route"]):
        named |= _named_alertnames(_route_matchers(route))
    for inhibit in config.get("inhibit_rules") or []:
        for side in ("source", "target"):
            named |= _named_alertnames(_inhibit_matchers(inhibit, side))
    unknown = sorted(named - rules)
    assert not unknown, f"Alertmanager names alerts that no rule defines (typo or stale): {unknown}"


if __name__ == "__main__":
    config = _config()
    human = _human_receivers(config)
    width = max(len(n) for n in _load_rules())
    for alert, per_rule in routing(config).items():
        receivers = list(dict.fromkeys(r for rs in per_rule for r in rs))
        if human & set(receivers):
            mark = ""
        elif alert in GRAFANA_ONLY:
            mark = "  (Grafana-only)"
        elif alert in _heartbeat_alerts():
            mark = "  (heartbeat)"
        else:
            mark = "  <-- NOTIFIES NOBODY"
        print(f"{alert:<{width}}  {', '.join(receivers)}{mark}")
    sys.exit(0)
