"""Tests for pantry_manager (F-015 phase 1): inventory CRUD + event ledger,
name resolution, parsing/sanitising helpers, summaries.
Scan pipeline + barcode tests live in test_pantry_scan.py.

All vision / network calls are mocked. Grocery names are generic.
"""

import json
from datetime import datetime, timedelta

import pytest

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 32


@pytest.fixture
def pm(tmp_db, tmp_path, monkeypatch):
    from orchestrator import pantry_manager, shared

    monkeypatch.setattr(shared, "PANTRY_PHOTOS_DIR", str(tmp_path / "pantry_photos"))
    monkeypatch.setattr(shared, "PANTRY_BARCODE_LOOKUP_ENABLED", True)
    monkeypatch.setattr(shared, "PANTRY_OFF_URL", "https://off.example.test")
    monkeypatch.setattr(shared, "HELIOS_WAKE_ENABLED", False)
    monkeypatch.setattr(pantry_manager, "_vision_lock", None)
    return pantry_manager


def _add(pm, name, **kw):
    res = pm.add_item(name, **kw)
    assert res["ok"], res
    return res["item"]


def _scan(pm, kind="fridge", photos=(JPEG,), status=None, proposed=None, age_minutes=0):
    paths = [pm.save_scan_photo(p)["path"] for p in photos]
    sid = pm.create_scan(kind, paths)["id"]
    fields = {}
    if status:
        fields["status"] = status
    if proposed is not None:
        fields["proposed_json"] = json.dumps({"items": proposed, "warnings": []})
    if age_minutes:
        fields["created_at"] = (datetime.now() - timedelta(minutes=age_minutes)).isoformat(timespec="seconds")
    if fields:
        pm._set_scan(sid, **fields)
    return sid, paths


# ---------------------------------------------------------------------------
# Helpers: sanitising + parsing
# ---------------------------------------------------------------------------


class TestCleanName:
    def test_none_and_blank(self, pm):
        assert pm._clean_name(None) == ""
        assert pm._clean_name("   ") == ""
        assert pm._clean_name("<>{}") == ""

    def test_markup_and_tool_syntax_stripped(self, pm):
        out = pm._clean_name('<tool_call>{"name": "x"}</tool_call> oat milk')
        for ch in '<>{}":[]':
            assert ch not in out
        assert "oat milk" in out

    def test_control_chars_removed_and_capitalised(self, pm):
        assert pm._clean_name("rice\x00‮\n crackers") == "Rice crackers"

    def test_unicode_letters_and_food_punctuation_kept(self, pm):
        assert pm._clean_name("crème fraîche 2% (light)") == "Crème fraîche 2% (light)"
        assert pm._clean_name("salt_and_pepper") == "Salt and pepper"

    def test_length_cap(self, pm):
        assert len(pm._clean_name("a" * 500)) == pm.MAX_NAME_LEN


class TestParseQty:
    @pytest.mark.parametrize("val", [None, "", True, False, "abc", float("nan"), "nan", -1, "-0.5", [1]])
    def test_rejects(self, pm, val):
        assert pm._parse_qty(val) is None

    def test_values(self, pm):
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
    def test_rejects(self, pm, val):
        assert pm._parse_int_days(val) is None

    def test_values(self, pm):
        assert pm._parse_int_days("7") == 7
        assert pm._parse_int_days(3650) == 3650


class TestDefaultShelfLife:
    def test_name_hint_overrides_category_in_fridge(self, pm):
        assert pm.default_shelf_life("protein", "fridge", "Eggs") == 28
        assert pm.default_shelf_life("protein", "fridge", "Chicken thighs") == 3

    def test_freezer_never_expires(self, pm):
        assert pm.default_shelf_life("protein", "freezer", "Eggs") is None

    def test_pantry_table(self, pm):
        assert pm.default_shelf_life("produce", "pantry", "Bananas") == 14
        assert pm.default_shelf_life("canned", "pantry", "Black beans") is None
        # Name hints are fridge-only.
        assert pm.default_shelf_life("protein", "pantry", "Eggs") is None


# ---------------------------------------------------------------------------
# Inventory CRUD
# ---------------------------------------------------------------------------


class TestAddItem:
    def test_count_item_and_event(self, pm):
        res = pm.add_item("Eggs", category="protein", location="fridge", qty=12)
        assert res["ok"] and res["merged"] is False
        item = res["item"]
        assert (item["track_mode"], item["qty"], item["level"], item["shelf_life_days"]) == ("count", 12, None, 28)
        ev = pm.get_events(item["id"])
        assert len(ev) == 1 and ev[0]["kind"] == "add" and ev[0]["before"] is None
        assert ev[0]["after"]["qty"] == 12

    def test_inferred_modes(self, pm):
        assert _add(pm, "Olive oil", category="condiment")["track_mode"] == "level"
        assert _add(pm, "Cumin", category="spice")["track_mode"] == "staple"
        assert _add(pm, "Mystery", category="nonsense", location="garage")["category"] == "other"
        assert pm.get_item(_add(pm, "Flour")["id"])["location"] == "pantry"

    def test_staple_never_half(self, pm):
        assert _add(pm, "Paprika", category="spice", level="half")["level"] == "full"

    def test_restock_count_adds(self, pm):
        first = _add(pm, "Yogurt cups", location="fridge", qty=2)
        res = pm.add_item("yogurt cup", location="fridge", qty=3)
        assert res["merged"] is True and res["item"]["id"] == first["id"]
        assert res["item"]["qty"] == 5
        res = pm.add_item("Yogurt cups", location="fridge")
        assert res["item"]["qty"] == 6

    def test_restock_level_resets_full_never_out(self, pm):
        item = _add(pm, "Olive oil", category="condiment")
        pm.mark_out(item["id"])
        assert pm.add_item("Olive oil", level="out")["item"]["level"] == "full"
        assert pm.add_item("Olive oil", level="half")["item"]["level"] == "half"

    def test_no_merge_across_locations_or_gone(self, pm):
        a = _add(pm, "Butter", location="fridge")
        b = _add(pm, "Butter", location="freezer")
        assert a["id"] != b["id"]
        pm.update_item(a["id"], {"status": "gone"})
        c = pm.add_item("Butter", location="fridge")
        assert c["merged"] is False

    def test_barcode_merge_and_validation(self, pm):
        a = _add(pm, "Chickpeas", category="legume", qty=1, barcode="12345678")
        res = pm.add_item("Garbanzo can", qty=2, barcode="12345678")
        assert res["merged"] and res["item"]["id"] == a["id"] and res["item"]["qty"] == 3
        assert pm.add_item("X", barcode="12ab")["ok"] is False
        assert pm.add_item("X", barcode="١٢٣٤٥٦٧٨")["ok"] is False

    def test_restock_backfills_barcode(self, pm):
        a = _add(pm, "Rice", qty=1)
        pm.add_item("Rice", qty=1, barcode="987654321")
        assert pm.get_item(a["id"])["barcode"] == "987654321"

    def test_name_required(self, pm):
        assert pm.add_item("")["ok"] is False
        assert pm.add_item("<<>>")["ok"] is False

    def test_shelf_life_override(self, pm):
        assert _add(pm, "Milk", category="dairy", location="fridge", shelf_life_days=3)["shelf_life_days"] == 3
        assert _add(pm, "Cream", category="dairy", location="fridge", shelf_life_days="junk")["shelf_life_days"] == 10


class TestUpdateItem:
    def test_qty_on_level_item_switches_to_count(self, pm):
        item = _add(pm, "Feta", category="dairy", location="fridge")
        res = pm.update_item(item["id"], {"qty": 3})
        assert res["ok"]
        assert (res["item"]["track_mode"], res["item"]["qty"], res["item"]["level"]) == ("count", 3, None)

    def test_level_on_count_item_refused(self, pm):
        item = _add(pm, "Eggs", location="fridge", qty=6)
        res = pm.update_item(item["id"], {"level": "half"})
        assert res["ok"] is False and "count" in res["error"]
        assert pm.get_item(item["id"])["qty"] == 6

    def test_level_out_on_count_item_zeroes(self, pm):
        item = _add(pm, "Eggs", location="fridge", qty=6)
        res = pm.update_item(item["id"], {"level": "out"})
        assert res["ok"] and res["item"]["qty"] == 0 and res["item"]["is_out"]

    def test_mode_switch_level_to_count(self, pm):
        full = _add(pm, "Apples", category="produce")
        assert pm.update_item(full["id"], {"track_mode": "count"})["item"]["qty"] == 1
        empty = _add(pm, "Pears", category="produce")
        pm.mark_out(empty["id"])
        assert pm.update_item(empty["id"], {"track_mode": "count"})["item"]["qty"] == 0

    def test_mode_switch_count_to_level_keeps_out_state(self, pm):
        a = _add(pm, "Lemons", qty=0)
        res = pm.update_item(a["id"], {"track_mode": "level"})
        assert (res["item"]["qty"], res["item"]["level"]) == (None, "out")
        b = _add(pm, "Limes", qty=4)
        assert pm.update_item(b["id"], {"track_mode": "level"})["item"]["level"] == "full"

    def test_qty_with_explicit_level_mode_refused(self, pm):
        item = _add(pm, "Honey", category="condiment")
        assert pm.update_item(item["id"], {"track_mode": "level", "qty": 2})["ok"] is False

    def test_staple_level_clamped(self, pm):
        item = _add(pm, "Salt", category="spice")
        assert pm.update_item(item["id"], {"level": "low"})["item"]["level"] == "full"
        assert pm.update_item(item["id"], {"level": "out"})["item"]["level"] == "out"

    def test_edit_unsticks_probably_gone(self, pm):
        item = _add(pm, "Spinach", category="produce", location="fridge")
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
    def test_invalid_fields(self, pm, fields):
        item = _add(pm, "Oats", category="grain")
        res = pm.update_item(item["id"], fields)
        assert res["ok"] is False and res["error"]

    def test_shelf_life_clear(self, pm):
        item = _add(pm, "Milk", category="dairy", location="fridge")
        assert pm.update_item(item["id"], {"shelf_life_days": ""})["item"]["shelf_life_days"] is None

    def test_not_found(self, pm):
        assert pm.update_item(999, {"level": "full"}) == {"ok": False, "error": "item not found"}

    def test_logs_adjust_event(self, pm):
        item = _add(pm, "Oats", category="grain")
        pm.update_item(item["id"], {"level": "low"}, source="chat")
        ev = pm.get_events(item["id"])[0]
        assert (ev["kind"], ev["source"], ev["before"]["level"], ev["after"]["level"]) == (
            "adjust",
            "chat",
            "full",
            "low",
        )


class TestConsumeOutDelete:
    def test_consume_count(self, pm):
        item = _add(pm, "Tortillas", qty=3)
        assert pm.consume_item(item["id"])["item"]["qty"] == 2
        assert pm.consume_item(item["id"], count=0)["item"]["qty"] == 1  # 0 means "one"
        assert pm.consume_item(item["id"], count=5)["item"]["qty"] == 0  # floors at 0
        assert pm.get_item(item["id"])["is_out"]

    def test_consume_level_steps_down_low_sticks(self, pm):
        item = _add(pm, "Peanut butter", category="condiment")
        assert pm.consume_item(item["id"])["item"]["level"] == "half"
        res = pm.consume_item(item["id"])
        assert res["item"]["level"] == "low" and res["was_low"] is False
        res = pm.consume_item(item["id"])
        assert res["item"]["level"] == "low" and res["was_low"] is True

    def test_consume_staple_unchanged(self, pm):
        item = _add(pm, "Oregano", category="spice")
        assert pm.consume_item(item["id"])["item"]["level"] == "full"

    def test_mark_out(self, pm):
        c = _add(pm, "Bagels", qty=4)
        lv = _add(pm, "Jam", category="condiment")
        assert pm.mark_out(c["id"])["item"]["qty"] == 0
        assert pm.mark_out(lv["id"])["item"]["level"] == "out"
        assert pm.get_events(lv["id"])[0]["note"] == "out"

    def test_delete_logs_remove(self, pm):
        item = _add(pm, "Crackers", category="snack")
        assert pm.delete_item(item["id"])["ok"]
        assert pm.get_item(item["id"]) is None
        ev = pm.get_events(item["id"])[0]
        assert ev["kind"] == "remove" and ev["after"] is None and ev["before"]["name"] == "Crackers"

    @pytest.mark.parametrize("fn", ["consume_item", "mark_out", "delete_item"])
    def test_missing(self, pm, fn):
        assert getattr(pm, fn)(12345)["ok"] is False

    def test_get_events_limit_and_order(self, pm):
        item = _add(pm, "Rice", qty=1)
        for _ in range(4):
            pm.add_item("Rice", qty=1)
        _add(pm, "Pasta", qty=1)
        evs = pm.get_events(item["id"], limit=2)
        assert len(evs) == 2 and evs[0]["id"] > evs[1]["id"]
        assert len(pm.get_events(limit=0)) == 1  # clamped to >= 1
        assert len(pm.get_events()) == 6


# ---------------------------------------------------------------------------
# Resolution + summaries
# ---------------------------------------------------------------------------


class TestResolveAndSearch:
    def test_exact_and_plural(self, pm):
        eggs = _add(pm, "Eggs", location="fridge", qty=6)
        assert pm.resolve_item("egg")["item"]["id"] == eggs["id"]

    def test_missing(self, pm):
        assert pm.resolve_item("anything") == {"missing": True}
        _add(pm, "Eggs", qty=1)
        assert pm.resolve_item("") == {"missing": True}
        assert pm.resolve_item("watermelon") == {"missing": True}

    def test_exact_duplicate_across_locations_is_ambiguous(self, pm):
        _add(pm, "Butter", location="fridge")
        _add(pm, "Butter", location="freezer")
        res = pm.resolve_item("butter")
        assert len(res["ambiguous"]) == 2

    def test_gone_items_ignored(self, pm):
        item = _add(pm, "Kale", category="produce")
        pm.update_item(item["id"], {"status": "gone"})
        assert pm.resolve_item("Kale") == {"missing": True}

    def test_fuzzy(self, pm):
        item = _add(pm, "Cheddar cheese", category="dairy", location="fridge")
        assert pm.resolve_item("cheddar chese")["item"]["id"] == item["id"]

    def test_search(self, pm):
        _add(pm, "Greek yogurt", category="dairy", location="fridge")
        _add(pm, "Rolled oats", category="grain")
        hits = pm.search_items("yogurt")
        assert [h["name"] for h in hits] == ["Greek yogurt"]
        assert pm.search_items("") == []


class TestInventorySummary:
    def test_empty(self, pm):
        assert "empty" in pm.inventory_summary()
        assert "in the fridge" in pm.inventory_summary("fridge")

    def test_sections(self, pm):
        _add(pm, "Eggs", location="fridge", qty=8)
        _add(pm, "Peas", category="frozen", location="freezer")
        oil = _add(pm, "Olive oil", category="condiment")
        pm.mark_out(oil["id"])
        maybe = _add(pm, "Spinach", category="produce", location="fridge")
        pm.update_item(maybe["id"], {"status": "probably_gone"})
        gone = _add(pm, "Old bread", category="bakery")
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

    def test_describe(self, pm):
        assert pm.describe({"name": "A", "track_mode": "count", "qty": 2.5, "level": None}) == "A ×2.5"
        assert pm.describe({"name": "B", "track_mode": "staple", "qty": None, "level": "full"}) == "B (have)"
