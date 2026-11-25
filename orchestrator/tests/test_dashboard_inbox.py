"""Tests for the dashboard inbox (Done/Snooze cards in /chat).

Covers:
  - reminder_actions.ack_reminder / snooze_reminder (the shared Done/Snooze path)
  - inbox.py card lifecycle (add / supersede / resolve / list / reopen / cleanup)
  - selfcare_manager.record_selfcare dispatch + inbox resolution on log
  - routes_inbox via TestClient over the api_routes router
  - tool_handlers.deliver_reminder_job creating an inbox card
  - informational "message" cards (morning/evening briefing digest, missed-
    reminder recap, self-audit): inbox.add_message_item / post_message, their
    always-generic Web Push text, Done refused / Dismiss works, and
    jobs_calendar._mirror_message + its call sites (Telegram kept for installs
    that still run the bot)

The shared AsyncIOScheduler is never started; add_job/get_job work on a stopped
scheduler, and delivery_clean_scheduler scrubs reminder_* jobs around each test.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import shared at module scope (embedding model load), same convention as
# test_ntfy_feedback / test_telegram_bot.
from orchestrator import (  # noqa: F401
    inbox,
    jobs_calendar,
    reminder_actions,
    selfcare_manager,
    shared,
    state_store,
    webpush_manager,
)
from orchestrator.config import settings
from orchestrator.tests.fixtures_delivery import drain_tasks


def _noop_job(*a, **kw):
    return None


@pytest.fixture(autouse=True)
def _fresh_selfcare_state(monkeypatch):
    """These tests log selfcare actions through the real recorders, which set
    selfcare_manager's module-level nudge gates. Give each test a fresh state
    and restore the original afterwards, so later suites (test_selfcare_bridge
    asserts the gates start empty) don't see our timestamps."""
    monkeypatch.setattr(selfcare_manager, "_state", selfcare_manager.SelfCareState())


@pytest.fixture
def no_webpush(monkeypatch):
    """Keep Web Push out of inbox tests (inbox.notify → fire_inbox_push)."""
    monkeypatch.setattr(settings, "webpush_enabled", False, raising=False)
    return settings


@pytest.fixture
def snooze_limit(monkeypatch):
    monkeypatch.setattr(settings, "ntfy_max_snooze_count", 3, raising=False)
    return 3


@pytest.fixture
def record_spy(monkeypatch):
    """Count record_selfcare calls without touching selfcare in-memory state."""
    calls = []

    def _spy(kind, label):
        calls.append((kind, label))
        return True

    monkeypatch.setattr(selfcare_manager, "record_selfcare", _spy)
    return calls


def _open_cards(kind=None, ref=None):
    with state_store.get_db() as conn:
        rows = conn.execute("SELECT * FROM inbox_items WHERE resolved_at IS NULL").fetchall()
    rows = [dict(r) for r in rows]
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    if ref:
        rows = [r for r in rows if r["ref"] == ref]
    return rows


def _selfcare_rows(action):
    with state_store.get_db() as conn:
        return conn.execute("SELECT * FROM selfcare_log WHERE action = ?", (action,)).fetchall()


# ===========================================================================
# reminder_actions.ack_reminder
# ===========================================================================


class TestAckReminder:
    async def test_unknown_returns_not_found(self, tmp_db):
        out = await reminder_actions.ack_reminder("nope", via="dashboard")
        assert out == {"status": "not_found"}

    async def test_ok_cancels_retry_bridges_water_and_resolves_card(self, tmp_db, delivery_clean_scheduler):
        state_store.save_reminder("r-water", "drink water", "2026-10-19T09:00:00")
        card = inbox.add_reminder_item("r-water", "drink water")
        delivery_clean_scheduler.add_job(
            _noop_job, trigger="date", run_date=datetime.now() + timedelta(hours=1), id="reminder_r-water_retry"
        )

        out = await reminder_actions.ack_reminder("r-water", via="ntfy")

        assert out["status"] == "ok"
        assert out["action"] == "water"
        assert delivery_clean_scheduler.get_job("reminder_r-water_retry") is None
        assert len(_selfcare_rows("water")) == 1
        item = inbox.get_item(card["id"])
        assert item["resolution"] == "done"
        assert item["resolved_via"] == "ntfy"
        row = state_store.get_reminder("r-water")
        assert row["status"] == "completed"
        assert row["acked_via"] == "ntfy"

    async def test_unrelated_text_has_no_selfcare_action(self, tmp_db, record_spy):
        state_store.save_reminder("r-call", "call the bank", "2026-10-19T09:00:00")
        out = await reminder_actions.ack_reminder("r-call", via="dashboard")
        assert out["status"] == "ok"
        assert out["action"] is None
        assert record_spy == []

    async def test_meal_plan_reminder_never_bridges(self, tmp_db, record_spy):
        state_store.save_reminder("mp-1", "cook rice and eat dinner", "2026-10-19T09:00:00")
        out = await reminder_actions.ack_reminder("mp-1", via="dashboard")
        assert out["status"] == "ok"
        assert out["action"] is None
        assert record_spy == []

    async def test_second_ack_is_idempotent_no_refire(self, tmp_db, record_spy):
        state_store.save_reminder("r-med", "take your meds", "2026-10-19T09:00:00")
        first = await reminder_actions.ack_reminder("r-med", via="ntfy")
        assert first["status"] == "ok"
        assert len(record_spy) == 1

        # A card that appeared after the ack (e.g. a redelivery race) is still cleared.
        card = inbox.add_reminder_item("r-med", "take your meds")
        second = await reminder_actions.ack_reminder("r-med", via="telegram")
        assert second["status"] == "already_acked"
        assert len(record_spy) == 1  # bridge NOT fired again
        assert inbox.get_item(card["id"])["resolution"] == "done"
        assert state_store.get_reminder("r-med")["acked_via"] == "ntfy"

    async def test_bridge_failure_does_not_fail_ack(self, tmp_db, monkeypatch):
        def _boom(kind, label):
            raise RuntimeError("db locked")

        monkeypatch.setattr(selfcare_manager, "record_selfcare", _boom)
        state_store.save_reminder("r-w2", "drink water", "2026-10-19T09:00:00")
        out = await reminder_actions.ack_reminder("r-w2", via="dashboard")
        assert out["status"] == "ok"
        assert state_store.get_reminder("r-w2")["status"] == "completed"

    async def test_inbox_failure_does_not_fail_ack(self, tmp_db, monkeypatch, record_spy):
        def _boom(*a, **kw):
            raise RuntimeError("no table")

        monkeypatch.setattr(inbox, "resolve_reminder", _boom)
        state_store.save_reminder("r-x", "stretch", "2026-10-19T09:00:00")
        out = await reminder_actions.ack_reminder("r-x", via="dashboard")
        assert out["status"] == "ok"


# ===========================================================================
# reminder_actions.snooze_reminder
# ===========================================================================


class TestSnoozeReminder:
    async def test_unknown_returns_not_found(self, tmp_db, delivery_clean_scheduler):
        out = await reminder_actions.snooze_reminder("nope", 10, via="dashboard")
        assert out == {"status": "not_found"}
        assert delivery_clean_scheduler.get_job("reminder_nope") is None

    async def test_ok_reschedules_reopens_counts_and_resolves(self, tmp_db, delivery_clean_scheduler, snooze_limit):
        state_store.save_reminder("r-s", "stretch", "2026-10-19T09:00:00")
        state_store.complete_reminder("r-s")
        card = inbox.add_reminder_item("r-s", "stretch")
        delivery_clean_scheduler.add_job(
            _noop_job, trigger="date", run_date=datetime.now() + timedelta(hours=1), id="reminder_r-s_retry"
        )

        out = await reminder_actions.snooze_reminder("r-s", 15, via="dashboard")

        assert out["status"] == "ok"
        assert out["minutes"] == 15
        assert out["snooze_count"] == 1
        job = delivery_clean_scheduler.get_job("reminder_r-s")
        assert job is not None
        assert job.args == ("r-s",)
        assert delivery_clean_scheduler.get_job("reminder_r-s_retry") is None
        rem = state_store.get_reminder("r-s")
        assert rem["status"] == "pending"
        assert rem["snooze_count"] == 1
        item = inbox.get_item(card["id"])
        assert item["resolution"] == "snoozed"
        assert item["resolved_via"] == "dashboard"

    @pytest.mark.parametrize("given,expected", [(0, 1), (-5, 1), (999, 120), (120, 120), (1, 1)])
    async def test_minutes_clamped(self, tmp_db, delivery_clean_scheduler, snooze_limit, given, expected):
        state_store.save_reminder("r-c", "stretch", "2026-10-19T09:00:00")
        out = await reminder_actions.snooze_reminder("r-c", given, via="dashboard")
        assert out["status"] == "ok"
        assert out["minutes"] == expected
        delta = (out["run_at"] - datetime.now(out["run_at"].tzinfo)).total_seconds()
        assert expected * 60 - 30 <= delta <= expected * 60 + 1

    async def test_limit_reached(self, tmp_db, delivery_clean_scheduler, snooze_limit):
        state_store.save_reminder("r-l", "stretch", "2026-10-19T09:00:00")
        for _ in range(snooze_limit):
            state_store.increment_snooze_count("r-l")
        card = inbox.add_reminder_item("r-l", "stretch")

        out = await reminder_actions.snooze_reminder("r-l", 10, via="dashboard")

        assert out == {"status": "limit", "snooze_count": snooze_limit}
        assert delivery_clean_scheduler.get_job("reminder_r-l") is None
        assert state_store.get_reminder("r-l")["snooze_count"] == snooze_limit
        assert inbox.get_item(card["id"])["resolved_at"] is None  # card stays open

    async def test_scheduler_failure_returns_error_and_leaves_state(self, tmp_db, monkeypatch, snooze_limit):
        class _BadScheduler:
            def add_job(self, *a, **kw):
                raise RuntimeError("scheduler down")

            def get_job(self, *a):
                return None

        monkeypatch.setattr(shared, "scheduler", _BadScheduler())
        state_store.save_reminder("r-e", "stretch", "2026-10-19T09:00:00")
        state_store.complete_reminder("r-e")
        out = await reminder_actions.snooze_reminder("r-e", 10, via="dashboard")
        assert out == {"status": "error"}
        rem = state_store.get_reminder("r-e")
        assert rem["status"] == "completed"
        assert (rem["snooze_count"] or 0) == 0


# ===========================================================================
# inbox.py
# ===========================================================================


class TestInboxModule:
    def test_add_reminder_supersedes_older_open_card(self, tmp_db):
        a = inbox.add_reminder_item("r1", "first")
        b = inbox.add_reminder_item("r1", "second")
        other = inbox.add_reminder_item("r2", "other")

        old = inbox.get_item(a["id"])
        assert old["resolution"] == "superseded"
        assert old["resolved_via"] == "redelivered"
        assert inbox.get_item(b["id"])["resolved_at"] is None
        assert inbox.get_item(other["id"])["resolved_at"] is None
        assert {c["id"] for c in _open_cards(kind="reminder")} == {b["id"], other["id"]}

    def test_add_reminder_truncates_text(self, tmp_db):
        item = inbox.add_reminder_item("r1", "x" * 2000)
        assert len(inbox.get_item(item["id"])["text"]) == 500

    def test_add_reminder_none_text(self, tmp_db):
        item = inbox.add_reminder_item("r1", None)
        assert inbox.get_item(item["id"])["text"] == ""

    def test_add_selfcare_supersedes_same_kind_only(self, tmp_db):
        w1 = inbox.add_selfcare_item("water", "drink water")
        m1 = inbox.add_selfcare_item("meal", "eat something")
        w2 = inbox.add_selfcare_item("water", "drink water again")

        assert inbox.get_item(w1["id"])["resolution"] == "superseded"
        assert inbox.get_item(m1["id"])["resolved_at"] is None
        assert inbox.get_item(w2["id"])["resolved_at"] is None

    def test_add_selfcare_ignores_unknown_kind(self, tmp_db):
        assert inbox.add_selfcare_item("sleep", "go to bed") is None
        assert inbox.add_selfcare_item("", "x") is None
        assert _open_cards() == []

    def test_resolve_reminder_returns_count(self, tmp_db):
        inbox.add_reminder_item("r1", "x")
        assert inbox.resolve_reminder("r1", resolution="done", via="ntfy") == 1
        assert inbox.resolve_reminder("r1", resolution="done", via="ntfy") == 0  # already closed
        assert inbox.resolve_reminder("missing", resolution="done", via="ntfy") == 0

    def test_resolve_selfcare_returns_count(self, tmp_db):
        inbox.add_selfcare_item("water", "x")
        assert inbox.resolve_selfcare("meal", resolution="done", via="logged") == 0
        assert inbox.resolve_selfcare("water", resolution="done", via="logged") == 1
        assert inbox.resolve_selfcare("water", resolution="done", via="logged") == 0

    def test_resolve_item(self, tmp_db):
        item = inbox.add_selfcare_item("meal", "x")
        assert inbox.resolve_item(item["id"], "dismissed", "dashboard") is True
        assert inbox.resolve_item(item["id"], "dismissed", "dashboard") is False
        assert inbox.resolve_item("0" * 32, "dismissed", "dashboard") is False

    def test_list_pending_excludes_resolved_and_old_newest_first(self, tmp_db):
        old = inbox.add_reminder_item("r-old", "old")
        resolved = inbox.add_reminder_item("r-res", "resolved")
        a = inbox.add_reminder_item("r-a", "a")
        b = inbox.add_selfcare_item("water", "b")
        inbox.resolve_reminder("r-res", resolution="done", via="x")
        with state_store.get_db() as conn:
            conn.execute(
                "UPDATE inbox_items SET created_at = ? WHERE id = ?",
                ((datetime.now() - timedelta(hours=25)).isoformat(), old["id"]),
            )
            conn.execute(
                "UPDATE inbox_items SET created_at = ? WHERE id = ?",
                ((datetime.now() - timedelta(minutes=5)).isoformat(), a["id"]),
            )

        items = inbox.list_pending()
        ids = [i["id"] for i in items]
        assert ids == [b["id"], a["id"]]
        assert resolved["id"] not in ids and old["id"] not in ids
        assert set(items[0].keys()) == {"id", "kind", "ref", "text", "created_at"}

    def test_list_pending_limit(self, tmp_db):
        for i in range(5):
            inbox.add_reminder_item(f"r{i}", "x")
        assert len(inbox.list_pending(limit=3)) == 3

    def test_reopen_item(self, tmp_db):
        item = inbox.add_selfcare_item("water", "x")
        inbox.resolve_item(item["id"], "done", "dashboard")
        assert inbox.reopen_item(item["id"]) is True
        row = inbox.get_item(item["id"])
        assert row["resolved_at"] is None and row["resolution"] is None and row["resolved_via"] is None
        assert inbox.reopen_item("f" * 32) is False

    def test_cleanup_old(self, tmp_db):
        old = inbox.add_reminder_item("r-old", "x")
        new = inbox.add_reminder_item("r-new", "x")
        with state_store.get_db() as conn:
            conn.execute(
                "UPDATE inbox_items SET created_at = ? WHERE id = ?",
                ((datetime.now() - timedelta(days=31)).isoformat(), old["id"]),
            )
        assert inbox.cleanup_old() == 1
        assert inbox.get_item(old["id"]) is None
        assert inbox.get_item(new["id"]) is not None

    def test_notify_none_is_noop(self):
        inbox.notify(None)  # must not raise

    def test_notify_swallows_webpush_errors(self, monkeypatch):
        from orchestrator import webpush_manager

        def _boom(item):
            raise RuntimeError("x")

        monkeypatch.setattr(webpush_manager, "fire_inbox_push", _boom)
        inbox.notify({"id": "a", "kind": "reminder", "ref": "r", "text": "t"})  # must not raise


# ===========================================================================
# selfcare_manager.record_selfcare + inbox resolution
# ===========================================================================


class TestSelfcareRecord:
    @pytest.mark.parametrize(
        "kind,fn",
        [
            ("medication", "record_medication_logged"),
            ("meal", "record_meal_logged"),
            ("water", "record_hydration_logged"),
            ("movement", "record_movement_logged"),
        ],
    )
    def test_dispatch(self, monkeypatch, kind, fn):
        calls = []
        monkeypatch.setattr(selfcare_manager, fn, lambda label: calls.append(label))
        assert selfcare_manager.record_selfcare(kind, "lbl") is True
        assert calls == ["lbl"]

    @pytest.mark.parametrize("kind", ["sleep", "", "WATER", None])
    def test_unknown_kind_returns_false(self, kind):
        assert selfcare_manager.record_selfcare(kind, "lbl") is False

    def test_record_hydration_clears_water_card_only(self, tmp_db):
        water = inbox.add_selfcare_item("water", "drink")
        meal = inbox.add_selfcare_item("meal", "eat")
        selfcare_manager.record_hydration_logged("test:water")
        w = inbox.get_item(water["id"])
        assert w["resolution"] == "done"
        assert w["resolved_via"] == "logged"
        assert inbox.get_item(meal["id"])["resolved_at"] is None

    @pytest.mark.parametrize("action", ["water", "medication", "movement", "meal"])
    async def test_log_selfcare_clears_card(self, tmp_db, monkeypatch, action):
        async def _no_routine(_a):
            return None

        monkeypatch.setattr(selfcare_manager, "_maybe_advance_routine_for_action", _no_routine)
        card = inbox.add_selfcare_item(action, "nudge")
        result = await selfcare_manager.log_selfcare(action)
        assert "Logged" in result
        assert inbox.get_item(card["id"])["resolution"] == "done"

    async def test_log_selfcare_unknown_action_leaves_cards(self, tmp_db):
        card = inbox.add_selfcare_item("water", "nudge")
        result = await selfcare_manager.log_selfcare("dance")
        assert result.startswith("Unknown action")
        assert inbox.get_item(card["id"])["resolved_at"] is None

    def test_resolve_inbox_never_raises(self, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("x")

        monkeypatch.setattr(inbox, "resolve_selfcare", _boom)
        selfcare_manager._resolve_inbox("water")  # must not raise


# ===========================================================================
# routes_inbox via TestClient
# ===========================================================================


class TestInboxRoutes:
    def test_list_shape(self, delivery_api_client, tmp_db):
        inbox.add_reminder_item("r1", "stretch")
        inbox.add_selfcare_item("water", "drink")
        r = delivery_api_client.get("/api/inbox")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert isinstance(body["items"], list) and len(body["items"]) == 2
        assert {i["kind"] for i in body["items"]} == {"reminder", "selfcare"}

    def test_list_empty(self, delivery_api_client, tmp_db):
        r = delivery_api_client.get("/api/inbox")
        assert r.json() == {"ok": True, "items": []}

    def test_done_reminder_card_acks_via_dashboard(self, delivery_api_client, tmp_db, record_spy):
        state_store.save_reminder("r-d", "drink water", "2026-10-19T09:00:00")
        card = inbox.add_reminder_item("r-d", "drink water")
        r = delivery_api_client.post(f"/api/inbox/{card['id']}/done")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "logged": "water"}
        rem = state_store.get_reminder("r-d")
        assert rem["acked_via"] == "dashboard"
        assert rem["status"] == "completed"
        assert inbox.get_item(card["id"])["resolution"] == "done"
        assert record_spy == [("water", "reminder:drink water")]

    def test_done_reminder_card_reminder_gone(self, delivery_api_client, tmp_db):
        card = inbox.add_reminder_item("r-deleted", "x")
        r = delivery_api_client.post(f"/api/inbox/{card['id']}/done")
        assert r.status_code == 404
        assert r.json()["error"] == "reminder_gone"
        assert inbox.get_item(card["id"])["resolution"] == "dismissed"

    def test_done_selfcare_logs_once_double_tap(self, delivery_api_client, tmp_db, record_spy):
        card = inbox.add_selfcare_item("meal", "eat something")
        r1 = delivery_api_client.post(f"/api/inbox/{card['id']}/done")
        assert r1.status_code == 200
        assert r1.json() == {"ok": True, "logged": "meal"}
        r2 = delivery_api_client.post(f"/api/inbox/{card['id']}/done")
        assert r2.status_code == 200
        assert r2.json()["already_resolved"] is True
        assert record_spy == [("meal", "dashboard:meal nudge")]
        item = inbox.get_item(card["id"])
        assert item["resolution"] == "done" and item["resolved_via"] == "dashboard"

    def test_done_selfcare_real_log_writes_row(self, delivery_api_client, tmp_db):
        card = inbox.add_selfcare_item("water", "drink")
        r = delivery_api_client.post(f"/api/inbox/{card['id']}/done")
        assert r.status_code == 200
        assert len(_selfcare_rows("water")) == 1
        # attributed to the dashboard, not overwritten by record_*'s own resolve
        assert inbox.get_item(card["id"])["resolved_via"] == "dashboard"

    def test_done_selfcare_log_failure_reopens_card(self, delivery_api_client, tmp_db, monkeypatch):
        def _boom(kind, label):
            raise RuntimeError("db locked")

        monkeypatch.setattr(selfcare_manager, "record_selfcare", _boom)
        card = inbox.add_selfcare_item("water", "drink")
        r = delivery_api_client.post(f"/api/inbox/{card['id']}/done")
        assert r.status_code == 500
        assert r.json() == {"ok": False, "error": "log_failed"}
        assert inbox.get_item(card["id"])["resolved_at"] is None
        assert card["id"] in [i["id"] for i in inbox.list_pending()]

    def test_snooze_reminder_card(self, delivery_api_client, tmp_db, delivery_clean_scheduler, snooze_limit):
        state_store.save_reminder("r-sn", "stretch", "2026-10-19T09:00:00")
        state_store.complete_reminder("r-sn")
        card = inbox.add_reminder_item("r-sn", "stretch")
        r = delivery_api_client.post(f"/api/inbox/{card['id']}/snooze", json={"minutes": 20})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["minutes"] == 20 and body["snooze_count"] == 1
        datetime.fromisoformat(body["rescheduled_for"])
        assert delivery_clean_scheduler.get_job("reminder_r-sn") is not None
        assert inbox.get_item(card["id"])["resolution"] == "snoozed"

    def test_snooze_default_minutes_without_body(
        self, delivery_api_client, tmp_db, delivery_clean_scheduler, snooze_limit
    ):
        state_store.save_reminder("r-sn2", "stretch", "2026-10-19T09:00:00")
        card = inbox.add_reminder_item("r-sn2", "stretch")
        r = delivery_api_client.post(f"/api/inbox/{card['id']}/snooze")
        assert r.status_code == 200
        assert r.json()["minutes"] == 15

    def test_snooze_limit_409(self, delivery_api_client, tmp_db, delivery_clean_scheduler, snooze_limit):
        state_store.save_reminder("r-lim", "stretch", "2026-10-19T09:00:00")
        for _ in range(snooze_limit):
            state_store.increment_snooze_count("r-lim")
        card = inbox.add_reminder_item("r-lim", "stretch")
        r = delivery_api_client.post(f"/api/inbox/{card['id']}/snooze", json={"minutes": 10})
        assert r.status_code == 409
        assert r.json()["error"] == "max_snoozes_reached"

    def test_snooze_selfcare_card_not_snoozable(self, delivery_api_client, tmp_db):
        card = inbox.add_selfcare_item("water", "drink")
        r = delivery_api_client.post(f"/api/inbox/{card['id']}/snooze", json={"minutes": 10})
        assert r.status_code == 400
        assert r.json()["error"] == "not_snoozable"
        assert inbox.get_item(card["id"])["resolved_at"] is None

    @pytest.mark.parametrize("body", [{"minutes": "x"}, {"minutes": None}, {"minutes": [1]}])
    def test_snooze_bad_minutes(self, delivery_api_client, tmp_db, body):
        card = inbox.add_reminder_item("r1", "x")
        r = delivery_api_client.post(f"/api/inbox/{card['id']}/snooze", json=body)
        assert r.status_code == 400
        assert r.json()["error"] == "bad_minutes"

    def test_snooze_infinity_minutes(self, delivery_api_client, tmp_db):
        card = inbox.add_reminder_item("r1", "x")
        r = delivery_api_client.post(
            f"/api/inbox/{card['id']}/snooze",
            content=b'{"minutes": Infinity}',
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400

    def test_snooze_non_dict_body(self, delivery_api_client, tmp_db):
        card = inbox.add_reminder_item("r1", "x")
        r = delivery_api_client.post(f"/api/inbox/{card['id']}/snooze", json=[1, 2])
        assert r.status_code == 400
        assert r.json()["error"] == "bad_body"

    @pytest.mark.parametrize("bad", ["abc", "G" * 32, "a" * 31, "a" * 33, "A" * 32])
    @pytest.mark.parametrize("action", ["done", "snooze", "dismiss"])
    def test_bad_id_400(self, delivery_api_client, tmp_db, bad, action):
        r = delivery_api_client.post(f"/api/inbox/{bad}/{action}", json={})
        assert r.status_code == 400
        assert r.json()["error"] == "bad_id"

    @pytest.mark.parametrize("action", ["done", "snooze", "dismiss"])
    def test_unknown_id_404(self, delivery_api_client, tmp_db, action):
        r = delivery_api_client.post(f"/api/inbox/{'a' * 32}/{action}", json={})
        assert r.status_code == 404
        assert r.json()["error"] == "not_found"

    def test_dismiss(self, delivery_api_client, tmp_db, record_spy):
        state_store.save_reminder("r-dis", "drink water", "2026-10-19T09:00:00")
        card = inbox.add_reminder_item("r-dis", "drink water")
        r = delivery_api_client.post(f"/api/inbox/{card['id']}/dismiss")
        assert r.status_code == 200 and r.json() == {"ok": True}
        item = inbox.get_item(card["id"])
        assert item["resolution"] == "dismissed" and item["resolved_via"] == "dashboard"
        assert record_spy == []  # dismiss logs nothing
        assert state_store.get_reminder("r-dis")["ack_at"] is None
        # acting on a resolved card is a no-op
        r2 = delivery_api_client.post(f"/api/inbox/{card['id']}/done")
        assert r2.json()["already_resolved"] is True
        assert record_spy == []

    def test_get_is_side_effect_free(self, delivery_api_client, tmp_db):
        card = inbox.add_selfcare_item("water", "drink")
        delivery_api_client.get("/api/inbox")
        delivery_api_client.get("/api/inbox")
        assert inbox.get_item(card["id"])["resolved_at"] is None


# ===========================================================================
# tool_handlers.deliver_reminder_job → inbox card
# ===========================================================================


class TestDeliverReminderJobInbox:
    async def test_phone_delivery_creates_card(self, tmp_db, delivery_quiet, delivery_clean_scheduler):
        from orchestrator.tool_handlers import deliver_reminder_job

        state_store.save_reminder("r-card", "take the bins out", "2026-10-19T09:00:00", target="phone")
        await deliver_reminder_job("r-card")
        await drain_tasks()

        cards = _open_cards(kind="reminder", ref="r-card")
        assert len(cards) == 1
        assert cards[0]["text"] == "take the bins out"
        assert state_store.get_reminder("r-card")["status"] == "completed"

    async def test_snoozed_redelivery_supersedes_old_card(self, tmp_db, delivery_quiet, delivery_clean_scheduler):
        from orchestrator.tool_handlers import deliver_reminder_job

        state_store.save_reminder("r-re", "stretch", "2026-10-19T09:00:00", target="phone")
        await deliver_reminder_job("r-re")
        state_store.reopen_reminder("r-re")
        await deliver_reminder_job("r-re")
        await drain_tasks()
        assert len(_open_cards(kind="reminder", ref="r-re")) == 1

    async def test_inbox_failure_does_not_block_delivery(
        self, tmp_db, delivery_quiet, delivery_clean_scheduler, monkeypatch
    ):
        from orchestrator.tool_handlers import deliver_reminder_job

        def _boom(*a, **kw):
            raise RuntimeError("no table")

        monkeypatch.setattr(inbox, "add_reminder_item", _boom)
        state_store.save_reminder("r-f", "stretch", "2026-10-19T09:00:00", target="phone")
        await deliver_reminder_job("r-f")
        await drain_tasks()
        assert state_store.get_reminder("r-f")["status"] == "completed"

    async def test_webpush_subscriber_counts_as_phone_delivery(
        self, tmp_db, delivery_quiet, delivery_clean_scheduler, monkeypatch
    ):
        """HA push fails and every other channel is off: a healthy Web Push
        device still counts as delivery (reminder completes, no retry)."""
        from orchestrator import webpush_manager
        from orchestrator.tool_handlers import deliver_reminder_job

        async def _fail(*a, **kw):
            return {"success": False, "error": "ha down"}

        monkeypatch.setattr("orchestrator.tool_handlers._send_notification", _fail)
        monkeypatch.setattr(webpush_manager, "has_subscribers", lambda: True)
        monkeypatch.setattr(webpush_manager, "fire_inbox_push", lambda item: None)
        state_store.save_reminder("r-wp", "stretch", "2026-10-19T09:00:00", target="phone")
        await deliver_reminder_job("r-wp")
        await drain_tasks()
        assert state_store.get_reminder("r-wp")["status"] == "completed"
        assert delivery_clean_scheduler.get_job("reminder_r-wp_retry") is None

    async def test_no_phone_channel_retries(self, tmp_db, delivery_quiet, delivery_clean_scheduler, monkeypatch):
        from orchestrator import webpush_manager
        from orchestrator.tool_handlers import deliver_reminder_job

        async def _fail(*a, **kw):
            return {"success": False, "error": "ha down"}

        monkeypatch.setattr("orchestrator.tool_handlers._send_notification", _fail)
        monkeypatch.setattr(webpush_manager, "has_subscribers", lambda: False)
        state_store.save_reminder("r-nowp", "stretch", "2026-10-19T09:00:00", target="phone")
        await deliver_reminder_job("r-nowp")
        await drain_tasks()
        assert state_store.get_reminder("r-nowp")["status"] == "pending"
        assert delivery_clean_scheduler.get_job("reminder_r-nowp_retry") is not None

    async def test_new_card_fires_web_push(self, tmp_db, delivery_quiet, delivery_clean_scheduler, monkeypatch):
        from orchestrator import webpush_manager
        from orchestrator.tool_handlers import deliver_reminder_job

        seen = []
        monkeypatch.setattr(webpush_manager, "fire_inbox_push", lambda item: seen.append(item))
        state_store.save_reminder("r-push", "stretch", "2026-10-19T09:00:00", target="phone")
        await deliver_reminder_job("r-push")
        await drain_tasks()
        assert len(seen) == 1
        assert seen[0]["kind"] == "reminder" and seen[0]["ref"] == "r-push"


# ===========================================================================
# selfcare nudge dispatch → inbox card
# ===========================================================================


class TestSelfcareNudgeInbox:
    async def test_dispatch_nudge_creates_selfcare_card(self, tmp_db, monkeypatch, no_webpush):
        async def _ok(*a, **kw):
            return {"success": True}

        monkeypatch.setattr(selfcare_manager, "_announce_voice", _ok)
        monkeypatch.setattr(selfcare_manager, "_send_notification", _ok)
        monkeypatch.setattr("orchestrator.telegram_bot.fire_selfcare_nudge", lambda *a, **kw: None)
        await selfcare_manager._dispatch_nudge("water", "time for water")
        await selfcare_manager._dispatch_nudge("water", "time for water again")
        await drain_tasks()
        cards = _open_cards(kind="selfcare", ref="water")
        assert len(cards) == 1
        assert cards[0]["text"] == "time for water again"


# ===========================================================================
# Informational "message" cards (kind "message")
# ===========================================================================


class TestAddMessageItem:
    def test_adds_card(self, tmp_db):
        item = inbox.add_message_item("morning_briefing", "Good morning.")
        assert item["kind"] == "message" and item["ref"] == "morning_briefing"
        assert inbox.list_pending()[0]["id"] == item["id"]

    def test_self_audit_ref_accepted_and_superseded(self, tmp_db):
        """The self-audit digest posts under ref "self_audit"; an unlisted ref
        would silently drop the card. Each audit replaces the previous one."""
        old = inbox.add_message_item("self_audit", "2 high")
        new = inbox.add_message_item("self_audit", "All clean")
        assert old is not None and new is not None
        assert inbox.get_item(old["id"])["resolution"] == "superseded"
        assert [i["id"] for i in inbox.list_pending()] == [new["id"]]

    @pytest.mark.parametrize("ref", ["", "reminder", "sneaky", "medication"])
    def test_unknown_ref_ignored(self, tmp_db, ref):
        assert inbox.add_message_item(ref, "text") is None
        assert inbox.list_pending() == []

    @pytest.mark.parametrize("text", ["", "   ", None])
    def test_empty_text_ignored(self, tmp_db, text):
        assert inbox.add_message_item("evening_briefing", text) is None

    def test_supersedes_same_ref_only(self, tmp_db):
        old = inbox.add_message_item("morning_briefing", "yesterday")
        recap = inbox.add_message_item("missed_recap", "recap")
        new = inbox.add_message_item("morning_briefing", "today")
        assert inbox.get_item(old["id"])["resolution"] == "superseded"
        assert inbox.get_item(old["id"])["resolved_via"] == "renewed"
        assert {i["id"] for i in inbox.list_pending()} == {new["id"], recap["id"]}

    def test_longer_cap_than_reminders(self, tmp_db):
        item = inbox.add_message_item("evening_briefing", "x" * 5000)
        assert len(inbox.get_item(item["id"])["text"]) == inbox._MAX_MESSAGE_TEXT
        # reminders keep the short cap
        r = inbox.add_reminder_item("r1", "y" * 5000)
        assert len(inbox.get_item(r["id"])["text"]) == inbox._MAX_TEXT

    def test_does_not_touch_reminder_or_selfcare_cards(self, tmp_db):
        r = inbox.add_reminder_item("morning_briefing", "same ref string")
        s = inbox.add_selfcare_item("water", "drink")
        inbox.add_message_item("morning_briefing", "brief")
        assert inbox.get_item(r["id"])["resolved_at"] is None
        assert inbox.get_item(s["id"])["resolved_at"] is None


class TestPostMessage:
    async def test_adds_and_notifies(self, tmp_db, monkeypatch):
        seen = []
        monkeypatch.setattr(inbox, "notify", lambda item: seen.append(item))
        item = await inbox.post_message("missed_recap", "• thing")
        assert item is not None and seen == [item]

    async def test_push_false_skips_notify(self, tmp_db, monkeypatch):
        notify = MagicMock()
        monkeypatch.setattr(inbox, "notify", notify)
        item = await inbox.post_message("missed_recap", "• thing", push=False)
        assert item is not None
        notify.assert_not_called()

    async def test_db_failure_never_raises(self, tmp_db, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("db locked")

        notify = MagicMock()
        monkeypatch.setattr(inbox, "add_message_item", _boom)
        monkeypatch.setattr(inbox, "notify", notify)
        assert await inbox.post_message("morning_briefing", "x") is None
        notify.assert_not_called()


@pytest.mark.parametrize(
    "ref,body",
    [
        ("morning_briefing", "Morning briefing ready"),
        ("evening_briefing", "Evening wrap-up ready"),
        ("missed_recap", "Some reminders didn't reach you"),
        ("self_audit", "Jess system check ready"),
        ("unknown_ref", "Message from Jess"),
    ],
)
async def test_message_push_is_generic_even_with_show_text(delivery_capture_push, monkeypatch, ref, body):
    monkeypatch.setattr(settings, "webpush_enabled", True, raising=False)
    monkeypatch.setattr(settings, "webpush_show_text", True, raising=False)
    monkeypatch.setattr(webpush_manager, "_subscriber_count", 1)
    webpush_manager.fire_inbox_push({"id": "m1", "kind": "message", "ref": ref, "text": "Take DrugName at 9"})
    await drain_tasks()
    assert delivery_capture_push == [{"title": "Jess", "body": body, "url": "/chat", "tag": "m1"}]


class TestMessageRoutes:
    def test_listed(self, delivery_api_client, tmp_db):
        card = inbox.add_message_item("evening_briefing", "Tomorrow: nothing early.")
        items = delivery_api_client.get("/api/inbox").json()["items"]
        assert items[0]["id"] == card["id"] and items[0]["kind"] == "message"

    def test_done_refused_and_card_stays_open(self, delivery_api_client, tmp_db, monkeypatch):
        record = MagicMock()
        monkeypatch.setattr(selfcare_manager, "record_selfcare", record)
        card = inbox.add_message_item("morning_briefing", "brief")
        r = delivery_api_client.post(f"/api/inbox/{card['id']}/done")
        assert r.status_code == 400 and r.json()["error"] == "not_actionable"
        record.assert_not_called()
        assert inbox.get_item(card["id"])["resolved_at"] is None

    def test_snooze_refused(self, delivery_api_client, tmp_db):
        card = inbox.add_message_item("morning_briefing", "brief")
        r = delivery_api_client.post(f"/api/inbox/{card['id']}/snooze", json={"minutes": 15})
        assert r.status_code == 400 and r.json()["error"] == "not_snoozable"

    def test_dismiss(self, delivery_api_client, tmp_db):
        card = inbox.add_message_item("missed_recap", "recap")
        r = delivery_api_client.post(f"/api/inbox/{card['id']}/dismiss")
        assert r.status_code == 200 and r.json() == {"ok": True}
        item = inbox.get_item(card["id"])
        assert item["resolution"] == "dismissed" and item["resolved_via"] == "dashboard"


class TestMirrorMessage:
    async def test_posts_to_inbox_and_telegram(self, tmp_db, no_webpush):
        with patch("orchestrator.telegram_bot.fire_system_message") as tg:
            await jobs_calendar._mirror_message("evening_briefing", "wrap-up", "🌙 wrap-up")
        tg.assert_called_once_with("🌙 wrap-up")
        items = inbox.list_pending()
        assert len(items) == 1 and items[0]["ref"] == "evening_briefing" and items[0]["text"] == "wrap-up"

    @pytest.mark.parametrize("dnd,pushed", [(False, True), (True, False)])
    async def test_dnd_posts_card_without_push(self, tmp_db, monkeypatch, dnd, pushed):
        monkeypatch.setattr(shared, "DND_ACTIVE", dnd)
        notify = MagicMock()
        monkeypatch.setattr(inbox, "notify", notify)
        with patch("orchestrator.telegram_bot.fire_system_message"):
            await jobs_calendar._mirror_message("missed_recap", "• one", "⚠️ • one")
        assert [i["ref"] for i in inbox.list_pending()] == ["missed_recap"]
        assert notify.called is pushed

    async def test_inbox_failure_still_sends_telegram(self, tmp_db, monkeypatch):
        monkeypatch.setattr(inbox, "post_message", AsyncMock(side_effect=RuntimeError("x")))
        with patch("orchestrator.telegram_bot.fire_system_message") as tg:
            await jobs_calendar._mirror_message("morning_briefing", "brief", "☀️ brief")
        tg.assert_called_once()

    async def test_telegram_failure_still_posts_card(self, tmp_db, no_webpush):
        with patch("orchestrator.telegram_bot.fire_system_message", side_effect=RuntimeError("no loop")):
            await jobs_calendar._mirror_message("morning_briefing", "brief", "☀️ brief")
        assert [i["ref"] for i in inbox.list_pending()] == ["morning_briefing"]

    async def test_telegram_disabled_card_still_posted(self, tmp_db, no_webpush, monkeypatch):
        """The owner's setup: TELEGRAM_ENABLED=false must not drop the message."""
        monkeypatch.setattr(settings, "telegram_enabled", False, raising=False)
        await jobs_calendar._mirror_message("missed_recap", "• one", "⚠️ • one")
        await drain_tasks()  # let the no-op telegram task run
        assert [i["ref"] for i in inbox.list_pending()] == ["missed_recap"]


def test_call_sites_use_mirror_with_known_refs():
    """Each former Telegram-only send goes through _mirror_message with a ref
    the inbox accepts (an unknown ref would silently drop the card)."""
    import inspect
    import re

    src = inspect.getsource(jobs_calendar)
    refs = re.findall(r'_mirror_message\("([a-z_]+)"', src)
    # Set, not list: the evening briefing mirrors from two places (normal
    # night + routine-active night), and that's intended.
    assert set(refs) == {"evening_briefing", "missed_recap", "morning_briefing"}
    assert all(r in inbox.MESSAGE_REFS for r in refs)
    # fire_system_message is only reached via the helper now.
    assert src.count("fire_system_message(") == 1
