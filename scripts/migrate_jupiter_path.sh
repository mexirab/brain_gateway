#!/usr/bin/env bash
# Migrate the repo on Jupiter from /opt/jupiter/gateway_mvp → /opt/gateway_mvp
# so every box uses the same install path (FHS-standard, no hostname prefix).
#
# This mirrors what was done on Helios in commit 1e0df3a. Safe to re-run —
# it no-ops if the new path already exists and the old one is gone.
#
# Run as labadmin on Jupiter after `git pull` on an existing checkout:
#   cd /opt/jupiter/gateway_mvp && git pull
#   bash scripts/migrate_jupiter_path.sh

set -euo pipefail

OLD=/opt/jupiter/gateway_mvp
NEW=/opt/gateway_mvp

if [ -d "$NEW" ] && [ ! -e "$OLD" ]; then
  echo "[migrate] Already migrated — $NEW exists, $OLD is gone. Nothing to do."
  exit 0
fi

if [ ! -d "$OLD" ]; then
  echo "[migrate] ERROR: expected $OLD to exist but it doesn't. Aborting."
  exit 1
fi

if [ -e "$NEW" ]; then
  echo "[migrate] ERROR: $NEW already exists — refusing to overwrite. Resolve manually."
  exit 1
fi

echo "[migrate] stopping monitoring stack..."
cd "$OLD"
docker compose -f monitoring/docker-compose.yml down || true

echo "[migrate] moving $OLD → $NEW"
sudo mv "$OLD" "$NEW"

# Remove the now-empty /opt/jupiter if nothing else lives there. Best-effort.
sudo rmdir /opt/jupiter 2>/dev/null || \
  echo "[migrate] /opt/jupiter not removed (not empty or already gone) — review manually"

echo "[migrate] repairing git worktrees (if any)..."
cd "$NEW"
git worktree repair 2>&1 || true

echo "[migrate] updating .env GATEWAY_ROOT_PATH..."
if grep -q '^GATEWAY_ROOT_PATH=/opt/jupiter/gateway_mvp' .env 2>/dev/null; then
  sed -i 's|^GATEWAY_ROOT_PATH=/opt/jupiter/gateway_mvp|GATEWAY_ROOT_PATH=/opt/gateway_mvp|' .env
fi
if [ -f .env ]; then
  grep ^GATEWAY_ROOT_PATH= .env || echo "[migrate] (no GATEWAY_ROOT_PATH in .env — may not be used on this host)"
fi

if [ -f .claude/settings.local.json ]; then
  echo "[migrate] updating .claude/settings.local.json..."
  sed -i 's|/opt/jupiter/gateway_mvp|/opt/gateway_mvp|g' .claude/settings.local.json
fi

echo "[migrate] starting monitoring stack from new location..."
docker compose -f monitoring/docker-compose.yml pull
docker compose -f monitoring/docker-compose.yml up -d

echo "[migrate] done. Verify:"
echo "  - docker ps       (grafana/prometheus/loki/blackbox-exporter/promtail up)"
echo "  - curl http://localhost:3000  (Grafana)"
echo "  - curl http://localhost:9090  (Prometheus)"
echo
echo "[migrate] If your shell was cd'd inside $OLD, start a fresh shell or cd $NEW."
