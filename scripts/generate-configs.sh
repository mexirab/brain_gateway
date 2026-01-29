#!/bin/bash
# Brain Gateway - Config Generator
# ==================================
# Generates configuration files from templates using envsubst
#
# Templates:
#   - monitoring/prometheus/prometheus.yml.template -> prometheus.yml
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
    # shellcheck source=/dev/null
    set -a
    source "$ENV_FILE"
    set +a
else
    echo -e "${YELLOW}Warning: .env not found, using defaults${NC}"
fi

# Set defaults for any unset variables
export NODE_JUPITER_IP="${NODE_JUPITER_IP:-10.0.0.248}"
export NODE_SATURN_IP="${NODE_SATURN_IP:-10.0.0.58}"
export NODE_URANUS_IP="${NODE_URANUS_IP:-10.0.0.173}"
export NODE_HELIOS_IP="${NODE_HELIOS_IP:-10.0.0.195}"
export NODE_HA_IP="${NODE_HA_IP:-10.0.0.106}"
export SERVICE_ORCHESTRATOR_PORT="${SERVICE_ORCHESTRATOR_PORT:-8888}"
export SERVICE_TTS_PORT="${SERVICE_TTS_PORT:-8002}"
export SERVICE_STT_PORT="${SERVICE_STT_PORT:-8003}"
export SERVICE_NEMOTRON_PORT="${SERVICE_NEMOTRON_PORT:-8001}"
export SERVICE_HELIOS_PORT="${SERVICE_HELIOS_PORT:-8080}"
export MONITORING_GPU_EXPORTER_PORT="${MONITORING_GPU_EXPORTER_PORT:-9400}"

echo ""
echo "Using configuration:"
echo "  NODE_JUPITER_IP: ${NODE_JUPITER_IP}"
echo "  NODE_SATURN_IP:  ${NODE_SATURN_IP}"
echo "  NODE_URANUS_IP:  ${NODE_URANUS_IP}"
echo "  NODE_HELIOS_IP:  ${NODE_HELIOS_IP}"
echo ""

# Define template -> output mappings
declare -A TEMPLATES=(
    ["${PROJECT_DIR}/monitoring/prometheus/prometheus.yml.template"]="${PROJECT_DIR}/monitoring/prometheus/prometheus.yml"
)

# Process each template
echo -e "${BLUE}Generating configs...${NC}"

for template in "${!TEMPLATES[@]}"; do
    output="${TEMPLATES[$template]}"

    if [[ ! -f "$template" ]]; then
        echo -e "  ${YELLOW}SKIP${NC}: $(basename "$template") (template not found)"
        continue
    fi

    # Create output directory if needed
    mkdir -p "$(dirname "$output")"

    # Generate config using envsubst
    # Only substitute defined variables, not shell variables like $2
    envsubst '${NODE_JUPITER_IP} ${NODE_SATURN_IP} ${NODE_URANUS_IP} ${NODE_HELIOS_IP} ${NODE_HA_IP} ${SERVICE_ORCHESTRATOR_PORT} ${SERVICE_TTS_PORT} ${SERVICE_STT_PORT} ${SERVICE_NEMOTRON_PORT} ${SERVICE_HELIOS_PORT} ${MONITORING_GPU_EXPORTER_PORT}' \
        < "$template" > "$output"

    echo -e "  ${GREEN}OK${NC}: $(basename "$template") -> $(basename "$output")"
done

echo ""
echo -e "${GREEN}Config generation complete!${NC}"
echo ""
echo "Generated files:"
for template in "${!TEMPLATES[@]}"; do
    output="${TEMPLATES[$template]}"
    if [[ -f "$output" ]]; then
        echo "  - ${output}"
    fi
done
echo ""
echo "To apply Prometheus config changes without restart:"
echo "  curl -X POST http://localhost:9090/-/reload"
