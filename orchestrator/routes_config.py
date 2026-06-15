"""
Settings page API — `/api/config/*`.

Backs the four panels in the dashboard `/settings` page:

- Identity & tone   → user_profile.yaml overrides
- Selfcare nudges   → data/selfcare_schedule.yaml (categories.*)
- Quiet hours       → data/selfcare_schedule.yaml (quiet_hours.*)
- Recurring reminders → routes_config_recurring.py (separate file to keep
  this one focused on YAML-backed settings)

All endpoints inherit bearer auth via `BearerAuthMiddleware` — they are
NOT in `PUBLIC_PREFIXES`. Each PUT atomically writes via
`config_writer.atomic_write_yaml` and appends a row to the
`config_changes` audit table.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", tags=["config"])

# Tighter than `Exception:` on `await req.json()` — these are the only
# things the parser raises in practice.
_JSON_PARSE_ERRORS = (json.JSONDecodeError, UnicodeDecodeError, ValueError)


async def _parse_json_body(req: Request) -> Any:
    try:
        return await req.json()
    except _JSON_PARSE_ERRORS:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from None


_KNOWN_SELFCARE_CATEGORIES = {"meds", "meals", "water", "movement"}


def _format_validation_errors(e: ValidationError) -> List[Dict[str, Any]]:
    """Strip non-JSON-serializable bits (e.g. raw `ValueError` in `ctx`)
    out of a Pydantic V2 errors() list before handing it to FastAPI.
    Without this, an `HTTPException(detail=e.errors())` 500s in the
    JSONResponse render step.
    """
    out: List[Dict[str, Any]] = []
    for err in e.errors():
        clean = {
            "type": err.get("type"),
            "loc": list(err.get("loc", ())),
            "msg": err.get("msg"),
        }
        # `input` is usually JSON-friendly (str/int/list) but stringify
        # defensively so an exotic value can't break the response.
        if "input" in err:
            try:
                json.dumps(err["input"])
                clean["input"] = err["input"]
            except (TypeError, ValueError):
                clean["input"] = repr(err["input"])
        out.append(clean)
    return out


# ---------------------------------------------------------------------------
# Feature flags — runtime nav gating
# ---------------------------------------------------------------------------


@router.get("/features")
async def get_features():
    """Runtime feature flags so the dashboard nav can hide features that are
    disabled on this install (otherwise the links 404 — `/api/workouts/*` and
    `/api/meals/*` aren't mounted when off, and the Finance surface is gated
    behind `JESS_ADVANCED`).

    Bearer-gated like its `/api/config/*` siblings — these are non-secret
    booleans, but keeping the whole prefix uniform avoids a one-off public hole.
    Read from `settings` (the live config source) so it matches what
    `api_routes.py` used to mount the routers at import time.
    """
    from orchestrator.config import settings

    return JSONResponse(
        {
            "workouts_enabled": settings.workouts_enabled,
            "meals_enabled": settings.meals_enabled,
            "jess_advanced": settings.jess_advanced,
        }
    )


# ---------------------------------------------------------------------------
# Identity panel
# ---------------------------------------------------------------------------


class IdentityIn(BaseModel):
    """Partial-update payload for `/api/config/identity`. All fields optional."""

    assistant_name: Optional[str] = Field(default=None, max_length=64)
    user_name: Optional[str] = Field(default=None, max_length=64)
    adhd_mode: Optional[bool] = None
    tone_preference: Optional[str] = Field(default=None, max_length=32)
    timezone: Optional[str] = Field(default=None, max_length=64)

    @field_validator("tone_preference")
    @classmethod
    def _tone_choice(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return v
        allowed = {"warm", "balanced", "direct"}
        if v not in allowed:
            raise ValueError(f"tone_preference must be one of {sorted(allowed)} or empty string")
        return v

    @field_validator("assistant_name", "user_name")
    @classmethod
    def _no_blank(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("name cannot be blank")
        return v

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return v
        try:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            ZoneInfo(v)
        except ZoneInfoNotFoundError as e:
            raise ValueError(f"unknown IANA timezone: {v!r}") from e
        return v


def _identity_snapshot() -> Dict[str, Any]:
    """Snapshot of identity-relevant profile fields for the GET response."""
    from orchestrator.user_profile import get_profile

    p = get_profile()
    return {
        "assistant_name": p.assistant_name,
        "user_name": p.user_name,
        "adhd_mode": p.adhd_mode,
        "tone_preference": p.tone_preference,
        "timezone": p.timezone,
    }


@router.get("/identity")
async def get_identity():
    return JSONResponse(_identity_snapshot())


@router.put("/identity")
async def put_identity(req: Request):
    body = await _parse_json_body(req)

    try:
        payload = IdentityIn.model_validate(body)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=_format_validation_errors(e)) from None

    updates = payload.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided")

    from orchestrator.config_writer import log_config_change
    from orchestrator.user_profile import save_profile_partial

    before = _identity_snapshot()
    try:
        save_profile_partial(updates)
    except OSError as e:
        logger.error(f"[CONFIG] Identity write failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to persist profile") from None

    after = _identity_snapshot()
    log_config_change("identity", before, after)
    logger.info(f"[CONFIG] Identity updated: {sorted(updates)}")
    return JSONResponse(after)


# ---------------------------------------------------------------------------
# Selfcare panel — categories.*
# ---------------------------------------------------------------------------


class ActiveHoursIn(BaseModel):
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")


class SelfcareCategoryIn(BaseModel):
    enabled: Optional[bool] = None
    interval_minutes: Optional[int] = Field(default=None, ge=0, le=24 * 60)
    interval_hours: Optional[float] = Field(default=None, ge=0, le=24)
    times: Optional[List[str]] = None
    active_hours: Optional[ActiveHoursIn] = None
    message_template: Optional[str] = Field(default=None, max_length=500)

    @field_validator("times")
    @classmethod
    def _times_format(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        if len(v) > 24:
            raise ValueError("at most 24 times per category")
        for t in v:
            if not isinstance(t, str) or len(t) != 5 or t[2] != ":":
                raise ValueError(f"time '{t}' must be HH:MM")
        return v


class SelfcareIn(BaseModel):
    categories: Dict[str, SelfcareCategoryIn]


@router.get("/selfcare")
async def get_selfcare():
    from orchestrator.selfcare_schedule import load_schedule

    return JSONResponse({"categories": load_schedule().get("categories", {})})


@router.put("/selfcare")
async def put_selfcare(req: Request):
    body = await _parse_json_body(req)

    try:
        payload = SelfcareIn.model_validate(body)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=_format_validation_errors(e)) from None

    unknown = sorted(set(payload.categories) - _KNOWN_SELFCARE_CATEGORIES)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown selfcare categories: {unknown}. Allowed: {sorted(_KNOWN_SELFCARE_CATEGORIES)}",
        )

    from orchestrator.config_writer import log_config_change
    from orchestrator.selfcare_schedule import load_schedule, save_schedule

    current = load_schedule()
    before = {"categories": current.get("categories", {})}

    # Merge incoming categories on top of current
    merged_categories = dict(current.get("categories", {}))
    for cat_name, cat_in in payload.categories.items():
        existing = dict(merged_categories.get(cat_name, {}))
        existing.update(cat_in.model_dump(exclude_none=True))
        merged_categories[cat_name] = existing

    new_schedule = {**current, "categories": merged_categories}
    try:
        saved = save_schedule(new_schedule)
    except (OSError, ValueError) as e:
        logger.error(f"[CONFIG] Selfcare write failed: {e}")
        raise HTTPException(status_code=400, detail=str(e)) from None

    after = {"categories": saved.get("categories", {})}
    log_config_change("selfcare", before, after)
    logger.info(f"[CONFIG] Selfcare updated: categories={sorted(payload.categories)}")
    return JSONResponse(after)


# ---------------------------------------------------------------------------
# Quiet hours panel
# ---------------------------------------------------------------------------


class QuietHoursIn(BaseModel):
    start: Optional[str] = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    end: Optional[str] = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    days: Optional[List[str]] = None

    @field_validator("days")
    @classmethod
    def _day_choice(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        allowed = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
        norm = []
        for d in v:
            if not isinstance(d, str) or d.lower() not in allowed:
                raise ValueError(f"day '{d}' must be one of {sorted(allowed)}")
            norm.append(d.lower())
        return norm


@router.get("/quiet_hours")
async def get_quiet_hours():
    from orchestrator.selfcare_schedule import quiet_hours

    return JSONResponse(quiet_hours())


@router.put("/quiet_hours")
async def put_quiet_hours(req: Request):
    body = await _parse_json_body(req)

    try:
        payload = QuietHoursIn.model_validate(body)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=_format_validation_errors(e)) from None

    updates = payload.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided")

    from orchestrator.config_writer import log_config_change
    from orchestrator.selfcare_schedule import load_schedule, save_schedule

    current = load_schedule()
    before = current.get("quiet_hours", {})

    merged_qh = {**before, **updates}
    new_schedule = {**current, "quiet_hours": merged_qh}
    try:
        saved = save_schedule(new_schedule)
    except (OSError, ValueError) as e:
        logger.error(f"[CONFIG] Quiet hours write failed: {e}")
        raise HTTPException(status_code=400, detail=str(e)) from None

    after = saved.get("quiet_hours", {})
    log_config_change("quiet_hours", before, after)
    logger.info(f"[CONFIG] Quiet hours updated: {sorted(updates)}")
    return JSONResponse(after)


# ---------------------------------------------------------------------------
# Routines panel — morning / evening / etc.
# ---------------------------------------------------------------------------


class RoutineTriggerIn(BaseModel):
    time: Optional[str] = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    days: Optional[List[str]] = None

    @field_validator("days")
    @classmethod
    def _day_choice(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        allowed = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
        norm = []
        for d in v:
            if not isinstance(d, str) or d.lower() not in allowed:
                raise ValueError(f"day '{d}' must be one of {sorted(allowed)}")
            norm.append(d.lower())
        return norm


class RoutineStepIn(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    label: str = Field(min_length=1, max_length=200)
    est_minutes: int = Field(default=5, ge=0, le=240)
    skippable: bool = True


class RoutineIn(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=200)
    trigger: Optional[RoutineTriggerIn] = None
    speaker: Optional[str] = Field(default=None, max_length=200)
    nudge_delay_minutes: Optional[int] = Field(default=None, ge=1, le=240)
    # Per-routine override of global ROUTINE_NUDGE_MAX (default 3).
    nudge_max: Optional[int] = Field(default=None, ge=1, le=20)
    # Per-routine override of global ROUTINE_AUTO_SKIP (default False).
    # True = on hitting nudge_max, advance past skippable steps instead of
    # ending the whole routine.
    auto_skip: Optional[bool] = None
    # 64-step cap defends against a 10k-step paste DoS — well above any
    # realistic routine.
    steps: Optional[List[RoutineStepIn]] = Field(default=None, max_length=64)


class RoutinesIn(BaseModel):
    routines: Dict[str, RoutineIn]


@router.get("/routines")
async def get_routines():
    from orchestrator.routines_config import list_routines_for_panel

    return JSONResponse(list_routines_for_panel())


@router.put("/routines")
async def put_routines(req: Request):
    body = await _parse_json_body(req)

    try:
        payload = RoutinesIn.model_validate(body)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=_format_validation_errors(e)) from None

    if not payload.routines:
        raise HTTPException(status_code=400, detail="routines must be a non-empty object")

    # Convert Pydantic models to plain dicts (excluding None) so the YAML
    # writer doesn't persist `key: null` for fields the user didn't touch.
    payload_dict: Dict[str, Any] = {"routines": {}}
    for rid, r in payload.routines.items():
        payload_dict["routines"][rid] = r.model_dump(exclude_none=True)

    from orchestrator.config_writer import log_config_change
    from orchestrator.routines_config import (
        list_routines_for_panel,
        reload_routines_and_reschedule,
        save_routines,
    )

    before = list_routines_for_panel()
    try:
        save_routines(payload_dict)
    except (OSError, ValueError) as e:
        logger.error(f"[CONFIG] Routines write failed: {e}")
        raise HTTPException(status_code=400, detail=str(e)) from None

    # Hot-reload routine_manager state and re-register APScheduler crons
    # so a time/day change takes effect without an orchestrator restart.
    try:
        reload_summary = await reload_routines_and_reschedule()
    except Exception as e:  # noqa: BLE001
        logger.error(f"[CONFIG] Routines reload/reschedule failed: {e}", exc_info=True)
        # The save already landed; don't 500 — surface the partial outcome.
        reload_summary = {"loaded": [], "rescheduled": [], "removed": [], "reload_error": str(e)}

    after = list_routines_for_panel()
    log_config_change("routines", before, after)
    logger.info(
        f"[CONFIG] Routines updated: ids={sorted(payload.routines)} "
        f"rescheduled={reload_summary.get('rescheduled')} removed={reload_summary.get('removed')}"
    )
    return JSONResponse({**after, "_reload": reload_summary})


# ---------------------------------------------------------------------------
# Speakers panel — per-category announcement routes
# ---------------------------------------------------------------------------


class SpeakersIn(BaseModel):
    routes: Dict[str, str]


@router.get("/speakers")
async def get_speakers():
    from orchestrator.announcement_routes import panel_view

    return JSONResponse(panel_view())


@router.put("/speakers")
async def put_speakers(req: Request):
    body = await _parse_json_body(req)

    try:
        payload = SpeakersIn.model_validate(body)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=_format_validation_errors(e)) from None

    from orchestrator.announcement_routes import CATEGORIES, panel_view, save_routes
    from orchestrator.config_writer import log_config_change

    # Reject unknown categories so the panel can't accidentally seed garbage
    # rows (different from the per-routine settings panel which keys on
    # arbitrary user-defined ids).
    unknown = sorted(set(payload.routes) - set(CATEGORIES) - {"default"})
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown speaker categories: {unknown}. Allowed: {list(CATEGORIES) + ['default']}",
        )

    before = panel_view()
    try:
        save_routes({"routes": dict(payload.routes)})
    except (OSError, ValueError) as e:
        logger.error(f"[CONFIG] Speakers write failed: {e}")
        raise HTTPException(status_code=400, detail=str(e)) from None

    after = panel_view()
    log_config_change("speakers", before, after)
    logger.info(f"[CONFIG] Speakers updated: categories={sorted(payload.routes)}")
    return JSONResponse(after)


@router.get("/speakers/discover")
async def discover_speakers():
    """Return all `media_player.*` HA entities for the panel autocomplete.
    Failure returns an empty list so the panel still works as free text."""
    from orchestrator.announcement_routes import discover_ha_speakers

    return JSONResponse({"speakers": await discover_ha_speakers()})


# ---------------------------------------------------------------------------
# Recurring reminders panel
# ---------------------------------------------------------------------------


class RecurringReminderIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    cron_expression: str = Field(min_length=1, max_length=200)
    target: str = Field(default="both", pattern=r"^(tts|push|both)$")
    days_of_week: Optional[List[str]] = None
    enabled: bool = True


class RecurringReminderPatch(BaseModel):
    text: Optional[str] = Field(default=None, min_length=1, max_length=500)
    cron_expression: Optional[str] = Field(default=None, min_length=1, max_length=200)
    target: Optional[str] = Field(default=None, pattern=r"^(tts|push|both)$")
    days_of_week: Optional[List[str]] = None
    enabled: Optional[bool] = None


@router.get("/recurring_reminders")
async def list_recurring():
    from orchestrator.recurring_reminders import list_rules

    return JSONResponse({"rules": list_rules()})


@router.post("/recurring_reminders")
async def create_recurring(req: Request):
    body = await _parse_json_body(req)

    try:
        payload = RecurringReminderIn.model_validate(body)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=_format_validation_errors(e)) from None

    from orchestrator.config_writer import log_config_change
    from orchestrator.recurring_reminders import RecurringReminderError, create_rule

    try:
        rule = create_rule(
            text=payload.text,
            cron_expression=payload.cron_expression,
            target=payload.target,
            days_of_week=payload.days_of_week,
            enabled=payload.enabled,
        )
    except RecurringReminderError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None

    log_config_change("recurring_reminders", None, rule)
    return JSONResponse(rule, status_code=201)


@router.put("/recurring_reminders/{rule_id}")
async def update_recurring(rule_id: str, req: Request):
    body = await _parse_json_body(req)

    try:
        payload = RecurringReminderPatch.model_validate(body)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=_format_validation_errors(e)) from None

    updates = payload.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided")

    from orchestrator.config_writer import log_config_change
    from orchestrator.recurring_reminders import RecurringReminderError, get_rule, update_rule

    before = get_rule(rule_id)
    if before is None:
        raise HTTPException(status_code=404, detail=f"Rule {rule_id} not found")

    try:
        rule = update_rule(rule_id, updates)
    except RecurringReminderError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None

    log_config_change("recurring_reminders", before, rule)
    return JSONResponse(rule)


@router.delete("/recurring_reminders/{rule_id}")
async def delete_recurring(rule_id: str):
    from orchestrator.config_writer import log_config_change
    from orchestrator.recurring_reminders import delete_rule, get_rule

    before = get_rule(rule_id)
    if before is None:
        raise HTTPException(status_code=404, detail=f"Rule {rule_id} not found")

    if not delete_rule(rule_id):
        raise HTTPException(status_code=404, detail=f"Rule {rule_id} not found")

    log_config_change("recurring_reminders", before, None)
    return JSONResponse({"ok": True, "id": rule_id})
