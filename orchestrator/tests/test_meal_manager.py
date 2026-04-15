"""
Tests for meal_manager.py.

Vision calls (analyze_image) are mocked throughout — no real Qwen2.5-VL hits.
MEAL_PHOTOS_DIR is redirected to a tmp directory via monkeypatch.
"""

import os
import tempfile
import unittest.mock as mock

import pytest


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def meal_photos_dir(tmp_path, monkeypatch):
    """Point MEAL_PHOTOS_DIR at a fresh temp dir and reload meal_manager's module constant."""
    photos = tmp_path / "meal_photos"
    photos.mkdir()
    monkeypatch.setattr("orchestrator.meal_manager.MEAL_PHOTOS_DIR", str(photos))
    return str(photos)


# ---------------------------------------------------------------------------
# _is_under_photos_dir
# ---------------------------------------------------------------------------


def test_is_under_photos_dir_accepts_valid(meal_photos_dir):
    import orchestrator.meal_manager as mm

    # A file path directly under the dir should pass
    safe = os.path.join(meal_photos_dir, "abc123.jpg")
    assert mm._is_under_photos_dir(safe) is True


def test_is_under_photos_dir_rejects_etc_passwd(meal_photos_dir):
    import orchestrator.meal_manager as mm

    assert mm._is_under_photos_dir("/etc/passwd") is False


def test_is_under_photos_dir_rejects_parent_traversal(meal_photos_dir, tmp_path):
    import orchestrator.meal_manager as mm

    # Path that resolves to a sibling dir, not under photos
    sibling = str(tmp_path / "sibling" / "evil.jpg")
    assert mm._is_under_photos_dir(sibling) is False


def test_is_under_photos_dir_rejects_empty(meal_photos_dir):
    import orchestrator.meal_manager as mm

    assert mm._is_under_photos_dir("") is False


# ---------------------------------------------------------------------------
# save_photo_bytes — extension allowlist enforcement
# ---------------------------------------------------------------------------


def test_save_photo_bytes_php_becomes_jpg(meal_photos_dir):
    import orchestrator.meal_manager as mm

    path = mm.save_photo_bytes(b"data", "php")
    assert path.endswith(".jpg")
    assert not path.endswith(".php")
    assert os.path.exists(path)


def test_save_photo_bytes_sh_becomes_jpg(meal_photos_dir):
    import orchestrator.meal_manager as mm

    path = mm.save_photo_bytes(b"data", "sh")
    assert path.endswith(".jpg")


def test_save_photo_bytes_html_becomes_jpg(meal_photos_dir):
    import orchestrator.meal_manager as mm

    path = mm.save_photo_bytes(b"data", "html")
    assert path.endswith(".jpg")


def test_save_photo_bytes_svg_becomes_jpg(meal_photos_dir):
    import orchestrator.meal_manager as mm

    path = mm.save_photo_bytes(b"data", "svg")
    assert path.endswith(".jpg")


def test_save_photo_bytes_png_stays_png(meal_photos_dir):
    import orchestrator.meal_manager as mm

    path = mm.save_photo_bytes(b"data", "png")
    assert path.endswith(".png")


def test_save_photo_bytes_webp_stays_webp(meal_photos_dir):
    import orchestrator.meal_manager as mm

    path = mm.save_photo_bytes(b"data", "webp")
    assert path.endswith(".webp")


# ---------------------------------------------------------------------------
# log_meal — photo_path safety
# ---------------------------------------------------------------------------


def test_log_meal_rejects_etc_passwd_photo_path(tmp_db, meal_photos_dir):
    """Supplying photo_path=/etc/passwd must result in photo_path=None in the stored row."""
    import orchestrator.meal_manager as mm

    result = mm.log_meal(
        description="Salad",
        calories=300,
        meal_type="lunch",
        photo_path="/etc/passwd",
    )
    assert result["ok"] is True
    assert result["meal"]["photo_path"] is None


def test_log_meal_accepts_valid_photo_path(tmp_db, meal_photos_dir):
    import orchestrator.meal_manager as mm

    # Create a real file under the photos dir so _is_under_photos_dir resolves correctly
    safe_path = os.path.join(meal_photos_dir, "test.jpg")
    with open(safe_path, "wb") as f:
        f.write(b"fake-image")

    result = mm.log_meal(
        description="Burger",
        calories=700,
        meal_type="dinner",
        photo_path=safe_path,
    )
    assert result["ok"] is True
    assert result["meal"]["photo_path"] == safe_path


def test_log_meal_requires_description(tmp_db, meal_photos_dir):
    import orchestrator.meal_manager as mm

    result = mm.log_meal(description="", calories=100)
    assert result["ok"] is False
    assert "Description" in result["error"]


def test_log_meal_clamps_invalid_calories(tmp_db, meal_photos_dir):
    import orchestrator.meal_manager as mm

    # Negative calories should be stored as None
    result = mm.log_meal(description="Mystery food", calories=-500)
    assert result["ok"] is True
    assert result["meal"]["calories"] is None


def test_log_meal_bridges_to_selfcare_nudge_gate(tmp_db, meal_photos_dir):
    """Regression: logging a meal via meal_manager.log_meal must advance the
    selfcare_manager nudge gate. Otherwise the "no meals logged today" nudge
    fires every scheduler tick past 12pm even after the user logged lunch.
    """
    import orchestrator.meal_manager as mm
    from orchestrator import selfcare_manager

    # Reset the in-memory state so we can detect the advance
    selfcare_manager._state.last_meal_reported = None

    result = mm.log_meal(description="Turkey sandwich", calories=500, meal_type="lunch")

    assert result["ok"] is True
    assert selfcare_manager._state.last_meal_reported is not None, (
        "log_meal did not advance selfcare_manager._state.last_meal_reported — "
        "the 'no meals logged today' nudge will keep firing"
    )


def test_log_meal_bridge_failure_does_not_break_meal_logging(tmp_db, meal_photos_dir, monkeypatch):
    """If the selfcare bridge raises, the meal must still be persisted. The
    bridge is best-effort — losing a nudge advance is worse than losing the
    meal row the user explicitly asked us to save.
    """
    import orchestrator.meal_manager as mm
    from orchestrator import selfcare_manager

    def _boom(*args, **kwargs):
        raise RuntimeError("selfcare exploded")

    monkeypatch.setattr(selfcare_manager, "record_meal_logged", _boom)

    result = mm.log_meal(description="Resilient oats", calories=300)
    assert result["ok"] is True
    assert result["meal"]["description"] == "Resilient oats"


# ---------------------------------------------------------------------------
# delete_meal — does NOT call os.remove for unsafe photo_path
# ---------------------------------------------------------------------------


def test_delete_meal_skips_remove_for_unsafe_path(tmp_db, meal_photos_dir, monkeypatch):
    """
    If a stored row has photo_path outside MEAL_PHOTOS_DIR (e.g. a legacy poison
    row), delete_meal must NOT call os.remove on it.
    """
    from orchestrator import state_store
    import orchestrator.meal_manager as mm

    # Bypass log_meal sanitization — insert directly via state_store
    meal = state_store.add_meal(
        description="Poisoned row",
        meal_type="snack",
        calories=100,
        photo_path="/etc/shadow",  # inserted directly, bypassing log_meal
    )

    remove_mock = mock.MagicMock()
    monkeypatch.setattr("orchestrator.meal_manager.os.remove", remove_mock)

    result = mm.delete_meal(meal["id"])
    assert result["ok"] is True
    remove_mock.assert_not_called()


def test_delete_meal_removes_photo_under_photos_dir(tmp_db, meal_photos_dir, monkeypatch):
    """delete_meal SHOULD call os.remove when photo is inside MEAL_PHOTOS_DIR."""
    from orchestrator import state_store
    import orchestrator.meal_manager as mm

    safe_path = os.path.join(meal_photos_dir, "real.jpg")
    with open(safe_path, "wb") as f:
        f.write(b"img")

    meal = state_store.add_meal(
        description="Pasta",
        meal_type="dinner",
        calories=800,
        photo_path=safe_path,
    )

    result = mm.delete_meal(meal["id"])
    assert result["ok"] is True
    # File should have been removed
    assert not os.path.exists(safe_path)


def test_delete_meal_not_found(tmp_db, meal_photos_dir):
    import orchestrator.meal_manager as mm

    result = mm.delete_meal(99999)
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# get_today / get_history
# ---------------------------------------------------------------------------


def test_get_today_empty(tmp_db, meal_photos_dir):
    import orchestrator.meal_manager as mm

    result = mm.get_today()
    assert result["meal_count"] == 0
    assert result["total_calories"] == 0


def test_get_today_sums_calories(tmp_db, meal_photos_dir):
    import orchestrator.meal_manager as mm

    mm.log_meal("Oats", calories=350, meal_type="breakfast")
    mm.log_meal("Salad", calories=450, meal_type="lunch")

    result = mm.get_today()
    assert result["meal_count"] == 2
    assert result["total_calories"] == 800
