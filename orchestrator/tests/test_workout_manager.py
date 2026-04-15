"""
Tests for workout_manager.py.

state_store calls are mocked or backed by tmp_db (the fixture from conftest)
depending on the test. External LLM/vision calls are never made.
"""

import unittest.mock as mock

import pytest


# ---------------------------------------------------------------------------
# Exercise catalog fixtures
# ---------------------------------------------------------------------------

_EXERCISES = [
    {
        "name": "Barbell Squat",
        "primary_muscle": "quads",
        "secondary_muscles": ["glutes", "hamstrings"],
        "equipment": "barbell",
        "is_compound": True,
        "movement_pattern": "squat",
    },
    {
        "name": "Romanian Deadlift",
        "primary_muscle": "hamstrings",
        "secondary_muscles": ["glutes", "back"],
        "equipment": "barbell",
        "is_compound": True,
        "movement_pattern": "hinge",
    },
    {
        "name": "Bench Press",
        "primary_muscle": "chest",
        "secondary_muscles": ["triceps", "front_delts"],
        "equipment": "barbell",
        "is_compound": True,
        "movement_pattern": "push_horizontal",
    },
    {
        "name": "Barbell Row",
        "primary_muscle": "back",
        "secondary_muscles": ["biceps", "rear_delts"],
        "equipment": "barbell",
        "is_compound": True,
        "movement_pattern": "pull_horizontal",
    },
    {
        "name": "Overhead Press",
        "primary_muscle": "front_delts",
        "secondary_muscles": ["triceps"],
        "equipment": "barbell",
        "is_compound": True,
        "movement_pattern": "push_vertical",
    },
    {
        "name": "Pull-up",
        "primary_muscle": "back",
        "secondary_muscles": ["biceps"],
        "equipment": "bodyweight",
        "is_compound": True,
        "movement_pattern": "pull_vertical",
    },
    {
        "name": "Plank",
        "primary_muscle": "core",
        "secondary_muscles": [],
        "equipment": "bodyweight",
        "is_compound": False,
        "movement_pattern": "core",
    },
    {
        "name": "Bicep Curl",
        "primary_muscle": "biceps",
        "secondary_muscles": [],
        "equipment": "dumbbell",
        "is_compound": False,
        "movement_pattern": "isolation",
    },
    {
        "name": "Lunge",
        "primary_muscle": "quads",
        "secondary_muscles": ["glutes"],
        "equipment": "bodyweight",
        "is_compound": True,
        "movement_pattern": "lunge",
    },
]


@pytest.fixture()
def seeded_db(tmp_db):
    from orchestrator import state_store

    state_store.seed_exercises(_EXERCISES)
    return tmp_db


# ---------------------------------------------------------------------------
# generate_workout — _decide_workout_type branches
# ---------------------------------------------------------------------------


def test_generate_workout_no_history_returns_full_body(seeded_db):
    """No prior workouts → _decide_workout_type should pick full_body."""
    from orchestrator import workout_manager

    # Both callables return "no history" state
    with mock.patch.object(
        workout_manager.state_store, "count_workouts_in_window", return_value=0
    ), mock.patch.object(
        workout_manager.state_store, "days_since_last_workout", return_value=None
    ), mock.patch.object(
        workout_manager.state_store, "get_recent_muscle_groups", return_value={}
    ):
        result = workout_manager.generate_workout()

    assert result["ok"] is True
    assert result["workout_type"] == "full_body"
    assert result["workout_id"] > 0
    assert len(result["exercises"]) > 0


def test_generate_workout_long_gap_returns_full_body(seeded_db):
    """5 days since last workout → full_body."""
    from orchestrator import workout_manager

    with mock.patch.object(
        workout_manager.state_store, "count_workouts_in_window", return_value=1
    ), mock.patch.object(
        workout_manager.state_store, "days_since_last_workout", return_value=5
    ), mock.patch.object(
        workout_manager.state_store, "get_recent_muscle_groups", return_value={}
    ):
        result = workout_manager.generate_workout()

    assert result["ok"] is True
    assert result["workout_type"] == "full_body"


def test_generate_workout_complement_after_one_session(seeded_db):
    """1 session in last 4 days and less than 4 days ago → full_body_complement."""
    from orchestrator import workout_manager

    with mock.patch.object(
        workout_manager.state_store, "count_workouts_in_window", return_value=1
    ), mock.patch.object(
        workout_manager.state_store, "days_since_last_workout", return_value=1
    ), mock.patch.object(
        workout_manager.state_store, "get_recent_muscle_groups", return_value={}
    ):
        result = workout_manager.generate_workout()

    assert result["ok"] is True
    assert result["workout_type"] == "full_body_complement"


def test_generate_workout_split_push_day(seeded_db):
    """2+ sessions in last 4 days, legs most trained → push_day selected."""
    from orchestrator import workout_manager

    # quads/hamstrings/glutes loaded high → legs load is highest → push is lowest
    recent = {"quads": 10, "hamstrings": 10, "glutes": 10, "chest": 1, "back": 1}

    with mock.patch.object(
        workout_manager.state_store, "count_workouts_in_window", return_value=2
    ), mock.patch.object(
        workout_manager.state_store, "days_since_last_workout", return_value=1
    ), mock.patch.object(
        workout_manager.state_store, "get_recent_muscle_groups", return_value=recent
    ):
        result = workout_manager.generate_workout()

    assert result["ok"] is True
    assert result["workout_type"] == "push_day"


def test_generate_workout_split_legs_day(seeded_db):
    """2+ sessions, push+pull muscles loaded → legs_day chosen."""
    from orchestrator import workout_manager

    recent = {
        "chest": 12,
        "triceps": 10,
        "front_delts": 8,
        "back": 12,
        "biceps": 10,
        "quads": 0,
    }

    with mock.patch.object(
        workout_manager.state_store, "count_workouts_in_window", return_value=3
    ), mock.patch.object(
        workout_manager.state_store, "days_since_last_workout", return_value=0
    ), mock.patch.object(
        workout_manager.state_store, "get_recent_muscle_groups", return_value=recent
    ):
        result = workout_manager.generate_workout()

    assert result["ok"] is True
    assert result["workout_type"] == "legs_day"


def test_generate_workout_sets_structure(seeded_db):
    """Each exercise in the plan must have 3 sets with set_number 1, 2, 3."""
    from orchestrator import workout_manager

    with mock.patch.object(
        workout_manager.state_store, "count_workouts_in_window", return_value=0
    ), mock.patch.object(
        workout_manager.state_store, "days_since_last_workout", return_value=None
    ), mock.patch.object(
        workout_manager.state_store, "get_recent_muscle_groups", return_value={}
    ):
        result = workout_manager.generate_workout()

    assert result["ok"] is True
    for ex in result["exercises"]:
        assert len(ex["sets"]) == 3
        set_numbers = [s["set_number"] for s in ex["sets"]]
        assert set_numbers == [1, 2, 3]


# ---------------------------------------------------------------------------
# log_set — creates workout when none exists today
# ---------------------------------------------------------------------------


def test_log_set_creates_custom_workout_when_none(seeded_db):
    """log_set with no existing today's workout creates a new one with workout_type='custom'."""
    from orchestrator import state_store, workout_manager

    assert state_store.get_todays_workout() is None

    result = workout_manager.log_set(
        exercise_name="Barbell Squat",
        weight_lbs=135.0,
        reps=8,
    )

    assert result["ok"] is True
    assert result["workout_id"] > 0

    workout = state_store.get_workout(result["workout_id"])
    assert workout["workout_type"] == "custom"


def test_log_set_uses_existing_workout(seeded_db):
    """If today's workout already exists, log_set reuses it."""
    from orchestrator import state_store, workout_manager

    wid = state_store.create_workout("full_body", True)
    result = workout_manager.log_set(
        exercise_name="Barbell Squat",
        weight_lbs=100.0,
        reps=5,
    )

    assert result["ok"] is True
    assert result["workout_id"] == wid


def test_log_set_with_explicit_workout_id(seeded_db):
    from orchestrator import state_store, workout_manager

    wid = state_store.create_workout("push_day", True)
    result = workout_manager.log_set(
        exercise_name="Bench Press",
        weight_lbs=185.0,
        reps=5,
        workout_id=wid,
    )
    assert result["ok"] is True
    assert result["workout_id"] == wid
    assert result["set"]["exercise_name"] == "Bench Press"


# ---------------------------------------------------------------------------
# modify_workout — completed sets must not be removed
# ---------------------------------------------------------------------------


def test_modify_workout_remove_only_planned_sets(seeded_db):
    """Removing an exercise from a workout only deletes uncompleted sets."""
    from orchestrator import state_store, workout_manager

    state_store.seed_exercises(_EXERCISES)
    wid = state_store.create_workout("full_body", True)

    # Add one planned set and one completed set for the same exercise
    planned_id = state_store.add_planned_set(wid, "Bench Press", ["chest"], 1, 8, 135.0)
    state_store.log_completed_set(wid, "Bench Press", 135.0, 8, set_id=planned_id)

    extra_planned = state_store.add_planned_set(wid, "Bench Press", ["chest"], 2, 8, 135.0)

    workout_before = state_store.get_workout(wid)
    bench_sets_before = [s for s in workout_before["sets"] if s["exercise_name"] == "Bench Press"]
    assert len(bench_sets_before) == 2

    result = workout_manager.modify_workout(wid, remove_exercises=["Bench Press"])
    assert result["ok"] is True
    # Only the uncompleted set (extra_planned) should be removed
    assert result["removed_sets"] == 1

    workout_after = state_store.get_workout(wid)
    bench_sets_after = [s for s in workout_after["sets"] if s["exercise_name"] == "Bench Press"]
    # The completed set must still be there
    assert len(bench_sets_after) == 1
    assert bench_sets_after[0]["completed"] is True


def test_modify_workout_not_found(seeded_db):
    from orchestrator import workout_manager

    result = workout_manager.modify_workout(99999, remove_exercises=["Bench Press"])
    assert result["ok"] is False
    assert "not found" in result["error"].lower()


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


def test_get_status_no_workout(seeded_db):
    from orchestrator import workout_manager

    status = workout_manager.get_status()
    assert status["has_workout"] is False


def test_get_status_with_workout(seeded_db):
    from orchestrator import state_store, workout_manager

    wid = state_store.create_workout("full_body", True)
    state_store.add_planned_set(wid, "Barbell Squat", ["quads"], 1, 8, 135.0)

    status = workout_manager.get_status()
    assert status["has_workout"] is True
    assert status["workout_type"] == "full_body"
    assert status["total_sets"] == 1
    assert status["completed_sets"] == 0


# ---------------------------------------------------------------------------
# get_history
# ---------------------------------------------------------------------------


def test_get_history_empty(seeded_db):
    from orchestrator import workout_manager

    assert workout_manager.get_history() == []


def test_get_history_summary(seeded_db):
    from orchestrator import state_store, workout_manager

    wid = state_store.create_workout("full_body", True)
    state_store.log_completed_set(wid, "Barbell Squat", 135.0, 8)

    history = workout_manager.get_history(days=1)
    assert len(history) == 1
    h = history[0]
    assert h["id"] == wid
    assert h["workout_type"] == "full_body"
    assert h["completed_set_count"] == 1
    assert h["total_volume_lbs"] == 135.0 * 8
