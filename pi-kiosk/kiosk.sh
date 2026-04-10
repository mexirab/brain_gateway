#!/usr/bin/env bash
# Chromium kiosk mode for Grafana dashboards
# Runs fullscreen on the Pi's Wayland (labwc) session.
#
# Launched by labwc autostart (~/.config/labwc/autostart).
# Manual: ~/kiosk/kiosk.sh

set -euo pipefail

GRAFANA_HOST="${GRAFANA_HOST:-10.0.0.195:3000}"
DASHBOARD_UID="${GRAFANA_DASHBOARD:-brain-gateway-overview}"
GRAFANA_URL="http://${GRAFANA_HOST}/d/${DASHBOARD_UID}?orgId=1&kiosk&refresh=30s"

export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# Wait for Wayland compositor to be ready
echo "Waiting for Wayland socket..."
for i in $(seq 1 30); do
    if [ -S "${XDG_RUNTIME_DIR}/wayland-0" ]; then
        echo "Wayland compositor is ready."
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "ERROR: Wayland socket not found after 30s, aborting."
        exit 1
    fi
    sleep 1
done

# Wait for Grafana to be reachable
echo "Waiting for Grafana at ${GRAFANA_HOST}..."
for i in $(seq 1 60); do
    if curl -sf "http://${GRAFANA_HOST}/api/health" > /dev/null 2>&1; then
        echo "Grafana is ready."
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "WARNING: Grafana not responding after 120s, launching anyway."
    fi
    sleep 2
done

# Kill any existing Chromium kiosk instances (from previous runs)
pkill -f 'chromium.*kiosk-chromium' 2>/dev/null || true
sleep 1

# Separate user-data-dir to avoid merging with the desktop Chromium session
KIOSK_DATA="${HOME}/.kiosk-chromium"
mkdir -p "${KIOSK_DATA}"

# Launch Chromium in kiosk mode on Wayland
# Memory-optimized flags for Pi 4 (2GB RAM)
exec chromium \
    --user-data-dir="${KIOSK_DATA}" \
    --password-store=basic \
    --noerrdialogs \
    --disable-infobars \
    --kiosk \
    --ozone-platform=wayland \
    --disable-session-crashed-bubble \
    --disable-component-update \
    --check-for-update-interval=31536000 \
    --disable-translate \
    --no-first-run \
    --start-fullscreen \
    --window-position=0,0 \
    --disable-dev-shm-usage \
    "${GRAFANA_URL}"
