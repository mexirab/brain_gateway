"""Tests for meal_planner (F-015 phase 2): sanitising/parsing helpers, week
maths, inventory matching + safety checks, plan validation, prompt contents,
background generation (LLM mocked via meal_planner._ask), reading plans,
accept/discard/swap/rate, reminders, and the chat-tool text helpers.

Never calls a real model. Medication names/notes are invented
("examplamab"); grocery names are generic.
"""

import asyncio
import json
import sys
import types
from datetime import date, datetime, timedelta

import httpx
import pytest

from orchestrator import diet_profiles, shared, state_store
from orchestrator import meal_planner as mp
from orchestrator import pantry_manager as pm
from orchestrator.reminder_manager import get_reminder, infer_selfcare_action_from_text, selfcare_action_for_reminder
from orchestrator.tests._helpers import metric_value
from orchestrator.tests.fixtures_pantry import SWAP_REPLY, fake_ask, recipe, store_plan, week_plan

pytestmark = pytest.mark.usefixtures("pantry_env")

MEDS = {
    "daily": {
        "morning": [{"name": "Examplamab 10mg", "dose": "1 tab"}],
        "evening": [{"name": "Fictorin", "dose": "2 tabs"}],
    },
    "weekly": [{"name": "zz"}],  # too short — ignored
    "interactions": ["Avoid grapefruit while taking examplamab", "Limit large amounts of licorice"],
    "pharmacy": {"name": "Corner Chemist"},
}


def status_of(pid):
    with state_store.get_db() as conn:
        return conn.execute("SELECT status, error FROM meal_plans WHERE id = ?", (pid,)).fetchone()


def reply_for(start, n_dinners=4, batch=True, **over):
    dates = [(start + timedelta(days=i)).isoformat() for i in range(7)]
    obj = {
        "batch": recipe("Lentil barley stew", ["red lentils", "pearl barley", "carrot"], 60) if batch else None,
        "dinners": [
            dict(recipe(f"Quick dish {i}", [f"vegetable {i}", "olive oil"]), date=dates[i * 2 % 7])
            for i in range(n_dinners)
        ],
        "notes": "Balanced week.",
    }
    obj.update(over)
    return json.dumps(obj)


async def _true():
    return True


async def _false():
    return False


# ---------------------------------------------------------------------------
# Text hygiene + parsing
# ---------------------------------------------------------------------------


class TestRecipeName:
    def test_plain_name_kept(self):
        assert mp._recipe_name("Salmon & greens (one-pan), v2") == "Salmon & greens (one-pan), v2"

    def test_disallowed_chars_removed(self):
        out = mp._recipe_name("Call: me/now @ <b>stew</b>")
        for ch in ":/@<>":
            assert ch not in out
        assert "stew" in out

    @pytest.mark.parametrize("raw", ["Call 555 123 4567 now", "Soup 12345", "Visit www example", "Stew at evil.com"])
    def test_phone_and_url_like_rejected(self, raw):
        assert mp._recipe_name(raw) == ""

    def test_four_digits_ok_and_length_cap(self):
        assert mp._recipe_name("Stew 2026") == "Stew 2026"
        assert len(mp._recipe_name("a" * 200)) == 80

    def test_none_and_control(self):
        assert mp._recipe_name(None) == ""
        assert mp._recipe_name("Rice\x00\n bowl") == "Rice bowl"


class TestNormRecipe:
    def test_happy(self):
        r = mp._norm_recipe(recipe("Bean chili", ["kidney beans", "Kidney beans", "onion"], minutes=35))
        assert r["name"] == "Bean chili"
        assert [i["item"] for i in r["ingredients"]] == ["Kidney beans", "Onion"]  # deduped
        assert r["minutes"] == 35 and r["est_kcal"] == 500

    def test_string_ingredients_accepted(self):
        r = mp._norm_recipe({"name": "Toast", "ingredients": ["bread", 5, None], "steps": ["Toast it."]})
        assert [i["item"] for i in r["ingredients"]] == ["Bread"]

    @pytest.mark.parametrize("field,val", [("steps", "Cook it."), ("ingredients", "rice, beans"), ("steps", None)])
    def test_non_list_fields_rejected(self, field, val):
        raw = recipe("Rice and beans", ["rice", "beans"])
        raw[field] = val
        assert mp._norm_recipe(raw) is None

    @pytest.mark.parametrize("raw", [None, "x", [], {"name": "", "ingredients": ["a"], "steps": ["b"]}])
    def test_unusable(self, raw):
        assert mp._norm_recipe(raw) is None

    def test_med_name_steps_dropped(self):
        raw = recipe("Soup", ["leek"], steps=["Simmer the leek.", "Take your Examplamab with it."])
        r = mp._norm_recipe(raw, ["examplamab"])
        assert r["steps"] == ["Simmer the leek."]

    def test_all_steps_dropped_makes_unusable(self):
        raw = recipe("Soup", ["leek"], steps=["Take examplamab now."])
        assert mp._norm_recipe(raw, ["examplamab"]) is None

    def test_bad_numbers_nulled(self):
        raw = recipe("Soup", ["leek"])
        raw.update(minutes=True, est_kcal=99999, est_satfat_g="nan", est_fiber_g="4.25")
        r = mp._norm_recipe(raw)
        assert r["minutes"] is None and r["est_kcal"] is None and r["est_satfat_g"] is None
        assert r["est_fiber_g"] in (4.2, 4.3)

    def test_ingredient_cap(self):
        r = mp._norm_recipe(recipe("Big salad", [f"leaf {i}" for i in range(40)]))
        assert len(r["ingredients"]) == mp.MAX_INGREDIENTS


class TestParseJson:
    @pytest.mark.parametrize(
        "text,want",
        [
            pytest.param('{"a": 1}', {"a": 1}, id="plain"),
            pytest.param('Here:\n```json\n{"a": 2}\n```\nbye', {"a": 2}, id="fenced"),
            pytest.param('Sure! {"a": {"b": 3}} hope that helps', {"a": {"b": 3}}, id="embedded"),
            pytest.param("", None, id="empty"),
            pytest.param(None, None, id="none"),
            pytest.param("no json", None, id="prose"),
            pytest.param("[1, 2]", None, id="list"),
            pytest.param("{broken", None, id="broken"),
        ],
    )
    def test_parse(self, text, want):
        assert mp._parse_json_object(text) == want


class TestMentionsAny:
    def test_word_boundary(self):
        assert mp._mentions_any("Take Examplamab daily", ["examplamab"])
        assert not mp._mentions_any("superexamplamab", ["examplamab"])
        assert not mp._mentions_any("anything", [])
        assert not mp._mentions_any("anything", [""])


# ---------------------------------------------------------------------------
# Week maths
# ---------------------------------------------------------------------------


class TestWeekStart:
    S = {"batch_day": "sun"}

    def test_batch_day_before_18(self):
        sun = datetime(2026, 10, 11, 17, 59)  # a Sunday
        assert mp.plan_week_start(self.S, sun) == date(2026, 10, 11)

    def test_batch_day_after_18_rolls_a_week(self):
        assert mp.plan_week_start(self.S, datetime(2026, 10, 11, 18, 0)) == date(2026, 10, 18)

    def test_other_weekdays(self):
        assert mp.plan_week_start(self.S, datetime(2026, 10, 14, 9)) == date(2026, 10, 18)  # Wed
        assert mp.plan_week_start({"batch_day": "wed"}, datetime(2026, 10, 15, 9)) == date(2026, 10, 21)  # Thu
        assert mp.plan_week_start({"batch_day": "mon"}, datetime(2026, 10, 18, 23)) == date(2026, 10, 19)

    def test_week_dates(self):
        d = mp.week_dates(date(2026, 12, 28))
        assert len(d) == 7 and d[0] == date(2026, 12, 28) and d[-1] == date(2027, 1, 3)


# ---------------------------------------------------------------------------
# Inventory matching + safety checks
# ---------------------------------------------------------------------------


class TestMatching:
    def test_have_item_fuzzy(self):
        inv = [{"name": "Chickpeas"}, {"name": "Brown rice"}]
        assert mp.have_item("canned chickpeas", inv)
        assert mp.have_item("brown rice", inv)
        assert not mp.have_item("salmon fillet", inv)
        assert not mp.have_item("", inv)
        assert not mp.have_item("rice", [])

    def test_avoid_violations_word_boundary_and_plural(self):
        r = mp._norm_recipe(recipe("Peanut noodles", ["rice noodles", "peanuts", "scallion"]))
        assert mp.avoid_violations(r, ["peanut"]) == ["peanut"]
        assert mp.avoid_violations(r, ["peanuts"]) == ["peanuts"]
        r2 = mp._norm_recipe(recipe("Pineapple salsa", ["pineapple", "lime"]))
        assert mp.avoid_violations(r2, ["apple"]) == []  # not a substring match
        assert mp.avoid_violations(r2, ["lime", "fish"]) == ["lime"]
        assert mp.avoid_violations(r2, [""]) == []

    def test_interaction_flags(self):
        notes = MEDS["interactions"]
        recipes = [
            ("Mon dinner", mp._norm_recipe(recipe("Citrus salad", ["grapefruits", "spinach"]))),
            ("batch prep", mp._norm_recipe(recipe("Big soup", ["leek", "large potato", "food"]))),
        ]
        flags = mp.interaction_flags(recipes, notes)
        assert len(flags) == 1
        assert flags[0].startswith("Grapefruits (Mon dinner)")
        assert "prescriber" in flags[0]

    def test_interaction_flags_no_notes(self):
        assert mp.interaction_flags([("x", mp._norm_recipe(recipe("Salad", ["grapefruit"])))], []) == []

    def test_med_names_walks_structure(self, monkeypatch):
        monkeypatch.setattr(mp, "_meds_data", lambda: MEDS)
        names = mp._med_names()
        assert "examplamab" in names and "examplamab 10mg" in names and "fictorin" in names
        assert "zz" not in names
        assert not any("corner" in n for n in names)  # pharmacy skipped

    def test_interaction_notes(self, monkeypatch):
        monkeypatch.setattr(mp, "_meds_data", lambda: {"interactions": ["Avoid <grapefruit>", 5, "  "]})
        assert mp._interaction_notes() == ["Avoid grapefruit"]


# ---------------------------------------------------------------------------
# validate_plan
# ---------------------------------------------------------------------------


START = date(2026, 10, 18)


def _settings(**over):
    return diet_profiles.validate(over)


def _obj(dinners, batch=True, notes="Good week."):
    return {
        "batch": recipe("Lentil stew", ["lentils", "carrot"], 60) if batch else None,
        "dinners": dinners,
        "notes": notes,
    }


def _d(offset, name, items=("rice",)):
    return dict(recipe(name, list(items)), date=(START + timedelta(days=offset)).isoformat())


class TestValidatePlan:
    def test_happy(self):
        s = _settings(fresh_dinners_per_week=2)
        plan, problems = mp.validate_plan(_obj([_d(3, "Tacos"), _d(1, "Curry")]), s, START)
        assert problems == []
        assert [d["name"] for d in plan["dinners"]] == ["Curry", "Tacos"]  # sorted by date
        assert plan["batch"]["name"] == "Lentil stew" and plan["notes"] == "Good week."

    def test_none_obj(self):
        plan, problems = mp.validate_plan(None, _settings(), START)
        assert plan is None and problems

    def test_dates_outside_week_and_duplicates_dropped(self):
        s = _settings(fresh_dinners_per_week=3)
        dinners = [
            _d(0, "A"),
            _d(0, "B"),  # duplicate date
            _d(7, "C"),  # next week
            _d(-1, "D"),  # before
            dict(recipe("E", ["x"]), date="not-a-date"),
            "junk",
        ]
        plan, problems = mp.validate_plan(_obj(dinners), s, START)
        assert [d["name"] for d in plan["dinners"]] == ["A"]
        assert any("expected 3 dinners" in p for p in problems)

    def test_extra_dinners_truncated(self):
        s = _settings(fresh_dinners_per_week=1)
        plan, problems = mp.validate_plan(_obj([_d(2, "Late"), _d(1, "Early")]), s, START)
        assert [d["name"] for d in plan["dinners"]] == ["Early"]
        assert problems == []

    def test_batch_violating_avoid_kills_plan(self):
        s = _settings(fresh_dinners_per_week=1, avoid=["lentil"])
        plan, problems = mp.validate_plan(_obj([_d(1, "Curry")]), s, START)
        assert plan is None
        assert any("avoided food" in p for p in problems)

    def test_missing_batch_kills_plan(self):
        s = _settings(fresh_dinners_per_week=1)
        plan, problems = mp.validate_plan(_obj([_d(1, "Curry")], batch=False), s, START)
        assert plan is None and any("batch recipe" in p for p in problems)

    def test_dinner_violating_avoid_dropped(self):
        s = _settings(fresh_dinners_per_week=2, avoid=["shrimp"])
        plan, problems = mp.validate_plan(
            _obj([_d(1, "Shrimp pasta", ["shrimp", "pasta"]), _d(2, "Veg pasta", ["pasta"])]), s, START
        )
        assert [d["name"] for d in plan["dinners"]] == ["Veg pasta"]
        assert any("Shrimp pasta" in p and "avoided" in p for p in problems)

    def test_all_dinners_invalid_with_dinners_wanted(self):
        s = _settings(fresh_dinners_per_week=2, batch_lunch_days=0)
        plan, _ = mp.validate_plan(_obj([_d(9, "X")], batch=False), s, START)
        assert plan is None

    def test_duplicate_dish_names(self):
        s = _settings(fresh_dinners_per_week=2)
        _, problems = mp.validate_plan(_obj([_d(1, "Lentil Stew"), _d(2, "Other")]), s, START)
        assert "a dish appears twice" in problems

    def test_notes_mentioning_med_blanked(self):
        s = _settings(fresh_dinners_per_week=1)
        plan, _ = mp.validate_plan(
            _obj([_d(1, "Curry")], notes="Kept clear of Examplamab issues."), s, START, ["examplamab"]
        )
        assert plan["notes"] == ""

    def test_zero_batch_days_ignores_batch(self):
        s = _settings(fresh_dinners_per_week=1, batch_lunch_days=0)
        plan, problems = mp.validate_plan(_obj([_d(1, "Curry")], batch=True), s, START)
        assert plan["batch"] is None and problems == []

    def test_zero_dinners_batch_only(self):
        s = _settings(fresh_dinners_per_week=0)
        plan, problems = mp.validate_plan(_obj([]), s, START)
        assert plan["dinners"] == [] and plan["batch"] and problems == []


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def test_contents(self):
        s = _settings(profile="dash", avoid=["peanuts"], extra_rules=["Spicy food is welcome"])
        inv = [
            {
                "name": "Spinach",
                "location": "fridge",
                "track_mode": "level",
                "level": "half",
                "qty": None,
                "shelf_life_days": 5,
                "last_confirmed_at": (datetime.now() - timedelta(days=4)).isoformat(),
            },
            {
                "name": "Eggs",
                "location": "fridge",
                "track_mode": "count",
                "qty": 6,
                "level": None,
                "shelf_life_days": None,
                "last_confirmed_at": None,
            },
        ]
        p = mp.build_prompt(s, START, inv, ["Avoid grapefruit"])
        assert "DIET (DASH)" in p
        assert "Keep sodium low" in p and "Spicy food is welcome" in p
        assert "NEVER use (may be allergies): peanuts" in p
        assert "Avoid grapefruit" in p
        for d in mp.week_dates(START):
            assert d.isoformat() in p
        assert "Spinach (fridge, half) — USE SOON" in p
        assert "Eggs (fridge, x6)" in p and "Eggs (fridge, x6) — USE SOON" not in p
        assert "Never mention medications" in p
        assert '"batch": null' not in p

    def test_no_batch_and_empty_inventory(self):
        s = _settings(batch_lunch_days=0)
        p = mp.build_prompt(s, START, [], [])
        assert '"batch": null' in p
        assert "nothing tracked" in p
        assert "Batch prep on" not in p

    def test_history_included(self):
        pid = store_plan(START, dinners=[(1, recipe("Old favourite", ["rice"]))], status="active")
        plan = mp.get_plan(pid)
        rid = int(next(iter(plan["recipes"])))
        mp.rate_recipe(rid, "down")
        p = mp.build_prompt(_settings(), START, [], [])
        assert "Eaten in the last few weeks (don't repeat): Old favourite" in p
        assert "Disliked (never suggest): Old favourite" in p


# ---------------------------------------------------------------------------
# start_generation_sync
# ---------------------------------------------------------------------------


class TestStartGeneration:
    def test_creates_generating_then_busy(self):
        res = mp.start_generation_sync()
        assert res["ok"] and status_of(res["plan_id"])["status"] == "generating"
        assert res["week_start"] == mp.plan_week_start(mp.diet_profiles.load()).isoformat()
        again = mp.start_generation_sync()
        assert again["ok"] is False and again["status_code"] == 409 and again["plan_id"] == res["plan_id"]

    def test_stale_generating_failed_and_new_allowed(self):
        old = mp.start_generation_sync()["plan_id"]
        stale = (datetime.now() - timedelta(minutes=mp.STALE_GENERATING_MINUTES + 1)).isoformat(timespec="seconds")
        mp._set_plan(old, created_at=stale)
        res = mp.start_generation_sync()
        assert res["ok"] and res["plan_id"] != old
        row = status_of(old)
        assert row["status"] == "failed" and "timed out" in row["error"]

    def test_nothing_to_plan(self):
        mp.diet_profiles.save({"batch_lunch_days": 0, "fresh_dinners_per_week": 0})
        res = mp.start_generation_sync()
        assert res["ok"] is False and res["status_code"] == 400


# ---------------------------------------------------------------------------
# generate_plan (LLM mocked)
# ---------------------------------------------------------------------------


def _gen_start():
    res = mp.start_generation_sync()
    return res["plan_id"], date.fromisoformat(res["week_start"])


class TestGeneratePlan:
    async def test_happy_path(self, monkeypatch):
        pid, start = _gen_start()
        calls = []
        monkeypatch.setattr(mp, "_ask", fake_ask([reply_for(start)], calls))
        before = metric_value(mp.MEAL_PLAN_GENERATED, result="ok")
        await mp.generate_plan(pid)
        plan = mp.get_plan(pid)
        assert plan["status"] == "draft" and plan["notes"] == "Balanced week."
        slots = [e["slot"] for e in plan["entries"]]
        assert slots.count("prep") == 1 and slots.count("lunch") == 4 and slots.count("dinner") == 4
        prep = next(e for e in plan["entries"] if e["slot"] == "prep")
        assert prep["day"] == start.isoformat() and prep["portions"] == 4
        lunch_days = sorted(e["day"] for e in plan["entries"] if e["slot"] == "lunch")
        assert lunch_days == [(start + timedelta(days=i)).isoformat() for i in range(1, 5)]
        assert len(calls) == 1 and calls[0]["thinking"] is True
        assert metric_value(mp.MEAL_PLAN_GENERATED, result="ok") == before + 1

    async def test_retry_path(self, monkeypatch):
        pid, start = _gen_start()
        calls = []
        monkeypatch.setattr(mp, "_ask", fake_ask(["I think you'd like soup.", reply_for(start)], calls))
        before = metric_value(mp.MEAL_PLAN_GENERATED, result="retry_ok")
        await mp.generate_plan(pid)
        assert status_of(pid)["status"] == "draft"
        assert [c["thinking"] for c in calls] == [True, False]
        assert "fix them" in calls[1]["prompt"] and "not a JSON object" in calls[1]["prompt"]
        assert metric_value(mp.MEAL_PLAN_GENERATED, result="retry_ok") == before + 1

    async def test_both_invalid_fails(self, monkeypatch):
        pid, _ = _gen_start()
        monkeypatch.setattr(mp, "_ask", fake_ask(["nope", '{"batch": null}']))
        await mp.generate_plan(pid)
        row = status_of(pid)
        assert row["status"] == "failed" and "usable plan" in row["error"]

    async def test_unreachable_marks_asleep(self, monkeypatch):
        pid, _ = _gen_start()
        calls = []
        monkeypatch.setattr(mp, "_ask", fake_ask([httpx.ConnectError("refused")], calls))
        before = metric_value(mp.MEAL_PLAN_GENERATED, result="asleep")
        await mp.generate_plan(pid)
        row = status_of(pid)
        assert row["status"] == "failed" and "asleep" in row["error"]
        assert len(calls) == 1  # no retry against a dead brain
        assert metric_value(mp.MEAL_PLAN_GENERATED, result="asleep") == before + 1

    async def test_first_timeout_then_ok(self, monkeypatch):
        pid, start = _gen_start()
        monkeypatch.setattr(mp, "_ask", fake_ask([httpx.ReadTimeout("slow"), reply_for(start)]))
        await mp.generate_plan(pid)
        assert status_of(pid)["status"] == "draft"

    async def test_both_timeouts(self, monkeypatch):
        pid, _ = _gen_start()
        monkeypatch.setattr(mp, "_ask", fake_ask([httpx.ReadTimeout("a"), httpx.ReadTimeout("b")]))
        await mp.generate_plan(pid)
        row = status_of(pid)
        assert row["status"] == "failed" and "too long" in row["error"]

    async def test_discarded_mid_flight_not_resurrected(self, monkeypatch):
        pid, start = _gen_start()

        def discard_then_reply():
            mp._set_plan(pid, status="discarded")
            return reply_for(start)

        monkeypatch.setattr(mp, "_ask", fake_ask([discard_then_reply]))
        await mp.generate_plan(pid)
        assert status_of(pid)["status"] == "discarded"
        with state_store.get_db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM meal_plan_entries WHERE plan_id = ?", (pid,)).fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0] == 0

    async def test_not_generating_is_noop(self, monkeypatch):
        pid, _ = _gen_start()
        mp._set_plan(pid, status="failed")
        monkeypatch.setattr(mp, "_ask", fake_ask([]))  # would raise StopIteration if called
        await mp.generate_plan(pid)
        assert status_of(pid)["status"] == "failed"
        await mp.generate_plan(999999)

    async def test_interaction_warning_and_med_text_scrubbed(self, monkeypatch):
        monkeypatch.setattr(mp, "_meds_data", lambda: MEDS)
        pid, start = _gen_start()
        reply = json.loads(reply_for(start, notes="Safe with Examplamab."))
        reply["dinners"][0]["ingredients"].append({"item": "grapefruit", "amount": "1"})
        monkeypatch.setattr(mp, "_ask", fake_ask([json.dumps(reply)]))
        await mp.generate_plan(pid)
        plan = mp.get_plan(pid)
        assert plan["status"] == "draft" and plan["notes"] == ""
        assert any("Grapefruit" in w for w in plan["warnings"])

    async def test_new_draft_supersedes_older_draft_same_week(self, monkeypatch):
        pid1, start = _gen_start()
        monkeypatch.setattr(mp, "_ask", fake_ask([reply_for(start), reply_for(start)]))
        await mp.generate_plan(pid1)
        pid2, _ = _gen_start()
        await mp.generate_plan(pid2)
        assert status_of(pid1)["status"] == "discarded"
        assert status_of(pid2)["status"] == "draft"

    def test_recover_interrupted(self):
        pid, _ = _gen_start()
        assert mp.recover_interrupted_plans() == 1
        assert status_of(pid)["status"] == "failed"
        assert mp.recover_interrupted_plans() == 0


# ---------------------------------------------------------------------------
# Reading plans
# ---------------------------------------------------------------------------


class TestReading:
    def test_get_plan_missing_vs_inventory(self):
        pm.add_item("Chickpeas", location="pantry")
        out = pm.add_item("Spinach", location="fridge")
        pm.mark_out(out["item"]["id"])
        pid = store_plan(
            START,
            dinners=[
                (1, recipe("Chickpea curry", ["canned chickpeas", "spinach", "coconut milk"])),
                (2, recipe("Coconut rice", ["Coconut milk", "rice"])),
            ],
        )
        plan = mp.get_plan(pid)
        assert plan["missing"] == ["Coconut milk", "Rice", "Spinach"]  # chickpeas on hand; spinach is out
        rec = plan["recipes"][str(plan["entries"][0]["recipe_id"])]
        assert rec["ingredients"][0]["have"] is True

    def test_get_plan_unknown(self):
        assert mp.get_plan(424242) is None

    def test_current_plans_prefers_covering_active(self):
        today = date.today()
        upcoming = store_plan(today + timedelta(days=3), dinners=[(0, recipe("Later", ["rice"]))], status="active")
        covering = store_plan(today - timedelta(days=2), dinners=[(0, recipe("Now", ["rice"]))], status="active")
        store_plan(today - timedelta(days=20), dinners=[(0, recipe("Ancient", ["rice"]))], status="active")
        cur = mp.current_plans()
        assert cur["active"]["id"] == covering
        assert cur["draft"] is None
        mp._set_plan(covering, status="replaced")
        assert mp.current_plans()["active"]["id"] == upcoming

    def test_current_plans_draft_over_failed_and_old_failed_hidden(self):
        today = date.today()
        failed = store_plan(today, dinners=[(0, recipe("F", ["rice"]))], status="failed")
        assert mp.current_plans()["draft"]["id"] == failed
        mp._set_plan(failed, created_at=(datetime.now() - timedelta(days=3)).isoformat())
        assert mp.current_plans()["draft"] is None
        mp._set_plan(failed, created_at=mp._now())
        draft = store_plan(today + timedelta(days=1), dinners=[(0, recipe("D", ["rice"]))])
        assert mp.current_plans()["draft"]["id"] == draft


# ---------------------------------------------------------------------------
# Accept / discard / reminders
# ---------------------------------------------------------------------------


def _reminder_ids(pid):
    with state_store.get_db() as conn:
        return json.loads(
            conn.execute("SELECT reminder_ids FROM meal_plans WHERE id = ?", (pid,)).fetchone()[0] or "[]"
        )


class TestAccept:
    def test_accept_flow(self, pantry_sched):
        pid = week_plan()
        res = mp.accept_plan(pid)
        assert res["ok"] and res["added_to_list"] == 5  # carrot deduped across recipes
        assert status_of(pid)["status"] == "active"
        groceries = sorted(i["item"] for i in state_store.get_shopping_list("grocery"))
        assert groceries == ["Black beans", "Carrot", "Pearl barley", "Peas", "Tortillas"]
        ids = _reminder_ids(pid)
        assert res["reminders"] == len(ids) == 3  # 1 prep + 2 dinners, lunches never remind
        for rid in ids:
            assert rid.startswith("mp-")
            assert f"reminder_{rid}" in pantry_sched.added
            row = get_reminder(rid)
            assert "meal" not in row["text"].lower()
            assert selfcare_action_for_reminder(row) is None
        again = mp.accept_plan(pid)
        assert again["ok"] is False and again["status_code"] == 409

    def test_accept_unknown_and_bad_already_have(self):
        assert mp.accept_plan(9999)["status_code"] == 404
        pid = week_plan()
        assert mp.accept_plan(pid, already_have="carrot")["status_code"] == 400
        assert status_of(pid)["status"] == "draft"

    def test_already_have_goes_to_pantry_not_grocery(self):
        state_store.add_shopping_item("Peas", "grocery")  # already on the list
        pid = week_plan()
        res = mp.accept_plan(pid, already_have=["carrots", 7, None])
        assert res["added_to_pantry"] == 1
        assert res["added_to_list"] == 3  # barley, beans, tortillas (peas already listed)
        groceries = [i["item"] for i in state_store.get_shopping_list("grocery")]
        assert "Carrot" not in groceries and groceries.count("Peas") == 1
        stocked = [i for i in pm.list_items("active") if i["name"] == "Carrot"]
        assert len(stocked) == 1 and stocked[0]["track_mode"] != "staple"

    def test_reminders_only_in_future(self, pantry_sched):
        start = date.today() - timedelta(days=1)
        pid = store_plan(
            start,
            batch=recipe("Barley stew", ["barley"], 60),
            dinners=[(0, recipe("Past dinner", ["rice"])), (4, recipe("Future dinner", ["beans"]))],
        )
        mp.accept_plan(pid)
        ids = _reminder_ids(pid)
        assert len(ids) == 1
        assert "Future dinner" in get_reminder(ids[0])["text"]

    def test_reminder_flags_off(self, pantry_sched):
        mp.diet_profiles.save({"reminders": {"prep": False, "dinners": False}})
        pid = week_plan()
        assert mp.accept_plan(pid)["reminders"] == 0
        assert pantry_sched.added == {}

    def test_older_active_same_week_replaced(self, pantry_sched):
        old = week_plan()
        mp.accept_plan(old)
        old_ids = _reminder_ids(old)
        new = week_plan()
        mp.accept_plan(new)
        assert status_of(old)["status"] == "replaced"
        assert status_of(new)["status"] == "active"
        for rid in old_ids:
            assert get_reminder(rid) is None
            assert f"reminder_{rid}" not in pantry_sched.added
        assert all(get_reminder(r) for r in _reminder_ids(new))


class TestDiscard:
    def test_discard_states(self):
        for status in ("draft", "failed", "active"):
            pid = week_plan(status=status)
            assert mp.discard_plan(pid)["ok"], status
            assert status_of(pid)["status"] == "discarded"
        assert mp.discard_plan(9999)["status_code"] == 404

    def test_discard_twice_409(self):
        pid = week_plan()
        mp.discard_plan(pid)
        assert mp.discard_plan(pid)["status_code"] == 409

    def test_generating_only_when_stuck(self):
        pid = mp.start_generation_sync()["plan_id"]
        assert mp.discard_plan(pid)["status_code"] == 409
        old = (datetime.now() - timedelta(minutes=mp.STUCK_PLAN_MINUTES + 1)).isoformat(timespec="seconds")
        mp._set_plan(pid, created_at=old)
        assert mp.discard_plan(pid)["ok"]

    def test_cancels_pending_keeps_delivered(self, pantry_sched):
        pid = week_plan()
        mp.accept_plan(pid)
        ids = _reminder_ids(pid)
        state_store.complete_reminder(ids[0])
        assert mp.discard_plan(pid)["ok"]
        assert get_reminder(ids[0]) is not None
        assert all(get_reminder(r) is None for r in ids[1:])
        assert all(f"reminder_{r}" not in pantry_sched.added for r in ids)


# ---------------------------------------------------------------------------
# Swap
# ---------------------------------------------------------------------------


def _entries(pid, slot):
    return [e for e in mp.get_plan(pid)["entries"] if e["slot"] == slot]


class TestSwap:
    @pytest.fixture(autouse=True)
    def _brain(self, monkeypatch):
        monkeypatch.setattr(mp, "brain_ready", _true)

    async def test_batch_swap_replaces_prep_and_lunches(self, monkeypatch):
        pid = week_plan()
        lunch = _entries(pid, "lunch")[0]
        old_rid = lunch["recipe_id"]
        calls = []
        monkeypatch.setattr(mp, "_ask", fake_ask([json.dumps(recipe("Farro salad", ["farro", "cucumber"], 40))], calls))
        res = await mp.swap_entry(lunch["id"])
        assert res["ok"] and res["added_to_list"] == 0  # draft: grocery untouched
        rids = {e["recipe_id"] for e in _entries(pid, "prep") + _entries(pid, "lunch")}
        assert len(rids) == 1 and old_rid not in rids
        assert res["plan"]["recipes"][str(rids.pop())]["name"] == "Farro salad"
        assert "batch-prep" in calls[0]["prompt"] and "Barley stew" in calls[0]["prompt"]
        # A swap runs inside a chat turn: it must never wait for chat to go idle.
        assert calls[0]["wait_for_idle"] is False
        dinners = _entries(pid, "dinner")
        assert all(e["recipe_id"] != old_rid for e in dinners)

    async def test_active_swap_updates_grocery_and_reminders(self, monkeypatch, pantry_sched):
        pid = week_plan()
        mp.accept_plan(pid)
        before_ids = _reminder_ids(pid)
        dinner = _entries(pid, "dinner")[0]
        monkeypatch.setattr(mp, "_ask", fake_ask([SWAP_REPLY]))
        res = await mp.swap_entry(dinner["id"])
        assert res["ok"] and res["added_to_list"] == 2
        groceries = [i["item"] for i in state_store.get_shopping_list("grocery")]
        assert "Firm tofu" in groceries and "Broccoli" in groceries
        after_ids = _reminder_ids(pid)
        assert len(after_ids) == len(before_ids) and set(after_ids).isdisjoint(before_ids)
        assert all(f"reminder_{r}" not in pantry_sched.added for r in before_ids)
        assert any("Tofu stir fry" in get_reminder(r)["text"] for r in after_ids)

    async def test_current_avoid_list_honoured(self, monkeypatch):
        pid = week_plan()  # snapshot has no avoid list
        mp.diet_profiles.save({"avoid": ["tofu"]})
        dinner = _entries(pid, "dinner")[0]
        calls = []
        monkeypatch.setattr(mp, "_ask", fake_ask([SWAP_REPLY, SWAP_REPLY], calls))
        res = await mp.swap_entry(dinner["id"])
        assert res["ok"] is False and res["status_code"] == 502
        assert len(calls) == 2
        assert "NEVER use (may be allergies): tofu" in calls[0]["prompt"]
        assert _entries(pid, "dinner")[0]["recipe_id"] == dinner["recipe_id"]

    async def test_second_attempt_used(self, monkeypatch):
        pid = week_plan()
        dinner = _entries(pid, "dinner")[0]
        monkeypatch.setattr(mp, "_ask", fake_ask(["garbage", SWAP_REPLY]))
        assert (await mp.swap_entry(dinner["id"]))["ok"]

    async def test_busy(self, monkeypatch):
        pid = week_plan()
        dinner = _entries(pid, "dinner")[0]
        lock = mp._get_swap_lock()
        await lock.acquire()
        try:
            res = await mp.swap_entry(dinner["id"])
        finally:
            lock.release()
        assert res["status_code"] == 409 and "in progress" in res["error"]

    async def test_brain_asleep(self, monkeypatch):
        pid = week_plan()
        monkeypatch.setattr(mp, "brain_ready", _false)
        res = await mp.swap_entry(_entries(pid, "dinner")[0]["id"])
        assert res["status_code"] == 503 and "asleep" in res["error"]

    async def test_llm_error(self, monkeypatch):
        pid = week_plan()
        monkeypatch.setattr(mp, "_ask", fake_ask([httpx.ConnectError("x")]))
        res = await mp.swap_entry(_entries(pid, "dinner")[0]["id"])
        assert res["status_code"] == 502

    async def test_discarded_plan(self, monkeypatch):
        pid = week_plan()
        eid = _entries(pid, "dinner")[0]["id"]
        mp.discard_plan(pid)
        res = await mp.swap_entry(eid)
        assert res["status_code"] == 409

    async def test_discarded_mid_flight(self, monkeypatch):
        pid = week_plan()
        dinner = _entries(pid, "dinner")[0]

        def discard_then_reply():
            mp.discard_plan(pid)
            return SWAP_REPLY

        monkeypatch.setattr(mp, "_ask", fake_ask([discard_then_reply]))
        res = await mp.swap_entry(dinner["id"])
        assert res["status_code"] == 409
        assert _entries(pid, "dinner")[0]["recipe_id"] == dinner["recipe_id"]

    async def test_unknown_entry(self):
        assert (await mp.swap_entry(987654))["status_code"] == 404

    async def test_lock_released_after_swap(self, monkeypatch):
        pid = week_plan()
        monkeypatch.setattr(mp, "_ask", fake_ask(["junk", "junk"]))
        await mp.swap_entry(_entries(pid, "dinner")[0]["id"])
        assert not mp._get_swap_lock().locked()


# ---------------------------------------------------------------------------
# Rate / reschedule
# ---------------------------------------------------------------------------


class TestRateAndReschedule:
    def test_rate(self):
        pid = week_plan()
        rid = _entries(pid, "dinner")[0]["recipe_id"]
        assert mp.rate_recipe(rid, "up")["ok"]
        with state_store.get_db() as conn:
            assert conn.execute("SELECT rating FROM recipes WHERE id = ?", (rid,)).fetchone()[0] == 5
        mp.rate_recipe(rid, "down")
        mp.rate_recipe(rid, "clear")
        with state_store.get_db() as conn:
            assert conn.execute("SELECT rating FROM recipes WHERE id = ?", (rid,)).fetchone()[0] is None

    @pytest.mark.parametrize("bad", [["up"], {"r": "up"}, None, 5, "UP", "meh"])
    def test_rate_bad_value(self, bad):
        assert mp.rate_recipe(1, bad)["status_code"] == 400

    def test_rate_unknown(self):
        assert mp.rate_recipe(31337, "up")["status_code"] == 404

    def test_reschedule_active_plans(self, pantry_sched):
        pid = week_plan()
        mp.accept_plan(pid)
        old_ids = _reminder_ids(pid)
        week_plan(days_ahead=30)  # a draft — not rescheduled
        mp.diet_profiles.save({"reminders": {"dinner_time": "19:15"}})
        assert mp.reschedule_active_plans() == 1
        new_ids = _reminder_ids(pid)
        assert len(new_ids) == len(old_ids) and set(new_ids).isdisjoint(old_ids)
        dinner_runs = [
            pantry_sched.added[f"reminder_{r}"]["run_date"] for r in new_ids if "cooking" in get_reminder(r)["text"]
        ]
        assert dinner_runs and all(d.hour == 19 and d.minute == 15 for d in dinner_runs)


# ---------------------------------------------------------------------------
# Tool text helpers
# ---------------------------------------------------------------------------


class TestToolText:
    def test_summary_states(self):
        assert "no meal plan" in mp.plan_summary()
        pid = mp.start_generation_sync()["plan_id"]
        assert "being made" in mp.plan_summary()
        mp._set_plan(pid, status="failed", error="timed out — try again")
        assert "failed: timed out" in mp.plan_summary()

    def test_summary_draft(self):
        week_plan()
        s = mp.plan_summary()
        assert "DRAFT" in s and "batch prep Barley stew (4 portions)" in s
        assert "dinner Bean tacos, ~20 min" in s and "Lunches" in s and "Need to buy:" in s

    def test_recipe_text_and_find_entry_across_plans(self):
        today = date.today()
        this_week = store_plan(
            today - timedelta(days=1),
            batch=recipe("This week stew", ["barley"], 60),
            dinners=[(1, recipe("Tonight pasta", ["pasta"]))],
            status="active",
        )
        next_start = today + timedelta(days=6)
        next_week = store_plan(
            next_start,
            batch=recipe("Next week chili", ["beans"], 60),
            dinners=[(2, recipe("Later curry", ["rice"]))],
            status="active",
        )
        txt = mp.recipe_text(None, "dinner")
        assert txt.startswith("Tonight pasta") and "1. Cook it all together." in txt and "(need to buy)" in txt
        later = (next_start + timedelta(days=2)).isoformat()
        assert mp.recipe_text(later, "dinner").startswith("Later curry")
        assert mp.recipe_text(None, "batch").startswith("This week stew")
        assert mp.recipe_text((next_start + timedelta(days=1)).isoformat(), "batch").startswith("Next week chili")
        assert mp.recipe_text("2001-01-01", "dinner").startswith("No dinner planned")

        eid = mp.find_entry(later, "dinner")
        entry = next(e for e in mp.get_plan(next_week)["entries"] if e["id"] == eid)
        assert entry["slot"] == "dinner"
        assert mp.find_entry(None, "dinner") in {e["id"] for e in mp.get_plan(this_week)["entries"]}
        assert mp.find_entry("2001-01-01", "dinner") is None


# ---------------------------------------------------------------------------
# Selfcare bridge guard (reminder_manager)
# ---------------------------------------------------------------------------


class TestSelfcareGuard:
    def test_mp_prefix_never_counts(self):
        assert selfcare_action_for_reminder({"id": "mp-abc123", "text": "Take your meds with this meal"}) is None

    def test_normal_ids_delegate(self):
        row = {"id": "r-1", "text": "Take your meds"}
        assert selfcare_action_for_reminder(row) == infer_selfcare_action_from_text(row["text"]) == "medication"
        assert selfcare_action_for_reminder({"id": "abc", "text": "eat a meal"}) == "meal"
        assert selfcare_action_for_reminder({"id": "abc", "text": None}) is None

    def test_none_row(self):
        assert selfcare_action_for_reminder(None) is None
        assert selfcare_action_for_reminder({}) is None


def test_asleep_message_without_wake():
    assert "asleep" in mp.asleep_message()


async def test_brain_ready(monkeypatch):
    monkeypatch.setattr(shared, "_http", None)
    assert await mp.brain_ready() is False

    class Client:
        def __init__(self, result):
            self.result = result

        async def get(self, url, timeout):
            if isinstance(self.result, Exception):
                raise self.result
            return httpx.Response(self.result)

    monkeypatch.setattr(shared, "_http", Client(200))
    assert await mp.brain_ready() is True
    monkeypatch.setattr(shared, "_http", Client(503))
    assert await mp.brain_ready() is False
    monkeypatch.setattr(shared, "_http", Client(httpx.ConnectError("x")))
    assert await mp.brain_ready() is False
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# _ask idle-wait (HA Assist / Telegram non-streaming turns count themselves in
# ACTIVE_REQUESTS — a swap called from inside one must not wait on itself)
# ---------------------------------------------------------------------------


class TestAskIdleWait:
    @pytest.fixture
    def fake_orchestrator(self, monkeypatch):
        calls = []

        async def call_model(url, model, messages, timeout, extra_body):
            calls.append(extra_body)
            return {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "usage": {}}

        mod = types.ModuleType("orchestrator.orchestrator")
        mod.call_model = call_model
        monkeypatch.setitem(sys.modules, "orchestrator.orchestrator", mod)
        return calls

    async def test_wait_for_idle_false_skips_the_wait(self, monkeypatch, fake_orchestrator):
        waited = []

        async def wait():
            waited.append(1)

        monkeypatch.setattr(mp, "_wait_for_idle_chat", wait)
        await mp._ask("p", 100, False, "meal_plan_swap", wait_for_idle=False)
        assert waited == [] and len(fake_orchestrator) == 1

    async def test_default_still_waits(self, monkeypatch, fake_orchestrator):
        waited = []

        async def wait():
            waited.append(1)

        monkeypatch.setattr(mp, "_wait_for_idle_chat", wait)
        await mp._ask("p", 100, True, "meal_plan")
        assert waited == [1]
