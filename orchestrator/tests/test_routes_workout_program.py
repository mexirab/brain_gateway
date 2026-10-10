"""Tests for the workout program-builder REST routes (routes_workout.py).

TestClient on an app with only routes_workout.router (no auth middleware);
tmp_db gives a fresh SQLite DB with the real exercise catalog.
"""

import pytest


@pytest.fixture
def client(tmp_db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.routes_workout import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _make_loc(client, name, **body):
    r = client.post("/api/workouts/locations", json={"name": name, **body})
    assert r.status_code == 201, r.text
    return r.json()["location"]


# --- program -------------------------------------------------------------------


def test_program_get_put_delete_flow(client):
    assert client.get("/api/workouts/program").json() == {"program": None}

    r = client.put("/api/workouts/program", json={"goal": "muscle", "days_per_week": 4, "session_minutes": 60})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    p = body["program"]
    assert (p["goal"], p["days_per_week"], p["session_minutes"]) == ("muscle", 4, 60)
    assert p["split"] == ["Upper", "Lower", "Upper", "Lower"]
    assert p["next_day_label"] == "Upper"
    assert p["exercises_per_session"] == 7

    assert client.get("/api/workouts/program").json()["program"]["id"] == p["id"]

    r = client.delete("/api/workouts/program")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert client.get("/api/workouts/program").json() == {"program": None}


def test_program_put_deload(client):
    r = client.put(
        "/api/workouts/program",
        json={"goal": "general", "days_per_week": 3, "session_minutes": 45, "deload_every_weeks": 4},
    )
    p = r.json()["program"]
    assert p["deload_every_weeks"] == 4
    assert p["deload"] == {"enabled": True, "is_deload": False, "sessions_until_deload": 12}

    r = client.put(
        "/api/workouts/program",
        json={"goal": "general", "days_per_week": 3, "session_minutes": 45, "deload_every_weeks": 2},
    )
    assert r.status_code == 400 and r.json()["ok"] is False
    assert client.get("/api/workouts/program").json()["program"]["deload_every_weeks"] == 4


def test_program_delete_when_none_is_404(client):
    r = client.delete("/api/workouts/program")
    assert r.status_code == 404
    assert r.json() == {"ok": False, "error": "no active program"}


@pytest.mark.parametrize(
    "body",
    [
        {"goal": "cardio", "days_per_week": 3, "session_minutes": 45},
        {"goal": "strength", "days_per_week": 7, "session_minutes": 45},
        {"goal": "strength", "days_per_week": 1, "session_minutes": 45},
        {"goal": "strength", "days_per_week": 3, "session_minutes": 5},
        {"goal": "strength", "days_per_week": "x", "session_minutes": 45},
        {},
    ],
)
def test_program_put_validation_400(client, body):
    r = client.put("/api/workouts/program", json=body)
    assert r.status_code == 400
    assert r.json()["ok"] is False
    assert client.get("/api/workouts/program").json() == {"program": None}


def test_program_put_non_json_body_400(client):
    r = client.put("/api/workouts/program", content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 400


# --- locations ---------------------------------------------------------------


def test_locations_list_includes_vocabulary_and_templates(client):
    body = client.get("/api/workouts/locations").json()
    assert body["locations"] == []
    assert "kettlebell" in body["equipment_vocabulary"]
    assert body["templates"]["home_basic"] == ["dumbbells", "kettlebell"]


def test_locations_create_patch_delete_flow(client):
    loc = _make_loc(client, "Home", template="home_basic", limits={"dumbbell_max_lbs": 50})
    assert loc["equipment"] == ["dumbbells", "kettlebell"]
    assert loc["limits"] == {"dumbbell_max_lbs": 50.0}

    r = client.patch(
        f"/api/workouts/locations/{loc['id']}",
        json={"add_equipment": ["bench"], "limits": {"dumbbell_max_lbs": None}},
    )
    assert r.status_code == 200
    assert r.json()["location"]["equipment"] == ["bench", "dumbbells", "kettlebell"]
    assert r.json()["location"]["limits"] == {}

    names = [x["name"] for x in client.get("/api/workouts/locations").json()["locations"]]
    assert names == ["Home"]

    r = client.delete(f"/api/workouts/locations/{loc['id']}")
    assert r.status_code == 200 and r.json() == {"ok": True, "removed": "Home"}
    assert client.get("/api/workouts/locations").json()["locations"] == []


def test_locations_create_defaults_to_full_gym(client):
    from orchestrator.exercises_seed import EQUIPMENT_TOKENS

    assert _make_loc(client, "Big Gym")["equipment"] == list(EQUIPMENT_TOKENS)


def test_locations_duplicate_name_case_insensitive_400(client):
    _make_loc(client, "Big Gym")
    r = client.post("/api/workouts/locations", json={"name": "big gym"})
    assert r.status_code == 400
    assert "already exists" in r.json()["error"]


@pytest.mark.parametrize(
    "body",
    [
        {"name": "Hotel", "equipment": ["barbell", "hot_tub"]},
        {"name": "Hotel", "template": "castle"},
        {"name": ""},
        {},
        {"name": "Hotel", "limits": {"dumbbell_max_lbs": 9000}},
    ],
)
def test_locations_create_validation_400(client, body):
    r = client.post("/api/workouts/locations", json=body)
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_locations_patch_unknown_equipment_400(client):
    loc = _make_loc(client, "Home", template="home_basic")
    r = client.patch(f"/api/workouts/locations/{loc['id']}", json={"equipment": ["hot_tub"]})
    assert r.status_code == 400
    assert "hot_tub" in r.json()["error"]


def test_locations_patch_rename_collision_400(client):
    _make_loc(client, "Home")
    other = _make_loc(client, "Hotel")
    r = client.patch(f"/api/workouts/locations/{other['id']}", json={"name": "HOME"})
    assert r.status_code == 400


def test_locations_unknown_id_404(client):
    assert client.patch("/api/workouts/locations/9999", json={"name": "X"}).status_code == 404
    assert client.delete("/api/workouts/locations/9999").status_code == 404


def test_locations_non_int_id_422(client):
    assert client.delete("/api/workouts/locations/home").status_code == 422


# --- generate + exercises ----------------------------------------------------


def test_generate_with_location_id(client):
    home = _make_loc(client, "Home", template="home_basic")
    r = client.post("/api/workouts/generate", json={"location_id": home["id"]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["location"] == {"id": home["id"], "name": "Home"}


def test_generate_non_int_location_400(client):
    from orchestrator import state_store

    r = client.post("/api/workouts/generate", json={"location_id": "abc"})
    assert r.status_code == 400
    assert r.json() == {"ok": False, "error": "location_id must be an integer"}
    assert state_store.get_todays_workout() is None


def test_generate_unknown_location_400(client):
    r = client.post("/api/workouts/generate", json={"location_id": 9999})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_generate_without_body_uses_only_location(client):
    home = _make_loc(client, "Home", template="home_basic")
    r = client.post("/api/workouts/generate")
    assert r.status_code == 200
    assert r.json()["location"]["id"] == home["id"]


def test_generate_without_body_uses_last_used_location(client):
    from orchestrator import state_store

    _make_loc(client, "Home", template="home_basic")
    gym = _make_loc(client, "Big Gym")
    state_store.touch_workout_location(gym["id"])
    r = client.post("/api/workouts/generate", json={})
    assert r.json()["location"]["id"] == gym["id"]


def test_generate_without_body_two_unused_locations_uses_whole_catalog(client):
    _make_loc(client, "Home", template="home_basic")
    _make_loc(client, "Big Gym")
    r = client.post("/api/workouts/generate", json={})
    assert r.status_code == 200
    assert r.json()["location"] is None


def test_generate_already_exists_reports_location_mismatch(client):
    home = _make_loc(client, "Home", template="home_basic")
    gym = _make_loc(client, "Big Gym")
    first = client.post("/api/workouts/generate", json={"location_id": home["id"]}).json()

    same = client.post("/api/workouts/generate", json={"location_id": home["id"]}).json()
    assert same["already_exists"] is True
    assert same["workout_id"] == first["workout_id"]
    assert same["location_mismatch"] is False

    other = client.post("/api/workouts/generate", json={"location_id": gym["id"]}).json()
    assert other["already_exists"] is True
    assert other["location_mismatch"] is True

    no_loc = client.post("/api/workouts/generate", json={}).json()
    assert no_loc["already_exists"] is True
    assert no_loc["location_mismatch"] is False


def test_exercises_filtered_by_location(client):
    home = _make_loc(client, "Home", template="home_basic")
    everything = client.get("/api/workouts/exercises").json()
    at_home = client.get(f"/api/workouts/exercises?location_id={home['id']}").json()
    assert 0 < len(at_home) < len(everything)
    assert all(set(e["requires"]) <= {"dumbbells", "kettlebell"} for e in at_home)


def test_exercises_unknown_location_404(client):
    r = client.get("/api/workouts/exercises?location_id=9999")
    assert r.status_code == 404
    assert r.json() == {"ok": False, "error": "location not found"}


def test_today_reports_location_after_location_deleted(client):
    home = _make_loc(client, "Home", template="home_basic")
    client.post("/api/workouts/generate", json={"location_id": home["id"]})
    client.delete(f"/api/workouts/locations/{home['id']}")
    r = client.get("/api/workouts/today")
    assert r.status_code == 200
    body = r.json()
    assert body["has_workout"] is True
    assert body["location"] is None


def test_generate_explicit_null_location_uses_whole_catalog(client):
    """An explicit null location_id skips the last-used default (dashboard "Not sure (full gym)")."""
    from orchestrator import state_store

    loc = client.post("/api/workouts/locations", json={"name": "Home", "template": "home_basic"}).json()["location"]
    state_store.touch_workout_location(loc["id"])

    resp = client.post("/api/workouts/generate", json={"location_id": None})

    assert resp.status_code == 200
    assert resp.json()["location"] is None


def test_today_exposes_deload_flag_and_exercise_notes(client, monkeypatch):
    from orchestrator import state_store

    client.put(
        "/api/workouts/program",
        json={"goal": "general", "days_per_week": 3, "session_minutes": 30, "deload_every_weeks": 4},
    )
    monkeypatch.setattr(state_store, "count_trained_program_sessions", lambda _pid: 12)
    gen = client.post("/api/workouts/generate", json={"location_id": None}).json()
    assert gen["ok"] is True and gen["deload"] is True

    body = client.get("/api/workouts/today").json()
    assert body["workout_id"] == gen["workout_id"]
    assert body["is_deload"] is True
    assert body["exercises"] and all(e["note"] == "deload week — lighter on purpose" for e in body["exercises"])
    # Only set 1 carries the note.
    assert all([s["notes"] for s in e["sets"]][1:] == [None] * (len(e["sets"]) - 1) for e in body["exercises"])


def test_today_note_is_null_for_new_exercises(client):
    gen = client.post("/api/workouts/generate", json={"location_id": None}).json()
    body = client.get("/api/workouts/today").json()
    assert body["workout_id"] == gen["workout_id"]
    assert body["is_deload"] is False
    assert body["exercises"] and all(e["note"] is None for e in body["exercises"])
