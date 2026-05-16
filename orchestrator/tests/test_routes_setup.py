"""
Tests for routes_setup.py — the `/api/setup/*` first-boot setup-wizard backend.

Covers the JSON helpers (`_atomic_write_json`, `_read_json`, `_setup_state`,
`is_first_boot`) and the three async endpoints (`get_setup_status`,
`get_hardware_scan`, `post_setup_complete`).

All filesystem state is redirected to `tmp_path` via monkeypatching the
module-level `_SETUP_STATE_PATH` / `_HARDWARE_SCAN_PATH` constants — the real
`/app/data` files are never touched.

Requires full orchestrator dependencies (runs inside Docker).
"""

from __future__ import annotations

import json

import pytest


def _can_import():
    try:
        from orchestrator import routes_setup  # noqa: F401

        return True
    except (ImportError, ModuleNotFoundError):
        return False


_skip_no_deps = pytest.mark.skipif(
    not _can_import(),
    reason="routes_setup requires full orchestrator dependencies (fastapi)",
)


def _body(response):
    """Decode the JSON body of a JSONResponse into a dict."""
    return json.loads(response.body)


@pytest.fixture
def routes_setup(tmp_path, monkeypatch):
    """Import routes_setup with its state-file paths redirected into tmp_path.

    Returns the module; the two JSON files do NOT exist until a test writes
    them, which is the "fresh box" starting condition.
    """
    if not _can_import():
        pytest.skip("deps unavailable")

    from orchestrator import routes_setup as mod

    monkeypatch.setattr(mod, "_SETUP_STATE_PATH", str(tmp_path / "setup_state.json"), raising=True)
    monkeypatch.setattr(mod, "_HARDWARE_SCAN_PATH", str(tmp_path / "hardware_scan.json"), raising=True)
    return mod


# ---------------------------------------------------------------------------
# _read_json
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestReadJson:
    def test_missing_file_returns_none(self, routes_setup, tmp_path):
        """A path that does not exist → None (not an exception)."""
        assert routes_setup._read_json(str(tmp_path / "nope.json")) is None

    def test_valid_object_returns_dict(self, routes_setup, tmp_path):
        """A JSON object on disk round-trips to an equal dict."""
        p = tmp_path / "obj.json"
        p.write_text(json.dumps({"a": 1, "b": "two"}))
        assert routes_setup._read_json(str(p)) == {"a": 1, "b": "two"}

    def test_malformed_json_returns_none(self, routes_setup, tmp_path):
        """Unparseable JSON → None (degrades, does not raise)."""
        p = tmp_path / "bad.json"
        p.write_text("{not valid json,,,")
        assert routes_setup._read_json(str(p)) is None

    def test_json_array_returns_none(self, routes_setup, tmp_path):
        """A top-level JSON array is valid JSON but not an object → None."""
        p = tmp_path / "arr.json"
        p.write_text(json.dumps([1, 2, 3]))
        assert routes_setup._read_json(str(p)) is None

    def test_json_scalar_returns_none(self, routes_setup, tmp_path):
        """A top-level JSON scalar is valid JSON but not an object → None."""
        p = tmp_path / "scalar.json"
        p.write_text(json.dumps("just a string"))
        assert routes_setup._read_json(str(p)) is None


# ---------------------------------------------------------------------------
# _atomic_write_json round-trip
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestAtomicWriteJson:
    def test_write_then_read_round_trip(self, routes_setup, tmp_path):
        """Data written via _atomic_write_json reads back equal via _read_json."""
        p = tmp_path / "rt.json"
        payload = {"setup_completed": True, "completed_at": "2026-05-16T00:00:00+00:00"}
        routes_setup._atomic_write_json(str(p), payload)
        assert p.exists()
        assert routes_setup._read_json(str(p)) == payload

    def test_write_creates_parent_dirs(self, routes_setup, tmp_path):
        """Missing parent directories are created on write."""
        p = tmp_path / "nested" / "deeper" / "rt.json"
        routes_setup._atomic_write_json(str(p), {"x": 1})
        assert routes_setup._read_json(str(p)) == {"x": 1}

    def test_write_leaves_no_tmp_files(self, routes_setup, tmp_path):
        """A successful atomic write leaves no `.tmp` siblings behind."""
        p = tmp_path / "clean.json"
        routes_setup._atomic_write_json(str(p), {"x": 1})
        leftovers = [f.name for f in tmp_path.iterdir() if f.name.endswith(".tmp")]
        assert leftovers == []


# ---------------------------------------------------------------------------
# is_first_boot
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestIsFirstBoot:
    def test_no_state_file_is_first_boot(self, routes_setup):
        """No setup_state.json on a fresh box → first boot."""
        assert routes_setup.is_first_boot() is True

    def test_completed_state_is_not_first_boot(self, routes_setup):
        """setup_completed: true → not first boot."""
        routes_setup._atomic_write_json(
            routes_setup._SETUP_STATE_PATH,
            {"setup_completed": True, "completed_at": "2026-05-16T00:00:00+00:00"},
        )
        assert routes_setup.is_first_boot() is False

    def test_incomplete_state_is_first_boot(self, routes_setup):
        """A state file present but setup_completed falsy → still first boot."""
        routes_setup._atomic_write_json(routes_setup._SETUP_STATE_PATH, {"setup_completed": False})
        assert routes_setup.is_first_boot() is True

    def test_corrupt_state_degrades_to_first_boot(self, routes_setup):
        """A corrupt setup_state.json degrades safely to first boot."""
        with open(routes_setup._SETUP_STATE_PATH, "w") as f:
            f.write("{garbage")
        assert routes_setup.is_first_boot() is True


# ---------------------------------------------------------------------------
# get_setup_status
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestGetSetupStatus:
    @pytest.mark.asyncio
    async def test_fresh_box_not_completed(self, routes_setup):
        """Fresh box → ok envelope, setup_completed false, completed_at null."""
        resp = await routes_setup.get_setup_status()
        body = _body(resp)
        assert body["ok"] is True
        assert body["setup_completed"] is False
        assert body["completed_at"] is None

    @pytest.mark.asyncio
    async def test_completed_after_post(self, routes_setup):
        """After post_setup_complete, status reports completed with a timestamp."""
        await routes_setup.post_setup_complete()
        resp = await routes_setup.get_setup_status()
        body = _body(resp)
        assert body["ok"] is True
        assert body["setup_completed"] is True
        assert body["completed_at"] is not None


# ---------------------------------------------------------------------------
# get_hardware_scan
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestGetHardwareScan:
    @pytest.mark.asyncio
    async def test_no_scan_file_available_false(self, routes_setup):
        """No hardware_scan.json → ok envelope, available false, hint present."""
        resp = await routes_setup.get_hardware_scan()
        body = _body(resp)
        assert body["ok"] is True
        assert body["available"] is False
        assert "hint" in body
        assert "scan" not in body

    @pytest.mark.asyncio
    async def test_scan_file_present_returns_scan(self, routes_setup):
        """A hardware_scan.json present → available true with the scan payload."""
        scan = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "cpu_count": 16}
        routes_setup._atomic_write_json(routes_setup._HARDWARE_SCAN_PATH, scan)
        resp = await routes_setup.get_hardware_scan()
        body = _body(resp)
        assert body["ok"] is True
        assert body["available"] is True
        assert body["scan"] == scan

    @pytest.mark.asyncio
    async def test_corrupt_scan_file_available_false(self, routes_setup):
        """A corrupt hardware_scan.json degrades to available false."""
        with open(routes_setup._HARDWARE_SCAN_PATH, "w") as f:
            f.write("{not json")
        resp = await routes_setup.get_hardware_scan()
        body = _body(resp)
        assert body["ok"] is True
        assert body["available"] is False


# ---------------------------------------------------------------------------
# post_setup_complete
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestPostSetupComplete:
    @pytest.mark.asyncio
    async def test_marks_completed_and_persists(self, routes_setup):
        """First POST marks completed, persists the file, returns ok envelope."""
        resp = await routes_setup.post_setup_complete()
        body = _body(resp)
        assert body["ok"] is True
        assert body["setup_completed"] is True
        assert body["completed_at"] is not None

        # Persisted to disk.
        on_disk = routes_setup._read_json(routes_setup._SETUP_STATE_PATH)
        assert on_disk == {
            "setup_completed": True,
            "completed_at": body["completed_at"],
        }

    @pytest.mark.asyncio
    async def test_idempotent_preserves_completed_at(self, routes_setup):
        """A second POST must keep the original completed_at, not clobber it."""
        first = _body(await routes_setup.post_setup_complete())
        original_ts = first["completed_at"]

        second = _body(await routes_setup.post_setup_complete())

        assert second["setup_completed"] is True
        assert second["completed_at"] == original_ts

        # And the persisted file still carries the original timestamp.
        on_disk = routes_setup._read_json(routes_setup._SETUP_STATE_PATH)
        assert on_disk["completed_at"] == original_ts

    @pytest.mark.asyncio
    async def test_completed_at_regenerated_if_flag_set_without_timestamp(self, routes_setup):
        """A half-written state (completed flag, no timestamp) self-heals."""
        routes_setup._atomic_write_json(routes_setup._SETUP_STATE_PATH, {"setup_completed": True})
        body = _body(await routes_setup.post_setup_complete())
        assert body["setup_completed"] is True
        assert body["completed_at"] is not None
