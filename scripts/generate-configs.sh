#!/bin/bash
# Brain Gateway - Config Generator
# ==================================
# Generates configuration files from templates using envsubst
#
# Templates:
#   - monitoring/prometheus/prometheus.yml.template -> prometheus.yml
#   - monitoring/alertmanager/alertmanager.yml.template -> alertmanager.yml
#     (injects PUSHOVER_USER_KEY/PUSHOVER_APP_TOKEN from .env — rendered
#      file is gitignored; never commit it)
#
# The rendered files are bind-mounted into the running containers, so this
# script must truncate them IN PLACE (envsubst > output). Never rm/mv the
# outputs — replacing the inode silently detaches the container's mount.
#
# Usage: ./scripts/generate-configs.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}"
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║           Brain Gateway - Config Generator                    ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# Check for envsubst
if ! command -v envsubst &>/dev/null; then
    echo -e "${RED}Error: envsubst not found. Install gettext package.${NC}"
    echo "  Ubuntu/Debian: sudo apt install gettext-base"
    echo "  macOS: brew install gettext && brew link --force gettext"
    exit 1
fi

# Load environment variables
ENV_FILE="${PROJECT_DIR}/.env"
if [[ -f "$ENV_FILE" ]]; then
    echo -e "${GREEN}Loading environment from ${ENV_FILE}${NC}"
    # .env holds every secret on the box, and this script runs inside the
    # CI deploy job whose logs are public — make sure xtrace can never echo
    # the sourced lines (e.g. someone debugging with `bash -x`).
    XTRACE_WAS_ON=0
    case $- in *x*) XTRACE_WAS_ON=1; set +x;; esac
    # shellcheck source=/dev/null
    set -a
    source "$ENV_FILE"
    set +a
    if [[ "$XTRACE_WAS_ON" == 1 ]]; then set -x; fi
else
    echo -e "${YELLOW}Warning: .env not found, using defaults${NC}"
fi

# Set defaults for any unset variables
export NODE_JUPITER_IP="${NODE_JUPITER_IP:-10.0.0.248}"
export NODE_HELIOS_IP="${NODE_HELIOS_IP:-10.0.0.195}"
export NODE_SATURN_IP="${NODE_SATURN_IP:-10.0.0.58}"
export NODE_URANUS_IP="${NODE_URANUS_IP:-10.0.0.173}"
export NODE_HA_IP="${NODE_HA_IP:-10.0.0.106}"
export SERVICE_ORCHESTRATOR_PORT="${SERVICE_ORCHESTRATOR_PORT:-8888}"
export SERVICE_TTS_PORT="${SERVICE_TTS_PORT:-8002}"
export SERVICE_STT_PORT="${SERVICE_STT_PORT:-8003}"
export MONITORING_GPU_EXPORTER_PORT="${MONITORING_GPU_EXPORTER_PORT:-9400}"

echo ""
echo "Using configuration:"
echo "  NODE_JUPITER_IP: ${NODE_JUPITER_IP}  (Pi-hole primary, monitoring stack)"
echo "  NODE_HELIOS_IP:  ${NODE_HELIOS_IP}  (brain gateway, primary LLM, TTS/STT, coder)"
echo "  NODE_SATURN_IP:  ${NODE_SATURN_IP}  (vision, Pi-hole secondary)"
echo "  NODE_URANUS_IP:  ${NODE_URANUS_IP}  (ComfyUI)"
echo ""

# Validate a candidate render before it touches the live bind mount — a
# bad file would otherwise sit armed until the next container restart
# (reloads reject it loudly, unattended restarts crash-loop on it).
# Uses the digest-pinned images from monitoring/docker-compose.yml so the
# validator always matches what actually runs.
validate_config() {
    local kind="$1" candidate="$2"
    local compose_file="${PROJECT_DIR}/monitoring/docker-compose.yml"
    local image

    if ! command -v docker &>/dev/null; then
        echo -e "  ${YELLOW}WARN${NC}: docker unavailable — skipping ${kind} config validation"
        return 0
    fi

    case "$kind" in
        prometheus)
            image="$(grep -oE 'prom/prometheus:[^[:space:]]+' "$compose_file" | head -1)"
            # The config references rule_files/credentials_file under
            # /etc/prometheus, so mount the real dir there. This also
            # syntax-checks alert-rules.yml on every render.
            docker run --rm \
                -v "${PROJECT_DIR}/monitoring/prometheus:/etc/prometheus:ro" \
                -v "$candidate:/tmp/candidate.yml:ro" \
                --entrypoint promtool "$image" check config /tmp/candidate.yml
            ;;
        alertmanager)
            image="$(grep -oE 'prom/alertmanager:[^[:space:]]+' "$compose_file" | head -1)"
            docker run --rm \
                -v "$candidate:/tmp/candidate.yml:ro" \
                --entrypoint amtool "$image" check-config /tmp/candidate.yml
            ;;
        *)
            return 0
            ;;
    esac
}

# Render one template -> output with an explicit envsubst variable list
# (only listed variables are substituted; everything else — e.g. Go
# template braces in the alertmanager config — passes through untouched).
render_template() {
    local template="$1" output="$2" subst_vars="$3" mode="${4:-}" validator="${5:-}"

    # These renders are load-bearing (compose bind-mounts them) — a missing
    # template means a broken checkout or a rename that skipped this script.
    if [[ ! -f "$template" ]]; then
        echo -e "  ${RED}FAIL${NC}: $(basename "$template") not found at ${template}"
        return 1
    fi

    # If compose ever came up before the first render, docker auto-created
    # a root-owned DIRECTORY at the bind-mount source.
    if [[ -d "$output" ]]; then
        echo -e "  ${RED}FAIL${NC}: ${output} is a directory (docker auto-created it before the first render)"
        echo "        Fix: stop the container mounting it, sudo rmdir the directory, re-run this script, restart the container."
        return 1
    fi

    # The outputs are live bind mounts — refuse to render if we can't
    # truncate in place. (A root-owned leftover from a sudo render is the
    # usual cause: fix with `sudo chown $(id -un) <file>`.)
    if [[ -e "$output" && ! -w "$output" ]]; then
        echo -e "  ${RED}FAIL${NC}: $(basename "$output") exists but is not writable by $(id -un)"
        echo "        Fix ownership, then re-run: sudo chown $(id -un): $output"
        return 1
    fi

    mkdir -p "$(dirname "$output")"

    # Render to a temp file and validate BEFORE overwriting the live mount,
    # then `cat` (not mv) so the mounted inode is preserved.
    local tmp
    tmp="$(mktemp)"
    envsubst "$subst_vars" < "$template" > "$tmp"
    chmod 644 "$tmp"  # validator containers run as nobody and must read it

    if [[ -n "$validator" ]] && ! validate_config "$validator" "$tmp"; then
        rm -f "$tmp"
        echo -e "  ${RED}FAIL${NC}: rendered $(basename "$output") failed ${validator} validation — live config left untouched"
        return 1
    fi

    cat "$tmp" > "$output"
    rm -f "$tmp"

    if [[ -n "$mode" ]]; then
        chmod "$mode" "$output"
    fi

    echo -e "  ${GREEN}OK${NC}: $(basename "$template") -> $(basename "$output")"
}

GENERATED_FILES=(
    "${PROJECT_DIR}/monitoring/prometheus/prometheus.yml"
    "${PROJECT_DIR}/monitoring/alertmanager/alertmanager.yml"
)

# Process each template
echo -e "${BLUE}Generating configs...${NC}"

render_template \
    "${PROJECT_DIR}/monitoring/prometheus/prometheus.yml.template" \
    "${PROJECT_DIR}/monitoring/prometheus/prometheus.yml" \
    '${NODE_JUPITER_IP} ${NODE_SATURN_IP} ${NODE_URANUS_IP} ${NODE_HELIOS_IP} ${NODE_HA_IP} ${SERVICE_ORCHESTRATOR_PORT} ${SERVICE_TTS_PORT} ${SERVICE_STT_PORT} ${MONITORING_GPU_EXPORTER_PORT}' \
    "" \
    prometheus

# Rendered alertmanager.yml holds the Pushover credentials. 644 (not 600)
# because the container runs as nobody(65534) and must read it across the
# bind mount — same posture as monitoring/prometheus/api_token. The file
# is gitignored; the secrets live canonically in .env (0600).
# With the keys unset the render is UNLOADABLE (amtool: "one of user_key
# or user_key_file must be configured") — skip it and keep whatever render
# exists rather than arming a crash-loop for the next container restart.
if [[ -z "${PUSHOVER_USER_KEY:-}" || -z "${PUSHOVER_APP_TOKEN:-}" ]]; then
    echo -e "  ${YELLOW}SKIP${NC}: alertmanager.yml.template (PUSHOVER_USER_KEY/PUSHOVER_APP_TOKEN not set in .env;"
    echo "        the pushover receiver cannot render a loadable config — existing render left untouched)"
else
    render_template \
        "${PROJECT_DIR}/monitoring/alertmanager/alertmanager.yml.template" \
        "${PROJECT_DIR}/monitoring/alertmanager/alertmanager.yml" \
        '${PUSHOVER_USER_KEY} ${PUSHOVER_APP_TOKEN}' \
        644 \
        alertmanager
fi

echo ""
echo -e "${GREEN}Config generation complete!${NC}"
echo ""
echo "Generated files:"
for output in "${GENERATED_FILES[@]}"; do
    if [[ -f "$output" ]]; then
        echo "  - ${output}"
    fi
done
echo ""
echo "To apply config changes without restart:"
echo "  curl -X POST http://localhost:9090/-/reload   # Prometheus"
echo "  curl -X POST http://localhost:9093/-/reload   # Alertmanager"
