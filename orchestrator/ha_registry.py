"""Home Assistant registry metadata for the dashboard /home page.

HA's REST ``/api/states`` has no room or entity-category information; both
live in the registries, which HA only exposes over its WebSocket API. One
short-lived connection reads three registries and the result is cached:

- ``config/entity_registry/list_for_display`` — per entity: its own area
  override, device id, entity category (config / diagnostic) and hidden flag;
- ``config/device_registry/list`` — the device's area (entities inherit it);
- ``config/area_registry/list`` — area id → display name.

``entity_view()`` shapes one entity for ``GET /api/ha/entities`` (identity,
state, room, category and the few attributes the page's controls use).

``get_registry_meta()`` never raises: on any failure it returns the last good
result (or ``{}``), and the page falls back to grouping by domain. The
"hidden" flag only trims the listing — it is not an access control (chat and
``POST /api/ha/command`` can still target a hidden entity). Read-only —
nothing here writes to Home Assistant.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Dict, Optional

from orchestrator.metrics import HA_REGISTRY_FETCH

if TYPE_CHECKING:
    from orchestrator.ha_integration import Entity

logger = logging.getLogger(__name__)

REGISTRY_TTL_SECONDS = 600
FAILURE_RETRY_SECONDS = 60
FETCH_TIMEOUT_SECONDS = 8
_MAX_MESSAGE_BYTES = 16 * 1024 * 1024

_cache: Dict[str, Dict[str, Any]] = {}
_fetched_at: float = 0.0
_failed_at: float = 0.0
_lock = asyncio.Lock()


def _ws_url(http_url: str) -> Optional[str]:
    if http_url.startswith("https://"):
        return "wss://" + http_url[len("https://") :].rstrip("/") + "/api/websocket"
    if http_url.startswith("http://"):
        return "ws://" + http_url[len("http://") :].rstrip("/") + "/api/websocket"
    return None


async def _fetch(ws_url: str, token: str) -> Dict[str, Dict[str, Any]]:
    import websockets

    async with websockets.connect(ws_url, max_size=_MAX_MESSAGE_BYTES, open_timeout=5, close_timeout=1) as ws:
        hello = json.loads(await ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError("unexpected_handshake")
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            raise RuntimeError("auth_failed")

        commands = {
            1: "config/entity_registry/list_for_display",
            2: "config/device_registry/list",
            3: "config/area_registry/list",
        }
        for msg_id, msg_type in commands.items():
            await ws.send(json.dumps({"id": msg_id, "type": msg_type}))
        results: Dict[int, Any] = {}
        while len(results) < len(commands):
            msg = json.loads(await ws.recv())
            if msg.get("type") != "result" or msg.get("id") not in commands:
                continue
            if not msg.get("success"):
                raise RuntimeError("command_failed")
            results[msg["id"]] = msg.get("result")

    entity_reg = results[1] or {}
    categories = entity_reg.get("entity_categories") or {}
    device_area = {d.get("id"): d.get("area_id") for d in (results[2] or []) if isinstance(d, dict)}
    area_name = {a.get("area_id"): a.get("name") for a in (results[3] or []) if isinstance(a, dict)}

    meta: Dict[str, Dict[str, Any]] = {}
    for e in entity_reg.get("entities") or []:
        if not isinstance(e, dict) or not isinstance(e.get("ei"), str):
            continue
        area_id = e.get("ai") or device_area.get(e.get("di"))
        ec = e.get("ec")
        meta[e["ei"]] = {
            "area": area_name.get(area_id) if area_id else None,
            "category": categories.get(str(ec)) if ec is not None else None,
            "hidden": bool(e.get("hb")),
        }
    return meta


async def get_registry_meta(http_url: str, token: str) -> Dict[str, Dict[str, Any]]:
    """``{entity_id: {"area", "category", "hidden"}}`` — cached, never raises."""
    global _cache, _fetched_at, _failed_at

    now = time.monotonic()
    if _fetched_at and now - _fetched_at < REGISTRY_TTL_SECONDS:
        return _cache
    if _failed_at and now - _failed_at < FAILURE_RETRY_SECONDS:
        return _cache
    ws_url = _ws_url(http_url or "")
    if not ws_url or not token:
        return _cache

    async with _lock:
        now = time.monotonic()
        if _fetched_at and now - _fetched_at < REGISTRY_TTL_SECONDS:
            return _cache
        if _failed_at and now - _failed_at < FAILURE_RETRY_SECONDS:
            return _cache
        try:
            meta = await asyncio.wait_for(_fetch(ws_url, token), timeout=FETCH_TIMEOUT_SECONDS)
        except Exception as e:
            _failed_at = time.monotonic()
            HA_REGISTRY_FETCH.labels(result="error").inc()
            logger.warning(f"[HA] Registry fetch failed ({type(e).__name__}); rooms unavailable, using last cache")
            return _cache
        _cache, _fetched_at, _failed_at = meta, time.monotonic(), 0.0
        HA_REGISTRY_FETCH.labels(result="ok").inc()
        return _cache


def entity_view(e: Entity, meta: Dict[str, Any]) -> Dict[str, Any]:
    """One entity as the dashboard /home page sees it: identity + state plus
    the few attributes its controls need (room, category, brightness, volume)."""
    attrs = e.attributes or {}
    out: Dict[str, Any] = {
        "entity_id": e.entity_id,
        "friendly_name": e.friendly_name,
        "state": e.state,
        "area": meta.get("area"),
        "category": meta.get("category"),
    }
    if e.domain == "light":
        modes = attrs.get("supported_color_modes")
        modes = modes if isinstance(modes, list) else []
        out["supports_brightness"] = any(m != "onoff" for m in modes) if modes else "brightness" in attrs
        out["brightness"] = attrs.get("brightness") if isinstance(attrs.get("brightness"), int) else None
        out["is_group"] = isinstance(attrs.get("entity_id"), list)
    elif e.domain == "media_player":
        vol = attrs.get("volume_level")
        out["volume"] = vol if isinstance(vol, (int, float)) else None
        title = attrs.get("media_title")
        out["media_title"] = title[:120] if isinstance(title, str) else None
    elif e.domain == "climate":
        for key in ("current_temperature", "temperature"):
            val = attrs.get(key)
            out[key] = val if isinstance(val, (int, float)) else None
    return out


def _reset_cache_for_tests() -> None:
    global _cache, _fetched_at, _failed_at, _lock
    _cache, _fetched_at, _failed_at = {}, 0.0, 0.0
    _lock = asyncio.Lock()
