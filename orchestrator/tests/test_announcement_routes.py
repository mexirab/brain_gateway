"""
Tests for orchestrator/announcement_routes.py — Speakers panel backend.

Covers:
- _validate_speaker_string: accepts entity_id, comma-list, empty; rejects
  non-strings, missing dots, embedded whitespace.
- _validate: top-level shape, per-category type checks.
- load_routes: defaults derived from legacy env-var fallbacks; overrides win;
  empty values fall through to defaults; bad YAML returns defaults.
- save_routes: round-trip, cache invalidation.
- route_for: per-category lookup, default key fallback, reminder fallback,
  legacy env-var fallback when nothing configured.
- discover_ha_speakers: graceful empty list when ha_client is None or raises.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

# ---------------------------------------------------------------------------
# Fixtures: redirect the YAML path + reset module state per test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_announcement_routes(monkeypatch, tmp_path):
    """Each test gets a fresh tmp YAML + a clean module cache."""
    yaml_path = tmp_path / "announcement_routes.yaml"
    monkeypatch.setenv("ANNOUNCEMENT_ROUTES_PATH", str(yaml_path))

    # Force the module to re-read the env var by reassigning ROUTES_PATH at
    # import time. The module captures `os.environ.get(...)` at module-load.
    from orchestrator import announcement_routes as ar

    monkeypatch.setattr(ar, "ROUTES_PATH", str(yaml_path))
    # Reset the cache between tests
    monkeypatch.setattr(ar, "_cache", None)
    yield yaml_path


@pytest.fixture
def stub_legacy_env(monkeypatch):
    """Stub the shared/reminder_manager fallback constants so test results
    are deterministic regardless of host env."""
    import sys

    fake_shared = MagicMock()
    fake_shared.MORNING_BRIEFING_SPEAKER = "media_player.briefing_default"
    fake_shared.FOCUS_AUDIO_PLAYER = "media_player.focus_default"
    monkeypatch.setitem(sys.modules, "orchestrator.shared", fake_shared)

    fake_rm = MagicMock()
    fake_rm.REMINDER_SPEAKER = "media_player.generic_default"
    monkeypatch.setitem(sys.modules, "orchestrator.reminder_manager", fake_rm)
    return {
        "reminder": "media_player.generic_default",
        "briefing": "media_player.briefing_default",
        "focus": "media_player.focus_default",
    }


# ---------------------------------------------------------------------------
# _validate_speaker_string
# ---------------------------------------------------------------------------


def test_validate_speaker_accepts_single_entity_id():
    from orchestrator.announcement_routes import _validate_speaker_string

    assert _validate_speaker_string("media_player.office_max", "x") == "media_player.office_max"


def test_validate_speaker_accepts_comma_list():
    from orchestrator.announcement_routes import _validate_speaker_string

    out = _validate_speaker_string("media_player.a,media_player.b , media_player.c", "x")
    assert out == "media_player.a,media_player.b,media_player.c"


def test_validate_speaker_accepts_empty():
    from orchestrator.announcement_routes import _validate_speaker_string

    assert _validate_speaker_string("", "x") == ""
    assert _validate_speaker_string(None, "x") == ""


def test_validate_speaker_rejects_non_string():
    from orchestrator.announcement_routes import _validate_speaker_string

    with pytest.raises(ValueError, match="must be a string"):
        _validate_speaker_string(42, "routes.x")


def test_validate_speaker_rejects_missing_dot():
    from orchestrator.announcement_routes import _validate_speaker_string

    with pytest.raises(ValueError, match="must look like"):
        _validate_speaker_string("office_max", "x")


def test_validate_speaker_rejects_embedded_whitespace():
    from orchestrator.announcement_routes import _validate_speaker_string

    with pytest.raises(ValueError, match="contains whitespace"):
        _validate_speaker_string("media player.office", "x")


# ---------------------------------------------------------------------------
# _validate
# ---------------------------------------------------------------------------


def test_validate_rejects_non_dict():
    from orchestrator.announcement_routes import _validate

    with pytest.raises(ValueError, match="must be an object"):
        _validate([])


def test_validate_rejects_routes_not_dict():
    from orchestrator.announcement_routes import _validate

    with pytest.raises(ValueError, match="routes must be an object"):
        _validate({"routes": []})


def test_validate_rejects_blank_category_name():
    from orchestrator.announcement_routes import _validate

    with pytest.raises(ValueError, match="category id"):
        _validate({"routes": {"": "media_player.x"}})


def test_validate_strips_per_category_invalid_speaker():
    from orchestrator.announcement_routes import _validate

    with pytest.raises(ValueError, match="routes.selfcare"):
        _validate({"routes": {"selfcare": "no-dot-here"}})


# ---------------------------------------------------------------------------
# load_routes / save_routes round-trip
# ---------------------------------------------------------------------------


def test_load_routes_returns_defaults_when_file_missing(stub_legacy_env):
    from orchestrator.announcement_routes import CATEGORIES, load_routes

    data = load_routes()
    assert set(data["routes"].keys()) == set(CATEGORIES)
    assert data["routes"]["briefing"] == stub_legacy_env["briefing"]
    assert data["routes"]["focus"] == stub_legacy_env["focus"]
    assert data["routes"]["reminder"] == stub_legacy_env["reminder"]


def test_save_routes_round_trip(_isolate_announcement_routes, stub_legacy_env):
    from orchestrator.announcement_routes import load_routes, save_routes

    saved = save_routes({"routes": {"selfcare": "media_player.office_max"}})
    assert saved["routes"]["selfcare"] == "media_player.office_max"

    # Untouched categories stay at legacy fallback
    assert saved["routes"]["briefing"] == stub_legacy_env["briefing"]

    # On-disk YAML matches what was saved
    on_disk = yaml.safe_load(_isolate_announcement_routes.read_text())
    assert on_disk["routes"]["selfcare"] == "media_player.office_max"

    # And re-loading returns the same merged shape
    reloaded = load_routes()
    assert reloaded["routes"]["selfcare"] == "media_player.office_max"


def test_save_routes_empty_value_falls_back_to_legacy(_isolate_announcement_routes, stub_legacy_env):
    """The killer edge case: user clears a field. Backend should fall back
    to the legacy default, not silence the category with an empty string."""
    from orchestrator.announcement_routes import save_routes

    # First set a custom value
    save_routes({"routes": {"selfcare": "media_player.office_max"}})
    # Then clear it
    saved = save_routes({"routes": {"selfcare": ""}})
    # selfcare should now show the legacy fallback, not "" or the previous custom value
    assert saved["routes"]["selfcare"] == stub_legacy_env["reminder"]


def test_load_routes_returns_defaults_on_corrupt_yaml(_isolate_announcement_routes, stub_legacy_env):
    from orchestrator.announcement_routes import load_routes

    _isolate_announcement_routes.write_text("not: [valid: yaml")
    data = load_routes()
    # Should fall back to defaults, not raise
    assert data["routes"]["briefing"] == stub_legacy_env["briefing"]


def test_save_routes_invalidates_cache(_isolate_announcement_routes, stub_legacy_env):
    from orchestrator import announcement_routes as ar

    # Prime the cache
    ar.load_routes()
    assert ar._cache is not None

    ar.save_routes({"routes": {"reminder": "media_player.new_target"}})

    # Cache should have been refreshed via reload_routes()
    assert ar._cache["routes"]["reminder"] == "media_player.new_target"


# ---------------------------------------------------------------------------
# route_for
# ---------------------------------------------------------------------------


def test_route_for_returns_per_category_value(_isolate_announcement_routes, stub_legacy_env):
    from orchestrator.announcement_routes import route_for, save_routes

    save_routes({"routes": {"selfcare": "media_player.office_max"}})
    assert route_for("selfcare") == "media_player.office_max"


def test_route_for_falls_back_to_default_key(_isolate_announcement_routes, stub_legacy_env):
    """When a category has no entry but a `default` is configured, default wins."""
    from orchestrator.announcement_routes import route_for, save_routes

    # selfcare gets cleared, default gets set
    save_routes({"routes": {"selfcare": "", "default": "media_player.fallback_a"}})
    # An announcement_type the categories list doesn't know about should hit `default`
    assert route_for("unknown_category") == "media_player.fallback_a"


def test_route_for_falls_back_to_reminder_when_default_missing(_isolate_announcement_routes, stub_legacy_env):
    from orchestrator.announcement_routes import route_for, save_routes

    save_routes({"routes": {"reminder": "media_player.reminder_target"}})
    # An unknown category with no `default` falls through to `reminder`
    assert route_for("zzz_unknown") == "media_player.reminder_target"


def test_route_for_falls_back_to_legacy_when_nothing_configured(stub_legacy_env):
    from orchestrator.announcement_routes import route_for

    # No save calls — everything is at legacy fallbacks
    assert route_for("briefing") == stub_legacy_env["briefing"]
    assert route_for("focus") == stub_legacy_env["focus"]
    assert route_for("selfcare") == stub_legacy_env["reminder"]


def test_route_for_handles_none_announcement_type(stub_legacy_env):
    from orchestrator.announcement_routes import route_for

    # None / "" should still return a usable speaker (legacy reminder fallback)
    assert route_for(None) == stub_legacy_env["reminder"]
    assert route_for("") == stub_legacy_env["reminder"]


# ---------------------------------------------------------------------------
# discover_ha_speakers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_returns_empty_when_ha_client_none(monkeypatch):
    """Frontend can degrade gracefully if HA isn't reachable."""
    import sys

    fake_shared = MagicMock()
    fake_shared.ha_client = None
    monkeypatch.setitem(sys.modules, "orchestrator.shared", fake_shared)

    from orchestrator.announcement_routes import discover_ha_speakers

    assert await discover_ha_speakers() == []


@pytest.mark.asyncio
async def test_discover_returns_sorted_friendly_names(monkeypatch):
    import sys

    e1 = MagicMock(entity_id="media_player.zebra", friendly_name="Zebra room", state="off")
    e2 = MagicMock(entity_id="media_player.alpha", friendly_name="Alpha room", state="playing")

    fake_client = MagicMock()
    fake_client.refresh_entities = AsyncMock()
    fake_client.get_entities_by_domain.return_value = [e1, e2]

    fake_shared = MagicMock()
    fake_shared.ha_client = fake_client
    monkeypatch.setitem(sys.modules, "orchestrator.shared", fake_shared)

    from orchestrator.announcement_routes import discover_ha_speakers

    out = await discover_ha_speakers()
    assert [s["entity_id"] for s in out] == ["media_player.alpha", "media_player.zebra"]
    assert out[0]["friendly_name"] == "Alpha room"
    assert out[0]["state"] == "playing"


@pytest.mark.asyncio
async def test_discover_swallows_exceptions(monkeypatch):
    import sys

    fake_client = MagicMock()
    fake_client.refresh_entities = AsyncMock(side_effect=RuntimeError("HA down"))

    fake_shared = MagicMock()
    fake_shared.ha_client = fake_client
    monkeypatch.setitem(sys.modules, "orchestrator.shared", fake_shared)

    from orchestrator.announcement_routes import discover_ha_speakers

    # Swallows exception, returns empty list
    assert await discover_ha_speakers() == []
