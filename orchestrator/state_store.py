"""
Persistent State Store for Brain Gateway.

SQLite-backed persistence for reminders, focus sessions, and notification
tracking. Survives orchestrator restarts.

Uses the same pattern as finance_manager.py (contextmanager + WAL mode).
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("STATE_DB_PATH", "/app/data/brain_state.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    trigger_time TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT 'both',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS focus_sessions (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    active INTEGER NOT NULL DEFAULT 0,
    task TEXT,
    started_at TEXT,
    duration_minutes INTEGER,
    break_duration_minutes INTEGER,
    job_id TEXT,
    audio_player TEXT,
    block_sites INTEGER NOT NULL DEFAULT 0,
    task_description TEXT,
    sprint_count INTEGER NOT NULL DEFAULT 0,
    sprints_planned INTEGER,
    check_in_interval INTEGER,
    check_in_job_id TEXT,
    total_focus_minutes INTEGER NOT NULL DEFAULT 0,
    audio_source TEXT NOT NULL DEFAULT 'endel'
);

CREATE TABLE IF NOT EXISTS notification_tracking (
    key TEXT PRIMARY KEY,
    notified_at TEXT NOT NULL
);
"""


@contextmanager
def get_db():
    """Get a SQLite connection with row factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Initialize database schema."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.executescript(SCHEMA_SQL)
        # Seed empty focus session if not exists
        row = conn.execute("SELECT COUNT(*) FROM focus_sessions").fetchone()
        if row[0] == 0:
            conn.execute("INSERT INTO focus_sessions (id) VALUES (1)")
        # F-004 migration: add new columns if they don't exist yet
        _f004_cols = [
            ("task_description", "TEXT"),
            ("sprint_count", "INTEGER NOT NULL DEFAULT 0"),
            ("sprints_planned", "INTEGER"),
            ("check_in_interval", "INTEGER"),
            ("check_in_job_id", "TEXT"),
            ("total_focus_minutes", "INTEGER NOT NULL DEFAULT 0"),
            ("audio_source", "TEXT NOT NULL DEFAULT 'endel'"),
        ]
        existing = {row[1] for row in conn.execute("PRAGMA table_info(focus_sessions)").fetchall()}
        for col_name, col_def in _f004_cols:
            if col_name not in existing:
                conn.execute(f"ALTER TABLE focus_sessions ADD COLUMN {col_name} {col_def}")
                logger.info(f"[STATE] Migrated focus_sessions: added {col_name}")
    logger.info(f"[STATE] Database initialized at {DB_PATH}")


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------


def save_reminder(reminder_id: str, text: str, trigger_time: str, target: str = "both") -> None:
    """Save a reminder to persistent storage."""
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO reminders (id, text, trigger_time, target, status, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?)""",
            (reminder_id, text, trigger_time, target, datetime.now().isoformat()),
        )


def get_reminder(reminder_id: str) -> Optional[Dict[str, Any]]:
    """Get a single reminder by ID."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        return dict(row) if row else None


def get_pending_reminders() -> List[Dict[str, Any]]:
    """Get all pending reminders."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM reminders WHERE status = 'pending' ORDER BY trigger_time").fetchall()
        return [dict(r) for r in rows]


def complete_reminder(reminder_id: str) -> bool:
    """Mark a reminder as completed."""
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE reminders SET status = 'completed', completed_at = ? WHERE id = ?",
            (datetime.now().isoformat(), reminder_id),
        )
        return cursor.rowcount > 0


def cancel_reminder(reminder_id: str) -> bool:
    """Mark a reminder as cancelled."""
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE reminders SET status = 'cancelled', completed_at = ? WHERE id = ?",
            (datetime.now().isoformat(), reminder_id),
        )
        return cursor.rowcount > 0


def delete_reminder(reminder_id: str) -> bool:
    """Delete a reminder from storage."""
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Focus Sessions
# ---------------------------------------------------------------------------


def save_focus_session(session: Dict[str, Any]) -> None:
    """Save focus session state to persistent storage."""
    with get_db() as conn:
        conn.execute(
            """UPDATE focus_sessions SET
                active = ?, task = ?, started_at = ?, duration_minutes = ?,
                break_duration_minutes = ?, job_id = ?, audio_player = ?, block_sites = ?,
                task_description = ?, sprint_count = ?, sprints_planned = ?,
                check_in_interval = ?, check_in_job_id = ?, total_focus_minutes = ?,
                audio_source = ?
               WHERE id = 1""",
            (
                1 if session.get("active") else 0,
                session.get("task"),
                session.get("started").isoformat() if session.get("started") else None,
                session.get("duration"),
                session.get("break_duration"),
                session.get("job_id"),
                session.get("audio_player"),
                1 if session.get("block_sites") else 0,
                session.get("task_description"),
                session.get("sprint_count", 0),
                session.get("sprints_planned"),
                session.get("check_in_interval"),
                session.get("check_in_job_id"),
                session.get("total_focus_minutes", 0),
                session.get("audio_source", "endel"),
            ),
        )


def load_focus_session() -> Dict[str, Any]:
    """Load focus session state from persistent storage."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM focus_sessions WHERE id = 1").fetchone()
        if not row or not row["active"]:
            return {
                "active": False,
                "task": None,
                "started": None,
                "duration": None,
                "break_duration": None,
                "job_id": None,
                "audio_player": None,
                "block_sites": False,
                "task_description": None,
                "sprint_count": 0,
                "sprints_planned": None,
                "check_in_interval": None,
                "check_in_job_id": None,
                "total_focus_minutes": 0,
                "audio_source": "endel",
            }
        started = datetime.fromisoformat(row["started_at"]) if row["started_at"] else None
        return {
            "active": True,
            "task": row["task"],
            "started": started,
            "duration": row["duration_minutes"],
            "break_duration": row["break_duration_minutes"],
            "job_id": row["job_id"],
            "audio_player": row["audio_player"],
            "block_sites": bool(row["block_sites"]),
            "task_description": row["task_description"],
            "sprint_count": row["sprint_count"] or 0,
            "sprints_planned": row["sprints_planned"],
            "check_in_interval": row["check_in_interval"],
            "check_in_job_id": row["check_in_job_id"],
            "total_focus_minutes": row["total_focus_minutes"] or 0,
            "audio_source": row["audio_source"] or "endel",
        }


def clear_focus_session() -> None:
    """Reset focus session to inactive."""
    with get_db() as conn:
        conn.execute(
            """UPDATE focus_sessions SET
                active = 0, task = NULL, started_at = NULL, duration_minutes = NULL,
                break_duration_minutes = NULL, job_id = NULL, audio_player = NULL, block_sites = 0,
                task_description = NULL, sprint_count = 0, sprints_planned = NULL,
                check_in_interval = NULL, check_in_job_id = NULL, total_focus_minutes = 0,
                audio_source = 'endel'
               WHERE id = 1"""
        )


# ---------------------------------------------------------------------------
# Notification Tracking
# ---------------------------------------------------------------------------


def mark_notified(key: str) -> None:
    """Mark a notification key as sent."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO notification_tracking (key, notified_at) VALUES (?, ?)",
            (key, datetime.now().isoformat()),
        )


def is_notified(key: str) -> bool:
    """Check if a notification key has been sent."""
    with get_db() as conn:
        row = conn.execute("SELECT 1 FROM notification_tracking WHERE key = ?", (key,)).fetchone()
        return row is not None


def clear_stale_notifications(older_than_hours: int = 48) -> int:
    """Remove notification tracking entries older than the threshold."""
    cutoff = (datetime.now() - timedelta(hours=older_than_hours)).isoformat()
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM notification_tracking WHERE notified_at < ?", (cutoff,))
        count = cursor.rowcount
    if count > 0:
        logger.info(f"[STATE] Cleared {count} stale notification entries (>{older_than_hours}h)")
    return count


def clear_notifications_by_prefix(prefix: str) -> int:
    """Remove all notification tracking entries matching a prefix (e.g., 'temp:')."""
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM notification_tracking WHERE key LIKE ?", (f"{prefix}%",))
        return cursor.rowcount
