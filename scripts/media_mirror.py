#!/usr/bin/env python3
"""Nightly second copy of the irreplaceable folders on Jupiter's /mnt/media onto the /mnt/mirror SSD.

Why: /mnt/media is a single 10 TB HDD. The 2026-11-27 disk audit found three
things on it that exist nowhere else: the audiobook library (with
Audiobookshelf's config and metadata), the Immich library, and the model
archive's jess-live folder (the copy of Helios's models plus the downloads
Jess needs to rebuild without the internet). Jupiter's spare 1 TB NVMe was
wiped and mounted at /mnt/mirror (nosuid,nodev,noexec, mode 700) for this. It
protects against the HDD dying or a folder being wiped, not against losing
Jupiter. The rest of the 5.2 TB model archive doesn't fit and is not copied.

What it copies: ITEMS below, relative to --source (default /mnt/media), each
to the same relative path under --dest (default /mnt/mirror).

Every night, per gated item:
1. The two trees are compared (lstat only, no rsync): every file or symlink
   in the mirror that the copy would delete or replace counts, whether it is
   gone from the source, a different type there (a folder, a hard link to
   something else, a symlink where symlinks are copied) or a file of another
   size or mtime; a source symlink, FIFO or device that rsync skips leaves the
   mirror copy alone and doesn't count. rsync's own --stats can't be used for
   this: it reports a file turned into a folder or symlink as "created".
2. The item is HELD when tonight's count, or tonight's plus everything moved
   in the window (= --keep-deleted-days, 30, so nothing is pruned from
   .deleted/ without having been counted; kept in
   <dest>/.media-mirror-state.json), is more than --max-changes (500), or more
   than --max-fraction (10 %) of the folder once 10 or more files are
   involved (Item.window_floor: 60 instead of 10 for the window total of the
   small, churny Immich library). A slow drain under that budget is only
   slowed, not stopped. A held item copies nothing at all, new files
   included, and the run fails (no success metric, so the stale alert fires).
   It stays held while the loss is there (a hold from the window total alone
   clears as old nights age out) unless the owner accepts the printed number:
       sudo media-mirror --check --list            # what would go, by name
       echo <N> | sudo tee /mnt/mirror/.accept/<slug>   # slug: the path, / as -
       sudo systemctl start media-mirror
   The accept file is used up when that item starts (whatever happens next),
   is ignored when older than 24 h, and allows N plus 10 % (at least 10).
3. Otherwise a dry run checks the new data fits (5 % of the disk, at least
   10 GB, stays free; a copy that wouldn't fit is HELD), the item is marked
   in flight in the state file, and rsync -a --delete copies it (-H only for
   the host-written model-archive items). Nothing leaves the mirror the night
   it leaves the source: every deleted or overwritten file moves into
   <dest>/.deleted/<UTC stamp>/<item>/. Those files are counted after the copy
   too, whatever rsync's exit code; more than the limit (a source that changed
   between the comparison and the copy) trips the item, which stays held until
   accepted. The .deleted/
   folders are removed after --keep-deleted-days (30), and only after a run
   where every item succeeded, so a failing or held mirror keeps all of them.
4. Audiobookshelf's database (SQLITE_FILES) is kept out of rsync and
   snapshotted with SQLite's online backup API, which is consistent even
   mid-write, then checked with PRAGMA quick_check. A changed snapshot moves
   the previous one into .deleted/ first. It is taken even when its folder is
   held or its copy failed.
model-archive/software is not gated and keeps no .deleted/ history: it is
public source, re-fetchable from GitHub, and git's own housekeeping churns it.

Safety (Immich and Audiobookshelf write their folders as root, so the source
is treated as hostile):
- A real copy refuses unless --source is read-only to it, which is what the
  unit's sandbox does (it also hides /home, /var/lib, sockets and secrets).
  `sudo systemctl start media-mirror` is the way to run one by hand.
  `--check` (compares, writes nothing) runs anywhere.
- Refuses unless --source and --dest are both mount points (an unmounted
  /mnt/media is an empty folder; an unmounted /mnt/mirror is the root disk),
  the mirror root is mode 700 and owned by the runner, every item is the
  right type with no symlink anywhere on its path, and the source doesn't
  look like a mirror (swapped arguments).
- The container-written items are copied with --no-links, so no symlink ever
  enters the mirror there. No device files, FIFOs or setuid/setgid bits are
  copied, and rsync stays on one filesystem (-x). Every mirror folder the
  script writes into is checked to be a real folder, component by component.
- Never prints file names (audiobook titles, photo names), only counts.
  `--check --list` prints names to the terminal and is refused under systemd.
- One run at a time: flock on <dest>/.media-mirror.lock. A second run exits 0.

Metrics (--metrics-dir, a node_exporter textfile directory), file
jess_media_mirror.prom, written atomically and only after a fully good run:
  jess_media_mirror_success_timestamp_seconds  unix time of the last good run
  jess_media_mirror_used_bytes                 bytes used on --dest
  jess_media_mirror_duration_seconds           how long the last good run took
  jess_media_mirror_last_files{change="new"|"moved"}  that run's new files and
                                                     files moved to .deleted/
Alert: JessMediaMirrorStale (no good run for 36 h, or no metric while
/mnt/mirror is mounted; quiet tier).

Exit codes: 0 = everything copied (or another run holds the lock; --check:
nothing would be held); 1 = an item failed or is held (the others still ran;
the metric isn't updated; --check: something would be held or failed);
2 = refused or setup error (nothing copied); 143 = stopped (SIGTERM).

Install (Jupiter, as labadmin; units in scripts/media-mirror/):
  sudo install -m 755 scripts/media_mirror.py /usr/local/sbin/media-mirror
  sudo install -m 644 scripts/media-mirror/media-mirror.service \
      scripts/media-mirror/media-mirror.timer /etc/systemd/system/
  sudo systemctl daemon-reload && sudo systemctl enable --now media-mirror.timer
  sudo systemctl start media-mirror.service      # first copy, about 200 GB
  journalctl -u media-mirror -n 20
  cat /home/labadmin/node_exporter/textfile_collector/jess_media_mirror.prom

Restore: docs/BACKUP.md → Local mirror of /mnt/media.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import filecmp
import fnmatch
import json
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

METRICS_FILE = "jess_media_mirror.prom"
DELETED_DIR = ".deleted"
ACCEPT_DIR = ".accept"
STATE_FILE = ".media-mirror-state.json"
LOCK_FILE = ".media-mirror.lock"
ERRORS_DIR = ".media-mirror-errors"
UNREADABLE_STATE = "unreadable-state"
# Free space a copy must leave on the mirror: 5 % of the disk, at least this much.
RESERVE_MIN_BYTES = 10 * 10**9
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
# Below this many files leaving, the fraction limit doesn't apply: Immich's library
# has ~20 files and rotates one DB dump a night.
FRACTION_FLOOR = 10
ACCEPT_MAX_AGE = 24 * 3600


@dataclass(frozen=True)
class Item:
    path: str  # relative to --source
    is_dir: bool = True
    gated: bool = True  # limit on files leaving the mirror; .deleted/ history
    links: bool = False  # copy symlinks: only for folders no container writes
    # Floor of the window total (tonight alone always uses FRACTION_FLOOR).
    window_floor: int = FRACTION_FLOOR
    # rsync excludes, in three shapes only (Item.excluded mirrors them):
    # "/name*" anchored at the item root, "dir/sub/" a folder anywhere, "*.ext" a name anywhere.
    excludes: tuple[str, ...] = ()

    @property
    def slug(self) -> str:
        return self.path.replace("/", "-")

    def excluded(self, rel: Path, is_dir: bool) -> bool:
        for pattern in self.excludes:
            if pattern.endswith("/"):
                parts = tuple(pattern.rstrip("/").split("/"))
                if is_dir and rel.parts[-len(parts) :] == parts:
                    return True
            elif pattern.startswith("/"):
                if fnmatch.fnmatchcase(rel.as_posix(), pattern[1:]):
                    return True
            elif fnmatch.fnmatchcase(rel.name, pattern):
                return True
        return False


ITEMS = (
    # Written by the Audiobookshelf container (root inside): no symlinks copied.
    Item("audiobooks"),
    Item("podcasts"),
    Item("ebooks"),
    Item("config", excludes=("/absdatabase.sqlite", "/absdatabase.sqlite-*")),  # DB: snapshotted
    Item("metadata"),
    # Written by the Immich server: photos + its own nightly DB dumps (library/backups).
    # ~20 files with daily churn (a dump rotates out, .immich markers get rewritten): a small
    # folder's normal month would cross the default window floor.
    Item("immich/library", window_floor=60),
    # Written by the host only (model-archive's own timers, as labadmin).
    Item("model-archive/jess-live", links=True, excludes=(".cache/huggingface/", "*.incomplete")),
    Item("model-archive/_tools", links=True),
    Item("model-archive/software", gated=False, links=True),
    Item("model-archive/README.md", is_dir=False, links=True),
)
SQLITE_FILES = ("config/absdatabase.sqlite",)
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
# Errors a read-only open of a cleanly closed WAL database gives in a read-only folder.
SQLITE_READONLY_ERRORS = frozenset(
    {"SQLITE_CANTOPEN", "SQLITE_READONLY", "SQLITE_READONLY_DIRECTORY", "SQLITE_READONLY_CANTINIT"}
)
# 24 = some source files vanished mid-copy (Immich rotating its dumps): not a failure.
RSYNC_OK = frozenset({0, 24})
RSYNC_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}

_NUM = r"([\d,]+)"
_CREATED_REG = re.compile(rf"^Number of created files: [\d,]+ \([^)]*\breg: {_NUM}", re.M)
_BYTES = re.compile(rf"^Total transferred file size: {_NUM}", re.M)


class MirrorError(RuntimeError):
    """A failure with a fixed message (never a file name), so it is safe to print."""


class UnsafePath(MirrorError):
    """A path the script would read or write is not what it must be (symlink, wrong type...)."""


# --------------------------------------------------------------------------
# Comparing the trees
# --------------------------------------------------------------------------


@dataclass
class Plan:
    existing: int = 0  # files + symlinks in the mirror's copy of the item
    leaving: int = 0  # of which the copy would delete or replace
    names: list[str] = field(default_factory=list)  # only with want_names (--check --list)


def _would_replace(dst: Path, src: Path, item: Item) -> bool:
    """Would the copy delete or replace this mirror file or symlink? Mirrors rsync's own rules."""
    d = os.lstat(dst)
    try:
        s = os.lstat(src)
    except (FileNotFoundError, NotADirectoryError):
        return True
    # rsync skips these in the source (--no-links / --no-devices --no-specials) and keeps the mirror copy.
    if stat.S_ISLNK(s.st_mode) and not item.links:
        return False
    if stat.S_ISCHR(s.st_mode) or stat.S_ISBLK(s.st_mode) or stat.S_ISFIFO(s.st_mode) or stat.S_ISSOCK(s.st_mode):
        return False
    if stat.S_ISLNK(d.st_mode):
        return not (stat.S_ISLNK(s.st_mode) and os.readlink(src) == os.readlink(dst))
    if stat.S_ISREG(d.st_mode):
        if not stat.S_ISREG(s.st_mode):
            return True
        return (s.st_size, s.st_mtime_ns) != (d.st_size, d.st_mtime_ns)  # rsync runs with --modify-window=-1
    return True


def _raise(exc: OSError) -> None:
    raise exc


def _entries_below(path: Path, item: Item, rel: Path) -> list[str]:
    """Files + symlinks under a mirror folder, as item-relative names, excludes skipped."""
    names = []
    for dirpath, dirnames, filenames in os.walk(path, onerror=_raise):
        rel_dir = rel / Path(dirpath).relative_to(path)
        for name in list(dirnames):
            if os.path.islink(os.path.join(dirpath, name)):
                dirnames.remove(name)
                filenames.append(name)
            elif item.excluded(rel_dir / name, is_dir=True):
                dirnames.remove(name)
        names += [(rel_dir / n).as_posix() for n in filenames if not item.excluded(rel_dir / n, is_dir=False)]
    return names


def plan_changes(source: Path, dest: Path, item: Item, want_names: bool = False) -> Plan:
    """Count what the copy would take out of the mirror, from the two trees themselves.

    Walks the mirror (lstat only, never following a symlink) and checks each source twin. A mirror
    folder whose source twin is gone or no longer a real folder loses everything below it, except a
    source symlink in a --no-links item: rsync skips it and keeps the mirror folder.
    """
    plan = Plan()

    def leave(names: list[str]) -> None:
        plan.leaving += len(names)
        if want_names:
            plan.names += [f"{item.path}/{n}" for n in names]

    root, src_root = dest / item.path, source / item.path
    if not item.is_dir:
        if os.path.lexists(root):
            plan.existing = 1
            if _would_replace(root, src_root, item):
                plan.leaving = 1
                if want_names:
                    plan.names.append(item.path)
        return plan
    if not os.path.lexists(root):
        return plan
    for dirpath, dirnames, filenames in os.walk(root, onerror=_raise):
        rel_dir = Path(dirpath).relative_to(root)
        entries = list(filenames)
        for name in list(dirnames):
            rel = rel_dir / name
            if os.path.islink(os.path.join(dirpath, name)):
                dirnames.remove(name)
                entries.append(name)
                continue
            if item.excluded(rel, is_dir=True):
                dirnames.remove(name)
                continue
            try:
                twin = os.lstat(src_root / rel).st_mode
            except (FileNotFoundError, NotADirectoryError):
                twin = None
            if twin is not None and stat.S_ISDIR(twin):
                continue  # walk into it
            dirnames.remove(name)
            below = _entries_below(root / rel, item, rel)
            plan.existing += len(below)
            if not (twin is not None and stat.S_ISLNK(twin) and not item.links):
                leave(below)
        for name in entries:
            rel = rel_dir / name
            if item.excluded(rel, is_dir=False):
                continue
            plan.existing += 1
            if _would_replace(root / rel, src_root / rel, item):
                leave([rel.as_posix()])
    return plan


def over_limit(leaving: int, existing: int, max_changes: int, max_fraction: float, floor: int = FRACTION_FLOOR) -> bool:
    if leaving > max_changes:
        return True
    return leaving >= floor and leaving > max_fraction * max(existing, 1)


def accepted_limit(accepted: int) -> int:
    return accepted + max(FRACTION_FLOOR, accepted // 10)


def count_entries(path: Path) -> int:
    """Files + symlinks under path (a symlink to a folder counts as one, never followed)."""
    if not os.path.lexists(path):
        return 0
    if not stat.S_ISDIR(os.lstat(path).st_mode):
        return 1
    total = 0
    for dirpath, dirnames, filenames in os.walk(path, onerror=_raise):
        links = [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]
        total += len(filenames) + len(links)
    return total


def remove_empty_dirs(path: Path) -> None:
    """Remove the empty folders under (and including) path, bottom-up. Never removes a file."""
    if not os.path.lexists(path) or not stat.S_ISDIR(os.lstat(path).st_mode):
        return
    for dirpath, _dirnames, _filenames in os.walk(path, topdown=False):
        with contextlib.suppress(OSError):  # not empty
            os.rmdir(dirpath)


# --------------------------------------------------------------------------
# State: the window, trips, copies in flight
# --------------------------------------------------------------------------


class State:
    """Per gated item: files moved in the window (= --keep-deleted-days, so nothing is pruned from
    .deleted/ without having been counted), a trip, and the stamp of a copy in flight."""

    def __init__(self, items: dict[str, dict] | None = None, window: float = 30 * 86400, unreadable: bool = False):
        self.items = items or {}
        self.window = window
        self.unreadable = unreadable

    @classmethod
    def load(cls, dest: Path, window: float) -> State:
        path = dest / STATE_FILE
        if not os.path.lexists(path):
            return cls(window=window)
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise UnsafePath("the state file is not a regular file")
        try:
            items = json.loads(path.read_text())["items"]
            for entry in items.values():
                entry["recent"] = [[float(ts), int(n)] for ts, n in entry.get("recent", [])]
                entry["tripped"] = bool(entry.get("tripped", False))
                entry["stamp"] = str(entry.get("stamp", ""))
                entry["tripped_count"] = int(entry.get("tripped_count", 0))
                entry["in_flight"] = str(entry.get("in_flight", ""))
        except (ValueError, KeyError, TypeError, AttributeError):
            return cls(window=window, unreadable=True)  # fail closed: mirror() trips every gated item
        return cls(items, window)

    def _entry(self, slug: str) -> dict:
        return self.items.setdefault(
            slug, {"recent": [], "tripped": False, "stamp": "", "tripped_count": 0, "in_flight": ""}
        )

    def recent(self, slug: str, now: float) -> int:
        return sum(n for ts, n in self._entry(slug)["recent"] if now - ts < self.window)

    def tripped(self, slug: str) -> tuple[str, int] | None:
        """(stamp of the tripped copy, files it moved), or None."""
        entry = self._entry(slug)
        return (entry["stamp"] or "an earlier night", entry["tripped_count"]) if entry["tripped"] else None

    def trip(self, slug: str, stamp: str, moved: int) -> None:
        entry = self._entry(slug)
        count = entry["tripped_count"] + moved if entry["tripped"] else moved
        entry.update(tripped=True, stamp=stamp, tripped_count=count)

    def start(self, slug: str, stamp: str) -> None:
        self._entry(slug)["in_flight"] = stamp

    def in_flight(self) -> list[tuple[str, str]]:
        return [(slug, e["in_flight"]) for slug, e in sorted(self.items.items()) if e.get("in_flight")]

    def record(self, slug: str, now: float, moved: int, *, accepted: bool, trip: bool, stamp: str) -> None:
        """After a copy (or a recovered interrupted one). An accepted, untripped copy starts the
        window afresh: what it moved was approved."""
        entry = self._entry(slug)
        entry["in_flight"] = ""
        if accepted and not trip:
            entry.update(recent=[], tripped=False, stamp="", tripped_count=0)
            return
        entry["recent"] = [e for e in entry["recent"] if now - e[0] < self.window]
        if moved:
            entry["recent"].append([now, moved])
        if trip:
            self.trip(slug, stamp, moved)

    def save(self, dest: Path) -> None:
        write_atomic(dest, STATE_FILE, json.dumps({"items": self.items}, sort_keys=True) + "\n", 0o600)


def _read_accept(path: Path, now: float) -> tuple[int | None, str]:
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or st.st_size > 64:
        return None, "accept file ignored (not a small regular file)"
    if now - st.st_mtime > ACCEPT_MAX_AGE:
        return None, "accept file ignored (older than 24 h)"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, errors="replace") as fh:
        text = fh.read().strip()
    if not re.fullmatch(r"[0-9]{1,9}", text):
        return None, "accept file ignored (it must hold the number of files the journal printed)"
    return int(text), ""


def take_accept(dest: Path, item: Item, now: float) -> tuple[int | None, str]:
    """Use up <dest>/.accept/<slug>. Returns (accepted count or None, a note to print or "")."""
    path = dest / ACCEPT_DIR / item.slug
    if not os.path.lexists(path):
        return None, ""
    try:
        return _read_accept(path, now)
    finally:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()  # used up, whatever happens next


def peek_accept(dest: Path, item: Item, now: float) -> tuple[int | None, str]:
    """take_accept without using the file up (--check)."""
    path = dest / ACCEPT_DIR / item.slug
    return _read_accept(path, now) if os.path.lexists(path) else (None, "")


# --------------------------------------------------------------------------
# Paths, checks, rsync
# --------------------------------------------------------------------------


def is_mount(path: Path) -> bool:
    """Module-level so tests can stand in for a real mount point."""
    return os.path.ismount(path)


def source_read_only(path: Path) -> bool:
    """True inside the unit's sandbox (ProtectSystem=strict). Module-level for tests."""
    return bool(os.statvfs(path).f_flag & os.ST_RDONLY)


def _check_real_dirs(root: Path, rel: Path) -> None:
    current = root
    for part in rel.parts:
        current = current / part
        if not stat.S_ISDIR(os.lstat(current).st_mode):
            raise UnsafePath("a source folder on an item's path is not a real folder")


def check_item_source(source: Path, item: Item) -> None:
    """Every folder on the item's path is a real folder, and the item is the declared type."""
    rel = Path(item.path)
    _check_real_dirs(source, rel.parent)
    mode = os.lstat(source / rel).st_mode
    if not (stat.S_ISDIR(mode) if item.is_dir else stat.S_ISREG(mode)):
        raise UnsafePath(f"{item.path} is not a {'folder' if item.is_dir else 'regular file'} in the source")


def check_ready(source: Path, dest: Path) -> list[str]:
    """What stops a run, as printable reasons (fixed names only). Empty = go."""
    problems = [f"{p} is not a mount point" for p in (source, dest) if not is_mount(p)]
    if problems:
        return problems
    root = os.lstat(dest)
    if root.st_uid != os.geteuid() or root.st_mode & 0o077:
        problems.append(f"{dest} must be mode 700 and owned by the user running the copy")
    for marker in (LOCK_FILE, DELETED_DIR, ACCEPT_DIR, STATE_FILE):
        if os.path.lexists(source / marker):
            problems.append(f"{source} holds {marker}: it looks like the mirror (--source and --dest swapped?)")
    for item in ITEMS:
        if not os.path.lexists(source / item.path):
            problems.append(f"{item.path} is missing from {source}")
            continue
        try:
            check_item_source(source, item)
        except UnsafePath as exc:
            problems.append(str(exc))
    return problems


def utc_stamp(now: float) -> str:
    return dt.datetime.fromtimestamp(now, dt.UTC).strftime(STAMP_FORMAT)


def rsync_cmd(src: Path, dst: Path, item: Item, *, backup_dir: Path | None, dry_run: bool = False) -> list[str]:
    # No --max-delete: it counts folders too, so it can't match the file counts the gate uses. Every
    # deleted or replaced file goes to backup_dir instead, and the files counted there trip the item.
    # -H only where the source is trusted: with -H a container can hard-link a decoy to every file and
    # make rsync replace each mirror copy with the decoy's content while every name looks unchanged.
    cmd = ["rsync", "-a"] + (["-H"] if item.links else [])
    cmd += ["-x", "--numeric-ids", "--no-devices", "--no-specials", "--chmod=ug-s", "--stats"]
    if dry_run:
        cmd.append("-n")
    cmd.append("--modify-window=-1")  # compare mtimes to the nanosecond, as plan_changes does
    if not item.links:
        cmd.append("--no-links")
    if item.is_dir:
        cmd.append("--delete")
    if backup_dir is not None:
        cmd += ["--backup", f"--backup-dir={backup_dir}"]
    # Excluded files are also protected from --delete on the receiving side, so
    # the SQLite snapshot survives the rsync of its folder.
    cmd += [f"--exclude={pattern}" for pattern in item.excludes]
    cmd += [f"{src}/", f"{dst}/"] if item.is_dir else [str(src), str(dst)]
    return cmd


def run_rsync(cmd: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", check=False, env=RSYNC_ENV)
    return proc.returncode, proc.stdout, proc.stderr


def _stat_number(pattern: re.Pattern[str], output: str) -> int:
    match = pattern.search(output)
    return int(match.group(1).replace(",", "")) if match else 0


def _copy_owner_mode(src: Path, dst: Path) -> None:
    st = os.lstat(src)
    os.chmod(dst, stat.S_IMODE(st.st_mode) & 0o777)
    with contextlib.suppress(PermissionError):  # only root can chown; tests run unprivileged
        os.chown(dst, st.st_uid, st.st_gid)


def secure_dir(root: Path, rel: Path, like: Path | None = None) -> Path:
    """root/rel as a chain of real folders, creating missing ones. Raises UnsafePath on a non-folder.

    `like`: the source root; a created folder copies its source twin's owner and mode.
    """
    current = root
    for part in rel.parts:
        current = current / part
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            os.mkdir(current, 0o755)
            twin = like / current.relative_to(root) if like is not None else None
            if twin is not None and stat.S_ISDIR(os.lstat(twin).st_mode):
                _copy_owner_mode(twin, current)
            st = os.lstat(current)
        if not stat.S_ISDIR(st.st_mode):
            raise UnsafePath("a mirror folder is not a real folder")
    return current


# --------------------------------------------------------------------------
# SQLite snapshot
# --------------------------------------------------------------------------


def _backup_into(uri: str, target: Path) -> None:
    src_db = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        # The source is hostile-writable: no triggers/views from its schema run, no shadow-table writes.
        if hasattr(src_db, "setconfig"):
            src_db.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
        src_db.execute("PRAGMA trusted_schema=OFF")
        dst_db = sqlite3.connect(target)
        try:
            if hasattr(dst_db, "setconfig"):
                dst_db.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
            dst_db.execute("PRAGMA trusted_schema=OFF")  # quick_check reads a copy of the hostile schema
            src_db.backup(dst_db)
            result = dst_db.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            dst_db.close()
    finally:
        src_db.close()
    if result != "ok":
        raise MirrorError("a snapshot failed PRAGMA quick_check")


def _has_sidecar(src: Path) -> bool:
    return any(os.path.lexists(f"{src}{s}") for s in SQLITE_SIDECARS)


def _fingerprint(st: os.stat_result) -> tuple[int, ...]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def sqlite_backup(src: Path, target: Path) -> None:
    """Online backup of src into target, opening src read-only.

    mode=ro on purpose: a hot journal left by a crash makes the open fail (the
    run fails, the stale alert fires) instead of this job rolling it back. In
    the unit /mnt/media is read-only, and a cleanly closed WAL database can't be
    opened mode=ro without creating its -shm: then, and only when no sidecar
    exists before or after, retry with immutable=1 and fail if the file changed.
    SQLite opens by path, so the file's identity is compared before and after.
    """
    uri = "file:" + urllib.parse.quote(os.fsencode(src)) + "?mode=ro"
    before = os.lstat(src)
    try:
        _backup_into(uri, target)
    except sqlite3.OperationalError as exc:
        if getattr(exc, "sqlite_errorname", "") not in SQLITE_READONLY_ERRORS or _has_sidecar(src):
            raise
        _backup_into(uri + "&immutable=1", target)
        if _fingerprint(before) != _fingerprint(os.lstat(src)) or _has_sidecar(src):
            raise MirrorError("a database changed during its immutable snapshot") from None
        return
    after = os.lstat(src)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or not stat.S_ISREG(after.st_mode):
        raise UnsafePath("a database was swapped during its snapshot")


def snapshot_sqlite(source: Path, dest: Path, rel: str, stamp: str) -> str:
    """Snapshot source/rel to dest/rel. Returns "unchanged", "new" or "updated".

    A changed snapshot moves the previous one into .deleted/<stamp>/ first.
    """
    rel_path = Path(rel)
    _check_real_dirs(source, rel_path.parent)
    src = source / rel_path
    if not os.path.lexists(src):
        raise MirrorError("the database is missing from the source")
    st = os.lstat(src)
    if not stat.S_ISREG(st.st_mode):
        raise UnsafePath("a database is not a regular file")
    parent = secure_dir(dest, rel_path.parent, like=source)
    dst = parent / rel_path.name
    fd, tmp_name = tempfile.mkstemp(prefix=f".{rel_path.name}.", dir=parent)
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, stat.S_IMODE(st.st_mode) & 0o777)
        with contextlib.suppress(PermissionError):  # only root can chown; tests run unprivileged
            os.fchown(fd, st.st_uid, st.st_gid)
        os.close(fd)
        fd = -1
        sqlite_backup(src, tmp)
        try:
            old = os.lstat(dst)
        except FileNotFoundError:
            os.replace(tmp, dst)
            return "new"
        if not stat.S_ISREG(old.st_mode):
            raise UnsafePath("a mirror database is not a regular file")
        if filecmp.cmp(tmp, dst, shallow=False):
            tmp.unlink()
            return "unchanged"
        keep = secure_dir(dest, Path(DELETED_DIR) / stamp / rel_path.parent)
        os.rename(dst, keep / rel_path.name)
        os.replace(tmp, dst)
        return "updated"
    except BaseException:
        if fd >= 0:
            os.close(fd)
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


# --------------------------------------------------------------------------
# Pruning, metrics
# --------------------------------------------------------------------------


def prune_deleted(dest: Path, keep_days: int, now: float) -> int:
    """Remove .deleted/<stamp> folders older than keep_days. Names that aren't stamps are never touched."""
    root = dest / DELETED_DIR
    if not root.is_dir() or root.is_symlink():
        return 0
    cutoff = now - keep_days * 86400
    removed = 0
    for entry in sorted(root.iterdir()):
        try:
            taken = dt.datetime.strptime(entry.name, STAMP_FORMAT).replace(tzinfo=dt.UTC).timestamp()
        except ValueError:
            continue
        if taken < cutoff and entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
            removed += 1
    return removed


def render(now: float, used_bytes: int, duration: float, totals: dict[str, int]) -> str:
    lines = [
        "# HELP jess_media_mirror_success_timestamp_seconds Unix time of the last fully successful media mirror run.",
        "# TYPE jess_media_mirror_success_timestamp_seconds gauge",
        f"jess_media_mirror_success_timestamp_seconds {int(now)}",
        "# HELP jess_media_mirror_used_bytes Bytes used on the mirror disk after the last good run.",
        "# TYPE jess_media_mirror_used_bytes gauge",
        f"jess_media_mirror_used_bytes {used_bytes}",
        "# HELP jess_media_mirror_duration_seconds How long the last good media mirror run took.",
        "# TYPE jess_media_mirror_duration_seconds gauge",
        f"jess_media_mirror_duration_seconds {duration:.0f}",
        "# HELP jess_media_mirror_last_files Files the last good run copied in (new) or moved to .deleted/ (moved).",
        "# TYPE jess_media_mirror_last_files gauge",
    ]
    lines += [f'jess_media_mirror_last_files{{change="{k}"}} {totals[k]}' for k in ("new", "moved")]
    return "\n".join(lines) + "\n"


def write_atomic(directory: Path, name: str, text: str, mode: int = 0o644) -> None:
    """Readers must never see a half-written file: temp file in the same dir, then rename."""
    fd, tmp = tempfile.mkstemp(prefix=f".{name}.", dir=directory)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, directory / name)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1000:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} TB"


def _describe(exc: BaseException) -> str:
    """Exception class + a fixed code. Messages can name files, so only MirrorError's are printed."""
    if isinstance(exc, MirrorError):
        return f"{type(exc).__name__}: {exc}"
    code = getattr(exc, "sqlite_errorname", None) or getattr(exc, "errno", None) or ""
    return f"{type(exc).__name__} {code}".strip()


def _pct(leaving: int, existing: int) -> str:
    return f"{100 * leaving / max(existing, 1):.0f}%"


# --------------------------------------------------------------------------
# The runs
# --------------------------------------------------------------------------


def verdict(
    item: Item, plan: Plan, state: State, accepted: int | None, args: argparse.Namespace, now: float
) -> tuple[bool, str, int, int]:
    """(held, reason, files moved in the window, the number an accept must hold) for a gated item."""
    recent = state.recent(item.slug, now)
    tripped = state.tripped(item.slug)
    needed = max(plan.leaving, tripped[1] if tripped else 0)
    if accepted is not None:
        if tripped and accepted < tripped[1]:
            return (
                True,
                f"the accept is less than the {tripped[1]} files the tripped copy of {tripped[0]} moved",
                0,
                needed,
            )
        limit = accepted_limit(accepted)
        return plan.leaving > limit, f"more than the {accepted} accepted (+ margin {limit - accepted})", 0, needed
    if tripped:
        reason = f"tripped: the copy of {tripped[0]} moved {tripped[1]} files, more than the limit (see .deleted/{tripped[0]}/)"
        return True, reason, recent, needed
    held = over_limit(plan.leaving, plan.existing, args.max_changes, args.max_fraction) or over_limit(
        recent + plan.leaving, plan.existing + recent, args.max_changes, args.max_fraction, item.window_floor
    )
    return held, "over the limit", recent, needed


def check_items(args: argparse.Namespace) -> int:
    """--check: compare and report per item, write nothing."""
    now = time.time()
    state = State.load(args.dest, args.keep_deleted_days * 86400)
    bad = 0
    if state.unreadable:
        bad += 1
        print(
            f"{STATE_FILE} is unreadable: the next real run sets it aside and holds every gated folder until accepted"
        )
    for item in ITEMS:
        try:
            check_item_source(args.source, item)
            plan = plan_changes(args.source, args.dest, item, want_names=args.list)
            accepted, note = peek_accept(args.dest, item, now) if item.gated else (None, "")
        except (OSError, MirrorError) as exc:
            bad += 1
            print(f"{item.path}: check FAILED ({_describe(exc)})")
            continue
        pct = _pct(plan.leaving, plan.existing)
        line = f"{item.path}: {plan.leaving} of {plan.existing} mirror files would leave ({pct})"
        if not item.gated:
            print(f"{line} - not gated")
        else:
            held, reason, recent, needed = verdict(item, plan, state, accepted, args, now)
            if recent:
                line += f", {recent} more in the last {args.keep_deleted_days} days"
            if note:
                line += f" - {note}"
            if held:
                bad += 1
                accept = args.dest / ACCEPT_DIR / item.slug
                line += f" - would be HELD ({reason}); accept with: echo {needed} | sudo tee {accept}"
            elif accepted is not None:
                line += f" - accept for {accepted} waiting: would mirror"
            else:
                line += " - would mirror"
            print(line)
        for name in plan.names:
            print(f"    {name!r}")
    return 1 if bad else 0


def mirror_item(args: argparse.Namespace, item: Item, stamp: str, state: State, totals: dict[str, int]) -> str:
    """Copy one item. Returns "ok", "held" or "failed". The SQLite snapshot is the caller's."""
    now = time.time()
    check_item_source(args.source, item)
    src, dst = args.source / item.path, args.dest / item.path
    rel_dir = Path(item.path) if item.is_dir else Path(item.path).parent
    secure_dir(args.dest, rel_dir, like=args.source)

    backup_dir = accepted = None
    recent = 0
    if item.gated:
        accepted, note = take_accept(args.dest, item, now)
        if note:
            print(f"[media-mirror] {item.path}: {note}")
        plan = plan_changes(args.source, args.dest, item)
        held, reason, recent, needed = verdict(item, plan, state, accepted, args, now)
        if held:
            window = f", plus {recent} in the last {args.keep_deleted_days} days," if recent else ""
            print(
                f"[media-mirror] {item.path}: HELD - {plan.leaving} of its {plan.existing} files would leave the "
                f"mirror tonight{window} which is {reason}. Nothing was copied, new files included. See what "
                f"would go: `sudo media-mirror --check --list`. If it's expected: `echo {needed} | sudo tee "
                f"{args.dest}/{ACCEPT_DIR}/{item.slug}` then `sudo systemctl start media-mirror`."
            )
            return "held"
        backup_dir = secure_dir(args.dest, Path(DELETED_DIR) / stamp / rel_dir)

    # New data is never gated by count, so check it fits: a container could fill the mirror (a sparse
    # 800 GB file costs the source nothing), which would stop every item and every state save.
    rc, out, _err = run_rsync(rsync_cmd(src, dst, item, backup_dir=None, dry_run=True))
    if rc not in RSYNC_OK:
        _keep_errors(args, item, stamp, _err)
        print(
            f"[media-mirror] {item.path}: dry run FAILED rc={rc}. rsync's lines: {args.dest}/{ERRORS_DIR}/{item.slug}.txt"
        )
        return "failed"
    usage = shutil.disk_usage(args.dest)
    room = usage.free - max(RESERVE_MIN_BYTES, usage.total // 20)
    need = _stat_number(_BYTES, out)
    if need > room:
        print(
            f"[media-mirror] {item.path}: HELD - {_human(need)} of new or changed files would not fit "
            f"({_human(max(room, 0))} free above the reserve). Nothing was copied. Look at what grew: "
            f"`sudo du -xh --max-depth=2 {src} | sort -h | tail`; a sudden huge file in a container folder is worth "
            f"treating as an attack."
        )
        return "held"
    if item.gated:
        state.start(item.slug, stamp)  # written before the copy: an interrupted run is counted on the next one
        state.save(args.dest)

    kept = (backup_dir if item.is_dir else backup_dir / Path(item.path).name) if backup_dir else None
    kept_before = count_entries(kept) if kept else 0  # a second run in the same second shares the stamp
    rc, out, err = run_rsync(rsync_cmd(src, dst, item, backup_dir=backup_dir))
    moved = count_entries(kept) - kept_before if kept else 0  # counted whatever rsync's exit code
    status = "ok"
    if rc not in RSYNC_OK:
        _keep_errors(args, item, stamp, err)
        print(
            f"[media-mirror] {item.path}: rsync FAILED rc={rc}, {moved} moved to {DELETED_DIR}/. rsync's lines "
            f"(they name files): `sudo less {args.dest}/{ERRORS_DIR}/{item.slug}.txt`."
        )
        status = "failed"
    else:
        new = _stat_number(_CREATED_REG, out)
        totals["new"] += new
        totals["moved"] += moved
        note = " (some files vanished mid-copy)" if rc == 24 else ""
        print(
            f"[media-mirror] {item.path}: {new} new, {moved} moved to {DELETED_DIR}/ "
            f"({_human(_stat_number(_BYTES, out))} copied){note}"
        )
    if item.gated:
        if accepted is not None:
            tripped = moved > accepted_limit(accepted)
        else:
            tripped = over_limit(moved, plan.existing, args.max_changes, args.max_fraction) or over_limit(
                recent + moved, plan.existing + recent, args.max_changes, args.max_fraction, item.window_floor
            )
        state.record(item.slug, now, moved, accepted=accepted is not None and status == "ok", trip=tripped, stamp=stamp)
        state.save(args.dest)
        if tripped:
            print(
                f"[media-mirror] {item.path}: TRIPPED - {moved} files left the mirror during the copy, more than "
                f"the comparison said and over the limit. They are in {DELETED_DIR}/{stamp}/: look there before "
                f"accepting. The item stays HELD until `echo {moved} | sudo tee {args.dest}/{ACCEPT_DIR}/{item.slug}`."
            )
            return "held"
        if accepted is not None and status == "ok":
            print(f"[media-mirror] {item.path}: accepted once; the limit applies again from the next run")
    return status


def _keep_errors(args: argparse.Namespace, item: Item, stamp: str, err: str) -> None:
    """rsync's stderr names files: keep it root-only on the mirror, never in the journal."""
    try:
        folder = secure_dir(args.dest, Path(ERRORS_DIR))
        os.chmod(folder, 0o700)
        write_atomic(folder, f"{item.slug}.txt", f"{stamp}\n{err}", 0o600)
    except OSError:
        pass  # a full disk: the failure is reported anyway


def recover_in_flight(args: argparse.Namespace, state: State, now: float) -> None:
    """Count what an interrupted copy (SIGTERM, crash, failed state save) moved, and trip if too much."""
    by_slug = {i.slug: i for i in ITEMS if i.gated}
    for slug, stamp in state.in_flight():
        item = by_slug.get(slug)
        if item is None:
            state.record(slug, now, 0, accepted=False, trip=False, stamp=stamp)
            continue
        rel = Path(DELETED_DIR) / stamp / item.path  # a file item's backup sits at the same relative path
        moved = count_entries(args.dest / rel)
        existing = count_entries(args.dest / item.path) + moved
        recent = state.recent(slug, now)
        trip = over_limit(moved, existing, args.max_changes, args.max_fraction) or over_limit(
            recent + moved, existing + recent, args.max_changes, args.max_fraction, item.window_floor
        )
        state.record(slug, now, moved, accepted=False, trip=trip, stamp=stamp)
        print(
            f"[media-mirror] {item.path}: the interrupted copy of {stamp} moved {moved} files{' - TRIPPED' if trip else ''}"
        )
    state.save(args.dest)


def snapshot_item(args: argparse.Namespace, item: Item, stamp: str) -> bool:
    """The SQLite snapshots under this item, taken whether its copy succeeded, failed or was held."""
    ok = True
    for rel in SQLITE_FILES:
        if not rel.startswith(item.path + "/"):
            continue
        try:
            result = snapshot_sqlite(args.source, args.dest, rel, stamp)
            print(f"[media-mirror] {item.path}: database snapshot {result}")
        except Exception as exc:  # reported per database, never stops the run
            ok = False
            print(f"[media-mirror] {item.path}: database snapshot FAILED ({_describe(exc)})")
    return ok


def mirror(args: argparse.Namespace) -> int:
    started = time.time()
    stamp = utc_stamp(started)
    state = State.load(args.dest, args.keep_deleted_days * 86400)
    if state.unreadable:
        # Fail closed: moving the file aside must not clear a trip, so every gated item starts tripped.
        os.rename(args.dest / STATE_FILE, args.dest / f"{STATE_FILE}.{UNREADABLE_STATE}-{stamp}")
        for item in ITEMS:
            if item.gated:
                state.trip(item.slug, UNREADABLE_STATE, 0)
        state.unreadable = False
        state.save(args.dest)
        print(
            f"[media-mirror] {STATE_FILE} was unreadable: set aside as {STATE_FILE}.{UNREADABLE_STATE}-{stamp}; every "
            f"gated folder is HELD until accepted (`sudo media-mirror --check` prints each accept line)"
        )
    recover_in_flight(args, state, started)
    stamp_dir = secure_dir(args.dest, Path(DELETED_DIR) / stamp)  # refuses a planted symlink
    secure_dir(args.dest, Path(ACCEPT_DIR))
    os.chmod(args.dest / ACCEPT_DIR, 0o700)
    totals = {"new": 0, "moved": 0}
    failures = 0
    for item in ITEMS:
        try:
            if mirror_item(args, item, stamp, state, totals) != "ok":
                failures += 1
        except Exception as exc:  # one bad item must not stop the others
            failures += 1
            print(f"[media-mirror] {item.path}: FAILED ({_describe(exc)})")
        if not snapshot_item(args, item, stamp):
            failures += 1
    remove_empty_dirs(stamp_dir)
    with contextlib.suppress(OSError):  # only when empty
        (args.dest / DELETED_DIR).rmdir()
    if failures:
        print(
            f"[media-mirror] {failures} item(s) failed or held: metric not updated, nothing pruned from "
            f"{DELETED_DIR}/, JessMediaMirrorStale fires after 36 h"
        )
        return 1
    pruned = prune_deleted(args.dest, args.keep_deleted_days, started)
    used = shutil.disk_usage(args.dest).used
    duration = time.time() - started
    if args.metrics_dir:
        write_atomic(args.metrics_dir, METRICS_FILE, render(time.time(), used, duration, totals))
    print(
        f"[media-mirror] done in {duration:.0f} s, {_human(used)} used on {args.dest}, "
        f"pruned {pruned} old {DELETED_DIR}/ folders"
    )
    return 0


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def _fraction(text: str) -> float:
    value = float(text)
    if not 0 < value <= 1:
        raise argparse.ArgumentTypeError("must be above 0 and at most 1")
    return value


def _on_sigterm(_signum: int, _frame: object) -> None:
    raise SystemExit(143)  # run the cleanup handlers (temp snapshot files) on a unit timeout or stop


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", type=Path, default=Path("/mnt/media"))
    ap.add_argument("--dest", type=Path, default=Path("/mnt/mirror"))
    ap.add_argument("--metrics-dir", type=Path, help="node_exporter textfile collector directory")
    ap.add_argument("--max-changes", type=_positive_int, default=500, help="files leaving per item, tonight or in the window")
    ap.add_argument("--max-fraction", type=_fraction, default=0.10, help="of an item's files, once 10+ leave")
    ap.add_argument("--keep-deleted-days", type=_positive_int, default=30)
    ap.add_argument("--check", action="store_true", help="compare and say what each item would do; write nothing")
    ap.add_argument("--list", action="store_true", help="with --check: name each file that would leave (terminal only)")
    args = ap.parse_args(argv)
    os.umask(0o022)
    signal.signal(signal.SIGTERM, _on_sigterm)

    if args.list and not args.check:
        ap.error("--list only works with --check")
    if args.list and os.environ.get("INVOCATION_ID"):
        print("[media-mirror] refusing --list under systemd: it would put file names in the journal")
        return 2
    if shutil.which("rsync") is None:
        print("[media-mirror] rsync is not installed", file=sys.stderr)
        return 2
    try:
        problems = check_ready(args.source, args.dest)
        if not problems and not args.check and not source_read_only(args.source):
            problems.append(
                f"{args.source} is writable here, so this isn't the sandboxed unit: run "
                "`sudo systemctl start media-mirror`, or `--check` for a dry run"
            )
    except OSError as exc:
        print(f"[media-mirror] can't check the disks: {_describe(exc)}", file=sys.stderr)
        return 2
    if problems:
        for problem in problems:
            print(f"[media-mirror] refusing: {problem}", file=sys.stderr)
        return 2
    if args.check:
        try:
            return check_items(args)
        except (OSError, MirrorError) as exc:
            print(f"[media-mirror] check FAILED ({_describe(exc)})")
            return 1
    try:
        lock_fd = os.open(args.dest / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        print(f"[media-mirror] can't open the lock file: {_describe(exc)}", file=sys.stderr)
        return 2
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("[media-mirror] another run holds the lock: skipped")
            return 0
        try:
            return mirror(args)
        except (OSError, MirrorError) as exc:
            print(f"[media-mirror] FAILED ({_describe(exc)})")
            return 1
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
