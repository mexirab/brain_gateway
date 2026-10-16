#!/usr/bin/env bash
# Nightly Actual Budget backup (Jupiter). Snapshots every SQLite file with the
# online backup API (consistent even mid-write), copies the budget blobs, archives
# the lot, rotates locally and mirrors to Saturn, then writes a Prometheus
# freshness metric. Runs as labadmin (the container runs as 1000:1000 too, so
# everything is readable without sudo).
set -uo pipefail
umask 077

DATA=${JESS_ACTUAL_DATA:-/opt/apps/actual/data}
BACKUP_DIR=/home/labadmin/actual/backups
REMOTE=${JESS_ACTUAL_BACKUP_REMOTE:-labadmin@saturn:/home/labadmin/actual-backups}
METRICS=${JESS_ACTUAL_BACKUP_METRICS_PATH:-/home/labadmin/node_exporter/textfile_collector/jess_actual_backup.prom}
KEEP=30

if [ ! -r "$DATA/server-files/account.sqlite" ]; then
  echo "[actual-backup] $DATA/server-files/account.sqlite missing or unreadable — wrong path or still root-owned?"; exit 2
fi

STAMP=$(date -u +%Y%m%d-%H%M%S)
mkdir -p "$BACKUP_DIR"
# A SIGKILL/OOM mid-run skips the EXIT trap and leaves a plaintext stage dir.
find "$BACKUP_DIR" -maxdepth 1 -name '.stage-*' -mmin +120 -exec rm -rf {} +
STAGE=$(mktemp -d "$BACKUP_DIR/.stage-XXXXXX") || { echo "[actual-backup] mktemp failed"; exit 2; }
ARCHIVE="$BACKUP_DIR/actual-$STAMP.tar.gz"
TMP="$ARCHIVE.tmp"
trap 'rm -rf "$STAGE" "$TMP"' EXIT

# *.sqlite → sqlite3 online backup (WAL/journal-safe). Everything else (budget
# .blob uploads, .migrate) → plain copy, retried once if the file changed while
# being read (blobs are rewritten in place on upload, not renamed). Symlinks are
# skipped: the container writes this dir as uid 1000, so a compromised server
# could plant a link to a labadmin secret and have it shipped to Saturn.
if ! python3 -I - "$DATA" "$STAGE/data" <<'PY'
import os, shutil, sqlite3, sys, urllib.parse
src_root, dst_root = sys.argv[1], sys.argv[2]
for dirpath, _dirs, files in os.walk(src_root):
    rel = os.path.relpath(dirpath, src_root)
    os.makedirs(os.path.join(dst_root, rel), exist_ok=True)
    for name in files:
        if name.endswith(("-wal", "-shm", "-journal")):
            continue  # folded into the .sqlite snapshot
        src, dst = os.path.join(dirpath, name), os.path.join(dst_root, rel, name)
        if os.path.islink(src):
            print(f"[actual-backup] skipping symlink {os.path.join(rel, name)}")
            continue
        if name.endswith(".sqlite"):
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
  echo "[actual-backup] snapshot failed"; exit 2
fi

if ! tar czf "$TMP" -C "$STAGE" data; then
  echo "[actual-backup] tar failed"; exit 2
fi
mv "$TMP" "$ARCHIVE" || { echo "[actual-backup] mv failed"; exit 2; }   # 600 via umask: budget data is personal content
FILES=$(find "$STAGE/data/user-files" -type f | wc -l)

OFFBOX=0
if [ -n "$REMOTE" ]; then
  if rsync -az --timeout=60 "$ARCHIVE" "$REMOTE/"; then OFFBOX=1; else echo "[actual-backup] off-box rsync failed (rc=$?)"; fi
fi

# Rotate: keep newest $KEEP
ls -1t "$BACKUP_DIR"/actual-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

if [ -n "$METRICS" ]; then
  mkdir -p "$(dirname "$METRICS")"
  {
    echo "# HELP jess_actual_backup_success_timestamp_seconds Unix time of last successful Actual Budget backup."
    echo "# TYPE jess_actual_backup_success_timestamp_seconds gauge"
    echo "jess_actual_backup_success_timestamp_seconds $(date +%s)"
    echo "# HELP jess_actual_backup_offbox_success Whether the last Actual backup copied off-box (1) or not (0)."
    echo "# TYPE jess_actual_backup_offbox_success gauge"
    echo "jess_actual_backup_offbox_success $OFFBOX"
    echo "# HELP jess_actual_backup_files Number of files under user-files/ in the last Actual backup."
    echo "# TYPE jess_actual_backup_files gauge"
    echo "jess_actual_backup_files $FILES"
  } > "$METRICS.tmp" && mv "$METRICS.tmp" "$METRICS"
fi

echo "[actual-backup] wrote $(basename "$ARCHIVE") ($(du -h "$ARCHIVE" | cut -f1)), offbox=$OFFBOX files=$FILES"
