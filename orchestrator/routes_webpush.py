"""Web Push subscription API for the dashboard (orchestrator/webpush_manager.py).

Bearer-gated; the dashboard reaches it through its cookie-checked,
CSRF-guarded /api/proxy. GET is side-effect-free (the VAPID key is created
at startup, not on read).
"""

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from orchestrator import webpush_manager
from orchestrator.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


def _disabled() -> JSONResponse:
    return JSONResponse({"ok": False, "error": "disabled"}, status_code=409)


async def _json_body(req: Request):
    try:
        return await req.json()
    except ValueError:
        return None


@router.get("/api/webpush/public-key")
async def webpush_public_key():
    """{enabled, public_key, devices} — what the dashboard needs to subscribe."""
    if not settings.webpush_enabled:
        return JSONResponse({"ok": True, "enabled": False, "public_key": None, "devices": 0})
    try:
        key = await asyncio.to_thread(webpush_manager.public_key)
    except Exception as e:
        logger.error("[WEBPUSH] Public key unavailable: %s", type(e).__name__)
        return JSONResponse(
            {"ok": False, "enabled": False, "public_key": None, "devices": 0, "error": "key_unavailable"}
        )
    return JSONResponse({"ok": True, "enabled": True, "public_key": key, "devices": webpush_manager.subscriber_count()})


@router.post("/api/webpush/subscribe")
async def webpush_subscribe(req: Request):
    if not settings.webpush_enabled:
        return _disabled()
    sub, err = webpush_manager.parse_subscription(await _json_body(req))
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    err = await asyncio.to_thread(webpush_manager.save_subscription, sub)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=409)
    return JSONResponse({"ok": True, "devices": webpush_manager.subscriber_count()})


@router.post("/api/webpush/unsubscribe")
async def webpush_unsubscribe(req: Request):
    body = await _json_body(req)
    endpoint = body.get("endpoint") if isinstance(body, dict) else None
    if webpush_manager.endpoint_error(endpoint):
        return JSONResponse({"ok": False, "error": "bad_endpoint"}, status_code=400)
    removed = await asyncio.to_thread(webpush_manager.delete_subscription, endpoint)
    return JSONResponse({"ok": True, "removed": removed, "devices": webpush_manager.subscriber_count()})


@router.post("/api/webpush/test")
async def webpush_test():
    """Send a test notification to every subscribed device and report counts."""
    if not settings.webpush_enabled:
        return _disabled()
    counts = await webpush_manager.send_to_all(
        "Jess", "Test notification — reminders will show up like this.", url="/chat", tag="test"
    )
    return JSONResponse({"ok": counts["sent"] > 0, **counts})
