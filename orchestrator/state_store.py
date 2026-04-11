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
"""


def get_db():
    """Get a SQLite connection with row factory."""
    from db import get_db as _get_db

    return _get_db(DB_PATH, foreign_keys=True)


def init_db():
    """Initialize database schema."""
    from db import init_db as _init_db

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


def set_notification_flag(key: str) -> None:
    """Set a persistent flag in the notification_tracking table."""
    mark_notified(key)


def clear_notification_flag(key: str) -> None:
    """Clear a persistent flag from the notification_tracking table."""
    with get_db() as conn:
        conn.execute("DELETE FROM notification_tracking WHERE key = ?", (key,))


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
    with get_db() as conn:
        # Total counts
        total = conn.execute("SELECT COUNT(*) FROM announcement_history").fetchone()[0]
        successes = conn.execute("SELECT COUNT(*) FROM announcement_history WHERE success = 1").fetchone()[0]
        failures = conn.execute("SELECT COUNT(*) FROM announcement_history WHERE success = 0").fetchone()[0]

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

        # Average latency
        avg_latency = conn.execute(
            "SELECT AVG(latency_ms) FROM announcement_history WHERE latency_ms IS NOT NULL"
        ).fetchone()[0]

        # Today's count
        today = datetime.now().strftime("%Y-%m-%d")
        today_count = conn.execute(
            "SELECT COUNT(*) FROM announcement_history WHERE timestamp >= ?",
            (today,),
        ).fetchone()[0]

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
    from db import vacuum_db as _vacuum_db

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
