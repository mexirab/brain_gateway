"""
Tests for scripts/media_mirror.py — the nightly second copy of /mnt/media's
irreplaceable folders (audiobooks, Immich, the archive's jess-live) onto
Jupiter's /mnt/mirror NVMe.

No root and no real mounts: each test builds a fake source and mirror under
tmp_path and stands in for the mount-point and read-only-source checks (a
real copy refuses unless the source is read-only, as in the unit's sandbox).
rsync is faked with a stub for the exit-code paths. The tests that need the
real thing (holds across nights, the attacks the reviews reproduced, the
.deleted/ layout) skip where rsync isn't installed: the throwaway orchestrator
image has none, so run this file on the host too:
`python3 -m pytest --noconftest orchestrator/tests/test_media_mirror.py`.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.tests._helpers import REPO_ROOT, load_script

mm = load_script(REPO_ROOT / "scripts" / "media_mirror.py", "media_mirror_under_test")

PROM = "jess_media_mirror.prom"
SECRET_NAME = "Private Title Nobody Should Log.m4b"
DB = "config/absdatabase.sqlite"
needs_rsync = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
not_root = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores folder permissions")

STATS = """
Number of files: 1,234 (reg: 1,200, dir: 34)
Number of created files: 3 (reg: 2, dir: 1)
Number of deleted files: 4 (reg: 4)
Number of regular files transferred: 7
Total transferred file size: 1,234,567 bytes
"""


def make_db(path: Path, rows: int = 3, wal: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS t (n INTEGER)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    con.commit()
    con.close()


def rows(path: Path) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT count(*) FROM t").fetchone()[0]
    finally:
        con.close()


@pytest.fixture
def host(tmp_path, monkeypatch):
    src, dst, metrics = tmp_path / "media", tmp_path / "mirror", tmp_path / "textfile"
    for d in (src, dst, metrics):
        d.mkdir()
    os.chmod(dst, 0o700)
    for item in mm.ITEMS:
        if item.is_dir:
            (src / item.path).mkdir(parents=True, exist_ok=True)
        else:
            (src / item.path).parent.mkdir(parents=True, exist_ok=True)
            (src / item.path).write_text("archive readme\n")
    (src / "audiobooks" / "Author").mkdir()
    (src / "audiobooks" / "Author" / SECRET_NAME).write_bytes(b"x" * 100)
    make_db(src / DB)  # always there on the real disk
    (src / "immich" / "postgres").mkdir()
    (src / "immich" / "postgres" / "PG_VERSION").write_text("14\n")
    mounted = {src, dst}
    monkeypatch.setattr(mm, "is_mount", lambda p: Path(p) in mounted)
    monkeypatch.setattr(mm, "source_read_only", lambda _p: True)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(mm, "RESERVE_MIN_BYTES", 0)  # tmp_path may be a small tmpfs (the test container's is 8 GB)
    return SimpleNamespace(src=src, dst=dst, metrics=metrics, mounted=mounted, tmp=tmp_path)


def run(host, *extra: str) -> int:
    return mm.main(["--source", str(host.src), "--dest", str(host.dst), "--metrics-dir", str(host.metrics), *extra])


def fake_rsync(monkeypatch, rc: int = 0, stdout: str = STATS, stderr: str = ""):
    calls = []

    def _run(cmd, **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(mm.subprocess, "run", _run)
    monkeypatch.setattr(mm.shutil, "which", lambda _name: "/usr/bin/rsync")  # the test image has no rsync
    return calls


def books(host, n: int) -> list[Path]:
    paths = [host.src / "audiobooks" / f"book{i:03}.mp3" for i in range(n)]
    for p in paths:
        p.write_text(f"audio {p.name}")
    return paths


def mirror_books(host) -> list[Path]:
    return sorted((host.dst / "audiobooks").glob("book*.mp3"))


def write_accept(host, slug: str, text: str) -> Path:
    path = host.dst / mm.ACCEPT_DIR / slug
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    return path


def item(path: str):
    return next(i for i in mm.ITEMS if i.path == path)


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------


@pytest.mark.parametrize("which", ["src", "dst"])
def test_refuses_when_a_disk_is_not_mounted(host, monkeypatch, capsys, which):
    calls = fake_rsync(monkeypatch)
    host.mounted.discard(getattr(host, which))
    assert run(host) == 2
    assert "is not a mount point" in capsys.readouterr().err
    assert calls == []
    assert not (host.dst / mm.LOCK_FILE).exists()
    assert not (host.metrics / PROM).exists()


def test_refuses_when_an_item_is_missing(host, monkeypatch, capsys):
    calls = fake_rsync(monkeypatch)
    shutil.rmtree(host.src / "immich" / "library")
    assert run(host) == 2
    assert "immich/library is missing" in capsys.readouterr().err
    assert calls == []


def test_refuses_a_symlinked_item(host, monkeypatch, capsys):
    fake_rsync(monkeypatch)
    elsewhere = host.tmp / "elsewhere"
    elsewhere.mkdir()
    shutil.rmtree(host.src / "audiobooks")
    (host.src / "audiobooks").symlink_to(elsewhere)
    assert run(host) == 2
    assert "audiobooks is not a folder" in capsys.readouterr().err


def test_refuses_a_symlink_on_an_items_path(host, monkeypatch, capsys):
    fake_rsync(monkeypatch)
    real = host.tmp / "real-immich"
    shutil.move(host.src / "immich", real)
    (host.src / "immich").symlink_to(real)
    assert run(host) == 2
    assert "not a real folder" in capsys.readouterr().err


def test_refuses_a_file_item_of_the_wrong_type(host, monkeypatch, capsys):
    fake_rsync(monkeypatch)
    (host.src / "model-archive" / "README.md").unlink()
    (host.src / "model-archive" / "README.md").mkdir()
    assert run(host) == 2
    assert "README.md is not a regular file" in capsys.readouterr().err


@pytest.mark.parametrize("marker", [".deleted", ".accept", ".media-mirror.lock", ".media-mirror-state.json"])
def test_refuses_swapped_source_and_dest(host, monkeypatch, capsys, marker):
    calls = fake_rsync(monkeypatch)
    (host.src / marker).mkdir()
    assert run(host) == 2
    assert "swapped" in capsys.readouterr().err
    assert calls == []


def test_refuses_a_mirror_root_others_can_read(host, monkeypatch, capsys):
    calls = fake_rsync(monkeypatch)
    os.chmod(host.dst, 0o755)
    assert run(host) == 2
    assert "mode 700" in capsys.readouterr().err
    assert calls == []


def test_refuses_a_real_copy_with_a_writable_source(host, monkeypatch, capsys):
    calls = fake_rsync(monkeypatch)
    monkeypatch.setattr(mm, "source_read_only", lambda _p: False)
    assert run(host) == 2
    assert "systemctl start media-mirror" in capsys.readouterr().err
    assert calls == []
    assert list(host.dst.iterdir()) == []


def test_check_runs_with_a_writable_source_and_writes_nothing(host, monkeypatch, capsys):
    calls = fake_rsync(monkeypatch)
    monkeypatch.setattr(mm, "source_read_only", lambda _p: False)
    assert run(host, "--check") == 0
    assert calls == []
    assert list(host.dst.iterdir()) == []
    assert "audiobooks: 0 of 0 mirror files would leave" in capsys.readouterr().out


def test_refuses_list_under_systemd(host, monkeypatch, capsys):
    monkeypatch.setenv("INVOCATION_ID", "x")
    assert run(host, "--check", "--list") == 2
    assert "journal" in capsys.readouterr().out


def test_list_needs_check(host):
    with pytest.raises(SystemExit):
        run(host, "--list")


def test_refuses_without_rsync(host, monkeypatch, capsys):
    monkeypatch.setattr(mm.shutil, "which", lambda _name: None)
    assert run(host) == 2
    assert "rsync is not installed" in capsys.readouterr().err


def test_second_run_skips_while_the_lock_is_held(host, monkeypatch, capsys):
    calls = fake_rsync(monkeypatch)
    fd = os.open(host.dst / mm.LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert run(host) == 0
    finally:
        os.close(fd)
    assert "skipped" in capsys.readouterr().out
    assert calls == []


@pytest.mark.parametrize(
    ("flag", "value"),
    [("--max-changes", "0"), ("--keep-deleted-days", "0"), ("--max-fraction", "0"), ("--max-fraction", "1.5")],
)
def test_rejects_bad_limits(host, flag, value):
    with pytest.raises(SystemExit):
        run(host, flag, value)


# --------------------------------------------------------------------------
# rsync commands and excludes
# --------------------------------------------------------------------------


def test_container_folders_are_copied_without_symlinks():
    for path in ("audiobooks", "podcasts", "ebooks", "config", "metadata", "immich/library"):
        cmd = mm.rsync_cmd(Path("/m/x"), Path("/r/x"), item(path), backup_dir=Path("/r/d"))
        assert "--no-links" in cmd, path
    for path in ("model-archive/jess-live", "model-archive/_tools", "model-archive/software"):
        cmd = mm.rsync_cmd(Path("/m/x"), Path("/r/x"), item(path), backup_dir=None)
        assert "--no-links" not in cmd, path


def test_folder_cmd():
    cmd = mm.rsync_cmd(Path("/m/a"), Path("/r/a"), item("audiobooks"), backup_dir=Path("/r/d/a"))
    assert cmd[:3] == ["rsync", "-a", "-x"]  # no -H for a container folder
    for flag in ("--numeric-ids", "--no-devices", "--no-specials", "--chmod=ug-s", "--delete", "--backup"):
        assert flag in cmd
    assert "--backup-dir=/r/d/a" in cmd and "--modify-window=-1" in cmd
    assert not [c for c in cmd if c.startswith("--max-delete")]  # it counts folders; the gate counts files
    assert cmd[-2:] == ["/m/a/", "/r/a/"]
    assert "--delete-excluded" not in cmd and "--ignore-existing" not in cmd and "-n" not in cmd


def test_file_item_cmd():
    readme = item("model-archive/README.md")
    cmd = mm.rsync_cmd(Path("/m/x/README.md"), Path("/r/x/README.md"), readme, backup_dir=Path("/r/d"))
    assert cmd[-2:] == ["/m/x/README.md", "/r/x/README.md"]
    assert "--delete" not in cmd


def test_config_excludes_only_its_anchored_database():
    cmd = mm.rsync_cmd(Path("/m/c"), Path("/r/c"), item("config"), backup_dir=None)
    assert "--exclude=/absdatabase.sqlite" in cmd and "--exclude=/absdatabase.sqlite-*" in cmd
    audio = mm.rsync_cmd(Path("/m"), Path("/r"), item("audiobooks"), backup_dir=None)
    assert not [c for c in audio if "exclude" in c]


def test_excluded_mirrors_rsyncs_patterns():
    config, live = item("config"), item("model-archive/jess-live")
    assert config.excluded(Path("absdatabase.sqlite"), is_dir=False)
    assert config.excluded(Path("absdatabase.sqlite-journal"), is_dir=False)
    assert not config.excluded(Path("sub/absdatabase.sqlite"), is_dir=False)  # anchored
    assert live.excluded(Path("org__m/.cache/huggingface"), is_dir=True)
    assert not live.excluded(Path("org__m/.cache"), is_dir=True)
    assert live.excluded(Path("org__m/w.safetensors.incomplete"), is_dir=False)


def test_every_sqlite_file_belongs_to_an_item():
    for rel in mm.SQLITE_FILES:
        assert any(rel.startswith(i.path + "/") for i in mm.ITEMS)


# --------------------------------------------------------------------------
# Comparing the trees (no rsync)
# --------------------------------------------------------------------------


def twin_trees(host, n: int = 30) -> list[Path]:
    """n books in the source and an identical mirror copy (same sizes and mtimes)."""
    paths = books(host, n)
    shutil.copytree(host.src / "audiobooks", host.dst / "audiobooks", copy_function=shutil.copy2)
    return paths


def plan(host, path: str = "audiobooks", names: bool = False):
    return mm.plan_changes(host.src, host.dst, item(path), want_names=names)


def test_identical_trees_lose_nothing(host):
    twin_trees(host)
    p = plan(host)
    assert (p.existing, p.leaving) == (31, 0)  # 30 books + the Author's one


def test_deletions_and_overwrites_count(host):
    paths = twin_trees(host)
    paths[0].unlink()
    paths[1].write_text("a different size now")
    assert plan(host).leaving == 2


def test_type_changes_count(host):
    """rsync --stats calls a file turned into a folder "created": the comparison must not."""
    paths = twin_trees(host)
    paths[0].unlink()
    paths[0].mkdir()
    assert plan(host).leaving == 1


def test_source_symlinks_and_fifos_keep_the_mirror_copy_in_container_folders(host):
    """--no-links / --no-specials: rsync skips them and keeps the mirror's file, so nothing leaves."""
    paths = twin_trees(host)
    paths[0].unlink()
    paths[0].symlink_to("/etc/hostname")
    paths[1].unlink()
    os.mkfifo(paths[1])
    shutil.rmtree(host.src / "audiobooks" / "Author")
    (host.src / "audiobooks" / "Author").symlink_to(host.tmp)
    assert plan(host).leaving == 0


def test_a_folder_swapped_for_a_symlink_counts_in_a_links_item(host):
    live = host.src / "model-archive" / "jess-live"
    (live / "m").mkdir()
    for i in range(5):
        (live / "m" / f"w{i}").write_text("w")
    shutil.copytree(live, host.dst / "model-archive" / "jess-live", symlinks=True, copy_function=shutil.copy2)
    (live / "m").rename(live / "m.old")
    (live / "m").symlink_to("m.old")
    assert plan(host, "model-archive/jess-live").leaving == 5


def test_hard_links_of_one_new_file_count(host):
    paths = twin_trees(host)
    note = host.src / "audiobooks" / "note.txt"
    note.write_text("pay up")
    for p in paths[:5]:
        p.unlink()
        os.link(note, p)
    assert plan(host).leaving == 5


def test_a_deleted_folder_counts_each_file(host):
    twin_trees(host)
    shutil.rmtree(host.src / "audiobooks" / "Author")
    assert plan(host).leaving == 1


def test_excluded_mirror_files_are_not_counted(host):
    (host.dst / "config").mkdir()
    shutil.copy2(host.src / DB, host.dst / DB)
    make_db(host.src / DB, rows=1)  # source DB changed: snapshotted, not rsynced
    assert plan(host, "config").existing == 0


def test_names_only_on_request(host):
    paths = twin_trees(host)
    paths[0].unlink()
    assert plan(host).names == []
    assert plan(host, names=True).names == ["audiobooks/book000.mp3"]


@pytest.mark.parametrize(
    ("leaving", "existing", "held"),
    [
        (501, 100_000, True),  # over the absolute cap
        (9, 14, False),  # under the floor: a small folder's normal churn
        (10, 14, True),  # a small folder wiped
        (100, 1552, False),  # 6 %: an author deleted
        (200, 1552, True),  # 13 %
    ],
)
def test_over_limit(leaving, existing, held):
    assert mm.over_limit(leaving, existing, 500, 0.10) is held


def test_accepted_limit_has_a_margin():
    assert mm.accepted_limit(20) == 30
    assert mm.accepted_limit(800) == 880


# --------------------------------------------------------------------------
# State and accept files
# --------------------------------------------------------------------------


def test_state_window_and_reset():
    state = mm.State()  # 30-day window
    now = 1_800_000_000.0
    state.record("a", now - 31 * 86400, 50, accepted=False, trip=False, stamp="S0")
    state.record("a", now - 86400, 5, accepted=False, trip=False, stamp="S1")
    assert state.recent("a", now) == 5 and state.tripped("a") is None
    state.record("a", now, 3, accepted=False, trip=True, stamp="S2")
    assert state.tripped("a") == ("S2", 3) and state.recent("a", now) == 8
    state.record("a", now, 40, accepted=True, trip=False, stamp="S3")
    assert state.tripped("a") is None and state.recent("a", now) == 0  # what an accept moved was approved


def test_unreadable_state_holds_every_gated_folder(host, monkeypatch, capsys):
    """Fail closed: setting a broken state file aside must not clear a trip."""
    calls = fake_rsync(monkeypatch, stdout="")
    (host.dst / mm.STATE_FILE).write_text("{not json")
    assert run(host) == 1
    out = capsys.readouterr().out
    assert "unreadable" in out and "audiobooks: HELD" in out
    assert [c for c in calls if "-n" not in c and c[-2].rstrip("/").endswith("software")]  # ungated still copied
    assert not [c for c in calls if c[-2].rstrip("/").endswith("audiobooks")]
    assert list(host.dst.glob(f"{mm.STATE_FILE}.unreadable-state-*"))
    write_accept(host, "audiobooks", "0")
    assert run(host) == 1  # the other folders are still held
    assert "audiobooks: HELD" not in capsys.readouterr().out


def test_take_accept_uses_the_file_up(host):
    path = write_accept(host, "audiobooks", "20\n")
    assert mm.take_accept(host.dst, item("audiobooks"), time.time()) == (20, "")
    assert not path.exists()


@pytest.mark.parametrize(
    ("text", "age", "note"),
    [("", 0, "number"), ("lots", 0, "number"), ("20", 25 * 3600, "24 h")],
)
def test_bad_or_stale_accept_is_ignored_and_used_up(host, text, age, note):
    path = write_accept(host, "audiobooks", text)
    old = time.time() - age
    os.utime(path, (old, old))
    accepted, message = mm.take_accept(host.dst, item("audiobooks"), time.time())
    assert accepted is None and note in message
    assert not path.exists()


def test_a_symlinked_accept_is_ignored(host):
    target = host.tmp / "target"
    target.write_text("999")
    (host.dst / mm.ACCEPT_DIR).mkdir()
    (host.dst / mm.ACCEPT_DIR / "audiobooks").symlink_to(target)
    accepted, message = mm.take_accept(host.dst, item("audiobooks"), time.time())
    assert accepted is None and "regular file" in message
    assert target.read_text() == "999"


def test_accept_is_used_up_even_when_the_copy_fails(host, monkeypatch):
    fake_rsync(monkeypatch, 23, stdout="")
    path = write_accept(host, "audiobooks", "20")
    assert run(host) == 1
    assert not path.exists()


# --------------------------------------------------------------------------
# Exit codes with a stub rsync
# --------------------------------------------------------------------------


def test_every_item_is_copied_and_postgres_is_not(host, monkeypatch):
    calls = fake_rsync(monkeypatch, stdout="")
    assert run(host) == 0
    assert len(calls) == 2 * len(mm.ITEMS)  # a dry run (does it fit?) then the copy
    calls = [c for c in calls if "-n" not in c]
    for it in mm.ITEMS:
        assert any(c[-2].rstrip("/") == str(host.src / it.path) for c in calls)
    assert not any("postgres" in c[-2] for c in calls)
    software = next(c for c in calls if c[-2].rstrip("/").endswith("software"))
    assert "--backup" not in software and not [x for x in software if x.startswith("--max-delete")]


def test_failed_rsync_returns_1_and_leaves_the_metric_alone(host, monkeypatch, capsys):
    (host.metrics / PROM).write_text("old\n")
    fake_rsync(monkeypatch, 23, stdout="", stderr=f'rsync: open "{SECRET_NAME}" failed\n')
    assert run(host) == 1
    out = capsys.readouterr()
    assert "FAILED rc=23" in out.out
    assert SECRET_NAME not in out.out + out.err
    assert (host.metrics / PROM).read_text() == "old\n"


def test_vanished_files_count_as_success(host, monkeypatch, capsys):
    fake_rsync(monkeypatch, 24)
    assert run(host) == 0
    assert "vanished mid-copy" in capsys.readouterr().out
    text = (host.metrics / PROM).read_text()
    for name in ("success_timestamp_seconds ", "used_bytes ", "duration_seconds ", 'last_files{change="new"} '):
        assert f"jess_media_mirror_{name}" in text
    assert oct((host.metrics / PROM).stat().st_mode & 0o777) == "0o644"


def test_an_exception_in_one_item_does_not_stop_the_rest(host, monkeypatch, capsys):
    calls = fake_rsync(monkeypatch, stdout="")
    real = mm.secure_dir

    def flaky(root, rel, like=None):
        if str(rel) == "audiobooks":
            raise OSError(5, "Input/output error", str(host.src / SECRET_NAME))
        return real(root, rel, like)

    monkeypatch.setattr(mm, "secure_dir", flaky)
    assert run(host) == 1
    out = capsys.readouterr().out
    assert "audiobooks: FAILED (OSError 5)" in out
    assert SECRET_NAME not in out
    assert any(c[-2].rstrip("/").endswith("jess-live") for c in calls)


def test_parent_folders_copy_the_source_owner_and_mode(host, monkeypatch):
    fake_rsync(monkeypatch, stdout="")
    os.chmod(host.src / "model-archive", 0o775)
    assert run(host) == 0
    assert stat.S_IMODE(os.stat(host.dst / "model-archive").st_mode) == 0o775


def test_a_symlinked_deleted_folder_fails_the_run(host, monkeypatch):
    fake_rsync(monkeypatch, stdout="")
    target = host.tmp / "elsewhere"
    target.mkdir()
    (host.dst / mm.DELETED_DIR).symlink_to(target)
    assert run(host) == 1
    assert list(target.iterdir()) == []


def test_nothing_is_pruned_after_a_failed_run(host, monkeypatch):
    old = host.dst / mm.DELETED_DIR / "20200101T000000Z" / "audiobooks"
    old.mkdir(parents=True)
    (old / "f").write_text("1")
    fake_rsync(monkeypatch, 23, stdout="")
    assert run(host) == 1
    assert (old / "f").exists()
    fake_rsync(monkeypatch, 0, stdout="")
    assert run(host) == 0
    assert not old.parent.exists()


# --------------------------------------------------------------------------
# SQLite snapshots
# --------------------------------------------------------------------------


def test_snapshot_new_unchanged_updated(host, monkeypatch):
    fake_rsync(monkeypatch, stdout="")
    assert run(host) == 0
    assert rows(host.dst / DB) == 3
    assert run(host) == 0
    assert not (host.dst / mm.DELETED_DIR).exists()  # unchanged: no history entry
    make_db(host.src / DB, rows=5)  # 8 rows now
    assert mm.snapshot_sqlite(host.src, host.dst, DB, "20260101T000000Z") == "updated"
    assert rows(host.dst / DB) == 8
    assert rows(host.dst / mm.DELETED_DIR / "20260101T000000Z" / DB) == 3
    assert not [p for p in (host.dst / "config").iterdir() if p.name.startswith(".absdatabase")]


def test_snapshot_keeps_mode_without_setuid(host):
    os.chmod(host.src / DB, 0o4640)
    mm.snapshot_sqlite(host.src, host.dst, DB, "S")
    assert stat.S_IMODE(os.stat(host.dst / DB).st_mode) == 0o640


def test_a_broken_database_fails_the_run(host, monkeypatch, capsys):
    (host.metrics / PROM).write_text("old\n")
    fake_rsync(monkeypatch, stdout="")
    (host.src / DB).write_bytes(b"not a database at all" * 100)
    assert run(host) == 1
    assert "config: database snapshot FAILED (DatabaseError" in capsys.readouterr().out
    assert (host.metrics / PROM).read_text() == "old\n"
    assert not [p for p in (host.dst / "config").iterdir() if p.name.startswith(".absdatabase")]


def test_a_missing_database_fails_with_a_clear_message(host, monkeypatch, capsys):
    fake_rsync(monkeypatch, stdout="")
    (host.src / DB).unlink()
    assert run(host) == 1
    assert "database is missing from the source" in capsys.readouterr().out


def test_snapshot_refuses_a_symlinked_source_database(host):
    secret = host.tmp / "secret.sqlite"
    make_db(secret)
    (host.src / DB).unlink()
    (host.src / DB).symlink_to(secret)
    with pytest.raises(mm.UnsafePath):
        mm.snapshot_sqlite(host.src, host.dst, DB, "S")


def test_snapshot_refuses_to_write_through_a_mirror_symlink(host):
    outside = host.tmp / "outside"
    outside.mkdir()
    (host.dst / "config").symlink_to(outside)
    with pytest.raises(mm.UnsafePath):
        mm.snapshot_sqlite(host.src, host.dst, DB, "S")
    assert list(outside.iterdir()) == []


@not_root
def test_snapshot_of_a_closed_wal_database_in_a_read_only_folder(host):
    (host.src / DB).unlink()
    make_db(host.src / DB, rows=4, wal=True)
    assert not os.path.exists(f"{host.src / DB}-wal")
    (host.dst / "config").mkdir()  # rsync made it, as on a real night
    os.chmod(host.src / "config", 0o555)
    try:
        assert mm.snapshot_sqlite(host.src, host.dst, DB, "S") == "new"
    finally:
        os.chmod(host.src / "config", 0o755)
    assert rows(host.dst / DB) == 4
    assert sorted(p.name for p in (host.src / "config").iterdir()) == ["absdatabase.sqlite"]  # source untouched


@not_root
def test_no_immutable_retry_while_a_sidecar_exists(host):
    (host.src / DB).unlink()
    make_db(host.src / DB, rows=4, wal=True)
    (host.src / f"{DB}-journal").write_bytes(b"")  # a sidecar: the database may be mid-write
    (host.dst / "config").mkdir()
    os.chmod(host.src / "config", 0o555)
    try:
        with pytest.raises(sqlite3.Error):
            mm.snapshot_sqlite(host.src, host.dst, DB, "S")
    finally:
        os.chmod(host.src / "config", 0o755)


# --------------------------------------------------------------------------
# .deleted/ pruning
# --------------------------------------------------------------------------


def test_prune_removes_only_old_stamp_folders(tmp_path):
    now = 1_800_000_000.0
    deleted = tmp_path / mm.DELETED_DIR
    old, recent = mm.utc_stamp(now - 31 * 86400), mm.utc_stamp(now - 2 * 86400)
    for name in (old, recent, "keep-me-not-a-stamp"):
        (deleted / name / "x").mkdir(parents=True)
        (deleted / name / "x" / "f").write_text("1")
    target = tmp_path / "target"
    target.mkdir()
    (deleted / mm.utc_stamp(now - 40 * 86400)).symlink_to(target)
    assert mm.prune_deleted(tmp_path, 30, now) == 1
    assert sorted(p.name for p in deleted.iterdir() if not p.is_symlink()) == sorted([recent, "keep-me-not-a-stamp"])
    assert target.exists()


def test_count_entries_counts_files_and_symlinks(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "f").write_text("1")
    (tmp_path / "a" / "b" / "g").write_text("1")
    (tmp_path / "a" / "dirlink").symlink_to(tmp_path / "a" / "b")
    assert mm.count_entries(tmp_path / "a") == 3
    assert mm.count_entries(tmp_path / "missing") == 0


# --------------------------------------------------------------------------
# With the real rsync
# --------------------------------------------------------------------------


@needs_rsync
def test_real_copy_then_delete_goes_to_deleted_folder(host, capsys):
    assert run(host) == 0
    book = host.dst / "audiobooks" / "Author" / SECRET_NAME
    assert book.read_bytes() == b"x" * 100
    assert (host.dst / "model-archive" / "README.md").read_text() == "archive readme\n"
    assert rows(host.dst / DB) == 3
    assert not (host.dst / "immich" / "postgres").exists()
    assert not (host.dst / mm.DELETED_DIR).exists()

    (host.src / "audiobooks" / "Author" / SECRET_NAME).unlink()
    (host.src / "model-archive" / "README.md").write_text("new readme, longer\n")
    assert run(host) == 0
    assert not book.exists()
    assert len(list((host.dst / mm.DELETED_DIR).glob(f"*/audiobooks/Author/{SECRET_NAME}"))) == 1
    assert [p.read_text() for p in (host.dst / mm.DELETED_DIR).glob("*/model-archive/README.md")] == [
        "archive readme\n"
    ]
    assert rows(host.dst / DB) == 3  # the excluded snapshot survived --delete
    assert 'last_files{change="moved"} 2' in (host.metrics / PROM).read_text()
    out = capsys.readouterr()
    assert SECRET_NAME not in out.out + out.err


@needs_rsync
def test_real_mass_deletion_is_held_every_night_until_accepted(host, capsys):
    paths = books(host, 30)
    assert run(host) == 0
    before = (host.metrics / PROM).read_text()
    for p in paths[:20]:
        p.unlink()
    (host.src / "audiobooks" / "brand-new.mp3").write_text("new")
    for _night in range(3):
        assert run(host) == 1
        assert len(mirror_books(host)) == 30  # nothing drained
    assert not (host.dst / "audiobooks" / "brand-new.mp3").exists()  # a held folder copies nothing
    assert (host.metrics / PROM).read_text() == before
    out = capsys.readouterr().out
    assert "audiobooks: HELD" in out and "echo 20 | sudo tee" in out

    write_accept(host, "audiobooks", "20")
    assert run(host) == 0
    assert len(mirror_books(host)) == 10
    assert (host.dst / "audiobooks" / "brand-new.mp3").exists()
    assert len(list((host.dst / mm.DELETED_DIR).glob("*/audiobooks/book*.mp3"))) == 20
    assert not (host.dst / mm.ACCEPT_DIR / "audiobooks").exists()
    assert (host.metrics / PROM).read_text() != before


@needs_rsync
def test_real_accept_for_fewer_files_holds_again(host):
    paths = books(host, 100)
    assert run(host) == 0
    for p in paths[:60]:
        p.unlink()
    write_accept(host, "audiobooks", "20")  # 20 + margin 10 < 60
    assert run(host) == 1
    assert len(mirror_books(host)) == 100
    assert not (host.dst / mm.ACCEPT_DIR / "audiobooks").exists()


@needs_rsync
@pytest.mark.parametrize("change", ["overwrite", "folder", "hardlink"])
def test_real_mass_change_of_any_kind_is_held(host, change):
    paths = books(host, 30)
    assert run(host) == 0
    note = host.src / "audiobooks" / "note.txt"
    note.write_text("pay up")
    for p in paths[:20]:
        if change == "overwrite":
            p.write_text("encrypted garbage, a different size")
            continue
        p.unlink()
        if change == "folder":
            p.mkdir()
        else:
            os.link(note, p)
    assert run(host) == 1
    assert len(mirror_books(host)) == 30
    assert all(p.is_file() and not p.is_symlink() for p in mirror_books(host))
    assert mirror_books(host)[0].read_text() == "audio book000.mp3"


@needs_rsync
def test_real_slow_drain_is_held_once_the_week_adds_up(host):
    paths = books(host, 30)
    assert run(host) == 0
    results = []
    for night in range(5):
        for p in paths[night * 3 : night * 3 + 3]:
            p.unlink()
        results.append(run(host))
    assert results == [0, 0, 0, 1, 1]  # 3, 6, 9 pass; 12 in a week is over 10 %


@needs_rsync
def test_real_change_between_compare_and_copy_trips_the_item(host, monkeypatch):
    paths = books(host, 30)
    assert run(host) == 0
    for p in paths[:20]:
        p.unlink()
    real_plan = mm.plan_changes
    monkeypatch.setattr(mm, "plan_changes", lambda *a, **k: mm.Plan(existing=31, leaving=0))  # the race
    assert run(host) == 1
    assert len(list((host.dst / mm.DELETED_DIR).glob("*/audiobooks/book*.mp3"))) == 20  # kept, not lost
    monkeypatch.setattr(mm, "plan_changes", real_plan)
    assert run(host) == 1  # stays held
    state = json.loads((host.dst / mm.STATE_FILE).read_text())
    assert state["items"]["audiobooks"]["tripped"] is True


@needs_rsync
def test_real_no_write_through_a_symlink_left_in_the_mirror(host):
    """The reviews' chain: an old mirror symlink, then the source makes it a folder and forces a hold."""
    paths = books(host, 30)
    assert run(host) == 0
    outside = host.tmp / "outside"
    outside.mkdir()
    (host.dst / "audiobooks" / "a").symlink_to(outside)  # what an older version could have copied in
    (host.src / "audiobooks" / "a").mkdir()
    (host.src / "audiobooks" / "a" / "audiobooks").write_text("forged accept")
    saved = {p: p.read_bytes() for p in paths[:20]}
    for p in paths[:20]:
        p.unlink()
    assert run(host) == 1  # held: copies nothing
    assert list(outside.iterdir()) == []
    assert not list((host.dst / mm.ACCEPT_DIR).iterdir())
    for p, data in saved.items():  # put the books back exactly as the mirror has them
        p.write_bytes(data)
        shutil.copystat(host.dst / "audiobooks" / p.name, p)
    assert run(host) == 0  # not held: rsync replaces the symlink with a real folder
    assert list(outside.iterdir()) == []
    assert (host.dst / "audiobooks" / "a").is_dir() and not (host.dst / "audiobooks" / "a").is_symlink()


@needs_rsync
def test_real_symlinks_never_enter_container_folders(host):
    (host.src / "audiobooks" / "link").symlink_to("/etc/hostname")
    (host.src / "model-archive" / "_tools" / "python3").symlink_to("/usr/bin/python3")
    assert run(host) == 0
    assert not os.path.lexists(host.dst / "audiobooks" / "link")
    assert (host.dst / "model-archive" / "_tools" / "python3").is_symlink()


@needs_rsync
def test_real_held_config_still_snapshots_its_database(host):
    for i in range(12):
        (host.src / "config" / f"f{i}.json").write_text("{}")
    assert run(host) == 0
    for i in range(12):
        (host.src / "config" / f"f{i}.json").unlink()
    make_db(host.src / DB, rows=2)
    assert run(host) == 1  # config held
    assert rows(host.dst / DB) == 5


@needs_rsync
def test_real_jess_live_skips_partial_downloads(host):
    live = host.src / "model-archive" / "jess-live" / "org__model"
    (live / ".cache" / "huggingface" / "download").mkdir(parents=True)
    (live / ".cache" / "huggingface" / "download" / "w.incomplete").write_text("half")
    (live / "part.safetensors.incomplete").write_text("half")
    (live / "config.json").write_text("{}")
    assert run(host) == 0
    copied = host.dst / "model-archive" / "jess-live" / "org__model"
    assert (copied / "config.json").exists()
    assert not (copied / ".cache" / "huggingface").exists()
    assert not (copied / "part.safetensors.incomplete").exists()


@needs_rsync
def test_real_setuid_bits_and_specials_are_not_copied(host):
    tool = host.src / "model-archive" / "_tools" / "tool"
    tool.write_text("#!/bin/sh\n")
    os.chmod(tool, 0o4755)
    os.mkfifo(host.src / "audiobooks" / "fifo")
    assert run(host) == 0
    assert stat.S_IMODE(os.stat(host.dst / "model-archive" / "_tools" / "tool").st_mode) == 0o755
    assert not os.path.lexists(host.dst / "audiobooks" / "fifo")


@needs_rsync
def test_real_check_reports_and_lists(host, capsys):
    paths = books(host, 30)
    assert run(host) == 0
    capsys.readouterr()
    paths[0].unlink()
    assert run(host, "--check") == 0
    out = capsys.readouterr().out
    assert "audiobooks: 1 of 31 mirror files would leave" in out and "would mirror" in out
    assert "book000" not in out
    assert run(host, "--check", "--list") == 0
    assert "'audiobooks/book000.mp3'" in capsys.readouterr().out
    for p in paths[1:20]:
        p.unlink()
    assert run(host, "--check") == 1
    assert "would be HELD" in capsys.readouterr().out
    write_accept(host, "audiobooks", "20")
    assert run(host, "--check") == 0
    assert "accept for 20 waiting: would mirror" in capsys.readouterr().out


@needs_rsync
def test_real_files_turned_into_symlinks_are_kept_not_held(host):
    paths = books(host, 30)
    assert run(host) == 0
    for p in paths[:20]:
        p.unlink()
        p.symlink_to("/etc/hostname")
    assert run(host) == 0  # rsync skips the symlinks and keeps the mirror's files: nothing at risk
    assert run(host) == 0
    assert len(mirror_books(host)) == 30 and all(not p.is_symlink() for p in mirror_books(host))


@needs_rsync
def test_real_accept_then_a_quiet_night_is_not_held_again(host, capsys):
    paths = books(host, 100)
    assert run(host) == 0
    for p in paths[:60]:
        p.unlink()
    assert run(host) == 1
    write_accept(host, "audiobooks", "60")
    assert run(host) == 0
    assert run(host) == 0  # the accepted 60 don't count against the next nights
    assert "HELD" not in capsys.readouterr().out.split("accepted once")[-1]


@needs_rsync
def test_real_accept_for_deleted_one_file_folders(host):
    """rsync's --max-delete counts folders too: deleting 20 one-book folders must work with accept 20."""
    for i in range(40):
        (host.src / "audiobooks" / f"Book {i:02}").mkdir()
        (host.src / "audiobooks" / f"Book {i:02}" / "a.m4b").write_text(f"book {i}")
    assert run(host) == 0
    for i in range(20):
        shutil.rmtree(host.src / "audiobooks" / f"Book {i:02}")
    assert run(host) == 1
    write_accept(host, "audiobooks", "20")
    assert run(host) == 0
    assert len(list((host.dst / "audiobooks").glob("Book */a.m4b"))) == 20


def test_a_failed_copy_still_snapshots_the_database(host, monkeypatch, capsys):
    fake_rsync(monkeypatch, 23, stdout="")
    assert run(host) == 1
    assert rows(host.dst / DB) == 3
    assert "config: database snapshot new" in capsys.readouterr().out


def test_immich_has_a_higher_window_floor():
    """A normal week in the ~20-file Immich library (dumps rotating, markers rewritten) isn't held."""
    immich, config = item("immich/library"), item("config")
    state = mm.State()
    now = 1_800_000_000.0
    state.record(immich.slug, now - 86400, 12, accepted=False, trip=False, stamp="S")
    state.record(config.slug, now - 86400, 12, accepted=False, trip=False, stamp="S")
    args = SimpleNamespace(max_changes=500, max_fraction=0.10)
    tonight = mm.Plan(existing=20, leaving=1)
    assert mm.verdict(immich, tonight, state, None, args, now)[0] is False
    assert mm.verdict(config, tonight, state, None, args, now)[0] is True
    wiped = mm.Plan(existing=20, leaving=20)  # tonight alone still uses the normal floor
    assert mm.verdict(immich, wiped, mm.State(), None, args, now)[0] is True


@pytest.mark.parametrize("text", ["\u00b2", "1e3", "-5", "9999999999"])
def test_odd_accept_numbers_are_ignored(host, text):
    write_accept(host, "audiobooks", text)
    assert mm.take_accept(host.dst, item("audiobooks"), time.time())[0] is None


@needs_rsync
def test_real_check_counts_a_bad_accept_as_held(host, capsys):
    paths = books(host, 30)
    assert run(host) == 0
    for p in paths[:20]:
        p.unlink()
    path = write_accept(host, "audiobooks", "5")  # fewer than tonight's 20
    assert run(host, "--check") == 1
    assert path.exists()  # --check never uses it up
    old = time.time() - 25 * 3600
    path.write_text("20")
    os.utime(path, (old, old))
    assert run(host, "--check") == 1
    assert "older than 24 h" in capsys.readouterr().out


@needs_rsync
def test_real_a_trip_names_its_night(host, monkeypatch, capsys):
    paths = books(host, 30)
    assert run(host) == 0
    for p in paths[:20]:
        p.unlink()
    real_plan = mm.plan_changes
    monkeypatch.setattr(mm, "plan_changes", lambda *a, **k: mm.Plan(existing=31, leaving=0))
    assert run(host) == 1
    stamp = json.loads((host.dst / mm.STATE_FILE).read_text())["items"]["audiobooks"]["stamp"]
    monkeypatch.setattr(mm, "plan_changes", real_plan)
    capsys.readouterr()
    assert run(host) == 1
    assert f"copy of {stamp}" in capsys.readouterr().out


def test_hard_links_only_for_host_written_folders():
    for it in mm.ITEMS:
        cmd = mm.rsync_cmd(Path("/m/x"), Path("/r/x"), it, backup_dir=None)
        assert ("-H" in cmd) is it.links, it.path


def test_a_copy_that_would_not_fit_is_held(host, monkeypatch, capsys):
    huge = STATS.replace("1,234,567", "999,999,999,999,999")
    calls = fake_rsync(monkeypatch, stdout=huge)
    assert run(host) == 1
    assert "would not fit" in capsys.readouterr().out
    assert all("-n" in c for c in calls)  # nothing was really copied


def test_rsync_errors_go_to_a_root_only_file_not_the_journal(host, monkeypatch, capsys):
    fake_rsync(monkeypatch, 23, stdout="", stderr=f'rsync: open "{SECRET_NAME}" failed\n')
    assert run(host) == 1
    out = capsys.readouterr()
    assert SECRET_NAME not in out.out + out.err
    kept = host.dst / mm.ERRORS_DIR / "audiobooks.txt"
    assert SECRET_NAME in kept.read_text()
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600


@needs_rsync
def test_real_hard_link_decoys_cannot_poison_the_mirror(host):
    """The review's chain: decoys with each book's size and mtime, later hard-linked to the books."""
    paths = books(host, 30)
    assert run(host) == 0
    decoys = []
    for p in paths:
        d = p.with_name("a_" + p.name)
        d.write_text("x" * p.stat().st_size)
        shutil.copystat(p, d)
        decoys.append(d)
    assert run(host) == 0  # new files: copied
    for p, d in zip(paths, decoys, strict=True):
        d.unlink()
        os.link(p, d)
    assert run(host) == 0
    assert [b.read_text() for b in mirror_books(host)] == [f"audio {p.name}" for p in paths]


@needs_rsync
def test_real_a_trip_needs_its_own_count_to_clear(host, monkeypatch):
    paths = books(host, 30)
    assert run(host) == 0
    for p in paths[:20]:
        p.unlink()
    real_plan = mm.plan_changes
    monkeypatch.setattr(mm, "plan_changes", lambda *a, **k: mm.Plan(existing=31, leaving=0))
    assert run(host) == 1  # tripped: 20 moved
    monkeypatch.setattr(mm, "plan_changes", real_plan)
    write_accept(host, "audiobooks", "0")  # what `plan.leaving` would say now
    assert run(host) == 1
    write_accept(host, "audiobooks", "20")
    assert run(host) == 0


@needs_rsync
def test_real_an_interrupted_copy_is_counted_on_the_next_run(host, monkeypatch, capsys):
    paths = books(host, 30)
    assert run(host) == 0
    for p in paths[:20]:
        p.unlink()
    real_plan, real_run = mm.plan_changes, mm.run_rsync
    monkeypatch.setattr(mm, "plan_changes", lambda *a, **k: mm.Plan(existing=31, leaving=0))

    def killed_after_copy(cmd):
        result = real_run(cmd)
        if "-n" not in cmd and cmd[-2].rstrip("/").endswith("audiobooks"):
            raise SystemExit(143)  # SIGTERM right after rsync, before anything was recorded
        return result

    monkeypatch.setattr(mm, "run_rsync", killed_after_copy)
    with pytest.raises(SystemExit):
        run(host)
    monkeypatch.setattr(mm, "plan_changes", real_plan)
    monkeypatch.setattr(mm, "run_rsync", real_run)
    capsys.readouterr()
    assert run(host) == 1
    out = capsys.readouterr().out
    assert "interrupted copy" in out and "moved 20 files - TRIPPED" in out
    assert json.loads((host.dst / mm.STATE_FILE).read_text())["items"]["audiobooks"]["tripped"] is True
