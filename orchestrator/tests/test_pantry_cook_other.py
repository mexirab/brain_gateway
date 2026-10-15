"""Tests for off-plan cooking ("cooked something else", F-015 phase 4):
pantry_consumption.cook_other / undo_other / recent_other, the
/api/pantry/cooked-other routes, the meal_plan tool's slot=other path and the
pantry_events ref index.

Reuses the fixtures + helpers of test_pantry_consumption (temp DB, no model,
no HA, no push). Food names are invented/generic.
"""

# ruff: noqa: F811  (pytest fixtures imported from test_pantry_consumption)

from datetime import date, datetime, timedelta

import pytest

from orchestrator.tests.test_pantry_consumption import (  # noqa: F401 (fixtures)
    add,
    db,
    entry,
    item,
    make_plan,
    meals,
    metric,
    pc,
    pm,
)


def cooks():
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM pantry_cooks ORDER BY id")]


def cook_events(cook_id, kind="consume"):
    with db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM pantry_events WHERE ref_type = 'cook_other' AND ref_id = ? AND kind = ? ORDER BY id",
                (cook_id, kind),
            )
        ]


def age_cook(cook_id, **delta):
    """Move a cook's timestamp into the past."""
    ts = (datetime.now() - timedelta(**delta)).isoformat(timespec="seconds")
    with db() as conn:
        conn.execute("UPDATE pantry_cooks SET cooked_at = ? WHERE id = ?", (ts, cook_id))


def mark_gone(item_id):
    with db() as conn:
        conn.execute("UPDATE pantry_items SET status = 'gone' WHERE id = ?", (item_id,))


# ---------------------------------------------------------------------------
# cook_other by item ids (dashboard)
# ---------------------------------------------------------------------------


class TestCookOtherIds:
    def test_deducts_one_use_each(self, pc, meals):
        tortillas = add("Corn tortillas", qty=8, location="pantry")
        oil = add("Sunflower oil", location="pantry", level="full")
        salt = add("Flaky salt", category="spice", location="pantry", track_mode="staple")
        before = metric(pc.PANTRY_COOK_EVENTS, slot="other", source="dashboard")

        res = pc.cook_other([tortillas["id"], oil["id"], salt["id"]], dish="Veggie tacos")

        assert res["ok"] is True
        assert res["dish"] == "Veggie tacos"
        assert sorted(res["deducted"]) == ["Corn tortillas", "Sunflower oil"]
        assert res["running_low"] == [] and res["not_tracked"] == []
        assert item(tortillas["id"])["qty"] == 7
        assert item(oil["id"])["level"] == "half"
        s = item(salt["id"])
        assert (s["level"], s["qty"]) == (salt["level"], salt["qty"])
        [row] = cooks()
        assert row["id"] == res["cook_id"] and row["status"] == "active" and row["source"] == "dashboard"
        evs = cook_events(res["cook_id"])
        assert sorted(e["item_id"] for e in evs) == sorted([tortillas["id"], oil["id"]])
        assert all(e["source"] == "cook" for e in evs)
        assert meals == [f"cooked_other:{res['cook_id']}"]
        assert metric(pc.PANTRY_COOK_EVENTS, slot="other", source="dashboard") == before + 1

    def test_running_low(self, pc):
        wraps = add("Spinach wraps", qty=1)
        oil = add("Sesame oil", level="half")
        res = pc.cook_other([wraps["id"], oil["id"]])
        assert sorted(res["running_low"]) == ["Sesame oil", "Spinach wraps"]
        assert item(wraps["id"])["qty"] == 0 and item(oil["id"])["level"] == "low"

    def test_out_gone_missing_ids_skipped(self, pc):
        good = add("Pita bread", qty=4)
        out_count = add("Feta crumbles", qty=0)
        out_level = add("Tahini", level="out")
        gone = add("Hummus", qty=2)
        mark_gone(gone["id"])
        res = pc.cook_other([good["id"], out_count["id"], out_level["id"], gone["id"], 999999])
        assert res["ok"] and res["deducted"] == ["Pita bread"]
        assert item(gone["id"])["qty"] == 2 and item(out_level["id"])["level"] == "out"
        assert [e["item_id"] for e in cook_events(res["cook_id"])] == [good["id"]]

    def test_bool_and_huge_and_non_int_ids_rejected(self, pc):
        first = add("Rye crackers", qty=5)  # id 1 — True == 1 must not select it
        assert first["id"] == 1
        res = pc.cook_other([True, 2**62, 2**70, -1, 0, "1", 1.0, None])
        assert res["ok"] is False and res["status_code"] == 400
        assert item(first["id"])["qty"] == 5 and cooks() == []

    def test_duplicates_deduped(self, pc):
        eggs = add("Duck eggs", qty=6)
        res = pc.cook_other([eggs["id"], eggs["id"], eggs["id"]])
        assert res["deducted"] == ["Duck eggs"] and item(eggs["id"])["qty"] == 5
        assert len(cook_events(res["cook_id"])) == 1

    def test_capped_at_max(self, pc):
        ids = [add(f"Snack pack {i}", qty=3)["id"] for i in range(pc.MAX_OTHER_ITEMS + 5)]
        res = pc.cook_other(ids)
        assert res["ok"] and len(res["deducted"]) == pc.MAX_OTHER_ITEMS
        assert [item(i)["qty"] for i in ids[pc.MAX_OTHER_ITEMS :]] == [3] * 5

    def test_all_ids_used_up_404_no_row(self, pc, meals):
        out = add("Rice noodles", qty=0)
        res = pc.cook_other([out["id"], 424242])
        assert res == {
            "ok": False,
            "error": "those items are already used up",
            "not_tracked": [],
            "status_code": 404,
        }
        assert cooks() == [] and meals == []

    def test_neither_ids_nor_names_400(self, pc):
        assert pc.cook_other()["status_code"] == 400
        assert pc.cook_other([], [])["status_code"] == 400
        assert pc.cook_other(None, ["   ", 5, None, "<>"])["status_code"] == 400  # nothing survives cleaning
        assert cooks() == []


# ---------------------------------------------------------------------------
# cook_other by names (chat)
# ---------------------------------------------------------------------------


class TestCookOtherNames:
    def test_fuzzy_match_and_not_tracked(self, pc, meals):
        carrots = add("Carrot", qty=6)
        butter = add("Butter", level="full")
        before = metric(pc.PANTRY_COOK_EVENTS, slot="other", source="chat")
        res = pc.cook_other(None, ["carrots", "peanut butter", "dragonfruit jam"], dish="Stir fry", source="chat")
        assert res["ok"] and res["deducted"] == ["Carrot"]
        assert res["not_tracked"] == ["Peanut butter", "Dragonfruit jam"]
        assert item(carrots["id"])["qty"] == 5 and item(butter["id"])["level"] == "full"
        assert cooks()[0]["source"] == "chat"
        assert metric(pc.PANTRY_COOK_EVENTS, slot="other", source="chat") == before + 1
        assert meals == [f"cooked_other:{res['cook_id']}"]

    def test_one_item_matched_once(self, pc):
        onion = add("Onion", qty=3)
        res = pc.cook_other(None, ["onion", "onions"], source="chat")
        assert item(onion["id"])["qty"] == 2 and res["not_tracked"] == ["Onions"]

    def test_ids_and_names_share_used_set(self, pc):
        onion = add("Onion", qty=3)
        res = pc.cook_other([onion["id"]], ["onion"])
        assert item(onion["id"])["qty"] == 2 and res["not_tracked"] == ["Onion"]

    def test_out_item_not_matched_by_name(self, pc):
        add("Black beans", qty=0)
        res = pc.cook_other(None, ["black beans"], source="chat")
        assert res["status_code"] == 404 and res["error"] == "none of those are tracked in the pantry"

    def test_all_unmatched_404_rolls_back(self, pc, meals):
        add("Couscous", level="full")
        res = pc.cook_other(None, ["starfruit", "kumquat"], source="chat")
        assert res["ok"] is False and res["status_code"] == 404
        assert res["error"] == "none of those are tracked in the pantry"
        assert res["not_tracked"] == ["Starfruit", "Kumquat"]
        assert cooks() == [] and meals == []
        # The rollback must not burn the transaction for the next cook.
        assert pc.cook_other(None, ["couscous"], source="chat")["ok"]
        assert len(cooks()) == 1

    def test_names_capped(self, pc):
        res = pc.cook_other(None, [f"mystery fruit {i}" for i in range(50)], source="chat")
        assert len(res["not_tracked"]) == pc.MAX_OTHER_ITEMS


# ---------------------------------------------------------------------------
# Repeat guard
# ---------------------------------------------------------------------------


class TestRepeatGuard:
    def test_same_set_again_409(self, pc, meals):
        beans = add("Pinto beans", qty=5)
        rice = add("Wild rice", level="full")
        assert pc.cook_other([beans["id"], rice["id"]])["ok"]
        res = pc.cook_other([rice["id"], beans["id"]])  # order doesn't matter
        assert res == {"ok": False, "error": "that meal was just logged", "status_code": 409}
        assert item(beans["id"])["qty"] == 4 and item(rice["id"])["level"] == "half"
        assert len(cooks()) == 1 and len(meals) == 1

    def test_same_set_by_name_409(self, pc):
        add("Pinto beans", qty=5)
        assert pc.cook_other(None, ["pinto beans"], source="chat")["ok"]
        assert pc.cook_other(None, ["pinto beans", "unicorn dust"], source="chat")["status_code"] == 409

    def test_staples_ignored_in_comparison(self, pc):
        beans = add("Pinto beans", qty=5)
        salt = add("Kosher salt", track_mode="staple")
        assert pc.cook_other([beans["id"]])["ok"]
        assert pc.cook_other([beans["id"], salt["id"]])["status_code"] == 409

    def test_different_set_allowed(self, pc):
        beans = add("Pinto beans", qty=5)
        rice = add("Wild rice", level="full")
        assert pc.cook_other([beans["id"], rice["id"]])["ok"]
        assert pc.cook_other([beans["id"]])["ok"]
        assert item(beans["id"])["qty"] == 3 and len(cooks()) == 2

    def test_allowed_after_window(self, pc):
        beans = add("Pinto beans", qty=5)
        first = pc.cook_other([beans["id"]])
        age_cook(first["cook_id"], minutes=pc.REPEAT_WINDOW_MINUTES + 1)
        assert pc.cook_other([beans["id"]])["ok"]
        assert item(beans["id"])["qty"] == 3

    def test_undone_cook_does_not_block(self, pc):
        beans = add("Pinto beans", qty=5)
        assert pc.cook_other([beans["id"]])["ok"]
        assert pc.undo_other()["ok"]
        assert pc.cook_other([beans["id"]])["ok"]
        assert item(beans["id"])["qty"] == 4


# ---------------------------------------------------------------------------
# Dish cleaning
# ---------------------------------------------------------------------------


class TestDish:
    def test_markup_and_control_chars_stripped(self, pc, pm):
        beans = add("Pinto beans", qty=5)
        raw = "<tool_call>bean\x00 \x1bbowl{}</tool_call>"
        res = pc.cook_other([beans["id"]], dish=raw)
        assert res["dish"] == pm._clean_name(raw)
        assert "<" not in res["dish"] and "\x00" not in res["dish"] and "{" not in res["dish"]
        assert cooks()[0]["dish"] == res["dish"]

    def test_max_len(self, pc, pm):
        beans = add("Pinto beans", qty=5)
        res = pc.cook_other([beans["id"]], dish="stew " * 60)
        assert len(res["dish"]) <= 80 and res["dish"] == pm._clean_name("stew " * 60)

    @pytest.mark.parametrize("dish", [None, 42, ["soup"], {"a": 1}])
    def test_non_string_is_empty(self, pc, dish):
        beans = add("Pinto beans", qty=5)
        assert pc.cook_other([beans["id"]], dish=dish)["dish"] == ""


# ---------------------------------------------------------------------------
# undo_other
# ---------------------------------------------------------------------------


class TestUndoOther:
    def test_latest_when_no_id(self, pc):
        beans = add("Pinto beans", qty=5)
        rice = add("Wild rice", level="full")
        pc.cook_other([beans["id"]], dish="Beans")
        second = pc.cook_other([rice["id"]], dish="Rice bowl")
        before = metric(pc.PANTRY_COOK_EVENTS, slot="other", source="undo")
        res = pc.undo_other()
        assert res == {"ok": True, "cook_id": second["cook_id"], "dish": "Rice bowl", "restored": 1}
        assert item(rice["id"])["level"] == "full" and item(beans["id"])["qty"] == 4
        assert metric(pc.PANTRY_COOK_EVENTS, slot="other", source="undo") == before + 1
        assert [c["status"] for c in cooks()] == ["active", "undone"]
        assert len(cook_events(second["cook_id"], "consume")) == 1
        with db() as conn:
            [ev] = conn.execute(
                "SELECT * FROM pantry_events WHERE ref_type = 'uncook_other' AND ref_id = ?", (second["cook_id"],)
            ).fetchall()
        assert ev["kind"] == "adjust" and ev["item_id"] == rice["id"]

    def test_by_id(self, pc):
        beans = add("Pinto beans", qty=5)
        rice = add("Wild rice", level="full")
        first = pc.cook_other([beans["id"]])
        pc.cook_other([rice["id"]])
        res = pc.undo_other(first["cook_id"])
        assert res["ok"] and res["cook_id"] == first["cook_id"]
        assert item(beans["id"])["qty"] == 5 and item(rice["id"])["level"] == "half"

    def test_restock_after_cook_survives(self, pc):
        beans = add("Pinto beans", qty=5, location="pantry")
        pc.cook_other([beans["id"]])  # 4
        add("Pinto beans", qty=3, location="pantry")  # 7
        pc.undo_other()
        assert item(beans["id"])["qty"] == 8

    def test_level_changed_by_hand_left_alone(self, pc, pm):
        oil = add("Avocado oil", level="full")
        beans = add("Pinto beans", qty=5)
        pc.cook_other([oil["id"], beans["id"]])
        assert pm.update_item(oil["id"], {"level": "low"})["ok"]
        res = pc.undo_other()
        assert res["restored"] == 1
        assert item(oil["id"])["level"] == "low" and item(beans["id"])["qty"] == 5

    def test_double_undo_409(self, pc):
        beans = add("Pinto beans", qty=5)
        c = pc.cook_other([beans["id"]])
        assert pc.undo_other(c["cook_id"])["ok"]
        res = pc.undo_other(c["cook_id"])
        assert res["status_code"] == 409 and res["error"] == "that was already undone"
        assert item(beans["id"])["qty"] == 5
        # With no id, the undone cook isn't "latest" any more.
        assert pc.undo_other()["status_code"] == 404

    def test_older_than_48h_409(self, pc):
        beans = add("Pinto beans", qty=5)
        c = pc.cook_other([beans["id"]])
        age_cook(c["cook_id"], hours=pc.OTHER_UNDO_HOURS + 1)
        res = pc.undo_other(c["cook_id"])
        assert res["status_code"] == 409 and "too long ago" in res["error"]
        assert item(beans["id"])["qty"] == 4 and cooks()[0]["status"] == "active"
        assert pc.undo_other()["status_code"] == 404  # latest-mode ignores old cooks

    def test_unknown_and_none_recent(self, pc):
        assert pc.undo_other(424242) == {"ok": False, "error": "cook not found", "status_code": 404}
        res = pc.undo_other()
        assert res["status_code"] == 404 and res["error"] == "no recent off-plan cooking to undo"

    def test_planned_undo_unaffected_by_other_cook(self, pc):
        """Planned and off-plan ledgers are separate (ref_type cook vs cook_other)."""
        beans = add("Pinto beans", qty=6)
        p = make_plan(dinner=[("pinto beans", "2 cans")])
        pc.cook_entry(p["dinner_id"])  # 4
        pc.cook_other([beans["id"]])  # 3
        assert pc.undo_cook(p["dinner_id"])["restored"] == 1  # 5
        assert item(beans["id"])["qty"] == 5
        assert pc.undo_other()["restored"] == 1
        assert item(beans["id"])["qty"] == 6


# ---------------------------------------------------------------------------
# recent_other
# ---------------------------------------------------------------------------


class TestRecentOther:
    def test_newest_first_items_and_can_undo(self, pc):
        beans = add("Pinto beans", qty=5)
        rice = add("Wild rice", level="full")
        salt = add("Kosher salt", track_mode="staple")
        a = pc.cook_other([beans["id"], salt["id"]], dish="Beans")
        b = pc.cook_other([rice["id"], beans["id"]], dish="Rice and beans")
        c = pc.cook_other([rice["id"]], dish="Rice")
        pc.undo_other(c["cook_id"])
        age_cook(a["cook_id"], hours=pc.OTHER_UNDO_HOURS + 1)

        rows = pc.recent_other(days=3)
        assert [r["id"] for r in rows] == [c["cook_id"], b["cook_id"], a["cook_id"]]
        by_id = {r["id"]: r for r in rows}
        assert by_id[a["cook_id"]]["items"] == ["Pinto beans"]  # staple isn't a consume event
        assert sorted(by_id[b["cook_id"]]["items"]) == ["Pinto beans", "Wild rice"]
        assert by_id[b["cook_id"]]["can_undo"] is True
        assert by_id[c["cook_id"]]["can_undo"] is False  # undone
        assert by_id[a["cook_id"]]["can_undo"] is False  # too old
        assert by_id[b["cook_id"]]["dish"] == "Rice and beans"

    def test_days_window_and_clamp(self, pc):
        beans = add("Pinto beans", qty=50)
        old = pc.cook_other([beans["id"]])
        age_cook(old["cook_id"], days=20)
        mid = pc.cook_other([beans["id"]])
        age_cook(mid["cook_id"], days=5)
        assert pc.recent_other(days=2) == []
        assert [r["id"] for r in pc.recent_other(days=7)] == [mid["cook_id"]]
        assert [r["id"] for r in pc.recent_other(days=999)] == [mid["cook_id"]]  # clamped to 14
        assert pc.recent_other(days=0) == pc.recent_other(days=1) == []  # clamped to 1
        assert pc.recent_other(days=-5) == []


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@pytest.fixture
def client(pc):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.routes_pantry import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestRoutes:
    def test_cook_list_undo(self, client):
        beans = add("Pinto beans", qty=5)
        r = client.post("/api/pantry/cooked-other", json={"item_ids": [beans["id"]], "dish": "Bean bowl"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["deducted"] == ["Pinto beans"] and "status_code" not in body
        cid = body["cook_id"]

        r = client.post("/api/pantry/cooked-other", json={"item_ids": [beans["id"]]})
        assert r.status_code == 409 and r.json()["error"] == "that meal was just logged"

        r = client.get("/api/pantry/cooked-other")
        [row] = r.json()["cooks"]
        assert row["id"] == cid and row["dish"] == "Bean bowl" and row["can_undo"] is True

        r = client.post(f"/api/pantry/cooked-other/{cid}/undo")
        assert r.status_code == 200 and r.json()["restored"] == 1
        assert item(beans["id"])["qty"] == 5
        assert client.post(f"/api/pantry/cooked-other/{cid}/undo").status_code == 409

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"item_ids": []},
            {"item_ids": "1"},
            {"item_ids": 1},
            {"item_ids": {"a": 1}},
            {"item_ids": list(range(1, 32))},
            {"item_ids": [True]},
            [1, 2],
        ],
    )
    def test_bad_body_400(self, client, body):
        r = client.post("/api/pantry/cooked-other", json=body)
        assert r.status_code == 400 and r.json()["ok"] is False

    def test_non_json_400(self, client):
        r = client.post("/api/pantry/cooked-other", content=b"nope", headers={"content-type": "application/json"})
        assert r.status_code == 400

    def test_used_up_404(self, client):
        out = add("Rice noodles", qty=0)
        r = client.post("/api/pantry/cooked-other", json={"item_ids": [out["id"]]})
        assert r.status_code == 404 and r.json()["error"] == "those items are already used up"

    @pytest.mark.parametrize(
        "days,status", [("0", 422), ("15", 422), ("-1", 422), ("abc", 422), ("1", 200), ("14", 200)]
    )
    def test_list_days_bounds(self, client, days, status):
        assert client.get("/api/pantry/cooked-other", params={"days": days}).status_code == status

    def test_undo_bad_ids(self, client):
        assert client.post("/api/pantry/cooked-other/0/undo").status_code == 404
        assert client.post(f"/api/pantry/cooked-other/{2**70}/undo").status_code == 404
        assert client.post("/api/pantry/cooked-other/99999/undo").status_code == 404
        assert client.post("/api/pantry/cooked-other/abc/undo").status_code == 422


# ---------------------------------------------------------------------------
# meal_plan tool
# ---------------------------------------------------------------------------


class TestTool:
    @pytest.fixture
    def tool(self, pc):
        from orchestrator.tool_handlers import _reg_meal_plan

        return _reg_meal_plan

    async def test_cooked_other(self, tool, meals):
        beans = add("Pinto beans", qty=1)
        add("Corn tortillas", qty=8)
        out = await tool(
            {
                "action": "cooked",
                "slot": "other",
                "ingredients": ["pinto beans", "corn tortillas", "moon cheese"],
                "dish": "Tacos",
            }
        )
        assert out.startswith("Logged the meal")
        assert "Used from the pantry: Pinto beans, Corn tortillas." in out
        assert "Running low or out now: Pinto beans." in out
        assert "Not tracked in the pantry: Moon cheese." in out
        assert item(beans["id"])["qty"] == 0
        [row] = cooks()
        assert row["source"] == "chat" and row["dish"] == "Tacos"
        assert len(meals) == 1

    async def test_repeat_and_untracked_errors(self, tool):
        add("Pinto beans", qty=5)
        args = {"action": "cooked", "slot": "other", "ingredients": ["pinto beans"]}
        assert (await tool(args)).startswith("Logged the meal")
        assert (await tool(args)).startswith("that meal was just logged")
        out = await tool({"action": "cooked", "slot": "other", "ingredients": ["moon cheese"]})
        assert out.startswith("none of those are tracked in the pantry") and "Moon cheese" in out

    @pytest.mark.parametrize("ingredients", [None, [], "pinto beans"])
    async def test_missing_ingredients_asks(self, tool, ingredients):
        args = {"action": "cooked", "slot": "other"}
        if ingredients is not None:
            args["ingredients"] = ingredients
        out = await tool(args)
        assert out.startswith("Which pantry items") and cooks() == []

    async def test_ingredients_no_slot_with_planned_dinner_uses_plan(self, tool):
        beans = add("Pinto beans", qty=5)
        tortillas = add("Corn tortillas", qty=8)
        make_plan(dinner=[("pinto beans", "2 cans")])  # active, today
        out = await tool({"action": "cooked", "ingredients": ["corn tortillas"]})
        assert out.startswith("Marked Bean skillet as cooked")
        assert item(beans["id"])["qty"] == 3 and item(tortillas["id"])["qty"] == 8
        assert cooks() == []

    async def test_ingredients_no_slot_nothing_planned_goes_off_plan(self, tool):
        tortillas = add("Corn tortillas", qty=8)
        make_plan(dinner=[("pinto beans", "2 cans")], start=date.today() - timedelta(days=1))  # yesterday only
        out = await tool({"action": "cooked", "ingredients": ["corn tortillas"], "dish": "Quesadilla"})
        assert out.startswith("Logged the meal") and item(tortillas["id"])["qty"] == 7
        assert len(cooks()) == 1

    async def test_ingredients_no_slot_draft_plan_goes_off_plan(self, tool):
        """A draft dinner can't be cooked, so it must not swallow an off-plan meal."""
        tortillas = add("Corn tortillas", qty=8)
        p = make_plan(status="draft", dinner=[("pinto beans", "2 cans")])
        out = await tool({"action": "cooked", "ingredients": ["corn tortillas"]})
        assert out.startswith("Logged the meal") and item(tortillas["id"])["qty"] == 7
        assert entry(p["dinner_id"])["status"] == "planned"

    @pytest.mark.parametrize("action", ["skip", "swap", "recipe"])
    async def test_slot_other_never_touches_the_planned_dinner(self, tool, action):
        p = make_plan(dinner=[("pinto beans", "2 cans")])
        out = await tool({"action": action, "slot": "other"})
        assert "slot other is only for cooked / uncook" in out
        assert entry(p["dinner_id"])["status"] == "planned"

    async def test_uncook_other(self, tool):
        beans = add("Pinto beans", qty=5)
        assert "no recent off-plan" in await tool({"action": "uncook", "slot": "other"})
        await tool({"action": "cooked", "slot": "other", "ingredients": ["pinto beans"]})
        out = await tool({"action": "uncook", "slot": "other"})
        assert out == "Undone — 1 item(s) put back in the pantry."
        assert item(beans["id"])["qty"] == 5

    async def test_plain_cooked_no_entry_hints_slot_other(self, tool, monkeypatch):
        from orchestrator import meal_planner

        monkeypatch.setattr(meal_planner, "find_entry", lambda day, slot: None)
        out = await tool({"action": "cooked"})
        assert out.startswith("No dinner in the plan for that day.") and "slot other" in out
        assert cooks() == []


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_pantry_events_ref_index(tmp_db):
    from orchestrator import state_store

    with state_store.get_db() as conn:
        rows = conn.execute("PRAGMA index_list('pantry_events')").fetchall()
        names = {r["name"] for r in rows}
        assert "idx_pantry_events_ref" in names
        cols = [r["name"] for r in conn.execute("PRAGMA index_info('idx_pantry_events_ref')")]
    assert cols == ["ref_type", "ref_id"]


def test_pantry_cooks_table(tmp_db):
    from orchestrator import state_store

    with state_store.get_db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info('pantry_cooks')")}
    assert cols == {"id", "dish", "source", "cooked_at", "status"}
