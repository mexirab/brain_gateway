"""
Exercise how-to library: start/end photos + numbered steps for each exercise.

Source: free-exercise-db (github.com/yuhonas/free-exercise-db, The Unlicense —
public domain), pinned to one commit. Nothing is committed to this repo; the
operator downloads it once onto the data volume:

    docker exec brain-orchestrator python -m orchestrator.exercise_library fetch

Layout under `settings.exercise_library_path`:
    library.json        trimmed entries {id, name, instructions, muscles, equipment, frames}
    meta.json           {source_sha, fetched_at, count}
    images/<id>/<n>.jpg two (sometimes more) frames per exercise

Lookup order for an exercise name: the hand-checked `_ALIASES` map for the seed
catalog (an explicit None means "no faithful match" — the dashboard shows a
video-search link instead of a wrong picture), then an exact normalized-name
match against the library, so a future catalog addition named like a library
entry gets photos without code changes. No fuzzy matching: a near-miss photo of
the wrong movement is worse than none.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus

from orchestrator.config import settings

logger = logging.getLogger(__name__)

SOURCE_REPO = "yuhonas/free-exercise-db"
SOURCE_SHA = "f00c92c7dcf1216a928a52c3706c7ce8e2f71ed5"  # 2026-09-27
_RAW_BASE = f"https://raw.githubusercontent.com/{SOURCE_REPO}/{SOURCE_SHA}"

_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,120}$")
_MAX_FRAMES = 4
_MAX_IMAGE_BYTES = 3 * 1024 * 1024
_MAX_JSON_BYTES = 20 * 1024 * 1024
_MAX_STEPS = 15
_MAX_STEP_CHARS = 1000

# Seed catalog name → library id. None = checked, no faithful match.
_ALIASES: dict[str, Optional[str]] = {
    "Back Squat": "Barbell_Squat",
    "Front Squat": "Front_Barbell_Squat",
    "Goblet Squat": "Goblet_Squat",
    "Leg Press": "Leg_Press",
    "Bulgarian Split Squat": "Split_Squat_with_Dumbbells",
    "Walking Lunge": "Dumbbell_Lunges",
    "Leg Extension": "Leg_Extensions",
    "Deadlift": "Barbell_Deadlift",
    "Romanian Deadlift": "Romanian_Deadlift",
    "Dumbbell RDL": "Stiff-Legged_Dumbbell_Deadlift",
    "Trap Bar Deadlift": "Trap_Bar_Deadlift",
    "Hip Thrust": "Barbell_Hip_Thrust",
    "Kettlebell Swing": "One-Arm_Kettlebell_Swings",
    "Leg Curl": "Lying_Leg_Curls",
    "Good Morning": "Good_Morning",
    "Bench Press": "Barbell_Bench_Press_-_Medium_Grip",
    "Incline Bench Press": "Barbell_Incline_Bench_Press_-_Medium_Grip",
    "Dumbbell Bench Press": "Dumbbell_Bench_Press",
    "Incline Dumbbell Press": "Incline_Dumbbell_Press",
    "Push-Up": "Pushups",
    "Dips": "Dips_-_Chest_Version",
    "Cable Chest Fly": "Cable_Crossover",
    "Overhead Press": "Standing_Military_Press",
    "Dumbbell Shoulder Press": "Dumbbell_Shoulder_Press",
    "Arnold Press": "Arnold_Dumbbell_Press",
    "Lateral Raise": "Side_Lateral_Raise",
    "Rear Delt Fly": "Seated_Bent-Over_Rear_Delt_Raise",
    "Barbell Row": "Bent_Over_Barbell_Row",
    "Dumbbell Row": "One-Arm_Dumbbell_Row",
    "Seated Cable Row": "Seated_Cable_Rows",
    "T-Bar Row": "T-Bar_Row_with_Handle",
    "Chest-Supported Row": "Dumbbell_Incline_Row",
    "Face Pull": "Face_Pull",
    "Pull-Up": "Pullups",
    "Chin-Up": "Chin-Up",
    "Lat Pulldown": "Wide-Grip_Lat_Pulldown",
    "Barbell Curl": "Barbell_Curl",
    "Dumbbell Curl": "Dumbbell_Bicep_Curl",
    "Hammer Curl": "Hammer_Curls",
    "Preacher Curl": "Preacher_Curl",
    "Tricep Pushdown": "Triceps_Pushdown",
    "Skull Crusher": "EZ-Bar_Skullcrusher",
    "Overhead Tricep Extension": "Standing_Dumbbell_Triceps_Extension",
    "Plank": "Plank",
    "Hanging Leg Raise": "Hanging_Leg_Raise",
    "Cable Crunch": "Cable_Crunch",
    "Ab Wheel Rollout": "Ab_Roller",
    "Russian Twist": "Russian_Twist",
    "Standing Calf Raise": "Standing_Calf_Raises",
    "Seated Calf Raise": "Seated_Calf_Raise",
    "Farmer's Carry": "Farmers_Walk",
    "Shrug": "Barbell_Shrug",
    "Bodyweight Squat": "Bodyweight_Squat",
    "Bodyweight Reverse Lunge": None,
    "Kettlebell Goblet Squat": "Goblet_Squat",
    "Kettlebell Deadlift": None,
    "Kettlebell Clean": "One-Arm_Kettlebell_Clean",
    "Single-Leg Dumbbell RDL": "Kettlebell_One-Legged_Deadlift",
    "Dumbbell Reverse Lunge": "Dumbbell_Rear_Lunge",
    "Kettlebell Front-Rack Lunge": None,
    "Dumbbell Floor Press": "Dumbbell_Floor_Press",
    "Kettlebell Floor Press": "One-Arm_Kettlebell_Floor_Press",
    "Kettlebell Overhead Press": "One-Arm_Kettlebell_Military_Press_To_The_Side",
    "Kettlebell Push Press": "One-Arm_Kettlebell_Push_Press",
    "Kettlebell Single-Arm Row": "One-Arm_Kettlebell_Row",
    "Dumbbell Renegade Row": "Alternating_Renegade_Row",
    "Dumbbell Floor Pullover": None,
    "Turkish Get-Up": "Kettlebell_Turkish_Get-Up_Lunge_style",
    "Kettlebell Suitcase Carry": None,
    "Dumbbell Floor Skull Crusher": None,
    "Single-Leg Dumbbell Calf Raise": "Standing_Dumbbell_Calf_Raise",
    "Glute Bridge": "Butt_Lift_Bridge",
}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


_ALIASES_NORM: dict[str, Optional[str]] = {_norm(k): v for k, v in _ALIASES.items()}


def _root() -> Path:
    return Path(settings.exercise_library_path)


# ---------------------------------------------------------------------------
# Lookup (runtime)
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"mtime": None, "by_id": {}, "by_name": {}}


def _valid_loaded(e: Any) -> bool:
    """Shape check for library.json entries (a hand-edited file must not 500)."""
    return (
        isinstance(e, dict)
        and isinstance(e.get("id"), str)
        and bool(_ID_RE.match(e["id"]))
        and isinstance(e.get("name"), str)
        and isinstance(e.get("frames"), int)
        and isinstance(e.get("instructions"), list)
        and isinstance(e.get("primary_muscles"), list)
        and isinstance(e.get("equipment"), str)
    )


def _load() -> tuple[dict[str, dict], dict[str, str]]:
    """Return (by_id, by_norm_name), reloading when library.json changes.

    A corrupt file is logged once per mtime and treated as "no library".
    """
    path = _root() / "library.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}, {}
    with _cache_lock:
        if _cache["mtime"] == mtime:
            return _cache["by_id"], _cache["by_name"]
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(entries, list):
                raise ValueError("top level is not a list")
        except (OSError, ValueError) as e:
            logger.error("[EXERCISE_LIBRARY] Unreadable %s: %s", path, e)
            _cache.update(mtime=mtime, by_id={}, by_name={})
            return {}, {}
        by_id = {e["id"]: e for e in entries if _valid_loaded(e)}
        if len(by_id) < len(entries):
            logger.warning("[EXERCISE_LIBRARY] Skipped %d malformed entries", len(entries) - len(by_id))
        by_name = {_norm(e["name"]): e["id"] for e in by_id.values()}
        _cache.update(mtime=mtime, by_id=by_id, by_name=by_name)
        logger.info("[EXERCISE_LIBRARY] Loaded %d exercises", len(by_id))
        return by_id, by_name


def is_available() -> bool:
    return bool(_load()[0])


def _resolve(name: str) -> Optional[dict]:
    by_id, by_name = _load()
    key = _norm(name)
    if key in _ALIASES_NORM:
        lib_id = _ALIASES_NORM[key]
        return by_id.get(lib_id) if lib_id else None
    lib_id = by_name.get(key)
    return by_id.get(lib_id) if lib_id else None


def video_search_url(name: str) -> str:
    return "https://www.youtube.com/results?search_query=" + quote_plus(f"{name} exercise form")


def get_info(name: str) -> dict:
    """How-to payload for one exercise name; never raises."""
    from orchestrator.metrics import EXERCISE_INFO_LOOKUPS

    name = name.strip()[:200]
    available = is_available()
    entry = _resolve(name) if available else None
    result = "found" if entry else ("not_found" if available else "library_missing")
    EXERCISE_INFO_LOOKUPS.labels(result=result).inc()
    info: dict[str, Any] = {
        "name": name,
        "library_available": available,
        "found": entry is not None,
        "video_url": video_search_url(name),
    }
    if entry:
        info.update(
            match=entry["name"],
            instructions=entry["instructions"],
            primary_muscles=entry["primary_muscles"],
            equipment=entry["equipment"],
            frames=[
                f"/api/workouts/exercise-library/{entry['id']}/{n}.jpg?v={SOURCE_SHA[:7]}"
                for n in range(entry["frames"])
            ],
        )
    return info


def image_file(lib_id: str, frame: int) -> Optional[Path]:
    """Path of one frame, only for ids/frames present in the loaded index."""
    by_id, _ = _load()
    entry = by_id.get(lib_id)
    if not entry or not (0 <= frame < entry["frames"]):
        return None
    root = (_root() / "images").resolve()
    path = (root / lib_id / f"{frame}.jpg").resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


# ---------------------------------------------------------------------------
# Fetch (operator CLI)
# ---------------------------------------------------------------------------


def _clean_entry(raw: Any) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    lib_id = str(raw.get("id") or "")
    name = raw.get("name")
    if not _ID_RE.match(lib_id) or not isinstance(name, str) or not name.strip():
        return None
    frames = 0
    for i, img in enumerate((raw.get("images") or [])[:_MAX_FRAMES]):
        if img != f"{lib_id}/{i}.jpg":
            break
        frames = i + 1
    steps = [s.strip()[:_MAX_STEP_CHARS] for s in (raw.get("instructions") or []) if isinstance(s, str) and s.strip()]

    def _strs(v: Any) -> list[str]:
        return [str(x)[:40] for x in v if isinstance(x, str)][:8] if isinstance(v, list) else []

    return {
        "id": lib_id,
        "name": name.strip()[:200],
        "instructions": steps[:_MAX_STEPS],
        "primary_muscles": _strs(raw.get("primaryMuscles")),
        "secondary_muscles": _strs(raw.get("secondaryMuscles")),
        "equipment": str(raw.get("equipment") or "")[:40],
        "category": str(raw.get("category") or "")[:40],
        "frames": frames,
    }


async def _get_capped(client: Any, url: str, cap: int) -> bytes:
    """GET url, aborting once the body passes `cap` bytes (caps the download, not just storage)."""
    async with client.stream("GET", url) as resp:
        resp.raise_for_status()
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > cap:
                raise ValueError(f"larger than {cap} bytes")
            chunks.append(chunk)
        return b"".join(chunks)


# Refuse to replace the live library when more than this share of photos failed
# (rate limit / network blip) — a photo-less library over a good one is worse.
_MAX_FAILED_SHARE = 0.05


async def _fetch(force: bool = False) -> int:
    import httpx

    root = _root()
    meta_path = root / "meta.json"
    if not force and meta_path.exists():
        try:
            if json.loads(meta_path.read_text()).get("source_sha") == SOURCE_SHA:
                print(f"Already up to date ({SOURCE_SHA[:7]}). Use --force to re-download.")
                return 0
        except (OSError, ValueError):
            pass

    staging = root.with_name(root.name + ".new")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        (staging / "images").mkdir(parents=True)
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            raw = json.loads(await _get_capped(client, f"{_RAW_BASE}/dist/exercises.json", _MAX_JSON_BYTES))
            if not isinstance(raw, list):
                raise ValueError("exercises.json is not a list")
            entries = [e for e in (_clean_entry(r) for r in raw) if e]
            if not entries:
                raise ValueError("exercises.json has no usable entries")
            print(f"{len(entries)} exercises in the index; downloading photos…")

            sem = asyncio.Semaphore(8)
            failed: list[str] = []

            async def grab(entry: dict, frame: int) -> None:
                rel = f"{entry['id']}/{frame}.jpg"
                async with sem:
                    try:
                        body = await _get_capped(client, f"{_RAW_BASE}/exercises/{rel}", _MAX_IMAGE_BYTES)
                        if not body.startswith(b"\xff\xd8\xff"):
                            raise ValueError("not a JPEG")
                    except (httpx.HTTPError, ValueError) as e:
                        failed.append(f"{rel}: {e}")
                        return
                dest = staging / "images" / entry["id"]
                dest.mkdir(exist_ok=True)
                (dest / f"{frame}.jpg").write_bytes(body)

            expected = sum(e["frames"] for e in entries)
            await asyncio.gather(*(grab(e, n) for e in entries for n in range(e["frames"])))
    except (httpx.HTTPError, ValueError, OSError) as e:
        shutil.rmtree(staging, ignore_errors=True)
        print(f"Fetch failed, existing library left untouched: {e}")
        return 1

    if failed:
        print(f"{len(failed)} of {expected} photo(s) failed:")
        for f in failed[:20]:
            print("  ", f)
    if expected and len(failed) / expected > _MAX_FAILED_SHARE:
        shutil.rmtree(staging, ignore_errors=True)
        print("Too many failures — existing library left untouched. Try again later.")
        return 1

    # An exercise keeps only the frames that actually landed (0..k-1).
    for e in entries:
        k = 0
        while k < e["frames"] and (staging / "images" / e["id"] / f"{k}.jpg").is_file():
            k += 1
        e["frames"] = k

    (staging / "library.json").write_text(json.dumps(entries), encoding="utf-8")
    (staging / "meta.json").write_text(
        json.dumps(
            {"source_repo": SOURCE_REPO, "source_sha": SOURCE_SHA, "fetched_at": time.time(), "count": len(entries)}
        ),
        encoding="utf-8",
    )
    old = root.with_name(root.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    had_root = root.exists()
    if had_root:
        os.replace(root, old)
    try:
        os.replace(staging, root)
    except OSError as e:
        if had_root:
            os.replace(old, root)  # put the previous library back
        print(f"Could not install the new library ({e}); previous one restored.")
        return 1
    shutil.rmtree(old, ignore_errors=True)

    photos = sum(e["frames"] for e in entries)
    print(f"Done: {len(entries)} exercises, {photos} photos in {root}.")
    # Partial (under the threshold) still installs, but report it as non-zero.
    return 1 if failed else 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m orchestrator.exercise_library")
    sub = parser.add_subparsers(dest="cmd", required=True)
    fetch = sub.add_parser("fetch", help="download the pinned exercise library onto the data volume")
    fetch.add_argument("--force", action="store_true", help="re-download even if already at the pinned commit")
    sub.add_parser("check", help="report how the seed catalog maps onto the downloaded library")
    args = parser.parse_args(argv)

    if args.cmd == "fetch":
        return asyncio.run(_fetch(force=args.force))

    from orchestrator.exercises_seed import EXERCISES

    if not is_available():
        print(f"No library at {_root()} — run `fetch` first.")
        return 1
    missing = [e["name"] for e in EXERCISES if _resolve(e["name"]) is None]
    print(f"{len(EXERCISES) - len(missing)}/{len(EXERCISES)} catalog exercises have photos.")
    for n in missing:
        print("  video link only:", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
