"""Workout API routes — dashboard + voice flow share the same endpoints."""

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from orchestrator import state_store
from orchestrator.workout_manager import (
    generate_workout,
    get_history,
    get_status,
    log_set,
    modify_workout,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Hard limits — anything past these is data corruption, not a real workout.
_MAX_WEIGHT_LBS = 2000.0
_MAX_REPS = 1000
_MAX_NAME_LEN = 200


async def _safe_json(req: Request) -> dict:
    try:
        body = await req.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def _str_list(value) -> list[str]:
    """Coerce a JSON value into a list[str], dropping anything else."""
    if not isinstance(value, list):
        return []
    return [str(v)[:_MAX_NAME_LEN] for v in value if isinstance(v, (str, int, float))]


@router.get("/api/workouts/today")
async def workouts_today() -> JSONResponse:
    """Return today's workout (plan + progress) or an empty-state payload."""
    status = await asyncio.to_thread(get_status)
    return JSONResponse(status)


@router.post("/api/workouts/generate")
async def workouts_generate() -> JSONResponse:
    """Generate today's workout via the adaptive planner.

    Idempotent for the day: if a workout already exists for today it is
    returned instead of creating a duplicate row. Concurrent calls cannot
    spam the DB.
    """
    existing = await asyncio.to_thread(state_store.get_todays_workout)
    if existing:
        return JSONResponse(
            {
                "ok": True,
                "workout_id": existing["id"],
                "workout_type": existing["workout_type"],
                "reasoning": existing.get("reasoning"),
                "exercises": [],
                "already_exists": True,
            }
        )
    result = await asyncio.to_thread(generate_workout)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@router.get("/api/workouts/history")
async def workouts_history(days: int = Query(14, ge=1, le=365)) -> JSONResponse:
    history = await asyncio.to_thread(get_history, days)
    return JSONResponse({"days": days, "sessions": history})


@router.get("/api/workouts/exercises")
async def workouts_exercises(
    movement_pattern: Optional[str] = None,
    equipment: Optional[str] = None,
) -> JSONResponse:
    rows = await asyncio.to_thread(state_store.get_exercises, movement_pattern, equipment)
    return JSONResponse(rows)


@router.post("/api/workouts/sets")
async def workouts_log_set(req: Request) -> JSONResponse:
    """Record a completed set. Body: {exercise, weight_lbs, reps, rpe?, set_id?, workout_id?}."""
    body = await _safe_json(req)
    exercise = str(body.get("exercise") or "").strip()[:_MAX_NAME_LEN]
    if not exercise:
        return JSONResponse({"error": "exercise is required"}, status_code=400)
    try:
        weight = float(body.get("weight_lbs"))
        reps = int(body.get("reps"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "weight_lbs and reps must be numeric"}, status_code=400)
    if not (0 < weight <= _MAX_WEIGHT_LBS):
        return JSONResponse(
            {"error": f"weight_lbs must be between 0 and {_MAX_WEIGHT_LBS}"},
            status_code=400,
        )
    if not (0 < reps <= _MAX_REPS):
        return JSONResponse({"error": f"reps must be between 1 and {_MAX_REPS}"}, status_code=400)

    rpe_raw = body.get("rpe")
    rpe: Optional[float] = None
    if rpe_raw not in (None, ""):
        try:
            rpe = float(rpe_raw)
        except (TypeError, ValueError):
            return JSONResponse({"error": "rpe must be numeric"}, status_code=400)
        if not (0 < rpe <= 10):
            return JSONResponse({"error": "rpe must be between 1 and 10"}, status_code=400)

    workout_id = body.get("workout_id")
    set_id = body.get("set_id")
    try:
        wid = int(workout_id) if workout_id not in (None, "") else None
        sid = int(set_id) if set_id not in (None, "") else None
    except (TypeError, ValueError):
        return JSONResponse({"error": "workout_id/set_id must be integers"}, status_code=400)

    result = await asyncio.to_thread(log_set, exercise, weight, reps, rpe, wid, sid)
    return JSONResponse(result)


@router.patch("/api/workouts/{workout_id}")
async def workouts_modify(workout_id: int, req: Request) -> JSONResponse:
    body = await _safe_json(req)
    remove = _str_list(body.get("remove_exercises"))
    add = _str_list(body.get("add_exercises"))
    result = await asyncio.to_thread(modify_workout, workout_id, remove, add)
    return JSONResponse(result)


@router.post("/api/workouts/{workout_id}/end")
async def workouts_end(workout_id: int, req: Request) -> JSONResponse:
    body = await _safe_json(req)
    notes_raw = body.get("notes")
    notes = str(notes_raw)[:1000] if notes_raw else None
    ok = await asyncio.to_thread(state_store.end_workout, workout_id, notes)
    return JSONResponse({"ok": ok})


@router.delete("/api/workouts/sets/{set_id}")
async def workouts_delete_set(set_id: int) -> JSONResponse:
    ok = await asyncio.to_thread(state_store.delete_workout_set, set_id)
    return JSONResponse({"ok": ok})


@router.delete("/api/workouts/{workout_id}")
async def workouts_delete(workout_id: int) -> JSONResponse:
    ok = await asyncio.to_thread(state_store.delete_workout, workout_id)
    return JSONResponse({"ok": ok})
