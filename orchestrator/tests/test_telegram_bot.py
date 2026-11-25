"""Tests for the Telegram bot (two-way capture + reminder channel)."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from httpx import Response

# Import shared at module scope: its import loads the embedding model (a
# HuggingFace HEAD request on first load), which must happen OUTSIDE any
# respx.mock context. Same convention as test_ntfy_feedback.
import orchestrator.selfcare_manager as sc
import orchestrator.vision_handler as vh
from orchestrator import helios_power, shared, state_store, telegram_bot  # noqa: F401
from orchestrator.config import settings
from orchestrator.metrics import TELEGRAM_MEDIA_TOTAL, TELEGRAM_SEND_TOTAL
from orchestrator.tests._helpers import metric_value

API_BASE = "https://tg.test"
TOKEN = "123456:test-token"
BOT = f"{API_BASE}/bot{TOKEN}"
SELF_URL = "http://self.test:8888"
CHAT_ID = "1111"
TTS_URL = "http://tts.test:5000"


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


@pytest.fixture
async def client():
    """A real httpx client; respx.mock (entered inside each test) intercepts it."""
    async with httpx.AsyncClient() as c:
        yield c


def _send_total(result: str, kind: str, reason: str) -> float:
    return metric_value(TELEGRAM_SEND_TOTAL, result=result, kind=kind, reason=reason)


def _media_total(kind: str, result: str) -> float:
    return metric_value(TELEGRAM_MEDIA_TOTAL, kind=kind, result=result)


def _tg_ok(payload=None):
    return Response(200, json={"ok": True, "result": payload if payload is not None else {}})


def _sent_json(route, i=0) -> dict:
    return json.loads(route.calls[i].request.content)


# ---------------------------------------------------------------------------
# deliver_via_telegram
# ---------------------------------------------------------------------------


async def test_deliver_disabled_is_skipped(monkeypatch):
    monkeypatch.setattr(settings, "telegram_enabled", False, raising=False)
    before = _send_total("skipped", "reminder", "disabled")
    result = await telegram_bot.deliver_via_telegram("r1", "take meds")
    assert result == {"success": False, "skipped": True, "reason": "disabled"}
    assert _send_total("skipped", "reminder", "disabled") == before + 1


async def test_deliver_without_chat_id_is_skipped(tg_on, monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_chat_id", "", raising=False)
    result = await telegram_bot.deliver_via_telegram("r1", "take meds")
    assert result["skipped"] is True
    assert result["reason"] == "missing_chat_id"


async def test_deliver_sends_buttons(tg_on):
    with respx.mock:
        route = respx.post(f"{BOT}/sendMessage").mock(return_value=_tg_ok({"message_id": 7}))
        before = _send_total("ok", "reminder", "ok")
        result = await telegram_bot.deliver_via_telegram("r42", "stretch break")

    assert result["success"] is True
    assert _send_total("ok", "reminder", "ok") == before + 1
    body = _sent_json(route)
    assert body["chat_id"] == CHAT_ID
    assert "stretch break" in body["text"]
    buttons = body["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == "ack:r42"
    assert buttons[1]["callback_data"] == "snooze:r42:10"


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


async def test_message_from_unknown_chat_is_dropped(tg_on, client):
    with respx.mock:  # no routes mocked: any request would raise
        await telegram_bot._handle_message(client, _msg("hello", chat_id="9999"))
    # No exception = no outbound call was attempted.


async def test_help_command_replies_without_chat_relay(tg_on, client):
    with respx.mock:
        route = respx.post(f"{BOT}/sendMessage").mock(return_value=_tg_ok({"message_id": 1}))
        await telegram_bot._handle_message(client, _msg("/help"))
    assert route.called
    assert "Done / Snooze" in _sent_json(route)["text"]


def _mock_chat_relay(reply="ok", status=200):
    respx.post(f"{BOT}/sendChatAction").mock(return_value=_tg_ok(True))
    chat_response = {"choices": [{"message": {"role": "assistant", "content": reply}}]}
    relay = respx.post(f"{SELF_URL}/v1/chat/completions").mock(
        return_value=Response(200, json=chat_response) if status == 200 else Response(status, text="boom")
    )
    send = respx.post(f"{BOT}/sendMessage").mock(return_value=_tg_ok({"message_id": 2}))
    return relay, send


async def test_text_relays_through_chat_route(tg_on, client):
    with respx.mock:
        relay, send = _mock_chat_relay("Added to your list.")
        await telegram_bot._handle_message(client, _msg("add milk to the shopping list"))

    assert relay.called
    relay_body = _sent_json(relay)
    assert relay_body["messages"][-1] == {"role": "user", "content": "add milk to the shopping list"}
    assert relay.calls[0].request.headers["authorization"] == "Bearer test-api-token"
    assert _sent_json(send)["text"] == "Added to your list."
    # History committed on success
    assert len(telegram_bot._history[CHAT_ID]) == 2


async def test_history_capped_and_new_resets(tg_on, client):
    telegram_bot._history[CHAT_ID] = [{"role": "user", "content": str(i)} for i in range(10)]
    with respx.mock:
        _mock_chat_relay()
        await telegram_bot._handle_message(client, _msg("hi"))
        # capped to telegram_history_turns (6)
        assert len(telegram_bot._history[CHAT_ID]) == 6
        await telegram_bot._handle_message(client, _msg("/new"))
    assert CHAT_ID not in telegram_bot._history


async def test_relay_failure_not_committed_to_history(tg_on, client):
    with respx.mock:
        _, send = _mock_chat_relay(status=500)
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


def _mock_callback_api():
    answer = respx.post(f"{BOT}/answerCallbackQuery").mock(return_value=_tg_ok(True))
    edit = respx.post(f"{BOT}/editMessageText").mock(return_value=_tg_ok(True))
    return answer, edit


async def test_callback_ack_completes_reminder(tg_on, tmp_db, client):
    state_store.save_reminder("tg-r1", "take meds", "2026-07-05T10:00:00")
    with respx.mock:
        _mock_callback_api()
        await telegram_bot._handle_callback(client, _callback("ack:tg-r1"))
    assert state_store.get_reminder("tg-r1")["status"] == "completed"


async def test_callback_ack_is_idempotent(tg_on, tmp_db, client):
    state_store.save_reminder("tg-r2", "take meds", "2026-07-05T10:00:00")
    state_store.mark_reminder_acked("tg-r2", via="telegram")
    with respx.mock:
        answer, edit = _mock_callback_api()
        await telegram_bot._handle_callback(client, _callback("ack:tg-r2"))
    assert answer.called
    assert not edit.called  # replay: toast only, message untouched


async def test_callback_snooze_reopens_and_reschedules(tg_on, tmp_db, delivery_clean_scheduler, client):
    state_store.save_reminder("tg-r3", "stretch", "2026-07-05T10:00:00")
    state_store.complete_reminder("tg-r3")

    with respx.mock:
        _mock_callback_api()
        await telegram_bot._handle_callback(client, _callback("snooze:tg-r3:15"))

    rem = state_store.get_reminder("tg-r3")
    assert rem["status"] == "pending"
    assert rem["snooze_count"] == 1
    assert delivery_clean_scheduler.get_job("reminder_tg-r3") is not None


async def test_callback_snooze_respects_limit(tg_on, tmp_db, monkeypatch, client):
    monkeypatch.setattr(settings, "ntfy_max_snooze_count", 2, raising=False)
    state_store.save_reminder("tg-r4", "stretch", "2026-07-05T10:00:00")
    state_store.increment_snooze_count("tg-r4")
    state_store.increment_snooze_count("tg-r4")
    with respx.mock:
        answer = respx.post(f"{BOT}/answerCallbackQuery").mock(return_value=_tg_ok(True))
        await telegram_bot._handle_callback(client, _callback("snooze:tg-r4:10"))
    assert answer.called
    assert state_store.get_reminder("tg-r4")["snooze_count"] == 2  # unchanged


async def test_callback_from_unknown_chat_changes_nothing(tg_on, tmp_db, client):
    state_store.save_reminder("tg-r5", "take meds", "2026-07-05T10:00:00")
    with respx.mock:  # any outbound call would raise
        await telegram_bot._handle_callback(client, _callback("ack:tg-r5", chat_id="9999"))
    assert state_store.get_reminder("tg-r5")["status"] == "pending"


# ---------------------------------------------------------------------------
# poll cycle (_poll_once)
# ---------------------------------------------------------------------------


async def _drain_bg_tasks():
    if telegram_bot._bg_tasks:
        await asyncio.gather(*list(telegram_bot._bg_tasks), return_exceptions=True)


async def test_poll_once_advances_offset_and_dispatches(tg_on, client):
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
        offset, hint = await telegram_bot._poll_once(client, 0)
        assert (offset, hint) == (8, 0.0)  # max(update_id) + 1
        await _drain_bg_tasks()
        offset2, hint2 = await telegram_bot._poll_once(client, offset)

    # Second request carries the advanced offset.
    assert _sent_json(poll, 1)["offset"] == 8
    assert (offset2, hint2) == (8, 0.0)
    # The /help message in the batch was actually handled (dispatch ran).
    assert send.called


@pytest.mark.parametrize(
    "response,hint",
    [
        pytest.param(Response(409, json={"ok": False, "description": "Conflict"}), 60.0, id="409-long-sleep"),
        pytest.param(Response(500, text="oops"), -1.0, id="500-backoff"),
    ],
)
async def test_poll_once_error_keeps_offset_and_signals(tg_on, client, response, hint):
    with respx.mock:
        respx.post(f"{BOT}/getUpdates").mock(return_value=response)
        assert await telegram_bot._poll_once(client, 3) == (3, hint)


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


async def test_selfcare_nudge_medication_sends_with_done_button(tg_on):
    with respx.mock:
        route = respx.post(f"{BOT}/sendMessage").mock(return_value=_tg_ok({"message_id": 11}))
        result = await telegram_bot.send_selfcare_nudge("medication", "Hey, did you take your Vyvanse?")
    assert result["success"] is True
    body = _sent_json(route)
    assert "Vyvanse" in body["text"]
    assert body["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "sc:medication"


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


@pytest.mark.parametrize(
    "data,logged",
    [
        pytest.param("sc:medication", ["telegram:medication nudge"], id="medication-logged"),
        pytest.param("sc:rm_rf_slash", [], id="unknown-kind-rejected"),
    ],
)
async def test_callback_selfcare(tg_on, monkeypatch, client, data, logged):
    seen = []
    monkeypatch.setattr(sc, "record_medication_logged", lambda label: seen.append(label))
    with respx.mock:
        answer, _ = _mock_callback_api()
        await telegram_bot._handle_callback(client, _callback(data))
    assert seen == logged
    assert answer.called


async def test_dispatch_nudge_mirrors_to_telegram(tg_on, monkeypatch):
    async def _noop(*a, **k):
        return {"success": True}

    fired = []
    monkeypatch.setattr(sc, "_announce_voice", _noop)
    monkeypatch.setattr(sc, "_send_notification", _noop)
    monkeypatch.setattr(telegram_bot, "fire_selfcare_nudge", lambda kind, text: fired.append((kind, text)))

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
    monkeypatch.setattr(vh, "check_vision_health", AsyncMock(return_value=True))
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


def _patch_relay(monkeypatch, reply=None):
    relay = AsyncMock(return_value=reply)
    monkeypatch.setattr(telegram_bot, "_relay_locked", relay)
    return relay


# ----- _pick_image -----


@pytest.mark.parametrize(
    "msg,expected",
    [
        pytest.param(
            {
                "photo": [
                    {"file_id": "a", "file_size": 100, "width": 90},
                    {"file_id": "c", "file_size": 9000, "width": 1600},
                    {"file_id": "b", "file_size": 5000, "width": 1280},
                ]
            },
            ("c", "image/jpeg"),
            id="largest-photo",
        ),
        pytest.param(
            {"document": {"file_id": "doc1", "mime_type": "image/png"}}, ("doc1", "image/png"), id="image-doc"
        ),
        pytest.param({"document": {"file_id": "doc2", "mime_type": "application/pdf"}}, (None, ""), id="pdf-rejected"),
        pytest.param({}, (None, ""), id="empty"),
    ],
)
def test_pick_image(msg, expected):
    assert telegram_bot._pick_image(msg) == expected


# ----- _stt_reachable -----


@pytest.mark.parametrize(
    "outcome,reachable",
    [
        pytest.param(Response(200, json={}), True, id="200"),
        # A 404 on /v1/models still proves the server is answering.
        pytest.param(Response(404, text="not found"), True, id="404"),
        pytest.param(Response(503, text="down"), False, id="503"),
        pytest.param(httpx.ConnectError("refused"), False, id="connect-error"),
    ],
)
async def test_stt_reachable(voice_on, outcome, reachable):
    class _C:
        async def get(self, url, timeout=None):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    assert await telegram_bot._stt_reachable(_C()) is reachable


# ----- _handle_voice_impl -----


@pytest.mark.parametrize(
    "flag,value",
    [pytest.param("telegram_voice_enabled", False, id="voice-off"), pytest.param("stt_url", "", id="no-stt-url")],
)
async def test_voice_disabled_short_circuits(voice_on, monkeypatch, flag, value):
    monkeypatch.setattr(settings, flag, value, raising=False)
    sends = _patch_send(monkeypatch)
    downloaded = []
    monkeypatch.setattr(telegram_bot, "_download_file", lambda *a, **k: downloaded.append(1))

    before = _media_total("voice", "disabled")
    await telegram_bot._handle_voice_impl(_DummyClient(), CHAT_ID, _voice_msg())

    assert _media_total("voice", "disabled") == before + 1
    assert downloaded == []  # never attempted the download
    assert sends and "enabled" in sends[-1]["text"]


async def test_voice_too_long(voice_on, monkeypatch):
    _patch_send(monkeypatch)
    downloaded = []
    monkeypatch.setattr(telegram_bot, "_download_file", lambda *a, **k: downloaded.append(1))
    before = _media_total("voice", "too_long")
    await telegram_bot._handle_voice_impl(_DummyClient(), CHAT_ID, _voice_msg(duration=999))
    assert _media_total("voice", "too_long") == before + 1
    assert downloaded == []


async def test_voice_happy_path_transcribes_and_relays(voice_on, monkeypatch):
    sends = _patch_send(monkeypatch)
    monkeypatch.setattr(telegram_bot, "_download_file", AsyncMock(return_value=b"audio-bytes"))
    monkeypatch.setattr(telegram_bot, "_stt_reachable", AsyncMock(return_value=True))
    monkeypatch.setattr(telegram_bot, "_transcribe", AsyncMock(return_value="add milk to the list"))
    relay = _patch_relay(monkeypatch, "Added milk.")
    wake = AsyncMock()
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


@pytest.mark.parametrize("wake_enabled", [True, False], ids=["wake-then-give-up", "wake-disabled"])
async def test_voice_stt_unreachable(voice_on, monkeypatch, wake_enabled):
    monkeypatch.setattr(settings, "telegram_voice_wake_helios", wake_enabled, raising=False)
    sends = _patch_send(monkeypatch)
    monkeypatch.setattr(telegram_bot, "_download_file", AsyncMock(return_value=b"audio"))
    monkeypatch.setattr(telegram_bot, "_stt_reachable", AsyncMock(return_value=False))
    monkeypatch.setattr(telegram_bot, "_wait_for_stt", AsyncMock(return_value=False))
    relay = _patch_relay(monkeypatch)
    wake = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(helios_power, "wake_helios", wake)

    before = _media_total("voice", "stt_unreachable")
    await telegram_bot._handle_voice_impl(_DummyClient(), CHAT_ID, _voice_msg())

    assert _media_total("voice", "stt_unreachable") == before + 1
    relay.assert_not_awaited()  # never got to transcription
    if wake_enabled:
        wake.assert_awaited_once()
        assert any("Waking" in s["text"] for s in sends)  # "waking" notice sent
    else:
        wake.assert_not_awaited()


async def test_voice_empty_transcript(voice_on, monkeypatch):
    _patch_send(monkeypatch)
    monkeypatch.setattr(telegram_bot, "_download_file", AsyncMock(return_value=b"audio"))
    monkeypatch.setattr(telegram_bot, "_stt_reachable", AsyncMock(return_value=True))
    monkeypatch.setattr(telegram_bot, "_transcribe", AsyncMock(return_value=""))
    relay = _patch_relay(monkeypatch)

    before = _media_total("voice", "empty_transcript")
    await telegram_bot._handle_voice_impl(_DummyClient(), CHAT_ID, _voice_msg())

    assert _media_total("voice", "empty_transcript") == before + 1
    relay.assert_not_awaited()


# ----- _handle_photo_impl -----


@pytest.fixture
def photo_downloaded(photo_on, monkeypatch):
    """Photo enabled, the download succeeds, sends recorded."""
    monkeypatch.setattr(telegram_bot, "_download_file", AsyncMock(return_value=b"jpegbytes"))
    return _patch_send(monkeypatch)


async def test_photo_disabled(photo_on, monkeypatch):
    monkeypatch.setattr(settings, "vision_enabled", False, raising=False)
    _patch_send(monkeypatch)
    downloaded = []
    monkeypatch.setattr(telegram_bot, "_download_file", lambda *a, **k: downloaded.append(1))
    before = _media_total("photo", "disabled")
    await telegram_bot._handle_photo_impl(_DummyClient(), CHAT_ID, _photo_msg())
    assert _media_total("photo", "disabled") == before + 1
    assert downloaded == []


async def test_photo_happy_path_relays_with_caption(photo_downloaded, monkeypatch):
    monkeypatch.setattr(vh, "analyze_image", AsyncMock(return_value="A grocery receipt for milk and eggs."))
    relay = _patch_relay(monkeypatch, "Got it — added milk and eggs.")

    before = _media_total("photo", "ok")
    await telegram_bot._handle_photo_impl(_DummyClient(), CHAT_ID, _photo_msg(caption="from the store"))

    assert _media_total("photo", "ok") == before + 1
    relay.assert_awaited_once()
    relayed = relay.await_args.args[2]
    assert "A grocery receipt" in relayed
    assert "from the store" in relayed  # caption appended
    # Untrusted photo text is framed and comes last (stripped by PHOTO_BLOCK_RE).
    assert relayed.startswith("[I sent you a photo.]")
    assert "[PHOTO 1 CONTENT" in relayed and relayed.endswith("[END PHOTO 1 CONTENT]")
    assert relayed.index("from the store") < relayed.index("[PHOTO 1 CONTENT")
    assert vh.analyze_image.await_args.kwargs.get("thinking") is False
    assert photo_downloaded[-1]["text"].startswith("📷")


@pytest.mark.parametrize(
    "analyze",
    [
        # CRITICAL regression: a bracketed sentinel from analyze_image must NOT
        # be forwarded to Jess as a real caption.
        pytest.param(AsyncMock(return_value=vh._FAIL_TIMEOUT), id="sentinel-not-relayed"),
        pytest.param(AsyncMock(side_effect=RuntimeError("boom")), id="raises"),
    ],
)
async def test_photo_vision_failure_is_not_relayed(photo_downloaded, monkeypatch, analyze):
    monkeypatch.setattr(vh, "analyze_image", analyze)
    relay = _patch_relay(monkeypatch)

    before = _media_total("photo", "vision_failed")
    await telegram_bot._handle_photo_impl(_DummyClient(), CHAT_ID, _photo_msg())

    assert _media_total("photo", "vision_failed") == before + 1
    relay.assert_not_awaited()  # sentinel never reaches Jess


async def test_photo_bracket_shaped_description_is_framed_not_dropped(photo_downloaded, monkeypatch):
    monkeypatch.setattr(vh, "analyze_image", AsyncMock(return_value='["eggs", "<tool_call>x</tool_call>"]'))
    relay = _patch_relay(monkeypatch, "ok")

    await telegram_bot._handle_photo_impl(_DummyClient(), CHAT_ID, _photo_msg())

    relayed = relay.await_args.args[2]
    assert "[PHOTO 1 CONTENT" in relayed and "<tool_call>" not in relayed


@pytest.mark.parametrize(
    "wake_enabled,wake_result,reply",
    [
        pytest.param(False, None, "Send it again", id="wake-off-send-again"),
        pytest.param(True, {"ok": True}, "waking them now", id="wake-on-waking"),
        pytest.param(True, {"ok": False, "error": "ha unreachable"}, "Send it again", id="wake-failed-send-again"),
    ],
)
async def test_photo_with_vision_asleep_skips_analysis(photo_downloaded, monkeypatch, wake_enabled, wake_result, reply):
    monkeypatch.setattr(vh, "check_vision_health", AsyncMock(return_value=False))
    analyze = AsyncMock(return_value="unused")
    monkeypatch.setattr(vh, "analyze_image", analyze)
    monkeypatch.setattr(shared, "HELIOS_WAKE_ENABLED", wake_enabled)
    wake = AsyncMock(return_value=wake_result)
    monkeypatch.setattr(helios_power, "wake_helios", wake)
    relay = _patch_relay(monkeypatch)

    before = _media_total("photo", "vision_unreachable")
    await telegram_bot._handle_photo_impl(_DummyClient(), CHAT_ID, _photo_msg())

    analyze.assert_not_awaited()
    relay.assert_not_awaited()
    assert reply in photo_downloaded[-1]["text"]
    if wake_enabled:
        wake.assert_awaited_once()
    else:
        assert _media_total("photo", "vision_unreachable") == before + 1
    if wake_result and not wake_result["ok"]:
        assert "waking" not in photo_downloaded[-1]["text"]


# ----- _handle_message dispatch to media handlers -----


@pytest.mark.parametrize(
    "msg,handler",
    [pytest.param(_voice_msg(), "_handle_voice", id="voice"), pytest.param(_photo_msg(), "_handle_photo", id="photo")],
)
async def test_dispatch_media_message_routes_to_its_handler(tg_on, monkeypatch, msg, handler):
    handlers = {"_handle_voice": AsyncMock(), "_handle_photo": AsyncMock()}
    for name, mock in handlers.items():
        monkeypatch.setattr(telegram_bot, name, mock)

    await telegram_bot._handle_message(_DummyClient(), msg)

    for name, mock in handlers.items():
        assert mock.await_count == (1 if name == handler else 0)


async def test_drain_pending_awaits_inflight_handlers():
    """On shutdown, _drain_pending lets an already-dispatched handler finish
    (so a deploy doesn't drop e.g. a Done-tap ack)."""
    flag: dict = {}

    async def slow_handler():
        await asyncio.sleep(0.05)
        flag["done"] = True

    t = asyncio.create_task(slow_handler())
    telegram_bot._bg_tasks.add(t)
    t.add_done_callback(telegram_bot._bg_tasks.discard)

    await telegram_bot._drain_pending(timeout=1.0)
    assert flag.get("done") is True
    assert t not in telegram_bot._bg_tasks


# ---------------------------------------------------------------------------
# spoken walkie-talkie reply (feat/telegram-voice-reply)
# ---------------------------------------------------------------------------


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
    client = MagicMock()
    client.post = post_mock
    return client


# ----- _synthesize_speech -----


async def test_synthesize_speech_returns_bytes_on_200(voice_reply_on):
    post = AsyncMock(return_value=MagicMock(status_code=200, content=b"mp3-bytes"))

    audio = await telegram_bot._synthesize_speech(_mock_client_with_post(post), "hello there")
    assert audio == b"mp3-bytes"
    # Posts to the OpenAI-compatible speech endpoint requesting mp3.
    assert post.await_args.args[0] == f"{TTS_URL}/v1/audio/speech"
    assert post.await_args.kwargs["json"]["response_format"] == "mp3"
    assert post.await_args.kwargs["json"]["input"] == "hello there"


@pytest.mark.parametrize(
    "post",
    [
        pytest.param(AsyncMock(return_value=MagicMock(status_code=200, content=b"")), id="empty-content"),
        pytest.param(AsyncMock(return_value=MagicMock(status_code=500, content=b"boom", text="boom")), id="non-200"),
        pytest.param(AsyncMock(side_effect=RuntimeError("connect refused")), id="exception"),
    ],
)
async def test_synthesize_speech_failure_is_none(voice_reply_on, post):
    assert await telegram_bot._synthesize_speech(_mock_client_with_post(post), "hi") is None


# ----- _send_voice_reply -----


def _tg_resp(status_code: int, payload: dict) -> MagicMock:
    resp = MagicMock(status_code=status_code, content=json.dumps(payload).encode())
    resp.json.return_value = payload
    return resp


async def test_send_voice_reply_true_on_ok(voice_reply_on):
    post = AsyncMock(return_value=_tg_resp(200, {"ok": True}))

    assert await telegram_bot._send_voice_reply(_mock_client_with_post(post), CHAT_ID, b"mp3") is True
    # Multipart file upload of the mp3.
    assert "files" in post.await_args.kwargs
    assert post.await_args.kwargs["data"]["chat_id"] == CHAT_ID


@pytest.mark.parametrize(
    "post",
    [
        pytest.param(AsyncMock(return_value=_tg_resp(200, {"ok": False, "description": "bad"})), id="ok-false"),
        pytest.param(AsyncMock(return_value=_tg_resp(400, {"ok": False, "description": "Bad Request"})), id="400"),
        pytest.param(AsyncMock(side_effect=RuntimeError("boom")), id="exception"),
    ],
)
async def test_send_voice_reply_false_on_failure(voice_reply_on, post):
    assert await telegram_bot._send_voice_reply(_mock_client_with_post(post), CHAT_ID, b"mp3") is False


# ----- _maybe_send_voice_reply -----


@pytest.mark.parametrize(
    "flag,value,reply",
    [
        pytest.param("telegram_voice_reply_enabled", False, "hi there", id="disabled"),
        pytest.param("tts_url", "", "hi there", id="no-tts-url"),
        pytest.param(None, None, "   \n\t ", id="blank-reply"),
    ],
)
async def test_maybe_voice_reply_skips_without_synth_or_metric(voice_reply_on, monkeypatch, flag, value, reply):
    if flag:
        monkeypatch.setattr(settings, flag, value, raising=False)
    synth = AsyncMock()
    monkeypatch.setattr(telegram_bot, "_synthesize_speech", synth)

    before = {r: _media_total("voice_reply", r) for r in ("ok", "tts_failed")}
    await telegram_bot._maybe_send_voice_reply(_DummyClient(), CHAT_ID, reply)

    synth.assert_not_awaited()
    assert {r: _media_total("voice_reply", r) for r in before} == before  # no metric bumped


@pytest.mark.parametrize(
    "audio,sent_ok,result",
    [
        pytest.param(b"mp3-bytes", True, "ok", id="ok"),
        pytest.param(None, None, "tts_failed", id="tts-failed-no-send"),
        pytest.param(b"mp3-bytes", False, "send_failed", id="send-failed"),
    ],
)
async def test_maybe_voice_reply_outcome_metric(voice_reply_on, monkeypatch, audio, sent_ok, result):
    synth = AsyncMock(return_value=audio)
    send = AsyncMock(return_value=sent_ok)
    monkeypatch.setattr(telegram_bot, "_synthesize_speech", synth)
    monkeypatch.setattr(telegram_bot, "_send_voice_reply", send)

    before = _media_total("voice_reply", result)
    await telegram_bot._maybe_send_voice_reply(_DummyClient(), CHAT_ID, "all done")

    assert _media_total("voice_reply", result) == before + 1
    synth.assert_awaited_once()
    if audio is None:
        send.assert_not_awaited()  # no audio -> never attempt sendVoice
    else:
        send.assert_awaited_once()
        assert send.await_args.args[2] == audio  # synthesized audio handed to sender


async def test_maybe_voice_reply_truncates_to_max_chars(voice_reply_on, monkeypatch):
    monkeypatch.setattr(settings, "telegram_voice_reply_max_chars", 50, raising=False)
    synth = AsyncMock(return_value=b"mp3-bytes")
    monkeypatch.setattr(telegram_bot, "_synthesize_speech", synth)
    monkeypatch.setattr(telegram_bot, "_send_voice_reply", AsyncMock(return_value=True))

    await telegram_bot._maybe_send_voice_reply(_DummyClient(), CHAT_ID, "x" * 500)

    spoken = synth.await_args.args[1]
    assert len(spoken) <= 51  # 50 chars + the ellipsis
    assert spoken.endswith("…")
