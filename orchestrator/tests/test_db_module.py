"""Tests for orchestrator/db.py — get_db, init_db, vacuum_db."""

import os
import sqlite3
import sys
from sqlite3 import ProgrammingError

import pytest

# Ensure orchestrator package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from orchestrator.db import get_db, init_db, vacuum_db


class TestGetDb:
    """get_db context manager: open, commit, rollback, close."""

    def test_commits_on_success(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        with get_db(db_path) as conn:
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
            conn.execute("INSERT INTO t (val) VALUES ('hello')")

        # Verify data persisted (committed)
        with get_db(db_path) as conn:
            row = conn.execute("SELECT val FROM t").fetchone()
            assert row["val"] == "hello"

    def test_rolls_back_on_exception(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        # Create table first
        with get_db(db_path) as conn:
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")

        # Insert then raise — should rollback
        with pytest.raises(RuntimeError), get_db(db_path) as conn:
            conn.execute("INSERT INTO t (val) VALUES ('should_vanish')")
            raise RuntimeError("boom")

        # Verify the insert was rolled back
        with get_db(db_path) as conn:
            row = conn.execute("SELECT COUNT(*) as cnt FROM t").fetchone()
            assert row["cnt"] == 0

    def test_connection_closed_after_context(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        with get_db(db_path) as conn:
            conn.execute("CREATE TABLE t (id INTEGER)")

        # Connection should be closed — executing on it should fail
        with pytest.raises(ProgrammingError):
            conn.execute("SELECT 1")

    def test_row_factory_is_set(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        with get_db(db_path) as conn:
            assert conn.row_factory == sqlite3.Row

    def test_foreign_keys_enabled_by_default(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        with get_db(db_path) as conn:
            fk = conn.execute("PRAGMA foreign_keys").fetchone()
            assert fk[0] == 1

    def test_foreign_keys_disabled(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        with get_db(db_path, foreign_keys=False) as conn:
            fk = conn.execute("PRAGMA foreign_keys").fetchone()
            assert fk[0] == 0


class TestInitDb:
    """init_db creates dirs and executes schema SQL."""

    def test_creates_table(self, tmp_path):
        db_path = str(tmp_path / "sub" / "dir" / "test.db")
        schema = "CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, name TEXT)"
        init_db(db_path, schema)

        # Verify table exists
        with get_db(db_path) as conn:
            conn.execute("INSERT INTO items (name) VALUES ('test')")
            row = conn.execute("SELECT name FROM items").fetchone()
            assert row["name"] == "test"

    def test_creates_parent_directories(self, tmp_path):
        db_path = str(tmp_path / "a" / "b" / "c" / "test.db")
        init_db(db_path, "CREATE TABLE IF NOT EXISTS t (id INTEGER)")
        assert os.path.exists(db_path)

    def test_idempotent_with_if_not_exists(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        schema = "CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)"
        init_db(db_path, schema)
        init_db(db_path, schema)  # Should not raise

        with get_db(db_path) as conn:
            # Table should still be there
            conn.execute("INSERT INTO t (id) VALUES (1)")


class TestVacuumDb:
    """vacuum_db runs VACUUM without error."""

    def test_vacuum_succeeds(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        init_db(db_path, "CREATE TABLE IF NOT EXISTS t (id INTEGER)")
        # Should not raise
        vacuum_db(db_path)
        assert os.path.exists(db_path)
