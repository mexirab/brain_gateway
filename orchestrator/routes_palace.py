"""MemPalace API routes."""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from orchestrator import shared

logger = logging.getLogger(__name__)

# Wings only in-process writers may use (see palace_store).
_RESERVED_WINGS = frozenset({"system"})


def _require_palace_enabled():
    """FastAPI dependency: reject requests when PALACE_ENABLED is false.

    Applied to every handler in this router so the env var is an actual
    kill switch, not just a hint.
    """
    if not shared.PALACE_ENABLED:
        raise HTTPException(status_code=503, detail="MemPalace is disabled (PALACE_ENABLED=false)")


router = APIRouter(dependencies=[Depends(_require_palace_enabled)])


@router.get("/api/palace/search")
async def palace_search(query: str, wing: str = "", room: str = "", n: int = 5):
    """Semantic search across the memory palace."""
    from orchestrator.shared import get_palace

    palace = get_palace()
    n = max(1, min(n, 20))
    results = await palace.search(query=query, wing=wing, room=room, n_results=n)
    return JSONResponse({"query": query, "count": len(results), "results": results})


@router.post("/api/palace/store")
async def palace_store(req: Request):
    """Store a memory in the palace."""
    from orchestrator.shared import get_palace

    palace = get_palace()
    try:
        body = await req.json()
    except ValueError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "JSON object body required"}, status_code=400)
    # Non-string values used to raise inside store() (500) and would skip the
    # reserved-wing check below; reject them cleanly.
    for field in ("wing", "room", "source", "category", "confidence", "project", "session_id"):
        if field in body and body[field] is not None and not isinstance(body[field], str):
            return JSONResponse({"error": f"{field} must be a string"}, status_code=400)

    text = str(body.get("text", "")).strip()
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)
    # `system` holds Jess's own operational facts (self-audit), written only
    # in-process. API/MCP callers can't file text there, so nothing outside
    # the orchestrator can plant fake "audit facts".
    if str(body.get("wing", "")).strip().lower() in _RESERVED_WINGS:
        return JSONResponse({"error": "wing is reserved for internal use"}, status_code=403)

    doc_id = await palace.store(
        text=text,
        # `or <default>`: an explicit null must not reach ChromaDB metadata.
        wing=body.get("wing") or "",
        room=body.get("room") or "",
        source=body.get("source") or "api",
        category=body.get("category") or "general",
        confidence=body.get("confidence") or "high",
        project=body.get("project") or "",
        session_id=body.get("session_id") or "",
    )

    if doc_id:
        return JSONResponse({"ok": True, "id": doc_id})
    return JSONResponse({"ok": False, "reason": "duplicate or error"}, status_code=409)


@router.get("/api/palace/memory/{doc_id}")
async def palace_get(doc_id: str):
    """Get a single memory by ID."""
    from orchestrator.shared import get_palace

    palace = get_palace()
    memory = palace.get_by_id(doc_id)
    if memory:
        return JSONResponse(memory)
    return JSONResponse({"error": "Memory not found"}, status_code=404)


@router.delete("/api/palace/memory/{doc_id}")
async def palace_delete(doc_id: str):
    """Delete a memory by ID."""
    from orchestrator.shared import get_palace

    palace = get_palace()
    ok = palace.delete(doc_id)
    if ok:
        return JSONResponse({"ok": True, "deleted": doc_id})
    return JSONResponse({"ok": False, "error": "Not found or delete failed"}, status_code=404)


@router.get("/api/palace/wings")
async def palace_wings():
    """List palace wing structure."""
    from orchestrator.shared import get_palace

    palace = get_palace()
    return JSONResponse(palace.list_wings())


@router.get("/api/palace/wings/{wing}/rooms")
async def palace_rooms(wing: str):
    """List rooms in a wing with memory counts."""
    from orchestrator.shared import get_palace

    palace = get_palace()
    rooms = palace.list_rooms(wing)
    if not rooms:
        return JSONResponse({"error": f"Wing '{wing}' not found"}, status_code=404)
    return JSONResponse(rooms)


@router.get("/api/palace/stats")
async def palace_stats():
    """Memory counts by wing and room."""
    from orchestrator.shared import get_palace

    palace = get_palace()
    return JSONResponse(palace.room_stats())


# One mining run at a time: it makes one model call per session file on the
# single llama slot (up to 120 s each), so stacked requests would starve chat.
_MINE_LOCK = asyncio.Lock()


@router.post("/api/palace/mine")
async def palace_mine(req: Request):
    """Trigger session mining for Claude Code sessions."""

    body = await req.json() if req.headers.get("content-type", "").startswith("application/json") else {}
    session_path = body.get("session_path", "")

    if _MINE_LOCK.locked():
        return JSONResponse({"ok": False, "error": "a mining run is already in progress"}, status_code=409)
    async with _MINE_LOCK:
        try:
            from orchestrator.session_miner import mine_sessions

            stats = await mine_sessions(session_path=session_path)
            return JSONResponse({"ok": True, **stats})
        except ImportError:
            return JSONResponse({"ok": False, "error": "session_miner not available"}, status_code=501)
        except Exception as e:
            logger.error("[PALACE] Session mining failed: %r", e)
            return JSONResponse(
                {"ok": False, "error": "session mining failed (see orchestrator logs)"}, status_code=500
            )
