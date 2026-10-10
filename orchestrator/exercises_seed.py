"""
Seed catalog of ~70 common strength exercises (gym + kettlebell/dumbbell home work).

Muscle groups are normalized to: chest, back, shoulders, biceps, triceps,
quads, hamstrings, glutes, calves, core, traps, forearms.

Movement patterns: squat, hinge, push_horizontal, push_vertical,
pull_horizontal, pull_vertical, lunge, carry, core, isolation.

`requires` lists the EQUIPMENT_TOKENS an exercise needs. Most entries omit it
and get it derived from `equipment` (see `exercise_requires`); it's spelled out
only where the gear is more than the implement itself (a bench, a rack, a bar).
An exercise is available at a workout location when its requires ⊆ the
location's equipment.
"""

from typing import Any, Dict, List

# Fixed vocabulary for workout-location equipment. Order is display order.
EQUIPMENT_TOKENS: tuple[str, ...] = (
    "barbell",
    "squat_rack",
    "bench",
    "trap_bar",
    "dumbbells",
    "kettlebell",
    "cable",
    "machines",
    "leg_press",
    "pull_up_bar",
    "dip_station",
    "ab_wheel",
)

# Starting points for a new location; the user edits the differences.
LOCATION_TEMPLATES: Dict[str, List[str]] = {
    "commercial_gym": list(EQUIPMENT_TOKENS),
    "home_basic": ["dumbbells", "kettlebell"],
    "empty": [],
}

# Floor substitutes for bench/bar movements. The planner only picks these
# when nothing else fits the slot, so a full gym never gets "floor pullover".
FALLBACK_VARIANTS: frozenset[str] = frozenset(
    {
        "Dumbbell Floor Press",
        "Kettlebell Floor Press",
        "Dumbbell Floor Pullover",
        "Dumbbell Floor Skull Crusher",
    }
)

_EQUIPMENT_TO_TOKENS: Dict[str, List[str]] = {
    "barbell": ["barbell"],
    "dumbbell": ["dumbbells"],
    "kettlebell": ["kettlebell"],
    "cable": ["cable"],
    "machine": ["machines"],
    "bodyweight": [],
}


def exercise_requires(ex: Dict[str, Any]) -> List[str]:
    """Equipment tokens an exercise needs: explicit `requires`, else derived from `equipment`."""
    if "requires" in ex and ex["requires"] is not None:
        return list(ex["requires"])
    return list(_EQUIPMENT_TO_TOKENS.get(ex.get("equipment", ""), [ex.get("equipment", "")]))


EXERCISES: List[Dict[str, Any]] = [
    # --- Squat / quad-dominant ---
    {
        "name": "Back Squat",
        "primary_muscle": "quads",
        "secondary_muscles": ["glutes", "core", "hamstrings"],
        "equipment": "barbell",
        "requires": ["barbell", "squat_rack"],
        "movement_pattern": "squat",
    },
    {
        "name": "Front Squat",
        "primary_muscle": "quads",
        "secondary_muscles": ["glutes", "core"],
        "equipment": "barbell",
        "requires": ["barbell", "squat_rack"],
        "movement_pattern": "squat",
    },
    {
        "name": "Goblet Squat",
        "primary_muscle": "quads",
        "secondary_muscles": ["glutes", "core"],
        "equipment": "dumbbell",
        "movement_pattern": "squat",
    },
    {
        "name": "Leg Press",
        "primary_muscle": "quads",
        "secondary_muscles": ["glutes", "hamstrings"],
        "equipment": "machine",
        "requires": ["leg_press"],
        "movement_pattern": "squat",
    },
    {
        "name": "Bulgarian Split Squat",
        "primary_muscle": "quads",
        "secondary_muscles": ["glutes", "hamstrings"],
        "equipment": "dumbbell",
        "movement_pattern": "lunge",
    },
    {
        "name": "Walking Lunge",
        "primary_muscle": "quads",
        "secondary_muscles": ["glutes", "hamstrings"],
        "equipment": "dumbbell",
        "movement_pattern": "lunge",
    },
    {
        "name": "Leg Extension",
        "primary_muscle": "quads",
        "secondary_muscles": [],
        "equipment": "machine",
        "is_compound": False,
        "movement_pattern": "isolation",
    },
    # --- Hinge / posterior chain ---
    {
        "name": "Deadlift",
        "primary_muscle": "hamstrings",
        "secondary_muscles": ["glutes", "back", "core", "traps"],
        "equipment": "barbell",
        "movement_pattern": "hinge",
    },
    {
        "name": "Romanian Deadlift",
        "primary_muscle": "hamstrings",
        "secondary_muscles": ["glutes", "back"],
        "equipment": "barbell",
        "movement_pattern": "hinge",
    },
    {
        "name": "Dumbbell RDL",
        "primary_muscle": "hamstrings",
        "secondary_muscles": ["glutes", "back"],
        "equipment": "dumbbell",
        "movement_pattern": "hinge",
    },
    {
        "name": "Trap Bar Deadlift",
        "primary_muscle": "quads",
        "secondary_muscles": ["glutes", "hamstrings", "back", "traps"],
        "equipment": "barbell",
        "requires": ["trap_bar"],
        "movement_pattern": "hinge",
    },
    {
        "name": "Hip Thrust",
        "primary_muscle": "glutes",
        "secondary_muscles": ["hamstrings", "core"],
        "equipment": "barbell",
        "requires": ["barbell", "bench"],
        "movement_pattern": "hinge",
    },
    {
        "name": "Kettlebell Swing",
        "primary_muscle": "glutes",
        "secondary_muscles": ["hamstrings", "core", "back"],
        "equipment": "kettlebell",
        "movement_pattern": "hinge",
    },
    {
        "name": "Leg Curl",
        "primary_muscle": "hamstrings",
        "secondary_muscles": [],
        "equipment": "machine",
        "is_compound": False,
        "movement_pattern": "isolation",
    },
    {
        "name": "Good Morning",
        "primary_muscle": "hamstrings",
        "secondary_muscles": ["glutes", "back"],
        "equipment": "barbell",
        "movement_pattern": "hinge",
    },
    # --- Horizontal push ---
    {
        "name": "Bench Press",
        "primary_muscle": "chest",
        "secondary_muscles": ["triceps", "shoulders"],
        "equipment": "barbell",
        "requires": ["barbell", "bench"],
        "movement_pattern": "push_horizontal",
    },
    {
        "name": "Incline Bench Press",
        "primary_muscle": "chest",
        "secondary_muscles": ["shoulders", "triceps"],
        "equipment": "barbell",
        "requires": ["barbell", "bench"],
        "movement_pattern": "push_horizontal",
    },
    {
        "name": "Dumbbell Bench Press",
        "primary_muscle": "chest",
        "secondary_muscles": ["triceps", "shoulders"],
        "equipment": "dumbbell",
        "requires": ["dumbbells", "bench"],
        "movement_pattern": "push_horizontal",
    },
    {
        "name": "Incline Dumbbell Press",
        "primary_muscle": "chest",
        "secondary_muscles": ["shoulders", "triceps"],
        "equipment": "dumbbell",
        "requires": ["dumbbells", "bench"],
        "movement_pattern": "push_horizontal",
    },
    {
        "name": "Push-Up",
        "primary_muscle": "chest",
        "secondary_muscles": ["triceps", "shoulders", "core"],
        "equipment": "bodyweight",
        "movement_pattern": "push_horizontal",
    },
    {
        "name": "Dips",
        "primary_muscle": "chest",
        "secondary_muscles": ["triceps", "shoulders"],
        "equipment": "bodyweight",
        "requires": ["dip_station"],
        "movement_pattern": "push_horizontal",
    },
    {
        "name": "Cable Chest Fly",
        "primary_muscle": "chest",
        "secondary_muscles": ["shoulders"],
        "equipment": "cable",
        "is_compound": False,
        "movement_pattern": "isolation",
    },
    # --- Vertical push ---
    {
        "name": "Overhead Press",
        "primary_muscle": "shoulders",
        "secondary_muscles": ["triceps", "core"],
        "equipment": "barbell",
        "requires": ["barbell", "squat_rack"],
        "movement_pattern": "push_vertical",
    },
    {
        "name": "Dumbbell Shoulder Press",
        "primary_muscle": "shoulders",
        "secondary_muscles": ["triceps"],
        "equipment": "dumbbell",
        "movement_pattern": "push_vertical",
    },
    {
        "name": "Arnold Press",
        "primary_muscle": "shoulders",
        "secondary_muscles": ["triceps"],
        "equipment": "dumbbell",
        "movement_pattern": "push_vertical",
    },
    {
        "name": "Lateral Raise",
        "primary_muscle": "shoulders",
        "secondary_muscles": [],
        "equipment": "dumbbell",
        "is_compound": False,
        "movement_pattern": "isolation",
    },
    {
        "name": "Rear Delt Fly",
        "primary_muscle": "shoulders",
        "secondary_muscles": ["back"],
        "equipment": "dumbbell",
        "is_compound": False,
        "movement_pattern": "isolation",
    },
    # --- Horizontal pull ---
    {
        "name": "Barbell Row",
        "primary_muscle": "back",
        "secondary_muscles": ["biceps", "traps"],
        "equipment": "barbell",
        "movement_pattern": "pull_horizontal",
    },
    {
        "name": "Dumbbell Row",
        "primary_muscle": "back",
        "secondary_muscles": ["biceps"],
        "equipment": "dumbbell",
        "movement_pattern": "pull_horizontal",
    },
    {
        "name": "Seated Cable Row",
        "primary_muscle": "back",
        "secondary_muscles": ["biceps", "traps"],
        "equipment": "cable",
        "movement_pattern": "pull_horizontal",
    },
    {
        "name": "T-Bar Row",
        "primary_muscle": "back",
        "secondary_muscles": ["biceps", "traps"],
        "equipment": "barbell",
        "movement_pattern": "pull_horizontal",
    },
    {
        "name": "Chest-Supported Row",
        "primary_muscle": "back",
        "secondary_muscles": ["biceps"],
        "equipment": "machine",
        "movement_pattern": "pull_horizontal",
    },
    {
        "name": "Face Pull",
        "primary_muscle": "shoulders",
        "secondary_muscles": ["back", "traps"],
        "equipment": "cable",
        "is_compound": False,
        "movement_pattern": "isolation",
    },
    # --- Vertical pull ---
    {
        "name": "Pull-Up",
        "primary_muscle": "back",
        "secondary_muscles": ["biceps", "forearms"],
        "equipment": "bodyweight",
        "requires": ["pull_up_bar"],
        "movement_pattern": "pull_vertical",
    },
    {
        "name": "Chin-Up",
        "primary_muscle": "back",
        "secondary_muscles": ["biceps", "forearms"],
        "equipment": "bodyweight",
        "requires": ["pull_up_bar"],
        "movement_pattern": "pull_vertical",
    },
    {
        "name": "Lat Pulldown",
        "primary_muscle": "back",
        "secondary_muscles": ["biceps"],
        "equipment": "cable",
        "movement_pattern": "pull_vertical",
    },
    # --- Arms ---
    {
        "name": "Barbell Curl",
        "primary_muscle": "biceps",
        "secondary_muscles": ["forearms"],
        "equipment": "barbell",
        "is_compound": False,
        "movement_pattern": "isolation",
    },
    {
        "name": "Dumbbell Curl",
        "primary_muscle": "biceps",
        "secondary_muscles": ["forearms"],
        "equipment": "dumbbell",
        "is_compound": False,
        "movement_pattern": "isolation",
    },
    {
        "name": "Hammer Curl",
        "primary_muscle": "biceps",
        "secondary_muscles": ["forearms"],
        "equipment": "dumbbell",
        "is_compound": False,
        "movement_pattern": "isolation",
    },
    {
        "name": "Preacher Curl",
        "primary_muscle": "biceps",
        "secondary_muscles": [],
        "equipment": "machine",
        "is_compound": False,
        "movement_pattern": "isolation",
    },
    {
        "name": "Tricep Pushdown",
        "primary_muscle": "triceps",
        "secondary_muscles": [],
        "equipment": "cable",
        "is_compound": False,
        "movement_pattern": "isolation",
    },
    {
        "name": "Skull Crusher",
        "primary_muscle": "triceps",
        "secondary_muscles": [],
        "equipment": "barbell",
        "requires": ["barbell", "bench"],
        "is_compound": False,
        "movement_pattern": "isolation",
    },
    {
        "name": "Overhead Tricep Extension",
        "primary_muscle": "triceps",
        "secondary_muscles": [],
        "equipment": "dumbbell",
        "is_compound": False,
        "movement_pattern": "isolation",
    },
    # --- Core ---
    {
        "name": "Plank",
        "primary_muscle": "core",
        "secondary_muscles": ["shoulders"],
        "equipment": "bodyweight",
        "movement_pattern": "core",
    },
    {
        "name": "Hanging Leg Raise",
        "primary_muscle": "core",
        "secondary_muscles": [],
        "equipment": "bodyweight",
        "requires": ["pull_up_bar"],
        "is_compound": False,
        "movement_pattern": "core",
    },
    {
        "name": "Cable Crunch",
        "primary_muscle": "core",
        "secondary_muscles": [],
        "equipment": "cable",
        "is_compound": False,
        "movement_pattern": "core",
    },
    {
        "name": "Ab Wheel Rollout",
        "primary_muscle": "core",
        "secondary_muscles": ["shoulders"],
        "equipment": "bodyweight",
        "requires": ["ab_wheel"],
        "movement_pattern": "core",
    },
    {
        "name": "Russian Twist",
        "primary_muscle": "core",
        "secondary_muscles": [],
        "equipment": "dumbbell",
        "is_compound": False,
        "movement_pattern": "core",
    },
    # --- Calves / misc ---
    {
        "name": "Standing Calf Raise",
        "primary_muscle": "calves",
        "secondary_muscles": [],
        "equipment": "machine",
        "is_compound": False,
        "movement_pattern": "isolation",
    },
    {
        "name": "Seated Calf Raise",
        "primary_muscle": "calves",
        "secondary_muscles": [],
        "equipment": "machine",
        "is_compound": False,
        "movement_pattern": "isolation",
    },
    {
        "name": "Farmer's Carry",
        "primary_muscle": "traps",
        "secondary_muscles": ["forearms", "core", "back"],
        "equipment": "dumbbell",
        "movement_pattern": "carry",
    },
    {
        "name": "Shrug",
        "primary_muscle": "traps",
        "secondary_muscles": ["forearms"],
        "equipment": "barbell",
        "is_compound": False,
        "movement_pattern": "isolation",
    },
    # --- Home-friendly: kettlebell / dumbbells / floor only ---
    {
        "name": "Bodyweight Squat",
        "primary_muscle": "quads",
        "secondary_muscles": ["glutes"],
        "equipment": "bodyweight",
        "movement_pattern": "squat",
    },
    {
        "name": "Bodyweight Reverse Lunge",
        "primary_muscle": "quads",
        "secondary_muscles": ["glutes", "hamstrings"],
        "equipment": "bodyweight",
        "movement_pattern": "lunge",
    },
    {
        "name": "Kettlebell Goblet Squat",
        "primary_muscle": "quads",
        "secondary_muscles": ["glutes", "core"],
        "equipment": "kettlebell",
        "movement_pattern": "squat",
    },
    {
        "name": "Kettlebell Deadlift",
        "primary_muscle": "glutes",
        "secondary_muscles": ["hamstrings", "back"],
        "equipment": "kettlebell",
        "movement_pattern": "hinge",
    },
    {
        "name": "Kettlebell Clean",
        "primary_muscle": "glutes",
        "secondary_muscles": ["hamstrings", "shoulders", "core"],
        "equipment": "kettlebell",
        "movement_pattern": "hinge",
    },
    {
        "name": "Single-Leg Dumbbell RDL",
        "primary_muscle": "hamstrings",
        "secondary_muscles": ["glutes", "core"],
        "equipment": "dumbbell",
        "movement_pattern": "hinge",
    },
    {
        "name": "Dumbbell Reverse Lunge",
        "primary_muscle": "quads",
        "secondary_muscles": ["glutes", "hamstrings"],
        "equipment": "dumbbell",
        "movement_pattern": "lunge",
    },
    {
        "name": "Kettlebell Front-Rack Lunge",
        "primary_muscle": "quads",
        "secondary_muscles": ["glutes", "core"],
        "equipment": "kettlebell",
        "movement_pattern": "lunge",
    },
    {
        "name": "Dumbbell Floor Press",
        "primary_muscle": "chest",
        "secondary_muscles": ["triceps", "shoulders"],
        "equipment": "dumbbell",
        "movement_pattern": "push_horizontal",
    },
    {
        "name": "Kettlebell Floor Press",
        "primary_muscle": "chest",
        "secondary_muscles": ["triceps", "shoulders"],
        "equipment": "kettlebell",
        "movement_pattern": "push_horizontal",
    },
    {
        "name": "Kettlebell Overhead Press",
        "primary_muscle": "shoulders",
        "secondary_muscles": ["triceps", "core"],
        "equipment": "kettlebell",
        "movement_pattern": "push_vertical",
    },
    {
        "name": "Kettlebell Push Press",
        "primary_muscle": "shoulders",
        "secondary_muscles": ["triceps", "quads"],
        "equipment": "kettlebell",
        "movement_pattern": "push_vertical",
    },
    {
        "name": "Kettlebell Single-Arm Row",
        "primary_muscle": "back",
        "secondary_muscles": ["biceps", "core"],
        "equipment": "kettlebell",
        "movement_pattern": "pull_horizontal",
    },
    {
        "name": "Dumbbell Renegade Row",
        "primary_muscle": "back",
        "secondary_muscles": ["core", "biceps"],
        "equipment": "dumbbell",
        "movement_pattern": "pull_horizontal",
    },
    {
        "name": "Dumbbell Floor Pullover",
        "primary_muscle": "back",
        "secondary_muscles": ["chest", "triceps"],
        "equipment": "dumbbell",
        "movement_pattern": "pull_vertical",
    },
    {
        "name": "Turkish Get-Up",
        "primary_muscle": "core",
        "secondary_muscles": ["shoulders", "glutes"],
        "equipment": "kettlebell",
        "movement_pattern": "core",
    },
    {
        "name": "Kettlebell Suitcase Carry",
        "primary_muscle": "core",
        "secondary_muscles": ["forearms", "traps"],
        "equipment": "kettlebell",
        "movement_pattern": "carry",
    },
    {
        "name": "Dumbbell Floor Skull Crusher",
        "primary_muscle": "triceps",
        "secondary_muscles": [],
        "equipment": "dumbbell",
        "is_compound": False,
        "movement_pattern": "isolation",
    },
    {
        "name": "Single-Leg Dumbbell Calf Raise",
        "primary_muscle": "calves",
        "secondary_muscles": [],
        "equipment": "dumbbell",
        "is_compound": False,
        "movement_pattern": "isolation",
    },
    {
        "name": "Glute Bridge",
        "primary_muscle": "glutes",
        "secondary_muscles": ["hamstrings"],
        "equipment": "bodyweight",
        "is_compound": False,
        "movement_pattern": "isolation",
    },
]
