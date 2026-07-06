"""
Persistent State Store for Brain Gateway.

SQLite-backed persistence for reminders, focus sessions, and notification
tracking. Survives orchestrator restarts.

Uses the same pattern as finance_manager.py (contextmanager + WAL mode).
"""

import logging
import os
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

CREATE TABLE IF NOT EXISTS announcement_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    text TEXT NOT NULL,
    announcement_type TEXT NOT NULL DEFAULT 'unknown',
    speaker TEXT,
    success INTEGER NOT NULL DEFAULT 1,
    error TEXT,
    latency_ms INTEGER
    -- note: legacy deployments may still have a fallback_used column.
    -- It's harmless (default 0, never written by current code) and dropped
    -- by the natural db-wipe cycle.
);

CREATE INDEX IF NOT EXISTS idx_announcement_timestamp ON announcement_history(timestamp);
CREATE INDEX IF NOT EXISTS idx_announcement_type ON announcement_history(announcement_type);

CREATE TABLE IF NOT EXISTS shopping_list (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item TEXT NOT NULL,
    list_name TEXT NOT NULL DEFAULT 'grocery',
    checked INTEGER NOT NULL DEFAULT 0,
    added_at TEXT NOT NULL,
    checked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_shopping_list_name ON shopping_list(list_name);

CREATE TABLE IF NOT EXISTS selfcare_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    detail TEXT,
    logged_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_selfcare_action ON selfcare_log(action);
CREATE INDEX IF NOT EXISTS idx_selfcare_logged_at ON selfcare_log(logged_at);

CREATE TABLE IF NOT EXISTS chat_conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_conv_updated ON chat_conversations(updated_at);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    routing TEXT,
    announcement_type TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_msg_conv ON chat_messages(conversation_id);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'other',
    tags TEXT,
    notes TEXT,
    file_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_type TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    extracted_text TEXT,
    rag_doc_id TEXT,
    uploaded_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_documents_category ON documents(category);
CREATE INDEX IF NOT EXISTS idx_documents_uploaded ON documents(uploaded_at);

CREATE TABLE IF NOT EXISTS claude_code_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT,
    project TEXT,
    turn_type TEXT NOT NULL,
    content TEXT,
    tool_uses TEXT,
    files_touched TEXT,
    commit_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_claude_code_timestamp ON claude_code_turns(timestamp);
CREATE INDEX IF NOT EXISTS idx_claude_code_session ON claude_code_turns(session_id);
CREATE INDEX IF NOT EXISTS idx_claude_code_project ON claude_code_turns(project);

CREATE TABLE IF NOT EXISTS exercises (
    name TEXT PRIMARY KEY,
    primary_muscle TEXT NOT NULL,
    secondary_muscles TEXT NOT NULL DEFAULT '[]',
    equipment TEXT NOT NULL DEFAULT 'barbell',
    is_compound INTEGER NOT NULL DEFAULT 1,
    movement_pattern TEXT NOT NULL DEFAULT 'other'
);

CREATE TABLE IF NOT EXISTS workouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    workout_type TEXT NOT NULL DEFAULT 'full_body',
    generated_by_jess INTEGER NOT NULL DEFAULT 0,
    reasoning TEXT,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_workouts_started ON workouts(started_at);

CREATE TABLE IF NOT EXISTS workout_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workout_id INTEGER NOT NULL REFERENCES workouts(id) ON DELETE CASCADE,
    exercise_name TEXT NOT NULL,
    muscle_groups TEXT NOT NULL DEFAULT '[]',
    set_number INTEGER NOT NULL,
    target_reps INTEGER,
    target_weight_lbs REAL,
    weight_lbs REAL,
    reps INTEGER,
    rpe REAL,
    completed INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_workout_sets_workout ON workout_sets(workout_id);
CREATE INDEX IF NOT EXISTS idx_workout_sets_exercise ON workout_sets(exercise_name);
CREATE INDEX IF NOT EXISTS idx_workout_sets_completed ON workout_sets(completed_at);

CREATE TABLE IF NOT EXISTS meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meal_type TEXT NOT NULL DEFAULT 'snack',
    description TEXT NOT NULL,
    calories INTEGER,
    logged_at TEXT NOT NULL,
    photo_path TEXT,
    source TEXT NOT NULL DEFAULT 'manual'
);

CREATE INDEX IF NOT EXISTS idx_meals_logged ON meals(logged_at);

CREATE TABLE IF NOT EXISTS budget_imports (
    name TEXT PRIMARY KEY,
    source_file TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    date_min TEXT,
    date_max TEXT,
    total_outflow REAL NOT NULL DEFAULT 0,
    total_inflow REAL NOT NULL DEFAULT 0,
    column_map TEXT,
    summary_doc_id TEXT
);

CREATE TABLE IF NOT EXISTS budget_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset TEXT NOT NULL,
    txn_date TEXT NOT NULL,
    amount REAL NOT NULL,
    category TEXT,
    payee TEXT,
    description TEXT,
    FOREIGN KEY (dataset) REFERENCES budget_imports(name) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_budget_txn_dataset_date ON budget_transactions(dataset, txn_date);
CREATE INDEX IF NOT EXISTS idx_budget_txn_category ON budget_transactions(dataset, category);

CREATE TABLE IF NOT EXISTS recurring_reminders (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    cron_expression TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT 'both',
    enabled INTEGER NOT NULL DEFAULT 1,
    days_of_week TEXT NOT NULL DEFAULT 'mon,tue,wed,thu,fri,sat,sun',
    last_fired_at TEXT,
    next_fire_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recurring_next_fire ON recurring_reminders(enabled, next_fire_at);

CREATE TABLE IF NOT EXISTS config_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at TEXT NOT NULL,
    panel TEXT NOT NULL,
    diff_json TEXT,
    changed_by TEXT NOT NULL DEFAULT 'user'
);

CREATE INDEX IF NOT EXISTS idx_config_changes_panel ON config_changes(panel, changed_at);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',      -- open | done | dropped
    priority TEXT NOT NULL DEFAULT 'normal',  -- low | normal | high
    source TEXT NOT NULL DEFAULT 'chat',      -- chat | voice | brain_dump
    notes TEXT,
    due_date TEXT,
    created_at TEXT NOT NULL,
    done_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_open ON tasks(status, priority, created_at);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def get_db():
    """Get a SQLite connection with row factory."""
    from orchestrator.db import get_db as _get_db

    return _get_db(DB_PATH, foreign_keys=True)


def init_db():
    """Initialize database schema."""
    from orchestrator.db import init_db as _init_db

    _init_db(DB_PATH, SCHEMA_SQL, foreign_keys=True)
    with get_db() as conn:
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
        # F-011 migration: reminders gain ack/snooze state so ntfy callbacks
        # can close the loop back to the selfcare bridge.
        _f011_cols = [
            ("ack_at", "TEXT"),
            ("acked_via", "TEXT"),
            ("snooze_count", "INTEGER NOT NULL DEFAULT 0"),
        ]
        existing_rem = {row[1] for row in conn.execute("PRAGMA table_info(reminders)").fetchall()}
        for col_name, col_def in _f011_cols:
            if col_name not in existing_rem:
                conn.execute(f"ALTER TABLE reminders ADD COLUMN {col_name} {col_def}")
                logger.info(f"[STATE] Migrated reminders: added {col_name}")
    # Seed exercises catalog (idempotent)
    try:
        from orchestrator.exercises_seed import EXERCISES

        seeded = seed_exercises(EXERCISES)
        if seeded:
            logger.info(f"[STATE] Seeded {seeded} exercises into catalog")
    except Exception as e:
        logger.warning(f"[STATE] Exercise seed failed: {e}")
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


def get_recent_reminder_outcomes(hours: int = 24, limit: int = 20) -> List[Dict[str, Any]]:
    """Terminal-state reminders (completed/missed/failed) whose transition
    happened in the last N hours, newest first.

    `completed_at` doubles as the transition timestamp for ALL terminal
    states — mark_reminder_missed/mark_reminder_failed stamp it too. Powers
    the trust layer: the dashboard delivery log and the morning
    missed-reminder recap.
    """
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM reminders
               WHERE status IN ('completed', 'missed', 'failed')
                 AND completed_at IS NOT NULL AND completed_at >= ?
               ORDER BY completed_at DESC LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def complete_reminder(reminder_id: str) -> bool:
    """Mark a reminder as completed."""
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE reminders SET status = 'completed', completed_at = ? WHERE id = ?",
            (datetime.now().isoformat(), reminder_id),
        )
        return cursor.rowcount > 0


def mark_reminder_acked(reminder_id: str, via: str) -> Optional[Dict[str, Any]]:
    """Mark a reminder acknowledged (F-011 ntfy feedback loop).

    Idempotent: if already acked, returns the existing row unchanged with
    `already_acked=True`. If unknown, returns None. Otherwise sets
    ack_at=now, acked_via=<via>, and status='completed' so the reminder
    drops out of the pending list.
    """
    now = datetime.now().isoformat()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        if row is None:
            return None
        existing = dict(row)
        if existing.get("ack_at"):
            existing["already_acked"] = True
            return existing
        conn.execute(
            """UPDATE reminders
                  SET ack_at = ?, acked_via = ?, status = 'completed', completed_at = ?
                WHERE id = ?""",
            (now, via, now, reminder_id),
        )
        existing["ack_at"] = now
        existing["acked_via"] = via
        existing["status"] = "completed"
        existing["completed_at"] = now
        existing["already_acked"] = False
        return existing


def increment_snooze_count(reminder_id: str) -> Optional[int]:
    """Bump snooze_count for a reminder and return the new value.

    Returns None if the reminder doesn't exist.
    """
    with get_db() as conn:
        row = conn.execute("SELECT snooze_count FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        if row is None:
            return None
        new_count = (row["snooze_count"] or 0) + 1
        conn.execute(
            "UPDATE reminders SET snooze_count = ? WHERE id = ?",
            (new_count, reminder_id),
        )
        return new_count


def reopen_reminder(reminder_id: str) -> bool:
    """Reset a reminder to 'pending' so a rescheduled delivery can fire again.

    Used by the snooze callback: delivery marks the reminder 'completed', and
    deliver_reminder_job skips anything non-pending — so without this reset a
    snoozed reminder would never redeliver. Also clears ack state: a Snooze
    tap after a Done tap means the user changed their mind, and the next ack
    must re-fire the selfcare bridge.
    """
    with get_db() as conn:
        cursor = conn.execute(
            """UPDATE reminders
                  SET status = 'pending', completed_at = NULL, ack_at = NULL, acked_via = NULL
                WHERE id = ?""",
            (reminder_id,),
        )
        return cursor.rowcount > 0


def mark_reminder_missed(reminder_id: str) -> bool:
    """Mark a reminder 'missed' — it came due while the orchestrator was down
    and is too stale to late-deliver. Terminal state; keeps it out of the
    pending list instead of stranding it 'pending' forever."""
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE reminders SET status = 'missed', completed_at = ? WHERE id = ?",
            (datetime.now().isoformat(), reminder_id),
        )
        return cursor.rowcount > 0


def mark_reminder_failed(reminder_id: str) -> bool:
    """Mark a reminder 'failed' — every delivery channel failed after retries.

    Terminal state so the row doesn't zombie in the pending list, but distinct
    from 'completed' so the failure stays visible in the DB and metrics.
    """
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE reminders SET status = 'failed', completed_at = ? WHERE id = ?",
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
# Tasks (durable backlog — survives restarts, unlike task_decomposition's
# in-memory step tracking). Ordered so open/high/oldest surfaces first.
# ---------------------------------------------------------------------------

# High before normal before low; within a priority, oldest first (FIFO, so
# tasks don't rot at the bottom).
_TASK_PRIORITY_RANK = "CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END"


def add_task(
    task_id: str,
    text: str,
    *,
    priority: str = "normal",
    source: str = "chat",
    notes: Optional[str] = None,
    due_date: Optional[str] = None,
) -> None:
    """Insert a task into the backlog."""
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO tasks (id, text, status, priority, source, notes, due_date, created_at)
               VALUES (?, ?, 'open', ?, ?, ?, ?, ?)""",
            (task_id, text, priority, source, notes, due_date, datetime.now().isoformat()),
        )


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    """Get a single task by id."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None


def list_tasks(status: str = "open") -> List[Dict[str, Any]]:
    """List tasks in surfacing order (high/old first). status=None returns all."""
    with get_db() as conn:
        if status is None:
            rows = conn.execute(f"SELECT * FROM tasks ORDER BY {_TASK_PRIORITY_RANK}, created_at").fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM tasks WHERE status = ? ORDER BY {_TASK_PRIORITY_RANK}, created_at",
                (status,),
            ).fetchall()
        return [dict(r) for r in rows]


def open_task_count() -> int:
    """Number of open tasks (for the pending gauge)."""
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'open'").fetchone()[0]


def complete_task(task_id: str) -> bool:
    """Mark a task done. Only transitions open→done (idempotent replay is a no-op)."""
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE tasks SET status = 'done', done_at = ? WHERE id = ? AND status = 'open'",
            (datetime.now().isoformat(), task_id),
        )
        return cursor.rowcount > 0


def drop_task(task_id: str) -> bool:
    """Drop a task (status='dropped') — no-guilt removal, kept for history."""
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE tasks SET status = 'dropped', done_at = ? WHERE id = ? AND status = 'open'",
            (datetime.now().isoformat(), task_id),
        )
        return cursor.rowcount > 0


def update_task(
    task_id: str, *, priority: Optional[str] = None, notes: Optional[str] = None, due_date: Optional[str] = None
) -> bool:
    """Update mutable fields of an open task; only provided fields change."""
    fields = {"priority": priority, "notes": notes, "due_date": due_date}
    sets = [f"{col} = ?" for col, val in fields.items() if val is not None]
    params = [val for val in fields.values() if val is not None]
    if not sets:
        return False
    params.append(task_id)
    with get_db() as conn:
        cursor = conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ? AND status = 'open'", params)
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


def set_notification_flag(key: str) -> None:
    """Set a persistent flag in the notification_tracking table."""
    mark_notified(key)


def clear_notification_flag(key: str) -> None:
    """Clear a persistent flag from the notification_tracking table."""
    with get_db() as conn:
        conn.execute("DELETE FROM notification_tracking WHERE key = ?", (key,))


# ---------------------------------------------------------------------------
# App state (generic key → value, survives restarts)
# ---------------------------------------------------------------------------


def set_app_state(key: str, value: str) -> None:
    """Set a persistent key → value pair (e.g. the evening ritual's parked item)."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_state (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, datetime.now().isoformat()),
        )


def get_app_state(key: str) -> Optional[str]:
    """Get a persistent value by key, or None if unset."""
    with get_db() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None


def delete_app_state(key: str) -> None:
    """Delete a persistent key (no-op if unset)."""
    with get_db() as conn:
        conn.execute("DELETE FROM app_state WHERE key = ?", (key,))


# ---------------------------------------------------------------------------
# Announcement History
# ---------------------------------------------------------------------------


def record_announcement(
    text: str,
    announcement_type: str = "unknown",
    speaker: Optional[str] = None,
    success: bool = True,
    error: Optional[str] = None,
    latency_ms: Optional[int] = None,
) -> None:
    """Record a TTS announcement in the history table."""
    with get_db() as conn:
        conn.execute(
            """INSERT INTO announcement_history
               (timestamp, text, announcement_type, speaker, success, error, latency_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().isoformat(),
                text[:500],  # cap text length
                announcement_type,
                speaker,
                1 if success else 0,
                error,
                latency_ms,
            ),
        )


def get_announcement_history(limit: int = 50, announcement_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get recent announcement history."""
    with get_db() as conn:
        if announcement_type:
            rows = conn.execute(
                "SELECT * FROM announcement_history WHERE announcement_type = ? ORDER BY timestamp DESC LIMIT ?",
                (announcement_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM announcement_history ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_announcement_stats() -> Dict[str, Any]:
    """Get announcement statistics."""
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        # Single aggregate pass instead of five separate table scans
        agg = conn.execute(
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(success = 1), 0) AS successes, "
            "COALESCE(SUM(success = 0), 0) AS failures, "
            "AVG(latency_ms) AS avg_latency, "
            "COALESCE(SUM(timestamp >= ?), 0) AS today_count "
            "FROM announcement_history",
            (today,),
        ).fetchone()
        total = agg["total"]
        successes = agg["successes"]
        failures = agg["failures"]
        avg_latency = agg["avg_latency"]
        today_count = agg["today_count"]

        # By type
        type_rows = conn.execute(
            "SELECT announcement_type, COUNT(*) as cnt, SUM(success) as ok "
            "FROM announcement_history GROUP BY announcement_type"
        ).fetchall()

        # By speaker
        speaker_rows = conn.execute(
            "SELECT speaker, COUNT(*) as cnt, SUM(success) as ok "
            "FROM announcement_history WHERE speaker IS NOT NULL GROUP BY speaker"
        ).fetchall()

    return {
        "total": total,
        "successes": successes,
        "failures": failures,
        "success_rate": round(successes / total * 100, 1) if total > 0 else 100.0,
        "avg_latency_ms": round(avg_latency) if avg_latency else None,
        "today_count": today_count,
        "by_type": {r["announcement_type"]: {"total": r["cnt"], "success": r["ok"]} for r in type_rows},
        "by_speaker": {r["speaker"]: {"total": r["cnt"], "success": r["ok"]} for r in speaker_rows},
    }


def cleanup_old_announcements(keep_days: int = 30) -> int:
    """Remove announcements older than keep_days."""
    cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat()
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM announcement_history WHERE timestamp < ?", (cutoff,))
        return cursor.rowcount


def cleanup_old_config_changes(keep_days: int = 180) -> int:
    """Remove config_changes audit rows older than keep_days.

    The audit table grows ~10 rows/day at typical usage (a few panel saves
    per week, each writing 1 row). 180 days gives plenty of "what did I
    change last quarter" lookback while keeping the table small.
    """
    cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat()
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM config_changes WHERE changed_at < ?", (cutoff,))
        return cursor.rowcount


def clear_announcements() -> int:
    """Delete all announcement history. Returns count of deleted rows."""
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM announcement_history")
        return cursor.rowcount


# ---------------------------------------------------------------------------
# Self-care log persistence
# ---------------------------------------------------------------------------


def save_selfcare_log(action: str, detail: Optional[str] = None) -> None:
    """Persist a self-care action (meal, medication, water, movement).

    Deduplicates: skips if the same action+detail was logged in the last 5 minutes.
    """
    cutoff = (datetime.now() - timedelta(minutes=5)).isoformat()
    truncated = detail[:200] if detail else None
    with get_db() as conn:
        existing = conn.execute(
            "SELECT 1 FROM selfcare_log WHERE action = ? AND detail IS ? AND logged_at > ? LIMIT 1",
            (action, truncated, cutoff),
        ).fetchone()
        if existing:
            return  # duplicate within 5 minutes
        conn.execute(
            "INSERT INTO selfcare_log (action, detail, logged_at) VALUES (?, ?, ?)",
            (action, truncated, datetime.now().isoformat()),
        )


def get_last_selfcare(action: str) -> Optional[datetime]:
    """Get the most recent timestamp for a given selfcare action."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT logged_at FROM selfcare_log WHERE action = ? ORDER BY logged_at DESC LIMIT 1",
            (action,),
        ).fetchone()
        if row:
            return datetime.fromisoformat(row["logged_at"])
    return None


def cleanup_old_selfcare(keep_days: int = 90) -> int:
    """Remove selfcare log entries older than keep_days."""
    cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat()
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM selfcare_log WHERE logged_at < ?", (cutoff,))
        return cursor.rowcount


def vacuum_db() -> None:
    """Run VACUUM to reclaim disk space. Should be called periodically."""
    from orchestrator.db import vacuum_db as _vacuum_db

    _vacuum_db(DB_PATH)


def get_selfcare_today(action: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get today's selfcare log entries, optionally filtered by action."""
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        if action:
            rows = conn.execute(
                "SELECT * FROM selfcare_log WHERE action = ? AND logged_at >= ? ORDER BY logged_at DESC",
                (action, today),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM selfcare_log WHERE logged_at >= ? ORDER BY logged_at DESC",
                (today,),
            ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Shopping / grocery list
# ---------------------------------------------------------------------------


def add_shopping_item(item: str, list_name: str = "grocery") -> Dict[str, Any]:
    """Add an item to a shopping list. Returns the created item."""
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO shopping_list (item, list_name, added_at) VALUES (?, ?, ?)",
            (item[:200], list_name[:50], datetime.now().isoformat()),
        )
        row = conn.execute("SELECT * FROM shopping_list WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)


def get_shopping_list(list_name: Optional[str] = None, include_checked: bool = False) -> List[Dict[str, Any]]:
    """Get shopping list items, optionally filtered by list name."""
    clauses = []
    params: list = []
    if list_name:
        clauses.append("list_name = ?")
        params.append(list_name)
    if not include_checked:
        clauses.append("checked = 0")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    order = "list_name, " if not list_name else ""
    order += "checked ASC, added_at DESC" if include_checked else "added_at DESC"
    with get_db() as conn:
        rows = conn.execute(f"SELECT * FROM shopping_list{where} ORDER BY {order}", params).fetchall()
    return [dict(r) for r in rows]


def check_shopping_item(item_id: int, checked: bool = True) -> bool:
    """Toggle checked state on a shopping list item."""
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE shopping_list SET checked = ?, checked_at = ? WHERE id = ?",
            (1 if checked else 0, datetime.now().isoformat() if checked else None, item_id),
        )
        return cursor.rowcount > 0


def remove_shopping_item(item_id: int) -> bool:
    """Delete a shopping list item."""
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM shopping_list WHERE id = ?", (item_id,))
        return cursor.rowcount > 0


def clear_checked_items(list_name: Optional[str] = None) -> int:
    """Remove all checked items, optionally from a specific list."""
    with get_db() as conn:
        if list_name:
            cursor = conn.execute("DELETE FROM shopping_list WHERE checked = 1 AND list_name = ?", (list_name,))
        else:
            cursor = conn.execute("DELETE FROM shopping_list WHERE checked = 1")
        return cursor.rowcount


# ---------------------------------------------------------------------------
# Chat Conversations
# ---------------------------------------------------------------------------


def create_conversation(conv_id: str, title: str) -> Dict[str, Any]:
    """Create a new chat conversation."""
    now = datetime.now().isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO chat_conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (conv_id, title, now, now),
        )
    return {"id": conv_id, "title": title, "created_at": now, "updated_at": now}


def list_conversations(limit: int = 50) -> List[Dict[str, Any]]:
    """List conversations ordered by most recently updated."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM chat_conversations ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_conversation(conv_id: str) -> Optional[Dict[str, Any]]:
    """Get a single conversation by ID."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM chat_conversations WHERE id = ?", (conv_id,)).fetchone()
        return dict(row) if row else None


def update_conversation_title(conv_id: str, title: str) -> bool:
    """Update a conversation's title."""
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE chat_conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title, datetime.now().isoformat(), conv_id),
        )
        return cursor.rowcount > 0


def delete_conversation(conv_id: str) -> bool:
    """Delete a conversation and its messages."""
    with get_db() as conn:
        conn.execute("DELETE FROM chat_messages WHERE conversation_id = ?", (conv_id,))
        cursor = conn.execute("DELETE FROM chat_conversations WHERE id = ?", (conv_id,))
        return cursor.rowcount > 0


def save_chat_message(
    conv_id: str, role: str, content: str, routing: Optional[str] = None, announcement_type: Optional[str] = None
) -> Dict[str, Any]:
    """Save a message to a conversation and bump updated_at."""
    now = datetime.now().isoformat()
    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO chat_messages (conversation_id, role, content, routing, announcement_type, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (conv_id, role, content, routing, announcement_type, now),
        )
        conn.execute("UPDATE chat_conversations SET updated_at = ? WHERE id = ?", (now, conv_id))
        return {"id": cursor.lastrowid, "conversation_id": conv_id, "role": role, "content": content, "created_at": now}


def get_conversation_messages(conv_id: str) -> List[Dict[str, Any]]:
    """Get all messages in a conversation."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_messages WHERE conversation_id = ? ORDER BY created_at", (conv_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Document Vault
# ---------------------------------------------------------------------------


def save_document(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Save a document record."""
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO documents
               (id, title, category, tags, notes, file_name, file_path, file_type, file_size,
                extracted_text, rag_doc_id, uploaded_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                doc["id"],
                doc["title"],
                doc["category"],
                doc.get("tags", ""),
                doc.get("notes", ""),
                doc["file_name"],
                doc["file_path"],
                doc["file_type"],
                doc["file_size"],
                doc.get("extracted_text"),
                doc.get("rag_doc_id"),
                doc["uploaded_at"],
                doc["updated_at"],
            ),
        )
    return doc


def get_document(doc_id: str) -> Optional[Dict[str, Any]]:
    """Get a single document by ID."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return dict(row) if row else None


def list_documents(
    category: Optional[str] = None, search: Optional[str] = None, limit: int = 50, offset: int = 0
) -> List[Dict[str, Any]]:
    """List documents with optional category filter and text search."""
    query = "SELECT * FROM documents WHERE 1=1"
    params: list = []
    if category:
        query += " AND category = ?"
        params.append(category)
    if search:
        query += " AND (title LIKE ? OR tags LIKE ? OR notes LIKE ?)"
        term = f"%{search}%"
        params.extend([term, term, term])
    query += " ORDER BY uploaded_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def update_document(doc_id: str, updates: Dict[str, Any]) -> bool:
    """Update document metadata (title, category, tags, notes)."""
    allowed = {"title", "category", "tags", "notes"}
    fields = {k: v for k, v in updates.items() if k in allowed}
    if not fields:
        return False
    fields["updated_at"] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [doc_id]
    with get_db() as conn:
        cursor = conn.execute(f"UPDATE documents SET {set_clause} WHERE id = ?", values)  # noqa: S608
        return cursor.rowcount > 0


def delete_document(doc_id: str) -> Optional[Dict[str, Any]]:
    """Delete a document, returning it first for file cleanup."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if not row:
            return None
        doc = dict(row)
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        return doc


def get_document_categories() -> List[Dict[str, Any]]:
    """Get document counts per category."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT category, COUNT(*) as count FROM documents GROUP BY category ORDER BY count DESC"
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Claude Code turn tracking
# ---------------------------------------------------------------------------


def log_claude_code_turn(turn: Dict[str, Any]) -> int:
    """Record a Claude Code turn (from Stop hook or session miner)."""
    import json as _json

    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO claude_code_turns
               (timestamp, session_id, project, turn_type, content, tool_uses, files_touched, commit_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                turn.get("timestamp") or datetime.now().isoformat(),
                turn.get("session_id", ""),
                turn.get("project", ""),
                turn.get("turn_type", "assistant"),
                (turn.get("content") or "")[:10000],  # cap to 10k chars
                _json.dumps(turn.get("tool_uses", [])),
                _json.dumps(turn.get("files_touched", [])),
                turn.get("commit_hash", ""),
            ),
        )
        return cursor.lastrowid


def get_claude_code_turns(
    since_minutes: int = 60,
    limit: int = 50,
    project: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Retrieve recent Claude Code turns from the rolling buffer."""
    import json as _json

    cutoff = (datetime.now() - timedelta(minutes=since_minutes)).isoformat()

    sql = "SELECT * FROM claude_code_turns WHERE timestamp >= ?"
    params: List[Any] = [cutoff]
    if project:
        sql += " AND project = ?"
        params.append(project)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()

    turns = []
    for row in rows:
        d = dict(row)
        # Decode JSON fields
        for field in ("tool_uses", "files_touched"):
            try:
                d[field] = _json.loads(d[field]) if d[field] else []
            except (ValueError, TypeError):
                d[field] = []
        turns.append(d)
    return turns


def get_claude_code_files_touched(since_minutes: int = 60, project: Optional[str] = None) -> List[str]:
    """Return unique file paths touched by Claude Code in the time window."""
    turns = get_claude_code_turns(since_minutes=since_minutes, limit=200, project=project)
    seen = set()
    files = []
    for turn in turns:
        for f in turn.get("files_touched", []):
            if f and f not in seen:
                seen.add(f)
                files.append(f)
    return files


def cleanup_old_claude_code_turns(days: int = 7) -> int:
    """Delete turns older than `days`. Returns count deleted."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with get_db() as conn:
        cursor = conn.execute(
            "DELETE FROM claude_code_turns WHERE timestamp < ?",
            (cutoff,),
        )
        return cursor.rowcount


# ---------------------------------------------------------------------------
# Exercises catalog (seeded on init)
# ---------------------------------------------------------------------------


def seed_exercises(exercises: List[Dict[str, Any]]) -> int:
    """Insert exercises that don't already exist. Returns count inserted."""
    import json as _json

    with get_db() as conn:
        before = conn.total_changes
        # name is the PRIMARY KEY — INSERT OR IGNORE replaces the previous
        # one-SELECT-per-exercise existence check
        conn.executemany(
            """INSERT OR IGNORE INTO exercises
               (name, primary_muscle, secondary_muscles, equipment, is_compound, movement_pattern)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (
                    ex["name"],
                    ex["primary_muscle"],
                    _json.dumps(ex.get("secondary_muscles", [])),
                    ex.get("equipment", "barbell"),
                    1 if ex.get("is_compound", True) else 0,
                    ex.get("movement_pattern", "other"),
                )
                for ex in exercises
            ],
        )
        return conn.total_changes - before


def get_exercises(
    movement_pattern: Optional[str] = None,
    equipment: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Get exercises from the catalog, optionally filtered."""
    import json as _json

    query = "SELECT * FROM exercises WHERE 1=1"
    params: list = []
    if movement_pattern:
        query += " AND movement_pattern = ?"
        params.append(movement_pattern)
    if equipment:
        query += " AND equipment = ?"
        params.append(equipment)
    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["secondary_muscles"] = _json.loads(d["secondary_muscles"] or "[]")
        except (ValueError, TypeError):
            d["secondary_muscles"] = []
        d["is_compound"] = bool(d["is_compound"])
        out.append(d)
    return out


def get_exercise(name: str) -> Optional[Dict[str, Any]]:
    """Get a single exercise by name."""
    import json as _json

    with get_db() as conn:
        row = conn.execute("SELECT * FROM exercises WHERE name = ?", (name,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["secondary_muscles"] = _json.loads(d["secondary_muscles"] or "[]")
    except (ValueError, TypeError):
        d["secondary_muscles"] = []
    d["is_compound"] = bool(d["is_compound"])
    return d


# ---------------------------------------------------------------------------
# Workouts
# ---------------------------------------------------------------------------


def create_workout(
    workout_type: str,
    generated_by_jess: bool,
    reasoning: Optional[str] = None,
) -> int:
    """Create a new workout row. Returns the workout id."""
    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO workouts (started_at, workout_type, generated_by_jess, reasoning)
               VALUES (?, ?, ?, ?)""",
            (datetime.now().isoformat(), workout_type, 1 if generated_by_jess else 0, reasoning),
        )
        return cursor.lastrowid


def add_planned_set(
    workout_id: int,
    exercise_name: str,
    muscle_groups: List[str],
    set_number: int,
    target_reps: Optional[int],
    target_weight_lbs: Optional[float],
) -> int:
    """Add a planned (uncompleted) set to a workout."""
    import json as _json

    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO workout_sets
               (workout_id, exercise_name, muscle_groups, set_number, target_reps, target_weight_lbs)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                workout_id,
                exercise_name,
                _json.dumps(muscle_groups or []),
                set_number,
                target_reps,
                target_weight_lbs,
            ),
        )
        return cursor.lastrowid


def log_completed_set(
    workout_id: int,
    exercise_name: str,
    weight_lbs: float,
    reps: int,
    rpe: Optional[float] = None,
    set_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Record a completed set.

    If set_id is provided, updates that planned set; otherwise inserts a new one
    after looking up the exercise's muscle groups.
    """
    import json as _json

    now = datetime.now().isoformat()
    with get_db() as conn:
        if set_id:
            conn.execute(
                """UPDATE workout_sets
                   SET weight_lbs = ?, reps = ?, rpe = ?, completed = 1, completed_at = ?
                   WHERE id = ?""",
                (weight_lbs, reps, rpe, now, set_id),
            )
            row = conn.execute("SELECT * FROM workout_sets WHERE id = ?", (set_id,)).fetchone()
        else:
            # Look up muscle groups for this exercise
            ex_row = conn.execute(
                "SELECT primary_muscle, secondary_muscles FROM exercises WHERE name = ?",
                (exercise_name,),
            ).fetchone()
            if ex_row:
                try:
                    secondary = _json.loads(ex_row["secondary_muscles"] or "[]")
                except (ValueError, TypeError):
                    secondary = []
                muscle_groups = [ex_row["primary_muscle"]] + secondary
            else:
                muscle_groups = []
            # Determine next set_number for this exercise in this workout
            row_n = conn.execute(
                "SELECT COALESCE(MAX(set_number), 0) + 1 AS next_n FROM workout_sets WHERE workout_id = ? AND exercise_name = ?",
                (workout_id, exercise_name),
            ).fetchone()
            next_n = row_n["next_n"] if row_n else 1
            cursor = conn.execute(
                """INSERT INTO workout_sets
                   (workout_id, exercise_name, muscle_groups, set_number,
                    weight_lbs, reps, rpe, completed, completed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (
                    workout_id,
                    exercise_name,
                    _json.dumps(muscle_groups),
                    next_n,
                    weight_lbs,
                    reps,
                    rpe,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM workout_sets WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return _deserialize_set(dict(row)) if row else {}


def _deserialize_set(d: Dict[str, Any]) -> Dict[str, Any]:
    import json as _json

    try:
        d["muscle_groups"] = _json.loads(d.get("muscle_groups") or "[]")
    except (ValueError, TypeError):
        d["muscle_groups"] = []
    d["completed"] = bool(d.get("completed"))
    return d


def get_workout(workout_id: int) -> Optional[Dict[str, Any]]:
    """Get a workout + its sets."""
    with get_db() as conn:
        w_row = conn.execute("SELECT * FROM workouts WHERE id = ?", (workout_id,)).fetchone()
        if not w_row:
            return None
        workout = dict(w_row)
        workout["generated_by_jess"] = bool(workout["generated_by_jess"])
        set_rows = conn.execute(
            "SELECT * FROM workout_sets WHERE workout_id = ? ORDER BY exercise_name, set_number",
            (workout_id,),
        ).fetchall()
        workout["sets"] = [_deserialize_set(dict(r)) for r in set_rows]
    return workout


def get_todays_workout() -> Optional[Dict[str, Any]]:
    """Return the most recent workout from today, if any."""
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM workouts WHERE started_at >= ? ORDER BY started_at DESC LIMIT 1",
            (today,),
        ).fetchone()
    return get_workout(row["id"]) if row else None


def get_recent_workouts(days: int = 7, limit: int = 20) -> List[Dict[str, Any]]:
    """Return workouts from the last N days, each with their sets."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id FROM workouts WHERE started_at >= ? ORDER BY started_at DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
    return [w for r in rows if (w := get_workout(r["id"]))]


def get_recent_muscle_groups(days: int = 3) -> Dict[str, int]:
    """Return muscle-group -> set count from completed sets in the last N days."""
    import json as _json

    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    counts: Dict[str, int] = {}
    with get_db() as conn:
        rows = conn.execute(
            "SELECT muscle_groups FROM workout_sets WHERE completed = 1 AND completed_at >= ?",
            (cutoff,),
        ).fetchall()
    for r in rows:
        try:
            groups = _json.loads(r["muscle_groups"] or "[]")
        except (ValueError, TypeError):
            groups = []
        for g in groups:
            counts[g] = counts.get(g, 0) + 1
    return counts


def count_workouts_in_window(days: int) -> int:
    """Count workouts started in the last N days."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM workouts WHERE started_at >= ?",
            (cutoff,),
        ).fetchone()
    return row["n"] if row else 0


def days_since_last_workout() -> Optional[int]:
    """Days since the most recent workout, or None if none exist."""
    with get_db() as conn:
        row = conn.execute("SELECT started_at FROM workouts ORDER BY started_at DESC LIMIT 1").fetchone()
    if not row:
        return None
    last = datetime.fromisoformat(row["started_at"])
    return (datetime.now() - last).days


def get_exercise_prs(exercise_name: str) -> Optional[Dict[str, Any]]:
    """Return best weight×reps for an exercise (completed sets only)."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT weight_lbs, reps, completed_at
               FROM workout_sets
               WHERE exercise_name = ? AND completed = 1 AND weight_lbs IS NOT NULL
               ORDER BY weight_lbs DESC, reps DESC LIMIT 1""",
            (exercise_name,),
        ).fetchone()
    return dict(row) if row else None


def delete_workout_set(set_id: int) -> bool:
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM workout_sets WHERE id = ?", (set_id,))
        return cursor.rowcount > 0


def delete_workout(workout_id: int) -> bool:
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM workouts WHERE id = ?", (workout_id,))
        return cursor.rowcount > 0


def end_workout(workout_id: int, notes: Optional[str] = None) -> bool:
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE workouts SET ended_at = ?, notes = COALESCE(?, notes) WHERE id = ?",
            (datetime.now().isoformat(), notes, workout_id),
        )
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Meals
# ---------------------------------------------------------------------------


def add_meal(
    description: str,
    meal_type: str = "snack",
    calories: Optional[int] = None,
    photo_path: Optional[str] = None,
    source: str = "manual",
) -> Dict[str, Any]:
    """Insert a meal. Returns the created row."""
    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO meals (meal_type, description, calories, logged_at, photo_path, source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                meal_type[:30],
                description[:500],
                calories,
                datetime.now().isoformat(),
                photo_path,
                source[:20],
            ),
        )
        row = conn.execute("SELECT * FROM meals WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row) if row else {}


def update_meal(meal_id: int, updates: Dict[str, Any]) -> bool:
    # photo_path intentionally NOT in allowlist — it's set only by the
    # /api/meals/photo upload route, never by user PATCH.
    allowed = {"description", "meal_type", "calories"}
    fields = {k: v for k, v in updates.items() if k in allowed}
    if not fields:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [meal_id]
    with get_db() as conn:
        cursor = conn.execute(f"UPDATE meals SET {set_clause} WHERE id = ?", values)  # noqa: S608
        return cursor.rowcount > 0


def delete_meal(meal_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM meals WHERE id = ?", (meal_id,)).fetchone()
        if not row:
            return None
        meal = dict(row)
        conn.execute("DELETE FROM meals WHERE id = ?", (meal_id,))
    return meal


def get_meals_today() -> List[Dict[str, Any]]:
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM meals WHERE logged_at >= ? ORDER BY logged_at ASC",
            (today,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_meals_recent(days: int = 7) -> List[Dict[str, Any]]:
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM meals WHERE logged_at >= ? ORDER BY logged_at DESC",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_meal(meal_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM meals WHERE id = ?", (meal_id,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Budget imports (historical CSV/Excel budget data)
# ---------------------------------------------------------------------------


def save_budget_import(meta: Dict[str, Any]) -> None:
    """Upsert a budget_imports metadata row.

    Uses ON CONFLICT DO UPDATE (not INSERT OR REPLACE) so that updating an
    existing row does NOT trigger ON DELETE CASCADE on budget_transactions.
    INSERT OR REPLACE would silently wipe the child transactions we just
    inserted during the import flow — see 2026-04-18 debugging session.
    """
    import json as _json

    with get_db() as conn:
        conn.execute(
            """INSERT INTO budget_imports
               (name, source_file, imported_at, row_count, date_min, date_max,
                total_outflow, total_inflow, column_map, summary_doc_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   source_file = excluded.source_file,
                   imported_at = excluded.imported_at,
                   row_count = excluded.row_count,
                   date_min = excluded.date_min,
                   date_max = excluded.date_max,
                   total_outflow = excluded.total_outflow,
                   total_inflow = excluded.total_inflow,
                   column_map = excluded.column_map,
                   summary_doc_id = excluded.summary_doc_id""",
            (
                meta["name"],
                meta["source_file"],
                meta.get("imported_at") or datetime.now().isoformat(),
                int(meta["row_count"]),
                meta.get("date_min"),
                meta.get("date_max"),
                float(meta.get("total_outflow", 0)),
                float(meta.get("total_inflow", 0)),
                _json.dumps(meta.get("column_map") or {}),
                meta.get("summary_doc_id"),
            ),
        )


def list_budget_imports() -> List[Dict[str, Any]]:
    """List all budget imports ordered by most recent."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM budget_imports ORDER BY imported_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_budget_import(name: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM budget_imports WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def delete_budget_import(name: str) -> int:
    """Delete a budget import + all its transactions (via FK cascade)."""
    with get_db() as conn:
        cur = conn.execute("DELETE FROM budget_imports WHERE name = ?", (name,))
        return cur.rowcount


def save_budget_transactions(dataset: str, rows: List[Dict[str, Any]]) -> Dict[str, int]:
    """Bulk insert budget transactions with multiset-aware dedup.

    A SQL UNIQUE index doesn't work here because YNAB datasets legitimately
    contain repeat rows (daily $6 coffee from the same payee with empty
    memo). Dedup on the MULTISET: count each existing key, only insert rows
    whose incoming count exceeds existing count. A first import of 10
    identical Starbucks rows keeps all 10; a second --append of the same
    file doesn't double them to 20.
    """
    if not rows:
        return {"attempted": 0, "inserted": 0, "skipped_duplicates": 0}

    def _key(date, amount, category, payee, description):
        return (date, round(float(amount), 2), category or "", payee or "", description or "")

    with get_db() as conn:
        existing_counts: Dict[tuple, int] = {}
        for r in conn.execute(
            "SELECT txn_date, amount, category, payee, description FROM budget_transactions WHERE dataset = ?",
            (dataset,),
        ).fetchall():
            k = _key(r["txn_date"], r["amount"], r["category"], r["payee"], r["description"])
            existing_counts[k] = existing_counts.get(k, 0) + 1

        incoming_seen: Dict[tuple, int] = {}
        to_insert: List[tuple] = []
        skipped = 0
        for r in rows:
            k = _key(r["txn_date"], r["amount"], r.get("category"), r.get("payee"), r.get("description"))
            incoming_seen[k] = incoming_seen.get(k, 0) + 1
            if incoming_seen[k] <= existing_counts.get(k, 0):
                skipped += 1
                continue
            to_insert.append(
                (
                    dataset,
                    r["txn_date"],
                    float(r["amount"]),
                    r.get("category"),
                    r.get("payee"),
                    r.get("description"),
                )
            )

        if to_insert:
            conn.executemany(
                """INSERT INTO budget_transactions
                   (dataset, txn_date, amount, category, payee, description)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                to_insert,
            )

    return {
        "attempted": len(rows),
        "inserted": len(to_insert),
        "skipped_duplicates": skipped,
    }


def clear_budget_transactions(dataset: str) -> int:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM budget_transactions WHERE dataset = ?", (dataset,))
        return cur.rowcount


def query_budget_transactions(
    dataset: str,
    group_by: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    category: Optional[str] = None,
    category_contains: Optional[str] = None,
    payee_contains: Optional[str] = None,
    amount_sign: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Run a bounded aggregation query against budget_transactions.

    group_by: None -> list raw transactions (sorted by date desc, limit applied).
              'category' | 'payee' | 'month' -> group + sum + count + return top N by abs(total).
    category: EXACT (case-insensitive) match. Use only when you know the literal
              category string. YNAB users often prefix with emoji + spaces
              ('🎮 🎲 Gaming'), which will NOT match category='Gaming' — use
              category_contains instead for those.
    category_contains: substring (case-insensitive) match — the right default
              when the caller only knows a keyword ('gaming', 'food', 'gas').
    amount_sign: 'outflow' (amount < 0), 'inflow' (amount > 0), or None (both).
    """
    where = ["dataset = ?"]
    params: list = [dataset]
    if start_date:
        where.append("txn_date >= ?")
        params.append(start_date)
    if end_date:
        where.append("txn_date <= ?")
        params.append(end_date)
    if category:
        where.append("LOWER(COALESCE(category,'')) = LOWER(?)")
        params.append(category)
    if category_contains:
        where.append("LOWER(COALESCE(category,'')) LIKE LOWER(?)")
        params.append(f"%{category_contains}%")
    if payee_contains:
        where.append("LOWER(COALESCE(payee,'')) LIKE LOWER(?)")
        params.append(f"%{payee_contains}%")
    if amount_sign == "outflow":
        where.append("amount < 0")
    elif amount_sign == "inflow":
        where.append("amount > 0")
    where_sql = " AND ".join(where)

    if group_by is None:
        sql = (
            f"SELECT txn_date, amount, category, payee, description "
            f"FROM budget_transactions WHERE {where_sql} "
            f"ORDER BY txn_date DESC LIMIT ?"
        )
        params.append(int(limit))
        with get_db() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    if group_by == "category":
        group_expr = "COALESCE(NULLIF(category, ''), '(uncategorized)')"
    elif group_by == "payee":
        group_expr = "COALESCE(NULLIF(payee, ''), '(unknown payee)')"
    elif group_by == "month":
        group_expr = "substr(txn_date, 1, 7)"
    else:
        raise ValueError(f"Unsupported group_by: {group_by}")

    sql = (
        f"SELECT {group_expr} AS group_key, "
        f"SUM(amount) AS total, COUNT(*) AS count, "
        f"SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END) AS outflow, "
        f"SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) AS inflow "
        f"FROM budget_transactions WHERE {where_sql} "
        f"GROUP BY group_key "
        f"ORDER BY ABS(SUM(amount)) DESC LIMIT ?"
    )
    params.append(int(limit))
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def query_budget_outliers(
    dataset: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    n_std: float = 2.0,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Find transactions with |amount| more than n_std stddev from the mean
    of outflow magnitudes in the dataset (optionally date-filtered)."""
    where = ["dataset = ?", "amount < 0"]
    params: list = [dataset]
    if start_date:
        where.append("txn_date >= ?")
        params.append(start_date)
    if end_date:
        where.append("txn_date <= ?")
        params.append(end_date)
    where_sql = " AND ".join(where)
    with get_db() as conn:
        stats = conn.execute(
            f"SELECT AVG(ABS(amount)) AS mean, "
            f"AVG(amount*amount) - AVG(amount)*AVG(amount) AS var "
            f"FROM budget_transactions WHERE {where_sql}",
            params,
        ).fetchone()
        if not stats or stats["mean"] is None or stats["var"] is None or stats["var"] <= 0:
            return []
        import math

        threshold = abs(stats["mean"]) + n_std * math.sqrt(stats["var"])
        rows = conn.execute(
            f"SELECT txn_date, amount, category, payee, description FROM budget_transactions "
            f"WHERE {where_sql} AND ABS(amount) >= ? "
            f"ORDER BY ABS(amount) DESC LIMIT ?",
            params + [threshold, int(limit)],
        ).fetchall()
    return [dict(r) for r in rows]
