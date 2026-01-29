#!/bin/bash
# Check Helios Expert Model Status
#
# Helios: RTX 5090 (32GB VRAM) + RAM offload
# Model: unsloth_gpt-oss-120b-GGUF_Q4_K_S via llama.cpp
#
# Usage: ./helios-status.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Source environment file if it exists
if [[ -f "${PROJECT_DIR}/.env" ]]; then
    # shellcheck source=/dev/null
    set -a
    source "${PROJECT_DIR}/.env"
    set +a
fi

# Configuration with defaults (can be overridden via .env)
HELIOS_HOST="${HELIOS_HOST:-helios}"
HELIOS_PORT="${SERVICE_HELIOS_PORT:-8080}"
HELIOS_USER="${HELIOS_SSH_USER:-labadmin}"
HELIOS_IP="${NODE_HELIOS_IP:-10.0.0.195}"

echo "=== Helios Expert Model Status ==="
echo ""

# Check if service is responding
if curl -s --connect-timeout 2 "http://${HELIOS_IP}:${HELIOS_PORT}/health" > /dev/null 2>&1; then
    echo "Status: RUNNING"
    echo "Endpoint: http://${HELIOS_IP}:${HELIOS_PORT}/v1"
    echo ""
    echo "Health check response:"
    curl -s "http://${HELIOS_IP}:${HELIOS_PORT}/health" | jq . 2>/dev/null || \
        curl -s "http://${HELIOS_IP}:${HELIOS_PORT}/health"
else
    echo "Status: STOPPED (saves ~150W)"
    echo ""
    echo "To start: ./start-helios.sh"
fi

echo ""
echo "=== GPU Status ==="
ssh "${HELIOS_USER}@${HELIOS_HOST}" "nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu,power.draw --format=csv,noheader" 2>/dev/null || \
    echo "Could not connect to Helios"
