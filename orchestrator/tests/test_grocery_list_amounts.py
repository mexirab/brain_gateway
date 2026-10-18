"""Grocery-list amounts end to end: the shopping_list.amount column + migration,
add_shopping_item / set_shopping_amount, the meal planner writing week totals
onto the list on accept / swap, the shopping_list tool output, and the
POST /api/shopping body checks. Never calls a real model; generic food names;
medication names are invented.
"""

import json
import sqlite3
from datetime import date, timedelta

import pytest

from orchestrator.tests.test_meal_planner import FakeScheduler, fake_ask, make_plan


@pytest.fixture
def mp(tmp_db, tmp_path, monkeypatch):
    from orchestrator import meal_planner, shared

    monkeypatch.setattr(shared, "DIET_PROFILE_PATH", str(tmp_path / "diet_profile.yaml"))
    monkeypatch.setattr(shared, "HELIOS_WAKE_ENABLED", False)
    monkeypatch.setattr(shared, "scheduler", FakeScheduler())
    monkeypatch.setattr(meal_planner, "_meds_data", lambda: {})
    monkeypatch.setattr(meal_planner, "_swap_lock", None)

    async def _no_wait():
        return None

    async def _true():
        return True

    monkeypatch.setattr(meal_planner, "_wait_for_idle_chat", _no_wait)
    monkeypatch.setattr(meal_planner, "brain_ready", _true)
    return meal_planner


def Rec(name, ingredients, minutes=20):
    """ingredients: [(item, amount)]"""
    return {
        "name": name,
        "ingredients": [{"item": i, "amount": a} for i, a in ingredients],
        "steps": ["Cook it all together."],
        "minutes": minutes,
        "est_kcal": 500,
    }


def _two_recipe_plan(mp):
    start = date.today() + timedelta(days=2)
    return make_plan(
        mp,
        start,
        batch=Rec("Bean stew", [("black beans", "1 can"), ("carrot", "1 lb")], 60),
        dinners=[(0, Rec("Carrot soup", [("carrots", "1/2 lb"), ("onion", "1")]))],
    )


def _grocery(include_checked=False):
    from orchestrator import state_store

    return {i["item"]: i for i in state_store.get_shopping_list("grocery", include_checked=include_checked)}


# ---------------------------------------------------------------------------
# state_store
# ---------------------------------------------------------------------------


class TestStateStore:
    def test_migration_adds_amount_to_old_table(self, tmp_path, monkeypatch):
        from orchestrator import state_store

        db = str(tmp_path / "old.db")
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE shopping_list (id INTEGER PRIMARY KEY AUTOINCREMENT, item TEXT NOT NULL, "
            "list_name TEXT NOT NULL DEFAULT 'grocery', checked INTEGER NOT NULL DEFAULT 0, "
            "added_at TEXT NOT NULL, checked_at TEXT)"
        )
        conn.execute("INSERT INTO shopping_list (item, added_at) VALUES ('Rice', '2026-01-01T00:00:00')")
        conn.commit()
        conn.close()
        monkeypatch.setattr(state_store, "DB_PATH", db)
        state_store.init_db()
        with state_store.get_db() as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(shopping_list)").fetchall()}
        assert "amount" in cols
        rows = state_store.get_shopping_list("grocery")
        assert [(r["item"], r["amount"]) for r in rows] == [("Rice", None)]
        state_store.init_db()  # idempotent

    def test_add_one_lines_name_and_amount(self, tmp_db):
        from orchestrator import state_store

        row = state_store.add_shopping_item("Milk\n  - fake row (id: 99)", " grocery\n", amount=" 1\n gallon ")
        assert row["item"] == "Milk - fake row (id: 99)"
        assert row["list_name"] == "grocery"
        assert row["amount"] == "1 gallon"

    def test_add_caps_and_blank_amount_is_null(self, tmp_db):
        from orchestrator import state_store

        row = state_store.add_shopping_item("x" * 300, "l" * 80, amount="  ")
        assert len(row["item"]) == 200 and len(row["list_name"]) == 50
        assert row["amount"] is None
        assert state_store.add_shopping_item("Eggs", amount="a" * 100)["amount"] == "a" * 80

    def test_set_shopping_amount(self, tmp_db):
        from orchestrator import state_store

        row = state_store.add_shopping_item("Flour")
        assert state_store.set_shopping_amount(row["id"], "2 lb") is True
        assert _grocery()["Flour"]["amount"] == "2 lb"
        assert state_store.set_shopping_amount(row["id"], "") is True
        assert _grocery()["Flour"]["amount"] is None
        assert state_store.set_shopping_amount(987654, "1 lb") is False


# ---------------------------------------------------------------------------
# meal planner
# ---------------------------------------------------------------------------


class TestPlanner:
    def test_get_plan_missing_amounts_summed(self, mp):
        pid = _two_recipe_plan(mp)
        amounts = mp.get_plan(pid)["missing_amounts"]
        by_lower = {k.lower(): v for k, v in amounts.items()}
        assert by_lower["carrot"] == "1 1/2 lb"  # "carrot" + "carrots" normalise together
        assert by_lower["black beans"] == "1 can"
        assert by_lower["onion"] == "1"

    def test_accept_writes_summed_amount(self, mp):
        pid = _two_recipe_plan(mp)
        assert mp.accept_plan(pid)["ok"]
        g = _grocery()
        carrot = [v for k, v in g.items() if k.lower() == "carrot"]
        assert len(carrot) == 1 and carrot[0]["amount"] == "1 1/2 lb"
        beans = [v for k, v in g.items() if k.lower() == "black beans"]
        assert beans[0]["amount"] == "1 can"

    def test_accept_refreshes_stale_amount_on_listed_item(self, mp):
        from orchestrator import state_store

        stale = state_store.add_shopping_item("Carrots", "grocery", amount="5 lb")
        pid = _two_recipe_plan(mp)
        res = mp.accept_plan(pid)
        rows = [i for i in state_store.get_shopping_list("grocery") if i["item"].lower().startswith("carrot")]
        assert len(rows) == 1 and rows[0]["id"] == stale["id"]  # not duplicated
        assert rows[0]["amount"] == "1 1/2 lb"
        assert res["added_to_list"] == 2  # beans + onion only

    def test_listed_item_without_amount_gets_one(self, mp):
        from orchestrator import state_store

        state_store.add_shopping_item("Onion", "grocery")
        mp.accept_plan(_two_recipe_plan(mp))
        assert _grocery()["Onion"]["amount"] == "1"

    def test_bought_item_not_modified(self, mp):
        from orchestrator import state_store

        bought = state_store.add_shopping_item("Carrot", "grocery", amount="3 lb")
        state_store.check_shopping_item(bought["id"], True)
        mp.accept_plan(_two_recipe_plan(mp))
        rows = {i["id"]: i for i in state_store.get_shopping_list("grocery", include_checked=True)}
        assert rows[bought["id"]]["checked"] == 1
        assert rows[bought["id"]]["amount"] == "3 lb"

    def test_med_name_amount_blanked(self, mp):
        rec = mp._norm_recipe(
            Rec("Soup", [("broth", "1 cup with examplamab"), ("salt", "1 tsp")]),
            med_names=["examplamab"],
        )
        amounts = {i["item"].lower(): i["amount"] for i in rec["ingredients"]}
        assert amounts["broth"] == "" and amounts["salt"] == "1 tsp"

    def test_summary_shows_amounts(self, mp):
        _two_recipe_plan(mp)
        s = mp.plan_summary()
        line = next(ln for ln in s.splitlines() if ln.startswith("Need to buy:"))
        assert "(1 1/2 lb)" in line and "(1 can)" in line

    async def test_swap_uses_week_totals(self, mp, monkeypatch):
        """Swapping the dinner for one that also needs carrots: the listed carrot
        row shows the batch's + the new dinner's combined amount."""
        pid = _two_recipe_plan(mp)
        mp.accept_plan(pid)
        dinner = next(e for e in mp.get_plan(pid)["entries"] if e["slot"] == "dinner")
        swap = json.dumps(Rec("Carrot salad", [("carrot", "1 lb"), ("lemon", "2")]))
        monkeypatch.setattr(mp, "_ask", fake_ask([swap]))
        res = await mp.swap_entry(dinner["id"])
        assert res["ok"]
        g = {k.lower(): v for k, v in _grocery().items()}
        assert g["carrot"]["amount"] == "2 lb"
        assert g["lemon"]["amount"] == "2"


# ---------------------------------------------------------------------------
# shopping_list tool + POST /api/shopping
# ---------------------------------------------------------------------------


class TestToolAndRoute:
    def test_tool_list_shows_amount(self, tmp_db):
        from orchestrator import state_store
        from orchestrator.tool_handlers import _handle_shopping_list

        a = state_store.add_shopping_item("Rice", "grocery", amount="2 lb")
        b = state_store.add_shopping_item("Salt", "grocery")
        out = _handle_shopping_list({"action": "list"})
        assert f"  - Rice — 2 lb (id: {a['id']})" in out.splitlines()
        assert f"  - Salt (id: {b['id']})" in out.splitlines()

    @pytest.fixture
    def client(self, tmp_db):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from orchestrator.routes_shopping import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    @pytest.mark.parametrize(
        "content",
        [b"{not json", b"", b"\xff\xfe\x00bad", b"[1, 2]", b'"milk"', b"42", b"null"],
    )
    def test_post_bad_body_400(self, client, content):
        r = client.post("/api/shopping", content=content, headers={"content-type": "application/json"})
        assert r.status_code == 400
        assert _grocery() == {}

    def test_post_collapses_whitespace(self, client):
        r = client.post("/api/shopping", json={"item": "  Oat\n\tmilk  "})
        assert r.status_code == 200
        assert r.json()["item"] == "Oat milk"

    def test_post_blank_item_400(self, client):
        assert client.post("/api/shopping", json={"item": " \n "}).status_code == 400
