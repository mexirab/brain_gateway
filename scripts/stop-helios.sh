#!/bin/bash
# Stop Helios Expert Model (120B)
# Saves ~150W of power
#
# Helios: 10.0.0.195 - RTX 5090 (32GB VRAM) + RAM offload
# Uses llama.cpp for inference
#
# Usage: ./stop-helios.sh

HELIOS_HOST="helios"
HELIOS_PORT="8080"
HELIOS_USER="labadmin"
HELIOS_IP="10.0.0.195"
SERVICE_NAME="llama-server"

echo "[$(date)] Stopping Helios Expert Model..."

# Check if running
if ! curl -s --connect-timeout 2 "http://${HELIOS_IP}:${HELIOS_PORT}/health" > /dev/null 2>&1; then
    echo "[$(date)] Helios is already stopped."
    exit 0
fi

# Stop llama-server service on Helios
# Requires passwordless sudo for systemctl commands (see HELIOS_SETUP.md)
if ! ssh "${HELIOS_USER}@${HELIOS_HOST}" "sudo systemctl stop ${SERVICE_NAME}" 2>/dev/null; then
    echo ""
    echo "[$(date)] ERROR: Could not stop service via SSH."
    echo "Either:"
    echo "  1. SSH to Helios manually and run: sudo systemctl stop ${SERVICE_NAME}"
    echo "  2. Configure passwordless sudo (see HELIOS_SETUP.md)"
    exit 1
fi

# Verify stopped
sleep 2
if curl -s --connect-timeout 2 "http://${HELIOS_IP}:${HELIOS_PORT}/health" > /dev/null 2>&1; then
    echo "[$(date)] WARNING: Helios may still be running"
    exit 1
fi

echo "[$(date)] Helios Expert Model stopped."
echo "Power savings: ~150W"
exit 0
