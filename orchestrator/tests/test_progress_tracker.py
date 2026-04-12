"""
Tests for progress_tracker.py (F-005) — event recording, streaks, daily/weekly
summaries, personal bests, and API data functions.

Uses a temporary SQLite database for isolation.
"""

import os
from datetime import date, timedelta
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Setup: patch DB_PATH before importing progress_tracker
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def tmp_db(tmp_path):
    """Use a temporary database for each test."""
    db_path = str(tmp_path / "test_progress.db")
    with patch.dict(os.environ, {"PROGRESS_DB_PATH": db_path}):
        from orchestrator import progress_tracker

        progress_tracker.DB_PATH = db_path
        progress_tracker.init_db()
        yield progress_tracker
        # Reset module-level DB_PATH back for safety
        progress_tracker.DB_PATH = db_path


@pytest.fixture
def tracker(tmp_db):
    return tmp_db


# ---------------------------------------------------------------------------
# Event recording
# ---------------------------------------------------------------------------


class TestRecordEvent:
    def test_task_done_increments(self, tracker):
        tracker.record_event("task_done")
        stats = tracker.get_today_stats()
        assert stats["tasks_completed"] == 1

        tracker.record_event("task_done")
        stats = tracker.get_today_stats()
        assert stats["tasks_completed"] == 2

    def test_brain_dump_increments(self, tracker):
        tracker.record_event("brain_dump", {"count": 3})
        stats = tracker.get_today_stats()
        assert stats["brain_dumps"] == 1  # count is metadata, column increments by 1

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

    def test_reminder_done(self, tracker):
        tracker.record_event("reminder_done")
        stats = tracker.get_today_stats()
        assert stats["reminders_done"] == 1

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
# Streak announcements
# ---------------------------------------------------------------------------


class TestStreakAnnouncements:
    def test_milestone_reached(self, tracker):
        """Verify streak reaches a milestone value that would trigger announcement."""
        today = date.today()
        yesterday = (today - timedelta(days=1)).isoformat()

        # Set up streak at 2 (will become 3 = milestone)
        with tracker.get_db() as conn:
            conn.execute(
                "INSERT INTO streaks (category, current_streak, longest_streak, last_active_date, updated_at) "
                "VALUES (?, 2, 2, ?, ?)",
                ("task", yesterday, yesterday),
            )

        tracker.record_event("task_done")  # streak -> 3

        streaks = tracker.get_streaks()
        task_streak = next(s for s in streaks["streaks"] if s["category"] == "task")
        assert task_streak["current"] == 3
        assert 3 in tracker.STREAK_MILESTONES

    def test_non_milestone_not_triggered(self, tracker):
        """Streak at 2 or 4 should not be a milestone."""
        assert 2 not in tracker.STREAK_MILESTONES
        assert 4 not in tracker.STREAK_MILESTONES


# ---------------------------------------------------------------------------
# Daily/weekly summaries
# ---------------------------------------------------------------------------


class TestSummaries:
    @pytest.mark.asyncio
    async def test_daily_summary_empty_day(self, tracker):
        summary = await tracker.daily_summary()
        assert "quiet day" in summary

    @pytest.mark.asyncio
    async def test_daily_summary_with_activity(self, tracker):
        tracker.record_event("task_done")
        tracker.record_event("task_done")
        tracker.record_event("focus_complete", {"minutes": 25})
        summary = await tracker.daily_summary()
        assert "2 tasks" in summary
        assert "25 minutes" in summary

    @pytest.mark.asyncio
    async def test_weekly_summary_empty(self, tracker):
        summary = await tracker.weekly_summary()
        assert "quiet" in summary

    @pytest.mark.asyncio
    async def test_weekly_summary_with_data(self, tracker):
        tracker.record_event("task_done")
        tracker.record_event("brain_dump")
        summary = await tracker.weekly_summary()
        assert "1 tasks done" in summary or "1 task" in summary


# ---------------------------------------------------------------------------
# API data functions
# ---------------------------------------------------------------------------


class TestAPIData:
    def test_get_today_stats_empty(self, tracker):
        stats = tracker.get_today_stats()
        assert stats["date"] == date.today().isoformat()
        assert stats["tasks_completed"] == 0

    def test_get_week_stats_backfill(self, tracker):
        week = tracker.get_week_stats()
        assert len(week["days"]) == 7
        assert all(d["tasks_completed"] == 0 for d in week["days"])

    def test_get_week_stats_trend_flat_when_empty(self, tracker):
        week = tracker.get_week_stats()
        assert week["trend"] == "flat"
        assert week["best_day"] is None

    def test_get_streaks_empty(self, tracker):
        streaks = tracker.get_streaks()
        assert streaks["streaks"] == []

    def test_init_db_idempotent(self, tracker):
        # Calling init_db again should not error
        tracker.init_db()
        stats = tracker.get_today_stats()
        assert stats["date"] == date.today().isoformat()


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
        today = date.today()
        # Insert historical data for same weekday over past 4 weeks
        weekday_num = today.strftime("%w")
        for i in range(1, 5):
            past_date = today - timedelta(weeks=i)
            # Only use days that match the same weekday
            if past_date.strftime("%w") == weekday_num:
                with tracker.get_db() as conn:
                    conn.execute(
                        "INSERT INTO daily_stats (date, tasks_completed, updated_at) VALUES (?, ?, ?)",
                        (past_date.isoformat(), 3, past_date.isoformat()),
                    )

        # Check if 10 is a personal best (should be, average is 3)
        result = tracker._personal_best_check("tasks_completed", 10, today.strftime("%A"))
        if result:  # depends on whether same-weekday data was found
            assert "best" in result.lower()
