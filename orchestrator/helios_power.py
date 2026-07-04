"""
Helios wake-on-demand + manual sleep (PT-C).

Helios (the RTX 5090 box) runs only the model layer (vLLM / TTS / STT) and is
powered off most of the time to save electricity. Its NIC Wake-on-LAN is a dead
end (Aquantia ``atlantic`` driver), so remote power control is done by
power-cycling Helios via a TP-Link Tapo smart plug — driven entirely through
Home Assistant (reuses ``HA_URL`` / ``HA_TOKEN``; no python-kasa, no TP-Link
credentials).

Power semantics (BIOS ``AC Back = Last State``):
  * **Wake** = ``switch.turn_on`` the plug. If Helios was running when power was
    cut, restoring power auto-boots it. Triggered automatically from the
    brain-asleep chat path (debounced) and on demand via the API / tool.
  * **Sleep** = ``switch.turn_off`` the plug while Helios is running — a hard
    power-cut. Acceptable because Helios is now stateless: all DBs / ChromaDB
    live on Jupiter; it just reloads model servers on the next boot. Manual
    only — there is no idle auto-sleep.

Never raises: every entry point returns a dict and increments its metric exactly
once per call (matches the F-011 / F-013 manager pattern). When the feature is
disabled the functions no-op with a clear reason.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import httpx

from orchestrator.config import settings as _settings

logger = logging.getLogger(__name__)

# Short timeout: HA is on the LAN/tailnet and these are cheap REST calls. A wake
# is fire-and-forget from the chat path, so it must never stall a request.
_HA_TIMEOUT_SECONDS = 8.0

# Above this draw (watts) we treat the box as actually running, not just
# plugged-in-but-powered-down. A 5090 box idles well above this; a powered-off
# machine on a live plug draws only a few watts of standby.
_RUNNING_WATTS_THRESHOLD = 30.0

# Module-level debounce state: monotonic timestamp of the last *successful* wake.
# Repeated chat attempts while Helios is booting must not spam switch.turn_on.
_last_wake_monotonic: Optional[float] = None

# Status-poll noise control. The scheduler calls helios_power_status() every
# 60s, and under power tiering Helios spends most of its life asleep — an
# unreachable status read is the EXPECTED steady state, not an incident.
# Logging it at ERROR every minute produced ~5.7k errors/week of pure noise.
# Instead we track consecutive failures and log only on state *transitions*:
# once when reads start failing, once when they recover. The exception is the
# genuinely suspicious case (last successful read said the plug was ON, i.e.
# the box should be up, yet reads keep failing) which escalates to WARNING
# after a few consecutive polls and then re-warns only about once an hour.
_status_fail_count: int = 0  # consecutive helios_power_status() failures
_status_last_switch: Optional[str] = None  # switch state from the last successful read

# Plug said ON but status has been unreachable for this many consecutive polls
# → escalate once to WARNING (at 60s poll cadence this is ~5 minutes).
_UNEXPECTED_FAILURE_POLLS = 5
# ...and re-warn only every this-many further consecutive failures (~hourly).
_UNEXPECTED_REWARN_EVERY = 60


def _ha_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_settings.ha_token}",
        "Content-Type": "application/json",
    }


def reset_debounce() -> None:
    """Clear the wake debounce timer (test/diagnostic helper)."""
    global _last_wake_monotonic
    _last_wake_monotonic = None


def reset_status_state() -> None:
    """Clear the status-poll failure tracking (test/diagnostic helper)."""
    global _status_fail_count, _status_last_switch
    _status_fail_count = 0
    _status_last_switch = None


def _log_status_failure(exc: Exception) -> None:
    """Log a failed status read at a level proportional to how surprising it is.

    Called with `_status_fail_count` already incremented for this failure.
    - 1st failure after a healthy read: one INFO ("appears asleep") — or one
      WARNING if the plug was ON at the last successful read.
    - Plug-was-ON failures: escalate to WARNING once after
      `_UNEXPECTED_FAILURE_POLLS` consecutive polls, then roughly hourly.
    - Everything else: DEBUG (invisible at the default log level).
    """
    err = f"{type(exc).__name__}: {exc}"
    plug_was_on = _status_last_switch == "on"

    if _status_fail_count == 1:
        if plug_was_on:
            logger.warning(
                "[HELIOS] Status read failed while plug was ON (%s) — watching for recovery; suppressing repeats",
                err,
            )
        else:
            logger.info(
                "[HELIOS] Status unreachable (%s) — Helios appears asleep; suppressing repeats until state changes",
                err,
            )
        return

    if plug_was_on and (
        _status_fail_count == _UNEXPECTED_FAILURE_POLLS
        or (_status_fail_count > _UNEXPECTED_FAILURE_POLLS and _status_fail_count % _UNEXPECTED_REWARN_EVERY == 0)
    ):
        logger.warning(
            "[HELIOS] Plug was ON at last successful read but status has been unreachable "
            "for %d consecutive polls (%s)",
            _status_fail_count,
            err,
        )
        return

    logger.debug("[HELIOS] Status still unreachable (%d consecutive: %s)", _status_fail_count, err)


async def wake_helios() -> Dict[str, Any]:
    """Power Helios on by turning the smart plug on via Home Assistant.

    Debounced: a call within ``HELIOS_WAKE_DEBOUNCE_SECONDS`` of a prior
    successful wake no-ops so repeated chat requests don't spam HA while the box
    boots (~2 min).

    Returns one of:
      {"ok": True, "action": "wake", "entity": "..."}
      {"ok": True, "skipped": "debounced", "retry_after_s": int}
      {"ok": False, "skipped": "disabled", "reason": "..."}
      {"ok": False, "error": "TypeName: msg"}
    """
    from orchestrator.metrics import HELIOS_WAKE_TOTAL

    global _last_wake_monotonic

    if not _settings.helios_wake_enabled:
        HELIOS_WAKE_TOTAL.labels(result="disabled").inc()
        return {
            "ok": False,
            "skipped": "disabled",
            "reason": "HELIOS_WAKE_ENABLED is false",
        }

    now = time.monotonic()
    debounce = _settings.helios_wake_debounce_seconds
    if _last_wake_monotonic is not None and (now - _last_wake_monotonic) < debounce:
        retry_after = int(debounce - (now - _last_wake_monotonic))
        logger.info("[HELIOS] Wake debounced (%ds remaining)", retry_after)
        HELIOS_WAKE_TOTAL.labels(result="debounced").inc()
        return {"ok": True, "skipped": "debounced", "retry_after_s": retry_after}

    # Optimistically claim the debounce slot BEFORE the await: two near-
    # simultaneous wakes (e.g. a burst of chat hitting the asleep path) would
    # otherwise both pass the check above and double-fire turn_on. Restore the
    # prior value on failure so a failed wake doesn't block an immediate retry.
    prev_wake = _last_wake_monotonic
    _last_wake_monotonic = now

    entity = _settings.helios_plug_entity
    try:
        async with httpx.AsyncClient(timeout=_HA_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{_settings.ha_url.rstrip('/')}/api/services/switch/turn_on",
                headers=_ha_headers(),
                json={"entity_id": entity},
            )
            resp.raise_for_status()
    except Exception as e:  # noqa: BLE001 — never raise into the caller
        _last_wake_monotonic = prev_wake
        logger.error("[HELIOS] Wake failed for %s: %s: %s", entity, type(e).__name__, e)
        HELIOS_WAKE_TOTAL.labels(result="error").inc()
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    logger.info("[HELIOS] Wake sent — turned on plug %s", entity)
    HELIOS_WAKE_TOTAL.labels(result="ok").inc()
    return {"ok": True, "action": "wake", "entity": entity}


async def sleep_helios() -> Dict[str, Any]:
    """Power Helios off by turning the smart plug off via Home Assistant.

    This is a hard power-cut (see module docstring) — manual only. Returns:
      {"ok": True, "action": "sleep", "entity": "..."}
      {"ok": False, "skipped": "disabled", "reason": "..."}
      {"ok": False, "error": "TypeName: msg"}
    """
    from orchestrator.metrics import HELIOS_SLEEP_TOTAL

    if not _settings.helios_wake_enabled:
        HELIOS_SLEEP_TOTAL.labels(result="disabled").inc()
        return {
            "ok": False,
            "skipped": "disabled",
            "reason": "HELIOS_WAKE_ENABLED is false",
        }

    entity = _settings.helios_plug_entity
    try:
        async with httpx.AsyncClient(timeout=_HA_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{_settings.ha_url.rstrip('/')}/api/services/switch/turn_off",
                headers=_ha_headers(),
                json={"entity_id": entity},
            )
            resp.raise_for_status()
    except Exception as e:  # noqa: BLE001 — never raise into the caller
        logger.error("[HELIOS] Sleep failed for %s: %s: %s", entity, type(e).__name__, e)
        HELIOS_SLEEP_TOTAL.labels(result="error").inc()
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    logger.info("[HELIOS] Sleep sent — hard-cut plug %s", entity)
    HELIOS_SLEEP_TOTAL.labels(result="ok").inc()
    return {"ok": True, "action": "sleep", "entity": entity}


async def _get_state(client: httpx.AsyncClient, entity_id: str) -> Optional[Dict[str, Any]]:
    """Fetch one HA entity state; return None on any failure (caller handles)."""
    resp = await client.get(
        f"{_settings.ha_url.rstrip('/')}/api/states/{entity_id}",
        headers=_ha_headers(),
    )
    resp.raise_for_status()
    return resp.json()


async def helios_power_status() -> Dict[str, Any]:
    """Read the plug switch state + power sensor and infer running/asleep.

    Returns:
      {"ok": True, "switch": "on|off|unknown", "watts": float|None,
       "inferred": "running|asleep|unknown", "entity": "..."}
      {"ok": False, "skipped": "disabled", "reason": "..."}
      {"ok": False, "error": "TypeName: msg"}
    """
    from orchestrator.metrics import HELIOS_STATUS_TOTAL

    if not _settings.helios_wake_enabled:
        HELIOS_STATUS_TOTAL.labels(result="disabled").inc()
        return {
            "ok": False,
            "skipped": "disabled",
            "reason": "HELIOS_WAKE_ENABLED is false",
        }

    global _status_fail_count, _status_last_switch

    switch_entity = _settings.helios_plug_entity
    power_entity = _settings.helios_plug_power_sensor
    try:
        async with httpx.AsyncClient(timeout=_HA_TIMEOUT_SECONDS) as client:
            switch_state = await _get_state(client, switch_entity)
            power_state = await _get_state(client, power_entity)
    except Exception as e:  # noqa: BLE001 — never raise into the caller
        _status_fail_count += 1
        _log_status_failure(e)
        HELIOS_STATUS_TOTAL.labels(result="error").inc()
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    switch = (switch_state or {}).get("state", "unknown")

    # Reads are working again — log the recovery exactly once per outage.
    if _status_fail_count:
        logger.info(
            "[HELIOS] Status reads recovered after %d consecutive failure(s) — switch=%s",
            _status_fail_count,
            switch,
        )
        _status_fail_count = 0
    _status_last_switch = switch

    watts: Optional[float] = None
    raw_watts = (power_state or {}).get("state")
    try:
        if raw_watts not in (None, "", "unknown", "unavailable"):
            watts = float(raw_watts)
    except (TypeError, ValueError):
        watts = None

    if switch != "on":
        inferred = "asleep"
    elif watts is None:
        inferred = "unknown"
    elif watts >= _RUNNING_WATTS_THRESHOLD:
        inferred = "running"
    else:
        inferred = "asleep"

    from orchestrator.metrics import HELIOS_PLUG_WATTS, HELIOS_RUNNING

    if watts is not None:
        HELIOS_PLUG_WATTS.set(watts)
    HELIOS_RUNNING.set(1 if inferred == "running" else 0)
    HELIOS_STATUS_TOTAL.labels(result="ok").inc()

    return {
        "ok": True,
        "switch": switch,
        "watts": watts,
        "inferred": inferred,
        "entity": switch_entity,
    }
