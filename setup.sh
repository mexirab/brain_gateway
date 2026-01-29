#!/bin/bash
# Brain Gateway - Interactive Setup Script
# =========================================
# Generates .env file from .env.example by prompting for values
#
# Usage: ./setup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_EXAMPLE="${SCRIPT_DIR}/.env.example"
ENV_FILE="${SCRIPT_DIR}/.env"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}"
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║           Brain Gateway - Environment Setup                   ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# Check if .env.example exists
if [[ ! -f "$ENV_EXAMPLE" ]]; then
    echo -e "${RED}Error: .env.example not found at ${ENV_EXAMPLE}${NC}"
    exit 1
fi

# Check if .env already exists
if [[ -f "$ENV_FILE" ]]; then
    echo -e "${YELLOW}Warning: .env already exists at ${ENV_FILE}${NC}"
    read -p "Overwrite? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi
    # Backup existing .env
    cp "$ENV_FILE" "${ENV_FILE}.backup.$(date +%Y%m%d_%H%M%S)"
    echo -e "${GREEN}Backed up existing .env${NC}"
fi

# Start with the template
cp "$ENV_EXAMPLE" "$ENV_FILE"

echo ""
echo -e "${BLUE}=== Network Configuration ===${NC}"
echo "Enter IP addresses for your cluster nodes."
echo "(Press Enter to accept default values shown in brackets)"
echo ""

# Function to prompt and update a value
prompt_value() {
    local var_name="$1"
    local description="$2"
    local current_value
    current_value=$(grep "^${var_name}=" "$ENV_FILE" | cut -d'=' -f2- | head -1)

    read -p "${description} [${current_value}]: " new_value
    if [[ -n "$new_value" ]]; then
        # Escape special characters for sed
        local escaped_value
        escaped_value=$(printf '%s\n' "$new_value" | sed -e 's/[\/&]/\\&/g')
        sed -i "s|^${var_name}=.*|${var_name}=${escaped_value}|" "$ENV_FILE"
    fi
}

# Function to prompt for required secrets
prompt_secret() {
    local var_name="$1"
    local description="$2"
    local current_value
    current_value=$(grep "^${var_name}=" "$ENV_FILE" | cut -d'=' -f2- | head -1)

    # Check if it's a placeholder
    if [[ "$current_value" == "your-"*"-here" ]]; then
        echo -e "${YELLOW}${description} (REQUIRED)${NC}"
        read -sp "  Enter value: " new_value
        echo ""
        if [[ -n "$new_value" ]]; then
            local escaped_value
            escaped_value=$(printf '%s\n' "$new_value" | sed -e 's/[\/&]/\\&/g')
            sed -i "s|^${var_name}=.*|${var_name}=${escaped_value}|" "$ENV_FILE"
            echo -e "  ${GREEN}Set${NC}"
        else
            echo -e "  ${YELLOW}Skipped (you must set this manually)${NC}"
        fi
    else
        echo -e "${description}: ${GREEN}[already set]${NC}"
    fi
}

# Network nodes
prompt_value "NODE_JUPITER_IP" "Jupiter IP (gateway host)"
prompt_value "NODE_SATURN_IP" "Saturn IP (Nemotron)"
prompt_value "NODE_URANUS_IP" "Uranus IP (TTS/STT)"
prompt_value "NODE_HELIOS_IP" "Helios IP (expert model)"
prompt_value "NODE_HA_IP" "Home Assistant IP"

echo ""
echo -e "${BLUE}=== Paths ===${NC}"
prompt_value "GATEWAY_ROOT_PATH" "Gateway installation path"
prompt_value "USER_HOME" "User home directory"
prompt_value "RAG_BASE" "RAG data directory"

echo ""
echo -e "${BLUE}=== Secrets (Required) ===${NC}"
echo "These values are required for the gateway to function."
echo ""
prompt_secret "HA_TOKEN" "Home Assistant Token"
prompt_secret "LITELLM_MASTER_KEY" "LiteLLM Master Key"
prompt_secret "GF_SECURITY_ADMIN_PASSWORD" "Grafana Admin Password"

echo ""
echo -e "${BLUE}=== SSH Configuration ===${NC}"
prompt_value "HELIOS_SSH_USER" "Helios SSH username"

echo ""
echo -e "${BLUE}=== Optional: Service Ports ===${NC}"
read -p "Configure service ports? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    prompt_value "SERVICE_ORCHESTRATOR_PORT" "Orchestrator port"
    prompt_value "SERVICE_TTS_PORT" "TTS port"
    prompt_value "SERVICE_STT_PORT" "STT port"
    prompt_value "SERVICE_NEMOTRON_PORT" "Nemotron port"
    prompt_value "SERVICE_HELIOS_PORT" "Helios port"
fi

echo ""
echo -e "${GREEN}╔═══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║           Configuration Complete!                             ║${NC}"
echo -e "${GREEN}╚═══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo "Configuration saved to: ${ENV_FILE}"
echo ""
echo -e "${YELLOW}Next steps:${NC}"
echo "  1. Review your .env file: cat ${ENV_FILE}"
echo "  2. Validate configuration: ./scripts/validate-config.sh"
echo "  3. Generate derived configs: ./scripts/generate-configs.sh"
echo "  4. Start services: docker compose up -d"
echo ""
echo -e "${BLUE}To regenerate this configuration, run: ./setup.sh${NC}"
