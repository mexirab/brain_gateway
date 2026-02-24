#!/usr/bin/env bash
# Deploy Grafana kiosk display to Raspberry Pi (Callisto - 10.0.0.136)
# Run from the brain_gateway repo root on Jupiter.
#
# The Pi runs labwc (Wayland) via LightDM. The kiosk launches Chromium
# via labwc autostart inside the existing Wayland session.
#
# Prerequisites:
#   - Pi on the LAN at 10.0.0.136 with SSH enabled (labadmin user)
#   - HDMI display connected, labwc desktop session active
#
# Usage:
#   ./pi-kiosk/deploy.sh                # full deploy (bootstrap + start kiosk)
#   ./pi-kiosk/deploy.sh stop           # stop kiosk display
#   ./pi-kiosk/deploy.sh restart        # restart kiosk display
#   ./pi-kiosk/deploy.sh status         # check kiosk process status
#   ./pi-kiosk/deploy.sh ssh            # open SSH session to Pi
#   ./pi-kiosk/deploy.sh screenshot     # capture current screen

set -euo pipefail

PI_IP="${NODE_CALLISTO_IP:-10.0.0.136}"
PI_USER="${CALLISTO_SSH_USER:-labadmin}"
PI_DIR="/home/${PI_USER}/kiosk"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
JUPITER_IP="${NODE_JUPITER_IP:-10.0.0.248}"
GRAFANA_PORT="${MONITORING_GRAFANA_PORT:-3000}"

# Load .env if present
if [[ -f "${PROJECT_DIR}/.env" ]]; then
    # shellcheck disable=SC1091
    source "${PROJECT_DIR}/.env"
fi

ssh_cmd() {
    ssh -o ConnectTimeout=5 "${PI_USER}@${PI_IP}" "$@"
}

case "${1:-deploy}" in
    deploy)
        echo "==> Installing kiosk packages on Pi..."
        ssh_cmd "sudo apt-get update -qq && sudo apt-get install -y -qq \
            chromium curl grim"

        echo "==> Disabling screen blanking..."
        ssh_cmd "sudo raspi-config nonint do_blanking 1 2>/dev/null || true"

        echo "==> Creating ${PI_DIR} on Pi..."
        ssh_cmd "mkdir -p ${PI_DIR}"

        echo "==> Copying kiosk script..."
        scp -o ConnectTimeout=5 \
            "${SCRIPT_DIR}/kiosk.sh" \
            "${PI_USER}@${PI_IP}:${PI_DIR}/kiosk.sh"
        ssh_cmd "chmod +x ${PI_DIR}/kiosk.sh"

        echo "==> Configuring labwc autostart (replaces default desktop)..."
        ssh_cmd "mkdir -p ~/.config/labwc && cat > ~/.config/labwc/autostart << 'EOF'
# Kiosk mode — suppress default desktop (pcmanfm, panel, etc.)
/usr/bin/kanshi &
/home/${PI_USER}/kiosk/kiosk.sh &
EOF"

        echo "==> Disabling old systemd kiosk service (if any)..."
        ssh_cmd "sudo systemctl disable kiosk-chromium.service 2>/dev/null || true"

        echo "==> Starting kiosk..."
        ssh_cmd "pkill chromium 2>/dev/null || true; pkill pcmanfm 2>/dev/null || true; \
                 pkill wf-panel-pi 2>/dev/null || true; pkill lwrespawn 2>/dev/null || true; \
                 sleep 1; nohup ${PI_DIR}/kiosk.sh > /tmp/kiosk.log 2>&1 &"

        echo ""
        echo "==> Kiosk deployed!"
        echo "    Pi:      ${PI_IP}"
        echo "    Grafana: http://${JUPITER_IP}:${GRAFANA_PORT}"
        echo "    Manage:  ./pi-kiosk/deploy.sh {status|restart|stop|screenshot}"
        ;;

    stop)
        echo "==> Stopping kiosk on Pi..."
        ssh_cmd "pkill -f 'chromium.*kiosk-chromium' || true"
        ;;

    restart)
        echo "==> Restarting kiosk on Pi..."
        ssh_cmd "pkill -f 'chromium.*kiosk-chromium' || true; sleep 2; \
                 nohup ${PI_DIR}/kiosk.sh > /tmp/kiosk.log 2>&1 &"
        ;;

    status)
        echo "==> Kiosk process:"
        ssh_cmd "pgrep -af 'chromium.*kiosk-chromium' || echo 'Not running'"
        echo ""
        echo "==> Last kiosk log:"
        ssh_cmd "tail -5 /tmp/kiosk.log 2>/dev/null || echo 'No log'"
        ;;

    ssh)
        exec ssh "${PI_USER}@${PI_IP}"
        ;;

    screenshot)
        echo "==> Capturing screenshot from Pi..."
        ssh_cmd "WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/1000 \
                 grim /tmp/kiosk-screenshot.png"
        scp -o ConnectTimeout=5 \
            "${PI_USER}@${PI_IP}:/tmp/kiosk-screenshot.png" \
            "/tmp/callisto-screenshot.png"
        echo "    Saved to /tmp/callisto-screenshot.png"
        ;;

    *)
        echo "Usage: $0 {deploy|stop|restart|status|ssh|screenshot}"
        exit 1
        ;;
esac
