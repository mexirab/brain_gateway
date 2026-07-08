"""
Telegram bot — two-way capture + reminder delivery channel.

The away-from-home capture surface (ROADMAP Tier 1). A single long-polling
background task inside the orchestrator:

- Inbound text from the allow-listed chat goes through the orchestrator's
  own `/v1/chat/completions` route (self-HTTP with the Bearer token), so a
  Telegram message gets the exact same Jess as the voice pucks and web chat:
  mode router, fast-path, tools, backlog capture — everything.
- Reminders are delivered as messages with inline **Done** / **Snooze**
  buttons. Button taps arrive as `callback_query` updates and are handled
  in-process by replicating the F-011 ack/snooze route semantics directly
  (state_store + scheduler + selfcare bridge). No HMAC needed here: unlike
  the ntfy/Pushover callback URLs, which traverse untrusted channels, a
  callback_query only reaches us through Telegram's API from a chat we
  already allow-listed — the chat-ID check IS the auth boundary.

Long-polling means outbound HTTPS only — no webhook, no public ingress.

Security posture:
- Every update is dropped unless its chat ID is in `telegram_allowed_chat_id`.
  Unknown chats are logged (ID only, never message content) so the owner can
  discover their chat ID during setup, rate-limited to once per minute.
- Replies are sent as plain text (no parse_mode): Telegram's Markdown parser
  rejects unbalanced entities, and LLM output regularly contains them; plain
  text can neither fail to send nor smuggle formatting-based spoofs.

Conversation history is RAM-only by design (like task_decomposition): a
rolling window per chat, cleared with /new. Durable memory lives in the
gateway (RAG, tasks, reminders), not in this transport.

`deliver_via_telegram` follows the pushover_manager contract: never raises,
returns a dict, increments `TELEGRAM_SEND_TOTAL{result, kind, reason}`
exactly once per exit path.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from orchestrator.config import settings as _settings

logger = logging.getLogger(__name__)

# Rolling per-chat history: chat_id -> list[{"role": ..., "content": ...}].
# RAM-only by design; capped at telegram_history_turns entries.
_history: Dict[str, List[Dict[str, str]]] = {}

# Rate-limit for unknown-chat log lines: chat_id -> last-logged monotonic ts.
_denied_log_ts: Dict[str, float] = {}
_DENIED_LOG_INTERVAL = 60.0

# Strong refs to in-flight update-handler tasks (bare create_task results can
# be garbage-collected mid-flight).
_bg_tasks: set = set()

# Per-chat lock: serializes message handling within one chat so the RAM
# history stays ordered, while button taps and other chats run concurrently.
_chat_locks: Dict[str, asyncio.Lock] = {}

_TELEGRAM_MSG_LIMIT = 4096
_CHUNK_AT = 4000

_HELP_TEXT = (
    "Hey, it's Jess. Text me like you would at home:\n"
    '• "add milk to the shopping list"\n'
    '• "remind me Thursday 3pm to call the dentist"\n'
    '• "what\'s on my calendar tomorrow?"\n'
    '• "what should I do right now?"\n'
    "• or just brain-dump a paragraph and I'll sort it.\n\n"
    "Commands: /new — start a fresh conversation, /help — this message.\n"
    "Reminders show up here with Done / Snooze buttons."
)


def _api_base() -> str:
    return f"{_settings.telegram_api_base.rstrip('/')}/bot{_settings.telegram_bot_token}"


def _redact(text: str) -> str:
    """Strip the bot token from a string headed for a log line.

    Telegram's API design puts the token in the URL path, so httpx exception
    messages (which embed the request URL) would otherwise leak it.
    """
    tok = _settings.telegram_bot_token
    return text.replace(tok, "<bot-token>") if tok else text


def _allowed_chat_ids() -> set:
    return {c.strip() for c in _settings.telegram_allowed_chat_id.split(",") if c.strip()}


def _chat_allowed(chat_id: str) -> bool:
    return chat_id in _allowed_chat_ids()


def _log_denied(chat_id: str, kind: str) -> None:
    """Log an unknown chat's ID (never its content), max once/min per chat.

    This doubles as the setup flow: with TELEGRAM_ALLOWED_CHAT_ID unset,
    message the bot once and read your chat ID out of the orchestrator log.
    """
    now = time.monotonic()
    if now - _denied_log_ts.get(chat_id, 0.0) >= _DENIED_LOG_INTERVAL:
        _denied_log_ts[chat_id] = now
        logger.warning(
            f"[TELEGRAM] Dropped {kind} from non-allowlisted chat {chat_id} "
            f"(add to TELEGRAM_ALLOWED_CHAT_ID to authorize)"
        )


def _chunk_text(text: str) -> List[str]:
    """Split a reply into <=4096-char Telegram messages, preferring newlines."""
    if len(text) <= _TELEGRAM_MSG_LIMIT:
        return [text]
    chunks: List[str] = []
    rest = text
    while len(rest) > _TELEGRAM_MSG_LIMIT:
        cut = rest.rfind("\n", 0, _CHUNK_AT)
        if cut < _CHUNK_AT // 2:
            cut = _CHUNK_AT
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest:
        chunks.append(rest)
    return chunks


async def _tg_call(
    client: httpx.AsyncClient, method: str, payload: Dict[str, Any], timeout: Optional[float] = None
) -> Dict[str, Any]:
    """POST a Telegram Bot API method. Returns the parsed body; never raises."""
    try:
        resp = await client.post(
            f"{_api_base()}/{method}",
            json=payload,
            timeout=timeout if timeout is not None else 15.0,
        )
        body = resp.json() if resp.content else {}
        if resp.status_code != 200 or not body.get("ok", False):
            desc = str(body.get("description", ""))[:200]
            logger.warning(f"[TELEGRAM] {method} -> {resp.status_code}: {desc}")
            return {"ok": False, "status_code": resp.status_code, "description": desc}
        return body
    except Exception as e:
        logger.warning(f"[TELEGRAM] {method} failed: {type(e).__name__}: {_redact(str(e))}")
        return {"ok": False, "error": f"{type(e).__name__}: {_redact(str(e))}"}


async def _send_text(
    client: httpx.AsyncClient,
    chat_id: str,
    text: str,
    kind: str,
    reply_markup: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """sendMessage with chunking + metrics. Returns the LAST send's result."""
    from orchestrator.metrics import TELEGRAM_SEND_LATENCY, TELEGRAM_SEND_TOTAL

    result: Dict[str, Any] = {"ok": False, "error": "empty"}
    chunks = _chunk_text(text) if text else [""]
    t0 = time.time()
    for i, chunk in enumerate(chunks):
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": chunk or "…"}
        # Buttons only on the final chunk.
        if reply_markup and i == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        result = await _tg_call(client, "sendMessage", payload)
        if not result.get("ok"):
            break
    TELEGRAM_SEND_LATENCY.labels(kind=kind).observe(time.time() - t0)
    if result.get("ok"):
        TELEGRAM_SEND_TOTAL.labels(result="ok", kind=kind, reason="ok").inc()
    else:
        reason = "http_error" if result.get("status_code") else "network_error"
        TELEGRAM_SEND_TOTAL.labels(result="fail", kind=kind, reason=reason).inc()
    return result


# ---------------------------------------------------------------------------
# Reminder delivery channel (called from deliver_reminder_job)
# ---------------------------------------------------------------------------


async def deliver_via_telegram(reminder_id: str, text: str) -> Dict[str, Any]:
    """Deliver a reminder to the allow-listed chat(s) with Done/Snooze buttons.

    Mirrors deliver_via_pushover's contract: never raises, returns a dict,
    one TELEGRAM_SEND_TOTAL increment per exit path.
    """
    from orchestrator.metrics import TELEGRAM_SEND_TOTAL

    if not _settings.telegram_enabled:
        TELEGRAM_SEND_TOTAL.labels(result="skipped", kind="reminder", reason="disabled").inc()
        return {"success": False, "skipped": True, "reason": "disabled"}
    if not _settings.telegram_bot_token:
        TELEGRAM_SEND_TOTAL.labels(result="skipped", kind="reminder", reason="missing_token").inc()
        return {"success": False, "skipped": True, "reason": "missing_token"}
    chat_ids = _allowed_chat_ids()
    if not chat_ids:
        TELEGRAM_SEND_TOTAL.labels(result="skipped", kind="reminder", reason="missing_chat_id").inc()
        return {"success": False, "skipped": True, "reason": "missing_chat_id"}

    snooze_min = _settings.telegram_snooze_minutes
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✓ Done", "callback_data": f"ack:{reminder_id}"},
                {"text": f"\U0001f4a4 Snooze {snooze_min}m", "callback_data": f"snooze:{reminder_id}:{snooze_min}"},
            ]
        ]
    }

    ok_any = False
    async with httpx.AsyncClient(timeout=15.0) as client:
        for chat_id in sorted(chat_ids):
            result = await _send_text(client, chat_id, f"⏰ {text}", kind="reminder", reply_markup=keyboard)
            ok_any = ok_any or bool(result.get("ok"))

    if ok_any:
        logger.info(f"[TELEGRAM] Delivered reminder {reminder_id} ({len(text)} chars)")
        return {"success": True}
    return {"success": False, "error": "send_failed"}


async def send_system_message(text: str) -> Dict[str, Any]:
    """Send a plain system message (no buttons) to all allow-listed chats.

    Used by the trust layer's morning missed-reminder recap. Same contract
    as deliver_via_telegram: never raises, silently no-ops when the bot is
    disabled or unconfigured.
    """
    from orchestrator.metrics import TELEGRAM_SEND_TOTAL

    if not _settings.telegram_enabled:
        TELEGRAM_SEND_TOTAL.labels(result="skipped", kind="system", reason="disabled").inc()
        return {"success": False, "skipped": True, "reason": "disabled"}
    if not _settings.telegram_bot_token or not _allowed_chat_ids():
        TELEGRAM_SEND_TOTAL.labels(result="skipped", kind="system", reason="missing_chat_id").inc()
        return {"success": False, "skipped": True, "reason": "unconfigured"}

    ok_any = False
    async with httpx.AsyncClient(timeout=15.0) as client:
        for chat_id in sorted(_allowed_chat_ids()):
            result = await _send_text(client, chat_id, text, kind="system")
            ok_any = ok_any or bool(result.get("ok"))
    return {"success": ok_any}


def fire_system_message(text: str) -> None:
    """Fire-and-forget send_system_message with a strong task ref.

    A bare create_task result can be garbage-collected mid-flight; this
    parks the task in _bg_tasks (same guard the update dispatcher uses).
    """
    task = asyncio.create_task(send_system_message(text))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# Selfcare nudge kinds → the record function names the sc: callback maps to.
# Keys match the F-011 selfcare-bridge vocabulary (infer_selfcare_action).
SELFCARE_KINDS = ("medication", "meal", "water", "movement")


def _selfcare_nudge_kinds() -> set:
    """Kinds allowed to push to Telegram (TELEGRAM_SELFCARE_NUDGES)."""
    raw = _settings.telegram_selfcare_nudges.strip().lower()
    if raw == "all":
        return set(SELFCARE_KINDS)
    return {k.strip() for k in raw.split(",") if k.strip() in SELFCARE_KINDS}


async def send_selfcare_nudge(kind: str, text: str) -> Dict[str, Any]:
    """Mirror a selfcare nudge (F-008) to Telegram with a one-tap ✓ Done
    button that logs the action (sc:<kind> callback).

    Kind-gated: only kinds listed in TELEGRAM_SELFCARE_NUDGES push (default
    just `medication` — meds are the high-stakes nudge; hourly movement/
    hydration pings would be phone spam). Never raises.
    """
    from orchestrator.metrics import TELEGRAM_SEND_TOTAL

    if not _settings.telegram_enabled:
        TELEGRAM_SEND_TOTAL.labels(result="skipped", kind="nudge", reason="disabled").inc()
        return {"success": False, "skipped": True, "reason": "disabled"}
    if kind not in _selfcare_nudge_kinds():
        TELEGRAM_SEND_TOTAL.labels(result="skipped", kind="nudge", reason="kind_not_enabled").inc()
        return {"success": False, "skipped": True, "reason": "kind_not_enabled"}
    if not _settings.telegram_bot_token or not _allowed_chat_ids():
        TELEGRAM_SEND_TOTAL.labels(result="skipped", kind="nudge", reason="missing_chat_id").inc()
        return {"success": False, "skipped": True, "reason": "unconfigured"}

    keyboard = {"inline_keyboard": [[{"text": "✓ Done", "callback_data": f"sc:{kind}"}]]}
    ok_any = False
    async with httpx.AsyncClient(timeout=15.0) as client:
        for chat_id in sorted(_allowed_chat_ids()):
            result = await _send_text(
                client, chat_id, f"💊 {text}" if kind == "medication" else text, kind="nudge", reply_markup=keyboard
            )
            ok_any = ok_any or bool(result.get("ok"))
    return {"success": ok_any}


def fire_selfcare_nudge(kind: str, text: str) -> None:
    """Fire-and-forget send_selfcare_nudge with a strong task ref."""
    task = asyncio.create_task(send_selfcare_nudge(kind, text))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# ---------------------------------------------------------------------------
# Inbound: chat relay
# ---------------------------------------------------------------------------


async def _ask_jess(chat_id: str, text: str) -> str:
    """Relay a message through the orchestrator's own chat route.

    Self-HTTP (not a direct cloud_brain call) so Telegram traffic gets the
    identical path to every other client: auth middleware aside, that's the
    mode router, fast-path, tool loop, and any future routing improvements.
    """
    history = _history.setdefault(chat_id, [])
    messages = [*history, {"role": "user", "content": text}]

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
            resp = await client.post(
                f"{_settings.telegram_self_url.rstrip('/')}/v1/chat/completions",
                headers={"Authorization": f"Bearer {_settings.api_token}"},
                json={"messages": messages, "stream": False},
            )
        if resp.status_code != 200:
            logger.error(f"[TELEGRAM] Chat route returned {resp.status_code}: {resp.text[:200]}")
            return "Something went wrong on my end — the gateway answered with an error. Try again in a minute."
        data = resp.json()
        reply = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    except httpx.TimeoutException:
        logger.error("[TELEGRAM] Chat route timed out")
        return "That took too long — the model may still be waking up. Give it a minute and ask me again."
    except Exception as e:
        logger.error(f"[TELEGRAM] Chat relay failed: {type(e).__name__}: {e}")
        return "I couldn't reach the gateway just now. Try again in a minute."

    # Only commit to history on success, and cap the window.
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    max_len = max(2, _settings.telegram_history_turns)
    if len(history) > max_len:
        del history[: len(history) - max_len]
    return reply or "(no reply)"


async def _handle_message(client: httpx.AsyncClient, msg: Dict[str, Any]) -> None:
    from orchestrator.metrics import TELEGRAM_UPDATE_TOTAL

    chat_id = str(msg.get("chat", {}).get("id", ""))
    if not _chat_allowed(chat_id):
        _log_denied(chat_id, "message")
        TELEGRAM_UPDATE_TOTAL.labels(kind="message", result="denied").inc()
        return

    text = (msg.get("text") or "").strip()
    if not text:
        TELEGRAM_UPDATE_TOTAL.labels(kind="message", result="ignored").inc()
        await _send_text(
            client,
            chat_id,
            "I can only handle text so far — voice notes and photos are on the roadmap.",
            kind="system",
        )
        return

    if text.startswith("/start") or text.startswith("/help"):
        TELEGRAM_UPDATE_TOTAL.labels(kind="message", result="ok").inc()
        await _send_text(client, chat_id, _HELP_TEXT, kind="system")
        return
    if text.startswith("/new") or text.startswith("/reset"):
        _history.pop(chat_id, None)
        TELEGRAM_UPDATE_TOTAL.labels(kind="message", result="ok").inc()
        await _send_text(client, chat_id, "Fresh start — what's up?", kind="system")
        return

    # Serialize the relay per chat so concurrent dispatch (see _dispatch)
    # can't interleave one chat's history; other chats and button taps
    # proceed unblocked.
    lock = _chat_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        # Best-effort typing indicator while the tool loop runs.
        await _tg_call(client, "sendChatAction", {"chat_id": chat_id, "action": "typing"})
        reply = await _ask_jess(chat_id, text)
        TELEGRAM_UPDATE_TOTAL.labels(kind="message", result="ok").inc()
        await _send_text(client, chat_id, reply, kind="chat")


# ---------------------------------------------------------------------------
# Inbound: Done / Snooze button taps
# ---------------------------------------------------------------------------


async def _answer_callback(client: httpx.AsyncClient, callback_id: str, text: str) -> None:
    await _tg_call(client, "answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})


async def _strip_buttons(client: httpx.AsyncClient, cb: Dict[str, Any], suffix: str) -> None:
    """Rewrite the reminder message to show its outcome and drop the buttons."""
    message = cb.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    if chat_id is None or message_id is None:
        return
    original = message.get("text") or ""
    await _tg_call(
        client,
        "editMessageText",
        {"chat_id": chat_id, "message_id": message_id, "text": f"{original}\n\n{suffix}"},
    )


async def _handle_callback(client: httpx.AsyncClient, cb: Dict[str, Any]) -> None:
    """Replicates api_routes.ntfy_ack_reminder / ntfy_snooze_reminder minus
    the HTTP+HMAC layer — the allowlist check is the auth boundary here."""
    from orchestrator import shared, state_store
    from orchestrator.metrics import TELEGRAM_CALLBACK_TOTAL, TELEGRAM_UPDATE_TOTAL

    callback_id = str(cb.get("id", ""))
    chat_id = str((cb.get("message") or {}).get("chat", {}).get("id", ""))
    if not _chat_allowed(chat_id):
        _log_denied(chat_id, "callback")
        TELEGRAM_UPDATE_TOTAL.labels(kind="callback", result="denied").inc()
        return
    TELEGRAM_UPDATE_TOTAL.labels(kind="callback", result="ok").inc()

    data = str(cb.get("data") or "")
    parts = data.split(":")
    action = parts[0] if parts else ""

    if action == "ack" and len(parts) == 2:
        reminder_id = parts[1]
        result = state_store.mark_reminder_acked(reminder_id, via="telegram")
        if result is None:
            TELEGRAM_CALLBACK_TOTAL.labels(action="ack", result="not_found").inc()
            await _answer_callback(client, callback_id, "That reminder no longer exists.")
            return
        if result.get("already_acked"):
            TELEGRAM_CALLBACK_TOTAL.labels(action="ack", result="already").inc()
            await _answer_callback(client, callback_id, "✓ Already done")
            return

        # Cancel any pending TTS-failure retry, same as the ntfy ack route.
        scheduler = shared.scheduler
        retry_job_id = f"reminder_{reminder_id}_retry"
        try:
            if scheduler and scheduler.get_job(retry_job_id):
                scheduler.remove_job(retry_job_id)
        except Exception as job_err:
            logger.warning(f"[TELEGRAM-ACK] Failed to cancel retry job: {job_err}")

        # Selfcare bridge — identical inference to the ntfy route.
        text = result.get("text", "") or ""
        try:
            from orchestrator.reminder_manager import infer_selfcare_action_from_text

            sc_action = infer_selfcare_action_from_text(text)
            if sc_action:
                from orchestrator import selfcare_manager

                label = f"reminder:{text[:80]}"
                if sc_action == "medication":
                    selfcare_manager.record_medication_logged(label)
                elif sc_action == "meal":
                    selfcare_manager.record_meal_logged(label)
                elif sc_action == "water":
                    selfcare_manager.record_hydration_logged(label)
                elif sc_action == "movement":
                    selfcare_manager.record_movement_logged(label)
        except Exception as bridge_err:
            logger.error(f"[TELEGRAM-ACK] Selfcare bridge failed for {reminder_id}: {bridge_err}", exc_info=True)

        TELEGRAM_CALLBACK_TOTAL.labels(action="ack", result="ok").inc()
        logger.info(f"[TELEGRAM-ACK] {reminder_id} acked")
        await _answer_callback(client, callback_id, "✓ Done")
        await _strip_buttons(client, cb, "✓ Done")
        return

    if action == "snooze" and len(parts) == 3:
        reminder_id = parts[1]
        try:
            minutes = max(1, min(120, int(parts[2])))
        except ValueError:
            minutes = _settings.telegram_snooze_minutes

        reminder = state_store.get_reminder(reminder_id)
        if reminder is None:
            TELEGRAM_CALLBACK_TOTAL.labels(action="snooze", result="not_found").inc()
            await _answer_callback(client, callback_id, "That reminder no longer exists.")
            return
        current = reminder.get("snooze_count") or 0
        if current >= _settings.ntfy_max_snooze_count:
            TELEGRAM_CALLBACK_TOTAL.labels(action="snooze", result="limit").inc()
            await _answer_callback(client, callback_id, f"Snooze limit reached ({current}).")
            return

        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        from orchestrator import shared as _shared
        from orchestrator.tool_handlers import deliver_reminder_job

        run_at = datetime.now(ZoneInfo(_shared.TIMEZONE)) + timedelta(minutes=minutes)
        scheduler = _shared.scheduler
        try:
            scheduler.add_job(
                deliver_reminder_job,
                trigger="date",
                run_date=run_at,
                args=[reminder_id],
                id=f"reminder_{reminder_id}",
                replace_existing=True,
            )
        except Exception as sch_err:
            logger.error(f"[TELEGRAM-SNOOZE] Reschedule failed for {reminder_id}: {sch_err}", exc_info=True)
            TELEGRAM_CALLBACK_TOTAL.labels(action="snooze", result="error").inc()
            await _answer_callback(client, callback_id, "Couldn't snooze — rescheduling failed.")
            return

        # Same reset semantics as the ntfy snooze route: delivery marked the
        # reminder completed, and deliver_reminder_job skips non-pending.
        state_store.reopen_reminder(reminder_id)

        retry_job_id = f"reminder_{reminder_id}_retry"
        try:
            if scheduler.get_job(retry_job_id):
                scheduler.remove_job(retry_job_id)
        except Exception:
            pass

        new_count = state_store.increment_snooze_count(reminder_id)
        TELEGRAM_CALLBACK_TOTAL.labels(action="snooze", result="ok").inc()
        fire_time = run_at.strftime("%-I:%M %p")
        logger.info(f"[TELEGRAM-SNOOZE] {reminder_id} snoozed {minutes}m (count={new_count})")
        await _answer_callback(client, callback_id, f"\U0001f4a4 Snoozed until {fire_time}")
        await _strip_buttons(client, cb, f"\U0001f4a4 Snoozed until {fire_time}")
        return

    if action == "sc" and len(parts) == 2:
        # One-tap selfcare log from a nudge (sc:<kind>). Same auth boundary
        # as ack/snooze; kind is validated against the fixed vocabulary, so
        # arbitrary callback_data from the (allow-listed) chat can't reach
        # anything else.
        kind = parts[1]
        if kind not in SELFCARE_KINDS:
            TELEGRAM_CALLBACK_TOTAL.labels(action="selfcare", result="error").inc()
            await _answer_callback(client, callback_id, "Unknown action.")
            return
        try:
            from orchestrator import selfcare_manager

            label = f"telegram:{kind} nudge"
            if kind == "medication":
                selfcare_manager.record_medication_logged(label)
            elif kind == "meal":
                selfcare_manager.record_meal_logged(label)
            elif kind == "water":
                selfcare_manager.record_hydration_logged(label)
            elif kind == "movement":
                selfcare_manager.record_movement_logged(label)
        except Exception as sc_err:
            logger.error(f"[TELEGRAM-SC] Selfcare log failed for {kind}: {sc_err}", exc_info=True)
            TELEGRAM_CALLBACK_TOTAL.labels(action="selfcare", result="error").inc()
            await _answer_callback(client, callback_id, "Couldn't log it — try by voice.")
            return
        TELEGRAM_CALLBACK_TOTAL.labels(action="selfcare", result="ok").inc()
        logger.info(f"[TELEGRAM-SC] {kind} logged via nudge button")
        await _answer_callback(client, callback_id, "✓ Logged")
        await _strip_buttons(client, cb, "✓ Logged")
        return

    TELEGRAM_CALLBACK_TOTAL.labels(action="unknown", result="error").inc()
    await _answer_callback(client, callback_id, "Unknown action.")


# ---------------------------------------------------------------------------
# The long-poll loop
# ---------------------------------------------------------------------------


async def _handle_update(client: httpx.AsyncClient, update: Dict[str, Any]) -> None:
    """Dispatch one update; guarded so a poison update can't kill the loop."""
    from orchestrator.metrics import TELEGRAM_UPDATE_TOTAL

    try:
        if "message" in update:
            await _handle_message(client, update["message"])
        elif "callback_query" in update:
            await _handle_callback(client, update["callback_query"])
        else:
            TELEGRAM_UPDATE_TOTAL.labels(kind="other", result="ignored").inc()
    except Exception as e:
        TELEGRAM_UPDATE_TOTAL.labels(kind="message" if "message" in update else "callback", result="error").inc()
        logger.error(f"[TELEGRAM] Update handling failed: {type(e).__name__}: {e}", exc_info=True)


def _dispatch(client: httpx.AsyncClient, update: Dict[str, Any]) -> None:
    """Fire-and-forget an update handler so a slow chat relay (up to 180s in
    _ask_jess) never blocks a Done/Snooze tap sitting behind it in the same
    batch. _handle_update never raises; per-chat ordering is preserved by the
    lock in _handle_message."""
    task = asyncio.create_task(_handle_update(client, update))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# Persisted getUpdates offset. Telegram retains unconfirmed updates for ~24h,
# so a bot that restarts with offset=0 (the old behavior) re-fetches and
# re-processes recent updates — re-sending old reminders/nudges/replies. On a
# day with several deploys that means duplicate meds pings the user never
# re-triggered. Persisting the offset makes a restart resume exactly where it
# left off.
_OFFSET_STATE_KEY = "telegram_update_offset"


def _load_offset() -> int:
    """Restore the last-acknowledged getUpdates offset (0 on first run / bad value)."""
    from orchestrator import state_store

    try:
        raw = state_store.get_app_state(_OFFSET_STATE_KEY)
        return int(raw) if raw else 0
    except (ValueError, TypeError):
        return 0
    except Exception as e:  # noqa: BLE001 — a bad state read must not sink the bot
        logger.warning("[TELEGRAM] Could not load persisted offset: %s", e)
        return 0


def _save_offset(offset: int) -> None:
    """Persist the offset so a restart doesn't re-deliver old updates."""
    from orchestrator import state_store
    from orchestrator.metrics import TELEGRAM_UPDATE_OFFSET

    try:
        state_store.set_app_state(_OFFSET_STATE_KEY, str(offset))
        TELEGRAM_UPDATE_OFFSET.set(offset)
    except Exception as e:  # noqa: BLE001
        logger.warning("[TELEGRAM] Could not persist offset %d: %s", offset, e)


async def _drain_pending(timeout: float = 8.0) -> None:
    """On shutdown, let in-flight update handlers finish (bounded) so a deploy
    doesn't DROP an update whose offset was already persisted — especially a
    fast Done/Snooze ack (a lost ack leaves a reminder nagging). Slow chat
    relays may still be cut at the timeout; that's the acceptable residual."""
    pending = [t for t in list(_bg_tasks) if not t.done()]
    if not pending:
        return
    logger.info("[TELEGRAM] Draining %d in-flight handler(s) before shutdown", len(pending))
    _, still = await asyncio.wait(pending, timeout=timeout)
    if still:
        logger.warning("[TELEGRAM] %d handler(s) unfinished after %.0fs drain", len(still), timeout)


async def _poll_once(client: httpx.AsyncClient, offset: int) -> tuple:
    """One getUpdates cycle: fetch, dispatch, advance the offset.

    Returns (new_offset, sleep_hint): 0 = healthy, a positive number = sleep
    exactly that long (409 conflict), -1 = error, caller applies exponential
    backoff. Split out of the loop for direct test coverage.
    """
    resp = await client.post(
        f"{_api_base()}/getUpdates",
        json={
            "offset": offset,
            "timeout": max(1, _settings.telegram_poll_timeout_seconds),
            "allowed_updates": ["message", "callback_query"],
        },
    )
    try:
        body = resp.json() if resp.content else {}
    except Exception:
        body = {}
    if resp.status_code == 409:
        # Another poller (stale container?) holds getUpdates.
        logger.error("[TELEGRAM] getUpdates conflict (409) — another poller is running; sleeping 60s")
        return offset, 60.0
    if resp.status_code != 200 or not body.get("ok", False):
        logger.warning(f"[TELEGRAM] getUpdates -> {resp.status_code}: {str(body.get('description', ''))[:200]}")
        return offset, -1.0

    for update in body.get("result", []):
        offset = max(offset, int(update.get("update_id", 0)) + 1)
        _dispatch(client, update)
    return offset, 0.0


async def telegram_poll_loop() -> None:
    """Forever long-poll getUpdates. Started as a dedicated asyncio task at
    startup (NOT a scheduler job — a 50s long-poll would pin a scheduler
    worker). Exponential backoff on transport errors, capped at 60s."""
    poll_timeout = max(1, _settings.telegram_poll_timeout_seconds)

    # The bot token rides in the URL path (Telegram's API design), and httpx
    # logs full request URLs at DEBUG. Pin these loggers to WARNING so a
    # global LOG_LEVEL=DEBUG never writes the live token into logs/Loki.
    for _name in ("httpx", "httpcore"):
        logging.getLogger(_name).setLevel(logging.WARNING)

    logger.info(f"[TELEGRAM] Long-poll loop starting (timeout={poll_timeout}s)")
    # Resume from the persisted offset so a restart (e.g. a deploy) doesn't
    # re-fetch and re-send the last ~24h of Telegram updates. Logged
    # unconditionally (incl. the fresh-0 case) so post-deploy you can tell
    # "resumed at N" from "code not deployed / offset reset".
    offset = _load_offset()
    logger.info(
        "[TELEGRAM] Resuming from persisted update offset %d%s",
        offset,
        "" if offset else " (fresh — no persisted offset)",
    )
    backoff = 1.0

    async with httpx.AsyncClient(timeout=httpx.Timeout(poll_timeout + 15.0, connect=10.0)) as client:
        while True:
            try:
                prev_offset = offset
                offset, sleep_hint = await _poll_once(client, offset)
                if offset != prev_offset:
                    _save_offset(offset)
                if sleep_hint > 0:
                    await asyncio.sleep(sleep_hint)
                elif sleep_hint < 0:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
                else:
                    backoff = 1.0
            except asyncio.CancelledError:
                logger.info("[TELEGRAM] Long-poll loop cancelled (shutdown)")
                # Let already-dispatched handlers finish so a deploy doesn't
                # drop an update whose offset we already persisted.
                await _drain_pending()
                raise
            except Exception as e:
                logger.warning(
                    f"[TELEGRAM] Poll error: {type(e).__name__}: {_redact(str(e))}; retrying in {backoff:.0f}s"
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
