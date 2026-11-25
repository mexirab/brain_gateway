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

import sys
from pathlib import Path

import pytest

if __package__ in (None, ""):  # run as a script (see above): make `orchestrator` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchestrator.tests.fixtures_ops import (
    AM_TEMPLATE,
    MON,
    PROM_CONFIG,
    all_routes,
    am_config,
    inhibit_matchers,
    load_rules,
    match_route,
    named_alertnames,
    parse_matcher,
    route_matchers,
    routing,
)

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

pytestmark = pytest.mark.skipif(not MON.is_dir(), reason="monitoring/ not mounted")


def _human_receivers(config: dict) -> set[str]:
    """Receivers that reach a person: have *_configs, not machine or heartbeat."""
    return {
        r["name"]
        for r in config["receivers"]
        if any(k.endswith("_configs") for k in r) and r["name"] not in MACHINE_ONLY | HEARTBEAT_ONLY.keys()
    }


def _heartbeat_alerts() -> frozenset[str]:
    return frozenset().union(*HEARTBEAT_ONLY.values())


# --------------------------------------------------------------------------


def test_rules_and_routes_were_found():
    assert AM_TEMPLATE.is_file() and PROM_CONFIG.is_file(), "alertmanager/prometheus template moved — update this test"
    rules = load_rules()
    assert len(rules) > 50, "rule files parsed to almost nothing — the test itself is broken"
    assert "HeliosHardwareFault" in rules, "Loki ruler rules not read"
    assert "OrchestratorDown" in rules, "Prometheus rules not read"
    assert _human_receivers(am_config()) >= {"pushover", "pushover-quiet"}


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
    assert match_route(tree, {"alertname": "A"}, None) == ["copy"]
    assert match_route(tree, {"alertname": "B"}, None) == ["copy", "loud"]
    assert match_route(tree, {"alertname": "C"}, None) == ["loud"]
    assert match_route(tree, {"alertname": "D"}, None) == ["null"]
    assert match_route(tree, {"alertname": "AB"}, None) == ["null"]  # anchored
    assert match_route(tree, {"alertname": "E"}, None) == ["bare"]
    assert match_route(tree, {"alertname": "F1"}, None) == ["digit"]  # \d stays a regex escape
    assert match_route(tree, {"alertname": "Fd"}, None) == ["null"]
    assert match_route(tree, {"alertname": "G"}, None) == ["null"]
    assert parse_matcher(r'x="a\"b\\c"') == ("x", "=", 'a"b\\c')


def test_route_matchers_only_use_labels_known_statically():
    # Labels the query produces at runtime (node, job, tool...) are unknown
    # here and would read as "", so a negative matcher on one always "passes".
    rules = load_rules()
    static = set.intersection(*(set(labels) for variants in rules.values() for labels in variants))
    for strip in (False, True):
        for route in all_routes(am_config(strip)["route"]):
            used = {name for name, _, _ in route_matchers(route)}
            assert used <= static, (
                f"route matches on {sorted(used - static)}, which not every rule sets statically — "
                "this test can't judge it; match on alertname or extend the test"
            )


def test_every_alert_notifies_someone_or_is_grafana_only():
    for strip in (False, True):
        config = am_config(strip)
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
    config = am_config()
    names = {r["name"] for r in config["receivers"]}
    for name, per_rule in routing(config).items():
        for receivers in per_rule:
            for hb, allowed in HEARTBEAT_ONLY.items():
                if hb in receivers:
                    assert name in allowed, f"{name} is routed to heartbeat receiver {hb!r}, which drops it"
                if name in allowed and hb in names:
                    assert receivers == [hb], f"{name} must reach only {hb!r}, not {receivers}"


def test_grafana_only_alerts_exist_and_stay_unrouted():
    table = routing(am_config())
    missing = sorted(n for n in GRAFANA_ONLY if n not in table)
    assert not missing, f"GRAFANA_ONLY names no rule defines (renamed or deleted? drop them): {missing}"
    human = _human_receivers(am_config())
    routed = sorted(n for n in GRAFANA_ONLY if any(human & set(r) for r in table[n]))
    assert not routed, f"GRAFANA_ONLY alerts that a route now notifies — drop them from GRAFANA_ONLY: {routed}"


def test_every_route_receiver_is_defined():
    for strip in (False, True):
        config = am_config(strip)
        names = {r["name"] for r in config["receivers"]}
        used = {route["receiver"] for route in all_routes(config["route"]) if "receiver" in route}
        assert used <= names, f"routes name undefined receivers: {sorted(used - names)}"


def test_every_alertname_in_routes_and_inhibits_exists():
    # A typo in an allow-list regex routes nothing and fails silently.
    rules = set(load_rules())
    config = am_config()
    named: set[str] = set()
    for route in all_routes(config["route"]):
        named |= named_alertnames(route_matchers(route))
    for inhibit in config.get("inhibit_rules") or []:
        for side in ("source", "target"):
            named |= named_alertnames(inhibit_matchers(inhibit, side))
    unknown = sorted(named - rules)
    assert not unknown, f"Alertmanager names alerts that no rule defines (typo or stale): {unknown}"


if __name__ == "__main__":
    config = am_config()
    human = _human_receivers(config)
    width = max(len(n) for n in load_rules())
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
