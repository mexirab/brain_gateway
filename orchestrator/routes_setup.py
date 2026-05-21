"""
First-boot setup-wizard backend — `/api/setup/*`.

Backs the (future) browser setup wizard. Three endpoints:

- GET  /api/setup/status   → has the wizard been completed?
- GET  /api/setup/hardware → the cached hardware scan (for the model step)
- POST /api/setup/complete → mark the wizard done

All endpoints inherit bearer auth via `BearerAuthMiddleware` — they are NOT in
`PUBLIC_PREFIXES`. The orchestrator already refuses to boot without
`API_TOKEN`, so a token always exists by the time these are reachable.

State lives in two JSON files under the `/app/data` bind mount (no DB schema
change — matches the repo's file-config pattern, e.g. `selfcare_schedule.yaml`):

- `setup_state.json`   — written here (`{setup_completed, completed_at}`)
- `hardware_scan.json` — written HOST-SIDE by `scripts/detect_hardware.sh
  --json` (the orchestrator container is CPU-only and cannot run nvidia-smi);
  read-only here.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from orchestrator import setup_env

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/setup", tags=["setup"])

# /app/data is the orchestrator's rw bind mount (docker-compose.yml:
# ${GATEWAY_ROOT_PATH}/data/app:/app/data:rw). Module-level so tests can patch.
_SETUP_STATE_PATH = "/app/data/setup_state.json"
_HARDWARE_SCAN_PATH = "/app/data/hardware_scan.json"


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    """Write a JSON file atomically (tmpfile + os.replace + fsync).

    JSON sibling of `config_writer.atomic_write_yaml` — no torn write if the
    process dies mid-flush.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    """Read a JSON object from `path`. None if absent/unreadable/not an object."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        logger.warning("[SETUP] could not read %s: %s", path, e)
        return None
    return data if isinstance(data, dict) else None


def _setup_state() -> Dict[str, Any]:
    """Current setup state, or an empty dict if the wizard hasn't run."""
    return _read_json(_SETUP_STATE_PATH) or {}


def is_first_boot() -> bool:
    """True if the setup wizard has not been completed. Used by the startup log.

    A corrupt/unreadable setup_state.json degrades to True (treated as first
    boot) — safe for an informational log line; the file self-heals on the
    next POST /complete.
    """
    return not bool(_setup_state().get("setup_completed"))


@router.get("/status")
async def get_setup_status():
    """Report whether the first-boot setup wizard has been completed."""
    state = _setup_state()
    return JSONResponse(
        {
            "ok": True,
            "setup_completed": bool(state.get("setup_completed")),
            "completed_at": state.get("completed_at"),
        }
    )


@router.get("/hardware")
async def get_hardware_scan():
    """Return the cached hardware scan that feeds the wizard's model step.

    The scan is produced host-side by `scripts/detect_hardware.sh --json` — the
    orchestrator container has no GPU access and cannot detect hardware itself.
    """
    scan = _read_json(_HARDWARE_SCAN_PATH)
    if scan is None:
        return JSONResponse(
            {
                "ok": True,
                "available": False,
                "hint": (
                    "No hardware scan found. On the host, run: "
                    "bash scripts/detect_hardware.sh --json data/app/hardware_scan.json "
                    "(data/app/ may be root-owned once the orchestrator has run — "
                    "use sudo if the write is denied)."
                ),
            }
        )
    return JSONResponse({"ok": True, "available": True, "scan": scan})


# --------------------------------------------------------------------------
# Env-overrides endpoints — first-boot wizard writes credential/config values
# into `setup_overrides.env` (see `orchestrator/setup_env.py`). All write paths
# are locked the moment `setup_completed` flips true — the wizard window is
# the only attack surface. Reads + validation remain available afterwards so
# the operator can introspect what the wizard set.
# --------------------------------------------------------------------------


class _SetEnvBody(BaseModel):
    values: Dict[str, str]


class _ValidateBody(BaseModel):
    service: str
    values: Dict[str, str]


def _ensure_first_boot() -> None:
    """Raise 410 Gone if setup is already complete — kills the write window."""
    if not is_first_boot():
        raise HTTPException(
            status_code=410,
            detail="setup already completed — env overrides are locked",
        )


@router.get("/env")
async def get_setup_env():
    """Return the per-key state for every allow-listed env override.

    Secret keys report `present` only; non-secret keys include their current
    value (so the wizard can pre-fill inputs on re-entry).
    """
    return JSONResponse(
        {
            "ok": True,
            "locked": not is_first_boot(),
            "restart_required": setup_env.is_dirty_since_boot(),
            "keys": setup_env.key_status_map(),
        }
    )


@router.post("/env")
async def post_setup_env(body: _SetEnvBody):
    """Write one or more env overrides. First-boot only.

    The lock guards the first-boot check + write as a unit so a concurrent
    `POST /complete` can't slip in between.
    """
    if not body.values:
        raise HTTPException(status_code=400, detail="`values` must not be empty")
    async with setup_env._write_lock:
        _ensure_first_boot()
        try:
            written = setup_env.set_keys(body.values)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    logger.info(
        "[SETUP] env override write: %s",
        setup_env.redact_for_log(body.values),
    )
    return JSONResponse(
        {
            "ok": True,
            "written": written,
            "restart_required": True,
        }
    )


@router.delete("/env/{key}")
async def delete_setup_env(key: str = PathParam(..., min_length=1, max_length=64)):
    """Remove a single key from the overrides file. First-boot only."""
    async with setup_env._write_lock:
        _ensure_first_boot()
        try:
            existed = setup_env.delete_key(key)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    logger.info("[SETUP] env override delete: %s (existed=%s)", key, existed)
    return JSONResponse({"ok": True, "key": key, "removed": existed, "restart_required": existed})


@router.post("/env/validate")
async def post_setup_env_validate(body: _ValidateBody):
    """Live-test a credential/config combo without persisting it.

    Not locked by `setup_completed` — read-only against the external service,
    useful for re-checking a stored token. Doesn't accept arbitrary keys: the
    `values` dict is filtered to the allow-list before being sent to the
    validator (defence-in-depth against the validator being tricked into using
    attacker-controlled URLs/headers).
    """
    filtered = {k: v for k, v in body.values.items() if setup_env.is_allowed(k)}
    ok, detail = await setup_env.validate_service(body.service, filtered)
    return JSONResponse({"ok": ok, "detail": detail})


@router.post("/complete")
async def post_setup_complete():
    """Mark the setup wizard complete and persist the timestamp.

    Idempotent — a re-POST keeps the original `completed_at` rather than
    clobbering it with a fresh timestamp.

    Held under `setup_env._write_lock` so it can't race a concurrent
    `POST /env` past the first-boot check.
    """
    async with setup_env._write_lock:
        existing = _setup_state()
        completed_at = existing.get("completed_at")
        if not (existing.get("setup_completed") and completed_at):
            completed_at = datetime.now(UTC).isoformat()
        state = {"setup_completed": True, "completed_at": completed_at}
        _atomic_write_json(_SETUP_STATE_PATH, state)
    logger.info("[SETUP] Setup wizard marked complete")
    return JSONResponse({"ok": True, **state})
