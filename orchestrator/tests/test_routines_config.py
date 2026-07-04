"""
Tests for orchestrator/routines_config.py — Routines panel backend.

Covers:
- validate_routines: structural + semantic rejection cases.
- merge_with_existing: power-user field preservation, renamed-step semantics,
  top-level field preservation, untouched-routine preservation, trigger.type
  default of "scheduled".
- effective_path: overrides win when present; falls back to base.
- save_routines: validates, merges, atomic-writes, ha_action survives a
  panel-shaped round-trip that doesn't mention it.
- list_routines_for_panel: strips power-user step fields; defaults for missing
  routine-level fields.
- reload_routines_and_reschedule: reloads routine_manager state, schedules
  cron jobs, prunes deleted routines.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

# ---------------------------------------------------------------------------
# Helpers + fixtures
# ---------------------------------------------------------------------------


def _canonical_payload() -> Dict[str, Any]:
    """A minimum-correct routines payload mirroring the panel's PUT shape."""
    return {
        "routines": {
            "morning": {
                "display_name": "Morning Routine",
                "trigger": {"time": "07:00", "days": ["mon", "tue", "wed"]},
                "speaker": "media_player.bedroom",
                "nudge_delay_minutes": 10,
                "steps": [
                    {"id": "meds", "label": "Take meds", "est_minutes": 2, "skippable": False},
                    {"id": "shower", "label": "Shower", "est_minutes": 15, "skippable": True},
                ],
            }
        }
    }


def _existing_with_ha_action() -> Dict[str, Any]:
    """An on-disk YAML shape with power-user fields the panel can't touch."""
    return {
        "routines": {
            "morning": {
                "display_name": "Morning Routine",
                "trigger": {"type": "scheduled", "time": "07:00", "days": ["mon"]},
                "speaker": "media_player.bedroom",
                "nudge_delay_minutes": 10,
                "steps": [
                    {
                        "id": "meds",
                        "label": "Take meds",
                        "est_minutes": 2,
                        "skippable": False,
                        "ha_action": {
                            "entity_id": "light.bedroom",
                            "service": "turn_on",
                            "data": {"brightness": 200},
                        },
                    },
                    {
                        "id": "breakfast",
                        "label": "Eat breakfast",
                        "est_minutes": 20,
                        "skippable": True,
                        "fallback_label": "Grab a bar",
                        "fallback_threshold_minutes": 30,
                    },
                ],
            },
            "evening": {
                "display_name": "Evening Routine",
                "trigger": {"type": "scheduled", "time": "21:00", "days": ["mon"]},
                "speaker": "media_player.bedroom",
                "nudge_delay_minutes": 15,
                "steps": [
                    {"id": "wind_down", "label": "Wind down", "est_minutes": 5, "skippable": True},
                ],
            },
        }
    }


@pytest.fixture
def routines_paths(tmp_path, monkeypatch):
    """Redirect routines_config to a tmp base + overrides path pair."""
    base = tmp_path / "base.yaml"
    overrides = tmp_path / "overrides.yaml"
    monkeypatch.setenv("ROUTINES_YAML_PATH", str(base))
    monkeypatch.setenv("ROUTINES_OVERRIDES_PATH", str(overrides))
    return {"base": base, "overrides": overrides, "tmp": tmp_path}


# ---------------------------------------------------------------------------
# validate_routines
# ---------------------------------------------------------------------------


def test_validate_routines_accepts_canonical_shape():
    from orchestrator.routines_config import validate_routines

    payload = _canonical_payload()
    out = validate_routines(payload)
    assert out is payload  # validate returns the same dict on success


@pytest.mark.parametrize(
    "payload",
    [
        "not a dict",
        ["a", "list"],
        42,
        None,
    ],
)
def test_validate_routines_rejects_non_dict_root(payload):
    from orchestrator.routines_config import validate_routines

    with pytest.raises(ValueError):
        validate_routines(payload)


def test_validate_routines_rejects_missing_routines():
    from orchestrator.routines_config import validate_routines

    with pytest.raises(ValueError, match="routines must be a non-empty object"):
        validate_routines({})


def test_validate_routines_rejects_empty_routines():
    from orchestrator.routines_config import validate_routines

    with pytest.raises(ValueError, match="routines must be a non-empty object"):
        validate_routines({"routines": {}})


def test_validate_routines_rejects_non_dict_routine():
    from orchestrator.routines_config import validate_routines

    with pytest.raises(ValueError, match="must be an object"):
        validate_routines({"routines": {"morning": "not a dict"}})


@pytest.mark.parametrize("bad_time", ["25:99", "abc", "12", "12:60", "24:00"])
def test_validate_routines_rejects_bad_trigger_time(bad_time):
    from orchestrator.routines_config import validate_routines

    payload = {"routines": {"morning": {"trigger": {"time": bad_time, "days": ["mon"]}}}}
    with pytest.raises(ValueError):
        validate_routines(payload)


def test_validate_routines_rejects_non_string_trigger_time():
    from orchestrator.routines_config import validate_routines

    payload = {"routines": {"morning": {"trigger": {"time": 700, "days": ["mon"]}}}}
    with pytest.raises(ValueError, match="must be HH:MM string"):
        validate_routines(payload)


@pytest.mark.parametrize("bad_day", ["funday", "Monday1", "MO", "", 7])
def test_validate_routines_rejects_bad_day_name(bad_day):
    from orchestrator.routines_config import validate_routines

    payload = {"routines": {"morning": {"trigger": {"time": "07:00", "days": ["mon", bad_day]}}}}
    with pytest.raises(ValueError):
        validate_routines(payload)


def test_validate_routines_rejects_non_list_days():
    from orchestrator.routines_config import validate_routines

    payload = {"routines": {"morning": {"trigger": {"time": "07:00", "days": "mon"}}}}
    with pytest.raises(ValueError, match="trigger.days must be a list"):
        validate_routines(payload)


def test_validate_routines_rejects_duplicate_step_ids():
    from orchestrator.routines_config import validate_routines

    payload = {
        "routines": {
            "morning": {
                "steps": [
                    {"id": "meds", "label": "first"},
                    {"id": "meds", "label": "duplicate"},
                ]
            }
        }
    }
    with pytest.raises(ValueError, match="duplicate step id"):
        validate_routines(payload)


def test_validate_routines_rejects_missing_step_id():
    from orchestrator.routines_config import validate_routines

    payload = {"routines": {"morning": {"steps": [{"label": "no id"}]}}}
    with pytest.raises(ValueError, match=r"step\[0\].id"):
        validate_routines(payload)


@pytest.mark.parametrize("bad_id", [42, ["meds"], "", "   ", None])
def test_validate_routines_rejects_non_string_step_id(bad_id):
    from orchestrator.routines_config import validate_routines

    payload = {"routines": {"morning": {"steps": [{"id": bad_id, "label": "x"}]}}}
    with pytest.raises(ValueError):
        validate_routines(payload)


def test_validate_routines_rejects_negative_est_minutes():
    from orchestrator.routines_config import validate_routines

    payload = {"routines": {"morning": {"steps": [{"id": "x", "label": "x", "est_minutes": -1}]}}}
    with pytest.raises(ValueError, match="est_minutes"):
        validate_routines(payload)


def test_validate_routines_rejects_oversized_est_minutes():
    from orchestrator.routines_config import validate_routines

    payload = {"routines": {"morning": {"steps": [{"id": "x", "label": "x", "est_minutes": 241}]}}}
    with pytest.raises(ValueError, match="est_minutes"):
        validate_routines(payload)


@pytest.mark.parametrize("bad_skip", ["true", 1, 0, "yes", None])
def test_validate_routines_rejects_non_bool_skippable(bad_skip):
    from orchestrator.routines_config import validate_routines

    payload = {"routines": {"morning": {"steps": [{"id": "x", "label": "x", "skippable": bad_skip}]}}}
    if bad_skip is None:
        # The validator only checks `if sk is not None` — so None is allowed.
        validate_routines(payload)
    else:
        with pytest.raises(ValueError, match="skippable"):
            validate_routines(payload)


def test_validate_routines_rejects_non_string_display_name():
    from orchestrator.routines_config import validate_routines

    payload = {"routines": {"morning": {"display_name": 42, "steps": [{"id": "x"}]}}}
    with pytest.raises(ValueError, match="display_name"):
        validate_routines(payload)


@pytest.mark.parametrize("bad_n", [0, -1, 241, 9999])
def test_validate_routines_rejects_bad_nudge_delay(bad_n):
    from orchestrator.routines_config import validate_routines

    payload = {
        "routines": {
            "morning": {
                "nudge_delay_minutes": bad_n,
                "steps": [{"id": "x", "label": "x"}],
            }
        }
    }
    with pytest.raises(ValueError, match="nudge_delay_minutes"):
        validate_routines(payload)


def test_validate_routines_rejects_non_string_speaker():
    from orchestrator.routines_config import validate_routines

    payload = {"routines": {"morning": {"speaker": ["a", "b"], "steps": [{"id": "x"}]}}}
    with pytest.raises(ValueError, match="speaker"):
        validate_routines(payload)


# ---------------------------------------------------------------------------
# merge_with_existing
# ---------------------------------------------------------------------------


def test_merge_preserves_ha_action_on_matching_step_id():
    """The killer test: incoming panel payload doesn't mention ha_action, but
    matching step.id picks it up from existing on-disk YAML."""
    from orchestrator.routines_config import merge_with_existing

    existing = _existing_with_ha_action()
    incoming = {
        "routines": {
            "morning": {
                "steps": [
                    # Same id 'meds' — should pick up ha_action, fallback_*, etc.
                    {"id": "meds", "label": "Take meds NEW LABEL", "est_minutes": 3, "skippable": False},
                ],
            }
        }
    }
    merged = merge_with_existing(incoming, existing)
    meds_step = merged["routines"]["morning"]["steps"][0]
    assert meds_step["label"] == "Take meds NEW LABEL"
    assert meds_step["est_minutes"] == 3
    assert meds_step["ha_action"] == {
        "entity_id": "light.bedroom",
        "service": "turn_on",
        "data": {"brightness": 200},
    }


def test_merge_preserves_all_PRESERVED_step_keys():
    """Every key in PRESERVED_STEP_KEYS comes back on a matching id."""
    from orchestrator.routines_config import PRESERVED_STEP_KEYS, merge_with_existing

    existing = {
        "routines": {
            "morning": {
                "steps": [
                    {
                        "id": "everything",
                        "label": "Old label",
                        "ha_action": {"x": 1},
                        "fallback_label": "fb label",
                        "fallback_threshold_minutes": 20,
                        "include_calendar_summary": True,
                        "calendar_days_ahead": 2,
                    }
                ]
            }
        }
    }
    incoming = {
        "routines": {
            "morning": {"steps": [{"id": "everything", "label": "New label", "est_minutes": 5, "skippable": True}]}
        }
    }
    merged_step = merge_with_existing(incoming, existing)["routines"]["morning"]["steps"][0]
    for k in PRESERVED_STEP_KEYS:
        assert k in merged_step, f"PRESERVED_STEP_KEY {k} dropped on round-trip"


def test_merge_renamed_step_id_loses_power_user_fields():
    """Deliberate behavior: a renamed step.id is treated as a new step. The
    panel can't accidentally re-attach an old ha_action to a different action."""
    from orchestrator.routines_config import merge_with_existing

    existing = _existing_with_ha_action()
    incoming = {
        "routines": {
            "morning": {
                "steps": [
                    # Rename 'meds' -> 'morning_meds'. Different id => different step.
                    {"id": "morning_meds", "label": "Take morning meds", "est_minutes": 2, "skippable": False},
                ],
            }
        }
    }
    merged = merge_with_existing(incoming, existing)
    new_step = merged["routines"]["morning"]["steps"][0]
    assert new_step["id"] == "morning_meds"
    assert "ha_action" not in new_step
    assert "fallback_label" not in new_step


def test_merge_preserves_top_level_fields_panel_didnt_send():
    """speaker, nudge_delay_minutes, display_name absent from incoming
    payload should survive from existing."""
    from orchestrator.routines_config import merge_with_existing

    existing = _existing_with_ha_action()
    incoming = {
        "routines": {
            "morning": {
                # Only steps changed; no speaker/nudge_delay/display_name in payload
                "steps": [{"id": "meds", "label": "Take meds", "est_minutes": 2, "skippable": False}]
            }
        }
    }
    merged_routine = merge_with_existing(incoming, existing)["routines"]["morning"]
    assert merged_routine["speaker"] == "media_player.bedroom"
    assert merged_routine["nudge_delay_minutes"] == 10
    assert merged_routine["display_name"] == "Morning Routine"


def test_merge_preserves_routines_not_in_incoming_payload():
    """If incoming only mentions 'morning', 'evening' must still be present."""
    from orchestrator.routines_config import merge_with_existing

    existing = _existing_with_ha_action()
    incoming = {
        "routines": {"morning": {"steps": [{"id": "meds", "label": "X", "est_minutes": 2, "skippable": False}]}}
    }
    merged = merge_with_existing(incoming, existing)
    assert "evening" in merged["routines"]
    assert merged["routines"]["evening"]["display_name"] == "Evening Routine"


def test_merge_defaults_trigger_type_to_scheduled():
    """When the panel sends a trigger without 'type', merge fills in 'scheduled'."""
    from orchestrator.routines_config import merge_with_existing

    existing = {"routines": {}}
    incoming = {
        "routines": {
            "morning": {
                "trigger": {"time": "08:30", "days": ["mon"]},
                "steps": [{"id": "x", "label": "x", "est_minutes": 1, "skippable": True}],
            }
        }
    }
    merged = merge_with_existing(incoming, existing)
    trig = merged["routines"]["morning"]["trigger"]
    assert trig["type"] == "scheduled"
    assert trig["time"] == "08:30"


# ---------------------------------------------------------------------------
# effective_path
# ---------------------------------------------------------------------------


def test_effective_path_returns_overrides_when_present(routines_paths):
    from orchestrator.routines_config import effective_path

    routines_paths["overrides"].write_text("routines: {}\n")
    assert effective_path() == str(routines_paths["overrides"])


def test_effective_path_falls_back_to_base_when_overrides_missing(routines_paths):
    from orchestrator.routines_config import effective_path

    # overrides file does NOT exist
    assert not routines_paths["overrides"].exists()
    assert effective_path() == str(routines_paths["base"])


def test_load_routines_from_overrides(routines_paths):
    from orchestrator.routines_config import load_routines

    routines_paths["overrides"].write_text(yaml.safe_dump(_existing_with_ha_action()))
    data = load_routines()
    assert "morning" in data["routines"]
    assert data["routines"]["morning"]["steps"][0]["ha_action"]["entity_id"] == "light.bedroom"


def test_load_routines_from_base_when_no_overrides(routines_paths):
    from orchestrator.routines_config import load_routines

    routines_paths["base"].write_text(yaml.safe_dump({"routines": {"morning": {"display_name": "Base"}}}))
    data = load_routines()
    assert data["routines"]["morning"]["display_name"] == "Base"


# ---------------------------------------------------------------------------
# save_routines
# ---------------------------------------------------------------------------


def test_save_routines_writes_to_overrides(routines_paths):
    from orchestrator.routines_config import load_routines, save_routines

    payload = _canonical_payload()
    save_routines(payload)
    assert routines_paths["overrides"].exists()
    on_disk = yaml.safe_load(routines_paths["overrides"].read_text())
    assert "morning" in on_disk["routines"]
    # And load_routines reads it back identically.
    loaded = load_routines()
    assert loaded == on_disk


def test_save_routines_validates_before_write(routines_paths):
    from orchestrator.routines_config import save_routines

    bad = {"routines": {"morning": {"trigger": {"time": "99:99"}}}}
    with pytest.raises(ValueError):
        save_routines(bad)
    # Nothing landed on disk.
    assert not routines_paths["overrides"].exists()


def test_save_routines_ha_action_survives_round_trip(routines_paths):
    """The KILLER test for the panel write path:
    1. Seed an existing routines.yaml with a step that has ha_action.
    2. Save a panel-shaped payload that does NOT mention ha_action.
    3. Re-load + verify ha_action is still on disk.
    """
    from orchestrator.routines_config import load_routines, save_routines

    routines_paths["overrides"].write_text(yaml.safe_dump(_existing_with_ha_action()))

    panel_payload = {
        "routines": {
            "morning": {
                "display_name": "Morning Routine",
                "trigger": {"time": "07:30", "days": ["mon", "tue"]},
                "speaker": "media_player.bedroom",
                "nudge_delay_minutes": 10,
                "steps": [
                    # Same id 'meds' — ha_action MUST come back from disk.
                    {"id": "meds", "label": "Take meds", "est_minutes": 2, "skippable": False},
                    {"id": "breakfast", "label": "Eat breakfast", "est_minutes": 20, "skippable": True},
                ],
            }
        }
    }
    save_routines(panel_payload)

    on_disk = load_routines()
    meds = on_disk["routines"]["morning"]["steps"][0]
    assert meds["ha_action"] == {
        "entity_id": "light.bedroom",
        "service": "turn_on",
        "data": {"brightness": 200},
    }
    # And label was actually edited (sanity).
    assert meds["label"] == "Take meds"
    assert on_disk["routines"]["morning"]["trigger"]["time"] == "07:30"
    # 'evening' routine NOT in payload — must still be on disk.
    assert "evening" in on_disk["routines"]


def test_save_routines_preserves_fallback_fields(routines_paths):
    """fallback_label + fallback_threshold_minutes survive a round-trip."""
    from orchestrator.routines_config import load_routines, save_routines

    routines_paths["overrides"].write_text(yaml.safe_dump(_existing_with_ha_action()))

    panel_payload = {
        "routines": {
            "morning": {
                "steps": [
                    {"id": "meds", "label": "M", "est_minutes": 2, "skippable": False},
                    {"id": "breakfast", "label": "Eat", "est_minutes": 20, "skippable": True},
                ]
            }
        }
    }
    save_routines(panel_payload)
    on_disk = load_routines()
    breakfast = on_disk["routines"]["morning"]["steps"][1]
    assert breakfast["fallback_label"] == "Grab a bar"
    assert breakfast["fallback_threshold_minutes"] == 30


# ---------------------------------------------------------------------------
# list_routines_for_panel
# ---------------------------------------------------------------------------


def test_list_routines_strips_power_user_step_keys(routines_paths):
    from orchestrator.routines_config import list_routines_for_panel

    routines_paths["overrides"].write_text(yaml.safe_dump(_existing_with_ha_action()))
    out = list_routines_for_panel()

    morning_steps = out["routines"]["morning"]["steps"]
    meds = morning_steps[0]
    # Editable keys present:
    assert meds["id"] == "meds"
    assert meds["label"] == "Take meds"
    assert "est_minutes" in meds
    assert "skippable" in meds
    # Power-user keys stripped:
    for k in (
        "ha_action",
        "fallback_label",
        "fallback_threshold_minutes",
        "include_calendar_summary",
        "calendar_days_ahead",
    ):
        assert k not in meds, f"power-user key {k!r} leaked into panel view"

    breakfast = morning_steps[1]
    for k in ("fallback_label", "fallback_threshold_minutes"):
        assert k not in breakfast


def test_list_routines_defaults_for_missing_routine_fields(routines_paths):
    from orchestrator.routines_config import list_routines_for_panel

    # Bare routine — no display_name, no speaker, no nudge_delay_minutes, no trigger.
    routines_paths["overrides"].write_text(yaml.safe_dump({"routines": {"weekend": {"steps": []}}}))
    out = list_routines_for_panel()
    weekend = out["routines"]["weekend"]
    assert weekend["display_name"] == "Weekend"  # rid.title()
    assert weekend["speaker"] == ""
    assert weekend["nudge_delay_minutes"] == 10
    assert weekend["trigger"]["time"] == "07:00"
    assert weekend["trigger"]["days"] == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    assert weekend["steps"] == []


def test_list_routines_skips_non_dict_routine(routines_paths):
    from orchestrator.routines_config import list_routines_for_panel

    routines_paths["overrides"].write_text(
        yaml.safe_dump({"routines": {"morning": "garbage", "evening": {"steps": []}}})
    )
    out = list_routines_for_panel()
    assert "morning" not in out["routines"]
    assert "evening" in out["routines"]


# ---------------------------------------------------------------------------
# reload_routines_and_reschedule
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_scheduler(monkeypatch):
    """Replace shared.scheduler with a MagicMock that records add_job/remove_job/get_jobs."""
    fake = MagicMock()
    fake._jobs = {}

    def add_job(func, *, trigger, hour, minute, day_of_week, args, id, name, replace_existing):
        fake._jobs[id] = MagicMock(id=id, hour=hour, minute=minute, day_of_week=day_of_week, args=args)
        return fake._jobs[id]

    def remove_job(job_id):
        if job_id in fake._jobs:
            del fake._jobs[job_id]
        else:
            raise KeyError(job_id)

    def get_jobs():
        return list(fake._jobs.values())

    fake.add_job.side_effect = add_job
    fake.remove_job.side_effect = remove_job
    fake.get_jobs.side_effect = get_jobs

    from orchestrator import shared

    monkeypatch.setattr(shared, "scheduler", fake)
    return fake


@pytest.mark.asyncio
async def test_reload_loads_routines_and_schedules_crons(routines_paths, fake_scheduler, monkeypatch):
    from orchestrator import background_jobs, routine_manager
    from orchestrator.routines_config import reload_routines_and_reschedule

    # Mock out trigger_routine and routine_manager.load_routines
    monkeypatch.setattr(background_jobs, "trigger_routine", AsyncMock())
    fake_load = AsyncMock()
    monkeypatch.setattr(routine_manager, "load_routines", fake_load)
    monkeypatch.setattr(routine_manager, "_routines", {"morning": {}, "evening": {}})

    routines_paths["overrides"].write_text(yaml.safe_dump(_existing_with_ha_action()))

    summary = await reload_routines_and_reschedule()

    fake_load.assert_awaited_once_with(str(routines_paths["overrides"]))
    assert sorted(summary["loaded"]) == ["evening", "morning"]
    assert "routine_morning" in summary["rescheduled"]
    assert "routine_evening" in summary["rescheduled"]
    assert summary["removed"] == []

    # The scheduler got two cron jobs added with correct hour/minute
    morning_job = fake_scheduler._jobs["routine_morning"]
    assert morning_job.hour == 7 and morning_job.minute == 0
    assert morning_job.args == ["morning"]
    evening_job = fake_scheduler._jobs["routine_evening"]
    assert evening_job.hour == 21 and evening_job.minute == 0


@pytest.mark.asyncio
async def test_reload_removes_stale_routine_jobs(routines_paths, fake_scheduler, monkeypatch):
    from orchestrator import background_jobs, routine_manager
    from orchestrator.routines_config import reload_routines_and_reschedule

    monkeypatch.setattr(background_jobs, "trigger_routine", AsyncMock())
    monkeypatch.setattr(routine_manager, "load_routines", AsyncMock())
    monkeypatch.setattr(routine_manager, "_routines", {"morning": {}})

    # Pre-seed the scheduler with a stale job that's no longer in YAML.
    fake_scheduler._jobs["routine_old_id"] = MagicMock(id="routine_old_id")
    fake_scheduler._jobs["routine_unrelated"] = MagicMock(id="non_routine")  # also pre-seed
    # Only 'morning' exists in incoming YAML.
    yaml_only_morning = {
        "routines": {
            "morning": {
                "trigger": {"type": "scheduled", "time": "07:00", "days": ["mon"]},
                "steps": [{"id": "x", "label": "x"}],
            }
        }
    }
    routines_paths["overrides"].write_text(yaml.safe_dump(yaml_only_morning))

    summary = await reload_routines_and_reschedule()
    assert "routine_old_id" in summary["removed"]
    assert "routine_morning" in summary["rescheduled"]


@pytest.mark.asyncio
async def test_reload_preserves_live_nudge_jobs(routines_paths, fake_scheduler, monkeypatch):
    """Regression: a settings PUT during an active routine must NOT prune the
    live routine_nudge_* job. Killing it disabled the stuck-step auto-skip /
    auto-end escape, and the orphaned session then blocked every future
    scheduled routine."""
    from orchestrator import background_jobs, routine_manager
    from orchestrator.routines_config import reload_routines_and_reschedule

    monkeypatch.setattr(background_jobs, "trigger_routine", AsyncMock())
    monkeypatch.setattr(routine_manager, "load_routines", AsyncMock())
    monkeypatch.setattr(routine_manager, "_routines", {"morning": {}})

    # Simulate an active routine session: its nudge job is in the scheduler
    # (naming scheme from routine_manager._schedule_nudge), plus a genuinely
    # stale trigger job that SHOULD be pruned.
    fake_scheduler._jobs["routine_nudge_073015"] = MagicMock(id="routine_nudge_073015")
    fake_scheduler._jobs["routine_old_id"] = MagicMock(id="routine_old_id")

    yaml_only_morning = {
        "routines": {
            "morning": {
                "trigger": {"type": "scheduled", "time": "07:00", "days": ["mon"]},
                "steps": [{"id": "x", "label": "x"}],
            }
        }
    }
    routines_paths["overrides"].write_text(yaml.safe_dump(yaml_only_morning))

    summary = await reload_routines_and_reschedule()

    # Stale trigger pruned; live nudge job untouched.
    assert summary["removed"] == ["routine_old_id"]
    assert "routine_nudge_073015" in fake_scheduler._jobs
    assert "routine_old_id" not in fake_scheduler._jobs


def test_is_routine_trigger_job_classifier():
    from orchestrator.routines_config import _is_routine_trigger_job

    assert _is_routine_trigger_job("routine_morning")
    assert _is_routine_trigger_job("routine_evening")
    assert not _is_routine_trigger_job("routine_nudge_073015")
    assert not _is_routine_trigger_job("reminder_42")
    assert not _is_routine_trigger_job("selfcare_check")


@pytest.mark.asyncio
async def test_reload_skips_non_scheduled_triggers(routines_paths, fake_scheduler, monkeypatch):
    from orchestrator import background_jobs, routine_manager
    from orchestrator.routines_config import reload_routines_and_reschedule

    monkeypatch.setattr(background_jobs, "trigger_routine", AsyncMock())
    monkeypatch.setattr(routine_manager, "load_routines", AsyncMock())
    monkeypatch.setattr(routine_manager, "_routines", {"adhoc": {}})

    routines_paths["overrides"].write_text(
        yaml.safe_dump({"routines": {"adhoc": {"trigger": {"type": "manual"}, "steps": [{"id": "x"}]}}})
    )

    summary = await reload_routines_and_reschedule()
    assert summary["rescheduled"] == []


@pytest.mark.asyncio
async def test_reload_skips_routine_with_bad_time(routines_paths, fake_scheduler, monkeypatch, caplog):
    """Defensive: if YAML somehow contains a bad trigger.time after save,
    reload logs a warning and skips that routine instead of crashing."""
    from orchestrator import background_jobs, routine_manager
    from orchestrator.routines_config import reload_routines_and_reschedule

    monkeypatch.setattr(background_jobs, "trigger_routine", AsyncMock())
    monkeypatch.setattr(routine_manager, "load_routines", AsyncMock())
    monkeypatch.setattr(routine_manager, "_routines", {})

    routines_paths["overrides"].write_text(
        yaml.safe_dump(
            {"routines": {"broken": {"trigger": {"type": "scheduled", "time": "not-a-time"}, "steps": [{"id": "x"}]}}}
        )
    )

    summary = await reload_routines_and_reschedule()
    assert summary["rescheduled"] == []
