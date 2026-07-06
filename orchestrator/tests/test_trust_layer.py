"""Tests for the reminder trust layer: delivery-outcome queries, the morning
missed-recap, and the /api/reminders last-24h delivery log."""

from datetime import datetime, timedelta

import pytest

# Module-scope import: shared's import loads the embedding model (HF HEAD
# request on first load) and must happen outside any mocked-transport
# context. Same convention as test_ntfy_feedback / test_telegram_bot.
from orchestrator import shared, state_store  # noqa: F401
from orchestrator.jobs_calendar import build_missed_recap


@pytest.fixture
def client():
    """FastAPI TestClient over the api_routes router."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.api_routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _backdate_completed_at(reminder_id: str, days: int) -> None:
    old = (datetime.now() - timedelta(days=days)).isoformat()
    with state_store.get_db() as conn:
        conn.execute("UPDATE reminders SET completed_at = ? WHERE id = ?", (old, reminder_id))


# ---------------------------------------------------------------------------
# state_store.get_recent_reminder_outcomes
# ---------------------------------------------------------------------------


class TestRecentOutcomes:
    def test_returns_terminal_states_within_window(self, tmp_db):
        state_store.save_reminder("t1", "delivered one", "2026-07-05T08:00:00")
        state_store.complete_reminder("t1")
        state_store.save_reminder("t2", "missed one", "2026-07-05T08:00:00")
        state_store.mark_reminder_missed("t2")
        state_store.save_reminder("t3", "failed one", "2026-07-05T08:00:00")
        state_store.mark_reminder_failed("t3")
        state_store.save_reminder("t4", "still pending", "2026-07-06T08:00:00")

        rows = state_store.get_recent_reminder_outcomes(hours=24)
        ids = {r["id"] for r in rows}
        assert ids == {"t1", "t2", "t3"}
        by_id = {r["id"]: r for r in rows}
        assert by_id["t2"]["status"] == "missed"
        assert by_id["t3"]["status"] == "failed"

    def test_window_excludes_old_outcomes(self, tmp_db):
        state_store.save_reminder("old", "ancient history", "2026-07-01T08:00:00")
        state_store.complete_reminder("old")
        _backdate_completed_at("old", days=3)
        state_store.save_reminder("new", "fresh", "2026-07-05T08:00:00")
        state_store.complete_reminder("new")

        rows = state_store.get_recent_reminder_outcomes(hours=24)
        assert [r["id"] for r in rows] == ["new"]

    def test_newest_first_and_limit(self, tmp_db):
        for i in range(5):
            state_store.save_reminder(f"r{i}", f"reminder {i}", "2026-07-05T08:00:00")
            state_store.complete_reminder(f"r{i}")
        rows = state_store.get_recent_reminder_outcomes(hours=24, limit=3)
        assert len(rows) == 3
        stamps = [r["completed_at"] for r in rows]
        assert stamps == sorted(stamps, reverse=True)


# ---------------------------------------------------------------------------
# build_missed_recap (morning briefing sentence)
# ---------------------------------------------------------------------------


class TestMissedRecap:
    def test_single_reminder_singular(self):
        s = build_missed_recap([{"text": "take meds", "status": "missed"}])
        assert "1 reminder didn't reach you" in s
        assert "take meds" in s
        assert "dashboard" in s

    def test_three_named_no_more_suffix(self):
        items = [{"text": t} for t in ("a", "b", "c")]
        s = build_missed_recap(items)
        assert "3 reminders" in s
        assert "a, b, c" in s
        assert "more" not in s

    def test_overflow_counts_the_rest(self):
        items = [{"text": f"item {i}"} for i in range(5)]
        s = build_missed_recap(items)
        assert "5 reminders" in s
        assert "and 2 more" in s
        assert "item 3" not in s

    def test_blank_texts_tolerated(self):
        s = build_missed_recap([{"text": ""}, {"text": "  "}])
        assert "2 reminders" in s
        assert ":" not in s.split("dashboard")[0].replace("Heads up:", "")


# ---------------------------------------------------------------------------
# GET /api/reminders — delivery log
# ---------------------------------------------------------------------------


class TestRemindersApi:
    def test_recent_section_included(self, client, tmp_db):
        state_store.save_reminder("api1", "went fine", "2026-07-05T08:00:00")
        state_store.mark_reminder_acked("api1", via="telegram")
        state_store.save_reminder("api2", "vanished", "2026-07-05T08:00:00")
        state_store.mark_reminder_failed("api2")

        body = client.get("/api/reminders").json()
        assert "recent" in body
        recent = {r["id"]: r for r in body["recent"]}
        assert recent["api1"]["status"] == "completed"
        assert recent["api1"]["acked_via"] == "telegram"
        assert recent["api2"]["status"] == "failed"
        assert recent["api2"]["acked_via"] is None
        # Delivery-log rows expose the render-ready field set.
        assert set(recent["api2"].keys()) == {
            "id",
            "text",
            "time",
            "status",
            "completed_at",
            "acked_via",
            "snooze_count",
        }

    def test_pending_rows_get_time_alias(self, client, tmp_db):
        state_store.save_reminder("api3", "upcoming", "2026-07-06T09:00:00")
        body = client.get("/api/reminders").json()
        pend = [r for r in body["reminders"] if r["id"] == "api3"]
        assert pend and pend[0]["time"] == "2026-07-06T09:00:00"
