"""Shared pytest fixtures and helpers for the pantry / meal-plan / receipt /
grocery / workout test files.

conftest.py star-imports this module, so every fixture listed in ``__all__``
is available to every test file. Give fixtures names unique across the suite
(prefix with the area) — a clash with another fixtures_*.py module is caught
by conftest at import time. Plain helper functions can be imported directly.

conftest imports this module before any test runs, so orchestrator modules
are imported inside the helpers, never at module level (importing
``orchestrator.shared`` loads the embedding model).

What lives here:
- fixtures: ``pantry_env`` (temp DB + every shared setting the pantry, meal
  planner and receipt code reads; ``FakeScheduler`` installed; the meal
  planner's meds / swap lock / idle-wait stubbed), ``pantry_meals`` (the
  selfcare meal bridge recorded instead of run — override it in a test file to
  keep the real one), ``pantry_sched`` (the installed ``FakeScheduler``) and
  ``pantry_client`` (a TestClient on routes_pantry.router only, no auth);
- photo bytes ``JPEG`` / ``PNG`` / ``WEBP``, ``fake_vision`` (vision health +
  analyze_image mocked), ``add`` / ``item`` (pantry rows), ``make_scan``;
- meal plans: ``recipe`` (the model's recipe JSON), ``fake_ask`` and
  ``SWAP_REPLY`` (meal_planner._ask stand-ins), ``store_plan`` / ``week_plan``
  (plans through the real ``_store_plan``), ``insert_plan`` / ``insert_batch``
  / ``bare_recipe`` (rows inserted directly) and the row readers ``db``,
  ``entry``, ``batches``, ``cooks``;
- workouts: ``workout_session`` (a past planned session), ``workout_train_all``,
  ``workout_backdate``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

__all__: list[str] = ["pantry_meals", "pantry_env", "pantry_sched", "pantry_client"]

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 32


class FakeScheduler:
    """The add_job / remove_job surface of the APScheduler instance."""

    def __init__(self):
        self.added = {}
        self.removed = []
        self.fail = False

    def add_job(self, func, trigger=None, run_date=None, args=None, id=None, replace_existing=False, **kw):
        if self.fail:
            raise RuntimeError("scheduler down")
        self.added[id] = {"run_date": run_date, "args": args}

    def remove_job(self, job_id):
        self.removed.append(job_id)
        if job_id not in self.added:
            raise LookupError(job_id)
        del self.added[job_id]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pantry_meals(monkeypatch):
    """Labels the selfcare meal bridge was called with (the bridge itself is not run)."""
    from orchestrator import selfcare_manager

    logged = []
    monkeypatch.setattr(selfcare_manager, "record_meal_logged", lambda label="a meal": logged.append(label))
    return logged


@pytest.fixture
def pantry_env(tmp_db, tmp_path, monkeypatch, pantry_meals):
    from orchestrator import meal_planner, pantry_manager, shared

    for name, value in {
        "PANTRY_ENABLED": True,
        "PANTRY_PHOTOS_DIR": str(tmp_path / "pantry_photos"),
        "PANTRY_BARCODE_LOOKUP_ENABLED": True,
        "PANTRY_OFF_URL": "https://off.example.test",
        "PANTRY_MAX_UPLOAD_MB": 15,
        "VISION_ENABLED": True,
        "VISION_MAX_IMAGE_SIZE": 20 * 1024 * 1024,
        "HELIOS_WAKE_ENABLED": False,
        "DIET_PROFILE_PATH": str(tmp_path / "diet_profile.yaml"),
        "scheduler": FakeScheduler(),
    }.items():
        monkeypatch.setattr(shared, name, value)
    monkeypatch.setattr(pantry_manager, "_vision_lock", None)
    monkeypatch.setattr(meal_planner, "_meds_data", lambda: {})
    monkeypatch.setattr(meal_planner, "_swap_lock", None)

    async def _no_wait():
        return None

    monkeypatch.setattr(meal_planner, "_wait_for_idle_chat", _no_wait)
    return tmp_db


@pytest.fixture
def pantry_sched(pantry_env):
    from orchestrator import shared

    return shared.scheduler


@pytest.fixture
def pantry_client(pantry_env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.routes_pantry import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Pantry rows, scans, vision
# ---------------------------------------------------------------------------


def db():
    from orchestrator import state_store

    return state_store.get_db()


def add(name, **kw):
    from orchestrator import pantry_manager

    res = pantry_manager.add_item(name, **kw)
    assert res["ok"], res
    return res["item"]


def item(item_id):
    from orchestrator import pantry_manager

    return pantry_manager.get_item(item_id)


def make_scan(kind="fridge", photos=(JPEG,), *, status=None, items=None, proposed=None, age_minutes=0, sha=None):
    """A scan row with real saved photos → (scan_id, photo_paths).

    ``items`` stores a stock-take proposal ``{"items": items, "warnings": []}``;
    ``proposed`` stores that dict as given (receipts carry a "receipt" key).
    """
    from orchestrator import pantry_manager

    paths = [pantry_manager.save_scan_photo(p)["path"] for p in photos]
    sid = pantry_manager.create_scan(kind, paths, sha)["id"]
    fields = {}
    if status:
        fields["status"] = status
    if items is not None:
        proposed = {"items": items, "warnings": []}
    if proposed is not None:
        fields["proposed_json"] = json.dumps(proposed)
    if age_minutes:
        fields["created_at"] = (datetime.now() - timedelta(minutes=age_minutes)).isoformat(timespec="seconds")
    if fields:
        pantry_manager._set_scan(sid, **fields)
    return sid, paths


def fake_vision(monkeypatch, healthy=True, replies=None, side_effect=None):
    """Mock vision health + analyze_image; returns the analyze_image mock."""
    from orchestrator import vision_handler

    analyze = AsyncMock(side_effect=side_effect) if side_effect else AsyncMock(side_effect=list(replies or []))
    monkeypatch.setattr(vision_handler, "check_vision_health", AsyncMock(return_value=healthy))
    monkeypatch.setattr(vision_handler, "analyze_image", analyze)
    return analyze


# ---------------------------------------------------------------------------
# Meal plans
# ---------------------------------------------------------------------------


def recipe(name, items, minutes=20, steps=("Cook it all together.",)):
    """A recipe as the model returns it. ``items``: names (amount "1 cup") or (name, amount) pairs."""
    pairs = [i if isinstance(i, tuple) else (i, "1 cup") for i in items]
    return {
        "name": name,
        "ingredients": [{"item": i, "amount": a} for i, a in pairs],
        "steps": list(steps),
        "minutes": minutes,
        "est_kcal": 500,
    }


SWAP_REPLY = json.dumps(recipe("Tofu stir fry", ["firm tofu", "broccoli"], 25))


def fake_ask(replies, calls=None):
    """Async meal_planner._ask stand-in yielding ``replies`` in order.

    A reply may be a string, an exception (raised) or a callable (its result
    is the reply). ``calls`` collects each call's arguments.
    """
    it = iter(replies)

    async def _ask(prompt, max_tokens, thinking, purpose, wait_for_idle=True):
        if calls is not None:
            calls.append(
                {
                    "prompt": prompt,
                    "max_tokens": max_tokens,
                    "thinking": thinking,
                    "purpose": purpose,
                    "wait_for_idle": wait_for_idle,
                }
            )
        r = next(it)
        if isinstance(r, BaseException):
            raise r
        if callable(r):
            r = r()
        return r, "stop", 100

    return _ask


def store_plan(start, batch=None, dinners=(), status="draft", settings=None, created_at=None):
    """A plan through the real meal_planner._store_plan. dinners: [(day_offset, recipe)]."""
    from orchestrator import diet_profiles, meal_planner

    settings = settings or diet_profiles.load()
    with db() as conn:
        pid = conn.execute(
            "INSERT INTO meal_plans (week_start, diet_profile, status, settings_json, created_at) "
            "VALUES (?, ?, 'generating', ?, ?)",
            (start.isoformat(), settings["profile"], json.dumps(settings), created_at or meal_planner._now()),
        ).lastrowid
    plan = {
        "batch": meal_planner._norm_recipe(batch) if batch else None,
        "dinners": [
            dict(meal_planner._norm_recipe(r), date=(start + timedelta(days=o)).isoformat()) for o, r in dinners
        ],
        "notes": "A plan.",
    }
    meal_planner._store_plan(pid, plan, settings, start, [])
    if status != "draft":
        meal_planner._set_plan(pid, status=status)
    return pid


def week_plan(days_ahead=2, **kw):
    """A plan starting ``days_ahead`` days from today: a barley-stew batch prep
    (4 lunches) and two dinners (day 0 and day 3)."""
    return store_plan(
        date.today() + timedelta(days=days_ahead),
        batch=recipe("Barley stew", ["pearl barley", "carrot"], 60),
        dinners=[(0, recipe("Bean tacos", ["black beans", "tortillas"])), (3, recipe("Pea soup", ["peas", "carrot"]))],
        **kw,
    )


def _recipe_row(conn, name, ings):
    rid = conn.execute(
        "INSERT INTO recipes (name, diet_tags, servings, batchable, minutes, method, created_at) "
        "VALUES (?, '[\"balanced\"]', 2, 0, 20, '[\"Cook it.\"]', ?)",
        (name, datetime.now().isoformat(timespec="seconds")),
    ).lastrowid
    for ing in ings:
        nm, amount, optional = (ing + (0,))[:3] if len(ing) == 2 else ing
        conn.execute(
            "INSERT INTO recipe_ingredients (recipe_id, item_name, amount, optional) VALUES (?, ?, ?, ?)",
            (rid, nm, amount, optional),
        )
    return rid


def insert_plan(status="active", dinner=None, prep=None, portions=4, lunch_days=None, start=None):
    """Plan rows inserted directly (dinner "Bean skillet", prep "Barley stew").

    dinner/prep: list of (item_name, amount[, optional]). Returns plan_id,
    dinner_id/dinner_recipe, prep_id/prep_recipe and lunch_ids.
    """
    start = start or date.today()
    out = {"lunch_ids": []}
    with db() as conn:
        pid = conn.execute(
            "INSERT INTO meal_plans (week_start, diet_profile, status, settings_json, created_at) "
            "VALUES (?, 'balanced', ?, '{}', ?)",
            (start.isoformat(), status, datetime.now().isoformat(timespec="seconds")),
        ).lastrowid
        out["plan_id"] = pid
        if dinner is not None:
            rid = _recipe_row(conn, "Bean skillet", dinner)
            out["dinner_recipe"] = rid
            out["dinner_id"] = conn.execute(
                "INSERT INTO meal_plan_entries (plan_id, day, slot, recipe_id, mode, portions) "
                "VALUES (?, ?, 'dinner', ?, 'fresh', 2)",
                (pid, start.isoformat(), rid),
            ).lastrowid
        if prep is not None:
            rid = _recipe_row(conn, "Barley stew", prep)
            out["prep_recipe"] = rid
            out["prep_id"] = conn.execute(
                "INSERT INTO meal_plan_entries (plan_id, day, slot, recipe_id, mode, portions) "
                "VALUES (?, ?, 'prep', ?, 'batch', ?)",
                (pid, start.isoformat(), rid, portions),
            ).lastrowid
            for d in lunch_days if lunch_days is not None else [start + timedelta(days=1), start + timedelta(days=2)]:
                out["lunch_ids"].append(
                    conn.execute(
                        "INSERT INTO meal_plan_entries (plan_id, day, slot, recipe_id, mode, portions) "
                        "VALUES (?, ?, 'lunch', ?, 'portion', 2)",
                        (pid, d.isoformat(), rid),
                    ).lastrowid
                )
    return out


def bare_recipe(name="Barley stew"):
    with db() as conn:
        return _recipe_row(conn, name, [])


def insert_batch(recipe_id, good_until, portions=3, left=None, status="active"):
    with db() as conn:
        return conn.execute(
            "INSERT INTO prep_batches (plan_id, entry_id, recipe_id, cooked_at, portions_made, portions_left, "
            "good_until, status) VALUES (NULL, NULL, ?, ?, ?, ?, ?, ?)",
            (
                recipe_id,
                datetime.now().isoformat(timespec="seconds"),
                portions,
                portions if left is None else left,
                good_until.isoformat(),
                status,
            ),
        ).lastrowid


def entry(entry_id):
    with db() as conn:
        return dict(conn.execute("SELECT * FROM meal_plan_entries WHERE id = ?", (entry_id,)).fetchone())


def batches():
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM prep_batches ORDER BY id")]


def cooks():
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM pantry_cooks ORDER BY id")]


# ---------------------------------------------------------------------------
# Workouts
# ---------------------------------------------------------------------------


def workout_backdate(workout_id, days):
    with db() as conn:
        when = (datetime.now() - timedelta(days=days)).isoformat()
        conn.execute("UPDATE workouts SET started_at = ? WHERE id = ?", (when, workout_id))


def workout_session(name, weight, reps, target_reps=8, *, days_ago=2, rpe=None, location_id=None, deload=False):
    """Record a past planned session: one planned set per entry in ``reps``
    (None = planned but never done), all targeted at ``weight``."""
    from orchestrator import state_store

    wid = state_store.create_workout("full_body", True)
    workout_backdate(wid, days_ago)
    state_store.set_workout_context(wid, location_id, None, None, is_deload=deload)
    for i, r in enumerate(reps, start=1):
        sid = state_store.add_planned_set(wid, name, [], i, target_reps, weight)
        if r is not None:
            state_store.log_completed_set(wid, name, weight, r, rpe=rpe, set_id=sid)
    return wid


def workout_train_all(workout_id):
    """Complete every planned set at its target (20 lb where none)."""
    from orchestrator import state_store

    for s in state_store.get_workout(workout_id)["sets"]:
        state_store.log_completed_set(
            workout_id, s["exercise_name"], s["target_weight_lbs"] or 20.0, s["target_reps"], set_id=s["id"]
        )
