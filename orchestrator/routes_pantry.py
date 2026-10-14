"""Pantry API routes (F-015 phase 1) — inventory, photo scans, barcode lookup.

Mounted only when PANTRY_ENABLED (see api_routes.py). Bearer-gated by the
global middleware; the dashboard reaches these through /api/proxy/*.
"""

import asyncio
import json
import logging
from typing import List

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from orchestrator import diet_profiles, meal_planner, shared
from orchestrator import pantry_manager as pm

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pantry")

# Per-ID routes accept only positive SQLite-range ints (FastAPI would 500 on
# anything past int64 when it reaches sqlite3).
_MAX_ID = 2**62


async def _safe_json(req: Request) -> dict:
    try:
        body = await req.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def _bad_id(item_id: int) -> bool:
    return not 0 < item_id < _MAX_ID


def _result(res: dict, ok_status: int = 200) -> JSONResponse:
    status = res.pop("status_code", None)
    if res.get("ok"):
        return JSONResponse(res, status_code=ok_status)
    if status is None:
        status = 404 if str(res.get("error", "")).endswith("not found") else 400
    return JSONResponse(res, status_code=status)


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


@router.get("/items")
async def pantry_items(
    status: str = Query("active", pattern="^(active|probably_gone|gone|all)$"),
    location: str = Query("", pattern="^(|fridge|freezer|pantry)$"),
) -> JSONResponse:
    items = await asyncio.to_thread(pm.list_items, status, location or None)
    return JSONResponse({"items": items, "count": len(items)})


@router.post("/items")
async def pantry_add(req: Request) -> JSONResponse:
    body = await _safe_json(req)
    if not isinstance(body.get("name"), str):
        return JSONResponse({"ok": False, "error": "name must be a string"}, status_code=400)
    res = await asyncio.to_thread(
        pm.add_item,
        body.get("name"),
        body.get("category"),
        body.get("location"),
        body.get("track_mode"),
        body.get("qty"),
        body.get("level"),
        body.get("shelf_life_days"),
        body.get("barcode"),
        "barcode" if body.get("barcode") else "manual",
    )
    return _result(res, ok_status=201 if res.get("ok") and not res.get("merged") else 200)


@router.patch("/items/{item_id}")
async def pantry_update(item_id: int, req: Request) -> JSONResponse:
    if _bad_id(item_id):
        return JSONResponse({"ok": False, "error": "item not found"}, status_code=404)
    body = await _safe_json(req)
    return _result(await asyncio.to_thread(pm.update_item, item_id, body, "manual"))


@router.post("/items/{item_id}/use")
async def pantry_use(item_id: int, req: Request) -> JSONResponse:
    if _bad_id(item_id):
        return JSONResponse({"ok": False, "error": "item not found"}, status_code=404)
    body = await _safe_json(req)
    return _result(await asyncio.to_thread(pm.consume_item, item_id, body.get("count"), "manual"))


@router.post("/items/{item_id}/out")
async def pantry_out(item_id: int) -> JSONResponse:
    if _bad_id(item_id):
        return JSONResponse({"ok": False, "error": "item not found"}, status_code=404)
    return _result(await asyncio.to_thread(pm.mark_out, item_id, "manual"))


@router.delete("/items/{item_id}")
async def pantry_delete(item_id: int) -> JSONResponse:
    if _bad_id(item_id):
        return JSONResponse({"ok": False, "error": "item not found"}, status_code=404)
    return _result(await asyncio.to_thread(pm.delete_item, item_id, "manual"))


@router.get("/events")
async def pantry_events(item_id: int | None = Query(None, ge=1, lt=_MAX_ID), limit: int = Query(50, ge=1, le=500)):
    events = await asyncio.to_thread(pm.get_events, item_id, limit)
    return JSONResponse({"events": events})


# ---------------------------------------------------------------------------
# Photo scans
# ---------------------------------------------------------------------------


@router.post("/scan")
async def pantry_scan_create(
    files: List[UploadFile] = File(...),
    kind: str = Form("fridge"),
) -> JSONResponse:
    """Upload 1-8 shelf photos. Returns immediately with a scan id; vision runs
    in the background (one photo at a time). Poll GET /scan/{id}."""
    if kind not in pm.SCAN_KINDS:
        return JSONResponse({"ok": False, "error": f"kind must be one of {', '.join(pm.SCAN_KINDS)}"}, status_code=400)
    if not files:
        return JSONResponse({"ok": False, "error": "at least one photo is required"}, status_code=400)
    if len(files) > pm.MAX_PHOTOS_PER_SCAN:
        return JSONResponse(
            {"ok": False, "error": f"too many photos (max {pm.MAX_PHOTOS_PER_SCAN} per scan)"}, status_code=400
        )
    if not shared.VISION_ENABLED:
        return JSONResponse({"ok": False, "error": "vision is disabled (VISION_ENABLED=false)"}, status_code=409)

    if await asyncio.to_thread(pm.count_open_scans) >= pm.MAX_OPEN_SCANS:
        return JSONResponse(
            {"ok": False, "error": "finish or discard your open scans first (max 3 at a time)"}, status_code=429
        )

    # Per photo: never more than the vision model will accept, or the scan
    # would upload fine and then fail in the background.
    cap = min(shared.PANTRY_MAX_UPLOAD_MB * 1024 * 1024, shared.VISION_MAX_IMAGE_SIZE)
    saved: List[str] = []
    for f in files:
        data = await f.read(cap + 1)
        if not data:
            pm._delete_photos(saved)
            return JSONResponse({"ok": False, "error": "empty photo"}, status_code=400)
        if len(data) > cap:
            pm._delete_photos(saved)
            return JSONResponse(
                {"ok": False, "error": f"photo too large (max {cap // (1024 * 1024)}MB)"}, status_code=413
            )
        res = await asyncio.to_thread(pm.save_scan_photo, data)
        if not res.get("ok"):
            pm._delete_photos(saved)
            return JSONResponse(res, status_code=415)
        saved.append(res["path"])

    scan = await asyncio.to_thread(pm.create_scan, kind, saved)
    pm.start_scan(scan["id"])
    logger.info("[PANTRY] Scan %s queued: %s, %d photo(s)", scan["id"], kind, len(saved))
    return JSONResponse({"ok": True, "scan_id": scan["id"], "status": "queued"}, status_code=202)


@router.get("/scans")
async def pantry_scans(
    status: str = Query("", pattern="^(|queued|processing|pending_review|failed|committed|discarded)$"),
    limit: int = Query(10, ge=1, le=50),
) -> JSONResponse:
    scans = await asyncio.to_thread(pm.list_scans, status or None, limit)
    return JSONResponse({"scans": scans})


@router.get("/scan/{scan_id}")
async def pantry_scan_get(scan_id: int) -> JSONResponse:
    if _bad_id(scan_id):
        return JSONResponse({"ok": False, "error": "scan not found"}, status_code=404)
    scan = await asyncio.to_thread(pm.get_scan, scan_id)
    if not scan:
        return JSONResponse({"ok": False, "error": "scan not found"}, status_code=404)
    return JSONResponse(scan)


@router.get("/scan/{scan_id}/photo/{index}")
async def pantry_scan_photo(scan_id: int, index: int):
    """Serve one scan photo (review screen thumbnails). Gone after commit/discard."""
    if _bad_id(scan_id) or not 0 <= index < pm.MAX_PHOTOS_PER_SCAN:
        return JSONResponse({"error": "not found"}, status_code=404)
    path = await asyncio.to_thread(pm.scan_photo_path, scan_id, index)
    if not path:
        return JSONResponse({"error": "not found"}, status_code=404)
    ext = path.rsplit(".", 1)[-1]
    media = {"png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")
    return FileResponse(path, media_type=media, headers={"Cache-Control": "private, no-store"})


@router.post("/scan/{scan_id}/commit")
async def pantry_scan_commit(scan_id: int, req: Request) -> JSONResponse:
    if _bad_id(scan_id):
        return JSONResponse({"ok": False, "error": "scan not found"}, status_code=404)
    body = await _safe_json(req)
    return _result(await asyncio.to_thread(pm.commit_scan, scan_id, body.get("items")))


@router.post("/scan/{scan_id}/discard")
async def pantry_scan_discard(scan_id: int) -> JSONResponse:
    if _bad_id(scan_id):
        return JSONResponse({"ok": False, "error": "scan not found"}, status_code=404)
    return _result(await asyncio.to_thread(pm.discard_scan, scan_id))


@router.post("/scan/{scan_id}/retry")
async def pantry_scan_retry(scan_id: int) -> JSONResponse:
    if _bad_id(scan_id):
        return JSONResponse({"ok": False, "error": "scan not found"}, status_code=404)
    res = await asyncio.to_thread(pm.retry_scan, scan_id)
    if res.get("ok"):
        pm.start_scan(scan_id)
        return JSONResponse({"ok": True, "scan_id": scan_id, "status": "queued"}, status_code=202)
    return _result(res)


# ---------------------------------------------------------------------------
# Barcode
# ---------------------------------------------------------------------------


@router.get("/barcode/{code}")
async def pantry_barcode(code: str) -> JSONResponse:
    res = await pm.lookup_barcode(code)
    if res.get("ok"):
        return JSONResponse(res)
    status = {"invalid": 400, "disabled": 409, "miss": 404}.get(res.get("reason"), 502)
    return JSONResponse(res, status_code=status)


# ---------------------------------------------------------------------------
# Diet settings + weekly meal plan (F-015 phase 2)
# ---------------------------------------------------------------------------


@router.get("/diet")
async def pantry_diet_get() -> JSONResponse:
    settings = await asyncio.to_thread(diet_profiles.load)
    return JSONResponse({"settings": settings, "profiles": diet_profiles.public_profiles()})


@router.put("/diet")
async def pantry_diet_put(req: Request) -> JSONResponse:
    body = await _safe_json(req)
    before = await asyncio.to_thread(diet_profiles.load)
    try:
        settings = await asyncio.to_thread(diet_profiles.save, body)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    moved = 0
    if settings["reminders"] != before["reminders"]:
        # New reminder times apply to the plan already accepted, too.
        moved = await asyncio.to_thread(meal_planner.reschedule_active_plans)
    return JSONResponse({"ok": True, "settings": settings, "rescheduled_plans": moved})


@router.get("/plan")
async def pantry_plan_current() -> JSONResponse:
    return JSONResponse(await asyncio.to_thread(meal_planner.current_plans))


@router.post("/plan/generate")
async def pantry_plan_generate() -> JSONResponse:
    if not await meal_planner.brain_ready():
        meal_planner.MEAL_PLAN_GENERATED.labels(result="asleep").inc()
        return JSONResponse({"ok": False, "error": meal_planner.asleep_message()}, status_code=503)
    res = await asyncio.to_thread(meal_planner.start_generation_sync)
    if res.get("ok"):
        meal_planner.launch(res["plan_id"])
        return JSONResponse(res, status_code=202)
    return _result(res)


@router.get("/plan/{plan_id}")
async def pantry_plan_get(plan_id: int) -> JSONResponse:
    if _bad_id(plan_id):
        return JSONResponse({"ok": False, "error": "plan not found"}, status_code=404)
    plan = await asyncio.to_thread(meal_planner.get_plan, plan_id)
    if not plan:
        return JSONResponse({"ok": False, "error": "plan not found"}, status_code=404)
    return JSONResponse(plan)


@router.post("/plan/{plan_id}/accept")
async def pantry_plan_accept(plan_id: int, req: Request) -> JSONResponse:
    if _bad_id(plan_id):
        return JSONResponse({"ok": False, "error": "plan not found"}, status_code=404)
    body = await _safe_json(req)
    return _result(await asyncio.to_thread(meal_planner.accept_plan, plan_id, body.get("already_have")))


@router.post("/plan/{plan_id}/discard")
async def pantry_plan_discard(plan_id: int) -> JSONResponse:
    if _bad_id(plan_id):
        return JSONResponse({"ok": False, "error": "plan not found"}, status_code=404)
    return _result(await asyncio.to_thread(meal_planner.discard_plan, plan_id))


@router.post("/plan/entry/{entry_id}/swap")
async def pantry_plan_swap(entry_id: int) -> JSONResponse:
    if _bad_id(entry_id):
        return JSONResponse({"ok": False, "error": "entry not found"}, status_code=404)
    return _result(await meal_planner.swap_entry(entry_id))


@router.post("/recipe/{recipe_id}/rate")
async def pantry_recipe_rate(recipe_id: int, req: Request) -> JSONResponse:
    if _bad_id(recipe_id):
        return JSONResponse({"ok": False, "error": "recipe not found"}, status_code=404)
    body = await _safe_json(req)
    return _result(await asyncio.to_thread(meal_planner.rate_recipe, recipe_id, body.get("rating")))
