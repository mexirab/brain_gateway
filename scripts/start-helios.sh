#!/bin/bash
# Start Helios Expert Model (120B)
# Manual start script for deep conversation mode
#
# Helios: RTX 5090 (32GB VRAM) + RAM offload
# Model: unsloth_gpt-oss-120b-GGUF_Q4_K_S via llama.cpp
#
# Usage: ./start-helios.sh
#
# This saves ~150W of power when not in use.
# Start it when you need deep, thoughtful conversations.

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
SERVICE_NAME="llama-server"

echo "[$(date)] Starting Helios Expert Model..."

# Check if already running
if curl -s --connect-timeout 2 "http://${HELIOS_IP}:${HELIOS_PORT}/health" > /dev/null 2>&1; then
    echo "[$(date)] Helios is already running!"
    curl -s "http://${HELIOS_IP}:${HELIOS_PORT}/health" | jq .
    exit 0
fi

# Start llama-server service on Helios
# Requires passwordless sudo for systemctl commands (see HELIOS_SETUP.md)
echo "[$(date)] Starting llama-server on Helios..."
if ! ssh "${HELIOS_USER}@${HELIOS_HOST}" "sudo systemctl start ${SERVICE_NAME}" 2>/dev/null; then
    echo ""
    echo "[$(date)] ERROR: Could not start service via SSH."
    echo "Either:"
    echo "  1. SSH to Helios manually and run: sudo systemctl start ${SERVICE_NAME}"
    echo "  2. Configure passwordless sudo (see HELIOS_SETUP.md)"
    exit 1
fi

# Wait for service to become ready
echo "[$(date)] Waiting for model to load (this may take 1-2 minutes)..."
MAX_WAIT=180
WAIT_INTERVAL=5
ELAPSED=0

while [ $ELAPSED -lt $MAX_WAIT ]; do
    if curl -s --connect-timeout 2 "http://${HELIOS_IP}:${HELIOS_PORT}/health" > /dev/null 2>&1; then
        echo ""
        echo "[$(date)] Helios Expert Model is ready!"
        echo ""
        echo "Model: unsloth_gpt-oss-120b-GGUF_Q4_K_S (120B parameters)"
        echo "Endpoint: http://${HELIOS_IP}:${HELIOS_PORT}/v1"
        echo ""
        echo "The orchestrator will automatically use Helios via the 'ask_expert' tool."
        echo "You can also access it directly via LiteLLM as 'helios-ost120b'."
        exit 0
    fi

    echo -n "."
    sleep $WAIT_INTERVAL
    ELAPSED=$((ELAPSED + WAIT_INTERVAL))
done

echo ""
echo "[$(date)] ERROR: Helios failed to start within ${MAX_WAIT} seconds"
echo "Check logs with: ssh ${HELIOS_USER}@${HELIOS_HOST} 'sudo journalctl -u ${SERVICE_NAME} -n 50'"
exit 1
