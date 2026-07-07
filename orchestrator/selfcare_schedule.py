"""
Selfcare schedule + quiet hours runtime config.

Replaces the env-var-driven `shared.MEAL_NUDGE_HOURS / HYDRATION_INTERVAL /
MOVEMENT_INTERVAL / QUIET_HOURS_*` constants with a YAML file the user can
edit through the `/settings` page. Loader always returns a dict — if the
file is missing/corrupt, fall back to defaults built from `shared.*` so
the behavior matches the pre-settings-page baseline.

Schema (see also docs/ENV_VARS.md once we land docs):

```yaml
categories:
  water:
    enabled: true
    interval_minutes: 90
    active_hours: {start: "09:00", end: "21:00"}
    message_template: "Water check. Take a few sips."
  meds:
    enabled: true
    times: ["07:00", "20:00"]   # fixed times, not interval
  meals:
    enabled: true
    interval_hours: 4
    active_hours: {start: "09:00", end: "21:00"}
  movement:
    enabled: true
    interval_minutes: 90
    active_hours: {start: "09:00", end: "18:00"}

quiet_hours:
  start: "22:00"
  end: "07:00"
  days: [mon, tue, wed, thu, fri, sat, sun]
```
"""

from __future__ import annotations

import logging
import os
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger(__name__)

SCHEDULE_PATH = os.environ.get("SELFCARE_SCHEDULE_PATH", "/app/data/selfcare_schedule.yaml")

VALID_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}

_lock = threading.Lock()
_cache: Optional[Dict[str, Any]] = None


def _build_defaults() -> Dict[str, Any]:
    """Build the default schedule dict, sourcing intervals from `shared` so
    pre-settings-page defaults stay aligned with the current env-var values.
    """
    try:
        from orchestrator import shared

        meal_hours = int(getattr(shared, "MEAL_NUDGE_HOURS", 4))
        hydration = int(getattr(shared, "HYDRATION_INTERVAL", 90))
        movement = int(getattr(shared, "MOVEMENT_INTERVAL", 90))
        quiet_start = str(getattr(shared, "QUIET_HOURS_START", "22:00"))
        quiet_end = str(getattr(shared, "QUIET_HOURS_END", "07:00"))
    except Exception:
        meal_hours, hydration, movement = 4, 90, 90
        quiet_start, quiet_end = "22:00", "07:00"

    return {
        "categories": {
            "water": {
                "enabled": True,
                "interval_minutes": hydration,
                "active_hours": {"start": "09:00", "end": "21:00"},
                "message_template": "Water check. Take a few sips.",
            },
            "meds": {
                "enabled": True,
                # 07:00 preserves the old hardcoded morning-window start so a
                # ~07:45 dose keeps its nudge; users set their real times via
                # Settings → Selfcare. Evening 20:00 = old default.
                "times": ["07:00", "20:00"],
                "message_template": "Time for meds.",
            },
            "meals": {
                "enabled": True,
                "interval_hours": meal_hours,
                "active_hours": {"start": "09:00", "end": "21:00"},
                "message_template": "",
            },
            "movement": {
                "enabled": True,
                "interval_minutes": movement,
                "active_hours": {"start": "09:00", "end": "18:00"},
                "message_template": "",
            },
        },
        "quiet_hours": {
            "start": quiet_start,
            "end": quiet_end,
            "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        },
    }


def _validate(data: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce + validate a schedule dict. Raises ValueError on malformed
    structure so PUT handlers can return 400 cleanly."""
    if not isinstance(data, dict):
        raise ValueError("schedule must be an object")

    categories = data.get("categories")
    if not isinstance(categories, dict) or not categories:
        raise ValueError("categories must be a non-empty object")

    for cat_name, cat in categories.items():
        if not isinstance(cat, dict):
            raise ValueError(f"category '{cat_name}' must be an object")
        if "enabled" in cat and not isinstance(cat["enabled"], bool):
            raise ValueError(f"category '{cat_name}' enabled must be bool")
        ah = cat.get("active_hours")
        if ah is not None:
            if not isinstance(ah, dict) or "start" not in ah or "end" not in ah:
                raise ValueError(f"category '{cat_name}' active_hours must be {{start, end}}")
            _parse_hhmm(ah["start"], f"{cat_name}.active_hours.start")
            _parse_hhmm(ah["end"], f"{cat_name}.active_hours.end")
        times = cat.get("times")
        if times is not None:
            if not isinstance(times, list):
                raise ValueError(f"category '{cat_name}' times must be a list")
            for i, t in enumerate(times):
                _parse_hhmm(t, f"{cat_name}.times[{i}]")
        for k in ("interval_minutes", "interval_hours"):
            if k in cat and not isinstance(cat[k], (int, float)):
                raise ValueError(f"category '{cat_name}' {k} must be a number")
            if k in cat and cat[k] < 0:
                raise ValueError(f"category '{cat_name}' {k} must be >= 0")

    qh = data.get("quiet_hours") or {}
    if qh:
        if "start" in qh:
            _parse_hhmm(qh["start"], "quiet_hours.start")
        if "end" in qh:
            _parse_hhmm(qh["end"], "quiet_hours.end")
        days = qh.get("days")
        if days is not None:
            if not isinstance(days, list):
                raise ValueError("quiet_hours.days must be a list")
            for d in days:
                if not isinstance(d, str) or d.lower() not in VALID_DAYS:
                    raise ValueError(f"quiet_hours.days entry '{d}' must be one of {sorted(VALID_DAYS)}")

    return data


def _parse_hhmm(value: Any, field: str) -> None:
    """Validate that `value` is a HH:MM string. Raises ValueError on miss."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be HH:MM string, got {type(value).__name__}")
    try:
        h, m = value.split(":")
        h_int, m_int = int(h), int(m)
    except (ValueError, AttributeError):
        raise ValueError(f"{field}='{value}' is not a valid HH:MM time") from None
    if not (0 <= h_int < 24 and 0 <= m_int < 60):
        raise ValueError(f"{field}='{value}' is out of range")


def load_schedule() -> Dict[str, Any]:
    """Load the schedule from disk, falling back to defaults. Cached."""
    global _cache
    with _lock:
        if _cache is not None:
            return deepcopy(_cache)

        path = Path(SCHEDULE_PATH)
        if not path.exists():
            logger.info(f"[SELFCARE_SCHEDULE] No file at {path}, using defaults")
            _cache = _build_defaults()
            return deepcopy(_cache)

        try:
            with open(path) as f:
                raw = yaml.safe_load(f) or {}
            data = _validate(raw)
        except (yaml.YAMLError, ValueError) as e:
            logger.error(f"[SELFCARE_SCHEDULE] Bad YAML at {path}: {e}; using defaults")
            _cache = _build_defaults()
            return deepcopy(_cache)
        except OSError as e:
            logger.error(f"[SELFCARE_SCHEDULE] Read failed for {path}: {e}; using defaults")
            _cache = _build_defaults()
            return deepcopy(_cache)

        # Merge with defaults so callers can rely on every key existing.
        merged = _build_defaults()
        merged_categories = merged["categories"]
        for cat_name, cat in (data.get("categories") or {}).items():
            base = merged_categories.get(cat_name, {})
            base.update(cat)
            merged_categories[cat_name] = base
        if "quiet_hours" in data:
            merged["quiet_hours"].update(data["quiet_hours"])
        _cache = merged
        return deepcopy(_cache)


def save_schedule(data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate + atomically write + invalidate cache. Returns the merged
    schedule that was actually persisted (with defaults filled in)."""
    from orchestrator.config_writer import atomic_write_yaml

    validated = _validate(data)
    atomic_write_yaml(SCHEDULE_PATH, validated)
    return reload_schedule()


def reload_schedule() -> Dict[str, Any]:
    """Force re-read from disk."""
    global _cache
    with _lock:
        _cache = None
    return load_schedule()


# ---------------------------------------------------------------------------
# Convenience accessors used by selfcare_manager.py
# ---------------------------------------------------------------------------


def category_enabled(name: str) -> bool:
    cat = load_schedule()["categories"].get(name, {})
    return bool(cat.get("enabled", True))


def category_interval_minutes(name: str, fallback: int) -> int:
    cat = load_schedule()["categories"].get(name, {})
    if "interval_minutes" in cat:
        return int(cat["interval_minutes"])
    if "interval_hours" in cat:
        return int(float(cat["interval_hours"]) * 60)
    return fallback


def category_active_hours(name: str) -> tuple[Optional[str], Optional[str]]:
    cat = load_schedule()["categories"].get(name, {})
    ah = cat.get("active_hours") or {}
    return ah.get("start"), ah.get("end")


def category_times(name: str) -> list[str]:
    """Return list of fixed times (HH:MM) for time-based categories like meds."""
    cat = load_schedule()["categories"].get(name, {})
    times = cat.get("times") or []
    return [t for t in times if isinstance(t, str)]


def quiet_hours() -> Dict[str, Any]:
    return load_schedule().get("quiet_hours", {})


def is_quiet_day(weekday_iso: int) -> bool:
    """weekday_iso: Monday=1 ... Sunday=7. Matches datetime.isoweekday()."""
    days = quiet_hours().get("days") or list(VALID_DAYS)
    iso_to_label = {1: "mon", 2: "tue", 3: "wed", 4: "thu", 5: "fri", 6: "sat", 7: "sun"}
    return iso_to_label.get(weekday_iso, "") in {d.lower() for d in days}
