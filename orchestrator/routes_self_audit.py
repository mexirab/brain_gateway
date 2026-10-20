"""Alertmanager -> self-investigator trigger (self-monitoring step 2).

``POST /api/self_audit/alert`` takes Alertmanager's standard webhook JSON and
starts a read-only investigation (``jobs_self_audit.run_self_audit(trigger=
"alert")``) in the background for the first firing, allow-listed alert that is
not rate-limited.

Auth: behind ``BearerAuthMiddleware`` like every other route. Alertmanager
holds ``SELF_AUDIT_WEBHOOK_TOKEN``, which the middleware accepts on this one
exact path only, so its config never carries ``API_TOKEN``. Body capped at
64 KB (``_PATH_UPLOAD_LIMITS``, Content-Length required).

Everything in the payload is untrusted (a Loki-ruler alert can carry log
text): only an ``alertname`` from ``ALERT_TRIGGERS`` selects what runs; labels
and annotations are size-capped here and defanged + framed before they reach
the model. Nothing from the payload is logged except the alertname.

Responses: 409 when the feature is off (misconfiguration should be visible
in Alertmanager), 400 on a malformed body, otherwise 200/202 — ignored,
rate-limited and busy deliveries are 200 so Alertmanager doesn't retry them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from orchestrator.config import settings
from orchestrator.metrics import SELF_AUDIT_ALERT_TRIGGER_TOTAL

logger = logging.getLogger(__name__)

router = APIRouter()

# Alerts worth an investigation: Helios/brain health and Jess-health signals
# the investigator can actually look into from logs + code. Paging "box is
# down" alerts (OrchestratorDown, NodeDown, backups) are left out on purpose.
#
# Not BrainOnCPU / BrainCudaInitFailed / HeliosGpuDriverBroken: the brain is
# on CPU (~6 tok/s) or about to be, so an investigation would only time out
# while occupying the degraded slot. Those need a human (usually a reboot).
ALERT_TRIGGERS = frozenset(
    {
        "HeliosHardwareFault",
        "ChatStreamTruncating",
        "EmptyRepliesReachingUsers",
        "ToolCallsSilentlyDropped",
        "EmbeddingErrors",
        "AutoLearnFailing",
        "SchedulerJobsMissed",
        "HeliosLogsSilent",
    }
)

MAX_BODY_BYTES = 64 * 1024
MAX_ALERTS_READ = 20
PER_ALERT_COOLDOWN_S = 6 * 3600
MAX_PER_DAY = 6
_MAX_KEYS = 30
_MAX_VALUE_CHARS = 500

# RAM-only rate-limit state (a restart resets it — acceptable: at worst one
# extra investigation per alert after a deploy).
_last_run: dict[str, float] = {}
_runs_today: list[float] = []
_tasks: set[asyncio.Task] = set()


def reset_rate_limits() -> None:
    """Tests only."""
    _last_run.clear()
    _runs_today.clear()


def _capped_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in list(value.items())[:_MAX_KEYS]:
        out[str(k)[:100]] = str(v)[:_MAX_VALUE_CHARS]
    return out


def _result(result: str, status: int, **extra: Any) -> JSONResponse:
    SELF_AUDIT_ALERT_TRIGGER_TOTAL.labels(result=result).inc()
    return JSONResponse({"ok": status < 400, "result": result, **extra}, status_code=status)


@router.post("/api/self_audit/alert", response_model=None)
async def self_audit_alert(request: Request) -> JSONResponse:
    """Alertmanager webhook -> background read-only investigation.

    The body is parsed by hand rather than with a Pydantic model on purpose:
    it's an untrusted, loosely-shaped payload, and every field we keep is
    capped explicitly below.

    Known narrow race: if the cron takes the audit lock between the busy check
    and the task start, the task returns "busy" after the cooldown was spent.
    Alertmanager's 4 h repeat retries it later.
    """
    if not (settings.self_audit_enabled and settings.jess_advanced and settings.self_audit_investigate):
        return _result("disabled", 409)

    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return _result("bad_request", 413)
    try:
        payload = json.loads(body or b"{}")
        alerts = payload.get("alerts") if isinstance(payload, dict) else None
        if not isinstance(alerts, list):
            raise ValueError("alerts must be a list")
    except (ValueError, UnicodeDecodeError):
        return _result("bad_request", 400)

    candidates: list[tuple[str, dict[str, Any]]] = []
    for alert in alerts[:MAX_ALERTS_READ]:
        if not isinstance(alert, dict) or alert.get("status") != "firing":
            continue
        labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
        name = labels.get("alertname")
        if isinstance(name, str) and name in ALERT_TRIGGERS:
            candidates.append((name, alert))
    if not candidates:
        return _result("ignored", 200)

    now = time.monotonic()
    _runs_today[:] = [t for t in _runs_today if now - t < 86400]
    if len(_runs_today) >= MAX_PER_DAY:
        logger.info("[SELF_AUDIT] Alert trigger rate-limited (daily cap %d)", MAX_PER_DAY)
        return _result("rate_limited", 200)
    pick = next(((n, a) for n, a in candidates if now - _last_run.get(n, -1e12) >= PER_ALERT_COOLDOWN_S), None)
    if pick is None:
        logger.info("[SELF_AUDIT] Alert trigger rate-limited (per-alert cooldown)")
        return _result("rate_limited", 200)

    from orchestrator import jobs_self_audit

    if jobs_self_audit._AUDIT_LOCK.locked():
        return _result("busy", 200)

    name, alert = pick
    _last_run[name] = now
    _runs_today.append(now)
    context = {
        "alertname": name,
        "labels": _capped_map(alert.get("labels")),
        "annotations": _capped_map(alert.get("annotations")),
        "startsAt": str(alert.get("startsAt") or "")[:40],
    }
    task = asyncio.create_task(jobs_self_audit.run_self_audit(trigger="alert", alert=context))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    # log-safe: alertname is from the ALERT_TRIGGERS allow-list
    logger.info("[SELF_AUDIT] Investigation started for alert %s", name)
    return _result("accepted", 202, alertname=name)
