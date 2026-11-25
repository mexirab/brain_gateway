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

from orchestrator import announcement_routes as ar
from orchestrator import shared
from orchestrator.announcement_routes import (
    CATEGORIES,
    _validate,
    _validate_speaker_string,
    discover_ha_speakers,
    load_routes,
    route_for,
    save_routes,
)

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
    monkeypatch.setattr(ar, "ROUTES_PATH", str(yaml_path))
    # Reset the cache between tests
    monkeypatch.setattr(ar, "_cache", None)
    yield yaml_path


@pytest.fixture
def stub_legacy_env(monkeypatch):
    """Stub the shared/reminder_manager fallback constants so test results
    are deterministic regardless of host env.

    `_legacy_fallback` reads these via ``from orchestrator import shared`` /
    ``from orchestrator.reminder_manager import REMINDER_SPEAKER``, which bind
    to the already-imported real modules' attributes. Patch those attributes
    directly — swapping the whole module in ``sys.modules`` does NOT take
    effect once the real package attribute exists (it leaks the real
    MORNING_BRIEFING_SPEAKER, e.g. ``media_player.bedroom_pair``).
    """
    monkeypatch.setattr("orchestrator.shared.MORNING_BRIEFING_SPEAKER", "media_player.briefing_default", raising=False)
    monkeypatch.setattr("orchestrator.shared.FOCUS_AUDIO_PLAYER", "media_player.focus_default", raising=False)
    monkeypatch.setattr("orchestrator.reminder_manager.REMINDER_SPEAKER", "media_player.generic_default", raising=False)
    return {
        "reminder": "media_player.generic_default",
        "briefing": "media_player.briefing_default",
        "focus": "media_player.focus_default",
    }


# ---------------------------------------------------------------------------
# _validate_speaker_string
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        pytest.param("media_player.office_max", "media_player.office_max", id="single"),
        pytest.param(
            "media_player.a,media_player.b , media_player.c",
            "media_player.a,media_player.b,media_player.c",
            id="comma-list",
        ),
        pytest.param("", "", id="empty"),
        pytest.param(None, "", id="none"),
    ],
)
def test_validate_speaker_accepts(raw, expected):
    assert _validate_speaker_string(raw, "x") == expected


@pytest.mark.parametrize(
    "raw, match",
    [
        pytest.param(42, "must be a string", id="non-string"),
        pytest.param("office_max", "must look like", id="missing-dot"),
        # The strict entity regex (2026-10-02) replaced a separate whitespace check.
        pytest.param("media player.office", "must look like", id="embedded-whitespace"),
    ],
)
def test_validate_speaker_rejects(raw, match):
    with pytest.raises(ValueError, match=match):
        _validate_speaker_string(raw, "routes.x")


# ---------------------------------------------------------------------------
# _validate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload, match",
    [
        pytest.param([], "must be an object", id="non-dict"),
        pytest.param({"routes": []}, "routes must be an object", id="routes-not-dict"),
        pytest.param({"routes": {"": "media_player.x"}}, "category id", id="blank-category"),
        pytest.param({"routes": {"selfcare": "no-dot-here"}}, "routes.selfcare", id="bad-speaker-named"),
    ],
)
def test_validate_rejects(payload, match):
    with pytest.raises(ValueError, match=match):
        _validate(payload)


# ---------------------------------------------------------------------------
# load_routes / save_routes round-trip
# ---------------------------------------------------------------------------


def test_load_routes_returns_defaults_when_file_missing(stub_legacy_env):
    data = load_routes()
    assert set(data["routes"].keys()) == set(CATEGORIES)
    assert data["routes"]["briefing"] == stub_legacy_env["briefing"]
    assert data["routes"]["focus"] == stub_legacy_env["focus"]
    assert data["routes"]["reminder"] == stub_legacy_env["reminder"]


def test_save_routes_round_trip(_isolate_announcement_routes, stub_legacy_env):
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
    # First set a custom value
    save_routes({"routes": {"selfcare": "media_player.office_max"}})
    # Then clear it
    saved = save_routes({"routes": {"selfcare": ""}})
    # selfcare should now show the legacy fallback, not "" or the previous custom value
    assert saved["routes"]["selfcare"] == stub_legacy_env["reminder"]


def test_load_routes_returns_defaults_on_corrupt_yaml(_isolate_announcement_routes, stub_legacy_env):
    _isolate_announcement_routes.write_text("not: [valid: yaml")
    data = load_routes()
    # Should fall back to defaults, not raise
    assert data["routes"]["briefing"] == stub_legacy_env["briefing"]


def test_save_routes_invalidates_cache(_isolate_announcement_routes, stub_legacy_env):
    # Prime the cache
    ar.load_routes()
    assert ar._cache is not None

    ar.save_routes({"routes": {"reminder": "media_player.new_target"}})

    # Cache should have been refreshed via reload_routes()
    assert ar._cache["routes"]["reminder"] == "media_player.new_target"


# ---------------------------------------------------------------------------
# route_for
# ---------------------------------------------------------------------------


_REMINDER = {"reminder": "media_player.reminder_target"}


@pytest.mark.parametrize(
    "saved, category, expected",
    [
        pytest.param({"selfcare": "media_player.office_max"}, "selfcare", "media_player.office_max", id="per-category"),
        # A category the panel doesn't know: `default` wins when set...
        pytest.param(
            {"selfcare": "", "default": "media_player.fallback_a"},
            "unknown_category",
            "media_player.fallback_a",
            id="unknown-uses-default",
        ),
        # ...else `reminder`.
        pytest.param(_REMINDER, "zzz_unknown", "media_player.reminder_target", id="unknown-uses-reminder"),
        # Nothing saved: the legacy env-var speakers.
        pytest.param(None, "briefing", "media_player.briefing_default", id="legacy-briefing"),
        pytest.param(None, "focus", "media_player.focus_default", id="legacy-focus"),
        pytest.param(None, "selfcare", "media_player.generic_default", id="legacy-selfcare"),
        pytest.param(None, None, "media_player.generic_default", id="legacy-none-type"),
        pytest.param(None, "", "media_player.generic_default", id="legacy-empty-type"),
        # `temperature` was added after the panel shipped: left unset it must keep
        # following the panel's default/reminder route, not jump to the legacy speaker.
        pytest.param(_REMINDER, "temperature", "media_player.reminder_target", id="temperature-follows-reminder"),
        pytest.param(
            {"default": "media_player.default_target", **_REMINDER},
            "temperature",
            "media_player.default_target",
            id="temperature-follows-default",
        ),
        pytest.param(
            {**_REMINDER, "temperature": "media_player.closet_target"},
            "temperature",
            "media_player.closet_target",
            id="temperature-own-route",
        ),
        pytest.param(
            {**_REMINDER, "temperature": "media_player.closet_target"},
            "reminder",
            "media_player.reminder_target",
            id="temperature-route-leaves-reminder",
        ),
        pytest.param(None, "temperature", "media_player.generic_default", id="temperature-legacy"),
    ],
)
def test_route_for(stub_legacy_env, saved, category, expected):
    if saved is not None:
        save_routes({"routes": saved})
    assert route_for(category) == expected


# ---------------------------------------------------------------------------
# discover_ha_speakers
# ---------------------------------------------------------------------------
async def test_discover_returns_empty_when_ha_client_none(monkeypatch):
    """Frontend can degrade gracefully if HA isn't reachable."""
    monkeypatch.setattr(shared, "ha_client", None)
    assert await discover_ha_speakers() == []


async def test_discover_returns_sorted_friendly_names(monkeypatch):
    e1 = MagicMock(entity_id="media_player.zebra", friendly_name="Zebra room", state="off")
    e2 = MagicMock(entity_id="media_player.alpha", friendly_name="Alpha room", state="playing")
    fake_client = MagicMock()
    fake_client.refresh_entities = AsyncMock()
    fake_client.get_entities_by_domain.return_value = [e1, e2]
    monkeypatch.setattr(shared, "ha_client", fake_client)

    out = await discover_ha_speakers()
    assert [s["entity_id"] for s in out] == ["media_player.alpha", "media_player.zebra"]
    assert out[0]["friendly_name"] == "Alpha room"
    assert out[0]["state"] == "playing"


async def test_discover_swallows_exceptions(monkeypatch):
    fake_client = MagicMock()
    fake_client.refresh_entities = AsyncMock(side_effect=RuntimeError("HA down"))
    monkeypatch.setattr(shared, "ha_client", fake_client)
    assert await discover_ha_speakers() == []
