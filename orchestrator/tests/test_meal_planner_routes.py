"""Tests for the F-015 phase 2 REST routes (routes_pantry: diet + meal plan +
recipe rating) and the `meal_plan` chat tool (tool_handlers._reg_meal_plan).

Routes are mounted on a bare FastAPI app (no auth middleware). The model is
never called: brain_ready / launch / _ask are monkeypatched.
"""

import json
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest


class FakeScheduler:
    def __init__(self):
        self.added = {}

    def add_job(self, func, trigger=None, run_date=None, args=None, id=None, replace_existing=False):
        self.added[id] = run_date

    def remove_job(self, job_id):
        self.added.pop(job_id)


@pytest.fixture
def mp(tmp_db, tmp_path, monkeypatch):
    from orchestrator import meal_planner, shared

    monkeypatch.setattr(shared, "DIET_PROFILE_PATH", str(tmp_path / "diet_profile.yaml"))
    monkeypatch.setattr(shared, "HELIOS_WAKE_ENABLED", False)
    monkeypatch.setattr(shared, "PANTRY_ENABLED", True)
    monkeypatch.setattr(shared, "scheduler", FakeScheduler())
    monkeypatch.setattr(meal_planner, "_meds_data", lambda: {})
    monkeypatch.setattr(meal_planner, "_swap_lock", None)
    monkeypatch.setattr(meal_planner, "launch", MagicMock())
    _set_brain(monkeypatch, meal_planner, True)
    return meal_planner


def _set_brain(monkeypatch, mp, up):
    async def ready():
        return up

    monkeypatch.setattr(mp, "brain_ready", ready)


@pytest.fixture
def client(mp):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.routes_pantry import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def R(name, items, minutes=20):
    return {
        "name": name,
        "ingredients": [{"item": i, "amount": "1"} for i in items],
        "steps": ["Cook it."],
        "minutes": minutes,
    }


def make_draft(mp, start=None, status="draft"):
    from orchestrator import diet_profiles, state_store

    start = start or date.today() + timedelta(days=2)
    settings = diet_profiles.load()
    with state_store.get_db() as conn:
        pid = conn.execute(
            "INSERT INTO meal_plans (week_start, diet_profile, status, settings_json, created_at) "
            "VALUES (?, ?, 'generating', ?, ?)",
            (start.isoformat(), settings["profile"], json.dumps(settings), mp._now()),
        ).lastrowid
    plan = {
        "batch": mp._norm_recipe(R("Barley stew", ["pearl barley", "carrot"], 60)),
        "dinners": [
            dict(mp._norm_recipe(R("Bean tacos", ["black beans", "tortillas"])), date=start.isoformat()),
            dict(mp._norm_recipe(R("Pea soup", ["peas"])), date=(start + timedelta(days=3)).isoformat()),
        ],
        "notes": "",
    }
    mp._store_plan(pid, plan, settings, start, [])
    if status != "draft":
        mp._set_plan(pid, status=status)
    return pid


def fake_ask(replies):
    it = iter(replies)

    async def _ask(prompt, max_tokens, thinking, purpose):
        return next(it), "stop", 10

    return _ask


SWAP_REPLY = json.dumps(R("Tofu stir fry", ["firm tofu", "broccoli"]))


# ---------------------------------------------------------------------------
# Diet settings
# ---------------------------------------------------------------------------


class TestDietRoutes:
    def test_get_defaults(self, client):
        r = client.get("/api/pantry/diet")
        assert r.status_code == 200
        body = r.json()
        assert body["settings"]["profile"] == "mediterranean"
        assert any(p["key"] == "dash" for p in body["profiles"])

    @pytest.mark.parametrize(
        "payload",
        [{"servings": 0}, {"profile": "keto"}, {"reminders": {"prep_time": "25:00"}}, {"avoid": "nuts"}],
    )
    def test_put_bad_payload(self, client, payload):
        r = client.put("/api/pantry/diet", json=payload)
        assert r.status_code == 400 and r.json()["ok"] is False

    def test_put_non_object_body_is_rejected(self, client):
        # Was a silent 200 no-op (phase 2 pentest nit) — now an explicit 400.
        for body in (["x"], "hello", 3):
            r = client.put("/api/pantry/diet", json=body)
            assert r.status_code == 400
        r = client.put("/api/pantry/diet", content=b"{bad json", headers={"content-type": "application/json"})
        assert r.status_code == 400
        assert client.get("/api/pantry/diet").json()["settings"]["profile"] == "mediterranean"

    def test_put_saves_and_reschedules_only_on_reminder_change(self, client, mp):
        pid = make_draft(mp)
        assert mp.accept_plan(pid)["ok"]
        r = client.put("/api/pantry/diet", json={"servings": 2})
        assert r.status_code == 200 and r.json()["rescheduled_plans"] == 0
        assert r.json()["settings"]["servings"] == 2
        r = client.put("/api/pantry/diet", json={"reminders": {"dinner_time": "19:00"}})
        assert r.json()["rescheduled_plans"] == 1
        assert client.get("/api/pantry/diet").json()["settings"]["reminders"]["dinner_time"] == "19:00"


# ---------------------------------------------------------------------------
# Plan routes
# ---------------------------------------------------------------------------


class TestPlanRoutes:
    def test_current_empty(self, client):
        assert client.get("/api/pantry/plan").json() == {"active": None, "draft": None}

    def test_generate_asleep(self, client, mp, monkeypatch):
        _set_brain(monkeypatch, mp, False)
        r = client.post("/api/pantry/plan/generate")
        assert r.status_code == 503 and "asleep" in r.json()["error"]
        mp.launch.assert_not_called()

    def test_generate_then_busy(self, client, mp):
        r = client.post("/api/pantry/plan/generate")
        assert r.status_code == 202
        pid = r.json()["plan_id"]
        mp.launch.assert_called_once_with(pid)
        r2 = client.post("/api/pantry/plan/generate")
        assert r2.status_code == 409 and r2.json()["plan_id"] == pid
        assert "status_code" not in r2.json()
        assert client.get("/api/pantry/plan").json()["draft"]["status"] == "generating"

    def test_generate_nothing_to_plan(self, client, mp):
        client.put("/api/pantry/diet", json={"batch_lunch_days": 0, "fresh_dinners_per_week": 0})
        assert client.post("/api/pantry/plan/generate").status_code == 400

    @pytest.mark.parametrize("pid", [0, -1, 2**62, 2**70])
    def test_get_bad_ids(self, client, pid):
        assert client.get(f"/api/pantry/plan/{pid}").status_code == 404

    def test_get_plan(self, client, mp):
        pid = make_draft(mp)
        r = client.get(f"/api/pantry/plan/{pid}")
        assert r.status_code == 200 and r.json()["status"] == "draft"
        assert client.get("/api/pantry/plan/123456").status_code == 404

    def test_accept_and_discard_status_codes(self, client, mp):
        pid = make_draft(mp)
        r = client.post(f"/api/pantry/plan/{pid}/accept", json={"already_have": ["carrot"]})
        assert r.status_code == 200 and r.json()["added_to_pantry"] == 1
        assert client.post(f"/api/pantry/plan/{pid}/accept").status_code == 409
        assert client.post("/api/pantry/plan/99999/accept").status_code == 404
        assert client.post(f"/api/pantry/plan/{2**70}/accept").status_code == 404
        assert client.post(f"/api/pantry/plan/{pid}/discard").status_code == 200
        assert client.post(f"/api/pantry/plan/{pid}/discard").status_code == 409
        assert client.post("/api/pantry/plan/99999/discard").status_code == 404

    def test_accept_bad_already_have(self, client, mp):
        pid = make_draft(mp)
        r = client.post(f"/api/pantry/plan/{pid}/accept", json={"already_have": "carrot"})
        assert r.status_code == 400

    def test_accept_garbage_body(self, client, mp):
        pid = make_draft(mp)
        r = client.post(f"/api/pantry/plan/{pid}/accept", content=b"not json", headers={"content-type": "text/plain"})
        assert r.status_code == 200

    def test_swap(self, client, mp, monkeypatch):
        pid = make_draft(mp)
        eid = next(e["id"] for e in mp.get_plan(pid)["entries"] if e["slot"] == "dinner")
        monkeypatch.setattr(mp, "_ask", fake_ask([SWAP_REPLY]))
        r = client.post(f"/api/pantry/plan/entry/{eid}/swap")
        assert r.status_code == 200 and r.json()["ok"] is True
        assert any(rec["name"] == "Tofu stir fry" for rec in r.json()["plan"]["recipes"].values())
        assert client.post("/api/pantry/plan/entry/777777/swap").status_code == 404
        assert client.post(f"/api/pantry/plan/entry/{2**70}/swap").status_code == 404

    def test_swap_asleep(self, client, mp, monkeypatch):
        pid = make_draft(mp)
        eid = mp.get_plan(pid)["entries"][0]["id"]
        _set_brain(monkeypatch, mp, False)
        assert client.post(f"/api/pantry/plan/entry/{eid}/swap").status_code == 503

    def test_rate(self, client, mp):
        pid = make_draft(mp)
        rid = mp.get_plan(pid)["entries"][0]["recipe_id"]
        assert client.post(f"/api/pantry/recipe/{rid}/rate", json={"rating": "up"}).status_code == 200
        assert client.post(f"/api/pantry/recipe/{rid}/rate", json={"rating": ["up"]}).status_code == 400
        assert client.post(f"/api/pantry/recipe/{rid}/rate", json={"rating": {"a": 1}}).status_code == 400
        assert client.post("/api/pantry/recipe/55555/rate", json={"rating": "up"}).status_code == 404
        assert client.post(f"/api/pantry/recipe/{2**70}/rate", json={"rating": "up"}).status_code == 404


class TestItemNameGuard:
    @pytest.mark.parametrize("name", [123, None, ["eggs"], {"n": 1}])
    def test_non_string_name(self, client, name):
        r = client.post("/api/pantry/items", json={"name": name})
        assert r.status_code == 400 and r.json()["error"] == "name must be a string"


# ---------------------------------------------------------------------------
# meal_plan chat tool
# ---------------------------------------------------------------------------


class TestMealPlanTool:
    @pytest.fixture
    def tool(self, mp):
        from orchestrator.tool_handlers import _reg_meal_plan

        return _reg_meal_plan

    async def test_disabled(self, tool, monkeypatch):
        from orchestrator import shared

        monkeypatch.setattr(shared, "PANTRY_ENABLED", False)
        assert "isn't enabled" in await tool({"action": "view"})

    @pytest.mark.parametrize("day", ["tomorrow", "2026-1-5", "2026-10-14T00:00", 20261014])
    async def test_bad_day(self, tool, day):
        assert await tool({"action": "recipe", "day": day}) == "day must be YYYY-MM-DD."

    async def test_view_and_recipe(self, tool, mp):
        assert "no meal plan" in await tool({})
        start = date.today() + timedelta(days=2)
        make_draft(mp, start)
        assert "DRAFT" in await tool({"action": "VIEW"})
        txt = await tool({"action": "recipe", "day": start.isoformat()})
        assert txt.startswith("Bean tacos")
        assert (await tool({"action": "recipe", "slot": "batch", "day": start.isoformat()})).startswith("Barley stew")

    async def test_generate(self, tool, mp, monkeypatch):
        _set_brain(monkeypatch, mp, False)
        assert "asleep" in await tool({"action": "generate"})
        mp.launch.assert_not_called()
        _set_brain(monkeypatch, mp, True)
        out = await tool({"action": "generate"})
        assert out.startswith("Started planning the week of")
        mp.launch.assert_called_once()
        assert "already being made" in await tool({"action": "generate"})

    async def test_accept(self, tool, mp):
        assert await tool({"action": "accept"}) == "There's no draft plan to accept."
        pid = make_draft(mp)
        out = await tool({"action": "accept"})
        assert out.startswith("Plan accepted.") and "3 cooking reminder(s)" in out
        assert mp.get_plan(pid)["status"] == "active"

    async def test_swap(self, tool, mp, monkeypatch):
        assert (await tool({"action": "swap"})).startswith("No dinner in the plan")
        start = date.today() + timedelta(days=2)
        make_draft(mp, start)
        monkeypatch.setattr(mp, "_ask", fake_ask([SWAP_REPLY]))
        out = await tool({"action": "swap", "day": start.isoformat()})
        assert out.startswith("Swapped.") and "Tofu stir fry" in out
        _set_brain(monkeypatch, mp, False)
        assert "asleep" in await tool({"action": "swap", "day": start.isoformat()})

    async def test_unknown_action(self, tool):
        assert (await tool({"action": "delete_everything"})).startswith("Unknown meal_plan action")
