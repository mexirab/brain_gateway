#!/bin/bash
# Brain Gateway - Monitoring Reload + Verify
# ===========================================
# Reloads Prometheus and Alertmanager from their live-mounted rendered
# configs and verifies the running instances actually picked them up.
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
#   PROM_URL / AM_URL env vars override the default localhost endpoints.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

PROM_URL="${PROM_URL:-http://localhost:9090}"
AM_URL="${AM_URL:-http://localhost:9093}"

RULES_FILE="${PROJECT_DIR}/monitoring/prometheus/alert-rules.yml"
AM_RENDER="${PROJECT_DIR}/monitoring/alertmanager/alertmanager.yml"

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

echo "Monitoring reload complete."
