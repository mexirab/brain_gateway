"""
Tests for workout_setup.py — workout location + program validation layer.

Backed by the tmp_db fixture (fresh SQLite per test, real exercise catalog).
"""

import json

import pytest

from orchestrator.workout_setup import add_location, set_program

# ---------------------------------------------------------------------------
# clean_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Home", "Home"),
        ("  Big   Gym  ", "Big Gym"),
        ("Ho\nme", "Ho me"),
        ("Hotel​Gym", "Hotel Gym"),  # zero-width space (Cf)
        ("Gym‮evil", "Gym evil"),  # bidi override (Cf)
        ("Gym Two", "Gym Two"),  # line separator (Zl)
        ("Gym Two", "Gym Two"),  # paragraph separator (Zp)
        ("Gym\x00Two", "Gym Two"),  # NUL (Cc)
        ("x" * 60, "x" * 60),
    ],
)
def test_clean_name_strips_invisible_and_controls(raw, expected):
    from orchestrator.workout_setup import clean_name

    assert clean_name(raw) == expected


@pytest.mark.parametrize("raw", [None, 5, ["Home"], "", "   ", "​​", "x" * 61])
def test_clean_name_rejects_empty_long_or_non_string(raw):
    from orchestrator.workout_setup import clean_name

    assert clean_name(raw) is None


# ---------------------------------------------------------------------------
# clean_equipment
# ---------------------------------------------------------------------------


def test_clean_equipment_normalizes_and_orders_by_vocabulary():
    from orchestrator.workout_setup import clean_equipment

    tokens, unknown = clean_equipment([" Kettlebell", "BARBELL", "dumbbells", "barbell"])
    assert tokens == ["barbell", "dumbbells", "kettlebell"]
    assert unknown == []


def test_clean_equipment_reports_unknown_tokens():
    from orchestrator.workout_setup import clean_equipment

    tokens, unknown = clean_equipment(["barbell", "rowing_machine", 5])
    assert tokens == ["barbell"]
    assert "rowing_machine" in unknown
    assert "5" in unknown


def test_clean_equipment_non_list_is_none():
    from orchestrator.workout_setup import clean_equipment

    assert clean_equipment("barbell") == (None, [])
    assert clean_equipment(None) == (None, [])


def test_clean_equipment_unknown_list_is_bounded():
    from orchestrator.workout_setup import clean_equipment

    _, unknown = clean_equipment([f"thing{i}" * 20 for i in range(10)])
    assert len(unknown) == 5
    assert all(len(u) <= 30 for u in unknown)


# ---------------------------------------------------------------------------
# merge_limits
# ---------------------------------------------------------------------------


def test_merge_limits_sets_and_merges():
    from orchestrator.workout_setup import merge_limits

    out, err = merge_limits(
        {"dumbbell_max_lbs": 50.0}, {"dumbbell_step_lbs": 5, "kettlebell_weights_lbs": [35, 18, 35]}
    )
    assert err is None
    assert out == {"dumbbell_max_lbs": 50.0, "dumbbell_step_lbs": 5.0, "kettlebell_weights_lbs": [18.0, 35.0]}


@pytest.mark.parametrize("clear_value", [None, "", []])
def test_merge_limits_clears_key(clear_value):
    from orchestrator.workout_setup import merge_limits

    out, err = merge_limits({"dumbbell_max_lbs": 50.0, "dumbbell_step_lbs": 5.0}, {"dumbbell_max_lbs": clear_value})
    assert err is None
    assert out == {"dumbbell_step_lbs": 5.0}


def test_merge_limits_does_not_mutate_existing():
    from orchestrator.workout_setup import merge_limits

    existing = {"dumbbell_max_lbs": 50.0}
    merge_limits(existing, {"dumbbell_max_lbs": 60})
    assert existing == {"dumbbell_max_lbs": 50.0}


@pytest.mark.parametrize(
    "patch",
    [
        "not a dict",
        {"barbell_max_lbs": 200},
        {"dumbbell_max_lbs": 0},
        {"dumbbell_max_lbs": 301},
        {"dumbbell_max_lbs": True},
        {"dumbbell_max_lbs": "heavy"},
        {"dumbbell_step_lbs": 0.1},
        {"dumbbell_step_lbs": 51},
        {"kettlebell_weights_lbs": 35},
        {"kettlebell_weights_lbs": [35, "x"]},
        {"kettlebell_weights_lbs": [0]},
        {"kettlebell_weights_lbs": list(range(1, 14))},
    ],
)
def test_merge_limits_rejects_invalid(patch):
    from orchestrator.workout_setup import merge_limits

    out, err = merge_limits({}, patch)
    assert out is None
    assert isinstance(err, str) and err


# ---------------------------------------------------------------------------
# Locations: add / find / update / remove
# ---------------------------------------------------------------------------


def test_add_location_from_template(tmp_db):
    from orchestrator.workout_setup import add_location

    r = add_location("Home", template="home_basic")
    assert r["ok"] is True
    assert r["location"]["name"] == "Home"
    assert r["location"]["equipment"] == ["dumbbells", "kettlebell"]
    assert r["location"]["limits"] == {}


def test_add_location_default_template_is_full_gym(tmp_db):
    from orchestrator.exercises_seed import EQUIPMENT_TOKENS
    from orchestrator.workout_setup import add_location

    r = add_location("Big Gym")
    assert r["location"]["equipment"] == list(EQUIPMENT_TOKENS)


def test_add_location_explicit_equipment_overrides_template(tmp_db):
    from orchestrator.workout_setup import add_location

    r = add_location("Hotel", template="commercial_gym", equipment=["dumbbells"], limits={"dumbbell_max_lbs": 50})
    assert r["ok"] is True
    assert r["location"]["equipment"] == ["dumbbells"]
    assert r["location"]["limits"] == {"dumbbell_max_lbs": 50.0}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": ""},
        {"name": "x" * 61},
        {"name": "Home", "template": "garage"},
        {"name": "Home", "equipment": ["rowing_machine"]},
        {"name": "Home", "equipment": "dumbbells"},
        {"name": "Home", "limits": {"bogus": 1}},
    ],
)
def test_add_location_validation_errors(tmp_db, kwargs):
    from orchestrator import state_store
    from orchestrator.workout_setup import add_location

    r = add_location(**kwargs)
    assert r["ok"] is False
    assert r["error"]
    assert state_store.list_workout_locations() == []


def test_add_location_duplicate_name_case_insensitive(tmp_db):
    from orchestrator.workout_setup import add_location

    assert add_location("Home")["ok"] is True
    r = add_location("HOME")
    assert r["ok"] is False
    assert "already exists" in r["error"]


def test_add_location_caps_count(tmp_db):
    from orchestrator.workout_setup import MAX_LOCATIONS, add_location

    for i in range(MAX_LOCATIONS):
        assert add_location(f"Place {i}", template="empty")["ok"] is True
    r = add_location("One Too Many", template="empty")
    assert r["ok"] is False
    assert str(MAX_LOCATIONS) in r["error"]


def test_add_location_writes_audit_row(tmp_db):
    from orchestrator import state_store
    from orchestrator.workout_setup import add_location

    add_location("Home", template="home_basic", changed_by="jess")
    with state_store.get_db() as conn:
        rows = conn.execute("SELECT panel, diff_json, changed_by FROM config_changes WHERE panel='workouts'").fetchall()
    assert len(rows) == 1
    assert rows[0]["changed_by"] == "jess"
    assert json.loads(rows[0]["diff_json"])["before"] is None


def test_find_location_exact_and_fuzzy(tmp_db):
    from orchestrator.workout_setup import add_location, find_location

    add_location("Home", template="home_basic")
    add_location("Big Gym")
    add_location("Hotel Gym", template="empty")

    assert find_location("home")[0]["name"] == "Home"
    assert find_location("  BIG gym! ")[0]["name"] == "Big Gym"
    assert find_location("at home")[0]["name"] == "Home"  # name inside query
    assert find_location("big")[0]["name"] == "Big Gym"  # query inside name
    match, all_locs = find_location("gym")  # ambiguous: two names contain it
    assert match is None
    assert len(all_locs) == 3
    assert find_location("")[0] is None
    assert find_location("garage")[0] is None


def test_find_location_exact_mode_disables_fuzzy(tmp_db):
    from orchestrator.workout_setup import add_location, find_location

    add_location("Big Gym")
    assert find_location("big", exact=True)[0] is None
    assert find_location("big gym", exact=True)[0]["name"] == "Big Gym"


def test_update_location_add_remove_equipment_and_limits(tmp_db):
    from orchestrator.workout_setup import add_location, update_location

    loc = add_location("Home", template="home_basic", limits={"dumbbell_max_lbs": 50})["location"]
    r = update_location(
        loc["id"],
        add_equipment=["bench", "pull_up_bar"],
        remove_equipment=["kettlebell"],
        limits={"dumbbell_step_lbs": 5},
    )
    assert r["ok"] is True
    assert r["location"]["equipment"] == ["bench", "dumbbells", "pull_up_bar"]
    assert r["location"]["limits"] == {"dumbbell_max_lbs": 50.0, "dumbbell_step_lbs": 5.0}


def test_update_location_rename(tmp_db):
    from orchestrator.workout_setup import add_location, update_location

    loc = add_location("Home")["location"]
    r = update_location(loc["id"], name="  Garage  ")
    assert r["ok"] is True
    assert r["location"]["name"] == "Garage"


def test_update_location_rename_to_existing_name_fails(tmp_db):
    from orchestrator.workout_setup import add_location, update_location

    add_location("Home")
    other = add_location("Hotel")["location"]
    r = update_location(other["id"], name="home")
    assert r["ok"] is False
    assert "already exists" in r["error"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": ""},
        {"equipment": ["teleporter"]},
        {"add_equipment": ["teleporter"]},
        {"remove_equipment": "bench"},
        {"limits": {"dumbbell_max_lbs": -1}},
    ],
)
def test_update_location_validation_errors_leave_row_untouched(tmp_db, kwargs):
    from orchestrator import state_store
    from orchestrator.workout_setup import add_location, update_location

    loc = add_location("Home", template="home_basic")["location"]
    r = update_location(loc["id"], **kwargs)
    assert r["ok"] is False
    after = state_store.get_workout_location(loc["id"])
    assert after["name"] == "Home"
    assert after["equipment"] == ["dumbbells", "kettlebell"]


def test_update_location_unknown_id(tmp_db):
    from orchestrator.workout_setup import update_location

    assert update_location(9999, name="X") == {"ok": False, "error": "location not found"}


def test_remove_location(tmp_db):
    from orchestrator import state_store
    from orchestrator.workout_setup import add_location, remove_location

    loc = add_location("Home")["location"]
    assert remove_location(loc["id"]) == {"ok": True, "removed": "Home"}
    assert state_store.get_workout_location(loc["id"]) is None
    assert remove_location(loc["id"]) == {"ok": False, "error": "location not found"}


# ---------------------------------------------------------------------------
# Program
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "goal, days, minutes",
    [
        ("cardio", 3, 45),
        ("strength", 1, 45),
        ("strength", 7, 45),
        ("strength", 3, 19),
        ("strength", 3, 121),
        ("strength", "three", 45),
        ("strength", None, 45),
    ],
)
def test_set_program_validation(tmp_db, goal, days, minutes):
    from orchestrator import state_store
    from orchestrator.workout_setup import set_program

    r = set_program(goal, days, minutes)
    assert r["ok"] is False
    assert r["error"]
    assert state_store.get_active_program() is None


def test_set_program_normalizes_goal_and_defaults_general(tmp_db):
    from orchestrator.workout_setup import set_program

    assert set_program(" Strength ", 3, 45)["program"]["goal"] == "strength"
    assert set_program(None, 4, 45)["program"]["goal"] == "general"


def test_set_program_identical_is_unchanged(tmp_db):
    from orchestrator.workout_setup import set_program

    first = set_program("muscle", 4, 60)
    again = set_program("muscle", 4, 60)
    assert again["ok"] is True
    assert again.get("unchanged") is True
    assert again["program"]["id"] == first["program"]["id"]


def test_set_program_same_days_updates_in_place(tmp_db):
    from orchestrator import state_store
    from orchestrator.workout_setup import set_program

    first = set_program("muscle", 4, 60)["program"]
    r = set_program("strength", 4, 90)
    assert r["ok"] is True
    assert "unchanged" not in r
    assert r["program"]["id"] == first["id"]
    active = state_store.get_active_program()
    assert (active["id"], active["goal"], active["session_minutes"]) == (first["id"], "strength", 90)


def test_set_program_different_days_creates_new_row(tmp_db):
    from orchestrator import state_store
    from orchestrator.workout_setup import set_program

    first = set_program("muscle", 4, 60)["program"]
    second = set_program("muscle", 5, 60)["program"]
    assert second["id"] != first["id"]
    assert state_store.get_active_program()["id"] == second["id"]
    with state_store.get_db() as conn:
        n_active = conn.execute("SELECT COUNT(*) AS n FROM workout_programs WHERE active = 1").fetchone()["n"]
    assert n_active == 1


def test_clear_program(tmp_db):
    from orchestrator import state_store
    from orchestrator.workout_setup import clear_program, set_program

    assert clear_program() == {"ok": False, "error": "no active program"}
    set_program("general", 3, 45)
    assert clear_program() == {"ok": True}
    assert state_store.get_active_program() is None


@pytest.mark.parametrize("weeks", [0, 3, 6, 12, "8"])
def test_set_program_deload_valid(tmp_db, weeks):
    from orchestrator.workout_setup import set_program

    r = set_program("general", 3, 45, deload_every_weeks=weeks)
    assert r["ok"] is True and r["program"]["deload_every_weeks"] == int(weeks)


@pytest.mark.parametrize("weeks", [1, 2, 13, -1, "x", True, 4.5j, float("inf"), 6.5])
def test_set_program_deload_invalid(tmp_db, weeks):
    from orchestrator import state_store
    from orchestrator.workout_setup import set_program

    r = set_program("general", 3, 45, deload_every_weeks=weeks)
    assert r["ok"] is False and "deload_every_weeks" in r["error"]
    assert state_store.get_active_program() is None


def test_set_program_deload_defaults_and_is_kept(tmp_db):
    from orchestrator.workout_setup import set_program

    assert set_program("general", 3, 45)["program"]["deload_every_weeks"] == 6
    pid = set_program("general", 3, 45, deload_every_weeks=4)["program"]["id"]
    # Omitted → keep the current value; changing only the deload updates in place.
    r = set_program("muscle", 3, 45)
    assert (r["program"]["id"], r["program"]["deload_every_weeks"]) == (pid, 4)
    assert set_program("muscle", 3, 45, deload_every_weeks=4).get("unchanged") is True
    # A new split (new row) carries the setting over.
    assert set_program("muscle", 4, 45)["program"]["deload_every_weeks"] == 4


# ---------------------------------------------------------------------------
# Tool handlers: generate_workout (location arg) + workout_setup
# ---------------------------------------------------------------------------


@pytest.fixture
def workouts_on(tmp_db, monkeypatch):
    from orchestrator import shared

    monkeypatch.setattr(shared, "WORKOUTS_ENABLED", True)
    return tmp_db


async def _gen(args):
    from orchestrator import tool_handlers

    return await tool_handlers._reg_generate_workout(args)


async def _setup(args):
    from orchestrator import tool_handlers

    return await tool_handlers._reg_workout_setup(args)


async def test_handlers_self_gate_when_disabled(tmp_db, monkeypatch):
    from orchestrator import shared, state_store

    monkeypatch.setattr(shared, "WORKOUTS_ENABLED", False)
    assert "isn't enabled" in await _gen({})
    assert "isn't enabled" in await _setup({"action": "add_location", "name": "Home"})
    assert state_store.get_todays_workout() is None
    assert state_store.list_workout_locations() == []


async def test_generate_handler_no_locations_generates(workouts_on):
    from orchestrator import state_store

    out = await _gen({})
    assert out.startswith("Workout ready")
    assert "Location:" not in out
    assert state_store.get_todays_workout() is not None


async def test_generate_handler_single_location_is_used(workouts_on):
    from orchestrator import state_store

    add_location("Home", template="home_basic")
    out = await _gen({})
    assert "Location: Home" in out
    assert state_store.get_todays_workout()["location_id"] is not None


async def test_generate_handler_asks_where_with_two_locations(workouts_on):
    from orchestrator import state_store
    from orchestrator.metrics import WORKOUT_GENERATED

    add_location("Home", template="home_basic")
    add_location("Big Gym")
    before = WORKOUT_GENERATED.labels(result="ask_location")._value.get()

    out = await _gen({})

    assert out.startswith("Not generated yet — ask the user where")
    assert "Home" in out and "Big Gym" in out
    assert "last time" not in out
    assert state_store.get_todays_workout() is None
    assert WORKOUT_GENERATED.labels(result="ask_location")._value.get() == before + 1


async def test_generate_handler_ask_mentions_last_used(workouts_on):
    from orchestrator import state_store

    add_location("Home", template="home_basic")
    gym = add_location("Big Gym")["location"]
    state_store.touch_workout_location(gym["id"])
    out = await _gen({})
    assert "(last time: Big Gym)" in out
    assert state_store.get_todays_workout() is None


async def test_generate_handler_named_location(workouts_on):
    from orchestrator import state_store

    add_location("Home", template="home_basic")
    gym = add_location("Big Gym")["location"]
    out = await _gen({"location": "the big gym"})
    assert "Location: Big Gym" in out
    assert state_store.get_todays_workout()["location_id"] == gym["id"]


async def test_generate_handler_unknown_location_lists_known(workouts_on):
    from orchestrator import state_store

    add_location("Home", template="home_basic")
    add_location("Big Gym")
    out = await _gen({"location": "Hotel"})
    assert out.startswith("Not generated: unknown location 'Hotel'")
    assert "Home" in out and "Big Gym" in out
    assert state_store.get_todays_workout() is None


async def test_generate_handler_name_but_no_locations(workouts_on):
    from orchestrator import state_store

    out = await _gen({"location": "Hotel"})
    assert out.startswith("Not generated: no workout locations")
    assert state_store.get_todays_workout() is None


async def test_generate_handler_shows_program_day(workouts_on):
    from orchestrator import state_store

    set_program("general", 3, 20)
    add_location("Home", template="home_basic")
    out = await _gen({})
    assert "Program day: Full Body A (1 of 3)" in out
    assert state_store.get_todays_workout() is not None


async def test_setup_handler_get(workouts_on):
    add_location("Home", template="home_basic")
    data = json.loads(await _setup({"action": "get"}))
    assert data["program"] is None
    assert data["locations"] == [{"name": "Home", "equipment": ["dumbbells", "kettlebell"], "limits": {}}]
    assert "barbell" in data["equipment_vocabulary"]
    assert "home_basic" in data["templates"]

    set_program("muscle", 4, 60)
    data = json.loads(await _setup({}))  # default action is get
    assert data["program"]["split"] == ["Upper", "Lower", "Upper", "Lower"]
    assert data["program"]["next_day"] == "Upper"


async def test_setup_handler_set_and_clear_program(workouts_on):
    from orchestrator import state_store

    out = await _setup({"action": "set_program", "goal": "strength", "days_per_week": 5, "session_minutes": 45})
    assert out.startswith("Program saved: strength, 5 days/week, 45 min (~5 exercises)")
    assert "Upper / Lower / Push / Pull / Legs" in out

    bad = json.loads(await _setup({"action": "set_program", "days_per_week": 9, "session_minutes": 45}))
    assert bad["ok"] is False
    assert state_store.get_active_program()["days_per_week"] == 5

    assert "Program cleared" in await _setup({"action": "clear_program"})
    assert json.loads(await _setup({"action": "clear_program"}))["ok"] is False


async def test_setup_handler_add_location_uses_template_only(workouts_on):
    out = await _setup({"action": "add_location", "name": "Hotel", "template": "empty", "equipment": ["barbell"]})
    assert out == "Location added: Hotel — equipment: none (bodyweight only)."
    dup = json.loads(await _setup({"action": "add_location", "name": "hotel"}))
    assert dup["ok"] is False


async def test_setup_handler_update_location_fuzzy(workouts_on):
    from orchestrator import state_store

    add_location("Home", template="home_basic")
    out = await _setup(
        {
            "action": "update_location",
            "name": "at home",
            "add_equipment": ["bench"],
            "limits": {"dumbbell_max_lbs": 50},
        }
    )
    assert out.startswith("Updated Home")
    loc = state_store.list_workout_locations()[0]
    assert "bench" in loc["equipment"]
    assert loc["limits"] == {"dumbbell_max_lbs": 50.0}


async def test_setup_handler_remove_requires_exact_name(workouts_on):
    from orchestrator import state_store

    add_location("Big Gym")
    fuzzy = json.loads(await _setup({"action": "remove_location", "name": "big"}))
    assert fuzzy == {"ok": False, "error": "location not found", "known_locations": ["Big Gym"]}
    assert len(state_store.list_workout_locations()) == 1

    assert await _setup({"action": "remove_location", "name": "BIG GYM"}) == "Removed location Big Gym."
    assert state_store.list_workout_locations() == []


async def test_setup_handler_unknown_action(workouts_on):
    out = json.loads(await _setup({"action": "drop_tables"}))
    assert out == {"ok": False, "error": "unknown action 'drop_tables'"}


def _workout_setup_schema():
    from orchestrator.tool_definitions import STATIC_TOOLS

    return next(t for t in STATIC_TOOLS if t["function"]["name"] == "workout_setup")["function"]["parameters"]


def test_workout_setup_schema_enums_match_code():
    """The LLM-facing enums must not drift from what workout_setup validates."""
    from orchestrator.exercises_seed import LOCATION_TEMPLATES
    from orchestrator.workout_setup import GOALS

    props = _workout_setup_schema()["properties"]
    assert set(props["goal"]["enum"]) == set(GOALS)
    assert set(props["template"]["enum"]) == set(LOCATION_TEMPLATES)


async def test_every_schema_action_is_handled(workouts_on):
    for action in _workout_setup_schema()["properties"]["action"]["enum"]:
        out = await _setup({"action": action})
        assert "unknown action" not in out, action


async def test_setup_handler_set_program_keyword_args(workouts_on):
    """Regression: changed_by must not land in the deload_every_weeks slot."""
    from orchestrator import state_store

    out = await _setup({"action": "set_program", "goal": "strength", "days_per_week": 4, "session_minutes": 60})
    assert out.startswith("Program saved"), out
    assert "Deload week every 6 weeks" in out
    with state_store.get_db() as conn:
        row = conn.execute("SELECT changed_by FROM config_changes ORDER BY id DESC LIMIT 1").fetchone()
    assert row["changed_by"] == "jess"

    out = await _setup(
        {
            "action": "set_program",
            "goal": "strength",
            "days_per_week": 4,
            "session_minutes": 60,
            "deload_every_weeks": 0,
        }
    )
    assert "No scheduled deload weeks" in out
    assert state_store.get_active_program()["deload_every_weeks"] == 0


def test_set_program_rejects_infinite_numbers(tmp_db):
    from orchestrator.workout_setup import set_program

    assert set_program("general", float("inf"), 45)["ok"] is False
    assert set_program("general", 3, float("inf"))["ok"] is False


def _train_and_backdate(workout_id, days):
    from datetime import datetime, timedelta

    from orchestrator import state_store

    for s in state_store.get_workout(workout_id)["sets"]:
        state_store.log_completed_set(
            workout_id, s["exercise_name"], s["target_weight_lbs"] or 20.0, s["target_reps"], set_id=s["id"]
        )
    when = (datetime.now() - timedelta(days=days)).isoformat()
    with state_store.get_db() as conn:
        conn.execute("UPDATE workouts SET started_at = ? WHERE id = ?", (when, workout_id))


async def test_generate_handler_lists_per_exercise_progression_note(workouts_on):
    from orchestrator import workout_manager

    set_program("general", 2, 20)
    first = workout_manager.generate_workout()
    _train_and_backdate(first["workout_id"], 4)
    _train_and_backdate(workout_manager.generate_workout()["workout_id"], 2)

    out = await _gen({})
    plan = [line for line in out.splitlines() if line.startswith("  - ")]
    assert len(plan) == len(first["exercises"])
    assert all(" — " in line and line.endswith("from last time") for line in plan), plan
    assert "Same lifts as last time" in out


async def test_generate_handler_deload_week_text(workouts_on, monkeypatch):
    from orchestrator import state_store

    set_program("general", 3, 20, deload_every_weeks=4)
    monkeypatch.setattr(state_store, "count_trained_program_sessions", lambda _pid: 12)
    out = await _gen({})
    assert "Deload week: fewer sets, lighter weights, on purpose." in out
    plan = [line for line in out.splitlines() if line.startswith("  - ")]
    assert plan and all(": 2x" in line and line.endswith("— deload week — lighter on purpose") for line in plan), plan
