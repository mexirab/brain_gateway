"""
Tests for scripts/backup_state.py — the state backup that protects the
irreplaceable data (SQLite DBs + auto_learn.key). Run end-to-end via
subprocess so exit codes and the real entry point are covered.

Guarantees under test:
  - live SQLite DBs are captured *consistently* (the snapshot opens and passes
    integrity_check, with the row we wrote)
  - auto_learn.key's 0600 mode survives the round-trip
  - hf_cache/ and prior app-backup-*/ are excluded; raw -wal/-shm are excluded
  - the archive is 0600 (it holds the token + key)
  - rotation keeps exactly JESS_BACKUP_KEEP archives
  - the Prometheus freshness metric is written
  - empty / missing data dir is refused (exit 1), not an empty archive
"""

import os
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "backup_state.py"


def _make_wal_db(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t (v) VALUES (?)", (marker,))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def state_tree(tmp_path):
    """A realistic data/ + credentials/ layout with excludable noise."""
    data = tmp_path / "data"
    creds = tmp_path / "credentials"

    _make_wal_db(data / "app" / "brain_state.db", "hello-reminders")
    _make_wal_db(data / "chroma" / "personal_rag" / "chroma.sqlite3", "vectors")

    key = data / "app" / "auto_learn.key"
    key.write_bytes(b"0" * 44)
    key.chmod(0o600)

    (data / "app" / "self_audits").mkdir(parents=True)
    (data / "app" / "self_audits" / "audit.json").write_text("{}")

    # Excludable noise:
    (data / "hf_cache").mkdir()
    (data / "hf_cache" / "big.bin").write_bytes(b"x" * 4096)
    (data / "app-backup-20200101-000000").mkdir()
    (data / "app-backup-20200101-000000" / "old.db").write_bytes(b"stale")
    # A stray WAL sidecar that must not be copied raw:
    (data / "app" / "brain_state.db-wal").write_bytes(b"torn")

    creds.mkdir()
    (creds / "google_token.json").write_text('{"refresh_token": "x"}')

    return tmp_path, data, creds


def _run(tmp_path, data, creds, **extra_env):
    env = {
        **os.environ,
        "JESS_DATA_DIR": str(data),
        "JESS_CREDENTIALS_DIR": str(creds),
        "JESS_BACKUP_DIR": str(tmp_path / "backups"),
        **extra_env,
    }
    return subprocess.run(
        [sys.executable, str(_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _only_archive(tmp_path):
    archives = list((tmp_path / "backups").glob("jess-state-*.tar.gz"))
    assert len(archives) == 1, archives
    return archives[0]


class TestBackupContents:
    def test_archive_written_and_locked_down(self, state_tree):
        tmp_path, data, creds = state_tree
        r = _run(tmp_path, data, creds)
        assert r.returncode == 0, r.stderr
        archive = _only_archive(tmp_path)
        assert (archive.stat().st_mode & 0o777) == 0o600

    def test_includes_critical_excludes_noise(self, state_tree):
        tmp_path, data, creds = state_tree
        _run(tmp_path, data, creds)
        with tarfile.open(_only_archive(tmp_path)) as tar:
            names = set(tar.getnames())
        assert "data/app/brain_state.db" in names
        assert "data/app/auto_learn.key" in names
        assert "data/app/self_audits/audit.json" in names
        assert "data/chroma/personal_rag/chroma.sqlite3" in names
        assert "credentials/google_token.json" in names
        # excluded
        assert not any("hf_cache" in n for n in names)
        assert not any("app-backup-" in n for n in names)
        assert not any(n.endswith("-wal") or n.endswith("-shm") for n in names)

    def test_sqlite_snapshot_is_consistent(self, state_tree, tmp_path):
        src_tmp, data, creds = state_tree
        _run(src_tmp, data, creds)
        extract = tmp_path / "extract"
        extract.mkdir()
        with tarfile.open(_only_archive(src_tmp)) as tar:
            tar.extract("data/app/brain_state.db", extract)
        db = extract / "data" / "app" / "brain_state.db"
        conn = sqlite3.connect(str(db))
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("SELECT v FROM t").fetchone()[0] == "hello-reminders"
        finally:
            conn.close()

    def test_key_mode_preserved(self, state_tree, tmp_path):
        src_tmp, data, creds = state_tree
        _run(src_tmp, data, creds)
        extract = tmp_path / "extract"
        extract.mkdir()
        with tarfile.open(_only_archive(src_tmp)) as tar:
            tar.extract("data/app/auto_learn.key", extract)
        key = extract / "data" / "app" / "auto_learn.key"
        assert (key.stat().st_mode & 0o777) == 0o600


class TestMetricAndRotation:
    def test_metric_written(self, state_tree, tmp_path):
        src_tmp, data, creds = state_tree
        metric = tmp_path / "jess_backup.prom"
        r = _run(src_tmp, data, creds, JESS_BACKUP_METRICS_PATH=str(metric))
        assert r.returncode == 0
        body = metric.read_text()
        assert "jess_backup_success_timestamp_seconds " in body
        assert "jess_backup_size_bytes " in body

    def test_rotation_keeps_only_n(self, state_tree):
        src_tmp, data, creds = state_tree
        backups = src_tmp / "backups"
        backups.mkdir()
        # Pre-seed three older archives; sortable names older than "now".
        for stamp in ("20200101-000000", "20200102-000000", "20200103-000000"):
            (backups / f"jess-state-{stamp}.tar.gz").write_bytes(b"old")
        r = _run(src_tmp, data, creds, JESS_BACKUP_KEEP="2")
        assert r.returncode == 0
        remaining = sorted(p.name for p in backups.glob("jess-state-*.tar.gz"))
        assert len(remaining) == 2, remaining
        # The two newest survive: the just-written one + 20200103.
        assert any("20200103" in n for n in remaining)
        assert not any("20200101" in n for n in remaining)
        assert not any("20200102" in n for n in remaining)


class TestResilience:
    """The orchestrator container runs as root and writes some data/app files
    root-owned, so a host-user cron can't read every file. A single unreadable
    (or mid-run-vanished) file must not sink the whole backup — unless it's
    critical. Dangling symlinks force a copy failure portably (chmod 000 won't,
    since the container test runs as root)."""

    def test_noncritical_unreadable_file_is_skipped(self, state_tree, tmp_path):
        src_tmp, data, creds = state_tree
        # A non-critical file that can't be read (points nowhere).
        (data / "app" / "self_audits" / "ghost.json").symlink_to(data / "does_not_exist")
        metric = tmp_path / "m.prom"
        r = _run(src_tmp, data, creds, JESS_BACKUP_METRICS_PATH=str(metric))
        assert r.returncode == 0, r.stderr
        # Backup still written, still contains the critical DB.
        with tarfile.open(_only_archive(src_tmp)) as tar:
            names = set(tar.getnames())
        assert "data/app/brain_state.db" in names
        assert "jess_backup_skipped_files 1" in metric.read_text()

    def test_critical_unreadable_file_fails_loudly(self, state_tree):
        src_tmp, data, creds = state_tree
        # A critical (.db) file that can't be read must abort with exit 2 —
        # never a backup silently missing state.
        (data / "app" / "extra.db").symlink_to(data / "does_not_exist")
        r = _run(src_tmp, data, creds)
        assert r.returncode == 2, r.stderr
        assert list((src_tmp / "backups").glob("jess-state-*.tar.gz")) == []


class TestRefusals:
    def test_missing_data_dir_exits_1(self, tmp_path):
        r = _run(tmp_path, tmp_path / "nope", tmp_path / "nocreds")
        assert r.returncode == 1
        assert list((tmp_path / "backups").glob("jess-state-*.tar.gz")) == []

    def test_empty_dirs_refuse_empty_archive(self, tmp_path):
        data = tmp_path / "data"
        creds = tmp_path / "credentials"
        data.mkdir()
        creds.mkdir()
        r = _run(tmp_path, data, creds, JESS_BACKUP_METRICS_PATH=str(tmp_path / "m.prom"))
        assert r.returncode == 1
        assert list((tmp_path / "backups").glob("jess-state-*.tar.gz")) == []
        # a failed run still records a (non-success) metric file
        assert "jess_backup_success_timestamp_seconds" in (tmp_path / "m.prom").read_text()
