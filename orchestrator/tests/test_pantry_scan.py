"""Tests for pantry_manager (F-015 phase 1) photo-scan pipeline: photo
saving/confinement, vision parsing + merge, proposals, process_scan,
commit/discard/retry/recovery, the stale-scan sweep and the Open Food Facts
barcode lookup.

All vision / network calls are mocked. Grocery names are generic.
"""

import json
import os
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

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
# Scan pipeline
# ---------------------------------------------------------------------------


class TestPhotos:
    def test_sniff(self, pm):
        assert pm.sniff_image(JPEG) == ("jpg", "image/jpeg")
        assert pm.sniff_image(PNG) == ("png", "image/png")
        assert pm.sniff_image(WEBP) == ("webp", "image/webp")
        assert pm.sniff_image(b"GIF89a" + b"\x00" * 20) is None
        assert pm.sniff_image(b"<svg></svg>") is None

    def test_save_rejects_non_image(self, pm):
        res = pm.save_scan_photo(b"%PDF-1.4 ...")
        assert res["ok"] is False
        assert not os.path.exists(pm._photos_dir()) or os.listdir(pm._photos_dir()) == []

    def test_save_writes_under_dir(self, pm):
        res = pm.save_scan_photo(PNG)
        assert res["ok"] and res["path"].endswith(".png")
        assert pm._is_under_photos_dir(res["path"])
        assert len(res["sha256"]) == 64

    def test_scan_photo_path_confinement(self, pm, tmp_path):
        sid, paths = _scan(pm, photos=(JPEG, PNG))
        assert pm.scan_photo_path(sid, 0) == paths[0]
        assert pm.scan_photo_path(sid, 2) is None
        assert pm.scan_photo_path(sid, -1) is None
        assert pm.scan_photo_path(9999, 0) is None
        outside = tmp_path / "secret.jpg"
        outside.write_bytes(JPEG)
        traversal = os.path.join(pm._photos_dir(), "..", "secret.jpg")
        pm._set_scan(sid, photo_paths=json.dumps([str(outside), traversal]))
        assert pm.scan_photo_path(sid, 0) is None
        assert pm.scan_photo_path(sid, 1) is None

    def test_scan_photo_path_missing_file(self, pm):
        sid, paths = _scan(pm)
        os.remove(paths[0])
        assert pm.scan_photo_path(sid, 0) is None

    def test_scan_public_hides_paths(self, pm):
        sid, _ = _scan(pm, photos=(JPEG, JPEG))
        scan = pm.get_scan(sid)
        assert scan["photo_count"] == 2 and scan["status"] == "queued"
        assert "photo_paths" not in scan and pm._photos_dir() not in json.dumps(scan)

    def test_count_open_scans(self, pm):
        _scan(pm)
        _scan(pm, status="pending_review")
        _scan(pm, status="failed")
        _scan(pm, status="committed")
        assert pm.count_open_scans() == 2


class TestParseVisionItems:
    def test_fenced_json(self, pm):
        text = 'Here you go:\n```json\n{"items": [{"name": "Eggs", "category": "protein", "count": 6}]}\n```'
        items = pm._parse_vision_items(text)["items"]
        assert items == [{"name": "Eggs", "category": "protein", "qty": 6.0, "level": None, "confidence": "medium"}]

    def test_bare_list(self, pm):
        out = pm._parse_vision_items('[{"name": "milk", "level": "half", "confidence": "high"}]')
        assert out["items"][0]["name"] == "Milk" and out["items"][0]["level"] == "half"

    def test_json_embedded_in_prose(self, pm):
        out = pm._parse_vision_items('Sure! {"items": [{"name": "Tofu"}]} hope that helps')
        assert out["items"][0]["name"] == "Tofu"

    def test_bracketed_vision_error(self, pm):
        out = pm._parse_vision_items("[Vision model timed out after 60s]")
        assert out == {"error": "Vision model timed out after 60s"}

    @pytest.mark.parametrize("text", ["", "   ", "I see a fridge with stuff", '{"stuff": 1}', '"items"'])
    def test_unreadable(self, pm, text):
        assert "error" in pm._parse_vision_items(text)

    def test_normalisation(self, pm):
        raw = {
            "items": [
                {"name": "Cans of soup", "category": "weird", "count": 2.7, "level": "out", "confidence": "??"},
                {"name": "Apples", "count": 0, "level": "low"},
                {"name": "<script>"},
                "not a dict",
                {"name": "Olives", "count": True},
            ]
        }
        items = pm._parse_vision_items(json.dumps(raw))["items"]
        assert items[0] == {
            "name": "Cans of soup",
            "category": "other",
            "qty": 2.0,
            "level": None,
            "confidence": "medium",
        }
        assert items[1]["qty"] is None and items[1]["level"] == "low"
        assert items[2]["name"] == "Script"
        assert items[3]["qty"] is None
        assert len(items) == 4

    def test_item_cap(self, pm):
        raw = {"items": [{"name": f"Item {i}"} for i in range(pm.MAX_ITEMS_PER_PHOTO + 20)]}
        assert len(pm._parse_vision_items(json.dumps(raw))["items"]) == pm.MAX_ITEMS_PER_PHOTO


def _det(name, qty=None, level=None, confidence="high", category="other"):
    return {"name": name, "category": category, "qty": qty, "level": level, "confidence": confidence}


class TestMergeAndProposal:
    def test_merge_takes_max_not_sum(self, pm):
        merged = pm._merge_photo_items(
            [
                [_det("Eggs", qty=6, confidence="low"), _det("Milk", level=None)],
                [_det("Egg", qty=4, confidence="high"), _det("Milk", level="half"), _det("Butter")],
            ]
        )
        by = {m["name"]: m for m in merged}
        assert set(by) == {"Eggs", "Milk", "Butter"}
        assert by["Eggs"]["qty"] == 6 and by["Eggs"]["confidence"] == "high"
        assert by["Milk"]["level"] == "half"

    def test_proposal_matches_same_location_only(self, pm):
        fridge_butter = _add(pm, "Butter", category="dairy", location="fridge")
        _add(pm, "Butter", category="dairy", location="freezer")
        lines = pm.build_proposal("fridge", [_det("Butter", level="half")])
        assert lines[0]["action"] == "update" and lines[0]["match_id"] == fridge_butter["id"]
        assert lines[0]["category"] == "dairy"
        lines = pm.build_proposal("pantry", [_det("Butter")])
        assert lines[0]["action"] == "add" and lines[0]["match_id"] is None

    def test_existing_matched_at_most_once(self, pm):
        _add(pm, "Greek yogurt", category="dairy", location="fridge")
        lines = pm.build_proposal("fridge", [_det("Greek yogurt"), _det("Yogurt greek")])
        assert [ln["action"] for ln in lines] == ["update", "add"]

    def test_line_shapes(self, pm):
        _add(pm, "Eggs", location="fridge", qty=12)
        lines = pm.build_proposal(
            "fridge",
            [_det("Eggs", level="half"), _det("Pickles", qty=None), _det("Unlabeled tub", confidence="low")],
        )
        eggs, pickles, tub = lines
        # Matched item keeps its own track mode (count) even though the photo saw a level.
        assert (eggs["track_mode"], eggs["qty"], eggs["level"]) == ("count", 1, None)
        assert (pickles["track_mode"], pickles["qty"], pickles["level"]) == ("level", None, "full")
        assert pickles["include"] is True and tub["include"] is False
        assert [ln["key"] for ln in lines] == [0, 1, 2]

    def test_transition_is_conditional(self, pm):
        sid, _ = _scan(pm)
        assert pm._transition(sid, ("queued",), "processing") is True
        assert pm._transition(sid, ("queued",), "processing") is False
        assert pm.get_scan(sid)["status"] == "processing"


def _vision(monkeypatch, healthy=True, replies=None, side_effect=None):
    from orchestrator import vision_handler

    health = AsyncMock(return_value=healthy)
    analyze = AsyncMock(side_effect=side_effect) if side_effect else AsyncMock(side_effect=list(replies or []))
    monkeypatch.setattr(vision_handler, "check_vision_health", health)
    monkeypatch.setattr(vision_handler, "analyze_image", analyze)
    return analyze


class TestProcessScan:
    async def test_happy_path(self, pm, monkeypatch):
        _add(pm, "Eggs", category="protein", location="fridge", qty=2)
        analyze = _vision(
            monkeypatch,
            replies=[
                json.dumps({"items": [{"name": "Eggs", "count": 6, "confidence": "high"}]}),
                json.dumps({"items": [{"name": "Eggs", "count": 5}, {"name": "Hummus", "category": "prepared"}]}),
            ],
        )
        sid, _ = _scan(pm, photos=(JPEG, PNG))
        await pm.process_scan(sid)
        assert analyze.await_count == 2
        assert analyze.await_args_list[0].args[0].startswith("data:image/jpeg;base64,")
        assert analyze.await_args_list[1].args[0].startswith("data:image/png;base64,")
        scan = pm.get_scan(sid)
        assert scan["status"] == "pending_review"
        items = {i["name"]: i for i in scan["proposed"]["items"]}
        assert items["Eggs"]["action"] == "update" and items["Eggs"]["qty"] == 6
        assert items["Hummus"]["action"] == "add"
        assert scan["proposed"]["warnings"] == []

    async def test_brain_asleep_fails_fast(self, pm, monkeypatch):
        analyze = _vision(monkeypatch, healthy=False)
        sid, _ = _scan(pm, photos=(JPEG, JPEG))
        await pm.process_scan(sid)
        analyze.assert_not_awaited()
        scan = pm.get_scan(sid)
        assert scan["status"] == "failed" and "asleep" in scan["error"]

    async def test_asleep_triggers_wake_when_enabled(self, pm, monkeypatch):
        from orchestrator import shared

        monkeypatch.setattr(shared, "HELIOS_WAKE_ENABLED", True)
        wake = MagicMock()
        monkeypatch.setattr(pm, "_maybe_wake_helios", wake)
        _vision(monkeypatch, healthy=False)
        sid, _ = _scan(pm)
        await pm.process_scan(sid)
        wake.assert_called_once()
        assert "waking it now" in pm.get_scan(sid)["error"]

    async def test_timeout_mid_scan_stops(self, pm, monkeypatch):
        analyze = _vision(monkeypatch, replies=["[Vision model timed out after 60s]", '{"items": []}'])
        sid, _ = _scan(pm, photos=(JPEG, JPEG, JPEG))
        await pm.process_scan(sid)
        assert analyze.await_count == 1
        scan = pm.get_scan(sid)
        assert scan["status"] == "failed" and "Helios" in scan["error"]

    async def test_partial_failure_keeps_warnings(self, pm, monkeypatch):
        _vision(monkeypatch, replies=["no idea, sorry", json.dumps({"items": [{"name": "Lettuce"}]})])
        sid, _ = _scan(pm, photos=(JPEG, JPEG))
        await pm.process_scan(sid)
        scan = pm.get_scan(sid)
        assert scan["status"] == "pending_review"
        assert [i["name"] for i in scan["proposed"]["items"]] == ["Lettuce"]
        assert scan["proposed"]["warnings"][0].startswith("photo 1:")

    async def test_all_unreadable_fails(self, pm, monkeypatch):
        _vision(monkeypatch, replies=["nope"])
        sid, _ = _scan(pm)
        await pm.process_scan(sid)
        scan = pm.get_scan(sid)
        assert scan["status"] == "failed" and "photo 1" in scan["error"]

    async def test_empty_result_is_reviewable(self, pm, monkeypatch):
        _vision(monkeypatch, replies=['{"items": []}'])
        sid, _ = _scan(pm)
        await pm.process_scan(sid)
        scan = pm.get_scan(sid)
        assert scan["status"] == "pending_review" and scan["proposed"]["items"] == []

    async def test_corrupt_and_outside_photos(self, pm, monkeypatch, tmp_path):
        analyze = _vision(monkeypatch, replies=[])
        sid, paths = _scan(pm, photos=(JPEG,))
        with open(paths[0], "wb") as f:
            f.write(b"not an image anymore")
        outside = tmp_path / "x.jpg"
        outside.write_bytes(JPEG)
        missing = os.path.join(pm._photos_dir(), "gone.jpg")
        pm._set_scan(sid, photo_paths=json.dumps([paths[0], str(outside), missing]))
        await pm.process_scan(sid)
        analyze.assert_not_awaited()
        err = pm.get_scan(sid)["error"]
        assert "photo 1: unsupported image" in err and "photo 2: invalid path" in err and "photo 3:" in err

    async def test_crash_marks_failed(self, pm, monkeypatch):
        _vision(monkeypatch, side_effect=RuntimeError("boom"))
        sid, _ = _scan(pm)
        await pm.process_scan(sid)
        scan = pm.get_scan(sid)
        assert scan["status"] == "failed" and scan["error"] == "internal error — tap Retry"

    async def test_not_queued_is_noop(self, pm, monkeypatch):
        analyze = _vision(monkeypatch, replies=['{"items": []}'])
        sid, _ = _scan(pm, status="discarded")
        await pm.process_scan(sid)
        analyze.assert_not_awaited()
        assert pm.get_scan(sid)["status"] == "discarded"
        await pm.process_scan(424242)  # missing scan: no exception

    async def test_discarded_mid_flight_not_resurrected(self, pm, monkeypatch):
        sid, _ = _scan(pm)

        async def discard_then_reply(*a, **k):
            pm._set_scan(sid, status="discarded")
            return '{"items": [{"name": "Milk"}]}'

        _vision(monkeypatch, side_effect=discard_then_reply)
        await pm.process_scan(sid)
        assert pm.get_scan(sid)["status"] == "discarded"


# ---------------------------------------------------------------------------
# Commit / discard / retry / recovery / sweep
# ---------------------------------------------------------------------------


class TestCommit:
    def test_commit_adds_updates_skips(self, pm):
        eggs = _add(pm, "Eggs", category="protein", location="fridge", qty=2)
        lines = [
            {"name": "Eggs", "match_id": eggs["id"], "track_mode": "count", "qty": 6, "category": "dairy"},
            {"name": "Hummus", "category": "prepared", "track_mode": "level", "level": "half"},
            {"name": "Unlabeled tub", "include": False},
            "garbage",
        ]
        sid, paths = _scan(pm, status="pending_review", proposed=lines)
        res = pm.commit_scan(sid, lines)
        assert res == {"ok": True, "added": 1, "updated": 1, "skipped": 2}
        e = pm.get_item(eggs["id"])
        assert e["qty"] == 6 and e["category"] == "dairy"  # update honours category
        hummus = pm.resolve_item("Hummus")["item"]
        assert hummus["location"] == "fridge" and hummus["level"] == "half"
        assert pm.get_scan(sid)["status"] == "committed"
        assert not any(os.path.exists(p) for p in paths)
        ev = pm.get_events(eggs["id"])[0]
        assert ev["source"] == "scan" and ev["ref_type"] == "scan" and ev["ref_id"] == sid

    def test_second_commit_409_and_no_double_count(self, pm):
        sid, _ = _scan(pm, status="pending_review")
        lines = [{"name": "Yogurt cups", "track_mode": "count", "qty": 4}]
        assert pm.commit_scan(sid, lines)["ok"]
        res = pm.commit_scan(sid, lines)
        assert res["ok"] is False and res["status_code"] == 409
        assert pm.resolve_item("Yogurt cups")["item"]["qty"] == 4

    def test_duplicate_match_id_not_applied_twice(self, pm):
        eggs = _add(pm, "Eggs", location="fridge", qty=2)
        sid, _ = _scan(pm, status="pending_review")
        line = {"name": "Eggs", "match_id": eggs["id"], "track_mode": "count", "qty": 6}
        res = pm.commit_scan(sid, [line, dict(line)])
        assert (res["updated"], res["skipped"]) == (1, 1)
        assert pm.get_item(eggs["id"])["qty"] == 6

    def test_stale_match_id_falls_back_to_add(self, pm):
        sid, _ = _scan(pm, status="pending_review")
        res = pm.commit_scan(sid, [{"name": "Kiwi", "match_id": 9999, "qty": 3, "track_mode": "count"}])
        assert res["added"] == 1
        assert pm.resolve_item("Kiwi")["item"]["qty"] == 3

    def test_bool_match_id_ignored(self, pm):
        first = _add(pm, "Carrots", location="fridge")
        sid, _ = _scan(pm, status="pending_review")
        pm.commit_scan(sid, [{"name": "Oat milk", "match_id": True, "level": "full"}])
        assert pm.get_item(first["id"])["name"] == "Carrots"
        assert pm.resolve_item("Oat milk")["item"]["id"] != first["id"]

    def test_validation(self, pm):
        assert pm.commit_scan(999, [])["status_code"] == 404
        sid, _ = _scan(pm, status="pending_review")
        assert pm.commit_scan(sid, {"name": "x"})["status_code"] == 400
        assert pm.commit_scan(sid, [{}] * (pm.MAX_COMMIT_ITEMS + 1))["status_code"] == 400
        assert pm.get_scan(sid)["status"] == "pending_review"
        queued, _ = _scan(pm)
        assert pm.commit_scan(queued, [])["status_code"] == 409

    def test_exception_rolls_back_to_review(self, pm, monkeypatch):
        sid, _ = _scan(pm, status="pending_review")

        def boom(*a, **k):
            raise RuntimeError("db gone")

        monkeypatch.setattr(pm, "add_item", boom)
        with pytest.raises(RuntimeError):
            pm.commit_scan(sid, [{"name": "Rice"}])
        assert pm.get_scan(sid)["status"] == "pending_review"


class TestDiscardRetryRecover:
    @pytest.mark.parametrize("status", ["pending_review", "failed"])
    def test_discard_allowed(self, pm, status):
        sid, paths = _scan(pm, status=status)
        assert pm.discard_scan(sid) == {"ok": True}
        assert pm.get_scan(sid)["status"] == "discarded"
        assert not os.path.exists(paths[0])

    @pytest.mark.parametrize("status", ["queued", "processing"])
    def test_discard_in_flight_only_when_stuck(self, pm, status):
        fresh, paths = _scan(pm, status=status)
        res = pm.discard_scan(fresh)
        assert res["status_code"] == 409 and os.path.exists(paths[0])
        old, _ = _scan(pm, status=status, age_minutes=pm.STUCK_SCAN_MINUTES + 1)
        assert pm.discard_scan(old)["ok"]

    def test_discard_terminal_and_missing(self, pm):
        sid, _ = _scan(pm, status="committed")
        assert pm.discard_scan(sid)["status_code"] == 409
        assert pm.discard_scan(999)["status_code"] == 404

    def test_retry(self, pm):
        sid, _ = _scan(pm, status="failed")
        pm._set_scan(sid, error="old")
        assert pm.retry_scan(sid) == {"ok": True}
        scan = pm.get_scan(sid)
        assert scan["status"] == "queued" and scan["error"] is None
        assert pm.retry_scan(sid)["status_code"] == 409
        assert pm.retry_scan(999)["status_code"] == 404

    def test_recover_interrupted(self, pm):
        q, _ = _scan(pm)
        p, _ = _scan(pm, status="processing")
        c, _ = _scan(pm, status="committing")
        r, _ = _scan(pm, status="pending_review")
        assert pm.recover_interrupted_scans() == 2
        assert pm.get_scan(q)["status"] == "failed" and "restart" in pm.get_scan(q)["error"]
        assert pm.get_scan(p)["status"] == "failed"
        assert pm.get_scan(c)["status"] == "pending_review"
        assert pm.get_scan(r)["status"] == "pending_review"


class TestSweep:
    def test_sweep(self, pm):
        old_age = (pm.SCAN_RETENTION_HOURS + 1) * 60
        stale_failed, sf_paths = _scan(pm, status="failed", age_minutes=old_age)
        stale_review, sr_paths = _scan(pm, status="pending_review", age_minutes=old_age)
        fresh_review, fr_paths = _scan(pm, status="pending_review")
        old_committed, _ = _scan(pm, status="committed", age_minutes=old_age)

        old_ts = time.time() - (pm.ORPHAN_PHOTO_HOURS + 1) * 3600
        # Referenced by an open scan but with an old mtime: must survive.
        os.utime(fr_paths[0], (old_ts, old_ts))
        orphan_old = pm.save_scan_photo(JPEG)["path"]
        os.utime(orphan_old, (old_ts, old_ts))
        orphan_young = pm.save_scan_photo(JPEG)["path"]

        res = pm.sweep_stale_scans()
        assert res == {"discarded": 2, "orphans": 1}
        assert pm.get_scan(stale_failed)["status"] == "discarded"
        assert pm.get_scan(stale_review)["status"] == "discarded"
        assert pm.get_scan(fresh_review)["status"] == "pending_review"
        assert pm.get_scan(old_committed)["status"] == "committed"
        assert not os.path.exists(sf_paths[0]) and not os.path.exists(sr_paths[0])
        assert os.path.exists(fr_paths[0])
        assert not os.path.exists(orphan_old)
        assert os.path.exists(orphan_young)

    def test_sweep_no_dir(self, pm):
        assert pm.sweep_stale_scans() == {"discarded": 0, "orphans": 0}


# ---------------------------------------------------------------------------
# Barcode lookup (Open Food Facts)
# ---------------------------------------------------------------------------

OFF = "https://off.example.test/api/v2/product/{}.json"


class TestBarcode:
    @respx.mock
    async def test_hit_then_cache(self, pm):
        route = respx.get(OFF.format("5000000000017")).mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": 1,
                    "product": {
                        "product_name": "Chunky <b>Tomato</b> Soup",
                        "brands": "Acme Foods, Other",
                        "quantity": "400 g",
                        "categories_tags": ["en:canned-foods", "en:soups"],
                    },
                },
            )
        )
        res = await pm.lookup_barcode("5000000000017")
        assert res == {
            "ok": True,
            "barcode": "5000000000017",
            "name": "Chunky bTomato/b Soup",
            "category": "canned",
            "brand": "Acme Foods",
            "quantity": "400 g",
            "source": "off",
        }
        assert route.calls.last.request.headers["user-agent"].startswith("BrainGateway-Pantry")
        again = await pm.lookup_barcode(" 5000000000017 ")
        assert again["source"] == "cache" and again["name"] == res["name"] and again["brand"] == "Acme Foods"
        assert route.call_count == 1

    @respx.mock
    async def test_404_miss_cached_with_ttl(self, pm):
        route = respx.get(OFF.format("12345678")).mock(return_value=httpx.Response(404))
        res = await pm.lookup_barcode("12345678")
        assert res["ok"] is False and res["reason"] == "miss"
        assert (await pm.lookup_barcode("12345678"))["reason"] == "miss"
        assert route.call_count == 1
        # Age the cached miss past the 7-day TTL → refetch.
        from orchestrator import state_store

        old = (datetime.now() - timedelta(days=8)).isoformat(timespec="seconds")
        with state_store.get_db() as conn:
            conn.execute("UPDATE barcode_cache SET fetched_at = ? WHERE barcode = ?", (old, "12345678"))
        await pm.lookup_barcode("12345678")
        assert route.call_count == 2

    @respx.mock
    async def test_status_zero_is_miss(self, pm):
        respx.get(OFF.format("87654321")).mock(return_value=httpx.Response(200, json={"status": 0}))
        assert (await pm.lookup_barcode("87654321"))["reason"] == "miss"

    @respx.mock
    async def test_unusable_name_is_miss(self, pm):
        respx.get(OFF.format("87654322")).mock(
            return_value=httpx.Response(200, json={"status": 1, "product": {"product_name": "<<>>"}})
        )
        assert (await pm.lookup_barcode("87654322"))["reason"] == "miss"

    @respx.mock
    async def test_oversize_body_errors(self, pm):
        respx.get(OFF.format("11111111")).mock(return_value=httpx.Response(200, content=b"x" * 300_000))
        res = await pm.lookup_barcode("11111111")
        assert res["ok"] is False and res["reason"] == "error"
        assert pm._cache_get("11111111") is None

    @respx.mock
    async def test_server_error_and_bad_json_not_cached(self, pm):
        respx.get(OFF.format("22222222")).mock(return_value=httpx.Response(503))
        respx.get(OFF.format("33333333")).mock(return_value=httpx.Response(200, content=b"<html>"))
        assert (await pm.lookup_barcode("22222222"))["reason"] == "error"
        assert (await pm.lookup_barcode("33333333"))["reason"] == "error"
        assert pm._cache_get("22222222") is None and pm._cache_get("33333333") is None

    @respx.mock
    async def test_network_error(self, pm):
        respx.get(OFF.format("44444444")).mock(side_effect=httpx.ConnectError("down"))
        assert (await pm.lookup_barcode("44444444"))["reason"] == "error"

    @respx.mock
    async def test_redirect_not_followed(self, pm):
        respx.get(OFF.format("55555555")).mock(
            return_value=httpx.Response(302, headers={"location": "http://evil.example.test/"})
        )
        assert (await pm.lookup_barcode("55555555"))["reason"] == "error"

    async def test_disabled(self, pm, monkeypatch):
        from orchestrator import shared

        monkeypatch.setattr(shared, "PANTRY_BARCODE_LOOKUP_ENABLED", False)
        assert (await pm.lookup_barcode("12345678"))["reason"] == "disabled"

    @pytest.mark.parametrize("code", ["", None, "1234567", "123456789012345", "12a45678", "١٢٣٤٥٦٧٨", "1234567８"])
    async def test_invalid(self, pm, code):
        assert (await pm.lookup_barcode(code))["reason"] == "invalid"

    def test_off_category(self, pm):
        assert pm._off_category(["en:frozen-vegetables"]) == "frozen"
        assert pm._off_category(["en:dairies", "en:cheeses"]) == "dairy"
        assert pm._off_category("not a list") == "other"
        assert pm._off_category([]) == "other"
