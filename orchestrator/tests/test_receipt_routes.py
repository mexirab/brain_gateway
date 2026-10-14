"""Route tests for receipt uploads (F-015 phase 3): POST /api/pantry/scan with
kind=receipt — every photo's hash stored, the same-photo duplicate guard
(409 reason=duplicate, bypassed by force=true), and stock-take kinds left
alone. Mounts only routes_pantry.router; start_scan is stubbed so no vision
runs. (PUT /api/pantry/diet non-object → 400 lives in
test_meal_planner_routes.py.)
"""

import hashlib
import json
import os
from unittest.mock import MagicMock

import pytest

JPEG_A = b"\xff\xd8\xff\xe0" + b"\x00" * 64
JPEG_B = b"\xff\xd8\xff\xe0" + b"\x01" * 64
JPEG_C = b"\xff\xd8\xff\xe0" + b"\x02" * 64


@pytest.fixture
def pm(tmp_db, tmp_path, monkeypatch):
    from orchestrator import pantry_manager, shared

    monkeypatch.setattr(shared, "PANTRY_PHOTOS_DIR", str(tmp_path / "pantry_photos"))
    monkeypatch.setattr(shared, "VISION_ENABLED", True)
    monkeypatch.setattr(shared, "PANTRY_MAX_UPLOAD_MB", 15)
    monkeypatch.setattr(shared, "VISION_MAX_IMAGE_SIZE", 20 * 1024 * 1024)
    return pantry_manager


@pytest.fixture
def start_scan(pm, monkeypatch):
    stub = MagicMock()
    monkeypatch.setattr(pm, "start_scan", stub)
    return stub


@pytest.fixture
def client(pm, start_scan):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.routes_pantry import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _files(*blobs):
    return [("files", (f"p{i}.jpg", b, "image/jpeg")) for i, b in enumerate(blobs)]


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _photos(pm):
    d = pm._photos_dir()
    return os.listdir(d) if os.path.isdir(d) else []


def _upload(client, *blobs, kind="receipt", force=None):
    data = {"kind": kind}
    if force is not None:
        data["force"] = force
    return client.post("/api/pantry/scan", files=_files(*blobs), data=data)


class TestReceiptUpload:
    def test_upload_stores_every_photo_hash(self, client, pm, start_scan):
        r = _upload(client, JPEG_A, JPEG_B)
        assert r.status_code == 202 and r.json()["ok"] is True
        sid = r.json()["scan_id"]
        row = pm._scan_row(sid)
        assert row["kind"] == "receipt"
        assert row["image_sha256"].split(",") == [_sha(JPEG_A), _sha(JPEG_B)]
        assert len(json.loads(row["photo_paths"])) == 2
        start_scan.assert_called_once_with(sid)

    def test_same_photo_again_is_409(self, client, pm, start_scan):
        first = _upload(client, JPEG_A, JPEG_B).json()["scan_id"]
        photos_before = len(_photos(pm))
        # Re-sending just the second section is caught too.
        r = _upload(client, JPEG_C, JPEG_B)
        assert r.status_code == 409
        body = r.json()
        assert body["ok"] is False and body["reason"] == "duplicate"
        assert body["duplicate_of"] == first and body["duplicate_status"] == "queued"
        assert len(_photos(pm)) == photos_before  # the refused upload's photos were deleted
        assert start_scan.call_count == 1

    def test_duplicate_status_reflects_committed(self, client, pm):
        first = _upload(client, JPEG_A).json()["scan_id"]
        pm._set_scan(first, status="committed")
        body = _upload(client, JPEG_A).json()
        assert body["duplicate_of"] == first and body["duplicate_status"] == "committed"

    @pytest.mark.parametrize("force", ["true", "TRUE"])
    def test_force_bypasses_guard(self, client, pm, force):
        first = _upload(client, JPEG_A).json()["scan_id"]
        r = _upload(client, JPEG_A, force=force)
        assert r.status_code == 202 and r.json()["scan_id"] != first

    def test_force_false_still_guarded(self, client):
        _upload(client, JPEG_A)
        assert _upload(client, JPEG_A, force="false").status_code == 409

    @pytest.mark.parametrize("status", ["failed", "discarded"])
    def test_dead_earlier_scan_does_not_block(self, client, pm, status):
        first = _upload(client, JPEG_A).json()["scan_id"]
        pm._set_scan(first, status=status)
        assert _upload(client, JPEG_A).status_code == 202

    @pytest.mark.parametrize("kind", ["fridge", "pantry", "freezer"])
    def test_stock_take_kinds_unaffected(self, client, pm, kind):
        assert _upload(client, JPEG_A, kind=kind).status_code == 202
        assert _upload(client, JPEG_A, kind=kind).status_code == 202
        # ...and a stock-take photo never blocks a receipt with the same bytes.
        assert _upload(client, JPEG_A).status_code == 202

    def test_bad_kind_lists_receipt(self, client):
        r = _upload(client, JPEG_A, kind="garage")
        assert r.status_code == 400 and "receipt" in r.json()["error"]
