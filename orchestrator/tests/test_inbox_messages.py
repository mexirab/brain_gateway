"""Tests for informational inbox cards (kind "message").

The morning briefing digest, the evening digest and the missed-reminder recap
used to go only to Telegram (telegram_bot.fire_system_message). They now also
land in the dashboard inbox + Web Push, with Telegram kept for installs that
still run the bot.

Covers:
  - inbox.add_message_item / post_message (refs, supersede, cap, never raises)
  - webpush_manager.fire_inbox_push: message bodies are always generic
  - routes_inbox: Done refused, Dismiss works
  - jobs_calendar._mirror_message + the three call sites
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator import inbox, shared, state_store, webpush_manager  # noqa: F401
from orchestrator.config import settings


@pytest.fixture
def no_webpush(monkeypatch):
    monkeypatch.setattr(settings, "webpush_enabled", False, raising=False)


# ===========================================================================
# inbox.py
# ===========================================================================


class TestAddMessageItem:
    def test_adds_card(self, tmp_db):
        item = inbox.add_message_item("morning_briefing", "Good morning.")
        assert item["kind"] == "message" and item["ref"] == "morning_briefing"
        assert inbox.list_pending()[0]["id"] == item["id"]

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


# ===========================================================================
# Web Push lock-screen text
# ===========================================================================


@pytest.fixture
def capture_send(monkeypatch):
    calls = []

    async def _fake(title, body, url="/chat", tag=None):
        calls.append({"title": title, "body": body, "url": url, "tag": tag})
        return {"sent": 1, "failed": 0}

    monkeypatch.setattr(settings, "webpush_enabled", True, raising=False)
    monkeypatch.setattr(webpush_manager, "_subscriber_count", 1)
    monkeypatch.setattr(webpush_manager, "send_to_all", _fake)
    return calls


async def _drain():
    pending = list(webpush_manager._bg_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize(
    "ref,body",
    [
        ("morning_briefing", "Morning briefing ready"),
        ("evening_briefing", "Evening wrap-up ready"),
        ("missed_recap", "Some reminders didn't reach you"),
        ("unknown_ref", "Message from Jess"),
    ],
)
async def test_message_push_is_generic_even_with_show_text(capture_send, monkeypatch, ref, body):
    monkeypatch.setattr(settings, "webpush_show_text", True, raising=False)
    webpush_manager.fire_inbox_push({"id": "m1", "kind": "message", "ref": ref, "text": "Take DrugName at 9"})
    await _drain()
    assert capture_send == [{"title": "Jess", "body": body, "url": "/chat", "tag": "m1"}]


# ===========================================================================
# routes_inbox
# ===========================================================================


@pytest.fixture
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.api_routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestMessageRoutes:
    def test_listed(self, client, tmp_db):
        card = inbox.add_message_item("evening_briefing", "Tomorrow: nothing early.")
        items = client.get("/api/inbox").json()["items"]
        assert items[0]["id"] == card["id"] and items[0]["kind"] == "message"

    def test_done_refused_and_card_stays_open(self, client, tmp_db, monkeypatch):
        from orchestrator import selfcare_manager

        record = MagicMock()
        monkeypatch.setattr(selfcare_manager, "record_selfcare", record)
        card = inbox.add_message_item("morning_briefing", "brief")
        r = client.post(f"/api/inbox/{card['id']}/done")
        assert r.status_code == 400 and r.json()["error"] == "not_actionable"
        record.assert_not_called()
        assert inbox.get_item(card["id"])["resolved_at"] is None

    def test_snooze_refused(self, client, tmp_db):
        card = inbox.add_message_item("morning_briefing", "brief")
        r = client.post(f"/api/inbox/{card['id']}/snooze", json={"minutes": 15})
        assert r.status_code == 400 and r.json()["error"] == "not_snoozable"

    def test_dismiss(self, client, tmp_db):
        card = inbox.add_message_item("missed_recap", "recap")
        r = client.post(f"/api/inbox/{card['id']}/dismiss")
        assert r.status_code == 200 and r.json() == {"ok": True}
        item = inbox.get_item(card["id"])
        assert item["resolution"] == "dismissed" and item["resolved_via"] == "dashboard"


# ===========================================================================
# jobs_calendar mirroring
# ===========================================================================


class TestMirrorMessage:
    async def test_posts_to_inbox_and_telegram(self, tmp_db, no_webpush):
        from orchestrator import jobs_calendar

        with patch("orchestrator.telegram_bot.fire_system_message") as tg:
            await jobs_calendar._mirror_message("evening_briefing", "wrap-up", "🌙 wrap-up")
        tg.assert_called_once_with("🌙 wrap-up")
        items = inbox.list_pending()
        assert len(items) == 1 and items[0]["ref"] == "evening_briefing" and items[0]["text"] == "wrap-up"

    @pytest.mark.parametrize("dnd,pushed", [(False, True), (True, False)])
    async def test_dnd_posts_card_without_push(self, tmp_db, monkeypatch, dnd, pushed):
        from orchestrator import jobs_calendar

        monkeypatch.setattr(shared, "DND_ACTIVE", dnd)
        notify = MagicMock()
        monkeypatch.setattr(inbox, "notify", notify)
        with patch("orchestrator.telegram_bot.fire_system_message"):
            await jobs_calendar._mirror_message("missed_recap", "• one", "⚠️ • one")
        assert [i["ref"] for i in inbox.list_pending()] == ["missed_recap"]
        assert notify.called is pushed

    async def test_inbox_failure_still_sends_telegram(self, tmp_db, monkeypatch):
        from orchestrator import jobs_calendar

        monkeypatch.setattr(inbox, "post_message", AsyncMock(side_effect=RuntimeError("x")))
        with patch("orchestrator.telegram_bot.fire_system_message") as tg:
            await jobs_calendar._mirror_message("morning_briefing", "brief", "☀️ brief")
        tg.assert_called_once()

    async def test_telegram_failure_still_posts_card(self, tmp_db, no_webpush):
        from orchestrator import jobs_calendar

        with patch("orchestrator.telegram_bot.fire_system_message", side_effect=RuntimeError("no loop")):
            await jobs_calendar._mirror_message("morning_briefing", "brief", "☀️ brief")
        assert [i["ref"] for i in inbox.list_pending()] == ["morning_briefing"]

    async def test_telegram_disabled_card_still_posted(self, tmp_db, no_webpush, monkeypatch):
        """The owner's setup: TELEGRAM_ENABLED=false must not drop the message."""
        from orchestrator import jobs_calendar

        monkeypatch.setattr(settings, "telegram_enabled", False, raising=False)
        await jobs_calendar._mirror_message("missed_recap", "• one", "⚠️ • one")
        await asyncio.sleep(0)  # let the no-op telegram task run
        assert [i["ref"] for i in inbox.list_pending()] == ["missed_recap"]


def test_call_sites_use_mirror_with_known_refs():
    """Each former Telegram-only send goes through _mirror_message with a ref
    the inbox accepts (an unknown ref would silently drop the card)."""
    import inspect
    import re

    from orchestrator import jobs_calendar

    src = inspect.getsource(jobs_calendar)
    refs = re.findall(r'_mirror_message\("([a-z_]+)"', src)
    assert sorted(refs) == ["evening_briefing", "missed_recap", "morning_briefing"]
    assert all(r in inbox.MESSAGE_REFS for r in refs)
    # fire_system_message is only reached via the helper now.
    assert src.count("fire_system_message(") == 1
