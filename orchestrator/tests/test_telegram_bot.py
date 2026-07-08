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


# ---------------------------------------------------------------------------
# selfcare nudges (F-008 mirror)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_selfcare_nudge_medication_sends_with_done_button(tg_on):
    with respx.mock:
        route = respx.post(f"{BOT}/sendMessage").mock(return_value=_tg_ok({"message_id": 11}))
        result = await telegram_bot.send_selfcare_nudge("medication", "Hey, did you take your Vyvanse?")
    assert result["success"] is True
    import json

    body = json.loads(route.calls[0].request.content)
    assert "Vyvanse" in body["text"]
    assert body["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "sc:medication"


@pytest.mark.asyncio
async def test_selfcare_nudge_kind_gating(tg_on, monkeypatch):
    # default TELEGRAM_SELFCARE_NUDGES=medication → movement is skipped
    monkeypatch.setattr(settings, "telegram_selfcare_nudges", "medication", raising=False)
    result = await telegram_bot.send_selfcare_nudge("movement", "Stand up and stretch")
    assert result == {"success": False, "skipped": True, "reason": "kind_not_enabled"}

    # "all" opens every kind
    monkeypatch.setattr(settings, "telegram_selfcare_nudges", "all", raising=False)
    with respx.mock:
        respx.post(f"{BOT}/sendMessage").mock(return_value=_tg_ok({"message_id": 12}))
        result = await telegram_bot.send_selfcare_nudge("movement", "Stand up and stretch")
    assert result["success"] is True


@pytest.mark.asyncio
async def test_callback_selfcare_logs_medication(tg_on, monkeypatch):
    logged = []
    import orchestrator.selfcare_manager as sc

    monkeypatch.setattr(sc, "record_medication_logged", lambda label: logged.append(label))
    with respx.mock:
        answer = respx.post(f"{BOT}/answerCallbackQuery").mock(return_value=_tg_ok(True))
        respx.post(f"{BOT}/editMessageText").mock(return_value=_tg_ok(True))
        import httpx

        async with httpx.AsyncClient() as client:
            await telegram_bot._handle_callback(client, _callback("sc:medication"))
    assert logged == ["telegram:medication nudge"]
    assert answer.called


@pytest.mark.asyncio
async def test_callback_selfcare_rejects_unknown_kind(tg_on, monkeypatch):
    logged = []
    import orchestrator.selfcare_manager as sc

    monkeypatch.setattr(sc, "record_medication_logged", lambda label: logged.append(label))
    with respx.mock:
        answer = respx.post(f"{BOT}/answerCallbackQuery").mock(return_value=_tg_ok(True))
        import httpx

        async with httpx.AsyncClient() as client:
            await telegram_bot._handle_callback(client, _callback("sc:rm_rf_slash"))
    assert logged == []
    assert answer.called


@pytest.mark.asyncio
async def test_dispatch_nudge_mirrors_to_telegram(tg_on, monkeypatch):
    import orchestrator.selfcare_manager as sc
    from orchestrator import telegram_bot as tb

    async def _noop(*a, **k):
        return {"success": True}

    fired = []
    monkeypatch.setattr(sc, "_announce_voice", _noop)
    monkeypatch.setattr(sc, "_send_notification", _noop)
    monkeypatch.setattr(tb, "fire_selfcare_nudge", lambda kind, text: fired.append((kind, text)))

    await sc._dispatch_nudge("medication", "Hey, did you take your Guanfacine?")
    assert fired == [("medication", "Hey, did you take your Guanfacine?")]


# ---------------------------------------------------------------------------
# getUpdates offset persistence — a restart must not re-send old updates
# ---------------------------------------------------------------------------


def test_offset_persistence_roundtrip(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(state_store, "set_app_state", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(state_store, "get_app_state", lambda k: store.get(k))

    telegram_bot._save_offset(123456789)
    # Stored as a string under the dedicated key...
    assert store[telegram_bot._OFFSET_STATE_KEY] == "123456789"
    # ...and reloaded as an int (what a fresh poll loop would resume from).
    assert telegram_bot._load_offset() == 123456789


def test_load_offset_defaults_to_zero(monkeypatch):
    # No persisted value -> start at 0 (first run).
    monkeypatch.setattr(state_store, "get_app_state", lambda k: None)
    assert telegram_bot._load_offset() == 0
    # Corrupt value -> 0, never raise into the poll loop.
    monkeypatch.setattr(state_store, "get_app_state", lambda k: "not-an-int")
    assert telegram_bot._load_offset() == 0


# ---------------------------------------------------------------------------
# inbound media: voice notes + photos (feat/telegram-voice-photos)
# ---------------------------------------------------------------------------


def _media_total(kind: str, result: str) -> float:
    from orchestrator.metrics import TELEGRAM_MEDIA_TOTAL

    return TELEGRAM_MEDIA_TOTAL.labels(kind=kind, result=result)._value.get()


@pytest.fixture
def voice_on(tg_on, monkeypatch):
    """Enable inbound voice with a configured STT endpoint."""
    monkeypatch.setattr(settings, "telegram_voice_enabled", True, raising=False)
    monkeypatch.setattr(settings, "stt_url", "http://stt.test:9000", raising=False)
    monkeypatch.setattr(settings, "telegram_voice_max_seconds", 300, raising=False)
    monkeypatch.setattr(settings, "telegram_voice_wake_helios", True, raising=False)
    monkeypatch.setattr(settings, "telegram_stt_ready_timeout_seconds", 180, raising=False)
    return settings


@pytest.fixture
def photo_on(tg_on, monkeypatch):
    monkeypatch.setattr(settings, "telegram_photo_enabled", True, raising=False)
    monkeypatch.setattr(settings, "vision_enabled", True, raising=False)
    monkeypatch.setattr(settings, "vision_max_image_size", 10 * 1024 * 1024, raising=False)
    return settings


class _DummyClient:
    """Stand-in for httpx.AsyncClient; the media handlers only pass it through
    to the (patched) helpers, so it never needs to make a real request."""


def _voice_msg(duration: int = 3, file_id: str = "voice-file-1", file_size: int = 1000) -> dict:
    return {
        "chat": {"id": int(CHAT_ID)},
        "voice": {"duration": duration, "file_id": file_id, "file_size": file_size, "mime_type": "audio/ogg"},
    }


def _photo_msg(caption: str = "") -> dict:
    m = {
        "chat": {"id": int(CHAT_ID)},
        "photo": [
            {"file_id": "small", "file_size": 100, "width": 90},
            {"file_id": "big", "file_size": 5000, "width": 1280},
        ],
    }
    if caption:
        m["caption"] = caption
    return m


def _patch_send(monkeypatch):
    """Stub _send_text so media handlers never touch HTTP; record the sends."""
    sends: list = []

    async def _fake_send(client, chat_id, text, kind, reply_markup=None):
        sends.append({"chat_id": chat_id, "text": text, "kind": kind})
        return {"ok": True}

    monkeypatch.setattr(telegram_bot, "_send_text", _fake_send)
    return sends


# ----- _pick_image -----


def test_pick_image_selects_largest_photo():
    msg = {
        "photo": [
            {"file_id": "a", "file_size": 100, "width": 90},
            {"file_id": "c", "file_size": 9000, "width": 1600},
            {"file_id": "b", "file_size": 5000, "width": 1280},
        ]
    }
    file_id, mime = telegram_bot._pick_image(msg)
    assert file_id == "c"
    assert mime == "image/jpeg"


def test_pick_image_supported_document():
    msg = {"document": {"file_id": "doc1", "mime_type": "image/png"}}
    assert telegram_bot._pick_image(msg) == ("doc1", "image/png")


def test_pick_image_rejects_non_image_document():
    msg = {"document": {"file_id": "doc2", "mime_type": "application/pdf"}}
    assert telegram_bot._pick_image(msg) == (None, "")


def test_pick_image_empty_message():
    assert telegram_bot._pick_image({}) == (None, "")


# ----- _stt_reachable -----


@pytest.mark.asyncio
async def test_stt_reachable_true_on_200(voice_on):
    class _C:
        async def get(self, url, timeout=None):
            return Response(200, json={})

    assert await telegram_bot._stt_reachable(_C()) is True


@pytest.mark.asyncio
async def test_stt_reachable_true_on_404(voice_on):
    # A 404 on /v1/models still proves the server is answering.
    class _C:
        async def get(self, url, timeout=None):
            return Response(404, text="not found")

    assert await telegram_bot._stt_reachable(_C()) is True


@pytest.mark.asyncio
async def test_stt_reachable_false_on_500(voice_on):
    class _C:
        async def get(self, url, timeout=None):
            return Response(503, text="down")

    assert await telegram_bot._stt_reachable(_C()) is False


@pytest.mark.asyncio
async def test_stt_reachable_false_on_connect_error(voice_on):
    import httpx

    class _C:
        async def get(self, url, timeout=None):
            raise httpx.ConnectError("refused")

    assert await telegram_bot._stt_reachable(_C()) is False


# ----- _handle_voice_impl -----


@pytest.mark.asyncio
async def test_voice_disabled_short_circuits(voice_on, monkeypatch):
    monkeypatch.setattr(settings, "telegram_voice_enabled", False, raising=False)
    sends = _patch_send(monkeypatch)
    downloaded = []
    monkeypatch.setattr(telegram_bot, "_download_file", lambda *a, **k: downloaded.append(1))

    before = _media_total("voice", "disabled")
    await telegram_bot._handle_voice_impl(_DummyClient(), CHAT_ID, _voice_msg())

    assert _media_total("voice", "disabled") == before + 1
    assert downloaded == []  # never attempted the download
    assert sends and "enabled" in sends[-1]["text"]


@pytest.mark.asyncio
async def test_voice_disabled_when_stt_url_empty(voice_on, monkeypatch):
    monkeypatch.setattr(settings, "stt_url", "", raising=False)
    sends = _patch_send(monkeypatch)
    before = _media_total("voice", "disabled")
    await telegram_bot._handle_voice_impl(_DummyClient(), CHAT_ID, _voice_msg())
    assert _media_total("voice", "disabled") == before + 1


@pytest.mark.asyncio
async def test_voice_too_long(voice_on, monkeypatch):
    _patch_send(monkeypatch)
    downloaded = []
    monkeypatch.setattr(telegram_bot, "_download_file", lambda *a, **k: downloaded.append(1))
    before = _media_total("voice", "too_long")
    await telegram_bot._handle_voice_impl(_DummyClient(), CHAT_ID, _voice_msg(duration=999))
    assert _media_total("voice", "too_long") == before + 1
    assert downloaded == []


@pytest.mark.asyncio
async def test_voice_happy_path_transcribes_and_relays(voice_on, monkeypatch):
    from unittest.mock import AsyncMock

    sends = _patch_send(monkeypatch)
    monkeypatch.setattr(telegram_bot, "_download_file", AsyncMock(return_value=b"audio-bytes"))
    monkeypatch.setattr(telegram_bot, "_stt_reachable", AsyncMock(return_value=True))
    monkeypatch.setattr(telegram_bot, "_transcribe", AsyncMock(return_value="add milk to the list"))
    relay = AsyncMock(return_value="Added milk.")
    monkeypatch.setattr(telegram_bot, "_relay_locked", relay)
    wake = AsyncMock()
    from orchestrator import helios_power

    monkeypatch.setattr(helios_power, "wake_helios", wake)

    before = _media_total("voice", "ok")
    await telegram_bot._handle_voice_impl(_DummyClient(), CHAT_ID, _voice_msg())

    assert _media_total("voice", "ok") == before + 1
    relay.assert_awaited_once()
    assert relay.await_args.args[2] == "add milk to the list"  # transcript relayed
    wake.assert_not_awaited()  # STT was reachable; no wake needed
    # Final reply echoes the transcript back to the user.
    final = sends[-1]["text"]
    assert "add milk to the list" in final and "Added milk." in final


@pytest.mark.asyncio
async def test_voice_wakes_helios_then_gives_up(voice_on, monkeypatch):
    from unittest.mock import AsyncMock

    sends = _patch_send(monkeypatch)
    monkeypatch.setattr(telegram_bot, "_download_file", AsyncMock(return_value=b"audio"))
    monkeypatch.setattr(telegram_bot, "_stt_reachable", AsyncMock(return_value=False))
    monkeypatch.setattr(telegram_bot, "_wait_for_stt", AsyncMock(return_value=False))
    relay = AsyncMock()
    monkeypatch.setattr(telegram_bot, "_relay_locked", relay)
    wake = AsyncMock(return_value={"ok": True})
    from orchestrator import helios_power

    monkeypatch.setattr(helios_power, "wake_helios", wake)

    before = _media_total("voice", "stt_unreachable")
    await telegram_bot._handle_voice_impl(_DummyClient(), CHAT_ID, _voice_msg())

    assert _media_total("voice", "stt_unreachable") == before + 1
    wake.assert_awaited_once()  # wake attempted
    relay.assert_not_awaited()  # never got to transcription
    assert any("Waking" in s["text"] for s in sends)  # "waking" notice sent


@pytest.mark.asyncio
async def test_voice_stt_unreachable_wake_disabled(voice_on, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(settings, "telegram_voice_wake_helios", False, raising=False)
    _patch_send(monkeypatch)
    monkeypatch.setattr(telegram_bot, "_download_file", AsyncMock(return_value=b"audio"))
    monkeypatch.setattr(telegram_bot, "_stt_reachable", AsyncMock(return_value=False))
    relay = AsyncMock()
    monkeypatch.setattr(telegram_bot, "_relay_locked", relay)
    wake = AsyncMock()
    from orchestrator import helios_power

    monkeypatch.setattr(helios_power, "wake_helios", wake)

    before = _media_total("voice", "stt_unreachable")
    await telegram_bot._handle_voice_impl(_DummyClient(), CHAT_ID, _voice_msg())

    assert _media_total("voice", "stt_unreachable") == before + 1
    wake.assert_not_awaited()  # wake disabled -> never called
    relay.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_empty_transcript(voice_on, monkeypatch):
    from unittest.mock import AsyncMock

    _patch_send(monkeypatch)
    monkeypatch.setattr(telegram_bot, "_download_file", AsyncMock(return_value=b"audio"))
    monkeypatch.setattr(telegram_bot, "_stt_reachable", AsyncMock(return_value=True))
    monkeypatch.setattr(telegram_bot, "_transcribe", AsyncMock(return_value=""))
    relay = AsyncMock()
    monkeypatch.setattr(telegram_bot, "_relay_locked", relay)

    before = _media_total("voice", "empty_transcript")
    await telegram_bot._handle_voice_impl(_DummyClient(), CHAT_ID, _voice_msg())

    assert _media_total("voice", "empty_transcript") == before + 1
    relay.assert_not_awaited()


# ----- _handle_photo_impl -----


@pytest.mark.asyncio
async def test_photo_disabled(photo_on, monkeypatch):
    monkeypatch.setattr(settings, "vision_enabled", False, raising=False)
    _patch_send(monkeypatch)
    downloaded = []
    monkeypatch.setattr(telegram_bot, "_download_file", lambda *a, **k: downloaded.append(1))
    before = _media_total("photo", "disabled")
    await telegram_bot._handle_photo_impl(_DummyClient(), CHAT_ID, _photo_msg())
    assert _media_total("photo", "disabled") == before + 1
    assert downloaded == []


@pytest.mark.asyncio
async def test_photo_happy_path_relays_with_caption(photo_on, monkeypatch):
    from unittest.mock import AsyncMock

    import orchestrator.vision_handler as vh

    sends = _patch_send(monkeypatch)
    monkeypatch.setattr(telegram_bot, "_download_file", AsyncMock(return_value=b"jpegbytes"))
    monkeypatch.setattr(vh, "analyze_image", AsyncMock(return_value="A grocery receipt for milk and eggs."))
    relay = AsyncMock(return_value="Got it — added milk and eggs.")
    monkeypatch.setattr(telegram_bot, "_relay_locked", relay)

    before = _media_total("photo", "ok")
    await telegram_bot._handle_photo_impl(_DummyClient(), CHAT_ID, _photo_msg(caption="from the store"))

    assert _media_total("photo", "ok") == before + 1
    relay.assert_awaited_once()
    relayed = relay.await_args.args[2]
    assert "A grocery receipt" in relayed
    assert "from the store" in relayed  # caption appended
    assert sends[-1]["text"].startswith("📷")


@pytest.mark.asyncio
async def test_photo_vision_sentinel_not_relayed(photo_on, monkeypatch):
    """CRITICAL regression: a bracketed sentinel from analyze_image must NOT be
    forwarded to Jess as a real caption; result is vision_failed."""
    from unittest.mock import AsyncMock

    import orchestrator.vision_handler as vh

    _patch_send(monkeypatch)
    monkeypatch.setattr(telegram_bot, "_download_file", AsyncMock(return_value=b"jpegbytes"))
    monkeypatch.setattr(vh, "analyze_image", AsyncMock(return_value="[Vision model timed out. Try again later.]"))
    relay = AsyncMock()
    monkeypatch.setattr(telegram_bot, "_relay_locked", relay)

    before = _media_total("photo", "vision_failed")
    await telegram_bot._handle_photo_impl(_DummyClient(), CHAT_ID, _photo_msg())

    assert _media_total("photo", "vision_failed") == before + 1
    relay.assert_not_awaited()  # sentinel never reaches Jess


@pytest.mark.asyncio
async def test_photo_vision_raises_is_vision_failed(photo_on, monkeypatch):
    from unittest.mock import AsyncMock

    import orchestrator.vision_handler as vh

    _patch_send(monkeypatch)
    monkeypatch.setattr(telegram_bot, "_download_file", AsyncMock(return_value=b"jpegbytes"))
    monkeypatch.setattr(vh, "analyze_image", AsyncMock(side_effect=RuntimeError("boom")))
    relay = AsyncMock()
    monkeypatch.setattr(telegram_bot, "_relay_locked", relay)

    before = _media_total("photo", "vision_failed")
    await telegram_bot._handle_photo_impl(_DummyClient(), CHAT_ID, _photo_msg())

    assert _media_total("photo", "vision_failed") == before + 1
    relay.assert_not_awaited()


# ----- _handle_message dispatch to media handlers -----


@pytest.mark.asyncio
async def test_dispatch_voice_message_routes_to_voice_handler(tg_on, monkeypatch):
    from unittest.mock import AsyncMock

    voice_h = AsyncMock()
    photo_h = AsyncMock()
    monkeypatch.setattr(telegram_bot, "_handle_voice", voice_h)
    monkeypatch.setattr(telegram_bot, "_handle_photo", photo_h)

    await telegram_bot._handle_message(_DummyClient(), _voice_msg())
    voice_h.assert_awaited_once()
    photo_h.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_photo_message_routes_to_photo_handler(tg_on, monkeypatch):
    from unittest.mock import AsyncMock

    voice_h = AsyncMock()
    photo_h = AsyncMock()
    monkeypatch.setattr(telegram_bot, "_handle_voice", voice_h)
    monkeypatch.setattr(telegram_bot, "_handle_photo", photo_h)

    await telegram_bot._handle_message(_DummyClient(), _photo_msg())
    photo_h.assert_awaited_once()
    voice_h.assert_not_awaited()


@pytest.mark.asyncio
async def test_drain_pending_awaits_inflight_handlers():
    """On shutdown, _drain_pending lets an already-dispatched handler finish
    (so a deploy doesn't drop e.g. a Done-tap ack)."""
    import asyncio as _a

    flag: dict = {}

    async def slow_handler():
        await _a.sleep(0.05)
        flag["done"] = True

    t = _a.create_task(slow_handler())
    telegram_bot._bg_tasks.add(t)
    t.add_done_callback(telegram_bot._bg_tasks.discard)

    await telegram_bot._drain_pending(timeout=1.0)
    assert flag.get("done") is True
    assert t not in telegram_bot._bg_tasks


# ---------------------------------------------------------------------------
# spoken walkie-talkie reply (feat/telegram-voice-reply)
# ---------------------------------------------------------------------------


TTS_URL = "http://tts.test:5000"


@pytest.fixture
def voice_reply_on(tg_on, monkeypatch):
    """Enable the spoken walkie-talkie reply with a configured TTS endpoint."""
    monkeypatch.setattr(settings, "telegram_voice_reply_enabled", True, raising=False)
    monkeypatch.setattr(settings, "tts_url", TTS_URL, raising=False)
    monkeypatch.setattr(settings, "tts_voice", "default", raising=False)
    monkeypatch.setattr(settings, "telegram_voice_reply_max_chars", 1200, raising=False)
    return settings


def _mock_client_with_post(post_mock):
    """A stand-in httpx client whose .post is the given AsyncMock."""
    from unittest.mock import MagicMock

    client = MagicMock()
    client.post = post_mock
    return client


# ----- _synthesize_speech -----


@pytest.mark.asyncio
async def test_synthesize_speech_returns_bytes_on_200(voice_reply_on):
    from unittest.mock import AsyncMock, MagicMock

    resp = MagicMock(status_code=200, content=b"mp3-bytes")
    post = AsyncMock(return_value=resp)
    client = _mock_client_with_post(post)

    audio = await telegram_bot._synthesize_speech(client, "hello there")
    assert audio == b"mp3-bytes"
    # Posts to the OpenAI-compatible speech endpoint requesting mp3.
    called_url = post.await_args.args[0]
    assert called_url == f"{TTS_URL}/v1/audio/speech"
    assert post.await_args.kwargs["json"]["response_format"] == "mp3"
    assert post.await_args.kwargs["json"]["input"] == "hello there"


@pytest.mark.asyncio
async def test_synthesize_speech_empty_content_is_none(voice_reply_on):
    from unittest.mock import AsyncMock, MagicMock

    resp = MagicMock(status_code=200, content=b"")
    client = _mock_client_with_post(AsyncMock(return_value=resp))
    assert await telegram_bot._synthesize_speech(client, "hi") is None


@pytest.mark.asyncio
async def test_synthesize_speech_non_200_is_none(voice_reply_on):
    from unittest.mock import AsyncMock, MagicMock

    resp = MagicMock(status_code=500, content=b"boom", text="boom")
    client = _mock_client_with_post(AsyncMock(return_value=resp))
    assert await telegram_bot._synthesize_speech(client, "hi") is None


@pytest.mark.asyncio
async def test_synthesize_speech_exception_is_none(voice_reply_on):
    from unittest.mock import AsyncMock

    post = AsyncMock(side_effect=RuntimeError("connect refused"))
    client = _mock_client_with_post(post)
    assert await telegram_bot._synthesize_speech(client, "hi") is None


# ----- _send_voice_reply -----


@pytest.mark.asyncio
async def test_send_voice_reply_true_on_ok(voice_reply_on):
    from unittest.mock import AsyncMock, MagicMock

    resp = MagicMock(status_code=200, content=b'{"ok": true}')
    resp.json.return_value = {"ok": True}
    post = AsyncMock(return_value=resp)
    client = _mock_client_with_post(post)

    assert await telegram_bot._send_voice_reply(client, CHAT_ID, b"mp3") is True
    # Multipart file upload of the mp3.
    assert "files" in post.await_args.kwargs
    assert post.await_args.kwargs["data"]["chat_id"] == CHAT_ID


@pytest.mark.asyncio
async def test_send_voice_reply_false_on_ok_false(voice_reply_on):
    from unittest.mock import AsyncMock, MagicMock

    resp = MagicMock(status_code=200, content=b'{"ok": false}')
    resp.json.return_value = {"ok": False, "description": "bad"}
    client = _mock_client_with_post(AsyncMock(return_value=resp))
    assert await telegram_bot._send_voice_reply(client, CHAT_ID, b"mp3") is False


@pytest.mark.asyncio
async def test_send_voice_reply_false_on_non_200(voice_reply_on):
    from unittest.mock import AsyncMock, MagicMock

    resp = MagicMock(status_code=400, content=b'{"ok": false}')
    resp.json.return_value = {"ok": False, "description": "Bad Request"}
    client = _mock_client_with_post(AsyncMock(return_value=resp))
    assert await telegram_bot._send_voice_reply(client, CHAT_ID, b"mp3") is False


@pytest.mark.asyncio
async def test_send_voice_reply_false_on_exception(voice_reply_on):
    from unittest.mock import AsyncMock

    client = _mock_client_with_post(AsyncMock(side_effect=RuntimeError("boom")))
    assert await telegram_bot._send_voice_reply(client, CHAT_ID, b"mp3") is False


# ----- _maybe_send_voice_reply -----


@pytest.mark.asyncio
async def test_maybe_voice_reply_disabled_no_synth_no_metric(voice_reply_on, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(settings, "telegram_voice_reply_enabled", False, raising=False)
    synth = AsyncMock()
    monkeypatch.setattr(telegram_bot, "_synthesize_speech", synth)

    before_ok = _media_total("voice_reply", "ok")
    before_fail = _media_total("voice_reply", "tts_failed")
    await telegram_bot._maybe_send_voice_reply(_DummyClient(), CHAT_ID, "hi there")

    synth.assert_not_awaited()
    assert _media_total("voice_reply", "ok") == before_ok  # no metric bumped
    assert _media_total("voice_reply", "tts_failed") == before_fail


@pytest.mark.asyncio
async def test_maybe_voice_reply_empty_tts_url_no_synth(voice_reply_on, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(settings, "tts_url", "", raising=False)
    synth = AsyncMock()
    monkeypatch.setattr(telegram_bot, "_synthesize_speech", synth)

    before = _media_total("voice_reply", "tts_failed")
    await telegram_bot._maybe_send_voice_reply(_DummyClient(), CHAT_ID, "hi there")

    synth.assert_not_awaited()
    assert _media_total("voice_reply", "tts_failed") == before


@pytest.mark.asyncio
async def test_maybe_voice_reply_blank_reply_no_synth(voice_reply_on, monkeypatch):
    from unittest.mock import AsyncMock

    synth = AsyncMock()
    monkeypatch.setattr(telegram_bot, "_synthesize_speech", synth)

    before = _media_total("voice_reply", "tts_failed")
    await telegram_bot._maybe_send_voice_reply(_DummyClient(), CHAT_ID, "   \n\t ")

    synth.assert_not_awaited()
    assert _media_total("voice_reply", "tts_failed") == before


@pytest.mark.asyncio
async def test_maybe_voice_reply_happy_path_ok_metric(voice_reply_on, monkeypatch):
    from unittest.mock import AsyncMock

    synth = AsyncMock(return_value=b"mp3-bytes")
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(telegram_bot, "_synthesize_speech", synth)
    monkeypatch.setattr(telegram_bot, "_send_voice_reply", send)

    before = _media_total("voice_reply", "ok")
    await telegram_bot._maybe_send_voice_reply(_DummyClient(), CHAT_ID, "all done")

    synth.assert_awaited_once()
    send.assert_awaited_once()
    assert send.await_args.args[2] == b"mp3-bytes"  # synthesized audio handed to sender
    assert _media_total("voice_reply", "ok") == before + 1


@pytest.mark.asyncio
async def test_maybe_voice_reply_tts_failed_metric_no_send(voice_reply_on, monkeypatch):
    from unittest.mock import AsyncMock

    synth = AsyncMock(return_value=None)
    send = AsyncMock()
    monkeypatch.setattr(telegram_bot, "_synthesize_speech", synth)
    monkeypatch.setattr(telegram_bot, "_send_voice_reply", send)

    before = _media_total("voice_reply", "tts_failed")
    await telegram_bot._maybe_send_voice_reply(_DummyClient(), CHAT_ID, "all done")

    assert _media_total("voice_reply", "tts_failed") == before + 1
    send.assert_not_awaited()  # no audio -> never attempt sendVoice


@pytest.mark.asyncio
async def test_maybe_voice_reply_send_failed_metric(voice_reply_on, monkeypatch):
    from unittest.mock import AsyncMock

    synth = AsyncMock(return_value=b"mp3-bytes")
    send = AsyncMock(return_value=False)
    monkeypatch.setattr(telegram_bot, "_synthesize_speech", synth)
    monkeypatch.setattr(telegram_bot, "_send_voice_reply", send)

    before = _media_total("voice_reply", "send_failed")
    await telegram_bot._maybe_send_voice_reply(_DummyClient(), CHAT_ID, "all done")

    assert _media_total("voice_reply", "send_failed") == before + 1


@pytest.mark.asyncio
async def test_maybe_voice_reply_truncates_to_max_chars(voice_reply_on, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(settings, "telegram_voice_reply_max_chars", 50, raising=False)
    synth = AsyncMock(return_value=b"mp3-bytes")
    monkeypatch.setattr(telegram_bot, "_synthesize_speech", synth)
    monkeypatch.setattr(telegram_bot, "_send_voice_reply", AsyncMock(return_value=True))

    long_reply = "x" * 500
    await telegram_bot._maybe_send_voice_reply(_DummyClient(), CHAT_ID, long_reply)

    spoken = synth.await_args.args[1]
    assert len(spoken) <= 51  # 50 chars + the ellipsis
    assert spoken.endswith("…")
