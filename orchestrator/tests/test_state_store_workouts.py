"""
Tests for state_store workout and meal functions.

Uses the tmp_db fixture from conftest to get a fresh isolated SQLite DB per test.
"""


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


# ---------------------------------------------------------------------------
# Program builder: catalog requires, upsert, migration, locations, programs
# ---------------------------------------------------------------------------


def test_seed_catalog_is_unchanged_on_reseed(tmp_db):
    """init_db already seeded the real catalog; seeding it again changes nothing."""
    from orchestrator import state_store
    from orchestrator.exercises_seed import EXERCISES

    assert state_store.seed_exercises(EXERCISES) == 0


def test_seed_upsert_updates_requires(tmp_db):
    from orchestrator import state_store

    ex = {**_BENCH_EX, "name": "ZZZ_Upsert_Bench", "requires": ["barbell"]}
    assert state_store.seed_exercises([ex]) == 1
    assert state_store.get_exercise("ZZZ_Upsert_Bench")["requires"] == ["barbell"]

    fixed = {**ex, "requires": ["barbell", "bench"]}
    assert state_store.seed_exercises([fixed]) == 1
    assert state_store.get_exercise("ZZZ_Upsert_Bench")["requires"] == ["barbell", "bench"]
    assert state_store.seed_exercises([fixed]) == 0


def test_seed_derives_requires_from_equipment(tmp_db):
    from orchestrator import state_store

    state_store.seed_exercises(
        [
            {**_BENCH_EX, "name": "ZZZ_Db", "equipment": "dumbbell"},
            {**_BENCH_EX, "name": "ZZZ_Bw", "equipment": "bodyweight"},
            {**_BENCH_EX, "name": "ZZZ_Machine", "equipment": "machine"},
        ]
    )
    assert state_store.get_exercise("ZZZ_Db")["requires"] == ["dumbbells"]
    assert state_store.get_exercise("ZZZ_Bw")["requires"] == []
    assert state_store.get_exercise("ZZZ_Machine")["requires"] == ["machines"]


def test_catalog_requires_only_use_known_tokens():
    from orchestrator.exercises_seed import EQUIPMENT_TOKENS, EXERCISES, FALLBACK_VARIANTS, exercise_requires

    names = {ex["name"] for ex in EXERCISES}
    assert len(names) == len(EXERCISES), "duplicate exercise names in seed"
    for ex in EXERCISES:
        assert set(exercise_requires(ex)) <= set(EQUIPMENT_TOKENS), ex["name"]
    assert names >= FALLBACK_VARIANTS


def test_get_exercises_available_equipment_filter(tmp_db):
    from orchestrator import state_store

    everything = state_store.get_exercises()
    home = state_store.get_exercises(available_equipment={"dumbbells", "kettlebell"})
    bare = state_store.get_exercises(available_equipment=set())
    assert len(bare) < len(home) < len(everything)
    assert all(set(e["requires"]) <= {"dumbbells", "kettlebell"} for e in home)
    assert all(e["requires"] == [] for e in bare)
    assert [e["name"] for e in everything] == sorted(e["name"] for e in everything)


def _old_schema_db(path):
    import sqlite3

    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE exercises (
            name TEXT PRIMARY KEY,
            primary_muscle TEXT NOT NULL,
            secondary_muscles TEXT NOT NULL DEFAULT '[]',
            equipment TEXT NOT NULL DEFAULT 'barbell',
            is_compound INTEGER NOT NULL DEFAULT 1,
            movement_pattern TEXT NOT NULL DEFAULT 'other'
        );
        CREATE TABLE workouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            workout_type TEXT NOT NULL DEFAULT 'full_body',
            generated_by_jess INTEGER NOT NULL DEFAULT 0,
            reasoning TEXT,
            notes TEXT
        );
        CREATE TABLE workout_sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workout_id INTEGER NOT NULL REFERENCES workouts(id) ON DELETE CASCADE,
            exercise_name TEXT NOT NULL,
            muscle_groups TEXT NOT NULL DEFAULT '[]',
            set_number INTEGER NOT NULL,
            target_reps INTEGER,
            target_weight_lbs REAL,
            weight_lbs REAL,
            reps INTEGER,
            rpe REAL,
            completed INTEGER NOT NULL DEFAULT 0,
            completed_at TEXT,
            notes TEXT
        );
        INSERT INTO exercises (name, primary_muscle, equipment, movement_pattern)
            VALUES ('Back Squat', 'quads', 'barbell', 'squat');
        INSERT INTO exercises (name, primary_muscle, equipment, movement_pattern)
            VALUES ('ZZZ_Legacy_Only', 'core', 'bodyweight', 'core');
        INSERT INTO workouts (started_at, workout_type, generated_by_jess, reasoning)
            VALUES ('2026-01-05T10:00:00', 'push_day', 1, 'legacy row');
        INSERT INTO workout_sets (workout_id, exercise_name, set_number, weight_lbs, reps, completed)
            VALUES (1, 'Back Squat', 1, 135, 5, 1);
        """
    )
    conn.commit()
    conn.close()


def test_init_db_migrates_pre_program_schema(tmp_path, monkeypatch):
    import sqlite3

    from orchestrator import state_store

    path = str(tmp_path / "old.db")
    _old_schema_db(path)
    monkeypatch.setattr(state_store, "DB_PATH", path)

    state_store.init_db()

    conn = sqlite3.connect(path)
    ex_cols = {r[1] for r in conn.execute("PRAGMA table_info(exercises)")}
    w_cols = {r[1] for r in conn.execute("PRAGMA table_info(workouts)")}
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "requires" in ex_cols
    assert {"location_id", "program_id", "program_day", "is_deload"} <= w_cols
    assert {"workout_locations", "workout_programs"} <= tables

    # Existing rows survive; the catalog row gets its requires from the upsert,
    # a row the seed doesn't know keeps the column default.
    w = state_store.get_workout(1)
    assert w["reasoning"] == "legacy row"
    assert w["location_id"] is None and w["program_id"] is None and w["program_day"] is None
    assert w["is_deload"] == 0
    assert len(w["sets"]) == 1
    assert state_store.get_exercise("Back Squat")["requires"] == ["barbell", "squat_rack"]
    assert state_store.get_exercise("ZZZ_Legacy_Only")["requires"] == []

    # Idempotent: a second init on the migrated DB is a no-op.
    state_store.init_db()
    assert state_store.get_workout(1)["reasoning"] == "legacy row"


def test_location_crud_and_ordering(tmp_db):
    import sqlite3

    import pytest

    from orchestrator import state_store

    a = state_store.create_workout_location("Home", ["dumbbells"], {"dumbbell_max_lbs": 50})
    b = state_store.create_workout_location("Big Gym", ["barbell"], {})
    c = state_store.create_workout_location("Hotel", [], {})
    assert a["equipment"] == ["dumbbells"] and a["limits"] == {"dumbbell_max_lbs": 50}

    with pytest.raises(sqlite3.IntegrityError):
        state_store.create_workout_location("home", [], {})

    # Never-used sorted by name; then most recently used first.
    assert [loc["name"] for loc in state_store.list_workout_locations()] == ["Big Gym", "Home", "Hotel"]
    state_store.touch_workout_location(c["id"])
    assert [loc["name"] for loc in state_store.list_workout_locations()] == ["Hotel", "Big Gym", "Home"]

    upd = state_store.update_workout_location(b["id"], equipment=["barbell", "bench"])
    assert upd["name"] == "Big Gym" and upd["equipment"] == ["barbell", "bench"]
    assert state_store.update_workout_location(9999, name="X") is None

    assert state_store.delete_workout_location(a["id"]) is True
    assert state_store.delete_workout_location(a["id"]) is False
    assert state_store.get_workout_location(a["id"]) is None


def test_location_bad_json_columns_deserialize_safely(tmp_db):
    from orchestrator import state_store

    loc = state_store.create_workout_location("Home", [], {})
    with state_store.get_db() as conn:
        conn.execute("UPDATE workout_locations SET equipment='nope', limits='{' WHERE id=?", (loc["id"],))
    got = state_store.get_workout_location(loc["id"])
    assert got["equipment"] == [] and got["limits"] == {}


def test_active_program_lifecycle(tmp_db):
    from orchestrator import state_store

    assert state_store.get_active_program() is None
    p1 = state_store.set_active_program("general", 3, 45)
    p2 = state_store.set_active_program("strength", 4, 60)
    assert state_store.get_active_program()["id"] == p2["id"]
    with state_store.get_db() as conn:
        row = conn.execute("SELECT active FROM workout_programs WHERE id=?", (p1["id"],)).fetchone()
    assert row["active"] == 0  # kept for history

    upd = state_store.update_active_program(p2["id"], "muscle", 90)
    assert (upd["id"], upd["goal"], upd["session_minutes"], upd["days_per_week"]) == (p2["id"], "muscle", 90, 4)

    assert state_store.clear_active_program() is True
    assert state_store.get_active_program() is None
    assert state_store.clear_active_program() is False


def test_last_trained_program_day_requires_completed_set(tmp_db):
    from orchestrator import state_store

    p = state_store.set_active_program("general", 3, 45)
    assert state_store.last_trained_program_day(p["id"]) is None

    w0 = state_store.create_workout("full_body", True)
    state_store.set_workout_context(w0, None, p["id"], 0)
    state_store.add_planned_set(w0, "Push-Up", ["chest"], 1, 10, None)
    assert state_store.last_trained_program_day(p["id"]) is None  # planned only

    state_store.log_completed_set(w0, "Push-Up", 0.0, 10)
    assert state_store.last_trained_program_day(p["id"]) == 0

    w1 = state_store.create_workout("full_body", True)
    state_store.set_workout_context(w1, 7, p["id"], 1)
    assert state_store.last_trained_program_day(p["id"]) == 0  # newer but untouched
    w = state_store.get_workout(w1)
    assert (w["location_id"], w["program_id"], w["program_day"]) == (7, p["id"], 1)


# ---------------------------------------------------------------------------
# Progression phase: exercise session history + deload migration
# ---------------------------------------------------------------------------


def _past_session(exercise, reps, days_ago, location_id=None, is_deload=False, weight=100.0):
    from datetime import datetime, timedelta

    from orchestrator import state_store

    wid = state_store.create_workout("full_body", True)
    when = (datetime.now() - timedelta(days=days_ago)).isoformat()
    with state_store.get_db() as conn:
        conn.execute("UPDATE workouts SET started_at = ? WHERE id = ?", (when, wid))
    state_store.set_workout_context(wid, location_id, None, None, is_deload=is_deload)
    for i, r in enumerate(reps, start=1):
        sid = state_store.add_planned_set(wid, exercise, [], i, 8, weight)
        if r is not None:
            state_store.log_completed_set(wid, exercise, weight, r, set_id=sid)
    return wid


def test_get_exercise_sessions_corrupt_location_limits(tmp_db):
    from orchestrator import state_store

    bad = state_store.create_workout_location("Bad", [], {"dumbbell_max_lbs": 50})
    listy = state_store.create_workout_location("Listy", [], {})
    good = state_store.create_workout_location("Good", [], {"dumbbell_max_lbs": 40})
    with state_store.get_db() as conn:
        conn.execute("UPDATE workout_locations SET limits = '{oops' WHERE id = ?", (bad["id"],))
        conn.execute("UPDATE workout_locations SET limits = '[1, 2]' WHERE id = ?", (listy["id"],))
    w_bad = _past_session("Bench Press", [8], 3, location_id=bad["id"])
    w_list = _past_session("Bench Press", [8], 2, location_id=listy["id"])
    w_good = _past_session("Bench Press", [8], 1, location_id=good["id"])
    w_none = _past_session("Bench Press", [8], 4, location_id=9999)  # location row gone

    sessions = state_store.get_exercise_sessions("Bench Press", limit=10)
    assert [s["workout_id"] for s in sessions] == [w_good, w_list, w_bad, w_none]
    assert [s["location_limits"] for s in sessions] == [{"dumbbell_max_lbs": 40}, {}, {}, {}]


def test_get_exercise_sessions_limit_exclusions_and_rows(tmp_db):
    from orchestrator import state_store

    oldest = _past_session("Bench Press", [8, 8], 6)
    middle = _past_session("Bench Press", [8, None], 4)
    newest = _past_session("Bench Press", [8, 8], 2)
    _past_session("Bench Press", [8, 8], 1, is_deload=True)  # deload: excluded
    _past_session("Bench Press", [None, None], 0)  # planned only: excluded
    _past_session("Back Squat", [5], 1)  # other exercise

    assert [s["workout_id"] for s in state_store.get_exercise_sessions("Bench Press", limit=2)] == [newest, middle]
    assert [s["workout_id"] for s in state_store.get_exercise_sessions("Bench Press", exclude_workout_id=newest)] == [
        middle,
        oldest,
    ]
    rows = next(s for s in state_store.get_exercise_sessions("Bench Press") if s["workout_id"] == middle)["sets"]
    assert [(r["reps"], r["target_reps"], r["completed"]) for r in rows] == [(8, 8, True), (None, 8, False)]


def test_last_exercise_completed_at_counts_deload_ignores_planned(tmp_db):
    from orchestrator import state_store

    assert state_store.last_exercise_completed_at("Bench Press") is None
    _past_session("Bench Press", [8], 5)
    deload = _past_session("Bench Press", [8], 2, is_deload=True)
    _past_session("Bench Press", [None], 0)
    got = state_store.last_exercise_completed_at("Bench Press")
    assert got == state_store.get_workout(deload)["started_at"]
    assert state_store.last_exercise_completed_at("Bench Press", exclude_workout_id=deload) < got


def test_init_db_adds_deload_columns_to_program_builder_schema(tmp_path, monkeypatch):
    """A DB from the program-builder phase (programs + locations, no deload columns)."""
    import sqlite3

    from orchestrator import state_store

    path = str(tmp_path / "phase1.db")
    _old_schema_db(path)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        ALTER TABLE exercises ADD COLUMN requires TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE workouts ADD COLUMN location_id INTEGER;
        ALTER TABLE workouts ADD COLUMN program_id INTEGER;
        ALTER TABLE workouts ADD COLUMN program_day INTEGER;
        CREATE TABLE workout_locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            equipment TEXT NOT NULL DEFAULT '[]',
            limits TEXT NOT NULL DEFAULT '{}',
            last_used_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE workout_programs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal TEXT NOT NULL DEFAULT 'general',
            days_per_week INTEGER NOT NULL,
            session_minutes INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        INSERT INTO workout_programs (goal, days_per_week, session_minutes, active, created_at)
            VALUES ('strength', 4, 60, 1, '2026-09-01T10:00:00');
        UPDATE workouts SET program_id = 1, program_day = 0 WHERE id = 1;
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(state_store, "DB_PATH", path)

    state_store.init_db()

    program = state_store.get_active_program()
    assert (program["goal"], program["days_per_week"], program["deload_every_weeks"]) == ("strength", 4, 6)
    w = state_store.get_workout(1)
    assert (w["program_id"], w["program_day"], w["is_deload"]) == (1, 0, 0)
    # The trained legacy session counts toward the deload cycle and is history for progression.
    assert state_store.count_trained_program_sessions(1) == 1
    assert [s["workout_id"] for s in state_store.get_exercise_sessions("Back Squat")] == [1]

    state_store.init_db()  # idempotent
    assert state_store.get_active_program()["deload_every_weeks"] == 6
