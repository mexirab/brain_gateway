"""Task backlog API routes (dashboard). Business logic lives in backlog_manager
so the REST surface and the voice tools share one store + metrics path."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()

_VALID_STATUS = {"open", "done", "dropped"}
_VALID_PRIORITY = {"low", "normal", "high"}


@router.get("/api/tasks")
async def get_tasks(status: str = "open"):
    """List backlog tasks (default open), in surfacing order (high/oldest first)."""
    from orchestrator.state_store import list_tasks

    st = status if status in _VALID_STATUS else "open"
    return JSONResponse(list_tasks(st))


@router.post("/api/tasks")
async def add_task_route(req: Request):
    """Add a task to the backlog."""
    from orchestrator import backlog_manager

    body = await req.json()
    text = str(body.get("text", "")).strip()
    if not text:
        return JSONResponse({"error": "Task text is required"}, status_code=400)
    priority = str(body.get("priority", "normal")).strip().lower()
    if priority not in _VALID_PRIORITY:
        priority = "normal"
    notes = (str(body.get("notes")).strip() or None) if body.get("notes") else None
    due_date = (str(body.get("due_date")).strip() or None) if body.get("due_date") else None

    task = backlog_manager.create(text, priority=priority, source="dashboard", notes=notes, due_date=due_date)
    if task is None:
        return JSONResponse({"error": "Task text is required"}, status_code=400)
    return JSONResponse(task, status_code=201)


@router.post("/api/tasks/{task_id}/complete")
async def complete_task_route(task_id: str):
    """Mark a task done."""
    from orchestrator.backlog_manager import complete_by_id

    return JSONResponse({"ok": complete_by_id(task_id)})


@router.post("/api/tasks/{task_id}/drop")
async def drop_task_route(task_id: str):
    """Drop a task (no-guilt removal, kept for history)."""
    from orchestrator.backlog_manager import drop_by_id

    return JSONResponse({"ok": drop_by_id(task_id)})


@router.patch("/api/tasks/{task_id}")
async def update_task_route(task_id: str, req: Request):
    """Update an open task's priority (and/or notes/due_date)."""
    from orchestrator.state_store import update_task

    body = await req.json()
    priority = body.get("priority")
    if priority is not None and str(priority).lower() not in _VALID_PRIORITY:
        return JSONResponse({"error": "invalid priority"}, status_code=400)
    ok = update_task(
        task_id,
        priority=str(priority).lower() if priority is not None else None,
        notes=body.get("notes"),
        due_date=body.get("due_date"),
    )
    return JSONResponse({"ok": ok})
