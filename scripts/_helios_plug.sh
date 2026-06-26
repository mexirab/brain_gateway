#!/bin/bash
# Shared helper: control Helios power via its Home-Assistant-managed Tapo smart plug.
# Post power-tiering, Helios's NIC Wake-on-LAN is a dead end (Aquantia atlantic driver),
# so power on/off = toggling the smart plug through HA — the same mechanism the
# orchestrator's helios_power module uses. Sourced by start/stop/status-helios.sh.
# Requires HA_URL + HA_TOKEN in the environment (source the project .env first).

HA_URL="${HA_URL:-}"
HA_TOKEN="${HA_TOKEN:-}"
HELIOS_PLUG_ENTITY="${HELIOS_PLUG_ENTITY:-switch.helios_monitoring_plug}"
HELIOS_PLUG_POWER_SENSOR="${HELIOS_PLUG_POWER_SENSOR:-sensor.helios_monitoring_plug_current_consumption}"

_plug_require_ha() {
  if [[ -z "$HA_URL" || -z "$HA_TOKEN" ]]; then
    echo "ERROR: HA_URL / HA_TOKEN not set. Run from the always-on node's checkout so .env is sourced." >&2
    return 1
  fi
}

# plug_set on|off  -> prints the HA HTTP status code
plug_set() {
  _plug_require_ha || return 1
  curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
    -H "Authorization: Bearer ${HA_TOKEN}" -H "Content-Type: application/json" \
    -X POST "${HA_URL}/api/services/switch/turn_${1}" \
    -d "{\"entity_id\":\"${HELIOS_PLUG_ENTITY}\"}"
}

# plug_state -> on|off|unknown
plug_state() {
  _plug_require_ha || { echo unknown; return 1; }
  curl -s --max-time 8 -H "Authorization: Bearer ${HA_TOKEN}" \
    "${HA_URL}/api/states/${HELIOS_PLUG_ENTITY}" \
    | python3 -c "import sys,json;print(json.load(sys.stdin).get('state','unknown'))" 2>/dev/null || echo unknown
}

# plug_watts -> number|?
plug_watts() {
  _plug_require_ha || { echo "?"; return 1; }
  curl -s --max-time 8 -H "Authorization: Bearer ${HA_TOKEN}" \
    "${HA_URL}/api/states/${HELIOS_PLUG_POWER_SENSOR}" \
    | python3 -c "import sys,json;print(json.load(sys.stdin).get('state','?'))" 2>/dev/null || echo "?"
}
