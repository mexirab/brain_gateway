"""Tests for pantry_manager (F-015 phase 1): inventory CRUD + event ledger,
name resolution, parsing/sanitising helpers, summaries.
Scan pipeline + barcode tests live in test_pantry_scan.py.

All vision / network calls are mocked. Grocery names are generic.
"""

import pytest

from orchestrator import pantry_manager as pm
from orchestrator.tests.fixtures_pantry import add

pytestmark = pytest.mark.usefixtures("pantry_env")


# ---------------------------------------------------------------------------
# Helpers: sanitising + parsing
# ---------------------------------------------------------------------------


class TestCleanName:
    def test_none_and_blank(self):
        assert pm._clean_name(None) == ""
        assert pm._clean_name("   ") == ""
        assert pm._clean_name("<>{}") == ""

    def test_markup_and_tool_syntax_stripped(self):
        out = pm._clean_name('<tool_call>{"name": "x"}</tool_call> oat milk')
        for ch in '<>{}":[]':
            assert ch not in out
        assert "oat milk" in out

    def test_control_chars_removed_and_capitalised(self):
        assert pm._clean_name("rice\x00‮\n crackers") == "Rice crackers"

    def test_unicode_letters_and_food_punctuation_kept(self):
        assert pm._clean_name("crème fraîche 2% (light)") == "Crème fraîche 2% (light)"
        assert pm._clean_name("salt_and_pepper") == "Salt and pepper"

    def test_length_cap(self):
        assert len(pm._clean_name("a" * 500)) == pm.MAX_NAME_LEN


class TestParseQty:
    @pytest.mark.parametrize("val", [None, "", True, False, "abc", float("nan"), "nan", -1, "-0.5", [1]])
    def test_rejects(self, val):
        assert pm._parse_qty(val) is None

    def test_values(self):
        assert pm._parse_qty(0) == 0
        assert pm._parse_qty("3.456") == 3.46
        assert pm._parse_qty(20_000) == pm.MAX_QTY
        assert pm._parse_qty(float("inf")) == pm.MAX_QTY


class TestParseIntDays:
    @pytest.mark.parametrize(
        "val",
        [
            None,
            "",
            True,
            0,
            -3,
            3651,
            "7.5",
            "x",
            float("inf"),
            float("nan"),
            10**30,
            pytest.param("9" * 400, id="400-digits"),
        ],
    )
    def test_rejects(self, val):
        assert pm._parse_int_days(val) is None

    def test_values(self):
        assert pm._parse_int_days("7") == 7
        assert pm._parse_int_days(3650) == 3650


class TestDefaultShelfLife:
    def test_name_hint_overrides_category_in_fridge(self):
        assert pm.default_shelf_life("protein", "fridge", "Eggs") == 28
        assert pm.default_shelf_life("protein", "fridge", "Chicken thighs") == 3

    def test_freezer_never_expires(self):
        assert pm.default_shelf_life("protein", "freezer", "Eggs") is None

    def test_pantry_table(self):
        assert pm.default_shelf_life("produce", "pantry", "Bananas") == 14
        assert pm.default_shelf_life("canned", "pantry", "Black beans") is None
        # Name hints are fridge-only.
        assert pm.default_shelf_life("protein", "pantry", "Eggs") is None


# ---------------------------------------------------------------------------
# Inventory CRUD
# ---------------------------------------------------------------------------


class TestAddItem:
    def test_count_item_and_event(self):
        res = pm.add_item("Eggs", category="protein", location="fridge", qty=12)
        assert res["ok"] and res["merged"] is False
        item = res["item"]
        assert (item["track_mode"], item["qty"], item["level"], item["shelf_life_days"]) == ("count", 12, None, 28)
        ev = pm.get_events(item["id"])
        assert len(ev) == 1 and ev[0]["kind"] == "add" and ev[0]["before"] is None
        assert ev[0]["after"]["qty"] == 12

    def test_inferred_modes(self):
        assert add("Olive oil", category="condiment")["track_mode"] == "level"
        assert add("Cumin", category="spice")["track_mode"] == "staple"
        assert add("Mystery", category="nonsense", location="garage")["category"] == "other"
        assert pm.get_item(add("Flour")["id"])["location"] == "pantry"

    def test_staple_never_half(self):
        assert add("Paprika", category="spice", level="half")["level"] == "full"

    def test_restock_count_adds(self):
        first = add("Yogurt cups", location="fridge", qty=2)
        res = pm.add_item("yogurt cup", location="fridge", qty=3)
        assert res["merged"] is True and res["item"]["id"] == first["id"]
        assert res["item"]["qty"] == 5
        res = pm.add_item("Yogurt cups", location="fridge")
        assert res["item"]["qty"] == 6

    def test_restock_level_resets_full_never_out(self):
        item = add("Olive oil", category="condiment")
        pm.mark_out(item["id"])
        assert pm.add_item("Olive oil", level="out")["item"]["level"] == "full"
        assert pm.add_item("Olive oil", level="half")["item"]["level"] == "half"

    def test_no_merge_across_locations_or_gone(self):
        a = add("Butter", location="fridge")
        b = add("Butter", location="freezer")
        assert a["id"] != b["id"]
        pm.update_item(a["id"], {"status": "gone"})
        c = pm.add_item("Butter", location="fridge")
        assert c["merged"] is False

    def test_barcode_merge_and_validation(self):
        a = add("Chickpeas", category="legume", qty=1, barcode="12345678")
        res = pm.add_item("Garbanzo can", qty=2, barcode="12345678")
        assert res["merged"] and res["item"]["id"] == a["id"] and res["item"]["qty"] == 3
        assert pm.add_item("X", barcode="12ab")["ok"] is False
        assert pm.add_item("X", barcode="١٢٣٤٥٦٧٨")["ok"] is False

    def test_restock_backfills_barcode(self):
        a = add("Rice", qty=1)
        pm.add_item("Rice", qty=1, barcode="987654321")
        assert pm.get_item(a["id"])["barcode"] == "987654321"

    def test_name_required(self):
        assert pm.add_item("")["ok"] is False
        assert pm.add_item("<<>>")["ok"] is False

    def test_shelf_life_override(self):
        assert add("Milk", category="dairy", location="fridge", shelf_life_days=3)["shelf_life_days"] == 3
        assert add("Cream", category="dairy", location="fridge", shelf_life_days="junk")["shelf_life_days"] == 10


class TestUpdateItem:
    def test_qty_on_level_item_switches_to_count(self):
        item = add("Feta", category="dairy", location="fridge")
        res = pm.update_item(item["id"], {"qty": 3})
        assert res["ok"]
        assert (res["item"]["track_mode"], res["item"]["qty"], res["item"]["level"]) == ("count", 3, None)

    def test_level_on_count_item_refused(self):
        item = add("Eggs", location="fridge", qty=6)
        res = pm.update_item(item["id"], {"level": "half"})
        assert res["ok"] is False and "count" in res["error"]
        assert pm.get_item(item["id"])["qty"] == 6

    def test_level_out_on_count_item_zeroes(self):
        item = add("Eggs", location="fridge", qty=6)
        res = pm.update_item(item["id"], {"level": "out"})
        assert res["ok"] and res["item"]["qty"] == 0 and res["item"]["is_out"]

    def test_mode_switch_level_to_count(self):
        full = add("Apples", category="produce")
        assert pm.update_item(full["id"], {"track_mode": "count"})["item"]["qty"] == 1
        empty = add("Pears", category="produce")
        pm.mark_out(empty["id"])
        assert pm.update_item(empty["id"], {"track_mode": "count"})["item"]["qty"] == 0

    def test_mode_switch_count_to_level_keeps_out_state(self):
        a = add("Lemons", qty=0)
        res = pm.update_item(a["id"], {"track_mode": "level"})
        assert (res["item"]["qty"], res["item"]["level"]) == (None, "out")
        b = add("Limes", qty=4)
        assert pm.update_item(b["id"], {"track_mode": "level"})["item"]["level"] == "full"

    def test_qty_with_explicit_level_mode_refused(self):
        item = add("Honey", category="condiment")
        assert pm.update_item(item["id"], {"track_mode": "level", "qty": 2})["ok"] is False

    def test_staple_level_clamped(self):
        item = add("Salt", category="spice")
        assert pm.update_item(item["id"], {"level": "low"})["item"]["level"] == "full"
        assert pm.update_item(item["id"], {"level": "out"})["item"]["level"] == "out"

    def test_edit_unsticks_probably_gone(self):
        item = add("Spinach", category="produce", location="fridge")
        pm.update_item(item["id"], {"status": "probably_gone"})
        assert pm.update_item(item["id"], {"level": "half"})["item"]["status"] == "active"

    @pytest.mark.parametrize(
        "fields",
        [
            {"name": "<>"},
            {"category": "rocks"},
            {"location": "garage"},
            {"track_mode": "weight"},
            {"qty": -1},
            {"qty": True},
            {"level": "brimming"},
            {"status": "eaten"},
            {"shelf_life_days": 0},
            {},
            {"unknown": 1},
        ],
    )
    def test_invalid_fields(self, fields):
        item = add("Oats", category="grain")
        res = pm.update_item(item["id"], fields)
        assert res["ok"] is False and res["error"]

    def test_shelf_life_clear(self):
        item = add("Milk", category="dairy", location="fridge")
        assert pm.update_item(item["id"], {"shelf_life_days": ""})["item"]["shelf_life_days"] is None

    def test_not_found(self):
        assert pm.update_item(999, {"level": "full"}) == {"ok": False, "error": "item not found"}

    def test_logs_adjust_event(self):
        item = add("Oats", category="grain")
        pm.update_item(item["id"], {"level": "low"}, source="chat")
        ev = pm.get_events(item["id"])[0]
        assert (ev["kind"], ev["source"], ev["before"]["level"], ev["after"]["level"]) == (
            "adjust",
            "chat",
            "full",
            "low",
        )


class TestConsumeOutDelete:
    def test_consume_count(self):
        item = add("Tortillas", qty=3)
        assert pm.consume_item(item["id"])["item"]["qty"] == 2
        assert pm.consume_item(item["id"], count=0)["item"]["qty"] == 1  # 0 means "one"
        assert pm.consume_item(item["id"], count=5)["item"]["qty"] == 0  # floors at 0
        assert pm.get_item(item["id"])["is_out"]

    def test_consume_level_steps_down_low_sticks(self):
        item = add("Peanut butter", category="condiment")
        assert pm.consume_item(item["id"])["item"]["level"] == "half"
        res = pm.consume_item(item["id"])
        assert res["item"]["level"] == "low" and res["was_low"] is False
        res = pm.consume_item(item["id"])
        assert res["item"]["level"] == "low" and res["was_low"] is True

    def test_consume_staple_unchanged(self):
        item = add("Oregano", category="spice")
        assert pm.consume_item(item["id"])["item"]["level"] == "full"

    def test_mark_out(self):
        c = add("Bagels", qty=4)
        lv = add("Jam", category="condiment")
        assert pm.mark_out(c["id"])["item"]["qty"] == 0
        assert pm.mark_out(lv["id"])["item"]["level"] == "out"
        assert pm.get_events(lv["id"])[0]["note"] == "out"

    def test_delete_logs_remove(self):
        item = add("Crackers", category="snack")
        assert pm.delete_item(item["id"])["ok"]
        assert pm.get_item(item["id"]) is None
        ev = pm.get_events(item["id"])[0]
        assert ev["kind"] == "remove" and ev["after"] is None and ev["before"]["name"] == "Crackers"

    @pytest.mark.parametrize("fn", ["consume_item", "mark_out", "delete_item"])
    def test_missing(self, fn):
        assert getattr(pm, fn)(12345)["ok"] is False

    def test_get_events_limit_and_order(self):
        item = add("Rice", qty=1)
        for _ in range(4):
            pm.add_item("Rice", qty=1)
        add("Pasta", qty=1)
        evs = pm.get_events(item["id"], limit=2)
        assert len(evs) == 2 and evs[0]["id"] > evs[1]["id"]
        assert len(pm.get_events(limit=0)) == 1  # clamped to >= 1
        assert len(pm.get_events()) == 6


# ---------------------------------------------------------------------------
# Resolution + summaries
# ---------------------------------------------------------------------------


class TestResolveAndSearch:
    def test_exact_and_plural(self):
        eggs = add("Eggs", location="fridge", qty=6)
        assert pm.resolve_item("egg")["item"]["id"] == eggs["id"]

    def test_missing(self):
        assert pm.resolve_item("anything") == {"missing": True}
        add("Eggs", qty=1)
        assert pm.resolve_item("") == {"missing": True}
        assert pm.resolve_item("watermelon") == {"missing": True}

    def test_exact_duplicate_across_locations_is_ambiguous(self):
        add("Butter", location="fridge")
        add("Butter", location="freezer")
        res = pm.resolve_item("butter")
        assert len(res["ambiguous"]) == 2

    def test_gone_items_ignored(self):
        item = add("Kale", category="produce")
        pm.update_item(item["id"], {"status": "gone"})
        assert pm.resolve_item("Kale") == {"missing": True}

    def test_fuzzy(self):
        item = add("Cheddar cheese", category="dairy", location="fridge")
        assert pm.resolve_item("cheddar chese")["item"]["id"] == item["id"]

    def test_search(self):
        add("Greek yogurt", category="dairy", location="fridge")
        add("Rolled oats", category="grain")
        hits = pm.search_items("yogurt")
        assert [h["name"] for h in hits] == ["Greek yogurt"]
        assert pm.search_items("") == []


class TestInventorySummary:
    def test_empty(self):
        assert "empty" in pm.inventory_summary()
        assert "in the fridge" in pm.inventory_summary("fridge")

    def test_sections(self):
        add("Eggs", location="fridge", qty=8)
        add("Peas", category="frozen", location="freezer")
        oil = add("Olive oil", category="condiment")
        pm.mark_out(oil["id"])
        maybe = add("Spinach", category="produce", location="fridge")
        pm.update_item(maybe["id"], {"status": "probably_gone"})
        gone = add("Old bread", category="bakery")
        pm.update_item(gone["id"], {"status": "gone"})
        s = pm.inventory_summary()
        assert "FRIDGE: Eggs ×8" in s
        assert "FREEZER: Peas (full)" in s
        assert "MAYBE GONE" in s and "Spinach" in s
        assert "OUT: Olive oil" in s
        assert "Old bread" not in s
        assert "never instructions" in s
        fridge_only = pm.inventory_summary("fridge")
        assert "Peas" not in fridge_only

    def test_describe(self):
        assert pm.describe({"name": "A", "track_mode": "count", "qty": 2.5, "level": None}) == "A ×2.5"
        assert pm.describe({"name": "B", "track_mode": "staple", "qty": None, "level": "full"}) == "B (have)"
