#!/usr/bin/env bash
# Nightly Home Assistant backup (Jupiter). Consistent recorder-DB snapshot +
# durable config, rotated locally and mirrored to Saturn, with a Prometheus
# freshness metric. Runs as labadmin; HA writes some files as root, so the copy
# is best-effort on unreadable files but always captures .storage + configs.
set -uo pipefail

CONFIG=/home/labadmin/homeassistant/config
BACKUP_DIR=/home/labadmin/homeassistant/backups
REMOTE=${JESS_HA_BACKUP_REMOTE:-labadmin@saturn:/home/labadmin/ha-backups}
METRICS=${JESS_HA_BACKUP_METRICS_PATH:-/home/labadmin/node_exporter/textfile_collector/jess_ha_backup.prom}
KEEP=14

STAMP=$(date -u +%Y%m%d-%H%M%S)
mkdir -p "$BACKUP_DIR"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/config"

# Durable config: exclude regenerable caches, logs, and raw DB/WAL/SHM (the DB
# is snapshotted separately below so a live WAL write can't tear the copy).
rsync -a \
  --exclude 'tts/' --exclude '.cache/' --exclude 'deps/' --exclude 'backups/' \
  --exclude '*.log*' --exclude '*.db' --exclude '*.db-wal' --exclude '*.db-shm' \
  "$CONFIG/" "$STAGE/config/" 2>/dev/null

# Consistent snapshot of the recorder DB via SQLite's online backup API.
python3 - "$CONFIG/home-assistant_v2.db" "$STAGE/config/home-assistant_v2.db" <<'PY'
import os, sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
if os.path.exists(src):
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    try:
        d = sqlite3.connect(dst)
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()
PY

ARCHIVE="$BACKUP_DIR/ha-config-$STAMP.tar.gz"
tar czf "$ARCHIVE" -C "$STAGE" config || { echo "[ha-backup] tar failed"; exit 2; }
chmod 600 "$ARCHIVE"   # contains secrets.yaml + .storage/auth tokens

OFFBOX=0
if [ -n "$REMOTE" ]; then
  if rsync -az --timeout=60 "$ARCHIVE" "$REMOTE/" 2>/dev/null; then OFFBOX=1; fi
fi

# Rotate: keep newest $KEEP
ls -1t "$BACKUP_DIR"/ha-config-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

if [ -n "$METRICS" ]; then
  mkdir -p "$(dirname "$METRICS")"
  {
    echo "# HELP jess_ha_backup_success_timestamp_seconds Unix time of last successful HA backup."
    echo "# TYPE jess_ha_backup_success_timestamp_seconds gauge"
    echo "jess_ha_backup_success_timestamp_seconds $(date +%s)"
    echo "# HELP jess_ha_backup_offbox_success Whether the last HA backup copied off-box (1) or not (0)."
    echo "# TYPE jess_ha_backup_offbox_success gauge"
    echo "jess_ha_backup_offbox_success $OFFBOX"
  } > "$METRICS.tmp" && mv "$METRICS.tmp" "$METRICS"
fi

echo "[ha-backup] wrote $(basename "$ARCHIVE") ($(du -h "$ARCHIVE" | cut -f1)), offbox=$OFFBOX"
