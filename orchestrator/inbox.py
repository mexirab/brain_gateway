"""Dashboard inbox: reminders + selfcare nudges waiting for a tap in /chat.

The self-hosted replacement for the Telegram Done/Snooze buttons. Every
delivered reminder and every selfcare nudge becomes one row in
``inbox_items``; the dashboard chat tray lists the unresolved ones and
resolves them through ``routes_inbox``.

Resolution is shared across channels: an ack from ntfy/Pushover/Telegram
(``reminder_actions``) or a selfcare log from any path (voice, routine,
Telegram — ``selfcare_manager._resolve_inbox``) clears the card, so the tray
never shows something already dealt with elsewhere.

The add/resolve/list functions are sync SQLite (callers run add_* via
asyncio.to_thread and then call ``notify`` on the loop for Web Push).
Callers wrap them in try/except: a failed insert only costs the card, never
the reminder. Logs carry ids/kinds/lengths only.
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from orchestrator.metrics import INBOX_ITEMS_TOTAL, INBOX_RESOLVED_TOTAL
from orchestrator.state_store import get_db

logger = logging.getLogger(__name__)

KINDS = ("reminder", "selfcare")
SELFCARE_KINDS = ("medication", "meal", "water", "movement")
RESOLUTIONS = ("done", "snoozed", "dismissed", "superseded")

# Cards older than this drop out of the tray: a day-old "drink water" isn't
# actionable. They stay in the table (unresolved) until pruned.
PENDING_WINDOW_HOURS = 24
KEEP_DAYS = 30
_MAX_TEXT = 500


def _now() -> str:
    return datetime.now().isoformat()


def _add(kind: str, ref: str, text: str) -> Dict[str, Any]:
    item = {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "ref": ref,
        "text": (text or "")[:_MAX_TEXT],
        "created_at": _now(),
    }
    with get_db() as conn:
        conn.execute(
            "INSERT INTO inbox_items (id, kind, ref, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (item["id"], kind, ref, item["text"], item["created_at"]),
        )
    INBOX_ITEMS_TOTAL.labels(kind=kind).inc()
    logger.info("[INBOX] Added %s item %s (%d chars)", kind, item["id"], len(item["text"]))
    return item


def notify(item: Optional[Dict[str, Any]]) -> None:
    """Web Push a new card to subscribed devices. Fire-and-forget; call from
    the event loop (the add_* helpers run in a worker thread). Never raises."""
    if not item:
        return
    try:
        from orchestrator import webpush_manager

        webpush_manager.fire_inbox_push(item)
    except Exception as e:
        logger.warning("[INBOX] Web Push dispatch failed: %s", type(e).__name__)


def _resolve_where(where: str, params: tuple, kind: str, resolution: str, via: str) -> int:
    with get_db() as conn:
        cur = conn.execute(
            f"UPDATE inbox_items SET resolved_at = ?, resolution = ?, resolved_via = ? "
            f"WHERE resolved_at IS NULL AND {where}",
            (_now(), resolution, via, *params),
        )
        n = cur.rowcount
    if n:
        INBOX_RESOLVED_TOTAL.labels(kind=kind, resolution=resolution, via=via).inc(n)
    return n


def add_reminder_item(reminder_id: str, text: str) -> Dict[str, Any]:
    """A reminder was delivered. Replaces any older open card for the same
    reminder (a snoozed reminder coming back, or a retry) so it shows once."""
    _resolve_where("kind = 'reminder' AND ref = ?", (reminder_id,), "reminder", "superseded", "redelivered")
    return _add("reminder", reminder_id, text)


def add_selfcare_item(kind: str, text: str) -> Optional[Dict[str, Any]]:
    """A selfcare nudge fired. The newest nudge of a kind supersedes the old
    one — two "drink water" cards would be one too many."""
    if kind not in SELFCARE_KINDS:
        return None
    _resolve_where("kind = 'selfcare' AND ref = ?", (kind,), "selfcare", "superseded", "renudged")
    return _add("selfcare", kind, text)


def resolve_reminder(reminder_id: str, resolution: str, via: str) -> int:
    """Close the open card(s) for a reminder. Returns rows changed."""
    return _resolve_where("kind = 'reminder' AND ref = ?", (reminder_id,), "reminder", resolution, via)


def resolve_selfcare(kind: str, resolution: str, via: str) -> int:
    """Close the open card for a selfcare kind (logged some other way)."""
    return _resolve_where("kind = 'selfcare' AND ref = ?", (kind,), "selfcare", resolution, via)


def resolve_item(item_id: str, resolution: str, via: str) -> bool:
    item = get_item(item_id)
    if item is None:
        return False
    return _resolve_where("id = ?", (item_id,), item["kind"], resolution, via) > 0


def reopen_item(item_id: str) -> bool:
    """Undo a resolve (the action behind it failed), so the card can be retried."""
    with get_db() as conn:
        return (
            conn.execute(
                "UPDATE inbox_items SET resolved_at = NULL, resolution = NULL, resolved_via = NULL WHERE id = ?",
                (item_id,),
            ).rowcount
            > 0
        )


def get_item(item_id: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM inbox_items WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def list_pending(limit: int = 20) -> List[Dict[str, Any]]:
    """Unresolved cards from the last PENDING_WINDOW_HOURS, newest first."""
    since = (datetime.now() - timedelta(hours=PENDING_WINDOW_HOURS)).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, kind, ref, text, created_at FROM inbox_items "
            "WHERE resolved_at IS NULL AND created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (since, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def cleanup_old(keep_days: int = KEEP_DAYS) -> int:
    cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat()
    with get_db() as conn:
        return conn.execute("DELETE FROM inbox_items WHERE created_at < ?", (cutoff,)).rowcount
