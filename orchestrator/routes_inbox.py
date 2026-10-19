"""Dashboard inbox API: the Done / Snooze cards in /chat (orchestrator/inbox.py).

Bearer-gated like every /api route; the dashboard reaches it through its
cookie-checked, CSRF-guarded /api/proxy. GET is side-effect-free.
"""

import asyncio
import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from orchestrator import inbox, reminder_actions

logger = logging.getLogger(__name__)

router = APIRouter()

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_VIA = "dashboard"


def _bad_id() -> JSONResponse:
    return JSONResponse({"ok": False, "error": "bad_id"}, status_code=400)


async def _load(item_id: str):
    """(item, error_response). Only open cards can be acted on."""
    if not _ID_RE.match(item_id):
        return None, _bad_id()
    item = await asyncio.to_thread(inbox.get_item, item_id)
    if item is None:
        return None, JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    if item["resolved_at"]:
        return None, JSONResponse(
            {"ok": True, "already_resolved": True, "resolution": item["resolution"]},
        )
    return item, None


@router.get("/api/inbox")
async def list_inbox():
    """Open cards from the last 24 h, newest first."""
    items = await asyncio.to_thread(inbox.list_pending)
    return JSONResponse({"ok": True, "items": items})


@router.post("/api/inbox/{item_id}/done")
async def inbox_done(item_id: str):
    item, err = await _load(item_id)
    if err:
        return err

    if item["kind"] == "message":
        # Informational card: nothing to log — the tray only offers Dismiss.
        return JSONResponse({"ok": False, "error": "not_actionable"}, status_code=400)

    if item["kind"] == "reminder":
        outcome = await reminder_actions.ack_reminder(item["ref"], via=_VIA)
        if outcome["status"] == "not_found":
            # The reminder row is gone (deleted); the card has nothing behind it.
            await asyncio.to_thread(inbox.resolve_item, item_id, "dismissed", _VIA)
            return JSONResponse({"ok": False, "error": "reminder_gone"}, status_code=404)
        # ack_reminder resolved the card (also on an idempotent replay).
        return JSONResponse({"ok": True, "logged": outcome.get("action")})

    # selfcare: close the card first (so the resolution is attributed to the
    # dashboard, and record_*'s own inbox resolve is a no-op), and re-open it
    # if the log fails so the user can retry.
    kind = item["ref"]
    # The UPDATE only matches an open card, so of two quick taps exactly one
    # gets True — the other must not log the meal/med a second time.
    if not await asyncio.to_thread(inbox.resolve_item, item_id, "done", _VIA):
        return JSONResponse({"ok": True, "already_resolved": True})
    try:
        from orchestrator import selfcare_manager

        # On the loop like every other caller: it mutates in-memory nudge gates.
        selfcare_manager.record_selfcare(kind, f"dashboard:{kind} nudge")
    except Exception as e:
        logger.error("[INBOX] Selfcare log failed for %s: %s", kind, type(e).__name__, exc_info=True)
        await asyncio.to_thread(inbox.reopen_item, item_id)
        return JSONResponse({"ok": False, "error": "log_failed"}, status_code=500)
    logger.info("[INBOX] %s logged via dashboard", kind)
    return JSONResponse({"ok": True, "logged": kind})


@router.post("/api/inbox/{item_id}/snooze")
async def inbox_snooze(item_id: str, req: Request):
    try:
        body = await req.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "bad_body"}, status_code=400)
    try:
        minutes = int(body.get("minutes", 15))
    except (TypeError, ValueError, OverflowError):  # OverflowError: {"minutes": Infinity}
        return JSONResponse({"ok": False, "error": "bad_minutes"}, status_code=400)

    item, err = await _load(item_id)
    if err:
        return err
    if item["kind"] != "reminder":
        return JSONResponse({"ok": False, "error": "not_snoozable"}, status_code=400)

    outcome = await reminder_actions.snooze_reminder(item["ref"], minutes, via=_VIA)
    status = outcome["status"]
    if status == "not_found":
        await asyncio.to_thread(inbox.resolve_item, item_id, "dismissed", _VIA)
        return JSONResponse({"ok": False, "error": "reminder_gone"}, status_code=404)
    if status == "limit":
        return JSONResponse(
            {"ok": False, "error": "max_snoozes_reached", "snooze_count": outcome["snooze_count"]},
            status_code=409,
        )
    if status != "ok":
        return JSONResponse({"ok": False, "error": "reschedule_failed"}, status_code=500)
    return JSONResponse(
        {
            "ok": True,
            "rescheduled_for": outcome["run_at"].isoformat(),
            "minutes": outcome["minutes"],
            "snooze_count": outcome["snooze_count"],
        }
    )


@router.post("/api/inbox/{item_id}/dismiss")
async def inbox_dismiss(item_id: str):
    """Hide a card without logging anything (the reminder stays delivered)."""
    item, err = await _load(item_id)
    if err:
        return err
    await asyncio.to_thread(inbox.resolve_item, item_id, "dismissed", _VIA)
    return JSONResponse({"ok": True})
