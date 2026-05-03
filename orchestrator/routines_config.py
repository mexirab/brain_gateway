"""
Routines config — loader/saver for /api/config/routines.

Routines (`morning`, `evening`, …) ship as `/app/config/routines.yaml`,
mounted read-only in the container. Settings-page edits write to a
writable shadow at `/app/data/routines.yaml` (`ROUTINES_OVERRIDES_PATH`).
The loader prefers the shadow when present.

What the panel edits (v1):
- `display_name`, `trigger.time`, `trigger.days`, `speaker`,
  `nudge_delay_minutes`
- per-step: `id`, `label`, `est_minutes`, `skippable`

What it preserves untouched on round-trip (power-user fields):
- per-step: `ha_action`, `fallback_label`, `fallback_threshold_minutes`,
  `include_calendar_summary`, `calendar_days_ahead`
- top-level: anything we don't recognize

The merge is "outer keys win, inner power-user keys preserved" — the
panel sends a full routines payload, but for any step whose `id` matches
an existing step, we splice the power-user fields back in before write.
"""

from __future__ import annotations

import asyncio
import logging
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# These are the keys the settings page is allowed to set on a step.
# Anything else on an existing step is preserved verbatim.
EDITABLE_STEP_KEYS = {"id", "label", "est_minutes", "skippable"}
PRESERVED_STEP_KEYS = {
    "ha_action",
    "fallback_label",
    "fallback_threshold_minutes",
    "include_calendar_summary",
    "calendar_days_ahead",
}

VALID_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


def _base_path() -> str:
    """Read-only base path mounted into the container."""
    return os.environ.get("ROUTINES_YAML_PATH", "/app/config/routines.yaml")


def _overrides_path() -> str:
    """Writable shadow that the settings page writes to."""
    return os.environ.get("ROUTINES_OVERRIDES_PATH", "/app/data/routines.yaml")


def _load_yaml(path: str) -> Dict[str, Any]:
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    except (yaml.YAMLError, OSError) as e:
        logger.error(f"[ROUTINES] Failed to read {path}: {e}")
        return {}


def effective_path() -> str:
    """Path the running orchestrator should read from. Overrides win when present."""
    overrides = _overrides_path()
    if Path(overrides).exists():
        return overrides
    return _base_path()


def load_routines() -> Dict[str, Any]:
    """Return the full routines dict from whichever file is effective."""
    return _load_yaml(effective_path())


def _validate_hhmm(value: Any, field: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be HH:MM string")
    try:
        h, m = value.split(":")
        h_i, m_i = int(h), int(m)
    except (ValueError, AttributeError):
        raise ValueError(f"{field}={value!r} is not HH:MM") from None
    if not (0 <= h_i < 24 and 0 <= m_i < 60):
        raise ValueError(f"{field}={value!r} is out of range")


def validate_routines(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce + validate an incoming `{routines: {...}}` payload.
    Raises ValueError on malformed structure so PUT can return 400.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    routines = payload.get("routines")
    if not isinstance(routines, dict) or not routines:
        raise ValueError("routines must be a non-empty object")

    for rid, routine in routines.items():
        if not isinstance(rid, str) or not rid.strip():
            raise ValueError("routine id must be a non-empty string")
        if not isinstance(routine, dict):
            raise ValueError(f"routine '{rid}' must be an object")

        if "display_name" in routine and not isinstance(routine["display_name"], str):
            raise ValueError(f"routine '{rid}' display_name must be a string")

        trigger = routine.get("trigger")
        if trigger is not None:
            if not isinstance(trigger, dict):
                raise ValueError(f"routine '{rid}' trigger must be an object")
            if "time" in trigger:
                _validate_hhmm(trigger["time"], f"routine '{rid}' trigger.time")
            if "days" in trigger:
                days = trigger["days"]
                if not isinstance(days, list):
                    raise ValueError(f"routine '{rid}' trigger.days must be a list")
                for d in days:
                    if not isinstance(d, str) or d.lower() not in VALID_DAYS:
                        raise ValueError(
                            f"routine '{rid}' trigger.days entry {d!r} must be one of {sorted(VALID_DAYS)}"
                        )

        if "speaker" in routine and not isinstance(routine["speaker"], str):
            raise ValueError(f"routine '{rid}' speaker must be a string")
        if "nudge_delay_minutes" in routine:
            n = routine["nudge_delay_minutes"]
            if not isinstance(n, (int, float)) or n < 1 or n > 240:
                raise ValueError(f"routine '{rid}' nudge_delay_minutes must be 1–240")
        if "nudge_max" in routine:
            nm = routine["nudge_max"]
            if not isinstance(nm, int) or nm < 1 or nm > 20:
                raise ValueError(f"routine '{rid}' nudge_max must be 1–20")
        if "auto_skip" in routine and not isinstance(routine["auto_skip"], bool):
            raise ValueError(f"routine '{rid}' auto_skip must be bool")

        steps = routine.get("steps")
        if steps is not None:
            if not isinstance(steps, list) or not steps:
                raise ValueError(f"routine '{rid}' steps must be a non-empty list")
            seen_ids: set[str] = set()
            for i, step in enumerate(steps):
                if not isinstance(step, dict):
                    raise ValueError(f"routine '{rid}' step[{i}] must be an object")
                sid = step.get("id")
                if not isinstance(sid, str) or not sid.strip():
                    raise ValueError(f"routine '{rid}' step[{i}].id must be a non-empty string")
                if sid in seen_ids:
                    raise ValueError(f"routine '{rid}' duplicate step id {sid!r}")
                seen_ids.add(sid)
                label = step.get("label")
                if label is not None and not isinstance(label, str):
                    raise ValueError(f"routine '{rid}' step[{i}].label must be a string")
                em = step.get("est_minutes")
                if em is not None and (not isinstance(em, (int, float)) or em < 0 or em > 240):
                    raise ValueError(f"routine '{rid}' step[{i}].est_minutes must be 0–240")
                sk = step.get("skippable")
                if sk is not None and not isinstance(sk, bool):
                    raise ValueError(f"routine '{rid}' step[{i}].skippable must be bool")

    return payload


def _merge_step(incoming: Dict[str, Any], existing_by_id: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Take an incoming step from the panel + splice in preserved
    power-user fields from the matching existing step (by id)."""
    sid = incoming.get("id")
    out: Dict[str, Any] = {k: v for k, v in incoming.items() if k in EDITABLE_STEP_KEYS}
    if sid and sid in existing_by_id:
        for k in PRESERVED_STEP_KEYS:
            if k in existing_by_id[sid]:
                out[k] = existing_by_id[sid][k]
    return out


def merge_with_existing(payload: Dict[str, Any], existing: Dict[str, Any]) -> Dict[str, Any]:
    """Merge the incoming editable payload onto the existing on-disk YAML
    so power-user fields (ha_action, fallback_*, include_calendar_summary,
    calendar_days_ahead) survive the round-trip."""
    out = deepcopy(existing)
    out_routines = out.setdefault("routines", {})

    for rid, incoming_routine in payload["routines"].items():
        existing_routine = out_routines.get(rid, {})
        merged = deepcopy(existing_routine)

        for key in ("display_name", "speaker", "nudge_delay_minutes", "nudge_max", "auto_skip"):
            if key in incoming_routine:
                merged[key] = incoming_routine[key]

        if "trigger" in incoming_routine:
            existing_trigger = merged.get("trigger") or {}
            new_trigger = dict(existing_trigger)
            for tk in ("time", "days", "type"):
                if tk in incoming_routine["trigger"]:
                    new_trigger[tk] = incoming_routine["trigger"][tk]
            new_trigger.setdefault("type", "scheduled")
            merged["trigger"] = new_trigger

        if "steps" in incoming_routine:
            existing_steps_by_id = {
                s.get("id"): s for s in (existing_routine.get("steps") or []) if isinstance(s, dict) and s.get("id")
            }
            merged["steps"] = [_merge_step(s, existing_steps_by_id) for s in incoming_routine["steps"]]

        out_routines[rid] = merged

    return out


def save_routines(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate, merge with existing (preserving power-user fields), and
    atomically write to the writable overrides path. Returns the saved dict."""
    from orchestrator.config_writer import atomic_write_yaml

    validate_routines(payload)
    existing = _load_yaml(effective_path())
    merged = merge_with_existing(payload, existing)
    atomic_write_yaml(_overrides_path(), merged)
    return merged


def list_routines_for_panel() -> Dict[str, Any]:
    """Return the editable view of all routines for the GET endpoint.
    Strips out power-user fields the panel doesn't render so the UI's
    JSON.stringify dirty-state diff doesn't get confused by fields it
    can't touch.
    """
    raw = load_routines()
    routines = raw.get("routines") or {}
    out: Dict[str, Any] = {}
    for rid, r in routines.items():
        if not isinstance(r, dict):
            continue
        steps_out: List[Dict[str, Any]] = []
        for s in r.get("steps") or []:
            if not isinstance(s, dict):
                continue
            steps_out.append({k: s.get(k) for k in ("id", "label", "est_minutes", "skippable") if k in s})
        # Surface per-routine values when set, else fall through to the
        # global ROUTINE_NUDGE_MAX / ROUTINE_AUTO_SKIP env-var defaults so
        # the panel always shows the effective value.
        global_nudge_max = 3
        global_auto_skip = False
        try:
            from orchestrator import shared  # local import to avoid bootstrap cycles

            global_nudge_max = int(getattr(shared, "ROUTINE_NUDGE_MAX", 3))
            global_auto_skip = bool(getattr(shared, "ROUTINE_AUTO_SKIP", False))
        except Exception:
            pass

        out[rid] = {
            "display_name": r.get("display_name", rid.title()),
            "trigger": {
                "time": (r.get("trigger") or {}).get("time", "07:00"),
                "days": (r.get("trigger") or {}).get("days", ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]),
            },
            "speaker": r.get("speaker", ""),
            "nudge_delay_minutes": r.get("nudge_delay_minutes", 10),
            "nudge_max": r.get("nudge_max", global_nudge_max),
            "auto_skip": bool(r.get("auto_skip", global_auto_skip)),
            "steps": steps_out,
        }
    return {"routines": out}


# ---------------------------------------------------------------------------
# Hot-reload: re-register APScheduler triggers + reload routine_manager state
# ---------------------------------------------------------------------------

# Serializes concurrent reloads — without this, two overlapping PUTs could
# race on `existing_routine_jobs - rescheduled` and one PUT's prune step
# could delete a routine the other PUT just added (silent until next restart).
_reload_lock = asyncio.Lock()


async def reload_routines_and_reschedule() -> Dict[str, Any]:
    """Called from the PUT handler after a successful save. Updates
    `routine_manager._routines` in place AND replaces the
    `routine_<id>` cron jobs in APScheduler so a time/day change takes
    effect without restart.

    Returns a small dict useful for the API response and tests:
        {"loaded": ["morning", "evening"],
         "rescheduled": ["routine_morning", "routine_evening"],
         "removed": ["routine_old_id"]}  # ids that no longer exist

    Concurrency: serialized via a module-level `asyncio.Lock` so two
    simultaneous PUTs can't interleave and silently delete each other's
    new routines during the prune step.
    """
    async with _reload_lock:
        return await _reschedule_locked()


async def _reschedule_locked() -> Dict[str, Any]:
    from orchestrator import routine_manager
    from orchestrator.background_jobs import trigger_routine
    from orchestrator.shared import scheduler

    # 1. Reload routine_manager._routines from disk
    await routine_manager.load_routines(effective_path())
    loaded = sorted(routine_manager._routines.keys())

    # 2. Find existing routine_* jobs in the scheduler so we can prune
    existing_routine_jobs = {job.id for job in scheduler.get_jobs() if job.id.startswith("routine_")}

    # 3. Re-register cron triggers from the freshly loaded YAML
    rescheduled: List[str] = []
    raw = load_routines()
    for rid, rdef in (raw.get("routines") or {}).items():
        trigger = rdef.get("trigger") or {}
        if trigger.get("type") != "scheduled":
            continue
        time_str = trigger.get("time")
        if not time_str:
            continue
        try:
            hour, minute = map(int, time_str.split(":"))
        except (ValueError, AttributeError):
            logger.warning(f"[ROUTINES] Skipping {rid}: bad trigger.time={time_str!r}")
            continue
        days = trigger.get("days") or ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        dow = ",".join(d[:3].lower() for d in days)
        job_id = f"routine_{rid}"
        scheduler.add_job(
            trigger_routine,
            trigger="cron",
            hour=hour,
            minute=minute,
            day_of_week=dow,
            args=[rid],
            id=job_id,
            name=f"Routine trigger: {rid}",
            replace_existing=True,
        )
        rescheduled.append(job_id)

    # 4. Remove any routine_* jobs whose routine has been deleted
    removed: List[str] = []
    for job_id in existing_routine_jobs - set(rescheduled):
        try:
            scheduler.remove_job(job_id)
            removed.append(job_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[ROUTINES] Could not remove stale job {job_id}: {e}")

    logger.info(
        f"[ROUTINES] Reloaded {len(loaded)} routine(s); rescheduled {sorted(rescheduled)}; removed {sorted(removed)}"
    )
    return {"loaded": loaded, "rescheduled": sorted(rescheduled), "removed": sorted(removed)}


def reload_routines_path_only() -> Optional[str]:
    """Tiny helper used by tests + tooling — returns the effective path
    without doing any side effects. Convenient when monkeypatching."""
    return effective_path()
