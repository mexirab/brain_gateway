#!/usr/bin/env bash
# Deploy Pi-hole secondary to Saturn (10.0.0.58)
# Run from the brain_gateway repo root on Jupiter or your workstation.
#
# Prerequisites:
#   - SSH key access to Saturn (ssh nadim@10.0.0.58)
#   - PIHOLE_PASSWORD set in .env or exported
#
# Usage:
#   ./saturn/deploy-pihole.sh            # deploy and start
#   ./saturn/deploy-pihole.sh stop       # stop Pi-hole on Saturn
#   ./saturn/deploy-pihole.sh logs       # tail Pi-hole logs on Saturn

set -euo pipefail

SATURN_IP="${NODE_SATURN_IP:-10.0.0.58}"
SATURN_USER="${SATURN_SSH_USER:-nadim}"
SATURN_DIR="/opt/saturn/pihole"
COMPOSE_FILE="docker-compose.pihole.yml"

# Load .env if present (for PIHOLE_PASSWORD)
if [[ -f "$(dirname "$0")/../.env" ]]; then
    # shellcheck disable=SC1091
    source "$(dirname "$0")/../.env"
fi

PIHOLE_PASSWORD="${PIHOLE_PASSWORD:?PIHOLE_PASSWORD must be set}"

ssh_cmd() {
    ssh -o ConnectTimeout=5 "${SATURN_USER}@${SATURN_IP}" "$@"
}

case "${1:-deploy}" in
    deploy)
        echo "==> Creating ${SATURN_DIR} on Saturn..."
        ssh_cmd "sudo mkdir -p ${SATURN_DIR}"
        ssh_cmd "sudo chown ${SATURN_USER}:${SATURN_USER} ${SATURN_DIR}"

        echo "==> Copying compose file..."
        scp -o ConnectTimeout=5 \
            "$(dirname "$0")/${COMPOSE_FILE}" \
            "${SATURN_USER}@${SATURN_IP}:${SATURN_DIR}/${COMPOSE_FILE}"

        echo "==> Starting Pi-hole on Saturn..."
        ssh_cmd "cd ${SATURN_DIR} && PIHOLE_PASSWORD='${PIHOLE_PASSWORD}' docker compose -f ${COMPOSE_FILE} up -d"

        echo "==> Waiting for Pi-hole to start..."
        sleep 5

        echo "==> Checking Pi-hole health..."
        if ssh_cmd "curl -sf http://localhost:8053/api" > /dev/null 2>&1; then
            echo "Pi-hole is running on Saturn (http://${SATURN_IP}:8053/admin)"
        else
            echo "Warning: Pi-hole API not responding yet — may still be initializing"
        fi
        ;;

    stop)
        echo "==> Stopping Pi-hole on Saturn..."
        ssh_cmd "cd ${SATURN_DIR} && docker compose -f ${COMPOSE_FILE} down"
        ;;

    logs)
        ssh_cmd "docker logs pihole --tail 50 -f"
        ;;

    *)
        echo "Usage: $0 {deploy|stop|logs}"
        exit 1
        ;;
esac
