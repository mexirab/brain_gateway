#!/usr/bin/env python3
"""Back up Jess's irreplaceable runtime state — independently of the orchestrator.

Why this exists
---------------
`data/` is ~1.9 GB, but almost all of that is `hf_cache/` (the HuggingFace
model cache, which re-downloads on demand). The genuinely irreplaceable state
is tiny (~15 MB) and has never had a backup:

  - data/app/*.db        brain_state / progress / finance (reminders, focus,
                         routines, selfcare, workouts, meals, chat history…)
  - data/app/auto_learn.key   the Fernet key that decrypts learned personal
                         facts — LOSE THIS AND THE ENCRYPTED MEMORIES ARE GONE
                         FOREVER, even though the ciphertext survives.
  - data/chroma/         the RAG / mempalace vector store
  - data/app/ subdirs    self-audits, budget summaries, imports, meal photos,
                         paperless inbox, training corpus, documents
  - credentials/         the Google OAuth token + client secret

This script is deliberately standalone and stdlib-only: it snapshots the live
SQLite databases *consistently* (Python's `sqlite3` `.backup()` copies a WAL
database mid-write without tearing), archives the small critical set, rotates
old archives, optionally rsyncs off-box, and — like scripts/refresh_google_token.py
— writes a Prometheus textfile metric the JessBackupStale alert watches. It
runs fine while the orchestrator container is writing to the DBs, and even when
the container is down.

Configuration (all optional; sensible defaults):
  JESS_DATA_DIR           default <repo>/data
  JESS_CREDENTIALS_DIR    default <repo>/credentials
  JESS_BACKUP_DIR         default <repo>/backups   (where archives are written)
  JESS_BACKUP_KEEP        default 30               (archives to retain locally)
  JESS_BACKUP_REMOTE      optional rsync target, e.g. user@saturn:/backups/jess
                          (set up key-based SSH first; empty = local only)
  JESS_BACKUP_METRICS_PATH  optional Prometheus textfile, e.g.
                          /home/labadmin/node_exporter/textfile_collector/jess_backup.prom

Exit codes: 0 = backup written · 1 = nothing to back up (data dir missing) ·
2 = failed.

Restore: see docs/BACKUP.md — it is just "stop the container, extract the tar
into data/, `chmod 600` the key, start". The archive is a plain tar.gz.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.environ.get("JESS_DATA_DIR", REPO_ROOT / "data"))
CREDENTIALS_DIR = Path(os.environ.get("JESS_CREDENTIALS_DIR", REPO_ROOT / "credentials"))
BACKUP_DIR = Path(os.environ.get("JESS_BACKUP_DIR", REPO_ROOT / "backups"))
KEEP = int(os.environ.get("JESS_BACKUP_KEEP", "30"))
REMOTE = os.environ.get("JESS_BACKUP_REMOTE", "").strip()
METRICS_PATH = os.environ.get("JESS_BACKUP_METRICS_PATH", "").strip()

# Subtrees under data/ that are large and reconstructable — never worth backing
# up. hf_cache re-downloads from HuggingFace; app-backup-* are prior one-off
# backups (don't back up backups); backups/ is our own output if nested.
EXCLUDE_DIRS = {"hf_cache", "backups"}
EXCLUDE_DIR_PREFIXES = ("app-backup-",)

# SQLite sidecar files: never copy these raw. The .backup() snapshot already
# folds the WAL into a single consistent file, and copying a live -wal/-shm
# alongside a raw .db is exactly how you capture a torn database.
SQLITE_SIDECAR_SUFFIXES = (".db-wal", ".db-shm", ".sqlite3-wal", ".sqlite3-shm")

# Files whose loss is unrecoverable — if one of these EXISTS but can't be
# captured, the whole backup is a failure (a backup silently missing
# brain_state.db or the Fernet key is worse than a loud error). Everything else
# (training corpus shards, setup_state.json, meal photos…) is best-effort: an
# unreadable or vanished file is skipped with a warning, not fatal. The
# orchestrator container runs as root and writes some data/app files root-owned,
# so a host-user cron legitimately can't read every file.
CRITICAL_NAMES = {"auto_learn.key"}
CRITICAL_SUFFIXES = (".db", ".sqlite3")


def _is_critical(path: Path) -> bool:
    return path.name in CRITICAL_NAMES or path.suffix in CRITICAL_SUFFIXES


def _log(msg: str) -> None:
    print(f"[backup] {msg}", flush=True)


def _is_excluded(rel: Path) -> bool:
    parts = rel.parts
    if any(p in EXCLUDE_DIRS for p in parts):
        return True
    return any(p.startswith(pref) for p in parts for pref in EXCLUDE_DIR_PREFIXES)


def _is_sqlite_db(path: Path) -> bool:
    if path.suffix not in (".db", ".sqlite3"):
        return False
    # Confirm it is really a SQLite file, not something that merely ends in .db.
    try:
        with open(path, "rb") as fh:
            return fh.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _snapshot_sqlite(src: Path, dst: Path) -> None:
    """Consistent hot copy of a live SQLite DB via the online backup API."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    # immutable/uri open would refuse a WAL db; a normal read-only connection is
    # correct and .backup() takes care of a concurrent writer.
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


class StageResult:
    def __init__(self) -> None:
        self.count = 0
        self.skipped: list[str] = []  # non-critical files we couldn't read/copy
        self.critical_failures: list[str] = []  # critical files that existed but failed


def _stage_tree(src_root: Path, staging: Path, arcprefix: str, result: StageResult) -> None:
    """Copy src_root into staging/<arcprefix>, snapshotting SQLite DBs and
    skipping excluded subtrees and raw WAL/SHM sidecars.

    Best-effort: an unreadable or mid-run-vanished file is recorded and skipped
    rather than aborting the whole backup — EXCEPT a critical file (see
    _is_critical), whose failure is recorded as fatal.
    """
    if not src_root.exists():
        return
    for path in src_root.rglob("*"):
        try:
            if path.is_dir():
                continue
            rel = path.relative_to(src_root)
            if _is_excluded(rel):
                continue
            if path.name.endswith(SQLITE_SIDECAR_SUFFIXES):
                continue
            dest = staging / arcprefix / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if _is_sqlite_db(path):
                _snapshot_sqlite(path, dest)
            else:
                # copy2 preserves mode/mtime — important for auto_learn.key (0600).
                shutil.copy2(path, dest)
            result.count += 1
        except (OSError, sqlite3.Error) as exc:
            label = f"{arcprefix}/{path.name}"
            if _is_critical(path):
                result.critical_failures.append(label)
                _log(f"ERROR could not back up critical file {label}: {exc}")
            else:
                result.skipped.append(label)
                _log(f"WARN skipped unreadable/vanished file {label}: {exc}")


def _rotate(keep: int) -> None:
    archives = sorted(BACKUP_DIR.glob("jess-state-*.tar.gz"))
    stale = archives[:-keep] if keep > 0 else []
    for old in stale:
        try:
            old.unlink()
            _log(f"rotated out {old.name}")
        except OSError as exc:
            _log(f"WARN could not remove {old.name}: {exc}")


def _rsync_offbox(archive: Path, remote: str) -> bool:
    if not shutil.which("rsync"):
        _log("WARN JESS_BACKUP_REMOTE set but rsync not installed — skipping off-box copy")
        return False
    dest = remote if remote.endswith("/") else remote + "/"
    try:
        subprocess.run(
            ["rsync", "-az", "--timeout=60", str(archive), dest],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        _log(f"copied off-box to {remote}")
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        _log(f"WARN off-box rsync failed: {detail.strip()[:200]}")
        return False


def _write_metric(ok: bool, size_bytes: int, offbox_ok: bool, skipped: int = 0) -> None:
    if not METRICS_PATH:
        return
    now = int(time.time())
    lines = [
        "# HELP jess_backup_success_timestamp_seconds Unix time of the last successful Jess state backup.",
        "# TYPE jess_backup_success_timestamp_seconds gauge",
    ]
    if ok:
        lines.append(f"jess_backup_success_timestamp_seconds {now}")
        lines.append("# HELP jess_backup_size_bytes Size of the last state backup archive.")
        lines.append("# TYPE jess_backup_size_bytes gauge")
        lines.append(f"jess_backup_size_bytes {size_bytes}")
        lines.append("# HELP jess_backup_offbox_success Whether the last backup also copied off-box (1) or not (0).")
        lines.append("# TYPE jess_backup_offbox_success gauge")
        lines.append(f"jess_backup_offbox_success {1 if offbox_ok else 0}")
        lines.append(
            "# HELP jess_backup_skipped_files Non-critical files skipped (unreadable/vanished) in the last backup."
        )
        lines.append("# TYPE jess_backup_skipped_files gauge")
        lines.append(f"jess_backup_skipped_files {skipped}")
    try:
        target = Path(METRICS_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text("\n".join(lines) + "\n")
        tmp.replace(target)  # atomic — node_exporter never reads a half-written file
    except OSError as exc:
        _log(f"WARN could not write metric to {METRICS_PATH}: {exc}")


def main() -> int:
    if not DATA_DIR.exists():
        _log(f"data dir {DATA_DIR} does not exist — nothing to back up")
        return 1

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    # A build-safe, sortable, timezone-free stamp (UTC).
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    archive = BACKUP_DIR / f"jess-state-{stamp}.tar.gz"

    result = StageResult()
    try:
        with tempfile.TemporaryDirectory(prefix="jess-backup-") as tmpdir:
            staging = Path(tmpdir)
            _stage_tree(DATA_DIR, staging, "data", result)
            _stage_tree(CREDENTIALS_DIR, staging, "credentials", result)

            # A critical file existed but couldn't be captured (e.g. a
            # root-owned brain_state.db a host-user cron can't read). Failing
            # loudly beats writing a backup that's silently missing the state
            # it exists to protect.
            if result.critical_failures:
                _log(f"ERROR critical files could not be backed up: {result.critical_failures}")
                _write_metric(False, 0, False)
                return 2

            if result.count == 0:
                _log("no files matched — refusing to write an empty backup")
                _write_metric(False, 0, False)
                return 1

            with tarfile.open(archive, "w:gz") as tar:
                for child in sorted(staging.iterdir()):
                    tar.add(child, arcname=child.name)
        # Lock the archive down: it contains the OAuth token and the Fernet key.
        os.chmod(archive, 0o600)
    except Exception as exc:  # noqa: BLE001 — top-level guard so a failure still records the metric
        _log(f"ERROR backup failed: {exc}")
        _write_metric(False, 0, False)
        return 2

    size = archive.stat().st_size
    skipped_note = f", {len(result.skipped)} skipped" if result.skipped else ""
    _log(f"wrote {archive.name} ({size / 1024:.0f} KiB, {result.count} files{skipped_note})")

    offbox_ok = _rsync_offbox(archive, REMOTE) if REMOTE else False
    _rotate(KEEP)
    _write_metric(True, size, offbox_ok, skipped=len(result.skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
