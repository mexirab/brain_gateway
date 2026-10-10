"""
Workout program builder setup — workout locations (what gear each place has)
and the weekly training program.

One validation layer shared by the dashboard routes (`routes_workout.py`) and
the `workout_setup` chat tool, so both paths enforce the same rules. Every
function returns `{"ok": True, ...}` or `{"ok": False, "error": ...}` and
never raises for bad input.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import unicodedata
from typing import Any, Dict, List, Optional

from orchestrator import state_store
from orchestrator.exercises_seed import EQUIPMENT_TOKENS, LOCATION_TEMPLATES

logger = logging.getLogger(__name__)

GOALS = ("strength", "muscle", "general")
MIN_DAYS, MAX_DAYS = 2, 6
MIN_MINUTES, MAX_MINUTES = 20, 120
MAX_LOCATIONS = 20
DEFAULT_DELOAD_WEEKS = 6
MIN_DELOAD_WEEKS, MAX_DELOAD_WEEKS = 3, 12  # 0 = no scheduled deload weeks
_MAX_NAME_LEN = 60
_MAX_KETTLEBELLS = 12
_MAX_LOAD_LBS = 300.0
_MAX_STEP_LBS = 50.0


def _err(msg: str) -> Dict[str, Any]:
    return {"ok": False, "error": msg}


def _audit(before: Optional[dict], after: Optional[dict], changed_by: str) -> None:
    from orchestrator.config_writer import log_config_change

    log_config_change("workouts", before, after, changed_by=changed_by)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def clean_name(raw: Any) -> Optional[str]:
    """Strip control chars + whitespace; None if empty or too long."""
    if not isinstance(raw, str):
        return None
    # Controls, invisible format chars (zero-width, bidi overrides) and line/
    # paragraph separators → space: a name is echoed into tool results.
    raw = "".join(" " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch for ch in raw)
    name = " ".join(raw.split())
    if not name or len(name) > _MAX_NAME_LEN:
        return None
    return name


def clean_equipment(raw: Any) -> tuple[Optional[List[str]], List[str]]:
    """Return (tokens in vocabulary order, unknown tokens). (None, []) if not a list."""
    if not isinstance(raw, list):
        return None, []
    wanted = {t.strip().lower() if isinstance(t, str) else repr(t) for t in raw}
    unknown = sorted(t[:30] for t in wanted - set(EQUIPMENT_TOKENS))[:5]
    return [t for t in EQUIPMENT_TOKENS if t in wanted], unknown


def _num(value: Any, lo: float, hi: float) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if lo <= f <= hi else None


def merge_limits(existing: Dict[str, Any], raw: Any) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Merge a limits patch onto existing limits.

    Keys: dumbbell_max_lbs, dumbbell_step_lbs, kettlebell_weights_lbs.
    A key set to null / "" / [] clears it. Unknown keys are an error.
    """
    if not isinstance(raw, dict):
        return None, "limits must be an object"
    out = dict(existing)
    for key, value in raw.items():
        if key not in ("dumbbell_max_lbs", "dumbbell_step_lbs", "kettlebell_weights_lbs"):
            return None, f"unknown limit '{str(key)[:40]}'"
        if value in (None, "", []):
            out.pop(key, None)
            continue
        if key == "dumbbell_max_lbs":
            n = _num(value, 1, _MAX_LOAD_LBS)
            if n is None:
                return None, f"dumbbell_max_lbs must be 1–{_MAX_LOAD_LBS:.0f}"
            out[key] = n
        elif key == "dumbbell_step_lbs":
            n = _num(value, 0.5, _MAX_STEP_LBS)
            if n is None:
                return None, f"dumbbell_step_lbs must be 0.5–{_MAX_STEP_LBS:.0f}"
            out[key] = n
        else:
            if not isinstance(value, list) or len(value) > _MAX_KETTLEBELLS:
                return None, f"kettlebell_weights_lbs must be a list of up to {_MAX_KETTLEBELLS} weights"
            weights = [_num(v, 1, _MAX_LOAD_LBS) for v in value]
            if any(w is None for w in weights):
                return None, f"each kettlebell weight must be 1–{_MAX_LOAD_LBS:.0f} lb"
            out[key] = sorted(set(weights))
    return out, None


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def find_location(query: str, exact: bool = False) -> tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Resolve a spoken location name.

    Exact (case/punctuation-insensitive) match first, then a unique
    containment match either way ("downtown" → "Downtown Gym", "at home" →
    "Home"). Returns (match or None, all locations).
    """
    locations = state_store.list_workout_locations()
    q = _norm(query or "")
    if not q:
        return None, locations
    for loc in locations:
        if _norm(loc["name"]) == q:
            return loc, locations
    if exact:
        return None, locations
    padded_q = f" {q} "
    # Name inside the query as whole words ("at home"), or query inside the name ("downtown").
    hits = [loc for loc in locations if (n := _norm(loc["name"])) and (f" {n} " in padded_q or q in n)]
    if len(hits) == 1:
        return hits[0], locations
    return None, locations


def add_location(
    name: Any,
    template: Any = "commercial_gym",
    equipment: Any = None,
    limits: Any = None,
    changed_by: str = "user",
) -> Dict[str, Any]:
    clean = clean_name(name)
    if not clean:
        return _err(f"name is required (max {_MAX_NAME_LEN} characters)")
    if equipment is not None:
        tokens, unknown = clean_equipment(equipment)
        if tokens is None or unknown:
            return _err(f"unknown equipment: {', '.join(unknown) or 'equipment must be a list'}")
    else:
        tmpl = str(template or "commercial_gym")
        if tmpl not in LOCATION_TEMPLATES:
            return _err(f"template must be one of: {', '.join(LOCATION_TEMPLATES)}")
        tokens = list(LOCATION_TEMPLATES[tmpl])
    merged: Dict[str, Any] = {}
    if limits is not None:
        merged, err = merge_limits({}, limits)
        if err:
            return _err(err)
    if len(state_store.list_workout_locations()) >= MAX_LOCATIONS:
        return _err(f"at most {MAX_LOCATIONS} locations")
    try:
        loc = state_store.create_workout_location(clean, tokens, merged or {})
    except sqlite3.IntegrityError:
        return _err(f"a location named '{clean}' already exists")
    _audit(None, loc, changed_by)
    logger.info("[WORKOUT] Location added: id=%d equipment=%d tokens", loc["id"], len(tokens))
    return {"ok": True, "location": loc}


def update_location(
    location_id: int,
    name: Any = None,
    equipment: Any = None,
    add_equipment: Any = None,
    remove_equipment: Any = None,
    limits: Any = None,
    changed_by: str = "user",
) -> Dict[str, Any]:
    before = state_store.get_workout_location(location_id)
    if not before:
        return _err("location not found")

    new_name = None
    if name is not None:
        new_name = clean_name(name)
        if not new_name:
            return _err(f"name must be 1–{_MAX_NAME_LEN} characters")

    new_equipment: Optional[List[str]] = None
    if equipment is not None:
        new_equipment, unknown = clean_equipment(equipment)
        if new_equipment is None or unknown:
            return _err(f"unknown equipment: {', '.join(unknown) or 'equipment must be a list'}")
    if add_equipment is not None or remove_equipment is not None:
        add, unknown_a = clean_equipment(add_equipment if add_equipment is not None else [])
        rem, unknown_r = clean_equipment(remove_equipment if remove_equipment is not None else [])
        if add is None or rem is None or unknown_a or unknown_r:
            bad = unknown_a + unknown_r
            return _err(f"unknown equipment: {', '.join(bad) or 'equipment lists must be lists'}")
        current = set(new_equipment if new_equipment is not None else before["equipment"])
        current = (current | set(add)) - set(rem)
        new_equipment = [t for t in EQUIPMENT_TOKENS if t in current]

    new_limits = None
    if limits is not None:
        new_limits, err = merge_limits(before.get("limits") or {}, limits)
        if err:
            return _err(err)

    try:
        after = state_store.update_workout_location(location_id, new_name, new_equipment, new_limits)
    except sqlite3.IntegrityError:
        return _err(f"a location named '{new_name}' already exists")
    if not after:
        return _err("location not found")
    _audit(before, after, changed_by)
    logger.info("[WORKOUT] Location updated: id=%d equipment=%d tokens", location_id, len(after["equipment"]))
    return {"ok": True, "location": after}


def remove_location(location_id: int, changed_by: str = "user") -> Dict[str, Any]:
    before = state_store.get_workout_location(location_id)
    if not before or not state_store.delete_workout_location(location_id):
        return _err("location not found")
    _audit(before, None, changed_by)
    logger.info("[WORKOUT] Location removed: id=%d", location_id)
    return {"ok": True, "removed": before["name"]}


# ---------------------------------------------------------------------------
# Program
# ---------------------------------------------------------------------------


def set_program(
    goal: Any,
    days_per_week: Any,
    session_minutes: Any,
    deload_every_weeks: Any = None,
    changed_by: str = "user",
) -> Dict[str, Any]:
    """Save the program. `deload_every_weeks`: 0 = off, 3–12; None keeps the current value.

    The deload cycle counts trained sessions in this program, so changing the
    cadence in place re-reads the position against the new cycle length (the
    next deload can move earlier or later).
    """
    goal_s = str(goal or "general").strip().lower()
    if goal_s not in GOALS:
        return _err(f"goal must be one of: {', '.join(GOALS)}")
    try:
        days = int(days_per_week)
        minutes = int(session_minutes)
    except (TypeError, ValueError, OverflowError):
        return _err("days_per_week and session_minutes must be whole numbers")
    if not (MIN_DAYS <= days <= MAX_DAYS):
        return _err(f"days_per_week must be {MIN_DAYS}–{MAX_DAYS}")
    if not (MIN_MINUTES <= minutes <= MAX_MINUTES):
        return _err(f"session_minutes must be {MIN_MINUTES}–{MAX_MINUTES}")
    before = state_store.get_active_program()
    if deload_every_weeks is None:
        deload = int(before["deload_every_weeks"]) if before else DEFAULT_DELOAD_WEEKS
    else:
        try:
            deload = int(deload_every_weeks)
        except (TypeError, ValueError, OverflowError):
            deload = -1
        if isinstance(deload_every_weeks, float) and not deload_every_weeks.is_integer():
            deload = -1
        if isinstance(deload_every_weeks, bool) or not (deload == 0 or MIN_DELOAD_WEEKS <= deload <= MAX_DELOAD_WEEKS):
            return _err(f"deload_every_weeks must be 0 (off) or {MIN_DELOAD_WEEKS}–{MAX_DELOAD_WEEKS}")
    if before and before["days_per_week"] == days:
        if before["goal"] == goal_s and before["session_minutes"] == minutes and before["deload_every_weeks"] == deload:
            return {"ok": True, "program": before, "unchanged": True}
        # Same split → update in place so the next-day position isn't reset.
        program = state_store.update_active_program(before["id"], goal_s, minutes, deload)
    else:
        program = state_store.set_active_program(goal_s, days, minutes, deload)
    _audit(before, program, changed_by)
    logger.info("[WORKOUT] Program set: %s, %d days/week, %d min, deload every %d wk", goal_s, days, minutes, deload)
    return {"ok": True, "program": program}


def clear_program(changed_by: str = "user") -> Dict[str, Any]:
    before = state_store.get_active_program()
    if not before:
        return _err("no active program")
    state_store.clear_active_program()
    _audit(before, None, changed_by)
    logger.info("[WORKOUT] Program cleared: id=%d", before["id"])
    return {"ok": True}
