"""
Tests for progress_tracker.py (F-005) — event recording, streaks, daily/weekly
summaries, personal bests, and API data functions.

Uses a temporary SQLite database for isolation.
"""

from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest

from orchestrator import progress_tracker
from orchestrator.tests._helpers import metric_value


@pytest.fixture(autouse=True)
def tracker(tmp_path, monkeypatch):
    """progress_tracker on a fresh database; monkeypatch restores DB_PATH after."""
    monkeypatch.setattr(progress_tracker, "DB_PATH", str(tmp_path / "test_progress.db"))
    progress_tracker.init_db()
    return progress_tracker


# ---------------------------------------------------------------------------
# Event recording
# ---------------------------------------------------------------------------


class TestRecordEvent:
    @pytest.mark.parametrize(
        ("event", "meta", "column"),
        [
            pytest.param("task_done", None, "tasks_completed", id="task"),
            # count is metadata; the column increments by 1 per dump
            pytest.param("brain_dump", {"count": 3}, "brain_dumps", id="brain-dump"),
            pytest.param("reminder_done", None, "reminders_done", id="reminder"),
        ],
    )
    def test_counter_events_increment(self, tracker, event, meta, column):
        tracker.record_event(event, meta)
        assert tracker.get_today_stats()[column] == 1
        tracker.record_event(event, meta)
        assert tracker.get_today_stats()[column] == 2

    def test_focus_complete(self, tracker):
        tracker.record_event("focus_complete", {"minutes": 25, "sprints": 1})
        stats = tracker.get_today_stats()
        assert stats["focus_sessions"] == 1
        assert stats["focus_minutes"] == 25

    def test_focus_partial_only_adds_minutes(self, tracker):
        tracker.record_event("focus_partial", {"minutes": 12})
        stats = tracker.get_today_stats()
        assert stats["focus_sessions"] == 0  # no session increment
        assert stats["focus_minutes"] == 12

    def test_unknown_event_type_ignored(self, tracker):
        tracker.record_event("unknown_type")
        stats = tracker.get_today_stats()
        assert stats["tasks_completed"] == 0

    def test_focus_minutes_accumulate(self, tracker):
        tracker.record_event("focus_complete", {"minutes": 25})
        tracker.record_event("focus_complete", {"minutes": 30})
        tracker.record_event("focus_partial", {"minutes": 10})
        stats = tracker.get_today_stats()
        assert stats["focus_sessions"] == 2
        assert stats["focus_minutes"] == 65


# ---------------------------------------------------------------------------
# Streaks
# ---------------------------------------------------------------------------


class TestStreaks:
    def test_streak_starts_at_one(self, tracker):
        tracker.record_event("task_done")
        streaks = tracker.get_streaks()
        task_streak = next(s for s in streaks["streaks"] if s["category"] == "task")
        assert task_streak["current"] == 1

    def test_streak_same_day_idempotent(self, tracker):
        tracker.record_event("task_done")
        tracker.record_event("task_done")
        streaks = tracker.get_streaks()
        task_streak = next(s for s in streaks["streaks"] if s["category"] == "task")
        assert task_streak["current"] == 1  # still 1, same day

    def test_streak_increments_consecutive_days(self, tracker):
        today = date.today()

        # Manually insert yesterday's streak
        yesterday = (today - timedelta(days=1)).isoformat()
        with tracker.get_db() as conn:
            conn.execute(
                "INSERT INTO streaks (category, current_streak, longest_streak, last_active_date, updated_at) "
                "VALUES (?, 3, 3, ?, ?)",
                ("task", yesterday, yesterday),
            )

        tracker.record_event("task_done")
        streaks = tracker.get_streaks()
        task_streak = next(s for s in streaks["streaks"] if s["category"] == "task")
        assert task_streak["current"] == 4
        assert task_streak["longest"] == 4

    def test_streak_breaks_after_gap(self, tracker):
        today = date.today()

        # Insert streak from 2 days ago (gap of 1 day)
        two_days_ago = (today - timedelta(days=2)).isoformat()
        with tracker.get_db() as conn:
            conn.execute(
                "INSERT INTO streaks (category, current_streak, longest_streak, last_active_date, updated_at) "
                "VALUES (?, 5, 5, ?, ?)",
                ("task", two_days_ago, two_days_ago),
            )

        tracker.record_event("task_done")
        streaks = tracker.get_streaks()
        task_streak = next(s for s in streaks["streaks"] if s["category"] == "task")
        assert task_streak["current"] == 1  # reset
        assert task_streak["longest"] == 5  # longest preserved

    def test_focus_partial_no_streak(self, tracker):
        tracker.record_event("focus_partial", {"minutes": 10})
        streaks = tracker.get_streaks()
        assert len(streaks["streaks"]) == 0  # no streak created


# ---------------------------------------------------------------------------
# Daily/weekly summaries
# ---------------------------------------------------------------------------


class TestSummaries:
    async def test_daily_summary_empty_day(self, tracker):
        summary = await tracker.daily_summary()
        assert "quiet day" in summary

    async def test_daily_summary_with_activity(self, tracker):
        tracker.record_event("task_done")
        tracker.record_event("task_done")
        tracker.record_event("focus_complete", {"minutes": 25})
        summary = await tracker.daily_summary()
        assert "2 tasks" in summary
        assert "25 minutes" in summary

    async def test_weekly_summary_empty(self, tracker):
        summary = await tracker.weekly_summary()
        assert "quiet" in summary

    async def test_weekly_summary_with_data(self, tracker):
        tracker.record_event("task_done")
        tracker.record_event("brain_dump")
        summary = await tracker.weekly_summary()
        assert "1 tasks done" in summary or "1 task" in summary


# ---------------------------------------------------------------------------
# API data functions
# ---------------------------------------------------------------------------


class TestAPIData:
    def test_empty_database(self, tracker):
        stats = tracker.get_today_stats()
        assert stats["date"] == date.today().isoformat()
        assert stats["tasks_completed"] == 0
        week = tracker.get_week_stats()
        assert len(week["days"]) == 7  # backfilled
        assert all(d["tasks_completed"] == 0 for d in week["days"])
        assert week["trend"] == "flat"
        assert week["best_day"] is None
        assert tracker.get_streaks()["streaks"] == []

    def test_init_db_idempotent(self, tracker):
        tracker.init_db()  # second call must not error
        assert tracker.get_today_stats()["date"] == date.today().isoformat()


# ---------------------------------------------------------------------------
# Personal best
# ---------------------------------------------------------------------------


class TestPersonalBest:
    def test_no_best_when_zero(self, tracker):
        result = tracker._personal_best_check("tasks_completed", 0, "Monday")
        assert result is None

    def test_no_best_with_no_history(self, tracker):
        result = tracker._personal_best_check("tasks_completed", 5, "Monday")
        assert result is None  # no historical data to compare against

    def test_best_when_exceeds_average(self, tracker):
        """Same weekday, each of the past 4 weeks at 3 tasks: 10 today is a best."""
        today = date.today()
        with tracker.get_db() as conn:
            for weeks in range(1, 5):
                past = (today - timedelta(weeks=weeks)).isoformat()
                conn.execute(
                    "INSERT INTO daily_stats (date, tasks_completed, updated_at) VALUES (?, ?, ?)",
                    (past, 3, past),
                )

        result = tracker._personal_best_check("tasks_completed", 10, today.strftime("%A"))
        assert result is not None
        assert "best" in result.lower()
        assert today.strftime("%A") in result
        # Not above the average (+10 %): no best.
        assert tracker._personal_best_check("tasks_completed", 3, today.strftime("%A")) is None


# ---------------------------------------------------------------------------
# Streak milestone announcements
# ---------------------------------------------------------------------------


class TestStreakAnnouncements:
    async def test_announces_each_milestone_once(self, tracker, tmp_db, monkeypatch):
        """Only today's streaks that sit on a milestone are spoken, each once."""
        from orchestrator import reminder_manager
        from orchestrator.metrics import PROGRESS_STREAK_MILESTONES

        today = date.today().isoformat()
        with tracker.get_db() as conn:
            conn.executemany(
                "INSERT INTO streaks (category, current_streak, longest_streak, last_active_date, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    ("focus_sessions", 3, 3, today, today),  # milestone, active today
                    ("tasks_completed", 4, 4, today, today),  # not a milestone
                    ("brain_dumps", 5, 5, "2000-01-01", today),  # milestone, but not today
                ],
            )
        announce = AsyncMock()
        monkeypatch.setattr(reminder_manager, "_announce_voice", announce)
        before = metric_value(PROGRESS_STREAK_MILESTONES, category="focus_sessions")

        await tracker.check_and_announce_streaks()
        await tracker.check_and_announce_streaks()  # already notified: silent

        announce.assert_awaited_once_with("focus sessions streak: 3 days in a row!", announcement_type="progress")
        assert metric_value(PROGRESS_STREAK_MILESTONES, category="focus_sessions") == before + 1

    async def test_failed_announcement_is_retried_next_time(self, tracker, tmp_db, monkeypatch):
        from orchestrator import reminder_manager

        today = date.today().isoformat()
        with tracker.get_db() as conn:
            conn.execute(
                "INSERT INTO streaks (category, current_streak, longest_streak, last_active_date, updated_at) "
                "VALUES ('focus_sessions', 7, 7, ?, ?)",
                (today, today),
            )
        monkeypatch.setattr(reminder_manager, "_announce_voice", AsyncMock(side_effect=RuntimeError("speaker down")))
        await tracker.check_and_announce_streaks()  # must not raise
        announce = AsyncMock()
        monkeypatch.setattr(reminder_manager, "_announce_voice", announce)
        await tracker.check_and_announce_streaks()
        announce.assert_awaited_once()
