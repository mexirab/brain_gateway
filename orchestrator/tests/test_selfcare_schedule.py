"""
Tests for orchestrator/selfcare_schedule.py.

Covers:
1. load_schedule defaults when file missing.
2. save_schedule round-trip merges with defaults to fill untouched keys.
3. _validate rejects bad HH:MM, wrong day names, negative interval, non-list days.
4. category_enabled / category_interval_minutes / category_active_hours / category_times.
5. is_quiet_day correctly maps Mon=1..Sun=7 against configured days.
6. reload_schedule invalidates the module cache.
"""

from __future__ import annotations

import pytest
import yaml


@pytest.fixture
def isolated_schedule(tmp_path, monkeypatch):
    """Point SCHEDULE_PATH at a tmp file and clear the module cache."""
    from orchestrator import selfcare_schedule

    sched_path = tmp_path / "selfcare_schedule.yaml"
    monkeypatch.setattr(selfcare_schedule, "SCHEDULE_PATH", str(sched_path))
    # Wipe any cached load from a prior test in the session.
    monkeypatch.setattr(selfcare_schedule, "_cache", None)
    return sched_path


# ---------------------------------------------------------------------------
# load_schedule + defaults
# ---------------------------------------------------------------------------


def test_load_schedule_returns_defaults_when_file_missing(isolated_schedule):
    from orchestrator.selfcare_schedule import load_schedule

    assert not isolated_schedule.exists()
    sched = load_schedule()

    # Default categories present.
    assert set(sched["categories"].keys()) >= {"water", "meds", "meals", "movement"}
    assert sched["categories"]["water"]["enabled"] is True
    assert "interval_minutes" in sched["categories"]["water"]
    # quiet_hours present with all days.
    assert sched["quiet_hours"]["start"]
    assert sched["quiet_hours"]["end"]
    assert set(sched["quiet_hours"]["days"]) == {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


def test_save_schedule_round_trip_merges_with_defaults(isolated_schedule):
    """Writing a partial schedule still has untouched keys filled in by defaults."""
    from orchestrator.selfcare_schedule import save_schedule

    partial = {
        "categories": {
            "water": {
                "enabled": False,
                "interval_minutes": 120,
                "active_hours": {"start": "10:00", "end": "20:00"},
            },
        },
    }
    saved = save_schedule(partial)

    # The user-specified change is persisted.
    assert saved["categories"]["water"]["enabled"] is False
    assert saved["categories"]["water"]["interval_minutes"] == 120
    # Untouched categories remain via defaults.
    for missing in ("meds", "meals", "movement"):
        assert missing in saved["categories"], f"default category {missing} dropped"
    # Quiet hours not specified → still present from defaults.
    assert saved["quiet_hours"]["start"]
    assert saved["quiet_hours"]["days"]


# ---------------------------------------------------------------------------
# _validate
# ---------------------------------------------------------------------------


def test_validate_rejects_bad_hhmm():
    from orchestrator.selfcare_schedule import _validate

    with pytest.raises(ValueError):
        _validate(
            {
                "categories": {
                    "water": {
                        "enabled": True,
                        "active_hours": {"start": "25:00", "end": "21:00"},
                    },
                },
            }
        )

    with pytest.raises(ValueError):
        _validate(
            {
                "categories": {
                    "water": {
                        "enabled": True,
                        "active_hours": {"start": "abc", "end": "21:00"},
                    },
                },
            }
        )


def test_validate_rejects_bad_day_name():
    from orchestrator.selfcare_schedule import _validate

    with pytest.raises(ValueError):
        _validate(
            {
                "categories": {"water": {"enabled": True}},
                "quiet_hours": {"days": ["mon", "funday"]},
            }
        )


def test_validate_rejects_negative_interval():
    from orchestrator.selfcare_schedule import _validate

    with pytest.raises(ValueError):
        _validate(
            {
                "categories": {"water": {"enabled": True, "interval_minutes": -10}},
            }
        )


def test_validate_rejects_non_list_days():
    from orchestrator.selfcare_schedule import _validate

    with pytest.raises(ValueError):
        _validate(
            {
                "categories": {"water": {"enabled": True}},
                "quiet_hours": {"days": "mon,tue"},  # string, not list
            }
        )


def test_validate_rejects_non_dict_root():
    from orchestrator.selfcare_schedule import _validate

    with pytest.raises(ValueError):
        _validate(["not", "a", "dict"])  # type: ignore[arg-type]


def test_validate_rejects_empty_categories():
    from orchestrator.selfcare_schedule import _validate

    with pytest.raises(ValueError):
        _validate({"categories": {}})


def test_validate_rejects_bad_times_entry():
    from orchestrator.selfcare_schedule import _validate

    with pytest.raises(ValueError):
        _validate(
            {
                "categories": {"meds": {"enabled": True, "times": ["08:00", "30:99"]}},
            }
        )


# ---------------------------------------------------------------------------
# Convenience accessors
# ---------------------------------------------------------------------------


def test_category_enabled(isolated_schedule):
    from orchestrator.selfcare_schedule import category_enabled, save_schedule

    save_schedule(
        {
            "categories": {
                "water": {"enabled": False},
                "meds": {"enabled": True},
            },
        }
    )
    assert category_enabled("water") is False
    assert category_enabled("meds") is True
    # Unknown category → defaults to True (the function's default fallback).
    assert category_enabled("nonexistent_category") is True


def test_category_interval_minutes_with_interval_minutes(isolated_schedule):
    from orchestrator.selfcare_schedule import category_interval_minutes, save_schedule

    save_schedule(
        {
            "categories": {"water": {"enabled": True, "interval_minutes": 75}},
        }
    )
    assert category_interval_minutes("water", fallback=999) == 75


def test_category_interval_minutes_converts_hours_to_minutes(isolated_schedule):
    from orchestrator.selfcare_schedule import category_interval_minutes, save_schedule

    save_schedule(
        {
            "categories": {"meals": {"enabled": True, "interval_hours": 3}},
        }
    )
    assert category_interval_minutes("meals", fallback=999) == 180


def test_category_interval_minutes_falls_back(isolated_schedule):
    """If a category has no interval_minutes / interval_hours, the fallback is used.

    We use a category name that isn't in the defaults (`workshop`) so the
    default-merge in load_schedule doesn't quietly fill in `interval_minutes=90`
    from the `water` default.
    """
    from orchestrator.selfcare_schedule import category_interval_minutes, save_schedule

    save_schedule(
        {
            "categories": {
                "workshop": {"enabled": True},  # custom cat, no defaults to merge in
            },
        }
    )
    assert category_interval_minutes("workshop", fallback=42) == 42
    # An unknown category also falls through to the fallback.
    assert category_interval_minutes("nonexistent", fallback=17) == 17


def test_category_active_hours(isolated_schedule):
    from orchestrator.selfcare_schedule import category_active_hours, save_schedule

    save_schedule(
        {
            "categories": {
                "water": {
                    "enabled": True,
                    "active_hours": {"start": "08:00", "end": "22:00"},
                },
            },
        }
    )
    start, end = category_active_hours("water")
    assert start == "08:00"
    assert end == "22:00"


def test_category_times(isolated_schedule):
    from orchestrator.selfcare_schedule import category_times, save_schedule

    save_schedule(
        {
            "categories": {"meds": {"enabled": True, "times": ["08:00", "20:00"]}},
        }
    )
    assert category_times("meds") == ["08:00", "20:00"]
    # Category with no times → empty list.
    assert category_times("water") == []


# ---------------------------------------------------------------------------
# is_quiet_day
# ---------------------------------------------------------------------------


def test_is_quiet_day_only_listed_days_match(isolated_schedule):
    """is_quiet_day returns True when the weekday IS in the configured days list."""
    from orchestrator.selfcare_schedule import is_quiet_day, save_schedule

    save_schedule(
        {
            "categories": {"water": {"enabled": True}},
            "quiet_hours": {"start": "22:00", "end": "07:00", "days": ["sat", "sun"]},
        }
    )
    # Mon=1..Sun=7 (datetime.isoweekday). Sat=6, Sun=7 are configured.
    assert is_quiet_day(1) is False  # Mon
    assert is_quiet_day(2) is False  # Tue
    assert is_quiet_day(5) is False  # Fri
    assert is_quiet_day(6) is True  # Sat
    assert is_quiet_day(7) is True  # Sun


def test_is_quiet_day_handles_mixed_case(isolated_schedule):
    """The accessor lowercases configured days for the comparison."""
    from orchestrator.selfcare_schedule import is_quiet_day, save_schedule

    # _validate accepts mixed case as long as lowercased value is valid.
    save_schedule(
        {
            "categories": {"water": {"enabled": True}},
            "quiet_hours": {"start": "22:00", "end": "07:00", "days": ["MON", "Tue"]},
        }
    )
    assert is_quiet_day(1) is True
    assert is_quiet_day(2) is True
    assert is_quiet_day(3) is False


# ---------------------------------------------------------------------------
# reload_schedule cache invalidation
# ---------------------------------------------------------------------------


def test_reload_schedule_invalidates_cache(isolated_schedule):
    """Mutating the file directly must NOT be visible until reload_schedule()."""
    from orchestrator.selfcare_schedule import load_schedule, reload_schedule, save_schedule

    save_schedule(
        {
            "categories": {"water": {"enabled": True, "interval_minutes": 60}},
        }
    )
    assert load_schedule()["categories"]["water"]["interval_minutes"] == 60

    # Mutate the file behind the cache's back.
    raw = yaml.safe_load(isolated_schedule.read_text())
    raw["categories"]["water"]["interval_minutes"] = 333
    isolated_schedule.write_text(yaml.safe_dump(raw))

    # Cached value still returns the old number.
    assert load_schedule()["categories"]["water"]["interval_minutes"] == 60

    # After reload, the new value is visible.
    assert reload_schedule()["categories"]["water"]["interval_minutes"] == 333
    assert load_schedule()["categories"]["water"]["interval_minutes"] == 333
