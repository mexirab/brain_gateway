"""
Shared SQLite database utilities for Brain Gateway.

Provides a consistent context manager and initialization helper used by
state_store, finance_manager, and progress_tracker.
"""

import logging
import os
import sqlite3
from contextlib import contextmanager

logger = logging.getLogger(__name__)


@contextmanager
def get_db(db_path: str, *, foreign_keys: bool = True):
    """
    Get a SQLite connection as a context manager.

    Sets WAL journal mode, row_factory, and optionally foreign keys.
    Commits on success, rolls back on exception, always closes.

    Args:
        db_path: Path to the SQLite database file.
        foreign_keys: Whether to enable foreign key constraints (default True).
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str, schema_sql: str, *, foreign_keys: bool = True) -> None:
    """
    Initialize a database: create parent dirs and execute schema SQL.

    Args:
        db_path: Path to the SQLite database file.
        schema_sql: SQL string to execute (CREATE TABLE IF NOT EXISTS, etc.).
        foreign_keys: Whether to enable foreign key constraints.
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with get_db(db_path, foreign_keys=foreign_keys) as conn:
        conn.executescript(schema_sql)
    logger.info("[DB] Initialized %s", db_path)


def vacuum_db(db_path: str) -> None:
    """Run VACUUM to reclaim disk space. Should be called periodically."""
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()
    logger.info("[DB] Vacuumed %s", db_path)
