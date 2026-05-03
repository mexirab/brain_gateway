"""
Announcement-routing config — which speaker(s) each announcement category
plays on.

Replaces the catch-all `REMINDER_SPEAKER` env var as the single fallback for
selfcare / reminders / calendar / ambient / progress / focus / briefing.
Each category gets its own line; `_announce_voice(speaker=None,
announcement_type=...)` consults this map.

Storage: writable shadow at `/app/data/announcement_routes.yaml`. When the
file is missing or a category has no entry, falls back to the legacy
env-var defaults (REMINDER_SPEAKER for most categories, MORNING_BRIEFING_SPEAKER
for `briefing`, FOCUS_AUDIO_PLAYER for `focus`). Same posture as
`selfcare_schedule.py` and `routines_config.py`.

Schema:

```yaml
routes:
  selfcare: "media_player.office_max"
  reminder: "media_player.office_max,media_player.bedroom_pair"   # multi-room
  calendar: "media_player.office_max"
  ambient:  "media_player.office_max"
  progress: "media_player.office_max"
  focus:    "media_player.office_max"
  briefing: "media_player.bedroom_pair"
```

Values are strings — single entity_id, comma-separated list, or empty
(empty = use the legacy env-var fallback).
"""

from __future__ import annotations

import logging
import os
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

ROUTES_PATH = os.environ.get("ANNOUNCEMENT_ROUTES_PATH", "/app/data/announcement_routes.yaml")

# Categories the panel exposes. Other announcement_type values that hit
# `_announce_voice` are treated as "use the configured `default` route", or
# the `reminder` route if no `default` is set, or finally the legacy
# REMINDER_SPEAKER fallback.
CATEGORIES = (
    "selfcare",
    "reminder",
    "calendar",
    "ambient",
    "progress",
    "focus",
    "briefing",
)

_lock = threading.Lock()
_cache: Optional[Dict[str, Any]] = None


def _legacy_fallback(category: str) -> str:
    """Reproduce the pre-Speakers-panel default for each category so users
    upgrading without configuring anything get identical behavior."""
    try:
        from orchestrator import shared
    except Exception:
        return ""
    if category == "briefing":
        return getattr(shared, "MORNING_BRIEFING_SPEAKER", "") or ""
    if category == "focus":
        return getattr(shared, "FOCUS_AUDIO_PLAYER", "") or ""
    # Most categories historically fell through to REMINDER_SPEAKER, which
    # is read from env at module-import time in reminder_manager.py.
    try:
        from orchestrator.reminder_manager import REMINDER_SPEAKER

        return REMINDER_SPEAKER or ""
    except Exception:
        return ""


def _build_defaults() -> Dict[str, Any]:
    """Build the default route map from current legacy fallbacks so an
    on-disk file written from the panel mirrors today's behavior."""
    return {"routes": {cat: _legacy_fallback(cat) for cat in CATEGORIES}}


def _validate_speaker_string(value: Any, field: str) -> str:
    """Empty string is allowed (means: use the legacy fallback)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string (entity_id or comma-list), got {type(value).__name__}")
    parts = [p.strip() for p in value.split(",")]
    cleaned = [p for p in parts if p]
    for p in cleaned:
        # Light validation — full HA entity-id regex is `^[a-z0-9_]+\.[a-z0-9_]+$`
        # but we don't want to reject valid quirks like double-underscore. Just
        # gate the obvious garbage.
        if "." not in p:
            raise ValueError(f"{field} entry {p!r} must look like 'media_player.<name>'")
        if any(c.isspace() for c in p):
            raise ValueError(f"{field} entry {p!r} contains whitespace")
    return ",".join(cleaned)


def _validate(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("announcement_routes payload must be an object")
    routes = data.get("routes")
    if not isinstance(routes, dict):
        raise ValueError("announcement_routes.routes must be an object")
    cleaned: Dict[str, Any] = {}
    for cat, value in routes.items():
        if not isinstance(cat, str) or not cat.strip():
            raise ValueError("category id must be a non-empty string")
        cleaned[cat] = _validate_speaker_string(value, f"routes.{cat}")
    return {"routes": cleaned}


def load_routes_raw() -> Dict[str, Any]:
    """Return the raw on-disk route map (or empty dict if file missing).
    Empty-string values are preserved — these mean "use legacy fallback"
    but the panel needs to know the user explicitly cleared the field
    versus never set it."""
    path = Path(ROUTES_PATH)
    if not path.exists():
        return {"routes": {}}
    try:
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        return _validate(raw)
    except (yaml.YAMLError, ValueError) as e:
        logger.error(f"[ANNOUNCEMENT_ROUTES] Bad YAML at {path}: {e}; returning empty")
        return {"routes": {}}
    except OSError as e:
        logger.error(f"[ANNOUNCEMENT_ROUTES] Read failed for {path}: {e}; returning empty")
        return {"routes": {}}


def load_routes() -> Dict[str, Any]:
    """Return the EFFECTIVE route map: defaults overlaid with any non-empty
    on-disk overrides. Empty string in the YAML means "I cleared this
    field" — falls back to the legacy default rather than silencing the
    category entirely. This is the shape `route_for()` uses for dispatch.

    Use `load_routes_raw()` for the panel to render unmodified user values
    + a separate `effective` view.
    """
    global _cache
    with _lock:
        if _cache is not None:
            return deepcopy(_cache)

        defaults = _build_defaults()
        raw_data = load_routes_raw()
        merged = deepcopy(defaults)
        for cat, value in (raw_data.get("routes") or {}).items():
            if value:
                merged["routes"][cat] = value
        _cache = merged
        return deepcopy(_cache)


def panel_view() -> Dict[str, Any]:
    """The view the Speakers panel renders. Returns:
    {
        "routes": {<cat>: <user-typed value or "">, ...},   # raw, what to show in inputs
        "effective": {<cat>: <post-fallback value>, ...},   # what would be used if saved as-is
        "categories": [...]                                 # ordered category list incl. `default`
    }
    """
    raw = load_routes_raw().get("routes") or {}
    effective = load_routes().get("routes") or {}
    # Ensure every known category appears in `routes` (with "" if absent on
    # disk) so the panel renders a row for every category instead of
    # missing rows for never-set categories.
    routes_out: Dict[str, str] = {cat: raw.get(cat, "") for cat in CATEGORIES}
    # Plus any user-set extras (currently only `default`)
    for cat, value in raw.items():
        if cat not in routes_out:
            routes_out[cat] = value
    return {
        "routes": routes_out,
        "effective": effective,
        "categories": [*CATEGORIES, "default"],
    }


def save_routes(data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate + atomic write + invalidate cache. Returns the merged map."""
    from orchestrator.config_writer import atomic_write_yaml

    validated = _validate(data)
    atomic_write_yaml(ROUTES_PATH, validated)
    return reload_routes()


def reload_routes() -> Dict[str, Any]:
    global _cache
    with _lock:
        _cache = None
    return load_routes()


# ---------------------------------------------------------------------------
# Lookup used by _announce_voice
# ---------------------------------------------------------------------------


def route_for(announcement_type: Optional[str]) -> str:
    """Return the speaker string for a given announcement_type.

    Resolution order:
        1. Configured route for the exact category (if non-empty)
        2. Configured route for `default` (if present and non-empty)
        3. Configured route for `reminder` (if non-empty)
        4. Legacy env-var fallback for the original category

    All paths return a string — possibly empty. Callers handle empty-string
    by raising the existing "No speakers configured" error.
    """
    routes = load_routes().get("routes") or {}
    cat = (announcement_type or "").lower()

    if cat and routes.get(cat):
        return str(routes[cat])
    if routes.get("default"):
        return str(routes["default"])
    if routes.get("reminder"):
        return str(routes["reminder"])
    return _legacy_fallback(cat or "reminder")


# ---------------------------------------------------------------------------
# HA discovery — used by the Speakers panel autocomplete
# ---------------------------------------------------------------------------


async def discover_ha_speakers() -> List[Dict[str, str]]:
    """Return all `media_player.*` entities from HA so the panel can render
    an autocomplete datalist. Each entry: {entity_id, friendly_name, state}.

    Failures return an empty list — the panel still works as a free-text input.
    """
    try:
        from orchestrator.shared import ha_client

        if ha_client is None:
            return []
        await ha_client.refresh_entities()
        entities = ha_client.get_entities_by_domain("media_player")
        out: List[Dict[str, str]] = []
        for e in entities:
            out.append(
                {
                    "entity_id": e.entity_id,
                    "friendly_name": getattr(e, "friendly_name", e.entity_id) or e.entity_id,
                    "state": getattr(e, "state", "") or "",
                }
            )
        out.sort(key=lambda x: x["friendly_name"].lower())
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[ANNOUNCEMENT_ROUTES] HA speaker discovery failed: {e}")
        return []
