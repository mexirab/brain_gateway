"""Tests for the F-015 phase 2 REST routes (routes_pantry: diet + meal plan +
recipe rating) and the `meal_plan` chat tool (tool_handlers._reg_meal_plan).

Routes are mounted on a bare FastAPI app (no auth middleware). The model is
never called: brain_ready / launch / _ask are monkeypatched.
"""

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from orchestrator import meal_planner as mp
from orchestrator.tests.fixtures_pantry import SWAP_REPLY, fake_ask, week_plan
from orchestrator.tool_handlers import _reg_meal_plan as meal_plan_tool

pytestmark = pytest.mark.usefixtures("pantry_env")


def _set_brain(monkeypatch, up):
    async def ready():
        return up

    monkeypatch.setattr(mp, "brain_ready", ready)


@pytest.fixture(autouse=True)
def _planner_stubs(monkeypatch):
    """No background generation; the brain reads as awake unless a test says otherwise."""
    monkeypatch.setattr(mp, "launch", MagicMock())
    _set_brain(monkeypatch, True)


# ---------------------------------------------------------------------------
# Diet settings
# ---------------------------------------------------------------------------


class TestDietRoutes:
    def test_get_defaults(self, pantry_client):
        r = pantry_client.get("/api/pantry/diet")
        assert r.status_code == 200
        body = r.json()
        assert body["settings"]["profile"] == "mediterranean"
        assert any(p["key"] == "dash" for p in body["profiles"])

    @pytest.mark.parametrize(
        "payload",
        [{"servings": 0}, {"profile": "keto"}, {"reminders": {"prep_time": "25:00"}}, {"avoid": "nuts"}],
    )
    def test_put_bad_payload(self, pantry_client, payload):
        r = pantry_client.put("/api/pantry/diet", json=payload)
        assert r.status_code == 400 and r.json()["ok"] is False

    def test_put_non_object_body_is_rejected(self, pantry_client):
        # Was a silent 200 no-op (phase 2 pentest nit) — now an explicit 400.
        for body in (["x"], "hello", 3):
            r = pantry_client.put("/api/pantry/diet", json=body)
            assert r.status_code == 400
        r = pantry_client.put("/api/pantry/diet", content=b"{bad json", headers={"content-type": "application/json"})
        assert r.status_code == 400
        assert pantry_client.get("/api/pantry/diet").json()["settings"]["profile"] == "mediterranean"

    def test_put_saves_and_reschedules_only_on_reminder_change(self, pantry_client):
        pid = week_plan()
        assert mp.accept_plan(pid)["ok"]
        r = pantry_client.put("/api/pantry/diet", json={"servings": 2})
        assert r.status_code == 200 and r.json()["rescheduled_plans"] == 0
        assert r.json()["settings"]["servings"] == 2
        r = pantry_client.put("/api/pantry/diet", json={"reminders": {"dinner_time": "19:00"}})
        assert r.json()["rescheduled_plans"] == 1
        assert pantry_client.get("/api/pantry/diet").json()["settings"]["reminders"]["dinner_time"] == "19:00"


# ---------------------------------------------------------------------------
# Plan routes
# ---------------------------------------------------------------------------


class TestPlanRoutes:
    def test_current_empty(self, pantry_client):
        assert pantry_client.get("/api/pantry/plan").json() == {"active": None, "draft": None}

    def test_generate_asleep(self, pantry_client, monkeypatch):
        _set_brain(monkeypatch, False)
        r = pantry_client.post("/api/pantry/plan/generate")
        assert r.status_code == 503 and "asleep" in r.json()["error"]
        mp.launch.assert_not_called()

    def test_generate_then_busy(self, pantry_client):
        r = pantry_client.post("/api/pantry/plan/generate")
        assert r.status_code == 202
        pid = r.json()["plan_id"]
        mp.launch.assert_called_once_with(pid)
        r2 = pantry_client.post("/api/pantry/plan/generate")
        assert r2.status_code == 409 and r2.json()["plan_id"] == pid
        assert "status_code" not in r2.json()
        assert pantry_client.get("/api/pantry/plan").json()["draft"]["status"] == "generating"

    def test_generate_nothing_to_plan(self, pantry_client):
        pantry_client.put("/api/pantry/diet", json={"batch_lunch_days": 0, "fresh_dinners_per_week": 0})
        assert pantry_client.post("/api/pantry/plan/generate").status_code == 400

    def test_get_plan(self, pantry_client):
        pid = week_plan()
        r = pantry_client.get(f"/api/pantry/plan/{pid}")
        assert r.status_code == 200 and r.json()["status"] == "draft"

    def test_accept_and_discard_status_codes(self, pantry_client):
        pid = week_plan()
        r = pantry_client.post(f"/api/pantry/plan/{pid}/accept", json={"already_have": ["carrot"]})
        assert r.status_code == 200 and r.json()["added_to_pantry"] == 1
        assert pantry_client.post(f"/api/pantry/plan/{pid}/accept").status_code == 409
        assert pantry_client.post(f"/api/pantry/plan/{pid}/discard").status_code == 200
        assert pantry_client.post(f"/api/pantry/plan/{pid}/discard").status_code == 409

    def test_accept_bad_already_have(self, pantry_client):
        pid = week_plan()
        r = pantry_client.post(f"/api/pantry/plan/{pid}/accept", json={"already_have": "carrot"})
        assert r.status_code == 400

    def test_accept_garbage_body(self, pantry_client):
        pid = week_plan()
        r = pantry_client.post(
            f"/api/pantry/plan/{pid}/accept", content=b"not json", headers={"content-type": "text/plain"}
        )
        assert r.status_code == 200

    def test_swap(self, pantry_client, monkeypatch):
        pid = week_plan()
        eid = next(e["id"] for e in mp.get_plan(pid)["entries"] if e["slot"] == "dinner")
        monkeypatch.setattr(mp, "_ask", fake_ask([SWAP_REPLY]))
        r = pantry_client.post(f"/api/pantry/plan/entry/{eid}/swap")
        assert r.status_code == 200 and r.json()["ok"] is True
        assert any(rec["name"] == "Tofu stir fry" for rec in r.json()["plan"]["recipes"].values())

    def test_swap_asleep(self, pantry_client, monkeypatch):
        pid = week_plan()
        eid = mp.get_plan(pid)["entries"][0]["id"]
        _set_brain(monkeypatch, False)
        assert pantry_client.post(f"/api/pantry/plan/entry/{eid}/swap").status_code == 503

    def test_rate(self, pantry_client):
        pid = week_plan()
        rid = mp.get_plan(pid)["entries"][0]["recipe_id"]
        assert pantry_client.post(f"/api/pantry/recipe/{rid}/rate", json={"rating": "up"}).status_code == 200
        assert pantry_client.post(f"/api/pantry/recipe/{rid}/rate", json={"rating": ["up"]}).status_code == 400
        assert pantry_client.post(f"/api/pantry/recipe/{rid}/rate", json={"rating": {"a": 1}}).status_code == 400


# ---------------------------------------------------------------------------
# meal_plan chat tool
# ---------------------------------------------------------------------------


class TestMealPlanTool:
    @pytest.mark.parametrize("day", ["tomorrow", "2026-1-5", "2026-10-14T00:00", 20261014])
    async def test_bad_day(self, day):
        assert await meal_plan_tool({"action": "recipe", "day": day}) == "day must be YYYY-MM-DD."

    async def test_view_and_recipe(self):
        assert "no meal plan" in await meal_plan_tool({})
        start = date.today() + timedelta(days=2)
        week_plan()
        assert "DRAFT" in await meal_plan_tool({"action": "VIEW"})
        txt = await meal_plan_tool({"action": "recipe", "day": start.isoformat()})
        assert txt.startswith("Bean tacos")
        assert (await meal_plan_tool({"action": "recipe", "slot": "batch", "day": start.isoformat()})).startswith(
            "Barley stew"
        )

    async def test_generate(self, monkeypatch):
        _set_brain(monkeypatch, False)
        assert "asleep" in await meal_plan_tool({"action": "generate"})
        mp.launch.assert_not_called()
        _set_brain(monkeypatch, True)
        out = await meal_plan_tool({"action": "generate"})
        assert out.startswith("Started planning the week of")
        mp.launch.assert_called_once()
        assert "already being made" in await meal_plan_tool({"action": "generate"})

    async def test_accept(self):
        assert await meal_plan_tool({"action": "accept"}) == "There's no draft plan to accept."
        pid = week_plan()
        out = await meal_plan_tool({"action": "accept"})
        assert out.startswith("Plan accepted.") and "3 cooking reminder(s)" in out
        assert mp.get_plan(pid)["status"] == "active"

    async def test_swap(self, monkeypatch):
        assert (await meal_plan_tool({"action": "swap"})).startswith("No dinner in the plan")
        start = date.today() + timedelta(days=2)
        week_plan()
        monkeypatch.setattr(mp, "_ask", fake_ask([SWAP_REPLY]))
        out = await meal_plan_tool({"action": "swap", "day": start.isoformat()})
        assert out.startswith("Swapped.") and "Tofu stir fry" in out
        _set_brain(monkeypatch, False)
        assert "asleep" in await meal_plan_tool({"action": "swap", "day": start.isoformat()})
