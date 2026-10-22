#!/usr/bin/env bash
# Nightly Grist backup (Jupiter). Every Grist document (docs/*.grist) and the
# home DB (home.sqlite3: users, orgs, API keys) is a SQLite file: each is
# snapshotted with the online backup API (consistent even mid-write), the lot
# is archived, rotated locally and mirrored to Saturn, then a Prometheus
# freshness metric is written. Runs as labadmin (the container runs as
# 1000:1000 too, so everything is readable without sudo). Same shape as
# actual/backup_actual.sh.
set -uo pipefail
umask 077

DATA=${JESS_GRIST_DATA:-/opt/apps/grist/persist}
BACKUP_DIR=/home/labadmin/grist/backups
REMOTE=${JESS_GRIST_BACKUP_REMOTE:-labadmin@saturn:/home/labadmin/grist-backups}
METRICS=${JESS_GRIST_BACKUP_METRICS_PATH:-/home/labadmin/node_exporter/textfile_collector/jess_grist_backup.prom}
KEEP=30

if [ ! -r "$DATA/home.sqlite3" ]; then
  echo "[grist-backup] $DATA/home.sqlite3 missing or unreadable — wrong path, or Grist never started?"; exit 2
fi

STAMP=$(date -u +%Y%m%d-%H%M%S)
mkdir -p "$BACKUP_DIR"
# A SIGKILL/OOM mid-run skips the EXIT trap and leaves a plaintext stage dir.
find "$BACKUP_DIR" -maxdepth 1 -name '.stage-*' -mmin +120 -exec rm -rf {} +
STAGE=$(mktemp -d "$BACKUP_DIR/.stage-XXXXXX") || { echo "[grist-backup] mktemp failed"; exit 2; }
ARCHIVE="$BACKUP_DIR/grist-$STAMP.tar.gz"
TMP="$ARCHIVE.tmp"
trap 'rm -rf "$STAGE" "$TMP"' EXIT

# SQLite files (.grist docs, home.sqlite3) → sqlite3 online backup. Sessions
# (grist-sessions.db) are skipped: login is forwarded-header, nothing to keep.
# Other files → plain copy, retried once if the file changed while being read.
# Symlinks are skipped: the container writes this dir as uid 1000, so a
# compromised server could plant a link to a labadmin secret.
if ! python3 -I - "$DATA" "$STAGE/data" <<'PY'
import os, shutil, sqlite3, sys, urllib.parse
src_root, dst_root = sys.argv[1], sys.argv[2]
SQLITE = (".grist", ".sqlite3")
for dirpath, _dirs, files in os.walk(src_root):
    rel = os.path.relpath(dirpath, src_root)
    os.makedirs(os.path.join(dst_root, rel), exist_ok=True)
    for name in files:
        if name.endswith(("-wal", "-shm", "-journal")) or name == "grist-sessions.db":
            continue
        src, dst = os.path.join(dirpath, name), os.path.join(dst_root, rel, name)
        if os.path.islink(src):
            print(f"[grist-backup] skipping symlink {os.path.join(rel, name)}")
            continue
        if name.endswith(SQLITE):
            # mode=ro on purpose: a hot journal after a crash makes this fail
            # closed (stale alert) instead of the backup rolling it back.
            s = sqlite3.connect("file:" + urllib.parse.quote(src) + "?mode=ro", uri=True, timeout=30)
            d = sqlite3.connect(dst)
            s.backup(d)
            ok = d.execute("PRAGMA quick_check").fetchone()[0]
            d.close(); s.close()
            if ok != "ok":
                sys.exit(f"{os.path.join(rel, name)} failed quick_check")
            continue
        for _ in range(2):
            before = os.stat(src)
            shutil.copy2(src, dst)
            after = os.stat(src)
            if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns):
                break
        else:
            sys.exit(f"{os.path.join(rel, name)} kept changing during copy")
PY
then
  echo "[grist-backup] snapshot failed"; exit 2
fi

if ! tar czf "$TMP" -C "$STAGE" data; then
  echo "[grist-backup] tar failed"; exit 2
fi
mv "$TMP" "$ARCHIVE" || { echo "[grist-backup] mv failed"; exit 2; }   # 600 via umask: sheets are personal content
DOCS=$(find "$STAGE/data" -name '*.grist' -type f | wc -l)

OFFBOX=0
if [ -n "$REMOTE" ]; then
  if rsync -az --timeout=60 "$ARCHIVE" "$REMOTE/"; then OFFBOX=1; else echo "[grist-backup] off-box rsync failed (rc=$?)"; fi
fi

# Rotate: keep newest $KEEP
ls -1t "$BACKUP_DIR"/grist-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

if [ -n "$METRICS" ]; then
  mkdir -p "$(dirname "$METRICS")"
  {
    echo "# HELP jess_grist_backup_success_timestamp_seconds Unix time of last successful Grist backup."
    echo "# TYPE jess_grist_backup_success_timestamp_seconds gauge"
    echo "jess_grist_backup_success_timestamp_seconds $(date +%s)"
    echo "# HELP jess_grist_backup_offbox_success Whether the last Grist backup copied off-box (1) or not (0)."
    echo "# TYPE jess_grist_backup_offbox_success gauge"
    echo "jess_grist_backup_offbox_success $OFFBOX"
    echo "# HELP jess_grist_backup_docs Number of Grist documents in the last backup."
    echo "# TYPE jess_grist_backup_docs gauge"
    echo "jess_grist_backup_docs $DOCS"
  } > "$METRICS.tmp" && mv "$METRICS.tmp" "$METRICS"
fi

echo "[grist-backup] wrote $(basename "$ARCHIVE") ($(du -h "$ARCHIVE" | cut -f1)), offbox=$OFFBOX docs=$DOCS"
