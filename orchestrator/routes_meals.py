"""Meal API routes — dashboard-facing calorie log."""

import asyncio
import json
import logging
import os

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from orchestrator.meal_manager import (
    MEAL_PHOTOS_DIR,
    delete_meal,
    estimate_from_photo,
    get_history,
    get_stats,
    get_today,
    log_meal,
    save_photo_bytes,
    update_meal,
)

logger = logging.getLogger(__name__)

router = APIRouter()


async def _safe_json(req: Request) -> dict:
    """Parse a JSON body without crashing on empty/invalid input."""
    try:
        body = await req.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


@router.get("/api/meals/today")
async def meals_today() -> JSONResponse:
    today = await asyncio.to_thread(get_today)
    return JSONResponse(today)


@router.get("/api/meals/history")
async def meals_history(days: int = Query(7, ge=1, le=365)) -> JSONResponse:
    history = await asyncio.to_thread(get_history, days)
    stats = await asyncio.to_thread(get_stats, days)
    return JSONResponse({"days": days, "history": history, "stats": stats})


@router.post("/api/meals")
async def meals_create(req: Request) -> JSONResponse:
    body = await _safe_json(req)
    description = str(body.get("description") or "").strip()
    if not description:
        return JSONResponse({"error": "description is required"}, status_code=400)
    if len(description) > 500:
        description = description[:500]
    calories_raw = body.get("calories")
    calories: int | None
    if calories_raw in (None, ""):
        calories = None
    else:
        try:
            calories = int(calories_raw)
        except (TypeError, ValueError):
            return JSONResponse({"error": "calories must be an integer"}, status_code=400)
        if calories < 0 or calories > 20000:
            return JSONResponse({"error": "calories must be between 0 and 20000"}, status_code=400)
    meal_type = body.get("meal_type")
    # photo_path intentionally NOT accepted from the user — it's set only by
    # the photo upload route, so manual logs cannot point at arbitrary files.
    result = await asyncio.to_thread(log_meal, description, calories, meal_type, None, "manual")
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@router.patch("/api/meals/{meal_id}")
async def meals_update(meal_id: int, req: Request) -> JSONResponse:
    body = await _safe_json(req)
    if "calories" in body and body["calories"] is not None:
        try:
            cal = int(body["calories"])
        except (TypeError, ValueError):
            return JSONResponse({"error": "calories must be an integer"}, status_code=400)
        if cal < 0 or cal > 20000:
            return JSONResponse({"error": "calories must be between 0 and 20000"}, status_code=400)
        body["calories"] = cal
    result = await asyncio.to_thread(update_meal, meal_id, body)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@router.delete("/api/meals/{meal_id}")
async def meals_delete(meal_id: int) -> JSONResponse:
    result = await asyncio.to_thread(delete_meal, meal_id)
    if not result.get("ok"):
        return JSONResponse(result, status_code=404)
    return JSONResponse(result)


@router.post("/api/meals/photo")
async def meals_photo(
    file: UploadFile = File(...),
    auto_log: str = Form("false"),
    meal_type: str = Form(""),
) -> JSONResponse:
    """Upload a meal photo and get a vision-based calorie estimate.

    If auto_log=true, also persists a meal row with the estimate. Otherwise
    returns the estimate + photo_path for the user to confirm in the UI.
    """
    data = await file.read()
    if not data:
        return JSONResponse({"error": "empty file"}, status_code=400)
    if len(data) > 10 * 1024 * 1024:
        return JSONResponse({"error": "file too large (max 10MB)"}, status_code=413)

    ext = "jpg"
    if file.filename and "." in file.filename:
        ext = file.filename.rsplit(".", 1)[-1].lower()
    photo_path = await asyncio.to_thread(save_photo_bytes, data, ext)

    estimate = await estimate_from_photo(photo_path)
    if "error" in estimate:
        return JSONResponse(
            {"ok": False, "error": estimate["error"], "photo_path": photo_path},
            status_code=502,
        )

    response: dict = {
        "ok": True,
        "photo_path": photo_path,
        "estimate": estimate,
    }

    if auto_log.lower() == "true":
        logged = await asyncio.to_thread(
            log_meal,
            estimate.get("description", "Meal"),
            estimate.get("calories"),
            meal_type or None,
            photo_path,
            "photo",
        )
        response["meal"] = logged.get("meal")

    return JSONResponse(response)


@router.get("/api/meals/photo/{filename}")
async def meals_photo_get(filename: str):
    """Serve a stored meal photo for the dashboard preview."""
    safe = os.path.basename(filename)
    if not safe or safe != filename:
        return JSONResponse({"error": "invalid filename"}, status_code=400)
    path = os.path.realpath(os.path.join(MEAL_PHOTOS_DIR, safe))
    base = os.path.realpath(MEAL_PHOTOS_DIR)
    if not (path == base or path.startswith(base + os.sep)):
        return JSONResponse({"error": "invalid path"}, status_code=400)
    if not os.path.exists(path):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, media_type="image/jpeg")
