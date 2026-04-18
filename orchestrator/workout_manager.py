"""
Workout manager — adaptive strength workout generation + logging.

The generator is recency-aware. It looks at the last 7 days of completed sets and:
- returns full-body by default (most common pattern for inconsistent attendance)
- rotates the focus to complement what's been hit hard recently
- escalates to an actual split only after 2+ sessions inside 4 days (split-as-reward)

All weights are in pounds. No macro targets, no progression schemes — just a
sensible structure that adapts to whether you actually showed up this week.
"""

from __future__ import annotations

import logging
import random
from typing import Any, Dict, List, Optional

from orchestrator import state_store

logger = logging.getLogger(__name__)

# Muscle-group set counts from the last 3 days that count as "hit hard"
_RECENT_HIT_THRESHOLD = 6

# Lookback for "did you train recently?" decision
_RECENCY_LOOKBACK_DAYS = 4

# Default rep schemes by goal — keep simple for v1
_DEFAULT_SETS = 3
_DEFAULT_REPS_COMPOUND = 8
_DEFAULT_REPS_ISOLATION = 12

# Movement patterns that make up a full-body day
_FULL_BODY_PATTERNS = [
    "squat",
    "hinge",
    "push_horizontal",
    "pull_horizontal",
    "push_vertical",
    "pull_vertical",
    "core",
]

# "Upper"/"lower" split contents
_UPPER_PATTERNS = [
    "push_horizontal",
    "pull_horizontal",
    "push_vertical",
    "pull_vertical",
    "isolation",
]
_LOWER_PATTERNS = ["squat", "hinge", "lunge", "core"]

# Push / pull / legs split
_PUSH_PATTERNS = ["push_horizontal", "push_vertical"]
_PULL_PATTERNS = ["pull_horizontal", "pull_vertical"]
_LEGS_PATTERNS = ["squat", "hinge", "lunge"]


# ---------------------------------------------------------------------------
# Adaptive plan selection
# ---------------------------------------------------------------------------


def _decide_workout_type() -> tuple[str, str]:
    """Decide what type of workout to generate today.

    Returns (workout_type, reasoning_line).
    """
    sessions_last_4d = state_store.count_workouts_in_window(_RECENCY_LOOKBACK_DAYS)
    days_since = state_store.days_since_last_workout()
    recent_muscles = state_store.get_recent_muscle_groups(days=3)

    # No history or long gap → full-body, no complement logic needed
    if days_since is None or days_since >= 4:
        return (
            "full_body",
            "No training in the last 4+ days — starting fresh with full-body.",
        )

    # Consistent week (2+ in last 4 days) → reward with a split
    if sessions_last_4d >= 2:
        # Decide which split — complement what's fresh, train what's not
        back_load = recent_muscles.get("back", 0) + recent_muscles.get("biceps", 0)
        chest_load = recent_muscles.get("chest", 0) + recent_muscles.get("triceps", 0)
        leg_load = (
            recent_muscles.get("quads", 0)
            + recent_muscles.get("hamstrings", 0)
            + recent_muscles.get("glutes", 0)
        )

        loads = {"push": chest_load, "pull": back_load, "legs": leg_load}
        focus = min(loads, key=loads.get)
        return (
            f"{focus}_day",
            f"You're {sessions_last_4d} sessions into the last 4 days — earned a split. "
            f"Least-trained: {focus}.",
        )

    # 1 session in last 3 days → full-body, but skew complementary
    return (
        "full_body_complement",
        "You trained recently — full-body but skewed toward what was undertrained.",
    )


def _pick_exercises_for_patterns(
    patterns: List[str],
    undertrain_bias: Optional[Dict[str, int]] = None,
    max_per_pattern: int = 1,
) -> List[Dict[str, Any]]:
    """Pick one exercise per movement pattern.

    `undertrain_bias` maps muscle group -> recent set count; lower counts win ties.
    """
    picked: List[Dict[str, Any]] = []
    used_names: set[str] = set()

    for pattern in patterns:
        candidates = state_store.get_exercises(movement_pattern=pattern)
        if not candidates:
            continue
        # Filter out ones already picked (avoid duplicates)
        candidates = [c for c in candidates if c["name"] not in used_names]
        if not candidates:
            continue

        if undertrain_bias:
            # Score each candidate by how fresh its primary muscle is
            def _score(ex: Dict[str, Any]) -> int:
                return undertrain_bias.get(ex["primary_muscle"], 0)

            candidates.sort(key=_score)
            # Keep the lowest-score bucket and pick randomly within it for variety
            min_score = _score(candidates[0])
            freshest = [c for c in candidates if _score(c) == min_score]
            chosen = random.choice(freshest)
        else:
            chosen = random.choice(candidates)

        picked.append(chosen)
        used_names.add(chosen["name"])
        if len(picked) >= len(patterns) * max_per_pattern:
            break

    return picked


def _build_plan_for_type(workout_type: str) -> List[Dict[str, Any]]:
    """Return an ordered list of exercise dicts for the chosen workout type."""
    recent = state_store.get_recent_muscle_groups(days=3)

    if workout_type == "full_body":
        patterns = _FULL_BODY_PATTERNS
        return _pick_exercises_for_patterns(patterns)

    if workout_type == "full_body_complement":
        patterns = _FULL_BODY_PATTERNS
        return _pick_exercises_for_patterns(patterns, undertrain_bias=recent)

    if workout_type == "push_day":
        patterns = _PUSH_PATTERNS + ["isolation", "isolation"]
        return _pick_exercises_for_patterns(patterns, undertrain_bias=recent)

    if workout_type == "pull_day":
        patterns = _PULL_PATTERNS + ["isolation", "isolation"]
        return _pick_exercises_for_patterns(patterns, undertrain_bias=recent)

    if workout_type == "legs_day":
        patterns = _LEGS_PATTERNS + ["isolation"]
        return _pick_exercises_for_patterns(patterns, undertrain_bias=recent)

    if workout_type == "upper_day":
        return _pick_exercises_for_patterns(_UPPER_PATTERNS, undertrain_bias=recent)

    if workout_type == "lower_day":
        return _pick_exercises_for_patterns(_LOWER_PATTERNS, undertrain_bias=recent)

    # Fallback
    return _pick_exercises_for_patterns(_FULL_BODY_PATTERNS)


def _suggested_target(exercise: Dict[str, Any]) -> tuple[Optional[int], Optional[float]]:
    """Suggest target reps and weight based on PR history.

    v1: if we have a PR, drop 10% from best weight for a working set.
    Otherwise return (reps_target, None) so the user fills in the weight.
    """
    reps = _DEFAULT_REPS_COMPOUND if exercise.get("is_compound") else _DEFAULT_REPS_ISOLATION
    pr = state_store.get_exercise_prs(exercise["name"])
    if pr and pr.get("weight_lbs"):
        target = round(float(pr["weight_lbs"]) * 0.9 / 5) * 5  # round to nearest 5 lb
        return reps, float(target)
    return reps, None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_workout() -> Dict[str, Any]:
    """Generate today's workout, persist it, and return the full plan.

    Returns a dict with: workout_id, workout_type, reasoning, exercises list.
    Each exercise has: name, primary_muscle, muscle_groups, sets (list of
    {set_number, target_reps, target_weight_lbs}).
    """
    workout_type, reasoning = _decide_workout_type()
    exercises = _build_plan_for_type(workout_type)

    if not exercises:
        logger.warning("[WORKOUT] Generator returned no exercises — catalog empty?")
        return {
            "ok": False,
            "error": "No exercises available in catalog. Seed the exercises table first.",
        }

    workout_id = state_store.create_workout(
        workout_type=workout_type,
        generated_by_jess=True,
        reasoning=reasoning,
    )

    plan: List[Dict[str, Any]] = []
    for ex in exercises:
        reps, weight = _suggested_target(ex)
        muscle_groups = [ex["primary_muscle"]] + ex.get("secondary_muscles", [])
        sets_planned = []
        for i in range(1, _DEFAULT_SETS + 1):
            state_store.add_planned_set(
                workout_id=workout_id,
                exercise_name=ex["name"],
                muscle_groups=muscle_groups,
                set_number=i,
                target_reps=reps,
                target_weight_lbs=weight,
            )
            sets_planned.append(
                {"set_number": i, "target_reps": reps, "target_weight_lbs": weight}
            )
        plan.append(
            {
                "name": ex["name"],
                "primary_muscle": ex["primary_muscle"],
                "muscle_groups": muscle_groups,
                "equipment": ex.get("equipment"),
                "is_compound": bool(ex.get("is_compound", True)),
                "sets": sets_planned,
            }
        )

    logger.info(
        "[WORKOUT] Generated %s (id=%d, %d exercises): %s",
        workout_type,
        workout_id,
        len(plan),
        reasoning,
    )
    return {
        "ok": True,
        "workout_id": workout_id,
        "workout_type": workout_type,
        "reasoning": reasoning,
        "exercises": plan,
    }


def log_set(
    exercise_name: str,
    weight_lbs: float,
    reps: int,
    rpe: Optional[float] = None,
    workout_id: Optional[int] = None,
    set_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Log a completed set. Creates a workout row if none exists today."""
    if workout_id is None:
        today = state_store.get_todays_workout()
        if today:
            workout_id = today["id"]
        else:
            workout_id = state_store.create_workout(
                workout_type="custom",
                generated_by_jess=False,
                reasoning="Ad-hoc workout started by logging a set.",
            )

    row = state_store.log_completed_set(
        workout_id=workout_id,
        exercise_name=exercise_name,
        weight_lbs=float(weight_lbs),
        reps=int(reps),
        rpe=float(rpe) if rpe is not None else None,
        set_id=set_id,
    )
    logger.info(
        "[WORKOUT] Logged set: %s %.1f lb x %d (workout=%d)",
        exercise_name,
        float(weight_lbs),
        int(reps),
        workout_id,
    )
    # Bridge: lifting a set is obvious movement — reset the sitting/movement
    # nudge gate so selfcare doesn't fire "you've been sitting for 274 min"
    # while the user is actively at the gym.
    try:
        from orchestrator.selfcare_manager import record_movement_logged

        record_movement_logged(f"set:{exercise_name}")
    except Exception as e:
        # ERROR not warning — consistent with the other state bridges; if
        # this fires the selfcare API surface has drifted.
        logger.error(f"[WORKOUT] Selfcare movement bridge failed: {e}", exc_info=True)
    return {"ok": True, "workout_id": workout_id, "set": row}


def get_status() -> Dict[str, Any]:
    """Return today's workout plan + completion progress."""
    workout = state_store.get_todays_workout()
    if not workout:
        return {
            "has_workout": False,
            "message": "No workout today. Ask Jess to generate one.",
        }
    sets = workout.get("sets", [])
    completed = sum(1 for s in sets if s.get("completed"))
    return {
        "has_workout": True,
        "workout_id": workout["id"],
        "workout_type": workout["workout_type"],
        "reasoning": workout.get("reasoning"),
        "started_at": workout["started_at"],
        "ended_at": workout.get("ended_at"),
        "total_sets": len(sets),
        "completed_sets": completed,
        "exercises": _group_sets_by_exercise(sets),
    }


def _group_sets_by_exercise(sets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group a flat list of sets into one entry per exercise, preserving order."""
    order: List[str] = []
    grouped: Dict[str, Dict[str, Any]] = {}
    for s in sets:
        name = s["exercise_name"]
        if name not in grouped:
            order.append(name)
            grouped[name] = {
                "name": name,
                "muscle_groups": s.get("muscle_groups", []),
                "sets": [],
            }
        grouped[name]["sets"].append(s)
    return [grouped[n] for n in order]


def get_history(days: int = 7) -> List[Dict[str, Any]]:
    """Return recent workouts with a compact summary per session."""
    workouts = state_store.get_recent_workouts(days=days)
    summaries = []
    for w in workouts:
        sets = w.get("sets", [])
        completed = [s for s in sets if s.get("completed")]
        total_volume = sum(
            float(s.get("weight_lbs") or 0) * int(s.get("reps") or 0) for s in completed
        )
        summaries.append(
            {
                "id": w["id"],
                "workout_type": w["workout_type"],
                "started_at": w["started_at"],
                "ended_at": w.get("ended_at"),
                "set_count": len(sets),
                "completed_set_count": len(completed),
                "total_volume_lbs": round(total_volume, 1),
                "reasoning": w.get("reasoning"),
                "exercises": sorted({s["exercise_name"] for s in sets}),
            }
        )
    return summaries


def modify_workout(
    workout_id: int,
    remove_exercises: Optional[List[str]] = None,
    add_exercises: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Swap/add/remove exercises on an existing workout plan.

    Removes only *planned* (not completed) sets. Adds default 3 sets per new
    exercise with the same target suggestion logic.
    """
    workout = state_store.get_workout(workout_id)
    if not workout:
        return {"ok": False, "error": f"Workout {workout_id} not found."}

    removed_count = 0
    if remove_exercises:
        for s in workout.get("sets", []):
            if s["exercise_name"] in remove_exercises and not s.get("completed"):
                if state_store.delete_workout_set(s["id"]):
                    removed_count += 1

    added: List[str] = []
    if add_exercises:
        for name in add_exercises:
            ex = state_store.get_exercise(name)
            if not ex:
                continue
            reps, weight = _suggested_target(ex)
            muscle_groups = [ex["primary_muscle"]] + ex.get("secondary_muscles", [])
            for i in range(1, _DEFAULT_SETS + 1):
                state_store.add_planned_set(
                    workout_id=workout_id,
                    exercise_name=ex["name"],
                    muscle_groups=muscle_groups,
                    set_number=i,
                    target_reps=reps,
                    target_weight_lbs=weight,
                )
            added.append(ex["name"])

    return {
        "ok": True,
        "workout_id": workout_id,
        "removed_sets": removed_count,
        "added_exercises": added,
    }
