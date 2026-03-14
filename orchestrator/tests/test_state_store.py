"""
Tests for state_store.py — SQLite persistence.

Tests reminder CRUD, focus session save/load, and notification tracking.
Uses tmp_db fixture for isolated temporary databases.
"""

from datetime import datetime


class TestReminderCRUD:
    def test_save_and_get_reminder(self, tmp_db):
        import state_store

        state_store.save_reminder("test1", "Take meds", "2026-03-12T09:00:00", "voice")
        rem = state_store.get_reminder("test1")
        assert rem is not None
        assert rem["text"] == "Take meds"
        assert rem["target"] == "voice"
        assert rem["status"] == "pending"

    def test_get_nonexistent_reminder(self, tmp_db):
        import state_store

        assert state_store.get_reminder("nonexistent") is None

    def test_get_pending_reminders(self, tmp_db):
        import state_store

        state_store.save_reminder("r1", "Task 1", "2026-03-12T09:00:00")
        state_store.save_reminder("r2", "Task 2", "2026-03-12T10:00:00")
        state_store.save_reminder("r3", "Task 3", "2026-03-12T11:00:00")
        pending = state_store.get_pending_reminders()
        assert len(pending) == 3
        # Should be ordered by trigger_time
        assert pending[0]["id"] == "r1"
        assert pending[2]["id"] == "r3"

    def test_complete_reminder(self, tmp_db):
        import state_store

        state_store.save_reminder("r1", "Task 1", "2026-03-12T09:00:00")
        assert state_store.complete_reminder("r1")
        rem = state_store.get_reminder("r1")
        assert rem["status"] == "completed"
        assert rem["completed_at"] is not None

    def test_complete_nonexistent(self, tmp_db):
        import state_store

        assert not state_store.complete_reminder("nonexistent")

    def test_cancel_reminder(self, tmp_db):
        import state_store

        state_store.save_reminder("r1", "Task 1", "2026-03-12T09:00:00")
        assert state_store.cancel_reminder("r1")
        rem = state_store.get_reminder("r1")
        assert rem["status"] == "cancelled"

    def test_delete_reminder(self, tmp_db):
        import state_store

        state_store.save_reminder("r1", "Task 1", "2026-03-12T09:00:00")
        assert state_store.delete_reminder("r1")
        assert state_store.get_reminder("r1") is None

    def test_delete_nonexistent(self, tmp_db):
        import state_store

        assert not state_store.delete_reminder("nonexistent")

    def test_completed_not_in_pending(self, tmp_db):
        import state_store

        state_store.save_reminder("r1", "Task 1", "2026-03-12T09:00:00")
        state_store.complete_reminder("r1")
        pending = state_store.get_pending_reminders()
        assert len(pending) == 0

    def test_save_reminder_upsert(self, tmp_db):
        """INSERT OR REPLACE should update existing reminder."""
        import state_store

        state_store.save_reminder("r1", "Original", "2026-03-12T09:00:00")
        state_store.save_reminder("r1", "Updated", "2026-03-12T10:00:00")
        rem = state_store.get_reminder("r1")
        assert rem["text"] == "Updated"


class TestFocusSession:
    def test_save_and_load_active(self, tmp_db):
        import state_store

        session = {
            "active": True,
            "task": "coding",
            "started": datetime.now(),
            "duration": 25,
            "break_duration": 5,
            "job_id": "focus_123",
            "audio_player": "media_player.office",
            "block_sites": True,
        }
        state_store.save_focus_session(session)
        loaded = state_store.load_focus_session()
        assert loaded["active"] is True
        assert loaded["task"] == "coding"
        assert loaded["duration"] == 25
        assert loaded["break_duration"] == 5
        assert loaded["block_sites"] is True

    def test_load_default_inactive(self, tmp_db):
        import state_store

        loaded = state_store.load_focus_session()
        assert loaded["active"] is False
        assert loaded["task"] is None

    def test_clear_focus_session(self, tmp_db):
        import state_store

        session = {
            "active": True,
            "task": "test",
            "started": datetime.now(),
            "duration": 25,
            "break_duration": 5,
            "job_id": "j1",
            "audio_player": None,
            "block_sites": False,
        }
        state_store.save_focus_session(session)
        state_store.clear_focus_session()
        loaded = state_store.load_focus_session()
        assert loaded["active"] is False


class TestNotificationTracking:
    def test_mark_and_check(self, tmp_db):
        import state_store

        assert not state_store.is_notified("cal:event123")
        state_store.mark_notified("cal:event123")
        assert state_store.is_notified("cal:event123")

    def test_clear_stale(self, tmp_db):
        import state_store

        # Mark a notification then clear entries older than 0 hours (should clear everything)
        state_store.mark_notified("old:event")
        count = state_store.clear_stale_notifications(older_than_hours=0)
        assert count >= 1
        assert not state_store.is_notified("old:event")

    def test_clear_by_prefix(self, tmp_db):
        import state_store

        state_store.mark_notified("temp:closet_80")
        state_store.mark_notified("temp:closet_85")
        state_store.mark_notified("cal:event1")
        count = state_store.clear_notifications_by_prefix("temp:")
        assert count == 2
        assert not state_store.is_notified("temp:closet_80")
        assert state_store.is_notified("cal:event1")
