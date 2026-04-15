"""
Tests for state_store workout and meal functions.

Uses the tmp_db fixture from conftest to get a fresh isolated SQLite DB per test.
"""

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SQUAT_EX = {
    "name": "Barbell Squat",
    "primary_muscle": "quads",
    "secondary_muscles": ["glutes", "hamstrings"],
    "equipment": "barbell",
    "is_compound": True,
    "movement_pattern": "squat",
}

_BENCH_EX = {
    "name": "Bench Press",
    "primary_muscle": "chest",
    "secondary_muscles": ["triceps", "front_delts"],
    "equipment": "barbell",
    "is_compound": True,
    "movement_pattern": "push_horizontal",
}


# ---------------------------------------------------------------------------
# seed_exercises — idempotency
# ---------------------------------------------------------------------------


def test_seed_exercises_inserts(tmp_db):
    """seed_exercises inserts exercises that don't yet exist."""
    from orchestrator import state_store

    # Use novel names that the production seed definitely doesn't contain
    novel = [
        {**_SQUAT_EX, "name": "ZZZ_Test_Squat_Novel"},
        {**_BENCH_EX, "name": "ZZZ_Test_Bench_Novel"},
    ]
    count = state_store.seed_exercises(novel)
    assert count == 2


def test_seed_exercises_idempotent(tmp_db):
    """Calling seed_exercises twice with the same data inserts each exercise once."""
    from orchestrator import state_store

    novel = [
        {**_SQUAT_EX, "name": "ZZZ_Idempotent_Squat"},
        {**_BENCH_EX, "name": "ZZZ_Idempotent_Bench"},
    ]
    first = state_store.seed_exercises(novel)
    second = state_store.seed_exercises(novel)
    assert first == 2
    assert second == 0  # nothing new — both already exist

    # Verify exact count in DB for these names
    all_ex = state_store.get_exercises()
    names = [e["name"] for e in all_ex]
    assert names.count("ZZZ_Idempotent_Squat") == 1
    assert names.count("ZZZ_Idempotent_Bench") == 1


# ---------------------------------------------------------------------------
# get_exercises — filtering
# ---------------------------------------------------------------------------


def test_get_exercises_by_pattern(tmp_db):
    """get_exercises(movement_pattern=...) filters correctly."""
    from orchestrator import state_store

    # Use a novel pattern name that won't exist in the production seed
    novel = {**_SQUAT_EX, "name": "ZZZ_PatternFilter_Squat", "movement_pattern": "zzz_novel_pattern"}
    state_store.seed_exercises([novel])
    results = state_store.get_exercises(movement_pattern="zzz_novel_pattern")
    assert len(results) == 1
    assert results[0]["name"] == "ZZZ_PatternFilter_Squat"
    assert isinstance(results[0]["secondary_muscles"], list)
    assert "glutes" in results[0]["secondary_muscles"]


def test_get_exercises_catalog_is_seeded(tmp_db):
    """init_db seeds the production catalog — catalog must be non-empty."""
    from orchestrator import state_store

    result = state_store.get_exercises()
    assert len(result) > 0


# ---------------------------------------------------------------------------
# create_workout / get_workout / get_todays_workout
# ---------------------------------------------------------------------------


def test_create_and_get_workout(tmp_db):
    from orchestrator import state_store

    wid = state_store.create_workout(
        workout_type="full_body",
        generated_by_jess=True,
        reasoning="Test reasoning",
    )
    assert isinstance(wid, int) and wid > 0

    w = state_store.get_workout(wid)
    assert w is not None
    assert w["workout_type"] == "full_body"
    assert w["generated_by_jess"] is True
    assert w["reasoning"] == "Test reasoning"
    assert w["sets"] == []


def test_get_todays_workout(tmp_db):
    from orchestrator import state_store

    assert state_store.get_todays_workout() is None
    wid = state_store.create_workout("full_body", True)
    today = state_store.get_todays_workout()
    assert today is not None
    assert today["id"] == wid


# ---------------------------------------------------------------------------
# add_planned_set / log_completed_set
# ---------------------------------------------------------------------------


def test_add_planned_set(tmp_db):
    from orchestrator import state_store

    wid = state_store.create_workout("full_body", True)
    sid = state_store.add_planned_set(
        workout_id=wid,
        exercise_name="Barbell Squat",
        muscle_groups=["quads", "glutes"],
        set_number=1,
        target_reps=8,
        target_weight_lbs=135.0,
    )
    assert isinstance(sid, int) and sid > 0

    w = state_store.get_workout(wid)
    assert len(w["sets"]) == 1
    s = w["sets"][0]
    assert s["exercise_name"] == "Barbell Squat"
    assert s["target_reps"] == 8
    assert s["completed"] is False


def test_log_completed_set_updates_planned(tmp_db):
    from orchestrator import state_store

    state_store.seed_exercises([_SQUAT_EX])
    wid = state_store.create_workout("full_body", True)
    sid = state_store.add_planned_set(wid, "Barbell Squat", ["quads"], 1, 8, 135.0)

    row = state_store.log_completed_set(
        workout_id=wid,
        exercise_name="Barbell Squat",
        weight_lbs=135.0,
        reps=8,
        rpe=7.0,
        set_id=sid,
    )
    assert row["completed"] is True
    assert row["weight_lbs"] == 135.0
    assert row["reps"] == 8


def test_log_completed_set_insert_new(tmp_db):
    """Log a set without an existing planned set — should insert new row."""
    from orchestrator import state_store

    state_store.seed_exercises([_SQUAT_EX])
    wid = state_store.create_workout("full_body", True)
    row = state_store.log_completed_set(
        workout_id=wid,
        exercise_name="Barbell Squat",
        weight_lbs=100.0,
        reps=5,
    )
    assert row["completed"] is True
    assert row["exercise_name"] == "Barbell Squat"


# ---------------------------------------------------------------------------
# count_workouts_in_window / days_since_last_workout
# ---------------------------------------------------------------------------


def test_count_workouts_in_window_zero(tmp_db):
    from orchestrator import state_store

    assert state_store.count_workouts_in_window(7) == 0


def test_count_workouts_in_window(tmp_db):
    from orchestrator import state_store

    state_store.create_workout("full_body", True)
    state_store.create_workout("push_day", True)
    assert state_store.count_workouts_in_window(7) == 2


def test_days_since_last_workout_none_when_empty(tmp_db):
    from orchestrator import state_store

    assert state_store.days_since_last_workout() is None


def test_days_since_last_workout_today(tmp_db):
    from orchestrator import state_store

    state_store.create_workout("full_body", True)
    assert state_store.days_since_last_workout() == 0


# ---------------------------------------------------------------------------
# get_recent_muscle_groups
# ---------------------------------------------------------------------------


def test_get_recent_muscle_groups_empty(tmp_db):
    from orchestrator import state_store

    result = state_store.get_recent_muscle_groups(days=3)
    assert result == {}


def test_get_recent_muscle_groups_counts(tmp_db):
    from orchestrator import state_store

    state_store.seed_exercises([_SQUAT_EX])
    wid = state_store.create_workout("full_body", True)
    state_store.log_completed_set(wid, "Barbell Squat", 135.0, 8)
    state_store.log_completed_set(wid, "Barbell Squat", 135.0, 8)

    muscles = state_store.get_recent_muscle_groups(days=1)
    assert muscles.get("quads", 0) == 2


# ---------------------------------------------------------------------------
# get_exercise_prs
# ---------------------------------------------------------------------------


def test_get_exercise_prs_none_when_empty(tmp_db):
    from orchestrator import state_store

    assert state_store.get_exercise_prs("Barbell Squat") is None


def test_get_exercise_prs_returns_best(tmp_db):
    from orchestrator import state_store

    state_store.seed_exercises([_SQUAT_EX])
    wid = state_store.create_workout("full_body", True)
    state_store.log_completed_set(wid, "Barbell Squat", 100.0, 8)
    state_store.log_completed_set(wid, "Barbell Squat", 150.0, 5)  # heavier — should be PR

    pr = state_store.get_exercise_prs("Barbell Squat")
    assert pr is not None
    assert float(pr["weight_lbs"]) == 150.0


# ---------------------------------------------------------------------------
# update_meal — photo_path NOT in allowlist (security regression)
# ---------------------------------------------------------------------------


def test_update_meal_rejects_photo_path(tmp_db):
    """photo_path must NOT be in the update_meal allowlist."""
    from orchestrator import state_store

    meal = state_store.add_meal(
        description="Salad",
        meal_type="lunch",
        calories=300,
        photo_path=None,
    )
    meal_id = meal["id"]

    # Attempt to overwrite photo_path via update — must return False (not allowed)
    ok = state_store.update_meal(meal_id, {"photo_path": "/etc/passwd"})
    assert ok is False

    # Row must be unchanged
    fetched = state_store.get_meal(meal_id)
    assert fetched["photo_path"] is None


def test_update_meal_allowed_fields(tmp_db):
    """description/meal_type/calories are allowed via update_meal."""
    from orchestrator import state_store

    meal = state_store.add_meal(description="Oats", meal_type="breakfast", calories=350)
    ok = state_store.update_meal(meal["id"], {"calories": 400, "description": "Oats + banana"})
    assert ok is True
    updated = state_store.get_meal(meal["id"])
    assert updated["calories"] == 400
    assert updated["description"] == "Oats + banana"


# ---------------------------------------------------------------------------
# get_meals_today
# ---------------------------------------------------------------------------


def test_get_meals_today_empty(tmp_db):
    from orchestrator import state_store

    assert state_store.get_meals_today() == []


def test_get_meals_today_returns_todays_meals(tmp_db):
    from orchestrator import state_store

    state_store.add_meal("Breakfast", "breakfast", 400)
    state_store.add_meal("Lunch", "lunch", 600)
    meals = state_store.get_meals_today()
    assert len(meals) == 2
    descs = {m["description"] for m in meals}
    assert "Breakfast" in descs
    assert "Lunch" in descs
