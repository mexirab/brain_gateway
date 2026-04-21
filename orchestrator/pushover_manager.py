"""
Pushover push client (F-013).

Parallel to F-011's ntfy path. Pushover's native APNs integration is more
reliable than ntfy-upstream for iOS lockscreen banners on a self-hosted
stack, at the cost of an external paid (or free with device trial)
service holding reminder bodies briefly in transit.

Never raises: every entry returns a dict. Every exit path increments
`PUSHOVER_PUSH_TOTAL{result, kind}` exactly once so the dashboard can
honestly distinguish reminder vs confirm vs skipped vs fail.

Reuses F-011's HMAC-signed callback URLs — Pushover's `url` field carries
the Done callback as the primary tap action, and the Snooze callback is
embedded as a tappable HTML link in the message body (Pushover's html=1
mode makes `<a href>` tags tappable inside the expanded notification).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import httpx

from orchestrator.config import settings as _settings

logger = logging.getLogger(__name__)


def _sanitize_body(text: str, limit: int = 300) -> str:
    """Strip control chars (except tab/newline) and cap length.

    Pushover error responses occasionally include HTML or odd encoding
    when something upstream fails; cheap hygiene before surfacing into
    the caller's return dict (which the unified loop may echo back to
    the LLM).
    """
    if not text:
        return ""
    cleaned = "".join(ch for ch in text if ch in ("\t", "\n") or 0x20 <= ord(ch) < 0x7F)
    return cleaned[:limit]


# Defensive: if Pushover's error body ever echoed our submitted `token=` or
# `user=` query values back (it doesn't today, per security review), strip
# them before the body reaches logs or LLM context. Matches the form-encoded
# shape of Pushover's POST body with a word boundary so it doesn't eat real
# English words.
_CRED_RE = __import__("re").compile(r"\b(?:token|user)=[A-Za-z0-9]{8,}")


def _strip_credentials(text: str) -> str:
    """Remove any token=/user= patterns from a string (defensive)."""
    if not text:
        return text
    return _CRED_RE.sub(lambda m: m.group(0).split("=", 1)[0] + "=<redacted>", text)


async def deliver_via_pushover(reminder_id: str, text: str, priority: Optional[int] = None) -> Dict[str, Any]:
    """Push a reminder via Pushover with Done as primary action and Snooze
    as an inline HTML link.

    Returns one of:
      {"success": True, "request_id": "...", "latency_ms": ...}
      {"success": False, "skipped": True, "reason": "disabled|missing_*"}
      {"success": False, "status_code": int, "body": "..."}
      {"success": False, "error": "TypeName: msg"}
    """
    # Lazy import to avoid any chance of load-time circular refs with the
    # reminder_manager callback helpers.
    from orchestrator.metrics import PUSHOVER_PUSH_LATENCY, PUSHOVER_PUSH_TOTAL
    from orchestrator.reminder_manager import _build_callback_url

    if not _settings.pushover_enabled:
        PUSHOVER_PUSH_TOTAL.labels(result="skipped", kind="reminder", reason="disabled").inc()
        return {"success": False, "skipped": True, "reason": "disabled"}

    if not _settings.pushover_user_key:
        PUSHOVER_PUSH_TOTAL.labels(result="skipped", kind="reminder", reason="missing_user_key").inc()
        return {"success": False, "skipped": True, "reason": "missing_user_key"}
    if not _settings.pushover_app_token:
        PUSHOVER_PUSH_TOTAL.labels(result="skipped", kind="reminder", reason="missing_app_token").inc()
        return {"success": False, "skipped": True, "reason": "missing_app_token"}

    done_url = _build_callback_url(reminder_id, "ack")
    snooze_url = _build_callback_url(reminder_id, "snooze", {"minutes": "10"}, signed_extra="10")

    push_prio = priority if priority is not None else _settings.pushover_default_priority
    push_prio = max(-2, min(2, push_prio))

    # Pushover's html=1 mode supports <a>, <b>, <i>, <u>, <font color=...>.
    # We embed the Snooze URL in the body so the user can tap it from the
    # expanded Pushover notification. The Done URL is the primary `url`
    # field — tapping the banner itself triggers that.
    #
    # Escape the reminder text before embedding it in HTML: the text comes
    # from the `set_reminder` tool, whose args originate in an LLM response
    # to user input. A prompt injection that plants e.g. an `<a
    # href="attacker/...">` in the reminder body would otherwise be rendered
    # as a tappable link inside a "Jess reminder" notification — classic
    # phishing vector for a user who trusts the source. `html.escape` turns
    # any user/LLM-supplied angle-brackets into entities; we still emit the
    # literal snooze anchor we build ourselves.
    import html as _html

    escaped_text = _html.escape(text, quote=False)
    html_body = f'{escaped_text}\n\n<a href="{snooze_url}">\U0001f4a4 Snooze 10 min</a>'

    data = {
        "token": _settings.pushover_app_token,
        "user": _settings.pushover_user_key,
        "title": "Jess reminder",
        "message": html_body,
        "html": "1",
        "priority": str(push_prio),
        "url": done_url,
        "url_title": "\u2713 Done",
    }

    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=_settings.pushover_upload_timeout_seconds) as client:
            resp = await client.post(_settings.pushover_api_url, data=data)
        latency = time.time() - t0
        PUSHOVER_PUSH_LATENCY.labels(kind="reminder").observe(latency)

        if resp.status_code == 200:
            try:
                request_id = resp.json().get("request", "")
            except Exception:
                request_id = ""
            logger.info(
                f"[PUSHOVER] Pushed reminder {reminder_id} ({len(text)} chars, "
                f"prio={push_prio}) request={request_id[:40]}"
            )
            PUSHOVER_PUSH_TOTAL.labels(result="ok", kind="reminder", reason="ok").inc()
            return {
                "success": True,
                "request_id": request_id,
                "latency_ms": int(latency * 1000),
            }

        body_snippet = _strip_credentials(_sanitize_body(resp.text))
        reason = "http_4xx" if 400 <= resp.status_code < 500 else "http_5xx"
        logger.warning(f"[PUSHOVER] Push {reminder_id} returned {resp.status_code}: {body_snippet}")
        PUSHOVER_PUSH_TOTAL.labels(result="fail", kind="reminder", reason=reason).inc()
        return {
            "success": False,
            "status_code": resp.status_code,
            "body": body_snippet,
        }
    except httpx.TimeoutException as e:
        PUSHOVER_PUSH_TOTAL.labels(result="fail", kind="reminder", reason="timeout").inc()
        logger.error(f"[PUSHOVER] Push {reminder_id} timed out: {e}")
        return {"success": False, "error": f"TimeoutException: {e}"}
    except httpx.ConnectError as e:
        PUSHOVER_PUSH_TOTAL.labels(result="fail", kind="reminder", reason="connect_error").inc()
        logger.error(f"[PUSHOVER] Push {reminder_id} connect failed: {e}")
        return {"success": False, "error": f"ConnectError: {e}"}
    except Exception as e:
        PUSHOVER_PUSH_TOTAL.labels(result="fail", kind="reminder", reason="other").inc()
        logger.error(f"[PUSHOVER] Push {reminder_id} failed: {type(e).__name__}: {e}")
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


async def deliver_pushover_confirm(title: str, message: str, reminder_id: Optional[str] = None) -> Dict[str, Any]:
    """Low-priority confirmation push after a successful Done/Snooze tap.

    Priority=-1 (quiet; iOS delivers without sound or vibration). No
    tappable URL — this is purely a visible "it registered" receipt.

    Mirrors F-011's `deliver_ack_confirm` privacy rule: the title stays
    generic ("Logged" / "Snoozed until H:MM"). Action-specific detail
    (meal/meds/water/movement) lives in the body only.
    """
    from orchestrator.metrics import PUSHOVER_PUSH_LATENCY, PUSHOVER_PUSH_TOTAL

    if not _settings.pushover_enabled:
        PUSHOVER_PUSH_TOTAL.labels(result="skipped", kind="confirm", reason="disabled").inc()
        return {"success": False, "skipped": True, "reason": "disabled"}

    if not _settings.pushover_user_key or not _settings.pushover_app_token:
        PUSHOVER_PUSH_TOTAL.labels(result="skipped", kind="confirm", reason="missing_credentials").inc()
        return {"success": False, "skipped": True, "reason": "missing_credentials"}

    rid_hint = f" rid={reminder_id}" if reminder_id else ""

    data = {
        "token": _settings.pushover_app_token,
        "user": _settings.pushover_user_key,
        "title": title[:120],
        "message": message[:500],
        "priority": "-1",  # quiet — no sound/vibration
    }

    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=_settings.pushover_upload_timeout_seconds) as client:
            resp = await client.post(_settings.pushover_api_url, data=data)
        latency = time.time() - t0
        PUSHOVER_PUSH_LATENCY.labels(kind="confirm").observe(latency)

        if resp.status_code == 200:
            PUSHOVER_PUSH_TOTAL.labels(result="ok", kind="confirm", reason="ok").inc()
            return {"success": True}

        body_snippet = _strip_credentials(_sanitize_body(resp.text))
        reason = "http_4xx" if 400 <= resp.status_code < 500 else "http_5xx"
        logger.warning(f"[PUSHOVER-CONFIRM] Push returned {resp.status_code}{rid_hint}: {body_snippet}")
        PUSHOVER_PUSH_TOTAL.labels(result="fail", kind="confirm", reason=reason).inc()
        return {"success": False, "status_code": resp.status_code, "body": body_snippet}
    except httpx.TimeoutException as e:
        PUSHOVER_PUSH_TOTAL.labels(result="fail", kind="confirm", reason="timeout").inc()
        logger.error(f"[PUSHOVER-CONFIRM] Push timed out{rid_hint}: {e}")
        return {"success": False, "error": f"TimeoutException: {e}"}
    except httpx.ConnectError as e:
        PUSHOVER_PUSH_TOTAL.labels(result="fail", kind="confirm", reason="connect_error").inc()
        logger.error(f"[PUSHOVER-CONFIRM] Push connect failed{rid_hint}: {e}")
        return {"success": False, "error": f"ConnectError: {e}"}
    except Exception as e:
        PUSHOVER_PUSH_TOTAL.labels(result="fail", kind="confirm", reason="other").inc()
        logger.error(f"[PUSHOVER-CONFIRM] Push failed{rid_hint}: {type(e).__name__}: {e}")
        return {"success": False, "error": f"{type(e).__name__}: {e}"}
