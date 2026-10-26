"""
Workout manager — adaptive strength workout generation + logging.

Two ways a day gets planned:
- **Program** (when one is active — see `workout_setup.py`): a weekly split
  chosen from days/week (2 → Full A/B … 6 → PPL×2). The next day is the one
  after the last program day you actually trained, so a skipped day comes
  back. Session minutes set the exercise count, the goal sets the rep scheme.
- **No program** (the original behavior): recency-aware — full-body by
  default, complementing what was hit recently, escalating to a split only
  after 2+ sessions inside 4 days (split-as-reward).

Either way, a workout location (a gym, home) restricts the catalog to the
gear that place has: an exercise is available when its `requires` ⊆ the
location's equipment. A slot with nothing available falls back to a related
movement pattern, and loads are capped to the location's dumbbell max / owned
kettlebells. All weights are in pounds.
"""

from __future__ import annotations

import logging
import math
import random
from datetime import datetime
from typing import Any, Dict, List, Optional

from orchestrator import state_store
from orchestrator.exercises_seed import FALLBACK_VARIANTS
from orchestrator.metrics import WORKOUT_GENERATED, WORKOUT_PROGRESSION

logger = logging.getLogger(__name__)

# Muscle-group set counts from the last 3 days that count as "hit hard"
_RECENT_HIT_THRESHOLD = 6

# Lookback for "did you train recently?" decision
_RECENCY_LOOKBACK_DAYS = 4

# Rep schemes by goal: (sets, reps) for compound / isolation work.
# "general" is the original v1 default and is used when no program is set.
_REP_SCHEMES: Dict[str, Dict[str, tuple[int, int]]] = {
    "general": {"compound": (3, 8), "isolation": (3, 12)},
    "strength": {"compound": (4, 5), "isolation": (3, 10)},
    "muscle": {"compound": (3, 10), "isolation": (3, 12)},
}

# When a load cap is hit, add reps instead (up to this many).
_CAPPED_REP_BONUS = 2
_CAPPED_REP_MAX = 20

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

# When a location has nothing for a slot's pattern, try these instead (in order).
_PATTERN_FALLBACK: Dict[str, List[str]] = {
    "pull_vertical": ["pull_horizontal"],
    "pull_horizontal": ["pull_vertical"],
    "push_vertical": ["push_horizontal"],
    "push_horizontal": ["push_vertical"],
    "squat": ["lunge"],
    "lunge": ["squat"],
    "hinge": [],
    "core": ["carry"],
    "carry": ["core"],
    "isolation": [],
}

_PATTERN_LABELS = {
    "squat": "squat",
    "hinge": "hinge",
    "lunge": "lunge",
    "push_horizontal": "horizontal push",
    "push_vertical": "overhead press",
    "pull_horizontal": "row",
    "pull_vertical": "vertical pull",
    "core": "core",
    "carry": "carry",
    "isolation": "isolation",
}

_UPPER_MUSCLES = frozenset({"chest", "back", "shoulders", "biceps", "triceps", "traps", "forearms"})
_LOWER_MUSCLES = frozenset({"quads", "hamstrings", "glutes", "calves"})

# Program day templates. Slots are in priority order — a short session takes
# the first N. Isolation slots only pick exercises whose primary muscle is in
# the day's focus (None = any).
_DAY_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "full_a": {
        "label": "Full Body A",
        "workout_type": "full_body",
        "focus": None,
        "slots": [
            "squat",
            "push_horizontal",
            "pull_horizontal",
            "hinge",
            "push_vertical",
            "pull_vertical",
            "core",
            "isolation",
        ],
    },
    "full_b": {
        "label": "Full Body B",
        "workout_type": "full_body",
        "focus": None,
        "slots": [
            "hinge",
            "push_vertical",
            "pull_vertical",
            "lunge",
            "push_horizontal",
            "pull_horizontal",
            "core",
            "isolation",
        ],
    },
    "full_c": {
        "label": "Full Body C",
        "workout_type": "full_body",
        "focus": None,
        "slots": [
            "squat",
            "pull_vertical",
            "push_horizontal",
            "lunge",
            "pull_horizontal",
            "push_vertical",
            "core",
            "isolation",
        ],
    },
    "upper": {
        "label": "Upper",
        "workout_type": "upper_day",
        "focus": _UPPER_MUSCLES,
        "slots": [
            "push_horizontal",
            "pull_horizontal",
            "push_vertical",
            "pull_vertical",
            "isolation",
            "isolation",
            "isolation",
            "isolation",
        ],
    },
    "lower": {
        "label": "Lower",
        "workout_type": "lower_day",
        "focus": _LOWER_MUSCLES,
        "slots": ["squat", "hinge", "lunge", "core", "isolation", "isolation", "carry", "isolation"],
    },
    "push": {
        "label": "Push",
        "workout_type": "push_day",
        "focus": frozenset({"chest", "shoulders", "triceps"}),
        "slots": [
            "push_horizontal",
            "push_vertical",
            "push_horizontal",
            "isolation",
            "isolation",
            "isolation",
            "isolation",
            "core",
        ],
    },
    "pull": {
        "label": "Pull",
        "workout_type": "pull_day",
        "focus": frozenset({"back", "biceps", "shoulders", "traps", "forearms"}),
        "slots": [
            "pull_vertical",
            "pull_horizontal",
            "pull_horizontal",
            "isolation",
            "isolation",
            "carry",
            "isolation",
            "core",
        ],
    },
    "legs": {
        "label": "Legs",
        "workout_type": "legs_day",
        "focus": _LOWER_MUSCLES,
        "slots": ["squat", "hinge", "lunge", "isolation", "isolation", "core", "isolation", "carry"],
    },
}

# days/week -> ordered day keys
_PROGRAM_SPLITS: Dict[int, List[str]] = {
    2: ["full_a", "full_b"],
    3: ["full_a", "full_b", "full_c"],
    4: ["upper", "lower", "upper", "lower"],
    5: ["upper", "lower", "push", "pull", "legs"],
    6: ["push", "pull", "legs", "push", "pull", "legs"],
}

_MIN_EXERCISES, _MAX_EXERCISES = 3, 8
_MINUTES_PER_EXERCISE = 9


def exercises_for_minutes(minutes: int) -> int:
    """Session length -> exercise count (~9 min each incl. rest), clamped 3–8."""
    return max(_MIN_EXERCISES, min(_MAX_EXERCISES, round(minutes / _MINUTES_PER_EXERCISE)))


def program_split(days_per_week: int) -> List[str]:
    return _PROGRAM_SPLITS.get(days_per_week, _PROGRAM_SPLITS[3])


def next_program_day(program: Dict[str, Any]) -> int:
    """Index into the split of the day to train next."""
    split = program_split(program["days_per_week"])
    last = state_store.last_trained_program_day(program["id"])
    if last is None:
        return 0
    return (int(last) + 1) % len(split)


# ---------------------------------------------------------------------------
# Adaptive plan selection (no-program path)
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
            recent_muscles.get("quads", 0) + recent_muscles.get("hamstrings", 0) + recent_muscles.get("glutes", 0)
        )

        loads = {"push": chest_load, "pull": back_load, "legs": leg_load}
        focus = min(loads, key=loads.get)
        return (
            f"{focus}_day",
            f"You're {sessions_last_4d} sessions into the last 4 days — earned a split. Least-trained: {focus}.",
        )

    # 1 session in last 3 days → full-body, but skew complementary
    return (
        "full_body_complement",
        "You trained recently — full-body but skewed toward what was undertrained.",
    )


def _pick_for_slots(
    slots: List[str],
    available: Optional[set[str]] = None,
    undertrain_bias: Optional[Dict[str, int]] = None,
    focus: Optional[frozenset[str]] = None,
    sticky: Optional[List[str]] = None,
) -> tuple[List[Dict[str, Any]], List[str]]:
    """Pick one exercise per slot (a movement pattern).

    `available` restricts to a location's gear (None = everything).
    `undertrain_bias` maps muscle group -> recent set count; fresher wins.
    `focus` limits isolation slots to those primary muscles.
    `sticky` names exercises to keep when they fit a slot (last time's plan
    for this program day), so lifts repeat and can progress.
    Returns (exercises, notes) — notes explain substitutions/skips.
    """
    used_names: set[str] = set()
    notes: List[str] = []
    bias = undertrain_bias or {}
    chosen_by_slot: List[Optional[Dict[str, Any]]] = [None] * len(slots)

    def _candidates(pattern: str) -> List[Dict[str, Any]]:
        cands = [
            c
            for c in state_store.get_exercises(movement_pattern=pattern, available_equipment=available)
            if c["name"] not in used_names
        ]
        if pattern == "isolation" and focus:
            cands = [c for c in cands if c["primary_muscle"] in focus]
        preferred = [c for c in cands if c["name"] not in FALLBACK_VARIANTS]
        return preferred or cands

    def _choose(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
        for name in sticky or []:
            keep = next((c for c in candidates if c["name"] == name), None)
            if keep:
                used_names.add(keep["name"])
                return keep
        # Freshest muscles first, then avoid repeating a primary muscle already
        # in the plan; pick randomly within the best bucket for variety.
        used_primaries = [c["primary_muscle"] for c in chosen_by_slot if c]
        scores = [(bias.get(c["primary_muscle"], 0), used_primaries.count(c["primary_muscle"])) for c in candidates]
        best = min(scores)
        chosen = random.choice([c for c, sc in zip(candidates, scores, strict=True) if sc == best])
        used_names.add(chosen["name"])
        return chosen

    # Pass 1: every slot gets its own pattern first, so a fallback for an
    # early slot can't steal the only exercise a later slot needed.
    for i, pattern in enumerate(slots):
        candidates = _candidates(pattern)
        if candidates:
            chosen_by_slot[i] = _choose(candidates)

    # Pass 2: related patterns for the slots still empty.
    for i, pattern in enumerate(slots):
        if chosen_by_slot[i]:
            continue
        for alt in _PATTERN_FALLBACK.get(pattern, []):
            candidates = _candidates(alt)
            if candidates:
                chosen_by_slot[i] = _choose(candidates)
                if available is not None:
                    notes.append(
                        f"No {_PATTERN_LABELS.get(pattern, pattern)} option here — "
                        f"used a {_PATTERN_LABELS.get(alt, alt)} instead."
                    )
                break
        if not chosen_by_slot[i] and available is not None and pattern != "isolation":
            notes.append(f"Skipped {_PATTERN_LABELS.get(pattern, pattern)} — nothing for it here.")

    picked = [c for c in chosen_by_slot if c]
    return picked, notes


def _build_plan_for_type(
    workout_type: str, available: Optional[set[str]] = None
) -> tuple[List[Dict[str, Any]], List[str]]:
    """Return (ordered exercises, notes) for the chosen no-program workout type."""
    recent = state_store.get_recent_muscle_groups(days=3)

    if workout_type == "full_body":
        return _pick_for_slots(_FULL_BODY_PATTERNS, available)
    if workout_type == "full_body_complement":
        return _pick_for_slots(_FULL_BODY_PATTERNS, available, undertrain_bias=recent)
    if workout_type == "push_day":
        return _pick_for_slots(_PUSH_PATTERNS + ["isolation", "isolation"], available, undertrain_bias=recent)
    if workout_type == "pull_day":
        return _pick_for_slots(_PULL_PATTERNS + ["isolation", "isolation"], available, undertrain_bias=recent)
    if workout_type == "legs_day":
        return _pick_for_slots(_LEGS_PATTERNS + ["isolation"], available, undertrain_bias=recent)
    if workout_type == "upper_day":
        return _pick_for_slots(_UPPER_PATTERNS, available, undertrain_bias=recent)
    if workout_type == "lower_day":
        return _pick_for_slots(_LOWER_PATTERNS, available, undertrain_bias=recent)
    # Fallback
    return _pick_for_slots(_FULL_BODY_PATTERNS, available)


def _build_program_day(
    day_key: str,
    session_minutes: int,
    available: Optional[set[str]],
    sticky: Optional[List[str]] = None,
) -> tuple[List[Dict[str, Any]], List[str]]:
    template = _DAY_TEMPLATES[day_key]
    n = exercises_for_minutes(session_minutes)
    slots = list(template["slots"][:n])
    slots += ["isolation"] * (n - len(slots))
    recent = state_store.get_recent_muscle_groups(days=3)
    return _pick_for_slots(slots, available, undertrain_bias=recent, focus=template["focus"], sticky=sticky)


def _block_exercises(program: Dict[str, Any], day_index: int, location_id: Optional[int], deload: bool) -> List[str]:
    """Last time's plan for this program day, within the current training block.

    Blocks end at a deload week, so exercises rotate after each deload. During
    the deload week itself the block's lifts are kept (just lighter).
    """
    since = None if deload else state_store.last_deload_started_at(program["id"])
    return state_store.last_program_day_exercises(program["id"], day_index, location_id, since=since)


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


def _cap_load(
    exercise: Dict[str, Any], weight: Optional[float], limits: Dict[str, Any]
) -> tuple[Optional[float], bool]:
    """Fit a target weight to the location's gear. Returns (weight, capped)."""
    equipment = exercise.get("equipment")
    if equipment == "dumbbell":
        max_lbs = limits.get("dumbbell_max_lbs")
        step = limits.get("dumbbell_step_lbs")
        if weight is None:
            return None, False
        capped = False
        if max_lbs and weight > max_lbs:
            weight, capped = float(max_lbs), True
        if step:
            weight = float(max(float(step), math.floor(weight / step + 1e-9) * step))
        return weight, capped
    if equipment == "kettlebell":
        bells = sorted(limits.get("kettlebell_weights_lbs") or [])
        if not bells:
            return weight, False
        if weight is None:
            # Exactly one bell → that's the weight; otherwise let the user pick.
            return (float(bells[0]), False) if len(bells) == 1 else (None, False)
        fitting = [b for b in bells if b <= weight]
        if not fitting:
            return float(bells[0]), False
        return float(fitting[-1]), weight > bells[-1]
    return weight, False


# --- Progression -------------------------------------------------------------
#
# Deterministic progressive overload from your own logged sets:
# - Hit every planned set at the target → the weight goes up next time
#   (+10 lb lower-body barbell/machine compounds, +5 lb otherwise, the
#   location's dumbbell step for dumbbells, ~4 kg for kettlebells).
#   An extra heavy single on top of the planned sets doesn't change this.
# - Missed a set → hold the weight. Missed at the same weight twice in a row →
#   drop ~10% for that exercise (stall deload).
# - Gear can't go heavier (dumbbell max, heaviest owned bell) → +1 rep per
#   session instead. Kettlebells with a heavier bell owned move up once you're
#   4 reps over the scheme.
# - Not done in 3+ weeks → restart at ~90%.
# - A session at a place whose gear capped the lift (home dumbbells) doesn't
#   become the reference for a day somewhere with heavier gear.
# - A goal change that moves the rep range re-bases the weight (Epley).
# - Program deload week (every N trained weeks): fewer sets at ~85%.

_PROGRESSION_LOOKBACK = 8  # sessions per exercise
_STALL_DELOAD_AFTER = 2  # consecutive misses at the same weight
_STALL_DELOAD_FACTOR = 0.9
_COMEBACK_DAYS = 21
_COMEBACK_FACTOR = 0.9
_DELOAD_WEEK_LOAD = 0.85
_DELOAD_WEEK_SETS = 0.6
_KB_REPS_BEFORE_NEXT_BELL = 4
_GRINDER_RPE = 10.0
_LOWER_JUMP_EQUIPMENT = frozenset({"barbell", "machine"})
_KB_STEP_LBS = 9.0  # 4 kg — standard kettlebell jumps (16 → 20 → 24 kg)


def _round5(weight: float) -> float:
    return float(max(5, round(weight / 5) * 5))


def _increment(exercise: Dict[str, Any], limits: Dict[str, Any]) -> float:
    if exercise.get("equipment") == "dumbbell":
        return float(limits.get("dumbbell_step_lbs") or 5)
    if exercise.get("equipment") == "kettlebell":
        return _KB_STEP_LBS  # no bell list: assume the usual 4 kg jump
    if (
        exercise.get("is_compound")
        and exercise.get("primary_muscle") in _LOWER_MUSCLES
        and exercise.get("equipment") in _LOWER_JUMP_EQUIPMENT
    ):
        return 10.0
    return 5.0


def _gear_ceiling(exercise: Dict[str, Any], limits: Dict[str, Any]) -> Optional[float]:
    """Heaviest load this place's gear allows for the exercise, or None (no cap)."""
    equipment = exercise.get("equipment")
    if equipment == "dumbbell" and limits.get("dumbbell_max_lbs"):
        return float(limits["dumbbell_max_lbs"])
    bells = limits.get("kettlebell_weights_lbs")
    if equipment == "kettlebell" and isinstance(bells, list):
        weights = [float(b) for b in bells if isinstance(b, (int, float)) and not isinstance(b, bool)]
        return max(weights) if weights else None
    return None


def _session_summary(
    session: Dict[str, Any], scheme_reps: int, bodyweight: bool, rep_counting: bool = False
) -> Optional[Dict[str, Any]]:
    """Reduce one past session of an exercise to what progression needs.

    weight: the working weight (None = bodyweight). For a planned session it is
    the planned weight, or heavier if every planned set was done heavier; an
    extra top single doesn't count. outcome: "hit" (every planned set done at
    the weight and target reps), "miss", "grind" (hit at RPE 10) or "adhoc"
    (logged without a plan). Returns None for a session with nothing usable
    (a loaded lift logged only at 0 lb).
    """
    rows = session["sets"]
    planned = [r for r in rows if r.get("target_reps") is not None]
    prev_reps = max((int(r["target_reps"]) for r in planned), default=scheme_reps)
    done = [r for r in rows if r["completed"]]
    if not bodyweight:
        done = [r for r in done if float(r.get("weight_lbs") or 0) > 0]
        if not done:
            return None

    def reached(r: Dict[str, Any], target: int) -> bool:
        return int(r.get("reps") or 0) >= target

    if planned:
        needed = len(planned)
        target_w = max((float(r["target_weight_lbs"]) for r in planned if r.get("target_weight_lbs")), default=None)
        good = [r for r in done if reached(r, int(r.get("target_reps") or prev_reps))]
        if bodyweight:
            weight, hit = None, len(good) >= needed
        else:
            good_ws = sorted((float(r["weight_lbs"]) for r in good), reverse=True)
            heavy_enough = [w for w in good_ws if target_w is None or w >= target_w - 0.01]
            hit = len(heavy_enough) >= needed
            if hit:
                weight = heavy_enough[needed - 1]
            elif target_w is not None:
                weight = target_w
            else:
                all_ws = sorted((float(r["weight_lbs"]) for r in done), reverse=True)
                weight = all_ws[min(needed, len(all_ws)) - 1]
        outcome = "hit" if hit else "miss"
        hits = good
    else:
        if bodyweight:
            weight = None
            at_w = done
        else:
            enough = [float(r["weight_lbs"]) for r in done if reached(r, scheme_reps)]
            weight = max(enough) if enough else max(float(r["weight_lbs"]) for r in done)
            at_w = [r for r in done if abs(float(r["weight_lbs"]) - weight) < 0.01]
        hits = [r for r in at_w if reached(r, scheme_reps)]
        outcome = "hit" if at_w and len(hits) == len(at_w) else "adhoc"
        if outcome == "hit" and rep_counting:
            # No plan to read reps from: the reps actually done are the baseline.
            prev_reps = min(int(r.get("reps") or 0) for r in at_w)
    if outcome == "hit" and any(float(r.get("rpe") or 0) >= _GRINDER_RPE for r in hits):
        outcome = "grind"
    return {
        "weight": weight,
        "prev_reps": prev_reps,
        "outcome": outcome,
        "started_at": session["started_at"],
        "limits": session.get("location_limits") or {},
    }


def _fmt_lb(weight: float) -> str:
    return f"{weight:g} lb"


def _epley_convert(weight: float, from_reps: int, to_reps: int) -> float:
    """Same estimated 1RM, different rep count (Epley)."""
    return weight * (1 + from_reps / 30) / (1 + to_reps / 30)


def _plan_target(
    exercise: Dict[str, Any],
    goal: str = "general",
    limits: Optional[Dict[str, Any]] = None,
    *,
    deload: bool = False,
    exclude_workout_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Next target for an exercise: {sets, reps, weight, progression, note}.

    progression is one of: new, up, reps, hold, stall_deload, comeback,
    capped, deload_week. `note` is a short human line (None for "new").
    """
    limits = limits or {}
    now = now or datetime.now()
    scheme = _REP_SCHEMES.get(goal, _REP_SCHEMES["general"])
    sets, scheme_reps = scheme["compound" if exercise.get("is_compound") else "isolation"]
    bodyweight = exercise.get("equipment") == "bodyweight"
    rep_counting = bodyweight or exercise.get("equipment") == "kettlebell"
    history = [
        h
        for s in state_store.get_exercise_sessions(
            exercise["name"], exclude_workout_id=exclude_workout_id, limit=_PROGRESSION_LOOKBACK
        )
        if (h := _session_summary(s, scheme_reps, bodyweight, rep_counting)) is not None
    ]
    bells = sorted(float(b) for b in limits.get("kettlebell_weights_lbs") or [])
    owned_bells = exercise.get("equipment") == "kettlebell" and bool(bells)
    today_ceiling = _gear_ceiling(exercise, limits)

    def elsewhere_capped(h: Dict[str, Any]) -> bool:
        # Done at the max of a place whose gear tops out below today's: it says
        # nothing about what you can do here (home dumbbells vs the gym rack).
        ceiling = _gear_ceiling(exercise, h["limits"])
        return (
            ceiling is not None
            and h["weight"] is not None
            and h["weight"] >= ceiling - 0.01
            and (today_ceiling is None or today_ceiling > ceiling + 0.01)
        )

    relevant = [h for h in history if not elsewhere_capped(h)]
    ref = relevant[0] if relevant else (history[0] if history else None)
    # Rep-count progression (bodyweight, owned bells, maxed-out gear) carries
    # last time's reps forward; loaded lifts otherwise use the goal's scheme.
    rep_mode = ref is not None and (
        bodyweight
        or owned_bells
        or (today_ceiling is not None and ref["weight"] is not None and ref["weight"] >= today_ceiling - 0.01)
    )

    reps = scheme_reps
    weight: Optional[float] = None
    progression, note = "new", None

    if ref is not None:
        last_done = state_store.last_exercise_completed_at(exercise["name"], exclude_workout_id=exclude_workout_id)
        try:
            days_off = (now - datetime.fromisoformat(last_done or ref["started_at"])).days
        except ValueError:
            days_off = 0
        w = ref["weight"]
        prev_reps = ref["prev_reps"]
        if days_off >= _COMEBACK_DAYS:
            progression = "comeback"
            weight = _round5(w * _COMEBACK_FACTOR) if w else None
            note = f"first time in {days_off} days — easing back in"
        elif w is not None and not rep_mode and abs(prev_reps - scheme_reps) > 1:
            # Goal changed the rep range: re-base instead of progressing.
            progression = "hold"
            if scheme_reps > prev_reps:
                weight = _round5(_epley_convert(w, prev_reps, scheme_reps))
                note = "new rep range — weight adjusted to match"
            else:
                weight = w
                note = "new rep range — same weight, fewer reps"
        elif ref["outcome"] in ("miss", "adhoc", "grind"):
            streak = 0
            for h in relevant:
                if h["weight"] != w or h["outcome"] != "miss":
                    break
                streak += 1
            if streak >= _STALL_DELOAD_AFTER and w:
                progression = "stall_deload"
                weight = _round5(w * _STALL_DELOAD_FACTOR)
                note = f"stalled at {_fmt_lb(w)} twice — dropping ~10% to build back up"
            else:
                progression = "hold"
                weight = w
                if rep_mode:
                    reps = max(scheme_reps, prev_reps)
                note = {
                    "miss": "same as last time — finish every set to move up",
                    "grind": "holding — last time was a grinder",
                    "adhoc": None,
                }[ref["outcome"]]
        elif w is None:
            # Bodyweight: progress reps.
            reps = min(_CAPPED_REP_MAX, max(scheme_reps, prev_reps) + 1)
            progression = "reps"
            note = (
                f"at {_CAPPED_REP_MAX} reps — try a harder variation"
                if reps >= _CAPPED_REP_MAX
                else "+1 rep from last time"
            )
        elif owned_bells:
            heavier = [b for b in bells if b > w + 0.01]
            if heavier and prev_reps >= scheme_reps + _KB_REPS_BEFORE_NEXT_BELL:
                weight, progression = heavier[0], "up"
                note = f"moving up to the {_fmt_lb(heavier[0])} bell"
            else:
                weight, progression = w, "reps"
                reps = min(_CAPPED_REP_MAX, max(scheme_reps, prev_reps) + 1)
                note = "+1 rep from last time"
        else:
            weight, progression = w + _increment(exercise, limits), "up"

    if weight is None and progression in ("new", "comeback") and owned_bells and len(bells) == 1:
        # Nothing to go on but the gear: a single owned bell is the obvious pick.
        weight = bells[0]

    fitted, capped = _cap_load(exercise, weight, limits)
    if capped:
        # Today's gear can't reach the plan. Progress reps against the last
        # session done at this same ceiling, if there is one.
        local = next((h for h in history if h["weight"] is not None and abs(h["weight"] - fitted) < 0.01), None)
        if local and local["outcome"] == "hit":
            reps = min(_CAPPED_REP_MAX, max(scheme_reps, local["prev_reps"]) + 1)
            note = "maxed out on this gear — +1 rep from last time"
        elif local:
            reps = min(_CAPPED_REP_MAX, max(scheme_reps, local["prev_reps"]))
            note = "maxed out on this gear — same reps as last time"
        else:
            reps = min(_CAPPED_REP_MAX, scheme_reps + _CAPPED_REP_BONUS)
            note = "maxed out on this gear — go for extra reps"
        progression = "capped"
    elif progression == "up" and ref is not None and ref["weight"] is not None and fitted is not None:
        if fitted <= ref["weight"] + 0.01:
            # The jump didn't fit the gear's step: add a rep instead.
            progression = "reps"
            reps = min(_CAPPED_REP_MAX, max(scheme_reps, ref["prev_reps"]) + 1)
            note = "+1 rep from last time"
        elif note is None:
            note = f"+{fitted - ref['weight']:g} lb from last time"
    weight = fitted

    if deload:
        sets = max(2, round(sets * _DELOAD_WEEK_SETS))
        reps = scheme_reps
        base = ref["weight"] if ref is not None and ref["weight"] else None
        weight, _ = _cap_load(exercise, _round5(base * _DELOAD_WEEK_LOAD) if base else weight, limits)
        progression = "deload_week"
        note = "deload week — lighter on purpose"

    return {"sets": sets, "reps": reps, "weight": weight, "progression": progression, "note": note}


def _add_exercise_sets(
    workout_id: int,
    ex: Dict[str, Any],
    goal: str,
    limits: Dict[str, Any],
    deload: bool = False,
) -> Dict[str, Any]:
    """Persist planned sets for one exercise and return its plan entry."""
    target = _plan_target(ex, goal, limits, deload=deload, exclude_workout_id=workout_id)
    WORKOUT_PROGRESSION.labels(progression=target["progression"]).inc()
    if target["progression"] in ("stall_deload", "comeback"):
        # log-safe: exercise name from the built-in catalogue
        logger.info("[WORKOUT] %s: %s → %s lb", ex["name"], target["progression"], target["weight"])
    muscle_groups = [ex["primary_muscle"]] + ex.get("secondary_muscles", [])
    sets_planned = []
    for i in range(1, target["sets"] + 1):
        state_store.add_planned_set(
            workout_id=workout_id,
            exercise_name=ex["name"],
            muscle_groups=muscle_groups,
            set_number=i,
            target_reps=target["reps"],
            target_weight_lbs=target["weight"],
            # The "why" rides on set 1 so the dashboard can show it.
            notes=target["note"] if i == 1 else None,
        )
        sets_planned.append({"set_number": i, "target_reps": target["reps"], "target_weight_lbs": target["weight"]})
    entry = {
        "name": ex["name"],
        "primary_muscle": ex["primary_muscle"],
        "muscle_groups": muscle_groups,
        "equipment": ex.get("equipment"),
        "is_compound": bool(ex.get("is_compound", True)),
        "sets": sets_planned,
        "progression": target["progression"],
    }
    if target["note"]:
        entry["note"] = target["note"]
    return entry


def deload_status(program: Dict[str, Any]) -> Dict[str, Any]:
    """Where the program is in its deload cycle.

    Counts trained sessions (>=1 completed set). After `deload_every_weeks`
    weeks' worth of sessions, the next week's worth is a deload week.
    Returns {enabled, is_deload, sessions_until_deload}.
    """
    every = int(program.get("deload_every_weeks") or 0)
    days = int(program["days_per_week"])
    if every <= 0:
        return {"enabled": False, "is_deload": False, "sessions_until_deload": None}
    pos = state_store.count_trained_program_sessions(program["id"]) % (days * (every + 1))
    is_deload = pos >= days * every
    return {
        "enabled": True,
        "is_deload": is_deload,
        "sessions_until_deload": 0 if is_deload else days * every - pos,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_program_overview() -> Dict[str, Any]:
    """Active program + its split + the next day, and the known locations."""
    program = state_store.get_active_program()
    locations = state_store.list_workout_locations()
    out: Dict[str, Any] = {"program": None, "locations": locations}
    if program:
        split = program_split(program["days_per_week"])
        nxt = next_program_day(program)
        out["program"] = {
            **program,
            "split": [_DAY_TEMPLATES[k]["label"] for k in split],
            "next_day_index": nxt,
            "next_day_label": _DAY_TEMPLATES[split[nxt]]["label"],
            "exercises_per_session": exercises_for_minutes(program["session_minutes"]),
            "deload": deload_status(program),
        }
    return out


def generate_workout(location_id: Optional[int] = None) -> Dict[str, Any]:
    """Generate today's workout, persist it, and return the full plan.

    `location_id` restricts exercises + loads to that location's gear
    (None = whole catalog, no load caps).

    Returns a dict with: workout_id, workout_type, reasoning, exercises list,
    location ({id, name} or None), program_day ({index, of, label} or None),
    notes. Each exercise has: name, primary_muscle, muscle_groups, sets (list
    of {set_number, target_reps, target_weight_lbs}), optional note.
    """
    location = None
    if location_id is not None:
        location = state_store.get_workout_location(location_id)
        if not location:
            WORKOUT_GENERATED.labels(result="bad_location").inc()
            return {"ok": False, "error": f"Workout location {location_id} not found."}
    available = set(location["equipment"]) if location else None
    limits = (location or {}).get("limits") or {}

    program = state_store.get_active_program()
    program_day = None
    goal = "general"
    deload = False
    if program:
        goal = program["goal"]
        split = program_split(program["days_per_week"])
        idx = next_program_day(program)
        template = _DAY_TEMPLATES[split[idx]]
        workout_type = template["workout_type"]
        deload = deload_status(program)["is_deload"]
        sticky = _block_exercises(program, idx, location["id"] if location else None, deload)
        exercises, notes = _build_program_day(split[idx], program["session_minutes"], available, sticky)
        program_day = {"index": idx, "of": len(split), "label": template["label"]}
        reasoning = (
            f"Program day {idx + 1} of {len(split)}: {template['label']} ({goal}, {program['session_minutes']} min)."
        )
        if deload:
            reasoning += " Deload week: fewer sets, lighter weights, on purpose."
        elif sticky and any(e["name"] in sticky for e in exercises):
            reasoning += " Same lifts as last time so you can progress" + (
                "; they rotate after the next deload week." if program.get("deload_every_weeks") else "."
            )
    else:
        workout_type, reasoning = _decide_workout_type()
        exercises, notes = _build_plan_for_type(workout_type, available)

    if location:
        reasoning += f" At {location['name']}."
    if notes:
        reasoning += " " + " ".join(notes)

    if not exercises:
        logger.warning("[WORKOUT] Generator returned no exercises (location=%s)", location["id"] if location else None)
        WORKOUT_GENERATED.labels(result="no_exercises").inc()
        if location:
            return {
                "ok": False,
                "error": f"Nothing in the exercise catalog fits the equipment at {location['name']}. "
                "Add equipment to that location first.",
            }
        return {
            "ok": False,
            "error": "No exercises available in catalog. Seed the exercises table first.",
        }

    workout_id = state_store.create_workout(
        workout_type=workout_type,
        generated_by_jess=True,
        reasoning=reasoning,
    )
    state_store.set_workout_context(
        workout_id,
        location["id"] if location else None,
        program["id"] if program else None,
        program_day["index"] if program_day else None,
        is_deload=deload,
    )
    if location:
        state_store.touch_workout_location(location["id"])

    plan = [_add_exercise_sets(workout_id, ex, goal, limits, deload=deload) for ex in exercises]
    WORKOUT_GENERATED.labels(result="ok").inc()

    # log-safe: program and location are logged by numeric id only
    logger.info(
        "[WORKOUT] Generated %s (id=%d, %d exercises, program=%s, location=%s, deload=%s, %d notes)",
        workout_type,
        workout_id,
        len(plan),
        program["id"] if program else None,
        location["id"] if location else None,
        deload,
        len(notes),
    )
    return {
        "ok": True,
        "workout_id": workout_id,
        "workout_type": workout_type,
        "reasoning": reasoning,
        "exercises": plan,
        "location": {"id": location["id"], "name": location["name"]} if location else None,
        "program_day": program_day,
        "deload": deload,
        "notes": notes,
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
    location = state_store.get_workout_location(workout["location_id"]) if workout.get("location_id") else None
    return {
        "has_workout": True,
        "workout_id": workout["id"],
        "workout_type": workout["workout_type"],
        "location_id": location["id"] if location else None,
        "location": location["name"] if location else None,
        "is_deload": bool(workout.get("is_deload")),
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
                "note": None,
                "sets": [],
            }
        grouped[name]["sets"].append(s)
        if s.get("notes") and not grouped[name]["note"]:
            grouped[name]["note"] = s["notes"]
    return [grouped[n] for n in order]


def get_history(days: int = 7) -> List[Dict[str, Any]]:
    """Return recent workouts with a compact summary per session."""
    workouts = state_store.get_recent_workouts(days=days)
    summaries = []
    for w in workouts:
        sets = w.get("sets", [])
        completed = [s for s in sets if s.get("completed")]
        total_volume = sum(float(s.get("weight_lbs") or 0) * int(s.get("reps") or 0) for s in completed)
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

    Removes only *planned* (not completed) sets. Added exercises get the active
    program's rep scheme and the workout location's load caps. An exercise the
    location can't support (or that isn't in the catalog) is skipped with a
    reason and up to 2 alternatives for the same movement pattern. If adds were
    requested and none fit, the removals are held too (a failed swap must not
    just delete the original).
    """
    workout = state_store.get_workout(workout_id)
    if not workout:
        return {"ok": False, "error": f"Workout {workout_id} not found."}

    location = state_store.get_workout_location(workout["location_id"]) if workout.get("location_id") else None
    available = set(location["equipment"]) if location else None
    limits = (location or {}).get("limits") or {}
    program = state_store.get_active_program()
    goal = program["goal"] if program and program["id"] == workout.get("program_id") else "general"

    # Validate adds BEFORE removing anything: a swap whose replacement can't
    # be done here must not leave a hole in the plan.
    to_add: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    removing = set(remove_exercises or [])
    planned = {s["exercise_name"] for s in workout.get("sets", [])} - removing
    for name in add_exercises or []:
        ex = state_store.get_exercise(name)
        if not ex:
            skipped.append({"name": name, "reason": "not in the exercise catalog"})
            continue
        if ex["name"] in planned or any(a["name"] == ex["name"] for a in to_add):
            skipped.append({"name": ex["name"], "reason": "already in today's plan"})
            continue
        if available is not None and not set(ex["requires"]) <= available:
            missing = sorted(set(ex["requires"]) - available)
            alternatives = [
                c["name"]
                for c in state_store.get_exercises(
                    movement_pattern=ex["movement_pattern"], available_equipment=available
                )
                if c["name"] != ex["name"]
            ]
            skipped.append(
                {
                    "name": ex["name"],
                    "reason": f"needs {', '.join(missing)}, which {location['name']} doesn't have",
                    "alternatives": random.sample(alternatives, min(2, len(alternatives))),
                }
            )
            continue
        to_add.append(ex)

    removed_count = 0
    removal_held = bool(add_exercises) and not to_add and bool(remove_exercises)
    if remove_exercises and not removal_held:
        for s in workout.get("sets", []):
            if (
                s["exercise_name"] in remove_exercises
                and not s.get("completed")
                and state_store.delete_workout_set(s["id"])
            ):
                removed_count += 1

    added: List[str] = []
    for ex in to_add:
        _add_exercise_sets(workout_id, ex, goal, limits, deload=bool(workout.get("is_deload")))
        added.append(ex["name"])

    return {
        "ok": True,
        "workout_id": workout_id,
        "removed_sets": removed_count,
        "added_exercises": added,
        "skipped_exercises": skipped,
        # True when nothing was removed because none of the replacements fit.
        "removal_held": removal_held,
    }
