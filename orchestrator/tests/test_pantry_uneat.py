"""Tests for un-eating a prepped portion and for cook undos removing their
meal log (F-015 phase 4 follow-up): pantry_consumption.uneat_portion,
undo_cook / undo_other -> selfcare_manager.unrecord_meal_logged,
state_store.delete_selfcare_log, POST /api/pantry/prep/{id}/uneat and the
meal_plan tool's ``uneat`` action.

Unlike test_pantry_consumption, the selfcare meal bridge is REAL here (the
``meals`` fixture is overridden to not patch it) so the selfcare_log rows and
``_state.last_meal_reported`` can be checked. No model, HA or push service is
ever called. Food names are invented/generic.
"""

# ruff: noqa: F811  (pytest fixtures imported from test_pantry_consumption)

from datetime import date, datetime, timedelta

import pytest

from orchestrator.tests.test_pantry_consumption import (  # noqa: F401 (fixtures)
    add,
    bare_recipe,
    batches,
    db,
    entry,
    insert_batch,
    make_plan,
    pc,
)


@pytest.fixture
def meals(monkeypatch):
    """Override: keep the real record_meal_logged; start the nudge gate clean."""
    from orchestrator import selfcare_manager

    monkeypatch.setattr(selfcare_manager._state, "last_meal_reported", None)
    return selfcare_manager


def meal_rows():
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM selfcare_log WHERE action = 'meal' ORDER BY id")]


def meal_details():
    return [r["detail"] for r in meal_rows()]


def insert_meal(detail, ago=None):
    ts = (datetime.now() - (ago or timedelta())).isoformat()
    with db() as conn:
        return conn.execute(
            "INSERT INTO selfcare_log (action, detail, logged_at) VALUES ('meal', ?, ?)", (detail, ts)
        ).lastrowid


def age_meals(**delta):
    ts = (datetime.now() - timedelta(**delta)).isoformat()
    with db() as conn:
        conn.execute("UPDATE selfcare_log SET logged_at = ? WHERE action = 'meal'", (ts,))


def batch(bid):
    return next(b for b in batches() if b["id"] == bid)


def last_meal():
    from orchestrator import selfcare_manager

    return selfcare_manager._state.last_meal_reported


def cooked_batch(pc, portions=2, lunch_days=None):
    """A real cooked prep batch with linked lunches (today first)."""
    today = date.today()
    add("Pearl barley", qty=2)
    p = make_plan(
        prep=[("pearl barley", "1 bag")],
        portions=portions,
        lunch_days=lunch_days if lunch_days is not None else [today, today + timedelta(days=1)],
    )
    assert pc.cook_entry(p["prep_id"])["ok"]
    return p, batches()[0]["id"]


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


class TestLabels:
    def test_round_trip(self, pc):
        assert pc._parse_portion_label(pc._portion_label(7, 2, 31)) == {"batch_id": 7, "left_after": 2, "entry_id": 31}

    def test_no_entry_is_none(self, pc):
        assert pc._parse_portion_label(pc._portion_label(7, 0, 0)) == {"batch_id": 7, "left_after": 0, "entry_id": None}

    def test_legacy(self, pc):
        assert pc._parse_portion_label("portion:12") == {"batch_id": 12, "left_after": None, "entry_id": None}

    @pytest.mark.parametrize("bad", ["", "portion:", "portion:x", "cooked:3", "portionx:1", None, 5])
    def test_garbage(self, pc, bad):
        assert pc._parse_portion_label(bad) is None


# ---------------------------------------------------------------------------
# uneat_portion
# ---------------------------------------------------------------------------


class TestUneat:
    def test_round_trip(self, pc):
        p, bid = cooked_batch(pc)
        earlier = insert_meal("breakfast", ago=timedelta(hours=3))
        res = pc.eat_portion(bid)
        assert res["portions_left"] == 1
        assert entry(p["lunch_ids"][0])["status"] == "eaten"
        assert len(meal_rows()) == 2
        assert last_meal() is not None and last_meal() > datetime.now() - timedelta(minutes=1)

        before = pc.PANTRY_PORTIONS_EATEN.labels(source="undo")._value.get()
        res = pc.uneat_portion()
        assert res == {
            "ok": True,
            "batch_id": bid,
            "recipe": "Barley stew",
            "portions_left": 2,
            "portion_restored": True,
        }
        assert (batch(bid)["portions_left"], batch(bid)["status"]) == (2, "active")
        assert entry(p["lunch_ids"][0])["status"] == "planned"
        assert entry(p["lunch_ids"][1])["status"] == "planned"
        assert [r["id"] for r in meal_rows()] == [earlier]
        # Nudge gate recomputed from what's left: the earlier breakfast.
        assert last_meal() < datetime.now() - timedelta(hours=2)
        assert pc.PANTRY_PORTIONS_EATEN.labels(source="undo")._value.get() == before + 1

    def test_last_meal_none_when_nothing_left(self, pc):
        _, bid = cooked_batch(pc)
        pc.eat_portion(bid)
        assert pc.uneat_portion()["ok"]
        assert meal_rows() == [] and last_meal() is None

    def test_eat_again_after_uneat_logs_again(self, pc):
        _, bid = cooked_batch(pc)
        pc.eat_portion(bid)
        pc.uneat_portion()
        pc.eat_portion(bid)
        assert len(meal_rows()) == 1 and batch(bid)["portions_left"] == 1

    def test_two_eats_two_uneats_then_404(self, pc):
        p, bid = cooked_batch(pc, portions=3)
        pc.eat_portion(bid)
        pc.eat_portion(bid)
        # Distinct labels, so the 5-minute selfcare dedup didn't merge them.
        details = meal_details()
        assert len(details) == 2 and details[0] != details[1]
        assert [entry(i)["status"] for i in p["lunch_ids"]] == ["eaten", "eaten"]

        first = pc.uneat_portion()
        assert first["portions_left"] == 2 and first["portion_restored"] is True
        # Newest eat undone first: it ticked the second lunch.
        assert [entry(i)["status"] for i in p["lunch_ids"]] == ["eaten", "planned"]
        second = pc.uneat_portion(bid)
        assert second["portions_left"] == 3
        assert [entry(i)["status"] for i in p["lunch_ids"]] == ["planned", "planned"]
        assert meal_rows() == []

        third = pc.uneat_portion()
        assert third["ok"] is False and third["status_code"] == 404
        assert pc.uneat_portion(bid)["status_code"] == 404
        assert batch(bid)["portions_left"] == 3

    def test_last_portion_reactivates_batch(self, pc):
        bid = insert_batch(bare_recipe(), date.today() + timedelta(days=2), portions=1)
        assert pc.eat_portion(bid)["portions_left"] == 0
        assert batch(bid)["status"] == "finished"
        res = pc.uneat_portion(bid)
        assert res["portion_restored"] is True and res["portions_left"] == 1
        assert (batch(bid)["portions_left"], batch(bid)["status"]) == (1, "active")
        assert meal_rows() == []

    def test_binned_after_eat_keeps_count(self, pc):
        bid = insert_batch(bare_recipe(), date.today() + timedelta(days=2), portions=3)
        pc.eat_portion(bid)
        assert pc.finish_batch(bid) == {"ok": True}
        res = pc.uneat_portion()
        assert res["ok"] is True and res["portion_restored"] is False and res["portions_left"] == 0
        assert (batch(bid)["portions_left"], batch(bid)["status"]) == (0, "finished")
        assert meal_rows() == []

    def test_expired_keeps_count(self, pc):
        bid = insert_batch(bare_recipe(), date.today() + timedelta(days=2), portions=3)
        pc.eat_portion(bid)
        with db() as conn:
            conn.execute("UPDATE prep_batches SET status = 'expired' WHERE id = ?", (bid,))
        res = pc.uneat_portion(bid)
        assert res["ok"] is True and res["portion_restored"] is False and res["portions_left"] == 2
        assert (batch(bid)["portions_left"], batch(bid)["status"]) == (2, "expired")
        assert meal_rows() == []

    def test_older_than_window_404_nothing_changed(self, pc):
        p, bid = cooked_batch(pc)
        pc.eat_portion(bid)
        age_meals(hours=pc.UNEAT_WINDOW_HOURS, minutes=5)
        rows = meal_rows()
        assert pc.uneat_portion()["status_code"] == 404
        assert pc.uneat_portion(bid)["status_code"] == 404
        assert meal_rows() == rows
        assert batch(bid)["portions_left"] == 1
        assert entry(p["lunch_ids"][0])["status"] == "eaten"

    def test_explicit_batch_picks_its_own_newest(self, pc):
        a = insert_batch(bare_recipe("Lentil soup"), date.today() + timedelta(days=2), portions=3)
        b = insert_batch(bare_recipe("Chickpea curry"), date.today() + timedelta(days=3), portions=3)
        pc.eat_portion(a)
        pc.eat_portion(a)
        pc.eat_portion(b)  # newest overall
        res = pc.uneat_portion(a)
        assert res["batch_id"] == a and res["recipe"] == "Lentil soup" and res["portions_left"] == 2
        assert batch(b)["portions_left"] == 2
        details = meal_details()
        assert details == [pc._portion_label(a, 2, 0), pc._portion_label(b, 2, 0)]

    def test_explicit_batch_prefix_not_confused(self, pc):
        """portion:1:… must not match a request for batch 11 (or vice versa)."""
        r = bare_recipe()
        ids = [insert_batch(r, date.today() + timedelta(days=2)) for _ in range(11)]
        pc.eat_portion(ids[0])  # batch 1
        assert pc.uneat_portion(ids[10])["status_code"] == 404  # batch 11
        assert len(meal_rows()) == 1

    def test_without_id_takes_newest_overall(self, pc):
        a = insert_batch(bare_recipe("Lentil soup"), date.today() + timedelta(days=2), portions=3)
        b = insert_batch(bare_recipe("Chickpea curry"), date.today() + timedelta(days=3), portions=3)
        pc.eat_portion(a)
        pc.eat_portion(b)
        assert pc.uneat_portion()["batch_id"] == b
        assert batch(a)["portions_left"] == 2

    def test_legacy_label(self, pc):
        bid = insert_batch(bare_recipe(), date.today() + timedelta(days=2), portions=3, left=2)
        insert_meal(f"portion:{bid}")
        res = pc.uneat_portion(bid)
        assert res["ok"] is True and res["portion_restored"] is True and res["portions_left"] == 3
        assert meal_rows() == []

    def test_legacy_label_without_id(self, pc):
        bid = insert_batch(bare_recipe(), date.today() + timedelta(days=2), portions=3, left=2)
        insert_meal(f"portion:{bid}")
        assert pc.uneat_portion()["batch_id"] == bid

    def test_never_above_portions_made(self, pc):
        bid = insert_batch(bare_recipe(), date.today() + timedelta(days=2), portions=3)
        insert_meal(pc._portion_label(bid, 2, 0))  # log without a matching decrement
        res = pc.uneat_portion(bid)
        assert res["ok"] is True and res["portion_restored"] is False and res["portions_left"] == 3

    def test_batch_gone_404(self, pc):
        insert_meal(pc._portion_label(424242, 1, 0))
        res = pc.uneat_portion()
        assert res["status_code"] == 404 and res["error"] == "batch not found"
        assert len(meal_rows()) == 1

    def test_non_portion_meals_ignored(self, pc):
        insert_meal("cooked:5")
        insert_meal("sandwich")
        assert pc.uneat_portion()["status_code"] == 404
        assert len(meal_rows()) == 2

    def test_lunch_status_changed_since_left_alone(self, pc):
        p, bid = cooked_batch(pc)
        pc.eat_portion(bid)
        with db() as conn:
            conn.execute("UPDATE meal_plan_entries SET status = 'skipped' WHERE id = ?", (p["lunch_ids"][0],))
        assert pc.uneat_portion()["portion_restored"] is True
        assert entry(p["lunch_ids"][0])["status"] == "skipped"

    def test_refresh_failure_never_breaks_undo(self, pc, monkeypatch):
        from orchestrator import selfcare_manager

        bid = insert_batch(bare_recipe(), date.today() + timedelta(days=2), portions=2)
        pc.eat_portion(bid)

        def boom():
            raise RuntimeError("selfcare down")

        monkeypatch.setattr(selfcare_manager, "refresh_last_meal", boom)
        assert pc.uneat_portion()["portion_restored"] is True
        assert meal_rows() == []


# ---------------------------------------------------------------------------
# Cook undos remove their meal log
# ---------------------------------------------------------------------------


class TestUndoRemovesMealLog:
    def test_undo_cook_dinner(self, pc):
        p = make_plan(dinner=[("rice", "1 cup")])
        other = insert_meal("sandwich", ago=timedelta(hours=1))
        assert pc.cook_entry(p["dinner_id"])["ok"]
        assert meal_details() == ["sandwich", f"cooked:{p['dinner_id']}"]
        assert pc.undo_cook(p["dinner_id"])["ok"]
        assert [r["id"] for r in meal_rows()] == [other]
        assert last_meal() < datetime.now() - timedelta(minutes=30)

    def test_undo_cook_leaves_other_entries_logs(self, pc):
        p1 = make_plan(dinner=[("rice", "1 cup")])
        p2 = make_plan(dinner=[("rice", "1 cup")])
        pc.cook_entry(p1["dinner_id"])
        pc.cook_entry(p2["dinner_id"])
        pc.undo_cook(p1["dinner_id"])
        assert meal_details() == [f"cooked:{p2['dinner_id']}"]

    def test_undo_cook_prep_logs_no_meal(self, pc):
        p = make_plan(prep=[("pearl barley", "1 bag")], portions=2)
        insert_meal("sandwich")
        pc.cook_entry(p["prep_id"])
        assert pc.undo_cook(p["prep_id"])["ok"]
        assert meal_details() == ["sandwich"]

    def test_undo_cook_old_log_kept(self, pc):
        p = make_plan(dinner=[("rice", "1 cup")])
        pc.cook_entry(p["dinner_id"])
        age_meals(hours=pc.UNEAT_WINDOW_HOURS + 1)
        assert pc.undo_cook(p["dinner_id"])["ok"]  # the cook undo still happens
        assert meal_details() == [f"cooked:{p['dinner_id']}"]

    def test_unlog_failure_never_breaks_undo(self, pc, monkeypatch):
        from orchestrator import selfcare_manager

        def boom(label, since):
            raise RuntimeError("selfcare down")

        monkeypatch.setattr(selfcare_manager, "unrecord_meal_logged", boom)
        p = make_plan(dinner=[("rice", "1 cup")])
        pc.cook_entry(p["dinner_id"])
        assert pc.undo_cook(p["dinner_id"])["ok"]
        assert entry(p["dinner_id"])["status"] == "planned"

    def test_undo_other(self, pc):
        wraps = add("Tortillas", qty=4)
        insert_meal("sandwich", ago=timedelta(hours=1))
        res = pc.cook_other([wraps["id"]], dish="Veggie tacos")
        assert res["ok"], res
        cid = res["cook_id"]
        assert meal_details() == ["sandwich", f"cooked_other:{cid}"]
        undo = pc.undo_other()
        assert undo["ok"] and undo["cook_id"] == cid
        assert meal_details() == ["sandwich"]


class TestDeleteSelfcareLog:
    def test_newest_matching_in_window_only(self, tmp_db):
        from orchestrator import state_store

        since = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        old = insert_meal("cooked:1", ago=timedelta(hours=2))
        a = insert_meal("cooked:1", ago=timedelta(minutes=10))
        b = insert_meal("cooked:1")
        keep = insert_meal("cooked:2")
        with db() as conn:
            conn.execute(
                "INSERT INTO selfcare_log (action, detail, logged_at) VALUES ('water', 'cooked:1', ?)",
                (datetime.now().isoformat(),),
            )
        assert state_store.delete_selfcare_log("meal", "cooked:1", since) is not None
        assert [r["id"] for r in meal_rows()] == [old, a, keep]
        assert state_store.delete_selfcare_log("meal", "cooked:1", since) is not None
        assert state_store.delete_selfcare_log("meal", "cooked:1", since) is None
        assert [r["id"] for r in meal_rows()] == [old, keep]
        assert b not in [r["id"] for r in meal_rows()]

    def test_unrecord_returns_false_when_nothing(self, tmp_db, meals):
        assert meals.unrecord_meal_logged("cooked:9", "2000-01-01T00:00:00") is False


# ---------------------------------------------------------------------------
# Route + tool
# ---------------------------------------------------------------------------


@pytest.fixture
def client(pc):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.routes_pantry import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestRoute:
    def test_uneat(self, client):
        bid = insert_batch(bare_recipe("Lentil soup"), date.today() + timedelta(days=2), portions=2)
        assert client.post(f"/api/pantry/prep/{bid}/eat").status_code == 200
        r = client.post(f"/api/pantry/prep/{bid}/uneat")
        assert r.status_code == 200
        assert r.json() == {
            "ok": True,
            "batch_id": bid,
            "recipe": "Lentil soup",
            "portions_left": 2,
            "portion_restored": True,
        }
        r = client.post(f"/api/pantry/prep/{bid}/uneat")
        assert r.status_code == 404 and r.json()["ok"] is False and "status_code" not in r.json()

    def test_bad_ids(self, client):
        assert client.post("/api/pantry/prep/0/uneat").status_code == 404
        assert client.post(f"/api/pantry/prep/{2**70}/uneat").status_code == 404
        assert client.post("/api/pantry/prep/4242/uneat").status_code == 404
        assert client.post("/api/pantry/prep/abc/uneat").status_code == 422


class TestTool:
    @pytest.fixture
    def tool(self, pc):
        from orchestrator.tool_handlers import _reg_meal_plan

        return _reg_meal_plan

    async def test_uneat_restored(self, tool):
        bid = insert_batch(bare_recipe("Lentil soup"), date.today() + timedelta(days=2), portions=2)
        await tool({"action": "ate"})
        out = await tool({"action": "uneat"})
        assert out.startswith("Put the portion of Lentil soup back") and out.endswith("2 left.")
        assert batch(bid)["portions_left"] == 2 and meal_rows() == []

    async def test_uneat_nothing(self, tool):
        assert await tool({"action": "uneat"}) == "no portion eaten in the last day to undo"

    async def test_uneat_not_restored(self, tool):
        bid = insert_batch(bare_recipe("Lentil soup"), date.today() + timedelta(days=2), portions=3)
        await tool({"action": "ate"})
        from orchestrator import pantry_consumption

        pantry_consumption.finish_batch(bid)
        out = await tool({"action": "uneat"})
        assert out.startswith("Removed the meal log for Lentil soup") and "can't go back" in out
        assert batch(bid)["portions_left"] == 0
