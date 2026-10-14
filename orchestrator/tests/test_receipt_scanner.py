"""Tests for receipt scanning (F-015 phase 3): orchestrator/receipt_scanner.py
plus its hooks in pantry_manager (process_scan delegation, commit_scan →
_commit_receipt_scan, discard_scan minimising the record).

All vision calls are mocked. Receipt data is invented: generic grocery names,
made-up shop names, obviously fake card digits.
"""

import json
import logging
import os
from unittest.mock import AsyncMock

import pytest
from prometheus_client import REGISTRY

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def pm(tmp_db, tmp_path, monkeypatch):
    from orchestrator import pantry_manager, shared

    monkeypatch.setattr(shared, "PANTRY_PHOTOS_DIR", str(tmp_path / "pantry_photos"))
    monkeypatch.setattr(shared, "HELIOS_WAKE_ENABLED", False)
    monkeypatch.setattr(pantry_manager, "_vision_lock", None)
    return pantry_manager


@pytest.fixture
def rs(pm):
    from orchestrator import receipt_scanner

    return receipt_scanner


def _add(pm, name, **kw):
    res = pm.add_item(name, **kw)
    assert res["ok"], res
    return res["item"]


def _ln(raw, name=None, qty=1, food=True, cat="other", sure=True):
    out = {"raw": raw, "qty": qty, "food": food, "cat": cat, "sure": sure}
    if name is not None:
        out["name"] = name
    return out


def _reply(lines, store="Corner Grocer", date="2026-01-31", total=12.5, items_sold=None):
    return json.dumps(
        {"store": store, "date": date, "total": total, "items_sold": items_sold, "lines": lines},
        separators=(",", ":"),
    )


def _metric(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _latency_count(result=None):
    if result:
        return _metric("bgw_pantry_scan_latency_seconds_count", kind="receipt", result=result)
    return sum(_latency_count(r) for r in ("proposed", "empty", "failed"))


def _vision(monkeypatch, healthy=True, replies=None, side_effect=None):
    from orchestrator import vision_handler

    health = AsyncMock(return_value=healthy)
    analyze = AsyncMock(side_effect=side_effect) if side_effect else AsyncMock(side_effect=list(replies or []))
    monkeypatch.setattr(vision_handler, "check_vision_health", health)
    monkeypatch.setattr(vision_handler, "analyze_image", analyze)
    return analyze


def _receipt_scan(pm, photos=(JPEG,), status=None, proposed=None, sha=None):
    paths = [pm.save_scan_photo(p)["path"] for p in photos]
    sid = pm.create_scan("receipt", paths, sha)["id"]
    fields = {}
    if status:
        fields["status"] = status
    if proposed is not None:
        fields["proposed_json"] = json.dumps(proposed)
    if fields:
        pm._set_scan(sid, **fields)
    return sid, paths


def _pending_receipt(pm, rs, lines, **meta):
    """A receipt scan awaiting review, its proposal built by the real code."""
    parsed = rs.parse_receipt(_reply(lines, **meta))
    proposal = rs.build_receipt_proposal(parsed)
    receipt = {k: parsed[k] for k in ("store", "date", "total", "items_sold")}
    sid, paths = _receipt_scan(
        pm, status="pending_review", proposed={"items": proposal, "warnings": [], "receipt": receipt}
    )
    return sid, proposal, paths


def _aliases():
    from orchestrator import state_store

    with state_store.get_db() as conn:
        return {r["alias_norm"]: (r["item_name"], r["hits"]) for r in conn.execute("SELECT * FROM pantry_aliases")}


def _learn(rs, raw, name):
    from orchestrator import state_store

    with state_store.get_db() as conn:
        assert rs._learn_alias(conn, raw, name)


def _grocery(name):
    from orchestrator import state_store

    return state_store.add_shopping_item(name, "grocery")


def _checked():
    from orchestrator import state_store

    return {e["item"] for e in state_store.get_shopping_list("grocery", include_checked=True) if e["checked"]}


def _by_name(pm):
    return {i["name"]: i for i in pm.list_items("all")}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_alias_key(self, rs):
        assert rs.alias_key("KS ORG BBY SPIN 5OZ") == "KS ORG BBY SPIN OZ"
        assert rs.alias_key("ks  org-bby_spin!") == "KS ORG BBY SPIN"
        assert rs.alias_key(None) == "" and rs.alias_key("1234 5678") == ""
        assert len(rs.alias_key("ABCD " * 40)) <= rs.MAX_RAW_LEN

    def test_clean_raw_masks_long_digit_runs(self, rs):
        assert rs._clean_raw("CARD 00001234") == "CARD #"
        assert rs._clean_raw("REF 0000 TRN 99999999") == "REF # TRN #"
        assert rs._clean_raw("SODA 12OZ 355") == "SODA 12OZ 355"  # short numbers are product sizes
        assert "<" not in rs._clean_raw("<b>CHIPS</b>")
        assert len(rs._clean_raw("X" * 200)) <= rs.MAX_RAW_LEN

    def test_clean_store(self, rs):
        assert rs._clean_store("Corner Grocer #0042") == "Corner Grocer"
        out = rs._clean_store("Corner Grocer 000 Example Rd, Springfield 00000")
        assert not any(ch.isdigit() for ch in out) and "," not in out
        assert rs._clean_store("Sam's Fresh & Co") == "Sam's Fresh & Co"
        assert rs._clean_store("  12345  ") is None and rs._clean_store(None) is None
        assert len(rs._clean_store("Shop " * 30)) <= 40

    @pytest.mark.parametrize(
        "raw,want",
        [
            (12.5, 12.5),
            ("$1,234.50", 1234.5),
            ("0", 0.0),
            (-1, None),
            (100_000, None),
            (True, None),
            (None, None),
            ("abc", None),
        ],
    )
    def test_bounded_money(self, rs, raw, want):
        assert rs._bounded_money(raw) == want

    @pytest.mark.parametrize(
        "raw,want",
        [
            ("2026-01-31", "2026-01-31"),
            ("2026-01-31T10:15:00", "2026-01-31"),
            ("2026-02-30", None),
            ("01/31/2026", None),
            (None, None),
            ("", None),
        ],
    )
    def test_parse_date(self, rs, raw, want):
        assert rs._parse_date(raw) == want


# ---------------------------------------------------------------------------
# parse_receipt
# ---------------------------------------------------------------------------


class TestParseReceipt:
    def test_compact_json(self, rs):
        out = rs.parse_receipt(
            _reply([_ln("BNNA", "Bananas", qty=2, cat="produce")], store="Corner Grocer #12", items_sold=1)
        )
        assert out["store"] == "Corner Grocer" and out["date"] == "2026-01-31"
        assert out["total"] == 12.5 and out["items_sold"] == 1 and out["truncated"] is False
        assert out["lines"] == [
            {"raw": "BNNA", "name": "Bananas", "qty": 2, "food": True, "category": "produce", "sure": True}
        ]

    def test_fenced_json(self, rs):
        out = rs.parse_receipt("Here it is:\n```json\n" + _reply([_ln("OAT MLK", "Oat milk")]) + "\n```")
        assert [ln["name"] for ln in out["lines"]] == ["Oat milk"]

    def test_json_in_prose(self, rs):
        out = rs.parse_receipt("Sure! " + _reply([_ln("EGGS LG", "Eggs")]) + " Hope that helps.")
        assert [ln["name"] for ln in out["lines"]] == ["Eggs"]

    @pytest.mark.parametrize(
        "raw,name",
        [
            ("VISA 0000 APPR#000000", "Visa payment"),
            ("SUBTOTAL", "Subtotal"),
            ("XX1234", "Card"),
            ("XX1234", None),
            ("XX1234", "Unknown"),
            ("************0000", None),
            ("CHANGE DUE", "Change"),
            ("TAX", None),
        ],
    )
    def test_payment_lines_dropped_even_when_returned(self, rs, raw, name):
        out = rs.parse_receipt(_reply([_ln("BREAD WHT", "White bread"), _ln(raw, name)]))
        assert [ln["name"] for ln in out["lines"]] == ["White bread"]

    def test_no_card_digits_survive(self, rs):
        text = json.dumps({"lines": [_ln("GUM 00001111", "Gum"), _ln("XX0000", "Mystery"), _ln("MINTS", "Mints")]})
        dumped = json.dumps(rs.parse_receipt(text))
        assert "0000" not in dumped and "1111" not in dumped

    @pytest.mark.parametrize(
        "text,reason",
        [
            ("[Vision model timed out after 60s]", "busy"),
            ("[Vision analysis failed: ConnectError]", "unreachable"),
            ("[Vision model returned an error: HTTP 500]", "unreachable"),
            ("[No HTTP client available for vision]", "unreachable"),
        ],
    )
    def test_bracketed_failures(self, rs, pm, text, reason):
        out = rs.parse_receipt(text)
        assert out["reason"] == reason
        if reason == "busy":
            assert out["error"] == rs._BUSY_MSG
        else:
            assert out["error"] == pm._ASLEEP_MSG

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "I can see a receipt from QUUXMART with ZORBLAX FLAKES",
            "[Something odd happened: QUUXMART ZORBLAX]",
            '{"store": "QUUXMART", "items": []}',
        ],
    )
    def test_unreadable_never_echoes_model_text(self, rs, text):
        out = rs.parse_receipt(text)
        assert out["reason"] == "unreadable"
        assert "QUUXMART" not in out["error"] and "ZORBLAX" not in out["error"]

    @pytest.mark.parametrize("text", ['[{"raw":"timed out"}]', '["analysis failed"]'])
    def test_bracketed_json_list_is_not_a_transport_error(self, rs, text):
        assert rs.parse_receipt(text)["reason"] == "unreadable"

    def test_truncated_json_salvaged(self, rs):
        text = (
            '{"store":"Corner Grocer","date":"2026-01-31","total":12.50,"items_sold":9,"lines":['
            '{"raw":"BNNA","name":"Bananas","qty":1,"food":true,"cat":"produce","sure":true},'
            '{"raw":"EGGS LG","name":"Eggs","qty":2,"food":true,"cat":"protein","sure":true},'
            '{"raw":"OAT M'
        )
        out = rs.parse_receipt(text)
        assert out["truncated"] is True
        assert [ln["name"] for ln in out["lines"]] == ["Bananas", "Eggs"]
        assert out["store"] == "Corner Grocer" and out["date"] == "2026-01-31"
        assert out["total"] == 12.5 and out["items_sold"] == 9

    def test_truncated_with_null_header(self, rs):
        text = '{"store":null,"date":null,"total":null,"lines":[{"raw":"BNNA","name":"Bananas"},{"raw":"EG'
        out = rs.parse_receipt(text)
        assert out["truncated"] is True and out["store"] is None and out["total"] is None
        assert [ln["name"] for ln in out["lines"]] == ["Bananas"]

    @pytest.mark.parametrize("qty,want", [(0, 1), (-3, 1), ("two", 1), (None, 1), (2.7, 2), ("3", 3), (10**9, 50)])
    def test_qty_clamped(self, rs, qty, want):
        assert rs.parse_receipt(_reply([_ln("SODA", "Soda", qty=qty)]))["lines"][0]["qty"] == want

    def test_defaults_and_category(self, rs):
        text = json.dumps(
            {
                "lines": [
                    {"raw": "SODA", "name": "Soda"},
                    {"raw": "SOAP", "name": "Soap", "food": False},
                    {"raw": "YOGURT", "name": "Yogurt", "category": "dairy"},
                    {"raw": "THING", "name": "Thing", "cat": "weapons", "sure": False},
                    "not a dict",
                    {"raw": "", "name": ""},
                ]
            }
        )
        lines = rs.parse_receipt(text)["lines"]
        assert [ln["food"] for ln in lines] == [True, False, True, True]
        assert [ln["category"] for ln in lines] == ["other", "other", "dairy", "other"]
        assert [ln["sure"] for ln in lines] == [True, True, True, False]

    def test_name_falls_back_to_raw(self, rs):
        line = rs.parse_receipt(json.dumps({"lines": [{"raw": "KS ORG BBY SPIN"}]}))["lines"][0]
        assert line["raw"] == "KS ORG BBY SPIN" and line["name"] == "KS ORG BBY SPIN"

    def test_line_cap_per_photo(self, rs):
        lines = [
            _ln(f"ITEM Q{chr(65 + i % 26)}{chr(65 + i // 26)}", f"Item {i}") for i in range(rs.MAX_LINES_PER_PHOTO + 20)
        ]
        assert len(rs.parse_receipt(_reply(lines))["lines"]) == rs.MAX_LINES_PER_PHOTO

    def test_items_sold_bounds(self, rs):
        assert rs.parse_receipt(_reply([], items_sold=999))["items_sold"] is None
        assert rs.parse_receipt(_reply([], items_sold="x"))["items_sold"] is None
        assert rs.parse_receipt(_reply([], items_sold=0))["items_sold"] is None


# ---------------------------------------------------------------------------
# merge_photos
# ---------------------------------------------------------------------------


def _part(raws, truncated=False, **meta):
    base = {"store": None, "date": None, "total": None, "items_sold": None}
    base.update(meta)
    return {**base, "lines": [{"raw": r, "name": r.title()} for r in raws], "truncated": truncated}


class TestMergePhotos:
    def test_header_from_first_photo_that_has_it(self, rs):
        out = rs.merge_photos(
            [_part(["A"], date="2026-01-31"), _part(["B"], store="Shop", date="2026-02-01", total=9.5)]
        )
        assert out["store"] == "Shop" and out["date"] == "2026-01-31" and out["total"] == 9.5

    def test_seam_duplicate_dropped_once(self, rs):
        out = rs.merge_photos([_part(["APPLE", "BREAD"]), _part(["bread 2", "BREAD", "CHEESE"])])
        # "bread 2" and "BREAD" have the same alias key: one seam copy goes, the real second one stays
        assert [ln["raw"] for ln in out["lines"]] == ["APPLE", "BREAD", "BREAD", "CHEESE"]

    def test_repeats_within_one_photo_kept(self, rs):
        out = rs.merge_photos([_part(["SODA", "SODA"]), _part(["CHIPS"])])
        assert [ln["raw"] for ln in out["lines"]] == ["SODA", "SODA", "CHIPS"]

    def test_receipt_line_cap(self, rs):
        raws = [f"ITEM Q{chr(65 + i % 26)}{chr(65 + i // 26)}" for i in range(80)]
        out = rs.merge_photos([_part(raws), _part([r + " X" for r in raws])])
        assert len(out["lines"]) == rs.MAX_LINES_PER_RECEIPT

    def test_truncated_propagates(self, rs):
        assert rs.merge_photos([_part(["A"]), _part(["B"], truncated=True)])["truncated"] is True
        assert rs.merge_photos([_part(["A"])])["truncated"] is False


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


class TestNameScore:
    @pytest.mark.parametrize(
        "a,b",
        [
            ("roasted salted cashew", "cashew"),
            ("great value banana chip", "banana chip"),
            ("hummus with roasted vegetable", "hummus"),
        ],
    )
    def test_matches(self, rs, a, b):
        assert rs.name_score(a, b) >= rs.INVENTORY_MATCH
        assert rs.name_score(b, a) >= rs.INVENTORY_MATCH

    @pytest.mark.parametrize(
        "a,b",
        [
            ("peanut butter", "butter"),
            ("oat milk", "milk"),
            ("almond milk", "milk"),
            ("chocolate milk", "milk"),
            ("great value banana chip", "banana"),
            ("salt and pepper chip", "salt"),
        ],
    )
    def test_non_matches(self, rs, a, b):
        assert rs.name_score(a, b) < rs.INVENTORY_MATCH
        assert rs.name_score(b, a) < rs.INVENTORY_MATCH

    def test_equal_and_empty(self, rs):
        assert rs.name_score("milk", "milk") == 100
        assert rs.name_score("", "milk") == 0 and rs.name_score("milk", "") == 0


class TestBuildProposal:
    def test_alias_wins_over_model_name(self, pm, rs):
        spinach = _add(pm, "Baby spinach", category="produce", location="fridge")
        _learn(rs, "KS ORG BBY SPIN", "Baby spinach")
        parsed = rs.parse_receipt(_reply([_ln("KS ORG BBY SPIN", "Kale", sure=False)]))
        (line,) = rs.build_receipt_proposal(parsed)
        assert line["name"] == "Baby spinach" and line["read_name"] == "Baby spinach"
        assert line["matched_by"] == "alias" and line["confidence"] == "high" and line["include"] is True
        assert line["action"] == "restock" and line["match_id"] == spinach["id"]

    def test_inventory_match_restocks_with_match_location(self, pm, rs):
        cashews = _add(pm, "Cashews", category="snack", location="pantry", track_mode="count", qty=1)
        parsed = rs.parse_receipt(_reply([_ln("RST SLT CASHEW", "Roasted salted cashews", qty=2, cat="produce")]))
        (line,) = rs.build_receipt_proposal(parsed)
        assert line["action"] == "restock" and line["match_id"] == cashews["id"]
        assert line["name"] == "Cashews" and line["read_name"] == "Roasted salted cashews"
        assert line["location"] == "pantry" and line["track_mode"] == "count" and line["qty"] == 2
        assert line["category"] == "snack" and line["matched_by"] == "inventory"

    def test_same_inventory_item_matched_once(self, pm, rs):
        _add(pm, "Cashews", category="snack", location="pantry")
        parsed = rs.parse_receipt(_reply([_ln("CASHEW A", "Cashews"), _ln("CASHEW B", "Cashews")]))
        a, b = rs.build_receipt_proposal(parsed)
        assert a["action"] == "restock" and b["action"] == "add" and b["match_id"] is None

    def test_new_item_location_by_category(self, pm, rs):
        parsed = rs.parse_receipt(
            _reply(
                [
                    _ln("FRZ PEAS", "Frozen peas", cat="frozen"),
                    _ln("YOG", "Yogurt", cat="dairy"),
                    _ln("RICE", "Rice", cat="grain"),
                ]
            )
        )
        assert [ln["location"] for ln in rs.build_receipt_proposal(parsed)] == ["freezer", "fridge", "pantry"]

    def test_non_food_unticked_and_never_matched(self, pm, rs):
        _add(pm, "Paper towels", location="pantry")
        _grocery("Paper towels")
        parsed = rs.parse_receipt(_reply([_ln("PPR TWL", "Paper towels", food=False)]))
        (line,) = rs.build_receipt_proposal(parsed)
        assert line["include"] is False and line["match_id"] is None and line["action"] == "add"
        assert line["ticks"] == []

    def test_unsure_guess_unticked(self, pm, rs):
        parsed = rs.parse_receipt(_reply([_ln("ZQ CRNCH FLK", "Zq crnch flk", sure=False)]))
        (line,) = rs.build_receipt_proposal(parsed)
        assert line["include"] is False and line["confidence"] == "low" and line["matched_by"] == "model"

    def test_ticks_use_read_name_not_inventory_name(self, pm, rs):
        _add(pm, "Cashews", category="snack", location="pantry")
        _grocery("Cashews")
        _grocery("Roasted salted cashews")
        _grocery("Butter")
        parsed = rs.parse_receipt(
            _reply([_ln("RST SLT CASHEW", "Roasted salted cashews"), _ln("PNT BTR", "Peanut butter")])
        )
        cashew, pb = rs.build_receipt_proposal(parsed)
        assert cashew["ticks"] == ["Roasted salted cashews"]
        assert pb["ticks"] == []

    def test_gone_items_not_matched(self, pm, rs):
        item = _add(pm, "Cashews", location="pantry")
        pm.update_item(item["id"], {"status": "gone"})
        parsed = rs.parse_receipt(_reply([_ln("CASHEW", "Cashews")]))
        assert rs.build_receipt_proposal(parsed)[0]["match_id"] is None


# ---------------------------------------------------------------------------
# Duplicates / record minimising
# ---------------------------------------------------------------------------


class TestDuplicates:
    def _committed(self, pm, store="Corner Grocer", date="2026-01-31", total=12.5, status="committed", sha=None):
        sid, _ = _receipt_scan(
            pm, status=status, sha=sha, proposed={"receipt": {"store": store, "date": date, "total": total}}
        )
        return sid

    def test_find_duplicate(self, pm, rs):
        old = self._committed(pm)
        meta = {"store": "corner grocer", "date": "2026-01-31", "total": 12.5}
        assert rs._find_duplicate(9999, meta) == old
        assert rs._find_duplicate(old, meta) is None  # never itself
        assert rs._find_duplicate(9999, {**meta, "store": "Other Shop"}) is None
        assert rs._find_duplicate(9999, {**meta, "total": 12.51}) is None
        assert rs._find_duplicate(9999, {**meta, "date": None}) is None
        assert rs._find_duplicate(9999, {**meta, "total": None}) is None

    def test_find_duplicate_ignores_uncommitted(self, pm, rs):
        self._committed(pm, status="pending_review")
        self._committed(pm, status="discarded")
        assert rs._find_duplicate(9999, {"store": "Corner Grocer", "date": "2026-01-31", "total": 12.5}) is None

    def test_find_duplicate_after_minimise(self, pm, rs):
        sid, _ = _receipt_scan(
            pm,
            status="committed",
            proposed={
                "items": [{"key": 0}],
                "receipt": {"store": "Corner Grocer", "date": "2026-01-31", "total": 3.0, "items_sold": 1},
            },
        )
        rs.minimise_record(sid)
        assert rs._find_duplicate(9999, {"store": "Corner Grocer", "date": "2026-01-31", "total": 3.0}) == sid

    def test_photo_already_scanned(self, pm, rs):
        sid = self._committed(pm, sha="aaa111,bbb222")
        assert rs.photo_already_scanned("bbb222") == {"id": sid, "status": "committed"}
        assert rs.photo_already_scanned("aaa111")["id"] == sid
        assert rs.photo_already_scanned("ccc333") is None

    @pytest.mark.parametrize("status", ["failed", "discarded"])
    def test_photo_already_scanned_ignores_dead_scans(self, pm, rs, status):
        self._committed(pm, sha="aaa111", status=status)
        assert rs.photo_already_scanned("aaa111") is None

    def test_photo_already_scanned_receipts_only(self, pm, rs):
        sid = pm.create_scan("fridge", [pm.save_scan_photo(JPEG)["path"]], "aaa111")["id"]
        assert sid and rs.photo_already_scanned("aaa111") is None

    def test_minimise_record(self, pm, rs):
        sid, _ = _receipt_scan(
            pm,
            proposed={
                "items": [{"key": 0, "raw": "ZRBLX", "name": "Zorblax"}],
                "warnings": ["x"],
                "receipt": {
                    "store": "Corner Grocer",
                    "date": "2026-01-31",
                    "total": 4.0,
                    "items_sold": 1,
                    "duplicate_of": None,
                },
            },
        )
        rs.minimise_record(sid)
        assert json.loads(pm._scan_row(sid)["proposed_json"]) == {
            "receipt": {"store": "Corner Grocer", "date": "2026-01-31", "total": 4.0}
        }

    def test_minimise_record_tolerates_bad_rows(self, pm, rs):
        rs.minimise_record(424242)  # missing: no exception
        sid, _ = _receipt_scan(pm)
        pm._set_scan(sid, proposed_json="{not json")
        rs.minimise_record(sid)
        assert json.loads(pm._scan_row(sid)["proposed_json"]) == {
            "receipt": {"store": None, "date": None, "total": None}
        }


# ---------------------------------------------------------------------------
# process_scan → process_receipt
# ---------------------------------------------------------------------------


class TestProcessReceipt:
    async def test_happy_path_two_photos(self, pm, rs, monkeypatch):
        cashews = _add(pm, "Cashews", category="snack", location="pantry")
        proposed_before = _metric("bgw_pantry_scan_total", kind="receipt", result="proposed")
        lat_before = _latency_count()
        lines_before = {
            r: _metric("bgw_pantry_receipt_lines_total", result=r)
            for r in ("matched_inventory", "new", "non_food", "matched_alias")
        }
        analyze = _vision(
            monkeypatch,
            replies=[
                _reply(
                    [_ln("RST SLT CASHEW", "Roasted salted cashews"), _ln("BNNA", "Bananas")],
                    total=None,
                    items_sold=None,
                ),
                _reply([_ln("BNNA", "Bananas"), _ln("PPR TWL", "Paper towels", food=False)], store=None, items_sold=3),
            ],
        )
        sid, _ = _receipt_scan(pm, photos=(JPEG, PNG))
        await pm.process_scan(sid)

        assert analyze.await_count == 2
        assert analyze.await_args_list[0].kwargs["prompt"] == rs._RECEIPT_PROMPT
        scan = pm.get_scan(sid)
        assert scan["status"] == "pending_review"
        items = scan["proposed"]["items"]
        assert [i["read_name"] for i in items] == ["Roasted salted cashews", "Bananas", "Paper towels"]
        assert items[0]["match_id"] == cashews["id"]
        assert scan["proposed"]["receipt"] == {
            "store": "Corner Grocer",
            "date": "2026-01-31",
            "total": 12.5,
            "items_sold": 3,
            "duplicate_of": None,
        }
        assert scan["proposed"]["warnings"] == []

        assert _metric("bgw_pantry_scan_total", kind="receipt", result="proposed") == proposed_before + 1
        assert _latency_count() == lat_before + 1  # delegated: observed exactly once
        assert (
            _metric("bgw_pantry_receipt_lines_total", result="matched_inventory")
            == lines_before["matched_inventory"] + 1
        )
        assert _metric("bgw_pantry_receipt_lines_total", result="new") == lines_before["new"] + 1
        assert _metric("bgw_pantry_receipt_lines_total", result="non_food") == lines_before["non_food"] + 1

    async def test_warnings(self, pm, rs, monkeypatch):
        old, _ = _receipt_scan(
            pm,
            status="committed",
            proposed={"receipt": {"store": "Corner Grocer", "date": "2026-01-31", "total": 12.5}},
        )
        truncated = (
            '{"store":"Corner Grocer","date":"2026-01-31","total":12.5,"items_sold":5,"lines":['
            '{"raw":"BNNA","name":"Bananas"},{"raw":"EG'
        )
        _vision(monkeypatch, replies=[truncated])
        sid, _ = _receipt_scan(pm)
        await pm.process_scan(sid)
        scan = pm.get_scan(sid)
        assert scan["status"] == "pending_review"
        assert scan["proposed"]["receipt"]["duplicate_of"] == old
        warnings = " ".join(scan["proposed"]["warnings"])
        assert "too long" in warnings
        assert "says 5 items but Jess read 1" in warnings
        assert "already added" in warnings

    async def test_partial_unreadable_photo_becomes_warning(self, pm, monkeypatch):
        _vision(monkeypatch, replies=["no idea", _reply([_ln("BNNA", "Bananas")])])
        sid, _ = _receipt_scan(pm, photos=(JPEG, JPEG))
        await pm.process_scan(sid)
        scan = pm.get_scan(sid)
        assert scan["status"] == "pending_review"
        assert scan["proposed"]["warnings"][0].startswith("photo 1:")

    async def test_busy_stops_and_fails(self, pm, rs, monkeypatch):
        failed_before = _latency_count("failed")
        analyze = _vision(monkeypatch, replies=["[Vision model timed out after 60s]", _reply([_ln("A", "Apples")])])
        sid, _ = _receipt_scan(pm, photos=(JPEG, JPEG, JPEG))
        await pm.process_scan(sid)
        assert analyze.await_count == 1
        scan = pm.get_scan(sid)
        assert scan["status"] == "failed" and scan["error"] == rs._BUSY_MSG
        assert _latency_count("failed") == failed_before + 1

    async def test_unreachable_gives_asleep_message(self, pm, monkeypatch):
        analyze = _vision(monkeypatch, replies=["[Vision analysis failed: ConnectError]", _reply([])])
        sid, _ = _receipt_scan(pm, photos=(JPEG, JPEG))
        await pm.process_scan(sid)
        assert analyze.await_count == 1
        scan = pm.get_scan(sid)
        assert scan["status"] == "failed" and scan["error"] == pm._ASLEEP_MSG

    async def test_health_check_down_counts_once(self, pm, monkeypatch):
        lat_before = _latency_count()
        analyze = _vision(monkeypatch, healthy=False)
        sid, _ = _receipt_scan(pm)
        await pm.process_scan(sid)
        analyze.assert_not_awaited()
        assert pm.get_scan(sid)["status"] == "failed"
        assert _latency_count() == lat_before + 1

    async def test_all_unreadable_fails(self, pm, monkeypatch):
        _vision(monkeypatch, replies=["no idea", "QUUXMART ZORBLAX"])
        sid, _ = _receipt_scan(pm, photos=(JPEG, JPEG))
        await pm.process_scan(sid)
        scan = pm.get_scan(sid)
        assert scan["status"] == "failed"
        assert "photo 1" in scan["error"] and "photo 2" in scan["error"] and "ZORBLAX" not in scan["error"]

    async def test_crash_marks_failed(self, pm, monkeypatch):
        failed_before = _metric("bgw_pantry_scan_total", kind="receipt", result="failed")
        lat_before = _latency_count()
        _vision(monkeypatch, side_effect=RuntimeError("boom"))
        sid, _ = _receipt_scan(pm)
        await pm.process_scan(sid)
        scan = pm.get_scan(sid)
        assert scan["status"] == "failed" and scan["error"] == "internal error — tap Retry"
        assert _metric("bgw_pantry_scan_total", kind="receipt", result="failed") == failed_before + 1
        assert _latency_count() == lat_before + 1

    async def test_empty_receipt_is_reviewable(self, pm, monkeypatch):
        before = _metric("bgw_pantry_scan_total", kind="receipt", result="empty")
        _vision(monkeypatch, replies=[_reply([_ln("SUBTOTAL"), _ln("VISA 0000")])])
        sid, _ = _receipt_scan(pm)
        await pm.process_scan(sid)
        scan = pm.get_scan(sid)
        assert scan["status"] == "pending_review" and scan["proposed"]["items"] == []
        assert _metric("bgw_pantry_scan_total", kind="receipt", result="empty") == before + 1

    async def test_lines_not_counted_when_not_stored(self, pm, monkeypatch):
        sid, _ = _receipt_scan(pm)
        new_before = _metric("bgw_pantry_receipt_lines_total", result="new")

        async def discard_then_reply(*a, **k):
            pm._set_scan(sid, status="discarded")
            return _reply([_ln("BNNA", "Bananas")])

        _vision(monkeypatch, side_effect=discard_then_reply)
        await pm.process_scan(sid)
        assert pm.get_scan(sid)["status"] == "discarded"
        assert _metric("bgw_pantry_receipt_lines_total", result="new") == new_before


# ---------------------------------------------------------------------------
# commit / discard
# ---------------------------------------------------------------------------


class TestCommitReceipt:
    def test_restock_count_and_level(self, pm, rs):
        beans = _add(pm, "Canned beans", category="canned", location="pantry", track_mode="count", qty=2)
        oil = _add(pm, "Olive oil", category="condiment", location="pantry", track_mode="level", level="low")
        sid, proposal, _ = _pending_receipt(
            pm, rs, [_ln("CND BEANS", "Canned beans", qty=3), _ln("OLV OIL", "Olive oil")]
        )
        assert [p["match_id"] for p in proposal] == [beans["id"], oil["id"]]
        lines = [dict(p) for p in proposal]
        lines[0]["qty"] = 999  # clamped per line
        res = pm.commit_scan(sid, lines)
        assert res["ok"] and res["updated"] == 2 and res["added"] == 0
        assert pm.get_item(beans["id"])["qty"] == 2 + rs.MAX_QTY_PER_LINE
        assert pm.get_item(oil["id"])["level"] == "full"
        events = pm.get_events(beans["id"])
        assert events[0]["source"] == "receipt"

    def test_new_items_use_chosen_location(self, pm, rs):
        sid, proposal, _ = _pending_receipt(pm, rs, [_ln("FRZ PEAS", "Frozen peas", cat="frozen")])
        line = dict(proposal[0], location="pantry")
        res = pm.commit_scan(sid, [line])
        assert res["added"] == 1
        item = _by_name(pm)["Frozen peas"]
        assert item["location"] == "pantry" and item["source"] == "receipt"

    def test_matched_item_deleted_meanwhile_falls_back_to_add(self, pm, rs):
        cashews = _add(pm, "Cashews", category="snack", location="pantry")
        sid, proposal, _ = _pending_receipt(pm, rs, [_ln("CASHEW", "Cashews")])
        pm.delete_item(cashews["id"])
        res = pm.commit_scan(sid, [dict(p) for p in proposal])
        assert res["added"] == 1 and res["updated"] == 0
        assert "Cashews" in _by_name(pm)

    def test_match_id_outside_proposal_ignored(self, pm, rs):
        other = _add(pm, "Canned beans", category="canned", location="pantry", track_mode="count", qty=2)
        sid, proposal, _ = _pending_receipt(pm, rs, [_ln("BNNA", "Bananas")])
        res = pm.commit_scan(sid, [dict(proposal[0], match_id=other["id"])])
        assert res["added"] == 1 and res["updated"] == 0
        assert pm.get_item(other["id"])["qty"] == 2

    def test_duplicate_match_id_skipped(self, pm, rs):
        beans = _add(pm, "Canned beans", category="canned", location="pantry", track_mode="count", qty=2)
        sid, proposal, _ = _pending_receipt(pm, rs, [_ln("CND BEANS", "Canned beans"), _ln("BNNA", "Bananas")])
        res = pm.commit_scan(sid, [dict(proposal[0]), dict(proposal[1], match_id=beans["id"], name="Canned beans")])
        assert res["updated"] == 1 and res["skipped"] == 1
        assert pm.get_item(beans["id"])["qty"] == 3

    def test_excluded_and_nameless_lines_skipped(self, pm, rs):
        sid, proposal, _ = _pending_receipt(pm, rs, [_ln("BNNA", "Bananas"), _ln("EGGS", "Eggs")])
        res = pm.commit_scan(sid, [dict(proposal[0], include=False), dict(proposal[1], name="<>"), "junk"])
        assert res["skipped"] == 3 and res["added"] == 0 and pm.list_items("all") == []

    def test_alias_learned_for_renamed_line(self, pm, rs):
        sid, proposal, _ = _pending_receipt(pm, rs, [_ln("ZQ CRNCH FLK", "Zq crnch flk", sure=False)])
        res = pm.commit_scan(sid, [dict(proposal[0], name="Corn flakes", include=True)])
        assert res["aliases_learned"] == 1
        assert _aliases() == {"ZQ CRNCH FLK": ("Corn flakes", 1)}

    def test_untouched_model_guess_teaches_nothing(self, pm, rs):
        sid, proposal, _ = _pending_receipt(pm, rs, [_ln("BNNA", "Bananas")])
        res = pm.commit_scan(sid, [dict(proposal[0])])
        assert res["aliases_learned"] == 0 and _aliases() == {}

    def test_renamed_but_still_matched_teaches_nothing(self, pm, rs):
        _add(pm, "Cashews", category="snack", location="pantry")
        sid, proposal, _ = _pending_receipt(pm, rs, [_ln("RST SLT CASHEW", "Roasted salted cashews")])
        assert proposal[0]["match_id"]
        res = pm.commit_scan(sid, [dict(proposal[0], name="Cashew nuts")])
        assert res["aliases_learned"] == 0 and _aliases() == {}

    def test_alias_matched_line_reinforced(self, pm, rs):
        _learn(rs, "KS ORG BBY SPIN", "Baby spinach")
        sid, proposal, _ = _pending_receipt(pm, rs, [_ln("KS ORG BBY SPIN", "Kale")])
        res = pm.commit_scan(sid, [dict(proposal[0])])
        assert res["aliases_learned"] == 1
        assert _aliases() == {"KS ORG BBY SPIN": ("Baby spinach", 2)}

    def test_forged_raw_ignored(self, pm, rs):
        sid, proposal, _ = _pending_receipt(pm, rs, [_ln("ZQ CRNCH FLK", "Zq crnch flk")])
        res = pm.commit_scan(sid, [dict(proposal[0], name="Corn flakes", raw="FORGED TEXT")])
        assert res["aliases_learned"] == 1
        assert set(_aliases()) == {"ZQ CRNCH FLK"}

    def test_hand_added_line_without_key_learns_nothing(self, pm, rs):
        sid, _proposal, _ = _pending_receipt(pm, rs, [_ln("BNNA", "Bananas")])
        res = pm.commit_scan(sid, [{"name": "Apples", "raw": "APPL", "location": "fridge"}])
        assert res["added"] == 1 and res["aliases_learned"] == 0 and _aliases() == {}

    def test_grocery_ticks(self, pm, rs):
        _grocery("Bananas")
        _grocery("Butter")
        sid, proposal, _ = _pending_receipt(pm, rs, [_ln("BNNA", "Bananas"), _ln("PNT BTR", "Peanut butter")])
        res = pm.commit_scan(sid, [dict(p) for p in proposal])
        assert res["grocery_checked"] == 1 and _checked() == {"Bananas"}

    def test_grocery_tick_uses_read_name_not_restocked_item(self, pm, rs):
        milk = _add(pm, "Milk", category="dairy", location="fridge")
        _grocery("Milk")
        _grocery("Chocolate milk")
        parsed = rs.parse_receipt(_reply([_ln("CHOC MLK", "Chocolate milk", cat="dairy")]))
        (line,) = rs.build_receipt_proposal(parsed)
        assert line["match_id"] is None  # fuzzy matching would never do this...
        # ...but the user can point the line at Milk on the review screen.
        line.update(match_id=milk["id"], match_name="Milk", name="Milk", action="restock")
        sid, _ = _receipt_scan(pm, status="pending_review", proposed={"items": [line], "warnings": [], "receipt": {}})
        res = pm.commit_scan(sid, [dict(line)])
        assert res["updated"] == 1
        assert _checked() == {"Chocolate milk"}

    def test_grocery_tick_uses_corrected_name(self, pm, rs):
        _grocery("Corn flakes")
        sid, proposal, _ = _pending_receipt(pm, rs, [_ln("ZQ CRNCH FLK", "Zq crnch flk")])
        res = pm.commit_scan(sid, [dict(proposal[0], name="Corn flakes")])
        assert res["grocery_checked"] == 1 and _checked() == {"Corn flakes"}

    def test_all_or_nothing(self, pm, rs, monkeypatch):
        beans = _add(pm, "Canned beans", category="canned", location="pantry", track_mode="count", qty=2)
        _grocery("Corn flakes")
        sid, proposal, paths = _pending_receipt(
            pm, rs, [_ln("ZQ CRNCH FLK", "Zq crnch flk"), _ln("CND BEANS", "Canned beans")]
        )
        assert proposal[1]["match_id"] == beans["id"]

        def boom(*a, **k):
            raise RuntimeError("disk full")

        monkeypatch.setattr(rs, "_restock", boom)
        with pytest.raises(RuntimeError):
            pm.commit_scan(sid, [dict(proposal[0], name="Corn flakes"), dict(proposal[1])])

        assert set(_by_name(pm)) == {"Canned beans"}  # the new line was rolled back
        assert pm.get_item(beans["id"])["qty"] == 2
        assert _aliases() == {} and _checked() == set()
        scan = pm.get_scan(sid)
        assert scan["status"] == "pending_review" and len(scan["proposed"]["items"]) == 2
        assert all(os.path.exists(p) for p in paths)

        monkeypatch.undo()  # retry after the fault clears applies everything exactly once
        from orchestrator import shared

        monkeypatch.setattr(shared, "PANTRY_PHOTOS_DIR", os.path.dirname(paths[0]))
        res = pm.commit_scan(sid, [dict(proposal[0], name="Corn flakes"), dict(proposal[1])])
        assert res["added"] == 1 and res["updated"] == 1
        assert pm.get_item(beans["id"])["qty"] == 3

    def test_commit_minimises_record_and_deletes_photos(self, pm, rs):
        committed_before = _metric("bgw_pantry_scan_total", kind="receipt", result="committed")
        sid, proposal, paths = _pending_receipt(pm, rs, [_ln("ZRBLX", "Zorblax")], store="Corner Grocer", total=4.0)
        res = pm.commit_scan(sid, [dict(p) for p in proposal])
        assert res["ok"] is True
        row = pm._scan_row(sid)
        assert row["status"] == "committed" and row["committed_at"]
        assert json.loads(row["proposed_json"]) == {
            "receipt": {"store": "Corner Grocer", "date": "2026-01-31", "total": 4.0}
        }
        assert not any(os.path.exists(p) for p in paths)
        assert _metric("bgw_pantry_scan_total", kind="receipt", result="committed") == committed_before + 1

    def test_commit_twice_conflicts(self, pm, rs):
        sid, proposal, _ = _pending_receipt(pm, rs, [_ln("BNNA", "Bananas")])
        assert pm.commit_scan(sid, [dict(p) for p in proposal])["ok"]
        res = pm.commit_scan(sid, [dict(p) for p in proposal])
        assert res["ok"] is False and res["status_code"] == 409
        assert pm.list_items("all")[0]["qty"] in (1, None)

    def test_discard_minimises(self, pm, rs):
        sid, _proposal, paths = _pending_receipt(pm, rs, [_ln("ZRBLX", "Zorblax")])
        assert pm.discard_scan(sid)["ok"]
        row = pm._scan_row(sid)
        assert row["status"] == "discarded"
        assert "Zorblax" not in row["proposed_json"] and "ZRBLX" not in row["proposed_json"]
        assert not any(os.path.exists(p) for p in paths)

    def test_fridge_commit_unaffected(self, pm, rs):
        sid = pm.create_scan("fridge", [pm.save_scan_photo(JPEG)["path"]])["id"]
        lines = [{"key": 0, "name": "Eggs", "action": "add", "location": "fridge", "qty": 6}]
        pm._set_scan(sid, status="pending_review", proposed_json=json.dumps({"items": lines, "warnings": []}))
        res = pm.commit_scan(sid, lines)
        assert res["ok"] and "aliases_learned" not in res
        assert json.loads(pm._scan_row(sid)["proposed_json"])["items"]  # stock-takes are not minimised


# ---------------------------------------------------------------------------
# Privacy: receipt text never reaches the logs
# ---------------------------------------------------------------------------


class TestNoReceiptTextInLogs:
    async def test_scan_and_commit_log_no_receipt_text(self, pm, rs, monkeypatch, caplog):
        caplog.set_level(logging.DEBUG)
        _add(pm, "Quibblefruit jam", category="condiment", location="pantry")
        _grocery("Zorblax crunch flakes")
        _vision(
            monkeypatch,
            replies=[
                _reply(
                    [
                        _ln("ZRBLX CRNCH FLK", "Zorblax crunch flakes", qty=2),
                        _ln("QBLFRT JAM", "Quibblefruit jam"),
                        _ln("XX0000", "Card ending"),
                    ],
                    store="Quuxmart",
                    items_sold=7,
                ),
                "[Something odd: ZRBLX QUUXMART]",
            ],
        )
        sid, _ = _receipt_scan(pm, photos=(JPEG, JPEG))
        await pm.process_scan(sid)
        scan = pm.get_scan(sid)
        assert scan["status"] == "pending_review"
        lines = [dict(p, include=True) for p in scan["proposed"]["items"]]
        lines[0]["name"] = "Zorblax crunch flakes deluxe"
        assert pm.commit_scan(sid, lines)["ok"]

        # A crash path too
        _vision(monkeypatch, side_effect=RuntimeError("ZRBLX QUUXMART"))
        sid2, _ = _receipt_scan(pm)
        await pm.process_scan(sid2)

        assert caplog.records, "expected some log output"
        for rec in caplog.records:
            text = rec.getMessage().lower()
            for secret in ("zorblax", "zrblx", "quuxmart", "quibblefruit", "qblfrt"):
                assert secret not in text, (rec.name, rec.levelname)


# ---------------------------------------------------------------------------
# Payment filter must not eat real food (unit-test review finding)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,name",
    [
        ("KS SIG ALMONDS", "Kirkland Signature almonds"),
        ("FAGE TOTAL 0%", "Fage Total yogurt"),
        ("KOOL AID JAMMERS", "Kool-Aid"),
        ("BALANCE BAR", "Balance bar"),
        ("TRANS FAT FREE CRACKERS", "Crackers"),
        ("CASHEW R/S", "Total cashews"),
        ("AUTHENTIC SALSA", "Authentic salsa"),
        ("TAXCO MANGO", "Mango"),
        ("SIGNATURE SELECT SOUP", "Signature Select soup"),
    ],
)
def test_payment_filter_keeps_real_food(raw, name):
    from orchestrator import receipt_scanner as rs

    out = rs.parse_receipt(json.dumps({"lines": [{"raw": raw, "name": name, "qty": 1}]}))
    assert [ln["name"] for ln in out["lines"]] == [rs.pm._clean_name(name)]


@pytest.mark.parametrize(
    "raw",
    ["TOTAL 31.53", "SUBTOTAL", "VISA TEND 31.53", "CHANGE DUE 0.00", "TAX2 6.975%", "APPR# 011623", "XX1234"],
)
def test_payment_filter_still_drops_payment_lines(raw):
    from orchestrator import receipt_scanner as rs

    out = rs.parse_receipt(json.dumps({"lines": [{"raw": raw, "name": "Something", "qty": 1}]}))
    assert out["lines"] == []


@pytest.mark.parametrize("a,b", [("sweet potato", "potato"), ("brown sugar", "sugar"), ("ground beef", "beef")])
def test_name_score_kind_changing_modifiers_do_not_match(a, b):
    from orchestrator import receipt_scanner as rs

    assert rs.name_score(a, b) < rs.INVENTORY_MATCH
