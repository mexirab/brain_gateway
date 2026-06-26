#!/bin/bash
# Power Helios OFF via its HA-managed Tapo smart plug (saves the GPU box's draw).
# NOTE: BIOS AC Back = Last State, so "sleep" = cut plug power WHILE Helios runs (a hard
# power-cut). That records "on" so a later plug-on auto-boots it. Safe: Helios is stateless
# post power-tiering (all DBs/ChromaDB live on the always-on node). For graceful shutdowns,
# set BIOS AC Back = Always On and use `ssh helios sudo poweroff` before cutting the plug.
# Usage: ./stop-helios.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
[[ -f "${PROJECT_DIR}/.env" ]] && { set -a; source "${PROJECT_DIR}/.env"; set +a; }
# shellcheck source=scripts/_helios_plug.sh
source "${SCRIPT_DIR}/_helios_plug.sh"

echo "[$(date)] Powering Helios off (cutting smart-plug power)..."
echo "[$(date)] plug turn_off -> HTTP $(plug_set off)"
sleep 4
echo "[$(date)] Plug now: $(plug_state) (draw: $(plug_watts)W)"
echo "[$(date)] Done. Wake with ./start-helios.sh."
