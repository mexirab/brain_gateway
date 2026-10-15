#!/bin/bash
# Brain Gateway - Monitoring Reload + Verify
# ===========================================
# Reloads Prometheus, Alertmanager and the Blackbox exporter from their
# live-mounted configs and verifies the running instances actually picked
# them up.
#
# Run after scripts/generate-configs.sh; the CI deploy job runs it whenever
# monitoring config sources change on main.
#
# Verification is CONTENT-based, not status-based: a reload can return 200
# against a stale bind mount or the wrong container — exactly how the
# pre-compose alertmanager rotted, silently dropping EveningBriefingStale
# paging during PR #54. So we assert the alert rule names and the
# alertmanager route/receivers, not just the HTTP status.
#
# Usage: ./scripts/reload-monitoring.sh
#   PROM_URL / AM_URL / BLACKBOX_URL env vars override the default localhost
#   endpoints.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

PROM_URL="${PROM_URL:-http://localhost:9090}"
AM_URL="${AM_URL:-http://localhost:9093}"
BLACKBOX_URL="${BLACKBOX_URL:-http://localhost:9115}"

RULES_FILE="${PROJECT_DIR}/monitoring/prometheus/alert-rules.yml"
AM_RENDER="${PROJECT_DIR}/monitoring/alertmanager/alertmanager.yml"
BLACKBOX_CONFIG="${PROJECT_DIR}/monitoring/blackbox/blackbox.yml"

# --- Render freshness -----------------------------------------------------
# The content checks below compare live config against the RENDER, so a stale
# render (generate-configs.sh failed or wasn't run after a template edit)
# passes them while new routes never go live — bit us 2026-10-02 when a
# SelfAudit* routing change sat unrendered and both alerts went to "null".
for tmpl in monitoring/alertmanager/alertmanager.yml.template monitoring/prometheus/prometheus.yml.template; do
    out="${PROJECT_DIR}/${tmpl%.template}"
    if [[ ! -f "$out" || "${PROJECT_DIR}/${tmpl}" -nt "$out" ]]; then
        echo "ERROR: ${tmpl%.template} is older than its template — run scripts/generate-configs.sh first" >&2
        exit 1
    fi
done

# --- Prometheus -----------------------------------------------------------
echo "Reloading Prometheus at ${PROM_URL} ..."
# --retry-all-errors covers the container still coming up after a compose
# reconcile; a genuinely bad config keeps 500ing and fails after 5 tries.
curl -sS --fail-with-body --retry 5 --retry-delay 2 --retry-all-errors \
    -X POST "${PROM_URL}/-/reload"

EXPECTED="$(python3 -c '
import sys, yaml
with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)
for group in doc["groups"]:
    for rule in group["rules"]:
        if "alert" in rule:
            print(rule["alert"])
' "$RULES_FILE" | sort -u)"

if [[ -z "$EXPECTED" ]]; then
    echo "ERROR: no alert rules parsed from ${RULES_FILE} — the verify step itself is broken; refusing to pass." >&2
    exit 1
fi

LOADED="$(curl -sS --fail-with-body "${PROM_URL}/api/v1/rules" | python3 -c '
import sys, json
for group in json.load(sys.stdin)["data"]["groups"]:
    for rule in group["rules"]:
        print(rule["name"])
' | sort -u)"

MISSING="$(comm -23 <(printf '%s\n' "$EXPECTED") <(printf '%s\n' "$LOADED"))"
if [[ -n "$MISSING" ]]; then
    echo "ERROR: Prometheus reloaded but these alert rules are missing from /api/v1/rules:" >&2
    printf '  %s\n' $MISSING >&2
    exit 1
fi
echo "Prometheus reloaded; $(wc -l <<<"$EXPECTED") alert rules verified in /api/v1/rules."

# --- Alertmanager ----------------------------------------------------------
echo "Reloading Alertmanager at ${AM_URL} ..."
curl -sS --fail-with-body --retry 5 --retry-delay 2 --retry-all-errors \
    -X POST "${AM_URL}/-/reload"

# /api/v2/status masks secrets as <secret>, so compare structure (receiver
# names + route matchers), not bytes. Matchers are re-marshaled by
# alertmanager, so normalize whitespace before comparing.
python3 - "$AM_RENDER" "$AM_URL" <<'PYEOF'
import json
import sys
import urllib.request

import yaml

render_path, am_url = sys.argv[1], sys.argv[2]

with open(render_path) as f:
    want = yaml.safe_load(f)
with urllib.request.urlopen(f"{am_url}/api/v2/status") as resp:
    got = yaml.safe_load(json.load(resp)["config"]["original"])

want_recv = sorted(r["name"] for r in want["receivers"])
got_recv = sorted(r["name"] for r in got["receivers"])
if want_recv != got_recv:
    sys.exit(f"ERROR: receiver mismatch — rendered {want_recv} vs loaded {got_recv}")


def norm_routes(route):
    return sorted(
        tuple(sorted(m.replace(" ", "") for m in sub.get("matchers", [])))
        + (sub.get("receiver", ""),)
        for sub in route.get("routes", [])
    )


if norm_routes(want["route"]) != norm_routes(got["route"]):
    sys.exit(
        "ERROR: route mismatch — rendered "
        f"{norm_routes(want['route'])} vs loaded {norm_routes(got['route'])}"
    )

print(
    f"Alertmanager loaded config matches the render "
    f"({len(want_recv)} receivers, {len(norm_routes(want['route']))} routes)."
)
PYEOF

# --- Blackbox exporter -----------------------------------------------------
# blackbox.yml is not templated — the checkout file IS the live config. A bad
# config makes /-/reload fail (non-2xx) and the exporter keeps the old one.
echo "Reloading Blackbox exporter at ${BLACKBOX_URL} ..."
curl -sS --fail-with-body --retry 5 --retry-delay 2 --retry-all-errors \
    -X POST "${BLACKBOX_URL}/-/reload"

# /config returns the loaded config re-marshaled with defaults filled in, so
# check every module in the file is loaded and every setting we wrote is
# present with the same value (subset match) — catches an edited module, not
# just an added one.
python3 - "$BLACKBOX_CONFIG" "$BLACKBOX_URL" <<'PYEOF'
import re
import sys
import urllib.request

import yaml

config_path, bb_url = sys.argv[1], sys.argv[2]

with open(config_path) as f:
    want = yaml.safe_load(f)["modules"]
with urllib.request.urlopen(f"{bb_url}/config", timeout=10) as resp:
    got = (yaml.safe_load(resp.read()) or {}).get("modules") or {}

if not want:
    sys.exit(f"ERROR: no modules parsed from {config_path} — refusing to pass.")


# The exporter re-marshals: durations canonicalized (60s -> 1m), secrets
# masked as <secret>, enum-like values may change case. Everything else
# (regexps, header values, bodies) is compared exactly.
CASE_INSENSITIVE_KEYS = {"method", "preferred_ip_protocol", "prober"}
DURATION = re.compile(r"^(?:\d+(?:y|w|d|h|ms|m|s))+$")
UNIT_MS = {"y": 31536000000, "w": 604800000, "d": 86400000, "h": 3600000,
           "m": 60000, "s": 1000, "ms": 1}


def norm(v, key):
    if not isinstance(v, str):
        return v
    if DURATION.match(v):
        return sum(int(n) * UNIT_MS[u] for n, u in re.findall(r"(\d+)(ms|y|w|d|h|m|s)", v))
    return v.lower() if key in CASE_INSENSITIVE_KEYS else v


def diff(w, g, path, key=""):
    # The exporter marshals with omitempty, so a zero value we set explicitly
    # (e.g. ip_protocol_fallback: false) is absent from /config.
    if g is None and w in (False, 0, "", [], {}):
        return []
    if g == "<secret>":
        return []
    if isinstance(w, dict):
        if not isinstance(g, dict):
            return [path]
        return [d for k, v in w.items() for d in diff(v, g.get(k), f"{path}.{k}", k)]
    if isinstance(w, list):
        if not isinstance(g, list) or len(w) != len(g):
            return [path]
        return [d for i, (a, b) in enumerate(zip(w, g)) for d in diff(a, b, f"{path}[{i}]", key)]
    return [] if norm(w, key) == norm(g, key) else [path]


missing = sorted(set(want) - set(got))
if missing:
    sys.exit(f"ERROR: Blackbox reloaded but these modules are not loaded: {missing}")
# The exporter has no built-in modules, so anything extra is a stale config
# (e.g. a module deleted from the file but still served).
extra = sorted(set(got) - set(want))
if extra:
    sys.exit(f"ERROR: Blackbox still serves modules not in {config_path}: {extra}")
drift = [d for name, mod in want.items() for d in diff(mod, got[name], name)]
if drift:
    sys.exit(f"ERROR: Blackbox loaded config differs from {config_path} at: {drift}")

print(f"Blackbox loaded config matches blackbox.yml ({len(want)} modules).")
PYEOF

echo "Monitoring reload complete."
