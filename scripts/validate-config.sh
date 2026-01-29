#!/bin/bash
# Brain Gateway - Configuration Validator
# =========================================
# Verifies that all required environment variables are set
#
# Usage: ./scripts/validate-config.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${PROJECT_DIR}/.env"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}"
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║           Brain Gateway - Configuration Validator             ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# Track errors
ERRORS=0
WARNINGS=0

# Load .env if it exists
if [[ -f "$ENV_FILE" ]]; then
    echo -e "${GREEN}Found .env file${NC}"
    # shellcheck source=/dev/null
    set -a
    source "$ENV_FILE"
    set +a
else
    echo -e "${RED}ERROR: .env file not found at ${ENV_FILE}${NC}"
    echo "Run ./setup.sh to create one, or copy .env.example to .env"
    exit 1
fi

# Function to check if a variable is set and not a placeholder
check_required() {
    local var_name="$1"
    local description="$2"
    local value="${!var_name:-}"

    if [[ -z "$value" ]]; then
        echo -e "  ${RED}MISSING${NC}: ${var_name} - ${description}"
        ((ERRORS++))
    elif [[ "$value" == "your-"*"-here" ]]; then
        echo -e "  ${RED}NOT SET${NC}: ${var_name} - ${description} (still has placeholder)"
        ((ERRORS++))
    else
        echo -e "  ${GREEN}OK${NC}: ${var_name}"
    fi
}

# Function to check optional variable
check_optional() {
    local var_name="$1"
    local description="$2"
    local value="${!var_name:-}"

    if [[ -z "$value" ]]; then
        echo -e "  ${YELLOW}MISSING${NC}: ${var_name} - ${description} (will use default)"
        ((WARNINGS++))
    else
        echo -e "  ${GREEN}OK${NC}: ${var_name}"
    fi
}

# Function to check IP reachability
check_ip_reachable() {
    local var_name="$1"
    local description="$2"
    local ip="${!var_name:-}"

    if [[ -n "$ip" && "$ip" != "your-"*"-here" ]]; then
        if ping -c 1 -W 2 "$ip" &>/dev/null; then
            echo -e "  ${GREEN}REACHABLE${NC}: ${var_name} (${ip}) - ${description}"
        else
            echo -e "  ${YELLOW}UNREACHABLE${NC}: ${var_name} (${ip}) - ${description}"
            ((WARNINGS++))
        fi
    fi
}

# Function to check URL endpoint
check_endpoint() {
    local url="$1"
    local description="$2"

    if [[ -n "$url" ]]; then
        if curl -s --connect-timeout 3 "${url}" &>/dev/null; then
            echo -e "  ${GREEN}UP${NC}: ${description} (${url})"
        else
            echo -e "  ${YELLOW}DOWN${NC}: ${description} (${url})"
            ((WARNINGS++))
        fi
    fi
}

echo ""
echo -e "${BLUE}=== Required Secrets ===${NC}"
check_required "HA_TOKEN" "Home Assistant access token"
check_required "LITELLM_MASTER_KEY" "LiteLLM API key"
check_required "GF_SECURITY_ADMIN_PASSWORD" "Grafana admin password"

echo ""
echo -e "${BLUE}=== Network Nodes ===${NC}"
check_optional "NODE_JUPITER_IP" "Gateway host"
check_optional "NODE_SATURN_IP" "Nemotron server"
check_optional "NODE_URANUS_IP" "TTS/STT server"
check_optional "NODE_HELIOS_IP" "Expert model server"
check_optional "NODE_HA_IP" "Home Assistant"

echo ""
echo -e "${BLUE}=== Paths ===${NC}"
check_optional "GATEWAY_ROOT_PATH" "Installation path"
check_optional "USER_HOME" "User home directory"
check_optional "RAG_BASE" "RAG data directory"
check_optional "CHROMA_PERSIST" "ChromaDB path"

# Check if paths exist
if [[ -n "${GATEWAY_ROOT_PATH:-}" && -d "$GATEWAY_ROOT_PATH" ]]; then
    echo -e "  ${GREEN}EXISTS${NC}: GATEWAY_ROOT_PATH directory"
else
    echo -e "  ${YELLOW}MISSING${NC}: GATEWAY_ROOT_PATH directory"
    ((WARNINGS++))
fi

if [[ -n "${RAG_BASE:-}" && -d "$RAG_BASE" ]]; then
    echo -e "  ${GREEN}EXISTS${NC}: RAG_BASE directory"
else
    echo -e "  ${YELLOW}MISSING${NC}: RAG_BASE directory"
    ((WARNINGS++))
fi

echo ""
echo -e "${BLUE}=== Service URLs ===${NC}"
check_optional "NEMOTRON_URL" "Nemotron endpoint"
check_optional "HELIOS_URL" "Helios endpoint"
check_optional "HA_URL" "Home Assistant URL"
check_optional "TTS_URL" "TTS endpoint"
check_optional "ORCHESTRATOR_URL" "Orchestrator URL"

echo ""
echo -e "${BLUE}=== SSH Configuration ===${NC}"
check_optional "HELIOS_SSH_USER" "Helios SSH user"

echo ""
echo -e "${BLUE}=== Network Connectivity (Optional) ===${NC}"
echo "Testing if nodes are reachable..."
check_ip_reachable "NODE_JUPITER_IP" "Gateway"
check_ip_reachable "NODE_SATURN_IP" "Nemotron"
check_ip_reachable "NODE_URANUS_IP" "TTS/STT"
check_ip_reachable "NODE_HELIOS_IP" "Helios"
check_ip_reachable "NODE_HA_IP" "Home Assistant"

echo ""
echo -e "${BLUE}=== Service Health (Optional) ===${NC}"
echo "Testing if services are responding..."

# Test endpoints
if [[ -n "${HA_URL:-}" && -n "${HA_TOKEN:-}" && "$HA_TOKEN" != "your-"*"-here" ]]; then
    if curl -s --connect-timeout 3 -H "Authorization: Bearer ${HA_TOKEN}" "${HA_URL}/api/" &>/dev/null; then
        echo -e "  ${GREEN}UP${NC}: Home Assistant (${HA_URL})"
    else
        echo -e "  ${YELLOW}DOWN${NC}: Home Assistant (${HA_URL})"
        ((WARNINGS++))
    fi
fi

# Expand variables in NEMOTRON_URL for testing
NEMOTRON_TEST_URL="${NEMOTRON_URL:-}"
NEMOTRON_TEST_URL="${NEMOTRON_TEST_URL//\$\{NODE_SATURN_IP:-10.0.0.58\}/${NODE_SATURN_IP:-10.0.0.58}}"
NEMOTRON_TEST_URL="${NEMOTRON_TEST_URL//\$\{SERVICE_NEMOTRON_PORT:-8001\}/${SERVICE_NEMOTRON_PORT:-8001}}"
if [[ -n "$NEMOTRON_TEST_URL" ]]; then
    base_url="${NEMOTRON_TEST_URL%/v1}"
    if curl -s --connect-timeout 3 "${base_url}/health" &>/dev/null; then
        echo -e "  ${GREEN}UP${NC}: Nemotron (${base_url})"
    else
        echo -e "  ${YELLOW}DOWN${NC}: Nemotron (${base_url})"
        ((WARNINGS++))
    fi
fi

# TTS service
TTS_TEST_URL="${TTS_URL:-}"
TTS_TEST_URL="${TTS_TEST_URL//\$\{NODE_URANUS_IP:-10.0.0.173\}/${NODE_URANUS_IP:-10.0.0.173}}"
TTS_TEST_URL="${TTS_TEST_URL//\$\{SERVICE_TTS_PORT:-8002\}/${SERVICE_TTS_PORT:-8002}}"
if [[ -n "$TTS_TEST_URL" ]]; then
    if curl -s --connect-timeout 3 "${TTS_TEST_URL}/health" &>/dev/null; then
        echo -e "  ${GREEN}UP${NC}: TTS (${TTS_TEST_URL})"
    else
        echo -e "  ${YELLOW}DOWN${NC}: TTS (${TTS_TEST_URL})"
        ((WARNINGS++))
    fi
fi

echo ""
echo "═══════════════════════════════════════════════════════════════"

if [[ $ERRORS -gt 0 ]]; then
    echo -e "${RED}VALIDATION FAILED${NC}"
    echo -e "  Errors: ${RED}${ERRORS}${NC}"
    echo -e "  Warnings: ${YELLOW}${WARNINGS}${NC}"
    echo ""
    echo "Please fix the errors above before starting services."
    echo "Run ./setup.sh to reconfigure, or edit .env directly."
    exit 1
elif [[ $WARNINGS -gt 0 ]]; then
    echo -e "${YELLOW}VALIDATION PASSED WITH WARNINGS${NC}"
    echo -e "  Errors: ${GREEN}0${NC}"
    echo -e "  Warnings: ${YELLOW}${WARNINGS}${NC}"
    echo ""
    echo "Services may work with defaults, but review warnings above."
    exit 0
else
    echo -e "${GREEN}VALIDATION PASSED${NC}"
    echo -e "  Errors: ${GREEN}0${NC}"
    echo -e "  Warnings: ${GREEN}0${NC}"
    echo ""
    echo "Configuration looks good! You can now start services:"
    echo "  docker compose up -d"
    exit 0
fi
