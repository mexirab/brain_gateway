"""MemPalace API routes."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()


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
    body = await req.json()

    text = str(body.get("text", "")).strip()
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)

    doc_id = await palace.store(
        text=text,
        wing=body.get("wing", ""),
        room=body.get("room", ""),
        source=body.get("source", "api"),
        category=body.get("category", "general"),
        confidence=body.get("confidence", "high"),
        project=body.get("project", ""),
        session_id=body.get("session_id", ""),
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


@router.get("/api/palace/wakeup")
async def palace_wakeup():
    """Get compressed identity context for system prompts."""
    from orchestrator.shared import get_palace

    palace = get_palace()
    context = palace.generate_wakeup_context()
    return JSONResponse({"context": context, "token_estimate": len(context) // 4})


@router.post("/api/palace/migrate")
async def palace_migrate():
    """Backfill auto_learn facts into the palace."""
    from orchestrator.shared import get_palace

    palace = get_palace()
    stats = await palace.migrate_auto_learn()
    return JSONResponse({"ok": True, **stats})


@router.post("/api/palace/mine")
async def palace_mine(req: Request):
    """Trigger session mining for Claude Code sessions."""
    from orchestrator.shared import get_palace

    body = await req.json() if req.headers.get("content-type", "").startswith("application/json") else {}
    session_path = body.get("session_path", "")

    try:
        from orchestrator.session_miner import mine_sessions

        stats = await mine_sessions(session_path=session_path)
        return JSONResponse({"ok": True, **stats})
    except ImportError:
        return JSONResponse({"ok": False, "error": "session_miner not available"}, status_code=501)
    except Exception as e:
        logger.error("[PALACE] Session mining failed: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
