"""Coder-box gate routes (coder_gate.py has the design and the rules).

Gate (served only to requests that arrived on the gate address from the coder
subnet; BearerAuthMiddleware sends nothing else from coder-net and 404s these
paths everywhere else):
  POST /api/coder/v1/chat/completions   coder token
  GET  /api/coder/v1/models             coder token

Sessions (normal API_TOKEN routes, for scripts/jess-coder on the host):
  POST   /api/coder/sessions            mint a session token  {label?, hours?}
  GET    /api/coder/sessions            live sessions (no tokens)
  DELETE /api/coder/sessions/{id}       revoke

All answer 404 while CODER_ENABLED is off. Gate errors use the OpenAI error
shape ({"error": {"message", "type"}}) because Qwen Code reads that; the
session routes use the usual {ok, error} envelope.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from orchestrator import coder_gate, shared
from orchestrator.config import settings as _settings
from orchestrator.metrics import (
    CODER_BRAIN_TOKENS_TOTAL,
    CODER_DROPPED_KEYS_TOTAL,
    CODER_REJECTED_TOTAL,
    CODER_REQUESTS_TOTAL,
    CODER_SESSIONS_TOTAL,
)
from orchestrator.schemas import CoderSessionRequest, error_response, ok_response

logger = logging.getLogger(__name__)
router = APIRouter()

_NOT_FOUND = {"error": "Not found"}


def _error(status: int, message: str, err_type: str, retry_after: Optional[int] = None) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after else None
    return JSONResponse({"error": {"message": message, "type": err_type}}, status_code=status, headers=headers)


def _bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


async def _read_capped(request: Request, limit: int) -> Optional[bytes]:
    """The body, or None once it passes ``limit`` (chunked bodies included)."""
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            return None
    return bytes(buf)


async def _gate_token(request: Request) -> Optional[Dict[str, Any]]:
    return await asyncio.to_thread(coder_gate.lookup_token, _bearer(request))


def _where(exc: BaseException) -> str:
    """file:line of the innermost frame (no exception message: it could carry content)."""
    tb = exc.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    return f"{tb.tb_frame.f_code.co_filename.rsplit('/', 1)[-1]}:{tb.tb_lineno}" if tb else "?"


@router.get(coder_gate.GATE_MODELS_PATH, response_model=None)
async def coder_models(request: Request) -> Response:
    if not coder_gate.gate_enabled() or not coder_gate.arrived_on_gate(request.scope):
        return JSONResponse(_NOT_FOUND, status_code=404)
    if await _gate_token(request) is None:
        return _error(401, "invalid or expired coder token", "unauthorized")
    model = shared.MODEL_NAME or "default"
    return JSONResponse({"object": "list", "data": [{"id": model, "object": "model", "owned_by": "jess"}]})


@router.post(coder_gate.GATE_CHAT_PATH, response_model=None)
async def coder_chat(request: Request) -> Response:
    if not coder_gate.gate_enabled() or not coder_gate.arrived_on_gate(request.scope):
        CODER_REQUESTS_TOTAL.labels(origin="none", result="disabled").inc()
        return JSONResponse(_NOT_FOUND, status_code=404)
    row = await _gate_token(request)
    if row is None:
        CODER_REQUESTS_TOTAL.labels(origin="none", result="unauthorized").inc()
        return _error(401, "invalid or expired coder token", "unauthorized")
    origin, token_id = row["kind"], row["id"]

    def count(result: str) -> None:
        CODER_REQUESTS_TOTAL.labels(origin=origin, result=result).inc()

    if not coder_gate.budget_left(row):
        count("budget")
        return _error(403, "this coder token has used its budget", "budget_exhausted")
    # Admission before the body is read, so a flood can't pile bodies up in RAM.
    try:
        admission = coder_gate.admit(token_id)
    except coder_gate.GateBusy:
        count("busy")
        return _error(429, "too many coder requests in flight; try again shortly", "rate_limit_error", retry_after=15)
    try:
        return await _serve_chat(request, admission, token_id, count)
    finally:
        admission.release()


async def _serve_chat(request: Request, admission: coder_gate.Admission, token_id: str, count) -> Response:
    try:
        await asyncio.to_thread(coder_gate.count_request, token_id)
    except Exception as exc:  # noqa: BLE001 — budgets are re-checked before the brain anyway
        logger.error("[CODER] Request not counted for token %s: %s", token_id, type(exc).__name__)
    # Parsing JSON runs on the event loop Jess shares, so each token gets a byte budget.
    allowance = coder_gate.bytes_allowance(token_id)
    limit = min(coder_gate.MAX_BODY_BYTES, allowance)
    raw = await _read_capped(request, limit)
    if raw is None:
        coder_gate.note_bytes(token_id, limit)
        if limit < coder_gate.MAX_BODY_BYTES:
            count("busy")
            return _error(429, "this token sent too much data this minute", "rate_limit_error", retry_after=30)
        count("too_large")
        return _error(413, "request body too large", "too_large")
    coder_gate.note_bytes(token_id, len(raw))
    try:
        body = json.loads(raw)
        up, dropped = coder_gate.sanitize_request(body, _settings.coder_max_tokens_per_request)
    except (ValueError, RecursionError, OverflowError, coder_gate.GateRejected) as exc:
        reason = exc.reason if isinstance(exc, coder_gate.GateRejected) else "bad_json"
        CODER_REJECTED_TOTAL.labels(reason=reason).inc()
        count("rejected")
        logger.info("[CODER] Request refused by the filter: %s (token %s)", reason, token_id)
        if await asyncio.to_thread(coder_gate.note_rejection, token_id):
            logger.warning("[CODER] Token %s revoked after %d refused requests", token_id, coder_gate.MAX_REJECTIONS)
        return _error(400, f"request refused by the coder gate ({reason})", "invalid_request_error")
    del raw
    wants_stream = isinstance(body, dict) and body.get("stream") is True
    del body
    for key in dropped:
        CODER_DROPPED_KEYS_TOTAL.labels(key=coder_gate.dropped_key_label(key)).inc()

    async def still_usable() -> bool:
        return await asyncio.to_thread(coder_gate.token_still_usable, token_id)

    try:
        chunks, usage = await admission.run(up, still_usable)
    except coder_gate.Superseded:
        count("superseded")
        return _error(409, "replaced by a newer request from the same token", "superseded")
    except coder_gate.TokenRevoked:
        count("revoked")
        return _error(401, "the coder token was revoked or ran out while waiting", "unauthorized")
    except coder_gate.GateBusy:
        count("busy")
        return _error(429, "the brain is busy with chat; try again shortly", "rate_limit_error", retry_after=15)
    except coder_gate.BrainUnavailable:
        count("brain_unavailable")
        return _error(
            503, "the brain is not reachable yet (Helios asleep or loading)", "brain_unavailable", retry_after=30
        )
    except coder_gate.UpstreamError as exc:
        await _record_usage(token_id, 0, 0)
        if exc.err_type in coder_gate.PASS_THROUGH_ERRORS:
            count("context_too_long")
            return _error(400, coder_gate.PASS_THROUGH_ERRORS[exc.err_type], exc.err_type)
        if 400 <= exc.status < 500:
            count("client_error")
            logger.info("[CODER] Brain refused a request: %s (token %s)", exc.status, token_id)
            return _error(400, "the brain refused this request", "invalid_request_error")
        count("upstream_error")
        logger.warning("[CODER] Brain error %s for token %s", exc.status, token_id)
        return _error(502, "the brain returned an error", "upstream_error")
    except Exception as exc:  # noqa: BLE001 — never an unmetered 500
        count("internal")
        logger.error("[CODER] Unexpected gate error %s at %s (token %s)", type(exc).__name__, _where(exc), token_id)
        return _error(502, "the coder gate hit an internal error", "internal")

    await _record_usage(token_id, usage["prompt_tokens"], usage["completion_tokens"])
    CODER_BRAIN_TOKENS_TOTAL.labels(type="prompt").inc(usage["prompt_tokens"])
    CODER_BRAIN_TOKENS_TOTAL.labels(type="completion").inc(usage["completion_tokens"])
    count("ok")
    if wants_stream:
        return Response(coder_gate.sse_body(chunks), media_type="text/event-stream")
    return JSONResponse(coder_gate.assemble(chunks, usage))


async def _record_usage(token_id: str, prompt_tokens: int, completion_tokens: int) -> None:
    """Best effort: a locked DB must not throw away a reply the brain already made."""
    try:
        await asyncio.to_thread(coder_gate.record_usage, token_id, prompt_tokens, completion_tokens)
    except Exception as exc:  # noqa: BLE001
        logger.error("[CODER] Usage not recorded for token %s: %s", token_id, type(exc).__name__)


# ----------------------------------------------------------------- sessions


@router.post("/api/coder/sessions", response_model=None)
async def mint_session(request: Request) -> Response:
    if not coder_gate.gate_enabled():
        return error_response("Not found", 404)
    raw = await request.body()
    try:
        data = json.loads(raw) if raw.strip() else {}
        body = CoderSessionRequest.model_validate(data)
    except (ValueError, RecursionError, ValidationError):
        return error_response("body: {label?: a-z 0-9 and dashes (max 40), hours?: positive number}", 400)
    hours = min(
        max(body.hours if body.hours is not None else _settings.coder_session_hours, 0.25),
        _settings.coder_session_hours,
    )
    result = await asyncio.to_thread(coder_gate.mint_token, "session", body.label, hours)
    CODER_SESSIONS_TOTAL.labels(action="minted").inc()
    logger.info("[CODER] Session %s minted, expires %s", result["id"], result["expires_at"])
    return JSONResponse({"ok": True, **result}, status_code=201)


@router.get("/api/coder/sessions", response_model=None)
async def list_sessions() -> Response:
    if not coder_gate.gate_enabled():
        return error_response("Not found", 404)
    return ok_response(sessions=await asyncio.to_thread(coder_gate.list_tokens, "session"))


@router.delete("/api/coder/sessions/{session_id}", response_model=None)
async def revoke_session(session_id: str) -> Response:
    if not coder_gate.gate_enabled():
        return error_response("Not found", 404)
    if not coder_gate.valid_token_id(session_id):
        return error_response("bad session id", 400)
    if not await asyncio.to_thread(coder_gate.revoke_token, session_id, "session"):
        return error_response("Not found", 404)
    CODER_SESSIONS_TOTAL.labels(action="revoked").inc()
    logger.info("[CODER] Session %s revoked", session_id)
    return ok_response(revoked=session_id)
