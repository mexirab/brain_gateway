"""
Dopamine-Aware Progress Tracking (F-005).

SQLite-backed daily stats, streaks, and personal best detection.
Provides record_event() for other managers, daily/weekly summaries,
and API data for the frontend dashboard.
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("PROGRESS_DB_PATH", "/app/data/progress.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    tasks_completed INTEGER NOT NULL DEFAULT 0,
    brain_dumps INTEGER NOT NULL DEFAULT 0,
    focus_sessions INTEGER NOT NULL DEFAULT 0,
    focus_minutes INTEGER NOT NULL DEFAULT 0,
    reminders_done INTEGER NOT NULL DEFAULT 0,
    routine_steps INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS streaks (
    category TEXT PRIMARY KEY,
    current_streak INTEGER NOT NULL DEFAULT 0,
    longest_streak INTEGER NOT NULL DEFAULT 0,
    last_active_date TEXT,
    updated_at TEXT NOT NULL
);
"""

# Streak milestone thresholds that trigger TTS announcements
STREAK_MILESTONES = [3, 5, 7, 14, 30]

# Map event_type -> column in daily_stats to increment
_EVENT_COLUMN_MAP = {
    "task_done": "tasks_completed",
    "brain_dump": "brain_dumps",
    "focus_complete": "focus_sessions",
    "focus_partial": None,  # only adds focus_minutes
    "reminder_done": "reminders_done",
    "routine_done": "routine_steps",  # F-006
}

# Map event_type -> streak category (None = no streak tracking)
_EVENT_STREAK_MAP = {
    "task_done": "task",
    "brain_dump": "brain_dump",
    "focus_complete": "focus",
    "focus_partial": None,
    "reminder_done": None,
    "routine_done": "routine",  # F-006
}

# TTS encouragements for daily summaries
_ENCOURAGEMENTS = [
    "Solid day.",
    "Momentum building.",
    "Consistent beats intense.",
    "Good stuff.",
    "That's progress.",
]


@contextmanager
def get_db():
    """Get a SQLite connection with row factory."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Initialize progress database schema."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.executescript(SCHEMA_SQL)
    logger.info(f"[PROGRESS] Database initialized at {DB_PATH}")


def record_event(event_type: str, metadata: Optional[Dict[str, Any]] = None) -> None:
    """Record a progress event. Called synchronously by managers.

    Args:
        event_type: One of task_done, brain_dump, focus_complete, focus_partial,
                    reminder_done, routine_done.
        metadata: Optional dict. focus_complete uses {"minutes": int, "sprints": int}.
                  focus_partial uses {"minutes": int}.
    """
    if metadata is None:
        metadata = {}

    column = _EVENT_COLUMN_MAP.get(event_type)
    if event_type not in _EVENT_COLUMN_MAP:
        logger.warning(f"[PROGRESS] Unknown event type: {event_type}")
        return

    today = date.today().isoformat()
    now = datetime.now().isoformat()

    try:
        with get_db() as conn:
            # Ensure row exists for today
            conn.execute(
                "INSERT OR IGNORE INTO daily_stats (date, updated_at) VALUES (?, ?)",
                (today, now),
            )

            # Increment the primary counter column
            if column:
                conn.execute(
                    f"UPDATE daily_stats SET {column} = {column} + 1, updated_at = ? WHERE date = ?",
                    (now, today),
                )

            # Add focus minutes for focus events
            if event_type in ("focus_complete", "focus_partial"):
                minutes = metadata.get("minutes", 0)
                if minutes > 0:
                    conn.execute(
                        "UPDATE daily_stats SET focus_minutes = focus_minutes + ?, updated_at = ? WHERE date = ?",
                        (minutes, now, today),
                    )

            # Update streak
            streak_category = _EVENT_STREAK_MAP.get(event_type)
            if streak_category:
                _update_streak(conn, streak_category, today, now)

        # Increment Prometheus metric (deferred import to avoid circular)
        try:
            from metrics import PROGRESS_EVENTS_RECORDED

            PROGRESS_EVENTS_RECORDED.labels(event_type=event_type).inc()
        except Exception:
            pass  # metrics not critical

    except Exception as e:
        logger.error(f"[PROGRESS] Failed to record event {event_type}: {e}")


def _update_streak(conn: sqlite3.Connection, category: str, today: str, now: str) -> None:
    """Update streak for a category within an existing transaction."""
    row = conn.execute("SELECT * FROM streaks WHERE category = ?", (category,)).fetchone()

    if not row:
        conn.execute(
            "INSERT INTO streaks (category, current_streak, longest_streak, last_active_date, updated_at) "
            "VALUES (?, 1, 1, ?, ?)",
            (category, today, now),
        )
        return

    last_active = row["last_active_date"]

    if last_active == today:
        return  # Already counted today

    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()

    if last_active == yesterday:
        new_streak = row["current_streak"] + 1
    else:
        new_streak = 1  # Streak broken

    longest = max(row["longest_streak"], new_streak)

    conn.execute(
        "UPDATE streaks SET current_streak = ?, longest_streak = ?, "
        "last_active_date = ?, updated_at = ? WHERE category = ?",
        (new_streak, longest, today, now, category),
    )


async def check_and_announce_streaks() -> None:
    """Check for streak milestones and announce via TTS. Fire-and-forget."""
    try:
        import state_store

        today = date.today().isoformat()

        with get_db() as conn:
            rows = conn.execute("SELECT * FROM streaks WHERE last_active_date = ?", (today,)).fetchall()

        for row in rows:
            category = row["category"]
            current = row["current_streak"]

            if current not in STREAK_MILESTONES:
                continue

            notif_key = f"streak:{category}:{current}:{today}"
            if state_store.is_notified(notif_key):
                continue

            # Build friendly category name
            friendly = category.replace("_", " ")
            message = f"{friendly} streak: {current} days in a row!"

            try:
                from reminder_manager import _announce_voice

                await _announce_voice(message)
                state_store.mark_notified(notif_key)
                logger.info(f"[PROGRESS] Streak milestone announced: {category} = {current}")

                try:
                    from metrics import PROGRESS_STREAK_MILESTONES

                    PROGRESS_STREAK_MILESTONES.labels(category=category).inc()
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"[PROGRESS] Failed to announce streak {category}: {e}")

    except Exception as e:
        logger.error(f"[PROGRESS] Error checking streaks: {e}")


async def daily_summary() -> str:
    """Build a TTS-friendly daily summary."""
    import random

    today_weekday = date.today().strftime("%A")

    stats = get_today_stats()

    parts = []
    tasks = stats.get("tasks_completed", 0)
    brain_dumps = stats.get("brain_dumps", 0)
    focus_sessions = stats.get("focus_sessions", 0)
    focus_minutes = stats.get("focus_minutes", 0)

    if tasks + brain_dumps + focus_sessions == 0:
        return "Daily recap: quiet day today. Tomorrow's a fresh start."

    if tasks > 0:
        parts.append(f"completed {tasks} task{'s' if tasks != 1 else ''}")
    if focus_sessions > 0:
        parts.append(
            f"worked {focus_sessions} focus session{'s' if focus_sessions != 1 else ''} "
            f"totaling {focus_minutes} minutes"
        )
    if brain_dumps > 0:
        parts.append(f"captured {brain_dumps} brain dump{'s' if brain_dumps != 1 else ''}")

    summary = f"Daily recap: you {', '.join(parts)}."

    # Check for personal best
    best = _personal_best_check("tasks_completed", tasks, today_weekday)
    if best:
        summary += f" {best}"
    else:
        summary += f" {random.choice(_ENCOURAGEMENTS)}"

    return summary


async def weekly_summary() -> str:
    """Build a TTS-friendly weekly summary."""
    week_data = get_week_stats()
    totals = week_data["totals"]
    trend = week_data["trend"]
    best_day = week_data["best_day"]

    parts = []
    if totals["tasks_completed"] > 0:
        parts.append(f"{totals['tasks_completed']} tasks done")
    if totals["focus_sessions"] > 0:
        parts.append(f"{totals['focus_sessions']} focus sessions")
    if totals["brain_dumps"] > 0:
        parts.append(f"{totals['brain_dumps']} brain dumps")

    if not parts:
        return "Your week: pretty quiet. A new week is a fresh start."

    summary = f"Your week: {', '.join(parts)}."

    if best_day:
        day_name = date.fromisoformat(best_day).strftime("%A")
        summary += f" {day_name} was your most productive day."

    if trend == "up":
        summary += " Trending up from last week."
    elif trend == "down":
        summary += " Down a bit from last week, but that's okay."
    else:
        summary += " Holding steady from last week."

    return summary


_BESTABLE_COLUMNS = {"tasks_completed", "brain_dumps", "focus_sessions", "focus_minutes"}


def _personal_best_check(stat: str, value: int, day_of_week: str) -> Optional[str]:
    """Compare against historical same-day-of-week average. Return text or None."""
    if value <= 0:
        return None

    if stat not in _BESTABLE_COLUMNS:
        return None

    try:
        # strftime('%w') gives 0=Sunday ... 6=Saturday
        weekday_num = date.today().strftime("%w")
        four_weeks_ago = (date.today() - timedelta(days=28)).isoformat()

        with get_db() as conn:
            row = conn.execute(
                f"SELECT AVG({stat}) as avg_val, COUNT(*) as cnt FROM daily_stats "
                f"WHERE strftime('%w', date) = ? AND date >= ? AND date < ?",
                (weekday_num, four_weeks_ago, date.today().isoformat()),
            ).fetchone()

        if not row or row["cnt"] == 0:
            return None

        avg_val = row["avg_val"] or 0
        if avg_val > 0 and value > avg_val * 1.1:
            return f"That's your best {day_of_week} this month."

    except Exception as e:
        logger.warning(f"[PROGRESS] Personal best check failed: {e}")

    return None


def get_today_stats() -> Dict[str, Any]:
    """Get today's progress stats for the API."""
    today = date.today().isoformat()

    with get_db() as conn:
        row = conn.execute("SELECT * FROM daily_stats WHERE date = ?", (today,)).fetchone()

    if not row:
        return {
            "date": today,
            "tasks_completed": 0,
            "brain_dumps": 0,
            "focus_sessions": 0,
            "focus_minutes": 0,
            "reminders_done": 0,
            "routine_steps": 0,
        }

    return {
        "date": row["date"],
        "tasks_completed": row["tasks_completed"],
        "brain_dumps": row["brain_dumps"],
        "focus_sessions": row["focus_sessions"],
        "focus_minutes": row["focus_minutes"],
        "reminders_done": row["reminders_done"],
        "routine_steps": row["routine_steps"],
    }


def get_week_stats() -> Dict[str, Any]:
    """Get this week's stats, prior week for comparison, and trend."""
    today = date.today()
    week_start = today - timedelta(days=6)
    prior_week_start = week_start - timedelta(days=7)
    prior_week_end = week_start - timedelta(days=1)

    with get_db() as conn:
        # This week's days
        rows = conn.execute(
            "SELECT * FROM daily_stats WHERE date >= ? AND date <= ? ORDER BY date",
            (week_start.isoformat(), today.isoformat()),
        ).fetchall()

        # Prior week totals
        prior_rows = conn.execute(
            "SELECT * FROM daily_stats WHERE date >= ? AND date <= ?",
            (prior_week_start.isoformat(), prior_week_end.isoformat()),
        ).fetchall()

    # Build day-by-day with zero backfill
    days_map = {dict(r)["date"]: dict(r) for r in rows}
    days = []
    for i in range(7):
        d = (week_start + timedelta(days=i)).isoformat()
        if d in days_map:
            row = days_map[d]
            days.append(
                {
                    "date": d,
                    "tasks_completed": row["tasks_completed"],
                    "focus_sessions": row["focus_sessions"],
                    "brain_dumps": row["brain_dumps"],
                    "focus_minutes": row["focus_minutes"],
                }
            )
        else:
            days.append(
                {
                    "date": d,
                    "tasks_completed": 0,
                    "focus_sessions": 0,
                    "brain_dumps": 0,
                    "focus_minutes": 0,
                }
            )

    # Totals
    totals = _sum_stats([dict(r) for r in rows])
    prior_totals = _sum_stats([dict(r) for r in prior_rows])

    # Trend: compare total tasks
    this_total = totals["tasks_completed"] + totals["focus_sessions"]
    prior_total = prior_totals["tasks_completed"] + prior_totals["focus_sessions"]

    if prior_total == 0:
        trend = "up" if this_total > 0 else "flat"
    elif this_total > prior_total * 1.1:
        trend = "up"
    elif this_total < prior_total * 0.9:
        trend = "down"
    else:
        trend = "flat"

    # Best day by tasks_completed
    best_day = None
    best_count = 0
    for d in days:
        total_activity = d["tasks_completed"] + d["focus_sessions"]
        if total_activity > best_count:
            best_count = total_activity
            best_day = d["date"]

    return {
        "days": days,
        "totals": totals,
        "prior_week_totals": prior_totals,
        "trend": trend,
        "best_day": best_day if best_count > 0 else None,
    }


def _sum_stats(rows: List[Dict]) -> Dict[str, int]:
    """Sum daily_stats rows into totals."""
    return {
        "tasks_completed": sum(r.get("tasks_completed", 0) for r in rows),
        "brain_dumps": sum(r.get("brain_dumps", 0) for r in rows),
        "focus_sessions": sum(r.get("focus_sessions", 0) for r in rows),
        "focus_minutes": sum(r.get("focus_minutes", 0) for r in rows),
        "reminders_done": sum(r.get("reminders_done", 0) for r in rows),
        "routine_steps": sum(r.get("routine_steps", 0) for r in rows),
    }


def get_streaks() -> Dict[str, Any]:
    """Get all streaks for the API."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM streaks ORDER BY category").fetchall()

    streaks = []
    for row in rows:
        streaks.append(
            {
                "category": row["category"],
                "current": row["current_streak"],
                "longest": row["longest_streak"],
                "last_active": row["last_active_date"],
            }
        )

    return {"streaks": streaks}
