"""Tests for pantry consumption tracking (F-015 phase 4): pantry_consumption
(cook / undo / skip, prepped portions, cooking-reminder Done taps, decay,
"use today" line, weekly fridge-check nudge) plus the phase-4 hooks in
meal_planner (swap guard + reminder links), pantry_manager (not-seen lines in
a stock-take, PREPPED summary), routes_pantry, the meal_plan tool and the
api_routes ack hook.

No model, HA, or push service is ever called. Food names are invented/generic.
"""

import json
import threading
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


class FakeScheduler:
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


@pytest.fixture
def meals(monkeypatch):
    from orchestrator import selfcare_manager

    logged = []
    monkeypatch.setattr(selfcare_manager, "record_meal_logged", lambda label="a meal": logged.append(label))
    return logged


@pytest.fixture
def pc(tmp_db, tmp_path, monkeypatch, meals):
    from orchestrator import meal_planner, pantry_consumption, shared

    monkeypatch.setattr(shared, "DIET_PROFILE_PATH", str(tmp_path / "diet_profile.yaml"))
    monkeypatch.setattr(shared, "PANTRY_PHOTOS_DIR", str(tmp_path / "pantry_photos"))
    monkeypatch.setattr(shared, "HELIOS_WAKE_ENABLED", False)
    monkeypatch.setattr(shared, "PANTRY_ENABLED", True)
    monkeypatch.setattr(shared, "scheduler", FakeScheduler())
    monkeypatch.setattr(meal_planner, "_meds_data", lambda: {})
    monkeypatch.setattr(meal_planner, "_swap_lock", None)

    async def _no_wait():
        return None

    monkeypatch.setattr(meal_planner, "_wait_for_idle_chat", _no_wait)
    return pantry_consumption


@pytest.fixture
def pm(pc):
    from orchestrator import pantry_manager

    return pantry_manager


@pytest.fixture
def sched(pc):
    from orchestrator import shared

    return shared.scheduler


def db():
    from orchestrator import state_store

    return state_store.get_db()


def metric(counter, **labels):
    return counter.labels(**labels)._value.get()


def add(name, **kw):
    from orchestrator import pantry_manager

    res = pantry_manager.add_item(name, **kw)
    assert res["ok"], res
    return res["item"]


def item(item_id):
    from orchestrator import pantry_manager

    return pantry_manager.get_item(item_id)


def _recipe(conn, name, ings):
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


def make_plan(status="active", dinner=None, prep=None, portions=4, lunch_days=None, start=None):
    """Plan rows inserted directly. dinner/prep: list of (item_name, amount[, optional])."""
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
            rid = _recipe(conn, "Bean skillet", dinner)
            out["dinner_recipe"] = rid
            out["dinner_id"] = conn.execute(
                "INSERT INTO meal_plan_entries (plan_id, day, slot, recipe_id, mode, portions) "
                "VALUES (?, ?, 'dinner', ?, 'fresh', 2)",
                (pid, start.isoformat(), rid),
            ).lastrowid
        if prep is not None:
            rid = _recipe(conn, "Barley stew", prep)
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


def entry(entry_id):
    with db() as conn:
        return dict(conn.execute("SELECT * FROM meal_plan_entries WHERE id = ?", (entry_id,)).fetchone())


def batches():
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM prep_batches ORDER BY id")]


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


def bare_recipe(name="Barley stew"):
    with db() as conn:
        return _recipe(conn, name, [])


# ---------------------------------------------------------------------------
# _deduct_count
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "amount,n",
    [
        ("2 cans", 2),
        ("3 eggs", 3),
        ("3", 3),
        ("20 cans", 12),  # capped
        ("12oz", 1),
        ("10 oz spinach", 1),
        ("200 g", 1),
        ("1.5 lbs", 1),
        ("1/2 cup", 1),
        ("a handful", 1),
        ("", 1),
        (None, 1),
    ],
)
def test_deduct_count(pc, amount, n):
    assert pc._deduct_count(amount) == n


# ---------------------------------------------------------------------------
# cook_entry
# ---------------------------------------------------------------------------


class TestCook:
    def test_draft_plan_refused(self, pc):
        beans = add("Black beans", qty=5, location="pantry")
        p = make_plan(status="draft", dinner=[("black beans", "2 cans")])
        res = pc.cook_entry(p["dinner_id"])
        assert res["ok"] is False and res["status_code"] == 409
        assert entry(p["dinner_id"])["status"] == "planned"
        assert item(beans["id"])["qty"] == 5

    def test_missing_entry_and_lunch_slot(self, pc):
        assert pc.cook_entry(999999)["status_code"] == 404
        p = make_plan(prep=[("pearl barley", "1 cup")])
        assert pc.cook_entry(p["lunch_ids"][0])["status_code"] == 400

    def test_dinner_cook_deducts(self, pc, meals):
        beans = add("Black beans", qty=5, location="pantry")
        eggs = add("Eggs", qty=6, location="fridge")
        rice = add("Jasmine rice", location="pantry", level="full")
        salt = add("Sea salt", category="spice", location="pantry", track_mode="staple")
        p = make_plan(
            dinner=[
                ("black beans", "2 cans"),
                ("eggs", "3 eggs"),
                ("jasmine rice", "1 cup"),
                ("sea salt", "a pinch"),
                ("saffron threads", "a pinch"),
                ("fresh dill", "to serve", 1),  # optional + untracked → not reported
            ]
        )
        before = metric(pc.PANTRY_COOK_EVENTS, slot="dinner", source="dashboard")
        res = pc.cook_entry(p["dinner_id"])
        assert res["ok"] is True and res["recipe"] == "Bean skillet" and res["slot"] == "dinner"
        assert item(beans["id"])["qty"] == 3
        assert item(eggs["id"])["qty"] == 3
        assert item(rice["id"])["level"] == "half"
        s = item(salt["id"])
        assert s["track_mode"] == "staple" and s["level"] == salt["level"] and s["qty"] == salt["qty"]
        assert sorted(res["deducted"]) == ["Black beans", "Eggs", "Jasmine rice"]
        assert res["not_tracked"] == ["saffron threads"]
        assert res["running_low"] == []
        assert res["prep_batch_id"] is None and res["portions"] is None
        assert entry(p["dinner_id"])["status"] == "cooked"
        assert meals == [f"cooked:{p['dinner_id']}"]
        assert metric(pc.PANTRY_COOK_EVENTS, slot="dinner", source="dashboard") == before + 1
        with db() as conn:
            evs = conn.execute(
                "SELECT item_id, kind, source FROM pantry_events WHERE ref_type = 'cook' AND ref_id = ?",
                (p["dinner_id"],),
            ).fetchall()
            times = conn.execute("SELECT times_cooked FROM recipes WHERE id = ?", (p["dinner_recipe"],)).fetchone()
        assert sorted(e["item_id"] for e in evs) == sorted([beans["id"], eggs["id"], rice["id"]])
        assert all(e["kind"] == "consume" and e["source"] == "cook" for e in evs)
        assert times[0] == 1

    @pytest.mark.parametrize("amount", ["12oz", "10 oz spinach", "200 g"])
    def test_unit_amount_deducts_one(self, pc, amount):
        spinach = add("Spinach", qty=4, location="fridge")
        p = make_plan(dinner=[("spinach", amount)])
        assert pc.cook_entry(p["dinner_id"])["ok"]
        assert item(spinach["id"])["qty"] == 3

    def test_levels_step_down_and_running_low(self, pc):
        oil = add("Olive oil", location="pantry", level="full")
        p1 = make_plan(dinner=[("olive oil", "2 tbsp")])
        assert pc.cook_entry(p1["dinner_id"])["running_low"] == []
        assert item(oil["id"])["level"] == "half"
        p2 = make_plan(dinner=[("olive oil", "2 tbsp")])
        res = pc.cook_entry(p2["dinner_id"])
        assert item(oil["id"])["level"] == "low" and res["running_low"] == ["Olive oil"]
        p3 = make_plan(dinner=[("olive oil", "2 tbsp")])
        pc.cook_entry(p3["dinner_id"])
        assert item(oil["id"])["level"] == "low"  # never steps to out by itself

    def test_count_to_zero_is_running_low(self, pc):
        tortillas = add("Tortillas", qty=2, location="pantry")
        p = make_plan(dinner=[("tortillas", "4")])
        res = pc.cook_entry(p["dinner_id"])
        assert item(tortillas["id"])["qty"] == 0 and res["running_low"] == ["Tortillas"]

    def test_second_cook_409(self, pc):
        beans = add("Black beans", qty=5)
        p = make_plan(dinner=[("black beans", "2 cans")])
        assert pc.cook_entry(p["dinner_id"])["ok"]
        res = pc.cook_entry(p["dinner_id"])
        assert res["status_code"] == 409 and res["error"].startswith("already")
        assert item(beans["id"])["qty"] == 3

    def test_skipped_then_cooked(self, pc):
        beans = add("Black beans", qty=5)
        p = make_plan(dinner=[("black beans", "1 can")])
        assert pc.skip_entry(p["dinner_id"])["ok"]
        assert pc.cook_entry(p["dinner_id"])["ok"]
        assert entry(p["dinner_id"])["status"] == "cooked"
        assert item(beans["id"])["qty"] == 4

    def test_name_matching(self, pc):
        butter = add("Butter", location="fridge", level="full")
        carrots = add("Carrot", qty=6, location="fridge")
        p = make_plan(dinner=[("peanut butter", "2 tbsp"), ("carrots", "2")])
        res = pc.cook_entry(p["dinner_id"])
        # "peanut butter" is not butter (receipt_scanner.name_score head-noun rule).
        assert item(butter["id"])["level"] == "full"
        assert "peanut butter" in res["not_tracked"]
        # Plural recipe line still matches the singular inventory name.
        assert item(carrots["id"])["qty"] == 4

    def test_out_and_gone_items_never_matched(self, pc, pm):
        out = add("Black beans", qty=0)
        p = make_plan(dinner=[("black beans", "1 can")])
        res = pc.cook_entry(p["dinner_id"])
        assert res["not_tracked"] == ["black beans"] and item(out["id"])["qty"] == 0

    def test_one_item_used_once_per_cook(self, pc):
        onion = add("Onion", qty=3)
        p = make_plan(dinner=[("onion", "1"), ("onions", "1")])
        res = pc.cook_entry(p["dinner_id"])
        assert item(onion["id"])["qty"] == 2
        assert res["not_tracked"] == ["onions"]

    def test_prep_cook_creates_batch_and_links_lunches(self, pc, meals):
        barley = add("Pearl barley", qty=2)
        p = make_plan(prep=[("pearl barley", "1 bag")], dinner=[("rice", "1 cup")], portions=4)
        before = metric(pc.PANTRY_COOK_EVENTS, slot="prep", source="push")
        res = pc.cook_entry(p["prep_id"], source="push")
        assert res["ok"] and res["slot"] == "prep" and res["portions"] == 4
        assert item(barley["id"])["qty"] == 1
        [b] = batches()
        assert res["prep_batch_id"] == b["id"]
        assert (b["portions_made"], b["portions_left"], b["status"], b["location"]) == (4, 4, "active", "fridge")
        assert b["good_until"] == (date.today() + timedelta(days=pc.PREP_FRIDGE_DAYS)).isoformat()
        assert all(entry(lid)["prep_batch_id"] == b["id"] for lid in p["lunch_ids"])
        assert entry(p["dinner_id"])["prep_batch_id"] is None
        assert meals == []  # cooking a batch is not eating it
        assert metric(pc.PANTRY_COOK_EVENTS, slot="prep", source="push") == before + 1


# ---------------------------------------------------------------------------
# undo_cook
# ---------------------------------------------------------------------------


class TestUndo:
    def test_restores(self, pc):
        beans = add("Black beans", qty=5)
        oil = add("Olive oil", level="full")
        p = make_plan(dinner=[("black beans", "2 cans"), ("olive oil", "1 tbsp")])
        pc.cook_entry(p["dinner_id"])
        res = pc.undo_cook(p["dinner_id"])
        assert res == {"ok": True, "restored": 2}
        assert item(beans["id"])["qty"] == 5 and item(oil["id"])["level"] == "full"
        e = entry(p["dinner_id"])
        assert e["status"] == "planned"
        with db() as conn:
            assert (
                conn.execute("SELECT times_cooked FROM recipes WHERE id = ?", (p["dinner_recipe"],)).fetchone()[0] == 0
            )

    def test_not_cooked_and_missing(self, pc):
        p = make_plan(dinner=[("rice", "1 cup")])
        assert pc.undo_cook(p["dinner_id"])["status_code"] == 409
        assert pc.undo_cook(424242)["status_code"] == 404

    def test_restock_after_cook_survives_undo(self, pc):
        beans = add("Black beans", qty=5, location="pantry")
        p = make_plan(dinner=[("black beans", "2 cans")])
        pc.cook_entry(p["dinner_id"])  # 5 → 3
        add("Black beans", qty=4, location="pantry")  # restock → 7
        assert item(beans["id"])["qty"] == 7
        pc.undo_cook(p["dinner_id"])
        assert item(beans["id"])["qty"] == 9

    def test_second_undo_restores_only_second_cook(self, pc):
        beans = add("Black beans", qty=5, location="pantry")
        p = make_plan(dinner=[("black beans", "2 cans")])
        pc.cook_entry(p["dinner_id"])  # 3
        pc.undo_cook(p["dinner_id"])  # 5
        add("Black beans", qty=4, location="pantry")  # 9
        pc.cook_entry(p["dinner_id"])  # 7
        res = pc.undo_cook(p["dinner_id"])
        assert res["restored"] == 1
        assert item(beans["id"])["qty"] == 9  # not 11: the first cook was already given back

    def test_level_changed_by_hand_left_alone(self, pc, pm):
        oil = add("Olive oil", level="full")
        beans = add("Black beans", qty=5)
        p = make_plan(dinner=[("olive oil", "1 tbsp"), ("black beans", "1 can")])
        pc.cook_entry(p["dinner_id"])
        assert item(oil["id"])["level"] == "half"
        assert pm.update_item(oil["id"], {"level": "low"})["ok"]
        res = pc.undo_cook(p["dinner_id"])
        assert res["restored"] == 1
        assert item(oil["id"])["level"] == "low"
        assert item(beans["id"])["qty"] == 5

    def test_prep_undo_drops_batch_and_unlinks(self, pc):
        barley = add("Pearl barley", qty=2)
        p = make_plan(prep=[("pearl barley", "1 bag")])
        pc.cook_entry(p["prep_id"])
        assert pc.undo_cook(p["prep_id"])["ok"]
        [b] = batches()
        assert b["status"] == "undone" and b["portions_left"] == 0
        assert all(entry(lid)["prep_batch_id"] is None for lid in p["lunch_ids"])
        assert item(barley["id"])["qty"] == 2
        # Undone batches aren't listed or eaten from.
        assert pc.list_batches() == []
        assert pc.eat_portion()["status_code"] == 404

    def test_refused_after_portion_eaten(self, pc):
        add("Pearl barley", qty=2)
        p = make_plan(prep=[("pearl barley", "1 bag")])
        pc.cook_entry(p["prep_id"])
        assert pc.eat_portion()["ok"]
        res = pc.undo_cook(p["prep_id"])
        assert res["ok"] is False and res["status_code"] == 409
        assert entry(p["prep_id"])["status"] == "cooked"


# ---------------------------------------------------------------------------
# skip + reminder cancellation
# ---------------------------------------------------------------------------


class TestSkipAndReminders:
    @pytest.fixture
    def cancelled(self, pc, monkeypatch):
        from orchestrator import meal_planner

        calls = []
        monkeypatch.setattr(meal_planner, "_cancel_reminders", lambda ids: calls.append(sorted(ids)))
        return calls

    def _link(self, pc, rid, eid):
        with db() as conn:
            pc.link_reminder(conn, rid, eid)

    def test_skip(self, pc, cancelled):
        p = make_plan(dinner=[("rice", "1 cup")], prep=[("barley", "1 cup")])
        self._link(pc, "mp-aaa", p["dinner_id"])
        self._link(pc, "mp-bbb", p["prep_id"])
        assert pc.skip_entry(p["dinner_id"]) == {"ok": True}
        assert entry(p["dinner_id"])["status"] == "skipped"
        assert cancelled == [["mp-aaa"]]
        again = pc.skip_entry(p["dinner_id"])
        assert again["status_code"] == 409 and "skipped" in again["error"]
        assert pc.skip_entry(p["lunch_ids"][0])["status_code"] == 409  # lunches aren't skippable
        assert pc.skip_entry(777777)["status_code"] == 404

    def test_cook_cancels_linked_reminders(self, pc, cancelled):
        p = make_plan(dinner=[("rice", "1 cup")])
        self._link(pc, "mp-ccc", p["dinner_id"])
        self._link(pc, "mp-ddd", p["dinner_id"])
        assert pc.cook_entry(p["dinner_id"])["ok"]
        assert cancelled == [["mp-ccc", "mp-ddd"]]

    def test_real_cancel_removes_pending_keeps_delivered(self, pc, sched):
        from orchestrator import state_store
        from orchestrator.reminder_manager import add_reminder, get_reminder

        p = make_plan(dinner=[("rice", "1 cup")])
        when = datetime.now() + timedelta(hours=3)
        for rid in ("mp-pending1", "mp-done1"):
            add_reminder(rid, "Cooking: bean skillet.", when, "both")
            sched.added[f"reminder_{rid}"] = {}
            self._link(pc, rid, p["dinner_id"])
        state_store.complete_reminder("mp-done1")
        assert pc.skip_entry(p["dinner_id"])["ok"]
        assert get_reminder("mp-pending1") is None
        assert get_reminder("mp-done1") is not None
        assert "reminder_mp-pending1" not in sched.added

    def test_cancel_failure_never_breaks_the_skip(self, pc, monkeypatch):
        from orchestrator import meal_planner

        def boom(ids):
            raise RuntimeError("x")

        monkeypatch.setattr(meal_planner, "_cancel_reminders", boom)
        p = make_plan(dinner=[("rice", "1 cup")])
        assert pc.skip_entry(p["dinner_id"])["ok"]


# ---------------------------------------------------------------------------
# Portions
# ---------------------------------------------------------------------------


class TestPortions:
    def test_eat_flow_ticks_lunches(self, pc, meals):
        today = date.today()
        add("Pearl barley", qty=2)
        p = make_plan(prep=[("pearl barley", "1 bag")], portions=2, lunch_days=[today + timedelta(days=1), today])
        pc.cook_entry(p["prep_id"])
        bid = batches()[0]["id"]
        before = metric(pc.PANTRY_PORTIONS_EATEN, source="chat")
        res = pc.eat_portion(source="chat")
        assert res["ok"] and res["batch_id"] == bid and res["portions_left"] == 1 and res["recipe"] == "Barley stew"
        # Today's lunch is ticked first, not the earlier-inserted tomorrow one.
        assert entry(p["lunch_ids"][1])["status"] == "eaten"
        assert entry(p["lunch_ids"][0])["status"] == "planned"
        assert meals == [f"portion:{bid}"]
        assert metric(pc.PANTRY_PORTIONS_EATEN, source="chat") == before + 1
        res = pc.eat_portion(bid)
        assert res["portions_left"] == 0
        assert batches()[0]["status"] == "finished"
        assert entry(p["lunch_ids"][0])["status"] == "eaten"
        assert pc.eat_portion(bid)["status_code"] == 409
        assert pc.eat_portion()["status_code"] == 404

    def test_default_picks_soonest_expiring(self, pc):
        r1, r2 = bare_recipe("Lentil soup"), bare_recipe("Chickpea curry")
        later = insert_batch(r1, date.today() + timedelta(days=3))
        sooner = insert_batch(r2, date.today() + timedelta(days=1))
        insert_batch(r2, date.today(), left=0)  # empty — never picked
        res = pc.eat_portion()
        assert res["batch_id"] == sooner and res["recipe"] == "Chickpea curry"
        assert [b["portions_left"] for b in batches() if b["id"] == later] == [3]

    def test_unknown_batch(self, pc):
        assert pc.eat_portion(123456)["status_code"] == 404

    def test_finish_batch(self, pc):
        bid = insert_batch(bare_recipe(), date.today() + timedelta(days=2))
        assert pc.finish_batch(bid) == {"ok": True}
        b = batches()[0]
        assert b["status"] == "finished" and b["portions_left"] == 0
        assert pc.finish_batch(bid)["status_code"] == 404
        assert pc.finish_batch(99999)["status_code"] == 404

    def test_list_batches(self, pc):
        r = bare_recipe("Lentil soup")
        insert_batch(r, date.today() - timedelta(days=1))
        insert_batch(r, date.today() + timedelta(days=2), status="finished")
        active = pc.list_batches()
        assert len(active) == 1 and active[0]["expired"] is True and active[0]["recipe_name"] == "Lentil soup"
        assert len(pc.list_batches(include_finished=True)) == 2


# ---------------------------------------------------------------------------
# Cooking-reminder Done taps
# ---------------------------------------------------------------------------


class TestReminderDone:
    def _linked(self, pc, status="active"):
        add("Black beans", qty=5)
        p = make_plan(status=status, dinner=[("black beans", "1 can")])
        with db() as conn:
            pc.link_reminder(conn, "mp-0123456789", p["dinner_id"])
        return p

    def test_not_ours(self, pc):
        assert pc.on_reminder_done(None) is None
        assert pc.on_reminder_done({}) is None
        assert pc.on_reminder_done({"id": "abc123"}) is None
        assert pc.on_reminder_done({"id": "pc-0123456789"}) is None

    def test_pantry_disabled(self, pc, monkeypatch):
        from orchestrator import shared

        p = self._linked(pc)
        monkeypatch.setattr(shared, "PANTRY_ENABLED", False)
        assert pc.on_reminder_done({"id": "mp-0123456789"}) is None
        assert entry(p["dinner_id"])["status"] == "planned"

    def test_unlinked(self, pc):
        before = metric(pc.PANTRY_REMINDER_DONE, result="unlinked")
        assert pc.on_reminder_done({"id": "mp-old000"}) == "unlinked"
        assert metric(pc.PANTRY_REMINDER_DONE, result="unlinked") == before + 1

    def test_cooked_then_already(self, pc):
        p = self._linked(pc)
        c0 = metric(pc.PANTRY_REMINDER_DONE, result="cooked")
        a0 = metric(pc.PANTRY_REMINDER_DONE, result="already")
        push0 = metric(pc.PANTRY_COOK_EVENTS, slot="dinner", source="push")
        assert pc.on_reminder_done({"id": "mp-0123456789", "text": "x"}) == "cooked"
        assert entry(p["dinner_id"])["status"] == "cooked"
        assert pc.on_reminder_done({"id": "mp-0123456789"}) == "already"
        assert metric(pc.PANTRY_REMINDER_DONE, result="cooked") == c0 + 1
        assert metric(pc.PANTRY_REMINDER_DONE, result="already") == a0 + 1
        assert metric(pc.PANTRY_COOK_EVENTS, slot="dinner", source="push") == push0 + 1

    def test_refused_when_plan_not_active(self, pc):
        p = self._linked(pc, status="replaced")
        before = metric(pc.PANTRY_REMINDER_DONE, result="refused")
        assert pc.on_reminder_done({"id": "mp-0123456789"}) == "refused"
        assert entry(p["dinner_id"])["status"] == "planned"
        assert metric(pc.PANTRY_REMINDER_DONE, result="refused") == before + 1

    def test_error_never_raises(self, pc, monkeypatch):
        self._linked(pc)

        def boom(*a, **k):
            raise RuntimeError("db gone")

        monkeypatch.setattr(pc, "cook_entry", boom)
        before = metric(pc.PANTRY_REMINDER_DONE, result="error")
        assert pc.on_reminder_done({"id": "mp-0123456789"}) == "error"
        assert metric(pc.PANTRY_REMINDER_DONE, result="error") == before + 1


# ---------------------------------------------------------------------------
# Decay + use-today
# ---------------------------------------------------------------------------


def _confirmed(item_id, days_ago):
    ts = (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds")
    with db() as conn:
        conn.execute("UPDATE pantry_items SET last_confirmed_at = ? WHERE id = ?", (ts, item_id))


class TestDecay:
    def test_marks_items_and_expires_batches(self, pc):
        from orchestrator import state_store

        old = add("Spinach", location="fridge", qty=1, shelf_life_days=3)
        fresh = add("Yogurt", location="fridge", qty=2, shelf_life_days=10)
        out = add("Basil", location="fridge", qty=0, shelf_life_days=2)
        shelf = add("Rolled oats", location="pantry", level="full")  # no shelf life
        _confirmed(old["id"], 5)
        _confirmed(fresh["id"], 2)
        _confirmed(out["id"], 9)
        _confirmed(shelf["id"], 400)
        r = bare_recipe()
        expired = insert_batch(r, date.today() - timedelta(days=1))
        today_b = insert_batch(r, date.today())
        i0 = metric(pc.PANTRY_DECAY, kind="item")
        res = pc.run_decay()
        assert res == {"items": 1, "batches": 1}
        assert item(old["id"])["status"] == "probably_gone"
        assert item(fresh["id"])["status"] == "active"
        assert item(out["id"])["status"] == "active"
        assert item(shelf["id"])["status"] == "active"
        st = {b["id"]: b["status"] for b in batches()}
        assert st[expired] == "expired" and st[today_b] == "active"
        assert metric(pc.PANTRY_DECAY, kind="item") == i0 + 1
        with db() as conn:
            ev = conn.execute(
                "SELECT kind, source FROM pantry_events WHERE item_id = ? ORDER BY id DESC LIMIT 1", (old["id"],)
            ).fetchone()
        assert (ev["kind"], ev["source"]) == ("adjust", "decay")
        stamp = state_store.get_app_state_entry(pc.DECAY_LAST_RUN_KEY)
        assert stamp and abs(float(stamp["value"]) - datetime.now().timestamp()) < 60
        assert pc.PANTRY_DECAY_LAST_RUN._value.get() == float(stamp["value"])
        # Idempotent.
        assert pc.run_decay() == {"items": 0, "batches": 0}

    def test_reconfirmed_between_read_and_update_not_marked(self, pc, pm, monkeypatch):
        import sqlite3

        from orchestrator import state_store

        spinach = add("Spinach", location="fridge", qty=1, shelf_life_days=3)
        _confirmed(spinach["id"], 5)
        real_is_out = pm.is_out
        fresh = datetime.now().isoformat(timespec="seconds")

        def racing_is_out(it):
            if it.get("id") == spinach["id"]:
                other = sqlite3.connect(state_store.DB_PATH)
                other.execute("UPDATE pantry_items SET last_confirmed_at = ? WHERE id = ?", (fresh, spinach["id"]))
                other.commit()
                other.close()
            return real_is_out(it)

        monkeypatch.setattr(pm, "is_out", racing_is_out)
        assert pc.run_decay()["items"] == 0
        got = item(spinach["id"])
        assert got["status"] == "active" and got["last_confirmed_at"] == fresh

    def test_exception_caught(self, pc, monkeypatch):
        from orchestrator import state_store

        def boom():
            raise RuntimeError("disk")

        monkeypatch.setattr(state_store, "get_db", boom)
        assert pc.run_decay() == {"items": 0, "batches": 0}

    def test_seed_gauge(self, pc, monkeypatch):
        from orchestrator import state_store

        state_store.set_app_state(pc.DECAY_LAST_RUN_KEY, "1700000000.5")
        pc.PANTRY_DECAY_LAST_RUN.set(0)
        pc.seed_decay_gauge()
        assert pc.PANTRY_DECAY_LAST_RUN._value.get() == 1700000000.5
        # Unset / unreadable: no crash, gauge untouched.
        state_store.delete_app_state(pc.DECAY_LAST_RUN_KEY)
        pc.seed_decay_gauge()
        assert pc.PANTRY_DECAY_LAST_RUN._value.get() == 1700000000.5
        state_store.set_app_state(pc.DECAY_LAST_RUN_KEY, "not-a-number")
        pc.seed_decay_gauge()
        assert pc.PANTRY_DECAY_LAST_RUN._value.get() == 1700000000.5


class TestUseSoon:
    def test_none_when_empty(self, pc):
        assert pc.use_soon_line() is None
        fresh = add("Yogurt", location="fridge", qty=2, shelf_life_days=10)
        _confirmed(fresh["id"], 1)
        assert pc.use_soon_line() is None

    def test_items_and_batches(self, pc):
        last_day = add("Spinach", location="fridge", qty=1, shelf_life_days=3)
        _confirmed(last_day["id"], 2)  # 1 day left
        too_old = add("Cilantro", location="fridge", qty=1, shelf_life_days=3)
        _confirmed(too_old["id"], 6)  # 3 days past — decay's job
        add("Rolled oats", location="pantry", level="full")
        r = bare_recipe("Lentil soup")
        insert_batch(r, date.today() + timedelta(days=1))
        insert_batch(bare_recipe("Bean chili"), date.today() + timedelta(days=3))  # not due yet
        insert_batch(bare_recipe("Pea soup"), date.today(), left=0)  # nothing left
        assert pc.use_soon_line() == "Use today if you can: spinach and the lentil soup."

    def test_out_items_skipped_and_cap_at_four(self, pc):
        add("Basil", location="fridge", qty=0, shelf_life_days=2)
        for nm in ("Spinach", "Kale", "Chard", "Arugula", "Romaine"):
            it = add(nm, location="fridge", qty=1, shelf_life_days=3)
            _confirmed(it["id"], 3)
        line = pc.use_soon_line()
        assert line.startswith("Use today if you can: ") and "basil" not in line
        names = line[len("Use today if you can: ") : -1].replace(" and ", ", ").split(", ")
        assert len(names) == 4


# ---------------------------------------------------------------------------
# Weekly fridge check
# ---------------------------------------------------------------------------


SAT = datetime(2026, 10, 17, 18, 30)  # a Saturday


class TestFridgeCheck:
    @pytest.fixture
    def clock(self, pc, monkeypatch):
        def set_now(dt):
            class FixedDT(datetime):
                @classmethod
                def now(cls, tz=None):
                    return dt

            monkeypatch.setattr(pc, "datetime", FixedDT)

        return set_now

    @pytest.fixture
    def added(self, pc, monkeypatch):
        from orchestrator import reminder_manager

        calls = []
        monkeypatch.setattr(reminder_manager, "add_reminder", lambda *a, **k: calls.append(a))
        return calls

    def test_sends_once(self, pc, clock, added, sched):
        from orchestrator import state_store
        from orchestrator.reminder_manager import selfcare_action_for_reminder

        clock(SAT)
        before = metric(pc.PANTRY_FRIDGE_CHECK, result="sent")
        assert pc.fridge_check_tick() == "sent"
        [(rid, text, when, target)] = added
        assert rid.startswith("pc-") and len(rid) == 13
        assert when == SAT + timedelta(seconds=30) and target == "phone"
        assert "fridge" in text.lower()
        assert f"reminder_{rid}" in sched.added and sched.added[f"reminder_{rid}"]["args"] == [rid]
        assert state_store.get_app_state_entry("pantry_fridge_check_sent")["value"] == "2026-10-17"
        assert metric(pc.PANTRY_FRIDGE_CHECK, result="sent") == before + 1
        assert selfcare_action_for_reminder({"id": rid, "text": "ate lunch"}) is None
        clock(SAT + timedelta(minutes=15))
        assert pc.fridge_check_tick() == "already"
        assert len(added) == 1

    @pytest.mark.parametrize(
        "now,expected",
        [
            (datetime(2026, 10, 16, 18, 30), "not_today"),  # Friday
            (datetime(2026, 10, 17, 17, 59), "too_early"),
            (datetime(2026, 10, 17, 21, 0), "sent"),  # edge of the 3h window
            (datetime(2026, 10, 17, 21, 1), "missed"),
        ],
    )
    def test_timing(self, pc, clock, added, now, expected):
        clock(now)
        assert pc.fridge_check_tick() == expected
        assert len(added) == (1 if expected == "sent" else 0)

    def test_disabled_and_custom_day(self, pc, clock, added):
        from orchestrator import diet_profiles

        clock(SAT)
        diet_profiles.save({"fridge_check": {"enabled": False}})
        assert pc.fridge_check_tick() == "disabled"
        diet_profiles.save({"fridge_check": {"enabled": True, "day": "sun", "time": "09:00"}})
        assert pc.fridge_check_tick() == "not_today"
        clock(datetime(2026, 10, 18, 9, 5))
        assert pc.fridge_check_tick() == "sent"

    def test_unreadable_settings(self, pc, clock, monkeypatch):
        from orchestrator import diet_profiles

        clock(SAT)
        monkeypatch.setattr(diet_profiles, "load", lambda: {"fridge_check": {"enabled": True, "time": "late"}})
        before = metric(pc.PANTRY_FRIDGE_CHECK, result="error")
        assert pc.fridge_check_tick() == "error"
        assert metric(pc.PANTRY_FRIDGE_CHECK, result="error") == before + 1

    def test_send_failure_clears_claim(self, pc, clock, added, sched):
        from orchestrator import state_store

        clock(SAT)
        sched.fail = True
        assert pc.fridge_check_tick() == "error"
        assert state_store.get_app_state_entry("pantry_fridge_check_sent") is None
        sched.fail = False
        assert pc.fridge_check_tick() == "sent"  # retried on the next tick


# ---------------------------------------------------------------------------
# meal_planner hooks
# ---------------------------------------------------------------------------


def R(name, items, minutes=20):
    return {
        "name": name,
        "ingredients": [{"item": i, "amount": "1 cup"} for i in items],
        "steps": ["Cook it all together."],
        "minutes": minutes,
        "est_kcal": 500,
    }


def planner_plan(start=None, status="draft"):
    """A plan through the real meal_planner._store_plan (prep + 2 lunches... + 2 dinners)."""
    from orchestrator import diet_profiles, meal_planner, state_store

    start = start or date.today() + timedelta(days=2)
    settings = diet_profiles.load()
    with state_store.get_db() as conn:
        pid = conn.execute(
            "INSERT INTO meal_plans (week_start, diet_profile, status, settings_json, created_at) "
            "VALUES (?, ?, 'generating', ?, ?)",
            (start.isoformat(), settings["profile"], json.dumps(settings), meal_planner._now()),
        ).lastrowid
    plan = {
        "batch": meal_planner._norm_recipe(R("Barley stew", ["pearl barley", "carrot"], 60)),
        "dinners": [
            dict(meal_planner._norm_recipe(R("Bean tacos", ["black beans", "tortillas"])), date=start.isoformat()),
            dict(
                meal_planner._norm_recipe(R("Pea soup", ["peas", "carrot"])),
                date=(start + timedelta(days=3)).isoformat(),
            ),
        ],
        "notes": "A plan.",
    }
    meal_planner._store_plan(pid, plan, settings, start, [])
    if status != "draft":
        meal_planner._set_plan(pid, status=status)
    return pid


def planner_entries(pid, slot):
    from orchestrator import meal_planner

    return [e for e in meal_planner.get_plan(pid)["entries"] if e["slot"] == slot]


def links():
    with db() as conn:
        return {r["reminder_id"]: r["entry_id"] for r in conn.execute("SELECT * FROM meal_plan_reminders")}


def fake_ask(reply):
    async def _ask(prompt, max_tokens, thinking, purpose, wait_for_idle=True):
        return reply, "stop", 100

    return _ask


SWAP_REPLY = json.dumps(R("Tofu stir fry", ["firm tofu", "broccoli"], 25))


class TestMealPlannerHooks:
    def test_accept_links_each_reminder_to_its_entry(self, pc, sched):
        from orchestrator import meal_planner

        pid = planner_plan()
        res = meal_planner.accept_plan(pid)
        assert res["ok"] and res["reminders"] == 3
        got = links()
        assert len(got) == 3 and all(r.startswith("mp-") and len(r) == 13 for r in got)
        wanted = {e["id"] for e in planner_entries(pid, "prep") + planner_entries(pid, "dinner")}
        assert set(got.values()) == wanted

    def test_failed_link_still_schedules(self, pc, sched, monkeypatch):
        from orchestrator import meal_planner

        def boom(conn, rid, eid):
            raise RuntimeError("locked")

        monkeypatch.setattr(pc, "link_reminder", boom)
        pid = planner_plan()
        res = meal_planner.accept_plan(pid)
        assert res["reminders"] == 3
        assert len(sched.added) == 3
        assert links() == {}

    async def test_swap_refused_for_cooked_entry_before_brain_check(self, pc, monkeypatch):
        from orchestrator import meal_planner

        async def no_brain():
            raise AssertionError("brain_ready must not be reached")

        monkeypatch.setattr(meal_planner, "brain_ready", no_brain)
        pid = planner_plan(status="active")
        dinner = planner_entries(pid, "dinner")[0]
        with db() as conn:
            conn.execute("UPDATE meal_plan_entries SET status = 'cooked' WHERE id = ?", (dinner["id"],))
        res = await meal_planner.swap_entry(dinner["id"])
        assert res["status_code"] == 409 and "cooked" in res["error"]

    async def test_swap_unlinks_old_reminders_draft(self, pc, monkeypatch):
        from orchestrator import meal_planner

        async def up():
            return True

        monkeypatch.setattr(meal_planner, "brain_ready", up)
        monkeypatch.setattr(meal_planner, "_ask", fake_ask(SWAP_REPLY))
        pid = planner_plan()
        d1, d2 = planner_entries(pid, "dinner")
        with db() as conn:
            pc.link_reminder(conn, "mp-swapped01", d1["id"])
            pc.link_reminder(conn, "mp-kept00001", d2["id"])
        res = await meal_planner.swap_entry(d1["id"])
        assert res["ok"], res
        assert links() == {"mp-kept00001": d2["id"]}

    async def test_swap_active_relinks_new_reminders(self, pc, sched, monkeypatch):
        from orchestrator import meal_planner

        async def up():
            return True

        monkeypatch.setattr(meal_planner, "brain_ready", up)
        monkeypatch.setattr(meal_planner, "_ask", fake_ask(SWAP_REPLY))
        pid = planner_plan()
        meal_planner.accept_plan(pid)
        dinner = planner_entries(pid, "dinner")[0]
        old = {r for r, e in links().items() if e == dinner["id"]}
        assert len(old) == 1
        res = await meal_planner.swap_entry(dinner["id"])
        assert res["ok"], res
        now = links()
        assert old.isdisjoint(now)
        assert sum(1 for e in now.values() if e == dinner["id"]) == 1
        # _reschedule replaces every pending reminder; each prep/dinner entry
        # gets exactly one live (pending) linked reminder afterwards.
        from orchestrator.reminder_manager import get_reminder

        live = {r: e for r, e in now.items() if get_reminder(r)}
        assert sorted(live.values()) == sorted(
            e["id"] for e in planner_entries(pid, "prep") + planner_entries(pid, "dinner")
        )

    async def test_reschedule_skips_cooked_and_skipped_entries(self, pc, sched, monkeypatch):
        """A swap (or a reminder-time change) re-creates the plan's reminders; a
        dinner already cooked or skipped must not start pinging again."""
        from orchestrator import meal_planner
        from orchestrator.reminder_manager import get_reminder

        async def up():
            return True

        monkeypatch.setattr(meal_planner, "brain_ready", up)
        monkeypatch.setattr(meal_planner, "_ask", fake_ask(SWAP_REPLY))
        pid = planner_plan()
        meal_planner.accept_plan(pid)
        prep = planner_entries(pid, "prep")[0]
        d1, d2 = planner_entries(pid, "dinner")
        assert pc.skip_entry(d1["id"])["ok"]
        assert pc.cook_entry(prep["id"])["ok"]
        res = await meal_planner.swap_entry(d2["id"])
        assert res["ok"], res
        pending_for = {e for r, e in links().items() if (get_reminder(r) or {}).get("status") == "pending"}
        assert pending_for == {d2["id"]}
        assert meal_planner.reschedule_active_plans() == 1
        pending_for = {e for r, e in links().items() if (get_reminder(r) or {}).get("status") == "pending"}
        assert pending_for == {d2["id"]}

    def test_get_plan_includes_batches(self, pc):
        from orchestrator import meal_planner

        add("Pearl barley", qty=3)
        pid = planner_plan(status="active")
        prep = planner_entries(pid, "prep")[0]
        res = pc.cook_entry(prep["id"])
        plan = meal_planner.get_plan(pid)
        b = plan["batches"][str(res["prep_batch_id"])]
        assert b["portions_left"] == b["portions_made"] == prep["portions"]
        assert all(e["prep_batch_id"] == res["prep_batch_id"] for e in plan["entries"] if e["slot"] == "lunch")


# ---------------------------------------------------------------------------
# pantry_manager hooks
# ---------------------------------------------------------------------------


class TestStockTakeNotSeen:
    def _scan(self, pm, lines):
        sid = pm.create_scan("fridge", [pm.save_scan_photo(JPEG)["path"]])["id"]
        pm._set_scan(sid, status="pending_review", proposed_json=json.dumps({"items": lines, "warnings": []}))
        return sid

    def test_build_proposal_lists_unseen_active_items_unticked(self, pm):
        seen = add("Eggs", location="fridge", qty=6)
        unseen = add("Leftover rice", location="fridge", level="half")
        add("Basil", location="fridge", qty=0)  # already out — not listed
        add("Frozen peas", location="freezer", level="full")  # other location
        lines = pm.build_proposal(
            "fridge", [{"name": "Eggs", "category": "protein", "qty": 6, "level": None, "confidence": "high"}]
        )
        assert lines[0]["match_id"] == seen["id"] and lines[0]["action"] == "update"
        ns = [ln for ln in lines if ln["action"] == "not_seen"]
        assert [ln["match_id"] for ln in ns] == [unseen["id"]]
        assert ns[0]["include"] is False and ns[0]["key"] == 1

    def test_commit_marks_out_only_when_ticked(self, pm):
        rice = add("Leftover rice", location="fridge", level="half")
        hummus = add("Hummus", location="fridge", level="full")
        salsa = add("Salsa", location="fridge", qty=2)
        proposal = [
            {"name": n, "match_id": i["id"], "action": "not_seen", "include": False}
            for n, i in (("Leftover rice", rice), ("Hummus", hummus), ("Salsa", salsa))
        ]
        sid = self._scan(pm, proposal)
        res = pm.commit_scan(
            sid,
            [
                {"name": "Leftover rice", "match_id": rice["id"], "include": True},
                {"name": "Hummus", "match_id": hummus["id"]},  # include omitted → never marked out
                {"name": "Salsa", "match_id": salsa["id"], "include": False},
            ],
        )
        assert res == {"ok": True, "added": 0, "updated": 0, "skipped": 2, "gone": 1}
        assert item(rice["id"])["is_out"] is True
        assert item(hummus["id"])["level"] == "full"
        assert item(salsa["id"])["qty"] == 2

    def test_request_cannot_turn_an_update_into_not_seen(self, pm):
        eggs = add("Eggs", location="fridge", qty=2)
        proposal = [{"name": "Eggs", "match_id": eggs["id"], "action": "update", "include": True}]
        sid = self._scan(pm, proposal)
        res = pm.commit_scan(
            sid, [{"name": "Eggs", "match_id": eggs["id"], "action": "not_seen", "include": True, "qty": 5}]
        )
        assert res["gone"] == 0 and res["updated"] == 1
        assert item(eggs["id"])["qty"] == 5

    def test_inventory_summary_lists_prepped(self, pc, pm):
        add("Eggs", location="fridge", qty=6)
        insert_batch(bare_recipe("Lentil soup"), date.today() + timedelta(days=2), portions=4, left=3)
        insert_batch(bare_recipe("Old stew"), date.today(), left=0)
        text = pm.inventory_summary()
        assert "PREPPED: Lentil soup ×3" in text and "Old stew" not in text


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
    def test_cook_undo_skip(self, client):
        beans = add("Black beans", qty=5)
        p = make_plan(dinner=[("black beans", "2 cans")])
        eid = p["dinner_id"]
        r = client.post(f"/api/pantry/plan/entry/{eid}/cooked")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["deducted"] == ["Black beans"] and "status_code" not in body
        assert client.post(f"/api/pantry/plan/entry/{eid}/cooked").status_code == 409
        r = client.post(f"/api/pantry/plan/entry/{eid}/uncook")
        assert r.status_code == 200 and r.json() == {"ok": True, "restored": 1}
        assert item(beans["id"])["qty"] == 5
        assert client.post(f"/api/pantry/plan/entry/{eid}/uncook").status_code == 409
        r = client.post(f"/api/pantry/plan/entry/{eid}/skip")
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert client.post(f"/api/pantry/plan/entry/{eid}/skip").status_code == 409

    def test_cook_draft_409(self, client):
        p = make_plan(status="draft", dinner=[("rice", "1 cup")])
        r = client.post(f"/api/pantry/plan/entry/{p['dinner_id']}/cooked")
        assert r.status_code == 409 and r.json()["ok"] is False

    @pytest.mark.parametrize("action", ["cooked", "uncook", "skip"])
    def test_entry_bad_ids(self, client, action):
        assert client.post(f"/api/pantry/plan/entry/0/{action}").status_code == 404
        assert client.post(f"/api/pantry/plan/entry/{2**70}/{action}").status_code == 404
        assert client.post(f"/api/pantry/plan/entry/99999/{action}").status_code == 404
        assert client.post(f"/api/pantry/plan/entry/abc/{action}").status_code == 422

    def test_prep_list_eat_finish(self, client):
        assert client.get("/api/pantry/prep").json() == {"batches": []}
        bid = insert_batch(bare_recipe("Lentil soup"), date.today() + timedelta(days=2), portions=2)
        r = client.get("/api/pantry/prep")
        [b] = r.json()["batches"]
        assert b["id"] == bid and b["recipe_name"] == "Lentil soup" and b["expired"] is False
        r = client.post(f"/api/pantry/prep/{bid}/eat")
        assert r.status_code == 200 and r.json()["portions_left"] == 1
        r = client.post(f"/api/pantry/prep/{bid}/finish")
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert client.post(f"/api/pantry/prep/{bid}/eat").status_code == 409
        assert client.post(f"/api/pantry/prep/{bid}/finish").status_code == 404
        assert client.get("/api/pantry/prep").json() == {"batches": []}

    @pytest.mark.parametrize("action", ["eat", "finish"])
    def test_prep_bad_ids(self, client, action):
        assert client.post(f"/api/pantry/prep/0/{action}").status_code == 404
        assert client.post(f"/api/pantry/prep/{2**70}/{action}").status_code == 404
        assert client.post(f"/api/pantry/prep/4242/{action}").status_code == 404


# ---------------------------------------------------------------------------
# meal_plan tool
# ---------------------------------------------------------------------------


class TestTool:
    @pytest.fixture
    def tool(self, pc):
        from orchestrator.tool_handlers import _reg_meal_plan

        return _reg_meal_plan

    @pytest.fixture
    def found(self, pc, monkeypatch):
        from orchestrator import meal_planner

        calls = []
        target = {"id": None}

        def find_entry(day, slot):
            calls.append((day, slot))
            return target["id"]

        monkeypatch.setattr(meal_planner, "find_entry", find_entry)
        return target, calls

    async def test_cooked_uncook(self, tool, found):
        target, calls = found
        add("Black beans", qty=1)
        add("Olive oil", level="full")
        p = make_plan(dinner=[("black beans", "1 can"), ("olive oil", "1 tbsp"), ("saffron", "a pinch")])
        target["id"] = p["dinner_id"]
        out = await tool({"action": "cooked", "day": "2026-10-15"})
        assert calls == [("2026-10-15", "dinner")]
        assert out.startswith("Marked Bean skillet as cooked")
        assert "Used from the pantry: Black beans, Olive oil." in out
        assert "Running low or out now: Black beans." in out
        assert "Not tracked in the pantry: saffron." in out
        assert entry(p["dinner_id"])["status"] == "cooked"
        assert "already marked cooked" in await tool({"action": "cooked"})
        out = await tool({"action": "uncook"})
        assert out.startswith("Undone — 2 item(s) put back")
        assert "isn't marked cooked" in await tool({"action": "uncook"})

    async def test_cooked_batch_mentions_portions(self, tool, found):
        target, calls = found
        p = make_plan(prep=[("pearl barley", "1 bag")], portions=4)
        target["id"] = p["prep_id"]
        out = await tool({"action": "cooked", "slot": "batch"})
        assert calls[-1] == (None, "batch")
        assert "4 prepped portions are in the fridge." in out

    async def test_skip(self, tool, found):
        target, _ = found
        p = make_plan(dinner=[("rice", "1 cup")])
        target["id"] = p["dinner_id"]
        assert (await tool({"action": "skip"})).startswith("Marked as skipped")
        assert "can't skip" in await tool({"action": "skip"})

    async def test_no_entry(self, tool, found):
        out = await tool({"action": "cooked"})
        assert out.startswith("No dinner in the plan for that day.") and "slot other" in out
        assert await tool({"action": "skip", "slot": "batch"}) == "No batch prep in the plan for that day."

    async def test_ate(self, tool, meals):
        assert await tool({"action": "ate"}) == "no prepped portions to eat"
        good = date.today() + timedelta(days=2)
        insert_batch(bare_recipe("Lentil soup"), good, portions=2)
        out = await tool({"action": "ate"})
        assert out.startswith("Logged a portion of Lentil soup") and f"1 left, good until {good.isoformat()}" in out
        assert len(meals) == 1

    async def test_unknown(self, tool):
        out = await tool({"action": "eat_everything"})
        assert out.startswith("Unknown meal_plan action") and "cooked, uncook, ate or skip" in out

    async def test_disabled(self, tool, monkeypatch):
        from orchestrator import shared

        monkeypatch.setattr(shared, "PANTRY_ENABLED", False)
        assert "isn't enabled" in await tool({"action": "cooked"})


# ---------------------------------------------------------------------------
# api_routes ack hook
# ---------------------------------------------------------------------------


class TestAckHook:
    async def test_runs_off_the_event_loop(self, monkeypatch):
        import inspect

        from orchestrator import api_routes, pantry_consumption

        assert inspect.iscoroutinefunction(api_routes._pantry_reminder_done)
        seen = {}

        def fake(reminder):
            seen["thread"] = threading.get_ident()
            seen["reminder"] = reminder
            return "cooked"

        monkeypatch.setattr(pantry_consumption, "on_reminder_done", fake)
        await api_routes._pantry_reminder_done({"id": "mp-abc"})
        assert seen["reminder"] == {"id": "mp-abc"}
        assert seen["thread"] != threading.get_ident()

    async def test_swallows_exceptions(self, monkeypatch):
        from orchestrator import api_routes, pantry_consumption

        mock = MagicMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr(pantry_consumption, "on_reminder_done", mock)
        assert await api_routes._pantry_reminder_done({"id": "mp-abc"}) is None
        mock.assert_called_once()
