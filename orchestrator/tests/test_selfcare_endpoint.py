"""
Tests for GET /api/selfcare/today (orchestrator/api_routes.py).

Covers:
1. Empty database — all 4 actions zeroed, ok=True.
2. Mixed: today + historical-only — logged_today reflects today; last_ever reflects all-time.
3. Multiple entries today — count, DESC ordering, last_today == entries[0].logged_at.
4. Whitelist — rows with action="garbage" never echoed (load-bearing security defense).
5. Error path — get_selfcare_today raising returns HTTP 500 with ok=False.

Pattern matches test_ntfy_feedback.py / test_paperless_bridge.py — mounts the
api_routes.router on a bare FastAPI app (no middleware → no bearer required).
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest


def _can_import():
    try:
        from orchestrator import api_routes  # noqa: F401
        from orchestrator import state_store  # noqa: F401

        return True
    except (ImportError, ModuleNotFoundError):
        return False


_skip_no_deps = pytest.mark.skipif(
    not _can_import(),
    reason="api_routes / state_store require full orchestrator dependencies",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    """TestClient over the api_routes router (no middleware → no auth)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.api_routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def clean_selfcare():
    """Truncate selfcare_log before and after each test for isolation."""
    from orchestrator import state_store

    with state_store.get_db() as conn:
        conn.execute("DELETE FROM selfcare_log")
    yield
    with state_store.get_db() as conn:
        conn.execute("DELETE FROM selfcare_log")


def _insert_raw(action: str, detail, logged_at: str) -> None:
    """Insert a selfcare_log row directly, bypassing the 5-min dedup in
    save_selfcare_log so we can backdate / batch entries deterministically.
    """
    from orchestrator import state_store

    with state_store.get_db() as conn:
        conn.execute(
            "INSERT INTO selfcare_log (action, detail, logged_at) VALUES (?, ?, ?)",
            (action, detail, logged_at),
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestSelfcareToday:
    def test_empty_database_returns_zeros_for_all_actions(self, client, clean_selfcare):
        r = client.get("/api/selfcare/today")
        assert r.status_code == 200
        body = r.json()

        assert body["ok"] is True
        assert "as_of" in body
        assert body["today_date"] == datetime.now().strftime("%Y-%m-%d")

        actions = body["actions"]
        # All four whitelisted actions present even with empty DB.
        assert set(actions.keys()) == {"medication", "meal", "water", "movement"}
        for name, payload in actions.items():
            assert payload["logged_today"] is False, name
            assert payload["count_today"] == 0, name
            assert payload["last_today"] is None, name
            assert payload["last_ever"] is None, name
            assert payload["entries"] == [], name

    def test_mixed_today_and_historical_only(self, client, clean_selfcare):
        """medication logged today; water last seen yesterday only.

        Validates that logged_today is False for water (no entry today) but
        last_ever still reflects the historical row.
        """
        from orchestrator import state_store

        # Today: one medication entry.
        state_store.save_selfcare_log("medication", "Adderall")

        # Historical-only: water logged yesterday (well before midnight today).
        # Insert raw so we can backdate the timestamp past the dedup window.
        yesterday = (datetime.now() - timedelta(days=1, hours=2)).isoformat()
        _insert_raw("water", "glass", yesterday)

        r = client.get("/api/selfcare/today")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        actions = body["actions"]

        # medication: logged today, last_today + last_ever both set.
        med = actions["medication"]
        assert med["logged_today"] is True
        assert med["count_today"] == 1
        assert med["last_today"] is not None
        assert med["last_ever"] is not None
        # Today's entry is also the latest ever in this isolated DB.
        assert med["last_today"] == med["last_ever"]
        assert len(med["entries"]) == 1
        assert med["entries"][0]["detail"] == "Adderall"

        # water: NOT logged today, but last_ever is the yesterday row.
        water = actions["water"]
        assert water["logged_today"] is False
        assert water["count_today"] == 0
        assert water["last_today"] is None
        assert water["last_ever"] == yesterday
        assert water["entries"] == []

        # meal & movement: never logged.
        for name in ("meal", "movement"):
            assert actions[name]["logged_today"] is False
            assert actions[name]["count_today"] == 0
            assert actions[name]["last_today"] is None
            assert actions[name]["last_ever"] is None
            assert actions[name]["entries"] == []

    def test_multiple_entries_today_count_and_desc_order(self, client, clean_selfcare):
        """Three meal rows today → count=3, entries DESC, last_today == entries[0].

        Use raw inserts because save_selfcare_log dedups within a 5-min window
        on (action, detail) — direct SQL lets us pin distinct timestamps.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        # Three timestamps today, ascending real time.
        ts_early = f"{today}T07:30:00"
        ts_mid = f"{today}T12:15:00"
        ts_late = f"{today}T18:45:00"

        _insert_raw("meal", "breakfast", ts_early)
        _insert_raw("meal", "lunch", ts_mid)
        _insert_raw("meal", "dinner", ts_late)

        r = client.get("/api/selfcare/today")
        assert r.status_code == 200
        meal = r.json()["actions"]["meal"]

        assert meal["logged_today"] is True
        assert meal["count_today"] == 3
        assert len(meal["entries"]) == 3

        # DESC order: latest first.
        ordered_ts = [e["logged_at"] for e in meal["entries"]]
        assert ordered_ts == [ts_late, ts_mid, ts_early]
        ordered_details = [e["detail"] for e in meal["entries"]]
        assert ordered_details == ["dinner", "lunch", "breakfast"]

        # last_today is the most recent entry.
        assert meal["last_today"] == ts_late
        assert meal["last_today"] == meal["entries"][0]["logged_at"]
        # last_ever in this isolated DB is also the latest of those rows.
        assert meal["last_ever"] == ts_late

    def test_whitelist_drops_unknown_action(self, client, clean_selfcare):
        """A row with action='garbage' must NOT appear in the response.

        This is the load-bearing security defense — the endpoint hardcodes
        the 4-action whitelist so a poisoned row (or a future schema drift)
        can't surface unexpected categories on the dashboard.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        ts = f"{today}T10:00:00"

        # Inject a garbage row alongside a legit one so we know "actions" is
        # populated but only with whitelisted keys.
        _insert_raw("garbage", "should-not-appear", ts)
        _insert_raw("medication", "Adderall", ts)

        r = client.get("/api/selfcare/today")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True

        actions = body["actions"]
        # Exactly the 4 whitelisted keys, nothing else.
        assert set(actions.keys()) == {"medication", "meal", "water", "movement"}
        assert "garbage" not in actions

        # No whitelisted action's entries should reference the garbage detail.
        for name, payload in actions.items():
            for e in payload["entries"]:
                assert e["detail"] != "should-not-appear", name

        # Sanity: the legit medication row did land.
        assert actions["medication"]["count_today"] == 1
        assert actions["medication"]["entries"][0]["detail"] == "Adderall"

    def test_unhandled_exception_returns_500_with_ok_false(self, client, clean_selfcare):
        """If get_selfcare_today raises, handler returns HTTP 500 + ok:False."""
        with patch(
            "orchestrator.state_store.get_selfcare_today",
            side_effect=RuntimeError("simulated DB explosion"),
        ):
            r = client.get("/api/selfcare/today")

        assert r.status_code == 500
        body = r.json()
        assert body == {"ok": False, "error": "Selfcare read failed"}
