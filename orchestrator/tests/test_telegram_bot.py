"""Tests for the Telegram bot (two-way capture + reminder channel)."""

import pytest
import respx
from httpx import Response

# Import shared at module scope: its import loads the embedding model (a
# HuggingFace HEAD request on first load), which must happen OUTSIDE any
# respx.mock context. Same convention as test_ntfy_feedback.
from orchestrator import shared, state_store, telegram_bot  # noqa: F401
from orchestrator.config import settings

API_BASE = "https://tg.test"
TOKEN = "123456:test-token"
BOT = f"{API_BASE}/bot{TOKEN}"
SELF_URL = "http://self.test:8888"
CHAT_ID = "1111"


@pytest.fixture
def tg_on(monkeypatch):
    """Enable telegram on the live settings singleton."""
    monkeypatch.setattr(settings, "telegram_enabled", True, raising=False)
    monkeypatch.setattr(settings, "telegram_bot_token", TOKEN, raising=False)
    monkeypatch.setattr(settings, "telegram_allowed_chat_id", CHAT_ID, raising=False)
    monkeypatch.setattr(settings, "telegram_api_base", API_BASE, raising=False)
    monkeypatch.setattr(settings, "telegram_self_url", SELF_URL, raising=False)
    monkeypatch.setattr(settings, "telegram_snooze_minutes", 10, raising=False)
    monkeypatch.setattr(settings, "telegram_history_turns", 6, raising=False)
    monkeypatch.setattr(settings, "api_token", "test-api-token", raising=False)
    return settings


@pytest.fixture(autouse=True)
def _clear_history():
    telegram_bot._history.clear()
    telegram_bot._denied_log_ts.clear()
    yield
    telegram_bot._history.clear()
    telegram_bot._denied_log_ts.clear()


def _send_total(result: str, kind: str, reason: str) -> float:
    from orchestrator.metrics import TELEGRAM_SEND_TOTAL

    return TELEGRAM_SEND_TOTAL.labels(result=result, kind=kind, reason=reason)._value.get()


def _tg_ok(payload=None):
    return Response(200, json={"ok": True, "result": payload if payload is not None else {}})


# ---------------------------------------------------------------------------
# deliver_via_telegram
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_disabled_is_skipped(monkeypatch):
    monkeypatch.setattr(settings, "telegram_enabled", False, raising=False)
    before = _send_total("skipped", "reminder", "disabled")
    result = await telegram_bot.deliver_via_telegram("r1", "take meds")
    assert result == {"success": False, "skipped": True, "reason": "disabled"}
    assert _send_total("skipped", "reminder", "disabled") == before + 1


@pytest.mark.asyncio
async def test_deliver_without_chat_id_is_skipped(tg_on, monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_chat_id", "", raising=False)
    result = await telegram_bot.deliver_via_telegram("r1", "take meds")
    assert result["skipped"] is True
    assert result["reason"] == "missing_chat_id"


@pytest.mark.asyncio
async def test_deliver_sends_buttons(tg_on):
    with respx.mock:
        route = respx.post(f"{BOT}/sendMessage").mock(return_value=_tg_ok({"message_id": 7}))
        before = _send_total("ok", "reminder", "ok")
        result = await telegram_bot.deliver_via_telegram("r42", "stretch break")

    assert result["success"] is True
    assert _send_total("ok", "reminder", "ok") == before + 1
    sent = route.calls[0].request
    import json

    body = json.loads(sent.content)
    assert body["chat_id"] == CHAT_ID
    assert "stretch break" in body["text"]
    buttons = body["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == "ack:r42"
    assert buttons[1]["callback_data"] == "snooze:r42:10"


@pytest.mark.asyncio
async def test_deliver_api_error_is_failure(tg_on):
    with respx.mock:
        respx.post(f"{BOT}/sendMessage").mock(
            return_value=Response(400, json={"ok": False, "description": "Bad Request"})
        )
        result = await telegram_bot.deliver_via_telegram("r1", "take meds")
    assert result["success"] is False


# ---------------------------------------------------------------------------
# chunking
# ---------------------------------------------------------------------------


def test_chunk_text_short_passthrough():
    assert telegram_bot._chunk_text("hi") == ["hi"]


def test_chunk_text_long_splits_under_limit():
    text = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
    chunks = telegram_bot._chunk_text(text)
    assert len(chunks) > 1
    assert all(len(c) <= 4096 for c in chunks)
    reassembled = "\n".join(chunks)
    assert "line 0" in reassembled and "line 199" in reassembled


# ---------------------------------------------------------------------------
# inbound messages
# ---------------------------------------------------------------------------


def _msg(text: str, chat_id: str = CHAT_ID) -> dict:
    return {"chat": {"id": int(chat_id)}, "text": text}


@pytest.mark.asyncio
async def test_message_from_unknown_chat_is_dropped(tg_on):
    with respx.mock:  # no routes mocked: any request would raise
        import httpx

        async with httpx.AsyncClient() as client:
            await telegram_bot._handle_message(client, _msg("hello", chat_id="9999"))
    # No exception = no outbound call was attempted.


@pytest.mark.asyncio
async def test_help_command_replies_without_chat_relay(tg_on):
    with respx.mock:
        route = respx.post(f"{BOT}/sendMessage").mock(return_value=_tg_ok({"message_id": 1}))
        import httpx

        async with httpx.AsyncClient() as client:
            await telegram_bot._handle_message(client, _msg("/help"))
    assert route.called
    import json

    assert "Done / Snooze" in json.loads(route.calls[0].request.content)["text"]


@pytest.mark.asyncio
async def test_text_relays_through_chat_route(tg_on):
    chat_response = {"choices": [{"message": {"role": "assistant", "content": "Added to your list."}}]}
    with respx.mock:
        respx.post(f"{BOT}/sendChatAction").mock(return_value=_tg_ok(True))
        relay = respx.post(f"{SELF_URL}/v1/chat/completions").mock(return_value=Response(200, json=chat_response))
        send = respx.post(f"{BOT}/sendMessage").mock(return_value=_tg_ok({"message_id": 2}))
        import httpx

        async with httpx.AsyncClient() as client:
            await telegram_bot._handle_message(client, _msg("add milk to the shopping list"))

    assert relay.called
    import json

    relay_body = json.loads(relay.calls[0].request.content)
    assert relay_body["messages"][-1] == {"role": "user", "content": "add milk to the shopping list"}
    assert relay.calls[0].request.headers["authorization"] == "Bearer test-api-token"
    assert json.loads(send.calls[0].request.content)["text"] == "Added to your list."
    # History committed on success
    assert len(telegram_bot._history[CHAT_ID]) == 2


@pytest.mark.asyncio
async def test_history_capped_and_new_resets(tg_on):
    telegram_bot._history[CHAT_ID] = [{"role": "user", "content": str(i)} for i in range(10)]
    chat_response = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
    with respx.mock:
        respx.post(f"{BOT}/sendChatAction").mock(return_value=_tg_ok(True))
        respx.post(f"{SELF_URL}/v1/chat/completions").mock(return_value=Response(200, json=chat_response))
        respx.post(f"{BOT}/sendMessage").mock(return_value=_tg_ok({"message_id": 3}))
        import httpx

        async with httpx.AsyncClient() as client:
            await telegram_bot._handle_message(client, _msg("hi"))
            # capped to telegram_history_turns (6)
            assert len(telegram_bot._history[CHAT_ID]) == 6
            await telegram_bot._handle_message(client, _msg("/new"))
    assert CHAT_ID not in telegram_bot._history


@pytest.mark.asyncio
async def test_relay_failure_not_committed_to_history(tg_on):
    with respx.mock:
        respx.post(f"{BOT}/sendChatAction").mock(return_value=_tg_ok(True))
        respx.post(f"{SELF_URL}/v1/chat/completions").mock(return_value=Response(500, text="boom"))
        send = respx.post(f"{BOT}/sendMessage").mock(return_value=_tg_ok({"message_id": 4}))
        import httpx

        async with httpx.AsyncClient() as client:
            await telegram_bot._handle_message(client, _msg("hello?"))
    # Error reply still sent to the user, but nothing enters history.
    assert send.called
    assert telegram_bot._history.get(CHAT_ID, []) == []


# ---------------------------------------------------------------------------
# Done / Snooze callbacks
# ---------------------------------------------------------------------------


def _callback(data: str, chat_id: str = CHAT_ID) -> dict:
    return {
        "id": "cb1",
        "data": data,
        "message": {"chat": {"id": int(chat_id)}, "message_id": 55, "text": "⏰ take meds"},
    }


@pytest.mark.asyncio
async def test_callback_ack_completes_reminder(tg_on, tmp_db):
    state_store.save_reminder("tg-r1", "take meds", "2026-07-05T10:00:00")
    with respx.mock:
        respx.post(f"{BOT}/answerCallbackQuery").mock(return_value=_tg_ok(True))
        respx.post(f"{BOT}/editMessageText").mock(return_value=_tg_ok(True))
        import httpx

        async with httpx.AsyncClient() as client:
            await telegram_bot._handle_callback(client, _callback("ack:tg-r1"))

    rem = state_store.get_reminder("tg-r1")
    assert rem["status"] == "completed"


@pytest.mark.asyncio
async def test_callback_ack_is_idempotent(tg_on, tmp_db):
    state_store.save_reminder("tg-r2", "take meds", "2026-07-05T10:00:00")
    state_store.mark_reminder_acked("tg-r2", via="telegram")
    with respx.mock:
        answer = respx.post(f"{BOT}/answerCallbackQuery").mock(return_value=_tg_ok(True))
        edit = respx.post(f"{BOT}/editMessageText").mock(return_value=_tg_ok(True))
        import httpx

        async with httpx.AsyncClient() as client:
            await telegram_bot._handle_callback(client, _callback("ack:tg-r2"))
    assert answer.called
    assert not edit.called  # replay: toast only, message untouched


@pytest.fixture
def clean_scheduler():
    """Remove reminder_* jobs from the shared scheduler before and after."""
    import contextlib

    from orchestrator.shared import scheduler

    def _sweep():
        for job in list(scheduler.get_jobs()):
            if job.id.startswith("reminder_"):
                with contextlib.suppress(Exception):
                    scheduler.remove_job(job.id)

    _sweep()
    yield scheduler
    _sweep()


@pytest.mark.asyncio
async def test_callback_snooze_reopens_and_reschedules(tg_on, tmp_db, clean_scheduler):
    state_store.save_reminder("tg-r3", "stretch", "2026-07-05T10:00:00")
    state_store.complete_reminder("tg-r3")

    with respx.mock:
        respx.post(f"{BOT}/answerCallbackQuery").mock(return_value=_tg_ok(True))
        respx.post(f"{BOT}/editMessageText").mock(return_value=_tg_ok(True))
        import httpx

        async with httpx.AsyncClient() as client:
            await telegram_bot._handle_callback(client, _callback("snooze:tg-r3:15"))

    rem = state_store.get_reminder("tg-r3")
    assert rem["status"] == "pending"
    assert rem["snooze_count"] == 1
    assert clean_scheduler.get_job("reminder_tg-r3") is not None


@pytest.mark.asyncio
async def test_callback_snooze_respects_limit(tg_on, tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "ntfy_max_snooze_count", 2, raising=False)
    state_store.save_reminder("tg-r4", "stretch", "2026-07-05T10:00:00")
    state_store.increment_snooze_count("tg-r4")
    state_store.increment_snooze_count("tg-r4")
    with respx.mock:
        answer = respx.post(f"{BOT}/answerCallbackQuery").mock(return_value=_tg_ok(True))
        import httpx

        async with httpx.AsyncClient() as client:
            await telegram_bot._handle_callback(client, _callback("snooze:tg-r4:10"))
    assert answer.called
    assert state_store.get_reminder("tg-r4")["snooze_count"] == 2  # unchanged


@pytest.mark.asyncio
async def test_callback_from_unknown_chat_changes_nothing(tg_on, tmp_db):
    state_store.save_reminder("tg-r5", "take meds", "2026-07-05T10:00:00")
    with respx.mock:  # any outbound call would raise
        import httpx

        async with httpx.AsyncClient() as client:
            await telegram_bot._handle_callback(client, _callback("ack:tg-r5", chat_id="9999"))
    assert state_store.get_reminder("tg-r5")["status"] == "pending"


# ---------------------------------------------------------------------------
# allowlist parsing
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# poll cycle (_poll_once)
# ---------------------------------------------------------------------------


async def _drain_bg_tasks():
    import asyncio

    if telegram_bot._bg_tasks:
        await asyncio.gather(*list(telegram_bot._bg_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_poll_once_advances_offset_and_dispatches(tg_on):
    batch = {
        "ok": True,
        "result": [
            {"update_id": 5, "message": {"chat": {"id": int(CHAT_ID)}, "text": "/help"}},
            {"update_id": 7, "unknown_kind": {}},
        ],
    }
    with respx.mock:
        poll = respx.post(f"{BOT}/getUpdates").mock(
            side_effect=[
                Response(200, json=batch),
                Response(200, json={"ok": True, "result": []}),
            ]
        )
        send = respx.post(f"{BOT}/sendMessage").mock(return_value=_tg_ok({"message_id": 9}))
        import httpx

        async with httpx.AsyncClient() as client:
            offset, hint = await telegram_bot._poll_once(client, 0)
            assert (offset, hint) == (8, 0.0)  # max(update_id) + 1
            await _drain_bg_tasks()
            offset2, hint2 = await telegram_bot._poll_once(client, offset)

    import json

    # Second request carries the advanced offset.
    assert json.loads(poll.calls[1].request.content)["offset"] == 8
    assert (offset2, hint2) == (8, 0.0)
    # The /help message in the batch was actually handled (dispatch ran).
    assert send.called


@pytest.mark.asyncio
async def test_poll_once_conflict_409_asks_for_long_sleep(tg_on):
    with respx.mock:
        respx.post(f"{BOT}/getUpdates").mock(return_value=Response(409, json={"ok": False, "description": "Conflict"}))
        import httpx

        async with httpx.AsyncClient() as client:
            offset, hint = await telegram_bot._poll_once(client, 3)
    assert (offset, hint) == (3, 60.0)


@pytest.mark.asyncio
async def test_poll_once_server_error_signals_backoff(tg_on):
    with respx.mock:
        respx.post(f"{BOT}/getUpdates").mock(return_value=Response(500, text="oops"))
        import httpx

        async with httpx.AsyncClient() as client:
            offset, hint = await telegram_bot._poll_once(client, 3)
    assert (offset, hint) == (3, -1.0)


def test_redact_strips_bot_token(tg_on):
    msg = f"ConnectError for url https://tg.test/bot{TOKEN}/getUpdates"
    assert TOKEN not in telegram_bot._redact(msg)
    assert "<bot-token>" in telegram_bot._redact(msg)


def test_allowlist_supports_comma_list(tg_on, monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_chat_id", "1111, 2222", raising=False)
    assert telegram_bot._chat_allowed("1111")
    assert telegram_bot._chat_allowed("2222")
    assert not telegram_bot._chat_allowed("3333")


def test_allowlist_empty_denies_everyone(tg_on, monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_chat_id", "", raising=False)
    assert not telegram_bot._chat_allowed("1111")
