"""Workout API routes — dashboard + voice flow share the same endpoints."""

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from orchestrator import exercise_library, state_store, workout_setup
from orchestrator.exercises_seed import EQUIPMENT_TOKENS, LOCATION_TEMPLATES
from orchestrator.workout_manager import (
    generate_workout,
    get_history,
    get_program_overview,
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


def _default_location_id() -> Optional[int]:
    """Last-used location, or the only one; None when there's nothing to go on."""
    locations = state_store.list_workout_locations()
    if len(locations) == 1:
        return locations[0]["id"]
    if locations and locations[0].get("last_used_at"):
        return locations[0]["id"]
    return None


@router.post("/api/workouts/generate")
async def workouts_generate(req: Request) -> JSONResponse:
    """Generate today's workout via the planner. Body (optional): {location_id}.

    Without location_id, uses the last-used (or only) location; an explicit
    null means no location (whole catalog). Idempotent for
    the day: if a workout already exists for today it is returned instead of
    creating a duplicate row. Concurrent calls cannot spam the DB.
    """
    body = await _safe_json(req)
    raw_loc = body.get("location_id")
    try:
        location_id = int(raw_loc) if raw_loc not in (None, "") else None
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "location_id must be an integer"}, status_code=400)
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
                # The dashboard picked a different place than today's plan uses.
                "location_mismatch": location_id is not None and location_id != existing.get("location_id"),
            }
        )
    # Omitted → only/last-used location. An explicit null ("Not sure (full
    # gym)" on the dashboard) means no location: the whole catalog.
    if "location_id" not in body:
        location_id = await asyncio.to_thread(_default_location_id)
    result = await asyncio.to_thread(generate_workout, location_id)
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
    location_id: Optional[int] = None,
) -> JSONResponse:
    available = None
    if location_id is not None:
        loc = await asyncio.to_thread(state_store.get_workout_location, location_id)
        if not loc:
            return JSONResponse({"ok": False, "error": "location not found"}, status_code=404)
        available = set(loc["equipment"])
    rows = await asyncio.to_thread(state_store.get_exercises, movement_pattern, equipment, available)
    return JSONResponse(rows)


@router.get("/api/workouts/exercise-info")
async def workouts_exercise_info(name: str = Query(..., min_length=1, max_length=_MAX_NAME_LEN)) -> JSONResponse:
    """How-to for one exercise: steps + photo frame URLs, or a video-search link."""
    if not name.strip():
        return JSONResponse({"ok": False, "error": "name is required"}, status_code=400)
    info = await asyncio.to_thread(exercise_library.get_info, name)
    return JSONResponse({"ok": True, **info})


@router.get("/api/workouts/exercise-library/{lib_id}/{frame}.jpg", response_model=None)
async def workouts_exercise_photo(lib_id: str, frame: int) -> FileResponse | JSONResponse:
    path = await asyncio.to_thread(exercise_library.image_file, lib_id, frame)
    if path is None:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    # Pinned upstream commit → frames never change under a given URL.
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})


# --- Program builder: weekly program + workout locations -------------------
# Registered BEFORE the /api/workouts/{workout_id} routes — those would
# otherwise capture "program"/"locations" and 422 on the int conversion.


def _setup_response(result: dict, ok_status: int = 200) -> JSONResponse:
    if result.get("ok"):
        return JSONResponse(result, status_code=ok_status)
    status = 404 if result.get("error") in ("location not found", "no active program") else 400
    return JSONResponse(result, status_code=status)


@router.get("/api/workouts/program")
async def workouts_program_get() -> JSONResponse:
    overview = await asyncio.to_thread(get_program_overview)
    return JSONResponse({"program": overview["program"]})


@router.put("/api/workouts/program")
async def workouts_program_put(req: Request) -> JSONResponse:
    """Body: {goal, days_per_week, session_minutes, deload_every_weeks?}. Replaces the active program."""
    body = await _safe_json(req)
    result = await asyncio.to_thread(
        workout_setup.set_program,
        body.get("goal"),
        body.get("days_per_week"),
        body.get("session_minutes"),
        deload_every_weeks=body.get("deload_every_weeks"),
    )
    if not result.get("ok"):
        return _setup_response(result)
    overview = await asyncio.to_thread(get_program_overview)
    return JSONResponse({"ok": True, "program": overview["program"]})


@router.delete("/api/workouts/program")
async def workouts_program_delete() -> JSONResponse:
    return _setup_response(await asyncio.to_thread(workout_setup.clear_program))


@router.get("/api/workouts/locations")
async def workouts_locations_list() -> JSONResponse:
    locations = await asyncio.to_thread(state_store.list_workout_locations)
    return JSONResponse(
        {
            "locations": locations,
            "equipment_vocabulary": list(EQUIPMENT_TOKENS),
            "templates": LOCATION_TEMPLATES,
        }
    )


@router.post("/api/workouts/locations")
async def workouts_locations_create(req: Request) -> JSONResponse:
    """Body: {name, template? | equipment?, limits?}."""
    body = await _safe_json(req)
    result = await asyncio.to_thread(
        workout_setup.add_location,
        body.get("name"),
        body.get("template") or "commercial_gym",
        body.get("equipment"),
        body.get("limits"),
    )
    return _setup_response(result, ok_status=201)


@router.patch("/api/workouts/locations/{location_id}")
async def workouts_locations_update(location_id: int, req: Request) -> JSONResponse:
    """Body: any of {name, equipment, add_equipment, remove_equipment, limits}."""
    body = await _safe_json(req)
    result = await asyncio.to_thread(
        workout_setup.update_location,
        location_id,
        body.get("name"),
        body.get("equipment"),
        body.get("add_equipment"),
        body.get("remove_equipment"),
        body.get("limits"),
    )
    return _setup_response(result)


@router.delete("/api/workouts/locations/{location_id}")
async def workouts_locations_delete(location_id: int) -> JSONResponse:
    return _setup_response(await asyncio.to_thread(workout_setup.remove_location, location_id))


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
