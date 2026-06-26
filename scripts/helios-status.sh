#!/bin/bash
# Helios power + model status: smart-plug state/draw (via HA) + the vLLM endpoint + GPU.
# Usage: ./helios-status.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
[[ -f "${PROJECT_DIR}/.env" ]] && { set -a; source "${PROJECT_DIR}/.env"; set +a; }
# shellcheck source=scripts/_helios_plug.sh
source "${SCRIPT_DIR}/_helios_plug.sh"

HELIOS_IP="${NODE_HELIOS_IP:-10.0.0.195}"
VLLM_PORT="${SERVICE_HELIOS_PORT:-8080}"

echo "=== Helios status ==="
echo "Smart plug : $(plug_state)  (draw: $(plug_watts)W)"
if curl -s --connect-timeout 2 "http://${HELIOS_IP}:${VLLM_PORT}/v1/models" >/dev/null 2>&1; then
  echo "Model      : UP (vLLM http://${HELIOS_IP}:${VLLM_PORT}/v1)"
else
  echo "Model      : down (Helios off or still booting)"
fi
if ping -c1 -W2 "${HELIOS_IP}" >/dev/null 2>&1; then
  echo "Host       : ${HELIOS_IP} reachable"
  ssh -o BatchMode=yes -o ConnectTimeout=4 "${HELIOS_SSH_USER:-labadmin}@${HELIOS_IP}" \
    "nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu,power.draw --format=csv,noheader" 2>/dev/null \
    | sed "s/^/GPU        : /" || true
else
  echo "Host       : ${HELIOS_IP} unreachable (powered off)"
fi
