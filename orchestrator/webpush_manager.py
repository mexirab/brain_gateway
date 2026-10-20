"""Web Push for the dashboard: lockscreen notifications without a third-party app.

The orchestrator is its own VAPID sender: each notification is encrypted
(aes128gcm, RFC 8291) for one browser subscription and POSTed straight to
that browser's push service. Apple/Google/Mozilla relay it but can't read it,
and no account or API key is involved. Devices subscribe from the dashboard
(/settings → Notifications) through ``routes_webpush``.

Fired by ``inbox.notify`` for every new inbox card (reminders + selfcare
nudges); tapping the notification opens /chat, where the card's Done /
Snooze buttons live (iOS web notifications can't carry action buttons).

Safety:
  * Subscription endpoints are client-supplied URLs the server will POST to,
    so they are allow-listed to the known push services (https, port 443,
    no userinfo) and redirects are refused — no SSRF.
  * The VAPID private key lives at ``WEBPUSH_KEY_PATH`` (0600, on the
    backed-up data volume). Losing it only means devices re-subscribe.
  * Never raises into delivery paths; logs carry hosts/counts, never text.
"""

import asyncio
import base64
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from orchestrator.config import settings
from orchestrator.metrics import WEBPUSH_SEND_TOTAL, WEBPUSH_SUBSCRIPTIONS
from orchestrator.state_store import get_db

logger = logging.getLogger(__name__)

# Push services browsers actually hand out. Suffix match on the hostname.
ALLOWED_PUSH_HOST_SUFFIXES = (
    ".push.apple.com",  # Safari / iOS home-screen apps (web.push.apple.com)
    "fcm.googleapis.com",  # Chrome, Edge (Android + desktop)
    "android.googleapis.com",
    ".push.services.mozilla.com",  # Firefox
    ".notify.windows.com",  # legacy Edge / WNS
)
MAX_SUBSCRIPTIONS = 20
_MAX_ENDPOINT_LEN = 1024
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
_UNSAFE_URL_CHARS = re.compile(r"[\\\s\x00-\x1f\x7f@]")
_SEND_TIMEOUT_SECONDS = 10
# Sends are blocking (requests) and run on the shared default executor; cap
# how many can hold a worker at once so a slow push service can't starve
# every other to_thread caller.
_SEND_CONCURRENCY = 4
# A device counts as reachable (reminder delivery may rely on it) if a push
# reached it recently, or it subscribed recently and hasn't been tried yet.
_HEALTHY_DAYS = 14

_vapid = None  # py_vapid.Vapid02, loaded by init()
_vapid_lock = threading.Lock()
_subscriber_count = 0
_healthy_count = 0
_bg_tasks: set[asyncio.Task] = set()
_send_sem: Optional[asyncio.Semaphore] = None


# ---------------------------------------------------------------------------
# Key + startup
# ---------------------------------------------------------------------------


def _load_or_create_key():
    from py_vapid import Vapid02

    path = settings.webpush_key_path
    if os.path.exists(path):
        return Vapid02.from_file(path)
    v = Vapid02()
    v.generate_keys()
    pem = v.private_pem()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    logger.info("[WEBPUSH] Generated a new VAPID key")
    return v


def _get_vapid():
    global _vapid
    with _vapid_lock:
        if _vapid is None:
            _vapid = _load_or_create_key()
        return _vapid


def init() -> None:
    """Startup: load (or create) the key and seed the subscriber count."""
    _refresh_count()
    if not settings.webpush_enabled:
        return
    try:
        _get_vapid()
        logger.info("[WEBPUSH] Enabled, %d device(s) subscribed", _subscriber_count)
    except Exception as e:
        logger.error("[WEBPUSH] VAPID key load failed: %s", type(e).__name__, exc_info=True)


def public_key() -> Optional[str]:
    """applicationServerKey for PushManager.subscribe (base64url, no padding)."""
    if not settings.webpush_enabled:
        return None
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    raw = _get_vapid().public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


def _refresh_count() -> None:
    global _subscriber_count, _healthy_count
    cutoff = (datetime.now() - timedelta(days=_HEALTHY_DAYS)).isoformat()
    try:
        with get_db() as conn:
            _subscriber_count = conn.execute("SELECT COUNT(*) FROM webpush_subscriptions").fetchone()[0]
            _healthy_count = conn.execute(
                "SELECT COUNT(*) FROM webpush_subscriptions WHERE COALESCE(last_ok_at, created_at) >= ?",
                (cutoff,),
            ).fetchone()[0]
    except Exception as e:
        logger.warning("[WEBPUSH] Subscriber count failed: %s", type(e).__name__)
    WEBPUSH_SUBSCRIPTIONS.set(_subscriber_count)


def subscriber_count() -> int:
    return _subscriber_count


def has_subscribers() -> bool:
    """True when a device has recently proven reachable — used to count Web
    Push as phone delivery, so a long-dead subscription can't mark a
    phone-only reminder delivered into the void."""
    return settings.webpush_enabled and _healthy_count > 0


def endpoint_error(endpoint: Any) -> Optional[str]:
    """None when the endpoint is an allow-listed push-service URL, else why not."""
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > _MAX_ENDPOINT_LEN:
        return "bad_endpoint"
    # urlsplit and urllib3 (what requests actually connects with) disagree on
    # some characters — e.g. urllib3 ends the host at "\", so
    # "https://10.0.0.1\.push.apple.com/" would pass a urlsplit-only check
    # and POST to 10.0.0.1. Refuse those characters outright, then require
    # both parsers to agree on the host.
    if _UNSAFE_URL_CHARS.search(endpoint):
        return "bad_endpoint"
    try:
        from urllib3.util import parse_url

        parts = urlsplit(endpoint)
        port = parts.port
        u3_host = (parse_url(endpoint).host or "").lower()
    except ValueError:
        return "bad_endpoint"
    except Exception:  # urllib3 LocationParseError
        return "bad_endpoint"
    host = (parts.hostname or "").lower()
    if not host or host != u3_host:
        return "bad_endpoint"
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        return "bad_endpoint"
    # ".x" entries match subdomains only; bare entries match the exact host.
    if not any(host.endswith(s) if s.startswith(".") else host == s for s in ALLOWED_PUSH_HOST_SUFFIXES):
        return "push_service_not_allowed"
    return None


def _key_ok(value: Any, min_len: int, max_len: int) -> bool:
    return isinstance(value, str) and min_len <= len(value) <= max_len and bool(_B64URL_RE.fullmatch(value))


def parse_subscription(body: Any) -> tuple[Optional[Dict[str, str]], Optional[str]]:
    """(subscription, error) from a PushSubscription.toJSON() body."""
    if not isinstance(body, dict):
        return None, "bad_body"
    endpoint = body.get("endpoint")
    err = endpoint_error(endpoint)
    if err:
        return None, err
    keys = body.get("keys")
    if not isinstance(keys, dict):
        return None, "bad_keys"
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    # p256dh = 65-byte P-256 point (87 chars b64url); auth = 16 bytes (22 chars).
    if not _key_ok(p256dh, 80, 100) or not _key_ok(auth, 16, 32):
        return None, "bad_keys"
    return {"endpoint": endpoint, "p256dh": p256dh, "auth": auth}, None


def save_subscription(sub: Dict[str, str]) -> Optional[str]:
    """Upsert one device. Returns an error code when the cap is reached."""
    with get_db() as conn:
        exists = conn.execute("SELECT 1 FROM webpush_subscriptions WHERE endpoint = ?", (sub["endpoint"],)).fetchone()
        if not exists:
            n = conn.execute("SELECT COUNT(*) FROM webpush_subscriptions").fetchone()[0]
            if n >= MAX_SUBSCRIPTIONS:
                return "too_many_devices"
        conn.execute(
            "INSERT INTO webpush_subscriptions (endpoint, p256dh, auth, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth",
            (sub["endpoint"], sub["p256dh"], sub["auth"], datetime.now().isoformat()),
        )
    _refresh_count()
    logger.info("[WEBPUSH] Device subscribed (%s); %d total", urlsplit(sub["endpoint"]).hostname, _subscriber_count)
    return None


def prune_stale_subscriptions(days: int = 30) -> int:
    """Drop devices no push has reached in ``days`` (dead 401/403/5xx rows
    would otherwise sit there and eventually hit MAX_SUBSCRIPTIONS)."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with get_db() as conn:
        n = conn.execute(
            "DELETE FROM webpush_subscriptions WHERE COALESCE(last_ok_at, created_at) < ?", (cutoff,)
        ).rowcount
    _refresh_count()
    if n:
        logger.info("[WEBPUSH] Pruned %d stale subscription(s)", n)
    return n


def delete_subscription(endpoint: str) -> bool:
    with get_db() as conn:
        n = conn.execute("DELETE FROM webpush_subscriptions WHERE endpoint = ?", (endpoint,)).rowcount
    _refresh_count()
    return n > 0


def _list_subscriptions() -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute("SELECT endpoint, p256dh, auth FROM webpush_subscriptions").fetchall()
    return [dict(r) for r in rows]


def _mark_ok(endpoint: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE webpush_subscriptions SET last_ok_at = ? WHERE endpoint = ?",
            (datetime.now().isoformat(), endpoint),
        )
    _refresh_count()


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def _send_one(sub: Dict[str, Any], data: str) -> str:
    """Blocking send to one device; returns the metric result label."""
    import requests
    from pywebpush import WebPushException, webpush

    # Re-check at send time: a row stored before a guard change can't slip by.
    if endpoint_error(sub["endpoint"]):
        return "error"
    session = requests.Session()
    session.max_redirects = 0  # a push service never redirects; refuse to follow one
    try:
        webpush(
            subscription_info={"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
            data=data,
            vapid_private_key=_get_vapid(),
            vapid_claims={"sub": settings.webpush_subject.strip()},
            ttl=settings.webpush_ttl_seconds,
            timeout=_SEND_TIMEOUT_SECONDS,
            headers={"Urgency": "high"},
            requests_session=session,
        )
        return "ok"
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        if status in (404, 410):
            return "gone"
        if status in (401, 403):
            # The push service no longer accepts our VAPID key for this
            # subscription (key regenerated/lost): the device must re-subscribe.
            return "auth"
        if status is not None and 400 <= status < 500:
            return "http_4xx"
        if status is not None:
            return "http_5xx"
        logger.warning("[WEBPUSH] Send error without response: %s", type(e).__name__)
        return "error"
    except requests.TooManyRedirects:
        return "redirect"
    except requests.Timeout:
        return "timeout"
    except Exception as e:
        logger.warning("[WEBPUSH] Send error: %s", type(e).__name__)
        return "error"
    finally:
        session.close()


async def send_to_all(title: str, body: str, url: str = "/chat", tag: Optional[str] = None) -> Dict[str, int]:
    """Send one notification to every subscribed device. Never raises.

    Returns ``{"sent": n, "failed": n}``. Subscriptions the push service
    reports gone (404/410 — app removed, permission revoked) are deleted.
    """
    counts = {"sent": 0, "failed": 0}
    if not settings.webpush_enabled:
        return counts
    try:
        subs = await asyncio.to_thread(_list_subscriptions)
    except Exception as e:
        logger.error("[WEBPUSH] Could not read subscriptions: %s", type(e).__name__)
        return counts
    if not subs:
        WEBPUSH_SEND_TOTAL.labels(result="no_subscribers").inc()
        return counts

    data = json.dumps({"title": title, "body": body, "url": url, "tag": tag or "jess"})
    global _send_sem
    if _send_sem is None:
        _send_sem = asyncio.Semaphore(_SEND_CONCURRENCY)

    async def _bounded(sub: Dict[str, Any]) -> str:
        async with _send_sem:
            return await asyncio.to_thread(_send_one, sub, data)

    results = await asyncio.gather(*(_bounded(s) for s in subs), return_exceptions=True)
    for sub, result in zip(subs, results, strict=True):
        label = result if isinstance(result, str) else "error"
        WEBPUSH_SEND_TOTAL.labels(result=label).inc()
        host = urlsplit(sub["endpoint"]).hostname
        try:
            if label == "ok":
                counts["sent"] += 1
                await asyncio.to_thread(_mark_ok, sub["endpoint"])
            else:
                counts["failed"] += 1
                if label == "gone":
                    await asyncio.to_thread(delete_subscription, sub["endpoint"])
                    logger.info("[WEBPUSH] Removed expired subscription (%s)", host)
                elif label == "auth":
                    logger.warning(
                        "[WEBPUSH] %s rejected our VAPID key — was %s replaced? Re-enable on that device.",
                        host,
                        settings.webpush_key_path,
                    )
                else:
                    logger.warning("[WEBPUSH] Send to %s failed: %s", host, label)
        except Exception as e:
            logger.warning("[WEBPUSH] Bookkeeping failed: %s", type(e).__name__)
    return counts


_GENERIC_BODY = {"reminder": "Reminder waiting", "selfcare": "Check-in waiting", "message": "Message from Jess"}
# kind="message" cards always use these (never the text, whatever
# WEBPUSH_SHOW_TEXT says): a briefing names meds and calendar events.
_MESSAGE_BODY = {
    "morning_briefing": "Morning briefing ready",
    "evening_briefing": "Evening wrap-up ready",
    "missed_recap": "Some reminders didn't reach you",
    "self_audit": "Jess system check ready",
}


def fire_inbox_push(item: Dict[str, Any]) -> None:
    """Fire-and-forget push for a new inbox card (called on the event loop)."""
    if not settings.webpush_enabled:
        return
    if _subscriber_count == 0:
        WEBPUSH_SEND_TOTAL.labels(result="no_subscribers").inc()
        return
    # Medication nudges name the drug — never put that on a lock screen.
    private = item.get("kind") == "selfcare" and item.get("ref") == "medication"
    if item.get("kind") == "message":
        body = _MESSAGE_BODY.get(item.get("ref"), _GENERIC_BODY["message"])
    elif settings.webpush_show_text and not private:
        body = item.get("text")
    else:
        body = _GENERIC_BODY.get(item.get("kind"), "Jess")
    task = asyncio.create_task(send_to_all("Jess", (body or "Jess")[:240], url="/chat", tag=item.get("id")))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
