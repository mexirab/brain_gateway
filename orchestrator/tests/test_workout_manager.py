"""
Tests for workout_manager.py.

state_store calls are mocked or backed by tmp_db (the fixture from conftest)
depending on the test. External LLM/vision calls are never made.
"""

import logging
import unittest.mock as mock

import pytest

from orchestrator import state_store, workout_manager
from orchestrator.exercises_seed import FALLBACK_VARIANTS
from orchestrator.metrics import WORKOUT_GENERATED, WORKOUT_PROGRESSION
from orchestrator.tests._helpers import metric_value
from orchestrator.tests.fixtures_pantry import workout_backdate as _backdate
from orchestrator.tests.fixtures_pantry import workout_session as _session
from orchestrator.tests.fixtures_pantry import workout_train_all as _train_all
from orchestrator.workout_manager import (
    _add_exercise_sets,
    _cap_load,
    _gear_ceiling,
    _pick_for_slots,
    _plan_target,
    deload_status,
    exercises_for_minutes,
    generate_workout,
    get_program_overview,
    get_status,
    modify_workout,
    next_program_day,
    program_split,
)
from orchestrator.workout_setup import add_location, clear_program, remove_location, set_program

# ---------------------------------------------------------------------------
# generate_workout — _decide_workout_type branches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "count,days_since,recent,expected",
    [
        pytest.param(0, None, {}, "full_body", id="no-history"),
        pytest.param(1, 5, {}, "full_body", id="long-gap"),
        pytest.param(1, 1, {}, "full_body_complement", id="one-recent-session"),
        # 2+ sessions in 4 days: the least-loaded split wins
        pytest.param(
            2, 1, {"quads": 10, "hamstrings": 10, "glutes": 10, "chest": 1, "back": 1}, "push_day", id="legs-loaded"
        ),
        pytest.param(
            3,
            0,
            {"chest": 12, "triceps": 10, "front_delts": 8, "back": 12, "biceps": 10, "quads": 0},
            "legs_day",
            id="push-pull-loaded",
        ),
    ],
)
def test_generate_workout_decides_type_from_history(tmp_db, count, days_since, recent, expected):
    with (
        mock.patch.object(workout_manager.state_store, "count_workouts_in_window", return_value=count),
        mock.patch.object(workout_manager.state_store, "days_since_last_workout", return_value=days_since),
        mock.patch.object(workout_manager.state_store, "get_recent_muscle_groups", return_value=recent),
    ):
        result = workout_manager.generate_workout()

    assert result["ok"] is True
    assert result["workout_type"] == expected
    assert result["workout_id"] > 0 and result["exercises"]
    # Each exercise gets 3 sets numbered 1, 2, 3.
    assert all([s["set_number"] for s in ex["sets"]] == [1, 2, 3] for ex in result["exercises"])


# ---------------------------------------------------------------------------
# log_set — creates workout when none exists today
# ---------------------------------------------------------------------------


def test_log_set_creates_custom_workout_when_none(tmp_db):
    """log_set with no existing today's workout creates a new one with workout_type='custom'."""
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


def test_log_set_uses_existing_workout(tmp_db):
    """If today's workout already exists, log_set reuses it."""
    wid = state_store.create_workout("full_body", True)
    result = workout_manager.log_set(
        exercise_name="Barbell Squat",
        weight_lbs=100.0,
        reps=5,
    )

    assert result["ok"] is True
    assert result["workout_id"] == wid


def test_log_set_with_explicit_workout_id(tmp_db):
    wid = state_store.create_workout("push_day", True)
    with mock.patch("orchestrator.selfcare_manager.record_movement_logged") as mock_rec:
        result = workout_manager.log_set(exercise_name="Bench Press", weight_lbs=185.0, reps=5, workout_id=wid)
    assert result["ok"] is True
    assert result["workout_id"] == wid
    assert result["set"]["exercise_name"] == "Bench Press"
    # The selfcare movement bridge fires for an explicit workout id too.
    mock_rec.assert_called_once_with("set:Bench Press")


# ---------------------------------------------------------------------------
# modify_workout — completed sets must not be removed
# ---------------------------------------------------------------------------


def test_modify_workout_remove_only_planned_sets(tmp_db):
    """Removing an exercise from a workout only deletes uncompleted sets."""
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


def test_modify_workout_not_found(tmp_db):
    result = workout_manager.modify_workout(99999, remove_exercises=["Bench Press"])
    assert result["ok"] is False
    assert "not found" in result["error"].lower()


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


def test_get_status_no_workout(tmp_db):
    status = workout_manager.get_status()
    assert status["has_workout"] is False


def test_get_status_with_workout(tmp_db):
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


def test_get_history_empty(tmp_db):
    assert workout_manager.get_history() == []


def test_get_history_summary(tmp_db):
    wid = state_store.create_workout("full_body", True)
    state_store.log_completed_set(wid, "Barbell Squat", 135.0, 8)

    history = workout_manager.get_history(days=1)
    assert len(history) == 1
    h = history[0]
    assert h["id"] == wid
    assert h["workout_type"] == "full_body"
    assert h["completed_set_count"] == 1
    assert h["total_volume_lbs"] == 135.0 * 8


# ---------------------------------------------------------------------------
# log_set → selfcare movement bridge
# ---------------------------------------------------------------------------
# Logging a set is obvious movement — the bridge resets sitting_since so the
# 15-min selfcare loop doesn't fire "you've been sitting for 274 min" while
# the user is actively at the gym. Bridge failures must not break log_set.


def test_log_set_calls_record_movement_logged(tmp_db):
    """log_set routes through selfcare_manager.record_movement_logged with
    label 'set:<exercise_name>' so the movement gate resets on every set."""
    with mock.patch("orchestrator.selfcare_manager.record_movement_logged") as mock_rec:
        result = workout_manager.log_set("Bench Press", 135.0, 8)

    assert result["ok"] is True
    mock_rec.assert_called_once_with("set:Bench Press")


def test_log_set_bridge_exception_does_not_propagate(tmp_db, caplog):
    """If record_movement_logged raises, log_set still returns its normal
    {"ok": True, ...} dict. ERROR with exc_info is logged."""

    def _boom(label):
        raise RuntimeError("selfcare api drifted")

    with (
        mock.patch("orchestrator.selfcare_manager.record_movement_logged", side_effect=_boom),
        caplog.at_level(logging.ERROR, logger="orchestrator.workout_manager"),
    ):
        result = workout_manager.log_set("Bench Press", 135.0, 8)

    # Normal return value — bridge failure doesn't break the set log
    assert result["ok"] is True
    assert result["workout_id"] > 0
    assert result["set"]["exercise_name"] == "Bench Press"

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("Selfcare movement bridge failed" in r.getMessage() for r in errors), (
        f"Expected 'Selfcare movement bridge failed' ERROR; got: {[r.getMessage() for r in errors]}"
    )
    assert any(r.exc_info is not None for r in errors), "Expected exc_info on ERROR record"


# ===========================================================================
# Program builder: locations, programs, load caps (real seed catalog via tmp_db)
# ===========================================================================

_HOME_GEAR = {"dumbbells", "kettlebell"}


def _add_loc(name, template="commercial_gym", equipment=None, limits=None):
    r = add_location(name, template=template, equipment=equipment, limits=limits)
    assert r["ok"] is True, r
    return r["location"]


def _complete_first_set(workout_id, weight=20.0, reps=8):
    s = state_store.get_workout(workout_id)["sets"][0]
    state_store.log_completed_set(workout_id, s["exercise_name"], weight, reps, set_id=s["id"])


def _seed_pr(exercise_name, weight_lbs, reps=5):
    wid = state_store.create_workout("custom", False)
    state_store.log_completed_set(wid, exercise_name, weight_lbs, reps)


# --- exercises_for_minutes / program_split ----------------------------------


@pytest.mark.parametrize(
    "minutes, expected",
    [(20, 3), (27, 3), (36, 4), (45, 5), (60, 7), (72, 8), (120, 8), (0, 3), (1000, 8)],
)
def test_exercises_for_minutes_clamped(minutes, expected):
    assert exercises_for_minutes(minutes) == expected


@pytest.mark.parametrize(
    "days, labels",
    [
        (2, ["Full Body A", "Full Body B"]),
        (3, ["Full Body A", "Full Body B", "Full Body C"]),
        (4, ["Upper", "Lower", "Upper", "Lower"]),
        (5, ["Upper", "Lower", "Push", "Pull", "Legs"]),
        (6, ["Push", "Pull", "Legs", "Push", "Pull", "Legs"]),
    ],
)
def test_program_overview_split_labels(tmp_db, days, labels):
    set_program("general", days, 45)
    p = get_program_overview()["program"]
    assert p["split"] == labels
    assert p["next_day_index"] == 0
    assert p["next_day_label"] == labels[0]
    assert p["exercises_per_session"] == 5


def test_program_split_unknown_days_falls_back_to_three():
    assert program_split(7) == program_split(3)


def test_program_overview_without_program(tmp_db):
    _add_loc("Home", template="home_basic")
    ov = get_program_overview()
    assert ov["program"] is None
    assert [loc["name"] for loc in ov["locations"]] == ["Home"]


@pytest.mark.parametrize("minutes, expected", [(20, 3), (45, 5), (120, 8)])
def test_program_minutes_set_exercise_count(tmp_db, minutes, expected):
    set_program("general", 3, minutes)
    r = generate_workout()
    assert r["ok"] is True
    assert len(r["exercises"]) == expected
    assert r["program_day"] == {"index": 0, "of": 3, "label": "Full Body A"}
    assert r["workout_type"] == "full_body"


def test_program_goal_sets_rep_scheme(tmp_db):
    set_program("strength", 3, 20)  # 3 slots: squat, push, row — all compound
    r = generate_workout()
    for ex in r["exercises"]:
        assert ex["is_compound"] is True
        assert len(ex["sets"]) == 4
        assert all(s["target_reps"] == 5 for s in ex["sets"])


# --- next_program_day --------------------------------------------------------


def test_next_program_day_advances_only_after_completed_sets(tmp_db):
    program = set_program("general", 3, 45)["program"]

    first = generate_workout()
    assert first["program_day"]["index"] == 0

    # Generated but untouched → the same day is offered again (skipped day).
    again = generate_workout()
    assert again["program_day"]["index"] == 0
    assert next_program_day(program) == 0

    _complete_first_set(again["workout_id"])
    assert next_program_day(program) == 1

    second = generate_workout()
    assert second["program_day"] == {"index": 1, "of": 3, "label": "Full Body B"}
    w = state_store.get_workout(second["workout_id"])
    assert (w["program_id"], w["program_day"]) == (program["id"], 1)

    _complete_first_set(second["workout_id"])
    third = generate_workout()
    assert third["program_day"]["index"] == 2
    _complete_first_set(third["workout_id"])
    # Wraps around after the last day of the split
    assert get_program_overview()["program"]["next_day_index"] == 0


def test_set_program_minutes_change_keeps_split_position(tmp_db):
    program = set_program("general", 4, 45)["program"]
    _complete_first_set(generate_workout()["workout_id"])
    assert next_program_day(program) == 1

    updated = set_program("muscle", 4, 60)["program"]
    assert updated["id"] == program["id"]
    assert next_program_day(state_store.get_active_program()) == 1
    assert generate_workout()["program_day"]["label"] == "Lower"


def test_set_program_days_change_restarts_split(tmp_db):
    set_program("general", 4, 45)
    _complete_first_set(generate_workout()["workout_id"])
    set_program("general", 5, 45)
    assert next_program_day(state_store.get_active_program()) == 0


def test_last_trained_program_day_ignores_other_programs(tmp_db):
    wid = state_store.create_workout("full_body", True)
    state_store.set_workout_context(wid, None, 999, 2)
    state_store.log_completed_set(wid, "Push-Up", 0.0, 10)
    assert state_store.last_trained_program_day(999) == 2
    assert state_store.last_trained_program_day(1000) is None


# --- location filtering ------------------------------------------------------


@pytest.mark.parametrize("days", [None, 2, 3, 4, 5, 6])
def test_home_basic_never_needs_missing_gear(tmp_db, days):
    """Every day of every split (and the no-program planner) at a dumbbell+kettlebell
    home only picks exercises whose requires ⊆ that gear."""
    home = _add_loc("Home", template="home_basic")
    if days:
        set_program("general", days, 120)
    for _ in range(max(days or 0, 6)):
        r = generate_workout(home["id"])
        assert r["ok"] is True, r
        assert r["exercises"]
        for ex in r["exercises"]:
            requires = set(state_store.get_exercise(ex["name"])["requires"])
            assert requires <= _HOME_GEAR, f"{ex['name']} needs {requires - _HOME_GEAR}"
        if days:
            _complete_first_set(r["workout_id"])  # walk through every day of the split
        else:
            state_store.delete_workout(r["workout_id"])


def test_full_gym_never_picks_fallback_variants(tmp_db):
    gym = _add_loc("Big Gym")
    for _ in range(8):
        r = generate_workout(gym["id"])
        assert not {ex["name"] for ex in r["exercises"]} & FALLBACK_VARIANTS
        assert r["notes"] == []
        state_store.delete_workout(r["workout_id"])


def test_pick_prefers_non_fallback_variant(tmp_db):
    """Push-Up beats the floor presses at home; the floor pullover is still used
    when it is the only vertical pull."""
    for _ in range(10):
        picked, notes = _pick_for_slots(["push_horizontal", "pull_vertical"], set(_HOME_GEAR))
        assert [p["name"] for p in picked] == ["Push-Up", "Dumbbell Floor Pullover"]
        assert notes == []


def test_pick_pattern_fallback_reported_in_notes(tmp_db):
    picked, notes = _pick_for_slots(["pull_vertical"], {"kettlebell"})
    assert [p["movement_pattern"] for p in picked] == ["pull_horizontal"]
    assert notes == ["No vertical pull option here — used a row instead."]


def test_pick_skip_reported_in_notes(tmp_db):
    picked, notes = _pick_for_slots(["hinge"], set())
    assert picked == []
    assert notes == ["Skipped hinge — nothing for it here."]


def test_pick_no_location_means_no_notes(tmp_db):
    # Even when a slot has to fall back, notes are location-only.
    picked, notes = _pick_for_slots(["isolation", "pull_vertical", "pull_vertical", "pull_vertical", "pull_vertical"])
    assert notes == []
    assert picked


def test_pick_primary_pass_runs_before_fallbacks(tmp_db):
    """An early slot's fallback must not steal the only exercise a later slot needs."""
    # Bodyweight only: push_vertical has nothing and falls back to push_horizontal,
    # whose only option (Push-Up) belongs to the later push_horizontal slot.
    picked, notes = _pick_for_slots(["push_vertical", "push_horizontal"], set())
    assert [p["name"] for p in picked] == ["Push-Up"]
    assert notes == ["Skipped overhead press — nothing for it here."]


def test_pick_isolation_respects_focus(tmp_db):
    picked, _ = _pick_for_slots(["isolation"] * 3, None, focus=frozenset({"biceps"}))
    assert picked
    assert all(p["primary_muscle"] == "biceps" for p in picked)
    assert len({p["name"] for p in picked}) == len(picked)


def test_generate_at_location_reports_fallback_notes(tmp_db):
    kb = _add_loc("Hotel", equipment=["kettlebell"])
    r = generate_workout(kb["id"])
    assert r["ok"] is True
    assert r["workout_type"] == "full_body"  # no program → old planner
    assert "Skipped vertical pull — nothing for it here." in r["notes"]
    assert "At Hotel." in r["reasoning"]
    assert r["notes"][0] in r["reasoning"]


def test_generate_without_location_has_no_notes(tmp_db):
    r = generate_workout()
    assert r["ok"] is True
    assert r["notes"] == []
    assert r["location"] is None
    assert r["program_day"] is None


def test_generate_saves_location_context_and_touches_last_used(tmp_db):
    home = _add_loc("Home", template="home_basic")
    assert home["last_used_at"] is None
    r = generate_workout(home["id"])
    assert r["location"] == {"id": home["id"], "name": "Home"}
    assert state_store.get_workout(r["workout_id"])["location_id"] == home["id"]
    assert state_store.get_workout_location(home["id"])["last_used_at"] is not None
    status = get_status()
    assert (status["location_id"], status["location"]) == (home["id"], "Home")


def test_generate_bad_location_id(tmp_db):
    before = metric_value(WORKOUT_GENERATED, result="bad_location")
    r = generate_workout(9999)
    assert r["ok"] is False
    assert "9999" in r["error"]
    assert state_store.get_todays_workout() is None
    assert metric_value(WORKOUT_GENERATED, result="bad_location") == before + 1


def test_generate_no_exercises_at_location(tmp_db):
    home = _add_loc("Home", template="empty")
    before = metric_value(WORKOUT_GENERATED, result="no_exercises")
    with mock.patch.object(workout_manager.state_store, "get_exercises", return_value=[]):
        r = workout_manager.generate_workout(home["id"])
    assert r["ok"] is False
    assert "Home" in r["error"]
    assert state_store.get_todays_workout() is None
    assert metric_value(WORKOUT_GENERATED, result="no_exercises") == before + 1


def test_generate_ok_counter(tmp_db):
    before = metric_value(WORKOUT_GENERATED, result="ok")
    assert generate_workout()["ok"] is True
    assert metric_value(WORKOUT_GENERATED, result="ok") == before + 1


# --- load caps ----------------------------------------------------------------


def _ex(name):
    ex = state_store.get_exercise(name)
    assert ex, name
    return ex


def _target(name, goal="general", limits=None, **kw):
    return _plan_target(_ex(name), goal, limits, **kw)


def test_dumbbell_cap_adds_reps(tmp_db):
    _seed_pr("Dumbbell Floor Press", 100.0)  # free-logged → hold 100
    t = _target("Dumbbell Floor Press", limits={"dumbbell_max_lbs": 50, "dumbbell_step_lbs": 5})
    assert (t["sets"], t["reps"], t["weight"], t["progression"]) == (3, 10, 50.0, "capped")
    assert t["note"] == "maxed out on this gear — go for extra reps"


def test_dumbbell_step_rounds_down(tmp_db):
    _seed_pr("Dumbbell Floor Press", 45.0)
    t = _target("Dumbbell Floor Press", limits={"dumbbell_step_lbs": 10})
    assert (t["reps"], t["weight"], t["progression"]) == (8, 40.0, "hold")


def test_dumbbell_step_is_floor(tmp_db):
    _seed_pr("Dumbbell Floor Press", 5.0)  # lighter than the lightest pair
    assert _target("Dumbbell Floor Press", limits={"dumbbell_step_lbs": 10})["weight"] == 10.0


def test_dumbbell_no_pr_stays_none(tmp_db):
    assert _cap_load(_ex("Dumbbell Floor Press"), None, {"dumbbell_max_lbs": 50}) == (None, False)


def test_no_limits_leaves_weight_alone(tmp_db):
    _seed_pr("Dumbbell Floor Press", 100.0)
    t = _target("Dumbbell Floor Press")
    assert (t["sets"], t["reps"], t["weight"]) == (3, 8, 100.0)


@pytest.mark.parametrize(
    "logged, bells, expected_weight, expected_capped",
    [
        (36.0, [18, 26, 35, 53], 35.0, False),  # heaviest owned bell ≤ 36
        (55.0, [18, 35], 35.0, True),  # heavier than every bell → capped
        (10.0, [26, 35], 26.0, False),  # lighter than every bell → lightest bell
    ],
)
def test_kettlebell_snaps_to_owned_bell(tmp_db, logged, bells, expected_weight, expected_capped):
    _seed_pr("Kettlebell Goblet Squat", logged)
    t = _target("Kettlebell Goblet Squat", limits={"kettlebell_weights_lbs": bells})
    assert t["weight"] == expected_weight
    assert (t["progression"] == "capped") is expected_capped
    assert t["reps"] == (10 if expected_capped else 8)


def test_single_kettlebell_auto_target(tmp_db):
    t = _target("Kettlebell Goblet Squat", limits={"kettlebell_weights_lbs": [35]})
    assert (t["weight"], t["progression"]) == (35.0, "new")


def test_multiple_kettlebells_no_history_lets_user_pick(tmp_db):
    assert _target("Kettlebell Goblet Squat", limits={"kettlebell_weights_lbs": [26, 35]})["weight"] is None


def test_capped_reps_never_exceed_max(tmp_db, monkeypatch):
    monkeypatch.setattr(workout_manager, "_REP_SCHEMES", {"general": {"compound": (3, 19), "isolation": (3, 19)}})
    _seed_pr("Dumbbell Floor Press", 100.0)
    t = _target("Dumbbell Floor Press", limits={"dumbbell_max_lbs": 50})
    assert (t["progression"], t["reps"]) == ("capped", 20)


def test_capped_exercise_entry_has_note_and_persists_targets(tmp_db):
    _seed_pr("Dumbbell Floor Press", 100.0)
    wid = state_store.create_workout("full_body", True)
    entry = _add_exercise_sets(wid, _ex("Dumbbell Floor Press"), "general", {"dumbbell_max_lbs": 50})
    assert entry["note"] == "maxed out on this gear — go for extra reps"
    assert entry["progression"] == "capped"
    planned = state_store.get_workout(wid)["sets"]
    assert [(s["target_reps"], s["target_weight_lbs"]) for s in planned] == [(10, 50.0)] * 3
    # The "why" is stored on set 1 only.
    assert [s["notes"] for s in planned] == [entry["note"], None, None]

    new = _add_exercise_sets(wid, _ex("Push-Up"), "general", {"dumbbell_max_lbs": 50})
    assert "note" not in new and new["progression"] == "new"


# --- progression ---------------------------------------------------------------


def test_progression_hit_lower_barbell_adds_ten(tmp_db):
    _session("Back Squat", 100.0, [8, 8, 8])
    t = _target("Back Squat")
    assert (t["weight"], t["reps"], t["progression"]) == (110.0, 8, "up")
    assert t["note"] == "+10 lb from last time"


def test_progression_hit_upper_barbell_adds_five(tmp_db):
    _session("Bench Press", 100.0, [8, 8, 8])
    assert _target("Bench Press")["weight"] == 105.0


def test_progression_dumbbell_uses_step(tmp_db):
    _session("Dumbbell Floor Press", 40.0, [8, 8, 8])
    assert _target("Dumbbell Floor Press")["weight"] == 45.0
    assert _target("Dumbbell Floor Press", limits={"dumbbell_step_lbs": 10})["weight"] == 50.0


def test_progression_extra_reps_still_count_as_hit(tmp_db):
    _session("Bench Press", 100.0, [10, 9, 8])
    assert _target("Bench Press")["progression"] == "up"


def test_progression_miss_holds(tmp_db):
    _session("Bench Press", 100.0, [8, 8, 6])
    t = _target("Bench Press")
    assert (t["weight"], t["reps"], t["progression"]) == (100.0, 8, "hold")
    assert t["note"] == "same as last time — finish every set to move up"


def test_progression_skipped_planned_set_is_a_miss(tmp_db):
    _session("Bench Press", 100.0, [8, 8, None])
    assert _target("Bench Press")["progression"] == "hold"


def test_progression_two_misses_deload_ten_percent(tmp_db):
    _session("Back Squat", 200.0, [8, 6, 5], days_ago=5)
    _session("Back Squat", 200.0, [8, 7, 6], days_ago=2)
    t = _target("Back Squat")
    assert (t["weight"], t["progression"]) == (180.0, "stall_deload")
    assert "stalled at 200 lb twice" in t["note"]


def test_progression_after_stall_deload_climbs_again(tmp_db):
    # Missed twice at 200, dropped to 180, hit it → next time 190, not another drop.
    _session("Back Squat", 200.0, [6, 6, 6], days_ago=9)
    _session("Back Squat", 200.0, [6, 6, 6], days_ago=6)
    _session("Back Squat", 180.0, [8, 8, 8], days_ago=2)
    t = _target("Back Squat")
    assert (t["weight"], t["progression"]) == (190.0, "up")


def test_progression_miss_after_hit_is_not_a_stall(tmp_db):
    _session("Back Squat", 200.0, [8, 8, 8], days_ago=5)
    _session("Back Squat", 210.0, [8, 6, 6], days_ago=2)
    assert _target("Back Squat")["weight"] == 210.0


def test_progression_grinder_holds(tmp_db):
    _session("Bench Press", 100.0, [8, 8, 8], rpe=10)
    t = _target("Bench Press")
    assert (t["weight"], t["progression"]) == (100.0, "hold")
    assert t["note"] == "holding — last time was a grinder"


def test_progression_comeback_after_three_weeks(tmp_db):
    _session("Bench Press", 100.0, [8, 8, 8], days_ago=30)
    t = _target("Bench Press")
    assert (t["weight"], t["progression"]) == (90.0, "comeback")
    assert "30 days" in t["note"]


def test_progression_bodyweight_adds_a_rep(tmp_db):
    _session("Push-Up", 0.0, [12, 12, 12], target_reps=12)
    t = _target("Push-Up")
    assert (t["weight"], t["reps"], t["progression"]) == (None, 13, "reps")


def test_progression_bodyweight_stops_at_rep_cap(tmp_db):
    _session("Push-Up", 0.0, [20, 20, 20], target_reps=20)
    t = _target("Push-Up")
    assert t["reps"] == 20
    assert "harder variation" in t["note"]


def test_progression_kettlebell_adds_reps_then_moves_up_a_bell(tmp_db):
    bells = {"kettlebell_weights_lbs": [26, 35]}
    _session("Kettlebell Goblet Squat", 26.0, [8, 8, 8], days_ago=4)
    t = _target("Kettlebell Goblet Squat", limits=bells)
    assert (t["weight"], t["reps"], t["progression"]) == (26.0, 9, "reps")

    _session("Kettlebell Goblet Squat", 26.0, [12, 12, 12], target_reps=12, days_ago=2)
    t = _target("Kettlebell Goblet Squat", limits=bells)
    assert (t["weight"], t["reps"], t["progression"]) == (35.0, 8, "up")
    assert t["note"] == "moving up to the 35 lb bell"


def test_progression_single_bell_keeps_adding_reps(tmp_db):
    _session("Kettlebell Goblet Squat", 35.0, [14, 14, 14], target_reps=14)
    t = _target("Kettlebell Goblet Squat", limits={"kettlebell_weights_lbs": [35]})
    assert (t["weight"], t["reps"]) == (35.0, 15)


def test_progression_home_session_does_not_drag_gym_numbers(tmp_db):
    gym = _add_loc("Gym A")
    home = _add_loc("Home", template="home_basic", limits={"dumbbell_max_lbs": 50})
    _session("Dumbbell Floor Press", 80.0, [8, 8, 8], days_ago=6, location_id=gym["id"])
    _session("Dumbbell Floor Press", 50.0, [10, 10, 10], target_reps=10, days_ago=2, location_id=home["id"])

    at_gym = _target("Dumbbell Floor Press", limits=gym["limits"])
    assert (at_gym["weight"], at_gym["progression"]) == (85.0, "up")

    # At home: still capped, but +1 rep over the last home session.
    at_home = _target("Dumbbell Floor Press", limits=home["limits"])
    assert (at_home["weight"], at_home["reps"], at_home["progression"]) == (50.0, 11, "capped")
    assert at_home["note"] == "maxed out on this gear — +1 rep from last time"


def test_progression_ignores_the_workout_being_built(tmp_db):
    wid = _session("Bench Press", 100.0, [8, 8, 8], days_ago=0)
    assert _target("Bench Press", exclude_workout_id=wid)["progression"] == "new"
    assert state_store.get_exercise_sessions("Bench Press")[0]["workout_id"] == wid


def test_progression_skips_deload_sessions(tmp_db):
    _session("Back Squat", 200.0, [8, 8, 8], days_ago=6)
    _session("Back Squat", 170.0, [5, 5], target_reps=8, days_ago=2, deload=True)
    assert _target("Back Squat")["weight"] == 210.0


def test_deload_week_target(tmp_db):
    _session("Back Squat", 200.0, [8, 8, 8])
    t = _target("Back Squat", deload=True)
    assert (t["sets"], t["reps"], t["weight"], t["progression"]) == (2, 8, 170.0, "deload_week")
    assert t["note"] == "deload week — lighter on purpose"


def test_deload_week_respects_gear(tmp_db):
    _session("Kettlebell Goblet Squat", 35.0, [8, 8, 8])
    t = _target("Kettlebell Goblet Squat", limits={"kettlebell_weights_lbs": [26, 35]}, deload=True)
    assert t["weight"] == 26.0  # 85% of 35 → heaviest bell under it


def test_progression_counter_increments(tmp_db):
    before = metric_value(WORKOUT_PROGRESSION, progression="new")
    _add_exercise_sets(state_store.create_workout("full_body", True), _ex("Push-Up"), "general", {})
    assert metric_value(WORKOUT_PROGRESSION, progression="new") == before + 1


# --- deload weeks ----------------------------------------------------------------


@pytest.mark.parametrize(
    "trained, is_deload, until",
    [(0, False, 12), (11, False, 1), (12, True, 0), (14, True, 0), (15, False, 12)],
)
def test_deload_status_cycle(tmp_db, monkeypatch, trained, is_deload, until):
    monkeypatch.setattr(state_store, "count_trained_program_sessions", lambda _pid: trained)
    status = workout_manager.deload_status({"id": 1, "days_per_week": 3, "deload_every_weeks": 4})
    assert (status["is_deload"], status["sessions_until_deload"]) == (is_deload, until)


def test_generate_workout_deload_week(tmp_db, monkeypatch):
    set_program("general", 3, 45, deload_every_weeks=4)
    monkeypatch.setattr(state_store, "count_trained_program_sessions", lambda _pid: 12)
    result = workout_manager.generate_workout()
    assert result["ok"] is True and result["deload"] is True
    assert "Deload week" in result["reasoning"]
    assert all(e["progression"] == "deload_week" for e in result["exercises"])
    assert all(len(e["sets"]) == 2 for e in result["exercises"])
    status = workout_manager.get_status()
    assert status["is_deload"] is True
    assert status["exercises"][0]["note"] == "deload week — lighter on purpose"

    # A swap on a deload day is planned as deload too.
    added = workout_manager.modify_workout(result["workout_id"], add_exercises=["Plank"])
    assert added["added_exercises"] == ["Plank"]
    plank = [s for s in state_store.get_workout(result["workout_id"])["sets"] if s["exercise_name"] == "Plank"]
    assert len(plank) == 2


def test_count_trained_program_sessions(tmp_db):
    pid = set_program("general", 3, 45)["program"]["id"]
    trained = state_store.create_workout("full_body", True)
    state_store.set_workout_context(trained, None, pid, 0)
    _complete_first_set_raw(trained)
    untouched = state_store.create_workout("full_body", True)
    state_store.set_workout_context(untouched, None, pid, 1)
    state_store.add_planned_set(untouched, "Bench Press", [], 1, 8, 100.0)
    assert state_store.count_trained_program_sessions(pid) == 1


def _complete_first_set_raw(workout_id):
    sid = state_store.add_planned_set(workout_id, "Bench Press", [], 1, 8, 100.0)
    state_store.log_completed_set(workout_id, "Bench Press", 100.0, 8, set_id=sid)


# --- modify_workout with locations -------------------------------------------


def _home_workout():
    home = _add_loc("Home", template="home_basic")
    r = generate_workout(home["id"])
    assert r["ok"] is True
    return home, r


def _unsupported_at_home():
    return next(e for e in state_store.get_exercises(movement_pattern="push_horizontal") if "bench" in e["requires"])


def test_modify_unsupported_swap_holds_removal(tmp_db):
    _, r = _home_workout()
    original = r["exercises"][0]["name"]
    bench_ex = _unsupported_at_home()
    sets_before = state_store.get_workout(r["workout_id"])["sets"]

    out = modify_workout(r["workout_id"], remove_exercises=[original], add_exercises=[bench_ex["name"]])

    assert out["ok"] is True
    assert out["removal_held"] is True
    assert out["removed_sets"] == 0
    assert out["added_exercises"] == []
    [skip] = out["skipped_exercises"]
    assert skip["name"] == bench_ex["name"]
    assert "bench" in skip["reason"] and "Home" in skip["reason"]
    home_push = {e["name"] for e in state_store.get_exercises("push_horizontal", available_equipment=_HOME_GEAR)}
    assert 1 <= len(skip["alternatives"]) <= 2
    assert set(skip["alternatives"]) <= home_push
    assert len(state_store.get_workout(r["workout_id"])["sets"]) == len(sets_before)


def test_modify_unknown_exercise_skipped_and_removal_held(tmp_db):
    _, r = _home_workout()
    original = r["exercises"][0]["name"]
    out = modify_workout(r["workout_id"], remove_exercises=[original], add_exercises=["Moon Squat"])
    assert out["removal_held"] is True
    assert out["skipped_exercises"] == [{"name": "Moon Squat", "reason": "not in the exercise catalog"}]


def test_modify_successful_swap(tmp_db):
    _, r = _home_workout()
    plan_names = {ex["name"] for ex in r["exercises"]}
    original = r["exercises"][0]
    replacement = next(
        e["name"] for e in state_store.get_exercises(available_equipment=_HOME_GEAR) if e["name"] not in plan_names
    )

    out = modify_workout(r["workout_id"], remove_exercises=[original["name"]], add_exercises=[replacement])

    assert out["removal_held"] is False
    assert out["removed_sets"] == len(original["sets"])
    assert out["added_exercises"] == [replacement]
    assert out["skipped_exercises"] == []
    names_after = {s["exercise_name"] for s in state_store.get_workout(r["workout_id"])["sets"]}
    assert original["name"] not in names_after
    assert replacement in names_after


def test_modify_partial_swap_still_removes(tmp_db):
    """One add fits, one doesn't → the swap goes ahead, the misfit is reported."""
    _, r = _home_workout()
    original = r["exercises"][0]
    plan_names = {ex["name"] for ex in r["exercises"]}
    replacement = next(
        e["name"] for e in state_store.get_exercises(available_equipment=_HOME_GEAR) if e["name"] not in plan_names
    )
    out = modify_workout(
        r["workout_id"],
        remove_exercises=[original["name"]],
        add_exercises=[_unsupported_at_home()["name"], replacement],
    )
    assert out["removal_held"] is False
    assert out["removed_sets"] == len(original["sets"])
    assert out["added_exercises"] == [replacement]
    assert len(out["skipped_exercises"]) == 1


def test_modify_uses_program_goal_only_for_program_workout(tmp_db):
    set_program("strength", 3, 20)
    r = generate_workout()
    plan_names = {ex["name"] for ex in r["exercises"]}
    compound = next(
        e["name"] for e in state_store.get_exercises(movement_pattern="hinge") if e["name"] not in plan_names
    )
    modify_workout(r["workout_id"], add_exercises=[compound])
    added = [s for s in state_store.get_workout(r["workout_id"])["sets"] if s["exercise_name"] == compound]
    assert [s["target_reps"] for s in added] == [5, 5, 5, 5]

    clear_program()
    other = state_store.create_workout("custom", False)
    modify_workout(other, add_exercises=[compound])
    added = state_store.get_workout(other)["sets"]
    assert [s["target_reps"] for s in added] == [8, 8, 8]


def test_deleted_location_leaves_todays_workout_readable(tmp_db):
    home, r = _home_workout()
    assert remove_location(home["id"])["ok"] is True

    status = get_status()
    assert status["has_workout"] is True
    assert status["workout_id"] == r["workout_id"]
    assert status["location_id"] is None
    assert status["location"] is None
    assert status["exercises"]
    # Dangling location → modify just stops enforcing that gear.
    out = modify_workout(r["workout_id"], add_exercises=[_unsupported_at_home()["name"]])
    assert out["added_exercises"]


def test_modify_workout_add_already_planned_is_skipped(tmp_db):
    """Adding an exercise that's already in the plan doesn't duplicate its sets."""
    result = workout_manager.generate_workout()
    name = result["exercises"][0]["name"]
    before = len(state_store.get_workout(result["workout_id"])["sets"])

    out = workout_manager.modify_workout(result["workout_id"], add_exercises=[name])

    assert out["added_exercises"] == []
    assert out["skipped_exercises"] == [{"name": name, "reason": "already in today's plan"}]
    assert len(state_store.get_workout(result["workout_id"])["sets"]) == before


# --- sticky program days (training blocks) ------------------------------------


def _names(result):
    return [e["name"] for e in result["exercises"]]


def test_program_day_repeats_its_exercises(tmp_db):
    set_program("general", 2, 45)
    first_a = generate_workout()
    _train_all(first_a["workout_id"])
    _train_all(generate_workout()["workout_id"])  # day B
    second_a = generate_workout()
    assert second_a["program_day"]["index"] == 0
    assert _names(second_a) == _names(first_a)
    assert "Same lifts as last time" in second_a["reasoning"]
    assert "rotate after the next deload week" in second_a["reasoning"]


def test_untrained_day_is_not_sticky(tmp_db):
    pid = set_program("general", 2, 45)["program"]["id"]
    wid = state_store.create_workout("full_body", True)
    state_store.set_workout_context(wid, None, pid, 0)
    state_store.add_planned_set(wid, "Bench Press", [], 1, 8, 100.0)
    assert state_store.last_program_day_exercises(pid, 0, None) == []


def test_last_program_day_exercises_prefers_same_location_and_respects_since(tmp_db):
    pid = set_program("general", 2, 45)["program"]["id"]
    gym = _add_loc("Gym A")
    home = _add_loc("Home", template="home_basic")

    def trained(location_id, names, deload=False):
        wid = state_store.create_workout("full_body", True)
        state_store.set_workout_context(wid, location_id, pid, 0, is_deload=deload)
        for i, n in enumerate(names, start=1):
            sid = state_store.add_planned_set(wid, n, [], i, 8, 50.0)
            state_store.log_completed_set(wid, n, 50.0, 8, set_id=sid)
        return state_store.get_workout(wid)["started_at"]

    trained(home["id"], ["Kettlebell Goblet Squat", "Dumbbell Floor Press"])
    gym_ts = trained(gym["id"], ["Back Squat", "Bench Press"])

    assert state_store.last_program_day_exercises(pid, 0, home["id"]) == [
        "Kettlebell Goblet Squat",
        "Dumbbell Floor Press",
    ]
    assert state_store.last_program_day_exercises(pid, 0, gym["id"]) == ["Back Squat", "Bench Press"]
    # A location never trained at falls back to the newest of any location.
    assert state_store.last_program_day_exercises(pid, 0, None) == ["Back Squat", "Bench Press"]
    # Nothing after the block boundary → a fresh pick.
    assert state_store.last_program_day_exercises(pid, 0, gym["id"], since=gym_ts) == []

    # Deload workouts are never the template, but they do mark the boundary.
    deload_ts = trained(gym["id"], ["Front Squat"], deload=True)
    assert state_store.last_program_day_exercises(pid, 0, gym["id"]) == ["Back Squat", "Bench Press"]
    assert state_store.last_deload_started_at(pid) == deload_ts


def test_block_exercises_rotate_after_deload_but_not_during(tmp_db, monkeypatch):
    calls = []
    monkeypatch.setattr(state_store, "last_deload_started_at", lambda pid: "2026-01-01T00:00:00")
    monkeypatch.setattr(
        state_store,
        "last_program_day_exercises",
        lambda pid, day, loc, since=None: calls.append(since) or [],
    )
    program = {"id": 1, "days_per_week": 2}
    workout_manager._block_exercises(program, 0, None, deload=False)
    workout_manager._block_exercises(program, 0, None, deload=True)
    assert calls == ["2026-01-01T00:00:00", None]


def test_sticky_exercise_skipped_when_gear_missing(tmp_db):
    home = {"dumbbells", "kettlebell"}
    picked, _ = _pick_for_slots(["squat"], home, sticky=["Back Squat"])
    assert picked and picked[0]["name"] != "Back Squat"
    picked, _ = _pick_for_slots(["squat"], None, sticky=["Back Squat"])
    assert picked[0]["name"] == "Back Squat"


# --- progression: code-review regressions -------------------------------------


def test_home_dumbbells_at_max_progress_reps_not_stuck(tmp_db):
    home = _add_loc("Home", template="home_basic", limits={"dumbbell_max_lbs": 50, "dumbbell_step_lbs": 5})
    _session("Dumbbell Floor Press", 45.0, [8, 8, 8], days_ago=6, location_id=home["id"])
    _session("Dumbbell Floor Press", 50.0, [8, 8, 8], days_ago=4, location_id=home["id"])
    _session("Dumbbell Floor Press", 50.0, [9, 9, 9], target_reps=9, days_ago=2, location_id=home["id"])
    t = _target("Dumbbell Floor Press", limits=home["limits"])
    assert (t["weight"], t["reps"], t["progression"]) == (50.0, 10, "capped")
    assert t["note"] == "maxed out on this gear — +1 rep from last time"


def test_top_bell_keeps_double_progression(tmp_db):
    home = _add_loc("Home", template="home_basic", limits={"kettlebell_weights_lbs": [26, 35]})
    _session("Kettlebell Goblet Squat", 26.0, [12, 12, 12], target_reps=12, days_ago=6, location_id=home["id"])
    _session("Kettlebell Goblet Squat", 35.0, [8, 8, 8], days_ago=4, location_id=home["id"])
    _session("Kettlebell Goblet Squat", 35.0, [9, 9, 9], target_reps=9, days_ago=2, location_id=home["id"])
    t = _target("Kettlebell Goblet Squat", limits=home["limits"])
    assert (t["weight"], t["reps"]) == (35.0, 10)


def test_kettlebell_never_goes_back_down_a_bell(tmp_db):
    home = _add_loc("Home", template="home_basic", limits={"kettlebell_weights_lbs": [26, 35]})
    _session("Kettlebell Goblet Squat", 26.0, [9, 9, 9], target_reps=9, days_ago=4, location_id=home["id"])
    _session("Kettlebell Goblet Squat", 35.0, [8, 8, 8], days_ago=2, location_id=home["id"])
    t = _target("Kettlebell Goblet Squat", limits=home["limits"])
    assert (t["weight"], t["reps"]) == (35.0, 9)


def test_extra_heavy_single_does_not_turn_a_hit_into_a_miss(tmp_db):
    wid = _session("Bench Press", 100.0, [8, 8, 8])
    state_store.log_completed_set(wid, "Bench Press", 110.0, 3)  # free-logged top single
    t = _target("Bench Press")
    assert (t["weight"], t["progression"]) == (105.0, "up")


def test_all_planned_sets_done_heavier_counts_from_the_heavier_weight(tmp_db):
    wid = state_store.create_workout("full_body", True)
    for i in (1, 2, 3):
        sid = state_store.add_planned_set(wid, "Bench Press", [], i, 8, 100.0)
        state_store.log_completed_set(wid, "Bench Press", 110.0, 8, set_id=sid)
    assert _target("Bench Press")["weight"] == 115.0


def test_goal_change_to_fewer_reps_keeps_weight(tmp_db):
    _session("Bench Press", 100.0, [10, 10, 7], target_reps=10)
    t = _target("Bench Press", goal="strength")
    assert (t["sets"], t["reps"], t["weight"], t["progression"]) == (4, 5, 100.0, "hold")
    assert t["note"] == "new rep range — same weight, fewer reps"


def test_goal_change_to_more_reps_rebases_weight(tmp_db):
    _session("Back Squat", 225.0, [5, 5, 5, 5], target_reps=5)
    t = _target("Back Squat", goal="muscle")
    assert (t["reps"], t["weight"], t["progression"]) == (10, 195.0, "hold")
    assert t["note"] == "new rep range — weight adjusted to match"


def test_deload_week_does_not_trigger_comeback(tmp_db):
    _session("Back Squat", 200.0, [8, 8, 8], days_ago=24)
    _session("Back Squat", 170.0, [8, 8], days_ago=10, deload=True)
    t = _target("Back Squat")
    assert (t["weight"], t["progression"]) == (210.0, "up")


def test_stall_streak_must_be_consecutive(tmp_db):
    _session("Back Squat", 200.0, [8, 6, 6], days_ago=6)
    _session("Back Squat", 190.0, [8, 8, 8], days_ago=4)
    _session("Back Squat", 200.0, [8, 6, 6], days_ago=2)
    t = _target("Back Squat")
    assert (t["weight"], t["progression"]) == (200.0, "hold")


def test_dumbbell_step_rounding_is_float_and_note_is_real_jump(tmp_db):
    # +10 step from 45 would be 55; pairs only exist in 10s from 10 → 50 (+5).
    _session("Dumbbell Floor Press", 45.0, [8, 8, 8])
    t = _target("Dumbbell Floor Press", limits={"dumbbell_step_lbs": 10})
    assert t["weight"] == 50.0 and isinstance(t["weight"], float)
    assert t["note"] == "+5 lb from last time"


def test_kettlebell_without_bell_list_jumps_four_kilos(tmp_db):
    _session("Kettlebell Goblet Squat", 35.0, [8, 8, 8])
    t = _target("Kettlebell Goblet Squat")
    assert (t["weight"], t["note"]) == (44.0, "+9 lb from last time")


def test_zero_weight_log_on_loaded_lift_is_ignored(tmp_db):
    _session("Bench Press", 100.0, [8, 8, 8], days_ago=4)
    _session("Bench Press", 0.0, [8, 8, 8], days_ago=2)
    assert _target("Bench Press")["weight"] == 105.0


# --- progression phase: gap fill ---------------------------------------------


def test_progression_planned_session_with_one_completed_set_is_a_miss(tmp_db):
    _session("Bench Press", 100.0, [8, None, None])
    t = _target("Bench Press")
    assert (t["weight"], t["reps"], t["progression"]) == (100.0, 8, "hold")
    assert t["note"] == "same as last time — finish every set to move up"


def test_progression_two_partial_sessions_at_same_weight_stall(tmp_db):
    _session("Bench Press", 100.0, [8, None, None], days_ago=5)
    _session("Bench Press", 100.0, [8, None, None], days_ago=2)
    t = _target("Bench Press")
    assert (t["weight"], t["progression"]) == (90.0, "stall_deload")


def test_progression_bodyweight_grinder_holds_reps(tmp_db):
    _session("Push-Up", 0.0, [12, 12, 12], target_reps=12, rpe=10)
    t = _target("Push-Up")
    assert (t["weight"], t["reps"], t["progression"]) == (None, 12, "hold")
    assert t["note"] == "holding — last time was a grinder"


def test_progression_bodyweight_miss_holds_reps(tmp_db):
    _session("Push-Up", 0.0, [12, 12, 9], target_reps=12)
    t = _target("Push-Up")
    assert (t["weight"], t["reps"], t["progression"]) == (None, 12, "hold")


def test_progression_adhoc_bodyweight_all_sets_at_scheme_is_a_hit(tmp_db):
    wid = state_store.create_workout("custom", False)
    state_store.log_completed_set(wid, "Push-Up", 0.0, 10)
    state_store.log_completed_set(wid, "Push-Up", 0.0, 9)
    t = _target("Push-Up")
    assert (t["weight"], t["progression"]) == (None, "reps")
    assert t["reps"] > 8 and t["note"] == "+1 rep from last time"


def test_progression_adhoc_bodyweight_short_set_holds_without_note(tmp_db):
    wid = state_store.create_workout("custom", False)
    state_store.log_completed_set(wid, "Push-Up", 0.0, 10)
    state_store.log_completed_set(wid, "Push-Up", 0.0, 5)
    t = _target("Push-Up")
    assert (t["weight"], t["reps"], t["progression"], t["note"]) == (None, 8, "hold", None)


def test_progression_bodyweight_comeback_has_no_weight(tmp_db):
    _session("Push-Up", 0.0, [12, 12, 12], target_reps=12, days_ago=25)
    t = _target("Push-Up")
    assert (t["weight"], t["progression"]) == (None, "comeback")
    assert "25 days" in t["note"]


def test_progression_corrupt_location_limits_do_not_crash(tmp_db):
    loc = state_store.create_workout_location("Garage", ["dumbbells"], {"dumbbell_max_lbs": 50})
    with state_store.get_db() as conn:
        conn.execute("UPDATE workout_locations SET limits = '{not json' WHERE id = ?", (loc["id"],))
    _session("Dumbbell Floor Press", 50.0, [8, 8, 8], location_id=loc["id"])
    t = _target("Dumbbell Floor Press")
    assert (t["weight"], t["progression"]) == (55.0, "up")


@pytest.mark.parametrize(
    "program", [{"deload_every_weeks": 0}, {}, {"deload_every_weeks": None}, {"deload_every_weeks": -2}]
)
def test_deload_status_missing_or_bad_cadence_is_disabled(tmp_db, program):
    assert deload_status({"id": 1, "days_per_week": 3, **program}) == {
        "enabled": False,
        "is_deload": False,
        "sessions_until_deload": None,
    }


def _progression_logs(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.INFO and "→" in r.getMessage()]


def test_stall_deload_is_logged(tmp_db, caplog):
    _session("Back Squat", 200.0, [8, 6, 5], days_ago=5)
    _session("Back Squat", 200.0, [8, 7, 6], days_ago=2)
    wid = state_store.create_workout("full_body", True)
    with caplog.at_level(logging.INFO, logger="orchestrator.workout_manager"):
        entry = _add_exercise_sets(wid, _ex("Back Squat"), "general", {})
    assert entry["progression"] == "stall_deload"
    assert _progression_logs(caplog) == ["[WORKOUT] Back Squat: stall_deload → 180.0 lb"]


def test_comeback_is_logged(tmp_db, caplog):
    _session("Bench Press", 100.0, [8, 8, 8], days_ago=30)
    wid = state_store.create_workout("full_body", True)
    with caplog.at_level(logging.INFO, logger="orchestrator.workout_manager"):
        _add_exercise_sets(wid, _ex("Bench Press"), "general", {})
    assert _progression_logs(caplog) == ["[WORKOUT] Bench Press: comeback → 90.0 lb"]


@pytest.mark.parametrize("reps, expected", [([8, 8, 6], "hold"), ([8, 8, 8], "up")])
def test_hold_and_up_are_not_logged(tmp_db, caplog, reps, expected):
    _session("Bench Press", 100.0, reps)
    wid = state_store.create_workout("full_body", True)
    with caplog.at_level(logging.INFO, logger="orchestrator.workout_manager"):
        entry = _add_exercise_sets(wid, _ex("Bench Press"), "general", {})
    assert entry["progression"] == expected
    assert _progression_logs(caplog) == []


def test_sticky_day_carries_progression_notes_to_status(tmp_db):
    set_program("general", 2, 45)
    first_a = generate_workout()
    _train_all(first_a["workout_id"])
    _backdate(first_a["workout_id"], 4)
    day_b = generate_workout()
    _train_all(day_b["workout_id"])
    _backdate(day_b["workout_id"], 2)

    second_a = generate_workout()
    assert _names(second_a) == _names(first_a)
    assert all(e["progression"] in ("up", "reps") for e in second_a["exercises"])
    assert all(e["note"].endswith("from last time") for e in second_a["exercises"])

    status = get_status()
    assert status["workout_id"] == second_a["workout_id"]
    assert status["is_deload"] is False
    assert {e["name"]: e["note"] for e in status["exercises"]} == {e["name"]: e["note"] for e in second_a["exercises"]}


def test_adhoc_bodyweight_reps_carry_forward(tmp_db):
    wid = state_store.create_workout("custom", False)
    for _ in range(3):
        state_store.log_completed_set(wid, "Push-Up", 0.0, 15)
    t = _target("Push-Up")
    assert (t["reps"], t["progression"]) == (16, "reps")


def test_adhoc_kettlebell_reps_drive_bell_move(tmp_db):
    wid = state_store.create_workout("custom", False)
    for _ in range(3):
        state_store.log_completed_set(wid, "Kettlebell Goblet Squat", 26.0, 12)
    t = _target("Kettlebell Goblet Squat", limits={"kettlebell_weights_lbs": [26, 35]})
    assert (t["weight"], t["progression"]) == (35.0, "up")


def test_gear_ceiling_ignores_malformed_bell_list(tmp_db):
    kb = _ex("Kettlebell Goblet Squat")
    assert _gear_ceiling(kb, {"kettlebell_weights_lbs": 35}) is None
    assert _gear_ceiling(kb, {"kettlebell_weights_lbs": ["x", True]}) is None
    assert _gear_ceiling(kb, {"kettlebell_weights_lbs": [26, 35]}) == 35.0
