#!/usr/bin/env bash
# Nightly Radicale (self-hosted calendar) backup (Jupiter). Takes Radicale's own
# storage lock in shared mode so no write can tear the copy, archives the
# collections, rotates locally and mirrors to Saturn, then writes a Prometheus
# freshness metric. Runs as labadmin (the container runs as 1000:1000 too, so
# everything is readable without sudo).
set -uo pipefail
umask 077

DATA=/home/labadmin/radicale/data
BACKUP_DIR=/home/labadmin/radicale/backups
REMOTE=${JESS_CALENDAR_BACKUP_REMOTE:-labadmin@saturn:/home/labadmin/calendar-backups}
METRICS=${JESS_CALENDAR_BACKUP_METRICS_PATH:-/home/labadmin/node_exporter/textfile_collector/jess_calendar_backup.prom}
KEEP=30

STAMP=$(date -u +%Y%m%d-%H%M%S)
mkdir -p "$BACKUP_DIR"
ARCHIVE="$BACKUP_DIR/radicale-$STAMP.tar.gz"
TMP="$ARCHIVE.tmp"
trap 'rm -f "$TMP"' EXIT

# Radicale takes fcntl.flock on collections/.Radicale.lock: exclusive for writes,
# shared for reads. Holding it shared blocks writes for the few ms the tar takes.
# Reads still rewrite .Radicale.cache/ under the shared lock, so the cache (fully
# regenerable; clients do one full resync after a restore) is excluded, or tar
# would fail with "file changed as we read it".
LOCK="$DATA/collections/.Radicale.lock"
if [ ! -e "$LOCK" ]; then
  echo "[calendar-backup] $LOCK missing — is Radicale's data dir mounted?"; exit 2
fi
if ! flock -s -w 60 "$LOCK" tar czf "$TMP" --exclude='.Radicale.cache' --exclude='.Radicale.tmp-*' \
    -C "$DATA" collections; then
  echo "[calendar-backup] tar failed (or lock not acquired in 60s)"; exit 2
fi
mv "$TMP" "$ARCHIVE"   # 600 via umask: the calendars themselves are personal content
ITEMS=$(find "$DATA/collections" -name '*.ics' -not -path '*/.Radicale.cache/*' | wc -l)

OFFBOX=0
if [ -n "$REMOTE" ]; then
  if rsync -az --timeout=60 "$ARCHIVE" "$REMOTE/"; then OFFBOX=1; else echo "[calendar-backup] off-box rsync failed (rc=$?)"; fi
fi

# Rotate: keep newest $KEEP
ls -1t "$BACKUP_DIR"/radicale-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

if [ -n "$METRICS" ]; then
  mkdir -p "$(dirname "$METRICS")"
  {
    echo "# HELP jess_calendar_backup_success_timestamp_seconds Unix time of last successful Radicale calendar backup."
    echo "# TYPE jess_calendar_backup_success_timestamp_seconds gauge"
    echo "jess_calendar_backup_success_timestamp_seconds $(date +%s)"
    echo "# HELP jess_calendar_backup_offbox_success Whether the last calendar backup copied off-box (1) or not (0)."
    echo "# TYPE jess_calendar_backup_offbox_success gauge"
    echo "jess_calendar_backup_offbox_success $OFFBOX"
    echo "# HELP jess_calendar_backup_items Number of calendar items (.ics) in the last backup."
    echo "# TYPE jess_calendar_backup_items gauge"
    echo "jess_calendar_backup_items $ITEMS"
  } > "$METRICS.tmp" && mv "$METRICS.tmp" "$METRICS"
fi

echo "[calendar-backup] wrote $(basename "$ARCHIVE") ($(du -h "$ARCHIVE" | cut -f1)), offbox=$OFFBOX items=$ITEMS"
