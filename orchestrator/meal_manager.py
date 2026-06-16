"""
Meal manager — calorie-only meal logging with optional photo estimation.

V1 scope:
- Manual meal entry (description + calories + meal_type)
- Photo-based calorie estimation via Qwen2.5-VL (routed through vision_handler)
- Today/history/stats queries for the dashboard and Jess tools

No macro targets, no protein/carb/fat tracking. Can extend later.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from orchestrator import shared, state_store
from orchestrator.vision_handler import analyze_image

logger = logging.getLogger(__name__)

# Where meal photos live on disk. Matches the pattern used by documents/.
MEAL_PHOTOS_DIR = os.environ.get("MEAL_PHOTOS_DIR", "/app/data/meal_photos")

_VALID_MEAL_TYPES = {"breakfast", "lunch", "dinner", "snack"}
_ALLOWED_PHOTO_EXTS = {"jpg", "jpeg", "png", "gif", "webp"}


def _is_under_photos_dir(path: str) -> bool:
    """Return True iff `path` resolves to a file inside MEAL_PHOTOS_DIR."""
    if not path:
        return False
    try:
        base = os.path.realpath(MEAL_PHOTOS_DIR)
        target = os.path.realpath(path)
    except (OSError, ValueError):
        return False
    return target == base or target.startswith(base + os.sep)


# ---------------------------------------------------------------------------
# Photo storage + vision estimate
# ---------------------------------------------------------------------------


def _ensure_photos_dir() -> None:
    os.makedirs(MEAL_PHOTOS_DIR, exist_ok=True)


def save_photo_bytes(data: bytes, extension: str = "jpg") -> str:
    """Save raw photo bytes to disk and return the absolute path.

    Forces the file extension into a small image-only allowlist to prevent
    storing executable/scripty content under MEAL_PHOTOS_DIR.
    """
    _ensure_photos_dir()
    ext = (extension or "").lower().lstrip(".")
    if ext == "jpg" or ext not in _ALLOWED_PHOTO_EXTS:
        ext = "jpg"
    filename = f"{uuid.uuid4().hex}.{ext}"
    path = os.path.join(MEAL_PHOTOS_DIR, filename)
    with open(path, "wb") as f:
        f.write(data)
    return path


_ESTIMATE_PROMPT = (
    "You are a nutrition estimator. Look at this photo of a meal and estimate the total "
    "calories in the entire visible meal. Be conservative. Return ONLY valid JSON in this "
    'exact shape: {"description": "short plain-English description", "calories": <integer>, '
    '"confidence": "low"|"medium"|"high"}. No prose, no markdown, no code fences.'
)


async def estimate_from_photo(photo_path: str) -> Dict[str, Any]:
    """Send a saved photo to the vision model and parse the JSON response.

    Returns {description, calories, confidence, raw} or {error}.
    """
    if not shared.VISION_ENABLED:
        return {"error": "Vision model is disabled. Set VISION_ENABLED=true."}

    if not _is_under_photos_dir(photo_path):
        return {"error": "Refusing to read photo outside MEAL_PHOTOS_DIR."}

    def _read() -> bytes:
        with open(photo_path, "rb") as f:
            return f.read()

    try:
        raw = await asyncio.to_thread(_read)
    except OSError as e:
        return {"error": f"Could not read photo: {e}"}

    ext = os.path.splitext(photo_path)[1].lstrip(".").lower() or "jpeg"
    if ext == "jpg":
        ext = "jpeg"
    mime = f"image/{ext}"
    data_uri = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    try:
        text = await analyze_image(data_uri, prompt=_ESTIMATE_PROMPT)
    except Exception as e:
        logger.warning("[MEAL] Vision estimate failed: %s", e)
        return {"error": f"Vision call failed: {e}"}

    parsed = _parse_estimate(text)
    parsed["raw"] = text
    return parsed


def _parse_estimate(text: str) -> Dict[str, Any]:
    """Best-effort JSON parse from the vision model response."""
    if not text:
        return {"error": "Empty response from vision model."}
    # Try direct parse
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        # Pull out the first {...} block
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {"error": "Could not parse JSON from vision response.", "raw": text[:500]}
        try:
            obj = json.loads(match.group(0))
        except (ValueError, TypeError):
            return {"error": "Invalid JSON from vision response.", "raw": text[:500]}

    desc = str(obj.get("description") or "").strip()[:300] or "Meal"
    cal_raw = obj.get("calories")
    try:
        calories = int(round(float(cal_raw))) if cal_raw is not None else None
    except (ValueError, TypeError):
        calories = None
    confidence = str(obj.get("confidence") or "").lower().strip() or "medium"
    if confidence not in {"low", "medium", "high"}:
        confidence = "medium"
    return {"description": desc, "calories": calories, "confidence": confidence}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _classify_meal_type() -> str:
    """Infer meal type from current time."""
    hour = datetime.now().hour
    if 5 <= hour < 11:
        return "breakfast"
    if 11 <= hour < 15:
        return "lunch"
    if 15 <= hour < 18:
        return "snack"
    if 18 <= hour < 22:
        return "dinner"
    return "snack"


def log_meal(
    description: str,
    calories: Optional[int] = None,
    meal_type: Optional[str] = None,
    photo_path: Optional[str] = None,
    source: str = "manual",
) -> Dict[str, Any]:
    """Persist a new meal row. Returns the created meal."""
    if not description or not description.strip():
        return {"ok": False, "error": "Description is required."}

    mt = (meal_type or "").lower().strip()
    if mt not in _VALID_MEAL_TYPES:
        mt = _classify_meal_type()

    # Only photos saved under MEAL_PHOTOS_DIR are allowed — prevents
    # confused-deputy attacks where a user supplies an arbitrary path
    # and later triggers os.remove() through delete_meal().
    safe_photo_path: Optional[str] = None
    if photo_path:
        if _is_under_photos_dir(photo_path):
            safe_photo_path = photo_path
        else:
            logger.warning("[MEAL] Rejected photo_path outside MEAL_PHOTOS_DIR: %r", photo_path)

    if calories is not None:
        try:
            calories = int(calories)
        except (TypeError, ValueError):
            calories = None
        else:
            if calories < 0 or calories > 20000:
                calories = None

    row = state_store.add_meal(
        description=description.strip()[:500],
        meal_type=mt,
        calories=calories,
        photo_path=safe_photo_path,
        source=source,
    )
    logger.info(
        "[MEAL] Logged: %s (%s, %s kcal)",
        description[:60],
        mt,
        calories if calories is not None else "?",
    )

    # Bridge to the self-care nudge gate so the "no meals logged today"
    # scheduler nudge sees this meal too. meals and selfcare_log live in
    # separate tables; without this the nudge fires every tick past 12pm
    # regardless of how recently we logged lunch.
    try:
        from orchestrator.selfcare_manager import record_meal_logged

        record_meal_logged(mt or description.strip()[:60])
    except Exception as e:
        logger.warning("[MEAL] Selfcare nudge-gate bridge failed: %s", e)

    return {"ok": True, "meal": row}


def update_meal(meal_id: int, updates: Dict[str, Any]) -> Dict[str, Any]:
    ok = state_store.update_meal(meal_id, updates)
    if not ok:
        return {"ok": False, "error": "Meal not found or no valid fields to update."}
    return {"ok": True, "meal": state_store.get_meal(meal_id)}


def delete_meal(meal_id: int) -> Dict[str, Any]:
    meal = state_store.delete_meal(meal_id)
    if not meal:
        return {"ok": False, "error": "Meal not found."}
    # Best-effort cleanup of the photo file — only if it lives under
    # MEAL_PHOTOS_DIR. Defends against poisoned legacy rows whose
    # photo_path was set before sanitization landed.
    path = meal.get("photo_path")
    if path and _is_under_photos_dir(path) and os.path.exists(path):
        with contextlib.suppress(OSError):
            os.remove(path)
    elif path:
        logger.warning("[MEAL] Skipped photo cleanup for unsafe path: %r", path)
    return {"ok": True, "meal": meal}


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def get_today() -> Dict[str, Any]:
    meals = state_store.get_meals_today()
    total = sum(int(m.get("calories") or 0) for m in meals)
    return {
        "meals": meals,
        "total_calories": total,
        "meal_count": len(meals),
    }


def get_history(days: int = 7) -> List[Dict[str, Any]]:
    """Return daily rollups for the last N days: [{date, total_calories, meal_count}]."""
    meals = state_store.get_meals_recent(days=days)
    by_date: Dict[str, Dict[str, Any]] = {}
    for m in meals:
        day = m["logged_at"][:10]
        entry = by_date.setdefault(day, {"date": day, "total_calories": 0, "meal_count": 0, "meals": []})
        entry["total_calories"] += int(m.get("calories") or 0)
        entry["meal_count"] += 1
        entry["meals"].append(m)
    return sorted(by_date.values(), key=lambda e: e["date"], reverse=True)


def get_stats(days: int = 7) -> Dict[str, Any]:
    history = get_history(days=days)
    if not history:
        return {"days": days, "avg_calories": 0, "total_meals": 0, "day_count": 0}
    totals = [h["total_calories"] for h in history if h["total_calories"] > 0]
    avg = round(sum(totals) / len(totals)) if totals else 0
    return {
        "days": days,
        "avg_calories": avg,
        "total_meals": sum(h["meal_count"] for h in history),
        "day_count": len(history),
    }
