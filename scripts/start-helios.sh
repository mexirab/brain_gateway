#!/bin/bash
# Power Helios ON via its HA-managed Tapo smart plug, then wait for the model.
# Post power-tiering, Helios is powered off most of the time and its NIC WoL is dead;
# restoring plug power auto-boots it (BIOS AC Back = Last State). The orchestrator also
# does this automatically when you chat while Helios is asleep — this is the manual path.
# Usage: ./start-helios.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
[[ -f "${PROJECT_DIR}/.env" ]] && { set -a; source "${PROJECT_DIR}/.env"; set +a; }
# shellcheck source=scripts/_helios_plug.sh
source "${SCRIPT_DIR}/_helios_plug.sh"

HELIOS_IP="${NODE_HELIOS_IP:-10.0.0.195}"
VLLM_PORT="${SERVICE_HELIOS_PORT:-8080}"

echo "[$(date)] Powering Helios on via smart plug..."
if curl -s --connect-timeout 2 "http://${HELIOS_IP}:${VLLM_PORT}/v1/models" >/dev/null 2>&1; then
  echo "[$(date)] Helios model already up: http://${HELIOS_IP}:${VLLM_PORT}/v1"
  exit 0
fi
echo "[$(date)] plug turn_on -> HTTP $(plug_set on)"
echo "[$(date)] Waiting for boot + model load (1-2 min)..."
MAX_WAIT=240; ELAPSED=0
while [ "$ELAPSED" -lt "$MAX_WAIT" ]; do
  if curl -s --connect-timeout 2 "http://${HELIOS_IP}:${VLLM_PORT}/v1/models" >/dev/null 2>&1; then
    echo; echo "[$(date)] Helios model ready: http://${HELIOS_IP}:${VLLM_PORT}/v1"
    exit 0
  fi
  echo -n "."; sleep 5; ELAPSED=$((ELAPSED + 5))
done
echo; echo "[$(date)] ERROR: model not ready after ${MAX_WAIT}s (plug=$(plug_state), draw=$(plug_watts)W)." >&2
exit 1
