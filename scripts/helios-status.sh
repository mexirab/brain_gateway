#!/bin/bash
# Check Helios Expert Model Status
#
# Helios: 10.0.0.195 - RTX 5090 (32GB VRAM) + RAM offload
# Model: unsloth_gpt-oss-120b-GGUF_Q4_K_S via llama.cpp
#
# Usage: ./helios-status.sh

HELIOS_HOST="helios"
HELIOS_PORT="8080"
HELIOS_USER="labadmin"
HELIOS_IP="10.0.0.195"

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
